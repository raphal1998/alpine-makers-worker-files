# -*- coding: utf-8 -*-
"""Maillage de la carte des couleurs, au pixel près (copie de alpine_tools/tableau_couleur/maillage.py) : portage
vectorisé (numpy) de Gift TableauMaillage.kt.

Chaque couleur est découpée en rectangles (plages d'une ligne fusionnées tant que les lignes suivantes ont exactement
la même plage). Les sommets d'une ligne horizontale L sont tous les changements de couleur des rangées L − 1 et L :
deux rectangles voisins partagent les mêmes sommets (aucune jonction en T). Chaque rectangle se triangule « en
fermeture éclair » entre ses deux bords (même règle d'égalité que la boucle Kotlin : à abscisse égale, le bord du
bas avance d'abord). Parois sur les bords de la couleur, dessous plat : chaque pièce est fermée. Un coin pincé (damier
2 × 2) dédouble son sommet pour le rectangle situé sous la ligne.
Repère : X vers la droite, Y vers le haut (ligne 0 en haut), Z vers le haut ; ``miroir`` retourne de gauche à droite.
Les triangles et sommets sont les mêmes que dans Gift ; seul leur ordre dans les listes diffère.
"""
import math
from dataclasses import dataclass, replace

import numpy as np

from . import carte as K
from .couleurs import VIDE

# Au-delà, le fichier devient trop lourd pour un trancheur : la finesse est élargie (TableauMaillage.TRIANGLES_MAX).
TRIANGLES_MAX = 5_000_000


@dataclass
class Maille:
    """Pièce fermée : sommets (n, 3) mm, triangles (m, 3) orientés vers l'extérieur ; ``u``, ``ligne``, ``niveau`` :
    indices entiers de chaque sommet (colonne de coin, ligne de coin, 0 = dessous / 1 = dessus)."""
    sommets: np.ndarray
    triangles: np.ndarray
    u: np.ndarray
    ligne: np.ndarray
    niveau: np.ndarray


def _plages(debuts, longueurs):
    """Indices debuts[r] .. debuts[r] + longueurs[r] − 1 mis bout à bout, et le rang r de chacun."""
    longueurs = np.asarray(longueurs, np.int64)
    total = int(longueurs.sum())
    rang = np.repeat(np.arange(len(longueurs)), longueurs)
    decal = np.arange(total, dtype=np.int64) - np.repeat(np.cumsum(longueurs) - longueurs, longueurs)
    return rang, np.repeat(np.asarray(debuts, np.int64), longueurs) + decal


class _Structure:
    """Rectangles, sommets de chaque ligne et segments des bords d'une pièce (masque booléen (H, W))."""

    def __init__(self, masque):
        self.masque = masque = np.asarray(masque, bool)
        self.H, self.W = H, W = masque.shape
        self.pas = pas = W + 1
        bord = np.zeros((H, W + 2), bool)
        bord[:, 1:-1] = masque
        change = bord[:, 1:] != bord[:, :-1]                         # (H, W + 1) : transitions de chaque rangée
        lignes, colonnes = np.nonzero(change)
        debut = bord[lignes, colonnes + 1]
        v, u0, u1 = lignes[debut], colonnes[debut], colonnes[~debut]
        ordre = np.lexsort((v, u1, u0))
        v, u0, u1 = v[ordre], u0[ordre], u1[ordre]
        suite = np.zeros(len(v), bool)
        suite[1:] = (u0[1:] == u0[:-1]) & (u1[1:] == u1[:-1]) & (v[1:] == v[:-1] + 1)
        departs = np.flatnonzero(~suite)
        fins = np.append(departs[1:], len(v)) - 1
        self.u0 = u0[departs].astype(np.int64)
        self.u1 = u1[departs].astype(np.int64)
        self.v0 = v[departs].astype(np.int64)
        self.v1 = (v[fins] + 1).astype(np.int64)
        sur_ligne = np.zeros((H + 1, pas), bool)
        sur_ligne[:-1] |= change
        sur_ligne[1:] |= change
        self.cles = cles = np.flatnonzero(sur_ligne).astype(np.int64)  # clé = L × pas + x, triées
        ia = np.searchsorted(cles, self.v0 * pas + self.u0)
        ib = np.searchsorted(cles, self.v0 * pas + self.u1)
        ja = np.searchsorted(cles, self.v1 * pas + self.u0)
        jb = np.searchsorted(cles, self.v1 * pas + self.u1)
        self.rh, self.kh = _plages(ia, ib - ia)                       # segments du bord du haut
        self.rb, self.kb = _plages(ja, jb - ja)                       # segments du bord du bas
        xh = cles[self.kh] % pas
        vh = self.v0[self.rh]
        self.libre_haut = (vh == 0) | ~masque[np.maximum(vh - 1, 0), np.minimum(xh, W - 1)]
        xb = cles[self.kb] % pas
        vb = self.v1[self.rb]
        self.libre_bas = (vb == H) | ~masque[np.minimum(vb, H - 1), np.minimum(xb, W - 1)]

    def triangles(self):
        return (2 * (len(self.kh) + len(self.kb))
                + 2 * (2 * len(self.u0) + int(self.libre_haut.sum()) + int(self.libre_bas.sum())))


def _masque(indices, valeur):
    return indices != VIDE if valeur < 0 else indices == valeur


def compter_masque(masque):
    masque = np.asarray(masque, bool)
    return 0 if not masque.any() else _Structure(masque).triangles()


def compter(c, valeur):
    """Nombre exact de triangles de la pièce ``valeur`` (−1 = plaque : tout pixel non vide)."""
    return compter_masque(_masque(c.indices, valeur))


def compter_tout(c):
    """Total des triangles du tableau (plaque + couleurs présentes)."""
    n = compter(c, -1)
    for i in range(len(c.couleurs)):
        if c.comptes[i] > 0:
            n += compter(c, i)
    return n


def mailler(indices, valeur, z0, z1, pas_x, pas_y, miroir, limite=TRIANGLES_MAX):
    """Pièce fermée de la valeur ``valeur`` de la carte ``indices`` (h, l)."""
    masque = _masque(np.asarray(indices), valeur)
    vide = np.zeros(0, np.int64)
    if not masque.any():
        return Maille(np.zeros((0, 3)), np.zeros((0, 3), np.int64), vide, vide, vide)
    st = _Structure(masque)
    n = st.triangles()
    if n > limite:
        raise ValueError(f"Tableau trop morcelé (plus de {K.entier_fr(limite)} triangles).")
    H, W, pas, cles = st.H, st.W, st.pas, st.cles
    s = -1 if miroir else 1
    plein = np.pad(masque, 1)
    damier = ((plein[:-1, :-1] == plein[1:, 1:]) & (plein[:-1, 1:] == plein[1:, :-1])
              & (plein[:-1, :-1] != plein[:-1, 1:])).ravel()                  # index = clé L × pas + x

    def haut(cle):          # sommet d'un bord du haut de rectangle : dédoublé sur un coin pincé
        return cle * 2 + damier[cle]

    def bas(cle):
        return cle * 2

    # Faces : segments du bord du haut (sommet opposé : dernier du bas d'abscisse ≤), puis du bas (dernier du haut <).
    a1, b1 = cles[st.kh], cles[st.kh + 1]
    c1 = cles[np.searchsorted(cles, st.v1[st.rh] * pas + b1 % pas, side="right") - 1]
    a2, b2 = cles[st.kb], cles[st.kb + 1]
    c2 = cles[np.searchsorted(cles, st.v0[st.rb] * pas + b2 % pas, side="left") - 1]
    ka, kb_, kc = np.concatenate((a1, a2)), np.concatenate((b1, b2)), np.concatenate((c1, c2))
    va_, vb_, vc_ = (np.concatenate((haut(a1), bas(a2))), np.concatenate((haut(b1), bas(b2))),
                     np.concatenate((bas(c1), haut(c2))))
    ua, ub, uc = ka % pas, kb_ % pas, kc % pas
    la, lb, lc = ka // pas, kb_ // pas, kc // pas
    cr = (s * (ub - ua)) * (-(lc - la)) - (-(lb - la)) * (s * (uc - ua))     # aire signée dans (s·u, −v)
    p = np.where(cr > 0, vb_, vc_)
    q = np.where(cr > 0, vc_, vb_)
    dessus = np.column_stack((va_ * 2 + 1, p * 2 + 1, q * 2 + 1))
    dessous = np.column_stack((va_ * 2, q * 2, p * 2))
    # Parois : gauche, droite, segments du haut sans matière au-dessus, du bas sans matière au-dessous.
    kh_l, kh_r = st.v0 * pas + st.u0, st.v0 * pas + st.u1
    kb_l, kb_r = st.v1 * pas + st.u0, st.v1 * pas + st.u1
    h_a, h_b = a1[st.libre_haut], b1[st.libre_haut]
    b_a, b_b = a2[st.libre_bas], b2[st.libre_bas]
    p0 = np.concatenate((haut(kh_l), haut(kh_r), haut(h_a), bas(b_a)))
    q0 = np.concatenate((bas(kb_l), bas(kb_r), haut(h_b), bas(b_b)))
    kp = np.concatenate((kh_l, kh_r, h_a, b_a))
    kq = np.concatenate((kb_l, kb_r, h_b, b_b))
    nr, nh, nb = len(st.u0), len(h_a), len(b_a)
    ou = np.concatenate((np.full(nr, -1), np.full(nr, 1), np.zeros(nh + nb, np.int64)))
    ov = np.concatenate((np.zeros(2 * nr, np.int64), np.full(nh, -1), np.full(nb, 1)))
    dx = s * (kq % pas - kp % pas)
    dy = -(kq // pas - kp // pas)
    ox, oy = s * ou, -ov
    garde = dy * ox - dx * oy > 0           # matière à gauche de p → q : normale droite vers l'extérieur
    pp, qq = np.where(garde, p0, q0), np.where(garde, q0, p0)
    parois = np.concatenate((np.column_stack((pp * 2, qq * 2, qq * 2 + 1)), np.column_stack((pp * 2, qq * 2 + 1, pp * 2 + 1))))
    codes = np.concatenate((dessus, dessous, parois))
    uniques, inverse = np.unique(codes, return_inverse=True)
    niveau = uniques & 1
    cle = uniques >> 2
    u, ligne = cle % pas, cle // pas
    x = (W - u if miroir else u) * pas_x
    y = (H - ligne) * pas_y
    z = np.where(niveau == 1, z1, z0)
    sommets = np.column_stack((x.astype(np.float64), y.astype(np.float64), z.astype(np.float64)))
    tris = inverse.reshape(-1, 3).astype(np.int64)
    return Maille(sommets, tris, u, ligne, niveau)


@dataclass
class Preparation:
    carte: object
    triangles: int
    avertissement: str


def preparer(src, r, p, seuils, limite=TRIANGLES_MAX, progress=None):
    """Carte d'impression dont le maillage tient dans ``limite`` triangles : sinon le pixel est élargi (jusqu'à 1 mm) et
    un avertissement l'explique. Les seuils de l'aperçu sont repris (même répartition des couleurs)."""
    pp = p
    avertissement = None
    for essai in range(8):
        if progress:
            progress(essai)
        c = K.carte_impression(src, r, pp, seuils)
        n = compter_tout(c)
        if n <= limite:
            return Preparation(c, n, avertissement)
        nouveau = math.ceil(c.pixel_mm * math.sqrt(n / limite) * 1.08 * 100) / 100
        if c.pixel_mm >= 1.0:
            raise ValueError("Tableau trop morcelé même avec des pixels de 1 mm : réduis le nombre de couleurs ou la taille.")
        pp = replace(pp, pixel_mm=min(1.0, max(nouveau, c.pixel_mm + 0.01)))
        avertissement = (f"Pixel élargi à {pp.pixel_mm:.2f} mm pour garder un fichier raisonnable "
                         f"({K.entier_fr(limite)} triangles au plus).").replace(".", ",", 1)
    raise ValueError("Tableau trop morcelé : réduis le nombre de couleurs, la taille ou la finesse.")
