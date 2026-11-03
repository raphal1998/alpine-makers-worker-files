# -*- coding: utf-8 -*-
"""Feuille de repères A4 (portage de FeuilleReperes.kt d'Alpine Gift) : quatre carrés noirs de 12 mm aux coins et un
cadre de dessin de 160 × 240 mm. Photographiée de travers, elle est redressée par homographie : le dessin est retrouvé
à sa vraie taille, sans perspective. Détection : seuil d'Otsu, composantes connexes, quatre carrés pleins les plus
proches des coins de la photo. Feuille à imprimer : SVG (celui de l'application) et PDF à imprimer à 100 %.
"""
import math

import numpy as np

from . import composantes as K
from .image import F32, echantillon_points, otsu_feuille, redimensionner

LARGEUR = 210.0
HAUTEUR = 297.0
COTE = 12.0
# Centres des repères (mm, origine en haut à gauche) : haut gauche, haut droite, bas droite, bas gauche.
CENTRES = [(15.0, 15.0), (195.0, 15.0), (195.0, 282.0), (15.0, 282.0)]
# Cadre de dessin (mm) : x0, y0, x1, y1.
CADRE = (25.0, 32.0, 185.0, 272.0)
PAS_REDRESSE = 0.25


class Homographie:
    """(x, y) → ((a x + b y + c) / (g x + h y + 1), (d x + e y + f) / (g x + h y + 1))."""

    def __init__(self, h):
        self.h = [float(v) for v in h]

    def appliquer(self, x, y):
        h = self.h
        w = h[6] * x + h[7] * y + 1.0
        return (h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w

    @staticmethod
    def depuis(de, vers):
        """Homographie qui envoie les quatre points ``de`` sur ``vers`` (élimination de Gauss, pivot partiel)."""
        if len(de) != 4 or len(vers) != 4:
            raise ValueError("Quatre points attendus.")
        a = []
        for (x, y), (u, v) in zip(de, vers):
            x, y, u, v = float(x), float(y), float(u), float(v)
            a.append([x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y, u])
            a.append([0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y, v])
        for c in range(8):
            piv = c
            for r in range(c + 1, 8):
                if abs(a[r][c]) > abs(a[piv][c]):
                    piv = r
            a[c], a[piv] = a[piv], a[c]
            if not abs(a[c][c]) > 1e-12:
                raise ValueError("Repères alignés : redressement impossible.")
            for r in range(8):
                if r != c:
                    f = a[r][c] / a[c][c]
                    for k in range(c, 9):
                        a[r][k] -= f * a[c][k]
        return Homographie([a[i][8] / a[i][i] for i in range(8)])


def taches(sombre):
    """Composantes 4-connexes des pixels sombres qui ne touchent pas le bord : (cx, cy, aire, x0, y0, x1, y1),
    dans l'ordre de découverte."""
    et, n = K.etiqueter(sombre, False)
    if n == 0:
        return []
    h, l = sombre.shape
    lab = et.ravel()
    sel = lab > 0
    ys, xs = np.divmod(np.flatnonzero(sel), l)
    e = lab[sel]
    aire = np.bincount(e, minlength=n + 1)
    sx = np.bincount(e, weights=xs.astype(np.float64), minlength=n + 1)
    sy = np.bincount(e, weights=ys.astype(np.float64), minlength=n + 1)
    x0 = np.full(n + 1, l, dtype=np.int64)
    y0 = np.full(n + 1, h, dtype=np.int64)
    x1 = np.zeros(n + 1, dtype=np.int64)
    y1 = np.zeros(n + 1, dtype=np.int64)
    np.minimum.at(x0, e, xs)
    np.minimum.at(y0, e, ys)
    np.maximum.at(x1, e, xs)
    np.maximum.at(y1, e, ys)
    bord = K.touche_bord(et, n)
    r = []
    for k in range(1, n + 1):
        if bord[k]:
            continue
        a = int(aire[k])
        r.append((sx[k] / a + 0.5, sy[k] / a + 0.5, a, int(x0[k]), int(y0[k]), int(x1[k]), int(y1[k])))
    return r


def convexe(p):
    """Quadrilatère convexe parcouru dans un seul sens."""
    signe = 0
    for i in range(4):
        a, b, c = p[i], p[(i + 1) % 4], p[(i + 2) % 4]
        z = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        sg = 1 if z > 0 else (-1 if z < 0 else 0)
        if sg == 0:
            return False
        if signe == 0:
            signe = sg
        elif sg != signe:
            return False
    return True


def _arrondi(v):
    """Math.round de Kotlin (roundToInt) : arrondi au plus proche, demi vers +∞."""
    return int(math.floor(v + 0.5))


def detecter(photo):
    """Centres des quatre repères (pixels de ``photo``) dans l'ordre de CENTRES, ou None si la feuille n'est pas
    reconnue."""
    hp, lp = photo.shape
    s = min(1.0, 900.0 / max(lp, hp))
    l, h = max(8, _arrondi(lp * s)), max(8, _arrondi(hp * s))
    g = redimensionner(photo, l, h)
    seuil = min(otsu_feuille(g), float(F32(0.45)))
    sombre = g < F32(seuil)
    cote = float(min(l, h))
    candidats = []
    for t in taches(sombre):
        bw, bh = t[5] - t[3] + 1, t[6] - t[4] + 1
        ratio = bw / bh
        if t[2] >= 25 and 0.55 <= ratio <= 1.8 and t[2] >= 0.6 * bw * bh and max(bw, bh) < cote / 5:
            candidats.append(t)
    if len(candidats) < 4:
        return None
    coins = [(0.0, 0.0), (float(l), 0.0), (float(l), float(h)), (0.0, float(h))]
    choisis = []
    for i, (cx, cy) in enumerate(coins):
        meilleur, valeur = None, math.inf
        for t in candidats:
            gauche = t[0] < l / 2.0
            haut = t[1] < h / 2.0
            if (gauche if i in (0, 3) else not gauche) and (haut if i < 2 else not haut):
                v = math.hypot(t[0] - cx, t[1] - cy) - 0.5 * math.sqrt(t[2])
                if meilleur is None or v < valeur:
                    meilleur, valeur = t, v
        if meilleur is None:
            return None
        choisis.append(meilleur)
    aires = [t[2] for t in choisis]
    if max(aires) > 4 * min(aires):
        return None
    pts = [(t[0] / s, t[1] / s) for t in choisis]
    return pts if convexe(pts) else None


def redresser(photo, centres, pas_mm=PAS_REDRESSE):
    """Cadre de dessin redressé : image de CADRE à ``pas_mm`` mm par pixel, échantillonnée dans ``photo`` dont les
    repères sont en ``centres`` (pixels)."""
    hm = Homographie.depuis(CENTRES, centres).h
    l = _arrondi((CADRE[2] - CADRE[0]) / pas_mm)
    ht = _arrondi((CADRE[3] - CADRE[1]) / pas_mm)
    x = CADRE[0] + (np.arange(l) + 0.5) * pas_mm
    y = CADRE[1] + (np.arange(ht) + 0.5) * pas_mm
    X, Y = np.meshgrid(x, y)
    w = hm[6] * X + hm[7] * Y + 1.0
    px = (hm[0] * X + hm[1] * Y + hm[2]) / w
    py = (hm[3] * X + hm[4] * Y + hm[5]) / w
    hp, lp = photo.shape
    return echantillon_points(photo, px / lp, py / hp)


# ------------------------------------------------------------------ feuille à imprimer
TITRE = "Feuille de repères — Alpine Makers"
LIGNE_HAUT = "Dessine dans le cadre au feutre noir · Alpine Makers"
LIGNE_BAS = "Imprime à 100 % · photographie la feuille entière, à plat, bien éclairée"


def _kt(v):
    """Double.toString de Kotlin pour les valeurs de la feuille (9.0, 160.0…)."""
    return repr(float(v))


def feuille_svg():
    """SVG imprimable de la feuille (à imprimer à 100 %, sans « ajuster à la page ») : FeuilleReperes.feuilleSvg."""
    t = ['<?xml version="1.0" encoding="UTF-8"?>\n',
         '<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" viewBox="0 0 210 297">\n',
         f'  <title>{TITRE}</title>\n  <rect width="210" height="297" fill="#ffffff"/>\n']
    for cx, cy in CENTRES:
        t.append(f'  <rect x="{_kt(cx - COTE / 2)}" y="{_kt(cy - COTE / 2)}" width="{_kt(COTE)}" height="{_kt(COTE)}" fill="#000000"/>\n')
    t.append(f'  <rect x="{_kt(CADRE[0])}" y="{_kt(CADRE[1])}" width="{_kt(CADRE[2] - CADRE[0])}" height="{_kt(CADRE[3] - CADRE[1])}" '
             'fill="none" stroke="#9a9a9a" stroke-width="0.3" stroke-dasharray="2 2"/>\n')
    t.append(f'  <text x="105" y="25" font-family="sans-serif" font-size="4.2" fill="#7a7a7a" text-anchor="middle">{LIGNE_HAUT}</text>\n')
    t.append(f'  <text x="105" y="283" font-family="sans-serif" font-size="3.4" fill="#9a9a9a" text-anchor="middle">{LIGNE_BAS}</text>\n')
    t.append("</svg>\n")
    return "".join(t)


PT = 72.0 / 25.4
# Largeur moyenne d'un caractère d'Helvetica (fraction du corps), pour centrer approximativement une ligne.
_CHASSE = 0.5


def _n(v):
    t = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if t in ("", "-0") else t


def _texte(t):
    o = bytearray(b"(")
    for c in t.encode("cp1252", errors="replace"):
        if c in (0x28, 0x29, 0x5C):
            o.append(0x5C)
        o.append(c)
    o.append(0x29)
    return bytes(o)


def _contenu():
    s = bytearray()

    def w(t):
        s.extend(t.encode("ascii"))

    def rect(x0, y0, l, h):
        w(f"{_n(x0 * PT)} {_n((HAUTEUR - y0 - h) * PT)} {_n(l * PT)} {_n(h * PT)} re f\n")

    w("0 g\n")
    for cx, cy in CENTRES:
        rect(cx - COTE / 2, cy - COTE / 2, COTE, COTE)
    x0, y0, x1, y1 = CADRE
    w(f"q 0.604 G {_n(0.3 * PT)} w [{_n(2 * PT)} {_n(2 * PT)}] 0 d {_n(x0 * PT)} {_n((HAUTEUR - y1) * PT)} "
      f"{_n((x1 - x0) * PT)} {_n((y1 - y0) * PT)} re S Q\n")

    def ligne(gris, taille_mm, y_mm, t):
        corps = taille_mm * PT
        x = 105.0 * PT - len(t) * corps * _CHASSE / 2
        w(f"{gris} g BT /F1 {_n(corps)} Tf {_n(x)} {_n((HAUTEUR - y_mm) * PT)} Td ")
        s.extend(_texte(t))
        w(" Tj ET\n")

    ligne("0.478", 4.2, 25.0, LIGNE_HAUT)
    ligne("0.604", 3.4, 283.0, LIGNE_BAS)
    return bytes(s)


def feuille_pdf():
    """La même feuille en PDF vectoriel, page A4 exacte, /PrintScaling /None, police standard Helvetica."""
    sortie = bytearray()
    decalages = []

    def w(t):
        sortie.extend(t.encode("latin-1"))

    def objet(num, corps):
        decalages.append(len(sortie))
        w(f"{num} 0 obj\n")
        corps()
        w("\nendobj\n")

    w("%PDF-1.6\n%âãÏÓ\n")
    objet(1, lambda: w("<< /Type /Catalog /Pages 2 0 R /ViewerPreferences << /PrintScaling /None >> >>"))
    objet(2, lambda: w("<< /Type /Pages /Kids [3 0 R] /Count 1 >>"))
    objet(3, lambda: w(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_n(LARGEUR * PT)} {_n(HAUTEUR * PT)}] "
                       "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"))
    contenu = _contenu()

    def flux():
        w(f"<< /Length {len(contenu)} >>\nstream\n")
        sortie.extend(contenu)
        w("endstream")

    objet(4, flux)
    objet(5, lambda: w("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"))

    def info():
        w("<< /Title ")
        sortie.extend(_texte("Alpine Makers - feuille de repères A4 (dessin d'enfant)"))
        w(" /Producer (Alpine Makers) >>")

    objet(6, info)
    xref = len(sortie)
    w("xref\n0 7\n0000000000 65535 f \n")
    for d in decalages:
        w(f"{d:010d} 00000 n \n")
    w(f"trailer\n<< /Size 7 /Root 1 0 R /Info 6 0 R >>\nstartxref\n{xref}\n%%EOF\n")
    return bytes(sortie)
