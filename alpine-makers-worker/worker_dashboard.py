"""Tableau de bord du Worker, commun au menu console (MENU-WORKER.bat) et à worker.exe (capacité worker_dashboard_v1).

Une action locale en lecture seule, ``dashboard``, rend en moins d'une seconde l'état complet du Worker tel que l'agent
le connaît déjà : jobs en cours et file, mode de partage, RAM, disque du dossier du Worker, place occupée par les
installations Alpine Makers, GPU et VRAM, versions CUDA et pilote, composants et leur état, IA locales (allumées,
éteintes, installées, associées, ON/OFF), comptes IA du Worker, connexion au site, version et mise à jour.

Règles tenues ici :

- aucun appel réseau, aucune sonde lourde dans le fil de la demande : tout vient de l'instantané d'inventaire de
  l'agent, du dernier état lu sur le site (``self_state``) et de deux mesures faites dans leur propre fil, gardées en
  mémoire (place occupée : toutes les 30 minutes ; comptes IA : toutes les 15 minutes, seulement si on les regarde) ;
- jamais de jeton, de clé, d'identité, d'adresse du site, de chemin absolu ni de nom de compte d'un tiers ;
- la mesure de la place ne suit jamais un lien ni une jonction, reste dans le dossier du Worker et s'arrête à un
  budget de fichiers et de temps (résultat marqué « partiel »).

Le heartbeat porte le résumé de la place occupée (``storage_for_heartbeat``) : Mes Workers l'affiche aussi.
"""
import os
import re
import stat
import threading
import time
from pathlib import Path

if __package__:
    from .local_control import _agent_version, _heartbeat_view, indicator, v_bool, version_newer
else:
    from local_control import _agent_version, _heartbeat_view, indicator, v_bool, version_newer

STORAGE_PERIOD = 1800          # une mesure de la place occupée toutes les 30 minutes au plus
STORAGE_FIRST_DELAY = 90       # première mesure 90 s après la première demande (démarrage de l'agent plus léger)
STORAGE_REFRESH_MIN = 120      # « refresh » ne relance pas une mesure de moins de deux minutes
STORAGE_MAX_ENTRIES = 3_000_000
STORAGE_MAX_SECONDS = 600
AI_ACCOUNTS_PERIOD = 900
AI_ACCOUNTS_REFRESH_MIN = 60
CATEGORIES = ("engines", "models", "venv", "python", "cache", "outputs", "jobs", "data", "logs", "temp", "other")
CATEGORY_LABELS = {"engines": "Moteurs et modules", "models": "Modèles IA", "venv": "Environnements Python des moteurs",
                   "python": "Python isolé du Worker", "cache": "Caches (Hugging Face, pip, torch…)",
                   "outputs": "Résultats et espace de travail", "jobs": "Jobs", "data": "Données et état de l'agent",
                   "logs": "Journaux", "temp": "Fichiers temporaires", "other": "Agent et divers"}
VENV_NAMES = frozenset({".venv", "venv", "python_embeded", "python_embedded"})
TOP_CATEGORIES = {"cache": "cache", "outputs": "outputs", "workspace": "outputs", "jobs": "jobs", "data": "data",
                  "logs": "logs", "temp": "temp"}
TOOL_LABELS = {"image_generation": "Génération d'images (ComfyUI)", "ai3d": "IA 3D (Hunyuan3D)",
               "converter": "Convertisseur / CNC (FreeCAD)", "printguard": "PrintGuard (surveillance d'impression)",
               "model-studio": "Model Studio (OrcaSlicer)", "laser-studio": "Laser Studio", "cnc-studio": "CNC Studio"}
ENGINE_TOOLS = ("image_generation", "ai3d", "model-studio", "printguard")
SHARING_LABELS = {"prive": "Privé", "comptes": "Comptes choisis", "public": "Public"}
AUDIENCE_LABELS = {"team": "équipe", "contributors": "contributeurs", "everyone": "tous les comptes"}
COMPONENT_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}")
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_TEXT = re.compile(r"[\x00-\x1f\x7f]")


class _Runtime:
    def __init__(self):
        self.guard = threading.Lock()
        self.storage = None            # dernière mesure terminée
        self.storage_running = False
        self.storage_started = None    # time.monotonic() de la dernière mesure lancée
        self.first_seen = time.monotonic()
        self.ai = None
        self.ai_running = False
        self.ai_started = None


_GUARD = threading.Lock()


def _runtime(agent):
    runtime = getattr(agent, "_worker_dashboard", None)
    if not isinstance(runtime, _Runtime):
        with _GUARD:
            runtime = getattr(agent, "_worker_dashboard", None)
            if not isinstance(runtime, _Runtime):
                runtime = agent._worker_dashboard = _Runtime()
    return runtime


def _text(value, limit=120):
    return _TEXT.sub(" ", str(value if value is not None else ""))[:limit]


def _int(value, minimum=0, maximum=1 << 62):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    return number if minimum <= number <= maximum else None


# --- Place occupée par les installations Alpine Makers ------------------------------------------------------------

class _Budget:
    def __init__(self, entries, seconds):
        self.entries, self.deadline, self.partial = entries, time.monotonic() + seconds, False

    def spend(self):
        self.entries -= 1
        if self.entries <= 0 or time.monotonic() > self.deadline:
            self.partial = True
            return False
        return True


def _is_link(entry):
    try:
        if entry.is_symlink():
            return True
        info = entry.stat(follow_symlinks=False)
        return bool(getattr(info, "st_file_attributes", 0) & _REPARSE)
    except OSError:
        return True  # illisible : jamais suivi


def _size(path, budget, skip=()):
    """Octets des fichiers sous ``path`` (dossier), sans suivre de lien ni de jonction. ``skip`` : noms du 1er niveau."""
    total, stack, first = 0, [Path(path)], True
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if not budget.spend():
                        return total
                    if first and entry.name in skip:
                        continue
                    if _is_link(entry):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            pass
        first = False
    return total


def _root_entries(root):
    try:
        with os.scandir(root) as entries:
            return [entry for entry in entries]
    except OSError:
        return []


def measure_storage(root, max_entries=STORAGE_MAX_ENTRIES, max_seconds=STORAGE_MAX_SECONDS):
    """Place occupée dans le dossier du Worker, par catégorie et par composant. Rend le résumé envoyé au site."""
    root = Path(root).resolve()
    budget = _Budget(max_entries, max_seconds)
    categories = {key: 0 for key in CATEGORIES}
    components = []
    for entry in _root_entries(root):
        if _is_link(entry):
            continue
        name = entry.name
        try:
            if entry.is_file(follow_symlinks=False):
                categories["other"] += entry.stat(follow_symlinks=False).st_size
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        lowered = name.casefold()
        if lowered == "components":
            for child in _root_entries(entry.path):
                if _is_link(child) or not child.is_dir(follow_symlinks=False):
                    continue
                folder = Path(child.path)
                venv = 0
                for sub in _root_entries(folder):
                    if sub.name in VENV_NAMES and not _is_link(sub) and sub.is_dir(follow_symlinks=False):
                        venv += _size(sub.path, budget)
                models_dir = folder / "models"
                models = _size(models_dir, budget) if models_dir.is_dir() and not models_dir.is_symlink() else 0
                engine = _size(folder, budget, skip=VENV_NAMES | {"models"})
                categories["engines"] += engine
                categories["models"] += models
                categories["venv"] += venv
                if COMPONENT_ID.fullmatch(child.name.casefold()) and len(components) < 24:
                    components.append({"id": child.name.casefold(), "engine_bytes": engine, "models_bytes": models,
                                       "venv_bytes": venv})
        elif lowered == "runtime":
            for child in _root_entries(entry.path):
                if _is_link(child):
                    continue
                try:
                    if child.is_dir(follow_symlinks=False):
                        key = "python" if child.name.casefold().startswith("python") else "data"
                        categories[key] += _size(child.path, budget)
                    elif child.is_file(follow_symlinks=False):
                        categories["data"] += child.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
        elif lowered == "config":
            continue  # identité et configuration : jamais parcourues (quelques kilo-octets)
        else:
            categories[TOP_CATEGORIES.get(lowered, "other")] += _size(entry.path, budget)
    components.sort(key=lambda item: -(item["engine_bytes"] + item["models_bytes"] + item["venv_bytes"]))
    return {"categories": categories, "components": components, "total_bytes": sum(categories.values()),
            "computed_at": time.time(), "partial": budget.partial}


def _storage_worker(agent, runtime):
    result = None
    try:
        result = measure_storage(agent.root)
    except Exception as error:  # le fil ne sort jamais en exception
        print(f"Mesure de la place occupée interrompue ({type(error).__name__}).", flush=True)
    finally:
        with runtime.guard:
            if result is not None:
                runtime.storage = result
            runtime.storage_running = False


def refresh_storage(agent, force=False):
    """Lance une mesure dans son propre fil si elle est due. Ne bloque jamais."""
    runtime = _runtime(agent)
    now = time.monotonic()
    with runtime.guard:
        if runtime.storage_running or getattr(agent, "disconnecting", False):
            return False
        if runtime.storage_started is None:
            due = force or now - runtime.first_seen >= STORAGE_FIRST_DELAY
        else:
            age = now - runtime.storage_started
            due = age >= STORAGE_PERIOD or (force and age >= STORAGE_REFRESH_MIN)
        if not due:
            return False
        runtime.storage_running, runtime.storage_started = True, now
    try:
        threading.Thread(target=_storage_worker, args=(agent, runtime), name="worker-storage-usage", daemon=True).start()
    except BaseException:
        with runtime.guard:
            runtime.storage_running = False
        raise
    return True


def storage_for_heartbeat(agent):
    """Dernière mesure terminée, sous la forme lue par le site (_storage_usage), ou None. Lance la suivante si due."""
    refresh_storage(agent)
    runtime = _runtime(agent)
    with runtime.guard:
        usage = runtime.storage
    if not usage:
        return None
    return {"categories": dict(usage["categories"]), "components": [dict(item) for item in usage["components"]],
            "computed_at": usage["computed_at"], "partial": usage["partial"]}


def _storage_view(agent):
    runtime = _runtime(agent)
    with runtime.guard:
        usage, running = runtime.storage, runtime.storage_running
    if not usage:
        return {"state": "computing" if running else "pending", "total_bytes": None, "items": [], "components": [],
                "computed_at": None, "partial": False}
    items = [{"key": key, "label": CATEGORY_LABELS[key], "bytes": usage["categories"].get(key, 0)}
             for key in CATEGORIES if usage["categories"].get(key, 0) > 0]
    items.sort(key=lambda item: -item["bytes"])
    return {"state": "computing" if running else "ready", "total_bytes": usage["total_bytes"], "items": items,
            "components": [dict(item) for item in usage["components"]], "computed_at": usage["computed_at"],
            "partial": usage["partial"]}


# --- Comptes IA du Worker (Claude Code, Codex) --------------------------------------------------------------------

def _ai_worker(agent, runtime):
    snapshot = None
    try:
        snapshot = agent.ai_account_manager().snapshot()
    except Exception as error:
        snapshot = {"error": type(error).__name__}
    finally:
        with runtime.guard:
            runtime.ai = snapshot
            runtime.ai_running = False


def _ai_view(agent, force):
    runtime = _runtime(agent)
    now = time.monotonic()
    launch = False
    with runtime.guard:
        if not runtime.ai_running and callable(getattr(agent, "ai_account_manager", None)):
            age = None if runtime.ai_started is None else now - runtime.ai_started
            if age is None or age >= AI_ACCOUNTS_PERIOD or (force and age >= AI_ACCOUNTS_REFRESH_MIN):
                runtime.ai_running, runtime.ai_started, launch = True, now, True
        snapshot, running = runtime.ai, runtime.ai_running
    if launch:
        try:
            threading.Thread(target=_ai_worker, args=(agent, runtime), name="worker-dashboard-ai", daemon=True).start()
        except BaseException:
            with runtime.guard:
                runtime.ai_running = False
            raise
    if not isinstance(snapshot, dict):
        return {"state": "computing" if running or launch else "unknown", "cli": {}, "accounts": [], "checked_at": None}
    if snapshot.get("error"):
        return {"state": "error", "cli": {}, "accounts": [], "checked_at": None,
                "message": "Comptes IA illisibles sur ce Worker (" + _text(snapshot["error"], 40) + ")."}
    cli = {}
    for provider, facts in (snapshot.get("cli") or {}).items() if isinstance(snapshot.get("cli"), dict) else ():
        if isinstance(facts, dict):
            cli[_text(provider, 16)] = {"installed": facts.get("installed") is True, "version": _text(facts.get("version"), 40)}
    accounts = []
    for row in snapshot.get("accounts") or [] if isinstance(snapshot.get("accounts"), list) else []:
        if not isinstance(row, dict):
            continue
        accounts.append({"provider": _text(row.get("provider"), 16), "label": _text(row.get("label"), 60),
                         "default": row.get("default") is True, "logged_in": row.get("logged_in") is True,
                         "method": _text(row.get("method"), 30), "message": _text(row.get("message"), 160)})
    return {"state": "computing" if running else "ready", "cli": cli, "accounts": accounts[:16],
            "checked_at": snapshot.get("checked_at") if isinstance(snapshot.get("checked_at"), (int, float)) else None}


# --- Le reste vient de ce que l'agent sait déjà -------------------------------------------------------------------

def _site(agent):
    state = getattr(agent, "self_state", None)
    site = state.get("site") if isinstance(state, dict) else None
    return state if isinstance(state, dict) else {}, site if isinstance(site, dict) else {}


def _connection(agent):
    state, site = _site(agent)
    beat = None
    try:
        beat = _heartbeat_view(agent)
    except Exception:
        beat = None
    fetched = state.get("fetched_at") if isinstance(state.get("fetched_at"), (int, float)) else None
    worker = site.get("worker") if isinstance(site.get("worker"), dict) else {}
    level = "unknown"
    if beat and beat.get("ok") is True:
        level = "ok"
    elif beat and beat.get("ok") is False:
        level = "offline" if beat.get("http") is None else "refused"
    return {"state": level, "heartbeat": beat,
            "site": {"known": bool(site), "fetched_at": fetched,
                     "age_seconds": max(0, int(time.time() - fetched)) if fetched else None,
                     "unreachable": state.get("unreachable") is True, "missing": state.get("missing") is True,
                     "online": worker.get("online") is True if worker else None,
                     "session_required": site.get("session_required") is True}}


def _jobs(agent):
    _, site = _site(agent)
    active = []
    try:
        for job_id, item in agent.journal.records("jobs").items():
            if not agent._job_active(item) or not agent._job_identity_matches(item):
                continue
            active.append({"job_id": _text(job_id, 13), "tool_id": _text(item.get("tool_id"), 40),
                           "tool": TOOL_LABELS.get(str(item.get("tool_id") or ""), _text(item.get("tool_id"), 40)),
                           "state": _text(item.get("state"), 30)})
    except Exception:
        active = []
    jobs = site.get("jobs") if isinstance(site.get("jobs"), dict) else {}
    desktop = getattr(agent, "_desktop_tasks", None)
    desktop_running = sum(1 for task in list(desktop.values()) if getattr(task, "state", "") == "running") if isinstance(desktop, dict) else 0
    try:
        grbl = [_text(name, 80) for name in (agent.active_grbl_jobs() or [])][:16]
    except Exception:
        grbl = []
    return {"active": active[:32], "count": len(active), "site_active": _int(jobs.get("active")),
            "site_queued": _int(jobs.get("queued")), "max_concurrent": _int(jobs.get("max_concurrent_jobs"))
            or _int((getattr(agent, "config", {}) or {}).get("max_concurrent_jobs")) or 1,
            "desktop_tasks": desktop_running, "grbl_jobs": grbl,
            "maintenance": bool(getattr(agent, "maintenance_active", False)),
            "maintenance_pending": jobs.get("maintenance_pending") is True}


def _sharing(agent):
    _, site = _site(agent)
    sharing = site.get("sharing") if isinstance(site.get("sharing"), dict) else None
    if sharing is None:
        return {"known": False, "mode": "", "label": "inconnu (site pas encore lu)", "audiences": [], "tools": [],
                "auto_accept_jobs": False, "users_count": 0}
    mode = sharing.get("mode") if sharing.get("mode") in SHARING_LABELS else ""
    audiences = [name for name in AUDIENCE_LABELS if name in (sharing.get("audiences") or [])]
    label = SHARING_LABELS.get(mode, "inconnu")
    if mode == "public" and audiences:
        label += " (" + ", ".join(AUDIENCE_LABELS[name] for name in audiences) + ")"
    return {"known": True, "mode": mode, "label": label, "audiences": audiences,
            "tools": [_text(tool, 40) for tool in sharing.get("tools") or []][:8],
            "auto_accept_jobs": sharing.get("auto_accept_jobs") is True, "users_count": _int(sharing.get("users_count")) or 0}


def _snapshot(agent):
    lock = getattr(agent, "inventory_lock", None)
    if lock is None:
        return {}
    with lock:
        snapshot = getattr(agent, "inventory_snapshot", None) or {}
        return {"ram": dict(snapshot.get("ram") or {}), "disk": dict(snapshot.get("disk") or {}),
                "gpus": [dict(item) for item in snapshot.get("gpus") or [] if isinstance(item, dict)],
                "cuda_version": snapshot.get("cuda_version"), "driver_version": snapshot.get("driver_version"),
                "tools": [dict(item, config=dict(item.get("config") or {})) for item in snapshot.get("tools") or [] if isinstance(item, dict)],
                "models": [dict(item) for item in snapshot.get("models") or [] if isinstance(item, dict)],
                "cpu": dict(snapshot.get("cpu") or {})}


def _memory(snapshot):
    ram = snapshot.get("ram") or {}
    total, free = _int(ram.get("total_mb")), _int(ram.get("free_mb"))
    if not total:
        return {"total_mb": None, "used_mb": None, "free_mb": free, "percent": None}
    free = min(free or 0, total)
    return {"total_mb": total, "used_mb": total - free, "free_mb": free, "percent": int(round(100 * (total - free) / total))}


def _disk(agent, snapshot):
    disk = snapshot.get("disk") or {}
    total, free = _int(disk.get("total_bytes")), _int(disk.get("free_bytes"))
    try:
        import shutil
        usage = shutil.disk_usage(agent.root)  # appel système instantané, toujours à jour
        total, free = usage.total, usage.free
    except (OSError, AttributeError, TypeError):
        pass
    if not total:
        return {"total_bytes": None, "free_bytes": free, "used_bytes": None, "percent": None}
    return {"total_bytes": total, "free_bytes": free, "used_bytes": total - (free or 0),
            "percent": int(round(100 * (total - (free or 0)) / total))}


def _gpus(snapshot):
    gpus = []
    for gpu in snapshot.get("gpus") or []:
        total, free = _int(gpu.get("vram_total_mb")), _int(gpu.get("vram_free_mb"))
        gpus.append({"name": _text(gpu.get("name"), 80), "vram_total_mb": total, "vram_free_mb": free,
                     "vram_used_mb": (total - free) if total and free is not None else None,
                     "utilization_percent": _int(gpu.get("utilization_percent"), 0, 100),
                     "temperature_c": _int(gpu.get("temperature_c"), 0, 150)})
    return gpus[:8]


def _components(agent, snapshot):
    _, site = _site(agent)
    local_ai = site.get("local_ai") if isinstance(site.get("local_ai"), dict) else {}
    associated = set(local_ai.get("associated_tool_ids") or []) if local_ai else None
    off = {(str(item.get("kind")), str(item.get("id"))) for item in local_ai.get("off") or [] if isinstance(item, dict)} if local_ai else set()
    rows = []
    for tool in snapshot.get("tools") or []:
        tool_id = str(tool.get("tool_id") or "")
        if not tool_id:
            continue
        config = tool.get("config") or {}
        facts = config.get("installation") if isinstance(config.get("installation"), dict) else {}
        running = None
        port = None
        if tool_id in ENGINE_TOOLS:
            try:
                running = bool(agent._runtime_running(tool_id))
                port = agent._runtime_port(tool_id)
            except Exception:
                running = None
        status = str(tool.get("status") or "")
        installed = status in {"ready", "online"} or facts.get("engine_status") == "installed"
        modules = facts.get("modules") if isinstance(facts.get("modules"), dict) else {}
        rows.append({"tool_id": _text(tool_id, 40), "name": TOOL_LABELS.get(tool_id, _text(tool_id, 40)),
                     "engine_id": _text(tool.get("engine_id"), 40), "status": _text(status, 20), "installed": installed,
                     "running": running, "port": port if isinstance(port, int) and not isinstance(port, bool) else None,
                     "runtime_enabled": config.get("runtime_enabled") if isinstance(config.get("runtime_enabled"), bool) else None,
                     "associated": (tool_id in associated) if associated is not None else None,
                     "repair_required": facts.get("repair_required") is True,
                     "size_bytes": _int(facts.get("size_bytes")) or 0,
                     "modules": [{"id": _text(name, 40), "state": _text(state, 20), "off": ("component", str(name)) in off}
                                 for name, state in sorted(modules.items())][:12]})
    models = snapshot.get("models") or []
    by_tool = {}
    for model in models:
        key = str(model.get("tool_id") or "")
        entry = by_tool.setdefault(key, {"tool_id": _text(key, 40), "installed": 0, "off": 0, "bytes": 0})
        if str(model.get("status") or "") in {"ready", "online", "installed"}:
            entry["installed"] += 1
            entry["bytes"] += _int(model.get("size_bytes")) or 0
        if ("model", str(model.get("model_id") or "")) in off:
            entry["off"] += 1
    return rows, {"by_tool": sorted(by_tool.values(), key=lambda item: item["tool_id"]),
                  "installed": sum(item["installed"] for item in by_tool.values()),
                  "off": sum(item["off"] for item in by_tool.values()),
                  "site_known": bool(local_ai)}


def _fire_watch(agent):
    watch = getattr(agent, "fire_watch", None)
    if watch is None:
        return {"state": "not_started"}
    try:
        status = watch.local_status()
    except Exception:
        return {"state": "error"}
    return {"state": _text(status.get("state"), 30) if isinstance(status, dict) else "unknown"}


def _agent(agent):
    version = _agent_version(agent)
    available = getattr(agent, "available_agent_version", "")
    _, site = _site(agent)
    worker = site.get("worker") if isinstance(site.get("worker"), dict) else {}
    if not available and isinstance(worker.get("available_agent_version"), str):
        available = worker["available_agent_version"]
    update = available if isinstance(available, str) and version_newer(version, available) else ""
    return {"version": version or "", "available_version": _text(available, 30) if isinstance(available, str) else "",
            "update_available": update, "pid": os.getpid(),
            "maintenance_active": bool(getattr(agent, "maintenance_active", False)),
            "disconnecting": bool(getattr(agent, "disconnecting", False))}


def _guard(view, *args):
    try:
        return view(*args)
    except Exception:
        return None


def dashboard(agent, params):
    """État complet du Worker, lecture seule, sans réseau. ``refresh`` relance les mesures d'arrière-plan si possible."""
    force = bool((params or {}).get("refresh", False))
    snapshot = _guard(_snapshot, agent) or {}
    refresh_storage(agent, force=force)
    components = _guard(_components, agent, snapshot)
    rows, models = components if components else ([], {"by_tool": [], "installed": 0, "off": 0, "site_known": False})
    cpu = snapshot.get("cpu") or {}
    return {"checked_at": time.time(), "agent": _guard(_agent, agent), "connection": _guard(_connection, agent),
            "indicator": _guard(indicator, agent), "jobs": _guard(_jobs, agent), "sharing": _guard(_sharing, agent),
            "ram": _memory(snapshot), "disk": _guard(_disk, agent, snapshot), "storage": _guard(_storage_view, agent),
            "gpus": _gpus(snapshot), "cuda_version": _text(snapshot.get("cuda_version"), 30),
            "driver_version": _text(snapshot.get("driver_version"), 30),
            "cpu": {"name": _text(cpu.get("name"), 80), "cores": _int(cpu.get("cores"), 1, 4096)},
            "components": rows, "models": models, "ai_accounts": _guard(_ai_view, agent, force),
            "fire_watch": _guard(_fire_watch, agent), "inventory_known": bool(snapshot.get("tools") or snapshot.get("gpus"))}


LOCAL_ACTIONS = {
    "dashboard": {"handler": dashboard, "params": {"refresh": v_bool}, "read": True},
}
