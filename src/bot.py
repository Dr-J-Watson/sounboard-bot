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
import copy
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
from routine_manager import RoutineManager
from blocks import (
    ACTION_MENU,
    CONDITION_BLOCKS,
    TRIGGER_MENU,
    WEEKDAYS,
    action_syntax_help,
    condition_syntax_help,
    describe_action,
    describe_condition,
    describe_trigger,
    parse_duration_range,
    trigger_by_key,
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
        
        # Statut, entièrement paramétrable par variables d'environnement
        await self._apply_presence()

    async def _apply_presence(self) -> None:
        """
        Applique le statut configuré (ACTIVITY_TYPE, ACTIVITY_TEXT, STATUS).

        Un texte vide retire l'activité sans toucher à la présence.
        """
        status = {
            "online": discord.Status.online,
            "idle": discord.Status.idle,
            "dnd": discord.Status.dnd,
            "invisible": discord.Status.invisible,
        }.get(Config.STATUS, discord.Status.online)

        text = (Config.ACTIVITY_TEXT or "").strip()
        if not text:
            await self.change_presence(status=status, activity=None)
            logger.info(f"Statut appliqué: {Config.STATUS}, sans activité")
            return

        if Config.ACTIVITY_TYPE == "custom":
            activity = discord.CustomActivity(name=text)
        else:
            activity_type = {
                "playing": discord.ActivityType.playing,
                "listening": discord.ActivityType.listening,
                "watching": discord.ActivityType.watching,
                "competing": discord.ActivityType.competing,
            }.get(Config.ACTIVITY_TYPE, discord.ActivityType.listening)
            activity = discord.Activity(type=activity_type, name=text)

        await self.change_presence(status=status, activity=activity)
        logger.info(f"Statut appliqué: {Config.ACTIVITY_TYPE} « {text} » ({Config.STATUS})")

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


def render_flat_trame(flat: list, limit: int = 1000) -> str:
    """
    Rend une trame stockée à plat sous forme de liste indentée.

    Args:
        flat: Trame telle qu'enregistrée en base
        limit: Longueur maximale du texte produit

    Returns:
        Texte prêt pour un embed
    """
    if not flat:
        return "*Trame vide*"

    lines = []
    for node in flat:
        depth = int(node.get("depth", 0))
        prefix = "-" * (depth + 1)
        link = " *(ou)*" if node.get("link") == "or" else ""

        if node.get("kind") == "if":
            conditions = node.get("conditions") or []
            label = "🤔 SI " + (describe_condition(conditions[0]) if conditions else "?")
        else:
            label = describe_action(node.get("action") or {})

        lines.append(f"{prefix} {label}{link}")

    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit - 10] + "\n…"


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

# --- Contenu de l'aide, une section par page ---------------------------------

HELP_SECTIONS = {
    "accueil": {
        "label": "Accueil",
        "emoji": "🏠",
        "title": "📖 Aide du Soundboard",
        "description": (
            "Un soundboard Discord avec file d'attente multi-salons et "
            "routines automatisées.\n\n"
            "Choisissez une section dans le menu ci-dessous."
        ),
        "fields": [
            ("🎵 Sons & lecture", "Jouer, mettre en file, ajouter et lister les sons.", True),
            ("⚙️ Administration", "Limites, volume, salons ignorés, maintenance.", True),
            ("👑 Propriétaire", "Sons globaux et remise à zéro du bot.", True),
            ("🤖 Routines", "Déclencheurs multiples et trame par blocs.", True),
            ("📝 Syntaxe", "Écrire une routine en une ligne de texte.", True),
            ("💡 Exemples", "Des routines prêtes à copier.", True),
        ],
    },
    "sons": {
        "label": "Sons & lecture",
        "emoji": "🎵",
        "title": "🎵 Sons et lecture",
        "description": "Commandes accessibles à tous les membres.",
        "fields": [
            (
                "Jouer",
                "`/play <nom> [salon]` — joue un son, ou l'ajoute à la file\n"
                "`/skip` — passe au son suivant\n"
                "`/stop` — arrête la lecture **et vide toute la file**\n"
                "`/clear [salon]` — vide la file **sans couper** le son en cours",
                False
            ),
            (
                "Consulter",
                "`/queue` — file d'attente, avec le salon cible de chaque son\n"
                "`/list_sounds` — tous les sons disponibles\n"
                "`/stats [limite]` — classement des sons les plus joués, "
                "routines comprises",
                False
            ),
            (
                "Ajouter",
                "`/add_sound <fichier> [nom]` — ajoute un son au serveur\n"
                "Formats : mp3, wav, ogg, m4a, flac, webm. "
                "Durée et taille limitées par `/config`.",
                False
            ),
            (
                "À savoir",
                "La file est commune au serveur mais chaque son garde son salon. "
                "Si un salon se vide, seuls les sons qui le visaient sont "
                "ignorés, les autres continuent.",
                False
            ),
        ],
    },
    "admin": {
        "label": "Administration",
        "emoji": "⚙️",
        "title": "⚙️ Administration",
        "description": "Réservé aux membres ayant la permission Administrateur.",
        "fields": [
            (
                "Configuration",
                "`/config` — affiche toute la configuration du serveur\n"
                "`/config <paramètre> <valeur>` — modifie un réglage\n"
                "Paramètres : durée max, taille max, longueur du nom, "
                "**volume actuel** et **volume maximum** (0 = illimité)",
                False
            ),
            (
                "Gestion des sons",
                "`/delete_sound <nom>` — supprime un son et son fichier\n"
                "`/rename_sound` — renomme un son via un menu\n"
                "`/sync` — importe les fichiers présents sur le disque\n"
                "`/cleanup` — supprime les entrées sans fichier et signale "
                "les fichiers non référencés",
                False
            ),
            (
                "Salons ignorés",
                "`/ignore <salon> <action>` — ignore ou réautorise un salon\n"
                "`/ignored` — liste les salons ignorés *(accessible à tous)*\n"
                "Le bot ne joue jamais rien dans un salon ignoré.",
                False
            ),
        ],
    },
    "owner": {
        "label": "Propriétaire",
        "emoji": "👑",
        "title": "👑 Commandes du propriétaire",
        "description": (
            "Réservé au propriétaire de l'application Discord, "
            "pas aux administrateurs de serveur."
        ),
        "fields": [
            (
                "Sons globaux",
                "`/owner_add <portée> <nom> <fichier>` — ajoute un son global "
                "(disponible sur tous les serveurs) ou ciblé sur un serveur\n"
                "`/owner_manage` — panel de gestion de tous les sons",
                False
            ),
            (
                "Configuration",
                "`/owner_config <portée> <paramètre> <valeur>` — configure "
                "n'importe quel serveur sans y être",
                False
            ),
            (
                "Remise à zéro",
                "`/owner_reset <portée> [supprimer_fichiers]`\n"
                "Efface sons, routines, salons ignorés et configurations, "
                "sur un serveur ou sur tous. Les fichiers audio sont conservés "
                "sauf demande explicite.\n"
                "⚠️ Irréversible : une confirmation par mot-clé est demandée.",
                False
            ),
        ],
    },
    "routines": {
        "label": "Routines",
        "emoji": "🤖",
        "title": "🤖 Routines (automatisations)",
        "description": (
            "Une routine exécute des actions quand un événement se produit. "
            "Création réservée aux administrateurs."
        ),
        "fields": [
            (
                "Créer",
                "`/routine_create` — assistant interactif ; la sauvegarde "
                "ne ferme pas le panel, l'embed passe au vert\n"
                "`/routine_cmd <nom> <commande>` — création en une ligne "
                "*(voir la section Syntaxe)*",
                False
            ),
            (
                "Gérer",
                "`/routine_list` — liste des routines *(accessible à tous)*\n"
                "`/routine_manage` — panel : modifier, activer, supprimer\n"
                "`/routine_toggle <id>` — active ou désactive\n"
                "`/routine_delete <id>` — supprime",
                False
            ),
            (
                "Le principe (2.0)",
                "Une routine = plusieurs **déclencheurs** (n'importe lequel "
                "suffit) et une **trame** : une suite de blocs indentés, "
                "à la manière de Scratch.\n"
                "Un bloc **condition** ne s'exécute que si elle est vraie, et "
                "tout ce qui est indenté dessous lui appartient.",
                False
            ),
            (
                "Dans l'assistant",
                "**⚡ Déclencheurs** : un menu déroulant liste tous les "
                "événements possibles.\n"
                "Membres, salons et rôles se choisissent dans une liste ; "
                "plus besoin de copier un identifiant.\n"
                "Les déclencheurs *mot-clé* et *réaction* peuvent être "
                "limités à certains salons.\n"
                "**🧩 Ajouter** : deux menus, un pour les conditions, un pour "
                "les actions. Le bloc choisi se pose au bout de la trame.\n"
                "**🧵 Organiser** : déplacer, imbriquer (`➡️`), sortir (`⬅️`), "
                "et `🔀 ET/OU` pour le « sinon si ».",
                False
            ),
        ],
    },
    "syntaxe_triggers": {
        "label": "Syntaxe : déclencheurs",
        "emoji": "📝",
        "title": "📝 Syntaxe — déclencheurs",
        "description": (
            "**`<déclencheur> [or <déclencheur>…] [if <conditions>] do <actions>`**\n"
            "Plusieurs déclencheurs séparés par `or` : n'importe lequel lance la trame."
        ),
        "fields": [
            (
                "⏰ Temps",
                "`timer 30s` · `5m` · `1h30m` — intervalle régulier\n"
                "`timer 10m-20m` — intervalle tiré au sort à chaque fois\n"
                "`at 18:00` — tous les jours à cette heure\n"
                "`at lun,ven 09:30` — seulement certains jours",
                False
            ),
            (
                "🔊 Vocal",
                "`on join` · `on leave` · `on move`\n"
                "`on first_join` — premier arrivé dans un salon vide\n"
                "`on count>=3` — le salon atteint ce nombre de membres\n"
                "`on mute` · `on unmute` · `on deafen` · `on undeafen`\n"
                "`on stream` *(= `stream_start`)* · `on stream_stop`\n"
                "`on video` *(= `video_start`)* · `on video_stop`",
                False
            ),
            (
                "💬 Texte",
                "`on message <mot-clé>` — un message contient ce mot\n"
                "`on reaction <émoji>` — quelqu'un ajoute cette réaction\n"
                "*Dans le panel, ces deux déclencheurs peuvent être limités "
                "à un ou plusieurs salons.*\n"
                "*Le mot-clé nécessite l'intent « Message Content ».*",
                False
            ),
        ],
    },
    "syntaxe_actions": {
        "label": "Syntaxe : conditions & actions",
        "emoji": "🎬",
        "title": "🎬 Syntaxe — conditions et actions",
        "description": "Conditions liées par `and`, actions enchaînées par `then`.",
        "fields": [
            (
                "🤔 Conditions *(optionnelles, imbriquées en ET)*",
                condition_syntax_help(),
                False
            ),
            (
                "🎬 Actions",
                action_syntax_help(),
                False
            ),
        ],
    },
    "exemples": {
        "label": "Exemples",
        "emoji": "💡",
        "title": "💡 Exemples de routines",
        "description": "À utiliser avec `/routine_cmd <nom> <commande>`.",
        "fields": [
            (
                "Simples",
                "```\n"
                "at 18:00 do play apero\n"
                "on first_join do play intro\n"
                "timer 10m-20m do play ambiance\n"
                "on count>=4 do play foule\n```",
                False
            ),
            (
                "Avec conditions",
                "```\n"
                "on join if chance=25 do play rare\n"
                "on join if playing=false do play bienvenue\n"
                "on join or on leave do play bruit\n"
                "on join if day=sam,dim and time=20:00-23:59 do play soiree\n"
                "on join if user=123456789 do play theme_perso\n```",
                False
            ),
            (
                "Enchaînements",
                "```\n"
                "on join do wait 10s-2m then play surprise\n"
                "on join do volume 150 then play fort then volume reset\n"
                "at 23:00 do play derniere then leave\n"
                "on leave do stop then leave_now\n```",
                False
            ),
        ],
    },
}

HELP_ORDER = [
    "accueil", "sons", "admin", "owner",
    "routines", "syntaxe_triggers", "syntaxe_actions", "exemples",
]


def build_help_embed(key: str) -> discord.Embed:
    """
    Construit l'embed d'une section d'aide.

    Args:
        key: Clé de la section dans HELP_SECTIONS

    Returns:
        L'embed prêt à être envoyé
    """
    section = HELP_SECTIONS.get(key, HELP_SECTIONS["accueil"])

    embed = discord.Embed(
        title=section["title"],
        description=section["description"],
        color=discord.Color.gold()
    )
    for name, value, inline in section["fields"]:
        embed.add_field(name=name, value=value, inline=inline)

    position = HELP_ORDER.index(key) + 1 if key in HELP_ORDER else 1
    embed.set_footer(text=f"Section {position}/{len(HELP_ORDER)} · menu ci-dessous")
    return embed


class HelpView(discord.ui.View):
    """Navigation entre les sections de l'aide."""

    def __init__(self, author_id: int, current: str = "accueil"):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.current = current if current in HELP_SECTIONS else "accueil"
        self._build_select()

    def _build_select(self) -> None:
        """(Re)construit le menu avec la section courante pré-sélectionnée."""
        self.clear_items()
        self.add_item(discord.ui.Select(
            placeholder="Choisir une section…",
            custom_id="help_section",
            options=[
                discord.SelectOption(
                    label=HELP_SECTIONS[key]["label"],
                    value=key,
                    emoji=HELP_SECTIONS[key]["emoji"],
                    default=(key == self.current)
                )
                for key in HELP_ORDER
            ]
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Change de section, en réservant l'aide à son destinataire."""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Cette aide appartient à quelqu'un d'autre. Tapez `/help`.",
                ephemeral=True
            )
            return False

        if interaction.data.get("custom_id") == "help_section":
            self.current = interaction.data["values"][0]
            self._build_select()
            await interaction.response.edit_message(
                embed=build_help_embed(self.current),
                view=self
            )
        return True


@bot.tree.command(name="help", description="Affiche l'aide du bot, section par section.")
@app_commands.describe(section="Ouvrir directement une section particulière.")
@app_commands.choices(section=[
    app_commands.Choice(name="Sons & lecture", value="sons"),
    app_commands.Choice(name="Administration", value="admin"),
    app_commands.Choice(name="Propriétaire", value="owner"),
    app_commands.Choice(name="Routines", value="routines"),
    app_commands.Choice(name="Syntaxe : déclencheurs", value="syntaxe_triggers"),
    app_commands.Choice(name="Syntaxe : conditions & actions", value="syntaxe_actions"),
    app_commands.Choice(name="Exemples", value="exemples"),
])
async def help_command(
    interaction: discord.Interaction,
    section: Optional[str] = None
) -> None:
    """Affiche l'aide, navigable par sections."""
    key = section if section in HELP_SECTIONS else "accueil"

    await interaction.response.send_message(
        embed=build_help_embed(key),
        view=HelpView(interaction.user.id, key),
        ephemeral=True
    )


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
        target_channel,
        owner_guild_id=sound_data['guild_id']
    )
    
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
    value="La nouvelle valeur (0 = illimité). Laisser vide pour consulter."
)
@app_commands.choices(setting=[
    app_commands.Choice(name="Durée max (secondes)", value="max_duration"),
    app_commands.Choice(name="Taille max (Mo)", value="max_file_size_mb"),
    app_commands.Choice(name="Longueur nom max", value="max_name_length"),
    app_commands.Choice(name="Volume actuel (%)", value="volume"),
    app_commands.Choice(name="Volume maximum (%)", value="max_volume"),
])
async def config(
    interaction: discord.Interaction,
    setting: Optional[str] = None,
    value: Optional[int] = None
) -> None:
    """Consulte ou modifie la configuration du serveur."""
    if not interaction.guild_id:
        await interaction.response.send_message(
            "❌ Commande serveur uniquement.",
            ephemeral=True
        )
        return

    guild_id = str(interaction.guild_id)
    player = bot.player_manager.get_player(interaction.guild_id)

    # --- Consultation (aucun paramètre, ou paramètre sans valeur) ---------
    if value is None:
        current_volume = round(await player.get_volume() * 100)
        max_volume = await player.get_max_volume()

        embed = discord.Embed(
            title="⚙️ Configuration du serveur",
            color=discord.Color.blurple()
        )
        embed.add_field(
            name="Sons",
            value=(
                f"• Durée max : `{await db.get_config(guild_id, 'max_duration', Config.MAX_DURATION_SECONDS)}s`\n"
                f"• Taille max : `{await db.get_config(guild_id, 'max_file_size_mb', Config.MAX_FILE_SIZE_MB)} Mo`\n"
                f"• Longueur du nom : `{await db.get_config(guild_id, 'max_name_length', Config.MAX_NAME_LENGTH)}`"
            ),
            inline=False
        )
        embed.add_field(
            name="Volume",
            value=(
                f"• Volume actuel : `{current_volume}%`\n"
                f"• Volume maximum : `{max_volume}%` "
                f"*(plafond absolu : {Config.VOLUME_HARD_LIMIT}%)*"
            ),
            inline=False
        )
        embed.set_footer(text="0 = illimité · /config <paramètre> <valeur> pour modifier")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # --- Modification ------------------------------------------------------
    if setting is None:
        await interaction.response.send_message(
            "❌ Indiquez le paramètre à modifier.",
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

    # Contrôles propres aux réglages de volume
    if setting == "max_volume":
        if value > Config.VOLUME_HARD_LIMIT:
            await interaction.response.send_message(
                f"🚫 Le volume maximum ne peut pas dépasser {Config.VOLUME_HARD_LIMIT}%. "
                "Ce plafond absolu se règle avec la variable d'environnement "
                "`VOLUME_HARD_LIMIT`.",
                ephemeral=True
            )
            return

        await db.set_config(guild_id, "max_volume", value)

        # Ramener le volume courant sous le nouveau plafond si besoin
        current = await db.get_config(guild_id, "volume", Config.DEFAULT_VOLUME)
        note = ""
        if current > value:
            await db.set_config(guild_id, "volume", value)
            note = f"\nℹ️ Le volume actuel ({current}%) a été ramené à {value}%."

        player.invalidate_volume_cache()
        player.set_volume(min(current, value), value)

        await interaction.response.send_message(
            f"✅ Volume maximum : `{value}%`{note}",
            ephemeral=True
        )
        return

    if setting == "volume":
        ceiling = await player.get_max_volume()
        if value > ceiling:
            await interaction.response.send_message(
                f"🚫 Le volume ne peut pas dépasser le maximum du serveur "
                f"(`{ceiling}%`). Modifiez-le avec "
                f"`/config setting:Volume maximum (%) value:...`.",
                ephemeral=True
            )
            return

        await db.set_config(guild_id, "volume", value)
        applied = player.set_volume(value, ceiling)

        warning = "\n⚠️ Au-dessus de 100%, le son peut saturer." if value > 100 else ""
        await interaction.response.send_message(
            f"✅ Volume : `{round(applied * 100)}%`{warning}",
            ephemeral=True
        )
        return

    # Autres réglages
    await db.set_config(guild_id, setting, value)

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
        
        triggers = (r['trigger_data'] or {}).get('triggers', [])
        trigger_desc = "\n".join(
            describe_trigger(t.get('type'), t.get('data', {})) for t in triggers
        ) or "*Aucun déclencheur*"
        
        blocks = r['actions'] or []
        desc = f"{trigger_desc}\n🧵 {len(blocks)} bloc(s)"
        
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
            # SelectOption n'accepte pas `disabled`: c'est le Select lui-même
            # qui se désactive.
            self.add_item(discord.ui.Select(
                placeholder="Aucun son disponible",
                custom_id="sound_select",
                options=[discord.SelectOption(label="Aucun son disponible", value="none")],
                disabled=True,
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
            self.target_channel,
            owner_guild_id=sound_data['guild_id']
        )
        
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
            # SelectOption n'accepte pas `disabled`: c'est le Select lui-même
            # qui se désactive.
            self.add_item(discord.ui.Select(
                placeholder="Aucun son disponible",
                custom_id="sound_select",
                options=[discord.SelectOption(label="Aucun son disponible", value="none")],
                disabled=True,
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
                
                # Déclencheurs (n'importe lequel lance la trame)
                triggers = (selected_routine['trigger_data'] or {}).get('triggers', [])
                embed.add_field(
                    name=f"⚡ Déclencheurs ({len(triggers)})",
                    value="\n".join(
                        f"• {describe_trigger(t.get('type'), t.get('data', {}))}"
                        for t in triggers
                    ) or "*Aucun*",
                    inline=False
                )

                # Trame
                embed.add_field(
                    name="🧵 Trame",
                    value=render_flat_trame(selected_routine['actions'] or []),
                    inline=False
                )
                
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

class ResetConfirmView(discord.ui.View):
    """
    Confirmation en deux temps de la remise à zéro.

    Le bouton n'agit que si le propriétaire retape le mot de confirmation
    dans une modale : un clic accidentel sur une action irréversible ne
    suffit pas.
    """

    CONFIRM_WORD = "RESET"

    def __init__(self, bot, db, scope: str, guild_id: Optional[int], delete_files: bool, owner_id: int):
        super().__init__(timeout=60)
        self.bot = bot
        self.db = db
        self.scope = scope              # "guild" ou "all"
        self.guild_id = guild_id
        self.delete_files = delete_files
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Seul le propriétaire qui a lancé la commande peut confirmer."""
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "⛔ Cette confirmation ne vous appartient pas.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirmer la remise à zéro", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ResetConfirmModal(self))

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            content="❌ Remise à zéro annulée.", embed=None, view=None
        )

    async def run_reset(self, interaction: discord.Interaction) -> None:
        """
        Exécute la remise à zéro : players, base, puis fichiers.

        Args:
            interaction: Interaction de la modale de confirmation
        """
        await interaction.response.defer(ephemeral=True)
        self.stop()

        report = []

        # 1. Couper la lecture et libérer les salons vocaux
        if self.scope == "all":
            await self.bot.player_manager.disconnect_all()
            report.append("🔌 Tous les players déconnectés")
        else:
            player = self.bot.player_manager.find_player(self.guild_id)
            if player is not None:
                player.queue.clear()
                await player.disconnect()
                self.bot.player_manager.players.pop(int(self.guild_id), None)
            report.append("🔌 Player du serveur déconnecté")

        # 2. Base de données
        if self.scope == "all":
            counts = await self.db.reset_all()
        else:
            counts = await self.db.delete_guild_data(str(self.guild_id))
        report.append(
            f"🗄️ Base: {counts.get('sounds', 0)} son(s), "
            f"{counts.get('routines', 0)} routine(s), "
            f"{counts.get('ignored_channels', 0)} salon(s) ignoré(s), "
            f"{counts.get('guild_configs', 0)} config(s)"
        )

        # 3. Fichiers audio, si demandé
        if self.delete_files:
            deleted, errors = 0, 0
            if self.scope == "all":
                targets = []
                if os.path.isdir(Config.SOUNDS_DIR):
                    targets = [
                        os.path.join(Config.SOUNDS_DIR, d)
                        for d in os.listdir(Config.SOUNDS_DIR)
                        if os.path.isdir(os.path.join(Config.SOUNDS_DIR, d))
                    ]
            else:
                targets = [os.path.join(Config.SOUNDS_DIR, str(self.guild_id))]

            for directory in targets:
                if not os.path.isdir(directory):
                    continue
                for filename in os.listdir(directory):
                    path = os.path.join(directory, filename)
                    try:
                        if os.path.isfile(path):
                            os.remove(path)
                            deleted += 1
                    except OSError as e:
                        errors += 1
                        logger.error(f"Suppression impossible de {path}: {e}")

            report.append(f"📂 Fichiers: {deleted} supprimé(s)"
                          + (f", {errors} en erreur" if errors else ""))
        else:
            report.append("📂 Fichiers audio conservés sur le disque")

        # 4. Recharger les routines en mémoire
        await self.bot.routine_manager.load_routines()
        report.append("🔄 Routines rechargées")

        scope_label = "**tous les serveurs**" if self.scope == "all" else "ce serveur"
        logger.warning(
            f"⚠️ Remise à zéro effectuée par {interaction.user} "
            f"(portée={self.scope}, fichiers={self.delete_files})"
        )

        embed = discord.Embed(
            title="✅ Remise à zéro effectuée",
            description=f"Portée : {scope_label}\n\n" + "\n".join(report),
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


class ResetConfirmModal(discord.ui.Modal, title="Confirmer la remise à zéro"):
    """Demande de retaper le mot de confirmation avant d'effacer."""

    mot = discord.ui.TextInput(
        label="Tapez RESET pour confirmer",
        placeholder="RESET",
        max_length=10
    )

    def __init__(self, view: ResetConfirmView):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        if self.mot.value.strip().upper() != self.view.CONFIRM_WORD:
            await interaction.response.send_message(
                "❌ Mot de confirmation incorrect. Rien n'a été supprimé.",
                ephemeral=True
            )
            return

        await self.view.run_reset(interaction)


@bot.tree.command(
    name="owner_reset",
    description="Remet le bot à zéro : sons, routines et configs (Owner uniquement)."
)
@app_commands.describe(
    portee="Serveur actuel, ou tous les serveurs du bot.",
    supprimer_fichiers="Supprimer aussi les fichiers audio du disque (irréversible)."
)
@app_commands.choices(portee=[
    app_commands.Choice(name="Ce serveur uniquement", value="guild"),
    app_commands.Choice(name="Tous les serveurs", value="all"),
])
async def owner_reset(
    interaction: discord.Interaction,
    portee: app_commands.Choice[str],
    supprimer_fichiers: bool = False
) -> None:
    """Remise à zéro complète, réservée au propriétaire du bot."""
    if not await bot.is_owner(interaction.user):
        await interaction.response.send_message(
            "⛔ Cette commande est réservée au propriétaire du bot.",
            ephemeral=True
        )
        return

    scope = portee.value

    if scope == "guild" and not interaction.guild_id:
        await interaction.response.send_message(
            "❌ La portée « ce serveur » nécessite d'être dans un serveur.",
            ephemeral=True
        )
        return

    # Aperçu de ce qui va disparaître
    if scope == "all":
        counts = await db.count_all()
        cible = f"**tous les serveurs** ({len(bot.guilds)} connecté(s))"
    else:
        counts = await db.count_for_guild(str(interaction.guild_id))
        cible = f"**{interaction.guild.name}**"

    embed = discord.Embed(
        title="⚠️ Remise à zéro du bot",
        description=f"Cible : {cible}\n\nCette action est **irréversible**.",
        color=discord.Color.red()
    )
    embed.add_field(
        name="Seront supprimés",
        value=(
            f"• {counts['sounds']} son(s) en base\n"
            f"• {counts['routines']} routine(s)\n"
            f"• {counts['ignored_channels']} salon(s) ignoré(s)\n"
            f"• {counts['guild_configs']} configuration(s)"
        ),
        inline=False
    )
    embed.add_field(
        name="Fichiers audio",
        value=(
            "🗑️ **Supprimés du disque également**"
            if supprimer_fichiers else
            "💾 Conservés sur le disque (`/sync` permettra de les réimporter)"
        ),
        inline=False
    )
    embed.set_footer(text="Confirmation requise · expire dans 60 secondes")

    view = ResetConfirmView(
        bot, db, scope,
        interaction.guild_id,
        supprimer_fichiers,
        interaction.user.id
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


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
    """
    Éditeur de routine par blocs.

    Le principe : on fabrique des **blocs** (conditions et actions) dans une
    bibliothèque, puis on les place dans la **trame**, qui est la suite
    ordonnée et indentée réellement exécutée.

    La trame est stockée à plat, chaque entrée portant sa profondeur. C'est
    ce qui permet de monter, descendre et indenter un bloc par de simples
    opérations de liste ; le moteur reconstruit l'imbrication à l'exécution.
    """

    MAX_DEPTH = 5

    def __init__(self, bot, db, guild_id, routine_data=None, routine_id=None):
        super().__init__(timeout=600)
        self.bot = bot
        self.db = db
        self.guild_id = guild_id
        self.routine_id = routine_id

        # Pagination du sélecteur de sons
        self.sound_page = 0
        self.sounds_per_page = 24
        self.all_sounds = []

        # Données de la routine
        self.triggers = []      # [{"type": ..., "data": {...}}]
        self.blocks = []        # [{"id": n, "kind": "if"|"action", ...}]
        self.trame = []         # [{"block_id": n, "depth": d, "link": "and"|"or"}]
        self._next_block_id = 1

        if routine_data:
            self.name = routine_data['name']
            self.triggers = list((routine_data.get('trigger_data') or {}).get('triggers', []))
            self._load_trame(routine_data.get('actions') or [])
        else:
            self.name = "Nouvelle Routine"

        # État de l'interface
        self.mode = "main"
        self.selected_index = None
        self.pages = {}    # page courante de chaque menu déroulant
        self.picker = None  # sélection en cours (membre, salon, rôle)
        self.saved = False  # la routine est-elle enregistrée en l'état ?
        self.update_components()

    # ------------------------------------------------------------------
    # Chargement et sauvegarde
    # ------------------------------------------------------------------

    def _load_trame(self, flat: list) -> None:
        """
        Reconstruit bibliothèque et trame à partir de la liste enregistrée.

        Args:
            flat: Trame telle que stockée en base
        """
        for entry in flat:
            kind = entry.get('kind', 'action')
            block = {"id": self._next_block_id, "kind": kind}

            if kind == 'if':
                block["conditions"] = entry.get('conditions', [])
            else:
                block["action"] = entry.get('action', {})

            self.blocks.append(block)
            self.trame.append({
                "block_id": block["id"],
                "depth": int(entry.get('depth', 0)),
                "link": entry.get('link', 'and'),
            })
            self._next_block_id += 1

        # Une trame enregistrée avant ce contrôle peut être incohérente
        fixed = self.normalize_depths()
        if fixed:
            logger.warning(
                f"Routine « {self.name} » : {fixed} bloc(s) mal imbriqué(s) "
                "ont été réalignés au chargement."
            )

    def build_flat_trame(self) -> list:
        """
        Produit la trame à enregistrer, blocs résolus.

        Returns:
            Liste de blocs à plat, prête pour la base
        """
        self.normalize_depths()

        flat = []
        for entry in self.trame:
            block = self.get_block(entry["block_id"])
            if block is None:
                continue

            node = {
                "depth": entry.get("depth", 0),
                "link": entry.get("link", "and"),
                "kind": block["kind"],
            }
            if block["kind"] == "if":
                node["logic"] = "AND"
                node["conditions"] = block.get("conditions", [])
            else:
                node["action"] = block.get("action", {})
            flat.append(node)
        return flat

    # ------------------------------------------------------------------
    # Manipulation des blocs
    # ------------------------------------------------------------------

    def get_block(self, block_id: int):
        """Retrouve un bloc par son identifiant."""
        return next((b for b in self.blocks if b["id"] == block_id), None)

    def _new_block(self, block: dict) -> dict:
        """Enregistre un nouveau bloc dans la bibliothèque."""
        block["id"] = self._next_block_id
        self._next_block_id += 1
        self.blocks.append(block)
        return block

    def add_action_block(self, action: dict) -> dict:
        """
        Crée un bloc action et le place à la fin de la trame.

        Args:
            action: Données de l'action

        Returns:
            Le bloc créé
        """
        block = self._new_block({"kind": "action", "action": action})
        self._append_to_trame(block["id"])
        return block

    def add_condition_block(self, condition: dict) -> dict:
        """
        Crée un bloc condition et le place à la fin de la trame.

        Un bloc ne porte qu'une condition : combiner plusieurs conditions se
        fait en imbriquant les blocs (ET) ou en les chaînant en OU.

        Args:
            condition: Condition feuille

        Returns:
            Le bloc créé
        """
        block = self._new_block({"kind": "if", "conditions": [condition]})
        self._append_to_trame(block["id"])
        return block

    def _append_to_trame(self, block_id: int) -> None:
        """
        Ajoute un bloc à la fin de la trame.

        Le nouveau bloc hérite du niveau du précédent, et descend d'un cran
        si celui-ci est un bloc conditionnel : c'est presque toujours ce
        qu'on veut après avoir posé une condition.
        """
        depth = 0
        if self.trame:
            last = self.trame[-1]
            last_block = self.get_block(last["block_id"])
            depth = last["depth"]
            if last_block and last_block["kind"] == "if":
                depth = min(self.MAX_DEPTH, depth + 1)

        self.trame.append({"block_id": block_id, "depth": depth, "link": "and"})
        self.selected_index = len(self.trame) - 1
        self.saved = False

    def delete_trame_entry(self, index: int) -> None:
        """
        Retire une entrée de la trame, avec tout ce qu'elle contient.

        Args:
            index: Position dans la trame
        """
        if not 0 <= index < len(self.trame):
            return

        end = self._group_end(index)
        removed = self.trame[index:end]
        del self.trame[index:end]

        # Les blocs qui ne sont plus placés retournent... nulle part :
        # la bibliothèque ne garde que ce qui est utilisé.
        used = {e["block_id"] for e in self.trame}
        for entry in removed:
            if entry["block_id"] not in used:
                self.blocks = [b for b in self.blocks if b["id"] != entry["block_id"]]

        # Retirer une condition peut laisser ses voisins trop indentés
        self.normalize_depths()
        self.selected_index = None
        self.saved = False

    def move_entry(self, index: int, direction: int) -> None:
        """
        Déplace un bloc et son contenu vers le haut ou vers le bas.

        Args:
            index: Position du bloc
            direction: -1 pour monter, +1 pour descendre
        """
        if not 0 <= index < len(self.trame):
            return

        depth = self.trame[index]["depth"]
        end = self._group_end(index)
        group = self.trame[index:end]

        if direction < 0:
            # Chercher le frère précédent de même niveau
            prev = index - 1
            while prev >= 0 and self.trame[prev]["depth"] > depth:
                prev -= 1
            if prev < 0 or self.trame[prev]["depth"] < depth:
                return
            del self.trame[index:end]
            self.trame[prev:prev] = group
            self.selected_index = prev
            self.normalize_depths()
            self.saved = False
        else:
            if end >= len(self.trame) or self.trame[end]["depth"] < depth:
                return
            # Fin du groupe suivant
            nxt = end + 1
            while nxt < len(self.trame) and self.trame[nxt]["depth"] > self.trame[end]["depth"]:
                nxt += 1
            block_after = self.trame[end:nxt]
            del self.trame[index:nxt]
            self.trame[index:index] = block_after + group
            self.selected_index = index + len(block_after)
            self.normalize_depths()
            self.saved = False

    def indent_entry(self, index: int) -> str:
        """
        Place un bloc à l'intérieur de la condition qui le précède.

        L'imbrication n'a de sens que sous une condition : le frère
        précédent au même niveau doit être un bloc conditionnel.

        Args:
            index: Position du bloc

        Returns:
            Un message d'erreur, ou une chaîne vide si l'opération a réussi
        """
        if not 0 <= index < len(self.trame):
            return "Aucun bloc sélectionné."

        entry = self.trame[index]
        depth = entry["depth"]

        if depth >= self.MAX_DEPTH:
            return f"Profondeur maximale atteinte ({self.MAX_DEPTH} niveaux)."

        # Frère précédent : le dernier bloc de même niveau avant celui-ci,
        # en sautant tout ce qui est imbriqué plus profond.
        sibling = None
        for i in range(index - 1, -1, -1):
            if self.trame[i]["depth"] < depth:
                break
            if self.trame[i]["depth"] == depth:
                sibling = self.trame[i]
                break

        if sibling is None:
            return "Aucun bloc au-dessus dans lequel imbriquer celui-ci."

        sibling_block = self.get_block(sibling["block_id"])
        if not sibling_block or sibling_block["kind"] != "if":
            return (
                "Seule une condition peut contenir d'autres blocs. "
                "Placez d'abord une condition juste au-dessus."
            )

        end = self._group_end(index)
        for e in self.trame[index:end]:
            e["depth"] += 1
        self.saved = False
        return ""

    def outdent_entry(self, index: int) -> str:
        """
        Sort un bloc de la condition qui le contient.

        Le bloc est déplacé *après* les frères qui le suivaient dans la
        condition : sans cela, ces frères se retrouveraient rattachés à lui
        et l'affichage annoncerait une imbrication que le moteur ignore.

        Args:
            index: Position du bloc

        Returns:
            Un message d'erreur, ou une chaîne vide si l'opération a réussi
        """
        if not 0 <= index < len(self.trame):
            return "Aucun bloc sélectionné."

        entry = self.trame[index]
        depth = entry["depth"]
        if depth == 0:
            return "Ce bloc est déjà au niveau le plus à gauche."

        end = self._group_end(index)
        group = self.trame[index:end]

        # Fin de la condition qui le contenait : tout ce qui suit et reste
        # plus profond appartient encore à cette condition.
        after = end
        while after < len(self.trame) and self.trame[after]["depth"] >= depth:
            after += 1

        for e in group:
            e["depth"] -= 1

        del self.trame[index:end]
        insert_at = after - len(group)
        self.trame[insert_at:insert_at] = group
        self.selected_index = insert_at
        self.saved = False
        return ""

    def _group_end(self, index: int) -> int:
        """
        Retourne l'index de fin du bloc et de tout son contenu.

        Args:
            index: Position du bloc

        Returns:
            Index juste après le dernier descendant
        """
        depth = self.trame[index]["depth"]
        end = index + 1
        while end < len(self.trame) and self.trame[end]["depth"] > depth:
            end += 1
        return end

    def normalize_depths(self) -> int:
        """
        Répare les profondeurs incohérentes de la trame.

        Un bloc ne peut être imbriqué que d'un niveau sous le précédent, et
        seulement si celui-ci est une condition. Toute autre profondeur est
        ramenée au maximum autorisé : l'affichage reflète alors exactement
        ce que le moteur exécutera.

        Returns:
            Nombre de blocs corrigés
        """
        fixed = 0
        allowed = 0   # profondeur maximale pour le bloc courant

        for entry in self.trame:
            depth = max(0, min(int(entry.get("depth", 0)), allowed, self.MAX_DEPTH))
            if depth != entry.get("depth"):
                entry["depth"] = depth
                fixed += 1

            block = self.get_block(entry["block_id"])
            # Seule une condition ouvre un niveau supplémentaire
            allowed = depth + 1 if (block and block["kind"] == "if") else depth

        return fixed

    def toggle_link(self, index: int) -> str:
        """
        Bascule un bloc entre enchaînement « et » et « ou ».

        Args:
            index: Position du bloc

        Returns:
            Un message d'erreur, ou une chaîne vide
        """
        if not 0 <= index < len(self.trame):
            return "Aucun bloc sélectionné."

        entry = self.trame[index]
        if index == 0:
            return "Le premier bloc n'a rien à quoi se chaîner."

        # Le « ou » n'a de sens qu'entre deux frères de même niveau
        previous = self.trame[index - 1]
        if previous["depth"] != entry["depth"]:
            sibling = None
            for e in reversed(self.trame[:index]):
                if e["depth"] == entry["depth"]:
                    sibling = e
                    break
            if sibling is None:
                return "Aucun bloc frère avant celui-ci."

        entry["link"] = "or" if entry.get("link", "and") == "and" else "and"
        self.saved = False
        return ""

    # ------------------------------------------------------------------
    # Rendu
    # ------------------------------------------------------------------

    def format_block(self, block: dict) -> str:
        """Décrit un bloc en une ligne."""
        if block is None:
            return "*bloc manquant*"
        if block["kind"] == "if":
            conditions = block.get("conditions", [])
            return "🤔 SI " + (describe_condition(conditions[0]) if conditions else "?")
        return describe_action(block.get("action", {}))

    def render_trame(self, marker: bool = True) -> str:
        """
        Rend la trame sous forme de liste indentée.

        Args:
            marker: Afficher le curseur sur le bloc sélectionné

        Returns:
            Le texte prêt pour l'embed
        """
        if not self.trame:
            return "*Trame vide — créez un bloc pour commencer.*"

        lines = []
        for i, entry in enumerate(self.trame):
            block = self.get_block(entry["block_id"])
            prefix = "-" * (entry["depth"] + 1)
            link = " *(ou)*" if entry.get("link") == "or" else ""
            cursor = "▸ " if (marker and i == self.selected_index) else ""
            lines.append(f"`{i + 1:2}.` {cursor}{prefix} {self.format_block(block)}{link}")

        text = "\n".join(lines)
        return text if len(text) <= 1000 else text[:990] + "\n…"

    def format_trigger(self, t):
        """Décrit un déclencheur du panel."""
        return describe_trigger(t.get('type'), t.get('data', {}))

    # Un menu déroulant Discord accepte 25 options ; on en réserve deux
    # pour la navigation dès qu'il y a plusieurs pages.
    MENU_PAGE_SIZE = 23

    PAGE_PREV = "__page_prev__"
    PAGE_NEXT = "__page_next__"

    def _paginate(self, items: list, key: str):
        """
        Découpe une liste pour l'afficher dans un menu déroulant.

        Args:
            items: Éléments à afficher
            key: Nom de la liste, pour mémoriser la page courante

        Returns:
            Tuple (éléments de la page, index de la page, nombre de pages)
        """
        total_pages = max(1, (len(items) - 1) // self.MENU_PAGE_SIZE + 1)
        page = max(0, min(self.pages.get(key, 0), total_pages - 1))
        self.pages[key] = page

        start = page * self.MENU_PAGE_SIZE
        return items[start:start + self.MENU_PAGE_SIZE], page, total_pages

    def _page_options(self, page: int, total_pages: int) -> list:
        """
        Construit les options de navigation d'un menu paginé.

        Elles vivent dans le menu lui-même : les lignes du panel sont trop
        rares pour y consacrer des boutons.

        Args:
            page: Page courante
            total_pages: Nombre total de pages

        Returns:
            Liste d'options à ajouter à la fin du menu
        """
        if total_pages <= 1:
            return []

        options = []
        if page > 0:
            options.append(discord.SelectOption(
                label=f"◀️ Page précédente ({page}/{total_pages})",
                value=self.PAGE_PREV,
                description="Revenir en arrière dans la liste"
            ))
        if page < total_pages - 1:
            options.append(discord.SelectOption(
                label=f"▶️ Page suivante ({page + 2}/{total_pages})",
                value=self.PAGE_NEXT,
                description="Voir la suite de la liste"
            ))
        return options

    def _handle_page_change(self, key: str, value: str) -> bool:
        """
        Traite la sélection d'une option de navigation.

        Args:
            key: Nom de la liste concernée
            value: Valeur sélectionnée

        Returns:
            True s'il s'agissait d'un changement de page
        """
        if value == self.PAGE_PREV:
            self.pages[key] = max(0, self.pages.get(key, 0) - 1)
            return True
        if value == self.PAGE_NEXT:
            self.pages[key] = self.pages.get(key, 0) + 1
            return True
        return False

    def _add_nav_bar(self, row: int) -> None:
        """
        Barre commune : retour, sections voisines et sauvegarde.

        Args:
            row: Ligne où placer la barre
        """
        self.add_item(discord.ui.Button(
            label="Retour", style=discord.ButtonStyle.secondary,
            custom_id="back", row=row
        ))

        sections = [
            ("menu_triggers", "Déclencheurs", "⚡", "triggers"),
            ("menu_blocks", "Ajouter", "🧩", "blocks"),
            ("menu_trame", "Organiser", "🧵", "trame"),
        ]
        for custom_id, label, emoji, mode in sections:
            if mode == self.mode:
                continue
            self.add_item(discord.ui.Button(
                label=label, style=discord.ButtonStyle.primary,
                custom_id=custom_id, emoji=emoji, row=row
            ))

        self.add_item(discord.ui.Button(
            label="Sauvegarder", style=discord.ButtonStyle.success,
            custom_id="save", emoji="💾", row=row,
            disabled=(not self.triggers or not self.trame)
        ))

    def update_components(self):
        """Reconstruit les composants selon la section courante."""
        self.clear_items()

        if self.mode == "main":
            self.add_item(discord.ui.Button(label="Renommer", style=discord.ButtonStyle.secondary, custom_id="set_name", emoji="✏️", row=0))
            self.add_item(discord.ui.Button(label="Déclencheurs", style=discord.ButtonStyle.primary, custom_id="menu_triggers", emoji="⚡", row=0))
            self.add_item(discord.ui.Button(label="Ajouter un bloc", style=discord.ButtonStyle.primary, custom_id="menu_blocks", emoji="🧩", row=0))
            self.add_item(discord.ui.Button(label="Organiser", style=discord.ButtonStyle.primary, custom_id="menu_trame", emoji="🧵", row=0))

            self.add_item(discord.ui.Button(
                label="Enregistré" if self.saved else "Sauvegarder",
                style=discord.ButtonStyle.success,
                custom_id="save", emoji="💾", row=1,
                disabled=(self.saved or not self.triggers or not self.trame)
            ))
            self.add_item(discord.ui.Button(
                label="Fermer", style=discord.ButtonStyle.secondary,
                custom_id="close", emoji="✖️", row=1
            ))
            if not self.saved:
                self.add_item(discord.ui.Button(
                    label="Abandonner", style=discord.ButtonStyle.danger,
                    custom_id="cancel", row=1
                ))

        elif self.mode == "triggers":
            # Chaque déclencheur du catalogue a sa propre entrée : plus de
            # bouton « Event » qui ouvre un second menu.
            page_items, page, total = self._paginate(TRIGGER_MENU, "trigger_menu")
            options = [
                discord.SelectOption(
                    label=t.label[:100],
                    value=t.key,
                    emoji=t.emoji,
                    description=t.hint[:100] or None
                )
                for t in page_items
            ] + self._page_options(page, total)

            self.add_item(discord.ui.Select(
                placeholder="➕ Ajouter un déclencheur…",
                custom_id="menu_add_trigger", options=options, row=0
            ))

            if self.triggers:
                items, page, total = self._paginate(
                    list(enumerate(self.triggers)), "triggers_list"
                )
                options = [
                    discord.SelectOption(
                        label=f"{i + 1}. {self.format_trigger(t)}"[:100],
                        value=str(i),
                        description="Sélectionner pour pouvoir le retirer"[:100],
                        default=(i == self.selected_index)
                    )
                    for i, t in items
                ] + self._page_options(page, total)

                self.add_item(discord.ui.Select(
                    placeholder="🗑️ Déclencheurs déjà ajoutés…",
                    custom_id="select_item", options=options, row=1
                ))
                self.add_item(discord.ui.Button(
                    label="Retirer celui-ci", style=discord.ButtonStyle.danger,
                    custom_id="delete_item", emoji="🗑️", row=2,
                    disabled=self.selected_index is None
                ))

            self._add_nav_bar(row=3)

        elif self.mode == "blocks":
            # Deux menus séparés : les conditions d'un côté, les actions de
            # l'autre. Chaque type a son entrée, avec sa propre explication.
            page_items, page, total = self._paginate(
                list(CONDITION_BLOCKS.values()), "condition_menu"
            )
            options = [
                discord.SelectOption(
                    label=(b.label or b.type)[:100],
                    value=b.type,
                    emoji=b.emoji,
                    description=b.hint[:100] or None
                )
                for b in page_items
            ] + self._page_options(page, total)

            self.add_item(discord.ui.Select(
                placeholder="🤔 Ajouter une condition…",
                custom_id="menu_add_condition", options=options, row=0
            ))

            page_items, page, total = self._paginate(ACTION_MENU, "action_menu")
            options = [
                discord.SelectOption(
                    label=entry.label[:100],
                    value=entry.key,
                    emoji=entry.emoji,
                    description=entry.hint[:100] or None
                )
                for entry in page_items
            ] + self._page_options(page, total)

            self.add_item(discord.ui.Select(
                placeholder="🎬 Ajouter une action…",
                custom_id="menu_add_action", options=options, row=1
            ))

            self._add_nav_bar(row=3)

        elif self.mode == "picker":
            spec = self.picker or {}
            kind = spec.get("component")

            if kind == "user":
                self.add_item(discord.ui.UserSelect(
                    placeholder="Choisir un ou plusieurs membres…",
                    custom_id="picker_select",
                    min_values=1, max_values=spec.get("max_values", 1), row=0
                ))
            elif kind == "role":
                self.add_item(discord.ui.RoleSelect(
                    placeholder="Choisir un ou plusieurs rôles…",
                    custom_id="picker_select",
                    min_values=1, max_values=spec.get("max_values", 1), row=0
                ))
            else:
                self.add_item(discord.ui.ChannelSelect(
                    placeholder="Choisir un salon…",
                    custom_id="picker_select",
                    channel_types=spec.get("channel_types") or [
                        discord.ChannelType.voice, discord.ChannelType.stage_voice
                    ],
                    min_values=1, max_values=spec.get("max_values", 1), row=0
                ))

            # Options propres au bloc en cours de construction
            if spec.get("ops") and len(spec["ops"]) > 1:
                negated = spec.get("op") == "!="
                self.add_item(discord.ui.Button(
                    label="Ne doit PAS correspondre" if negated else "Doit correspondre",
                    style=discord.ButtonStyle.danger if negated else discord.ButtonStyle.success,
                    custom_id="picker_toggle_op", emoji="🔁", row=1
                ))

            if spec.get("purpose") == "move":
                is_member = spec.get("target") == "member"
                self.add_item(discord.ui.Button(
                    label="Déplacer le membre" if is_member else "Déplacer le bot",
                    style=discord.ButtonStyle.primary,
                    custom_id="picker_toggle_target", emoji="🔁", row=1
                ))

            if spec.get("purpose") == "message":
                self.add_item(discord.ui.Button(
                    label="Salon du déclencheur", style=discord.ButtonStyle.secondary,
                    custom_id="picker_default_channel", emoji="📍", row=1
                ))

            if spec.get("purpose") == "trigger_channels":
                self.add_item(discord.ui.Button(
                    label="Partout sur le serveur", style=discord.ButtonStyle.primary,
                    custom_id="picker_default_channel", emoji="🌍", row=1
                ))

            if spec.get("purpose") != "trigger_channels":
                self.add_item(discord.ui.Button(
                    label="Saisir un ID à la place", style=discord.ButtonStyle.secondary,
                    custom_id="picker_manual", emoji="⌨️", row=2
                ))
            self.add_item(discord.ui.Button(
                label="Annuler", style=discord.ButtonStyle.danger,
                custom_id="picker_cancel", row=2
            ))

        elif self.mode == "trame":
            self.add_item(discord.ui.Button(label="Monter", style=discord.ButtonStyle.secondary, custom_id="move_up", emoji="⬆️", row=0))
            self.add_item(discord.ui.Button(label="Descendre", style=discord.ButtonStyle.secondary, custom_id="move_down", emoji="⬇️", row=0))
            self.add_item(discord.ui.Button(label="Imbriquer", style=discord.ButtonStyle.secondary, custom_id="indent", emoji="➡️", row=0))
            self.add_item(discord.ui.Button(label="Sortir", style=discord.ButtonStyle.secondary, custom_id="outdent", emoji="⬅️", row=0))
            self.add_item(discord.ui.Button(label="ET / OU", style=discord.ButtonStyle.secondary, custom_id="toggle_link", emoji="🔀", row=0))

            if self.trame:
                items, page, total = self._paginate(
                    list(enumerate(self.trame)), "trame_list"
                )
                options = []
                for i, entry in items:
                    block = self.get_block(entry["block_id"])
                    label = ("· " * entry["depth"]) + self.format_block(block)
                    description = f"Niveau {entry['depth'] + 1}"
                    if entry.get("link") == "or":
                        description += " · sinon-si"
                    options.append(discord.SelectOption(
                        label=label[:100],
                        value=str(i),
                        description=description[:100],
                        default=(i == self.selected_index)
                    ))
                options += self._page_options(page, total)

                self.add_item(discord.ui.Select(
                    placeholder="✏️ Choisir un bloc à déplacer ou supprimer…",
                    custom_id="select_item", options=options, row=1
                ))
                self.add_item(discord.ui.Button(
                    label="Supprimer ce bloc", style=discord.ButtonStyle.danger,
                    custom_id="delete_item", emoji="🗑️", row=2,
                    disabled=self.selected_index is None
                ))

            self._add_nav_bar(row=3)

    # ------------------------------------------------------------------
    # Interactions
    # ------------------------------------------------------------------

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Route les clics et les sélections du panel."""
        cid = interaction.data.get("custom_id")
        values = interaction.data.get("values") or []
        value = values[0] if values else None
        notice = ""

        # --- Navigation entre sections ---
        if cid == "back":
            if self.all_sounds:
                self.all_sounds = []
                self.mode = "blocks"
            else:
                self.mode = "main"
            self.selected_index = None
        elif cid == "menu_triggers":
            self.mode = "triggers"
            self.selected_index = None
        elif cid == "menu_blocks":
            self.mode = "blocks"
            self.selected_index = None
        elif cid == "menu_trame":
            self.mode = "trame"
            self.selected_index = None
        elif cid == "cancel":
            self.stop()
            message = (
                "❌ Modifications abandonnées." if self.routine_id
                else "❌ Création abandonnée."
            )
            await interaction.response.edit_message(
                content=message, embed=None, view=None
            )
            return False
        elif cid == "close":
            self.stop()
            embed = discord.Embed(
                title=f"{'✅' if self.saved else '📕'} {self.name}",
                description=(
                    "Routine enregistrée. Rouvrez-la avec `/routine_manage`."
                    if self.saved else
                    "Panel fermé **sans enregistrer**."
                ),
                color=discord.Color.green() if self.saved else discord.Color.greyple()
            )
            await interaction.response.edit_message(embed=embed, view=None)
            return False
        elif cid == "save":
            await self.save_routine(interaction)
            return False
        elif cid == "set_name":
            await interaction.response.send_modal(NameInputModal(self))
            return False

        # --- Menu : ajouter un déclencheur ---
        elif cid == "menu_add_trigger":
            if not self._handle_page_change("trigger_menu", value):
                entry = trigger_by_key(value)
                if entry is None:
                    notice = "Ce déclencheur n'existe plus."
                elif entry.modal:
                    opened = await self._open_form(interaction, entry.modal, entry.label)
                    if opened:
                        return False
                    notice = "Ce déclencheur est mal configuré, voir les logs."
                elif entry.trigger:
                    self.triggers.append(copy.deepcopy(entry.trigger))
                    self.saved = False

        # --- Menu : ajouter une condition ---
        elif cid == "menu_add_condition":
            if not self._handle_page_change("condition_menu", value):
                block = CONDITION_BLOCKS.get(value)
                if block is None:
                    notice = "Cette condition n'existe plus."
                elif block.picker:
                    # Sélection native : pas d'identifiant à recopier
                    self.picker = {
                        "component": block.picker,
                        "purpose": "condition",
                        "block_type": block.type,
                        "op": block.default_op,
                        "ops": block.ops,
                        "max_values": 5,
                    }
                    self.mode = "picker"
                else:
                    await interaction.response.send_modal(
                        ConditionFormModal(self, block)
                    )
                    return False

        # --- Menu : ajouter une action ---
        elif cid == "menu_add_action":
            if not self._handle_page_change("action_menu", value):
                entry = next((e for e in ACTION_MENU if e.key == value), None)

                if entry is None:
                    notice = "Cette action n'existe plus."
                elif entry.special == "sound":
                    await self._show_sound_selector(interaction)
                    return False
                elif entry.special == "move":
                    self.picker = {
                        "component": "channel",
                        "purpose": "move",
                        "target": "bot",
                    }
                    self.mode = "picker"
                elif entry.modal:
                    opened = await self._open_form(interaction, entry.modal, entry.label)
                    if opened:
                        return False
                    notice = "Cette action est mal configurée, voir les logs."
                elif entry.payload:
                    self.add_action_block(copy.deepcopy(entry.payload))
                    self.mode = "trame"

        # --- Sélection native de membre, salon ou rôle ---
        elif cid == "picker_select":
            ids = ",".join(str(v) for v in values)
            notice = self._apply_picker(ids)

        elif cid == "picker_toggle_op":
            spec = self.picker or {}
            spec["op"] = "!=" if spec.get("op") == "==" else "=="

        elif cid == "picker_toggle_target":
            spec = self.picker or {}
            spec["target"] = "bot" if spec.get("target") == "member" else "member"

        elif cid == "picker_default_channel":
            notice = self._apply_picker(None)

        elif cid == "picker_manual":
            block_type = (self.picker or {}).get("block_type")
            block = CONDITION_BLOCKS.get(block_type)
            if block is not None:
                await interaction.response.send_modal(ConditionFormModal(self, block))
                self.picker = None
                self.mode = "trame"
                return False
            await interaction.response.send_modal(ManualIdModal(self))
            return False

        elif cid == "picker_cancel":
            purpose = (self.picker or {}).get("purpose")
            self.picker = None
            self.mode = "triggers" if purpose == "trigger_channels" else "blocks"

        # --- Sélecteur de sons ---
        elif cid == "quick_select_sound":
            if value and value != "none":
                self.add_action_block({
                    "type": "play_sound",
                    "sound_name": value,
                    "target_strategy": "active"
                })
            self.all_sounds = []
            self.mode = "trame"

        elif cid in ("sound_page_prev", "sound_page_next"):
            self.sound_page += -1 if cid == "sound_page_prev" else 1
            await self._show_sound_selector(interaction)
            return False

        # --- Sélection dans une liste existante ---
        elif cid == "select_item":
            list_key = "triggers_list" if self.mode == "triggers" else "trame_list"
            if not self._handle_page_change(list_key, value):
                self.selected_index = int(value)

        elif cid == "delete_item":
            if self.selected_index is None:
                notice = "Choisissez d'abord un élément dans la liste."
            elif self.mode == "triggers":
                if self.selected_index < len(self.triggers):
                    self.triggers.pop(self.selected_index)
                self.selected_index = None
                self.saved = False
            else:
                self.delete_trame_entry(self.selected_index)

        # --- Réorganisation de la trame ---
        elif cid in ("move_up", "move_down"):
            if self.selected_index is None:
                notice = "Choisissez d'abord un bloc dans la liste."
            else:
                self.move_entry(self.selected_index, -1 if cid == "move_up" else 1)

        elif cid == "indent":
            notice = (
                "Choisissez d'abord un bloc dans la liste."
                if self.selected_index is None
                else self.indent_entry(self.selected_index)
            )

        elif cid == "outdent":
            notice = (
                "Choisissez d'abord un bloc dans la liste."
                if self.selected_index is None
                else self.outdent_entry(self.selected_index)
            )

        elif cid == "toggle_link":
            notice = (
                "Choisissez d'abord un bloc dans la liste."
                if self.selected_index is None
                else self.toggle_link(self.selected_index)
            )

        self.update_components()
        await self.refresh_embed(interaction, notice)
        return False

    def _apply_picker(self, ids) -> str:
        """
        Construit le bloc à partir de la sélection native.

        Args:
            ids: Identifiants choisis, séparés par des virgules, ou None
                pour « salon du déclencheur »

        Returns:
            Un message d'avertissement, ou une chaîne vide
        """
        spec = self.picker or {}
        purpose = spec.get("purpose")

        if purpose == "condition":
            self.add_condition_block({
                "type": spec["block_type"],
                "value": ids,
                "op": spec.get("op", "=="),
            })

        elif purpose == "message":
            self.add_action_block({
                "type": "message",
                "content": spec.get("content", ""),
                "channel_id": (ids or "").split(",")[0] or None,
            })

        elif purpose == "trigger_channels":
            data = dict(spec.get("trigger") or {})
            if ids:
                data["channels"] = ids.split(",")
            self.triggers.append({"type": "event", "data": data})
            self.saved = False
            self.picker = None
            self.mode = "triggers"
            return ""

        elif purpose == "move":
            if not ids:
                return "Choisissez un salon de destination."
            self.add_action_block({
                "type": "move",
                "target": spec.get("target", "bot"),
                "channel_id": ids.split(",")[0],
            })

        else:
            return "Sélection sans destination, annulée."

        self.picker = None
        self.mode = "trame"
        return ""

    async def _open_form(
        self,
        interaction: discord.Interaction,
        modal_name: str,
        label: str
    ) -> bool:
        """
        Ouvre le formulaire déclaré par une entrée de catalogue.

        Args:
            interaction: Interaction en cours
            modal_name: Nom de la classe de formulaire
            label: Libellé de l'entrée, pour le message d'erreur

        Returns:
            True si le formulaire a bien été ouvert
        """
        modal_cls = globals().get(modal_name)
        if modal_cls is None:
            logger.error(
                f"« {label} » déclare le formulaire '{modal_name}', "
                "qui n'existe pas dans bot.py."
            )
            return False

        # Le plafond de volume dépend du serveur : il est lu avant l'ouverture
        if modal_name == "VolumeInputModal":
            ceiling = await self.bot.player_manager.get_player(
                self.guild_id
            ).get_max_volume()
            await interaction.response.send_modal(modal_cls(self, ceiling))
        else:
            await interaction.response.send_modal(modal_cls(self))
        return True

    # ------------------------------------------------------------------
    # Sélecteur de sons
    # ------------------------------------------------------------------

    async def _show_sound_selector(self, interaction: discord.Interaction):
        """Affiche le sélecteur de sons paginé."""
        if not self.all_sounds:
            sounds = await self.db.get_available_sounds(str(self.guild_id))
            self.all_sounds = sorted(sounds.keys())
            self.sound_page = 0

        if not self.all_sounds:
            self.mode = "blocks"
            self.update_components()
            await interaction.response.send_message(
                "🔇 Aucun son disponible. Ajoutez-en avec `/add_sound`, "
                "ou importez les fichiers existants avec `/sync`.",
                ephemeral=True
            )
            return

        total_pages = (len(self.all_sounds) - 1) // self.sounds_per_page + 1
        self.sound_page = max(0, min(self.sound_page, total_pages - 1))
        start_idx = self.sound_page * self.sounds_per_page
        page_sounds = self.all_sounds[start_idx:start_idx + self.sounds_per_page]

        options = []
        if self.sound_page == 0:
            options.append(discord.SelectOption(
                label="Random 🔥", value="__random__", emoji="🎲"
            ))
        options.extend(
            discord.SelectOption(label=name[:100], value=name[:100])
            for name in page_sounds
        )

        self.clear_items()
        self.add_item(discord.ui.Select(
            placeholder=f"Choisir un son (page {self.sound_page + 1}/{total_pages})",
            custom_id="quick_select_sound", options=options, row=0
        ))

        if total_pages > 1:
            self.add_item(discord.ui.Button(
                label="◀️ Précédent", style=discord.ButtonStyle.secondary,
                custom_id="sound_page_prev", disabled=self.sound_page == 0, row=1
            ))
            self.add_item(discord.ui.Button(
                label=f"Page {self.sound_page + 1}/{total_pages}",
                style=discord.ButtonStyle.secondary,
                custom_id="sound_page_info", disabled=True, row=1
            ))
            self.add_item(discord.ui.Button(
                label="Suivant ▶️", style=discord.ButtonStyle.secondary,
                custom_id="sound_page_next",
                disabled=self.sound_page >= total_pages - 1, row=1
            ))

        self.add_item(discord.ui.Button(
            label="Annuler", style=discord.ButtonStyle.danger, custom_id="back", row=2
        ))

        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)

    # ------------------------------------------------------------------
    # Affichage et erreurs
    # ------------------------------------------------------------------

    async def refresh_embed(self, interaction: discord.Interaction, notice: str = ""):
        """
        Met à jour l'embed du panel.

        Args:
            interaction: Interaction en cours
            notice: Message d'avertissement à afficher en bas
        """
        embed = discord.Embed(
            title=f"{'✅' if self.saved else '🛠️'} {self.name}",
            color=discord.Color.green() if self.saved else discord.Color.blue()
        )

        triggers = (
            "\n".join(f"`{i + 1}.` {self.format_trigger(t)}" for i, t in enumerate(self.triggers))
            or "*Aucun déclencheur — la routine ne partira jamais.*"
        )
        embed.add_field(
            name=f"⚡ Se déclenche quand… ({len(self.triggers)})",
            value=triggers + (
                "\n*N'importe lequel suffit à lancer la trame.*"
                if len(self.triggers) > 1 else ""
            ),
            inline=False
        )

        embed.add_field(
            name=f"🧵 Puis exécute ({len(self.trame)} bloc(s))",
            value=self.render_trame(),
            inline=False
        )

        if self.mode == "trame":
            embed.add_field(
                name="ℹ️ Organiser la trame",
                value=(
                    "Choisissez un bloc dans la liste, puis déplacez-le.\n"
                    "**➡️ Imbriquer** le place *à l'intérieur* de la condition "
                    "juste au-dessus : il ne s'exécutera que si elle est vraie.\n"
                    "**🔀 ET/OU** : un bloc en *ou* n'est tenté que si le "
                    "précédent n'a **pas** été exécuté."
                ),
                inline=False
            )
        elif self.mode == "blocks":
            embed.add_field(
                name="ℹ️ Ajouter un bloc",
                value=(
                    "Le bloc choisi se pose à la fin de la trame. S'il suit "
                    "une condition, il est imbriqué dedans automatiquement.\n"
                    "Utilisez **🧵 Organiser** pour le déplacer ensuite."
                ),
                inline=False
            )
        elif self.mode == "triggers":
            embed.add_field(
                name="ℹ️ Déclencheurs",
                value=(
                    "Ajoutez-en autant que vous voulez : la routine part dès "
                    "que **l'un d'eux** se produit."
                ),
                inline=False
            )

        if notice:
            embed.add_field(name="⚠️", value=notice, inline=False)

        if not self.triggers or not self.trame:
            embed.set_footer(
                text="Il faut au moins un déclencheur et un bloc pour sauvegarder."
            )
        elif self.saved:
            embed.set_footer(text="Enregistrée. Continuez à modifier si besoin.")
        else:
            embed.set_footer(text="Modifications non enregistrées.")

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item
    ) -> None:
        """Rattrape toute erreur d'un composant et restaure un panel cohérent."""
        logger.error(
            f"Erreur dans le panel de routine (item={getattr(item, 'label', item)}): {error}",
            exc_info=error
        )

        try:
            self.mode = "main"
            self.selected_index = None
            self.all_sounds = []
            self.sound_page = 0
            self.update_components()
        except Exception:
            logger.exception("Restauration du panel impossible")

        message = (
            "❌ Une erreur est survenue dans le panel. Il a été réinitialisé, "
            "votre routine en cours est conservée."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------
    # Sauvegarde
    # ------------------------------------------------------------------

    async def save_routine(self, interaction: discord.Interaction):
        """
        Enregistre la routine sans fermer le panel.

        L'embed passe au vert et l'édition reste possible : toute
        modification ultérieure le remet au bleu et réactive la sauvegarde.
        """
        if not self.triggers or not self.trame:
            await interaction.response.send_message(
                "❌ Une routine a besoin d'au moins un déclencheur et un bloc.",
                ephemeral=True
            )
            return

        trigger_data = {"triggers": self.triggers}
        flat = self.build_flat_trame()

        if self.routine_id:
            await self.db.update_routine(
                self.routine_id, self.name, "v2", trigger_data,
                flat, None, str(self.guild_id)
            )
            notice = "Routine mise à jour."
        else:
            # L'identifiant est conservé : une deuxième sauvegarde met à
            # jour la routine au lieu d'en créer une copie.
            self.routine_id = await self.db.add_routine(
                str(self.guild_id), self.name, "v2", trigger_data, flat, None
            )
            notice = "Routine créée."

        await self.bot.routine_manager.load_routines()

        self.saved = True
        self.mode = "main"
        self.update_components()
        await self.refresh_embed(interaction, notice)

class TimeInputModal(discord.ui.Modal, title="Ajouter Timer"):
    duration = discord.ui.TextInput(
        label="Intervalle, ou plage aléatoire",
        placeholder="5m · 1h30m · 10m-20m pour un intervalle au hasard"
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

        if low <= 0:
            await interaction.response.send_message(
                "❌ L'intervalle doit être supérieur à 0.", ephemeral=True)
            return

        # Une plage fait tirer un nouvel intervalle après chaque déclenchement
        data = (
            {"interval_min": low, "interval_max": high}
            if high > low else
            {"interval_seconds": low}
        )
        self.view.triggers.append({"type": "timer", "data": data})
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


class KeywordTriggerModal(discord.ui.Modal, title="Déclencheur : mot-clé"):
    """Déclenche la routine quand un message contient un mot."""

    keyword = discord.ui.TextInput(
        label="Mot ou expression à repérer",
        placeholder="bonjour",
        max_length=100
    )

    def __init__(self, view):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        value = self.keyword.value.strip()
        if not value:
            await interaction.response.send_message(
                "❌ Il faut indiquer un mot-clé.", ephemeral=True)
            return

        # Étape suivante : limiter à certains salons, ou non
        self.view.picker = {
            "component": "channel",
            "purpose": "trigger_channels",
            "trigger": {"event": "message", "keyword": value},
            "channel_types": [discord.ChannelType.text, discord.ChannelType.news],
            "max_values": 10,
        }
        self.view.mode = "picker"
        self.view.update_components()
        await self.view.refresh_embed(interaction)

        if not Config.MESSAGE_CONTENT_INTENT:
            await interaction.followup.send(
                "⚠️ Déclencheur ajouté, mais l'intent « Message Content » est "
                "désactivé : il ne se déclenchera pas tant que "
                "MESSAGE_CONTENT_INTENT=true n'est pas défini et l'intent "
                "coché dans le portail développeur Discord.",
                ephemeral=True
            )


class ReactionTriggerModal(discord.ui.Modal, title="Déclencheur : réaction"):
    """Déclenche la routine quand quelqu'un ajoute une réaction."""

    emoji = discord.ui.TextInput(
        label="Émoji de la réaction",
        placeholder="🎉",
        max_length=100
    )

    def __init__(self, view):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        value = self.emoji.value.strip()
        if not value:
            await interaction.response.send_message(
                "❌ Il faut indiquer un émoji.", ephemeral=True)
            return

        self.view.picker = {
            "component": "channel",
            "purpose": "trigger_channels",
            "trigger": {"event": "reaction", "emoji": value},
            "channel_types": [
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.voice,
            ],
            "max_values": 10,
        }
        self.view.mode = "picker"
        self.view.update_components()
        await self.view.refresh_embed(interaction)


class ManualIdModal(discord.ui.Modal, title="Saisir un identifiant"):
    """
    Repli quand la sélection native ne convient pas.

    Utile pour viser un salon d'un autre serveur, ou un membre absent de
    la liste proposée par Discord.
    """

    identifiant = discord.ui.TextInput(
        label="Identifiant",
        placeholder="123456789012345678",
        max_length=200
    )

    def __init__(self, view):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.identifiant.value.strip().strip("<>#@&")
        ids = [part.strip() for part in raw.split(",") if part.strip()]

        if not ids or not all(part.isdigit() for part in ids):
            await interaction.response.send_message(
                "❌ Indiquez un identifiant numérique, ou plusieurs séparés "
                "par des virgules.",
                ephemeral=True
            )
            return

        notice = self.view._apply_picker(",".join(ids))
        self.view.update_components()
        await self.view.refresh_embed(interaction, notice)


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
            self.view.add_action_block({"type": "wait", "delay_min": low, "delay_max": high})
        else:
            self.view.add_action_block({"type": "wait", "delay": low})

        self.view.mode = "trame"
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

        self.view.add_action_block({"type": "chance", "percent": value})
        self.view.mode = "trame"
        self.view.update_components()
        await self.view.refresh_embed(interaction)

class VolumeInputModal(discord.ui.Modal, title="Changer le volume"):
    def __init__(self, view, max_volume: int = None):
        """
        Args:
            view: Panel appelant
            max_volume: Plafond du serveur, lu avant l'ouverture de la modale
                (une modale ne peut pas interroger la base elle-même).
        """
        super().__init__()
        self.view = view
        self.max_volume = (
            Config.DEFAULT_MAX_VOLUME if max_volume is None else int(max_volume)
        )

        # Champ construit ici pour que le libellé reflète le plafond réel
        # du serveur, sans passer par un setter déprécié.
        self.value = discord.ui.TextInput(
            label=f"Volume 0-{self.max_volume}, ou 'reset'",
            placeholder="150"
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.value.value.strip().lower()

        if raw != "reset":
            if not raw.isdigit() or not 0 <= int(raw) <= self.max_volume:
                await interaction.response.send_message(
                    f"❌ Indiquez un entier entre 0 et {self.max_volume} "
                    "(volume maximum du serveur), ou « reset ». "
                    "Ce plafond se règle avec `/config`.",
                    ephemeral=True
                )
                return
            raw = int(raw)

        self.view.add_action_block({"type": "volume", "value": raw})
        self.view.mode = "trame"
        self.view.update_components()
        await self.view.refresh_embed(interaction)


class ConditionFormModal(discord.ui.Modal):
    """
    Formulaire d'une condition précise.

    Les libellés, l'exemple et les opérateurs autorisés viennent du
    catalogue : le formulaire s'adapte à la condition choisie au lieu de
    demander un type que l'utilisateur devrait deviner.
    """

    def __init__(self, view, block):
        super().__init__(title=f"Condition : {block.label or block.type}"[:45])
        self.view = view
        self.block = block

        self.value = discord.ui.TextInput(
            label=block.value_label[:45],
            placeholder=block.value_placeholder[:100],
            max_length=200
        )
        self.add_item(self.value)

        # Le choix de l'opérateur n'a de sens que s'il y en a plusieurs
        self.operator = None
        if len(block.ops) > 1:
            self.operator = discord.ui.TextInput(
                label=f"Comparaison ({' '.join(block.ops)})",
                placeholder=block.default_op,
                default=block.default_op,
                required=False,
                max_length=2
            )
            self.add_item(self.operator)

    async def on_submit(self, interaction: discord.Interaction):
        value = self.value.value.strip()
        if not value:
            await interaction.response.send_message(
                "❌ Il faut indiquer une valeur.", ephemeral=True)
            return

        op = self.block.default_op
        if self.operator is not None and self.operator.value.strip():
            op = self.operator.value.strip()

        if op not in self.block.ops:
            await interaction.response.send_message(
                f"❌ Comparaison « {op} » impossible pour cette condition. "
                f"Utilisez : {', '.join(self.block.ops)}",
                ephemeral=True
            )
            return

        self.view.add_condition_block({
            "type": self.block.type,
            "value": value,
            "op": op
        })
        self.view.mode = "trame"
        self.view.update_components()
        await self.view.refresh_embed(interaction)





















class MessageInputModal(discord.ui.Modal, title="Envoyer un message"):
    """Saisie du texte ; le salon est choisi ensuite dans une liste."""

    content = discord.ui.TextInput(
        label="Texte du message",
        placeholder="Salut {user} !",
        style=discord.TextStyle.paragraph,
        max_length=1500
    )
    prive = discord.ui.TextInput(
        label="En message privé ? (oui / non)",
        placeholder="non",
        default="non",
        required=False,
        max_length=5
    )

    def __init__(self, view):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        content = self.content.value.strip()
        if not content:
            await interaction.response.send_message(
                "❌ Le message ne peut pas être vide.", ephemeral=True)
            return

        if (self.prive.value or "non").strip().lower().startswith(("o", "y", "1")):
            self.view.add_action_block({"type": "dm", "content": content})
            self.view.mode = "trame"
            self.view.update_components()
            await self.view.refresh_embed(interaction)
            return

        # Le salon se choisit dans une liste, pas en recopiant un ID
        self.view.picker = {
            "component": "channel",
            "purpose": "message",
            "content": content,
            "channel_types": [
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.voice,
            ],
        }
        self.view.mode = "picker"
        self.view.update_components()
        await self.view.refresh_embed(interaction)


class NameInputModal(discord.ui.Modal, title="Nommer la routine"):
    name = discord.ui.TextInput(label="Nom", placeholder="Ma Super Routine")
    def __init__(self, view):
        super().__init__()
        self.view = view
    async def on_submit(self, interaction: discord.Interaction):
        self.view.name = self.name.value
        self.view.saved = False
        self.view.mode = "main"
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
        trigger_data, trame = bot.routine_manager.parse_routine_string(command)
        
        await db.add_routine(
            str(interaction.guild_id),
            name,
            "v2",
            trigger_data,
            trame,
            None
        )
        await bot.routine_manager.load_routines()
        
        triggers = trigger_data["triggers"]
        embed = discord.Embed(title="✅ Routine créée", color=discord.Color.green())
        embed.add_field(name="Nom", value=name, inline=False)
        embed.add_field(
            name=f"⚡ Déclencheurs ({len(triggers)})",
            value="\n".join(
                f"• {describe_trigger(t['type'], t['data'])}" for t in triggers
            ),
            inline=False
        )
        embed.add_field(name="🧵 Trame", value=render_flat_trame(trame), inline=False)
        
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