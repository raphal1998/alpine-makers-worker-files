# -*- coding: utf-8 -*-
"""Pièces du puzzle imprimées en 3D, portage d'Impression3D (Puzzle.kt, Alpine Gift 1.0.10).

Une pièce fermée par corps : contour de la pièce découpée réduit du jeu tout autour (champ de distance signée décalé),
prisme de l'épaisseur choisie ; prénom en relief sur sa pièce (corps unique pour le STL, pièce « Prénom » séparée pour
le 3MF) ; plateau de rangement à bord posé à droite du puzzle.
"""
from . import geometrie as G
from . import kotlin as K
from . import maillage as M
from .decoupe import NOM_PRENOM

TRIANGLES_MAX = 1_500_000        # Lithophanie.TRIANGLES_MAX d'Alpine Gift


class Resultat3D:
    def __init__(self, pieces, tout, avertissements):
        self.pieces = pieces          # [(nom, Maillage)] : pièces, relief du prénom, plateau
        self.tout = tout              # tout en un maillage (corps séparés, chacun fermé)
        self.avertissements = avertissements

    @property
    def triangles(self):
        return self.tout.nb_triangles

    @staticmethod
    def filament(nom):
        return 1 if nom == NOM_PRENOM else 0


def verifier(p3, avec_prenom):
    """Bornes de Puzzle.pieces3D."""
    if not 2.0 <= p3["epaisseur_mm"] <= 12.0:
        raise ValueError("Épaisseur des pièces de 2 à 12 mm.")
    if not 0.05 <= p3["jeu_mm"] <= 0.5:
        raise ValueError("Jeu de 0,05 à 0,5 mm par côté.")
    if avec_prenom and not 0.4 <= p3["relief_prenom_mm"] <= 3.0:
        raise ValueError("Relief du prénom de 0,4 à 3 mm.")
    if p3["plateau"] and not (2.0 <= p3["bord_plateau_mm"] <= 15.0 and 0.6 <= p3["fond_plateau_mm"] <= 4.0):
        raise ValueError("Bord du plateau de 2 à 15 mm, fond de 0,6 à 4 mm.")


def pieces3d(d, p3, progress=None, triangles_max=TRIANGLES_MAX):
    """Puzzle.pieces3D : ``p3`` = epaisseur_mm, jeu_mm, relief_prenom_mm, plateau, bord_plateau_mm, fond_plateau_mm."""
    verifier(p3, bool(d.prenom))
    avert = list(d.avertissements)
    pieces = []
    tout = M.Maillage()
    for n, pc in enumerate(d.pieces):
        if progress:
            progress(0.9 * n / len(d.pieces), f"Pièce {n + 1} sur {len(d.pieces)}")
        _piece(d, pc, p3, pieces, tout)
        if tout.nb_triangles > triangles_max:
            raise ValueError(f"Puzzle trop lourd (plus de {K.entier(triangles_max)} triangles) : moins de pièces.")
    if p3["plateau"]:
        _plateau(d, p3, pieces, tout)
    if progress:
        progress(1.0, "Pièces prêtes")
    return Resultat3D(pieces, tout, avert)


def _piece(d, pc, p3, pieces, tout):
    bt = G.boite_une(pc.contour)
    pas = min(max(min(bt[2] - bt[0], bt[3] - bt[1]) / 400, 0.05), 0.1)
    grille = G.Champ.sur(bt, pas, 1.0)
    jeu = p3["jeu_mm"]
    texte = bool(pc.prenom and d.prenom)
    f = G.distance_signee(grille, [pc.contour], decalage_max=jeu + (0.8 + 0.2 if texte else 0.2)).decaler(-jeu)
    forme = G.regions(f)
    nom = f"Pièce {pc.colonne + 1}-{d.lignes - pc.ligne}"
    if not forme:
        raise ValueError("Pièce vide : jeu trop grand.")
    e = p3["epaisseur_mm"]
    if texte:
        f_texte = G.distance_signee(grille, d.prenom, decalage_max=0.2)
        motif = G.regions(f_texte.inter(f.decaler(-0.8)))
        relief = e + p3["relief_prenom_mm"]
        m = M.Maillage()
        M.socle_avec_motif(m, forme, motif, e, relief)
        tout.fusionner(m)
        base = M.Maillage()
        M.prisme(base, forme, 0.0, e)
        pieces.append((nom, base))
        if motif:
            r = M.Maillage()
            M.prisme(r, motif, e, relief)
            pieces.append((NOM_PRENOM, r))
    else:
        m = M.Maillage()
        M.prisme(m, forme, 0.0, e)
        tout.fusionner(m)
        pieces.append((nom, m))


def _plateau(d, p3, pieces, tout):
    """Plateau à bord posé à droite du puzzle : fond plein, logement du puzzle (jeu compris), bord."""
    import numpy as np
    b, j = p3["bord_plateau_mm"], p3["jeu_mm"]
    ox = d.largeur_mm + 2 * b + 10.0

    def rect(x0, y0, x1, y1):
        return (np.array([[x0 + ox, y0], [x1 + ox, y0], [x1 + ox, y1], [x0 + ox, y1]], dtype=np.float64), [])

    socle = [rect(-b - j, -b - j, d.largeur_mm + b + j, d.hauteur_mm + b + j)]
    logement = [rect(-j, -j, d.largeur_mm + j, d.hauteur_mm + j)]
    m = M.Maillage()
    M.socle_avec_motif(m, socle, logement, p3["fond_plateau_mm"] + 0.75 * p3["epaisseur_mm"], p3["fond_plateau_mm"])
    tout.fusionner(m)
    pieces.append(("Plateau", m))
