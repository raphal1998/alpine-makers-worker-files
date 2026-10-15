"""Small P1S JPEG -> MJPEG relay, bound to the Worker loopback only.

The P1S reader remains equipment_runtime.Device: this component does not assume
that a generic RTSP server understands the proprietary P1S TLS protocol.
"""
import hmac
import json
import os
import re
import secrets
import socket
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# 1.1.0 (agent 1.50.10) : cadence (fps), largeur/hauteur par source, redimensionnement Pillow mis en cache par trame.
# 1.2.0 : cadence tenue (échéances sur une grille ; « maintenant + période » servait 22 i/s pour 30 demandées),
#         jusqu'à 120 i/s.
# 1.3.0 (agent 1.50.20) : jeton de lecture par source, exigé en paramètre « token » de l'adresse (403 sans lui).
#         Aléatoire (secrets), stocké dans <racine du Worker>/runtime/camera-relay/<port>-<source>.token (ACL de
#         l'installation), stable d'un redémarrage à l'autre : le lien copié depuis le site reste valable pour OBS,
#         LightBurn ou PrintGuard. Supprimer le fichier puis reconnecter la caméra donne un nouveau jeton.
COMPONENT_VERSION = "1.3.0"
MAX_FPS = 120.0
RESIZE_UNAVAILABLE = "Redimensionnement indisponible (Pillow absent des dépendances du relais) : flux transmis tel quel."


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}")
SOURCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,80}")
TOKEN_REQUIRED_MESSAGE = "Jeton de lecture absent ou invalide : copie le Lien local de la camera depuis le site."


def token_directory(root):
    """Dossier des jetons du relais : <racine du Worker>/runtime/camera-relay (protégé par l'ACL de l'installation)."""
    return Path(root) / "runtime" / "camera-relay"


def _token_path(directory, port, source_id):
    port = int(port)
    if not 1024 <= port <= 65535 or not SOURCE_PATTERN.fullmatch(str(source_id or "")):
        raise ValueError("Port ou identifiant du relais invalide.")
    return Path(directory) / f"{port}-{source_id}.token"


def source_token(directory, port, source_id, create=False):
    """Jeton de lecture de la source ``source_id`` sur ``port`` ; créé (aléatoire, 32 caractères) si ``create``.

    Rend "" quand il n'existe pas encore et que ``create`` est faux. Écriture atomique (fichier temporaire puis
    remplacement) : un lecteur ne voit jamais un jeton à moitié écrit.
    """
    path = _token_path(directory, port, source_id)
    try:
        text = path.read_text(encoding="ascii").strip()
        if TOKEN_PATTERN.fullmatch(text):
            return text
    except (OSError, ValueError):
        pass
    if not create:
        return ""
    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
    temporary.write_text(token, encoding="ascii")
    os.replace(temporary, path)
    return token


def relay_url(port, source_id, token="", frame=False):
    """Adresse loopback d'une source : flux MJPEG (ou image fixe si ``frame``), jeton compris s'il y en a un."""
    url = f"http://127.0.0.1:{int(port)}/api/{'frame.jpeg' if frame else 'stream.mjpeg'}?src={source_id}"
    return url + ("&token=" + token if token else "")


def with_token(url, directory):
    """Ajoute le jeton local à une adresse du relais loopback qui n'en porte pas (lien d'avant le jeton, collé dans une
    fiche caméra ou dans PrintGuard). Toute autre adresse est rendue telle quelle."""
    from urllib.parse import parse_qs, urlsplit
    try:
        parsed = urlsplit(str(url or ""))
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port is None
                or parsed.path not in {"/api/stream.mjpeg", "/api/frame.jpeg"} or set(query) != {"src"} or len(query["src"]) != 1):
            return url
        token = source_token(directory, parsed.port, query["src"][0])
    except (TypeError, ValueError):
        return url
    return url + "&token=" + token if token else url


def component_version():
    """Lu par le Worker pour comparer le composant chargé/installé à celui du paquet."""
    return COMPONENT_VERSION


def _positive_int(value, maximum):
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if 0 < number <= maximum else 0


def source_options(fps=None, width=None, height=None):
    """Options normalisées d'une source : 0/None = valeur par défaut (10 i/s) ou taille d'origine."""
    try:
        rate = float(fps or 0)
    except (TypeError, ValueError):
        rate = 0.0
    rate = min(max(rate, 0.5), MAX_FPS) if rate > 0 else 10.0
    return {"fps": rate, "width": _positive_int(width, 4096), "height": _positive_int(height, 4096)}


def dependency_directory(root):
    """Compiled wheels must never be shared across Python minor versions."""
    import sys
    from pathlib import Path
    return Path(root) / "components" / "printer-link" / ("python-" + sys.implementation.cache_tag)


def check_dependencies(target):
    """Used in a fresh interpreter, so global packages/cached imports cannot lie."""
    import importlib
    import io
    import sys
    from pathlib import Path
    directory = Path(target).resolve()
    sys.path.insert(0, str(directory))
    errors = []
    names = ("paho.mqtt.client", "cryptography.x509", "_cffi_backend", "PIL.Image", "PIL._imaging")
    for name in names:
        try:
            module = importlib.import_module(name)
            origin = Path(module.__file__).resolve()
            if not origin.is_relative_to(directory):
                errors.append({"module": name, "error": "outside_worker_component"})
        except (ImportError, OSError, AttributeError) as error:
            errors.append({"module": name, "error": type(error).__name__})
    if not errors:
        try:
            image = importlib.import_module("PIL.Image")
            data = io.BytesIO()
            image.new("RGB", (8, 8)).save(data, format="JPEG")
            data.seek(0)
            with image.open(data) as decoded:
                decoded.load()
        except Exception as error:
            errors.append({"module": "PIL.JPEG", "error": type(error).__name__})
    return {"ok": not errors, "python_abi": sys.implementation.cache_tag, "errors": errors}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    # Do not steal a port from another local service on Windows.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class VideoRelay:
    """One listener per port; one native producer shared by every consumer."""
    def __init__(self, token_dir=None):
        # Jetons de lecture (1.3.0) : persistés dans token_dir (runtime/camera-relay) ; sans dossier, en mémoire.
        self.token_dir = Path(token_dir) if token_dir else None
        self.tokens = {}           # (port, source_id) -> jeton exigé en paramètre « token »
        self.lock = threading.RLock()
        self.servers = {}
        self.sources = {}
        self.options = {}          # (port, source_id) -> {"fps", "width", "height"}
        self.rendered = {}         # (port, source_id) -> (trame d'origine, trame servie) : une conversion par trame
        self.render_locks = {}     # (port, source_id) -> verrou de conversion (lecteurs multiples)
        self.resize_errors = {}    # (port, source_id) -> message si Pillow manque (repli : trame telle quelle)
        self.slots = threading.BoundedSemaphore(8)

    def source_options(self, port, source_id):
        with self.lock:
            return dict(self.options.get((int(port), source_id)) or source_options())

    def render(self, key, frame):
        """Trame servie pour ``key`` : redimensionnée (Pillow, JPEG qualité 80) si la source le demande.

        Le résultat est mis en cache par identité de trame : plusieurs lecteurs d'une même source
        partagent une seule conversion. Sans Pillow, la trame est transmise telle quelle et le
        message est conservé dans ``resize_errors``.
        """
        with self.lock:
            options = self.options.get(key) or source_options()
            cached = self.rendered.get(key)
            render_lock = self.render_locks.setdefault(key, threading.Lock())
        width, height = options["width"], options["height"]
        if not width and not height:
            return frame
        if cached and cached[0] is frame:
            return cached[1]
        with render_lock:
            with self.lock:
                cached = self.rendered.get(key)
            if cached and cached[0] is frame:
                return cached[1]
            try:
                import importlib
                import io
                Image = importlib.import_module("PIL.Image")
                with Image.open(io.BytesIO(frame)) as picture:
                    picture.load()
                    original = picture.size
                    if width and height:
                        picture.thumbnail((width, height))
                    elif width and picture.width > width:
                        picture = picture.resize((width, max(1, round(picture.height * width / picture.width))))
                    elif height and picture.height > height:
                        picture = picture.resize((max(1, round(picture.width * height / picture.height)), height))
                    if picture.size == original:
                        output = frame
                    else:
                        buffer = io.BytesIO()
                        picture.convert("RGB").save(buffer, format="JPEG", quality=80)
                        output = buffer.getvalue()
                with self.lock:
                    self.resize_errors.pop(key, None)
            except ImportError:
                output = frame
                with self.lock:
                    self.resize_errors[key] = RESIZE_UNAVAILABLE
            except Exception:
                output = frame  # trame indécodable : le lecteur la reçoit telle quelle, le lecteur natif la rejettera
            with self.lock:
                self.rendered[key] = (frame, output)
            return output

    def add(self, port, source_id, get_frame, *, fps=None, width=None, height=None):
        port = int(port)
        if not 1024 <= port <= 65535 or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", source_id):
            raise ValueError("Port ou identifiant du relais invalide.")
        token = source_token(self.token_dir, port, source_id, create=True) if self.token_dir else ""
        with self.lock:
            if not token:
                token = self.tokens.get((port, source_id)) or secrets.token_urlsafe(24)
            if port not in self.servers:
                hub = self

                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass  # No camera URLs or credentials in HTTP access logs.

                    def do_GET(self):
                        self.connection.settimeout(5)
                        host = self.headers.get("Host", "").lower()
                        if host not in {f"localhost:{port}", f"127.0.0.1:{port}"} or self.headers.get("Origin"):
                            self.send_error(403)
                            return
                        parsed = urlsplit(self.path)
                        query = parse_qs(parsed.query, keep_blank_values=True)
                        if (parsed.scheme or parsed.netloc or parsed.fragment or not {"src"} <= set(query) <= {"src", "token"}
                                or any(len(values) != 1 for values in query.values()) or parsed.path not in {"/api/stream.mjpeg", "/api/frame.jpeg"}):
                            self.send_error(404)
                            return
                        key = (port, query["src"][0])
                        with hub.lock:
                            callback = hub.sources.get(key)
                            expected = hub.tokens.get(key, "")
                        if not callback:
                            self.send_error(404)
                            return
                        supplied = (query.get("token") or [""])[0]
                        # Jeton de lecture exigé (comparaison à temps constant) : un processus local quelconque ne lit
                        # plus la caméra sans le lien copié depuis le site.
                        if not expected or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
                            self.send_error(403, TOKEN_REQUIRED_MESSAGE)
                            return
                        if not hub.slots.acquire(blocking=False):
                            self.send_error(503)
                            return
                        try:
                            frame, stamp = callback()
                            if not frame or time.time() - stamp >= 8:
                                self.send_error(503)
                                return
                            self.send_response(200)
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("X-Content-Type-Options", "nosniff")
                            if parsed.path == "/api/frame.jpeg":
                                frame = hub.render(key, frame)
                                self.send_header("Content-Type", "image/jpeg")
                                self.send_header("Content-Length", str(len(frame)))
                                self.end_headers()
                                self.wfile.write(frame)
                                return
                            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=alpine-frame")
                            self.end_headers()
                            previous = 0
                            next_due = 0.0
                            while True:
                                with hub.lock:
                                    if hub.sources.get(key) is not callback:
                                        break
                                    period = 1.0 / (hub.options.get(key) or source_options())["fps"]
                                frame, stamp = callback()
                                if not frame or time.time() - stamp >= 8:
                                    break
                                now = time.monotonic()
                                if stamp != previous and now >= next_due:
                                    frame = hub.render(key, frame)
                                    self.wfile.write(b"--alpine-frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                                    self.wfile.flush()
                                    previous = stamp
                                    # Échéance suivante sur la grille de la cadence : le temps d'envoi et le réveil
                                    # tardif d'un tour ne s'ajoutent plus à chaque période. En retard, pas de rafale.
                                    next_due = next_due + period if next_due + period > now else now
                                    continue
                                # Avant l'échéance : attendre jusqu'à elle. Échéance passée : la trame suivante de
                                # la source est guettée de près (2 ms). Borne de 0,1 s pour suivre un arrêt.
                                time.sleep(min(max(next_due - now, .002), .1))
                        except (OSError, ValueError):
                            pass
                        finally:
                            hub.slots.release()

                server = _Server(("127.0.0.1", port), Handler)
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .1}, name=f"equipment-video-{port}", daemon=True)
                self.servers[port] = (server, thread)
                thread.start()
            key = (port, source_id)
            self.sources[key] = get_frame
            self.tokens[key] = token
            self.options[key] = source_options(fps, width, height)
            self.rendered.pop(key, None)
            self.resize_errors.pop(key, None)
        return relay_url(port, source_id, token)

    def token(self, port, source_id):
        """Jeton exigé pour lire cette source ("" si elle n'est pas servie)."""
        with self.lock:
            return self.tokens.get((int(port), source_id), "")

    def url(self, port, source_id, frame=False):
        """Adresse complète (jeton compris) d'une source servie, "" sinon : seule forme à donner à un lecteur local."""
        token = self.token(port, source_id)
        return relay_url(port, source_id, token, frame) if token else ""

    def remove(self, port, source_id):
        port = int(port)
        with self.lock:
            key = (port, source_id)
            self.sources.pop(key, None)
            for table in (self.options, self.rendered, self.render_locks, self.resize_errors, self.tokens):
                table.pop(key, None)
            pair = self.servers.pop(port, None) if not any(key[0] == port for key in self.sources) else None
        if pair:
            server, thread = pair
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def close(self):
        for port, source_id in list(self.sources):
            self.remove(port, source_id)


PINNED_REQUIREMENTS = ("paho-mqtt==2.1.0", "pyserial==3.5", "cryptography>=43.0.3", "Pillow==11.3.0")
# Dépendances posées seules, à la demande d'un connecteur : le reste du relais n'est jamais
# recopié (un module chargé par le relais en cours ne peut pas être écrasé sous Windows).
# Agent 1.50.4 : capture par Media Foundation (WinRT) des caméras USB, Windows seulement (wheels purs, ~6 Mo).
USB_CAMERA_WINRT_REQUIREMENTS = tuple(
    name + "==3.2.1" for name in ("winrt-runtime", "winrt-Windows.Foundation", "winrt-Windows.Foundation.Collections",
                                  "winrt-Windows.Devices.Enumeration", "winrt-Windows.Media", "winrt-Windows.Media.Capture",
                                  "winrt-Windows.Media.Capture.Frames", "winrt-Windows.Media.MediaProperties",
                                  "winrt-Windows.Media.Devices", "winrt-Windows.Graphics.Imaging", "winrt-Windows.Storage.Streams")
) if __import__("os").name == "nt" else ()
EXTRA_PROFILES = {
    "serial": {"requirements": ("pyserial==3.5",), "label": "de la liaison série GRBL (pyserial)"},
    "usb_camera": {"requirements": ("opencv-python-headless==4.11.0.86",) + USB_CAMERA_WINRT_REQUIREMENTS,
                   "label": "de capture vidéo USB (OpenCV et Media Foundation, sans GPU)"},
}


def same_file(source, target):
    """Même contenu (taille, puis SHA-256) : rien à recopier, donc rien à écraser."""
    import hashlib
    from pathlib import Path
    source, target = Path(source), Path(target)
    try:
        if not target.is_file() or target.stat().st_size != source.stat().st_size:
            return False
        digests = []
        for path in (source, target):
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digests.append(digest.digest())
        return digests[0] == digests[1]
    except OSError:
        return False


def install_dependencies(target, requirements=PINNED_REQUIREMENTS, label="réseau et JPEG"):
    """Fixed dependencies only. Called by the durable, tracked installer runner.

    Un fichier déjà identique dans le dossier de dépendances n'est pas recopié : le relais
    vidéo en cours a ses modules chargés (PIL, paho…) et Windows refuse de les écraser ; ils
    n'ont de toute façon pas changé. Un écrasement encore refusé donne un message clair.
    """
    import os
    import subprocess
    import sys
    import shutil
    import uuid
    from pathlib import Path

    destination = Path(target).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # pip moves package directories from its temporary directory. On Windows,
    # that can retain ACLs that the normal Worker account cannot read. Copying
    # into newly created directories inherits the protected Worker-root ACLs.
    staging = destination.parent / ("dependency-stage-" + uuid.uuid4().hex)
    staging.mkdir()

    def progress(value, message):
        print("ALPINE_PROGRESS=" + json.dumps({"progress": value, "message": message}, ensure_ascii=True), flush=True)

    progress(15, f"Téléchargement/vérification des dépendances {label}…")
    environment = dict(os.environ, PIP_NO_INPUT="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    try:
        process = subprocess.Popen([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--timeout", "20", "--retries", "1", "--target", str(staging), *requirements], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in process.stdout:
            if "Downloading" in line or "Using cached" in line:
                progress(30, f"Téléchargement des dépendances {label} en cours…")
            elif "Installing collected" in line:
                progress(50, "Installation des dépendances dans le dossier de données du Worker…")
        if process.wait():
            # pip output may contain a private package-index URL; never expose it.
            raise SystemExit("Installation des dépendances impossible. Vérifie Internet, Python et l’espace disque du Worker.")
        destination.mkdir(parents=True, exist_ok=True)
        copied = skipped = 0
        # Dossiers de paquets créés par cette copie : si elle échoue en route, ils sont retirés,
        # sinon un « serial/ » vide passerait pour un paquet installé (paquet namespace) et la
        # liaison série échouerait avec AttributeError tant que personne ne le supprime.
        created = []
        try:
            for path in staging.rglob("*"):
                relative = path.relative_to(staging)
                if path.is_symlink():
                    raise ValueError("Lien symbolique inattendu dans les dépendances.")
                target_path = destination / relative
                if path.is_dir():
                    if not target_path.exists():
                        created.append(target_path)
                    target_path.mkdir(parents=True, exist_ok=True)
                elif path.is_file():
                    if not target_path.parent.exists():
                        created.append(target_path.parent)
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    if same_file(path, target_path):
                        skipped += 1
                        continue
                    try:
                        shutil.copyfile(path, target_path)
                    except PermissionError:
                        raise SystemExit(
                            f"Le fichier {relative.as_posix()} est utilisé par le relais vidéo en cours : arrête-le "
                            "(Déconnecter la caméra) ou Redémarre le Worker, relance Installer, puis reconnecte la caméra."
                        ) from None
                    copied += 1
        except BaseException:
            for folder in sorted(set(created), key=lambda item: len(str(item)), reverse=True):
                if folder.resolve() != destination.resolve() and destination.resolve() in folder.resolve().parents:
                    shutil.rmtree(folder, ignore_errors=True)
            raise
        progress(60, f"Dépendances copiées : {copied} fichier(s) nouveau(x), {skipped} déjà à jour.")
    finally:
        # Exactly the generated staging directory, never a user-selected path.
        if staging.parent.resolve() == destination.parent.resolve() and staging.name.startswith("dependency-stage-"):
            shutil.rmtree(staging, ignore_errors=True)
    progress(65, "Dépendances installées ; validation des composants…")


def install_profile(target, profile):
    """Une seule famille de dépendances (liaison série, capture USB), jamais le reste du relais."""
    chosen = EXTRA_PROFILES.get(str(profile or ""))
    if not chosen:
        raise SystemExit("Profil de dépendances inconnu.")
    install_dependencies(target, chosen["requirements"], chosen["label"])


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--install-dependencies")
    group.add_argument("--install-profile")
    group.add_argument("--check-dependencies")
    parser.add_argument("--profile", default="")
    options = parser.parse_args()
    if options.check_dependencies:
        print(json.dumps(check_dependencies(options.check_dependencies)))
    elif options.install_profile:
        install_profile(options.install_profile, options.profile)
    else:
        install_dependencies(options.install_dependencies)
