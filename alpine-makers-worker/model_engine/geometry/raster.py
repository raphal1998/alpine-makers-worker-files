"""Rastérisation de Paths (oracle indépendant des tests, vignettes).

Deux moteurs :
- ``winding`` (défaut, numpy pur) : un pixel est plein si son centre a un indice d'enroulement > 0, calculé par
  rayons horizontaux (règle positive exacte, rings auto-intersectants compris). Échantillonnage des centres :
  masque(A ∩ B) = masque(A) & masque(B) pixel à pixel, ce qui en fait un oracle fidèle pour les booléennes.
- ``pillow`` (``ImageDraw.polygon``, chaque ring compté +1 CCW / −1 CW) : remplissage inclusif (un pixel touché
  par le contour est plein, soit une bande d'un pixel en plus) et pair-impair par ring : pour les vignettes.
Convention : ``bbox_mm = (xmin, ymin, xmax, ymax)``, ligne 0 = ymin (axe y vers le haut), colonne 0 = xmin,
centre du pixel (i, j) = (xmin + (j + 0,5)/ppm, ymin + (i + 0,5)/ppm)."""
import numpy as np
from PIL import Image, ImageDraw

from . import SCALE
from .poly import area2, as_ring


def _grid(px_per_mm: float, bbox_mm):
    xmin, ymin, xmax, ymax = (float(v) for v in bbox_mm)
    width = max(1, int(round((xmax - xmin) * px_per_mm)))
    height = max(1, int(round((ymax - ymin) * px_per_mm)))
    return xmin, ymin, width, height


def _rasterize_winding(paths, px_per_mm, bbox_mm):
    xmin, ymin, width, height = _grid(px_per_mm, bbox_mm)
    step = SCALE / px_per_mm
    xs = xmin * SCALE + (np.arange(width) + 0.5) * step
    ys = ymin * SCALE + (np.arange(height) + 0.5) * step
    toggles = np.zeros((height, width + 1), dtype=np.int32)
    for ring in paths:
        ring = as_ring(ring)
        if len(ring) < 3:
            continue
        p = ring.astype(np.float64)
        q = np.roll(p, -1, axis=0)
        x1, y1, x2, y2 = p[:, 0], p[:, 1], q[:, 0], q[:, 1]
        keep = y1 != y2
        if not keep.any():
            continue
        x1, y1, x2, y2 = x1[keep], y1[keep], x2[keep], y2[keep]
        lo = np.minimum(y1, y2)
        hi = np.maximum(y1, y2)
        r0 = np.searchsorted(ys, lo, side="left")
        r1 = np.searchsorted(ys, hi, side="left")  # lignes avec lo ≤ y_c < hi
        cnt = r1 - r0
        tot = int(cnt.sum())
        if not tot:
            continue
        e = np.repeat(np.arange(len(x1)), cnt)
        rows = np.repeat(r0, cnt) + (np.arange(tot) - np.repeat(np.cumsum(cnt) - cnt, cnt))
        yc = ys[rows]
        xcross = x1[e] + (yc - y1[e]) * (x2[e] - x1[e]) / (y2[e] - y1[e])
        cols = np.searchsorted(xs, xcross, side="right")  # premiers centres strictement à droite du croisement
        # Rayon vers −x depuis le centre : arête descendante → +1, montante → −1 (CCW = +1 à l'intérieur).
        sign = np.where(y2[e] < y1[e], 1, -1).astype(np.int32)
        np.add.at(toggles, (rows, cols), sign)
    return np.cumsum(toggles[:, :width], axis=1) > 0


def _rasterize_pillow(paths, px_per_mm, bbox_mm):
    xmin, ymin, width, height = _grid(px_per_mm, bbox_mm)
    acc = np.zeros((height, width), dtype=np.int16)
    scale = px_per_mm / SCALE
    for ring in paths:
        ring = as_ring(ring)
        if len(ring) < 3:
            continue
        a2 = area2(ring)
        if a2 == 0:
            continue
        pts = ring.astype(np.float64)
        xs = (pts[:, 0] - xmin * SCALE) * scale - 0.5
        ys = (pts[:, 1] - ymin * SCALE) * scale - 0.5
        img = Image.new("1", (width, height), 0)
        ImageDraw.Draw(img).polygon(list(zip(xs.tolist(), ys.tolist())), fill=1, outline=None)
        mask = np.array(img, dtype=bool)
        if a2 > 0:
            acc[mask] += 1
        else:
            acc[mask] -= 1
    return acc > 0


def rasterize(paths, px_per_mm: float, bbox_mm, engine: str = "winding") -> np.ndarray:
    """Masque booléen ``(hauteur, largeur)`` du Paths (règle positive). ``px_per_mm`` : pixels par mm
    (100 = 0,01 mm/px). ``engine`` : ``winding`` (exact, défaut) ou ``pillow`` (vignettes)."""
    if engine == "winding":
        return _rasterize_winding(paths, px_per_mm, bbox_mm)
    if engine == "pillow":
        return _rasterize_pillow(paths, px_per_mm, bbox_mm)
    raise ValueError(f"moteur de rastérisation inconnu : {engine}")


def area_px(mask) -> int:
    """Nombre de pixels vrais."""
    return int(np.count_nonzero(mask))


def iou(a, b) -> float:
    """Intersection sur union de deux masques booléens (1,0 si les deux sont vides)."""
    a = np.asarray(a, dtype=bool)
    b = np.asarray(b, dtype=bool)
    union = np.count_nonzero(a | b)
    if union == 0:
        return 1.0
    return float(np.count_nonzero(a & b)) / float(union)


def to_image(mask) -> Image.Image:
    """Image Pillow « L » (blanc = plein), ligne du haut = ymax, pour une vignette."""
    arr = (np.asarray(mask, dtype=bool)[::-1] * 255).astype(np.uint8)
    return Image.fromarray(arr, mode="L")
