# -*- coding: utf-8 -*-
"""Projet CAM déclaratif d'Alpine Forge 3D (« Préparer pour une CNC ») : contrat v1 et v2.

Une seule fonction de validation, appelée trois fois : par la route Flask, par
``WorkerPlatform.create_job`` et par le runner du Worker avant FreeCADCmd (puis
une quatrième fois par ``freecad_cam.py`` lui-même). Bibliothèque standard
seulement : ce fichier est livré à plat dans le paquet du Worker et importé par
le site.

Règles :
- une clé inconnue est un refus (jamais ignorée) ;
- ``bool`` n'est jamais un nombre, NaN et l'infini sont refusés ;
- toutes les listes sont bornées ;
- aucun chemin, aucun script, aucune commande : seulement des valeurs ;
- la fonction rend une COPIE normalisée (valeurs par défaut posées), jamais
  l'objet de l'appelant ;
- un réglage que FreeCAD 1.1.3 ne peut pas honorer est refusé (ou seule sa valeur
  neutre est admise) : aucun champ muet (docs/cam_projet_v2.md).

Versions : ``version`` 1 (contrat de la tranche 1, inchangé : le moteur ``freecad_cam.py``
et les Workers ``freecad_cam_v1`` le consomment) et ``version`` 2 (docs/cam_projet_v2.md).
``validate_cam_project`` rend un projet de la même version que l'entrée ; ``as_version``
migre d'abord (``cam_migrate``).

Unités : les valeurs du bloc ``machine`` (courses, avances, hauteur de sécurité
minimale) sont TOUJOURS en millimètres (instantané du catalogue serveur).
Toutes les autres longueurs et avances du projet (brut, origine, hauteurs,
fixtures, outils, opérations) sont dans ``units`` du projet ; les bornes sont
contrôlées après conversion en mm. Le moteur convertit tout en mm pour FreeCAD.
"""
import copy
import math
import re

CAM_PAYLOAD_VERSION = 2
CAM_PAYLOAD_VERSIONS = (1, 2)
CAM_CAPABILITY = "freecad_cam_v1"
CAM_CAPABILITY_V2 = "freecad_cam_v2"
INCH_MM = 25.4

UNITS = ("mm", "inch")
SOURCE_KINDS = ("step", "stp", "fcstd", "brep", "brp", "iges", "igs", "stl", "3mf", "obj", "ply", "off", "amf")
MESH_SOURCE_KINDS = ("stl", "3mf", "obj", "ply", "off", "amf")
# Programmes déjà post-traités (imprimante ou CNC) : jamais une géométrie.
GCODE_EXTENSIONS = ("gcode", "gco", "g", "bgcode", "nc", "ngc", "tap", "cnc")
GCODE_SOURCE_MESSAGE = (
    "Un fichier G-code ne peut pas servir de modèle pour une CNC : un G-code d'imprimante 3D décrit un dépôt de "
    "filament couche par couche, pas un usinage par enlèvement de matière, et il ne contient plus la forme de la "
    "pièce. Reviens au modèle source (STL, STEP, 3MF ou fichier CAO) et choisis-le comme géométrie."
)
TOP_FACES = ("+z", "-z", "+x", "-x", "+y", "-y")
ROTATIONS_Z = (0, 90, 180, 270)
MACHINE_KINDS = ("router", "mill", "vmc", "engraver")
MACHINE_AXES = ("X", "Y", "Z")
SPINDLE_DIRECTIONS = ("cw", "ccw")
TOOL_CHANGE_MODES = ("manual", "atc")
COOLANT_MODES = ("flood", "mist", "air")
OPERATION_COOLANT = ("off",) + COOLANT_MODES
OUTPUT_EXTENSIONS = ("nc", "ngc", "tap", "gcode")
STOCK_SHAPES = ("box",)
STOCK_SHAPES_V2 = ("box", "cylinder")
STOCK_MODES = ("from_model", "explicit")
STOCK_PLACEMENTS = ("center_top", "min_corner", "custom")
MATERIAL_REF_KINDS = ("catalog", "library")
ORIGIN_PRESETS = ("top_front_left", "top_front_right", "top_rear_left", "top_rear_right",
                  "top_center", "bottom_center", "center", "custom")
WCS_CODES = ("G54", "G55", "G56", "G57", "G58", "G59")
FIXTURE_KINDS = ("box",)
# Toutes les familles connues du catalogue ; seules celles de la tranche 1 sont acceptées en v1.
TOOL_FAMILIES_ALL = ("endmill", "drill", "ballend", "bullnose", "vbit", "chamfer", "engraver", "threadmill",
                     "tap", "reamer", "slittingsaw", "dovetail", "radius", "taperedballnose", "probe")
TOOL_FAMILIES_SUPPORTED = ("endmill", "drill")
TOOL_FAMILIES_SUPPORTED_V2 = ("endmill", "drill", "ballend", "bullnose", "vbit", "chamfer", "engraver", "threadmill")
TOOL_FAMILY_REFUSED_V2 = {
    "tap": "le taraudage (G84) n'existe pas sur GRBL et n'est pas traduit par le post grbl",
    "reamer": "aucune opération FreeCAD 1.1.3 ne distingue un alésoir d'un foret",
    "slittingsaw": "aucune opération FreeCAD 1.1.3 n'exploite une scie à fendre en 3 axes",
    "dovetail": "aucune opération FreeCAD 1.1.3 n'exploite une fraise à queue d'aronde",
    "radius": "aucune opération FreeCAD 1.1.3 n'exploite une fraise à rayon",
    "taperedballnose": "forme non prise en charge par les opérations 3 axes vérifiées",
    "probe": "le palpage (G38.2, fichier PROBEOPEN) suit une convention LinuxCNC non vérifiée ici",
}
TOOL_MATERIALS = ("carbide", "hss", "cobalt", "diamond", "ceramic", "other")
OPERATION_TYPES_ALL = ("facing", "pocket", "profile", "drilling", "adaptive", "engrave", "vcarve", "helix", "slot",
                       "surface", "waterline", "deburr", "thread_milling", "custom", "pause", "tool_change", "note")
OPERATION_TYPES_SUPPORTED = ("facing", "pocket", "profile", "drilling", "pause", "tool_change", "note")
OPERATION_TYPES_SUPPORTED_V2 = ("facing", "pocket", "profile", "drilling", "adaptive", "engrave", "vcarve", "helix",
                                "slot", "surface", "waterline", "deburr", "thread_milling", "custom", "pause",
                                "tool_change", "note")
MACHINING_TYPES = ("facing", "pocket", "profile", "drilling")
MACHINING_TYPES_V2 = ("facing", "pocket", "profile", "drilling", "adaptive", "engrave", "vcarve", "helix", "slot",
                      "surface", "waterline", "deburr", "thread_milling", "custom")
OCL_OPERATIONS = ("surface", "waterline")
# Familles d'outils admises par type d'usinage (un foret ne fraise pas de côté).
OPERATION_TOOL_FAMILIES = {"facing": ("endmill",), "pocket": ("endmill",), "profile": ("endmill",),
                           "drilling": ("drill", "endmill")}
OPERATION_TOOL_FAMILIES_V2 = {
    "facing": ("endmill", "bullnose"),
    "pocket": ("endmill", "bullnose", "ballend"),
    "profile": ("endmill", "bullnose", "ballend"),
    "drilling": ("drill", "endmill"),
    "adaptive": ("endmill", "bullnose", "ballend"),
    "engrave": ("vbit", "engraver", "endmill", "ballend", "chamfer"),
    "vcarve": ("vbit", "engraver", "chamfer"),
    "helix": ("endmill", "bullnose"),
    "slot": ("endmill", "bullnose", "ballend"),
    "surface": ("ballend", "bullnose", "endmill", "vbit", "chamfer", "engraver"),
    "waterline": ("ballend", "bullnose", "endmill", "vbit", "chamfer", "engraver"),
    "deburr": ("chamfer", "vbit", "engraver"),
    "thread_milling": ("threadmill",),
    "custom": TOOL_FAMILIES_SUPPORTED_V2,
    "tool_change": TOOL_FAMILIES_SUPPORTED_V2,
}
SELECTION_MODES = ("auto",)
SELECTION_MODES_V2 = ("auto", "faces")
CUT_DIRECTIONS = ("climb", "conventional")
PATH_DIRECTIONS = ("cw", "ccw")
ENTRY_MODES = ("plunge", "ramp", "helix")
PROFILE_SIDES = ("outside", "inside")
# « none » : aucune compensation par le contrôleur (G41/G42 jamais émis) ; le parcours est
# décalé du rayon de fraise par FreeCAD. La compensation contrôleur n'est pas vérifiée en tranche 1.
COMPENSATION_MODES = ("none",)
PHASES = ("setup", "roughing", "semi_finishing", "finishing", "drilling", "engraving", "other")
DEFAULT_PHASES = {"facing": "roughing", "pocket": "roughing", "profile": "finishing", "drilling": "drilling",
                  "adaptive": "roughing", "engrave": "engraving", "vcarve": "engraving", "helix": "drilling",
                  "slot": "roughing", "surface": "finishing", "waterline": "semi_finishing", "deburr": "finishing",
                  "thread_milling": "finishing", "custom": "other", "pause": "setup", "tool_change": "setup", "note": "setup"}
DRESSUP_TYPES = ("tags", "dogbone", "boundary")
DRESSUP_TYPES_REFUSED = {
    "ramp_entry": "RampEntry n'a pas de fonction Create en console FreeCAD 1.1.3 (classe GUI)",
    "lead_in_out": "LeadInOut n'a pas de fonction Create en console FreeCAD 1.1.3 (classe GUI)",
    "dragknife": "Dragknife est une classe GUI en 1.1.3",
    "z_correct": "ZCorrect lit un fichier de palpage et n'existe qu'en GUI",
    "axis_map": "AxisMap (4e axe) n'existe qu'en GUI ; machines 3 axes seulement",
    "array": "non proposé en 2.0",
}
DRESSUP_OPERATIONS = {
    "tags": ("profile", "pocket", "adaptive", "slot", "engrave", "vcarve"),
    "dogbone": ("pocket", "profile", "adaptive", "slot"),
    "boundary": tuple(kind for kind in MACHINING_TYPES_V2 if kind != "custom"),
}
DOGBONE_STYLES = ("dogbone", "t_bone_horizontal", "t_bone_vertical", "t_bone_long_edge", "t_bone_short_edge")
DOGBONE_INCISIONS = ("adaptive", "fixed", "custom")
DOGBONE_SIDES = ("left", "right")
TOOL_CHANGE_POLICIES = ("pause_m0", "split_files", "atc_m6")
PROVENANCE_SOURCES = ("measured", "profile", "assumed", "user")
PROVENANCE_FIELDS = ("travel.x", "travel.y", "travel.z", "max_feed", "max_rapid", "max_accel", "spindle.min_rpm",
                     "spindle.max_rpm", "spindle.power_w", "safe_height_min", "tool_change", "tool_count", "controller",
                     "coolant")
RESULT_STATES = ("not_computed", "computing", "computed", "stale", "error")
OPERATION_RESULT_STATES = ("not_computed", "computing", "computed", "stale", "error", "skipped")
MISSING_DATA_SEVERITIES = ("info", "warning", "blocking")
CONFIDENCE_LEVELS = ("measured", "profile", "assumed")
FACING_BOUNDARIES = ("stock", "boundbox", "perimeter", "face_region")
CLEARING_PATTERNS = ("zigzag", "offset", "zigzag_offset", "line", "grid")
START_AT = ("center", "edge")
JOIN_TYPES = ("round", "square", "miter")
HANDLE_MULTIPLE = ("collectively", "individually")
RETRACT_MODES = ("G98", "G99")
DRILL_EXTRA_OFFSETS = ("none", "drill_tip", "2x_drill_tip")
ADAPTIVE_OPERATION_TYPES = ("clearing", "profiling")
SLOT_PATTERNS = ("line", "zigzag")
LAYER_MODES = ("single", "multi")
SURFACE_PATTERNS = ("circular", "circular_zigzag", "line", "offset", "spiral", "zigzag")
PROFILE_EDGES = ("none", "only", "first", "last")
DROP_CUTTER_DIRS = ("x", "y")
DEBURR_JOINS = ("round", "miter")
THREAD_TYPES = ("custom_internal", "custom_external", "metric_internal_6h", "metric_external_6g", "metric_external_4g6g",
                "imperial_internal_2b", "imperial_internal_3b", "imperial_external_2a", "imperial_external_3a")
THREAD_ORIENTATIONS = ("right", "left")
WATERLINE_ALGORITHMS = ("ocl_dropcutter",)
SURFACE_SCAN_TYPES = ("planar",)

# Mots autorisés dans un préambule / postambule personnalisé (en plus des commentaires « ( … ) »
# et des mots S, F, T bornés). Jamais M98/M99 (sous-programmes), M6, G28/G53 (références machine),
# G91 ou G20/G21 (le post-processeur fixe les unités et le mode de distance).
CAM_CUSTOM_WORDS_ALLOWED = frozenset({
    "G17", "G40", "G49", "G54", "G55", "G56", "G57", "G58", "G59", "G61", "G64", "G80", "G90", "G94",
    "M0", "M1", "M2", "M3", "M4", "M5", "M7", "M8", "M9", "M30",
})
CAM_CUSTOM_VALUE_WORDS = {"S": (0.0, 100000.0), "F": (1.0, 50000.0), "T": (0.0, 999.0)}
# Opération « custom » (Path.Op.Custom) : déplacements linéaires seulement, dans les unités du projet.
CAM_MOTION_WORDS_ALLOWED = frozenset({"G0", "G1", "G4", "G17", "G90", "G94", "M0", "M1", "M3", "M4", "M5", "M7", "M8", "M9"})
CAM_MOTION_VALUE_WORDS = {"X": (-3000.0, 3000.0), "Y": (-3000.0, 3000.0), "Z": (-3000.0, 3000.0),
                          "F": (1.0, 50000.0), "S": (0.0, 100000.0), "P": (0.0, 60.0)}
# Options de post-processeur jamais transmises par cette voie.
POST_OPTIONS_DENIED = frozenset({"preamble", "postamble", "show-editor", "output_all_arguments",
                                 "output_visible_arguments", "help"})

CAM_BOUNDS = {
    "length_mm": (0.001, 3000.0),
    "travel_mm": (1.0, 3000.0),
    "coordinate_mm": (-3000.0, 3000.0),
    "margin_mm": (0.0, 500.0),
    "height_mm": (0.001, 500.0),
    "safe_height_min_mm": (0.0, 500.0),
    "tolerance_mm": (0.0001, 1.0),
    "feed_mm_min": (1.0, 50000.0),
    "accel_mm_s2": (1.0, 100000.0),
    "spindle_rpm": (0.0, 100000.0),
    "spindle_power_w": (0.0, 100000.0),
    "angle_deg": (0.0, 90.0),
    "tip_angle_deg": (0.0, 180.0),
    "tool_diameter_mm": (0.05, 100.0),
    "tool_length_mm": (1.0, 500.0),
    "flute_length_mm": (0.1, 300.0),
    "chip_load_mm": (0.0001, 5.0),
    "stock_to_leave_mm": (0.0, 50.0),
    "stepover_pct": (1.0, 100.0),
    "dwell_s": (0.0, 60.0),
    "tool_number": (1, 999),
    "length_offset": (0, 999),
    "flutes": (1, 16),
    "tool_count": (1, 64),
    "tab_count": (0, 16),
    # v2
    "scale": (0.001, 1000.0),
    "cutting_edge_angle_deg": (1.0, 179.0),
    "tip_diameter_mm": (0.0, 50.0),
    "holder_diameter_mm": (1.0, 200.0),
    "holder_length_mm": (1.0, 500.0),
    "corner_radius_mm": (0.0, 50.0),
    "crest_mm": (0.01, 5.0),
    "pass_count": (1, 20),
    "zigzag_angle_deg": (0.0, 180.0),
    "helix_angle_deg": (1.0, 89.0),
    "sample_interval_mm": (0.01, 10.0),
    "deburr_width_mm": (0.01, 20.0),
    "extra_depth_mm": (0.0, 10.0),
    "discretize_mm": (0.01, 5.0),
    "colinear_deg": (0.0, 45.0),
    "finish_depth_mm": (0.0, 50.0),
    "retract_height_mm": (0.0, 500.0),
    "lift_distance_mm": (0.0, 50.0),
    "extend_mm": (0.0, 100.0),
    "boundary_adjustment_mm": (-100.0, 100.0),
    "linear_deflection_mm": (0.0001, 1.0),
    "pattern_angle_deg": (0.0, 360.0),
    "spindle_wait_s": (0.0, 60.0),
    "pitch_mm": (0.1, 10.0),
    "tpi": (1.0, 100.0),
    "thread_fit_pct": (0.0, 100.0),
    "thread_diameter_mm": (0.5, 200.0),
    "vertex_index": (0, 10000),
    "estimated_s": (0.0, 10000000.0),
    "result_version": (0, 1000000),
    "fixtures_max": 16,
    "tools_max": 32,
    "operations_max": 40,
    "dressups_max": 8,
    "faces_max": 200,
    "custom_gcode_lines_max": 40,
    "missing_data_max": 40,
    "recommendations_max": 80,
    "unsupported_commands_max": 64,
    "custom_lines_max": 20,
    "custom_line_chars": 80,
    "post_options_max": 40,
    "post_option_value_chars": 120,
    "string_chars": 80,
    "note_chars": 240,
    "stem_chars": 60,
    "source_name_chars": 120,
    "id_pattern": r"^[a-z0-9][a-z0-9_-]{0,31}$",
    "profile_id_pattern": r"^[a-z0-9][a-z0-9_-]{0,47}$",
    "name_pattern": r"^[A-Za-z0-9 ._\-()'éèêëàâçôöûùüîïÉÈÊÀÂÇÔÛÎœŒ]+$",
    "face_pattern": r"^Face[0-9]{1,5}$",
    "sha256_pattern": r"^[a-f0-9]{64}$",
    "iso_datetime_pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?$",
    "ref_pattern": r"^[A-Za-z0-9_-]{1,64}$",
    "machine_units": "mm",
}

_ID = re.compile(CAM_BOUNDS["id_pattern"])
_PROFILE_ID = re.compile(CAM_BOUNDS["profile_id_pattern"])
_NAME = re.compile(CAM_BOUNDS["name_pattern"])
_NOTE = re.compile(r"^[A-Za-z0-9 ._\-()'éèêëàâçôöûùüîïÉÈÊÀÂÇÔÛÎœŒ,;:!?«»/%+°]*$")
_POST_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_CONTROLLER = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_STEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,59}$")
_SOURCE_NAME = re.compile(r"^[A-Za-z0-9 ._\-()'éèêëàâçôöûùüîïÉÈÊÀÂÇÔÛÎ]+$")
_COMMAND_WORD = re.compile(r"^[GM][0-9]{1,3}(\.[0-9])?$")
_OPTION_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_OPTION_VALUE = re.compile(r"^[A-Za-z0-9 ._,:+\-]*$")
_FACE = re.compile(CAM_BOUNDS["face_pattern"])
_SHA256 = re.compile(CAM_BOUNDS["sha256_pattern"])
_ISO_DATETIME = re.compile(CAM_BOUNDS["iso_datetime_pattern"])
_REF = re.compile(CAM_BOUNDS["ref_pattern"])
_LIBRARY_MATERIAL = re.compile(r"^mat-[a-f0-9]{12}$")
_LIBRARY_TOOL = re.compile(r"^tool-[a-f0-9]{12}$")
_PLAIN_TEXT = re.compile(r"^[A-Za-z0-9 ._\-()'éèêëàâçôöûùüîïÉÈÊÀÂÇÔÛÎœŒ,;:!?«»/%+°\[\]=]*$")
_VERSION_TEXT = re.compile(r"^[A-Za-z0-9._-]*$")
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


class CamPayloadError(ValueError):
    """Refus d'un projet CAM ; ``path`` désigne le champ fautif."""

    def __init__(self, path, message):
        self.path = path
        super().__init__(f"{path} : {message}" if path else message)


def _fail(path, message):
    raise CamPayloadError(path, message)


def _keys(value, path, required, optional=()):
    if not isinstance(value, dict):
        _fail(path, "objet attendu.")
    unknown = sorted(set(value) - set(required) - set(optional), key=str)
    if unknown:
        _fail(f"{path}.{unknown[0]}" if path else str(unknown[0]), "champ inconnu, refusé.")
    missing = [key for key in required if key not in value]
    if missing:
        _fail(f"{path}.{missing[0]}" if path else missing[0], "champ obligatoire manquant.")
    return value


def _number(value, path, bounds, *, factor=1.0, integer=False):
    """Nombre fini dans ``bounds`` (exprimées en mm quand ``factor`` convertit)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "nombre attendu.")
    if not math.isfinite(value):
        _fail(path, "nombre fini attendu (NaN et infini refusés).")
    if integer and (not isinstance(value, int) and not float(value).is_integer()):
        _fail(path, "nombre entier attendu.")
    low, high = bounds
    checked = float(value) * factor
    if not low - 1e-9 <= checked <= high + 1e-9:
        unit = " mm" if factor != 1.0 or path.endswith(("_mm",)) else ""
        _fail(path, f"valeur hors limites (attendu entre {low:g} et {high:g}{unit}).")
    return int(value) if integer else float(value)


def _optional_number(value, path, bounds, **kwargs):
    return None if value is None else _number(value, path, bounds, **kwargs)


def _bool(value, path):
    if not isinstance(value, bool):
        _fail(path, "vrai ou faux attendu.")
    return value


def _choice(value, path, choices):
    if not isinstance(value, str) or value not in choices:
        _fail(path, f"valeur non admise (attendu : {', '.join(str(item) for item in choices)}).")
    return value


def _text(value, path, *, pattern=_NAME, limit=None, allow_empty=False):
    limit = limit or CAM_BOUNDS["string_chars"]
    if not isinstance(value, str):
        _fail(path, "texte attendu.")
    value = value.strip()
    if not value and not allow_empty:
        _fail(path, "texte vide.")
    if len(value) > limit:
        _fail(path, f"texte trop long ({limit} caractères au plus).")
    if value and not pattern.fullmatch(value):
        _fail(path, "caractères non autorisés.")
    return value


def _note(value, path):
    return _text(value, path, pattern=_NOTE, limit=CAM_BOUNDS["note_chars"], allow_empty=True)


def _identifier(value, path, pattern=_ID):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _fail(path, "identifiant invalide (minuscules, chiffres, « _ » et « - »).")
    return value


def _list(value, path, limit, *, minimum=0):
    if not isinstance(value, list):
        _fail(path, "liste attendue.")
    if len(value) > limit:
        _fail(path, f"liste trop longue ({limit} éléments au plus).")
    if len(value) < minimum:
        _fail(path, f"au moins {minimum} élément(s) attendu(s).")
    return value


def _xyz(value, path, bounds, factor, keys=("x", "y", "z")):
    _keys(value, path, keys)
    return {key: _number(value[key], f"{path}.{key}", bounds, factor=factor) for key in keys}


def _iso_datetime(value, path):
    if value is None:
        return None
    if not isinstance(value, str) or not _ISO_DATETIME.fullmatch(value.strip()):
        _fail(path, "date ISO 8601 attendue (ex. 2026-10-27T14:05:00Z) ou null.")
    return value.strip()


def _sha256(value, path):
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value.strip().lower()):
        _fail(path, "empreinte SHA-256 attendue (64 caractères hexadécimaux) ou null.")
    return value.strip().lower()


def _ref(value, path):
    if value is None:
        return None
    if not isinstance(value, str) or not _REF.fullmatch(value):
        _fail(path, "référence invalide (lettres, chiffres, « _ » et « - », 64 caractères au plus).")
    return value


def _neutral(item, key, where, neutral, reason):
    """Un réglage non pris en charge : seule sa valeur neutre (ou l'absence) est admise."""
    if key in item and item[key] is not None and item[key] != neutral:
        _fail(f"{where}.{key}", f"réglage non pris en charge par FreeCAD 1.1.3 pour cette opération ({reason}) : "
                                f"laisse « {neutral!r} » ou retire le champ.".replace("'", ""))
    return neutral


def _source(value, factor, version=1):
    path = "source"
    if isinstance(value, dict):
        name = value.get("file")
        kind = value.get("kind")
        extension = name.rsplit(".", 1)[-1].lower() if isinstance(name, str) and "." in name else ""
        if extension in GCODE_EXTENSIONS or (isinstance(kind, str) and kind.lower() in GCODE_EXTENSIONS):
            _fail(f"{path}.file", GCODE_SOURCE_MESSAGE)
    _keys(value, path, ("file", "kind", "units"), ("scale", "sha256") if version == 2 else ())
    name = value["file"]
    if not isinstance(name, str) or not 1 <= len(name) <= CAM_BOUNDS["source_name_chars"]:
        _fail(f"{path}.file", "nom de fichier invalide.")
    if "/" in name or "\\" in name or ":" in name or ".." in name or name.startswith(".") or name != name.strip():
        _fail(f"{path}.file", "un nom de fichier seul est attendu, jamais un chemin.")
    if not _SOURCE_NAME.fullmatch(name) or "." not in name:
        _fail(f"{path}.file", "nom de fichier invalide (caractères ou extension).")
    stem, extension = name.rsplit(".", 1)
    if stem.split(".")[0].lower() in _WINDOWS_RESERVED:
        _fail(f"{path}.file", "nom de fichier réservé par Windows.")
    kind = _choice(value["kind"], f"{path}.kind", SOURCE_KINDS)
    if extension.lower() != kind:
        _fail(f"{path}.kind", f"le type « {kind} » ne correspond pas à l'extension « .{extension.lower()} ».")
    if value["units"] == "unknown":
        _fail(f"{path}.units", "unités du modèle inconnues : confirme d'abord si le fichier est en mm ou en pouces.")
    units = _choice(value["units"], f"{path}.units", UNITS)
    clean = {"file": name, "kind": kind, "units": units}
    if version == 2:
        clean["scale"] = _number(value.get("scale", 1), f"{path}.scale", CAM_BOUNDS["scale"])
        clean["sha256"] = _sha256(value.get("sha256"), f"{path}.sha256")
    return clean


def _orientation(value):
    value = {"top_face": "+z", "rotation_z_deg": 0, "flip": False} if value is None else value
    path = "orientation"
    _keys(value, path, (), ("top_face", "rotation_z_deg", "flip"))
    rotation = value.get("rotation_z_deg", 0)
    if isinstance(rotation, bool) or not isinstance(rotation, (int, float)) or rotation not in ROTATIONS_Z:
        _fail(f"{path}.rotation_z_deg", "rotation admise : 0, 90, 180 ou 270 degrés.")
    return {"top_face": _choice(value.get("top_face", "+z"), f"{path}.top_face", TOP_FACES),
            "rotation_z_deg": int(rotation), "flip": _bool(value.get("flip", False), f"{path}.flip")}


def _machine(value, version=1):
    path = "machine"
    required = ("profile_id", "label", "kind", "axes", "travel", "max_feed", "spindle", "safe_height_min",
                "controller", "post_processor", "output_extension")
    optional = ("max_rapid", "max_accel", "tool_change", "tool_count", "probe_z", "tool_length_measure", "coolant",
                "units", "unsupported_commands", "notes")
    if version == 2:
        if isinstance(value, dict) and "verified" in value:
            _fail(f"{path}.verified", "remplacé en v2 par machine.provenance (provenance par champ).")
        optional = optional + ("provenance", "firmware_snapshot", "grbl", "tool_change_position")
    else:
        optional = optional + ("verified",)
    if isinstance(value, dict) and isinstance(value.get("axes"), list):
        axes = value["axes"]
        if [item for item in axes if item not in MACHINE_AXES] or len(axes) != 3 or sorted(set(axes), key=str) != sorted(axes, key=str):
            _fail(f"{path}.axes", "seules les machines 3 axes X Y Z sont calculables dans cette version "
                                  "(les axes rotatifs ou supplémentaires ne sont pas vérifiés).")
    _keys(value, path, required, optional)
    axes = value["axes"]
    if not isinstance(axes, list) or list(axes) != list(MACHINE_AXES):
        _fail(f"{path}.axes", "axes attendus : X, Y, Z (dans cet ordre).")
    if isinstance(value["post_processor"], str) and not value["post_processor"].strip():
        _fail(f"{path}.post_processor", "aucun post-processeur choisi : la génération du G-code est impossible.")
    spindle = _keys(value["spindle"], f"{path}.spindle", ("min_rpm", "max_rpm"), ("power_w", "directions"))
    clean_spindle = {
        "min_rpm": _number(spindle["min_rpm"], f"{path}.spindle.min_rpm", CAM_BOUNDS["spindle_rpm"]),
        "max_rpm": _number(spindle["max_rpm"], f"{path}.spindle.max_rpm", CAM_BOUNDS["spindle_rpm"]),
        "power_w": _optional_number(spindle.get("power_w"), f"{path}.spindle.power_w", CAM_BOUNDS["spindle_power_w"]),
    }
    if clean_spindle["max_rpm"] <= 0 or clean_spindle["min_rpm"] > clean_spindle["max_rpm"]:
        _fail(f"{path}.spindle", "vitesse de broche minimale supérieure à la maximale, ou maximale nulle.")
    directions = _list(spindle.get("directions", ["cw"]), f"{path}.spindle.directions", 2, minimum=1)
    clean_spindle["directions"] = [_choice(item, f"{path}.spindle.directions[{index}]", SPINDLE_DIRECTIONS)
                                   for index, item in enumerate(directions)]
    if len(set(clean_spindle["directions"])) != len(clean_spindle["directions"]):
        _fail(f"{path}.spindle.directions", "sens de rotation en double.")
    coolant = _list(value.get("coolant", []), f"{path}.coolant", len(COOLANT_MODES))
    commands = _list(value.get("unsupported_commands", []), f"{path}.unsupported_commands",
                     CAM_BOUNDS["unsupported_commands_max"])
    clean_commands = []
    for index, word in enumerate(commands):
        if not isinstance(word, str) or not _COMMAND_WORD.fullmatch(word.strip().upper()):
            _fail(f"{path}.unsupported_commands[{index}]", "mot G ou M attendu (ex. G41, M8).")
        word = _normalize_word(word.strip().upper())
        if word not in clean_commands:
            clean_commands.append(word)
    clean = {
        "profile_id": _identifier(value["profile_id"], f"{path}.profile_id", _PROFILE_ID),
        "label": _text(value["label"], f"{path}.label"),
        "kind": _choice(value["kind"], f"{path}.kind", MACHINE_KINDS),
        "axes": list(MACHINE_AXES),
        "travel": _xyz(value["travel"], f"{path}.travel", CAM_BOUNDS["travel_mm"], 1.0),
        "max_feed": _number(value["max_feed"], f"{path}.max_feed", CAM_BOUNDS["feed_mm_min"]),
        "max_rapid": _optional_number(value.get("max_rapid"), f"{path}.max_rapid", CAM_BOUNDS["feed_mm_min"]),
        "max_accel": _optional_number(value.get("max_accel"), f"{path}.max_accel", CAM_BOUNDS["accel_mm_s2"]),
        "spindle": clean_spindle,
        "tool_change": _choice(value.get("tool_change", "manual"), f"{path}.tool_change", TOOL_CHANGE_MODES),
        "tool_count": _number(value.get("tool_count", 1), f"{path}.tool_count", CAM_BOUNDS["tool_count"], integer=True),
        "probe_z": _bool(value.get("probe_z", False), f"{path}.probe_z"),
        "tool_length_measure": _bool(value.get("tool_length_measure", False), f"{path}.tool_length_measure"),
        "coolant": [_choice(item, f"{path}.coolant[{index}]", COOLANT_MODES) for index, item in enumerate(coolant)],
        "safe_height_min": _number(value["safe_height_min"], f"{path}.safe_height_min", CAM_BOUNDS["safe_height_min_mm"]),
        "units": _choice(value.get("units", "mm"), f"{path}.units", UNITS),
        "controller": _identifier(value["controller"], f"{path}.controller", _CONTROLLER),
        "post_processor": _identifier(value["post_processor"], f"{path}.post_processor", _POST_NAME),
        "output_extension": _choice(value["output_extension"], f"{path}.output_extension", OUTPUT_EXTENSIONS),
        "unsupported_commands": clean_commands,
        "notes": _note(value.get("notes", ""), f"{path}.notes"),
    }
    if version == 1 and "verified" in value:
        clean["verified"] = _bool(value["verified"], f"{path}.verified")
    if len(set(clean["coolant"])) != len(clean["coolant"]):
        _fail(f"{path}.coolant", "mode d'arrosage en double.")
    if version == 2:
        clean["provenance"] = _provenance(value.get("provenance"), f"{path}.provenance")
        clean["firmware_snapshot"] = _firmware_snapshot(value.get("firmware_snapshot"), f"{path}.firmware_snapshot")
        clean["grbl"] = _grbl(value.get("grbl"), f"{path}.grbl", clean)
        clean["tool_change_position"] = _tool_change_position(value.get("tool_change_position"), f"{path}.tool_change_position")
    return clean


def _provenance(value, path):
    value = {} if value is None else value
    if not isinstance(value, dict):
        _fail(path, "objet attendu : {champ: {source, read_at}}.")
    clean = {}
    for field, item in value.items():
        where = f"{path}.{field}"
        if field not in PROVENANCE_FIELDS:
            _fail(where, f"champ de provenance inconnu (attendu : {', '.join(PROVENANCE_FIELDS)}).")
        if isinstance(item, str):
            item = {"source": item, "read_at": None}
        _keys(item, where, ("source",), ("read_at",))
        clean[field] = {"source": _choice(item["source"], f"{where}.source", PROVENANCE_SOURCES),
                        "read_at": _iso_datetime(item.get("read_at"), f"{where}.read_at")}
    return clean


def _firmware_snapshot(value, path):
    if value is None:
        return None
    _keys(value, path, ("id",), ("taken_at", "version"))
    return {"id": _ref(value["id"], f"{path}.id"), "taken_at": _iso_datetime(value.get("taken_at"), f"{path}.taken_at"),
            "version": _text(value.get("version", ""), f"{path}.version", pattern=_PLAIN_TEXT, limit=40, allow_empty=True)}


def _optional_xyz(value, path, bounds):
    if value is None:
        return None
    _keys(value, path, ("x", "y", "z"))
    return {key: _optional_number(value[key], f"{path}.{key}", bounds) for key in ("x", "y", "z")}


def _grbl(value, path, machine):
    empty = {"s_max": None, "laser_mode": None, "homing": None, "max_travel": None, "max_rate": None, "accel": None,
             "read_at": None}
    if value is None:
        return empty
    _keys(value, path, (), tuple(empty))
    clean = {
        "s_max": _optional_number(value.get("s_max"), f"{path}.s_max", (0.0, 1000000.0)),
        "laser_mode": None if value.get("laser_mode") is None else _bool(value["laser_mode"], f"{path}.laser_mode"),
        "homing": None if value.get("homing") is None else _bool(value["homing"], f"{path}.homing"),
        "max_travel": _optional_xyz(value.get("max_travel"), f"{path}.max_travel", (0.0, 100000.0)),
        "max_rate": _optional_xyz(value.get("max_rate"), f"{path}.max_rate", (0.0, 1000000.0)),
        "accel": _optional_xyz(value.get("accel"), f"{path}.accel", (0.0, 1000000.0)),
        "read_at": _iso_datetime(value.get("read_at"), f"{path}.read_at"),
    }
    if clean["laser_mode"] is True:
        _fail(f"{path}.laser_mode", "$32=1 : la carte est en mode laser ; désactive le mode laser ($32=0) avant un usinage.")
    if clean["max_travel"]:
        for axis in ("x", "y", "z"):
            measured = clean["max_travel"][axis]
            if measured is not None and measured > 0 and machine["travel"][axis] > measured + 1e-9:
                _fail(f"machine.travel.{axis}", f"course déclarée ({machine['travel'][axis]:g} mm) au-delà de la course "
                                                f"mesurée sur la carte ($13{'xyz'.index(axis)} = {measured:g} mm).")
    if clean["max_rate"]:
        rates = [rate for rate in clean["max_rate"].values() if rate is not None and rate > 0]
        if rates and machine["max_feed"] > min(rates) + 1e-9:
            _fail("machine.max_feed", f"avance maximale déclarée ({machine['max_feed']:g} mm/min) au-delà de la vitesse "
                                      f"maximale mesurée sur la carte ($110-112 : {min(rates):g} mm/min).")
    return clean


def _tool_change_position(value, path):
    if value is None:
        return None
    _keys(value, path, ("x", "y", "z"), ("verified",))
    clean = _xyz({key: value[key] for key in ("x", "y", "z")}, path, CAM_BOUNDS["coordinate_mm"], 1.0)
    clean["verified"] = _bool(value.get("verified", False), f"{path}.verified")
    return clean


def _normalize_word(word):
    """« G01 » → « G1 », « M08 » → « M8 » : une seule écriture par commande."""
    letter, number = word[0], word[1:]
    if "." in number:
        whole, fraction = number.split(".", 1)
        return f"{letter}{int(whole)}.{fraction}"
    return f"{letter}{int(number)}"


def _stock(value, factor, machine, version=1, heights=None):
    path = "stock"
    optional = ("shape", "size", "margins", "material_id")
    if version == 2:
        optional = optional + ("diameter", "height", "placement", "placement_offset", "material_ref", "material_label")
    _keys(value, path, ("mode",), optional)
    mode = _choice(value["mode"], f"{path}.mode", STOCK_MODES)
    shape = _choice(value.get("shape", "box"), f"{path}.shape", STOCK_SHAPES_V2 if version == 2 else STOCK_SHAPES)
    size = value.get("size", {"x": 0, "y": 0, "z": 0})
    if mode == "explicit" and shape == "box":
        size = _xyz(size, f"{path}.size", CAM_BOUNDS["length_mm"], factor)
        for axis in ("x", "y", "z"):
            if size[axis] * factor > machine["travel"][axis] + 1e-9:
                _fail(f"{path}.size.{axis}", f"brut plus grand que la course {axis.upper()} de la machine "
                                             f"({machine['travel'][axis]:g} mm).")
    else:
        size = _xyz(size, f"{path}.size", (0.0, 3000.0), factor)
    margins = value.get("margins", {"x": 0, "y": 0, "top": 0, "bottom": 0})
    margins = _xyz(margins, f"{path}.margins", CAM_BOUNDS["margin_mm"], factor, keys=("x", "y", "top", "bottom"))
    clean = {"shape": shape, "mode": mode, "size": size, "margins": margins}
    if version == 1:
        material = value.get("material_id", "")
        if material not in ("", None):
            material = _identifier(material, f"{path}.material_id", _PROFILE_ID)
        clean["material_id"] = material or ""
        return clean
    # v2
    if shape == "cylinder":
        if mode != "explicit":
            _fail(f"{path}.shape", "un brut cylindrique doit être saisi (mode explicit : diameter et height).")
        for key in ("diameter", "height"):
            if key not in value:
                _fail(f"{path}.{key}", "champ obligatoire pour un brut cylindrique.")
        clean["diameter"] = _number(value["diameter"], f"{path}.diameter", CAM_BOUNDS["length_mm"], factor=factor)
        clean["height"] = _number(value["height"], f"{path}.height", CAM_BOUNDS["length_mm"], factor=factor)
        if clean["diameter"] * factor > min(machine["travel"]["x"], machine["travel"]["y"]) + 1e-9:
            _fail(f"{path}.diameter", "diamètre du brut plus grand que la course X ou Y de la machine.")
        if clean["height"] * factor > machine["travel"]["z"] + 1e-9:
            _fail(f"{path}.height", f"brut plus haut que la course Z de la machine ({machine['travel']['z']:g} mm).")
        stock_height = clean["height"]
    else:
        for key in ("diameter", "height"):
            if value.get(key) is not None:
                _fail(f"{path}.{key}", "réservé au brut cylindrique (shape = cylinder).")
        clean["diameter"] = None
        clean["height"] = None
        stock_height = size["z"] if mode == "explicit" else None
    if heights is not None and stock_height is not None and (stock_height + heights["clearance"]) * factor > machine["travel"]["z"] + 1e-9:
        _fail("heights.clearance", f"brut ({stock_height:g}) + dégagement ({heights['clearance']:g}) dépassent la course Z "
                                   f"de la machine ({machine['travel']['z']:g} mm).")
    placement = _choice(value.get("placement", "center_top"), f"{path}.placement", STOCK_PLACEMENTS)
    offset = value.get("placement_offset")
    if placement == "custom":
        if offset is None:
            _fail(f"{path}.placement_offset", "décalage {x, y, z} obligatoire pour un placement « custom ».")
        clean["placement_offset"] = _xyz(offset, f"{path}.placement_offset", CAM_BOUNDS["coordinate_mm"], factor)
    else:
        if offset is not None:
            _fail(f"{path}.placement_offset", "réservé au placement « custom ».")
        clean["placement_offset"] = None
    clean["placement"] = placement
    if "material_id" in value and value.get("material_ref") is not None:
        _fail(f"{path}.material_id", "alias v1 : ne pas le combiner avec material_ref.")
    material = value.get("material_ref")
    if material is None and value.get("material_id") not in (None, ""):
        material = {"kind": "catalog", "id": value["material_id"]}
    if material is not None:
        _keys(material, f"{path}.material_ref", ("kind", "id"))
        kind = _choice(material["kind"], f"{path}.material_ref.kind", MATERIAL_REF_KINDS)
        pattern = _LIBRARY_MATERIAL if kind == "library" else _PROFILE_ID
        material = {"kind": kind, "id": _identifier(material["id"], f"{path}.material_ref.id", pattern)}
    clean["material_ref"] = material
    clean["material_label"] = _text(value.get("material_label", ""), f"{path}.material_label", allow_empty=True)
    return clean


def _origin(value, factor):
    value = {"preset": "top_front_left"} if value is None else value
    path = "origin"
    _keys(value, path, ("preset",), ("offset", "wcs"))
    return {"preset": _choice(value["preset"], f"{path}.preset", ORIGIN_PRESETS),
            "offset": _xyz(value.get("offset", {"x": 0, "y": 0, "z": 0}), f"{path}.offset",
                           CAM_BOUNDS["coordinate_mm"], factor),
            "wcs": _choice(value.get("wcs", "G54"), f"{path}.wcs", WCS_CODES)}


def _heights(value, factor, machine):
    path = "heights"
    _keys(value, path, ("clearance", "safe", "retract"), ("tolerance",))
    clean = {key: _number(value[key], f"{path}.{key}", CAM_BOUNDS["height_mm"], factor=factor)
             for key in ("clearance", "safe", "retract")}
    clean["tolerance"] = _number(value.get("tolerance", 0.01 / factor), f"{path}.tolerance",
                                 CAM_BOUNDS["tolerance_mm"], factor=factor)
    if not clean["clearance"] >= clean["safe"] >= clean["retract"] > 0:
        _fail(path, "ordre attendu : dégagement ≥ sécurité ≥ retrait > 0.")
    if clean["safe"] * factor + 1e-9 < machine["safe_height_min"]:
        _fail(f"{path}.safe", f"hauteur de sécurité inférieure au minimum de la machine "
                              f"({machine['safe_height_min']:g} mm).")
    return clean


def _fixtures(value, factor):
    path = "fixtures"
    items = _list([] if value is None else value, path, CAM_BOUNDS["fixtures_max"])
    clean = []
    for index, item in enumerate(items):
        where = f"{path}[{index}]"
        _keys(item, where, ("min", "max"), ("kind", "name"))
        low = _xyz(item["min"], f"{where}.min", CAM_BOUNDS["coordinate_mm"], factor)
        high = _xyz(item["max"], f"{where}.max", CAM_BOUNDS["coordinate_mm"], factor)
        for axis in ("x", "y", "z"):
            if high[axis] <= low[axis]:
                _fail(f"{where}.max.{axis}", "le maximum doit dépasser le minimum.")
        clean.append({"kind": _choice(item.get("kind", "box"), f"{where}.kind", FIXTURE_KINDS),
                      "name": _text(item.get("name", f"Bride {index + 1}"), f"{where}.name"),
                      "min": low, "max": high})
    return clean


_TOOL_V2_OPTIONAL = ("cutting_edge_angle", "tip_diameter", "crest", "neck_diameter", "neck_length", "holder_diameter",
                     "holder_length", "source_ref")
_ANGLE_FAMILIES = ("vbit", "chamfer", "engraver")


def _tools(value, factor, machine, version=1):
    path = "tools"
    items = _list(value, path, CAM_BOUNDS["tools_max"], minimum=1)
    required = ("number", "name", "family", "diameter", "flute_length", "spindle_rpm", "feed_xy", "feed_plunge")
    optional = ("shank_diameter", "length", "stickout", "flutes", "material", "coating", "max_rpm", "chip_load",
                "feed_ramp", "step_down", "stepover_pct", "coolant", "length_offset", "tip_angle", "corner_radius")
    if version == 2:
        optional = optional + _TOOL_V2_OPTIONAL
    supported = TOOL_FAMILIES_SUPPORTED_V2 if version == 2 else TOOL_FAMILIES_SUPPORTED
    clean, numbers = [], set()
    for index, item in enumerate(items):
        where = f"{path}[{index}]"
        if isinstance(item, dict) and isinstance(item.get("family"), str):
            family = item["family"]
            if family in TOOL_FAMILIES_ALL and family not in supported:
                if version == 2:
                    _fail(f"{where}.family", f"la famille d'outil « {family} » est refusée : {TOOL_FAMILY_REFUSED_V2[family]}.")
                _fail(f"{where}.family", f"la famille d'outil « {family} » n'est pas encore vérifiée : "
                                         "fraise droite (endmill) ou foret (drill) seulement dans cette version.")
        _keys(item, where, required, optional)
        number = _number(item["number"], f"{where}.number", CAM_BOUNDS["tool_number"], integer=True)
        if number in numbers:
            _fail(f"{where}.number", "deux outils portent le même numéro.")
        numbers.add(number)
        family = _choice(item["family"], f"{where}.family", supported)
        diameter = _number(item["diameter"], f"{where}.diameter", CAM_BOUNDS["tool_diameter_mm"], factor=factor)
        flute = _number(item["flute_length"], f"{where}.flute_length", CAM_BOUNDS["flute_length_mm"], factor=factor)
        length = _number(item.get("length", max(flute * 2.0, 1.0 / factor)), f"{where}.length",
                         CAM_BOUNDS["tool_length_mm"], factor=factor)
        if flute > length:
            _fail(f"{where}.flute_length", "longueur taillée supérieure à la longueur de l'outil.")
        max_rpm = _number(item.get("max_rpm", machine["spindle"]["max_rpm"]), f"{where}.max_rpm",
                          CAM_BOUNDS["spindle_rpm"])
        rpm = _number(item["spindle_rpm"], f"{where}.spindle_rpm", CAM_BOUNDS["spindle_rpm"])
        _check_rpm(rpm, f"{where}.spindle_rpm", max_rpm, machine)
        feeds = {}
        for key in ("feed_xy", "feed_plunge", "feed_ramp"):
            raw = item.get(key) if key == "feed_ramp" else item[key]
            feeds[key] = _optional_number(raw, f"{where}.{key}", CAM_BOUNDS["feed_mm_min"], factor=factor)
            if feeds[key] is not None:
                _check_feed(feeds[key] * factor, f"{where}.{key}", machine)
        step_down = _number(item.get("step_down", min(diameter / 2.0, flute)), f"{where}.step_down",
                            CAM_BOUNDS["length_mm"], factor=factor)
        if step_down > flute + 1e-9:
            _fail(f"{where}.step_down", "profondeur de passe supérieure à la longueur taillée.")
        corner = _number(item.get("corner_radius", 0), f"{where}.corner_radius", (0.0, 50.0), factor=factor)
        if corner > diameter / 2.0 + 1e-9:
            _fail(f"{where}.corner_radius", "rayon de coin supérieur au rayon de l'outil.")
        entry = {
            "number": number, "name": _text(item["name"], f"{where}.name"), "family": family,
            "diameter": diameter,
            "shank_diameter": _number(item.get("shank_diameter", diameter), f"{where}.shank_diameter",
                                      CAM_BOUNDS["tool_diameter_mm"], factor=factor),
            "length": length, "flute_length": flute,
            "stickout": _optional_number(item.get("stickout"), f"{where}.stickout", CAM_BOUNDS["tool_length_mm"],
                                         factor=factor),
            "flutes": _number(item.get("flutes", 2), f"{where}.flutes", CAM_BOUNDS["flutes"], integer=True),
            "material": _choice(item.get("material", "carbide"), f"{where}.material", TOOL_MATERIALS),
            "coating": _text(item.get("coating", ""), f"{where}.coating", allow_empty=True, limit=40),
            "max_rpm": max_rpm,
            "chip_load": _optional_number(item.get("chip_load"), f"{where}.chip_load", CAM_BOUNDS["chip_load_mm"],
                                          factor=factor),
            "spindle_rpm": rpm, "feed_xy": feeds["feed_xy"], "feed_plunge": feeds["feed_plunge"],
            "feed_ramp": feeds["feed_ramp"], "step_down": step_down,
            "stepover_pct": _number(item.get("stepover_pct", 40), f"{where}.stepover_pct", CAM_BOUNDS["stepover_pct"]),
            "coolant": _choice(item.get("coolant", "off"), f"{where}.coolant", OPERATION_COOLANT),
            "length_offset": None if item.get("length_offset") is None else _number(
                item["length_offset"], f"{where}.length_offset", CAM_BOUNDS["length_offset"], integer=True),
            "tip_angle": _number(item.get("tip_angle", 118 if family == "drill" else 0), f"{where}.tip_angle",
                                 CAM_BOUNDS["tip_angle_deg"]),
            "corner_radius": corner,
        }
        if entry["coolant"] != "off" and entry["coolant"] not in machine["coolant"]:
            _fail(f"{where}.coolant", "arrosage absent de la machine.")
        if version == 2:
            entry.update(_tool_shape_v2(item, where, family, diameter, flute, factor))
        clean.append(entry)
    return clean


def _tool_shape_v2(item, where, family, diameter, flute, factor):
    """Paramètres de forme v2 : exigés par certaines familles, refusés pour les autres (aucun champ muet)."""
    clean = {}
    angle = item.get("cutting_edge_angle")
    if family in _ANGLE_FAMILIES:
        if angle is None:
            _fail(f"{where}.cutting_edge_angle", f"angle de coupe obligatoire pour un outil « {family} » "
                                                 "(CuttingEdgeAngle : Vcarve, Deburr, fraise conique OCL).")
        clean["cutting_edge_angle"] = _number(angle, f"{where}.cutting_edge_angle", CAM_BOUNDS["cutting_edge_angle_deg"])
        clean["tip_diameter"] = _number(item.get("tip_diameter", 0), f"{where}.tip_diameter", CAM_BOUNDS["tip_diameter_mm"],
                                        factor=factor)
        if clean["tip_diameter"] >= diameter:
            _fail(f"{where}.tip_diameter", "diamètre de pointe supérieur ou égal au diamètre de l'outil.")
    elif family == "threadmill":
        clean["cutting_edge_angle"] = _number(60 if angle is None else angle, f"{where}.cutting_edge_angle",
                                              CAM_BOUNDS["cutting_edge_angle_deg"])
        clean["tip_diameter"] = _neutral(item, "tip_diameter", where, None, "réservé aux V-bit, chanfreins et graveurs")
    else:
        clean["cutting_edge_angle"] = _neutral(item, "cutting_edge_angle", where, None,
                                               "réservé aux V-bit, chanfreins, graveurs et fraises à fileter")
        clean["tip_diameter"] = _neutral(item, "tip_diameter", where, None, "réservé aux V-bit, chanfreins et graveurs")
    if family == "threadmill":
        clean["crest"] = _number(item.get("crest", 0.1 / factor), f"{where}.crest", CAM_BOUNDS["crest_mm"], factor=factor)
        clean["neck_diameter"] = _number(item.get("neck_diameter", diameter * 0.6), f"{where}.neck_diameter",
                                         CAM_BOUNDS["tool_diameter_mm"], factor=factor)
        if clean["neck_diameter"] >= diameter:
            _fail(f"{where}.neck_diameter", "diamètre de col supérieur ou égal au diamètre de l'outil.")
        clean["neck_length"] = _number(item.get("neck_length", flute), f"{where}.neck_length", CAM_BOUNDS["tool_length_mm"],
                                       factor=factor)
    else:
        for key in ("crest", "neck_diameter", "neck_length"):
            clean[key] = _neutral(item, key, where, None, "réservé aux fraises à fileter")
    if family == "bullnose" and item.get("corner_radius", 0) in (0, None):
        _fail(f"{where}.corner_radius", "rayon de coin obligatoire (> 0) pour une fraise torique (CornerRadius).")
    holder_d = _optional_number(item.get("holder_diameter"), f"{where}.holder_diameter", CAM_BOUNDS["holder_diameter_mm"],
                                factor=factor)
    holder_l = _optional_number(item.get("holder_length"), f"{where}.holder_length", CAM_BOUNDS["holder_length_mm"],
                                factor=factor)
    if (holder_d is None) != (holder_l is None):
        _fail(f"{where}.holder_diameter", "diamètre et longueur du porte-outil vont ensemble.")
    clean["holder_diameter"], clean["holder_length"] = holder_d, holder_l
    source_ref = item.get("source_ref")
    if source_ref is not None:
        _keys(source_ref, f"{where}.source_ref", ("kind", "id"))
        _choice(source_ref["kind"], f"{where}.source_ref.kind", ("library",))
        source_ref = {"kind": "library", "id": _identifier(source_ref["id"], f"{where}.source_ref.id", _LIBRARY_TOOL)}
    clean["source_ref"] = source_ref
    return clean


def _check_rpm(rpm, path, tool_max, machine):
    if rpm <= 0:
        _fail(path, "vitesse de broche nulle : une fraise doit tourner pour couper.")
    if rpm > tool_max + 1e-9:
        _fail(path, f"vitesse de broche au-dessus du maximum de l'outil ({tool_max:g} tr/min).")
    if rpm > machine["spindle"]["max_rpm"] + 1e-9:
        _fail(path, f"vitesse de broche au-dessus du maximum de la machine ({machine['spindle']['max_rpm']:g} tr/min).")
    if rpm + 1e-9 < machine["spindle"]["min_rpm"]:
        _fail(path, f"vitesse de broche sous le minimum de la machine ({machine['spindle']['min_rpm']:g} tr/min).")


def _check_feed(feed_mm, path, machine):
    if feed_mm > machine["max_feed"] + 1e-9:
        _fail(path, f"avance au-dessus du maximum de la machine ({machine['max_feed']:g} mm/min).")


_COMMON_OP = ("id", "type")
_COMMON_OP_OPTIONAL = ("name", "enabled", "operator_note")
_MACHINING_REQUIRED = ("tool", "depth")
_MACHINING_OPTIONAL = ("selection", "stepover_pct", "stock_to_leave", "tolerance", "direction", "entry", "ramp_angle",
                       "lead_in", "lead_out", "feed_xy", "feed_plunge", "spindle_rpm", "spindle_dir", "coolant",
                       "depends_on", "pause_before")
_TYPE_EXTRA = {"facing": (), "pocket": (), "profile": ("side", "tabs", "compensation"), "drilling": ("peck", "dwell")}
_DEFAULT_NAMES = {"facing": "Surfaçage", "pocket": "Poche", "profile": "Contour", "drilling": "Perçage",
                  "adaptive": "Ébauche adaptative", "engrave": "Gravure", "vcarve": "Gravure en V", "helix": "Hélice",
                  "slot": "Rainure", "surface": "Surface 3D", "waterline": "Lignes de niveau", "deburr": "Ébavurage",
                  "thread_milling": "Filetage à la fraise", "custom": "G-code personnalisé",
                  "pause": "Pause", "tool_change": "Changement d'outil", "note": "Consigne"}


def _operations(value, factor, machine, tools, heights, version=1, sequence=None):
    path = "operations"
    items = _list(value, path, CAM_BOUNDS["operations_max"], minimum=1)
    by_number = {tool["number"]: tool for tool in tools}
    supported = OPERATION_TYPES_SUPPORTED_V2 if version == 2 else OPERATION_TYPES_SUPPORTED
    machining = MACHINING_TYPES_V2 if version == 2 else MACHINING_TYPES
    common_optional = _COMMON_OP_OPTIONAL + (("description", "phase", "depends_on", "pause_before") if version == 2 else ())
    clean, defined = [], []
    for index, item in enumerate(items):
        where = f"{path}[{index}]"
        if not isinstance(item, dict):
            _fail(where, "objet attendu.")
        kind = item.get("type")
        if isinstance(kind, str) and kind in OPERATION_TYPES_ALL and kind not in supported:
            if version == 2:
                _fail(f"{where}.type", f"l'opération « {kind} » n'existe pas dans le contrat v2.")
            _fail(f"{where}.type", f"l'opération « {kind} » n'est pas encore prise en charge dans cette version.")
        kind = _choice(kind, f"{where}.type", supported)
        if version == 1:
            if kind in MACHINING_TYPES:
                _keys(item, where, _COMMON_OP + _MACHINING_REQUIRED, _COMMON_OP_OPTIONAL + _MACHINING_OPTIONAL + _TYPE_EXTRA[kind])
            elif kind == "tool_change":
                _keys(item, where, _COMMON_OP + ("tool",), _COMMON_OP_OPTIONAL)
            elif kind == "note":
                _keys(item, where, _COMMON_OP + ("operator_note",), _COMMON_OP_OPTIONAL)
            else:
                _keys(item, where, _COMMON_OP, _COMMON_OP_OPTIONAL)
        else:
            if kind in MACHINING_TYPES_V2:
                spec = _V2_SPECS[kind]
                _keys(item, where, _COMMON_OP + spec["required"], common_optional + spec["optional"])
            elif kind == "tool_change":
                _keys(item, where, _COMMON_OP + ("tool",), common_optional + ("go_to_change_position",))
            elif kind == "note":
                _keys(item, where, _COMMON_OP + ("operator_note",), common_optional)
            else:
                _keys(item, where, _COMMON_OP, common_optional + ("stop_spindle",))
        identifier = _identifier(item["id"], f"{where}.id")
        if identifier in defined:
            _fail(f"{where}.id", "deux opérations portent le même identifiant.")
        entry = {"id": identifier, "type": kind,
                 "name": _text(item.get("name", _DEFAULT_NAMES[kind]), f"{where}.name"),
                 "enabled": _bool(item.get("enabled", True), f"{where}.enabled"),
                 "operator_note": _note(item.get("operator_note", ""), f"{where}.operator_note")}
        if kind == "note" and not entry["operator_note"]:
            _fail(f"{where}.operator_note", "une consigne opérateur ne peut pas être vide.")
        if kind in machining or kind == "tool_change":
            number = _number(item["tool"], f"{where}.tool", CAM_BOUNDS["tool_number"], integer=True)
            if number not in by_number:
                _fail(f"{where}.tool", f"outil n°{number} absent de la bibliothèque du projet.")
            entry["tool"] = number
        if version == 1:
            if kind in MACHINING_TYPES:
                entry.update(_machining(item, where, kind, factor, machine, by_number[entry["tool"]], heights, defined))
        else:
            entry["description"] = _note(item.get("description", ""), f"{where}.description")
            entry["phase"] = _choice(item.get("phase", DEFAULT_PHASES[kind]), f"{where}.phase", PHASES)
            entry["depends_on"] = _depends_on(item.get("depends_on", []), where, defined)
            entry["pause_before"] = _bool(item.get("pause_before", False), f"{where}.pause_before")
            if kind in MACHINING_TYPES_V2:
                entry.update(_machining_v2(item, where, kind, factor, machine, by_number[entry["tool"]], heights, sequence))
            elif kind == "pause":
                entry["stop_spindle"] = _bool(item.get("stop_spindle", True), f"{where}.stop_spindle")
            elif kind == "tool_change":
                go = _bool(item.get("go_to_change_position", False), f"{where}.go_to_change_position")
                position = machine.get("tool_change_position")
                if go and not (position and position.get("verified")):
                    _fail(f"{where}.go_to_change_position", "aucune position de changement d'outil configurée ET vérifiée "
                                                             "sur la machine (machine.tool_change_position.verified).")
                entry["go_to_change_position"] = go
        clean.append(entry)
        defined.append(identifier)
    if not any(op["type"] in machining and op["enabled"] for op in clean):
        _fail(path, "aucune opération d'usinage activée.")
    if version == 2 and sequence and sequence["tool_change_policy"] == "atc_m6":
        used = {op["tool"] for op in clean if op.get("tool") is not None and op["enabled"]}
        if len(used) > machine["tool_count"]:
            _fail("sequence.tool_change_policy", f"{len(used)} outils distincts pour un changeur de {machine['tool_count']} "
                                                 "poste(s) (machine.tool_count).")
    return clean


def _depends_on(value, where, defined):
    depends = _list(value, f"{where}.depends_on", CAM_BOUNDS["operations_max"])
    for position, reference in enumerate(depends):
        if not isinstance(reference, str) or reference not in defined:
            _fail(f"{where}.depends_on[{position}]", "une opération ne peut dépendre que d'une opération définie avant elle.")
    if len(set(depends)) != len(depends):
        _fail(f"{where}.depends_on", "dépendance en double.")
    return list(depends)


def _machining(item, where, kind, factor, machine, tool, heights, defined):
    """Réglages d'usinage du contrat v1 (inchangés)."""
    if tool["family"] not in OPERATION_TOOL_FAMILIES[kind]:
        _fail(f"{where}.tool", f"un outil « {tool['family']} » ne convient pas à l'opération « {kind} ».")
    selection = item.get("selection", {"mode": "auto"})
    selection_keys = ("min_diameter", "max_diameter") if kind == "drilling" else ()
    _keys(selection, f"{where}.selection", ("mode",), selection_keys)
    clean_selection = {"mode": _choice(selection["mode"], f"{where}.selection.mode", SELECTION_MODES)}
    if kind == "drilling":
        low = _number(selection.get("min_diameter", 0.5 / factor), f"{where}.selection.min_diameter",
                      CAM_BOUNDS["length_mm"], factor=factor)
        high = _number(selection.get("max_diameter", 100.0 / factor), f"{where}.selection.max_diameter",
                       CAM_BOUNDS["length_mm"], factor=factor)
        if low > high:
            _fail(f"{where}.selection", "diamètre minimal supérieur au maximal.")
        clean_selection.update(min_diameter=low, max_diameter=high)
    depth = _keys(item["depth"], f"{where}.depth", ("final",), ("start", "step_down"))
    final = _number(depth["final"], f"{where}.depth.final", CAM_BOUNDS["length_mm"], factor=factor)
    start = _optional_number(depth.get("start"), f"{where}.depth.start", (0.0, 3000.0), factor=factor)
    if start is not None and start >= final:
        _fail(f"{where}.depth.start", "la profondeur de départ doit être au-dessus de la profondeur finale.")
    if final > tool["flute_length"] + 1e-9:
        _fail(f"{where}.depth.final", f"profondeur ({final:g}) supérieure à la longueur taillée de l'outil "
                                      f"n°{tool['number']} ({tool['flute_length']:g}).")
    step_down = _number(depth.get("step_down", min(tool["step_down"], final)), f"{where}.depth.step_down",
                        CAM_BOUNDS["length_mm"], factor=factor)
    if step_down > final + 1e-9:
        _fail(f"{where}.depth.step_down", "profondeur de passe supérieure à la profondeur finale.")
    if step_down > tool["flute_length"] + 1e-9:
        _fail(f"{where}.depth.step_down", "profondeur de passe supérieure à la longueur taillée.")
    leave = item.get("stock_to_leave", {"radial": 0, "axial": 0})
    _keys(leave, f"{where}.stock_to_leave", ("radial", "axial"))
    entry = {
        "selection": clean_selection,
        "depth": {"start": start, "final": final, "step_down": step_down},
        "stepover_pct": _number(item["stepover_pct"], f"{where}.stepover_pct", CAM_BOUNDS["stepover_pct"])
        if item.get("stepover_pct") is not None else tool["stepover_pct"],
        "stock_to_leave": {key: _number(leave[key], f"{where}.stock_to_leave.{key}", CAM_BOUNDS["stock_to_leave_mm"],
                                        factor=factor) for key in ("radial", "axial")},
        "tolerance": _number(item.get("tolerance", heights["tolerance"]), f"{where}.tolerance",
                             CAM_BOUNDS["tolerance_mm"], factor=factor),
        "direction": _choice(item.get("direction", "climb"), f"{where}.direction", CUT_DIRECTIONS),
        "entry": _choice(item.get("entry", "plunge"), f"{where}.entry", ENTRY_MODES),
        "ramp_angle": _number(item.get("ramp_angle", 3), f"{where}.ramp_angle", CAM_BOUNDS["angle_deg"]),
        "lead_in": _number(item.get("lead_in", 0), f"{where}.lead_in", (0.0, 3000.0), factor=factor),
        "lead_out": _number(item.get("lead_out", 0), f"{where}.lead_out", (0.0, 3000.0), factor=factor),
        "spindle_dir": _choice(item.get("spindle_dir", "cw"), f"{where}.spindle_dir", SPINDLE_DIRECTIONS),
        "coolant": _choice(item.get("coolant", "off"), f"{where}.coolant", OPERATION_COOLANT),
        "pause_before": _bool(item.get("pause_before", False), f"{where}.pause_before"),
    }
    if entry["spindle_dir"] not in machine["spindle"]["directions"]:
        _fail(f"{where}.spindle_dir", "sens de rotation non pris en charge par la broche de la machine.")
    if entry["coolant"] != "off" and entry["coolant"] not in machine["coolant"]:
        _fail(f"{where}.coolant", "arrosage absent de la machine.")
    for key in ("feed_xy", "feed_plunge"):
        entry[key] = _optional_number(item.get(key), f"{where}.{key}", CAM_BOUNDS["feed_mm_min"], factor=factor)
        if entry[key] is not None:
            _check_feed(entry[key] * factor, f"{where}.{key}", machine)
    entry["spindle_rpm"] = _optional_number(item.get("spindle_rpm"), f"{where}.spindle_rpm", CAM_BOUNDS["spindle_rpm"])
    if entry["spindle_rpm"] is not None:
        _check_rpm(entry["spindle_rpm"], f"{where}.spindle_rpm", tool["max_rpm"], machine)
    entry["depends_on"] = _depends_on(item.get("depends_on", []), where, defined)
    if kind == "profile":
        entry["side"] = _choice(item.get("side", "outside"), f"{where}.side", PROFILE_SIDES)
        entry["compensation"] = _choice(item.get("compensation", "none"), f"{where}.compensation", COMPENSATION_MODES)
        tabs = item.get("tabs")
        if tabs is None:
            entry["tabs"] = {"count": 0, "width": 0.0, "height": 0.0}
        else:
            _keys(tabs, f"{where}.tabs", ("count",), ("width", "height"))
            count = _number(tabs["count"], f"{where}.tabs.count", CAM_BOUNDS["tab_count"], integer=True)
            width = _number(tabs.get("width", 6.0 / factor), f"{where}.tabs.width", CAM_BOUNDS["length_mm"], factor=factor)
            height = _number(tabs.get("height", 2.0 / factor), f"{where}.tabs.height", CAM_BOUNDS["length_mm"], factor=factor)
            if count and height >= final:
                _fail(f"{where}.tabs.height", "pont plus haut que la profondeur de coupe.")
            entry["tabs"] = {"count": count, "width": width, "height": height}
    if kind == "drilling":
        peck = _number(item.get("peck", 0), f"{where}.peck", (0.0, 3000.0), factor=factor)
        if peck and peck > final:
            _fail(f"{where}.peck", "débourrage plus profond que le perçage.")
        entry["peck"] = peck
        entry["dwell"] = _number(item.get("dwell", 0), f"{where}.dwell", CAM_BOUNDS["dwell_s"])
    return entry


# ---------------------------------------------------------------------------------------------
# Contrat v2 des opérations : réglages communs d'usinage + réglages propres (docs/cam_projet_v2.md § 6).
# Chaque réglage propre : (genre, bornes/choix, défaut). Genres : choice, bool, number, length (unités projet), int.
# ---------------------------------------------------------------------------------------------
_V2_COMMON_OPTIONAL = ("selection", "stepover_pct", "stock_to_leave", "tolerance", "direction", "feed_xy", "feed_plunge",
                       "feed_ramp", "spindle_rpm", "spindle_dir", "coolant", "keep_tool_down", "min_travel", "heights",
                       "dressups")


def _spec(own, *, step_down=True, stepover=True, stepover_default=None, finish=False, radial=False, axial=False,
          tolerance=False, tolerance_default=None, direction=False, keep_tool_down=False, min_travel=False,
          diameters=False, required=()):
    return {"required": _MACHINING_REQUIRED + tuple(required), "optional": _V2_COMMON_OPTIONAL + tuple(own),
            "own": own, "step_down": step_down, "stepover": stepover, "stepover_default": stepover_default,
            "finish": finish, "radial": radial, "axial": axial, "tolerance": tolerance,
            "tolerance_default": tolerance_default, "direction": direction, "keep_tool_down": keep_tool_down,
            "min_travel": min_travel, "diameters": diameters}


_V2_SPECS = {
    "facing": _spec({
        "boundary": ("choice", FACING_BOUNDARIES, "stock"), "pattern": ("choice", CLEARING_PATTERNS, "zigzag"),
        "zigzag_angle": ("number", "zigzag_angle_deg", 45), "clear_edges": ("bool", None, True),
        "start_at": ("choice", START_AT, "center"), "exclude_raised": ("bool", None, False),
        "rest_machining": ("bool", None, False)}, finish=True, radial=True, direction=True, keep_tool_down=True,
        min_travel=True),
    "pocket": _spec({
        "pattern": ("choice", CLEARING_PATTERNS, "zigzag"), "zigzag_angle": ("number", "zigzag_angle_deg", 45),
        "start_at": ("choice", START_AT, "center"), "rest_machining": ("bool", None, False)},
        finish=True, radial=True, direction=True, keep_tool_down=True, min_travel=True),
    "profile": _spec({
        "side": ("choice", PROFILE_SIDES, "outside"), "use_comp": ("bool", None, True),
        "passes": ("int", "pass_count", 1), "pass_stepover": ("length_or_null", "length_mm", None),
        "join_type": ("choice", JOIN_TYPES, "round"), "process_perimeter": ("bool", None, True),
        "process_holes": ("bool", None, False), "process_circles": ("bool", None, False),
        "handle_multiple": ("choice", HANDLE_MULTIPLE, "collectively")}, radial=True, direction=True),
    "drilling": _spec({
        "peck": ("length", (0.0, 3000.0), 0), "dwell": ("number", "dwell_s", 0),
        "retract_mode": ("choice", RETRACT_MODES, "G98"), "retract_height": ("length_or_null", "retract_height_mm", None),
        "extra_offset": ("choice", DRILL_EXTRA_OFFSETS, "none")}, step_down=False, stepover=False, keep_tool_down=True,
        diameters=True),
    "adaptive": _spec({
        "operation_type": ("choice", ADAPTIVE_OPERATION_TYPES, "clearing"), "side": ("choice", PROFILE_SIDES, "inside"),
        "helix_angle": ("number", "helix_angle_deg", 5), "helix_cone_angle": ("number", (0.0, 89.0), 0),
        "helix_diameter_limit": ("length", (0.0, 3000.0), 0), "use_helix_arcs": ("bool", None, False),
        "lift_distance": ("length", "lift_distance_mm", 0), "keep_tool_down_ratio": ("number", (0.0, 10.0), 3),
        "finishing_profile": ("bool", None, True), "force_inside_out": ("bool", None, True),
        "order_by_region": ("bool", None, True)}, stepover_default=20, radial=True, axial=True, tolerance=True,
        tolerance_default=0.1),
    "engrave": _spec({"start_vertex": ("int", "vertex_index", 0)}, stepover=False),
    "vcarve": _spec({
        "discretize": ("length", "discretize_mm", 0.25), "colinear": ("number", "colinear_deg", 10),
        "finishing_pass": ("bool", None, False), "finishing_pass_z_offset": ("length", (0.0, 10.0), 0),
        "optimize_movements": ("bool", None, True)}, stepover=False, tolerance=True, tolerance_default=0.01),
    "helix": _spec({
        "start_side": ("choice", PROFILE_SIDES, "inside"), "start_radius": ("length", (0.0, 100.0), 0),
        "path_direction": ("choice", PATH_DIRECTIONS, "cw")}, diameters=True),
    "slot": _spec({
        "pattern": ("choice", SLOT_PATTERNS, "line"), "layer_mode": ("choice", LAYER_MODES, "multi"),
        "extend_start": ("length", "extend_mm", 0), "extend_end": ("length", "extend_mm", 0),
        "reverse": ("bool", None, False)}, stepover=False),
    "surface": _spec({
        "requires": ("requires", None, ["ocl"]), "bound_box": ("choice", ("base", "stock"), "stock"),
        "scan_type": ("choice", SURFACE_SCAN_TYPES, "planar"), "pattern": ("choice", SURFACE_PATTERNS, "line"),
        "pattern_angle": ("number", "pattern_angle_deg", 0), "layer_mode": ("choice", LAYER_MODES, "single"),
        "sample_interval": ("length", "sample_interval_mm", 1), "drop_cutter_dir": ("choice", DROP_CUTTER_DIRS, "x"),
        "profile_edges": ("choice", PROFILE_EDGES, "none"), "boundary_enforcement": ("bool", None, True),
        "boundary_adjustment": ("length", "boundary_adjustment_mm", 0), "internal_features_cut": ("bool", None, True),
        "optimize_linear_paths": ("bool", None, True), "optimize_stepover_transitions": ("bool", None, False),
        "circular_use_g2g3": ("bool", None, False), "linear_deflection": ("length", "linear_deflection_mm", 0.0025)},
        stepover_default=50),
    "waterline": _spec({
        "requires": ("requires", None, ["ocl"]), "algorithm": ("choice", WATERLINE_ALGORITHMS, "ocl_dropcutter"),
        "bound_box": ("choice", ("stock",), "stock"), "layer_mode": ("choice", ("multi",), "multi"),
        "clear_last_layer": ("bool", None, False), "pattern": ("choice", SURFACE_PATTERNS, "line"),
        "ignore_outer_above": ("length", (0.0, 3000.0), 0), "sample_interval": ("length", "sample_interval_mm", 1),
        "boundary_enforcement": ("bool", None, True), "boundary_adjustment": ("length", "boundary_adjustment_mm", 0),
        "internal_features_cut": ("bool", None, True)}, stepover_default=50),
    "deburr": _spec({
        "width": ("length", "deburr_width_mm", 1), "extra_depth": ("length", "extra_depth_mm", 0.5),
        "join": ("choice", DEBURR_JOINS, "round"), "path_direction": ("choice", PATH_DIRECTIONS, "cw"),
        "side": ("choice", PROFILE_SIDES, "outside"), "entry_point": ("int", "vertex_index", 0)},
        step_down=False, stepover=False),
    "thread_milling": _spec({
        "thread": ("thread", None, None), "passes": ("int", (1, 10), 1),
        "orientation": ("choice", THREAD_ORIENTATIONS, "right"), "path_direction": ("choice", PATH_DIRECTIONS, "cw"),
        "lead_in_out": ("bool", None, True)}, step_down=False, stepover=False, diameters=True, required=("thread",)),
    "custom": {"required": ("tool", "gcode_lines"), "optional": ("spindle_rpm", "spindle_dir", "feed_xy", "feed_plunge", "coolant"),
               "own": {}, "custom": True},
}
_V2_SPECS["custom"].update(step_down=False, stepover=False, stepover_default=None, finish=False, radial=False, axial=False,
                           tolerance=False, tolerance_default=None, direction=False, keep_tool_down=False, min_travel=False,
                           diameters=False)


def _own_setting(item, where, key, spec, factor):
    genre, bounds, default = spec
    raw = item.get(key, default)
    path = f"{where}.{key}"
    if genre == "choice":
        return _choice(raw, path, bounds)
    if genre == "bool":
        return _bool(raw, path)
    if genre == "int":
        return _number(raw, path, CAM_BOUNDS[bounds] if isinstance(bounds, str) else bounds, integer=True)
    if genre == "number":
        return _number(raw, path, CAM_BOUNDS[bounds] if isinstance(bounds, str) else bounds)
    if genre == "length":
        limits = CAM_BOUNDS[bounds] if isinstance(bounds, str) else bounds
        if key not in item:
            raw = default / factor
        return _number(raw, path, limits, factor=factor)
    if genre == "length_or_null":
        return _optional_number(raw, path, CAM_BOUNDS[bounds] if isinstance(bounds, str) else bounds, factor=factor)
    if genre == "requires":
        if raw != default:
            _fail(path, f"valeur fixe attendue : {default} (dépendance OpenCAMLib annoncée par le Worker).")
        return list(default)
    raise AssertionError(genre)


def _machining_v2(item, where, kind, factor, machine, tool, heights, sequence):
    spec = _V2_SPECS[kind]
    if tool["family"] not in OPERATION_TOOL_FAMILIES_V2[kind]:
        _fail(f"{where}.tool", f"un outil « {tool['family']} » ne convient pas à l'opération « {kind} » "
                               f"(admis : {', '.join(OPERATION_TOOL_FAMILIES_V2[kind])}).")
    entry = {}
    # Avances, broche, arrosage : communs à toutes les opérations d'usinage, custom compris.
    entry["spindle_dir"] = _choice(item.get("spindle_dir", "cw"), f"{where}.spindle_dir", SPINDLE_DIRECTIONS)
    if entry["spindle_dir"] not in machine["spindle"]["directions"]:
        _fail(f"{where}.spindle_dir", "sens de rotation non pris en charge par la broche de la machine.")
    entry["coolant"] = _choice(item.get("coolant", "off"), f"{where}.coolant", OPERATION_COOLANT)
    if entry["coolant"] != "off" and entry["coolant"] not in machine["coolant"]:
        _fail(f"{where}.coolant", "arrosage absent de la machine.")
    for key in ("feed_xy", "feed_plunge", "feed_ramp"):
        entry[key] = _optional_number(item.get(key), f"{where}.{key}", CAM_BOUNDS["feed_mm_min"], factor=factor)
        if entry[key] is not None:
            _check_feed(entry[key] * factor, f"{where}.{key}", machine)
    entry["spindle_rpm"] = _optional_number(item.get("spindle_rpm"), f"{where}.spindle_rpm", CAM_BOUNDS["spindle_rpm"])
    if entry["spindle_rpm"] is not None:
        _check_rpm(entry["spindle_rpm"], f"{where}.spindle_rpm", tool["max_rpm"], machine)
    if spec.get("custom"):
        entry.pop("feed_ramp")
        lines = _list(item["gcode_lines"], f"{where}.gcode_lines", CAM_BOUNDS["custom_gcode_lines_max"], minimum=1)
        entry["gcode_lines"] = [validate_custom_motion_line(line, f"{where}.gcode_lines[{index}]", factor)
                                for index, line in enumerate(lines)]
        return entry
    # Sélection
    selection = item.get("selection", {"mode": "auto"})
    _keys(selection, f"{where}.selection", ("mode",), ("faces",) + (("min_diameter", "max_diameter") if spec["diameters"] else ()))
    mode = _choice(selection["mode"], f"{where}.selection.mode", SELECTION_MODES_V2)
    clean_selection = {"mode": mode, "faces": []}
    if mode == "faces":
        faces = _list(selection.get("faces", []), f"{where}.selection.faces", CAM_BOUNDS["faces_max"], minimum=1)
        for position, face in enumerate(faces):
            if not isinstance(face, str) or not _FACE.fullmatch(face):
                _fail(f"{where}.selection.faces[{position}]", "nom de face attendu (ex. Face12, issu d'un calcul précédent).")
        if len(set(faces)) != len(faces):
            _fail(f"{where}.selection.faces", "face en double.")
        clean_selection["faces"] = list(faces)
    elif selection.get("faces") not in (None, []):
        _fail(f"{where}.selection.faces", "réservé au mode « faces ».")
    if spec["diameters"]:
        low_default = (tool["diameter"] + 0.1 / factor) if kind in ("helix", "thread_milling") else 0.5 / factor
        low = _number(selection.get("min_diameter", low_default), f"{where}.selection.min_diameter",
                      CAM_BOUNDS["length_mm"], factor=factor)
        high = _number(selection.get("max_diameter", 100.0 / factor), f"{where}.selection.max_diameter",
                       CAM_BOUNDS["length_mm"], factor=factor)
        if low > high:
            _fail(f"{where}.selection", "diamètre minimal supérieur au maximal.")
        if kind in ("helix", "thread_milling") and low <= tool["diameter"] + 1e-9:
            _fail(f"{where}.selection.min_diameter", "un trou à fraiser doit être plus large que l'outil.")
        clean_selection.update(min_diameter=low, max_diameter=high)
    entry["selection"] = clean_selection
    # Profondeurs
    depth = _keys(item["depth"], f"{where}.depth", ("final",), ("start", "step_down", "finish"))
    final = _number(depth["final"], f"{where}.depth.final", CAM_BOUNDS["length_mm"], factor=factor)
    start = _optional_number(depth.get("start"), f"{where}.depth.start", (0.0, 3000.0), factor=factor)
    if start is not None and start >= final:
        _fail(f"{where}.depth.start", "la profondeur de départ doit être au-dessus de la profondeur finale.")
    if final > tool["flute_length"] + 1e-9:
        _fail(f"{where}.depth.final", f"profondeur ({final:g}) supérieure à la longueur taillée de l'outil "
                                      f"n°{tool['number']} ({tool['flute_length']:g}).")
    if spec["step_down"]:
        step_down = _number(depth.get("step_down", min(tool["step_down"], final)), f"{where}.depth.step_down",
                            CAM_BOUNDS["length_mm"], factor=factor)
        if step_down > final + 1e-9:
            _fail(f"{where}.depth.step_down", "profondeur de passe supérieure à la profondeur finale.")
        if step_down > tool["flute_length"] + 1e-9:
            _fail(f"{where}.depth.step_down", "profondeur de passe supérieure à la longueur taillée.")
    else:
        step_down = _neutral(depth, "step_down", f"{where}.depth", None,
                             "cette opération n'a pas de passes en Z : perçage par débourrage, ébavurage ou filetage")
    if spec["finish"]:
        finish = _number(depth.get("finish", 0), f"{where}.depth.finish", CAM_BOUNDS["finish_depth_mm"], factor=factor)
        if finish >= final:
            _fail(f"{where}.depth.finish", "passe de finition plus épaisse que la profondeur finale.")
    else:
        finish = _neutral(depth, "finish", f"{where}.depth", 0, "FinishDepth n'existe que pour le surfaçage et la poche")
    entry["depth"] = {"start": start, "final": final, "step_down": step_down, "finish": float(finish)}
    # Hauteurs par opération
    own_heights = item.get("heights")
    if own_heights is None:
        entry["heights"] = None
    else:
        _keys(own_heights, f"{where}.heights", ("clearance", "safe"))
        clean_heights = {key: _number(own_heights[key], f"{where}.heights.{key}", CAM_BOUNDS["height_mm"], factor=factor)
                         for key in ("clearance", "safe")}
        if clean_heights["clearance"] < clean_heights["safe"]:
            _fail(f"{where}.heights", "dégagement inférieur à la hauteur de sécurité.")
        if clean_heights["safe"] * factor + 1e-9 < machine["safe_height_min"]:
            _fail(f"{where}.heights.safe", f"hauteur de sécurité inférieure au minimum de la machine "
                                           f"({machine['safe_height_min']:g} mm).")
        entry["heights"] = clean_heights
    # Recouvrement, surépaisseur, tolérance, sens de coupe, maintien en bas
    if spec["stepover"]:
        default = spec["stepover_default"] if spec["stepover_default"] is not None else tool["stepover_pct"]
        raw = item.get("stepover_pct")
        entry["stepover_pct"] = _number(default if raw is None else raw, f"{where}.stepover_pct", CAM_BOUNDS["stepover_pct"])
    else:
        entry["stepover_pct"] = _neutral(item, "stepover_pct", where, None, "pas de recouvrement (StepOver) pour cette opération")
    leave = item.get("stock_to_leave", {"radial": 0, "axial": 0})
    _keys(leave, f"{where}.stock_to_leave", (), ("radial", "axial"))
    radial = _number(leave.get("radial", 0), f"{where}.stock_to_leave.radial", CAM_BOUNDS["stock_to_leave_mm"], factor=factor)
    axial = _number(leave.get("axial", 0), f"{where}.stock_to_leave.axial", CAM_BOUNDS["stock_to_leave_mm"], factor=factor)
    if radial and not spec["radial"]:
        _fail(f"{where}.stock_to_leave.radial", "surépaisseur radiale non prise en charge par FreeCAD 1.1.3 pour cette "
                                                "opération (OffsetExtra/ExtraOffset/StockToLeave : surfaçage, poche, "
                                                "contour, adaptative) : laisse 0.")
    if axial and not spec["axial"]:
        _fail(f"{where}.stock_to_leave.axial", "surépaisseur axiale non prise en charge par FreeCAD 1.1.3 hors Adaptive "
                                               "(ZStockToLeave) : laisse 0.")
    entry["stock_to_leave"] = {"radial": radial, "axial": axial}
    if spec["tolerance"]:
        raw = item.get("tolerance")
        entry["tolerance"] = _number(spec["tolerance_default"] / factor if raw is None else raw, f"{where}.tolerance",
                                     CAM_BOUNDS["tolerance_mm"], factor=factor)
    else:
        entry["tolerance"] = _neutral(item, "tolerance", where, None,
                                      "pas de tolérance par opération : heights.tolerance (Job.GeometryTolerance) s'applique")
    direction = _choice(item.get("direction", "climb"), f"{where}.direction", CUT_DIRECTIONS)
    if direction != "climb" and not spec["direction"]:
        _fail(f"{where}.direction", "sens de coupe non réglable pour cette opération en FreeCAD 1.1.3 (CutMode : surfaçage, "
                                    "poche ; Direction déduite du côté pour le contour) : laisse « climb ».")
    entry["direction"] = direction
    entry["keep_tool_down"] = _bool(item.get("keep_tool_down", False), f"{where}.keep_tool_down")
    if entry["keep_tool_down"] and not spec["keep_tool_down"]:
        _fail(f"{where}.keep_tool_down", "KeepToolDown n'existe que pour le surfaçage, la poche et le perçage.")
    entry["min_travel"] = _bool(item.get("min_travel", False), f"{where}.min_travel")
    if entry["min_travel"] and not spec["min_travel"]:
        _fail(f"{where}.min_travel", "MinTravel n'existe que pour le surfaçage et la poche.")
    # Réglages propres
    for key, own in spec["own"].items():
        if own[0] == "thread":
            entry["thread"] = _thread(item.get("thread"), f"{where}.thread", factor, tool)
        else:
            entry[key] = _own_setting(item, where, key, own, factor)
    if kind == "drilling":
        if entry["peck"] > final + 1e-9:
            _fail(f"{where}.peck", "débourrage plus profond que le perçage.")
        if entry["peck"] > 0 and entry["dwell"] > 0:
            _fail(f"{where}.dwell", "débourrage et temporisation ensemble : FreeCAD refuse la combinaison "
                                    "(« Peck and Dwell cannot be used together ») et n'émet aucun cycle.")
    if kind == "profile" and entry["pass_stepover"] is not None and entry["pass_stepover"] > tool["diameter"] + 1e-9:
        _fail(f"{where}.pass_stepover", "pas radial supérieur au diamètre de l'outil.")
    entry["dressups"] = _dressups(item.get("dressups", []), where, kind, entry, final, factor)
    return entry


def _thread(value, path, factor, tool):
    if value is None:
        _fail(path, "bloc de filetage obligatoire (type, nom, diamètres, pas ou tpi) : sans lui FreeCAD produit un parcours "
                    "faux (« minor diameter D=0.0 »).")
    _keys(value, path, ("type", "name", "major_diameter", "minor_diameter"), ("pitch", "tpi", "fit"))
    clean = {"type": _choice(value["type"], f"{path}.type", THREAD_TYPES),
             "name": _text(value["name"], f"{path}.name", pattern=_PLAIN_TEXT, limit=40),
             "major_diameter": _number(value["major_diameter"], f"{path}.major_diameter", CAM_BOUNDS["thread_diameter_mm"], factor=factor),
             "minor_diameter": _number(value["minor_diameter"], f"{path}.minor_diameter", CAM_BOUNDS["thread_diameter_mm"], factor=factor),
             "pitch": _optional_number(value.get("pitch"), f"{path}.pitch", CAM_BOUNDS["pitch_mm"], factor=factor),
             "tpi": _optional_number(value.get("tpi"), f"{path}.tpi", CAM_BOUNDS["tpi"]),
             "fit": _number(value.get("fit", 50), f"{path}.fit", CAM_BOUNDS["thread_fit_pct"])}
    if clean["minor_diameter"] >= clean["major_diameter"]:
        _fail(f"{path}.minor_diameter", "diamètre mineur supérieur ou égal au diamètre majeur.")
    if (clean["pitch"] is None) == (clean["tpi"] is None):
        _fail(f"{path}.pitch", "exactement l'un des deux : pas métrique (pitch) ou filets par pouce (tpi).")
    if tool["diameter"] >= clean["minor_diameter"] - 1e-9:
        _fail(f"{path}.minor_diameter", f"outil (Ø {tool['diameter']:g}) trop gros pour le diamètre mineur "
                                        f"({clean['minor_diameter']:g}) : FreeCAD refuse (« tool too big »).")
    return clean


def _dressups(value, where, kind, entry, final, factor):
    items = _list(value, f"{where}.dressups", CAM_BOUNDS["dressups_max"])
    clean = []
    for index, item in enumerate(items):
        path = f"{where}.dressups[{index}]"
        if not isinstance(item, dict):
            _fail(path, "objet attendu.")
        kind_dressup = item.get("type")
        if kind_dressup in DRESSUP_TYPES_REFUSED:
            _fail(f"{path}.type", f"dressup « {kind_dressup} » refusé : {DRESSUP_TYPES_REFUSED[kind_dressup]}.")
        kind_dressup = _choice(kind_dressup, f"{path}.type", DRESSUP_TYPES)
        if kind not in DRESSUP_OPERATIONS[kind_dressup]:
            _fail(f"{path}.type", f"dressup « {kind_dressup} » non applicable à l'opération « {kind} » "
                                  f"(admis : {', '.join(DRESSUP_OPERATIONS[kind_dressup])}).")
        if any(done["type"] == kind_dressup for done in clean):
            _fail(f"{path}.type", "dressup en double sur la même opération.")
        if kind_dressup == "tags":
            if kind == "adaptive" and entry.get("operation_type") != "profiling":
                _fail(f"{path}.type", "ponts (Tags) seulement sur une adaptative en mode « profiling ».")
            _keys(item, path, ("type", "count"), ("width", "height", "angle", "radius", "segmentation"))
            count = _number(item["count"], f"{path}.count", CAM_BOUNDS["tab_count"], integer=True)
            if count < 1:
                _fail(f"{path}.count", "au moins un pont (sinon retire le dressup).")
            height = _number(item.get("height", 2.0 / factor), f"{path}.height", CAM_BOUNDS["length_mm"], factor=factor)
            if height >= final:
                _fail(f"{path}.height", "pont plus haut que la profondeur de coupe.")
            clean.append({"type": "tags", "count": count,
                          "width": _number(item.get("width", 6.0 / factor), f"{path}.width", CAM_BOUNDS["length_mm"], factor=factor),
                          "height": height, "angle": _number(item.get("angle", 45), f"{path}.angle", CAM_BOUNDS["angle_deg"]),
                          "radius": _number(item.get("radius", 0), f"{path}.radius", (0.0, 50.0), factor=factor),
                          "segmentation": _number(item.get("segmentation", 0), f"{path}.segmentation", (0, 1000), integer=True)})
        elif kind_dressup == "dogbone":
            if kind == "profile" and entry.get("side") != "inside":
                _fail(f"{path}.type", "dogbone sur un contour : côté intérieur seulement (angles internes).")
            _keys(item, path, ("type",), ("style", "incision", "custom_length", "side"))
            incision = _choice(item.get("incision", "adaptive"), f"{path}.incision", DOGBONE_INCISIONS)
            custom = item.get("custom_length")
            if incision == "custom" and custom is None:
                _fail(f"{path}.custom_length", "longueur obligatoire pour une incision « custom ».")
            if incision != "custom" and custom is not None:
                _fail(f"{path}.custom_length", "réservé à l'incision « custom ».")
            clean.append({"type": "dogbone", "style": _choice(item.get("style", "dogbone"), f"{path}.style", DOGBONE_STYLES),
                          "incision": incision,
                          "custom_length": _optional_number(custom, f"{path}.custom_length", CAM_BOUNDS["length_mm"], factor=factor),
                          "side": _choice(item.get("side", "left"), f"{path}.side", DOGBONE_SIDES)})
        else:
            _keys(item, path, ("type",), ("inside",))
            clean.append({"type": "boundary", "inside": _bool(item.get("inside", True), f"{path}.inside")})
    return clean


def _sequence(value, machine):
    value = {} if value is None else value
    path = "sequence"
    _keys(value, path, (), ("tool_change_policy", "spindle_wait_s"))
    policy = _choice(value.get("tool_change_policy", "pause_m0"), f"{path}.tool_change_policy", TOOL_CHANGE_POLICIES)
    if policy == "atc_m6":
        if machine["tool_change"] != "atc":
            _fail(f"{path}.tool_change_policy", "changement d'outil automatique (M6) : la machine ne déclare pas de changeur "
                                                "(machine.tool_change = atc).")
        if machine["controller"].startswith("grbl"):
            _fail(f"{path}.tool_change_policy", "GRBL 1.1 refuse M6 (erreur 20) : utilise pause_m0 ou split_files.")
    return {"tool_change_policy": policy,
            "spindle_wait_s": _number(value.get("spindle_wait_s", 0), f"{path}.spindle_wait_s", CAM_BOUNDS["spindle_wait_s"])}


def _assistant(value):
    if value is None:
        return None
    path = "assistant"
    _keys(value, path, (), ("guided", "confidence_rules_version", "missing_data", "recommendations"))
    clean = {"guided": _bool(value.get("guided", False), f"{path}.guided"),
             "confidence_rules_version": _text(value.get("confidence_rules_version", ""), f"{path}.confidence_rules_version",
                                               pattern=_VERSION_TEXT, limit=40, allow_empty=True),
             "missing_data": [], "recommendations": []}
    for index, item in enumerate(_list(value.get("missing_data", []), f"{path}.missing_data", CAM_BOUNDS["missing_data_max"])):
        where = f"{path}.missing_data[{index}]"
        _keys(item, where, ("field", "consequence"), ("severity",))
        clean["missing_data"].append({
            "field": _text(item["field"], f"{where}.field", pattern=_PLAIN_TEXT, limit=120),
            "consequence": _text(item["consequence"], f"{where}.consequence", pattern=_NOTE, limit=CAM_BOUNDS["note_chars"]),
            "severity": _choice(item.get("severity", "warning"), f"{where}.severity", MISSING_DATA_SEVERITIES)})
    for index, item in enumerate(_list(value.get("recommendations", []), f"{path}.recommendations",
                                       CAM_BOUNDS["recommendations_max"])):
        where = f"{path}.recommendations[{index}]"
        _keys(item, where, ("target", "value", "confidence"), ("unit", "basis", "applied", "at"))
        raw = item["value"]
        if isinstance(raw, bool):
            recommended = raw
        elif isinstance(raw, (int, float)):
            if not math.isfinite(raw):
                _fail(f"{where}.value", "nombre fini attendu.")
            recommended = float(raw)
        else:
            recommended = _text(raw, f"{where}.value", pattern=_PLAIN_TEXT, limit=80)
        basis = _list(item.get("basis", []), f"{where}.basis", 8)
        clean["recommendations"].append({
            "target": _text(item["target"], f"{where}.target", pattern=_PLAIN_TEXT, limit=120),
            "value": recommended,
            "unit": _text(item.get("unit", ""), f"{where}.unit", pattern=_PLAIN_TEXT, limit=16, allow_empty=True),
            "confidence": _choice(item["confidence"], f"{where}.confidence", CONFIDENCE_LEVELS),
            "basis": [_text(text, f"{where}.basis[{position}]", pattern=_PLAIN_TEXT, limit=160) for position, text in enumerate(basis)],
            "applied": _bool(item.get("applied", False), f"{where}.applied"),
            "at": _iso_datetime(item.get("at"), f"{where}.at")})
    return clean


def _results(value, operations):
    if value is None:
        return None
    path = "results"
    _keys(value, path, (), ("state", "fingerprint", "version", "job_id", "worker_id", "computed_at", "error",
                            "freecad_version", "post_processor", "operations"))
    clean = {"state": _choice(value.get("state", "not_computed"), f"{path}.state", RESULT_STATES),
             "fingerprint": _sha256(value.get("fingerprint"), f"{path}.fingerprint"),
             "version": _number(value.get("version", 0), f"{path}.version", CAM_BOUNDS["result_version"], integer=True),
             "job_id": _ref(value.get("job_id"), f"{path}.job_id"), "worker_id": _ref(value.get("worker_id"), f"{path}.worker_id"),
             "computed_at": _iso_datetime(value.get("computed_at"), f"{path}.computed_at"),
             "error": None if value.get("error") is None else _note(value["error"], f"{path}.error"),
             "freecad_version": None if value.get("freecad_version") is None else _text(
                 value["freecad_version"], f"{path}.freecad_version", pattern=_PLAIN_TEXT, limit=40, allow_empty=True),
             "post_processor": None if value.get("post_processor") is None else _identifier(
                 value["post_processor"], f"{path}.post_processor", _POST_NAME),
             "operations": {}}
    known = {op["id"] for op in operations}
    per_op = value.get("operations", {})
    if not isinstance(per_op, dict):
        _fail(f"{path}.operations", "objet attendu : {identifiant: état}.")
    for identifier, item in per_op.items():
        where = f"{path}.operations.{identifier}"
        if identifier not in known:
            _fail(where, "identifiant d'opération inconnu dans ce projet.")
        _keys(item, where, (), ("state", "estimated_s", "segments", "ok", "error"))
        clean["operations"][identifier] = {
            "state": _choice(item.get("state", "not_computed"), f"{where}.state", OPERATION_RESULT_STATES),
            "estimated_s": _optional_number(item.get("estimated_s"), f"{where}.estimated_s", CAM_BOUNDS["estimated_s"]),
            "segments": _optional_number(item.get("segments"), f"{where}.segments", (0, 100000000), integer=True),
            "ok": None if item.get("ok") is None else _bool(item["ok"], f"{where}.ok"),
            "error": None if item.get("error") is None else _note(item["error"], f"{where}.error")}
    return clean


def validate_custom_line(line, path):
    """Une ligne de préambule / postambule : mots autorisés et commentaires seulement."""
    return _custom_words(line, path, CAM_CUSTOM_WORDS_ALLOWED, CAM_CUSTOM_VALUE_WORDS, 1.0,
                         "commande « {word} » non autorisée dans un préambule ou un postambule.")


def validate_custom_motion_line(line, path, factor=1.0):
    """Une ligne d'opération « custom » : déplacements linéaires G0/G1, G4, broche, arrosage, pauses, commentaires.

    Les coordonnées sont dans les unités du projet (``factor`` les ramène en mm pour les bornes).
    Jamais G2/G3 (arcs), G28/G53 (références machine), G91, G20/G21, M6, M98/M99.
    """
    values = dict(CAM_MOTION_VALUE_WORDS)
    for axis in ("X", "Y", "Z"):
        low, high = CAM_MOTION_VALUE_WORDS[axis]
        values[axis] = (low / factor, high / factor)
    return _custom_words(line, path, CAM_MOTION_WORDS_ALLOWED, values, factor,
                         "commande « {word} » non autorisée dans une opération personnalisée (G0, G1, G4, broche, arrosage, M0).")


def _custom_words(line, path, allowed, value_words, factor, refusal):
    if not isinstance(line, str):
        _fail(path, "texte attendu.")
    text = line.strip()
    if not text or len(text) > CAM_BOUNDS["custom_line_chars"]:
        _fail(path, f"ligne vide ou trop longue ({CAM_BOUNDS['custom_line_chars']} caractères au plus).")
    words, position = [], 0
    upper = text.upper()
    while position < len(text):
        char = text[position]
        if char == " ":
            position += 1
            continue
        if char == "(":
            end = text.find(")", position)
            comment = text[position + 1:end] if end > 0 else ""
            if end < 0 or "(" in comment or not _NOTE.fullmatch(comment.replace(")", "")):
                _fail(path, "commentaire invalide : « ( … ) » sur une ligne, sans parenthèse imbriquée.")
            words.append(f"({comment.strip()})")
            position = end + 1
            continue
        match = re.match(r"([A-Z])\s*([-+]?[0-9]+(?:\.[0-9]+)?)", upper[position:])
        if not match:
            _fail(path, f"texte libre refusé près de « {text[position:position + 12]} » : mots G, M, S, F, T et "
                        "commentaires seulement.")
        letter, number = match.group(1), match.group(2)
        if letter in ("G", "M"):
            if not re.fullmatch(r"[0-9]{1,3}(\.[0-9])?", number):
                _fail(path, f"mot « {letter}{number} » invalide.")
            word = _normalize_word(f"{letter}{number}")
            if word not in allowed:
                _fail(path, refusal.format(word=word))
            words.append(word)
        elif letter in value_words:
            value = float(number)
            low, high = value_words[letter]
            if not low - 1e-9 <= value <= high + 1e-9:
                _fail(path, f"valeur « {letter}{number} » hors limites.")
            words.append(f"{letter}{number}")
        else:
            _fail(path, f"lettre « {letter} » non autorisée ({', '.join(sorted(value_words))} et G, M seulement).")
        position += match.end()
    return " ".join(words)


def _post(value):
    value = {"options": {}, "preamble": [], "postamble": []} if value is None else value
    path = "post"
    _keys(value, path, (), ("options", "preamble", "postamble"))
    options = value.get("options", {})
    if not isinstance(options, dict):
        _fail(f"{path}.options", "objet attendu.")
    if len(options) > CAM_BOUNDS["post_options_max"]:
        _fail(f"{path}.options", f"trop d'options ({CAM_BOUNDS['post_options_max']} au plus).")
    clean_options = {}
    for key, option in options.items():
        where = f"{path}.options.{key}"
        if not isinstance(key, str) or not _OPTION_KEY.fullmatch(key):
            _fail(where, "nom d'option invalide (sans tirets initiaux, minuscules).")
        if key in POST_OPTIONS_DENIED:
            _fail(where, "option non transmissible par cette voie (préambule et postambule ont leurs propres champs).")
        if isinstance(option, bool):
            clean_options[key] = option
        elif isinstance(option, (int, float)):
            if not math.isfinite(option) or abs(option) > 1e6:
                _fail(where, "nombre fini et raisonnable attendu.")
            clean_options[key] = option
        elif isinstance(option, str):
            if len(option) > CAM_BOUNDS["post_option_value_chars"] or not _OPTION_VALUE.fullmatch(option):
                _fail(where, "valeur texte invalide (caractères ou longueur).")
            clean_options[key] = option
        else:
            _fail(where, "valeur texte, nombre ou vrai/faux attendue.")
    clean = {"options": clean_options}
    for block in ("preamble", "postamble"):
        lines = _list(value.get(block, []), f"{path}.{block}", CAM_BOUNDS["custom_lines_max"])
        clean[block] = [validate_custom_line(line, f"{path}.{block}[{index}]") for index, line in enumerate(lines)]
    return clean


def _output(value, name):
    if value is None:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")[:60] or "programme_cnc"
        value = {"stem": slug}
    _keys(value, "output", ("stem",))
    stem = value["stem"]
    if not isinstance(stem, str) or not _STEM.fullmatch(stem) or ".." in stem or stem.endswith("."):
        _fail("output.stem", "nom de base invalide (lettres, chiffres, « . », « _ », « - », 60 caractères au plus).")
    if stem.split(".")[0].lower() in _WINDOWS_RESERVED:
        _fail("output.stem", "nom réservé par Windows.")
    return {"stem": stem}


def _migrate_module():
    try:
        from worker_agent import cam_migrate  # dépôt (site, tests)
    except ImportError:  # pragma: no cover - paquet Worker à plat
        import cam_migrate
    return cam_migrate


def project_version(payload):
    """Version déclarée d'un projet (1 ou 2) ; ``CamPayloadError`` sinon."""
    if not isinstance(payload, dict):
        _fail("", "Projet CAM invalide : objet attendu.")
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, (int, float)) or version not in CAM_PAYLOAD_VERSIONS:
        _fail("version", "version de projet CAM non prise en charge (1 ou 2).")
    return int(version)


def validate_cam_project(payload, as_version=None, capabilities=None):
    """Valide et normalise un projet CAM ; rend une copie, lève ``CamPayloadError`` (``ValueError``).

    - ``as_version`` absent : la copie rendue garde la version de l'entrée (v1 → v1, v2 → v2) ;
    - ``as_version=2`` : un projet v1 est d'abord migré (``cam_migrate.upgrade``) ;
    - ``as_version=1`` : un projet v2 est rétrogradé (``cam_migrate.downgrade``, ``CamDowngradeError`` si impossible) ;
    - ``capabilities`` (capacités CAM lues sur le Worker) : ``capability_errors`` appliqué comme refus.
    """
    version = project_version(payload)
    payload = copy.deepcopy(payload)
    if as_version is not None:
        if as_version not in CAM_PAYLOAD_VERSIONS:
            _fail("version", "version cible inconnue (1 ou 2).")
        if as_version == 2 and version == 1:
            payload, _notes = _migrate_module().upgrade(payload)
            version = 2
        elif as_version == 1 and version == 2:
            payload = _migrate_module().downgrade(payload)
            version = 1
    clean = _validate_v1(payload) if version == 1 else _validate_v2(payload)
    if capabilities is not None:
        problems = capability_errors(clean, capabilities)
        if problems:
            _fail(problems[0]["path"], problems[0]["message"])
    return clean


def _validate_v1(payload):
    _keys(payload, "", ("version", "name", "units", "source", "machine", "stock", "tools", "operations"),
          ("orientation", "origin", "heights", "fixtures", "post", "output"))
    name = _text(payload["name"], "name")
    units = _choice(payload["units"], "units", UNITS)
    factor = INCH_MM if units == "inch" else 1.0
    machine = _machine(payload["machine"])
    # Hauteurs par défaut : sécurité au minimum de la machine (5 mm au moins), dégagement jamais sous la sécurité.
    safe_default = max(5.0, machine["safe_height_min"])
    heights = _heights(payload.get("heights", {"clearance": max(10.0, safe_default) / factor, "safe": safe_default / factor,
                                               "retract": 3 / factor}), factor, machine)
    tools = _tools(payload["tools"], factor, machine)
    return {
        "version": 1,
        "name": name,
        "units": units,
        "source": _source(payload["source"], factor),
        "orientation": _orientation(payload.get("orientation")),
        "machine": machine,
        "stock": _stock(payload["stock"], factor, machine),
        "origin": _origin(payload.get("origin"), factor),
        "heights": heights,
        "fixtures": _fixtures(payload.get("fixtures"), factor),
        "tools": tools,
        "operations": _operations(payload["operations"], factor, machine, tools, heights),
        "post": _post(payload.get("post")),
        "output": _output(payload.get("output"), name),
    }


def _validate_v2(payload):
    _keys(payload, "", ("version", "name", "units", "source", "machine", "stock", "tools", "operations"),
          ("orientation", "origin", "heights", "fixtures", "sequence", "post", "output", "assistant", "results"))
    name = _text(payload["name"], "name")
    units = _choice(payload["units"], "units", UNITS)
    factor = INCH_MM if units == "inch" else 1.0
    machine = _machine(payload["machine"], version=2)
    safe_default = max(5.0, machine["safe_height_min"])
    heights = _heights(payload.get("heights", {"clearance": max(10.0, safe_default) / factor, "safe": safe_default / factor,
                                               "retract": 3 / factor}), factor, machine)
    tools = _tools(payload["tools"], factor, machine, version=2)
    sequence = _sequence(payload.get("sequence"), machine)
    operations = _operations(payload["operations"], factor, machine, tools, heights, version=2, sequence=sequence)
    return {
        "version": 2,
        "name": name,
        "units": units,
        "source": _source(payload["source"], factor, version=2),
        "orientation": _orientation(payload.get("orientation")),
        "machine": machine,
        "stock": _stock(payload["stock"], factor, machine, version=2, heights=heights),
        "origin": _origin(payload.get("origin"), factor),
        "heights": heights,
        "fixtures": _fixtures(payload.get("fixtures"), factor),
        "tools": tools,
        "operations": operations,
        "sequence": sequence,
        "post": _post(payload.get("post")),
        "output": _output(payload.get("output"), name),
        "assistant": _assistant(payload.get("assistant")),
        "results": _results(payload.get("results"), operations),
    }


# ---------------------------------------------------------------------------------------------
# Capacités du Worker, capacité d'agent requise, forme réellement utilisée par le moteur
# ---------------------------------------------------------------------------------------------
def required_capability(project):
    """``freecad_cam_v1`` si le projet peut tourner sur un agent v1 (v1, ou v2 sans fonction v2), sinon ``freecad_cam_v2``."""
    if not isinstance(project, dict):
        _fail("", "Projet CAM invalide : objet attendu.")
    if project.get("version") == 1:
        return CAM_CAPABILITY
    return CAM_CAPABILITY if not _migrate_module().v2_features(project) else CAM_CAPABILITY_V2


def capability_errors(project, capabilities):
    """Croisement d'un projet normalisé avec les capacités CAM d'un Worker : ``[{path, code, message}]``.

    ``capabilities`` : document nettoyé (``cam_projects.sanitize_capabilities`` ou ``freecad_cam.capabilities``) :
    ``operations`` (modules importables, noms FreeCAD en minuscules ou types du contrat), ``dependencies``
    (``opencamlib``/``ocl``/``area``), ``tool_families`` (formes présentes), ``post_processors[].arguments[].name``.
    Une liste absente n'est pas contrôlée (seule la dépendance OpenCAMLib est exigée pour surface/waterline).
    """
    problems = []
    if not isinstance(capabilities, dict):
        return [{"path": "", "code": "capabilities_invalid", "message": "capacités du Worker illisibles."}]
    operations = project.get("operations") if isinstance(project.get("operations"), list) else []
    tools = {tool["number"]: tool for tool in project.get("tools", []) if isinstance(tool, dict)}
    dependencies = capabilities.get("dependencies") if isinstance(capabilities.get("dependencies"), dict) else {}
    ocl = bool(dependencies.get("opencamlib") or dependencies.get("ocl"))
    available = capabilities.get("operations") if isinstance(capabilities.get("operations"), list) else None
    available = {str(item).lower().replace("_", "") for item in available} if available is not None else None
    families = capabilities.get("tool_families") if isinstance(capabilities.get("tool_families"), list) else None
    families = {str(item).lower().replace("-", "") for item in families} if families is not None else None
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict) or not operation.get("enabled", True):
            continue
        kind = operation.get("type")
        where = f"operations[{index}]"
        if kind in OCL_OPERATIONS and not ocl:
            problems.append({"path": f"{where}.type", "code": "ocl_missing",
                             "message": f"l'opération « {kind} » exige OpenCAMLib, absent sur ce Worker "
                                        "(installation à demander dans Mes Workers)."})
        if available is not None and kind in MACHINING_TYPES_V2 and _MODULE_NAMES[kind] not in available \
                and kind.replace("_", "") not in available:
            problems.append({"path": f"{where}.type", "code": "operation_unavailable",
                             "message": f"l'opération « {kind} » (module Path.Op.{_MODULE_NAMES[kind].capitalize()}) n'est pas "
                                        "importable sur ce Worker."})
        tool = tools.get(operation.get("tool"))
        if families is not None and tool and _SHAPE_FILES[tool["family"]].replace("-", "") not in families:
            problems.append({"path": f"{where}.tool", "code": "tool_shape_missing",
                             "message": f"forme d'outil « {_SHAPE_FILES[tool['family']]} » absente de ce Worker."})
    sequence = project.get("sequence") if isinstance(project.get("sequence"), dict) else {}
    wait = sequence.get("spindle_wait_s") or 0
    posts = capabilities.get("post_processors") if isinstance(capabilities.get("post_processors"), list) else None
    if wait and posts is not None:
        machine = project.get("machine") if isinstance(project.get("machine"), dict) else {}
        post = next((item for item in posts if isinstance(item, dict) and item.get("name") == machine.get("post_processor")), None)
        if post is None:
            problems.append({"path": "machine.post_processor", "code": "post_unavailable",
                             "message": f"post-processeur « {machine.get('post_processor')} » absent de ce Worker."})
        else:
            names = {str(arg.get("name")) for arg in post.get("arguments", []) if isinstance(arg, dict)}
            if "--wait-for-spindle" not in names:
                problems.append({"path": "sequence.spindle_wait_s", "code": "post_argument_missing",
                                 "message": "le post-processeur choisi n'accepte pas --wait-for-spindle : laisse 0."})
    return problems


_MODULE_NAMES = {"facing": "millface", "pocket": "pocket", "profile": "profile", "drilling": "drilling", "adaptive": "adaptive",
                 "engrave": "engrave", "vcarve": "vcarve", "helix": "helix", "slot": "slot", "surface": "surface",
                 "waterline": "waterline", "deburr": "deburr", "thread_milling": "threadmilling", "custom": "custom"}
_SHAPE_FILES = {"endmill": "endmill", "drill": "drill", "ballend": "ballend", "bullnose": "bullnose", "vbit": "v-bit",
                "chamfer": "chamfer", "engraver": "v-bit", "threadmill": "thread-mill"}
ENGINE_SHAPE_USAGE = {
    "diameter_only": "FreeCAD 1.1.3 ne lit que Diameter : l'outil est traité comme une fraise plate du même diamètre.",
    "full_shape": "FreeCAD 1.1.3 choisit la fraise OpenCAMLib selon la forme (Cyl, Ball, Bull, Cone) : forme respectée.",
    "angle_tip": "FreeCAD 1.1.3 lit CuttingEdgeAngle et TipDiameter : profondeur dérivée de l'angle.",
    "none": "l'opération ne lit pas la forme de l'outil (trajectoire sur les arêtes ou texte libre).",
    "thread": "FreeCAD 1.1.3 compare Diameter au diamètre mineur et lit Crest / cuttingAngle.",
}
_ENGINE_SHAPE = {"facing": "diameter_only", "pocket": "diameter_only", "profile": "diameter_only", "drilling": "diameter_only",
                 "adaptive": "diameter_only", "helix": "diameter_only", "slot": "diameter_only", "surface": "full_shape",
                 "waterline": "full_shape", "vcarve": "angle_tip", "deburr": "angle_tip", "engrave": "none", "custom": "none",
                 "thread_milling": "thread"}


def engine_tool_shape(operation_type, family):
    """Forme réellement utilisée par le moteur pour (type d'opération, famille d'outil) : ``{reads, shape_file, note, faithful}``."""
    reads = _ENGINE_SHAPE.get(operation_type)
    if reads is None or family not in _SHAPE_FILES:
        return None
    faithful = reads in ("full_shape", "angle_tip", "thread") or (reads == "diameter_only" and family in ("endmill", "drill"))
    return {"reads": reads, "shape_file": f"{_SHAPE_FILES[family]}.fcstd", "note": ENGINE_SHAPE_USAGE[reads],
            "faithful": faithful}


CAM_JOB_OPS = ("capabilities", "compute", "preview")


def validate_cam_job_parameters(parameters):
    """Paramètres d'un job ``converter`` portant ``cam`` : copie validée, autres clés conservées.

    ``cam.op`` : ``capabilities`` (aucun fichier), ``compute`` (projet v1 ou v2, validé dans sa version),
    ``preview`` (import + orientation + STL d'aperçu, sans Job : ``source``, ``units``, ``orientation``, ``scale``).
    """
    if not isinstance(parameters, dict) or not isinstance(parameters.get("cam"), dict):
        _fail("cam", "paramètres CAM invalides.")
    if "scene" in parameters:
        _fail("scene", "un job CAM ne porte pas de scène paramétrique.")
    clean = copy.deepcopy(parameters)
    cam = clean["cam"]
    operation = cam.get("op")
    if operation == "capabilities":
        _keys(cam, "cam", ("op",))
        clean["cam"] = {"op": "capabilities"}
        for key in ("input_file", "input_files", "output_name"):
            if key in clean:
                _fail(key, "la lecture des capacités ne prend aucun fichier.")
        return clean
    if operation == "preview":
        _keys(cam, "cam", ("op", "source", "units"), ("orientation",))
        units = _choice(cam["units"], "cam.units", UNITS)
        source = _source(cam["source"], INCH_MM if units == "inch" else 1.0, version=2)
        clean["cam"] = {"op": "preview", "units": units, "source": source, "orientation": _orientation(cam.get("orientation"))}
        if "input_files" in clean:
            _fail("input_files", "un aperçu CAM prend un seul fichier source.")
        return clean
    if operation != "compute":
        _fail("cam.op", "opération CAM inconnue (capabilities, compute ou preview).")
    _keys(cam, "cam", ("op", "project"))
    project = validate_cam_project(cam["project"])
    clean["cam"] = {"op": "compute", "project": project}
    if "input_files" in clean:
        _fail("input_files", "un calcul CAM prend un seul fichier source.")
    output_name = clean.get("output_name")
    if output_name is not None and output_name != project["output"]["stem"]:
        _fail("output_name", "le nom de sortie doit être celui du projet (output.stem).")
    clean["output_name"] = project["output"]["stem"]
    return clean


def project_mm_factor(project):
    """Facteur vers les mm pour les valeurs du projet (hors bloc machine)."""
    return INCH_MM if project.get("units") == "inch" else 1.0
