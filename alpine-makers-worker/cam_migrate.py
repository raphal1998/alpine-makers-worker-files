# -*- coding: utf-8 -*-
"""Migration du projet CAM déclaratif : v1 → v2 (``upgrade``) et v2 → v1 (``downgrade``).

Fonctions pures, bibliothèque standard seulement (le fichier est livré à plat dans le paquet du
Worker et importé par le site). Aucune validation ici : ``upgrade`` tolère un brouillon incomplet et ne
transforme que les clés présentes ; ``validate_cam_project`` (cam_payload) valide ensuite. Contrat :
docs/cam_projet_v2.md § 11.

- ``upgrade(project)`` → ``(project_v2, notes)`` ; déterministe ; ne modifie jamais l'objet reçu.
- ``downgrade(project)`` → projet v1 ou ``CamDowngradeError`` listant les fonctions v2 utilisées.
- ``v2_features(project)`` → liste (chemin, raison) des fonctions v2 qui empêchent le retour en v1.
"""
import copy

V1 = 1
V2 = 2

# Opérations et familles d'outils connues du contrat v1 (reproduites ici pour rester sans import).
V1_OPERATION_TYPES = ("facing", "pocket", "profile", "drilling", "pause", "tool_change", "note")
V1_TOOL_FAMILIES = ("endmill", "drill")
V1_REMOVED_OPERATION_KEYS = ("entry", "ramp_angle", "lead_in", "lead_out", "compensation", "tabs")
V1_MACHINE_PROVENANCE_FIELDS = ("travel.x", "travel.y", "travel.z", "max_feed", "max_rapid", "max_accel",
                                "spindle.min_rpm", "spindle.max_rpm", "spindle.power_w", "safe_height_min",
                                "tool_change", "tool_count", "controller", "coolant")
# Clés v2 des outils sans équivalent v1 (si renseignées, le retour en v1 perd une information de forme).
V2_TOOL_SHAPE_KEYS = ("cutting_edge_angle", "tip_diameter", "crest", "neck_diameter", "neck_length",
                      "holder_diameter", "holder_length")
V2_TOOL_INFO_KEYS = ("source_ref",)
# Réglages propres v2 : (clé, valeur neutre = comportement v1). Toute autre valeur bloque le retour en v1.
V2_OPERATION_NEUTRAL = {
    "pattern": "zigzag", "zigzag_angle": 45, "clear_edges": True, "start_at": "center", "exclude_raised": False,
    "rest_machining": False, "boundary": "stock", "use_comp": True, "passes": 1, "pass_stepover": None,
    "join_type": "round", "process_perimeter": True, "process_holes": False, "process_circles": False,
    "handle_multiple": "collectively", "retract_mode": "G98", "retract_height": None, "extra_offset": "none",
    "keep_tool_down": False, "min_travel": False, "heights": None, "stop_spindle": True,
    "go_to_change_position": False,
}
V2_OPERATION_INFO_KEYS = ("description", "phase")
V2_MACHINE_INFO_KEYS = ("provenance", "firmware_snapshot", "grbl", "tool_change_position")
V2_ROOT_INFO_KEYS = ("assistant", "results")
TOLERANCE_OPERATIONS = ("adaptive", "vcarve")
AXIAL_LEAVE_OPERATIONS = ("adaptive",)


class CamDowngradeError(ValueError):
    """Le projet utilise des fonctions v2 : ``features`` = ``[(chemin, raison), …]``."""

    def __init__(self, features):
        self.features = list(features)
        summary = " ; ".join(f"{path} : {reason}" for path, reason in self.features[:6])
        if len(self.features) > 6:
            summary += f" ; … ({len(self.features)} fonctions v2 au total)"
        super().__init__("Ce projet utilise des fonctions du contrat v2 et ne peut pas être envoyé à un Worker "
                         f"freecad_cam_v1 (agent à mettre à jour) : {summary}")


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _dig(value, *keys):
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def project_version(project):
    """Version déclarée (1 ou 2) ; ``None`` si absente ou invalide."""
    version = project.get("version") if isinstance(project, dict) else None
    if isinstance(version, bool) or not isinstance(version, (int, float)) or version not in (V1, V2):
        return None
    return int(version)


# ---------------------------------------------------------------------------------------------
# v1 → v2
# ---------------------------------------------------------------------------------------------
def upgrade(project):
    """Projet v2 et notes de migration. Un projet déjà v2 est rendu copié, sans note."""
    if not isinstance(project, dict):
        raise ValueError("Projet CAM invalide : objet attendu.")
    result = copy.deepcopy(project)
    if project_version(project) == V2:
        return result, []
    notes = []
    result["version"] = V2
    source = result.get("source")
    if isinstance(source, dict) and "scale" not in source:
        source["scale"] = 1
    machine = result.get("machine")
    if isinstance(machine, dict):
        verified = machine.pop("verified", None)
        if "provenance" not in machine:
            origin = "profile" if verified is True else "assumed"
            provenance = {}
            for field in V1_MACHINE_PROVENANCE_FIELDS:
                if _dig(machine, *field.split(".")) is not None:
                    provenance[field] = {"source": origin, "read_at": None}
            if provenance:
                machine["provenance"] = provenance
                notes.append({"path": "machine.provenance", "code": "provenance_unknown",
                              "message": "Provenance des valeurs machine inconnue avant v2 : marquée « "
                                         + ("profil" if origin == "profile" else "supposée")
                                         + " » (verified v1 : " + ("oui" if verified is True else "non") + ")."})
    stock = result.get("stock")
    if isinstance(stock, dict):
        if "material_ref" not in stock:
            material = stock.pop("material_id", None)
            stock["material_ref"] = {"kind": "catalog", "id": material} if isinstance(material, str) and material else None
        else:
            stock.pop("material_id", None)
        if "placement" not in stock:
            stock["placement"] = "center_top"
    heights_tolerance = _dig(result, "heights", "tolerance")
    operations = result.get("operations")
    if isinstance(operations, list):
        for index, operation in enumerate(operations):
            if isinstance(operation, dict):
                _upgrade_operation(operation, f"operations[{index}]", heights_tolerance, notes)
    if "sequence" not in result:
        result["sequence"] = {"tool_change_policy": "pause_m0", "spindle_wait_s": 0}
    return result, notes


def _upgrade_operation(operation, where, heights_tolerance, notes):
    kind = operation.get("type")
    tabs = operation.get("tabs")
    if "dressups" not in operation and kind == "profile":
        dressups = []
        if isinstance(tabs, dict) and _is_number(tabs.get("count")) and tabs["count"] > 0:
            tags = {"type": "tags", "count": tabs["count"]}
            for key in ("width", "height"):
                if key in tabs:
                    tags[key] = tabs[key]
            dressups.append(tags)
        operation["dressups"] = dressups
    entry = operation.get("entry")
    if isinstance(entry, str) and entry != "plunge":
        notes.append({"path": f"{where}.entry", "code": "entry_without_effect",
                      "message": f"Entrée « {entry} » retirée : aucune rampe ni hélice n'était produite en v1 "
                                 "(RampEntry indisponible en console FreeCAD 1.1.3) ; l'entrée reste une plongée."})
    for key in ("lead_in", "lead_out"):
        if _is_number(operation.get(key)) and operation[key] != 0:
            notes.append({"path": f"{where}.{key}", "code": "lead_without_effect",
                          "message": f"« {key} » retiré : LeadInOut n'existe pas en console FreeCAD 1.1.3 "
                                     "(la valeur v1 n'avait aucun effet)."})
    for key in V1_REMOVED_OPERATION_KEYS:
        operation.pop(key, None)
    if "tolerance" in operation and kind not in TOLERANCE_OPERATIONS:
        value = operation.get("tolerance")
        if _is_number(value) and (not _is_number(heights_tolerance) or abs(value - heights_tolerance) > 1e-12):
            notes.append({"path": f"{where}.tolerance", "code": "tolerance_global_only",
                          "message": f"Tolérance par opération retirée pour « {kind} » : FreeCAD 1.1.3 n'en a pas, "
                                     "la tolérance globale heights.tolerance (Job.GeometryTolerance) s'applique."})
        operation["tolerance"] = None
    leave = operation.get("stock_to_leave")
    if isinstance(leave, dict) and kind not in AXIAL_LEAVE_OPERATIONS and _is_number(leave.get("axial")) and leave["axial"] != 0:
        notes.append({"path": f"{where}.stock_to_leave.axial", "code": "axial_leave_without_effect",
                      "message": f"Surépaisseur axiale remise à 0 pour « {kind} » : sans effet en v1 et non prise en "
                                 "charge par FreeCAD 1.1.3 hors Adaptive."})
        leave["axial"] = 0
    if kind == "drilling":
        # Le perçage n'a ni passes en Z (StepDown) ni recouvrement (StepOver) : valeurs v1 sans effet.
        depth = operation.get("depth")
        if isinstance(depth, dict):
            depth.pop("step_down", None)
        if "stepover_pct" in operation:
            operation["stepover_pct"] = None
    if kind == "drilling" and _is_number(operation.get("peck")) and operation["peck"] > 0 \
            and _is_number(operation.get("dwell")) and operation["dwell"] > 0:
        notes.append({"path": f"{where}.dwell", "code": "peck_and_dwell",
                      "message": "Débourrage et temporisation ensemble : FreeCAD refuse la combinaison ; la "
                                 "temporisation est remise à 0 (le débourrage est conservé)."})
        operation["dwell"] = 0


# ---------------------------------------------------------------------------------------------
# v2 → v1
# ---------------------------------------------------------------------------------------------
def v2_features(project):
    """Fonctions v2 utilisées qui empêchent le retour en v1 : ``[(chemin, raison), …]``."""
    found = []
    if not isinstance(project, dict):
        return found
    # Les opérations d'abord : c'est la raison la plus parlante dans le message de refus.
    found.extend(_operation_features(project))
    scale = _dig(project, "source", "scale")
    if _is_number(scale) and abs(scale - 1.0) > 1e-12:
        found.append(("source.scale", "facteur d'échelle différent de 1"))
    stock = project.get("stock") if isinstance(project.get("stock"), dict) else {}
    if stock.get("shape") not in (None, "box"):
        found.append(("stock.shape", f"brut « {stock.get('shape')} »"))
    if stock.get("placement") not in (None, "center_top"):
        found.append(("stock.placement", "placement de la pièce dans le brut"))
    material = stock.get("material_ref")
    if isinstance(material, dict) and material.get("kind") == "library":
        found.append(("stock.material_ref", "matériau de la bibliothèque du compte"))
    for index, tool in enumerate(project.get("tools") if isinstance(project.get("tools"), list) else []):
        if not isinstance(tool, dict):
            continue
        where = f"tools[{index}]"
        if tool.get("family") not in V1_TOOL_FAMILIES and tool.get("family") is not None:
            found.append((f"{where}.family", f"famille d'outil « {tool.get('family')} »"))
        for key in V2_TOOL_SHAPE_KEYS:
            if tool.get(key) not in (None, 0):
                found.append((f"{where}.{key}", "paramètre de forme ou de porte-outil v2"))
    sequence = project.get("sequence") if isinstance(project.get("sequence"), dict) else {}
    if sequence.get("tool_change_policy") not in (None, "pause_m0"):
        found.append(("sequence.tool_change_policy", f"politique « {sequence.get('tool_change_policy')} »"))
    if _is_number(sequence.get("spindle_wait_s")) and sequence["spindle_wait_s"] > 0:
        found.append(("sequence.spindle_wait_s", "attente de broche"))
    return found


def _operation_features(project):
    found = []
    for index, operation in enumerate(project.get("operations") if isinstance(project.get("operations"), list) else []):
        if not isinstance(operation, dict):
            continue
        where = f"operations[{index}]"
        kind = operation.get("type")
        if kind not in V1_OPERATION_TYPES and kind is not None:
            found.append((f"{where}.type", f"opération « {kind} »"))
            continue
        if _dig(operation, "selection", "mode") not in (None, "auto"):
            found.append((f"{where}.selection.mode", "sélection de faces"))
        if operation.get("feed_ramp") is not None:
            found.append((f"{where}.feed_ramp", "avance de rampe par opération"))
        if kind in ("pause", "tool_change", "note") and operation.get("pause_before") is True:
            found.append((f"{where}.pause_before", "pause avant une étape manuelle"))
        for dressup in operation.get("dressups") if isinstance(operation.get("dressups"), list) else []:
            if isinstance(dressup, dict) and dressup.get("type") != "tags":
                found.append((f"{where}.dressups", f"dressup « {dressup.get('type')} »"))
            elif isinstance(dressup, dict) and any(dressup.get(key) not in (None, 45 if key == "angle" else 0)
                                                   for key in ("angle", "radius", "segmentation")):
                found.append((f"{where}.dressups", "paramètres de ponts v2 (angle, rayon, segmentation)"))
        finish = _dig(operation, "depth", "finish")
        if _is_number(finish) and finish != 0:
            found.append((f"{where}.depth.finish", "passe de finition"))
        if operation.get("tolerance") is not None and kind in TOLERANCE_OPERATIONS:
            found.append((f"{where}.tolerance", "tolérance par opération"))
        for key, neutral in V2_OPERATION_NEUTRAL.items():
            if key in operation and operation[key] != neutral:
                found.append((f"{where}.{key}", "réglage propre v2 différent du comportement v1"))
    return found


def downgrade(project):
    """Projet v1 équivalent ; ``CamDowngradeError`` si une fonction v2 est utilisée. Un v1 est rendu copié."""
    if not isinstance(project, dict):
        raise ValueError("Projet CAM invalide : objet attendu.")
    if project_version(project) == V1:
        return copy.deepcopy(project)
    features = v2_features(project)
    if features:
        raise CamDowngradeError(features)
    result = copy.deepcopy(project)
    result["version"] = V1
    for key in V2_ROOT_INFO_KEYS + ("sequence",):
        result.pop(key, None)
    source = result.get("source")
    if isinstance(source, dict):
        source.pop("scale", None)
        source.pop("sha256", None)
    machine = result.get("machine")
    if isinstance(machine, dict):
        for key in V2_MACHINE_INFO_KEYS:
            machine.pop(key, None)
    stock = result.get("stock")
    if isinstance(stock, dict):
        material = stock.pop("material_ref", None)
        stock["material_id"] = material["id"] if isinstance(material, dict) and isinstance(material.get("id"), str) else ""
        stock.pop("placement", None)
        stock.pop("placement_offset", None)
        stock.pop("material_label", None)
        stock.pop("diameter", None)
        stock.pop("height", None)
    for tool in result.get("tools") if isinstance(result.get("tools"), list) else []:
        if isinstance(tool, dict):
            for key in V2_TOOL_SHAPE_KEYS + V2_TOOL_INFO_KEYS:
                tool.pop(key, None)
    for operation in result.get("operations") if isinstance(result.get("operations"), list) else []:
        if isinstance(operation, dict):
            _downgrade_operation(operation)
    return result


def _downgrade_operation(operation):
    kind = operation.get("type")
    for key in V2_OPERATION_INFO_KEYS:
        operation.pop(key, None)
    for key in V2_OPERATION_NEUTRAL:
        operation.pop(key, None)
    depth = operation.get("depth")
    if isinstance(depth, dict):
        depth.pop("finish", None)
    dressups = operation.pop("dressups", None)
    if kind == "profile":
        tags = next((item for item in dressups or [] if isinstance(item, dict) and item.get("type") == "tags"), None)
        if tags:
            operation["tabs"] = {"count": tags.get("count", 0), "width": tags.get("width", 6.0), "height": tags.get("height", 2.0)}
        else:
            operation["tabs"] = {"count": 0, "width": 0.0, "height": 0.0}
        operation["compensation"] = "none"
    if kind in ("facing", "pocket", "profile", "drilling"):
        operation.setdefault("entry", "plunge")
        operation.setdefault("ramp_angle", 3)
        operation.setdefault("lead_in", 0)
        operation.setdefault("lead_out", 0)
        operation.pop("feed_ramp", None)
        if "tolerance" in operation and operation["tolerance"] is None:
            del operation["tolerance"]
        if isinstance(depth, dict) and depth.get("step_down") is None:
            depth.pop("step_down", None)
        selection = operation.get("selection")
        if isinstance(selection, dict):
            selection.pop("faces", None)
    else:
        # pause, tool_change, note : le contrat v1 n'admet ni dépendances ni pause_before sur une étape manuelle.
        operation.pop("depends_on", None)
        operation.pop("pause_before", None)
