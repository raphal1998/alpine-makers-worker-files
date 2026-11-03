# -*- coding: utf-8 -*-
"""Lecture du dessin (portage de Lecture et Dessins, DessinEnfant.kt d'Alpine Gift) : seuillage, nettoyage (taches,
trous, traits interrompus), silhouette (dessin rempli), contours lisses des traits et de la silhouette en champs de
distance signée (mm, positif dans la matière). Grille de travail : y vers le haut, origine au coin bas gauche."""
from decimal import ROUND_HALF_UP, Decimal

import numpy as np

from . import composantes as K
from . import contours as G
from .champ import F32, Champ, depuis_masque, distance_signee, edt_carre, flou, valeur
from .image import echantillon_grille

TAILLE_MIN = 15.0
TAILLE_MAX = 250.0
MARGE_GRILLE = 24.0
TOLERANCE = 0.03
ESPACEMENT = 0.15


def format_fr(v, decimales=1):
    """String.format(Locale.FRANCE, "%.Nf") : chiffres courts du double arrondis au demi supérieur, virgule."""
    q = Decimal(repr(float(v))).quantize(Decimal(1).scaleb(-decimales), rounding=ROUND_HALF_UP)
    return f"{q:f}".replace(".", ",")


class ParamDessin:
    def __init__(self, largeur_mm=80.0, hauteur_mm=60.0, seuil=-1.0, inverser=False, nettoyage_mm2=1.0,
                 lissage_mm=0.3, trait_min_mm=0.9):
        self.largeur_mm, self.hauteur_mm = float(largeur_mm), float(hauteur_mm)
        self.seuil, self.inverser = float(seuil), bool(inverser)
        self.nettoyage_mm2, self.lissage_mm, self.trait_min_mm = float(nettoyage_mm2), float(lissage_mm), float(trait_min_mm)


class AnalyseDessin:
    def __init__(self, param, seuil, grille, traits, silhouette, contours_traits, contours_silhouette, avertissements):
        self.param, self.seuil, self.grille = param, seuil, grille
        self.traits, self.silhouette = traits, silhouette
        self.contours_traits, self.contours_silhouette = contours_traits, contours_silhouette
        self.avertissements = avertissements

    @property
    def aire_traits(self):
        return sum(G.aire(b) for b in self.contours_traits)

    @property
    def aire_silhouette(self):
        return sum(G.aire(b) for b in self.contours_silhouette)

    def apercu(self, largeur=400):
        """Aperçu de la lecture (RGB uint8) : traits noirs, silhouette bleutée, fond blanc."""
        p = self.param
        l = min(max(int(largeur), 16), 2000)
        h = max(1, int(l * p.hauteur_mm / p.largeur_mm))
        x = (np.arange(l) + 0.5) / l * p.largeur_mm
        y = p.hauteur_mm - (np.arange(h) + 0.5) / h * p.hauteur_mm
        X, Y = np.meshgrid(x, y)
        img = np.full((h, l, 3), 255, dtype=np.uint8)
        img[valeur(self.silhouette, X, Y) > 0] = (0xD6, 0xE6, 0xF5)
        img[valeur(self.traits, X, Y) > 0] = (0x1D, 0x1D, 0x1F)
        return img


def otsu(g):
    """DessinEnfant.otsu : seuil (0..1) au milieu du plateau des maxima de la variance inter-classes."""
    q = (np.clip(g, F32(0), F32(1)) * F32(255) + F32(0.5)).astype(np.int64)
    hist = np.bincount(q.ravel(), minlength=256)[:256].tolist()
    total = float(g.size)
    somme = 0.0
    for i in range(256):
        somme += i * float(hist[i])
    somme_b = poids_b = 0.0
    variance = [-1.0] * 256
    for i in range(256):
        poids_b += hist[i]
        somme_b += i * float(hist[i])
        poids_f = total - poids_b
        if poids_b == 0.0 or poids_f == 0.0:
            continue
        mb = somme_b / poids_b
        mf = (somme - somme_b) / poids_f
        variance[i] = poids_b * poids_f * (mb - mf) * (mb - mf)
    meilleur = max(variance)
    if meilleur <= 0:
        return 0.5
    limite = meilleur * (1 - 1e-9)
    premier = next(i for i in range(256) if variance[i] >= limite)
    dernier = next(i for i in range(255, -1, -1) if variance[i] >= limite)
    return min(0.95, max(0.05, ((premier + dernier) / 2.0 + 0.5) / 255.0))


# ------------------------------------------------------------------ outils de grille (Dessins)
def contours(c):
    """Contours lisses (niveau 0) d'un champ, simplifiés."""
    r = []
    for b in c.contours():
        s = G.simplifier(b, TOLERANCE, ESPACEMENT)
        if s is not None and abs(G.aire(s)) > 0.05:
            r.append(s)
    return r


def champ_lisse(modele, masque):
    """Champ lisse d'un masque : distance signée adoucie, contours, puis distance exacte à ces contours."""
    brut = flou(flou(depuis_masque(modele, masque)))
    b = contours(brut)
    if not b:
        return modele.avec(np.full((modele.ny, modele.nx), F32(-100.0), dtype=F32)), b
    return distance_signee(modele, b), b


def boucher(m, aire_max):
    """Bouche les trous (fond non relié au bord, 4-voisinage) de moins de ``aire_max`` nœuds (tous si < 0)."""
    et, n = K.etiqueter(~m, False)
    if n == 0:
        return m.copy()
    bord = K.touche_bord(et, n)
    t = K.tailles(et, n)
    remplir = ~bord & ((t < aire_max) if aire_max >= 0 else True)
    remplir[0] = False
    return m | remplir[et]


def nettoyer(m, aire_min):
    """Retire les composantes (8-voisinage) de moins de ``aire_min`` nœuds."""
    et, n = K.etiqueter(m, True)
    if n == 0:
        return m.copy()
    garde = K.tailles(et, n) >= aire_min
    garde[0] = False
    return garde[et]


def fermer(m, r):
    """Dilatation puis érosion d'un rayon ``r`` nœuds (raccorde les traits interrompus sans les épaissir)."""
    if r <= 0:
        return m
    r2 = float(F32(r * r))
    dilate = edt_carre(m, True).astype(F32) <= F32(r2)
    return edt_carre(dilate, False).astype(F32) > F32(r2)


# ------------------------------------------------------------------ lecture
def analyser(image, p, progress=None):
    """Lit le dessin ``image`` (float32, l'image entière mesure largeur × hauteur mm). ValueError si la saisie ne
    convient pas (taille, dessin vide)."""
    if not (TAILLE_MIN <= p.largeur_mm <= TAILLE_MAX and TAILLE_MIN <= p.hauteur_mm <= TAILLE_MAX):
        raise ValueError("Taille du dessin de 15 à 250 mm de côté.")
    if p.seuil >= 0 and not 0.02 <= p.seuil <= 0.98:
        raise ValueError("Seuil de 0,02 à 0,98 (ou automatique).")
    if not 0.0 <= p.nettoyage_mm2 <= 50.0:
        raise ValueError("Nettoyage de 0 à 50 mm².")
    if not 0.0 <= p.lissage_mm <= 3.0:
        raise ValueError("Lissage de 0 à 3 mm.")
    if not 0.0 <= p.trait_min_mm <= 4.0:
        raise ValueError("Épaisseur minimale des traits de 0 à 4 mm.")
    return _Lecture(image, p, progress or (lambda f, m="": None)).lire()


class _Lecture:
    def __init__(self, image, p, progress):
        self.image, self.p, self.progress = image, p, progress
        self.avert = []
        self.pas = max(0.1, (max(p.largeur_mm, p.hauteur_mm) + 2 * MARGE_GRILLE) / 900.0)
        self.grille = Champ.sur((0.0, 0.0, p.largeur_mm, p.hauteur_mm), self.pas, MARGE_GRILLE)

    def lire(self):
        p, g = self.p, self.grille
        seuil = otsu(F32(1) - self.image if p.inverser else self.image) if p.seuil < 0 else p.seuil
        traits = self.masque(seuil)
        self.progress(0.1, "Lecture des traits")
        aire_min = max(1, int(p.nettoyage_mm2 / (self.pas * self.pas)))
        traits = nettoyer(traits, aire_min)
        traits = boucher(traits, aire_min)
        traits = fermer(traits, p.lissage_mm / self.pas)
        if not traits.any():
            raise ValueError("Aucun trait trouvé : vérifie le seuil, le cadrage ou l'option « traits clairs ».")
        self.progress(0.3, "Silhouette")
        sil = self.silhouette(traits)
        self.progress(0.45, "Contours des traits")
        f_traits, _ = champ_lisse(g, traits)
        e = self.epaississement(f_traits)
        if e > 0:
            f_traits = f_traits.decaler(e)
            self.avert.append(f"Traits fins épaissis de {format_fr(2 * e)} mm pour s'imprimer "
                              f"({format_fr(p.trait_min_mm)} mm au moins).")
        self.progress(0.7, "Contours de la silhouette")
        f_sil, _ = champ_lisse(g, sil)
        b_traits = contours(f_traits)
        b_sil = contours(f_sil)
        if not b_sil:
            raise ValueError("Dessin trop petit pour être lu : agrandis-le.")
        aire_t = sum(G.aire(b) for b in b_traits)
        aire_s = sum(G.aire(b) for b in b_sil)
        if aire_s < 1.3 * aire_t:
            self.avert.append("Le contour du dessin semble ouvert : la silhouette suit les traits. "
                              "Referme le contour au feutre pour un emporte-pièce plein.")
        self.progress(1.0, "Dessin lu")
        return AnalyseDessin(p, seuil, g, f_traits, f_sil, b_traits, b_sil, self.avert)

    def masque(self, seuil):
        """Nœud de trait : gris sous le seuil (fond blanc hors du dessin)."""
        p, g = self.p, self.grille
        xs, ys = g.xs(), g.ys()
        dans_x = (xs >= 0) & (xs <= p.largeur_mm)
        dans_y = (ys >= 0) & (ys <= p.hauteur_mm)
        m = np.zeros((g.ny, g.nx), dtype=bool)
        if not dans_x.any() or not dans_y.any():
            return m
        ix, jy = np.flatnonzero(dans_x), np.flatnonzero(dans_y)
        v = echantillon_grille(self.image, xs[ix] / p.largeur_mm, 1.0 - ys[jy] / p.hauteur_mm).astype(np.float64)
        if p.inverser:
            v = 1.0 - v
        m[np.ix_(jy, ix)] = v < seuil
        return m

    def silhouette(self, traits):
        """Dessin rempli : trous bouchés, parties proches raccordées, plus grande partie gardée."""
        s = boucher(traits, -1)
        s = fermer(s, max(self.p.lissage_mm, 1.0) / self.pas)
        s = boucher(s, -1)
        et, n = K.etiqueter(s, True)
        if n > 1:
            t = K.tailles(et, n)
            grande = 1 + int(np.argmax(t[1:]))
            perdues = sum(1 for k in range(1, n + 1) if k != grande and t[k] * self.pas * self.pas > 2.0)
            if perdues > 0:
                self.avert.append(f"{perdues} partie(s) séparée(s) du dessin ignorée(s) pour la silhouette : relie-les au feutre.")
            return et == grande
        return s

    def epaississement(self, f):
        """Épaississement (mm, par côté) : la largeur moyenne des traits (≈ 4 × distance moyenne au bord) portée au
        minimum demandé."""
        v = f.v.ravel()
        pos = v[v > 0].astype(np.float64)
        if pos.size == 0:
            return 0.0
        largeur = 4 * float(np.cumsum(pos)[-1]) / pos.size
        return max(0.0, (self.p.trait_min_mm - largeur) / 2)
