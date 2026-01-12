FROM python:3.11-slim

# Installation des dépendances système
# ffmpeg: pour l'audio
# libopus0: codec audio
# libsodium23: chiffrement vocal
# build-essential, git: pour compiler certaines libs python si besoin
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    libsodium23 \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Installation des dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copie du code source
COPY . .

# Création des dossiers de données
RUN mkdir -p data sounds

CMD ["python", "src/bot.py"]
