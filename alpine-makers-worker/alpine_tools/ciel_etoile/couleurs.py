# -*- coding: utf-8 -*-
"""Couleurs (copie de alpine_tools/tableau_couleur/couleurs.py) : palettes, nuancier, tonalité, quantification.

Portage de C:\\AlpineGift core TableauCouleur.kt (parties « couleurs », « tonalité », « quantification »), lui-même
identique à Alpine Toile Creator (22 palettes, toneValue, curveEval, computeThresholds, quantize).
"""
import math
import re
from dataclasses import dataclass

import numpy as np

VIDE = 255
COULEURS_MIN = 2
COULEURS_MAX = 8


@dataclass(frozen=True)
class Teinte:
    """Une couleur de filament (#RRGGBB) et son nom."""
    hex: str
    nom: str


@dataclass(frozen=True)
class Palette:
    id: str
    nom: str
    couleurs: tuple


_HEX3 = re.compile(r"#?([0-9a-fA-F])([0-9a-fA-F])([0-9a-fA-F])")
_HEX6 = re.compile(r"[0-9a-fA-F]{6}")
_HEX6D = re.compile(r"#[0-9a-fA-F]{6}")


def normaliser_hex(valeur):
    """« abc », « #abc », « aabbcc » → « #AABBCC » ; chaîne vide si invalide."""
    t = str(valeur or "").strip()
    m = _HEX3.fullmatch(t)
    if m:
        r, g, b = m.groups()
        t = f"#{r}{r}{g}{g}{b}{b}"
    if _HEX6.fullmatch(t):
        t = "#" + t
    return t.upper() if _HEX6D.fullmatch(t) else ""


def rgb(hex_):
    c = normaliser_hex(hex_) or "#000000"
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def luminance(hex_):
    """Luminance relative (sRGB linéarisé) : ordre sombre → clair."""
    def lin(c):
        v = c / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb(hex_)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _t(hex_, nom):
    return Teinte(normaliser_hex(hex_), nom)


def _palette(id_, *couleurs):
    return Palette(id_, " · ".join(c.nom for c in couleurs), tuple(couleurs))


# Les 22 palettes de Toile Creator (mêmes identifiants, couleurs et noms), rangées sombre → clair.
PALETTES = (
    _palette("noir-gris-blanc", _t("#141414", "Noir"), _t("#8C8C8C", "Gris"), _t("#F4F4F4", "Blanc")),
    _palette("noir-blanc", _t("#141414", "Noir"), _t("#F4F4F4", "Blanc")),
    _palette("noir-olive-sable-blanc", _t("#141414", "Noir"), _t("#5E6234", "Olive"), _t("#C8B38A", "Sable"), _t("#F4F1E8", "Blanc")),
    _palette("noir-nuit-gris-blanc", _t("#141414", "Noir"), _t("#1E2D5A", "Bleu nuit"), _t("#9AA3AE", "Gris"), _t("#F4F4F4", "Blanc")),
    _palette("noir-violet-azur-blanc", _t("#141414", "Noir"), _t("#5A2A88", "Violet"), _t("#3AA6E0", "Azur"), _t("#F4F4F4", "Blanc")),
    _palette("violet-rose-orange-jaune", _t("#4A1C78", "Violet"), _t("#D3337E", "Rose"), _t("#F08A26", "Orange"), _t("#F8E04A", "Jaune")),
    _palette("noir-violet-rose-blanc", _t("#141414", "Noir"), _t("#5A2A88", "Violet"), _t("#E66AA5", "Rose"), _t("#F7F1F5", "Blanc")),
    _palette("noir-rouge-or-blanc", _t("#141414", "Noir"), _t("#B21F2A", "Rouge"), _t("#D3A63A", "Or"), _t("#F7F3EA", "Blanc")),
    _palette("noir-rouge-orange-jaune", _t("#141414", "Noir"), _t("#BE202E", "Rouge"), _t("#F07A1A", "Orange"), _t("#F8D84A", "Jaune")),
    _palette("noir-vert-anis-blanc", _t("#141414", "Noir"), _t("#1E7838", "Vert"), _t("#B4D234", "Anis"), _t("#F4F4F4", "Blanc")),
    _palette("noir-bleu-glacier-blanc", _t("#141414", "Noir"), _t("#1E5EBE", "Bleu"), _t("#9ED2EC", "Glacier"), _t("#F4F8FB", "Blanc")),
    _palette("prune-violet-lavande-blanc", _t("#3A1535", "Prune"), _t("#6E3EA0", "Violet"), _t("#B6A1E0", "Lavande"), _t("#F6F3FB", "Blanc")),
    _palette("noir-rouge-blanc", _t("#141414", "Noir"), _t("#BE202E", "Rouge"), _t("#F4F4F4", "Blanc")),
    _palette("noir-jaune-blanc", _t("#141414", "Noir"), _t("#E8B812", "Jaune"), _t("#F7F7F7", "Blanc")),
    _palette("noir-bleu-blanc", _t("#141414", "Noir"), _t("#1E5EBE", "Bleu"), _t("#F4F4F4", "Blanc")),
    _palette("noir-vert-blanc", _t("#141414", "Noir"), _t("#1E7838", "Vert"), _t("#F4F4F4", "Blanc")),
    _palette("noir-orange-blanc", _t("#141414", "Noir"), _t("#E8701A", "Orange"), _t("#F4F4F4", "Blanc")),
    _palette("noir-rose-blanc", _t("#141414", "Noir"), _t("#D3337E", "Rose"), _t("#F7F1F5", "Blanc")),
    _palette("noir-anthracite-perle-blanc", _t("#111111", "Noir"), _t("#4A4A4A", "Anthracite"), _t("#B0B0B0", "Perle"), _t("#F4F4F4", "Blanc")),
    _palette("marron-caramel-creme", _t("#3A2316", "Marron"), _t("#A8673A", "Caramel"), _t("#F0E2C6", "Crème")),
    _palette("nuit-turquoise-blanc", _t("#13234A", "Bleu nuit"), _t("#1AA0A0", "Turquoise"), _t("#F4F4F4", "Blanc")),
    _palette("foret-sauge-creme", _t("#1D3B2A", "Vert forêt"), _t("#7F9C7A", "Sauge"), _t("#EFE9D6", "Crème")),
)
PALETTES_PAR_ID = {p.id: p for p in PALETTES}
# Palette de départ de l'écran Tableau de Gift (TableauUi.kt : PALETTES[7]).
PALETTE_DEFAUT = PALETTES[7]

# Nuancier de filaments courants (choix rapide d'une couleur).
NUANCIER = (
    _t("#141414", "Noir"), _t("#4A4A4A", "Anthracite"), _t("#8C8C8C", "Gris"), _t("#F4F4F4", "Blanc"), _t("#F0E2C6", "Crème"),
    _t("#3A2316", "Marron"), _t("#A8673A", "Caramel"), _t("#C8B38A", "Sable"), _t("#BE202E", "Rouge"), _t("#E8701A", "Orange"),
    _t("#F8D84A", "Jaune"), _t("#D3A63A", "Or"), _t("#1E7838", "Vert"), _t("#B4D234", "Anis"), _t("#1AA0A0", "Turquoise"),
    _t("#1E5EBE", "Bleu"), _t("#9ED2EC", "Glacier"), _t("#1E2D5A", "Bleu nuit"), _t("#5A2A88", "Violet"), _t("#B6A1E0", "Lavande"),
    _t("#D3337E", "Rose"), _t("#E66AA5", "Rose clair"),
)


def nom_connu(hex_):
    """Nom d'une couleur saisie en hexadécimal : celui du nuancier ou d'une palette, sinon le code lui-même."""
    h = normaliser_hex(hex_)
    for t in NUANCIER:
        if t.hex == h:
            return t.nom
    for p in PALETTES:
        for t in p.couleurs:
            if t.hex == h:
                return t.nom
    return h


def couleur_suivante(actuelles):
    """Couleur du nuancier la plus éloignée (en luminance) des couleurs déjà choisies (TableauUi.couleurSuivante)."""
    prises = {normaliser_hex(t.hex) for t in actuelles}
    meilleure, score = None, None
    for t in NUANCIER:
        if t.hex in prises:
            continue
        lt = math.sqrt(luminance(t.hex))
        s = min((abs(math.sqrt(luminance(c.hex)) - lt) for c in actuelles), default=1.0)
        if score is None or s > score:
            meilleure, score = t, s
    return meilleure or Teinte("#808080", "Gris")


def fusion(couleurs, auto=True):
    """Couleurs du tableau (hex normalisé, doublons retirés) et correspondance palier → index (mergePalettes) :
    rangées sombre → clair si ``auto`` ; sinon dans l'ordre donné."""
    vues = {}
    for c in couleurs:
        h = normaliser_hex(c.hex)
        if h and h not in vues:
            vues[h] = Teinte(h, (c.nom or h)[:40])
    liste = list(vues.values())
    if auto:
        liste = [c for _, _, c in sorted(((luminance(c.hex), i, c) for i, c in enumerate(liste)), key=lambda e: (e[0], e[1]))]
    hexes = [c.hex for c in liste]
    carte = [hexes.index(normaliser_hex(c.hex)) if normaliser_hex(c.hex) in hexes else -1 for c in couleurs]
    return liste, carte


# ================================================================================================ tonalité
def clamp(v, lo, hi):
    return min(hi, max(lo, v))


def js_round(v):
    return math.floor(v + 0.5)


def tonalite(v, luminosite, contraste, gamma):
    """Tonalité d'une valeur 0..1 : gamma, luminosité, contraste (toneValue de Toile Creator)."""
    g = clamp(gamma if math.isfinite(gamma) else 1.0, 0.2, 5.0)
    b = clamp(luminosite, -100.0, 100.0) / 100.0
    cc = clamp(contraste, -100.0, 100.0) / 100.0
    out = clamp(v, 0.0, 1.0)
    if g != 1.0:
        out = math.pow(out, 1 / g)
    out += b * 0.4
    f = 1 + cc * 2 if cc >= 0 else 1 + cc * 0.9
    out = (out - 0.5) * f + 0.5
    return clamp(out, 0.0, 1.0)


def trier_courbe(points):
    pts = sorted(([clamp(p[0] if len(p) > 0 else 0.0, 0.0, 1.0), clamp(p[1] if len(p) > 1 else 0.0, 0.0, 1.0)]
                  for p in (points or [])), key=lambda p: p[0])
    out = []
    for p in pts:
        if out and abs(out[-1][0] - p[0]) < 1e-6:
            out[-1] = p
        else:
            out.append(p)
    if not out or out[0][0] > 0:
        out.insert(0, [0.0, out[0][1] if out else 0.0])
    if out[-1][0] < 1:
        out.append([1.0, out[-1][1] if out else 1.0])
    return out


def courbe(points, x_in):
    """Courbe monotone (Fritsch–Carlson), comme curveEval de Toile Creator."""
    p = trier_courbe(points)
    n = len(p)
    if n == 1:
        return p[0][1]
    x = clamp(x_in, 0.0, 1.0)
    d = [(p[i + 1][1] - p[i][1]) / max(1e-9, p[i + 1][0] - p[i][0]) for i in range(n - 1)]
    m = [0.0] * n
    m[0] = d[0]
    m[n - 1] = d[n - 2]
    for i in range(1, n - 1):
        m[i] = 0.0 if d[i - 1] * d[i] <= 0 else (d[i - 1] + d[i]) / 2
    for i in range(n - 1):
        if d[i] == 0.0:
            m[i] = 0.0
            m[i + 1] = 0.0
            continue
        a = m[i] / d[i]
        b = m[i + 1] / d[i]
        s = a * a + b * b
        if s > 9:
            tt = 3 / math.sqrt(s)
            m[i] = tt * a * d[i]
            m[i + 1] = tt * b * d[i]
    k = 0
    while k < n - 2 and x > p[k + 1][0]:
        k += 1
    h = p[k + 1][0] - p[k][0]
    if h <= 0:
        return p[k][1]
    tt = (x - p[k][0]) / h
    t2 = tt * tt
    t3 = t2 * tt
    y = ((2 * t3 - 3 * t2 + 1) * p[k][1] + (t3 - 2 * t2 + tt) * h * m[k] + (-2 * t3 + 3 * t2) * p[k + 1][1]
         + (t3 - t2) * h * m[k + 1])
    return clamp(y, 0.0, 1.0)


DIAGONALE = ([0.0, 0.0], [1.0, 1.0])


def table(luminosite, contraste, gamma, points=None):
    """Table 256 entrées : gris → tonalité → courbe → 0..255 (buildLut)."""
    pts = trier_courbe(points if points is not None else DIAGONALE)
    return np.array([int(js_round(courbe(pts, tonalite(i / 255.0, luminosite, contraste, gamma)) * 255)) for i in range(256)],
                    dtype=np.int64)


# ================================================================================================ quantification
def seuils(valeurs, valide, n_in, egale):
    """Seuils croissants (n − 1) : index(v) = nombre de seuils ≤ v. Égale = quantiles ; sinon paliers égaux
    (computeThresholds)."""
    n = min(16, max(1, int(n_in)))

    def lineaire():
        return [int(math.ceil(256.0 * (k + 1) / n)) for k in range(n - 1)]

    if n <= 1:
        return []
    if not egale:
        return lineaire()
    v = np.asarray(valeurs, np.uint8).ravel()
    if valide is not None:
        v = v[np.asarray(valide, bool).ravel()]
    hist = np.bincount(v, minlength=256).astype(np.float64).tolist()
    total = float(sum(hist))
    if total == 0.0:
        return lineaire()
    out = [0] * (n - 1)
    dessous = 0.0
    niveau = 0
    for k in range(1, n):
        cible = total * k / n
        while niveau < 255 and dessous + hist[niveau] <= cible:
            dessous += hist[niveau]
            niveau += 1
        prec = out[k - 2] if k > 1 else 0
        haut = dessous + hist[niveau]
        s = niveau if cible - dessous <= haut - cible else niveau + 1
        out[k - 1] = clamp(max(max(s, prec), 1), 1, 255)
    return out


def palier(v, seuils_):
    return sum(1 for s in seuils_ if v >= s)


def quantifier(valeurs, valide, seuils_, carte=None):
    """Valeurs 0..255 → index de couleur (via ``carte`` palier → index), VIDE hors forme."""
    lut = np.empty(256, np.uint8)
    for v in range(256):
        n = palier(v, seuils_)
        if carte is None:
            lut[v] = n
        else:
            i = carte[n] if 0 <= n < len(carte) else -1
            lut[v] = i if 0 <= i <= 254 else VIDE
    out = lut[np.asarray(valeurs, np.uint8)]
    if valide is not None:
        out = np.where(np.asarray(valide, bool), out, np.uint8(VIDE)).astype(np.uint8)
    return out
