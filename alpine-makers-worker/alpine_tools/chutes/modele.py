"""Chute, pièces et fabrique des pièces (port de Chute.kt, Pieces.kt et des classes de Model.kt d'Alpine Chutes).

Géométrie en mm, Y VERS LE BAS, règle pair / impair (une boucle dans une autre est un trou). Comme dans l'application,
les formes gardées (List<Polyligne>) ont leurs coordonnées arrondies à 0,1 µm : le placement travaille sur ces valeurs.
"""
import math

from . import geometrie as geo
from .geometrie import Boucle, Refus, nombre

ROTATIONS = ("libre", "quart", "aucune")
LIBELLES_ROTATION = {"libre": "libre", "quart": "par 90°", "aucune": "aucune"}
COTE_MAX = 3000.0


class Retouche:
    """Zone interdite peinte : trait de disques de rayon ``rayon_mm`` le long de ``points`` [(x, y) mm]."""
    __slots__ = ("rayon_mm", "points")

    def __init__(self, rayon_mm, points):
        self.rayon_mm = float(rayon_mm)
        self.points = [(float(x), float(y)) for x, y in points]


class Chute:
    """Chute de matière : ``bords`` (boucles pair / impair), zones ``interdits``, ``marge_mm`` au bord, sens du fil
    ``fil_deg`` (0 = horizontal, 90 = vertical, None = sans fil). ``hauteur_feuille`` : hauteur de la feuille de
    référence (origine « coin de la feuille » du DXF et du G-code), None pour une chute saisie à la main."""

    def __init__(self, bords, interdits=(), marge_mm=2.0, fil_deg=None, nom="Chute", matiere="", epaisseur_mm=3.0,
                 hauteur_feuille=None, id=1):
        self.id = id
        self.nom, self.matiere, self.epaisseur_mm = nom, matiere, float(epaisseur_mm)
        self.bords = geo.polylignes(bords)
        self.interdits = list(interdits)
        self.marge_mm = float(marge_mm)
        self.fil_deg = None if fil_deg is None else float(fil_deg)
        self.hauteur_feuille = hauteur_feuille
        self._aire = None

    @property
    def boucles(self):
        return self.bords

    @property
    def boite(self):
        return geo.geo_boite(self.bords)

    @property
    def largeur(self):
        b = self.boite
        return b[2] - b[0]

    @property
    def hauteur(self):
        b = self.boite
        return b[3] - b[1]

    @property
    def aire(self):
        """Aire de matière (trous déduits, zones interdites comprises)."""
        if self._aire is None:
            self._aire = geo.aire_matiere(self.bords)
        return self._aire

    @property
    def trous(self):
        bs = self.bords
        dedans = sum(1 for b in bs if any(o is not b and abs(o.aire) > abs(b.aire) and o.contient(float(b.x[0]), float(b.y[0]))
                                         for o in bs))
        return max(0, dedans)

    @staticmethod
    def rectangulaire(largeur, hauteur, rayon_coins=0.0):
        return [geo.rectangle(largeur, hauteur, rayon_coins)]


class Piece:
    """Pièce à découper : ``boucles`` (mm, boîte en (0, 0)), ``quantite`` exemplaires, ``rotation`` permise
    (libre / quart / aucune), ``fil`` : suit le sens du fil de la chute (son axe X le long du fil, à 0° ou 180°)."""

    def __init__(self, nom, boucles, id=0, type="rectangle", quantite=1, rotation="quart", fil=False, ordre=0):
        if rotation not in ROTATIONS:
            raise Refus("Rotation inconnue.")
        self.id, self.nom, self.type = id, nom, type
        self.boucles = geo.polylignes(boucles)
        self.quantite, self.rotation, self.fil, self.ordre = int(quantite), rotation, bool(fil), int(ordre)
        self._aire = None
        self._boite = None

    def copie(self, **changes):
        p = Piece(self.nom, [], self.id, self.type, self.quantite, self.rotation, self.fil, self.ordre)
        p.boucles = self.boucles
        for k, v in changes.items():
            setattr(p, k, v)
        p._aire = None
        p._boite = None
        return p

    @property
    def boite(self):
        if self._boite is None:
            self._boite = geo.geo_boite(self.boucles)
        return self._boite

    @property
    def largeur(self):
        return self.boite[2] - self.boite[0]

    @property
    def hauteur(self):
        return self.boite[3] - self.boite[1]

    @property
    def aire(self):
        if self._aire is None:
            self._aire = geo.aire_matiere(self.boucles)
        return self._aire

    @property
    def points(self):
        return sum(b.n for b in self.boucles)


# ------------------------------------------------------------------ fabrique (Pieces.kt)

def _controler(v, nom, mini=0.5):
    if not math.isfinite(v) or v < mini:
        raise Refus(f"{nom} : au moins {nombre(mini)} mm.")
    if v > COTE_MAX:
        raise Refus(f"{nom} : {nombre(COTE_MAX)} mm au plus.")


def rectangle(nom, largeur, hauteur, rayon=0.0, trou=0.0):
    _controler(largeur, "Largeur")
    _controler(hauteur, "Hauteur")
    if rayon < 0 or rayon > min(largeur, hauteur) / 2:
        raise Refus(f"Rayon des coins : de 0 à {nombre(min(largeur, hauteur) / 2)} mm.")
    boucles = [geo.rectangle(largeur, hauteur, rayon)]
    if trou > 0:
        if trou > min(largeur, hauteur) - 2:
            raise Refus("Le trou central doit laisser au moins 1 mm de matière autour.")
        boucles.append(geo.cercle(trou).translater((largeur - trou) / 2, (hauteur - trou) / 2))
    return Piece(nom.strip() and nom or f"Rectangle {nombre(largeur)} × {nombre(hauteur)}", boucles, type="rectangle")


def cercle(nom, diametre, trou=0.0):
    _controler(diametre, "Diamètre", 1.0)
    boucles = [geo.cercle(diametre)]
    if trou > 0:
        if trou > diametre - 2:
            raise Refus("Le trou central doit laisser au moins 1 mm de matière autour.")
        boucles.append(geo.cercle(trou).translater((diametre - trou) / 2, (diametre - trou) / 2))
    return Piece(nom.strip() and nom or f"Disque Ø {nombre(diametre)}", boucles, type="cercle", rotation="aucune")


def texte(nom, contenu, hauteur, epaisseur, pochoir=False):
    """Texte découpé (police au trait d'Alpine Chutes, capitales), lettres épaissies de ``epaisseur`` mm."""
    from . import police
    propre = contenu.strip()
    if not propre:
        raise Refus("Saisis le texte à découper.")
    if len(propre) > 60:
        raise Refus("60 caractères au plus.")
    _controler(hauteur, "Hauteur des lettres", 4.0)
    if epaisseur < 0.8 or epaisseur > hauteur / 3:
        raise Refus(f"Épaisseur du trait : de 0,8 à {nombre(hauteur / 3)} mm.")
    absents = police.absents_liste(propre)
    if absents:
        raise Refus("Caractères non dessinés par la police : " + " ".join(absents))
    boucles = police.pochoir(propre, hauteur, epaisseur) if pochoir else police.contours(propre, hauteur, epaisseur)
    return Piece(nom.strip() and nom or f"Texte « {propre[:24]} »", geo.normaliser(boucles), type="texte", rotation="aucune")


def importee(nom, dessin, fichier, echelle=1.0, miroir=False):
    """Dessin importé (SVG / DXF) : ``echelle`` appliquée, miroir facultatif."""
    if not math.isfinite(echelle) or echelle <= 0.01 or echelle > 100:
        raise Refus("Échelle : de 0,01 à 100.")
    if not dessin.boucles:
        raise Refus("Ce fichier ne contient aucune forme fermée à découper.")
    b = geo.normaliser(geo.echelle(dessin.boucles, echelle))
    if miroir:
        b = geo.miroir(b)
    bo = geo.geo_boite(b)
    if bo[2] > COTE_MAX or bo[3] > COTE_MAX:
        raise Refus(f"Dessin trop grand ({geo.dimensions(bo[2], bo[3])}) : vérifie l'échelle.")
    if bo[2] < 0.5 or bo[3] < 0.5:
        raise Refus(f"Dessin minuscule ({geo.dimensions(bo[2], bo[3])}) : vérifie l'échelle ou les unités du fichier.")
    defaut = fichier.rsplit(".", 1)[0] if "." in fichier else fichier
    return Piece(nom.strip() and nom or (defaut.strip() or "Dessin importé"), b, type="import")
