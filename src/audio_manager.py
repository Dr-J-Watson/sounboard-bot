"""
Module de gestion des fichiers audio pour le bot Soundboard.

Ce module gère la validation, le traitement et le stockage des fichiers
audio uploadés par les utilisateurs. Il vérifie les formats, durées et
tailles des fichiers avant de les accepter.

Auteur: Soundboard Bot
"""

import os
import logging
import shutil
import uuid
from typing import Optional
from mutagen import File as MutagenFile
from mutagen.mp3 import MP3
from mutagen.oggvorbis import OggVorbis
from mutagen.flac import FLAC
from mutagen.mp4 import MP4

from config import Config

logger = logging.getLogger(__name__)


class AudioManager:
    """
    Gestionnaire des fichiers audio.
    
    Cette classe est responsable de :
    - Valider les fichiers audio uploadés
    - Vérifier la durée des fichiers
    - Sauvegarder les fichiers dans le bon répertoire
    - Appliquer les limites configurées par serveur
    
    Attributes:
        db: Instance du gestionnaire de base de données
    """
    
    def __init__(self, db_manager):
        """
        Initialise le gestionnaire audio.
        
        Args:
            db_manager: Instance de DatabaseManager pour récupérer les configurations
        """
        self.db = db_manager

    def is_valid_audio_file(self, file_path: str) -> bool:
        """
        Vérifie si le fichier est un fichier audio valide et lisible.
        
        Utilise la bibliothèque Mutagen pour analyser le fichier et vérifier
        qu'il contient des métadonnées audio valides.
        
        Args:
            file_path: Chemin absolu vers le fichier à vérifier
            
        Returns:
            True si le fichier est un audio valide, False sinon
        """
        try:
            audio = MutagenFile(file_path)
            if audio is None:
                logger.debug(f"Mutagen n'a pas pu identifier le format de {file_path}")
                return False
            # Vérifier que le fichier a des informations audio
            if audio.info is None:
                logger.debug(f"Pas d'informations audio dans {file_path}")
                return False
            return True
        except Exception as e:
            logger.warning(f"Échec de la vérification audio pour {file_path}: {e}")
            return False

    def get_duration(self, file_path: str) -> float:
        """
        Retourne la durée du fichier audio en secondes.
        
        Utilise Mutagen pour extraire les informations de durée du fichier.
        En cas d'erreur, retourne 0.0 pour permettre au flux de continuer.
        
        Args:
            file_path: Chemin absolu vers le fichier audio
            
        Returns:
            Durée en secondes (float), ou 0.0 en cas d'erreur
        """
        try:
            audio = MutagenFile(file_path)
            if audio is not None and audio.info is not None:
                return audio.info.length
            return 0.0
        except Exception as e:
            logger.warning(f"Impossible d'obtenir la durée de {file_path}: {e}")
            return 0.0

    def get_audio_info(self, file_path: str) -> Optional[dict]:
        """
        Récupère les informations détaillées d'un fichier audio.
        
        Args:
            file_path: Chemin absolu vers le fichier audio
            
        Returns:
            Dictionnaire contenant les infos (durée, bitrate, sample_rate, channels)
            ou None en cas d'erreur
        """
        try:
            audio = MutagenFile(file_path)
            if audio is None or audio.info is None:
                return None
            
            info = {
                'duration': audio.info.length,
                'bitrate': getattr(audio.info, 'bitrate', None),
                'sample_rate': getattr(audio.info, 'sample_rate', None),
                'channels': getattr(audio.info, 'channels', None)
            }
            return info
        except Exception as e:
            logger.error(f"Erreur lors de l'analyse de {file_path}: {e}")
            return None

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """
        Nettoie un nom de fichier pour le rendre sûr.
        
        Supprime les caractères spéciaux et les espaces pour éviter
        les problèmes de compatibilité système.
        
        Args:
            filename: Nom de fichier original
            
        Returns:
            Nom de fichier nettoyé
        """
        # Caractères autorisés : alphanumériques, tirets, underscores, points
        safe_chars = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.')
        sanitized = ''.join(c if c in safe_chars else '_' for c in filename)
        # Éviter les doubles underscores
        while '__' in sanitized:
            sanitized = sanitized.replace('__', '_')
        return sanitized

    async def save_upload(self, attachment, filename: str, guild_id: str) -> str:
        """
        Sauvegarde une pièce jointe Discord dans le dossier sounds du serveur.
        
        Cette méthode effectue plusieurs vérifications :
        1. Extension du fichier autorisée
        2. Taille du fichier dans les limites
        3. Fichier audio valide
        4. Durée dans les limites configurées
        
        Args:
            attachment: Objet Attachment Discord (pièce jointe)
            filename: Nom de fichier original
            guild_id: ID du serveur Discord ou "global"
            
        Returns:
            Chemin complet du fichier sauvegardé
            
        Raises:
            ValueError: Si le fichier ne passe pas les validations
        """
        # Vérification de l'extension
        file_ext = os.path.splitext(filename)[1].lower()
        if file_ext not in Config.ALLOWED_EXTENSIONS:
            allowed = ', '.join(sorted(Config.ALLOWED_EXTENSIONS))
            raise ValueError(f"Extension '{file_ext}' non autorisée. Formats acceptés: {allowed}")

        # Vérification de la taille du fichier
        max_size_mb = await self.db.get_config(guild_id, "max_file_size_mb", Config.MAX_FILE_SIZE_MB)
        if max_size_mb > 0:
            max_size_bytes = max_size_mb * 1024 * 1024
            if attachment.size > max_size_bytes:
                actual_size = attachment.size / (1024 * 1024)
                raise ValueError(
                    f"Fichier trop volumineux ({actual_size:.2f} Mo). "
                    f"Maximum autorisé: {max_size_mb} Mo."
                )

        # Créer le dossier du serveur si nécessaire
        guild_sounds_dir = os.path.join(Config.SOUNDS_DIR, str(guild_id))
        os.makedirs(guild_sounds_dir, exist_ok=True)

        # Générer un UUID pour le nom de fichier (seul le nom d'affichage sera modifiable)
        file_ext = os.path.splitext(filename)[1].lower()
        uuid_filename = f"{uuid.uuid4().hex}{file_ext}"
        
        # Chemins temporaire et final
        temp_path = os.path.join(guild_sounds_dir, f"temp_{uuid_filename}")
        final_path = os.path.join(guild_sounds_dir, uuid_filename)

        try:
            # Télécharger le fichier temporairement
            await attachment.save(temp_path)
            
            # Vérifier que c'est un fichier audio valide
            if not self.is_valid_audio_file(temp_path):
                raise ValueError(
                    "Le fichier ne semble pas être un fichier audio valide. "
                    "Vérifiez le format et réessayez."
                )

            # Vérifier la durée
            duration = self.get_duration(temp_path)
            max_duration = await self.db.get_config(guild_id, "max_duration", Config.MAX_DURATION_SECONDS)
            
            if max_duration > 0 and duration > max_duration:
                raise ValueError(
                    f"Le son est trop long ({duration:.1f}s). "
                    f"Maximum autorisé: {max_duration}s."
                )

            # Tout est valide : déplacer vers l'emplacement final
            if os.path.exists(final_path):
                os.remove(final_path)
            
            shutil.move(temp_path, final_path)
            logger.info(f"Fichier audio sauvegardé: {final_path} (durée: {duration:.1f}s)")
            return final_path

        except Exception as e:
            # Nettoyer le fichier temporaire en cas d'erreur
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    logger.warning(f"Impossible de supprimer le fichier temporaire: {temp_path}")
            raise e

    async def delete_sound_file(self, guild_id: str, filename: str) -> bool:
        """
        Supprime un fichier son du disque.
        
        Args:
            guild_id: ID du serveur ou "global"
            filename: Nom du fichier à supprimer
            
        Returns:
            True si la suppression a réussi, False sinon
        """
        file_path = os.path.join(Config.SOUNDS_DIR, str(guild_id), filename)
        
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"Fichier supprimé: {file_path}")
                return True
            except OSError as e:
                logger.error(f"Erreur lors de la suppression de {file_path}: {e}")
                return False
        else:
            logger.warning(f"Fichier introuvable pour suppression: {file_path}")
            return False
