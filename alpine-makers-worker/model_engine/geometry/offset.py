"""Décalage (offset) de Paths : ``offset``, ``offset_polygon``, ``inset_rounds``.

Méthode : pour chaque arête, un rectangle balayé du côté du décalage ; à chaque sommet où le côté s'ouvre, une
jointure (arc, pointe limitée ou chanfrein). Agrandissement (delta > 0) = union(paths + bande) ; rétrécissement
(delta < 0) = différence(paths, bande). Les trous (CW) se traitent par leur orientation. Les parties plus étroites
que 2·|delta| disparaissent, et les lamelles résiduelles d'arrondi (largeur ≤ 3 µm) sont retirées."""
import math

import numpy as np

from .boolean import difference, union
from .poly import INT, area2, as_ring, clean, from_polytree, perimeter_um, to_polytree

JOINS = ("round", "miter", "square")


def _band(ring: np.ndarray, delta: float, side: int, join: str, miter_limit: float, arc_tol: float):
    """Polygones CCW (int64) couvrant la bande de largeur delta du côté ``side`` (+1 droite, −1 gauche) du ring."""
    n = len(ring)
    a = ring.astype(np.float64)
    b = np.roll(a, -1, axis=0)
    d = b - a
    L = np.hypot(d[:, 0], d[:, 1])
    ud = d / L[:, None]
    if side > 0:
        nrm = np.column_stack([ud[:, 1], -ud[:, 0]])
    else:
        nrm = np.column_stack([-ud[:, 1], ud[:, 0]])
    oa_f = a + delta * nrm
    ob_f = b + delta * nrm
    # Coins voisins (fin du quad i, début du quad i+1) à ≤ 2 µm : un seul point partagé exact, sinon l'arrondi
    # crée des recouvrements sub-micrométriques entre quads voisins (cascade d'intersections).
    oa_next_f = np.roll(oa_f, -1, axis=0)
    close = np.abs(ob_f - oa_next_f).max(axis=1) <= 2.0
    shared = np.rint((ob_f + oa_next_f) / 2.0).astype(INT)
    oa = np.rint(oa_f).astype(INT)
    ob = np.rint(ob_f).astype(INT)
    ob[close] = shared[close]
    prev_close = np.roll(close, 1)
    oa[prev_close] = np.roll(shared, 1, axis=0)[prev_close]
    ia = ring
    ib = np.roll(ring, -1, axis=0)
    quads = np.empty((n, 4, 2), dtype=INT)
    if side > 0:
        quads[:, 0], quads[:, 1], quads[:, 2], quads[:, 3] = ia, oa, ob, ib
    else:
        quads[:, 0], quads[:, 1], quads[:, 2], quads[:, 3] = ia, ib, ob, oa
    polys = list(quads)
    # Jointures au sommet b_i (entre l'arête i et i+1) quand le côté du décalage s'ouvre.
    ud_next = np.roll(ud, -1, axis=0)
    nrm_next = np.roll(nrm, -1, axis=0)
    turn = side * (ud[:, 0] * ud_next[:, 1] - ud[:, 1] * ud_next[:, 0])
    cosphi = np.clip((nrm * nrm_next).sum(axis=1), -1.0, 1.0)
    need = (turn > 1e-12) & ~close
    idx = np.flatnonzero(need)
    if not len(idx):
        return polys
    v = ib[idx]
    p1 = ob[idx]                      # fin du quad i
    p2 = np.roll(oa, -1, axis=0)[idx]  # début du quad i+1
    n1, n2 = nrm[idx], nrm_next[idx]
    phi = np.arccos(cosphi[idx])
    if side > 0:
        p_start, p_end, n_start, out_start, out_end = p1, p2, n1, ud[idx], -ud_next[idx]
    else:
        p_start, p_end, n_start, out_start, out_end = p2, p1, n2, -ud_next[idx], ud[idx]
    vf = v.astype(np.float64)
    if join == "round":
        step = 2.0 * math.acos(max(-1.0, min(1.0, 1.0 - arc_tol / delta))) if arc_tol < delta else math.pi
        step = max(step, 0.02)
        m = np.maximum(1, np.ceil(phi / step - 1e-9)).astype(INT)
        inner = m - 1
        tot = int(inner.sum())
        if tot:
            rep = np.repeat(np.arange(len(idx)), inner)
            k = np.arange(tot) - np.repeat(np.cumsum(inner) - inner, inner) + 1
            th0 = np.arctan2(n_start[:, 1], n_start[:, 0])
            sweep = phi / m
            th = th0[rep] + k * sweep[rep]
            arc = np.rint(vf[rep] + delta * np.column_stack([np.cos(th), np.sin(th)])).astype(INT)
            bounds = np.r_[0, np.cumsum(inner)]
        for j in range(len(idx)):
            pts = [v[j], p_start[j]]
            if tot and inner[j]:
                pts.extend(arc[bounds[j]:bounds[j + 1]])
            pts.append(p_end[j])
            polys.append(np.array(pts, dtype=INT))
        return polys
    bis = n1 + n2
    bl = np.hypot(bis[:, 0], bis[:, 1])
    bl = np.where(bl < 1e-12, 1.0, bl)
    mdir = bis / bl[:, None]
    half_cos2 = (1.0 + cosphi[idx]) / 2.0  # cos²(phi/2)
    use_miter = (join == "miter") & (half_cos2 >= 1.0 / (miter_limit * miter_limit)) & (half_cos2 > 1e-9)
    for j in range(len(idx)):
        if use_miter[j]:
            mp = np.rint(vf[j] + delta * bis[j] / (1.0 + cosphi[idx][j])).astype(INT)
            polys.append(np.array([v[j], p_start[j], mp, p_end[j]], dtype=INT))
        else:
            denom = float(out_start[j] @ mdir[j])
            if denom <= 1e-12:
                polys.append(np.array([v[j], p_start[j], p_end[j]], dtype=INT))
                continue
            s = delta * (1.0 - float(n_start[j] @ mdir[j])) / denom
            q1 = np.rint(p_start[j] + s * out_start[j]).astype(INT)
            q2 = np.rint(p_end[j] + s * out_end[j]).astype(INT)
            polys.append(np.array([v[j], p_start[j], q1, q2, p_end[j]], dtype=INT))
    return polys


def _drop_slivers(paths, width_um: float = 3.0):
    """Retire les rings de largeur moyenne ≤ width (lamelles d'arrondi), trous d'un extérieur retiré compris."""
    tree = to_polytree(paths)
    out = []
    for outer, holes in tree:
        if abs(area2(outer)) / 2.0 <= width_um * perimeter_um(outer) / 2.0:
            continue
        kept = [h for h in holes if abs(area2(h)) / 2.0 > width_um * perimeter_um(h) / 2.0]
        out.append((outer, kept))
    return from_polytree(out)


def offset(paths, delta_um, join: str = "round", miter_limit: float = 2.0, arc_tolerance_um: float = 20,
           resolve: bool = True):
    """Décale un Paths de ``delta_um`` (> 0 agrandit, < 0 rétrécit) ; résultat propre (résolu par union/différence).
    ``resolve`` : l'entrée est d'abord résolue par ``union`` (auto-intersections, recouvrements, rings CW seuls) ;
    ``False`` pour un Paths déjà propre (sortie d'une booléenne ou d'un offset), ce qui épargne une passe."""
    if join not in JOINS:
        raise ValueError(f"jointure inconnue : {join}")
    delta = float(delta_um)
    rings = []
    for r in paths:
        c = clean(as_ring(r), 0)
        if c is not None:
            rings.append(c)
    if not rings:
        return []
    if resolve or abs(delta) < 0.5:
        rings = union(rings)
        if not rings or abs(delta) < 0.5:
            return rings
    mag = abs(delta)
    side = 1 if delta > 0 else -1
    band = []
    for r in rings:
        band.extend(_band(r, mag, side, join, max(1.0, float(miter_limit)), max(0.5, float(arc_tolerance_um))))
    if delta > 0:
        return union(rings + band)
    result = difference(rings, band)
    return _drop_slivers(result) if result else result


def offset_polygon(polygon, delta_um, join: str = "round", miter_limit: float = 2.0, arc_tolerance_um: float = 20):
    """Décale un Polygon ``(outer, holes)`` ; renvoie un PolyTree."""
    return to_polytree(offset(from_polytree([polygon]), delta_um, join, miter_limit, arc_tolerance_um))


def inset_rounds(paths, delta_um, count=None, join: str = "round", **kw):
    """Décalages successifs vers l'intérieur de |delta| : liste de Paths, s'arrête quand le résultat est vide
    (ou après ``count`` tours)."""
    step = -abs(float(delta_um))
    rounds = []
    current = paths
    while count is None or len(rounds) < count:
        current = offset(current, step, join, **kw)
        if not current:
            break
        rounds.append(current)
    return rounds
