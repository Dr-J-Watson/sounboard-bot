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
import time
import datetime
import os
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field

from config import Config

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

    async def load_routines(self) -> int:
        """
        Charge toutes les routines actives depuis la base de données.
        
        Returns:
            Nombre de routines chargées
        """
        self.routines = []
        
        for guild in self.bot.guilds:
            try:
                routines = await self.db.get_routines(str(guild.id))
                for r in routines:
                    if r['active']:
                        # Initialiser le timestamp de dernière exécution
                        r['_last_run'] = 0
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
            await self._execute_actions(routine, context)
            routine['_last_run'] = current_time
            logger.debug(f"Timer routine '{routine['name']}' exécutée")

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
                
                logger.debug(f"  -> Vérification des conditions...")
                if await self._check_conditions(routine, context):
                    logger.info(f"🎯 Routine '{routine['name']}' déclenchée par {event_type}")
                    triggered_routines.add(routine_key)
                    await self._execute_actions(routine, context)
                else:
                    logger.debug(f"  -> Conditions non satisfaites")
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
            true_count = sum(
                1 for sub in node.get('sub', [])
                if asyncio.get_event_loop().run_until_complete(
                    self._evaluate_condition_node(sub, context)
                )
            )
            # Version async propre
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
        - user_id: ID d'utilisateur
        - channel_id: ID de salon
        - role_id: ID de rôle
        - time_range: Plage horaire (HH:MM-HH:MM)
        - date_range: Plage de dates (DD/MM-DD/MM)
        
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

        # Comparaison standard
        if actual_value is None:
            return False

        if op == '==':
            return actual_value == value
        elif op == '!=':
            return actual_value != value
        
        return False

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
                
                # Gestion de l'attente (wait)
                if action_type == 'wait':
                    delay = action.get('delay', 0)
                    if delay > 0:
                        logger.debug(f"⏳ Attente de {delay}s...")
                        await asyncio.sleep(delay)
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
                else:
                    logger.warning(f"Type d'action inconnu: {action_type}")
                    
            except Exception as e:
                logger.error(
                    f"Erreur lors de l'exécution de l'action {action} "
                    f"dans la routine '{routine['name']}': {e}",
                    exc_info=True
                )

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
        # Priorité 1: Canal du contexte
        if context and context.get('channel'):
            return context['channel']
        
        # Priorité 2: Canal spécifique dans l'action
        target_strategy = action.get('target_strategy', 'active')
        
        if target_strategy == 'specific':
            channel_id = action.get('channel_id')
            if channel_id:
                channel = guild.get_channel(int(channel_id))
                if isinstance(channel, discord.VoiceChannel):
                    return channel
        
        # Priorité 3: Canal avec des membres (stratégie 'active')
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
        
        if tokens[0] == "timer":
            if len(tokens) < 2:
                raise ValueError("Durée timer manquante (ex: timer 3s)")
            
            duration_str = tokens[1]
            trigger_data = self._parse_duration(duration_str)
            trigger_type = "timer"
            
        elif tokens[0] == "on":
            if len(tokens) < 2:
                raise ValueError("Événement manquant (ex: on join)")
            
            event_map = {
                "join": "voice_join",
                "leave": "voice_leave",
                "move": "voice_move"
            }
            
            evt = tokens[1].lower()
            if evt not in event_map:
                raise ValueError(
                    f"Événement inconnu '{evt}'. "
                    f"Utilisez: {', '.join(event_map.keys())}"
                )
            
            trigger_type = "event"
            trigger_data['event'] = event_map[evt]
        else:
            raise ValueError(
                f"Trigger inconnu '{tokens[0]}'. "
                "Commencez par 'timer' ou 'on'."
            )
        return trigger_type, trigger_data

    def _parse_duration(self, duration_str: str) -> Dict:
        """
        Parse une durée en données de trigger.
        
        Args:
            duration_str: Durée au format Xs, Xm, ou Xh
            
        Returns:
            Dictionnaire avec interval_seconds ou interval_minutes
        """
        duration_str = duration_str.lower().strip()
        
        if duration_str.endswith("s"):
            return {'interval_seconds': int(duration_str[:-1])}
        elif duration_str.endswith("m"):
            return {'interval_minutes': int(duration_str[:-1])}
        elif duration_str.endswith("h"):
            return {'interval_minutes': int(duration_str[:-1]) * 60}
        elif duration_str.isdigit():
            return {'interval_seconds': int(duration_str)}
        else:
            raise ValueError(
                f"Format de durée invalide '{duration_str}'. "
                "Utilisez 30s, 5m, ou 1h."
            )

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
            
            # Déterminer l'opérateur
            if "!=" in token:
                op = "!="
                key, val = token.split("!=", 1)
            elif "=" in token:
                op = "=="
                key, val = token.split("=", 1)
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
                "date": "date_range"
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
                duration = self._parse_wait_duration(args)
                actions.append({
                    "type": "wait",
                    "delay": duration
                })
            elif verb == "msg" or verb == "message":
                # Format: msg #channel_id message content
                # ou: msg message content (utilise le channel par défaut)
                actions.append({
                    "type": "message",
                    "content": args,
                    "channel_id": None  # À définir par l'utilisateur
                })
            else:
                raise ValueError(
                    f"Action inconnue: {verb}. "
                    "Utilisez: play, wait, msg"
                )
        
        return actions

    def _parse_wait_duration(self, duration_str: str) -> int:
        """
        Parse une durée d'attente en secondes.
        
        Args:
            duration_str: Durée au format Xs ou X
            
        Returns:
            Durée en secondes
        """
        duration_str = duration_str.lower().strip()
        
        if duration_str.endswith("s"):
            return int(duration_str[:-1])
        elif duration_str.endswith("m"):
            return int(duration_str[:-1]) * 60
        elif duration_str.isdigit():
            return int(duration_str)
        else:
            return 0
