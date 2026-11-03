"""Primitives sur les rings et les Paths (µm entiers, int64) : aires, orientation, boîtes, simplification,
nettoyage, enroulement, imbrication en PolyTree, transformations, enveloppe convexe."""
import math

import numpy as np

from . import SCALE

INT = np.int64


def as_ring(points) -> np.ndarray:
    """Convertit une séquence de points en Ring int64 ``(n, 2)`` (copie au besoin)."""
    ring = np.asarray(points, dtype=INT)
    if ring.ndim != 2 or ring.shape[1] != 2:
        raise ValueError("un ring est un tableau (n, 2)")
    return ring


def area2(ring) -> int:
    """Deux fois l'aire signée (µm²), > 0 pour le sens trigonométrique."""
    ring = as_ring(ring)
    if len(ring) < 3:
        return 0
    x, y = ring[:, 0], ring[:, 1]
    xn, yn = np.roll(x, -1), np.roll(y, -1)
    return int(np.sum(x * yn - xn * y, dtype=INT))


def area_um2(ring) -> float:
    """Aire signée en µm²."""
    return area2(ring) / 2.0


def area_mm2(ring) -> float:
    """Aire signée en mm²."""
    return area2(ring) / (2.0 * SCALE * SCALE)


def paths_area_mm2(paths) -> float:
    """Aire signée totale d'un Paths propre (extérieurs − trous) en mm²."""
    return float(sum(area_mm2(r) for r in paths))


def is_ccw(ring) -> bool:
    """Vrai si le ring tourne dans le sens trigonométrique (contour extérieur)."""
    return area2(ring) > 0


def reverse(ring) -> np.ndarray:
    """Inverse le sens de parcours."""
    return np.ascontiguousarray(as_ring(ring)[::-1])


def bbox(shape):
    """Boîte ``(xmin, ymin, xmax, ymax)`` d'un ring ou d'un Paths ; None si vide."""
    if isinstance(shape, np.ndarray):
        rings = [shape] if len(shape) else []
    else:
        rings = [as_ring(r) for r in shape if len(r)]
    if not rings:
        return None
    mins = np.min([r.min(axis=0) for r in rings], axis=0)
    maxs = np.max([r.max(axis=0) for r in rings], axis=0)
    return int(mins[0]), int(mins[1]), int(maxs[0]), int(maxs[1])


def perimeter_um(ring) -> float:
    """Longueur du contour fermé en µm."""
    ring = as_ring(ring)
    if len(ring) < 2:
        return 0.0
    d = np.roll(ring, -1, axis=0) - ring
    return float(np.sum(np.hypot(d[:, 0].astype(np.float64), d[:, 1].astype(np.float64))))


def centroid(ring):
    """Centre de gravité ``(x, y)`` en µm (flottants) ; moyenne des sommets si l'aire est nulle."""
    ring = as_ring(ring)
    a2 = area2(ring)
    if a2 == 0:
        m = ring.mean(axis=0)
        return float(m[0]), float(m[1])
    x, y = ring[:, 0], ring[:, 1]
    xn, yn = np.roll(x, -1), np.roll(y, -1)
    cross = (x * yn - xn * y).astype(np.float64)
    cx = float(np.sum((x + xn) * cross)) / (3.0 * a2)
    cy = float(np.sum((y + yn) * cross)) / (3.0 * a2)
    return cx, cy


def _dedupe_consecutive(ring: np.ndarray, eps_um: int) -> np.ndarray:
    """Retire les sommets consécutifs confondus (distance de Tchebychev ≤ eps), fermeture comprise."""
    if len(ring) == 0:
        return ring
    d = np.abs(np.roll(ring, -1, axis=0) - ring).max(axis=1)
    keep = d > eps_um
    out = ring[keep]
    return out


def _remove_collinear(ring: np.ndarray) -> np.ndarray:
    """Retire les sommets alignés avec leurs voisins (produit vectoriel nul), y compris les retours en arrière."""
    n = len(ring)
    while n >= 3:
        prev_ = np.roll(ring, 1, axis=0)
        next_ = np.roll(ring, -1, axis=0)
        cross = (ring[:, 0] - prev_[:, 0]) * (next_[:, 1] - ring[:, 1]) - (ring[:, 1] - prev_[:, 1]) * (next_[:, 0] - ring[:, 0])
        keep = cross != 0
        if keep.all():
            break
        # Un seul passage retire des voisins alignés contigus ; on itère pour les chaînes (A-B-C-D alignés).
        # Retirer tous les alignés d'un coup peut supprimer deux points consécutifs d'un même segment : accepté,
        # car ils sont alors tous sur la même droite entre leurs voisins conservés.
        ring = ring[keep]
        n = len(ring)
    return ring


def clean(ring, eps_um: int = 1):
    """Ring sans sommets confondus (≤ eps) ni alignés ; None si moins de 3 points ou aire nulle."""
    ring = as_ring(ring)
    if len(ring) < 3:
        return None
    ring = _dedupe_consecutive(ring, eps_um)
    if len(ring) < 3:
        return None
    ring = _remove_collinear(ring)
    if len(ring) < 3 or area2(ring) == 0:
        return None
    return np.ascontiguousarray(ring)


def clean_paths(paths, eps_um: int = 1):
    """Nettoie chaque ring et retire ceux qui disparaissent."""
    out = []
    for r in paths:
        c = clean(r, eps_um)
        if c is not None:
            out.append(c)
    return out


def _dp_mask(points: np.ndarray, first: int, last: int, tol2: float, keep: np.ndarray):
    """Douglas–Peucker itératif (pile explicite) sur points[first..last] inclus."""
    stack = [(first, last)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = points[a]
        bx, by = points[b]
        seg = points[a + 1:b]
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            d2 = (seg[:, 0] - ax) ** 2 + (seg[:, 1] - ay) ** 2
        else:
            cross = (seg[:, 0] - ax) * dy - (seg[:, 1] - ay) * dx
            d2 = cross * cross / float(dx * dx + dy * dy)
        i = int(np.argmax(d2))
        if d2[i] > tol2:
            idx = a + 1 + i
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))


def simplify(ring, tol_um: float) -> np.ndarray:
    """Douglas–Peucker sur un ring fermé (garde ≥ 3 points, retire les alignés). Tolérance en µm."""
    ring = as_ring(ring)
    n = len(ring)
    if n <= 3:
        return ring
    pts = ring.astype(np.float64)
    # Deux points d'ancrage : le premier et le plus éloigné de lui.
    d2 = (pts[:, 0] - pts[0, 0]) ** 2 + (pts[:, 1] - pts[0, 1]) ** 2
    far = int(np.argmax(d2))
    if far == 0:
        return ring[:3]
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[far] = True
    tol2 = float(tol_um) ** 2
    _dp_mask(pts, 0, far, tol2, keep)
    # Seconde moitié : far → n-1 puis retour au point 0 (on ajoute le point 0 en fin de tableau).
    closed = np.vstack([pts, pts[:1]])
    keep2 = np.zeros(n + 1, dtype=bool)
    keep2[:n] = keep
    keep2[n] = True
    _dp_mask(closed, far, n, tol2, keep2)
    keep = keep2[:n] | keep
    out = ring[keep]
    if len(out) < 3:
        # Garde au moins trois points : les trois plus « saillants » disponibles.
        return ring[np.sort(np.argsort(-d2)[:3])] if n >= 3 else ring
    out = _remove_collinear(out)
    if len(out) < 3:
        return ring[keep][:3] if keep.sum() >= 3 else ring[:3]
    return np.ascontiguousarray(out)


def point_in_ring(ring, x, y) -> int:
    """Indice d'enroulement du point (x, y) autour du ring (entier, 0 = dehors). Point sur une arête : non défini
    mais borné (0 ou 1)."""
    ring = as_ring(ring)
    if len(ring) < 3:
        return 0
    x, y = int(x), int(y)
    x1, y1 = ring[:, 0], ring[:, 1]
    x2, y2 = np.roll(x1, -1), np.roll(y1, -1)
    # Arête traversant la demi-droite horizontale vers +x : règle demi-ouverte sur y.
    up = (y1 <= y) & (y2 > y)
    down = (y2 <= y) & (y1 > y)
    cross = (x2 - x1) * (y - y1) - (x - x1) * (y2 - y1)
    # cross > 0 : le point est à gauche de l'arête (montante → la demi-droite +x la coupe).
    w = int(np.count_nonzero(up & (cross > 0))) - int(np.count_nonzero(down & (cross < 0)))
    return w


def point_in_paths(paths, x, y) -> bool:
    """Vrai si l'indice d'enroulement total du point est > 0 (règle positive)."""
    return sum(point_in_ring(r, x, y) for r in paths) > 0


def _sample_point(ring: np.ndarray):
    """Un point du ring (son premier sommet) : suffisant pour l'imbrication de contours propres."""
    return int(ring[0, 0]), int(ring[0, 1])


def _contains_vertex(outer: np.ndarray, ring: np.ndarray) -> bool:
    """Le ring est dans outer si un de ses sommets qui n'est pas un sommet d'outer est dedans ;
    à défaut (tous les sommets communs), on teste le milieu de sa première arête."""
    outer_set = None
    for i in range(min(len(ring), 8)):
        x, y = int(ring[i, 0]), int(ring[i, 1])
        if outer_set is None:
            outer_set = set(map(tuple, outer.tolist()))
        if (x, y) in outer_set:
            continue
        return point_in_ring(outer, x, y) != 0
    mx = (int(ring[0, 0]) + int(ring[1, 0])) // 2
    my = (int(ring[0, 1]) + int(ring[1, 1])) // 2
    return point_in_ring(outer, mx, my) != 0


def to_polytree(paths):
    """Imbrication des rings par aire décroissante et test de point : liste de ``(outer CCW, [trous CW])``.
    Les rings de profondeur paire deviennent des extérieurs, les impairs des trous de leur parent."""
    rings = [as_ring(r) for r in paths if len(r) >= 3]
    rings = [r for r in rings if area2(r) != 0]
    order = sorted(range(len(rings)), key=lambda i: -abs(area2(rings[i])))
    parent = [-1] * len(rings)
    depth = [0] * len(rings)
    bboxes = [bbox(rings[i]) for i in range(len(rings))]
    processed = []
    for i in order:
        bx = bboxes[i]
        best = -1
        for j in reversed(processed):  # du plus petit au plus grand déjà placé
            bj = bboxes[j]
            if bx[0] < bj[0] or bx[1] < bj[1] or bx[2] > bj[2] or bx[3] > bj[3]:
                continue
            if _contains_vertex(rings[j], rings[i]):
                best = j
                break
        parent[i] = best
        depth[i] = 0 if best < 0 else depth[best] + 1
        processed.append(i)
    polygons = {}
    for i in order:
        if depth[i] % 2 == 0:
            outer = rings[i] if area2(rings[i]) > 0 else reverse(rings[i])
            polygons[i] = (outer, [])
    for i in order:
        if depth[i] % 2 == 1:
            hole = rings[i] if area2(rings[i]) < 0 else reverse(rings[i])
            polygons[parent[i]][1].append(hole)
    return [polygons[i] for i in order if i in polygons]


def from_polytree(tree):
    """Aplatit un PolyTree en Paths (extérieurs puis trous)."""
    out = []
    for outer, holes in tree:
        out.append(as_ring(outer))
        out.extend(as_ring(h) for h in holes)
    return out


def translate(paths, dx, dy):
    """Translation entière d'un Paths (ou d'un ring)."""
    delta = np.array([int(dx), int(dy)], dtype=INT)
    if isinstance(paths, np.ndarray):
        return paths + delta
    return [as_ring(r) + delta for r in paths]


def rotate(paths, angle_rad, cx=0, cy=0):
    """Rotation autour de (cx, cy), résultat arrondi au µm."""
    c, s = math.cos(angle_rad), math.sin(angle_rad)

    def rot(ring):
        p = as_ring(ring).astype(np.float64)
        x = p[:, 0] - cx
        y = p[:, 1] - cy
        out = np.empty_like(p)
        out[:, 0] = x * c - y * s + cx
        out[:, 1] = x * s + y * c + cy
        return np.rint(out).astype(INT)

    if isinstance(paths, np.ndarray):
        return rot(paths)
    return [rot(r) for r in paths]


def from_mm(points) -> np.ndarray:
    """Points flottants en mm → Ring int64 en µm (arrondi)."""
    return np.rint(np.asarray(points, dtype=np.float64) * SCALE).astype(INT)


def to_mm(ring) -> np.ndarray:
    """Ring µm → flottants ``(n, 2)`` en mm."""
    return as_ring(ring).astype(np.float64) / SCALE


def convex_hull(points) -> np.ndarray:
    """Enveloppe convexe (chaîne monotone d'Andrew), ring CCW sans points alignés sur les côtés."""
    pts = np.asarray(points, dtype=INT).reshape(-1, 2)
    if len(pts) == 0:
        return np.zeros((0, 2), dtype=INT)
    pts = np.unique(pts, axis=0)  # tri lexicographique (x puis y) et dédoublonnage
    if len(pts) <= 2:
        return pts
    P = [tuple(map(int, p)) for p in pts]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in P:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(P):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    return np.array(hull, dtype=INT)
