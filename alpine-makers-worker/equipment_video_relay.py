"""Small P1S JPEG -> MJPEG relay, bound to the Worker loopback only.

The P1S reader remains equipment_runtime.Device: this component does not assume
that a generic RTSP server understands the proprietary P1S TLS protocol.
"""
import json
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

COMPONENT_VERSION = "1.0.0"


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
    def __init__(self):
        self.lock = threading.RLock()
        self.servers = {}
        self.sources = {}
        self.slots = threading.BoundedSemaphore(8)

    def add(self, port, source_id, get_frame):
        port = int(port)
        if not 1024 <= port <= 65535 or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", source_id):
            raise ValueError("Port ou identifiant du relais invalide.")
        with self.lock:
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
                        if parsed.scheme or parsed.netloc or parsed.fragment or set(query) != {"src"} or len(query["src"]) != 1 or parsed.path not in {"/api/stream.mjpeg", "/api/frame.jpeg"}:
                            self.send_error(404)
                            return
                        key = (port, query["src"][0])
                        with hub.lock:
                            callback = hub.sources.get(key)
                        if not callback:
                            self.send_error(404)
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
                                self.send_header("Content-Type", "image/jpeg")
                                self.send_header("Content-Length", str(len(frame)))
                                self.end_headers()
                                self.wfile.write(frame)
                                return
                            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=alpine-frame")
                            self.end_headers()
                            previous = 0
                            while True:
                                with hub.lock:
                                    if hub.sources.get(key) is not callback:
                                        break
                                frame, stamp = callback()
                                if not frame or time.time() - stamp >= 8:
                                    break
                                if stamp != previous:
                                    self.wfile.write(b"--alpine-frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                                    self.wfile.flush()
                                    previous = stamp
                                time.sleep(.1)
                        except (OSError, ValueError):
                            pass
                        finally:
                            hub.slots.release()

                server = _Server(("127.0.0.1", port), Handler)
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .1}, name=f"equipment-video-{port}", daemon=True)
                self.servers[port] = (server, thread)
                thread.start()
            self.sources[(port, source_id)] = get_frame
        return f"http://127.0.0.1:{port}/api/stream.mjpeg?src={source_id}"

    def remove(self, port, source_id):
        with self.lock:
            self.sources.pop((port, source_id), None)
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
EXTRA_PROFILES = {
    "serial": {"requirements": ("pyserial==3.5",), "label": "de la liaison série GRBL (pyserial)"},
    "usb_camera": {"requirements": ("opencv-python-headless==4.11.0.86",), "label": "de capture vidéo USB (OpenCV, sans GPU)"},
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
