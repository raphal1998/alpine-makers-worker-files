# -*- coding: utf-8 -*-
"""Fonctions PURES du moteur CAM v2 (« Préparer pour une CNC », tranche T2) — aucun import FreeCAD.

Contrat : docs/cam_projet_v2.md (chaque réglage « oui : <Propriété FreeCAD> » est transmis ici à cette propriété,
voir ``operation_properties``) ; constats réels : docs/cam_audit_freecad_1_1_3.md. Livré à plat dans le paquet du
Worker à côté de ``freecad_cam.py`` ; importé par le moteur (sous freecadcmd), par le runner et par les tests.

Contenu :
- ``normalize_project`` : projet v1 (migré par cam_migrate) ou v2 → copie v2 avec TOUS les défauts posés et les
  contrôles que le moteur exige (familles d'outils, Peck + Dwell, bloc thread, valeurs fixes de Waterline, dressups
  refusés, politique de changement d'outil…). Ce n'est pas le validateur du site (cam_payload, T1) : quand celui-ci
  connaît la v2, il passe d'abord ; sinon cette couche garantit seule que rien de muet n'atteint FreeCAD.
- ``plan`` : séquence en mm ; ``operation_properties`` / ``tool_properties`` : correspondance exacte vers FreeCAD ;
- ``sequence_steps`` / ``program_runs`` : pauses M0, consignes, changements d'outil manuels, découpe par outil ;
- ``post_arguments`` / ``grbl_program_checks`` : arguments filtrés par le parser réel, vérification du programme ;
- ``path_analysis`` : polylignes d'aperçu, longueur et durée ESTIMÉE par opération (trapèze si accélération connue) ;
- contrôles : porte-outil, outil trop court, zones trop petites, dégagement vs course, ``not_simulated``.
"""
import copy
import hashlib
import math
import re

CONTRACT_VERSION = 2
ENGINE_VERSION = "2.0"
INCH_MM = 25.4

# Identifiants Alpine ↔ modules FreeCAD CAM 1.1.3 (tous testés en console : audit §2).
OPERATION_MODULES = {
    "facing": "Path.Op.MillFace", "pocket": "Path.Op.Pocket", "profile": "Path.Op.Profile",
    "drilling": "Path.Op.Drilling", "adaptive": "Path.Op.Adaptive", "engrave": "Path.Op.Engrave",
    "vcarve": "Path.Op.Vcarve", "helix": "Path.Op.Helix", "slot": "Path.Op.Slot", "surface": "Path.Op.Surface",
    "waterline": "Path.Op.Waterline", "deburr": "Path.Op.Deburr", "thread_milling": "Path.Op.ThreadMilling",
    "custom": "Path.Op.Custom",
}
MACHINING_TYPES = tuple(key for key in OPERATION_MODULES if key != "custom")
CONTROL_TYPES = ("pause", "tool_change", "note")
OPERATION_TYPES = MACHINING_TYPES + ("custom",) + CONTROL_TYPES
OPERATION_REQUIREMENTS = {"surface": ("ocl",), "waterline": ("ocl",), "adaptive": ("area",), "vcarve": ("area",)}
OPERATIONS_REFUSED = {
    "tapping": "G84 (taraudage) n'existe pas dans GRBL et grbl_post ne le traduit pas.",
    "probe": "le palpage G38.2 avec fichier PROBEOPEN est une convention LinuxCNC, non vérifiée ici.",
}
DEFAULT_NAMES = {"facing": "Surfaçage", "pocket": "Poche", "profile": "Contour", "drilling": "Perçage",
                 "adaptive": "Ébauche adaptative", "engrave": "Gravure", "vcarve": "Gravure en V", "helix": "Hélice",
                 "slot": "Rainure", "surface": "Surfaçage 3D", "waterline": "Niveaux 3D", "deburr": "Ébavurage",
                 "thread_milling": "Fraisage de filet", "custom": "Bloc personnalisé", "pause": "Pause",
                 "tool_change": "Changement d'outil", "note": "Consigne"}
DEFAULT_PHASES = {"facing": "setup", "pocket": "roughing", "profile": "finishing", "drilling": "drilling",
                  "adaptive": "roughing", "engrave": "engraving", "vcarve": "engraving", "helix": "drilling",
                  "slot": "roughing", "surface": "semi_finishing", "waterline": "semi_finishing", "deburr": "finishing",
                  "thread_milling": "finishing", "custom": "other", "pause": "other", "tool_change": "other", "note": "other"}

TOOL_FAMILIES = ("endmill", "drill", "ballend", "bullnose", "vbit", "chamfer", "engraver", "threadmill")
TOOL_FAMILIES_REFUSED = {
    "tap": "taraud : G84 inconnu de GRBL (le filetage se fraise avec « threadmill »).",
    "reamer": "alésoir : aucune opération console ne l'exploite.", "slittingsaw": "fraise scie : aucune opération console.",
    "dovetail": "queue d'aronde : contre-dépouille impossible sur 3 axes.", "radius": "fraise à rayon : non vérifiée.",
    "taperedballnose": "boule conique : non vérifiée en console.", "probe": "palpeur : voir « probe » refusé.",
}
# Formes Mod/CAM/Tools/Shape/<forme>.fcstd (14/14 vérifiées) ; « engraver » utilise la forme V.
FAMILY_SHAPES = {"endmill": "endmill", "drill": "drill", "ballend": "ballend", "bullnose": "bullnose", "vbit": "v-bit",
                 "chamfer": "chamfer", "engraver": "v-bit", "threadmill": "thread-mill"}
OPERATION_TOOL_FAMILIES = {
    "facing": ("endmill", "bullnose"), "pocket": ("endmill", "bullnose", "ballend"), "profile": ("endmill", "bullnose", "ballend"),
    "drilling": ("drill", "endmill"), "adaptive": ("endmill", "bullnose", "ballend"),
    "engrave": ("vbit", "engraver", "endmill", "ballend", "chamfer"), "vcarve": ("vbit", "engraver", "chamfer"),
    "helix": ("endmill", "bullnose"), "slot": ("endmill", "bullnose", "ballend"),
    "surface": ("ballend", "bullnose", "endmill", "vbit", "chamfer", "engraver"),
    "waterline": ("ballend", "bullnose", "endmill", "vbit", "chamfer", "engraver"), "deburr": ("chamfer", "vbit", "engraver"),
    "thread_milling": ("threadmill",), "custom": TOOL_FAMILIES, "tool_change": TOOL_FAMILIES,
}
ANGLE_FAMILIES = ("vbit", "chamfer", "engraver")

DRESSUP_TYPES = ("tags", "dogbone", "boundary")
DRESSUP_MODULES = {"tags": "Path.Dressup.Tags", "dogbone": "Path.Dressup.DogboneII", "boundary": "Path.Dressup.Boundary"}
DRESSUP_REFUSED = {
    "ramp_entry": "RampEntry n'a pas de fonction Create en console FreeCAD 1.1.3 (classe GUI).",
    "lead_in_out": "LeadInOut n'a pas de fonction Create en console FreeCAD 1.1.3 (classe GUI).",
    "dragknife": "Dragknife : classe GUI seulement.", "z_correct": "ZCorrect : classe GUI et fichier de palpage.",
    "axis_map": "AxisMap : 4e axe inexistant.", "array": "Array non proposé en 2.0.",
}
DRESSUP_OPERATIONS = {"tags": ("profile", "pocket", "adaptive", "slot", "engrave", "vcarve"),
                      "dogbone": ("pocket", "profile", "adaptive", "slot"), "boundary": MACHINING_TYPES}

# Forme réellement lue par FreeCAD 1.1.3 (contrat §5, audit §4).
FLAT_READ_TYPES = ("facing", "pocket", "profile", "adaptive", "helix", "slot", "drilling")
TOOL_CHANGE_POLICIES = ("pause_m0", "split_files", "atc_m6")
COOLANT_FREECAD = {"flood": "Flood", "mist": "Mist", "air": "Mist", "off": "None"}
CUSTOM_WORDS = frozenset({"G0", "G1", "G4", "G17", "G90", "G94", "M0", "M1", "M3", "M4", "M5", "M7", "M8", "M9"})
# Mots qu'un programme GRBL 1.1 ne doit jamais contenir (TLO, cycles, taraudage, compensation).
GRBL_FORBIDDEN = ("G43", "G49", "G84", "G81", "G82", "G83", "G73", "G98", "G99", "G80", "G41", "G42")
STEPOVER_DEFAULTS = {"adaptive": 20.0, "surface": 50.0, "waterline": 50.0}
TOLERANCE_DEFAULTS = {"adaptive": 0.1, "vcarve": 0.01}
STEPOVER_TYPES = ("facing", "pocket", "profile", "adaptive", "helix", "slot", "surface", "waterline")
PREVIEW_POINTS_PER_OP = 6000
PREVIEW_POINTS_TOTAL = 60000

# Réglages propres (contrat §6.3) : clé → (défaut en mm ou valeur, admis). ``None`` = toute valeur du type attendu.
TYPE_SETTINGS = {
    "facing": {"boundary": ("stock", ("stock", "boundbox", "perimeter", "face_region")),
               "pattern": ("zigzag", ("zigzag", "offset", "zigzag_offset", "line", "grid")), "zigzag_angle": (45.0, None),
               "clear_edges": (True, None), "start_at": ("center", ("center", "edge")), "exclude_raised": (False, None),
               "rest_machining": (False, None)},
    "pocket": {"pattern": ("zigzag", ("zigzag", "offset", "zigzag_offset", "line", "grid")), "zigzag_angle": (45.0, None),
               "start_at": ("center", ("center", "edge")), "rest_machining": (False, None)},
    "profile": {"side": ("outside", ("outside", "inside")), "use_comp": (True, None), "passes": (1, None),
                "pass_stepover": (None, None), "join_type": ("round", ("round", "square", "miter")),
                "process_perimeter": (True, None), "process_holes": (False, None), "process_circles": (False, None),
                "handle_multiple": ("collectively", ("collectively", "individually"))},
    "drilling": {"peck": (0.0, None), "dwell": (0.0, None), "retract_mode": ("G98", ("G98", "G99")),
                 "retract_height": (None, None), "extra_offset": ("none", ("none", "drill_tip", "2x_drill_tip"))},
    "adaptive": {"operation_type": ("clearing", ("clearing", "profiling")), "side": ("inside", ("inside", "outside")),
                 "helix_angle": (5.0, None), "helix_cone_angle": (0.0, None), "helix_diameter_limit": (0.0, None),
                 "use_helix_arcs": (False, None), "lift_distance": (0.0, None), "keep_tool_down_ratio": (3.0, None),
                 "finishing_profile": (True, None), "force_inside_out": (True, None), "order_by_region": (True, None)},
    "engrave": {"start_vertex": (0, None)},
    "vcarve": {"discretize": (0.25, None), "colinear": (10.0, None), "finishing_pass": (False, None),
               "finishing_pass_z_offset": (0.0, None), "optimize_movements": (True, None)},
    "helix": {"start_side": ("inside", ("inside", "outside")), "start_radius": (0.0, None), "path_direction": ("cw", ("cw", "ccw"))},
    "slot": {"pattern": ("line", ("line", "zigzag")), "layer_mode": ("multi", ("single", "multi")), "extend_start": (0.0, None),
             "extend_end": (0.0, None), "reverse": (False, None)},
    "surface": {"requires": (["ocl"], None), "bound_box": ("stock", ("base", "stock")), "scan_type": ("planar", ("planar",)),
                "pattern": ("line", ("circular", "circular_zigzag", "line", "offset", "spiral", "zigzag")), "pattern_angle": (0.0, None),
                "layer_mode": ("single", ("single", "multi")), "sample_interval": (1.0, None), "drop_cutter_dir": ("x", ("x", "y")),
                "profile_edges": ("none", ("none", "only", "first", "last")), "boundary_enforcement": (True, None),
                "boundary_adjustment": (0.0, None), "internal_features_cut": (True, None), "optimize_linear_paths": (True, None),
                "optimize_stepover_transitions": (False, None), "circular_use_g2g3": (False, None), "linear_deflection": (0.0025, None)},
    "waterline": {"requires": (["ocl"], None), "algorithm": ("ocl_dropcutter", ("ocl_dropcutter",)), "bound_box": ("stock", ("stock",)),
                  "layer_mode": ("multi", ("multi",)), "clear_last_layer": (False, None),
                  "pattern": ("line", ("circular", "circular_zigzag", "line", "offset", "spiral", "zigzag")),
                  "ignore_outer_above": (0.0, None), "sample_interval": (1.0, None), "boundary_enforcement": (True, None),
                  "boundary_adjustment": (0.0, None), "internal_features_cut": (True, None)},
    "deburr": {"width": (1.0, None), "extra_depth": (0.5, None), "join": ("round", ("round", "miter")),
               "path_direction": ("cw", ("cw", "ccw")), "side": ("outside", ("inside", "outside")), "entry_point": (0, None)},
    "thread_milling": {"thread": (None, None), "passes": (1, None), "orientation": ("right", ("right", "left")),
                       "path_direction": ("cw", ("cw", "ccw")), "lead_in_out": (True, None)},
    "custom": {"gcode_lines": (None, None)},
    "pause": {"stop_spindle": (True, None)},
    "tool_change": {"tool": (None, None), "go_to_change_position": (False, None)},
    "note": {},
}
# Clés en mm à convertir (le reste est sans unité, en degrés, en secondes ou énuméré).
LENGTH_SETTINGS = ("pass_stepover", "peck", "retract_height", "helix_diameter_limit", "lift_distance", "keep_tool_down_ratio",
                   "sample_interval", "boundary_adjustment", "linear_deflection", "ignore_outer_above", "width", "extra_depth",
                   "discretize", "start_radius", "extend_start", "extend_end")
THREAD_TYPES = {"custom_internal": "CustomInternal", "custom_external": "CustomExternal", "metric_internal_6h": "MetricInternal6H",
                "metric_external_6g": "MetricExternal6G", "metric_external_4g6g": "MetricExternal4G6G",
                "imperial_internal_2b": "ImperialInternal2B", "imperial_internal_3b": "ImperialInternal3B",
                "imperial_external_2a": "ImperialExternal2A", "imperial_external_3a": "ImperialExternal3A"}
PATTERNS_FREECAD = {"zigzag": "ZigZag", "offset": "Offset", "zigzag_offset": "ZigZagOffset", "line": "Line", "grid": "Grid",
                    "circular": "Circular", "circular_zigzag": "CircularZigZag", "spiral": "Spiral"}
BOUNDARY_FREECAD = {"stock": "Stock", "boundbox": "Boundbox", "perimeter": "Perimeter", "face_region": "Face Region"}
DOGBONE_STYLES = {"dogbone": "Dogbone", "t_bone_horizontal": "T-bone horizontal", "t_bone_vertical": "T-bone vertical",
                  "t_bone_long_edge": "T-bone long edge", "t_bone_short_edge": "T-bone short edge"}


class CamOpsError(ValueError):
    """Refus lisible ; ``path`` désigne le champ fautif (même convention que cam_payload.CamPayloadError)."""

    def __init__(self, path, message):
        self.path = path
        super().__init__("%s : %s" % (path, message) if path else message)


def _fail(path, message):
    raise CamOpsError(path, message)


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _require_number(value, path, low=None, high=None):
    if not _num(value):
        _fail(path, "nombre fini attendu.")
    if (low is not None and value < low - 1e-9) or (high is not None and value > high + 1e-9):
        _fail(path, "valeur hors limites (attendu entre %g et %g)." % (low if low is not None else -math.inf, high if high is not None else math.inf))
    return float(value)


def _require_bool(value, path):
    if not isinstance(value, bool):
        _fail(path, "vrai ou faux attendu.")
    return value


def _require_choice(value, path, choices):
    if not isinstance(value, str) or value not in choices:
        _fail(path, "valeur non admise (attendu : %s)." % ", ".join(choices))
    return value


def comment_text(text):
    """Texte sûr dans un commentaire G-code « ( … ) » : jamais de parenthèse qui fermerait le commentaire."""
    clean = "".join(char if char.isprintable() else " " for char in str(text))
    return clean.replace("(", "[").replace(")", "]").replace(";", ",").strip()[:200]


def file_sha256(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def project_factor(project):
    return INCH_MM if project.get("units") == "inch" else 1.0


# ---------------------------------------------------------------------------------------------
# Forme réellement utilisée
# ---------------------------------------------------------------------------------------------
def engine_tool_shape(op_type, family):
    """Forme que FreeCAD 1.1.3 lit vraiment pour cette opération (contrat §5)."""
    if op_type in FLAT_READ_TYPES:
        note = "" if family in ("endmill", "drill") else "lu comme une fraise plate du même diamètre (seule la propriété Diameter est utilisée)"
        return {"shape": "flat", "diameter_only": True, "note": note}
    if op_type in ("surface", "waterline"):
        return {"shape": family, "diameter_only": False, "note": "forme réelle (OCL : CylCutter / BallCutter / BullCutter / ConeCutter)"}
    if op_type in ("vcarve", "deburr"):
        return {"shape": "v_angle", "diameter_only": False, "note": "angle de coupe et diamètre de pointe (profondeur dérivée de l'angle)"}
    if op_type == "thread_milling":
        return {"shape": "thread", "diameter_only": False, "note": "diamètre comparé au diamètre mineur, crête, angle"}
    return {"shape": "none", "diameter_only": False, "note": "trajectoire sur les arêtes ou texte : le diamètre ne sert qu'aux contrôles"}


# ---------------------------------------------------------------------------------------------
# Normalisation v2 (défauts + contrôles moteur)
# ---------------------------------------------------------------------------------------------
def _migrate_module():
    try:
        from worker_agent import cam_migrate
        return cam_migrate
    except ImportError:
        try:
            import cam_migrate
            return cam_migrate
        except ImportError:
            return None


def normalize_request_project(project, cam_payload=None):
    """Projet reçu (v1 ou v2) → projet v2 normalisé prêt pour le moteur.

    Si ``cam_payload`` connaît la v2 (T1 livrée), il valide d'abord ; sinon un v1 passe par le validateur v1 et un v2
    est contrôlé par ``normalize_project`` seule. La migration v1 → v2 est celle de cam_migrate.upgrade.
    """
    if not isinstance(project, dict):
        _fail("", "Projet CAM invalide : objet attendu.")
    version = project.get("version")
    if cam_payload is not None and (getattr(cam_payload, "CAM_PAYLOAD_VERSION", 1) >= 2 or version == 1):
        project = cam_payload.validate_cam_project(project)
    elif version not in (1, 2) or isinstance(version, bool):
        _fail("version", "version de projet CAM non prise en charge (1 ou 2).")
    return normalize_project(project)


CAM_JOB_OPS = ("capabilities", "compute", "preview")


def required_capability(project):
    """``freecad_cam_v1`` pour un projet v1 (ou v2 sans fonction v2), ``freecad_cam_v2`` sinon."""
    if not isinstance(project, dict):
        return "freecad_cam_v2"
    if project.get("version") == 1:
        return "freecad_cam_v1"
    migrate = _migrate_module()
    if migrate is not None and not migrate.v2_features(project):
        return "freecad_cam_v1"
    return "freecad_cam_v2"


def validate_job_parameters(parameters, cam_payload=None):
    """Paramètres d'un job ``converter`` portant ``cam`` (ops capabilities / compute / preview) : copie validée.

    Même rôle que ``cam_payload.validate_cam_job_parameters`` (v1) avec la v2 et l'aperçu en plus. Le projet GARDE sa
    version : un v1 reste v1 (les Workers 1.50.44 le consomment), un v2 est normalisé v2 ; le moteur migre lui-même.
    """
    if not isinstance(parameters, dict) or not isinstance(parameters.get("cam"), dict):
        _fail("cam", "paramètres CAM invalides.")
    if "scene" in parameters:
        _fail("scene", "un job CAM ne porte pas de scène paramétrique.")
    if cam_payload is not None and getattr(cam_payload, "CAM_PAYLOAD_VERSION", 1) >= 2:
        # Contrat T1 livré : cam_payload valide capabilities / compute (version conservée) / preview ; le moteur ajoute
        # ses propres contrôles (familles, Peck + Dwell, Waterline…) sans réécrire le projet transmis.
        clean = cam_payload.validate_cam_job_parameters(parameters)
        if clean["cam"]["op"] == "compute":
            normalize_project(clean["cam"]["project"])
        return clean
    clean = copy.deepcopy(parameters)
    cam = clean["cam"]
    operation = cam.get("op")
    if operation == "preview":
        _fail("cam.op", "l'aperçu exige le validateur cam_payload v2.")
    if operation == "capabilities":
        if set(cam) - {"op"}:
            _fail("cam", "la lecture des capacités ne prend que « op ».")
        clean["cam"] = {"op": "capabilities"}
        for key in ("input_file", "input_files", "output_name"):
            if key in clean:
                _fail(key, "la lecture des capacités ne prend aucun fichier.")
        return clean
    if operation not in ("compute", "preview"):
        _fail("cam.op", "opération CAM inconnue (capabilities, compute ou preview).")
    unknown = sorted(set(cam) - {"op", "project"}, key=str)
    if unknown:
        _fail("cam.%s" % unknown[0], "champ inconnu, refusé.")
    if "project" not in cam:
        _fail("cam.project", "champ obligatoire manquant.")
    project = cam["project"]
    version = project.get("version") if isinstance(project, dict) else None
    if version == 1 and cam_payload is not None:
        project = cam_payload.validate_cam_project(project)
        normalize_project(project)   # contrôles moteur (familles, Peck + Dwell…) sans réécrire le projet v1 envoyé
    else:
        project = normalize_request_project(project, cam_payload)
    clean["cam"] = {"op": operation, "project": project}
    if "input_files" in clean:
        _fail("input_files", "un calcul CAM prend un seul fichier source.")
    output_name = clean.get("output_name")
    if output_name is not None and output_name != project["output"]["stem"]:
        _fail("output_name", "le nom de sortie doit être celui du projet (output.stem).")
    clean["output_name"] = project["output"]["stem"]
    return clean


def normalize_project(project):
    """Copie v2 normalisée (défauts posés, unités du projet conservées) ; ``CamOpsError`` sur un refus moteur."""
    if not isinstance(project, dict):
        _fail("", "Projet CAM invalide : objet attendu.")
    version = project.get("version")
    if version == 1:
        migrate = _migrate_module()
        if migrate is None:
            _fail("version", "module cam_migrate absent : impossible de migrer un projet v1.")
        project, _notes = migrate.upgrade(project)
    elif version != 2 or isinstance(version, bool):
        _fail("version", "version de projet CAM non prise en charge (1 ou 2).")
    clean = copy.deepcopy(project)
    clean["version"] = 2
    units = clean.get("units")
    if units not in ("mm", "inch"):
        _fail("units", "unités attendues : mm ou inch.")
    factor = INCH_MM if units == "inch" else 1.0
    for key in ("name", "source", "machine", "stock", "tools", "operations"):
        if key not in clean:
            _fail(key, "champ obligatoire manquant.")
    source = clean["source"]
    if not isinstance(source, dict) or not isinstance(source.get("file"), str) or not source.get("kind"):
        _fail("source", "fichier source et type attendus.")
    source.setdefault("scale", 1)
    source.setdefault("sha256", None)
    _require_number(source["scale"], "source.scale", 0.001, 1000.0)
    if source.get("units") not in ("mm", "inch"):
        _fail("source.units", "unités du modèle attendues : mm ou inch.")
    clean["orientation"] = dict({"top_face": "+z", "rotation_z_deg": 0, "flip": False}, **(clean.get("orientation") or {}))
    machine = _normalize_machine(clean["machine"])
    clean["machine"] = machine
    clean["stock"] = _normalize_stock(clean["stock"], factor, machine)
    clean["origin"] = dict({"preset": "top_front_left", "offset": {"x": 0, "y": 0, "z": 0}, "wcs": "G54"}, **(clean.get("origin") or {}))
    safe_default = max(5.0, machine["safe_height_min"])
    heights = dict({"clearance": max(10.0, safe_default) / factor, "safe": safe_default / factor, "retract": 3.0 / factor,
                    "tolerance": 0.01 / factor}, **(clean.get("heights") or {}))
    for key in ("clearance", "safe", "retract", "tolerance"):
        _require_number(heights[key], "heights.%s" % key, 0.0)
    if not heights["clearance"] >= heights["safe"] >= heights["retract"] > 0:
        _fail("heights", "ordre attendu : dégagement ≥ sécurité ≥ retrait > 0.")
    clean["heights"] = heights
    clean["fixtures"] = list(clean.get("fixtures") or [])
    clean["tools"] = [_normalize_tool(tool, index, factor, machine) for index, tool in enumerate(clean["tools"])]
    numbers = [tool["number"] for tool in clean["tools"]]
    if len(set(numbers)) != len(numbers):
        _fail("tools", "deux outils portent le même numéro.")
    by_number = {tool["number"]: tool for tool in clean["tools"]}
    clean["sequence"] = dict({"tool_change_policy": "pause_m0", "spindle_wait_s": 0}, **(clean.get("sequence") or {}))
    policy = _require_choice(clean["sequence"]["tool_change_policy"], "sequence.tool_change_policy", TOOL_CHANGE_POLICIES)
    _require_number(clean["sequence"]["spindle_wait_s"], "sequence.spindle_wait_s", 0.0, 60.0)
    operations, defined = [], []
    for index, operation in enumerate(clean["operations"]):
        operations.append(_normalize_operation(operation, index, factor, machine, by_number, heights, defined))
        defined.append(operations[-1]["id"])
    if len(set(defined)) != len(defined):
        _fail("operations", "deux opérations portent le même identifiant.")
    if not any(op["enabled"] and op["type"] in MACHINING_TYPES + ("custom",) for op in operations):
        _fail("operations", "aucune opération d'usinage activée.")
    clean["operations"] = operations
    if policy == "atc_m6":
        if machine["tool_change"] != "atc":
            _fail("sequence.tool_change_policy", "« atc_m6 » exige une machine à changeur automatique (tool_change = atc).")
        if str(machine["controller"]).startswith("grbl"):
            _fail("sequence.tool_change_policy", "GRBL 1.1 refuse M6 (erreur 20) : « pause_m0 » ou « split_files ».")
        distinct = {op["tool"] for op in operations if op.get("tool") and op["type"] != "tool_change" and op["enabled"]}
        if len(distinct) > machine["tool_count"]:
            _fail("sequence.tool_change_policy", "plus d'outils distincts que de places dans le changeur.")
    clean["post"] = dict({"options": {}, "preamble": [], "postamble": []}, **(clean.get("post") or {}))
    if not isinstance(clean.get("output"), dict) or not clean["output"].get("stem"):
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(clean["name"])).strip("._-")[:60] or "programme_cnc"
        clean["output"] = {"stem": slug}
    return clean


def _normalize_machine(machine):
    if not isinstance(machine, dict):
        _fail("machine", "objet attendu.")
    machine = dict(machine)
    for key in ("travel", "max_feed", "spindle", "safe_height_min", "controller", "post_processor", "output_extension"):
        if key not in machine:
            _fail("machine.%s" % key, "champ obligatoire manquant.")
    if list(machine.get("axes") or ["X", "Y", "Z"]) != ["X", "Y", "Z"]:
        _fail("machine.axes", "seules les machines 3 axes X Y Z sont calculables.")
    machine["axes"] = ["X", "Y", "Z"]
    for axis in ("x", "y", "z"):
        _require_number((machine["travel"] or {}).get(axis), "machine.travel.%s" % axis, 1.0, 3000.0)
    _require_number(machine["max_feed"], "machine.max_feed", 1.0, 50000.0)
    for key, default in (("max_rapid", None), ("max_accel", None), ("tool_change", "manual"), ("tool_count", 1), ("units", "mm"),
                         ("coolant", []), ("unsupported_commands", []), ("notes", ""), ("label", str(machine.get("profile_id", ""))),
                         ("tool_change_position", None), ("grbl", None), ("provenance", {}), ("firmware_snapshot", None),
                         ("probe_z", False), ("tool_length_measure", False), ("kind", "router")):
        machine.setdefault(key, default)
    if machine.get("max_rapid") is not None:
        _require_number(machine["max_rapid"], "machine.max_rapid", 1.0, 50000.0)
    if machine.get("max_accel") is not None:
        _require_number(machine["max_accel"], "machine.max_accel", 1.0, 100000.0)
    machine["tool_count"] = int(_require_number(machine["tool_count"], "machine.tool_count", 1, 64))
    machine["safe_height_min"] = _require_number(machine["safe_height_min"], "machine.safe_height_min", 0.0, 500.0)
    _require_choice(machine["tool_change"], "machine.tool_change", ("manual", "atc"))
    _require_choice(machine["units"], "machine.units", ("mm", "inch"))
    spindle = machine["spindle"]
    if not isinstance(spindle, dict):
        _fail("machine.spindle", "objet attendu.")
    spindle = dict(spindle)
    spindle.setdefault("directions", ["cw"])
    spindle.setdefault("power_w", None)
    _require_number(spindle.get("min_rpm"), "machine.spindle.min_rpm", 0.0, 100000.0)
    _require_number(spindle.get("max_rpm"), "machine.spindle.max_rpm", 0.0, 100000.0)
    machine["spindle"] = spindle
    grbl = machine.get("grbl")
    if isinstance(grbl, dict) and grbl.get("laser_mode") is True:
        _fail("machine.grbl.laser_mode", "$32=1 : carte en mode laser, aucun fraisage n'est envoyé.")
    position = machine.get("tool_change_position")
    if position is not None:
        if not isinstance(position, dict):
            _fail("machine.tool_change_position", "objet {x, y, z, verified} ou null attendu.")
        position = dict(position)
        position.setdefault("verified", False)
        for axis in ("x", "y", "z"):
            _require_number(position.get(axis), "machine.tool_change_position.%s" % axis, -3000.0, 3000.0)
        machine["tool_change_position"] = position
    return machine


def _normalize_stock(stock, factor, machine):
    if not isinstance(stock, dict):
        _fail("stock", "objet attendu.")
    stock = dict(stock)
    stock.setdefault("shape", "box")
    stock.setdefault("size", {"x": 0, "y": 0, "z": 0})
    stock.setdefault("margins", {"x": 0, "y": 0, "top": 0, "bottom": 0})
    stock.setdefault("placement", "center_top")
    stock.setdefault("placement_offset", {"x": 0, "y": 0, "z": 0})
    stock.setdefault("material_ref", None)
    stock.setdefault("material_label", "")
    stock.pop("material_id", None)
    mode = _require_choice(stock.get("mode"), "stock.mode", ("from_model", "explicit"))
    shape = _require_choice(stock["shape"], "stock.shape", ("box", "cylinder"))
    _require_choice(stock["placement"], "stock.placement", ("center_top", "min_corner", "custom"))
    for key in ("x", "y", "top", "bottom"):
        _require_number(stock["margins"].get(key), "stock.margins.%s" % key, 0.0)
    if shape == "cylinder":
        if mode != "explicit":
            _fail("stock.shape", "un brut cylindrique exige mode = explicit (diamètre et hauteur saisis).")
        diameter = _require_number(stock.get("diameter"), "stock.diameter", 0.001) * factor
        height = _require_number(stock.get("height"), "stock.height", 0.001) * factor
        if diameter > min(machine["travel"]["x"], machine["travel"]["y"]) + 1e-9 or height > machine["travel"]["z"] + 1e-9:
            _fail("stock.diameter", "brut cylindrique plus grand que la course de la machine.")
    elif mode == "explicit":
        for axis in ("x", "y", "z"):
            _require_number(stock["size"].get(axis), "stock.size.%s" % axis, 0.001)
    return stock


def _normalize_tool(tool, index, factor, machine):
    where = "tools[%d]" % index
    if not isinstance(tool, dict):
        _fail(where, "objet attendu.")
    tool = dict(tool)
    family = tool.get("family")
    if family in TOOL_FAMILIES_REFUSED:
        _fail(where + ".family", "famille refusée : " + TOOL_FAMILIES_REFUSED[family])
    _require_choice(family, where + ".family", TOOL_FAMILIES)
    for key in ("number", "name", "diameter", "flute_length", "spindle_rpm", "feed_xy", "feed_plunge"):
        if key not in tool:
            _fail("%s.%s" % (where, key), "champ obligatoire manquant.")
    tool["number"] = int(_require_number(tool["number"], where + ".number", 1, 999))
    diameter = _require_number(tool["diameter"], where + ".diameter", 0.05 / factor, 100.0 / factor)
    flute = _require_number(tool["flute_length"], where + ".flute_length", 0.1 / factor, 300.0 / factor)
    defaults = {"length": max(2.0 * flute, 1.0 / factor), "shank_diameter": diameter, "flutes": 2, "material": "carbide",
                "coating": "", "chip_load": None, "stickout": None, "holder_diameter": None, "holder_length": None,
                "length_offset": None, "tip_angle": 118.0 if family == "drill" else 0.0, "corner_radius": 0.0,
                "cutting_edge_angle": 60.0 if family == "threadmill" else None, "tip_diameter": 0.0, "crest": 0.1 / factor,
                "neck_diameter": diameter * 0.6, "neck_length": flute, "source_ref": None,
                "max_rpm": machine["spindle"]["max_rpm"], "feed_ramp": None, "step_down": min(diameter / 2.0, flute),
                "stepover_pct": 40.0, "coolant": "off"}
    for key, value in defaults.items():
        if tool.get(key) is None:
            tool[key] = value
    tool["length"] = _require_number(tool["length"], where + ".length", 1.0 / factor, 500.0 / factor)
    if flute > tool["length"] + 1e-9:
        _fail(where + ".flute_length", "longueur taillée supérieure à la longueur de l'outil.")
    tool["flutes"] = int(_require_number(tool["flutes"], where + ".flutes", 1, 16))
    _require_number(tool["spindle_rpm"], where + ".spindle_rpm", 0.0, 100000.0)
    if tool["spindle_rpm"] <= 0:
        _fail(where + ".spindle_rpm", "vitesse de broche nulle : une fraise doit tourner pour couper.")
    for key in ("feed_xy", "feed_plunge"):
        feed = _require_number(tool[key], "%s.%s" % (where, key), 1.0 / factor, 50000.0 / factor)
        if feed * factor > machine["max_feed"] + 1e-9:
            _fail("%s.%s" % (where, key), "avance au-dessus du maximum de la machine (%g mm/min)." % machine["max_feed"])
    if tool["feed_ramp"] is not None:
        _require_number(tool["feed_ramp"], where + ".feed_ramp", 1.0 / factor, 50000.0 / factor)
    tool["step_down"] = _require_number(tool["step_down"], where + ".step_down", 0.001 / factor)
    if tool["step_down"] > flute + 1e-9:
        _fail(where + ".step_down", "profondeur de passe supérieure à la longueur taillée.")
    tool["stepover_pct"] = _require_number(tool["stepover_pct"], where + ".stepover_pct", 1.0, 100.0)
    tool["corner_radius"] = _require_number(tool["corner_radius"], where + ".corner_radius", 0.0, 50.0 / factor)
    if tool["corner_radius"] > diameter / 2.0 + 1e-9:
        _fail(where + ".corner_radius", "rayon de coin supérieur au rayon de l'outil.")
    if family == "bullnose" and tool["corner_radius"] <= 0:
        _fail(where + ".corner_radius", "une fraise torique (bullnose) exige un rayon de coin > 0.")
    if family in ANGLE_FAMILIES:
        if tool.get("cutting_edge_angle") is None:
            _fail(where + ".cutting_edge_angle", "l'angle de coupe est obligatoire pour « %s »." % family)
        _require_number(tool["cutting_edge_angle"], where + ".cutting_edge_angle", 1.0, 179.0)
        tool["tip_diameter"] = _require_number(tool["tip_diameter"], where + ".tip_diameter", 0.0, 50.0 / factor)
        if tool["tip_diameter"] >= diameter:
            _fail(where + ".tip_diameter", "diamètre de pointe supérieur ou égal au diamètre de l'outil.")
    elif family == "threadmill":
        _require_number(tool["cutting_edge_angle"], where + ".cutting_edge_angle", 1.0, 179.0)
        _require_number(tool["crest"], where + ".crest", 0.01 / factor, 5.0 / factor)
        if _require_number(tool["neck_diameter"], where + ".neck_diameter", 0.05 / factor) >= diameter:
            _fail(where + ".neck_diameter", "col plus large que le diamètre de coupe.")
    elif tool.get("cutting_edge_angle") not in (None, 0):
        _fail(where + ".cutting_edge_angle", "angle de coupe sans objet pour « %s »." % family)
    if (tool["holder_diameter"] is None) != (tool["holder_length"] is None):
        _fail(where + ".holder_diameter", "diamètre et longueur du porte-outil vont ensemble.")
    if tool["holder_diameter"] is not None:
        _require_number(tool["holder_diameter"], where + ".holder_diameter", 1.0 / factor, 200.0 / factor)
        _require_number(tool["holder_length"], where + ".holder_length", 1.0 / factor, 500.0 / factor)
    if tool["stickout"] is not None:
        _require_number(tool["stickout"], where + ".stickout", 1.0 / factor, 500.0 / factor)
    _require_choice(tool["material"], where + ".material", ("carbide", "hss", "cobalt", "diamond", "ceramic", "other"))
    _require_choice(tool["coolant"], where + ".coolant", ("off", "flood", "mist", "air"))
    return tool


def _normalize_operation(operation, index, factor, machine, tools, heights, defined):
    where = "operations[%d]" % index
    if not isinstance(operation, dict):
        _fail(where, "objet attendu.")
    operation = dict(operation)
    kind = operation.get("type")
    if kind in OPERATIONS_REFUSED:
        _fail(where + ".type", "opération refusée : " + OPERATIONS_REFUSED[kind])
    _require_choice(kind, where + ".type", OPERATION_TYPES)
    identifier = operation.get("id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", identifier):
        _fail(where + ".id", "identifiant invalide (minuscules, chiffres, « _ » et « - »).")
    for key, default in (("name", DEFAULT_NAMES[kind]), ("description", ""), ("enabled", True), ("phase", DEFAULT_PHASES[kind]),
                         ("operator_note", ""), ("depends_on", []), ("pause_before", False)):
        if operation.get(key) is None:
            operation[key] = default
    _require_bool(operation["enabled"], where + ".enabled")
    _require_bool(operation["pause_before"], where + ".pause_before")
    operation["operator_note"] = str(operation["operator_note"])[:240]
    for position, reference in enumerate(operation["depends_on"]):
        if reference not in defined:
            _fail("%s.depends_on[%d]" % (where, position), "une opération ne peut dépendre que d'une opération définie avant elle.")
    for key, (default, choices) in TYPE_SETTINGS[kind].items():
        if operation.get(key) is None:
            operation[key] = (default / factor if key in LENGTH_SETTINGS and _num(default) else copy.deepcopy(default))
        if choices is not None:
            _require_choice(operation[key], "%s.%s" % (where, key), choices)
    if kind == "note":
        if not operation["operator_note"].strip():
            _fail(where + ".operator_note", "une consigne opérateur ne peut pas être vide.")
        return operation
    if kind == "pause":
        _require_bool(operation["stop_spindle"], where + ".stop_spindle")
        return operation
    if "tool" not in operation or operation["tool"] is None:
        _fail(where + ".tool", "outil obligatoire.")
    number = operation["tool"]
    if isinstance(number, bool) or not isinstance(number, (int, float)) or int(number) not in tools:
        _fail(where + ".tool", "outil n°%s absent de la bibliothèque du projet." % number)
    operation["tool"] = int(number)
    tool = tools[operation["tool"]]
    if kind == "tool_change":
        _require_bool(operation["go_to_change_position"], where + ".go_to_change_position")
        position = machine.get("tool_change_position")
        if operation["go_to_change_position"] and not (isinstance(position, dict) and position.get("verified") is True):
            _fail(where + ".go_to_change_position", "position de changement d'outil absente ou non vérifiée sur la machine.")
        return operation
    if tool["family"] not in OPERATION_TOOL_FAMILIES[kind]:
        _fail(where + ".tool", "un outil « %s » ne convient pas à l'opération « %s »." % (tool["family"], kind))
    for key in ("spindle_dir", "coolant", "feed_xy", "feed_plunge", "feed_ramp", "spindle_rpm"):
        operation.setdefault(key, None)
    operation["spindle_dir"] = operation["spindle_dir"] or "cw"
    operation["coolant"] = operation["coolant"] or "off"
    _require_choice(operation["spindle_dir"], where + ".spindle_dir", ("cw", "ccw"))
    if operation["spindle_dir"] not in machine["spindle"]["directions"]:
        _fail(where + ".spindle_dir", "sens de rotation non pris en charge par la broche de la machine.")
    _require_choice(operation["coolant"], where + ".coolant", ("off", "flood", "mist", "air"))
    if operation["coolant"] != "off" and operation["coolant"] not in machine["coolant"]:
        _fail(where + ".coolant", "arrosage absent de la machine.")
    for key in ("feed_xy", "feed_plunge", "feed_ramp"):
        if operation[key] is not None:
            feed = _require_number(operation[key], "%s.%s" % (where, key), 1.0 / factor)
            if feed * factor > machine["max_feed"] + 1e-9:
                _fail("%s.%s" % (where, key), "avance au-dessus du maximum de la machine.")
    if operation["spindle_rpm"] is not None:
        rpm = _require_number(operation["spindle_rpm"], where + ".spindle_rpm", 0.0, 100000.0)
        if rpm <= 0 or rpm > min(machine["spindle"]["max_rpm"], tool["max_rpm"]) + 1e-9 or rpm + 1e-9 < machine["spindle"]["min_rpm"]:
            _fail(where + ".spindle_rpm", "vitesse de broche hors des bornes de l'outil ou de la machine.")
    if kind == "custom":
        lines = operation.get("gcode_lines")
        if not isinstance(lines, list) or not 1 <= len(lines) <= 40:
            _fail(where + ".gcode_lines", "1 à 40 lignes attendues.")
        operation["gcode_lines"] = [validate_custom_motion_line(line, "%s.gcode_lines[%d]" % (where, position), factor)
                                    for position, line in enumerate(lines)]
        for key in ("depth", "stepover_pct", "stock_to_leave", "tolerance", "dressups", "selection", "heights", "direction"):
            if key in operation:
                _fail("%s.%s" % (where, key), "sans objet pour un bloc personnalisé (refusé).")
        return operation
    return _normalize_machining(operation, where, kind, factor, machine, tool, heights)


def _normalize_machining(operation, where, kind, factor, machine, tool, heights):
    selection = dict(operation.get("selection") or {"mode": "auto"})
    mode = _require_choice(selection.get("mode", "auto"), where + ".selection.mode", ("auto", "faces"))
    if mode == "faces":
        faces = selection.get("faces")
        if not isinstance(faces, list) or not 1 <= len(faces) <= 200 or any(
                not isinstance(face, str) or not re.fullmatch(r"Face[0-9]{1,5}", face) for face in faces):
            _fail(where + ".selection.faces", "noms FaceN (1 à 200) attendus.")
    if kind in ("drilling", "helix", "thread_milling"):
        low_default = (tool["diameter"] + 0.1 / factor) if kind in ("helix", "thread_milling") else 0.5 / factor
        low = _require_number(selection.get("min_diameter", low_default), where + ".selection.min_diameter", 0.001 / factor)
        high = _require_number(selection.get("max_diameter", 100.0 / factor), where + ".selection.max_diameter", 0.001 / factor)
        if low > high:
            _fail(where + ".selection", "diamètre minimal supérieur au maximal.")
        if kind in ("helix", "thread_milling") and low <= tool["diameter"]:
            _fail(where + ".selection.min_diameter", "le diamètre minimal des trous doit dépasser le diamètre de l'outil.")
        selection.update(min_diameter=low, max_diameter=high)
    operation["selection"] = selection
    depth = dict(operation.get("depth") or {})
    if kind == "deburr":
        # FreeCAD 1.1.3 : Deburr n'a pas de FinalDepth (profondeur = ExtraDepth sous l'arête) ; depth.final reste null.
        if depth.get("final") is not None:
            _fail(where + ".depth.final", "l'ébavurage n'a pas de profondeur finale (elle vient de extra_depth) : laisse null.")
        step_down = depth.get("step_down")
        operation["depth"] = {"start": depth.get("start"), "final": None,
                              "step_down": tool["step_down"] if step_down is None else _require_number(step_down, where + ".depth.step_down", 0.001 / factor),
                              "finish": 0.0}
        final = float(operation.get("extra_depth") if _num(operation.get("extra_depth")) else 0.5 / factor)
    else:
        if depth.get("final") is None:
            _fail(where + ".depth.final", "profondeur finale obligatoire.")
        final = _require_number(depth["final"], where + ".depth.final", 0.001 / factor)
        if final > tool["flute_length"] + 1e-9:
            _fail(where + ".depth.final", "profondeur (%g) supérieure à la longueur taillée de l'outil n°%d (%g)."
                  % (final, tool["number"], tool["flute_length"]))
        start = depth.get("start")
        if start is not None and _require_number(start, where + ".depth.start", 0.0) >= final:
            _fail(where + ".depth.start", "la profondeur de départ doit être au-dessus de la profondeur finale.")
        step_down = depth.get("step_down")
        step_down = min(tool["step_down"], final) if step_down is None else _require_number(step_down, where + ".depth.step_down", 0.001 / factor)
        if step_down > final + 1e-9 or step_down > tool["flute_length"] + 1e-9:
            _fail(where + ".depth.step_down", "profondeur de passe supérieure à la profondeur finale ou à la longueur taillée.")
        finish = _require_number(depth.get("finish") if depth.get("finish") is not None else 0.0, where + ".depth.finish", 0.0, 50.0 / factor)
        if finish and kind not in ("facing", "pocket"):
            _fail(where + ".depth.finish", "passe de finition non prise en charge par FreeCAD 1.1.3 hors surfaçage et poche.")
        operation["depth"] = {"start": start, "final": final, "step_down": step_down, "finish": finish}
    op_heights = operation.get("heights")
    if op_heights is not None:
        for key in ("clearance", "safe"):
            _require_number(op_heights.get(key), "%s.heights.%s" % (where, key), 0.001 / factor)
        if op_heights["clearance"] < op_heights["safe"] or op_heights["safe"] * factor + 1e-9 < machine["safe_height_min"]:
            _fail(where + ".heights", "dégagement ≥ sécurité ≥ minimum de la machine attendu.")
    operation["heights"] = op_heights
    stepover = operation.get("stepover_pct")
    if kind in STEPOVER_TYPES:
        operation["stepover_pct"] = STEPOVER_DEFAULTS.get(kind, tool["stepover_pct"]) if stepover is None else \
            _require_number(stepover, where + ".stepover_pct", 1.0, 100.0)
    elif stepover is not None:
        _fail(where + ".stepover_pct", "recouvrement sans effet pour « %s » (doit rester null)." % kind)
    else:
        operation["stepover_pct"] = None
    leave = dict(operation.get("stock_to_leave") or {"radial": 0, "axial": 0})
    radial = _require_number(leave.get("radial", 0), where + ".stock_to_leave.radial", 0.0, 50.0 / factor)
    axial = _require_number(leave.get("axial", 0), where + ".stock_to_leave.axial", 0.0, 50.0 / factor)
    if radial and kind not in ("profile", "pocket", "facing", "adaptive"):
        _fail(where + ".stock_to_leave.radial", "surépaisseur radiale non prise en charge pour « %s »." % kind)
    if axial and kind != "adaptive":
        _fail(where + ".stock_to_leave.axial", "surépaisseur axiale prise en charge seulement par l'ébauche adaptative.")
    operation["stock_to_leave"] = {"radial": radial, "axial": axial}
    tolerance = operation.get("tolerance")
    if kind in TOLERANCE_DEFAULTS:
        operation["tolerance"] = TOLERANCE_DEFAULTS[kind] / factor if tolerance is None else \
            _require_number(tolerance, where + ".tolerance", 0.0001 / factor, 1.0 / factor)
    elif tolerance is not None:
        _fail(where + ".tolerance", "tolérance par opération sans effet pour « %s » : heights.tolerance s'applique." % kind)
    else:
        operation["tolerance"] = None
    direction = operation.get("direction") or "climb"
    _require_choice(direction, where + ".direction", ("climb", "conventional"))
    if direction != "climb" and kind not in ("facing", "pocket", "profile"):
        _fail(where + ".direction", "aucun mode de coupe pour « %s » en FreeCAD 1.1.3 (doit rester climb)." % kind)
    operation["direction"] = direction
    for key, allowed in (("keep_tool_down", ("facing", "pocket", "drilling")), ("min_travel", ("facing", "pocket"))):
        value = operation.get(key, False)
        _require_bool(value, "%s.%s" % (where, key))
        if value and kind not in allowed:
            _fail("%s.%s" % (where, key), "sans effet pour « %s » (doit rester false)." % kind)
        operation[key] = value
    operation["dressups"] = [_normalize_dressup(item, "%s.dressups[%d]" % (where, position), kind, factor, operation, final)
                             for position, item in enumerate(operation.get("dressups") or [])]
    if len(operation["dressups"]) > 8:
        _fail(where + ".dressups", "8 dressups au plus.")
    _check_type_settings(operation, where, kind, factor, tool, final)
    return operation


def _check_type_settings(operation, where, kind, factor, tool, final):
    if kind == "drilling":
        peck = _require_number(operation["peck"], where + ".peck", 0.0)
        dwell = _require_number(operation["dwell"], where + ".dwell", 0.0, 60.0)
        if peck > final + 1e-9:
            _fail(where + ".peck", "débourrage plus profond que le perçage.")
        if peck > 0 and dwell > 0:
            _fail(where + ".dwell", "FreeCAD refuse « Peck and Dwell » ensemble : garde l'un des deux.")
        if operation["retract_height"] is not None:
            _require_number(operation["retract_height"], where + ".retract_height", 0.0, 500.0 / factor)
    elif kind == "profile":
        operation["passes"] = int(_require_number(operation["passes"], where + ".passes", 1, 20))
        if operation["pass_stepover"] is not None:
            if _require_number(operation["pass_stepover"], where + ".pass_stepover", 0.001 / factor) > tool["diameter"] + 1e-9:
                _fail(where + ".pass_stepover", "pas radial supérieur au diamètre de l'outil.")
        for key in ("use_comp", "process_perimeter", "process_holes", "process_circles"):
            _require_bool(operation[key], "%s.%s" % (where, key))
    elif kind == "adaptive":
        _require_number(operation["helix_angle"], where + ".helix_angle", 1.0, 89.0)
        _require_number(operation["helix_cone_angle"], where + ".helix_cone_angle", 0.0, 89.0)
        _require_number(operation["keep_tool_down_ratio"], where + ".keep_tool_down_ratio", 0.0, 10.0 / factor)
    elif kind == "facing":
        _require_number(operation["zigzag_angle"], where + ".zigzag_angle", 0.0, 180.0)
    elif kind == "pocket":
        _require_number(operation["zigzag_angle"], where + ".zigzag_angle", 0.0, 180.0)
    elif kind in ("surface", "waterline"):
        if list(operation["requires"]) != ["ocl"]:
            _fail(where + ".requires", "liste fixe [\"ocl\"] attendue.")
        _require_number(operation["sample_interval"], where + ".sample_interval", 0.01 / factor, 10.0 / factor)
    elif kind == "deburr":
        _require_number(operation["width"], where + ".width", 0.01 / factor, 20.0 / factor)
        _require_number(operation["extra_depth"], where + ".extra_depth", 0.0, 10.0 / factor)
    elif kind == "thread_milling":
        thread = operation.get("thread")
        if not isinstance(thread, dict):
            _fail(where + ".thread", "bloc thread obligatoire : sans lui FreeCAD produit des G3 faux (diamètre mineur 0).")
        thread = dict(thread)
        _require_choice(thread.get("type"), where + ".thread.type", tuple(THREAD_TYPES))
        if not isinstance(thread.get("name"), str) or not thread["name"].strip():
            _fail(where + ".thread.name", "nom du filet obligatoire (ex. M8x1.25).")
        major = _require_number(thread.get("major_diameter"), where + ".thread.major_diameter", 0.5 / factor, 200.0 / factor)
        minor = _require_number(thread.get("minor_diameter"), where + ".thread.minor_diameter", 0.5 / factor, 200.0 / factor)
        if minor >= major:
            _fail(where + ".thread.minor_diameter", "diamètre mineur ≥ diamètre majeur.")
        if tool["diameter"] >= minor:
            _fail(where + ".thread.minor_diameter", "l'outil (Ø %g) ne passe pas dans le diamètre mineur (%g)." % (tool["diameter"], minor))
        if (thread.get("pitch") is None) == (thread.get("tpi") is None):
            _fail(where + ".thread.pitch", "exactement l'un de pitch (métrique) ou tpi (impérial) attendu.")
        if thread.get("pitch") is not None:
            _require_number(thread["pitch"], where + ".thread.pitch", 0.1 / factor, 10.0 / factor)
        else:
            thread["tpi"] = int(_require_number(thread["tpi"], where + ".thread.tpi", 1, 100))
        thread["fit"] = _require_number(thread.get("fit", 50), where + ".thread.fit", 0.0, 100.0)
        operation["thread"] = thread
        operation["passes"] = int(_require_number(operation["passes"], where + ".passes", 1, 10))


def _normalize_dressup(item, where, kind, factor, operation, final):
    if not isinstance(item, dict):
        _fail(where, "objet attendu.")
    item = dict(item)
    dressup = item.get("type")
    if dressup in DRESSUP_REFUSED:
        _fail(where + ".type", "dressup refusé : " + DRESSUP_REFUSED[dressup])
    _require_choice(dressup, where + ".type", DRESSUP_TYPES)
    if kind not in DRESSUP_OPERATIONS[dressup]:
        _fail(where + ".type", "« %s » ne s'applique pas à l'opération « %s »." % (dressup, kind))
    if dressup == "tags":
        if kind == "adaptive" and operation.get("operation_type") != "profiling":
            _fail(where + ".type", "les ponts sur une ébauche adaptative exigent operation_type = profiling.")
        if kind == "profile" and int(operation.get("passes") or 1) > 1:
            _fail(where + ".type", "FreeCAD 1.1.3 ne place aucun pont sur un contour à plusieurs passes radiales (mesuré : 0 position) : "
                                   "passes = 1 avec des ponts, ou pas de ponts.")
        count = int(_require_number(item.get("count", 4), where + ".count", 1, 16))
        width = _require_number(item.get("width", 6.0 / factor), where + ".width", 0.001 / factor)
        height = _require_number(item.get("height", 2.0 / factor), where + ".height", 0.001 / factor)
        if height >= final:
            _fail(where + ".height", "pont plus haut que la profondeur de coupe.")
        return {"type": "tags", "count": count, "width": width, "height": height,
                "angle": _require_number(item.get("angle", 45), where + ".angle", 0.0, 90.0),
                "radius": _require_number(item.get("radius", 0), where + ".radius", 0.0, 50.0 / factor),
                "segmentation": _require_number(item.get("segmentation", 0), where + ".segmentation", 0.0, 1000.0)}
    if dressup == "dogbone":
        if kind == "profile" and operation.get("side") != "inside":
            _fail(where + ".type", "les dogbones sur un contour exigent side = inside.")
        clean = {"type": "dogbone", "style": _require_choice(item.get("style", "dogbone"), where + ".style", tuple(DOGBONE_STYLES)),
                 "incision": _require_choice(item.get("incision", "adaptive"), where + ".incision", ("adaptive", "fixed", "custom")),
                 "side": _require_choice(item.get("side", "left"), where + ".side", ("left", "right")), "custom_length": None}
        if clean["incision"] == "custom":
            clean["custom_length"] = _require_number(item.get("custom_length"), where + ".custom_length", 0.001 / factor)
        return clean
    return {"type": "boundary", "inside": _require_bool(item.get("inside", True), where + ".inside")}


def validate_custom_motion_line(line, path, factor=1.0):
    """Ligne d'un bloc personnalisé : mots G0 G1 G4 G17 G90 G94 M0 M1 M3 M4 M5 M7 M8 M9, X Y Z F S P bornés, commentaires.

    Rend la ligne normalisée en mm / mm/min (coordonnées et avance converties si le projet est en pouces).
    """
    if not isinstance(line, str):
        _fail(path, "texte attendu.")
    text = line.strip()
    if not text or len(text) > 80:
        _fail(path, "ligne vide ou trop longue (80 caractères au plus).")
    words, position, upper = [], 0, text.upper()
    while position < len(text):
        char = text[position]
        if char == " ":
            position += 1
            continue
        if char == "(":
            end = text.find(")", position)
            if end < 0 or "(" in text[position + 1:end]:
                _fail(path, "commentaire invalide : « ( … ) » sur une ligne, sans parenthèse imbriquée.")
            words.append("(%s)" % comment_text(text[position + 1:end]))
            position = end + 1
            continue
        match = re.match(r"([A-Z])\s*([-+]?[0-9]+(?:\.[0-9]+)?)", upper[position:])
        if not match:
            _fail(path, "texte libre refusé près de « %s »." % text[position:position + 12])
        letter, number = match.group(1), match.group(2)
        if letter in ("G", "M"):
            word = "%s%d" % (letter, int(float(number)))
            if word not in CUSTOM_WORDS or "." in number:
                _fail(path, "commande « %s%s » non autorisée dans un bloc personnalisé." % (letter, number))
            words.append(word)
        elif letter in ("X", "Y", "Z"):
            value = float(number) * factor
            if abs(value) > 3000.0:
                _fail(path, "coordonnée hors limites.")
            words.append("%s%s" % (letter, ("%.4f" % value).rstrip("0").rstrip(".")))
        elif letter == "F":
            value = float(number) * factor
            if not 1.0 <= value <= 50000.0:
                _fail(path, "avance F hors limites.")
            words.append("F%s" % ("%.3f" % value).rstrip("0").rstrip("."))
        elif letter == "S":
            if not 0.0 <= float(number) <= 100000.0:
                _fail(path, "vitesse S hors limites.")
            words.append("S%s" % number)
        elif letter == "P":
            if not 0.0 <= float(number) <= 60.0:
                _fail(path, "temporisation P hors limites.")
            words.append("P%s" % number)
        else:
            _fail(path, "lettre « %s » non autorisée (G, M, X, Y, Z, F, S, P seulement)." % letter)
        position += match.end()
    return " ".join(words)


# ---------------------------------------------------------------------------------------------
# Plan en mm
# ---------------------------------------------------------------------------------------------
def tool_in_mm(tool, factor):
    scaled = dict(tool)
    for key in ("diameter", "flute_length", "length", "shank_diameter", "feed_xy", "feed_plunge", "feed_ramp", "step_down",
                "stickout", "holder_diameter", "holder_length", "chip_load", "corner_radius", "tip_diameter", "crest",
                "neck_diameter", "neck_length"):
        if scaled.get(key) is not None:
            scaled[key] = float(scaled[key]) * factor
    scaled["shape"] = FAMILY_SHAPES[tool["family"]]
    return scaled


def tools_mm(project):
    factor = project_factor(project)
    return {tool["number"]: tool_in_mm(tool, factor) for tool in project["tools"]}


def controller_key(entry, tool):
    """Un contrôleur d'outil FreeCAD par combinaison (outil, avances, broche, sens)."""
    return (tool["number"], round(entry.get("feed_xy") or tool["feed_xy"], 4), round(entry.get("feed_plunge") or tool["feed_plunge"], 4),
            round(entry.get("spindle_rpm") or tool["spindle_rpm"], 1), entry.get("spindle_dir", "cw"))


def plan(project):
    """(outils en mm, séquence en mm dans l'ordre du projet : usinages activés, blocs, pauses, consignes, changements)."""
    factor = project_factor(project)
    tools = tools_mm(project)
    entries = []
    for operation in project["operations"]:
        if not operation["enabled"]:
            continue
        kind = operation["type"]
        entry = {"id": operation["id"], "type": kind, "name": operation["name"], "note": operation.get("operator_note") or "",
                 "pause_before": operation.get("pause_before", False)}
        if kind == "pause":
            entry["stop_spindle"] = operation.get("stop_spindle", True)
        elif kind == "tool_change":
            entry["tool"] = operation["tool"]
            entry["go_to_change_position"] = operation.get("go_to_change_position", False)
        elif kind == "note":
            pass
        else:
            tool = tools[operation["tool"]]
            entry.update({"tool": tool["number"], "spindle_rpm": operation.get("spindle_rpm"), "spindle_dir": operation.get("spindle_dir", "cw"),
                          "coolant": operation.get("coolant", "off"),
                          "feed_xy": (operation.get("feed_xy") or 0.0) * factor or None,
                          "feed_plunge": (operation.get("feed_plunge") or 0.0) * factor or None,
                          "feed_ramp": (operation.get("feed_ramp") or 0.0) * factor or None})
            entry["controller"] = controller_key(entry, tool)
            if kind == "custom":
                entry["gcode_lines"] = list(operation["gcode_lines"])
            else:
                depth = operation["depth"]
                entry.update({"final": None if depth["final"] is None else depth["final"] * factor, "step_down": depth["step_down"] * factor,
                              "start": None if depth["start"] is None else depth["start"] * factor, "finish": depth["finish"] * factor,
                              "stepover_pct": operation["stepover_pct"], "direction": operation["direction"],
                              "stock_to_leave": {key: value * factor for key, value in operation["stock_to_leave"].items()},
                              "tolerance": None if operation["tolerance"] is None else operation["tolerance"] * factor,
                              "keep_tool_down": operation["keep_tool_down"], "min_travel": operation["min_travel"],
                              "heights": None if operation["heights"] is None else {key: value * factor for key, value in operation["heights"].items()},
                              "selection": _selection_mm(operation["selection"], factor),
                              "settings": _settings_mm(operation, kind, factor),
                              "dressups": [_dressup_mm(item, factor) for item in operation["dressups"]],
                              "engine_shape": engine_tool_shape(kind, tool["family"])})
        entries.append(entry)
    return tools, entries


operations_plan = plan   # nom utilisé par CNC Studio (cnc_sequences.py) : signature et tuples stables


def _selection_mm(selection, factor):
    clean = dict(selection)
    for key in ("min_diameter", "max_diameter"):
        if clean.get(key) is not None:
            clean[key] = clean[key] * factor
    return clean


def _settings_mm(operation, kind, factor):
    settings = {}
    for key in TYPE_SETTINGS[kind]:
        value = operation.get(key)
        if key in LENGTH_SETTINGS and _num(value):
            value = value * factor
        settings[key] = copy.deepcopy(value)
    if kind == "thread_milling":
        thread = dict(settings["thread"])
        for key in ("major_diameter", "minor_diameter", "pitch"):
            if thread.get(key) is not None:
                thread[key] = thread[key] * factor
        settings["thread"] = thread
    return settings


def _dressup_mm(item, factor):
    clean = dict(item)
    for key in ("width", "height", "radius", "custom_length"):
        if clean.get(key) is not None:
            clean[key] = clean[key] * factor
    return clean


# ---------------------------------------------------------------------------------------------
# Correspondance exacte vers FreeCAD
# ---------------------------------------------------------------------------------------------
def _mm(value):
    return "%r mm" % float(value)


def tool_properties(tool):
    """Propriétés du ToolBit à poser APRÈS ``attach_to_doc`` (audit §4 : avant, elles sont écrasées). ``tool`` en mm."""
    family = tool["family"]
    props = [("Diameter", _mm(tool["diameter"])), ("Length", _mm(tool["length"])),
             ("Material", "HSS" if tool.get("material") == "hss" else "Carbide")]
    if tool.get("chip_load"):
        props.append(("Chipload", _mm(tool["chip_load"])))
    if family in ("endmill", "ballend", "bullnose"):
        props += [("CuttingEdgeHeight", _mm(tool["flute_length"])), ("ShankDiameter", _mm(tool["shank_diameter"])), ("Flutes", int(tool["flutes"]))]
        if family == "bullnose":
            props.append(("CornerRadius", _mm(tool["corner_radius"])))
    elif family == "drill":
        props += [("TipAngle", "%r deg" % float(tool["tip_angle"])), ("Flutes", int(tool["flutes"]))]
    elif family in ANGLE_FAMILIES:
        props += [("CuttingEdgeAngle", "%r deg" % float(tool["cutting_edge_angle"])), ("TipDiameter", _mm(tool["tip_diameter"])),
                  ("CuttingEdgeHeight", _mm(tool["flute_length"]))]
    elif family == "threadmill":
        props += [("Crest", _mm(tool["crest"])), ("NeckDiameter", _mm(tool["neck_diameter"])), ("NeckLength", _mm(tool["neck_length"])),
                  ("cuttingAngle", "%r deg" % float(tool["cutting_edge_angle"]))]
    return props


def climb_direction(side, spindle_dir="cw"):
    """Sens d'un contour en avalant (M3) : extérieur horaire, intérieur antihoraire."""
    clockwise = side == "outside"
    if spindle_dir == "ccw":
        clockwise = not clockwise
    return "CW" if clockwise else "CCW"


def operation_properties(entry, tool, heights, stock_top):
    """Liste ordonnée (propriété FreeCAD, valeur) d'une opération d'usinage — le contrat §6 ligne à ligne.

    ``heights`` : hauteurs du projet en mm ; ``stock_top`` : Z absolu du dessus du brut (repère pièce).
    """
    kind = entry["type"]
    own = entry.get("heights") or heights
    props = [("ClearanceHeight", stock_top + own["clearance"]), ("SafeHeight", stock_top + own["safe"])]
    settings = entry["settings"]
    leave = entry["stock_to_leave"]
    if kind == "facing":
        props += [("BoundaryShape", BOUNDARY_FREECAD[settings["boundary"]]), ("ClearingPattern", PATTERNS_FREECAD[settings["pattern"]]),
                  ("ZigZagAngle", float(settings["zigzag_angle"])), ("ClearEdges", bool(settings["clear_edges"])),
                  ("StartAt", settings["start_at"].capitalize()), ("ExcludeRaisedAreas", bool(settings["exclude_raised"])),
                  ("UseRestMachining", bool(settings["rest_machining"])), ("StepOver", int(round(entry["stepover_pct"]))),
                  ("CutMode", "Climb" if entry["direction"] == "climb" else "Conventional"), ("FinishDepth", entry["finish"]),
                  ("ExtraOffset", leave["radial"]), ("KeepToolDown", entry["keep_tool_down"]), ("MinTravel", entry["min_travel"])]
    elif kind == "pocket":
        props += [("ClearingPattern", PATTERNS_FREECAD[settings["pattern"]]), ("ZigZagAngle", float(settings["zigzag_angle"])),
                  ("StartAt", settings["start_at"].capitalize()), ("UseRestMachining", bool(settings["rest_machining"])),
                  ("StepOver", int(round(entry["stepover_pct"]))), ("CutMode", "Climb" if entry["direction"] == "climb" else "Conventional"),
                  ("FinishDepth", entry["finish"]), ("ExtraOffset", leave["radial"]), ("KeepToolDown", entry["keep_tool_down"]),
                  ("MinTravel", entry["min_travel"])]
    elif kind == "profile":
        direction = climb_direction(settings["side"], entry["spindle_dir"])
        if entry["direction"] == "conventional":
            direction = "CCW" if direction == "CW" else "CW"
        props += [("Side", "Outside" if settings["side"] == "outside" else "Inside"), ("UseComp", bool(settings["use_comp"])),
                  ("Direction", direction), ("NumPasses", int(settings["passes"])),
                  ("JoinType", settings["join_type"].capitalize()), ("processPerimeter", bool(settings["process_perimeter"])),
                  ("processHoles", bool(settings["process_holes"])), ("processCircles", bool(settings["process_circles"])),
                  ("HandleMultipleFeatures", settings["handle_multiple"].capitalize()), ("OffsetExtra", leave["radial"])]
        if settings["passes"] > 1:
            props.append(("Stepover", settings["pass_stepover"] if settings["pass_stepover"] else tool["diameter"] * entry["stepover_pct"] / 100.0))
    elif kind == "drilling":
        props += [("PeckEnabled", settings["peck"] > 0), ("DwellEnabled", settings["dwell"] > 0), ("RetractMode", settings["retract_mode"]),
                  ("ExtraOffset", {"none": "None", "drill_tip": "Drill Tip", "2x_drill_tip": "2x Drill Tip"}[settings["extra_offset"]]),
                  ("KeepToolDown", entry["keep_tool_down"]),
                  ("RetractHeight", stock_top + (settings["retract_height"] if settings["retract_height"] is not None else heights["retract"]))]
        if settings["peck"] > 0:
            props.append(("PeckDepth", settings["peck"]))
        if settings["dwell"] > 0:
            props.append(("DwellTime", float(settings["dwell"])))
    elif kind == "adaptive":
        props += [("OperationType", settings["operation_type"].capitalize()), ("Side", settings["side"].capitalize()),
                  ("StepOver", int(round(entry["stepover_pct"]))), ("Tolerance", float(entry["tolerance"])),
                  ("HelixAngle", float(settings["helix_angle"])), ("HelixConeAngle", float(settings["helix_cone_angle"])),
                  ("HelixDiameterLimit", settings["helix_diameter_limit"]), ("UseHelixArcs", bool(settings["use_helix_arcs"])),
                  ("LiftDistance", settings["lift_distance"]), ("KeepToolDownRatio", settings["keep_tool_down_ratio"]),
                  ("StockToLeave", leave["radial"]), ("ZStockToLeave", leave["axial"]),
                  ("FinishingProfile", bool(settings["finishing_profile"])), ("ForceInsideOut", bool(settings["force_inside_out"])),
                  ("OrderCutsByRegion", bool(settings["order_by_region"]))]
    elif kind == "engrave":
        props.append(("StartVertex", int(settings["start_vertex"])))
    elif kind == "vcarve":
        props += [("Discretize", float(settings["discretize"])), ("Colinear", float(settings["colinear"])), ("Tolerance", float(entry["tolerance"])),
                  ("FinishingPass", bool(settings["finishing_pass"])), ("FinishingPassZOffset", float(settings["finishing_pass_z_offset"])),
                  ("OptimizeMovements", bool(settings["optimize_movements"]))]
    elif kind == "helix":
        props += [("StartSide", settings["start_side"].capitalize()), ("StartRadius", settings["start_radius"]),
                  ("Direction", settings["path_direction"].upper()), ("StepOver", int(round(entry["stepover_pct"])))]
    elif kind == "slot":
        props += [("CutPattern", "Line" if settings["pattern"] == "line" else "ZigZag"),
                  ("LayerMode", "Single-pass" if settings["layer_mode"] == "single" else "Multi-pass"),
                  ("ExtendPathStart", settings["extend_start"]), ("ExtendPathEnd", settings["extend_end"]),
                  ("ReverseDirection", bool(settings["reverse"]))]
    elif kind == "surface":
        props += [("BoundBox", "Stock" if settings["bound_box"] == "stock" else "BaseBoundBox"), ("ScanType", "Planar"),
                  ("CutPattern", PATTERNS_FREECAD[settings["pattern"]]), ("CutPatternAngle", float(settings["pattern_angle"])),
                  ("LayerMode", "Single-pass" if settings["layer_mode"] == "single" else "Multi-pass"), ("StepOver", float(entry["stepover_pct"])),
                  ("SampleInterval", settings["sample_interval"]), ("DropCutterDir", settings["drop_cutter_dir"].upper()),
                  ("ProfileEdges", settings["profile_edges"].capitalize()), ("BoundaryEnforcement", bool(settings["boundary_enforcement"])),
                  ("BoundaryAdjustment", settings["boundary_adjustment"]), ("InternalFeaturesCut", bool(settings["internal_features_cut"])),
                  ("OptimizeLinearPaths", bool(settings["optimize_linear_paths"])),
                  ("OptimizeStepOverTransitions", bool(settings["optimize_stepover_transitions"])),
                  ("CircularUseG2G3", bool(settings["circular_use_g2g3"])), ("LinearDeflection", settings["linear_deflection"])]
    elif kind == "waterline":
        props += [("Algorithm", "OCL Dropcutter"), ("BoundBox", "Stock"), ("LayerMode", "Multi-pass"),
                  ("ClearLastLayer", PATTERNS_FREECAD[settings["pattern"]] if settings["clear_last_layer"] else "Off"),
                  ("CutPattern", PATTERNS_FREECAD[settings["pattern"]]), ("IgnoreOuterAbove", settings["ignore_outer_above"]),
                  ("SampleInterval", settings["sample_interval"]), ("StepOver", float(entry["stepover_pct"])),
                  ("BoundaryEnforcement", bool(settings["boundary_enforcement"])), ("BoundaryAdjustment", settings["boundary_adjustment"]),
                  ("InternalFeaturesCut", bool(settings["internal_features_cut"]))]
    elif kind == "deburr":
        props += [("Width", settings["width"]), ("ExtraDepth", settings["extra_depth"]), ("Join", settings["join"].capitalize()),
                  ("Direction", settings["path_direction"].upper()), ("Side", settings["side"].capitalize()), ("EntryPoint", int(settings["entry_point"]))]
    elif kind == "thread_milling":
        thread = settings["thread"]
        props += [("ThreadType", THREAD_TYPES[thread["type"]]), ("ThreadName", str(thread["name"])[:40]),
                  ("MajorDiameter", thread["major_diameter"]), ("MinorDiameter", thread["minor_diameter"]),
                  ("ThreadFit", int(round(thread["fit"]))), ("Passes", int(settings["passes"])),
                  ("ThreadOrientation", "RightHand" if settings["orientation"] == "right" else "LeftHand"),
                  ("Direction", "Climb" if settings["path_direction"] == "cw" else "Conventional"), ("LeadInOut", bool(settings["lead_in_out"]))]
        if thread.get("pitch") is not None:
            props.append(("Pitch", thread["pitch"]))
        else:
            props.append(("TPI", int(thread["tpi"])))
    props.append(("CoolantMode", COOLANT_FREECAD.get(entry["coolant"], "None")))
    return props


def dressup_properties(dressup):
    """(module, propriétés, nombre de ponts) d'un dressup normalisé en mm."""
    kind = dressup["type"]
    if kind == "tags":
        return DRESSUP_MODULES[kind], [("Width", dressup["width"]), ("Height", dressup["height"]), ("Angle", float(dressup["angle"])),
                                       ("Radius", dressup["radius"]), ("SegmentationFactor", int(round(dressup["segmentation"])))], int(dressup["count"])
    if kind == "dogbone":
        props = [("Style", DOGBONE_STYLES[dressup["style"]]), ("Incision", dressup["incision"]), ("Side", dressup["side"].capitalize())]
        if dressup["incision"] == "custom":
            props.append(("Custom", dressup["custom_length"]))
        return DRESSUP_MODULES[kind], props, 0
    return DRESSUP_MODULES["boundary"], [("Inside", bool(dressup["inside"]))], 0


# ---------------------------------------------------------------------------------------------
# Séquence : pauses, consignes, changements d'outil, programmes par outil
# ---------------------------------------------------------------------------------------------
def _spindle_on(entry, tools):
    rpm = entry.get("spindle_rpm") or tools[entry["tool"]]["spindle_rpm"]
    return ("M4" if entry.get("spindle_dir") == "ccw" else "M3") + " S%d" % int(round(rpm))


def tool_change_message(tool, position=None):
    text = "Changement d'outil : monter T%d %s" % (tool["number"], comment_text(tool["name"]))
    if position:
        text += " - position de changement X%.1f Y%.1f Z%.1f" % (position["x"], position["y"], position["z"])
    return text


def sequence_steps(entries, tools, policy="pause_m0", manual=True, change_position=None):
    """Blocs à poser dans le Job : [("custom", libellé, lignes, contrôleur, entrée|None, programme) | ("op", entrée, programme)].

    - ``pause_m0`` (v1) : chaque changement d'outil devient « M5, consigne, M0 » (même sans étape explicite sur une
      machine manuelle) ; une pause arrête la broche (M5) avant M0 et la relance quand l'outil ne change pas ;
    - ``split_files`` : un changement d'outil ferme le programme courant, le suivant commence un nouveau fichier ;
      aucun M0 de changement (la consigne vit dans le manifeste), les pauses restent des M0 dans leur fichier ;
    - ``atc_m6`` : aucun bloc d'arrêt pour les changements (le post émet M6 Tn).
    ``change_position`` : position vérifiée ({x, y, z}) ajoutée au TEXTE de la consigne seulement (jamais un déplacement).
    """
    steps, previous, explicit_change, program = [], None, False, 0
    split = policy == "split_files"
    for position, entry in enumerate(entries):
        following = next((item for item in entries[position + 1:] if "controller" in item), None)
        kind = entry["type"]
        if kind in ("note", "pause", "tool_change"):
            text = comment_text(entry["note"] or entry["name"])
            anchor = previous or following
            key = anchor["controller"] if anchor else None
            if kind == "note":
                lines = ["(%s)" % text]
            elif kind == "pause":
                stop = entry.get("stop_spindle", True)
                lines = ["(%s)" % text, "M0"]
                if stop and anchor:
                    lines = ["M5"] + lines + [_spindle_on(anchor, tools)]
            else:
                tool = tools.get(entry["tool"], {"number": entry["tool"], "name": ""})
                label = tool_change_message(tool, change_position if entry.get("go_to_change_position") else None)
                if split:
                    if previous is not None and previous["tool"] != entry["tool"]:
                        program += 1
                    lines = ["(%s)" % label]
                    if following and following["tool"] == entry["tool"]:
                        key = following["controller"]
                elif manual and policy == "pause_m0":
                    lines = ["M5", "(%s)" % label, "M0"] if anchor else ["(%s)" % label, "M0"]
                    restart = following if (previous is None or (following and following["tool"] == previous["tool"])) else None
                    if anchor and restart:
                        lines.append(_spindle_on(restart, tools))
                else:
                    lines = ["(%s)" % label]
                explicit_change = True
            steps.append(("custom", entry["name"], lines, key, entry, program))
            continue
        changing = previous is not None and previous["tool"] != entry["tool"]
        if split and changing and not explicit_change:
            program += 1
        automatic = manual and changing and not explicit_change and policy == "pause_m0"
        if automatic or entry["pause_before"]:
            lines = ["M5"]
            if automatic:
                lines.append("(%s)" % tool_change_message(tools[entry["tool"]]))
            if entry["pause_before"]:
                lines.append("(Pause avant %s)" % comment_text(entry["name"]))
            lines.append("M0")
            if previous is None or not changing or split:
                lines.append(_spindle_on(entry, tools))
            key = previous["controller"] if previous and not (split and changing) else entry["controller"]
            steps.append(("custom", "Arrêt avant " + entry["name"], lines, key, None, program))
        steps.append(("op", entry, program))
        previous, explicit_change = entry, False
    return steps


def program_runs(steps):
    """Programmes dans l'ordre : [{index, tool, operations, steps}] (un seul sans découpe)."""
    runs = []
    for step in steps:
        program = step[-1]
        while len(runs) <= program:
            runs.append({"index": len(runs), "tool": None, "operations": [], "tool_numbers": []})
        run = runs[program]
        if step[0] == "op":
            entry = step[1]
            run["operations"].append(entry["id"])
            if entry.get("tool") is not None and entry["tool"] not in run["tool_numbers"]:
                run["tool_numbers"].append(entry["tool"])
            if run["tool"] is None:
                run["tool"] = entry.get("tool")
        elif step[4] is not None:
            run["operations"].append(step[4]["id"])
    return [run for run in runs if run["operations"]]


def program_file_names(runs, stem, extension):
    """Noms des fichiers G-code : ``<stem>.<ext>`` seul, ``<stem>_T<n>.<ext>`` par outil (suffixe -2… si un outil revient)."""
    if len(runs) <= 1:
        return ["%s.%s" % (stem, extension)]
    names, seen = [], {}
    for run in runs:
        base = "%s_T%s" % (stem, run["tool"] if run["tool"] is not None else "0")
        seen[base] = seen.get(base, 0) + 1
        names.append("%s%s.%s" % (base, "" if seen[base] == 1 else "-%d" % seen[base], extension))
    return names


def manual_steps(entries, tools, runs, file_names, change_position=None):
    """Étapes manuelles du manifeste (cam_handoff.manual_steps) : pauses, changements d'outil, consignes."""
    steps, previous_tool, index = [], None, 0
    file_of = {}
    for run, name in zip(runs, file_names):
        for identifier in run["operations"]:
            file_of[identifier] = name
    for position, entry in enumerate(entries):
        kind = entry["type"]
        if kind in ("pause", "note", "tool_change"):
            index += 1
            step = {"index": index, "kind": kind, "operation_id": entry["id"], "message": entry["note"] or entry["name"],
                    "tool_from": previous_tool, "tool_to": entry.get("tool") if kind == "tool_change" else previous_tool,
                    "stop_spindle": entry.get("stop_spindle", True) if kind == "pause" else True,
                    "position": dict(change_position) if (kind == "tool_change" and entry.get("go_to_change_position") and change_position) else None,
                    "file": file_of.get(entry["id"])}
            if kind == "tool_change":
                tool = tools.get(entry["tool"])
                step["message"] = (entry["note"] + " - " if entry["note"] else "") + tool_change_message(tool, step["position"])
                previous_tool = entry["tool"]
            steps.append(step)
            continue
        if entry.get("tool") is not None and previous_tool is not None and entry["tool"] != previous_tool:
            index += 1
            steps.append({"index": index, "kind": "tool_change", "operation_id": entry["id"], "message": tool_change_message(tools[entry["tool"]]),
                          "tool_from": previous_tool, "tool_to": entry["tool"], "stop_spindle": True, "position": None,
                          "file": file_of.get(entry["id"])})
        if entry.get("tool") is not None:
            previous_tool = entry["tool"]
    return steps


# ---------------------------------------------------------------------------------------------
# Post-processeur
# ---------------------------------------------------------------------------------------------
def post_arguments(post_name, known_arguments, project, policy="pause_m0"):
    """Ligne d'options du post. Seules les options que le post DÉCLARE (parser réel) sont transmises ; ``--no-tlo`` jamais.

    grbl : ``--translate_drill`` explicite (G81/G82/G83 → G0/G1), ``--precision`` posée, ``--wait-for-spindle`` si demandé,
    ``--tool-change`` seulement en ``atc_m6``. Un argument inconnu est refusé AVANT l'export (sinon SystemExit dans freecadcmd).
    """
    known = set(known_arguments or ())
    tokens = []
    if "--no-show-editor" in known:
        tokens.append("--no-show-editor")
    options = dict(project["post"].get("options") or {})
    for key, value in sorted(options.items()):
        flag = "--" + key
        if flag not in known or key in ("no-tlo", "tlo", "show-editor", "tool-change", "tool_change"):
            raise CamOpsError("post.options." + key, "option « %s » inconnue du post-processeur « %s » installé ou non transmissible." % (key, post_name))
        if isinstance(value, bool):
            if value:
                tokens.append(flag)
        else:
            tokens.append("%s=%s" % (flag, value))
    if "precision" not in options and "--precision" in known:
        tokens.append("--precision=3")
    if "--translate_drill" in known and "translate_drill" not in options:
        tokens.append("--translate_drill")
    if project["machine"]["units"] == "inch":
        if "--inches" not in known:
            raise CamOpsError("machine.units", "le post-processeur « %s » ne sait pas produire un programme en pouces." % post_name)
        tokens.append("--inches")
    wait = float(project.get("sequence", {}).get("spindle_wait_s") or 0)
    if wait > 0:
        if "--wait-for-spindle" not in known:
            raise CamOpsError("sequence.spindle_wait_s", "le post-processeur « %s » n'a pas d'option --wait-for-spindle." % post_name)
        tokens.append("--wait-for-spindle=%g" % wait)
    if policy == "atc_m6":
        if "--tool-change" not in known and "--tool_change" not in known:
            raise CamOpsError("sequence.tool_change_policy", "le post-processeur « %s » ne sait pas émettre M6." % post_name)
        tokens.append("--tool-change" if "--tool-change" in known else "--tool_change")
    for block in ("preamble", "postamble"):
        lines = project["post"].get(block) or []
        if not lines:
            continue
        if "--" + block not in known:
            raise CamOpsError("post." + block, "le post-processeur « %s » ne prend pas de %s personnalisé."
                              % (post_name, "préambule" if block == "preamble" else "postambule"))
        tokens.append("--%s=%s" % (block, "\\n".join(lines)))
    import shlex
    return " ".join(shlex.quote(token) for token in tokens)


def _program_words(text):
    """[(ligne, [mots G/M/T/S hors commentaires])] d'un programme."""
    out = []
    for number, raw in enumerate(str(text).splitlines(), start=1):
        clean = re.sub(r"\([^)]*\)", " ", raw).split(";", 1)[0].upper()
        words = []
        for match in re.finditer(r"([A-Z])\s*([-+]?[0-9]*\.?[0-9]+)", clean):
            letter, number_text = match.group(1), match.group(2)
            if letter in "GMTS":
                try:
                    value = float(number_text)
                    words.append("%s%d" % (letter, int(value)) if value.is_integer() else "%s%g" % (letter, value))
                except ValueError:
                    pass
        out.append((number, words))
    return out


def grbl_program_checks(text, machine, policy="pause_m0"):
    """Vérifications propres au programme posté (en plus de cam_gcode_check) : [{code, message, line}].

    - unités (G21, ou G20 si la machine est en pouces), plan G17 et G90 avant le premier mouvement ;
    - broche démarrée (M3/M4 avec S) avant la première coupe, M5 avant la fin ;
    - aucun mot interdit sur GRBL : G43/G49, G84, cycles G81-G83/G73/G98/G99/G80, G41/G42, M6 réel hors ``atc_m6``.
    """
    issues = []
    units = "G20" if machine.get("units") == "inch" else "G21"
    seen = set()
    first_motion = first_cut = None
    spindle_before_cut = speed_seen = False
    last_m5 = last_cut = None
    for line, words in _program_words(text):
        for word in words:
            if word in GRBL_FORBIDDEN or (word == "M6" and policy != "atc_m6"):
                issues.append({"code": "post_forbidden_word", "line": line,
                               "message": "Mot « %s » dans le programme : GRBL 1.1 le refuse (ou l'exécute mal) ; vérifie les options du post." % word})
            if word.startswith("T") and policy != "atc_m6":
                issues.append({"code": "post_tool_word", "line": line, "message": "Mot T hors commentaire sans changeur automatique."})
        motion = any(word in ("G0", "G1", "G2", "G3") for word in words)
        if motion and first_motion is None:
            first_motion = line
            for word, code in ((units, "post_units_missing"), ("G17", "post_plane_missing"), ("G90", "post_distance_missing")):
                if word not in seen:
                    issues.append({"code": code, "line": line, "message": "« %s » absent avant le premier mouvement." % word})
        if any(word.startswith("S") for word in words):
            speed_seen = True
        if "M3" in words or "M4" in words:
            spindle_before_cut = speed_seen or any(word.startswith("S") for word in words)
        if any(word in ("G1", "G2", "G3") for word in words):
            last_cut = line
            if first_cut is None:
                first_cut = line
                if not spindle_before_cut:
                    issues.append({"code": "post_spindle_missing", "line": line, "message": "Première coupe sans M3/M4 S… avant elle."})
        if "M5" in words:
            last_m5 = line
        seen.update(words)
    if last_cut is not None and (last_m5 is None or last_m5 < last_cut):
        issues.append({"code": "post_spindle_not_stopped", "line": last_cut, "message": "Aucun M5 après la dernière coupe."})
    if "M2" not in seen and "M30" not in seen:
        issues.append({"code": "post_end_missing", "line": 0, "message": "Fin de programme (M2/M30) absente."})
    return issues


# ---------------------------------------------------------------------------------------------
# Analyse d'une trajectoire FreeCAD (commandes brutes) : aperçu, longueur, durée estimée
# ---------------------------------------------------------------------------------------------
def segment_duration(length, speed, accel):
    """Durée (s) d'un segment : trapèze si l'accélération est connue, sinon longueur / vitesse. ``speed`` en mm/s."""
    if length <= 0 or speed <= 0:
        return 0.0
    if not accel or accel <= 0:
        return length / speed
    if length >= speed * speed / accel:
        return length / speed + speed / accel
    return 2.0 * math.sqrt(length / accel)


def path_analysis(commands, horiz_feed, vert_feed, rapid_speed, max_accel=None, start=None, points_limit=PREVIEW_POINTS_PER_OP):
    """Analyse pure d'une liste de commandes (nom, paramètres) telle que ``op.Path.Commands`` la donne (mm, absolu).

    Rend ``{"segments", "length_mm": {rapid, cut, plunge}, "estimated_s", "bounds", "polylines": {rapid, cut, plunge}, "truncated"}``.
    Les avances viennent des mots F des commandes FreeCAD (unité interne : mm/s, mesuré 13,333 pour 800 mm/min) ou du
    contrôleur (``horiz_feed`` / ``vert_feed`` en mm/min). La durée est une ESTIMATION (trapèze par segment quand
    ``max_accel`` mm/s² est connu, sans jerk ni look-ahead).
    """
    position = list(start) if start else [None, None, None]
    feed = None
    lengths = {"rapid": 0.0, "cut": 0.0, "plunge": 0.0}
    poly = {"rapid": [], "cut": [], "plunge": []}
    seconds = 0.0
    segments = 0
    canned_r = None
    low, high = [math.inf] * 3, [-math.inf] * 3
    rapid_mm_s = max(float(rapid_speed or 1000.0), 1.0) / 60.0

    def push(kind, a, b, speed_mm_s):
        nonlocal seconds, segments
        length = math.dist(a, b)
        lengths[kind] += length
        seconds += segment_duration(length, speed_mm_s, max_accel)
        if kind != "rapid":
            segments += 1
        for point in (a, b):
            for axis in range(3):
                low[axis] = min(low[axis], point[axis])
                high[axis] = max(high[axis], point[axis])
        line = poly[kind]
        if line and line[-1] == a:
            line.append(b)
        else:
            line.append(None)
            line.append(a)
            line.append(b)

    for name, params in commands:
        word = str(name).upper()
        if word.startswith("("):
            continue
        params = {str(key).upper(): float(value) for key, value in (params or {}).items() if isinstance(value, (int, float))}
        if "F" in params and params["F"] > 0:
            feed = params["F"] * 60.0   # mm/s (interne FreeCAD) → mm/min
        if word in ("G98", "G99", "G80", "G90", "G17", "G21", "G54", "M3", "M4", "M5", "M0", "M1", "G4", "G94"):
            continue
        if word in ("G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03"):
            target = [params.get(axis, position[index]) for index, axis in enumerate("XYZ")]
            if None in target:
                position = target
                continue
            if None in position:
                position = list(target)
                continue
            rapid = word in ("G0", "G00")
            if word in ("G2", "G02", "G3", "G03"):
                points = _arc_points(position, target, params, word in ("G2", "G02"))
            else:
                points = [target]
            previous = list(position)
            for point in points:
                if rapid:
                    kind, speed = "rapid", rapid_mm_s
                else:
                    vertical = abs(point[0] - previous[0]) < 1e-9 and abs(point[1] - previous[1]) < 1e-9 and point[2] < previous[2] - 1e-9
                    kind = "plunge" if vertical else "cut"
                    speed = ((feed if feed else (vert_feed if vertical else horiz_feed)) or 1.0) / 60.0
                push(kind, previous, list(point), speed)
                previous = list(point)
            position = list(target)
        elif word in ("G81", "G82", "G83", "G73"):
            if None in position:
                continue
            xy = [params.get("X", position[0]), params.get("Y", position[1])]
            if "R" in params:
                canned_r = params["R"]
            bottom = params.get("Z")
            above = [xy[0], xy[1], position[2]]
            push("rapid", list(position), above, rapid_mm_s)
            current = above
            if canned_r is not None and canned_r < current[2]:
                lowered = [xy[0], xy[1], canned_r]
                push("rapid", current, lowered, rapid_mm_s)
                current = lowered
            if bottom is not None:
                down = [xy[0], xy[1], bottom]
                push("plunge", current, down, (feed or vert_feed or 1.0) / 60.0)
                push("rapid", down, [xy[0], xy[1], position[2]], rapid_mm_s)
                current = [xy[0], xy[1], position[2]]
            if word == "G82" and "P" in params:
                seconds += params["P"]
            position = current
    total_points = sum(len(line) for line in poly.values())
    truncated = total_points > points_limit
    if truncated:
        for kind, line in poly.items():
            stride = max(1, int(math.ceil(total_points / float(points_limit))))
            poly[kind] = [point for index, point in enumerate(line) if point is None or index % stride == 0 or index == len(line) - 1]
    compact = {kind: [None if point is None else [round(point[0], 3), round(point[1], 3), round(point[2], 3)] for point in line]
               for kind, line in poly.items()}
    bounds = None if math.isinf(low[0]) else {"min": [round(v, 3) for v in low], "max": [round(v, 3) for v in high]}
    return {"segments": segments, "length_mm": {key: round(value, 3) for key, value in lengths.items()},
            "estimated_s": round(seconds, 1), "estimate": True, "bounds": bounds, "polylines": compact, "truncated": truncated,
            "end": position if None not in position else None}


def _arc_points(start, end, params, clockwise):
    center = [start[0] + params.get("I", 0.0), start[1] + params.get("J", 0.0)]
    radius = math.hypot(start[0] - center[0], start[1] - center[1])
    if radius < 1e-9:
        return [end]
    a0 = math.atan2(start[1] - center[1], start[0] - center[0])
    a1 = math.atan2(end[1] - center[1], end[0] - center[0])
    sweep = (a0 - a1) if clockwise else (a1 - a0)
    sweep %= 2 * math.pi
    if sweep < 1e-9:
        sweep = 2 * math.pi
    count = max(2, min(48, int(sweep / (math.pi / 16)) + 1))
    points = []
    for step in range(1, count + 1):
        fraction = step / count
        angle = a0 + (-1 if clockwise else 1) * sweep * fraction
        points.append([center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle), start[2] + (end[2] - start[2]) * fraction])
    points[-1] = list(end)
    return points


def preview_budget(previews, total_limit=PREVIEW_POINTS_TOTAL):
    """Réduit l'ensemble des aperçus sous ``total_limit`` points (sous-échantillonnage, ``truncated`` posé)."""
    total = sum(sum(len(line) for line in item["polylines"].values()) for item in previews.values())
    if total <= total_limit:
        return previews
    stride = int(math.ceil(total / float(total_limit)))
    for item in previews.values():
        for kind, line in item["polylines"].items():
            item["polylines"][kind] = [point for index, point in enumerate(line) if point is None or index % stride == 0 or index == len(line) - 1]
        item["truncated"] = True
    return previews


# ---------------------------------------------------------------------------------------------
# Contrôles
# ---------------------------------------------------------------------------------------------
def tool_reach_issues(entry, tool, stock_top, final_z):
    """Outil trop court, porte-outil dans le brut : [(code, sévérité, message)]. ``final_z`` : Z absolu atteint."""
    issues = []
    depth = stock_top - final_z
    if depth > tool["flute_length"] + 1e-6:
        issues.append(("tool_too_short", "error", "Opération « %s » : profondeur %.2f mm supérieure à la longueur taillée de T%d (%.2f mm)."
                       % (entry["name"], depth, tool["number"], tool["flute_length"])))
    stickout = tool.get("stickout")
    if stickout is not None:
        if depth > stickout + 1e-6:
            issues.append(("holder_collision", "error", "Opération « %s » : le porte-outil de T%d descendrait %.2f mm sous le dessus du brut "
                           "(dépassement %.2f mm pour %.2f mm de profondeur)." % (entry["name"], tool["number"], depth - stickout, stickout, depth)))
        elif tool.get("holder_diameter") and stickout - depth < 2.0:
            issues.append(("holder_close", "warning", "Opération « %s » : porte-outil de T%d à %.2f mm du dessus du brut en fond de passe."
                           % (entry["name"], tool["number"], stickout - depth)))
    return issues


def zone_issues(entry, tool, zones):
    """Zones (fond de poche, rainure) plus étroites que l'outil : ``zones`` = [{"name", "width"}] en mm."""
    issues = []
    for zone in zones:
        if zone.get("width") is not None and zone["width"] < tool["diameter"] - 1e-6:
            issues.append(("zone_too_small_for_tool", "warning", "Opération « %s » : la zone %s (%.2f mm) est plus étroite que T%d (Ø %.2f mm) : "
                           "elle ne sera pas usinée." % (entry["name"], zone.get("name", ""), zone["width"], tool["number"], tool["diameter"])))
    return issues


def clearance_issue(stock_size_z, clearance, travel_z):
    if stock_size_z + clearance > travel_z + 1e-6:
        return ("clearance_outside_travel", "error", "Brut (%.1f mm) + dégagement (%.1f mm) dépassent la course Z (%g mm)."
                % (stock_size_z, clearance, travel_z))
    return None


def not_simulated(project, tools):
    """Ce que le calcul NE vérifie pas (manifeste, cam_handoff.not_simulated)."""
    items = ["Enlèvement de matière (aucune simulation volumique : CAMSimulator est graphique)",
             "Collisions 3D hors du brut (table, étau, broche, câbles)",
             "Origine pièce réelle sur la machine (G54 posé par l'opérateur) et course machine depuis cette origine",
             "Déflexion de l'outil, usure, état de surface, broche et avances réellement tenues",
             "Serrage et tenue de la pièce pendant les contours (ponts à vérifier)"]
    if not project.get("fixtures"):
        items.append("Brides non renseignées : aucun contrôle de collision bride / parcours")
    if any(tool.get("holder_diameter") is None for tool in tools.values()):
        items.append("Porte-outil non renseigné pour au moins un outil : aucun contrôle de collision porte-outil")
    if any(tool.get("stickout") is None for tool in tools.values()):
        items.append("Longueur sortie (stickout) absente pour au moins un outil : profondeur atteignable non contrôlée")
    if project.get("source", {}).get("kind") in ("stl", "3mf", "obj", "ply", "off", "amf"):
        items.append("Géométrie maillée : trous et poches détectés par facettes, perçage automatique indisponible")
    return items[:40]


def dressup_not_applied_warning(entry, dressup, reason):
    return ("dressup_not_applied", "warning", "Opération « %s » : dressup « %s » non appliqué (%s)." % (entry["name"], dressup["type"], reason))


def result_operation(entry, state, **fields):
    """Entrée ``results.operations[id]`` (contrat §9) + forme d'outil réellement utilisée."""
    item = {"id": entry["id"], "type": entry["type"], "name": entry["name"], "state": state, "ok": state == "computed",
            "estimated_s": None, "segments": None, "error": None, "progress": None, "tool": entry.get("tool"),
            "engine_shape": entry.get("engine_shape", {"shape": "none"}).get("shape")}
    item.update(fields)
    return item
