# -*- coding: utf-8 -*-
"""Carte des couleurs (copie de alpine_tools/tableau_couleur/carte.py : le Ciel étoilé en utilise CarteTableau,
ParamTableau, index_base, rendre, entier_fr et DENSITE_PLA) : réglages, bornes, rééchantillonnage, formes, quantification.

Portage de C:\\AlpineGift core TableauCouleur.kt (ParamTableau, borner, dimensions, plateau, echantillon,
masqueForme, carte, carteImpression, conseils) ; boucles par pixel remplacées par des calculs numpy dans le même
ordre d'opérations (mêmes sommes, mêmes arrondis « jsRound »).
"""
import math
from dataclasses import dataclass, replace

import numpy as np

from . import couleurs as C

COTE_CARTE_MAX = 4000
DENSITE_PLA = 1.24          # g/cm³, indicatif comme Toile Creator
PLATEAU_P1S = 256.0
PIXELS = (0.1, 0.15, 0.2, 0.3, 0.4)
FORMES = {"rectangle": "Rectangle", "arrondi": "Arrondi", "cercle": "Cercle", "coeur": "Cœur"}
# Pixels de la carte à l'aperçu au plus (TableauUi.APERCU_PIXELS_MAX) : ses seuils sont repris à l'impression.
APERCU_PIXELS_MAX = 1_200_000
_BLOC = 1 << 20             # pixels traités par bloc (mémoire bornée)


@dataclass(frozen=True)
class Recadrage:
    """Recadrage normalisé 0..1 dans la photo."""
    x: float = 0.0
    y: float = 0.0
    l: float = 1.0
    h: float = 1.0


@dataclass(frozen=True)
class ParamTableau:
    couleurs: tuple = C.PALETTES[0].couleurs
    ordre_auto: bool = True
    luminosite: float = 0.0
    contraste: float = 0.0
    gamma: float = 1.0
    repartition_egale: bool = True
    largeur_mm: float = 150.0
    hauteur_mm: float = 150.0
    forme: str = "rectangle"
    rayon_mm: float = 8.0
    base_mm: float = 2.0
    couche_mm: float = 0.6
    pixel_mm: float = 0.2
    face_dessous: bool = False
    couleur_base: int = -1        # index dans les couleurs rangées ; −1 = la plus claire


@dataclass
class CarteTableau:
    """``indices`` (h, l) uint8, ligne 0 en haut : index de couleur 0..N-1, 255 = vide."""
    l: int
    h: int
    pixel_mm: float
    largeur_mm: float
    hauteur_mm: float
    indices: np.ndarray
    couleurs: list
    seuils: list
    comptes: list

    @property
    def pas_x(self):
        return self.largeur_mm / self.l

    @property
    def pas_y(self):
        return self.hauteur_mm / self.h

    @property
    def pixels_pleins(self):
        return int(sum(self.comptes))


@dataclass(frozen=True)
class Dimensions:
    l: int
    h: int
    pixel_mm: float
    largeur_mm: float
    hauteur_mm: float
    ajuste: bool


@dataclass(frozen=True)
class ControlePlateau:
    tient: bool
    tourne: bool
    message: str


clamp = C.clamp
js_round = C.js_round


def arrondi(v, chiffres=2):
    f = 10.0 ** chiffres
    return js_round(v * f) / f


def nombre_fr(v):
    """Formats.nombre : entier sans décimale, sinon deux décimales au plus, virgule française."""
    if v == math.floor(v):
        return f"{v:.0f}"
    t = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return t.rstrip("0").rstrip(",")


def entier_fr(v):
    return f"{int(v):,}".replace(",", " ")


def dimensions(largeur_mm, hauteur_mm, pixel_mm):
    """Taille de la carte : arrondi(mm / pixel), ≤ COTE_CARTE_MAX (pixel élargi au besoin), mm recalés."""
    demande = pixel_mm if math.isfinite(pixel_mm) else 0.2
    pixel = clamp(demande, 0.08, 1.0)
    long = max(largeur_mm, hauteur_mm)
    if long / pixel > COTE_CARTE_MAX:
        pixel = math.ceil(long / COTE_CARTE_MAX * 1000) / 1000
    l = int(clamp(js_round(largeur_mm / pixel), 1, COTE_CARTE_MAX))
    h = int(clamp(js_round(hauteur_mm / pixel), 1, COTE_CARTE_MAX))
    return Dimensions(l, h, pixel, arrondi(l * pixel, 4), arrondi(h * pixel, 4), pixel != demande)


def plateau(largeur_mm, hauteur_mm, l=PLATEAU_P1S, h=PLATEAU_P1S):
    """Plateau P1S 256 × 256 : tient, tient tourné de 90°, ou dépasse (plateCheck)."""
    if largeur_mm <= l and hauteur_mm <= h:
        return ControlePlateau(True, False, f"Tient sur le plateau P1S ({int(l)} × {int(h)} mm).")
    if largeur_mm <= h and hauteur_mm <= l:
        return ControlePlateau(True, True, "Tient sur le plateau P1S en tournant le tableau de 90°.")
    return ControlePlateau(False, False, f"Le tableau ({nombre_fr(arrondi(largeur_mm, 1))} × {nombre_fr(arrondi(hauteur_mm, 1))} mm) "
                                         f"dépasse le plateau P1S ({int(l)} × {int(h)} mm).")


def borner(p):
    """Réglages ramenés dans leurs bornes (contrat de Toile Creator)."""
    return replace(
        p, couleurs=tuple(p.couleurs[:C.COULEURS_MAX]),
        luminosite=clamp(p.luminosite, -100.0, 100.0), contraste=clamp(p.contraste, -100.0, 100.0),
        gamma=clamp(p.gamma, 0.2, 5.0),
        largeur_mm=clamp(p.largeur_mm, 5.0, 400.0), hauteur_mm=clamp(p.hauteur_mm, 5.0, 400.0),
        rayon_mm=clamp(p.rayon_mm, 0.0, min(p.largeur_mm, p.hauteur_mm) / 2),
        base_mm=clamp(p.base_mm, 0.4, 10.0), couche_mm=clamp(p.couche_mm, 0.08, 3.0), pixel_mm=clamp(p.pixel_mm, 0.08, 1.0),
    )


def index_base(p, n):
    return p.couleur_base if 0 <= p.couleur_base < n else n - 1


# ================================================================================================ rééchantillonnage
def _empaqueter(a, r, g, b):
    """(alpha, r, g, b prémultipliés) → luminance Rec. 709 entière et opacité (alpha ≥ 128) ; a ≤ 0 → pixel nul."""
    ok = a > 0
    sa = np.where(ok, a, 1.0)
    aa = np.clip(np.floor(sa + 0.5), 0, 255)
    rr = np.clip(np.floor(r / sa + 0.5), 0, 255).astype(np.int64)
    gg = np.clip(np.floor(g / sa + 0.5), 0, 255).astype(np.int64)
    bb = np.clip(np.floor(b / sa + 0.5), 0, 255).astype(np.int64)
    luma = np.where(ok, (54 * rr + 183 * gg + 19 * bb) >> 8, 0).astype(np.uint8)
    return luma, ok & (aa >= 128)


def echantillon(src, r, l, h):
    """Zone ``r`` de ``src`` (tableau RGBA uint8 (H, W, 4)) en l × h : moyenne de surface en réduction, bilinéaire en
    agrandissement, couleurs prémultipliées (resampleTo). Rend luminance (h, l) uint8 et opacité (h, l) bool."""
    src = np.asarray(src, np.uint8)
    sh, sw = src.shape[:2]
    cx, cy = r.x * sw, r.y * sh
    cw, ch = max(1e-6, r.l * sw), max(1e-6, r.h * sh)
    luma = np.zeros((h, l), np.uint8)
    opaque = np.zeros((h, l), bool)
    if cw >= l and ch >= h:
        sx, sy = cw / l, ch / h
        ox = np.arange(l, dtype=np.float64)
        x0 = cx + ox * sx
        x1 = x0 + sx
        xa = np.clip(np.floor(x0).astype(np.int64), 0, sw - 1)
        xb = np.minimum(np.maximum(np.ceil(x1).astype(np.int64), xa + 1), sw)
        w_premier = np.maximum(0.0, np.minimum(xa + 1.0, x1) - np.maximum(xa.astype(np.float64), x0))
        w_dernier = np.maximum(0.0, np.minimum(xb.astype(np.float64), x1) - np.maximum(xb - 1.0, x0))
        kx = int((xb - xa).max())
        idx_x, w_x = [], []
        for k in range(kx):
            xx = xa + k
            w = np.where(xb - xa == 1, np.maximum(w_premier, 1e-9),
                         np.where(xx == xa, w_premier, np.where(xx == xb - 1, w_dernier, 1.0)))
            w_x.append(np.where(xx < xb, w, 0.0))
            idx_x.append(np.minimum(xx, sw - 1))
        sw_x = np.zeros(l)
        for w in w_x:                       # même ordre que « sw += wx »
            sw_x = sw_x + w
        oy_all = np.arange(h, dtype=np.float64)
        y0_all = cy + oy_all * sy
        y1_all = y0_all + sy
        ya_all = np.clip(np.floor(y0_all).astype(np.int64), 0, sh - 1)
        yb_all = np.minimum(np.maximum(np.ceil(y1_all).astype(np.int64), ya_all + 1), sh)
        rangs = max(1, min(_BLOC // max(1, l * kx), _BLOC // max(1, l * (int(math.ceil(sy)) + 1))))
        for o0 in range(0, h, rangs):
            o1 = min(h, o0 + rangs)
            ya, yb, y0, y1 = ya_all[o0:o1], yb_all[o0:o1], y0_all[o0:o1], y1_all[o0:o1]
            r0, r1 = int(ya.min()), int(yb.max())
            bloc = src[r0:r1]
            hx = np.zeros((r1 - r0, l, 4))
            for k in range(kx):             # Σ sur xx croissant, comme la boucle Kotlin
                q = bloc[:, idx_x[k], :].astype(np.float64)
                al = q[..., 3]
                prem = np.stack((al, q[..., 0] * al, q[..., 1] * al, q[..., 2] * al), axis=-1)
                hx = hx + prem * w_x[k][None, :, None]
            acc = np.zeros((o1 - o0, l, 4))
            acc_w = np.zeros((o1 - o0, l))
            for k in range(int((yb - ya).max())):
                sy0 = ya + k
                wy = np.maximum(0.0, np.minimum(sy0 + 1.0, y1) - np.maximum(sy0.astype(np.float64), y0))
                wy = np.where(yb - ya == 1, np.maximum(wy, 1e-9), wy)
                wy = np.where((sy0 < yb) & (wy > 0), wy, 0.0)
                lignes = np.minimum(sy0, r1 - 1) - r0
                acc = acc + hx[lignes] * wy[:, None, None]
                acc_w = acc_w + sw_x[None, :] * wy[:, None]
            tw = np.where(acc_w > 0, acc_w, 1.0)
            a = np.where(acc_w > 0, acc[..., 0] / tw, 0.0)
            lu, op = _empaqueter(a, acc[..., 1] / tw, acc[..., 2] / tw, acc[..., 3] / tw)
            luma[o0:o1], opaque[o0:o1] = lu, op
    else:
        ox = np.arange(l, dtype=np.float64)
        fx = np.clip(cx + (ox + 0.5) * cw / l - 0.5, 0.0, sw - 1.0)
        x0 = np.floor(fx).astype(np.int64)
        x1 = np.minimum(sw - 1, x0 + 1)
        tx = fx - x0
        rangs = max(1, _BLOC // max(1, l))
        for o0 in range(0, h, rangs):
            o1 = min(h, o0 + rangs)
            oy = np.arange(o0, o1, dtype=np.float64)
            fy = np.clip(cy + (oy + 0.5) * ch / h - 0.5, 0.0, sh - 1.0)
            y0 = np.floor(fy).astype(np.int64)
            y1 = np.minimum(sh - 1, y0 + 1)
            ty = (fy - y0)[:, None]
            sa = np.zeros((o1 - o0, l))
            sr = np.zeros_like(sa)
            sg = np.zeros_like(sa)
            sb = np.zeros_like(sa)
            for coin in range(4):
                ys = y0 if coin < 2 else y1
                xs = x0 if coin % 2 == 0 else x1
                p = src[ys][:, xs].astype(np.float64)
                w = (1 - tx if coin % 2 == 0 else tx)[None, :] * (1 - ty if coin < 2 else ty)
                al = p[..., 3]
                sa = sa + al * w
                sr = sr + p[..., 0] * al * w
                sg = sg + p[..., 1] * al * w
                sb = sb + p[..., 2] * al * w
            lu, op = _empaqueter(sa, sr, sg, sb)
            luma[o0:o1], opaque[o0:o1] = lu, op
    return luma, opaque


# ================================================================================================ formes
def contour_coeur(cx, cy, largeur, hauteur, n=360):
    """Contour du cœur (Forme.coeur) : x = 16 sin³t, y = 13 cos t − 5 cos 2t − 2 cos 3t − cos 4t, sens direct, inscrit."""
    px, py = [], []
    for k in range(n):
        t = 2 * math.pi * (n - k) / n
        px.append(16 * math.sin(t) ** 3)
        py.append(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))
    x0, x1, y0, y1 = min(px), max(px), min(py), max(py)
    px = [cx + ((v - x0) / (x1 - x0) - 0.5) * largeur for v in px]
    py = [cy + ((v - y0) / (y1 - y0) - 0.5) * hauteur for v in py]
    aire = sum(px[i] * py[(i + 1) % n] - px[(i + 1) % n] * py[i] for i in range(n)) / 2
    if aire < 0:
        px.reverse()
        py.reverse()
    return np.array(px), np.array(py)


def masque_forme(l, h, forme, rayon_px):
    """Forme au pixel près (h, l) bool : rectangle, coins arrondis, ellipse inscrite, cœur (balayage pair-impair)."""
    m = np.ones((h, l), bool)
    if forme == "cercle":
        rx, ry = l / 2.0, h / 2.0
        dy = (np.arange(h) + 0.5 - ry) / ry
        span = np.where(dy * dy <= 1, rx * np.sqrt(np.maximum(0.0, 1 - dy * dy)), -1.0)
        m &= ~(np.abs(np.arange(l) + 0.5 - rx)[None, :] > span[:, None])
    elif forme == "arrondi":
        r = clamp(rayon_px if math.isfinite(rayon_px) else 0.0, 0.0, min(l, h) / 2.0)
        if r > 0:
            py = (np.arange(h) + 0.5)[:, None]
            px = (np.arange(l) + 0.5)[None, :]
            ligne = ~((py >= r) & (py <= h - r))
            cy = np.where(py < r, r, h - r)
            cxx = np.where(px < r, r, np.where(px > l - r, l - r, px))
            hors = (cxx != px) & ((px - cxx) ** 2 + (py - cy) ** 2 > r * r) & ligne
            m &= ~hors
    elif forme == "coeur":
        bx, by = contour_coeur(l / 2.0, h / 2.0, float(l), float(h))
        bx2, by2 = np.roll(bx, -1), np.roll(by, -1)
        cx = np.arange(l) + 0.5
        for y in range(h):
            py = h - (y + 0.5)            # le contour a y vers le haut
            coupe = (by <= py) != (by2 <= py)
            xs = np.sort(bx[coupe] + (py - by[coupe]) / (by2[coupe] - by[coupe]) * (bx2[coupe] - bx[coupe]))
            dedans = np.searchsorted(xs, cx, side="right") % 2 == 1
            m[y] &= dedans
    return m


# ================================================================================================ carte
def carte(src, r, p0, l, h, seuils_imposes=None):
    """Carte des couleurs en l × h pixels : échantillon, tonalité, forme ∧ opacité, quantification sombre → clair.
    ``seuils_imposes`` : seuils d'un aperçu repris tels quels (ce que l'on voit est ce que l'on imprime)."""
    p = borner(p0)
    if len(p.couleurs) < C.COULEURS_MIN:
        raise ValueError("Choisis au moins deux couleurs.")
    luma, opaque = echantillon(src, r, l, h)
    lut = C.table(p.luminosite, p.contraste, p.gamma)
    valeurs = lut[luma].astype(np.uint8)
    valide = masque_forme(l, h, p.forme, p.rayon_mm * l / p.largeur_mm) & opaque
    teintes, correspondance = C.fusion(p.couleurs, p.ordre_auto)
    if seuils_imposes is not None and len(seuils_imposes) == len(p.couleurs) - 1:
        s = list(seuils_imposes)
    else:
        s = C.seuils(valeurs, valide, len(p.couleurs), p.repartition_egale)
    indices = C.quantifier(valeurs, valide, s, correspondance)
    hist = np.bincount(indices.ravel(), minlength=256)
    comptes = [int(hist[i]) for i in range(len(teintes))]
    return CarteTableau(l, h, p.largeur_mm / l, p.largeur_mm, p.hauteur_mm, indices, teintes, s, comptes)


def carte_impression(src, r, p, seuils_imposes=None):
    """Carte d'impression à la finesse demandée (dimensions de ``dimensions``)."""
    d = dimensions(p.largeur_mm, p.hauteur_mm, p.pixel_mm)
    c = carte(src, r, replace(p, largeur_mm=d.largeur_mm, hauteur_mm=d.hauteur_mm), d.l, d.h, seuils_imposes)
    if c.pixels_pleins == 0:
        raise ValueError("La carte est vide : rien à imprimer.")
    return c


def carte_apercu(src, r, p, d):
    """Carte d'aperçu (≤ APERCU_PIXELS_MAX pixels) dont l'écran de Gift reprend les seuils à l'export."""
    reduction = min(1.0, math.sqrt(APERCU_PIXELS_MAX / (d.l * d.h)))
    l, h = max(8, int(d.l * reduction)), max(8, int(d.h * reduction))
    return carte(src, r, replace(p, largeur_mm=d.largeur_mm, hauteur_mm=d.hauteur_mm), l, h)


def rendre(c):
    """Carte → image RGBA (couleurs des filaments, vide transparent)."""
    teintes = np.zeros((256, 4), np.uint8)
    for i, t in enumerate(c.couleurs):
        teintes[i, :3] = C.rgb(t.hex)
        teintes[i, 3] = 255
    return teintes[c.indices]


def conseils(p):
    return [
        "Ouvre le 3MF dans Bambu Studio ou OrcaSlicer : chaque couleur arrive déjà sur son filament (N° 1 = la plus sombre).",
        "Place les bobines dans l'AMS dans le même ordre que les couleurs du tableau.",
        "Hauteur de couche 0,2 mm (0,1 mm pour la couche couleur si elle fait moins de 0,4 mm), remplissage 15 %.",
        ("Face contre le plateau : la couche couleur est imprimée en premier, sur une plaque lisse, et se lit à l'endroit "
         "une fois retournée.") if p.face_dessous else
        "Couche couleur sur le dessus : relief léger au toucher ; coche « Face contre le plateau » pour une surface lisse.",
        "Une buse 0,4 mm suit des pixels de 0,2 mm ; préfère 0,3 à 0,4 mm pour une buse 0,6 mm.",
    ]
