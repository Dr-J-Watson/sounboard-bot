import discord
from discord import app_commands
from discord.ext import commands
import logging
import os
import sys
from typing import Optional

# Import des modules locaux
from config import Config
from database import DatabaseManager
from audio_manager import AudioManager
from player import PlayerManager
from routine_manager import RoutineManager

# Configuration du logging simple
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SoundboardBot")

# Validation de la configuration
try:
    Config.validate()
except ValueError as e:
    logger.critical(f"Erreur de configuration: {e}")
    sys.exit(1)

# Initialisation des composants
db = DatabaseManager(Config.DB_FILE)
audio_manager = AudioManager(db)

# Configuration des intents
intents = discord.Intents.default()
intents.voice_states = True # Required for voice routines

class SoundboardBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.player_manager = PlayerManager(self, Config.VOICE_TIMEOUT_SECONDS)
        self.routine_manager = RoutineManager(self, db)

    async def setup_hook(self):
        await db.init_db()
        await self.routine_manager.load_routines()
        
        # Sync global sounds
        global_path = os.path.join(Config.SOUNDS_DIR, "global")
        if os.path.exists(global_path):
            await db.sync_with_folder("global", global_path)
        else:
            os.makedirs(global_path, exist_ok=True)

        # Sync all guilds found in sounds dir
        if os.path.exists(Config.SOUNDS_DIR):
            for guild_id in os.listdir(Config.SOUNDS_DIR):
                if guild_id == "global": continue
                guild_path = os.path.join(Config.SOUNDS_DIR, guild_id)
                if os.path.isdir(guild_path):
                    await db.sync_with_folder(guild_id, guild_path)
        
        await self.tree.sync()
        logger.info("Commandes slash synchronisées.")

    async def on_ready(self):
        logger.info(f'Connecté en tant que {self.user} (ID: {self.user.id})')
        if not discord.opus.is_loaded():
            discord.opus.load_opus('libopus.so.0')
        logger.info(f'Opus loaded: {discord.opus.is_loaded()}')

    async def on_voice_state_update(self, member, before, after):
        await self.routine_manager.on_voice_state_update(member, before, after)

bot = SoundboardBot()

@bot.tree.command(name="help", description="Affiche la liste des commandes et l'aide pour les routines.")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 Aide du Soundboard", color=discord.Color.gold())
    
    # Commandes Générales
    embed.add_field(
        name="🎵 Sons",
        value=(
            "`/play <nom>` : Joue un son\n"
            "`/stop` : Arrête la lecture\n"
            "`/list_sounds` : Liste les sons disponibles\n"
            "`/add_sound <fichier> [nom]` : Ajoute un son au serveur"
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
            "`/routine_toggle <id>` : Activer/Désactiver\n"
            "`/routine_delete <id>` : Supprimer\n"
            "`/routine_cmd <nom> <commande>` : Créer une routine via commande"
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
        "• `user=ID`\n"
        "• `channel=ID`\n"
        "• `role=ID`\n"
        "• `time=18:00-23:00`\n"
        "*(Séparer par `and`)*\n\n"
        "**Actions :**\n"
        "• `play <nom_son>`\n"
        "• `wait <durée>`\n"
        "*(Séparer par `then`)*\n\n"
        "**Exemple :**\n"
        "`timer 10m do play alerte`\n"
        "`on join if user=12345 do wait 2s then play bienvenue`"
    )
    embed.add_field(name="📝 Syntaxe des Routines", value=routine_help, inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="list_sounds", description="Liste tous les sons disponibles.")
async def list_sounds(interaction: discord.Interaction):
    if not interaction.guild_id:
        await interaction.response.send_message("Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        return

    sounds = await db.get_available_sounds(str(interaction.guild_id))
    if not sounds:
        await interaction.response.send_message("Aucun son disponible.", ephemeral=True)
        return
    
    sound_list = sorted(sounds.keys())
    message = "**Sons disponibles :**\n" + ", ".join([f"`{s}`" for s in sound_list])
    if len(message) > 2000:
        message = message[:1997] + "..."
    await interaction.response.send_message(message, ephemeral=True)

async def sound_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    if not interaction.guild_id:
        return []
    sounds = await db.get_available_sounds(str(interaction.guild_id))
    return [
        app_commands.Choice(name=sound, value=sound)
        for sound in sounds.keys()
        if current.lower() in sound.lower()
    ][:25]

@bot.tree.command(name="play", description="Joue un son.")
@app_commands.describe(
    sound_name="Le nom du son à jouer",
    channel="Le salon vocal où jouer le son (optionnel)"
)
@app_commands.autocomplete(sound_name=sound_autocomplete)
async def play(interaction: discord.Interaction, sound_name: str, channel: Optional[discord.VoiceChannel] = None):
    if not interaction.guild_id:
        await interaction.response.send_message("Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        return

    target_channel = channel
    if not target_channel:
        if interaction.user.voice:
            target_channel = interaction.user.voice.channel

    if not target_channel:
        await interaction.response.send_message("Vous devez être dans un salon vocal ou spécifier un salon.", ephemeral=True)
        return

    # Check local first, then global
    sound_data = await db.get_sound(str(interaction.guild_id), sound_name)
    if not sound_data:
        sound_data = await db.get_sound("global", sound_name)

    if not sound_data:
        await interaction.response.send_message(f"Le son `{sound_name}` n'existe pas.", ephemeral=True)
        return

    # Determine correct path based on where the sound was found
    sound_guild_id = sound_data['guild_id']
    file_path = os.path.join(Config.SOUNDS_DIR, sound_guild_id, sound_data['filename'])
    
    if not os.path.exists(file_path):
        await interaction.response.send_message(f"Fichier introuvable pour `{sound_name}`.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    
    player = bot.player_manager.get_player(interaction.guild_id)
    player.add_to_queue(file_path, interaction.user.display_name, sound_name, target_channel)
    
    await interaction.followup.send(f"🎵 **{sound_name}** ajouté à la file dans {target_channel.mention}.", ephemeral=True)

@bot.tree.command(name="stop", description="Arrête la lecture.")
async def stop(interaction: discord.Interaction):
    player = bot.player_manager.get_player(interaction.guild_id)
    player.stop()
    await interaction.response.send_message("⏹️ Lecture arrêtée.", ephemeral=True)

@bot.tree.command(name="add_sound", description="Ajoute un son.")
async def add_sound(interaction: discord.Interaction, attachment: discord.Attachment, name: Optional[str] = None):
    if not interaction.guild_id:
        await interaction.response.send_message("Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    if not name:
        name = os.path.splitext(attachment.filename)[0]
    name = name.lower().replace(" ", "_")

    max_name_length = await db.get_config(str(interaction.guild_id), "max_name_length", Config.MAX_NAME_LENGTH)
    if max_name_length > 0 and len(name) > max_name_length:
        await interaction.followup.send(f"Le nom est trop long (max {max_name_length} caractères).", ephemeral=True)
        return

    if await db.get_sound(str(interaction.guild_id), name):
        await interaction.followup.send(f"Le son `{name}` existe déjà.", ephemeral=True)
        return

    try:
        saved_path = await audio_manager.save_upload(attachment, attachment.filename, str(interaction.guild_id))
        filename = os.path.basename(saved_path)
        await db.add_sound(str(interaction.guild_id), name, filename, str(interaction.user))
        await interaction.followup.send(f"✅ Son `{name}` ajouté !", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Erreur: {e}", ephemeral=True)

@bot.tree.command(name="delete_sound", description="Supprime un son (Admin uniquement).")
@app_commands.describe(sound_name="Le nom du son à supprimer")
@app_commands.autocomplete(sound_name=sound_autocomplete)
async def delete_sound(interaction: discord.Interaction, sound_name: str):
    if not interaction.guild_id:
        await interaction.response.send_message("Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("🚫 Vous devez être administrateur pour supprimer un son.", ephemeral=True)
        return

    sound_data = await db.get_sound(str(interaction.guild_id), sound_name)
    if not sound_data:
        await interaction.response.send_message(f"Le son `{sound_name}` n'existe pas.", ephemeral=True)
        return

    # Delete file
    file_path = os.path.join(Config.SOUNDS_DIR, str(interaction.guild_id), sound_data['filename'])
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception as e:
            await interaction.response.send_message(f"Erreur lors de la suppression du fichier : {e}", ephemeral=True)
            return

    # Delete from DB
    await db.remove_sound(str(interaction.guild_id), sound_name)
    
    await interaction.response.send_message(f"✅ Le son `{sound_name}` a été supprimé.", ephemeral=True)

@bot.tree.command(name="config", description="Configure les paramètres du bot (Admin uniquement).")
@app_commands.describe(
    setting="Le paramètre à modifier",
    value="La nouvelle valeur"
)
@app_commands.choices(setting=[
    app_commands.Choice(name="Durée max (secondes)", value="max_duration"),
    app_commands.Choice(name="Taille max (Mo)", value="max_file_size_mb"),
    app_commands.Choice(name="Longueur nom max", value="max_name_length")
])
async def config(interaction: discord.Interaction, setting: str, value: int):
    if not interaction.guild_id:
        await interaction.response.send_message("Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.administrator:
         await interaction.response.send_message("🚫 Vous devez être administrateur pour modifier la configuration.", ephemeral=True)
         return

    if value < 0:
        await interaction.response.send_message("🚫 La valeur doit être positive ou nulle (0 pour désactiver).", ephemeral=True)
        return

    await db.set_config(str(interaction.guild_id), setting, value)
    if value == 0:
        await interaction.response.send_message(f"✅ Configuration mise à jour : `{setting}` = `Désactivé (Illimité)`", ephemeral=True)
    else:
        await interaction.response.send_message(f"✅ Configuration mise à jour : `{setting}` = `{value}`", ephemeral=True)

@bot.tree.command(name="sync", description="Synchronise la base de données avec les fichiers du dossier (Admin).")
async def sync(interaction: discord.Interaction):
    if not interaction.guild_id:
        await interaction.response.send_message("Commande serveur uniquement.", ephemeral=True)
        return
    
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Réservé aux administrateurs.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    
    guild_id = str(interaction.guild_id)
    guild_dir = os.path.join(Config.SOUNDS_DIR, guild_id)
    
    await db.sync_with_folder(guild_id, guild_dir)
    await interaction.followup.send("✅ Synchronisation terminée. Les fichiers présents sur le disque ont été ajoutés.", ephemeral=True)

# --- Routines Commands ---

@bot.tree.command(name="routine_list", description="Liste les routines configurées.")
async def routine_list(interaction: discord.Interaction):
    if not interaction.guild_id:
        await interaction.response.send_message("Commande serveur uniquement.", ephemeral=True)
        return
    
    routines = await db.get_routines(str(interaction.guild_id))
    if not routines:
        await interaction.response.send_message("Aucune routine configurée.", ephemeral=True)
        return
    
    embed = discord.Embed(title="Routines", color=discord.Color.purple())
    for r in routines:
        status = "✅" if r['active'] else "❌"
        desc = f"Type: {r['trigger_type']}\n"
        if r['trigger_type'] == 'event':
            desc += f"Event: {r['trigger_data'].get('event')}\n"
        else:
            desc += f"Interval: {r['trigger_data'].get('interval_minutes')} min\n"
        
        actions = r['actions']
        desc += f"Actions: {len(actions)}"
        
        embed.add_field(name=f"{status} {r['name']} (ID: {r['id']})", value=desc, inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

async def routine_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    if not interaction.guild_id:
        return []
    
    routines = await db.get_routines(str(interaction.guild_id))
    choices = []
    for r in routines:
        display = f"{r['name']} ({'ON' if r['active'] else 'OFF'})"
        if current.lower() in display.lower():
            choices.append(app_commands.Choice(name=display, value=r['id']))
    
    return choices[:25]

@bot.tree.command(name="routine_delete", description="Supprime une routine.")
@app_commands.describe(routine_id="La routine à supprimer")
@app_commands.autocomplete(routine_id=routine_autocomplete)
async def routine_delete(interaction: discord.Interaction, routine_id: int):
    if not interaction.guild_id: return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Réservé aux administrateurs.", ephemeral=True)
        return

    await db.delete_routine(routine_id)
    await bot.routine_manager.load_routines() # Reload
    await interaction.response.send_message(f"✅ Routine supprimée.", ephemeral=True)

@bot.tree.command(name="routine_toggle", description="Active/Désactive une routine.")
@app_commands.describe(routine_id="La routine à basculer")
@app_commands.autocomplete(routine_id=routine_autocomplete)
async def routine_toggle(interaction: discord.Interaction, routine_id: int):
    if not interaction.guild_id: return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Réservé aux administrateurs.", ephemeral=True)
        return

    new_state = await db.toggle_routine(routine_id)
    await bot.routine_manager.load_routines() # Reload
    status = "activée" if new_state else "désactivée"
    await interaction.response.send_message(f"✅ Routine {status}.", ephemeral=True)

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
        
        # Data State
        if routine_data:
            self.name = routine_data['name']
            self.triggers = [{"type": routine_data['trigger_type'], "data": routine_data['trigger_data']}]
            self.actions = routine_data['actions']
            
            # Parse conditions
            self.conditions = []
            self.condition_logic = "AND"
            if routine_data['conditions']:
                c = routine_data['conditions']
                if c.get('type') in ['AND', 'OR']:
                    self.condition_logic = c['type']
                    self.conditions = c.get('sub', [])
                else:
                    self.conditions = [c]
        else:
            self.name = "Nouvelle Routine"
            self.triggers = [] 
            self.conditions = [] 
            self.actions = [] 
            self.condition_logic = "AND"
        
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
            
            # Logic Toggle
            style = discord.ButtonStyle.primary if self.condition_logic == "AND" else discord.ButtonStyle.secondary
            label = "Logique: TOUT (AND)" if self.condition_logic == "AND" else "Logique: AU MOINS 1 (OR)"
            self.add_item(discord.ui.Button(label=label, style=style, custom_id="toggle_logic", row=0))

            if self.conditions:
                options = []
                for i, c in enumerate(self.conditions):
                    label = f"{i+1}. {self.format_condition(c)}"
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
        if a['type'] == 'play_sound': return f"Joue: {a['sound_name']}"
        if a['type'] == 'wait': return f"Pause: {a['delay']}s"
        if a['type'] == 'message': return f"Msg: {a['content']}"
        return "Action"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.type == discord.InteractionType.component:
            cid = interaction.data.get("custom_id")
            
            # Navigation
            if cid == "back":
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
                self.condition_logic = "OR" if self.condition_logic == "AND" else "AND"

            # Action Actions
            elif cid == "add_action_sound":
                # Show sound selector
                sounds = await self.db.get_available_sounds(self.guild_id)
                options = [discord.SelectOption(label=name, value=name) for name in sorted(sounds.keys())[:25]]
                if not options: options = [discord.SelectOption(label="Aucun son", value="none", disabled=True)]
                
                # Replace view temporarily with sound selector
                # Actually, let's just add a select to the current view
                self.add_item(discord.ui.Select(placeholder="Choisir un son", custom_id="quick_select_sound", options=options))
                await interaction.response.edit_message(view=self)
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
                    self.actions.append({"type": "play_sound", "sound_name": val, "target_strategy": "active"})

            self.update_components()
            await self.refresh_embed(interaction)
        return True

    async def refresh_embed(self, interaction: discord.Interaction):
        embed = discord.Embed(title=f"🛠️ {self.name}", color=discord.Color.blue())
        
        # Build Description based on state
        desc = ""
        
        # Triggers
        desc += f"**⚡ Triggers ({len(self.triggers)})**\n"
        if not self.triggers: desc += "*Aucun déclencheur*\n"
        for i, t in enumerate(self.triggers):
            desc += f"`{i+1}.` {self.format_trigger(t)}\n"
        
        # Conditions
        desc += f"\n**🤔 Conditions ({self.condition_logic})**\n"
        if not self.conditions: desc += "*Aucune condition*\n"
        for i, c in enumerate(self.conditions):
            desc += f"`{i+1}.` {self.format_condition(c)}\n"
            
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
        
        # Compile conditions
        final_conditions = None
        if self.conditions:
            if len(self.conditions) == 1:
                final_conditions = self.conditions[0]
            else:
                final_conditions = {"type": self.condition_logic, "sub": self.conditions}

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


if __name__ == "__main__":
    bot.run(Config.DISCORD_TOKEN)
