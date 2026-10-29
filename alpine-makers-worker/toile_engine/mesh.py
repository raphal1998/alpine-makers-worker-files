# -*- coding: utf-8 -*-
"""Maillage d'une toile multicolore à partir de la carte des couleurs (composant « toile-engine »).

Carte : tableau 8 bits (lignes × colonnes), valeur = index de couleur 0..N-1, 255 = vide. Les contours suivent les
bords des pixels : coins entiers (u = colonne, v = ligne), si bien que deux couleurs voisines partagent exactement
leur frontière, sans trou ni recouvrement. Repère du modèle en mm : X vers la droite, Y vers le haut (la ligne 0 de
la carte est en haut), Z vers le haut ; ``miroir`` (face contre le plateau) retourne la carte de gauche à droite.

Triangulation par balayage, en temps linéaire (numpy) : chaque couleur est découpée en rectangles (plages d'une
ligne, fusionnées tant que les lignes suivantes ont exactement la même plage). Les sommets d'un bord horizontal sont
tous les changements de couleur des deux lignes qui le bordent : les rectangles voisins partagent donc les mêmes
sommets (aucune jonction en T) et chaque rectangle se triangule en « échelle » entre ses deux bords. Les parois sont
posées sur les arêtes du dessus qui n'ont pas de jumelle. (Une triangulation par contours + earcut, essayée d'abord,
devenait quadratique avec les milliers de trous d'une image quantifiée.)

Pièces fermées : dessus + dessous + parois, chaque arête partagée par exactement deux triangles. Là où deux pixels
d'une même couleur ne se touchent que par un coin (damier), le sommet est dédoublé par quadrant : chaque moitié reste
une coque fermée, aucune arête n'est partagée par quatre triangles.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import ToileEngineError

VIDE = 255
# Code d'un sommet : ((v * (W + 1) + u) * 5 + quadrant) * 2 + niveau (0 = dessous, 1 = dessus).
QUADRANTS = 5
# Au-delà, le 3MF dépasserait le gigaoctet et les trancheurs ne l'ouvriraient plus : refus clair.
MAX_TRIANGLES = 20_000_000


@dataclass
class Piece:
    """Une pièce fermée du modèle (plaque de base ou couleur de la couche image)."""
    nom: str
    couleur: int
    hex: str
    pixels: int
    z0: float
    z1: float
    sommets: np.ndarray            # (n, 3) float64, mm
    triangles: np.ndarray          # (m, 3) int64, orientés vers l'extérieur
    plaque: bool = False

    @property
    def epaisseur(self) -> float:
        return self.z1 - self.z0


@dataclass
class Toile:
    pieces: list = field(default_factory=list)
    largeur_px: int = 0
    hauteur_px: int = 0
    pas_x: float = 0.0
    pas_y: float = 0.0
    avertissements: list = field(default_factory=list)

    @property
    def triangles(self) -> int:
        return int(sum(len(piece.triangles) for piece in self.pieces))


class Repere:
    """Passage des coins entiers (u, v) de la carte aux millimètres, et orientation dans le plan XY."""

    def __init__(self, largeur, hauteur, pas_x, pas_y, miroir):
        self.largeur, self.hauteur = int(largeur), int(hauteur)
        self.pas_x, self.pas_y, self.miroir = float(pas_x), float(pas_y), bool(miroir)
        # (u, v) → (x, y) inverse l'orientation (Y vers le haut) ; le miroir la rétablit.
        self.signe = 1 if self.miroir else -1

    def code(self, u, v, quadrant, niveau):
        return ((np.asarray(v, np.int64) * (self.largeur + 1) + np.asarray(u, np.int64)) * QUADRANTS
                + np.asarray(quadrant, np.int64)) * 2 + np.asarray(niveau, np.int64)

    def decoder(self, codes, z0, z1):
        codes = np.asarray(codes, np.int64)
        niveau = codes % 2
        coin = codes // 2 // QUADRANTS
        u, v = coin % (self.largeur + 1), coin // (self.largeur + 1)
        x = (self.largeur - u if self.miroir else u) * self.pas_x
        y = (self.hauteur - v) * self.pas_y
        return np.column_stack((x.astype(np.float64), y.astype(np.float64), np.where(niveau == 1, z1, z0).astype(np.float64)))


def _quadrant(du, dv):
    """Quadrant (1..4) d'une direction par rapport à un coin, dans le repère de la carte (u à droite, v en bas)."""
    return 1 + (np.asarray(du) > 0).astype(np.int64) + 2 * (np.asarray(dv) > 0).astype(np.int64)


def rectangles(masque):
    """Masque booléen (H, W) → rectangles (u0, u1, v0, v1) : plages d'une ligne fusionnées verticalement tant que
    les lignes suivantes ont exactement la même plage. Les rectangles d'une même bande ne se touchent jamais."""
    hauteur, largeur = masque.shape
    bord = np.zeros((hauteur, largeur + 2), bool)
    bord[:, 1:-1] = masque
    change = bord[:, 1:] != bord[:, :-1]                                  # (H, W + 1) : transitions par ligne
    lignes, colonnes = np.nonzero(change)
    debut = bord[lignes, colonnes + 1]                                    # vrai : début de plage
    v, u0, u1 = lignes[debut], colonnes[debut], colonnes[~debut]          # même nombre de débuts et de fins par ligne
    # Tri par (u0, u1, v) : une plage prolonge la précédente si même (u0, u1) à la ligne juste au-dessus.
    ordre = np.lexsort((v, u1, u0))
    v, u0, u1 = v[ordre], u0[ordre], u1[ordre]
    suite = np.zeros(len(v), bool)
    suite[1:] = (u0[1:] == u0[:-1]) & (u1[1:] == u1[:-1]) & (v[1:] == v[:-1] + 1)
    departs = np.flatnonzero(~suite)
    fins = np.append(departs[1:], len(v)) - 1
    return u0[departs], u1[departs], v[departs], v[fins] + 1, change


def mailler(repere, masque, z0, z1):
    """Masque booléen → (sommets mm, triangles orientés vers l'extérieur, pixels) d'une pièce fermée."""
    masque = np.asarray(masque, bool)
    pixels = int(np.count_nonzero(masque))
    if not pixels:
        return np.zeros((0, 3), np.float64), np.zeros((0, 3), np.int64), 0
    hauteur, largeur = masque.shape
    pas = largeur + 1
    r_u0, r_u1, r_v0, r_v1, change = rectangles(masque)
    # Sommets de chaque ligne horizontale L (0..H) : transitions des lignes L - 1 et L, triés par clé L × pas + u.
    sur_ligne = np.zeros((hauteur + 1, pas), bool)
    sur_ligne[:-1] |= change
    sur_ligne[1:] |= change
    cles = np.flatnonzero(sur_ligne).astype(np.int64)
    triangles = []
    for haut_v, bas_v, sens in ((r_v0, r_v1, 1), (r_v1, r_v0, -1)):
        # Segments entre deux sommets consécutifs d'une ligne, rattachés au rectangle dont ce bord les contient.
        debuts = haut_v * pas + r_u0
        ordre = np.argsort(debuts)
        debuts, fins = debuts[ordre], (haut_v * pas + r_u1)[ordre]
        autre = (bas_v * pas)[ordre]
        a, c = cles[:-1], cles[1:]
        rang = np.searchsorted(debuts, a, side="right") - 1
        valide = (rang >= 0) & (c <= fins[np.maximum(rang, 0)]) & (a // pas == c // pas)
        a, c, rang = a[valide], c[valide], rang[valide]
        # Sommet opposé sur l'autre bord : prédécesseur (bord du haut) ou prédécesseur au sens large (bord du bas)
        # de l'extrémité du segment — le balayage « échelle » qui pave exactement le rectangle.
        cible = autre[rang] + c % pas
        if sens > 0:
            p = cles[np.searchsorted(cles, cible, side="left") - 1]
            tri = np.column_stack((a, c, p))
        else:
            p = cles[np.searchsorted(cles, cible, side="right") - 1]
            tri = np.column_stack((c, a, p))
        triangles.append(tri)
    triple = np.concatenate(triangles)                                    # clés L × pas + u, directes dans (u, v)
    if repere.signe < 0:
        triple = triple[:, ::-1]
    u, v = triple % pas, triple // pas
    # Coins pincés (damier 2 × 2 de la couleur) : quadrant du triangle vu depuis le coin (son angle y est contenu).
    plein = np.pad(masque, 1)
    damier = (plein[:-1, :-1] == plein[1:, 1:]) & (plein[:-1, 1:] == plein[1:, :-1]) & (plein[:-1, :-1] != plein[:-1, 1:])
    quadrants = np.zeros(triple.shape, np.int64)
    if damier.any():
        pinces = damier[v, u]
        centre_u, centre_v = u.sum(axis=1, keepdims=True), v.sum(axis=1, keepdims=True)
        quadrants = np.where(pinces, _quadrant(centre_u - 3 * u, centre_v - 3 * v), 0)
    dessus = repere.code(u, v, quadrants, 1)
    # Parois : arêtes orientées du dessus sans jumelle inverse (matière à gauche dans XY → normale extérieure).
    aretes = np.concatenate((dessus[:, [0, 1]], dessus[:, [1, 2]], dessus[:, [2, 0]]))
    maximum = int(dessus.max()) + 1
    directes = aretes[:, 0] * maximum + aretes[:, 1]
    bordure = ~np.isin(aretes[:, 1] * maximum + aretes[:, 0], directes)
    p1, q1 = aretes[bordure, 0], aretes[bordure, 1]
    p0, q0 = p1 - 1, q1 - 1
    codes = np.concatenate((dessus, (dessus - 1)[:, ::-1], np.column_stack((p0, q0, q1)), np.column_stack((p0, q1, p1))))
    uniques, inverse = np.unique(codes, return_inverse=True)
    return repere.decoder(uniques, z0, z1), inverse.reshape(-1, 3).astype(np.int64), pixels


def construire(carte, largeur_mm, hauteur_mm, epaisseur_base_mm, epaisseur_image_mm, couleurs, couleur_base,
               face_dessous=False):
    """Carte des couleurs → Toile (plaque de base + une pièce par couleur présente)."""
    carte = np.asarray(carte)
    if carte.ndim != 2 or carte.dtype != np.uint8:
        raise ToileEngineError("Carte des couleurs : image 8 bits en niveaux de gris attendue.", 400, "CARTE_INVALIDE")
    hauteur, largeur = carte.shape
    repere = Repere(largeur, hauteur, float(largeur_mm) / largeur, float(hauteur_mm) / hauteur, face_dessous)
    plein = carte != VIDE
    if not plein.any():
        raise ToileEngineError("La carte des couleurs est vide : rien à imprimer.", 400, "CARTE_VIDE")
    base_mm, image_mm = float(epaisseur_base_mm), float(epaisseur_image_mm)
    if face_dessous:
        image_z, plaque_z = (0.0, image_mm), (image_mm, image_mm + base_mm)
    else:
        plaque_z, image_z = (0.0, base_mm), (base_mm, base_mm + image_mm)
    toile = Toile(largeur_px=largeur, hauteur_px=hauteur, pas_x=repere.pas_x, pas_y=repere.pas_y)
    sommets, triangles, pixels = mailler(repere, plein, *plaque_z)
    toile.pieces.append(Piece("Plaque de base", int(couleur_base), couleurs[couleur_base]["hex"], pixels, *plaque_z,
                              sommets, triangles, plaque=True))
    presentes = np.bincount(carte[plein], minlength=256)
    noms = set()
    for index, couleur in enumerate(couleurs):
        if not presentes[index]:
            toile.avertissements.append(f"Couleur « {couleur['nom']} » absente de la carte : aucune pièce.")
            continue
        nom = couleur["nom"]
        if nom in noms or nom == "Plaque de base":
            nom = f"{nom} ({index + 1})"
        noms.add(nom)
        sommets, triangles, pixels = mailler(repere, carte == index, *image_z)
        toile.pieces.append(Piece(nom, index, couleur["hex"], pixels, *image_z, sommets, triangles))
        if toile.triangles > MAX_TRIANGLES:
            raise ToileEngineError("Toile trop morcelée pour un fichier imprimable : réduis le nombre de couleurs, la taille "
                                   "ou la finesse.", 422, "MAILLAGE_TROP_LOURD")
    return toile


def volume_maillage(sommets, triangles):
    """Volume d'une surface fermée orientée (théorème de la divergence), en mm³."""
    if not len(triangles):
        return 0.0
    a, b, c = sommets[triangles[:, 0]], sommets[triangles[:, 1]], sommets[triangles[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)
