# -*- coding: utf-8 -*-
"""
Alpine Forge 3D — moteur FreeCAD CAM (« Préparer pour une CNC »), première tranche.

  FreeCADCmd.exe freecad_cam.py cam_request.json WORKDIR

La requête arrive comme FICHIER JSON ({"op": "capabilities"} ou {"op": "compute", "project": …,
"source": "<nom du fichier dans WORKDIR>"}), jamais comme code : ce moteur ne contient ni eval ni
exec piloté par la requête et REVALIDE le projet par worker_agent/cam_payload.py (même fonction que
le site et le runner). Seuls les modules CAM installés avec FreeCAD sont importés.

Organisation :
- fonctions de calcul PURES (orientation, brut, origine, profondeurs, options du post-processeur,
  fabricabilité, fiche de réglage) testables sans FreeCAD ;
- ``FreeCadCam`` : seul endroit qui parle à FreeCAD (Job, brut, outils, opérations, post) ;
- ``compute`` / ``capabilities`` : l'enchaînement, sur un adaptateur injectable.

Résultat sur stdout : une ligne ``RAPHAL_FREECAD_RESULT=<json>`` comme les deux autres moteurs.
Aucun G-code n'est envoyé à une machine : le moteur écrit des fichiers dans WORKDIR, rien d'autre.
"""
import html
import importlib
import importlib.util
import json
import math
import os
import shlex
import sys
import traceback

RESULT_PREFIX = "RAPHAL_FREECAD_RESULT="
HERE = os.path.dirname(os.path.abspath(__file__))

# Identifiants Alpine des opérations ↔ modules FreeCAD CAM (lus dans Mod/CAM/Path/Op de FreeCAD 1.1).
OPERATION_MODULES = {
    "facing": "Path.Op.MillFace", "pocket": "Path.Op.Pocket", "profile": "Path.Op.Profile",
    "drilling": "Path.Op.Drilling", "adaptive": "Path.Op.Adaptive", "engrave": "Path.Op.Engrave",
    "vcarve": "Path.Op.Vcarve", "helix": "Path.Op.Helix", "slot": "Path.Op.Slot", "surface": "Path.Op.Surface",
    "waterline": "Path.Op.Waterline", "deburr": "Path.Op.Deburr", "thread_milling": "Path.Op.ThreadMilling",
}
# Formes d'outils FreeCAD (Mod/CAM/Tools/Shape/<forme>.fcstd) ↔ familles Alpine.
SHAPE_FAMILIES = {"endmill": "endmill", "drill": "drill", "ballend": "ballend", "v-bit": "vbit", "chamfer": "chamfer",
                  "bullnose": "bullnose", "thread-mill": "threadmill", "radius": "radius", "dovetail": "dovetail",
                  "slittingsaw": "slittingsaw", "taperedballnose": "taperedballnose", "reamer": "reamer", "tap": "tap",
                  "probe": "probe"}
FAMILY_SHAPES = {"endmill": "endmill.fcstd", "drill": "drill.fcstd"}
MAX_FACETS = 2000000
SCALE_MAX_MM = 3000.0
SCALE_MIN_MM = 0.5
SIMULATION_LIMIT = 20000
GCODE_MAX_BYTES = 64 * 1024 * 1024


def result(ok, **kwargs):
    print(RESULT_PREFIX + json.dumps({"ok": bool(ok), **kwargs}, ensure_ascii=False, allow_nan=False), flush=True)


class CamEngineError(RuntimeError):
    """Refus ou échec lisible par l'utilisateur."""


def load_contract_modules():
    """cam_payload et cam_gcode_check : paquet ``worker_agent`` dans le dépôt, modules à plat sur le Worker."""
    try:
        from worker_agent import cam_gcode_check, cam_payload  # dépôt (tests, site)
    except ImportError:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import cam_gcode_check  # Worker installé : fichiers à plat à côté de ce moteur
        import cam_payload
    return cam_payload, cam_gcode_check


# ---------------------------------------------------------------------------------------------
# Requête
# ---------------------------------------------------------------------------------------------
def read_request(path, workdir, cam_payload):
    """Lit et REVALIDE la requête ; rend (op, projet normalisé ou None, chemin de la source ou None)."""
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
    if op != "compute":
        raise CamEngineError("Opération CAM inconnue.")
    project = cam_payload.validate_cam_project(request.get("project"))
    source_name = request.get("source")
    if not isinstance(source_name, str) or source_name != project["source"]["file"]:
        raise CamEngineError("Le fichier source joint ne correspond pas au projet.")
    source = os.path.join(workdir, source_name)
    if os.path.dirname(os.path.abspath(source)) != os.path.abspath(workdir) or not os.path.isfile(source):
        raise CamEngineError("Fichier source introuvable dans le dossier du job.")
    return "compute", project, source


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
    ``explicit`` : boîte saisie, pièce centrée en X/Y et posée sous la marge du dessus.
    """
    low, high = [float(v) for v in model_bounds[0]], [float(v) for v in model_bounds[1]]
    margins = {key: float(value) * factor for key, value in stock["margins"].items()}
    if stock["mode"] == "from_model":
        return ([low[0] - margins["x"], low[1] - margins["y"], low[2] - margins["bottom"]],
                [high[0] + margins["x"], high[1] + margins["y"], high[2] + margins["top"]]), [0.0, 0.0, 0.0]
    size = [float(stock["size"][axis]) * factor for axis in ("x", "y", "z")]
    center = [(low[0] + high[0]) / 2.0, (low[1] + high[1]) / 2.0]
    top = high[2] + margins["top"]
    box = ([center[0] - size[0] / 2.0, center[1] - size[1] / 2.0, top - size[2]],
           [center[0] + size[0] / 2.0, center[1] + size[1] / 2.0, top])
    return box, [0.0, 0.0, 0.0]


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
    # Le décalage déplace l'origine sur le brut : la pièce est translatée de l'opposé.
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
                                    "moins fiable. Préfère un STEP quand il existe.", "operation": ""})
    if not info.get("is_solid"):
        warnings.append({"code": "not_a_solid", "severity": "warning",
                         "message": "Le modèle n'est pas un solide fermé : les profondeurs et les détections peuvent être "
                                    "fausses.", "operation": ""})
    return warnings


def tool_in_mm(tool, factor):
    return {
        "number": tool["number"], "name": tool["name"], "family": tool["family"],
        "diameter": tool["diameter"] * factor, "flute_length": tool["flute_length"] * factor,
        "length": tool["length"] * factor, "shank_diameter": tool["shank_diameter"] * factor,
        "flutes": tool["flutes"], "spindle_rpm": tool["spindle_rpm"], "max_rpm": tool["max_rpm"],
        "feed_xy": (tool["feed_xy"] or 0.0) * factor, "feed_plunge": (tool["feed_plunge"] or 0.0) * factor,
        "feed_ramp": (tool["feed_ramp"] or 0.0) * factor if tool.get("feed_ramp") else None,
        "coolant": tool["coolant"], "tip_angle": tool["tip_angle"],
    }


def controller_key(operation, tool):
    """Un contrôleur d'outil FreeCAD par combinaison (outil, avances, broche, sens)."""
    return (tool["number"], round(operation.get("feed_xy") or tool["feed_xy"], 4),
            round(operation.get("feed_plunge") or tool["feed_plunge"], 4),
            round(operation.get("spindle_rpm") or tool["spindle_rpm"], 1), operation.get("spindle_dir", "cw"))


def operations_plan(project):
    """Séquence en mm, dans l'ordre du projet : opérations d'usinage activées, pauses, consignes, changements d'outil."""
    factor = 25.4 if project["units"] == "inch" else 1.0
    tools = {tool["number"]: tool_in_mm(tool, factor) for tool in project["tools"]}
    plan = []
    for operation in project["operations"]:
        if not operation["enabled"]:
            continue
        entry = {"id": operation["id"], "type": operation["type"], "name": operation["name"],
                 "note": operation.get("operator_note") or ""}
        if operation["type"] in ("facing", "pocket", "profile", "drilling"):
            tool = tools[operation["tool"]]
            depth = operation["depth"]
            entry.update({
                "tool": tool["number"], "final": depth["final"] * factor, "step_down": depth["step_down"] * factor,
                "start": None if depth["start"] is None else depth["start"] * factor,
                "stepover_pct": operation["stepover_pct"], "direction": operation["direction"],
                "entry": operation["entry"], "coolant": operation["coolant"], "pause_before": operation["pause_before"],
                "feed_xy": (operation["feed_xy"] or 0.0) * factor or None,
                "feed_plunge": (operation["feed_plunge"] or 0.0) * factor or None,
                "spindle_rpm": operation["spindle_rpm"], "spindle_dir": operation["spindle_dir"],
                "stock_to_leave": {key: value * factor for key, value in operation["stock_to_leave"].items()},
                "tolerance": operation["tolerance"] * factor,
            })
            entry["controller"] = controller_key(entry, tool)
            if operation["type"] == "profile":
                entry.update(side=operation["side"], tabs={"count": operation["tabs"]["count"],
                                                           "width": operation["tabs"]["width"] * factor,
                                                           "height": operation["tabs"]["height"] * factor})
            if operation["type"] == "drilling":
                entry.update(peck=operation["peck"] * factor, dwell=operation["dwell"],
                             min_diameter=operation["selection"]["min_diameter"] * factor,
                             max_diameter=operation["selection"]["max_diameter"] * factor)
        elif operation["type"] == "tool_change":
            entry["tool"] = operation["tool"]
        plan.append(entry)
    return tools, plan


def depth_window(kind, stock_top, model_top, geometry_final, entry):
    """(départ, fin, avertissement) en Z absolu. Jamais plus profond que la géométrie ni que ``final`` sous le brut.

    - surfaçage : du dessus du brut jusqu'à ``final`` sous le brut, jamais sous le dessus du modèle ;
    - poche, contour, perçage : la profondeur vient de la géométrie (FreeCAD), bornée par ``final``.
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


def climb_direction(side, spindle_dir="cw"):
    """Sens de parcours d'un contour pour un fraisage en avalant (broche M3) : extérieur horaire, intérieur antihoraire."""
    clockwise = side == "outside"
    if spindle_dir == "ccw":
        clockwise = not clockwise
    return "CW" if clockwise else "CCW"


def comment_text(text):
    """Texte sûr dans un commentaire G-code « ( … ) » : jamais de parenthèse qui fermerait le commentaire."""
    clean = "".join(char if char.isprintable() else " " for char in str(text))
    return clean.replace("(", "[").replace(")", "]").replace(";", ",").strip()[:200]


def _spindle_on(entry, tools):
    rpm = entry.get("spindle_rpm") or tools[entry["tool"]]["spindle_rpm"]
    return ("M4" if entry.get("spindle_dir") == "ccw" else "M3") + " S%d" % int(round(rpm))


def sequence_steps(plan, tools, manual):
    """Ordre des blocs à poser dans le Job : [("custom", libellé, lignes, contrôleur, entrée|None) | ("op", entrée)].

    Sécurité des arrêts (les posts « script » comme grbl mettent M6 en commentaire) :
    - machine à changement MANUEL : chaque changement d'outil devient « M5, consigne, M0 », même sans étape
      « changement d'outil » dans la séquence ; le bloc du nouvel outil (posé par FreeCAD) relance la broche ;
    - une pause arrête la broche (M5) avant M0 et la relance ensuite quand l'outil ne change pas ;
    - un bloc personnalisé prend le contrôleur de l'usinage PRÉCÉDENT (aucun changement parasite) ; sans
      usinage avant lui, celui du suivant, dont FreeCAD pose le démarrage juste avant : on l'arrête puis relance.
    """
    steps, previous, explicit_change = [], None, False
    for position, entry in enumerate(plan):
        following = next((item for item in plan[position + 1:] if "controller" in item), None)
        kind = entry["type"]
        if kind in ("note", "pause", "tool_change"):
            text = comment_text(entry["note"] or entry["name"])
            anchor = previous or following
            key = anchor["controller"] if anchor else None
            if kind == "note":
                lines = ["(%s)" % text]
            elif kind == "pause":
                lines = ["M5", "(%s)" % text, "M0", _spindle_on(anchor, tools)] if anchor else ["(%s)" % text, "M0"]
            else:
                tool = tools.get(entry["tool"], {"name": ""})
                label = "Changement d'outil : monter T%d %s" % (entry["tool"], comment_text(tool["name"]))
                if manual:
                    lines = ["M5", "(%s)" % label, "M0"] if anchor else ["(%s)" % label, "M0"]
                    restart = following if (previous is None or (following and following["tool"] == previous["tool"])) else None
                    if anchor and restart:
                        lines.append(_spindle_on(restart, tools))
                else:
                    lines = ["(%s)" % label]
                explicit_change = True
            steps.append(("custom", entry["name"], lines, key, entry))
            continue
        changing = previous is not None and previous["tool"] != entry["tool"]
        automatic = manual and changing and not explicit_change
        if automatic or entry["pause_before"]:
            lines = ["M5"]
            if automatic:
                tool = tools[entry["tool"]]
                lines.append("(Changement d'outil : monter T%d %s)" % (tool["number"], comment_text(tool["name"])))
            if entry["pause_before"]:
                lines.append("(Pause avant %s)" % comment_text(entry["name"]))
            lines.append("M0")
            if previous is None or not changing:
                lines.append(_spindle_on(entry, tools))
            key = previous["controller"] if previous else entry["controller"]
            steps.append(("custom", "Arrêt avant " + entry["name"], lines, key, None))
        steps.append(("op", entry))
        previous, explicit_change = entry, False
    return steps


def post_arguments(post_name, known_arguments, project):
    """Ligne d'options du post-processeur. Seules les options que le post déclare sont transmises.

    ``known_arguments`` : noms « --xxx » lus dans son ``parser``. Un préambule ou postambule non vide est refusé si le
    post ne prend pas ``--preamble`` / ``--postamble`` (jamais de texte collé à la main dans le fichier).
    """
    known = set(known_arguments or ())
    tokens = []
    if "--no-show-editor" in known:
        tokens.append("--no-show-editor")
    for key, value in sorted(project["post"]["options"].items()):
        flag = "--" + key
        if flag not in known:
            raise CamEngineError("Option « %s » inconnue du post-processeur « %s » installé." % (key, post_name))
        if isinstance(value, bool):
            if value:
                tokens.append(flag)
        else:
            tokens.append("%s=%s" % (flag, value))
    if project["machine"]["units"] == "inch":
        if "--inches" not in known:
            raise CamEngineError("Le post-processeur « %s » ne sait pas produire un programme en pouces." % post_name)
        tokens.append("--inches")
    for block in ("preamble", "postamble"):
        lines = project["post"][block]
        if not lines:
            continue
        if "--" + block not in known:
            raise CamEngineError("Le post-processeur « %s » ne prend pas de %s personnalisé." % (
                post_name, "préambule" if block == "preamble" else "postambule"))
        tokens.append("--%s=%s" % (block, "\\n".join(lines)))
    return " ".join(shlex.quote(token) for token in tokens)


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
    for fixture in project["fixtures"]:
        factor = 25.4 if project["units"] == "inch" else 1.0
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
        if report["machining"] and not report["segments"]:
            issues.append({"code": "operation_empty", "severity": "warning", "operation": report["id"],
                           "message": "L'opération « %s » n'a produit aucun parcours (aucune géométrie détectée)." % report["name"]})
        for code, message in report.get("warnings", []):
            issues.append({"code": code, "severity": "warning", "operation": report["id"], "message": message})
    return issues


def setup_sheet_html(project, summary, stock, issues, validation, freecad_version):
    """Fiche de réglage : HTML simple, sans script, chaque valeur échappée."""
    e = lambda value: html.escape(str(value), quote=True)
    machine = project["machine"]
    rows = []
    rows.append("<h1>Fiche de réglage — %s</h1>" % e(project["name"]))
    rows.append("<p>Machine : %s · contrôleur %s · post-processeur %s · FreeCAD %s</p>"
                % (e(machine["label"]), e(machine["controller"]), e(machine["post_processor"]), e(freecad_version)))
    if stock:
        size = [stock[1][i] - stock[0][i] for i in range(3)]
        rows.append("<p>Brut : %.2f × %.2f × %.2f mm · origine %s (%s)</p>"
                    % (size[0], size[1], size[2], e(project["origin"]["preset"]), e(project["origin"]["wcs"])))
    rows.append("<h2>Outils</h2><table><tr><th>N°</th><th>Nom</th><th>Ø</th><th>Taillée</th><th>Broche</th></tr>")
    for tool in project["tools"]:
        rows.append("<tr><td>T%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s tr/min</td></tr>"
                    % (e(tool["number"]), e(tool["name"]), e(tool["diameter"]), e(tool["flute_length"]), e(tool["spindle_rpm"])))
    rows.append("</table><h2>Séquence</h2><ol>")
    for operation in project["operations"]:
        if operation["enabled"]:
            rows.append("<li>%s (%s)%s</li>" % (e(operation["name"]), e(operation["type"]),
                                                 " — " + e(operation["operator_note"]) if operation.get("operator_note") else ""))
    rows.append("</ol><h2>Contrôles</h2><ul>")
    for entry in list(validation.get("errors", [])) + list(validation.get("warnings", [])) + list(issues):
        rows.append("<li>%s : %s</li>" % (e(entry["code"]), e(entry["message"])))
    rows.append("</ul><p>Durée indicative : %s s (sans accélérations). Les vitesses et avances sont des points de départ, "
                "à vérifier par l'opérateur.</p>" % e(summary.get("time_estimate_s", 0)))
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
                      "units_argument": "--inches" in names})
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
    """Capacités CAM réelles de cette installation de FreeCAD (contrat §3)."""
    cam_available = _importable("Path.Main.Job")
    scripts_dir = ""
    try:
        import Path.Post.scripts as scripts
        scripts_dir = os.path.dirname(scripts.__file__)
    except Exception:
        scripts_dir = os.path.join(app.getHomePath(), "Mod", "CAM", "Path", "Post", "scripts")
    shapes_dir = os.path.join(app.getHomePath(), "Mod", "CAM", "Tools", "Shape")
    families = []
    if os.path.isdir(shapes_dir):
        for file_name in sorted(os.listdir(shapes_dir)):
            stem, extension = os.path.splitext(file_name)
            if extension.lower() == ".fcstd" and stem.lower() in SHAPE_FAMILIES:
                families.append(SHAPE_FAMILIES[stem.lower()])
    find = importlib.util.find_spec
    return {
        "freecad_version": ".".join(str(part) for part in app.Version()[:3]),
        "cam_available": cam_available,
        "operations": [alpine for alpine, module in OPERATION_MODULES.items() if cam_available and _importable(module)],
        "post_processors": discover_posts(scripts_dir, _load_module) if cam_available else [],
        "simulation": cam_available and find("CAMSimulator") is not None,
        "dependencies": {"opencamlib": find("opencamlib") is not None, "ocl": find("ocl") is not None,
                         "area": find("area") is not None},
        "tool_families": families,
    }


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
        elif kind in ("step", "stp", "iges", "igs"):
            shape = Part.Shape()
            shape.read(path)
        elif kind in ("brep", "brp"):
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
                # Concave (paroi d'un creux) : la normale extérieure de la face pointe vers l'axe.
                center = surface.Center
                point = face.valueAt((u0 + u1) / 2.0, (v0 + v1) / 2.0)
                radial = self.App.Vector(point.x - center.x, point.y - center.y, 0)
                entry["concave"] = radial.Length > 1e-9 and (radial.x * normal.x + radial.y * normal.y) < 0
                # Un trou rond complet se perce : seul un congé (cylindre partiel) limite la fraise.
                entry["partial"] = (u1 - u0) < 2.0 * math.pi - 1e-3
            out.append(entry)
        return out

    # -- Job CAM -------------------------------------------------------------------------------
    def create_job(self, shape, stock, wcs="G54"):
        App = self.App
        self.doc = App.newDocument("AlpineCam")
        App.setActiveDocument(self.doc.Name)
        self.model = self.doc.addObject("Part::Feature", "Piece")
        self.model.Shape = shape
        self.doc.recompute()
        import Path.Main.Job as PathJob
        import Path.Main.Stock as PathStock
        self.job = PathJob.Create("Job", [self.model], None)
        # Les opérations désignent les faces du modèle CLONÉ par le Job (Job.Model), jamais l'objet d'origine.
        self.base = self.job.Model.Group[0]
        # Repère de travail du projet (G54 à G59) : le post l'émet en tête de programme.
        if "Fixtures" in self.job.PropertiesList:
            self.job.Fixtures = [wcs]
        # Le brut créé par défaut est remplacé par la boîte du projet (repère pièce).
        old = self.job.Stock
        low, high = stock
        extent = App.Vector(high[0] - low[0], high[1] - low[1], high[2] - low[2])
        placement = App.Placement(App.Vector(low[0], low[1], low[2]), App.Rotation())
        self.job.Stock = PathStock.CreateBox(self.job, extent, placement)
        if old is not None and old.Name != self.job.Stock.Name:
            self.doc.removeObject(old.Name)
        # Les outils viennent du projet : le contrôleur « 5mm Endmill » posé par défaut est retiré.
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

    def add_controller(self, tool, key, label):
        App = self.App
        from Path.Tool.toolbit import ToolBit
        import Path.Tool.Controller as PathToolController
        bit = ToolBit.from_shape_id(FAMILY_SHAPES[tool["family"]], label=comment_text(tool["name"]))
        values = {"Diameter": tool["diameter"], "CuttingEdgeHeight": tool["flute_length"], "Length": tool["length"],
                  "ShankDiameter": tool["shank_diameter"]}
        for name, value in values.items():
            try:
                bit.set_property(name, App.Units.Quantity("%r mm" % float(value)))
            except Exception:
                pass
        try:
            bit.set_property("Flutes", int(tool["flutes"]))
        except Exception:
            pass
        tool_object = bit.attach_to_doc(doc=self.doc)
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
        return controller

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

    def add_operation(self, entry, controller, heights, stock_top):
        kind = entry["type"]
        module = importlib.import_module(OPERATION_MODULES[kind])
        self._chooser.current = controller
        op = module.Create(OPERATION_MODULES[kind].rsplit(".", 1)[-1], parentJob=self.job)
        op.Label = comment_text(entry["name"])
        op.ToolController = controller
        self._set(op, "ClearanceHeight", stock_top + heights["clearance"])
        self._set(op, "SafeHeight", stock_top + heights["safe"])
        if kind == "facing":
            # Sans face de base, MillFace ne produit aucun parcours sous FreeCADCmd (essai FreeCAD 1.1.3) : on lui donne
            # la ou les faces du dessus du modèle et on surface tout le brut (frontière « Stock », bords dégagés).
            shape = self.base.Shape
            top = shape.BoundBox.ZMax
            faces = ["Face%d" % (index + 1) for index, face in enumerate(shape.Faces)
                     if abs(face.BoundBox.ZMax - top) < 1e-6 and abs(face.BoundBox.ZMin - top) < 1e-6]
            if faces:
                op.Base = [(self.base, faces)]
            self._set(op, "BoundaryShape", "Stock")
            self._set(op, "ClearEdges", True)
        if kind in ("facing", "pocket"):
            self._set(op, "StepOver", int(round(entry["stepover_pct"])))
            if "CutMode" in op.PropertiesList:
                op.CutMode = "Climb" if entry["direction"] == "climb" else "Conventional"
        if kind == "pocket":
            bottoms = [("Face%d" % (index + 1)) for index, face in enumerate(self.base.Shape.Faces)
                       if self._is_pocket_floor(face)]
            if bottoms:
                op.Base = [(self.base, bottoms)]
        if kind == "profile":
            self._set(op, "Side", "Outside" if entry["side"] == "outside" else "Inside")
            self._set(op, "processPerimeter", entry["side"] == "outside")
            self._set(op, "processHoles", entry["side"] == "inside")
            self._set(op, "processCircles", False)   # les trous ronds restent au perçage
            self._set(op, "UseComp", True)
            direction = climb_direction(entry["side"], entry["spindle_dir"])
            if entry["direction"] == "conventional":
                direction = "CCW" if direction == "CW" else "CW"
            self._set(op, "Direction", direction)
            if entry["stock_to_leave"]["radial"]:
                self._set(op, "OffsetExtra", entry["stock_to_leave"]["radial"])
        if kind == "drilling":
            import Path.Base.Drillable as Drillable
            targets = []
            for obj, sub in Drillable.getDrillableTargets(self.base):
                face = self.base.Shape.getElement(sub)
                diameter = 2.0 * float(getattr(face.Surface, "Radius", 0.0))
                if entry["min_diameter"] - 1e-6 <= diameter <= entry["max_diameter"] + 1e-6:
                    targets.append(sub)
            if targets:
                op.Base = [(self.base, targets)]
            self._set(op, "PeckEnabled", bool(entry["peck"]))
            if entry["peck"]:
                self._set(op, "PeckDepth", entry["peck"])
            self._set(op, "DwellEnabled", bool(entry["dwell"]))
            if entry["dwell"]:
                self._set(op, "DwellTime", float(entry["dwell"]))
            self._set(op, "RetractHeight", stock_top + heights["retract"])
        if "CoolantMode" in op.PropertiesList:
            op.CoolantMode = {"flood": "Flood", "mist": "Mist"}.get(entry["coolant"], "None")
        return op

    def _is_pocket_floor(self, face):
        """Fond de poche : face plane horizontale tournée vers le haut, sous le dessus du modèle."""
        try:
            u0, u1, v0, v1 = face.ParameterRange
            normal = face.normalAt((u0 + u1) / 2.0, (v0 + v1) / 2.0)
        except Exception:
            return False
        box = self.base.Shape.BoundBox
        return (type(face.Surface).__name__ == "Plane" and abs(normal.z - 1.0) < 1e-6
                and face.BoundBox.ZMax < box.ZMax - 1e-6 and face.BoundBox.ZMin > box.ZMin + 1e-6)

    def set_depths(self, op, start, final, step_down):
        self._set(op, "StartDepth", start)
        self._set(op, "FinalDepth", final)
        self._set(op, "StepDown", step_down)

    def geometry_final(self, op):
        try:
            return float(op.FinalDepth.Value)
        except Exception:
            return None

    def add_tabs(self, op, tabs):
        if not tabs["count"]:
            return None
        import Path.Dressup.Tags as Tags
        dressup = Tags.Create(op)
        if dressup is None:
            raise CamEngineError("FreeCAD a refusé les ponts sur ce contour.")
        dressup.Width = tabs["width"]
        dressup.Height = tabs["height"]
        dressup.Proxy.generateTags(dressup, int(tabs["count"]))
        # Tags.Create (FreeCAD 1.1) insère le dressup AVANT le contour sans retirer ce dernier du Job : le post
        # couperait le contour deux fois, la seconde sans ponts. Comme Boundary et Array (removeBefore=True), le
        # contour de base ne reste que la source du dressup.
        group = list(self.job.Operations.Group)
        if op in group:
            group.remove(op)
            self.job.Operations.Group = group
        return dressup

    def add_custom(self, label, lines, controller):
        import Path.Op.Custom as Custom
        # Une opération FreeCAD prend toujours un contrôleur : celui de l'usinage voisin, pour ne provoquer aucun
        # changement d'outil parasite (pour un changement d'outil : celui de l'outil suivant).
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

    def path_count(self, op):
        try:
            return sum(1 for command in op.Path.Commands if command.Name in ("G1", "G01", "G2", "G02", "G3", "G03",
                                                                               "G81", "G82", "G83", "G73"))
        except Exception:
            return 0

    def post(self, name, arguments):
        from Path.Post.Processor import PostProcessorFactory
        processor = PostProcessorFactory.get_post_processor(self.job, name)
        if processor is None:
            raise CamEngineError("Post-processeur « %s » introuvable dans cette installation de FreeCAD." % name)
        self.job.PostProcessorArgs = arguments
        sections = processor.export() or []
        text = "".join(code or "" for _name, code in sections)
        return text

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
def compute(project, source, workdir, backend, cam_gcode_check, freecad_version):
    factor = 25.4 if project["units"] == "inch" else 1.0
    source_scale = 25.4 if project["source"]["units"] == "inch" else 1.0
    shape = backend.import_model(source, project["source"]["kind"])
    shape = backend.transform(shape, source_scale, orientation_steps(project["orientation"]))
    info = backend.model_info(shape)
    issues = model_checks(info)
    stock_model, _ = stock_box(info["bounds"], project["stock"], factor)
    low, high = info["bounds"]
    if project["stock"]["mode"] == "explicit" and any(low[i] < stock_model[0][i] - 1e-6 or high[i] > stock_model[1][i] + 1e-6
                                                      for i in range(3)):
        raise CamEngineError("La pièce ne tient pas dans le brut saisi : agrandis le brut ou choisis « depuis le modèle ».")
    origin = origin_point(stock_model, project["origin"], factor)
    shape = backend.translate(shape, origin)
    stock = shift_box(stock_model, origin)
    info = backend.model_info(shape)
    machine = project["machine"]
    size = [stock[1][i] - stock[0][i] for i in range(3)]
    for index, axis in enumerate("xyz"):
        if size[index] > machine["travel"][axis] + 1e-6:
            raise CamEngineError("Brut plus grand que la course %s de la machine (%.1f mm pour %g mm)."
                                 % (axis.upper(), size[index], machine["travel"][axis]))
    heights = {key: project["heights"][key] * factor for key in ("clearance", "safe", "retract")}
    stock_top, model_top = stock[1][2], info["bounds"][1][2]
    tools, plan = operations_plan(project)
    backend.create_job(shape, stock, project["origin"]["wcs"])
    controllers, op_reports = {}, []
    for entry in plan:   # tous les contrôleurs d'abord : une opération FreeCAD en exige un
        if "controller" in entry and entry["controller"] not in controllers:
            tool = tools[entry["tool"]]
            controllers[entry["controller"]] = backend.add_controller(
                tool, (tool["number"], entry["feed_xy"] or tool["feed_xy"], entry["feed_plunge"] or tool["feed_plunge"],
                       entry["spindle_rpm"] or tool["spindle_rpm"], entry["spindle_dir"]),
                "T%d %s" % (tool["number"], comment_text(tool["name"])))
    manual = project["machine"]["tool_change"] == "manual"
    for step in sequence_steps(plan, tools, manual):
        if step[0] == "custom":
            _kind, label, lines, key, report = step
            backend.add_custom(label, lines, controllers[key] if key else None)
            if report:
                op_reports.append({"id": report["id"], "type": report["type"], "name": report["name"],
                                   "machining": False, "segments": 0, "ok": True})
            continue
        entry = step[1]
        tool = tools[entry["tool"]]
        op = backend.add_operation(entry, controllers[entry["controller"]], heights, stock_top)
        backend.recompute()
        start, final, warning = depth_window(entry["type"], stock_top, model_top, backend.geometry_final(op), entry)
        backend.set_depths(op, start, final, entry["step_down"])
        warnings = [warning] if warning else []
        if entry["type"] == "profile" and entry["tabs"]["count"]:
            try:
                backend.add_tabs(op, entry["tabs"])
            except Exception as error:
                warnings.append(("tabs_not_applied", "Ponts non posés (%s) : la pièce peut se détacher en fin de contour."
                                 % type(error).__name__))
        if stock_top - final > tool["flute_length"] + 1e-6:
            raise CamEngineError("Opération « %s » : profondeur %.2f mm supérieure à la longueur taillée de T%d."
                                 % (entry["name"], stock_top - final, tool["number"]))
        op_reports.append({"id": entry["id"], "type": entry["type"], "name": entry["name"], "machining": True,
                           "op": op, "warnings": warnings})
    backend.recompute()
    for report in op_reports:
        if report["machining"]:
            report["segments"] = backend.path_count(report.pop("op"))
            report["ok"] = report["segments"] > 0
    post_name = machine["post_processor"]
    arguments = post_arguments(post_name, backend.post_known_arguments(post_name), project)
    gcode = backend.post(post_name, arguments)
    if not gcode or not gcode.strip():
        raise CamEngineError("Le post-processeur n'a produit aucun G-code.")
    if len(gcode.encode("utf-8")) > GCODE_MAX_BYTES:
        raise CamEngineError("G-code trop volumineux (plus de 64 Mo).")
    stem = project["output"]["stem"]
    gcode_name = "%s.%s" % (stem, machine["output_extension"])
    with open(os.path.join(workdir, gcode_name), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(gcode)
    stock_bounds = {"min": stock[0], "max": stock[1]}
    validation = cam_gcode_check.check_gcode(gcode, project, machine, stock=stock_bounds)
    simulation = cam_gcode_check.simulation_segments(gcode, SIMULATION_LIMIT)
    issues += manufacturability(project, tools, stock, simulation["segments"], backend.faces(shape), op_reports)
    if info.get("solids", 1) > 1:
        issues.append({"code": "closed_cavity", "severity": "warning", "operation": "",
                       "message": "Le modèle contient plusieurs solides (cavité fermée ou pièces séparées) : vérifie le résultat."})
    stats = validation["stats"]
    summary = {"bounds": stats["bounds"], "length_mm": stats["length_mm"], "time_estimate_s": stats["time_estimate_s"],
               "tools_used": sorted({entry["tool"] for entry in plan if entry.get("tool") and entry["type"] != "tool_change"}),
               "operations": [{"id": report["id"], "type": report["type"], "ok": report["ok"], "segments": report["segments"]}
                              for report in op_reports]}
    fcstd_name, json_name, html_name = stem + ".FCStd", stem + ".cam.json", stem + ".setup.html"
    backend.save(os.path.join(workdir, fcstd_name))
    with open(os.path.join(workdir, json_name), "w", encoding="utf-8") as handle:
        json.dump({"project": project, "freecad_version": freecad_version, "post_processor": post_name,
                   "post_arguments": arguments}, handle, ensure_ascii=False, indent=1, allow_nan=False)
    with open(os.path.join(workdir, html_name), "w", encoding="utf-8") as handle:
        handle.write(setup_sheet_html(project, summary, stock, issues, validation, freecad_version))
    cam_result = {
        "ok": True, "freecad_version": freecad_version, "post_processor": post_name, "gcode_file": gcode_name,
        "gcode_lines": len(gcode.splitlines()), "gcode_bytes": len(gcode.encode("utf-8")), "summary": summary,
        "model": {"bounds": {"min": info["bounds"][0], "max": info["bounds"][1]}, "is_solid": info["is_solid"],
                  "is_mesh": info["is_mesh"], "facets": info["facets"], "units_assumed": project["source"]["units"]},
        "stock": {"size": {"x": size[0], "y": size[1], "z": size[2]}, "origin_machine": [0.0, 0.0, 0.0],
                  "bounds": {"min": stock[0], "max": stock[1]}},
        "manufacturability": issues,
        "validation": {"ok": validation["ok"], "errors": validation["errors"], "warnings": validation["warnings"]},
        "simulation": simulation,
    }
    with open(os.path.join(workdir, "cam_result.json"), "w", encoding="utf-8") as handle:
        json.dump(cam_result, handle, ensure_ascii=False, allow_nan=False)
    artifacts = [gcode_name, fcstd_name, json_name, html_name, "cam_result.json"]
    return {"op": "compute", "gcode_file": gcode_name, "artifacts": artifacts,
            "validation_ok": validation["ok"], "errors": len(validation["errors"]),
            "warnings": len(validation["warnings"]), "manufacturability": len(issues), "freecad_version": freecad_version}


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
        with open(os.path.join(workdir, "cam_capabilities.json"), "w", encoding="utf-8") as handle:
            json.dump(found, handle, ensure_ascii=False)
        result(True, op="capabilities", freecad_version=version, artifacts=["cam_capabilities.json"],
               cam_available=found["cam_available"], post_processors=len(found["post_processors"]))
        return
    if not _importable("Path.Main.Job"):
        raise CamEngineError("FreeCAD CAM n'est pas disponible sur ce Worker (module Path absent).")
    backend = FreeCadCam()
    try:
        details = compute(project, source, workdir, backend, cam_gcode_check, version)
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
    except Exception as exc:
        traceback.print_exc()
        message = str(exc) if isinstance(exc, (CamEngineError, ValueError)) else "Erreur du moteur CAM : %s" % type(exc).__name__
        result(False, error=message)
        if __name__ == "__main__":
            sys.exit(2)  # importé par FreeCADCmd, un SystemExit relancerait le fichier : la ligne de résultat suffit
