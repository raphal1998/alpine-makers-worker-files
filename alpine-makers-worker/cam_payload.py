# -*- coding: utf-8 -*-
"""Projet CAM déclaratif d'Alpine Forge 3D (« Préparer pour une CNC »), tranche 1.

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
  l'objet de l'appelant.

Unités : les valeurs du bloc ``machine`` (courses, avances, hauteur de sécurité
minimale) sont TOUJOURS en millimètres (instantané du catalogue serveur).
Toutes les autres longueurs et avances du projet (brut, origine, hauteurs,
fixtures, outils, opérations) sont dans ``units`` du projet ; les bornes sont
contrôlées après conversion en mm. Le moteur convertit tout en mm pour FreeCAD.
"""
import copy
import math
import re

CAM_PAYLOAD_VERSION = 1
CAM_CAPABILITY = "freecad_cam_v1"
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
STOCK_MODES = ("from_model", "explicit")
ORIGIN_PRESETS = ("top_front_left", "top_front_right", "top_rear_left", "top_rear_right",
                  "top_center", "bottom_center", "center", "custom")
WCS_CODES = ("G54", "G55", "G56", "G57", "G58", "G59")
FIXTURE_KINDS = ("box",)
# Toutes les familles connues du catalogue ; seules celles de la tranche 1 sont acceptées.
TOOL_FAMILIES_ALL = ("endmill", "drill", "ballend", "bullnose", "vbit", "chamfer", "engraver", "threadmill",
                     "tap", "reamer", "slittingsaw", "dovetail", "radius", "taperedballnose", "probe")
TOOL_FAMILIES_SUPPORTED = ("endmill", "drill")
TOOL_MATERIALS = ("carbide", "hss", "cobalt", "diamond", "ceramic", "other")
OPERATION_TYPES_ALL = ("facing", "pocket", "profile", "drilling", "adaptive", "engrave", "vcarve", "helix", "slot",
                       "surface", "waterline", "deburr", "thread_milling", "pause", "tool_change", "note")
OPERATION_TYPES_SUPPORTED = ("facing", "pocket", "profile", "drilling", "pause", "tool_change", "note")
MACHINING_TYPES = ("facing", "pocket", "profile", "drilling")
# Familles d'outils admises par type d'usinage (un foret ne fraise pas de côté).
OPERATION_TOOL_FAMILIES = {"facing": ("endmill",), "pocket": ("endmill",), "profile": ("endmill",),
                           "drilling": ("drill", "endmill")}
SELECTION_MODES = ("auto",)
CUT_DIRECTIONS = ("climb", "conventional")
ENTRY_MODES = ("plunge", "ramp", "helix")
PROFILE_SIDES = ("outside", "inside")
# « none » : aucune compensation par le contrôleur (G41/G42 jamais émis) ; le parcours est
# décalé du rayon de fraise par FreeCAD. La compensation contrôleur n'est pas vérifiée en tranche 1.
COMPENSATION_MODES = ("none",)

# Mots autorisés dans un préambule / postambule personnalisé (en plus des commentaires « ( … ) »
# et des mots S, F, T bornés). Jamais M98/M99 (sous-programmes), M6, G28/G53 (références machine),
# G91 ou G20/G21 (le post-processeur fixe les unités et le mode de distance).
CAM_CUSTOM_WORDS_ALLOWED = frozenset({
    "G17", "G40", "G49", "G54", "G55", "G56", "G57", "G58", "G59", "G61", "G64", "G80", "G90", "G94",
    "M0", "M1", "M2", "M3", "M4", "M5", "M7", "M8", "M9", "M30",
})
CAM_CUSTOM_VALUE_WORDS = {"S": (0.0, 100000.0), "F": (1.0, 50000.0), "T": (0.0, 999.0)}
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
    "fixtures_max": 16,
    "tools_max": 32,
    "operations_max": 40,
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


def _source(value, factor):
    path = "source"
    if isinstance(value, dict):
        name = value.get("file")
        kind = value.get("kind")
        extension = name.rsplit(".", 1)[-1].lower() if isinstance(name, str) and "." in name else ""
        if extension in GCODE_EXTENSIONS or (isinstance(kind, str) and kind.lower() in GCODE_EXTENSIONS):
            _fail(f"{path}.file", GCODE_SOURCE_MESSAGE)
    _keys(value, path, ("file", "kind", "units"))
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
    return {"file": name, "kind": kind, "units": units}


def _orientation(value):
    value = {"top_face": "+z", "rotation_z_deg": 0, "flip": False} if value is None else value
    path = "orientation"
    _keys(value, path, (), ("top_face", "rotation_z_deg", "flip"))
    rotation = value.get("rotation_z_deg", 0)
    if isinstance(rotation, bool) or not isinstance(rotation, (int, float)) or rotation not in ROTATIONS_Z:
        _fail(f"{path}.rotation_z_deg", "rotation admise : 0, 90, 180 ou 270 degrés.")
    return {"top_face": _choice(value.get("top_face", "+z"), f"{path}.top_face", TOP_FACES),
            "rotation_z_deg": int(rotation), "flip": _bool(value.get("flip", False), f"{path}.flip")}


def _machine(value):
    path = "machine"
    required = ("profile_id", "label", "kind", "axes", "travel", "max_feed", "spindle", "safe_height_min",
                "controller", "post_processor", "output_extension")
    optional = ("max_rapid", "max_accel", "tool_change", "tool_count", "probe_z", "tool_length_measure", "coolant",
                "units", "unsupported_commands", "notes", "verified")
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
    if "verified" in value:
        clean["verified"] = _bool(value["verified"], f"{path}.verified")
    if len(set(clean["coolant"])) != len(clean["coolant"]):
        _fail(f"{path}.coolant", "mode d'arrosage en double.")
    return clean


def _normalize_word(word):
    """« G01 » → « G1 », « M08 » → « M8 » : une seule écriture par commande."""
    letter, number = word[0], word[1:]
    if "." in number:
        whole, fraction = number.split(".", 1)
        return f"{letter}{int(whole)}.{fraction}"
    return f"{letter}{int(number)}"


def _stock(value, factor, machine):
    path = "stock"
    _keys(value, path, ("mode",), ("shape", "size", "margins", "material_id"))
    mode = _choice(value["mode"], f"{path}.mode", STOCK_MODES)
    size = value.get("size", {"x": 0, "y": 0, "z": 0})
    if mode == "explicit":
        size = _xyz(size, f"{path}.size", CAM_BOUNDS["length_mm"], factor)
        for axis in ("x", "y", "z"):
            if size[axis] * factor > machine["travel"][axis] + 1e-9:
                _fail(f"{path}.size.{axis}", f"brut plus grand que la course {axis.upper()} de la machine "
                                             f"({machine['travel'][axis]:g} mm).")
    else:
        size = _xyz(size, f"{path}.size", (0.0, 3000.0), factor)
    margins = value.get("margins", {"x": 0, "y": 0, "top": 0, "bottom": 0})
    margins = _xyz(margins, f"{path}.margins", CAM_BOUNDS["margin_mm"], factor, keys=("x", "y", "top", "bottom"))
    material = value.get("material_id", "")
    if material not in ("", None):
        material = _identifier(material, f"{path}.material_id", _PROFILE_ID)
    return {"shape": _choice(value.get("shape", "box"), f"{path}.shape", STOCK_SHAPES), "mode": mode,
            "size": size, "margins": margins, "material_id": material or ""}


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


def _tools(value, factor, machine):
    path = "tools"
    items = _list(value, path, CAM_BOUNDS["tools_max"], minimum=1)
    required = ("number", "name", "family", "diameter", "flute_length", "spindle_rpm", "feed_xy", "feed_plunge")
    optional = ("shank_diameter", "length", "stickout", "flutes", "material", "coating", "max_rpm", "chip_load",
                "feed_ramp", "step_down", "stepover_pct", "coolant", "length_offset", "tip_angle", "corner_radius")
    clean, numbers = [], set()
    for index, item in enumerate(items):
        where = f"{path}[{index}]"
        if isinstance(item, dict) and isinstance(item.get("family"), str):
            family = item["family"]
            if family in TOOL_FAMILIES_ALL and family not in TOOL_FAMILIES_SUPPORTED:
                _fail(f"{where}.family", f"la famille d'outil « {family} » n'est pas encore vérifiée : "
                                         "fraise droite (endmill) ou foret (drill) seulement dans cette version.")
        _keys(item, where, required, optional)
        number = _number(item["number"], f"{where}.number", CAM_BOUNDS["tool_number"], integer=True)
        if number in numbers:
            _fail(f"{where}.number", "deux outils portent le même numéro.")
        numbers.add(number)
        family = _choice(item["family"], f"{where}.family", TOOL_FAMILIES_SUPPORTED)
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
        clean.append({
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
        })
        if clean[-1]["coolant"] != "off" and clean[-1]["coolant"] not in machine["coolant"]:
            _fail(f"{where}.coolant", "arrosage absent de la machine.")
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
                  "pause": "Pause", "tool_change": "Changement d'outil", "note": "Consigne"}


def _operations(value, factor, machine, tools, heights):
    path = "operations"
    items = _list(value, path, CAM_BOUNDS["operations_max"], minimum=1)
    by_number = {tool["number"]: tool for tool in tools}
    clean, defined = [], []
    for index, item in enumerate(items):
        where = f"{path}[{index}]"
        if not isinstance(item, dict):
            _fail(where, "objet attendu.")
        kind = item.get("type")
        if isinstance(kind, str) and kind in OPERATION_TYPES_ALL and kind not in OPERATION_TYPES_SUPPORTED:
            _fail(f"{where}.type", f"l'opération « {kind} » n'est pas encore prise en charge dans cette version.")
        kind = _choice(kind, f"{where}.type", OPERATION_TYPES_SUPPORTED)
        if kind in MACHINING_TYPES:
            _keys(item, where, _COMMON_OP + _MACHINING_REQUIRED, _COMMON_OP_OPTIONAL + _MACHINING_OPTIONAL + _TYPE_EXTRA[kind])
        elif kind == "tool_change":
            _keys(item, where, _COMMON_OP + ("tool",), _COMMON_OP_OPTIONAL)
        elif kind == "note":
            _keys(item, where, _COMMON_OP + ("operator_note",), _COMMON_OP_OPTIONAL)
        else:
            _keys(item, where, _COMMON_OP, _COMMON_OP_OPTIONAL)
        identifier = _identifier(item["id"], f"{where}.id")
        if identifier in defined:
            _fail(f"{where}.id", "deux opérations portent le même identifiant.")
        entry = {"id": identifier, "type": kind,
                 "name": _text(item.get("name", _DEFAULT_NAMES[kind]), f"{where}.name"),
                 "enabled": _bool(item.get("enabled", True), f"{where}.enabled"),
                 "operator_note": _note(item.get("operator_note", ""), f"{where}.operator_note")}
        if kind == "note" and not entry["operator_note"]:
            _fail(f"{where}.operator_note", "une consigne opérateur ne peut pas être vide.")
        if kind in MACHINING_TYPES or kind == "tool_change":
            number = _number(item["tool"], f"{where}.tool", CAM_BOUNDS["tool_number"], integer=True)
            if number not in by_number:
                _fail(f"{where}.tool", f"outil n°{number} absent de la bibliothèque du projet.")
            entry["tool"] = number
        if kind in MACHINING_TYPES:
            entry.update(_machining(item, where, kind, factor, machine, by_number[entry["tool"]], heights, defined))
        clean.append(entry)
        defined.append(identifier)
    if not any(op["type"] in MACHINING_TYPES and op["enabled"] for op in clean):
        _fail(path, "aucune opération d'usinage activée.")
    return clean


def _machining(item, where, kind, factor, machine, tool, heights, defined):
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
    depends = _list(item.get("depends_on", []), f"{where}.depends_on", CAM_BOUNDS["operations_max"])
    for position, reference in enumerate(depends):
        if not isinstance(reference, str) or reference not in defined:
            _fail(f"{where}.depends_on[{position}]", "une opération ne peut dépendre que d'une opération définie avant elle.")
    if len(set(depends)) != len(depends):
        _fail(f"{where}.depends_on", "dépendance en double.")
    entry["depends_on"] = list(depends)
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


def validate_custom_line(line, path):
    """Une ligne de préambule / postambule : mots autorisés et commentaires seulement."""
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
            if word not in CAM_CUSTOM_WORDS_ALLOWED:
                _fail(path, f"commande « {word} » non autorisée dans un préambule ou un postambule.")
            words.append(word)
        elif letter in CAM_CUSTOM_VALUE_WORDS:
            value = float(number)
            low, high = CAM_CUSTOM_VALUE_WORDS[letter]
            if not low <= value <= high:
                _fail(path, f"valeur « {letter}{number} » hors limites.")
            words.append(f"{letter}{number}")
        else:
            _fail(path, f"lettre « {letter} » non autorisée (G, M, S, F, T seulement).")
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


def validate_cam_project(payload):
    """Valide et normalise un projet CAM ; rend une copie, lève ``CamPayloadError`` (``ValueError``)."""
    if not isinstance(payload, dict):
        _fail("", "Projet CAM invalide : objet attendu.")
    payload = copy.deepcopy(payload)
    _keys(payload, "", ("version", "name", "units", "source", "machine", "stock", "tools", "operations"),
          ("orientation", "origin", "heights", "fixtures", "post", "output"))
    if isinstance(payload["version"], bool) or payload["version"] != CAM_PAYLOAD_VERSION:
        _fail("version", "version de projet CAM non prise en charge.")
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
        "version": CAM_PAYLOAD_VERSION,
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


CAM_JOB_OPS = ("capabilities", "compute")


def validate_cam_job_parameters(parameters):
    """Paramètres d'un job ``converter`` portant ``cam`` : copie validée, autres clés conservées."""
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
    if operation != "compute":
        _fail("cam.op", "opération CAM inconnue (capabilities ou compute).")
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
