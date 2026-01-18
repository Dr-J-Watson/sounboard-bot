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
import logging
import os
import sys
from typing import Optional, List

# Import des modules locaux
from config import Config
from database import DatabaseManager
from audio_manager import AudioManager
from player import PlayerManager
from routine_manager import RoutineManager

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
        self.player_manager = PlayerManager(self, Config.VOICE_TIMEOUT_SECONDS)
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
        Détecte aussi quand le bot se retrouve seul dans un salon.
        """
        # Vérifier si le bot se retrouve seul dans un salon
        await self._check_bot_alone(member, before)
        
        # Transmettre aux routines
        await self.routine_manager.on_voice_state_update(member, before, after)

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
            
            # Arrêter le player de ce serveur
            guild_id = str(member.guild.id)
            if guild_id in self.player_manager.players:
                player = self.player_manager.players[guild_id]
                player.stop()  # Arrête la lecture et vide la queue
            
            # Déconnecter immédiatement
            await voice_client.disconnect(force=True)

    async def close(self) -> None:
        """Nettoyage lors de l'arrêt du bot."""
        logger.info("🛑 Arrêt du bot...")
        
        # Arrêter les routines
        await self.routine_manager.stop()
        
        # Déconnecter tous les players
        await self.player_manager.disconnect_all()
        
        await super().close()


# === Instance du bot ===
bot = SoundboardBot()


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
            "`/stop` : Arrête la lecture\n"
            "`/skip` : Passe au son suivant\n"
            "`/list_sounds` : Liste les sons disponibles\n"
            "`/add_sound <fichier> [nom]` : Ajoute un son"
        ),
        inline=False
    )
    
    # Commandes Admin
    embed.add_field(
        name="⚙️ Administration",
        value=(
            "`/config <setting> <value>` : Configure les limites\n"
            "`/delete_sound <nom>` : Supprime un son\n"
            "`/sync` : Synchronise les fichiers du disque"
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
    
    # Syntaxe Routine
    routine_help = (
        "**Syntaxe :** `<trigger> [if <conditions>] do <actions>`\n\n"
        "**Triggers (Déclencheurs) :**\n"
        "• `timer 30s` / `5m` / `1h`\n"
        "• `on join` / `leave` / `move`\n\n"
        "**Conditions (Optionnel) :**\n"
        "• `user=ID` • `channel=ID`\n"
        "• `role=ID` • `time=18:00-23:00`\n"
        "*(Séparer par `and`)*\n\n"
        "**Actions :**\n"
        "• `play <nom_son>`\n"
        "• `wait <durée>`\n"
        "*(Séparer par `then`)*\n\n"
        "**Exemple :**\n"
        "`timer 10m do play alerte`\n"
        "`on join if user=123 do wait 2s then play bienvenue`"
    )
    embed.add_field(name="📝 Syntaxe des Routines", value=routine_help, inline=False)
    
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
    
    # Message de confirmation
    if position == 1:
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
            f"{i+1}. `{item['name']}` (par {item['requester']})"
            for i, item in enumerate(info['queue'][:10])
        ])
        if len(info['queue']) > 10:
            queue_text += f"\n... et {len(info['queue']) - 10} autre(s)"
        embed.add_field(name="📝 En attente", value=queue_text, inline=False)
    else:
        embed.add_field(name="📝 En attente", value="*File vide*", inline=False)
    
    embed.set_footer(text=f"Connecté: {'Oui' if info['is_connected'] else 'Non'}")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

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
        if r['trigger_type'] == 'timer':
            interval = r['trigger_data'].get('interval_minutes', 0)
            if interval == 0:
                interval = f"{r['trigger_data'].get('interval_seconds', 0)}s"
            else:
                interval = f"{interval}m"
            trigger_desc = f"⏰ Timer ({interval})"
        else:
            event_name = r['trigger_data'].get('event', '?')
            trigger_desc = f"⚡ {event_name.replace('voice_', '')}"
        
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

    deleted = await db.delete_routine(routine_id)
    
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

    new_state = await db.toggle_routine(routine_id)
    
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
        
        # Cancel button
        cancel_btn = discord.ui.Button(
            label="Annuler",
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
    
    async def cancel_callback(self, interaction: discord.Interaction):
        """Annule la sélection."""
        await interaction.response.edit_message(content="❌ Sélection annulée.", embed=None, view=None)
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
        
        new_state = await self.db.toggle_routine(self.selected_routine_id)
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
        
        await self.db.delete_routine(self.selected_routine_id)
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
                if t_type == 'timer':
                    interval = t_data.get('interval_minutes', 0)
                    if interval == 0:
                         interval = f"{t_data.get('interval_seconds')} sec"
                    else:
                         interval = f"{interval} min"
                    embed.add_field(name="Trigger", value=f"⏰ Timer ({interval})", inline=True)
                else:
                    embed.add_field(name="Trigger", value=f"⚡ Event ({t_data.get('event')})", inline=True)
                
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
            self.add_item(discord.ui.Button(label="Ajouter Timer", style=discord.ButtonStyle.success, custom_id="add_timer", emoji="⏰", row=0))
            self.add_item(discord.ui.Button(label="Ajouter Event", style=discord.ButtonStyle.success, custom_id="add_event", emoji="📥", row=0))
            
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
            return f"Timer: {t['data'].get('interval_seconds')}s"
        elif t['type'] == 'event':
            return f"Event: {t['data'].get('event')}"
        return "Inconnu"

    def format_condition(self, c):
        return f"{c['type']} {c['op']} {c['value']}"

    def format_action(self, a):
        if a['type'] == 'play_sound':
            if a['sound_name'] == '__random__':
                return "🎲 Joue: Random 🔥"
            return f"Joue: {a['sound_name']}"
        if a['type'] == 'wait': return f"Pause: {a['delay']}s"
        if a['type'] == 'message': return f"Msg: {a['content']}"
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
            elif cid == "add_event":
                # Quick select for event
                self.add_item(discord.ui.Select(placeholder="Choisir l'événement", custom_id="quick_select_event", options=[
                    discord.SelectOption(label="Join Vocal", value="voice_join"),
                    discord.SelectOption(label="Leave Vocal", value="voice_leave")
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
                final_conditions
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
    duration = discord.ui.TextInput(label="Durée (ex: 10s, 5m)", placeholder="10s")
    def __init__(self, view):
        super().__init__()
        self.view = view
    async def on_submit(self, interaction: discord.Interaction):
        val = self.duration.value.strip()
        seconds = 0
        if val.endswith("s"): seconds = int(val[:-1])
        elif val.endswith("m"): seconds = int(val[:-1]) * 60
        elif val.isdigit(): seconds = int(val)
        if seconds > 0:
            self.view.triggers.append({"type": "timer", "data": {"interval_seconds": seconds}})
            self.view.update_components()
            await self.view.refresh_embed(interaction)

class ConditionInputModal(discord.ui.Modal, title="Ajouter Condition"):
    c_type = discord.ui.TextInput(label="Type (user, channel, role, time, date)", placeholder="user")
    value = discord.ui.TextInput(label="Valeur (ID, HH:MM-HH:MM, JJ:MM-JJ:MM)", placeholder="123456789")
    op = discord.ui.TextInput(label="Opérateur (==, !=)", placeholder="==", required=False, default="==")

    def __init__(self, view):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        t = self.c_type.value.lower().strip()
        v = self.value.value.strip()
        o = self.op.value.strip()
        
        valid_types = {"user": "user_id", "channel": "channel_id", "role": "role_id", "time": "time_range", "date": "date_range"}
        if t in valid_types:
            self.view.conditions.append({"type": valid_types[t], "value": v, "op": o})
            self.view.update_components()
            await self.view.refresh_embed(interaction)
        else:
            await interaction.response.send_message(f"Type invalide. Utilisez: {', '.join(valid_types.keys())}", ephemeral=True)

class WaitInputModal(discord.ui.Modal, title="Ajouter Pause"):
    duration = discord.ui.TextInput(label="Durée (secondes)", placeholder="5")
    def __init__(self, view):
        super().__init__()
        self.view = view
    async def on_submit(self, interaction: discord.Interaction):
        if self.duration.value.isdigit():
            self.view.actions.append({"type": "wait", "delay": int(self.duration.value)})
            self.view.update_components()
            await self.view.refresh_embed(interaction)

class MessageInputModal(discord.ui.Modal, title="Ajouter Message"):
    content = discord.ui.TextInput(label="Message", placeholder="Coucou {user}!")
    channel_id = discord.ui.TextInput(label="ID Salon (Optionnel)", required=False, placeholder="Laisser vide pour salon courant")
    def __init__(self, view):
        super().__init__()
        self.view = view
    async def on_submit(self, interaction: discord.Interaction):
        cid = self.channel_id.value.strip() if self.channel_id.value else None
        self.view.actions.append({"type": "message", "content": self.content.value, "channel_id": cid})
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
        trigger_desc = f"Timer {trigger_data.get('interval_seconds', trigger_data.get('interval_minutes', 0)*60)}s" if trigger_type == "timer" else f"Event {trigger_data.get('event')}"
        actions_desc = ", ".join([a.get('sound_name', f"wait {a.get('delay')}s") if a['type'] != 'wait' else f"wait {a.get('delay')}s" for a in actions])
        
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
    bot.run(Config.DISCORD_TOKEN)
