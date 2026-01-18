"""
Module de gestion de la lecture audio pour le bot Soundboard.

Ce module gère la connexion aux salons vocaux Discord et la lecture
des fichiers audio via FFmpeg. Il implémente un système de file d'attente
pour gérer plusieurs sons consécutifs.

Auteur: Soundboard Bot
"""

import discord
import asyncio
import logging
from collections import deque
from typing import Optional, Dict, Tuple, NamedTuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class QueueItem:
    """
    Élément de la file d'attente de lecture.
    
    Attributes:
        source_path: Chemin vers le fichier audio
        requester_name: Nom de l'utilisateur ayant demandé le son
        sound_name: Nom du son à jouer
        channel: Salon vocal cible
    """
    source_path: str
    requester_name: str
    sound_name: str
    channel: discord.VoiceChannel


class GuildPlayer:
    """
    Gestionnaire de lecture audio pour un serveur Discord.
    
    Gère la connexion vocale, la file d'attente et la lecture
    séquentielle des sons pour un serveur spécifique.
    
    Attributes:
        guild_id: ID du serveur Discord
        bot: Instance du bot Discord
        queue: File d'attente des sons à jouer
        voice_client: Client vocal Discord actuel
        current_sound: Son actuellement en lecture
        voice_timeout: Délai avant déconnexion automatique
    """
    
    # Options FFmpeg pour la lecture audio
    # Note: Les options reconnect ne sont pas supportées par toutes les versions de FFmpeg
    FFMPEG_OPTIONS = {
        'options': '-vn'  # Pas de vidéo, audio uniquement
    }
    
    def __init__(self, guild_id: int, bot, voice_timeout: int):
        """
        Initialise le player pour un serveur.
        
        Args:
            guild_id: ID du serveur Discord
            bot: Instance du bot
            voice_timeout: Délai en secondes avant déconnexion automatique
        """
        self.guild_id = guild_id
        self.bot = bot
        self.queue: deque[QueueItem] = deque()
        self.voice_client: Optional[discord.VoiceClient] = None
        self.current_sound: Optional[Tuple[str, str]] = None  # (sound_name, requester)
        self.voice_timeout = voice_timeout
        self._disconnect_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()  # Protection contre les accès concurrents

    async def join(self, channel: discord.VoiceChannel) -> bool:
        """
        Rejoint un salon vocal.
        
        Args:
            channel: Salon vocal à rejoindre
            
        Returns:
            True si la connexion a réussi
        """
        try:
            if self.voice_client is None or not self.voice_client.is_connected():
                self.voice_client = await channel.connect(timeout=10.0, reconnect=True)
                logger.info(f"Connecté au salon vocal: {channel.name} (guild={self.guild_id})")
            elif self.voice_client.channel.id != channel.id:
                await self.voice_client.move_to(channel)
                logger.info(f"Déplacé vers le salon: {channel.name} (guild={self.guild_id})")
            
            # Annuler le timer de déconnexion si actif
            self._cancel_disconnect_timer()
            return True
            
        except asyncio.TimeoutError:
            logger.error(f"Timeout lors de la connexion au salon {channel.name}")
            return False
        except discord.ClientException as e:
            logger.error(f"Erreur client Discord lors de la connexion: {e}")
            return False

    async def disconnect(self) -> None:
        """Déconnecte le bot du salon vocal."""
        if self.voice_client and self.voice_client.is_connected():
            try:
                await self.voice_client.disconnect(force=True)
                logger.info(f"Déconnecté du salon vocal (guild={self.guild_id})")
            except Exception as e:
                logger.warning(f"Erreur lors de la déconnexion: {e}")
        self.voice_client = None
        self._cancel_disconnect_timer()

    def _cancel_disconnect_timer(self) -> None:
        """Annule le timer de déconnexion automatique."""
        if self._disconnect_task and not self._disconnect_task.done():
            self._disconnect_task.cancel()
            self._disconnect_task = None

    def _start_disconnect_timer(self) -> None:
        """Démarre le timer de déconnexion automatique."""
        self._cancel_disconnect_timer()
        
        if self.voice_timeout > 0:
            self._disconnect_task = asyncio.create_task(self._auto_disconnect())

    async def _auto_disconnect(self) -> None:
        """
        Coroutine de déconnexion automatique.
        
        Attend le délai configuré puis déconnecte si aucune lecture.
        """
        try:
            await asyncio.sleep(self.voice_timeout)
            
            # Vérifier qu'il n'y a plus de lecture en cours
            if not self.queue and not self.current_sound:
                await self.disconnect()
                logger.info(f"Déconnexion automatique après {self.voice_timeout}s d'inactivité")
                
        except asyncio.CancelledError:
            pass  # Timer annulé, c'est normal

    def add_to_queue(
        self,
        source_path: str,
        requester_name: str,
        sound_name: str,
        channel: discord.VoiceChannel
    ) -> int:
        """
        Ajoute un son à la file d'attente.
        
        Args:
            source_path: Chemin vers le fichier audio
            requester_name: Nom de l'utilisateur
            sound_name: Nom du son
            channel: Salon vocal cible
            
        Returns:
            Position dans la file d'attente (0 = en cours de lecture)
        """
        item = QueueItem(
            source_path=source_path,
            requester_name=requester_name,
            sound_name=sound_name,
            channel=channel
        )
        self.queue.append(item)
        position = len(self.queue)
        
        logger.debug(f"Son ajouté à la queue: {sound_name} (position={position})")
        
        # Lancer la lecture si rien n'est en cours
        if not self.current_sound:
            asyncio.run_coroutine_threadsafe(self.process_next(), self.bot.loop)
        
        return position

    async def process_next(self) -> None:
        """
        Traite le prochain élément de la file d'attente.
        
        Gère la connexion au salon vocal et lance la lecture.
        Thread-safe grâce au verrou asyncio.
        """
        async with self._lock:
            # Vérifier si la queue est vide
            if not self.queue:
                self.current_sound = None
                self._start_disconnect_timer()
                return

            # Vérifier si une lecture est déjà en cours
            if self.voice_client and self.voice_client.is_playing():
                return

            # Récupérer le prochain élément
            item = self.queue[0]

            # Se connecter au salon vocal
            try:
                if not await self.join(item.channel):
                    # Échec de connexion, retirer l'élément et passer au suivant
                    self.queue.popleft()
                    await self.process_next()
                    return
            except Exception as e:
                logger.error(f"Erreur de connexion vocale: {e}")
                self.queue.popleft()
                await self.process_next()
                return

            # Retirer l'élément de la queue après connexion réussie
            self.queue.popleft()
            self.current_sound = (item.sound_name, item.requester_name)

            logger.info(f"▶️ Lecture de '{item.sound_name}' dans {item.channel.name}")

            # Lancer la lecture
            try:
                source = discord.FFmpegPCMAudio(
                    item.source_path,
                    **self.FFMPEG_OPTIONS
                )
                # Appliquer un transformateur de volume pour éviter la saturation
                source = discord.PCMVolumeTransformer(source, volume=0.7)
                
                self.voice_client.play(
                    source,
                    after=lambda e: self._after_play(e)
                )
            except Exception as e:
                logger.error(f"Erreur lors du démarrage de la lecture: {e}")
                self._after_play(e)

    def _after_play(self, error: Optional[Exception]) -> None:
        """
        Callback appelé après la fin de la lecture.
        
        Args:
            error: Exception éventuelle survenue pendant la lecture
        """
        if error:
            logger.error(f"Erreur pendant la lecture: {error}")
        
        self.current_sound = None
        
        # Planifier le traitement du prochain son
        coro = self.process_next()
        fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
        
        try:
            fut.result(timeout=5.0)  # Attendre max 5 secondes
        except asyncio.TimeoutError:
            logger.warning("Timeout lors du passage au son suivant")
        except Exception as e:
            logger.error(f"Erreur lors du traitement du son suivant: {e}")

    def stop(self) -> None:
        """
        Arrête la lecture en cours et vide la file d'attente.
        """
        self.queue.clear()
        
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
            logger.info(f"Lecture arrêtée (guild={self.guild_id})")
        
        self.current_sound = None
        self._start_disconnect_timer()

    def skip(self) -> bool:
        """
        Passe au son suivant dans la file d'attente.
        
        Returns:
            True si un son a été passé
        """
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()  # Déclenche after_play automatiquement
            return True
        return False

    def clear_queue(self) -> int:
        """
        Vide la file d'attente sans arrêter la lecture en cours.
        
        Returns:
            Nombre d'éléments supprimés
        """
        count = len(self.queue)
        self.queue.clear()
        logger.info(f"Queue vidée: {count} élément(s) supprimé(s)")
        return count

    def get_queue_info(self) -> Dict:
        """
        Récupère les informations sur l'état actuel du player.
        
        Returns:
            Dictionnaire avec les infos de la queue et du son en cours
        """
        return {
            'is_playing': self.voice_client.is_playing() if self.voice_client else False,
            'is_connected': self.voice_client.is_connected() if self.voice_client else False,
            'current_sound': self.current_sound,
            'queue_length': len(self.queue),
            'queue': [
                {'name': item.sound_name, 'requester': item.requester_name}
                for item in self.queue
            ]
        }


class PlayerManager:
    """
    Gestionnaire central des players audio.
    
    Maintient un player par serveur et gère leur cycle de vie.
    
    Attributes:
        bot: Instance du bot Discord
        voice_timeout: Délai de déconnexion automatique
        players: Dictionnaire des players par ID de serveur
    """
    
    def __init__(self, bot, voice_timeout: int):
        """
        Initialise le gestionnaire de players.
        
        Args:
            bot: Instance du bot Discord
            voice_timeout: Délai en secondes avant déconnexion automatique
        """
        self.bot = bot
        self.voice_timeout = voice_timeout
        self.players: Dict[int, GuildPlayer] = {}

    def get_player(self, guild_id: int) -> GuildPlayer:
        """
        Récupère ou crée un player pour un serveur.
        
        Args:
            guild_id: ID du serveur Discord
            
        Returns:
            Instance de GuildPlayer pour le serveur
        """
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer(
                guild_id,
                self.bot,
                self.voice_timeout
            )
            logger.debug(f"Nouveau player créé pour guild={guild_id}")
        
        return self.players[guild_id]

    async def disconnect_all(self) -> None:
        """Déconnecte tous les players (utile pour le shutdown)."""
        for guild_id, player in self.players.items():
            try:
                await player.disconnect()
            except Exception as e:
                logger.error(f"Erreur lors de la déconnexion du player {guild_id}: {e}")
        
        self.players.clear()
        logger.info("Tous les players ont été déconnectés")

    def get_active_players(self) -> Dict[int, GuildPlayer]:
        """
        Récupère tous les players actuellement connectés.
        
        Returns:
            Dictionnaire des players actifs
        """
        return {
            guild_id: player
            for guild_id, player in self.players.items()
            if player.voice_client and player.voice_client.is_connected()
        }
