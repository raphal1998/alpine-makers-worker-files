"""Scène du trancheur Alpine Model Engine (spécification §4.2) : plateau, objets, placement, arrangement, orientation.

``Bed`` décrit le volume imprimable (origine coin par défaut : X de 0 à ``width_mm``, Y de 0 à ``depth_mm``) et une
zone exclue éventuelle (``exclude_mm = (x0, y0, x1, y1)``, zone avant-gauche 18 × 28 mm sur la P1S). ``Scene`` porte la
liste des maillages ; les parts d'un même objet 3MF (même ``object_id`` > 0) se déplacent ensemble.

``auto_orient`` est le port numpy de notre ``static/model_studio_orient.js`` (candidateOrientations, analyzeOrientation,
rankOrientations) : mêmes orientations candidates, mêmes métriques, même classement. Code Alpine Makers, numpy +
bibliothèque standard seulement. Coordonnées en millimètres.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .mesh import Mesh, quaternion_matrix

EPS_MM = 1e-6

# Réglages d'estimation (model_studio_orient.js : DEFAULTS, LIMITS, MATERIAL_DENSITY).
DEFAULTS = {"layer_height": 0.2, "walls": 2, "line_width": 0.42, "infill": 15, "support_angle": 30, "support_density": 15,
            "support_interface": True, "support_type": "normal(auto)", "material": "PLA"}
LIMITS = {"bed_epsilon": 0.15, "part_flow": 11, "support_flow": 16, "layer_overhead": 3.2, "max_triangles": 600000,
          "interface_thickness": 0.6, "tall_ratio": 4, "min_contact_ratio": 0.04}
MATERIAL_DENSITY = {"PLA": 1.24, "PLA-CF": 1.3, "PETG": 1.27, "PETG-CF": 1.3, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21,
                    "PA": 1.14, "PA-CF": 1.2, "PC": 1.2, "PVA": 1.23, "HIPS": 1.04, "PET": 1.38}
_JS_KEYS = {"layerHeight": "layer_height", "lineWidth": "line_width", "supportAngle": "support_angle",
            "supportDensity": "support_density", "supportInterface": "support_interface", "supportType": "support_type"}

WARN_NO_SUPPORT = "Surplombs sans support : supports désactivés dans le profil"
WARN_LOW_CONTACT = "Peu de surface posée : risque de décollement, prévoir une bordure"
WARN_TALL = "Pièce haute sur une petite base : risque de basculement"


class SceneError(ValueError):
    """Objet hors du plateau ou arrangement impossible (code stable ``OUT_OF_BED``)."""

    code = "OUT_OF_BED"


# ---------------------------------------------------------------------------------------------- plateau

@dataclass(frozen=True)
class Bed:
    """Plateau : largeur X, profondeur Y, hauteur Z (mm), zone exclue ``(x0, y0, x1, y1)`` ou ``None``, origine
    ``"corner"`` (0..largeur) ou ``"center"`` (−largeur/2..+largeur/2). Même signature que ``machine.base.Bed``."""

    width_mm: float
    depth_mm: float
    height_mm: float
    exclude_mm: tuple | None = None
    origin: str = "corner"

    def __post_init__(self):
        for label, value in (("largeur", self.width_mm), ("profondeur", self.depth_mm), ("hauteur", self.height_mm)):
            if not (float(value) > 0):
                raise SceneError(f"plateau : {label} invalide ({value!r})")
        if self.origin not in ("corner", "center"):
            raise SceneError(f"plateau : origine inconnue « {self.origin} » (corner ou center)")
        if self.exclude_mm is not None:
            if len(self.exclude_mm) != 4:
                raise SceneError("plateau : zone exclue attendue sous la forme (x0, y0, x1, y1)")
            object.__setattr__(self, "exclude_mm", _normalized_rect(self.exclude_mm))

    def rect(self):
        """Rectangle imprimable ``(x0, y0, x1, y1)`` selon l'origine."""
        return _bed_rect(self)

    def center_xy(self):
        x0, y0, x1, y1 = self.rect()
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)

    def contains_xy(self, x_mm, y_mm, margin_mm=0.0):
        """Point dans le rectangle imprimable (marge positive = zone autorisée rétrécie)."""
        x0, y0, x1, y1 = self.rect()
        return (x0 + margin_mm <= x_mm <= x1 - margin_mm) and (y0 + margin_mm <= y_mm <= y1 - margin_mm)

    def in_exclusion(self, x_mm, y_mm):
        """Point dans la zone exclue (bords inclus)."""
        if not self.exclude_mm:
            return False
        x0, y0, x1, y1 = self.exclude_mm
        return x0 <= x_mm <= x1 and y0 <= y_mm <= y1

    def printable_area(self):
        """Quatre coins du plateau, sens trigonométrique."""
        x0, y0, x1, y1 = self.rect()
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]

    def exclude_area(self):
        """Quatre coins de la zone exclue (liste vide si aucune)."""
        if not self.exclude_mm:
            return []
        x0, y0, x1, y1 = self.exclude_mm
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _normalized_rect(rect):
    x0, y0, x1, y1 = (float(v) for v in rect)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _bed_rect(bed):
    """Rectangle imprimable d'un plateau quelconque (``scene.Bed`` ou ``machine.base.Bed``)."""
    w, d = float(bed.width_mm), float(bed.depth_mm)
    if getattr(bed, "origin", "corner") == "center":
        return (-w / 2.0, -d / 2.0, w / 2.0, d / 2.0)
    return (0.0, 0.0, w, d)


def _bed_exclusion(bed):
    rect = getattr(bed, "exclude_mm", None)
    return _normalized_rect(rect) if rect else None


def _rects_overlap(a, b, eps=EPS_MM):
    """Deux rectangles ``(x0, y0, x1, y1)`` se recouvrent (intérieurs, un simple contact de bord ne compte pas)."""
    return a[0] < b[2] - eps and b[0] < a[2] - eps and a[1] < b[3] - eps and b[1] < a[3] - eps


# Plateau de la Bambu Lab P1S (valeurs publiques) : 256 × 256 × 256 mm, zone exclue avant-gauche 18 × 28 mm.
P1S_BED = Bed(256.0, 256.0, 256.0, exclude_mm=(0.0, 0.0, 18.0, 28.0))


# ---------------------------------------------------------------------------------------------- scène

class Scene:
    """Plateau + maillages. ``objects`` : liste de ``Mesh`` (parts d'un même objet 3MF : même ``object_id`` > 0)."""

    __slots__ = ("bed", "objects")

    def __init__(self, bed=None, objects=None):
        self.bed = bed if bed is not None else P1S_BED
        self.objects = list(objects or [])

    def add(self, mesh):
        self.objects.append(mesh)
        return mesh

    def __len__(self):
        return len(self.objects)

    def __repr__(self):
        return f"Scene({len(self.objects)} maillage(s), plateau {self.bed.width_mm:g} × {self.bed.depth_mm:g} × {self.bed.height_mm:g} mm)"

    def bounds(self):
        """``(min, max)`` de tous les maillages non vides ; zéros si aucun."""
        points = [m.vertices for m in self.objects if len(m.vertices)]
        if not points:
            return np.zeros(3), np.zeros(3)
        lo = np.min([p.min(axis=0) for p in points], axis=0)
        hi = np.max([p.max(axis=0) for p in points], axis=0)
        return lo, hi

    def groups(self):
        """Groupes d'objets (parts liées par ``object_id``), dans l'ordre d'apparition."""
        return _groups(self)

    def copy(self):
        return Scene(self.bed, [m.copy() for m in self.objects])


class _Group:
    """Un objet imprimable : ses parts, et ses parts « normal » qui définissent l'encombrement."""

    __slots__ = ("key", "name", "meshes")

    def __init__(self, key, meshes):
        self.key = key
        self.meshes = meshes
        self.name = next((m.name for m in meshes if m.name), "") or f"objet {key[1] + 1 if key[0] == 'mesh' else key[1]}"

    def reference(self):
        normal = [m for m in self.meshes if m.volume_type == "normal" and len(m.vertices)]
        return normal or [m for m in self.meshes if len(m.vertices)]

    def bounds(self):
        ref = self.reference()
        if not ref:
            return None
        lo = np.min([m.vertices.min(axis=0) for m in ref], axis=0)
        hi = np.max([m.vertices.max(axis=0) for m in ref], axis=0)
        return lo, hi

    def faces(self):
        return sum(len(m.faces) for m in self.meshes)

    def translate(self, dx, dy, dz):
        for m in self.meshes:
            m.translate(dx, dy, dz)

    def rotate_z(self, angle_rad, center):
        for m in self.meshes:
            m.rotate("z", angle_rad, center=center)


def _groups(scene):
    buckets, order = {}, []
    for index, mesh in enumerate(scene.objects):
        key = ("obj", int(mesh.object_id)) if int(mesh.object_id) > 0 else ("mesh", index)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(mesh)
    return [_Group(key, buckets[key]) for key in order]


# ---------------------------------------------------------------------------------------------- placement, validation

def place_default(scene, keep_inside=False, spacing_mm=5.0):
    """Pose chaque objet sur le plateau (z min = 0) et le centre en XY : avec l'origine coin, un STL centré en (0, 0)
    est translaté au centre du plateau. Un objet centré qui entre dans la zone exclue est décalé au minimum pour en
    sortir (grande pièce sur la P1S). ``keep_inside`` : un objet déjà entièrement dans la zone imprimable (hors zone
    exclue) garde sa position. Plusieurs objets déplacés au même centre sont ensuite répartis par ``arrange`` (avec
    ``keep_inside``, seulement si un objet déplacé en recouvre un autre) ; ``arrange`` sert aussi quand le décalage
    minimal ne suffit pas, et lève ``SceneError`` si rien ne tient."""
    groups = _groups(scene)
    cx, cy = _center_xy(scene.bed)
    moved = []
    stuck = False
    for group in groups:
        bounds = group.bounds()
        if bounds is None:
            continue
        if keep_inside and not _placement_errors(scene.bed, group, bounds):
            continue
        lo, hi = bounds
        group.translate(cx - (lo[0] + hi[0]) / 2.0, cy - (lo[1] + hi[1]) / 2.0, -lo[2])
        if not _nudge_out_of_exclusion(group, scene.bed):
            stuck = True
        moved.append(group)
    if moved and (stuck or (len(groups) > 1 and (not keep_inside or _any_overlap(moved, groups)))):
        arrange(scene, spacing_mm=spacing_mm)
    return scene


def _nudge_out_of_exclusion(group, bed):
    """Décale au minimum (droite, haut, gauche ou bas) un groupe dont la boîte XY entre dans la zone exclue, en restant
    dans le plateau. Renvoie ``False`` si aucun décalage ne convient (rien à faire ou déplacé = ``True``)."""
    exclusion = _bed_exclusion(bed)
    box = _xy_box(group)
    if exclusion is None or box is None or not _rects_overlap(box, exclusion):
        return True
    x0, y0, x1, y1 = _bed_rect(bed)
    ex0, ey0, ex1, ey1 = exclusion
    moves = [(ex1 - box[0], 0.0), (0.0, ey1 - box[1]), (ex0 - box[2], 0.0), (0.0, ey0 - box[3])]
    for dx, dy in sorted(moves, key=lambda m: abs(m[0]) + abs(m[1])):
        shifted = (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)
        if (shifted[0] >= x0 - EPS_MM and shifted[1] >= y0 - EPS_MM and shifted[2] <= x1 + EPS_MM
                and shifted[3] <= y1 + EPS_MM):
            group.translate(dx, dy, 0.0)
            return True
    return False


def _xy_box(group):
    bounds = group.bounds()
    if bounds is None:
        return None
    lo, hi = bounds
    return (float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1]))


def _any_overlap(moved, groups):
    """Un groupe déplacé recouvre-t-il (en XY) un autre groupe de la scène ?"""
    for a in moved:
        box_a = _xy_box(a)
        if box_a is None:
            continue
        for b in groups:
            if b is a:
                continue
            box_b = _xy_box(b)
            if box_b is not None and _rects_overlap(box_a, box_b):
                return True
    return False


def _center_xy(bed):
    x0, y0, x1, y1 = _bed_rect(bed)
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _placement_errors(bed, group, bounds):
    x0, y0, x1, y1 = _bed_rect(bed)
    height = float(bed.height_mm)
    lo, hi = bounds
    name = group.name
    errors = []
    if lo[2] < -EPS_MM:
        errors.append(f"Objet « {name} » : passe sous le plateau (z min {lo[2]:.2f} mm).")
    if hi[2] > height + EPS_MM:
        errors.append(f"Objet « {name} » : trop haut ({hi[2]:.1f} mm > {height:g} mm imprimables).")
    if lo[0] < x0 - EPS_MM or hi[0] > x1 + EPS_MM or lo[1] < y0 - EPS_MM or hi[1] > y1 + EPS_MM:
        errors.append(f"Objet « {name} » : dépasse le plateau (X {lo[0]:.1f}…{hi[0]:.1f}, Y {lo[1]:.1f}…{hi[1]:.1f} mm, "
                      f"plateau X {x0:g}…{x1:g}, Y {y0:g}…{y1:g}).")
    exclusion = _bed_exclusion(bed)
    if exclusion and _rects_overlap((lo[0], lo[1], hi[0], hi[1]), exclusion):
        ex = exclusion
        errors.append(f"Objet « {name} » : entre dans la zone exclue du plateau (X {ex[0]:g}…{ex[2]:g}, Y {ex[1]:g}…{ex[3]:g} mm).")
    return errors


def validate(scene):
    """Liste de messages d'erreur en français (vide = scène imprimable) : maillages vides, objets hors du volume
    imprimable, sous le plateau, trop hauts, ou dans la zone exclue. Seules les parts « normal » comptent pour le
    volume ; les modificateurs et volumes de support ne sont contrôlés que s'ils sont seuls dans leur objet."""
    errors = []
    if not scene.objects:
        return ["Scène vide : aucun maillage à trancher."]
    for group in _groups(scene):
        if group.faces() == 0:
            errors.append(f"Objet « {group.name} » : aucun triangle.")
            continue
        bounds = group.bounds()
        if bounds is None:
            errors.append(f"Objet « {group.name} » : aucun sommet.")
            continue
        errors.extend(_placement_errors(scene.bed, group, bounds))
    return errors


def check(scene):
    """``validate`` qui lève ``SceneError`` (message = erreurs jointes par des retours à la ligne)."""
    errors = validate(scene)
    if errors:
        raise SceneError("\n".join(errors))
    return scene


# ---------------------------------------------------------------------------------------------- arrangement

class _Shelf:
    __slots__ = ("y", "height", "cursor")

    def __init__(self, y, height):
        self.y, self.height, self.cursor = y, height, 0.0


def arrange(scene, spacing_mm=5.0, allow_rotate=True):
    """Répartit les objets sur le plateau par étagères (rangées le long de X) : boîtes XY triées par aire décroissante,
    hors zone exclue, espacement ``spacing_mm`` entre objets, chaque objet posé sur le plateau. Un objet qui ne tient
    pas dans une rangée est essayé tourné de 90° (``allow_rotate``). L'ensemble est recentré sur le plateau quand
    c'est possible sans toucher la zone exclue. Lève ``SceneError`` si un objet est plus grand que le plateau ou si
    tout ne tient pas. Renvoie la liste des placements ``{name, x, y, width, depth, rotated}``."""
    spacing = max(0.0, float(spacing_mm))
    groups = _groups(scene)
    if not groups:
        return []
    x0, y0, x1, y1 = _bed_rect(scene.bed)
    width, depth = x1 - x0, y1 - y0
    exclusion = _bed_exclusion(scene.bed)
    local_exclusion = None if exclusion is None else (exclusion[0] - x0, exclusion[1] - y0, exclusion[2] - x0, exclusion[3] - y0)
    items = []
    for index, group in enumerate(groups):
        bounds = group.bounds()
        if bounds is None or group.faces() == 0:
            raise SceneError(f"Objet « {group.name} » : aucun triangle, arrangement impossible.")
        lo, hi = bounds
        w, d = float(hi[0] - lo[0]), float(hi[1] - lo[1])
        items.append((-(w * d), index, group, w, d))
    items.sort(key=lambda item: (item[0], item[1]))
    shelves = []
    placed = []  # (group, rotated, x, y, bw, bd) en coordonnées locales (coin du plateau = 0, 0)

    def fit(cursor, y, bw, bd):
        # La zone exclue n'est pas un objet : la boîte peut la toucher par le bord, pas la recouvrir.
        x = cursor
        if local_exclusion is not None and _rects_overlap((x, y, x + bw, y + bd), local_exclusion):
            x = local_exclusion[2]
        return x if x + bw <= width + EPS_MM else None

    for _area, _index, group, w, d in items:
        if min(w, d) > min(width, depth) + EPS_MM or max(w, d) > max(width, depth) + EPS_MM:
            raise SceneError(f"Objet « {group.name} » ({w:.1f} × {d:.1f} mm) plus grand que le plateau ({width:g} × {depth:g} mm).")
        done = False
        for rotated in ((False, True) if allow_rotate else (False,)):
            bw, bd = (d, w) if rotated else (w, d)
            if bw > width + EPS_MM or bd > depth + EPS_MM:
                continue
            for number, shelf in enumerate(shelves):
                last = number == len(shelves) - 1
                if bd > shelf.height + EPS_MM and not (last and shelf.y + bd <= depth + EPS_MM):
                    continue
                x = fit(shelf.cursor, shelf.y, bw, bd)
                if x is None:
                    continue
                shelf.height = max(shelf.height, bd)
                shelf.cursor = x + bw + spacing
                placed.append((group, rotated, x, shelf.y, bw, bd))
                done = True
                break
            if done:
                break
            y = shelves[-1].y + shelves[-1].height + spacing if shelves else 0.0
            if y + bd <= depth + EPS_MM:
                x = fit(0.0, y, bw, bd)
                if x is not None:
                    shelf = _Shelf(y, bd)
                    shelf.cursor = x + bw + spacing
                    shelves.append(shelf)
                    placed.append((group, rotated, x, y, bw, bd))
                    done = True
                    break
        if not done:
            raise SceneError(f"Impossible de placer « {group.name} » : le plateau ({width:g} × {depth:g} mm) est trop petit "
                             f"pour {len(items)} objets avec {spacing:g} mm d'espacement.")
    # Recentrage de l'ensemble, uniquement s'il reste hors de la zone exclue et dans le plateau.
    gx0 = min(p[2] for p in placed)
    gy0 = min(p[3] for p in placed)
    gx1 = max(p[2] + p[4] for p in placed)
    gy1 = max(p[3] + p[5] for p in placed)
    shift_x, shift_y = (width - (gx1 - gx0)) / 2.0 - gx0, (depth - (gy1 - gy0)) / 2.0 - gy0
    centred_ok = all(_box_allowed((x + shift_x, y + shift_y, x + shift_x + bw, y + shift_y + bd), width, depth, local_exclusion)
                     for _g, _r, x, y, bw, bd in placed)
    if not centred_ok:
        shift_x = shift_y = 0.0
    result = []
    for group, rotated, x, y, bw, bd in placed:
        lo, hi = group.bounds()
        if rotated:
            centre = (lo + hi) / 2.0
            group.rotate_z(math.pi / 2.0, centre)
            lo, hi = group.bounds()
        target_x, target_y = x0 + x + shift_x, y0 + y + shift_y
        group.translate(target_x - lo[0], target_y - lo[1], -lo[2])
        result.append({"name": group.name, "x": target_x, "y": target_y, "width": bw, "depth": bd, "rotated": rotated})
    return result


def _box_allowed(box, width, depth, exclusion):
    if box[0] < -EPS_MM or box[1] < -EPS_MM or box[2] > width + EPS_MM or box[3] > depth + EPS_MM:
        return False
    return exclusion is None or not _rects_overlap(box, exclusion)


# ---------------------------------------------------------------------------------------------- orientation (port de model_studio_orient.js)

def _settings(options):
    s = dict(DEFAULTS)
    for key, value in (options or {}).items():
        s[_JS_KEYS.get(key, key)] = value
    return s


def _num(value, fallback):
    """``Number(v) || fallback`` de JavaScript : 0, NaN, texte illisible → ``fallback``."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return fallback
    return fallback if (v == 0 or not math.isfinite(v)) else v


def _normalize(v):
    v = np.asarray(v, dtype=np.float64).reshape(3)
    length = float(np.linalg.norm(v)) or 1.0
    return v / length


def _triangles(tris):
    return np.ascontiguousarray(np.asarray(tris, dtype=np.float64).reshape(-1, 3, 3))


def _areas_normals(tris):
    """Aires (m,) et normales unitaires (m, 3) (nulles pour un triangle dégénéré), comme ``triangle()`` du JS."""
    cross = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    length = np.linalg.norm(cross, axis=1)
    normals = np.divide(cross, length[:, None], out=np.zeros_like(cross), where=length[:, None] > 0)
    return length / 2.0, normals


def sample_triangles(tris, maximum=None):
    """Échantillon régulier au-delà de ``maximum`` triangles ; renvoie ``(tris, weight)`` (surfaces/volumes à remettre à l'échelle)."""
    tris = _triangles(tris)
    maximum = int(LIMITS["max_triangles"] if maximum is None else maximum)
    count = len(tris)
    if count <= maximum:
        return tris, 1.0
    step = count / maximum
    index = np.floor(np.arange(maximum, dtype=np.float64) * step).astype(np.int64)
    return np.ascontiguousarray(tris[index]), count / maximum


def quaternion_to_down(n):
    """Quaternion ``[x, y, z, w]`` qui amène la direction ``n`` sur −Z (la face de normale ``n`` posée sur le plateau)."""
    ax, ay, az = _normalize(n)
    r = -az + 1.0  # n · (0, 0, −1) + 1
    if r < 1e-6:   # n = +Z : demi-tour autour de X
        return np.array([1.0, 0.0, 0.0, 0.0])
    q = np.array([-ay, ax, 0.0, r], dtype=np.float64)
    return q / np.linalg.norm(q)


def rotate_triangles(tris, q):
    """Applique la rotation du quaternion ``q`` ``[x, y, z, w]`` à des triangles (m, 3, 3)."""
    tris = _triangles(tris)
    rotation = quaternion_matrix(q)[:3, :3]
    return tris @ rotation.T


def candidate_orientations(tris, planar=4):
    """Orientations candidates : l'actuelle, les six faces de la boîte, puis les plus grandes faces planes (normales
    regroupées à 1/20 près, sans doublon à 5° près). Chaque entrée : ``{id, label, down, q}``."""
    tris = _triangles(tris)
    candidates = [{"id": "current", "label": "Orientation actuelle", "down": np.array([0.0, 0.0, -1.0]), "q": np.array([0.0, 0.0, 0.0, 1.0])}]
    for label, down in (("Face +Z dessous", (0, 0, 1)), ("Face +X dessous", (1, 0, 0)), ("Face −X dessous", (-1, 0, 0)),
                        ("Face +Y dessous", (0, 1, 0)), ("Face −Y dessous", (0, -1, 0))):
        candidates.append({"id": "axis:" + ",".join(str(c) for c in down), "label": label,
                           "down": np.array(down, dtype=np.float64), "q": quaternion_to_down(down)})
    if len(tris) == 0 or planar <= 0:
        return candidates
    areas, normals = _areas_normals(tris)
    keep = areas > 0
    areas, normals = areas[keep], normals[keep]
    if len(areas) == 0:
        return candidates
    rounded = np.floor(normals * 20.0 + 0.5).astype(np.int64) + 20      # Math.round(c * 20) ; composantes dans 0..40
    key = (rounded[:, 0] * 41 + rounded[:, 1]) * 41 + rounded[:, 2]
    unique, first, inverse = np.unique(key, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1)
    bucket_area = np.bincount(inverse, weights=areas, minlength=len(unique))
    weighted = np.stack([np.bincount(inverse, weights=normals[:, axis] * areas, minlength=len(unique)) for axis in range(3)], axis=1)
    order = np.lexsort((first, -bucket_area))                            # aire décroissante, puis ordre d'apparition
    for b in order:
        if len(candidates) >= 6 + planar:
            break
        down = _normalize(weighted[b])
        if any(float(np.dot(c["down"], down)) > 0.996 for c in candidates):
            continue
        candidates.append({"id": "face:" + ",".join(f"{c:.3f}" for c in down), "label": "Grande face plane",
                           "down": down, "q": quaternion_to_down(down)})
    return candidates


_GRID_CACHE = {}


def _barycentric_grid(n):
    """Poids ``(k, 3)`` des points du réseau barycentrique de ``samplePoints`` (ordre : v0 → c, v1 → a, v2 → b)."""
    grid = _GRID_CACHE.get(n)
    if grid is None:
        i, j = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
        mask = j < n - i
        a = (i[mask] + 1.0 / 3.0) / n
        b = (j[mask] + 1.0 / 3.0) / n
        grid = np.stack([1.0 - a - b, a, b], axis=1)
        _GRID_CACHE[n] = grid
    return grid


def _sample_counts(projected, cell, limit):
    """``n`` par triangle : ``max(1, min(limit, ceil(sqrt(max(0, aire projetée)) / cell)))``."""
    n = np.ceil(np.sqrt(np.maximum(projected, 0.0)) / cell)
    return np.clip(n, 1, limit).astype(np.int64)


def _sample_points(tris, counts):
    """Points échantillonnés (p, 3) et indice du triangle de chaque point (p,)."""
    points, owners = [], []
    for n in np.unique(counts):
        which = np.flatnonzero(counts == n)
        grid = _barycentric_grid(int(n))
        pts = np.einsum("kj,tjd->tkd", grid, tris[which]).reshape(-1, 3)
        points.append(pts)
        owners.append(np.repeat(which, len(grid)))
    if not points:
        return np.zeros((0, 3)), np.zeros(0, dtype=np.int64)
    return np.concatenate(points), np.concatenate(owners)


def analyze_orientation(rotated, options=None, weight=1.0):
    """Estime une orientation (triangles déjà tournés, (m, 3, 3) mm) : hauteur, contact plateau, surplombs au-delà
    du seuil, volume de supports (colonnes jusqu'à la surface tournée vers le haut la plus proche sous le surplomb,
    ou le plateau), matière, durée indicative, avertissements. ``None`` sans triangle."""
    s = _settings(options)
    tris = _triangles(rotated)
    count = len(tris)
    if count == 0:
        return None
    points = tris.reshape(-1, 3)
    if not np.isfinite(points).all():
        return None
    lo, hi = points.min(axis=0), points.max(axis=0)
    min_x, min_y, min_z = (float(v) for v in lo)
    max_x, max_y, max_z = (float(v) for v in hi)
    height = max_z - min_z
    dx, dy = max_x - min_x, max_y - min_y
    footprint = max(dx, 1e-6) * max(dy, 1e-6)
    cell = max(0.8, max(dx, dy) / 160.0)
    cols = math.ceil(dx / cell) + 1
    cos_threshold = math.cos(max(0.0, min(89.0, _num(s.get("support_angle"), 30.0))) * math.pi / 180.0)
    areas, normals = _areas_normals(tris)
    nz = normals[:, 2]
    surface = float(areas.sum())
    volume = float(np.einsum("ij,ij->i", tris[:, 0], np.cross(tris[:, 1], tris[:, 2])).sum() / 6.0)
    on_bed = tris[:, :, 2].max(axis=1) < min_z + LIMITS["bed_epsilon"]
    contact_mask = on_bed & (nz < -0.9)
    contact = float(areas[contact_mask].sum())
    up_mask = ~contact_mask & (nz > 0.15)
    over_mask = ~contact_mask & (nz < -cos_threshold) & ~on_bed

    def cell_key(pts):
        return (np.floor((pts[:, 1] - min_y) / cell) * cols + np.floor((pts[:, 0] - min_x) / cell)).astype(np.int64)

    # Grille des surfaces tournées vers le haut : chaque surplomb s'appuie sur la plus haute d'entre elles sous lui.
    top_keys = np.zeros(0, dtype=np.int64)
    top_z = np.zeros(0)
    if up_mask.any():
        up = np.flatnonzero(up_mask)
        counts = _sample_counts(areas[up] * nz[up], cell / 2.5, 64)
        pts, _owner = _sample_points(tris[up], counts)
        keys = cell_key(pts)
        order = np.lexsort((pts[:, 2], keys))
        top_keys, top_z = keys[order], pts[order, 2]
    span = height + 1.0
    top_composite = top_keys + (top_z - min_z) / span
    overhang_area = support_columns = supported_area = 0.0
    if over_mask.any():
        over = np.flatnonzero(over_mask)
        projected = areas[over] * -nz[over]
        overhang_area = float(projected.sum())
        counts = _sample_counts(projected, cell, 24)
        pts, owner = _sample_points(tris[over], counts)
        share = (projected / (counts * (counts + 1) / 2.0))[owner]     # n(n+1)/2 points par triangle, comme samplePoints()
        z = pts[:, 2]
        floor = np.full(len(pts), min_z)
        if len(top_keys):
            keys = cell_key(pts)
            query = keys + np.minimum((z + 0.05 - min_z) / span, 0.999999)
            index = np.searchsorted(top_composite, query, side="right") - 1
            valid = index >= 0
            index = np.where(valid, index, 0)
            valid &= top_keys[index] == keys
            floor = np.where(valid, top_z[index], min_z)
        column = z - floor
        needs = column > 0.1          # posé sur la pièce : rien
        support_columns = float((share * column)[needs].sum())
        supported_area = float(share[needs].sum())
    volume = abs(volume) * weight
    surface *= weight
    contact *= weight
    overhang_area *= weight
    support_columns *= weight
    supported_area *= weight
    support_type = str(s.get("support_type"))
    tree = "tree" in support_type.lower()
    supports_on = support_type != "off"
    fill = (max(5.0, min(100.0, _num(s.get("support_density"), 15.0))) / 100.0) * (0.6 if tree else 1.0)
    interface = bool(s.get("support_interface"))
    support_mm3 = (support_columns * fill + (supported_area * LIMITS["interface_thickness"] if interface else 0.0)) if supports_on else 0.0
    shell = min(volume, surface * max(1.0, _num(s.get("walls"), 2.0)) * _num(s.get("line_width"), 0.42))
    part_mm3 = shell + max(0.0, volume - shell) * max(0.0, min(100.0, _num(s.get("infill"), 0.0))) / 100.0
    density = MATERIAL_DENSITY.get(str(s.get("material") or "PLA").upper(), MATERIAL_DENSITY["PLA"])
    layers = max(1, math.ceil(height / _num(s.get("layer_height"), 0.2)))
    seconds = part_mm3 / LIMITS["part_flow"] + support_mm3 / LIMITS["support_flow"] + layers * LIMITS["layer_overhead"]
    warnings = []
    if not supports_on and supported_area > 1:
        warnings.append(WARN_NO_SUPPORT)
    if contact < footprint * LIMITS["min_contact_ratio"]:
        warnings.append(WARN_LOW_CONTACT)
    if height > LIMITS["tall_ratio"] * math.sqrt(max(contact, 1.0)) and height > 40:
        warnings.append(WARN_TALL)
    return {"height": height, "footprint": footprint, "layers": layers, "contact_area": contact, "overhang_area": supported_area,
            "support_volume": support_mm3, "support_grams": support_mm3 / 1000.0 * density, "part_grams": part_mm3 / 1000.0 * density,
            "total_grams": (support_mm3 + part_mm3) / 1000.0 * density, "seconds": seconds, "warnings": warnings,
            "supports_on": supports_on}


def rank_orientations(results):
    """Classement : matière et durée d'abord, la surface de contact départage, chaque avertissement pénalise.
    Renvoie les résultats valides triés (``score``, ``rank``, ``best`` ajoutés)."""
    valid = [r for r in results if r and r.get("metrics")]
    if not valid:
        return results
    mg = max(max(r["metrics"]["total_grams"] for r in valid), 1e-9)
    ms = max(max(r["metrics"]["seconds"] for r in valid), 1e-9)
    mc = max(max(r["metrics"]["contact_area"] for r in valid), 1e-9)
    for r in valid:
        m = r["metrics"]
        r["score"] = 0.45 * m["total_grams"] / mg + 0.4 * m["seconds"] / ms + 0.15 * (1.0 - m["contact_area"] / mc) + len(m["warnings"]) * 0.05
    valid.sort(key=lambda r: r["score"])
    for rank, r in enumerate(valid, start=1):
        r["rank"], r["best"] = rank, rank == 1
    return valid


def compare_orientations(tris, options=None):
    """Toutes les orientations candidates estimées et classées (meilleure en premier)."""
    sample, weight = sample_triangles(tris)
    results = []
    for candidate in candidate_orientations(sample):
        entry = dict(candidate)
        entry["metrics"] = analyze_orientation(rotate_triangles(sample, candidate["q"]), options, weight)
        results.append(entry)
    return rank_orientations(results)


def auto_orient(mesh, options=None, apply=True):
    """Choisit la meilleure orientation d'un maillage (notre comparateur du Model Studio) et, si ``apply``, tourne le
    maillage en place autour de son centre, le repose sur z = 0 en gardant son centre XY. Renvoie le candidat retenu
    (``id``, ``label``, ``q``, ``metrics``, ``score``) complété de ``ranked`` (tous les candidats) et ``applied``."""
    ranked = compare_orientations(mesh.triangles(), options)
    if not ranked or not ranked[0].get("metrics"):
        return {"id": "current", "label": "Orientation actuelle", "q": np.array([0.0, 0.0, 0.0, 1.0]), "metrics": None,
                "ranked": ranked, "applied": False}
    best = dict(ranked[0])
    best["ranked"] = ranked
    best["applied"] = False
    if apply and best["id"] != "current":
        centre = mesh.center()
        mesh.rotate_quaternion(best["q"], center=centre)
        after = mesh.center()
        mesh.translate(centre[0] - after[0], centre[1] - after[1], 0.0)
        mesh.drop_to_bed()
        best["applied"] = True
    return best


__all__ = ["Bed", "Scene", "SceneError", "P1S_BED", "DEFAULTS", "LIMITS", "MATERIAL_DENSITY", "place_default", "arrange",
           "validate", "check", "auto_orient", "compare_orientations", "candidate_orientations", "analyze_orientation",
           "rank_orientations", "quaternion_to_down", "rotate_triangles", "sample_triangles"]
