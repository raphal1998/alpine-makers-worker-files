"""Contrôleur GRBL 1.1 (laser ou CNC) exécuté sur le Worker.

La machine est jointe depuis le Worker, jamais depuis le serveur web : par liaison
série (pyserial, dépendance facultative → erreur explicite si absente) ou par TCP
(cartes Wi-Fi GRBL, port 23 ou 8080, bibliothèque standard, adresse privée seulement).

Sécurité : aucune commande n'est envoyée sans appel explicite. Le laser est coupé
(M5) à la connexion, à la déconnexion et à la fin de chaque job. Un job (``stream``)
exige des sécurités (« interlocks ») satisfaites avant et pendant l'envoi ; il
s'arrête sur ALARM, sur ``error:N`` (exception avec le numéro de ligne), sur
annulation, sur sécurité ouverte et sur inactivité. L'arrêt de protection enchaîne
feed hold (``!``), arrêt broche/laser en pause (0x9E, l'équivalent immédiat de M5 :
un ``M5`` envoyé en ligne resterait bloqué derrière la pause), puis reset (0x18) qui
vide le tampon de planification. Le protocole d'envoi compte les caractères :
jamais plus de 127 octets en vol vers GRBL.

Arrêt incendie (agent 1.34.0, surveillance IA flamme/fumée) : ``fire_stop`` envoie le soft
reset 0x18 seul, tout de suite (pas de ``!`` avant : en M3 le laser resterait allumé pendant
la décélération), puis exige la preuve de l'arrêt par la carte (bannière, puis deux rapports
d'état). Il lève un blocage d'écriture : plus aucune ligne ne part tant qu'un opérateur n'a
pas acquitté (``release_halt``) ; seuls les octets temps réel passent encore. Jamais de
``$X``, ``$H``, ``~`` ni ``!`` automatiques.
"""
import math
import re
import socket
import threading
import time
from collections import deque

try:
    from .equipment_runtime import GRBL_WRITABLE_SETTINGS, local_host, port_number
except ImportError:  # Sur un Worker installé les modules sont à plat.
    from equipment_runtime import GRBL_WRITABLE_SETTINGS, local_host, port_number

RX_BUFFER_SIZE = 127      # 128 octets côté GRBL, moins un pour le terminateur.
BOOT_BANNER_WAIT = 1.5  # s : bannière spontanée d'une carte redémarrée par l'ouverture du port, avant tout envoi
STALL_SECONDS = 10.0   # s : cadrage continu sans progrès machine à l'arrêt → arrêt de protection et motif
READ_SLICE = 0.05       # s : délai de lecture du port série, fixé une fois pour toutes à l'ouverture
SETTLE_QUIET = 0.2      # s : silence attendu après la bannière avant la première commande
SETTLE_LIMIT = 1.0      # s : au plus, pour une carte qui parle encore après son démarrage
RESYNC_GRACE = 0.25     # s : délai après le dernier « ok » avant de conclure que des accusés sont perdus
MAX_LINE_LENGTH = 79      # LINE_BUFFER_SIZE 80 : error:11 au-delà.
MACHINE_KINDS = ("laser", "cnc")
STATES = ("Idle", "Run", "Hold", "Jog", "Alarm", "Door", "Check", "Home", "Sleep")
# Commandes temps réel : un seul octet, traité par GRBL sans passer par le tampon de lignes.
REALTIME = {"status": b"?", "hold": b"!", "resume": b"~", "reset": b"\x18", "jog_cancel": b"\x85", "spindle_stop": b"\x9e",
            # Overrides GRBL 1.1 (agent 1.33.0) : la carte borne elle-même l'avance et la broche à 10–200 %, les rapides à 25/50/100 %.
            "feed_reset": b"\x90", "feed_plus10": b"\x91", "feed_minus10": b"\x92", "feed_plus1": b"\x93", "feed_minus1": b"\x94",
            "rapid_100": b"\x95", "rapid_50": b"\x96", "rapid_25": b"\x97",
            "spindle_reset": b"\x99", "spindle_plus10": b"\x9a", "spindle_minus10": b"\x9b", "spindle_plus1": b"\x9c", "spindle_minus1": b"\x9d",
            # Accessoires pendant un job : bascule de l'arrosage/air assist (M8) et de la brumisation (M7).
            "flood_toggle": b"\xa0", "mist_toggle": b"\xa1"}
REALTIME_BYTES = frozenset(REALTIME.values())
# Arrêt incendie : attente bornée du verrou d'écriture avant l'écriture forcée du reset, cadence des demandes « ? »
# après la bannière, écart minimal entre les deux rapports de preuve, tolérance de position et sorties interdites.
FIRE_STOP_LOCK_WAIT = 1.5
FIRE_STOP_POLL = 0.25
FIRE_STOP_REPORT_GAP = 0.2
FIRE_STOP_POSITION_TOLERANCE = 0.01  # mm
FIRE_STOP_ACTIVE_OUTPUTS = ("S", "C", "F", "M")  # champ A: broche/laser (S, C), air assist/arrosage (F), brumisation (M)
HALTED_MESSAGE = ("Arrêt incendie : envoi vers la graveuse bloqué jusqu’à l’acquittement de l’arrêt par un opérateur "
                  "(aucune reprise automatique).")
# Overrides admis par « kind » ; « power » est la broche de GRBL (octets 0x99–0x9D), c'est-à-dire le laser sur une graveuse.
OVERRIDE_ACTIONS = {"feed": ("reset", "plus10", "minus10", "plus1", "minus1"),
                    "rapid": ("100", "50", "25"),
                    "power": ("reset", "plus10", "minus10", "plus1", "minus1")}
# Sorties accessoires : (commande d'allumage, lettre du champ « A: » du rapport, bascule temps réel).
ACCESSORIES = {"air": ("M8", "F", "flood_toggle"), "mist": ("M7", "M", "mist_toggle")}
BANNER = re.compile(r"^Grbl(?:HAL)?\s+(\d+)\.(\d+)([A-Za-z]?)")
SETTING = re.compile(r"^\$(\d+)=(-?\d+(?:\.\d+)?)")
PROBE = re.compile(r"^\[PRB:([^:\]]+):([01])\]")
OPTIONS = re.compile(r"^\[OPT:([^\]]*)\]")
SERIAL_PORT = re.compile(r"(?:COM\d{1,3}|/dev/[A-Za-z0-9_.\-/]{1,60})")

ALARM_CODES = {
    1: "Fin de course matérielle atteinte : position probablement perdue, homing recommandé.",
    2: "Cible de mouvement hors course machine : position conservée, déverrouillage possible.",
    3: "Reset pendant un mouvement : pas perdus probables, homing recommandé.",
    4: "Palpage : état initial du palpeur incorrect avant le cycle.",
    5: "Palpage : aucun contact dans la course programmée.",
    6: "Homing : reset pendant le cycle.",
    7: "Homing : porte de sécurité ouverte pendant le cycle.",
    8: "Homing : le fin de course ne se relâche pas ; augmenter le retrait ($27) ou vérifier le câblage.",
    9: "Homing : fin de course introuvable dans la distance de recherche.",
    10: "Homing : second fin de course d’axe double non déclenché.",
}

ERROR_CODES = {
    1: "Lettre de mot G-code absente.",
    2: "Valeur numérique invalide ou manquante.",
    3: "Commande « $ » inconnue ou non prise en charge.",
    4: "Valeur négative reçue là où une valeur positive est attendue.",
    5: "Cycle de homing désactivé dans les réglages ($22).",
    6: "Durée minimale d’impulsion de pas inférieure à 3 µs.",
    7: "Lecture EEPROM impossible : réglages par défaut restaurés.",
    8: "Commande « $ » refusée : GRBL doit être à l’arrêt (Idle).",
    9: "G-code verrouillé : machine en alarme ou en jog.",
    10: "Limites logicielles impossibles sans homing activé.",
    11: "Ligne trop longue : non traitée.",
    12: "Valeur de réglage au-delà de la fréquence de pas maximale.",
    13: "Porte de sécurité ouverte : état Door engagé.",
    14: "Ligne de démarrage ou info de build trop longue pour l’EEPROM.",
    15: "Cible de jog hors course machine : commande ignorée.",
    16: "Commande de jog sans « = » ou avec un G-code interdit.",
    17: "Le mode laser exige une sortie PWM.",
    20: "Commande G-code non prise en charge ou invalide.",
    21: "Plusieurs commandes du même groupe modal dans le bloc.",
    22: "Vitesse d’avance (F) non définie.",
    23: "Cette commande G-code exige une valeur entière.",
    24: "Deux commandes utilisant les axes XYZ dans le même bloc.",
    25: "Mot G-code répété dans le bloc.",
    26: "Commande exigeant des mots d’axe XYZ, aucun trouvé.",
    27: "Numéro de ligne N hors de la plage 1 – 9 999 999.",
    28: "Mot P ou L requis manquant dans la commande.",
    29: "Système de coordonnées non pris en charge (G54 à G59 seulement).",
    30: "G53 exige un mode G0 ou G1 actif.",
    31: "Mots d’axe inutilisés alors que G80 est actif.",
    32: "Arc G2/G3 sans mots d’axe dans le plan sélectionné.",
    33: "Cible de mouvement invalide (arc impossible ou palpage sur place).",
    34: "Erreur de calcul de l’arc défini par rayon : fragmenter l’arc.",
    35: "Arc G2/G3 sans décalage IJK dans le plan sélectionné.",
    36: "Mots G-code inutilisés restant dans le bloc.",
    37: "Décalage d’outil G43.1 appliqué à un axe non configuré.",
    38: "Numéro d’outil supérieur au maximum pris en charge.",
}


def describe_alarm(code):
    return ALARM_CODES.get(code, f"Alarme GRBL inconnue ({code}).")


def describe_error(code):
    return ERROR_CODES.get(code, f"Erreur GRBL inconnue ({code}).")


class GrblError(ValueError):
    """Erreur déjà formulée pour l'utilisateur : sans adresse, port ni contenu brut."""


class GrblAlarm(GrblError):
    """GRBL a déclenché une alarme : la machine est verrouillée ($X ou homing)."""

    def __init__(self, code, report=None):
        self.code = code
        self.report = report
        super().__init__(f"ALARM:{code} — {describe_alarm(code)}")


class GrblCommandError(GrblError):
    """GRBL a refusé une ligne (``error:N``), avec son numéro dans le fichier envoyé."""

    def __init__(self, code, line_number=None, command="", report=None):
        self.code = code
        self.line_number = line_number
        self.command = command
        self.report = report
        prefix = f"Ligne {line_number} : " if line_number else ""
        super().__init__(f"{prefix}error:{code} — {describe_error(code)}")


class GrblInterlockError(GrblError):
    """Une sécurité (porte, extraction, présence…) n'est pas satisfaite."""

    def __init__(self, failed):
        self.failed = failed
        super().__init__("Sécurité non satisfaite : " + ", ".join(item["label"] for item in failed) + ".")


class GrblTimeout(GrblError):
    """GRBL n'a pas répondu dans le délai."""


class GrblHalted(GrblError):
    """Arrêt incendie non acquitté : aucune ligne n'est envoyée à la carte (octets temps réel seuls)."""

    def __init__(self, message=None):
        super().__init__(message or HALTED_MESSAGE)


def fire_evidence():
    """Preuves d'un arrêt incendie, vides : forme commune aux résultats confirmés, échoués et impossibles."""
    return {"banner": False, "alarm": None, "state": None, "spindle": None, "feed": None, "position_stable": False,
            "accessories": [], "reports": 0, "forced_write": False}


def fire_unsafe(status):
    """Motif (français) pour lequel un rapport d'état reçu après le reset ne prouve pas l'arrêt, ou None.

    ``FS:`` porte les valeurs COMMANDÉES (pas une mesure) : après un reset, GRBL les remet à zéro ; une valeur
    non nulle dit que le reset n'a pas été appliqué. ``A:`` est absent quand broche et accessoires sont coupés.
    """
    state = status.get("state")
    if state in {"Run", "Jog", "Home"}:
        return f"la machine rapporte encore un mouvement ({state})"
    # Capot ouvert après le reset (opérateur qui intervient) : Door:0 et Door:1 = machine arrêtée, broche coupée ;
    # Door:2 et Door:3 = rétraction ou reprise en cours, donc un mouvement.
    door_stopped = state == "Door" and status.get("substate") in (0, 1)
    if state == "Door" and not door_stopped:
        return f"la carte rapporte l’état Door:{status.get('substate')} (rétraction ou reprise en cours)"
    if state not in {"Idle", "Alarm"} and not door_stopped:
        return f"la carte rapporte l’état {state} au lieu de Idle, Alarm ou Door arrêté"
    spindle = status.get("spindle")
    if spindle is not None and spindle != 0:
        return f"la carte rapporte encore une puissance non nulle (S{_format(spindle)})"
    feed = status.get("feed")
    if feed is not None and feed != 0:
        return f"la carte rapporte encore une avance non nulle (F{_format(feed)})"
    active = [letter for letter in status.get("accessories") or [] if letter in FIRE_STOP_ACTIVE_OUTPUTS]
    if active:
        return "la carte rapporte encore des sorties actives (A:" + "".join(active) + ")"
    return None


def fire_position(status):
    """Position rapportée (MPos de préférence, sinon WPos) : ``(repère, (x, y, z))`` ou None."""
    for key in ("mpos", "wpos"):
        position = status.get(key)
        if position and len(position) >= 3:
            return key, tuple(position[:3])
    return None


def _number(value, label, *, positive=False, limit=1e6):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} invalide.") from None
    if isinstance(value, bool) or not math.isfinite(number) or abs(number) > limit or (positive and number <= 0):
        raise ValueError(f"{label} invalide.")
    return number


def _format(number):
    """Nombre G-code compact : 10.000 → 10, 0.500 → 0.5."""
    text = f"{number:.3f}".rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


def _floats(value):
    return tuple(float(part) for part in value.split(","))


def parse_status(line):
    """Rapport ``?`` de GRBL 1.1 → dictionnaire normalisé.

    Exemples : ``<Idle|MPos:0.000,0.000,0.000|FS:0,0|WCO:0.000,0.000,0.000>`` ou
    ``<Run|WPos:10.5,2.0,0|Bf:15,128|FS:500,800|Ov:100,100,100|A:S>``. La position
    manquante (MPos ou WPos) est déduite de l'autre quand WCO figure dans le rapport ;
    le contrôleur complète sinon avec le dernier WCO reçu.
    """
    text = line.decode("ascii", "replace") if isinstance(line, bytes) else str(line)
    match = re.fullmatch(r"<([^<>]*)>", text.strip())
    if not match:
        raise ValueError("Rapport d’état GRBL invalide.")
    parts = match.group(1).split("|")
    state, _, substate = parts[0].partition(":")
    if state not in STATES:
        raise ValueError("État GRBL inconnu.")
    result = {"state": state, "substate": int(substate) if substate.isdigit() else None,
              "mpos": None, "wpos": None, "wco": None, "feed": None, "spindle": None, "overrides": None,
              "pins": [], "accessories": [], "buffer": None, "line": None}
    try:
        for field in parts[1:]:
            key, _, value = field.partition(":")
            if key == "MPos":
                result["mpos"] = _floats(value)
            elif key == "WPos":
                result["wpos"] = _floats(value)
            elif key == "WCO":
                result["wco"] = _floats(value)
            elif key == "Bf":
                planner, rx = (int(part) for part in value.split(","))
                result["buffer"] = {"planner": planner, "rx": rx}
            elif key == "FS":
                feed, spindle = _floats(value)
                result["feed"], result["spindle"] = feed, spindle
            elif key == "F":
                result["feed"] = float(value)
            elif key == "Ov":
                feed, rapid, spindle = (int(part) for part in value.split(","))
                result["overrides"] = {"feed": feed, "rapid": rapid, "spindle": spindle}
            elif key == "A":
                result["accessories"] = list(value)
            elif key == "Pn":
                result["pins"] = list(value)
            elif key == "Ln":
                result["line"] = int(value)
            elif key:
                result.setdefault("extra", {})[key] = value
    except ValueError:
        raise ValueError("Rapport d’état GRBL invalide.") from None
    return complete_positions(result, result["wco"])


def complete_positions(status, wco):
    """Déduit MPos ou WPos manquante à partir de l'offset de travail connu."""
    if wco and len(wco) >= 3:
        if status["mpos"] and not status["wpos"] and len(status["mpos"]) >= 3:
            status["wpos"] = tuple(round(m - w, 3) for m, w in zip(status["mpos"], wco))
        elif status["wpos"] and not status["mpos"] and len(status["wpos"]) >= 3:
            status["mpos"] = tuple(round(p + w, 3) for p, w in zip(status["wpos"], wco))
    return status


def parse_settings(lines):
    """Réponse de ``$$`` → ``{numéro: valeur}`` (``$32=1`` → ``{32: 1}``)."""
    if isinstance(lines, (str, bytes)):
        lines = lines.decode("ascii", "replace").splitlines() if isinstance(lines, bytes) else lines.splitlines()
    settings = {}
    for line in lines:
        text = line.decode("ascii", "replace") if isinstance(line, bytes) else str(line)
        match = SETTING.match(text.strip())
        if match:
            raw = match.group(2)
            settings[int(match.group(1))] = float(raw) if "." in raw else int(raw)
    return settings


def prepare_gcode(text):
    """Lignes prêtes à l'envoi : ``[(numéro de ligne, texte)]``.

    Commentaires et blancs retirés ; une ligne non ASCII, trop longue ou contenant un
    caractère temps réel (``!``, ``~``, ``?`` : ils agiraient hors de tout contrôle)
    est refusée avant tout envoi.
    """
    prepared = []
    for number, raw in enumerate(str(text or "").splitlines(), 1):
        line = re.sub(r"\([^)]*\)", "", raw).split(";", 1)[0].strip()
        if not line or line == "%":
            continue
        if not line.isascii() or not line.isprintable():
            raise GrblError(f"Ligne {number} : caractère non ASCII ou de contrôle refusé par GRBL.")
        if any(char in "!~?" for char in line):
            raise GrblError(f"Ligne {number} : commande temps réel interdite dans un fichier G-code.")
        if len(line) > MAX_LINE_LENGTH:
            raise GrblError(f"Ligne {number} : {len(line)} caractères, GRBL en accepte {MAX_LINE_LENGTH} au plus.")
        prepared.append((number, line))
    return prepared


_GCODE_WORD = re.compile(r"([A-Za-z])\s*([-+]?(?:\d*\.\d+|\d+\.?))")


def gcode_emits(line):
    """True si la ligne allume le laser ou la broche : mot M3/M4, ou mot S strictement positif."""
    for letter, value in _GCODE_WORD.findall(str(line or "")):
        number = float(value)
        if letter.upper() == "M" and number in (3.0, 4.0):
            return True
        if letter.upper() == "S" and number > 0:
            return True
    return False


# Retours vers une position prédéfinie et décalages : G28/G30 (et G28.1/G30.1), G53, G92 (G92.x), G10.
_MDI_FORBIDDEN_G = re.compile(r"G\s*0*(?:28|30|53|92|10)(?:\.\d+)?(?!\d)", re.I)


def mdi_forbidden(line):
    """True si la ligne (déjà nettoyée par ``prepare_gcode``) contient un retour prédéfini ou un décalage."""
    return bool(_MDI_FORBIDDEN_G.search(str(line or "")))


UNKNOWN_POSITION = object()   # ligne après laquelle la position n'est plus déductible du G-code
_G_NUMBER = re.compile(r"G\s*0*(\d+(?:\.\d)?)")
_AXIS_WORD = re.compile(r"([XYZ])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))")
# Relatif, pouces, repères et décalages, mouvements vers des points mémorisés, palpage : hors du suivi.
_UNTRACKED_G = {"10", "20", "28", "30", "38.2", "38.3", "38.4", "38.5", "43.1", "53", "54", "55", "56", "57", "58", "59", "91", "92"}


class Endpoints:
    """Position d'arrivée attendue de chaque ligne envoyée (mm, absolu, repère de travail).

    Sert de preuve qu'une ligne a été exécutée quand son « ok » n'est jamais arrivé. ``after`` rend un
    tuple (X, Y, Z), une valeur inconnue restant ``None``. Il rend ``None`` pour une ligne sans mouvement,
    et ``UNKNOWN_POSITION`` quand le G-code ne permet plus de déduire la position. Tout ce qui sort du cas
    simple, G90 en mm dans un repère fixe, arrête le suivi pour tout le job : aucune preuve, donc aucune
    reprise automatique.
    """

    def __init__(self):
        self.axes = [None, None, None]
        self.enabled = True

    def after(self, text):
        upper = text.upper()
        if upper.startswith("$"):
            self.axes = [None, None, None]  # $H, $J=… : position sortie du suivi
            return UNKNOWN_POSITION
        if any(number in _UNTRACKED_G for number in _G_NUMBER.findall(upper)):
            self.enabled = False
        if not self.enabled:
            return UNKNOWN_POSITION
        words = _AXIS_WORD.findall(upper)
        if not words:
            return None
        for axis, value in words:
            self.axes["XYZ".index(axis)] = float(value)
        return tuple(self.axes)


def _near(position, target, tolerance):
    compared = [(value, expected) for value, expected in zip(position, target) if expected is not None and value is not None]
    return bool(compared) and all(abs(value - expected) <= tolerance for value, expected in compared)


def _apart(first, second, gap):
    compared = [(a, b) for a, b in zip(first, second) if a is not None and b is not None]
    return any(abs(a - b) > gap for a, b in compared)


class Transport:
    """Interface minimale d'une liaison GRBL ; les implémentations sont en dessous."""

    def open(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def write(self, data):
        raise NotImplementedError

    def readline(self, timeout=1.0):
        """Une ligne terminée par ``\\n`` (octets), ou ``b""`` si rien dans le délai."""
        raise NotImplementedError

    def abort_output(self):
        """Abandonne l'envoi en attente (arrêt incendie, avant le reset) ; rien par défaut. Renvoie True si fait."""
        return False

    @property
    def in_waiting(self):
        return 0


class SerialTransport(Transport):
    """Liaison série USB. pyserial est facultatif sur le Worker : absent → erreur claire.

    Le délai de lecture est fixé une fois pour toutes à l'ouverture. pyserial reconfigure tout le port à
    chaque affectation de ``timeout``, même inchangée : sous Windows, SetCommState envoie des requêtes de
    contrôle USB à une carte à USB natif. Le faire à chaque réponse limitait une gravure d'image à quelques
    lignes par seconde (Falcon2 Pro, 27/09). Les octets arrivés sont lus d'un bloc et découpés ici en lignes.
    """

    def __init__(self, port, baud=115200):
        if not isinstance(port, str) or not SERIAL_PORT.fullmatch(port):
            raise ValueError("Port série invalide.")
        if isinstance(baud, bool) or not isinstance(baud, int) or not 1200 <= baud <= 1000000:
            raise ValueError("Débit série invalide.")
        self.port, self.baud = port, baud
        self._serial = None
        self._buffer = b""
        self._write_timeout = ()  # classe SerialTimeoutException de pyserial, connue à l'ouverture
        self.drained = 0          # octets en attente vidés à l'ouverture (diagnostic)

    def open(self):
        try:
            import serial  # noqa: dépendance facultative, importée seulement ici
        except ImportError as error:
            raise GrblError("pyserial absent sur ce Worker : installe le paquet « pyserial » ou utilise une liaison TCP.") from error
        if not hasattr(serial, "Serial"):
            # Dossier serial/ vide ou partiel (copie interrompue) : Python y voit un paquet namespace.
            raise GrblError("pyserial incomplet sur ce Worker (dossier serial/ vide) : clique Installer sur cette fiche pour le réinstaller.")
        try:
            self._serial = serial.Serial(self.port, self.baud, timeout=READ_SLICE, write_timeout=5)
        except (OSError, ValueError) as error:
            raise GrblError(f"Port série inaccessible ({type(error).__name__}) : vérifie le câble et le port choisi.") from error
        self._buffer = b""
        self._write_timeout = getattr(serial, "SerialTimeoutException", ())
        self._drain()

    def _drain(self, seconds=0.5):
        """Vide ce que la carte a mis en file avant notre première écriture (une demi-seconde au plus).

        Une carte USB (ESP32 notamment) qui poussait des messages que plus personne ne lisait — Worker
        redémarré, câble rebranché — peut rester bloquée sur son envoi et ne plus lire le port : notre
        première écriture expirerait alors (SerialTimeoutException). Lire d'abord la débloque.
        """
        port = self._serial
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                waiting = int(getattr(port, "in_waiting", 0) or 0)
                chunk = port.read(min(waiting, 65536) if waiting else 1)
                if not chunk:
                    break  # rien pendant READ_SLICE : la file est vide
                self.drained += len(chunk)
            port.reset_input_buffer()
        except (OSError, ValueError) as error:
            # Vidange facultative : une vraie panne de liaison ressortira dès l'écriture qui suit.
            self.drain_error = type(error).__name__

    def close(self):
        port, self._serial = self._serial, None
        if port is not None:
            port.close()

    def _require(self):
        if self._serial is None:
            raise GrblError("Liaison série non ouverte.")
        return self._serial

    def write(self, data):
        port = self._require()
        try:
            port.write(data)
        except self._write_timeout as error:
            # La carte n'accepte plus les données : on annule l'envoi en attente et on dit quoi faire.
            try:
                port.reset_output_buffer()
            except (OSError, ValueError):
                pass  # le port est de toute façon refermé par l'appelant
            raise GrblError(f"La graveuse n’accepte plus les données sur {self.port} (écriture bloquée plus de 5 s) : "
                            "éteins-la, rallume-la capot fermé et attends la reconnexion automatique ; "
                            "si ça persiste, débranche puis rebranche son câble USB.") from error

    def abort_output(self):
        """Purge l'envoi en attente : ``reset_output_buffer`` (PurgeComm TXABORT|TXCLEAR sous Windows).

        Une écriture bloquée dans un autre fil rend alors la main sans erreur (pyserial : « canceled IO is no
        error ») et ses octets non partis sont perdus. L'arrêt incendie s'en sert AVANT d'écrire le reset, jamais
        après : la purge effacerait le reset lui-même s'il n'était pas encore parti.
        """
        port = self._serial
        if port is None:
            return False
        try:
            port.reset_output_buffer()
        except (OSError, ValueError):
            return False
        return True

    def readline(self, timeout=1.0):
        """Une ligne complète, fin de ligne comprise, ou ``b""`` si aucune n'arrive dans le délai.

        Tout ce que la carte a déjà envoyé est lu d'un seul appel ; les lignes suivantes sortent
        ensuite du tampon sans toucher au port. Le délai du port n'est jamais modifié.
        """
        port = self._require()
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            end = self._buffer.find(b"\n")
            if end >= 0:
                line, self._buffer = self._buffer[:end + 1], self._buffer[end + 1:]
                return line
            waiting = int(port.in_waiting or 0)
            if waiting:
                self._buffer += port.read(min(waiting, 65536))
                if b"\n" not in self._buffer and time.monotonic() >= deadline:
                    return b""  # ligne incomplète : elle se termine à l'appel suivant
                continue
            if time.monotonic() >= deadline:
                return b""
            self._buffer += port.read(1)  # attend l'octet suivant, READ_SLICE au plus

    @property
    def in_waiting(self):
        return len(self._buffer) + (int(self._serial.in_waiting) if self._serial is not None else 0)


class TcpTransport(Transport):
    """Liaison TCP vers une carte GRBL Wi-Fi (port 23 ou 8080) : adresse privée uniquement."""

    def __init__(self, host, port=23, connect_timeout=5.0):
        self.host = local_host(host)
        self.port = port_number(port, 23)
        self.connect_timeout = float(connect_timeout)
        self._socket = None
        self._buffer = b""

    def open(self):
        try:
            self._socket = socket.create_connection((self.host, self.port), self.connect_timeout)
        except OSError as error:
            raise GrblError(f"Connexion TCP à la carte GRBL impossible ({type(error).__name__}).") from error
        self._buffer = b""

    def close(self):
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _require(self):
        if self._socket is None:
            raise GrblError("Liaison TCP non ouverte.")
        return self._socket

    def write(self, data):
        try:
            self._require().sendall(data)
        except OSError as error:
            raise GrblError("Liaison TCP GRBL interrompue à l’envoi.") from error

    def abort_output(self):
        """Rien à purger côté TCP : un ``sendall`` en cours ne s'annule pas sans fermer la liaison. L'arrêt
        incendie attend alors le verrou d'écriture au plus FIRE_STOP_LOCK_WAIT, puis écrit hors verrou."""
        return False

    def _fill(self, timeout):
        sock = self._require()
        sock.settimeout(timeout)
        try:
            chunk = sock.recv(4096)
        except (socket.timeout, BlockingIOError):
            return False
        except OSError as error:
            raise GrblError("Liaison TCP GRBL interrompue.") from error
        if not chunk:
            raise GrblError("Liaison TCP GRBL fermée par la carte.")
        self._buffer += chunk
        return True

    def readline(self, timeout=1.0):
        deadline = time.monotonic() + max(0.0, float(timeout))
        while b"\n" not in self._buffer:
            if not self._fill(max(0.0, deadline - time.monotonic())):
                return b""
        line, separator, self._buffer = self._buffer.partition(b"\n")
        return line + separator

    @property
    def in_waiting(self):
        if self._socket is not None:
            try:
                self._fill(0.0)
            except GrblError:
                pass
        return len(self._buffer)


class GrblController:
    """Pilotage d'une carte GRBL 1.1 via un ``Transport``.

    ``on_status(dict)`` reçoit chaque rapport d'état, ``on_log(str)`` les messages ;
    ``now`` remplace l'horloge (tests). ``interlocks`` est une fonction renvoyant
    ``[{"key", "label", "satisfied"}]`` vérifiée avant un job ou un test laser et
    toutes les ``interlock_every`` lignes pendant un job.
    """

    def __init__(self, transport, machine_kind, on_status=None, on_log=None, now=time.time, interlocks=None,
                 idle_timeout=30.0, interlock_every=20, status_interval=0.5, poll_interval=0.05):
        if machine_kind not in MACHINE_KINDS:
            raise ValueError("Type de machine GRBL invalide (laser ou cnc).")
        self.transport = transport
        self.machine_kind = machine_kind
        self.on_status = on_status
        self.on_log = on_log
        self.now = now
        self.interlocks = interlocks
        self.idle_timeout = float(idle_timeout)
        self.interlock_every = max(1, int(interlock_every))
        self.status_interval = float(status_interval)
        self.poll_interval = float(poll_interval)
        self.rx_buffer = RX_BUFFER_SIZE
        self.connected = False
        self.version = None
        self._version_number = (0, 0)
        self.settings = {}
        self.build_info = []
        self.last_status = None
        self.wco = None
        self.alarm = None
        self.messages = deque(maxlen=50)
        self.job = None
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._streaming = threading.Event()
        self._cancel = threading.Event()
        # Arrêt incendie : blocage d'écriture (jusqu'à l'acquittement), arrêts en série, compteur d'arrêts.
        # Pas de verrou persistant ici : un nouveau contrôleur (reconnexion) repart débloqué pour sa poignée
        # de main ; le verrou persistant est appliqué par la garde de départ du relais.
        self._halt = threading.Event()
        self._fire_lock = threading.Lock()
        self._fire_reports = None   # pendant un arrêt : rapports d'état reçus, avec le nombre de bannières à leur réception
        self.fire_stops = 0
        # Marqueurs tenus par _classify / _remember_status, quel que soit le fil qui lit la liaison.
        self.banner_seq = 0
        self.banner_at = None       # time.monotonic() de la dernière bannière
        self.status_seq = 0
        self.alarm_seq = 0
        self.last_alarm_code = None

    # ------------------------------------------------------------------ liaison
    def _write(self, data):
        with self._write_lock:
            # Vérifié sous le verrou : une écriture qui l'obtient après l'arrêt incendie ne part jamais.
            if self._halt.is_set() and not (len(data) == 1 and bytes(data) in REALTIME_BYTES):
                raise GrblHalted()
            self.transport.write(data)

    def _realtime(self, name):
        self._write(REALTIME[name])

    def _read(self, timeout):
        """Une ligne décodée sans fin de ligne, ou None si rien dans le délai."""
        raw = self.transport.readline(timeout)
        return raw.decode("ascii", "replace").strip() if raw else None

    def _log(self, message):
        self.messages.append(message)
        if self.on_log:
            try:
                self.on_log(message)
            except Exception:
                pass  # Un journal défaillant ne doit pas interrompre une commande.

    def _remember_status(self, status):
        status["received_at"] = time.monotonic()
        if status["wco"]:
            self.wco = status["wco"]
        else:
            complete_positions(status, self.wco)
        self.last_status = status
        reports = self._fire_reports
        if reports is not None:
            reports.append((self.banner_seq, status))
        self.status_seq += 1
        if status["state"] != "Alarm":
            self.alarm = None
        if self.on_status:
            try:
                self.on_status(status)
            except Exception:
                pass

    def _classify(self, line):
        """Interprète une ligne reçue : ``(genre, charge)``."""
        if not line:
            return "empty", None
        if line == "ok":
            return "ok", None
        if line.startswith("error:"):
            return "error", int(line[6:]) if line[6:].isdigit() else 0
        if line.startswith("ALARM:"):
            code = int(line[6:]) if line[6:].isdigit() else 0
            self.alarm = code
            self.last_alarm_code = code
            self.alarm_seq += 1
            self._log(f"ALARM:{code} — {describe_alarm(code)}")
            return "alarm", code
        if line.startswith("<") and line.endswith(">"):
            try:
                status = parse_status(line)
            except ValueError:
                return "other", line
            self._remember_status(status)
            return "status", status
        banner = BANNER.match(line)
        if banner:
            self._version_number = (int(banner.group(1)), int(banner.group(2)))
            self.version = banner.group(1) + "." + banner.group(2) + banner.group(3)
            self.banner_at = time.monotonic()
            self.banner_seq += 1
            return "banner", self.version
        if line.startswith("[MSG:"):
            self._log(line[5:-1] if line.endswith("]") else line[5:])
            return "feedback", line
        if line.startswith(("[", "$", ">")):
            return "feedback", line
        return "other", line

    def _expect_ok(self, command, timeout, tolerate=()):
        """Envoie une ligne et attend « ok » ; renvoie les lignes de retour intermédiaires."""
        self._write(command.encode("ascii") + b"\n")
        feedback = []
        deadline = self.now() + timeout
        while True:
            remaining = deadline - self.now()
            if remaining <= 0:
                raise GrblTimeout(f"Aucune réponse de GRBL à « {command} » en {timeout:g} s.")
            line = self._read(min(self.poll_interval, remaining))
            if line is None:
                continue
            kind, payload = self._classify(line)
            if kind == "ok":
                return feedback
            if kind == "error":
                if payload in tolerate:
                    return feedback
                raise GrblCommandError(payload, command=command)
            if kind == "alarm":
                raise GrblAlarm(payload)
            if kind == "banner":
                raise GrblError("GRBL s’est réinitialisé pendant la commande.")
            if kind in {"feedback", "other"}:
                feedback.append(line)

    def _settle(self, quiet=SETTLE_QUIET, limit=SETTLE_LIMIT):
        """Après la bannière, laisse passer ce que la carte envoie encore avant la première commande.

        Un « ok » resté en file serait pris pour la réponse à ``$$`` : réglages vides, mode laser
        affiché comme désactivé, et chaque réponse suivante décalée d'une commande.
        """
        started = last = self.now()
        while self.now() - started < limit:
            line = self._read(min(self.poll_interval, quiet))
            if line is None:
                if self.now() - last >= quiet:
                    return
                continue
            last = self.now()
            self._classify(line)  # messages et alarme retenus ; un « ok » isolé est simplement écarté

    def _wait_banner(self, timeout):
        deadline = self.now() + timeout
        while True:
            remaining = deadline - self.now()
            if remaining <= 0:
                raise GrblTimeout("Aucune bannière Grbl reçue : vérifie la liaison, le débit (115200) et l’alimentation de la carte.")
            line = self._read(min(self.poll_interval, remaining))
            if line is not None and self._classify(line)[0] == "banner":
                return self.version

    def _status_sync(self, timeout):
        self._realtime("status")
        deadline = self.now() + timeout
        while True:
            remaining = deadline - self.now()
            if remaining <= 0:
                raise GrblTimeout("Aucun rapport d’état reçu de GRBL.")
            line = self._read(min(self.poll_interval, remaining))
            if line is None:
                continue
            kind, payload = self._classify(line)
            if kind == "status":
                return payload

    def _wait_motion_stopped(self, timeout):
        """Après ``!`` : attendre la fin de la décélération (Hold:0) avant un reset sans alarme."""
        deadline = self.now() + timeout
        asked_at = None
        while self.now() < deadline:
            if asked_at is None or self.now() - asked_at >= self.status_interval:
                self._realtime("status")
                asked_at = self.now()
            line = self._read(self.poll_interval)
            if line is None:
                continue
            kind, payload = self._classify(line)
            if kind == "status" and (payload["state"] in {"Idle", "Alarm"} or (payload["state"] in {"Hold", "Door"} and payload["substate"] == 0)):
                return True
            if kind == "alarm":
                return True
        return False

    def _protective_stop(self):
        """Pause immédiate, laser/broche coupés en pause (0x9E), puis reset qui vide le tampon."""
        self._realtime("hold")
        self._realtime("spindle_stop")
        self._wait_motion_stopped(5.0)
        self._realtime("reset")
        try:
            self._wait_banner(5.0)
        except GrblTimeout:
            self._log("Aucune bannière après le reset de protection.")
            return
        try:
            self._status_sync(2.0)
        except GrblError:
            pass

    def _failed_interlocks(self, provider):
        if provider is None:
            return []
        try:
            conditions = list(provider() or [])
        except Exception:
            return [{"key": "interlocks", "label": "Sécurités indisponibles", "satisfied": False}]
        failed = []
        for item in conditions:
            if not isinstance(item, dict) or item.get("satisfied") is not True:
                key = str(item.get("key") or "") if isinstance(item, dict) else ""
                label = str(item.get("label") or key or "Sécurité") if isinstance(item, dict) else "Sécurité"
                failed.append({"key": key, "label": label, "satisfied": False})
        return failed

    def _require_interlocks(self, provider):
        failed = self._failed_interlocks(provider)
        if failed:
            raise GrblInterlockError(failed)

    def _require_ready(self):
        if self._halt.is_set():
            raise GrblHalted()
        if self._streaming.is_set():
            raise GrblError("Un job GRBL est en cours : commande refusée.")
        if not self.connected:
            raise GrblError("Contrôleur GRBL non connecté.")

    def _reset_power_override(self, timeout=2.0):
        """Retour de l'override de puissance à 100 % (0x99) avant toute émission, vérifié sur le rapport d'état.

        GRBL applique l'override de broche à tout ``M3 S…`` : un « +10 % » resté actif ferait émettre un test
        laser à 20 % ou un cadrage capot ouvert à 5 % bien au-delà de leur borne. Une carte qui n'émet pas
        ``Ov:`` (le champ ne sort qu'à son changement) est crue sur parole. Renvoie le rapport lu.
        """
        self._realtime("spindle_reset")
        status = self._status_sync(timeout)
        overrides = status.get("overrides")
        if overrides and overrides.get("spindle") != 100:
            raise GrblError(f"Override de puissance encore actif ({overrides.get('spindle')} %) : réinitialise la carte (0x18) avant toute émission.")
        return status

    # ---------------------------------------------------------------- commandes
    def connect(self, timeout=5.0):
        """Ouvre la liaison, reset, attend la bannière Grbl 1.1, lit ``$$`` et ``$I``."""
        with self._lock:
            if self._streaming.is_set():
                raise GrblError("Un job GRBL est en cours.")
            self.transport.open()
            try:
                # Carte à USB natif (ESP32) : l'ouverture du port la redémarre souvent et elle ne lit rien pendant
                # son démarrage. Sa bannière arrive alors d'elle-même : on l'attend avant toute écriture, et on
                # n'envoie le reset (Ctrl-X) qu'à défaut.
                try:
                    self._wait_banner(min(timeout, BOOT_BANNER_WAIT))
                except GrblTimeout:
                    self._realtime("reset")
                    self._wait_banner(timeout)
                if self._version_number < (1, 1):
                    raise GrblError(f"GRBL {self.version} non pris en charge : version 1.1 requise.")
                self._settle()
                self.settings = parse_settings(self._expect_ok("$$", timeout))
                if not self.settings:
                    # Un « ok » isolé a devancé la réponse : on laisse passer la vraie liste, puis on relit.
                    self._settle()
                    self.settings = parse_settings(self._expect_ok("$$", timeout))
                self.build_info = self._expect_ok("$I", timeout)
                for line in self.build_info:
                    options = OPTIONS.match(line)
                    parts = options.group(1).split(",") if options else []
                    if len(parts) >= 3 and parts[2].isdigit() and int(parts[2]) > 1:
                        self.rx_buffer = min(RX_BUFFER_SIZE, int(parts[2]) - 1)
                if self.machine_kind == "laser":
                    # Laser coupé dès la connexion, même si la machine est verrouillée (error:9).
                    self._expect_ok("M5", timeout, tolerate=(9,))
                self.connected = True
                return self._status_sync(timeout)
            except Exception:
                self.connected = False
                self.transport.close()
                raise

    def disconnect(self):
        """Annule un job éventuel, coupe le laser, ferme la liaison."""
        if self._streaming.is_set():
            self._cancel.set()
            deadline = time.monotonic() + 10
            while self._streaming.is_set() and time.monotonic() < deadline:
                time.sleep(0.05)
        with self._lock:
            if self.connected and self.machine_kind == "laser":
                try:
                    self._expect_ok("M5", 2.0, tolerate=(9,))
                except (GrblError, OSError):
                    pass
            self.connected = False
            self.transport.close()

    def status(self, timeout=1.0):
        """Envoie ``?`` et renvoie le rapport parsé (pendant un job : le dernier reçu)."""
        if self._streaming.is_set():
            self._realtime("status")
            return self.last_status
        with self._lock:
            return self._status_sync(timeout)

    def home(self, timeout=120.0):
        with self._lock:
            self._require_ready()
            self._expect_ok("$H", timeout)
            self.alarm = None
            return self._status_sync(2.0)

    def unlock(self):
        with self._lock:
            self._require_ready()
            self._expect_ok("$X", 5.0)
            self.alarm = None
            return self._status_sync(2.0)

    def reset(self, timeout=5.0):
        with self._lock:
            if self._streaming.is_set():
                raise GrblError("Un job GRBL est en cours : utilise l’arrêt d’urgence.")
            self._realtime("reset")
            self._wait_banner(timeout)
            return self._status_sync(2.0)

    def hold(self):
        """Pause immédiate ; sur un laser, la broche est aussi coupée pendant la pause."""
        self._realtime("hold")
        if self.machine_kind == "laser":
            self._realtime("spindle_stop")

    def resume(self):
        self._realtime("resume")

    def cancel_jog(self):
        self._realtime("jog_cancel")

    def emergency_stop(self):
        """Arrêt d'urgence, utilisable à tout moment : pause, laser coupé, reset."""
        self._realtime("hold")
        self._realtime("spindle_stop")
        self._realtime("reset")
        if self._streaming.is_set():
            self._cancel.set()
            return None
        with self._lock:
            self._wait_banner(5.0)
            return self._status_sync(2.0)

    # ------------------------------------------------------------ arrêt incendie
    @property
    def halted(self):
        """Arrêt incendie levé et pas encore acquitté : plus aucune ligne ne part vers la carte."""
        return self._halt.is_set()

    def release_halt(self):
        """Acquittement d'un opérateur : les envois sont de nouveau permis. Rien n'est envoyé ici (ni ``$X``, ni
        homing, ni reprise) ; le verrou persistant de la surveillance reste appliqué par la garde de départ."""
        self._halt.clear()

    def _abort_output(self):
        """Purge de l'envoi en attente (sans verrou) ; une purge impossible n'empêche jamais le reset."""
        abort = getattr(self.transport, "abort_output", None)
        if abort is None:
            return False
        try:
            return bool(abort())
        except Exception:
            return False

    def _fire_write(self, byte, lock_wait, force=False):
        """Un octet temps réel de l'arrêt incendie : ``"written"``, ``"forced"`` (hors verrou) ou None (pas écrit).

        Le verrou d'écriture est attendu au plus ``lock_wait`` s. À défaut, avec ``force``, l'envoi bloqué est
        purgé et l'octet part hors verrou ; sans ``force``, rien n'est écrit. Une erreur du transport remonte.
        """
        if self._write_lock.acquire(timeout=lock_wait):
            try:
                self.transport.write(byte)
            finally:
                self._write_lock.release()
            return "written"
        if not force:
            return None
        self._abort_output()
        self.transport.write(byte)
        return "forced"

    def _fire_pump(self, limit):
        """Lit les réponses de la carte si le verrou de lecture est libre ; sinon le fil qui le tient (job, suivi
        d'état) les lit, et ``_classify`` tient les marqueurs à jour pour l'arrêt en cours."""
        if not self._lock.acquire(timeout=0.05):
            return
        handled = 0
        try:
            line = self._read(max(0.0, min(READ_SLICE, limit - time.monotonic())))
            while line is not None and handled < 64:
                handled += 1
                self._classify(line)
                line = self._read(0)
        finally:
            self._lock.release()
        if not handled:
            time.sleep(0.005)  # transport qui rend la main aussitôt : pas de boucle à vide

    def fire_stop(self, timeout=6.0, banner_timeout=2.0, attempts=3):
        """Arrêt incendie : soft reset GRBL (0x18) seul et immédiat, puis preuve de l'arrêt par la carte.

        GRBL traite 0x18 dès la réception : broche/laser et air coupés, moteurs arrêtés, tampons vidés ;
        ALARM:3 si la machine bougeait, puis la bannière. Aucun ``!`` avant (le laser resterait allumé pendant
        la décélération en M3), jamais ``~``, ``$X``, ``$H`` ni ligne G-code. Les octets reçus avant le reset
        sont jetés par la carte, ceux reçus après seraient exécutés : l'écriture est donc bloquée (``_halt``)
        et l'envoi en attente purgé AVANT le reset. Le job en cours s'arrête de lui-même (ALARM, bannière ou
        écriture refusée) ; ``_cancel`` n'est pas levé, pour qu'aucun arrêt de protection (``!``) ne suive.

        Confirmé seulement avec une bannière puis deux rapports d'état reçus après elle, à 0,2 s d'écart au
        moins : état Idle ou Alarm, avance et puissance commandées nulles si rapportées, aucune sortie active
        dans ``A:``, même position à 0,01 mm. Horloge ``time.monotonic`` (pas ``self.now``). Renvoie
        ``{"status": "confirmed"|"failed", "requested_at", "written_at", "confirmed_at", "elapsed_ms",
        "attempts", "message", "evidence"}`` ; ne lève jamais.
        """
        requested_at = time.time()
        started = time.monotonic()
        self._halt.set()  # d'abord : plus aucune ligne ne part, quel que soit le fil
        with self._fire_lock:
            self.fire_stops += 1
            self._abort_output()  # débloque une écriture en cours : le reset ne doit pas attendre derrière elle
            self._fire_reports = deque(maxlen=256)
            try:
                return self._fire_confirm(requested_at, started, timeout, banner_timeout, attempts)
            finally:
                self._fire_reports = None

    def _fire_confirm(self, requested_at, started, timeout, banner_timeout, attempts):
        deadline = started + max(0.2, float(timeout))
        banner_wait = max(0.05, float(banner_timeout))
        tries = max(1, int(attempts))
        banner_before, alarm_before = self.banner_seq, self.alarm_seq
        evidence = fire_evidence()
        result = {"status": "failed", "requested_at": requested_at, "written_at": None, "confirmed_at": None,
                  "elapsed_ms": None, "attempts": 0, "message": "", "evidence": evidence}
        action = " Coupe immédiatement l’alimentation de la graveuse et surveille-la."

        def finish(status, message):
            if self.alarm_seq != alarm_before:
                evidence["alarm"] = self.last_alarm_code
            elapsed = round((time.monotonic() - started) * 1000, 1)
            if status == "confirmed":
                alarm = evidence["alarm"]
                message = (f"Arrêt incendie confirmé par la carte en {elapsed:.0f} ms : reset GRBL (0x18) reçu, laser et "
                           f"mouvement coupés (état {evidence['state']}"
                           + (f", ALARM:{alarm} — {describe_alarm(alarm)}" if alarm else "")
                           + "). Aucun envoi vers la graveuse avant l’acquittement d’un opérateur.")
                result["confirmed_at"] = time.time()
            result.update(status=status, message=message, elapsed_ms=elapsed)
            self._log(message)
            return result

        def detail(error):
            return str(error) if isinstance(error, GrblError) and str(error) else type(error).__name__

        try:
            # Reset, renvoyé tant qu'aucune bannière ne répond (octet perdu, carte occupée), dans la limite des essais.
            while True:
                try:
                    written = self._fire_write(REALTIME["reset"], FIRE_STOP_LOCK_WAIT, force=True)
                except Exception as error:
                    return finish("failed", f"Arrêt incendie NON confirmé : écriture impossible du reset (0x18) vers la "
                                            f"graveuse ({detail(error)})." + action)
                result["attempts"] += 1
                if written == "forced":
                    evidence["forced_write"] = True
                if result["written_at"] is None:
                    result["written_at"] = time.time()
                limit = min(deadline, time.monotonic() + banner_wait)
                while self.banner_seq == banner_before and time.monotonic() < limit:
                    self._fire_pump(limit)
                if self.banner_seq != banner_before:
                    break
                if result["attempts"] >= tries or time.monotonic() >= deadline:
                    return finish("failed", f"Arrêt incendie NON confirmé : aucune bannière GRBL après {result['attempts']} envoi(s) "
                                            "du reset (0x18), la carte ne répond pas." + action)
            evidence["banner"] = True
            # Preuve : deux rapports d'état après la bannière, sûrs, à la même position, espacés d'au moins 0,2 s.
            anchor = anchor_position = None
            problem = None
            next_poll = time.monotonic()
            while time.monotonic() < deadline:
                if time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + FIRE_STOP_POLL
                    try:
                        # Jamais forcée : si un envoi bloqué tient encore la liaison, ses octets pourraient partir
                        # après le reset ; sans rapport frais, l'arrêt n'est pas confirmé.
                        if self._fire_write(REALTIME["status"], FIRE_STOP_POLL) is None:
                            problem = "demande d’état impossible : un envoi bloqué tient encore la liaison et pourrait atteindre la carte"
                    except Exception as error:
                        problem = f"demande d’état impossible ({detail(error)})"
                self._fire_pump(min(deadline, next_poll))
                reports = self._fire_reports
                while reports:
                    banners, status = reports.popleft()
                    if banners == banner_before:
                        continue  # rapport antérieur au reset
                    evidence["reports"] += 1
                    evidence.update(state=status.get("state"), spindle=status.get("spindle"), feed=status.get("feed"),
                                    accessories=list(status.get("accessories") or []))
                    unsafe = fire_unsafe(status)
                    position = fire_position(status)
                    if unsafe or position is None:
                        problem = unsafe or "rapport d’état sans position"
                        anchor = anchor_position = None
                        continue
                    if (anchor is None or position[0] != anchor_position[0]
                            or not _near(position[1], anchor_position[1], FIRE_STOP_POSITION_TOLERANCE)):
                        if anchor is not None:
                            problem = "la position rapportée change encore"
                        anchor, anchor_position = status, position
                        continue
                    if status["received_at"] - anchor["received_at"] >= FIRE_STOP_REPORT_GAP:
                        evidence["position_stable"] = True
                        return finish("confirmed", "")
            if not evidence["reports"]:
                cause = "aucun rapport d’état après la bannière" + (f" ; {problem}" if problem else "")
            elif anchor is None:
                cause = problem or "aucun rapport d’état exploitable"
            else:
                cause = "pas de second rapport d’état concordant dans le délai" + (f" (avant : {problem})" if problem else "")
            return finish("failed", f"Arrêt incendie NON confirmé : reset reçu (bannière GRBL), mais {cause}." + action)
        except Exception as error:
            return finish("failed", f"Arrêt incendie NON confirmé : liaison interrompue pendant la confirmation "
                                    f"({detail(error)})." + action)

    def jog(self, dx=0, dy=0, dz=0, feed=1000, timeout=5.0):
        """Déplacement relatif ``$J=G91 G21 …`` (annulable par ``cancel_jog``)."""
        axes = {"X": _number(dx, "Déplacement X", limit=10000), "Y": _number(dy, "Déplacement Y", limit=10000), "Z": _number(dz, "Déplacement Z", limit=10000)}
        rate = _number(feed, "Vitesse de jog", positive=True)
        words = [axis + _format(value) for axis, value in axes.items() if value]
        if not words:
            raise ValueError("Aucun déplacement demandé.")
        with self._lock:
            self._require_ready()
            self._expect_ok("$J=G91 G21 " + " ".join(words) + " F" + _format(rate), timeout)
            return True

    def set_work_origin(self, axes="XYZ"):
        """Origine de travail au point courant : ``G10 L20 P1`` sur les axes demandés (X, Y et Z par défaut)."""
        wanted = str(axes or "").upper()
        chosen = "".join(axis for axis in "XYZ" if axis in wanted)
        if not chosen or any(char not in "XYZ" for char in wanted):
            raise ValueError("Axes de l’origine invalides (X, Y, Z ou leur combinaison).")
        with self._lock:
            self._require_ready()
            self._expect_ok("G10 L20 P1 " + " ".join(axis + "0" for axis in chosen), 5.0)
            return True

    def goto_work_origin(self, timeout=5.0):
        """Retour rapide à l'origine de travail, X et Y seulement : Z (focale ou outil) ne bouge jamais ici."""
        with self._lock:
            self._require_ready()
            self._expect_ok("G90 G0 X0 Y0", timeout)
            return True

    def goto_machine_zero(self, timeout=5.0):
        """Retour rapide au zéro machine (``G53``), X et Y seulement ; n'a de sens que sur une machine référencée."""
        with self._lock:
            self._require_ready()
            self._expect_ok("G90 G53 G0 X0 Y0", timeout)
            return True

    def move_abs(self, x=None, y=None, z=None, feed=1000, timeout=5.0):
        """Déplacement absolu ``$J=G90 G21 …`` dans le repère de travail : un jog, donc annulable et sans effet modal."""
        words = [axis + _format(_number(value, f"Position {axis}", limit=10000)) for axis, value in (("X", x), ("Y", y), ("Z", z)) if value is not None]
        if not words:
            raise ValueError("Aucune position demandée.")
        rate = _number(feed, "Vitesse de déplacement", positive=True)
        with self._lock:
            self._require_ready()
            self._expect_ok("$J=G90 G21 " + " ".join(words) + " F" + _format(rate), timeout)
            return True

    def override(self, kind, action):
        """Override temps réel (avance, rapides, broche ou laser) : un seul octet, accepté même pendant un job.

        La carte borne elle-même le résultat (10–200 % pour l'avance et la broche, 25/50/100 % pour les
        rapides) et son rapport d'état suivant porte les valeurs effectives (champ ``Ov:``).
        """
        if action not in OVERRIDE_ACTIONS.get(kind, ()):
            raise ValueError("Override GRBL invalide (feed, rapid ou power ; action inconnue).")
        if not self.connected:
            raise GrblError("Contrôleur GRBL non connecté.")
        name = ("spindle" if kind == "power" else kind) + "_" + action
        self._realtime(name)
        return name

    def read_settings(self, timeout=5.0):
        """Relit ``$$`` et mémorise les réglages ; renvoie ``{numéro: valeur}``."""
        with self._lock:
            self._require_ready()
            self.settings = parse_settings(self._expect_ok("$$", timeout))
            return dict(self.settings)

    def write_setting(self, number, value, timeout=5.0):
        """Écrit un réglage de la liste autorisée (``$n=v``) puis relit ``$$`` ; renvoie les réglages à jour.

        Jamais ``$RST`` ni un réglage hors liste : pas par mm, inversions, fins de course et activation du
        homing ne se modifient pas depuis le dashboard. GRBL refuse l'écriture hors Idle/Alarm (error:8).
        """
        if isinstance(number, bool) or not isinstance(number, int) or number not in GRBL_WRITABLE_SETTINGS:
            raise ValueError(f"Réglage ${number} protégé : il ne se modifie pas depuis le dashboard.")
        amount = _number(value, f"Valeur du réglage ${number}")
        text = str(int(amount)) if amount == int(amount) else _format(amount)
        with self._lock:
            self._require_ready()
            self._expect_ok(f"${number}={text}", timeout)
            self.settings = parse_settings(self._expect_ok("$$", timeout))
            return dict(self.settings)

    def info(self, timeout=5.0):
        """Informations de la carte : ``$I`` (version, options), ``$G`` (état du parseur), ``$#`` (décalages)."""
        with self._lock:
            self._require_ready()
            build = self._expect_ok("$I", timeout)
            parser = self._expect_ok("$G", timeout)
            offsets = self._expect_ok("$#", timeout)
            if build:
                self.build_info = build
            return {"build_info": list(build), "parser_state": list(parser), "offsets": list(offsets)}

    def accessory(self, name, on, timeout=5.0):
        """Air assist / arrosage (``M8``) ou brumisation (``M7``) hors job ; ``M9`` coupe les deux.

        Une carte compilée sans la sortie demandée répond error:20 : dit clairement, rien d'autre n'est tenté.
        """
        if name not in ACCESSORIES:
            raise ValueError("Accessoire GRBL invalide (air ou mist).")
        command = ACCESSORIES[name][0] if on else "M9"
        with self._lock:
            self._require_ready()
            try:
                self._expect_ok(command, timeout)
            except GrblCommandError as error:
                if error.code == 20:
                    label = "air assist (M8)" if name == "air" else "brumisation (M7)"
                    raise GrblError(f"Sortie {label} non prise en charge par ce firmware.") from error
                raise
            return True

    def accessory_toggle(self, name):
        """Bascule temps réel de l'air assist (0xA0) ou de la brumisation (0xA1), utilisable pendant un job."""
        if name not in ACCESSORIES:
            raise ValueError("Accessoire GRBL invalide (air ou mist).")
        if not self.connected:
            raise GrblError("Contrôleur GRBL non connecté.")
        self._realtime(ACCESSORIES[name][2])

    def mdi(self, line, timeout=30.0):
        """Une ligne G-code saisie à la main : nettoyée comme un fichier, jamais une commande ``$`` ; sur un laser,
        aucune émission (M3/M4, S > 0 : le test laser est le seul chemin). Renvoie les lignes de retour de GRBL.
        """
        prepared = prepare_gcode(line)
        if len(prepared) != 1:
            raise ValueError("La console accepte une seule ligne G-code à la fois.")
        text = prepared[0][1]
        if text.startswith("$"):
            raise ValueError("Les commandes $ passent par Paramètres GRBL et Informations, pas par la console.")
        if mdi_forbidden(text):
            raise ValueError("Les retours et décalages (G28/G30/G53/G92/G10) passent par Origine / Zéro machine, pas par la console.")
        if self.machine_kind == "laser" and gcode_emits(text):
            raise GrblError("Émission laser (M3/M4 ou S > 0) refusée en console : utilise le test laser.")
        with self._lock:
            self._require_ready()
            return list(self._expect_ok(text, timeout))

    def probe_z(self, feed, max_travel, timeout=None):
        """Palpage Z descendant (``G38.3`` : sans contact, rapport ``[PRB:…:0]`` et pas d'alarme) ; renvoie la position de contact.

        Le retour en absolu (``G90``) part quoi qu'il arrive : un palpage sans contact laissait sinon la carte
        en G91 et la ligne MDI suivante s'exécutait en relatif.
        """
        rate = _number(feed, "Vitesse de palpage", positive=True)
        travel = _number(max_travel, "Course de palpage", positive=True, limit=500)
        delay = timeout if timeout else travel / rate * 60 + 10
        with self._lock:
            self._require_ready()
            try:
                feedback = self._expect_ok(f"G91 G38.3 Z-{_format(travel)} F{_format(rate)}", delay)
            finally:
                try:
                    self._expect_ok("G90", 5.0, tolerate=(9,))
                except GrblError:
                    self._log("Mode G91 possiblement actif : réinitialise (0x18) avant toute ligne MDI.")
            for line in feedback:
                match = PROBE.match(line)
                if match:
                    position = _floats(match.group(1))
                    if match.group(2) != "1":
                        raise GrblError("Palpage sans contact dans la course demandée.")
                    return {"success": True, "position": position, "z": position[2] if len(position) > 2 else None}
            raise GrblError("Palpage terminé sans rapport [PRB] de GRBL.")

    def laser_test(self, power_pct, ms, interlocks=None):
        """Impulsion laser sur place (M3 S… G4 P… M5), refusée si une sécurité manque."""
        if self.machine_kind != "laser":
            raise GrblError("Test laser réservé aux machines laser.")
        power = _number(power_pct, "Puissance laser", positive=True, limit=100)
        duration = _number(ms, "Durée du test laser", positive=True, limit=5000)
        self._require_interlocks(self.interlocks if interlocks is None else interlocks)
        with self._lock:
            self._require_ready()
            status = self._status_sync(2.0)
            if status["state"] != "Idle":
                raise GrblError(f"Machine non prête pour un test laser (état {status['state']}).")
            self._reset_power_override()  # un « +10 % » resté actif ferait émettre au-delà de la puissance demandée
            maximum = self.settings.get(30) or 1000
            spindle = int(round(maximum * power / 100))
            # En mode laser ($32=1) GRBL ne fire M3 que sous un mode G1/G2/G3 actif.
            laser_mode = self.settings.get(32) == 1
            sequence = (["G1 F100"] if laser_mode else []) + [f"M3 S{spindle}", f"G4 P{_format(duration / 1000)}", "M5"] + (["G0"] if laser_mode else [])
            try:
                for command in sequence:
                    self._expect_ok(command, duration / 1000 + 5)
            except GrblError:
                # Le laser pourrait être allumé : reset immédiat, qui coupe tout.
                self._realtime("reset")
                try:
                    self._wait_banner(5.0)
                except GrblTimeout:
                    pass
                raise
            return {"power_pct": power, "duration_ms": duration, "spindle": spindle}

    def stream(self, gcode_text, progress=None, should_cancel=None, interlocks=None, allow_door=False, should_stop=None, max_pending=None):
        """Envoie un G-code complet par comptage de caractères ; renvoie un rapport.

        Rapport : ``{"ok", "lines", "total_lines", "duration_seconds", "error", "reason"}``
        (``reason`` : None, ``cancelled``, ``stopped``, ``interlock``, ``timeout`` ou ``reset``).
        ``error:N`` lève ``GrblCommandError`` (numéro de ligne), ALARM lève ``GrblAlarm`` ;
        les deux portent le rapport dans ``.report``. ``progress(dict)`` reçoit
        ``{"total", "sent", "done", "percent", "started"}``.

        ``allow_door`` accepte l'état Door au départ (cadrage capot ouvert, laser coupé : la carte
        décide ensuite si elle bouge). ``should_stop()`` vrai = ne plus envoyer de ligne, laisser
        finir celles déjà parties et rendre ``reason="stopped"`` (fin propre, sans reset).
        ``max_pending`` borne les lignes en vol pour qu'un arrêt reste réactif.
        """
        lines = prepare_gcode(gcode_text)
        if not lines:
            raise GrblError("Aucune ligne de G-code à envoyer.")
        provider = self.interlocks if interlocks is None else interlocks
        with self._lock:
            self._require_ready()
            self._require_interlocks(provider)
            status = self._status_sync(2.0)
            if status["state"] == "Alarm":
                raise GrblError("Machine en alarme : déverrouille ($X) ou lance un homing avant un job.")
            if status["state"] != "Idle" and not (allow_door and status["state"] == "Door"):
                raise GrblError(f"Machine non prête (état {status['state']}).")
            self._reset_power_override()  # tout job démarre à 100 % de puissance ; l'opérateur la règle ensuite en direct
            self._streaming.set()
            self._cancel.clear()
            try:
                return self._run_job(lines, provider, progress, should_cancel, should_stop, max_pending)
            finally:
                self._streaming.clear()

    def wait_idle(self, timeout, should_stop=None, door_timeout=None):
        """Attend l'état Idle après un job : les derniers segments s'exécutent après leur « ok ».

        Lève ``GrblError`` en alarme, si la machine reste occupée au-delà de ``timeout`` s, ou si
        elle reste en état Door plus de ``door_timeout`` s (la carte n'exécute aucun mouvement
        capot ouvert). ``should_stop()`` vrai interrompt l'attente sans erreur.
        """
        with self._lock:
            self._require_ready()
            started = self.now()
            asked_at = None
            door_since = None
            while True:
                if should_stop and should_stop():
                    return self.last_status or {}
                if asked_at is None or self.now() - asked_at >= self.status_interval:
                    self._realtime("status")
                    asked_at = self.now()
                line = self._read(self.poll_interval)
                if line is not None:
                    kind, payload = self._classify(line)
                    if kind == "alarm":
                        raise GrblAlarm(payload)
                    if kind == "status":
                        state = payload["state"]
                        if state == "Idle":
                            return payload
                        if state == "Alarm":
                            raise GrblError("Machine en alarme : déverrouille ($X) ou lance un homing.")
                        if state == "Door":
                            door_since = self.now() if door_since is None else door_since
                            if door_timeout is not None and self.now() - door_since >= door_timeout:
                                raise GrblError("Porte de sécurité ouverte (état Door) : la carte n’exécute aucun mouvement tant que le capot est ouvert.")
                        else:
                            door_since = None
                if self.now() - started >= timeout:
                    state = (self.last_status or {}).get("state") or "inconnu"
                    raise GrblError(f"Machine toujours occupée (état {state}) {timeout:g} s après la fin de l’envoi.")

    def _position_tolerance(self):
        """Écart admis entre position rapportée et position programmée : un pas moteur, entre 0,01 et 0,1 mm."""
        steps = [value for value in (self.settings.get(100), self.settings.get(101), self.settings.get(102))
                 if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0]
        return min(0.1, max(0.01, 1.0 / min(steps))) if steps else 0.02

    def _run_job(self, lines, provider, progress, should_cancel, should_stop=None, max_pending=None):
        started = self.now()
        total = len(lines)
        pending = deque()  # (octets envoyés, numéro de ligne, texte, position d'arrivée) non encore acquittés
        in_flight = 0
        index = 0
        done = 0
        last_activity = started
        last_status_at = started
        endpoints = Endpoints()
        reached = None         # position d'arrivée de la dernière ligne de mouvement acquittée
        last_ack_at = started
        status_at = None       # réception du dernier rapport d'état
        lost = 0               # accusés « ok » jamais reçus, lignes prouvées exécutées par la position
        tolerance = self._position_tolerance()
        self.job = {"total": total, "sent": 0, "done": 0, "percent": 0, "started": started}

        def report(ok, error=None, reason=None, **extra):
            result = {"ok": ok, "lines": done, "total_lines": total, "duration_seconds": round(self.now() - started, 3), "error": error,
                      "reason": reason, "lost_acks": lost}
            result.update(extra)
            return result

        def executed_without_ack():
            """Lignes en attente que la machine a prouvé avoir exécutées alors que leur « ok » n'est jamais arrivé.

            Constaté sur la Falcon2 Pro (27/09) : 7 « ok » perdus en 383 293 lignes. Chacun laisse sa ligne
            comptée dans le tampon de la carte ; au septième, le tampon semble plein, plus rien ne part, et la
            machine attend, à l'arrêt, au bout de la dernière ligne reçue. Preuve exigée : un rapport d'état reçu
            au moins RESYNC_GRACE après le dernier « ok », machine Idle, à la position d'arrivée d'une ligne en
            attente qu'aucune autre position possible ne confond. Rend (nombre de lignes, position) ou (0, None).
            """
            if not pending or reached is None or status_at is None or status_at - last_ack_at < RESYNC_GRACE:
                return 0, None
            status = self.last_status or {}
            position = status.get("wpos")
            if status.get("state") != "Idle" or not position:
                return 0, None
            entries = list(pending)
            moves = [offset for offset, entry in enumerate(entries) if isinstance(entry[3], tuple)]
            if moves:
                if any(entry[3] is UNKNOWN_POSITION for entry in entries[:moves[-1] + 1]):
                    return 0, None
                target = entries[moves[-1]][3]
                others = [reached] + [entries[offset][3] for offset in moves[:-1]]
                if _near(position, target, tolerance) and all(_apart(target, other, 2.5 * tolerance) for other in others):
                    return moves[-1] + 1, target
                return 0, None
            # Fin du job : il ne reste que des lignes sans mouvement (M5, M9…), et la machine est arrêtée au bout du
            # dernier mouvement. Leur position ne prouve rien : on attend plus longtemps, et le M5 de fin de job,
            # qui doit recevoir sa propre réponse, vérifie ensuite que la carte lit toujours.
            if (index >= total and all(entry[3] is None for entry in entries) and self.now() - last_ack_at >= 4 * RESYNC_GRACE
                    and _near(position, reached, tolerance)):
                return len(entries), reached
            return 0, None

        def notify():
            self.job.update(sent=index, done=done, percent=round(100 * done / total, 1))
            if progress:
                try:
                    progress(dict(self.job))
                except Exception:
                    pass

        stopping = False
        while (index < total and not stopping) or pending:
            if self._cancel.is_set() or (should_cancel and should_cancel()):
                self._protective_stop()
                return report(False, "Job annulé.", "cancelled")
            if not stopping and should_stop and should_stop():
                stopping = True  # plus aucune ligne n'est envoyée ; celles en vol se terminent normalement
            # Toutes les lignes qui tiennent dans le tampon de la carte partent en une seule écriture : une
            # image tramée compte une ligne par point, et un transfert USB par ligne ralentissait l'envoi.
            batch = bytearray()
            while index < total and not stopping and in_flight + len(lines[index][1]) + 1 <= self.rx_buffer and (max_pending is None or len(pending) < max_pending):
                if index and index % self.interlock_every == 0:
                    if batch:
                        # Les lignes validées par le contrôle précédent partent avant le suivant, comme une à une.
                        self._write(bytes(batch))
                        batch.clear()
                    failed = self._failed_interlocks(provider)
                    if failed:
                        self._protective_stop()
                        return report(False, "Sécurité non satisfaite : " + ", ".join(item["label"] for item in failed) + ".", "interlock", interlocks=failed)
                number, text = lines[index]
                payload = text.encode("ascii") + b"\n"
                batch += payload
                pending.append((len(payload), number, text, endpoints.after(text)))
                in_flight += len(payload)
                index += 1
            if batch:
                self._write(bytes(batch))
                notify()
            line = self._read(self.poll_interval)
            if line is None:
                now = self.now()
                if now - last_status_at >= self.status_interval:
                    self._realtime("status")
                    last_status_at = now
                count, target = executed_without_ack()
                if count:
                    for _ in range(count):
                        in_flight -= pending.popleft()[0]
                    done += count
                    lost += count
                    reached = target
                    last_activity = last_ack_at = now
                    self._log(f"{count} accusé(s) « ok » jamais reçu(s) : la position prouve que la graveuse a exécuté ces lignes, envoi repris.")
                    notify()
                    continue
                if now - last_activity > self.idle_timeout:
                    self._protective_stop()
                    waiting = f" (ligne {pending[0][1]} « {pending[0][2]} » sans réponse)" if pending else ""
                    return report(False, f"GRBL ne répond plus depuis {self.idle_timeout:g} s{waiting} : job interrompu.", "timeout")
                continue
            acked = False
            handled = 0
            while line is not None:
                handled += 1
                kind, payload = self._classify(line)
                if kind == "ok":
                    if pending:  # un « ok » sans ligne en attente n'accuse rien, comme pour le cadrage
                        size, _, _, end = pending.popleft()
                        in_flight -= size
                        if end is UNKNOWN_POSITION:
                            reached = None
                        elif end is not None:
                            reached = end
                        done += 1
                        last_activity = last_ack_at = self.now()
                        acked = True
                elif kind == "error":
                    size, number, text, _ = pending.popleft() if pending else (0, None, "", None)
                    in_flight -= size
                    self._protective_stop()
                    raise GrblCommandError(payload, line_number=number, command=text, report=report(False, f"Ligne {number} refusée (error:{payload}).", "error"))
                elif kind == "alarm":
                    # GRBL a déjà tout arrêté et verrouillé la machine.
                    raise GrblAlarm(payload, report=report(False, f"ALARM:{payload} — {describe_alarm(payload)}", "alarm"))
                elif kind == "status":
                    status_at = self.now()
                    # Une machine occupée (Run, Hold, Door, Jog, Home) n'est pas inactive :
                    # une seule longue passe peut dépasser idle_timeout sans aucun « ok ».
                    if payload["state"] != "Idle":
                        last_activity = self.now()
                elif kind == "banner":
                    return report(False, "GRBL s’est réinitialisé pendant le job (reset ou coupure).", "reset")
                if self._cancel.is_set() or (should_cancel and should_cancel()):
                    self._protective_stop()  # aussi réactif qu'avant : contrôlé après chaque réponse
                    return report(False, "Job annulé.", "cancelled")
                # Réponses déjà arrivées : traitées d'un coup, puis le tampon libéré est rempli en une écriture.
                # Borne : une carte qui n'arrête pas de parler ne doit pas retarder le remplissage.
                line = self._read(0) if handled < 256 else None
            if acked:
                notify()
        if self.machine_kind == "laser":
            try:
                self._expect_ok("M5", 5.0, tolerate=(9,))
            except GrblError as error:
                self._log(f"Coupure laser de fin de job non confirmée : {error}")
        return report(True, reason="stopped" if stopping else None)

    def stream_cycle(self, prologue, cycle, epilogue, progress=None, should_cancel=None, should_stop=None, interlocks=None,
                     allow_door=False, door_timeout=None, max_seconds=None, planner_ahead=2, on_cycle=None):
        """Cadrage en continu sans pause : ``prologue`` (mise en place, allumage éventuel), puis ``cycle``
        (contour fermé) répété sans arrêt jusqu'à ``should_stop()`` ou ``max_seconds``, puis ``epilogue``
        (extinction). Le planificateur de GRBL ne garde que ``planner_ahead`` segments d'avance (champ Bf des
        rapports d'état) : la tête ne s'arrête ni aux coins ni entre deux tours, le laser reste allumé, et un
        arrêt ne laisse que ces segments à parcourir. Sans champ Bf, une ligne part à chaque accusé.

        Capot ouvert autorisé (``allow_door``) : une carte restée en état Door plus de ``door_timeout`` s
        n'exécute rien ; le cadrage s'arrête alors avec ce motif. Rapport comme ``stream``, avec ``cycles`` ;
        ``reason`` vaut « stopped » ou « loop_timeout » en fin normale.
        """
        parts = (prepare_gcode(prologue), prepare_gcode(cycle), prepare_gcode(epilogue))
        if not parts[1]:
            raise GrblError("Contour de cadrage vide : rien à répéter.")
        provider = self.interlocks if interlocks is None else interlocks
        with self._lock:
            self._require_ready()
            self._require_interlocks(provider)
            status = self._status_sync(2.0)
            if status["state"] == "Alarm":
                raise GrblError("Machine en alarme : déverrouille ($X) ou lance un homing avant un job.")
            if status["state"] != "Idle" and not (allow_door and status["state"] == "Door"):
                raise GrblError(f"Machine non prête (état {status['state']}).")
            status = self._reset_power_override()  # le cadrage laser allumé part à 100 % de la puissance bornée
            self._streaming.set()
            self._cancel.clear()
            try:
                return self._run_cycle(parts, provider, progress, should_cancel, should_stop, door_timeout if allow_door else None,
                                       max_seconds, max(1, int(planner_ahead)), on_cycle, status)
            finally:
                self._streaming.clear()

    def _run_cycle(self, parts, provider, progress, should_cancel, should_stop, door_timeout, max_seconds, planner_ahead, on_cycle, status):
        prologue, cycle, epilogue = parts
        started = self.now()
        deadline = started + float(max_seconds) if max_seconds else None
        pending = deque()
        in_flight = sent = done = cycles = 0
        phase, index, reason = "prologue", 0, None
        last_activity = last_status_at = started
        poll_status = min(self.status_interval, 0.2)  # Bf frais : c'est lui qui règle l'avance du planificateur
        capacity = (status.get("buffer") or {}).get("planner")  # planificateur vide au départ : blocs libres = capacité
        planner_used = 0 if capacity is not None else None
        acked_since_status = 0
        door_since = None
        self.job = {"total": 0, "sent": 0, "done": 0, "percent": 0, "started": started, "cycles": 0}

        def report(ok, error=None, reason=None, **extra):
            result = {"ok": ok, "lines": done, "total_lines": sent, "cycles": cycles, "duration_seconds": round(self.now() - started, 3),
                      "error": error, "reason": reason}
            result.update(extra)
            return result

        def notify():
            self.job.update(sent=sent, done=done, cycles=cycles)
            if progress:
                try:
                    progress(dict(self.job))
                except Exception:
                    pass

        def current():
            """Prochaine ligne à envoyer (sans avancer), d'une phase à l'autre ; None une fois l'épilogue parti."""
            nonlocal phase, index, cycles, reason
            while True:
                if phase == "prologue":
                    if index < len(prologue):
                        return prologue[index]
                    phase, index = "cycle", 0
                elif phase == "cycle":
                    if index >= len(cycle):  # tour terminé : compté avant de regarder la demande d'arrêt
                        index = 0
                        cycles += 1
                        if on_cycle:
                            try:
                                on_cycle(cycles)
                            except Exception:
                                pass
                    if reason is None and should_stop and should_stop():
                        reason = "stopped"
                    if reason is None and deadline is not None and self.now() >= deadline:
                        reason = "loop_timeout"
                    if reason is not None:
                        phase, index = "epilogue", 0
                        continue
                    return cycle[index]
                elif phase == "epilogue":
                    if index < len(epilogue):
                        return epilogue[index]
                    phase = "done"
                else:
                    return None

        last_progress = started

        def stalled_message():
            """Rien n'avance machine à l'arrêt : on dit quelle ligne la carte n'a pas acceptée."""
            stuck = pending[0][2] if pending else ""
            buffer = (self.last_status or {}).get("buffer") or {}
            detail = f"Bf {buffer['planner']}" if buffer.get("planner") is not None else "sans Bf"
            if stuck.upper().startswith(("M3", "M4")):
                # Sécurité de capot propre à la machine : le logiciel ne la contourne pas, l'opérateur la désactive
                # sur la graveuse (Falcon2 Pro : deux boutons appuyés ensemble, comme pour LightBurn).
                return (f"La graveuse retient la commande laser (« {stuck} ») depuis {STALL_SECONDS:g} s sans l’exécuter : sa "
                        "sécurité de capot est active. Pour cadrer laser allumé capot ouvert, désactive-la sur la graveuse "
                        "elle-même (Falcon2 Pro : ses deux boutons appuyés ensemble, comme avec LightBurn, à revérifier après "
                        "un rallumage), puis relance le cadrage. Sinon, ferme le capot ou cadre laser éteint (0 %).")
            if stuck:
                return f"La graveuse n’exécute plus la ligne « {stuck} » (sans accusé depuis {STALL_SECONDS:g} s, {detail}) : cadrage arrêté."
            return f"Le cadrage n’avance plus depuis {STALL_SECONDS:g} s machine à l’arrêt ({detail}) : cadrage arrêté."

        while True:
            if self._cancel.is_set() or (should_cancel and should_cancel()):
                self._protective_stop()
                return report(False, "Cadrage annulé.", "cancelled")
            # Contrôles à chaque tour, même si la carte envoie sans cesse des lignes (rapports, messages).
            now = self.now()
            if now - last_status_at >= poll_status:
                self._realtime("status")
                last_status_at = now
            if now - last_activity > self.idle_timeout:
                self._protective_stop()
                return report(False, f"GRBL ne répond plus depuis {self.idle_timeout:g} s : cadrage interrompu.", "timeout")
            if (self.last_status or {}).get("state") == "Idle" and now - last_progress > STALL_SECONDS:
                self._protective_stop()  # coupe le laser et vide ce qui reste en file
                return report(False, stalled_message(), "stalled")
            while True:
                item = current()
                if item is None:
                    break
                number, text = item
                if in_flight + len(text) + 1 > self.rx_buffer:
                    break
                if phase == "cycle":
                    # Avance bornée : segments en vol + segments déjà planifiés d'après le dernier Bf.
                    if planner_used is None:
                        if pending:
                            break
                    elif len(pending) + planner_used + acked_since_status >= planner_ahead:
                        break
                if sent and sent % self.interlock_every == 0:
                    failed = self._failed_interlocks(provider)
                    if failed:
                        self._protective_stop()
                        return report(False, "Sécurité non satisfaite : " + ", ".join(item["label"] for item in failed) + ".", "interlock", interlocks=failed)
                payload = text.encode("ascii") + b"\n"
                self._write(payload)
                pending.append((len(payload), number, text))
                in_flight += len(payload)
                sent += 1
                index += 1
                last_progress = self.now()
                notify()
            if phase == "done" and not pending:
                break
            line = self._read(self.poll_interval)
            if line is None:
                continue
            kind, payload = self._classify(line)
            if kind == "ok":
                if not pending:
                    continue  # « ok » sans ligne en attente (réponse de certaines cartes à « ? ») : ni accusé ni activité
                in_flight -= pending.popleft()[0]
                done += 1
                acked_since_status += 1
                last_activity = last_progress = self.now()
                notify()
            elif kind == "error":
                size, number, text = pending.popleft() if pending else (0, None, "")
                in_flight -= size
                self._protective_stop()
                raise GrblCommandError(payload, line_number=number, command=text, report=report(False, f"Ligne {number} refusée (error:{payload}).", "error"))
            elif kind == "alarm":
                raise GrblAlarm(payload, report=report(False, f"ALARM:{payload} — {describe_alarm(payload)}", "alarm"))
            elif kind == "status":
                buffer = payload.get("buffer") or {}
                acked_since_status = 0
                if buffer.get("planner") is not None:
                    capacity = max(capacity or 0, buffer["planner"])
                    planner_used = capacity - buffer["planner"]
                if payload["state"] == "Idle" and planner_used is not None:
                    planner_used = 0  # machine à l'arrêt : sa file de mouvements est vide, quoi qu'en dise Bf
                if payload["state"] == "Door" and door_timeout is not None:
                    door_since = self.now() if door_since is None else door_since
                    if self.now() - door_since >= door_timeout:
                        self._protective_stop()
                        return report(False, "Porte de sécurité ouverte (état Door) : la carte n’exécute aucun mouvement tant que le capot est ouvert.", "door")
                else:
                    door_since = None
                if payload["state"] != "Idle":
                    last_activity = self.now()
            elif kind == "banner":
                return report(False, "GRBL s’est réinitialisé pendant le cadrage (reset ou coupure).", "reset")
        if self.machine_kind == "laser":
            try:
                self._expect_ok("M5", 5.0, tolerate=(9,))
            except GrblError as error:
                self._log(f"Coupure laser de fin de cadrage non confirmée : {error}")
        return report(True, reason=reason)

    def snapshot(self):
        """Rapport d'état pour le dashboard, dans l'esprit de ``Device.report``."""
        status = self.last_status or {}
        return {"connected": self.connected, "machine_kind": self.machine_kind, "version": self.version,
                "state": status.get("state"), "substate": status.get("substate"), "mpos": status.get("mpos"), "wpos": status.get("wpos"),
                "feed": status.get("feed"), "spindle": status.get("spindle"),
                # Rapport étendu (agent 1.33.0) : overrides, broches, accessoires, tampon, ligne, réglages, $I, messages.
                "overrides": dict(status["overrides"]) if status.get("overrides") else None,
                "pins": list(status.get("pins") or []), "accessories": list(status.get("accessories") or []),
                "buffer": dict(status["buffer"]) if status.get("buffer") else None, "line": status.get("line"),
                "settings": dict(self.settings), "build_info": list(self.build_info), "messages": list(self.messages)[-20:],
                "alarm": self.alarm, "alarm_label": describe_alarm(self.alarm) if self.alarm else None,
                "laser_mode": self.settings.get(32) == 1, "streaming": self._streaming.is_set(),
                "job": dict(self.job) if self.job else None, "message": self.messages[-1] if self.messages else "",
                "halted": self._halt.is_set()}
