"""
Module de gestion de la base de données pour le bot Soundboard.

Ce module gère toutes les opérations de base de données SQLite :
- Configuration des serveurs
- Gestion des sons (ajout, suppression, liste)
- Gestion des routines (automatisations)

Utilise aiosqlite pour des opérations asynchrones.

Auteur: Soundboard Bot
"""

import json
import os
import logging
from typing import Dict, Optional, List, Any
from contextlib import asynccontextmanager

import aiosqlite

from config import Config

logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    Gestionnaire de la base de données SQLite asynchrone.
    
    Cette classe gère toutes les interactions avec la base de données,
    incluant la configuration des serveurs, les sons et les routines.
    
    Attributes:
        db_path: Chemin vers le fichier de base de données SQLite
    """
    
    # Clés de configuration valides pour éviter les injections SQL
    VALID_CONFIG_KEYS = frozenset({"max_duration", "max_file_size_mb", "max_name_length"})
    
    def __init__(self, db_path: str):
        """
        Initialise le gestionnaire de base de données.
        
        Args:
            db_path: Chemin absolu vers le fichier de base de données
        """
        self.db_path = db_path

    @asynccontextmanager
    async def _get_connection(self):
        """
        Context manager pour obtenir une connexion à la base de données.
        
        Assure que la connexion est correctement fermée après utilisation.
        
        Yields:
            Connexion aiosqlite configurée avec row_factory
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            yield db

    async def init_db(self) -> None:
        """
        Initialise la structure de la base de données SQLite.
        
        Crée les tables nécessaires si elles n'existent pas :
        - guild_configs : Configuration par serveur
        - sounds : Catalogue des sons
        - routines : Automatisations configurées
        
        Ajoute également les index pour optimiser les recherches.
        """
        async with aiosqlite.connect(self.db_path) as db:
            # Table des configurations par serveur
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_configs (
                    guild_id TEXT PRIMARY KEY,
                    max_duration INTEGER,
                    max_file_size_mb INTEGER,
                    max_name_length INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Table des sons
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    added_by TEXT,
                    active BOOLEAN DEFAULT 1,
                    play_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(guild_id, name)
                )
            """)
            
            # Table des routines
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Table des salons ignorés
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ignored_channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    added_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(guild_id, channel_id)
                )
            """)
            
            # Index pour optimiser les recherches fréquentes
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_sounds_guild 
                ON sounds(guild_id)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_sounds_name 
                ON sounds(guild_id, name)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_routines_guild 
                ON routines(guild_id)
            """)
            
            # === Migrations pour bases existantes ===
            # Ajouter play_count si manquant
            try:
                await db.execute("ALTER TABLE sounds ADD COLUMN play_count INTEGER DEFAULT 0")
                logger.info("Migration: colonne play_count ajoutée")
            except Exception:
                pass  # Colonne existe déjà
            
            # Ajouter created_at si manquant
            try:
                await db.execute("ALTER TABLE sounds ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                logger.info("Migration: colonne created_at ajoutée à sounds")
            except Exception:
                pass
            
            await db.commit()
            logger.info("Base de données initialisée avec succès")

    # ==================== Configuration ====================

    async def get_config(self, guild_id: str, key: str, default: Any = None) -> Any:
        """
        Récupère une valeur de configuration pour un serveur.
        
        Args:
            guild_id: ID du serveur Discord
            key: Clé de configuration (max_duration, max_file_size_mb, max_name_length)
            default: Valeur par défaut si la clé n'existe pas
            
        Returns:
            La valeur de configuration ou la valeur par défaut
        """
        if key not in self.VALID_CONFIG_KEYS:
            logger.warning(f"Clé de configuration invalide demandée: {key}")
            return default
            
        async with self._get_connection() as db:
            async with db.execute(
                "SELECT * FROM guild_configs WHERE guild_id = ?", 
                (str(guild_id),)
            ) as cursor:
                row = await cursor.fetchone()
                if row and key in row.keys() and row[key] is not None:
                    return row[key]
                return default

    async def set_config(self, guild_id: str, key: str, value: Any) -> bool:
        """
        Définit une valeur de configuration pour un serveur.
        
        Args:
            guild_id: ID du serveur Discord
            key: Clé de configuration
            value: Nouvelle valeur
            
        Returns:
            True si la configuration a été mise à jour avec succès
        """
        if key not in self.VALID_CONFIG_KEYS:
            logger.warning(f"Tentative de définir une clé invalide: {key}")
            return False

        async with aiosqlite.connect(self.db_path) as db:
            # Vérifier si le serveur existe déjà
            async with db.execute(
                "SELECT 1 FROM guild_configs WHERE guild_id = ?", 
                (str(guild_id),)
            ) as cursor:
                exists = await cursor.fetchone()
            
            if exists:
                # Mise à jour avec timestamp
                await db.execute(
                    f"UPDATE guild_configs SET {key} = ?, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ?",
                    (value, str(guild_id))
                )
            else:
                # Insertion nouvelle entrée
                await db.execute(
                    f"INSERT INTO guild_configs (guild_id, {key}) VALUES (?, ?)",
                    (str(guild_id), value)
                )
            await db.commit()
            logger.debug(f"Configuration mise à jour: guild={guild_id}, {key}={value}")
            return True

    # ==================== Sons ====================

    async def add_sound(self, guild_id: str, name: str, filename: str, added_by: str = "System") -> bool:
        """
        Ajoute ou met à jour un son dans la base de données.
        
        Utilise ON CONFLICT pour mettre à jour si le son existe déjà.
        
        Args:
            guild_id: ID du serveur ou "global"
            name: Nom du son (identifiant unique par serveur)
            filename: Nom du fichier audio
            added_by: Utilisateur qui a ajouté le son
            
        Returns:
            True si l'opération a réussi
        """
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute("""
                    INSERT INTO sounds (guild_id, name, filename, added_by, active)
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(guild_id, name) DO UPDATE SET
                        filename = excluded.filename,
                        added_by = excluded.added_by,
                        active = 1
                """, (str(guild_id), name.lower(), filename, added_by))
                await db.commit()
                logger.info(f"Son ajouté: {name} (guild={guild_id})")
                return True
            except Exception as e:
                logger.error(f"Erreur lors de l'ajout du son {name}: {e}")
                return False

    async def remove_sound(self, guild_id: str, name: str) -> bool:
        """
        Supprime un son de la base de données.
        
        Note: Cette méthode ne supprime pas le fichier physique.
        
        Args:
            guild_id: ID du serveur ou "global"
            name: Nom du son à supprimer
            
        Returns:
            True si le son a été supprimé
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM sounds WHERE guild_id = ? AND name = ?",
                (str(guild_id), name.lower())
            )
            await db.commit()
            deleted = cursor.rowcount > 0
            if deleted:
                logger.info(f"Son supprimé: {name} (guild={guild_id})")
            return deleted

    async def get_sound(self, guild_id: str, name: str) -> Optional[Dict[str, Any]]:
        """
        Récupère les informations d'un son spécifique.
        
        Args:
            guild_id: ID du serveur ou "global"
            name: Nom du son
            
        Returns:
            Dictionnaire avec les infos du son, ou None si non trouvé
        """
        async with self._get_connection() as db:
            async with db.execute(
                "SELECT * FROM sounds WHERE guild_id = ? AND name = ? AND active = 1",
                (str(guild_id), name.lower())
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def list_sounds(self, guild_id: str) -> Dict[str, Dict[str, Any]]:
        """
        Liste tous les sons actifs d'un serveur.
        
        Args:
            guild_id: ID du serveur ou "global"
            
        Returns:
            Dictionnaire {nom_son: infos_son}
        """
        async with self._get_connection() as db:
            async with db.execute(
                "SELECT * FROM sounds WHERE guild_id = ? AND active = 1 ORDER BY name",
                (str(guild_id),)
            ) as cursor:
                rows = await cursor.fetchall()
                return {row['name']: dict(row) for row in rows}

    async def get_available_sounds(self, guild_id: str) -> Dict[str, Dict[str, Any]]:
        """
        Récupère tous les sons disponibles pour un serveur.
        
        Combine les sons globaux et les sons spécifiques au serveur.
        Les sons du serveur ont priorité sur les sons globaux en cas de conflit.
        
        Args:
            guild_id: ID du serveur Discord
            
        Returns:
            Dictionnaire {nom_son: infos_son} combiné
        """
        sounds = {}
        
        # Sons globaux (priorité basse)
        global_sounds = await self.list_sounds("global")
        sounds.update(global_sounds)
        
        # Sons du serveur (priorité haute, écrasent les globaux si même nom)
        guild_sounds = await self.list_sounds(guild_id)
        sounds.update(guild_sounds)
        
        return sounds

    async def increment_play_count(self, guild_id: str, name: str) -> None:
        """
        Incrémente le compteur de lecture d'un son.
        
        Args:
            guild_id: ID du serveur ou "global"
            name: Nom du son
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sounds SET play_count = play_count + 1 WHERE guild_id = ? AND name = ?",
                (str(guild_id), name.lower())
            )
            await db.commit()

    async def rename_sound(self, guild_id: str, old_name: str, new_name: str) -> bool:
        """
        Renomme un son.
        
        Args:
            guild_id: ID du serveur ou "global"
            old_name: Ancien nom du son
            new_name: Nouveau nom du son
            
        Returns:
            True si le renommage a réussi, False sinon
        """
        async with aiosqlite.connect(self.db_path) as db:
            # Vérifier que le nouveau nom n'existe pas déjà
            cursor = await db.execute(
                "SELECT 1 FROM sounds WHERE guild_id = ? AND name = ?",
                (str(guild_id), new_name.lower())
            )
            if await cursor.fetchone():
                await cursor.close()
                return False  # Le nom existe déjà
            await cursor.close()
            
            # Renommer le son
            await db.execute(
                "UPDATE sounds SET name = ? WHERE guild_id = ? AND name = ?",
                (new_name.lower(), str(guild_id), old_name.lower())
            )
            await db.commit()
            return True

    async def sync_with_folder(self, guild_id: str, folder_path: str) -> int:
        """
        Synchronise la base de données avec les fichiers présents dans un dossier.
        
        Ajoute les fichiers audio présents sur le disque mais absents de la DB.
        
        Args:
            guild_id: ID du serveur ou "global"
            folder_path: Chemin vers le dossier à synchroniser
            
        Returns:
            Nombre de fichiers ajoutés
        """
        if not os.path.exists(folder_path):
            logger.warning(f"Dossier inexistant pour sync: {folder_path}")
            return 0

        try:
            files = os.listdir(folder_path)
        except PermissionError:
            logger.error(f"Permission refusée pour lire: {folder_path}")
            return 0
            
        # Filtrer les fichiers audio valides
        audio_files = [
            f for f in files 
            if os.path.splitext(f)[1].lower() in Config.ALLOWED_EXTENSIONS
        ]
        
        # Récupérer les sons existants
        db_sounds = await self.list_sounds(guild_id)
        
        added_count = 0
        for filename in audio_files:
            # Générer le nom à partir du fichier
            name = os.path.splitext(filename)[0].lower().replace(" ", "_")
            
            # Ajouter si absent de la DB
            if name not in db_sounds:
                await self.add_sound(guild_id, name, filename, "System Sync")
                added_count += 1
        
        if added_count > 0:
            logger.info(f"Sync: {added_count} fichier(s) ajouté(s) pour guild={guild_id}")
        
        return added_count

    async def get_all_sounds(self) -> List[Dict[str, Any]]:
        """
        Récupère tous les sons de la base de données.
        
        Utilisé principalement pour l'autocomplétion globale.
        
        Returns:
            Liste de dictionnaires contenant les infos de tous les sons
        """
        async with self._get_connection() as db:
            async with db.execute("SELECT * FROM sounds WHERE active = 1") as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    # ==================== Routines ====================

    async def add_routine(
        self,
        guild_id: str,
        name: str,
        trigger_type: str,
        trigger_data: Dict,
        actions: List[Dict],
        conditions: Optional[Dict] = None
    ) -> int:
        """
        Ajoute une nouvelle routine.
        
        Args:
            guild_id: ID du serveur
            name: Nom de la routine
            trigger_type: Type de déclencheur ("timer" ou "event")
            trigger_data: Données du déclencheur (intervalle, événement, etc.)
            actions: Liste des actions à exécuter
            conditions: Conditions optionnelles pour l'exécution
            
        Returns:
            ID de la routine créée
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
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
            routine_id = cursor.lastrowid
            logger.info(f"Routine créée: {name} (id={routine_id}, guild={guild_id})")
            return routine_id

    async def get_routines(self, guild_id: str) -> List[Dict[str, Any]]:
        """
        Récupère toutes les routines d'un serveur.
        
        Les données JSON sont automatiquement désérialisées.
        
        Args:
            guild_id: ID du serveur
            
        Returns:
            Liste des routines avec leurs données parsées
        """
        async with self._get_connection() as db:
            async with db.execute(
                "SELECT * FROM routines WHERE guild_id = ? ORDER BY created_at DESC",
                (str(guild_id),)
            ) as cursor:
                rows = await cursor.fetchall()
                routines = []
                for row in rows:
                    r = dict(row)
                    # Désérialiser les champs JSON
                    r['trigger_data'] = json.loads(r['trigger_data'])
                    r['conditions'] = json.loads(r['conditions']) if r['conditions'] else None
                    r['actions'] = json.loads(r['actions'])
                    routines.append(r)
                return routines

    async def get_routine_by_id(self, routine_id: int) -> Optional[Dict[str, Any]]:
        """
        Récupère une routine par son ID.
        
        Args:
            routine_id: ID de la routine
            
        Returns:
            Dictionnaire de la routine ou None si non trouvée
        """
        async with self._get_connection() as db:
            async with db.execute(
                "SELECT * FROM routines WHERE id = ?",
                (routine_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    r = dict(row)
                    r['trigger_data'] = json.loads(r['trigger_data'])
                    r['conditions'] = json.loads(r['conditions']) if r['conditions'] else None
                    r['actions'] = json.loads(r['actions'])
                    return r
                return None

    async def delete_routine(self, routine_id: int) -> bool:
        """
        Supprime une routine.
        
        Args:
            routine_id: ID de la routine à supprimer
            
        Returns:
            True si la routine a été supprimée
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM routines WHERE id = ?",
                (routine_id,)
            )
            await db.commit()
            deleted = cursor.rowcount > 0
            if deleted:
                logger.info(f"Routine supprimée: id={routine_id}")
            return deleted

    async def toggle_routine(self, routine_id: int) -> Optional[bool]:
        """
        Active ou désactive une routine.
        
        Args:
            routine_id: ID de la routine
            
        Returns:
            Nouvel état (True=actif, False=inactif), ou None si routine non trouvée
        """
        async with aiosqlite.connect(self.db_path) as db:
            # Récupérer l'état actuel
            async with db.execute(
                "SELECT active FROM routines WHERE id = ?",
                (routine_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                current_state = row[0]
            
            # Inverser l'état
            new_state = 0 if current_state else 1
            await db.execute(
                "UPDATE routines SET active = ? WHERE id = ?",
                (new_state, routine_id)
            )
            await db.commit()
            
            logger.info(f"Routine {routine_id} {'activée' if new_state else 'désactivée'}")
            return bool(new_state)

    async def update_routine(
        self,
        routine_id: int,
        name: str,
        trigger_type: str,
        trigger_data: Dict,
        actions: List[Dict],
        conditions: Optional[Dict] = None
    ) -> bool:
        """
        Met à jour une routine existante.
        
        Args:
            routine_id: ID de la routine à mettre à jour
            name: Nouveau nom
            trigger_type: Nouveau type de déclencheur
            trigger_data: Nouvelles données du déclencheur
            actions: Nouvelle liste d'actions
            conditions: Nouvelles conditions
            
        Returns:
            True si la mise à jour a réussi
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                UPDATE routines 
                SET name = ?, trigger_type = ?, trigger_data = ?, 
                    conditions = ?, actions = ?
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
            updated = cursor.rowcount > 0
            if updated:
                logger.info(f"Routine mise à jour: id={routine_id}, name={name}")
            return updated
    # ==================== Salons Ignorés ====================

    async def add_ignored_channel(
        self,
        guild_id: str,
        channel_id: str,
        added_by: Optional[str] = None
    ) -> bool:
        """
        Ajoute un salon à la liste des salons ignorés.
        
        Args:
            guild_id: ID du serveur
            channel_id: ID du salon à ignorer
            added_by: ID de l'utilisateur qui a ajouté
            
        Returns:
            True si le salon a été ajouté, False s'il était déjà ignoré
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """INSERT INTO ignored_channels (guild_id, channel_id, added_by)
                       VALUES (?, ?, ?)""",
                    (str(guild_id), str(channel_id), str(added_by) if added_by else None)
                )
                await db.commit()
                logger.info(f"Salon ignoré ajouté: {channel_id} (guild={guild_id})")
                return True
        except Exception as e:
            if "UNIQUE constraint" in str(e):
                return False
            raise

    async def remove_ignored_channel(self, guild_id: str, channel_id: str) -> bool:
        """
        Retire un salon de la liste des salons ignorés.
        
        Args:
            guild_id: ID du serveur
            channel_id: ID du salon à retirer
            
        Returns:
            True si le salon a été retiré
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM ignored_channels WHERE guild_id = ? AND channel_id = ?",
                (str(guild_id), str(channel_id))
            )
            await db.commit()
            removed = cursor.rowcount > 0
            if removed:
                logger.info(f"Salon ignoré retiré: {channel_id} (guild={guild_id})")
            return removed

    async def get_ignored_channels(self, guild_id: str) -> List[str]:
        """
        Récupère la liste des salons ignorés pour un serveur.
        
        Args:
            guild_id: ID du serveur
            
        Returns:
            Liste des IDs de salons ignorés
        """
        async with self._get_connection() as db:
            async with db.execute(
                "SELECT channel_id FROM ignored_channels WHERE guild_id = ?",
                (str(guild_id),)
            ) as cursor:
                rows = await cursor.fetchall()
                return [row[0] for row in rows]

    async def is_channel_ignored(self, guild_id: str, channel_id: str) -> bool:
        """
        Vérifie si un salon est ignoré.
        
        Args:
            guild_id: ID du serveur
            channel_id: ID du salon
            
        Returns:
            True si le salon est ignoré
        """
        async with self._get_connection() as db:
            async with db.execute(
                "SELECT 1 FROM ignored_channels WHERE guild_id = ? AND channel_id = ?",
                (str(guild_id), str(channel_id))
            ) as cursor:
                return await cursor.fetchone() is not None