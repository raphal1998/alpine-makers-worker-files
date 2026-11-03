# -*- coding: utf-8 -*-
"""Boucles, champs de distance signée et contours, portés de Contours.kt et Champ.kt (Alpine Gift) pour le puzzle.

Le champ garde les flottants 32 bits de l'application (FloatArray) et les mêmes opérations : remplissage non nul aux
nœuds, transformée de distance euclidienne exacte, distance exacte aux segments près du bord, marching squares
orienté (matière à gauche, même ordre de parcours et mêmes points d'arête), simplification (distance radiale puis
Douglas–Peucker) et regroupement en polygones à trous. Les pièces imprimées ont ainsi les mêmes sommets que dans Alpine
Gift. Une boucle est un tableau numpy (n, 2) de flottants 64 bits en mm, y vers le haut.
"""
import math

import numpy as np

from . import kotlin as K

F32 = np.float32
_MOINS_ZERO = F32(-1e-7)


# --- Boucles (Boucle de Contours.kt) ---------------------------------------------------------------------------------
def aire(b):
    """Aire signée (positive en sens direct), somme dans l'ordre de l'application."""
    x, y = b[:, 0], b[:, 1]
    termes = x * np.roll(y, -1) - np.roll(x, -1) * y
    return float(np.cumsum(termes)[-1]) / 2 if len(termes) else 0.0


def boite_une(b):
    return (float(b[:, 0].min()), float(b[:, 1].min()), float(b[:, 0].max()), float(b[:, 1].max()))


def boite(boucles):
    """Contours.boite : [xmin, ymin, xmax, ymax] de toutes les boucles."""
    r = [math.inf, math.inf, -math.inf, -math.inf]
    for b in boucles:
        c = boite_une(b)
        r = [min(r[0], c[0]), min(r[1], c[1]), max(r[2], c[2]), max(r[3], c[3])]
    return r


def contient(b, px, py):
    """Boucle.contient (règle pair-impair) pour un point."""
    return bool(contient_vec(b, np.array([px]), np.array([py]))[0])


def contient_vec(b, px, py):
    """Boucle.contient pour des tableaux de points (mêmes opérations que l'application)."""
    x, y = b[:, 0], b[:, 1]
    xj, yj = np.roll(x, 1), np.roll(y, 1)
    px = np.asarray(px, dtype=np.float64).ravel()
    py = np.asarray(py, dtype=np.float64).ravel()
    r = np.zeros(px.shape, dtype=bool)
    bloc = max(1, 4_000_000 // max(1, len(x)))
    with np.errstate(divide="ignore", invalid="ignore"):
        for d in range(0, len(px), bloc):
            qx, qy = px[d:d + bloc, None], py[d:d + bloc, None]
            traverse = ((y > qy) != (yj > qy)) & (qx < (xj - x) * (qy - y) / (yj - y) + x)
            r[d:d + bloc] = (np.count_nonzero(traverse, axis=1) % 2) == 1
    return r


def inverse(b):
    return b[::-1].copy()


def regrouper(boucles):
    """Contours.regrouper : boucles sans croisement → polygones (extérieur direct, trous indirects) selon l'imbrication."""
    valides = [b for b in boucles if len(b) >= 3 and abs(aire(b)) > 1e-9]
    aires = [abs(aire(b)) for b in valides]
    boites = [boite_une(b) for b in valides]
    n = len(valides)
    parent = [-1] * n
    profondeur = [0] * n
    for i in range(n):
        px, py = float(valides[i][0, 0]), float(valides[i][0, 1])
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
        ext = inverse(valides[e]) if aire(valides[e]) < 0 else valides[e]
        trous = [inverse(valides[t]) if aire(valides[t]) > 0 else valides[t]
                 for t in range(n) if profondeur[t] % 2 == 1 and parent[t] == e]
        polys.append((ext, trous))
    return polys


def boucles_de(polys):
    return [b for ext, trous in polys for b in [ext] + list(trous)]


def _hypot_seuil(dx, dy, seuil):
    """hypot de l'application quand la valeur est comparée à ``seuil`` (math.hypot si la décision est sûre)."""
    h = math.hypot(dx, dy)
    if abs(h - seuil) <= 1e-14 * seuil:
        h = K.hypot(dx, dy)
    return h


def sans_alignes(xs, ys):
    """Contours.sansAlignes : retire doublons et sommets alignés. Rend un tableau (n, 2) ou None."""
    xs, ys = list(xs), list(ys)
    change = True
    while change and len(xs) >= 3:
        change = False
        i = 0
        while i < len(xs) and len(xs) >= 3:
            n = len(xs)
            p = (i - 1 + n) % n
            s = (i + 1) % n
            croix = (xs[i] - xs[p]) * (ys[s] - ys[p]) - (ys[i] - ys[p]) * (xs[s] - xs[p])
            l = K.hypot(xs[s] - xs[p], ys[s] - ys[p])
            doublon = xs[i] == xs[p] and ys[i] == ys[p]
            if doublon or abs(croix) <= 1e-9 * max(l, 1e-9) * max(l, 1.0):
                del xs[i]
                del ys[i]
                change = True
            else:
                i += 1
    return np.column_stack([xs, ys]) if len(xs) >= 3 else None


def simplifier(b, tolerance, pas):
    """Contours.simplifier : points à moins de ``pas`` du dernier gardé retirés, puis Douglas–Peucker fermé."""
    n0 = len(b)
    if n0 < 4:
        return b
    bx, by = b[:, 0].tolist(), b[:, 1].tolist()
    gx, gy = [bx[0]], [by[0]]
    lx, ly = bx[0], by[0]
    for i in range(1, n0):
        if _hypot_seuil(bx[i] - lx, by[i] - ly, pas) >= pas:
            lx, ly = bx[i], by[i]
            gx.append(lx)
            gy.append(ly)
    if len(gx) > 3 and _hypot_seuil(gx[-1] - gx[0], gy[-1] - gy[0], pas) < pas:
        gx.pop()
        gy.pop()
    if len(gx) < 3:
        return None
    n = len(gx)
    X = np.asarray(gx, dtype=np.float64)
    Y = np.asarray(gy, dtype=np.float64)
    loin = 1 + int(np.argmax(K.hypot_vec(X[1:] - X[0], Y[1:] - Y[0])))
    garde = np.zeros(n, dtype=bool)
    garde[0] = garde[loin] = True
    pile = [(0, loin), (loin, n)]
    while pile:
        a, z = pile.pop()
        if z - a < 2:
            continue
        d = K.distance_segment_vec(X[a + 1:z], Y[a + 1:z], X[a], Y[a], X[z % n], Y[z % n])
        k = int(np.argmax(d))
        if d[k] > tolerance:
            i = a + 1 + k
            garde[i] = True
            pile.append((a, i))
            pile.append((i, z))
    if np.count_nonzero(garde) < 3:
        return None
    return sans_alignes(X[garde].tolist(), Y[garde].tolist())


# --- Champ (Champ.kt) ------------------------------------------------------------------------------------------------
class Champ:
    """Champ scalaire float32 sur une grille régulière (valeur > 0 : dans la matière ; ≈ distance signée en mm)."""

    def __init__(self, nx, ny, x0, y0, pas, v=None):
        if nx < 2 or ny < 2:
            raise ValueError("Grille trop petite.")
        self.nx, self.ny, self.x0, self.y0, self.pas = int(nx), int(ny), float(x0), float(y0), float(pas)
        self.v = np.zeros((self.ny, self.nx), dtype=F32) if v is None else v

    @staticmethod
    def sur(b, pas, marge):
        x0 = math.floor((b[0] - marge) / pas) * pas
        y0 = math.floor((b[1] - marge) / pas) * pas
        nx = int(math.ceil((b[2] + marge - x0) / pas)) + 1
        ny = int(math.ceil((b[3] + marge - y0) / pas)) + 1
        return Champ(max(nx, 2), max(ny, 2), x0, y0, pas)

    def avec(self, v):
        return Champ(self.nx, self.ny, self.x0, self.y0, self.pas, v)

    def xs(self):
        return self.x0 + np.arange(self.nx, dtype=np.float64) * self.pas

    def ys(self):
        return self.y0 + np.arange(self.ny, dtype=np.float64) * self.pas

    def inter(self, autre):
        return self.avec(np.minimum(self.v, autre.v))

    def decaler(self, d):
        return self.avec((self.v + F32(d)).astype(F32))

    def contours(self):
        return marching_squares(self)


def _segments(boucles):
    """Segments (k → i) de chaque boucle dans l'ordre de l'application (k = point précédent, i de 0 à n − 1)."""
    parts = [np.column_stack([np.roll(b[:, 0], 1), np.roll(b[:, 1], 1), b[:, 0], b[:, 1]]) for b in boucles if len(b) >= 1]
    return np.vstack(parts) if parts else np.zeros((0, 4))


def rasteriser(c, boucles):
    """Champ.rasteriser : remplissage (règle non nulle) des boucles aux nœuds, masque booléen (ny, nx)."""
    masque = np.zeros((c.ny, c.nx), dtype=bool)
    seg = _segments(boucles)
    if seg.shape[0] == 0:
        return masque
    ax, ay, bx, by = seg.T
    lo, hi = np.minimum(ay, by), np.maximum(ay, by)
    j0 = np.clip(np.floor((lo - c.y0) / c.pas).astype(np.int64) - 1, 0, c.ny - 1)
    j1 = np.clip(np.ceil((hi - c.y0) / c.pas).astype(np.int64) + 1, 0, c.ny - 1)
    n = np.where(ay == by, 0, np.maximum(j1 - j0 + 1, 0))
    total = int(n.sum())
    if total == 0:
        return masque
    if total > 80_000_000:
        raise ValueError("Contour trop détaillé pour la grille de calcul.")
    rep = np.repeat(np.arange(seg.shape[0]), n)
    debut = np.repeat(np.cumsum(n) - n, n)
    jj = j0[rep] + (np.arange(total) - debut)
    py = c.y0 + jj * c.pas
    a_y, b_y = ay[rep], by[rep]
    ok = (a_y <= py) != (b_y <= py)
    rep, jj, py, a_y, b_y = rep[ok], jj[ok], py[ok], a_y[ok], b_y[ok]
    t = (py - a_y) / (b_y - a_y)
    x = ax[rep] + t * (bx[rep] - ax[rep])
    sens = np.where(b_y > a_y, 1, -1)
    ordre = np.lexsort((rep, x, jj))     # par rangée, puis x, puis ordre d'ajout (tri stable de l'application)
    jj, x, sens = jj[ordre], x[ordre], sens[ordre]
    w = np.cumsum(sens)
    debut_rang = np.r_[True, jj[1:] != jj[:-1]]
    base = np.maximum.accumulate(np.where(debut_rang, np.arange(len(jj)), 0))
    w = w - np.r_[0, w][base]            # enroulement remis à zéro à chaque rangée
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


def edt_carre(masque, cible, rayon):
    """Distance² (en nœuds) de chaque nœud au plus proche nœud où ``masque == cible`` : exacte jusqu'à ``rayon`` nœuds
    (les mêmes entiers que la transformée de Felzenszwalb–Huttenlocher de l'application), au-delà > rayon²."""
    ny, nx = masque.shape
    cibles = masque == cible
    plafond = rayon + 1
    idx = np.arange(nx, dtype=np.int32)
    gauche = np.maximum.accumulate(np.where(cibles, idx, np.int32(-10 * nx - 10 * plafond)), axis=1)
    droite = np.minimum.accumulate(np.where(cibles, idx, np.int32(10 * nx + 10 * plafond))[:, ::-1], axis=1)[:, ::-1]
    g = np.minimum(np.minimum(idx - gauche, droite - idx), plafond).astype(np.int32)
    g2 = g * g
    d = g2.copy()
    for dk in range(1, min(rayon, ny - 1) + 1):
        e = np.int32(dk * dk)
        np.minimum(d[dk:], g2[:-dk] + e, out=d[dk:])
        np.minimum(d[:-dk], g2[dk:] + e, out=d[:-dk])
    return d


def depuis_masque(c, masque, rayon):
    """Champ.depuisMasque : + dedans, − dehors, zéro à mi-chemin entre deux nœuds (float32)."""
    dehors = edt_carre(masque, True, rayon)
    dedans = edt_carre(masque, False, rayon)
    demi = 0.5 * c.pas
    v = np.where(masque, np.sqrt(dedans.astype(np.float64)) * c.pas - demi, demi - np.sqrt(dehors.astype(np.float64)) * c.pas)
    return c.avec(v.astype(F32))


def affiner(c, masque, boucles, tuile=64):
    """Champ.affiner : près du bord (|valeur| < 1,5 pas), distance exacte aux segments des contours."""
    seg = _segments(boucles)
    r = c.v.copy()
    if seg.shape[0] == 0:
        return c.avec(r)
    bande = np.abs(r) < F32(1.5) * F32(c.pas)
    sx0 = np.minimum(seg[:, 0], seg[:, 2])
    sx1 = np.maximum(seg[:, 0], seg[:, 2])
    sy0 = np.minimum(seg[:, 1], seg[:, 3])
    sy1 = np.maximum(seg[:, 1], seg[:, 3])
    m = 4.0 * c.pas
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
            choix = (sx1 >= px.min() - m) & (sx0 <= px.max() + m) & (sy1 >= py.min() - m) & (sy0 <= py.max() + m)
            if not choix.any():
                continue
            s = seg[choix]
            d = np.full(px.shape, np.inf)
            pas_bloc = max(1, 250_000 // max(1, len(px)))
            for k in range(0, len(s), pas_bloc):
                t = s[k:k + pas_bloc]
                dd = K.distance_segment_vec(px[:, None], py[:, None], t[None, :, 0], t[None, :, 1], t[None, :, 2], t[None, :, 3])
                np.minimum(d, dd.min(axis=1), out=d)
            ok = np.isfinite(d)
            val = np.maximum(d[ok], 1e-6).astype(F32)
            jo, io = jj[ok], ii[ok]
            r[jo, io] = np.where(masque[jo, io], val, -val)
    return c.avec(r)


def rayon_exact(pas, decalage_max):
    """Rayon (nœuds) jusqu'où la transformée doit être exacte : décalages appliqués ensuite + marge de 3 nœuds."""
    return int(math.ceil((decalage_max + 0.5) / pas)) + 3


def distance_signee(c, boucles, decalage_max=1.0):
    """Champ.distanceSignee : remplissage non nul puis distance signée (mm), affinée près du bord. Valeurs exactes
    (identiques à l'application) jusqu'à ``decalage_max`` mm du bord et au-delà ; plus loin, signe et ordre de grandeur."""
    masque = rasteriser(c, boucles)
    return affiner(depuis_masque(c, masque, rayon_exact(c.pas, decalage_max)), masque, boucles)


def marching_squares(c):
    """MarchingSquares.contours : niveau 0, matière (valeur > 0) à gauche ; cas ambigus tranchés par la valeur au
    centre ; boucles dans l'ordre de l'application (départ à la plus petite arête), mêmes points d'arête."""
    nx, ny = c.nx, c.ny
    w = nx + 2
    p = np.full((ny + 2, nx + 2), F32(-1.0), dtype=F32)
    p[1:-1, 1:-1] = np.where(c.v == 0, _MOINS_ZERO, c.v)
    pos = p > 0
    xs = c.x0 + (np.arange(nx + 2, dtype=np.float64) - 1) * c.pas     # x(i) pour i = −1 … nx
    ys = c.y0 + (np.arange(ny + 2, dtype=np.float64) - 1) * c.pas
    points = {}
    # Arêtes horizontales (i, j)–(i+1, j) : point calculé de gauche à droite (dernière écriture de l'application).
    J, I = np.nonzero(pos[:, :-1] != pos[:, 1:])
    if J.size:
        a, b = p[J, I], p[J, I + 1]
        t = (a / (a - b)).astype(np.float64)
        px = xs[I] + t * (xs[I + 1] - xs[I])
        py = ys[J] + t * (ys[J] - ys[J])
        for idv, x, y in zip((2 * (J * w + I)).tolist(), px.tolist(), py.tolist()):
            points[idv] = (x, y)
    # Arêtes verticales (i, j)–(i, j+1) : point calculé de haut en bas.
    J, I = np.nonzero(pos[:-1, :] != pos[1:, :])
    if J.size:
        a, b = p[J + 1, I], p[J, I]
        t = (a / (a - b)).astype(np.float64)
        px = xs[I] + t * (xs[I] - xs[I])
        py = ys[J + 1] + t * (ys[J] - ys[J + 1])
        for idv, x, y in zip((2 * (J * w + I) + 1).tolist(), px.tolist(), py.tolist()):
            points[idv] = (x, y)
    # Cellules : coins BG (J, I), BD (J, I+1), HD (J+1, I+1), HG (J+1, I) du tableau bordé.
    c0, c1, c2, c3 = pos[:-1, :-1], pos[:-1, 1:], pos[1:, 1:], pos[1:, :-1]
    n_pos = c0.astype(np.int8) + c1 + c2 + c3
    J, I = np.nonzero((n_pos > 0) & (n_pos < 4))
    suivant = {}
    if J.size:
        cv = np.stack([p[J, I], p[J, I + 1], p[J + 1, I + 1], p[J + 1, I]], axis=1)
        ids = np.stack([2 * (J * w + I), 2 * (J * w + I + 1) + 1, 2 * ((J + 1) * w + I), 2 * (J * w + I) + 1], axis=1)
        positif = cv > 0
        suiv = np.roll(positif, -1, axis=1)
        coupe = positif != suiv
        debut = positif & ~suiv
        simples = debut.sum(axis=1) == 1
        if simples.any():
            s = np.argmax(debut[simples], axis=1)
            e = np.argmax(coupe[simples] & ~debut[simples], axis=1)
            idx = ids[simples]
            r = np.arange(idx.shape[0])
            suivant.update(zip(idx[r, s].tolist(), idx[r, e].tolist()))
        for m in np.nonzero(~simples)[0].tolist():
            v = cv[m]
            centre = (((v[0] + v[1]) + v[2]) + v[3]) / F32(4)
            for k in range(4):
                if debut[m, k]:
                    e = (k + 1) % 4 if centre > 0 else (k + 3) % 4
                    suivant[int(ids[m, k])] = int(ids[m, e])
    vu = set()
    boucles = []
    for depart in sorted(suivant):
        if depart in vu:
            continue
        pts = []
        cle = depart
        while cle not in vu:
            vu.add(cle)
            pts.append(points[cle])
            cle = suivant.get(cle, -1)
            if cle < 0:
                break
        if len(pts) >= 3:
            boucles.append(np.asarray(pts, dtype=np.float64))
    return boucles


def regions(champ, tolerance=0.02, pas=0.1, aire_min=0.05):
    """Impression3D.regions : contours simplifiés (0,02 mm ; 0,1 mm), boucles de plus de 0,05 mm², polygones."""
    boucles = []
    for b in champ.contours():
        s = simplifier(b, tolerance, pas)
        if s is not None and abs(aire(s)) > aire_min:
            boucles.append(s)
    return regrouper(boucles)
