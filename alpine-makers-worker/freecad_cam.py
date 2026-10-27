# -*- coding: utf-8 -*-
"""
Alpine Forge 3D — moteur FreeCAD CAM (« Préparer pour une CNC »), tranche T2 : projet CAM v2.

  FreeCADCmd.exe freecad_cam.py cam_request.json WORKDIR

La requête arrive comme FICHIER JSON ({"op": "capabilities"}, {"op": "compute", "project": …, "source": "<nom>"}
ou {"op": "preview", "project": …, "source": "<nom>"}), jamais comme code : ce moteur ne contient ni eval ni exec
piloté par la requête et REVALIDE le projet (cam_payload quand il connaît la version, puis cam_ops_v2.normalize_project
qui pose les défauts du contrat v2 et refuse tout réglage muet). Seuls les modules CAM installés avec FreeCAD sont importés.

Organisation :
- fonctions de calcul PURES (orientation, brut, origine, profondeurs, découverte des posts, capacités, fabricabilité,
  fiche de réglage) testables sans FreeCAD ; la correspondance réglage → propriété FreeCAD vit dans ``cam_ops_v2`` ;
- ``FreeCadCam`` : seul endroit qui parle à FreeCAD (Job, brut, outils, opérations, dressups, post, tessellation) ;
- ``compute`` / ``preview`` / ``capabilities`` : l'enchaînement, sur un adaptateur injectable.

Résultat sur stdout : une ligne ``RAPHAL_FREECAD_RESULT=<json>``. Progression réelle dans ``cam_progress.json`` (opérations
terminées / total ; jamais de pourcentage inventé au sein d'une opération). Annulation propre : la présence de
``.cancel.json`` dans WORKDIR (posé par l'agent) arrête le calcul entre deux opérations. Aucun G-code n'est envoyé à une
machine : le moteur écrit des fichiers dans WORKDIR, rien d'autre.
"""
import html
import importlib
import importlib.util
import json
import math
import os
import sys
import time
import traceback

RESULT_PREFIX = "RAPHAL_FREECAD_RESULT="
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import cam_ops_v2 as ops  # noqa: E402  (à plat à côté du moteur, dans le dépôt comme sur le Worker)

OPERATION_MODULES = ops.OPERATION_MODULES
# Formes d'outils FreeCAD (Mod/CAM/Tools/Shape/<forme>.fcstd) ↔ familles Alpine.
SHAPE_FAMILIES = {"endmill": "endmill", "drill": "drill", "ballend": "ballend", "v-bit": "vbit", "chamfer": "chamfer",
                  "bullnose": "bullnose", "thread-mill": "threadmill", "radius": "radius", "dovetail": "dovetail",
                  "slittingsaw": "slittingsaw", "taperedballnose": "taperedballnose", "reamer": "reamer", "tap": "tap",
                  "probe": "probe"}
FAMILY_SHAPES = {family: shape + ".fcstd" for family, shape in ops.FAMILY_SHAPES.items()}
MAX_FACETS = 2000000
SCALE_MAX_MM = 3000.0
SCALE_MIN_MM = 0.5
SIMULATION_LIMIT = 20000
GCODE_MAX_BYTES = 64 * 1024 * 1024
PREVIEW_STL_MAX_BYTES = 8 * 1024 * 1024
PROGRESS_FILE = "cam_progress.json"
CANCEL_FILE = ".cancel.json"
comment_text = ops.comment_text
climb_direction = ops.climb_direction


def result(ok, **kwargs):
    print(RESULT_PREFIX + json.dumps({"ok": bool(ok), **kwargs}, ensure_ascii=False, allow_nan=False), flush=True)


class CamEngineError(RuntimeError):
    """Refus ou échec lisible par l'utilisateur."""


class CamCancelled(CamEngineError):
    """Annulation demandée par l'agent (.cancel.json) : arrêt propre entre deux opérations."""


def load_contract_modules():
    """cam_payload et cam_gcode_check : paquet ``worker_agent`` dans le dépôt, modules à plat sur le Worker."""
    try:
        from worker_agent import cam_gcode_check, cam_payload  # dépôt (tests, site)
    except ImportError:
        import cam_gcode_check  # Worker installé : fichiers à plat à côté de ce moteur
        import cam_payload
    return cam_payload, cam_gcode_check


# ---------------------------------------------------------------------------------------------
# Requête
# ---------------------------------------------------------------------------------------------
REQUEST_OPS = ("capabilities", "compute", "preview")


def read_request(path, workdir, cam_payload):
    """Lit et REVALIDE la requête ; rend (op, projet v2 normalisé ou None, chemin de la source ou None)."""
    if not os.path.isfile(path) or os.path.getsize(path) > 2 * 1024 * 1024:
        raise CamEngineError("Requête CAM absente ou trop volumineuse.")
    with open(path, "r", encoding="utf-8") as handle:
        request = json.load(handle)
    if not isinstance(request, dict):
        raise CamEngineError("Requête CAM invalide.")
    unknown = sorted(set(request) - {"op", "project", "source"})
    if unknown:
        raise CamEngineError("Requête CAM invalide : champ « %s » refusé." % unknown[0])
    op = request.get("op")
    if op == "capabilities":
        if "project" in request or "source" in request:
            raise CamEngineError("La lecture des capacités ne prend ni projet ni fichier.")
        return "capabilities", None, None
    if op not in REQUEST_OPS:
        raise CamEngineError("Opération CAM inconnue.")
    project = ops.normalize_request_project(request.get("project"), cam_payload)
    source_name = request.get("source")
    if not isinstance(source_name, str) or source_name != project["source"]["file"]:
        raise CamEngineError("Le fichier source joint ne correspond pas au projet.")
    source = os.path.join(workdir, source_name)
    if os.path.dirname(os.path.abspath(source)) != os.path.abspath(workdir) or not os.path.isfile(source):
        raise CamEngineError("Fichier source introuvable dans le dossier du job.")
    return op, project, source


# ---------------------------------------------------------------------------------------------
# Calculs purs (sans FreeCAD)
# ---------------------------------------------------------------------------------------------
def orientation_steps(orientation):
    """Rotations (axe, degrés) à appliquer au modèle, dans l'ordre : face du dessus, retournement, rotation Z."""
    to_top = {"+z": [], "-z": [("x", 180)], "+x": [("y", -90)], "-x": [("y", 90)],
              "+y": [("x", 90)], "-y": [("x", -90)]}
    steps = list(to_top[orientation["top_face"]])
    if orientation.get("flip"):
        steps.append(("x", 180))
    if orientation.get("rotation_z_deg"):
        steps.append(("z", int(orientation["rotation_z_deg"])))
    return steps


def stock_box(model_bounds, stock, factor):
    """Boîte du brut (mm, repère du modèle orienté) : (min[3], max[3]) et décalage à donner au modèle.

    ``from_model`` : boîte englobante + marges (x et y de chaque côté, dessus, dessous).
    ``explicit`` : boîte saisie (ou cylindre : boîte englobante du cylindre) ; pièce placée selon ``placement`` :
    ``center_top`` (centrée en X/Y, posée sous la marge du dessus = v1), ``min_corner`` (coin minimal de la pièce au
    coin minimal du brut, dessus idem), ``custom`` (décalage ``placement_offset`` ajouté à ``center_top``).
    """
    low, high = [float(v) for v in model_bounds[0]], [float(v) for v in model_bounds[1]]
    margins = {key: float(value) * factor for key, value in stock["margins"].items()}
    if stock["mode"] == "from_model":
        return ([low[0] - margins["x"], low[1] - margins["y"], low[2] - margins["bottom"]],
                [high[0] + margins["x"], high[1] + margins["y"], high[2] + margins["top"]]), [0.0, 0.0, 0.0]
    if stock.get("shape") == "cylinder":
        diameter = float(stock["diameter"]) * factor
        size = [diameter, diameter, float(stock["height"]) * factor]
    else:
        size = [float(stock["size"][axis]) * factor for axis in ("x", "y", "z")]
    center = [(low[0] + high[0]) / 2.0, (low[1] + high[1]) / 2.0]
    top = high[2] + margins["top"]
    placement = stock.get("placement", "center_top")
    if placement == "min_corner":
        box_low = [low[0], low[1], top - size[2]]
    else:
        box_low = [center[0] - size[0] / 2.0, center[1] - size[1] / 2.0, top - size[2]]
    if placement == "custom":
        offset = stock.get("placement_offset") or {}
        box_low = [box_low[0] - float(offset.get("x", 0)) * factor, box_low[1] - float(offset.get("y", 0)) * factor,
                   box_low[2] - float(offset.get("z", 0)) * factor]
    return (box_low, [box_low[0] + size[0], box_low[1] + size[1], box_low[2] + size[2]]), [0.0, 0.0, 0.0]


def origin_point(box, origin, factor):
    """Point du brut choisi comme origine pièce (mm, repère du modèle), décalage compris."""
    low, high = box
    middle = [(low[i] + high[i]) / 2.0 for i in range(3)]
    presets = {
        "top_front_left": (low[0], low[1], high[2]), "top_front_right": (high[0], low[1], high[2]),
        "top_rear_left": (low[0], high[1], high[2]), "top_rear_right": (high[0], high[1], high[2]),
        "top_center": (middle[0], middle[1], high[2]), "bottom_center": (middle[0], middle[1], low[2]),
        "center": tuple(middle), "custom": (low[0], low[1], high[2]),
    }
    base = presets[origin["preset"]]
    offset = origin.get("offset") or {}
    return [base[0] + float(offset.get("x", 0)) * factor, base[1] + float(offset.get("y", 0)) * factor,
            base[2] + float(offset.get("z", 0)) * factor]


def shift_box(box, origin):
    return ([box[0][i] - origin[i] for i in range(3)], [box[1][i] - origin[i] for i in range(3)])


def model_checks(info):
    """Contrôles du modèle importé (mm). ``info`` : {is_null, is_mesh, facets, bounds, is_solid, is_closed}."""
    if info.get("is_null"):
        raise CamEngineError("Le fichier source ne contient aucune géométrie exploitable.")
    if info.get("facets", 0) > MAX_FACETS:
        raise CamEngineError("Maillage trop complexe (%d facettes, %d au plus) : simplifie-le ou exporte un STEP."
                             % (info["facets"], MAX_FACETS))
    low, high = info["bounds"]
    size = [high[i] - low[i] for i in range(3)]
    if min(size) <= 1e-6:
        raise CamEngineError("Le modèle a une dimension nulle : une pièce à usiner doit avoir un volume.")
    if max(size) > SCALE_MAX_MM:
        raise CamEngineError("Échelle suspecte : plus de %g mm. Vérifie les unités du modèle (mm ou pouces)." % SCALE_MAX_MM)
    if max(size) < SCALE_MIN_MM:
        raise CamEngineError("Échelle suspecte : moins de %g mm dans toutes les directions. Vérifie les unités." % SCALE_MIN_MM)
    warnings = []
    if info.get("is_mesh"):
        warnings.append({"code": "mesh_geometry", "severity": "warning",
                         "message": "Géométrie maillée (STL, 3MF…) : surfaces facettées, détection des poches et trous "
                                    "moins fiable (perçage automatique indisponible). Préfère un STEP quand il existe.", "operation": ""})
    if not info.get("is_solid"):
        warnings.append({"code": "not_a_solid", "severity": "warning",
                         "message": "Le modèle n'est pas un solide fermé : les profondeurs et les détections peuvent être "
                                    "fausses.", "operation": ""})
    return warnings


def depth_window(kind, stock_top, model_top, geometry_final, entry):
    """(départ, fin, avertissement) en Z absolu. Jamais plus profond que la géométrie ni que ``final`` sous le brut.

    - surfaçage : du dessus du brut jusqu'à ``final`` sous le brut, jamais sous le dessus du modèle ;
    - autres : la profondeur vient de la géométrie (FreeCAD), bornée par ``final``.
    """
    start = stock_top if entry.get("start") is None else stock_top - entry["start"]
    limit = stock_top - entry["final"]
    warning = None
    if kind == "facing":
        final = max(model_top, limit)
        if limit < model_top - 1e-6:
            warning = ("facing_limited", "Surfaçage arrêté au dessus de la pièce (%.3f mm) : la profondeur demandée "
                                         "entamerait le modèle." % (model_top,))
        return start, final, warning
    final = limit if geometry_final is None else max(geometry_final, limit)
    if geometry_final is not None and limit > geometry_final + 1e-6:
        warning = ("depth_limited", "Profondeur bornée par l'opération (%.3f mm sous le brut) : la géométrie descend "
                                    "plus bas et ne sera pas entièrement usinée." % entry["final"])
    return start, final, warning


def _segment_hits_box(a, b, low, high):
    """Le segment [a, b] traverse-t-il la boîte (méthode des dalles) ?"""
    t0, t1 = 0.0, 1.0
    for axis in range(3):
        delta = b[axis] - a[axis]
        if abs(delta) < 1e-12:
            if a[axis] < low[axis] or a[axis] > high[axis]:
                return False
            continue
        near, far = (low[axis] - a[axis]) / delta, (high[axis] - a[axis]) / delta
        if near > far:
            near, far = far, near
        t0, t1 = max(t0, near), min(t1, far)
        if t0 > t1:
            return False
    return True


def manufacturability(project, tools, stock, segments, faces, op_reports):
    """Contrôles de fabricabilité (mm, repère pièce). ``faces`` : [{normal_z, zmin, cyl_radius}] du modèle placé."""
    issues = []
    radius_max = max((tool["diameter"] / 2.0 for tool in tools.values()), default=0.0)
    factor = 25.4 if project["units"] == "inch" else 1.0
    for fixture in project["fixtures"]:
        low = [fixture["min"][axis] * factor - radius_max for axis in ("x", "y", "z")]
        high = [fixture["max"][axis] * factor + radius_max for axis in ("x", "y", "z")]
        hit = next((segment for segment in segments if segment["k"] != "tool"
                    and _segment_hits_box(segment["a"], segment["b"], low, high)), None)
        if hit:
            issues.append({"code": "fixture_collision", "severity": "error", "operation": "",
                           "message": "Le parcours traverse la bride « %s » (rayon d'outil compris) près de X%.1f Y%.1f Z%.1f."
                                      % (fixture["name"], hit["b"][0], hit["b"][1], hit["b"][2])})
    if stock is not None:
        low, high = stock
        margin = 2.0 * radius_max + 1.0
        outside = next((segment for segment in segments if segment["k"] in ("cut", "plunge") and any(
            point[0] < low[0] - margin or point[0] > high[0] + margin or point[1] < low[1] - margin or point[1] > high[1] + margin
            for point in (segment["a"], segment["b"]))), None)
        if outside:
            issues.append({"code": "path_outside_stock", "severity": "warning", "operation": "",
                           "message": "Une coupe sort du brut de plus d'un diamètre d'outil : vérifie le brut et l'origine."})
        travel = project["machine"]["travel"]
        extent = [0.0, 0.0, 0.0]
        for segment in segments:
            if segment["k"] == "tool":
                continue
            for point in (segment["a"], segment["b"]):
                for axis in range(3):
                    extent[axis] = max(extent[axis], abs(point[axis] - (low[axis] if axis < 2 else high[2])))
        for axis, key in enumerate("xyz"):
            if extent[axis] > float(travel[key]) + radius_max + 0.01:
                issues.append({"code": "path_outside_travel", "severity": "error", "operation": "",
                               "message": "Le parcours s'éloigne de %.1f mm du brut sur %s : course machine %g mm dépassée."
                                          % (extent[axis], key.upper(), float(travel[key]))})
                break
    model_bottom = min((face["zmin"] for face in faces), default=None)
    if any(face["normal_z"] < -0.01 and model_bottom is not None and face["zmin"] > model_bottom + 1e-3 for face in faces):
        issues.append({"code": "undercut", "severity": "warning", "operation": "",
                       "message": "Contre-dépouille : une face regarde vers le bas au-dessus du fond. Une machine 3 axes "
                                  "ne peut pas l'atteindre depuis le dessus."})
    endmill_radius = min((tool["diameter"] / 2.0 for tool in tools.values() if tool["family"] == "endmill"), default=None)
    if endmill_radius and any(face.get("cyl_radius") and face["cyl_radius"] < endmill_radius - 1e-6 and face.get("concave")
                              and face.get("partial", True) for face in faces):
        issues.append({"code": "internal_radius_small", "severity": "warning", "operation": "",
                       "message": "Un rayon intérieur du modèle est plus petit que la plus petite fraise (%.2f mm) : "
                                  "il restera de la matière dans ce coin." % endmill_radius})
    for report in op_reports:
        if report.get("machining") and report["state"] == "empty":
            issues.append({"code": "operation_empty", "severity": "warning", "operation": report["id"],
                           "message": "L'opération « %s » n'a produit aucun parcours (%s)." % (report["name"], report.get("error") or "aucune géométrie détectée")})
        if report.get("machining") and report["state"] == "error":
            issues.append({"code": "operation_error", "severity": "error", "operation": report["id"],
                           "message": "L'opération « %s » est en erreur : %s" % (report["name"], report.get("error") or "cause inconnue")})
        for code, severity, message in report.get("issues", []):
            issues.append({"code": code, "severity": severity, "operation": report["id"], "message": message})
    return issues


def setup_sheet_html(project, summary, stock, issues, validation, freecad_version, manual=(), files=()):
    """Fiche de réglage : HTML simple, sans script, chaque valeur échappée."""
    e = lambda value: html.escape(str(value), quote=True)
    machine = project["machine"]
    rows = []
    rows.append("<h1>Fiche de réglage — %s</h1>" % e(project["name"]))
    rows.append("<p>Machine : %s · contrôleur %s · post-processeur %s · FreeCAD %s · moteur CAM %s</p>"
                % (e(machine["label"]), e(machine["controller"]), e(machine["post_processor"]), e(freecad_version), e(ops.ENGINE_VERSION)))
    if stock:
        size = [stock[1][i] - stock[0][i] for i in range(3)]
        rows.append("<p>Brut : %.2f × %.2f × %.2f mm · origine %s (%s)</p>"
                    % (size[0], size[1], size[2], e(project["origin"]["preset"]), e(project["origin"]["wcs"])))
    rows.append("<h2>Outils</h2><table><tr><th>N°</th><th>Nom</th><th>Famille</th><th>Ø</th><th>Taillée</th><th>Broche</th><th>Lu par FreeCAD comme</th></tr>")
    shapes = summary.get("tool_shapes") or {}
    for tool in project["tools"]:
        rows.append("<tr><td>T%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s tr/min</td><td>%s</td></tr>"
                    % (e(tool["number"]), e(tool["name"]), e(tool["family"]), e(tool["diameter"]), e(tool["flute_length"]),
                       e(tool["spindle_rpm"]), e(", ".join(shapes.get(str(tool["number"]), [])) or "—")))
    rows.append("</table><h2>Programmes</h2><ol>")
    for item in files:
        rows.append("<li>%s — outils %s — %s lignes — sha256 %s…</li>" % (e(item["name"]), e(", ".join("T%d" % t for t in item["tool_numbers"])),
                                                                          e(item["lines"]), e(item["sha256"][:12])))
    rows.append("</ol><h2>Séquence</h2><ol>")
    for operation in summary.get("operations", []):
        rows.append("<li>%s (%s) — %s%s</li>" % (e(operation["name"]), e(operation["type"]), e(operation["state"]),
                                                   " — durée estimée %s s" % e(operation["estimated_s"]) if operation.get("estimated_s") is not None else ""))
    rows.append("</ol><h2>Étapes manuelles</h2><ol>")
    for step in manual:
        rows.append("<li>%s : %s%s</li>" % (e(step["kind"]), e(step["message"]), " (fichier %s)" % e(step["file"]) if step.get("file") else ""))
    rows.append("</ol><h2>Contrôles</h2><ul>")
    for entry in list(validation.get("errors", [])) + list(validation.get("warnings", [])) + list(issues):
        rows.append("<li>%s : %s</li>" % (e(entry["code"]), e(entry["message"])))
    rows.append("</ul><h2>Non simulé</h2><ul>")
    for text in summary.get("not_simulated", []):
        rows.append("<li>%s</li>" % e(text))
    rows.append("</ul><p>Durée indicative : %s s (estimation d'après les longueurs et les avances%s). Les vitesses et avances sont "
                "des points de départ, à vérifier par l'opérateur.</p>"
                % (e(summary.get("time_estimate_s", 0)), ", trapèze d'accélération" if machine.get("max_accel") else ", sans accélérations"))
    return ("<!doctype html><html lang=\"fr\"><head><meta charset=\"utf-8\"><title>Fiche de réglage</title>"
            "<style>body{font-family:sans-serif;margin:24px}table{border-collapse:collapse}td,th{border:1px solid #999;"
            "padding:4px 8px}</style></head><body>" + "".join(rows) + "</body></html>")


# ---------------------------------------------------------------------------------------------
# Découverte des post-processeurs et des capacités
# ---------------------------------------------------------------------------------------------
def post_arguments_of(module):
    """Arguments d'un post « script » lus depuis son ``parser`` argparse, sans appeler ``export``."""
    parser = getattr(module, "parser", None)
    actions = getattr(parser, "_actions", None) or []
    arguments = []
    for action in actions:
        for option in getattr(action, "option_strings", []) or []:
            if option.startswith("--") and option not in ("--help",):
                default = getattr(action, "default", None)
                arguments.append({"name": option, "help": str(getattr(action, "help", "") or "")[:300],
                                  "default": "" if default in (None, False) else str(default)[:120],
                                  "type": "flag" if getattr(action, "nargs", None) == 0 else "str"})
    return arguments


def discover_posts(scripts_dir, load_module=None):
    """Post-processeurs installés : ``<nom>_post.py`` (sauf ``*_test_post.py``), arguments lus sans export."""
    posts = []
    if not scripts_dir or not os.path.isdir(scripts_dir):
        return posts
    for file_name in sorted(os.listdir(scripts_dir)):
        if not file_name.endswith("_post.py") or file_name.endswith("_test_post.py"):
            continue
        name = file_name[:-len("_post.py")]
        if not name or not all(char.isalnum() or char == "_" for char in name) or not name[0].isalpha():
            continue
        name = name.lower()
        refactored = name.startswith("refactored_")
        arguments = []
        if not refactored and load_module is not None:
            try:
                arguments = post_arguments_of(load_module(os.path.join(scripts_dir, file_name), name + "_post"))
            except Exception:
                arguments = []
        names = {argument["name"] for argument in arguments}
        posts.append({"name": name, "file": file_name, "refactored": refactored, "arguments": arguments,
                      "units_argument": "--inches" in names, "translate_drill": "--translate_drill" in names,
                      "wait_for_spindle": "--wait-for-spindle" in names, "tool_change": "--tool-change" in names or "--tool_change" in names})
    return posts


def _load_module(path, module_name):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _importable(module_name):
    try:
        importlib.import_module(module_name)
        return True
    except Exception:
        return False


def capabilities(app):
    """Capacités CAM réelles de cette installation de FreeCAD (contrat v2 : détail par opération, forme, dressup, post)."""
    cam_available = _importable("Path.Main.Job")
    scripts_dir = ""
    try:
        import Path.Post.scripts as scripts
        scripts_dir = os.path.dirname(scripts.__file__)
    except Exception:
        scripts_dir = os.path.join(app.getHomePath(), "Mod", "CAM", "Path", "Post", "scripts")
    shapes_dir = os.path.join(app.getHomePath(), "Mod", "CAM", "Tools", "Shape")
    present_shapes = set()
    if os.path.isdir(shapes_dir):
        for file_name in os.listdir(shapes_dir):
            stem, extension = os.path.splitext(file_name)
            if extension.lower() == ".fcstd":
                present_shapes.add(stem.lower())
    find = importlib.util.find_spec
    ocl_version = None
    ocl_present = find("opencamlib") is not None
    if ocl_present:
        try:
            import opencamlib
            ocl_version = str(opencamlib.version()) if hasattr(opencamlib, "version") else None
        except Exception:
            ocl_version = None
    dependencies = {"opencamlib": ocl_present, "ocl": find("ocl") is not None, "area": find("area") is not None,
                    "opencamlib_version": ocl_version}
    satisfied_dependency = {"ocl": ocl_present or dependencies["ocl"], "area": dependencies["area"]}
    operations_detail = []
    for alpine, module in OPERATION_MODULES.items():
        requires = list(ops.OPERATION_REQUIREMENTS.get(alpine, ()))
        importable = cam_available and _importable(module)
        operations_detail.append({"id": alpine, "module": module, "importable": importable, "requires": requires,
                                  "satisfied": importable and all(satisfied_dependency.get(name, False) for name in requires),
                                  "tool_families": list(ops.OPERATION_TOOL_FAMILIES.get(alpine, ops.TOOL_FAMILIES))})
    tool_shapes = [{"family": family, "shape_file": shape + ".fcstd", "present": shape in present_shapes}
                   for family, shape in ops.FAMILY_SHAPES.items()]
    dressups = [{"id": name, "module": module, "importable": cam_available and _importable(module)} for name, module in ops.DRESSUP_MODULES.items()]
    controller_props = []
    if cam_available:
        try:
            import Path.Tool.Controller as PathToolController
            controller_props = sorted(name for name in ("HorizFeed", "VertFeed", "RampFeed", "HorizRapid", "VertRapid", "SpindleSpeed", "SpindleDir")
                                      if name in str(getattr(PathToolController, "__doc__", "")) or True)
            controller_props = _controller_property_names(PathToolController) or controller_props
        except Exception:
            controller_props = []
    restrictions = ["CAMSimulator / PathSimulator plantent sous freecadcmd : aucune simulation d'enlèvement de matière.",
                    "Pas de rampe ni d'entrée/sortie (RampEntry, LeadInOut : classes GUI sans Create en 1.1.3).",
                    "Pocket/Profile/Adaptive/MillFace/Slot/Helix/Drilling lisent seulement le diamètre de l'outil (forme plate).",
                    "M6 réel seulement en atc_m6 (hors GRBL) ; G43/G49 jamais émis ; cycles G81-G83 traduits en G0/G1 par grbl_post."]
    return {
        "freecad_version": ".".join(str(part) for part in app.Version()[:3]),
        "engine_version": ops.ENGINE_VERSION, "contract_version": ops.CONTRACT_VERSION,
        "cam_available": cam_available,
        "operations": [item["id"] for item in operations_detail if item["satisfied"]],
        "operations_detail": operations_detail,
        "operations_refused": dict(ops.OPERATIONS_REFUSED),
        "post_processors": discover_posts(scripts_dir, _load_module) if cam_available else [],
        "simulation": False,
        "dependencies": dependencies,
        "tool_families": [item["family"] for item in tool_shapes if item["present"]],
        "tool_shapes": tool_shapes,
        "tool_families_refused": dict(ops.TOOL_FAMILIES_REFUSED),
        "dressups": dressups,
        "dressups_refused": dict(ops.DRESSUP_REFUSED),
        "tool_change_policies": list(ops.TOOL_CHANGE_POLICIES),
        "tool_controller_props": controller_props,
        "restrictions": restrictions,
    }


def _controller_property_names(module):
    """Propriétés d'un contrôleur d'outil FreeCAD (lues sur un document temporaire, sans Job)."""
    try:
        import FreeCAD as App
        document = App.newDocument("AlpineCamProbe")
        try:
            controller = module.Create("Probe", assignViewProvider=False)
            names = [name for name in controller.PropertiesList
                     if name in ("HorizFeed", "VertFeed", "RampFeed", "HorizRapid", "VertRapid", "SpindleSpeed", "SpindleDir", "ToolNumber")]
        finally:
            App.closeDocument(document.Name)
        return sorted(names)
    except Exception:
        return []


# ---------------------------------------------------------------------------------------------
# Adaptateur FreeCAD (seul endroit qui manipule les objets FreeCAD)
# ---------------------------------------------------------------------------------------------
class FreeCadCam:
    def __init__(self):
        import FreeCAD as App
        import Part
        self.App, self.Part = App, Part
        self.doc = None
        self.model = None
        self.base = None
        self.job = None
        self.mesh = False
        self.facets = 0
        self._shape_cache = {}
        self._controller_notes = {}

    # -- géométrie ---------------------------------------------------------------------------
    def import_model(self, path, kind):
        App, Part = self.App, self.Part
        if kind == "fcstd":
            source = App.openDocument(path)
            shapes = [obj.Shape for obj in source.Objects if hasattr(obj, "Shape") and not obj.Shape.isNull()
                      and obj.Shape.Solids]
            shape = Part.makeCompound([shape for shape in shapes]) if len(shapes) > 1 else (shapes[0] if shapes else Part.Shape())
            shape = shape.copy()
            App.closeDocument(source.Name)
        elif kind in ("step", "stp", "iges", "igs", "brep", "brp"):
            shape = Part.Shape()
            shape.read(path)
        else:
            import Mesh
            mesh = Mesh.Mesh()
            if kind == "3mf":
                document = App.newDocument("AlpineCamMesh")
                try:
                    Mesh.insert(path, document.Name)
                    meshes = [obj.Mesh for obj in document.Objects if hasattr(obj, "Mesh") and obj.Mesh.CountFacets]
                    if meshes:
                        for item in meshes:
                            mesh.addMesh(item)
                    else:
                        import Import
                        Import.insert(path, document.Name)
                        shapes = [obj.Shape for obj in document.Objects if hasattr(obj, "Shape") and not obj.Shape.isNull()]
                        if shapes:
                            self.doc_shape = shapes[0].copy()
                finally:
                    App.closeDocument(document.Name)
            else:
                mesh.read(path)
            if getattr(self, "doc_shape", None) is not None:
                shape = self.doc_shape
            else:
                self.facets = int(mesh.CountFacets)
                if self.facets > MAX_FACETS:
                    raise CamEngineError("Maillage trop complexe (%d facettes, %d au plus)." % (self.facets, MAX_FACETS))
                if not self.facets:
                    raise CamEngineError("Le maillage est vide.")
                self.mesh = True
                shape = Part.Shape()
                shape.makeShapeFromMesh(mesh.Topology, 0.05)
                try:
                    shape = shape.removeSplitter()
                except Exception:
                    pass
                try:
                    if shape.isClosed():
                        solid = Part.makeSolid(shape)
                        if solid and not solid.isNull():
                            shape = solid
                except Exception:
                    pass
        return shape

    def model_info(self, shape):
        if shape.isNull():
            return {"is_null": True}
        box = shape.BoundBox
        return {"is_null": False, "is_mesh": self.mesh, "facets": self.facets,
                "bounds": ([box.XMin, box.YMin, box.ZMin], [box.XMax, box.YMax, box.ZMax]),
                "is_solid": bool(shape.Solids) and all(solid.isClosed() for solid in shape.Solids),
                "solids": len(shape.Solids)}

    def transform(self, shape, scale, steps):
        App = self.App
        shape = shape.copy()
        if abs(scale - 1.0) > 1e-12:
            matrix = App.Matrix()
            matrix.scale(scale, scale, scale)
            shape = shape.transformGeometry(matrix)
        axes = {"x": App.Vector(1, 0, 0), "y": App.Vector(0, 1, 0), "z": App.Vector(0, 0, 1)}
        for axis, angle in steps:
            shape.rotate(App.Vector(0, 0, 0), axes[axis], angle)
        return shape

    def translate(self, shape, offset):
        shape = shape.copy()
        shape.translate(self.App.Vector(-offset[0], -offset[1], -offset[2]))
        return shape

    def faces(self, shape):
        out = []
        for face in shape.Faces:
            try:
                u0, u1, v0, v1 = face.ParameterRange
                normal = face.normalAt((u0 + u1) / 2.0, (v0 + v1) / 2.0)
            except Exception:
                continue
            entry = {"normal_z": float(normal.z), "zmin": float(face.BoundBox.ZMin)}
            surface = face.Surface
            if type(surface).__name__ == "Cylinder" and abs(abs(surface.Axis.z) - 1.0) < 1e-6:
                entry["cyl_radius"] = float(surface.Radius)
                center = surface.Center
                point = face.valueAt((u0 + u1) / 2.0, (v0 + v1) / 2.0)
                radial = self.App.Vector(point.x - center.x, point.y - center.y, 0)
                entry["concave"] = radial.Length > 1e-9 and (radial.x * normal.x + radial.y * normal.y) < 0
                entry["partial"] = (u1 - u0) < 2.0 * math.pi - 1e-3
            out.append(entry)
        return out

    def tessellate(self, shape, path, max_bytes=PREVIEW_STL_MAX_BYTES):
        """STL binaire du modèle placé pour l'aperçu 3D, borné en taille (déflexion grossie si besoin)."""
        import Mesh
        for deflection in (0.1, 0.3, 1.0, 3.0):
            try:
                mesh = Mesh.Mesh(shape.tessellate(deflection))
                mesh.write(path)
            except Exception:
                return None
            size = os.path.getsize(path)
            if size <= max_bytes:
                return {"facets": int(mesh.CountFacets), "bytes": int(size), "deflection_mm": deflection}
        try:
            os.remove(path)
        except OSError:
            pass
        return None

    # -- Job CAM -------------------------------------------------------------------------------
    def create_job(self, shape, stock, wcs="G54", cylinder=None):
        App = self.App
        self.doc = App.newDocument("AlpineCam")
        App.setActiveDocument(self.doc.Name)
        self.model = self.doc.addObject("Part::Feature", "Piece")
        self.model.Shape = shape
        self.doc.recompute()
        import Path.Main.Job as PathJob
        import Path.Main.Stock as PathStock
        self.job = PathJob.Create("Job", [self.model], None)
        self.base = self.job.Model.Group[0]
        if "Fixtures" in self.job.PropertiesList:
            self.job.Fixtures = [wcs]
        old = self.job.Stock
        low, high = stock
        if cylinder:
            placement = App.Placement(App.Vector(cylinder["center"][0], cylinder["center"][1], low[2]), App.Rotation())
            self.job.Stock = PathStock.CreateCylinder(self.job, float(cylinder["radius"]), float(high[2] - low[2]), placement)
        else:
            extent = App.Vector(high[0] - low[0], high[1] - low[1], high[2] - low[2])
            placement = App.Placement(App.Vector(low[0], low[1], low[2]), App.Rotation())
            self.job.Stock = PathStock.CreateBox(self.job, extent, placement)
        if old is not None and old.Name != self.job.Stock.Name:
            self.doc.removeObject(old.Name)
        for controller in list(self.job.Tools.Group):
            tool = getattr(controller, "Tool", None)
            self.job.Tools.removeObject(controller)
            self.doc.removeObject(controller.Name)
            if tool is not None:
                self.doc.removeObject(tool.Name)
        self.doc.recompute()
        import PathScripts.PathUtils as PathUtils

        class _Chooser:
            """Sous FreeCADCmd, aucune fenêtre ne choisit l'outil d'une opération : c'est nous."""
            current = None

            def selectedToolController(self):
                return _Chooser.current

            def chooseToolController(self, controllers):
                return _Chooser.current

        self._chooser = _Chooser
        PathUtils.UserInput = _Chooser()
        self._shape_cache = {}

    def add_controller(self, tool, key, label):
        """Contrôleur + ToolBit : les propriétés de forme sont posées APRÈS l'attache (audit §4), puis relues."""
        from Path.Tool.toolbit import ToolBit
        import Path.Tool.Controller as PathToolController
        bit = ToolBit.from_shape_id(FAMILY_SHAPES[tool["family"]], label=comment_text(tool["name"]))
        tool_object = bit.attach_to_doc(doc=self.doc)
        applied, skipped = {}, []
        for name, value in ops.tool_properties(tool):
            if name in tool_object.PropertiesList:
                try:
                    setattr(tool_object, name, value)
                    got = getattr(tool_object, name)
                    applied[name] = float(got.Value) if hasattr(got, "Value") else got
                except Exception as error:
                    skipped.append("%s (%s)" % (name, type(error).__name__))
            else:
                skipped.append(name)
        number, feed_xy, feed_plunge, rpm, direction = key
        controller = PathToolController.Create(label, tool=tool_object, toolNumber=int(number), assignViewProvider=False)
        controller.HorizFeed = "%r mm/min" % float(feed_xy)
        controller.VertFeed = "%r mm/min" % float(feed_plunge)
        if tool.get("feed_ramp") and hasattr(controller, "RampFeed"):
            try:
                controller.RampFeed = "%r mm/min" % float(tool["feed_ramp"])
            except Exception:
                pass
        controller.SpindleSpeed = float(rpm)
        if hasattr(controller, "SpindleDir"):
            controller.SpindleDir = "Forward" if direction == "cw" else "Reverse"
        self.job.Proxy.addToolController(controller)
        self.doc.recompute()
        real = float(controller.Tool.Diameter.Value)
        if abs(real - float(tool["diameter"])) > 1e-6:
            raise CamEngineError("Outil T%d : FreeCAD a gardé un diamètre de %.3f mm au lieu de %.3f mm." % (int(number), real, tool["diameter"]))
        # Un FeaturePython n'accepte aucun attribut libre : les propriétés posées / ignorées sont gardées à part.
        self._controller_notes[controller.Name] = {"applied": applied, "skipped": skipped}
        return controller

    def controller_notes(self, controller):
        return self._controller_notes.get(getattr(controller, "Name", ""), {})

    @staticmethod
    def _set(obj, name, value):
        if name in obj.PropertiesList:
            try:
                obj.setExpression(name, None)
            except Exception:
                pass
            setattr(obj, name, value)
            return True
        return False

    # -- sélection géométrique ---------------------------------------------------------------
    def _face_entries(self):
        if "faces" not in self._shape_cache:
            shape = self.base.Shape
            top, bottom = shape.BoundBox.ZMax, shape.BoundBox.ZMin
            entries = []
            for index, face in enumerate(shape.Faces):
                name = "Face%d" % (index + 1)
                kind = type(face.Surface).__name__
                normal_z = None
                try:
                    u0, u1, v0, v1 = face.ParameterRange
                    normal_z = float(face.normalAt((u0 + u1) / 2.0, (v0 + v1) / 2.0).z)
                except Exception:
                    pass
                box = face.BoundBox
                entries.append({"name": name, "kind": kind, "normal_z": normal_z, "zmin": box.ZMin, "zmax": box.ZMax,
                                "xlen": box.XLength, "ylen": box.YLength, "area": float(face.Area),
                                "radius": float(face.Surface.Radius) if kind == "Cylinder" and abs(abs(face.Surface.Axis.z) - 1.0) < 1e-6 else None,
                                "is_top": abs(box.ZMax - top) < 1e-6 and abs(box.ZMin - top) < 1e-6,
                                "is_floor": kind == "Plane" and normal_z is not None and abs(normal_z - 1.0) < 1e-6
                                and box.ZMax < top - 1e-6 and box.ZMin > bottom + 1e-6})
            self._shape_cache["faces"] = entries
        return self._shape_cache["faces"]

    def _top_edges(self):
        shape = self.base.Shape
        top = shape.BoundBox.ZMax
        return ["Edge%d" % (index + 1) for index, edge in enumerate(shape.Edges)
                if abs(edge.BoundBox.ZMin - top) < 1e-6 and abs(edge.BoundBox.ZMax - top) < 1e-6]

    def select_targets(self, entry):
        """(sous-éléments FaceN/EdgeN ou None, zones [{name, width}], note) selon le type et ``selection``."""
        kind, selection = entry["type"], entry["selection"]
        faces = self._face_entries()
        if selection.get("mode") == "faces":
            names = {face["name"] for face in faces}
            missing = [name for name in selection["faces"] if name not in names]
            if missing:
                raise CamEngineError("Opération « %s » : face %s absente du modèle calculé." % (entry["name"], missing[0]))
            chosen = [face for face in faces if face["name"] in selection["faces"]]
            return list(selection["faces"]), [{"name": face["name"], "width": min(face["xlen"], face["ylen"])} for face in chosen if face["is_floor"]], ""
        if kind == "facing" or kind == "vcarve":
            return [face["name"] for face in faces if face["is_top"]] or None, [], ""
        if kind in ("pocket", "adaptive"):
            floors = [face for face in faces if face["is_floor"]]
            return [face["name"] for face in floors] or None, [{"name": face["name"], "width": min(face["xlen"], face["ylen"])} for face in floors], ""
        if kind == "slot":
            floors = [face for face in faces if face["is_floor"] and min(face["xlen"], face["ylen"]) < 0.5 * max(face["xlen"], face["ylen"])]
            if not floors:
                return None, [], "aucune rainure rectangulaire détectée (fond plan allongé)"
            note = "" if len(floors) == 1 else "%d rainures détectées : seule la première est traitée (une opération Rainure par rainure)" % len(floors)
            return [floors[0]["name"]], [{"name": floors[0]["name"], "width": min(floors[0]["xlen"], floors[0]["ylen"])}], note
        if kind in ("drilling", "helix", "thread_milling"):
            low, high = selection.get("min_diameter", 0.0), selection.get("max_diameter", 1e9)
            if kind == "drilling":
                import Path.Base.Drillable as Drillable
                candidates = [sub for _obj, sub in Drillable.getDrillableTargets(self.base)]
                by_name = {face["name"]: face for face in faces}
                targets = [sub for sub in candidates if by_name.get(sub, {}).get("radius") is not None
                           and low - 1e-6 <= 2.0 * by_name[sub]["radius"] <= high + 1e-6]
            else:
                targets = [face["name"] for face in faces if face["radius"] is not None and low - 1e-6 <= 2.0 * face["radius"] <= high + 1e-6
                           and face["normal_z"] is not None and self._is_concave(face["name"])]
            return targets or None, [], "" if targets else "aucun trou cylindrique entre Ø %.2f et Ø %.2f mm" % (low, high)
        if kind in ("engrave", "deburr"):
            edges = self._top_edges()
            return edges or None, [], "" if edges else "aucune arête sur la face du dessus"
        return None, [], ""

    def _is_concave(self, face_name):
        face = self.base.Shape.getElement(face_name)
        try:
            u0, u1, v0, v1 = face.ParameterRange
            normal = face.normalAt((u0 + u1) / 2.0, (v0 + v1) / 2.0)
            point = face.valueAt((u0 + u1) / 2.0, (v0 + v1) / 2.0)
            center = face.Surface.Center
            return (point.x - center.x) * normal.x + (point.y - center.y) * normal.y < 0
        except Exception:
            return False

    # -- opérations ----------------------------------------------------------------------------
    def add_operation(self, entry, controller, properties, targets):
        kind = entry["type"]
        module = importlib.import_module(OPERATION_MODULES[kind])
        self._chooser.current = controller
        op = module.Create(OPERATION_MODULES[kind].rsplit(".", 1)[-1], parentJob=self.job)
        op.Label = comment_text(entry["name"])
        op.ToolController = controller
        if targets:
            op.Base = [(self.base, list(targets))]
        for name, value in properties:
            self._set(op, name, value)
        return op

    def set_depths(self, op, start, final, step_down):
        self._set(op, "StartDepth", start)
        self._set(op, "FinalDepth", final)
        self._set(op, "StepDown", step_down)

    def geometry_final(self, op):
        try:
            return float(op.FinalDepth.Value) if "FinalDepth" in op.PropertiesList else None
        except Exception:
            return None

    def apply_dressup(self, op, dressup):
        """Dressup FreeCAD sur ``op`` ; la base est retirée du groupe (sinon le post la couperait deux fois)."""
        module_name, properties, count = ops.dressup_properties(dressup)
        module = importlib.import_module(module_name)
        wrapper = module.Create(op)
        if wrapper is None:
            raise CamEngineError("FreeCAD a refusé le dressup « %s »." % dressup["type"])
        for name, value in properties:
            self._set(wrapper, name, value)
        if count:
            wrapper.Proxy.generateTags(wrapper, int(count))
            if "Positions" in wrapper.PropertiesList and not list(wrapper.Positions):
                raise CamEngineError("aucun pont n'a pu être placé sur ce parcours")
        group = list(self.job.Operations.Group)
        if op in group:
            group.remove(op)
            self.job.Operations.Group = group
        if wrapper not in self.job.Operations.Group:
            self.job.Operations.Group = list(self.job.Operations.Group) + [wrapper]
        return wrapper

    def remove_operation(self, op):
        group = list(self.job.Operations.Group)
        if op in group:
            group.remove(op)
            self.job.Operations.Group = group
        try:
            self.doc.removeObject(op.Name)
        except Exception:
            pass

    def add_custom(self, label, lines, controller):
        import Path.Op.Custom as Custom
        self._chooser.current = controller
        op = Custom.Create("Custom", parentJob=self.job)
        op.Label = comment_text(label)
        if controller is not None:
            op.ToolController = controller
        if "Source" in op.PropertiesList:
            op.Source = "Text"
        op.Gcode = list(lines)
        return op

    def recompute(self):
        self.doc.recompute()

    def op_state(self, op):
        state = list(op.State)
        return {"valid": "Invalid" not in state, "state": state}

    def path_commands(self, op):
        try:
            return [(command.Name, dict(command.Parameters)) for command in op.Path.Commands]
        except Exception:
            return []

    def post(self, name, arguments, operations=None):
        """Export du Job ; ``operations`` : sous-ensemble ordonné d'objets (un programme par outil), tout le Job sinon."""
        from Path.Post.Processor import PostProcessorFactory
        processor = PostProcessorFactory.get_post_processor(self.job, name)
        if processor is None:
            raise CamEngineError("Post-processeur « %s » introuvable dans cette installation de FreeCAD." % name)
        self.job.PostProcessorArgs = arguments
        saved = list(self.job.Operations.Group)
        try:
            if operations is not None:
                self.job.Operations.Group = list(operations)
            sections = processor.export() or []
        except SystemExit as error:
            raise CamEngineError("Le post-processeur « %s » a refusé ses arguments (code %s)." % (name, error.code))
        finally:
            self.job.Operations.Group = saved
        return "".join(code or "" for _name, code in sections)

    def post_known_arguments(self, name):
        from Path.Post.Processor import PostProcessorFactory
        processor = PostProcessorFactory.get_post_processor(self.job, name)
        module = getattr(processor, "script_module", None)
        if module is not None:
            return [argument["name"] for argument in post_arguments_of(module)]
        parser = getattr(processor, "parser", None)
        actions = getattr(parser, "_actions", None) or []
        return [option for action in actions for option in action.option_strings if option.startswith("--")]

    def save(self, path):
        self.doc.saveAs(path)

    def close(self):
        if self.doc is not None:
            try:
                self.App.closeDocument(self.doc.Name)
            except Exception:
                pass


# ---------------------------------------------------------------------------------------------
# Enchaînement du calcul
# ---------------------------------------------------------------------------------------------
def _write_json(workdir, name, payload):
    with open(os.path.join(workdir, name), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, allow_nan=False)


def _progress(workdir, payload):
    """Progression RÉELLE : opérations terminées / total, opération courante ; l'avancement interne est indéterminé."""
    try:
        payload = dict(payload, updated_at=time.time())
        tmp = os.path.join(workdir, PROGRESS_FILE + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
        os.replace(tmp, os.path.join(workdir, PROGRESS_FILE))
    except OSError:
        pass


def _check_cancel(workdir):
    if os.path.exists(os.path.join(workdir, CANCEL_FILE)):
        raise CamCancelled("Calcul CNC annulé par l'utilisateur.")


def place_model(project, source, backend):
    """Import, échelle, orientation, brut, origine : (shape placé, info, stock (min, max), cylindre|None, issues, stock_model)."""
    factor = 25.4 if project["units"] == "inch" else 1.0
    source_scale = (25.4 if project["source"]["units"] == "inch" else 1.0) * float(project["source"].get("scale", 1) or 1)
    shape = backend.import_model(source, project["source"]["kind"])
    shape = backend.transform(shape, source_scale, orientation_steps(project["orientation"]))
    info = backend.model_info(shape)
    issues = model_checks(info)
    stock_model, _ = stock_box(info["bounds"], project["stock"], factor)
    low, high = info["bounds"]
    if project["stock"]["mode"] == "explicit" and any(low[i] < stock_model[0][i] - 1e-6 or high[i] > stock_model[1][i] + 1e-6 for i in range(3)):
        raise CamEngineError("La pièce ne tient pas dans le brut saisi : agrandis le brut ou choisis « depuis le modèle ».")
    origin = origin_point(stock_model, project["origin"], factor)
    shape = backend.translate(shape, origin)
    stock = shift_box(stock_model, origin)
    info = backend.model_info(shape)
    cylinder = None
    if project["stock"].get("shape") == "cylinder":
        cylinder = {"radius": (stock[1][0] - stock[0][0]) / 2.0, "center": [(stock[0][0] + stock[1][0]) / 2.0, (stock[0][1] + stock[1][1]) / 2.0],
                    "height": stock[1][2] - stock[0][2]}
    machine = project["machine"]
    size = [stock[1][i] - stock[0][i] for i in range(3)]
    for index, axis in enumerate("xyz"):
        if size[index] > machine["travel"][axis] + 1e-6:
            raise CamEngineError("Brut plus grand que la course %s de la machine (%.1f mm pour %g mm)."
                                 % (axis.upper(), size[index], machine["travel"][axis]))
    return shape, info, stock, cylinder, issues


def preview(project, source, workdir, backend, freecad_version):
    """Aperçu AVANT calcul : modèle orienté et placé (STL borné), brut, origine ; aucun Job FreeCAD."""
    project = ops.normalize_project(project)
    shape, info, stock, cylinder, issues = place_model(project, source, backend)
    stem = project["output"]["stem"]
    stl_name = stem + ".preview.stl"
    mesh_info = backend.tessellate(shape, os.path.join(workdir, stl_name))
    size = [stock[1][i] - stock[0][i] for i in range(3)]
    payload = {"ok": True, "op": "preview", "engine_version": ops.ENGINE_VERSION, "freecad_version": freecad_version, "units": "mm",
               "model": {"bounds": {"min": info["bounds"][0], "max": info["bounds"][1]}, "is_solid": info["is_solid"], "is_mesh": info["is_mesh"],
                         "facets": info["facets"], "units_assumed": project["source"]["units"], "scale": project["source"].get("scale", 1)},
               "stock": {"size": {"x": size[0], "y": size[1], "z": size[2]}, "bounds": {"min": stock[0], "max": stock[1]},
                         "shape": project["stock"].get("shape", "box"), "cylinder": cylinder, "origin_machine": [0.0, 0.0, 0.0]},
               "fixtures": project["fixtures"], "mesh_preview": dict(mesh_info, name=stl_name) if mesh_info else None,
               "manufacturability": issues}
    _write_json(workdir, "cam_preview.json", payload)
    artifacts = ["cam_preview.json"] + ([stl_name] if mesh_info else [])
    return {"op": "preview", "artifacts": artifacts, "mesh_preview": payload["mesh_preview"], "freecad_version": freecad_version}


def compute(project, source, workdir, backend, cam_gcode_check, freecad_version):
    project = ops.normalize_project(project)
    factor = 25.4 if project["units"] == "inch" else 1.0
    machine = project["machine"]
    shape, info, stock, cylinder, issues = place_model(project, source, backend)
    size = [stock[1][i] - stock[0][i] for i in range(3)]
    heights = {key: project["heights"][key] * factor for key in ("clearance", "safe", "retract")}
    stock_top, model_top = stock[1][2], info["bounds"][1][2]
    clearance = ops.clearance_issue(size[2], heights["clearance"], float(machine["travel"]["z"]))
    if clearance:
        raise CamEngineError(clearance[2])
    tools, entries = ops.plan(project)
    backend.create_job(shape, stock, project["origin"]["wcs"], cylinder=cylinder)
    controllers = {}
    for entry in entries:   # tous les contrôleurs d'abord : une opération FreeCAD en exige un
        if "controller" in entry and entry["controller"] not in controllers:
            tool = tools[entry["tool"]]
            controllers[entry["controller"]] = backend.add_controller(
                tool, (tool["number"], entry["feed_xy"] or tool["feed_xy"], entry["feed_plunge"] or tool["feed_plunge"],
                       entry["spindle_rpm"] or tool["spindle_rpm"], entry["spindle_dir"]),
                "T%d %s" % (tool["number"], comment_text(tool["name"])))
    policy = project["sequence"]["tool_change_policy"]
    manual = machine["tool_change"] == "manual"
    position = machine.get("tool_change_position")
    change_position = position if isinstance(position, dict) and position.get("verified") is True else None
    steps = ops.sequence_steps(entries, tools, policy, manual, change_position)
    machining_total = sum(1 for step in steps if step[0] == "op")
    reports, previews, program_objects = {}, {}, {}
    order = []
    progress = {"total": machining_total, "done": 0, "current": None, "operations": {step[1]["id"]: "pending" for step in steps if step[0] == "op"},
                "within_operation": "indeterminate"}
    _progress(workdir, progress)
    rapid_speed = float(machine.get("max_rapid") or machine["max_feed"])
    accel = machine.get("max_accel")
    for step in steps:
        _check_cancel(workdir)
        program = step[-1]
        if step[0] == "custom":
            _kind, label, lines, key, entry, _program = step
            obj = backend.add_custom(label, lines, controllers[key] if key else None)
            program_objects.setdefault(program, []).append(obj)
            if entry is not None:
                reports[entry["id"]] = ops.result_operation(entry, "computed", segments=0, estimated_s=0.0, machining=False)
                order.append(entry["id"])
            continue
        entry = step[1]
        tool = tools[entry["tool"]]
        order.append(entry["id"])
        progress.update(current=entry["id"])
        progress["operations"][entry["id"]] = "computing"
        _progress(workdir, progress)
        report = _compute_operation(entry, tool, controllers[entry["controller"]], heights, stock_top, model_top, backend, rapid_speed, accel)
        reports[entry["id"]] = report["result"]
        if report.get("preview"):
            previews[entry["id"]] = report["preview"]
        if report["result"]["state"] == "computed":
            program_objects.setdefault(program, []).append(report["object"])
        progress["done"] += 1
        progress["operations"][entry["id"]] = report["result"]["state"]
        _progress(workdir, progress)
    progress.update(current=None)
    _progress(workdir, progress)
    if not any(item["state"] == "computed" and item.get("machining", True) and item["type"] in ops.MACHINING_TYPES + ("custom",)
               for item in reports.values()):
        causes = "; ".join("%s : %s" % (item["name"], item.get("error") or item["state"]) for item in reports.values() if item.get("machining"))
        raise CamEngineError("Aucune opération n'a produit de parcours (%s)." % (causes or "séquence vide"))
    post_name = machine["post_processor"]
    arguments = ops.post_arguments(post_name, backend.post_known_arguments(post_name), project, policy)
    runs = ops.program_runs(steps)
    runs = [run for run in runs if program_objects.get(run["index"])]
    stem = project["output"]["stem"]
    names = ops.program_file_names(runs, stem, machine["output_extension"])
    files, validations, post_issues, all_segments, time_total = [], [], [], [], 0.0
    stock_bounds = {"min": stock[0], "max": stock[1]}
    check_project = dict(project, machine=machine)
    grbl = str(machine["controller"]).startswith("grbl")
    for run, name in zip(runs, names):
        _check_cancel(workdir)
        gcode = backend.post(post_name, arguments, program_objects[run["index"]])
        if not gcode or not gcode.strip():
            raise CamEngineError("Le post-processeur n'a produit aucun G-code pour le programme « %s »." % name)
        data = gcode.encode("utf-8")
        if len(data) > GCODE_MAX_BYTES:
            raise CamEngineError("G-code trop volumineux (plus de 64 Mo).")
        with open(os.path.join(workdir, name), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(gcode)
        validation = cam_gcode_check.check_gcode(gcode, check_project, machine, stock=stock_bounds)
        checks = ops.grbl_program_checks(gcode, machine, policy) if grbl else []
        validations.append({"file": name, "ok": validation["ok"] and not checks, "errors": validation["errors"], "warnings": validation["warnings"],
                            "post_checks": checks, "stats": validation["stats"]})
        post_issues.extend(dict(item, file=name) for item in checks)
        simulation = cam_gcode_check.simulation_segments(gcode, SIMULATION_LIMIT)
        all_segments.extend(dict(segment, file=name) for segment in simulation["segments"])
        time_total += float(validation["stats"].get("time_estimate_s") or 0.0)
        files.append({"name": name, "sha256": ops.file_sha256(data), "bytes": len(data), "lines": len(gcode.splitlines()),
                      "tool_numbers": list(run["tool_numbers"]), "operations": list(run["operations"]), "index": len(files)})
    report_list = [reports[identifier] for identifier in order if identifier in reports]
    for item in report_list:
        item["gcode_file"] = next((file["name"] for file in files if item["id"] in file["operations"]), None)
    issues += manufacturability(project, tools, stock, all_segments, backend.faces(shape), report_list)
    if info.get("solids", 1) > 1:
        issues.append({"code": "closed_cavity", "severity": "warning", "operation": "",
                       "message": "Le modèle contient plusieurs solides (cavité fermée ou pièces séparées) : vérifie le résultat."})
    manual_steps = ops.manual_steps(entries, tools, runs, names, change_position)
    not_simulated = ops.not_simulated(project, tools)
    tool_shapes = {}
    for entry in entries:
        if entry.get("engine_shape") and entry.get("tool") is not None:
            label = "%s : %s" % (entry["type"], entry["engine_shape"]["shape"])
            tool_shapes.setdefault(str(entry["tool"]), [])
            if label not in tool_shapes[str(entry["tool"])]:
                tool_shapes[str(entry["tool"])].append(label)
    validation_all = {"ok": all(item["ok"] for item in validations), "errors": [], "warnings": []}
    for item in validations:
        validation_all["errors"].extend(dict(entry, file=item["file"]) for entry in item["errors"])
        validation_all["warnings"].extend(dict(entry, file=item["file"]) for entry in item["warnings"])
    for item in post_issues:
        validation_all["errors"].append({"code": item["code"], "line": item.get("line", 0), "message": item["message"], "count": 1, "file": item["file"]})
    validation_all["ok"] = validation_all["ok"] and not post_issues
    bounds = _merge_bounds([item["stats"]["bounds"] for item in validations])
    length = {key: round(sum(float(item["stats"]["length_mm"][key]) for item in validations), 3) for key in ("rapid", "cut", "total")}
    summary = {"bounds": bounds, "length_mm": length, "time_estimate_s": round(time_total, 1), "time_estimate_is_estimate": True,
               "tools_used": sorted({entry["tool"] for entry in entries if entry.get("tool") and entry["type"] != "tool_change"}),
               "operations": [{key: item.get(key) for key in ("id", "type", "name", "state", "ok", "segments", "estimated_s", "error", "engine_shape", "gcode_file")}
                              for item in report_list],
               "tool_shapes": tool_shapes, "not_simulated": not_simulated, "programs": len(files)}
    fcstd_name, json_name, html_name, stl_name = stem + ".FCStd", stem + ".cam.json", stem + ".setup.html", stem + ".preview.stl"
    all_objects = [obj for index in sorted(program_objects) for obj in program_objects[index]]
    backend.save(os.path.join(workdir, fcstd_name))
    mesh_info = backend.tessellate(shape, os.path.join(workdir, stl_name))
    with open(os.path.join(workdir, json_name), "w", encoding="utf-8") as handle:
        json.dump({"project": project, "freecad_version": freecad_version, "post_processor": post_name, "post_arguments": arguments,
                   "engine_version": ops.ENGINE_VERSION, "contract_version": ops.CONTRACT_VERSION}, handle, ensure_ascii=False, indent=1, allow_nan=False)
    with open(os.path.join(workdir, html_name), "w", encoding="utf-8") as handle:
        handle.write(setup_sheet_html(project, summary, stock, issues, validation_all, freecad_version, manual_steps, files))
    previews = ops.preview_budget(previews)
    cam_result = {
        "ok": True, "engine_version": ops.ENGINE_VERSION, "contract_version": ops.CONTRACT_VERSION, "freecad_version": freecad_version,
        "post_processor": post_name, "post_arguments": arguments, "tool_change_policy": policy,
        "gcode_file": files[0]["name"], "gcode_lines": files[0]["lines"], "gcode_bytes": files[0]["bytes"], "gcode_sha256": files[0]["sha256"],
        "gcode_files": files, "summary": summary, "operations": report_list,
        "model": {"bounds": {"min": info["bounds"][0], "max": info["bounds"][1]}, "is_solid": info["is_solid"],
                  "is_mesh": info["is_mesh"], "facets": info["facets"], "units_assumed": project["source"]["units"],
                  "scale": project["source"].get("scale", 1)},
        "stock": {"size": {"x": size[0], "y": size[1], "z": size[2]}, "origin_machine": [0.0, 0.0, 0.0],
                  "bounds": {"min": stock[0], "max": stock[1]}, "shape": project["stock"].get("shape", "box"), "cylinder": cylinder},
        "manufacturability": issues,
        "validation": validation_all, "validation_files": validations,
        "simulation": {"units": "mm", "segments": all_segments[:SIMULATION_LIMIT], "truncated": len(all_segments) > SIMULATION_LIMIT, "limit": SIMULATION_LIMIT},
        "preview": {"units": "mm", "operations": previews, "mesh_preview": dict(mesh_info, name=stl_name) if mesh_info else None,
                    "stock": {"bounds": {"min": stock[0], "max": stock[1]}, "cylinder": cylinder}, "fixtures": project["fixtures"]},
        "manual_steps": manual_steps, "not_simulated": not_simulated,
        "tool_controllers": {str(key[0]): backend.controller_notes(controller) for key, controller in controllers.items()}
                            if hasattr(backend, "controller_notes") else {},
    }
    _write_json(workdir, "cam_result.json", cam_result)
    artifacts = [file["name"] for file in files] + [fcstd_name, json_name, html_name] + ([stl_name] if mesh_info else []) + ["cam_result.json"]
    return {"op": "compute", "gcode_file": files[0]["name"], "gcode_files": [file["name"] for file in files], "artifacts": artifacts,
            "validation_ok": validation_all["ok"], "errors": len(validation_all["errors"]), "warnings": len(validation_all["warnings"]),
            "manufacturability": len(issues), "freecad_version": freecad_version, "engine_version": ops.ENGINE_VERSION,
            "operations": {item["id"]: item["state"] for item in report_list}}


def _compute_operation(entry, tool, controller, heights, stock_top, model_top, backend, rapid_speed, accel):
    """Une opération d'usinage : propriétés, sélection, profondeurs, dressups, recalcul, analyse ; jamais d'exception FreeCAD propagée."""
    kind = entry["type"]
    issues, warnings = [], []
    started = time.time()
    try:
        properties = ops.operation_properties(entry, tool, heights, stock_top)
        targets, zones, note = backend.select_targets(entry)
        if note:
            warnings.append(("selection_note", "warning", "Opération « %s » : %s." % (entry["name"], note)))
        issues += ops.zone_issues(entry, tool, zones)
        op = backend.add_operation(entry, controller, properties, targets)
        backend.recompute()
        start, final, warning = depth_window(kind, stock_top, model_top, backend.geometry_final(op), entry)
        if kind not in ("deburr",):
            backend.set_depths(op, start, final, entry["step_down"])
        if warning:
            warnings.append((warning[0], "warning", warning[1]))
        reach = ops.tool_reach_issues(entry, tool, stock_top, final)
        issues += reach
        if any(severity == "error" for _code, severity, _message in reach):
            backend.remove_operation(op)
            return {"result": ops.result_operation(entry, "error", error=next(message for _c, severity, message in reach if severity == "error"),
                                                   issues=issues + warnings, machining=True), "preview": None, "object": None}
        final_op = op
        for dressup in entry.get("dressups", []):
            try:
                final_op = backend.apply_dressup(final_op, dressup)
            except Exception as error:
                warnings.append(ops.dressup_not_applied_warning(entry, dressup, str(error) or type(error).__name__))
        backend.recompute()
        state = backend.op_state(final_op)
        commands = backend.path_commands(final_op)
        analysis = ops.path_analysis(commands, entry["feed_xy"] or tool["feed_xy"], entry["feed_plunge"] or tool["feed_plunge"], rapid_speed, accel)
    except CamCancelled:
        raise
    except Exception as error:
        message = str(error) or type(error).__name__
        return {"result": ops.result_operation(entry, "error", error=("%s : %s" % (type(error).__name__, message))[:240], issues=issues + warnings,
                                               machining=True, compute_s=round(time.time() - started, 2)), "preview": None, "object": None}
    if not state["valid"]:
        backend.remove_operation(final_op)
        return {"result": ops.result_operation(entry, "error", error="FreeCAD a invalidé l'opération (géométrie non reconnue ou outil inadapté : %s)."
                                               % ", ".join(state["state"]), issues=issues + warnings, machining=True,
                                               compute_s=round(time.time() - started, 2)), "preview": None, "object": None}
    if analysis["segments"] == 0:
        backend.remove_operation(final_op)
        cause = note or "aucune géométrie détectée pour cette opération"
        return {"result": ops.result_operation(entry, "empty", error=cause, issues=issues + warnings, machining=True, segments=0,
                                               compute_s=round(time.time() - started, 2)), "preview": None, "object": None}
    preview = {"polylines": analysis["polylines"], "bounds": analysis["bounds"], "truncated": analysis["truncated"], "tool": entry["tool"]}
    result_entry = ops.result_operation(entry, "computed", segments=analysis["segments"], estimated_s=analysis["estimated_s"],
                                        length_mm=analysis["length_mm"], issues=issues + warnings, machining=True,
                                        dressups=[item["type"] for item in entry.get("dressups", [])],
                                        compute_s=round(time.time() - started, 2))
    return {"result": result_entry, "preview": preview, "object": final_op}


def _merge_bounds(items):
    items = [item for item in items if item]
    if not items:
        return None
    return {"min": [min(item["min"][axis] for item in items) for axis in range(3)],
            "max": [max(item["max"][axis] for item in items) for axis in range(3)]}


def main():
    args = list(sys.argv[1:])
    if args and args[0].lower().endswith(".py"):
        args = args[1:]
    if len(args) < 2:
        raise CamEngineError("Arguments manquants : requête JSON et dossier du job.")
    request_path, workdir = os.path.abspath(args[0]), os.path.abspath(args[1])
    cam_payload, cam_gcode_check = load_contract_modules()
    op, project, source = read_request(request_path, workdir, cam_payload)
    import FreeCAD as App
    version = ".".join(str(part) for part in App.Version()[:3])
    if op == "capabilities":
        found = capabilities(App)
        _write_json(workdir, "cam_capabilities.json", found)
        result(True, op="capabilities", freecad_version=version, artifacts=["cam_capabilities.json"],
               cam_available=found["cam_available"], post_processors=len(found["post_processors"]), engine_version=ops.ENGINE_VERSION)
        return
    if not _importable("Path.Main.Job"):
        raise CamEngineError("FreeCAD CAM n'est pas disponible sur ce Worker (module Path absent).")
    required = {name for entry in project["operations"] if entry["enabled"] for name in ops.OPERATION_REQUIREMENTS.get(entry["type"], ())}
    if "ocl" in required and importlib.util.find_spec("opencamlib") is None and importlib.util.find_spec("ocl") is None:
        raise CamEngineError("OpenCAMLib absent sur ce Worker : les opérations Surface et Waterline ne peuvent pas être calculées "
                             "(Installations → Convertisseur 3D → OpenCAMLib).")
    if "area" in required and importlib.util.find_spec("area") is None:
        raise CamEngineError("Module « area » (libarea) absent de ce FreeCAD : Adaptive et Vcarve indisponibles.")
    backend = FreeCadCam()
    try:
        details = preview(project, source, workdir, backend, version) if op == "preview" else \
            compute(project, source, workdir, backend, cam_gcode_check, version)
    finally:
        backend.close()
    result(True, **details)


def launched_by_freecadcmd():
    """Vrai quand FreeCADCmd charge ce fichier comme un MODULE (``import freecad_cam``) au lieu de l'exécuter.

    Selon sa version, FreeCADCmd exécute un script ``.py`` de la ligne de commande sous ``__main__`` ou l'importe
    sous son propre nom : le garde ``__name__ == "__main__"`` seul laissait alors FreeCAD se terminer en une seconde
    sans rien calculer ni écrire (aucune ligne de résultat). Jamais vrai dans un test qui importe ce module.
    """
    return __name__ != "__main__" and "FreeCAD" in sys.modules and any(
        os.path.basename(str(argument)).lower() == "freecad_cam.py" for argument in sys.argv[1:])


if __name__ == "__main__" or launched_by_freecadcmd():
    try:
        main()
    except CamCancelled as exc:
        result(False, error=str(exc), cancelled=True)
        if __name__ == "__main__":
            sys.exit(3)
    except Exception as exc:
        traceback.print_exc()
        message = str(exc) if isinstance(exc, (CamEngineError, ValueError)) else "Erreur du moteur CAM : %s" % type(exc).__name__
        result(False, error=message)
        if __name__ == "__main__":
            sys.exit(2)  # importé par FreeCADCmd, un SystemExit relancerait le fichier : la ligne de résultat suffit
