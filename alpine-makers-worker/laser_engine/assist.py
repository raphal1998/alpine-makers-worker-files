# -*- coding: utf-8 -*-
"""Bibliothèque des scripts de l'assistant de code d'Alpine Laser Studio (importée sous « laser_assist »).

Un script (écrit par Claude, GPT ou l'utilisateur) déclare ``PARAMS`` et ``build(p, ctx)`` ; il dessine en
millimètres — origine en haut à gauche, y vers le bas, comme le SVG — avec shapely et les aides ci-dessous,
puis renvoie un ``Drawing``. ``_finalize()`` prépare la géométrie pour la machine :

- gravure par niveaux (1 = le plus clair … N = le plus foncé) : union par niveau ; sur une zone commune, le
  niveau le plus foncé l'emporte, si bien que chaque point n'est gravé qu'une fois ; les trous sont gardés ;
- découpe et trait : lignes nouées puis fusionnées, un segment commun n'est tracé qu'une fois, un contour
  fermé reste un anneau fermé ; un trait posé sur une découpe est retiré ;
- grille de 0,001 mm, points répétés et nœuds alignés supprimés, simplification réglée par le niveau de
  détail, poussières sous la surface minimale écartées.

Le SVG final a une couleur de calque par niveau, rouge (#ff0000) pour la découpe et bleu (#0000ff) pour le
trait ; ``preview_svg`` montre les niveaux en gris. Aucune fonction publique n'ouvre de fichier : l'image
jointe au job est désignée par le bac à sable (``_configure``), jamais par le script.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

import shapely
from shapely import affinity
from shapely.geometry import (GeometryCollection, LinearRing, LineString, MultiLineString, MultiPolygon, Point,
                              Polygon, box)
from shapely.geometry.polygon import orient
from shapely.ops import linemerge, unary_union

from .script_sandbox import detail_pitch, detail_tolerance

GRID_MM = 0.001
MAX_LEVELS = 16
MAX_SIZE_MM = 3000.0
MAX_TRACE_PX = 2400
CUT_COLOR = "#ff0000"
MARK_COLOR = "#0000ff"
# Palette du studio, du plus foncé au plus clair ; rouge, bleu et magenta (mire caméra) restent libres.
LEVEL_COLORS = ("#000000", "#00c000", "#c0c000", "#ff8000", "#00c0c0", "#a0a0a0", "#000080", "#800000",
                "#008000", "#808000", "#c06000", "#0080ff", "#800080", "#404040", "#8060ff", "#ff6080")
MERGE_ELEMENTS_ABOVE = 300

_STATE: dict[str, Any] = {"tolerance": 0.02, "levels": 1, "detail": 6, "image": None, "warnings": []}


class ScriptError(ValueError):
    """Erreur signalée au script (et à l'assistant qui le corrige) : message clair, sans trace interne."""


# ---------------------------------------------------------------------------------------------------------
# Réglages et contexte
# ---------------------------------------------------------------------------------------------------------
class Context:
    """Contexte transmis à ``build(p, ctx)`` : lecture seule pour le script."""

    __slots__ = ("levels", "detail", "tolerance", "width_mm", "height_mm", "has_image", "seed")

    def __init__(self, levels: int, detail: float, width_mm: float, height_mm: float, has_image: bool, seed: int):
        self.levels = levels
        self.detail = detail
        self.tolerance = detail_tolerance(detail)
        self.width_mm = width_mm
        self.height_mm = height_mm
        self.has_image = has_image
        self.seed = seed


def _configure(levels: int, detail: float, image: str | None = None) -> None:
    _STATE.update(levels=int(levels), detail=float(detail), tolerance=detail_tolerance(detail), image=image, warnings=[])


def tolerance() -> float:
    """Écart courbe/segments en vigueur (mm), fixé par le niveau de détail."""
    return _STATE["tolerance"]


def warn(message: str) -> None:
    """Avertissement affiché avec le résultat (20 au plus)."""
    if len(_STATE["warnings"]) < 20:
        _STATE["warnings"].append(str(message)[:300])


def _number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ScriptError(f"{name} : nombre attendu (reçu {value!r}).") from None
    if not math.isfinite(number):
        raise ScriptError(f"{name} : nombre fini attendu.")
    return number


def _positive(value: Any, name: str) -> float:
    number = _number(value, name)
    if number <= 0:
        raise ScriptError(f"{name} doit être strictement positif (reçu {number:g}).")
    return number


def _segments(radius: float, sweep: float = 2 * math.pi, minimum: int = 8) -> int:
    """Nombre de segments pour que la flèche d'un arc reste sous la tolérance."""
    tol = tolerance()
    if radius <= tol:
        return max(3, minimum // 2)
    step = 2 * math.acos(max(-1.0, 1.0 - tol / radius))
    return max(minimum if sweep >= 2 * math.pi - 1e-9 else 2, math.ceil(sweep / step))


# ---------------------------------------------------------------------------------------------------------
# Formes de base (mm)
# ---------------------------------------------------------------------------------------------------------
def rect(x: float, y: float, w: float, h: float, r: float = 0.0) -> Polygon:
    """Rectangle de coin haut-gauche (x, y) ; ``r`` arrondit les coins."""
    x, y = _number(x, "x"), _number(y, "y")
    w, h, r = _positive(w, "largeur"), _positive(h, "hauteur"), max(0.0, _number(r, "rayon"))
    r = min(r, w / 2, h / 2)
    if r <= GRID_MM:
        return box(x, y, x + w, y + h)
    n = _segments(r, math.pi / 2)
    points = []
    for cx, cy, start in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0), (x + r, y + h - r, 90), (x + r, y + r, 180)):
        for i in range(n + 1):
            a = math.radians(start + 90 * i / n)
            points.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return Polygon(points)


def circle(cx: float, cy: float, r: float) -> Polygon:
    """Disque (polygone dont les sommets sont exactement sur le cercle)."""
    cx, cy, r = _number(cx, "cx"), _number(cy, "cy"), _positive(r, "rayon")
    n = _segments(r)
    return Polygon([(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)])


def ellipse(cx: float, cy: float, rx: float, ry: float, angle: float = 0.0) -> Polygon:
    """Ellipse de demi-axes rx, ry, tournée de ``angle`` degrés."""
    cx, cy = _number(cx, "cx"), _number(cy, "cy")
    rx, ry = _positive(rx, "rx"), _positive(ry, "ry")
    n = _segments(max(rx, ry))
    shape = Polygon([(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n)])
    return affinity.rotate(shape, _number(angle, "angle"), origin=(cx, cy)) if angle else shape


def regular_polygon(cx: float, cy: float, r: float, sides: int, rotation: float = -90.0) -> Polygon:
    """Polygone régulier inscrit dans un cercle de rayon r ; par défaut un sommet pointe vers le haut."""
    sides = int(sides)
    if sides < 3 or sides > 1000:
        raise ScriptError("regular_polygon : entre 3 et 1000 côtés.")
    cx, cy, r = _number(cx, "cx"), _number(cy, "cy"), _positive(r, "rayon")
    start = math.radians(_number(rotation, "rotation"))
    return Polygon([(cx + r * math.cos(start + 2 * math.pi * i / sides), cy + r * math.sin(start + 2 * math.pi * i / sides))
                    for i in range(sides)])


def star(cx: float, cy: float, r_outer: float, r_inner: float, points: int = 5, rotation: float = -90.0) -> Polygon:
    """Étoile à ``points`` branches."""
    points = int(points)
    if points < 2 or points > 500:
        raise ScriptError("star : entre 2 et 500 branches.")
    cx, cy = _number(cx, "cx"), _number(cy, "cy")
    ro, ri = _positive(r_outer, "r_outer"), _positive(r_inner, "r_inner")
    start = math.radians(_number(rotation, "rotation"))
    return Polygon([(cx + (ro if i % 2 == 0 else ri) * math.cos(start + math.pi * i / points),
                     cy + (ro if i % 2 == 0 else ri) * math.sin(start + math.pi * i / points)) for i in range(2 * points)])


def polygon(points: Iterable[Iterable[float]], holes: Iterable[Iterable[Iterable[float]]] | None = None) -> Polygon:
    """Polygone à partir de sommets [(x, y), …] (le dernier point n'a pas à répéter le premier)."""
    shell = [(_number(p[0], "x"), _number(p[1], "y")) for p in points]
    if len(shell) < 3:
        raise ScriptError("polygon : au moins 3 sommets.")
    rings = [[(_number(p[0], "x"), _number(p[1], "y")) for p in ring] for ring in (holes or [])]
    shape = Polygon(shell, rings)
    return shape if shape.is_valid else shapely.make_valid(shape)


def polyline(points: Iterable[Iterable[float]], closed: bool = False) -> LineString:
    """Ligne brisée ; ``closed=True`` la referme sur son premier point."""
    coords = [(_number(p[0], "x"), _number(p[1], "y")) for p in points]
    if len(coords) < 2:
        raise ScriptError("polyline : au moins 2 points.")
    if closed and coords[0] != coords[-1]:
        coords.append(coords[0])
    return LineString(coords)


def line(x1: float, y1: float, x2: float, y2: float) -> LineString:
    return LineString([(_number(x1, "x1"), _number(y1, "y1")), (_number(x2, "x2"), _number(y2, "y2"))])


def arc(cx: float, cy: float, r: float, start_deg: float, end_deg: float) -> LineString:
    """Arc de cercle (degrés, sens horaire à l'écran puisque y descend)."""
    cx, cy, r = _number(cx, "cx"), _number(cy, "cy"), _positive(r, "rayon")
    a0, a1 = math.radians(_number(start_deg, "start_deg")), math.radians(_number(end_deg, "end_deg"))
    n = _segments(r, abs(a1 - a0))
    return LineString([(cx + r * math.cos(a0 + (a1 - a0) * i / n), cy + r * math.sin(a0 + (a1 - a0) * i / n)) for i in range(n + 1)])


def slot(cx: float, cy: float, length: float, width: float, angle: float = 0.0, rounded: bool = True) -> Polygon:
    """Lumière (encoche oblongue) centrée en (cx, cy), longueur hors tout ``length``."""
    cx, cy = _number(cx, "cx"), _number(cy, "cy")
    length, width = _positive(length, "longueur"), _positive(width, "largeur")
    length = max(length, width) if rounded else length
    shape = rect(cx - length / 2, cy - width / 2, length, width, width / 2 if rounded else 0.0)
    return affinity.rotate(shape, _number(angle, "angle"), origin=(cx, cy)) if angle else shape


# ---------------------------------------------------------------------------------------------------------
# Transformations, booléens, répétitions
# ---------------------------------------------------------------------------------------------------------
def move(geom: Any, dx: float, dy: float) -> Any:
    return affinity.translate(_geometry(geom), _number(dx, "dx"), _number(dy, "dy"))


def rotate(geom: Any, angle: float, origin: Any = "center") -> Any:
    """Rotation en degrés (sens horaire à l'écran) autour de ``origin`` : "center", "centroid" ou (x, y)."""
    return affinity.rotate(_geometry(geom), _number(angle, "angle"), origin=origin)


def scale(geom: Any, fx: float, fy: float | None = None, origin: Any = "center") -> Any:
    return affinity.scale(_geometry(geom), _number(fx, "fx"), _number(fx if fy is None else fy, "fy"), origin=origin)


def mirror(geom: Any, axis: str = "x", origin: Any = "center") -> Any:
    """Symétrie : axis="x" retourne gauche/droite, axis="y" haut/bas."""
    return affinity.scale(_geometry(geom), -1 if axis == "x" else 1, -1 if axis == "y" else 1, origin=origin)


def union(*geoms: Any) -> Any:
    return unary_union([_geometry(item) for item in _flatten(geoms)])


def difference(a: Any, b: Any) -> Any:
    return _geometry(a).difference(union(b), grid_size=GRID_MM)


def intersection(a: Any, b: Any) -> Any:
    return _geometry(a).intersection(union(b), grid_size=GRID_MM)


def offset(geom: Any, distance: float, join: str = "round") -> Any:
    """Décalage (distance > 0 vers l'extérieur) ; join : "round", "mitre" ou "bevel"."""
    distance = _number(distance, "distance")
    joins = {"round": "round", "mitre": "mitre", "miter": "mitre", "bevel": "bevel"}
    quad = max(2, _segments(abs(distance) or 1.0, math.pi / 2, 2))
    return _geometry(geom).buffer(distance, quad_segs=quad, join_style=joins.get(str(join), "round"), mitre_limit=5.0)


def grid_points(nx: int, ny: int, dx: float, dy: float, x0: float = 0.0, y0: float = 0.0) -> list[tuple[float, float]]:
    """Points d'une grille nx × ny de pas (dx, dy) à partir de (x0, y0)."""
    nx, ny = int(nx), int(ny)
    if nx < 1 or ny < 1 or nx * ny > 200_000:
        raise ScriptError("grid_points : entre 1 et 200 000 points.")
    dx, dy, x0, y0 = _number(dx, "dx"), _number(dy, "dy"), _number(x0, "x0"), _number(y0, "y0")
    return [(x0 + i * dx, y0 + j * dy) for j in range(ny) for i in range(nx)]


def pattern(geom: Any, nx: int, ny: int, dx: float, dy: float) -> list[Any]:
    """Copies d'une forme en grille (liste, à passer telle quelle à engrave/cut)."""
    shape = _geometry(geom)
    return [affinity.translate(shape, x, y) for x, y in grid_points(nx, ny, dx, dy)]


def polar_pattern(geom: Any, count: int, cx: float, cy: float, total_angle: float = 360.0) -> list[Any]:
    """Copies d'une forme tournées autour de (cx, cy), réparties sur ``total_angle`` degrés."""
    count = int(count)
    if count < 1 or count > 10_000:
        raise ScriptError("polar_pattern : entre 1 et 10 000 copies.")
    shape, total = _geometry(geom), _number(total_angle, "total_angle")
    step = total / count if abs(total) >= 360 - 1e-9 else total / max(1, count - 1)
    return [affinity.rotate(shape, step * i, origin=(_number(cx, "cx"), _number(cy, "cy"))) for i in range(count)]


# ---------------------------------------------------------------------------------------------------------
# Encoches et assemblages
# ---------------------------------------------------------------------------------------------------------
def finger_count(length: float, finger: float) -> int:
    """Nombre impair de créneaux sur un bord (au moins 3) pour une largeur visée ``finger``."""
    n = max(3, int(round(_positive(length, "longueur") / _positive(finger, "créneau"))))
    return n if n % 2 else n + 1 if n * finger < length else n - 1


def panel(width: float, height: float, thickness: float, finger: float, edges: str = "ffff", kerf: float = 0.0) -> Any:
    """Panneau à créneaux (boîte à assembler) : rectangle width × height, bords haut, droite, bas, gauche.

    ``edges`` : une lettre par bord — "f" créneaux sortants (tenons), "h" créneaux rentrants (mortaises),
    "s" bord droit. Un bord "f" s'emboîte dans un bord "h" de même longueur. Les créneaux pairs (0, 2, …)
    sortent ou rentrent de ``thickness`` ; ``kerf`` élargit les tenons et resserre les mortaises
    (moitié de la saignée) pour un assemblage serré.
    """
    w, h, t = _positive(width, "largeur"), _positive(height, "hauteur"), _positive(thickness, "épaisseur")
    k = max(0.0, _number(kerf, "kerf"))
    edges = str(edges or "ffff").lower()
    if len(edges) != 4 or any(c not in "fhs" for c in edges):
        raise ScriptError('panel : edges attend 4 lettres parmi "f", "h", "s" (haut, droite, bas, gauche).')
    shape = box(0, 0, w, h)
    adds, cuts = [], []
    for side, kind in enumerate(edges):
        if kind == "s":
            continue
        length = w if side in (0, 2) else h
        n = finger_count(length, finger)
        size = length / n
        for i in range(0, n, 2):
            a, b = i * size - k, (i + 1) * size + k
            if kind == "h":
                a, b = i * size + k, (i + 1) * size - k
            a, b = max(0.0, a), min(length, b)
            if b <= a:
                continue
            depth = t
            if side == 0:
                piece = box(a, -depth, b, 0) if kind == "f" else box(a, 0, b, depth)
            elif side == 1:
                piece = box(w, a, w + depth, b) if kind == "f" else box(w - depth, a, w, b)
            elif side == 2:
                piece = box(w - b, h, w - a, h + depth) if kind == "f" else box(w - b, h - depth, w - a, h)
            else:
                piece = box(-depth, h - b, 0, h - a) if kind == "f" else box(0, h - b, depth, h - a)
            (adds if kind == "f" else cuts).append(piece)
    if adds:
        shape = unary_union([shape, *adds])
    if cuts:
        shape = shape.difference(unary_union(cuts), grid_size=GRID_MM)
    return shape


# ---------------------------------------------------------------------------------------------------------
# QR code
# ---------------------------------------------------------------------------------------------------------
def qr(data: str, x: float, y: float, size: float, style: str = "square", error: str = "m", border: int = 0,
       micro: bool = False) -> Any:
    """QR code (modules sombres) dans un carré ``size`` × ``size`` en (x, y).

    style : "square" (modules fusionnés), "dots" (pastilles), "rounded" (angles extérieurs adoucis).
    ``border`` : marge claire en modules (4 recommandés pour un lecteur exigeant). ``error`` : l, m, q ou h.
    """
    import segno  # importé ici : le module reste utilisable pour la géométrie seule

    text = str(data)
    if not text or len(text) > 2000:
        raise ScriptError("qr : texte de 1 à 2000 caractères.")
    if str(error).lower() not in ("l", "m", "q", "h"):
        raise ScriptError('qr : error vaut "l", "m", "q" ou "h".')
    code = segno.make(text, error=str(error).lower(), micro=bool(micro))
    matrix = [list(row) for row in code.matrix]
    n = len(matrix)
    border = max(0, int(border))
    x, y, size = _number(x, "x"), _number(y, "y"), _positive(size, "taille")
    m = size / (n + 2 * border)
    ox, oy = x + border * m, y + border * m
    style = str(style or "square").lower()
    if style == "dots":
        dot = circle(0, 0, m * 0.45)
        return unary_union([affinity.translate(dot, ox + (c + 0.5) * m, oy + (r + 0.5) * m)
                            for r in range(n) for c in range(n) if matrix[r][c]])
    boxes = []
    for r in range(n):
        c = 0
        while c < n:
            if matrix[r][c]:
                start = c
                while c < n and matrix[r][c]:
                    c += 1
                boxes.append(box(ox + start * m, oy + r * m, ox + c * m, oy + (r + 1) * m))
            c += 1
    shape = unary_union(boxes)
    shape = shapely.set_precision(shape, GRID_MM)
    if style == "rounded":
        quad = max(2, _segments(m * 0.3, math.pi / 2, 2))
        shape = shape.buffer(-m * 0.3, quad_segs=quad).buffer(m * 0.3, quad_segs=quad)
    elif style != "square":
        raise ScriptError('qr : style vaut "square", "dots" ou "rounded".')
    return shape


# ---------------------------------------------------------------------------------------------------------
# Image jointe (mode Image)
# ---------------------------------------------------------------------------------------------------------
def _image_gray():
    path = _STATE.get("image")
    if not path:
        raise ScriptError("Aucune image jointe : passe l'assistant en mode Image et choisis une image.")
    from PIL import Image, ImageOps

    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source)
        image.load()
    if image.mode in ("RGBA", "LA", "PA", "P"):
        image = image.convert("RGBA")
        background = Image.new("RGBA", image.size, (255, 255, 255, 255))
        background.alpha_composite(image)
        image = background
    return image.convert("L")


def image_size() -> tuple[int, int]:
    """Taille de l'image jointe en pixels (largeur, hauteur)."""
    return _image_gray().size


def _placement(ctx: Context, width: float | None, height: float | None, pixels: tuple[int, int]) -> tuple[float, float]:
    aspect = pixels[1] / max(1, pixels[0])
    if width is None and height is None:
        width = min(ctx.width_mm, ctx.height_mm / aspect)
        height = width * aspect
    elif width is None:
        height = _positive(height, "hauteur")
        width = height / aspect
    elif height is None:
        width = _positive(width, "largeur")
        height = width * aspect
    return _positive(width, "largeur"), _positive(height, "hauteur")


def _mask_geometry(mask, pitch: float, x0: float, y0: float):
    """Masque booléen (numpy) → polygones : plages de pixels par ligne, prolongées verticalement."""
    import numpy

    rows, cols = mask.shape
    open_runs: dict[tuple[int, int], int] = {}
    xmin, ymin, xmax, ymax = [], [], [], []
    padded = numpy.zeros(cols + 2, dtype=numpy.int8)
    for r in range(rows + 1):
        runs = set()
        if r < rows:
            padded[1:-1] = mask[r]
            edges = numpy.flatnonzero(numpy.diff(padded))
            runs = {(int(edges[i]), int(edges[i + 1])) for i in range(0, len(edges), 2)}
        for run in list(open_runs):
            if run not in runs:
                start = open_runs.pop(run)
                xmin.append(run[0]); xmax.append(run[1]); ymin.append(start); ymax.append(r)
        for run in runs:
            open_runs.setdefault(run, r)
    if not xmin:
        return GeometryCollection()
    boxes = shapely.box(numpy.array(xmin) * pitch + x0, numpy.array(ymin) * pitch + y0,
                        numpy.array(xmax) * pitch + x0, numpy.array(ymax) * pitch + y0)
    return shapely.set_precision(shapely.union_all(boxes), GRID_MM)


def trace_levels(ctx: Context, levels: int | None = None, width: float | None = None, height: float | None = None,
                 x: float = 0.0, y: float = 0.0, invert: bool = False, contrast: float = 1.0, smooth: bool = True) -> list[Any]:
    """Image jointe → [zones du niveau 1 (clair), …, niveau N (foncé)], en mm, posées en (x, y).

    Sans largeur ni hauteur, l'image remplit au mieux la zone de dessin. La finesse suit ``ctx.detail``
    (pixel de 1 mm à 0,08 mm) ; ``invert`` grave les clairs ; ``contrast`` > 1 durcit les tons ; ``smooth``
    retire les pixels isolés. Les zones sont disjointes et jointives d'un niveau à l'autre.
    """
    import numpy
    from PIL import Image, ImageFilter

    count = int(levels or ctx.levels)
    if count < 1 or count > MAX_LEVELS:
        raise ScriptError(f"trace_levels : entre 1 et {MAX_LEVELS} niveaux.")
    gray = _image_gray()
    width, height = _placement(ctx, width, height, gray.size)
    pitch = max(detail_pitch(ctx.detail), max(width, height) / MAX_TRACE_PX)
    cols, rows = max(1, round(width / pitch)), max(1, round(height / pitch))
    pitch_x, pitch_y = width / cols, height / rows
    small = gray.resize((cols, rows), Image.LANCZOS)
    values = numpy.asarray(small, dtype=numpy.float32) / 255.0
    darkness = values if invert else 1.0 - values
    k = _number(contrast, "contrast")
    if k != 1.0:
        darkness = numpy.clip((darkness - 0.5) * k + 0.5, 0.0, 1.0)
    bands = numpy.minimum((darkness * (count + 1)).astype(numpy.int32), count).astype(numpy.uint8)
    if smooth and min(cols, rows) >= 3:
        bands = numpy.asarray(Image.fromarray(bands, mode="L").filter(ImageFilter.ModeFilter(3)))
    x0, y0 = _number(x, "x"), _number(y, "y")
    result = []
    for level in range(1, count + 1):
        geom = _mask_geometry(bands == level, 1.0, 0.0, 0.0)
        result.append(affinity.affine_transform(geom, [pitch_x, 0, 0, pitch_y, x0, y0]) if not geom.is_empty else geom)
    tol = max(pitch_x, pitch_y) * 0.75
    filled = [index for index, geom in enumerate(result) if not geom.is_empty]
    if filled:
        try:
            simplified = shapely.coverage_simplify([result[i] for i in filled], tol)
        except Exception:  # noqa: BLE001 - couverture imparfaite : simplification indépendante
            simplified = [result[i].simplify(tol, preserve_topology=True) for i in filled]
        for index, geom in zip(filled, simplified):
            result[index] = _drop_small(geom, (max(pitch_x, pitch_y) * 1.5) ** 2)
    return result


def trace(ctx: Context, threshold: float = 0.5, width: float | None = None, height: float | None = None,
          x: float = 0.0, y: float = 0.0, invert: bool = False, smooth: bool = True) -> Any:
    """Image jointe → zones plus sombres que ``threshold`` (0 = blanc … 1 = noir), en mm."""
    import numpy
    from PIL import Image, ImageFilter

    level = _number(threshold, "threshold")
    if not 0.0 < level < 1.0:
        raise ScriptError("trace : threshold entre 0 et 1 (exclus).")
    gray = _image_gray()
    width, height = _placement(ctx, width, height, gray.size)
    pitch = max(detail_pitch(ctx.detail), max(width, height) / MAX_TRACE_PX)
    cols, rows = max(1, round(width / pitch)), max(1, round(height / pitch))
    values = numpy.asarray(gray.resize((cols, rows), Image.LANCZOS), dtype=numpy.float32) / 255.0
    darkness = values if invert else 1.0 - values
    mask = (darkness >= level).astype(numpy.uint8) * 255
    if smooth and min(cols, rows) >= 3:
        mask = numpy.asarray(Image.fromarray(mask, mode="L").filter(ImageFilter.ModeFilter(3)))
    geom = _mask_geometry(mask > 127, 1.0, 0.0, 0.0)
    if geom.is_empty:
        return geom
    pitch_x, pitch_y = width / cols, height / rows
    geom = affinity.affine_transform(geom, [pitch_x, 0, 0, pitch_y, _number(x, "x"), _number(y, "y")])
    tol = max(pitch_x, pitch_y) * 0.75
    return _drop_small(geom.simplify(tol, preserve_topology=True), (max(pitch_x, pitch_y) * 1.5) ** 2)


# ---------------------------------------------------------------------------------------------------------
# Dessin et nettoyage final
# ---------------------------------------------------------------------------------------------------------
def _flatten(items: Any) -> list[Any]:
    if isinstance(items, (list, tuple, set, frozenset)) or (hasattr(items, "__iter__") and not isinstance(items, (str, bytes)) and not hasattr(items, "geom_type")):
        out = []
        for item in items:
            out.extend(_flatten(item))
        return out
    return [items]


def _geometry(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return unary_union([_geometry(item) for item in _flatten(value)])
    if not hasattr(value, "geom_type"):
        raise ScriptError(f"Géométrie shapely attendue (reçu {type(value).__name__}).")
    return value


def _parts(geom: Any) -> tuple[list[Polygon], list[LineString]]:
    """Décompose en polygones valides et lignes."""
    polygons, lines = [], []
    stack = [geom]
    while stack:
        item = stack.pop()
        if item is None or item.is_empty:
            continue
        kind = item.geom_type
        if kind == "Polygon":
            fixed = item if item.is_valid else shapely.make_valid(item)
            if fixed.geom_type == "Polygon":
                polygons.append(fixed)
            else:
                stack.append(fixed)
        elif kind in ("LineString", "LinearRing"):
            lines.append(LineString(item.coords))
        elif kind in ("MultiPolygon", "MultiLineString", "GeometryCollection"):
            stack.extend(item.geoms)
        elif kind in ("Point", "MultiPoint"):
            continue
    return polygons, lines


def _drop_small(geom: Any, min_area: float) -> Any:
    """Retire les polygones et les trous plus petits que ``min_area`` (mm²)."""
    polygons, _ = _parts(geom)
    kept = []
    for poly in polygons:
        if poly.area < min_area:
            continue
        holes = [ring for ring in poly.interiors if Polygon(ring).area >= min_area]
        kept.append(Polygon(poly.exterior, holes) if len(holes) != len(poly.interiors) else poly)
    if not kept:
        return GeometryCollection()
    return kept[0] if len(kept) == 1 else MultiPolygon(kept)


class Drawing:
    """Zone de dessin de ``width`` × ``height`` mm ; y descend, l'origine est en haut à gauche."""

    def __init__(self, width: float, height: float):
        self.width = _positive(width, "largeur du dessin")
        self.height = _positive(height, "hauteur du dessin")
        if self.width > MAX_SIZE_MM or self.height > MAX_SIZE_MM:
            raise ScriptError(f"Dessin limité à {MAX_SIZE_MM:g} × {MAX_SIZE_MM:g} mm.")
        self._engraved: dict[int, list[Any]] = {}
        self._cuts: list[Any] = []
        self._marks: list[Any] = []

    def engrave(self, geom: Any, level: int | None = None) -> "Drawing":
        """Surface gravée au niveau ``level`` (1 = clair … ctx.levels = foncé ; par défaut le plus foncé)."""
        count = _STATE["levels"]
        value = count if level is None else int(level)
        if not 1 <= value <= count:
            raise ScriptError(f"engrave : niveau {value} hors plage (1 à {count}).")
        polygons, lines = _parts(_geometry(geom))
        if lines:
            warn(f"{len(lines)} ligne(s) passée(s) à engrave() tracée(s) en trait : une gravure demande une surface fermée.")
            self._marks.extend(lines)
        self._engraved.setdefault(value, []).extend(polygons)
        return self

    def cut(self, geom: Any) -> "Drawing":
        """Découpe : contours des surfaces (trous compris) et lignes telles quelles."""
        polygons, lines = _parts(_geometry(geom))
        for poly in polygons:
            self._cuts.append(poly.exterior)
            self._cuts.extend(poly.interiors)
        self._cuts.extend(lines)
        return self

    def mark(self, geom: Any) -> "Drawing":
        """Trait gravé (vecteur) : contours des surfaces et lignes."""
        polygons, lines = _parts(_geometry(geom))
        for poly in polygons:
            self._marks.append(poly.exterior)
            self._marks.extend(poly.interiors)
        self._marks.extend(lines)
        return self


def _fmt(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _ring_d(coords: list[tuple[float, float]], closed: bool) -> str:
    points = [(round(x, 3), round(y, 3)) for x, y in coords]
    cleaned = [points[0]]
    for point in points[1:]:
        if point != cleaned[-1]:
            cleaned.append(point)
    if closed and len(cleaned) > 1 and cleaned[0] == cleaned[-1]:
        cleaned.pop()
    if len(cleaned) < (3 if closed else 2):
        return ""
    head = f"M{_fmt(cleaned[0][0])} {_fmt(cleaned[0][1])}"
    body = " ".join(f"{_fmt(x)} {_fmt(y)}" for x, y in cleaned[1:])
    return f"{head}L{body}{'Z' if closed else ''}"


def _polygon_d(poly: Polygon) -> tuple[str, int]:
    poly = orient(poly, 1.0)
    rings = [poly.exterior, *poly.interiors]
    parts = [_ring_d(list(ring.coords), True) for ring in rings]
    parts = [part for part in parts if part]
    return "".join(parts), sum(len(ring.coords) - 1 for ring in rings)


def _clean_lines(lines: list[Any], tol: float) -> list[LineString]:
    if not lines:
        return []
    merged = shapely.set_precision(unary_union(lines), GRID_MM)
    merged = linemerge(merged) if merged.geom_type in ("MultiLineString", "GeometryCollection") else merged
    _, pieces = _parts(merged)
    result = []
    for piece in pieces:
        simplified = piece.simplify(tol * 0.5, preserve_topology=False)
        if simplified.length >= GRID_MM * 10 and len(simplified.coords) >= 2:
            result.append(simplified)
    return result


def _line_d(line: LineString) -> tuple[str, int, bool]:
    coords = list(line.coords)
    closed = len(coords) > 3 and coords[0] == coords[-1]
    return _ring_d(coords, closed), len(coords) - (1 if closed else 0), closed


def _level_color(level: int, count: int) -> str:
    return LEVEL_COLORS[count - level]


def _grey(level: int, count: int) -> str:
    value = round(225 - 205 * level / count) if count > 1 else 20
    return f"#{value:02x}{value:02x}{value:02x}"


def _finalize(drawing: Drawing) -> dict[str, Any]:
    """Dessin → SVG machine, aperçu, calques et statistiques (voir le docstring du module)."""
    if not isinstance(drawing, Drawing):
        raise ScriptError("build() doit renvoyer un Drawing (laser_assist.Drawing).")
    tol, count = tolerance(), _STATE["levels"]
    min_area = max(GRID_MM ** 2 * 100, (tol * 2) ** 2)
    warnings = list(_STATE["warnings"])
    # Gravure : du plus foncé au plus clair, chaque niveau perd ce que les niveaux plus foncés couvrent.
    areas: dict[int, Any] = {}
    covered = None
    for level in sorted(drawing._engraved, reverse=True):
        geom = shapely.set_precision(unary_union(drawing._engraved[level]), GRID_MM)
        if covered is not None and not geom.is_empty:
            geom = geom.difference(covered, grid_size=GRID_MM)
        if geom.is_empty:
            continue
        covered = geom if covered is None else unary_union([covered, geom])
        areas[level] = geom
    if areas:
        levels = sorted(areas)
        try:
            simplified = shapely.coverage_simplify([areas[level] for level in levels], tol, simplify_boundary=True)
        except Exception:  # noqa: BLE001 - couverture imparfaite : simplification indépendante
            simplified = [areas[level].simplify(tol, preserve_topology=True) for level in levels]
        for level, geom in zip(levels, simplified):
            areas[level] = _drop_small(shapely.set_precision(geom, GRID_MM), min_area)
    cuts = _clean_lines(drawing._cuts, tol)
    marks = _clean_lines(drawing._marks, tol)
    if cuts and marks:
        cut_geom = unary_union(cuts)
        marks = _clean_lines([line.difference(cut_geom, grid_size=GRID_MM) for line in marks], tol)
    layers, groups, preview, stats = [], [], [], {"paths": 0, "nodes": 0, "closed": 0, "open": 0, "levels": {}}
    for level in sorted(areas, reverse=True):
        polygons, _ = _parts(areas[level])
        if not polygons:
            continue
        color = _level_color(level, count)
        items = [_polygon_d(poly) for poly in polygons]
        items = [(d, n) for d, n in items if d]
        if not items:
            continue
        nodes = sum(n for _, n in items)
        paths = [d for d, _ in items] if len(items) <= MERGE_ELEMENTS_ABOVE else ["".join(d for d, _ in items)]
        area = round(sum(poly.area for poly in polygons), 2)
        layers.append({"color": color, "kind": "engrave", "level": level, "name": f"Niveau {level}/{count}",
                       "shapes": len(polygons), "nodes": nodes, "area_mm2": area})
        body = "".join(f'<path d="{d}"/>' for d in paths)
        groups.append(f'<g id="niveau-{level}" data-level="{level}" fill="{color}" stroke="none" fill-rule="evenodd">{body}</g>')
        preview.append(f'<g fill="{_grey(level, count)}" stroke="none" fill-rule="evenodd">{body}</g>')
        stats["paths"] += len(items)
        stats["closed"] += sum(1 + len(poly.interiors) for poly in polygons)
        stats["nodes"] += nodes
        stats["levels"][str(level)] = {"shapes": len(polygons), "area_mm2": area}
    for kind, color, name, lines in (("cut", CUT_COLOR, "Découpe", cuts), ("mark", MARK_COLOR, "Trait", marks)):
        items = [_line_d(line) for line in lines]
        items = [item for item in items if item[0]]
        if not items:
            continue
        closed = sum(1 for _, _, is_closed in items if is_closed)
        nodes = sum(n for _, n, _ in items)
        paths = [d for d, _, _ in items] if len(items) <= MERGE_ELEMENTS_ABOVE else [" ".join(d for d, _, _ in items)]
        layers.append({"color": color, "kind": kind, "name": name, "shapes": len(items), "nodes": nodes,
                       "closed": closed, "open": len(items) - closed})
        body = "".join(f'<path d="{d}"/>' for d in paths)
        groups.append(f'<g id="{"decoupe" if kind == "cut" else "trait"}" fill="none" stroke="{color}" stroke-width="0.1">{body}</g>')
        preview.append(f'<g fill="none" stroke="{color}" stroke-width="1.2" vector-effect="non-scaling-stroke">{body}</g>')
        stats["paths"] += len(items)
        stats["closed"] += closed
        stats["open"] += len(items) - closed
        stats["nodes"] += nodes
        if kind == "cut" and len(items) - closed:
            warnings.append(f"{len(items) - closed} tracé(s) de découpe ouvert(s) : vérifie qu'ils sont voulus (lignes de refente).")
    if not groups:
        raise ScriptError("Le dessin est vide : rien à graver, découper ni tracer.")
    everything = [geom for geom in list(areas.values()) + cuts + marks if not geom.is_empty]
    minx, miny, maxx, maxy = unary_union(everything).bounds
    if minx < -GRID_MM or miny < -GRID_MM or maxx > drawing.width + GRID_MM or maxy > drawing.height + GRID_MM:
        warnings.append(f"Le dessin déborde de sa zone ({_fmt(drawing.width)} × {_fmt(drawing.height)} mm) : "
                        f"étendue réelle {_fmt(minx)}…{_fmt(maxx)} × {_fmt(miny)}…{_fmt(maxy)} mm.")
    stats["bounds"] = [round(minx, 3), round(miny, 3), round(maxx, 3), round(maxy, 3)]
    w, h = _fmt(drawing.width), _fmt(drawing.height)
    head = f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}">'
    svg = head + "".join(groups) + "</svg>\n"
    frame = f'<rect x="0" y="0" width="{w}" height="{h}" fill="#ffffff" stroke="#c8c8c8" stroke-width="1" vector-effect="non-scaling-stroke"/>'
    preview_svg = head + frame + "".join(preview) + "</svg>\n"
    return {"svg": svg, "preview_svg": preview_svg, "layers": layers, "stats": stats, "warnings": warnings,
            "width_mm": drawing.width, "height_mm": drawing.height, "tolerance_mm": tol}


__all__ = ["Drawing", "ScriptError", "Context", "tolerance", "warn", "detail_tolerance", "detail_pitch", "rect", "circle",
           "ellipse", "regular_polygon", "star", "polygon", "polyline", "line", "arc", "slot", "move", "rotate", "scale",
           "mirror", "union", "difference", "intersection", "offset", "grid_points", "pattern", "polar_pattern",
           "finger_count", "panel", "qr", "image_size", "trace_levels", "trace", "Polygon", "MultiPolygon", "LineString",
           "MultiLineString", "LinearRing", "Point", "box", "affinity", "unary_union"]
