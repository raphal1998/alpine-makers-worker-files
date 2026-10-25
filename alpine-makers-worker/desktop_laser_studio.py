"""Alpine Laser Studio pour Alpine Makers Desktop (worker.exe), outil « laser-studio » de ``desktop_actions``.

L'interface native (worker_manager/DesktopToolLaserStudio*.cs) édite le projet ; ce module fait ce que le site faisait
faire au Worker par les jobs laser : il appelle le MOTEUR LASER DU WORKER (composant « laser-engine », Python isolé,
``python -m laser_engine.cli``), ici même, sans passer par le site :

- ``etat``      : moteur prêt ou non, bibliothèque de matériaux intégrée, rappels de la check-list (lecture) ;
- ``import``    : SVG ou DXF déposé par worker.exe (``ctx.StageFile``) → objets du projet (DXF converti en SVG par
  ``desktop_laser_studio_dxf``, puis analysé par le moteur, comme un import du studio web) ;
- ``compute``   : projet déposé (.alsproj) → aperçu du parcours (``preview``), G-code + simulation + durée estimée
  (``gcode``) ou G-code de cadrage laser coupé (``frame``). Fichiers dans ``runtime/desktop/cache/laser-studio/<job>``.

Ce qui part vers une machine passe par le SITE, jamais par une voie locale : routes signées
``/api/worker-protocol/self/laser-studio`` (worker_desktop_laser_studio.py) qui appellent la route du site
``POST /api/equipment/<id>/grbl`` elle-même (propriétaire, onglet, check-list nommée, bornes, état de la machine) :

- ``machines``   : graveuses du compte (fiche, état, sécurités déclarées) ;
- ``machine``    : état, ARRÊT, pause, reprise, home, déverrouillage, fin de cadrage ;
- ``frame``      : cadrage laser COUPÉ d'un G-code de cadrage calculé ici ;
- ``send``       : lance le G-code calculé ici (``job``), check-list nommée par l'opérateur, à sec en option ;
- ``operations`` : suivi des commandes envoyées (refus, échec, fin).

Toute action machine sauf ARRÊT et lecture d'état exige ``confirm: true`` (posé par l'interface après une
confirmation explicite). Aucune commande n'est rejouée ni mise en file pour plus tard.
"""
import json
import os
import re
import shutil
import subprocess
import time
import uuid

if __package__:
    from . import desktop_actions as desk
    from . import desktop_laser_studio_dxf as dxf
    from . import desktop_laser_studio_project as project
    from .desktop_actions import TaskCancelled, TaskError, v_bool, v_choice, v_hex, v_int, v_session, v_text
    from .desktop_library import v_file_name
    from .local_control import checked
    from .safety import is_link
else:
    import desktop_actions as desk
    import desktop_laser_studio_dxf as dxf
    import desktop_laser_studio_project as project
    from desktop_actions import TaskCancelled, TaskError, v_bool, v_choice, v_hex, v_int, v_session, v_text
    from desktop_library import v_file_name
    from local_control import checked
    from safety import is_link

TOOL = "laser-studio"
SITE_PATH = "/api/worker-protocol/self/laser-studio"
INPUT_MAX = 32 * 1024 * 1024            # requête du moteur : MAX_REQUEST_BYTES de laser_engine.cli
ENGINE_TIMEOUT = 1800                   # même borne que runners/laser-engine.py
RESULT_MARKER = "RAPHAL_LASER_RESULT="
JOBS_KEPT = 12                          # calculs gardés dans le cache (les plus anciens partent)
DISPLAY_SEGMENTS = 120_000              # simulation allégée pour l'affichage (comme la CLI)
PREVIEW_POINTS = 600_000                # aperçu : points dessinés au plus
# Corps signé du site : 6 Mio au plus (worker_security.py) ; le G-code y voyage en JSON, avec sa marge.
SEND_MAX_BYTES = 5 * 1024 * 1024
MACHINE_COMMANDS = ("status", "stop", "hold", "resume", "home", "unlock", "frame_stop")
UNCONFIRMED_COMMANDS = ("status", "stop")   # lecture, et ARRÊT d'urgence (jamais retardé par une question)
IMPORT_EXTENSIONS = (".svg", ".dxf")
MISSING_ENGINE = ("Le moteur laser n'est pas installé sur ce Worker. Installe Alpine Laser Studio depuis Mes Workers → "
                  "Installations (ou le Gestionnaire du Worker), puis relance.")
_EQUIPMENT = re.compile(r"[A-Za-z0-9_-]{1,80}")


def v_equipment(value):
    if type(value) is not str or not _EQUIPMENT.fullmatch(value):
        raise ValueError("Machine invalide.")
    return value


def v_true(value):
    if value is not True:
        raise ValueError("Commande machine non confirmée : confirme-la dans l'interface.")
    return value


def v_checklist(value):
    if type(value) is not list or len(value) > 32 or any(type(item) is not str or not 0 < len(item) <= 200 for item in value):
        raise ValueError("Check-list invalide.")
    return value


def v_color(value):
    if project.normalize_color(value) is None:
        raise ValueError("Couleur de calque invalide.")
    return project.normalize_color(value)


# --- dossiers -------------------------------------------------------------------------------------------------------

def cache_folder(agent):
    return desk.runtime_folder(agent, "cache", "laser-studio")


def _new_job(agent):
    folder = cache_folder(agent)
    jobs = sorted((path for path in folder.iterdir() if path.is_dir() and not is_link(path)), key=lambda path: path.stat().st_mtime)
    for old in jobs[:max(0, len(jobs) - JOBS_KEPT + 1)]:
        shutil.rmtree(old, ignore_errors=True)
    job = uuid.uuid4().hex
    path = checked(agent.root, f"runtime/desktop/cache/laser-studio/{job}")
    path.mkdir(parents=True)
    return job, path


def job_folder(agent, job):
    path = checked(agent.root, f"runtime/desktop/cache/laser-studio/{job}")
    if is_link(path) or not path.is_dir():
        raise ValueError("Calcul introuvable ou effacé : recalcule le G-code.")
    return path


def staged_text(agent, staged, name):
    """Lit puis efface le dépôt ``runtime/desktop/inbox/<staged>/<name>`` (fichier ordinaire, 32 Mio au plus)."""
    path = checked(agent.root, f"runtime/desktop/inbox/{staged}/{name}")
    try:
        if is_link(path.parent) or is_link(path) or not path.is_file():
            raise ValueError("Fichier déposé introuvable : recommence l'opération.")
        size = path.stat().st_size
        if size <= 0 or size > INPUT_MAX:
            raise ValueError(f"Fichier déposé vide ou trop gros ({INPUT_MAX // 1024 // 1024} Mio au plus).")
        return path.read_bytes().decode("utf-8-sig", errors="replace")
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)


# --- moteur laser du Worker ------------------------------------------------------------------------------------------

def engine_python(agent):
    finder = getattr(agent, "_laser_engine_python", None)
    try:
        python = finder() if callable(finder) else None
    except Exception:  # noqa: BLE001 — installation illisible : moteur absent
        python = None
    return str(python) if python and os.path.isfile(str(python)) else None


def _environment(agent):
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV"}}
    environment["PYTHONPATH"] = str(agent.root)
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def run_engine(agent, task, op, request, folder, label=""):
    """Une opération du moteur (``laser_engine.cli``) dans ``folder`` ; rend le résultat JSON. Annulable."""
    python = engine_python(agent)
    if not python:
        raise TaskError(MISSING_ENGINE, "ENGINE_MISSING")
    request_path, result_path = folder / f"{op}.request.json", folder / f"{op}.result.json"
    data = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    if len(data.encode("utf-8")) > INPUT_MAX:
        raise TaskError("Projet trop volumineux pour le moteur (32 Mio au plus) : réduis ou allège les images.", "REQUEST_TOO_LARGE")
    request_path.write_text(data, encoding="utf-8")
    stdout, stderr = folder / f"{op}.stdout.txt", folder / f"{op}.stderr.txt"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    task.update(None, label or f"Moteur laser : {op}")
    with open(stdout, "wb") as out, open(stderr, "wb") as err:
        process = subprocess.Popen([python, "-m", "laser_engine.cli", str(request_path), str(result_path), op],
                                   cwd=str(agent.root), env=_environment(agent), stdout=out, stderr=err,
                                   stdin=subprocess.DEVNULL, creationflags=flags)
        deadline = time.monotonic() + ENGINE_TIMEOUT
        try:
            while process.poll() is None:
                if task.cancelled():
                    raise TaskCancelled()
                if time.monotonic() > deadline:
                    raise TaskError("Calcul laser trop long (30 min) : arrêté.", "ENGINE_TIMEOUT")
                time.sleep(0.2)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(10)
    try:
        request_path.unlink()
    except OSError:
        pass
    lines = stdout.read_text(encoding="utf-8", errors="replace").splitlines()
    reported = next((line[len(RESULT_MARKER):] for line in reversed(lines) if line.startswith(RESULT_MARKER)), "")
    try:
        report = json.loads(reported) if reported else {}
    except ValueError:
        report = {}
    if report.get("ok") is not True:
        message = str(report.get("error") or "Le moteur laser n'a rendu aucun résultat.")
        raise TaskError(message[:500], str(report.get("code") or "ENGINE_ERROR")[:60])
    try:
        return json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise TaskError("Résultat du moteur laser illisible.", "ENGINE_RESULT") from None


# --- conversions pour l'affichage -------------------------------------------------------------------------------------

def display_segments(segments, limit=DISPLAY_SEGMENTS):
    """Segments de simulation allégés (même règle que la CLI) : voisins d'une même ligne fusionnés, puis échantillonnés."""
    merged = []
    for segment in segments:
        x1, y1, x2, y2, power = (float(value) for value in segment[:5])
        if merged and power > 0:
            last = merged[-1]
            if (last[4] > 0 and abs(last[2] - x1) < 1e-6 and abs(last[3] - y1) < 1e-6 and abs(last[1] - last[3]) < 1e-6
                    and abs(y1 - y2) < 1e-6):
                last[2], last[3], last[4] = x2, y2, max(last[4], power)
                continue
        merged.append([x1, y1, x2, y2, power])
    truncated = len(merged) > limit
    if truncated:
        step = len(merged) / limit
        merged = [merged[int(index * step)] for index in range(limit)]
    return merged, truncated


def _to_doc_segments(segments, profile, offset):
    out = []
    for x1, y1, x2, y2, power in segments:
        a = project.machine_to_doc((x1, y1), profile, offset)
        b = project.machine_to_doc((x2, y2), profile, offset)
        out.append([round(a[0], 3), round(a[1], 3), round(b[0], 3), round(b[1], 3), round(power, 2)])
    return out


def _doc_bounds(bounds, profile, offset):
    if not isinstance(bounds, dict) or not all(key in bounds for key in ("min_x", "min_y", "max_x", "max_y")):
        return None
    a = project.machine_to_doc((float(bounds["min_x"]), float(bounds["min_y"])), profile, offset)
    b = project.machine_to_doc((float(bounds["max_x"]), float(bounds["max_y"])), profile, offset)
    return {"x": round(min(a[0], b[0]), 3), "y": round(min(a[1], b[1]), 3), "w": round(abs(b[0] - a[0]), 3), "h": round(abs(b[1] - a[1]), 3)}


def _simulation_view(simulated, profile, offset, already_reduced=False):
    segments = simulated.get("segments") or []
    if already_reduced:
        reduced, truncated = segments, bool(simulated.get("truncated"))
    else:
        reduced, truncated = display_segments(segments)
    return {"segments": _to_doc_segments(reduced, profile, offset), "truncated": truncated,
            "estimated_seconds": simulated.get("estimated_seconds"), "cut_length_mm": simulated.get("cut_length_mm"),
            "travel_length_mm": simulated.get("travel_length_mm"), "moves": simulated.get("moves"),
            "bounds": simulated.get("bounds")}


def _preview_view(plan, profile, offset):
    operations, total, truncated = [], 0, False
    for item in plan.get("operations") or []:
        entry = {"layer": item.get("layer"), "mode": item.get("mode"), "name": item.get("name") or "", "passes": item.get("passes")}
        if item.get("summary") or not isinstance(item.get("segments"), list):
            entry["bounds"] = _doc_bounds(item.get("bounds"), profile, offset)
            entry["segments"] = []
        else:
            paths = []
            for segment in item["segments"]:
                if total >= PREVIEW_POINTS:
                    truncated = True
                    break
                points = [project.machine_to_doc((float(p[0]), float(p[1])), profile, offset) for p in segment]
                total += len(points)
                paths.append([[round(p[0], 3), round(p[1], 3)] for p in points])
            entry["segments"] = paths
        operations.append(entry)
    return {"operations": operations, "truncated": truncated, "stats": plan.get("stats") or {},
            "warnings": list(plan.get("warnings") or [])}


# --- actions locales (moteur) ----------------------------------------------------------------------------------------

def _etat(agent, params):
    python = engine_python(agent)
    jobs = 0
    try:
        jobs = sum(1 for path in cache_folder(agent).iterdir() if path.is_dir())
    except OSError:
        pass
    return {"engine": {"ready": bool(python), "message": "" if python else MISSING_ENGINE},
            "materials": project.MATERIALS, "material_classes": [list(item) for item in project.MATERIAL_CLASSES],
            "safety_items": project.SAFETY_ITEMS, "interlock_kinds": project.INTERLOCK_KINDS,
            "operator_interlock_kinds": list(project.OPERATOR_INTERLOCK_KINDS), "image_modes": list(project.IMAGE_MODES),
            "layer_modes": list(project.LAYER_MODES), "palette": list(project.PALETTE), "orders": list(project.PLAN_ORDERS),
            "origins": list(project.MACHINE_ORIGINS), "cache_jobs": jobs, "send_max_bytes": SEND_MAX_BYTES}


def _import(agent, params):
    name = params["name"]
    extension = os.path.splitext(name.lower())[1]
    if extension not in IMPORT_EXTENSIONS:
        raise ValueError("Import par le moteur : SVG ou DXF (les images et les projets s'ouvrent dans worker.exe).")
    text = staged_text(agent, params["staged"], name)   # lu tout de suite : le dépôt n'attend pas la tâche
    active = params.get("active_layer", "#000000")
    dpi = params.get("dpi", 96)

    def work(task):
        warnings = []
        svg = text
        if extension == ".dxf":
            try:
                svg, warnings, _ = dxf.dxf_to_svg(text)
            except dxf.DxfError as error:
                raise TaskError(str(error), "DXF_INVALID") from None
        job, folder = _new_job(agent)
        try:
            result = run_engine(agent, task, "parse", {"svg": svg, "dpi": dpi}, folder, "Analyse du dessin par le moteur laser")
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        converted = project.objects_from_parse(result, active)
        if not converted["objects"]:
            raise TaskError("Aucun tracé exploitable dans ce fichier (textes et images intégrées sont ignorés).", "IMPORT_EMPTY")
        converted["warnings"] = warnings + converted["warnings"]
        converted["source"] = name
        return desk.result_file(agent, converted)
    return desk.start_task(agent, TOOL, "Importer " + name, work)


def _compute(agent, params):
    op = params["op"]
    text = staged_text(agent, params["staged"], params["name"])
    try:
        data = json.loads(text)
        prepared = project.clean_project(data)
    except (ValueError, project.ProjectError) as error:
        raise ValueError(str(error) if isinstance(error, project.ProjectError) else "Projet illisible (JSON invalide).") from None
    profile = {**project.default_profile(), **prepared["profile"]}
    offset = prepared["offset"]
    speed = params.get("frame_speed", 3000)
    labels = {"preview": "Aperçu du parcours", "gcode": "Génération du G-code", "frame": "Cadrage (laser coupé)"}

    def work(task):
        body, warnings = project.plan_body(prepared, include_operations=op == "preview")
        if not body["layers"] and not body.get("raster_sources"):
            raise TaskError("Rien à graver : aucun tracé sur un calque actif ni image.", "PROJECT_EMPTY")
        job, folder = _new_job(agent)
        meta = {"op": op, "name": prepared["name"], "kind": project.machine_profile(profile)["kind"], "created": time.time()}
        (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        if op == "preview":
            result = run_engine(agent, task, "plan", body, folder, labels[op])
            view = _preview_view(result.get("plan") or {}, profile, offset)
            view["warnings"] = warnings + view["warnings"]
            view.update(job=job, op=op)
            return desk.result_file(agent, view)
        if op == "frame":
            result = run_engine(agent, task, "frame", {**body, "speed_mm_min": speed, "power_pct": 0}, folder, labels[op])
            gcode = folder / "output.gcode"
            return {"job": job, "op": op, "gcode_path": str(gcode), "bytes": gcode.stat().st_size if gcode.is_file() else 0,
                    "bounds": result.get("bounds"), "doc_bounds": _doc_bounds(result.get("bounds"), profile, offset),
                    "warnings": warnings}
        result = run_engine(agent, task, "gcode", body, folder, labels[op])
        gcode = folder / "output.gcode"
        size = gcode.stat().st_size if gcode.is_file() else 0
        if "simulation" in result:
            simulation = _simulation_view(result["simulation"], profile, offset, already_reduced=True)
        elif result.get("simulation_error"):
            simulation = {"error": str(result["simulation_error"])[:300]}
        else:
            task.update(None, "Simulation du G-code")
            simulation = _simulation_view(run_engine(agent, task, "simulate", {"gcode": result.get("gcode") or ""}, folder,
                                                     "Simulation du G-code"), profile, offset)
        lines = result.get("gcode_lines")
        if lines is None:
            lines = sum(1 for line in (result.get("gcode") or "").splitlines() if line.strip())
        excerpt = result.get("gcode_excerpt")
        if excerpt is None:
            excerpt = "\n".join((result.get("gcode") or "").splitlines()[:200])
        for name in ("gcode.result.json", "simulate.result.json"):   # le G-code est déjà dans output.gcode
            try:
                (folder / name).unlink()
            except OSError:
                pass
        return desk.result_file(agent, {"job": job, "op": op, "gcode_path": str(gcode), "bytes": size, "lines": lines,
                                        "excerpt": excerpt, "stats": result.get("stats") or {},
                                        "warnings": warnings + list(result.get("warnings") or []), "simulation": simulation,
                                        "sendable": size <= SEND_MAX_BYTES, "send_max_bytes": SEND_MAX_BYTES})
    # Seule la génération du G-code de travail est un job du journal local ; aperçu et cadrage n'en sont pas.
    return desk.start_task(agent, TOOL, labels[op], work, journal=op == "gcode")


# --- actions qui passent par le site (route /grbl du site, rien en local) ---------------------------------------------

def _site_error(error):
    code = getattr(error, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        site_code = getattr(error, "site_code", "") or ""
        if code == 404 and not site_code:
            return TaskError("Le Dashboard n'a pas encore Laser Studio Desktop : il doit être redémarré dans sa nouvelle version.",
                             "SITE_MISSING")
        return TaskError(str(error)[:400] or f"Erreur {code} du site.", site_code[:60] or f"HTTP_{code}")
    return TaskError(f"Site injoignable ({type(error).__name__}) : vérifie la connexion internet du PC.", "UNREACHABLE")


def _site(agent, op, args, session):
    try:
        answer = agent.request(SITE_PATH, "POST", {"op": op, "args": args, "session_token": session})
    except Exception as error:
        raise _site_error(error) from None
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        raise TaskError(str((answer or {}).get("error") or "Réponse du site illisible.")[:400], str((answer or {}).get("code") or ""))
    return answer.get("response") if isinstance(answer.get("response"), dict) else {}


def _machines(agent, params):
    session = params["session_token"]
    return desk.start_task(agent, TOOL, "Graveuses du compte", lambda task: _site(agent, "machines", {}, session))


def _machine(agent, params):
    command = params["command"]
    if command not in UNCONFIRMED_COMMANDS and params.get("confirm") is not True:
        raise ValueError("Commande machine non confirmée : confirme-la dans l'interface.")
    session = params["session_token"]
    args = {"equipment_id": params["equipment_id"], "body": {"command": command}}
    return desk.start_task(agent, TOOL, "Machine : " + command, lambda task: _site(agent, "grbl", args, session))


def _job_gcode(agent, job, expected):
    folder = job_folder(agent, job)
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    if meta.get("op") != expected:
        raise ValueError("Ce calcul n'est pas un G-code de " + ("travail" if expected == "gcode" else "cadrage") + " : recalcule-le.")
    path = folder / "output.gcode"
    if is_link(path) or not path.is_file():
        raise ValueError("G-code introuvable : recalcule-le.")
    size = path.stat().st_size
    if size <= 0:
        raise ValueError("G-code vide : recalcule-le.")
    if size > SEND_MAX_BYTES:
        raise ValueError(f"G-code de {size // 1024 // 1024} Mio : l'envoi depuis Desktop est limité à "
                         f"{SEND_MAX_BYTES // 1024 // 1024} Mio. Enregistre le .gcode et lance-le depuis le dashboard "
                         "(Alpine Laser Studio ou Gestion graveuse).")
    return path.read_text(encoding="utf-8"), meta


def _send(agent, params):
    gcode, meta = _job_gcode(agent, params["job"], "gcode")   # vérifié tout de suite : un refus n'ouvre pas de tâche
    session = params["session_token"]
    name = (params.get("name") or str(meta.get("name") or "job")).strip()[:100] or "job"
    body = {"command": "stream", "gcode": gcode, "name": name, "checklist_confirmed": True,
            "checklist": params.get("checklist", []), "dry_run": params.get("dry_run") is True}
    args = {"equipment_id": params["equipment_id"], "body": body}
    label = ("Envoi à sec : " if body["dry_run"] else "Envoi à la graveuse : ") + name
    return desk.start_task(agent, TOOL, label, lambda task: _site(agent, "grbl", args, session))


def _frame(agent, params):
    gcode, meta = _job_gcode(agent, params["job"], "frame")
    session = params["session_token"]
    name = ((params.get("name") or str(meta.get("name") or "job")).strip()[:90] or "job") + "-cadrage"
    args = {"equipment_id": params["equipment_id"], "body": {"command": "frame", "gcode": gcode, "name": name}}
    return desk.start_task(agent, TOOL, "Cadrage laser coupé : " + name, lambda task: _site(agent, "grbl", args, session))


def _operations(agent, params):
    session = params["session_token"]
    args = {"equipment_id": params["equipment_id"]}
    return desk.start_task(agent, TOOL, "Suivi des commandes", lambda task: _site(agent, "operations", args, session))


_SESSION = {"session_token": v_session}
DESKTOP_ACTIONS = {
    "info": {"handler": _etat, "read": True, "params": {}},
    "import": {"handler": _import, "read": True,
               "params": {"staged": v_hex(32), "name": v_file_name, "dpi": v_int(1, 2400), "active_layer": v_color}},
    "compute": {"handler": _compute, "read": True,
                "params": {"staged": v_hex(32), "name": v_file_name, "op": v_choice("preview", "gcode", "frame"),
                           "frame_speed": v_int(1, 100000)}},
    "machines": {"handler": _machines, "read": True, "params": {**_SESSION}},
    "machine": {"handler": _machine, "read": False,
                "params": {**_SESSION, "equipment_id": v_equipment, "command": v_choice(*MACHINE_COMMANDS), "confirm": v_true}},
    "frame": {"handler": _frame, "read": False,
              "params": {**_SESSION, "equipment_id": v_equipment, "job": v_hex(32), "name": v_text(100), "confirm": v_true}},
    "send": {"handler": _send, "read": False,
             "params": {**_SESSION, "equipment_id": v_equipment, "job": v_hex(32), "name": v_text(100),
                        "checklist": v_checklist, "dry_run": v_bool, "confirm": v_true}},
    "operations": {"handler": _operations, "read": True, "params": {**_SESSION, "equipment_id": v_equipment}},
}
REQUIRED = {"info": (), "import": ("staged", "name"), "compute": ("staged", "name", "op"), "machines": ("session_token",),
            "machine": ("session_token", "equipment_id", "command"),
            "frame": ("session_token", "equipment_id", "job", "confirm"),
            "send": ("session_token", "equipment_id", "job", "checklist", "confirm"),
            "operations": ("session_token", "equipment_id")}


def _required(name, handler):
    def run(agent, params):
        missing = [field for field in REQUIRED[name] if field not in params]
        if missing:
            hint = " (connecte-toi dans worker.exe)" if missing[0] == "session_token" else ""
            if missing[0] == "confirm":
                raise ValueError("Commande machine non confirmée : confirme-la dans l'interface.")
            raise ValueError("Paramètre manquant : " + missing[0] + hint)
        return handler(agent, dict(params))
    return run


for _name, _spec in DESKTOP_ACTIONS.items():
    _spec["handler"] = _required(_name, _spec["handler"])
del _name, _spec
