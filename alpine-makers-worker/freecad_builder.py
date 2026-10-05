# -*- coding: utf-8 -*-
"""
Raphal Production Dashboard — FreeCAD parametric build worker.

Builds an exact CAD solid from a declarative scene (primitives + booleans) and
exports it. The scene arrives as a JSON *file*, never as an inline string and
never as Python: this worker contains no eval, no exec and no import driven by
the payload. It re-checks every bound itself because an engine must not trust
the job it is handed, even when the server already validated it.

Called by:
  FreeCADCmd.exe freecad_builder.py SCENE_JSON OUTPUT
"""
import json
import os
import sys
import traceback

RESULT_PREFIX = "RAPHAL_FREECAD_RESULT="


def result(ok, **kwargs):
    print(RESULT_PREFIX + json.dumps({"ok": bool(ok), **kwargs}, ensure_ascii=False), flush=True)


try:
    import FreeCAD as App
    import Part
    import Mesh
    import Import
except Exception as exc:  # pragma: no cover - only on a Worker without FreeCAD
    result(False, error="Impossible de charger les modules FreeCAD: %s" % exc)
    raise

# Deliberately a literal duplicate of worker_agent/safety.py _SCENE_PRIMITIVES.
# The two must stay identical; a test builds one reference scene through both.
PRIMITIVE_BOUNDS = {
    "box": {"size": (0.1, 300.0)},
    "cylinder": {"radius": (0.05, 150.0), "height": (0.1, 300.0)},
    "sphere": {"radius": (0.05, 150.0)},
    "cone": {"radius1": (0.0, 150.0), "radius2": (0.0, 150.0), "height": (0.1, 300.0)},
    "torus": {"radius1": (0.1, 150.0), "radius2": (0.05, 75.0)},
}
BOOLEANS = {"union": "fuse", "difference": "cut", "intersection": "common"}
MAX_NODES = 64


def _number(value, bounds, label):
    low, high = bounds
    if type(value) not in (int, float) or value != value or value in (float("inf"), float("-inf")):
        raise RuntimeError("Valeur invalide pour %s." % label)
    if not low <= value <= high:
        raise RuntimeError("Valeur hors limites pour %s." % label)
    return float(value)


def check_scene(scene):
    """Second enforcement layer, independent from the server's validation."""
    if not isinstance(scene, dict) or scene.get("version") != 1:
        raise RuntimeError("Version de scene non prise en charge.")
    nodes = scene.get("nodes")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES:
        raise RuntimeError("Nombre d'elements de scene invalide.")
    seen = []
    for node in nodes:
        if not isinstance(node, dict):
            raise RuntimeError("Element de scene invalide.")
        operation, identifier = node.get("op"), node.get("id")
        if not isinstance(identifier, str) or identifier in seen:
            raise RuntimeError("Identifiant d'element invalide.")
        if operation in PRIMITIVE_BOUNDS:
            for field, bounds in PRIMITIVE_BOUNDS[operation].items():
                if field == "size":
                    values = node.get("size")
                    if not isinstance(values, list) or len(values) != 3:
                        raise RuntimeError("Taille de boite invalide.")
                    for item in values:
                        _number(item, bounds, "taille")
                else:
                    _number(node.get(field), bounds, field)
        elif operation in BOOLEANS:
            operands = node.get("operands")
            if not isinstance(operands, list) or not 2 <= len(operands) <= 8:
                raise RuntimeError("Operation booleenne invalide.")
            for operand in operands:
                if operand not in seen:
                    raise RuntimeError("Operande booleenne non definie avant usage.")
        else:
            raise RuntimeError("Operation de scene non autorisee.")
        seen.append(identifier)
    if scene.get("result") not in seen:
        raise RuntimeError("Resultat de scene introuvable.")
    return scene


def _primitive(node, api):
    operation = node["op"]
    if operation == "box":
        x, y, z = node["size"]
        return api.Part.makeBox(float(x), float(y), float(z))
    if operation == "cylinder":
        return api.Part.makeCylinder(float(node["radius"]), float(node["height"]))
    if operation == "sphere":
        return api.Part.makeSphere(float(node["radius"]))
    if operation == "cone":
        return api.Part.makeCone(float(node["radius1"]), float(node["radius2"]), float(node["height"]))
    return api.Part.makeTorus(float(node["radius1"]), float(node["radius2"]))


def _combine(node, shapes):
    method = BOOLEANS[node["op"]]
    shape = shapes[node["operands"][0]]
    for operand in node["operands"][1:]:
        shape = getattr(shape, method)(shapes[operand])
        # OCCT legitimately returns an empty shape for coincident faces or a
        # zero-thickness result. Without this the job would "succeed" and write
        # an empty STL, and the runner would report a missing output instead of
        # the real cause.
        if shape.isNull():
            raise RuntimeError("L'operation booleenne a produit une forme vide.")
        try:
            shape = shape.removeSplitter()
        except Exception:
            pass
    return shape


def _placed(shape, placement, api):
    position = placement["position"]
    rotation = placement.get("rotation") or [0.0, 0.0, 1.0, 0.0]
    shape.Placement = api.App.Placement(
        api.App.Vector(float(position[0]), float(position[1]), float(position[2])),
        api.App.Rotation(api.App.Vector(float(rotation[0]), float(rotation[1]), float(rotation[2])), float(rotation[3])),
    )
    return shape


def build_scene(scene, api):
    """Build the result solid. ``api`` carries App and Part so this stays testable."""
    shapes = {}
    for node in scene["nodes"]:
        shape = _primitive(node, api) if node["op"] in PRIMITIVE_BOUNDS else _combine(node, shapes)
        if shape is None or shape.isNull():
            raise RuntimeError("L'element « %s » n'a produit aucune forme." % node["id"])
        if node.get("placement"):
            shape = _placed(shape, node["placement"], api)
        shapes[node["id"]] = shape
    return shapes[scene["result"]]


class _Api(object):
    """The FreeCAD modules, injected rather than imported by the build logic."""

    def __init__(self, app, part):
        self.App = app
        self.Part = part


def main():
    # FreeCADCmd keeps its own executable in argv[0] and passes the Python
    # worker path again as argv[1]. Plain Python does not. Normalize both.
    args = list(sys.argv[1:])
    if args and args[0].lower().endswith(".py"):
        args = args[1:]
    if len(args) < 2:
        raise RuntimeError("Arguments manquants: scene JSON et sortie requises.")
    scene_path = os.path.abspath(args[0])
    output_path = os.path.abspath(args[1])
    if not os.path.isfile(scene_path):
        raise RuntimeError("Scene introuvable: " + scene_path)
    with open(scene_path, "r", encoding="utf-8") as handle:
        scene = check_scene(json.load(handle))

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    if os.path.exists(output_path):
        os.remove(output_path)

    shape = build_scene(scene, _Api(App, Part))
    doc = App.newDocument("AlpineForge")
    try:
        obj = doc.addObject("Part::Feature", "ForgeResult")
        obj.Shape = shape
        doc.recompute()
        extension = os.path.splitext(output_path)[1].lower()
        if extension == ".stl":
            Mesh.export([obj], output_path)
        elif extension in (".step", ".stp"):
            Import.export([obj], output_path)
        else:
            raise RuntimeError("Format de sortie non autorise: " + extension)
        if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError("FreeCAD n'a produit aucun fichier de sortie.")
        volume = 0.0
        try:
            volume = float(shape.Volume)
        except Exception:
            pass
        result(
            True,
            output=os.path.basename(output_path),
            bytes=os.path.getsize(output_path),
            freecad_version=".".join(map(str, App.Version()[:3])),
            nodes=len(scene["nodes"]),
            volume_mm3=round(volume, 3),
        )
    finally:
        try:
            App.closeDocument(doc.Name)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        traceback.print_exc()
        result(False, error=str(exc))
        sys.exit(2)
