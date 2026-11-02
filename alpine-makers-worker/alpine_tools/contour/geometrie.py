"""Géométrie plane d'Alpine Contour : homographie, boucles, regroupement, simplification et cotes.

Port de Homographie.kt, Contours.kt et Traceur.cotes (C:\\AlpineContour\\core). Boucles en mm, y vers le haut,
matière à gauche (extérieur direct, trou indirect).
"""
import math
from decimal import Decimal, ROUND_HALF_UP

import numpy as np


# ------------------------------------------------------------------ formats (Formats.kt, Locale.FRANCE)

def _groupes(entier):
    t = str(entier)
    morceaux = []
    while len(t) > 3:
        morceaux.insert(0, t[-3:])
        t = t[:-3]
    morceaux.insert(0, t)
    return "\u202f".join(morceaux)


def arrondi(v, decimales):
    """Arrondi « au plus proche, moitié vers le haut » sur l'écriture décimale courte (comme String.format de Java)."""
    q = Decimal(1).scaleb(-decimales)
    return Decimal(repr(float(v))).quantize(q, rounding=ROUND_HALF_UP)


def decimal_fr(v, decimales=1):
    d = arrondi(v, decimales)
    signe = "-" if d < 0 else ""
    t = format(abs(d), "f")
    ent, _, frac = t.partition(".")
    return signe + _groupes(ent) + ("," + frac if decimales > 0 else "")


def mm(v, decimales=1):
    return decimal_fr(v, decimales) + " mm"


def mm2(v):
    return decimal_fr(v / 100.0, 1) + " cm²" if v >= 10_000 else decimal_fr(v, 0) + " mm²"


# ------------------------------------------------------------------ homographie

class Homographie:
    """Matrice 3×3 ligne par ligne : x' = (h0 x + h1 y + h2) / (h6 x + h7 y + h8)."""
    __slots__ = ("h",)

    def __init__(self, h):
        h = [float(v) for v in h]
        if len(h) != 9:
            raise ValueError("Homographie : 9 coefficients attendus.")
        self.h = h

    def appliquer(self, x, y):
        h = self.h
        w = h[6] * x + h[7] * y + h[8]
        return (h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w

    def inverse(self):
        a = self.h
        c = [a[4] * a[8] - a[5] * a[7], a[2] * a[7] - a[1] * a[8], a[1] * a[5] - a[2] * a[4],
             a[5] * a[6] - a[3] * a[8], a[0] * a[8] - a[2] * a[6], a[2] * a[3] - a[0] * a[5],
             a[3] * a[7] - a[4] * a[6], a[1] * a[6] - a[0] * a[7], a[0] * a[4] - a[1] * a[3]]
        return Homographie(_normaliser(c))

    def apres(self, autre):
        """Composition : d'abord ``autre``, puis self."""
        a, b = self.h, autre.h
        c = [0.0] * 9
        for i in range(3):
            for j in range(3):
                s = 0.0
                for k in range(3):
                    s += a[3 * i + k] * b[3 * k + j]
                c[3 * i + j] = s
        return Homographie(_normaliser(c))

    def echelle_locale(self, x, y):
        """Pixels de la photo par mm de la feuille autour de (x, y) mm (racine du jacobien)."""
        e = 0.5
        ox, oy = self.appliquer(x, y)
        ax, ay = self.appliquer(x + e, y)
        bx, by = self.appliquer(x, y + e)
        dx = (ax - ox, ay - oy)
        dy = (bx - ox, by - oy)
        return math.sqrt(abs(dx[0] * dy[1] - dx[1] * dy[0])) / e

    @staticmethod
    def depuis4(source, cible):
        """Homographie qui envoie exactement les 4 points ``source`` sur ``cible`` (DLT normalisée de Hartley)."""
        if len(source) != 4 or len(cible) != 4:
            raise ValueError("Quatre points sont nécessaires.")
        source = [(float(p[0]), float(p[1])) for p in source]
        cible = [(float(p[0]), float(p[1])) for p in cible]
        if _alignes(source) or _alignes(cible):
            raise ValueError("Trois des quatre points sont alignés : redressement impossible.")
        ts, tc = _normalisation(source), _normalisation(cible)
        s = [ts.appliquer(*p) for p in source]
        c = [tc.appliquer(*p) for p in cible]
        a = []
        for k in range(4):
            x, y = s[k]
            u, v = c[k]
            a.append([x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y, u])
            a.append([0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y, v])
        sol = resoudre(a)
        if sol is None:
            raise ValueError("Les quatre points sont alignés : redressement impossible.")
        hn = Homographie(sol + [1.0])
        return tc.inverse().apres(hn.apres(ts))


IDENTITE = Homographie([1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0])


def _normaliser(c):
    s = c[8] if abs(c[8]) > 1e-15 else math.sqrt(sum(v * v for v in c))
    return [v / s for v in c]


def _alignes(p):
    e = max(max(q[0] for q in p) - min(q[0] for q in p), max(q[1] for q in p) - min(q[1] for q in p))
    if e <= 0:
        return True
    for i in range(4):
        a, b, c = p[(i + 1) % 4], p[(i + 2) % 4], p[(i + 3) % 4]
        if abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) / 2 < 1e-6 * e * e:
            return True
    return False


def _normalisation(p):
    cx = sum(q[0] for q in p) / len(p)
    cy = sum(q[1] for q in p) / len(p)
    d = sum(math.hypot(q[0] - cx, q[1] - cy) for q in p) / len(p)
    s = math.sqrt(2.0) / d if d > 1e-12 else 1.0
    return Homographie([s, 0.0, -s * cx, 0.0, s, -s * cy, 0.0, 0.0, 1.0])


def resoudre(a):
    """Système n×n (dernière colonne = second membre), pivot partiel ; None si singulier."""
    a = [list(r) for r in a]
    n = len(a)
    for col in range(n):
        piv = col
        for r in range(col + 1, n):
            if abs(a[r][col]) > abs(a[piv][col]):
                piv = r
        if abs(a[piv][col]) < 1e-12:
            return None
        a[piv], a[col] = a[col], a[piv]
        for r in range(n):
            if r != col:
                f = a[r][col] / a[col][col]
                if f != 0.0:
                    ligne, pivot = a[r], a[col]
                    for k in range(col, n + 1):
                        ligne[k] -= f * pivot[k]
    return [a[i][n] / a[i][i] for i in range(n)]


# ------------------------------------------------------------------ boucles et polygones

class Boucle:
    """Contour fermé (mm). Aire signée positive en sens direct."""
    __slots__ = ("x", "y", "_aire")

    def __init__(self, x, y):
        self.x = np.ascontiguousarray(x, dtype=np.float64)
        self.y = np.ascontiguousarray(y, dtype=np.float64)
        if self.x.shape != self.y.shape:
            raise ValueError("Boucle : x et y de tailles différentes.")
        self._aire = None

    @property
    def n(self):
        return int(self.x.size)

    @property
    def aire(self):
        if self._aire is None:
            x, y = self.x, self.y
            self._aire = float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y) / 2) if x.size else 0.0
        return self._aire

    def inverse(self):
        return Boucle(self.x[::-1].copy(), self.y[::-1].copy())

    def contient(self, px, py):
        x, y = self.x, self.y
        xj, yj = np.roll(x, 1), np.roll(y, 1)
        cond = (y > py) != (yj > py)
        if not cond.any():
            return False
        xi, yi, xj, yj = x[cond], y[cond], xj[cond], yj[cond]
        xint = (xj - xi) * (py - yi) / (yj - yi) + xi
        return bool(np.count_nonzero(px < xint) % 2)

    def boite(self):
        return [float(self.x.min()), float(self.y.min()), float(self.x.max()), float(self.y.max())]

    def translater(self, dx, dy):
        return Boucle(self.x + dx, self.y + dy)


class Polygone:
    __slots__ = ("exterieur", "trous")

    def __init__(self, exterieur, trous):
        self.exterieur = exterieur
        self.trous = list(trous)


def boite(boucles):
    b = [math.inf, math.inf, -math.inf, -math.inf]
    for l in boucles:
        c = l.boite()
        b = [min(b[0], c[0]), min(b[1], c[1]), max(b[2], c[2]), max(b[3], c[3])]
    return b


def regrouper(boucles):
    """Boucles sans croisement → polygones à trous d'après l'imbrication (profondeur paire = extérieur)."""
    valides = [b for b in boucles if b.n >= 3 and abs(b.aire) > 1e-9]
    aires = [abs(b.aire) for b in valides]
    boites = [b.boite() for b in valides]
    parent = [-1] * len(valides)
    profondeur = [0] * len(valides)
    for i, bi in enumerate(valides):
        px, py = float(bi.x[0]), float(bi.y[0])
        meilleur = -1
        for j, bj in enumerate(valides):
            if i == j or aires[j] <= aires[i]:
                continue
            b = boites[j]
            if px < b[0] or px > b[2] or py < b[1] or py > b[3]:
                continue
            if bj.contient(px, py):
                profondeur[i] += 1
                if meilleur < 0 or aires[j] < aires[meilleur]:
                    meilleur = j
        parent[i] = meilleur
    sortie = []
    for e in range(len(valides)):
        if profondeur[e] % 2:
            continue
        ext = valides[e] if valides[e].aire >= 0 else valides[e].inverse()
        trous = [valides[t] if valides[t].aire <= 0 else valides[t].inverse()
                 for t in range(len(valides)) if profondeur[t] % 2 == 1 and parent[t] == e]
        sortie.append(Polygone(ext, trous))
    return sortie


def distance_segment(px, py, ax, ay, bx, by):
    """Distance d'un point (ou de tableaux numpy de points) au segment [a, b]."""
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if np.ndim(l2) == 0 and l2 == 0.0:
        return np.hypot(px - ax, py - ay)
    with np.errstate(invalid="ignore", divide="ignore"):
        t = np.clip(((px - ax) * dx + (py - ay) * dy) / l2, 0.0, 1.0)
    t = np.where(l2 == 0.0, 0.0, t)
    return np.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplifier_boucle(b, tolerance, pas):
    """Distance radiale ``pas`` puis Douglas–Peucker fermé à ``tolerance`` ; sommets d'origine conservés."""
    if b.n < 4:
        return b
    xs, ys = b.x.tolist(), b.y.tolist()
    gx, gy = [xs[0]], [ys[0]]
    hyp = math.hypot
    for i in range(1, len(xs)):
        if hyp(xs[i] - gx[-1], ys[i] - gy[-1]) >= pas:
            gx.append(xs[i])
            gy.append(ys[i])
    if len(gx) > 3 and hyp(gx[-1] - gx[0], gy[-1] - gy[0]) < pas:
        gx.pop()
        gy.pop()
    if len(gx) < 3:
        return None
    X, Y = np.array(gx), np.array(gy)
    n = X.size
    d0 = np.hypot(X[1:] - X[0], Y[1:] - Y[0])
    loin = int(np.argmax(d0)) + 1
    garde = np.zeros(n, dtype=bool)
    garde[0] = garde[loin] = True
    pile = [(0, loin), (loin, n)]
    while pile:
        a, bb = pile.pop()
        if bb - a < 2:
            continue
        ex, ey = X[bb % n], Y[bb % n]
        d = distance_segment(X[a + 1:bb], Y[a + 1:bb], X[a], Y[a], ex, ey)
        k = int(np.argmax(d))
        if d[k] > tolerance:
            idx = a + 1 + k
            garde[idx] = True
            pile.append((idx, bb))
            pile.append((a, idx))
    if np.count_nonzero(garde) < 3:
        return None
    return sans_alignes(Boucle(X[garde], Y[garde]))


def sans_alignes(b):
    """Retire les sommets doublons et alignés."""
    xs, ys = b.x.tolist(), b.y.tolist()
    change = True
    while change and len(xs) >= 3:
        change = False
        i = 0
        while i < len(xs) and len(xs) >= 3:
            n = len(xs)
            p, s = (i - 1) % n, (i + 1) % n
            croix = (xs[i] - xs[p]) * (ys[s] - ys[p]) - (ys[i] - ys[p]) * (xs[s] - xs[p])
            l = math.hypot(xs[s] - xs[p], ys[s] - ys[p])
            doublon = xs[i] == xs[p] and ys[i] == ys[p]
            if doublon or abs(croix) <= 1e-9 * max(l, 1e-9) * max(l, 1.0):
                del xs[i]
                del ys[i]
                change = True
            else:
                i += 1
    return Boucle(xs, ys) if len(xs) >= 3 else None


def perimetre(b):
    return float(np.sum(np.hypot(np.roll(b.x, -1) - b.x, np.roll(b.y, -1) - b.y)))


# ------------------------------------------------------------------ enveloppe convexe et cotes

def enveloppe_convexe(points):
    """Chaîne monotone d'Andrew, sans points alignés ; ``points`` : itérable de (x, y)."""
    p = sorted(set((float(a), float(b)) for a, b in points))
    if len(p) < 3:
        return p

    def croix(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    bas = []
    for q in p:
        while len(bas) >= 2 and croix(bas[-2], bas[-1], q) <= 0:
            bas.pop()
        bas.append(q)
    haut = []
    for q in reversed(p):
        while len(haut) >= 2 and croix(haut[-2], haut[-1], q) <= 0:
            haut.pop()
        haut.append(q)
    return bas[:-1] + haut[:-1]


def aire_points(p):
    s = 0.0
    for i in range(len(p)):
        j = (i + 1) % len(p)
        s += p[i][0] * p[j][1] - p[j][0] * p[i][1]
    return s / 2


class Cotes:
    """Cotes d'un contour (mm) : boîte sur la feuille, plus petit rectangle englobant, aire, périmètre, trous."""
    __slots__ = ("largeur", "hauteur", "aire", "perimetre", "longueur", "largeur_min", "angle", "trous")

    def __init__(self, largeur, hauteur, aire, perimetre, longueur, largeur_min, angle, trous):
        self.largeur, self.hauteur, self.aire, self.perimetre = largeur, hauteur, aire, perimetre
        self.longueur, self.largeur_min, self.angle, self.trous = longueur, largeur_min, angle, trous

    def dict(self):
        return {k: (round(getattr(self, k), 3) if isinstance(getattr(self, k), float) else getattr(self, k)) for k in self.__slots__}


def cotes(boucles):
    ext = [b for b in boucles if b.aire > 0] or list(boucles)
    b = boite(ext)
    pts = [(x, y) for l in ext for x, y in zip(l.x.tolist(), l.y.tolist())]
    env = enveloppe_convexe(pts)
    meilleur = (b[2] - b[0], b[3] - b[1], 0.0)
    aire_min = meilleur[0] * meilleur[1]
    if len(env) >= 2:
        E = np.array(env)
        for i in range(len(env)):
            a, c = env[i], env[(i + 1) % len(env)]
            lg = math.hypot(c[0] - a[0], c[1] - a[1])
            if lg < 1e-9:
                continue
            ux, uy = (c[0] - a[0]) / lg, (c[1] - a[1]) / lg
            u = E[:, 0] * ux + E[:, 1] * uy
            v = -E[:, 0] * uy + E[:, 1] * ux
            du, dv = float(u.max() - u.min()), float(v.max() - v.min())
            s = du * dv
            if s < aire_min - 1e-9:
                aire_min = s
                meilleur = (du, dv, math.degrees(math.atan2(uy, ux)))
    longueur, largeur = max(meilleur[0], meilleur[1]), min(meilleur[0], meilleur[1])
    angle = meilleur[2] + (90.0 if meilleur[1] > meilleur[0] else 0.0)
    angle = ((angle % 180.0) + 180.0) % 180.0
    return Cotes(b[2] - b[0], b[3] - b[1], sum(l.aire for l in boucles), sum(perimetre(l) for l in boucles),
                 longueur, largeur, angle, sum(1 for l in boucles if l.aire < 0))
