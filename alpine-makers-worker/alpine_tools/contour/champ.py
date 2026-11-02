"""Champs scalaires sur grille régulière (port de Champ.kt) : remplissage, distance signée, marching squares.

Un champ : tableau float32 v[j, i] (j = rangée, y = y0 + j·pas ; i = colonne, x = x0 + i·pas) ; valeur > 0 = matière.
"""
import math

import numpy as np

from .geometrie import Boucle

F32 = np.float32


class Grille:
    __slots__ = ("nx", "ny", "x0", "y0", "pas")

    def __init__(self, nx, ny, x0, y0, pas):
        if nx < 2 or ny < 2:
            raise ValueError("Grille trop petite.")
        self.nx, self.ny, self.x0, self.y0, self.pas = int(nx), int(ny), float(x0), float(y0), float(pas)

    @staticmethod
    def sur(b, pas, marge):
        x0 = math.floor((b[0] - marge) / pas) * pas
        y0 = math.floor((b[1] - marge) / pas) * pas
        nx = math.ceil((b[2] + marge - x0) / pas) + 1
        ny = math.ceil((b[3] + marge - y0) / pas) + 1
        return Grille(max(nx, 2), max(ny, 2), x0, y0, pas)

    def xs(self):
        return self.x0 + np.arange(self.nx) * self.pas

    def ys(self):
        return self.y0 + np.arange(self.ny) * self.pas


LIMITE_NOEUDS = 40_000_000


def verifier_taille(g):
    if g.nx * g.ny > LIMITE_NOEUDS:
        raise ValueError("Contour trop grand pour ce calcul (grille de plus de 40 millions de points).")


def _aretes(boucles):
    ax, ay, bx, by = [], [], [], []
    for b in boucles:
        ax.append(np.roll(b.x, 1))
        ay.append(np.roll(b.y, 1))
        bx.append(b.x)
        by.append(b.y)
    return np.concatenate(ax), np.concatenate(ay), np.concatenate(bx), np.concatenate(by)


def rasteriser(g, boucles):
    """Nœuds de la grille dans les boucles (règle non nulle), par balayage de lignes."""
    masque = np.zeros((g.ny, g.nx), dtype=bool)
    if not boucles:
        return masque
    ax, ay, bx, by = _aretes(boucles)
    ylo, yhi = np.minimum(ay, by), np.maximum(ay, by)
    j0 = np.maximum(np.ceil((ylo - g.y0) / g.pas).astype(np.int64) - 1, 0)
    j1 = np.minimum(np.ceil((yhi - g.y0) / g.pas).astype(np.int64) + 1, g.ny - 1)
    nb = np.maximum(j1 - j0 + 1, 0)
    total = int(nb.sum())
    if total == 0:
        return masque
    k = np.repeat(np.arange(ax.size), nb)
    debut = np.cumsum(nb) - nb
    j = np.repeat(j0 - debut, nb) + np.arange(total)
    py = g.y0 + j * g.pas
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
    meme = j[:-1] == j[1:]
    rempli = meme & (enroulement[:-1] != 0)
    ja, xa, xb = j[:-1][rempli], x[:-1][rempli], x[1:][rempli]
    ia = np.maximum(0, np.ceil((xa - g.x0) / g.pas).astype(np.int64))
    ib = np.minimum(g.nx - 1, np.floor((xb - g.x0) / g.pas).astype(np.int64))
    ok = ia <= ib
    diff = np.zeros((g.ny, g.nx + 1), dtype=np.int32)
    np.add.at(diff, (ja[ok], ia[ok]), 1)
    np.add.at(diff, (ja[ok], ib[ok] + 1), -1)
    return np.cumsum(diff, axis=1)[:, :g.nx] > 0


def edt_carre(masque, cible, rayon):
    """Distance² (en nœuds) au plus proche nœud où masque == cible ; exacte jusqu'à ``rayon`` nœuds,
    au-delà une valeur > rayon² (au plus (rayon + 1)²)."""
    f = masque == cible
    ny, nx = f.shape
    plafond = rayon + 1
    idx = np.arange(nx, dtype=np.int64)
    grand = 1 << 40
    avant = np.maximum.accumulate(np.where(f, idx, -grand), axis=1)
    apres = np.minimum.accumulate(np.where(f, idx, grand)[:, ::-1], axis=1)[:, ::-1]
    g = np.minimum(np.minimum(idx - avant, apres - idx), plafond).astype(F32)
    g2 = g * g
    d2 = g2.copy()
    for k in range(1, min(rayon, ny - 1) + 1):
        k2 = F32(k * k)
        np.minimum(d2[k:], g2[:-k] + k2, out=d2[k:])
        np.minimum(d2[:-k], g2[k:] + k2, out=d2[:-k])
    np.minimum(d2, F32(plafond * plafond), out=d2)
    return d2


def depuis_masque(g, masque, rayon):
    """Distance signée (mm) : + dedans, − dehors, zéro à mi-chemin entre deux nœuds."""
    dehors = edt_carre(masque, True, rayon)
    dedans = edt_carre(masque, False, rayon)
    demi = 0.5 * g.pas
    return np.where(masque, np.sqrt(dedans.astype(np.float64)) * g.pas - demi,
                    demi - np.sqrt(dehors.astype(np.float64)) * g.pas).astype(F32)


def distances_segments(qx, qy, boucles, g, taille, portee):
    """Distance exacte des points (qx, qy) aux segments des boucles, parmi les seaux de ``taille`` mm voisins
    (± ``portee`` seaux), comme les seaux de l'application. inf si aucun segment voisin."""
    ax, ay, bx_, by_ = _aretes(boucles)
    nbx = int((g.nx - 1) * g.pas / taille) + 1
    nby = int((g.ny - 1) * g.pas / taille) + 1
    i0 = np.clip(((np.minimum(ax, bx_) - g.x0) / taille).astype(np.int64), 0, nbx - 1)
    i1 = np.clip(((np.maximum(ax, bx_) - g.x0) / taille).astype(np.int64), 0, nbx - 1)
    j0 = np.clip(((np.minimum(ay, by_) - g.y0) / taille).astype(np.int64), 0, nby - 1)
    j1 = np.clip(((np.maximum(ay, by_) - g.y0) / taille).astype(np.int64), 0, nby - 1)
    # Segment inscrit dans chaque seau de sa boîte.
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
    p = ((qx - g.x0) / taille).astype(np.int64)
    q = ((qy - g.y0) / taille).astype(np.int64)
    best = np.full(qx.size, np.inf)
    for dq in range(-portee, portee + 1):
        for dp in range(-portee, portee + 1):
            pp, qq = p + dp, q + dq
            ok = (pp >= 0) & (qq >= 0) & (pp < nbx) & (qq < nby)
            noeuds = np.flatnonzero(ok)
            if noeuds.size == 0:
                continue
            c = qq[noeuds] * nbx + pp[noeuds]
            n = fin[c] - debut[c]
            garder = n > 0
            noeuds, c, n = noeuds[garder], c[garder], n[garder]
            if noeuds.size == 0:
                continue
            # Par paquets pour borner la mémoire.
            cum = np.cumsum(n)
            depart = 0
            while depart < noeuds.size:
                limite = (cum[depart - 1] if depart else 0) + 2_000_000
                arret = max(depart + 1, int(np.searchsorted(cum, limite, side="right")))
                nn = n[depart:arret]
                paires = int(nn.sum())
                nd = np.repeat(noeuds[depart:arret], nn)
                off = np.arange(paires) - np.repeat(np.cumsum(nn) - nn, nn)
                s = seg[np.repeat(debut[c[depart:arret]], nn) + off]
                d = _dist(qx[nd], qy[nd], ax[s], ay[s], bx_[s], by_[s])
                np.minimum.at(best, nd, d)
                depart = arret
    return best


def _dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    with np.errstate(invalid="ignore", divide="ignore"):
        t = np.where(l2 == 0.0, 0.0, np.clip(((px - ax) * dx + (py - ay) * dy) / l2, 0.0, 1.0))
    return np.hypot(px - (ax + t * dx), py - (ay + t * dy))


def affiner(g, v, masque, boucles):
    """Près du bord (|v| < 1,5 pas), distance exacte aux segments des contours."""
    bande = F32(1.5 * g.pas)
    jj, ii = np.nonzero(np.abs(v) < bande)
    if jj.size == 0:
        return v
    qx, qy = g.x0 + ii * g.pas, g.y0 + jj * g.pas
    d = distances_segments(qx, qy, boucles, g, 4 * g.pas, 1)
    ok = np.isfinite(d)
    r = v.copy()
    val = np.maximum(d[ok], 1e-6).astype(F32)
    r[jj[ok], ii[ok]] = np.where(masque[jj[ok], ii[ok]], val, -val)
    return r


def distance_signee(g, boucles):
    verifier_taille(g)
    m = rasteriser(g, boucles)
    return affiner(g, depuis_masque(g, m, 4), m, boucles)


# ------------------------------------------------------------------ marching squares orienté

def _tables():
    """Segments (arête de départ, arête d'arrivée) par configuration, selon le signe du centre (> 0 ou non)."""
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


def contours(v, g, aire_min=0.0):
    """Contours au niveau 0 de v (matière à gauche), grille bordée de vide ; boucles d'aire |a| > aire_min."""
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
    for (c, ctr), segs in _TABLES.items():
        if not segs:
            continue
        sel = np.flatnonzero((cs == c) & (centre == ctr))
        if sel.size == 0:
            continue
        for s, e in segs:
            src.append(sel * 4 + s)
            dst.append(sel * 4 + e)
    src = np.concatenate(src)
    dst = np.concatenate(dst)
    src_id = ids.ravel()[src]
    dst_id = ids.ravel()[dst]
    # Ordre de parcours de l'application : identifiants d'arêtes croissants.
    ordre = np.argsort(src_id, kind="stable")
    src_id, dst_id = src_id[ordre], dst_id[ordre]
    suivant = np.searchsorted(src_id, dst_id)
    suivant = np.where((suivant < src_id.size) & (src_id[np.minimum(suivant, src_id.size - 1)] == dst_id), suivant, -1)
    # Points des arêtes : horizontale (pi, pj)→(pi+1, pj) ou verticale (pi, pj)→(pi, pj+1) en indices bordés.
    vert = (src_id & 1).astype(bool)
    b = src_id >> 1
    pj, pi = b // W, b % W
    a_val = V[pj, pi]
    b_val = np.where(vert, V[np.minimum(pj + 1, ny + 1), pi], V[pj, np.minimum(pi + 1, nx + 1)])
    t = (a_val / (a_val - b_val)).astype(np.float64)
    xa = g.x0 + (pi - 1) * g.pas
    ya = g.y0 + (pj - 1) * g.pas
    xb = np.where(vert, xa, g.x0 + pi * g.pas)
    yb = np.where(vert, g.y0 + pj * g.pas, ya)
    px = xa + t * (xb - xa)
    py = ya + t * (yb - ya)
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
            sel = np.array(chemin)
            bcl = Boucle(px[sel], py[sel])
            if abs(bcl.aire) > aire_min:
                boucles.append(bcl)
    return boucles
