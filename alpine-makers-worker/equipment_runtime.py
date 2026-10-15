"""Outbound equipment relay. All LAN sockets are opened on the owning Worker."""
import base64
import copy
import importlib
import importlib.util
import io
import ftplib
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import subprocess
import struct
import shutil
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

try:
    from .safety import protect_credentials
    from .equipment_signing import DeviceSigner
    from .equipment_video_relay import dependency_directory, COMPONENT_VERSION as RELAY_COMPONENT_VERSION
    from .equipment_video_relay import token_directory as relay_token_directory, with_token as relay_with_token
except ImportError:
    from safety import protect_credentials
    from equipment_signing import DeviceSigner
    from equipment_video_relay import dependency_directory, COMPONENT_VERSION as RELAY_COMPONENT_VERSION
    from equipment_video_relay import token_directory as relay_token_directory, with_token as relay_with_token

# Format libre des caméras (capacité equipment_camera_live_v1) : capture et lien local jusqu'à 120 i/s, image JPEG
# jusqu'à 4 Mio sur le Worker (3840×2160). L'image jointe au rapport du site reste sous 1 Mio (voir report_frame).
CAPTURE_FPS_MAX = 120
RELAY_FPS_MAX = 120
CAMERA_MAX_FRAME_BYTES = 4 * 1024 * 1024
REPORT_MAX_FRAME_BYTES = 1024 * 1024 - 1

# Composant relais trop ancien pour la cadence et la taille (agent 1.50.10, composant 1.1.0).
RELAY_OPTIONS_MIN_VERSION = (1, 1, 0)
RELAY_OUTDATED_MESSAGE = "Relance Installer pour la cadence et la taille du relais (composant vidéo à mettre à jour)."
RELAY_RESTART_MESSAGE = "Composant vidéo mis à jour : redémarre le Worker pour appliquer la cadence et la taille du relais."


def version_tuple(value):
    """« 1.1.0 » → (1, 1, 0) ; une valeur illisible vaut (0,) (composant d'avant le versionnage)."""
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts) if parts else (0,)


def component_file_version(path):
    """Version déclarée dans un composant relais sur disque, sans l'importer."""
    try:
        match = re.search(r'^COMPONENT_VERSION\s*=\s*"([^"]+)"', Path(path).read_text(encoding="utf-8"), re.M)
    except (OSError, ValueError):
        return (0,)
    return version_tuple(match.group(1)) if match else (0,)


def webcam_link(settings):
    """Fiche « webcam par lien MJPEG local » (agent 1.50.11) : connecteur mjpeg_http dont l'origine est une webcam
    de ce Worker (source_mode = "webcam"). Le Worker capture la webcam avec la chaîne du connecteur usb_camera, la
    publie sur son relais loopback, et la fiche lit ses images par ce lien (stream_url = lien de la fiche)."""
    return bool(settings) and settings.get("connector") == "mjpeg_http" and settings.get("source_mode") == "webcam"


# Webcams ouvertes par les fiches de ce Worker : (index, backend) -> {fiche: webcam par lien ?}. Une webcam n'alimente
# qu'une fiche à la fois dès que l'une d'elles est une « webcam par lien » ; deux fiches usb_camera gardent le
# comportement d'avant (le système refuse la seconde ouverture, la fiche le dit).
WEBCAM_CLAIMS = {}
WEBCAM_CLAIMS_LOCK = threading.Lock()
WEBCAM_BUSY_MESSAGE = ("Cette webcam (index {index}) est déjà ouverte par une autre fiche caméra de ce Worker : une webcam ne peut "
                       "alimenter qu’une fiche à la fois. Déconnecte l’autre fiche, ou lis son « Lien local » depuis celle-ci.")


def webcam_claim(key, owner, link):
    """Réserve la webcam ``key`` pour ``owner`` ; False si une autre fiche la tient et que l'une des deux est « par lien »."""
    with WEBCAM_CLAIMS_LOCK:
        holders = WEBCAM_CLAIMS.get(key) or {}
        if any(other is not owner and (link or other_link) for other, other_link in holders.items()):
            return False
        WEBCAM_CLAIMS.setdefault(key, {})[owner] = bool(link)
        return True


def webcam_release(key, owner):
    with WEBCAM_CLAIMS_LOCK:
        holders = WEBCAM_CLAIMS.get(key)
        if holders is not None:
            holders.pop(owner, None)
            if not holders:
                WEBCAM_CLAIMS.pop(key, None)


def relay_configured(spec):
    """Relais MJPEG local demandé pour cette caméra : réglage relay_enabled du site, ou mode relais P1S.

    Clé relay_enabled absente = False : un Worker à jour ne change rien tant que le site n'envoie pas la clé.
    Une caméra mjpeg_http dont l'URL d'entrée est déjà ce relais (même port, même source) est exclue : elle
    se lirait elle-même.
    """
    if spec.get("kind") != "camera":
        return False
    settings = spec.get("settings") or {}
    if settings.get("camera_mode") == "relay":
        return True
    if settings.get("relay_enabled") is not True:
        return False
    if webcam_link(settings):
        return True  # le lien est la source de la fiche : le relais est alimenté par la capture de la webcam
    if settings.get("connector") == "mjpeg_http":
        try:
            parsed = urlsplit(str(settings.get("stream_url") or ""))
            source = settings.get("source_id") or "cam_" + str(spec.get("id", "")).replace("-", "")
            if parsed.port == port_number(settings.get("relay_port"), 1984, 1024) and parse_qs(parsed.query).get("src") == [source]:
                return False
        except ValueError:
            return False
    return True


def relay_options(settings):
    """Noms lus dans settings : relay_fps, relay_width, relay_height (0/absent = défaut 10 i/s, taille d'origine)."""
    def number(key, maximum):
        try:
            value = float(settings.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        return value if 0 < value <= maximum else 0

    return {"fps": number("relay_fps", RELAY_FPS_MAX) or None, "width": int(number("relay_width", 4096)) or None, "height": int(number("relay_height", 4096)) or None}


def runtime_dependencies(root):
    target = dependency_directory(root)
    # Existing installations remain usable until explicitly repaired.
    return target if target.is_dir() else Path(root) / "components/printer-link/python"


def dependency_health(target):
    """Validate the exact Worker interpreter, isolated from cached imports."""
    try:
        process = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).with_name("equipment_video_relay.py")), "--check-dependencies", str(target)], capture_output=True, text=True, encoding="utf-8", timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        report = json.loads(process.stdout) if process.returncode == 0 else {}
        return report if isinstance(report, dict) and isinstance(report.get("ok"), bool) else {"ok": False}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"ok": False}


def dependency_error(error):
    name = getattr(error, "name", "") or ""
    if not re.fullmatch(r"(?:paho|cryptography|PIL|_cffi_backend)(?:\.[A-Za-z0-9_]+)*", name):
        name = "réseau/JPEG"
    return f"Dépendance Worker manquante ou incompatible : {name}. Utilise Installer le relais, puis redémarre le Worker. Ce défaut n’indique pas une mauvaise IP."


class PrinterFTP(ftplib.FTP_TLS):
    """FTPS implicite (port 990) des imprimantes Bambu.

    Le canal de données réutilise la session TLS du canal de commande : le serveur FTP de ces imprimantes
    (vsftpd) l'exige, sinon le transfert échoue (« 522 … session reuse required »). En fin d'envoi, certaines
    imprimantes ferment la connexion sans close_notify : la réponse 226 du serveur fait alors foi."""

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=getattr(self.sock, "session", None))
        return conn, size

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd, rest) as conn:
            while True:
                buffer = fp.read(blocksize)
                if not buffer:
                    break
                conn.sendall(buffer)
                if callback:
                    callback(buffer)
            if isinstance(conn, ssl.SSLSocket):
                try:
                    conn.unwrap()
                except (OSError, ValueError):
                    pass
        return self.voidresp()

    def connect(self, host, port=990, timeout=35):
        self.host, self.port, self.timeout = host, port, timeout
        raw = socket.create_connection((host, port), timeout)
        self.af = raw.family
        try:
            self.sock = self.context.wrap_socket(raw, server_hostname=host)
        except Exception:
            raw.close()
            raise
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


def local_host(value):
    address = ipaddress.ip_address(str(value))
    ranges = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
    if not any(address.version == network.version and address in network for network in map(ipaddress.ip_network, ranges)):
        raise ValueError("Adresse privée d’équipement requise.")
    return str(address)


def port_number(value, default, minimum=1):
    value = default if value is None or value == "" else value
    if isinstance(value, bool) or not re.fullmatch(r"\d{1,5}", str(value)) or not minimum <= int(value) <= 65535:
        raise ValueError("Port d’équipement invalide.")
    return int(value)


# Connecteurs imprimante en HTTP local (bibliothèque standard uniquement) et
# leur port par défaut. Le pilote lit ces valeurs : une seule source de vérité.
HTTP_PRINTER_PORTS = {"moonraker_http": 7125, "octoprint_http": 5000}
# Seules ces clés de télémétrie traversent le Worker, quel que soit le protocole.
TELEMETRY_KEYS = {"fun", "gcode_state", "mc_percent", "mc_remaining_time", "layer_num", "total_layer_num", "subtask_name", "gcode_file", "nozzle_temper", "nozzle_target_temper", "bed_temper", "bed_target_temper", "wifi_signal", "spd_lvl", "spd_mag", "print_error", "ams", "ams_status", "vt_tray", "cooling_fan_speed", "big_fan1_speed", "big_fan2_speed", "hms", "home_flag", "nozzle_diameter", "nozzle_type", "sdcard",
                  # Ajouts additifs : le dashboard les consomme déjà côté API.
                  # « lights_report » permet de vérifier l'éclairage réellement
                  # appliqué, « ota_version » de savoir si le firmware propose
                  # le Mode développeur. Aucun blob volumineux (ipcam, xcam) :
                  # la télémétrie est plafonnée à 128 KiB côté dashboard.
                  "lights_report", "heatbreak_fan_speed", "chamber_temper", "mc_print_error_code", "fail_reason", "ota_version"}


def valid_api_key(value):
    """Deuxième ligne de défense : la clé n'est jamais placée dans une URL."""
    key = str(value or "")
    if not 16 <= len(key) <= 128 or not key.isascii() or any(ord(char) < 33 for char in key):
        raise ValueError("Clé API d’équipement invalide.")
    return key


# Machines GRBL 1.1 (laser ou CNC) pilotées par equipment_grbl.py : la carte est
# jointe depuis le Worker (série USB ou TCP privé), jamais depuis le serveur.
GRBL_CONNECTORS = {"grbl_serial", "grbl_tcp"}
GRBL_KINDS = ("laser", "cnc")
GRBL_OPERATOR_INTERLOCKS = ("checklist", "water", "air", "fire")   # confirmées par l'opérateur, sans capteur
GRBL_COMMANDS = ("stream", "frame", "frame_stop", "jog", "home", "unlock", "hold", "resume", "stop", "reset", "status", "set_origin", "laser_test",
                 "override", "jog_cancel", "settings", "info", "goto_origin", "goto_machine_zero", "move_abs", "accessory", "mdi", "probe_z")
GRBL_JOB_COMMANDS = {"stream", "frame"}
# Seules commandes acceptées pendant un job : pause/reprise/arrêt, fin du cadrage, état et, depuis l'agent 1.33.0,
# les octets temps réel (overrides, annulation du jog) et la bascule d'accessoire, qui n'insèrent aucune ligne.
GRBL_LIVE_COMMANDS = {"status", "hold", "resume", "stop", "frame_stop", "override", "jog_cancel", "accessory"}
# Cadrage en continu (agent 1.27.0) : le contour repasse jusqu'à « frame_stop » (fin propre, sans reset
# donc sans alarme ni perte de position), au plus 10 min ; 2 lignes en vol pour un arrêt réactif.
GRBL_FRAME_LOOP_MAX_SECONDS = 600
GRBL_FRAME_LOOP_PAUSE = 0.3
GRBL_FRAME_PENDING_LINES = 2
GRBL_FRAME_IDLE_WAIT = 30.0     # fin des segments déjà partis entre deux passes
GRBL_FRAME_DOOR_WAIT = 8.0      # capot ouvert : délai avant de conclure que la carte bloque le mouvement
GRBL_FRAME_STOP_WAIT = 40.0
GRBL_FRAME_PLANNER_AHEAD = 2    # cadrage en continu : segments planifiés d'avance (mouvement fluide, arrêt réactif)
# Cadrage laser allumé (agent 1.29.0) : point laser visible sur la plaque, capot fermé, 10 % au plus.
GRBL_FRAME_LASER_MAX_PCT = 10.0
GRBL_FRAME_LASER_OPEN_MAX_PCT = 5.0   # capot ouvert (1.31.0, 5 % depuis 1.31.2) : repérage, check-list dédiée
GRBL_GCODE_LIMIT = 32 * 1024 * 1024
GRBL_WIRE_LIMIT = 96 * 1024                                          # 64 Ko de G-code en ligne + enveloppe
GRBL_LASER_TEST_MAX_PCT = 20.0
GRBL_LASER_TEST_MS = 1000
GRBL_LASER_TEST_MS_RANGE = (50, 3000)   # durée réglable (agent 1.33.0) ; 1000 ms reste la valeur par défaut
# Gestion de la machine (agent 1.33.0, capacité equipment_grbl_v2). Réglages $$ modifiables depuis le dashboard :
# délai de repos ($1), rapports et jog ($10-$13), homing ($24-$27), broche/laser ($30-$32), vitesses ($110-$112),
# accélérations ($120-$122), courses ($130-$132). Jamais les pas par mm, les inversions, les fins de course
# ni l'activation du homing ($0, $2-$6, $20-$23, $100-$102) ; jamais $RST.
GRBL_WRITABLE_SETTINGS = frozenset({1, 10, 11, 12, 13, 24, 25, 26, 27, 30, 31, 32, 110, 111, 112, 120, 121, 122, 130, 131, 132})
# Paramètres du contrôleur (agent 1.50.17, capacité equipment_grbl_firmware_v1) : action « write_batch » des réglages.
# Tout numéro présent dans le dernier ``$$`` peut être écrit, un à la fois avec attente de l'accusé ; les réglages
# critiques (impulsions, inversions, limites/référencement, pas par mm, courses, mode laser) et tout numéro hors du
# catalogue GRBL 1.1 / grblHAL exigent ``critical_ack`` ; jamais $RST, $X, $H, $N…, $SLP ni $J. Même liste que
# laser_grbl_catalog.CRITICAL_SETTINGS côté site (le Worker n'importe pas le catalogue).
GRBL_KNOWN_SETTINGS = frozenset({*range(0, 7), 10, 11, 12, 13, *range(20, 29), 30, 31, 32, 40, *range(100, 106), *range(110, 116),
                                 *range(120, 126), *range(130, 136)})
GRBL_CRITICAL_SETTINGS = frozenset({*range(0, 7), 20, 21, 22, 23, 32, *range(100, 106), *range(130, 136)})
GRBL_SETTINGS_BATCH_MAX = 64
GRBL_SETTINGS_TOLERANCE = 0.0015


def grbl_setting_critical(number):
    """Réglage à acquitter explicitement : critique du catalogue, ou inconnu de celui-ci (constructeur)."""
    return number in GRBL_CRITICAL_SETTINGS or number not in GRBL_KNOWN_SETTINGS
GRBL_PROBE_MAX_TRAVEL_MM = 500.0
GRBL_OVERRIDE_LABELS = {"feed": "d’avance", "rapid": "des rapides", "power": "de puissance"}
GRBL_OVERRIDE_ACTION_LABELS = {"reset": "retour à 100 %", "plus10": "+10 %", "minus10": "−10 %", "plus1": "+1 %", "minus1": "−1 %",
                               "100": "100 %", "50": "50 %", "25": "25 %"}
# Arrêt incendie (agent 1.34.0) : « stream », « frame » et « laser_test » passent par la garde de la surveillance ;
# un appel pendant un arrêt en cours en attend le résultat (délai borné) ; un appel moins de 10 s après un arrêt
# confirmé, sans job démarré depuis, reçoit ce même résultat au lieu d'un second reset.
GRBL_FIRE_COALESCE_SECONDS = 10.0
GRBL_FIRE_WAIT_SECONDS = 20.0


def grbl_module():
    """Import paresseux : equipment_grbl importe ce module, l'inverse au chargement bouclerait."""
    try:
        from . import equipment_grbl
    except ImportError:
        import equipment_grbl
    return equipment_grbl


def package_installed(name, marker):
    """Paquet optionnel réellement présent, pas un dossier vide ou partiel laissé par une copie
    interrompue : Python prend un dossier « serial/ » vide pour un paquet namespace, find_spec
    répond alors une spec sans origine et « serial.Serial » n'existe pas (Falcon2, 25.09.2026)."""
    importlib.invalidate_caches()
    spec = importlib.util.find_spec(name)
    if spec is None:
        return False
    locations = [Path(item) for item in (getattr(spec, "submodule_search_locations", None) or [])]
    origin = getattr(spec, "origin", None)
    if not locations and not origin:
        return True  # spec sans information de fichier : on s'en remet à l'import (tests, paquets gelés)
    if not locations and origin:
        locations = [Path(origin).parent]
    return any((location / marker).is_file() for location in locations)


def winrt_installed(target):
    """Paquets WinRT de la capture Media Foundation présents dans le dossier de dépendances. ``winrt`` est un paquet
    d'espace de noms (sans __init__.py, agent 1.50.6) : on regarde le module natif et l'espace Windows.Media.Capture.Frames."""
    folder = Path(target) / "winrt"
    return (folder / "windows" / "media" / "capture" / "frames" / "__init__.py").is_file() and any(folder.glob("_winrt.*.pyd"))


def purge_partial_package(target, name):
    """Retire du dossier de dépendances un dossier de paquet sans __init__.py (copie interrompue),
    pour que pip le réinstalle en entier ; jamais ailleurs que sous ce dossier."""
    target = Path(target).resolve()
    folder = (target / name).resolve()
    if folder.parent != target or not folder.is_dir() or (folder / "__init__.py").is_file():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    importlib.invalidate_caches()
    return True


def worker_site_headers(agent):
    """En-têtes d'un téléchargement depuis le site par le Worker (transferts d'équipement).

    Le User-Agent reprend celui de l'agent : devant le site, Cloudflare refuse le User-Agent
    Python-urllib par défaut (403, « error code 1010 »). Tout G-code laser de plus de 64 Ko et
    tout fichier d'impression transféré échouaient ainsi en « HTTPError » (incident du 27/09).
    """
    module = sys.modules.get("agent") or sys.modules.get("worker_agent.agent")
    version = str(getattr(module, "AGENT_VERSION", "") or "equipment")
    config = getattr(agent, "config", None) or {}
    return {"Authorization": "Bearer " + str(config.get("token", "")), "X-Worker-ID": str(config.get("worker_id", "")),
            "User-Agent": f"AlpineWorker/{version}"}


class LatestProgress:
    """Progression d'une commande envoyée au site par un fil à part : la plus récente, une fois par seconde au plus.

    Le fil qui alimente la machine GRBL ne doit jamais attendre le réseau. Avant l'agent 1.31.6, il postait
    lui-même la progression chaque seconde, et chaque requête vers le site suspendait l'envoi des lignes.
    ``post`` ne fait que retenir la valeur ; après ``close``, plus rien ne part : le site ignore la
    progression d'une commande déjà terminée, et le suivi d'un job passe par le rapport de l'équipement.
    """

    def __init__(self, send, interval=1.0):
        self._send = send
        self._interval = float(interval)
        self._condition = threading.Condition()
        self._latest = None
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="grbl-progress", daemon=True)
        self._thread.start()

    def post(self, *values):
        with self._condition:
            if self._closed:
                return
            self._latest = values
            self._condition.notify()

    def close(self):
        with self._condition:
            self._closed = True
            self._latest = None
            self._condition.notify_all()

    def _run(self):
        while True:
            with self._condition:
                while self._latest is None and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                values, self._latest = self._latest, None
            try:
                self._send(*values)
            except Exception:
                pass  # une progression perdue n'arrête pas le travail
            with self._condition:
                if self._condition.wait_for(lambda: self._closed, timeout=self._interval):
                    return


def grbl_transport(connector, cfg):
    """Liaison vers la carte : rien n'est ouvert ici, seulement validé."""
    grbl = grbl_module()
    if connector == "grbl_tcp":
        return grbl.TcpTransport(local_host(cfg.get("host", "")), port_number(cfg.get("port"), 23))
    baud = cfg.get("baud", 115200)
    return grbl.SerialTransport(str(cfg.get("serial_port") or ""), int(baud) if not isinstance(baud, bool) and re.fullmatch(r"\d{4,7}", str(baud)) else 0)


_GCODE_WORD = re.compile(r"([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")


def gcode_words(line):
    """Mots G-code d'une ligne, espacés ou collés : « G1 X150 S0 » comme le format compact « G1X150S0 »."""
    return [letter + value for letter, value in _GCODE_WORD.findall(str(line).upper())]


def frame_gcode(text, laser_s=0):
    """G-code de cadrage : M3/M4 et mots S retirés, « M5 S0 » en tête → mouvement seul, laser et broche coupés.

    ``laser_s`` > 0 (cadrage laser allumé, graveur laser seulement) : le Worker rallume lui-même le laser
    à S constant (``M3 S…``) juste avant chaque suite de déplacements travaillés (G1/G2/G3) et l'éteint
    (``M5 S0``) avant chaque déplacement rapide et en fin de programme. La puissance du G-code reçu n'est
    jamais reprise : seule la valeur bornée par le Worker est émise.
    """
    lines = ["M5 S0"]
    lit, modal = False, None
    for raw in str(text or "").splitlines():
        line = re.sub(r"\([^)]*\)", "", raw).split(";", 1)[0]
        line = re.sub(r"(?i)(?<![A-Z])M0?[34](?![0-9])", " ", line)
        line = re.sub(r"(?i)(?<![A-Z])S-?\d+(?:\.\d+)?", " ", line)
        line = " ".join(line.split())
        if not line:
            continue
        if laser_s > 0:
            words = gcode_words(line)
            motion = next(("G0" if word in {"G0", "G00"} else "G1" for word in words
                           if word in {"G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03"}), None)
            if motion:
                modal = motion
            elif any(word[:1] in "XYZ" for word in words):
                motion = modal  # déplacement modal : même mode que la ligne précédente
            if "M5" in words or "M05" in words:
                lit = False
            elif motion == "G1" and not lit:
                lines.append(f"M3 S{int(laser_s)}")
                lit = True
            elif lit and (motion == "G0" or words[:1] in (["M2"], ["M02"], ["M30"])):
                lines.append("M5 S0")
                lit = False
        lines.append(line)
    if lit:
        lines.append("M5 S0")
    return "\n".join(lines) + "\n"


def frame_cycle(text):
    """G-code de cadrage filtré → (mise en place, contour, extinction) pour le repasser sans pause.

    La mise en place va jusqu'au premier déplacement travaillé (« M3 S… » compris). Le contour est la suite
    de déplacements travaillés G1 qui suit, en absolu, et doit revenir à son point de départ. Le retour
    rapide à l'origine et la fin de programme sont retirés : à l'arrêt, la tête reste sur le contour.
    None si le G-code n'a pas cette forme (relatif, arcs, contour ouvert ou plusieurs contours) : le
    cadrage passe alors par passes successives.
    """
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    modal = None
    x = y = None
    parsed = []  # (mouvement, X après la ligne, Y après la ligne)
    for line in lines:
        words = gcode_words(line)
        if "G91" in words or any(word in {"G2", "G02", "G3", "G03"} for word in words):
            return None
        motion = next(("G0" if word in {"G0", "G00"} else "G1" for word in words if word in {"G0", "G00", "G1", "G01"}), None)
        if motion:
            modal = motion
        elif any(word[:1] in "XYZ" for word in words):
            motion = modal  # déplacement modal : même mode que la ligne précédente
        for word in words:
            if word[:1] in "XY":
                try:
                    value = float(word[1:])
                except ValueError:
                    return None
                if word[0] == "X":
                    x = value
                else:
                    y = value
        parsed.append((motion, x, y))
    cuts = [position for position, (motion, _, _) in enumerate(parsed) if motion == "G1"]
    if not cuts or cuts[0] == 0:
        return None
    start = end = cuts[0]
    while end + 1 < len(parsed) and parsed[end + 1][0] == "G1":
        end += 1
    if cuts[-1] != end:
        return None  # plusieurs contours séparés
    origin, final = parsed[start - 1][1:], parsed[end][1:]
    if None in origin or None in final or abs(final[0] - origin[0]) > 1e-3 or abs(final[1] - origin[1]) > 1e-3:
        return None  # contour ouvert : le répéter tracerait une diagonale
    return "\n".join(lines[:start]) + "\n", "\n".join(lines[start:end + 1]) + "\n", "M5 S0\n"


def validate_connector(spec):
    cfg = spec["settings"]
    kind = spec["kind"]
    if kind in GRBL_KINDS:
        connector = cfg.get("connector") or "grbl_serial"
        if connector not in GRBL_CONNECTORS:
            raise ValueError("Connecteur non pris en charge par ce Worker.")
        if cfg.get("machine_kind", kind) != kind:
            raise ValueError("Type de machine GRBL incohérent avec l’équipement.")
        if connector == "grbl_tcp":
            local_host(cfg.get("host", ""))
            port_number(cfg.get("port"), 23)
        else:
            grbl = grbl_module()
            if not grbl.SERIAL_PORT.fullmatch(str(cfg.get("serial_port") or "")):
                raise ValueError("Port série GRBL invalide.")
            baud = cfg.get("baud", 115200)
            if isinstance(baud, bool) or not re.fullmatch(r"\d{4,7}", str(baud)) or not 1200 <= int(baud) <= 1000000:
                raise ValueError("Débit série GRBL invalide.")
        return connector
    default = {"printer": "bambu_mqtt_tls", "bambu": "bambu_cloud", "camera": "mjpeg_http" if cfg.get("stream_url") else "p1s_tls"}.get(kind)
    connector = cfg.get("connector") or default
    allowed = {"printer": {"bambu_mqtt_tls", *HTTP_PRINTER_PORTS}, "bambu": {"bambu_cloud"}, "camera": {"p1s_tls", "mjpeg_http", USB_CAMERA_CONNECTOR}}
    if connector not in allowed.get(kind, set()):
        raise ValueError("Connecteur non pris en charge par ce Worker.")
    if connector == USB_CAMERA_CONNECTOR:
        usb_camera_settings(cfg)  # index et format seulement : rien de réseau, aucun secret
        return connector
    if connector == "mjpeg_http" and cfg.get("source_mode") not in (None, "", "url"):
        if cfg.get("source_mode") != "webcam":
            raise ValueError("Origine d’image non prise en charge par ce Worker.")
        usb_camera_settings(cfg)  # même périphérique et même format que le connecteur usb_camera
        relay_port = port_number(cfg.get("relay_port"), 1984, 1024)
        source = str(cfg.get("source_id") or "")
        if (cfg.get("relay_enabled") is not True or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", source)
                or cfg.get("stream_url") != f"http://127.0.0.1:{relay_port}/api/stream.mjpeg?src={source}"
                or port_number(cfg.get("port"), 1984) != relay_port):
            raise ValueError("Webcam par lien MJPEG local : le lien de la fiche doit être sa propre source.")
        return connector
    if connector in {"bambu_mqtt_tls", "p1s_tls", *HTTP_PRINTER_PORTS}:
        local_host(cfg["host"])
    port_number(cfg.get("port"), HTTP_PRINTER_PORTS.get(connector, 6000 if connector == "p1s_tls" else 1984 if connector == "mjpeg_http" else 8883))
    if connector == "p1s_tls" and cfg.get("camera_mode", "native") not in {"native", "relay"}:
        raise ValueError("Mode caméra invalide.")
    if cfg.get("api_key") or connector == "octoprint_http":
        # OctoPrint n'autorise aucune requête sans clé ; Moonraker accepte un
        # client de confiance déclaré dans sa propre configuration.
        valid_api_key(cfg.get("api_key"))
    return connector


USB_CAMERA_CONNECTOR = "usb_camera"


def usb_camera_settings(cfg):
    """Réglages d'une caméra USB (UVC) du Worker : index de périphérique et format, rien de réseau."""
    index = cfg.get("device_index", 0)
    if isinstance(index, bool) or not re.fullmatch(r"\d{1,2}", str(index)) or not 0 <= int(index) <= 15:
        raise ValueError("Index de caméra USB invalide (0 à 15).")
    name = str(cfg.get("device_name") or "")
    if len(name) > 80 or any(ord(character) < 32 for character in name):
        raise ValueError("Nom de caméra USB invalide.")
    size = {}
    for key, label in (("width", "largeur"), ("height", "hauteur")):
        value = cfg.get(key) or 0
        if isinstance(value, bool) or not re.fullmatch(r"\d{1,4}", str(value)) or not (int(value) == 0 or 120 <= int(value) <= 4096):
            raise ValueError(f"Résolution de caméra USB invalide ({label}).")
        size[key] = int(value)
    fps = cfg.get("fps", 10)
    if isinstance(fps, bool) or not re.fullmatch(r"\d{1,3}", str(fps)) or not 1 <= int(fps) <= CAPTURE_FPS_MAX:
        raise ValueError(f"Cadence de caméra USB invalide (1 à {CAPTURE_FPS_MAX} images par seconde).")
    # Agent 1.50.1 : backend OpenCV choisi sur la fiche ; absent (fiche d'avant) = automatique.
    backend = str(cfg.get("camera_backend") or "auto").strip().lower()
    if backend not in USB_BACKENDS:
        raise ValueError("Backend OpenCV de caméra USB invalide (auto, msmf, dshow ou v4l2).")
    return {"index": int(index), "name": name, "width": size["width"], "height": size["height"], "fps": int(fps),
            "backend": backend}


# Backend OpenCV de capture (fiche « Caméra USB ») → nom de la constante cv2. « auto » : sous Windows, Media Foundation
# d'abord puis DirectShow (mesuré le 11/10/2026 sur deux caméras UVC d'un même PC : DSHOW+DSHOW échoue, MSMF+MSMF et
# DSHOW+MSMF passent, MSMF+DSHOW lit mal) ; ailleurs, le backend par défaut d'OpenCV (V4L2 sous Linux).
USB_BACKENDS = {"auto": None, "msmf": "CAP_MSMF", "dshow": "CAP_DSHOW", "v4l2": "CAP_V4L2"}
USB_BACKEND_LABELS = {"CAP_MSMF": "MSMF", "CAP_DSHOW": "DirectShow", "CAP_V4L2": "V4L2", None: "backend par défaut"}


def usb_backend_candidates(cv2, backend):
    """Constantes cv2 à essayer dans l'ordre (None = VideoCapture(index) sans backend) ; jamais vide."""
    if backend == "auto":
        names = ("CAP_MSMF", "CAP_DSHOW") if os.name == "nt" else ()
    else:
        names = (USB_BACKENDS[backend],)
    found = [getattr(cv2, name) for name in names if getattr(cv2, name, None) is not None]
    return found or [None]   # constante absente de cet OpenCV : backend par défaut, comme avant


def usb_capture_module():
    """OpenCV (headless) depuis le dossier de dépendances du Worker ; ImportError s'il manque."""
    importlib.invalidate_caches()
    return importlib.import_module("cv2")


def raise_current_thread_priority():
    """Fil courant au-dessus de la normale (Windows, au mieux ; ailleurs rien). Vrai si la priorité a été changée.

    Pour le fil d'envoi GRBL pendant un travail : il passe devant l'analyse de la surveillance IA et devant un
    travail lourd du même PC, sans pouvoir les affamer, car il attend la carte sur le port série (lecture bloquante)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)   # prototypes propres à cet appel
        kernel32.GetCurrentThread.restype = wintypes.HANDLE
        kernel32.SetThreadPriority.argtypes = (wintypes.HANDLE, ctypes.c_int)
        kernel32.SetThreadPriority.restype = wintypes.BOOL
        return bool(kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 1))   # THREAD_PRIORITY_ABOVE_NORMAL
    except Exception:  # noqa: BLE001 - priorité inchangée : le travail continue comme avant
        return False


def usb_open_capture(cv2, options):
    """Ouvre la caméra USB avec le backend de la fiche (ou, en automatique, MSMF puis DirectShow sous Windows).

    En automatique, un backend qui n'ouvre pas la caméra ou ne livre pas d'image est libéré et le suivant est essayé :
    les autres captures du Worker ne sont jamais touchées (chaque fiche a son propre VideoCapture).
    L'objet rendu porte ``backend_label`` (nom affiché sur la fiche).

    Ordre imposé par DirectShow : taille, cadence, PUIS le format MJPEG. Mesuré le 2 octobre 2026 sur la caméra d'une
    Falcon2 (Creality Falcon Camera) en 1280×960 : MJPEG demandé en dernier → 30 i/s ; demandé avant la cadence, le
    réglage de cadence ramène le format non compressé (YUY2) → 1,2 i/s (surveillance IA « dégradée », lectures lentes
    jusqu'à la déconnexion). Une caméra sans MJPEG garde son format habituel."""
    candidates = usb_backend_candidates(cv2, options.get("backend", "auto"))
    capture = None
    for position, backend in enumerate(candidates):
        capture = usb_configure_capture(cv2, options, backend)
        if position == len(candidates) - 1:
            break   # dernier candidat : rendu tel quel, l'appelant juge isOpened() et les lectures
        probed = usb_probe(capture)
        if probed:
            break
        try:
            capture.release()
        except Exception:
            pass
    label = next((name for name, value in ((n, getattr(cv2, n, None)) for n in ("CAP_MSMF", "CAP_DSHOW", "CAP_V4L2"))
                  if value is not None and value == backend), None)
    options["backend_used"] = USB_BACKEND_LABELS[label]   # l'objet OpenCV n'accepte pas d'attribut libre
    try:
        capture.backend_label = options["backend_used"]
    except Exception:
        pass
    return capture


def usb_configure_capture(cv2, options, backend):
    capture = cv2.VideoCapture(options["index"], backend) if backend is not None else cv2.VideoCapture(options["index"])
    if options["width"] and options["height"]:
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, options["width"])
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, options["height"])
    capture.set(cv2.CAP_PROP_FPS, options["fps"])
    # Agent 1.50.2 : le format MJPEG et la taille de tampon ne sont imposés qu'à DirectShow (voir l'ordre ci-dessus).
    # Media Foundation choisit lui-même son format ; les lui imposer après l'ouverture faisait échouer la première
    # lecture (atelier, 11/10/2026) et l'automatique retombait sur DirectShow.
    if backend is not None and backend == getattr(cv2, "CAP_MSMF", object()):
        return capture
    fourcc, prop_fourcc = getattr(cv2, "VideoWriter_fourcc", None), getattr(cv2, "CAP_PROP_FOURCC", None)
    if callable(fourcc) and prop_fourcc is not None:
        capture.set(prop_fourcc, fourcc(*"MJPG"))
    buffered = getattr(cv2, "CAP_PROP_BUFFERSIZE", None)
    if buffered is not None:
        capture.set(buffered, 1)   # l'image la plus récente, jamais une file d'images anciennes
    return capture


def usb_probe(capture, attempts=3):
    """Vrai si la caméra ouverte livre une image (plusieurs essais : la première image MSMF peut tarder)."""
    if not capture.isOpened():
        return False
    for _ in range(attempts):
        try:
            if capture.read()[0]:
                return True
        except Exception:
            return False
        time.sleep(0.3)
    return False


def usb_enumerate(cv2, limit=4):
    """Index 0..limit-1 qui s'ouvrent, par backend, pour le message d'échec de la fiche (ouverture puis libération
    immédiate, sans lecture). Rend par exemple « MSMF : 0, 1 · DirectShow : 1 »."""
    parts = []
    for name, label in (("CAP_MSMF", "MSMF"), ("CAP_DSHOW", "DirectShow"), ("CAP_V4L2", "V4L2")):
        backend = getattr(cv2, name, None)
        if backend is None or (name == "CAP_V4L2") == (os.name == "nt"):
            continue
        found = []
        for index in range(limit):
            capture = None
            try:
                capture = cv2.VideoCapture(index, backend)
                if capture.isOpened():
                    found.append(str(index))
            except Exception:
                pass
            finally:
                if capture is not None:
                    try:
                        capture.release()
                    except Exception:
                        pass
        parts.append(f"{label} : {', '.join(found) if found else 'aucune'}")
    return " · ".join(parts)


# Agent 1.50.3 : sous Windows, chaque caméra USB est capturée dans son propre processus (usb_camera_capture.py).
# Deux captures DirectShow dans un même processus se font tomber l'une l'autre avec certains pilotes (atelier,
# 11/10/2026 : deux caméras qui marchent chacune seule, jamais ensemble), deux processus coexistent.
USB_ISOLATED_CAPTURE = os.name == "nt"
USB_ISOLATED_SCRIPT = "usb_camera_capture.py"
USB_FPS_WINDOW_S = 5.0          # cadence de capture mesurée sur les 5 dernières secondes
USB_MAX_FRAME_BYTES = CAMERA_MAX_FRAME_BYTES
USB_DECODE_CHECK_S = 1.0        # nos propres encodages : décodage complet d'une image par seconde, en-tête pour les autres


def usb_encode_frame(cv2, image, quality=80):
    """JPEG sous USB_MAX_FRAME_BYTES (4 Mio) : la qualité baisse par paliers au lieu de jeter l'image (une image écartée en silence
    ralentissait d'autant la cadence vue par la surveillance IA). Renvoie (octets, qualité retenue) ou (None, qualité)."""
    while True:
        encoded, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not encoded:
            raise ValueError("Encodage JPEG impossible")
        frame = bytes(buffer.tobytes() if hasattr(buffer, "tobytes") else buffer)
        if len(frame) < USB_MAX_FRAME_BYTES:
            return frame, quality
        if quality <= 40:
            return None, quality
        quality -= 15


def usb_capture_message(measured, wanted, backend="", size=None, wanted_size=None):
    """Message de la fiche : backend utilisé, taille et cadence réellement capturées, et la cause probable quand
    elles restent loin du réglage. ``size`` = (largeur, hauteur) obtenues, ``wanted_size`` = celles de la fiche."""
    where = f" ({backend})" if backend else ""
    obtained = ""
    if size and size[0] and size[1]:
        obtained = f"{size[0]}×{size[1]}"
        if wanted_size and wanted_size[0] and wanted_size[1] and tuple(wanted_size) != tuple(size):
            obtained += f" obtenu pour {wanted_size[0]}×{wanted_size[1]} demandé"
        obtained += ", "
    if measured is None:
        return f"Caméra USB connectée{where} — {obtained}images capturées par le Worker."
    text = f"Caméra USB connectée{where} — {obtained}" + f"{measured:.1f} image(s)/s capturée(s) par le Worker".replace(".", ",", 1)
    if measured < wanted * 0.6:
        text += (f" pour {wanted} demandée(s) : la caméra ne fournit pas plus à cette résolution "
                 "(baisse la résolution ou vérifie qu'elle propose le format MJPEG)")
    return text + "."


def usb_dependency_error(error):
    name = getattr(error, "name", "") or ""
    if name.split(".")[0] in {"cv2", "numpy"}:
        return "OpenCV absent sur ce Worker : clique Installer sur la fiche de la caméra USB (capture vidéo, sans GPU)."
    return dependency_error(error)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Redirection de caméra refusée.")


class Device:
    def __init__(self, spec, root):
        self.spec = spec
        self.root = Path(root)
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.client = None
        self.driver = None
        self.socket = None
        self.thread = None
        self.snapshot = {"id": spec["id"], "revision": spec["revision"], "connected": False, "progress": 0, "message": "Déconnecté"}
        self.frame = None
        self.frame_at = 0
        self.telemetry = {}
        self.acks = {}
        self.signer = DeviceSigner(root)
        self.relay_state = {}
        # Webcam par lien MJPEG local : la capture de la webcam vit dans un Device fils (chaîne usb_camera), publié
        # sur le relais ; cette fiche lit ses images par le lien. Un Device fils porte capture_owner (réservation).
        self.webcam_link = spec.get("kind") == "camera" and webcam_link(spec.get("settings") or {})
        self.webcam = None
        self.webcam_started = 0.0
        self.capture_owner = self
        self.capture_link = False
        self.capture_fps = None       # cadence réellement capturée (caméra USB, webcam par lien)
        self.capture_size = None      # (largeur, hauteur) réellement obtenues
        self.link_fps = None          # cadence lue par le lien MJPEG local (webcam par lien)
        self._decode_checked = 0.0
        self._report_cache = (None, None)   # (image d'origine, image réduite pour le rapport du site)
        # Machines GRBL : contrôleur connecté, job en cours (fil dédié) et confirmation opérateur.
        self.grbl = None
        self.grbl_job = {}
        self.grbl_thread = None
        self.grbl_checklist = False
        self.grbl_loop_stop = threading.Event()   # « Arrêter le cadrage » : fin propre du cadrage en continu
        self.grbl_message = ""

    def frame_value(self):
        with self.lock:
            return self.frame, self.frame_at

    def source_frame_value(self):
        """Image que le relais publie pour cette fiche : celle de la capture de la webcam pour une « webcam par
        lien » (la fiche elle-même ne détient que ce qu'elle a relu par le lien), sinon l'image de la fiche."""
        if not self.webcam_link:
            return self.frame_value()
        capture = self.webcam
        return capture.frame_value() if capture is not None else (None, 0)

    def validate_frame(self, frame, decode=True):
        """JPEG borné (taille, marqueurs, format, dimensions) ; rend (largeur, hauteur). ``decode=False`` ne lit que
        l'en-tête : réservé aux images que ce Worker vient d'encoder lui-même (capture USB), contrôlées en entier
        une fois par seconde."""
        if not 100 <= len(frame) < CAMERA_MAX_FRAME_BYTES or not frame.startswith(b"\xff\xd8") or not frame.endswith(b"\xff\xd9"):
            raise ValueError("Image JPEG invalide")
        target = runtime_dependencies(self.root)
        if str(target) not in sys.path:
            sys.path.insert(0, str(target))
        Image = importlib.import_module("PIL.Image")
        with Image.open(io.BytesIO(frame)) as picture:
            if picture.format != "JPEG" or not 0 < picture.width <= 4096 or not 0 < picture.height <= 4096:
                raise ValueError("Dimensions JPEG invalides")
            if decode:
                picture.load()
            return picture.width, picture.height

    def validate_own_frame(self, frame):
        """Image encodée par ce Worker (capture USB) : en-tête à chaque image, décodage complet une fois par seconde.
        Garde la taille obtenue dans ``capture_size``."""
        now = time.monotonic()
        decode = now - self._decode_checked >= USB_DECODE_CHECK_S
        self.capture_size = self.validate_frame(frame, decode=decode)
        if decode:
            self._decode_checked = now

    def report_frame(self):
        """Image jointe au rapport du site : telle quelle sous 1 Mio (toujours le cas jusqu'en 1080p), sinon
        recompressée (qualité 80, 65, 50, puis moitié de la taille) ; None si la réduction est impossible.
        Une seule réduction par image (cache), une image par tour de boucle : coût négligeable."""
        frame = self.frame
        if frame is None or len(frame) <= REPORT_MAX_FRAME_BYTES:
            return frame
        if self._report_cache[0] is frame:
            return self._report_cache[1]
        reduced = None
        try:
            Image = importlib.import_module("PIL.Image")
            with Image.open(io.BytesIO(frame)) as picture:
                picture = picture.convert("RGB")
                for scale in (1, 2, 4):
                    if scale > 1:
                        picture = picture.resize((max(1, picture.width // 2), max(1, picture.height // 2)))
                    for quality in (80, 65, 50):
                        buffer = io.BytesIO()
                        picture.save(buffer, format="JPEG", quality=quality)
                        if buffer.tell() <= REPORT_MAX_FRAME_BYTES:
                            reduced = buffer.getvalue()
                            break
                    if reduced is not None:
                        break
        except Exception:  # noqa: BLE001 - Pillow absent ou image illisible : le rapport part sans image
            reduced = None
        self._report_cache = (frame, reduced)
        return reduced

    def progress(self, percent, message, connected=False):
        with self.lock:
            self.snapshot.update(progress=percent, message=message, connected=connected)

    def start(self):
        if not self.spec.get("enabled") or (self.thread and self.thread.is_alive()):
            return
        self.thread = threading.Thread(target=self._run, name="equipment-"+self.spec["id"][:8], daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        capture = self.webcam
        if capture is not None:
            capture.close()  # arrête la capture de la webcam (processus fils compris) et libère sa réservation
        if self.client:
            self.client.disconnect()
            self.client.loop_stop()
        if self.driver:
            self.driver.close()
        controller = self.grbl
        if controller is not None:
            try:
                controller.disconnect()  # annule le job (arrêt de protection), coupe le laser, ferme la liaison
            except Exception:
                pass
            if self.grbl_thread and self.grbl_thread is not threading.current_thread():
                self.grbl_thread.join(timeout=5)
        if self.socket:
            try:
                self.socket.close()
            except OSError:
                pass
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=7)
        self.progress(100, "Déconnexion demandée ; transport fermé.")

    def report(self):
        with self.lock:
            result = dict(self.snapshot)
            if self.telemetry:
                result["telemetry"] = dict(self.telemetry)
            if self.spec["kind"] in {"printer", "bambu"}:
                # Le Worker est ce qui signe : le dashboard peut alors
                # distinguer « signatures exigées, on sait signer » de
                # « signatures exigées, aucune identité disponible ».
                result["signing_identity"] = bool(self.signer.material)
            if self.spec["kind"] in GRBL_KINDS:
                result["grbl"] = self.grbl_report()
            if self.frame and time.time()-self.frame_at < 8:
                frame = self.report_frame()
                if frame is not None:
                    result["frame"] = base64.b64encode(frame).decode()
                else:
                    result["connected"] = False
                    result["message"] = "Image trop lourde pour le site et réduction impossible (Pillow absent) : baisse la résolution de la caméra."
            elif self.spec["kind"] == "camera":
                result["connected"] = False
            if self.spec["kind"] == "camera":
                result["relay_installed"] = (self.root / "components" / "printer-link" / "equipment_video_relay.py").is_file()
                # Lien MJPEG local rapporté pour toute caméra (P1S, flux MJPEG, USB) ; absent = arrêté.
                result.update(relay_running=False, relay_verified=False)
            result.update(self.relay_state)
            if self.spec["kind"] == "camera" and not result.get("relay_running"):
                result.pop("relay_url", None)
            if self.spec["settings"].get("camera_mode") == "relay":
                # Seul le mode relais P1S exige l'image vérifiée par le relais pour être « connecté ».
                result["connected"] = bool(result.get("connected") and result.get("relay_running") and result.get("relay_verified"))
                if not result["connected"]:
                    result.pop("relay_url", None)
                    # A listening MJPEG socket is not evidence that the camera
                    # is reachable. Preserve the native reader's error/stage.
                    if self.relay_state.get("relay_message") and (not result.get("relay_running") or self.snapshot.get("connected")):
                        result["message"] = self.relay_state["relay_message"]
            return result

    def _run(self):
        try:
            connector = validate_connector(self.spec)
            if self.spec["kind"] == "camera":
                self._camera()
            elif self.spec["kind"] in GRBL_KINDS:
                self._grbl()
            elif connector in HTTP_PRINTER_PORTS:
                self._http_printer()
            else:
                self._printer()
        except (ImportError, ModuleNotFoundError) as error:
            self.progress(0, dependency_error(error))
        except Exception as error:
            # Never copy socket URLs, tokens or MQTT exceptions into browser logs.
            self.progress(0, f"Connexion impossible ({type(error).__name__}). Vérifie le réseau local, le code et l’installation du relais.")
        finally:
            if self.webcam_link:
                self._webcam_link_close()  # la capture de la webcam ne survit jamais au lecteur de la fiche

    def _printer(self):
        target = runtime_dependencies(self.root)
        if str(target) not in sys.path:
            sys.path.insert(0, str(target))
        mqtt = importlib.import_module("paho.mqtt.client")
        self.signer.load()
        cfg = self.spec["settings"]
        validate_connector(self.spec)
        cloud = self.spec["kind"] == "bambu"
        host = ("cn.mqtt.bambulab.com" if cfg.get("region") == "cn" else "us.mqtt.bambulab.com") if cloud else local_host(cfg["host"])
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                             client_id="alpine_"+self.spec["id"].replace("-", "")[:24], protocol=mqtt.MQTTv311)
        self.client = client
        if cloud:
            client.username_pw_set("u_"+cfg["cloud_user_id"].removeprefix("u_"), cfg["cloud_token"])
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        else:
            client.username_pw_set("bblp", cfg["access_code"])
            client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLSv1_2)
            client.tls_insecure_set(True)  # Bambu LAN certificate, same existing transport.
        serial = cfg["serial"]
        def connected(_client, _userdata, _flags, reason, _properties):
            self.signer.reset()
            if reason != 0:
                self.progress(0, "Authentification imprimante refusée : vérifie le code local ou le compte Bambu.")
                return
            self.progress(80, "Authentifié — abonnement à la télémétrie…")
            client.subscribe(f"device/{serial}/report", qos=0)
            # Lecture pure : la version du firmware décide si le Mode
            # développeur existe sur cette machine. Aucune action matérielle.
            # Demandée avant le pushall pour que la télémétrie complète reste
            # la dernière requête envoyée à la connexion.
            client.publish(f"device/{serial}/request", json.dumps({"info": {"sequence_id": "0", "command": "get_version"}}))
            client.publish(f"device/{serial}/request", json.dumps({"pushing": {"sequence_id": "0", "command": "pushall", "version": 1, "push_target": 1}}))
        def message(_client, _userdata, msg):
            if len(msg.payload) > 128*1024:
                return
            try:
                packet = json.loads(msg.payload)
                self.signer.accept(packet)
                payload = packet.get("print", {})
            except (ValueError, AttributeError):
                return
            info = packet.get("info") if isinstance(packet, dict) else None
            if isinstance(info, dict) and info.get("command") == "get_version":
                # Même enveloppe que la lecture de version côté dashboard : le
                # module « ota » porte la version du firmware.
                for module in info.get("module") or []:
                    if not isinstance(module, dict) or str(module.get("name") or "").strip() != "ota":
                        continue
                    version = str(module.get("sw_ver") or "").strip()
                    if version:
                        with self.lock:
                            self.telemetry["ota_version"] = version
            if not isinstance(payload, dict):
                return
            if payload.get("sequence_id") and payload.get("command"):
                with self.lock:
                    self.acks[str(payload["sequence_id"])] = {"command": str(payload["command"]), "result": str(payload.get("result") or "").lower()}
                    while len(self.acks) > 128:
                        self.acks.pop(next(iter(self.acks)))
            with self.lock:
                values = {k: v for k, v in payload.items() if k in TELEMETRY_KEYS}
                self.telemetry.update(values)
            if values:
                self.progress(100, "Imprimante connectée — télémétrie reçue.", True)
        client.on_connect = connected
        client.on_message = message
        client.on_connect_fail = lambda *_: self.progress(15, "Connexion MQTT TLS impossible depuis le Worker : vérifie l’adresse, le port, le pare-feu et l’alimentation de l’imprimante.")
        client.on_disconnect = lambda *_: self.progress(20, "Connexion perdue — reconnexion automatique via le Worker…")
        client.reconnect_delay_set(min_delay=2, max_delay=30)
        self.progress(20, "Ouverture MQTT TLS depuis le Worker…")
        client.connect_async(host, port_number(cfg.get("port"), 8883), keepalive=30)
        client.loop_start()
        self.stop.wait()

    def _http_printer(self):
        """Lecteur HTTP local (Moonraker/OctoPrint) : même télémétrie, même contrat.

        Aucune dépendance compilée n'est nécessaire, donc aucun import paresseux
        de paho ; seul le pilote est chargé au démarrage du fil d'exécution."""
        try:
            from .equipment_printer_http import DriverError, build_driver
        except ImportError:
            from equipment_printer_http import DriverError, build_driver
        driver = self.driver = build_driver(self.spec)
        self.progress(20, "Ouverture HTTP locale depuis le Worker…")
        delay = 2
        while not self.stop.is_set():
            try:
                values = driver.poll()
            except DriverError as error:
                # Le pilote nettoie déjà ses messages : ni URL, ni clé, ni corps.
                self.progress(15, str(error))
                if self.stop.wait(delay):
                    break
                delay = min(30, delay*2)
                continue
            with self.lock:
                self.telemetry.update({k: v for k, v in values.items() if k in TELEMETRY_KEYS})
            self.progress(100, "Imprimante connectée — télémétrie reçue.", True)
            delay = 2
            if self.stop.wait(2):
                break

    def _usb_camera_isolated(self, options):
        """Même déroulé que la capture en processus, mais OpenCV tourne dans un processus fils par caméra."""
        script = Path(__file__).with_name(USB_ISOLATED_SCRIPT)
        delay = 2
        while not self.stop.is_set():
            process = None
            try:
                self.progress(20, "Ouverture de la caméra USB sur le Worker…")
                if not script.is_file():
                    raise FileNotFoundError("usb_camera_capture.py absent : lance « Réparer » ou mets le Worker à jour")
                env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
                process = subprocess.Popen([sys.executable, str(script), "--root", str(self.root), "--options", json.dumps(options),
                                            "--parent", str(os.getpid())],
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env,
                                           cwd=str(self.root), creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                # L'arrêt demandé tue le processus : la lecture bloquante ci-dessous se termine alors sur une fin de flux.
                threading.Thread(target=self._usb_kill_on_stop, args=(process,), daemon=True).start()
                stamps, reported, measured, backend = [], 0.0, False, ""
                self.capture_fps = self.capture_size = None
                while True:
                    header = process.stdout.readline()
                    if not header:
                        raise ConnectionError("La capture de la caméra USB s’est arrêtée")
                    if header.startswith(b"STATUS "):
                        status = json.loads(header[7:].decode("utf-8", "replace"))
                        if status.get("dependency"):
                            self.progress(0, str(status.get("error") or usb_dependency_error(ImportError(name="cv2"))))
                            return
                        if not status.get("opened") or status.get("error"):
                            seen = str(status.get("seen") or "")
                            raise ConnectionError(str(status.get("error") or "Caméra USB indisponible")
                                                  + (f". Caméras ouvrables vues par le Worker (index) — {seen}" if seen else ""))
                        backend = str(status.get("backend") or "")
                        self.progress(60, "Caméra USB ouverte — attente de la première image…")
                        continue
                    if not header.startswith(b"FRAME "):
                        continue
                    size = int(header.split()[1])
                    if not 0 < size < CAMERA_MAX_FRAME_BYTES:
                        raise ConnectionError("La capture de la caméra USB a annoncé une image hors bornes")
                    frame = process.stdout.read(size)
                    if len(frame) != size:
                        raise ConnectionError("La capture de la caméra USB s’est arrêtée")
                    self.validate_own_frame(frame)
                    with self.lock:
                        self.frame, self.frame_at = frame, time.time()
                    now = time.monotonic()
                    stamps = [stamp for stamp in stamps if now - stamp <= USB_FPS_WINDOW_S] + [now]
                    if len(stamps) >= 2 and now - stamps[0] >= 1.0:
                        self.capture_fps = (len(stamps) - 1) / (now - stamps[0])
                    if reported == 0.0 or now - reported >= USB_FPS_WINDOW_S or (self.capture_fps and not measured):
                        reported, measured = now, bool(self.capture_fps)
                        self.progress(100, usb_capture_message(self.capture_fps, options["fps"], backend, self.capture_size,
                                                               (options["width"], options["height"])), True)
                    delay = 2
            except Exception as error:
                if self.stop.is_set():
                    break
                detail = str(error)
                self.progress(15, f"Caméra USB indisponible ({type(error).__name__}) — nouvel essai automatique." + (f" {detail}." if detail else ""))
            finally:
                if process is not None:
                    try:
                        process.kill()
                    except Exception:
                        pass
                    try:
                        process.stdout.close()
                    except Exception:
                        pass
            if self.stop.wait(delay):
                break
            delay = min(30, delay * 2)

    def _usb_kill_on_stop(self, process):
        self.stop.wait()
        try:
            process.kill()
        except Exception:
            pass

    def _camera(self):
        cfg = self.spec["settings"]
        connector = validate_connector(self.spec)
        if connector == USB_CAMERA_CONNECTOR:
            self._usb_camera()
            return
        delay = 2
        link = self.webcam_link
        while not self.stop.is_set():
            stream = None
            stage = "ouverture du flux MJPEG" if connector == "mjpeg_http" else "connexion TCP à la caméra"
            if link and not self._webcam_link_ready():
                # Relais arrêté, webcam pas encore ouverte ou déjà prise : rien à lire sur le lien pour l'instant.
                if self.stop.wait(1):
                    break
                continue
            try:
                self.progress(20, "Lecture de la webcam par son lien MJPEG local…" if link else "Connexion caméra depuis le réseau du Worker…")
                # A managed MJPEG relay consumes this native reader; it never
                # opens its own output URL as an input (which would deadlock).
                url = cfg.get("stream_url") if connector == "mjpeg_http" else ""
                if url:
                    # Relais loopback 1.3.0 (agent 1.50.20) : jeton de lecture par source. Un lien sans jeton (webcam
                    # par lien, lien collé avant la mise à jour) reçoit celui que ce Worker a créé pour la source.
                    url = relay_with_token(url, relay_token_directory(self.root))
                    parsed = urlsplit(url)
                    query = parse_qs(parsed.query)
                    if parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.port != port_number(cfg.get("port"), 1984) or parsed.scheme != "http" or parsed.path != "/api/stream.mjpeg" or parsed.username or parsed.password or parsed.fragment or not {"src"} <= set(query) <= {"src", "token"} or any(len(values) != 1 for values in query.values()) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", query["src"][0]) or ("token" in query and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", query["token"][0])):
                        raise ValueError("Flux de caméra non autorisé")
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                    stream = opener.open(url, timeout=5)
                    self.socket = stream
                    read = lambda: stream.read1(16384)
                else:
                    raw = socket.create_connection((local_host(cfg["host"]), port_number(cfg.get("port"), 6000)), timeout=5)
                    stage = "négociation TLS avec la caméra"
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                    try:
                        stream = context.wrap_socket(raw, server_hostname=cfg["host"])
                    except Exception:
                        raw.close()
                        raise
                    stream.settimeout(5)
                    self.socket = stream
                    stream.sendall(struct.pack("<IIII", 0x40, 0x3000, 0, 0) + b"bblp".ljust(32, b"\0") + cfg["access_code"].encode("ascii")[:32].ljust(32, b"\0"))
                    read = lambda: stream.recv(65536)
                self.progress(60, "Transport ouvert — attente de la première image…")
                stage = "réception/décodage des images après authentification"
                buffer = b""
                last_image = time.monotonic()
                stamps, told = [], 0.0
                while not self.stop.is_set():
                    chunk = read()
                    if not chunk:
                        raise ConnectionError("Flux fermé")
                    buffer += chunk
                    if len(buffer) > 2*CAMERA_MAX_FRAME_BYTES or time.monotonic()-last_image > 20:
                        raise ValueError("Flux JPEG invalide")
                    while True:
                        begin = buffer.find(b"\xff\xd8")
                        end = buffer.find(b"\xff\xd9", max(0, begin)+2)
                        if begin < 0 or end < 0:
                            break
                        frame, buffer = buffer[begin:end+2], buffer[end+2:]
                        if len(frame) < CAMERA_MAX_FRAME_BYTES:
                            if link:
                                self.validate_own_frame(frame)   # image que ce Worker vient d'encoder et de publier
                            else:
                                self.validate_frame(frame)
                            with self.lock:
                                self.frame, self.frame_at = frame, time.time()
                            last_image = time.monotonic()
                            if link:
                                stamps = [stamp for stamp in stamps if last_image - stamp <= USB_FPS_WINDOW_S] + [last_image]
                                if len(stamps) >= 2 and last_image - stamps[0] >= 1.0:
                                    self.link_fps = (len(stamps) - 1) / (last_image - stamps[0])
                                if told == 0.0 or last_image - told >= USB_FPS_WINDOW_S:
                                    told = last_image
                                    self.progress(100, self._webcam_link_message(), True)
                            else:
                                self.progress(100, "Caméra connectée — images reçues via le Worker.", True)
                            delay = 2
            except ImportError as error:
                self.progress(0, dependency_error(error))
                return
            except Exception as error:
                if link:
                    self.progress(15, f"Lien MJPEG local de la webcam illisible : {stage} ({type(error).__name__}) — nouvel essai automatique.")
                else:
                    self.progress(15, f"Caméra indisponible : {stage} ({type(error).__name__}) — nouvel essai automatique.")
            finally:
                if stream:
                    stream.close()
                self.socket = None
            if self.stop.wait(1 if link else delay):
                break
            delay = min(30, delay*2)

    def _webcam_link_ready(self):
        """Webcam par lien : le relais de la fiche tourne et la capture de la webcam fournit une image fraîche.

        La capture réutilise telle quelle la chaîne du connecteur usb_camera (Device fils, processus de capture
        sous Windows) ; elle n'est ouverte que si le relais tourne, et fermée dès qu'il est arrêté. Le message de
        la fiche reprend celui de la capture (webcam introuvable, déjà ouverte, OpenCV absent…)."""
        if self.relay_state.get("relay_running") is not True:
            self._webcam_link_close()
            self.progress(10, str(self.relay_state.get("relay_message") or "Lien MJPEG local arrêté : clique « Démarrer le relais » (ou Connecter) pour lancer la webcam."))
            return False
        capture = self.webcam
        ended = capture is not None and (capture.stop.is_set() or not (capture.thread and capture.thread.is_alive()))
        # Capture terminée d'elle-même (OpenCV absent…) : son message reste affiché, relance au plus toutes les 10 s.
        if capture is None or (ended and time.monotonic() - self.webcam_started >= 10):
            if capture is not None:
                capture.close()
            self.webcam_started = time.monotonic()
            settings = dict(self.spec["settings"], connector=USB_CAMERA_CONNECTOR)
            capture = Device({"id": self.spec["id"], "revision": self.spec["revision"], "kind": "camera", "enabled": True, "settings": settings}, self.root)
            capture.capture_owner, capture.capture_link = self, True
            self.webcam = capture
            capture.start()
        self.capture_fps = getattr(capture, "capture_fps", None)
        frame, stamp = capture.frame_value()
        if frame and time.time() - stamp < 8:
            return True
        with capture.lock:
            percent, detail = capture.snapshot.get("progress", 0), str(capture.snapshot.get("message") or "")
        self.progress(min(60, int(percent or 0)), detail or "Ouverture de la webcam sur le Worker…")
        return False

    def _webcam_link_message(self):
        """Fiche « webcam par lien » : ce que la webcam fournit vraiment et ce que le lien sert, à côté du réglage."""
        text = "Webcam connectée — images lues par son lien MJPEG local."
        capture, cfg = self.webcam, self.spec["settings"]
        size = getattr(capture, "capture_size", None)
        fps = getattr(capture, "capture_fps", None)
        parts = []
        if size or fps:
            got = (f"{size[0]}×{size[1]}" if size else "") + (" à " if size and fps else "") + (f"{fps:.1f} image(s)/s".replace(".", ",", 1) if fps else "")
            wanted_size = f"{cfg.get('width')}×{cfg.get('height')} " if cfg.get("width") and cfg.get("height") else ""
            parts.append(f"Capture : {got} (demandé : {wanted_size}à {cfg.get('fps', 10)} image(s)/s)")
        if self.link_fps:
            parts.append(f"lien : {self.link_fps:.1f} image(s)/s".replace(".", ",", 1) + f" (demandé : {cfg.get('relay_fps') or 10})")
        return text + (" " + " ; ".join(parts) + "." if parts else "")

    def _webcam_link_close(self):
        capture, self.webcam = self.webcam, None
        if capture is not None:
            capture.close()

    def _usb_camera(self):
        """Caméra USB (UVC) branchée au Worker : la webcam est réservée à cette fiche le temps de la capture."""
        options = usb_camera_settings(self.spec["settings"])
        key = (options["index"], options["backend"])
        while not webcam_claim(key, self.capture_owner, self.capture_link):
            self.progress(0, WEBCAM_BUSY_MESSAGE.format(index=options["index"]))
            if self.stop.wait(5):
                return
        try:
            self._usb_camera_capture(options)
        finally:
            webcam_release(key, self.capture_owner)

    def _usb_camera_capture(self, options):
        """Caméra USB (UVC) branchée au Worker : capture OpenCV, images JPEG comme les autres caméras."""
        if USB_ISOLATED_CAPTURE:
            self._usb_camera_isolated(options)
            return
        target = runtime_dependencies(self.root)
        if str(target) not in sys.path:
            sys.path.insert(0, str(target))
        delay = 2
        enumerated = False
        while not self.stop.is_set():
            capture = None
            try:
                self.progress(20, "Ouverture de la caméra USB sur le Worker…")
                cv2 = usb_capture_module()
                capture = usb_open_capture(cv2, options)
                if not capture.isOpened():
                    raise ConnectionError("Caméra USB introuvable ou déjà utilisée par un autre programme")
                enumerated = False
                self.progress(60, "Caméra USB ouverte — attente de la première image…")
                interval, misses, quality, stamps, reported, measured = 1.0 / options["fps"], 0, 80, [], 0.0, False
                self.capture_fps = self.capture_size = None
                while not self.stop.is_set():
                    started = time.monotonic()
                    ok, image = capture.read()
                    if not ok or image is None:
                        misses += 1
                        if misses >= 30:
                            raise ConnectionError("La caméra USB ne fournit plus d’images")
                        if self.stop.wait(0.2):
                            break
                        continue
                    misses = 0
                    frame, quality = usb_encode_frame(cv2, image, quality)
                    if frame is not None:
                        self.validate_own_frame(frame)
                        with self.lock:
                            self.frame, self.frame_at = frame, time.time()
                        now = time.monotonic()
                        stamps = [stamp for stamp in stamps if now - stamp <= USB_FPS_WINDOW_S] + [now]
                        if len(stamps) >= 2 and now - stamps[0] >= 1.0:
                            self.capture_fps = (len(stamps) - 1) / (now - stamps[0])
                        # Message mis à jour au plus toutes les 5 s : la fiche affiche la cadence réelle.
                        if reported == 0.0 or now - reported >= USB_FPS_WINDOW_S or (self.capture_fps and not measured):
                            reported, measured = now, bool(self.capture_fps)
                            self.progress(100, usb_capture_message(self.capture_fps, options["fps"], options.get("backend_used", ""), self.capture_size,
                                                                   (options["width"], options["height"])), True)
                        delay = 2
                    # La lecture attend déjà l'image suivante de la caméra : seul le reste de la période est attendu
                    # (attendre une période entière en plus divisait la cadence par deux).
                    if self.stop.wait(max(0.0, interval - (time.monotonic() - started))):
                        break
            except ImportError as error:
                self.progress(0, usb_dependency_error(error))
                return
            except Exception as error:
                seen = ""
                if not enumerated and options["backend"] == "auto":
                    # Une fois par série d'échecs : ce que ce Worker voit vraiment (index ouvrables par backend).
                    enumerated = True
                    try:
                        seen = " Caméras ouvrables vues par le Worker (index) — " + usb_enumerate(cv2) + "."
                    except Exception:
                        seen = ""
                self.progress(15, f"Caméra USB indisponible ({type(error).__name__}) — nouvel essai automatique.{seen}")
            finally:
                if capture is not None:
                    try:
                        capture.release()
                    except Exception:
                        pass
            if self.stop.wait(delay):
                break
            delay = min(30, delay * 2)

    # ------------------------------------------------------------ machines GRBL
    def _grbl(self):
        """Liaison GRBL depuis le Worker : connexion, rapports d'état toutes les 0,5 s, reconnexion bornée."""
        grbl = grbl_module()
        cfg = self.spec["settings"]
        connector = validate_connector(self.spec)
        target = runtime_dependencies(self.root)  # un pyserial déposé à côté des dépendances du relais est trouvé ici
        if str(target) not in sys.path:
            sys.path.insert(0, str(target))
        delay = 2
        while not self.stop.is_set():
            self.progress(20, "Ouverture de la liaison GRBL depuis le Worker…")
            try:
                transport = grbl_transport(connector, cfg)
                controller = grbl.GrblController(transport, self.spec["kind"], on_status=self._grbl_status, on_log=self._grbl_log, interlocks=self.grbl_interlocks)
                controller.connect(timeout=5.0)
            except grbl.GrblError as error:
                message = str(error)
                if message.startswith("pyserial absent"):
                    self.progress(0, message)  # réessayer sans la dépendance ne servirait à rien
                    return
                self.progress(15, message + " Nouvel essai automatique.")
            except Exception as error:
                self.progress(15, f"Connexion GRBL impossible ({type(error).__name__}) — nouvel essai automatique.")
            else:
                with self.lock:
                    self.grbl = controller
                self.progress(100, self._grbl_message(controller), True)
                delay = 2
                self._grbl_watch(controller)
                with self.lock:
                    self.grbl = None
                try:
                    controller.disconnect()
                except Exception:
                    pass
                if self.stop.is_set():
                    break
                self.progress(20, "Liaison GRBL perdue — reconnexion automatique via le Worker…")
            if self.stop.wait(delay):
                break
            delay = min(30, delay * 2)

    def _grbl_watch(self, controller):
        """Demande un rapport « ? » toutes les 0,5 s ; trois silences consécutifs = liaison perdue."""
        grbl = grbl_module()
        misses = 0
        while not self.stop.is_set():
            try:
                controller.status(timeout=1.0)
                misses = 0
            except grbl.GrblTimeout:
                misses += 1
                if misses >= 3:
                    return
            except Exception:
                return
            if self.stop.wait(0.5):
                return

    def _grbl_message(self, controller):
        status = controller.last_status or {}
        state = status.get("state") or "inconnu"
        text = f"GRBL {controller.version or ''} connecté — état {state}.".replace("  ", " ")
        if controller.alarm:
            text += " " + grbl_module().describe_alarm(controller.alarm)
        return text

    def _grbl_status(self, status):
        controller = self.grbl
        if controller is not None and not self.stop.is_set():
            self.progress(100, self._grbl_message(controller), True)

    def _grbl_log(self, message):
        with self.lock:
            self.grbl_message = str(message)[:300]

    def _grbl_door_closed(self):
        """True/False d'après le dernier rapport de la carte, None sans rapport."""
        controller = self.grbl
        status = controller.last_status if controller is not None else None
        if not status:
            return None
        return status.get("state") != "Door" and "D" not in (status.get("pins") or [])

    def grbl_interlocks(self, operator=True):
        """Sécurités exigées avant et pendant un job : ``[{"key", "label", "satisfied"}]`` (contrat du contrôleur)."""
        result = []
        for item in self.spec["settings"].get("interlocks") or []:
            if not isinstance(item, dict) or not item.get("required"):
                continue
            name = str(item.get("name") or "Sécurité")
            if item.get("kind") == "door":
                result.append({"key": name, "label": name, "satisfied": self._grbl_door_closed() is True})
            elif operator:
                result.append({"key": name, "label": name, "satisfied": self.grbl_checklist is True})
        return result

    def grbl_interlock_states(self):
        states = {}
        for item in self.spec["settings"].get("interlocks") or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            if item.get("kind") == "door":
                closed = self._grbl_door_closed()
                states[str(item["name"])] = "unknown" if closed is None else ("ok" if closed else "failed")
            else:
                states[str(item["name"])] = "ok" if self.grbl_checklist else "unknown"
        return states

    def grbl_report(self):
        """Bloc « grbl » du rapport : état, positions, alarme, job en cours, sécurités."""
        controller = self.grbl
        snapshot = controller.snapshot() if controller is not None else {}
        with self.lock:
            job = dict(self.grbl_job)
        return {"state": snapshot.get("state"), "mpos": list(snapshot["mpos"]) if snapshot.get("mpos") else None,
                "wpos": list(snapshot["wpos"]) if snapshot.get("wpos") else None,
                "feed": snapshot.get("feed"), "spindle": snapshot.get("spindle"),
                "alarm": f"ALARM:{snapshot['alarm']} — {snapshot.get('alarm_label')}" if snapshot.get("alarm") else None,
                "version": snapshot.get("version"), "laser_mode": snapshot.get("laser_mode") if controller is not None else None,
                # Rapport étendu (agent 1.33.0) : le site filtre et borne ces champs de son côté.
                "substate": snapshot.get("substate"), "overrides": snapshot.get("overrides"),
                "pins": list(snapshot.get("pins") or []), "accessories": list(snapshot.get("accessories") or []),
                "buffer": snapshot.get("buffer"), "line": snapshot.get("line"),
                "settings": dict(snapshot.get("settings") or {}), "build_info": list(snapshot.get("build_info") or [])[:8],
                "messages": list(snapshot.get("messages") or [])[-20:],
                "stream": {"active": bool(job.get("active")), "name": str(job.get("name") or ""), "sent_lines": int(job.get("done") or 0),
                           "total_lines": int(job.get("total") or 0), "percent": float(job.get("percent") or 0.0),
                           "started_at": job.get("started_at"), "finished_at": job.get("finished_at"),
                           "ok": job.get("ok"), "error": job.get("error"), "reason": job.get("reason"),
                           "loop": bool(job.get("loop")), "passes": int(job.get("passes") or 0), "door_open": bool(job.get("door_open")),
                           "laser_pct": float(job.get("laser_pct") or 0.0)},
                "interlocks": self.grbl_interlock_states()}

    def grbl_start_job(self, name, text, total, command_id, checklist_confirmed, progress, operator_checks=True, loop=False, door_open=False, laser_pct=0.0):
        """Envoi du G-code dans un fil dédié : le fil de commande de l'agent reste libre pour hold/stop.

        ``loop`` (cadrage seulement) repasse le contour jusqu'à ``grbl_loop_stop`` ; ``door_open``
        (cadrage seulement, laser coupé) ne vérifie pas la porte : la carte décide si elle bouge.
        """
        with self.lock:
            if self.grbl_job.get("active") or (self.grbl_thread and self.grbl_thread.is_alive()):
                raise ValueError("Un job GRBL est déjà en cours sur cette machine.")
            if self.grbl is None:
                raise ValueError("Contrôleur GRBL non connecté.")
            self.grbl_checklist = bool(checklist_confirmed)
            self.grbl_loop_stop.clear()
            self.grbl_job = {"active": True, "name": name, "command_id": command_id, "sent": 0, "done": 0, "total": total, "percent": 0.0,
                             "started_at": time.time(), "finished_at": None, "ok": None, "error": None, "reason": None,
                             "loop": bool(loop), "passes": 0, "door_open": bool(door_open), "laser_pct": float(laser_pct or 0.0)}
            self.grbl_thread = threading.Thread(target=self._grbl_run_job, args=(text, progress, operator_checks, bool(loop), bool(door_open)),
                                                name="grbl-job-" + self.spec["id"][:8], daemon=True)
            self.grbl_thread.start()

    def _grbl_count_pass(self, count):
        with self.lock:
            self.grbl_job["passes"] = int(count)

    def _grbl_run_job(self, text, progress, operator_checks, loop=False, door_open=False):
        raise_current_thread_priority()   # fil dédié au travail : la priorité disparaît avec lui
        grbl = grbl_module()
        controller = self.grbl
        outcome = {"ok": False, "error": None, "reason": None}
        fire_stops = getattr(controller, "fire_stops", 0)  # un arrêt incendie pendant le job en devient le motif
        # Capot ouvert autorisé (cadrage, laser coupé) : aucune sécurité vérifiée par le Worker pendant le parcours.
        provider = (lambda: []) if door_open else (lambda: self.grbl_interlocks(operator=operator_checks))

        def stopping():
            return self.grbl_loop_stop.is_set() or self.stop.is_set()

        def advance(job):
            with self.lock:
                self.grbl_job.update(sent=job.get("sent", 0), done=job.get("done", 0), percent=job.get("percent", 0))
            if progress:
                try:
                    progress(dict(job))
                except Exception:
                    pass

        try:
            if controller is None:
                raise grbl.GrblError("Contrôleur GRBL non connecté.")
            cycle_parts = frame_cycle(text) if loop else None
            if cycle_parts:
                # Contour fermé : repassé sans pause ni retour à l'origine ; le laser (s'il est allumé) le reste
                # d'un tour à l'autre et ne s'éteint qu'à l'arrêt du cadrage.
                report = controller.stream_cycle(*cycle_parts, progress=advance, should_cancel=self.stop.is_set, should_stop=stopping,
                                                 interlocks=provider, allow_door=door_open, door_timeout=GRBL_FRAME_DOOR_WAIT,
                                                 max_seconds=GRBL_FRAME_LOOP_MAX_SECONDS, planner_ahead=GRBL_FRAME_PLANNER_AHEAD,
                                                 on_cycle=self._grbl_count_pass)
                outcome.update(ok=bool(report.get("ok")), error=report.get("error"), reason=report.get("reason"))
                if report.get("reason") == "loop_timeout":
                    outcome["error"] = f"Cadrage en continu arrêté au bout de {GRBL_FRAME_LOOP_MAX_SECONDS // 60} min : relance-le si besoin."
                if report.get("ok"):
                    controller.wait_idle(GRBL_FRAME_IDLE_WAIT, should_stop=self.stop.is_set, door_timeout=GRBL_FRAME_DOOR_WAIT if door_open else None)
                return
            deadline = time.monotonic() + GRBL_FRAME_LOOP_MAX_SECONDS
            passes = 0
            while True:
                report = controller.stream(text, progress=advance, should_cancel=self.stop.is_set, interlocks=provider, allow_door=door_open,
                                           should_stop=stopping if loop else None, max_pending=GRBL_FRAME_PENDING_LINES if loop else None)
                passes += 1
                with self.lock:
                    self.grbl_job["passes"] = passes
                outcome.update(ok=bool(report.get("ok")), error=report.get("error"), reason=report.get("reason"))
                if not report.get("ok"):
                    break
                if loop or door_open:
                    # Les derniers segments s'exécutent après leur « ok » ; capot ouvert, une carte qui
                    # reste en état Door n'a rien exécuté : on le dit au lieu de boucler dans le vide.
                    # Même après « Arrêter le cadrage », on attend la fin du dernier segment : le job ne se
                    # termine que machine arrêtée (seule une déconnexion interrompt l'attente).
                    controller.wait_idle(GRBL_FRAME_IDLE_WAIT, should_stop=self.stop.is_set, door_timeout=GRBL_FRAME_DOOR_WAIT if door_open else None)
                if not loop or stopping():
                    if loop:
                        outcome.update(reason="stopped")
                    break
                if time.monotonic() >= deadline:
                    outcome.update(reason="loop_timeout", error=f"Cadrage en continu arrêté au bout de {GRBL_FRAME_LOOP_MAX_SECONDS // 60} min : relance-le si besoin.")
                    break
                if self.grbl_loop_stop.wait(GRBL_FRAME_LOOP_PAUSE):
                    outcome.update(reason="stopped")
                    break
        except grbl.GrblHalted as error:
            outcome.update(ok=False, error=str(error), reason="fire_stop")
        except grbl.GrblError as error:
            report = getattr(error, "report", None) or {}
            outcome.update(ok=False, error=str(error), reason=report.get("reason") or ("door" if "état Door" in str(error) else "error"))
        except Exception as error:
            outcome.update(ok=False, error=f"Liaison GRBL interrompue pendant le job ({type(error).__name__}).", reason="link")
        finally:
            if not outcome["ok"] and controller is not None and getattr(controller, "fire_stops", 0) != fire_stops:
                # ALARM:3, bannière ou écriture refusée : la cause réelle est l'arrêt incendie demandé pendant le job.
                outcome.update(reason="fire_stop", error="Arrêt incendie par la surveillance IA : travail interrompu (reset GRBL, "
                                                         "laser et mouvement coupés). Acquittement d’un opérateur exigé avant tout nouveau travail.")
            with self.lock:
                self.grbl_job.update(active=False, finished_at=time.time(), **outcome)
                self.grbl_checklist = False

    def grbl_wait_started(self, timeout):
        """Attend que le job envoie ses premières lignes, ou qu'il échoue tout de suite."""
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                job = dict(self.grbl_job)
            controller = self.grbl
            if not job.get("active") or job.get("done", 0) > 0 or (controller is not None and controller.snapshot().get("streaming")):
                return job
            if time.monotonic() >= deadline:
                return job
            time.sleep(0.05)


# ------------------------------------------------------------------ vue fluide du site (equipment_camera_live_v1)
# Le site annonce, dans la réponse signée de la boucle d'équipements (clé « camera_live »), les caméras regardées en ce
# moment, leur profil (cadence, largeur, qualité JPEG) et un ticket à durée courte. Le Worker pousse alors la DERNIÈRE
# image disponible, réduite au profil, par une connexion gardée ouverte. Rien n'est mis en file : une image en retard
# est sautée. Sans la clé (site plus ancien) ou sans spectateur, ce code ne fait rien et rien d'autre ne change :
# l'image du rapport (une par tour de boucle) continue de partir comme avant.
SITE_LIVE_ROUTE = re.compile(r"/api/worker-protocol/equipment/[a-z][a-z-]{0,40}/")
SITE_LIVE_WAIT_S = 0.004          # attente d'une nouvelle image de la capture
SITE_LIVE_TIMEOUT_S = 8           # délai d'un envoi
SITE_LIVE_LANES_FPS = 10          # à partir de cette cadence, deux connexions envoient en alternance
SITE_LIVE_WINDOW_S = 2.0          # fenêtre de mesure de la cadence envoyée
SITE_LIVE_MIN_QUALITY = 35
SITE_LIVE_MIN_WIDTH = 480
SITE_LIVE_MAX_STREAMS = 8


def site_live_profile(entry):
    """Profil d'une caméra regardée, tel qu'envoyé par le site, borné ; None s'il est inutilisable."""
    if not isinstance(entry, dict):
        return None
    try:
        fps = float(entry.get("fps"))
        width = int(entry.get("width") or 0)
        quality = int(entry.get("quality"))
        max_bytes = int(entry.get("max_bytes") or 1024 * 1024)
        revision = int(entry.get("revision"))
    except (TypeError, ValueError):
        return None
    ticket = entry.get("ticket")
    if (not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", ticket) or not 1 <= fps <= 30
            or not (width == 0 or 160 <= width <= 4096) or not 20 <= quality <= 95 or not 64 * 1024 <= max_bytes <= CAMERA_MAX_FRAME_BYTES):
        return None
    return {"fps": fps, "width": width, "quality": quality, "max_bytes": max_bytes, "revision": revision, "ticket": ticket}


def site_live_render(frame, width, quality, max_bytes):
    """Image pour la vue du site : largeur ramenée à ``width`` (0 = d'origine), JPEG de qualité ``quality`` abaissée
    par paliers tant que l'image dépasse ``max_bytes``. Rend (octets, (largeur, hauteur), (largeur, hauteur) d'origine)
    ou None si l'image ne tient pas."""
    Image = importlib.import_module("PIL.Image")
    with Image.open(io.BytesIO(frame)) as picture:
        source = picture.size
        if width and picture.width > width:
            height = max(1, round(picture.height * width / picture.width))
            picture.draft("RGB", (width, height))   # réduction à la décompression quand le rapport est d'au moins 2
            picture = picture.convert("RGB")
            if picture.width > width:
                picture = picture.resize((width, height), getattr(Image, "Resampling", Image).BILINEAR)
        else:
            picture = picture.convert("RGB")
        while True:
            buffer = io.BytesIO()
            picture.save(buffer, format="JPEG", quality=quality)
            if buffer.tell() <= max_bytes:
                return buffer.getvalue(), picture.size, source
            if quality <= 25:
                return None
            quality -= 15


class SiteLiveConnection:
    """Connexion HTTP gardée ouverte vers le site : une négociation TLS pour tout le flux, pas une par image."""

    def __init__(self, server, user_agent):
        parsed = urlsplit(str(server or ""))
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
            raise ValueError("HTTPS est obligatoire hors connexion locale.")
        self.secure, self.host, self.port = parsed.scheme == "https", parsed.hostname, parsed.port
        self.prefix = parsed.path.rstrip("/")
        self.user_agent = user_agent
        self.connection = None

    def post(self, path, body, headers):
        """Envoie une image ; rend (statut, réponse JSON ou {}). La connexion est refermée sur toute erreur."""
        try:
            if self.connection is None:
                factory = http.client.HTTPSConnection if self.secure else http.client.HTTPConnection
                self.connection = factory(self.host, self.port, timeout=SITE_LIVE_TIMEOUT_S)
            self.connection.request("POST", self.prefix + path, body=body,
                                    headers={"User-Agent": self.user_agent, "Accept": "application/json", "Content-Type": "image/jpeg", **headers})
            response = self.connection.getresponse()
            content = response.read(64 * 1024)
            if response.will_close:
                self.close()
        except Exception:
            self.close()
            raise
        try:
            data = json.loads(content.decode("utf-8"))
        except ValueError:
            data = {}
        return response.status, data if isinstance(data, dict) else {}

    def close(self):
        connection, self.connection = self.connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


class SiteLiveStream:
    """Envoi des images d'UNE caméra au site tant qu'un spectateur la regarde et que le bail est renouvelé."""

    def __init__(self, device, profile, route, lease, connect):
        self.device, self.connect = device, connect
        self.path = route + urllib.parse.quote(str(device.spec["id"]), safe="")
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.session = uuid.uuid4().hex
        self.seq, self.taken, self.next_pick = 0, 0.0, 0.0
        self.sent, self.busy = [], []          # (instant, durée) des envois réussis ; temps occupé par envoi
        self.send_ms = None
        self.calm = 0                          # fenêtres consécutives avec de la marge (pour remonter la qualité)
        self.adapted = time.monotonic()
        self.threads = []
        self.error = ""
        self.source_width = 0
        self.renew(profile, lease)
        self.quality, self.width = self.profile["quality"], self.profile["width"]

    def renew(self, profile, lease):
        with self.lock:
            previous = getattr(self, "profile", None)
            self.profile = profile
            self.lease_until = time.monotonic() + lease
            if previous is not None and any(previous[key] != profile[key] for key in ("fps", "width", "quality")):
                self.quality, self.width, self.calm = profile["quality"], profile["width"], 0

    def start(self):
        lanes = 2 if self.profile["fps"] >= SITE_LIVE_LANES_FPS else 1
        for index in range(lanes):
            thread = threading.Thread(target=self._lane, name=f"site-live-{self.device.spec['id'][:8]}-{index}", daemon=True)
            self.threads.append(thread)
            thread.start()

    def alive(self):
        return any(thread.is_alive() for thread in self.threads)

    def close(self):
        self.stop.set()

    def stopping(self):
        return self.stop.is_set() or self.device.stop.is_set() or time.monotonic() > self.lease_until

    def _pick(self):
        """Image suivante à envoyer : la plus récente de la capture, jamais deux fois la même, à la cadence du profil."""
        with self.lock:
            now = time.monotonic()
            if now < self.next_pick:
                return None
            frame, stamp = self.device.source_frame_value()
            if not frame or stamp <= self.taken or time.time() - stamp > 8:
                return None
            self.taken = stamp
            self.seq += 1
            period = 1.0 / self.profile["fps"]
            self.next_pick = max(self.next_pick + period, now)   # en retard : pas de rattrapage en rafale
            return frame, self.seq, dict(self.profile), self.quality, self.width

    def _record(self, started, finished):
        with self.lock:
            self.sent.append(finished)
            self.busy.append((finished, finished - started))
            self.send_ms = (finished - started) * 1000 if self.send_ms is None else self.send_ms * 0.8 + (finished - started) * 200
            horizon = finished - SITE_LIVE_WINDOW_S
            self.sent = [stamp for stamp in self.sent if stamp >= horizon]
            self.busy = [item for item in self.busy if item[0] >= horizon]
            if finished - self.adapted < SITE_LIVE_WINDOW_S:
                return
            self.adapted = finished
            achieved = len(self.sent) / SITE_LIVE_WINDOW_S
            occupied = sum(duration for _stamp, duration in self.busy) / (SITE_LIVE_WINDOW_S * max(1, len(self.threads)))
            target = self.profile["fps"]
            if occupied > 0.85 and achieved < 0.8 * target:
                # Le lien montant (ou l'encodage) ne suit pas : images plus légères, jamais de file d'attente.
                self.calm = 0
                if self.quality > SITE_LIVE_MIN_QUALITY:
                    self.quality = max(SITE_LIVE_MIN_QUALITY, self.quality - 10)
                else:
                    current = self.width or self.source_width or 1280
                    if current > SITE_LIVE_MIN_WIDTH:
                        self.width = max(SITE_LIVE_MIN_WIDTH, int(current * 0.8) // 16 * 16)
            elif occupied < 0.5:
                self.calm += 1
                if self.calm >= 5:   # dix secondes de marge : un cran vers le profil demandé
                    self.calm = 0
                    wanted = self.profile["width"]
                    if self.width and (not wanted or self.width < wanted):
                        grown = int(self.width / 0.8) // 16 * 16
                        self.width = wanted if (wanted and grown >= wanted) else (0 if not wanted and grown >= 3840 else grown)
                    elif self.quality < self.profile["quality"]:
                        self.quality = min(self.profile["quality"], self.quality + 10)
            else:
                self.calm = 0

    def stats(self):
        with self.lock:
            return {"fps": round(len(self.sent) / SITE_LIVE_WINDOW_S, 1), "quality": self.quality, "width": self.width,
                    "send_ms": round(self.send_ms, 1) if self.send_ms is not None else None, "error": self.error}

    def _lane(self):
        connection, failures = None, 0
        try:
            while not self.stopping():
                picked = self._pick()
                if picked is None:
                    self.stop.wait(SITE_LIVE_WAIT_S)
                    continue
                frame, seq, profile, quality, width = picked
                started = time.monotonic()
                try:
                    rendered = site_live_render(frame, width, quality, profile["max_bytes"])
                except ImportError:
                    self.error = "Pillow absent sur ce Worker : clique Installer sur la fiche de la caméra."
                    self.stop.set()
                    break
                except Exception:  # noqa: BLE001 - image illisible : la suivante sera essayée
                    continue
                if rendered is None:
                    continue
                body, size, source = rendered
                self.source_width = source[0]
                capture = self.device.webcam if self.device.webcam is not None else self.device
                info = {"width": size[0], "height": size[1], "quality": quality, "source_width": source[0], "source_height": source[1]}
                if getattr(capture, "capture_fps", None):
                    info["capture_fps"] = round(float(capture.capture_fps), 1)
                if self.send_ms is not None:
                    info["send_ms"] = round(self.send_ms, 1)
                try:
                    if connection is None:
                        connection = self.connect()
                    status, answer = connection.post(self.path, body, {"X-Live-Ticket": profile["ticket"], "X-Stream-Session": self.session,
                                                                      "X-Frame-Seq": str(seq), "X-Live-Info": json.dumps(info, separators=(",", ":"))})
                except Exception:  # noqa: BLE001 - réseau : nouvelle connexion, attente bornée, image suivante
                    failures += 1
                    if connection is not None:
                        connection.close()
                    connection = None
                    self.stop.wait(min(2.0, 0.25 * 2 ** min(failures, 3)))
                    continue
                if status == 200:
                    failures = 0
                    self._record(started, time.monotonic())
                    if answer.get("viewers") == 0:
                        self.stop.set()      # plus personne ne regarde : arrêt immédiat
                elif status in {401, 403, 404, 410}:
                    self.stop.set()          # ticket expiré ou caméra retirée : le prochain tour de boucle décidera
                elif status == 429:
                    self.stop.wait(0.5)
                else:
                    failures += 1
                    self.stop.wait(min(2.0, 0.25 * 2 ** min(failures, 3)))
        finally:
            if connection is not None:
                connection.close()


class SiteLive:
    """Flux vers le site des caméras regardées. Inactif tant que le site n'envoie pas « camera_live »."""

    def __init__(self, agent, connect=None):
        self.agent = agent
        self.lock = threading.Lock()
        self.streams = {}
        self._connect = connect

    def connect(self):
        if self._connect is not None:
            return self._connect()
        return SiteLiveConnection(self.agent.server, worker_site_headers(self.agent)["User-Agent"])

    def update(self, live, devices):
        """Applique la demande du site (``camera_live`` de la réponse de la boucle, None si absente)."""
        wanted, route, lease = {}, "", 10.0
        if isinstance(live, dict) and isinstance(live.get("cameras"), dict) and isinstance(live.get("route"), str) \
                and SITE_LIVE_ROUTE.fullmatch(live["route"]):
            route = live["route"]
            try:
                lease = min(30.0, max(3.0, float(live.get("lease") or 10.0)))
            except (TypeError, ValueError):
                lease = 10.0
            for identifier, entry in list(live["cameras"].items())[:SITE_LIVE_MAX_STREAMS]:
                device, profile = devices.get(str(identifier)), site_live_profile(entry)
                if (device is None or profile is None or device.spec.get("kind") != "camera" or not device.spec.get("enabled")
                        or device.spec.get("revision") != profile["revision"] or device.stop.is_set()):
                    continue
                wanted[str(identifier)] = (device, profile)
        with self.lock:
            for identifier in list(self.streams):
                stream = self.streams[identifier]
                if identifier not in wanted or stream.device is not wanted[identifier][0] or not stream.alive():
                    stream.close()
                    del self.streams[identifier]
            for identifier, (device, profile) in wanted.items():
                stream = self.streams.get(identifier)
                if stream is not None:
                    stream.renew(profile, lease)
                    continue
                stream = self.streams[identifier] = SiteLiveStream(device, profile, route, lease, self.connect)
                stream.start()

    def stats(self, identifier):
        with self.lock:
            stream = self.streams.get(str(identifier))
        return stream.stats() if stream is not None else None

    def close(self):
        with self.lock:
            streams, self.streams = list(self.streams.values()), {}
        for stream in streams:
            stream.close()


class EquipmentRelay:
    def __init__(self, agent):
        self.agent = agent
        self.devices = {}
        self.lock = threading.RLock()
        self.install_lock = threading.Lock()
        self.testing = set()
        self.video = None
        self.video_sources = {}
        self.video_options = {}   # identifiant -> options (fps, width, height) passées au relais
        self.video_version = None  # version du composant relais chargé en mémoire
        self.relay_intent = {}
        self.config_fetched_at = None   # dernière liste d'équipements lue sur le site (Salle de contrôle de worker.exe)
        self.site_live = SiteLive(agent)   # vue fluide du site : inactif sans « camera_live » dans la réponse du site
        # Surveillance incendie (agent 1.34.0) : garde de départ posée par l'agent (start_refusal / is_latched),
        # et état des arrêts par machine (arrêt en cours, dernier arrêt confirmé) pour les rendre idempotents.
        self.fire_guard = None
        self._fire_lock = threading.Lock()
        self._fire_state = {}
        self.state_path = self.agent.root / "components" / "printer-link" / "relay-state.json"
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.relay_intent = {str(key): value is True for key, value in state.items()} if isinstance(state, dict) else {}
        except (OSError, ValueError):
            pass

    def _save_intent(self, identifier, enabled):
        self.relay_intent[identifier] = bool(enabled)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.relay_intent), encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _spec(self, payload):
        identifier = str(payload.get("equipment_id") or "")
        spec = next((item for item in self.agent.request("/api/worker-protocol/equipment").get("equipment", []) if item.get("id") == identifier), None)
        if not spec or spec.get("revision") != payload.get("revision"):
            raise ValueError("Équipement supprimé, réaffecté ou reconfiguré ; opération annulée.")
        validate_connector(spec)
        return spec

    def _component(self):
        return self.agent.root / "components" / "printer-link" / "equipment_video_relay.py"

    def _hub(self):
        if self.video is None:
            component = self._component()
            if not component.is_file():
                raise ValueError("Installe d’abord le relais vidéo sur ce Worker.")
            # Composant posé par une version plus ancienne : recopié avant le chargement, pour que le jeton de lecture
            # (composant 1.3.0, agent 1.50.20) protège le relais dès le redémarrage qui suit la mise à jour.
            try:
                self._refresh_component()
            except OSError:
                pass
            spec = importlib.util.spec_from_file_location("alpine_installed_video_relay", component)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            # 1.3.0 : jetons dans runtime/camera-relay (ACL de l'installation) ; un composant plus ancien n'en a pas.
            self.video = module.VideoRelay(module.token_directory(self.agent.root)) if hasattr(module, "token_directory") else module.VideoRelay()
            self.video_version = version_tuple(getattr(module, "COMPONENT_VERSION", "1.0.0"))
        return self.video

    def _relay_wanted(self, spec):
        return bool(spec.get("enabled")) and relay_configured(spec)

    def _relay_outdated_message(self):
        """Message quand le composant chargé ne connaît pas la cadence/taille ; vide s'il est à jour."""
        if self.video is None or (self.video_version or (0,)) >= RELAY_OPTIONS_MIN_VERSION:
            return ""
        return RELAY_RESTART_MESSAGE if component_file_version(self._component()) >= RELAY_OPTIONS_MIN_VERSION else RELAY_OUTDATED_MESSAGE

    def _refresh_component(self):
        """Recopie le composant relais du paquet dans components/printer-link/ s'il y est absent ou plus ancien."""
        destination = self._component()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file() or component_file_version(destination) < version_tuple(RELAY_COMPONENT_VERSION):
            shutil.copy2(Path(__file__).with_name("equipment_video_relay.py"), destination)
            return True
        return False

    def _remove_source(self, identifier):
        previous = self.video_sources.pop(identifier, None)
        self.video_options.pop(identifier, None)
        if previous and self.video:
            self.video.remove(*previous)

    def _start_source(self, device):
        cfg = device.spec["settings"]
        source = cfg.get("source_id") or "cam_" + device.spec["id"].replace("-", "")
        pair = (port_number(cfg.get("relay_port"), 1984, 1024), source)
        options = relay_options(cfg)
        previous = self.video_sources.get(device.spec["id"])
        if previous and previous != pair:
            self._remove_source(device.spec["id"])
            previous = None
        if previous == pair and self.video_options.get(device.spec["id"]) == options:
            url = (self.video.url(*pair) if hasattr(self.video, "url") else "") or f"http://127.0.0.1:{pair[0]}/api/stream.mjpeg?src={pair[1]}"
        else:
            hub = self._hub()
            if (self.video_version or (0,)) >= RELAY_OPTIONS_MIN_VERSION:
                url = hub.add(*pair, device.source_frame_value, **options)
            else:
                url = hub.add(*pair, device.source_frame_value)  # composant 1.0.0 : ni cadence ni taille
        self.video_sources[device.spec["id"]] = pair
        self.video_options[device.spec["id"]] = options
        device.relay_state.update(relay_running=True, relay_url=url, relay_message=self._relay_outdated_message() or "Relais démarré — attente de validation du flux vidéo.")
        self._sync_printguard_url(url)
        device.start()

    def _sync_printguard_url(self, url):
        """PrintGuard configuré avant le jeton (agent 1.50.20) : son adresse de la même source reçoit le jeton actuel.

        Seules les caméras de data/printguard/state.json dont l'adresse vise ce port et cette source sont réécrites
        (et le repère alpine_camera.json) ; PrintGuard relit le fichier à son prochain démarrage."""
        try:
            target = urlsplit(url)
            source = parse_qs(target.query).get("src", [""])[0]
            if "token=" not in target.query or not source:
                return
            folder = Path(self.agent.root) / "data" / "printguard"
            for name, read, write in (("state.json", lambda data: [item.get("source") for item in data.get("cameras", []) if isinstance(item, dict)], "url"),
                                      ("alpine_camera.json", lambda data: [data], "stream_url")):
                path = folder / name
                if not path.is_file():
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                changed = False
                for holder in read(data):
                    if not isinstance(holder, dict) or not isinstance(holder.get(write), str):
                        continue
                    current = urlsplit(holder[write])
                    if (current.hostname in {"127.0.0.1", "localhost"} and current.port == target.port and current.path in {"/api/stream.mjpeg", "/api/frame.jpeg"}
                            and parse_qs(current.query).get("src") == [source] and holder[write] != url):
                        holder[write] = url
                        changed = True
                if changed:
                    temporary = path.with_name(path.name + ".tmp")
                    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(temporary, path)
        except (OSError, ValueError, TypeError):
            pass  # PrintGuard illisible : sa source se reconfigure depuis le site (Surveillance IA → caméra)

    def _probe_source(self, device):
        pair = self.video_sources.get(device.spec["id"])
        if not pair:
            raise ValueError("Relais vidéo arrêté.")
        url = (self.video.url(*pair, frame=True) if self.video is not None and hasattr(self.video, "url") else "") \
            or f"http://127.0.0.1:{pair[0]}/api/frame.jpeg?src={pair[1]}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(url, timeout=3) as response:
            if response.headers.get_content_type() != "image/jpeg":
                raise ValueError("Réponse vidéo invalide")
            frame = response.read(CAMERA_MAX_FRAME_BYTES+1)
        device.validate_frame(frame)
        resize_error = getattr(self.video, "resize_errors", {}).get(pair) if self.video else ""
        device.relay_state.update(relay_verified=True, relay_message=self._relay_outdated_message() or resize_error or "Relais MJPEG actif — image vérifiée sur ce Worker.")

    def install(self, payload, command_id):
        with self.install_lock:
            spec = self._spec(payload)
            if spec["settings"].get("connector") in HTTP_PRINTER_PORTS:
                # Ces connecteurs n'utilisent que la bibliothèque standard du
                # Worker : aucune roue pip, donc aucun redémarrage à imposer.
                message = "Aucune dépendance supplémentaire : ce connecteur HTTP utilise la bibliothèque standard du Worker. Teste la connexion."
                self.agent.command_progress(command_id, 100, message)
                return {"installed": True, "message": message}
            if spec["settings"].get("connector") in GRBL_CONNECTORS:
                # TCP : bibliothèque standard. Série : pyserial, vérifié sans rien télécharger.
                if spec["settings"].get("connector") == "grbl_serial":
                    target = dependency_directory(self.agent.root)
                    if str(target) not in sys.path:
                        sys.path.insert(0, str(target))
                    if not package_installed("serial", "serialutil.py"):
                        # pyserial seul, par l'installateur durable (pip --target dans le dossier de
                        # données du Worker), aucun GPU ; un dossier serial/ vide ou partiel est purgé avant.
                        if purge_partial_package(target, "serial"):
                            self.agent.command_progress(command_id, 5, "Dossier pyserial incomplet retiré (copie interrompue) ; réinstallation…")
                        self.agent.command_progress(command_id, 10, "pyserial absent : installation de pyserial 3.5 seul dans le dossier de dépendances du Worker…")
                        self.agent.run_installer([sys.executable, str(Path(__file__).with_name("equipment_video_relay.py")), "--install-profile", str(target), "--profile", "serial"], os.environ.copy(), command_id)
                        if not package_installed("serial", "serialutil.py"):
                            raise ValueError("pyserial reste introuvable ou incomplet après l’installation : vérifie les droits du dossier Worker et l’accès réseau à PyPI, puis relance Installer.")
                        message = "pyserial installé : la liaison série GRBL est utilisable. Teste la connexion (reset et lecture d’état, aucun mouvement)."
                    else:
                        message = "pyserial présent : la liaison série GRBL est utilisable. Teste la connexion (reset et lecture d’état, aucun mouvement)."
                else:
                    message = "Aucune dépendance supplémentaire : la liaison TCP GRBL utilise la bibliothèque standard du Worker. Teste la connexion."
                self.agent.command_progress(command_id, 100, message)
                return {"installed": True, "message": message}
            if spec["settings"].get("connector") == USB_CAMERA_CONNECTOR or webcam_link(spec["settings"]):
                # Caméra USB (et webcam par lien MJPEG local, même capture) : OpenCV seul (profil ciblé), les dépendances JPEG du relais si elles
                # manquent encore, et le composant relais loopback pour PrintGuard. Rien de recopié
                # par-dessus un module déjà à jour.
                target = dependency_directory(self.agent.root)
                if str(target) not in sys.path:
                    sys.path.insert(0, str(target))
                steps = []
                # Agent 1.50.5 : Media Foundation (WinRT, Windows) fait partie du profil ; un Worker qui avait déjà
                # OpenCV repasse par l'installation tant que winrt manque (incident de l'atelier, 12/10).
                winrt_missing = os.name == "nt" and not winrt_installed(target)
                if not package_installed("cv2", "__init__.py") or winrt_missing:
                    if purge_partial_package(target, "cv2"):
                        self.agent.command_progress(command_id, 5, "Dossier OpenCV incomplet retiré (copie interrompue) ; réinstallation…")
                    self.agent.command_progress(command_id, 10, "Installation de la capture vidéo USB (OpenCV sans GPU" + (" et Media Foundation / WinRT" if os.name == "nt" else "") + ")…")
                    self.agent.run_installer([sys.executable, str(Path(__file__).with_name("equipment_video_relay.py")), "--install-profile", str(target), "--profile", "usb_camera"], os.environ.copy(), command_id)
                    if not package_installed("cv2", "__init__.py"):
                        raise ValueError("OpenCV reste introuvable ou incomplet après l’installation : vérifie les droits du dossier Worker et l’accès réseau à PyPI, puis relance Installer.")
                    if os.name == "nt" and not winrt_installed(target):
                        raise ValueError("Media Foundation (paquets winrt) reste introuvable après l’installation : vérifie l’accès réseau à PyPI, puis relance Installer.")
                    steps.append("OpenCV installé" if winrt_missing is False else "OpenCV et Media Foundation installés")
                else:
                    steps.append("OpenCV et Media Foundation présents" if os.name == "nt" else "OpenCV présent")
                if not dependency_health(target)["ok"]:
                    self.agent.command_progress(command_id, 50, "Dépendances JPEG du Worker absentes : installation des dépendances épinglées du relais…")
                    self.agent.run_installer([sys.executable, str(Path(__file__).with_name("equipment_video_relay.py")), "--install-dependencies", str(target)], os.environ.copy(), command_id)
                    if not dependency_health(target)["ok"]:
                        raise ValueError("Dépendances JPEG non utilisables par le Python du Worker. Installation non validée ; réessaie Installer.")
                    steps.append("dépendances JPEG installées")
                self._refresh_component()  # relais loopback (PrintGuard, lien MJPEG local)
                message = ", ".join(steps) + " : la caméra USB est utilisable depuis ce Worker. Connecte-la, puis vérifie l’image."
                if self._relay_outdated_message():
                    message += " " + self._relay_outdated_message()
                self.agent.command_progress(command_id, 100, message)
                return {"installed": True, "message": message}
            destination = self._component()
            target = dependency_directory(self.agent.root)
            self.agent.command_progress(command_id, 5, "Vérification du Worker et du connecteur choisi…")
            # Dependencies and the video component go only into the Worker data
            # root; package code is already downloaded by the Worker updater.
            repaired = not dependency_health(target)["ok"]
            if repaired:
                self.agent.run_installer([sys.executable, str(Path(__file__).with_name("equipment_video_relay.py")), "--install-dependencies", str(target)], os.environ.copy(), command_id)
            if not dependency_health(target)["ok"]:
                raise ValueError("Dépendances du relais non utilisables par le Python du Worker. Installation non validée ; vérifie les droits du dossier Worker et réessaie Installer.")
            self.agent.command_progress(command_id, 70, "Installation du composant MJPEG inclus dans le paquet Worker…")
            self._refresh_component()
            if str(target) not in sys.path:
                sys.path.insert(0, str(target))
            importlib.invalidate_caches()
            self._spec(payload)  # Credentials/ownership may have changed during install.
            self.agent.command_progress(command_id, 85, "Composants vérifiés ; configuration propre à cet équipement…")
            result = {"installed": True, "message": "Lecteur réseau et relais vidéo installés sur le Worker. Aucune IA/GPU nécessaire."}
            if repaired:
                # Compiled extensions already loaded cannot safely be replaced
                # in place. Explicit activation also avoids interrupting jobs.
                result.update(restart_required=True, message="Dépendances du relais installées et vérifiées avec le Python du Worker. Redémarre le Worker pour les activer, puis teste la connexion.")
                self.agent.command_progress(command_id, 100, result["message"])
                return result
            if spec["settings"].get("camera_mode") != "relay":
                with self.lock:
                    device = self.devices.get(spec["id"])
                    if device and device.spec == spec and spec.get("enabled") and not device.stop.is_set():
                        device.start()  # A prior missing dependency may have ended the reader.
            # Installation must not change a stopped relay's persistent intent
            # or become a failed installation merely because the P1S is offline.
            # sync() continues any relay that was explicitly requested before.
            result["message"] += " La connexion de l’équipement se vérifie séparément avec Tester ou Démarrer le relais."
            if self._relay_outdated_message():
                result["message"] += " " + self._relay_outdated_message()
            self.agent.command_progress(command_id, 100, result["message"])
            return result

    def start_relay(self, payload, command_id):
        spec = self._spec(payload)
        if not relay_configured(spec):
            raise ValueError("Démarrage MJPEG réservé aux caméras dont le relais local est activé (ou au mode relais P1S).")
        if not spec.get("enabled"):
            raise ValueError("Connecte d’abord cette caméra sur le site.")
        self.agent.command_progress(command_id, 85, "Démarrage du relais vidéo sur l’interface locale du Worker…")
        with self.lock:
            device = self.devices.get(spec["id"])
            if device and device.spec != spec:
                self._remove_source(spec["id"])
                device.close()
                device = None
            if device is None or device.stop.is_set():
                device = self.devices[spec["id"]] = Device(spec, self.agent.root)
            self._save_intent(spec["id"], True)
            try:
                self._start_source(device)
            except Exception:
                self._save_intent(spec["id"], False)
                raise ValueError("Relais indisponible : installe le composant ou choisis un port libre sur le Worker.") from None
        self._wait_connected(device, command_id, relay=True)
        return {"relay_running": True, "relay_verified": True, "relay_url": device.relay_state["relay_url"], "message": "Relais MJPEG démarré et image décodée avec succès sur le Worker."}

    def _printguard_camera_id(self):
        marker_path = Path(self.agent.root) / "data" / "printguard" / "alpine_camera.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.is_file() else {}
        except (OSError, ValueError):
            return ""
        return str(marker.get("equipment_id") or "") if isinstance(marker, dict) else ""

    def loopback_stream(self, spec, command_id):
        """MJPEG URL PrintGuard can read on this Worker for one of its cameras.

        External MJPEG cameras are used as configured; relay-mode P1S cameras
        reuse the persisted relay; native P1S cameras get the same loopback
        relay fed by the frames the Worker already decodes (nothing is bound
        outside 127.0.0.1 and no intent is persisted for them)."""
        settings = spec.get("settings") or {}
        if settings.get("connector") == "mjpeg_http":
            url = str(settings.get("stream_url") or "")
            if not url:
                raise ValueError("Cette caméra n’a pas d’URL MJPEG configurée.")
            # Lien du relais loopback de ce Worker sans jeton (webcam par lien, lien collé avant 1.50.20) : jeton ajouté.
            return relay_with_token(url, relay_token_directory(self.agent.root))
        if settings.get("camera_mode") == "relay":
            return self.start_relay({"equipment_id": spec["id"], "revision": spec.get("revision")}, command_id)["relay_url"]
        with self.lock:
            device = self.devices.get(spec["id"])
            if device is None or device.stop.is_set():
                device = self.devices[spec["id"]] = Device(spec, self.agent.root)
                device.start()
            self._start_source(device)
        self._wait_connected(device, command_id, relay=True)
        return device.relay_state["relay_url"]

    def stop_relay(self, payload, command_id):
        spec = self._spec(payload)
        if not relay_configured(spec):
            raise ValueError("Cet équipement n’utilise pas le relais vidéo.")
        self.agent.command_progress(command_id, 30, "Arrêt du flux MJPEG et fermeture de la source caméra…")
        with self.lock:
            self._save_intent(spec["id"], False)
            self._remove_source(spec["id"])
            device = self.devices.get(spec["id"])
            if device:
                device.close()
                device.frame = None
                device.relay_state.update(relay_running=False, relay_verified=False, relay_message="Relais arrêté sur demande.")
        self.agent.command_progress(command_id, 100, "Relais arrêté ; il ne redémarrera pas automatiquement.")
        return {"relay_running": False, "message": "Relais arrêté sur demande."}

    def _wait_connected(self, device, command_id, relay=False):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and not self.agent.stop_requested.is_set():
            if device.stop.is_set():
                raise ValueError("Connexion annulée ou équipement déconnecté.")
            if relay and device.frame and time.time() - device.frame_at < 8:
                self._probe_source(device)
            report = device.report()
            if report.get("connected"):
                self.agent.command_progress(command_id, 100, "Image JPEG validée." if device.spec["kind"] == "camera" else "Bannière GRBL reçue et état lu." if device.spec["kind"] in GRBL_KINDS else "Authentification et télémétrie validées.")
                return report
            self.agent.command_progress(command_id, 90 if relay else max(10, min(90, report.get("progress", 10))), report.get("message") or "Test de connexion…")
            self.agent.stop_requested.wait(.5)
        detail = str(device.report().get("message") or "Aucun retour de l’équipement.")
        raise ValueError("Test non validé. " + detail + " Vérifie l’IP, le port et le code d’accès sur le réseau du Worker.")

    def test(self, payload, command_id):
        spec = self._spec(payload)
        self.agent.command_progress(command_id, 5, "Test réseau/authentification depuis le Worker sélectionné, sans mouvement ni impression…")
        with self.lock:
            if spec["id"] in self.testing:
                raise ValueError("Un test de cet équipement est déjà en cours.")
            existing = self.devices.get(spec["id"])
            if existing and existing.spec == spec and existing.spec.get("enabled") and not existing.stop.is_set():
                device, temporary = existing, False
                device.start()
            else:
                # A test is bounded and never changes the persisted desired state.
                if existing and existing.thread and existing.thread.is_alive():
                    raise ValueError("Reconfiguration en cours ; attends la synchronisation du Worker.")
                device = Device(dict(spec, enabled=True), self.agent.root)
                temporary = True
                if spec["settings"].get("camera_mode") == "relay":
                    raise ValueError("Démarre le relais pour tester son flux MJPEG ; le lecteur natif peut être testé en mode natif.")
                self.testing.add(spec["id"])
                self.devices[spec["id"]] = device
                device.start()
        try:
            if temporary and device.webcam_link:
                # Webcam par lien : l'essai porte sur toute la chaîne (capture, relais, lecture du lien) ; le relais
                # ouvert pour l'essai est retiré ensuite, sans toucher à l'intention enregistrée.
                with self.lock:
                    self._start_source(device)
            report = self._wait_connected(device, command_id, relay=spec["settings"].get("camera_mode") == "relay")
            self._spec(payload)
            return {"tested": True, "connected": True, "message": "Connexion validée sur le Worker (test en lecture seule).", "camera_frame_valid": spec["kind"] == "camera"}
        finally:
            if temporary:
                with self.lock:
                    if device.webcam_link:
                        self._remove_source(spec["id"])
                    device.close()
                    self.testing.discard(spec["id"])
                    if self.devices.get(spec["id"]) is device:
                        self.devices.pop(spec["id"])

    def command(self, payload, command_id):
        identifier = str(payload.get("equipment_id") or "")
        spec = next((item for item in self.agent.request("/api/worker-protocol/equipment").get("equipment", []) if item.get("id") == identifier), None)
        if not spec or not spec.get("enabled") or spec.get("revision") != payload.get("revision"):
            raise ValueError("Équipement déconnecté, supprimé ou reconfiguré ; commande annulée.")
        device = self.devices.get(identifier)
        if spec["kind"] in GRBL_KINDS:
            return self._grbl_command(device, spec, payload, command_id)
        # Un transport prêt reste exigé : MQTT pour Bambu, un pilote HTTP sinon.
        if not device or not (device.client or device.driver) or not device.report().get("connected"):
            raise ValueError("La connexion imprimante n’est pas encore prête sur le Worker.")
        wire = payload.get("wire")
        if not isinstance(wire, dict) or set(wire) - {"print", "pushing", "system"} or len(json.dumps(wire)) > 16384:
            raise ValueError("Commande imprimante invalide.")
        print_request = wire.get("print", {})
        # Keep temperature/fan controls available while printing, but never jog
        # or home a machine that is already executing a print.
        moving = print_request.get("command") == "gcode_line" and any(
            re.match(r"^\s*G(?:0?0|0?1|0?2|0?3|28|29|90|91|92)(?:\s|[XYZEF]|$)", line, re.I)
            for line in str(print_request.get("param", "")).splitlines())
        if device.telemetry.get("gcode_state") in {"RUNNING", "PREPARE", "PAUSE"} and (print_request.get("command") in {"project_file", "xyz_ctrl", "home"} or moving):
            raise ValueError("Une impression est déjà en cours ; lancement/déplacement refusé.")
        transfer = payload.get("transfer_id")
        uploaded = None
        if transfer:
            uuid.UUID(str(transfer))
            if spec["kind"] != "printer" or wire.get("print", {}).get("command") != "project_file":
                raise ValueError("Transfert réservé à une imprimante locale.")
            # La P1S imprime un fichier réseau depuis sa carte microSD : refus clair si elle dit ne pas en avoir.
            if not device.driver and device.telemetry.get("sdcard") is False:
                raise ValueError("Aucune carte microSD détectée dans l’imprimante : insère-la (elle est nécessaire pour imprimer un fichier envoyé depuis le site), puis relance.")
            folder = self.agent.root / "equipment-transfers"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (str(transfer)+".gcode.3mf")
            url = self.agent.server+"/api/worker-protocol/equipment/transfers/"+str(transfer)
            headers = worker_site_headers(self.agent)
            self.agent.command_progress(command_id, 10, "Téléchargement du fichier tranché sur le Worker…")
            try:
                with urllib.request.urlopen(protect_credentials(urllib.request.Request(url, headers=headers)), timeout=45) as response, path.open("wb") as output:
                    length = int(response.headers.get("Content-Length") or 0)
                    total = 0
                    while True:
                        chunk = response.read(1024*1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > 2*1024**3:
                            raise ValueError("Fichier trop volumineux")
                        output.write(chunk)
                        self.agent.command_progress(command_id, min(45, 10+35*total/max(1,length)), "Transfert du fichier tranché vers le Worker…")
                if device.driver:
                    # Même téléchargement authentifié ; l'envoi passe par l'API
                    # HTTP locale de la machine au lieu du FTPS propre à Bambu.
                    self.agent.command_progress(command_id, 50, "Envoi du fichier à l’imprimante via son API locale…")
                    uploaded = device.driver.upload(path, lambda sent: self.agent.command_progress(command_id, 50+40*sent/max(1, total), "Envoi du fichier vers l’imprimante…"))
                else:
                    context = ssl.create_default_context()
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                    ftp = PrinterFTP(context=context, timeout=35)
                    try:
                        cfg = spec["settings"]
                        self.agent.command_progress(command_id, 50, "Connexion FTPS locale à l’imprimante…")
                        ftp.connect(local_host(cfg["host"]), port_number(cfg.get("transfer_port"), 990))
                        ftp.login("bblp", cfg["access_code"])
                        ftp.prot_p()
                        # /cache comme Bambu Studio ; racine de la carte si ce dossier n'existe pas.
                        try:
                            ftp.cwd("/cache")
                            remote_folder = "/cache/"
                        except ftplib.error_perm:
                            ftp.cwd("/")
                            remote_folder = "/"
                        sent = 0
                        def progress(chunk):
                            nonlocal sent
                            sent += len(chunk)
                            self.agent.command_progress(command_id, 50+40*sent/max(1,total), "Envoi vers la carte SD de l’imprimante…")
                        with path.open("rb") as source:
                            ftp.storbinary("STOR "+path.name, source, blocksize=256*1024, callback=progress)
                    finally:
                        ftp.close()
                    wire["print"]["url"] = "ftp://"+remote_folder+path.name
            finally:
                path.unlink(missing_ok=True)
        self.agent.command_progress(command_id, 95, "Envoi de la commande à l’imprimante via son API HTTP locale…" if device.driver else "Envoi de la commande à l’imprimante via MQTT TLS…")
        latest = next((entry for entry in self.agent.request("/api/worker-protocol/equipment").get("equipment", []) if entry.get("id") == identifier), None)
        if not latest or not latest.get("enabled") or latest.get("revision") != spec["revision"] or device.stop.is_set():
            raise ValueError("Équipement déconnecté ou reconfiguré pendant la préparation ; commande non envoyée.")
        return self._envoyer(device, spec, wire, uploaded)

    def _envoyer(self, device, spec, wire, uploaded=None):
        """Publier une commande déjà validée et attendre ce que la machine confirme."""
        if device.driver:
            # Les accusés propres à Bambu (signature, sequence_id, ack MQTT) n'ont
            # pas d'équivalent ici : chaque pilote rapporte ce qu'il peut prouver.
            return device.driver.send(wire, uploaded=uploaded)
        for part in wire.values():
            if isinstance(part, dict):
                part["sequence_id"] = str(time.time_ns() % 2147483647)
        if isinstance(wire.get("print"), dict):
            device.signer.prepare(device.client, spec["settings"]["serial"])
        info = device.client.publish("device/"+spec["settings"]["serial"]+"/request", device.signer.encode(wire), qos=0)
        if info.rc != 0:
            raise ValueError("La connexion MQTT n’a pas accepté l’envoi.")
        print_command = wire.get("print", {})
        if print_command.get("command"):
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                with device.lock:
                    ack = device.acks.pop(print_command["sequence_id"], None)
                if ack:
                    if ack["command"] != print_command["command"] or ack["result"] not in {"", "success"}:
                        raise ValueError("L’imprimante a refusé la commande ; vérifie ses autorisations et son état.")
                    return {"sent": True, "device_confirmed": True, "message": "Commande confirmée par l’imprimante."}
                time.sleep(.2)
        return {"sent": True, "device_confirmed": False, "message": "Commande transmise ; vérifie l’état réel de l’imprimante. Aucun renvoi automatique."}

    # ------------------------------------------------------------ machines GRBL
    def _grbl_require_operator_checks(self, cfg, confirmed):
        required = [str(item.get("name") or "") for item in cfg.get("interlocks") or []
                    if isinstance(item, dict) and item.get("required") and item.get("kind") in GRBL_OPERATOR_INTERLOCKS]
        if required and not confirmed:
            raise ValueError("Confirme la liste de sécurité avant de lancer : " + ", ".join(required) + ".")

    def _grbl_feed(self, cfg, value, label, default=None):
        """Vitesse (mm/min) bornée par le maximum configuré ; ``default`` sert quand la valeur est absente."""
        maximum = float(cfg.get("max_feed_mm_min") or 3000)
        if value in (None, ""):
            if default is None:
                raise ValueError(f"{label} absente.")
            return float(default)
        try:
            feed = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label} invalide.") from None
        if isinstance(value, bool) or not math.isfinite(feed) or not 0 < feed <= maximum:
            raise ValueError(f"{label} hors limites (0 < vitesse ≤ {maximum:g} mm/min, maximum configuré pour cette machine).")
        return feed

    def _grbl_ready_to_move(self, controller, allowed=("Idle",)):
        """État relu avant une commande manuelle : ni alarme (sauf si admise), ni porte ouverte, et un état attendu.

        Hors de ces états (Hold, Run, Jog, Home…), une ligne resterait dans le tampon de la carte et partirait
        au prochain « Reprendre » : refusée avant tout envoi. Les commandes ``$`` (réglages, informations,
        homing) admettent aussi l'état Alarm, comme GRBL.
        """
        status = controller.status(1.0) or {}
        state = status.get("state")
        if state == "Alarm" and "Alarm" not in allowed:
            raise ValueError("Machine en alarme : déverrouille ($X) ou lance un homing avant de bouger.")
        if state == "Door" or "D" in (status.get("pins") or []):
            raise ValueError("Porte de sécurité ouverte (état Door) : commande refusée.")
        if state not in allowed:
            raise ValueError(f"Machine non prête (état {state or 'inconnu'}) : reprends ou arrête la pause avant cette commande.")
        return status

    def _grbl_download(self, transfer, command_id):
        """G-code déposé par le site : même route authentifiée que les fichiers d'impression."""
        url = self.agent.server + "/api/worker-protocol/equipment/transfers/" + str(transfer)
        headers = worker_site_headers(self.agent)
        self.agent.command_progress(command_id, 10, "Téléchargement du G-code sur le Worker…")
        chunks, total = [], 0
        try:
            with urllib.request.urlopen(protect_credentials(urllib.request.Request(url, headers=headers)), timeout=45) as response:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > GRBL_GCODE_LIMIT:
                        raise ValueError("G-code trop volumineux (32 Mo au plus).")
                    chunks.append(chunk)
        except (OSError, socket.timeout) as error:
            # Un échec HTTP ici vient du téléchargement depuis le site, pas de la
            # liaison série/TCP vers la carte : un message générique amont l'aurait
            # à tort décrit comme « Liaison GRBL interrompue » (incident 27/09). Le
            # code HTTP (absent hors HTTPError) distingue un aléa passager (503/500,
            # nouvel essai utile) d'un vrai rejet (404 équipement désactivé/supprimé).
            detail = f"{type(error).__name__} {error.code}" if isinstance(error, urllib.error.HTTPError) else type(error).__name__
            raise ValueError(f"Téléchargement du G-code depuis le site impossible ({detail}).") from None
        return b"".join(chunks).decode("utf-8", "replace")

    def _grbl_gcode(self, request, payload, command_id):
        transfer = payload.get("transfer_id")
        if transfer:
            uuid.UUID(str(transfer))
            if "gcode" in request:
                raise ValueError("G-code fourni deux fois (commande et transfert).")
            return self._grbl_download(transfer, command_id)
        text = request.get("gcode")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("G-code absent de la commande.")
        return text

    def _grbl_progress(self, command_id):
        """Progression du job vers le site, postée par un fil à part (``LatestProgress``), jamais par l'envoi.

        Le rappel renvoyé expose ``close`` : l'appelant le ferme dès que la commande a répondu.
        """
        poster = LatestProgress(lambda percent, message: self.agent.command_progress(command_id, percent, message))

        def report(job):
            percent = float(job.get("percent") or 0)
            poster.post(min(99.0, percent), f"Envoi ligne {job.get('done', 0)}/{job.get('total', 0)} ({percent:.0f} %)…")
        report.close = poster.close
        return report

    def _grbl_write_batch(self, controller, request, result):
        """Paramètres du contrôleur (agent 1.50.17) : ``settings`` / ``write_batch``, revalidé ici en entier.

        Contrôles avant tout envoi : 1 à 64 entrées, numéro et valeur valides, numéro présent dans un ``$$`` relu
        à l'instant, ``expected`` (facultatif) égal à la valeur relue, critique acquitté, machine Idle ou Alarm
        (aucun job : déjà refusé par ``_grbl_command``). Puis écriture ligne à ligne avec arrêt au premier échec,
        relecture et comparaison par le contrôleur. Un échec partiel rend ses résultats (commande terminée, non
        confirmée) : le site les lit un à un au lieu d'un succès global.
        """
        entries = request.get("entries")
        if not isinstance(entries, list) or not 1 <= len(entries) <= GRBL_SETTINGS_BATCH_MAX:
            raise ValueError(f"Lot de réglages invalide (1 à {GRBL_SETTINGS_BATCH_MAX} entrées).")
        prepared = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Entrée de réglage invalide.")
            number = entry.get("number")
            if isinstance(number, bool) or not re.fullmatch(r"[0-9]{1,3}", str(number)):
                raise ValueError("Numéro de réglage GRBL invalide.")
            number = int(number)
            value = entry.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 1e7:
                raise ValueError(f"Valeur du réglage ${number} invalide.")
            expected = entry.get("expected")
            if expected is not None and (isinstance(expected, bool) or not isinstance(expected, (int, float)) or not math.isfinite(expected)):
                raise ValueError(f"Valeur attendue du réglage ${number} invalide.")
            if any(item["number"] == number for item in prepared):
                raise ValueError(f"Réglage ${number} présent deux fois dans le lot.")
            if grbl_setting_critical(number) and entry.get("critical_ack") is not True:
                raise ValueError(f"Réglage ${number} critique : acquitte-le explicitement avant l’écriture.")
            prepared.append({"number": number, "value": value, "expected": expected})
        self._grbl_ready_to_move(controller, allowed=("Idle", "Alarm"))  # $n=v : Idle ou Alarm, sinon error:8 ou ligne en attente
        current = controller.read_settings()
        for item in prepared:
            if item["number"] not in current:
                raise ValueError(f"Réglage ${item['number']} absent du $$ de cette carte : écriture refusée.")
            if item["expected"] is not None and abs(float(current[item['number']]) - float(item["expected"])) > GRBL_SETTINGS_TOLERANCE:
                raise ValueError(f"Réglage ${item['number']} : la carte relit {current[item['number']]:g}, pas {item['expected']:g} ; relis les réglages avant d’écrire.")
        outcome = controller.write_settings_batch([{"number": item["number"], "value": item["value"]} for item in prepared])
        results = outcome["results"]
        written = sum(1 for item in results if item["status"] == "ok")
        # Ligne qui a arrêté l'envoi d'abord (error:N, alarme, liaison coupée), sinon premier écart à la relecture.
        failed = next((item for item in results if item["number"] == outcome.get("stopped_at")), None) \
            or next((item for item in results if item["status"] in {"error", "mismatch"}), None)
        if failed is None:
            message = f"{written} réglage(s) écrit(s) et relu(s) sur la carte."
        else:
            message = (f"Écriture interrompue au réglage ${failed['number']} ({failed['message']}) : {written} écrit(s), "
                       f"{sum(1 for item in results if item['status'] == 'not_sent')} non envoyé(s).")
        return result(message, confirmed=failed is None, results=results, settings=outcome["settings"], stopped=outcome["stopped"],
                      readback_ok=outcome["readback_ok"])

    def _grbl_command(self, device, spec, payload, command_id):
        """Commande laser/CNC déjà validée par le site, revalidée ici avec les réglages de la machine."""
        grbl = grbl_module()
        controller = device.grbl if device is not None else None
        if controller is None or not device.report().get("connected"):
            raise ValueError("La liaison GRBL n’est pas encore prête sur le Worker.")
        wire = payload.get("wire")
        if not isinstance(wire, dict) or set(wire) != {"grbl"} or not isinstance(wire["grbl"], dict) or len(json.dumps(wire)) > GRBL_WIRE_LIMIT:
            raise ValueError("Commande GRBL invalide.")
        request = wire["grbl"]
        command = str(request.get("command") or "")
        if command not in GRBL_COMMANDS:
            raise ValueError("Commande GRBL inconnue.")
        cfg = spec["settings"]
        with device.lock:
            active = bool(device.grbl_job.get("active"))
        if active and command not in GRBL_LIVE_COMMANDS:
            raise ValueError("Un job GRBL est en cours : seules les commandes Pause, Reprendre, Arrêter, Arrêter le cadrage, État, "
                             "les overrides, l’annulation du jog et l’air assist sont acceptées.")
        try:
            return self._grbl_execute(grbl, device, controller, cfg, command, request, payload, command_id, active)
        except (OSError, socket.timeout) as error:
            raise ValueError(f"Liaison GRBL interrompue ({type(error).__name__}).") from None

    def _grbl_execute(self, grbl, device, controller, cfg, command, request, payload, command_id, active):
        def result(message, confirmed=True, **extra):
            return {"sent": True, "device_confirmed": confirmed, "message": message, "grbl": device.grbl_report(), **extra}

        if command == "status":
            status = controller.status(1.0) or {}
            return result(f"État GRBL : {status.get('state') or 'inconnu'}.")
        if command == "frame_stop":
            # Fin propre du cadrage en continu : plus aucune ligne envoyée, les segments déjà partis se
            # terminent, pas de reset (donc ni alarme ni perte de position, contrairement à ARRÊT).
            with device.lock:
                job = dict(device.grbl_job)
            if not job.get("active") or not job.get("loop"):
                return result("Aucun cadrage en continu en cours.", confirmed=False)
            device.grbl_loop_stop.set()
            self.agent.command_progress(command_id, 50, "Arrêt du cadrage : la machine termine le segment en cours…")
            if device.grbl_thread is not None:
                device.grbl_thread.join(timeout=GRBL_FRAME_STOP_WAIT)
            with device.lock:
                job = dict(device.grbl_job)
            if job.get("active"):
                raise ValueError(f"Le cadrage ne s’est pas arrêté en {GRBL_FRAME_STOP_WAIT:g} s : utilise ARRÊT si la machine bouge encore.")
            if job.get("ok") is False and job.get("error"):
                return result(f"Cadrage terminé après {job.get('passes') or 0} passe(s) : {job['error']}", confirmed=False)
            return result(f"Cadrage arrêté après {job.get('passes') or 0} passe(s) : la machine reste en position, laser coupé.")
        if command == "hold":
            controller.hold()
            status = controller.status(1.0) or {}
            return result("Pause demandée (feed hold) ; le laser est coupé pendant la pause.", status.get("state") == "Hold")
        if command == "resume":
            controller.resume()
            status = controller.status(1.0) or {}
            return result("Reprise demandée.", status.get("state") in {"Run", "Idle", "Jog"})
        if command == "stop":
            self.agent.command_progress(command_id, 50, "Arrêt de protection : pause, laser/broche coupés, reset…")
            controller.emergency_stop()
            if active and device.grbl_thread is not None:
                device.grbl_thread.join(timeout=10)
            with device.lock:
                job = dict(device.grbl_job)
            if job.get("active"):
                raise ValueError("Arrêt envoyé, mais le job ne s’est pas terminé dans les 10 s : vérifie la machine.")
            return result("Arrêt de protection exécuté : machine réinitialisée, laser et broche coupés." + (f" Job interrompu ({job.get('reason')})." if active else ""))
        if command == "reset":
            controller.reset()
            return result("Reset GRBL effectué ; la machine est peut-être verrouillée (déverrouille ou lance un homing).")
        if command == "unlock":
            controller.unlock()
            return result("Machine déverrouillée ($X) : position à vérifier, homing recommandé.")
        if command == "home":
            if not cfg.get("homing"):
                raise ValueError("Homing désactivé dans les réglages de cette machine : active-le après avoir vérifié les fins de course ($22).")
            self._grbl_ready_to_move(controller, allowed=("Idle", "Alarm"))
            self.agent.command_progress(command_id, 20, "Homing en cours ($H)…")
            controller.home(timeout=120.0)
            return result("Homing terminé.")
        if command == "set_origin":
            axes = str(request.get("axes") or "XYZ").upper()
            chosen = "".join(axis for axis in "XYZ" if axis in axes)
            if not chosen or any(char not in "XYZ" for char in axes):
                raise ValueError("Axes de l’origine invalides (X, Y, Z ou leur combinaison).")
            self._grbl_ready_to_move(controller)
            controller.set_work_origin(chosen)
            return result(f"Origine de travail définie au point courant (axes {chosen}).")
        # Gestion de la machine (agent 1.33.0) : chaque borne est revérifiée ici, même déjà contrôlée par le site.
        if command == "override":
            kind = str(request.get("kind") or "").lower()
            action = str(request.get("action") or "").lower()
            if action not in grbl.OVERRIDE_ACTIONS.get(kind, ()):
                raise ValueError("Override GRBL invalide (feed, rapid ou power ; action inconnue).")
            if kind == "power" and not active and action != "reset":
                # GRBL garde l'override de broche : hors travail il s'appliquerait au prochain test laser ou cadrage.
                raise ValueError("Override de puissance hors travail refusé : il ne s’applique qu’à un travail en cours ; "
                                 "seul le retour à 100 % est accepté.")
            controller.override(kind, action)
            controller.status(1.0)  # pendant un job : « ? » seulement, le rapport suivant porte le champ Ov:
            return result(f"Override {GRBL_OVERRIDE_LABELS[kind]} ({GRBL_OVERRIDE_ACTION_LABELS[action]}) envoyé ; la valeur effective suit dans le rapport.",
                          confirmed=False)
        if command == "jog_cancel":
            controller.cancel_jog()
            return result("Jog annulé.", confirmed=False)
        if command == "settings":
            action = str(request.get("action") or "").lower()
            if action == "read":
                settings = controller.read_settings()
                return result(f"{len(settings)} réglages GRBL relus ($$).", settings=settings)
            if action == "write_batch":
                return self._grbl_write_batch(controller, request, result)
            if action != "write":
                raise ValueError("Action de réglage invalide (read, write ou write_batch).")
            number = request.get("number")
            if isinstance(number, bool) or not re.fullmatch(r"[0-9]{1,3}", str(number)):
                raise ValueError("Numéro de réglage GRBL invalide.")
            number = int(number)
            if number not in GRBL_WRITABLE_SETTINGS:
                raise ValueError(f"Réglage ${number} protégé (pas par mm, inversions, fins de course, activation du homing) : "
                                 "il ne se modifie pas depuis le dashboard.")
            value = request.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"Valeur du réglage ${number} invalide.")
            self._grbl_ready_to_move(controller, allowed=("Idle", "Alarm"))  # $n=v : Idle ou Alarm, sinon error:8 ou ligne en attente
            settings = controller.write_setting(number, value)
            return result(f"Réglage ${number} mis à jour : {settings.get(number, value):g}.", settings=settings)
        if command == "info":
            self._grbl_ready_to_move(controller, allowed=("Idle", "Alarm"))
            return result("Informations lues.", **controller.info())
        if command in {"goto_origin", "goto_machine_zero"}:
            if command == "goto_machine_zero" and not cfg.get("homing"):
                raise ValueError("Zéro machine (G53) : active d’abord le référencement dans les réglages de cette machine.")
            self._grbl_ready_to_move(controller)
            if command == "goto_origin":
                controller.goto_work_origin()
                return result("Retour à l’origine de travail (X0 Y0) lancé.")
            controller.goto_machine_zero()
            return result("Retour au zéro machine (G53 X0 Y0) lancé.")
        if command == "move_abs":
            area = cfg.get("work_area_mm") or {}
            target = {}
            for axis in ("x", "y", "z"):
                raw = request.get(axis)
                if raw in (None, ""):
                    continue
                limit = area.get(axis)
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    raise ValueError(f"Position {axis.upper()} invalide.") from None
                if isinstance(raw, bool) or not math.isfinite(value) or not limit or abs(value) > float(limit):
                    raise ValueError(f"Position {axis.upper()} refusée : hors de la course configurée ({limit or 'non définie'} mm).")
                target[axis] = value
            if not target:
                raise ValueError("Indique au moins une coordonnée X, Y ou Z.")
            feed = self._grbl_feed(cfg, request.get("feed_mm_min"), "Vitesse de déplacement", default=min(1000.0, float(cfg.get("max_feed_mm_min") or 3000)))
            self._grbl_ready_to_move(controller, allowed=("Idle", "Jog"))
            controller.move_abs(feed=feed, **target)
            words = " ".join(f"{axis.upper()}{value:g}" for axis, value in target.items())
            return result(f"Déplacement absolu vers {words} à {feed:g} mm/min accepté par GRBL.")
        if command == "accessory":
            name = str(request.get("name") or "").lower()
            if name not in grbl.ACCESSORIES:
                raise ValueError("Accessoire inconnu (air ou mist).")
            on = request.get("on") is True
            label, off_word = ("Air assist", "coupé") if name == "air" else ("Brumisation", "coupée")
            if active:
                # Pendant un job : bascule temps réel, seulement si l'état rapporté par la carte diffère de la demande.
                letter = grbl.ACCESSORIES[name][1]
                current = letter in ((controller.last_status or {}).get("accessories") or [])
                if current == on:
                    return result(f"{label} déjà {'en marche' if on else off_word} d’après la carte : rien à basculer.", confirmed=False)
                controller.accessory_toggle(name)
                return result(f"{label} : bascule envoyée pendant le job ({'mise en marche' if on else 'coupure'}) ; l’état réel suit dans le rapport.",
                              confirmed=False)
            self._grbl_ready_to_move(controller)
            controller.accessory(name, on)
            controller.status(1.0)
            state_text = f"en marche ({grbl.ACCESSORIES[name][0]})" if on else f"{off_word} (M9 : toutes les sorties accessoires coupées)"
            return result(f"{label} {state_text}.")
        if command == "mdi":
            line = request.get("line")
            if not isinstance(line, str) or not line.strip():
                raise ValueError("Ligne G-code absente.")
            prepared = grbl.prepare_gcode(line)  # mêmes refus qu'un fichier : ligne longue, temps réel, non ASCII
            if len(prepared) != 1:
                raise ValueError("La console accepte une seule ligne G-code à la fois.")
            text = prepared[0][1]
            if text.startswith("$"):
                raise ValueError("Les commandes $ passent par Paramètres GRBL et Informations, pas par la console.")
            if grbl.mdi_forbidden(text):
                raise ValueError("Les retours et décalages (G28/G30/G53/G92/G10) passent par Origine / Zéro machine, pas par la console.")
            if grbl.gcode_emits(text):
                if device.spec["kind"] == "laser":
                    raise ValueError("Émission laser (M3/M4 ou S > 0) refusée en console : utilise Test laser.")
                self._grbl_require_operator_checks(cfg, request.get("checklist_confirmed") is True)
            self._grbl_ready_to_move(controller)
            feedback = controller.mdi(text)
            return result(f"« {text} » acceptée par GRBL.", feedback=feedback)
        if command == "probe_z":
            if device.spec["kind"] != "cnc":
                raise ValueError("Palpage Z réservé aux CNC.")
            feed = self._grbl_feed(cfg, request.get("feed_mm_min"), "Vitesse de palpage")
            travel_limit = min(GRBL_PROBE_MAX_TRAVEL_MM, float((cfg.get("work_area_mm") or {}).get("z") or GRBL_PROBE_MAX_TRAVEL_MM))
            raw = request.get("max_travel_mm")
            try:
                travel = float(raw)
            except (TypeError, ValueError):
                raise ValueError("Course de palpage invalide.") from None
            if isinstance(raw, bool) or not math.isfinite(travel) or not 0.1 <= travel <= travel_limit:
                raise ValueError(f"Course de palpage entre 0,1 et {travel_limit:g} mm.")
            self._grbl_ready_to_move(controller)
            self.agent.command_progress(command_id, 20, "Palpage Z en cours (G38.3)…")
            probe = controller.probe_z(feed, travel)
            z = probe.get("z")
            return result(f"Palpage terminé : Z {z:.3f}." if z is not None else "Palpage terminé.", z=z, position=list(probe.get("position") or ()))
        if command == "jog":
            axis = str(request.get("axis") or "").upper()
            if axis not in {"X", "Y", "Z"}:
                raise ValueError("Axe de jog invalide (X, Y ou Z).")
            limit = (cfg.get("work_area_mm") or {}).get(axis.lower())
            try:
                distance = float(request.get("distance_mm"))
                feed = float(request.get("feed_mm_min") or min(1000.0, float(cfg.get("max_feed_mm_min") or 3000)))
            except (TypeError, ValueError):
                raise ValueError("Distance ou vitesse de jog invalide.") from None
            if not limit or not abs(distance) <= float(limit) or distance == 0:
                raise ValueError(f"Jog {axis} refusé : hors de la course configurée ({limit or 'non définie'} mm).")
            if not 0 < feed <= float(cfg.get("max_feed_mm_min") or 3000):
                raise ValueError("Vitesse de jog au-delà du maximum configuré pour cette machine.")
            self._grbl_ready_to_move(controller, allowed=("Idle", "Jog"))
            controller.jog(**{"d" + axis.lower(): distance}, feed=feed)
            return result(f"Jog {axis}{distance:+g} mm à {feed:g} mm/min accepté par GRBL.")
        if command == "laser_test":
            if device.spec["kind"] != "laser":
                raise ValueError("Test laser réservé aux machines laser.")
            try:
                power = float(request.get("power_pct") or 0)
            except (TypeError, ValueError):
                raise ValueError("Puissance du test laser invalide.") from None
            if not 0 < power <= GRBL_LASER_TEST_MAX_PCT:
                raise ValueError(f"Puissance du test laser limitée à {GRBL_LASER_TEST_MAX_PCT:g} %.")
            raw = request.get("duration_ms")
            try:
                duration = float(GRBL_LASER_TEST_MS if raw in (None, "") else raw)
            except (TypeError, ValueError):
                raise ValueError("Durée du test laser invalide.") from None
            low, high = GRBL_LASER_TEST_MS_RANGE
            if isinstance(raw, bool) or not math.isfinite(duration) or duration != int(duration) or not low <= duration <= high:
                raise ValueError(f"Durée du test laser : nombre entier de {low} à {high} ms.")
            duration = int(duration)
            confirmed = request.get("checklist_confirmed") is True
            self._grbl_require_operator_checks(cfg, confirmed)
            self._fire_start_check(device, controller, command, True)
            device.grbl_checklist = confirmed
            try:
                pulse = controller.laser_test(power, duration)
            finally:
                device.grbl_checklist = False
            return result(f"Impulsion laser de {duration} ms à {power:g} % (S{pulse['spindle']}) terminée, laser coupé.")
        # stream / frame : job complet dans un fil dédié du Device.
        laser_pct = 0.0
        if command == "frame" and request.get("laser_pct") not in (None, "", 0):
            # Cadrage laser allumé : émission réelle, donc laser seulement, capot fermé, puissance bornée.
            try:
                laser_pct = float(request.get("laser_pct"))
            except (TypeError, ValueError):
                raise ValueError("Puissance du laser au cadrage invalide.") from None
            if not 0 < laser_pct <= GRBL_FRAME_LASER_MAX_PCT:
                raise ValueError(f"Cadrage laser allumé limité à {GRBL_FRAME_LASER_MAX_PCT:g} %.")
            if device.spec["kind"] != "laser":
                raise ValueError("Cadrage laser allumé réservé aux graveurs laser.")
            if request.get("door_open") is True:
                # Capot ouvert : choix explicite de l'opérateur (check-list dédiée), repérage seulement.
                if laser_pct > GRBL_FRAME_LASER_OPEN_MAX_PCT:
                    raise ValueError(f"Capot ouvert, le laser de cadrage est limité à {GRBL_FRAME_LASER_OPEN_MAX_PCT:g} % (repérage).")
                if request.get("laser_open_lid_ack") is not True or request.get("checklist_confirmed") is not True:
                    raise ValueError("Laser allumé capot ouvert : la check-list dédiée doit être validée pour ce cadrage.")
        # Garde incendie avant tout téléchargement (refus immédiat), puis de nouveau juste avant le départ.
        fire_laser_on = laser_pct > 0 if command == "frame" else device.spec["kind"] == "laser"
        self._fire_start_check(device, controller, command, fire_laser_on)
        text = self._grbl_gcode(request, payload, command_id)
        if command == "frame":
            maximum = controller.settings.get(30) or cfg.get("laser_max_s") or 1000
            text = frame_gcode(text, laser_s=max(1, int(round(float(maximum) * laser_pct / 100))) if laser_pct else 0)
        lines = grbl.prepare_gcode(text)  # refuse ce que GRBL refuserait (ligne longue, temps réel, non ASCII)
        if not lines:
            raise ValueError("Aucune ligne de G-code exploitable.")
        confirmed = request.get("checklist_confirmed") is True
        laser_on = laser_pct > 0
        if command == "stream" or laser_on:
            self._grbl_require_operator_checks(cfg, confirmed)
        # Cadrage seulement (G-code déjà réduit par frame_gcode à des mouvements, laser coupé « M5 S0 ») :
        # en continu jusqu'à « frame_stop », et capot ouvert si l'opérateur l'autorise explicitement.
        loop = command == "frame" and request.get("loop") is True
        door_open = command == "frame" and request.get("door_open") is True
        status = controller.status(1.0) or {}
        if not door_open and (status.get("state") == "Door" or "D" in (status.get("pins") or [])):
            raise ValueError("Porte de sécurité ouverte (état Door) : job refusé."
                             + (" Coche « Capot ouvert autorisé » pour cadrer capot ouvert, laser coupé." if command == "frame" else ""))
        if status.get("state") == "Alarm":
            raise ValueError("Machine en alarme : déverrouille ($X) ou lance un homing avant un job.")
        if status.get("state") != "Idle" and not (door_open and status.get("state") == "Door"):
            raise ValueError(f"Machine non prête (état {status.get('state') or 'inconnu'}).")
        device.grbl_checklist = confirmed
        failed = [] if door_open else [item["label"] for item in device.grbl_interlocks(operator=command == "stream" or laser_on) if not item["satisfied"]]
        if failed:
            device.grbl_checklist = False
            raise ValueError("Sécurité non satisfaite : " + ", ".join(failed) + ".")
        if request.get("dry_run") is True:
            device.grbl_checklist = False
            return {"sent": False, "device_confirmed": False, "dry_run": True, "lines": len(lines),
                    "message": f"Vérification à blanc : {len(lines)} lignes acceptées, rien n’a été envoyé.", "grbl": device.grbl_report()}
        name = str(request.get("name") or ("cadrage" if command == "frame" else "job"))[:100]
        exiting = getattr(self.agent, "lifecycle_exit_pending", None)
        if callable(exiting) and exiting() is True:
            device.grbl_checklist = False
            raise ValueError("Mise à jour, redémarrage ou déconnexion de l’agent en cours : job refusé, la sortie couperait "
                             "la liaison série en pleine gravure. Relance-le quand le Worker est revenu.")
        try:
            self._fire_start_check(device, controller, command, fire_laser_on)
        except ValueError:
            device.grbl_checklist = False
            raise
        self.agent.command_progress(command_id, 5, f"Lancement du {'cadrage en continu' if loop else 'job'} « {name} » ({len(lines)} lignes)…")
        progress = self._grbl_progress(command_id)
        try:
            device.grbl_start_job(name, text, len(lines), command_id, confirmed, progress, operator_checks=command == "stream" or laser_on,
                                  loop=loop, door_open=door_open, laser_pct=laser_pct)
            job = device.grbl_wait_started(3.0)
        finally:
            # La commande répond ici : la suite du job est suivie par le rapport de l'équipement, pas par elle.
            progress.close()
        if not job.get("active") and not job.get("ok"):
            raise ValueError(job.get("error") or "Job GRBL interrompu avant son premier envoi.")
        laser_text = f"laser allumé à {laser_pct:g} %" if laser_on else "laser coupé"
        if loop:
            return result(f"Cadrage en continu « {name} » lancé ({len(lines)} lignes par passe, {laser_text}"
                          + (", capot ouvert autorisé" if door_open else "") + ") : « Arrêter le cadrage » pour terminer, ARRÊT en cas d’urgence.")
        if command == "frame" and laser_on:
            return result(f"Cadrage « {name} » lancé, {laser_text} sur le contour (éteint pendant les déplacements rapides)"
                          + (", capot ouvert autorisé" if door_open else "") + ".")
        if not job.get("active"):
            return result(f"Job « {name} » terminé : {job.get('done', 0)} lignes envoyées.")
        return result(f"Job « {name} » lancé : {len(lines)} lignes. Suivi dans l’état de la machine ; Pause, Reprendre et Arrêter restent disponibles.")

    def pause_locale(self, equipment_id):
        """Mettre l'impression en pause depuis le Worker, sans passer par le site.

        Réservé à la pause automatique de PrintGuard : la caméra, l'analyse et
        l'imprimante sont sur cette machine, la décision n'a donc aucune raison de
        dépendre du PC qui héberge le site. Rien d'autre que « pause » ne part d'ici,
        et seulement pendant une impression réellement en cours.
        """
        device = self.devices.get(str(equipment_id or ""))
        if not device or device.spec.get("kind") != "printer":
            raise ValueError("Imprimante inconnue de ce Worker.")
        if not (device.client or device.driver) or not device.report().get("connected"):
            raise ValueError("La connexion imprimante n’est pas prête sur le Worker.")
        etat = str(device.telemetry.get("gcode_state") or "").upper()
        if etat != "RUNNING":
            raise ValueError(f"Aucune impression en cours (état {etat or 'inconnu'}) ; aucune pause envoyée.")
        return self._envoyer(device, device.spec, {"print": {"command": "pause"}})

    # ------------------------------------------------ surveillance incendie (agent 1.34.0)
    def _fire_start_check(self, device, controller, command, laser_on):
        """Garde de départ d'un ``stream``, ``frame`` ou ``laser_test`` : ``ValueError`` si refusé.

        Un contrôleur encore bloqué par un arrêt incendie refuse tout départ. La garde de la surveillance
        (``fire_guard.start_refusal``) applique le verrou persistant et l'exigence d'une surveillance saine ;
        une garde en panne refuse le départ : jamais un départ silencieux sans surveillance.
        """
        if getattr(controller, "halted", False):
            raise ValueError(grbl_module().HALTED_MESSAGE)
        guard = self.fire_guard
        if guard is None:
            return
        try:
            refusal = guard.start_refusal(device.spec["id"], command, bool(laser_on))
        except Exception as error:
            raise ValueError(f"Surveillance incendie indisponible ({type(error).__name__}) : départ refusé par sécurité ; "
                             "vérifie l’état de la surveillance sur ce Worker.") from None
        if refusal is None:
            return
        if isinstance(refusal, str) and refusal.strip():
            raise ValueError(refusal.strip()[:500])
        raise ValueError("Surveillance incendie indisponible (réponse invalide de la garde) : départ refusé par sécurité.")

    def _fire_impossible(self, requested_at, reason, incident_id, message):
        return {"status": "impossible", "requested_at": requested_at, "written_at": None, "confirmed_at": None, "elapsed_ms": 0.0,
                "attempts": 0, "message": message, "evidence": grbl_module().fire_evidence(), "reason": reason,
                "incident_id": incident_id, "coalesced": False}

    def _fire_recent(self, identifier, last):
        """Dernier arrêt confirmé de cette machine, s'il vaut encore pour un nouvel appel (sinon None)."""
        if not last or last["result"].get("status") != "confirmed" or time.monotonic() - last["at"] >= GRBL_FIRE_COALESCE_SECONDS:
            return None
        device = self.devices.get(identifier)
        if device is None or device.grbl is not last["controller"]:
            return None  # machine reconnectée depuis : nouveau contrôleur, nouvel arrêt
        with device.lock:
            started = device.grbl_job.get("started_at")
        if started is not None and started >= last["requested_at"]:
            return None  # un job a démarré depuis l'arrêt
        return last["result"]

    def fire_stop(self, equipment_id, reason, incident_id=None):
        """Arrêt incendie demandé par la surveillance du Worker, sans passer par le site ni par la file de commandes.

        Prioritaire et idempotent : un appel pendant un arrêt en cours attend le résultat de celui-ci ; un appel
        moins de 10 s après un arrêt confirmé, sans job démarré depuis, reçoit ce résultat (``coalesced`` vrai)
        au lieu d'un second reset. ``impossible`` quand le Worker ne tient pas la liaison de la machine.
        Renvoie le résultat du contrôleur (``requested_at``, ``written_at``, ``confirmed_at``, ``elapsed_ms``,
        ``attempts``, ``message``, ``evidence``) complété de ``reason``, ``incident_id`` et ``coalesced``.
        """
        identifier = str(equipment_id or "")
        requested_at = time.time()
        reason = str(reason or "")[:60]
        with self._fire_lock:
            state = self._fire_state.setdefault(identifier, {"episode": None, "last": None})
            episode = state["episode"]
            owner = episode is None
            if owner:
                recent = self._fire_recent(identifier, state["last"])
                if recent is not None:
                    return {**copy.deepcopy(recent), "coalesced": True}
                episode = state["episode"] = {"done": threading.Event(), "result": None}
        if not owner:
            episode["done"].wait(GRBL_FIRE_WAIT_SECONDS)
            result = episode["result"]
            if result is None:
                failed = self._fire_impossible(requested_at, reason, incident_id, "Un arrêt incendie est déjà en cours sur cette "
                                               "machine et n’a pas rendu de résultat : coupe l’alimentation de la graveuse.")
                return {**failed, "status": "failed", "coalesced": True}
            return {**copy.deepcopy(result), "coalesced": True}
        controller = None
        try:
            device = self.devices.get(identifier)
            controller = device.grbl if device is not None else None
            if device is None:
                result = self._fire_impossible(requested_at, reason, incident_id, "Arrêt impossible : cette machine n’est pas connue de "
                                               "ce Worker (équipement absent ou désactivé). Coupe l’alimentation de la graveuse.")
            elif device.spec.get("kind") not in GRBL_KINDS:
                result = self._fire_impossible(requested_at, reason, incident_id, "Arrêt impossible : cet équipement n’est pas une "
                                               "machine GRBL (laser ou CNC).")
            elif controller is None:
                result = self._fire_impossible(requested_at, reason, incident_id, "Arrêt impossible : le Worker ne tient pas la liaison "
                                               "de cette graveuse (déconnectée, ou pilotée par un autre logiciel comme LightBurn). "
                                               "Coupe l’alimentation de la graveuse.")
            else:
                result = controller.fire_stop()
                # Après le reset, jamais avant : rien ne doit retarder l'octet 0x18.
                device.grbl_loop_stop.set()
                device.grbl_checklist = False
                result.update(requested_at=requested_at, reason=reason, incident_id=incident_id, coalesced=False)
        except Exception as error:
            result = self._fire_impossible(requested_at, reason, incident_id, f"Arrêt incendie non confirmé : erreur du Worker "
                                           f"({type(error).__name__}). Coupe immédiatement l’alimentation de la graveuse.")
            result["status"] = "failed"
        with self._fire_lock:
            episode["result"] = result
            state["episode"] = None
            if result.get("status") == "confirmed":
                state["last"] = {"result": result, "at": time.monotonic(), "requested_at": requested_at, "controller": controller}
        episode["done"].set()
        return copy.deepcopy(result)

    def release_halt(self, equipment_id):
        """Acquittement de l'arrêt incendie par un opérateur : les envois vers la machine sont de nouveau permis.

        Rien n'est envoyé à la carte (ni ``$X``, ni homing) ; le prochain arrêt fera de nouveau un vrai reset.
        """
        identifier = str(equipment_id or "")
        with self._fire_lock:
            state = self._fire_state.get(identifier)
            if state is not None:
                state["last"] = None
        device = self.devices.get(identifier)
        controller = device.grbl if device is not None else None
        release = getattr(controller, "release_halt", None)
        if release is None:
            return False
        release()
        return True

    def fire_snapshot(self, equipment_id):
        """État de la machine pour la surveillance, sans aucune lecture sur la liaison série (lecture seule)."""
        device = self.devices.get(str(equipment_id or ""))
        result = {"present": device is not None, "kind": device.spec.get("kind") if device is not None else None, "linked": False,
                  "job_active": False, "state": None, "spindle": None, "feed": None, "alarm": None, "laser_mode": None,
                  "last_status_age_s": None, "halted": False}
        if device is None or device.spec.get("kind") not in GRBL_KINDS:
            return result
        controller = device.grbl
        with device.lock:
            result["job_active"] = bool(device.grbl_job.get("active"))
        if controller is None:
            return result
        status = controller.last_status or {}
        received = status.get("received_at")
        result.update(linked=bool(controller.connected), state=status.get("state"), spindle=status.get("spindle"), feed=status.get("feed"),
                      alarm=controller.alarm, laser_mode=controller.settings.get(32) == 1, halted=bool(getattr(controller, "halted", False)),
                      last_status_age_s=round(max(0.0, time.monotonic() - received), 3) if received is not None else None)
        result["job_active"] = result["job_active"] or controller._streaming.is_set()
        return result

    def camera_frame(self, equipment_id):
        """Dernière image d'une caméra du Worker, sans encodage : ``{"present", "kind", "connected", "frame", "frame_at"}``."""
        device = self.devices.get(str(equipment_id or ""))
        if device is None:
            return {"present": False, "kind": None, "connected": False, "frame": None, "frame_at": 0.0}
        frame, frame_at = device.frame_value()
        with device.lock:
            connected = bool(device.snapshot.get("connected"))
        fps = getattr(device, "capture_fps", None)
        return {"present": True, "kind": device.spec.get("kind"), "connected": connected, "frame": frame,
                "frame_at": float(frame_at or 0.0), "fps": round(float(fps), 2) if fps else None}

    def sync(self, items):
        with self.lock:
            wanted = {item["id"]: item for item in items}
            for key in list(self.devices):
                # A routine save bumps the revision even without a meaningful settings
                # change (equipment_service.py's cancel_superseded only drops a queued,
                # not-yet-delivered command for this reason). An equipment.test already
                # delivered and running must finish under its own control, not be torn
                # down here just because the periodic sync saw a newer revision.
                if key in self.testing and key in wanted:
                    continue
                if key not in wanted or self.devices[key].spec != wanted[key]:
                    self._remove_source(key)
                    self.devices.pop(key).close()
            for key, spec in wanted.items():
                if key in self.testing:
                    continue
                wanted_relay = self._relay_wanted(spec)
                if key not in self.devices:
                    device = self.devices[key] = Device(spec, self.agent.root)
                    if spec["settings"].get("camera_mode") != "relay":
                        device.start()
                    if wanted_relay and spec["settings"].get("camera_mode") != "relay" and not self.relay_intent.get(key):
                        # Lien MJPEG local automatique (relay_enabled sur le site) : la fiche enregistrée vaut
                        # intention ; une sauvegarde (revision+1) ou un redémarrage de l'agent relance le relais.
                        # Un « Arrêter le relais » explicite reste respecté jusqu'à la prochaine fiche.
                        self._save_intent(key, True)
                device = self.devices[key]
                if (spec["kind"] == "camera" and spec.get("enabled") and key == self._printguard_camera_id() and key not in self.video_sources
                        and spec["settings"].get("connector") != "mjpeg_http" and not wanted_relay):
                    # PrintGuard reads this camera on the loopback relay: bring the
                    # relay back after an agent restart so the hub does not stay blind.
                    try:
                        self._start_source(device)
                    except Exception:
                        pass
                if not relay_configured(spec):
                    if spec["kind"] == "camera" and key not in self.video_sources and device.relay_state.get("relay_running"):
                        device.relay_state.update(relay_running=False, relay_verified=False)
                    continue
                device.relay_state["relay_installed"] = self._component().is_file()
                if not wanted_relay or not self.relay_intent.get(key):
                    if key in self.video_sources:
                        self._remove_source(key)
                    device.relay_state.update(relay_running=False, relay_verified=False, relay_message="Relais arrêté. Utilise Installer puis Démarrer le relais.")
                    continue
                try:
                    if key not in self.video_sources:
                        self._start_source(device)
                    if device.frame and time.time() - device.frame_at < 8 and not device.relay_state.get("relay_verified"):
                        self._probe_source(device)
                except Exception:
                    device.relay_state.update(relay_running=False, relay_verified=False, relay_message="Relais indisponible : vérifie le composant, le port libre et la source caméra sur le Worker.")

    def loop(self):
        delay = 2
        while not self.agent.stop_requested.is_set():
            if getattr(self.agent, "disconnecting", False):
                # Déconnexion en cours : ne rouvrir aucune liaison ni relais vidéo.
                self.site_live.close()
                self.sync([])
                self.agent.stop_requested.wait(1)
                continue
            try:
                response = self.agent.request("/api/worker-protocol/equipment")
                self.sync(response.get("equipment", []))
                self.config_fetched_at = time.time()
                # Vue fluide du site : caméras regardées en ce moment (clé absente d'un site plus ancien = rien).
                self.site_live.update(response.get("camera_live"), self.devices)
                with self.lock:
                    reports = [device.report() for device in self.devices.values()]
                for offset in range(0, len(reports), 3):
                    self.agent.request("/api/worker-protocol/equipment/report", "POST", {"equipment": reports[offset:offset+3]})
                delay = 2
            except Exception as error:
                if getattr(error, "code", None) in {401, 403}:
                    self.site_live.close()
                    self.sync([])
                delay = min(30, delay*2)
            self.agent.stop_requested.wait(delay)
        self.site_live.close()
        self.sync([])
