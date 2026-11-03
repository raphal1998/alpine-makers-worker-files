"""Vignettes du fichier tranché : rendu isométrique ombré d'un ensemble de maillages avec numpy + Pillow (§6.4).

Entrées en types simples : une liste d'objets ``{id, vertices (n, 3) mm, faces (m, 3), color}`` (dict ou objet à
attributs). Rendu par z-buffer numpy entièrement vectorisé (aucune boucle par triangle) : pour chaque triangle, les
pixels candidats de sa boîte englobante sont engendrés en bloc, testés par coordonnées barycentriques, puis le pixel
le plus proche de la caméra l'emporte. Le coût est borné par le nombre de pixels couverts, pas par le nombre de
triangles ; au-delà d'un budget, le rendu se fait à résolution réduite puis est agrandi.

Cinq images par plateau : ``plate_N.png`` (512², ombré, fond transparent), ``plate_N_small.png`` (128²),
``plate_no_light_N.png`` (sans éclairage), ``top_N.png`` (vue de dessus) et ``pick_N.png`` (identifiant d'objet
dans le canal rouge, octet haut dans le vert). Code Alpine Makers : numpy, Pillow ≤ 11.0 et bibliothèque standard.
"""
from __future__ import annotations

import io
import math
import re

import numpy as np
from PIL import Image, ImageDraw

DEFAULT_SIZE = 512
SMALL_SIZE = 128
DEFAULT_COLOR = (150, 160, 175)
BED_FILL = (40, 46, 54, 150)          # plateau semi-transparent sous les objets
BED_OUTLINE = (120, 130, 145, 220)
EXCLUDE_FILL = (120, 60, 60, 110)
BED_DEFAULT_MM = (256.0, 256.0, 256.0)
ISO_AZIMUTH_DEG = 225.0              # caméra devant à gauche (y = 0 est l'avant de l'imprimante)
ISO_ELEVATION_DEG = 32.0
LIGHT_DIR = (-0.45, -0.55, 0.85)     # lumière avant-gauche-dessus
AMBIENT = 0.38
CANDIDATE_BUDGET = 1_000_000         # pixels candidats par lot (mémoire bornée)
TOTAL_CANDIDATE_BUDGET = 24_000_000  # au-delà : rendu à résolution réduite
MAX_FACES = 4_000_000                # au-delà : décimation (garde un triangle sur k)


def _rgb(value) -> tuple[int, int, int]:
    """Couleur « #RRGGBB » / « RRGGBB » / (r, g, b) → tuple d'entiers ; gris par défaut."""
    if value is None:
        return DEFAULT_COLOR
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        return tuple(int(max(0, min(255, round(float(c))))) for c in value[:3])
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?", str(value).strip())
    if not match:
        return DEFAULT_COLOR
    raw = match.group(1)
    return tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))


def _get(item, key, default=None):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _as_object(item, index):
    """(identifiant, sommets float64 (n, 3), faces int64 (m, 3), couleur) d'une entrée dict ou objet."""
    try:
        vertices = np.asarray(_get(item, "vertices"), dtype=np.float64).reshape(-1, 3)
        faces = np.asarray(_get(item, "faces"), dtype=np.int64).reshape(-1, 3)
    except (TypeError, ValueError):
        raise ValueError(f"vignette : sommets ou faces mal formés pour l'objet {index + 1} ((n, 3) attendu)") from None
    if len(faces) and (faces.min() < 0 or faces.max() >= len(vertices)):
        raise ValueError(f"vignette : indices de faces hors des sommets pour l'objet {index + 1}")
    bad = ~np.isfinite(vertices).all(axis=1)
    if bad.any():                                   # sommets NaN/inf : leurs triangles sont retirés, eux ramenés à 0
        faces = faces[~bad[faces].any(axis=1)] if len(faces) else faces
        vertices = vertices.copy()
        vertices[bad] = 0.0
    ident = _get(item, "id", None)
    ident = int(ident) if ident is not None else index + 1
    color = _rgb(_get(item, "color", None) if _get(item, "color", None) is not None else _get(item, "colour", None))
    return ident, vertices, faces, color


def decimate(vertices, faces, max_faces=MAX_FACES):
    """Garde un triangle sur k pour rester sous ``max_faces`` (rendu approximatif pour des maillages énormes)."""
    faces = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
    if len(faces) <= max_faces:
        return np.asarray(vertices, dtype=np.float64).reshape(-1, 3), faces
    stride = int(math.ceil(len(faces) / max_faces))
    return np.asarray(vertices, dtype=np.float64).reshape(-1, 3), faces[::stride]


# ------------------------------------------------------------------------------------------------ caméras

def _basis(view: str):
    """(droite, haut, avant) : vecteurs unitaires de la caméra orthographique."""
    if view == "top":
        return np.array([1.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0]), np.array([0.0, 0.0, -1.0])
    az, el = math.radians(ISO_AZIMUTH_DEG), math.radians(ISO_ELEVATION_DEG)
    camera = np.array([math.cos(az) * math.cos(el), math.sin(az) * math.cos(el), math.sin(el)])
    forward = -camera
    right = np.cross(forward, np.array([0.0, 0.0, 1.0]))
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    return right, up, forward


def _framing(view: str, size: int, bed_mm, z_max: float, margin: float = 0.04):
    """Centre et échelle (px/mm) pour que le plateau (et la hauteur des objets) tienne dans l'image."""
    width, depth = float(bed_mm[0]), float(bed_mm[1])
    right, up, forward = _basis(view)
    if view == "top":
        corners = np.array([[0, 0, 0], [width, 0, 0], [width, depth, 0], [0, depth, 0]], dtype=np.float64)
    else:
        top = max(float(z_max), 1.0)
        corners = np.array([[x, y, z] for x in (0.0, width) for y in (0.0, depth) for z in (0.0, top)])
    sx, sy = corners @ right, corners @ up
    extent_x, extent_y = max(sx.max() - sx.min(), 1e-6), max(sy.max() - sy.min(), 1e-6)
    scale = min(size * (1 - 2 * margin) / extent_x, size * (1 - 2 * margin) / extent_y)
    center = np.array([(sx.max() + sx.min()) / 2, (sy.max() + sy.min()) / 2])
    return right, up, forward, center, scale


def _project(points, right, up, forward, center, scale, size):
    """Points (n, 3) mm → (px x, px y, profondeur) ; y image vers le bas."""
    sx = (points @ right - center[0]) * scale + size / 2.0
    sy = size / 2.0 - (points @ up - center[1]) * scale
    depth = points @ forward
    return sx, sy, depth


# ------------------------------------------------------------------------------------------------ z-buffer

def _rasterize(px, py, depth, size: int) -> np.ndarray:
    """Indice du triangle visible par pixel (−1 = vide). ``px``/``py`` : (m, 3) en pixels ; ``depth`` : (m, 3)."""
    index = np.full(size * size, -1, dtype=np.int64)
    zbuf = np.full(size * size, np.inf, dtype=np.float64)
    if len(px) == 0:
        return index.reshape(size, size)
    x0 = np.clip(np.floor(px.min(axis=1)), 0, size - 1).astype(np.int64)
    x1 = np.clip(np.ceil(px.max(axis=1)), 0, size - 1).astype(np.int64)
    y0 = np.clip(np.floor(py.min(axis=1)), 0, size - 1).astype(np.int64)
    y1 = np.clip(np.ceil(py.max(axis=1)), 0, size - 1).astype(np.int64)
    visible = (px.max(axis=1) >= 0) & (px.min(axis=1) <= size) & (py.max(axis=1) >= 0) & (py.min(axis=1) <= size)
    widths = np.where(visible, x1 - x0 + 1, 0)
    heights = np.where(visible, y1 - y0 + 1, 0)
    areas = widths * heights
    # Pixels des centroïdes : garantit qu'un triangle plus fin qu'un pixel marque quand même l'image.
    cx = np.clip(np.floor(px.mean(axis=1)), 0, size - 1).astype(np.int64)
    cy = np.clip(np.floor(py.mean(axis=1)), 0, size - 1).astype(np.int64)
    centroid_keys = (cy * size + cx)[visible]
    centroid_depth = depth.mean(axis=1)[visible]
    centroid_tris = np.nonzero(visible)[0]
    _commit(index, zbuf, centroid_keys, centroid_depth, centroid_tris)
    # Lots de triangles bornés en pixels candidats.
    cumulative = np.cumsum(areas)
    start = 0
    total = len(px)
    while start < total:
        base = cumulative[start - 1] if start else 0
        end = int(np.searchsorted(cumulative, base + CANDIDATE_BUDGET, side="right"))
        end = max(end, start + 1)
        end = min(end, total)
        _rasterize_chunk(index, zbuf, px, py, depth, x0, y0, widths, areas, start, end, size)
        start = end
    return index.reshape(size, size)


def _rasterize_chunk(index, zbuf, px, py, depth, x0, y0, widths, areas, start, end, size):
    tri_ids = np.arange(start, end)
    counts = areas[start:end]
    total = int(counts.sum())
    if total == 0:
        return
    tri = np.repeat(tri_ids, counts)
    offsets = np.repeat(np.cumsum(counts) - counts, counts)
    local = np.arange(total) - offsets
    w = widths[tri]
    pixel_x = x0[tri] + local % w
    pixel_y = y0[tri] + local // w
    sx, sy = pixel_x + 0.5, pixel_y + 0.5
    ax, ay, bx, by, cx, cy = px[tri, 0], py[tri, 0], px[tri, 1], py[tri, 1], px[tri, 2], py[tri, 2]
    det = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    w0 = (bx - sx) * (cy - sy) - (by - sy) * (cx - sx)
    w1 = (cx - sx) * (ay - sy) - (cy - sy) * (ax - sx)
    w2 = det - w0 - w1
    sign = np.sign(det)
    inside = (det != 0) & (w0 * sign >= -1e-9) & (w1 * sign >= -1e-9) & (w2 * sign >= -1e-9)
    if not inside.any():
        return
    tri, w0, w1, w2, det = tri[inside], w0[inside], w1[inside], w2[inside], det[inside]
    z = (w0 * depth[tri, 0] + w1 * depth[tri, 1] + w2 * depth[tri, 2]) / det
    keys = pixel_y[inside] * size + pixel_x[inside]
    _commit(index, zbuf, keys, z, tri)


def _commit(index, zbuf, keys, z, tri):
    """Garde par pixel le candidat le plus proche, puis le compare au z-buffer global."""
    if len(keys) == 0:
        return
    order = np.lexsort((z, keys))
    keys_sorted = keys[order]
    first = np.ones(len(order), dtype=bool)
    first[1:] = keys_sorted[1:] != keys_sorted[:-1]
    chosen = order[first]
    k = keys[chosen]
    better = z[chosen] < zbuf[k]
    k, chosen = k[better], chosen[better]
    zbuf[k] = z[chosen]
    index[k] = tri[chosen]


# ------------------------------------------------------------------------------------------------ scène

class _Scene:
    """Maillages concaténés, couleur et identifiant par triangle, ombrage par triangle."""

    def __init__(self, objects, max_faces=MAX_FACES):
        vertices, faces, tri_object, ids, colors = [], [], [], [], []
        offset = 0
        for i, item in enumerate(objects):
            ident, v, f, color = _as_object(item, i)
            v, f = decimate(v, f, max_faces=max(1, max_faces // max(1, len(objects))))
            if len(f) == 0 or len(v) == 0:
                continue
            vertices.append(v)
            faces.append(f + offset)
            tri_object.append(np.full(len(f), len(ids), dtype=np.int64))
            ids.append(ident)
            colors.append(color)
            offset += len(v)
        self.vertices = np.concatenate(vertices) if vertices else np.zeros((0, 3))
        self.faces = np.concatenate(faces) if faces else np.zeros((0, 3), dtype=np.int64)
        self.tri_object = np.concatenate(tri_object) if tri_object else np.zeros(0, dtype=np.int64)
        self.ids = np.asarray(ids, dtype=np.int64)
        self.colors = np.asarray(colors, dtype=np.float64).reshape(-1, 3)
        heights = self.vertices[:, 2][np.isfinite(self.vertices[:, 2])] if len(self.vertices) else self.vertices[:, 2]
        self.z_max = float(heights.max()) if len(heights) else 0.0
        self.shade = self._shading()

    def _shading(self):
        """Ombrage de Lambert par triangle ; les triangles dégénérés (aire nulle) sont retirés de la scène."""
        if len(self.faces) == 0:
            return np.zeros(0)
        a = self.vertices[self.faces[:, 0]]
        n = np.cross(self.vertices[self.faces[:, 1]] - a, self.vertices[self.faces[:, 2]] - a)
        norm = np.linalg.norm(n, axis=1)
        keep = norm > 1e-12
        if not keep.all():
            self.faces, self.tri_object, n, norm = self.faces[keep], self.tri_object[keep], n[keep], norm[keep]
        if len(self.faces) == 0:
            return np.zeros(0)
        n /= norm[:, None]
        light = np.asarray(LIGHT_DIR, dtype=np.float64)
        light /= np.linalg.norm(light)
        # |n·l| : les faces vues de dos (maillages ouverts) restent lisibles au lieu d'être noires.
        return np.clip(AMBIENT + (1.0 - AMBIENT) * np.abs(n @ light), 0.0, 1.0)

    def index_buffer(self, view: str, size: int, bed_mm):
        """Indice de triangle par pixel pour une vue (rendu à résolution réduite si le budget est dépassé)."""
        right, up, forward, center, scale = _framing(view, size, bed_mm, self.z_max)
        if len(self.faces) == 0:
            return np.full((size, size), -1, dtype=np.int64)
        sx, sy, depth = _project(self.vertices, right, up, forward, center, scale, size)
        px, py, pz = sx[self.faces], sy[self.faces], depth[self.faces]
        spans = (np.ceil(px.max(axis=1)) - np.floor(px.min(axis=1)) + 1) * (np.ceil(py.max(axis=1)) - np.floor(py.min(axis=1)) + 1)
        total = float(np.clip(spans, 0, size * size).sum())
        if total > TOTAL_CANDIDATE_BUDGET:
            factor = max(1, int(math.ceil(math.sqrt(total / TOTAL_CANDIDATE_BUDGET))))
            small = max(32, size // factor)
            buffer = _rasterize(px * (small / size), py * (small / size), pz, small)
            rows = (np.arange(size) * small) // size
            return buffer[rows[:, None], rows[None, :]]
        return _rasterize(px, py, pz, size)


def _bed_layer(view: str, size: int, bed_mm, exclude_mm, z_max: float) -> Image.Image:
    """Plateau semi-transparent (et zone exclue) sous les objets."""
    right, up, forward, center, scale = _framing(view, size, bed_mm, z_max)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def polygon(points_mm):
        pts = np.asarray([[x, y, 0.0] for x, y in points_mm], dtype=np.float64)
        sx, sy, _ = _project(pts, right, up, forward, center, scale, size)
        return [(float(x), float(y)) for x, y in zip(sx, sy)]

    width, depth = float(bed_mm[0]), float(bed_mm[1])
    draw.polygon(polygon([(0, 0), (width, 0), (width, depth), (0, depth)]), fill=BED_FILL, outline=BED_OUTLINE)
    exclude = [float(v) for v in exclude_mm] if exclude_mm is not None else []
    if len(exclude) == 4:                         # (x0, y0, x1, y1) ; toute autre forme est ignorée (pas de zone exclue)
        x0, y0, x1, y1 = exclude
        draw.polygon(polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]), fill=EXCLUDE_FILL)
    return image


def _compose(scene: _Scene, index: np.ndarray, mode: str) -> Image.Image:
    """Image RGBA à partir de l'indice de triangle par pixel : ombré, sans éclairage ou carte de sélection."""
    size = index.shape[0]
    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    hit = index >= 0
    if hit.any():
        tri = index[hit]
        obj = scene.tri_object[tri]
        if mode == "pick":
            ids = scene.ids[obj]
            rgba[hit, 0] = (ids & 0xFF).astype(np.uint8)
            rgba[hit, 1] = ((ids >> 8) & 0xFF).astype(np.uint8)
        else:
            base = scene.colors[obj]
            if mode == "shaded":
                base = base * scene.shade[tri][:, None]
            else:
                base = base * 0.92
            rgba[hit, :3] = np.clip(base, 0, 255).astype(np.uint8)
        rgba[hit, 3] = 255
    return Image.fromarray(rgba, "RGBA")


# ------------------------------------------------------------------------------------------------ API

def render(objects, size=DEFAULT_SIZE, *, mode="shaded", view="iso", bed_mm=BED_DEFAULT_MM, exclude_mm=None,
           bed_layer=True, max_faces=MAX_FACES) -> Image.Image:
    """Image RGBA (``size`` × ``size``) des objets sur le plateau.

    ``mode`` : « shaded » (ombré), « no_light » (couleur plate) ou « pick » (identifiant d'objet dans le rouge, octet
    haut dans le vert, fond transparent, sans plateau). ``view`` : « iso » ou « top ». ``bed_mm`` = (largeur,
    profondeur, hauteur) ; ``exclude_mm`` = (x0, y0, x1, y1) ou None. Fond transparent.
    """
    if mode not in ("shaded", "no_light", "pick"):
        raise ValueError(f"mode de vignette inconnu : {mode}")
    if view not in ("iso", "top"):
        raise ValueError(f"vue de vignette inconnue : {view}")
    scene = _Scene(objects, max_faces=max_faces)
    image = _compose(scene, scene.index_buffer(view, int(size), bed_mm), mode)
    if bed_layer and mode != "pick":
        image = Image.alpha_composite(_bed_layer(view, int(size), bed_mm, exclude_mm, scene.z_max), image)
    return image


def render_set(objects, *, size=DEFAULT_SIZE, small_size=SMALL_SIZE, bed_mm=BED_DEFAULT_MM, exclude_mm=None,
               max_faces=MAX_FACES) -> dict:
    """Les cinq images d'un plateau : ``plate``, ``small``, ``no_light``, ``top``, ``pick`` (deux rastérisations)."""
    scene = _Scene(objects, max_faces=max_faces)
    size, small_size = int(size), int(small_size)
    iso = scene.index_buffer("iso", size, bed_mm)
    bed = _bed_layer("iso", size, bed_mm, exclude_mm, scene.z_max)
    plate = Image.alpha_composite(bed, _compose(scene, iso, "shaded"))
    no_light = Image.alpha_composite(bed, _compose(scene, iso, "no_light"))
    pick = _compose(scene, iso, "pick")
    top_index = scene.index_buffer("top", size, bed_mm)
    top = Image.alpha_composite(_bed_layer("top", size, bed_mm, exclude_mm, scene.z_max), _compose(scene, top_index, "shaded"))
    small = plate.resize((small_size, small_size), Image.Resampling.LANCZOS)
    return {"plate": plate, "small": small, "no_light": no_light, "top": top, "pick": pick}


def png_bytes(image: Image.Image) -> bytes:
    """PNG (RGBA) en mémoire."""
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def thumbnail_pngs(objects, **kwargs) -> dict:
    """``render_set`` encodé en PNG : ``{plate, small, no_light, top, pick} -> bytes``."""
    return {name: png_bytes(image) for name, image in render_set(objects, **kwargs).items()}


__all__ = ["DEFAULT_SIZE", "SMALL_SIZE", "decimate", "png_bytes", "render", "render_set", "thumbnail_pngs"]
