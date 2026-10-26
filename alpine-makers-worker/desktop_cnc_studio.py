"""Alpine CNC Studio pour Alpine Makers Desktop (worker.exe), outil « cnc-studio » de ``desktop_actions``.

Ce qui se fait sur ce PC, sans le site (hors ligne compris) :

- ``machines`` : les CNC de ce Worker telles que l'agent les connaît (liaison, état GRBL, positions, travail en cours,
  réglages de la fiche utiles au studio : courses, avances, broche, Z de sécurité, contrôleur, sécurités nommées) ;
- ``stage`` : un programme G-code choisi dans worker.exe (déposé dans ``runtime/desktop/inbox/<hex>/<nom>``) passe dans
  le cache des programmes ``runtime/desktop/cache/cnc-studio/programs/<jeton>.nc`` ;
- ``analyze`` : analyse ou simulation par le moteur CNC du Worker (``cnc_engine.cli``, qui relit le programme avec
  ``cam_gcode_check``) : erreurs, avertissements, étendue, bornes, durée, outils, broche, parcours échantillonné pour
  l'aperçu 2D / 3D. Le Python du composant ``cnc-engine`` s'il est installé, sinon celui de l'agent (même code,
  bibliothèque standard seule) ; ``engine`` dit lequel a tourné. Aucune commande vers une machine.

Ce qui n'existe que sur le site passe par les routes signées ``/api/worker-protocol/self/cnc-studio*``
(``worker_desktop_cnc_studio.py``), qui appellent les MÊMES fonctions que la page CNC Studio : projets, bibliothèques
d'outils et de matériaux, Préparer pour une CNC (calcul FreeCAD sur CE Worker, suivi, acquittement, artefacts),
vérification pour un contrôleur, supervision, et commandes GRBL (« command »). Pour ces dernières, check-list nommée,
bornes course / broche, avertissements à acquitter et propriété de la fiche sont contrôlés par le site ; l'agent exige en
plus ``confirmed: true`` (posé par l'interface après confirmation explicite) pour tout ce qui fait bouger la machine ou
tourner la broche. ARRÊT, pause, état et annulation d'un jog n'en demandent pas. Rien n'est jamais rejoué ni mis en file
pour un retour de connexion.

Toutes les actions du site exigent ``session_token`` (connexion du propriétaire dans worker.exe) et ouvrent une tâche.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

if __package__:
    from . import desktop_actions as desk
    from .desktop_actions import TaskCancelled, TaskError, v_bool, v_choice, v_hex, v_int, v_object, v_session, v_text
    from .local_control import checked
    from .safety import is_link, protect_credentials
else:
    import desktop_actions as desk
    from desktop_actions import TaskCancelled, TaskError, v_bool, v_choice, v_hex, v_int, v_object, v_session, v_text
    from local_control import checked
    from safety import is_link, protect_credentials

TOOL = "cnc-studio"
STUDIO_PATH = "/api/worker-protocol/self/cnc-studio"
DOWNLOAD_PATH = "/api/worker-protocol/self/cnc-studio/download"
LIBRARY_PATH = "/api/worker-protocol/self/library"
LIBRARY_DOWNLOAD_PATH = "/api/worker-protocol/self/library/download"
PROGRAM_EXTENSIONS = (".nc", ".gcode", ".ngc", ".tap", ".txt", ".cnc", ".gc")
PROGRAM_MAX = 64 * 1024 * 1024          # même plafond que le moteur CNC
SITE_GCODE_MAX = 5 * 1024 * 1024        # requête signée du site : 6 Mio au plus, échappements JSON compris
DOWNLOAD_MAX = 64 * 1024 * 1024
CACHE_LIFETIME = 24 * 3600
CACHE_FILES_MAX = 200
ENGINE_TIMEOUT = 600
CHUNK = 256 * 1024
REF = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
_FILE_NAME = re.compile(r"[^\\/:*?\"<>|\x00-\x1f]{1,200}")
# Réglages de fiche utiles au studio (jamais l'hôte, le port série ni une adresse).
SETTING_KEYS = ("work_area_mm", "max_feed_mm_min", "spindle_max_rpm", "spindle_min_rpm", "spindle_control", "safe_z_mm",
                "jog_feed_mm_min", "rapid_feed_mm_min", "controller", "interlocks", "homing", "origin", "coolant_flood",
                "coolant_mist", "tool_change", "probe_plate_mm", "spindle_warmup_s", "spindle_s_max", "model")
# Commandes qui ne bougent rien ou qui arrêtent : pas de confirmation exigée (ARRÊT doit rester immédiat).
UNCONFIRMED = ("stop", "hold", "status", "info", "jog_cancel")


# --- validateurs ------------------------------------------------------------------------------------------------

def v_ref(value):
    if type(value) is not str or not REF.fullmatch(value) or set(value) == {"."}:
        raise ValueError("Identifiant invalide.")
    return value


def v_file_name(value):
    if type(value) is not str or not _FILE_NAME.fullmatch(value) or value in {".", ".."} or value != value.strip():
        raise ValueError("Nom de fichier invalide.")
    return value


def v_program(value):
    return v_hex(32)(value)


def v_true(value):
    if value is not True:
        raise ValueError("Action non confirmée : confirme-la dans l'interface.")
    return value


def v_codes(value):
    if type(value) is not list or not 1 <= len(value) <= 64 or any(type(item) is not str or not 1 <= len(item) <= 80 for item in value):
        raise ValueError("Liste d'avertissements invalide.")
    return value


def v_strings(maximum, length):
    def check(value):
        if type(value) is not list or len(value) > maximum or any(type(item) is not str or not 1 <= len(item) <= length for item in value):
            raise ValueError("Liste invalide.")
        return value
    return check


# --- cache des programmes ---------------------------------------------------------------------------------------

def _cache(agent, *parts):
    return desk.runtime_folder(agent, "cache", "cnc-studio", *parts)


def _tidy(folder):
    """Ménage du cache : fichiers de plus de 24 h, et au plus CACHE_FILES_MAX fichiers (les plus anciens partent)."""
    now = time.time()
    try:
        files = sorted((path for path in folder.iterdir() if path.is_file() and not is_link(path)), key=lambda path: path.stat().st_mtime)
    except OSError:
        return
    for index, path in enumerate(files):
        try:
            if now - path.stat().st_mtime > CACHE_LIFETIME or len(files) - index > CACHE_FILES_MAX:
                path.unlink()
        except OSError:
            pass


def program_path(agent, token):
    path = checked(agent.root, f"runtime/desktop/cache/cnc-studio/programs/{token}.nc")
    if is_link(path) or not path.is_file():
        raise ValueError("Programme introuvable dans le cache : rouvre-le.")
    return path


def _new_program(agent):
    folder = _cache(agent, "programs")
    _tidy(folder)
    token = secrets.token_hex(16)
    return token, checked(agent.root, f"runtime/desktop/cache/cnc-studio/programs/{token}.nc")


def _program_view(token, path, name, source, extra=None):
    raw = path.read_bytes()
    view = {"program": token, "name": name, "source": source, "path": str(path), "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), "lines": raw.count(b"\n") + (1 if raw and not raw.endswith(b"\n") else 0)}
    view.update(extra or {})
    return view


def _store_program(agent, data, name, source, extra=None):
    if not data:
        raise TaskError("Programme vide.", "GCODE_EMPTY")
    if len(data) > PROGRAM_MAX:
        raise TaskError("Programme trop volumineux (64 Mio au plus).", "GCODE_TOO_LARGE")
    token, path = _new_program(agent)
    partial = path.with_name(path.name + ".part")
    partial.write_bytes(data)
    os.replace(partial, path)
    return _program_view(token, path, name, source, extra)


def program_text(agent, token, limit=SITE_GCODE_MAX):
    try:
        path = program_path(agent, token)
    except ValueError as error:
        raise TaskError(str(error), "PROGRAM_MISSING") from None
    size = path.stat().st_size
    if size > limit:
        raise TaskError(f"Programme de {size / 1024 / 1024:.1f} Mio : le site n'en reçoit que {limit // 1024 // 1024} Mio "
                        "depuis worker.exe. Utilise le dashboard web pour ce programme.", "GCODE_TOO_LARGE_FOR_SITE")
    return path.read_bytes().decode("utf-8-sig", errors="replace")


def _stage(agent, params):
    """Programme déposé par worker.exe (inbox) → cache des programmes. Dépôt effacé, réussi ou non."""
    staged, name = params["staged"], params["name"]
    folder = checked(agent.root, f"runtime/desktop/inbox/{staged}")
    path = checked(agent.root, f"runtime/desktop/inbox/{staged}/{name}")
    try:
        if is_link(folder) or is_link(path) or not path.is_file():
            raise ValueError("Programme à ouvrir introuvable : choisis-le de nouveau.")
        if Path(name).suffix.lower() not in PROGRAM_EXTENSIONS:
            raise ValueError("Programme G-code attendu (" + ", ".join(PROGRAM_EXTENSIONS) + ").")
        size = path.stat().st_size
        if size <= 0 or size > PROGRAM_MAX:
            raise ValueError("Programme vide ou trop gros (64 Mio au plus).")
        token, target = _new_program(agent)
        os.replace(path, target)
        return _program_view(token, target, name, "file")
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)


# --- machines de ce Worker (lecture locale) ---------------------------------------------------------------------

def _settings(spec):
    settings = spec.get("settings") if isinstance(spec.get("settings"), dict) else {}
    kept = {key: settings[key] for key in SETTING_KEYS if key in settings}
    if isinstance(kept.get("interlocks"), list):
        kept["interlocks"] = [{"name": str(item.get("name") or "")[:60], "kind": str(item.get("kind") or "")[:20],
                               "required": item.get("required") is not False}
                              for item in kept["interlocks"][:16] if isinstance(item, dict)]
    return json.loads(json.dumps(kept, default=str))


def _engine(agent):
    folder = Path(agent.root) / "components" / "cnc-engine"
    python = folder / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    ready = (folder / ".raphal-ready").is_file() and python.is_file()
    return {"installed": ready, "python": str(python) if ready else sys.executable, "label": "cnc-engine" if ready else "agent"}


def _machines(agent, params):
    if __package__:
        from . import control_room
    else:
        import control_room
    relay = getattr(agent, "equipment_relay", None)
    entries, complete = control_room._devices(relay) if relay is not None else ([], False)
    items = []
    for identifier, device in entries:
        spec = getattr(device, "spec", None)
        if not isinstance(spec, dict) or spec.get("kind") != "cnc":
            continue
        try:
            machine = control_room._machine(str(identifier), device)
        except Exception:  # une fiche illisible n'empêche jamais la vue des autres
            continue
        machine["settings"] = _settings(spec)
        items.append(machine)
    engine = _engine(agent)
    return {"checked_at": time.time(), "available": relay is not None, "complete": complete, "machines": items,
            "engine": {"installed": engine["installed"], "label": engine["label"]},
            "absent": {"name": control_room.ABSENT["name"]}}


# --- analyse et simulation par le moteur CNC ---------------------------------------------------------------------

def _analyze(agent, params):
    token, op = params["program"], params.get("op", "analyze")
    source = program_path(agent, token)   # vérifié tout de suite : un refus n'ouvre pas de tâche
    machine = params.get("machine") or {}

    def work(task):
        engine = _engine(agent)
        folder = _cache(agent, "analyses")
        _tidy(folder)
        stamp = secrets.token_hex(8)
        request_path = checked(agent.root, f"runtime/desktop/cache/cnc-studio/analyses/{stamp}-request.json")
        result_path = checked(agent.root, f"runtime/desktop/cache/cnc-studio/analyses/{stamp}-result.json")
        request_path.write_text(json.dumps({"op": op, "gcode_path": str(source), "machine": machine}), encoding="utf-8")
        code_dir = Path(__file__).resolve().parent
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() not in {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV"}}
        environment["PYTHONPATH"] = str(code_dir)
        environment["PYTHONIOENCODING"] = "utf-8"
        task.update(None, "Analyse par le moteur CNC (" + engine["label"] + ")…")
        stdout_path = checked(agent.root, f"runtime/desktop/cache/cnc-studio/analyses/{stamp}-stdout.txt")
        try:
            with open(stdout_path, "wb") as stdout:   # fichier, jamais un tube : un rapport long ne bloque pas le moteur
                process = subprocess.Popen([engine["python"], "-m", "cnc_engine.cli", str(request_path), str(result_path)],
                                           cwd=str(folder), env=environment, stdout=stdout, stderr=subprocess.DEVNULL,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                deadline = time.monotonic() + ENGINE_TIMEOUT
                while process.poll() is None:
                    if task.cancelled():
                        process.kill()
                        process.wait(10)
                        raise TaskCancelled()
                    if time.monotonic() > deadline:
                        process.kill()
                        process.wait(10)
                        raise TaskError("Analyse trop longue (10 min) : programme trop lourd pour l'aperçu.", "ENGINE_TIMEOUT")
                    time.sleep(0.1)
            output = stdout_path.read_bytes()[-256 * 1024:].decode("utf-8", errors="replace")
        finally:
            request_path.unlink(missing_ok=True)
            stdout_path.unlink(missing_ok=True)
        report = {}
        for line in output.splitlines():
            if line.startswith("RAPHAL_CNC_RESULT="):
                try:
                    report = json.loads(line[len("RAPHAL_CNC_RESULT="):])
                except ValueError:
                    report = {}
        if report.get("ok") is not True:
            result_path.unlink(missing_ok=True)
            raise TaskError(str(report.get("error") or "Le moteur CNC n'a rien rendu.")[:400], str(report.get("code") or "ENGINE_FAILED")[:60])
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        finally:
            result_path.unlink(missing_ok=True)
            result_path.with_name("preview.json").unlink(missing_ok=True)
        result.update(op=op, engine=engine["label"], program=token, analyzed_at=time.time())
        return desk.result_file(agent, result)
    return desk.start_task(agent, TOOL, ("Simulation" if op == "simulate" else "Analyse") + " du programme", work, journal=True)


# --- appels signés au site --------------------------------------------------------------------------------------

def _version():
    module = sys.modules.get("agent") or sys.modules.get("worker_agent.agent")
    return str(getattr(module, "AGENT_VERSION", "") or "desktop")


def _urlopen(request, timeout):  # remplacé par les tests
    return urllib.request.urlopen(request, timeout=timeout)


def _signed(agent, path, data):
    agent.reload_config()
    if __package__:
        from .identity import configure
    else:
        from identity import configure
    configure(agent.root, agent.config)
    server = agent.server
    parsed = urllib.parse.urlsplit(server)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
        raise TaskError("HTTPS est obligatoire hors connexion locale.", "HTTPS_REQUIRED")
    headers = {"Authorization": "Bearer " + str(agent.config.get("token", "")), "X-Worker-ID": str(agent.config.get("worker_id") or ""),
               "User-Agent": "AlpineWorker/" + _version(), "Accept": "application/json, application/octet-stream",
               "Content-Type": "application/json"}
    return protect_credentials(urllib.request.Request(server + path, data=data, headers=headers, method="POST"))


class SiteRefusal(TaskError):
    def __init__(self, message, code, status, details):
        super().__init__(message, code)
        self.status = status
        self.details = details


def _refusal(error):
    if isinstance(error, urllib.error.HTTPError):
        body = {}
        try:
            body = json.loads(error.read(256 * 1024).decode("utf-8"))
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        code = str(body.get("code") or "")
        if error.code == 404 and not code:
            return SiteRefusal("Le Dashboard n'a pas encore CNC Studio Desktop : il doit être redémarré dans sa nouvelle version.",
                               "SITE_MISSING", 404, {})
        details = body.get("details") if isinstance(body.get("details"), dict) else {}
        return SiteRefusal(str(body.get("error") or f"Erreur {error.code} du site.")[:500], code[:80] or f"HTTP_{error.code}",
                           error.code, details)
    return SiteRefusal(f"Site injoignable ({type(error).__name__}) : vérifie la connexion internet du PC.", "UNREACHABLE", 0, {})


def site(agent, op, args, session, path=STUDIO_PATH):
    """Op JSON du site ; rend ``response`` ; lève SiteRefusal (message, code, statut, détails) sur un refus."""
    body = json.dumps({"op": op, "args": args, "session_token": session}).encode("utf-8")
    try:
        with _urlopen(_signed(agent, path, body), 120) as answer:
            value = json.loads(answer.read(32 * 1024 * 1024).decode("utf-8"))
    except (TaskError, TaskCancelled):
        raise
    except Exception as error:
        raise _refusal(error) from None
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise SiteRefusal(str((value or {}).get("error") or "Réponse du site illisible.")[:500], str((value or {}).get("code") or ""), 0, {})
    return value.get("response") if isinstance(value.get("response"), dict) else {}


def site_download(agent, task, path, payload):
    """Octets d'un téléchargement signé (borné), avec progression et annulation."""
    body = json.dumps(payload).encode("utf-8")
    chunks, total = [], 0
    try:
        with _urlopen(_signed(agent, path, body), 3600) as answer:
            expected = int(answer.headers.get("Content-Length") or 0)
            while True:
                if task.cancelled():
                    raise TaskCancelled()
                chunk = answer.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > DOWNLOAD_MAX:
                    raise TaskError("Fichier trop volumineux pour CNC Studio Desktop (64 Mio au plus).", "TOO_LARGE")
                chunks.append(chunk)
                task.update(total * 100 // expected if expected else None, f"{total // 1024} Kio reçus")
    except (TaskError, TaskCancelled):
        raise
    except Exception as error:
        raise _refusal(error) from None
    return b"".join(chunks)


def _export_file(agent, data, name):
    folder = _cache(agent, "exports")
    _tidy(folder)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")[:80] or "export"
    target = checked(agent.root, f"runtime/desktop/cache/cnc-studio/exports/{secrets.token_hex(4)}-{safe}")
    target.write_bytes(data)
    return {"path": str(target), "name": name, "bytes": len(data)}


def _task(agent, label, work):
    def guarded(task):
        value = work(task)
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 256 * 1024:
            return desk.result_file(agent, value)
        return value
    return desk.start_task(agent, TOOL, label, guarded)


def _forward(op, label, keep=()):
    """Action qui relaie ``op`` au site avec ses paramètres (sans la session)."""
    def handler(agent, params):
        session = params.pop("session_token")
        args = dict(params)
        return _task(agent, label, lambda task: site(agent, op, args, session))
    return handler


# --- actions composées --------------------------------------------------------------------------------------------

def _project_open(agent, params):
    session = params.pop("session_token")

    def work(task):
        task.update(None, "Lecture du projet sur le site…")
        project = site(agent, "project-get", {"project": params["project"]}, session).get("project") or {}
        program = None
        if (project.get("program") or {}).get("bytes"):
            args = {"project": params["project"]}
            if "version" in params:
                args["version"] = params["version"]
            data = site_download(agent, task, DOWNLOAD_PATH, {"op": "project-gcode", "args": args, "session_token": session})
            program = _store_program(agent, data, str((project.get("program") or {}).get("name") or "programme.nc"), "project",
                                     {"project": params["project"]})
        return {"project": project, "program": program}
    return _task(agent, "Ouvrir le projet", work)


PROJECT_META = ("name", "notes", "tools_used", "equipment_id", "machine_profile_id", "analysis")
PROGRAM_META = ("program_name", "source", "cam_job_id", "library_file", "lines", "bounds", "duration_s")


def _project_save(agent, params):
    session = params.pop("session_token")
    body = {key: params[key] for key in PROJECT_META if key in params}
    program = {}
    for key in PROGRAM_META:
        if key in params:
            program["name" if key == "program_name" else key] = params[key]
    if program:
        body["program"] = program
    token = params.get("program")

    def work(task):
        if token:
            body["gcode"] = program_text(agent, token)
        if params.get("project"):
            return site(agent, "project-update", {"project": params["project"], "body": body}, session)
        return site(agent, "project-create", {"body": body}, session)
    return _task(agent, "Enregistrer le projet", work)


def _project_export(agent, params):
    session = params.pop("session_token")

    def work(task):
        data = site_download(agent, task, DOWNLOAD_PATH, {"op": "project-export", "args": {"project": params["project"]},
                                                          "session_token": session})
        return _export_file(agent, data, "projet.cncproj.zip")
    return _task(agent, "Exporter le projet", work)


def _inbox(agent, staged, name, limit):
    folder = checked(agent.root, f"runtime/desktop/inbox/{staged}")
    path = checked(agent.root, f"runtime/desktop/inbox/{staged}/{name}")
    try:
        if is_link(folder) or is_link(path) or not path.is_file():
            raise ValueError("Fichier à importer introuvable : choisis-le de nouveau.")
        size = path.stat().st_size
        if size <= 0 or size > limit:
            raise ValueError(f"Fichier vide ou trop gros ({limit // 1024 // 1024} Mio au plus).")
        return path.read_bytes()
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)


def _project_import(agent, params):
    session = params.pop("session_token")
    raw = _inbox(agent, params["staged"], params["name"], 4 * 1024 * 1024)   # lu tout de suite, dépôt effacé
    if not zipfile.is_zipfile(__import__("io").BytesIO(raw)):
        raise ValueError("Archive de projet CNC (.cncproj.zip) attendue.")
    encoded = base64.b64encode(raw).decode("ascii")
    return _task(agent, "Importer un projet", lambda task: site(agent, "project-import", {"zip_b64": encoded}, session))


def _library_export(agent, params):
    session = params.pop("session_token")

    def work(task):
        data = site_download(agent, task, DOWNLOAD_PATH, {"op": "library-export", "args": {"kind": params["kind"]},
                                                          "session_token": session})
        return _export_file(agent, data, f"bibliotheque-{params['kind']}.json")
    return _task(agent, "Exporter la bibliothèque", work)


def _library_import(agent, params):
    session = params.pop("session_token")
    raw = _inbox(agent, params["staged"], params["name"], 2 * 1024 * 1024)
    try:
        body = json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        raise ValueError("Fichier JSON de bibliothèque illisible.") from None
    if not isinstance(body, dict):
        raise ValueError("Fichier de bibliothèque invalide (objet JSON attendu).")
    return _task(agent, "Importer dans la bibliothèque",
                 lambda task: site(agent, "library-import", {"kind": params["kind"], "body": body}, session))


def _files(agent, params):
    """Programmes de la bibliothèque CNC du compte (même route signée que la Bibliothèque Desktop)."""
    session = params.pop("session_token")
    args = {"family": "cnc", "scope": "private", "page": params.get("page", 1), "per_page": 50}
    if params.get("q"):
        args["q"] = params["q"]
    return _task(agent, "Bibliothèque CNC", lambda task: site(agent, "list", args, session, LIBRARY_PATH))


def _file_open(agent, params):
    session = params.pop("session_token")
    name = params["name"]

    def work(task):
        data = site_download(agent, task, LIBRARY_DOWNLOAD_PATH, {"name": name, "what": "file", "session_token": session})
        return _store_program(agent, data, name, "library", {"library_file": name})
    return _task(agent, "Ouvrir " + name, work)


def _check(agent, params):
    session = params.pop("session_token")

    def work(task):
        args = {"controller": params["controller"]}
        for key in ("variant", "machine", "job_id"):
            if key in params:
                args[key] = params[key]
        if "program" in params:
            args["gcode"] = program_text(agent, params["program"])
        return site(agent, "posts-check", args, session)
    return _task(agent, "Vérifier pour le contrôleur", work)


def _command(agent, params):
    """Commande GRBL par la route du site (check-list, bornes, propriétaire contrôlés par le site).

    Le résultat de la tâche garde le refus du site (``ok: false``, ``status``, ``code``, ``details``) au lieu d'échouer :
    l'interface a besoin de la check-list demandée ou des avertissements à acquitter pour rejouer le parcours du site."""
    session = params.pop("session_token")
    body = dict(params["body"])
    command = body.get("command")
    if not isinstance(command, str):
        raise ValueError("Commande manquante.")
    if "gcode" in body:
        raise ValueError("Le programme vient du cache de CNC Studio (« program »), jamais du corps de la commande.")
    if command not in UNCONFIRMED and params.get("confirmed") is not True:
        raise ValueError("Commande machine non confirmée : confirme-la dans l'interface.")
    if command == "stream" and "program" not in params:
        raise ValueError("Programme à lancer manquant.")
    equipment = params["equipment"]

    def work(task):
        if command == "stream":
            body["gcode"] = program_text(agent, params["program"])
        try:
            response = site(agent, "grbl", {"equipment": equipment, "body": body}, session)
        except SiteRefusal as refusal:
            if refusal.status in (400, 403, 404, 409, 422):
                return {"ok": False, "status": refusal.status, "code": refusal.code, "error": str(refusal), "details": refusal.details}
            raise
        return {"ok": True, "response": response}
    label = {"stream": "Vérification à blanc" if body.get("dry_run") is True else "Lancement de l'usinage"}.get(command, "Commande " + command)
    return _task(agent, label, work)


def _cam_compute(agent, params):
    session = params.pop("session_token")

    def work(task):
        task.update(None, "Lecture du projet CAM…")
        record = site(agent, "cam-project-get", {"project": params["project"]}, session)
        project = record.get("project")
        if not isinstance(project, dict):
            raise TaskError("Projet CAM illisible.", "CAM_PROJECT_INVALID")
        args = {"project": project}
        for key in ("request_key", "controller_variant"):
            if key in params:
                args[key] = params[key]
        try:
            return {"ok": True, "response": site(agent, "cam-compute", args, session)}
        except SiteRefusal as refusal:
            if refusal.status in (400, 409):
                return {"ok": False, "status": refusal.status, "code": refusal.code, "error": str(refusal), "details": refusal.details}
            raise
    return _task(agent, "Calcul CAM sur ce Worker", work)


def _cam_artifact(agent, params):
    session = params.pop("session_token")

    def work(task):
        data = site_download(agent, task, DOWNLOAD_PATH, {"op": "cam-artifact", "args": {"job": params["job"], "name": params["name"]},
                                                          "session_token": session})
        return _export_file(agent, data, params["name"])
    return _task(agent, "Télécharger " + params["name"], work)


def _cam_load(agent, params):
    """G-code d'un calcul CAM, seulement s'il est livré (comme le site : terminé, rien de bloquant, sortie disponible)."""
    session = params.pop("session_token")

    def work(task):
        job = site(agent, "cam-job", {"job": params["job"]}, session)
        if job.get("state") != "completed":
            raise TaskError("Calcul CAM pas encore terminé.", "CAM_JOB_NOT_COMPLETED")
        if not isinstance(job.get("blocked_by"), list) or job["blocked_by"]:
            raise TaskError("Acquitte d'abord les avertissements du calcul (onglet CAM) : le G-code reste retenu.", "CAM_GCODE_BLOCKED")
        output = next((item for item in job.get("outputs") or [] if isinstance(item, dict) and item.get("kind") == "gcode"
                       and item.get("available") is True and isinstance(item.get("name"), str)), None)
        if output is None:
            raise TaskError("Ce calcul n'a pas livré de G-code dans la bibliothèque.", "CAM_GCODE_MISSING")
        name = v_file_name(output["name"])
        data = site_download(agent, task, LIBRARY_DOWNLOAD_PATH, {"name": name, "what": "file", "session_token": session})
        return _store_program(agent, data, name, "cam_job", {"cam_job_id": params["job"], "library_file": name})
    return _task(agent, "Charger le résultat CAM", work)


def _supervision_program(agent, params):
    session = params.pop("session_token")

    def work(task):
        data = site_download(agent, task, DOWNLOAD_PATH, {"op": "supervision-program",
                                                          "args": {"equipment": params["equipment"], "run": params["run"]},
                                                          "session_token": session})
        return _store_program(agent, data, f"programme-{params['run']}.nc", "supervision")
    return _task(agent, "Programme archivé", work)


# --- table des actions ------------------------------------------------------------------------------------------

_S = {"session_token": v_session}
_BODY = v_object(depth=8, keys=400)
_MACHINE = v_object(depth=3, keys=20)
DESKTOP_ACTIONS = {
    # local (sans le site)
    "machines": {"handler": _machines, "read": True, "params": {}},
    "stage": {"handler": _stage, "read": True, "params": {"staged": v_hex(32), "name": v_file_name}},
    "analyze": {"handler": _analyze, "read": True,
                "params": {"program": v_program, "op": v_choice("analyze", "simulate"), "machine": _MACHINE}},
    # site : état, projets
    "state": {"handler": _forward("state", "État des CNC"), "read": True, "params": {**_S}},
    "projects": {"handler": _forward("projects", "Projets CNC"), "read": True, "params": {**_S}},
    "project-open": {"handler": _project_open, "read": True, "params": {**_S, "project": v_ref, "version": v_int(1, 10**6)}},
    "project-save": {"handler": _project_save, "read": False, "params": {
        **_S, "project": v_ref, "program": v_program, "name": v_text(120), "notes": v_text(4000),
        "tools_used": v_strings(64, 80), "equipment_id": v_ref, "machine_profile_id": v_ref, "analysis": v_object(depth=8, keys=400),
        "program_name": v_text(120), "source": v_choice("file", "library", "cam_job", "manual"), "cam_job_id": v_ref,
        "library_file": v_text(255), "lines": v_int(0, 50_000_000), "bounds": v_object(depth=1, keys=6), "duration_s": v_int(0, 10**8)}},
    "project-duplicate": {"handler": _forward("project-duplicate", "Dupliquer le projet"), "read": False, "params": {**_S, "project": v_ref}},
    "project-rename": {"handler": _forward("project-rename", "Renommer le projet"), "read": False,
                       "params": {**_S, "project": v_ref, "name": v_text(120, empty=False)}},
    "project-delete": {"handler": _forward("project-delete", "Supprimer le projet"), "read": False,
                       "params": {**_S, "project": v_ref, "confirm": v_true}},
    "project-versions": {"handler": _forward("project-versions", "Versions du projet"), "read": True, "params": {**_S, "project": v_ref}},
    "project-restore": {"handler": _forward("project-restore", "Restaurer une version"), "read": False,
                        "params": {**_S, "project": v_ref, "version": v_int(1, 10**6)}},
    "project-export": {"handler": _project_export, "read": True, "params": {**_S, "project": v_ref}},
    "project-import": {"handler": _project_import, "read": False, "params": {**_S, "staged": v_hex(32), "name": v_file_name}},
    # site : bibliothèques d'outils et de matériaux
    "library-list": {"handler": _forward("library-list", "Bibliothèque d'outils"), "read": True,
                     "params": {**_S, "kind": v_choice("tools", "materials")}},
    "library-create": {"handler": _forward("library-create", "Ajouter à la bibliothèque"), "read": False,
                       "params": {**_S, "kind": v_choice("tools", "materials"), "body": _BODY}},
    "library-update": {"handler": _forward("library-update", "Modifier la bibliothèque"), "read": False,
                       "params": {**_S, "kind": v_choice("tools", "materials"), "item": v_ref, "body": _BODY}},
    "library-delete": {"handler": _forward("library-delete", "Supprimer de la bibliothèque"), "read": False,
                       "params": {**_S, "kind": v_choice("tools", "materials"), "item": v_ref, "confirm": v_true}},
    "library-duplicate": {"handler": _forward("library-duplicate", "Dupliquer"), "read": False,
                          "params": {**_S, "kind": v_choice("tools", "materials"), "item": v_ref}},
    "library-usage": {"handler": _forward("library-usage", "Durée d'utilisation"), "read": False,
                      "params": {**_S, "item": v_ref, "minutes": v_int(1, 100000)}},
    "library-export": {"handler": _library_export, "read": True, "params": {**_S, "kind": v_choice("tools", "materials")}},
    "library-import": {"handler": _library_import, "read": False,
                       "params": {**_S, "kind": v_choice("tools", "materials"), "staged": v_hex(32), "name": v_file_name}},
    # site : programmes de la bibliothèque CNC du compte
    "files": {"handler": _files, "read": True, "params": {**_S, "page": v_int(1, 100000), "q": v_text(200)}},
    "file-open": {"handler": _file_open, "read": True, "params": {**_S, "name": v_file_name}},
    # site : Préparer pour une CNC (FreeCAD CAM sur ce Worker)
    "cam-projects": {"handler": _forward("cam-projects", "Projets CAM"), "read": True, "params": {**_S}},
    "cam-project": {"handler": _forward("cam-project-get", "Projet CAM"), "read": True, "params": {**_S, "project": v_ref}},
    "cam-project-save": {"handler": lambda agent, params: _forward("cam-project-update", "Enregistrer le projet CAM")(
        agent, {"session_token": params["session_token"], "project": params["project"],
                "body": {"project": params["data"], **({"name": params["name"]} if "name" in params else {})}}),
        "read": False, "params": {**_S, "project": v_ref, "data": _BODY, "name": v_text(120)}},
    "cam-project-duplicate": {"handler": _forward("cam-project-duplicate", "Dupliquer le projet CAM"), "read": False,
                              "params": {**_S, "project": v_ref, "name": v_text(120)}},
    "cam-project-rename": {"handler": _forward("cam-project-rename", "Renommer le projet CAM"), "read": False,
                           "params": {**_S, "project": v_ref, "name": v_text(120, empty=False)}},
    "cam-project-delete": {"handler": _forward("cam-project-delete", "Supprimer le projet CAM"), "read": False,
                           "params": {**_S, "project": v_ref, "confirm": v_true}},
    "cam-workers": {"handler": _forward("cam-workers", "Workers FreeCAD"), "read": True, "params": {**_S}},
    "cam-capabilities": {"handler": _forward("cam-capabilities", "Capacités FreeCAD CAM"), "read": False,
                         "params": {**_S, "request_key": v_text(120)}},
    "cam-compute": {"handler": _cam_compute, "read": False,
                    "params": {**_S, "project": v_ref, "request_key": v_text(120), "controller_variant": v_text(80)}},
    "cam-job": {"handler": _forward("cam-job", "Suivi du calcul CAM"), "read": True, "params": {**_S, "job": v_ref}},
    "cam-accept": {"handler": _forward("cam-accept", "Acquitter les avertissements"), "read": False,
                   "params": {**_S, "job": v_ref, "acknowledged": v_codes}},
    "cam-cancel": {"handler": _forward("cam-cancel", "Annuler le calcul CAM"), "read": False, "params": {**_S, "job": v_ref}},
    "cam-artifact": {"handler": _cam_artifact, "read": True, "params": {**_S, "job": v_ref, "name": v_file_name}},
    "cam-load": {"handler": _cam_load, "read": True, "params": {**_S, "job": v_ref}},
    # site : contrôleurs, supervision, commandes
    "controllers": {"handler": _forward("controllers", "Contrôleurs"), "read": True, "params": {**_S}},
    "check": {"handler": _check, "read": True, "params": {**_S, "controller": v_text(40, empty=False), "variant": v_text(60),
                                                          "program": v_program, "job_id": v_ref, "machine": _MACHINE}},
    "supervision": {"handler": _forward("supervision", "Supervision"), "read": True,
                    "params": {**_S, "equipment": v_ref, "page": v_int(1, 100000), "per_page": v_int(1, 100)}},
    "supervision-program": {"handler": _supervision_program, "read": True, "params": {**_S, "equipment": v_ref, "run": v_int(1, 10**9)}},
    "command": {"handler": _command, "read": False,
                "params": {**_S, "equipment": v_ref, "body": v_object(depth=3, keys=30), "program": v_program, "confirmed": v_bool}},
}
REQUIRED = {
    "stage": ("staged", "name"), "analyze": ("program",), "project-open": ("project",), "project-duplicate": ("project",),
    "project-rename": ("project", "name"), "project-delete": ("project", "confirm"), "project-versions": ("project",),
    "project-restore": ("project", "version"), "project-export": ("project",), "project-import": ("staged", "name"),
    "library-list": ("kind",), "library-create": ("kind", "body"), "library-update": ("kind", "item", "body"),
    "library-delete": ("kind", "item", "confirm"), "library-duplicate": ("kind", "item"), "library-usage": ("item", "minutes"),
    "library-export": ("kind",), "library-import": ("kind", "staged", "name"), "file-open": ("name",),
    "cam-project": ("project",), "cam-project-save": ("project", "data"), "cam-project-duplicate": ("project",),
    "cam-project-rename": ("project", "name"), "cam-project-delete": ("project", "confirm"), "cam-compute": ("project",),
    "cam-job": ("job",), "cam-accept": ("job", "acknowledged"), "cam-cancel": ("job",), "cam-artifact": ("job", "name"),
    "cam-load": ("job",), "check": ("controller",), "supervision": ("equipment",), "supervision-program": ("equipment", "run"),
    "command": ("equipment", "body"),
}
LOCAL = ("machines", "stage", "analyze")


def _required(name, handler):
    fields = REQUIRED.get(name, ()) + (() if name in LOCAL else ("session_token",))

    def run(agent, params):
        missing = [field for field in fields if field not in params]
        if missing:
            raise ValueError("Paramètre manquant : " + missing[0] + (" (connecte-toi dans worker.exe)" if missing[0] == "session_token" else ""))
        return handler(agent, dict(params))
    return run


for _name, _spec in DESKTOP_ACTIONS.items():
    _spec["handler"] = _required(_name, _spec["handler"])
del _name, _spec
