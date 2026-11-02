"""Chaîne complète d'un contour (port de Chaine.kt et EtatAssistant.exporter) : photo → boucles en mm → fichiers.

Comme l'application : aperçu sur la photo réduite (1600 px) à 4 px/mm, puis la même chaîne en pleine résolution
(photo source ≤ 3200 px) à 8 px/mm avec le seuil de l'aperçu, pour retrouver exactement les mêmes objets.
"""
import datetime
import re
import unicodedata

from . import detection, detourage as dt, trace as tr
from .geometrie import decimal_fr, mm, mm2

PX_MM_APERCU = 4.0
PX_MM_FINAL = 8.0
COTE_SOURCE = 3200
COTE_APERCU = 1600
MOIS = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc.")


class Resultat:
    def __init__(self, redressee, analyse, masque, trace):
        self.redressee, self.analyse, self.masque, self.trace = redressee, analyse, masque, trace


def calculer(photo, quad_px, fmt, reglages_detourage, reglages_contour, ppm, exclus=(), seuil=None, progress=None):
    """``quad_px`` : 4 points de référence en PIXELS de ``photo`` (ordre haut-gauche, haut-droite, bas-droite, bas-gauche)."""
    def p(a, b):
        return (lambda f: progress(a + (b - a) * f)) if progress else None

    h = dt.homographie(quad_px, fmt)
    img = dt.redresser(photo, h, fmt, ppm, p(0.0, 0.3))
    a = dt.analyser(img, fmt, ppm)
    if progress:
        progress(0.5)
    m = dt.masque(a, reglages_detourage, exclus, seuil)
    if progress:
        progress(0.6)
    t = tr.tracer(m, reglages_contour, p(0.6, 1.0))
    return Resultat(img, a, m, t)


def detecter(photo, fmt):
    return detection.detecter(photo, fmt)


def nom_sur(titre):
    """Nom de fichier sûr (lettres, chiffres, tirets), sans accent, 40 caractères au plus."""
    sans = "".join(c for c in unicodedata.normalize("NFD", titre) if not unicodedata.combining(c))
    t = re.sub(r"[^A-Za-z0-9]+", "-", sans).strip("-")[:40].strip("-")
    return (t or "contour").lower()


def titre_par_defaut(jour=None):
    jour = jour or datetime.date.today()
    return f"Contour du {jour.day} {MOIS[jour.month - 1]} {jour.year}"


def details(trace, fmt, reglages_contour):
    """Lignes lisibles du contour (comme la fiche de Mes contours et le cartouche du PNG)."""
    c = trace.cotes
    if c is None:
        return []
    n = len(trace.objets)
    jeu = reglages_contour.jeu_mm
    lignes = [
        f"{n} objet{'s' if n > 1 else ''} · {mm(c.largeur)} × {mm(c.hauteur)} hors tout",
        f"Jeu {'+' if jeu > 0 else ''}{decimal_fr(jeu, 1)} mm · lissage {decimal_fr(reglages_contour.lissage_mm, 2)} mm · "
        f"tolérance {decimal_fr(reglages_contour.tolerance_mm, 2)} mm",
        f"Feuille {fmt.libelle} · calcul à {decimal_fr(PX_MM_FINAL, 0)} px/mm",
    ]
    for o in trace.objets:
        k = o.cotes
        ligne = (f"Objet {o.objet.id} : {mm(k.longueur)} × {mm(k.largeur_min)}, aire {mm2(k.aire)}, coupe {mm(k.perimetre, 0)}")
        if k.trous > 0:
            ligne += f", {k.trous} trou{'s' if k.trous > 1 else ''}"
        lignes.append(ligne)
    return lignes
