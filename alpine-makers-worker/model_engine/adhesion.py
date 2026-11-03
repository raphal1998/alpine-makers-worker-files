"""Adhérence au plateau (§5.6 de docs/MODEL_ENGINE_ARCHITECTURE.md) : brim et jupe.

Brim : boucles concentriques obtenues par dilatations successives (``support.dilate``) de la matière de la couche 1
(objets ∪ supports), à partir de ``brim_object_gap + w/2`` puis tous les ``w`` jusqu'à ``brim_width`` ; les boucles
d'objets voisins fusionnent d'elles-mêmes (décalage d'une union). Types : ``outer_only`` (boucles extérieures, CCW),
``inner_only`` (boucles dans les trous, CW), ``outer_and_inner``, ``auto_brim`` (largeur calculée par objet, voir
``auto_brim_width_mm`` ; une boucle large est coupée autour d'un voisin dont le brim est plus étroit ou absent, pour ne
jamais recouvrir ses boucles ni son jeu), ``brim_ears`` (disques de rayon ``brim_width`` aux coins convexes d'angle
intérieur < ``brim_ears_max_angle``, remplis de boucles concentriques), ``no_brim``.
Jupe : ``skirt_loops`` boucles autour de l'enveloppe convexe de tout (matière des couches couvertes + brim) à
``skirt_distance``, sur ``skirt_height`` couches. Tout est découpé au plateau rétréci d'une demi-largeur de ligne
(``bed_bbox_um``) et hors de la zone exclue éventuelle (``exclude_bbox_um``, élargie d'une demi-ligne).
Sortie : ``layer.adhesion`` = liste ``(feature, Polyline, line_width_um)`` ; une boucle fermée est donnée comme
polyligne dont le dernier point répète le premier (une boucle coupée par le bord du plateau devient ouverte).
"""
from __future__ import annotations

import math

import numpy as np

from .geometry import SCALE
from .geometry.boolean import difference, union, union_many
from .geometry.lines import clip_segments, connect_segments, segments_of_ring
from .geometry.offset import offset
from .geometry.poly import INT, area_mm2, as_ring, bbox, convex_hull, is_ccw, reverse, simplify
from .support import dilate

DEFAULTS = {
    "brim_type": "auto_brim",
    "brim_width": 5.0,
    "brim_object_gap": 0.1,
    "brim_ears_max_angle": 110.0,
    "brim_ears_detection_length": 1.0,
    "skirt_loops": 0,
    "skirt_distance": 2.0,
    "skirt_height": 1,
    "outer_wall_line_width": 0.42,
}
BRIM_TYPES = ("no_brim", "outer_only", "inner_only", "outer_and_inner", "auto_brim", "brim_ears")
AUTO_SMALL_CONTACT_MM2 = 30.0     # aire de contact en dessous de laquelle un brim est toujours ajouté
AUTO_SLENDER_RATIO = 2.0          # hauteur / √(aire de contact) au-delà duquel la pièce est jugée élancée
AUTO_MIN_WIDTH_MM = 2.0
EAR_SEGMENTS = 48


def _get(settings, key):
    value = settings.get(key) if settings else None
    return DEFAULTS[key] if value is None else value


def _um(value_mm) -> int:
    return int(round(float(value_mm) * SCALE))


def _line_width_um(settings) -> int:
    w = _um(_get(settings, "outer_wall_line_width"))
    return w if w > 0 else _um(DEFAULTS["outer_wall_line_width"])   # largeur nulle : défaut du schéma


def _rect(x0, y0, x1, y1) -> np.ndarray:
    return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=INT)


def _box(bbox_um, grow_um: int):
    """Boîte entière ``(x0, y0, x1, y1)`` agrandie de ``grow`` (négatif = rétrécie) ; None si elle disparaît."""
    x0, y0, x1, y1 = (int(round(float(v))) for v in bbox_um)
    x0, x1 = min(x0, x1) - grow_um, max(x0, x1) + grow_um
    y0, y1 = min(y0, y1) - grow_um, max(y0, y1) + grow_um
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _bed_paths(bed_bbox_um, margin_um: int, exclude_bbox_um=None):
    """Zone libre du plateau (Paths) : plateau rétréci de ``margin`` moins la zone exclue élargie de ``margin`` ;
    None si aucun plateau n'est donné."""
    if bed_bbox_um is None:
        return None
    box = _box(bed_bbox_um, -margin_um)
    if box is None:
        return []
    paths = [_rect(*box)]
    if exclude_bbox_um is not None:
        ex = _box(exclude_bbox_um, margin_um)
        if ex is not None:
            paths.append(reverse(_rect(*ex)))
    return paths


def _closed(ring: np.ndarray) -> np.ndarray:
    return np.vstack([ring, ring[:1]])


def _boxes_disjoint(a, b) -> bool:
    return a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1]


def _free_zone(bed, blocked, fallback_box):
    """Zone libre = plateau (ou une grande boîte autour de ``fallback_box``) moins les zones bloquées (Paths propre)."""
    if blocked is None:
        return bed
    base = bed
    if base is None:
        if fallback_box is None:
            return None
        x0, y0, x1, y1 = fallback_box
        pad = max(1000, x1 - x0, y1 - y0)
        base = [_rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad)]
    return list(base) + [reverse(r) for r in blocked]


def _clip_rings(rings, free_paths, blocked=None) -> list:
    """Boucles → polylignes dans la zone libre : fermées si entièrement dedans, sinon découpées (ouvertes).
    ``blocked`` : zone bloquée (Paths propre) par boucle, ou None (liste alignée sur ``rings``)."""
    out = []
    free_box = bbox(free_paths) if free_paths else None
    exclude_boxes = [bbox(r) for r in free_paths if not is_ccw(r)] if free_paths else []
    zones = {}
    for i, ring in enumerate(rings):
        ring = as_ring(ring)
        if len(ring) < 3:
            continue
        block = blocked[i] if blocked is not None else None
        if block is None:
            free = free_paths
            if free is None:
                out.append(_closed(ring))
                continue
            if not free:
                continue
            rb = bbox(ring)
            if (rb[0] >= free_box[0] and rb[1] >= free_box[1] and rb[2] <= free_box[2] and rb[3] <= free_box[3]
                    and all(_boxes_disjoint(rb, ex) for ex in exclude_boxes)):
                out.append(_closed(ring))     # entièrement dans le plateau, loin de la zone exclue
                continue
        else:
            free = zones.get(id(block))
            if free is None:
                free = _free_zone(free_paths, block, bbox(list(block) + [ring]))
                zones[id(block)] = free
            if free is None:
                out.append(_closed(ring))
                continue
            if not free:
                continue
        kept = clip_segments(segments_of_ring(ring), free)
        if not len(kept):
            continue
        for pl in connect_segments(kept, 1):
            if len(pl) >= 2:
                out.append(pl)
    return out


def auto_brim_width_mm(contact_area_mm2: float, height_mm: float, brim_width_mm: float) -> float:
    """Règle du brim automatique : contact < 30 mm² → largeur réglée ; pièce élancée (hauteur / √aire > 2) →
    2 mm par unité d'élancement, entre 2 mm et la largeur réglée ; sinon 0 (pas de brim)."""
    if brim_width_mm <= 0:
        return 0.0
    area = max(0.0, float(contact_area_mm2))
    if area < AUTO_SMALL_CONTACT_MM2:
        return float(brim_width_mm)
    ratio = float(height_mm) / math.sqrt(area)
    if ratio > AUTO_SLENDER_RATIO:
        return float(min(brim_width_mm, max(AUTO_MIN_WIDTH_MM, AUTO_MIN_WIDTH_MM * ratio)))
    return 0.0


def _loop_count(width_um: int, line_w: int) -> int:
    return 0 if width_um <= 0 else max(1, int(round(width_um / line_w)))


def _concentric(base, gap_um: int, line_w: int, count: int) -> list:
    """Décalages successifs de ``base`` : liste (par boucle) de Paths propres."""
    shapes = []
    current = base
    for k in range(count):
        current = dilate(current, gap_um + line_w // 2 if k == 0 else line_w)
        if not current:
            break
        shapes.append(current)
    return shapes


def brim_ear_centers(paths, max_angle_deg: float, detection_length_um: int) -> list:
    """Sommets convexes des extérieurs (angle intérieur < max) après simplification à la longueur de détection."""
    centers = []
    cos_limit = math.cos(math.radians(max_angle_deg))
    for ring in paths:
        ring = as_ring(ring)
        if len(ring) < 3 or not is_ccw(ring):
            continue
        r = simplify(ring, detection_length_um) if detection_length_um > 0 else ring
        p = r.astype(np.float64)
        a = np.roll(p, 1, axis=0) - p
        b = np.roll(p, -1, axis=0) - p
        la, lb = np.hypot(a[:, 0], a[:, 1]), np.hypot(b[:, 0], b[:, 1])
        ok = (la > 0) & (lb > 0)
        cos_angle = np.where(ok, (a * b).sum(axis=1) / np.where(ok, la * lb, 1.0), -1.0)
        convex = (a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]) < 0   # virage à gauche du contour CCW
        for i in np.nonzero(convex & (cos_angle > cos_limit))[0].tolist():
            centers.append((int(r[i, 0]), int(r[i, 1])))
    return centers


def _disc(cx, cy, radius_um: int) -> np.ndarray:
    t = np.linspace(0.0, 2.0 * math.pi, EAR_SEGMENTS, endpoint=False)
    return np.rint(np.column_stack([cx + radius_um * np.cos(t), cy + radius_um * np.sin(t)])).astype(INT)


def _ears(material, gap_um: int, line_w: int, width_um: int, max_angle_deg: float, detection_um: int) -> list:
    """Oreilles : disques aux coins convexes, moins la pièce élargie du jeu, remplis de boucles concentriques."""
    centers = brim_ear_centers(material, max_angle_deg, detection_um)
    if not centers or width_um <= 0:
        return []
    discs = [_disc(cx, cy, width_um + gap_um) for cx, cy in centers]
    area = difference(union(discs), dilate(material, gap_um))
    rings = []
    current = area
    for k in range(_loop_count(width_um, line_w) + 1):
        current = offset(current, -(line_w // 2) if k == 0 else -line_w, join="miter", resolve=False)
        if not current:
            break
        rings.extend(current)
    return rings


def _select(shapes, kind: str) -> list:
    rings = []
    for paths in shapes:
        for ring in paths:
            if kind == "outer_and_inner" or (kind == "outer_only" and is_ccw(ring)) or (kind == "inner_only" and not is_ccw(ring)):
                rings.append(ring)
    return rings


def _brim(layer1, settings, support_paths, object_heights_um) -> tuple:
    """``(rings, extent, blocked)`` : boucles fermées non découpées, emprise extérieure (Paths) pour placer la jupe,
    et zone bloquée par boucle (Paths propre ou None, brim automatique seulement)."""
    brim_type = str(_get(settings, "brim_type")).lower()
    line_w = _line_width_um(settings)
    width = _um(_get(settings, "brim_width"))
    gap = max(0, _um(_get(settings, "brim_object_gap")))
    objects = {oid: list(p) for oid, p in layer1.regions.items() if p and int(oid) > 0}
    material = []
    for p in objects.values():
        material.extend(p)
    if support_paths:
        material.extend(support_paths)
    if not material or brim_type not in BRIM_TYPES or brim_type == "no_brim":
        return [], (union(material) if material else []), None
    material = union(material)
    if brim_type == "brim_ears":
        rings = _ears(material, gap, line_w, width, float(_get(settings, "brim_ears_max_angle")),
                      _um(_get(settings, "brim_ears_detection_length")))
        return rings, (union(material + rings) if rings else material), None
    if brim_type == "auto_brim":
        return _auto_brim(layer1, objects, support_paths, material, gap, line_w, width, object_heights_um or {})
    shapes = _concentric(material, gap, line_w, _loop_count(width, line_w))
    # Emprise : dernière boucle extérieure ; sans boucle extérieure (inner_only), la matière elle-même.
    extent = shapes[-1] if shapes and brim_type != "inner_only" else material
    return _select(shapes, brim_type), extent, None


def brim_rings(layer1, settings, support_paths=None, object_heights_um=None) -> tuple:
    """Boucles de brim de la couche 1 → ``(rings, extent)`` : rings (fermés, non découpés) et l'emprise
    extérieure (Paths) servant à placer la jupe. ``object_heights_um`` : hauteur de chaque objet (brim automatique ;
    sans elle, la hauteur de la couche 1 est prise)."""
    rings, extent, _ = _brim(layer1, settings, support_paths, object_heights_um)
    return rings, extent


def _auto_brim(layer1, objects, support_paths, material, gap, line_w, width_um, heights) -> tuple:
    """Brim automatique : largeur par objet (``auto_brim_width_mm``), supports toujours à la largeur réglée,
    boucles extérieures seulement, fusion des brims qui se touchent. Au niveau k, une boucle est bloquée (découpée)
    par la bande de brim des groupes arrêtés avant k (dernière boucle élargie de w) et par le jeu des objets sans
    brim (élargis de gap + w/2) : aucune boucle ne recouvre une autre ni ne colle à un voisin."""
    groups, no_brim = [], []
    for oid, paths in objects.items():
        area = sum(area_mm2(r) for r in paths)
        height_mm = heights.get(oid, layer1.z_um) / SCALE
        w = auto_brim_width_mm(area, height_mm, width_um / SCALE)
        if w > 0:
            groups.append((paths, _loop_count(_um(w), line_w)))
        else:
            no_brim.extend(paths)
    if support_paths:
        groups.append((list(support_paths), _loop_count(width_um, line_w)))
    if not groups:
        return [], material, None
    max_loops = max(count for _, count in groups)
    per_group = [_concentric(paths, gap, line_w, count) for paths, count in groups]
    blocked_parts = dilate(no_brim, gap + line_w // 2) if no_brim else []
    blocked = union(blocked_parts) if blocked_parts else None
    rings, blocks, extent = [], [], material
    for k in range(max_loops):
        level = [shapes[k] for shapes in per_group if k < len(shapes)]
        stopped = [shapes[-1] for shapes in per_group if shapes and len(shapes) == k]
        if stopped:
            # Les groupes arrêtés à ce niveau bloquent désormais leur bande de brim (dernière boucle élargie de w).
            blocked_parts = list(blocked_parts) + [r for s in stopped for r in dilate(s, line_w)]
            blocked = union(blocked_parts)
        if not level:
            break
        merged = union_many(level) if len(level) > 1 else level[0]
        for r in merged:
            if is_ccw(r):
                rings.append(r)
                blocks.append(blocked)
        extent = union(extent + merged)
    return rings, extent, blocks


def skirt_rings(extent, settings) -> list:
    """Boucles de jupe autour de l'enveloppe convexe de ``extent`` (matière + brim)."""
    loops = max(0, int(_get(settings, "skirt_loops")))
    if loops <= 0 or not extent:
        return []
    line_w = _line_width_um(settings)
    distance = max(0, _um(_get(settings, "skirt_distance")))
    pts = np.vstack([as_ring(r) for r in extent])
    hull = convex_hull(pts)
    if len(hull) < 3:
        return []
    rings = []
    current = [hull]
    for k in range(loops):
        current = dilate(current, distance + line_w // 2 if k == 0 else line_w)
        if not current:
            break
        rings.extend(current)
    return rings


def build_adhesion(layers, settings, bed_bbox_um, *, exclude_bbox_um=None) -> None:
    """Remplit ``layer.adhesion`` (remis à zéro sur toutes les couches) : brim (couche 1) et jupe (couches
    1..skirt_height), découpés au plateau ``bed_bbox_um`` et hors de la zone exclue ``exclude_bbox_um`` (µm)."""
    if not layers:
        return None
    for layer in layers:
        layer.adhesion = []
    layer1 = layers[0]
    line_w = _line_width_um(settings)
    # Hauteur de chaque objet (pour le brim automatique), sans toucher aux couches.
    heights = {}
    for layer in layers:
        for oid, paths in layer.regions.items():
            if paths and int(oid) > 0:
                heights[oid] = max(heights.get(oid, 0), layer.z_um)
    support_paths = []
    if layer1.support is not None:
        support_paths = list(layer1.support.base_area) + list(layer1.support.interface_area)
    brim, extent, blocked = _brim(layer1, settings, support_paths, heights)
    skirt_height = max(1, int(_get(settings, "skirt_height")))
    # La jupe entoure aussi la matière des couches qu'elle couvre (une pièce plus large en hauteur n'est pas traversée).
    extent_all = list(extent)
    for layer in layers[1:skirt_height]:
        for oid, paths in layer.regions.items():
            if int(oid) > 0:
                extent_all.extend(paths)
    skirt = skirt_rings(extent_all, settings)
    bed = _bed_paths(bed_bbox_um, line_w // 2, exclude_bbox_um)
    skirt_lines = _clip_rings(skirt, bed)
    brim_lines = _clip_rings(brim, bed, blocked)
    if not skirt_lines:
        skirt_height = 0
    for layer in layers:
        entries = []
        if layer.index <= skirt_height:
            entries.extend(("Skirt", pl, line_w) for pl in skirt_lines)
        if layer.index == 1:
            entries.extend(("Brim", pl, line_w) for pl in brim_lines)
        layer.adhesion = entries
    return None


__all__ = ["BRIM_TYPES", "DEFAULTS", "auto_brim_width_mm", "brim_ear_centers", "brim_rings", "build_adhesion",
           "skirt_rings"]
