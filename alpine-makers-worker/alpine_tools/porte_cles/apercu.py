# -*- coding: utf-8 -*-
"""Aperçu PNG du porte-clés vu de trois quarts dessus (portage de Rendu.porteCles d'Alpine Gift) : ombre portée,
tranche du socle, dessus éclairé, texte en relief ou gravé, liseré lumineux. Numpy + Pillow seulement."""
import re

import numpy as np
from PIL import Image, ImageFilter

from .geometrie import Champ, boite, rasteriser

SURECH = 3  # suréchantillonnage de l'anticrénelage


def couleur(texte, defaut):
    t = str(texte or "").strip()
    if not re.fullmatch(r"#?[0-9A-Fa-f]{6}", t):
        t = defaut
    t = t.lstrip("#")
    return np.array([int(t[k:k + 2], 16) for k in (0, 2, 4)], dtype=np.float32)


def assombrir(c, f):
    return c * f


def eclaircir(c, f):
    return c + (255 - c) * f


def _masque(boucles, s, ox, oy, largeur, hauteur):
    """Couverture anticrénelée (0..1) des boucles (règle non nulle), y de l'image vers le bas."""
    if not boucles:
        return np.zeros((hauteur, largeur), dtype=np.float32)
    px = [np.column_stack([ox + b[:, 0] * s, hauteur - (oy + b[:, 1] * s)]) for b in boucles]
    c = Champ(largeur * SURECH, hauteur * SURECH, 0.5 / SURECH, 0.5 / SURECH, 1.0 / SURECH, v=np.zeros(1))  # grille seule
    m = rasteriser(c, px).astype(np.float32)
    return m.reshape(hauteur, SURECH, largeur, SURECH).mean(axis=(1, 3))


def _decale(a, dx, dy):
    """Décale un masque de (dx, dy) pixels entiers, bord rempli de zéros."""
    out = np.zeros_like(a)
    h, w = a.shape
    dx, dy = int(round(dx)), int(round(dy))
    xs0, xs1 = max(0, -dx), min(w, w - dx)
    ys0, ys1 = max(0, -dy), min(h, h - dy)
    if xs1 > xs0 and ys1 > ys0:
        out[ys0 + dy:ys1 + dy, xs0 + dx:xs1 + dx] = a[ys0:ys1, xs0:xs1]
    return out


def _peindre(img, alpha, c):
    a = alpha[..., None]
    img *= (1 - a)
    img += a * c


def dessiner(r, chemin, couleur_socle, couleur_texte, cote=1080, hauteur=800):
    bx = boite(r.contours_socle)
    l, h = bx[2] - bx[0], bx[3] - bx[1]
    s = min(cote * 0.86 / l, hauteur * 0.80 / h)
    ox = (cote - l * s) / 2 - bx[0] * s
    oy = (hauteur - h * s) / 2 - bx[1] * s
    tranche = max(3.0, r.epaisseur * s * 0.35)
    m_socle = _masque(r.contours_socle, s, ox, oy, cote, hauteur)
    m_texte = _masque(r.contours_texte, s, ox, oy, cote, hauteur)
    yy, xx = np.mgrid[0:hauteur, 0:cote].astype(np.float32)
    # Fond : halo violet nuit, comme l'écran d'Alpine Gift.
    d = np.hypot((xx - cote / 2) / cote, (yy - hauteur / 2) / hauteur) / 0.75
    t = np.clip(d, 0, 1)[..., None]
    img = np.array([0x2B, 0x1A, 0x30], np.float32) * (1 - t) + np.array([0x0D, 0x07, 0x10], np.float32) * t
    # Ombre portée floue.
    ombre = _decale(m_socle, cote * 0.015, tranche + cote * 0.02)
    ombre = np.asarray(Image.fromarray((ombre * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(cote * 0.012)),
                       dtype=np.float32) / 255
    _peindre(img, ombre * 0.4, np.zeros(3, np.float32))
    # Tranche du socle.
    tr = np.zeros_like(m_socle)
    for k in range(int(tranche), 0, -1):
        np.maximum(tr, _decale(m_socle, 0, k), out=tr)
    _peindre(img, tr, assombrir(couleur_socle, 0.62))
    # Dessus éclairé en haut à gauche.
    g = ((xx * cote + yy * hauteur) / float(cote * cote + hauteur * hauteur))[..., None]
    dessus = eclaircir(couleur_socle, 0.12) * (1 - g) + assombrir(couleur_socle, 0.88) * g
    _peindre(img, m_socle, dessus)
    if r.contours_texte:
        if r.grave:
            _peindre(img, _decale(m_texte, 0, -tranche * 0.35), assombrir(couleur_texte, 0.7))
            _peindre(img, m_texte, couleur_texte)
        else:
            rel = max(2.0, tranche * 0.45)
            bord = np.zeros_like(m_texte)
            for k in range(int(rel), 0, -1):
                np.maximum(bord, _decale(m_texte, 0, k - rel), out=bord)
            _peindre(img, bord, assombrir(couleur_texte, 0.6))
            haut = eclaircir(couleur_texte, 0.18) * (1 - g) + couleur_texte * g
            _peindre(img, _decale(m_texte, 0, -rel), haut)
    # Liseré lumineux le long du bord du socle.
    erode = np.asarray(Image.fromarray((m_socle * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(3)),
                       dtype=np.float32) / 255
    _peindre(img, np.clip(m_socle - erode, 0, 1) * (0x55 / 255), np.full(3, 255, np.float32))
    Image.fromarray(np.clip(img + 0.5, 0, 255).astype(np.uint8), "RGB").save(chemin, optimize=True)
