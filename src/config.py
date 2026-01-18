"""
Configuration centralisée du bot Soundboard.

Ce module charge les variables d'environnement et définit toutes les constantes
de configuration utilisées par le bot. Utilise python-dotenv pour le fichier .env.

Auteur: Soundboard Bot
"""

import os
from typing import FrozenSet
from dotenv import load_dotenv

# Charger les variables d'environnement depuis le fichier .env
load_dotenv()


class Config:
    """
    Classe de configuration contenant toutes les constantes du bot.
    
    Attributes:
        DISCORD_TOKEN: Token d'authentification Discord
        SOUNDS_DIR: Répertoire de stockage des fichiers audio
        DATA_DIR: Répertoire de stockage des données (DB)
        DB_FILE: Chemin vers la base de données SQLite
        MAX_DURATION_SECONDS: Durée maximale d'un son (en secondes)
        MAX_FILE_SIZE_MB: Taille maximale d'un fichier audio (en Mo)
        MAX_NAME_LENGTH: Longueur maximale du nom d'un son
        ALLOWED_EXTENSIONS: Extensions de fichiers audio autorisées
        VOICE_TIMEOUT_SECONDS: Délai avant déconnexion automatique du vocal
    """
    
    # === Token Discord ===
    DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
    
    # === Chemins des fichiers et dossiers ===
    BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
    PROJECT_ROOT: str = os.path.dirname(BASE_DIR)
    SOUNDS_DIR: str = os.path.join(PROJECT_ROOT, "sounds")
    DATA_DIR: str = os.path.join(PROJECT_ROOT, "data")
    DB_FILE: str = os.path.join(DATA_DIR, "soundboard.db")

    # === Configuration Audio ===
    MAX_DURATION_SECONDS: int = int(os.getenv("MAX_DURATION_SECONDS", "30"))
    MAX_FILE_SIZE_MB: int = int(os.getenv("MAX_FILE_SIZE_MB", "5"))
    MAX_NAME_LENGTH: int = int(os.getenv("MAX_NAME_LENGTH", "32"))
    ALLOWED_EXTENSIONS: FrozenSet[str] = frozenset({'.mp3', '.wav', '.ogg', '.m4a', '.flac', '.webm'})
    
    # === Configuration Bot ===
    VOICE_TIMEOUT_SECONDS: int = int(os.getenv("VOICE_TIMEOUT_SECONDS", "300"))  # 5 minutes par défaut
    
    # === Configuration avancée ===
    DEBUG_MODE: bool = os.getenv("DEBUG_MODE", "false").lower() == "true"
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    @staticmethod
    def validate() -> None:
        """
        Valide la configuration et crée les dossiers nécessaires.
        
        Raises:
            ValueError: Si le token Discord est manquant ou invalide
        """
        # Vérification du token Discord
        if not Config.DISCORD_TOKEN:
            raise ValueError("La variable d'environnement DISCORD_TOKEN est manquante.")
        
        if len(Config.DISCORD_TOKEN) < 50:
            raise ValueError("Le DISCORD_TOKEN semble invalide (trop court).")
        
        # Vérification des valeurs numériques
        if Config.MAX_DURATION_SECONDS < 0:
            raise ValueError("MAX_DURATION_SECONDS doit être positif ou nul.")
        
        if Config.MAX_FILE_SIZE_MB < 0:
            raise ValueError("MAX_FILE_SIZE_MB doit être positif ou nul.")
        
        # Créer les dossiers s'ils n'existent pas
        os.makedirs(Config.SOUNDS_DIR, exist_ok=True)
        os.makedirs(Config.DATA_DIR, exist_ok=True)
    
    @classmethod
    def get_sound_path(cls, guild_id: str, filename: str) -> str:
        """
        Construit le chemin complet vers un fichier son.
        
        Args:
            guild_id: L'ID du serveur ou "global"
            filename: Le nom du fichier audio
            
        Returns:
            Le chemin absolu vers le fichier
        """
        return os.path.join(cls.SOUNDS_DIR, guild_id, filename)
