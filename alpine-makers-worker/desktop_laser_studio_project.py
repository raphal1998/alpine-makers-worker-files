"""Projet Alpine Laser Studio (.alsproj) → requêtes du moteur laser, pour Alpine Makers Desktop (worker.exe).

Mêmes règles que le studio web (``static/laser_studio.js``) : objets en mm dans le repère du document (origine en haut
à gauche, Y vers le bas), rotation autour du centre de la boîte, un calque par couleur. Ce module ne fait que des
conversions pures (aucun fichier, aucun réseau, aucun moteur) :

- ``plan_body(project, ...)`` : corps de requête du moteur (``svg``, ``layers``, ``machine``, ``order``,
  ``raster_sources``, ``offset_mm``) — celui que le navigateur envoyait aux jobs laser du site ;
- ``machine_to_doc`` / ``doc_to_machine`` : passage repère machine ↔ document (aperçu et simulation dessinés sur le plan) ;
- ``objects_from_parse`` : résultat de l'analyse d'un SVG par le moteur → objets « path » du projet (import) ;
- ``MATERIALS``, ``SAFETY_ITEMS`` : bibliothèque de matériaux intégrée et rappels de la check-list, comme le web.
"""
import math
import re
import secrets

LAYER_MODES = ("line", "fill", "offset_fill", "image")
IMAGE_MODES = ("threshold", "ordered", "atkinson", "dither", "stucki", "jarvis", "newsprint", "halftone", "sketch",
               "grayscale")
PLAN_ORDERS = ("layer", "inside-out", "nearest")
MACHINE_KINDS = ("laser", "cnc")
MACHINE_ORIGINS = ("front-left", "rear-left", "front-right", "rear-right", "center")
OBJECT_TYPES = ("rect", "ellipse", "poly", "path", "image", "group")
PALETTE = ("#000000", "#0000ff", "#ff0000", "#00c000", "#c0c000", "#ff8000", "#00c0c0", "#ff00ff", "#a0a0a0",
           "#000080", "#800000", "#008000", "#808000", "#c06000", "#0080ff", "#800080", "#404040", "#8060ff",
           "#ff6080", "#40a0ff", "#c03060", "#60c080", "#e0a060", "#20b040")
# Clés de réglage transmises au moteur (LAYER_ENGINE_KEYS du studio web).
LAYER_ENGINE_KEYS = (
    "name", "color", "mode", "power_pct", "min_power_pct", "speed_mm_min", "passes", "z_offset_mm", "z_step_mm",
    "air_assist", "interval_mm", "angle_deg", "enabled", "constant_power", "sublayers", "kerf_offset_mm", "perforation",
    "perf_cut_mm", "perf_skip_mm", "tabs_enabled", "tabs_mode", "tabs_points", "tab_size_mm", "tab_spacing_mode",
    "tab_spacing_mm", "tabs_per_shape", "tabs_max_enabled", "tabs_max", "tab_power_pct", "tabs_skip_inner",
    "lead_in_mm", "lead_out_mm", "overcut_mm", "bidirectional", "crosshatch", "overscan_pct", "fill_grouping",
    "ramp_length_mm", "image_mode", "negative", "dot_width_correction", "dot_width_mm", "pass_through",
    "halftone_cells_per_inch", "halftone_angle_deg")
MAX_OBJECTS = 50000
MAX_DEPTH = 32

SAFETY_ITEMS = {
    "laser": ["Porte ou capot fermé, aucune personne dans la zone du faisceau", "Extraction des fumées en marche",
              "Air assist prêt si le calque l'utilise",
              "Surveillance incendie : je reste devant la machine, extincteur ou couverture à portée",
              "Lunettes de protection adaptées à la longueur d'onde", "Pièce à plat, plateau dégagé, mise au point réglée"],
    "cnc": ["Fraise serrée et pièce bridée, Z de sécurité vérifié", "Aspiration ou soufflage des copeaux en marche",
            "Aucun outil ni main dans la zone de travail", "Surveillance permanente : je reste devant la machine",
            "Lunettes et protections auditives portées", "Origine du travail définie sur la pièce"],
}
INTERLOCK_KINDS = {"door": "porte / capot", "water": "refroidissement eau", "air": "air assist",
                   "fire": "détection incendie", "checklist": "check-list"}
OPERATOR_INTERLOCK_KINDS = ("checklist", "water", "air", "fire")
MATERIAL_CLASSES = (("diode-5w", "Laser diode 5 W"), ("diode-10w", "Laser diode 10 W"), ("co2-40w", "Laser CO2 40 W"),
                    ("cnc", "CNC (fraise 2 mm)"))


def _material(cls, name, mode, power, speed, passes, interval=0.1, air=False, z_offset=None, z_step=None, note=""):
    identifier = re.sub(r"[^a-z0-9:]+", "-", f"{cls}:{name}".lower())
    item = {"id": identifier, "cls": cls, "name": name, "mode": mode, "power_pct": power, "speed_mm_min": speed,
            "passes": passes, "interval_mm": interval, "air_assist": air, "note": note}
    if z_offset is not None:
        item["z_offset_mm"] = z_offset
    if z_step is not None:
        item["z_step_mm"] = z_step
    return item


# Bibliothèque intégrée : la même que le studio web (MATERIALS de static/laser_studio.js).
MATERIALS = [
    _material("diode-5w", "Contreplaqué peuplier 3 mm · découpe", "line", 100, 150, 3, air=True),
    _material("diode-5w", "MDF 3 mm · découpe", "line", 100, 100, 4, air=True),
    _material("diode-5w", "Acrylique opaque 3 mm · découpe", "line", 100, 60, 8, air=True,
              note="L'acrylique transparent ne se découpe pas au laser diode."),
    _material("diode-5w", "Carton ondulé · découpe", "line", 100, 500, 1, air=True),
    _material("diode-5w", "Papier 160 g · découpe", "line", 45, 1500, 1),
    _material("diode-5w", "Cuir végétal 2 mm · découpe", "line", 100, 250, 2, air=True),
    _material("diode-5w", "Bois clair · gravure remplissage", "fill", 50, 3000, 1, interval=0.1),
    _material("diode-5w", "Bois · gravure trait", "line", 40, 2500, 1),
    _material("diode-5w", "Aluminium anodisé · gravure", "fill", 100, 1500, 1, interval=0.08),
    _material("diode-10w", "Contreplaqué peuplier 3 mm · découpe", "line", 100, 300, 2, air=True),
    _material("diode-10w", "MDF 3 mm · découpe", "line", 100, 200, 3, air=True),
    _material("diode-10w", "Acrylique opaque 3 mm · découpe", "line", 100, 120, 5, air=True,
              note="L'acrylique transparent ne se découpe pas au laser diode."),
    _material("diode-10w", "Carton ondulé · découpe", "line", 90, 800, 1, air=True),
    _material("diode-10w", "Papier 160 g · découpe", "line", 30, 2000, 1),
    _material("diode-10w", "Cuir végétal 2 mm · découpe", "line", 100, 450, 1, air=True),
    _material("diode-10w", "Bois clair · gravure remplissage", "fill", 40, 4000, 1, interval=0.1),
    _material("diode-10w", "Bois · gravure trait", "line", 30, 3000, 1),
    _material("diode-10w", "Aluminium anodisé · gravure", "fill", 80, 2000, 1, interval=0.08),
    _material("co2-40w", "Contreplaqué 3 mm · découpe", "line", 60, 600, 1, air=True),
    _material("co2-40w", "MDF 3 mm · découpe", "line", 65, 500, 1, air=True),
    _material("co2-40w", "Acrylique 3 mm · découpe", "line", 55, 400, 1, air=True),
    _material("co2-40w", "Carton ondulé · découpe", "line", 30, 1500, 1, air=True),
    _material("co2-40w", "Papier 160 g · découpe", "line", 15, 2000, 1),
    _material("co2-40w", "Cuir végétal 2 mm · découpe", "line", 35, 800, 1, air=True),
    _material("co2-40w", "Bois · gravure remplissage", "fill", 25, 4000, 1, interval=0.1),
    _material("co2-40w", "Acrylique · gravure remplissage", "fill", 20, 4000, 1, interval=0.1),
    _material("co2-40w", "Aluminium anodisé · gravure", "fill", 40, 3000, 1, interval=0.08),
    _material("cnc", "Contreplaqué 3 mm · découpe fraise 2 mm", "line", 100, 800, 3, z_offset=-1.2, z_step=-1,
              note="Profondeur finale 3,2 mm : prévois un martyr sous la pièce."),
    _material("cnc", "MDF 6 mm · découpe fraise 3 mm", "line", 100, 600, 4, z_offset=-1.6, z_step=-1.6),
    _material("cnc", "Acrylique 3 mm · découpe fraise 2 mm", "line", 100, 500, 3, z_offset=-1.1, z_step=-1.1),
    _material("cnc", "Bois dur · gravure pointe V", "line", 100, 1000, 1, z_offset=-0.5, z_step=0),
    _material("cnc", "Aluminium 2 mm · fraise 2 mm (lubrifié)", "line", 100, 250, 8, z_offset=-0.3, z_step=-0.3),
]


class ProjectError(ValueError):
    """Projet illisible ou hors bornes : message pour l'interface."""


# --- petites conversions ------------------------------------------------------------------------------------------

def num(value, fallback):
    if isinstance(value, bool):
        return fallback
    if isinstance(value, str):
        value = value.replace(",", ".")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def clamp(value, low, high):
    return min(high, max(low, value))


def fmt(value):
    text = f"{round(value * 1000) / 1000:.3f}"
    return "0.000" if text == "-0.000" else text


def normalize_color(value):
    text = str(value if value is not None else "").strip().lower()
    if re.fullmatch(r"#[0-9a-f]{3}", text):
        text = "#" + "".join(char * 2 for char in text[1:])
    return text if re.fullmatch(r"#[0-9a-f]{6}", text) else None


def mode_or_line(mode):
    return mode if mode in LAYER_MODES else "line"


def new_id():
    return "o" + secrets.token_hex(5)


# --- calques -------------------------------------------------------------------------------------------------------

def default_layer(color, mode="line"):
    """Réglages complets d'un calque (contrat moteur + champs d'interface), comme defaultLayer() du web."""
    m = mode_or_line(mode)
    index = PALETTE.index(color) if color in PALETTE else None
    return {"name": f"Calque {color}", "color": color, "mode": m, "power_pct": 50, "min_power_pct": 0,
            "speed_mm_min": 1000, "passes": 1, "z_offset_mm": 0, "z_step_mm": 0, "air_assist": False,
            "interval_mm": 0.1, "angle_deg": 0, "enabled": True, "constant_power": False, "sublayers": [],
            "kerf_offset_mm": 0, "perforation": False, "perf_cut_mm": 0.1, "perf_skip_mm": 0.1, "tabs_enabled": False,
            "tabs_mode": "auto", "tabs_points": [], "tab_size_mm": 0.5, "tab_spacing_mode": "uniform",
            "tab_spacing_mm": 50, "tabs_per_shape": 1, "tabs_max_enabled": False, "tabs_max": 1, "tab_power_pct": 0,
            "tabs_skip_inner": False, "lead_in_mm": 0, "lead_out_mm": 0, "overcut_mm": 0, "bidirectional": True,
            "crosshatch": False, "overscan_pct": 0 if m == "line" else 2.5, "fill_grouping": "all",
            "ramp_length_mm": 0, "image_mode": "stucki" if m == "image" else "", "negative": False,
            "dot_width_correction": False, "dot_width_mm": 0.08, "pass_through": False,
            "halftone_cells_per_inch": 50, "halftone_angle_deg": 22.5, "visible": True, "auto_optimize": True,
            "line_operation": "cut", "number": index}


def point_list(value):
    points = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            points.append([num(item[0], 0.0), num(item[1], 0.0)])
    return points


def layer_settings(layer, nested=False):
    """Réglages envoyés au moteur pour un calque (layerSettings() du web) : valeurs manquantes = défaut du mode."""
    source = layer if isinstance(layer, dict) else {}
    color = normalize_color(source.get("color")) or "#000000"
    base = default_layer(color, mode_or_line(source.get("mode")))
    out = {}
    for key in LAYER_ENGINE_KEYS:
        if key == "sublayers":
            if not nested:
                subs = source.get("sublayers") if isinstance(source.get("sublayers"), list) else []
                out["sublayers"] = [layer_settings({**sub, "color": color}, True) for sub in subs[:8] if isinstance(sub, dict)]
            continue
        fallback = base[key]
        value = source[key] if key in source else fallback
        if key == "color":
            out["color"] = color
        elif key == "mode":
            out["mode"] = mode_or_line(value)
        elif key == "enabled":
            out["enabled"] = value is not False
        elif key == "passes":
            out["passes"] = int(clamp(round(num(value, 1)), 1, 200))
        elif key == "tabs_points":
            out["tabs_points"] = point_list(value)
        elif isinstance(fallback, bool):
            out[key] = bool(value)
        elif isinstance(fallback, (int, float)):
            out[key] = num(value, fallback)
        else:
            out[key] = str(value if value is not None else fallback)
    if nested and "line_operation" in source:
        out["line_operation"] = str(source.get("line_operation") or "cut")
    return out


# --- machine -------------------------------------------------------------------------------------------------------

def default_profile():
    return {"name": "Laser GRBL", "kind": "laser", "width_mm": 400, "height_mm": 400, "max_power": 1000,
            "laser_mode_dynamic": True, "travel_speed_mm_min": 3000, "origin": "front-left", "y_up": True,
            "homing_command": "$H", "homing_after": "machine_zero", "custom_home": None, "start_gcode": "",
            "end_gcode": "", "safe_z_mm": 5, "plunge_rate": 100, "spindle_rpm_max": 10000, "model": "",
            "laser_type": "diode", "laser_power_w": 0, "max_speed_mm_min": 0}


def _homing_command(value):
    return re.sub(r"[^\x20-\x7e]", "", str(value if value is not None else "")).strip()[:40]


def custom_home_area(profile):
    """Course disponible depuis un home personnalisé (customHomeArea du web) : {width_mm, height_mm, computable}."""
    width = max(1.0, num(profile.get("width_mm"), 400))
    height = max(1.0, num(profile.get("height_mm"), 400))
    home = profile.get("custom_home") if isinstance(profile.get("custom_home"), dict) else None
    full = {"width_mm": width, "height_mm": height, "computable": False}
    if not home:
        return full
    base = home.get("base_origin") if home.get("base_origin") in MACHINE_ORIGINS else "front-left"
    center = base == "center"

    def remain(value, size):
        low, high = (-size / 2, size / 2) if center else (0, size)
        if low - 1e-6 <= value <= high + 1e-6:
            return high - value
        if not center and -size - 1e-6 <= value <= 1e-6:
            return -value
        return float("nan")

    w, h = remain(num(home.get("x"), float("nan")), width), remain(num(home.get("y"), float("nan")), height)
    if not (math.isfinite(w) and math.isfinite(h)) or w < 1 or h < 1:
        return full
    return {"width_mm": round(w, 3), "height_mm": round(h, 3), "computable": True}


def effective_profile(profile):
    profile = profile if isinstance(profile, dict) else {}
    if profile.get("origin") != "custom":
        return profile
    area = custom_home_area(profile)
    return {**profile, "origin": "front-left", "y_up": True, "width_mm": area["width_mm"], "height_mm": area["height_mm"]}


def effective_start_gcode(profile):
    user = str(profile.get("start_gcode") or "")
    custom = profile.get("origin") == "custom"
    after = "custom_home" if custom else str(profile.get("homing_after") or "machine_zero")
    if not custom and not (after == "work_origin" and str(profile.get("homing_command") or "").strip()):
        return user
    return "G54\nG0 X0 Y0" + ("\n" + user if user.strip() else "")


def machine_profile(raw):
    """Profil envoyé au moteur (machineProfile() du web), avec l'origine personnalisée résolue."""
    raw = {**default_profile(), **(raw if isinstance(raw, dict) else {})}
    p = effective_profile(raw)
    laser_type = str(p.get("laser_type") or "diode")
    return {"name": str(p.get("name") or "Laser GRBL")[:80], "kind": p.get("kind") if p.get("kind") in MACHINE_KINDS else "laser",
            "width_mm": num(p.get("width_mm"), 400), "height_mm": num(p.get("height_mm"), 400),
            "max_power": num(p.get("max_power"), 1000), "laser_mode_dynamic": p.get("laser_mode_dynamic") is not False,
            "travel_speed_mm_min": num(p.get("travel_speed_mm_min"), 3000),
            "origin": p.get("origin") if p.get("origin") in MACHINE_ORIGINS else "front-left", "y_up": p.get("y_up") is not False,
            "homing_command": _homing_command(p.get("homing_command", "$H")), "start_gcode": effective_start_gcode(raw),
            "end_gcode": str(p.get("end_gcode") or ""), "safe_z_mm": num(p.get("safe_z_mm"), 5),
            "plunge_rate": num(p.get("plunge_rate"), 100), "spindle_rpm_max": num(p.get("spindle_rpm_max"), 10000),
            "model": str(p.get("model") or "")[:80],
            "laser_type": laser_type if laser_type in ("diode", "co2", "fiber", "infrared") else "diode",
            "laser_power_w": max(0.0, num(p.get("laser_power_w"), 0)), "max_speed_mm_min": max(0.0, num(p.get("max_speed_mm_min"), 0))}


def doc_to_machine(point, profile, offset=(0.0, 0.0)):
    p = effective_profile(profile)
    width, height = num(p.get("width_mm"), 400), num(p.get("height_mm"), 400)
    x, y = point[0], point[1]
    y_up = p.get("y_up") is not False
    if p.get("origin") == "center":
        mx, my = x - width / 2, (height / 2 - y) if y_up else (y - height / 2)
    else:
        mx = width - x if "right" in str(p.get("origin") or "") else x
        my = height - y if y_up else y
    return [mx + num(offset[0], 0), my + num(offset[1], 0)]


def machine_to_doc(point, profile, offset=(0.0, 0.0)):
    p = effective_profile(profile)
    width, height = num(p.get("width_mm"), 400), num(p.get("height_mm"), 400)
    y_up = p.get("y_up") is not False
    mx, my = point[0] - num(offset[0], 0), point[1] - num(offset[1], 0)
    if p.get("origin") == "center":
        return [mx + width / 2, (height / 2 - my) if y_up else (my + height / 2)]
    return [width - mx if "right" in str(p.get("origin") or "") else mx, height - my if y_up else my]


# --- géométrie des objets ------------------------------------------------------------------------------------------

def _rotate(point, center, degrees):
    if not degrees:
        return [point[0], point[1]]
    angle = math.radians(degrees)
    cos, sin = math.cos(angle), math.sin(angle)
    dx, dy = point[0] - center[0], point[1] - center[1]
    return [center[0] + dx * cos - dy * sin, center[1] + dx * sin + dy * cos]


def _ellipse_segments(obj):
    radius, tolerance = max(obj["w"], obj["h"]) / 2, 0.05
    if radius <= tolerance:
        return 16
    return int(clamp(math.ceil(2 * math.pi / (2 * math.acos(max(-1.0, 1 - tolerance / radius)))), 16, 720))


def _local_paths(obj):
    kind = obj["type"]
    if kind in ("rect", "image"):
        return [([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]], True)]
    if kind == "ellipse":
        count = _ellipse_segments(obj)
        points = [[0.5 + 0.5 * math.cos(2 * math.pi * i / count), 0.5 + 0.5 * math.sin(2 * math.pi * i / count)] for i in range(count)]
        points.append(list(points[0]))
        return [(points, True)]
    if kind == "poly":
        return [(obj.get("points") or [], obj.get("closed") is not False)]
    if kind == "path":
        return [(path.get("points") or [], bool(path.get("closed"))) for path in obj.get("paths") or []]
    return []


def leaves(objects, depth=0):
    if depth > MAX_DEPTH:
        raise ProjectError("Projet trop imbriqué (groupes).")
    out = []
    for obj in objects:
        if obj.get("type") == "group":
            out.extend(leaves(obj.get("children") or [], depth + 1))
        else:
            out.append(obj)
    return out


def world_paths(obj):
    """Contours d'un objet en mm document : [(points, fermé)]."""
    if obj.get("type") == "group":
        return [path for child in obj.get("children") or [] for path in world_paths(child)]
    center = [obj["x"] + obj["w"] / 2, obj["y"] + obj["h"] / 2]
    rotation = obj.get("rotation") or 0
    return [([_rotate([obj["x"] + p[0] * obj["w"], obj["y"] + p[1] * obj["h"]], center, rotation) for p in points], closed)
            for points, closed in _local_paths(obj)]


def clean_object(raw, depth=0):
    """Objet du projet nettoyé (sanitizeObject du web), ou None s'il est inutilisable."""
    if depth > MAX_DEPTH or not isinstance(raw, dict) or raw.get("type") not in OBJECT_TYPES:
        return None
    obj = {"id": raw["id"] if isinstance(raw.get("id"), str) and raw["id"] else new_id(), "type": raw["type"],
           "name": str(raw.get("name") or "")[:60], "x": num(raw.get("x"), 0.0), "y": num(raw.get("y"), 0.0),
           "w": max(0.0, num(raw.get("w"), 0.0)), "h": max(0.0, num(raw.get("h"), 0.0)),
           "rotation": num(raw.get("rotation"), 0.0) % 360, "layer": normalize_color(raw.get("layer")) or "#000000"}
    if obj["type"] == "group":
        obj["children"] = [child for child in (clean_object(item, depth + 1) for item in raw.get("children") or []) if child]
        if not obj["children"]:
            return None
    if obj["type"] == "poly":
        obj["points"] = point_list(raw.get("points"))
        obj["closed"] = raw.get("closed") is not False
        if len(obj["points"]) < 2:
            return None
    if obj["type"] == "path":
        paths = []
        for path in raw.get("paths") if isinstance(raw.get("paths"), list) else []:
            if isinstance(path, dict):
                points = point_list(path.get("points"))
                if len(points) >= 2:
                    paths.append({"points": points, "closed": bool(path.get("closed"))})
        if not paths:
            return None
        obj["paths"] = paths
    if obj["type"] == "image":
        source = raw.get("src")
        if not isinstance(source, str) or not source.startswith("data:image/"):
            return None
        obj["src"] = source
        obj["rasterMode"] = "dither" if raw.get("rasterMode") == "dither" else "grayscale"
        obj["invert"] = bool(raw.get("invert"))
    return obj


def clean_project(data):
    """Projet .alsproj (objet JSON) → {objects, layers, order, offset, profile, name} contrôlés."""
    if not isinstance(data, dict) or not isinstance(data.get("objects"), list):
        raise ProjectError("Projet illisible (.alsproj attendu).")
    if len(data["objects"]) > MAX_OBJECTS:
        raise ProjectError(f"Projet trop gros : {MAX_OBJECTS} objets au plus.")
    objects = [obj for obj in (clean_object(item) for item in data["objects"]) if obj]
    layers = {}
    raw_layers = data.get("layers") if isinstance(data.get("layers"), dict) else {}
    for key, value in raw_layers.items():
        color = normalize_color(key)
        if color and isinstance(value, dict):
            merged = default_layer(color, mode_or_line(value.get("mode")))
            merged.update({name: item for name, item in value.items() if name in merged and name not in ("color", "number")})
            if "image_mode" not in value:
                merged["image_mode"] = ""
            merged["color"] = color
            layers[color] = merged
    for obj in leaves(objects):
        layers.setdefault(obj["layer"], default_layer(obj["layer"]))
    machine = data.get("machine") if isinstance(data.get("machine"), dict) else {}
    offset = data.get("offset") if isinstance(data.get("offset"), list) and len(data.get("offset")) == 2 else [0, 0]
    return {"name": str(data.get("name") or "sans-titre")[:80], "objects": objects, "layers": layers,
            "order": data.get("order") if data.get("order") in PLAN_ORDERS else "layer",
            "offset": [num(offset[0], 0.0), num(offset[1], 0.0)],
            "profile": machine.get("profile") if isinstance(machine.get("profile"), dict) else default_profile()}


def document_svg(project):
    """SVG du document (documentSvg du web) : un <path> par contour, trait = couleur du calque. Images à part."""
    profile = effective_profile({**default_profile(), **project["profile"]})
    width, height = num(profile.get("width_mm"), 400), num(profile.get("height_mm"), 400)
    parts, warnings, used, skipped = [], [], [], {}
    for obj in leaves(project["objects"]):
        if obj["type"] == "image":
            continue
        layer = project["layers"].setdefault(obj["layer"], default_layer(obj["layer"]))
        if layer.get("mode") == "image":
            skipped[obj["layer"]] = skipped.get(obj["layer"], 0) + 1
            continue
        if obj["layer"] not in used:
            used.append(obj["layer"])
        for points, closed in world_paths(obj):
            if len(points) < 2:
                continue
            d = " ".join(("L" if index else "M") + f"{fmt(p[0])} {fmt(p[1])}" for index, p in enumerate(points)) + (" Z" if closed else "")
            identifier = re.sub(r"[^A-Za-z0-9_.-]", "_", obj["id"])[:60]
            parts.append(f'<path id="{identifier}" d="{d}" stroke="{obj["layer"]}" fill="none" stroke-width="0.1"/>')
    for color, count in skipped.items():
        warnings.append(f"Calque {project['layers'][color].get('name') or color} en mode image : {count} forme(s) vectorielle(s) ignorée(s).")
    for obj in leaves(project["objects"]):
        if obj["type"] == "image" and obj.get("rotation"):
            warnings.append(f"Image « {obj.get('name') or obj['id']} » : la rotation est ignorée pour la gravure.")
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{fmt(width)}mm" height="{fmt(height)}mm" '
           f'viewBox="0 0 {fmt(width)} {fmt(height)}">' + "".join(parts) + "</svg>")
    return svg, warnings, used


def raster_sources(project):
    """Images à tramer par le moteur (rasterSources du web) : image source + placement + réglages du calque."""
    sources = []
    for obj in leaves(project["objects"]):
        if obj["type"] != "image":
            continue
        layer = project["layers"].setdefault(obj["layer"], default_layer(obj["layer"]))
        if layer.get("enabled") is False:
            continue
        settings = {**layer_settings(layer), "mode": "image"}
        mode = layer.get("image_mode") or ("dither" if obj.get("rasterMode") == "dither" else "grayscale")
        sources.append({"image_b64": obj["src"], "width_mm": obj["w"], "height_mm": obj["h"],
                        "interval_mm": settings["interval_mm"], "mode": mode, "invert": bool(obj.get("invert")),
                        "settings": settings, "offset_mm": [obj["x"], obj["y"]], "name": str(layer.get("name") or "Image")[:80]})
    return sources


def plan_body(project, include_operations=False):
    """Corps de requête du moteur (buildPlanBody du web), trame des images comprise (raster_sources)."""
    svg, warnings, used = document_svg(project)
    body = {"svg": svg, "layers": {key: layer_settings(project["layers"][key]) for key in project["layers"] if key in used},
            "machine": machine_profile(project["profile"]), "order": project["order"], "images": [],
            "offset_mm": list(project["offset"]), "include_operations": bool(include_operations)}
    sources = raster_sources(project)
    if sources:
        body["raster_sources"] = sources
    return body, warnings


def bounds_of(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if not xs:
        return {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}
    return {"x": min(xs), "y": min(ys), "w": max(xs) - min(xs), "h": max(ys) - min(ys)}


def objects_from_parse(result, active_layer="#000000"):
    """Analyse d'un SVG par le moteur → objets « path » (points normalisés dans leur boîte) et noms de calques."""
    document = result.get("document") if isinstance(result.get("document"), dict) else {}
    infos = result.get("layers") if isinstance(result.get("layers"), dict) else {}
    objects, layers, points_total = [], {}, 0
    for shape in document.get("shapes") or []:
        polylines = [poly for poly in shape.get("paths") or [] if isinstance(poly, list)]
        points = [[num(p[0], 0.0), num(p[1], 0.0)] for poly in polylines for p in poly]
        if not points:
            continue
        points_total += len(points)
        box = bounds_of(points)
        key = normalize_color(shape.get("layer_key")) or active_layer
        info = infos.get(shape.get("layer_key")) if isinstance(infos.get(shape.get("layer_key")), dict) else {}
        if key not in layers:
            layers[key] = str(info.get("name") or f"Calque {key}")[:40]

        def norm(p):
            return [(num(p[0], 0.0) - box["x"]) / box["w"] if box["w"] else 0.0,
                    (num(p[1], 0.0) - box["y"]) / box["h"] if box["h"] else 0.0]

        paths = []
        for poly in polylines:
            if len(poly) < 2:
                continue
            closed = bool(shape.get("closed")) or (len(poly) > 2 and poly[0][0] == poly[-1][0] and poly[0][1] == poly[-1][1])
            paths.append({"points": [norm(p) for p in poly], "closed": closed})
        if paths:
            objects.append({"id": new_id(), "type": "path", "name": str(shape.get("id") or shape.get("kind") or "Tracé")[:60],
                            "x": box["x"], "y": box["y"], "w": box["w"], "h": box["h"], "rotation": 0, "layer": key,
                            "paths": paths})
    return {"objects": objects, "layers": layers, "warnings": list(document.get("warnings") or []), "points": points_total}


def operator_interlocks(settings):
    """Sécurités sans capteur que l'opérateur nomme dans la check-list (checklistNames du web, capot fermé)."""
    items = settings.get("interlocks") if isinstance(settings, dict) and isinstance(settings.get("interlocks"), list) else []
    return [str(item.get("name") or "") for item in items
            if isinstance(item, dict) and item.get("required") and item.get("kind") in OPERATOR_INTERLOCK_KINDS and item.get("name")]
