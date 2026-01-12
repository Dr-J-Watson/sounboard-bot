import os
import logging
import shutil
from mutagen import File as MutagenFile
from config import Config

logger = logging.getLogger(__name__)

class AudioManager:
    def __init__(self, db_manager):
        self.db = db_manager

    def is_valid_audio_file(self, file_path: str) -> bool:
        """Vérifie si le fichier est un fichier audio valide et lisible."""
        try:
            audio = MutagenFile(file_path)
            if audio is None:
                return False
            return True
        except Exception as e:
            logger.warning(f"Échec de la vérification audio pour {file_path}: {e}")
            return False

    def get_duration(self, file_path: str) -> float:
        """Retourne la durée du fichier audio en secondes."""
        try:
            audio = MutagenFile(file_path)
            if audio is not None and audio.info is not None:
                return audio.info.length
            return 0.0
        except Exception:
            return 0.0

    async def save_upload(self, attachment, filename: str, guild_id: str) -> str:
        """
        Sauvegarde une pièce jointe Discord dans le dossier sounds du serveur.
        Retourne le chemin complet du fichier sauvegardé.
        Lève une exception si le fichier est invalide ou trop long.
        """
        file_ext = os.path.splitext(filename)[1].lower()
        if file_ext not in Config.ALLOWED_EXTENSIONS:
            raise ValueError(f"Extension non autorisée. Formats acceptés: {', '.join(Config.ALLOWED_EXTENSIONS)}")

        # Vérification taille fichier
        max_size_mb = await self.db.get_config(guild_id, "max_file_size_mb", Config.MAX_FILE_SIZE_MB)
        if max_size_mb > 0:
            max_size_bytes = max_size_mb * 1024 * 1024
            if attachment.size > max_size_bytes:
                raise ValueError(f"Fichier trop volumineux ({attachment.size / (1024*1024):.2f} Mo). Max: {max_size_mb} Mo.")

        # Dossier du serveur
        guild_sounds_dir = os.path.join(Config.SOUNDS_DIR, str(guild_id))
        os.makedirs(guild_sounds_dir, exist_ok=True)

        # Chemin temporaire pour vérification
        temp_path = os.path.join(guild_sounds_dir, f"temp_{filename}")
        final_path = os.path.join(guild_sounds_dir, filename)

        try:
            await attachment.save(temp_path)
            
            # Vérification validité audio
            if not self.is_valid_audio_file(temp_path):
                os.remove(temp_path)
                raise ValueError("Le fichier ne semble pas être un fichier audio valide.")

            # Vérification durée
            duration = self.get_duration(temp_path)
            max_duration = await self.db.get_config(guild_id, "max_duration", Config.MAX_DURATION_SECONDS)
            
            if max_duration > 0 and duration > max_duration:
                os.remove(temp_path)
                raise ValueError(f"Le son est trop long ({duration:.1f}s). Maximum autorisé: {max_duration}s.")

            # Si tout est bon, on déplace vers le nom final
            if os.path.exists(final_path):
                os.remove(final_path)
            
            shutil.move(temp_path, final_path)
            return final_path

        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise e
