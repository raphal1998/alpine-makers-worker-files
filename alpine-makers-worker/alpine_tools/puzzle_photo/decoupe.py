# -*- coding: utf-8 -*-
"""Découpe du puzzle, portage exact de Puzzle.kt (Alpine Gift 1.0.10) : grille selon l'âge, coins décalés, bords à
tenons classiques (courbes de Bézier propres à Alpine Gift, aléa tiré de la graine), lignes communes, SVG laser.

Mêmes opérations flottantes, dans le même ordre, que l'application : les pièces, les tenons et le SVG de découpe sont
identiques à ceux d'Alpine Gift pour les mêmes réglages (recoupé avec le cœur Kotlin). Les pièces voisines partagent
exactement les mêmes points de bord : elles pavent le rectangle. Repère : mm, y vers le haut, origine en bas à gauche.
"""
import math

import numpy as np

from . import geometrie as G
from . import kotlin as K

NOM_PRENOM = "Prénom"
COTE_MIN = 10.0
PIECES_MAX = 600
PRENOM_MAX = 24

# AgePuzzle : libellé, nombre de pièces visé, côté minimal d'une pièce (mm).
AGES = {
    "2-3": ("2–3 ans", 4, 50.0),
    "4-5": ("4–5 ans", 12, 38.0),
    "6-7": ("6–7 ans", 24, 28.0),
    "8-10": ("8–10 ans", 48, 22.0),
    "11+": ("11 ans et plus", 96, 16.0),
    "adulte": ("Adulte", 192, 12.0),
}

# ModeDecoupePuzzle
MODES = {"lignes": "Lignes communes (une passe par bord)", "pieces": "Contour de chaque pièce"}


class Trace:
    """Polyligne de découpe (mm, y vers le haut) : listes de flottants, fermée ou non."""

    __slots__ = ("x", "y", "ferme")

    def __init__(self, x, y, ferme):
        self.x, self.y, self.ferme = list(x), list(y), bool(ferme)

    @property
    def n(self):
        return len(self.x)


class Piece:
    """Pièce : colonne depuis la gauche, ligne depuis le bas, contour fermé (tableau (n, 2), sens direct)."""

    __slots__ = ("colonne", "ligne", "contour", "prenom")

    def __init__(self, colonne, ligne, contour, prenom):
        self.colonne, self.ligne, self.contour, self.prenom = colonne, ligne, contour, prenom


class Decoupe:
    def __init__(self, p, colonnes, lignes, pieces, lignes_communes, prenom, avertissements):
        self.p = p
        self.colonnes, self.lignes = colonnes, lignes
        self.largeur_mm, self.hauteur_mm = p["largeur_mm"], p["hauteur_mm"]
        self.pieces = pieces
        self.lignes_communes = lignes_communes
        self.prenom = prenom
        self.avertissements = avertissements

    def traces(self, mode):
        if mode == "lignes":
            return self.lignes_communes
        return [Trace(pc.contour[:, 0].tolist(), pc.contour[:, 1].tolist(), True) for pc in self.pieces]

    def longueur(self, mode):
        """Longueur de découpe (mm), somme dans l'ordre de l'application."""
        total = 0.0
        for t in self.traces(mode):
            l = 0.0
            n = t.n
            fin = n if t.ferme else n - 1
            xs, ys = t.x, t.y
            for i in range(fin):
                j = (i + 1) % n
                l += K.hypot(xs[j] - xs[i], ys[j] - ys[i])
            total += l
        return total


def grille(largeur, hauteur, age, colonnes=0, lignes=0):
    """Puzzle.grille : colonnes et lignes selon l'âge (pièces presque carrées, côté minimal respecté), note si réduit."""
    if colonnes > 0 or lignes > 0:
        if not (2 <= colonnes <= 40 and 2 <= lignes <= 40):
            raise ValueError("De 2 à 40 colonnes et lignes.")
        return colonnes, lignes, None
    libelle, n, cote_min = AGES[age]
    c = max(2, K.arrondi(math.sqrt(n * largeur / hauteur)))
    l = max(2, K.arrondi(float(n) / c))
    reduit = False
    while min(largeur / c, hauteur / l) < cote_min and (c > 2 or l > 2):
        if largeur / c <= hauteur / l and c > 2:
            c -= 1
        elif l > 2:
            l -= 1
        else:
            c -= 1
        reduit = True
    note = (f"Puzzle petit pour {libelle} : {c * l} pièces au lieu de {n} (pièces de {K.arrondi(cote_min)} mm au moins)."
            if reduit else None)
    return c, l, note


def decouper(p, police=None):
    """Puzzle.decouper. ``p`` : largeur_mm, hauteur_mm, age, colonnes, lignes, graine, alea, prenom, colonne_prenom,
    ligne_prenom (−1 : pièce du centre). ``police`` : module de police (``ligne``, ``absents``) pour le prénom."""
    if not (50.0 <= p["largeur_mm"] <= 600.0 and 50.0 <= p["hauteur_mm"] <= 600.0):
        raise ValueError("Puzzle de 50 à 600 mm de côté.")
    if not (0.0 <= p["alea"] <= 1.0):
        raise ValueError("Irrégularité de 0 à 1.")
    c, l, note = grille(p["largeur_mm"], p["hauteur_mm"], p["age"], p.get("colonnes", 0), p.get("lignes", 0))
    if c * l > PIECES_MAX:
        raise ValueError(f"{PIECES_MAX} pièces au plus.")
    if min(p["largeur_mm"] / c, p["hauteur_mm"] / l) < COTE_MIN:
        raise ValueError(f"Pièces trop petites ({K.arrondi(COTE_MIN)} mm au moins) : moins de colonnes ou de lignes.")
    prenom = (p.get("prenom") or "").strip()
    if prenom and police is None:
        raise ValueError("Choisis une police pour le prénom.")
    if len(prenom) > PRENOM_MAX:
        raise ValueError(f"Prénom de {PRENOM_MAX} caractères au plus.")
    return _Decoupeur(p, c, l, police, prenom, note).calculer()


class _Decoupeur:
    def __init__(self, p, c, l, police, prenom, note):
        self.p, self.c, self.l, self.police, self.prenom, self.note = p, c, l, police, prenom, note
        self.w = p["largeur_mm"] / c
        self.h = p["hauteur_mm"] / l
        self.s = min(self.w, self.h)
        self.r = K.AleaKotlin(p["graine"])
        self.cx = [[0.0] * (l + 1) for _ in range(c + 1)]
        self.cy = [[0.0] * (l + 1) for _ in range(c + 1)]

    def _alea(self):
        return self.r.next_double() * 2 - 1

    def calculer(self):
        c, l, cx, cy = self.c, self.l, self.cx, self.cy
        self._coins()
        hz = [[self._bord(cx[i][j], cy[i][j], cx[i + 1][j], cy[i + 1][j], 1 <= j < l) for i in range(c)] for j in range(l + 1)]
        vt = [[self._bord(cx[i][j], cy[i][j], cx[i][j + 1], cy[i][j + 1], 1 <= i < c) for j in range(l)] for i in range(c + 1)]
        cp, lp = self.p.get("colonne_prenom", -1), self.p.get("ligne_prenom", -1)
        ip = -1 if not self.prenom else (cp if 0 <= cp < c else c // 2)
        jp = -1 if not self.prenom else (l - 1 - lp if 0 <= lp < l else (l - 1) // 2)
        pieces = []
        for j in range(l):
            for i in range(c):
                xs, ys = [], []
                self._suivre(hz[j][i], False, xs, ys)
                self._suivre(vt[i + 1][j], False, xs, ys)
                self._suivre(hz[j + 1][i], True, xs, ys)
                self._suivre(vt[i][j], True, xs, ys)
                pieces.append(Piece(i, j, np.column_stack([np.asarray(xs, dtype=np.float64), np.asarray(ys, dtype=np.float64)]),
                                    i == ip and j == jp))
        L, H = self.p["largeur_mm"], self.p["hauteur_mm"]
        traces = [Trace([0.0, L, L, 0.0], [0.0, 0.0, H, H], True)]
        for j in range(1, l):
            traces.append(self._joindre(hz[j]))
        for i in range(1, c):
            traces.append(self._joindre(vt[i]))
        avert = [self.note] if self.note else []
        texte = self._placer_prenom(ip, jp, avert) if ip >= 0 else []
        return Decoupe(self.p, c, l, pieces, traces, texte, avert)

    @staticmethod
    def _suivre(b, inverse, xs, ys):
        """Ajoute un bord au contour sans son dernier point (qui commence le bord suivant)."""
        bx, by = b
        n = len(bx)
        if inverse:
            xs.extend(bx[n - 1:0:-1])
            ys.extend(by[n - 1:0:-1])
        else:
            xs.extend(bx[:n - 1])
            ys.extend(by[:n - 1])

    def _coins(self):
        """Coins de la grille décalés au hasard ; ceux du bord glissent seulement le long du bord (bords droits)."""
        p, c, l, w, h = self.p, self.c, self.l, self.w, self.h
        amp = 0.06 * p["alea"] * self.s
        for i in range(c + 1):
            for j in range(l + 1):
                x = i * w
                y = j * h
                dx = self._alea() * amp
                dy = self._alea() * amp
                if 1 <= i < c:
                    x += dx
                if 1 <= j < l:
                    y += dy
                self.cx[i][j] = x
                self.cy[i][j] = y

    def _bord(self, ax, ay, bx, by, tenon):
        """Polyligne d'un bord de A à B : droite sur le cadre, à tenon (côté tiré au hasard) à l'intérieur."""
        if not tenon:
            return [ax, bx], [ay, by]
        longueur = K.hypot(bx - ax, by - ay)
        ux = (bx - ax) / longueur
        uy = (by - ay) / longueur
        sens = 1.0 if self.r.next_boolean() else -1.0
        vx = -uy * sens
        vy = ux * sens
        a = self.p["alea"]
        uc = longueur * (0.5 + 0.06 * a * self._alea())
        k = self.s * (1.0 + 0.1 * a * self._alea())
        vague = 0.03 * a * self._alea() * k
        e = uc - 0.10 * k
        f = uc + 0.10 * k
        # Points de contrôle (u le long du bord, v vers le tenon) : épaule, col, tête, col, épaule.
        ctrl = (
            (0.0, 0.0, 0.35 * e, vague, 0.75 * e, -0.02 * k, e, 0.0),
            (e, 0.0, uc - 0.085 * k, 0.03 * k, uc - 0.065 * k, 0.065 * k, uc - 0.075 * k, 0.10 * k),
            (uc - 0.075 * k, 0.10 * k, uc - 0.16 * k, 0.15 * k, uc - 0.13 * k, 0.25 * k, uc, 0.25 * k),
            (uc, 0.25 * k, uc + 0.13 * k, 0.25 * k, uc + 0.16 * k, 0.15 * k, uc + 0.075 * k, 0.10 * k),
            (uc + 0.075 * k, 0.10 * k, uc + 0.065 * k, 0.065 * k, uc + 0.085 * k, 0.03 * k, f, 0.0),
            (f, 0.0, f + 0.25 * (longueur - f), -0.02 * k, f + 0.65 * (longueur - f), -vague, longueur, 0.0),
        )
        xs, ys = [ax], [ay]
        dernier = len(ctrl) - 1
        for q, b in enumerate(ctrl):
            corde = K.hypot(b[6] - b[0], b[7] - b[1]) + K.hypot(b[2] - b[0], b[3] - b[1]) + K.hypot(b[6] - b[4], b[7] - b[5])
            nb = max(3, int(math.ceil(corde / 0.5)))
            for t in range(1, nb + 1):
                tt = float(t) / nb
                m = 1 - tt
                u = m * m * m * b[0] + 3 * m * m * tt * b[2] + 3 * m * tt * tt * b[4] + tt * tt * tt * b[6]
                v = m * m * m * b[1] + 3 * m * m * tt * b[3] + 3 * m * tt * tt * b[5] + tt * tt * tt * b[7]
                if q == dernier and t == nb:
                    xs.append(bx)
                    ys.append(by)
                else:
                    xs.append(ax + u * ux + v * vx)
                    ys.append(ay + u * uy + v * vy)
        return xs, ys

    @staticmethod
    def _joindre(bords):
        """Ligne commune d'un bord du cadre à l'autre."""
        xs, ys = [], []
        for k, (bx, by) in enumerate(bords):
            debut = 0 if k == 0 else 1
            xs.extend(bx[debut:])
            ys.extend(by[debut:])
        return Trace(xs, ys, False)

    def _placer_prenom(self, i, j, avert):
        """Prénom centré dans le cœur de sa pièce (hors tenons), réduit s'il est trop long."""
        pol = self.police
        cx, cy, w, h, s = self.cx, self.cy, self.w, self.h, self.s
        mx = (cx[i][j] + cx[i + 1][j] + cx[i][j + 1] + cx[i + 1][j + 1]) / 4
        my = (cy[i][j] + cy[i + 1][j] + cy[i][j + 1] + cy[i + 1][j + 1]) / 4
        largeur_dispo = w - 0.7 * s
        hauteur_dispo = h - 0.7 * s
        hauteur = min(0.32 * s, hauteur_dispo * 0.6)
        boucles = pol.ligne(self.prenom, hauteur)[0]
        if not boucles:
            raise ValueError("Le prénom n'a aucun caractère dessinable dans cette police.")
        bt = G.boite(boucles)
        if bt[2] - bt[0] > largeur_dispo:
            hauteur *= largeur_dispo / (bt[2] - bt[0])
            boucles = pol.ligne(self.prenom, hauteur)[0]
            bt = G.boite(boucles)
            if hauteur < 3.0:
                avert.append(f"Prénom long : lettres de {K.fr(hauteur, 1)} mm seulement ; agrandis le puzzle ou choisis "
                             "moins de pièces.")
        absents = pol.absents(self.prenom)
        if absents:
            avert.append("Caractères absents de la police : " + " ".join(absents) + ".")
        dx = mx - (bt[0] + bt[2]) / 2
        dy = my - (bt[1] + bt[3]) / 2
        return [np.asarray(b, dtype=np.float64) + np.array([dx, dy]) for b in boucles]


# --- Sorties laser ---------------------------------------------------------------------------------------------------
def _chemin(t, h):
    """Chemin SVG d'une polyligne (y retourné : SVG a y vers le bas)."""
    n = K.nombre_svg
    morceaux = []
    for k in range(t.n):
        morceaux.append(("M" if k == 0 else "L") + n(t.x[k]) + "," + n(h - t.y[k]))
    if t.ferme:
        morceaux.append("Z")
    return "".join(morceaux)


def echapper(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def svg(d, mode, titre, png=None, signature="Alpine Tools"):
    """Puzzle.svg : SVG en mm pour LightBurn — calque « gravure » (photo tramée en PNG, noir) et calque « decoupe »
    (traits rouges #FF0000 sans remplissage). Sans ``png`` : découpe seule."""
    import base64
    l, h = d.largeur_mm, d.hauteur_mm
    n = K.nombre_svg
    sortie = ['<?xml version="1.0" encoding="UTF-8"?>\n',
              f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{n(l)}mm" '
              f'height="{n(h)}mm" viewBox="0 0 {n(l)} {n(h)}">\n',
              f"  <title>{echapper(titre)} — {signature}</title>\n"]
    if png is not None:
        src = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        sortie.append('  <g id="gravure" fill="#000000" stroke="none">\n')
        sortie.append(f'    <image x="0" y="0" width="{n(l)}" height="{n(h)}" preserveAspectRatio="none" href="{src}" '
                      f'xlink:href="{src}"/>\n')
        sortie.append("  </g>\n")
    sortie.append('  <g id="decoupe" fill="none" stroke="#FF0000" stroke-width="0.1">\n')
    for t in d.traces(mode):
        sortie.append(f'    <path d="{_chemin(t, h)}"/>\n')
    sortie.append("  </g>\n</svg>\n")
    return "".join(sortie)


def conseils(mode):
    """Puzzle.conseils."""
    return [
        "Grave d'abord la photo (calque noir), puis découpe (calque rouge) sans bouger la plaque.",
        "Lignes communes : chaque bord n'est coupé qu'une fois (plus rapide, moins brûlé)." if mode == "lignes" else
        "Contour de chaque pièce : les bords communs sont coupés deux fois (coupe plus franche, plus de brûlure).",
        "Contreplaqué bouleau 3 mm : règle la découpe en une ou deux passes ; un ruban de masquage limite les traces de fumée.",
        "Pour les petits enfants, ponce légèrement les bords et préfère une finition sans solvant.",
    ]


def plaque_prenom(d):
    """Rectangle de la plaque claire du prénom (mm) : boîte du texte élargie d'un quart de sa hauteur + 1 mm."""
    bt = G.boite(d.prenom)
    marge = 0.25 * (bt[3] - bt[1]) + 1.0
    return bt[0] - marge, bt[1] - marge, bt[2] + marge, bt[3] + marge


def masques_prenom(d, largeur_px, hauteur_px, px_par_mm):
    """Pixels de la plaque et des lettres du prénom (centres des pixels, règle pair-impair), comme Puzzle.inscrire.
    Rend (tranche_y, tranche_x, plaque, lettres) ou None."""
    x0, y0, x1, y1 = plaque_prenom(d)
    H = d.hauteur_mm
    px0 = max(0, int(x0 * px_par_mm))
    px1 = min(largeur_px - 1, int(x1 * px_par_mm) + 1)
    py0 = max(0, int((H - y1) * px_par_mm))
    py1 = min(hauteur_px - 1, int((H - y0) * px_par_mm) + 1)
    if px1 < px0 or py1 < py0:
        return None
    xs = (np.arange(px0, px1 + 1) + 0.5) / px_par_mm
    ys = H - (np.arange(py0, py1 + 1) + 0.5) / px_par_mm
    X, Y = np.meshgrid(xs, ys)
    plaque = (X >= x0) & (X <= x1) & (Y >= y0) & (Y <= y1)
    lettres = np.zeros(X.shape, dtype=bool)
    for b in d.prenom:
        bb = G.boite_une(b)
        sel = plaque & (X >= bb[0]) & (X <= bb[2]) & (Y >= bb[1]) & (Y <= bb[3])
        if sel.any():
            lettres[sel] ^= G.contient_vec(b, X[sel], Y[sel])
    return slice(py0, py1 + 1), slice(px0, px1 + 1), plaque, lettres


def inscrire(d, image, px_par_mm):
    """Puzzle.inscrire : plaque claire et prénom noir dans l'image (float32 (h, l), y vers le bas)."""
    m = masques_prenom(d, image.shape[1], image.shape[0], px_par_mm)
    if m is None:
        return
    sy, sx, plaque, lettres = m
    zone = image[sy, sx]
    zone[plaque] = np.where(lettres[plaque], np.float32(0.0), np.float32(1.0))
