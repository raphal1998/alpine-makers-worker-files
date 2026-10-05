"""Pilotes d'imprimante HTTP locaux du Worker : Klipper/Moonraker et OctoPrint.

Le contrat reste celui déjà consommé par le dashboard : la télémétrie sort avec
les clés Bambu autorisées par ``equipment_runtime.TELEMETRY_KEYS`` et les
commandes arrivent au format fil ``{"print"|"pushing"|"system": {...}}``. Aucune
page du dashboard ne change donc pour ces machines.

Les secrets (clé API) ne circulent que dans l'en-tête ``X-Api-Key`` : jamais dans
une URL, jamais dans un message rendu à l'utilisateur. Aucun corps de réponse
n'est recopié dans un message : un serveur local compromis ne peut pas écrire
dans l'interface.
"""
import json
import re
import secrets
import time
import urllib.error
import urllib.request

try:
    from .equipment_runtime import HTTP_PRINTER_PORTS, NoRedirect, local_host, port_number
except ImportError:  # Sur un Worker installé les modules sont à plat.
    from equipment_runtime import HTTP_PRINTER_PORTS, NoRedirect, local_host, port_number

MAX_RESPONSE = 512 * 1024
MAX_UPLOAD = 2 * 1024 ** 3
UPLOAD_SUFFIXES = {".gcode", ".gco", ".g", ".gc"}
COMMAND_NAME = re.compile(r"[A-Za-z0-9_]{1,32}")


class DriverError(ValueError):
    """Erreur déjà formulée pour l'utilisateur : sans URL, clé ni corps de réponse."""


def _number(value, digits=1, limit=10 ** 9):
    try:
        number = round(float(value), digits)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if -limit <= number <= limit else None


def _decimal(value):
    """Température : une valeur hors plage physique n'est pas affichée."""
    return _number(value, 1, 2000)


def _integer(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = int(value)
    except (OverflowError, ValueError):
        return None
    return number if 0 <= number <= 1000000 else None


def _ratio(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, number))


def _gcode_script(param):
    """Retire le cadrage Bambu (espace avant chaque saut de ligne), inutile ici."""
    lines = [str(line).strip() for line in str(param or "").splitlines()]
    return "\n".join(line for line in lines if line)


def _command_label(command):
    name = str(command or "")
    return name if COMMAND_NAME.fullmatch(name) else "inconnue"


class HttpPrinterDriver:
    """Base commune : une IP privée, un port, une clé API facultative."""

    CONNECTOR = ""
    LABEL = "l’imprimante"
    START_STATES = frozenset()

    def __init__(self, settings):
        self.host = local_host(settings["host"])
        self.port = port_number(settings.get("port"), HTTP_PRINTER_PORTS[self.CONNECTOR])
        self.api_key = str(settings.get("api_key") or "")
        authority = f"[{self.host}]" if ":" in self.host else self.host
        self.base = f"http://{authority}:{self.port}"
        self.closed = False

    def close(self):
        # Aucune connexion persistante n'est conservée : rien à libérer.
        self.closed = True

    def status_message(self, code):
        if code in {401, 403}:
            return f"Accès refusé par {self.LABEL} (HTTP {code}) : vérifie la clé API de ce connecteur."
        if code == 404:
            return f"Point d’accès absent (HTTP 404) : {self.LABEL} n’écoute probablement pas sur ce port."
        return f"{self.LABEL} a refusé la requête (HTTP {code})."

    def _headers(self):
        headers = {"Accept": "application/json"}
        if self.api_key:
            # La clé reste dans cet en-tête : jamais dans l'URL, jamais journalisée.
            headers["X-Api-Key"] = self.api_key
        return headers

    def _call(self, method, path, *, body=None, stream=None, timeout=6):
        headers = self._headers()
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif stream is not None:
            content_type, length, data = stream
            headers["Content-Type"] = content_type
            headers["Content-Length"] = str(length)
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(request, timeout=timeout) as response:
                payload = response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise DriverError(self.status_message(code)) from None
        except ValueError:
            # NoRedirect : une redirection enverrait la clé API vers un autre hôte.
            raise DriverError("Redirection HTTP refusée : l’imprimante locale doit répondre elle-même.") from None
        except (OSError, urllib.error.URLError) as error:
            raise DriverError(f"{self.LABEL} injoignable depuis le Worker ({type(error).__name__}). Vérifie l’IP privée, le port et le pare-feu.") from None
        if len(payload) > MAX_RESPONSE:
            raise DriverError(f"Réponse trop volumineuse : ce port n’héberge pas l’API {self.LABEL}.")
        if not payload.strip():
            return {}
        try:
            result = json.loads(payload)
        except ValueError:
            raise DriverError(f"Réponse non JSON : ce port n’héberge pas l’API {self.LABEL}.") from None
        return result if isinstance(result, dict) else {}

    def _multipart(self, path, fields, progress=None):
        """Corps multipart diffusé depuis le disque : jamais chargé en mémoire."""
        if path.suffix.lower() not in UPLOAD_SUFFIXES:
            raise DriverError("Fichier non imprimable par ce connecteur : Klipper et OctoPrint attendent un G-code (.gcode), pas le 3MF Bambu du trancheur actuel.")
        size = path.stat().st_size
        if not 0 < size <= MAX_UPLOAD:
            raise DriverError("Fichier d’impression vide ou trop volumineux : envoi refusé.")
        boundary = "----alpine" + secrets.token_hex(16)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", path.name)[:120] or "print.gcode"
        prologue = b""
        for field, value in fields.items():
            prologue += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"\r\n\r\n{value}\r\n".encode()
        prologue += f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
        epilogue = f"\r\n--{boundary}--\r\n".encode()

        def chunks():
            yield prologue
            sent = 0
            with path.open("rb") as source:
                while True:
                    block = source.read(256 * 1024)
                    if not block:
                        break
                    sent += len(block)
                    if progress:
                        progress(sent)
                    yield block
            yield epilogue

        return f"multipart/form-data; boundary={boundary}", len(prologue) + size + len(epilogue), chunks()

    def _started(self, uploaded):
        if uploaded is None:
            raise DriverError("Lancement d’impression sans fichier transféré : opération annulée.")
        if uploaded:
            return {"sent": True, "device_confirmed": True, "message": "Fichier envoyé et démarrage confirmé par l’API locale de l’imprimante."}
        return {"sent": True, "device_confirmed": False, "message": "Fichier envoyé, mais le démarrage n’est pas confirmé. Vérifie l’imprimante avant toute nouvelle commande."}

    def _wire_command(self, wire):
        request = wire.get("print") if isinstance(wire.get("print"), dict) else {}
        command = str(request.get("command") or "")
        if command:
            return request, command
        if isinstance(wire.get("pushing"), dict):
            return request, "pushing"
        raise DriverError(f"Commande non prise en charge par {self.LABEL} ; rien n’a été envoyé.")

    def _refresh_only(self):
        # L'interrogation périodique du Worker rafraîchit déjà la télémétrie :
        # un « pushall » Bambu n'a aucun équivalent à demander ici.
        return {"sent": True, "device_confirmed": True, "message": "Télémétrie déjà actualisée par le Worker ; aucune requête supplémentaire."}


class MoonrakerDriver(HttpPrinterDriver):
    """Klipper via Moonraker : une seule requête rend toute la télémétrie."""

    CONNECTOR = "moonraker_http"
    LABEL = "Moonraker"
    OBJECTS = {"extruder": None, "heater_bed": None, "print_stats": None, "display_status": None, "virtual_sdcard": None, "toolhead": None}
    STATES = {"printing": "RUNNING", "paused": "PAUSE", "complete": "FINISH", "cancelled": "FAILED", "error": "FAILED", "standby": "IDLE"}
    KLIPPY_STATES = {"ready", "startup", "shutdown", "error"}
    ROUTES = {"pause": "/printer/print/pause", "resume": "/printer/print/resume", "stop": "/printer/print/cancel"}

    def poll(self):
        try:
            answer = self._call("POST", "/printer/objects/query", body={"objects": self.OBJECTS})
        except DriverError as failure:
            raise self._klippy_detail(failure) from None
        status = (answer.get("result") or {}).get("status")
        if not isinstance(status, dict) or not status:
            raise self._klippy_detail(DriverError("Moonraker répond sans état d’imprimante : vérifie que Klipper est démarré."))
        return self.telemetry(status)

    def _klippy_detail(self, failure):
        """Distingue « rien n'écoute » de « Moonraker répond, Klipper est en erreur »."""
        try:
            info = self._call("GET", "/printer/info", timeout=4).get("result") or {}
        except DriverError:
            return failure
        state = str(info.get("state") or "")
        if state in self.KLIPPY_STATES and state != "ready":
            # Seul un état de la liste connue est repris ; jamais le texte brut du firmware.
            return DriverError(f"Moonraker répond, mais Klipper n’est pas prêt (état « {state} »). Corrige l’erreur sur l’imprimante.")
        return failure

    def telemetry(self, status):
        extruder = status.get("extruder") or {}
        bed = status.get("heater_bed") or {}
        stats = status.get("print_stats") or {}
        display = status.get("display_status") or {}
        toolhead = status.get("toolhead") or {}
        info = stats.get("info") if isinstance(stats.get("info"), dict) else {}
        values = {"gcode_state": self.STATES.get(str(stats.get("state") or "").lower(), "UNKNOWN")}
        for key, source in (("nozzle_temper", extruder.get("temperature")), ("nozzle_target_temper", extruder.get("target")),
                            ("bed_temper", bed.get("temperature")), ("bed_target_temper", bed.get("target"))):
            number = _decimal(source)
            if number is not None:
                values[key] = number
        progress = _ratio(display.get("progress"))
        duration = _number(stats.get("print_duration"), 3) or 0
        values["mc_percent"] = int(round(progress * 100))
        values["mc_remaining_time"] = int(round(duration * (1 - progress) / progress / 60)) if progress > 0.001 and duration > 0 else 0
        name = str(stats.get("filename") or "")[:120]
        if name:
            values.update(subtask_name=name, gcode_file=name)
        for key, source in (("layer_num", info.get("current_layer")), ("total_layer_num", info.get("total_layer"))):
            number = _integer(source)
            if number is not None:
                values[key] = number
        if isinstance(toolhead.get("homed_axes"), str):
            # Bits Bambu X=1, Y=2, Z=4 : le garde-fou « axe non référencé » du
            # dashboard fonctionne alors sans modification pour Klipper.
            axes = toolhead["homed_axes"].lower()
            values["home_flag"] = sum(bit for axis, bit in (("x", 1), ("y", 2), ("z", 4)) if axis in axes)
        return values

    def send(self, wire, uploaded=None):
        request, command = self._wire_command(wire)
        if command == "pushing":
            return self._refresh_only()
        if command == "project_file":
            return self._started(uploaded)
        if command in self.ROUTES:
            self._call("POST", self.ROUTES[command], timeout=10)
            return {"sent": True, "device_confirmed": True, "message": "Commande confirmée par Moonraker."}
        if command == "gcode_line":
            script = _gcode_script(request.get("param"))
            if not script:
                raise DriverError("Ligne G-code vide : rien n’a été envoyé.")
            # /printer/gcode/script ne répond qu'après exécution par Klipper.
            self._call("POST", "/printer/gcode/script", body={"script": script}, timeout=20)
            return {"sent": True, "device_confirmed": True, "message": "G-code exécuté et confirmé par Klipper."}
        raise DriverError(f"Commande « {_command_label(command)} » non prise en charge par Klipper/Moonraker ; rien n’a été envoyé.")

    def upload(self, path, progress=None):
        stream = self._multipart(path, {"root": "gcodes", "print": "true"}, progress)
        answer = self._call("POST", "/server/files/upload", stream=stream, timeout=180)
        return answer.get("print_started") is True


class OctoPrintDriver(HttpPrinterDriver):
    """OctoPrint : clé API obligatoire, et 204 sans accusé sur les commandes."""

    CONNECTOR = "octoprint_http"
    LABEL = "OctoPrint"
    FLAGS = (("error", "FAILED"), ("closedOrError", "FAILED"), ("cancelling", "FAILED"),
             ("paused", "PAUSE"), ("pausing", "PAUSE"), ("printing", "RUNNING"))
    EXPECTED = {"pause": {"PAUSE"}, "resume": {"RUNNING"}, "stop": {"IDLE", "FAILED", "FINISH"}}

    def status_message(self, code):
        if code in {401, 403}:
            return "Clé API refusée par OctoPrint : vérifie la clé dans Réglages → Accès API."
        if code == 409:
            return "OctoPrint n’est pas connecté à l’imprimante (liaison série fermée). Connecte-la dans OctoPrint, puis réessaie."
        return super().status_message(code)

    def poll(self):
        printer = self._call("GET", "/api/printer?exclude=history")
        job = self._call("GET", "/api/job")
        return self.telemetry(printer, job)

    def state_of(self, printer):
        flags = (printer.get("state") or {}).get("flags") or {}
        for name, mapped in self.FLAGS:
            if flags.get(name) is True:
                return mapped
        return "IDLE"

    def telemetry(self, printer, job):
        temperature = printer.get("temperature") or {}
        tool = temperature.get("tool0") or {}
        bed = temperature.get("bed") or {}
        progress = job.get("progress") or {}
        descriptor = ((job.get("job") or {}).get("file") or {})
        values = {"gcode_state": self.state_of(printer)}
        for key, source in (("nozzle_temper", tool.get("actual")), ("nozzle_target_temper", tool.get("target")),
                            ("bed_temper", bed.get("actual")), ("bed_target_temper", bed.get("target"))):
            number = _decimal(source)
            if number is not None:
                values[key] = number
        completion = _number(progress.get("completion"), 2, 1000)
        values["mc_percent"] = int(round(min(100.0, max(0.0, completion)))) if completion is not None else 0
        remaining = _number(progress.get("printTimeLeft"), 0)
        values["mc_remaining_time"] = int(round(remaining / 60)) if remaining and remaining > 0 else 0
        name = str(descriptor.get("name") or "")[:120]
        if name:
            values.update(subtask_name=name, gcode_file=name)
        # OctoPrint n'expose ni numéro de couche ni axes référencés sans greffon :
        # ces clés restent absentes plutôt que d'être inventées.
        return values

    def _confirm(self, command, message):
        """OctoPrint répond 204 sans accusé : l'état réel est relu, en lecture seule."""
        expected = self.EXPECTED[command]
        for attempt in range(3):
            if attempt:
                time.sleep(.4)  # Le changement d'état n'est pas instantané côté firmware.
            try:
                if self.state_of(self._call("GET", "/api/printer?exclude=history,temperature,sd", timeout=5)) in expected:
                    return {"sent": True, "device_confirmed": True, "message": message}
            except DriverError:
                break
        return {"sent": True, "device_confirmed": False, "message": "Commande transmise ; OctoPrint n’a pas confirmé le nouvel état. Vérifie l’imprimante avant toute nouvelle commande."}

    def send(self, wire, uploaded=None):
        request, command = self._wire_command(wire)
        if command == "pushing":
            return self._refresh_only()
        if command == "project_file":
            return self._started(uploaded)
        if command in {"pause", "resume"}:
            self._call("POST", "/api/job", body={"command": "pause", "action": command}, timeout=10)
            return self._confirm(command, "Mise en pause confirmée par OctoPrint." if command == "pause" else "Reprise confirmée par OctoPrint.")
        if command == "stop":
            self._call("POST", "/api/job", body={"command": "cancel"}, timeout=10)
            return self._confirm(command, "Arrêt confirmé par OctoPrint.")
        if command == "gcode_line":
            lines = [line for line in _gcode_script(request.get("param")).splitlines() if line]
            if not lines:
                raise DriverError("Ligne G-code vide : rien n’a été envoyé.")
            self._call("POST", "/api/printer/command", body={"commands": lines}, timeout=20)
            # OctoPrint accepte la file d'envoi, pas l'exécution : ne pas prétendre l'inverse.
            return {"sent": True, "device_confirmed": False, "message": "Commande transmise ; OctoPrint ne confirme pas son exécution par l’imprimante."}
        raise DriverError(f"Commande « {_command_label(command)} » non prise en charge par OctoPrint ; rien n’a été envoyé.")

    def upload(self, path, progress=None):
        stream = self._multipart(path, {"select": "true", "print": "true"}, progress)
        answer = self._call("POST", "/api/files/local", stream=stream, timeout=180)
        return answer.get("effectivePrint") is True


DRIVERS = {MoonrakerDriver.CONNECTOR: MoonrakerDriver, OctoPrintDriver.CONNECTOR: OctoPrintDriver}


def build_driver(spec):
    settings = spec["settings"]
    driver = DRIVERS.get(str(settings.get("connector") or ""))
    if driver is None:
        raise ValueError("Connecteur HTTP non pris en charge par ce Worker.")
    return driver(settings)
