"""A verified Python 3.12 for engine venvs; never alters system Python.

Pinned release metadata (queried 2026-09-08, no runtime 'latest' lookup):
https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/20260901
https://github.com/astral-sh/python-build-standalone/releases/tag/20260901
The install_only_stripped distributions are relocatable runtime archives.
"""
import hashlib
import json
import os
import platform
import posixpath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

if __package__:
    from .transfer_progress import TransferProgress, progress
else:
    from transfer_progress import TransferProgress, progress
if __package__ and "." in __package__:
    from ..installer_process import installation_commit
    from ..job_state import atomic_json
    from ..safety import is_link, validate_archive
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from installer_process import installation_commit
    from job_state import atomic_json
    from safety import is_link, validate_archive


RELEASE = "20260901"
PYTHON_VERSION = "3.12.14"
DOWNLOAD_BASE = "https://github.com/astral-sh/python-build-standalone/releases/download/" + RELEASE + "/"
PYTHON_BUILDS = {
    "windows-x86_64": {
        "asset": "cpython-3.12.14%2B20260901-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
        "sha256": "7c45c9622400d578709a9b2cddbe8124cc21d382409d9f13406d706d28e31b14",
        "size": 21980728, "executable": "python.exe",
    },
    "linux-x86_64": {
        "asset": "cpython-3.12.14%2B20260901-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
        "sha256": "72748da13197c1fb161e3afeef20a6a385ff24f2165e6e2758e47008e7faba4c",
        "size": 34143368, "executable": "bin/python3.12",
    },
}
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_EXTRACTED_BYTES = 768 * 1024 * 1024
MAX_ARCHIVE_FILES = 40000
MARKER = ".alpine-python-runtime.json"


class PythonRuntimeError(RuntimeError):
    pass


def _platform_key():
    machine = platform.machine().casefold()
    if machine not in {"amd64", "x86_64"}:
        raise PythonRuntimeError("Le Python IA isolé est disponible uniquement pour Windows x64 et Linux x86_64 GNU.")
    system = platform.system()
    if system == "Windows":
        return "windows-x86_64"
    if system == "Linux" and platform.libc_ver()[0].casefold() in {"glibc", "gnu libc"}:
        return "linux-x86_64"
    raise PythonRuntimeError("Système non pris en charge : le Python IA isolé nécessite Windows x64 ou Linux x86_64 avec glibc.")


def _checked_root(value):
    root = Path(os.path.abspath(os.path.expanduser(str(value))))
    broad = {Path(root.anchor), Path.home(), Path.home().parent}
    broad.update(Path(os.path.abspath(os.environ[name])) for name in ("PUBLIC", "PROGRAMDATA", "WINDIR", "PROGRAMFILES", "USERPROFILE") if os.environ.get(name))
    if root in broad or str(root).startswith(("\\\\", "//")) or not root.is_dir():
        raise PythonRuntimeError("Le Python IA doit rester dans un dossier Worker existant et précis.")
    for candidate in (root, *root.parents, root / "runtime"):
        if is_link(candidate):
            raise PythonRuntimeError("Un dossier du runtime Python est un lien ou une jonction ; installation refusée.")
    return root


def probe_python(executable):
    """Read-only isolated probe; never installs pip or changes a global Python."""
    candidate = Path(executable)
    if not candidate.is_absolute() or str(candidate).startswith(("\\\\", "//")):
        return False
    try:
        if not candidate.is_file() or any(part.casefold() == "windowsapps" for part in candidate.parts):
            return False  # Do not invoke a Windows Store alias.
        # A system python symlink is fine after resolving it to a local file.
        candidate = candidate.resolve(strict=True)
        if str(candidate).startswith(("\\\\", "//")):
            return False
        environment = {key: value for key, value in os.environ.items() if key.upper() not in {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV"}}
        result = subprocess.run(
            [str(candidate), "-I", "-B", "-c", "import sys,struct,json,venv,ensurepip,ssl,sqlite3; print(json.dumps({'version':list(sys.version_info[:2]),'bits':struct.calcsize('P')*8,'implementation':sys.implementation.name}))"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False,
            timeout=20, env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        data = json.loads(result.stdout) if result.returncode == 0 else {}
        return data.get("version") == [3, 12] and data.get("bits") == 64 and data.get("implementation") == "cpython"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def _existing_candidates(key):
    candidates = [sys.executable]
    if key == "windows-x86_64":
        for variable in ("LOCALAPPDATA", "PROGRAMFILES"):
            value = os.environ.get(variable)
            if value:
                base = Path(value)
                candidates.append(base / ("Programs/Python/Python312/python.exe" if variable == "LOCALAPPDATA" else "Python312/python.exe"))
    else:
        candidates.extend(("/usr/bin/python3.12", "/usr/local/bin/python3.12"))
    # No py launcher or shell/PATH scan: neither Store activation nor automatic
    # Python installation should occur as a side-effect of looking for 3.12.
    seen = set()
    for value in candidates:
        candidate = Path(value)
        name = str(candidate).casefold() if os.name == "nt" else str(candidate)
        if name not in seen:
            seen.add(name)
            yield candidate


class _ReleaseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.hostname not in {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}:
            raise PythonRuntimeError("La redirection du téléchargement Python n’est pas une destination officielle autorisée.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(spec, destination):
    digest, received = hashlib.sha256(), 0
    transfer = TransferProgress("Téléchargement du Python 3.12 isolé pour les moteurs IA…", start=5, span=15)
    request = urllib.request.Request(DOWNLOAD_BASE + spec["asset"], headers={"User-Agent": "AlpineWorker-PythonRuntime/1"})
    try:
        with urllib.request.build_opener(_ReleaseRedirects()).open(request, timeout=45) as source, destination.open("xb") as output:
            advertised = int(source.headers.get("Content-Length") or 0)
            if advertised and advertised != spec["size"]:
                raise PythonRuntimeError("La taille du runtime Python distant ne correspond pas au paquet épinglé.")
            if spec["size"] > MAX_ARCHIVE_BYTES:
                raise PythonRuntimeError("Le paquet Python dépasse la limite autorisée.")
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > min(MAX_ARCHIVE_BYTES, spec["size"]):
                    raise PythonRuntimeError("Le téléchargement du runtime Python dépasse la taille autorisée.")
                output.write(chunk)
                digest.update(chunk)
                transfer.update(received, spec["size"])
            output.flush(); os.fsync(output.fileno())
        transfer.update(received, spec["size"], force=True)
        if received != spec["size"] or digest.hexdigest() != spec["sha256"]:
            raise PythonRuntimeError("Téléchargement Python incomplet ou empreinte SHA-256 incorrecte ; aucun runtime installé.")
    except urllib.error.HTTPError as error:
        error.close()
        raise PythonRuntimeError(f"Téléchargement du Python isolé refusé (HTTP {error.code}). Réessaie sans modifier le Python du PC.") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise PythonRuntimeError("Téléchargement du Python isolé interrompu ; aucune installation existante n’a été remplacée.") from error


def _member_name(name):
    # Cross-platform validation also rejects Windows drive/ADS/reserved names on Linux.
    if not isinstance(name, str) or "\\" in name or "\0" in name or name.startswith("/") or PureWindowsPath(name).drive:
        raise PythonRuntimeError("Chemin non autorisé dans l’archive Python.")
    parts = name.rstrip("/").split("/")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    if not parts or parts[0] != "python" or any(not part or part in {".", ".."} or ":" in part or part.endswith((" ", ".")) or part.split(".")[0].casefold() in reserved for part in parts):
        raise PythonRuntimeError("L’archive Python contient un chemin hors du runtime attendu.")
    return "/".join(parts)


def _extract_tar(path, destination):
    with tarfile.open(path, "r:*") as archive:
        members, folded, total = {}, set(), 0
        for item in archive:
            name = _member_name(item.name)
            if name.casefold() in folded or len(members) >= MAX_ARCHIVE_FILES:
                raise PythonRuntimeError("L’archive Python contient des doublons ou trop de fichiers.")
            if not (item.isfile() or item.isdir() or item.issym() or item.islnk()) or item.size < 0:
                raise PythonRuntimeError("Fichier spécial non autorisé dans l’archive Python.")
            total += item.size
            if total > MAX_EXTRACTED_BYTES:
                raise PythonRuntimeError("L’archive Python décompressée dépasse la taille autorisée.")
            members[name] = item
            folded.add(name.casefold())

        def regular_target(name, visited=None):
            visited = set() if visited is None else visited
            if name in visited or len(visited) > 32:
                raise PythonRuntimeError("Cycle de liens dans l’archive Python.")
            visited.add(name)
            item = members.get(name)
            if item is None:
                raise PythonRuntimeError("Cible de lien Python absente de l’archive.")
            if item.isfile():
                return item
            if not (item.issym() or item.islnk()):
                raise PythonRuntimeError("Un alias Python ne peut pas pointer vers un dossier.")
            link = item.linkname
            if not link or "\\" in link or "\0" in link or link.startswith("/") or PureWindowsPath(link).drive:
                raise PythonRuntimeError("Lien absolu ou externe interdit dans l’archive Python.")
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), link)) if item.issym() else posixpath.normpath(link)
            target = _member_name(target)
            return regular_target(target, visited)

        resolved = {}
        for name, item in members.items():
            for parent in PurePosixPath(name).parents:
                parent_item = members.get(str(parent))
                if parent_item is not None and not parent_item.isdir():
                    raise PythonRuntimeError("Un parent de fichier Python est un lien ou un fichier.")
            if not item.isdir():
                resolved[name] = regular_target(name)
        expanded_size = sum(item.size for item in resolved.values())
        if expanded_size > MAX_EXTRACTED_BYTES:
            raise PythonRuntimeError("Les alias de l’archive Python dépassent la taille autorisée.")
        # All members/links are checked before writes. Materialize file aliases
        # instead of creating filesystem links or requiring Windows privileges.
        for name, item in members.items():
            target = destination.joinpath(*name.split("/"))
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source_info = resolved[name]
            with archive.extractfile(source_info) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            os.chmod(target, 0o755 if source_info.mode & 0o111 else 0o644)


def safe_extract(path, destination):
    destination = Path(destination)
    if is_link(destination) or not destination.is_dir() or next(destination.iterdir(), None) is not None:
        raise PythonRuntimeError("L’extraction Python nécessite un dossier temporaire neuf et sans lien.")
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            validate_archive(archive, destination, MAX_EXTRACTED_BYTES, MAX_ARCHIVE_FILES)
            for item in archive.infolist():
                _member_name(item.filename)
                mode = item.external_attr >> 16
                if mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise PythonRuntimeError("Fichier spécial non autorisé dans l’archive Python.")
            archive.extractall(destination)
        return
    _extract_tar(path, destination)


def _private_runtime(target, spec):
    executable = target / spec["executable"]
    for entry in (target, target / MARKER, executable, *executable.parents):
        if entry == target.parent:
            break
        if is_link(entry):
            raise PythonRuntimeError("Le runtime Python existant est un lien ou une jonction ; il a été conservé.")
    if not target.exists():
        return None
    if not target.is_dir():
        raise PythonRuntimeError("Le chemin du Python privé est occupé ; aucune donnée remplacée.")
    try:
        marker_path = target / MARKER
        if marker_path.stat().st_size > 8192:
            raise ValueError()
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("owner") != "Alpine Makers" or marker.get("version") != PYTHON_VERSION or marker.get("sha256") != spec["sha256"]:
            raise ValueError()
    except (OSError, ValueError, AttributeError) as error:
        raise PythonRuntimeError("Un dossier Python 3.12 non reconnu ou incomplet existe déjà. Il est conservé ; vérifie-le avant de relancer l’installation.") from error
    if not probe_python(executable):
        raise PythonRuntimeError("Le Python IA privé existant n’est pas opérationnel. Aucun environnement installé n’a été remplacé.")
    return executable


def ensure_engine_python(worker_root):
    """Return a probed 64-bit CPython 3.12 executable for creating engine venvs.

    Runs synchronously inside the existing controlled installer process; network
    and probe subprocesses stay in its cancellation scope. An interrupted stage
    never replaces an earlier runtime or engine virtual environment.
    """
    key, root = _platform_key(), _checked_root(worker_root)
    spec = PYTHON_BUILDS[key]
    runtime, target = root / "runtime", root / "runtime" / "python-3.12"
    existing = _private_runtime(target, spec)
    if existing:
        return existing
    for candidate in _existing_candidates(key):
        if probe_python(candidate):
            progress(4, "Python 3.12 existant vérifié ; aucun Python système modifié.")
            return candidate.resolve()
    runtime.mkdir(exist_ok=True)
    lock = runtime / ".python-3.12-install.lock"
    if is_link(lock):
        raise PythonRuntimeError("Le verrou du runtime Python est un lien ; installation refusée.")
    with installation_commit(lock, blocking=False) as acquired:
        if not acquired:
            raise PythonRuntimeError("La préparation du Python IA est déjà en cours sur ce Worker. Attends sa fin.")
        _checked_root(root)
        existing = _private_runtime(target, spec)
        if existing:
            return existing
        # Temp is under runtime so final rename is on the same filesystem.
        with tempfile.TemporaryDirectory(prefix=".python-3.12-stage-", dir=runtime) as temporary:
            stage = Path(temporary)
            archive, unpacked = stage / "python.tar.gz", stage / "unpacked"
            _download(spec, archive)
            unpacked.mkdir()
            progress(21, "Vérification et extraction du Python IA isolé…")
            safe_extract(archive, unpacked)
            prepared = unpacked / "python"
            if not probe_python(prepared / spec["executable"]):
                raise PythonRuntimeError("Le Python téléchargé ne passe pas la vérification CPython 3.12 64 bits ; aucun runtime activé.")
            atomic_json(prepared / MARKER, {"owner": "Alpine Makers", "version": PYTHON_VERSION, "release": RELEASE,
                                         "sha256": spec["sha256"], "source": DOWNLOAD_BASE + spec["asset"]})
            _checked_root(root)
            if target.exists() or is_link(target):
                raise PythonRuntimeError("Le dossier Python est apparu pendant l’installation ; il n’a pas été remplacé.")
            with installation_commit():
                os.rename(prepared, target)
            progress(24, "Python 3.12 isolé prêt ; le Python du PC reste inchangé.")
            return target / spec["executable"]
