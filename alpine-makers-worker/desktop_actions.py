"""Socle des outils d'Alpine Makers Desktop (worker.exe), agent 1.50.22.

Une seule action locale, ``desktop`` (``local_control.LAZY_ACTIONS``), sert TOUS les outils natifs de worker.exe :

    python local_control.py --action desktop --json --params-b64 <{"tool": ..., "name": ..., "params": {...}}>

``tool`` désigne un module de la liste fixe ``TOOL_MODULES`` ci-dessous ; ``name`` une action du dictionnaire
``DESKTOP_ACTIONS`` de ce module ; ``params`` est validé champ par champ par les validateurs que l'action déclare
(mêmes règles que les autres actions locales : clé inconnue = refus, valeur nettoyée par son validateur).

Ajouter un outil côté agent (rien d'autre à toucher ici) :

1. créer ``worker_agent/desktop_<outil>.py`` avec ::

       from desktop_actions import v_text, ...        # (ou « from .desktop_actions import » en paquet)
       DESKTOP_ACTIONS = {
           "etat": {"handler": etat, "params": {"page": v_int(1, 1000)}, "read": True},
       }

   ``handler(agent, params) -> dict`` rend en moins de 2 secondes : un travail long passe par ``start_task`` ;
2. ajouter UNE ligne à ``TOOL_MODULES`` : ``"mon-outil": "desktop_mon_outil",``.

Gros paramètres : ``args_file`` (32 hexadécimaux) au lieu de ``params`` → ``runtime/local-control/<hex>.args.json``
écrit par worker.exe, lu une fois puis effacé (même mécanisme que ``login`` et ``manage``), au plus ``ARGS_MAX_BYTES``.
Gros résultats : ``result_file(agent, value)`` écrit ``runtime/desktop/out/<hex>.json`` et rend
``{"result_file": "<hex>"}`` ; worker.exe le lit puis l'efface.

Aucune commande physique dans ce module ni dans le socle : un outil qui en aurait besoin passe par les gardes de
l'agent (équipements) et une confirmation explicite dans l'interface.
"""
import importlib
import re
import secrets
import threading
import time

if __package__:
    from .job_state import atomic_json
    from .local_control import checked, v_bool, v_choice, v_hex, v_list_of
else:
    from job_state import atomic_json
    from local_control import checked, v_bool, v_choice, v_hex, v_list_of

ACTION = "desktop"
# Liste fixe des modules d'outils : identifiant (celui de worker.exe, IDesktopTool.AgentTool) -> module.
# Chaque nouvel outil ajoute SA ligne ici et rien d'autre dans ce fichier.
TOOL_MODULES = {
    "core": None,                       # tâches du socle (ce module)
    "library": "desktop_library",       # Bibliothèque du compte (routes signées self/library*)
    "laser-studio": "desktop_laser_studio",     # Alpine Laser Studio natif (moteur laser local, graveuse par self/laser-studio)
    "model-studio": "desktop_model_studio",   # Alpine Model Studio natif (Orca, FreeCAD, réparation, impression par le site)
    "image-lab": "desktop_image_lab",
    "cnc-studio": "desktop_cnc_studio",       # Alpine CNC Studio natif (analyse locale, projets/CAM/commandes par self/cnc-studio*)
    "converter": "desktop_converter",
}
ARGS_MAX_BYTES = 1024 * 1024            # fichier d'arguments d'un outil
OUT_DIR = "runtime/desktop/out"         # résultats volumineux, lus puis effacés par worker.exe
OUT_LIFETIME = 3600                     # un résultat non relu est effacé au bout d'une heure
TASKS_MAX = 32                          # tâches gardées en mémoire (les terminées les plus anciennes partent d'abord)
TASK_LIFETIME = 1800                    # une tâche terminée reste consultable trente minutes
TASKS_RUNNING_MAX = 4                   # tâches simultanées, tous outils confondus
_NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")
_TEXT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# --- Validateurs communs (en plus de v_bool, v_choice, v_hex, v_list_of de local_control) -------------------------

def v_text(maximum, *, empty=True):
    """Chaîne d'au plus ``maximum`` caractères, sans caractère de contrôle (sauts de ligne permis)."""
    def check(value):
        if type(value) is not str or len(value) > maximum or _TEXT_CONTROL.search(value):
            raise ValueError(f"Paramètre local invalide : texte d'au plus {maximum} caractères attendu.")
        if not empty and not value.strip():
            raise ValueError("Paramètre local invalide : texte vide.")
        return value
    return check


def v_int(minimum, maximum):
    """Entier JSON (jamais un booléen ni une chaîne) borné."""
    def check(value):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"Paramètre local invalide : entier de {minimum} à {maximum} attendu.")
        return value
    return check


def v_session(value):
    """Jeton de session de worker.exe (rendu par « login ») : transmis au site, jamais journalisé."""
    if type(value) is not str or not 16 <= len(value) <= 200 or not re.fullmatch(r"[A-Za-z0-9_\-]+", value):
        raise ValueError("Session worker.exe illisible : reconnecte-toi.")
    return value


def v_object(depth=5, keys=200):
    """Objet JSON simple (chaînes, nombres, booléens, null, listes, objets), profondeur et taille bornées."""
    def walk(value, level):
        if level > depth:
            raise ValueError("Paramètre local trop imbriqué.")
        if isinstance(value, dict):
            if len(value) > keys:
                raise ValueError("Paramètre local trop gros.")
            for key, item in value.items():
                if type(key) is not str or len(key) > 80:
                    raise ValueError("Paramètre local invalide.")
                walk(item, level + 1)
        elif isinstance(value, list):
            if len(value) > 10000:
                raise ValueError("Paramètre local trop gros.")
            for item in value:
                walk(item, level + 1)
        elif value is not None and type(value) not in (str, int, float, bool):
            raise ValueError("Paramètre local invalide.")

    def check(value):
        if type(value) is not dict:
            raise ValueError("Paramètre local invalide : un objet est attendu.")
        walk(value, 0)
        return value
    return check


def v_tool(value):
    if type(value) is not str or value not in TOOL_MODULES:
        raise ValueError("Outil Desktop inconnu de cet agent : mets le Worker à jour.")
    return value


def v_name(value):
    if type(value) is not str or not _NAME.fullmatch(value):
        raise ValueError("Action Desktop invalide.")
    return value


# --- Fichiers du runtime protégé ------------------------------------------------------------------------------------

def args_from_file(agent, token, limit=ARGS_MAX_BYTES):
    """Lit puis efface ``runtime/local-control/<token>.args.json`` (objet JSON). Effacé même illisible ou trop gros."""
    if __package__:
        from . import self_service
    else:
        import self_service
    return self_service.args_from_file(agent, token, v_object(), limit)


def result_file(agent, value):
    """Écrit un résultat volumineux dans ``runtime/desktop/out/<hex>.json`` ; rend ``{"result_file": "<hex>"}``."""
    folder = checked(agent.root, OUT_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for old in folder.glob("*.json"):  # résultats jamais relus : ménage, sans suivre de lien
        try:
            if now - old.stat().st_mtime > OUT_LIFETIME:
                old.unlink()
        except OSError:
            pass
    token = secrets.token_hex(16)
    atomic_json(checked(agent.root, f"{OUT_DIR}/{token}.json"), value)
    return {"result_file": token}


def runtime_folder(agent, *parts):
    """Dossier ``runtime/desktop/<parts…>`` du Worker (créé), vérifié par ``checked`` (jamais hors du dossier)."""
    for part in parts:
        if not isinstance(part, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", part):
            raise ValueError("Dossier Desktop invalide.")
    folder = checked(agent.root, "/".join(("runtime", "desktop") + tuple(parts)))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# --- Tâches : un travail long dans un fil démon, suivi par « core » / « task » ---------------------------------------

class Task:
    def __init__(self, tool, label):
        self.id = secrets.token_hex(16)
        self.tool = tool
        self.label = label
        self.state = "running"          # running, done, failed, cancelled
        self.progress = None            # 0..100 ou None (inconnue)
        self.message = ""
        self.result = None
        self.error = ""
        self.code = ""
        self.cancel = threading.Event()
        self.started = time.monotonic()
        self.finished = None

    def update(self, progress=None, message=None):
        """Appelé par le travail : progression 0-100 (None = inconnue) et court message pour l'interface."""
        if progress is not None:
            self.progress = max(0, min(100, int(progress)))
        if message is not None:
            self.message = str(message)[:300]

    def cancelled(self):
        return self.cancel.is_set()

    def view(self):
        now = time.monotonic()
        return {"task": self.id, "tool": self.tool, "label": self.label, "state": self.state, "progress": self.progress,
                "message": self.message, "result": self.result if self.state == "done" else None, "error": self.error,
                "code": self.code, "elapsed_s": round((self.finished or now) - self.started, 1)}


class TaskCancelled(Exception):
    """Levée par un travail qui voit ``task.cancelled()`` : la tâche finit « cancelled »."""


class TaskError(Exception):
    """Erreur prévue d'un travail : message pour l'interface et code stable (``code``)."""

    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


_GUARD = threading.Lock()          # table des tâches (non réentrant : ne jamais le reprendre en le tenant)
_TABLE_INIT = threading.Lock()     # création de la table seulement ; _tasks() peut être appelé en tenant _GUARD


def _tasks(agent):
    tasks = getattr(agent, "_desktop_tasks", None)
    if not isinstance(tasks, dict):
        with _TABLE_INIT:
            tasks = getattr(agent, "_desktop_tasks", None)
            if not isinstance(tasks, dict):
                tasks = agent._desktop_tasks = {}
    return tasks


def _expire(tasks, now):
    for key in [key for key, task in tasks.items() if task.finished is not None and now - task.finished > TASK_LIFETIME]:
        del tasks[key]
    finished = sorted((task for task in tasks.values() if task.finished is not None), key=lambda task: task.finished)
    while len(tasks) >= TASKS_MAX and finished:
        del tasks[finished.pop(0).id]


def start_task(agent, tool, label, work):
    """Lance ``work(task)`` dans un fil démon et rend aussitôt ``{"task": id, "state": "running"}``.

    ``work`` rend le résultat (objet JSON, petit : sinon ``result_file``), lève ``TaskError`` pour un refus prévu,
    ``TaskCancelled`` s'il voit ``task.cancelled()``. Aucune exception ne sort du fil."""
    tasks = _tasks(agent)
    task = Task(tool, str(label)[:120])
    with _GUARD:
        _expire(tasks, time.monotonic())
        if sum(1 for item in tasks.values() if item.state == "running") >= TASKS_RUNNING_MAX:
            raise ValueError("Trop de tâches Desktop en cours : attends la fin de l'une d'elles.")
        tasks[task.id] = task

    def run():
        try:
            result = work(task)
            if task.cancelled():
                raise TaskCancelled()
            task.result, task.state = result, "done"
            task.progress = 100
        except TaskCancelled:
            task.state, task.error = "cancelled", "Annulée."
        except TaskError as error:
            task.state, task.error, task.code = "failed", str(error)[:500], error.code[:60]
        except Exception as error:  # filet : le message d'une erreur réseau peut nommer une adresse, seul le type part
            task.state, task.error = "failed", f"Tâche interrompue ({type(error).__name__})."
            print(f"Tâche Desktop interrompue ({tool} : {type(error).__name__}).", flush=True)
        finally:
            task.finished = time.monotonic()

    try:
        threading.Thread(target=run, name="desktop-task-" + tool, daemon=True).start()
    except BaseException:
        with _GUARD:
            tasks.pop(task.id, None)
        raise
    return {"task": task.id, "state": "running"}


def _task(agent, params):
    with _GUARD:
        task = _tasks(agent).get(params["task"])
    if task is None:
        raise ValueError("Tâche inconnue ou expirée.")
    return task.view()


def _list_tasks(agent, params):
    with _GUARD:
        _expire(_tasks(agent), time.monotonic())
        items = [task.view() for task in _tasks(agent).values()
                 if "tool" not in params or task.tool == params["tool"]]
    for item in items:
        item["result"] = None  # la liste ne recopie pas les résultats : « task » les rend
    return {"tasks": sorted(items, key=lambda item: -item["elapsed_s"])}


def _cancel(agent, params):
    """Demande l'arrêt : le travail le voit à son prochain point de contrôle (pas d'arrêt brutal d'un fil)."""
    with _GUARD:
        task = _tasks(agent).get(params["task"])
    if task is None:
        raise ValueError("Tâche inconnue ou expirée.")
    task.cancel.set()
    return task.view()


def _ping(agent, params):
    """Socle présent : version de l'agent et outils connus (pour la page « bientôt disponible » honnête)."""
    module = __import__("sys").modules.get("agent") or __import__("sys").modules.get("worker_agent.agent")
    return {"agent_version": str(getattr(module, "AGENT_VERSION", "") or ""), "tools": sorted(TOOL_MODULES)}


CORE_ACTIONS = {
    "ping": {"handler": _ping, "params": {}, "read": True},
    "task": {"handler": _task, "params": {"task": v_hex(32)}, "read": True},
    "tasks": {"handler": _list_tasks, "params": {"tool": v_tool}, "read": True},
    "cancel": {"handler": _cancel, "params": {"task": v_hex(32)}, "read": True},
}


# --- Aiguillage ----------------------------------------------------------------------------------------------------

def _import(module):
    try:
        return importlib.import_module("." + module, __package__) if __package__ else importlib.import_module(module)
    except ImportError as error:
        raise ValueError(f"Fichier de l'agent manquant ({module}.py) : lance « Réparer ».") from error


def tool_actions(tool):
    """Le dictionnaire DESKTOP_ACTIONS de l'outil (vérifié : handler appelable, params = validateurs)."""
    module = TOOL_MODULES[tool]
    actions = CORE_ACTIONS if module is None else getattr(_import(module), "DESKTOP_ACTIONS", None)
    if not isinstance(actions, dict):
        raise ValueError("Outil Desktop sans actions.")
    return actions


def _spec(tool, name):
    spec = tool_actions(tool).get(name)
    if (not isinstance(spec, dict) or not callable(spec.get("handler")) or not isinstance(spec.get("params"), dict)
            or not all(callable(item) for item in spec["params"].values())):
        raise ValueError("Action Desktop inconnue de cet agent : mets le Worker à jour.")
    return spec


def clean(spec, params):
    cleaned = {}
    for key in sorted(params):
        validator = spec["params"].get(key)
        if not callable(validator):
            raise ValueError("Paramètre local non autorisé : " + str(key)[:40])
        cleaned[key] = validator(params[key])
    return cleaned


def desktop(agent, params):
    """Aiguille ``{tool, name, params | args_file}`` vers l'action déclarée par l'outil, après validation."""
    if params is None or not isinstance(params, dict):
        raise ValueError("Paramètres locaux illisibles.")
    outer = clean(LOCAL_ACTIONS[ACTION], params)  # seconde barrière : local_control a déjà validé
    if "tool" not in outer or "name" not in outer:
        raise ValueError("Outil ou action Desktop manquant.")
    if "params" in outer and "args_file" in outer:
        raise ValueError("Paramètres en double : « params » ou « args_file », pas les deux.")
    spec = _spec(outer["tool"], outer["name"])
    inner = args_from_file(agent, outer["args_file"]) if "args_file" in outer else outer.get("params", {})
    return spec["handler"](agent, clean(spec, inner))


LOCAL_ACTIONS = {
    # « read » : aucun heartbeat forcé après la demande (un outil qui change l'état du Worker le demande lui-même).
    ACTION: {"handler": desktop, "read": True,
             "params": {"tool": v_tool, "name": v_name, "params": v_object(), "args_file": v_hex(32)}},
}

__all__ = ["v_bool", "v_choice", "v_hex", "v_list_of", "v_text", "v_int", "v_session", "v_object", "start_task",
           "TaskError", "TaskCancelled", "result_file", "runtime_folder", "args_from_file", "TOOL_MODULES"]
