"""
Catalogue des blocs de routine.

Ce module est la source unique de vérité pour les blocs disponibles :
actions et conditions y sont déclarées une fois, avec tout ce qui les
concerne (exécution, écriture textuelle, affichage, bouton du panel).

Ajouter un bloc = ajouter une entrée ici, plus la méthode qui l'exécute
dans RoutineManager. Ni le parseur, ni le panel, ni l'aide n'ont besoin
d'être modifiés : ils lisent ce catalogue.

Auteur: Soundboard Bot
"""

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from config import Config


# =============================================================================
# DURÉES ET JOURS
# =============================================================================

# Durées composées : "1m20s", "2h", "90", "1h30m10s"
_DURATION_TOKEN_RE = re.compile(r'(\d+)\s*([hms])', re.IGNORECASE)
_UNIT_SECONDS = {'h': 3600, 'm': 60, 's': 1}

# Jours de la semaine acceptés dans les routines (index lundi=0)
WEEKDAYS = {
    'lun': 0, 'lundi': 0, 'mon': 0, 'monday': 0,
    'mar': 1, 'mardi': 1, 'tue': 1, 'tuesday': 1,
    'mer': 2, 'mercredi': 2, 'wed': 2, 'wednesday': 2,
    'jeu': 3, 'jeudi': 3, 'thu': 3, 'thursday': 3,
    'ven': 4, 'vendredi': 4, 'fri': 4, 'friday': 4,
    'sam': 5, 'samedi': 5, 'sat': 5, 'saturday': 5,
    'dim': 6, 'dimanche': 6, 'sun': 6, 'sunday': 6,
}


def parse_duration_seconds(text: str) -> int:
    """
    Convertit une durée écrite en secondes.

    Accepte les formes composées et les unités mélangées :
    "30s", "5m", "2h", "1m20s", "1h30m10s", ou un nombre nu (= secondes).

    Args:
        text: Durée à convertir

    Returns:
        Durée en secondes

    Raises:
        ValueError: Si le format n'est pas reconnu
    """
    if text is None:
        raise ValueError("Durée vide.")

    cleaned = str(text).strip().lower().replace(" ", "")
    if not cleaned:
        raise ValueError("Durée vide.")

    if cleaned.isdigit():
        return int(cleaned)

    matches = _DURATION_TOKEN_RE.findall(cleaned)
    if not matches:
        raise ValueError(
            f"Format de durée invalide '{text}'. "
            "Utilisez par exemple 30s, 5m, 2h ou 1m20s."
        )

    # Refuser les restes non reconnus ("5x", "1m20z")
    consumed = "".join(f"{value}{unit}" for value, unit in matches)
    if consumed != cleaned:
        raise ValueError(
            f"Format de durée invalide '{text}'. "
            "Utilisez par exemple 30s, 5m, 2h ou 1m20s."
        )

    return sum(int(value) * _UNIT_SECONDS[unit.lower()] for value, unit in matches)


def parse_duration_range(text: str) -> Tuple[int, int]:
    """
    Convertit une durée ou une plage de durées en secondes.

    "5s" donne (5, 5) ; "1m20s-2h" donne (80, 7200). Les bornes sont
    réordonnées si elles sont écrites à l'envers.

    Args:
        text: Durée simple ou plage "min-max"

    Returns:
        Tuple (minimum, maximum) en secondes

    Raises:
        ValueError: Si le format n'est pas reconnu
    """
    cleaned = str(text).strip().lower().replace(" ", "")

    if "-" in cleaned:
        low_raw, _, high_raw = cleaned.partition("-")
        low = parse_duration_seconds(low_raw)
        high = parse_duration_seconds(high_raw)
        return (low, high) if low <= high else (high, low)

    value = parse_duration_seconds(cleaned)
    return value, value


def format_duration(seconds: int) -> str:
    """
    Formate une durée en secondes de façon lisible ("1m20s", "2h").

    Args:
        seconds: Durée en secondes

    Returns:
        Représentation compacte de la durée
    """
    seconds = int(seconds)
    if seconds <= 0:
        return "0s"

    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)

    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return "".join(parts)


# =============================================================================
# DÉFINITION DES BLOCS
# =============================================================================

@dataclass(frozen=True)
class ActionBlock:
    """
    Description complète d'un type d'action.

    Attributes:
        type: Identifiant stocké en base
        handler: Nom de la méthode de RoutineManager qui l'exécute.
            Signature: async (action, context, routine) -> bool,
            le booléen indiquant s'il faut interrompre la routine.
        describe: Rendu court, pour les embeds
        verbs: Verbes acceptés par la syntaxe textuelle
        parse: Construit l'action depuis (verbe, arguments) textuels
        syntax: Ligne d'aide affichée dans /help
        refresh_context: Reconstruire le contexte avant l'exécution.
            Inutile pour les actions qui n'y touchent pas (pause, tirage).
    """
    type: str
    handler: str
    describe: Callable[[dict], str]
    verbs: Tuple[str, ...] = ()
    parse: Optional[Callable[[str, str], dict]] = None
    syntax: str = ""
    refresh_context: bool = True


@dataclass(frozen=True)
class ConditionBlock:
    """
    Description complète d'un type de condition.

    Attributes:
        type: Identifiant stocké en base
        handler: Nom de la méthode de RoutineManager qui l'évalue.
            Signature: (op, value, context) -> bool
        describe: Rendu court, pour les embeds
        label: Libellé dans le menu déroulant
        emoji: Émoji du menu
        hint: Phrase explicative sous le libellé, dans le menu
        value_label: Intitulé du champ de saisie du formulaire
        value_placeholder: Exemple affiché dans le champ
        picker: Sélecteur natif Discord à proposer ("user", "channel",
            "role"), qui évite d'avoir à copier un identifiant
        aliases: Noms acceptés dans la syntaxe textuelle et la modale
        ops: Opérateurs autorisés, le premier étant celui par défaut
        syntax: Ligne d'aide affichée dans /help
    """
    type: str
    handler: str
    describe: Callable[[dict], str]
    label: str = ""
    emoji: str = "🤔"
    hint: str = ""
    value_label: str = "Valeur"
    value_placeholder: str = ""
    picker: Optional[str] = None
    aliases: Tuple[str, ...] = ()
    ops: Tuple[str, ...] = ("==", "!=")
    syntax: str = ""

    @property
    def default_op(self) -> str:
        """Opérateur proposé par défaut dans le formulaire."""
        return self.ops[0] if self.ops else "=="


@dataclass
class MenuEntry:
    """
    Entrée d'un menu déroulant de création.

    Attributes:
        key: Valeur renvoyée par le menu
        label: Libellé affiché
        emoji: Émoji affiché
        hint: Phrase explicative sous le libellé
        modal: Nom de la classe de formulaire à ouvrir, si saisie nécessaire
        payload: Bloc créé directement, sans saisie
        special: Comportement particulier ("sound" pour le sélecteur de sons)
    """
    key: str
    label: str
    emoji: str
    hint: str = ""
    modal: Optional[str] = None
    payload: Optional[dict] = None
    special: Optional[str] = None


@dataclass(frozen=True)
class TriggerBlock:
    """
    Description d'un déclencheur proposé dans le menu.

    Attributes:
        key: Valeur renvoyée par le menu
        label: Libellé affiché
        emoji: Émoji affiché
        hint: Phrase explicative sous le libellé
        modal: Formulaire à ouvrir, si le déclencheur demande une saisie
        trigger: Déclencheur créé directement, sans saisie
    """
    key: str
    label: str
    emoji: str
    hint: str = ""
    modal: Optional[str] = None
    trigger: Optional[dict] = None


# =============================================================================
# PARSEURS TEXTUELS
# =============================================================================

def _parse_play(verb: str, args: str) -> dict:
    """Parse `play <son>`."""
    if not args:
        raise ValueError("Nom de son manquant après 'play'")
    return {"type": "play_sound", "sound_name": args.strip(), "target_strategy": "active"}


def _parse_wait(verb: str, args: str) -> dict:
    """Parse `wait 5s` ou `wait 10s-2m`."""
    if not args:
        raise ValueError("Durée manquante après 'wait'")

    low, high = parse_duration_range(args)
    if high > low:
        return {"type": "wait", "delay_min": low, "delay_max": high}
    return {"type": "wait", "delay": low}


def _parse_chance(verb: str, args: str) -> dict:
    """Parse `chance 30%`."""
    if not args:
        raise ValueError("Pourcentage manquant après 'chance' (ex: chance 30%)")

    try:
        percent = float(args.strip().rstrip('%'))
    except ValueError:
        raise ValueError(f"Pourcentage invalide: {args}")

    if not 0 <= percent <= 100:
        raise ValueError("Le pourcentage doit être compris entre 0 et 100.")
    return {"type": "chance", "percent": percent}


def _parse_volume(verb: str, args: str) -> dict:
    """Parse `volume 150` ou `volume reset`."""
    if not args:
        raise ValueError("Valeur manquante après 'volume' (ex: volume 150)")

    value = args.strip().lower()
    if value != "reset":
        limit = Config.VOLUME_HARD_LIMIT
        if not value.isdigit() or not 0 <= int(value) <= limit:
            raise ValueError(
                f"Le volume doit être un entier entre 0 et {limit}, ou 'reset'. "
                "Le plafond du serveur (/config max_volume) s'applique en plus."
            )
        value = int(value)
    return {"type": "volume", "value": value}


def _parse_move(verb: str, args: str) -> dict:
    """Parse `move <id>` ou `move member <id>`."""
    parts = args.split()
    if not parts:
        raise ValueError("Salon manquant après 'move'")

    if parts[0].lower() in ("member", "membre", "user"):
        if len(parts) < 2:
            raise ValueError("Salon manquant après 'move member'")
        target, channel_id = "member", parts[1]
    else:
        target, channel_id = "bot", parts[0]

    channel_id = channel_id.strip("<>#")
    if not channel_id.isdigit():
        raise ValueError(f"ID de salon invalide: {channel_id}")
    return {"type": "move", "target": target, "channel_id": channel_id}


def _parse_message(verb: str, args: str) -> dict:
    """Parse `msg <texte>`."""
    if not args:
        raise ValueError("Contenu manquant après 'msg'")
    return {"type": "message", "content": args, "channel_id": None}


def _parse_dm(verb: str, args: str) -> dict:
    """Parse `dm <texte>`."""
    if not args:
        raise ValueError("Contenu manquant après 'dm'")
    return {"type": "dm", "content": args}


def _parse_control(verb: str, args: str) -> dict:
    """Parse `stop`, `skip`, `clear`, `leave`, `leave_now`."""
    command = "leave" if verb == "quit" else verb
    return {"type": "player_control", "command": command}


# =============================================================================
# RENDUS
# =============================================================================

def _pct(value) -> str:
    """Formate un pourcentage sans décimale inutile (20.0 -> 20)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number == int(number) else f"{number:g}"


def _describe_user(c: dict) -> str:
    """Décrit la condition sur le membre."""
    if c.get('op') == '!=':
        return "c'est quelqu'un d'autre que le membre " + str(c.get('value'))
    return "c'est le membre " + str(c.get('value'))


def _describe_channel(c: dict) -> str:
    """Décrit la condition sur le salon."""
    verbe = "n'est pas" if c.get('op') == '!=' else "est"
    return f"le salon {verbe} {c.get('value')}"


def _describe_role(c: dict) -> str:
    """Décrit la condition sur le rôle."""
    verbe = "n'a pas" if c.get('op') == '!=' else "a"
    return f"le membre {verbe} le rôle {c.get('value')}"


def _describe_weekday(c: dict) -> str:
    """Décrit la condition sur le jour de la semaine."""
    verbe = "on n'est pas" if c.get('op') == '!=' else "on est"
    return f"{verbe} {c.get('value')}"


def _describe_play(a: dict) -> str:
    name = a.get('sound_name')
    return "🎲 jouer un son au hasard" if name == '__random__' else f"🎵 jouer {name}"


def _describe_wait(a: dict) -> str:
    if a.get('delay_min') is not None:
        return (f"💤 attendre entre {format_duration(a['delay_min'])} "
                f"et {format_duration(a['delay_max'])}")
    return f"💤 attendre {format_duration(a.get('delay', 0))}"


def _describe_volume(a: dict) -> str:
    value = a.get('value')
    if value == 'reset':
        return "🔊 remettre le volume par défaut"
    return f"🔊 régler le volume sur {value}%"


def _describe_control(a: dict) -> str:
    labels = {
        'stop': '⏹️ tout arrêter et vider la file',
        'skip': '⏭️ passer au son suivant',
        'clear': '🧹 vider la file d\'attente',
        'leave': '🚪 quitter après la file',
        'leave_now': '🏃 quitter immédiatement',
    }
    return labels.get(a.get('command'), 'contrôle')


# =============================================================================
# CATALOGUE DES ACTIONS
# =============================================================================

ACTION_BLOCKS: Dict[str, ActionBlock] = {
    block.type: block for block in [
        ActionBlock(
            type="play_sound",
            handler="_action_play_sound",
            describe=_describe_play,
            verbs=("play",),
            parse=_parse_play,
            syntax="`play <son>` — le son est ajouté à la file",
        ),
        ActionBlock(
            type="wait",
            handler="_action_wait",
            describe=_describe_wait,
            verbs=("wait",),
            parse=_parse_wait,
            syntax="`wait 1m20s` · `wait 10s-2m` — pause, fixe ou aléatoire",
            refresh_context=False,
        ),
        ActionBlock(
            type="chance",
            handler="_action_chance",
            describe=lambda a: f"🎲 tirage à {_pct(a.get('percent'))}%",
            verbs=("chance",),
            parse=_parse_chance,
            syntax="`chance 25%` — interrompt la routine si le tirage échoue",
            refresh_context=False,
        ),
        ActionBlock(
            type="volume",
            handler="_action_set_volume",
            describe=_describe_volume,
            verbs=("volume",),
            parse=_parse_volume,
            syntax="`volume 150` · `volume reset` — plafonné par `/config`",
        ),
        ActionBlock(
            type="message",
            handler="_action_send_message",
            describe=lambda a: f"💬 message « {str(a.get('content', ''))[:30]} »",
            verbs=("msg", "message"),
            parse=_parse_message,
            syntax="`msg <texte>` — `{user}` et `{username}` sont remplacés",
        ),
        ActionBlock(
            type="dm",
            handler="_action_send_dm",
            describe=lambda a: f"📩 privé « {str(a.get('content', ''))[:30]} »",
            verbs=("dm", "mp"),
            parse=_parse_dm,
            syntax="`dm <texte>` — en message privé",
        ),
        ActionBlock(
            type="move",
            handler="_action_move",
            describe=lambda a: "↔️ déplacer " + (
                "le membre" if a.get('target') == 'member' else "le bot"
            ) + f" vers {a.get('channel_id', '?')}",
            verbs=("move",),
            parse=_parse_move,
            syntax="`move <id_salon>` · `move member <id_salon>`",
        ),
        ActionBlock(
            type="player_control",
            handler="_action_player_control",
            describe=_describe_control,
            verbs=("stop", "skip", "clear", "leave", "leave_now", "quit"),
            parse=_parse_control,
            syntax=(
                "`stop` — coupe le son et vide la file\n"
                "`skip` · `clear`\n"
                "`leave` — quitte **après** la fin de la file\n"
                "`leave_now` — quitte immédiatement"
            ),
        ),
    ]
}


# Entrées du menu déroulant « Ajouter une action », dans l'ordre d'affichage.
ACTION_MENU: List[MenuEntry] = [
    MenuEntry("play", "Jouer un son", "🎵",
              "Ajoute un son à la file d'attente", special="sound"),
    MenuEntry("wait", "Attendre", "💤",
              "Met la trame en pause avant la suite", modal="WaitInputModal"),
    MenuEntry("volume", "Régler le volume", "🔊",
              "Change le volume du serveur", modal="VolumeInputModal"),
    MenuEntry("message", "Envoyer un message", "💬",
              "Écrit dans un salon, ou en privé", modal="MessageInputModal"),
    MenuEntry("move", "Déplacer quelqu'un", "↔️",
              "Change le bot ou le membre de salon", special="move"),
    MenuEntry("leave", "Quitter à la fin de la file", "🚪",
              "Attend que les sons en attente soient joués",
              payload={"type": "player_control", "command": "leave"}),
    MenuEntry("leave_now", "Quitter immédiatement", "🏃",
              "Coupe la file et se déconnecte tout de suite",
              payload={"type": "player_control", "command": "leave_now"}),
    MenuEntry("stop", "Tout arrêter", "⏹️",
              "Coupe le son en cours et vide la file",
              payload={"type": "player_control", "command": "stop"}),
    MenuEntry("skip", "Passer au suivant", "⏭️",
              "Interrompt le son en cours seulement",
              payload={"type": "player_control", "command": "skip"}),
    MenuEntry("clear", "Vider la file", "🧹",
              "Retire les sons en attente, sans couper le son actuel",
              payload={"type": "player_control", "command": "clear"}),
]


# =============================================================================
# CATALOGUE DES DÉCLENCHEURS
# =============================================================================

def _voice(key: str, label: str, emoji: str, hint: str) -> TriggerBlock:
    """Raccourci pour un déclencheur vocal sans saisie."""
    return TriggerBlock(
        key=key, label=label, emoji=emoji, hint=hint,
        trigger={"type": "event", "data": {"event": key}}
    )


TRIGGER_MENU: List[TriggerBlock] = [
    TriggerBlock("timer", "Toutes les X minutes", "⏰",
                 "À intervalle régulier, ou tiré au sort",
                 modal="TimeInputModal"),
    TriggerBlock("schedule", "À une heure précise", "🕐",
                 "Chaque jour, ou seulement certains jours",
                 modal="ScheduleInputModal"),
    _voice("voice_first_join", "Premier arrivé", "🥇",
           "Un membre rejoint un salon jusque-là vide"),
    _voice("voice_join", "Rejoint un salon", "🟢",
           "N'importe quel membre se connecte au vocal"),
    _voice("voice_leave", "Quitte un salon", "🔴",
           "N'importe quel membre se déconnecte du vocal"),
    _voice("voice_move", "Change de salon", "🔀",
           "Un membre passe d'un salon vocal à un autre"),
    TriggerBlock("voice_count_reached", "Le salon atteint X membres", "👥",
                 "Se déclenche au nombre exact indiqué",
                 modal="CountInputModal"),
    _voice("voice_mute", "Se coupe le micro", "🔇", "Un membre se met en sourdine"),
    _voice("voice_unmute", "Réactive son micro", "🔊", "Un membre quitte la sourdine"),
    _voice("voice_deafen", "Coupe le son", "🚫", "Un membre se met en sourdine totale"),
    _voice("voice_undeafen", "Réactive le son", "🎧", "Un membre réactive son casque"),
    _voice("voice_stream_start", "Lance un partage d'écran", "📺",
           "Un membre commence à streamer"),
    _voice("voice_stream_stop", "Arrête le partage d'écran", "📵",
           "Un membre arrête de streamer"),
    _voice("voice_video_start", "Allume sa caméra", "📹", "Un membre active sa webcam"),
    _voice("voice_video_stop", "Éteint sa caméra", "📷", "Un membre coupe sa webcam"),
    TriggerBlock("message", "Un message contient un mot", "💬",
                 "Nécessite l'intent « Message Content »",
                 modal="KeywordTriggerModal"),
    TriggerBlock("reaction", "Une réaction est ajoutée", "⭐",
                 "Sur n'importe quel message du serveur",
                 modal="ReactionTriggerModal"),
]


# Libellés des événements vocaux, pour l'affichage des routines existantes
EVENT_LABELS = {
    "voice_first_join": "🥇 un membre arrive dans un salon vide",
    "voice_join": "🟢 un membre rejoint un salon",
    "voice_leave": "🔴 un membre quitte un salon",
    "voice_move": "🔀 un membre change de salon",
    "voice_mute": "🔇 un membre coupe son micro",
    "voice_unmute": "🔊 un membre réactive son micro",
    "voice_deafen": "🚫 un membre coupe le son",
    "voice_undeafen": "🎧 un membre réactive le son",
    "voice_stream_start": "📺 un membre lance un partage d'écran",
    "voice_stream_stop": "📵 un membre arrête son partage d'écran",
    "voice_video_start": "📹 un membre allume sa caméra",
    "voice_video_stop": "📷 un membre éteint sa caméra",
}


def describe_trigger(trigger_type: str, trigger_data: dict) -> str:
    """
    Décrit un déclencheur enregistré, en une ligne lisible.

    Args:
        trigger_type: Type du déclencheur (timer, schedule, event)
        trigger_data: Données associées

    Returns:
        Description courte, préfixée d'un émoji
    """
    trigger_data = trigger_data or {}

    if trigger_type == 'timer':
        low = trigger_data.get('interval_min')
        high = trigger_data.get('interval_max')
        if low is not None and high is not None:
            return (f"⏰ toutes les {format_duration(low)} à "
                    f"{format_duration(high)} (au hasard)")
        seconds = trigger_data.get('interval_seconds', 0)
        return f"⏰ toutes les {format_duration(seconds)}"

    if trigger_type == 'schedule':
        days = trigger_data.get('days') or []
        day_names = ["lundi", "mardi", "mercredi", "jeudi",
                     "vendredi", "samedi", "dimanche"]
        when = (", ".join(day_names[d] for d in days) if days else "tous les jours")
        return f"🕐 à {trigger_data.get('time')}, {when}"

    event = trigger_data.get('event', '?')

    if event == 'voice_count_reached':
        return f"👥 le salon atteint {trigger_data.get('count', '?')} membres"

    if event == 'message':
        base = f"💬 un message contient « {trigger_data.get('keyword', '')} »"
        return base + _describe_channel_scope(trigger_data)

    if event == 'reaction':
        base = f"⭐ on réagit avec {trigger_data.get('emoji', '')}"
        return base + _describe_channel_scope(trigger_data)

    return EVENT_LABELS.get(event, f"⚡ {event}")


def _describe_channel_scope(trigger_data: dict) -> str:
    """
    Décrit la restriction de salons d'un déclencheur, s'il y en a une.

    Args:
        trigger_data: Données du déclencheur

    Returns:
        Un complément de phrase, ou une chaîne vide si aucun salon n'est
        imposé (le déclencheur vaut alors pour tout le serveur)
    """
    channels = trigger_data.get('channels') or []
    if not channels:
        return ""

    if len(channels) == 1:
        return f" dans <#{channels[0]}>"
    return f" dans {len(channels)} salons"


def trigger_by_key(key: str) -> Optional[TriggerBlock]:
    """
    Retrouve un déclencheur du menu par sa clé.

    Args:
        key: Valeur renvoyée par le menu

    Returns:
        Le déclencheur correspondant, ou None
    """
    return next((t for t in TRIGGER_MENU if t.key == key), None)


# =============================================================================
# CATALOGUE DES CONDITIONS
# =============================================================================

CONDITION_BLOCKS: Dict[str, ConditionBlock] = {
    block.type: block for block in [
        ConditionBlock(
            type="user_id",
            picker="user",
            handler="_check_user",
            describe=_describe_user,
            label="Membre concerné",
            emoji="👤",
            hint="Seulement pour un ou plusieurs membres précis",
            value_label="ID du membre",
            value_placeholder="123456789012345678, ou plusieurs séparés par des virgules",
            aliases=("user", "utilisateur"),
            syntax="`user=ID` — listes possibles : `user=1,2`",
        ),
        ConditionBlock(
            type="channel_id",
            picker="channel",
            handler="_check_channel",
            describe=_describe_channel,
            label="Salon concerné",
            emoji="🔊",
            hint="Seulement dans un ou plusieurs salons précis",
            value_label="ID du salon",
            value_placeholder="123456789012345678",
            aliases=("channel", "salon"),
            syntax="`channel=ID` — le salon concerné",
        ),
        ConditionBlock(
            type="role_id",
            picker="role",
            handler="_check_role",
            describe=_describe_role,
            label="Rôle du membre",
            emoji="🎭",
            hint="Seulement si le membre possède ce rôle",
            value_label="ID du rôle",
            value_placeholder="123456789012345678",
            aliases=("role",),
            syntax="`role=ID` — le membre possède ce rôle",
        ),
        ConditionBlock(
            type="time_range",
            handler="_check_time_condition",
            describe=lambda c: f"il est entre {c.get('value')}",
            label="Plage horaire",
            emoji="🕐",
            hint="Seulement à certaines heures de la journée",
            value_label="Plage horaire",
            value_placeholder="18:00-23:00",
            aliases=("time", "heure"),
            ops=("==",),
            syntax="`time=18:00-23:00` — plage horaire",
        ),
        ConditionBlock(
            type="date_range",
            handler="_check_date_condition",
            describe=lambda c: f"on est entre le {c.get('value')}",
            label="Plage de dates",
            emoji="📅",
            hint="Seulement pendant une période de l'année",
            value_label="Plage de dates (JJ/MM-JJ/MM)",
            value_placeholder="01/12-25/12",
            aliases=("date",),
            ops=("==",),
            syntax="`date=01/12-25/12` — plage de dates",
        ),
        ConditionBlock(
            type="weekday",
            handler="_check_weekday",
            describe=_describe_weekday,
            label="Jour de la semaine",
            emoji="📆",
            hint="Seulement certains jours",
            value_label="Jours",
            value_placeholder="lun,ven ou sam,dim",
            aliases=("day", "jour"),
            syntax="`day=lun,ven` — jours de la semaine",
        ),
        ConditionBlock(
            type="member_count",
            handler="_check_member_count",
            describe=lambda c: f"il y a {c.get('op', '==')} {c.get('value')} membres dans le salon",
            label="Nombre de membres",
            emoji="👥",
            hint="Selon le nombre de personnes dans le salon",
            value_label="Nombre de membres",
            value_placeholder="3",
            aliases=("count", "members", "membres"),
            ops=(">=", "==", "!=", ">", "<", "<="),
            syntax="`count>=3` — membres dans le salon (`>` `<` `>=` `<=`)",
        ),
        ConditionBlock(
            type="chance",
            handler="_check_chance_condition",
            describe=lambda c: f"un tirage à {_pct(c.get('value'))}% réussit",
            label="Tirage au sort",
            emoji="🎲",
            hint="N'exécute la suite qu'une fois sur X",
            value_label="Probabilité en %",
            value_placeholder="20",
            aliases=("chance",),
            ops=("==",),
            syntax="`chance=30` — ne se déclenche que 30% du temps",
        ),
        ConditionBlock(
            type="queue_length",
            handler="_check_queue_length",
            describe=lambda c: f"la file contient {c.get('op', '==')} {c.get('value')} son(s)",
            label="Sons en attente",
            emoji="📜",
            hint="Selon la longueur de la file d'attente",
            value_label="Nombre de sons en attente",
            value_placeholder="0",
            aliases=("queue", "file"),
            ops=("==", "!=", ">", "<", ">=", "<="),
            syntax="`queue<3` — nombre de sons dans la file",
        ),
        ConditionBlock(
            type="bot_connected",
            handler="_check_bot_connected",
            describe=lambda c: (
                "le bot est déjà en vocal"
                if str(c.get('value')).lower() in ('true', 'vrai', '1')
                else "le bot n'est pas en vocal"
            ),
            label="Bot connecté au vocal",
            emoji="🤖",
            hint="Selon que le bot est déjà dans un salon",
            value_label="true ou false",
            value_placeholder="false",
            aliases=("connected", "connecte"),
            ops=("==",),
            syntax="`connected=false` — le bot n'est pas déjà en vocal",
        ),
        ConditionBlock(
            type="is_bot",
            handler="_check_is_bot",
            describe=lambda c: (
                "c'est un bot qui a déclenché"
                if str(c.get('value')).lower() in ('true', 'vrai', '1')
                else "ce n'est pas un bot qui a déclenché"
            ),
            label="Déclenché par un bot",
            emoji="🚫",
            hint="Permet d'ignorer les autres bots",
            value_label="true ou false",
            value_placeholder="false",
            aliases=("bot",),
            ops=("==",),
            syntax="`bot=false` — ignore ce que font les autres bots",
        ),
        ConditionBlock(
            type="is_playing",
            handler="_check_is_playing",
            describe=lambda c: (
                "un son est déjà en cours"
                if str(c.get('value')).lower() in ('true', 'vrai', '1')
                else "aucun son n'est en cours"
            ),
            label="Lecture en cours",
            emoji="⏯️",
            hint="Selon qu'un son est déjà joué ou non",
            value_label="true ou false",
            value_placeholder="false",
            aliases=("playing", "lecture"),
            ops=("==",),
            syntax="`playing=false` — seulement si rien n'est en cours",
        ),
    ]
}


# =============================================================================
# ACCÈS AU CATALOGUE
# =============================================================================

def action_by_verb(verb: str) -> Optional[ActionBlock]:
    """
    Retrouve l'action correspondant à un verbe textuel.

    Args:
        verb: Verbe écrit par l'utilisateur

    Returns:
        Le bloc correspondant, ou None
    """
    verb = verb.lower()
    for block in ACTION_BLOCKS.values():
        if verb in block.verbs:
            return block
    return None


def all_action_verbs() -> List[str]:
    """Liste tous les verbes d'action reconnus, triés."""
    return sorted({verb for b in ACTION_BLOCKS.values() for verb in b.verbs})


def condition_by_alias(alias: str) -> Optional[ConditionBlock]:
    """
    Retrouve la condition correspondant à un nom écrit.

    Args:
        alias: Nom utilisé dans la syntaxe textuelle ou la modale

    Returns:
        Le bloc correspondant, ou None
    """
    alias = alias.lower()
    for block in CONDITION_BLOCKS.values():
        if alias in block.aliases or alias == block.type:
            return block
    return None


def all_condition_aliases() -> List[str]:
    """Liste le premier alias de chaque condition, pour les messages d'aide."""
    return [b.aliases[0] if b.aliases else b.type for b in CONDITION_BLOCKS.values()]


def describe_action(action: dict) -> str:
    """
    Décrit une action en une ligne.

    Args:
        action: Données de l'action

    Returns:
        Description courte
    """
    block = ACTION_BLOCKS.get(action.get('type'))
    if block is None:
        return str(action.get('type', '?'))
    try:
        return block.describe(action)
    except Exception:
        return block.type


def describe_condition(condition: dict) -> str:
    """
    Décrit une condition en une ligne.

    Args:
        condition: Données de la condition

    Returns:
        Description courte
    """
    block = CONDITION_BLOCKS.get(condition.get('type'))
    if block is None:
        return str(condition.get('type', '?'))
    try:
        return block.describe(condition)
    except Exception:
        return block.type


def action_syntax_help() -> str:
    """Construit la liste des actions pour /help, depuis le catalogue."""
    return "\n".join(b.syntax for b in ACTION_BLOCKS.values() if b.syntax)


def condition_syntax_help() -> str:
    """Construit la liste des conditions pour /help, depuis le catalogue."""
    return "\n".join(b.syntax for b in CONDITION_BLOCKS.values() if b.syntax)