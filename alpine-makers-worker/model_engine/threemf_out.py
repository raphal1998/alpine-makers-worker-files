"""Écrivain du fichier tranché ``worker_<execution_id>_sliced.gcode.3mf`` (spécification §6.4).

Conteneur OPC complet, en types simples (dicts, chaînes, tableaux numpy) : ``[Content_Types].xml``, ``_rels/.rels``,
``3D/3dmodel.model`` (+ ``3D/_rels/3dmodel.model.rels``, ``3D/Objects/object_<n>.model``), ``Metadata/plate_N.gcode``
+ ``.md5`` (hexadécimal majuscules, sans retour ligne), ``Metadata/slice_info.config``,
``Metadata/project_settings.config`` (JSON plat avec les clés Alpine), ``Metadata/model_settings.config`` (objets,
parts, plateaux avec les cinq clés de vignettes), ``Metadata/plate_N.json``, ``Metadata/filament_sequence.json`` et
les cinq PNG par plateau (STORED). Garanties : ``bambu_print_file.prepare_for_printer`` ne modifie rien
(``thumbnails_added`` 0, G-code à l'octet), ``app._inspect_3mf`` renvoie ``printable``, ``plates``, ``print_info``,
``modelable``, ``thumbnail`` ; ``desktop_model_studio.sliced_info`` lit ``prediction_s``, ``weight_g``, ``filaments``.

Seule la structure publique du format 3MF (Core 2015/02, extension Production) et les noms d'entrées lus par le site
sont implémentés ; aucun fichier d'un autre trancheur n'a été copié. Code Alpine Makers : numpy + bibliothèque standard
(Pillow par ``thumbnails``).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

import numpy as np

from . import VERSION

NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
NS_PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
NS_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_MODEL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
REL_THUMBNAIL = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"
REL_COVER_MIDDLE = "http://schemas.bambulab.com/package/2021/cover-thumbnail-middle"
REL_COVER_SMALL = "http://schemas.bambulab.com/package/2021/cover-thumbnail-small"
UUID_NAMESPACE = uuid.UUID("5a8f1d2e-4b7c-4a3e-9c1d-2f6e8b0a7c55")  # espace Alpine pour des p:UUID déterministes
IDENTITY_12 = "1 0 0 0 1 0 0 0 1 0 0 0"
IDENTITY_16 = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"
BED_SHORT = {"Textured PEI Plate": "textured_plate", "Cool Plate": "cool_plate", "High Temp Plate": "hot_plate",
             "Engineering Plate": "eng_plate"}
TIMELAPSE = {"none": 0, "traditional": 1, "smooth": 2}
MAX_FIRST_LAYER_TIME_S = 1e7 - 1     # au-delà, bambu_print_file remplace la valeur : on borne nous-mêmes
MAX_OBJECTS = 1024
MAX_EXTRUDERS = 16


class ThreeMFError(ValueError):
    """Entrée invalide pour l'écrivain 3MF (message en français)."""


# ================================================================================================ outils

_XML_INVALID = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F￾￿]")


def _text(value) -> str:
    """Texte sans caractères interdits en XML 1.0 (contrôles) : un nom ou un libellé ne doit jamais casser le fichier."""
    return _XML_INVALID.sub("", str(value if value is not None else ""))


def _attr(value) -> str:
    return _xml_escape(_text(value), {'"': "&quot;"})


def _int(value, what: str, default=None) -> int:
    """Entier d'une entrée (None → ``default``) ; ``ThreeMFError`` si non convertible."""
    if value is None:
        if default is None:
            raise ThreeMFError(f"fichier tranché : {what} manquant.")
        return default
    try:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError
        return int(value)
    except (TypeError, ValueError):
        raise ThreeMFError(f"fichier tranché : {what} invalide ({str(value)[:40]!r}).") from None


def _float(value, what: str, default=0.0) -> float:
    """Nombre fini d'une entrée (None → ``default``) ; ``ThreeMFError`` si non convertible, ``default`` si non fini."""
    if value is None:
        return float(default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ThreeMFError(f"fichier tranché : {what} invalide ({str(value)[:40]!r}).") from None
    return number if math.isfinite(number) else float(default)


def _fmt(value, decimals=3) -> str:
    """Nombre sans zéros inutiles (déterministe, jamais en notation scientifique)."""
    number = float(value)
    if not math.isfinite(number):
        number = 0.0
    text = f"{number:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("-0", "") else "0"


def _xy(points) -> list[str]:
    return [f"{_fmt(x)}x{_fmt(y)}" for x, y in points]


def md5_upper(data: bytes) -> str:
    """Empreinte MD5 hexadécimale en majuscules (format attendu par l'imprimante), sans retour ligne."""
    return hashlib.md5(data).hexdigest().upper()


def normalise_colour(value, default="#FFFFFF") -> str:
    """Couleur « #RRGGBB » en majuscules (« ffffff », « #FFFFFFFF », (r, g, b) acceptés) ; ``default`` sinon."""
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        try:
            return "#" + "".join(f"{int(max(0, min(255, round(float(c))))):02X}" for c in value[:3])
        except (TypeError, ValueError):
            return default
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?", str(value or "").strip())
    return f"#{match.group(1).upper()}" if match else default


def _puuid(*parts) -> str:
    return str(uuid.uuid5(UUID_NAMESPACE, "/".join(str(p) for p in parts)))


def _setting_text(value):
    """Valeur de réglage en chaîne pour project_settings.config (booléens « 1 »/« 0 », listes de chaînes)."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return _fmt(value, 6)
    if isinstance(value, (list, tuple)):
        return [_setting_text(v) if not isinstance(v, (list, tuple, dict)) else json.dumps(v) for v in value]
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value if value is not None else "")


# ================================================================================================ normalisation

def _norm_objects(objects) -> list[dict]:
    if not objects:
        raise ThreeMFError("fichier tranché : aucun objet.")
    if len(objects) > MAX_OBJECTS:
        raise ThreeMFError(f"fichier tranché : trop d'objets ({len(objects)} > {MAX_OBJECTS}).")
    result, seen = [], set()
    for index, item in enumerate(objects):
        get = item.get if isinstance(item, dict) else (lambda key, default=None, it=item: getattr(it, key, default))
        ident = _int(get("id"), f"identifiant de l'objet {index + 1}", index + 1)
        if ident < 1 or ident in seen:
            raise ThreeMFError(f"fichier tranché : identifiant d'objet invalide ou dupliqué ({ident}).")
        seen.add(ident)
        try:
            vertices = np.asarray(get("vertices"), dtype=np.float64).reshape(-1, 3)
            faces = np.asarray(get("faces"), dtype=np.int64).reshape(-1, 3)
        except (TypeError, ValueError):
            raise ThreeMFError(f"fichier tranché : objet {ident} avec des sommets ou des faces mal formés ((n, 3) attendu).") from None
        if len(vertices) < 3 or len(faces) < 1:
            raise ThreeMFError(f"fichier tranché : objet {ident} sans géométrie.")
        if faces.min() < 0 or faces.max() >= len(vertices):
            raise ThreeMFError(f"fichier tranché : objet {ident} avec des indices de faces hors des sommets.")
        if not np.isfinite(vertices).all():
            raise ThreeMFError(f"fichier tranché : objet {ident} avec des coordonnées non finies.")
        extruder = _int(get("extruder"), f"extrudeur de l'objet {ident}", 1) or 1
        if not 1 <= extruder <= MAX_EXTRUDERS:
            raise ThreeMFError(f"fichier tranché : extrudeur {extruder} hors de 1..{MAX_EXTRUDERS} (objet {ident}).")
        bbox = get("bbox")
        if bbox is None:
            lo, hi = vertices.min(axis=0), vertices.max(axis=0)
            bbox = (float(lo[0]), float(lo[1]), float(lo[2]), float(hi[0]), float(hi[1]), float(hi[2]))
        else:
            try:
                bbox = tuple(float(v) for v in bbox)
            except (TypeError, ValueError):
                raise ThreeMFError(f"fichier tranché : bbox de l'objet {ident} invalide (nombres attendus).") from None
            if len(bbox) == 4:
                bbox = (bbox[0], bbox[1], float(vertices[:, 2].min()), bbox[2], bbox[3], float(vertices[:, 2].max()))
            if len(bbox) != 6 or not all(math.isfinite(v) for v in bbox):
                raise ThreeMFError(f"fichier tranché : bbox de l'objet {ident} invalide (4 ou 6 valeurs finies attendues).")
        name = re.sub(r"\s+", " ", _text(get("name") or f"objet {ident}")).strip()[:120] or f"objet {ident}"
        result.append({"id": ident, "name": name, "vertices": vertices, "faces": faces, "extruder": extruder,
                       "bbox": bbox, "source_file": _text(get("source_file") or "")[:200]})
    return result


def _per_extruder(value, extruders) -> dict:
    """Quantité par extrudeur (dict {extrudeur: valeur}, liste indexée depuis l'extrudeur 1, ou scalaire)."""
    result = {int(e): 0.0 for e in extruders}
    if value is None:
        return result
    if isinstance(value, dict):
        for key, v in value.items():
            result[_int(key, f"extrudeur « {key} » des quantités de filament")] = max(0.0, _float(v, f"quantité de filament de l'extrudeur {key}"))
        return result
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            result[i + 1] = max(0.0, _float(v, f"quantité de filament de l'extrudeur {i + 1}"))
        return result
    first = int(extruders[0]) if extruders else 1
    result[first] = max(0.0, _float(value, "quantité de filament"))
    return result


def _norm_filaments(filaments, settings) -> list[dict]:
    result, seen = [], set()
    for index, item in enumerate(filaments or []):
        get = item.get if isinstance(item, dict) else (lambda key, default=None, it=item: getattr(it, key, default))
        ident = _int(get("id"), f"identifiant du filament {index + 1}", index + 1)
        if not 1 <= ident <= MAX_EXTRUDERS or ident in seen:
            raise ThreeMFError(f"fichier tranché : identifiant de filament invalide ou dupliqué ({ident}).")
        seen.add(ident)
        colour = normalise_colour(get("color", get("colour")))
        entry = {"id": ident, "type": _text(get("type") or settings.get("filament_type") or "PLA")[:40],
                 "color": colour, "settings_id": _text(get("settings_id") or settings.get("filament_settings_id") or "")[:80],
                 "tray_info_idx": _text(get("tray_info_idx") or "")[:20],
                 "density": _float(get("density") or settings.get("filament_density"), f"densité du filament {ident}", 1.24) or 1.24,
                 "vendor": _text(get("vendor") or settings.get("filament_vendor") or "Alpine Makers")[:60],
                 "used_g": max(0.0, _float(get("used_g"), f"used_g du filament {ident}")),
                 "used_m": max(0.0, _float(get("used_m"), f"used_m du filament {ident}"))}
        for key in ("nozzle_temperature", "nozzle_temperature_initial_layer", "filament_max_volumetric_speed",
                    "filament_cost", "filament_diameter", "cool_plate_temp", "cool_plate_temp_initial_layer",
                    "textured_plate_temp", "textured_plate_temp_initial_layer", "hot_plate_temp",
                    "hot_plate_temp_initial_layer", "eng_plate_temp", "eng_plate_temp_initial_layer"):
            value = get(key)
            entry[key] = value if value is not None else settings.get(key)
        result.append(entry)
    if not result:
        result.append({"id": 1, "type": str(settings.get("filament_type") or "PLA"),
                       "color": normalise_colour(settings.get("filament_colour")),
                       "settings_id": str(settings.get("filament_settings_id") or ""), "tray_info_idx": "",
                       "density": float(settings.get("filament_density") or 1.24),
                       "vendor": str(settings.get("filament_vendor") or "Alpine Makers"), "used_g": 0.0, "used_m": 0.0,
                       **{key: settings.get(key) for key in ("nozzle_temperature", "nozzle_temperature_initial_layer",
                                                            "filament_max_volumetric_speed", "filament_cost",
                                                            "filament_diameter")}})
    return sorted(result, key=lambda f: f["id"])


def _norm_plates(plates, objects, filaments) -> list[dict]:
    if not plates:
        raise ThreeMFError("fichier tranché : aucun plateau.")
    by_id = {o["id"]: o for o in objects}
    result, seen = [], set()
    for index, item in enumerate(plates):
        get = item.get if isinstance(item, dict) else (lambda key, default=None, it=item: getattr(it, key, default))
        number = _int(get("index"), f"index du plateau {index + 1}", index + 1)
        if number < 1 or number in seen:
            raise ThreeMFError(f"fichier tranché : index de plateau invalide ou dupliqué ({number}).")
        seen.add(number)
        gcode = get("gcode_text", get("gcode"))
        if gcode is None:
            raise ThreeMFError(f"fichier tranché : plateau {number} sans G-code.")
        gcode_bytes = gcode if isinstance(gcode, (bytes, bytearray)) else str(gcode).encode("utf-8")
        if not gcode_bytes:
            raise ThreeMFError(f"fichier tranché : plateau {number} avec un G-code vide.")
        object_ids = get("object_ids")
        if object_ids is None:
            ids = list(by_id)
        else:
            ids = []
            for raw in object_ids:
                ident = _int(raw, f"identifiant d'objet du plateau {number}")
                if ident not in ids:      # un objet n'apparaît qu'une fois par plateau
                    ids.append(ident)
        unknown = [i for i in ids if i not in by_id]
        if unknown:
            raise ThreeMFError(f"fichier tranché : plateau {number} référence des objets inconnus {unknown}.")
        if not ids:
            raise ThreeMFError(f"fichier tranché : plateau {number} sans objet.")
        stats = get("stats") or {}
        if not isinstance(stats, dict):
            raise ThreeMFError(f"fichier tranché : plateau {number} avec des statistiques invalides (dictionnaire attendu).")
        extruders = sorted({by_id[i]["extruder"] for i in ids} | {f["id"] for f in filaments})
        used_mm = _per_extruder(stats.get("filament_used_mm"), extruders)
        used_g = _per_extruder(stats.get("filament_used_g"), extruders)
        prediction = max(0.0, _float(stats.get("prediction_s"), f"prediction_s du plateau {number}"))
        first_layer = max(0.0, _float(stats.get("first_layer_time_s"), f"first_layer_time_s du plateau {number}"))
        first_layer = min(first_layer, MAX_FIRST_LAYER_TIME_S)
        weight = stats.get("weight_g")
        weight = _float(weight, f"weight_g du plateau {number}") if weight is not None else sum(used_g.values())
        max_z = stats.get("max_z_mm")
        max_z = _float(max_z, f"max_z_mm du plateau {number}") if max_z is not None else max(by_id[i]["bbox"][5] for i in ids)
        layers = _int(stats.get("layers"), f"layers du plateau {number}", 0)
        used_extruders = [e for e in extruders if used_g.get(e, 0) > 0 or used_mm.get(e, 0) > 0] or \
                         sorted({by_id[i]["extruder"] for i in ids})
        result.append({"index": number, "gcode": bytes(gcode_bytes), "object_ids": ids, "prediction_s": prediction,
                       "first_layer_time_s": first_layer, "weight_g": max(0.0, weight), "used_mm": used_mm,
                       "used_g": used_g, "layers": max(0, layers), "max_z_mm": max_z, "name": _text(get("name") or "")[:80],
                       "used_extruders": used_extruders, "support_used": bool(stats.get("support_used", False))})
    return sorted(result, key=lambda p: p["index"])


def _corners(value, what: str) -> list[tuple[float, float]]:
    """Polygone (liste de coins) d'une zone : (w, d), (x0, y0, x1, y1), coins (x, y) ou liste plate x0 y0 x1 y1… ; [] si vide."""
    if value is None:
        return []
    try:
        items = list(value)
    except TypeError:
        raise ThreeMFError(f"fichier tranché : {what} invalide (liste attendue).") from None
    if not items:
        return []
    try:
        if all(isinstance(v, (int, float)) for v in items):
            numbers = [float(v) for v in items]
            if len(numbers) == 2:
                return [(0.0, 0.0), (numbers[0], 0.0), (numbers[0], numbers[1]), (0.0, numbers[1])]
            if len(numbers) == 4:
                x0, y0, x1, y1 = numbers
                return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            if len(numbers) >= 6 and len(numbers) % 2 == 0:
                return list(zip(numbers[0::2], numbers[1::2]))
            raise ValueError
        corners = [(float(point[0]), float(point[1])) for point in items]
    except (TypeError, ValueError, IndexError):
        raise ThreeMFError(f"fichier tranché : {what} invalide ((largeur, profondeur), (x0, y0, x1, y1) ou coins (x, y) attendus).") from None
    if len(corners) < 3 or not all(math.isfinite(c) for point in corners for c in point):
        raise ThreeMFError(f"fichier tranché : {what} invalide (au moins trois coins finis attendus).")
    return corners


def _norm_machine(machine_info) -> dict:
    if machine_info is None:
        raise ThreeMFError("fichier tranché : machine_info manquant.")
    get = machine_info.get if isinstance(machine_info, dict) else (lambda key, default=None: getattr(machine_info, key, default))
    model_id = _text(get("model_id") or "").strip()
    if not model_id:
        raise ThreeMFError("fichier tranché : machine_info.model_id vide (l'imprimante refuse un fichier sans modèle).")
    model = _text(get("model") or "Bambu Lab P1S").strip()
    nozzle = _float(get("nozzle_mm"), "machine_info.nozzle_mm", 0.4) or 0.4
    area = _corners(get("printable_area"), "machine_info.printable_area") or [(0.0, 0.0), (256.0, 0.0), (256.0, 256.0), (0.0, 256.0)]
    exclude = _corners(get("bed_exclude_area"), "machine_info.bed_exclude_area")
    width = max(x for x, _ in area)
    depth = max(y for _, y in area)
    return {"model": model, "model_id": model_id, "preset_name": _text(get("preset_name") or f"{model} {_fmt(nozzle)} nozzle"),
            "nozzle_mm": nozzle, "printable_area": area,
            "printable_height": _float(get("printable_height"), "machine_info.printable_height", 256.0) or 256.0,
            "bed_exclude_area": exclude, "nozzle_type": _text(get("nozzle_type") or "stainless_steel"),
            "vendor": _text(get("vendor") or "Bambu Lab"), "width": width, "depth": depth}


# ================================================================================================ entrées XML / JSON

def _content_types() -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Types xmlns="{NS_TYPES}">\n'
            ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
            ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
            ' <Default Extension="png" ContentType="image/png"/>\n'
            ' <Default Extension="gcode" ContentType="text/x.gcode"/>\n'
            '</Types>\n')


def _root_rels(first_plate: int) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Relationships xmlns="{NS_RELS}">\n'
            f' <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="{REL_MODEL}"/>\n'
            f' <Relationship Target="/Metadata/plate_{first_plate}.png" Id="rel-2" Type="{REL_THUMBNAIL}"/>\n'
            f' <Relationship Target="/Metadata/plate_{first_plate}.png" Id="rel-4" Type="{REL_COVER_MIDDLE}"/>\n'
            f' <Relationship Target="/Metadata/plate_{first_plate}_small.png" Id="rel-5" Type="{REL_COVER_SMALL}"/>\n'
            '</Relationships>\n')


def _mesh_id(obj_id: int) -> int:
    return 2 * obj_id - 1


def _build_id(obj_id: int) -> int:
    return 2 * obj_id


def _object_model(obj: dict) -> str:
    """``3D/Objects/object_<n>.model`` : le maillage seul (sommets en mm dans le repère du plateau)."""
    mesh_id = _mesh_id(obj["id"])
    v = obj["vertices"]
    f = obj["faces"]
    vertex_lines = "\n".join(f'     <vertex x="{_fmt(x, 6)}" y="{_fmt(y, 6)}" z="{_fmt(z, 6)}"/>' for x, y, z in v.tolist())
    triangle_lines = "\n".join(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in f.tolist())
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<model unit="millimeter" xml:lang="en-US" xmlns="{NS_CORE}" xmlns:p="{NS_PROD}">\n'
            ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
            ' <resources>\n'
            f'  <object id="{mesh_id}" p:UUID="{_puuid("mesh", obj["id"])}" type="model">\n'
            '   <mesh>\n    <vertices>\n' + vertex_lines + '\n    </vertices>\n    <triangles>\n' + triangle_lines +
            '\n    </triangles>\n   </mesh>\n  </object>\n </resources>\n <build/>\n</model>\n')


def _root_model(objects: list[dict]) -> str:
    """``3D/3dmodel.model`` : un objet de construction par maillage (composant vers ``3D/Objects``)."""
    resources, items = [], []
    for obj in objects:
        mesh_id, build_id = _mesh_id(obj["id"]), _build_id(obj["id"])
        resources.append(f'  <object id="{build_id}" p:UUID="{_puuid("object", obj["id"])}" name="{_attr(obj["name"])}" type="model">\n'
                         '   <components>\n'
                         f'    <component p:path="/3D/Objects/object_{mesh_id}.model" objectid="{mesh_id}" '
                         f'p:UUID="{_puuid("component", obj["id"])}" transform="{IDENTITY_12}"/>\n'
                         '   </components>\n  </object>')
        items.append(f'  <item objectid="{build_id}" p:UUID="{_puuid("item", obj["id"])}" transform="{IDENTITY_12}" printable="1"/>')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<model unit="millimeter" xml:lang="en-US" xmlns="{NS_CORE}" xmlns:p="{NS_PROD}" requiredextensions="p">\n'
            f' <metadata name="Application">Alpine Model Engine {VERSION}</metadata>\n'
            ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
            f' <metadata name="CreationDate">{time.strftime("%Y-%m-%d")}</metadata>\n'
            ' <resources>\n' + "\n".join(resources) + '\n </resources>\n'
            f' <build p:UUID="{_puuid("build")}">\n' + "\n".join(items) + '\n </build>\n</model>\n')


def _model_rels(objects: list[dict]) -> str:
    rels = "\n".join(f' <Relationship Target="/3D/Objects/object_{_mesh_id(o["id"])}.model" Id="rel-obj-{_mesh_id(o["id"])}" '
                     f'Type="{REL_MODEL}"/>' for o in objects)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Relationships xmlns="{NS_RELS}">\n' + rels + '\n</Relationships>\n')


def _model_settings(objects: list[dict], plates: list[dict], settings: dict) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<config>']
    for obj in objects:
        mesh_id, build_id = _mesh_id(obj["id"]), _build_id(obj["id"])
        x0, y0, z0, x1, y1, z1 = obj["bbox"]
        lines += [f'  <object id="{build_id}">',
                  f'    <metadata key="name" value="{_attr(obj["name"])}"/>',
                  f'    <metadata key="extruder" value="{obj["extruder"]}"/>',
                  f'    <part id="{mesh_id}" subtype="normal_part">',
                  f'      <metadata key="name" value="{_attr(obj["name"])}"/>',
                  f'      <metadata key="matrix" value="{IDENTITY_16}"/>',
                  f'      <metadata key="source_file" value="{_attr(obj["source_file"])}"/>',
                  '      <metadata key="source_object_id" value="0"/>',
                  '      <metadata key="source_volume_id" value="0"/>',
                  f'      <metadata key="source_offset_x" value="{_fmt((x0 + x1) / 2)}"/>',
                  f'      <metadata key="source_offset_y" value="{_fmt((y0 + y1) / 2)}"/>',
                  f'      <metadata key="source_offset_z" value="{_fmt((z0 + z1) / 2)}"/>',
                  f'      <metadata key="extruder" value="{obj["extruder"]}"/>',
                  f'      <mesh_stat face_count="{len(obj["faces"])}" edges_fixed="0" degenerate_facets="0" '
                  'facets_removed="0" facets_reversed="0" backwards_edges="0"/>',
                  '    </part>', '  </object>']
    for plate in plates:
        n = plate["index"]
        lines += ['  <plate>',
                  f'    <metadata key="plater_id" value="{n}"/>',
                  f'    <metadata key="plater_name" value="{_attr(plate["name"])}"/>',
                  '    <metadata key="locked" value="false"/>',
                  f'    <metadata key="gcode_file" value="Metadata/plate_{n}.gcode"/>',
                  f'    <metadata key="thumbnail_file" value="Metadata/plate_{n}.png"/>',
                  f'    <metadata key="thumbnail_no_light_file" value="Metadata/plate_no_light_{n}.png"/>',
                  f'    <metadata key="top_file" value="Metadata/top_{n}.png"/>',
                  f'    <metadata key="pick_file" value="Metadata/pick_{n}.png"/>',
                  f'    <metadata key="pattern_bbox_file" value="Metadata/plate_{n}.json"/>']
        for obj_id in plate["object_ids"]:
            lines += ['    <model_instance>',
                      f'      <metadata key="object_id" value="{_build_id(obj_id)}"/>',
                      '      <metadata key="instance_id" value="0"/>',
                      f'      <metadata key="identify_id" value="{obj_id}"/>',
                      '    </model_instance>']
        lines.append('  </plate>')
    lines.append('  <assemble>')
    for obj in objects:
        lines.append(f'   <assemble_item object_id="{_build_id(obj["id"])}" instance_id="0" transform="{IDENTITY_12}" offset="0 0 0"/>')
    lines += ['  </assemble>', '</config>', '']
    return "\n".join(lines)


def _client_version() -> str:
    """Version du moteur au format à quatre groupes (« 1.0.0 » → « 01.00.00.00 »)."""
    parts = [int(p) for p in re.findall(r"\d+", VERSION)][:4]
    while len(parts) < 4:
        parts.append(0)
    return ".".join(f"{p:02d}" for p in parts)


def _slice_info(plates: list[dict], objects: list[dict], filaments: list[dict], machine: dict, settings: dict) -> str:
    by_id = {o["id"]: o for o in objects}
    timelapse = TIMELAPSE.get(str(settings.get("timelapse_type") or "none"), 0)
    nozzle = _fmt(machine["nozzle_mm"])
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<config>', '  <header>',
             '    <header_item key="X-BBL-Client-Type" value="slicer"/>',
             f'    <header_item key="X-BBL-Client-Version" value="{_client_version()}"/>',
             f'    <header_item key="AlpineModelEngine-Version" value="{VERSION}"/>',
             '  </header>']
    for plate in plates:
        by_ext = {f["id"]: f for f in filaments}
        lines += ['  <plate>',
                  f'    <metadata key="index" value="{plate["index"]}"/>',
                  f'    <metadata key="printer_model_id" value="{_attr(machine["model_id"])}"/>',
                  f'    <metadata key="nozzle_diameters" value="{nozzle}"/>',
                  f'    <metadata key="timelapse_type" value="{timelapse}"/>',
                  f'    <metadata key="prediction" value="{int(round(plate["prediction_s"]))}"/>',
                  f'    <metadata key="weight" value="{plate["weight_g"]:.2f}"/>',
                  f'    <metadata key="first_layer_time" value="{plate["first_layer_time_s"]:.2f}"/>',
                  '    <metadata key="outside" value="false"/>',
                  f'    <metadata key="support_used" value="{"true" if plate["support_used"] else "false"}"/>',
                  f'    <metadata key="label_object_enabled" value="{"true" if settings.get("label_objects", True) else "false"}"/>']
        for obj_id in plate["object_ids"]:
            lines.append(f'    <object identify_id="{obj_id}" name="{_attr(by_id[obj_id]["name"])}" skipped="false"/>')
        for ext in plate["used_extruders"]:
            f = by_ext.get(ext) or {"id": ext, "type": str(settings.get("filament_type") or "PLA"),
                                    "color": normalise_colour(settings.get("filament_colour")), "tray_info_idx": ""}
            used_mm = plate["used_mm"].get(ext, 0.0)
            used_g = plate["used_g"].get(ext, 0.0)
            lines.append(f'    <filament id="{ext}" tray_info_idx="{_attr(f["tray_info_idx"])}" type="{_attr(f["type"])}" '
                         f'color="{f["color"]}" used_m="{used_mm / 1000.0:.2f}" used_g="{used_g:.2f}" nozzle_diameter="{nozzle}"/>')
        lines.append(f'    <nozzle id="1" diameter="{nozzle}" type="{_attr(machine["nozzle_type"])}"/>')
        lines.append('    <layer_filament_lists>')
        ids = " ".join(str(e) for e in plate["used_extruders"])
        lines.append(f'      <layer_filament_list layer_from="1" layer_to="{max(plate["layers"], 1)}" filament_ids="{ids}"/>')
        lines.append('    </layer_filament_lists>')
        lines.append('  </plate>')
    lines += ['</config>', '']
    return "\n".join(lines)


def _plate_json(plate: dict, objects: list[dict], filaments: list[dict], machine: dict, settings: dict) -> dict:
    by_id = {o["id"]: o for o in objects}
    by_ext = {f["id"]: f for f in filaments}
    layer = float(settings.get("layer_height") or 0.2)
    boxes = []
    for obj_id in plate["object_ids"]:
        obj = by_id[obj_id]
        x0, y0, _, x1, y1, _ = obj["bbox"]
        boxes.append({"area": round((x1 - x0) * (y1 - y0), 3), "bbox": [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)],
                      "id": obj_id, "layer_height": layer, "name": obj["name"]})
    all_box = [min(b["bbox"][0] for b in boxes), min(b["bbox"][1] for b in boxes),
               max(b["bbox"][2] for b in boxes), max(b["bbox"][3] for b in boxes)]
    extruders = plate["used_extruders"]
    bed_type = str(settings.get("bed_type_short") or BED_SHORT.get(str(settings.get("curr_bed_type") or settings.get("bed_type") or ""), "textured_plate"))
    return {"bbox_all": all_box, "bbox_objects": boxes, "bed_type": bed_type,
            "filament_colors": [by_ext[e]["color"] if e in by_ext else normalise_colour(None) for e in extruders],
            "filament_ids": [int(e) for e in extruders], "first_extruder": int(extruders[0]) if extruders else 1,
            "first_layer_time": round(plate["first_layer_time_s"], 3), "is_seq_print": str(settings.get("print_sequence") or "") == "by_object",
            "nozzle_diameter": machine["nozzle_mm"], "version": 2}


def _filament_sequence(plates: list[dict]) -> dict:
    """Ordre des filaments par plateau (format Alpine : identifiants d'extrudeur dans l'ordre d'utilisation)."""
    return {"version": 1, "plates": [{"index": p["index"], "filament_ids": [int(e) for e in p["used_extruders"]]} for p in plates]}


def _project_settings(settings: dict, machine: dict, filaments: list[dict], plates: list[dict]) -> dict:
    """JSON plat : tout le ``Settings`` résolu (chaînes) puis les clés obligatoires du contrat du fichier tranché."""
    out = {}
    for key, value in sorted(settings.items()):
        if value is None or not re.fullmatch(r"[A-Za-z0-9_]+", str(key)):
            continue
        out[str(key)] = _setting_text(value)

    def per_filament(key, default=""):
        values = []
        for f in filaments:
            value = f.get(key)
            if value is None:
                value = settings.get(key, default)
            values.append(_setting_text(value) if not isinstance(value, (list, tuple)) else _setting_text(value[0] if value else default))
        return values

    bed_type = str(settings.get("curr_bed_type") or settings.get("bed_type") or "Textured PEI Plate")
    nozzle = _fmt(machine["nozzle_mm"])
    out.update({
        "curr_bed_type": bed_type,
        "printer_model": machine["model"],
        "printer_settings_id": machine["preset_name"],
        "print_settings_id": str(settings.get("print_settings_id") or "Alpine"),
        "filament_settings_id": [f["settings_id"] or str(settings.get("filament_settings_id") or "") for f in filaments],
        "nozzle_diameter": [nozzle],
        "printer_variant": nozzle,
        "nozzle_type": machine["nozzle_type"],
        "layer_height": _setting_text(settings.get("layer_height", 0.2)),
        "initial_layer_print_height": _setting_text(settings.get("initial_layer_print_height", settings.get("layer_height", 0.2))),
        "printable_area": _xy(machine["printable_area"]),
        "printable_height": _fmt(machine["printable_height"]),
        "bed_exclude_area": _xy(machine["bed_exclude_area"]),
        "filament_type": [f["type"] for f in filaments],
        "filament_colour": [f["color"] for f in filaments],
        "filament_ids": [f["tray_info_idx"] for f in filaments],
        "filament_vendor": [f["vendor"] for f in filaments],
        "filament_density": [_fmt(f["density"], 3) for f in filaments],
        "nozzle_temperature": per_filament("nozzle_temperature", 210),
        "nozzle_temperature_initial_layer": per_filament("nozzle_temperature_initial_layer", 215),
        "filament_max_volumetric_speed": per_filament("filament_max_volumetric_speed", 15),
        "filament_cost": per_filament("filament_cost", 20),
        "filament_diameter": per_filament("filament_diameter", 1.75),
        "gcode_flavor": "marlin",
        "version": VERSION,
        "from": "project",
        "generator": f"Alpine Model Engine {VERSION}",
        "wall_loops": _setting_text(settings.get("wall_loops", 2)),
        "sparse_infill_density": _percent_text(settings.get("sparse_infill_density", 15)),
        "sparse_infill_pattern": str(settings.get("sparse_infill_pattern") or "gyroid"),
        "enable_support": _setting_text(bool(settings.get("enable_support", False))),
        "support_type": str(settings.get("support_type") or "normal(auto)"),
        "brim_type": str(settings.get("brim_type") or "auto_brim"),
        "enable_prime_tower": _setting_text(bool(settings.get("prime_tower", settings.get("enable_prime_tower", False)))),
        "print_sequence": str(settings.get("print_sequence") or "by_layer"),
        "plate_count": str(len(plates)),
    })
    for key in ("cool_plate_temp", "cool_plate_temp_initial_layer", "textured_plate_temp", "textured_plate_temp_initial_layer",
                "hot_plate_temp", "hot_plate_temp_initial_layer", "eng_plate_temp", "eng_plate_temp_initial_layer"):
        out[key] = per_filament(key, settings.get(key, 0))
    return out


def _percent_text(value) -> str:
    text = str(value).strip()
    return text if text.endswith("%") else f"{_fmt(float(text), 3)}%"


# ================================================================================================ écriture

def build_entries(plates, objects, settings, machine_info, filaments, *, thumbnails=True) -> dict:
    """Toutes les entrées du conteneur ``{nom: bytes}`` (sans écrire de fichier). Voir ``write_gcode_3mf``."""
    settings = dict(settings or {})
    objs = _norm_objects(objects)
    fils = _norm_filaments(filaments, settings)
    plts = _norm_plates(plates, objs, fils)
    machine = _norm_machine(machine_info)
    by_id = {o["id"]: o for o in objs}
    by_ext = {f["id"]: f for f in fils}
    entries: dict[str, bytes] = {}
    entries["[Content_Types].xml"] = _content_types().encode("utf-8")
    entries["_rels/.rels"] = _root_rels(plts[0]["index"]).encode("utf-8")
    entries["3D/3dmodel.model"] = _root_model(objs).encode("utf-8")
    entries["3D/_rels/3dmodel.model.rels"] = _model_rels(objs).encode("utf-8")
    for obj in objs:
        entries[f"3D/Objects/object_{_mesh_id(obj['id'])}.model"] = _object_model(obj).encode("utf-8")
    entries["Metadata/model_settings.config"] = _model_settings(objs, plts, settings).encode("utf-8")
    entries["Metadata/slice_info.config"] = _slice_info(plts, objs, fils, machine, settings).encode("utf-8")
    entries["Metadata/project_settings.config"] = json.dumps(_project_settings(settings, machine, fils, plts), indent=4,
                                                             ensure_ascii=False).encode("utf-8")
    entries["Metadata/filament_sequence.json"] = json.dumps(_filament_sequence(plts), indent=2).encode("utf-8")
    exclude = machine["bed_exclude_area"]
    exclude_box = None
    if exclude:
        exclude_box = (min(x for x, _ in exclude), min(y for _, y in exclude), max(x for x, _ in exclude), max(y for _, y in exclude))
    for plate in plts:
        n = plate["index"]
        entries[f"Metadata/plate_{n}.gcode"] = plate["gcode"]
        entries[f"Metadata/plate_{n}.gcode.md5"] = md5_upper(plate["gcode"]).encode("ascii")
        entries[f"Metadata/plate_{n}.json"] = json.dumps(_plate_json(plate, objs, fils, machine, settings), indent=2).encode("utf-8")
        if thumbnails:
            from . import thumbnails as thumbs
            scene = [{"id": i, "vertices": by_id[i]["vertices"], "faces": by_id[i]["faces"],
                      "color": by_ext[by_id[i]["extruder"]]["color"] if by_id[i]["extruder"] in by_ext else None}
                     for i in plate["object_ids"]]
            pngs = thumbs.thumbnail_pngs(scene, bed_mm=(machine["width"], machine["depth"], machine["printable_height"]),
                                         exclude_mm=exclude_box)
        else:
            pngs = _blank_pngs()
        entries[f"Metadata/plate_{n}.png"] = pngs["plate"]
        entries[f"Metadata/plate_{n}_small.png"] = pngs["small"]
        entries[f"Metadata/plate_no_light_{n}.png"] = pngs["no_light"]
        entries[f"Metadata/top_{n}.png"] = pngs["top"]
        entries[f"Metadata/pick_{n}.png"] = pngs["pick"]
    return entries


def _blank_pngs() -> dict:
    """Vignettes transparentes (quand le rendu est désactivé) : les cinq entrées existent quand même."""
    import io

    from PIL import Image
    out = {}
    for name, size in (("plate", 512), ("small", 128), ("no_light", 512), ("top", 512), ("pick", 512)):
        buffer = io.BytesIO()
        Image.new("RGBA", (size, size), (0, 0, 0, 0)).save(buffer, format="PNG")
        out[name] = buffer.getvalue()
    return out


def write_gcode_3mf(path, plates, objects, settings, machine_info, filaments, *, thumbnails=True, timestamp=None) -> Path:
    """Écrit le fichier tranché complet et renvoie son chemin (écriture atomique : fichier temporaire puis remplacement).

    Entrées en types simples :

    * ``plates`` : ``[{index, gcode_text (str | bytes), stats, object_ids, name?}]`` avec ``stats = {prediction_s,
      first_layer_time_s, weight_g, filament_used_mm, filament_used_g, layers, max_z_mm, support_used?}`` ;
      ``filament_used_mm``/``_g`` = ``{extrudeur: valeur}`` (ou liste depuis l'extrudeur 1, ou scalaire) ;
      ``object_ids`` absent = tous les objets.
    * ``objects`` : ``[{id (≥ 1, unique), name, vertices (n, 3) mm dans le repère du plateau, faces (m, 3), extruder
      (1..16), bbox (x0, y0, z0, x1, y1, z1) ou (x0, y0, x1, y1), source_file?}]``.
    * ``settings`` : le dict résolu de ``profiles.process.resolve`` (tout est recopié dans ``project_settings.config``).
    * ``machine_info`` : ``{model, model_id (obligatoire, « C12 »), preset_name, nozzle_mm, printable_area ((w, d) ou
      quatre coins), printable_height, bed_exclude_area ((x0, y0, x1, y1), quatre coins ou None), nozzle_type?}``.
    * ``filaments`` : ``[{id (extrudeur), type, color « #RRGGBB », settings_id, tray_info_idx, density, used_g, used_m,
      nozzle_temperature?, nozzle_temperature_initial_layer?, …}]`` ; vide = un filament déduit des réglages.

    ``thumbnails=False`` écrit des PNG transparents (tests rapides). ``timestamp`` fixe la date des entrées ZIP.
    """
    path = Path(path)
    entries = build_entries(plates, objects, settings, machine_info, filaments, thumbnails=thumbnails)
    stamp = time.localtime(timestamp if timestamp is not None else time.time())[:6]
    if stamp[0] < 1980:
        stamp = (1980, 1, 1, 0, 0, 0)   # le format ZIP ne date rien avant 1980
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for name, data in entries.items():
                info = zipfile.ZipInfo(name, date_time=stamp)
                info.compress_type = zipfile.ZIP_STORED if name.endswith(".png") else zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return path


__all__ = ["ThreeMFError", "build_entries", "md5_upper", "normalise_colour", "write_gcode_3mf"]
