# -*- coding: utf-8 -*-
"""Carte du ciel d'une date, d'une heure et d'un lieu : portage de C:\\AlpineGift core CielEtoile.kt.

Projection stéréographique centrée sur le zénith (l'horizon est le bord du disque, le nord en haut, l'est à gauche
comme lorsqu'on regarde le ciel couché sur le dos). Étoiles du catalogue précessées à la date, constellations, Lune
(phase) et planètes visibles. Sorties : SVG en mm (gravure), trame de gravure laser, carte des couleurs d'une plaque 3D
multicolore (même 3MF que Tableau couleur).

Seule différence voulue avec l'application : les textes sont tracés avec la police « Alpine Ronde » dessinée pour les
Alpine Tools (police.py), l'application utilisant des polices TTF (Montserrat, Lobster…) que le contrat des outils
exclut. Formes, positions, tailles et règles sont celles de Gift.
"""
import math
from collections import namedtuple
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

import numpy as np

from . import astro as A
from . import catalogue as K
from . import fuseaux as Z
from . import police as P
from .geometrie import Boucle, boite, sans_alignes
from .trame import VIDE, Trame

# Classes des formes dessinées (index de couleur de la plaque 3D) : ClasseCiel.
FOND, ETOILE, LIGNE, TEXTE = 0, 1, 2, 3
PLANCHER_ETOILE_3D = 0.4    # rayon minimal d'une étoile imprimée (buse 0,4)
PLANCHER_TRAIT_3D = 0.5
FORMES = {"affiche": "Affiche avec texte", "disque": "Disque"}
STYLES = {"etoiles": "Étoiles gravées", "ciel": "Ciel gravé, étoiles claires"}
PIXELS_TRAME_MAX = 64_000_000   # trame de gravure (octets) : au-delà, refus explicite


@dataclass(frozen=True)
class ParamCiel:
    """Réglages d'une carte du ciel. Instant en millisecondes UTC ; ``zone`` : fuseau du lieu (affichage de l'heure)."""
    instant_ms: int = 1_750_000_000_000
    zone: str = "Europe/Zurich"
    lat: float = 46.2331
    lon: float = 7.3606
    lieu: str = "Sion"
    diametre_mm: float = 160.0
    forme: str = "affiche"
    marge_mm: float = 10.0
    mag_limite: float = 5.5
    taille_etoiles: float = 1.0
    lignes: bool = True
    noms: bool = False
    cardinaux: bool = True
    planetes: bool = True
    lune: bool = True
    titre: str = "Notre ciel"
    ligne_date: bool = True
    message: str = ""
    largeur_trait_mm: float = 0.3


# Formes de la carte en mm (repère de la plaque, y vers le haut).
Disque = namedtuple("Disque", "x y r classe")
Trait = namedtuple("Trait", "xa ya xb yb largeur classe")
Anneau = namedtuple("Anneau", "x y r0 r1 classe")
Surfaces = namedtuple("Surfaces", "boucles classe")
# Astre visible placé sur la carte.
AstreCiel = namedtuple("AstreCiel", "nom x y hauteur azimut")


@dataclass
class CarteCiel:
    largeur_mm: float
    hauteur_mm: float
    cx: float
    cy: float
    rayon: float
    forme: str
    formes: list
    contour: Boucle
    etoiles_visibles: int
    astres: list
    lignes_texte: list
    avertissements: list

    def dans_ciel(self, x, y):
        return (x - self.cx) * (x - self.cx) + (y - self.cy) * (y - self.cy) <= self.rayon * self.rayon


class Police:
    """Police « Alpine Ronde » (police.py) avec l'interface de la classe Police de Gift : ligne(), absents()."""

    def __init__(self, graisse="normale"):
        self.graisse = graisse

    def ligne(self, texte, hauteur, interlettrage=0.0):
        boucles, l = P.ligne(texte, hauteur, interlettrage, self.graisse)
        return [Boucle(b[:, 0], b[:, 1]) for b in boucles], l

    def absents(self, texte):
        return P.absents(texte)


# ================================================================================================ textes et nombres
def _demi_haut(v, decimales):
    """Arrondi de String.format de Java (« %.nf ») : chiffres décimaux du double puis demi vers le haut."""
    return Decimal(repr(float(v))).quantize(Decimal(1).scaleb(-decimales), rounding=ROUND_HALF_UP)


def n(v):
    """Nombre des SVG : %.3f (Locale.US) sans zéros finaux."""
    return format(_demi_haut(v, 3), "f").rstrip("0").rstrip(".")


def nombre_fr(v, decimales):
    return format(_demi_haut(v, decimales), "f").replace(".", ",")


def coordonnees(lat, lon):
    """« 46,233° N · 7,361° E » (Locale.FRANCE)."""
    return (f"{nombre_fr(abs(lat), 3)}° {'N' if lat >= 0 else 'S'} · "
            f"{nombre_fr(abs(lon), 3)}° {'E' if lon >= 0 else 'O'}")


def date_lisible(p):
    return Z.date_lisible(p.instant_ms, p.zone)


# ================================================================================================ calcul
def projeter(hauteur, azimut, r):
    """Projection : hauteur, azimut → point du disque (centre 0, rayon ``r``)."""
    rho = r * math.tan(A.rad(90.0 - hauteur) / 2)
    a = A.rad(azimut)
    return -rho * math.sin(a), rho * math.cos(a)


def rayon_etoile(mag, p, plancher):
    """Rayon d'une étoile selon sa magnitude (mm)."""
    d = p.diametre_mm
    f = min(1.0, max(0.0, (p.mag_limite + 1.0 - mag) / (p.mag_limite + 2.5)))
    return max(plancher, (d * 0.0016 + d * 0.0085 * f ** 1.6) * p.taille_etoiles)


def texte_centre(police, texte, hauteur, x, y, interlettrage):
    """Ligne de texte centrée en ``x``, ligne de base à ``y``."""
    if not texte.strip():
        return []
    b, l = police.ligne(texte, hauteur, interlettrage)
    return [bb.translater(x - l / 2, y) for bb in b]


def couper(xa, ya, xb, yb, cx, cy, r):
    """Segment coupé au disque (centre, rayon) ; None s'il est dehors."""
    dx, dy = xb - xa, yb - ya
    fx, fy = xa - cx, ya - cy
    a = dx * dx + dy * dy
    if a < 1e-12:
        return None
    b = 2 * (fx * dx + fy * dy)
    c = fx * fx + fy * fy - r * r
    disc = b * b - 4 * a * c
    if disc <= 0:
        return None
    s = math.sqrt(disc)
    t0 = max(0.0, (-b - s) / (2 * a))
    t1 = min(1.0, (-b + s) / (2 * a))
    if t1 <= t0:
        return None
    return xa + t0 * dx, ya + t0 * dy, xa + t1 * dx, ya + t1 * dy


def _direction_vers(de, vers, lat, tsl, r):
    """Direction (unitaire, repère de la carte) de ``de`` vers ``vers`` le long du grand cercle."""
    def vec(e):
        return (math.cos(A.rad(e.dec)) * math.cos(A.rad(e.ra)), math.cos(A.rad(e.dec)) * math.sin(A.rad(e.ra)),
                math.sin(A.rad(e.dec)))
    a, b = vec(de), vec(vers)
    pas = 0.02
    q = [a[k] + (b[k] - a[k]) * pas for k in range(3)]
    nq = math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2])
    ra = A.norm(A.deg(math.atan2(q[1], q[0])))
    dec = A.deg(math.asin(min(1.0, max(-1.0, q[2] / nq))))
    h0 = A.horizontales(de.ra, de.dec, lat, tsl)
    h1 = A.horizontales(ra, dec, lat, tsl)
    p0 = projeter(h0.hauteur, h0.azimut, r)
    p1 = projeter(h1.hauteur, h1.azimut, r)
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    l = math.hypot(dx, dy)
    return (1.0, 0.0) if l < 1e-12 else (dx / l, dy / l)


def croissant(x, y, r, fraction, direction, n_=48):
    """Partie éclairée de la Lune (polygone direct) : limbe côté Soleil + terminateur elliptique."""
    if fraction < 0.02:
        return None
    s = 1 - 2 * fraction  # abscisse relative du terminateur (+1 nouvelle lune, −1 pleine lune)
    ux, uy = direction
    vx, vy = -uy, ux
    xs, ys = [], []
    for k in range(n_ + 1):  # limbe éclairé : de −90° à +90° autour de la direction du Soleil
        t = -math.pi / 2 + math.pi * k / n_
        a = math.cos(t) * r
        b = math.sin(t) * r
        xs.append(x + a * ux + b * vx)
        ys.append(y + a * uy + b * vy)
    for k in range(1, n_):  # terminateur, retour de +90° à −90°
        t = math.pi / 2 - math.pi * k / n_
        a = s * math.cos(t) * r
        b = math.sin(t) * r
        xs.append(x + a * ux + b * vx)
        ys.append(y + a * uy + b * vy)
    bc = sans_alignes(Boucle(xs, ys))
    if bc is None:
        return None
    return bc.inverse() if bc.aire < 0 else bc


def cercle(cx, cy, r, n_):
    return Boucle([cx + r * math.cos(2 * math.pi * k / n_) for k in range(n_)],
                  [cy + r * math.sin(2 * math.pi * k / n_) for k in range(n_)])


def rect_arrondi(x0, y0, x1, y1, rayon, par_quart=10):
    xs, ys = [], []
    coins = ((x1 - rayon, y0 + rayon, -90.0), (x1 - rayon, y1 - rayon, 0.0), (x0 + rayon, y1 - rayon, 90.0),
             (x0 + rayon, y0 + rayon, 180.0))
    for c in coins:
        for k in range(par_quart + 1):
            a = A.rad(c[2] + 90.0 * k / par_quart)
            xs.append(c[0] + rayon * math.cos(a))
            ys.append(c[1] + rayon * math.sin(a))
    return sans_alignes(Boucle(xs, ys))


def calculer(p, police, police_titre=None, pour_3d=False, progress=None):
    """Carte complète. ``police`` : textes (None : aucun texte tracé) ; ``pour_3d`` : tailles relevées pour
    l'impression (étoiles, traits, textes)."""
    if police_titre is None:
        police_titre = police
    avancer = progress or (lambda f: None)
    if not 60.0 <= p.diametre_mm <= 400.0:
        raise ValueError("Diamètre du ciel de 60 à 400 mm.")
    if not (-90.0 <= p.lat <= 90.0 and -180.0 <= p.lon <= 180.0):
        raise ValueError("Coordonnées du lieu invalides.")
    avert = []
    r = p.diametre_mm / 2
    m = min(40.0, max(3.0, p.marge_mm))
    texte_affiche = p.forme == "affiche"
    h_titre = max(5.0 if pour_3d else 3.0, p.diametre_mm * 0.055)
    h_petit = max(3.5 if pour_3d else 2.0, p.diametre_mm * 0.026)
    titre_present = bool(p.titre.strip())
    if texte_affiche:
        lignes_texte = [t for t in (p.titre.strip() or None, p.message.strip() or None,
                                    date_lisible(p) if p.ligne_date else None,
                                    " · ".join(x for x in (p.lieu.strip(), coordonnees(p.lat, p.lon)) if x)
                                    if p.ligne_date else None) if t is not None]
    else:
        lignes_texte = []
    if texte_affiche:
        somme = 0.0
        for i in range(len(lignes_texte)):
            somme += (h_titre if i == 0 and titre_present else h_petit) * 1.9
        zone_texte = m * 0.6 + somme + m * 0.4
    else:
        zone_texte = 0.0
    ecart_cardinal = max(h_petit * 1.6, 5.0) if p.cardinaux else 0.0
    w = 2 * (r + max(m, ecart_cardinal + 1.0))
    h_total = w + zone_texte
    cx = w / 2
    cy = h_total - w / 2
    formes = []

    # Temps et lieu.
    jd_ut = A.jour_julien_ms(p.instant_ms)
    jd_tt = A.jour_julien_tt(jd_ut)
    tsl = A.norm(A.temps_sideral(jd_ut) + p.lon)
    annees = (jd_tt - A.J2000) / 365.25

    def vers_carte(eq):
        hz = A.horizontales(eq.ra, eq.dec, p.lat, tsl)
        pt = projeter(hz.hauteur, hz.azimut, r)
        return hz, (cx + pt[0], cy + pt[1])

    # Étoiles (précessées à la date), position mémorisée pour les constellations.
    position = {}
    plancher = PLANCHER_ETOILE_3D if pour_3d else 0.12
    visibles = 0
    for e in K.etoiles():
        mp = A.mouvement_propre(e.ra, e.dec, e.pm_ra, e.pm_dec, annees)
        hz, pt = vers_carte(A.precession(mp.ra, mp.dec, jd_tt))
        position[id(e)] = (pt[0], pt[1], hz.hauteur)
        if hz.hauteur < 0 or e.mag > p.mag_limite:
            continue
        visibles += 1
        formes.append(Disque(pt[0], pt[1], rayon_etoile(e.mag, p, plancher), ETOILE))
    avancer(0.4)
    trait = max(p.largeur_trait_mm, PLANCHER_TRAIT_3D if pour_3d else 0.12)

    # Constellations : segments coupés au bord du disque, nom au centre des étoiles visibles.
    lignes_formes, noms_formes = [], []
    h_nom = max(3.0 if pour_3d else 1.6, p.diametre_mm * 0.016)
    for c in K.constellations():
        sx = sy = 0.0
        nb = 0
        for a, b in c.segments:
            pa, pb = position[id(a)], position[id(b)]
            if pa[2] < 0 and pb[2] < 0:
                continue
            s = couper(pa[0], pa[1], pb[0], pb[1], cx, cy, r)
            if s is None:
                continue
            if p.lignes:
                lignes_formes.append(Trait(s[0], s[1], s[2], s[3], trait, LIGNE))
            if pa[2] >= 0:
                sx += pa[0]
                sy += pa[1]
                nb += 1
            if pb[2] >= 0:
                sx += pb[0]
                sy += pb[1]
                nb += 1
        if p.noms and police is not None and nb >= 4:
            lx, ly = sx / nb, sy / nb
            if math.hypot(lx - cx, ly - cy) < r - h_nom * 3:
                noms_formes.append(Surfaces(texte_centre(police, c.nom.upper(), h_nom, lx, ly - h_nom * 1.8, 0.08 * h_nom), LIGNE))
    formes[0:0] = lignes_formes
    formes.extend(noms_formes)
    avancer(0.55)

    # Lune et planètes.
    astres = []
    if p.planetes:
        for el in A.PLANETES:
            hz, pt = vers_carte(A.planete(el, jd_tt))
            if hz.hauteur < 0:
                continue
            astres.append(AstreCiel(el.nom, pt[0], pt[1], hz.hauteur, hz.azimut))
            rp = max(0.8 if pour_3d else 0.5, p.diametre_mm * 0.0075)
            formes.append(Disque(pt[0], pt[1], rp, ETOILE))
            formes.append(Anneau(pt[0], pt[1], rp * 1.7, rp * 1.7 + trait, ETOILE))
            if police is not None and math.hypot(pt[0] - cx, pt[1] - cy) < r - h_nom * 4:
                formes.append(Surfaces(texte_centre(police, el.nom, h_nom, pt[0], pt[1] - rp * 2.2 - h_nom * 1.1, 0.0), LIGNE))
    if p.lune:
        lu = A.lune(jd_tt)
        hz, pt = vers_carte(lu.position)
        if hz.hauteur >= 0:
            astres.append(AstreCiel("Lune", pt[0], pt[1], hz.hauteur, hz.azimut))
            rl = max(2.0, p.diametre_mm * 0.022)
            # Direction du Soleil sur la carte : petit pas sur le grand cercle Lune → Soleil.
            so = A.soleil(jd_tt)
            direction = _direction_vers(lu.position, so, p.lat, tsl, r)
            formes.append(Anneau(pt[0], pt[1], rl - trait, rl, ETOILE))
            bc = croissant(pt[0], pt[1], rl, lu.fraction, direction)
            if bc is not None:
                formes.append(Surfaces([bc], ETOILE))
    avancer(0.7)

    # Horizon, points cardinaux, textes.
    formes.append(Anneau(cx, cy, r, r + max(trait * 2, 1.0 if pour_3d else 0.6), TEXTE))
    if p.cardinaux and police is not None:
        hc = max(h_petit, 4.0 if pour_3d else 2.5)
        rc = r + ecart_cardinal / 2 + max(trait * 2, 0.6)
        for lettre, az in (("N", 0.0), ("E", 90.0), ("S", 180.0), ("O", 270.0)):
            pt = projeter(0.0, az, rc)
            formes.append(Surfaces(texte_centre(police, lettre, hc, cx + pt[0], cy + pt[1] - hc / 2, 0.0), TEXTE))
    if texte_affiche and lignes_texte:
        y = cy - w / 2 - m * 0.2
        for i, t in enumerate(lignes_texte):
            est_titre = i == 0 and titre_present
            ht = h_titre if est_titre else h_petit
            pol = (police_titre or police) if est_titre else police
            y -= ht * 1.45
            if pol is not None:
                absents = pol.absents(t)
                if absents:
                    avert.append(f"Caractères absents de la police : {' '.join(absents)}.")
                surf = texte_centre(pol, t, ht, cx, y, 0.0)
                bt = boite(surf) if surf else None
                if bt is not None and bt[2] - bt[0] > w - 2 * m * 0.6:
                    s = (w - 2 * m * 0.6) / (bt[2] - bt[0])
                    surf = texte_centre(pol, t, ht * s, cx, y, 0.0)
                    if not est_titre:
                        avert.append(f"Texte « {t[:30]} » réduit pour tenir dans la largeur.")
                formes.append(Surfaces(surf, TEXTE))
            y -= ht * 0.45
    contour = cercle(cx, cy, w / 2, 180) if p.forme == "disque" else rect_arrondi(0.0, 0.0, w, h_total, min(8.0, m))
    if visibles == 0:
        avert.append("Aucune étoile au-dessus de l'horizon avec ces réglages.")
    avancer(0.8)
    return CarteCiel(w, h_total, cx, cy, r, p.forme, formes, contour, visibles, astres, lignes_texte, avert)


# ================================================================================================ sorties
def trame(c, pas):
    """Trame d'index au pas ``pas`` (mm) : FOND dans la plaque, VIDE dehors, puis les formes."""
    t = Trame.pour(c.largeur_mm, c.hauteur_mm, pas, FOND)
    if t.l * t.h > PIXELS_TRAME_MAX:
        raise ValueError("Carte trop grande pour cette résolution : choisis une résolution plus basse ou un diamètre plus petit.")
    dy = t.hauteur_mm - c.hauteur_mm
    t.masquer(VIDE, c.contour, dy)
    for f in c.formes:
        if isinstance(f, Disque):
            t.disque(f.x, f.y + dy, f.r, f.classe)
        elif isinstance(f, Trait):
            t.trait(f.xa, f.ya + dy, f.xb, f.yb + dy, f.largeur, f.classe)
        elif isinstance(f, Anneau):
            t.anneau(f.x, f.y + dy, f.r0, f.r1, f.classe)
        else:
            t.polygones([b.translater(0.0, dy) for b in f.boucles], f.classe)
    # Rien ne dépasse de la plaque.
    t.masquer(VIDE, c.contour, dy)
    return t


def gravure(c, px_par_mm, style, t=None):
    """Pixels de gravure (0 = le laser brûle, 255 = rien) au ``px_par_mm`` demandé : (pixels (h, l) uint8, l, h)."""
    if t is None:
        t = trame(c, 1.0 / px_par_mm)
    dy = t.hauteur_mm - c.hauteur_mm
    px = np.full((t.h, t.l), 255, dtype=np.uint8)
    x = (np.arange(t.l, dtype=np.float64) + 0.5) * t.pas
    rx = (x - c.cx) * (x - c.cx)
    bloc = max(1, 4_000_000 // max(1, t.l))
    for j0 in range(0, t.h, bloc):
        j1 = min(t.h, j0 + bloc)
        v = t.v[j0:j1]
        y = (t.hauteur_mm - (np.arange(j0, j1, dtype=np.float64) + 0.5) * t.pas) - dy
        if style == "ciel":
            dans = (rx[None, :] + ((y - c.cy) * (y - c.cy))[:, None]) <= c.rayon * c.rayon
            brule = np.where(dans, v == FOND, v != FOND)
        else:
            brule = v != FOND
        brule &= v != VIDE
        px[j0:j1][brule] = 0
    return px, t.l, t.h


def carte_3d(c, pixel_mm, couleurs):
    """Carte des couleurs d'une plaque 3D (couleurs : fond, étoiles, constellations, textes) : CarteTableau."""
    from .carte import CarteTableau
    if len(couleurs) != 4:
        raise ValueError("Quatre couleurs attendues.")
    t = trame(c, pixel_mm)
    return CarteTableau(t.l, t.h, pixel_mm, t.largeur_mm, t.hauteur_mm, t.v, list(couleurs), [], t.comptes(4))


def decoupe_document(c):
    """Contour de découpe (mm, repère du document laser : origine en haut à gauche, y vers le bas)."""
    return [(c.contour.x[k], c.hauteur_mm - c.contour.y[k]) for k in range(c.contour.n)]


def _chemin(boucles, min_x, max_y):
    """Svg.chemin : « Mx,yLx,y…Z » par boucle, axe y retourné."""
    parties = []
    for b in boucles:
        for k in range(b.n):
            parties.append(("M" if k == 0 else "L") + n(b.x[k] - min_x) + "," + n(max_y - b.y[k]))
        parties.append("Z")
    return "".join(parties)


def _cercle_path(x, y, r):
    return (f"M{n(x - r)},{n(y)}A{n(r)},{n(r)} 0 1,0 {n(x + r)},{n(y)}"
            f"A{n(r)},{n(r)} 0 1,0 {n(x - r)},{n(y)}Z")


SIGNATURE_SVG = "Alpine Makers"


def svg(c, titre):
    """SVG en mm aux couleurs de LightBurn : étoiles, Lune, planètes et textes en noir plein (gravure, remplissage),
    constellations en bleu (#0000FF, mode Ligne), contour de découpe en rouge (#FF0000)."""
    h = c.hauteur_mm
    pleins, lignes, surfaces = [], [], []
    for f in c.formes:
        if isinstance(f, Disque):
            pleins.append(f'<circle cx="{n(f.x)}" cy="{n(h - f.y)}" r="{n(f.r)}"/>')
        elif isinstance(f, Anneau):
            pleins.append(f'<path fill-rule="evenodd" d="{_cercle_path(f.x, h - f.y, f.r1)}{_cercle_path(f.x, h - f.y, f.r0)}"/>')
        elif isinstance(f, Trait):
            lignes.append(f"M{n(f.xa)},{n(h - f.ya)}L{n(f.xb)},{n(h - f.yb)}")
        elif f.boucles:
            surfaces.append(_chemin(f.boucles, 0.0, h))
    trait = next((f.largeur for f in c.formes if isinstance(f, Trait)), 0.3)
    propre = titre.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = ['<?xml version="1.0" encoding="UTF-8"?>\n',
         f'<svg xmlns="http://www.w3.org/2000/svg" width="{n(c.largeur_mm)}mm" height="{n(h)}mm" '
         f'viewBox="0 0 {n(c.largeur_mm)} {n(h)}">\n',
         f"  <title>{propre} — {SIGNATURE_SVG}</title>\n",
         '  <g id="gravure" fill="#000000" stroke="none">' + "".join(pleins)]
    if surfaces:
        s.append(f'<path fill-rule="evenodd" d="{"".join(surfaces)}"/>')
    s.append("</g>\n")
    if lignes:
        s.append(f'  <path id="constellations" fill="none" stroke="#0000FF" stroke-width="{n(trait)}" '
                 f'stroke-linecap="round" d="{"".join(lignes)}"/>\n')
    s.append(f'  <path id="decoupe" fill="none" stroke="#FF0000" stroke-width="0.1" d="{_chemin([c.contour], 0.0, h)}"/>\n')
    s.append("</svg>\n")
    return "".join(s)


def conseils_3d():
    return [
        "Ouvre le 3MF dans Bambu Studio ou OrcaSlicer : fond, étoiles, constellations et textes arrivent chacun sur leur filament.",
        "Place les bobines dans l'AMS dans l'ordre indiqué (N° 1 = fond du ciel).",
        "Hauteur de couche 0,2 mm (0,12 mm pour la couche couleur), buse 0,4 mm ; les plus petites étoiles font 0,8 mm.",
        "Coche « Face contre le plateau » pour un ciel lisse et brillant une fois la plaque retournée.",
    ]


def conseils_laser(style):
    return [
        "Projet Alpine Laser Studio : la carte gravée (calque Image en Relais, à la taille réelle) et le contour de découpe en rouge.",
        "SVG : étoiles et textes en noir (remplissage), constellations en bleu (mode Ligne), découpe en rouge, comme dans LightBurn.",
        "Ciel gravé : prévois une puissance douce et un intervalle de 0,1 mm, le disque entier est gravé." if style == "ciel"
        else "Étoiles gravées : rapide, idéal sur bois clair, cuir ou ardoise (en négatif sur l'ardoise).",
    ]
