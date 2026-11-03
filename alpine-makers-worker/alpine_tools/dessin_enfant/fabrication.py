# -*- coding: utf-8 -*-
"""Fabrication des objets tirés du dessin (portage de Fabrication, DessinEnfant.kt d'Alpine Gift 1.0.11) :
emporte-pièce (rebord, paroi, tranchant ; inversé en miroir), porte-clés et magnet (silhouette, traits en relief, anneau
ou logement d'aimant), tampon (traits en relief inversés sur une plaque). Maillages fermés à l'échelle (mm, z = 0 sur
le plateau)."""
import math

import numpy as np

from . import contours as G
from .champ import F32
from .lecture import contours, format_fr
from .maillage import Maillage, miroir, paroi, prisme, socle_avec_motif, face

EMPORTE_PIECE, PORTE_CLES, MAGNET, TAMPON = "emporte_piece", "porte_cles", "magnet", "tampon"
SORTIES = {
    EMPORTE_PIECE: ("Emporte-pièce", "Paroi coupante qui suit le contour du dessin, rebord pour appuyer ; inversé (miroir) "
                                     "pour que le biscuit soit à l'endroit."),
    PORTE_CLES: ("Porte-clés", "Silhouette du dessin, traits en relief, anneau."),
    MAGNET: ("Magnet", "Silhouette et traits en relief, logement d'aimant au dos."),
    TAMPON: ("Tampon", "Traits en relief inversés (miroir) sur une plaque."),
}
ECART_BORD = 0.6   # jeu entre les traits et le bord du socle (mm)
# Borne propre au Worker (l'application n'en a pas) : au-delà, la triangulation des traits en relief dépasse la minute.
LIMITE_CONTOURS = 2000
LIMITE_POINTS = 150_000
MIROIR_EMPORTE_PIECE = "Le modèle est inversé (miroir) : une fois retourné pour couper, le biscuit a le sens du dessin."


class ParamFabrication:
    def __init__(self, sortie=EMPORTE_PIECE, hauteur_coupe_mm=14.0, paroi_mm=1.6, tranchant_mm=0.7,
                 hauteur_tranchant_mm=3.0, rebord_mm=4.0, hauteur_rebord_mm=2.0, socle_mm=3.0, relief_mm=1.2,
                 marge_mm=2.5, diametre_anneau_mm=4.5, paroi_anneau_mm=2.5, diametre_aimant_mm=10.0,
                 profondeur_aimant_mm=2.2):
        if sortie not in SORTIES:
            raise ValueError("Objet inconnu.")
        self.sortie = sortie
        self.hauteur_coupe_mm, self.paroi_mm, self.tranchant_mm = float(hauteur_coupe_mm), float(paroi_mm), float(tranchant_mm)
        self.hauteur_tranchant_mm, self.rebord_mm, self.hauteur_rebord_mm = (float(hauteur_tranchant_mm), float(rebord_mm),
                                                                            float(hauteur_rebord_mm))
        self.socle_mm, self.relief_mm, self.marge_mm = float(socle_mm), float(relief_mm), float(marge_mm)
        self.diametre_anneau_mm, self.paroi_anneau_mm = float(diametre_anneau_mm), float(paroi_anneau_mm)
        self.diametre_aimant_mm, self.profondeur_aimant_mm = float(diametre_aimant_mm), float(profondeur_aimant_mm)


class ResultatDessin:
    def __init__(self, sortie, tout, pieces, contours_objet, largeur, profondeur, hauteur, changement, avertissements,
                 controle):
        self.sortie, self.tout, self.pieces = sortie, tout, pieces
        self.contours = contours_objet
        self.largeur_mm, self.profondeur_mm, self.hauteur_mm = largeur, profondeur, hauteur
        self.changement_couleur = changement
        self.avertissements = avertissements
        self.controle = controle

    @property
    def libelle(self):
        return SORTIES[self.sortie][0]


def conseils(f):
    if f.sortie == EMPORTE_PIECE:
        return ["Imprime rebord sur le plateau, tranchant vers le haut, sans support.",
                MIROIR_EMPORTE_PIECE,
                "PLA ou PETG, 3 parois, remplissage 20 % ; lave à la main à l'eau tiède (pas de lave-vaisselle).",
                "Pour un usage alimentaire régulier, un filament certifié contact alimentaire est préférable."]
    if f.sortie in (PORTE_CLES, MAGNET):
        return ["Imprime à plat, socle sur le plateau ; change de filament à la hauteur indiquée pour des traits d'une autre couleur.",
                "Hauteur de couche 0,16 mm, remplissage 20 %."
                + (" Colle l'aimant dans son logement (colle cyanoacrylate)." if f.sortie == MAGNET else "")]
    return ["Imprime à plat, traits vers le haut ; les traits sont inversés pour que l'empreinte soit à l'endroit.",
            "TPU (souple) pour une empreinte nette, ou PLA collé sur un bloc de bois ; encre à tampon lavable."]


def fabriquer(a, f, progress=None):
    """Fabrique l'objet. ValueError si les réglages ne conviennent pas au dessin."""
    fab = _Fabrication(a, f, progress or (lambda x, m="": None))
    if f.sortie == EMPORTE_PIECE:
        return fab.emporte_piece()
    if f.sortie == TAMPON:
        return fab.tampon()
    return fab.plaque()


def _dans(v, lo, hi):
    return lo <= v <= hi


def _cercle(cx, cy, r, n=48):
    a = [2 * math.pi * k / n for k in range(n)]
    return G.boucle([cx + r * math.cos(t) for t in a], [cy + r * math.sin(t) for t in a])


class _Fabrication:
    def __init__(self, a, f, progress):
        self.a, self.f, self.progress = a, f, progress
        self.avert = list(a.avertissements)
        self.g = a.grille

    def _regions(self, c):
        return G.regrouper(contours(c))

    def _borner_traits(self):
        n = len(self.a.contours_traits)
        points = sum(len(b) for b in self.a.contours_traits)
        if n > LIMITE_CONTOURS or points > LIMITE_POINTS:
            raise ValueError(f"Dessin trop détaillé pour des traits en relief ({n} contours, {LIMITE_CONTOURS} au plus) : "
                             "augmente « Nettoyage des taches » ou « Raccord des traits interrompus », ou choisis l'emporte-pièce.")

    @staticmethod
    def _exiger(condition, message):
        if not condition:
            raise ValueError(message)

    # ---------------------------------------------------------------- emporte-pièce
    def emporte_piece(self):
        f, a, ex = self.f, self.a, self._exiger
        ex(_dans(f.hauteur_coupe_mm, 6.0, 40.0), "Hauteur de l'emporte-pièce de 6 à 40 mm.")
        ex(_dans(f.tranchant_mm, 0.4, 1.5) and _dans(f.paroi_mm, f.tranchant_mm + 0.2, 4.0),
           "Tranchant de 0,4 à 1,5 mm, paroi plus épaisse que le tranchant (4 mm au plus).")
        ex(_dans(f.rebord_mm, f.paroi_mm + 0.5, 10.0), "Rebord plus large que la paroi (10 mm au plus).")
        ex(_dans(f.hauteur_rebord_mm, 0.8, 5.0) and _dans(f.hauteur_tranchant_mm, 0.5, 10.0),
           "Rebord de 0,8 à 5 mm de haut, tranchant de 0,5 à 10 mm.")
        ex(f.hauteur_rebord_mm + f.hauteur_tranchant_mm < f.hauteur_coupe_mm - 1.0, "Hauteur trop faible pour le rebord et le tranchant.")
        dehors = a.silhouette.oppose()
        interieur = G.regrouper(contours(a.silhouette))
        ex(len(interieur) > 0, "Silhouette vide.")
        largeurs = (f.rebord_mm, f.paroi_mm, f.tranchant_mm)
        z = (0.0, f.hauteur_rebord_mm, f.hauteur_coupe_mm - f.hauteur_tranchant_mm, f.hauteur_coupe_mm)
        bords = [contours(dehors.avec(F32(w) - dehors.v)) for w in largeurs]
        self.progress(0.6, "Parois de l'emporte-pièce")
        trous = [G.inverse(b) if G.aire(b) > 0 else b for b in G.boucles_de(interieur)]
        m = Maillage()
        face(m, G.regrouper(bords[0] + trous), z[0], False)
        face(m, G.regrouper(bords[2] + trous), z[3], True)
        for k in range(3):
            for b in G.boucles_de(G.regrouper(bords[k])):
                paroi(m, b, z[k], z[k + 1], True)
        for k in range(2):
            face(m, G.regrouper(bords[k] + bords[k + 1]), z[k + 1], True)
        for b in trous:
            paroi(m, b, z[0], z[3], True)
        ext = G.boucles_de(G.regrouper(bords[0])) + trous
        # Imprimé rebord sur le plateau puis retourné pour couper : sans miroir, le biscuit sortirait inversé.
        bb = m.boite()
        sym = float(F32(bb[0]) + F32(bb[3]))
        retourne = miroir(m, sym)
        ext_retourne = [G.miroir_x(b, sym) for b in ext]
        return self._resultat(EMPORTE_PIECE, retourne, [("Emporte-pièce", retourne)], ext_retourne, None)

    # ---------------------------------------------------------------- porte-clés et magnet
    def plaque(self):
        f, a, ex = self.f, self.a, self._exiger
        magnet = f.sortie == MAGNET
        ex(_dans(f.socle_mm, 1.5, 8.0), "Socle de 1,5 à 8 mm.")
        ex(_dans(f.relief_mm, 0.4, 4.0), "Relief des traits de 0,4 à 4 mm.")
        ex(_dans(f.marge_mm, 1.0, 6.0), "Marge autour du dessin de 1 à 6 mm.")
        self._borner_traits()
        f_socle = a.silhouette.decaler(f.marge_mm)
        if not magnet:
            f_socle = self._anneau(f_socle)
        f_motif = a.traits.inter(f_socle.decaler(-ECART_BORD))
        self.progress(0.4, "Socle et traits")
        socle = self._regions(f_socle)
        motif = self._regions(f_motif)
        ex(len(socle) > 0, "Forme vide : agrandis le dessin.")
        poche = self._poche(f_socle) if magnet else []
        self.progress(0.7, "Maillage")
        z_s, z_m = f.socle_mm, f.socle_mm + f.relief_mm
        tout, m_socle, m_motif = Maillage(), Maillage(), Maillage()
        socle_avec_motif(tout, socle, motif, z_s, z_m, poche, f.profondeur_aimant_mm)
        socle_avec_motif(m_socle, socle, [], z_s, z_s, poche, f.profondeur_aimant_mm)
        pieces = [("Socle", m_socle)]
        if motif:
            prisme(m_motif, motif, z_s, z_m)
            pieces.append(("Traits", m_motif))
        return self._resultat(f.sortie, tout, pieces, G.boucles_de(socle), z_s if motif else None)

    def _anneau(self, f_socle):
        """Languette percée en haut de la silhouette (point le plus haut, le plus proche du centre)."""
        f, g = self.f, self.g
        r_trou = min(max(f.diametre_anneau_mm, 2.5), 10.0) / 2
        r_lang = r_trou + min(max(f.paroi_anneau_mm, 1.5), 6.0)
        ys, xs = g.ys(), g.xs()
        plein = f_socle.v > F32(0)
        lignes = np.flatnonzero(plein.any(axis=1))
        y_max = float(ys[lignes.max()]) if lignes.size else -1.7976931348623157e308
        bb = G.boite(self.a.contours_silhouette)
        cx0 = (bb[0] + bb[2]) / 2
        cx = cx0
        meilleur = 1.7976931348623157e308
        for j in range(g.ny):
            if ys[j] >= y_max - 0.3:
                for i in np.flatnonzero(plein[j]).tolist():
                    d = abs(float(xs[i]) - cx0)
                    if d < meilleur:
                        meilleur, cx = d, float(xs[i])
        cy = y_max + r_lang * 0.45
        lang = g.remplir(lambda X, Y: r_lang - np.sqrt((X - cx) * (X - cx) + (Y - cy) * (Y - cy)))
        trou = g.remplir(lambda X, Y: r_trou - np.sqrt((X - cx) * (X - cx) + (Y - cy) * (Y - cy)))
        return f_socle.union(lang).moins(trou)

    def _poche(self, f_socle):
        """Logement d'aimant au point le plus profond du socle, ou rien (avertissement) s'il n'y a pas la place."""
        f, g, ex = self.f, self.g, self._exiger
        ex(_dans(f.diametre_aimant_mm, 4.0, 30.0) and _dans(f.profondeur_aimant_mm, 0.8, 6.0),
           "Aimant de 4 à 30 mm de diamètre, logement de 0,8 à 6 mm.")
        ex(f.socle_mm >= f.profondeur_aimant_mm + 0.8, "Socle trop fin pour l'aimant : il faut au moins 0,8 mm au-dessus du logement.")
        k = int(np.argmax(f_socle.v.ravel()))
        v = float(f_socle.v.ravel()[k])
        r = f.diametre_aimant_mm / 2 + 0.15
        if v < r + 1.2:
            self.avert.append(f"Pas de place pour un aimant de {format_fr(f.diametre_aimant_mm, 0)} mm : colle-le à plat au dos.")
            return []
        cx, cy = g.x(k % g.nx), g.y(k // g.nx)
        return [(_cercle(cx, cy, r, 64), [])]

    # ---------------------------------------------------------------- tampon
    def tampon(self):
        f, a, ex = self.f, self.a, self._exiger
        ex(_dans(f.socle_mm, 1.5, 8.0), "Plaque de 1,5 à 8 mm.")
        ex(_dans(f.relief_mm, 0.8, 5.0), "Relief du tampon de 0,8 à 5 mm.")
        ex(_dans(f.marge_mm, 1.0, 10.0), "Marge autour du dessin de 1 à 10 mm.")
        self._borner_traits()
        traits = G.boucles_de(self._regions(a.traits))
        ex(len(traits) > 0, "Aucun trait à mettre en relief.")
        bb = G.boite(traits)
        sym = bb[0] + bb[2]
        # Traits en miroir : posé face encrée sur le papier, le tampon rend le dessin à l'endroit.
        inverses = G.regrouper([G.miroir_x(b, sym) for b in traits])
        l = bb[2] - bb[0] + 2 * f.marge_mm
        h = bb[3] - bb[1] + 2 * f.marge_mm
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        rayon = min(max(min(3.0, min(l, h) / 4), 0.0), min(l, h) / 2)
        bx, by = l / 2 - rayon, h / 2 - rayon

        def rect(X, Y):
            qx = np.abs(X - cx) - bx
            qy = np.abs(Y - cy) - by
            dehors = np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0)) + np.minimum(np.maximum(qx, qy), 0.0) - rayon
            return -dehors

        socle = self._regions(self.g.remplir(rect))
        ex(len(socle) > 0, "Plaque vide.")
        m = Maillage()
        socle_avec_motif(m, socle, inverses, f.socle_mm, f.socle_mm + f.relief_mm)
        return self._resultat(TAMPON, m, [("Tampon", m)], G.boucles_de(socle), None)

    def _resultat(self, sortie, tout, pieces, contours_objet, changement):
        c = tout.controle()
        if not c["ferme"]:
            self.avert.append("Maillage non fermé détecté : augmente le lissage ou la taille du dessin.")
        b = tout.boite()
        self.progress(1.0, "Objet prêt")
        return ResultatDessin(sortie, tout, pieces, contours_objet, float(F32(b[3]) - F32(b[0])),
                              float(F32(b[4]) - F32(b[1])), float(F32(b[5]) - F32(b[2])), changement, self.avert, c)
