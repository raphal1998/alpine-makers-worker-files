# -*- coding: utf-8 -*-
"""Champs de distance signée sur grille (portage de Champ.kt d'Alpine Gift : Champ, Edt, MarchingSquares).

Un champ : tableau float32 v[j, i] (rangée j : y = y0 + j·pas, colonne i : x = x0 + i·pas), valeur > 0 dans la
matière (≈ distance signée en mm). Union = max, intersection = min, différence = min(a, −b), dilatation de d mm = f + d.
Les opérations en Float de l'application sont faites en float32 dans le même ordre, pour tirer les mêmes contours.
"""
import math

import numpy as np

F32 = np.float32
INF = float(F32(1e20))
LIMITE_NOEUDS = 12_000_000


class Champ:
    __slots__ = ("nx", "ny", "x0", "y0", "pas", "v")

    def __init__(self, nx, ny, x0, y0, pas, v=None):
        if nx < 2 or ny < 2:
            raise ValueError("Grille trop petite.")
        if nx * ny > LIMITE_NOEUDS:
            raise ValueError("Dessin trop grand pour la grille de calcul.")
        self.nx, self.ny, self.x0, self.y0, self.pas = int(nx), int(ny), float(x0), float(y0), float(pas)
        self.v = np.zeros((self.ny, self.nx), dtype=F32) if v is None else v

    @staticmethod
    def sur(b, pas, marge):
        """Grille couvrant la boîte ``b`` (xmin, ymin, xmax, ymax) élargie de ``marge`` mm."""
        x0 = math.floor((b[0] - marge) / pas) * pas
        y0 = math.floor((b[1] - marge) / pas) * pas
        nx = math.ceil((b[2] + marge - x0) / pas) + 1
        ny = math.ceil((b[3] + marge - y0) / pas) + 1
        return Champ(max(nx, 2), max(ny, 2), x0, y0, pas)

    def x(self, i):
        return self.x0 + i * self.pas

    def y(self, j):
        return self.y0 + j * self.pas

    def xs(self):
        return self.x0 + np.arange(self.nx) * self.pas

    def ys(self):
        return self.y0 + np.arange(self.ny) * self.pas

    def avec(self, v):
        return Champ(self.nx, self.ny, self.x0, self.y0, self.pas, v)

    def remplir(self, f):
        """Champ des valeurs f(X, Y) (tableaux de coordonnées en mm, float64), arrondies en float32."""
        X, Y = np.meshgrid(self.xs(), self.ys())
        return self.avec(np.asarray(f(X, Y), dtype=np.float64).astype(F32))

    def union(self, autre):
        return self.avec(np.maximum(self.v, autre.v))

    def inter(self, autre):
        return self.avec(np.minimum(self.v, autre.v))

    def moins(self, autre):
        return self.avec(np.minimum(self.v, -autre.v))

    def decaler(self, d):
        return self.avec(self.v + F32(d))

    def oppose(self):
        return self.avec(-self.v)

    def contours(self):
        return marching_squares(self)


# ------------------------------------------------------------------ transformée de distance (Edt.carre)
def _lignes(cible):
    """Distance (en nœuds, float64) au plus proche nœud cible de la même rangée ; inf sans cible."""
    ny, nx = cible.shape
    idx = np.arange(nx, dtype=np.int64)
    grand = 1 << 40
    avant = np.maximum.accumulate(np.where(cible, idx, -grand), axis=1)
    apres = np.minimum.accumulate(np.where(cible, idx, grand)[:, ::-1], axis=1)[:, ::-1]
    g = np.minimum(idx - avant, apres - idx).astype(np.float64)
    g[g > nx] = np.inf
    return g


def _colonnes(g2):
    """min sur j' de (j − j')² + g2[j', i] pour chaque colonne : enveloppe inférieure de paraboles
    (Felzenszwalb–Huttenlocher) menée sur toutes les colonnes à la fois ; inf si la colonne est vide."""
    n, m = g2.shape
    cols = np.arange(m)
    fini = np.isfinite(g2)
    v = np.zeros((n, m), dtype=np.int64)
    z = np.full((n + 1, m), np.inf)
    k = np.full(m, -1, dtype=np.int64)
    for q in range(n):
        act = np.flatnonzero(fini[q])
        if act.size == 0:
            continue
        kk = k[act]
        fq = g2[q, act] + float(q * q)
        s = np.full(act.size, -np.inf)
        en_cours = np.flatnonzero(kk >= 0)
        while en_cours.size:
            c = act[en_cours]
            kc = kk[en_cours]
            p = v[kc, c]
            si = (fq[en_cours] - (g2[p, c] + (p * p).astype(np.float64))) / (2.0 * q - 2.0 * p)
            s[en_cours] = si
            retire = si <= z[kc, c]
            if not retire.any():
                break
            en_cours = en_cours[retire]
            kk[en_cours] -= 1
        kk = kk + 1
        v[kk, act] = q
        z[kk, act] = s
        z[kk + 1, act] = np.inf
        k[act] = kk
    plein = k >= 0
    d = np.full((n, m), np.inf)
    if not plein.any():
        return d
    c = cols[plein]
    kq = np.zeros(c.size, dtype=np.int64)
    for q in range(n):
        while True:
            avance = z[kq + 1, c] < q
            if not avance.any():
                break
            kq += avance
        p = v[kq, c]
        d[q, c] = (q - p) ** 2 + g2[p, c]
    return d


def edt_carre(masque, cible):
    """Distance² exacte (en nœuds) de chaque nœud au plus proche nœud où ``masque == cible`` ; 1e20 sans cible."""
    g = _lignes(masque == cible)
    d = _colonnes(g * g)
    d[~np.isfinite(d)] = INF
    return d


def depuis_masque(c, masque):
    """Distance signée (mm) d'un masque : + dedans, − dehors, zéro à mi-chemin entre deux nœuds."""
    dehors = edt_carre(masque, True)
    dedans = edt_carre(masque, False)
    demi = 0.5 * c.pas
    v = np.where(masque, np.sqrt(dedans) * c.pas - demi, demi - np.sqrt(dehors) * c.pas)
    return c.avec(v.astype(F32))


# ------------------------------------------------------------------ remplissage et distance exacte aux contours
def _aretes(boucles):
    ax, ay, bx, by = [], [], [], []
    for b in boucles:
        ax.append(np.roll(b[:, 0], 1))
        ay.append(np.roll(b[:, 1], 1))
        bx.append(b[:, 0])
        by.append(b[:, 1])
    return np.concatenate(ax), np.concatenate(ay), np.concatenate(bx), np.concatenate(by)


def rasteriser(c, boucles):
    """Nœuds dans les boucles (règle non nulle), par balayage de rangées : masque booléen (ny, nx)."""
    masque = np.zeros((c.ny, c.nx), dtype=bool)
    boucles = [b for b in boucles if len(b)]
    if not boucles:
        return masque
    ax, ay, bx, by = _aretes(boucles)
    ylo, yhi = np.minimum(ay, by), np.maximum(ay, by)
    j0 = np.maximum(np.ceil((ylo - c.y0) / c.pas).astype(np.int64) - 1, 0)
    j1 = np.minimum(np.ceil((yhi - c.y0) / c.pas).astype(np.int64) + 1, c.ny - 1)
    nb = np.maximum(j1 - j0 + 1, 0)
    total = int(nb.sum())
    if total == 0:
        return masque
    k = np.repeat(np.arange(ax.size), nb)
    debut = np.cumsum(nb) - nb
    j = np.repeat(j0 - debut, nb) + np.arange(total)
    py = c.y0 + j * c.pas
    ok = (ay[k] <= py) != (by[k] <= py)
    k, j, py = k[ok], j[ok], py[ok]
    if k.size == 0:
        return masque
    t = (py - ay[k]) / (by[k] - ay[k])
    x = ax[k] + t * (bx[k] - ax[k])
    sens = np.where(by[k] > ay[k], 1, -1)
    ordre = np.lexsort((x, j))
    j, x, sens = j[ordre], x[ordre], sens[ordre]
    enroulement = np.cumsum(sens)
    rempli = (j[:-1] == j[1:]) & (enroulement[:-1] != 0)
    ja, xa, xb = j[:-1][rempli], x[:-1][rempli], x[1:][rempli]
    ia = np.maximum(0, np.ceil((xa - c.x0) / c.pas).astype(np.int64))
    ib = np.minimum(c.nx - 1, np.floor((xb - c.x0) / c.pas).astype(np.int64))
    ok = ia <= ib
    diff = np.zeros((c.ny, c.nx + 1), dtype=np.int32)
    np.add.at(diff, (ja[ok], ia[ok]), 1)
    np.add.at(diff, (ja[ok], ib[ok] + 1), -1)
    return np.cumsum(diff, axis=1)[:, :c.nx] > 0


def _dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    with np.errstate(invalid="ignore", divide="ignore"):
        t = np.where(l2 == 0.0, 0.0, np.clip(((px - ax) * dx + (py - ay) * dy) / l2, 0.0, 1.0))
    return np.hypot(px - (ax + t * dx), py - (ay + t * dy))


def distances_segments(qx, qy, boucles, c, taille):
    """Distance exacte des points (qx, qy) aux segments des boucles rangés dans des seaux de ``taille`` mm, parmi les
    seaux voisins (± 1), comme Champ.affiner ; inf si aucun segment voisin."""
    ax, ay, bx_, by_ = _aretes(boucles)
    nbx = int((c.nx - 1) * c.pas / taille) + 1
    nby = int((c.ny - 1) * c.pas / taille) + 1
    i0 = np.clip(((np.minimum(ax, bx_) - c.x0) / taille).astype(np.int64), 0, nbx - 1)
    i1 = np.clip(((np.maximum(ax, bx_) - c.x0) / taille).astype(np.int64), 0, nbx - 1)
    j0 = np.clip(((np.minimum(ay, by_) - c.y0) / taille).astype(np.int64), 0, nby - 1)
    j1 = np.clip(((np.maximum(ay, by_) - c.y0) / taille).astype(np.int64), 0, nby - 1)
    wi, wj = i1 - i0 + 1, j1 - j0 + 1
    nb = wi * wj
    seg = np.repeat(np.arange(ax.size), nb)
    loc = np.arange(int(nb.sum())) - np.repeat(np.cumsum(nb) - nb, nb)
    si = i0[seg] + loc % wi[seg]
    sj = j0[seg] + loc // wi[seg]
    cle = sj * nbx + si
    ordre = np.argsort(cle, kind="stable")
    seg, cle = seg[ordre], cle[ordre]
    debut = np.searchsorted(cle, np.arange(nbx * nby), side="left")
    fin = np.searchsorted(cle, np.arange(nbx * nby), side="right")
    p = ((qx - c.x0) / taille).astype(np.int64)
    q = ((qy - c.y0) / taille).astype(np.int64)
    best = np.full(qx.size, np.inf)
    for dq in (-1, 0, 1):
        for dp in (-1, 0, 1):
            pp, qq = p + dp, q + dq
            noeuds = np.flatnonzero((pp >= 0) & (qq >= 0) & (pp < nbx) & (qq < nby))
            if noeuds.size == 0:
                continue
            cc = qq[noeuds] * nbx + pp[noeuds]
            n = fin[cc] - debut[cc]
            garder = n > 0
            noeuds, cc, n = noeuds[garder], cc[garder], n[garder]
            if noeuds.size == 0:
                continue
            cum = np.cumsum(n)
            depart = 0
            while depart < noeuds.size:
                limite = (cum[depart - 1] if depart else 0) + 2_000_000
                arret = max(depart + 1, int(np.searchsorted(cum, limite, side="right")))
                nn = n[depart:arret]
                paires = int(nn.sum())
                nd = np.repeat(noeuds[depart:arret], nn)
                off = np.arange(paires) - np.repeat(np.cumsum(nn) - nn, nn)
                s = seg[np.repeat(debut[cc[depart:arret]], nn) + off]
                np.minimum.at(best, nd, _dist(qx[nd], qy[nd], ax[s], ay[s], bx_[s], by_[s]))
                depart = arret
    return best


def affiner(c, masque, boucles):
    """Près du bord (|valeur| < 1,5 pas), distance exacte aux segments des contours (contours lisses ensuite)."""
    bande = F32(1.5) * F32(c.pas)
    jj, ii = np.nonzero(np.abs(c.v) < bande)
    r = c.v.copy()
    if jj.size == 0 or not boucles:
        return c.avec(r)
    d = distances_segments(c.x0 + ii * c.pas, c.y0 + jj * c.pas, boucles, c, 4 * c.pas)
    ok = np.isfinite(d)
    val = np.maximum(d[ok], 1e-6).astype(F32)
    r[jj[ok], ii[ok]] = np.where(masque[jj[ok], ii[ok]], val, -val)
    return c.avec(r)


def distance_signee(c, boucles):
    """Remplissage (règle non nulle) des boucles aux nœuds de la grille ``c``, puis distance signée exacte (mm)."""
    m = rasteriser(c, boucles)
    return affiner(depuis_masque(c, m), m, boucles)


def flou(c):
    """Moyenne 3 × 3 (bords prolongés), sommée en float32 dans l'ordre de Dessins.flou."""
    p = np.pad(c.v, 1, mode="edge")
    s = np.zeros_like(c.v)
    for dj in (-1, 0, 1):
        for di in (-1, 0, 1):
            s = s + p[1 + dj:1 + dj + c.ny, 1 + di:1 + di + c.nx]
    return c.avec(s / F32(9))


def valeur(c, x, y):
    """Valeur bilinéaire (float32) aux points (x, y) mm (tableaux), −1 hors de la grille (Dessins.valeur)."""
    fx = (x - c.x0) / c.pas
    fy = (y - c.y0) / c.pas
    i = np.trunc(fx).astype(np.int64)
    j = np.trunc(fy).astype(np.int64)
    dehors = (fx < 0) | (fy < 0) | (i >= c.nx - 1) | (j >= c.ny - 1)
    i = np.clip(i, 0, c.nx - 2)
    j = np.clip(j, 0, c.ny - 2)
    tx = (fx - i).astype(F32)
    ty = (fy - j).astype(F32)
    v = c.v
    a = v[j, i] + (v[j, i + 1] - v[j, i]) * tx
    b = v[j + 1, i] + (v[j + 1, i + 1] - v[j + 1, i]) * tx
    r = a + (b - a) * ty
    return np.where(dehors, F32(-1), r)


# ------------------------------------------------------------------ marching squares orienté (MarchingSquares.contours)
def _tables():
    tables = {}
    for code in range(16):
        pos = [bool(code >> k & 1) for k in range(4)]
        coupe = [pos[k] != pos[(k + 1) % 4] for k in range(4)]
        debut = [pos[k] and not pos[(k + 1) % 4] for k in range(4)]
        for centre in (True, False):
            segs = []
            if 0 < sum(pos) < 4:
                if sum(debut) == 1:
                    s = debut.index(True)
                    e = next(k for k in range(4) if coupe[k] and not debut[k])
                    segs.append((s, e))
                else:
                    for s in range(4):
                        if debut[s]:
                            segs.append((s, (s + 1) % 4 if centre else (s + 3) % 4))
            tables[(code, centre)] = segs
    return tables


_TABLES = _tables()


def marching_squares(c):
    """Contours au niveau 0 (matière à gauche), grille bordée de vide, cas ambigus tranchés par la moyenne de la
    cellule ; boucles parcourues dans l'ordre des identifiants d'arêtes, point de chaque arête calculé dans le sens où
    l'application l'écrit en dernier (arête horizontale de gauche à droite, verticale de haut en bas)."""
    v = c.v
    ny, nx = v.shape
    W = nx + 2
    V = np.full((ny + 2, nx + 2), F32(-1.0), dtype=F32)
    V[1:-1, 1:-1] = np.where(v == 0, F32(-1e-7), v)
    pos = V > 0
    code = (pos[:-1, :-1].astype(np.int8) | (pos[:-1, 1:].astype(np.int8) << 1)
            | (pos[1:, 1:].astype(np.int8) << 2) | (pos[1:, :-1].astype(np.int8) << 3))
    cj, ci = np.nonzero((code != 0) & (code != 15))
    if cj.size == 0:
        return []
    cs = code[cj, ci]
    centre = ((V[cj, ci] + V[cj, ci + 1] + V[cj + 1, ci + 1] + V[cj + 1, ci]) / F32(4)) > 0
    base = cj.astype(np.int64) * W + ci
    ids = np.stack([2 * base, 2 * (base + 1) + 1, 2 * (base + W), 2 * base + 1], axis=1)
    src, dst = [], []
    for (cc, ctr), segs in _TABLES.items():
        if not segs:
            continue
        sel = np.flatnonzero((cs == cc) & (centre == ctr))
        if sel.size == 0:
            continue
        for s, e in segs:
            src.append(sel * 4 + s)
            dst.append(sel * 4 + e)
    src = np.concatenate(src)
    dst = np.concatenate(dst)
    src_id = ids.ravel()[src]
    dst_id = ids.ravel()[dst]
    ordre = np.argsort(src_id, kind="stable")
    src_id, dst_id = src_id[ordre], dst_id[ordre]
    suivant = np.searchsorted(src_id, dst_id)
    suivant = np.where((suivant < src_id.size) & (src_id[np.minimum(suivant, src_id.size - 1)] == dst_id), suivant, -1)
    vert = (src_id & 1).astype(bool)
    b = src_id >> 1
    pj, pi = b // W, b % W
    xa = c.x0 + (pi - 1) * c.pas
    # Horizontale : de (pi, pj) à (pi + 1, pj). Verticale : de (pi, pj + 1) à (pi, pj).
    a_val = np.where(vert, V[np.minimum(pj + 1, ny + 1), pi], V[pj, pi])
    b_val = np.where(vert, V[pj, pi], V[pj, np.minimum(pi + 1, nx + 1)])
    t = (a_val / (a_val - b_val)).astype(np.float64)
    ya_h = c.y0 + (pj - 1) * c.pas
    xb_h = c.x0 + pi * c.pas
    ya_v = c.y0 + pj * c.pas
    yb_v = c.y0 + (pj - 1) * c.pas
    px = np.where(vert, xa + t * (xa - xa), xa + t * (xb_h - xa))
    py = np.where(vert, ya_v + t * (yb_v - ya_v), ya_h + t * (ya_h - ya_h))
    nxt = suivant.tolist()
    vu = bytearray(len(nxt))
    boucles = []
    for depart in range(len(nxt)):
        if vu[depart]:
            continue
        chemin = []
        i = depart
        while i >= 0 and not vu[i]:
            vu[i] = 1
            chemin.append(i)
            i = nxt[i]
        if len(chemin) >= 3:
            sel = np.asarray(chemin)
            boucles.append(np.column_stack([px[sel], py[sel]]))
    return boucles
