# -*- coding: utf-8 -*-
"""Champs de distance signée sur grille et contours (portage de Champ.kt et Contours.kt d'Alpine Gift).

Un champ vaut > 0 dans la matière (≈ distance signée en mm). Union = max, intersection = min, différence = min(a, −b),
dilatation de d mm = f + d. Les contours se tirent par marching squares (``police.contours_niveau``), matière à
gauche. Une boucle est un tableau numpy (n, 2) en mm, y vers le haut.
"""
import math

import numpy as np

from .police import aire, contours_niveau, distance_segments, simplifier as _simplifier_dp

INF = 1e10


class Champ:
    def __init__(self, nx, ny, x0, y0, pas, v=None):
        if nx < 2 or ny < 2:
            raise ValueError("Grille trop petite.")
        self.nx, self.ny, self.x0, self.y0, self.pas = int(nx), int(ny), float(x0), float(y0), float(pas)
        self.v = np.zeros((self.ny, self.nx), dtype=np.float64) if v is None else v

    @staticmethod
    def sur(boite, pas, marge):
        """Grille couvrant ``boite`` (xmin, ymin, xmax, ymax) élargie de ``marge`` mm."""
        x0 = math.floor((boite[0] - marge) / pas) * pas
        y0 = math.floor((boite[1] - marge) / pas) * pas
        nx = int(math.ceil((boite[2] + marge - x0) / pas)) + 1
        ny = int(math.ceil((boite[3] + marge - y0) / pas)) + 1
        return Champ(max(nx, 2), max(ny, 2), x0, y0, pas)

    def avec(self, v):
        return Champ(self.nx, self.ny, self.x0, self.y0, self.pas, v)

    def grille(self):
        xs = self.x0 + self.pas * np.arange(self.nx)
        ys = self.y0 + self.pas * np.arange(self.ny)
        return np.meshgrid(xs, ys)

    def union(self, autre):
        return self.avec(np.maximum(self.v, autre.v))

    def inter(self, autre):
        return self.avec(np.minimum(self.v, autre.v))

    def moins(self, autre):
        return self.avec(np.minimum(self.v, -autre.v))

    def decaler(self, d):
        return self.avec(self.v + d)

    def contours(self):
        return contours_niveau(self.v, self.x0, self.y0, self.pas)


def segments(boucles):
    """Segments (S, 4) de toutes les boucles fermées."""
    if not boucles:
        return np.zeros((0, 4))
    parts = [np.hstack([b, np.roll(b, -1, axis=0)]) for b in boucles if len(b) >= 2]
    return np.vstack(parts) if parts else np.zeros((0, 4))


def rasteriser(c, boucles):
    """Remplissage (règle non nulle) des boucles aux nœuds de la grille : masque booléen (ny, nx)."""
    masque = np.zeros((c.ny, c.nx), dtype=bool)
    seg = segments(boucles)
    if seg.shape[0] == 0:
        return masque
    ax, ay, bx, by = seg.T
    lo = np.minimum(ay, by)
    hi = np.maximum(ay, by)
    j0 = np.clip(np.floor((lo - c.y0) / c.pas).astype(np.int64), 0, c.ny - 1)
    j1 = np.clip(np.ceil((hi - c.y0) / c.pas).astype(np.int64), 0, c.ny - 1)
    n = np.maximum(j1 - j0 + 1, 0)
    n[ay == by] = 0
    total = int(n.sum())
    if total == 0:
        return masque
    if total > 60_000_000:
        raise ValueError("Texte trop détaillé pour la grille de calcul.")
    rep = np.repeat(np.arange(seg.shape[0]), n)
    debut = np.repeat(np.cumsum(n) - n, n)
    jj = j0[rep] + (np.arange(total) - debut)
    py = c.y0 + jj * c.pas
    a_y, b_y = ay[rep], by[rep]
    ok = (a_y <= py) != (b_y <= py)
    rep, jj, py, a_y, b_y = rep[ok], jj[ok], py[ok], a_y[ok], b_y[ok]
    x = ax[rep] + (py - a_y) / (b_y - a_y) * (bx[rep] - ax[rep])
    sens = np.where(b_y > a_y, 1, -1)
    ordre = np.lexsort((x, jj))
    jj, x, sens = jj[ordre], x[ordre], sens[ordre]
    # Enroulement cumulé : la somme des sens d'une rangée complète est nulle (boucles fermées).
    w = np.cumsum(sens)
    q = np.nonzero((w[:-1] != 0) & (jj[:-1] == jj[1:]))[0]
    if q.size == 0:
        return masque
    ia = np.maximum(0, np.ceil((x[q] - c.x0) / c.pas).astype(np.int64))
    ib = np.minimum(c.nx - 1, np.floor((x[q + 1] - c.x0) / c.pas).astype(np.int64))
    ok = ia <= ib
    j, ia, ib = jj[q][ok], ia[ok], ib[ok]
    diff = np.zeros((c.ny, c.nx + 1), dtype=np.int32)
    np.add.at(diff, (j, ia), 1)
    np.add.at(diff, (j, ib + 1), -1)
    return np.cumsum(diff, axis=1)[:, :c.nx] > 0


def _edt_lignes(f):
    """Transformée de distance 1D (Felzenszwalb–Huttenlocher) le long de l'axe 1, toutes les lignes à la fois."""
    m, n = f.shape
    lignes = np.arange(m)
    v = np.zeros((m, n), dtype=np.int64)
    z = np.full((m, n + 1), np.inf)
    z[:, 0] = -np.inf
    k = np.zeros(m, dtype=np.int64)
    for q in range(1, n):
        fq = f[:, q] + q * q
        while True:
            vk = v[lignes, k]
            s = (fq - (f[lignes, vk] + vk * vk)) / (2.0 * q - 2.0 * vk)
            retire = s <= z[lignes, k]
            if not retire.any():
                break
            k = k - retire
        k = k + 1
        v[lignes, k] = q
        z[lignes, k] = s
        z[lignes, k + 1] = np.inf
    d = np.empty_like(f)
    k = np.zeros(m, dtype=np.int64)
    for q in range(n):
        while True:
            avance = z[lignes, k + 1] < q
            if not avance.any():
                break
            k = k + avance
        p = v[lignes, k]
        d[:, q] = (q - p) ** 2 + f[lignes, p]
    return d


def edt_carre(masque, cible):
    """Distance² (en nœuds) de chaque nœud au plus proche nœud où ``masque == cible``."""
    f = np.where(masque == cible, 0.0, INF)
    f = _edt_lignes(f)
    return _edt_lignes(f.T.copy()).T


def depuis_masque(c, masque):
    dehors = edt_carre(masque, True)
    dedans = edt_carre(masque, False)
    demi = 0.5 * c.pas
    v = np.where(masque, np.sqrt(dedans) * c.pas - demi, demi - np.sqrt(dehors) * c.pas)
    return c.avec(v)


def affiner(c, masque, boucles, tuile=48):
    """Près du bord (|valeur| < 1,5 pas), distance exacte aux segments des contours : contours lisses ensuite."""
    seg = segments(boucles)
    r = c.v.copy()
    if seg.shape[0] == 0:
        return c.avec(r)
    bande = np.abs(r) < 1.5 * c.pas
    sx0 = np.minimum(seg[:, 0], seg[:, 2])
    sx1 = np.maximum(seg[:, 0], seg[:, 2])
    sy0 = np.minimum(seg[:, 1], seg[:, 3])
    sy1 = np.maximum(seg[:, 1], seg[:, 3])
    m = 3.0 * c.pas
    for tj in range(0, c.ny, tuile):
        for ti in range(0, c.nx, tuile):
            sous = bande[tj:tj + tuile, ti:ti + tuile]
            if not sous.any():
                continue
            jj, ii = np.nonzero(sous)
            jj = jj + tj
            ii = ii + ti
            px = c.x0 + ii * c.pas
            py = c.y0 + jj * c.pas
            choix = ((sx1 >= px.min() - m) & (sx0 <= px.max() + m) & (sy1 >= py.min() - m) & (sy0 <= py.max() + m))
            if not choix.any():
                continue
            d = distance_segments(px, py, seg[choix])
            ok = np.isfinite(d) & (d <= m)
            if not ok.any():
                continue
            val = np.maximum(d[ok], 1e-6)
            jo, io = jj[ok], ii[ok]
            r[jo, io] = np.where(masque[jo, io], val, -val)
    return c.avec(r)


def distance_signee(c, boucles):
    """Remplissage non nul des boucles puis distance signée (mm) : transformée exacte, affinée près du bord."""
    masque = rasteriser(c, boucles)
    return affiner(depuis_masque(c, masque), masque, boucles)


# --- Boucles ---------------------------------------------------------------------------------------------------------
def boite(boucles):
    pts = np.vstack(boucles)
    return [float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max())]


def contient(b, px, py):
    x, y = b[:, 0], b[:, 1]
    xj, yj = np.roll(x, 1), np.roll(y, 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        traverse = ((y > py) != (yj > py)) & (px < (xj - x) * (py - y) / (yj - y) + x)
    return bool(np.count_nonzero(traverse) % 2)


def sans_alignes(b):
    """Retire doublons et sommets alignés (la triangulation n'a ainsi que de vrais sommets)."""
    xs = b[:, 0].tolist()
    ys = b[:, 1].tolist()
    change = True
    while change and len(xs) >= 3:
        change = False
        i = 0
        while i < len(xs) and len(xs) >= 3:
            n = len(xs)
            p = (i - 1) % n
            s = (i + 1) % n
            croix = (xs[i] - xs[p]) * (ys[s] - ys[p]) - (ys[i] - ys[p]) * (xs[s] - xs[p])
            l = math.hypot(xs[s] - xs[p], ys[s] - ys[p])
            doublon = xs[i] == xs[p] and ys[i] == ys[p]
            if doublon or abs(croix) <= 1e-9 * max(l, 1e-9) * max(l, 1.0):
                del xs[i]
                del ys[i]
                change = True
            else:
                i += 1
    return np.column_stack([xs, ys]) if len(xs) >= 3 else None


def simplifier(b, tolerance, pas):
    s = _simplifier_dp(b, tolerance, pas)
    return None if s is None else sans_alignes(s)


def inverse(b):
    return b[::-1].copy()


def regrouper(boucles):
    """Boucles sans croisement → polygones (extérieur direct, trous indirects) d'après l'imbrication."""
    valides = [b for b in boucles if len(b) >= 3 and abs(aire(b)) > 1e-9]
    aires = [abs(aire(b)) for b in valides]
    boites = [(b[:, 0].min(), b[:, 1].min(), b[:, 0].max(), b[:, 1].max()) for b in valides]
    n = len(valides)
    parent = [-1] * n
    profondeur = [0] * n
    for i in range(n):
        px, py = valides[i][0]
        meilleur = -1
        for j in range(n):
            if i == j or aires[j] <= aires[i]:
                continue
            bj = boites[j]
            if px < bj[0] or px > bj[2] or py < bj[1] or py > bj[3]:
                continue
            if contient(valides[j], px, py):
                profondeur[i] += 1
                if meilleur < 0 or aires[j] < aires[meilleur]:
                    meilleur = j
        parent[i] = meilleur
    polys = []
    for e in range(n):
        if profondeur[e] % 2:
            continue
        ext = valides[e] if aire(valides[e]) > 0 else inverse(valides[e])
        trous = [valides[t] if aire(valides[t]) < 0 else inverse(valides[t])
                 for t in range(n) if profondeur[t] % 2 == 1 and parent[t] == e]
        polys.append((ext, trous))
    return polys


def boucles_de(polys):
    return [b for ext, trous in polys for b in [ext] + trous]
