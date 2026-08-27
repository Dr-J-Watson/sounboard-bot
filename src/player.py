"""
Module de gestion de la lecture audio pour le bot Soundboard.

Ce module gère la connexion aux salons vocaux Discord et la lecture
des fichiers audio via FFmpeg. Il implémente un système de file d'attente
pour gérer plusieurs sons consécutifs, éventuellement dans des salons
différents au sein d'un même serveur.

Auteur: Soundboard Bot
"""

import discord
import asyncio
import logging
from collections import deque
from typing import Optional, Dict, Tuple, Union
from dataclasses import dataclass

from config import Config

logger = logging.getLogger(__name__)

# Types de salons dans lesquels le bot peut émettre de l'audio
VoiceLike = Union[discord.VoiceChannel, discord.StageChannel]


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
    channel: VoiceLike


class GuildPlayer:
    """
    Gestionnaire de lecture audio pour un serveur Discord.

    Gère la connexion vocale, la file d'attente et la lecture
    séquentielle des sons pour un serveur spécifique.

    La file d'attente est unique pour le serveur mais chaque élément porte
    son propre salon cible. La validité du salon (existe encore, contient
    encore un humain) est vérifiée au moment de jouer le son, et non au
    moment où le bot quitte un salon : un salon momentanément vide qui se
    repeuple reste donc jouable.

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
        'before_options': '-loglevel error',  # Coupe le bruit sur stderr
        'options': '-vn'                      # Pas de vidéo, audio uniquement
    }

    # Volume de repli si la base est injoignable (0.0 - 2.0)
    FALLBACK_VOLUME = 0.7

    # Nombre de tentatives de connexion vocale (la 2e couvre le cas où
    # discord.py n'a pas encore fini de nettoyer une session précédente)
    JOIN_ATTEMPTS = 2
    JOIN_RETRY_DELAY = 1.0

    def __init__(self, guild_id: int, bot, voice_timeout: int, db=None):
        """
        Initialise le player pour un serveur.

        Args:
            guild_id: ID du serveur Discord
            bot: Instance du bot
            voice_timeout: Délai en secondes avant déconnexion automatique
            db: Gestionnaire de base de données (pour le volume par serveur)
        """
        self.guild_id = guild_id
        self.bot = bot
        self.db = db
        self._volume: Optional[float] = None  # Cache du volume, en 0.0-2.0
        self.queue: deque[QueueItem] = deque()
        self.voice_client: Optional[discord.VoiceClient] = None
        self.current_sound: Optional[Tuple[str, str]] = None  # (sound_name, requester)
        self.voice_timeout = voice_timeout
        self._disconnect_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()  # Protection contre les accès concurrents

    # ------------------------------------------------------------------
    # Connexion vocale
    # ------------------------------------------------------------------

    def _sync_voice_client(self) -> Optional[discord.VoiceClient]:
        """
        Resynchronise self.voice_client avec l'état réel de discord.py.

        Indispensable : le bot peut être déconnecté sans passer par ce
        player (déconnexion manuelle par un utilisateur, disconnect appelé
        ailleurs, coupure réseau). Sans cette resynchronisation on garde un
        client périmé, et channel.connect() lève alors
        "Already connected to a voice channel".

        Returns:
            Le client vocal réel du serveur, ou None
        """
        guild = self.bot.get_guild(self.guild_id)
        vc = guild.voice_client if guild else None

        if vc is not None and not vc.is_connected():
            vc = None

        self.voice_client = vc
        return vc

    async def join(self, channel: VoiceLike) -> bool:
        """
        Rejoint un salon vocal (ou s'y déplace si déjà connecté ailleurs).

        Args:
            channel: Salon vocal à rejoindre

        Returns:
            True si la connexion a réussi
        """
        for attempt in range(1, self.JOIN_ATTEMPTS + 1):
            try:
                self._sync_voice_client()

                if self.voice_client is None:
                    # Nettoyer un éventuel client fantôme resté attaché au serveur
                    guild = self.bot.get_guild(self.guild_id)
                    ghost = guild.voice_client if guild else None
                    if ghost is not None:
                        try:
                            await ghost.disconnect(force=True)
                        except Exception:
                            pass

                    self.voice_client = await channel.connect(timeout=10.0, reconnect=True)
                    logger.info(f"Connecté au salon vocal: {channel.name} (guild={self.guild_id})")

                elif self.voice_client.channel.id != channel.id:
                    await self.voice_client.move_to(channel)
                    logger.info(f"Déplacé vers le salon: {channel.name} (guild={self.guild_id})")

                # Annuler le timer de déconnexion si actif
                self._cancel_disconnect_timer()
                return True

            except (asyncio.TimeoutError, discord.ClientException, discord.DiscordException) as e:
                self.voice_client = None
                if attempt < self.JOIN_ATTEMPTS:
                    logger.warning(
                        f"Connexion à '{channel.name}' échouée ({e}), "
                        f"nouvelle tentative dans {self.JOIN_RETRY_DELAY}s"
                    )
                    await asyncio.sleep(self.JOIN_RETRY_DELAY)
                    continue
                logger.error(f"Connexion vocale impossible à '{channel.name}': {e}")
                return False

            except Exception as e:
                self.voice_client = None
                logger.error(f"Erreur inattendue lors de la connexion à '{channel.name}': {e}")
                return False

        return False

    async def disconnect(self) -> None:
        """Déconnecte le bot du salon vocal (sans toucher à la file d'attente)."""
        vc = self.voice_client or self._sync_voice_client()

        if vc is not None:
            try:
                if vc.is_playing():
                    vc.stop()
                await vc.disconnect(force=True)
                logger.info(f"Déconnecté du salon vocal (guild={self.guild_id})")
            except Exception as e:
                logger.warning(f"Erreur lors de la déconnexion: {e}")

        self.voice_client = None
        self._cancel_disconnect_timer()

    async def leave(self) -> None:
        """
        Quitte le salon vocal en conservant la file d'attente.

        Les sons destinés à d'autres salons restent jouables ; ceux dont le
        salon est devenu invalide seront écartés par process_next().
        """
        self.current_sound = None
        await self.disconnect()

    # ------------------------------------------------------------------
    # Déconnexion automatique
    # ------------------------------------------------------------------

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

            # Vérifier qu'il n'y a plus rien à jouer
            if self.queue or self.current_sound:
                return
            if self.voice_client and self.voice_client.is_playing():
                return

            logger.info(f"Déconnexion automatique après {self.voice_timeout}s d'inactivité")
            await self.disconnect()

        except asyncio.CancelledError:
            pass  # Timer annulé, c'est normal

    # ------------------------------------------------------------------
    # File d'attente
    # ------------------------------------------------------------------

    def add_to_queue(
        self,
        source_path: str,
        requester_name: str,
        sound_name: str,
        channel: VoiceLike
    ) -> int:
        """
        Ajoute un son à la file d'attente.

        Args:
            source_path: Chemin vers le fichier audio
            requester_name: Nom de l'utilisateur
            sound_name: Nom du son
            channel: Salon vocal cible

        Returns:
            Position dans la file d'attente
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

        # Toujours relancer le traitement : process_next() est idempotent et
        # ressort immédiatement si une lecture est déjà en cours. Se fier au
        # seul current_sound laissait la queue bloquée dès que celui-ci
        # restait coincé sur un état incohérent.
        asyncio.run_coroutine_threadsafe(self.process_next(), self.bot.loop)

        return position

    def purge_channel(self, channel_id: int) -> int:
        """
        Retire de la file tous les sons destinés à un salon donné.

        Utilisé quand un utilisateur déconnecte le bot à la main : c'est une
        intention explicite, contrairement à un salon qui se vide.

        Args:
            channel_id: ID du salon dont les sons doivent être retirés

        Returns:
            Nombre d'éléments supprimés
        """
        kept = [item for item in self.queue if item.channel.id != channel_id]
        removed = len(self.queue) - len(kept)

        # On conserve le même objet deque pour ne pas désynchroniser
        # un process_next() en cours d'exécution.
        self.queue.clear()
        self.queue.extend(kept)

        if removed:
            logger.info(f"🧹 {removed} son(s) retiré(s) de la queue pour le salon {channel_id}")

        return removed

    def _is_channel_playable(self, channel: VoiceLike) -> bool:
        """
        Vérifie qu'un salon est encore jouable.

        Un salon est jouable s'il existe toujours sur le serveur et s'il
        contient au moins un membre humain.

        Args:
            channel: Salon vocal à tester

        Returns:
            True si le son peut y être joué
        """
        if channel is None:
            return False

        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            return False

        # Le salon a-t-il été supprimé entre temps ?
        current = guild.get_channel(channel.id)
        if current is None:
            return False

        # Reste-t-il quelqu'un à qui jouer le son ?
        return any(not member.bot for member in current.members)

    # ------------------------------------------------------------------
    # Volume
    # ------------------------------------------------------------------

    async def get_volume(self) -> float:
        """
        Récupère le volume du serveur (0.0 - 2.0), avec mise en cache.

        Returns:
            Le facteur de volume à appliquer à la lecture
        """
        if self._volume is not None:
            return self._volume

        percent = self.FALLBACK_VOLUME * 100
        if self.db is not None:
            try:
                percent = await self.db.get_config(
                    str(self.guild_id), "volume", None
                )
                if percent is None:
                    percent = getattr(Config, "DEFAULT_VOLUME", 70)
            except Exception as e:
                logger.warning(f"Volume illisible en base, valeur de repli utilisée: {e}")
                percent = self.FALLBACK_VOLUME * 100

        self._volume = max(0.0, min(2.0, float(percent) / 100))
        return self._volume

    def set_volume(self, percent: int) -> float:
        """
        Applique un nouveau volume, y compris au son en cours de lecture.

        Args:
            percent: Volume en pourcentage (0-200)

        Returns:
            Le facteur de volume appliqué
        """
        self._volume = max(0.0, min(2.0, float(percent) / 100))

        # Le PCMVolumeTransformer du son en cours est modifiable à chaud
        vc = self.voice_client
        if vc is not None and vc.source is not None:
            try:
                vc.source.volume = self._volume
            except AttributeError:
                pass

        return self._volume

    # ------------------------------------------------------------------
    # Lecture
    # ------------------------------------------------------------------

    async def process_next(self) -> None:
        """
        Traite le prochain élément de la file d'attente.

        Gère la connexion au salon vocal et lance la lecture. Les éléments
        dont le salon n'est plus jouable sont écartés un par un, sans toucher
        au reste de la file.

        Important : la boucle remplace l'ancienne récursion. asyncio.Lock
        n'étant pas réentrant, un appel récursif à process_next() sous le
        verrou provoquait un interblocage définitif du player.
        """
        async with self._lock:
            while True:
                # Une lecture est déjà en cours : le callback after
                # rappellera process_next() à la fin du son.
                # Ce test passe AVANT celui sur la file vide : deux appels
                # concurrents à process_next() sinon, le second remettrait
                # current_sound à None pendant la lecture du premier.
                vc = self.voice_client
                if vc is not None and vc.is_connected() and vc.is_playing():
                    return

                # Plus rien à jouer
                if not self.queue:
                    self.current_sound = None
                    self._start_disconnect_timer()
                    return

                item = self.queue[0]

                # Salon disparu ou vide : on écarte uniquement CET élément
                if not self._is_channel_playable(item.channel):
                    self.queue.popleft()
                    logger.info(
                        f"⏭️ '{item.sound_name}' ignoré: "
                        f"salon '{item.channel.name}' vide ou supprimé"
                    )
                    continue

                connected = await self.join(item.channel)

                # L'élément est consommé dans tous les cas, pour éviter
                # de boucler indéfiniment sur un salon injoignable.
                self.queue.popleft()

                if not connected:
                    continue

                self.current_sound = (item.sound_name, item.requester_name)
                logger.info(f"▶️ Lecture de '{item.sound_name}' dans {item.channel.name}")

                try:
                    source = discord.FFmpegPCMAudio(
                        item.source_path,
                        **self.FFMPEG_OPTIONS
                    )
                    # Appliquer un transformateur de volume pour éviter la saturation
                    source = discord.PCMVolumeTransformer(source, volume=await self.get_volume())

                    self.voice_client.play(source, after=self._after_play)
                except Exception as e:
                    logger.error(f"Erreur lors du démarrage de la lecture: {e}")
                    self.current_sound = None
                    # On enchaîne dans la boucle plutôt que d'appeler
                    # _after_play(), qui replanifierait process_next().
                    continue

                return

    def _after_play(self, error: Optional[Exception]) -> None:
        """
        Callback appelé par discord.py à la fin de la lecture.

        Attention : cette méthode s'exécute dans le thread audio, pas dans la
        boucle asyncio. Elle ne doit jamais attendre le résultat de la
        coroutine planifiée, sous peine de bloquer le thread audio (c'était
        l'origine du "Timeout lors du passage au son suivant").

        Args:
            error: Exception éventuelle survenue pendant la lecture
        """
        if error:
            logger.error(f"Erreur pendant la lecture: {error}")

        self.current_sound = None

        try:
            asyncio.run_coroutine_threadsafe(self.process_next(), self.bot.loop)
        except Exception as e:
            logger.error(f"Impossible de planifier le son suivant: {e}")

    def stop(self) -> None:
        """
        Arrête la lecture en cours et vide toute la file d'attente.

        À réserver aux actions explicites de l'utilisateur (commande /stop).
        Pour quitter un salon en gardant les sons destinés aux autres salons,
        utiliser leave() ou purge_channel().
        """
        self.queue.clear()

        vc = self.voice_client
        if vc is not None and vc.is_playing():
            vc.stop()
            logger.info(f"Lecture arrêtée (guild={self.guild_id})")

        self.current_sound = None
        self._start_disconnect_timer()

    def skip(self) -> bool:
        """
        Passe au son suivant dans la file d'attente.

        Returns:
            True si un son a été passé
        """
        vc = self.voice_client
        if vc is not None and vc.is_playing():
            vc.stop()  # Déclenche _after_play automatiquement
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
        vc = self.voice_client
        connected = bool(vc and vc.is_connected())

        return {
            'is_playing': bool(connected and vc.is_playing()),
            'is_connected': connected,
            'current_sound': self.current_sound,
            'queue_length': len(self.queue),
            'queue': [
                {
                    'name': item.sound_name,
                    'requester': item.requester_name,
                    'channel': item.channel.name
                }
                for item in self.queue
            ]
        }


class PlayerManager:
    """
    Gestionnaire central des players audio.

    Maintient un player par serveur et gère leur cycle de vie.
    Les players sont indexés par ID de serveur (int).

    Attributes:
        bot: Instance du bot Discord
        voice_timeout: Délai de déconnexion automatique
        players: Dictionnaire des players par ID de serveur
    """

    def __init__(self, bot, voice_timeout: int, db=None):
        """
        Initialise le gestionnaire de players.

        Args:
            bot: Instance du bot Discord
            voice_timeout: Délai en secondes avant déconnexion automatique
            db: Gestionnaire de base de données (pour le volume par serveur)
        """
        self.bot = bot
        self.voice_timeout = voice_timeout
        self.db = db
        self.players: Dict[int, GuildPlayer] = {}

    def get_player(self, guild_id: int) -> GuildPlayer:
        """
        Récupère ou crée un player pour un serveur.

        Args:
            guild_id: ID du serveur Discord

        Returns:
            Instance de GuildPlayer pour le serveur
        """
        guild_id = int(guild_id)

        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer(
                guild_id,
                self.bot,
                self.voice_timeout,
                self.db
            )
            logger.debug(f"Nouveau player créé pour guild={guild_id}")

        return self.players[guild_id]

    def find_player(self, guild_id) -> Optional[GuildPlayer]:
        """
        Récupère le player d'un serveur sans en créer un.

        Accepte un ID sous forme d'int ou de str : c'est la confusion entre
        les deux qui empêchait le nettoyage du player lors des déconnexions.

        Args:
            guild_id: ID du serveur Discord

        Returns:
            Le player existant, ou None
        """
        try:
            return self.players.get(int(guild_id))
        except (TypeError, ValueError):
            return None

    async def disconnect_all(self) -> None:
        """Déconnecte tous les players (utile pour le shutdown)."""
        for guild_id, player in list(self.players.items()):
            try:
                player.queue.clear()
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