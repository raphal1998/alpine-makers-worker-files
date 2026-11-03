"""Segments et polylignes (µm entiers) : hachures, découpe de segments par des Paths, chaînage.

``clip_segments`` : pour chaque segment, les arêtes des Paths qui coupent sa droite support sont trouvées par
balayage (familles de segments parallèles : plage d'arêtes par tri des ordonnées dans le repère des segments ;
sinon toutes les arêtes), les traversées sont calculées exactement en entiers et triées le long du segment ;
l'indice d'enroulement part de 0 hors de tout et varie à chaque traversée : les tronçons où il est > 0 sont gardés."""
import math

import numpy as np

from .poly import INT, as_ring

_EMPTY_SEG = np.zeros((0, 2, 2), dtype=INT)


def segments_of_ring(ring) -> np.ndarray:
    """Arêtes d'un ring fermé : int64 ``(n, 2, 2)``."""
    ring = as_ring(ring)
    if len(ring) < 2:
        return _EMPTY_SEG
    out = np.empty((len(ring), 2, 2), dtype=INT)
    out[:, 0] = ring
    out[:, 1] = np.roll(ring, -1, axis=0)
    return out


def segments_of_paths(paths) -> np.ndarray:
    """Toutes les arêtes d'un Paths, segments nuls retirés."""
    parts = [segments_of_ring(r) for r in paths if len(r) >= 2]
    if not parts:
        return _EMPTY_SEG
    seg = np.vstack(parts)
    keep = (seg[:, 0] != seg[:, 1]).any(axis=1)
    return seg[keep]


def hatch(bbox, angle_rad: float, spacing_um: float, phase_um: float = 0) -> np.ndarray:
    """Droites parallèles d'angle ``angle_rad`` espacées de ``spacing_um`` couvrant la boîte ``(xmin, ymin, xmax, ymax)``
    (dépassement de 2 µm aux extrémités). Segments int64 ``(m, 2, 2)``, ordonnés par position transversale."""
    if bbox is None or spacing_um <= 0:
        return _EMPTY_SEG
    xmin, ymin, xmax, ymax = (float(v) for v in bbox)
    if xmax < xmin or ymax < ymin:
        return _EMPTY_SEG
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    corners = np.array([[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax]])
    along = corners[:, 0] * c + corners[:, 1] * s
    across = -corners[:, 0] * s + corners[:, 1] * c
    dmin, dmax = along.min() - 2.0, along.max() + 2.0
    nmin, nmax = across.min(), across.max()
    k0 = math.ceil((nmin - phase_um) / spacing_um - 1e-9)
    k1 = math.floor((nmax - phase_um) / spacing_um + 1e-9)
    if k1 < k0:
        return _EMPTY_SEG
    o = phase_um + np.arange(k0, k1 + 1, dtype=np.float64) * spacing_um
    out = np.empty((len(o), 2, 2), dtype=np.float64)
    out[:, 0, 0] = -o * s + dmin * c
    out[:, 0, 1] = o * c + dmin * s
    out[:, 1, 0] = -o * s + dmax * c
    out[:, 1, 1] = o * c + dmax * s
    return np.rint(out).astype(INT)


def _pairs_parallel(seg, edges):
    """Candidats (segment, arête) pour une famille de segments parallèles (à 0,02 rad près, sens libre) : arêtes
    dont la plage transversale rencontre celle de la droite support du segment, évaluée sur toute l'étendue des
    arêtes (un segment court ou légèrement désaligné voit sa droite dériver : les traversées lointaines comptent
    pour l'enroulement au départ du segment)."""
    d = (seg[:, 1] - seg[:, 0]).astype(np.float64)
    ref = d[0] / math.hypot(d[0, 0], d[0, 1])
    sign = np.where(d @ ref < 0, -1.0, 1.0)  # segments antiparallèles ramenés dans le même sens
    ang = math.atan2(float((d[:, 1] * sign).sum()), float((d[:, 0] * sign).sum()))
    c, s = math.cos(ang), math.sin(ang)
    su = seg[:, :, 0] * c + seg[:, :, 1] * s        # abscisse le long de la famille
    sv = -seg[:, :, 0] * s + seg[:, :, 1] * c       # ordonnée transversale
    eu = edges[:, :, 0] * c + edges[:, :, 1] * s
    ev = -edges[:, :, 0] * s + edges[:, :, 1] * c
    u0, u1 = float(eu.min()), float(eu.max())
    du = su[:, 1] - su[:, 0]
    du = np.where(np.abs(du) < 1e-9, 1e-9, du)
    slope = (sv[:, 1] - sv[:, 0]) / du
    v_at_u0 = sv[:, 0] + (u0 - su[:, 0]) * slope
    v_at_u1 = sv[:, 0] + (u1 - su[:, 0]) * slope
    smin = np.minimum(sv.min(axis=1), np.minimum(v_at_u0, v_at_u1)) - 2.0
    smax = np.maximum(sv.max(axis=1), np.maximum(v_at_u0, v_at_u1)) + 2.0
    extent = float((smax - smin).max())
    emin, emax = ev.min(axis=1) - 2.0, ev.max(axis=1) + 2.0
    order = np.argsort(smin, kind="stable")
    smin_s = smin[order]
    lo = np.searchsorted(smin_s, emin - extent, side="left")
    hi = np.searchsorted(smin_s, emax, side="right")
    cnt = hi - lo
    tot = int(cnt.sum())
    if not tot:
        return np.zeros(0, dtype=INT), np.zeros(0, dtype=INT)
    e = np.repeat(np.arange(len(edges)), cnt)
    k = np.repeat(lo, cnt) + (np.arange(tot) - np.repeat(np.cumsum(cnt) - cnt, cnt))
    sidx = order[k]
    keep = (smax[sidx] >= emin[e]) & (smin[sidx] <= emax[e])
    return sidx[keep], e[keep]


def _is_parallel_family(seg) -> bool:
    d = (seg[:, 1] - seg[:, 0]).astype(np.float64)
    L = np.hypot(d[:, 0], d[:, 1])
    if (L == 0).any():
        return False
    u = d / L[:, None]
    ref = u[0]
    cross = np.abs(u[:, 0] * ref[1] - u[:, 1] * ref[0])
    return bool(cross.max() < 0.02)


_PAIR_CHUNK = 2_000_000


def clip_segments(segments, paths) -> np.ndarray:
    """Parties des segments situées à l'intérieur des Paths (règle positive). Segments int64 ``(k, 2, 2)``,
    dans l'ordre des segments d'entrée puis le long de chacun. Famille parallèle : candidats par balayage ;
    sinon toutes les paires, par blocs de segments pour borner la mémoire (temps O(m·n))."""
    seg = np.asarray(segments, dtype=INT).reshape(-1, 2, 2)
    seg = seg[(seg[:, 0] != seg[:, 1]).any(axis=1)]
    edges = segments_of_paths(paths)
    if not len(seg) or not len(edges):
        return _EMPTY_SEG
    if _is_parallel_family(seg):
        si, ei = _pairs_parallel(seg, edges)
        return _clip_pairs(seg, edges, si, ei)
    m, n = len(seg), len(edges)
    block = max(1, _PAIR_CHUNK // n)
    parts = []
    for start in range(0, m, block):
        stop = min(m, start + block)
        si = np.repeat(np.arange(start, stop), n)
        ei = np.tile(np.arange(n), stop - start)
        part = _clip_pairs(seg, edges, si, ei)
        if len(part):
            parts.append(part)
    return np.vstack(parts) if parts else _EMPTY_SEG


def _clip_pairs(seg, edges, si, ei) -> np.ndarray:
    """Traversées exactes des droites support des segments par les arêtes candidates, puis tronçons à enroulement > 0."""
    if not len(si):
        return _EMPTY_SEG
    p = seg[si, 0]
    r = seg[si, 1] - p
    q = edges[ei, 0]
    s = edges[ei, 1] - q
    side1 = r[:, 0] * (q[:, 1] - p[:, 1]) - r[:, 1] * (q[:, 0] - p[:, 0])
    q2 = q + s
    side2 = r[:, 0] * (q2[:, 1] - p[:, 1]) - r[:, 1] * (q2[:, 0] - p[:, 0])
    crossing = (side1 > 0) != (side2 > 0)
    if not crossing.any():
        return _EMPTY_SEG
    si, p, r, q, s, side2 = si[crossing], p[crossing], r[crossing], q[crossing], s[crossing], side2[crossing]
    denom = r[:, 0] * s[:, 1] - r[:, 1] * s[:, 0]
    tnum = (q[:, 0] - p[:, 0]) * s[:, 1] - (q[:, 1] - p[:, 1]) * s[:, 0]
    t = tnum.astype(np.float64) / denom.astype(np.float64)
    delta = np.where(side2 > 0, -1, 1).astype(INT)
    order = np.lexsort((t, si))
    si, t, delta = si[order], t[order], delta[order]
    w = np.cumsum(delta)
    same = si[1:] == si[:-1]
    inside = same & (w[:-1] > 0)
    if not inside.any():
        return _EMPTY_SEG
    idx = np.flatnonzero(inside)
    ta = np.clip(t[idx], 0.0, 1.0)
    tb = np.clip(t[idx + 1], 0.0, 1.0)
    ok = tb > ta
    idx, ta, tb = idx[ok], ta[ok], tb[ok]
    if not len(idx):
        return _EMPTY_SEG
    k = si[idx]
    p0 = seg[k, 0].astype(np.float64)
    rv = (seg[k, 1] - seg[k, 0]).astype(np.float64)
    out = np.empty((len(idx), 2, 2), dtype=INT)
    out[:, 0] = np.rint(p0 + ta[:, None] * rv).astype(INT)
    out[:, 1] = np.rint(p0 + tb[:, None] * rv).astype(INT)
    keep = (out[:, 0] != out[:, 1]).any(axis=1)
    return out[keep]


def distance_point_segment_um(point, a, b) -> float:
    """Distance euclidienne (µm) du point au segment [a, b]."""
    px, py = float(point[0]), float(point[1])
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / l2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def polyline_length_um(polyline) -> float:
    """Longueur d'une polyligne ouverte ``(k, 2)``."""
    pl = np.asarray(polyline, dtype=np.float64).reshape(-1, 2)
    if len(pl) < 2:
        return 0.0
    d = np.diff(pl, axis=0)
    return float(np.hypot(d[:, 0], d[:, 1]).sum())


def connect_segments(segments, max_gap_um: float = 0) -> list:
    """Chaîne les segments bout à bout (extrémités distantes de ≤ max_gap) en polylignes int64 ``(k, 2)``,
    chacune parcourue dans l'ordre de chaînage ; glouton par grille, sans récursion."""
    seg = np.asarray(segments, dtype=INT).reshape(-1, 2, 2)
    m = len(seg)
    if m == 0:
        return []
    gap = max(0.0, float(max_gap_um))
    cell = max(1, int(math.ceil(gap)))
    ends = seg.reshape(-1, 2)  # 2m points : indice 2i = début, 2i+1 = fin
    cells = {}
    cx = (ends[:, 0] // cell).tolist()
    cy = (ends[:, 1] // cell).tolist()
    for i in range(2 * m):
        cells.setdefault((cx[i], cy[i]), []).append(i)
    used = [False] * m
    ends_l = ends.tolist()
    gap2 = gap * gap
    reach = 0 if gap == 0 else 1

    def nearest(point):
        bx, by = point[0] // cell, point[1] // cell
        best, best_d = -1, None
        for ox in range(-reach, reach + 1):
            for oy in range(-reach, reach + 1):
                for e in cells.get((bx + ox, by + oy), ()):
                    if used[e >> 1]:
                        continue
                    ex, ey = ends_l[e]
                    d2 = (ex - point[0]) ** 2 + (ey - point[1]) ** 2
                    if d2 <= gap2 and (best_d is None or d2 < best_d):
                        best, best_d = e, d2
        return best

    out = []
    for i in range(m):
        if used[i]:
            continue
        used[i] = True
        chain = [ends_l[2 * i], ends_l[2 * i + 1]]
        for direction in (1, -1):
            while True:
                tip = chain[-1] if direction == 1 else chain[0]
                e = nearest(tip)
                if e < 0:
                    break
                used[e >> 1] = True
                other = ends_l[e ^ 1]
                if direction == 1:
                    chain.append(other)
                else:
                    chain.insert(0, other)
        out.append(np.array(chain, dtype=INT))
    return out
