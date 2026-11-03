"""Chaîne complète du trancheur Alpine Model Engine (docs/MODEL_ENGINE_ARCHITECTURE.md §2 et §8).

``slice_job(parameters, inputs, workdir, execution_id=…, progress=…)`` enchaîne : réglages (profiles.process), fiches
filament (profiles.filament), machine (machine.bambu), lecture des maillages (mesh), scène (scene : pose, arrangement,
orientation, validation), découpe (slicing), murs (perimeters), surfaces (surfaces), remplissage (infill), supports
(support), adhérence (adhesion), planification (toolpath), G-code (gcode) et fichier tranché (threemf_out +
thumbnails). Résultat : le dict du §8 (artifacts, output, bytes, exit_code, profile_mode, fallback_used,
engine_version, bridge_version, bed_type, printer_profile, filament_profiles, plates, warnings).

Erreurs : ``PipelineError(code, message)`` avec un code stable (MESH_INVALID, OUT_OF_BED, PRINTER_UNSUPPORTED,
MATERIAL_UNKNOWN, SETTINGS_INVALID, EMPTY_SLICE) ; la CLI les traduit en ``RAPHAL_SLICER_ERROR``.

Parallélisme : au-delà de ``PARALLEL_MIN_LAYERS`` couches, les étapes par couche (murs, remplissage, planification)
tournent par blocs de couches dans un ``ProcessPoolExecutor`` (processus = ``settings["threads"]``, 0 = min(cœurs − 1,
8), 1 = séquentiel). Les blocs ont une taille fixe pour un même nombre de couches : le résultat ne dépend pas du
nombre de processus. Au début d'un bloc, la mémoire des coutures et la position courante repartent de zéro (couture
« aligned » : coin arrière, comme sans mémoire) ; ``threads = 1`` donne la sortie de référence. Toute panne du pool
est rattrapée par un calcul séquentiel, signalée dans ``warnings``.

Code Alpine Makers : numpy + Pillow + bibliothèque standard. Unités : µm int64 dans la pile, mm aux frontières.
"""
from __future__ import annotations

import concurrent.futures
import math
import multiprocessing
import os
import re
import time
from pathlib import Path

import numpy as np

from . import VERSION
from . import adhesion, gcode, infill, perimeters, slicing, support, surfaces, threemf_out, toolpath
from .geometry import SCALE, poly
from .layers import Layer
from .machine import PrintContext, PrinterUnsupportedError, bambu
from .mesh import Mesh, MeshError, read_any
from .profiles import filament as filament_profiles
from .profiles import process
from .scene import Bed, Scene, SceneError, arrange, auto_orient, place_default, validate

ENGINE_VERSION = VERSION
PROFILE_MODE = "alpine"
ERROR_CODES = ("MESH_INVALID", "OUT_OF_BED", "PRINTER_UNSUPPORTED", "MATERIAL_UNKNOWN", "SETTINGS_INVALID", "EMPTY_SLICE",
               "TIMEOUT", "ENGINE_FAILED")
PARALLEL_MIN_LAYERS = 40          # en dessous, tout est séquentiel
PARALLEL_MAX_PROCESSES = 8
PROGRESS_PERIOD_S = 2.0           # appel de progress au moins toutes les 2 s dans les boucles de couches
MAX_INPUT_FILES = 16
INPUT_SUFFIXES = (".stl", ".obj", ".3mf")
# Clés de la demande qui ne sont pas des réglages (jamais transmises à profiles.process).
_REQUEST_KEYS = frozenset({"input_file", "input_files", "files", "file", "multi", "output_name", "worker_execution_id",
                           "process_settings", "materials", "action", "tool", "execution_id", "job_id", "output",
                           "plate_name", "print_settings_id"})
_EXECUTION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
# Étapes et part de la progression (fractions cumulées).
_STAGES = (("réglages", 0.02), ("lecture des modèles", 0.06), ("mise en place", 0.08), ("découpe", 0.20), ("murs", 0.40),
           ("surfaces", 0.48), ("remplissage", 0.64), ("supports", 0.70), ("adhérence", 0.72), ("planification", 0.84),
           ("G-code", 0.94), ("fichier tranché", 1.0))

__all__ = ["ENGINE_VERSION", "ERROR_CODES", "PROFILE_MODE", "PARALLEL_MIN_LAYERS", "PipelineError", "slice_job",
           "resolve_settings", "load_inputs", "build_scene", "process_count", "output_name"]


class PipelineError(Exception):
    """Erreur du tranchage avec code stable (§8)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code if code in ERROR_CODES else "ENGINE_FAILED"
        self.message = str(message)


# ---------------------------------------------------------------------------------------------- progression

class _Progress:
    """Progression bornée par étape : ``stage(i)`` fixe la fenêtre, ``tick(fraction, message)`` interpole dedans."""

    def __init__(self, callback):
        self.callback = callback
        self.lo, self.hi = 0.0, 0.0
        self.message = ""
        self.last_time = 0.0
        self.last_fraction = -1.0

    def stage(self, index: int) -> None:
        self.lo = _STAGES[index - 1][1] if index > 0 else 0.0
        self.hi = _STAGES[index][1]
        self.message = _STAGES[index][0]
        self.emit(0.0, self.message, force=True)

    def emit(self, fraction: float, message: str | None = None, force: bool = False) -> None:
        if self.callback is None:
            return
        value = self.lo + (self.hi - self.lo) * max(0.0, min(1.0, float(fraction)))
        now = time.monotonic()
        if not force and now - self.last_time < 0.25 and value - self.last_fraction < 0.01:
            return
        self.last_time, self.last_fraction = now, value
        try:
            self.callback(round(value, 4), str(message or self.message)[:200])
        except Exception:   # la progression n'interrompt jamais le calcul
            pass

    def due(self) -> bool:
        return self.callback is not None and time.monotonic() - self.last_time >= PROGRESS_PERIOD_S


# ---------------------------------------------------------------------------------------------- réglages

def output_name(execution_id: str) -> str:
    return f"worker_{execution_id}_sliced.gcode.3mf"


def _check_execution_id(execution_id) -> str:
    text = str(execution_id or "")
    if not _EXECUTION_ID_RE.fullmatch(text):
        raise PipelineError("SETTINGS_INVALID", "Identifiant d'exécution invalide (lettres, chiffres, « _ » et « - »).")
    return text


def _materials(parameters: dict, count: int) -> list[str]:
    """Matériau de chaque fichier d'entrée : ``materials`` (liste), sinon ``material`` pour tous."""
    raw = parameters.get("materials")
    items = [str(m).strip() for m in raw if str(m or "").strip()] if isinstance(raw, (list, tuple)) else []
    single = str(parameters.get("material") or parameters.get("filament_type") or "").strip()
    if isinstance(parameters.get("filament_type"), (list, tuple)) and parameters["filament_type"]:
        single = str(parameters["filament_type"][0]).strip()
    if not items:
        items = [single or "PLA"]
    result = []
    for index in range(count):
        result.append(items[index] if index < len(items) else items[-1])
    return result


def resolve_settings(parameters: dict, *, material: str | None = None) -> tuple[dict, list[str]]:
    """Réglages résolus (dict plat) + avertissements à partir de la demande du client (25 clés, process_settings,
    nozzle, material, bed_type, printer_profile). Les erreurs portent un code stable."""
    parameters = dict(parameters or {})
    client = {key: value for key, value in parameters.items() if key not in _REQUEST_KEYS}
    advanced = parameters.get("process_settings")
    advanced = dict(advanced) if isinstance(advanced, dict) else {}
    material = material or _materials(parameters, 1)[0]
    try:
        resolved, warnings = process.resolve_with_report(client, advanced, nozzle=parameters.get("nozzle"), material=material,
                                                         bed_type=parameters.get("bed_type"))
    except process.SettingsError as error:
        key = str(getattr(error, "key", "") or "")
        code = "MATERIAL_UNKNOWN" if key == "material" else "PRINTER_UNSUPPORTED" if key == "printer_profile" else "SETTINGS_INVALID"
        raise PipelineError(code, str(error)) from None
    except filament_profiles.MaterialUnknownError as error:
        raise PipelineError("MATERIAL_UNKNOWN", str(error)) from None
    return resolved, list(warnings)


def _machine_for(resolved: dict):
    try:
        machine = bambu.machine_for_preset(resolved.get("printer_profile"))
        machine.ensure_sliceable()
    except PrinterUnsupportedError as error:
        raise PipelineError("PRINTER_UNSUPPORTED", str(error)) from None
    try:
        machine.validate_layer_height(float(resolved["layer_height"]))
    except (ValueError, KeyError, TypeError) as error:
        raise PipelineError("SETTINGS_INVALID", str(error)) from None
    return machine


def _fiche_for_gcode(fiche: dict) -> dict:
    """Fiche filament au format lu par gcode.Emitter (FILAMENT_KEYS) et toolpath, en gardant les clés Alpine."""
    merged = dict(fiche)
    merged.update({
        "filament_type": fiche["type"], "filament_colour": fiche["colour"], "filament_settings_id": fiche["settings_id"],
        "filament_vendor": fiche["vendor"], "filament_diameter": fiche["filament_diameter"],
        "filament_flow_ratio": fiche["flow_ratio"], "filament_density": fiche["filament_density"],
        "filament_cost": fiche["filament_cost"], "filament_max_volumetric_speed": fiche["filament_max_volumetric_speed"],
        "tray_info_idx": fiche.get("tray_info_idx") or "", "nozzle_temperature": fiche["nozzle_temperature"],
        "nozzle_temperature_initial_layer": fiche["nozzle_temperature_initial_layer"],
    })
    for key in ("retraction_length", "retraction_speed", "deretraction_speed", "z_hop", "wipe"):
        value = fiche.get(f"filament_{key}")
        if value is not None:
            merged[key] = value
    return merged


def _fiches(materials: list[str]) -> list[dict]:
    fiches = []
    for material in materials:
        try:
            fiches.append(filament_profiles.resolve_filament(material))
        except filament_profiles.MaterialUnknownError as error:
            raise PipelineError("MATERIAL_UNKNOWN", str(error)) from None
    return fiches


# ---------------------------------------------------------------------------------------------- entrées et scène

def load_inputs(inputs: list, *, multi: bool = False) -> list[Mesh]:
    """Lit STL / OBJ / 3MF ; en multi, chaque fichier reçoit l'extrudeur ``index + 1``. Les objets d'un 3MF gardent leur
    groupement (``object_id`` renuméroté par fichier pour éviter les collisions)."""
    paths = [Path(p) for p in (inputs or [])]
    if not paths:
        raise PipelineError("MESH_INVALID", "Aucun modèle transmis au trancheur.")
    if len(paths) > MAX_INPUT_FILES:
        raise PipelineError("MESH_INVALID", f"Trop de fichiers d'entrée ({len(paths)} > {MAX_INPUT_FILES}).")
    meshes: list[Mesh] = []
    next_group = 1
    for index, path in enumerate(paths):
        if not path.is_file():
            raise PipelineError("MESH_INVALID", f"Fichier d'entrée absent : {path.name}")
        if path.suffix.lower() not in INPUT_SUFFIXES:
            raise PipelineError("MESH_INVALID", f"Format non pris en charge : {path.suffix or 'sans extension'} (STL, OBJ ou 3MF).")
        try:
            parts = read_any(path)
        except MeshError as error:
            raise PipelineError("MESH_INVALID", f"{path.name} : {error}") from None
        except (OSError, ValueError) as error:
            raise PipelineError("MESH_INVALID", f"{path.name} : lecture impossible ({error}).") from None
        groups: dict[int, int] = {}
        kept = 0
        for mesh in parts:
            mesh.remove_degenerate()
            if len(mesh.faces) == 0:
                continue
            if not mesh.name:
                mesh.name = path.stem
            if multi or len(paths) > 1:
                mesh.extruder = min(16, index + 1)
            if int(mesh.object_id) > 0:
                if mesh.object_id not in groups:
                    groups[mesh.object_id] = next_group
                    next_group += 1
                mesh.object_id = groups[mesh.object_id]
            meshes.append(mesh)
            kept += 1
        if kept == 0:
            raise PipelineError("MESH_INVALID", f"{path.name} : aucun triangle exploitable.")
    return meshes


def build_scene(meshes: list[Mesh], machine, resolved: dict, warnings: list[str]) -> Scene:
    """Pose les objets sur le plateau de la machine (orientation et arrangement selon les réglages) et valide."""
    bed = machine.bed
    scene = Scene(Bed(bed.width_mm, bed.depth_mm, bed.height_mm, exclude_mm=bed.exclude_mm), meshes)
    try:
        if _bool(resolved.get("orient")):
            for group in scene.groups():
                if len(group.meshes) == 1 and group.meshes[0].volume_type == "normal":
                    auto_orient(group.meshes[0])
        place_default(scene)
        if _bool(resolved.get("arrange")):
            arrange(scene)
    except SceneError as error:
        raise PipelineError("OUT_OF_BED", str(error)) from None
    errors = validate(scene)
    if errors:
        raise PipelineError("OUT_OF_BED", " ".join(errors))
    return scene


def _bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "oui", "vrai")
    return bool(value)


# ---------------------------------------------------------------------------------------------- parallélisme

def process_count(settings: dict) -> int:
    """Nombre de processus de calcul : ``threads`` (> 0), sinon min(cœurs − 1, 8) ; 1 = séquentiel."""
    try:
        threads = int(float(settings.get("threads") or 0))
    except (TypeError, ValueError):
        threads = 0
    if threads > 0:
        return min(threads, 64)
    return max(1, min(PARALLEL_MAX_PROCESSES, (os.cpu_count() or 2) - 1))


def _block_size(n_layers: int) -> int:
    """Taille des blocs de couches (ne dépend que du nombre de couches : résultat indépendant des processus)."""
    return max(10, min(50, n_layers // 8))


def _blocks(n_layers: int) -> list[tuple[int, int]]:
    size = _block_size(n_layers)
    return [(start, min(start + size, n_layers)) for start in range(0, n_layers, size)]


def _light_layer(layer: Layer, *, regions=True, surfaces_=False, walls=False, planning=False) -> Layer:
    """Copie allégée d'une couche pour le transfert vers un processus (seuls les champs utiles à l'étape)."""
    copy = Layer(index=layer.index, z_um=layer.z_um, height_um=layer.height_um)
    if regions:
        copy.regions = layer.regions
        copy.all_regions = layer.all_regions
    if surfaces_:
        copy.surfaces = layer.surfaces
    if walls:
        copy.walls = layer.walls
    if planning:
        copy.walls = layer.walls
        copy.infill_lines = layer.infill_lines
        copy.support = layer.support
        copy.adhesion = layer.adhesion
    return copy


def _walls_block(block: list, previous, settings: dict) -> list:
    """Murs d'un bloc de couches (séquentiel dans le bloc) ; renvoie ``[walls par couche]``."""
    prev = previous
    out = []
    for layer in block:
        perimeters.build_layer_walls(layer, settings, prev)
        out.append(layer.walls)
        prev = layer
    return out


def _infill_block(block: list, settings: dict) -> list:
    out = []
    for layer in block:
        infill.fill_layer(layer, settings)
        out.append(layer.infill_lines)
    return out


def _plan_block(block: list, settings: dict, fiches: list, machine) -> list:
    state = toolpath.PlannerState()
    out = []
    for layer in block:
        fiche = fiches[state.extruder - 1] if 0 < state.extruder <= len(fiches) else (fiches[0] if fiches else None)
        out.append(toolpath.plan_layer(layer, settings, state, fiche, machine))
    return out


def _run_blocks(executor, function, jobs: list, progress: _Progress, label: str) -> list | None:
    """Soumet ``jobs`` (listes d'arguments) et rend les résultats dans l'ordre ; None si le pool a échoué."""
    futures = [executor.submit(function, *args) for args in jobs]
    results: list = [None] * len(futures)
    pending = set(range(len(futures)))
    try:
        while pending:
            done, _ = concurrent.futures.wait([futures[i] for i in pending], timeout=PROGRESS_PERIOD_S / 2,
                                              return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                index = futures.index(future)
                results[index] = future.result()
                pending.discard(index)
            finished = len(futures) - len(pending)
            progress.emit(finished / max(1, len(futures)), f"{label} : bloc {finished}/{len(futures)}")
    except (concurrent.futures.process.BrokenProcessPool, concurrent.futures.CancelledError, OSError, EOFError,
            ValueError, TypeError, AttributeError, RuntimeError):
        for future in futures:
            future.cancel()
        return None
    return results


# ---------------------------------------------------------------------------------------------- étapes

def _stage_walls(layers: list, settings: dict, executor, progress: _Progress, warnings: list) -> None:
    n = len(layers)
    if executor is not None:
        jobs = []
        for start, end in _blocks(n):
            block = [_light_layer(layer) for layer in layers[start:end]]
            previous = _light_layer(layers[start - 1]) if start > 0 else None
            jobs.append((block, previous, settings))
        results = _run_blocks(executor, _walls_block, jobs, progress, "murs")
        if results is not None:
            for (start, end), walls in zip(_blocks(n), results):
                for layer, w in zip(layers[start:end], walls):
                    layer.walls = w
            return
        warnings.append("Calcul parallèle des murs indisponible : calcul séquentiel.")
    previous = None
    for i, layer in enumerate(layers):
        perimeters.build_layer_walls(layer, settings, previous)
        previous = layer
        if progress.due() or i + 1 == n:
            progress.emit((i + 1) / n, f"murs : couche {i + 1}/{n}")


def _stage_infill(layers: list, settings: dict, executor, progress: _Progress, warnings: list) -> None:
    n = len(layers)
    if executor is not None:
        jobs = [([_light_layer(layer, regions=False, surfaces_=True) for layer in layers[start:end]], settings)
                for start, end in _blocks(n)]
        results = _run_blocks(executor, _infill_block, jobs, progress, "remplissage")
        if results is not None:
            for (start, end), lines in zip(_blocks(n), results):
                for layer, l in zip(layers[start:end], lines):
                    layer.infill_lines = l
            return
        warnings.append("Calcul parallèle du remplissage indisponible : calcul séquentiel.")
    for i, layer in enumerate(layers):
        infill.fill_layer(layer, settings)
        if progress.due() or i + 1 == n:
            progress.emit((i + 1) / n, f"remplissage : couche {i + 1}/{n}")


def _stage_plan(layers: list, settings: dict, fiches: list, machine, executor, progress: _Progress, warnings: list) -> list:
    n = len(layers)
    if executor is not None:
        jobs = [([_light_layer(layer, regions=False, planning=True) for layer in layers[start:end]], settings, fiches, machine)
                for start, end in _blocks(n)]
        results = _run_blocks(executor, _plan_block, jobs, progress, "planification")
        if results is not None:
            return [moves for block in results for moves in block]
        warnings.append("Calcul parallèle de la planification indisponible : calcul séquentiel.")
    state = toolpath.PlannerState()
    out = []
    for i, layer in enumerate(layers):
        fiche = fiches[state.extruder - 1] if 0 < state.extruder <= len(fiches) else (fiches[0] if fiches else None)
        out.append(toolpath.plan_layer(layer, settings, state, fiche, machine))
        if progress.due() or i + 1 == n:
            progress.emit((i + 1) / n, f"planification : couche {i + 1}/{n}")
    return out


def _volumes(layers: list, volume_type: str) -> dict:
    """Zones par couche (``{index: Paths}``) d'un type de volume (bloqueur / renfort de support), tous objets."""
    out = {}
    for layer in layers:
        paths = []
        for oid in slicing.model_object_ids(layer):
            paths.extend(slicing.volume_regions(layer, oid, volume_type))
        if paths:
            out[layer.index] = paths
    return out


def _assign_extruders(moves: list, extruder_by_object: dict) -> None:
    """Multi-matériau V1 : chaque parcours d'objet prend l'extrudeur de son objet (changement géré par l'émetteur)."""
    if not extruder_by_object or set(extruder_by_object.values()) == {1}:
        return
    for layer in moves:
        current = 1
        for move in layer.moves:
            if move.object_id is not None:
                current = int(extruder_by_object.get(int(move.object_id), 1))
            move.extruder = current


def _objects_for_file(scene: Scene) -> tuple[list[dict], dict, dict]:
    """Objets du fichier tranché (un par groupe de la scène : parts « normal » réunies), noms et extrudeurs."""
    objects, names, extruders = [], {}, {}
    for oid, group in enumerate(scene.groups(), 1):
        parts = [m for m in group.meshes if m.volume_type == "normal" and len(m.faces)] or [m for m in group.meshes if len(m.faces)]
        vertices, faces, offset = [], [], 0
        for part in parts:
            vertices.append(part.vertices)
            faces.append(part.faces + offset)
            offset += len(part.vertices)
        v = np.concatenate(vertices, axis=0) if vertices else np.zeros((0, 3))
        f = np.concatenate(faces, axis=0) if faces else np.zeros((0, 3), dtype=np.int64)
        extruder = int(parts[0].extruder) if parts else 1
        name = group.name or f"objet {oid}"
        objects.append({"id": oid, "name": name, "vertices": v, "faces": f, "extruder": extruder,
                        "source_file": str(parts[0].name if parts else "")[:200]})
        names[oid] = name
        extruders[oid] = extruder
    return objects, names, extruders


def _section_area_mm2(layer: Layer) -> float:
    return sum(poly.area_mm2(ring) for ring in (layer.all_regions or []))


# ---------------------------------------------------------------------------------------------- chaîne complète

def slice_job(parameters: dict, inputs: list, workdir, *, execution_id: str, progress=None, debug: dict | None = None) -> dict:
    """Tranche ``inputs`` (STL / OBJ / 3MF) selon ``parameters`` et écrit ``worker_<execution_id>_sliced.gcode.3mf``
    dans ``workdir``. ``progress(fraction, message)`` est appelé à chaque étape et au moins toutes les 2 s.
    ``debug`` (dict, tests) reçoit ``layers``, ``moves``, ``settings``, ``machine`` et ``gcode``."""
    started = time.perf_counter()
    execution_id = _check_execution_id(execution_id)
    workdir = Path(workdir)
    parameters = dict(parameters or {})
    prog = _Progress(progress)
    warnings: list[str] = []

    # 1. Réglages, machine, filaments.
    prog.stage(0)
    multi = _bool(parameters.get("multi")) or len(inputs or []) > 1
    materials = _materials(parameters, max(1, len(inputs or [])))
    resolved, warns = resolve_settings(parameters, material=materials[0])
    warnings.extend(warns)
    machine = _machine_for(resolved)
    fiches = _fiches(materials if multi else materials[:1])
    fiches_gcode = [_fiche_for_gcode(f) for f in fiches]
    processes = process_count(resolved)

    # 2. Maillages et scène.
    prog.stage(1)
    meshes = load_inputs(inputs, multi=multi)
    prog.stage(2)
    scene = build_scene(meshes, machine, resolved, warnings)
    objects, object_names, extruder_by_object = _objects_for_file(scene)

    # 3. Découpe.
    prog.stage(3)
    layers = slicing.slice_scene(scene, resolved, progress=lambda f, m: prog.emit(f, m))
    if not layers or not any(layer.all_regions for layer in layers):
        raise PipelineError("EMPTY_SLICE", "Aucune couche à imprimer : modèle vide ou plus fin qu'une couche.")
    for layer in layers:
        warnings.extend(f"couche {layer.index} : {w}" for w in layer.warnings)
    seen_warnings = [len(layer.warnings) for layer in layers]

    executor = None
    pool_context = None
    if processes > 1 and len(layers) > PARALLEL_MIN_LAYERS:
        try:
            pool_context = multiprocessing.get_context("spawn")
            executor = concurrent.futures.ProcessPoolExecutor(max_workers=min(processes, len(_blocks(len(layers)))),
                                                              mp_context=pool_context)
        except (OSError, ValueError, RuntimeError) as error:
            warnings.append(f"Calcul parallèle indisponible ({error}) : calcul séquentiel.")
            executor = None
    try:
        # 4. Murs, surfaces, remplissage.
        prog.stage(4)
        _stage_walls(layers, resolved, executor, prog, warnings)
        prog.stage(5)
        surfaces.classify(layers, resolved)
        prog.stage(6)
        _stage_infill(layers, resolved, executor, prog, warnings)
        # 5. Supports et adhérence.
        prog.stage(7)
        support.build_supports(layers, resolved, blockers=_volumes(layers, "support_blocker") or None,
                               enforcers=_volumes(layers, "support_enforcer") or None)
        prog.stage(8)
        bed = machine.bed
        bed_bbox_um = (0, 0, int(round(bed.width_mm * SCALE)), int(round(bed.depth_mm * SCALE)))
        exclude_um = tuple(int(round(float(v) * SCALE)) for v in bed.exclude_mm) if bed.exclude_mm else None
        adhesion.build_adhesion(layers, resolved, bed_bbox_um, exclude_bbox_um=exclude_um)
        # 6. Planification.
        prog.stage(9)
        moves = _stage_plan(layers, resolved, fiches_gcode, machine, executor, prog, warnings)
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
    _assign_extruders(moves, extruder_by_object)
    for layer, seen in zip(layers, seen_warnings):
        warnings.extend(f"couche {layer.index} : {w}" for w in layer.warnings[seen:])

    # 7. G-code.
    prog.stage(10)
    context = PrintContext(
        use_ams=_bool(resolved.get("use_ams")), bed_levelling=_bool(resolved.get("bed_levelling", True)),
        timelapse=str(resolved.get("timelapse_type") or "none") != "none",
        vibration_cali=_bool(resolved.get("vibration_calibration", True)), flow_cali=_bool(resolved.get("flow_calibration")),
        bed_type=str(resolved.get("bed_type_short") or "textured_plate"), object_ids=dict(object_names),
        label_objects=_bool(resolved.get("label_objects", True)),
    )
    gcode_settings = dict(resolved)
    gcode_settings["overhang_fan_threshold"] = toolpath._fan_class_threshold(resolved.get("overhang_fan_threshold", 25))
    try:
        result = gcode.emit_gcode(moves, machine, gcode_settings, fiches_gcode, context)
    except gcode.GcodeError as error:
        raise PipelineError(error.code, str(error)) from None
    stats = result.stats
    warnings.extend(stats.warnings)

    # 8. Fichier tranché.
    prog.stage(11)
    support_used = any(layer.support is not None and (layer.support.base_lines or layer.support.interface_lines) for layer in layers)
    usage = {f.extruder: f for f in stats.filaments}
    filaments = []
    for index, fiche in enumerate(fiches, 1):
        used = usage.get(index)
        filaments.append({"id": index, "type": fiche["type"], "color": fiche["colour"], "settings_id": fiche["settings_id"],
                          "tray_info_idx": fiche.get("tray_info_idx") or "", "density": fiche["filament_density"],
                          "vendor": fiche["vendor"], "used_g": float(used.g) if used else 0.0,
                          "used_m": float(used.mm) / 1000.0 if used else 0.0,
                          "nozzle_temperature": fiche["nozzle_temperature"],
                          "nozzle_temperature_initial_layer": fiche["nozzle_temperature_initial_layer"],
                          "filament_max_volumetric_speed": fiche["filament_max_volumetric_speed"],
                          "filament_cost": fiche["filament_cost"], "filament_diameter": fiche["filament_diameter"],
                          **{key: fiche.get(key) for key in ("cool_plate_temp", "cool_plate_temp_initial_layer", "textured_plate_temp",
                                                             "textured_plate_temp_initial_layer", "hot_plate_temp",
                                                             "hot_plate_temp_initial_layer", "eng_plate_temp",
                                                             "eng_plate_temp_initial_layer")}})
    plate = {"index": 1, "gcode_text": result.text, "object_ids": [o["id"] for o in objects], "name": "",
             "stats": {"prediction_s": stats.prediction_s, "first_layer_time_s": stats.first_layer_time_s,
                       "weight_g": stats.total_filament_g,
                       "filament_used_mm": {f.extruder: f.mm for f in stats.filaments},
                       "filament_used_g": {f.extruder: f.g for f in stats.filaments},
                       "layers": stats.layers, "max_z_mm": stats.max_z_mm, "support_used": support_used}}
    machine_info = {"model": machine.model, "model_id": machine.model_id, "preset_name": machine.name, "nozzle_mm": machine.nozzle_mm,
                    "printable_area": (machine.bed.width_mm, machine.bed.depth_mm), "printable_height": machine.bed.height_mm,
                    "bed_exclude_area": machine.bed.exclude_mm, "nozzle_type": machine.nozzle_type}
    output = workdir / output_name(execution_id)
    try:
        threemf_out.write_gcode_3mf(output, [plate], objects, resolved, machine_info, filaments, thumbnails=True)
    except threemf_out.ThreeMFError as error:
        raise PipelineError("ENGINE_FAILED", str(error)) from None
    size = output.stat().st_size
    if debug is not None:
        debug.update({"layers": layers, "moves": moves, "settings": resolved, "machine": machine, "gcode": result,
                      "scene": scene, "fiches": fiches_gcode})
    prog.emit(1.0, "tranchage terminé", force=True)
    plates = [{"index": 1, "prediction_s": round(stats.prediction_s, 1), "weight_g": round(stats.total_filament_g, 2),
               "filaments": [{"id": f.extruder, "type": f.filament_type, "color": f.colour, "used_g": round(f.g, 2),
                              "used_m": round(f.mm / 1000.0, 3)} for f in stats.filaments],
               "layers": int(stats.layers), "first_layer_time_s": round(stats.first_layer_time_s, 1),
               "max_z_mm": round(stats.max_z_mm, 3)}]
    return {
        "artifacts": [output.name], "output": output.name, "bytes": size, "exit_code": 0, "profile_mode": PROFILE_MODE,
        "fallback_used": False, "engine_version": ENGINE_VERSION, "bridge_version": ENGINE_VERSION,
        "bed_type": str(resolved.get("bed_type") or ""), "printer_profile": str(resolved.get("printer_profile") or ""),
        "filament_profiles": [f["settings_id"] for f in fiches], "plates": plates,
        "warnings": [str(w)[:300] for w in dict.fromkeys(warnings)],
        "seconds": round(time.perf_counter() - started, 2), "processes": processes if executor is not None else 1,
    }
