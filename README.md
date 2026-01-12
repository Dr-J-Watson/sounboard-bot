# Discord Soundboard Bot

Un bot Discord Soundboard complet écrit en Python, utilisant les commandes slash (interactions).

## Fonctionnalités

*   **Commandes Slash** : Interface moderne et intuitive.
*   **Gestion Audio** : Supporte MP3, WAV, OGG, M4A.
*   **File d'attente** : Système de file d'attente pour les demandes multiples.
*   **Upload facile** : Ajoutez des sons directement depuis Discord avec `/add_sound`.
*   **Persistance** : Base de données JSON simple et éditable.
*   **Validation** : Vérification du type de fichier et de la durée maximale.
*   **Docker** : Prêt à être déployé avec Docker.

## Prérequis

*   Python 3.8+ (si lancé localement)
*   FFmpeg (installé sur le système pour la lecture audio)
*   Un Token de Bot Discord

## Installation et Lancement

### 1. Configuration

Créez un fichier `.env` à la racine du projet (copiez `.env.example` si disponible) :

```env
DISCORD_TOKEN=votre_token_discord_ici
MAX_DURATION_SECONDS=30
VOICE_TIMEOUT_SECONDS=300
```

### 2. Lancement avec Docker (Recommandé)

```bash
# Construire et lancer le conteneur
docker-compose up --build -d
```

### 3. Lancement Local (Développement)

1.  Installez les dépendances :
    ```bash
    pip install -r requirements.txt
    ```
2.  Assurez-vous que FFmpeg est installé et accessible dans le PATH.
3.  Lancez le bot :
    ```bash
    python src/bot.py
    ```

## Commandes

| Commande | Description |
| :--- | :--- |
| `/play <nom>` | Joue un son. Supporte l'autocomplétion. |
| `/list_sounds` | Affiche la liste de tous les sons disponibles. |
| `/add_sound [nom]` | Ajoute un son (nécessite une pièce jointe). |
| `/stop` | Arrête la lecture et vide la file d'attente. |
| `/queue` | Affiche la file d'attente actuelle. |
| `/clear_queue` | Vide la file d'attente sans arrêter le son en cours. |
| `/config_duration <sec>` | Configure la durée maximale autorisée pour les nouveaux sons. |
| `/help` | Affiche l'aide. |

## Structure du Projet

*   `src/bot.py` : Point d'entrée du bot et gestion des commandes.
*   `src/audio_manager.py` : Validation et gestion des fichiers audio.
*   `src/database.py` : Gestion de la base de données JSON.
*   `src/player.py` : Logique de lecture et file d'attente.
*   `src/config.py` : Configuration centralisée.
*   `sounds/` : Dossier de stockage des fichiers audio.
*   `data/` : Dossier de stockage de la base de données (`sounds.json`).

## Notes

*   Au démarrage, le bot synchronise automatiquement la base de données avec les fichiers présents dans le dossier `sounds/`.
*   Si vous ajoutez des fichiers manuellement dans `sounds/`, redémarrez le bot pour qu'ils soient détectés.
