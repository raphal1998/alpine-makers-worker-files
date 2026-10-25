"""Alpine Model Studio natif (Alpine Makers Desktop, worker.exe) : outil agent « model-studio » de ``desktop_actions``.

Tout ce qui calcule tourne sur CE Worker, avec les moteurs déjà installés et les MÊMES lanceurs que les jobs du site
(``runners/orca.py``, ``runners/freecad.py``, ``runners/mesh_repair.py``), dans un dossier de travail du runtime :

- ``state``          : moteur OrcaSlicer, bridge (en marche, occupé, version), FreeCAD, bibliothèques de maillage ;
- ``bridge-start``   : démarre le bridge OrcaSlicer par le chemin gardé de « engines-start » (``local_control._start_one``) ;
- ``profiles``       : imprimantes que l'OrcaSlicer de ce Worker sait trancher (``GET /profiles`` du bridge) ;
- ``process-schema`` : réglages du profil de processus de référence (chaîne ``inherits`` résolue, comme le site) ;
- ``import``         : STEP / IGES / BREP / FCStd / AMF / PLY / OFF (FreeCAD) et 3MF (lecteur Python) → STL d'aperçu ;
- ``repair``         : analyse ou réparation / décimation (``mesh_repair_tool``, comme la réparation du site) ;
- ``build``          : pièce paramétrique · CAO exacte (scène déclarative revalidée, FreeCAD) → STL ;
- ``slice``          : tranchage local OrcaSlicer, simple ou multi-filaments, avec les réglages de processus ;
- ``export-3mf``     : STL → 3MF ;
- ``outputs``        : derniers fichiers tranchés du cache local ;
- ``print`` / ``print-status`` : envoi d'un fichier DÉJÀ dans la bibliothèque du compte à l'imprimante choisie dans
  Mes équipements, par la route signée ``self/model-studio/print`` qui appelle la route d'impression du site
  (``/api/print/start_file`` : imprimante choisie, état, carte SD, propriété, onglet). Jamais d'envoi local direct.

Les fichiers de l'utilisateur n'arrivent que par le dépôt ``runtime/desktop/inbox/<hex>/<nom>`` de worker.exe ; les
résultats vont dans ``runtime/desktop/cache/model-studio`` (worker.exe les relit, les copie, les importe dans la
bibliothèque par l'outil « library »). Aucune commande physique ici sauf « print », qui passe par le site et exige
``confirm: true`` (posé par l'interface après une confirmation explicite).
"""
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path

if __package__:
    from . import desktop_actions as desk
    from .desktop_actions import TaskCancelled, TaskError, v_bool, v_choice, v_hex, v_int, v_object, v_session, v_text
    from .desktop_library import inbox_file, v_file_name
    from .job_state import read_json
    from .safety import is_link, validate_freecad_scene
else:
    import desktop_actions as desk
    from desktop_actions import TaskCancelled, TaskError, v_bool, v_choice, v_hex, v_int, v_object, v_session, v_text
    from desktop_library import inbox_file, v_file_name
    from job_state import read_json
    from safety import is_link, validate_freecad_scene

TOOL = "model-studio"
RUNNERS_DIR = Path(__file__).resolve().with_name("runners")   # remplacé par les tests
PRINT_PATH = "/api/worker-protocol/self/model-studio/print"
PRINT_STATUS_PATH = "/api/worker-protocol/self/model-studio/print-status"
PROCESS_PROFILE = "0.20mm Standard @BBL X1C.json"        # même profil de référence que le site
PROCESS_METADATA = {"type", "name", "from", "inherits", "instantiation", "description", "setting_id", "renamed_from",
                    "compatible_printers", "compatible_printers_condition"}
BOOLEAN_PROCESS_KEYS = {
    "adaptive_layer_height", "bridge_no_support", "detect_floating_vertical_shell", "detect_overhang_wall",
    "detect_thin_wall", "enable_arc_fitting", "enable_circle_compensation", "enable_height_slowdown",
    "enable_overhang_speed", "enable_prime_tower", "enable_support", "enable_tower_interface_cooldown_during_tower",
    "enable_tower_interface_features", "enable_wrapping_detection", "infill_combination", "interface_shells",
    "only_one_wall_top", "override_filament_scarf_seam_setting", "prime_tower_enable_framework",
    "prime_tower_flat_ironing", "reduce_crossing_wall", "reduce_infill_retraction",
    "seam_placement_away_from_overhangs", "support_interface_loop_pattern", "support_on_build_plate_only",
    "symmetric_infill_y_axis", "spiral_mode", "wipe_tower_no_sparse_layers", "z_direction_outwall_speed_continuous"}
CAD_FORMATS = ("step", "stp", "iges", "igs", "brep", "brp", "fcstd", "amf", "ply", "off")   # FreeCAD → STL
MESH_FORMATS = ("3mf",)                                                                    # lecteur Python → STL
SLICE_FORMATS = ("stl", "obj", "3mf", "amf", "step", "stp")                                # comme le site
BED_TYPES = ("Textured PEI Plate", "Cool Plate", "Engineering Plate", "High Temp Plate")
PRINTER_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()+\-]{0,119}")
PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,29}")
SETTING_KEY = re.compile(r"[A-Za-z0-9_]{1,80}")
MATERIAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 +._/\-]{0,39}")
RUN_LIMIT = 3700                       # un tranchage peut durer (même plafond que le runner Orca : 3600 s)
PROFILE_LIMIT = 4000
INLINE_MAX = 256 * 1024
FILES_MAX = 16


# --- Validateurs ----------------------------------------------------------------------------------------------------

def v_num(minimum, maximum):
    """Nombre JSON fini (entier ou décimal, jamais un booléen) borné."""
    def check(value):
        if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"Paramètre local invalide : nombre de {minimum:g} à {maximum:g} attendu.")
        return value
    return check


def v_regex(pattern, label):
    def check(value):
        if type(value) is not str or not pattern.fullmatch(value):
            raise ValueError(f"Paramètre local invalide : {label}.")
        return value
    return check


def v_true(value):
    if value is not True:
        raise ValueError("Envoi à l'imprimante non confirmé : confirme-le dans l'interface.")
    return value


def v_ams_mapping(value):
    if (type(value) is not list or len(value) > 32 or any(type(item) is not int or
            item not in (*range(16), *range(128, 136), 254, -1) for item in value)):
        raise ValueError("Correspondance filament / bac AMS invalide.")
    return value


# Réglages compacts du panneau (mêmes clés que ORCA_SLICE_SETTING_KEYS du site, bornes raisonnables).
SETTINGS = {
    "layer_height": v_num(0.04, 1.0), "outer_wall_line_width": v_num(0.1, 2.0), "wall_loops": v_int(0, 50),
    "top_shell_layers": v_int(0, 100), "bottom_shell_layers": v_int(0, 100), "sparse_infill_density": v_num(0, 100),
    "sparse_infill_pattern": v_regex(PATTERN, "motif de remplissage"), "enable_support": v_bool,
    "support_threshold_angle": v_num(0, 90),
    "support_type": v_choice("normal(auto)", "tree(auto)", "normal(manual)", "tree(manual)"),
    "brim_type": v_choice("auto_brim", "brim_ears", "outer_only", "inner_only", "outer_and_inner", "no_brim"),
    "brim_width": v_num(0, 50), "outer_wall_speed": v_num(1, 1000), "sparse_infill_speed": v_num(1, 1000),
    "travel_speed": v_num(1, 1500), "orient": v_bool, "arrange": v_bool, "ironing": v_bool,
    "seam_position": v_choice("aligned", "nearest", "back", "random"), "fuzzy_skin": v_bool, "prime_tower": v_bool,
    "nozzle": v_choice("0.2", "0.4", "0.6", "0.8"), "bed_type": v_choice(*BED_TYPES),
    "printer_profile": v_regex(PRINTER_PROFILE, "profil d'imprimante OrcaSlicer"),
}


def v_settings(value):
    value = v_object(depth=1, keys=len(SETTINGS))(value)
    clean = {}
    for key, item in value.items():
        if key not in SETTINGS:
            raise ValueError("Réglage de tranchage non autorisé : " + key[:40])
        clean[key] = SETTINGS[key](item)
    return clean


def v_process_settings(value):
    """Réglages avancés OrcaSlicer (clé du profil → valeur simple), comme ``process_settings`` du site."""
    value = v_object(depth=1, keys=300)(value)
    clean = {}
    for key, item in value.items():
        if not SETTING_KEY.fullmatch(key) or type(item) not in (str, int, float, bool) or len(str(item)) > 1000:
            raise ValueError("Réglage avancé invalide : " + str(key)[:40])
        if type(item) is float and not math.isfinite(item):
            raise ValueError("Réglage avancé invalide : " + key[:40])
        clean[key] = item
    return clean


def v_slice_files(value):
    """1 à 16 fichiers déposés par worker.exe : ``{"staged", "name", "material"}`` (un par filament en multi)."""
    if type(value) is not list or not 1 <= len(value) <= FILES_MAX:
        raise ValueError(f"De 1 à {FILES_MAX} fichiers à trancher.")
    clean = []
    for item in value:
        if type(item) is not dict or set(item) - {"staged", "name", "material"} or not {"staged", "name"} <= set(item):
            raise ValueError("Fichier à trancher invalide.")
        name = v_file_name(item["name"])
        if Path(name).suffix.lower().lstrip(".") not in SLICE_FORMATS:
            raise ValueError("Ce format ne peut pas être tranché par OrcaSlicer : " + name[:60])
        clean.append({"staged": v_hex(32)(item["staged"]), "name": name,
                      "material": v_regex(MATERIAL, "matériau")(item.get("material", "PLA"))})
    return clean


# --- Outils internes ------------------------------------------------------------------------------------------------

def cache_folder(agent, *parts):
    return desk.runtime_folder(agent, "cache", "model-studio", *parts)


def _finish(agent, value):
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > INLINE_MAX:
        return desk.result_file(agent, value)
    return value


def _stem(name):
    lower = name.lower()
    for suffix in (".gcode.3mf", ".3mf", ".stl", ".obj", ".amf", ".step", ".stp", ".iges", ".igs", ".brep", ".brp",
                   ".fcstd", ".ply", ".off"):
        if lower.endswith(suffix):
            name = name[:-len(suffix)]
            break
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" ._")[:80]
    return name or "modele"


def _unique(folder, name):
    candidate, index = folder / name, 2
    base, suffix = (name[:-len(".gcode.3mf")], ".gcode.3mf") if name.endswith(".gcode.3mf") else (Path(name).stem, Path(name).suffix)
    while candidate.exists():
        candidate, index = folder / f"{base}_{index}{suffix}", index + 1
    return candidate


def _consume(agent, staged, name):
    """Fichier déposé par worker.exe (vérifié par la Bibliothèque) ; son dossier est effacé après usage."""
    return inbox_file(agent, staged, name)


def _drop_inbox(path):
    shutil.rmtree(path.parent, ignore_errors=True)


def _maintenance_guard(agent):
    if getattr(agent, "maintenance_active", False) or getattr(agent, "local_cleanup_active", False):
        raise TaskError("Une installation, une maintenance ou un nettoyage est en cours sur ce Worker : réessaie après sa fin.",
                        "WORKER_BUSY")


def _orca(agent):
    try:
        executable = agent._orca_executable()
    except Exception:
        executable = None
    return Path(executable) if executable else None


def _freecad(agent):
    try:
        executable = agent._freecad_executable()
    except Exception:
        executable = None
    return str(executable) if executable and Path(str(executable)).is_file() else ""


def _port(agent):
    try:
        port = int(agent._runtime_port("model-studio") or 15064)
    except (TypeError, ValueError):
        port = 15064
    return port if 1024 <= port <= 65535 else 15064


def _bridge_open(port, timeout=0.35):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _bridge_get(port, path, timeout, limit=8 * 1024 * 1024):  # remplacé par les tests
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
        return json.loads(response.read(limit))


def _installed_bridge_version(agent):
    try:
        text = (Path(agent.root) / "orca_core_bridge.ps1").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r'^\$BridgeVersion\s*=\s*"([^"]{1,20})"', text, re.M)
    return match.group(1) if match else ""


def _environment(agent, extra):
    try:
        if __package__:
            from .storage_paths import WorkerStorage
        else:
            from storage_paths import WorkerStorage
        environment = WorkerStorage(agent.root, agent.config).environment()
    except Exception:
        environment = dict(os.environ)
    environment["ALPINE_WORKER_ROOT"] = str(agent.root)
    environment["ALPINE_WORKER_CONTROL_TOKEN_FILE"] = str(Path(agent.root) / "config" / "engine-control-token")
    environment["PYTHONIOENCODING"] = "utf-8"
    environment.update(extra)
    return environment


def _run_runner(agent, task, runner, parameters, sources, extra_env, label):
    """Exécute le lanceur ``runners/<runner>.py`` du Worker sur un job LOCAL (même contrat que les jobs du site).

    ``sources`` : fichiers déjà copiés (``Path``) ; ils sont déplacés dans le dossier de travail du job. Annulation :
    marqueur ``.cancel.json`` du lanceur (arrêt ciblé du seul processus moteur de ce job, confirmé par le lanceur).
    Rend ``(workdir, state)`` ; le dossier de travail est à effacer par l'appelant."""
    execution = "desktop-" + secrets.token_hex(12)
    workdir = desk.runtime_folder(agent, "work", "model-studio", execution)
    inputs = []
    for index, source in enumerate(sources):
        target = workdir / (f"{index}-" + source.name if len(sources) > 1 else source.name)
        shutil.copyfile(source, target)
        inputs.append({"name": target.name, "path": str(target)})
    job = {"job_id": execution, "execution_id": execution, "parameters": dict(parameters)}
    if len(inputs) == 1 and not parameters.get("multi"):
        job["parameters"]["input_file"] = inputs[0]
    elif inputs:
        job["parameters"]["input_files"] = inputs
    (workdir / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    handler = RUNNERS_DIR / f"{runner}.py"
    if not handler.is_file():
        raise TaskError("Lanceur du moteur absent sur ce Worker : lance « Réparer ».", "RUNNER_MISSING")
    started = time.monotonic()
    with (workdir / "runner.log").open("a", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, str(handler), str(workdir / "job.json"), str(workdir)],
                                   stdout=log, stderr=subprocess.STDOUT, env=_environment(agent, extra_env),
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        cancel_sent = False
        while process.poll() is None:
            elapsed = int(time.monotonic() - started)
            state = read_json(workdir / ".runner-state.json", {}) or {}
            task.update(state.get("progress") if isinstance(state.get("progress"), (int, float)) else None,
                        f"{label} · {elapsed // 60} min {elapsed % 60:02d} s" + (" · arrêt demandé…" if cancel_sent else ""))
            if task.cancelled() and not cancel_sent:
                # Le lanceur arrête le seul processus moteur de CE job et confirme l'arrêt ; rien d'autre n'est tué.
                (workdir / ".cancel.json").write_text(json.dumps({"execution_id": execution}), encoding="utf-8")
                cancel_sent = True
            if elapsed > RUN_LIMIT:
                (workdir / ".cancel.json").write_text(json.dumps({"execution_id": execution}), encoding="utf-8")
                try:
                    process.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise TaskError("Délai dépassé : travail arrêté.", "TIMEOUT")
            time.sleep(0.5)
    state = read_json(workdir / ".runner-state.json", {}) or {}
    if cancel_sent or state.get("state") == "cancelled":
        shutil.rmtree(workdir, ignore_errors=True)
        raise TaskCancelled()
    if state.get("state") != "completed":
        message = str(state.get("error") or "").strip() or "Le moteur s'est arrêté sans résultat (voir runner.log)."
        shutil.rmtree(workdir, ignore_errors=True)
        raise TaskError(message[-700:], str(state.get("error_code") or "ENGINE_FAILED")[:60])
    return workdir, state


def _artifact(workdir, state):
    names = (state.get("result") or {}).get("artifacts") or []
    if not names:
        raise TaskError("Le moteur n'a produit aucun fichier.", "NO_OUTPUT")
    path = (workdir / Path(str(names[0])).name).resolve()
    if path.parent != workdir.resolve() or not path.is_file() or is_link(path):
        raise TaskError("Sortie du moteur invalide.", "BAD_OUTPUT")
    return path


def _keep(path, folder, name):
    target = _unique(folder, name)
    shutil.move(str(path), str(target))
    return target


# --- Lecture / écriture de maillages sans dépendance (3MF ↔ STL) ---------------------------------------------------

def _mesh_tool():
    if __package__:
        from . import mesh_repair_tool
    else:
        import mesh_repair_tool
    return mesh_repair_tool


def write_stl(path, triangles):
    """STL binaire depuis des triangles ((x, y, z) ×3) ; rend le nombre de faces."""
    import struct
    count = 0
    with open(path, "wb") as output:
        output.write(b"Alpine Makers Desktop".ljust(80, b"\0"))
        output.write(struct.pack("<I", 0))
        for a, b, c in triangles:
            ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
            vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
            nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            output.write(struct.pack("<12fH", nx / length, ny / length, nz / length, *a, *b, *c, 0))
            count += 1
        output.seek(80)
        output.write(struct.pack("<I", count))
    return count


def _3mf_to_stl(source, target):
    tool = _mesh_tool()
    vertices, faces = tool._read_3mf(source)   # objets et composants du 3MF à plat, transformations appliquées
    if not faces:
        raise TaskError("Ce 3MF ne contient aucun maillage.", "INVALID_MESH")
    return write_stl(target, ((vertices[a], vertices[b], vertices[c]) for a, b, c in faces))


def _stl_to_3mf(source, target):
    tool = _mesh_tool()
    triangles = tool._read_stl(source.read_bytes())
    vertices, faces = tool._merge_vertices(triangles)
    if not faces:
        raise TaskError("Le maillage ne contient aucune face.", "INVALID_MESH")
    tool._write_3mf(target, vertices, faces)
    return len(faces)


def sliced_info(path):
    """Plateaux d'un .gcode.3mf tranché : index, durée estimée (s), poids (g) — lus dans Metadata/slice_info.config."""
    plates = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            gcode = sorted(int(m.group(1)) for m in (re.fullmatch(r"Metadata/plate_(\d+)\.gcode", n) for n in names) if m)
            info = {}
            if "Metadata/slice_info.config" in names and archive.getinfo("Metadata/slice_info.config").file_size < 4 * 1024 * 1024:
                root = ElementTree.fromstring(archive.read("Metadata/slice_info.config"))
                for plate in root.iter("plate"):
                    values = {item.get("key"): item.get("value") for item in plate.findall("metadata")}
                    try:
                        index = int(values.get("index") or 0)
                    except ValueError:
                        continue
                    info[index] = {"prediction_s": _float(values.get("prediction")), "weight_g": _float(values.get("weight"))}
                    filaments = [{key: item.get(key, "") for key in ("id", "type", "color", "used_g", "used_m")}
                                 for item in plate.findall("filament")][:32]
                    if filaments:
                        info[index]["filaments"] = filaments
            for index in gcode:
                plates.append({"index": index, **info.get(index, {"prediction_s": None, "weight_g": None})})
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError, KeyError):
        return []
    return plates[:64]


def _float(value):
    try:
        number = float(value)
        return round(number, 2) if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


# --- Actions --------------------------------------------------------------------------------------------------------

def _state(agent, params):
    """Moteurs de ce Worker pour Model Studio, en moins de 2 s (aucun démarrage, aucun tranchage)."""
    orca = _orca(agent)
    port = _port(agent)
    bridge = {"port": port, "running": False, "busy": False, "version": "", "bridge_version": "",
              "installed_bridge_version": _installed_bridge_version(agent), "outdated": False}
    if orca and _bridge_open(port):
        bridge["running"] = True
        try:
            info = _bridge_get(port, "/ping", 1.2, 64 * 1024)
            bridge["version"] = str(info.get("version") or "")[:60]
            bridge["bridge_version"] = str(info.get("bridge_version") or "")[:20]
            bridge["outdated"] = bool(bridge["installed_bridge_version"] and bridge["bridge_version"] != bridge["installed_bridge_version"])
        except (OSError, ValueError, urllib.error.URLError):
            bridge["busy"] = True   # port ouvert mais pas de réponse : un tranchage occupe le bridge (un à la fois)
    import importlib.util
    libraries = {name: importlib.util.find_spec(name) is not None for name in ("numpy", "trimesh", "pymeshlab")}
    jobs = 0
    try:
        jobs = sum(1 for item in agent.journal.records("jobs").values()
                   if item.get("tool_id") == "model-studio" and agent._job_active(item))
    except Exception:
        jobs = 0
    return {"orca_installed": bool(orca), "bridge": bridge, "freecad": bool(_freecad(agent)), "libraries": libraries,
            "maintenance": bool(getattr(agent, "maintenance_active", False)), "site_slices_active": jobs,
            "formats": {"direct": ["stl", "obj"], "freecad": list(CAD_FORMATS), "mesh": list(MESH_FORMATS),
                        "slice": list(SLICE_FORMATS)}}


def _bridge_start(agent, params):
    if not _orca(agent):
        raise ValueError("OrcaSlicer n'est pas installé sur ce Worker : installe Alpine Model Studio (Mes Workers → Installations).")
    if __package__:
        from . import local_control
    else:
        import local_control

    def work(task):
        task.update(None, "Démarrage du bridge OrcaSlicer…")
        try:
            result = local_control._start_one(agent, "model-studio")   # mêmes gardes que « engines-start »
        except Exception as error:
            raise TaskError(str(error)[:300] or "Démarrage refusé.", "START_REFUSED") from None
        return {"started": True, "already_running": bool((result or {}).get("already_running"))}
    return desk.start_task(agent, TOOL, "Démarrer le bridge OrcaSlicer", work)


def _require_bridge(agent):
    if not _orca(agent):
        raise TaskError("OrcaSlicer n'est pas installé sur ce Worker.", "ORCA_MISSING")
    port = _port(agent)
    if not _bridge_open(port):
        raise TaskError("Le bridge OrcaSlicer est arrêté : clique « Démarrer le moteur ».", "BRIDGE_STOPPED")
    return port


def _profiles(agent, params):
    def work(task):
        port = _require_bridge(agent)
        task.update(None, "Lecture des profils d'imprimante d'OrcaSlicer…")
        try:
            info = _bridge_get(port, "/ping", 15, 64 * 1024)
            installed = _installed_bridge_version(agent)
            if installed and str(info.get("bridge_version") or "") != installed:
                raise TaskError("Le bridge en marche est plus ancien que celui installé : arrête puis relance le moteur.", "BRIDGE_OUTDATED")
            data = _bridge_get(port, "/profiles", 300)
        except TaskError:
            raise
        except (OSError, ValueError, urllib.error.URLError) as error:
            raise TaskError(f"Bridge OrcaSlicer injoignable ({type(error).__name__}) : il est peut-être occupé par un tranchage.",
                            "BRIDGE_UNREACHABLE") from None
        if not data.get("ok"):
            raise TaskError(str(data.get("error") or "Le bridge n'a pas listé ses profils.")[:300], "BRIDGE_REFUSED")
        machines = []
        for entry in data.get("machines") or []:
            if isinstance(entry, list) and len(entry) == 4:
                vendor, name, model, nozzle = (str(value or "")[:120] for value in entry)
                if vendor and name:
                    machines.append([vendor, name, model, nozzle])
            if len(machines) >= PROFILE_LIMIT:
                break
        return _finish(agent, {"bridge_version": str(data.get("bridge_version") or "")[:20], "machines": machines})
    return desk.start_task(agent, TOOL, "Profils d'imprimante OrcaSlicer", work)


def process_schema(orca_dir):
    """Chaîne du profil de processus de référence (``inherits``), clés simples triées — même règle que le site."""
    candidates = [path for path in Path(orca_dir).glob("**/resources/profiles/BBL/process") if path.is_dir()][:4]
    base = next((path for path in candidates if (path / PROCESS_PROFILE).is_file()), None)
    if base is None:
        raise TaskError("Profil de processus de référence introuvable dans cet OrcaSlicer.", "PROFILE_MISSING")
    base = base.resolve()
    resolved, visited = {}, set()

    def load(name):
        name = str(name or "").strip()
        if not name:
            return
        if not name.lower().endswith(".json"):
            name += ".json"
        path = (base / name).resolve()
        if base not in path.parents or path in visited or not path.is_file():
            return
        visited.add(path)
        profile = json.loads(path.read_text(encoding="utf-8"))
        load(profile.get("inherits"))
        for key, value in profile.items():
            if key not in PROCESS_METADATA:
                resolved[key] = value

    load(PROCESS_PROFILE)
    return [{"key": key, "value": str(value[0] if isinstance(value, list) and len(value) == 1 else value)[:300],
             "boolean": key in BOOLEAN_PROCESS_KEYS}
            for key, value in sorted(resolved.items())
            if value is not None and not isinstance(value, dict) and not (isinstance(value, list) and len(value) != 1)]


def _process_schema(agent, params):
    def work(task):
        orca = _orca(agent)
        if not orca:
            raise TaskError("OrcaSlicer n'est pas installé sur ce Worker.", "ORCA_MISSING")
        fields = process_schema(orca.parent)
        return _finish(agent, {"profile": "0,20 mm Standard (profil Bambu de référence)", "fields": fields})
    return desk.start_task(agent, TOOL, "Réglages de processus OrcaSlicer", work)


def _import(agent, params):
    name, staged = params["name"], params["staged"]
    extension = Path(name).suffix.lower().lstrip(".")
    if extension not in CAD_FORMATS + MESH_FORMATS:
        raise ValueError("Format non pris en charge à l'import : " + (extension or "?") + " (STL et OBJ s'ouvrent directement).")
    source = _consume(agent, staged, name)
    if extension in CAD_FORMATS and not _freecad(agent):
        _drop_inbox(source)
        raise ValueError("FreeCAD n'est pas installé sur ce Worker : installe le Convertisseur (Mes Workers → Installations).")

    def work(task):
        _maintenance_guard(agent)
        folder = cache_folder(agent, "imports")
        try:
            if extension in MESH_FORMATS:
                task.update(None, "Lecture du 3MF…")
                target = _unique(folder, _stem(name) + ".stl")
                faces = _3mf_to_stl(source, target)
                return {"path": str(target), "name": target.name, "bytes": target.stat().st_size, "faces": faces,
                        "engine": "python", "source_format": extension}
            parameters = {"output_format": "stl", "output_name": _stem(name) + ".stl", "linear_deflection": 0.05}
            workdir, state = _run_runner(agent, task, "freecad", parameters, [source], {"FREECAD_CMD": _freecad(agent)},
                                         "Conversion FreeCAD")
            try:
                target = _keep(_artifact(workdir, state), folder, _stem(name) + ".stl")
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
            return {"path": str(target), "name": target.name, "bytes": target.stat().st_size, "engine": "freecad",
                    "source_format": extension}
        except (TaskError, TaskCancelled):
            raise
        except Exception as error:
            raise TaskError(f"Lecture du fichier impossible ({type(error).__name__}) : {str(error)[:200]}", "IMPORT_FAILED") from None
        finally:
            _drop_inbox(source)
    return desk.start_task(agent, TOOL, "Ouvrir " + name, work)


def _repair(agent, params):
    name, staged = params["name"], params["staged"]
    if Path(name).suffix.lower() not in {".stl", ".obj", ".3mf"}:
        raise ValueError("Réparation : STL, OBJ ou 3MF attendu.")
    source = _consume(agent, staged, name)
    parameters = {"task": "mesh_repair", "analyze_only": bool(params.get("analyze_only")), "output_format": "stl",
                  "output_name": _stem(name) + "_repare.stl", "fill_holes": params.get("fill_holes", True),
                  "fix_normals": params.get("fix_normals", True), "remove_degenerate": params.get("remove_degenerate", True)}
    for key in ("target_faces", "decimate_ratio"):
        if key in params:
            parameters[key] = params[key]

    def work(task):
        _maintenance_guard(agent)
        try:
            workdir, state = _run_runner(agent, task, "mesh_repair", parameters, [source], {}, "Analyse du maillage"
                                         if parameters["analyze_only"] else "Réparation du maillage")
        finally:
            _drop_inbox(source)
        try:
            result = dict(state.get("result") or {})
            answer = {"report": result.get("report") or {}, "engine": str(result.get("engine") or "")[:40],
                      "analyze_only": parameters["analyze_only"]}
            if not parameters["analyze_only"]:
                target = _keep(_artifact(workdir, state), cache_folder(agent, "repairs"), parameters["output_name"])
                answer.update(path=str(target), name=target.name, bytes=target.stat().st_size)
            return _finish(agent, answer)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    return desk.start_task(agent, TOOL, ("Analyser " if parameters["analyze_only"] else "Réparer ") + name, work, journal=True)


def _build(agent, params):
    clean = validate_freecad_scene({"scene": params["scene"]})   # même validation que le site et le moteur
    name = _stem(params.get("name") or "piece") + ".stl"
    if not _freecad(agent):
        raise ValueError("FreeCAD n'est pas installé sur ce Worker : installe le Convertisseur (Mes Workers → Installations).")

    def work(task):
        _maintenance_guard(agent)
        parameters = {"scene": clean["scene"], "output_format": "stl", "output_name": name}
        workdir, state = _run_runner(agent, task, "freecad", parameters, [], {"FREECAD_CMD": _freecad(agent)},
                                     "Construction FreeCAD")
        try:
            target = _keep(_artifact(workdir, state), cache_folder(agent, "builds"), name)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return {"path": str(target), "name": target.name, "bytes": target.stat().st_size, "engine": "freecad"}
    return desk.start_task(agent, TOOL, "Pièce paramétrique " + name, work, journal=True)


def _slice(agent, params):
    files = params["files"]
    settings = dict(params.get("settings") or {})
    process_settings = dict(params.get("process_settings") or {})
    sources = []
    try:   # tout vérifié avant d'ouvrir la tâche ; un refus efface les dépôts déjà lus
        for item in files:
            sources.append(_consume(agent, item["staged"], item["name"]))
    except Exception:
        for source in sources:
            _drop_inbox(source)
        raise
    multi = len(files) > 1
    parameters = dict(settings)
    if multi:
        parameters.update(multi=True, materials=[item["material"] for item in files])
    else:
        parameters["material"] = files[0]["material"]
    if process_settings:
        parameters["process_settings"] = process_settings
    output_name = _stem(files[0]["name"]) + ("_multi" if multi else "") + ".gcode.3mf"

    def work(task):
        try:
            _maintenance_guard(agent)
            port = _require_bridge(agent)
            workdir, state = _run_runner(agent, task, "orca", parameters, sources, {"WORKER_ORCA_PORT": str(port)},
                                         "Tranchage OrcaSlicer")
        finally:
            for source in sources:
                _drop_inbox(source)
        execution = workdir.name
        try:
            target = _keep(_artifact(workdir, state), cache_folder(agent, "sliced"), output_name)
            result = dict(state.get("result") or {})
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            _clean_bridge_workspace(agent, execution)
        return {"path": str(target), "name": target.name, "bytes": target.stat().st_size,
                "printer_profile": str(result.get("printer_profile") or "")[:120],
                "filament_profiles": [str(item)[:120] for item in (result.get("filament_profiles") or [])][:16],
                "bed_type": str(result.get("bed_type") or "")[:40], "fallback_used": bool(result.get("fallback_used")),
                "profile_mode": str(result.get("profile_mode") or "")[:20],
                "bridge_version": str(result.get("bridge_version") or "")[:20], "plates": sliced_info(target)}
    return desk.start_task(agent, TOOL, "Trancher " + files[0]["name"] + (f" (+{len(files) - 1})" if multi else ""), work,
                           journal=True)


def _clean_bridge_workspace(agent, execution):
    """Copies de CE job dans le dossier du bridge (entrées et sortie) : effacées, rien d'autre."""
    workspace = Path(agent.root) / "workspace"
    if not re.fullmatch(r"desktop-[0-9a-f]{24}", execution) or not workspace.is_dir():
        return
    for path in workspace.glob(f"worker_{execution}_*"):
        try:
            if path.is_file() and not is_link(path):
                path.unlink()
        except OSError:
            pass
    shutil.rmtree(workspace / "_worker_jobs" / execution, ignore_errors=True)


def _export_3mf(agent, params):
    name, staged = params["name"], params["staged"]
    if Path(name).suffix.lower() != ".stl":
        raise ValueError("Export 3MF : un STL est attendu.")
    source = _consume(agent, staged, name)

    def work(task):
        try:
            target = _unique(cache_folder(agent, "exports"), _stem(name) + ".3mf")
            faces = _stl_to_3mf(source, target)
        except (TaskError, TaskCancelled):
            raise
        except Exception as error:
            raise TaskError(f"Export 3MF impossible ({type(error).__name__}).", "EXPORT_FAILED") from None
        finally:
            _drop_inbox(source)
        return {"path": str(target), "name": target.name, "bytes": target.stat().st_size, "faces": faces}
    return desk.start_task(agent, TOOL, "Exporter " + _stem(name) + ".3mf", work, journal=True)


def _outputs(agent, params):
    folder = cache_folder(agent, "sliced")
    items = []
    for path in sorted(folder.glob("*.gcode.3mf"), key=lambda item: -item.stat().st_mtime)[:100]:
        if path.is_file() and not is_link(path):
            stat = path.stat()
            items.append({"name": path.name, "path": str(path), "bytes": stat.st_size, "modified": stat.st_mtime})
    return {"folder": str(folder), "items": items}


# --- Impression : par la route du site uniquement -------------------------------------------------------------------

PRINT_OPTIONS = ("bed_levelling", "timelapse", "flow_cali", "vibration_cali", "layer_inspect", "use_ams")


def _site_call(agent, path, body):
    if __package__:
        from .desktop_library import _site_error
    else:
        from desktop_library import _site_error
    try:
        answer = agent.request(path, "POST", body)
    except Exception as error:
        raise _site_error(error) from None
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        raise TaskError(str((answer or {}).get("error") or "Réponse du site illisible.")[:300], str((answer or {}).get("code") or ""))
    return answer


def _print(agent, params):
    session = params.pop("session_token")
    if not params.get("name", "").lower().endswith(".gcode.3mf"):
        raise ValueError("Seul un fichier tranché (.gcode.3mf) de la bibliothèque peut être imprimé.")
    options = {key: params[key] for key in PRINT_OPTIONS if key in params}
    if "plate" in params:
        options["plate"] = params["plate"]
    if "ams_mapping" in params:
        options["ams_mapping"] = params["ams_mapping"]
    body = {"name": params["name"], "options": options, "confirm": True, "session_token": session}
    if "equipment" in params:
        body["equipment"] = params["equipment"]

    def work(task):
        task.update(None, "Demande d'impression envoyée au site…")
        answer = _site_call(agent, PRINT_PATH, body)
        return {"launch": answer.get("launch") or {}, "message": str(answer.get("message") or "")[:300]}
    return desk.start_task(agent, TOOL, "Imprimer " + params["name"], work)


def _print_status(agent, params):
    session = params.pop("session_token")

    def work(task):
        answer = _site_call(agent, PRINT_STATUS_PATH, {"launch_id": params["launch_id"], "session_token": session})
        return {"launch": answer.get("launch") or {}}
    return desk.start_task(agent, TOOL, "Suivi de l'impression", work)


DESKTOP_ACTIONS = {
    "state": {"handler": _state, "read": True, "params": {}},
    "bridge-start": {"handler": _bridge_start, "read": False, "params": {}},
    "profiles": {"handler": _profiles, "read": True, "params": {}},
    "process-schema": {"handler": _process_schema, "read": True, "params": {}},
    "import": {"handler": _import, "read": True, "params": {"staged": v_hex(32), "name": v_file_name}},
    "repair": {"handler": _repair, "read": True, "params": {
        "staged": v_hex(32), "name": v_file_name, "analyze_only": v_bool, "target_faces": v_int(4, 50_000_000),
        "decimate_ratio": v_num(0.001, 0.999), "fill_holes": v_bool, "fix_normals": v_bool, "remove_degenerate": v_bool}},
    "build": {"handler": _build, "read": True, "params": {"scene": v_object(depth=6, keys=80), "name": v_text(120)}},
    "slice": {"handler": _slice, "read": True, "params": {
        "files": v_slice_files, "settings": v_settings, "process_settings": v_process_settings}},
    "export-3mf": {"handler": _export_3mf, "read": True, "params": {"staged": v_hex(32), "name": v_file_name}},
    "outputs": {"handler": _outputs, "read": True, "params": {}},
    "print": {"handler": _print, "read": False, "params": {
        "session_token": v_session, "name": v_file_name, "plate": v_int(1, 64), "confirm": v_true,
        "equipment": v_regex(re.compile(r"[A-Za-z0-9_-]{1,80}"), "imprimante"), "ams_mapping": v_ams_mapping,
        **{key: v_bool for key in PRINT_OPTIONS}}},
    "print-status": {"handler": _print_status, "read": True, "params": {"session_token": v_session, "launch_id": v_hex(32)}},
}
REQUIRED = {"import": ("staged", "name"), "repair": ("staged", "name"), "build": ("scene",), "slice": ("files",),
            "export-3mf": ("staged", "name"), "print": ("session_token", "name", "confirm"),
            "print-status": ("session_token", "launch_id")}


def _required(name, handler):
    def run(agent, params):
        missing = [field for field in REQUIRED.get(name, ()) if field not in params]
        if missing:
            raise ValueError("Paramètre manquant : " + missing[0] + (" (connecte-toi dans worker.exe)" if missing[0] == "session_token" else ""))
        return handler(agent, dict(params))
    return run


for _name, _spec in DESKTOP_ACTIONS.items():
    _spec["handler"] = _required(_name, _spec["handler"])
del _name, _spec
