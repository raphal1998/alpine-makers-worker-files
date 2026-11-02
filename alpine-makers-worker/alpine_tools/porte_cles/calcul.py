# -*- coding: utf-8 -*-
"""Porte-clés et plaques (portage de PorteCles.kt, Formes.kt et Svg.kt d'Alpine Gift).

Texte (1 ou 2 lignes, police « Alpine Ronde » dessinée dans ``police.py``) en relief ou gravé sur un socle de forme
choisie, anneau en option, bordure en option. Les formes se combinent en champs de distance signée sur une grille
fine, les contours sont tirés par marching squares puis simplifiés (sommets espacés d'au moins 0,15 mm), triangulés
et extrudés en solides fermés.
"""
import math

import numpy as np

from . import police
from .geometrie import Champ, boite, distance_signee, regrouper, boucles_de, simplifier
from .maillage import Maillage, prisme, socle_avec_motif
from .police import aire

ECART_TEXTE = 0.6     # jeu minimal texte ↔ bord du socle (mm)
ESPACEMENT = 0.15     # espacement minimal des sommets (mm)
TOLERANCE = 0.02      # écart max au vrai contour (mm)

FORMES = {"arrondi": "Arrondi", "capsule": "Capsule", "cercle": "Cercle", "coeur": "Cœur",
          "plaque": "Plaque rectangulaire", "contour": "Contour du texte"}
ANNEAUX = {"gauche": "À gauche", "haut": "En haut", "aucun": "Sans anneau"}


class Resultat:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# --- Formes (champ de distance signée, positif dedans) ---------------------------------------------------------------
def champ_rect_arrondi(c, cx, cy, largeur, hauteur, rayon):
    r = min(max(rayon, 0.0), min(largeur, hauteur) / 2)
    bx, by = largeur / 2 - r, hauteur / 2 - r
    x, y = c.grille()
    qx = np.abs(x - cx) - bx
    qy = np.abs(y - cy) - by
    dehors = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r
    return c.avec(-dehors)


def champ_ellipse(c, cx, cy, rx, ry):
    x, y = c.grille()
    return c.avec((1.0 - np.hypot((x - cx) / rx, (y - cy) / ry)) * min(rx, ry))


def contour_coeur(cx, cy, largeur, hauteur, n=360):
    """Cœur inscrit dans la boîte (x = 16 sin³t, y = 13 cos t − 5 cos 2t − 2 cos 3t − cos 4t), sens direct."""
    t = 2 * np.pi * (n - np.arange(n)) / n
    px = 16 * np.sin(t) ** 3
    py = 13 * np.cos(t) - 5 * np.cos(2 * t) - 2 * np.cos(3 * t) - np.cos(4 * t)
    px = cx + ((px - px.min()) / (px.max() - px.min()) - 0.5) * largeur
    py = cy + ((py - py.min()) / (py.max() - py.min()) - 0.5) * hauteur
    b = np.column_stack([px, py])
    return b if aire(b) > 0 else b[::-1].copy()


def _nombre(p, cle, defaut, lo=None, hi=None):
    try:
        v = float(p.get(cle, defaut))
    except (TypeError, ValueError):
        raise ValueError(f"Valeur invalide pour « {cle} ».")
    if not math.isfinite(v):
        raise ValueError(f"Valeur invalide pour « {cle} ».")
    if lo is not None:
        v = min(max(v, lo), hi)
    return v


def calculer(p, progress=lambda f, m: None):
    l1 = str(p.get("ligne1") or "").strip()[:24]
    l2 = str(p.get("ligne2") or "").strip()[:32]
    forme_id = str(p.get("forme") or "arrondi")
    if forme_id not in FORMES:
        raise ValueError("Forme inconnue.")
    anneau = str(p.get("anneau") or "gauche")
    if anneau not in ANNEAUX:
        raise ValueError("Position de l'anneau inconnue.")
    graisse = str(p.get("graisse") or "normale")
    if graisse not in police.GRAISSES:
        raise ValueError("Graisse inconnue.")
    taille = _nombre(p, "hauteur_texte", 10.0)
    taille2 = _nombre(p, "taille_ligne2", 60.0) / 100.0
    socle_mm = _nombre(p, "socle", 3.0)
    relief = _nombre(p, "relief", 1.2)
    grave = bool(p.get("grave", False))
    marge = _nombre(p, "marge", 3.0, 0.5, 20.0)
    interlettrage = _nombre(p, "interlettrage", 0.0, -2.0, 5.0)
    bordure = bool(p.get("bordure", False))
    largeur_bordure = _nombre(p, "largeur_bordure", 1.2, 0.6, 4.0)
    diam = _nombre(p, "diametre_anneau", 4.5, 2.0, 12.0)
    paroi_anneau = _nombre(p, "paroi_anneau", 2.5, 1.2, 8.0)

    if not l1 and not l2 and forme_id == "contour":
        raise ValueError("Saisis un texte pour la forme « contour du texte ».")
    if not 3.0 <= taille <= 60.0:
        raise ValueError("Hauteur du texte de 3 à 60 mm.")
    if not 1.0 <= socle_mm <= 10.0:
        raise ValueError("Socle de 1 à 10 mm.")
    if not 0.4 <= relief <= 5.0:
        raise ValueError("Relief de 0,4 à 5 mm.")
    if grave and relief > socle_mm - 0.6 + 1e-9:
        raise ValueError("La gravure doit laisser au moins 0,6 mm sous le texte.")
    avert = []
    progress(0.02, "texte en contours")
    # 1) Texte en contours, centré.
    h1 = taille
    h2 = taille * min(max(taille2, 0.3), 1.0)
    lignes = []
    if l1:
        c1, w1 = police.ligne(l1, h1, interlettrage, graisse)
        lignes += [b - np.array([w1 / 2, 0.0]) for b in c1]
    if l2:
        c2, w2 = police.ligne(l2, h2, interlettrage, graisse)
        y2 = -(h2 + 0.45 * h1) if l1 else 0.0
        lignes += [b + np.array([-w2 / 2, y2]) for b in c2]
    manquants = police.absents(l1 + l2)
    if manquants:
        avert.append("Caractères absents de la police : " + " ".join(manquants) + ".")
    if taille < 5.0:
        avert.append("Texte de moins de 5 mm : les détails fins risquent de mal s'imprimer avec une buse 0,4.")
    texte = []
    if lignes:
        bt = boite(lignes)
        cx, cy = (bt[0] + bt[2]) / 2, (bt[1] + bt[3]) / 2
        texte = [b - np.array([cx, cy]) for b in lignes]
    bt = boite(texte) if texte else [-10.0, -6.0, 10.0, 6.0]
    tw, th = bt[2] - bt[0], bt[3] - bt[1]
    # 2) Forme du socle.
    m = marge
    forme = None
    if forme_id == "arrondi":
        w, h = tw + 2 * m, th + 2 * m
        forme = ("rect", 0.0, 0.0, w, h, min(min(w, h) * 0.28, 6.0))
    elif forme_id == "plaque":
        w, h = tw + 2 * m, th + 2 * m
        forme = ("rect", 0.0, 0.0, w, h, 1.0)
    elif forme_id == "capsule":
        h = th + 2 * m
        forme = ("rect", 0.0, 0.0, tw + h + m * 0.5, h, h / 2)
    elif forme_id == "cercle":
        r = math.hypot(tw, th) / 2 + m
        forme = ("ellipse", 0.0, 0.0, r, r)
    elif forme_id == "coeur":
        w = max((tw + 2 * m) * 1.42, (th + 2 * m) * 1.6)
        h = w * 0.92
        forme = ("coeur", 0.0, -h * 0.06, w, h)
    if forme is None:
        boite_forme = [bt[0] - m, bt[1] - m, bt[2] + m, bt[3] + m]
    elif forme[0] == "ellipse":
        boite_forme = [forme[1] - forme[3], forme[2] - forme[4], forme[1] + forme[3], forme[2] + forme[4]]
    else:
        boite_forme = [forme[1] - forme[3] / 2, forme[2] - forme[4] / 2, forme[1] + forme[3] / 2, forme[2] + forme[4] / 2]
    # 3) Anneau : languette ronde percée d'un trou, à cheval sur le bord.
    r_trou = diam / 2
    r_lang = r_trou + paroi_anneau
    centre = None
    hb = boite_forme[3] - boite_forme[1]
    if anneau == "gauche":
        centre = (boite_forme[0] - r_lang * 0.45,
                  (boite_forme[1] + boite_forme[3]) / 2 + (hb * 0.18 if forme_id == "coeur" else 0.0))
    elif anneau == "haut":
        centre = ((boite_forme[0] + boite_forme[2]) / 2,
                  boite_forme[3] - (r_lang * 1.2 if forme_id == "coeur" else 0.0) + r_lang * 0.45)
    bx = list(boite_forme)
    if centre is not None:
        bx = [min(bx[0], centre[0] - r_lang), min(bx[1], centre[1] - r_lang),
              max(bx[2], centre[0] + r_lang), max(bx[3], centre[1] + r_lang)]
    dim = max(bx[2] - bx[0], bx[3] - bx[1])
    if dim > 250:
        raise ValueError(f"Objet trop grand ({int(dim)} mm) : réduis le texte.")
    pas = max(0.08, dim / 900.0)
    grille = Champ.sur(bx, pas, 2.0)
    progress(0.1, "champ du texte")
    f_texte = distance_signee(grille, texte) if texte else grille.avec(np.full((grille.ny, grille.nx), -100.0))
    progress(0.3, "forme du socle")
    if forme is None:
        f_socle = f_texte.decaler(m)
    elif forme[0] == "rect":
        f_socle = champ_rect_arrondi(grille, *forme[1:])
    elif forme[0] == "ellipse":
        f_socle = champ_ellipse(grille, *forme[1:])
    else:
        f_socle = distance_signee(grille, [contour_coeur(*forme[1:])])
    if centre is not None:
        x, y = grille.grille()
        d = np.hypot(x - centre[0], y - centre[1])
        f_socle = f_socle.union(grille.avec(r_lang - d)).moins(grille.avec(r_trou - d))
    progress(0.45, "motif")
    # 4) Motif (texte + bordure), toujours à l'intérieur du socle.
    f_motif = f_texte.inter(f_socle.decaler(-ECART_TEXTE))
    if texte and np.count_nonzero((f_texte.v > 0) & (f_socle.v - ECART_TEXTE < 0)):
        avert.append(f"Le texte touche le bord : il est rogné de {ECART_TEXTE} mm.".replace(".6", ",6"))
    if bordure:
        v = f_socle.v
        f_motif = f_motif.union(grille.avec(np.minimum(v - 0.8, (0.8 + largeur_bordure) - v)))
    # 5) Contours, simplifiés.

    def extraire(c):
        res = []
        for b in c.contours():
            s = simplifier(b, TOLERANCE, ESPACEMENT)
            if s is not None and abs(aire(s)) > 0.05:
                res.append(s)
        return res

    b_socle = extraire(f_socle)
    b_motif = extraire(f_motif)
    progress(0.65, "maillage")
    if not b_socle:
        raise ValueError("Forme vide : vérifie les dimensions.")
    poly_socle = regrouper(b_socle)
    poly_motif = regrouper(b_motif)
    o_socle = boucles_de(poly_socle)
    o_motif = boucles_de(poly_motif)
    # 6) Maillages.
    z_s = socle_mm
    z_m = socle_mm - relief if grave else socle_mm + relief
    tout, socle, motif = Maillage(), Maillage(), Maillage()
    if not poly_motif:
        prisme(tout, poly_socle, 0.0, z_s)
        prisme(socle, poly_socle, 0.0, z_s)
    else:
        socle_avec_motif(tout, poly_socle, poly_motif, z_s, z_m)
        progress(0.8, "pièces séparées")
        if grave:
            socle_avec_motif(socle, poly_socle, poly_motif, z_s, z_m)
            prisme(motif, poly_motif, z_m, z_s)
        else:
            prisme(socle, poly_socle, 0.0, z_s)
            prisme(motif, poly_motif, z_s, z_m)
    progress(0.9, "SVG laser")
    bf = boite(o_socle)
    titre = " — ".join(t for t in (l1, l2) if t) or "Plaque"
    svg = svg_laser(o_socle, o_motif, titre)
    ferme = tout.controle()["ferme"]
    if not ferme:
        avert.append("Maillage non fermé détecté : réessaie avec un texte plus grand.")
    return Resultat(tout=tout, socle=socle, texte=motif, svg=svg, contours_socle=o_socle, contours_texte=o_motif,
                    largeur=bf[2] - bf[0], hauteur=bf[3] - bf[1], epaisseur=max(z_s, z_m),
                    changement_couleur=(z_s if not grave and poly_motif else None), avertissements=avert,
                    titre=titre, ferme=ferme, grave=grave, forme=FORMES[forme_id], relief=relief, socle_mm=socle_mm,
                    graisse=graisse)


# --- SVG laser ---------------------------------------------------------------------------------------------------------
def _n(v):
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _chemin(boucles, min_x, max_y):
    parts = []
    for b in boucles:
        for k, (x, y) in enumerate(b.tolist()):
            parts.append(("M" if k == 0 else "L") + _n(x - min_x) + "," + _n(max_y - y))
        parts.append("Z")
    return "".join(parts)


def svg_laser(decoupe, gravure, titre, marge=1.0):
    """SVG en millimètres : découpe en trait rouge (#FF0000, sans remplissage), gravure en noir plein (pair-impair)."""
    bt = boite(decoupe + gravure)
    min_x, max_y = bt[0] - marge, bt[3] + marge
    l, h = bt[2] - bt[0] + 2 * marge, bt[3] - bt[1] + 2 * marge
    propre = titre.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n',
           f'<svg xmlns="http://www.w3.org/2000/svg" width="{_n(l)}mm" height="{_n(h)}mm" viewBox="0 0 {_n(l)} {_n(h)}">\n',
           f"  <title>{propre} — Alpine Tools</title>\n"]
    if gravure:
        out.append(f'  <path id="gravure" fill="#000000" fill-rule="evenodd" stroke="none" d="{_chemin(gravure, min_x, max_y)}"/>\n')
    if decoupe:
        out.append(f'  <path id="decoupe" fill="none" stroke="#FF0000" stroke-width="0.1" d="{_chemin(decoupe, min_x, max_y)}"/>\n')
    out.append("</svg>\n")
    return "".join(out)
