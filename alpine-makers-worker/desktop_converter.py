"""Convertisseur 2D/3D (Alpine Forge 3D) pour Alpine Makers Desktop, outil « converter » de ``desktop_actions``.

Le calcul se fait SUR CE PC, par les mêmes lanceurs que les jobs « converter » venus du site :

- ``runners/freecad.py``     : conversion de format (``freecad_converter.py``) et « Préparer pour une CNC »
  (``parameters.cam``, projet déclaratif revalidé par ``cam_payload`` puis calculé par ``freecad_cam.py``) ;
- ``runners/mesh_repair.py`` : analyse et réparation de maillage (``mesh_repair_tool``), seule voie vers 3MF / glTF.

Chaque calcul a son dossier ``runtime/desktop/work/converter/<run>`` (``job.json`` de la même forme qu'un job du site,
``execution_id`` propre, annulation par ``.cancel.json`` : le lanceur FreeCAD arrête lui-même SON processus FreeCAD).
Les fichiers produits vont dans ``runtime/desktop/files/converter/<run>/`` avec ``run.json`` (historique local).

Sources admises (jamais un chemin arbitraire du PC) : un dépôt de worker.exe (``runtime/desktop/inbox/<hex>/<nom>``,
consommé), un fichier du cache de la Bibliothèque (``runtime/desktop/cache/library``, téléchargé par l'outil
« library ») ou un fichier déjà produit ici (enchaînement).

Règle de mise à disposition du G-code CNC : la même que le site (``cam_routes.blocked_by``) — le G-code reste retenu
(``work/converter/held-<run>``) tant que le contrôle indépendant signale une erreur, qu'une erreur de fabricabilité est
signalée ou qu'un avertissement acquittable n'a pas été acquitté (``cam-release``). Rien n'est jamais envoyé à une
machine : l'envoi passe par Alpine CNC Studio et les gardes du site.

Le site n'est joint que pour ce qui n'existe que là (``site`` → route signée ``/api/worker-protocol/self/converter``) :
table des formats, catalogue CNC, avances suggérées, contrôle des capacités, projets et préréglages CNC du compte.
Les deux tables lues sont gardées en cache pour travailler hors ligne.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from importlib import util as import_util
from pathlib import Path

if __package__:
    from . import desktop_actions as desk
    from . import desktop_library as library
    from .desktop_actions import TaskCancelled, TaskError, v_bool, v_choice, v_hex, v_int, v_object, v_session, v_text
    from .job_state import atomic_json, read_json
    from .local_control import checked
    from .process_control import process_identity, terminate_process_tree
    from .safety import is_link
else:
    import desktop_actions as desk
    import desktop_library as library
    from desktop_actions import TaskCancelled, TaskError, v_bool, v_choice, v_hex, v_int, v_object, v_session, v_text
    from job_state import atomic_json, read_json
    from local_control import checked
    from process_control import process_identity, terminate_process_tree
    from safety import is_link

TOOL = "converter"
ROUTE = "/api/worker-protocol/self/converter"
RUNNERS = Path(__file__).resolve().with_name("runners")   # remplacé par les tests
ENGINE_DIR = Path(__file__).resolve().parent              # freecad_converter.py, freecad_cam.py (paquet du Worker)
SOURCE_MAX = 512 * 1024 * 1024                            # même plafond que les lanceurs
RUN_TIMEOUT = 3700                                        # le lanceur FreeCAD a son propre délai (3600 s)
CANCEL_GRACE = 15                                         # annulation : délai laissé au lanceur avant l'arrêt de son arbre
INLINE_MAX = 192 * 1024
GCODE_TEXT_MAX = 8 * 1024 * 1024
CACHE_MAX = 4 * 1024 * 1024
# Sorties écrites par chaque moteur : copie des listes des lanceurs (runners/freecad.py, mesh_repair_tool.py), les mêmes
# que converter_formats.py publie ; tests/test_worker_desktop_converter.py vérifie qu'elles ne divergent pas.
FREECAD_OUTPUTS = ("fcstd", "step", "stp", "iges", "igs", "brep", "brp", "stl", "obj", "ply", "off", "amf", "dxf",
                   "svg", "dae", "iv", "wrl", "vrml")
FREECAD_INPUTS = ("fcstd", "step", "stp", "iges", "igs", "brep", "brp", "stl", "obj", "ply", "off", "amf", "3mf",
                  "dae", "dxf", "svg")
MESH_INPUTS = ("stl", "obj", "3mf")
MESH_OUTPUTS = ("stl", "3mf", "obj", "ply", "glb", "gltf", "off")       # obj/ply/glb/gltf/off : trimesh requis
MESH_ONLY_OUTPUTS = ("3mf", "glb", "gltf")                              # aucun autre moteur ne les écrit
PYTHON_MESH_OUTPUTS = ("stl", "3mf")                                    # sans trimesh
ALIASES = {"stp": "step", "igs": "iges", "brp": "brep", "vrml": "wrl"}
CACHE_FILES = {"formats": "formats.json", "cam-catalog": "cam_catalog.json", "cam-capabilities": "cam_capabilities.json"}
SITE_OPS = ("formats", "cam-catalog", "cam-feeds", "cam-check", "cam-projects", "cam-project-get", "cam-project-create",
            "cam-project-update", "cam-presets", "cam-preset-apply", "cam-preset-create")
SITE_READS = ("formats", "cam-catalog", "cam-feeds", "cam-check", "cam-projects", "cam-project-get", "cam-presets")
GCODE_SUFFIXES = (".nc", ".ngc", ".tap", ".gcode")
_RUN = re.compile(r"[0-9a-f]{32}")
_FILE_NAME = re.compile(r"[^\\/:*?\"<>|\x00-\x1f]{1,200}")


# --- Validateurs ------------------------------------------------------------------------------------------------

def v_file_name(value):
    if type(value) is not str or not _FILE_NAME.fullmatch(value) or value in {".", ".."} or value != value.strip() \
            or value.endswith("."):
        raise ValueError("Nom de fichier invalide.")
    return value


def v_format(value):
    value = value.lower().lstrip(".") if isinstance(value, str) else value
    if value not in set(FREECAD_OUTPUTS) | set(MESH_OUTPUTS):
        raise ValueError("Format de sortie inconnu du convertisseur.")
    return value


def v_source(value):
    """Source d'un calcul : dépôt de worker.exe, fichier du cache de la Bibliothèque, ou résultat déjà produit ici."""
    value = v_object(depth=1, keys=4)(value)
    kind = value.get("kind")
    if kind == "staged" and set(value) == {"kind", "staged", "name"}:
        return {"kind": kind, "staged": v_hex(32)(value["staged"]), "name": v_file_name(value["name"])}
    if kind == "library" and set(value) == {"kind", "file"}:
        return {"kind": kind, "file": v_file_name(value["file"])}
    if kind == "result" and set(value) == {"kind", "run", "name"}:
        return {"kind": kind, "run": v_hex(32)(value["run"]), "name": v_file_name(value["name"])}
    raise ValueError("Source invalide : dépôt, bibliothèque ou résultat attendu.")


def v_number(minimum, maximum):
    def check(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= float(value) <= maximum:
            raise ValueError(f"Paramètre local invalide : nombre de {minimum} à {maximum} attendu.")
        return float(value)
    return check


def v_codes(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 64 or any(
            type(code) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", code) for code in value):
        raise ValueError("Liste d'avertissements à acquitter invalide.")
    return list(dict.fromkeys(value))


def v_true(value):
    if value is not True:
        raise ValueError("Suppression non confirmée : confirme-la dans l'interface.")
    return value


# --- Règle de mise à disposition du G-code (copie exacte de cam_routes.blocked_by / ackable_codes) ----------------

def blocked_by(result, acknowledged):
    """Codes qui retiennent encore le G-code : erreurs (jamais acquittables) puis avertissements non acquittés."""
    validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
    codes = [str(entry.get("code")) for entry in validation.get("errors") or [] if isinstance(entry, dict)]
    codes += [str(entry.get("code")) for entry in result.get("manufacturability") or []
              if isinstance(entry, dict) and entry.get("severity") == "error"]
    codes += [str(entry.get("code")) for entry in validation.get("warnings") or []
              if isinstance(entry, dict) and str(entry.get("code")) not in acknowledged]
    if not result.get("gcode_file"):
        codes.append("gcode_missing")
    return list(dict.fromkeys(code for code in codes if code))


def ackable_codes(result):
    validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
    return {str(entry.get("code")) for entry in validation.get("warnings") or []
            if isinstance(entry, dict) and entry.get("ackable", True)}


# --- Dossiers ----------------------------------------------------------------------------------------------------

def _extension(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _canonical(extension):
    return ALIASES.get(extension, extension)


def _run_dir(agent, area, run):
    if not _RUN.fullmatch(run):
        raise ValueError("Identifiant de calcul invalide.")
    desk.runtime_folder(agent, area, TOOL)
    return checked(agent.root, f"runtime/desktop/{area}/{TOOL}/{run}")


def _held_dir(agent, run):
    if not _RUN.fullmatch(run):
        raise ValueError("Identifiant de calcul invalide.")
    desk.runtime_folder(agent, "work", TOOL)
    return checked(agent.root, f"runtime/desktop/work/{TOOL}/held-{run}")


def _cache_path(agent, what):
    desk.runtime_folder(agent, "cache", TOOL)
    return checked(agent.root, f"runtime/desktop/cache/{TOOL}/{CACHE_FILES[what]}")


def _display_name(file):
    """Nom d'un fichier du cache de la Bibliothèque sans son préfixe d'empreinte (« 0123456789ab-piece.stl »)."""
    match = re.fullmatch(r"[0-9a-f]{12}-(?:apercu-)?(.+)", file)
    return match.group(1) if match else file


def resolve_source(agent, source):
    """(chemin, nom affiché, consommé ?) ; refus immédiat (ValueError) d'une source absente, liée ou trop grosse."""
    if source["kind"] == "staged":
        path = library.inbox_file(agent, source["staged"], source["name"])
        name, consumed = source["name"], True
    elif source["kind"] == "library":
        if source["file"].endswith(".part"):
            raise ValueError("Téléchargement de la Bibliothèque inachevé.")
        path = checked(agent.root, "runtime/desktop/cache/library/" + source["file"])
        name, consumed = _display_name(source["file"]), False
    else:
        path = _run_dir(agent, "files", source["run"]) / source["name"]
        name, consumed = source["name"], False
    if is_link(path.parent) or is_link(path) or not path.is_file():
        raise ValueError("Fichier source introuvable : choisis-le de nouveau.")
    size = path.stat().st_size
    if size <= 0 or size > SOURCE_MAX:
        raise ValueError(f"Fichier source vide ou trop gros (au plus {SOURCE_MAX // 1024 // 1024} Mio).")
    return path, name, consumed


def _job_name(name, fallback="source"):
    """Nom du fichier dans le dossier du calcul : celui de la source, nettoyé (le lanceur refuse un nom exotique)."""
    stem, dot, extension = name.rpartition(".")
    stem = re.sub(r"[^A-Za-z0-9 ._\-()'éèêëàâçôöûùüîïÉÈÊÀÂÇÔÛÎ]+", "_", stem if dot else name).strip(" ._") or fallback
    if stem.split(".")[0].lower() in {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}:
        stem = "_" + stem
    return (stem[:100] + ("." + extension.lower() if dot and extension else "")).strip()


def _stage(agent, source, run, target_name=None):
    """Copie (ou déplace, pour un dépôt) la source dans le dossier du calcul ; rend (dossier, nom dans le dossier)."""
    path, name, consumed = resolve_source(agent, source)
    workdir = _run_dir(agent, "work", run)
    workdir.mkdir(parents=True, exist_ok=False)
    local = target_name or _job_name(name)
    destination = workdir / local
    if consumed:
        shutil.move(str(path), str(destination))
        shutil.rmtree(path.parent, ignore_errors=True)
    else:
        shutil.copyfile(path, destination)
    return workdir, local, name


# --- Lanceurs ----------------------------------------------------------------------------------------------------

def _environment(agent):
    try:
        if __package__:
            from .storage_paths import WorkerStorage
        else:
            from storage_paths import WorkerStorage
        environment = WorkerStorage(agent.root, agent.config).environment()
    except Exception:  # noqa: BLE001 — l'agent de test ou un ancien réglage : environnement du processus
        environment = dict(os.environ)
    environment["ALPINE_WORKER_ROOT"] = str(agent.root)
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _freecad(agent):
    finder = getattr(agent, "_freecad_executable", None)
    try:
        return finder() if callable(finder) else None
    except Exception:  # noqa: BLE001
        return None


def run_runner(agent, task, runner, workdir, parameters, run):
    """Lance ``runners/<runner>.py`` sur ``workdir/job.json`` et attend sa fin ; rend son ``result``.

    Annulation : marqueur ``.cancel.json`` (le lanceur FreeCAD arrête lui-même SON processus FreeCAD, jamais un autre) ;
    la réparation de maillage n'a pas de processus externe : son lanceur est arrêté s'il ne s'arrête pas seul."""
    handler = RUNNERS / f"{runner}.py"
    if not handler.is_file():
        raise TaskError("Lanceur du convertisseur absent : lance « Réparer » dans le Gestionnaire.", "RUNNER_MISSING")
    environment = _environment(agent)
    if runner == "freecad":
        freecad = _freecad(agent)
        if not freecad:
            raise TaskError("FreeCAD n'est pas installé sur ce Worker : installe-le dans Mes Workers (Convertisseur 3D).",
                            "FREECAD_MISSING")
        environment["FREECAD_CMD"] = freecad
    execution = secrets.token_hex(16)
    atomic_json(workdir / "job.json", {"job_id": "desktop-" + run, "execution_id": execution, "tool_id": "converter",
                                       "parameters": parameters})
    with (workdir / "runner.log").open("a", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, str(handler), str(workdir / "job.json"), str(workdir)],
                                   stdout=log, stderr=subprocess.STDOUT, env=environment, cwd=str(workdir),
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if hasattr(task, "attach"):
            task.attach(process)
        started, cancel_at, forced = time.monotonic(), None, False
        while process.poll() is None:
            now = time.monotonic()
            if task.cancelled() and cancel_at is None:
                atomic_json(workdir / ".cancel.json", {"execution_id": execution})
                cancel_at = now
            if runner != "freecad" and cancel_at is not None and now - cancel_at > 3:
                process.kill()   # calcul Python pur dans ce seul processus : rien d'autre ne calcule
            if now - started > RUN_TIMEOUT and runner != "freecad":
                process.kill()
            # Agent 1.50.33 : un lanceur FreeCAD qui n'a pas coupé SON FreeCAD après l'annulation (ou au-delà de son
            # propre délai) est arrêté avec tout son arbre : lanceur, FreeCADCmd et ses enfants, rien d'autre.
            late = cancel_at is not None and now - cancel_at > CANCEL_GRACE
            if runner == "freecad" and not forced and (late or now - started > RUN_TIMEOUT + 300):
                forced = True
                stop_tree(process)
            state = read_json(workdir / ".runner-state.json", {}) or {}
            if isinstance(state.get("progress"), (int, float)) or state.get("message"):
                task.update(state.get("progress") if isinstance(state.get("progress"), (int, float)) else None,
                            str(state.get("message") or "Calcul en cours…"))
            time.sleep(0.3)
        process.wait()
    state = read_json(workdir / ".runner-state.json", {}) or {}
    if task.cancelled() or state.get("state") == "cancelled":
        raise TaskCancelled()
    if state.get("state") != "completed":
        message = str(state.get("error") or "").strip()
        if not message:
            message = "Le moteur s'est arrêté sans résultat (voir runner.log)."
        raise TaskError(message[:500], str(state.get("error_code") or "ENGINE_FAILED")[:60])
    return dict(state.get("result") or {})


def stop_tree(process):
    """Arrêt forcé du lanceur et de ses descendants vérifiés (date de création) ; repli : le lanceur seul."""
    try:
        identity = process_identity(process.pid)
        if identity and terminate_process_tree(process.pid, identity)["stopped"]:
            return True
    except (PermissionError, OSError, ValueError):
        pass
    try:
        process.kill()
    except OSError:
        pass
    return False


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _deliver(agent, run, workdir, names, held=()):
    """Déplace les fichiers déclarés par le lanceur vers ``files/converter/<run>`` (ou ``held-<run>``) ; rend leur liste."""
    output = _run_dir(agent, "files", run)
    output.mkdir(parents=True, exist_ok=True)
    delivered = []
    for name in names:
        source = (workdir / str(name)).resolve()
        if source.parent != workdir.resolve() or not source.is_file() or is_link(source):
            raise TaskError("Le moteur a déclaré un fichier invalide.", "ARTIFACT_INVALID")
        target_dir = _held_dir(agent, run) if source.name in held else output
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        shutil.move(str(source), str(target))
        delivered.append({"name": target.name, "path": str(target), "bytes": target.stat().st_size,
                          "sha256": _sha256(target), "held": source.name in held})
    return delivered


def _finish(agent, value):
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > INLINE_MAX:
        return desk.result_file(agent, value)
    return value


def _record(agent, run, kind, source_name, files, extra=None):
    record = {"run": run, "kind": kind, "source": source_name, "created": time.time(),
              "files": [{"name": item["name"], "bytes": item["bytes"], "sha256": item["sha256"], "held": item["held"]}
                        for item in files]}
    record.update(extra or {})
    atomic_json(_run_dir(agent, "files", run) / "run.json", record)
    return record


def _cleanup(workdir):
    shutil.rmtree(workdir, ignore_errors=True)


def _started(agent, label, work, journal=False):
    """Ouvre la tâche ; un refus immédiat (source absente…) reste une ValueError sans tâche. ``journal`` : calcul de ce
    PC inscrit au journal local des jobs (conversion, analyse, réparation, calcul CNC ; jamais une lecture du site)."""
    return desk.start_task(agent, TOOL, label, work, journal=journal)


# --- Actions : état et caches ------------------------------------------------------------------------------------

def _status(agent, params):
    freecad = bool(_freecad(agent))
    cache = {}
    for what in CACHE_FILES:
        try:
            path = _cache_path(agent, what)
            cache[what] = path.stat().st_mtime if path.is_file() else None
        except (OSError, ValueError):
            cache[what] = None
    trimesh = import_util.find_spec("trimesh") is not None
    return {
        "freecad": freecad,
        "freecad_converter": (ENGINE_DIR / "freecad_converter.py").is_file(),
        "freecad_cam": (ENGINE_DIR / "freecad_cam.py").is_file() and (ENGINE_DIR / "cam_payload.py").is_file(),
        "mesh_repair": (RUNNERS / "mesh_repair.py").is_file(),
        "freecad_runner": (RUNNERS / "freecad.py").is_file(),
        "trimesh": trimesh, "numpy": import_util.find_spec("numpy") is not None,
        "writable": {"freecad": list(FREECAD_OUTPUTS) if freecad else [],
                     "mesh_repair": list(MESH_OUTPUTS if trimesh else PYTHON_MESH_OUTPUTS)},
        "readable": {"freecad": list(FREECAD_INPUTS) if freecad else [], "mesh_repair": list(MESH_INPUTS)},
        "cache": cache, "offline_capable": True,
    }


def _cached(agent, params):
    path = _cache_path(agent, params["what"])
    if not path.is_file() or is_link(path):
        return {"what": params["what"], "missing": True}
    value = read_json(path, {}) or {}
    return _finish(agent, {"what": params["what"], "missing": False, "cached_at": path.stat().st_mtime, "data": value})


def _site_error(error):
    failure = library._site_error(error)
    if failure.code == "SITE_MISSING":
        return TaskError("Le Dashboard n'a pas encore le Convertisseur Desktop : il doit être redémarré dans sa nouvelle "
                         "version. Le calcul local reste disponible.", "SITE_MISSING")
    return failure


def _site(agent, params):
    session = params.pop("session_token")
    op = params["op"]
    # Les arguments voyagent en texte JSON : un projet CNC dépasse la profondeur admise par l'action « desktop ».
    try:
        args = json.loads(params.get("args_json") or "{}")
    except ValueError:
        raise ValueError("Arguments du site illisibles.") from None
    if not isinstance(args, dict):
        raise ValueError("Arguments du site : objet JSON attendu.")

    def work(task):
        try:
            answer = agent.request(ROUTE, "POST", {"op": op, "args": args, "session_token": session})
        except Exception as error:
            raise _site_error(error) from None
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            raise TaskError(str((answer or {}).get("error") or "Réponse du site illisible.")[:300],
                            str((answer or {}).get("code") or "")[:60])
        response = answer.get("response") if isinstance(answer.get("response"), dict) else {}
        if op in ("formats", "cam-catalog"):
            text = json.dumps(response, ensure_ascii=False)
            if len(text.encode("utf-8")) <= CACHE_MAX:
                atomic_json(_cache_path(agent, op), response)
        return _finish(agent, {"op": op, "response": response})
    return _started(agent, "Site : " + op, work)


# --- Actions : conversion, analyse, réparation ------------------------------------------------------------------

REPAIR_KEYS = ("fill_holes", "remove_degenerate", "fix_normals", "target_faces", "decimate_ratio")


def _repair_parameters(params, local, output_format, output_name=None):
    parameters = {"task": "mesh_repair", "input_file": {"name": local, "path": ""}, "output_format": output_format}
    for key in REPAIR_KEYS:
        if key in params:
            parameters[key] = params[key]
    if output_name:
        parameters["output_name"] = output_name
    return parameters


def _check_output_name(name, extension):
    if name is None:
        return None
    if not name.lower().endswith("." + extension):
        name += "." + extension
    return _job_name(name, "converti")


def _convert(agent, params):
    source, target = params["source"], params["output_format"]
    _path, name, _consumed = resolve_source(agent, source)     # refus immédiat
    src = _extension(name)
    repair_first = params.get("repair_first", False)
    if src not in set(FREECAD_INPUTS) | set(MESH_INPUTS):
        raise ValueError(f"Format source « .{src or '?'} » non lu par les moteurs du convertisseur.")
    if _canonical(src) == _canonical(target) and not repair_first:
        raise ValueError("Même format : rien à convertir (coche « Réparer avant » pour réparer seulement).")
    mesh_only = target in MESH_ONLY_OUTPUTS
    if (mesh_only or repair_first) and src not in MESH_INPUTS:
        raise ValueError("Ce format n'est produit que par la réparation de maillage (entrées STL, OBJ, 3MF) : "
                         "convertis d'abord en STL.")
    if src in ("dxf", "svg") and target not in ("dxf", "svg", "fcstd", "step", "stp", "iges", "igs", "brep", "brp"):
        raise ValueError("Un dessin 2D n'a pas de surface à mailler : extrude-le d'abord (création paramétrique).")
    output_name = _check_output_name(params.get("output_name"), target)
    run = secrets.token_hex(16)

    def work(task):
        workdir, local, display = _stage(agent, source, run)
        try:
            steps, report = [], None
            current = local
            if mesh_only or (repair_first and target in MESH_OUTPUTS and _canonical(src) == _canonical(target)):
                task.update(5, "Réparation et écriture du maillage…")
                parameters = _repair_parameters(params, current, target, output_name)
                parameters["input_file"]["path"] = str(workdir / current)
                result = run_runner(agent, task, "mesh_repair", workdir, parameters, run)
                steps.append({"engine": "mesh_repair", "report": result.get("report")})
                artifacts = result.get("artifacts") or []
            else:
                if repair_first:
                    task.update(5, "Réparation avant conversion…")
                    parameters = _repair_parameters(params, current, "stl", Path(current).stem + "_repare.stl")
                    parameters["input_file"]["path"] = str(workdir / current)
                    result = run_runner(agent, task, "mesh_repair", workdir, parameters, run)
                    steps.append({"engine": "mesh_repair", "report": result.get("report")})
                    current = (result.get("artifacts") or [current])[0]
                    for marker in (".runner-state.json", ".cancel.json", "job.json"):
                        try:
                            (workdir / marker).unlink()
                        except OSError:
                            pass
                task.update(30 if repair_first else 10, "Conversion par FreeCAD…")
                parameters = {"input_file": {"name": current, "path": str(workdir / current)}, "output_format": target}
                if output_name:
                    parameters["output_name"] = output_name
                for key in ("linear_deflection", "max_cad_facets"):
                    if key in params:
                        parameters[key] = params[key]
                result = run_runner(agent, task, "freecad", workdir, parameters, run)
                report = {key: value for key, value in result.items() if key != "artifacts"}
                steps.append({"engine": "freecad", "report": report})
                artifacts = result.get("artifacts") or []
            if not artifacts:
                raise TaskError("Le moteur n'a produit aucun fichier.", "NO_OUTPUT")
            files = _deliver(agent, run, workdir, artifacts)
            record = _record(agent, run, "conversion", display, files,
                             {"from": src, "to": target, "steps": [step["engine"] for step in steps],
                              "options": {key: params[key] for key in params if key not in ("source",)}})
            return _finish(agent, {"run": run, "files": files, "steps": steps, "record": record})
        finally:
            _cleanup(workdir)
    return _started(agent, f"Conversion {name} → .{target}", work, journal=True)


def _analyze(agent, params):
    source = params["source"]
    _path, name, _consumed = resolve_source(agent, source)
    if _extension(name) not in MESH_INPUTS:
        raise ValueError("L'analyse lit les maillages STL, OBJ et 3MF : convertis d'abord ce fichier en STL.")
    run = secrets.token_hex(16)

    def work(task):
        workdir, local, display = _stage(agent, source, run)
        try:
            parameters = {"task": "mesh_repair", "analyze_only": True,
                          "input_file": {"name": local, "path": str(workdir / local)}}
            result = run_runner(agent, task, "mesh_repair", workdir, parameters, run)
            return {"name": display, "report": result.get("report") or {}, "engine": result.get("engine") or ""}
        finally:
            _cleanup(workdir)
    return _started(agent, "Analyse " + name, work, journal=True)


def _repair(agent, params):
    source = params["source"]
    _path, name, _consumed = resolve_source(agent, source)
    if _extension(name) not in MESH_INPUTS:
        raise ValueError("La réparation lit les maillages STL, OBJ et 3MF.")
    output_format = params.get("output_format", "stl")
    if output_format not in MESH_OUTPUTS:
        raise ValueError("Sortie de réparation : STL, 3MF (ou OBJ, PLY, OFF, GLB, glTF si trimesh est installé).")
    output_name = _check_output_name(params.get("output_name"), output_format)
    run = secrets.token_hex(16)

    def work(task):
        workdir, local, display = _stage(agent, source, run)
        try:
            parameters = _repair_parameters(params, local, output_format, output_name)
            parameters["input_file"]["path"] = str(workdir / local)
            result = run_runner(agent, task, "mesh_repair", workdir, parameters, run)
            files = _deliver(agent, run, workdir, result.get("artifacts") or [])
            if not files:
                raise TaskError("La réparation n'a produit aucun fichier.", "NO_OUTPUT")
            record = _record(agent, run, "repair", display, files, {"from": _extension(name), "to": output_format,
                                                                  "report": result.get("report")})
            return _finish(agent, {"run": run, "files": files, "report": result.get("report") or {},
                                   "engine": result.get("engine") or "", "record": record})
        finally:
            _cleanup(workdir)
    return _started(agent, "Réparation " + name, work, journal=True)


# --- Actions : Préparer pour une CNC ----------------------------------------------------------------------------

def _cam_payload():
    try:
        if __package__:
            from . import cam_payload
        else:
            import cam_payload
    except ImportError:
        raise ValueError("Le moteur CNC manque sur ce Worker : mets le Worker à jour.") from None
    return cam_payload


def _validate_project(project):
    module = _cam_payload()
    try:
        return module.validate_cam_project(project), []
    except ValueError as error:
        return None, [{"code": "CAM_PROJECT_INVALID", "message": str(error), "path": str(getattr(error, "path", "") or "")}]


def _cam_validate(agent, params):
    project, errors = _validate_project(params["project"])
    cache = _cache_path(agent, "cam-capabilities")
    capabilities = read_json(cache, None) if cache.is_file() else None
    return {"ok": not errors, "errors": errors, "project": project,
            "capabilities_read": bool(capabilities), "capabilities_at": cache.stat().st_mtime if capabilities else None}


def _cam_capabilities(agent, params):
    run = secrets.token_hex(16)

    def work(task):
        workdir = _run_dir(agent, "work", run)
        workdir.mkdir(parents=True, exist_ok=False)
        try:
            task.update(5, "Lecture de l'atelier CAM de FreeCAD…")
            run_runner(agent, task, "freecad", workdir, {"cam": {"op": "capabilities"}}, run)
            path = workdir / "cam_capabilities.json"
            if not path.is_file() or path.stat().st_size > CACHE_MAX:
                raise TaskError("Capacités FreeCAD CAM illisibles.", "CAM_RESULT_INVALID")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise TaskError("Capacités FreeCAD CAM illisibles.", "CAM_RESULT_INVALID")
            atomic_json(_cache_path(agent, "cam-capabilities"), value)
            return _finish(agent, {"capabilities": value})
        finally:
            _cleanup(workdir)
    return _started(agent, "Capacités FreeCAD CAM", work)


def _cam_compute(agent, params):
    project, errors = _validate_project(params["project"])
    if errors:
        raise ValueError(errors[0]["message"])
    source = params["source"]
    _path, name, _consumed = resolve_source(agent, source)
    if _extension(name) != project["source"]["kind"]:
        raise ValueError("Le fichier choisi ne correspond pas au type de source du projet.")
    run = secrets.token_hex(16)

    def work(task):
        workdir, local, display = _stage(agent, source, run, project["source"]["file"])
        try:
            task.update(5, "Calcul FreeCAD CAM…")
            parameters = {"cam": {"op": "compute", "project": project}, "output_name": project["output"]["stem"],
                          "input_file": {"name": local, "path": str(workdir / local)}}
            details = run_runner(agent, task, "freecad", workdir, parameters, run)
            result_path = workdir / "cam_result.json"
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise TaskError("Résultat du moteur CNC illisible.", "CAM_RESULT_INVALID") from None
            if not isinstance(result, dict):
                raise TaskError("Résultat du moteur CNC illisible.", "CAM_RESULT_INVALID")
            gcode = str(result.get("gcode_file") or details.get("gcode_file") or "")
            artifacts = [item for item in details.get("artifacts") or []]
            blocking = blocked_by(result, set())
            # Comme le site : rien de bloquant → G-code livré tout de suite ; sinon retenu jusqu'à « cam-release ».
            held = {gcode} if blocking and gcode and gcode.lower().endswith(GCODE_SUFFIXES) else set()
            files = _deliver(agent, run, workdir, artifacts, held)
            record = _record(agent, run, "cam", display, files,
                             {"project_name": project["name"], "gcode_file": gcode, "acknowledged": [],
                              "blocked_by": blocking, "machine": project["machine"]["label"]})
            light = {key: value for key, value in result.items() if key != "simulation"}   # parcours : relus du G-code
            return _finish(agent, {"run": run, "files": files, "result": light, "blocked_by": blocking,
                                   "ackable": sorted(ackable_codes(result)), "gcode_file": gcode, "record": record})
        finally:
            _cleanup(workdir)
    return _started(agent, "Calcul CNC " + project["name"], work, journal=True)


def _run_record(agent, run):
    folder = _run_dir(agent, "files", run)
    record = read_json(folder / "run.json", None) if (folder / "run.json").is_file() else None
    if not isinstance(record, dict):
        raise ValueError("Calcul introuvable sur ce PC.")
    return folder, record


def _cam_result(folder):
    try:
        value = json.loads((folder / "cam_result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Résultat CNC introuvable ou illisible.") from None
    if not isinstance(value, dict):
        raise ValueError("Résultat CNC illisible.")
    return value


def _cam_release(agent, params):
    """Acquitte des avertissements (jamais une erreur) ; libère le G-code s'il ne reste plus rien de bloquant."""
    folder, record = _run_record(agent, params["run"])
    if record.get("kind") != "cam":
        raise ValueError("Ce calcul n'est pas un calcul CNC.")
    result = _cam_result(folder)
    allowed = ackable_codes(result)
    refused = [code for code in params["acknowledged"] if code not in allowed]
    if refused:
        raise ValueError(f"« {refused[0]} » n'est pas un avertissement acquittable de ce calcul (une erreur ne "
                         "s'acquitte jamais).")
    acknowledged = list(dict.fromkeys(list(record.get("acknowledged") or []) + params["acknowledged"]))
    blocking = blocked_by(result, set(acknowledged))
    gcode = str(record.get("gcode_file") or "")
    released = None
    if gcode and not blocking:
        held = _held_dir(agent, params["run"]) / gcode
        target = folder / gcode
        if held.is_file() and not is_link(held):
            shutil.move(str(held), str(target))
            shutil.rmtree(held.parent, ignore_errors=True)
        if target.is_file():
            released = {"name": gcode, "path": str(target), "bytes": target.stat().st_size, "sha256": _sha256(target),
                        "held": False}
            for item in record.get("files") or []:
                if item.get("name") == gcode:
                    item["held"] = False
    record.update(acknowledged=acknowledged, blocked_by=blocking)
    atomic_json(folder / "run.json", record)
    return {"run": params["run"], "acknowledged": acknowledged, "blocked_by": blocking, "gcode": released}


def _cam_gcode(agent, params):
    """Texte du G-code pour l'aperçu des parcours (lecture seule, même retenu) ; refusé si le contrôle a des erreurs,
    comme la route d'artefact du site."""
    folder, record = _run_record(agent, params["run"])
    gcode = str(record.get("gcode_file") or "")
    if record.get("kind") != "cam" or not gcode:
        raise ValueError("Ce calcul n'a pas produit de G-code.")
    validation = _cam_result(folder).get("validation") or {}
    if isinstance(validation, dict) and validation.get("errors"):
        raise ValueError("G-code retenu : le contrôle indépendant a trouvé des erreurs bloquantes.")
    path = folder / gcode
    if not path.is_file():
        path = _held_dir(agent, params["run"]) / gcode
    if not path.is_file() or is_link(path):
        raise ValueError("G-code introuvable sur ce PC.")
    with open(path, "rb") as source:
        data = source.read(GCODE_TEXT_MAX + 1)
    return desk.result_file(agent, {"run": params["run"], "name": gcode, "truncated": len(data) > GCODE_TEXT_MAX,
                                    "text": data[:GCODE_TEXT_MAX].decode("utf-8", errors="replace"),
                                    "released": path.parent == folder})


# --- Actions : fichiers produits ---------------------------------------------------------------------------------

def _runs(agent, params):
    root = desk.runtime_folder(agent, "files", TOOL)
    items = []
    for folder in sorted(root.iterdir(), key=lambda item: item.name):
        if not folder.is_dir() or is_link(folder) or not _RUN.fullmatch(folder.name):
            continue
        record = read_json(folder / "run.json", None) if (folder / "run.json").is_file() else None
        if isinstance(record, dict):
            record = dict(record, folder=str(folder))
            for item in record.get("files") or []:
                item["path"] = str((_held_dir(agent, folder.name) if item.get("held") else folder) / str(item.get("name")))
            items.append(record)
    items.sort(key=lambda record: -float(record.get("created") or 0))
    return _finish(agent, {"folder": str(root), "runs": items[:300], "total": len(items)})


def _forget(agent, params):
    folder = _run_dir(agent, "files", params["run"])
    held = _held_dir(agent, params["run"])
    if not folder.is_dir():
        raise ValueError("Calcul introuvable sur ce PC.")
    shutil.rmtree(folder, ignore_errors=True)
    shutil.rmtree(held, ignore_errors=True)
    return {"run": params["run"], "deleted": not folder.exists()}


# --- Déclaration ----------------------------------------------------------------------------------------------------

REPAIR_PARAMS = {"fill_holes": v_bool, "remove_degenerate": v_bool, "fix_normals": v_bool,
                 "target_faces": v_int(4, 5000000), "decimate_ratio": v_number(0.01, 1.0)}
DESKTOP_ACTIONS = {
    "status": {"handler": _status, "read": True, "params": {}},
    "cached": {"handler": _cached, "read": True, "params": {"what": v_choice(*CACHE_FILES)}},
    "site": {"handler": _site, "read": True, "params": {"session_token": v_session, "op": v_choice(*SITE_OPS),
                                                        "args_json": v_text(900000)}},
    "convert": {"handler": _convert, "read": True, "params": {
        "source": v_source, "output_format": v_format, "output_name": v_text(120, empty=False),
        "linear_deflection": v_number(0.001, 10.0), "max_cad_facets": v_int(25000, 1000000), "repair_first": v_bool,
        **REPAIR_PARAMS}},
    "analyze": {"handler": _analyze, "read": True, "params": {"source": v_source}},
    "repair": {"handler": _repair, "read": True, "params": {
        "source": v_source, "output_format": v_format, "output_name": v_text(120, empty=False), **REPAIR_PARAMS}},
    "cam-capabilities": {"handler": _cam_capabilities, "read": True, "params": {}},
    "cam-validate": {"handler": _cam_validate, "read": True, "params": {"project": v_object(depth=6, keys=200)}},
    "cam-compute": {"handler": _cam_compute, "read": True, "params": {"source": v_source,
                                                                       "project": v_object(depth=6, keys=200)}},
    "cam-release": {"handler": _cam_release, "read": True, "params": {"run": v_hex(32), "acknowledged": v_codes}},
    "cam-gcode": {"handler": _cam_gcode, "read": True, "params": {"run": v_hex(32)}},
    "runs": {"handler": _runs, "read": True, "params": {}},
    "forget": {"handler": _forget, "read": True, "params": {"run": v_hex(32), "confirm": v_true}},
}
REQUIRED = {"status": (), "cached": ("what",), "site": ("session_token", "op"), "convert": ("source", "output_format"),
            "analyze": ("source",), "repair": ("source",), "cam-capabilities": (), "cam-validate": ("project",),
            "cam-compute": ("source", "project"), "cam-release": ("run", "acknowledged"), "cam-gcode": ("run",),
            "runs": (), "forget": ("run", "confirm")}


def _required(name, handler):
    def run(agent, params):
        missing = [field for field in REQUIRED[name] if field not in params]
        if missing:
            raise ValueError("Paramètre manquant : " + missing[0] + (" (connecte-toi dans worker.exe)" if missing[0] == "session_token" else ""))
        return handler(agent, dict(params))
    return run


for _name, _spec in DESKTOP_ACTIONS.items():
    _spec["handler"] = _required(_name, _spec["handler"])
del _name, _spec
