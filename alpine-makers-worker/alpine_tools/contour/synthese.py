"""Photos SYNTHÉTIQUES (port de Synthese.kt) : feuille vue en perspective sur une table sombre, objets de cotes
connues, éclairage inégal, ombre douce, bruit de capteur. Sert aux tests de précision (aucune vraie photo).

Formes : distance signée en mm (négative dedans), coordonnées de la feuille (y vers le bas), vectorisées.
"""
import math

import numpy as np

from .geometrie import Homographie
from .image import Reperes


def rectangle(cx, cy, l, h, rayon=0.0, angle_deg=0.0):
    a = math.radians(angle_deg)

    def d(x, y):
        px = (x - cx) * math.cos(a) + (y - cy) * math.sin(a)
        py = -(x - cx) * math.sin(a) + (y - cy) * math.cos(a)
        qx = np.abs(px) - (l / 2 - rayon)
        qy = np.abs(py) - (h / 2 - rayon)
        return np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0)) + np.minimum(np.maximum(qx, qy), 0.0) - rayon
    return d


def cercle(cx, cy, r):
    return lambda x, y: np.hypot(x - cx, y - cy) - r


def capsule(ax, ay, bx, by, r):
    def d(x, y):
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = np.clip(((x - ax) * dx + (y - ay) * dy) / l2, 0.0, 1.0) if l2 else 0.0
        return np.hypot(x - (ax + t * dx), y - (ay + t * dy)) - r
    return d


def hexagone(cx, cy, r, angle_deg=0.0):
    def d(x, y):
        m = None
        for k in range(6):
            a = math.radians(angle_deg + 60.0 * k)
            v = (x - cx) * math.cos(a) + (y - cy) * math.sin(a) - r
            m = v if m is None else np.maximum(m, v)
        return m
    return d


def union(*f):
    def d(x, y):
        m = f[0](x, y)
        for g in f[1:]:
            m = np.minimum(m, g(x, y))
        return m
    return d


def moins(a, b):
    return lambda x, y: np.maximum(a(x, y), -b(x, y))


class Objet:
    def __init__(self, forme, couleur):
        self.forme, self.couleur = forme, couleur


class Scene:
    def __init__(self, fmt, objets, coins, largeur, hauteur, ombre=0.0, bruit=0.012, eclairage=0.14, graine=1):
        self.format, self.objets, self.coins = fmt, objets, coins
        self.largeur, self.hauteur = largeur, hauteur
        self.ombre, self.bruit, self.eclairage, self.graine = ombre, bruit, eclairage, graine


def _lisse(a, b, x):
    t = np.clip((x - a) / (b - a), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _couleurs(s, x, y, ix, iy):
    """Couleur (n, 3) 0..1 des points (mm) ; table sombre hors de la feuille (coordonnées pixel ix, iy)."""
    f = s.format
    dans = (x >= 0) & (y >= 0) & (x <= f.largeur) & (y <= f.hauteur)
    v = 0.82 + 0.12 * np.sin(iy * 0.045 + 3 * np.sin(ix * 0.004)) + 0.05 * np.sin(iy * 0.31 + ix * 0.002)
    c = np.stack([0.30 * v, 0.24 * v, 0.19 * v], axis=1)
    lumiere = 1.0 - s.eclairage * (0.75 * x / f.largeur + 0.45 * y / f.hauteur) + 0.04 * np.sin(x / 40.0)
    papier = np.tile(np.array([0.95, 0.94, 0.91]), (x.size, 1))
    if f.reperes:
        for rx, ry in Reperes.centres:
            noir = (np.abs(x - rx) <= Reperes.COTE / 2) & (np.abs(y - ry) <= Reperes.COTE / 2) & Reperes.noir(x - rx, y - ry)
            papier[noir] = (0.08, 0.08, 0.09)
        texte = (((y >= 9.5) & (y <= 12.5) & (x >= 30.0) & (x <= 150.0))
                 | ((y >= 16.0) & (y <= 27.0) & (x >= 30.0) & (x <= 165.0) & (((y - 16.0) % 4.0) < 1.6) & (((x * 1.7) % 3.0) < 2.0))
                 | ((y >= 278.75) & (y <= 279.25) & (x >= 55.0) & (x <= 155.0))
                 | ((y >= 283.0) & (y <= 291.0) & (x >= 55.0) & (x <= 140.0) & (((y - 283.0) % 3.5) < 1.5) & (((x * 1.7) % 3.0) < 2.0)))
        papier[texte] = (0.2, 0.2, 0.22)
        cadre = ((((np.abs(x - Reperes.CADRE_X0) < 0.12) | (np.abs(x - (210 - Reperes.CADRE_X0)) < 0.12)) & (y >= 30.0) & (y <= 267.0) & ((y % 2.1) < 1.05))
                 | (((np.abs(y - Reperes.CADRE_Y0) < 0.12) | (np.abs(y - (297 - Reperes.CADRE_Y0)) < 0.12)) & (x >= 10.0) & (x <= 200.0) & ((x % 2.1) < 1.05)))
        papier[cadre] = (0.6, 0.6, 0.6)
    ombre = np.zeros(x.size)
    pris = np.zeros(x.size, dtype=bool)
    resultat = papier.copy()
    for o in s.objets:
        dedans = (o.forme(x, y) < 0) & ~pris
        if dedans.any():
            col = np.array([(o.couleur >> 16) & 0xFF, (o.couleur >> 8) & 0xFF, o.couleur & 0xFF]) / 255.0
            reflet = 1.0 + 0.08 * np.sin(x[dedans] / 7.0 + y[dedans] / 11.0)
            resultat[dedans] = col[None, :] * (lumiere[dedans] * reflet)[:, None]
        # L'ombre ne compte que pour les objets parcourus avant celui qui contient le point (comme l'original).
        if s.ombre > 0:
            libre = ~pris & ~dedans
            ombre[libre] = np.maximum(ombre[libre], s.ombre * (1 - _lisse(-1.6, 2.2, o.forme(x[libre] - 1.4, y[libre] - 1.9))))
        pris |= dedans
    k = lumiere * (1 - ombre)
    resultat[~pris] = papier[~pris] * k[~pris, None]
    c[dans] = resultat[dans]
    return c


def _simple(s, x, y, mm_par_px):
    f = s.format
    marge = 2.5 * mm_par_px + 0.3
    bord = np.minimum(np.minimum(x, f.largeur - x), np.minimum(y, f.hauteur - y))
    simple = np.abs(bord) >= marge
    dehors = bord < 0
    if f.reperes:
        simple &= dehors | ~((y < 33.0) | (y > 264.0) | (x < 12.0) | (x > 198.0))
    for o in s.objets:
        simple &= dehors | (np.abs(o.forme(x, y)) >= marge)
        if s.ombre > 0:
            simple &= dehors | (np.abs(o.forme(x - 1.4, y - 1.9)) >= 2.5)
    return simple


def rendre(s, sur=3):
    """Photo uint8 (hauteur, largeur, 3) : un échantillon loin des bords, sur × sur près des bords."""
    f = s.format
    coins_mm = [(0.0, 0.0), (f.largeur, 0.0), (f.largeur, f.hauteur), (0.0, f.hauteur)]
    vers_mm = Homographie.depuis4(coins_mm, s.coins).inverse()
    rng = np.random.default_rng(s.graine)
    sortie = np.empty((s.hauteur, s.largeur, 3), dtype=np.uint8)
    bande = max(1, 300_000 // s.largeur)
    for j0 in range(0, s.hauteur, bande):
        j1 = min(s.hauteur, j0 + bande)
        jj, ii = np.mgrid[j0:j1, 0:s.largeur]
        ix, iy = ii.ravel() + 0.5, jj.ravel() + 0.5
        cx, cy = vers_mm.appliquer(ix, iy)
        vx, vy = vers_mm.appliquer(ix + 1.0, iy)
        simple = _simple(s, cx, cy, np.hypot(vx - cx, vy - cy))
        c = np.zeros((ix.size, 3))
        k = np.flatnonzero(simple)
        c[k] = _couleurs(s, cx[k], cy[k], ix[k], iy[k])
        k = np.flatnonzero(~simple)
        if k.size:
            acc = np.zeros((k.size, 3))
            for sy in range(sur):
                for sx in range(sur):
                    ex = ii.ravel()[k] + (sx + 0.5) / sur
                    ey = jj.ravel()[k] + (sy + 0.5) / sur
                    px, py = vers_mm.appliquer(ex, ey)
                    acc += _couleurs(s, px, py, ex, ey)
            c[k] = acc / (sur * sur)
        b = rng.standard_normal(ix.size) * s.bruit
        c[:, 0] += b
        c[:, 1] += b
        c[:, 2] += b * 1.1
        sortie[j0:j1] = np.clip(np.trunc(c * 255), 0, 255).reshape(j1 - j0, s.largeur, 3).astype(np.uint8)
    return sortie


# ------------------------------------------------------------------ scènes de démonstration

def cle_et_rondelle(l=1600, h=1200):
    """Clé plate (tête ouverte et œil hexagonal) et rondelle sur une feuille A4 en paysage, ombre douce."""
    from .image import A4
    manche = capsule(105.0, 70.0, 105.0, 190.0, 7.0)
    tete = moins(cercle(105.0, 208.0, 17.0), union(rectangle(105.0, 222.0, 13.0, 30.0), cercle(105.0, 207.0, 6.5)))
    oeil = moins(cercle(105.0, 58.0, 13.5), hexagone(105.0, 58.0, 6.5, 30.0))
    cle = union(manche, tete, oeil)
    rondelle = moins(cercle(160.0, 120.0, 15.0), cercle(160.0, 120.0, 6.5))
    coins = [(l * 0.86, h * 0.10), (l * 0.89, h * 0.92), (l * 0.12, h * 0.95), (l * 0.15, h * 0.08)]
    return Scene(A4, [Objet(cle, 0x7A8088), Objet(rondelle, 0x9A9CA2)], coins, l, h, ombre=0.18, graine=11)
