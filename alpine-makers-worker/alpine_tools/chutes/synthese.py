"""Photos SYNTHÉTIQUES de chutes (port de Synthese.kt d'Alpine Chutes ; aucune vraie photo) : une chute de forme connue
(bois veiné, MDF, acrylique, contreplaqué) et la feuille de repères (ou une feuille blanche), vues en perspective sur
une table, éclairage inégal, ombre douce, bruit de capteur. Deux poses : chute posée SUR la feuille (petite chute) ou
feuille posée SUR la chute (grande chute). Coordonnées du monde = celles de la feuille (mm, y vers le bas).

Formes : distance signée en mm (négative dedans), vectorisées. Le bruit vient de numpy (pas de java.util.Random) :
mêmes scènes que l'application, pas les mêmes pixels.
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


def polygone(pts):
    """Polygone quelconque (sommets en mm) : distance signée exacte."""
    pts = [(float(a), float(b)) for a, b in pts]

    def d(x, y):
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        dist = np.full(x.shape, np.inf)
        dedans = np.zeros(x.shape, dtype=bool)
        j = len(pts) - 1
        for i in range(len(pts)):
            ax, ay = pts[j]
            bx, by = pts[i]
            dx, dy = bx - ax, by - ay
            l2 = dx * dx + dy * dy
            t = np.clip(((x - ax) * dx + (y - ay) * dy) / l2, 0.0, 1.0) if l2 else 0.0
            dist = np.minimum(dist, np.hypot(x - (ax + t * dx), y - (ay + t * dy)))
            if ay != by:
                croise = ((by > y) != (ay > y)) & (x < (ax - bx) * (y - by) / (ay - by) + bx)
                dedans ^= croise
            j = i
        return np.where(dedans, -dist, dist)
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


MATIERES = ("bois", "mdf", "acrylique_bleu", "contreplaque")


class Scene:
    """``chute`` : forme ; ``sur_feuille`` : chute posée sur la feuille (sinon feuille posée sur la chute) ; ``defauts`` :
    taches sombres (nœuds) ; ``coins_feuille`` : coins de la FEUILLE dans la photo (pixels)."""

    def __init__(self, fmt, chute, matiere, sur_feuille, coins_feuille, largeur, hauteur, defauts=(), ombre=0.12, bruit=0.010,
                 eclairage=0.12, graine=1):
        self.format, self.chute, self.matiere, self.sur_feuille = fmt, chute, matiere, sur_feuille
        self.coins_feuille, self.largeur, self.hauteur, self.defauts = coins_feuille, largeur, hauteur, list(defauts)
        self.ombre, self.bruit, self.eclairage, self.graine = ombre, bruit, eclairage, graine


def _lisse(a, b, x):
    t = np.clip((x - a) / (b - a), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _matiere(m, x, y):
    if m == "bois":
        veine = 0.5 + 0.5 * np.sin(y * 0.9 + 2.2 * np.sin(x * 0.021) + 0.6 * np.sin(x * 0.11))
        k = 0.86 + 0.10 * veine
        return np.stack([0.80 * k, 0.60 * k, 0.38 * k], axis=1)
    if m == "contreplaque":
        veine = 0.5 + 0.5 * np.sin(x * 0.35 + 1.6 * np.sin(y * 0.03))
        k = 0.90 + 0.07 * veine
        return np.stack([0.86 * k, 0.74 * k, 0.55 * k], axis=1)
    if m == "mdf":
        k = 0.94 + 0.03 * np.sin(x * 0.7) * np.sin(y * 0.9)
        return np.stack([0.62 * k, 0.47 * k, 0.33 * k], axis=1)
    return np.tile(np.array([0.12, 0.36, 0.72]), (x.size, 1))


def _feuille(f, x, y):
    """Couleur du papier (repères, texte et cadre pour la feuille de repères) et masque « sur la feuille »."""
    dans = (x >= 0) & (y >= 0) & (x <= f.largeur) & (y <= f.hauteur)
    c = np.tile(np.array([0.95, 0.94, 0.91]), (x.size, 1))
    if f.reperes:
        texte = (((y >= 9.5) & (y <= 12.5) & (x >= 30.0) & (x <= 150.0))
                 | ((y >= 16.0) & (y <= 27.0) & (x >= 30.0) & (x <= 165.0) & (((y - 16.0) % 4.0) < 1.6) & (((x * 1.7) % 3.0) < 2.0))
                 | ((y >= 278.75) & (y <= 279.25) & (x >= 55.0) & (x <= 155.0))
                 | ((y >= 283.0) & (y <= 291.0) & (x >= 55.0) & (x <= 140.0) & (((y - 283.0) % 3.5) < 1.5) & (((x * 1.7) % 3.0) < 2.0)))
        c[texte] = (0.2, 0.2, 0.22)
        cadre = ((((np.abs(x - Reperes.CADRE_X0) < 0.12) | (np.abs(x - (210 - Reperes.CADRE_X0)) < 0.12)) & (y >= 30.0) & (y <= 267.0) & ((y % 2.1) < 1.05))
                 | (((np.abs(y - Reperes.CADRE_Y0) < 0.12) | (np.abs(y - (297 - Reperes.CADRE_Y0)) < 0.12)) & (x >= 10.0) & (x <= 200.0) & ((x % 2.1) < 1.05)))
        c[cadre] = (0.6, 0.6, 0.6)
        for rx, ry in Reperes.centres:
            noir = (np.abs(x - rx) <= Reperes.COTE / 2) & (np.abs(y - ry) <= Reperes.COTE / 2) & Reperes.noir(x - rx, y - ry)
            c[noir] = (0.08, 0.08, 0.09)
    return c, dans


def _table(x, y):
    v = 0.80 + 0.10 * np.sin(y * 0.05 + 3 * np.sin(x * 0.006)) + 0.04 * np.sin(y * 0.29 + x * 0.01)
    return np.stack([0.20 * v, 0.22 * v, 0.25 * v], axis=1)


def _couleur(s, x, y):
    """Couleur (n, 3) 0..1 aux points (mm du monde)."""
    lumiere = 1.0 - s.eclairage * (0.6 * x / 400.0 + 0.4 * y / 400.0) + 0.03 * np.sin(x / 45.0)
    sur_chute = s.chute(x, y) < 0
    papier, sur_papier = _feuille(s.format, x, y)
    defaut = np.zeros(x.size, dtype=bool)
    for d in s.defauts:
        defaut |= d(x, y) < 0
    matiere = _matiere(s.matiere, x, y)
    matiere[defaut] = (0.22, 0.14, 0.08)
    base = _table(x, y)
    if s.sur_feuille:
        base[sur_papier] = papier[sur_papier]
        base[sur_chute] = matiere[sur_chute]
    else:
        base[sur_chute] = matiere[sur_chute]
        base[sur_papier] = papier[sur_papier]
    ombre = np.zeros(x.size)
    if s.ombre > 0:
        if s.sur_feuille:
            k = ~sur_chute
            ombre[k] = s.ombre * (1 - _lisse(-1.2, 2.4, s.chute(x[k] - 1.2, y[k] - 1.6)))
        else:
            k = ~sur_papier & sur_chute
            fl, fh = s.format.largeur, s.format.hauteur
            dx = np.maximum(-(x[k] - 0.8), (x[k] - 0.8) - fl)
            dy = np.maximum(-(y[k] - 1.0), (y[k] - 1.0) - fh)
            ombre[k] = 0.5 * s.ombre * (1 - _lisse(0.0, 1.8, np.maximum(dx, dy)))
    return base * (lumiere * (1 - ombre))[:, None]


def _simple(s, x, y, mm_par_px):
    """Pixel loin de tout bord : un seul échantillon suffit."""
    f = s.format
    marge = 2.5 * mm_par_px + 0.3
    bord = np.minimum(np.minimum(x, f.largeur - x), np.minimum(y, f.hauteur - y))
    simple = np.abs(bord) >= marge + 1.5
    if f.reperes:
        simple &= ~((bord > 0) & ((y < 33.0) | (y > 264.0) | (x < 12.0) | (x > 198.0)))
    simple &= np.abs(s.chute(x, y)) >= marge + 2.5
    for d in s.defauts:
        simple &= np.abs(d(x, y)) >= marge
    return simple


def rendre(s, sur=3):
    """Photo uint8 (hauteur, largeur, 3) : un échantillon loin des bords, sur × sur près des bords, bruit gaussien."""
    f = s.format
    coins_mm = [(0.0, 0.0), (f.largeur, 0.0), (f.largeur, f.hauteur), (0.0, f.hauteur)]
    vers_mm = Homographie.depuis4(coins_mm, s.coins_feuille).inverse()
    rng = np.random.default_rng(s.graine)
    sortie = np.empty((s.hauteur, s.largeur, 3), dtype=np.uint8)
    bande = max(1, 250_000 // s.largeur)
    for j0 in range(0, s.hauteur, bande):
        j1 = min(s.hauteur, j0 + bande)
        jj, ii = np.mgrid[j0:j1, 0:s.largeur]
        ii, jj = ii.ravel(), jj.ravel()
        cx, cy = vers_mm.appliquer(ii + 0.5, jj + 0.5)
        vx, vy = vers_mm.appliquer(ii + 1.5, jj + 0.5)
        simple = _simple(s, cx, cy, np.hypot(vx - cx, vy - cy))
        c = np.zeros((ii.size, 3))
        k = np.flatnonzero(simple)
        c[k] = _couleur(s, cx[k], cy[k])
        k = np.flatnonzero(~simple)
        if k.size:
            acc = np.zeros((k.size, 3))
            for sy in range(sur):
                for sx in range(sur):
                    px, py = vers_mm.appliquer(ii[k] + (sx + 0.5) / sur, jj[k] + (sy + 0.5) / sur)
                    acc += _couleur(s, px, py)
            c[k] = acc / (sur * sur)
        b = rng.standard_normal(ii.size) * s.bruit
        c[:, 0] += b
        c[:, 1] += b
        c[:, 2] += b * 1.1
        sortie[j0:j1] = np.clip(np.trunc(c * 255), 0, 255).reshape(j1 - j0, s.largeur, 3).astype(np.uint8)
    return sortie


# ------------------------------------------------------------------ scènes de démonstration (FICTIVES)

def contreplaque_sur_reperes(l=1600, h=1200):
    """Chute de contreplaqué en L, entaillée, posée sur la feuille de repères (petite chute)."""
    from .image import REPERES_A4
    chute = polygone([(28.0, 44.0), (168.0, 40.0), (182.0, 62.0), (178.0, 150.0), (120.0, 156.0), (112.0, 250.0), (34.0, 254.0),
                      (24.0, 180.0), (38.0, 120.0)])
    coins = [(l * 0.84, h * 0.06), (l * 0.87, h * 0.94), (l * 0.15, h * 0.96), (l * 0.13, h * 0.05)]
    return Scene(REPERES_A4, chute, "contreplaque", True, coins, l, h, defauts=[cercle(70.0, 205.0, 6.0)], graine=23)


def grande_chute_mdf(l=1600, h=1200):
    """Grande chute de MDF (≈ 420 × 300 mm), feuille de repères posée dessus."""
    from .image import REPERES_A4
    chute = polygone([(-120.0, -40.0), (300.0, -48.0), (318.0, 120.0), (296.0, 330.0), (140.0, 344.0), (60.0, 312.0), (-110.0, 318.0),
                      (-126.0, 140.0)])
    coins = [(l * 0.66, h * 0.30), (l * 0.67, h * 0.70), (l * 0.36, h * 0.71), (l * 0.37, h * 0.29)]
    return Scene(REPERES_A4, chute, "mdf", False, coins, l, h, defauts=[cercle(250.0, 260.0, 9.0)], graine=7)


def bois_sur_a4(l=1600, h=1200):
    """Chute de bois veiné posée sur une feuille A4 blanche."""
    from .image import A4
    chute = polygone([(30.0, 30.0), (176.0, 36.0), (170.0, 140.0), (140.0, 168.0), (150.0, 260.0), (40.0, 262.0)])
    coins = [(l * 0.86, h * 0.10), (l * 0.89, h * 0.92), (l * 0.12, h * 0.95), (l * 0.15, h * 0.08)]
    return Scene(A4, chute, "bois", True, coins, l, h, defauts=[cercle(110.0, 90.0, 5.0)], graine=11)
