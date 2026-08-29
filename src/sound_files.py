"""
Convention de nommage des fichiers sons.

Un fichier s'appelle `nom_uuid.ext` : l'UUID identifie le son de façon
stable, le préfixe sert uniquement à reconnaître le fichier sur le disque.
Renommer un son change le préfixe et laisse l'UUID intact, si bien que la
base et le fichier restent liés quoi qu'il arrive.

Auteur: Soundboard Bot
"""

import os
import re
import unicodedata
import uuid as uuid_module
from typing import Optional, Tuple

# UUID hexadécimal de 32 caractères, tel que produit par uuid4().hex
UUID_RE = r'[a-f0-9]{32}'

# Ancien format : le fichier ne portait que l'UUID
LEGACY_UUID_PATTERN = re.compile(rf'^({UUID_RE})(\.[a-zA-Z0-9]+)$')

# Format courant : préfixe lisible, puis UUID
NAMED_UUID_PATTERN = re.compile(rf'^(.*)_({UUID_RE})(\.[a-zA-Z0-9]+)$')

# Longueur maximale du préfixe, pour rester sous la limite des systèmes de
# fichiers une fois l'UUID et l'extension ajoutés
MAX_PREFIX_LENGTH = 64


def slugify(name: str) -> str:
    """
    Transforme un nom d'affichage en préfixe de fichier sûr.

    Les accents sont retirés et tout ce qui n'est ni lettre, ni chiffre,
    ni tiret devient un underscore : le nom reste lisible sur le disque
    sans dépendre du système de fichiers.

    Args:
        name: Nom d'affichage du son

    Returns:
        Un préfixe utilisable dans un nom de fichier, jamais vide
    """
    normalized = unicodedata.normalize('NFKD', str(name))
    ascii_only = normalized.encode('ascii', 'ignore').decode('ascii')

    slug = re.sub(r'[^a-zA-Z0-9-]+', '_', ascii_only).strip('_').lower()
    slug = re.sub(r'_{2,}', '_', slug)[:MAX_PREFIX_LENGTH]

    return slug or "son"


def build_filename(name: str, extension: str, uuid_hex: Optional[str] = None) -> str:
    """
    Construit un nom de fichier au format `nom_uuid.ext`.

    Args:
        name: Nom d'affichage du son
        extension: Extension du fichier, avec le point
        uuid_hex: UUID à réutiliser. Un nouvel UUID est tiré si absent, ce
            qui ne doit arriver qu'à la création d'un son.

    Returns:
        Le nom de fichier complet
    """
    if not uuid_hex:
        uuid_hex = uuid_module.uuid4().hex

    return f"{slugify(name)}_{uuid_hex}{extension.lower()}"


def extract_uuid(filename: str) -> Optional[str]:
    """
    Extrait l'UUID d'un nom de fichier, quel que soit son format.

    Args:
        filename: Nom du fichier

    Returns:
        L'UUID hexadécimal, ou None si le fichier n'en contient pas
    """
    match = NAMED_UUID_PATTERN.match(filename)
    if match:
        return match.group(2)

    match = LEGACY_UUID_PATTERN.match(filename)
    if match:
        return match.group(1)

    return None


def split_filename(filename: str) -> Tuple[Optional[str], Optional[str], str]:
    """
    Découpe un nom de fichier en préfixe, UUID et extension.

    Args:
        filename: Nom du fichier

    Returns:
        Tuple (préfixe, uuid, extension). Le préfixe et l'UUID valent None
        quand le fichier ne suit aucune des deux conventions.
    """
    match = NAMED_UUID_PATTERN.match(filename)
    if match:
        return match.group(1), match.group(2), match.group(3)

    match = LEGACY_UUID_PATTERN.match(filename)
    if match:
        return None, match.group(1), match.group(2)

    stem, ext = os.path.splitext(filename)
    return stem, None, ext


def display_name_from_filename(filename: str) -> str:
    """
    Devine un nom d'affichage à partir d'un fichier trouvé sur le disque.

    Sert à l'import de fichiers déposés à la main, par /sync.

    Args:
        filename: Nom du fichier

    Returns:
        Un nom d'affichage exploitable
    """
    prefix, uuid_hex, _ = split_filename(filename)

    if prefix:
        return prefix.lower().replace(" ", "_")

    # Fichier au seul format UUID : on fabrique un nom reconnaissable
    if uuid_hex:
        return f"son_{uuid_hex[:8]}"

    return "son"


def is_current_format(filename: str) -> bool:
    """
    Indique si le fichier suit déjà la convention `nom_uuid.ext`.

    Args:
        filename: Nom du fichier

    Returns:
        True si le format est le bon
    """
    return NAMED_UUID_PATTERN.match(filename) is not None


def rename_for(filename: str, new_name: str) -> str:
    """
    Calcule le nouveau nom de fichier après un renommage du son.

    L'UUID existant est conservé : le fichier reste le même, seul son
    préfixe change.

    Args:
        filename: Nom de fichier actuel
        new_name: Nouveau nom d'affichage

    Returns:
        Le nom de fichier à utiliser
    """
    _, uuid_hex, extension = split_filename(filename)
    return build_filename(new_name, extension, uuid_hex)
