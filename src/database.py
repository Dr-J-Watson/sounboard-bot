import json
import os
import logging
from typing import Dict, Optional, List
from config import Config

logger = logging.getLogger(__name__)

import aiosqlite


class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init_db(self):
        """Initialise la base de données SQLite."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_configs (
                    guild_id TEXT PRIMARY KEY,
                    max_duration INTEGER,
                    max_file_size_mb INTEGER,
                    max_name_length INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    added_by TEXT,
                    active BOOLEAN DEFAULT 1,
                    UNIQUE(guild_id, name)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS routines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    trigger_data TEXT NOT NULL,
                    conditions TEXT,
                    actions TEXT NOT NULL,
                    active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()

    async def get_config(self, guild_id: str, key: str, default=None):
        """Récupère une configuration pour un serveur donné."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM guild_configs WHERE guild_id = ?", (str(guild_id),)) as cursor:
                row = await cursor.fetchone()
                if row and key in row.keys() and row[key] is not None:
                    return row[key]
                return default

    async def set_config(self, guild_id: str, key: str, value):
        """Définit une configuration pour un serveur donné."""
        valid_keys = ["max_duration", "max_file_size_mb", "max_name_length"]
        if key not in valid_keys:
            return

        async with aiosqlite.connect(self.db_path) as db:
            # Check if guild exists
            async with db.execute("SELECT 1 FROM guild_configs WHERE guild_id = ?", (str(guild_id),)) as cursor:
                exists = await cursor.fetchone()
            
            if exists:
                await db.execute(f"UPDATE guild_configs SET {key} = ? WHERE guild_id = ?", (value, str(guild_id)))
            else:
                await db.execute(f"INSERT INTO guild_configs (guild_id, {key}) VALUES (?, ?)", (str(guild_id), value))
            await db.commit()

    async def add_sound(self, guild_id: str, name: str, filename: str, added_by: str = "System"):
        """Ajoute un son pour un serveur donné."""
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute("""
                    INSERT INTO sounds (guild_id, name, filename, added_by, active)
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(guild_id, name) DO UPDATE SET
                    filename = excluded.filename,
                    added_by = excluded.added_by,
                    active = 1
                """, (str(guild_id), name, filename, added_by))
                await db.commit()
            except Exception as e:
                logger.error(f"Erreur ajout son {name}: {e}")

    async def remove_sound(self, guild_id: str, name: str):
        """Supprime un son."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM sounds WHERE guild_id = ? AND name = ?", (str(guild_id), name))
            await db.commit()

    async def get_sound(self, guild_id: str, name: str) -> Optional[dict]:
        """Récupère les infos d'un son."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM sounds WHERE guild_id = ? AND name = ?", (str(guild_id), name)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return dict(row)
                return None

    async def list_sounds(self, guild_id: str) -> Dict[str, dict]:
        """Retourne tous les sons d'un serveur."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM sounds WHERE guild_id = ?", (str(guild_id),)) as cursor:
                rows = await cursor.fetchall()
                return {row['name']: dict(row) for row in rows}

    async def get_available_sounds(self, guild_id: str) -> Dict[str, dict]:
        """Retourne les sons du serveur ET les sons globaux."""
        sounds = {}
        
        # Global sounds
        global_sounds = await self.list_sounds("global")
        sounds.update(global_sounds)
        
        # Guild sounds
        guild_sounds = await self.list_sounds(guild_id)
        sounds.update(guild_sounds)
        
        return sounds

    async def sync_with_folder(self, guild_id: str, folder_path: str):
        """Synchronise la DB avec les fichiers présents dans le dossier."""
        if not os.path.exists(folder_path):
            return

        files = os.listdir(folder_path)
        # Note: Config.ALLOWED_EXTENSIONS needs to be accessible. 
        # It is imported at top of file.
        audio_files = [f for f in files if os.path.splitext(f)[1].lower() in Config.ALLOWED_EXTENSIONS]
        
        # Get existing sounds from DB
        db_sounds = await self.list_sounds(guild_id)
        
        # Add missing files to DB
        for filename in audio_files:
            name = os.path.splitext(filename)[0].lower().replace(" ", "_")
            # Check if filename matches (ignoring name key for a moment, but we key by name)
            # If name exists but filename is different? 
            # Simple sync: if name not in db, add it.
            if name not in db_sounds:
                await self.add_sound(guild_id, name, filename, "System Sync")

    async def get_all_sounds(self) -> List[dict]:
        """Retourne tous les sons de la base (pour l'autocomplétion globale)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM sounds") as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    # --- Routines ---

    async def add_routine(self, guild_id: str, name: str, trigger_type: str, trigger_data: dict, actions: list, conditions: dict = None):
        """Ajoute une routine."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO routines (guild_id, name, trigger_type, trigger_data, conditions, actions)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                str(guild_id), 
                name, 
                trigger_type, 
                json.dumps(trigger_data), 
                json.dumps(conditions) if conditions else None, 
                json.dumps(actions)
            ))
            await db.commit()

    async def get_routines(self, guild_id: str):
        """Récupère les routines d'un serveur."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM routines WHERE guild_id = ?", (str(guild_id),)) as cursor:
                rows = await cursor.fetchall()
                routines = []
                for row in rows:
                    r = dict(row)
                    r['trigger_data'] = json.loads(r['trigger_data'])
                    r['conditions'] = json.loads(r['conditions']) if r['conditions'] else None
                    r['actions'] = json.loads(r['actions'])
                    routines.append(r)
                return routines

    async def delete_routine(self, routine_id: int):
        """Supprime une routine."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM routines WHERE id = ?", (routine_id,))
            await db.commit()

    async def toggle_routine(self, routine_id: int) -> bool:
        """Active ou désactive une routine et retourne le nouvel état."""
        async with aiosqlite.connect(self.db_path) as db:
            # Get current state
            async with db.execute("SELECT active FROM routines WHERE id = ?", (routine_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return False
                current_state = row[0]
            
            new_state = 0 if current_state else 1
            await db.execute("UPDATE routines SET active = ? WHERE id = ?", (new_state, routine_id))
            await db.commit()
            return bool(new_state)

    async def update_routine(self, routine_id: int, name: str, trigger_type: str, trigger_data: dict, actions: list, conditions: dict = None):
        """Met à jour une routine existante."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE routines 
                SET name = ?, trigger_type = ?, trigger_data = ?, conditions = ?, actions = ?
                WHERE id = ?
            """, (
                name, 
                trigger_type, 
                json.dumps(trigger_data), 
                json.dumps(conditions) if conditions else None, 
                json.dumps(actions),
                routine_id
            ))
            await db.commit()
