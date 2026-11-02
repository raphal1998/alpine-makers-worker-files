"""Tracé des contours (port de Trace.kt) : extraction au sous-pixel, lissage, jeu (décalage exact), simplification."""
import math

import numpy as np

from . import champ as ch
from .geometrie import Boucle, boite, cotes, perimetre, simplifier_boucle

F32 = np.float32


class ReglagesContour:
    def __init__(self, lissage_mm=0.3, tolerance_mm=0.05, jeu_mm=0.0):
        self.lissage_mm = float(lissage_mm)
        self.tolerance_mm = float(tolerance_mm)
        self.jeu_mm = float(jeu_mm)


class ObjetContour:
    def __init__(self, objet, boucles, cts):
        self.objet, self.boucles, self.cotes = objet, boucles, cts


class Trace:
    """Contours des objets retenus (mm, y vers le haut, origine au coin bas-gauche de la feuille)."""

    def __init__(self, objets, hauteur_mm, largeur_mm):
        self.objets, self.hauteur_mm, self.largeur_mm = objets, hauteur_mm, largeur_mm

    @property
    def boucles(self):
        return [b for o in self.objets for b in o.boucles]

    @property
    def cotes(self):
        return cotes(self.boucles) if self.objets else None


def extraire(m, id):
    """Indicateur de l'objet adouci par [1 2 1] ⊗ [1 2 1], contour au niveau 0,5 (marching squares)."""
    o = next((x for x in m.objets if x.id == id), None)
    if o is None:
        return []
    ppm, l, h = m.ppm, m.largeur, m.hauteur
    c0 = max(0, math.floor(o.boite[0] * ppm) - 2)
    c1 = min(l - 1, math.ceil(o.boite[2] * ppm) + 1)
    r0 = max(0, math.floor(o.boite[1] * ppm) - 2)
    r1 = min(h - 1, math.ceil(o.boite[3] * ppm) + 1)
    nx, ny = c1 - c0 + 1, r1 - r0 + 1
    if nx < 2 or ny < 2:
        return []
    ind = (m.etiquettes[r0:r1 + 1, c0:c1 + 1] == id).astype(F32)
    t = F32(2) * ind
    t[:, 1:] += ind[:, :-1]
    t[:, :-1] += ind[:, 1:]
    t /= F32(4)
    v = F32(2) * t
    v[1:, :] += t[:-1, :]
    v[:-1, :] += t[1:, :]
    v = v / F32(4) - F32(0.5)
    g = ch.Grille(nx, ny, (c0 + 0.5) / ppm, (h - r1 - 0.5) / ppm, 1.0 / ppm)
    return ch.contours(v[::-1].copy(), g, 2.0 / (ppm * ppm))


def lisser(b, sigma_mm):
    """Lissage gaussien circulaire, écart type mesuré le long du contour."""
    if sigma_mm <= 0.0 or b.n < 8:
        return b
    esp = perimetre(b) / b.n
    if esp <= 0:
        return b
    s = sigma_mm / esp
    if s < 0.35:
        return b
    k = min(math.ceil(3 * s), b.n // 3)
    if k < 1:
        return b
    w = np.exp(-((np.arange(2 * k + 1) - k) ** 2) / (2 * s * s))
    somme = w.sum()
    x = np.zeros(b.n)
    y = np.zeros(b.n)
    for jj in range(-k, k + 1):
        x += w[jj + k] * np.roll(b.x, -jj)
        y += w[jj + k] * np.roll(b.y, -jj)
    return Boucle(x / somme, y / somme)


def decaler(boucles, d, pas):
    """Décalage de d mm (règle non nulle) : distance signée exacte près du niveau voulu, contour de (distance + d)."""
    if abs(d) < 1e-9 or not boucles:
        return boucles
    g = ch.Grille.sur(boite(boucles), pas, abs(d) + 4 * pas)
    ch.verifier_taille(g)
    masque = ch.rasteriser(g, boucles)
    rayon_noeuds = math.ceil(abs(d) / pas) + 4
    v = ch.depuis_masque(g, masque, rayon_noeuds)
    rayon = abs(d) + 3 * pas
    taille = max(4 * pas, rayon / 3)
    portee = math.ceil(rayon / taille)
    jj, ii = np.nonzero(np.abs(v + F32(d)) < F32(2 * pas))
    if jj.size:
        dist = ch.distances_segments(g.x0 + ii * pas, g.y0 + jj * pas, boucles, g, taille, portee)
        ok = np.isfinite(dist)
        val = np.maximum(dist[ok], 1e-6)
        v = v.copy()
        v[jj[ok], ii[ok]] = np.where(masque[jj[ok], ii[ok]], val, -val).astype(F32)
    return ch.contours(v + F32(d), g, 4 * pas * pas)


def simplifier(boucles, tolerance_mm):
    if tolerance_mm <= 0:
        return boucles
    sortie = []
    for b in boucles:
        s = simplifier_boucle(b, tolerance_mm, tolerance_mm)
        if s is not None and s.n >= 3 and abs(s.aire) > 1e-6:
            sortie.append(s)
    return sortie


def tracer(m, r, progress=None):
    """Contours finaux des objets retenus : extraction, lissage, jeu, simplification, cotes."""
    pas = 1.0 / m.ppm
    retenus = m.retenus
    objets = []
    for k, o in enumerate(retenus):
        brut = [lisser(b, r.lissage_mm) for b in extraire(m, o.id)]
        if abs(r.jeu_mm) > 1e-9:
            decale = [lisser(b, 0.6 * pas) for b in decaler(brut, r.jeu_mm, pas)]
        else:
            decale = brut
        final = simplifier(decale, r.tolerance_mm)
        if any(b.aire > 0 for b in final):
            objets.append(ObjetContour(o, final, cotes(final)))
        if progress:
            progress((k + 1) / max(1, len(retenus)))
    return Trace(objets, m.hauteur / m.ppm, m.largeur / m.ppm)
