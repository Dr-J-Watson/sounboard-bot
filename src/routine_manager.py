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
from dataclasses import dataclass, field

from config import Config

logger = logging.getLogger(__name__)


# Durées composées : "1m20s", "2h", "90", "1h30m10s"
_DURATION_TOKEN_RE = re.compile(r'(\d+)\s*([hms])', re.IGNORECASE)
_UNIT_SECONDS = {'h': 3600, 'm': 60, 's': 1}

# Jours de la semaine acceptés dans les routines (index lundi=0)
WEEKDAYS = {
    'lun': 0, 'lundi': 0, 'mon': 0, 'monday': 0,
    'mar': 1, 'mardi': 1, 'tue': 1, 'tuesday': 1,
    'mer': 2, 'mercredi': 2, 'wed': 2, 'wednesday': 2,
    'jeu': 3, 'jeudi': 3, 'thu': 3, 'thursday': 3,
    'ven': 4, 'vendredi': 4, 'fri': 4, 'friday': 4,
    'sam': 5, 'samedi': 5, 'sat': 5, 'saturday': 5,
    'dim': 6, 'dimanche': 6, 'sun': 6, 'sunday': 6,
}


def parse_duration_seconds(text: str) -> int:
    """
    Convertit une durée écrite en secondes.

    Accepte les formes composées et les unités mélangées :
    "30s", "5m", "2h", "1m20s", "1h30m10s", ou un nombre nu (= secondes).

    Args:
        text: Durée à convertir

    Returns:
        Durée en secondes

    Raises:
        ValueError: Si le format n'est pas reconnu
    """
    if text is None:
        raise ValueError("Durée vide.")

    cleaned = str(text).strip().lower().replace(" ", "")
    if not cleaned:
        raise ValueError("Durée vide.")

    # Nombre nu : interprété en secondes
    if cleaned.isdigit():
        return int(cleaned)

    matches = _DURATION_TOKEN_RE.findall(cleaned)
    if not matches:
        raise ValueError(
            f"Format de durée invalide '{text}'. "
            "Utilisez par exemple 30s, 5m, 2h ou 1m20s."
        )

    # Refuser les restes non reconnus ("5x", "1m20z")
    consumed = "".join(f"{value}{unit}" for value, unit in matches)
    if consumed != cleaned:
        raise ValueError(
            f"Format de durée invalide '{text}'. "
            "Utilisez par exemple 30s, 5m, 2h ou 1m20s."
        )

    return sum(int(value) * _UNIT_SECONDS[unit.lower()] for value, unit in matches)


def parse_duration_range(text: str) -> Tuple[int, int]:
    """
    Convertit une durée ou une plage de durées en secondes.

    "5s" donne (5, 5) ; "1m20s-2h" donne (80, 7200). Les bornes sont
    réordonnées si elles sont écrites à l'envers.

    Args:
        text: Durée simple ou plage "min-max"

    Returns:
        Tuple (minimum, maximum) en secondes

    Raises:
        ValueError: Si le format n'est pas reconnu
    """
    cleaned = str(text).strip().lower().replace(" ", "")

    if "-" in cleaned:
        low_raw, _, high_raw = cleaned.partition("-")
        low = parse_duration_seconds(low_raw)
        high = parse_duration_seconds(high_raw)
        return (low, high) if low <= high else (high, low)

    value = parse_duration_seconds(cleaned)
    return value, value


def format_duration(seconds: int) -> str:
    """
    Formate une durée en secondes de façon lisible ("1m20s", "2h").

    Args:
        seconds: Durée en secondes

    Returns:
        Représentation compacte de la durée
    """
    seconds = int(seconds)
    if seconds <= 0:
        return "0s"

    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)

    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return "".join(parts)


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
    
    # Mapping des événements vocaux
    EVENT_MAPPING = {
        "voice_join": "join",
        "voice_leave": "leave", 
        "voice_move": "move",
        "voice_mute": "mute",
        "voice_unmute": "unmute",
        "voice_deafen": "deafen",
        "voice_undeafen": "undeafen",
        "voice_stream_start": "stream_start",
        "voice_stream_stop": "stream_stop",
        "voice_video_start": "video_start",
        "voice_video_stop": "video_stop"
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
                    if routine['trigger_type'] == 'timer':
                        await self._process_timer_routine(routine, current_time)
                    elif routine['trigger_type'] == 'schedule':
                        await self._process_schedule_routine(routine, current_time)
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Erreur dans la boucle timer: {e}", exc_info=True)
            
            await asyncio.sleep(1)

    async def _process_timer_routine(self, routine: Dict, current_time: float) -> None:
        """
        Traite une routine de type timer.
        
        Args:
            routine: Données de la routine
            current_time: Timestamp actuel
        """
        last_run = routine.get('_last_run', 0)
        trigger_data = routine['trigger_data']
        
        # Calculer l'intervalle en secondes
        interval = trigger_data.get('interval_seconds', 0)
        if interval == 0:
            interval = trigger_data.get('interval_minutes', 0) * 60
        
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
            routine['_last_run'] = current_time
            self._spawn_actions(routine, context)
            logger.debug(f"Timer routine '{routine['name']}' exécutée")

    async def _process_schedule_routine(self, routine: Dict, current_time: float) -> None:
        """
        Traite une routine déclenchée à heure fixe ("at 18:00").

        Contrairement au timer qui compte un intervalle, celle-ci se
        déclenche quand l'horloge atteint l'heure demandée, éventuellement
        restreinte à certains jours de la semaine.

        Args:
            routine: Données de la routine
            current_time: Timestamp actuel
        """
        trigger_data = routine['trigger_data']
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
        if routine.get('_last_run', 0) >= minute_start:
            return

        guild = self.bot.get_guild(int(routine['guild_id']))
        if not guild:
            return

        context = await self._find_valid_context(routine, guild)
        if context:
            routine['_last_run'] = current_time
            self._spawn_actions(routine, context)
            logger.debug(f"Routine horaire '{routine['name']}' exécutée à {target}")

    async def _find_valid_context(
        self,
        routine: Dict,
        guild: discord.Guild
    ) -> Optional[RoutineContext]:
        """
        Trouve un contexte valide pour exécuter une routine.
        
        Parcourt les salons vocaux pour trouver un membre/channel
        qui satisfait les conditions de la routine.
        
        Args:
            routine: Données de la routine
            guild: Serveur Discord
            
        Returns:
            RoutineContext si trouvé, None sinon
        """
        # Vérifier si la routine a des conditions utilisateur spécifiques
        has_user_condition = False
        conditions = routine.get('conditions')
        if conditions:
            has_user_condition = self._has_user_condition(conditions)
        
        # Parcourir les salons vocaux avec des membres
        for vc in guild.voice_channels:
            if not vc.members:
                continue
                
            for member in vc.members:
                if member.bot:
                    continue
                    
                ctx = RoutineContext(guild=guild, channel=vc, member=member)
                
                if await self._check_conditions(routine, ctx):
                    return ctx
        
        # Si pas de condition utilisateur, créer un contexte minimal
        if not has_user_condition:
            # Trouver un salon avec des membres pour la lecture audio
            for vc in guild.voice_channels:
                if vc.members:
                    return RoutineContext(guild=guild, channel=vc)
        
        return None

    def _has_user_condition(self, conditions: Dict) -> bool:
        """Vérifie si les conditions contiennent une condition utilisateur."""
        if conditions.get('type') == 'user_id':
            return True
        if conditions.get('type') in ('AND', 'OR', 'XOR'):
            for sub in conditions.get('sub', []):
                if self._has_user_condition(sub):
                    return True
        return False

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
                # Éviter les doublons (ex: une routine voice_join ne doit pas se déclencher 2 fois)
                routine_key = (routine['id'], event_type)
                if routine_key in triggered_routines:
                    continue
                    
                logger.debug(f"Vérification routine '{routine['name']}' pour {event_type}")
                
                if str(routine['guild_id']) != str(member.guild.id):
                    continue
                
                if routine['trigger_type'] != 'event':
                    continue
                    
                if routine['trigger_data'].get('event') != event_type:
                    continue
                
                # Palier de membres : la routine ne se déclenche que sur le
                # nombre exact demandé, pour ne pas re-jouer à chaque arrivée.
                if event_type == "voice_count_reached":
                    expected = routine['trigger_data'].get('count')
                    if expected is None or human_count != int(expected):
                        continue
                
                logger.debug(f"  -> Vérification des conditions...")
                if await self._check_conditions(routine, context):
                    logger.info(f"🎯 Routine '{routine['name']}' déclenchée par {event_type}")
                    triggered_routines.add(routine_key)
                    self._spawn_actions(routine, context)
                else:
                    logger.debug(f"  -> Conditions non satisfaites")
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
            if routine['trigger_type'] != 'event':
                continue
            if routine['trigger_data'].get('event') != 'message':
                continue
            if str(routine['guild_id']) != str(message.guild.id):
                continue

            keyword = (routine['trigger_data'].get('keyword') or "").lower().strip()
            if not keyword or keyword not in content:
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
            if routine['trigger_type'] != 'event':
                continue
            if routine['trigger_data'].get('event') != 'reaction':
                continue
            if str(routine['guild_id']) != str(member.guild.id):
                continue

            expected = (routine['trigger_data'].get('emoji') or "").strip()
            if expected and expected != emoji:
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
        Évalue une condition feuille (non composite).
        
        Types supportés:
        - user_id: ID d'utilisateur (liste possible: "1,2,3")
        - channel_id: ID de salon (liste possible)
        - role_id: ID de rôle (liste possible)
        - time_range: Plage horaire (HH:MM-HH:MM)
        - date_range: Plage de dates (DD/MM-DD/MM)
        - member_count: Nombre d'humains dans le salon (ops >, <, >=, <=)
        - chance: Probabilité en pourcentage
        - weekday: Jours de la semaine ("lun,mar,ven")
        - is_playing: Le bot est-il déjà en train de jouer ("true"/"false")
        
        Args:
            node: Nœud de condition
            context: Contexte d'exécution
            
        Returns:
            True si la condition est satisfaite
        """
        c_type = node.get('type')
        op = node.get('op', '==')
        value = str(node.get('value', ''))
        
        logger.debug(f"Évaluation condition: type={c_type}, op={op}, value={value}")
        
        # Récupérer la valeur actuelle selon le type
        actual_value = None
        
        if c_type == 'user_id':
            if context and context.get('member'):
                actual_value = str(context['member'].id)
                logger.debug(f"  user_id: actual={actual_value}, expected={value}")
                
        elif c_type == 'channel_id':
            if context and context.get('channel'):
                actual_value = str(context['channel'].id)
                
        elif c_type == 'role_id':
            if context and context.get('member'):
                member_roles = [str(r.id) for r in context['member'].roles]
                if op == '==':
                    return value in member_roles
                elif op == '!=':
                    return value not in member_roles
            return False
            
        elif c_type == 'time_range':
            return self._check_time_range(value)
            
        elif c_type == 'date_range':
            return self._check_date_range(value)
        
        elif c_type == 'member_count':
            return self._check_member_count(op, value, context)
        
        elif c_type == 'chance':
            return self._check_chance(value)
        
        elif c_type == 'weekday':
            return self._check_weekday(op, value)
        
        elif c_type == 'is_playing':
            return self._check_is_playing(op, value, context)

        # Comparaison standard
        if actual_value is None:
            return False

        # Une valeur peut lister plusieurs IDs séparés par des virgules
        if ',' in value:
            allowed = {v.strip() for v in value.split(',') if v.strip()}
            return actual_value in allowed if op == '==' else actual_value not in allowed

        if op == '==':
            return actual_value == value
        elif op == '!=':
            return actual_value != value
        
        return False

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
    def _check_weekday(op: str, value: str) -> bool:
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

    async def _execute_actions(
        self,
        routine: Dict,
        context: Optional[RoutineContext]
    ) -> None:
        """
        Exécute les actions d'une routine.
        
        Args:
            routine: Données de la routine
            context: Contexte d'exécution
        """
        actions = routine.get('actions', [])
        
        logger.debug(f"Exécution de {len(actions)} action(s) pour routine '{routine['name']}'")
        
        for i, action in enumerate(actions):
            try:
                action_type = action.get('type')
                logger.debug(f"Action {i+1}/{len(actions)}: type={action_type}, data={action}")
                
                # Gestion de l'attente (wait), éventuellement aléatoire
                if action_type == 'wait':
                    delay = self._resolve_delay(action)
                    if delay > 0:
                        logger.debug(f"⏳ Attente de {format_duration(delay)}...")
                        await asyncio.sleep(delay)
                    continue
                
                # Tirage au sort : coupe la routine si le tirage échoue
                if action_type == 'chance':
                    percent = action.get('percent', 100)
                    if not self._check_chance(percent):
                        logger.debug(f"🎲 Tirage {percent}% raté, routine interrompue")
                        return
                    logger.debug(f"🎲 Tirage {percent}% réussi")
                    continue
                
                # Pour les autres actions, reconstruire le contexte frais
                # car après un délai, le membre peut avoir changé de salon
                fresh_context = None
                if context:
                    fresh_context = await self._refresh_context(context, routine)
                
                ctx_dict = fresh_context.to_dict() if fresh_context else None
                
                if action_type == 'play_sound':
                    await self._action_play_sound(action, ctx_dict, routine)
                elif action_type == 'message':
                    await self._action_send_message(action, ctx_dict)
                elif action_type == 'dm':
                    await self._action_send_dm(action, ctx_dict)
                elif action_type == 'player_control':
                    await self._action_player_control(action, ctx_dict)
                elif action_type == 'volume':
                    await self._action_set_volume(action, ctx_dict)
                elif action_type == 'move':
                    await self._action_move(action, ctx_dict)
                else:
                    logger.warning(f"Type d'action inconnu: {action_type}")
                    
            except Exception as e:
                logger.error(
                    f"Erreur lors de l'exécution de l'action {action} "
                    f"dans la routine '{routine['name']}': {e}",
                    exc_info=True
                )

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

    async def _action_player_control(self, action: Dict, context: Optional[Dict]) -> None:
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
        elif command == 'skip':
            skipped = player.skip()
            logger.info(f"⏭️ Routine: skip {'effectué' if skipped else 'sans effet'}")
        elif command == 'clear':
            removed = player.clear_queue()
            logger.info(f"🧹 Routine: {removed} son(s) retiré(s) de la file")
        elif command == 'leave':
            await player.leave()
            logger.info("🚪 Routine: le bot a quitté le salon vocal")
        else:
            logger.warning(f"Commande de contrôle inconnue: {command}")

    async def _action_set_volume(self, action: Dict, context: Optional[Dict]) -> None:
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
            player._volume = None  # forcera une relecture en base
            restored = await player.get_volume()
            logger.info(f"🔊 Routine: volume restauré à {round(restored * 100)}%")
            return

        try:
            percent = int(raw)
        except (TypeError, ValueError):
            logger.warning(f"Valeur de volume invalide dans une routine: {raw}")
            return

        applied = player.set_volume(percent)
        logger.info(f"🔊 Routine: volume réglé sur {round(applied * 100)}%")

    async def _action_move(self, action: Dict, context: Optional[Dict]) -> None:
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

    async def _action_send_dm(self, action: Dict, context: Optional[Dict]) -> None:
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
        context: Optional[Dict],
        routine: Dict
    ) -> None:
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
        player.add_to_queue(file_path, "Routine", sound_name, channel)
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
        context: Optional[Dict]
    ) -> None:
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
        
        # Parser le trigger et les conditions
        trigger_type, trigger_data, conditions = self._parse_trigger_and_conditions(lhs)
        
        # Parser les actions
        actions = self._parse_actions(rhs)
        
        return trigger_type, trigger_data, conditions, actions

    def _parse_trigger_and_conditions(
        self,
        lhs: str
    ) -> Tuple[str, Dict, Optional[Dict]]:
        """
        Parse la partie gauche (trigger + conditions).
        
        Args:
            lhs: Partie gauche de la commande
            
        Returns:
            Tuple (trigger_type, trigger_data, conditions)
        """
        # Séparer trigger et conditions
        if " if " in lhs:
            trigger_part, condition_part = lhs.split(" if ", 1)
        else:
            trigger_part = lhs
            condition_part = None
        
        # Parser le trigger
        trigger_type, trigger_data = self._parse_trigger(trigger_part)
        
        # Parser les conditions
        conditions = self._parse_conditions(condition_part) if condition_part else None
        
        return trigger_type, trigger_data, conditions

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
            Dictionnaire avec interval_seconds ou interval_minutes
        """
        seconds = parse_duration_seconds(duration_str)
        if seconds <= 0:
            raise ValueError("L'intervalle d'un timer doit être supérieur à 0.")
        return {'interval_seconds': seconds}

    def _parse_conditions(self, condition_str: str) -> Optional[Dict]:
        """
        Parse une chaîne de conditions.
        
        Args:
            condition_str: Chaîne de conditions séparées par "and"
            
        Returns:
            Dictionnaire de conditions ou None
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
            
            # Mapper les clés aux types
            type_map = {
                "user": "user_id",
                "channel": "channel_id",
                "role": "role_id",
                "time": "time_range",
                "date": "date_range",
                "count": "member_count",
                "members": "member_count",
                "chance": "chance",
                "day": "weekday",
                "jour": "weekday",
                "playing": "is_playing"
            }
            
            if key not in type_map:
                raise ValueError(
                    f"Clé de condition inconnue: {key}. "
                    f"Utilisez: {', '.join(type_map.keys())}"
                )
            
            cond_list.append({
                "type": type_map[key],
                "value": val,
                "op": op
            })
        
        # Retourner la structure appropriée
        if not cond_list:
            return None
        elif len(cond_list) == 1:
            return cond_list[0]
        else:
            return {"type": "AND", "sub": cond_list}

    def _parse_actions(self, actions_str: str) -> List[Dict]:
        """
        Parse une chaîne d'actions.
        
        Args:
            actions_str: Actions séparées par "then"
            
        Returns:
            Liste de dictionnaires d'actions
        """
        action_tokens = actions_str.split(" then ")
        actions = []
        
        for token in action_tokens:
            token = token.strip()
            parts = token.split(" ", 1)
            verb = parts[0].lower()
            args = parts[1].strip() if len(parts) > 1 else ""
            
            if verb == "play":
                if not args:
                    raise ValueError("Nom du son manquant après 'play'")
                actions.append({
                    "type": "play_sound",
                    "sound_name": args,
                    "target_strategy": "active"
                })
            elif verb == "wait":
                if not args:
                    raise ValueError("Durée manquante après 'wait'")
                actions.append(self._parse_wait_action(args))
            elif verb == "msg" or verb == "message":
                # Format: msg #channel_id message content
                # ou: msg message content (utilise le channel par défaut)
                actions.append({
                    "type": "message",
                    "content": args,
                    "channel_id": None  # À définir par l'utilisateur
                })
            elif verb == "dm" or verb == "mp":
                if not args:
                    raise ValueError("Contenu manquant après 'dm'")
                actions.append({"type": "dm", "content": args})
            elif verb in ("stop", "skip", "clear", "leave", "quit"):
                command = "leave" if verb == "quit" else verb
                actions.append({"type": "player_control", "command": command})
            elif verb == "chance":
                if not args:
                    raise ValueError("Pourcentage manquant après 'chance' (ex: chance 30%)")
                try:
                    percent = float(args.strip().rstrip('%'))
                except ValueError:
                    raise ValueError(f"Pourcentage invalide: {args}")
                if not 0 <= percent <= 100:
                    raise ValueError("Le pourcentage doit être compris entre 0 et 100.")
                actions.append({"type": "chance", "percent": percent})
            elif verb == "volume":
                if not args:
                    raise ValueError("Valeur manquante après 'volume' (ex: volume 150)")
                value = args.strip().lower()
                if value != "reset":
                    if not value.isdigit() or not 0 <= int(value) <= 200:
                        raise ValueError("Le volume doit être un entier entre 0 et 200, ou 'reset'.")
                    value = int(value)
                actions.append({"type": "volume", "value": value})
            elif verb == "move":
                # Format: move <id_salon>  |  move member <id_salon>
                parts_move = args.split()
                if not parts_move:
                    raise ValueError("Salon manquant après 'move'")
                if parts_move[0].lower() in ("member", "membre", "user"):
                    if len(parts_move) < 2:
                        raise ValueError("Salon manquant après 'move member'")
                    target, channel_id = "member", parts_move[1]
                else:
                    target, channel_id = "bot", parts_move[0]
                channel_id = channel_id.strip("<>#")
                if not channel_id.isdigit():
                    raise ValueError(f"ID de salon invalide: {channel_id}")
                actions.append({"type": "move", "target": target, "channel_id": channel_id})
            else:
                raise ValueError(
                    f"Action inconnue: {verb}. Utilisez: "
                    "play, wait, msg, dm, stop, skip, clear, leave, chance, volume, move"
                )
        
        return actions

    @staticmethod
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