"""Local-only maintenance mailbox, protected by the installation directory ACL.

No registration, token rotation, shell payload or server identity changes.
Cleanup never follows links and never removes global programs or shared caches.
"""
import argparse
import base64
import hashlib
import importlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import threading
import time
import uuid

if __package__:
    from .job_state import atomic_json
    from .safety import is_link
    from .installers.model_catalog import COMFYUI_CATALOG
else:
    from job_state import atomic_json
    from safety import is_link
    from installers.model_catalog import COMFYUI_CATALOG

ENGINES = ("comfyui", "hunyuan3d", "orca", "orca-bridge", "freecad", "cuda-toolkit", "printguard")
TOOLS = ("image_generation", "ai3d", "model-studio", "converter", "printguard")
KEEP = {"input", "output", "user", ".cache", "custom_nodes"}
CHECKPOINTS = {spec[0] for spec in COMFYUI_CATALOG.values()}

# Moteurs a processus permanent pilotes par status / engines-stop / engines-start.
# "converter" (FreeCAD) n'a pas de processus : il n'est jamais touche ici.
# Valeur = cle du verrou par moteur de l'agent (TOOL_HANDLERS, verifie par les tests).
ENGINE_LOCK_KEYS = {"image_generation": "comfyui", "ai3d": "hunyuan3d", "model-studio": "orca", "printguard": "printguard"}
# PrintGuard lit le relais camera de l'agent : arrete en premier, demarre en dernier.
ENGINE_STOP_ORDER = ("printguard", "model-studio", "ai3d", "image_generation")
ENGINE_START_ORDER = ("image_generation", "ai3d", "model-studio", "printguard")
ENGINE_ACTIONS = ("status", "engines-stop", "engines-start")
# Lectures seules sans parametre, traitees comme status (requete a echeance, aucun heartbeat force).
READ_ACTIONS = ("status", "fire-watch")
ENGINES_MEMO = "runtime/local-control/engines-memo.json"
REQUEST_MAX_LIFETIME = 660  # --timeout est borne a 600 s cote client
_ENGINES_START_GUARD = threading.Lock()

# Agent 1.48.0 : actions a parametres et a echeance, servies par des modules charges a la demande.
# nom de l'action -> module dont le dictionnaire LOCAL_ACTIONS porte le descripteur
# {"handler": callable(agent, params) -> dict, "params": {nom: validateur}, "read": bool}.
LAZY_ACTIONS = {
    "duplicates-audit": "duplicates", "duplicates-cancel": "duplicates", "duplicates-status": "duplicates",
    "duplicates-plan": "duplicates", "duplicates-apply": "duplicates",
    "site-state": "self_service", "site-update": "self_service", "site-repair": "self_service",
    "jobs-stop": "self_service", "sharing-set": "self_service", "relay-status": "self_service",
    # Agent 1.50.0 : gestion complète depuis worker.exe (route signée self/manage).
    "manage": "full_control",
}
PARAMS_MAX_BYTES = 2048
# Fichier d'etat a la racine du Worker : l'etat en une ligne, lisible sans rien lancer.
STATUS_FILE = "ETAT-WORKER.txt"
STATUS_FILE_REFRESH = 600  # sans changement, la date est rafraichie toutes les dix minutes
STATUS_FILE_RETRY = 60     # apres un echec d'ecriture (fichier tenu par un autre programme)
STATUS_FILE_STATES = {"ok": "ok", "update": "maj", "error": "erreur", "update_error": "maj_erreur", "unknown": "inconnu"}
INDICATOR_CODES_MAX = 12
_SITE_CODE = re.compile(r"[a-z0-9_]{1,60}")
_VERSION = re.compile(r"[0-9]{1,9}(\.[0-9]{1,9}){0,5}")
_HEX = "0123456789abcdefABCDEF"


def network_info():
    addresses = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
            address = ipaddress.ip_address(item[4][0])
            if not address.is_loopback and not address.is_unspecified:
                addresses.add(str(address))
        return {"ipv4": sorted(addresses), "checked_at": time.time()}
    except OSError:
        return {"ipv4": [], "checked_at": time.time(), "error": "network_unavailable"}


def checked(root, relative):
    root = Path(root).resolve()
    path = root / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to(root) or path.resolve() == root:
        raise ValueError("Chemin hors perimetre ; aucun nettoyage autorise.")
    cursor = path
    while cursor != root:
        if is_link(cursor):
            raise ValueError("Lien/jonction detecte ; composant conserve pour proteger sa cible.")
        cursor = cursor.parent
    return path


def cleanup_plan(root):
    root = Path(root).resolve()
    files, skipped = [], []
    checked(root, "components")
    for engine in ENGINES:
        relative = Path("components") / engine
        try:
            folder = checked(root, relative)
            if not folder.is_dir():
                continue
            # A directory name alone is not proof of an Alpine installation.
            owned = any((folder / marker).is_file() for marker in
                        (".alpine-managed.json", ".raphal-ready", "freecad-path.txt", "cuda-path.txt"))
            if not owned:
                skipped.append(str(relative) + " : origine non prouvee (installation ancienne/partielle)")
                continue
            candidates = []
            for directory, names, filenames in os.walk(folder, followlinks=False):
                directory = Path(directory)
                for name in list(names) + filenames:
                    checked(root, (directory / name).relative_to(root))
                if directory == folder:
                    names[:] = [name for name in names if name not in KEEP]
                for name in filenames:
                    if name in {".alpine-managed.json", ".disabled"}:
                        continue
                    path = directory / name
                    sub = path.relative_to(folder)
                    if sub.parts[0] == "models":
                        if engine == "comfyui" and not (len(sub.parts) == 3 and sub.parts[1] == "checkpoints" and name.removesuffix(".download") in CHECKPOINTS):
                            continue
                        if engine == "hunyuan3d" and not any((folder / "models" / sub.parts[1] / marker).is_file() for marker in (".raphal-ready", ".alpine-managed.json")):
                            continue
                    info = path.stat()
                    candidates.append({"path": path.relative_to(root).as_posix(), "bytes": info.st_size, "mtime_ns": info.st_mtime_ns})
            files.extend(candidates)
            if engine == "orca-bridge":
                bridge = checked(root, "orca_core_bridge.ps1")
                if bridge.is_file():
                    info = bridge.stat()
                    files.append({"path": "orca_core_bridge.ps1", "bytes": info.st_size, "mtime_ns": info.st_mtime_ns})
        except (OSError, ValueError) as error:
            skipped.append(str(relative) + " : " + str(error))
    files.sort(key=lambda item: item["path"])
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"files": files, "skipped": skipped, "digest": digest, "bytes": sum(item["bytes"] for item in files)}


def cleanup(agent, digest):
    # Block launches and wait for any already-dispatched lifecycle operation.
    with agent.maintenance_lock:
        if agent.maintenance_active:
            raise ValueError("Une operation est en cours. Reessaie apres sa fin.")
        agent._assert_no_active_jobs()
        agent.maintenance_count += 1
        agent.maintenance_active = True
        agent.local_cleanup_active = True
    removed, errors = [], []
    try:
        plan = cleanup_plan(agent.root)
        if plan["digest"] != digest:
            raise ValueError("Le contenu a change depuis la confirmation. Relance le BAT pour un nouvel apercu.")
        # Persist ALL stop intents before touching any file. No auto-restart.
        for tool in TOOLS:
            agent.journal.update("runtime_intent", tool, enabled=False)
        for tool in TOOLS:
            agent.control_runtime(tool, "stop")
        # A final whole-plan check prevents deleting a changed file after stop.
        if cleanup_plan(agent.root)["digest"] != digest:
            raise ValueError("Fichiers modifies pendant l'arret : relance le BAT. Aucun fichier supprime.")
        planned_engines = {Path(item["path"]).parts[1] for item in plan["files"] if item["path"].startswith("components/")}
        for engine in planned_engines:
            marker = checked(agent.root, Path("components") / engine / ".alpine-managed.json")
            atomic_json(marker, {"owner": "Alpine Makers", "component": engine})
        for item in plan["files"]:
            try:
                path = checked(agent.root, item["path"])
                info = path.stat()
                if info.st_size != item["bytes"] or info.st_mtime_ns != item["mtime_ns"]:
                    raise ValueError("Fichier modifie, conserve")
                if not info.st_mode & stat.S_IWRITE:
                    if info.st_nlink > 1:
                        raise ValueError("Fichier en lecture seule partage par lien dur, conserve")
                    path.chmod(info.st_mode | stat.S_IWRITE)
                path.unlink()
                removed.append(item["path"])
            except (OSError, ValueError) as error:
                errors.append({"path": item["path"], "error": str(error)})
        # Keep harmless empty directories: never recursively delete a broad root.
        # Disable automatic discovery of global installations without uninstalling them.
        for engine in planned_engines:
            marker = checked(agent.root, Path("components") / engine / ".disabled")
            marker.write_text("AI removed locally; global programs preserved", encoding="utf-8")
        return {"removed_files": len(removed), "errors": errors, "skipped": plan["skipped"],
                "identity_preserved": True, "resync": "next heartbeat"}
    finally:
        with agent.maintenance_lock:
            agent.maintenance_count -= 1
            agent.local_cleanup_active = False
            agent.maintenance_active = agent.maintenance_count > 0 or bool(agent.maintenance_recovery)
        with agent.inventory_lock:
            agent.inventory_revision += 1
            agent.inventory_updated_at = 0
            agent.inventory_snapshot = {}


def _engine_intent(agent, tool_id):
    intent = agent.journal.get("runtime_intent", tool_id)
    return intent.get("enabled") if isinstance(intent, dict) else None


def _read_memo(agent):
    try:
        memo = json.loads(checked(agent.root, ENGINES_MEMO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(memo, dict):
        return None

    def names(key):
        # Le memo n'est qu'une liste d'identifiants connus : rien d'autre n'en est repris.
        values = memo.get(key) if isinstance(memo.get(key), list) else []
        return [tool for tool in ENGINE_START_ORDER if tool in values]

    at = memo.get("at")
    return {"at": at if isinstance(at, (int, float)) and not isinstance(at, bool) else None,
            "were_running": names("were_running"), "intent_enabled": names("intent_enabled")}


def _patch_inventory(agent, tool_id, started):
    # Meme correctif d'instantane que tool.start / tool.stop dans l'agent.
    with agent.inventory_lock:
        agent.inventory_revision += 1
        agent.inventory_updated_at = time.monotonic()
        for tool in agent.inventory_snapshot.get("tools", []):
            if tool.get("tool_id") == tool_id:
                tool["status"] = "online" if started else "ready"
                tool["config"] = {**(tool.get("config") or {}), "runtime_enabled": started}


def _engine_lock(agent, tool_id):
    with agent.command_lock:
        return agent.component_locks.setdefault(ENGINE_LOCK_KEYS[tool_id], threading.Lock())


def _release_maintenance(agent):
    with agent.maintenance_lock:
        agent.maintenance_count -= 1
        agent.maintenance_active = agent.maintenance_count > 0 or bool(agent.maintenance_recovery)


def _import_optional(module):
    """Module facultatif de l'agent, importe a la demande (meme convention que les imports de tete)."""
    return importlib.import_module("." + module, __package__) if __package__ else importlib.import_module(module)


def _agent_version(agent):
    # La version vient du module de l'agent deja charge : aucun import circulaire.
    version = getattr(sys.modules.get(type(agent).__module__), "AGENT_VERSION", None)
    return version if isinstance(version, str) else None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _version_parts(value):
    if not isinstance(value, str) or len(value) > 40 or not _VERSION.fullmatch(value):
        return ()
    return tuple(int(part) for part in value.split("."))


def version_newer(installed, available):
    """Vrai si ``available`` est strictement plus recente : comparaison numerique par champs
    (« 1.9.0 » est anterieure a « 1.20.1 »), meme regle que worker_health.agent_outdated cote site."""
    left, right = _version_parts(installed), _version_parts(available)
    if not left or not right:
        return False
    size = max(len(left), len(right))
    return (left + (0,) * size)[:size] < (right + (0,) * size)[:size]


def _heartbeat_unreachable(http):
    return http is None or http in (502, 503, 504) or 520 <= http <= 529


def indicator(agent):
    """Etat en une ligne pour le bandeau et le fichier d'etat : mise a jour proposee, erreurs a reparer.

    Aucun reseau, aucun chemin. Tout ce qui vient du site (self_state, health, problems) est valide
    par son type avant d'etre lu : une valeur inattendue est ignoree, jamais indexee.
    """
    version = _agent_version(agent)
    available = getattr(agent, "available_agent_version", "")
    update = available if isinstance(available, str) and version_newer(version, available) else ""
    errors, alerts = [], []

    def add(code, blocking):
        if code not in errors and code not in alerts:
            (errors if blocking else alerts).append(code)

    beat = getattr(agent, "heartbeat_state", None)
    beat = beat if isinstance(beat, dict) else {}
    failures = _count(beat.get("failures"))
    http = beat.get("http") if isinstance(beat.get("http"), int) and not isinstance(beat.get("http"), bool) else None
    # Echecs de suite de chaque sorte, comptes par l'agent ; repli sur le dernier statut s'ils manquent.
    refused = beat.get("refused") if "refused" in beat else (failures if http in (401, 403, 410) else 0)
    unreachable = beat.get("unreachable") if "unreachable" in beat else (failures if _heartbeat_unreachable(http) else 0)
    if _count(refused) >= 3:
        add("A-LIAISON-REFUSEE", True)
    if _count(unreachable) >= 9:
        add("A-SITE-INJOIGNABLE", False)
    integrity = getattr(agent, "integrity", None)
    if isinstance(integrity, dict) and _count(integrity.get("missing")) + _count(integrity.get("modified")) > 0:
        add("A-FICHIERS-AGENT", True)
    if getattr(agent, "maintenance_recovery", None):
        add("A-INSTALLATION-INCERTAINE", True)
    watch = getattr(agent, "runtime_watch", None)
    if isinstance(watch, dict) and any(isinstance(item, dict) and item.get("exhausted") is True for item in tuple(watch.values())):
        add("A-MOTEUR-TOMBE", True)
    try:
        summary = agent.environment_maintenance_summary()
    except Exception:
        summary = None  # rapport de maintenance illisible : aucune alerte, le reste de l'indicateur tient
    if isinstance(summary, dict) and summary.get("status") == "failed":
        add("A-MAINTENANCE-ENV", False)
    state = getattr(agent, "self_state", None)
    site = state.get("site") if isinstance(state, dict) else None
    health = site.get("health") if isinstance(site, dict) else None
    problems = health.get("problems") if isinstance(health, dict) else None
    for problem in problems if isinstance(problems, list) else ():
        code = problem.get("code") if isinstance(problem, dict) else None
        # agent_outdated est l'indicateur de mise a jour ; offline n'a pas de sens vu de l'agent.
        if not isinstance(code, str) or not _SITE_CODE.fullmatch(code) or code in ("agent_outdated", "offline"):
            continue
        add("S-" + code, problem.get("severity") == "bloquant")
    if beat.get("ok") is None:
        level = "unknown"  # aucun heartbeat n'a encore reussi ni echoue
    elif errors:
        level = "update_error" if update else "error"
    else:
        level = "update" if update else "ok"
    return {"state": level, "update_available": update, "errors": len(errors), "alerts": len(alerts),
            "codes": (errors + alerts)[:INDICATOR_CODES_MAX], "checked_at": time.time()}


def _heartbeat_view(agent):
    state = agent.heartbeat_state
    if not isinstance(state, dict):
        return None
    ok, http = state.get("ok"), state.get("http")
    return {"ok": ok if isinstance(ok, bool) else None, "at": _number(state.get("at")), "failures": _count(state.get("failures")),
            "http": http if isinstance(http, int) and not isinstance(http, bool) else None}


def _site_view(agent):
    state = agent.self_state
    if not isinstance(state, dict):
        return None
    return {"known": state.get("known") is True, "fetched_at": _number(state.get("fetched_at")), "missing": state.get("missing") is True}


def _sharing_view(agent):
    state = agent.self_state
    if not isinstance(state, dict):
        return None
    site = state.get("site")
    sharing = site.get("sharing") if isinstance(site, dict) else None
    if not isinstance(sharing, dict):
        return {"known": False, "mode": "", "audiences": [], "auto_accept_jobs": False, "users_count": 0}
    mode, audiences = sharing.get("mode"), sharing.get("audiences")
    return {"known": True, "mode": mode if isinstance(mode, str) and mode in ("prive", "comptes", "public") else "",
            "audiences": [name for name in ("team", "contributors", "everyone") if isinstance(audiences, list) and name in audiences],
            "auto_accept_jobs": sharing.get("auto_accept_jobs") is True, "users_count": _count(sharing.get("users_count"))}


def _duplicates_view(agent):
    try:
        module = _import_optional("duplicates")
    except ImportError:
        return None  # module absent : le champ vaut null, rien d'autre ne change
    summary = module.status_summary(agent)
    if not isinstance(summary, dict):
        return None
    audit, applied = summary.get("audit_state"), summary.get("apply_state")
    # Seuls des etats connus et des nombres sortent d'ici : jamais un chemin.
    return {"audit_state": audit if isinstance(audit, str) and audit in ("idle", "running", "done", "cancelled", "failed") else "idle",
            "progress": _count(summary.get("progress")), "finished_at": _number(summary.get("finished_at")),
            "groups": _count(summary.get("groups")), "in_scope_groups": _count(summary.get("in_scope_groups")),
            "reclaimable_bytes": _count(summary.get("reclaimable_bytes")),
            "apply_state": applied if isinstance(applied, str) and applied in ("idle", "running", "done") else "idle"}


def _grbl_view(agent):
    names = agent.active_grbl_jobs()
    return [str(name)[:80] for name in names[:32]] if isinstance(names, list) else None


def _integrity_view(agent):
    state = agent.integrity
    if not isinstance(state, dict):
        return None
    return {"checked_at": _number(state.get("checked_at")), "missing": _count(state.get("missing")), "modified": _count(state.get("modified"))}


def _guarded(view, agent):
    # status est lu par le bandeau et par tous les outils : un champ nouveau qui echoue vaut null,
    # il ne fait jamais echouer la reponse.
    try:
        return view(agent)
    except Exception:
        return None


def status(agent):
    """Instantane en lecture seule. Jamais de jeton, d'identite complete ni de chemin."""
    jobs, previous_identity = [], 0
    for job_id, item in agent.journal.records("jobs").items():
        if not agent._job_active(item):
            continue
        if not agent._job_identity_matches(item):
            previous_identity += 1  # ignore par les gardes : identite precedente
            continue
        jobs.append({"job_id": str(job_id), "tool_id": item.get("tool_id"), "state": item.get("state"),
                     "engine_stop_confirmed": item.get("engine_stop_confirmed") is True})
    engines = []
    for tool_id in ENGINE_START_ORDER:
        pid = agent._runtime_record(tool_id).get("pid")
        engines.append({"tool_id": tool_id, "running": bool(agent._runtime_running(tool_id)),
                        "port": agent._runtime_port(tool_id),
                        "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
                        "intent": _engine_intent(agent, tool_id)})
    # "converter" (FreeCAD) n'a pas de processus permanent : il est omis.
    recovery = agent.maintenance_recovery
    return {"checked_at": time.time(), "agent_pid": os.getpid(),
            "agent_version": _agent_version(agent),
            "name": agent.config.get("name"), "server_url": agent.config.get("server_url"),
            "worker_id_prefix": str(agent.config.get("worker_id") or "")[:8],
            "maintenance_active": bool(agent.maintenance_active), "maintenance_count": int(agent.maintenance_count),
            "maintenance_recovery": len(recovery) if hasattr(recovery, "__len__") else int(bool(recovery)),
            "local_cleanup_active": bool(agent.local_cleanup_active), "disconnecting": bool(agent.disconnecting),
            "jobs_active": jobs, "jobs_previous_identity": previous_identity,
            "engines": engines,
            "engines_start": {"in_progress": bool(getattr(agent, "local_engines_starting", False)),
                              "last": getattr(agent, "local_engines_last_start", None)},
            "memo": _read_memo(agent),
            # Champs ajoutes par l'agent 1.48.0 (rien de retire), chacun calcule a part.
            "indicator": _guarded(indicator, agent), "heartbeat": _guarded(_heartbeat_view, agent),
            "site": _guarded(_site_view, agent), "sharing": _guarded(_sharing_view, agent),
            "duplicates": _guarded(_duplicates_view, agent), "grbl_jobs": _guarded(_grbl_view, agent),
            "integrity": _guarded(_integrity_view, agent)}


def status_file_text(info, version, now):
    """Contenu exact de ETAT-WORKER.txt (ASCII, CRLF) : une ligne lisible, puis les lignes cle=valeur des outils."""
    update, errors = info["update_available"], info["errors"]
    state = STATUS_FILE_STATES.get(info["state"], "inconnu")
    line = {"inconnu": "ETAT INCONNU (l'agent vient de demarrer)", "ok": "TOUT VA BIEN",
            "maj": f"MISE A JOUR DISPONIBLE : {update}", "erreur": f"{errors} ERREUR(S) A REPARER",
            "maj_erreur": f"MISE A JOUR DISPONIBLE : {update} - {errors} ERREUR(S) A REPARER"}[state]
    moment = time.localtime(now)  # heure locale du PC
    lines = [time.strftime("%Y-%m-%d %H:%M", moment) + "  " + line,
             f"Worker Alpine Makers, version installee : {version}.",
             "Pour agir : ouvre worker.exe (ou MENU-WORKER.bat), rubrique GESTION DU WORKER.",
             "Si la date ci-dessus a plus de 20 minutes, l'agent ne tourne plus : ouvre worker.exe pour le verifier.",
             "",
             "--- lignes lues par les outils, ne pas modifier ---",
             "etat=" + state,
             "date=" + time.strftime("%Y-%m-%dT%H:%M:%S", moment),
             f"version={version}",
             f"maj={update}",
             f"erreurs={errors}",
             f"alertes={info['alerts']}",
             "codes=" + ",".join(info["codes"])]
    return "\r\n".join(lines) + "\r\n"


def write_status_file(agent, force=False, now=None):
    """Ecrit ETAT-WORKER.txt a la racine du Worker, sans reseau. Rend True si le fichier a ete ecrit.

    Reecrit seulement si le coeur (etat, version, maj, erreurs, alertes, codes) a change, ou si la
    derniere ecriture reussie a plus de dix minutes (la date du fichier prouve que l'agent tourne).
    """
    now = time.time() if now is None else now
    memo = getattr(agent, "status_file_memo", None)
    if not isinstance(memo, dict):
        memo = agent.status_file_memo = {"core": None, "written_at": None, "failed_at": None, "logged": False}
    info = indicator(agent)
    version = _agent_version(agent) or ""
    core = (info["state"], version, info["update_available"], info["errors"], info["alerts"], tuple(info["codes"]))
    if not force:
        if memo["core"] == core and memo["written_at"] is not None and abs(now - memo["written_at"]) < STATUS_FILE_REFRESH:
            return False
        if memo["failed_at"] is not None and abs(now - memo["failed_at"]) < STATUS_FILE_RETRY:
            return False  # un autre programme tient le fichier : pas de nouvel essai avant une minute
    target = Path(agent.root) / STATUS_FILE
    try:
        if is_link(target):
            raise OSError("cible liee")  # jamais d'ecriture a travers un lien
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(status_file_text(info, version, now).encode("ascii", "replace"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as error:
        memo["failed_at"] = now
        if not memo["logged"]:  # une seule ligne par serie d'echecs
            memo["logged"] = True
            print(f"Fichier d'etat {STATUS_FILE} non ecrit ({type(error).__name__}) ; nouvel essai dans une minute.", flush=True)
        return False
    memo.update(core=core, written_at=now, failed_at=None, logged=False)
    return True


def fire_watch_status(agent):
    """Instantane de la surveillance IA des graveuses, en lecture seule (aucune image, aucun secret)."""
    watch = getattr(agent, "fire_watch", None)
    if watch is None:
        error = str(getattr(agent, "fire_watch_error", "") or "")
        return {"state": "not_started", "message": "Surveillance non demarree sur ce Worker.",
                **({"error": error[:300]} if error else {})}
    try:
        return watch.local_status()
    except Exception as error:
        return {"state": "error", "message": f"Etat de la surveillance illisible ({type(error).__name__})."}


def engines_stop(agent):
    # Meme rigueur que cleanup() : on bloque les lancements, puis on arrete.
    with agent.maintenance_lock:
        if getattr(agent, "local_engines_starting", False):
            raise ValueError("Arret refuse : un demarrage des moteurs est en cours. Reessaie apres sa fin.")
        if agent.maintenance_active:
            raise ValueError("Arret refuse : une installation ou une maintenance est en cours. Reessaie apres sa fin.")
        try:
            agent._assert_no_active_jobs()
        except Exception as error:
            raise ValueError(f"Arret des moteurs refuse : {error}") from error
        agent.maintenance_count += 1
        agent.maintenance_active = True
    stopped, errors = [], []
    try:
        were_running = [tool for tool in ENGINE_START_ORDER if agent._runtime_running(tool)]
        intent_enabled = [tool for tool in ENGINE_START_ORDER if _engine_intent(agent, tool) is True]
        # Un memo non consomme est FUSIONNE, jamais ecrase : apres un arret
        # partiel (un moteur en echec), le second arret ne voit plus que ce
        # moteur-la et ferait oublier ceux deja arretes par le premier.
        previous = _read_memo(agent) or {"at": None, "were_running": [], "intent_enabled": []}
        merged = {key: [tool for tool in ENGINE_START_ORDER if tool in previous[key] or tool in current]
                  for key, current in (("were_running", were_running), ("intent_enabled", intent_enabled))}
        if previous["at"] is not None and all(merged[key] == previous[key] for key in merged):
            memo = previous  # rien de nouveau : le memo utile reste tel quel
        else:
            memo = {"at": time.time(), **merged}
            atomic_json(checked(agent.root, ENGINES_MEMO), memo)
        # Toutes les intentions a False AVANT le premier arret : ni le chien de
        # garde ni un redemarrage de l'agent ne relancent un moteur arrete ici.
        for tool in ENGINE_STOP_ORDER:
            agent.journal.update("runtime_intent", tool, enabled=False)
        for tool in ENGINE_STOP_ORDER:
            try:
                with _engine_lock(agent, tool):
                    result = agent.control_runtime(tool, "stop")
                if isinstance(result, dict) and result.get("stopped"):
                    stopped.append(tool)
                _patch_inventory(agent, tool, False)
            except Exception as error:  # un echec n'empeche pas l'arret des suivants
                errors.append({"tool_id": tool, "error": str(error)})
        return {"stopped": stopped, "were_running": were_running, "errors": errors, "memo": memo}
    finally:
        _release_maintenance(agent)


def _start_one(agent, tool_id):
    # Chemin garde de tool.start (Agent.execute puis _execute_serialized), sans
    # commande serveur : aucun recu n'est fabrique dans le journal des commandes.
    with agent.maintenance_lock:
        if agent.local_cleanup_active:
            raise ValueError("Nettoyage IA local en cours ; reessaie apres sa fin.")
        agent._assert_no_active_jobs()
        agent.maintenance_count += 1
        agent.maintenance_active = True
    try:
        with _engine_lock(agent, tool_id):
            result = agent.control_runtime(tool_id, "start")
        agent.journal.update("runtime_intent", tool_id, enabled=True)
        _patch_inventory(agent, tool_id, True)
        return result
    finally:
        _release_maintenance(agent)


def _settle_memo(agent, started):
    # Le memo n'est consomme que pour les moteurs reellement demarres : ceux
    # qui restent a relancer y demeurent pour le prochain engines-start.
    # Il est relu ici : on ne retire que ce qui a demarre, rien d'autre.
    memo = _read_memo(agent)
    if memo is None:
        return
    path = checked(agent.root, ENGINES_MEMO)
    remaining = {key: [tool for tool in memo[key] if tool not in started] for key in ("were_running", "intent_enabled")}
    if remaining == {key: memo[key] for key in remaining}:
        return
    if remaining["were_running"] or remaining["intent_enabled"]:
        atomic_json(path, {"at": memo["at"] if memo["at"] is not None else time.time(), **remaining})
    else:
        path.unlink(missing_ok=True)


def _start_worker(agent, tools, source, memo):
    started, errors = [], []
    try:
        for tool in tools:
            if agent.stop_requested.is_set() or agent.disconnecting:
                errors.append({"tool_id": tool, "error": "Agent en cours d'arret ; demarrage abandonne."})
                continue
            try:
                _start_one(agent, tool)
                started.append(tool)
            except Exception as error:
                errors.append({"tool_id": tool, "error": str(error)})
                print(f"Demarrage local du moteur {tool} impossible ({type(error).__name__}).", flush=True)
    finally:
        try:
            if memo is not None:
                try:
                    _settle_memo(agent, started)
                except (OSError, ValueError) as error:
                    errors.append({"tool_id": None, "error": f"Memo des moteurs non mis a jour ({type(error).__name__})."})
        finally:
            agent.local_engines_last_start = {"at": time.time(), "source": source, "requested": list(tools),
                                              "started": started, "errors": errors}
            # Le drapeau ne tombe qu'APRES l'ecriture du memo : un engines-stop
            # (refuse tant qu'il est leve) ne peut pas ecrire en meme temps.
            with _ENGINES_START_GUARD:
                agent.local_engines_starting = False
            agent.reconnect_requested.set()


def engines_start(agent):
    with _ENGINES_START_GUARD:
        if getattr(agent, "local_engines_starting", False):
            raise ValueError("Un demarrage des moteurs est deja en cours. Attends sa fin (action status).")
        # Memes gardes que chaque demarrage, verifiees AVANT de repondre : on
        # n'annonce pas un demarrage qui sera refuse, et le memo reste intact.
        with agent.maintenance_lock:
            if agent.local_cleanup_active:
                raise ValueError("Demarrage refuse : un nettoyage IA local est en cours. Reessaie apres sa fin.")
            if agent.maintenance_active:
                raise ValueError("Demarrage refuse : une installation ou une maintenance est en cours. Reessaie apres sa fin.")
            try:
                agent._assert_no_active_jobs()
            except Exception as error:
                raise ValueError(f"Demarrage refuse : {error}") from error
        memo = _read_memo(agent)
        if memo is not None:
            wanted, source = set(memo["were_running"]) | set(memo["intent_enabled"]), "memo"
        else:
            # Sans memo, l'etat d'avant l'arret est inconnu : on se rabat sur les
            # moteurs que l'inventaire de l'agent declare installes et prets.
            with agent.inventory_lock:
                wanted = {str(tool.get("tool_id")) for tool in agent.inventory_snapshot.get("tools", [])
                          if str(tool.get("status") or "") in {"ready", "online"}}
            source = "inventory"
        tools = [tool for tool in ENGINE_START_ORDER if tool in wanted]
        if memo is None and not tools:
            raise ValueError("Aucun memo d'arret et aucun moteur pret dans l'inventaire. "
                             "Attends la fin de l'inventaire ou demarre le moteur depuis Mes Workers.")
        agent.local_engines_starting = True
        try:
            thread = threading.Thread(target=_start_worker, args=(agent, tools, source, memo), name="local-engines-start", daemon=True)
            agent.local_engines_thread = thread
            thread.start()
        except BaseException:
            agent.local_engines_starting = False
            raise
    # Le memo est solde par le thread, une fois le resultat connu (_settle_memo).
    return {"starting": tools, "source": source}


def _check_expiry(item):
    expires_at = item.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise ValueError("Requete locale sans echeance ; action refusee.")
    now = time.time()
    if expires_at < now or expires_at > now + REQUEST_MAX_LIFETIME:
        raise ValueError("Requete locale expiree ; action non executee. Relance l'outil.")


def _check_engine_request(item):
    # Aucune surface nouvelle : ni chemin ni parametre libre pour ces actions.
    if set(item) - {"worker_id", "action", "expires_at"}:
        raise ValueError("Cette action locale n'accepte aucun parametre.")
    _check_expiry(item)


def v_bool(value):
    """Validateur : un booleen JSON, rien d'autre (ni 0/1, ni "true")."""
    if type(value) is not bool:
        raise ValueError("Paramètre local invalide : un booléen est attendu.")
    return value


def v_hex(length):
    """Validateur : une chaine d'exactement ``length`` chiffres hexadecimaux, rendue en minuscules."""
    def check(value):
        if type(value) is not str or len(value) != length or any(char not in _HEX for char in value):
            raise ValueError(f"Paramètre local invalide : {length} caractères hexadécimaux sont attendus.")
        return value.lower()
    return check


def v_choice(*values):
    """Validateur : une des chaines de la liste, a l'identique."""
    def check(value):
        if type(value) is not str or value not in values:
            raise ValueError("Paramètre local invalide : valeur hors de la liste permise.")
        return value
    return check


def v_list_of(choices, maximum):
    """Validateur : une liste JSON (jamais une chaine) d'au plus ``maximum`` valeurs prises dans ``choices``, sans doublon."""
    choices = tuple(choices)

    def check(value):
        if type(value) is not list or len(value) > maximum:
            raise ValueError(f"Paramètre local invalide : une liste d'au plus {maximum} valeur(s) est attendue.")
        cleaned = []
        for item in value:
            if type(item) is not str or item not in choices:
                raise ValueError("Paramètre local invalide : valeur hors de la liste permise.")
            if item not in cleaned:
                cleaned.append(item)
        return cleaned
    return check


def _load_action(name):
    """Rend le descripteur {"handler": callable(agent, params) -> dict, "params": {nom: validateur}, "read": bool}."""
    module = LAZY_ACTIONS[name]
    try:
        actions = getattr(_import_optional(module), "LOCAL_ACTIONS", None)
    except ImportError as error:
        raise ValueError(f"Fichier de l'agent manquant ({module}.py) : lance « Réparer ».") from error
    spec = actions.get(name) if isinstance(actions, dict) else None
    if not isinstance(spec, dict) or not callable(spec.get("handler")) or not isinstance(spec.get("params"), dict):
        raise ValueError("Action locale inconnue")
    return spec


def _check_param_request(item, spec):
    """Cles ⊆ {worker_id, action, expires_at, params} ; echeance comme _check_engine_request ;
    params = dict, cles ⊆ spec["params"] ; chaque valeur passe par son validateur (rend la valeur nettoyee
    ou leve ValueError). Rend le dict nettoye. Toute cle inconnue : ValueError("Paramètre local non autorisé : <nom>")."""
    unknown = sorted(str(key) for key in set(item) - {"worker_id", "action", "expires_at", "params"})
    if unknown:
        raise ValueError(f"Paramètre local non autorisé : {unknown[0]}")
    _check_expiry(item)
    params = item.get("params", {})
    if type(params) is not dict:
        raise ValueError("Paramètres locaux invalides : un objet est attendu.")
    cleaned = {}
    for name in sorted(params):
        validator = spec["params"].get(name)
        if not callable(validator):
            raise ValueError(f"Paramètre local non autorisé : {name}")
        cleaned[name] = validator(params[name])
    return cleaned


def process_requests(agent):
    inbox = checked(agent.root, "runtime/local-control")
    inbox.mkdir(parents=True, exist_ok=True)
    for path in sorted(inbox.glob("*.request.json"))[:10]:
        result_path = path.with_name(path.name.replace(".request.json", ".result.json"))
        action = None
        read_only = False
        try:
            checked(agent.root, path.relative_to(agent.root))
            if path.stat().st_size > 4096:
                raise ValueError("Requete locale trop volumineuse")
            item = json.loads(path.read_text(encoding="utf-8"))
            if item.get("worker_id") != agent.config.get("worker_id"):
                raise ValueError("Cette requete appartient a un autre Worker")
            if result_path.exists():
                path.unlink()
                continue
            action = item.get("action")
            read_only = action in READ_ACTIONS
            if action in ENGINE_ACTIONS or action in READ_ACTIONS:
                _check_engine_request(item)
                # Requete retiree AVANT execution : un arret de l'agent en cours
                # de route ne la rejoue jamais au demarrage suivant.
                path.unlink()
                result = {"status": status, "engines-stop": engines_stop, "engines-start": engines_start,
                          "fire-watch": fire_watch_status}[action](agent)
            elif isinstance(action, str) and action in LAZY_ACTIONS:
                spec = _load_action(action)
                read_only = spec.get("read") is True
                params = _check_param_request(item, spec)
                path.unlink()  # retiree AVANT execution, comme ci-dessus : jamais rejouee
                # Le handler rend en moins de 2 s : un travail long part dans son propre fil.
                result = spec["handler"](agent, params)
            elif action == "cleanup":
                result = cleanup(agent, item.get("digest"))
            elif action in {"reconnect", "network"}:
                with agent.inventory_lock:
                    agent.inventory_updated_at = 0
                    agent.inventory_snapshot = {}
                result = {"network": network_info(), "identity_preserved": True,
                          "message": "Reconnexion demandee, sans changement d'identite. Envoi au prochain heartbeat."}
            else:
                raise ValueError("Action locale inconnue")
            atomic_json(result_path, {"ok": True, "result": result})
        except Exception as error:
            atomic_json(result_path, {"ok": False, "error": str(error)})
        path.unlink(missing_ok=True)
        if not read_only:  # lecture seule : aucun heartbeat force
            agent.reconnect_requested.set()


def local_loop(agent):
    status_file_failed = False
    while not agent.stop_requested.is_set():
        try:
            process_requests(agent)
        except Exception as error:  # rien ne doit tuer ce fil : toutes les actions locales en dependent
            print(f"Maintenance locale indisponible ({type(error).__name__})", flush=True)
        # Fichier d'etat : dans son propre try, jamais dans celui des demandes locales.
        try:
            write_status_file(agent)
            status_file_failed = False
        except Exception as error:
            if not status_file_failed:  # une seule ligne par serie d'echecs
                print(f"Fichier d'etat du Worker indisponible ({type(error).__name__})", flush=True)
            status_file_failed = True
        agent.stop_requested.wait(1)


def main():
    parser = argparse.ArgumentParser(description="Boite aux lettres locale du Worker (aucun reseau necessaire).")
    parser.add_argument("--root", required=True)
    parser.add_argument("--action", choices=("reconnect", "network", "cleanup") + ENGINE_ACTIONS + ("fire-watch",) + tuple(LAZY_ACTIONS),
                        required=True,
                        help="status : etat de l'agent et des moteurs ; engines-stop / engines-start : moteurs IA ; "
                             "fire-watch : etat de la surveillance IA des graveuses (lecture seule) ; "
                             "reconnect / network / cleanup : maintenance ; "
                             "duplicates-* / site-* / jobs-stop / sharing-set / relay-status : gestion du Worker")
    parser.add_argument("--json", action="store_true", help="n'imprime qu'une ligne JSON : la reponse de l'agent")
    parser.add_argument("--timeout", type=int, default=120, help="attente maximale en secondes (1 a 600)")
    parser.add_argument("--params-b64", default=None,
                        help="parametres de l'action : base64 d'un objet JSON UTF-8 d'au plus 2048 octets (actions de gestion seulement)")
    args = parser.parse_args()
    if args.json and args.action == "cleanup":
        parser.error("--json est incompatible avec cleanup (confirmation interactive).")
    if args.params_b64 is not None and args.action not in LAZY_ACTIONS:
        parser.error("--params-b64 n'est accepte que par les actions de gestion du Worker.")
    timeout = min(600, max(1, args.timeout))
    transient = args.action in ENGINE_ACTIONS or args.action in READ_ACTIONS or args.action in LAZY_ACTIONS

    def emit(response, code):
        print(json.dumps(response, ensure_ascii=True, separators=(",", ":")), flush=True)
        return code

    params = None
    if args.params_b64 is not None:
        try:
            raw = base64.b64decode(args.params_b64, validate=True)
            if len(raw) > PARAMS_MAX_BYTES:
                raise ValueError("too large")
            params = json.loads(raw.decode("utf-8"))
            if type(params) is not dict:
                raise ValueError("object expected")
        except (ValueError, RecursionError):  # base64, UTF-8, JSON ou type invalide
            if args.json:
                return emit({"ok": False, "error": "Parametres locaux illisibles."}, 1)
            raise SystemExit("Parametres locaux illisibles.")

    root = Path(args.root).resolve()
    try:
        config = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        if args.json:
            return emit({"ok": False, "error": "Configuration du Worker illisible."}, 1)
        raise
    if not config.get("worker_id") or not config.get("token"):
        if args.json:
            return emit({"ok": False, "error": "Worker non associe : aucun changement effectue."}, 1)
        raise SystemExit("Worker non associe : aucun changement effectue.")
    item = {"worker_id": config["worker_id"], "action": args.action}
    if transient:
        # L'agent refuse une requete echue : jamais d'execution differee.
        item["expires_at"] = time.time() + timeout + 5
    if params is not None:
        item["params"] = params
    inbox = checked(root, "runtime/local-control")
    inbox.mkdir(parents=True, exist_ok=True)
    if args.action == "cleanup":
        print("Inventaire des composants et verification des chemins, sans suppression...", flush=True)
        plan = cleanup_plan(root)
        preview = inbox / "cleanup-preview.json"
        atomic_json(preview, plan)
        print(f"Worker : {config['worker_id']}\nDossier : {root}")
        print(f"Suppression prevue : {len(plan['files'])} fichiers, {plan['bytes'] / 1024**3:.2f} Gio.")
        print("Composants : " + ", ".join(sorted({p['path'].split('/')[1] for p in plan['files'] if p['path'].startswith('components/')})))
        print(f"Liste complete : {preview}\nConserves : Worker, identite, associations, relais, travaux, caches et logiciels globaux.")
        for skipped in plan["skipped"]:
            print("CONSERVE : " + skipped)
        if not plan["files"] or input("Pour confirmer, tape SUPPRIMER IA : ").strip() != "SUPPRIMER IA":
            print("Aucune suppression demandee.")
            return 0
        item["digest"] = plan["digest"]
    request = inbox / (uuid.uuid4().hex + ".request.json")
    atomic_json(request, item)
    result = request.with_name(request.name.replace(".request.json", ".result.json"))
    if not args.json:
        print("Demande envoyee au Worker local. La connexion internet n'est pas necessaire au nettoyage.")
    deadline = time.monotonic() + timeout
    answered = False
    try:
        while time.monotonic() < deadline:
            if result.exists():
                try:
                    response = json.loads(result.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    time.sleep(.1)  # ecriture atomique : simple course de lecture
                    continue
                answered = True
                if args.json:
                    result.unlink(missing_ok=True)
                    emit(response, 0)
                else:
                    print(json.dumps(response, ensure_ascii=True, indent=2))
                failed = not response.get("ok") or (isinstance(response.get("result"), dict) and response["result"].get("errors"))
                return 1 if failed else 0
            time.sleep(.25 if transient or args.json else 1)
    finally:
        in_progress = False
        if transient and not answered:
            # Jamais d'execution differee : la requete non prise est retiree.
            # Si l'agent l'a deja retiree, l'action est en cours maintenant.
            try:
                request.unlink()
            except FileNotFoundError:
                in_progress = not result.exists()
            except OSError:
                pass
    pending = request.exists()
    if args.json:
        response = {"ok": False, "error": "timeout", "pending": pending}
        if in_progress:
            response["in_progress"] = True
        return emit(response, 2)
    if transient:
        print("Aucune reponse de l'agent dans le delai : demande retiree, rien ne sera execute plus tard."
              if not in_progress else "L'agent execute encore la demande ; consulte l'action status.")
    else:
        print(f"Demande toujours en attente, ne pas reinstaller. Resultat a consulter : {result}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
