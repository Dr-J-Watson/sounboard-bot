"""
Module de gestion des routines (automatisations) pour le bot Soundboard.

Ce module gère les routines automatisées qui peuvent être déclenchées par :
- Des timers (intervalles de temps)
- Des événements vocaux (join, leave, move)

Les routines peuvent avoir des conditions (utilisateur, channel, rôle, heure, date)
et exécuter des actions (jouer un son, envoyer un message, attendre).

Auteur: Soundboard Bot
"""

import discord
import asyncio
import logging
import random
import re
import time
import datetime
import os
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass

from config import Config
from blocks import (
    ACTION_BLOCKS,
    CONDITION_BLOCKS,
    WEEKDAYS,
    action_by_verb,
    all_action_verbs,
    all_condition_aliases,
    condition_by_alias,
    describe_action,
    describe_condition,
    format_duration,
    parse_duration_range,
    parse_duration_seconds,
)

# Ces noms sont réexportés : bot.py les importe depuis ce module.
__all__ = [
    "RoutineManager",
    "RoutineContext",
    "ACTION_BLOCKS",
    "CONDITION_BLOCKS",
    "WEEKDAYS",
    "describe_action",
    "describe_condition",
    "format_duration",
    "parse_duration_range",
    "parse_duration_seconds",
]

logger = logging.getLogger(__name__)



@dataclass
class RoutineContext:
    """
    Contexte d'exécution d'une routine.
    
    Contient les informations sur le membre, le channel et le serveur
    impliqués dans le déclenchement de la routine.
    """
    guild: discord.Guild
    channel: Optional[discord.VoiceChannel] = None
    member: Optional[discord.Member] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convertit le contexte en dictionnaire."""
        return {
            'guild': self.guild,
            'channel': self.channel,
            'member': self.member
        }


class RoutineManager:
    """
    Gestionnaire des routines automatisées.
    
    Gère le chargement, l'exécution et le cycle de vie des routines.
    Supporte les déclencheurs timer et événements vocaux.
    
    Attributes:
        bot: Instance du bot Discord
        db: Gestionnaire de base de données
        routines: Liste des routines actives en mémoire
        timer_task: Tâche asyncio pour les routines timer
    """
    
    # Alias acceptés par le parser texte (/routine_cmd)
    TEXT_EVENT_ALIASES = {
        "join": "voice_join",
        "leave": "voice_leave",
        "move": "voice_move",
        "mute": "voice_mute",
        "unmute": "voice_unmute",
        "deafen": "voice_deafen",
        "undeafen": "voice_undeafen",
        "stream": "voice_stream_start",
        "stream_start": "voice_stream_start",
        "stream_stop": "voice_stream_stop",
        "video": "voice_video_start",
        "video_start": "voice_video_start",
        "video_stop": "voice_video_stop",
        "first_join": "voice_first_join",
        "first": "voice_first_join",
    }
    
    
    def __init__(self, bot, db):
        """
        Initialise le gestionnaire de routines.
        
        Args:
            bot: Instance du bot Discord
            db: Instance de DatabaseManager
        """
        self.bot = bot
        self.db = db
        self.routines: List[Dict] = []
        self.timer_task: Optional[asyncio.Task] = None
        self._running = False
        # Tâches d'actions en cours : gardées pour éviter que le ramasse-
        # miettes ne les supprime avant la fin, et pour pouvoir les annuler.
        self._action_tasks: set = set()

    def _spawn_actions(self, routine: Dict, context: Optional["RoutineContext"]) -> None:
        """
        Lance les actions d'une routine sans bloquer l'appelant.
        
        Une action `wait` gelait sinon la boucle timer entière (et les
        routines suivantes du même événement) pendant toute sa durée.
        
        Args:
            routine: Données de la routine
            context: Contexte d'exécution
        """
        task = asyncio.create_task(self._execute_actions(routine, context))
        self._action_tasks.add(task)
        task.add_done_callback(self._action_tasks.discard)

    async def load_routines(self) -> int:
        """
        Charge toutes les routines actives depuis la base de données.
        
        Returns:
            Nombre de routines chargées
        """
        # Conserver les dernières exécutions : load_routines() est rappelé à
        # chaque on_ready (donc à chaque reconnexion gateway). Repartir de 0
        # faisait redéclencher toutes les routines timer d'un coup.
        previous_runs = {r['id']: r.get('_last_run', 0) for r in self.routines}
        previous_states = {r['id']: r.get('_sub_state', {}) for r in self.routines}
        now = time.time()
        
        self.routines = []
        
        for guild in self.bot.guilds:
            try:
                routines = await self.db.get_routines(str(guild.id))
                for r in routines:
                    if r['active']:
                        # Une routine déjà connue garde son historique ;
                        # une nouvelle démarre maintenant plutôt que de se
                        # déclencher immédiatement.
                        r['_last_run'] = previous_runs.get(r['id'], now)
                        # Chaque déclencheur temporel garde son propre état
                        r['_sub_state'] = previous_states.get(r['id']) or {
                            i: {'_last_run': now}
                            for i, t_type, _ in self.iter_triggers(r)
                            if t_type in ('timer', 'schedule')
                        }
                        self.routines.append(r)
            except Exception as e:
                logger.error(f"Erreur lors du chargement des routines pour {guild.id}: {e}")
        
        logger.info(f"✅ {len(self.routines)} routine(s) chargée(s)")
        
        # Démarrer la boucle timer si nécessaire
        await self._start_timer_loop()
        
        return len(self.routines)

    async def _start_timer_loop(self) -> None:
        """Démarre la boucle de vérification des timers."""
        if self._running:
            return
            
        self._running = True
        
        if self.timer_task is None or self.timer_task.done():
            self.timer_task = self.bot.loop.create_task(self._timer_loop())
            logger.debug("Boucle timer démarrée")

    async def stop(self) -> None:
        """Arrête proprement le gestionnaire de routines."""
        self._running = False
        
        if self.timer_task and not self.timer_task.done():
            self.timer_task.cancel()
            try:
                await self.timer_task
            except asyncio.CancelledError:
                pass
        
        # Annuler les actions encore en attente (ex: un `wait` en cours)
        for task in list(self._action_tasks):
            task.cancel()
        if self._action_tasks:
            await asyncio.gather(*self._action_tasks, return_exceptions=True)
        
        logger.info("Gestionnaire de routines arrêté")

    async def _timer_loop(self) -> None:
        """
        Boucle principale pour les routines basées sur le temps.
        
        Vérifie chaque seconde si des routines timer doivent être exécutées.
        """
        while self._running and not self.bot.is_closed():
            try:
                current_time = time.time()
                
                for routine in self.routines:
                    for index, t_type, t_data in self.iter_triggers(routine):
                        if t_type == 'timer':
                            await self._process_timer_routine(
                                routine, current_time, index, t_data
                            )
                        elif t_type == 'schedule':
                            await self._process_schedule_routine(
                                routine, current_time, index, t_data
                            )
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Erreur dans la boucle timer: {e}", exc_info=True)
            
            await asyncio.sleep(1)

    async def _process_timer_routine(
        self,
        routine: Dict,
        current_time: float,
        index: int = 0,
        trigger_data: Optional[Dict] = None
    ) -> None:
        """
        Traite un déclencheur de type timer.
        
        Args:
            routine: Données de la routine
            current_time: Timestamp actuel
            index: Index du déclencheur dans la routine
            trigger_data: Données du déclencheur (celles de la routine par défaut)
        """
        if trigger_data is None:
            trigger_data = routine['trigger_data']
        
        state = self._trigger_state(routine, index)
        last_run = state.get('_last_run', 0)
        
        interval = self._resolve_interval(trigger_data, state)
        if interval <= 0:
            return
        
        # Vérifier si l'intervalle est écoulé
        if current_time - last_run < interval:
            return
        
        # Rechercher un contexte valide
        guild = self.bot.get_guild(int(routine['guild_id']))
        if not guild:
            return
        
        context = await self._find_valid_context(routine, guild)
        
        if context:
            # Marquer AVANT de lancer les actions : sinon une action longue
            # laisserait la boucle redéclencher la routine à la seconde
            # suivante.
            state['_last_run'] = current_time
            
            # Intervalle aléatoire : on retire au sort pour le prochain tour
            if trigger_data.get('interval_max') is not None:
                state.pop('_next_interval', None)
                next_interval = self._resolve_interval(trigger_data, state)
                logger.debug(
                    f"Timer routine '{routine['name']}' exécutée, "
                    f"prochain déclenchement dans {format_duration(next_interval)}"
                )
            else:
                logger.debug(f"Timer routine '{routine['name']}' exécutée")
            
            self._spawn_actions(routine, context)

    @staticmethod
    def iter_triggers(routine: Dict) -> List[Tuple[int, str, Dict]]:
        """
        Énumère les déclencheurs d'une routine.

        Une routine v2 porte une liste de déclencheurs dans
        trigger_data["triggers"]. Ils fonctionnent en OU : n'importe lequel
        déclenche la trame.

        Args:
            routine: Données de la routine

        Returns:
            Liste de tuples (index, type, données)
        """
        trigger_data = routine.get('trigger_data') or {}

        return [
            (i, sub.get('type'), sub.get('data') or {})
            for i, sub in enumerate(trigger_data.get('triggers', []))
        ]

    @staticmethod
    def _trigger_state(routine: Dict, index: int) -> Dict:
        """
        Récupère l'état d'exécution propre à un déclencheur.

        Chaque déclencheur temporel a besoin de son propre `_last_run` :
        deux timers dans la même routine ne se déclenchent pas ensemble.
        Pour une routine à déclencheur unique, l'état est porté par la
        routine elle-même, ce qui préserve le comportement existant.

        Args:
            routine: Données de la routine
            index: Index du déclencheur

        Returns:
            Le dictionnaire d'état à lire et écrire
        """
        states = routine.setdefault('_sub_state', {})
        return states.setdefault(index, {'_last_run': routine.get('_last_run', 0)})

    @staticmethod
    def _resolve_interval(trigger_data: Dict, state: Dict) -> int:
        """
        Détermine l'intervalle courant d'une routine timer, en secondes.

        Un intervalle fixe est renvoyé tel quel. Un intervalle aléatoire
        ("timer 10m-20m") est tiré une fois puis mémorisé sur la routine,
        pour que la boucle ne retire pas au sort à chaque seconde ; il est
        renouvelé après chaque déclenchement.

        Args:
            trigger_data: Données du déclencheur
            state: Dictionnaire d'état où mémoriser le tirage

        Returns:
            L'intervalle à respecter, en secondes
        """
        low = trigger_data.get('interval_min')
        high = trigger_data.get('interval_max')

        if low is not None and high is not None:
            cached = state.get('_next_interval')
            if cached is None:
                low, high = int(low), int(high)
                cached = random.randint(low, high) if high > low else low
                state['_next_interval'] = cached
            return cached

        interval = trigger_data.get('interval_seconds', 0)
        if not interval:
            interval = trigger_data.get('interval_minutes', 0) * 60
        return int(interval)

    async def _process_schedule_routine(
        self,
        routine: Dict,
        current_time: float,
        index: int = 0,
        trigger_data: Optional[Dict] = None
    ) -> None:
        """
        Traite une routine déclenchée à heure fixe ("at 18:00").

        Contrairement au timer qui compte un intervalle, celle-ci se
        déclenche quand l'horloge atteint l'heure demandée, éventuellement
        restreinte à certains jours de la semaine.

        Args:
            routine: Données de la routine
            current_time: Timestamp actuel
        """
        if trigger_data is None:
            trigger_data = routine['trigger_data']
        
        state = self._trigger_state(routine, index)
        target = trigger_data.get('time')
        if not target:
            return

        now = datetime.datetime.now()

        # Jour de la semaine autorisé ?
        days = trigger_data.get('days') or []
        if days and now.weekday() not in days:
            return

        # Heure atteinte ? (comparaison à la minute)
        if now.strftime("%H:%M") != target:
            return

        # Une seule exécution par minute, même si la boucle tourne à 1 Hz
        minute_start = current_time - now.second - (now.microsecond / 1_000_000)
        if state.get('_last_run', 0) >= minute_start:
            return

        guild = self.bot.get_guild(int(routine['guild_id']))
        if not guild:
            return

        context = await self._find_valid_context(routine, guild)
        if context:
            state['_last_run'] = current_time
            self._spawn_actions(routine, context)
            logger.debug(f"Routine horaire '{routine['name']}' exécutée à {target}")

    @staticmethod
    def _wanted_ids(routine: Dict, condition_type: str) -> set:
        """
        Collecte les identifiants visés par les conditions de la trame.

        Sert à choisir un contexte pertinent : une routine déclenchée par un
        timer n'a ni membre ni salon d'origine, il faut donc en désigner un.
        Autant prendre celui que la trame cherche.

        Args:
            routine: Données de la routine
            condition_type: Type de condition à examiner (user_id, channel_id)

        Returns:
            Ensemble des identifiants visés par une égalité
        """
        wanted = set()

        for node in routine.get('actions') or []:
            if node.get('kind') != 'if':
                continue
            for condition in node.get('conditions') or []:
                if condition.get('type') != condition_type:
                    continue
                if condition.get('op', '==') != '==':
                    continue
                wanted |= {
                    part.strip()
                    for part in str(condition.get('value', '')).split(',')
                    if part.strip()
                }

        return wanted

    async def _find_valid_context(
        self,
        routine: Dict,
        guild: discord.Guild
    ) -> Optional[RoutineContext]:
        """
        Choisit un contexte d'exécution pour une routine sans origine.

        Un déclencheur temporel n'est lié ni à un membre ni à un salon : il
        faut en désigner un pour que les conditions et la lecture aient un
        point d'ancrage.

        Le tirage est aléatoire parmi les salons occupés, pour ne pas
        favoriser éternellement le premier salon de la liste. Si la trame
        vise des membres ou des salons précis, le choix se restreint d'abord
        à ceux-ci : sans cela, une condition « si c'est le membre X » ne
        serait presque jamais satisfaite.

        Args:
            routine: Données de la routine
            guild: Serveur Discord

        Returns:
            Un contexte, ou None si personne n'est en vocal
        """
        candidates = [
            (vc, member)
            for vc in guild.voice_channels
            for member in vc.members
            if not member.bot
        ]

        if not candidates:
            return None

        wanted_users = self._wanted_ids(routine, 'user_id')
        wanted_channels = self._wanted_ids(routine, 'channel_id')

        preferred = [
            (vc, member)
            for vc, member in candidates
            if (not wanted_users or str(member.id) in wanted_users)
            and (not wanted_channels or str(vc.id) in wanted_channels)
        ]

        # Si personne ne correspond, on garde un contexte quelconque : la
        # trame peut avoir d'autres branches, à elle de trancher.
        channel, member = random.choice(preferred or candidates)
        return RoutineContext(guild=guild, channel=channel, member=member)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ) -> None:
        """
        Gère les événements de changement d'état vocal.
        
        Détecte les événements join/leave/move et déclenche
        les routines correspondantes.
        
        Args:
            member: Membre concerné par le changement
            before: État vocal avant le changement
            after: État vocal après le changement
        """
        # Ignorer les bots
        if member.bot:
            return
        
        logger.debug(f"Voice state update: {member.name} ({member.id}) - before={before.channel} after={after.channel}")
        
        # Déterminer les événements (peut en générer plusieurs pour un move)
        events = self._determine_voice_events(before, after)
        
        # Événements dérivés du nombre d'humains présents après le changement
        human_count = None
        if after.channel is not None:
            human_count = sum(1 for m in after.channel.members if not m.bot)
            joined = any(evt == "voice_join" for evt, _ in events)
            
            if joined and human_count == 1:
                # Premier humain dans un salon jusque-là vide
                events.append(("voice_first_join", after.channel))
            if joined:
                # Le palier exact est vérifié routine par routine plus bas
                events.append(("voice_count_reached", after.channel))
        
        if not events:
            logger.debug(f"Aucun événement vocal détecté pour {member.name}")
            return

        logger.debug(f"Événements vocaux détectés: {[e[0] for e in events]} par {member.name} ({member.id})")
        logger.debug(f"Nombre de routines chargées: {len(self.routines)}")

        # Récupérer les salons ignorés pour ce serveur
        ignored_channels = await self.db.get_ignored_channels(str(member.guild.id))

        # Pour chaque événement, vérifier les routines correspondantes
        triggered_routines = set()  # Éviter de déclencher la même routine plusieurs fois
        
        for event_type, channel in events:
            # Vérifier si le salon est ignoré
            if channel and str(channel.id) in ignored_channels:
                logger.debug(f"Salon {channel.name} ignoré, routine non déclenchée")
                continue
                
            context = RoutineContext(
                guild=member.guild,
                channel=channel,
                member=member
            )

            # Exécuter les routines correspondantes
            for routine in self.routines:
                # Une routine ne se déclenche qu'une fois par événement, même
                # si plusieurs de ses déclencheurs correspondent.
                routine_key = (routine['id'], event_type)
                if routine_key in triggered_routines:
                    continue
                
                if str(routine['guild_id']) != str(member.guild.id):
                    continue
                
                # Les déclencheurs d'une routine fonctionnent en OU
                if not self._matches_voice_event(routine, event_type, human_count):
                    continue
                
                logger.debug(f"Routine '{routine['name']}' correspond à {event_type}")
                
                if await self._check_conditions(routine, context):
                    logger.info(f"🎯 Routine '{routine['name']}' déclenchée par {event_type}")
                    triggered_routines.add(routine_key)
                    self._spawn_actions(routine, context)
                else:
                    logger.debug("  -> Conditions non satisfaites")

    @staticmethod
    def _channel_allowed(trigger_data: Dict, channel) -> bool:
        """
        Vérifie qu'un déclencheur accepte le salon où l'événement a eu lieu.

        Une liste vide ou absente signifie « tous les salons » : c'est le
        comportement par défaut, un déclencheur non restreint reste global.

        Args:
            trigger_data: Données du déclencheur
            channel: Salon de l'événement

        Returns:
            True si le déclencheur s'applique à ce salon
        """
        allowed = trigger_data.get('channels') or []
        if not allowed:
            return True

        if channel is None:
            return False

        return str(channel.id) in {str(c) for c in allowed}

    def _matches_voice_event(
        self,
        routine: Dict,
        event_type: str,
        human_count: Optional[int]
    ) -> bool:
        """
        Indique si l'un des déclencheurs de la routine correspond à l'événement.

        Args:
            routine: Données de la routine
            event_type: Événement détecté
            human_count: Nombre d'humains dans le salon après l'événement

        Returns:
            True si au moins un déclencheur correspond
        """
        for _, t_type, t_data in self.iter_triggers(routine):
            if t_type != 'event' or t_data.get('event') != event_type:
                continue

            # Palier de membres : seul le nombre exact déclenche, sinon la
            # routine repartirait à chaque nouvelle arrivée.
            if event_type == "voice_count_reached":
                expected = t_data.get('count')
                if expected is None or human_count != int(expected):
                    continue

            return True

        return False

    async def on_message(self, message: discord.Message) -> None:
        """
        Déclenche les routines liées à un mot-clé dans un message.

        Args:
            message: Message reçu
        """
        if message.author.bot or message.guild is None:
            return

        content = (message.content or "").lower()
        if not content:
            return  # Sans l'intent message_content, le contenu est vide

        context = RoutineContext(
            guild=message.guild,
            channel=message.channel,
            member=message.author if isinstance(message.author, discord.Member) else None
        )

        for routine in self.routines:
            if str(routine['guild_id']) != str(message.guild.id):
                continue

            matched = False
            for _, t_type, t_data in self.iter_triggers(routine):
                if t_type != 'event' or t_data.get('event') != 'message':
                    continue
                if not self._channel_allowed(t_data, message.channel):
                    continue
                keyword = (t_data.get('keyword') or "").lower().strip()
                if keyword and keyword in content:
                    matched = True
                    break

            if not matched:
                continue

            if await self._check_conditions(routine, context):
                logger.info(f"🎯 Routine '{routine['name']}' déclenchée par message")
                self._spawn_actions(routine, context)

    async def on_reaction(
        self,
        emoji: str,
        member: discord.Member,
        channel: discord.abc.GuildChannel
    ) -> None:
        """
        Déclenche les routines liées à une réaction.

        Args:
            emoji: Représentation textuelle de l'émoji ajouté
            member: Membre ayant réagi
            channel: Salon où la réaction a eu lieu
        """
        if member is None or member.bot or member.guild is None:
            return

        context = RoutineContext(guild=member.guild, channel=channel, member=member)

        for routine in self.routines:
            if str(routine['guild_id']) != str(member.guild.id):
                continue

            matched = False
            for _, t_type, t_data in self.iter_triggers(routine):
                if t_type != 'event' or t_data.get('event') != 'reaction':
                    continue
                if not self._channel_allowed(t_data, channel):
                    continue
                expected = (t_data.get('emoji') or "").strip()
                if not expected or expected == emoji:
                    matched = True
                    break

            if not matched:
                continue

            if await self._check_conditions(routine, context):
                logger.info(f"🎯 Routine '{routine['name']}' déclenchée par réaction {emoji}")
                self._spawn_actions(routine, context)

    def _determine_voice_events(
        self,
        before: discord.VoiceState,
        after: discord.VoiceState
    ) -> List[Tuple[str, Optional[discord.VoiceChannel]]]:
        """
        Détermine les types d'événements vocaux.
        
        Un changement de salon génère plusieurs événements :
        - voice_join : arrivée dans un salon (aussi sur move)
        - voice_leave : départ d'un salon (aussi sur move)
        - voice_move : changement de salon uniquement
        - voice_mute/unmute : micro coupé/activé
        - voice_deafen/undeafen : casque coupé/activé
        - voice_stream_start/stop : partage d'écran
        - voice_video_start/stop : caméra
        
        Args:
            before: État avant
            after: État après
            
        Returns:
            Liste de tuples (event_type, channel)
        """
        events = []
        current_channel = after.channel or before.channel
        
        # Événements de changement de salon
        if before.channel is None and after.channel is not None:
            # Rejoint un salon depuis aucun salon
            events.append(("voice_join", after.channel))
        elif before.channel is not None and after.channel is None:
            # Quitte un salon vers aucun salon
            events.append(("voice_leave", before.channel))
        elif (before.channel is not None and after.channel is not None 
              and before.channel.id != after.channel.id):
            # Change de salon : génère join, leave ET move
            events.append(("voice_leave", before.channel))  # Quitte l'ancien
            events.append(("voice_join", after.channel))     # Rejoint le nouveau
            events.append(("voice_move", after.channel))     # Move spécifique
        
        # Événements de mute (self_mute = micro coupé par l'utilisateur)
        if not before.self_mute and after.self_mute:
            events.append(("voice_mute", current_channel))
        elif before.self_mute and not after.self_mute:
            events.append(("voice_unmute", current_channel))
        
        # Événements de deafen (self_deaf = casque coupé par l'utilisateur)
        if not before.self_deaf and after.self_deaf:
            events.append(("voice_deafen", current_channel))
        elif before.self_deaf and not after.self_deaf:
            events.append(("voice_undeafen", current_channel))
        
        # Événements de stream (partage d'écran)
        if not before.self_stream and after.self_stream:
            events.append(("voice_stream_start", current_channel))
        elif before.self_stream and not after.self_stream:
            events.append(("voice_stream_stop", current_channel))
        
        # Événements de vidéo (caméra)
        if not before.self_video and after.self_video:
            events.append(("voice_video_start", current_channel))
        elif before.self_video and not after.self_video:
            events.append(("voice_video_stop", current_channel))
        
        return events

    async def _check_conditions(
        self,
        routine: Dict,
        context: Optional[RoutineContext]
    ) -> bool:
        """
        Vérifie si les conditions d'une routine sont satisfaites.
        
        Args:
            routine: Données de la routine
            context: Contexte d'exécution
            
        Returns:
            True si toutes les conditions sont satisfaites
        """
        conditions = routine.get('conditions')
        if not conditions:
            return True
        
        ctx_dict = context.to_dict() if context else None
        return await self._evaluate_condition_node(conditions, ctx_dict)

    async def _evaluate_condition_node(
        self,
        node: Dict,
        context: Optional[Dict]
    ) -> bool:
        """
        Évalue récursivement un nœud de condition.
        
        Supporte les opérateurs logiques AND, OR, XOR, NOT
        et les conditions feuille (user_id, channel_id, etc.)
        
        Args:
            node: Nœud de condition à évaluer
            context: Contexte d'exécution
            
        Returns:
            Résultat de l'évaluation
        """
        c_type = node.get('type')
        
        # Opérateurs logiques
        if c_type == 'AND':
            for sub in node.get('sub', []):
                if not await self._evaluate_condition_node(sub, context):
                    return False
            return True
        
        elif c_type == 'OR':
            for sub in node.get('sub', []):
                if await self._evaluate_condition_node(sub, context):
                    return True
            return False
        
        elif c_type == 'XOR':
            true_count = 0
            for sub in node.get('sub', []):
                if await self._evaluate_condition_node(sub, context):
                    true_count += 1
            return true_count == 1
        
        elif c_type == 'NOT':
            sub_nodes = node.get('sub', [])
            if sub_nodes:
                return not await self._evaluate_condition_node(sub_nodes[0], context)
            return True
        
        # Condition feuille
        return self._evaluate_leaf_condition(node, context)

    def _evaluate_leaf_condition(
        self,
        node: Dict,
        context: Optional[Dict]
    ) -> bool:
        """
        Évalue une condition feuille en s'appuyant sur le catalogue.

        Les types disponibles sont déclarés dans blocks.CONDITION_BLOCKS ;
        chacun désigne la méthode qui l'évalue.

        Args:
            node: Nœud de condition
            context: Contexte d'exécution

        Returns:
            True si la condition est satisfaite
        """
        c_type = node.get('type')
        op = node.get('op', '==')
        value = str(node.get('value', ''))

        block = CONDITION_BLOCKS.get(c_type)
        if block is None:
            logger.warning(f"Type de condition inconnu: {c_type}")
            return False

        handler = getattr(self, block.handler, None)
        if handler is None:
            logger.error(
                f"Condition '{c_type}' déclarée avec le handler "
                f"'{block.handler}', qui n'existe pas dans RoutineManager."
            )
            return False

        logger.debug(f"Évaluation condition: type={c_type}, op={op}, value={value}")
        return bool(handler(op, value, context))

    # --- Évaluateurs de conditions (signature commune op, value, context) ---

    @staticmethod
    def _compare_values(op: str, actual: Optional[str], value: str) -> bool:
        """
        Compare une valeur du contexte à celle attendue.

        La valeur attendue peut lister plusieurs entrées séparées par des
        virgules : l'appartenance à la liste fait alors office d'égalité.

        Args:
            op: Opérateur (== ou !=)
            actual: Valeur constatée, ou None si absente du contexte
            value: Valeur attendue

        Returns:
            Résultat de la comparaison
        """
        if actual is None:
            return False

        if ',' in value:
            allowed = {v.strip() for v in value.split(',') if v.strip()}
            return actual in allowed if op == '==' else actual not in allowed

        return actual != value if op == '!=' else actual == value

    def _check_user(self, op: str, value: str, context: Optional[Dict]) -> bool:
        """Compare l'auteur du déclenchement."""
        member = context.get('member') if context else None
        return self._compare_values(op, str(member.id) if member else None, value)

    def _check_channel(self, op: str, value: str, context: Optional[Dict]) -> bool:
        """Compare le salon concerné."""
        channel = context.get('channel') if context else None
        return self._compare_values(op, str(channel.id) if channel else None, value)

    def _check_role(self, op: str, value: str, context: Optional[Dict]) -> bool:
        """Vérifie qu'un membre possède (ou non) l'un des rôles listés."""
        member = context.get('member') if context else None
        if member is None:
            return False

        member_roles = {str(r.id) for r in getattr(member, 'roles', [])}
        wanted = {v.strip() for v in value.split(',') if v.strip()}

        has_any = bool(member_roles & wanted)
        return not has_any if op == '!=' else has_any

    def _check_queue_length(self, op: str, value: str, context: Optional[Dict]) -> bool:
        """Compare le nombre de sons en attente."""
        guild = context.get('guild') if context else None
        if guild is None:
            return False

        player = self.bot.player_manager.find_player(guild.id)
        length = player.get_queue_info()['queue_length'] if player else 0

        try:
            expected = int(value)
        except (TypeError, ValueError):
            logger.warning(f"Valeur invalide pour queue_length: {value}")
            return False

        return self._compare_numbers(op, length, expected)

    def _check_bot_connected(self, op: str, value: str, context: Optional[Dict]) -> bool:
        """Indique si le bot est déjà présent dans un salon vocal."""
        guild = context.get('guild') if context else None
        if guild is None:
            return False

        player = self.bot.player_manager.find_player(guild.id)
        voice = player.voice_client if player else None
        connected = bool(voice and voice.is_connected())

        expected = str(value).lower() in ('true', 'vrai', '1', 'oui')
        return connected == expected

    def _check_is_bot(self, op: str, value: str, context: Optional[Dict]) -> bool:
        """Indique si l'auteur du déclenchement est un bot."""
        member = context.get('member') if context else None
        if member is None:
            return False

        expected = str(value).lower() in ('true', 'vrai', '1', 'oui')
        return bool(getattr(member, 'bot', False)) == expected

    def _check_time_condition(self, op: str, value: str, context: Optional[Dict]) -> bool:
        """Vérifie la plage horaire."""
        return self._check_time_range(value)

    def _check_date_condition(self, op: str, value: str, context: Optional[Dict]) -> bool:
        """Vérifie la plage de dates."""
        return self._check_date_range(value)

    def _check_chance_condition(self, op: str, value: str, context: Optional[Dict]) -> bool:
        """Tire au sort selon la probabilité donnée."""
        return self._check_chance(value)

    @staticmethod
    def _compare_numbers(op: str, actual: float, expected: float) -> bool:
        """
        Applique un opérateur de comparaison numérique.

        Args:
            op: Opérateur (==, !=, >, <, >=, <=)
            actual: Valeur constatée
            expected: Valeur attendue

        Returns:
            Résultat de la comparaison
        """
        if op == '>':
            return actual > expected
        if op == '<':
            return actual < expected
        if op == '>=':
            return actual >= expected
        if op == '<=':
            return actual <= expected
        if op == '!=':
            return actual != expected
        return actual == expected

    def _check_member_count(self, op: str, value: str, context: Optional[Dict]) -> bool:
        """
        Compare le nombre d'humains présents dans le salon du contexte.

        Args:
            op: Opérateur de comparaison
            value: Nombre attendu
            context: Contexte d'exécution

        Returns:
            True si la comparaison est vérifiée
        """
        channel = context.get('channel') if context else None
        if channel is None or not hasattr(channel, 'members'):
            return False

        try:
            expected = int(str(value).strip())
        except ValueError:
            logger.warning(f"Valeur member_count invalide: {value}")
            return False

        actual = sum(1 for m in channel.members if not m.bot)
        logger.debug(f"  member_count: actual={actual}, {op} {expected}")
        return self._compare_numbers(op, actual, expected)

    @staticmethod
    def _check_chance(value: str) -> bool:
        """
        Tire au sort selon une probabilité en pourcentage.

        Args:
            value: Probabilité, avec ou sans '%'

        Returns:
            True si le tirage est favorable
        """
        try:
            percent = float(str(value).strip().rstrip('%'))
        except ValueError:
            logger.warning(f"Valeur chance invalide: {value}")
            return False

        percent = max(0.0, min(100.0, percent))
        result = random.random() * 100 < percent
        logger.debug(f"  chance {percent}% -> {result}")
        return result

    @staticmethod
    def _check_weekday(op: str, value: str, context: Optional[Dict] = None) -> bool:
        """
        Vérifie le jour de la semaine courant.

        Args:
            op: '==' (dans la liste) ou '!=' (hors de la liste)
            value: Jours acceptés, ex. "lun,mar,ven"

        Returns:
            True si le jour courant correspond
        """
        wanted = set()
        for token in str(value).split(','):
            token = token.strip().lower()
            if not token:
                continue
            if token in WEEKDAYS:
                wanted.add(WEEKDAYS[token])
            elif token.isdigit() and 0 <= int(token) <= 6:
                wanted.add(int(token))
            else:
                logger.warning(f"Jour inconnu dans la condition weekday: {token}")

        if not wanted:
            return False

        today = datetime.datetime.now().weekday()
        return today not in wanted if op == '!=' else today in wanted

    def _check_is_playing(self, op: str, value: str, context: Optional[Dict]) -> bool:
        """
        Vérifie si le bot est déjà en train de jouer un son sur ce serveur.

        Permet d'écrire des routines qui ne coupent pas la parole à un son
        en cours.

        Args:
            op: Opérateur (== ou !=)
            value: "true" ou "false"
            context: Contexte d'exécution

        Returns:
            True si la condition est satisfaite
        """
        guild = context.get('guild') if context else None
        if guild is None:
            return False

        player = self.bot.player_manager.find_player(guild.id)
        playing = False
        if player is not None:
            info = player.get_queue_info()
            playing = bool(info['is_playing'] or info['queue_length'])

        expected = str(value).strip().lower() in ('true', '1', 'oui', 'yes')
        logger.debug(f"  is_playing: actual={playing}, expected={expected}")
        return playing != expected if op == '!=' else playing == expected

    def _check_time_range(self, value: str) -> bool:
        """
        Vérifie si l'heure actuelle est dans la plage spécifiée.
        
        Format: "HH:MM-HH:MM"
        Supporte les plages qui traversent minuit.
        
        Args:
            value: Plage horaire au format "HH:MM-HH:MM"
            
        Returns:
            True si l'heure actuelle est dans la plage
        """
        try:
            now = datetime.datetime.now().time()
            start_str, end_str = value.split('-')
            start = datetime.datetime.strptime(start_str.strip(), "%H:%M").time()
            end = datetime.datetime.strptime(end_str.strip(), "%H:%M").time()
            
            if start <= end:
                return start <= now <= end
            else:
                # Traverse minuit (ex: 22:00-06:00)
                return start <= now or now <= end
                
        except ValueError as e:
            logger.error(f"Format time_range invalide '{value}': {e}")
            return False

    def _check_date_range(self, value: str) -> bool:
        """
        Vérifie si la date actuelle est dans la plage spécifiée.
        
        Format: "DD/MM-DD/MM" ou "DD:MM-DD:MM"
        Supporte les plages qui traversent l'année.
        
        Args:
            value: Plage de dates
            
        Returns:
            True si la date actuelle est dans la plage
        """
        try:
            now = datetime.datetime.now().date()
            current_year = now.year
            
            start_str, end_str = value.split('-')
            # Normaliser les séparateurs
            start_str = start_str.strip().replace(':', '/')
            end_str = end_str.strip().replace(':', '/')
            
            start_date = datetime.datetime.strptime(
                f"{start_str}/{current_year}", "%d/%m/%Y"
            ).date()
            end_date = datetime.datetime.strptime(
                f"{end_str}/{current_year}", "%d/%m/%Y"
            ).date()
            
            if start_date <= end_date:
                return start_date <= now <= end_date
            else:
                # Traverse l'année (ex: 25/12-05/01)
                return start_date <= now or now <= end_date
                
        except ValueError as e:
            logger.error(f"Format date_range invalide '{value}': {e}")
            return False

    # ------------------------------------------------------------------
    # Exécution de la trame
    # ------------------------------------------------------------------

    # Profondeur maximale d'imbrication, garde-fou contre les trames
    # construites à la main avec une récursion absurde
    MAX_TRAME_DEPTH = 10

    @staticmethod
    def build_tree(flat: List[Dict]) -> List[Dict]:
        """
        Reconstruit l'arbre d'exécution à partir de la liste à plat.

        La trame est stockée à plat, chaque bloc portant sa profondeur.
        C'est ce qui rend l'édition simple (monter, descendre, indenter
        reviennent à manipuler une liste), mais l'exécution a besoin de
        l'imbrication réelle.

        Args:
            flat: Liste de blocs, chacun avec une clé `depth`

        Returns:
            Liste des blocs racines, chaque bloc portant ses `children`
        """
        roots: List[Dict] = []
        # Pile des blocs ouverts, indexée par profondeur
        stack: List[Dict] = []

        for block in flat:
            node = dict(block)
            node['children'] = []
            depth = max(0, int(node.get('depth', 0)))

            # Un bloc ne peut pas s'enfoncer de plus d'un niveau, ni se
            # rattacher à autre chose qu'un bloc conditionnel.
            while len(stack) > depth:
                stack.pop()

            if stack and len(stack) == depth:
                stack[-1]['children'].append(node)
            else:
                roots.append(node)
                stack.clear()

            if node.get('kind') == 'if':
                stack.append(node)
            elif stack and len(stack) == depth:
                pass

        return roots

    async def _execute_actions(
        self,
        routine: Dict,
        context: Optional[RoutineContext]
    ) -> None:
        """
        Exécute la trame d'une routine.

        Args:
            routine: Données de la routine
            context: Contexte d'exécution
        """
        flat = routine.get('actions') or []
        tree = self.build_tree(flat)

        logger.debug(
            f"Exécution de la trame de '{routine['name']}' "
            f"({len(flat)} bloc(s))"
        )

        await self._execute_nodes(tree, routine, context)

    async def _execute_nodes(
        self,
        nodes: List[Dict],
        routine: Dict,
        context: Optional[RoutineContext],
        depth: int = 0
    ) -> bool:
        """
        Exécute une suite de blocs de même niveau.

        Les blocs frères s'enchaînent normalement. Un bloc marqué `or`
        n'est évalué que si le précédent n'a pas été exécuté : c'est le
        « sinon si » de la trame.

        Args:
            nodes: Blocs à exécuter
            routine: Routine parente (pour les logs et le contexte)
            context: Contexte d'exécution
            depth: Profondeur courante, pour le garde-fou

        Returns:
            True si la routine doit être interrompue entièrement
        """
        if depth > self.MAX_TRAME_DEPTH:
            logger.warning(
                f"Trame de '{routine['name']}' trop imbriquée "
                f"(>{self.MAX_TRAME_DEPTH}), branche ignorée"
            )
            return False

        previous_ran = False

        for node in nodes:
            # Chaînage « ou » : on saute si la branche précédente a déjà pris
            if node.get('link') == 'or' and previous_ran:
                continue

            kind = node.get('kind', 'action')

            try:
                if kind == 'if':
                    matched = await self._check_block_conditions(node, context)
                    logger.debug(
                        f"{'  ' * depth}🤔 Bloc condition -> "
                        f"{'vrai' if matched else 'faux'}"
                    )
                    if matched:
                        stop = await self._execute_nodes(
                            node.get('children', []), routine, context, depth + 1
                        )
                        if stop:
                            return True
                    previous_ran = matched
                else:
                    stop = await self._run_action(node.get('action') or {}, routine, context)
                    if stop:
                        return True
                    previous_ran = True

            except Exception as e:
                logger.error(
                    f"Erreur dans un bloc de la routine '{routine['name']}': {e}",
                    exc_info=True
                )
                previous_ran = False

        return False

    async def _check_block_conditions(
        self,
        node: Dict,
        context: Optional[RoutineContext]
    ) -> bool:
        """
        Évalue les conditions d'un bloc conditionnel.

        Args:
            node: Bloc de type `if`
            context: Contexte d'exécution

        Returns:
            True si le bloc doit s'exécuter
        """
        conditions = node.get('conditions') or []
        if not conditions:
            return True

        ctx_dict = context.to_dict() if context else None
        logic = node.get('logic', 'AND')

        if len(conditions) == 1:
            return await self._evaluate_condition_node(conditions[0], ctx_dict)

        return await self._evaluate_condition_node(
            {'type': logic, 'sub': conditions}, ctx_dict
        )

    async def _run_action(
        self,
        action: Dict,
        routine: Dict,
        context: Optional[RoutineContext]
    ) -> bool:
        """
        Exécute une action unique.

        Args:
            action: Données de l'action
            routine: Routine parente
            context: Contexte d'exécution

        Returns:
            True si la routine entière doit être interrompue
        """
        action_type = action.get('type')
        block = ACTION_BLOCKS.get(action_type)

        if block is None:
            logger.warning(f"Type d'action inconnu: {action_type}")
            return False

        logger.debug(f"Action: type={action_type}, data={action}")

        # Le contexte est reconstruit avant les actions qui s'en servent :
        # après une attente, le membre a pu changer de salon.
        ctx_dict = None
        if block.refresh_context and context:
            fresh_context = await self._refresh_context(context, routine)
            ctx_dict = fresh_context.to_dict() if fresh_context else None

        handler = getattr(self, block.handler, None)
        if handler is None:
            logger.error(
                f"Bloc '{action_type}' déclaré avec le handler '{block.handler}', "
                "qui n'existe pas dans RoutineManager."
            )
            return False

        return bool(await handler(action, ctx_dict, routine))

    async def _action_wait(
        self,
        action: Dict,
        context: Optional[Dict] = None,
        routine: Optional[Dict] = None
    ) -> bool:
        """
        Met la trame en pause, d'une durée fixe ou tirée dans une plage.

        Args:
            action: Données de l'action
            context: Inutilisé
            routine: Inutilisé

        Returns:
            False : une pause n'interrompt jamais la routine
        """
        delay = self._resolve_delay(action)
        if delay > 0:
            logger.debug(f"⏳ Attente de {format_duration(delay)}...")
            await asyncio.sleep(delay)
        return False

    async def _action_chance(
        self,
        action: Dict,
        context: Optional[Dict] = None,
        routine: Optional[Dict] = None
    ) -> bool:
        """
        Tire au sort la poursuite de la routine.

        Args:
            action: Données de l'action
            context: Inutilisé
            routine: Inutilisé

        Returns:
            True si le tirage échoue, ce qui interrompt toute la routine
        """
        percent = action.get('percent', 100)
        if not self._check_chance(percent):
            logger.debug(f"🎲 Tirage {percent}% raté, routine interrompue")
            return True

        logger.debug(f"🎲 Tirage {percent}% réussi")
        return False

    @staticmethod
    def _resolve_delay(action: Dict) -> int:
        """
        Détermine la durée d'une pause, fixe ou tirée dans une plage.

        Args:
            action: Données de l'action wait

        Returns:
            Durée en secondes
        """
        low = action.get('delay_min')
        high = action.get('delay_max')

        if low is not None and high is not None:
            low, high = int(low), int(high)
            if high > low:
                delay = random.randint(low, high)
                logger.debug(f"⏳ Pause aléatoire entre {low}s et {high}s -> {delay}s")
                return delay
            return low

        return int(action.get('delay', 0) or 0)

    def _get_player(self, context: Optional[Dict]):
        """
        Récupère le player du serveur du contexte, sans en créer.

        Args:
            context: Contexte d'exécution

        Returns:
            GuildPlayer ou None
        """
        guild = context.get('guild') if context else None
        if guild is None:
            return None
        return self.bot.player_manager.find_player(guild.id)

    async def _action_player_control(
        self,
        action: Dict,
        context: Optional[Dict] = None,
        routine: Optional[Dict] = None
    ) -> bool:
        """
        Pilote la lecture : stop, skip, clear ou leave.

        Args:
            action: Données de l'action ({'command': 'stop'|'skip'|'clear'|'leave'})
            context: Contexte d'exécution
        """
        command = (action.get('command') or '').lower()
        player = self._get_player(context)

        if player is None:
            logger.debug(f"Contrôle '{command}' ignoré: aucun player actif")
            return

        if command == 'stop':
            player.stop()
            logger.info("⏹️ Routine: lecture arrêtée et file vidée")
        elif command == 'leave_now':
            left = await player.leave(wait_for_queue=False, clear_queue=True)
            logger.info(
                "🚪 Routine: déconnexion immédiate"
                if left else "🚪 Routine: le bot n'était pas connecté"
            )
        elif command == 'skip':
            skipped = player.skip()
            logger.info(f"⏭️ Routine: skip {'effectué' if skipped else 'sans effet'}")
        elif command == 'clear':
            removed = player.clear_queue()
            logger.info(f"🧹 Routine: {removed} son(s) retiré(s) de la file")
        elif command == 'leave':
            # Une action `play` placée avant n'a fait qu'empiler le son : on
            # attend la fin de la file, sinon le bot se reconnecterait juste
            # après être parti. Pour un départ immédiat, utiliser `stop`.
            wait = bool(action.get('wait_for_queue', True))
            left = await player.leave(wait_for_queue=wait, clear_queue=not wait)
            if left:
                logger.info("🚪 Routine: le bot a quitté le salon vocal")
            else:
                logger.info("🚪 Routine: le bot n'était pas connecté, rien à quitter")
        else:
            logger.warning(f"Commande de contrôle inconnue: {command}")

    async def _action_set_volume(
        self,
        action: Dict,
        context: Optional[Dict] = None,
        routine: Optional[Dict] = None
    ) -> bool:
        """
        Change le volume de lecture pour la session en cours.

        La valeur "reset" restaure le volume configuré en base pour le
        serveur. Le changement n'est pas persisté : il vaut jusqu'au
        prochain reset ou redémarrage.

        Args:
            action: Données de l'action ({'value': 150} ou {'value': 'reset'})
            context: Contexte d'exécution
        """
        guild = context.get('guild') if context else None
        if guild is None:
            return

        player = self.bot.player_manager.get_player(guild.id)
        raw = action.get('value')

        if str(raw).strip().lower() == 'reset':
            player.invalidate_volume_cache()  # forcera une relecture en base
            restored = await player.get_volume()
            logger.info(f"🔊 Routine: volume restauré à {round(restored * 100)}%")
            return

        try:
            percent = int(raw)
        except (TypeError, ValueError):
            logger.warning(f"Valeur de volume invalide dans une routine: {raw}")
            return

        # Le plafond du serveur s'applique aussi aux routines
        ceiling = await player.get_max_volume()
        applied = player.set_volume(percent, ceiling)

        if percent > ceiling:
            logger.info(
                f"🔊 Routine: volume {percent}% ramené au plafond du serveur "
                f"({ceiling}%)"
            )
        else:
            logger.info(f"🔊 Routine: volume réglé sur {round(applied * 100)}%")

    async def _action_move(
        self,
        action: Dict,
        context: Optional[Dict] = None,
        routine: Optional[Dict] = None
    ) -> bool:
        """
        Déplace le bot ou le membre déclencheur vers un salon vocal.

        Args:
            action: Données ({'target': 'bot'|'member', 'channel_id': ...})
            context: Contexte d'exécution
        """
        guild = context.get('guild') if context else None
        channel_id = action.get('channel_id')
        if guild is None or not channel_id:
            return

        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            logger.warning(f"Salon de déplacement invalide: {channel_id}")
            return

        target = (action.get('target') or 'bot').lower()

        if target == 'member':
            member = context.get('member') if context else None
            if member is None or not member.voice:
                logger.debug("Déplacement ignoré: le membre n'est pas en vocal")
                return
            try:
                await member.move_to(channel)
                logger.info(f"↔️ Routine: {member.display_name} déplacé vers {channel.name}")
            except discord.Forbidden:
                logger.warning("Permission « Déplacer des membres » manquante")
            except discord.HTTPException as e:
                logger.warning(f"Échec du déplacement du membre: {e}")
            return

        player = self.bot.player_manager.get_player(guild.id)
        if await player.join(channel):
            logger.info(f"↔️ Routine: bot déplacé vers {channel.name}")

    async def _action_send_dm(
        self,
        action: Dict,
        context: Optional[Dict] = None,
        routine: Optional[Dict] = None
    ) -> bool:
        """
        Envoie un message privé au membre à l'origine du déclenchement.

        Args:
            action: Données de l'action ({'content': ...})
            context: Contexte d'exécution
        """
        member = context.get('member') if context else None
        content = action.get('content', '')

        if member is None or not content:
            return

        content = content.replace("{user}", member.mention)
        content = content.replace("{username}", member.display_name)

        try:
            await member.send(content)
            logger.debug(f"MP envoyé à {member.display_name}")
        except discord.Forbidden:
            logger.debug(f"MP refusé par {member.display_name} (MP fermés)")
        except discord.HTTPException as e:
            logger.warning(f"Échec de l'envoi du MP: {e}")

    async def _refresh_context(
        self,
        original_context: RoutineContext,
        routine: Dict
    ) -> Optional[RoutineContext]:
        """
        Rafraîchit le contexte pour obtenir la position actuelle du membre.
        
        Args:
            original_context: Contexte original
            routine: Routine en cours
            
        Returns:
            Nouveau contexte avec les infos à jour, ou None
        """
        guild = original_context.guild
        member = original_context.member
        
        if not member:
            # Pas de membre spécifique, garder le contexte original
            return original_context
        
        # Récupérer le membre frais depuis le cache
        fresh_member = guild.get_member(member.id)
        
        if not fresh_member:
            logger.debug(f"Membre {member.id} non trouvé dans le cache")
            return original_context
        
        # Vérifier si le membre est toujours dans un salon vocal
        if fresh_member.voice and fresh_member.voice.channel:
            return RoutineContext(
                guild=guild,
                channel=fresh_member.voice.channel,
                member=fresh_member
            )
        else:
            # Le membre n'est plus en vocal, chercher un salon actif
            logger.debug(f"Membre {member.display_name} n'est plus en vocal, recherche d'un salon actif")
            active_channels = [
                vc for vc in guild.voice_channels
                if len([m for m in vc.members if not m.bot]) > 0
            ]
            
            if active_channels:
                return RoutineContext(
                    guild=guild,
                    channel=random.choice(active_channels),
                    member=fresh_member
                )
            
            logger.debug("Aucun salon vocal actif trouvé")
            return None

    async def _action_play_sound(
        self,
        action: Dict,
        context: Optional[Dict] = None,
        routine: Optional[Dict] = None
    ) -> bool:
        """
        Exécute une action de lecture de son.
        
        Args:
            action: Données de l'action
            context: Contexte d'exécution
            routine: Routine parente
        """
        sound_name = action.get('sound_name')
        if not sound_name:
            logger.warning("Action play_sound sans sound_name")
            return
            
        guild_id = str(routine['guild_id'])
        guild = self.bot.get_guild(int(guild_id))
        
        if not guild:
            logger.warning(f"Serveur introuvable: {guild_id}")
            return
        
        # Handle random sound selection
        if sound_name == "__random__":
            import random
            sounds = await self.db.get_available_sounds(guild_id)
            if not sounds:
                logger.warning(f"Aucun son disponible pour random (guild: {guild_id})")
                return
            sound_name = random.choice(list(sounds.keys()))
            logger.debug(f"🎲 Random sélectionné: '{sound_name}'")
        
        logger.debug(f"🎵 Tentative lecture son '{sound_name}' - context={context is not None}")
        
        # Déterminer le canal cible
        channel = await self._resolve_target_channel(action, context, guild)
        
        if not channel:
            logger.warning(f"Aucun canal valide pour jouer '{sound_name}' (routine: {routine['name']})")
            return
        
        logger.debug(f"Canal cible: {channel.name} ({channel.id})")

        # Récupérer le son
        sound_data = await self.db.get_sound(guild_id, sound_name)
        if not sound_data:
            sound_data = await self.db.get_sound("global", sound_name)
        
        if not sound_data:
            logger.warning(f"Son introuvable: {sound_name}")
            return

        # Construire le chemin du fichier
        file_path = os.path.join(
            Config.SOUNDS_DIR,
            sound_data['guild_id'],
            sound_data['filename']
        )
        
        if not os.path.exists(file_path):
            logger.warning(f"Fichier audio introuvable: {file_path}")
            return

        # Jouer le son
        player = self.bot.player_manager.get_player(int(guild_id))
        player.add_to_queue(
            file_path, "Routine", sound_name, channel,
            owner_guild_id=sound_data.get('guild_id')
        )
        logger.info(f"🎵 Son '{sound_name}' ajouté à la queue dans #{channel.name} (routine: {routine['name']})")

    async def _resolve_target_channel(
        self,
        action: Dict,
        context: Optional[Dict],
        guild: discord.Guild
    ) -> Optional[discord.VoiceChannel]:
        """
        Résout le canal cible pour une action audio.
        
        Args:
            action: Données de l'action
            context: Contexte d'exécution
            guild: Serveur Discord
            
        Returns:
            Canal vocal cible ou None
        """
        target_strategy = action.get('target_strategy', 'active')
        
        # Priorité 1: salon explicitement choisi dans l'action.
        # Il doit primer sur le contexte, sinon "toujours jouer dans #Général"
        # était ignoré au profit du salon de la personne qui a déclenché.
        if target_strategy == 'specific':
            channel_id = action.get('channel_id')
            if channel_id:
                channel = guild.get_channel(int(channel_id))
                if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                    return channel
                logger.warning(f"Salon cible {channel_id} introuvable ou non vocal")
        
        # Priorité 2: salon du contexte, s'il s'agit bien d'un salon vocal.
        # Un déclencheur message/réaction fournit un salon TEXTUEL, dans
        # lequel on ne peut pas jouer de son.
        ctx_channel = context.get('channel') if context else None
        if isinstance(ctx_channel, (discord.VoiceChannel, discord.StageChannel)):
            return ctx_channel
        
        # Priorité 3: salon vocal du membre à l'origine du déclenchement
        ctx_member = context.get('member') if context else None
        if ctx_member is not None and ctx_member.voice and ctx_member.voice.channel:
            return ctx_member.voice.channel
        
        # Priorité 4: Canal avec des membres (stratégie 'active')
        active_channels = [
            vc for vc in guild.voice_channels 
            if len([m for m in vc.members if not m.bot]) > 0
        ]
        
        if active_channels:
            return random.choice(active_channels)
        
        return None

    async def _action_send_message(
        self,
        action: Dict,
        context: Optional[Dict] = None,
        routine: Optional[Dict] = None
    ) -> bool:
        """
        Exécute une action d'envoi de message.
        
        Args:
            action: Données de l'action
            context: Contexte d'exécution
        """
        channel_id = action.get('channel_id')
        content = action.get('content', '')
        
        if not channel_id or not content:
            return
        
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            logger.warning(f"Canal introuvable pour message: {channel_id}")
            return
        
        # Remplacer les placeholders
        if context and context.get('member'):
            content = content.replace("{user}", context['member'].mention)
            content = content.replace("{username}", context['member'].display_name)
        
        try:
            await channel.send(content)
            logger.debug(f"Message envoyé dans #{channel.name}")
        except discord.Forbidden:
            logger.warning(f"Permission refusée pour envoyer un message dans #{channel.name}")
        except Exception as e:
            logger.error(f"Erreur lors de l'envoi du message: {e}")

    def parse_routine_string(self, cmd_str: str) -> Tuple[str, Dict, Optional[Dict], List[Dict]]:
        """
        Parse une commande textuelle pour créer une routine.
        
        Syntaxe: <trigger> [if <conditions>] do <actions>
        
        Exemples:
            timer 3s do play son
            on join if user=12345 do wait 2s then play welcome
            timer 5m if time=18:00-23:00 do play alerte
        
        Args:
            cmd_str: Commande textuelle à parser
            
        Returns:
            Tuple (trigger_type, trigger_data, conditions, actions)
            
        Raises:
            ValueError: Si la syntaxe est invalide
        """
        # Séparer trigger/conditions des actions
        parts = cmd_str.split(" do ")
        if len(parts) != 2:
            raise ValueError(
                "Syntaxe invalide: séparateur 'do' manquant. "
                "Format: <trigger> [if <conditions>] do <actions>"
            )
        
        lhs = parts[0].strip()
        rhs = parts[1].strip()
        
        # Isoler les conditions communes, s'il y en a
        conditions_part = None
        if " if " in lhs:
            trigger_part, conditions_part = lhs.split(" if ", 1)
        else:
            trigger_part = lhs
        
        # Plusieurs déclencheurs possibles, séparés par " or "
        triggers = []
        for chunk in re.split(r'\s+or\s+', trigger_part.strip()):
            chunk = chunk.strip()
            if not chunk:
                continue
            t_type, t_data = self._parse_trigger(chunk)
            triggers.append({"type": t_type, "data": t_data})
        
        if not triggers:
            raise ValueError("Aucun déclencheur reconnu.")
        
        # Les conditions deviennent des blocs englobants, une par niveau :
        # `if a and b` équivaut à imbriquer SI a puis SI b.
        trame = []
        depth = 0
        if conditions_part:
            for condition in self._parse_conditions(conditions_part.strip()):
                trame.append({
                    "depth": depth,
                    "link": "and",
                    "kind": "if",
                    "logic": "AND",
                    "conditions": [condition],
                })
                depth += 1
        
        for action in self._parse_actions(rhs):
            trame.append({
                "depth": depth,
                "link": "and",
                "kind": "action",
                "action": action,
            })
        
        return {"triggers": triggers}, trame

    def _parse_trigger(self, trigger_str: str) -> Tuple[str, Dict]:
        """
        Parse un trigger (déclencheur).
        
        Args:
            trigger_str: Chaîne du trigger
            
        Returns:
            Tuple (trigger_type, trigger_data)
        """
        tokens = trigger_str.split()
        if not tokens:
            raise ValueError("Trigger vide.")
        
        trigger_type = ""
        trigger_data = {}
        
        if tokens[0] == "at":
            # Heure fixe : "at 18:00" ou "at lun,ven 09:30"
            if len(tokens) < 2:
                raise ValueError("Heure manquante (ex: at 18:00)")
            
            days: List[int] = []
            time_token = tokens[-1]
            
            if len(tokens) > 2:
                for token in tokens[1].split(','):
                    token = token.strip().lower()
                    if token in WEEKDAYS:
                        days.append(WEEKDAYS[token])
                    elif token:
                        raise ValueError(f"Jour inconnu: {token}")
            
            if not re.fullmatch(r'\d{1,2}:\d{2}', time_token):
                raise ValueError(f"Heure invalide '{time_token}'. Format attendu HH:MM.")
            
            hours, minutes = (int(x) for x in time_token.split(':'))
            if not (0 <= hours <= 23 and 0 <= minutes <= 59):
                raise ValueError(f"Heure hors plage: {time_token}")
            
            return "schedule", {'time': f"{hours:02d}:{minutes:02d}", 'days': sorted(set(days))}
        
        if tokens[0] == "timer":
            if len(tokens) < 2:
                raise ValueError("Durée timer manquante (ex: timer 3s)")
            
            duration_str = tokens[1]
            trigger_data = self._parse_duration(duration_str)
            trigger_type = "timer"
            
        elif tokens[0] == "on":
            if len(tokens) < 2:
                raise ValueError("Événement manquant (ex: on join)")
            
            evt = tokens[1].lower()
            
            # Cas particulier : "on count>=3" / "on count 3"
            if evt.startswith("count"):
                raw = trigger_str.split(None, 1)[1]
                count = self._parse_member_count(raw)
                trigger_type = "event"
                trigger_data['event'] = "voice_count_reached"
                trigger_data['count'] = count
                return trigger_type, trigger_data
            
            # Cas particulier : "on message <mot-clé>"
            if evt == "message":
                if len(tokens) < 3:
                    raise ValueError("Mot-clé manquant (ex: on message bonjour)")
                trigger_type = "event"
                trigger_data['event'] = "message"
                trigger_data['keyword'] = " ".join(tokens[2:]).strip()
                return trigger_type, trigger_data
            
            # Cas particulier : "on reaction 🎉"
            if evt == "reaction":
                if len(tokens) < 3:
                    raise ValueError("Émoji manquant (ex: on reaction 🎉)")
                trigger_type = "event"
                trigger_data['event'] = "reaction"
                trigger_data['emoji'] = tokens[2].strip()
                return trigger_type, trigger_data
            
            if evt not in self.TEXT_EVENT_ALIASES:
                raise ValueError(
                    f"Événement inconnu '{evt}'. "
                    f"Utilisez: {', '.join(sorted(self.TEXT_EVENT_ALIASES))}"
                )
            
            trigger_type = "event"
            trigger_data['event'] = self.TEXT_EVENT_ALIASES[evt]
        else:
            raise ValueError(
                f"Trigger inconnu '{tokens[0]}'. "
                "Commencez par 'timer' ou 'on'."
            )
        return trigger_type, trigger_data

    @staticmethod
    def _parse_member_count(raw: str) -> int:
        """
        Extrait le palier de membres d'un trigger "count>=3".

        Args:
            raw: Fragment de trigger commençant par "count"

        Returns:
            Le nombre de membres attendu
        """
        digits = re.search(r'(\d+)', raw)
        if not digits:
            raise ValueError("Nombre manquant (ex: on count>=3)")
        
        count = int(digits.group(1))
        if count < 1:
            raise ValueError("Le palier de membres doit être au moins 1.")
        return count

    def _parse_duration(self, duration_str: str) -> Dict:
        """
        Parse une durée en données de trigger.
        
        Args:
            duration_str: Durée au format Xs, Xm, ou Xh
            
        Returns:
            Dictionnaire avec interval_seconds, ou interval_min/interval_max
            pour une plage aléatoire ("10m-20m")
        """
        low, high = parse_duration_range(duration_str)
        
        if low <= 0:
            raise ValueError("L'intervalle d'un timer doit être supérieur à 0.")
        
        # Plage: l'intervalle est retiré au sort avant chaque déclenchement
        if high > low:
            return {'interval_min': low, 'interval_max': high}
        return {'interval_seconds': low}

    def _parse_conditions(self, condition_str: str) -> List[Dict]:
        """
        Parse une chaîne de conditions.
        
        Args:
            condition_str: Chaîne de conditions séparées par "and"
            
        Returns:
            Liste de conditions feuilles, dans l'ordre d'écriture
        """
        cond_list = []
        cond_tokens = condition_str.split(" and ")
        
        for token in cond_tokens:
            token = token.strip()
            
            # Déterminer l'opérateur (les composés d'abord : >= avant >)
            for symbol, operator in (("!=", "!="), (">=", ">="), ("<=", "<="),
                                     ("==", "=="), (">", ">"), ("<", "<"), ("=", "==")):
                if symbol in token:
                    op = operator
                    key, val = token.split(symbol, 1)
                    break
            else:
                raise ValueError(f"Format de condition invalide: {token}")
            
            key = key.strip().lower()
            val = val.strip()
            
            # Le type est résolu dans le catalogue de blocs
            block = condition_by_alias(key)
            if block is None:
                raise ValueError(
                    f"Clé de condition inconnue: {key}. "
                    f"Utilisez: {', '.join(all_condition_aliases())}"
                )
            
            if op not in block.ops:
                raise ValueError(
                    f"Opérateur '{op}' non supporté pour '{key}'. "
                    f"Utilisez: {', '.join(block.ops)}"
                )
            
            cond_list.append({
                "type": block.type,
                "value": val,
                "op": op
            })
        
        return cond_list

    def _parse_actions(self, actions_str: str) -> List[Dict]:
        """
        Parse une chaîne d'actions.

        Chaque verbe est résolu dans le catalogue de blocs : ajouter une
        action au catalogue la rend utilisable ici sans rien changer.

        Args:
            actions_str: Actions séparées par "then"

        Returns:
            Liste de dictionnaires d'actions
        """
        actions = []

        for token in actions_str.split(" then "):
            token = token.strip()
            if not token:
                continue

            parts = token.split(" ", 1)
            verb = parts[0].lower()
            args = parts[1].strip() if len(parts) > 1 else ""

            block = action_by_verb(verb)
            if block is None or block.parse is None:
                raise ValueError(
                    f"Action inconnue: {verb}. "
                    f"Utilisez: {', '.join(all_action_verbs())}"
                )

            actions.append(block.parse(verb, args))

        return actions

    def _parse_wait_action(duration_str: str) -> Dict:
        """
        Construit une action wait, fixe ou aléatoire.

        Accepte "5s", "1m20s" ou une plage "1m20s-2h".

        Args:
            duration_str: Durée ou plage de durées

        Returns:
            Dictionnaire d'action prêt à être stocké
        """
        low, high = parse_duration_range(duration_str)
        
        if high > low:
            return {"type": "wait", "delay_min": low, "delay_max": high}
        return {"type": "wait", "delay": low}