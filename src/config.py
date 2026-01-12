import os
from dotenv import load_dotenv

# Charger les variables d'environnement depuis le fichier .env
load_dotenv()

class Config:
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
    
    # Chemins
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    PROJECT_ROOT = os.path.dirname(BASE_DIR)
    SOUNDS_DIR = os.path.join(PROJECT_ROOT, "sounds")
    DATA_DIR = os.path.join(PROJECT_ROOT, "data")
    DB_FILE = os.path.join(DATA_DIR, "soundboard.db")

    # Configuration Audio
    MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", 30))
    MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", 5))
    MAX_NAME_LENGTH = int(os.getenv("MAX_NAME_LENGTH", 32))
    ALLOWED_EXTENSIONS = {'.mp3', '.wav', '.ogg', '.m4a'}
    
    # Configuration Bot
    VOICE_TIMEOUT_SECONDS = int(os.getenv("VOICE_TIMEOUT_SECONDS", 300)) # 5 minutes

    @staticmethod
    def validate():
        if not Config.DISCORD_TOKEN:
            raise ValueError("La variable d'environnement DISCORD_TOKEN est manquante.")
        
        # Créer les dossiers s'ils n'existent pas
        os.makedirs(Config.SOUNDS_DIR, exist_ok=True)
        os.makedirs(Config.DATA_DIR, exist_ok=True)
