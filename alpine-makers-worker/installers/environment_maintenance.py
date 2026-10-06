"""Maintenance des programmes dont le Worker dépend, sans toucher à son fonctionnement.

Lancé par l'agent après « Mettre à jour Worker » (ou sur demande) dans le même périmètre
contrôlé que les autres installateurs (installer_process.py : groupe privé, priorité basse,
annulation, progression). Deux étapes, rapportées séparément et jamais fatales l'une pour l'autre :

1. le Python 3.12 isolé des moteurs (runtime/python-3.12) est remplacé par la version épinglée
   dans python_runtime.py quand elle a changé — uniquement si l'agent a confirmé qu'aucun moteur
   ne tourne, par un échange atomique avec retour arrière si un environnement ne répond plus ;
2. pip, wheel et setuptools de chaque environnement géré (components/<nom>/.venv portant
   .alpine-managed.json « Alpine Makers ») sont mis à niveau.

Quand l'échange du Python est reporté (moteur en cours, composant en cours d'installation, fichiers encore
utilisés), la demande est conservée avec « python_only » : l'agent la réessaie plus tard. Un échange interrompu
(ancien dossier renommé, nouveau absent) est rétabli au passage suivant.

Jamais modifiés : le Python du PC, les révisions épinglées des moteurs, PyTorch, les modèles et
les données utilisateur (ce sont les actions « Mettre à jour » des composants, avec leurs contrôles).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Même convention que component_installer : les voisins du dossier installers/ d'abord (paquet « installers »
# quand l'agent tourne en script : ``python agent.py``), puis les modules du dossier worker_agent/.
if __package__:
    from . import python_runtime
    from .transfer_progress import progress
else:
    import python_runtime
    from transfer_progress import progress

if __package__ and "." in __package__:
    from ..installer_process import installation_commit
    from ..job_state import atomic_json
    from ..safety import is_link
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from installer_process import installation_commit
    from job_state import atomic_json
    from safety import is_link

REQUEST_MARKER = ".environment-maintenance-request.json"
REPORT_FILE = "environment-maintenance.json"
RUNTIME_DIR = "python-3.12"
PREVIOUS_PREFIX = "python-3.12-previous-"
MANAGED_COMPONENTS = ("comfyui", "hunyuan3d", "hunyuan3d-21", "open3d-lab", "laser-engine", "fire-watch")
PIP_TOOLS = ("pip", "wheel", "setuptools")
PIP_TIMEOUT = 900
PROBE_TIMEOUT = 60
PROBE_CODE = "import sys, ssl, sqlite3"
STATUS_UPDATED, STATUS_CURRENT, STATUS_DEFERRED, STATUS_FAILED, STATUS_ABSENT, STATUS_SKIPPED = (
    "mis à jour", "à jour", "reporté", "échec", "absent", "ignoré")
DEFERRED_ENGINE_RUNNING = "reporté : moteur en cours d'exécution"
DEFERRED_FILES_IN_USE = "reporté : fichiers du Python isolé encore utilisés par un moteur"
DEFERRED_COMPONENT_BUSY = "reporté : installation d'un composant en cours"
RESTORED_NOTE = "ancien Python rétabli après une interruption"
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")


def request_path(root):
    return Path(root) / "runtime" / REQUEST_MARKER


def report_path(root):
    return Path(root) / "runtime" / REPORT_FILE


def write_request(root, from_version, to_version, *, requested_at=None, python_only=False):
    """Demande de maintenance posée par download_agent_update ; runtime/ survit à la mise à jour.
    « python_only » : les pip sont faits, seul l'échange du Python isolé attend un moment sûr."""
    request = {"requested_at": float(requested_at) if isinstance(requested_at, (int, float)) else time.time(),
               "from_version": str(from_version or ""), "to_version": str(to_version or "")}
    if python_only:
        request["python_only"] = True
    atomic_json(request_path(root), request)


def _read(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_request(root):
    return _read(request_path(root))


def read_report(root):
    return _read(report_path(root))


def write_report(root, report):
    atomic_json(report_path(root), report)
    return report


def failure_report(root, agent_version, status, message, *, clear_request=False):
    """Rapport posé par l'agent lui-même quand le script n'a pas pu rendre le sien (lancement impossible,
    maintenance reportée au-delà du délai)."""
    report = {"finished_at": time.time(), "agent_version": str(agent_version or ""), "status": status,
              "error": str(message or "")[:500],
              "python": {"before": runtime_marker_version(root), "after": runtime_marker_version(root),
                         "status": status, "error": str(message or "")[:500]},
              "environments": [], "skipped": []}
    write_report(root, report)
    if clear_request:
        request_path(root).unlink(missing_ok=True)
    return report


def _marker_version(marker):
    """Version d'un marqueur Alpine Makers, uniquement sous la forme N.N.N : ce fichier n'est jamais cru pour
    construire un chemin (un « ../ » dans la version déplacerait ou supprimerait hors de runtime/)."""
    if not isinstance(marker, dict) or marker.get("owner") != "Alpine Makers":
        return None
    version = str(marker.get("version") or "")
    return version if VERSION_PATTERN.fullmatch(version) else None


def runtime_marker_version(root):
    return _marker_version(_read(Path(root) / "runtime" / RUNTIME_DIR / python_runtime.MARKER))


def python_in(venv):
    return Path(venv) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _python_inside(python, root):
    """Le Python d'un .venv créé par ``python -m venv`` est un lien sous Linux (python → python3.12 → runtime isolé) :
    accepté tant que sa cible reste un fichier du dossier Worker. Un lien vers l'extérieur n'est jamais exécuté."""
    try:
        if not python.is_file():
            return False
        if not is_link(python):
            return True
        return python.resolve().is_relative_to(Path(root).resolve())
    except OSError:
        return False


def managed_environments(root):
    """(composant, python du .venv) des seuls environnements créés par Alpine Makers ; dossiers jamais des liens."""
    found = []
    for component in MANAGED_COMPONENTS:
        component_root = Path(root) / "components" / component
        marker = _read(component_root / ".alpine-managed.json")
        if not marker or marker.get("owner") != "Alpine Makers":
            continue
        venv = component_root / ".venv"
        python = python_in(venv)
        if any(is_link(entry) for entry in (component_root, venv)) or not _python_inside(python, root):
            continue
        found.append((component, python))
    return found


def _child_environment():
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV"}}
    environment.update(PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1", PYTHONUTF8="1")
    return environment


def _process_options(low_priority=True):
    """Même contrat que component_installer : fenêtre cachée et priorité basse, pour laisser le
    processeur à un envoi G-code ou à l'agent lui-même."""
    options = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0), "env": _child_environment(),
               "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True, "encoding": "utf-8", "errors": "replace"}
    if low_priority:
        if os.name == "nt":
            options["creationflags"] |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        else:
            options["preexec_fn"] = lambda: os.nice(10)
    return options


def tool_versions(python):
    """Versions installées de pip/wheel/setuptools, lues via importlib.metadata du Python de l'environnement."""
    code = ("import importlib.metadata as m, json\n"
            "def v(n):\n"
            "    try: return m.version(n)\n"
            "    except m.PackageNotFoundError: return None\n"
            f"print(json.dumps({{n: v(n) for n in {list(PIP_TOOLS)!r}}}))")
    try:
        result = subprocess.run([str(python), "-I", "-c", code], timeout=PROBE_TIMEOUT, check=False, **_process_options())
        data = json.loads(result.stdout.strip().splitlines()[-1]) if result.returncode == 0 and result.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return {name: (str(data[name]) if data.get(name) else None) for name in PIP_TOOLS} if isinstance(data, dict) else None


def probe_environment(python):
    """Vérification en lecture seule qu'un environnement démarre encore sur le Python en place."""
    try:
        result = subprocess.run([str(python), "-I", "-c", PROBE_CODE], timeout=PROBE_TIMEOUT, check=False, **_process_options())
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _tail(text, limit=400):
    lines = [line for line in str(text or "").strip().splitlines() if line.strip()]
    return " ".join(lines[-3:])[-limit:]


def upgrade_environment(component, python):
    """pip/wheel/setuptools d'un .venv : la mise à niveau ne touche pas un moteur qui tournerait avec ce venv."""
    entry = {"component": component, "pip_before": None, "pip_after": None, "versions_before": None,
             "versions_after": None, "status": STATUS_FAILED, "error": ""}
    before = tool_versions(python)
    entry["versions_before"] = before
    entry["pip_before"] = (before or {}).get("pip")
    if before is None:
        entry["error"] = "Le Python de l'environnement ne répond pas ; aucune mise à niveau tentée."
        return entry
    try:
        result = subprocess.run([str(python), "-m", "pip", "install", "--upgrade", *PIP_TOOLS],
                                timeout=PIP_TIMEOUT, check=False, **_process_options())
    except subprocess.TimeoutExpired:
        entry["error"] = f"pip n'a pas terminé en {PIP_TIMEOUT // 60} minutes ; environnement laissé tel quel."
        return entry
    except OSError as error:
        entry["error"] = f"Lancement de pip impossible ({type(error).__name__})."
        return entry
    after = tool_versions(python)
    entry["versions_after"] = after
    entry["pip_after"] = (after or {}).get("pip")
    if result.returncode != 0:
        entry["error"] = "pip a échoué : " + (_tail(result.stderr) or _tail(result.stdout) or f"code {result.returncode}")
        return entry
    if after is None:
        entry["error"] = "pip a terminé mais l'environnement ne répond plus à la lecture des versions."
        return entry
    entry["status"] = STATUS_UPDATED if after != before else STATUS_CURRENT
    return entry


def _previous_runtimes(runtime):
    found = []
    for entry in runtime.iterdir():
        try:
            if entry.is_dir() and entry.name.startswith(PREVIOUS_PREFIX) and not is_link(entry):
                found.append((entry.stat().st_mtime, entry))
        except OSError:
            continue  # entrée illisible ou verrouillée : ignorée, jamais fatale
    return [entry for _, entry in sorted(found, key=lambda item: item[0])]


def _previous_path(runtime, version):
    """Emplacement de l'ancien Python : toujours un enfant direct de runtime/ au nom attendu (version N.N.N validée)."""
    if not VERSION_PATTERN.fullmatch(str(version or "")):
        raise python_runtime.PythonRuntimeError("Version du Python isolé non reconnue ; échange refusé.")
    previous = runtime / (PREVIOUS_PREFIX + version)
    if previous.parent != runtime or not previous.name.startswith(PREVIOUS_PREFIX) or previous.name == PREVIOUS_PREFIX:
        raise python_runtime.PythonRuntimeError("Emplacement de l'ancien Python hors de runtime/ ; échange refusé.")
    return previous


def restore_interrupted_swap(root):
    """Échange interrompu (agent arrêté entre les deux renommages) : runtime/python-3.12 absent et un seul dossier
    « previous » valable → il redevient le Python courant. Renvoie la version rétablie, sinon None."""
    root = Path(root)
    runtime, target = root / "runtime", root / "runtime" / RUNTIME_DIR
    if target.exists() or is_link(target) or not runtime.is_dir():
        return None
    candidates = []
    for entry in _previous_runtimes(runtime):
        version = _marker_version(_read(entry / python_runtime.MARKER))
        if version and entry.name == PREVIOUS_PREFIX + version:
            candidates.append((version, entry))
    if len(candidates) != 1:
        return None
    version, previous = candidates[0]
    with installation_commit():
        os.rename(previous, target)
    return version


def refresh_python_runtime(root, environments, *, allowed, busy_components=()):
    """Remplace runtime/python-3.12 par la version épinglée. Le dossier courant devient
    python-3.12-previous-<ancienne> ; si un environnement géré ne démarre plus, l'échange est annulé.

    Reporté (demande conservée par run(), l'agent réessaie) quand un moteur tourne, quand un composant est en cours
    d'installation (son .venv ne pourrait pas être sondé) ou quand les fichiers du Python sont encore utilisés."""
    root = Path(root)
    runtime, target = root / "runtime", root / "runtime" / RUNTIME_DIR
    restored = None
    try:
        restored = restore_interrupted_swap(root)
    except (OSError, python_runtime.PythonRuntimeError):
        restored = None
    current = runtime_marker_version(root)
    entry = {"before": current, "after": current, "status": STATUS_ABSENT, "error": ""}
    note = f" ({RESTORED_NOTE} {restored})" if restored else ""
    if not target.exists():
        entry["error"] = "Aucun Python isolé installé par Alpine Makers sur ce Worker ; rien à rafraîchir."
        return entry
    if current is None or is_link(target):
        entry.update(status=STATUS_SKIPPED, error="Dossier Python 3.12 non reconnu ou lien : conservé tel quel.")
        return entry
    if current == python_runtime.PYTHON_VERSION:
        entry.update(status=STATUS_CURRENT, error=note.strip(" ()"))
        return entry
    if not allowed:
        entry.update(status=STATUS_DEFERRED, error=DEFERRED_ENGINE_RUNNING + note)
        return entry
    if busy_components:
        entry.update(status=STATUS_DEFERRED, error=DEFERRED_COMPONENT_BUSY + " (" + ", ".join(sorted(busy_components)) + ")" + note)
        return entry
    try:
        if Path(sys.executable).resolve().is_relative_to(target.resolve()):
            entry.update(status=STATUS_DEFERRED, error="reporté : l'agent lui-même tourne avec ce Python.")
            return entry
    except OSError:
        pass
    swapped = False
    try:
        key = python_runtime._platform_key()
        spec = python_runtime.PYTHON_BUILDS[key]
        python_runtime._checked_root(root)
        previous = _previous_path(runtime, current)
        lock = runtime / ".python-3.12-install.lock"
        if is_link(lock):
            raise python_runtime.PythonRuntimeError("Le verrou du runtime Python est un lien ; échange refusé.")
        with installation_commit(lock, blocking=False) as acquired:
            if not acquired:
                entry.update(status=STATUS_DEFERRED, error="reporté : une préparation du Python IA est déjà en cours.")
                return entry
            with tempfile.TemporaryDirectory(prefix=".python-3.12-stage-", dir=runtime) as temporary:
                stage = Path(temporary)
                archive, unpacked = stage / "python.tar.gz", stage / "unpacked"
                progress(8, f"Téléchargement du Python {python_runtime.PYTHON_VERSION} isolé pour les moteurs IA…")
                python_runtime._download(spec, archive)
                unpacked.mkdir()
                progress(24, "Vérification et extraction du nouveau Python isolé…")
                python_runtime.safe_extract(archive, unpacked)
                prepared = unpacked / "python"
                if not python_runtime.probe_python(prepared / spec["executable"]):
                    raise python_runtime.PythonRuntimeError("Le Python téléchargé ne passe pas la vérification CPython 3.12 64 bits ; l'ancien reste en place.")
                atomic_json(prepared / python_runtime.MARKER, {"owner": "Alpine Makers", "version": python_runtime.PYTHON_VERSION,
                                                              "release": python_runtime.RELEASE, "sha256": spec["sha256"],
                                                              "source": python_runtime.DOWNLOAD_BASE + spec["asset"]})
                if previous.exists() or is_link(previous):
                    if is_link(previous):
                        raise python_runtime.PythonRuntimeError("L'emplacement de l'ancien Python est un lien ; échange refusé.")
                    shutil.rmtree(previous)
                progress(30, "Échange du Python isolé (aucun moteur en cours)…")
                # Les deux renommages forment l'échange : le verrou de validation empêche une annulation entre les deux.
                with installation_commit():
                    try:
                        os.rename(target, previous)
                    except PermissionError:
                        # Windows : python312.dll encore chargée par un moteur qui démarre ou s'arrête ; rien n'a bougé.
                        entry.update(status=STATUS_DEFERRED, error=DEFERRED_FILES_IN_USE + note)
                        return entry
                    swapped = True
                    os.rename(prepared, target)
                failed = [component for component, python in environments if not probe_environment(python)]
                if failed:
                    with installation_commit():
                        os.rename(target, stage / "rejected")
                        os.rename(previous, target)
                    swapped = False
                    entry.update(status=STATUS_FAILED,
                                 error="Environnement(s) " + ", ".join(failed) + " ne démarrent pas avec le nouveau Python ; ancien Python rétabli.")
                    return entry
                entry.update(after=python_runtime.PYTHON_VERSION, status=STATUS_UPDATED, error=note.strip(" ()"))
                try:
                    # Rangement : un seul ancien Python conservé. Un échec ici ne change jamais le résultat de l'échange.
                    for older in _previous_runtimes(runtime):
                        if older != previous:
                            shutil.rmtree(older, ignore_errors=True)
                except OSError:
                    pass
                return entry
    except (python_runtime.PythonRuntimeError, OSError, ValueError) as error:
        if entry["status"] == STATUS_UPDATED:
            # Échange réussi ; seul le nettoyage de la zone de préparation a renâclé (antivirus, partage Windows).
            entry["error"] = (entry["error"] + " " if entry["error"] else "") + f"Nettoyage incomplet : {type(error).__name__}."
            return entry
        entry.update(status=STATUS_FAILED, error=str(error)[:500] or type(error).__name__)
        # Si l'échange a eu lieu puis a échoué avant le retour, le dossier courant doit rester un Python valable.
        if swapped and runtime_marker_version(root) is None and not target.exists():
            try:
                previous = _previous_path(runtime, current)
                if previous.is_dir():
                    os.rename(previous, target)
            except (OSError, python_runtime.PythonRuntimeError):
                entry["error"] += " Retour à l'ancien Python impossible : vérifie le dossier runtime du Worker."
        return entry


def run(root, *, refresh_python=True, skip=(), agent_version=""):
    """Exécute les deux étapes, écrit le rapport puis retire la demande. Chaque étape échoue seule."""
    root = Path(root)
    skip = {str(item) for item in skip}
    report = {"finished_at": None, "agent_version": str(agent_version or ""), "status": STATUS_UPDATED,
              "python": {"before": None, "after": None, "status": STATUS_FAILED, "error": ""},
              "environments": [], "skipped": sorted(skip & set(MANAGED_COMPONENTS))}
    try:
        environments = managed_environments(root)
    except OSError as error:
        environments = []
        report["skipped"].append(f"inventaire des environnements : {type(error).__name__}")
    active = [(component, python) for component, python in environments if component not in skip]
    # Un composant en cours d'installation n'est pas sondé après l'échange : l'échange attend qu'il soit libre.
    busy = sorted({component for component, _ in environments if component in skip})
    progress(3, "Maintenance des environnements : inventaire des Python gérés…")
    try:
        report["python"] = refresh_python_runtime(root, active, allowed=refresh_python, busy_components=busy)
    except Exception as error:  # noqa: BLE001 - jamais fatal pour les environnements
        report["python"] = {"before": runtime_marker_version(root), "after": runtime_marker_version(root),
                            "status": STATUS_FAILED, "error": f"{type(error).__name__} : {str(error)[:300]}"}
    for index, (component, python) in enumerate(active):
        progress(40 + 55 * index / max(1, len(active)), f"pip, wheel et setuptools de « {component} »…")
        try:
            report["environments"].append(upgrade_environment(component, python))
        except Exception as error:  # noqa: BLE001
            report["environments"].append({"component": component, "pip_before": None, "pip_after": None,
                                           "status": STATUS_FAILED, "error": f"{type(error).__name__} : {str(error)[:300]}"})
    statuses = [report["python"]["status"], *(item["status"] for item in report["environments"])]
    if STATUS_FAILED in statuses:
        report["status"] = STATUS_FAILED
    elif STATUS_DEFERRED in statuses:
        report["status"] = STATUS_DEFERRED
    else:
        report["status"] = "terminé"
    report["finished_at"] = time.time()
    report["python_pending"] = report["python"]["status"] == STATUS_DEFERRED
    write_report(root, report)
    request = read_request(root) or {}
    if report["python_pending"]:
        # Les pip sont faits ; seul l'échange du Python attend. La demande reste (python_only) : l'agent réessaie
        # toutes les cinq minutes jusqu'à 24 h après la demande initiale, puis la classe « reportée ».
        write_request(root, request.get("from_version"), request.get("to_version"),
                      requested_at=request.get("requested_at"), python_only=True)
    else:
        request_path(root).unlink(missing_ok=True)
    return report


def summary_line(report):
    updated = sum(1 for item in report.get("environments", []) if item.get("status") == STATUS_UPDATED)
    python = report.get("python") or {}
    parts = [f"Python isolé : {python.get('status', '?')}" + (f" ({python.get('after')})" if python.get("after") else ""),
             f"{updated}/{len(report.get('environments', []))} environnement(s) mis à niveau"]
    return "Maintenance des environnements " + str(report.get("status", "")) + " — " + ", ".join(parts) + "."


def main(argv=None):
    parser = argparse.ArgumentParser(description="Maintenance des environnements du Worker Alpine Makers")
    parser.add_argument("--refresh-python", action="store_true", help="Autorise l'échange du Python isolé (aucun moteur en cours)")
    parser.add_argument("--skip", action="append", default=[], help="Composant en cours d'installation à laisser de côté")
    parser.add_argument("--agent-version", default="")
    args = parser.parse_args(argv)
    root_value = os.environ.get("ALPINE_WORKER_ROOT")
    if not root_value:
        print("ALPINE_WORKER_ROOT absent : maintenance refusée.", file=sys.stderr)
        return 2
    root = Path(root_value)
    try:
        report = run(root, refresh_python=args.refresh_python, skip=args.skip, agent_version=args.agent_version)
    except Exception as error:  # noqa: BLE001 - rendre un rapport lisible plutôt qu'une trace
        try:
            failure_report(root, args.agent_version, STATUS_FAILED,
                           f"Maintenance interrompue ({type(error).__name__}) : {str(error)[:300]}", clear_request=True)
        except OSError:
            print(f"Maintenance des environnements interrompue ({type(error).__name__}) ; rapport non écrit.", file=sys.stderr)
            return 1
        print(f"Maintenance des environnements interrompue ({type(error).__name__}) ; voir le rapport.", flush=True)
        return 0
    print(summary_line(report), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
