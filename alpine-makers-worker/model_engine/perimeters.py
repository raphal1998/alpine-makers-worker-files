"""Murs (périmètres) d'une couche — §5.2 de ``docs/MODEL_ENGINE_ARCHITECTURE.md``.

Pour chaque île (polygone extérieur + trous) de la coupe : boucles concentriques par décalages successifs
(−w_ext/2 puis −w_int), ordre d'impression par île (plus proche voisin), couture par règle (``aligned`` | ``nearest`` |
``random`` | ``rear``/``back``), classes de surplomb du mur extérieur d'après la couche précédente, interstices entre le
dernier mur et le remplissage (traits médians), parois fines (trait médian à largeur mesurée), compensation du pied
d'éléphant (couche 1) et compensations XY des trous / contours.

Décalage : ``shift_paths`` décale chaque arête, relie les coins (arc ou pointe limitée) et résout les
auto-intersections par ``geometry.boolean.union`` (règle positive). Même résultat que ``geometry.offset.offset``
(vérifié par IoU dans les tests), environ six fois plus vite sur les contours à plusieurs milliers de sommets : la
différence avec une bande de quadrilatères coûte bien plus que l'union d'un seul contour brut. Un ring convexe seul
se décale sans union (intersection des droites décalées, ou contour brut simple par construction).

Traits (``gap_fill``, ``thin_walls``) : polylignes int64 (k, 2) ouvertes ; une bande fermée (tube, cadre) donne un
trait fermé, premier point répété en fin. Pied d'éléphant (couche 1) : les parties plus fines que
2·(compensation + w_ext/2) restent entières et soudées au cœur rétréci.

Unités : micromètres entiers (int64) ; les réglages en mm sont convertis à l'entrée. Déterministe : la couture
``random`` est tirée d'une graine (couche, objet, île, boucle)."""
from __future__ import annotations

import math

import numpy as np

from .geometry import RESOLUTION_UM, SCALE
from .geometry.boolean import difference, intersection, union
from .geometry.poly import INT, area2, as_ring, bbox, clean, convex_hull, perimeter_um, reverse, simplify, to_polytree
from .layers import Layer, WallLoop, Walls

# Réglages lus (noms de profiles/process.py) et défauts si absents du dict.
DEFAULTS = {
    "wall_loops": 2,
    "outer_wall_line_width": 0.42,          # mm
    "inner_wall_line_width": None,          # mm ; None = largeur du mur extérieur
    "wall_sequence": "inner_outer",         # inner_outer | outer_inner (inner_outer_inner traité comme inner_outer)
    "seam_position": "aligned",             # aligned | nearest | random | rear | back
    "gap_fill": True,
    "filter_out_gap_fill": 0.0,             # mm : interstices plus courts ignorés (0 = tous)
    "detect_thin_wall": True,
    "detect_overhang_wall": True,
    "elephant_foot_compensation_mm": 0.15,  # mm, couche 1 seulement
    "xy_hole_compensation": 0.0,            # mm (> 0 agrandit les trous)
    "xy_contour_compensation": 0.0,         # mm (> 0 dilate le contour)
    "resolution": RESOLUTION_UM / SCALE,    # mm
}
# Anciens noms ou variantes acceptés en entrée.
ALIASES = {
    "elefant_foot_compensation": "elephant_foot_compensation_mm",
    "elephant_foot_compensation": "elephant_foot_compensation_mm",
    "wall_order": "wall_sequence",
    "wall_infill_order": "wall_sequence",
    "perimeters": "wall_loops",
    "detect_thin_walls": "detect_thin_wall",
    "detect_overhang_walls": "detect_overhang_wall",
}
SEAM_MODES = ("aligned", "nearest", "random", "rear", "back")

GAP_MIN_FACTOR = 0.3       # interstice plus étroit que 0,3·w : ignoré
GAP_MAX_FACTOR = 1.5       # largeur de trait d'interstice plafonnée à 1,5·w
THIN_MIN_FACTOR = 0.25     # paroi fine plus étroite que 0,25·w : ignorée
THIN_MAX_FACTOR = 2.0      # largeur de trait de paroi fine plafonnée à 2·w
THIN_LOOP_FACTOR = 0.6     # boucle dont la bande est plus étroite que 0,6·w : abandonnée (chevauchement > 40 %)
CONCAVE_MIN_ANGLE = math.radians(15.0)  # coin concave retenu pour la couture
ARC_TOLERANCE_UM = 5.0     # flèche maximale des arcs de jointure
_CHUNK = 2_000_000         # éléments par bloc dans les tests d'intérieur vectorisés

_EMPTY = np.zeros((0, 2), dtype=INT)


# ---------------------------------------------------------------------------------------------------------------
# Réglages
# ---------------------------------------------------------------------------------------------------------------

def _get(settings: dict, key: str):
    """Valeur du réglage ``key`` (ou d'un de ses alias), sinon le défaut du module."""
    if settings:
        if key in settings and settings[key] is not None:
            return settings[key]
        for alias, target in ALIASES.items():
            if target == key and alias in settings and settings[alias] is not None:
                return settings[alias]
    return DEFAULTS[key]


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "vrai", "yes", "oui", "on")
    return bool(value)


def read_settings(settings: dict | None) -> dict:
    """Réglages des murs convertis (µm entiers, booléens, énumérations normalisées)."""
    settings = settings or {}
    outer_mm = float(_get(settings, "outer_wall_line_width"))
    inner_raw = _get(settings, "inner_wall_line_width")
    inner_mm = outer_mm if inner_raw in (None, "", 0, 0.0) else float(inner_raw)
    seam = str(_get(settings, "seam_position")).strip().lower()
    if seam == "back":
        seam = "rear"
    if seam not in SEAM_MODES:
        seam = "aligned"
    order = str(_get(settings, "wall_sequence")).strip().lower()
    order = "outer_inner" if order == "outer_inner" else "inner_outer"
    res_um = max(1, int(round(float(_get(settings, "resolution")) * SCALE)))
    return {
        "wall_loops": max(0, int(float(_get(settings, "wall_loops")))),
        "outer_um": max(20, int(round(outer_mm * SCALE))),
        "inner_um": max(20, int(round(inner_mm * SCALE))),
        "order": order,
        "seam": seam,
        "gap_fill": _as_bool(_get(settings, "gap_fill")),
        "gap_min_len_um": max(0, int(round(float(_get(settings, "filter_out_gap_fill")) * SCALE))),
        "thin_walls": _as_bool(_get(settings, "detect_thin_wall")),
        "overhangs": _as_bool(_get(settings, "detect_overhang_wall")),
        "elephant_um": max(0, int(round(float(_get(settings, "elephant_foot_compensation_mm")) * SCALE))),
        "hole_um": int(round(float(_get(settings, "xy_hole_compensation")) * SCALE)),
        "contour_um": int(round(float(_get(settings, "xy_contour_compensation")) * SCALE)),
        "resolution_um": res_um,
    }


# ---------------------------------------------------------------------------------------------------------------
# Décalage rapide (contournement local de geometry.offset, même résultat, voir l'en-tête)
# ---------------------------------------------------------------------------------------------------------------

def _shift_ring(ring: np.ndarray, d: float, join: str, miter_limit: float, arc_tol: float) -> np.ndarray:
    """Contour brut décalé de ``d`` vers la gauche du sens de parcours (d < 0 : vers la droite). Les coins qui se
    ferment créent une petite boucle inversée, ceux qui s'ouvrent reçoivent un arc (``round``) ou une pointe
    (``miter``, bornée par ``miter_limit``, sinon chanfrein) ; ``union`` résout ensuite l'ensemble."""
    a = ring.astype(np.float64)
    b = np.roll(a, -1, axis=0)
    e = b - a
    length = np.hypot(e[:, 0], e[:, 1])
    u = e / length[:, None]
    nrm = np.column_stack([-u[:, 1], u[:, 0]])  # normale gauche
    oa = a + d * nrm
    ob = b + d * nrm
    u2 = np.roll(u, -1, axis=0)
    n2 = np.roll(nrm, -1, axis=0)
    turn = u[:, 0] * u2[:, 1] - u[:, 1] * u2[:, 0]
    opening = (turn * (1.0 if d > 0 else -1.0)) < -1e-12
    cosphi = np.clip((nrm * n2).sum(axis=1), -1.0, 1.0)
    n = len(ring)
    if join == "round":
        phi = np.arccos(cosphi)
        mag = abs(d)
        step = 2.0 * math.acos(max(-1.0, min(1.0, 1.0 - arc_tol / mag))) if arc_tol < mag else math.pi
        step = max(step, 0.02)
        m = np.where(opening, np.maximum(1, np.ceil(phi / step - 1e-9)), 1).astype(INT)
        extra = m - 1
    else:
        half_cos2 = (1.0 + cosphi) / 2.0
        use = opening & (half_cos2 >= 1.0 / (miter_limit * miter_limit)) & (half_cos2 > 1e-9)
        extra = use.astype(INT)
    counts = 2 + extra
    starts = np.cumsum(counts) - counts
    out = np.empty((int(counts.sum()), 2), dtype=np.float64)
    out[starts] = oa
    out[starts + 1] = ob
    tot = int(extra.sum())
    if tot:
        idx = np.flatnonzero(extra > 0)
        if join == "round":
            cnt = extra[idx]
            rep = np.repeat(idx, cnt)
            k = np.arange(tot) - np.repeat(np.cumsum(cnt) - cnt, cnt) + 1
            th0 = np.arctan2(nrm[:, 1], nrm[:, 0])
            dth = np.arctan2(n2[:, 1], n2[:, 0]) - th0
            dth = (dth + math.pi) % (2.0 * math.pi) - math.pi
            sweep = dth / m
            th = th0[rep] + k * sweep[rep]
            out[starts[rep] + 1 + k] = b[rep] + d * np.column_stack([np.cos(th), np.sin(th)])
        else:
            scale = d / (1.0 + cosphi[idx])
            out[starts[idx] + 2] = b[idx] + scale[:, None] * (nrm[idx] + n2[idx])
    return np.rint(out).astype(INT)


def _is_convex_ccw(ring: np.ndarray) -> bool:
    """Vrai pour un ring nettoyé (sans sommets alignés) convexe et trigonométrique."""
    e1 = ring - np.roll(ring, 1, axis=0)
    e2 = np.roll(ring, -1, axis=0) - ring
    cross = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
    return bool((cross > 0).all())


def _shrink_convex(ring: np.ndarray, d: float):
    """Rétrécissement exact d'un ring convexe CCW de ``d`` (> 0) par intersection des droites décalées ; None si une
    arête est avalée (le chemin général par union tranche alors)."""
    a = ring.astype(np.float64)
    b = np.roll(a, -1, axis=0)
    e = b - a
    u = e / np.hypot(e[:, 0], e[:, 1])[:, None]
    nrm = np.column_stack([-u[:, 1], u[:, 0]])
    n2 = np.roll(nrm, -1, axis=0)
    denom = 1.0 + np.clip((nrm * n2).sum(axis=1), -1.0, 1.0)
    if (denom < 1e-6).any():
        return None
    x = b + d * (nrm + n2) / denom[:, None]           # sommet au bout de l'arête i
    if (((x - np.roll(x, 1, axis=0)) * u).sum(axis=1) <= 1.0).any():  # arête i : x[i-1] → x[i], même sens que u[i]
        return None
    out = clean(np.rint(x).astype(INT), 0)
    if out is None or area2(out) <= 0 or not _is_convex_ccw(out):
        return None
    return out


def shift_paths(paths, delta_um, join: str = "round", miter_limit: float = 2.0,
                arc_tolerance_um: float = ARC_TOLERANCE_UM):
    """Décale un Paths (règle positive) de ``delta_um`` : > 0 agrandit, < 0 rétrécit. Résultat propre (union).
    Un ring convexe seul se traite sans union (rétrécissement par intersection des droites décalées, agrandissement
    par le contour brut, simple par construction) : ~1,3 ms d'épargnés par appel sur les petites îles."""
    if join not in ("round", "miter"):
        raise ValueError(f"jointure inconnue : {join}")
    rings = []
    for r in paths:
        c = clean(as_ring(r), 0)
        if c is not None:
            rings.append(c)
    if not rings:
        return []
    delta = float(delta_um)
    if not math.isfinite(delta):
        raise ValueError("delta_um doit être un nombre fini")
    if abs(delta) < 0.5:
        return union(rings)
    if delta < 0:
        x0, y0, x1, y1 = bbox(rings)
        if -2.0 * delta >= min(x1 - x0, y1 - y0):
            return []
    miter_limit = max(1.0, float(miter_limit))
    arc_tol = max(0.5, float(arc_tolerance_um))
    if len(rings) == 1 and abs(delta) >= 10.0 and area2(rings[0]) > 0 and _is_convex_ccw(rings[0]):
        if delta < 0:
            fast = _shrink_convex(rings[0], -delta)
            if fast is not None:
                return [fast]
        else:
            grown = clean(_shift_ring(rings[0], -delta, join, miter_limit, arc_tol), 0)
            if grown is not None and area2(grown) > 0:
                return [grown]
    raws = [_shift_ring(r, -delta, join, miter_limit, arc_tol) for r in rings]
    return _drop_slivers(union(raws))


def _drop_slivers(paths, width_um: float = 3.0):
    """Retire les rings de largeur moyenne (2A/P) ≤ ``width_um`` : fantômes d'arrondi de l'union du contour brut
    (un fantôme de quelques µm² devenait, décalé au tour suivant, un triangle parasite de 0,02 mm²). Un trou d'un
    extérieur retiré est plus fin encore, donc retiré aussi."""
    return [r for r in paths if abs(area2(r)) > width_um * perimeter_um(r)]


def inset_rounds(paths, deltas_um):
    """Décalages successifs vers l'intérieur de chaque |delta| de ``deltas_um`` ; s'arrête dès que c'est vide."""
    rounds = []
    current = paths
    for d in deltas_um:
        current = shift_paths(current, -abs(float(d)))
        if not current:
            break
        rounds.append(current)
    return rounds


# ---------------------------------------------------------------------------------------------------------------
# Outils géométriques locaux
# ---------------------------------------------------------------------------------------------------------------

def _simplify_paths(paths, tol_um: int):
    out = []
    for r in paths:
        s = simplify(r, tol_um) if len(r) > 3 else as_ring(r)
        if len(s) >= 3 and area2(s) != 0:
            out.append(s)
    return _drop_slivers(out)


def _polyline_simplify(points: np.ndarray, tol_um: float) -> np.ndarray:
    """Douglas–Peucker sur une polyligne ouverte (pile explicite)."""
    pts = np.asarray(points, dtype=INT)
    n = len(pts)
    if n <= 2:
        return pts
    f = pts.astype(np.float64)
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    tol2 = float(tol_um) ** 2
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = f[a]
        bx, by = f[b]
        seg = f[a + 1:b]
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            d2 = (seg[:, 0] - ax) ** 2 + (seg[:, 1] - ay) ** 2
        else:
            cross = (seg[:, 0] - ax) * dy - (seg[:, 1] - ay) * dx
            d2 = cross * cross / (dx * dx + dy * dy)
        i = int(np.argmax(d2))
        if d2[i] > tol2:
            idx = a + 1 + i
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return pts[keep]


def _mean_width_um(paths) -> float:
    """Largeur moyenne d'une bande fine : 2·aire / périmètre (aire nette, trous déduits)."""
    a2 = sum(area2(r) for r in paths)
    p = sum(perimeter_um(r) for r in paths)
    return abs(a2) / p if p > 0 else 0.0


def _farthest_pair(ring: np.ndarray) -> tuple[int, int]:
    """Indices des deux sommets les plus éloignés (sur l'enveloppe convexe)."""
    hull = convex_hull(ring)
    if len(hull) < 2:
        return 0, 0
    if len(hull) > 400:
        hull = hull[:: int(math.ceil(len(hull) / 400))]
    h = hull.astype(np.float64)
    d2 = ((h[:, None, :] - h[None, :, :]) ** 2).sum(axis=2)
    k = int(np.argmax(d2))
    p, q = hull[k // len(hull)], hull[k % len(hull)]
    i = int(np.flatnonzero((ring == p).all(axis=1))[0])
    j = int(np.flatnonzero((ring == q).all(axis=1))[0])
    return (i, j) if i <= j else (j, i)


def _strip_caps(pts: np.ndarray, cap_len: float) -> np.ndarray:
    """Retire à chaque bout d'une polyligne les bouchons d'extrémité de la lamelle : arêtes de tête (et de queue)
    tant que leur longueur cumulée reste ≤ ``cap_len`` (bout plat = une arête, bout arrondi = quelques petites)."""
    f = pts.astype(np.float64)
    seg = np.hypot(*(np.diff(f, axis=0).T))
    n = len(f)
    lo, acc = 0, 0.0
    while lo < n - 2 and acc + seg[lo] <= cap_len:
        acc += seg[lo]
        lo += 1
    hi, acc = n - 1, 0.0
    while hi > lo + 1 and acc + seg[hi - 1] <= cap_len:
        acc += seg[hi - 1]
        hi -= 1
    return pts[lo:hi + 1]


def _shift_left(pts: np.ndarray, shift_um: float, closed: bool) -> np.ndarray:
    """Décale chaque sommet de ``shift_um`` à gauche du sens de parcours (normale moyenne des arêtes voisines)."""
    p = pts.astype(np.float64)
    if closed:
        e = np.roll(p, -1, axis=0) - p
    else:
        e = np.diff(p, axis=0)
    length = np.hypot(e[:, 0], e[:, 1])
    length[length == 0] = 1.0
    seg_n = np.column_stack([-e[:, 1], e[:, 0]]) / length[:, None]
    if closed:
        mid = seg_n + np.roll(seg_n, 1, axis=0)
        ml = np.hypot(mid[:, 0], mid[:, 1])
        ml[ml == 0] = 1.0
        vert_n = mid / ml[:, None]
    else:
        vert_n = np.empty_like(p)
        vert_n[0], vert_n[-1] = seg_n[0], seg_n[-1]
        if len(p) > 2:
            mid = seg_n[:-1] + seg_n[1:]
            ml = np.hypot(mid[:, 0], mid[:, 1])
            ml[ml == 0] = 1.0
            vert_n[1:-1] = mid / ml[:, None]
    return np.rint(p + vert_n * shift_um).astype(INT)


MEDIAL_SLIVER_FRACTIONS = (0.375, 0.25, 0.125)  # rétrécissement (× largeur) donnant la lamelle de l'axe médian


def _medial_polylines(polygon_paths, width_um: float, tol_um: float) -> list:
    """Traits médians approchés d'une bande fine (polygone ``[outer] + trous``, largeur moyenne ``width_um``).
    Le contour rétréci de 0,375·largeur est une lamelle (≈ un quart de largeur) qui longe l'axe médian ; chaque
    polygone de la lamelle est recentré d'une demi-largeur de lamelle vers la bande :
    - sans trou (bande ouverte) : une moitié du contour entre ses deux points les plus éloignés ;
    - un trou (bande fermée : tube, cadre) : le contour extérieur, trait fermé (premier point répété en fin) ;
    - plusieurs trous (réseau) : chaque trou, traits fermés (les segments entre deux cellules sont tracés deux
      fois, les ailettes libres du bord manquent : limite V1)."""
    sliver = []
    for frac in MEDIAL_SLIVER_FRACTIONS:
        d = width_um * frac
        if d < 1.0:
            continue
        sliver = shift_paths(polygon_paths, -d)
        if sliver:
            break
    if not sliver:
        outer = polygon_paths[0]
        i, j = _farthest_pair(outer)
        if i == j:
            return []
        return [np.array([outer[i], outer[j]], dtype=INT)]
    out = []
    for outer, holes in to_polytree(sliver):
        s_w = _mean_width_um([outer] + holes)  # largeur locale de la lamelle
        if not holes:
            i, j = _farthest_pair(outer)
            if i == j:
                continue
            half = _strip_caps(outer[i:j + 1], 2.0 * s_w)
            if len(half) < 2:
                continue
            half = _polyline_simplify(_shift_left(half, s_w / 2.0, closed=False), tol_um)
            if len(half) >= 2:
                out.append(half)
            continue
        for ring in ([outer] if len(holes) == 1 else holes):
            c = _shift_left(ring, s_w / 2.0, closed=True)
            c = simplify(c, tol_um) if len(c) > 3 else c
            if len(c) >= 3:
                out.append(np.vstack([c, c[:1]]))
    return out


def _significant(paths, min_area_um2: float):
    """Rings d'aire absolue ≥ ``min_area_um2`` (filtre des lamelles résiduelles avant tout traitement)."""
    return [r for r in paths if abs(area2(r)) >= 2.0 * min_area_um2]


def _paths_area_um2(paths) -> float:
    return sum(area2(r) for r in paths) / 2.0


def _polyline_len(points: np.ndarray) -> float:
    d = np.diff(points.astype(np.float64), axis=0)
    return float(np.hypot(d[:, 0], d[:, 1]).sum())


def _inside_many(points: np.ndarray, paths) -> np.ndarray:
    """Vrai pour chaque point (K, 2) dont l'indice d'enroulement total dans ``paths`` est > 0 (vectorisé par blocs)."""
    pts = np.asarray(points, dtype=INT).reshape(-1, 2)
    k = len(pts)
    w = np.zeros(k, dtype=INT)
    if k == 0:
        return w > 0
    for ring in paths:
        ring = as_ring(ring)
        n = len(ring)
        if n < 3:
            continue
        bx0, by0, bx1, by1 = bbox(ring)
        cand = np.flatnonzero((pts[:, 0] >= bx0) & (pts[:, 0] <= bx1) & (pts[:, 1] >= by0) & (pts[:, 1] <= by1))
        if not len(cand):
            continue
        x1, y1 = ring[:, 0], ring[:, 1]
        x2, y2 = np.roll(x1, -1), np.roll(y1, -1)
        block = max(1, _CHUNK // n)
        for s in range(0, len(cand), block):
            c = cand[s:s + block]
            px = pts[c, 0][:, None]
            py = pts[c, 1][:, None]
            up = (y1 <= py) & (y2 > py)
            down = (y2 <= py) & (y1 > py)
            cross = (x2 - x1) * (py - y1) - (px - x1) * (y2 - y1)
            w[c] += np.count_nonzero(up & (cross > 0), axis=1) - np.count_nonzero(down & (cross < 0), axis=1)
    return w > 0


def _concave_indices(ring: np.ndarray, min_angle: float = CONCAVE_MIN_ANGLE) -> np.ndarray:
    """Sommets concaves pour la matière (virage à droite, extérieur CCW comme trou CW) d'angle ≥ min_angle."""
    r = ring.astype(np.float64)
    e1 = r - np.roll(r, 1, axis=0)
    e2 = np.roll(r, -1, axis=0) - r
    cross = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
    dot = (e1 * e2).sum(axis=1)
    angle = np.arctan2(np.abs(cross), dot)
    return np.flatnonzero((cross < 0) & (angle >= min_angle))


def _nearest_point(ring: np.ndarray, targets) -> tuple | None:
    """Parmi ``targets`` (liste de (x, y)), celui qui passe le plus près d'un sommet du ring."""
    if targets is None or len(targets) == 0:
        return None
    t = np.asarray(targets, dtype=np.float64).reshape(-1, 2)
    r = ring.astype(np.float64)
    best, best_d = None, None
    block = max(1, _CHUNK // max(1, len(r)))
    for s in range(0, len(t), block):
        tt = t[s:s + block]
        d2 = ((r[None, :, :] - tt[:, None, :]) ** 2).sum(axis=2).min(axis=1)
        i = int(np.argmin(d2))
        if best_d is None or d2[i] < best_d:
            best, best_d = (float(tt[i, 0]), float(tt[i, 1])), float(d2[i])
    return best


def _rear_index(ring: np.ndarray, candidates: np.ndarray) -> int:
    """Sommet le plus en arrière (y max, puis x max) parmi les candidats."""
    sub = ring[candidates]
    order = np.lexsort((sub[:, 0], sub[:, 1]))
    return int(candidates[order[-1]])


def seam_index(ring: np.ndarray, mode: str, *, target=None, current=None, rng=None) -> int:
    """Indice du sommet de couture : ``aligned`` = coin concave (sinon tout sommet) le plus proche de ``target``
    (sans cible : le plus en arrière) ; ``nearest`` = sommet le plus proche de ``current`` ; ``rear`` = y max ;
    ``random`` = tirage dans ``rng``."""
    n = len(ring)
    if mode == "random":
        return int(rng.integers(n)) if rng is not None else 0
    everything = np.arange(n)
    if mode == "rear":
        return _rear_index(ring, everything)
    if mode == "nearest":
        cand, point = everything, current
    else:
        cand, point = _concave_indices(ring), target
        if not len(cand):
            cand = everything
    if point is None:
        return _rear_index(ring, cand)
    sub = ring[cand].astype(np.float64)
    d2 = (sub[:, 0] - float(point[0])) ** 2 + (sub[:, 1] - float(point[1])) ** 2
    return int(cand[int(np.argmin(d2))])


def classify_overhangs(points: np.ndarray, line_width_um: int, previous_regions) -> np.ndarray | None:
    """Classe de surplomb par segment (point i → i+1) d'un mur extérieur : quatre points témoins au milieu du
    segment, répartis sur la largeur de ligne vers l'extérieur (3w/8, w/8, −w/8, −3w/8) ; classe = nombre de
    témoins hors de ``previous_regions`` (1 = 25 %, …, 4 = 100 %). None si tout est soutenu."""
    k = len(points)
    if k < 2 or not previous_regions:
        return None
    a = points.astype(np.float64)
    b = np.roll(a, -1, axis=0)
    mid = (a + b) / 2.0
    e = b - a
    length = np.hypot(e[:, 0], e[:, 1])
    length[length == 0] = 1.0
    out_n = np.column_stack([e[:, 1], -e[:, 0]]) / length[:, None]  # normale droite = extérieur
    w = float(line_width_um)
    classes = np.zeros(k, dtype=np.int8)
    alive = np.arange(k)
    # Témoin le plus extérieur d'abord : s'il est soutenu, les plus intérieurs le sont aussi.
    for j, frac in enumerate((3.0 / 8.0, 1.0 / 8.0, -1.0 / 8.0, -3.0 / 8.0)):
        if not len(alive):
            break
        probes = np.rint(mid[alive] + out_n[alive] * (w * frac)).astype(INT)
        inside = _inside_many(probes, previous_regions)
        unsupported = alive[~inside]
        classes[unsupported] += 1
        alive = unsupported
    return classes if classes.any() else None


# ---------------------------------------------------------------------------------------------------------------
# Compensations d'une île
# ---------------------------------------------------------------------------------------------------------------

def _elephant_foot(island, cfg: dict) -> list:
    """Pied d'éléphant (couche 1) : l'île est rétrécie de ``elephant_um``, sauf ses parties fines (largeur
    < 2·(elephant_um + w_ext/2), par ouverture morphologique) qui restent entières, prolongées de ``elephant_um``
    à l'intérieur de l'île pour rester soudées au cœur rétréci. Une île entièrement fine reste intacte."""
    e = cfg["elephant_um"]
    shrunk = shift_paths(island, -e)
    if not shrunk:
        return island
    keep_d = e + cfg["outer_um"] / 2.0
    overlap = max(4, 2 * cfg["resolution_um"])
    core = shift_paths(island, -keep_d)
    # Ouverture légèrement élargie : les lamelles d'arrondi le long du bord ne comptent pas comme parties fines.
    regrown = shift_paths(core, keep_d + overlap, join="miter") if core else []
    if not regrown:
        return island
    lost = _significant(difference(island, regrown), (THIN_MIN_FACTOR * cfg["outer_um"]) * cfg["outer_um"])
    if not lost:
        return shrunk
    # Raccord en pointe (pas de fissure tangente), prolongé d'un léger recouvrement sur le cœur rétréci : un simple
    # contact arrondi au µm ne serait pas toujours fusionné par l'union (boucles parasites).
    attach = intersection(shift_paths(lost, e + overlap, join="miter"), island)
    return union(shrunk + attach) if attach else shrunk


def _compensate_island(outer: np.ndarray, holes: list, cfg: dict, layer_index: int) -> list:
    """Compensations XY (contour, trous) puis pied d'éléphant (couche 1, parties fines préservées)."""
    island = [outer] + list(holes)
    if cfg["contour_um"] or (cfg["hole_um"] and holes):
        outer_p = [outer]
        holes_p = [reverse(h) for h in holes]
        if cfg["contour_um"]:
            outer_p = shift_paths(outer_p, cfg["contour_um"])
        if cfg["hole_um"] and holes_p:
            holes_p = shift_paths(holes_p, cfg["hole_um"])
        if not outer_p:
            return []
        island = difference(outer_p, holes_p) if holes_p else outer_p
        if not island:
            return []
    if layer_index == 1 and cfg["elephant_um"] > 0:
        island = _elephant_foot(island, cfg)
    return island


# ---------------------------------------------------------------------------------------------------------------
# Construction des murs
# ---------------------------------------------------------------------------------------------------------------

def _island_geometry(island, cfg: dict) -> dict:
    """Tours de murs, zone intérieure brute, interstices et parois fines d'une île (Paths déjà compensée)."""
    w_out, w_in, tol = cfg["outer_um"], cfg["inner_um"], cfg["resolution_um"]
    n_loops = cfg["wall_loops"]
    deltas = []
    for k in range(n_loops):
        deltas.append(w_out / 2.0 if k == 0 else ((w_out + w_in) / 2.0 if k == 1 else float(w_in)))
    rounds = []
    current = island
    for k, d in enumerate(deltas):
        current = shift_paths(current, -d)
        if not current:
            break
        current = _simplify_paths(current, tol)
        if not current:
            break
        # Boucle dont les deux côtés sont partout plus proches que 0,6 ligne : deux cordons se chevaucheraient trop ;
        # elle est abandonnée (tour 0 : l'île devient une paroi fine ; sinon la bande revient aux interstices).
        # 2A/P (largeur d'une bande) sous-estime les formes compactes (disque : r/2) : un rétrécissement de 0,3
        # ligne confirme, sur ces seuls cas, qu'aucune partie n'atteint 0,6 ligne de large.
        w_k = w_out if k == 0 else w_in
        if cfg["thin_walls"] and _mean_width_um(current) < THIN_LOOP_FACTOR * w_k \
                and not shift_paths(current, -THIN_LOOP_FACTOR * w_k / 2.0):
            break
        rounds.append(current)
    thin_zone = []
    thin_min_area = (THIN_MIN_FACTOR * w_out) * w_out  # largeur × longueur minimales d'une paroi fine
    if n_loops and cfg["thin_walls"]:
        if not rounds:
            thin_zone = island
        else:
            covered = shift_paths(rounds[0], w_out / 2.0, join="miter")
            # Rien de plus fin que la ligne si l'ouverture couvre (presque) toute l'île : différence évitée.
            if covered and _paths_area_um2(island) - _paths_area_um2(covered) >= thin_min_area:
                thin_zone = _significant(difference(island, covered), thin_min_area)
            elif not covered:
                thin_zone = island
    if not n_loops:
        inner_raw = island
    elif rounds:
        w_last = w_out if len(rounds) == 1 else w_in
        inner_raw = shift_paths(rounds[-1], -w_last / 2.0)
    else:
        inner_raw = []
    gaps, inner_area = [], inner_raw
    if cfg["gap_fill"] and inner_raw and n_loops:
        w_g = w_out if len(rounds) <= 1 else w_in
        gap_min_area = (GAP_MIN_FACTOR * w_g) * w_g
        eroded = shift_paths(inner_raw, -w_g / 2.0)
        opened = shift_paths(eroded, w_g / 2.0, join="miter") if eroded else []
        if not opened:
            residual = inner_raw
        elif _paths_area_um2(inner_raw) - _paths_area_um2(opened) >= gap_min_area:
            residual = _significant(difference(inner_raw, opened), gap_min_area)
        else:
            residual = []
        min_len = max(cfg["gap_min_len_um"], w_g)
        for outer, holes in to_polytree(residual):
            poly = [outer] + holes
            wd = _mean_width_um(poly)
            if wd < GAP_MIN_FACTOR * w_g:
                continue
            width = int(round(min(wd, GAP_MAX_FACTOR * w_g)))
            lines_ = [pl for pl in _medial_polylines(poly, wd, tol) if _polyline_len(pl) >= min_len]
            if lines_:
                gaps.append((poly, width, lines_))
        if gaps:
            removed = [r for poly, _, _ in gaps for r in poly]
            inner_area = difference(inner_raw, removed)
    thin = []
    for outer, holes in to_polytree(thin_zone):
        poly = [outer] + holes
        wd = _mean_width_um(poly)
        if wd < THIN_MIN_FACTOR * w_out:
            continue
        width = int(round(min(max(wd, THIN_MIN_FACTOR * w_out), THIN_MAX_FACTOR * w_out)))
        for pl in _medial_polylines(poly, wd, tol):
            if _polyline_len(pl) >= w_out:
                thin.append((pl, width))
    return {"rounds": rounds, "inner_area": inner_area, "gaps": gaps, "thin": thin}


def _order_by_nearest(items, start_xy, key):
    """Ordre glouton du plus proche voisin à partir de ``start_xy`` ; ``key(item)`` = point (x, y)."""
    remaining = list(range(len(items)))
    pts = np.array([key(it) for it in items], dtype=np.float64).reshape(-1, 2)
    cur = np.array(start_xy, dtype=np.float64)
    order = []
    while remaining:
        sub = pts[remaining]
        d2 = ((sub - cur) ** 2).sum(axis=1)
        j = int(np.argmin(d2))
        order.append(remaining.pop(j))
        cur = pts[order[-1]]
    return order


def _ring_center(ring: np.ndarray) -> tuple:
    x0, y0, x1, y1 = bbox(ring)
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def build_walls(regions, settings: dict | None, *, layer_index: int, previous_seams: dict | None,
                object_id: int = 1, previous_regions=None, start_xy=None) -> Walls:
    """Murs d'une couche pour un objet. ``regions`` : coupe de l'objet (Paths) ; ``previous_seams`` : mémoire
    des coutures entre couches (``{object_id: [(x, y), …]}`` des murs extérieurs), lue puis mise à jour en place ;
    ``previous_regions`` : coupe de la couche précédente (surplombs) ; ``start_xy`` : position courante en µm
    (ordre des îles, couture ``nearest``), par défaut (0, 0)."""
    cfg = read_settings(settings)
    if previous_seams is None:
        previous_seams = {}
    walls = Walls()
    cleaned = [clean(as_ring(r), 0) for r in regions if len(r) >= 3]
    rings = _simplify_paths([r for r in cleaned if r is not None], cfg["resolution_um"])
    tree = to_polytree(rings)
    if not tree:
        previous_seams[object_id] = []
        return walls
    islands = []
    for outer, holes in tree:
        island = _compensate_island(outer, holes, cfg, layer_index)
        if island:
            islands.append(island)
    if not islands:
        previous_seams[object_id] = []
        return walls
    cur = tuple(float(v) for v in start_xy) if start_xy is not None else (0.0, 0.0)
    order = _order_by_nearest(islands, cur, lambda isl: _ring_center(isl[0]))
    prev_points = previous_seams.get(object_id) or []
    new_seams = []
    inner_area = []
    for island_no, idx in enumerate(order):
        island = islands[idx]
        geo = _island_geometry(island, cfg)
        rounds = geo["rounds"]
        inner_area.extend(geo["inner_area"])
        walls.gap_fill.extend(pl for _, _, lines_ in geo["gaps"] for pl in lines_)
        walls.thin_walls.extend(geo["thin"])
        # Tour 0 = murs extérieurs (contour et trous), tours suivants = murs intérieurs.
        island_seams = []
        loops_by_round = []
        for k, paths in enumerate(rounds):
            width = cfg["outer_um"] if k == 0 else cfg["inner_um"]
            loops = []
            for loop_no, ring in enumerate(paths):
                rng = np.random.default_rng([int(layer_index), int(object_id), island_no, k, loop_no]) \
                    if cfg["seam"] == "random" else None
                loops.append((ring, width, k == 0, rng))
            loops_by_round.append(loops)
        sequence = list(range(len(rounds)))
        if cfg["order"] == "inner_outer":
            sequence.reverse()
        # Les coutures du tour 0 (cible : couture de la couche précédente la plus proche) servent de cible aux
        # tours intérieurs : choisies d'abord, indépendamment de l'ordre d'impression (sauf « nearest »).
        seam_of = {}
        if rounds and cfg["seam"] != "nearest":
            for loop_no, (ring, width, is_outer, rng) in enumerate(loops_by_round[0]):
                i = seam_index(ring, cfg["seam"], target=_nearest_point(ring, prev_points), rng=rng)
                seam_of[(0, loop_no)] = i
                island_seams.append((int(ring[i, 0]), int(ring[i, 1])))
        for k in sequence:
            loops = loops_by_round[k]
            sub_order = _order_by_nearest(loops, cur, lambda lp: _ring_center(lp[0]))
            for loop_no in sub_order:
                ring, width, is_outer, rng = loops[loop_no]
                if (k, loop_no) in seam_of:
                    i = seam_of[(k, loop_no)]
                else:
                    target = _nearest_point(ring, island_seams if k > 0 else prev_points)
                    i = seam_index(ring, cfg["seam"], target=target, current=cur, rng=rng)
                    if k == 0:
                        island_seams.append((int(ring[i, 0]), int(ring[i, 1])))
                pts = np.ascontiguousarray(np.roll(ring, -i, axis=0))
                overhang = None
                if is_outer and cfg["overhangs"] and layer_index > 1 and previous_regions:
                    overhang = classify_overhangs(pts, width, previous_regions)
                walls.loops.append(WallLoop(points=pts, line_width_um=int(width), is_outer=bool(is_outer),
                                            island=island_no, object_id=int(object_id), overhang_class=overhang))
                cur = (float(pts[0, 0]), float(pts[0, 1]))
                if is_outer:
                    new_seams.append((int(pts[0, 0]), int(pts[0, 1])))
    walls.inner_area = inner_area
    walls.islands = len(islands)
    previous_seams[object_id] = new_seams
    return walls


def build_layer_walls(layer: Layer, settings: dict | None, previous_layer: Layer | None = None) -> None:
    """Remplit ``layer.walls[object_id]`` pour chaque objet de la couche. Les coutures de la couche précédente sont
    lues dans ``previous_layer.walls`` ; ses régions (tous objets) servent aux surplombs."""
    previous_seams = {}
    previous_regions = None
    if previous_layer is not None:
        for oid, w in (previous_layer.walls or {}).items():
            previous_seams[oid] = [(int(lp.points[0, 0]), int(lp.points[0, 1])) for lp in w.loops if lp.is_outer]
        previous_regions = previous_layer.all_regions or None
        if not previous_regions:
            merged = []
            for paths in (previous_layer.regions or {}).values():
                merged.extend(paths)
            previous_regions = merged or None
    cur = None
    for object_id in sorted(k for k in layer.regions if int(k) > 0):   # clés négatives = volumes non imprimés
        walls = build_walls(layer.regions[object_id], settings, layer_index=layer.index, previous_seams=previous_seams,
                            object_id=object_id, previous_regions=previous_regions, start_xy=cur)
        layer.walls[object_id] = walls
        if walls.loops:
            last = walls.loops[-1].points[0]
            cur = (float(last[0]), float(last[1]))


__all__ = ["ALIASES", "DEFAULTS", "SEAM_MODES", "build_layer_walls", "build_walls", "classify_overhangs",
           "inset_rounds", "read_settings", "seam_index", "shift_paths"]
