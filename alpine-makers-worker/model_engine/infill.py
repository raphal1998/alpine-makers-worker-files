"""Remplissage (§5.4 de docs/MODEL_ENGINE_ARCHITECTURE.md) : motifs écrits par Alpine Makers, en µm entiers.

``fill`` trace un motif dans un ``Paths`` et renvoie des polylignes int64 ``(k, 2)`` ; ``fill_layer`` remplit
``layer.infill_lines[object_id]`` par feature à partir de ``layer.surfaces``, ``layer.walls`` et des réglages
résolus (``profiles.process.resolve`` : densités et chevauchement en %, largeurs en mm, angles en degrés).

Calibration de la densité : pour chaque motif, la longueur de trait par mm² vaut densité / largeur de ligne
(± 10 %), ce qui donne l'espacement par motif (``_spacing_for``) :
- rectiligne, aligné, monotone, concentrique : espacement = w / d ;
- grille (2 familles) : 2·w / d ; triangles et cubique (3 familles) : 3·w / d ;
- nid d'abeille (3 arêtes par hexagone d'aire 3√3/2·a²) : côté a = 2 / √3 · w / d ;
- gyroïde : période P = GYROID_K · w / d, GYROID_K mesuré numériquement (longueur de l'isoligne par aire,
  moyennée sur z) ; valeur fixée par ``tests.test_model_engine_infill``.
- lightning (provisoire V1) : rectiligne clairsemé à la moitié de la densité demandée.

Découpe : ``lines.hatch`` + ``lines.clip_segments`` (familles parallèles, rapide) sur l'aire rétrécie de
``w/2 − chevauchement`` ; motifs libres (gyroïde, nid d'abeille) : segments proches du bord par ``clip_segments``,
les autres gardés d'après l'enroulement de leur milieu (vectorisé). Raccord zigzag entre lignes voisines quand la
corde est plus courte que 2·espacement et longe le bord (même arête) ou reste dans l'aire d'origine (bande de
chevauchement comprise, jamais au-delà : une surface voisine n'est pas un ancrage) ; traits < 2·w retirés
(< w/2 pour les surfaces pleines et les ponts, afin de remplir les bandes étroites) ; ponts non raccordés ;
``monotonic`` = chaînes un-à-un ordonnées topologiquement (jamais de retour en arrière).
Déterministe : aucun aléa, ordres de tri stables.
"""
from __future__ import annotations

import heapq
import math

import numpy as np

from .geometry import RESOLUTION_UM, SCALE
from .geometry import lines, poly
from .geometry.offset import offset
from .geometry.poly import INT, as_ring, bbox, to_polytree

# Période du gyroïde = GYROID_K · largeur / densité. Théorie (aire du gyroïde ≈ 3,09 par cellule L³, pente moyenne
# π/4) : longueur d'isoligne par aire ≈ 2,43 / P. Mesure (carré 100 mm, 15 % / 0,42 mm, 32 z sur une période) :
# K = 2,43 → ratio moyen 1,011, K = 2,45 → ≈ 1,00. La longueur d'une couche varie avec z de 0,93 à 1,20 fois la
# moyenne (pics étroits en z = P/8 mod P/4, où les isolignes s'alignent) : c'est inhérent au motif.
GYROID_K = 2.45
# Facteur de densité du motif « lightning » provisoire (V1 : rectiligne clairsemé).
LIGHTNING_DENSITY_FACTOR = 0.5

SPARSE_PATTERNS = ("rectilinear", "aligned_rectilinear", "grid", "triangles", "cubic", "adaptive cubic", "gyroid",
                   "honeycomb", "lightning", "concentric", "monotonic")
SOLID_PATTERNS = ("monotonic", "rectilinear", "concentric", "aligned_rectilinear")
PATTERNS = tuple(dict.fromkeys(SPARSE_PATTERNS + SOLID_PATTERNS))

_EMPTY_SEG = np.zeros((0, 2, 2), dtype=INT)
_SIN60 = math.sqrt(3.0) / 2.0


# ----------------------------------------------------------------------------------------------------------------
# Outils vectorisés
# ----------------------------------------------------------------------------------------------------------------

def _winding_inside(points, paths, chunk: int = 2_000_000) -> np.ndarray:
    """Vrai pour chaque point ``(K, 2)`` dont l'indice d'enroulement dans ``paths`` est > 0 (rayon horizontal
    vers +x, règle demi-ouverte sur y, même convention que ``poly.point_in_ring``) ; par blocs de paires."""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    K = len(pts)
    if K == 0:
        return np.zeros(0, dtype=bool)
    edges = lines.segments_of_paths(paths)
    n = len(edges)
    if n == 0:
        return np.zeros(K, dtype=bool)
    x1 = edges[:, 0, 0].astype(np.float64)
    y1 = edges[:, 0, 1].astype(np.float64)
    x2 = edges[:, 1, 0].astype(np.float64)
    y2 = edges[:, 1, 1].astype(np.float64)
    block = max(1, chunk // n)
    out = np.empty(K, dtype=bool)
    for start in range(0, K, block):
        px = pts[start:start + block, 0][:, None]
        py = pts[start:start + block, 1][:, None]
        up = (y1 <= py) & (y2 > py)
        down = (y2 <= py) & (y1 > py)
        cross = (x2 - x1) * (py - y1) - (px - x1) * (y2 - y1)
        w = (up & (cross > 0)).sum(axis=1) - (down & (cross < 0)).sum(axis=1)
        out[start:start + block] = w > 0
    return out


def _nearest_edge(points, edges, tol_um: float = 1.5, chunk: int = 2_000_000) -> np.ndarray:
    """Indice de la première arête à ≤ ``tol_um`` de chaque point (−1 sinon), par blocs."""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    K, n = len(pts), len(edges)
    out = np.full(K, -1, dtype=np.int64)
    if K == 0 or n == 0:
        return out
    a = edges[:, 0].astype(np.float64)
    d = (edges[:, 1] - edges[:, 0]).astype(np.float64)
    L2 = (d * d).sum(axis=1)
    L2 = np.where(L2 == 0, 1.0, L2)
    block = max(1, chunk // n)
    for start in range(0, K, block):
        p = pts[start:start + block]
        rx = p[:, 0][:, None] - a[:, 0]
        ry = p[:, 1][:, None] - a[:, 1]
        t = np.clip((rx * d[:, 0] + ry * d[:, 1]) / L2, 0.0, 1.0)
        dx = rx - t * d[:, 0]
        dy = ry - t * d[:, 1]
        close = (dx * dx + dy * dy) <= tol_um * tol_um
        has = close.any(axis=1)
        idx = np.argmax(close, axis=1)
        out[start:start + block] = np.where(has, idx, -1)
    return out


def _chords_clear(chords, edges, tol_um: float = 2.0, chunk: int = 2_000_000) -> np.ndarray:
    """Vrai pour chaque corde ``(K, 2, 2)`` qu'aucune arête ne traverse strictement entre ses extrémités (à
    ``tol_um`` près aux deux bouts, qui touchent le bord par construction) ; par blocs."""
    ch = np.asarray(chords, dtype=np.float64).reshape(-1, 2, 2)
    K, n = len(ch), len(edges)
    out = np.ones(K, dtype=bool)
    if K == 0 or n == 0:
        return out
    A = edges[:, 0].astype(np.float64)
    E = (edges[:, 1] - edges[:, 0]).astype(np.float64)
    block = max(1, chunk // n)
    for start in range(0, K, block):
        P = ch[start:start + block, 0]
        R = ch[start:start + block, 1] - P
        L = np.hypot(R[:, 0], R[:, 1])
        px, py = P[:, 0][:, None], P[:, 1][:, None]
        rx, ry = R[:, 0][:, None], R[:, 1][:, None]
        ax, ay = A[:, 0][None, :], A[:, 1][None, :]
        ex, ey = E[:, 0][None, :], E[:, 1][None, :]
        denom = rx * ey - ry * ex
        qx, qy = ax - px, ay - py
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (qx * ey - qy * ex) / denom       # position sur la corde
            s = (qx * ry - qy * rx) / denom       # position sur l'arête
        tol_t = (tol_um / np.maximum(L, 1.0))[:, None]
        cross = (denom != 0) & (s >= -1e-9) & (s <= 1 + 1e-9) & (t > tol_t) & (t < 1 - tol_t)
        out[start:start + block] = ~cross.any(axis=1)
    return out


def _segment_lengths(seg) -> np.ndarray:
    d = (seg[:, 1] - seg[:, 0]).astype(np.float64)
    return np.hypot(d[:, 0], d[:, 1])


def _simplify_polyline(points: np.ndarray, tol_um: float) -> np.ndarray:
    """Douglas–Peucker itératif sur une polyligne ouverte (extrémités conservées)."""
    pts = np.asarray(points, dtype=np.float64)
    n = len(pts)
    if n < 3 or tol_um <= 0:
        return np.asarray(points, dtype=INT)
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    tol2 = float(tol_um) ** 2
    stack = [(0, n - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        a = pts[first]
        d = pts[last] - a
        seg = pts[first + 1:last]
        l2 = float(d @ d)
        if l2 == 0.0:
            dist2 = ((seg - a) ** 2).sum(axis=1)
        else:
            t = np.clip(((seg - a) @ d) / l2, 0.0, 1.0)
            proj = a + t[:, None] * d
            dist2 = ((seg - proj) ** 2).sum(axis=1)
        i = int(np.argmax(dist2))
        if dist2[i] > tol2:
            idx = first + 1 + i
            keep[idx] = True
            stack.append((first, idx))
            stack.append((idx, last))
    return np.asarray(points, dtype=INT)[keep]


def _frame(box, angle_rad: float):
    """Étendue ``(umin, umax, vmin, vmax)`` de la boîte dans le repère tourné de ``angle_rad`` (origine globale,
    pour que les motifs restent alignés d'une couche et d'un objet à l'autre)."""
    xmin, ymin, xmax, ymax = (float(v) for v in box)
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    corners = np.array([[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax]])
    u = corners[:, 0] * c + corners[:, 1] * s
    v = -corners[:, 0] * s + corners[:, 1] * c
    return float(u.min()), float(u.max()), float(v.min()), float(v.max())


def _to_xy(u, v, angle_rad: float) -> np.ndarray:
    """Points (u, v) du repère tourné → (x, y) int64."""
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    u = np.asarray(u, dtype=np.float64)
    v = np.asarray(v, dtype=np.float64)
    out = np.empty(u.shape + (2,), dtype=np.float64)
    out[..., 0] = u * c - v * s
    out[..., 1] = u * s + v * c
    return np.rint(out).astype(INT)


# ----------------------------------------------------------------------------------------------------------------
# Découpe hybride pour les motifs libres
# ----------------------------------------------------------------------------------------------------------------

def _clip_free_segments(seg, area, cell_um: float) -> np.ndarray:
    """Découpe des segments quelconques (courts, ≤ cell_um) par ``area`` : ceux dont le milieu tombe dans une
    cellule touchée par une arête (dilatation 3×3) passent par ``lines.clip_segments`` ; les autres sont gardés
    ou rejetés d'après l'enroulement de leur milieu. Contournement local de ``clip_segments`` (O(m·n) hors
    familles parallèles)."""
    seg = np.asarray(seg, dtype=INT).reshape(-1, 2, 2)
    if not len(seg):
        return _EMPTY_SEG
    edges = lines.segments_of_paths(area)
    if not len(edges):
        return _EMPTY_SEG
    cell = max(1.0, float(cell_um))
    a = edges[:, 0].astype(np.float64)
    d = (edges[:, 1] - edges[:, 0]).astype(np.float64)
    L = np.hypot(d[:, 0], d[:, 1])
    cnt = np.ceil(L / (cell / 2.0)).astype(np.int64) + 1
    tot = int(cnt.sum())
    e = np.repeat(np.arange(len(edges)), cnt)
    i = np.arange(tot) - np.repeat(np.cumsum(cnt) - cnt, cnt)
    t = i / np.maximum(cnt[e] - 1, 1)
    sx = a[e, 0] + t * d[e, 0]
    sy = a[e, 1] + t * d[e, 1]
    kx = np.floor(sx / cell).astype(np.int64)
    ky = np.floor(sy / cell).astype(np.int64)
    off = np.array([-1, 0, 1], dtype=np.int64)
    kx = (kx[:, None, None] + off[None, :, None]).ravel()
    ky = (ky[:, None, None] + off[None, None, :]).ravel()
    big = np.int64(1 << 32)
    marked = np.unique(kx * big + ky)
    mid = seg.sum(axis=1).astype(np.float64) / 2.0
    mk = np.floor(mid[:, 0] / cell).astype(np.int64) * big + np.floor(mid[:, 1] / cell).astype(np.int64)
    near = np.isin(mk, marked)
    parts = []
    far = seg[~near]
    if len(far):
        inside = _winding_inside(mid[~near], area)
        if inside.any():
            parts.append(far[inside])
    if near.any():
        clipped = lines.clip_segments(seg[near], area)
        if len(clipped):
            parts.append(clipped)
    return np.vstack(parts) if parts else _EMPTY_SEG


# ----------------------------------------------------------------------------------------------------------------
# Chaînage des familles de hachures
# ----------------------------------------------------------------------------------------------------------------

def _hatch_family(clip_area, edges, box, angle_rad: float, spacing_um: float, phase_um: float, line_width_um: int,
                  mode: str, anchor_paths=None, min_len_um=None, accept_paths=None) -> list:
    """Une famille de droites parallèles découpée par ``clip_area`` puis chaînée (``mode`` : ``zigzag`` |
    ``monotonic`` | ``none``). Traits plus courts que ``min_len_um`` (défaut 2·w) retirés ; ``accept_paths`` :
    zone où le milieu d'une corde est admis (défaut ``clip_area``)."""
    seg = lines.hatch(box, angle_rad, spacing_um, phase_um)
    if not len(seg):
        return []
    seg = lines.clip_segments(seg, clip_area)
    if not len(seg):
        return []
    min_len = 2.0 * line_width_um if min_len_um is None else float(min_len_um)
    seg = seg[_segment_lengths(seg) >= min_len]
    if not len(seg):
        return []
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    p = seg.astype(np.float64)
    u = p[:, :, 0] * c + p[:, :, 1] * s
    v = -p[:, :, 0] * s + p[:, :, 1] * c
    swap = u[:, 0] > u[:, 1]
    seg = seg.copy()
    seg[swap] = seg[swap][:, ::-1]
    u[swap] = u[swap][:, ::-1]
    k = np.rint((v.mean(axis=1) - phase_um) / spacing_um).astype(np.int64)
    order = np.lexsort((u[:, 0], k))
    seg, u, k = seg[order], u[order], k[order]
    return _chain_segments(seg, u, k, spacing_um, clip_area, edges, mode, anchor_paths, accept_paths)


def _chain_segments(seg, u, k, spacing_um: float, clip_area, edges, mode: str, anchor_paths=None,
                    accept_paths=None) -> list:
    """Chaîne des segments triés par (ligne k, abscisse u) en polylignes. ``zigzag`` : glouton vers la ligne
    suivante ; ``monotonic`` : liaisons un-à-un + ordre topologique ; ``none`` : un segment par polyligne."""
    m = len(seg)
    if mode == "none" or m == 1:
        return [seg[i].copy() for i in range(m)]
    max_chord = 2.0 * spacing_um
    tol = float(spacing_um)
    ends = seg.reshape(-1, 2)  # 2i = début (u min), 2i+1 = fin
    edge_of = _nearest_edge(ends, edges) if len(edges) else np.full(2 * m, -1, dtype=np.int64)
    ends_f = ends.astype(np.float64)
    k_list = k.tolist()
    u0, u1 = u[:, 0].tolist(), u[:, 1].tolist()
    # Lignes → plage d'indices (segments déjà triés par k puis u0).
    line_start = {}
    line_end = {}
    for i, kk in enumerate(k_list):
        if kk not in line_start:
            line_start[kk] = i
        line_end[kk] = i + 1

    def overlaps(i, j):
        return u0[j] < u1[i] + tol and u1[j] > u0[i] - tol

    def succs(i):
        kk = k_list[i] + 1
        if kk not in line_start:
            return []
        return [j for j in range(line_start[kk], line_end[kk]) if overlaps(i, j)]

    def preds(j):
        kk = k_list[j] - 1
        if kk not in line_start:
            return []
        return [i for i in range(line_start[kk], line_end[kk]) if overlaps(i, j)]

    chord_mid = []  # cordes à valider par enroulement (différées, vectorisées) : (indice de chaîne, position)

    def chord_ok(ea, eb):
        """La corde ea → eb (indices d'extrémités) est admise si courte et (même arête du bord, ou milieu dans
        l'aire : validé après coup)."""
        dx = ends_f[eb, 0] - ends_f[ea, 0]
        dy = ends_f[eb, 1] - ends_f[ea, 1]
        if dx * dx + dy * dy >= max_chord * max_chord:
            return None
        if edge_of[ea] >= 0 and edge_of[ea] == edge_of[eb]:
            return True
        return "check"

    used = [False] * m
    chains = []        # liste de listes de (segment, sens) ; sens +1 = u croissant
    pending = []       # (indice chaîne, position dans la chaîne, ea, eb) cordes à vérifier

    def walk(start, monotonic):
        chain = [(start, 1)]
        used[start] = True
        cur, direction = start, 1
        while True:
            tip = 2 * cur + (1 if direction == 1 else 0)
            if monotonic:
                nxt = succs(cur)
                if len(nxt) != 1 or used[nxt[0]] or len(preds(nxt[0])) != 1:
                    break
                cand = [nxt[0]]
            else:
                cand = [j for j in succs(cur) if not used[j]]
                if not cand:
                    break
            best, best_d, best_dir = -1, None, 1
            for j in cand:
                for dj, ej in ((1, 2 * j), (-1, 2 * j + 1)):
                    dx = ends_f[ej, 0] - ends_f[tip, 0]
                    dy = ends_f[ej, 1] - ends_f[tip, 1]
                    d2 = dx * dx + dy * dy
                    if best_d is None or d2 < best_d:
                        best, best_d, best_dir = j, d2, dj
            ej = 2 * best + (0 if best_dir == 1 else 1)
            ok = chord_ok(tip, ej)
            if ok is None:
                break
            if ok == "check":
                pending.append((len(chains), len(chain), tip, ej))
            chain.append((best, best_dir))
            used[best] = True
            cur, direction = best, best_dir
        chains.append(chain)

    monotonic = mode == "monotonic"
    for i in range(m):
        if not used[i]:
            walk(i, monotonic)

    # Validation vectorisée des cordes : milieu dans l'aire admise (ou dans l'ancrage) et aucune arête traversée ;
    # une corde refusée coupe la chaîne.
    if pending:
        ea_idx = np.array([ea for _, _, ea, _ in pending], dtype=np.int64)
        eb_idx = np.array([eb for _, _, _, eb in pending], dtype=np.int64)
        mids = (ends_f[ea_idx] + ends_f[eb_idx]) / 2.0
        ok = _winding_inside(mids, clip_area if accept_paths is None else accept_paths)
        if anchor_paths:
            ok |= _winding_inside(mids, anchor_paths)
        chords = np.stack([ends[ea_idx], ends[eb_idx]], axis=1)
        ok &= _chords_clear(chords, edges)
        if accept_paths is not None:  # la bande admise ne doit pas laisser couper un coin de l'aire d'origine
            ok &= _chords_clear(chords, lines.segments_of_paths(accept_paths))
        cuts = {}
        for (ci, pos, _, _), good in zip(pending, ok.tolist()):
            if not good:
                cuts.setdefault(ci, []).append(pos)
        if cuts:
            new_chains = []
            for ci, chain in enumerate(chains):
                if ci not in cuts:
                    new_chains.append(chain)
                    continue
                last = 0
                for pos in sorted(cuts[ci]):
                    new_chains.append(chain[last:pos])
                    last = pos
                new_chains.append(chain[last:])
            chains = [c for c in new_chains if c]

    # Ordre d'impression.
    chain_of = {}
    for ci, chain in enumerate(chains):
        for sidx, _ in chain:
            chain_of[sidx] = ci
    keys = [(k_list[c[0][0]], u0[c[0][0]], ci) for ci, c in enumerate(chains)]
    if monotonic:
        n_chain = len(chains)
        indeg = [0] * n_chain
        out = [set() for _ in range(n_chain)]
        for ci, chain in enumerate(chains):
            for sidx, _ in chain:
                for j in succs(sidx):
                    cj = chain_of[j]
                    if cj != ci and cj not in out[ci]:
                        out[ci].add(cj)
                        indeg[cj] += 1
        heap = [keys[ci] for ci in range(n_chain) if indeg[ci] == 0]
        heapq.heapify(heap)
        done = [False] * n_chain
        order = []
        remaining = n_chain
        while remaining:
            if not heap:  # cycle (ne devrait pas arriver) : on relance sur la chaîne de plus petite clé
                heap = [keys[ci] for ci in range(n_chain) if not done[ci]]
                heapq.heapify(heap)
            _, _, ci = heapq.heappop(heap)
            if done[ci]:
                continue
            done[ci] = True
            remaining -= 1
            order.append(ci)
            for cj in sorted(out[ci]):
                indeg[cj] -= 1
                if indeg[cj] == 0 and not done[cj]:
                    heapq.heappush(heap, keys[cj])
    else:
        order = [ci for _, _, ci in sorted(keys)]

    result = []
    for ci in order:
        pts = []
        for sidx, direction in chains[ci]:
            a, b = seg[sidx]
            if direction == -1:
                a, b = b, a
            if pts and pts[-1][0] == a[0] and pts[-1][1] == a[1]:
                pts.append(b)
            else:
                pts.append(a)
                pts.append(b)
        result.append(np.array(pts, dtype=INT))
    return result


# ----------------------------------------------------------------------------------------------------------------
# Motifs libres : gyroïde (marching squares) et nid d'abeille
# ----------------------------------------------------------------------------------------------------------------

# Table de marching squares : code → paires d'arêtes (0 = bas, 1 = droite, 2 = haut, 3 = gauche) ; bits :
# 1 = bas-gauche, 2 = bas-droite, 4 = haut-droite, 8 = haut-gauche. 16/17 : variantes des selles 5/10 (centre > 0).
_MS_TABLE = {
    1: ((3, 0),), 2: ((0, 1),), 3: ((3, 1),), 4: ((1, 2),), 5: ((3, 0), (1, 2)), 6: ((0, 2),), 7: ((3, 2),),
    8: ((2, 3),), 9: ((0, 2),), 10: ((0, 1), (2, 3)), 11: ((1, 2),), 12: ((1, 3),), 13: ((0, 1),), 14: ((3, 0),),
    16: ((3, 2), (0, 1)), 17: ((3, 0), (1, 2)),
}


def _marching_squares(F: np.ndarray, us: np.ndarray, vs: np.ndarray) -> np.ndarray:
    """Segments (float64 ``(m, 2, 2)`` en (u, v)) de l'isoligne F = 0 sur la grille ``vs × us`` (F de forme
    (len(vs), len(us))) ; points d'arête partagés exactement entre cellules voisines."""
    ny, nx = F.shape
    if ny < 2 or nx < 2:
        return np.zeros((0, 2, 2), dtype=np.float64)
    s = F > 0
    # Points de traversée des arêtes horizontales (ny, nx-1) et verticales (ny-1, nx).
    f0, f1 = F[:, :-1], F[:, 1:]
    den = f0 - f1
    th = np.where(den != 0, f0 / np.where(den == 0, 1.0, den), 0.5)
    hx = us[:-1][None, :] + th * (us[1:] - us[:-1])[None, :]
    hy = np.broadcast_to(vs[:, None], hx.shape)
    g0, g1 = F[:-1, :], F[1:, :]
    den = g0 - g1
    tv = np.where(den != 0, g0 / np.where(den == 0, 1.0, den), 0.5)
    vy = vs[:-1][:, None] + tv * (vs[1:] - vs[:-1])[:, None]
    vx = np.broadcast_to(us[None, :], vy.shape)
    code = (s[:-1, :-1].astype(np.int64) + 2 * s[:-1, 1:] + 4 * s[1:, 1:] + 8 * s[1:, :-1])
    center = F[:-1, :-1] + F[:-1, 1:] + F[1:, 1:] + F[1:, :-1]
    code = np.where((code == 5) & (center > 0), 16, code)
    code = np.where((code == 10) & (center > 0), 17, code)
    parts = []
    for c, pairs in _MS_TABLE.items():
        ii, jj = np.nonzero(code == c)
        if not len(ii):
            continue
        for ea, eb in pairs:
            pa = _edge_point(ea, ii, jj, hx, hy, vx, vy)
            pb = _edge_point(eb, ii, jj, hx, hy, vx, vy)
            parts.append(np.stack([pa, pb], axis=1))
    if not parts:
        return np.zeros((0, 2, 2), dtype=np.float64)
    return np.vstack(parts)


def _edge_point(edge: int, ii, jj, hx, hy, vx, vy) -> np.ndarray:
    if edge == 0:    # bas : arête horizontale (i, j)
        return np.column_stack([hx[ii, jj], hy[ii, jj]])
    if edge == 2:    # haut : (i + 1, j)
        return np.column_stack([hx[ii + 1, jj], hy[ii + 1, jj]])
    if edge == 3:    # gauche : arête verticale (i, j)
        return np.column_stack([vx[ii, jj], vy[ii, jj]])
    return np.column_stack([vx[ii, jj + 1], vy[ii, jj + 1]])  # droite : (i, j + 1)


def _gyroid_segments(box, angle_rad: float, period_um: float, step_um: float, z_um: int) -> np.ndarray:
    """Isoligne sin U·cos V + sin V·cos Z + sin Z·cos U = 0 (U, V, Z = 2π·(u, v, z)/P) échantillonnée au pas
    ``step_um`` dans le repère tourné, en segments int64 (x, y)."""
    umin, umax, vmin, vmax = _frame(box, angle_rad)
    step = max(1.0, float(step_um))
    nu = int(math.ceil((umax - umin) / step)) + 2
    nv = int(math.ceil((vmax - vmin) / step)) + 2
    us = umin - step + np.arange(nu) * step
    vs = vmin - step + np.arange(nv) * step
    w = 2.0 * math.pi / period_um
    U = us * w
    V = vs * w
    Z = float(z_um) * w
    F = (np.sin(U)[None, :] * np.cos(V)[:, None] + np.sin(V)[:, None] * math.cos(Z)
         + math.sin(Z) * np.cos(U)[None, :])
    seg_uv = _marching_squares(F, us, vs)
    if not len(seg_uv):
        return _EMPTY_SEG
    seg = _to_xy(seg_uv[..., 0], seg_uv[..., 1], angle_rad)
    keep = (seg[:, 0] != seg[:, 1]).any(axis=1)
    return seg[keep]


def _honeycomb_segments(box, angle_rad: float, side_um: float):
    """Arêtes d'un pavage d'hexagones réguliers à sommet en haut (côté ``side_um``), chacune une seule fois :
    zigzags horizontaux (familles ±30°) et côtés verticaux. Renvoie (famille +30°, famille −30°, verticaux)
    en int64 (x, y)."""
    umin, umax, vmin, vmax = _frame(box, angle_rad)
    a = float(side_um)
    pitch = 2.0 * _SIN60 * a      # √3·a : pas horizontal
    half = pitch / 2.0
    r0 = int(math.floor(vmin / (1.5 * a))) - 1
    r1 = int(math.ceil(vmax / (1.5 * a))) + 1
    m0 = int(math.floor(umin / pitch)) - 1
    m1 = int(math.ceil(umax / pitch)) + 1
    ups, downs, verts = [], [], []
    xs_base = np.arange(m0, m1 + 1, dtype=np.float64) * pitch
    for r in range(r0, r1 + 1):
        offset_r = half if (r % 2) else 0.0
        peaks_x = xs_base + offset_r                      # sommets hauts du zigzag r (centres de la rangée r)
        y_peak = 1.5 * a * r + a
        y_valley = 1.5 * a * r + a / 2.0
        # Montées : vallée (x − half, y_valley) → sommet (x, y_peak) ; descentes : sommet → vallée (x + half).
        ups.append(np.stack([np.column_stack([peaks_x - half, np.full_like(peaks_x, y_valley)]),
                             np.column_stack([peaks_x, np.full_like(peaks_x, y_peak)])], axis=1))
        downs.append(np.stack([np.column_stack([peaks_x, np.full_like(peaks_x, y_peak)]),
                               np.column_stack([peaks_x + half, np.full_like(peaks_x, y_valley)])], axis=1))
        # Côtés verticaux de la rangée r : x = centre ± half, de y_c − a/2 à y_c + a/2 (un seul par position).
        vx = peaks_x + half
        yc = 1.5 * a * r
        verts.append(np.stack([np.column_stack([vx, np.full_like(vx, yc - a / 2.0)]),
                               np.column_stack([vx, np.full_like(vx, yc + a / 2.0)])], axis=1))
    out = []
    for fam in (ups, downs, verts):
        uv = np.vstack(fam)
        out.append(_to_xy(uv[..., 0], uv[..., 1], angle_rad))
    return out


# ----------------------------------------------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------------------------------------------

def _spacing_for(pattern: str, density: float, line_width_um: int) -> float:
    """Espacement (ou côté / période) en µm pour que la longueur de trait par aire vaille densité / largeur."""
    base = float(line_width_um) / density
    if pattern == "grid":
        return 2.0 * base
    if pattern in ("triangles", "cubic", "adaptive cubic"):
        return 3.0 * base
    if pattern == "honeycomb":
        return base * 2.0 / math.sqrt(3.0)
    if pattern == "gyroid":
        return GYROID_K * base
    return base


def _inset_series(paths, step_um: float, tol_um: float) -> list:
    """Décalages successifs de −``step_um`` avec simplification entre deux tours (contournement local :
    ``offset.inset_rounds`` accumule des sommets quasi confondus et sa subdivision booléenne devient très lente dès
    quelques tours sur un contour arrondi). Borné par la taille de la boîte."""
    box = bbox(paths)
    if box is None:
        return []
    limit = int(min(box[2] - box[0], box[3] - box[1]) / (2.0 * step_um)) + 2
    out = []
    current = paths
    tol = max(1.0, float(tol_um))
    while len(out) < limit:
        nxt = offset(current, -step_um, resolve=False)
        nxt = [r for r in (poly.simplify(as_ring(r), tol) for r in nxt) if r is not None and len(r) >= 3]
        if not nxt:
            break
        out.append(nxt)
        current = nxt
    return out


def _free_pattern_polylines(seg, clip_area, line_width_um: int, step_um: float, tol_um: float) -> list:
    """Segments d'un motif libre → découpe hybride → chaînage exact → simplification → filtre de longueur."""
    clipped = _clip_free_segments(seg, clip_area, step_um)
    if not len(clipped):
        return []
    out = []
    for pl in lines.connect_segments(clipped, 0):
        pl = _simplify_polyline(pl, tol_um)
        if len(pl) >= 2 and lines.polyline_length_um(pl) >= 2.0 * line_width_um:
            out.append(pl)
    return out


def fill(area, pattern: str, density: float, line_width_um: int, angle_rad: float, layer_index: int, settings: dict,
         *, z_um: int = 0, solid: bool = False, bridge: bool = False, anchor_paths=None) -> list:
    """Trace ``pattern`` dans ``area`` (Paths propre, µm) et renvoie des polylignes int64 ``(k, 2)``.

    ``density`` 0..1 (forcée à 1 si ``solid``) ; ``line_width_um`` largeur de ligne ; ``angle_rad`` direction de
    base ; ``layer_index`` (1 = première couche) alterne l'angle des motifs rectilignes ; ``z_um`` fait varier
    gyroïde et cubique ; ``bridge`` : droites à ``angle_rad`` jamais raccordées ; ``anchor_paths`` : zone
    supplémentaire où le milieu d'une corde de raccord est admis (doit exclure les autres surfaces). ``settings`` :
    ``infill_wall_overlap`` (% de la largeur, défaut 15) et ``resolution`` (mm, défaut 0,012). Aire vide ou densité
    nulle → ``[]`` ; angle non fini → ``ValueError``."""
    if not area or line_width_um <= 0:
        return []
    pattern = str(pattern or "rectilinear").strip().lower()
    if pattern == "adaptive cubic":
        pattern = "cubic"
    if pattern not in PATTERNS:
        raise ValueError(f"motif de remplissage inconnu : {pattern}")
    density = 1.0 if solid else float(density)
    if not math.isfinite(density) or density <= 0.0:
        return []
    density = min(1.0, density)
    if pattern == "lightning":  # V1 provisoire : rectiligne clairsemé (le vrai lightning est en vague D)
        pattern = "rectilinear"
        density = max(0.01, density * LIGHTNING_DENSITY_FACTOR)
    overlap_pct = settings.get("infill_wall_overlap", 15) if settings else 15
    try:
        overlap_pct = float(overlap_pct)
    except (TypeError, ValueError):
        overlap_pct = 15.0
    overlap_um = line_width_um * max(0.0, min(100.0, overlap_pct)) / 100.0
    res = settings.get("resolution") if settings else None
    try:
        tol_um = max(1.0, float(res) * SCALE) if res is not None else float(RESOLUTION_UM)
    except (TypeError, ValueError):
        tol_um = float(RESOLUTION_UM)
    if not math.isfinite(angle_rad):
        raise ValueError("angle de remplissage non fini")
    delta = -(line_width_um / 2.0) + overlap_um
    # Aire de découpe : rétrécie de w/2 − chevauchement (agrandie si le chevauchement dépasse w/2). Le milieu
    # d'une corde est admis dans la plus large des deux aires : bande de chevauchement comprise, jamais au-delà.
    clip_area = offset(area, delta) if abs(delta) >= 0.5 else [as_ring(r) for r in area]
    if not clip_area:
        return []
    accept = area if delta < 0 else clip_area
    box = bbox(clip_area)
    if box is None:
        return []
    edges = lines.segments_of_paths(clip_area)
    spacing = _spacing_for(pattern, density, line_width_um)
    if spacing < 1.0:
        return []
    # Pleins et ponts : traits conservés dès w/2 (bandes étroites remplies) ; clairsemé : ≥ 2·w.
    min_len = line_width_um / 2.0 if (solid or bridge) else 2.0 * line_width_um

    def family(a: float, phase: float, mode: str) -> list:
        return _hatch_family(clip_area, edges, box, a, spacing, phase, line_width_um, mode, anchor_paths, min_len,
                             accept)

    if pattern == "concentric":
        rings = [clip_area] + _inset_series(clip_area, spacing, tol_um)
        out = []
        for paths in rings:
            for r in paths:
                r = as_ring(r)
                if len(r) >= 3 and poly.perimeter_um(r) >= 2.0 * line_width_um:
                    out.append(np.vstack([r, r[:1]]))
        return out

    if bridge:
        return family(angle_rad, 0.0, "none")

    alt = (layer_index % 2) * (math.pi / 2.0)
    if pattern == "rectilinear":
        return family(angle_rad + alt, 0.0, "zigzag")
    if pattern == "aligned_rectilinear":
        return family(angle_rad, 0.0, "zigzag")
    if pattern == "monotonic":
        return family(angle_rad + alt, 0.0, "monotonic")
    if pattern == "grid":
        out = []
        for a in (angle_rad, angle_rad + math.pi / 2.0):
            out.extend(family(a, 0.0, "zigzag"))
        return out
    if pattern == "triangles":
        out = []
        for i in range(3):
            out.extend(family(angle_rad + i * math.pi / 3.0, 0.0, "zigzag"))
        return out
    if pattern == "cubic":
        # Trois familles à 60° dont la phase glisse de z·tan 60° (modulo l'espacement), signe alterné par famille.
        shift = math.fmod(float(z_um) * math.tan(math.pi / 3.0), spacing)
        out = []
        for i, sign in enumerate((1.0, -1.0, 1.0)):
            out.extend(family(angle_rad + i * math.pi / 3.0, sign * shift, "zigzag"))
        return out
    if pattern == "gyroid":
        base = float(line_width_um) / density
        step = min(base / 4.0, spacing / 12.0)
        seg = _gyroid_segments(box, angle_rad, spacing, step, z_um)
        return _free_pattern_polylines(seg, clip_area, line_width_um, step, max(tol_um, line_width_um / 8.0))
    if pattern == "honeycomb":
        return _honeycomb_polylines(clip_area, box, angle_rad, spacing, line_width_um)
    raise ValueError(f"motif de remplissage non géré : {pattern}")


def _honeycomb_polylines(clip_area, box, angle_rad: float, side_um: float, line_width_um: int) -> list:
    """Nid d'abeille découpé : par rangée, le zigzag (montées + descentes chaînées exactement) puis les côtés
    verticaux qui partent de ses sommets ; chaque arête une seule fois (sommets de degré 3 : un chemin unique
    est impossible, les verticaux restent des traits courts)."""
    ups, downs, verts = _honeycomb_segments(box, angle_rad, side_um)
    parts = []
    for fam in (ups, downs):
        if len(fam):
            c = lines.clip_segments(fam, clip_area)
            if len(c):
                parts.append(c)
    zig = np.vstack(parts) if parts else _EMPTY_SEG
    vert = lines.clip_segments(verts, clip_area) if len(verts) else _EMPTY_SEG
    if not len(zig) and not len(vert):
        return []
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    a = float(side_um)

    def rows(seg, kind):
        mid = seg.sum(axis=1).astype(np.float64) / 2.0
        v = -mid[:, 0] * s + mid[:, 1] * c
        u = mid[:, 0] * c + mid[:, 1] * s
        if kind == "zig":
            r = np.floor((v - a / 2.0) / (1.5 * a)).astype(np.int64)
        else:
            r = np.rint(v / (1.5 * a)).astype(np.int64) - 1  # côtés de la rangée r + 1 rattachés au zigzag r
        return r, u

    rz, uz = rows(zig, "zig") if len(zig) else (np.zeros(0, dtype=np.int64), np.zeros(0))
    rv, uv = rows(vert, "vert") if len(vert) else (np.zeros(0, dtype=np.int64), np.zeros(0))
    out = []
    min_len = 2.0 * line_width_um
    for r in sorted(set(rz.tolist()) | set(rv.tolist())):
        sel = zig[rz == r]
        if len(sel):
            for pl in lines.connect_segments(sel, 0):
                if len(pl) >= 2 and lines.polyline_length_um(pl) >= min_len:
                    out.append(pl)
        selv = vert[rv == r]
        if len(selv):
            order = np.argsort(uv[rv == r], kind="stable")
            for segm in selv[order]:
                if _segment_lengths(segm[None])[0] >= min_len:
                    out.append(segm.copy())
    return out


def _um(settings: dict, key: str, default_mm: float) -> int:
    try:
        return max(1, int(round(float(settings.get(key, default_mm)) * SCALE)))
    except (TypeError, ValueError):
        return max(1, int(round(default_mm * SCALE)))


def _num(settings: dict, key: str, default: float) -> float:
    try:
        v = float(settings.get(key, default))
    except (TypeError, ValueError):
        return float(default)
    return v if math.isfinite(v) else float(default)


def fill_layer(layer, settings: dict) -> None:
    """Remplit ``layer.infill_lines[object_id]`` = ``{"SparseInfill", "TopSurface", "BottomSurface",
    "InternalSolidInfill", "Bridge"}`` → polylignes, pour chaque objet de ``layer.surfaces``. Les îlots clairsemés
    d'aire < ``minimum_sparse_infill_area`` (mm²) sont remplis plein (motif plein interne). Couche 1 : largeur
    ``initial_layer_line_width`` pour toutes les features. Les cordes de raccord restent dans l'aire de chaque
    feature (bande de chevauchement comprise) : ``walls.inner_area`` n'est pas un ancrage, il contient les
    surfaces pleines voisines."""
    settings = settings or {}
    first = layer.index == 1
    sparse_w = _um(settings, "initial_layer_line_width" if first else "sparse_infill_line_width", 0.45)
    solid_w = _um(settings, "initial_layer_line_width" if first else "internal_solid_infill_line_width", 0.42)
    top_w = _um(settings, "initial_layer_line_width" if first else "top_surface_line_width", 0.42)
    sparse_pattern = str(settings.get("sparse_infill_pattern", "gyroid"))
    density = max(0.0, min(100.0, _num(settings, "sparse_infill_density", 15.0))) / 100.0
    infill_angle = math.radians(_num(settings, "infill_direction", 45.0))
    solid_angle = math.radians(_num(settings, "solid_infill_direction", 45.0))
    top_pattern = str(settings.get("top_surface_pattern", "monotonic"))
    bottom_pattern = str(settings.get("bottom_surface_pattern", "monotonic"))
    solid_pattern = str(settings.get("internal_solid_infill_pattern", "rectilinear"))
    min_area_mm2 = max(0.0, _num(settings, "minimum_sparse_infill_area", 15.0))
    bridge_density = max(0.1, min(1.0, _num(settings, "bridge_density", 100.0) / 100.0))
    forced_bridge = _num(settings, "bridge_angle", 0.0)
    idx = layer.index
    z = int(layer.z_um)

    for object_id, surf in layer.surfaces.items():
        out = {"SparseInfill": [], "TopSurface": [], "BottomSurface": [], "InternalSolidInfill": [], "Bridge": []}
        sparse_area, small = [], []
        if surf.sparse:
            if density > 0.0 and min_area_mm2 > 0.0:
                for outer, holes in to_polytree(surf.sparse):
                    net = poly.area_mm2(outer) + sum(poly.area_mm2(h) for h in holes)
                    (sparse_area if net >= min_area_mm2 else small).extend([outer] + list(holes))
            elif density > 0.0:
                sparse_area = list(surf.sparse)
        if sparse_area:
            out["SparseInfill"] = fill(sparse_area, sparse_pattern, density, sparse_w, infill_angle, idx, settings,
                                       z_um=z)
        if surf.top_solid:
            out["TopSurface"] = fill(surf.top_solid, top_pattern, 1.0, top_w, solid_angle, idx, settings, z_um=z,
                                     solid=True)
        if surf.bottom_solid:
            out["BottomSurface"] = fill(surf.bottom_solid, bottom_pattern, 1.0, solid_w, solid_angle, idx, settings,
                                        z_um=z, solid=True)
        internal = list(surf.internal_solid) + small
        if internal:
            out["InternalSolidInfill"] = fill(internal, solid_pattern, 1.0, solid_w, solid_angle, idx, settings,
                                              z_um=z, solid=True)
        if surf.bridge:
            angle = math.radians(forced_bridge) if forced_bridge else float(surf.bridge_angle_rad or 0.0)
            out["Bridge"] = fill(surf.bridge, "aligned_rectilinear", bridge_density, solid_w, angle, idx, settings,
                                 z_um=z, bridge=True)
        layer.infill_lines[object_id] = out


__all__ = ["GYROID_K", "LIGHTNING_DENSITY_FACTOR", "PATTERNS", "SOLID_PATTERNS", "SPARSE_PATTERNS", "fill",
           "fill_layer"]
