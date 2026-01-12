# Discord Soundboard Bot

Un bot Discord Soundboard complet écrit en Python, utilisant les commandes slash (interactions).

## Fonctionnalités

*   **Commandes Slash** : Interface moderne et intuitive avec autocomplétion.
*   **Gestion Audio** : Supporte MP3, WAV, OGG, M4A.
*   **File d'attente** : Système de file d'attente pour les demandes multiples.
*   **Upload facile** : Ajoutez des sons directement depuis Discord avec `/add_sound`.
*   **Sons Globaux & par Serveur** : Les sons peuvent être partagés globalement ou spécifiques à un serveur.
*   **Routines** : Système d'automatisation avec triggers (timer, événements vocaux) et conditions.
*   **Persistance** : Base de données SQLite robuste.
*   **Validation** : Vérification du type de fichier, durée et taille maximale.
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

### 🎵 Sons

| Commande | Description |
| :--- | :--- |
| `/play <nom> [channel]` | Joue un son. Supporte l'autocomplétion. Optionnellement dans un salon spécifique. |
| `/list_sounds` | Affiche la liste de tous les sons disponibles. |
| `/add_sound <fichier> [nom]` | Ajoute un son au serveur (nécessite une pièce jointe). |
| `/stop` | Arrête la lecture et vide la file d'attente. |
| `/help` | Affiche l'aide détaillée. |

### ⚙️ Administration (Admin uniquement)

| Commande | Description |
| :--- | :--- |
| `/delete_sound <nom>` | Supprime un son du serveur. |
| `/config <setting> <value>` | Configure les paramètres (durée max, taille max, longueur nom). |
| `/sync` | Synchronise la base de données avec les fichiers du dossier. |

### 🤖 Routines (Admin uniquement)

| Commande | Description |
| :--- | :--- |
| `/routine_list` | Affiche les routines configurées. |
| `/routine_create` | Ouvre l'assistant de création de routine (interface graphique). |
| `/routine_cmd <nom> <commande>` | Créer une routine via commande textuelle. |
| `/routine_manage` | Ouvre le panel de gestion des routines. |
| `/routine_toggle <id>` | Active/Désactive une routine. |
| `/routine_delete <id>` | Supprime une routine. |

### 👑 Owner (Propriétaire du bot uniquement)

| Commande | Description |
| :--- | :--- |
| `/owner_add <scope> <nom> <fichier>` | Ajoute un son global ou sur un serveur spécifique. |
| `/owner_config <scope> <setting> <value>` | Configure les paramètres pour global ou un serveur. |
| `/owner_manage` | Ouvre le panel de gestion avancée. |

## Système de Routines

Les routines permettent d'automatiser des actions basées sur des déclencheurs.

### Déclencheurs (Triggers)

*   **Timer** : Exécute une action à intervalle régulier (ex: `10s`, `5m`, `1h`)
*   **Event** : Déclenché par un événement vocal (`voice_join`, `voice_leave`, `voice_move`)

### Conditions (Optionnel)

*   `user` : ID de l'utilisateur
*   `channel` : ID du salon vocal
*   `role` : ID du rôle
*   `time` : Plage horaire (format `HH:MM-HH:MM`)
*   `date` : Plage de dates (format `JJ/MM-JJ/MM`)

### Connecteurs logiques

Les conditions peuvent être combinées avec différents connecteurs :

| Connecteur | Alias | Description |
| :--- | :--- | :--- |
| **ET** | AND | Toutes les conditions doivent être vraies |
| **OU** | OR | Au moins une condition doit être vraie |
| **XOR** | - | Exactement une seule condition vraie |
| **NON** | NOT | Inverse une condition |

#### Mode simple
Utilisez le bouton "Logique" pour basculer entre ET, OU et XOR.

#### Mode avancé
Pour des expressions complexes avec priorités, utilisez le bouton "🧮 Logique Avancée" (disponible à partir de 2 conditions).

Chaque condition reçoit un identifiant (C1, C2, C3...) et vous pouvez écrire des expressions avec parenthèses :

```
(C1 ET C2) OU C3
C1 ET (C2 OU C3)
NON C1 ET C2
C1 XOR C2
```

**Précédence des opérateurs** (du plus au moins prioritaire) :
1. `NON` / `NOT`
2. `ET` / `AND`
3. `XOR`
4. `OU` / `OR`

### Actions

*   **play_sound** : Joue un son
*   **wait** : Pause (en secondes)
*   **message** : Envoie un message dans un salon

### Exemple via commande textuelle

```
/routine_cmd nom:"Bienvenue VIP" commande:"on join if user=123456789 and time=18:00-23:00 do wait 2s then play bienvenue"
```

### Exemple via l'assistant graphique

Une routine qui joue un son de bienvenue quand un utilisateur spécifique rejoint un salon vocal :
- Trigger: `voice_join`
- Condition: `user == 123456789`
- Actions: `wait 2s` → `play_sound bienvenue`

## Structure du Projet

*   `src/bot.py` : Point d'entrée du bot et gestion des commandes.
*   `src/audio_manager.py` : Validation et gestion des fichiers audio.
*   `src/database.py` : Gestion de la base de données SQLite.
*   `src/player.py` : Logique de lecture et file d'attente.
*   `src/routine_manager.py` : Gestion des routines et automatisations.
*   `src/config.py` : Configuration centralisée.
*   `sounds/global/` : Sons globaux (disponibles sur tous les serveurs).
*   `sounds/<guild_id>/` : Sons spécifiques à un serveur.
*   `data/` : Dossier de stockage de la base de données SQLite.

## Notes

*   Au démarrage, le bot synchronise automatiquement la base de données avec les fichiers présents dans les dossiers `sounds/`.
*   Si vous ajoutez des fichiers manuellement dans `sounds/`, utilisez `/sync` ou redémarrez le bot pour qu'ils soient détectés.
*   Les sons du serveur prennent la priorité sur les sons globaux en cas de conflit de nom.
