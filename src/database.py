"""
Module de gestion de la base de données pour le bot Soundboard.

Ce module gère toutes les opérations de base de données SQLite :
- Configuration des serveurs
- Gestion des sons (ajout, suppression, liste)
- Gestion des routines (automatisations)

Utilise aiosqlite pour des opérations asynchrones.

Auteur: Soundboard Bot
"""

import asyncio
import json
import os
import logging
from typing import Dict, Optional, List, Any
from contextlib import asynccontextmanager

import aiosqlite

from config import Config
import sound_files

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
    VALID_CONFIG_KEYS = frozenset({
        "max_duration", "max_file_size_mb", "max_name_length",
        "volume", "max_volume"
    })
    
    def __init__(self, db_path: str):
        """
        Initialise le gestionnaire de base de données.
        
        Args:
            db_path: Chemin absolu vers le fichier de base de données
        """
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> aiosqlite.Connection:
        """
        Ouvre (une seule fois) la connexion partagée à la base.
        
        Une connexion persistante en mode WAL évite d'ouvrir un fichier à
        chaque requête et supprime les "database is locked" quand une
        routine écrit pendant qu'une commande lit.
        
        Returns:
            La connexion aiosqlite partagée
        """
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA synchronous=NORMAL")
            await self._conn.execute("PRAGMA busy_timeout=5000")
            await self._conn.commit()
            logger.debug("Connexion SQLite ouverte (WAL)")
        return self._conn

    async def close(self) -> None:
        """Ferme la connexion partagée (à appeler à l'arrêt du bot)."""
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception as e:
                logger.warning(f"Erreur lors de la fermeture de la base: {e}")
            finally:
                self._conn = None

    @asynccontextmanager
    async def _get_connection(self):
        """
        Context manager donnant accès à la connexion partagée.
        
        Le verrou sérialise les accès : sans lui, deux méthodes faisant
        SELECT puis INSERT pourraient s'entrelacer sur la même connexion et
        valider les écritures l'une de l'autre.
        
        Attention : ne jamais appeler une autre méthode de cette classe
        depuis l'intérieur de ce bloc (le verrou n'est pas réentrant).
        
        Yields:
            Connexion aiosqlite configurée avec row_factory
        """
        async with self._lock:
            db = await self.connect()
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
        async with self._get_connection() as db:
            # Table des configurations par serveur
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_configs (
                    guild_id TEXT PRIMARY KEY,
                    max_duration INTEGER,
                    max_file_size_mb INTEGER,
                    max_name_length INTEGER,
                    volume INTEGER,
                    max_volume INTEGER,
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
            
            # Ajouter volume / max_volume si manquants
            for column in ("volume", "max_volume"):
                try:
                    await db.execute(
                        f"ALTER TABLE guild_configs ADD COLUMN {column} INTEGER"
                    )
                    logger.info(f"Migration: colonne {column} ajoutée à guild_configs")
                except Exception:
                    pass
            
            await db.commit()
            
        # Migration des fichiers sons vers UUID (hors transaction pour accès fichiers)
        await self._migrate_sound_filenames()
        
        logger.info("Base de données initialisée avec succès")

    async def _purge_legacy_routines(self) -> int:
        """
        Supprime les routines créées avant le format v2.

        Returns:
            Nombre de routines supprimées
        """
        async with self._get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM routines WHERE trigger_type IS NOT 'v2'"
            )
            removed = cursor.rowcount
            await db.commit()

        if removed:
            logger.warning(
                f"⚠️ {removed} routine(s) au format antérieur supprimée(s). "
                "Le nouveau format par blocs n'est pas rétrocompatible : "
                "elles sont à recréer avec /routine_create."
            )
        return removed

    async def _migrate_sound_filenames(self) -> None:
        """
        Aligne les fichiers existants sur la convention `nom_uuid.ext`.

        Trois cas se présentent :
        - fichier déjà au bon format, rien à faire ;
        - fichier au seul format UUID, hérité de l'ancienne convention :
          son UUID est conservé et le nom d'affichage lui est préfixé ;
        - fichier au nom libre, déposé à la main : un UUID lui est attribué.

        L'UUID n'est jamais régénéré quand il existe déjà : c'est lui qui
        relie durablement la ligne en base au fichier sur le disque.
        """
        async with self._get_connection() as db:
            cursor = await db.execute("SELECT id, guild_id, name, filename FROM sounds")
            sounds = await cursor.fetchall()
            await cursor.close()

            renamed = 0
            adopted = 0

            for sound in sounds:
                old_filename = sound['filename']

                if sound_files.is_current_format(old_filename):
                    continue

                guild_dir = os.path.join(Config.SOUNDS_DIR, sound['guild_id'])
                old_path = os.path.join(guild_dir, old_filename)

                if not os.path.exists(old_path):
                    logger.warning(
                        f"Fichier introuvable, migration ignorée: {old_path}"
                    )
                    continue

                # L'UUID existant est réutilisé s'il y en a un
                new_filename = sound_files.rename_for(old_filename, sound['name'])
                new_path = os.path.join(guild_dir, new_filename)

                if os.path.exists(new_path):
                    logger.warning(
                        f"Nom déjà pris, migration ignorée: {new_filename}"
                    )
                    continue

                try:
                    os.rename(old_path, new_path)
                    await db.execute(
                        "UPDATE sounds SET filename = ? WHERE id = ?",
                        (new_filename, sound['id'])
                    )

                    if sound_files.extract_uuid(old_filename):
                        renamed += 1
                    else:
                        adopted += 1

                    logger.debug(f"Migré: {old_filename} -> {new_filename}")

                except OSError as e:
                    logger.error(f"Migration impossible pour {old_filename}: {e}")

            if renamed or adopted:
                await db.commit()
                logger.info(
                    f"Nommage des fichiers: {renamed} fichier(s) UUID préfixé(s), "
                    f"{adopted} fichier(s) doté(s) d'un UUID"
                )

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

        async with self._get_connection() as db:
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
        async with self._get_connection() as db:
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
        async with self._get_connection() as db:
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
        async with self._get_connection() as db:
            await db.execute(
                "UPDATE sounds SET play_count = play_count + 1 WHERE guild_id = ? AND name = ?",
                (str(guild_id), name.lower())
            )
            await db.commit()

    async def rename_sound(self, guild_id: str, old_name: str, new_name: str) -> bool:
        """
        Renomme un son, en base et sur le disque.

        Le fichier est renommé pour refléter le nouveau nom, mais son UUID
        est conservé : le lien entre la base et le fichier ne dépend jamais
        du nom d'affichage.

        Args:
            guild_id: ID du serveur ou "global"
            old_name: Ancien nom du son
            new_name: Nouveau nom du son

        Returns:
            True si le renommage a réussi, False sinon
        """
        async with self._get_connection() as db:
            cursor = await db.execute(
                "SELECT 1 FROM sounds WHERE guild_id = ? AND name = ?",
                (str(guild_id), new_name.lower())
            )
            if await cursor.fetchone():
                await cursor.close()
                return False  # Le nom est déjà pris
            await cursor.close()

            cursor = await db.execute(
                "SELECT id, filename FROM sounds WHERE guild_id = ? AND name = ?",
                (str(guild_id), old_name.lower())
            )
            row = await cursor.fetchone()
            await cursor.close()

            if row is None:
                return False

            old_filename = row['filename']
            new_filename = sound_files.rename_for(old_filename, new_name)

            # Renommer le fichier, sans faire échouer l'opération si le
            # disque résiste : la base reste la référence.
            if new_filename != old_filename:
                guild_dir = os.path.join(Config.SOUNDS_DIR, str(guild_id))
                old_path = os.path.join(guild_dir, old_filename)
                new_path = os.path.join(guild_dir, new_filename)

                if os.path.exists(new_path):
                    logger.warning(
                        f"Fichier déjà présent, nom de fichier inchangé: {new_filename}"
                    )
                    new_filename = old_filename
                elif os.path.exists(old_path):
                    try:
                        os.rename(old_path, new_path)
                    except OSError as e:
                        logger.error(f"Renommage du fichier impossible: {e}")
                        new_filename = old_filename
                else:
                    logger.warning(f"Fichier absent du disque: {old_path}")
                    new_filename = old_filename

            await db.execute(
                "UPDATE sounds SET name = ?, filename = ? WHERE id = ?",
                (new_name.lower(), new_filename, row['id'])
            )
            await db.commit()
            return True

    async def sync_with_folder(self, guild_id: str, folder_path: str) -> int:
        """
        Synchronise la base de données avec les fichiers présents dans un dossier.
        
        Ajoute les fichiers audio présents sur le disque mais absents de la
        base, et les renomme au format `nom_uuid.ext`. La comparaison se fait
        sur le nom de fichier, jamais sur le nom d'affichage, qui lui peut
        changer.
        
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
        
        # Récupérer les filenames existants dans la DB
        existing_filenames = set()
        async with self._get_connection() as db:
            async with db.execute(
                "SELECT filename FROM sounds WHERE guild_id = ?",
                (str(guild_id),)
            ) as cursor:
                rows = await cursor.fetchall()
                existing_filenames = {row[0] for row in rows}
        
        # Noms déjà pris : chargés UNE fois, puis maintenus en mémoire.
        # (Auparavant list_sounds() était rappelé pour chaque fichier.)
        taken_names = set(await self.list_sounds(guild_id))
        
        
        added_count = 0
        for filename in audio_files:
            # Vérifier si le fichier est déjà dans la DB (par filename)
            if filename in existing_filenames:
                continue
            
            # Le nom d'affichage se déduit du fichier, selon la convention
            name = sound_files.display_name_from_filename(filename)
            
            # Vérifier que le nom n'existe pas déjà, sinon ajouter un suffixe
            original_name = name
            counter = 1
            while name in taken_names:
                name = f"{original_name}_{counter}"
                counter += 1
            
            # Aligner tout de suite le fichier sur la convention, plutôt que
            # d'attendre le prochain démarrage
            stored_filename = filename
            if not sound_files.is_current_format(filename):
                candidate = sound_files.rename_for(filename, name)
                try:
                    os.rename(
                        os.path.join(folder_path, filename),
                        os.path.join(folder_path, candidate)
                    )
                    stored_filename = candidate
                except OSError as e:
                    logger.warning(f"Renommage impossible pour {filename}: {e}")
            
            await self.add_sound(guild_id, name, stored_filename, "System Sync")
            taken_names.add(name)
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
            trigger_type: Type de déclencheur ("timer", "schedule" ou "event")
            trigger_data: Données du déclencheur (intervalle, événement, etc.)
            actions: Liste des actions à exécuter
            conditions: Conditions optionnelles pour l'exécution
            
        Returns:
            ID de la routine créée
        """
        async with self._get_connection() as db:
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

    async def get_routine_by_id(
        self,
        routine_id: int,
        guild_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Récupère une routine par son ID.
        
        Args:
            routine_id: ID de la routine
            guild_id: Si fourni, la routine n'est retournée que si elle
                appartient à ce serveur
            
        Returns:
            Dictionnaire de la routine ou None si non trouvée
        """
        query = "SELECT * FROM routines WHERE id = ?"
        params: List[Any] = [routine_id]
        if guild_id is not None:
            query += " AND guild_id = ?"
            params.append(str(guild_id))
        
        async with self._get_connection() as db:
            async with db.execute(query, tuple(params)) as cursor:
                row = await cursor.fetchone()
                if row:
                    r = dict(row)
                    r['trigger_data'] = json.loads(r['trigger_data'])
                    r['conditions'] = json.loads(r['conditions']) if r['conditions'] else None
                    r['actions'] = json.loads(r['actions'])
                    return r
                return None

    async def delete_routine(self, routine_id: int, guild_id: Optional[str] = None) -> bool:
        """
        Supprime une routine.
        
        Args:
            routine_id: ID de la routine à supprimer
            guild_id: Si fourni, la suppression n'a lieu que si la routine
                appartient à ce serveur. Les IDs étant auto-incrémentés et
                globaux, ce filtre empêche un serveur d'agir sur un autre.
            
        Returns:
            True si la routine a été supprimée
        """
        query = "DELETE FROM routines WHERE id = ?"
        params: List[Any] = [routine_id]
        if guild_id is not None:
            query += " AND guild_id = ?"
            params.append(str(guild_id))
        
        async with self._get_connection() as db:
            cursor = await db.execute(query, tuple(params))
            await db.commit()
            deleted = cursor.rowcount > 0
            if deleted:
                logger.info(f"Routine supprimée: id={routine_id}")
            return deleted

    async def toggle_routine(self, routine_id: int, guild_id: Optional[str] = None) -> Optional[bool]:
        """
        Active ou désactive une routine.
        
        Args:
            routine_id: ID de la routine
            guild_id: Si fourni, la routine doit appartenir à ce serveur
            
        Returns:
            Nouvel état (True=actif, False=inactif), ou None si routine non trouvée
        """
        query = "SELECT active FROM routines WHERE id = ?"
        params: List[Any] = [routine_id]
        if guild_id is not None:
            query += " AND guild_id = ?"
            params.append(str(guild_id))
        
        async with self._get_connection() as db:
            # Récupérer l'état actuel
            async with db.execute(query, tuple(params)) as cursor:
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
        conditions: Optional[Dict] = None,
        guild_id: Optional[str] = None
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
            guild_id: Si fourni, la routine doit appartenir à ce serveur
            
        Returns:
            True si la mise à jour a réussi
        """
        query = """
                UPDATE routines 
                SET name = ?, trigger_type = ?, trigger_data = ?, 
                    conditions = ?, actions = ?
                WHERE id = ?
            """
        params: List[Any] = [
            name,
            trigger_type,
            json.dumps(trigger_data),
            json.dumps(conditions) if conditions else None,
            json.dumps(actions),
            routine_id
        ]
        if guild_id is not None:
            query += " AND guild_id = ?"
            params.append(str(guild_id))
        
        async with self._get_connection() as db:
            cursor = await db.execute(query, tuple(params))
            await db.commit()
            updated = cursor.rowcount > 0
            if updated:
                logger.info(f"Routine mise à jour: id={routine_id}, name={name}")
            return updated

    # ==================== Statistiques ====================

    async def get_stats(self, guild_id: str, limit: int = 10) -> Dict[str, Any]:
        """
        Récupère les statistiques de lecture d'un serveur.
        
        Prend en compte les sons du serveur et les sons globaux, dont le
        compteur play_count est alimenté à chaque lecture.
        
        Args:
            guild_id: ID du serveur
            limit: Nombre de sons à retourner dans le top
            
        Returns:
            Dictionnaire {total_sounds, total_plays, top: [(nom, compte, portée)]}
        """
        async with self._get_connection() as db:
            async with db.execute(
                """
                SELECT name, play_count, guild_id
                FROM sounds
                WHERE guild_id IN (?, 'global') AND active = 1
                ORDER BY play_count DESC, name ASC
                """,
                (str(guild_id),)
            ) as cursor:
                rows = await cursor.fetchall()
        
        return {
            'total_sounds': len(rows),
            'total_plays': sum(row['play_count'] or 0 for row in rows),
            'top': [
                {
                    'name': row['name'],
                    'plays': row['play_count'] or 0,
                    'global': row['guild_id'] == 'global'
                }
                for row in rows[:limit]
            ]
        }

    # ==================== Nettoyage ====================

    async def remove_sounds_by_filenames(self, guild_id: str, filenames: List[str]) -> int:
        """
        Supprime les entrées dont le fichier n'existe plus sur le disque.
        
        Args:
            guild_id: ID du serveur ou "global"
            filenames: Liste des filenames à retirer de la base
            
        Returns:
            Nombre d'entrées supprimées
        """
        if not filenames:
            return 0
        
        placeholders = ",".join("?" * len(filenames))
        async with self._get_connection() as db:
            cursor = await db.execute(
                f"DELETE FROM sounds WHERE guild_id = ? AND filename IN ({placeholders})",
                (str(guild_id), *filenames)
            )
            await db.commit()
            removed = cursor.rowcount
        
        if removed:
            logger.info(f"Nettoyage: {removed} entrée(s) sans fichier supprimée(s) (guild={guild_id})")
        return removed

    async def get_filenames(self, guild_id: str) -> Dict[str, str]:
        """
        Récupère la correspondance filename -> nom d'affichage d'un serveur.
        
        Args:
            guild_id: ID du serveur ou "global"
            
        Returns:
            Dictionnaire {filename: name}
        """
        async with self._get_connection() as db:
            async with db.execute(
                "SELECT filename, name FROM sounds WHERE guild_id = ?",
                (str(guild_id),)
            ) as cursor:
                rows = await cursor.fetchall()
                return {row['filename']: row['name'] for row in rows}

    async def delete_guild_data(self, guild_id: str) -> Dict[str, int]:
        """
        Supprime toutes les données d'un serveur.
        
        Appelé quand le bot est retiré d'un serveur, pour ne pas laisser
        de sons, routines et salons ignorés orphelins en base.
        
        Args:
            guild_id: ID du serveur
            
        Returns:
            Dictionnaire du nombre de lignes supprimées par table
        """
        result = {}
        async with self._get_connection() as db:
            for table in ("sounds", "routines", "ignored_channels", "guild_configs"):
                cursor = await db.execute(
                    f"DELETE FROM {table} WHERE guild_id = ?",
                    (str(guild_id),)
                )
                result[table] = cursor.rowcount
            await db.commit()
        
        logger.info(f"Données supprimées pour guild={guild_id}: {result}")
        return result

    async def reset_all(self) -> Dict[str, int]:
        """
        Vide entièrement la base : tous les serveurs, toutes les tables.

        Réservé à la commande de remise à zéro du propriétaire du bot.
        Le compteur d'auto-incrément est également réinitialisé pour que
        les prochains IDs de routines repartent de 1.

        Returns:
            Dictionnaire du nombre de lignes supprimées par table
        """
        result = {}
        async with self._get_connection() as db:
            for table in ("sounds", "routines", "ignored_channels", "guild_configs"):
                cursor = await db.execute(f"DELETE FROM {table}")
                result[table] = cursor.rowcount

            # Remise à zéro des AUTOINCREMENT (table absente si jamais utilisée)
            try:
                await db.execute("DELETE FROM sqlite_sequence")
            except Exception:
                pass

            await db.commit()

        logger.warning(f"⚠️ Base entièrement réinitialisée: {result}")
        return result

    async def count_all(self) -> Dict[str, int]:
        """
        Compte les lignes de chaque table, pour l'écran de confirmation.

        Returns:
            Dictionnaire {table: nombre de lignes}
        """
        result = {}
        async with self._get_connection() as db:
            for table in ("sounds", "routines", "ignored_channels", "guild_configs"):
                async with db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                    result[table] = (await cursor.fetchone())[0]
        return result

    async def count_for_guild(self, guild_id: str) -> Dict[str, int]:
        """
        Compte les lignes d'un serveur, pour l'écran de confirmation.

        Args:
            guild_id: ID du serveur

        Returns:
            Dictionnaire {table: nombre de lignes}
        """
        result = {}
        async with self._get_connection() as db:
            for table in ("sounds", "routines", "ignored_channels", "guild_configs"):
                async with db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE guild_id = ?",
                    (str(guild_id),)
                ) as cursor:
                    result[table] = (await cursor.fetchone())[0]
        return result

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
            async with self._get_connection() as db:
                await db.execute(
                    """INSERT INTO ignored_channels (guild_id, channel_id, added_by)
                       VALUES (?, ?, ?)""",
                    (str(guild_id), str(channel_id), str(added_by) if added_by else None)
                )
                await db.commit()
                logger.info(f"Salon ignoré ajouté: {channel_id} (guild={guild_id})")
                return True
        except aiosqlite.IntegrityError:
            # Le salon est déjà dans la liste (contrainte UNIQUE)
            return False

    async def remove_ignored_channel(self, guild_id: str, channel_id: str) -> bool:
        """
        Retire un salon de la liste des salons ignorés.
        
        Args:
            guild_id: ID du serveur
            channel_id: ID du salon à retirer
            
        Returns:
            True si le salon a été retiré
        """
        async with self._get_connection() as db:
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