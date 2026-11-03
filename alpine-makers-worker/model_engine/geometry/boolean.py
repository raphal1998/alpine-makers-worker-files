"""Opérations booléennes sur Paths (µm entiers) : union, différence, intersection, xor.

Méthode (code Alpine Makers) :
1. toutes les arêtes des opérandes (un « tag » par opérande) sont découpées à leurs intersections mutuelles,
   calculées exactement en entiers (produits vectoriels int64) et arrondies au µm ; les passes se répètent tant
   qu'un arrondi crée une nouvelle intersection (grille de hachage spatial pour les candidats) ;
2. le graphe planaire (nœuds = points uniques, arêtes non orientées fusionnées, contribution d'enroulement par tag)
   est parcouru par demi-arêtes (ordre angulaire par nœud, « tourner le plus à gauche ») pour obtenir les faces ;
3. l'indice d'enroulement de chaque face est propagé face par face depuis la face extérieure de chaque composante
   (lancer de rayon exact depuis son nœud le plus à gauche) ;
4. les faces retenues par l'opération donnent les arêtes frontières, chaînées en rings propres (extérieurs CCW,
   trous CW, sans sommet répété, sans point aligné).
Robuste aux segments colinéaires superposés, sommets confondus, arêtes horizontales, trous tangents, opérandes
identiques et résultats vides. Coordonnées acceptées : |x|, |y| ≤ 524 288 µm (52 cm, borne int64 des produits)."""
from bisect import bisect_left

import numpy as np

from .poly import INT, _remove_collinear, area2, as_ring

LIMIT_UM = 1 << 19
MAX_PASSES = 6
_OFF = 1 << 21
_KEY = 1 << 23
_MAX_GROUPS_MULTI = 12

_EMPTY2 = np.zeros((0, 2), dtype=INT)


# ---------------------------------------------------------------------------------------------------------------
# 1. Arêtes et découpe aux intersections
# ---------------------------------------------------------------------------------------------------------------

def _edges_from_groups(groups):
    """Arêtes (P → Q) de tous les rings, sans segment nul, avec le numéro d'opérande (tag)."""
    rings, lens, tags = [], [], []
    for g, paths in enumerate(groups):
        for ring in paths:
            r = as_ring(ring)
            if len(r) < 3:
                continue
            rings.append(r)
            lens.append(len(r))
            tags.append(g)
    if not rings:
        return _EMPTY2, _EMPTY2, np.zeros(0, dtype=np.int32)
    lens = np.array(lens, dtype=INT)
    P = np.vstack(rings)
    total = len(P)
    ends = np.cumsum(lens) - 1
    nxt = np.arange(1, total + 1)
    nxt[ends] = ends - lens + 1
    Q = P[nxt]
    tag = np.repeat(np.array(tags, dtype=np.int32), lens)
    keep = (P != Q).any(axis=1)
    if not keep.all():
        P, Q, tag = P[keep], Q[keep], tag[keep]
    if not len(P):
        return _EMPTY2, _EMPTY2, np.zeros(0, dtype=np.int32)
    if np.abs(P).max() > LIMIT_UM:
        raise ValueError("coordonnées hors de ±524 288 µm")
    return P, Q, tag


def _candidate_pairs(P, Q, fresh=None, chunk=3_000_000):
    """Paires d'arêtes (i < j) dont les boîtes se chevauchent, par grille de hachage spatial."""
    E = len(P)
    if E < 2:
        return np.zeros(0, dtype=INT), np.zeros(0, dtype=INT)
    lo = np.minimum(P, Q)
    hi = np.maximum(P, Q)
    ext = (hi - lo).max(axis=1)
    cell = max(int(np.percentile(ext, 50)) * 2, 64)
    span = int((hi.max(axis=0) - lo.min(axis=0)).max()) + 1
    while True:
        c0 = lo // cell
        c1 = hi // cell
        wx = c1[:, 0] - c0[:, 0] + 1
        ncell = wx * (c1[:, 1] - c0[:, 1] + 1)
        total = int(ncell.sum())
        if total <= 6 * E or cell >= span:
            break
        cell *= 2
    rep = np.repeat(np.arange(E), ncell)
    offs = np.arange(total) - np.repeat(np.cumsum(ncell) - ncell, ncell)
    wr = np.repeat(wx, ncell)
    cx = np.repeat(c0[:, 0], ncell) + offs % wr
    cy = np.repeat(c0[:, 1], ncell) + offs // wr
    key = (cx + (1 << 30)) * (1 << 32) + (cy + (1 << 30))
    order = np.argsort(key, kind="stable")
    key_s = key[order]
    edge_s = rep[order]
    cx_s, cy_s = cx[order], cy[order]
    starts = np.flatnonzero(np.r_[True, key_s[1:] != key_s[:-1]])
    sizes = np.diff(np.r_[starts, len(key_s)])
    gstart = np.repeat(starts, sizes)      # début du groupe de chaque entrée
    gsize = np.repeat(sizes, sizes)
    if fresh is None:
        # Toutes les paires (k, l) d'un même groupe, k < l : générées depuis k.
        gen = np.arange(len(key_s))
        cnt = gsize - (gen - gstart) - 1
        first = gen + 1
    else:
        # Seules les paires contenant une arête fraîche : générées depuis chaque entrée fraîche, vers tout le groupe.
        gen = np.flatnonzero(fresh[edge_s])
        cnt = gsize[gen] - 1
        first = gstart[gen]
    csum = np.cumsum(cnt)
    found_a, found_b = [], []
    start = 0
    n_el = len(cnt)
    while start < n_el:
        base = int(csum[start - 1]) if start > 0 else 0
        end = int(np.searchsorted(csum, base + chunk, side="right"))
        end = max(end, start + 1)
        c = cnt[start:end]
        tot = int(c.sum())
        if tot:
            g = gen[start:end]
            ri = np.repeat(g, c)
            ofs = np.arange(tot) - np.repeat(np.cumsum(c) - c, c)
            rj = np.repeat(first[start:end], c) + ofs
            if fresh is not None:
                rj = np.where(rj >= ri, rj + 1, rj)  # saute l'entrée génératrice elle-même
            i = edge_s[ri]
            j = edge_s[rj]
            a = np.minimum(i, j)
            b = np.maximum(i, j)
            olx = np.maximum(lo[a, 0], lo[b, 0])
            oly = np.maximum(lo[a, 1], lo[b, 1])
            # Une paire n'est retenue que dans la cellule du coin inférieur gauche du recouvrement des boîtes :
            # chaque paire est ainsi produite une seule fois, sans tri de dédoublonnage.
            keep = ((olx <= np.minimum(hi[a, 0], hi[b, 0])) & (oly <= np.minimum(hi[a, 1], hi[b, 1]))
                    & (olx // cell == cx_s[ri]) & (oly // cell == cy_s[ri]) & (a != b))
            if fresh is not None:
                keep &= ~(fresh[a] & fresh[b]) | (i == a)  # deux fraîches : produite une seule fois, depuis la plus petite
            a, b = a[keep], b[keep]
            if len(a):
                found_a.append(a)
                found_b.append(b)
        start = end
    if not found_a:
        return np.zeros(0, dtype=INT), np.zeros(0, dtype=INT)
    return np.concatenate(found_a), np.concatenate(found_b)


def _split_points(P, Q, I, J):
    """Points de découpe (indice d'arête, point arrondi) pour chaque paire candidate."""
    p = P[I]
    r = Q[I] - p
    q = P[J]
    s = Q[J] - q
    qp = q - p
    denom = r[:, 0] * s[:, 1] - r[:, 1] * s[:, 0]
    tn = qp[:, 0] * s[:, 1] - qp[:, 1] * s[:, 0]
    un = qp[:, 0] * r[:, 1] - qp[:, 1] * r[:, 0]
    out_e, out_x = [], []
    proper = denom != 0
    if proper.any():
        idx = np.flatnonzero(proper)
        d, t, u = denom[idx], tn[idx], un[idx]
        neg = d < 0
        d = np.where(neg, -d, d)
        t = np.where(neg, -t, t)
        u = np.where(neg, -u, u)
        ok = (t >= 0) & (t <= d) & (u >= 0) & (u <= d)
        idx, d, t, u = idx[ok], d[ok], t[ok], u[ok]
        if len(idx):
            # Point exact p + (t/d)·r arrondi au µm en entiers (demi vers le haut), relativement au coin de la
            # paire pour rester dans int64 : même résultat quel que soit l'ordre des opérandes.
            o = np.minimum(np.minimum(P[I[idx]], Q[I[idx]]), np.minimum(P[J[idx]], Q[J[idx]]))
            dd = d[:, None]
            num = (p[idx] - o) * dd + t[:, None] * r[idx]
            X = num // dd + ((num % dd) * 2 >= dd) + o
            in_i = (t > 0) & (t < d)
            in_j = (u > 0) & (u < d)
            out_e.append(I[idx][in_i])
            out_x.append(X[in_i])
            out_e.append(J[idx][in_j])
            out_x.append(X[in_j])
    col = (~proper) & (un == 0)
    if col.any():
        idx = np.flatnonzero(col)
        pi, ri, qi, si = p[idx], r[idx], q[idx], s[idx]
        rr = (ri * ri).sum(axis=1)
        ss = (si * si).sum(axis=1)
        for X in (qi, qi + si):
            dot = ((X - pi) * ri).sum(axis=1)
            m = (dot > 0) & (dot < rr)
            out_e.append(I[idx][m])
            out_x.append(X[m])
        for X in (pi, pi + ri):
            dot = ((X - qi) * si).sum(axis=1)
            m = (dot > 0) & (dot < ss)
            out_e.append(J[idx][m])
            out_x.append(X[m])
    if not out_e:
        return np.zeros(0, dtype=INT), _EMPTY2
    return np.concatenate(out_e), np.vstack(out_x)


def _apply_splits(P, Q, tag, se, sx):
    """Découpe les arêtes aux points donnés (triés le long de chaque arête) ; retire les sous-arêtes nulles."""
    E = len(P)
    r = Q - P
    rr = (r * r).sum(axis=1)
    ids = np.concatenate([np.arange(E), np.arange(E), se])
    pts = np.vstack([P, Q, sx])
    par = np.concatenate([np.zeros(E, dtype=INT), rr, ((sx - P[se]) * r[se]).sum(axis=1)])
    order = np.lexsort((pts[:, 1], pts[:, 0], par, ids))  # ex æquo départagés par (x, y) : ordre canonique
    ids, pts = ids[order], pts[order]
    same = ids[1:] == ids[:-1]
    a = pts[:-1][same]
    b = pts[1:][same]
    src = ids[:-1][same]
    nz = (a != b).any(axis=1)
    had = np.zeros(E, dtype=bool)
    had[se] = True
    return a[nz], b[nz], tag[src[nz]], had[src[nz]]


def _subdivide(P, Q, tag):
    """Passes de découpe jusqu'à ce qu'aucun arrondi ne crée de nouvelle intersection."""
    fresh = None
    for _ in range(MAX_PASSES):
        I, J = _candidate_pairs(P, Q, fresh)
        if not len(I):
            break
        se, sx = _split_points(P, Q, I, J)
        if not len(se):
            break
        n_before = len(P)
        P, Q, tag, fresh = _apply_splits(P, Q, tag, se, sx)
        if len(P) == n_before:
            break
    return P, Q, tag


# ---------------------------------------------------------------------------------------------------------------
# 2. Graphe planaire, faces, enroulements
# ---------------------------------------------------------------------------------------------------------------

def _winding_left_of(nodes, eu, ev, c, x0, y0):
    """Enroulement (par tag) juste à gauche du point (x0, y0) : arêtes coupant la demi-droite vers −x."""
    xu, yu = nodes[eu, 0], nodes[eu, 1]
    xv, yv = nodes[ev, 0], nodes[ev, 1]
    ymin = np.minimum(yu, yv)
    ymax = np.maximum(yu, yv)
    spans = (ymin <= y0) & (y0 < ymax)
    if not spans.any():
        return np.zeros(c.shape[1], dtype=np.int64)
    idx = np.flatnonzero(spans)
    xu, yu, xv, yv = xu[idx], yu[idx], xv[idx], yv[idx]
    val = (xu - x0) * (yv - yu) + (y0 - yu) * (xv - xu)
    down = yv < yu
    left = np.where(down, val > 0, val < 0)
    sgn = np.where(down, 1, -1)[left]
    return (c[idx[left]].astype(np.int64) * sgn[:, None]).sum(axis=0)


def _arrangement(P, Q, tag, T):
    """Construit le graphe planaire et renvoie (nodes, tail, head, angle, order, label, w, cor)."""
    E = len(P)
    allpts = np.vstack([P, Q])
    key = (allpts[:, 0] + _OFF) * _KEY + (allpts[:, 1] + _OFF)
    ukey, inv = np.unique(key, return_inverse=True)
    inv = inv.reshape(-1)
    N = len(ukey)
    nodes = np.column_stack([ukey // _KEY - _OFF, ukey % _KEY - _OFF]).astype(INT)
    a, b = inv[:E], inv[E:]
    u = np.minimum(a, b)
    v = np.maximum(a, b)
    sign = np.where(a < b, 1, -1).astype(np.int32)
    ekey = u.astype(INT) * N + v
    uek, einv = np.unique(ekey, return_inverse=True)
    einv = einv.reshape(-1)
    c = np.zeros((len(uek), T), dtype=np.int32)
    np.add.at(c, (einv, tag), sign)
    keep = (c != 0).any(axis=1)
    uek, c = uek[keep], c[keep]
    EU = len(uek)
    if EU == 0:
        return None
    eu = (uek // N).astype(INT)
    ev = (uek % N).astype(INT)
    H = 2 * EU
    tail = np.empty(H, dtype=INT)
    head = np.empty(H, dtype=INT)
    tail[0::2], tail[1::2] = eu, ev
    head[0::2], head[1::2] = ev, eu
    d = (nodes[head] - nodes[tail]).astype(np.float64)
    angle = np.arctan2(d[:, 1], d[:, 0])
    order = np.lexsort((angle, tail))
    pos = np.empty(H, dtype=INT)
    pos[order] = np.arange(H)
    node_start = np.searchsorted(tail[order], np.arange(N), side="left")
    node_count = np.bincount(tail, minlength=N)
    twin = np.arange(H) ^ 1
    v_of = tail[twin]
    ns = node_start[v_of]
    nxt = order[ns + (pos[twin] - ns - 1) % node_count[v_of]]
    # Faces : cycles de nxt (boucle Python sur des listes, sans récursion).
    nxt_l = nxt.tolist()
    label_l = [-1] * H
    F = 0
    for h0 in range(H):
        if label_l[h0] >= 0:
            continue
        h = h0
        while label_l[h] < 0:
            label_l[h] = F
            h = nxt_l[h]
        F += 1
    label = np.array(label_l, dtype=INT)
    # Aire signée exacte par face (2×), pour repérer la face extérieure de chaque composante.
    cross = nodes[tail, 0] * nodes[head, 1] - nodes[head, 0] * nodes[tail, 1]
    face_area = np.zeros(F, dtype=INT)
    np.add.at(face_area, label, cross)
    # Contribution orientée de chaque demi-arête (gauche − droite).
    cor = np.empty((H, T), dtype=np.int32)
    cor[0::2] = c
    cor[1::2] = -c
    # Demi-arêtes groupées par face.
    fo = np.argsort(label, kind="stable")
    fstart = np.searchsorted(label[fo], np.arange(F), side="left")
    fcount = np.bincount(label, minlength=F)
    # Enroulement des faces : BFS par niveaux depuis chaque face extérieure (aire < 0).
    w = np.zeros((F, T), dtype=np.int64)
    assigned = np.zeros(F, dtype=bool)
    face_min_node = np.full(F, N, dtype=INT)
    np.minimum.at(face_min_node, label, tail)
    for f0 in np.flatnonzero(face_area < 0).tolist():
        if assigned[f0]:
            continue
        n0 = int(face_min_node[f0])
        w[f0] = _winding_left_of(nodes, eu, ev, c, int(nodes[n0, 0]), int(nodes[n0, 1]))
        assigned[f0] = True
        frontier = np.array([f0], dtype=INT)
        while len(frontier):
            counts = fcount[frontier]
            tot = int(counts.sum())
            rep_f = np.repeat(frontier, counts)
            offs = np.arange(tot) - np.repeat(np.cumsum(counts) - counts, counts)
            hs = fo[fstart[rep_f] + offs]
            g = label[hs ^ 1]
            cand = ~assigned[g]
            g, hs, rep_f = g[cand], hs[cand], rep_f[cand]
            if not len(g):
                break
            ug, first = np.unique(g, return_index=True)
            w[ug] = w[rep_f[first]] - cor[hs[first]]
            assigned[ug] = True
            frontier = ug
    return nodes, tail, head, angle, order, label, w, assigned


def _chain(nodes, tail, head, angle, order, res):
    """Chaîne les demi-arêtes frontières (région à gauche) en rings, en séparant les boucles aux sommets répétés."""
    ro = order[res[order]]
    groups = {}
    tail_l, head_l, angle_l = tail.tolist(), head.tolist(), angle.tolist()
    for h in ro.tolist():
        groups.setdefault(tail_l[h], ([], []))
        ga, gh = groups[tail_l[h]]
        ga.append(angle_l[h])
        gh.append(h)
    nxt = {}
    for h in np.flatnonzero(res).tolist():
        grp = groups.get(head_l[h])
        if grp is None:
            continue
        ga, gh = grp
        k = bisect_left(ga, angle_l[h ^ 1])
        nxt[h] = gh[k - 1]
    visited = set()
    rings = []
    nodes_l = nodes.tolist()
    for h0 in nxt:
        if h0 in visited:
            continue
        seq, where = [], {}
        h = h0
        while h not in visited:
            visited.add(h)
            n = tail_l[h]
            if n in where:
                k = where[n]
                loop = seq[k:]
                for m in loop:
                    where.pop(m, None)
                seq = seq[:k]
                if len(loop) >= 3:
                    rings.append(loop)
            where[n] = len(seq)
            seq.append(n)
            h = nxt.get(h)
            if h is None:
                break
        if len(seq) >= 3:
            rings.append(seq)
    out = []
    for seq in rings:
        ring = np.array([nodes_l[n] for n in seq], dtype=INT)
        ring = _remove_collinear(ring)
        if len(ring) >= 3 and area2(ring) != 0:
            out.append(np.ascontiguousarray(ring))
    return out


def _execute(groups, predicate):
    """Applique ``predicate(w: (F, T) int64) -> bool (F,)`` à l'arrangement des opérandes."""
    T = len(groups)
    P, Q, tag = _edges_from_groups(groups)
    if not len(P):
        return []
    P, Q, tag = _subdivide(P, Q, tag)
    arr = _arrangement(P, Q, tag, T)
    if arr is None:
        return []
    nodes, tail, head, angle, order, label, w, assigned = arr
    inc = np.asarray(predicate(w), dtype=bool) & assigned
    res = inc[label] & ~inc[label[np.arange(len(label)) ^ 1]]
    if not res.any():
        return []
    return _chain(nodes, tail, head, angle, order, res)


# ---------------------------------------------------------------------------------------------------------------
# 3. API publique
# ---------------------------------------------------------------------------------------------------------------

def union(paths):
    """Union (règle positive) d'un Paths quelconque → Paths propre."""
    return _execute([paths], lambda w: w[:, 0] > 0)


def union_many(groups):
    """Union de plusieurs Paths pris chacun en règle positive."""
    groups = [g for g in groups if g]
    if not groups:
        return []
    if len(groups) == 1:
        return union(groups[0])
    if len(groups) > _MAX_GROUPS_MULTI:
        merged = []
        for g in groups:
            merged.extend(union(g))
        return union(merged)
    return _execute(groups, lambda w: (w > 0).any(axis=1))


def difference(subject, clip):
    """subject − clip."""
    return _execute([subject, clip], lambda w: (w[:, 0] > 0) & ~(w[:, 1] > 0))


def intersection(subject, clip):
    """subject ∩ clip."""
    return _execute([subject, clip], lambda w: (w[:, 0] > 0) & (w[:, 1] > 0))


def xor(subject, clip):
    """Différence symétrique."""
    return _execute([subject, clip], lambda w: (w[:, 0] > 0) != (w[:, 1] > 0))


def apply(op, subject, clip):
    """Opération par nom : ``union`` | ``difference`` | ``intersection`` | ``xor``."""
    if op == "union":
        return _execute([subject, clip], lambda w: (w[:, 0] > 0) | (w[:, 1] > 0))
    if op == "difference":
        return difference(subject, clip)
    if op == "intersection":
        return intersection(subject, clip)
    if op == "xor":
        return xor(subject, clip)
    raise ValueError(f"opération inconnue : {op}")
