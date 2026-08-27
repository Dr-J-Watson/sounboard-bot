"""
Bot Discord Soundboard - Module Principal.

Ce bot permet de gérer un soundboard sur Discord avec les fonctionnalités suivantes :
- Lecture de sons dans les salons vocaux
- Gestion des sons par serveur et sons globaux
- Routines automatisées (timers, événements vocaux)
- Configuration personnalisée par serveur
- Interface d'administration

Auteur: Soundboard Bot
"""

import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import logging
import os
import re
import sys
from typing import Optional, List

# Import des modules locaux
from config import Config
from database import DatabaseManager
from audio_manager import AudioManager
from player import PlayerManager
from routine_manager import (
    RoutineManager,
    format_duration,
    parse_duration_seconds,
    parse_duration_range,
    WEEKDAYS,
)

# === Configuration du logging ===
logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL, logging.INFO),
    format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("SoundboardBot")

# Réduire le bruit des bibliothèques externes
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)

# === Validation de la configuration ===
try:
    Config.validate()
    logger.info("✅ Configuration validée")
except ValueError as e:
    logger.critical(f"❌ Erreur de configuration: {e}")
    sys.exit(1)

# === Initialisation des composants ===
db = DatabaseManager(Config.DB_FILE)
audio_manager = AudioManager(db)

# === Configuration des intents Discord ===
intents = discord.Intents.default()
intents.voice_states = True  # Requis pour les routines vocales
intents.guilds = True        # Requis pour la gestion des serveurs

# Intent privilégié : sans lui, le cache des membres n'est alimenté que par
# les événements vocaux, ce qui rend guild.get_member() et les conditions
# role= peu fiables. Doit être activé dans le portail développeur Discord.
if Config.MEMBERS_INTENT:
    intents.members = True

# Sans cet intent, message.content est vide : le déclencheur "on message"
# ne peut rien détecter. Désactivé par défaut car privilégié lui aussi.
if Config.MESSAGE_CONTENT_INTENT:
    intents.message_content = True


class SoundboardBot(commands.Bot):
    """
    Bot principal du Soundboard.
    
    Hérite de commands.Bot et ajoute la gestion des composants
    spécifiques au soundboard (player, routines).
    
    Attributes:
        player_manager: Gestionnaire de lecture audio
        routine_manager: Gestionnaire des routines automatisées
    """
    
    def __init__(self):
        """Initialise le bot avec les intents et les gestionnaires."""
        super().__init__(command_prefix="!", intents=intents)
        self.player_manager = PlayerManager(self, Config.VOICE_TIMEOUT_SECONDS, db)
        self.routine_manager = RoutineManager(self, db)

    async def setup_hook(self) -> None:
        """
        Hook de configuration appelé avant la connexion.
        
        Initialise la base de données et synchronise les sons avec le système de fichiers.
        Note: Les routines sont chargées dans on_ready() car les guilds ne sont pas encore disponibles ici.
        """
        # Initialiser la base de données
        await db.init_db()
        logger.info("📦 Base de données initialisée")
        
        # Synchroniser les sons globaux
        global_path = os.path.join(Config.SOUNDS_DIR, "global")
        if os.path.exists(global_path):
            count = await db.sync_with_folder("global", global_path)
            if count > 0:
                logger.info(f"🔄 {count} son(s) global(aux) synchronisé(s)")
        else:
            os.makedirs(global_path, exist_ok=True)
            logger.info("📁 Dossier global créé")

        # Synchroniser les sons de chaque serveur
        if os.path.exists(Config.SOUNDS_DIR):
            for guild_id in os.listdir(Config.SOUNDS_DIR):
                if guild_id == "global":
                    continue
                guild_path = os.path.join(Config.SOUNDS_DIR, guild_id)
                if os.path.isdir(guild_path):
                    count = await db.sync_with_folder(guild_id, guild_path)
                    if count > 0:
                        logger.info(f"🔄 {count} son(s) synchronisé(s) pour {guild_id}")
        
        # Synchroniser les commandes slash
        await self.tree.sync()
        logger.info("⚡ Commandes slash synchronisées")

    async def on_ready(self) -> None:
        """Appelé quand le bot est prêt et connecté."""
        logger.info(f"🤖 Connecté en tant que {self.user} (ID: {self.user.id})")
        logger.info(f"📊 {len(self.guilds)} serveur(s) | {len(self.users)} utilisateur(s)")
        
        # Charger les routines maintenant que les guilds sont disponibles
        await self.routine_manager.load_routines()
        
        # Charger Opus pour l'audio
        if not discord.opus.is_loaded():
            try:
                discord.opus.load_opus('libopus.so.0')
                logger.info("🔊 Opus chargé avec succès")
            except Exception as e:
                logger.warning(f"⚠️ Impossible de charger Opus: {e}")
        
        # Définir le statut
        activity = discord.Activity(
            type=discord.ActivityType.listening,
            name="/play | /help"
        )
        await self.change_presence(activity=activity)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ) -> None:
        """
        Gère les changements d'état vocal.
        
        Transmet les événements au gestionnaire de routines.
        Détecte aussi quand le bot se retrouve seul dans un salon,
        et quand le bot lui-même est déconnecté ou déplacé.
        """
        if member.id == self.user.id:
            # Le bot a été déplacé ou déconnecté (manuellement ou non)
            await self._handle_bot_voice_change(member, before, after)
        else:
            # Vérifier si le bot se retrouve seul dans un salon
            await self._check_bot_alone(member, before)
        
        # Transmettre aux routines
        await self.routine_manager.on_voice_state_update(member, before, after)

    async def _handle_bot_voice_change(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ) -> None:
        """
        Réagit à un changement d'état vocal du bot lui-même.
        
        Déconnexion manuelle : on purge uniquement les sons destinés au salon
        quitté (intention explicite d'un utilisateur), on resynchronise le
        player puis on relance la file pour les autres salons.
        
        Déplacement manuel : on se contente de resynchroniser le client vocal.
        """
        if before.channel is None:
            return
        
        player = self.player_manager.find_player(member.guild.id)
        if player is None:
            return
        
        if after.channel is None:
            logger.info(f"👋 Bot déconnecté de {before.channel.name}")
            
            player.purge_channel(before.channel.id)
            player.current_sound = None
            player.voice_client = None
            
            # Les sons destinés aux autres salons doivent continuer
            asyncio.create_task(player.process_next())
        
        elif after.channel.id != before.channel.id:
            logger.info(f"↔️ Bot déplacé vers {after.channel.name}")
            player.voice_client = member.guild.voice_client

    async def _check_bot_alone(
        self,
        member: discord.Member,
        before: discord.VoiceState
    ) -> None:
        """
        Vérifie si le bot se retrouve seul dans un salon après un départ.
        Si oui, arrête la lecture et quitte le salon.
        """
        # On ne s'intéresse qu'aux départs de salon
        if before.channel is None:
            return
        
        # Ne pas réagir si c'est le bot qui part
        if member.id == self.user.id:
            return
        
        # Vérifier si le bot est dans ce salon
        voice_client = member.guild.voice_client
        if not voice_client or voice_client.channel != before.channel:
            return
        
        # Compter les membres humains restants (excluant les bots)
        human_members = [m for m in before.channel.members if not m.bot]
        
        if len(human_members) == 0:
            logger.info(f"🚶 Bot seul dans {before.channel.name}, arrêt et déconnexion")
            
            # La file d'attente est conservée : elle peut contenir des sons
            # destinés à d'autres salons. Ceux visant un salon resté vide
            # seront écartés au moment de les jouer par process_next().
            player = self.player_manager.find_player(member.guild.id)
            if player is not None:
                await player.leave()
            else:
                await voice_client.disconnect(force=True)

    async def on_message(self, message: discord.Message) -> None:
        """Transmet les messages aux routines (déclencheur par mot-clé)."""
        if message.author.bot or message.guild is None:
            return
        
        await self.routine_manager.on_message(message)
        await self.process_commands(message)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """
        Transmet les réactions aux routines.
        
        La variante « raw » est utilisée pour capter aussi les réactions sur
        des messages absents du cache (redémarrage, anciens messages).
        """
        if payload.guild_id is None:
            return
        
        guild = self.get_guild(payload.guild_id)
        if guild is None:
            return
        
        member = payload.member or guild.get_member(payload.user_id)
        if member is None or member.bot:
            return
        
        channel = guild.get_channel(payload.channel_id)
        await self.routine_manager.on_reaction(str(payload.emoji), member, channel)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """
        Appelé quand le bot est retiré d'un serveur.
        
        Nettoie le player et toutes les données du serveur en base, pour ne
        pas laisser de sons, routines et salons ignorés orphelins.
        Les fichiers audio du serveur sont conservés sur le disque.
        """
        logger.info(f"👋 Bot retiré du serveur {guild.name} ({guild.id})")
        
        player = self.player_manager.find_player(guild.id)
        if player is not None:
            player.queue.clear()
            await player.disconnect()
            self.player_manager.players.pop(guild.id, None)
        
        try:
            await db.delete_guild_data(str(guild.id))
        except Exception as e:
            logger.error(f"Erreur lors du nettoyage des données de {guild.id}: {e}")
        
        await self.routine_manager.load_routines()

    async def close(self) -> None:
        """Nettoyage lors de l'arrêt du bot."""
        logger.info("🛑 Arrêt du bot...")
        
        # Arrêter les routines
        await self.routine_manager.stop()
        
        # Déconnecter tous les players
        await self.player_manager.disconnect_all()
        
        # Fermer la connexion à la base
        await db.close()
        
        await super().close()


def describe_action(action: dict) -> str:
    """
    Décrit une action de routine en une ligne lisible.

    Args:
        action: Dictionnaire d'action

    Returns:
        Description courte de l'action
    """
    a_type = action.get('type')

    if a_type == 'play_sound':
        name = action.get('sound_name')
        return "🎲 son aléatoire" if name == '__random__' else f"🎵 {name}"
    if a_type == 'wait':
        if action.get('delay_min') is not None:
            return (f"💤 pause {format_duration(action['delay_min'])}"
                    f"-{format_duration(action['delay_max'])}")
        return f"💤 pause {format_duration(action.get('delay', 0))}"
    if a_type == 'message':
        return "💬 message"
    if a_type == 'dm':
        return "📩 message privé"
    if a_type == 'chance':
        return f"🎲 chance {action.get('percent')}%"
    if a_type == 'volume':
        value = action.get('value')
        return "🔊 volume reset" if value == 'reset' else f"🔊 volume {value}%"
    if a_type == 'move':
        return "↔️ déplacer " + ("le membre" if action.get('target') == 'member' else "le bot")
    if a_type == 'player_control':
        labels = {'stop': '⏹️ stop', 'skip': '⏭️ skip',
                  'clear': '🧹 vider la file', 'leave': '🚪 quitter le salon'}
        return labels.get(action.get('command'), 'contrôle')
    return str(a_type)


def describe_trigger(trigger_type: str, trigger_data: dict) -> str:
    """
    Décrit un déclencheur de routine en une ligne lisible.

    Args:
        trigger_type: Type du déclencheur (timer, schedule, event)
        trigger_data: Données associées

    Returns:
        Description courte, préfixée d'un émoji
    """
    if trigger_type == 'timer':
        seconds = trigger_data.get('interval_seconds', 0)
        if not seconds:
            seconds = trigger_data.get('interval_minutes', 0) * 60
        return f"⏰ Timer ({format_duration(seconds)})"

    if trigger_type == 'schedule':
        days = trigger_data.get('days') or []
        day_names = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
        when = ",".join(day_names[d] for d in days) if days else "tous les jours"
        return f"🕐 À {trigger_data.get('time')} ({when})"

    event = trigger_data.get('event', '?')
    if event == 'voice_count_reached':
        return f"👥 {trigger_data.get('count', '?')} membres dans le salon"
    if event == 'voice_first_join':
        return "🥇 Premier arrivé dans un salon vide"
    if event == 'message':
        return f"💬 Message contenant « {trigger_data.get('keyword', '')} »"
    if event == 'reaction':
        return f"⭐ Réaction {trigger_data.get('emoji', '')}"
    return f"⚡ {event.replace('voice_', '')}"


# === Instance du bot ===
bot = SoundboardBot()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
) -> None:
    """
    Gestionnaire global d'erreurs pour les commandes slash.
    
    Sans lui, une exception non rattrapée dans une commande se traduit par
    « L'application n'a pas répondu » côté utilisateur, sans trace exploitable
    côté serveur.
    """
    command_name = interaction.command.name if interaction.command else "inconnue"
    
    # Erreurs attendues : message clair, pas de stacktrace
    if isinstance(error, app_commands.CommandOnCooldown):
        message = f"⏳ Trop rapide, réessayez dans {error.retry_after:.0f}s."
        logger.debug(f"Cooldown sur /{command_name}")
    elif isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
        message = "🚫 Vous n'avez pas les permissions nécessaires."
        logger.debug(f"Permission refusée sur /{command_name} pour {interaction.user}")
    else:
        message = (
            "❌ Une erreur est survenue lors de l'exécution de la commande. "
            "L'incident a été enregistré."
        )
        logger.error(
            f"Erreur non gérée dans /{command_name} "
            f"(user={interaction.user}, guild={interaction.guild_id}): {error}",
            exc_info=error
        )
    
    # La réponse peut déjà avoir été envoyée ou différée
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass  # L'interaction a expiré, rien de plus à faire



# =============================================================================
# COMMANDES GÉNÉRALES
# =============================================================================

@bot.tree.command(name="help", description="Affiche la liste des commandes et l'aide pour les routines.")
async def help_command(interaction: discord.Interaction) -> None:
    """Affiche l'aide complète du bot."""
    embed = discord.Embed(
        title="📖 Aide du Soundboard",
        color=discord.Color.gold(),
        description="Bienvenue ! Voici toutes les commandes disponibles."
    )
    
    # Commandes Sons
    embed.add_field(
        name="🎵 Sons",
        value=(
            "`/play <nom>` : Joue un son\n"
            "`/stop` : Arrête la lecture et vide la file\n"
            "`/skip` : Passe au son suivant\n"
            "`/queue` : Affiche la file d'attente\n"
            "`/clear [salon]` : Vide la file sans couper le son en cours\n"
            "`/list_sounds` : Liste les sons disponibles\n"
            "`/stats` : Classement des sons les plus joués\n"
            "`/add_sound <fichier> [nom]` : Ajoute un son"
        ),
        inline=False
    )
    
    # Commandes Admin
    embed.add_field(
        name="⚙️ Administration",
        value=(
            "`/config <setting> <value>` : Configure les limites\n"
            "`/volume [0-200]` : Règle le volume de lecture\n"
            "`/delete_sound <nom>` : Supprime un son\n"
            "`/sync` : Synchronise les fichiers du disque\n"
            "`/cleanup` : Nettoie les fichiers et entrées orphelins"
        ),
        inline=False
    )
    
    # Commandes Routines
    embed.add_field(
        name="🤖 Routines (Automatisations)",
        value=(
            "`/routine_list` : Voir les routines actives\n"
            "`/routine_create` : Créer avec l'assistant\n"
            "`/routine_toggle <id>` : Activer/Désactiver\n"
            "`/routine_delete <id>` : Supprimer\n"
            "`/routine_cmd <nom> <commande>` : Créer via commande"
        ),
        inline=False
    )
    
    # Syntaxe Routine — déclencheurs
    embed.add_field(
        name="📝 Routines : déclencheurs",
        value=(
            "**Syntaxe :** `<trigger> [if <conditions>] do <actions>`\n"
            "• `timer 30s` / `5m` / `1h30m` — à intervalle régulier\n"
            "• `at 18:00` / `at lun,ven 09:30` — à heure fixe\n"
            "• `on join` / `leave` / `move` / `first_join`\n"
            "• `on mute` / `unmute` / `deafen` / `undeafen`\n"
            "• `on stream` / `stream_stop` / `video` / `video_stop`\n"
            "• `on count>=3` — le salon atteint 3 membres\n"
            "• `on message <mot-clé>` • `on reaction <émoji>`"
        ),
        inline=False
    )
    
    # Syntaxe Routine — conditions et actions
    embed.add_field(
        name="📝 Routines : conditions et actions",
        value=(
            "**Conditions** *(optionnel, séparées par `and`)* :\n"
            "• `user=ID` • `channel=ID` • `role=ID` *(listes: `user=1,2`)*\n"
            "• `time=18:00-23:00` • `date=01/12-25/12` • `day=lun,ven`\n"
            "• `count>=3` • `chance=30` • `playing=false`\n\n"
            "**Actions** *(séparées par `then`)* :\n"
            "• `play <son>` • `wait 1m20s` • `wait 1m20s-2h` *(aléatoire)*\n"
            "• `chance 25%` — interrompt la suite si le tirage échoue\n"
            "• `stop` • `skip` • `clear` • `leave` *(quitte le vocal)*\n"
            "• `volume 150` • `volume reset`\n"
            "• `move <id_salon>` • `move member <id_salon>`\n"
            "• `msg <texte>` • `dm <texte>`"
        ),
        inline=False
    )
    
    embed.add_field(
        name="💡 Exemples",
        value=(
            "`at 18:00 do play apero`\n"
            "`on first_join do play intro`\n"
            "`on count>=4 do play foule`\n"
            "`on join if chance=25 and playing=false do play rare`\n"
            "`on join do wait 10s-2m then play surprise`\n"
            "`on leave do stop then leave`"
        ),
        inline=False
    )
    
    embed.set_footer(text="💡 Utilisez /routine_create pour un assistant interactif !")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="list_sounds", description="Liste tous les sons disponibles.")
async def list_sounds(interaction: discord.Interaction) -> None:
    """Liste tous les sons disponibles pour le serveur."""
    if not interaction.guild_id:
        await interaction.response.send_message(
            "❌ Cette commande ne peut être utilisée que sur un serveur.",
            ephemeral=True
        )
        return

    sounds = await db.get_available_sounds(str(interaction.guild_id))
    
    if not sounds:
        await interaction.response.send_message(
            "📭 Aucun son disponible.\nUtilisez `/add_sound` pour en ajouter !",
            ephemeral=True
        )
        return
    
    # Trier et formater la liste
    sound_list = sorted(sounds.keys())
    
    # Créer un embed avec pagination si nécessaire
    embed = discord.Embed(
        title="🎵 Sons disponibles",
        color=discord.Color.blue(),
        description=f"**{len(sound_list)}** son(s) disponible(s)"
    )
    
    # Grouper les sons par chunks pour l'affichage
    chunk_size = 20
    for i in range(0, len(sound_list), chunk_size):
        chunk = sound_list[i:i + chunk_size]
        chunk_text = ", ".join([f"`{s}`" for s in chunk])
        field_name = f"Sons {i+1}-{min(i+chunk_size, len(sound_list))}"
        embed.add_field(name=field_name, value=chunk_text, inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def sound_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> List[app_commands.Choice[str]]:
    """Autocomplétion pour les noms de sons."""
    if not interaction.guild_id:
        return []
    
    sounds = await db.get_available_sounds(str(interaction.guild_id))
    
    # Filtrer et limiter les résultats
    filtered = [
        app_commands.Choice(name=sound, value=sound)
        for sound in sorted(sounds.keys())
        if current.lower() in sound.lower()
    ]
    
    return filtered[:25]


@bot.tree.command(name="play", description="Joue un son dans un salon vocal.")
@app_commands.describe(
    sound_name="Le nom du son à jouer (optionnel - affiche une liste si non spécifié)",
    channel="Le salon vocal où jouer le son (optionnel)"
)
@app_commands.autocomplete(sound_name=sound_autocomplete)
async def play(
    interaction: discord.Interaction,
    sound_name: Optional[str] = None,
    channel: Optional[discord.VoiceChannel] = None
) -> None:
    """Joue un son dans le salon vocal."""
    if not interaction.guild_id:
        await interaction.response.send_message(
            "❌ Cette commande ne peut être utilisée que sur un serveur.",
            ephemeral=True
        )
        return

    # Déterminer le salon cible
    target_channel = channel
    if not target_channel:
        if interaction.user.voice:
            target_channel = interaction.user.voice.channel
        else:
            await interaction.response.send_message(
                "❌ Vous devez être dans un salon vocal ou spécifier un salon.",
                ephemeral=True
            )
            return

    # Vérifier que le bot peut réellement jouer dans ce salon
    permissions = target_channel.permissions_for(interaction.guild.me)
    if not permissions.connect or not permissions.speak:
        await interaction.response.send_message(
            f"🚫 Je n'ai pas la permission de me connecter ou de parler dans "
            f"{target_channel.mention}.",
            ephemeral=True
        )
        return

    # Vérifier si le salon est ignoré
    if await db.is_channel_ignored(str(interaction.guild_id), str(target_channel.id)):
        await interaction.response.send_message(
            f"🔇 Le salon **{target_channel.name}** est ignoré.\n"
            "Utilisez `/ignored` pour voir la liste des salons ignorés.",
            ephemeral=True
        )
        return

    # Si aucun son spécifié, afficher le sélecteur
    if not sound_name:
        view = SoundSelectorView(bot, db, interaction.guild_id, target_channel, interaction.user)
        await view.initialize()
        
        if not view.all_sounds:
            await interaction.response.send_message(
                "❌ Aucun son disponible sur ce serveur.",
                ephemeral=True
            )
            return
        
        embed = view.build_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        return

    # Rechercher le son (local d'abord, puis global)
    sound_data = await db.get_sound(str(interaction.guild_id), sound_name)
    if not sound_data:
        sound_data = await db.get_sound("global", sound_name)

    if not sound_data:
        await interaction.response.send_message(
            f"❌ Le son `{sound_name}` n'existe pas.",
            ephemeral=True
        )
        return

    # Vérifier le fichier
    file_path = Config.get_sound_path(sound_data['guild_id'], sound_data['filename'])
    
    if not os.path.exists(file_path):
        await interaction.response.send_message(
            f"❌ Fichier introuvable pour `{sound_name}`.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    
    # Ajouter à la queue
    player = bot.player_manager.get_player(interaction.guild_id)
    position = player.add_to_queue(
        file_path,
        interaction.user.display_name,
        sound_name,
        target_channel
    )
    
    # Incrémenter le compteur de lecture
    await db.increment_play_count(sound_data['guild_id'], sound_name)
    
    # Message de confirmation : position == 1 signifie "premier de la file",
    # pas "en cours de lecture" (un autre son peut déjà être joué).
    info = player.get_queue_info()
    if position == 1 and not info['is_playing']:
        msg = f"🎵 **{sound_name}** en lecture dans {target_channel.mention}"
    else:
        msg = f"🎵 **{sound_name}** ajouté à la file (position {position}) dans {target_channel.mention}"
    
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="stop", description="Arrête la lecture et vide la file d'attente.")
async def stop(interaction: discord.Interaction) -> None:
    """Arrête la lecture en cours."""
    if not interaction.guild_id:
        return
    
    player = bot.player_manager.get_player(interaction.guild_id)
    player.stop()
    
    await interaction.response.send_message("⏹️ Lecture arrêtée.", ephemeral=True)


@bot.tree.command(name="skip", description="Passe au son suivant dans la file d'attente.")
async def skip(interaction: discord.Interaction) -> None:
    """Passe au son suivant."""
    if not interaction.guild_id:
        return
    
    player = bot.player_manager.get_player(interaction.guild_id)
    
    if player.skip():
        await interaction.response.send_message("⏭️ Son suivant.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "❌ Aucun son en cours de lecture.",
            ephemeral=True
        )


@bot.tree.command(name="queue", description="Affiche la file d'attente actuelle.")
async def queue(interaction: discord.Interaction) -> None:
    """Affiche la file d'attente."""
    if not interaction.guild_id:
        return
    
    player = bot.player_manager.get_player(interaction.guild_id)
    info = player.get_queue_info()
    
    embed = discord.Embed(title="📋 File d'attente", color=discord.Color.blue())
    
    if info['current_sound']:
        embed.add_field(
            name="▶️ En cours",
            value=f"`{info['current_sound'][0]}` (par {info['current_sound'][1]})",
            inline=False
        )
    else:
        embed.add_field(name="▶️ En cours", value="*Rien*", inline=False)
    
    if info['queue']:
        queue_text = "\n".join([
            f"{i+1}. `{item['name']}` → #{item['channel']} (par {item['requester']})"
            for i, item in enumerate(info['queue'][:10])
        ])
        if len(info['queue']) > 10:
            queue_text += f"\n... et {len(info['queue']) - 10} autre(s)"
        embed.add_field(name="📝 En attente", value=queue_text, inline=False)
    else:
        embed.add_field(name="📝 En attente", value="*File vide*", inline=False)
    
    embed.set_footer(text=f"Connecté: {'Oui' if info['is_connected'] else 'Non'}")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="clear", description="Vide la file d'attente sans couper le son en cours.")
@app_commands.describe(
    salon="Ne retirer que les sons destinés à ce salon (toute la file par défaut)."
)
async def clear(
    interaction: discord.Interaction,
    salon: Optional[discord.VoiceChannel] = None
) -> None:
    """
    Vide la file d'attente.
    
    Contrairement à /stop, le son en cours n'est pas interrompu.
    L'option `salon` permet de ne retirer que les sons visant un salon
    précis, les autres restant en attente.
    """
    if not interaction.guild_id:
        return
    
    player = bot.player_manager.get_player(interaction.guild_id)
    
    if salon is not None:
        removed = player.purge_channel(salon.id)
        cible = f" destiné(s) à {salon.mention}"
    else:
        removed = player.clear_queue()
        cible = ""
    
    if removed == 0:
        message = f"ℹ️ Aucun son en attente{cible}."
    else:
        message = f"🧹 {removed} son(s){cible} retiré(s) de la file."
        if player.current_sound:
            message += f"\n▶️ `{player.current_sound[0]}` continue (utilise `/skip` ou `/stop`)."
    
    await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="add_sound", description="Ajoute un nouveau son au serveur.")
@app_commands.describe(
    attachment="Le fichier audio à ajouter",
    name="Nom personnalisé pour le son (optionnel)"
)
async def add_sound(
    interaction: discord.Interaction,
    attachment: discord.Attachment,
    name: Optional[str] = None
) -> None:
    """Ajoute un son au soundboard du serveur."""
    if not interaction.guild_id:
        await interaction.response.send_message(
            "❌ Cette commande ne peut être utilisée que sur un serveur.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    
    # Générer le nom si non fourni
    if not name:
        name = os.path.splitext(attachment.filename)[0]
    
    # Normaliser le nom
    name = name.lower().replace(" ", "_").strip()
    
    # Vérifier la longueur du nom
    max_name_length = await db.get_config(
        str(interaction.guild_id),
        "max_name_length",
        Config.MAX_NAME_LENGTH
    )
    
    if max_name_length > 0 and len(name) > max_name_length:
        await interaction.followup.send(
            f"❌ Le nom est trop long ({len(name)} caractères). "
            f"Maximum: {max_name_length} caractères.",
            ephemeral=True
        )
        return

    # Vérifier si le son existe déjà
    if await db.get_sound(str(interaction.guild_id), name):
        await interaction.followup.send(
            f"❌ Le son `{name}` existe déjà sur ce serveur.",
            ephemeral=True
        )
        return

    try:
        # Sauvegarder et valider le fichier
        saved_path = await audio_manager.save_upload(
            attachment,
            attachment.filename,
            str(interaction.guild_id)
        )
        filename = os.path.basename(saved_path)
        
        # Ajouter à la base de données
        await db.add_sound(
            str(interaction.guild_id),
            name,
            filename,
            str(interaction.user)
        )
        
        await interaction.followup.send(
            f"✅ Son `{name}` ajouté avec succès !",
            ephemeral=True
        )
        
    except ValueError as e:
        await interaction.followup.send(f"❌ {e}", ephemeral=True)
    except Exception as e:
        logger.error(f"Erreur lors de l'ajout du son: {e}", exc_info=True)
        await interaction.followup.send(
            f"❌ Erreur inattendue: {e}",
            ephemeral=True
        )


@bot.tree.command(name="delete_sound", description="Supprime un son (Admin uniquement).")
@app_commands.describe(sound_name="Le nom du son à supprimer")
@app_commands.autocomplete(sound_name=sound_autocomplete)
async def delete_sound(interaction: discord.Interaction, sound_name: str) -> None:
    """Supprime un son du soundboard."""
    if not interaction.guild_id:
        await interaction.response.send_message(
            "❌ Cette commande ne peut être utilisée que sur un serveur.",
            ephemeral=True
        )
        return

    # Vérifier les permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "🚫 Vous devez être administrateur pour supprimer un son.",
            ephemeral=True
        )
        return

    # Vérifier que le son existe
    sound_data = await db.get_sound(str(interaction.guild_id), sound_name)
    if not sound_data:
        await interaction.response.send_message(
            f"❌ Le son `{sound_name}` n'existe pas.",
            ephemeral=True
        )
        return

    # Supprimer le fichier
    await audio_manager.delete_sound_file(
        str(interaction.guild_id),
        sound_data['filename']
    )
    
    # Supprimer de la base de données
    await db.remove_sound(str(interaction.guild_id), sound_name)
    
    await interaction.response.send_message(
        f"✅ Le son `{sound_name}` a été supprimé.",
        ephemeral=True
    )


@bot.tree.command(name="rename_sound", description="Renomme un son (Admin uniquement).")
async def rename_sound(interaction: discord.Interaction) -> None:
    """Renomme un son du soundboard via un sélecteur interactif."""
    if not interaction.guild_id:
        await interaction.response.send_message(
            "❌ Cette commande ne peut être utilisée que sur un serveur.",
            ephemeral=True
        )
        return

    # Vérifier les permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "🚫 Vous devez être administrateur pour renommer un son.",
            ephemeral=True
        )
        return

    # Créer et initialiser la vue
    view = RenameSoundView(bot, db, interaction.guild_id, interaction.user)
    await view.initialize()
    
    if not view.all_sounds:
        await interaction.response.send_message(
            "❌ Aucun son disponible sur ce serveur.",
            ephemeral=True
        )
        return
    
    embed = view.build_embed()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="config", description="Configure les paramètres du bot (Admin uniquement).")
@app_commands.describe(
    setting="Le paramètre à modifier",
    value="La nouvelle valeur (0 = illimité)"
)
@app_commands.choices(setting=[
    app_commands.Choice(name="Durée max (secondes)", value="max_duration"),
    app_commands.Choice(name="Taille max (Mo)", value="max_file_size_mb"),
    app_commands.Choice(name="Longueur nom max", value="max_name_length")
])
async def config(
    interaction: discord.Interaction,
    setting: str,
    value: int
) -> None:
    """Configure les paramètres du bot pour le serveur."""
    if not interaction.guild_id:
        await interaction.response.send_message(
            "❌ Commande serveur uniquement.",
            ephemeral=True
        )
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "🚫 Vous devez être administrateur pour modifier la configuration.",
            ephemeral=True
        )
        return

    if value < 0:
        await interaction.response.send_message(
            "🚫 La valeur doit être positive ou nulle (0 = illimité).",
            ephemeral=True
        )
        return

    await db.set_config(str(interaction.guild_id), setting, value)
    
    # Message de confirmation
    setting_names = {
        "max_duration": "Durée maximale",
        "max_file_size_mb": "Taille maximale",
        "max_name_length": "Longueur max du nom"
    }
    setting_display = setting_names.get(setting, setting)
    
    if value == 0:
        await interaction.response.send_message(
            f"✅ Configuration mise à jour : `{setting_display}` = `Illimité`",
            ephemeral=True
        )
    else:
        unit = "s" if setting == "max_duration" else ("Mo" if setting == "max_file_size_mb" else "")
        await interaction.response.send_message(
            f"✅ Configuration mise à jour : `{setting_display}` = `{value}{unit}`",
            ephemeral=True
        )

@bot.tree.command(name="sync", description="Synchronise la base de données avec les fichiers (Admin).")
async def sync(interaction: discord.Interaction) -> None:
    """Synchronise la DB avec les fichiers présents sur le disque."""
    if not interaction.guild_id:
        await interaction.response.send_message(
            "❌ Commande serveur uniquement.",
            ephemeral=True
        )
        return
    
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "🚫 Réservé aux administrateurs.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    
    guild_id = str(interaction.guild_id)
    guild_dir = os.path.join(Config.SOUNDS_DIR, guild_id)
    
    count = await db.sync_with_folder(guild_id, guild_dir)
    
    if count > 0:
        await interaction.followup.send(
            f"✅ Synchronisation terminée : {count} nouveau(x) fichier(s) ajouté(s).",
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            "✅ Synchronisation terminée. Aucun nouveau fichier trouvé.",
            ephemeral=True
        )


# =============================================================================
# COMMANDES STATISTIQUES ET MAINTENANCE
# =============================================================================

@bot.tree.command(name="stats", description="Affiche les statistiques de lecture des sons.")
@app_commands.describe(limite="Nombre de sons à afficher dans le classement (3-25).")
async def stats(interaction: discord.Interaction, limite: Optional[int] = 10) -> None:
    """Affiche le classement des sons les plus joués."""
    if not interaction.guild_id:
        await interaction.response.send_message(
            "❌ Commande serveur uniquement.",
            ephemeral=True
        )
        return

    limite = max(3, min(25, limite or 10))

    await interaction.response.defer(ephemeral=True)
    data = await db.get_stats(str(interaction.guild_id), limite)

    embed = discord.Embed(title="📊 Statistiques du soundboard", color=discord.Color.blurple())
    embed.add_field(name="Sons disponibles", value=str(data['total_sounds']), inline=True)
    embed.add_field(name="Lectures totales", value=str(data['total_plays']), inline=True)

    played = [s for s in data['top'] if s['plays'] > 0]
    if played:
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, sound in enumerate(played):
            rank = medals[i] if i < len(medals) else f"`{i + 1}.`"
            suffix = " 🌍" if sound['global'] else ""
            lines.append(f"{rank} **{sound['name']}**{suffix} — {sound['plays']} lecture(s)")
        embed.add_field(name=f"🏆 Top {len(played)}", value="\n".join(lines), inline=False)
    else:
        embed.add_field(
            name="🏆 Classement",
            value="*Aucun son n'a encore été joué.*",
            inline=False
        )

    embed.set_footer(text="🌍 = son global, partagé entre les serveurs")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="volume", description="Affiche ou modifie le volume de lecture du bot.")
@app_commands.describe(pourcentage="Nouveau volume entre 0 et 200 (Admin). Laisser vide pour consulter.")
async def volume(interaction: discord.Interaction, pourcentage: Optional[int] = None) -> None:
    """Consulte ou règle le volume de lecture du serveur."""
    if not interaction.guild_id:
        await interaction.response.send_message(
            "❌ Commande serveur uniquement.",
            ephemeral=True
        )
        return

    player = bot.player_manager.get_player(interaction.guild_id)

    # Simple consultation
    if pourcentage is None:
        current = await player.get_volume()
        await interaction.response.send_message(
            f"🔊 Volume actuel : **{round(current * 100)}%**",
            ephemeral=True
        )
        return

    # Modification : réservée aux admins
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "🚫 Réservé aux administrateurs pour modifier le volume.",
            ephemeral=True
        )
        return

    if not 0 <= pourcentage <= 200:
        await interaction.response.send_message(
            "❌ Le volume doit être compris entre 0 et 200.",
            ephemeral=True
        )
        return

    await db.set_config(str(interaction.guild_id), "volume", pourcentage)
    applied = player.set_volume(pourcentage)

    note = "\n⚠️ Au-dessus de 100%, le son peut saturer." if pourcentage > 100 else ""
    await interaction.response.send_message(
        f"🔊 Volume réglé sur **{round(applied * 100)}%**.{note}",
        ephemeral=True
    )


@bot.tree.command(name="cleanup", description="Nettoie les fichiers et entrées orphelins (Admin).")
async def cleanup(interaction: discord.Interaction) -> None:
    """
    Réconcilie la base et le disque.
    
    - Entrées en base dont le fichier a disparu : supprimées
    - Fichiers sur le disque absents de la base : signalés (`/sync` les ajoute)
    """
    if not interaction.guild_id:
        await interaction.response.send_message(
            "❌ Commande serveur uniquement.",
            ephemeral=True
        )
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "🚫 Réservé aux administrateurs.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    guild_id = str(interaction.guild_id)
    guild_dir = os.path.join(Config.SOUNDS_DIR, guild_id)

    # Fichiers réellement présents sur le disque
    try:
        on_disk = {
            f for f in os.listdir(guild_dir)
            if os.path.splitext(f)[1].lower() in Config.ALLOWED_EXTENSIONS
        }
    except FileNotFoundError:
        on_disk = set()

    # Entrées en base
    in_db = await db.get_filenames(guild_id)

    # Entrées sans fichier -> suppression
    missing = [filename for filename in in_db if filename not in on_disk]
    removed = await db.remove_sounds_by_filenames(guild_id, missing)

    # Fichiers sans entrée -> simple signalement
    orphan_files = sorted(on_disk - set(in_db))

    embed = discord.Embed(title="🧹 Nettoyage", color=discord.Color.green())

    if removed:
        names = ", ".join(f"`{in_db[f]}`" for f in missing[:15])
        if len(missing) > 15:
            names += f" … (+{len(missing) - 15})"
        embed.add_field(
            name=f"🗑️ {removed} entrée(s) sans fichier supprimée(s)",
            value=names,
            inline=False
        )
    else:
        embed.add_field(
            name="🗑️ Entrées sans fichier",
            value="*Aucune*",
            inline=False
        )

    if orphan_files:
        listing = "\n".join(f"`{f}`" for f in orphan_files[:10])
        if len(orphan_files) > 10:
            listing += f"\n… (+{len(orphan_files) - 10})"
        embed.add_field(
            name=f"📂 {len(orphan_files)} fichier(s) non référencé(s)",
            value=f"{listing}\n\nUtilisez `/sync` pour les ajouter au soundboard.",
            inline=False
        )
    else:
        embed.add_field(name="📂 Fichiers non référencés", value="*Aucun*", inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


# =============================================================================
# COMMANDES SALONS IGNORÉS
# =============================================================================

@bot.tree.command(name="ignore", description="Ajoute ou retire un salon de la liste des salons ignorés (Admin).")
@app_commands.describe(
    channel="Le salon vocal à ignorer/réactiver",
    action="Ajouter ou retirer le salon de la liste"
)
@app_commands.choices(action=[
    app_commands.Choice(name="Ignorer ce salon", value="add"),
    app_commands.Choice(name="Ne plus ignorer ce salon", value="remove")
])
async def ignore_channel(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
    action: str = "add"
) -> None:
    """Gère les salons ignorés par le bot."""
    if not interaction.guild_id:
        await interaction.response.send_message(
            "❌ Commande serveur uniquement.",
            ephemeral=True
        )
        return
    
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "🚫 Réservé aux administrateurs.",
            ephemeral=True
        )
        return
    
    guild_id = str(interaction.guild_id)
    channel_id = str(channel.id)
    
    if action == "add":
        success = await db.add_ignored_channel(
            guild_id, 
            channel_id, 
            str(interaction.user.id)
        )
        if success:
            await interaction.response.send_message(
                f"🔇 Le salon **{channel.name}** est maintenant ignoré.\n"
                "Le bot n'y déclenchera plus de routines et n'y jouera plus de sons automatiques.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ Le salon **{channel.name}** est déjà ignoré.",
                ephemeral=True
            )
    else:
        success = await db.remove_ignored_channel(guild_id, channel_id)
        if success:
            await interaction.response.send_message(
                f"🔊 Le salon **{channel.name}** n'est plus ignoré.\n"
                "Les routines pourront à nouveau s'y déclencher.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ Le salon **{channel.name}** n'était pas dans la liste des salons ignorés.",
                ephemeral=True
            )


@bot.tree.command(name="ignored", description="Affiche la liste des salons ignorés.")
async def ignored_channels(interaction: discord.Interaction) -> None:
    """Affiche les salons ignorés du serveur."""
    if not interaction.guild_id:
        await interaction.response.send_message(
            "❌ Commande serveur uniquement.",
            ephemeral=True
        )
        return
    
    ignored = await db.get_ignored_channels(str(interaction.guild_id))
    
    if not ignored:
        await interaction.response.send_message(
            "📭 Aucun salon n'est ignoré.\n"
            "Utilisez `/ignore` pour ajouter un salon à la liste.",
            ephemeral=True
        )
        return
    
    # Résoudre les noms des salons
    channel_list = []
    for channel_id in ignored:
        channel = interaction.guild.get_channel(int(channel_id))
        if channel:
            channel_list.append(f"🔇 {channel.mention}")
        else:
            channel_list.append(f"🔇 *(Salon supprimé: {channel_id})*")
    
    embed = discord.Embed(
        title="🔇 Salons Ignorés",
        description="\n".join(channel_list),
        color=discord.Color.orange()
    )
    embed.set_footer(text="Utilisez /ignore pour modifier cette liste")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


# =============================================================================
# COMMANDES ROUTINES
# =============================================================================

@bot.tree.command(name="routine_list", description="Liste les routines configurées.")
async def routine_list(interaction: discord.Interaction) -> None:
    """Affiche la liste des routines du serveur."""
    if not interaction.guild_id:
        await interaction.response.send_message(
            "❌ Commande serveur uniquement.",
            ephemeral=True
        )
        return
    
    routines = await db.get_routines(str(interaction.guild_id))
    
    if not routines:
        await interaction.response.send_message(
            "📭 Aucune routine configurée.\n"
            "Utilisez `/routine_create` ou `/routine_cmd` pour en créer une !",
            ephemeral=True
        )
        return
    
    embed = discord.Embed(
        title="🤖 Routines",
        color=discord.Color.purple(),
        description=f"**{len(routines)}** routine(s) configurée(s)"
    )
    
    for r in routines:
        status = "✅" if r['active'] else "❌"
        
        # Description du trigger
        trigger_desc = describe_trigger(r['trigger_type'], r['trigger_data'])
        
        # Nombre d'actions
        actions_count = len(r['actions'])
        
        desc = f"{trigger_desc}\n📋 {actions_count} action(s)"
        
        embed.add_field(
            name=f"{status} {r['name']} (ID: {r['id']})",
            value=desc,
            inline=True
        )
    
    embed.set_footer(text="💡 Utilisez /routine_toggle pour activer/désactiver")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def routine_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> List[app_commands.Choice[int]]:
    """Autocomplétion pour les routines."""
    if not interaction.guild_id:
        return []
    
    routines = await db.get_routines(str(interaction.guild_id))
    choices = []
    
    for r in routines:
        display = f"{r['name']} ({'ON' if r['active'] else 'OFF'})"
        if current.lower() in display.lower() or current in str(r['id']):
            choices.append(app_commands.Choice(name=display, value=r['id']))
    
    return choices[:25]


@bot.tree.command(name="routine_delete", description="Supprime une routine.")
@app_commands.describe(routine_id="La routine à supprimer")
@app_commands.autocomplete(routine_id=routine_autocomplete)
async def routine_delete(interaction: discord.Interaction, routine_id: int) -> None:
    """Supprime une routine."""
    if not interaction.guild_id:
        return
        
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "🚫 Réservé aux administrateurs.",
            ephemeral=True
        )
        return

    deleted = await db.delete_routine(routine_id, str(interaction.guild_id))
    
    if deleted:
        await bot.routine_manager.load_routines()
        await interaction.response.send_message(
            "✅ Routine supprimée.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "❌ Routine introuvable.",
            ephemeral=True
        )


@bot.tree.command(name="routine_toggle", description="Active/Désactive une routine.")
@app_commands.describe(routine_id="La routine à basculer")
@app_commands.autocomplete(routine_id=routine_autocomplete)
async def routine_toggle(interaction: discord.Interaction, routine_id: int) -> None:
    """Active ou désactive une routine."""
    if not interaction.guild_id:
        return
        
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "🚫 Réservé aux administrateurs.",
            ephemeral=True
        )
        return

    new_state = await db.toggle_routine(routine_id, str(interaction.guild_id))
    
    if new_state is not None:
        await bot.routine_manager.load_routines()
        status = "activée ✅" if new_state else "désactivée ❌"
        await interaction.response.send_message(
            f"✅ Routine {status}.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "❌ Routine introuvable.",
            ephemeral=True
        )


class SoundSelectorView(discord.ui.View):
    """Vue de sélection de son avec pagination pour /play."""
    
    def __init__(self, bot, db, guild_id: int, target_channel: discord.VoiceChannel, user: discord.Member):
        super().__init__(timeout=120)
        self.bot = bot
        self.db = db
        self.guild_id = guild_id
        self.target_channel = target_channel
        self.user = user
        
        # Pagination state
        self.page = 0
        self.sounds_per_page = 24
        self.all_sounds = []  # List of (name, sound_data) tuples
        
    async def initialize(self):
        """Charge les sons disponibles."""
        sounds = await self.db.get_available_sounds(str(self.guild_id))
        self.all_sounds = sorted(sounds.items(), key=lambda x: x[0].lower())
        self.update_components()
        
    def update_components(self):
        """Met à jour les composants de la vue."""
        self.clear_items()
        
        if not self.all_sounds:
            options = [discord.SelectOption(label="Aucun son disponible", value="none", disabled=True)]
            self.add_item(discord.ui.Select(
                placeholder="Aucun son disponible",
                custom_id="sound_select",
                options=options,
                row=0
            ))
        else:
            # Calculate pagination
            start_idx = self.page * self.sounds_per_page
            end_idx = start_idx + self.sounds_per_page
            page_sounds = self.all_sounds[start_idx:end_idx]
            total_pages = (len(self.all_sounds) - 1) // self.sounds_per_page + 1
            
            # Build options - add Random option on first page
            options = []
            if self.page == 0:
                options.append(discord.SelectOption(
                    label="Random 🔥", 
                    value="__random__", 
                    description="🎲 Jouer un son aléatoire",
                    emoji="🎲"
                ))
            
            options.extend([
                discord.SelectOption(label=name[:100], value=name, description=f"🎵 {data.get('play_count', 0)} lectures")
                for name, data in page_sounds
            ])
            
            self.add_item(discord.ui.Select(
                placeholder=f"🎵 Choisir un son (Page {self.page + 1}/{total_pages})",
                custom_id="sound_select",
                options=options,
                row=0
            ))
            
            # Pagination buttons if needed
            if total_pages > 1:
                prev_btn = discord.ui.Button(
                    label="◀️ Précédent",
                    style=discord.ButtonStyle.secondary,
                    custom_id="page_prev",
                    disabled=self.page == 0,
                    row=1
                )
                prev_btn.callback = self.page_prev_callback
                self.add_item(prev_btn)
                
                info_btn = discord.ui.Button(
                    label=f"Page {self.page + 1}/{total_pages}",
                    style=discord.ButtonStyle.secondary,
                    custom_id="page_info",
                    disabled=True,
                    row=1
                )
                self.add_item(info_btn)
                
                next_btn = discord.ui.Button(
                    label="Suivant ▶️",
                    style=discord.ButtonStyle.secondary,
                    custom_id="page_next",
                    disabled=self.page >= total_pages - 1,
                    row=1
                )
                next_btn.callback = self.page_next_callback
                self.add_item(next_btn)
        
        # Skip button (to skip current sound)
        skip_btn = discord.ui.Button(
            label="⏭️ Skip",
            style=discord.ButtonStyle.primary,
            custom_id="skip",
            row=2
        )
        skip_btn.callback = self.skip_callback
        self.add_item(skip_btn)
        
        # Cancel button
        cancel_btn = discord.ui.Button(
            label="Fermer",
            style=discord.ButtonStyle.danger,
            custom_id="cancel",
            row=2
        )
        cancel_btn.callback = self.cancel_callback
        self.add_item(cancel_btn)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Vérifie que seul l'utilisateur original peut interagir."""
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ Ce menu n'est pas pour vous.", ephemeral=True)
            return False
        
        # Handle sound selection
        if interaction.data.get("custom_id") == "sound_select":
            await self.handle_sound_selection(interaction)
            return False
        
        return True
    
    def build_embed(self, last_played: str = None):
        """Construit l'embed du sélecteur."""
        total_pages = (len(self.all_sounds) - 1) // self.sounds_per_page + 1 if self.all_sounds else 1
        
        embed = discord.Embed(
            title="🎵 Quel son voulez-vous jouer ?",
            description=f"Sélectionnez un son dans la liste ci-dessous.\n"
                        f"Le son sera joué dans {self.target_channel.mention}.\n\n"
                        f"📊 **{len(self.all_sounds)}** son(s) disponible(s)",
            color=discord.Color.blue()
        )
        
        if last_played:
            embed.add_field(
                name="✅ Dernier son joué",
                value=f"🎵 **{last_played}**",
                inline=False
            )
        
        embed.set_footer(text=f"⏱️ Ce menu expire dans 2 minutes • Page {self.page + 1}/{total_pages}")
        return embed
    
    async def handle_sound_selection(self, interaction: discord.Interaction):
        """Gère la sélection d'un son."""
        sound_name = interaction.data["values"][0]
        
        if sound_name == "none":
            return
        
        # Handle random selection
        if sound_name == "__random__":
            import random
            if self.all_sounds:
                sound_name, sound_data = random.choice(self.all_sounds)
            else:
                await interaction.response.send_message("❌ Aucun son disponible.", ephemeral=True)
                return
        else:
            # Find the sound data
            sound_data = None
            for name, data in self.all_sounds:
                if name == sound_name:
                    sound_data = data
                    break
        
        if not sound_data:
            await interaction.response.send_message("❌ Son introuvable.", ephemeral=True)
            return
        
        # Get file path
        file_path = Config.get_sound_path(sound_data['guild_id'], sound_data['filename'])
        
        if not os.path.exists(file_path):
            await interaction.response.send_message(f"❌ Fichier introuvable pour `{sound_name}`.", ephemeral=True)
            return
        
        # Add to queue
        player = self.bot.player_manager.get_player(self.guild_id)
        position = player.add_to_queue(
            file_path,
            self.user.display_name,
            sound_name,
            self.target_channel
        )
        
        # Increment play count
        await self.db.increment_play_count(sound_data['guild_id'], sound_name)
        
        # Update embed with last played sound and keep the view
        embed = self.build_embed(last_played=sound_name)
        
        # Confirmation in footer
        if position == 1:
            embed.set_footer(text=f"▶️ {sound_name} en lecture • Page {self.page + 1}/{((len(self.all_sounds) - 1) // self.sounds_per_page + 1)}")
        else:
            embed.set_footer(text=f"📋 {sound_name} ajouté (position {position}) • Page {self.page + 1}/{((len(self.all_sounds) - 1) // self.sounds_per_page + 1)}")
        
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def page_prev_callback(self, interaction: discord.Interaction):
        """Page précédente."""
        self.page = max(0, self.page - 1)
        self.update_components()
        await interaction.response.edit_message(view=self)
    
    async def page_next_callback(self, interaction: discord.Interaction):
        """Page suivante."""
        max_pages = (len(self.all_sounds) - 1) // self.sounds_per_page
        self.page = min(max_pages, self.page + 1)
        self.update_components()
        await interaction.response.edit_message(view=self)
    
    async def skip_callback(self, interaction: discord.Interaction):
        """Skip le son en cours."""
        player = self.bot.player_manager.get_player(self.guild_id)
        
        if player.skip():
            embed = self.build_embed()
            embed.set_footer(text=f"⏭️ Son passé • Page {self.page + 1}/{((len(self.all_sounds) - 1) // self.sounds_per_page + 1)}")
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.send_message("ℹ️ Aucun son en cours de lecture.", ephemeral=True)
    
    async def cancel_callback(self, interaction: discord.Interaction):
        """Ferme le sélecteur."""
        await interaction.response.edit_message(content="👋 Menu fermé.", embed=None, view=None)
        self.stop()
    
    async def on_timeout(self):
        """Appelé quand la vue expire."""
        pass  # Le message sera nettoyé automatiquement


class RenameSoundView(discord.ui.View):
    """Vue de sélection de son avec pagination pour /rename_sound."""
    
    def __init__(self, bot, db, guild_id: int, user: discord.Member):
        super().__init__(timeout=120)
        self.bot = bot
        self.db = db
        self.guild_id = guild_id
        self.user = user
        
        # Pagination state
        self.page = 0
        self.sounds_per_page = 25
        self.all_sounds = []  # List of (name, sound_data) tuples
        
    async def initialize(self):
        """Charge les sons disponibles."""
        sounds = await self.db.get_available_sounds(str(self.guild_id))
        self.all_sounds = sorted(sounds.items(), key=lambda x: x[0].lower())
        self.update_components()
        
    def update_components(self):
        """Met à jour les composants de la vue."""
        self.clear_items()
        
        if not self.all_sounds:
            options = [discord.SelectOption(label="Aucun son disponible", value="none", disabled=True)]
            self.add_item(discord.ui.Select(
                placeholder="Aucun son disponible",
                custom_id="sound_select",
                options=options,
                row=0
            ))
        else:
            # Calculate pagination
            start_idx = self.page * self.sounds_per_page
            end_idx = start_idx + self.sounds_per_page
            page_sounds = self.all_sounds[start_idx:end_idx]
            total_pages = (len(self.all_sounds) - 1) // self.sounds_per_page + 1
            
            # Build options
            options = [
                discord.SelectOption(label=name[:100], value=name, description=f"🎵 {data.get('play_count', 0)} lectures")
                for name, data in page_sounds
            ]
            
            self.add_item(discord.ui.Select(
                placeholder=f"✏️ Choisir un son à renommer (Page {self.page + 1}/{total_pages})",
                custom_id="sound_select",
                options=options,
                row=0
            ))
            
            # Pagination buttons if needed
            if total_pages > 1:
                prev_btn = discord.ui.Button(
                    label="◀️ Précédent",
                    style=discord.ButtonStyle.secondary,
                    custom_id="page_prev",
                    disabled=self.page == 0,
                    row=1
                )
                prev_btn.callback = self.page_prev_callback
                self.add_item(prev_btn)
                
                info_btn = discord.ui.Button(
                    label=f"Page {self.page + 1}/{total_pages}",
                    style=discord.ButtonStyle.secondary,
                    custom_id="page_info",
                    disabled=True,
                    row=1
                )
                self.add_item(info_btn)
                
                next_btn = discord.ui.Button(
                    label="Suivant ▶️",
                    style=discord.ButtonStyle.secondary,
                    custom_id="page_next",
                    disabled=self.page >= total_pages - 1,
                    row=1
                )
                next_btn.callback = self.page_next_callback
                self.add_item(next_btn)
        
        # Cancel button
        cancel_btn = discord.ui.Button(
            label="Annuler",
            style=discord.ButtonStyle.danger,
            custom_id="cancel",
            row=2
        )
        cancel_btn.callback = self.cancel_callback
        self.add_item(cancel_btn)
    
    def build_embed(self, selected_sound: str = None, renamed_to: str = None):
        """Construit l'embed du sélecteur."""
        total_pages = (len(self.all_sounds) - 1) // self.sounds_per_page + 1 if self.all_sounds else 1
        
        embed = discord.Embed(
            title="✏️ Renommer un son",
            description=f"Sélectionnez le son que vous souhaitez renommer.\n\n"
                        f"📊 **{len(self.all_sounds)}** son(s) disponible(s)",
            color=discord.Color.orange()
        )
        
        if renamed_to:
            embed.add_field(
                name="✅ Son renommé",
                value=f"**{selected_sound}** → **{renamed_to}**",
                inline=False
            )
        
        embed.set_footer(text=f"⏱️ Ce menu expire dans 2 minutes • Page {self.page + 1}/{total_pages}")
        return embed
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Vérifie que seul l'utilisateur original peut interagir."""
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ Ce menu n'est pas pour vous.", ephemeral=True)
            return False
        
        # Handle sound selection
        if interaction.data.get("custom_id") == "sound_select":
            await self.handle_sound_selection(interaction)
            return False
        
        return True
    
    async def handle_sound_selection(self, interaction: discord.Interaction):
        """Gère la sélection d'un son - ouvre le modal de renommage."""
        sound_name = interaction.data["values"][0]
        
        if sound_name == "none":
            return
        
        # Open modal to get new name
        modal = RenameSoundModal(self, sound_name)
        await interaction.response.send_modal(modal)
    
    async def page_prev_callback(self, interaction: discord.Interaction):
        """Page précédente."""
        self.page = max(0, self.page - 1)
        self.update_components()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def page_next_callback(self, interaction: discord.Interaction):
        """Page suivante."""
        max_pages = (len(self.all_sounds) - 1) // self.sounds_per_page
        self.page = min(max_pages, self.page + 1)
        self.update_components()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def cancel_callback(self, interaction: discord.Interaction):
        """Annule la sélection."""
        await interaction.response.edit_message(content="❌ Renommage annulé.", embed=None, view=None)
        self.stop()
    
    async def on_timeout(self):
        """Appelé quand la vue expire."""
        pass


class RenameSoundModal(discord.ui.Modal, title="Renommer le son"):
    """Modal pour saisir le nouveau nom du son."""
    
    new_name = discord.ui.TextInput(
        label="Nouveau nom",
        placeholder="Entrez le nouveau nom du son...",
        min_length=1,
        max_length=100
    )
    
    def __init__(self, view: RenameSoundView, old_name: str):
        super().__init__()
        self.view = view
        self.old_name = old_name
        self.new_name.default = old_name
    
    async def on_submit(self, interaction: discord.Interaction):
        """Traite le renommage du son."""
        new_name = self.new_name.value.strip()
        
        if not new_name:
            await interaction.response.send_message("❌ Le nom ne peut pas être vide.", ephemeral=True)
            return
        
        if new_name.lower() == self.old_name.lower():
            await interaction.response.send_message("ℹ️ Le nom est identique, aucun changement.", ephemeral=True)
            return
        
        # Try to rename
        success = await self.view.db.rename_sound(str(self.view.guild_id), self.old_name, new_name)
        
        if success:
            # Update the view's sound list
            await self.view.initialize()
            embed = self.view.build_embed(selected_sound=self.old_name, renamed_to=new_name)
            await interaction.response.edit_message(embed=embed, view=self.view)
        else:
            await interaction.response.send_message(
                f"❌ Un son nommé **{new_name}** existe déjà.", 
                ephemeral=True
            )


class RoutinePanelView(discord.ui.View):
    def __init__(self, bot, db, guild_id):
        super().__init__(timeout=None)
        self.bot = bot
        self.db = db
        self.guild_id = guild_id
        self.selected_routine_id = None

    @discord.ui.select(placeholder="Choisir une routine", custom_id="select_routine", options=[discord.SelectOption(label="Chargement...", value="loading")])
    async def select_routine(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_routine_id = int(select.values[0])
        await self.refresh_view(interaction)

    @discord.ui.button(label="Activer/Désactiver", style=discord.ButtonStyle.primary, disabled=True, custom_id="toggle_btn")
    async def toggle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_routine_id: return
        
        new_state = await self.db.toggle_routine(self.selected_routine_id, str(self.guild_id))
        await self.bot.routine_manager.load_routines()
        await self.refresh_view(interaction)

    @discord.ui.button(label="Modifier", style=discord.ButtonStyle.primary, disabled=True, custom_id="edit_btn")
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_routine_id: return
        
        # Fetch routine data
        routines = await self.db.get_routines(self.guild_id)
        routine = next((r for r in routines if r['id'] == self.selected_routine_id), None)
        
        if routine:
            view = RoutineCreationView(self.bot, self.db, self.guild_id, routine_data=routine, routine_id=self.selected_routine_id)
            embed = discord.Embed(title=f"🛠️ Modification : {routine['name']}", color=discord.Color.blue())
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            await view.refresh_embed(interaction)

    @discord.ui.button(label="Supprimer", style=discord.ButtonStyle.danger, disabled=True, custom_id="delete_btn")
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_routine_id: return
        
        await self.db.delete_routine(self.selected_routine_id, str(self.guild_id))
        await self.bot.routine_manager.load_routines()
        self.selected_routine_id = None
        await self.refresh_view(interaction)

    @discord.ui.button(label="Rafraîchir", style=discord.ButtonStyle.secondary, custom_id="refresh_btn")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.refresh_view(interaction)

    async def refresh_view(self, interaction: discord.Interaction):
        routines = await self.db.get_routines(self.guild_id)
        
        select = [x for x in self.children if isinstance(x, discord.ui.Select) and x.custom_id == "select_routine"][0]
        toggle_btn = [x for x in self.children if isinstance(x, discord.ui.Button) and x.custom_id == "toggle_btn"][0]
        edit_btn = [x for x in self.children if isinstance(x, discord.ui.Button) and x.custom_id == "edit_btn"][0]
        delete_btn = [x for x in self.children if isinstance(x, discord.ui.Button) and x.custom_id == "delete_btn"][0]
        
        if not routines:
            select.options = [discord.SelectOption(label="Aucune routine", value="none")]
            select.disabled = True
            toggle_btn.disabled = True
            edit_btn.disabled = True
            delete_btn.disabled = True
            embed = discord.Embed(title="Gestion des Routines", description="Aucune routine configurée.", color=discord.Color.orange())
        else:
            options = []
            selected_routine = None
            for r in routines:
                label = f"{r['name']} ({'ON' if r['active'] else 'OFF'})"
                is_selected = (r['id'] == self.selected_routine_id)
                if is_selected: selected_routine = r
                options.append(discord.SelectOption(label=label, value=str(r['id']), default=is_selected))
            
            select.options = options[:25] # Limit 25
            select.disabled = False
            
            if self.selected_routine_id and selected_routine:
                toggle_btn.disabled = False
                edit_btn.disabled = False
                delete_btn.disabled = False
                
                # Build detail embed
                status = "✅ Activée" if selected_routine['active'] else "❌ Désactivée"
                embed = discord.Embed(title=f"Routine : {selected_routine['name']}", color=discord.Color.blue())
                embed.add_field(name="État", value=status, inline=True)
                
                # Trigger
                t_type = selected_routine['trigger_type']
                t_data = selected_routine['trigger_data']
                embed.add_field(
                    name="Trigger",
                    value=describe_trigger(t_type, t_data),
                    inline=True
                )
                
                # Conditions
                conds = selected_routine.get('conditions')
                if conds:
                    c_desc = ""
                    if conds.get('type') in ['AND', 'OR']:
                        c_desc = f"Logique: {conds['type']}\n"
                        for sub in conds.get('sub', []):
                            c_desc += f"- {sub['type']} {sub['op']} {sub['value']}\n"
                    else:
                        c_desc = f"- {conds['type']} {conds['op']} {conds['value']}"
                    embed.add_field(name="Conditions", value=c_desc, inline=False)
                else:
                    embed.add_field(name="Conditions", value="Aucune", inline=False)

                # Actions
                actions_desc = ""
                for i, a in enumerate(selected_routine['actions']):
                    if a['type'] == 'play_sound': val = f"🎵 Joue {a['sound_name']}"
                    elif a['type'] == 'wait': val = f"💤 Pause {a['delay']}s"
                    elif a['type'] == 'message': val = f"💬 Msg: {a['content']}"
                    else: val = a['type']
                    actions_desc += f"{i+1}. {val}\n"
                
                embed.add_field(name="Actions", value=actions_desc or "Aucune", inline=False)
                
            else:
                toggle_btn.disabled = True
                edit_btn.disabled = True
                delete_btn.disabled = True
                embed = discord.Embed(title="Gestion des Routines", description="Sélectionnez une routine pour voir les détails.", color=discord.Color.blue())

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

@bot.tree.command(name="routine_manage", description="Ouvre le panel de gestion des routines.")
async def routine_manage(interaction: discord.Interaction):
    if not interaction.guild_id: return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Réservé aux administrateurs.", ephemeral=True)
        return

    view = RoutinePanelView(bot, db, str(interaction.guild_id))
    embed = discord.Embed(title="Gestion des Routines", description="Chargement...", color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    await view.refresh_view(interaction)

async def owner_scope_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    choices = [app_commands.Choice(name="GLOBAL", value="global")]
    
    for guild in bot.guilds:
        if current.lower() in guild.name.lower() or current in str(guild.id):
            choices.append(app_commands.Choice(name=f"{guild.name} ({guild.id})", value=str(guild.id)))
    
    return choices[:25]

@bot.tree.command(name="owner_add", description="Ajouter un son global ou serveur (Owner uniquement).")
@app_commands.describe(
    scope="Cible (Global ou Serveur)",
    sound_name="Nom du son",
    attachment="Fichier audio"
)
@app_commands.autocomplete(scope=owner_scope_autocomplete)
async def owner_add(interaction: discord.Interaction, scope: str, sound_name: str, attachment: discord.Attachment):
    if not await bot.is_owner(interaction.user):
        await interaction.response.send_message("⛔ Cette commande est réservée au propriétaire du bot.", ephemeral=True)
        return

    target_id = scope
    real_name = sound_name.strip()

    await interaction.response.defer(ephemeral=True)

    name = real_name.lower().replace(" ", "_")
    
    # Check if exists
    if await db.get_sound(target_id, name):
        await interaction.followup.send(f"Le son `{name}` existe déjà dans `{target_id}`.", ephemeral=True)
        return

    try:
        saved_path = await audio_manager.save_upload(attachment, attachment.filename, target_id)
        filename = os.path.basename(saved_path)
        await db.add_sound(target_id, name, filename, str(interaction.user))
        await interaction.followup.send(f"✅ Son `{name}` ajouté à `{target_id}` !", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Erreur: {e}", ephemeral=True)

@bot.tree.command(name="owner_config", description="Configuration avancée (Owner uniquement).")
@app_commands.describe(
    scope="Cible (Global ou Serveur)",
    setting="Paramètre à modifier",
    value="Nouvelle valeur (0 = illimité)"
)
@app_commands.choices(setting=[
    app_commands.Choice(name="Durée max (secondes)", value="max_duration"),
    app_commands.Choice(name="Taille max (Mo)", value="max_file_size_mb"),
    app_commands.Choice(name="Longueur nom max", value="max_name_length")
])
@app_commands.autocomplete(scope=owner_scope_autocomplete)
async def owner_config(interaction: discord.Interaction, scope: str, setting: str, value: int):
    if not await bot.is_owner(interaction.user):
        await interaction.response.send_message("⛔ Cette commande est réservée au propriétaire du bot.", ephemeral=True)
        return

    if value < 0:
        await interaction.response.send_message("🚫 La valeur doit être positive ou nulle (0 pour désactiver).", ephemeral=True)
        return

    await db.set_config(scope, setting, value)
    
    scope_name = "Global" if scope == "global" else f"Serveur {scope}"
    if value == 0:
        await interaction.response.send_message(f"✅ Config `{scope_name}` : `{setting}` = `Désactivé (Illimité)`", ephemeral=True)
    else:
        await interaction.response.send_message(f"✅ Config `{scope_name}` : `{setting}` = `{value}`", ephemeral=True)

class OwnerPanelView(discord.ui.View):
    def __init__(self, bot, db):
        super().__init__(timeout=None)
        self.bot = bot
        self.db = db
        self.selected_scope = "global"
        self.selected_sound = None

    @discord.ui.select(placeholder="Choisir la portée (Scope)", custom_id="select_scope", options=[
        discord.SelectOption(label="Global", value="global", description="Sons globaux")
    ])
    async def select_scope(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_scope = select.values[0]
        self.selected_sound = None
        await self.refresh_view(interaction)

    @discord.ui.select(placeholder="Choisir un son", custom_id="select_sound", options=[discord.SelectOption(label="Chargement...", value="loading")], disabled=True)
    async def select_sound(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_sound = select.values[0]
        await self.refresh_view(interaction)

    @discord.ui.button(label="Jouer", style=discord.ButtonStyle.success, disabled=True, custom_id="play_btn")
    async def play_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_sound:
            return
        
        if not interaction.user.voice:
            await interaction.response.send_message("❌ Vous devez être dans un salon vocal.", ephemeral=True)
            return

        sound_data = await self.db.get_sound(self.selected_scope, self.selected_sound)
        if sound_data:
            file_path = os.path.join(Config.SOUNDS_DIR, self.selected_scope, sound_data['filename'])
            if os.path.exists(file_path):
                player = self.bot.player_manager.get_player(interaction.guild_id)
                player.add_to_queue(file_path, interaction.user.display_name, self.selected_sound, interaction.user.voice.channel)
                await interaction.response.send_message(f"▶️ Lecture de `{self.selected_sound}`.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Fichier introuvable.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Son introuvable.", ephemeral=True)

    @discord.ui.button(label="Supprimer", style=discord.ButtonStyle.danger, disabled=True, custom_id="delete_btn")
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_sound:
            return
        
        sound_data = await self.db.get_sound(self.selected_scope, self.selected_sound)
        if sound_data:
            file_path = os.path.join(Config.SOUNDS_DIR, self.selected_scope, sound_data['filename'])
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass
            await self.db.remove_sound(self.selected_scope, self.selected_sound)
            await interaction.response.send_message(f"✅ Son `{self.selected_sound}` supprimé.", ephemeral=True)
            
            self.selected_sound = None
            await self.refresh_view(interaction)
        else:
            await interaction.response.send_message("❌ Son introuvable.", ephemeral=True)

    @discord.ui.button(label="Rafraîchir", style=discord.ButtonStyle.secondary, custom_id="refresh_btn")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.refresh_view(interaction)

    async def refresh_view(self, interaction: discord.Interaction):
        # Update Scope Select
        scope_select = [x for x in self.children if isinstance(x, discord.ui.Select) and x.custom_id == "select_scope"][0]
        scope_options = [discord.SelectOption(label="Global", value="global", description="Sons globaux", default=(self.selected_scope == "global"))]
        
        for guild in self.bot.guilds[:24]:
            is_selected = (str(guild.id) == self.selected_scope)
            scope_options.append(discord.SelectOption(label=guild.name, value=str(guild.id), description=f"ID: {guild.id}", default=is_selected))
            
        scope_select.options = scope_options

        # Update Sound Select
        sounds = await self.db.list_sounds(self.selected_scope)
        sound_select = [x for x in self.children if isinstance(x, discord.ui.Select) and x.custom_id == "select_sound"][0]
        play_btn = [x for x in self.children if isinstance(x, discord.ui.Button) and x.custom_id == "play_btn"][0]
        delete_btn = [x for x in self.children if isinstance(x, discord.ui.Button) and x.custom_id == "delete_btn"][0]

        if not sounds:
            sound_select.options = [discord.SelectOption(label="Aucun son", value="none")]
            sound_select.disabled = True
            play_btn.disabled = True
            delete_btn.disabled = True
        else:
            options = []
            sorted_sounds = sorted(sounds.keys())
            for name in sorted_sounds[:25]:
                is_selected = (name == self.selected_sound)
                options.append(discord.SelectOption(label=name, value=name, default=is_selected))
            sound_select.options = options
            sound_select.disabled = False
            
            if self.selected_sound:
                play_btn.disabled = False
                delete_btn.disabled = False
            else:
                play_btn.disabled = True
                delete_btn.disabled = True

        # Create Embed
        embed = discord.Embed(title="Panel Admin - Gestion des Sons", color=discord.Color.blue())
        embed.add_field(name="Portée actuelle", value=f"`{self.selected_scope}`", inline=True)
        embed.add_field(name="Son sélectionné", value=f"`{self.selected_sound}`" if self.selected_sound else "*Aucun*", inline=True)
        embed.add_field(name="Total sons", value=str(len(sounds)), inline=True)
        embed.add_field(name="Aide", value="Utilisez `/owner_add` pour ajouter des sons.", inline=False)

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

@bot.tree.command(name="owner_manage", description="Ouvre le panel de gestion (Owner uniquement).")
async def owner_manage(interaction: discord.Interaction):
    if not await bot.is_owner(interaction.user):
        await interaction.response.send_message("⛔ Cette commande est réservée au propriétaire du bot.", ephemeral=True)
        return

    view = OwnerPanelView(bot, db)
    # Initial refresh to populate
    # We need to mock an interaction or just call refresh logic manually?
    # Let's just send initial state.
    
    embed = discord.Embed(title="Panel Admin - Gestion des Sons", color=discord.Color.blue())
    embed.description = "Chargement..."
    
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    await view.refresh_view(interaction)

class RoutineCreationView(discord.ui.View):
    def __init__(self, bot, db, guild_id, routine_data=None, routine_id=None):
        super().__init__(timeout=600)
        self.bot = bot
        self.db = db
        self.guild_id = guild_id
        self.routine_id = routine_id
        
        # Sound pagination state
        self.sound_page = 0
        self.sounds_per_page = 24  # 24 + 1 pour le bouton "Plus"
        self.all_sounds = []  # Cache des sons disponibles
        
        # Data State
        if routine_data:
            self.name = routine_data['name']
            self.triggers = [{"type": routine_data['trigger_type'], "data": routine_data['trigger_data']}]
            self.actions = routine_data['actions']
            
            # Parse conditions
            self.conditions = []
            self.condition_logic = "AND"
            self.advanced_logic_expr = None  # Expression logique avancée
            if routine_data['conditions']:
                c = routine_data['conditions']
                if c.get('type') in ['AND', 'OR', 'XOR']:
                    self.condition_logic = c['type']
                    self.conditions = c.get('sub', [])
                elif c.get('type') == 'EXPR':
                    # Advanced expression mode
                    self.advanced_logic_expr = c.get('expr', '')
                    self.conditions = c.get('conditions', [])
                else:
                    self.conditions = [c]
        else:
            self.name = "Nouvelle Routine"
            self.triggers = [] 
            self.conditions = [] 
            self.actions = [] 
            self.condition_logic = "AND"
            self.advanced_logic_expr = None  # Expression logique avancée (ex: "(C1 ET C2) OU C3")
        
        # UI State
        self.mode = "main" 
        self.selected_index = None 
        
        self.update_components()

    def update_components(self):
        self.clear_items()
        
        if self.mode == "main":
            # Main Dashboard
            self.add_item(discord.ui.Button(label="Modifier Nom", style=discord.ButtonStyle.secondary, custom_id="edit_name", emoji="✏️", row=0))
            self.add_item(discord.ui.Button(label=f"Triggers ({len(self.triggers)})", style=discord.ButtonStyle.primary, custom_id="menu_triggers", emoji="⚡", row=1))
            self.add_item(discord.ui.Button(label=f"Conditions ({len(self.conditions)})", style=discord.ButtonStyle.primary, custom_id="menu_conditions", emoji="🤔", row=1))
            self.add_item(discord.ui.Button(label=f"Actions ({len(self.actions)})", style=discord.ButtonStyle.primary, custom_id="menu_actions", emoji="🎬", row=1))
            
            self.add_item(discord.ui.Button(label="Sauvegarder", style=discord.ButtonStyle.success, custom_id="save", emoji="💾", row=2, disabled=(len(self.triggers)==0 or len(self.actions)==0)))
            self.add_item(discord.ui.Button(label="Annuler", style=discord.ButtonStyle.danger, custom_id="cancel", row=2))

        elif self.mode == "triggers":
            # Trigger Management
            self.add_item(discord.ui.Button(label="Timer", style=discord.ButtonStyle.success, custom_id="add_timer", emoji="⏰", row=0))
            self.add_item(discord.ui.Button(label="Heure fixe", style=discord.ButtonStyle.success, custom_id="add_schedule", emoji="🕐", row=0))
            self.add_item(discord.ui.Button(label="Event", style=discord.ButtonStyle.success, custom_id="add_event", emoji="📥", row=0))
            self.add_item(discord.ui.Button(label="Nb membres", style=discord.ButtonStyle.success, custom_id="add_count", emoji="👥", row=0))
            self.add_item(discord.ui.Button(label="Mot-clé / Réaction", style=discord.ButtonStyle.success, custom_id="add_keyword", emoji="💬", row=4))
            
            # Selection for deletion/move
            if self.triggers:
                options = []
                for i, t in enumerate(self.triggers):
                    label = f"{i+1}. {self.format_trigger(t)}"
                    options.append(discord.SelectOption(label=label[:100], value=str(i)))
                
                self.add_item(discord.ui.Select(placeholder="Sélectionner un trigger", custom_id="select_item", options=options, row=1))
                
                self.add_item(discord.ui.Button(label="Supprimer", style=discord.ButtonStyle.danger, custom_id="delete_item", row=2))
            
            self.add_item(discord.ui.Button(label="Retour", style=discord.ButtonStyle.secondary, custom_id="back", row=3))

        elif self.mode == "conditions":
            # Condition Management
            self.add_item(discord.ui.Button(label="Ajouter Condition", style=discord.ButtonStyle.success, custom_id="add_condition", emoji="➕", row=0))
            
            # Logic Toggle (simple mode) - cycles through AND -> OR -> XOR
            # Disabled when advanced logic is set
            logic_labels = {
                "AND": "Logique: TOUT (ET)",
                "OR": "Logique: AU MOINS 1 (OU)",
                "XOR": "Logique: UN SEUL (XOR)"
            }
            label = logic_labels.get(self.condition_logic, "Logique: ET")
            toggle_disabled = bool(self.advanced_logic_expr)
            self.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.primary, custom_id="toggle_logic", row=0, disabled=toggle_disabled))
            
            # Advanced Logic Button - always shown, disabled if < 2 conditions
            if self.advanced_logic_expr:
                # Show reset button when advanced mode is active
                self.add_item(discord.ui.Button(label="Réinitialiser", style=discord.ButtonStyle.danger, custom_id="reset_advanced_logic", emoji="🔄", row=0))
            else:
                adv_disabled = len(self.conditions) < 2
                self.add_item(discord.ui.Button(label="Logique Avancée", style=discord.ButtonStyle.secondary, custom_id="advanced_logic", emoji="🧮", row=0, disabled=adv_disabled))

            if self.conditions:
                options = []
                for i, c in enumerate(self.conditions):
                    label = f"C{i+1}. {self.format_condition(c)}"
                    options.append(discord.SelectOption(label=label[:100], value=str(i)))
                
                self.add_item(discord.ui.Select(placeholder="Sélectionner une condition", custom_id="select_item", options=options, row=1))
                
                self.add_item(discord.ui.Button(label="Monter", style=discord.ButtonStyle.secondary, custom_id="move_up", row=2))
                self.add_item(discord.ui.Button(label="Descendre", style=discord.ButtonStyle.secondary, custom_id="move_down", row=2))
                self.add_item(discord.ui.Button(label="Supprimer", style=discord.ButtonStyle.danger, custom_id="delete_item", row=2))

            self.add_item(discord.ui.Button(label="Retour", style=discord.ButtonStyle.secondary, custom_id="back", row=3))

        elif self.mode == "actions":
            # Action Management
            self.add_item(discord.ui.Button(label="Son", style=discord.ButtonStyle.success, custom_id="add_action_sound", emoji="🎵", row=0))
            self.add_item(discord.ui.Button(label="Pause", style=discord.ButtonStyle.success, custom_id="add_action_wait", emoji="💤", row=0))
            self.add_item(discord.ui.Button(label="Message", style=discord.ButtonStyle.success, custom_id="add_action_msg", emoji="💬", row=0))
            self.add_item(discord.ui.Button(label="Chance", style=discord.ButtonStyle.success, custom_id="add_action_chance", emoji="🎲", row=0))
            self.add_item(discord.ui.Button(label="Volume", style=discord.ButtonStyle.success, custom_id="add_action_volume", emoji="🔊", row=0))
            self.add_item(discord.ui.Button(label="Contrôle", style=discord.ButtonStyle.primary, custom_id="add_action_control", emoji="🎛️", row=4))
            self.add_item(discord.ui.Button(label="Déplacer", style=discord.ButtonStyle.primary, custom_id="add_action_move", emoji="↔️", row=4))

            if self.actions:
                options = []
                for i, a in enumerate(self.actions):
                    label = f"{i+1}. {self.format_action(a)}"
                    options.append(discord.SelectOption(label=label[:100], value=str(i)))
                
                self.add_item(discord.ui.Select(placeholder="Sélectionner une action", custom_id="select_item", options=options, row=1))
                
                self.add_item(discord.ui.Button(label="Monter", style=discord.ButtonStyle.secondary, custom_id="move_up", row=2))
                self.add_item(discord.ui.Button(label="Descendre", style=discord.ButtonStyle.secondary, custom_id="move_down", row=2))
                self.add_item(discord.ui.Button(label="Supprimer", style=discord.ButtonStyle.danger, custom_id="delete_item", row=2))

            self.add_item(discord.ui.Button(label="Retour", style=discord.ButtonStyle.secondary, custom_id="back", row=3))

    def format_trigger(self, t):
        if t['type'] == 'timer':
            return f"⏰ Timer: {format_duration(t['data'].get('interval_seconds', 0))}"
        elif t['type'] == 'schedule':
            days = t['data'].get('days') or []
            day_names = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
            when = ",".join(day_names[d] for d in days) + " " if days else ""
            return f"🕐 À {when}{t['data'].get('time')}"
        elif t['type'] == 'event':
            event = t['data'].get('event', '')
            event_labels = {
                'voice_join': '🟢 Join Vocal',
                'voice_leave': '🔴 Leave Vocal',
                'voice_move': '🔀 Move Vocal',
                'voice_mute': '🔇 Mute',
                'voice_unmute': '🔊 Unmute',
                'voice_deafen': '🚫 Deafen',
                'voice_undeafen': '🎧 Undeafen',
                'voice_stream_start': '📺 Stream Start',
                'voice_stream_stop': '📵 Stream Stop',
                'voice_video_start': '📹 Vidéo Start',
                'voice_video_stop': '📷 Vidéo Stop',
                'voice_first_join': '🥇 Premier arrivé',
                'voice_count_reached': f"👥 {t['data'].get('count', '?')} membres",
                'message': f"💬 Mot-clé « {t['data'].get('keyword', '')} »",
                'reaction': f"⭐ Réaction {t['data'].get('emoji', '')}"
            }
            return f"Event: {event_labels.get(event, event)}"
        return "Inconnu"

    def format_condition(self, c):
        return f"{c['type']} {c['op']} {c['value']}"

    def format_action(self, a):
        if a['type'] == 'play_sound':
            if a['sound_name'] == '__random__':
                return "🎲 Joue: Random 🔥"
            return f"Joue: {a['sound_name']}"
        if a['type'] == 'wait':
            if a.get('delay_min') is not None:
                return f"Pause: {format_duration(a['delay_min'])}-{format_duration(a['delay_max'])}"
            return f"Pause: {format_duration(a.get('delay', 0))}"
        if a['type'] == 'message': return f"Msg: {a['content']}"
        if a['type'] == 'dm': return f"MP: {a['content']}"
        if a['type'] == 'chance': return f"🎲 Chance: {a.get('percent')}%"
        if a['type'] == 'volume':
            value = a.get('value')
            return "🔊 Volume: reset" if value == 'reset' else f"🔊 Volume: {value}%"
        if a['type'] == 'move':
            cible = "membre" if a.get('target') == 'member' else "bot"
            return f"↔️ Déplacer {cible}"
        if a['type'] == 'player_control':
            labels = {'stop': '⏹️ Stop', 'skip': '⏭️ Skip',
                      'clear': '🧹 Vider la file', 'leave': '🚪 Quitter le salon'}
            return labels.get(a.get('command'), 'Contrôle')
        return "Action"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.type == discord.InteractionType.component:
            cid = interaction.data.get("custom_id")
            
            # Navigation
            if cid == "back":
                # If we're in sound selector, go back to actions mode
                if self.all_sounds:
                    self.mode = "actions"
                    self.sound_page = 0
                    self.all_sounds = []
                else:
                    self.mode = "main"
                self.selected_index = None
            elif cid == "menu_triggers": self.mode = "triggers"
            elif cid == "menu_conditions": self.mode = "conditions"
            elif cid == "menu_actions": self.mode = "actions"
            elif cid == "cancel":
                await interaction.response.edit_message(content="❌ Création annulée.", embed=None, view=None)
                return False
            
            # Main Actions
            elif cid == "edit_name":
                await interaction.response.send_modal(NameInputModal(self))
                return False
            elif cid == "save":
                await self.save_routine(interaction)
                return False

            # Trigger Actions
            elif cid == "add_timer":
                await interaction.response.send_modal(TimeInputModal(self))
                return False
            elif cid == "add_schedule":
                await interaction.response.send_modal(ScheduleInputModal(self))
                return False
            elif cid == "add_count":
                await interaction.response.send_modal(CountInputModal(self))
                return False
            elif cid == "add_keyword":
                await interaction.response.send_modal(KeywordInputModal(self))
                return False
            elif cid == "add_event":
                # Quick select for event
                self.add_item(discord.ui.Select(placeholder="Choisir l'événement", custom_id="quick_select_event", options=[
                    discord.SelectOption(label="🥇 Premier arrivé", value="voice_first_join", description="Le premier humain dans un salon vide"),
                    discord.SelectOption(label="🟢 Join Vocal", value="voice_join", description="Quand quelqu'un rejoint un salon"),
                    discord.SelectOption(label="🔴 Leave Vocal", value="voice_leave", description="Quand quelqu'un quitte un salon"),
                    discord.SelectOption(label="🔀 Move Vocal", value="voice_move", description="Quand quelqu'un change de salon"),
                    discord.SelectOption(label="🔇 Mute", value="voice_mute", description="Quand quelqu'un coupe son micro"),
                    discord.SelectOption(label="🔊 Unmute", value="voice_unmute", description="Quand quelqu'un active son micro"),
                    discord.SelectOption(label="🚫 Deafen", value="voice_deafen", description="Quand quelqu'un coupe son casque"),
                    discord.SelectOption(label="🎧 Undeafen", value="voice_undeafen", description="Quand quelqu'un active son casque"),
                    discord.SelectOption(label="📺 Stream Start", value="voice_stream_start", description="Quand quelqu'un lance un stream"),
                    discord.SelectOption(label="📵 Stream Stop", value="voice_stream_stop", description="Quand quelqu'un arrête son stream"),
                    discord.SelectOption(label="📹 Vidéo Start", value="voice_video_start", description="Quand quelqu'un active sa caméra"),
                    discord.SelectOption(label="📷 Vidéo Stop", value="voice_video_stop", description="Quand quelqu'un désactive sa caméra")
                ]))
                await interaction.response.edit_message(view=self) # Update to show select
                return False
            
            # Condition Actions
            elif cid == "add_condition":
                await interaction.response.send_modal(ConditionInputModal(self))
                return False
            elif cid == "toggle_logic":
                # Cycle through AND -> OR -> XOR -> AND (only works when not in advanced mode)
                if not self.advanced_logic_expr:
                    if self.condition_logic == "AND":
                        self.condition_logic = "OR"
                    elif self.condition_logic == "OR":
                        self.condition_logic = "XOR"
                    else:
                        self.condition_logic = "AND"
            elif cid == "advanced_logic":
                await self.show_advanced_logic_panel(interaction)
                return False
            elif cid == "reset_advanced_logic":
                # Reset to simple mode with AND as default
                self.advanced_logic_expr = None
                self.condition_logic = "AND"

            # Action Actions
            elif cid == "add_action_sound":
                # Show paginated sound selector
                sounds = await self.db.get_available_sounds(self.guild_id)
                self.all_sounds = sorted(sounds.keys())
                self.sound_page = 0
                await self._show_sound_selector(interaction)
                return False
            elif cid == "sound_page_prev":
                # Page précédente des sons
                self.sound_page = max(0, self.sound_page - 1)
                await self._show_sound_selector(interaction)
                return False
            elif cid == "sound_page_next":
                # Page suivante des sons
                max_pages = (len(self.all_sounds) - 1) // self.sounds_per_page
                self.sound_page = min(max_pages, self.sound_page + 1)
                await self._show_sound_selector(interaction)
                return False
            elif cid == "add_action_wait":
                await interaction.response.send_modal(WaitInputModal(self))
                return False
            elif cid == "add_action_msg":
                await interaction.response.send_modal(MessageInputModal(self))
                return False
            elif cid == "add_action_chance":
                await interaction.response.send_modal(ChanceInputModal(self))
                return False
            elif cid == "add_action_volume":
                await interaction.response.send_modal(VolumeInputModal(self))
                return False
            elif cid == "add_action_move":
                await interaction.response.send_modal(MoveInputModal(self))
                return False
            elif cid == "add_action_control":
                self.add_item(discord.ui.Select(placeholder="Choisir un contrôle", custom_id="quick_select_control", options=[
                    discord.SelectOption(label="⏹️ Stop", value="stop", description="Arrête la lecture et vide la file"),
                    discord.SelectOption(label="⏭️ Skip", value="skip", description="Passe au son suivant"),
                    discord.SelectOption(label="🧹 Vider la file", value="clear", description="Vide la file sans couper le son"),
                    discord.SelectOption(label="🚪 Quitter le salon", value="leave", description="Le bot se déconnecte du vocal")
                ]))
                await interaction.response.edit_message(view=self)
                return False

            # List Management (Select)
            elif cid == "select_item":
                self.selected_index = int(interaction.data["values"][0])
            
            # List Management (Buttons)
            elif cid == "delete_item" and self.selected_index is not None:
                if self.mode == "triggers": self.triggers.pop(self.selected_index)
                elif self.mode == "conditions": self.conditions.pop(self.selected_index)
                elif self.mode == "actions": self.actions.pop(self.selected_index)
                self.selected_index = None
            
            elif cid == "move_up" and self.selected_index is not None and self.selected_index > 0:
                lst = self.conditions if self.mode == "conditions" else self.actions
                lst[self.selected_index], lst[self.selected_index-1] = lst[self.selected_index-1], lst[self.selected_index]
                self.selected_index -= 1
            
            elif cid == "move_down" and self.selected_index is not None:
                lst = self.conditions if self.mode == "conditions" else self.actions
                if self.selected_index < len(lst) - 1:
                    lst[self.selected_index], lst[self.selected_index+1] = lst[self.selected_index+1], lst[self.selected_index]
                    self.selected_index += 1

            # Quick Select Handlers
            elif cid == "quick_select_event":
                val = interaction.data["values"][0]
                self.triggers.append({"type": "event", "data": {"event": val}})
                # Remove the select by updating components
            elif cid == "quick_select_control":
                self.actions.append({
                    "type": "player_control",
                    "command": interaction.data["values"][0]
                })
            elif cid == "quick_select_sound":
                val = interaction.data["values"][0]
                if val != "none":
                    # For random, store special marker that routine_manager will handle
                    if val == "__random__":
                        self.actions.append({"type": "play_sound", "sound_name": "__random__", "target_strategy": "active"})
                    else:
                        self.actions.append({"type": "play_sound", "sound_name": val, "target_strategy": "active"})
                # Reset sound pagination state and return to actions menu
                self.sound_page = 0
                self.all_sounds = []
                self.mode = "actions"

            self.update_components()
            await self.refresh_embed(interaction)
        return True

    async def show_advanced_logic_panel(self, interaction: discord.Interaction):
        """Affiche le panel de logique avancée et attend un message de l'utilisateur."""
        # Build conditions list with diminutives
        cond_list = ""
        for i, c in enumerate(self.conditions):
            cond_list += f"  **C{i+1}** : {self.format_condition(c)}\n"
        
        embed = discord.Embed(
            title="🧮 Mode Conditions Avancées",
            color=discord.Color.purple()
        )
        
        embed.add_field(
            name="📋 Vos conditions",
            value=cond_list or "*Aucune condition*",
            inline=False
        )
        
        embed.add_field(
            name="📝 Connecteurs logiques",
            value=(
                "• **ET** / **AND** : Les deux doivent être vraies\n"
                "• **OU** / **OR** : Au moins une doit être vraie\n"
                "• **XOR** : Exactement une seule vraie\n"
                "• **NON** / **NOT** : Inverse la condition\n"
                "• **( )** : Définir les priorités"
            ),
            inline=False
        )
        
        embed.add_field(
            name="💡 Exemples",
            value=(
                "`(C1 ET C2) OU C3`\n"
                "→ Si (user ET time), OU si role\n\n"
                "`C1 ET (C2 OU C3)`\n"
                "→ Si user ET (time OU role)\n\n"
                "`NON C1 ET C2`\n"
                "→ Si PAS user ET time\n\n"
                "`C1 XOR C2`\n"
                "→ Si user OU time mais pas les deux"
            ),
            inline=False
        )
        
        current_expr = self.advanced_logic_expr or f"C1 ET C2 ET ... (défaut: {self.condition_logic})"
        embed.add_field(
            name="⚙️ Expression actuelle",
            value=f"`{current_expr}`",
            inline=False
        )
        
        embed.set_footer(text="⌨️ Envoyez votre expression dans le chat (ou 'annuler' pour revenir)...")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # Wait for user message
        def check(m):
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel_id
        
        try:
            msg = await self.bot.wait_for('message', timeout=120.0, check=check)
            
            # Try to delete the user's message
            try:
                await msg.delete()
            except:
                pass
            
            if msg.content.lower() == 'annuler':
                await interaction.followup.send("❌ Annulé. Retour au mode simple.", ephemeral=True)
                return
            
            # Parse the expression
            try:
                parsed = self.parse_logic_expression(msg.content)
                self.advanced_logic_expr = msg.content.upper()
                await interaction.followup.send(f"✅ Expression logique enregistrée : `{self.advanced_logic_expr}`", ephemeral=True)
            except ValueError as e:
                await interaction.followup.send(f"❌ Erreur de syntaxe : {e}", ephemeral=True)
                
        except asyncio.TimeoutError:
            await interaction.followup.send("⏰ Temps écoulé. Aucune modification.", ephemeral=True)

    def parse_logic_expression(self, expr: str) -> dict:
        """
        Parse une expression logique avec parenthèses et retourne un arbre de conditions.
        Exemple: "(C1 ET C2) OU C3" -> {"type": "OR", "sub": [{"type": "AND", "sub": [C1, C2]}, C3]}
        """
        # Normalize expression
        expr = expr.upper().strip()
        expr = expr.replace("AND", " ET ").replace("OR", " OU ").replace("NOT", " NON ")
        expr = " ".join(expr.split())  # Normalize whitespace
        
        # Tokenize
        tokens = self._tokenize(expr)
        
        # Parse with operator precedence: NOT > AND/ET > XOR > OR/OU
        result, pos = self._parse_or(tokens, 0)
        
        if pos < len(tokens):
            raise ValueError(f"Token inattendu : {tokens[pos]}")
        
        return result
    
    def _tokenize(self, expr: str) -> list:
        """Tokenize l'expression en liste de tokens."""
        tokens = []
        i = 0
        while i < len(expr):
            if expr[i] in '()':
                tokens.append(expr[i])
                i += 1
            elif expr[i] == ' ':
                i += 1
            else:
                # Read word
                j = i
                while j < len(expr) and expr[j] not in '() ':
                    j += 1
                word = expr[i:j]
                tokens.append(word)
                i = j
        return tokens
    
    def _parse_or(self, tokens: list, pos: int) -> tuple:
        """Parse OR/OU expressions (lowest precedence)."""
        left, pos = self._parse_xor(tokens, pos)
        
        while pos < len(tokens) and tokens[pos] == 'OU':
            pos += 1  # Skip 'OU'
            right, pos = self._parse_xor(tokens, pos)
            left = {"type": "OR", "sub": [left, right]}
        
        return left, pos
    
    def _parse_xor(self, tokens: list, pos: int) -> tuple:
        """Parse XOR expressions."""
        left, pos = self._parse_and(tokens, pos)
        
        while pos < len(tokens) and tokens[pos] == 'XOR':
            pos += 1  # Skip 'XOR'
            right, pos = self._parse_and(tokens, pos)
            left = {"type": "XOR", "sub": [left, right]}
        
        return left, pos
    
    def _parse_and(self, tokens: list, pos: int) -> tuple:
        """Parse AND/ET expressions."""
        left, pos = self._parse_not(tokens, pos)
        
        while pos < len(tokens) and tokens[pos] == 'ET':
            pos += 1  # Skip 'ET'
            right, pos = self._parse_not(tokens, pos)
            left = {"type": "AND", "sub": [left, right]}
        
        return left, pos
    
    def _parse_not(self, tokens: list, pos: int) -> tuple:
        """Parse NOT/NON expressions."""
        if pos < len(tokens) and tokens[pos] == 'NON':
            pos += 1  # Skip 'NON'
            operand, pos = self._parse_not(tokens, pos)  # NOT is right-associative
            return {"type": "NOT", "sub": [operand]}, pos
        
        return self._parse_primary(tokens, pos)
    
    def _parse_primary(self, tokens: list, pos: int) -> tuple:
        """Parse primary expressions (conditions or parenthesized expressions)."""
        if pos >= len(tokens):
            raise ValueError("Expression incomplète")
        
        token = tokens[pos]
        
        if token == '(':
            pos += 1  # Skip '('
            result, pos = self._parse_or(tokens, pos)
            if pos >= len(tokens) or tokens[pos] != ')':
                raise ValueError("Parenthèse fermante ')' manquante")
            pos += 1  # Skip ')'
            return result, pos
        
        elif token.startswith('C') and token[1:].isdigit():
            # Condition reference like C1, C2, etc.
            idx = int(token[1:]) - 1  # C1 -> index 0
            if idx < 0 or idx >= len(self.conditions):
                raise ValueError(f"Condition {token} n'existe pas (max: C{len(self.conditions)})")
            return self.conditions[idx], pos + 1
        
        else:
            raise ValueError(f"Token invalide : '{token}'. Utilisez C1, C2, etc.")

    def build_condition_tree_from_expr(self) -> dict:
        """Construit l'arbre de conditions à partir de l'expression avancée ou du mode simple."""
        if self.advanced_logic_expr:
            return self.parse_logic_expression(self.advanced_logic_expr)
        elif len(self.conditions) == 1:
            return self.conditions[0]
        elif len(self.conditions) > 1:
            return {"type": self.condition_logic, "sub": self.conditions}
        return None

    async def _show_sound_selector(self, interaction: discord.Interaction):
        """Affiche le sélecteur de sons avec pagination."""
        # Remove any existing sound selector
        self.clear_items()
        
        if not self.all_sounds:
            # No sounds available
            options = [discord.SelectOption(label="Aucun son disponible", value="none", disabled=True)]
            self.add_item(discord.ui.Select(
                placeholder="Aucun son disponible", 
                custom_id="quick_select_sound", 
                options=options
            ))
        else:
            # Calculate pagination
            start_idx = self.sound_page * self.sounds_per_page
            end_idx = start_idx + self.sounds_per_page
            page_sounds = self.all_sounds[start_idx:end_idx]
            total_pages = (len(self.all_sounds) - 1) // self.sounds_per_page + 1
            
            # Build options for current page - add Random option on first page
            options = []
            if self.sound_page == 0:
                options.append(discord.SelectOption(
                    label="Random 🔥", 
                    value="__random__", 
                    emoji="🎲"
                ))
            
            options.extend([discord.SelectOption(label=name[:100], value=name) for name in page_sounds])
            
            self.add_item(discord.ui.Select(
                placeholder=f"Choisir un son (Page {self.sound_page + 1}/{total_pages})", 
                custom_id="quick_select_sound", 
                options=options,
                row=0
            ))
            
            # Add pagination buttons if needed
            if total_pages > 1:
                prev_disabled = self.sound_page == 0
                next_disabled = self.sound_page >= total_pages - 1
                
                self.add_item(discord.ui.Button(
                    label="◀️ Précédent", 
                    style=discord.ButtonStyle.secondary, 
                    custom_id="sound_page_prev",
                    disabled=prev_disabled,
                    row=1
                ))
                self.add_item(discord.ui.Button(
                    label=f"Page {self.sound_page + 1}/{total_pages}", 
                    style=discord.ButtonStyle.secondary, 
                    custom_id="sound_page_info",
                    disabled=True,
                    row=1
                ))
                self.add_item(discord.ui.Button(
                    label="Suivant ▶️", 
                    style=discord.ButtonStyle.secondary, 
                    custom_id="sound_page_next",
                    disabled=next_disabled,
                    row=1
                ))
        
        # Add back button
        self.add_item(discord.ui.Button(
            label="Annuler", 
            style=discord.ButtonStyle.danger, 
            custom_id="back",
            row=2
        ))
        
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)

    async def refresh_embed(self, interaction: discord.Interaction):
        embed = discord.Embed(title=f"🛠️ {self.name}", color=discord.Color.blue())
        
        # Build Description based on state
        desc = ""
        
        # Triggers
        desc += f"**⚡ Triggers ({len(self.triggers)})**\n"
        if not self.triggers: desc += "*Aucun déclencheur*\n"
        for i, t in enumerate(self.triggers):
            desc += f"`{i+1}.` {self.format_trigger(t)}\n"
        
        # Conditions - show with C1, C2, etc. for advanced mode
        if self.advanced_logic_expr:
            desc += f"\n**🤔 Conditions (Avancé)**\n"
            desc += f"*Expression:* `{self.advanced_logic_expr}`\n"
        else:
            logic_label = {"AND": "ET", "OR": "OU", "XOR": "XOR"}.get(self.condition_logic, self.condition_logic)
            desc += f"\n**🤔 Conditions ({logic_label})**\n"
        
        if not self.conditions: 
            desc += "*Aucune condition*\n"
        else:
            for i, c in enumerate(self.conditions):
                desc += f"`C{i+1}.` {self.format_condition(c)}\n"
            
        # Actions
        desc += f"\n**🎬 Actions**\n"
        if not self.actions: desc += "*Aucune action*\n"
        for i, a in enumerate(self.actions):
            desc += f"`{i+1}.` {self.format_action(a)}\n"

        embed.description = desc
        
        if self.mode != "main":
            embed.set_footer(text=f"Mode Édition: {self.mode.upper()} - Utilisez les boutons pour modifier.")
        else:
            embed.set_footer(text="Configurez votre routine et sauvegardez.")

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def save_routine(self, interaction: discord.Interaction):
        primary_trigger = self.triggers[0]
        
        # Compile conditions using advanced expression or simple mode
        final_conditions = self.build_condition_tree_from_expr()

        if self.routine_id:
            await self.db.update_routine(
                self.routine_id,
                self.name,
                primary_trigger["type"],
                primary_trigger["data"],
                self.actions,
                final_conditions,
                str(self.guild_id)
            )
            msg = f"La routine **{self.name}** a été mise à jour."
        else:
            await self.db.add_routine(
                str(self.guild_id),
                self.name,
                primary_trigger["type"],
                primary_trigger["data"],
                self.actions,
                final_conditions
            )
            msg = f"La routine **{self.name}** a été créée."

        await self.bot.routine_manager.load_routines()
        
        embed = discord.Embed(title="✅ Routine Sauvegardée", description=msg, color=discord.Color.green())
        await interaction.response.edit_message(embed=embed, view=None)

class TimeInputModal(discord.ui.Modal, title="Ajouter Timer"):
    duration = discord.ui.TextInput(label="Intervalle (ex: 10s, 5m, 1h30m)", placeholder="10s")
    def __init__(self, view):
        super().__init__()
        self.view = view
    async def on_submit(self, interaction: discord.Interaction):
        try:
            seconds = parse_duration_seconds(self.duration.value)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        if seconds <= 0:
            await interaction.response.send_message(
                "❌ L'intervalle doit être supérieur à 0.", ephemeral=True)
            return
        self.view.triggers.append({"type": "timer", "data": {"interval_seconds": seconds}})
        self.view.update_components()
        await self.view.refresh_embed(interaction)


class ScheduleInputModal(discord.ui.Modal, title="Déclenchement à heure fixe"):
    heure = discord.ui.TextInput(label="Heure (HH:MM)", placeholder="18:00")
    jours = discord.ui.TextInput(
        label="Jours (optionnel)",
        placeholder="lun,mar,ven — vide = tous les jours",
        required=False
    )

    def __init__(self, view):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        raw_time = self.heure.value.strip()
        if not re.fullmatch(r'\d{1,2}:\d{2}', raw_time):
            await interaction.response.send_message(
                "❌ Heure invalide. Format attendu : HH:MM (ex: 18:00).", ephemeral=True)
            return

        hours, minutes = (int(x) for x in raw_time.split(':'))
        if not (0 <= hours <= 23 and 0 <= minutes <= 59):
            await interaction.response.send_message("❌ Heure hors plage.", ephemeral=True)
            return

        days = []
        for token in (self.jours.value or "").split(','):
            token = token.strip().lower()
            if not token:
                continue
            if token not in WEEKDAYS:
                await interaction.response.send_message(
                    f"❌ Jour inconnu : {token}. Utilisez lun, mar, mer, jeu, ven, sam, dim.",
                    ephemeral=True)
                return
            days.append(WEEKDAYS[token])

        self.view.triggers.append({
            "type": "schedule",
            "data": {"time": f"{hours:02d}:{minutes:02d}", "days": sorted(set(days))}
        })
        self.view.update_components()
        await self.view.refresh_embed(interaction)


class CountInputModal(discord.ui.Modal, title="Palier de membres"):
    count = discord.ui.TextInput(label="Nombre de membres", placeholder="3")

    def __init__(self, view):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.count.value.strip()
        if not raw.isdigit() or int(raw) < 1:
            await interaction.response.send_message(
                "❌ Indiquez un nombre entier supérieur ou égal à 1.", ephemeral=True)
            return

        self.view.triggers.append({
            "type": "event",
            "data": {"event": "voice_count_reached", "count": int(raw)}
        })
        self.view.update_components()
        await self.view.refresh_embed(interaction)


class KeywordInputModal(discord.ui.Modal, title="Mot-clé ou réaction"):
    kind = discord.ui.TextInput(label="Type (message ou reaction)", placeholder="message")
    value = discord.ui.TextInput(label="Mot-clé ou émoji", placeholder="bonjour")

    def __init__(self, view):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        kind = self.kind.value.strip().lower()
        value = self.value.value.strip()

        if not value:
            await interaction.response.send_message("❌ Valeur manquante.", ephemeral=True)
            return

        if kind.startswith("mess"):
            self.view.triggers.append({
                "type": "event",
                "data": {"event": "message", "keyword": value}
            })
            if not Config.MESSAGE_CONTENT_INTENT:
                await interaction.response.send_message(
                    "⚠️ Trigger ajouté, mais l'intent « Message Content » est désactivé : "
                    "il ne se déclenchera pas tant que MESSAGE_CONTENT_INTENT=true n'est pas "
                    "défini et l'intent coché dans le portail Discord.",
                    ephemeral=True
                )
                self.view.update_components()
                return
        elif kind.startswith("react"):
            self.view.triggers.append({
                "type": "event",
                "data": {"event": "reaction", "emoji": value}
            })
        else:
            await interaction.response.send_message(
                "❌ Type invalide. Utilisez « message » ou « reaction ».", ephemeral=True)
            return

        self.view.update_components()
        await self.view.refresh_embed(interaction)

class ConditionInputModal(discord.ui.Modal, title="Ajouter Condition"):
    c_type = discord.ui.TextInput(
        label="Type",
        placeholder="user, channel, role, time, date, count, chance, day, playing"
    )
    value = discord.ui.TextInput(
        label="Valeur",
        placeholder="123456789 · 18:00-23:00 · 3 · 30 · lun,ven · false"
    )
    op = discord.ui.TextInput(
        label="Opérateur (==, !=, >, <, >=, <=)",
        placeholder="==",
        required=False,
        default="=="
    )

    VALID_TYPES = {
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
        "playing": "is_playing",
    }
    VALID_OPS = {"==", "!=", ">", "<", ">=", "<="}

    def __init__(self, view):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        t = self.c_type.value.lower().strip()
        v = self.value.value.strip()
        o = (self.op.value or "==").strip() or "=="

        if t not in self.VALID_TYPES:
            await interaction.response.send_message(
                f"❌ Type invalide. Utilisez : {', '.join(sorted(set(self.VALID_TYPES)))}",
                ephemeral=True
            )
            return

        if o not in self.VALID_OPS:
            await interaction.response.send_message(
                f"❌ Opérateur invalide. Utilisez : {', '.join(sorted(self.VALID_OPS))}",
                ephemeral=True
            )
            return

        self.view.conditions.append({"type": self.VALID_TYPES[t], "value": v, "op": o})
        self.view.update_components()
        await self.view.refresh_embed(interaction)

class WaitInputModal(discord.ui.Modal, title="Ajouter Pause"):
    duration = discord.ui.TextInput(
        label="Durée ou plage",
        placeholder="5s, 1m20s, ou 1m20s-2h pour un délai aléatoire"
    )

    def __init__(self, view):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            low, high = parse_duration_range(self.duration.value)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        if high > low:
            self.view.actions.append({"type": "wait", "delay_min": low, "delay_max": high})
        else:
            self.view.actions.append({"type": "wait", "delay": low})

        self.view.update_components()
        await self.view.refresh_embed(interaction)


class ChanceInputModal(discord.ui.Modal, title="Probabilité"):
    percent = discord.ui.TextInput(label="Pourcentage (0-100)", placeholder="30")

    def __init__(self, view):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.percent.value.strip().rstrip('%')
        try:
            value = float(raw)
        except ValueError:
            await interaction.response.send_message("❌ Pourcentage invalide.", ephemeral=True)
            return

        if not 0 <= value <= 100:
            await interaction.response.send_message(
                "❌ Le pourcentage doit être compris entre 0 et 100.", ephemeral=True)
            return

        self.view.actions.append({"type": "chance", "percent": value})
        self.view.update_components()
        await self.view.refresh_embed(interaction)


class VolumeInputModal(discord.ui.Modal, title="Changer le volume"):
    value = discord.ui.TextInput(
        label="Volume 0-200, ou 'reset'",
        placeholder="150"
    )

    def __init__(self, view):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.value.value.strip().lower()

        if raw != "reset":
            if not raw.isdigit() or not 0 <= int(raw) <= 200:
                await interaction.response.send_message(
                    "❌ Indiquez un entier entre 0 et 200, ou « reset ».", ephemeral=True)
                return
            raw = int(raw)

        self.view.actions.append({"type": "volume", "value": raw})
        self.view.update_components()
        await self.view.refresh_embed(interaction)


class MoveInputModal(discord.ui.Modal, title="Déplacer vers un salon"):
    channel_id = discord.ui.TextInput(label="ID du salon vocal", placeholder="123456789012345678")
    target = discord.ui.TextInput(
        label="Qui déplacer ? (bot ou membre)",
        placeholder="bot",
        required=False,
        default="bot"
    )

    def __init__(self, view):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        cid = self.channel_id.value.strip().strip("<>#")
        if not cid.isdigit():
            await interaction.response.send_message("❌ ID de salon invalide.", ephemeral=True)
            return

        raw_target = (self.target.value or "bot").strip().lower()
        target = "member" if raw_target.startswith(("mem", "mbr", "user")) else "bot"

        self.view.actions.append({"type": "move", "target": target, "channel_id": cid})
        self.view.update_components()
        await self.view.refresh_embed(interaction)

class MessageInputModal(discord.ui.Modal, title="Ajouter Message"):
    content = discord.ui.TextInput(label="Message", placeholder="Coucou {user}!")
    channel_id = discord.ui.TextInput(
        label="ID Salon, ou 'mp'",
        required=False,
        placeholder="Vide = salon courant · 'mp' = message privé"
    )

    def __init__(self, view):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        cid = (self.channel_id.value or "").strip()

        # "mp" transforme l'action en message privé au membre déclencheur
        if cid.lower() in ("mp", "dm", "privé", "prive"):
            self.view.actions.append({"type": "dm", "content": self.content.value})
        else:
            self.view.actions.append({
                "type": "message",
                "content": self.content.value,
                "channel_id": cid or None
            })

        self.view.update_components()
        await self.view.refresh_embed(interaction)

class NameInputModal(discord.ui.Modal, title="Nommer la routine"):
    name = discord.ui.TextInput(label="Nom", placeholder="Ma Super Routine")
    def __init__(self, view):
        super().__init__()
        self.view = view
    async def on_submit(self, interaction: discord.Interaction):
        self.view.name = self.name.value
        self.view.update_components()
        await self.view.refresh_embed(interaction)

@bot.tree.command(name="routine_create", description="Assistant de création de routine.")
async def routine_create(interaction: discord.Interaction):
    if not interaction.guild_id: return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Réservé aux administrateurs.", ephemeral=True)
        return

    view = RoutineCreationView(bot, db, str(interaction.guild_id))
    embed = discord.Embed(title="🧙 Créateur de Routine", description="Chargement...", color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    await view.refresh_embed(interaction)

@bot.tree.command(name="routine_cmd", description="Créer une routine via commande textuelle.")
@app_commands.describe(
    name="Nom de la routine",
    command="Commande (ex: timer 30s do play son | on join if user=ID do wait 2s then play bienvenue)"
)
async def routine_cmd(interaction: discord.Interaction, name: str, command: str):
    if not interaction.guild_id: 
        await interaction.response.send_message("Commande serveur uniquement.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Réservé aux administrateurs.", ephemeral=True)
        return

    try:
        trigger_type, trigger_data, conditions, actions = bot.routine_manager.parse_routine_string(command)
        
        await db.add_routine(
            str(interaction.guild_id),
            name,
            trigger_type,
            trigger_data,
            actions,
            conditions
        )
        await bot.routine_manager.load_routines()
        
        # Build confirmation message
        trigger_desc = describe_trigger(trigger_type, trigger_data)
        actions_desc = " → ".join(describe_action(a) for a in actions)
        
        embed = discord.Embed(title="✅ Routine créée", color=discord.Color.green())
        embed.add_field(name="Nom", value=name, inline=True)
        embed.add_field(name="Trigger", value=trigger_desc, inline=True)
        embed.add_field(name="Actions", value=actions_desc or "Aucune", inline=False)
        if conditions:
            embed.add_field(name="Conditions", value=str(conditions), inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except ValueError as e:
        await interaction.response.send_message(f"❌ Erreur de syntaxe: {e}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Erreur: {e}", ephemeral=True)


if __name__ == "__main__":
    try:
        bot.run(Config.DISCORD_TOKEN)
    except discord.PrivilegedIntentsRequired:
        logger.critical(
            "❌ L'intent privilégié « Server Members » est demandé mais n'est pas "
            "activé pour ce bot.\n"
            "   → Activez-le dans le portail développeur Discord "
            "(Applications > votre bot > Bot > Privileged Gateway Intents),\n"
            "   → ou lancez le bot avec MEMBERS_INTENT=false pour vous en passer "
            "(les conditions role= des routines deviennent alors peu fiables)."
        )
        sys.exit(1)
    except discord.LoginFailure:
        logger.critical("❌ Token Discord refusé. Vérifiez DISCORD_TOKEN.")
        sys.exit(1)