# -*- coding: utf-8 -*-
"""Police « Alpine Ronde » : sans empattement à trait rond, dessinée pour les Alpine Tools (aucune police tierce).

Chaque glyphe est un squelette (tracés polygonaux et arcs) épaissi d'un rayon constant : le contour est la ligne de
niveau zéro du champ ``rayon − distance au squelette``, tirée par marching squares (matière à gauche : extérieurs dans
le sens direct, trous — o, a, e, 8… — dans le sens indirect). Les contours sont donc fermés, sans croisement, et les
unions de traits (jonctions, accents) sont exactes.

Unités du dessin : ligne de base y = 0, hauteur des capitales du squelette 86 (contour 86 + 2 × rayon), x vers la
droite. Les fonctions publiques travaillent en millimètres (porte-clés) ou en pixels (image d'annonce).

Ce fichier est autonome (numpy seulement) : il est copié tel quel dans chaque outil qui écrit du texte.
"""
import math
from functools import lru_cache

import numpy as np

CAP = 86.0          # haut des capitales (squelette)
XH = 60.0           # haut des minuscules (squelette)
ASC = 92.0          # haut des ascendantes (b, d, h, k, l)
DESC = -26.0        # bas des descendantes (g, j, p, q, y)
APPROCHE = 12.0     # blanc entre deux glyphes (unités)
GRAISSES = {"fine": 5.5, "normale": 7.0, "grasse": 9.0}
PAS_DESSIN = 0.5    # pas de la grille du contour d'un glyphe (unités)
TOLERANCE = 0.04    # écart max du contour simplifié (unités)


def _arc(cx, cy, rx, ry, a0, a1):
    """Arc d'ellipse de l'angle a0 à a1 (degrés, sens direct si a1 > a0), un point tous les 5° au plus."""
    n = max(2, int(math.ceil(abs(a1 - a0) / 5.0)) + 1)
    return [(cx + rx * math.cos(math.radians(a)), cy + ry * math.sin(math.radians(a)))
            for a in np.linspace(a0, a1, n)]


def _ellipse(cx, cy, rx, ry):
    return _arc(cx, cy, rx, ry, 0, 360)


_G = {}


def _g(car, largeur, *traits):
    _G[car] = (float(largeur), [list(t) for t in traits])


def _miroir(traits, largeur):
    return [[(largeur - x, y) for x, y in t] for t in traits]


def _decaler(traits, dx, dy=0.0):
    return [[(x + dx, y + dy) for x, y in t] for t in traits]


# --- Capitales -----------------------------------------------------------------------------------------------------
_g("A", 64, [(0, 0), (32, 86), (64, 0)], [(10.4, 28), (53.6, 28)])
_g("B", 54, [(0, 0), (0, 86), (30, 86)] + _arc(30, 65, 21, 21, 90, -90) + [(0, 44)],
   [(0, 44), (32, 44)] + _arc(32, 22, 22, 22, 90, -90) + [(0, 0)])
_g("C", 67, _arc(38, 43, 38, 43, 40, 320))
_g("D", 60, [(0, 0), (0, 86), (22, 86)] + _arc(22, 43, 38, 43, 90, -90) + [(0, 0)])
_g("E", 48, [(48, 86), (0, 86), (0, 0), (48, 0)], [(0, 44), (42, 44)])
_g("F", 48, [(48, 86), (0, 86), (0, 0)], [(0, 44), (42, 44)])
_g("G", 76, _arc(38, 43, 38, 43, 40, 360) + [(46, 43)])
_g("H", 58, [(0, 0), (0, 86)], [(58, 0), (58, 86)], [(0, 44), (58, 44)])
_g("I", 0, [(0, 0), (0, 86)])
_g("J", 40, [(40, 86), (40, 24)] + _arc(20, 24, 20, 24, 0, -165))
_g("K", 56, [(0, 0), (0, 86)], [(52, 86), (0, 32)], [(17, 50), (56, 0)])
_g("L", 46, [(0, 86), (0, 0), (46, 0)])
_g("M", 72, [(0, 0), (0, 86), (36, 22), (72, 86), (72, 0)])
_g("N", 58, [(0, 0), (0, 86), (58, 0), (58, 86)])
_g("O", 84, _ellipse(42, 43, 42, 43))
_g("P", 53, [(0, 0), (0, 86), (30, 86)] + _arc(30, 63, 23, 23, 90, -90) + [(0, 40)])
_g("Q", 86, _ellipse(42, 43, 42, 43), [(54, 20), (86, -8)])
_g("R", 56, [(0, 0), (0, 86), (30, 86)] + _arc(30, 63, 23, 23, 90, -90) + [(0, 40)], [(28, 40), (56, 0)])
_g("S", 56, _arc(28, 64, 26, 22, 25, 270) + _arc(28, 21, 28, 21, 90, -155))
_g("T", 60, [(0, 86), (60, 86)], [(30, 86), (30, 0)])
_g("U", 58, [(0, 86), (0, 30)] + _arc(29, 30, 29, 30, 180, 360) + [(58, 86)])
_g("V", 62, [(0, 86), (31, 0), (62, 86)])
_g("W", 92, [(0, 86), (22, 0), (46, 70), (70, 0), (92, 86)])
_g("X", 58, [(0, 86), (58, 0)], [(0, 0), (58, 86)])
_g("Y", 60, [(0, 86), (30, 42), (60, 86)], [(30, 42), (30, 0)])
_g("Z", 56, [(0, 86), (56, 86), (0, 0), (56, 0)])

# --- Minuscules ----------------------------------------------------------------------------------------------------
_BOL = _ellipse(26, 30, 26, 30)
_g("a", 52, _BOL, [(52, 60), (52, 0)])
_g("b", 52, [(0, ASC), (0, 0)], _BOL)
_g("c", 46, _arc(27, 30, 27, 30, 45, 315))
_g("d", 52, _BOL, [(52, ASC), (52, 0)])
_g("e", 54, [(0, 30), (54, 30)] + _arc(27, 30, 27, 30, 0, 318))
_g("f", 38, [(14, 0), (14, 74)] + _arc(30, 74, 16, 16, 180, 50), [(0, 58), (34, 58)])
_g("g", 52, _BOL, [(52, 60), (52, -6)] + _arc(27, -6, 25, 20, 0, -160))
_g("h", 50, [(0, ASC), (0, 0)], _arc(25, 35, 25, 25, 180, 0) + [(50, 0)])
_g("i", 0, [(0, 0), (0, 60)], [(0, 84)])
_g("j", 16, [(16, 60), (16, -10)] + _arc(4, -10, 12, 16, 0, -170), [(16, 84)])
_g("k", 46, [(0, ASC), (0, 0)], [(44, 60), (0, 22)], [(14, 34), (46, 0)])
_g("l", 0, [(0, ASC), (0, 0)])
_g("m", 84, [(0, 60), (0, 0)], _arc(21, 38, 21, 22, 180, 0) + [(42, 0)], _arc(63, 38, 21, 22, 180, 0) + [(84, 0)])
_g("n", 50, [(0, 60), (0, 0)], _arc(25, 35, 25, 25, 180, 0) + [(50, 0)])
_g("o", 56, _ellipse(28, 30, 28, 30))
_g("p", 52, [(0, 60), (0, DESC)], _BOL)
_g("q", 52, _BOL, [(52, 60), (52, DESC)])
_g("r", 32, [(0, 60), (0, 0)], _arc(24, 36, 24, 24, 180, 70))
_g("s", 44, _arc(22, 44.5, 20, 15.5, 25, 270) + _arc(22, 14.5, 22, 14.5, 90, -155))
_g("t", 36, [(14, 80), (14, 14)] + _arc(30, 14, 16, 14, 180, 290), [(0, 58), (34, 58)])
_g("u", 50, [(0, 60), (0, 25)] + _arc(25, 25, 25, 25, 180, 360), [(50, 60), (50, 0)])
_g("v", 48, [(0, 60), (24, 0), (48, 60)])
_g("w", 72, [(0, 60), (18, 0), (36, 48), (54, 0), (72, 60)])
_g("x", 46, [(0, 60), (46, 0)], [(0, 0), (46, 60)])
_g("y", 50, [(0, 60), (25, 4)], [(50, 60), (12, DESC)])
_g("z", 46, [(0, 60), (44, 60), (0, 0), (46, 0)])
_G["ı"] = (0.0, [[(0, 0), (0, 60)]])  # i sans point (base des i accentués)

# --- Chiffres ------------------------------------------------------------------------------------------------------
_g("0", 54, _ellipse(27, 43, 27, 43))
_g("1", 26, [(4, 70), (26, 86), (26, 0)])
_g("2", 54, _arc(26, 62, 26, 24, 160, -30) + [(0, 0), (54, 0)])
_g("3", 54, _arc(26, 65, 24, 21, 150, -90) + _arc(27, 22, 27, 22, 90, -150))
_g("4", 56, [(40, 0), (40, 86), (0, 24), (56, 24)])
_g("5", 54, [(50, 86), (6, 86), (6, 46)] + _arc(27, 28, 27, 28, 140, -150))
_g("6", 55, _ellipse(27, 27, 27, 27), _arc(44, 27, 44, 59, 180, 75))
_g("7", 54, [(0, 86), (54, 86), (16, 0)])
_g("8", 54, _ellipse(27, 65, 23, 21), _ellipse(27, 22, 27, 22))
_G["9"] = (55.0, [[(55 - x, 86 - y) for x, y in t] for t in _G["6"][1]])

# --- Ponctuation et signes -----------------------------------------------------------------------------------------
_g(" ", 14)
_g(".", 0, [(0, 0)])
_g(",", 4, [(4, 2), (-2, -14)])
_g(":", 0, [(0, 0)], [(0, 52)])
_g(";", 4, [(4, 52)], [(4, 2), (-2, -14)])
_g("!", 0, [(0, 86), (0, 26)], [(0, 0)])
_g("?", 48, _arc(24, 64, 24, 22, 160, -80) + [(24, 40), (24, 26)], [(24, 0)])
_g("'", 0, [(0, 86), (0, 64)])
_g("’", 4, [(4, 86), (-2, 68)])
_g("‘", 4, [(0, 68), (4, 86)])
_g('"', 18, [(0, 86), (0, 64)], [(18, 86), (18, 64)])
_g("-", 30, [(0, 34), (30, 34)])
_g("–", 50, [(0, 34), (50, 34)])
_g("—", 80, [(0, 34), (80, 34)])
_g("_", 56, [(0, -14), (56, -14)])
_g("(", 17, _arc(34, 40, 34, 56, 120, 240))
_G[")"] = (17.0, _miroir(_G["("][1], 17))
_g("[", 16, [(16, 96), (0, 96), (0, -14), (16, -14)])
_G["]"] = (16.0, _miroir(_G["["][1], 16))
_g("/", 40, [(0, -6), (40, 92)])
_g("+", 44, [(0, 36), (44, 36)], [(22, 14), (22, 58)])
_g("=", 44, [(0, 26), (44, 26)], [(0, 48), (44, 48)])
_g("×", 36, [(0, 14), (36, 50)], [(0, 50), (36, 14)])
_g("*", 32, [(16, 86), (16, 54)], [(2, 78), (30, 62)], [(2, 62), (30, 78)])
_g("#", 58, [(14, 4), (22, 82)], [(36, 4), (44, 82)], [(2, 30), (54, 30)], [(6, 56), (58, 56)])
_g("%", 60, _ellipse(11, 72, 11, 14), _ellipse(49, 14, 11, 14), [(4, 0), (56, 86)])
_g("&", 58, [(58, 0), (13, 56)] + _arc(26, 70, 15, 16, 215, -35) + [(8, 30)] + _arc(24, 22, 22, 22, 160, 345) + [(54, 34)])
_g("€", 75, _arc(48, 43, 38, 43, 45, 315), [(0, 52), (44, 52)], [(0, 34), (40, 34)])
_g("$", 56, _arc(28, 64, 26, 22, 25, 270) + _arc(28, 21, 28, 21, 90, -155), [(28, -10), (28, 96)])
_g("«", 34, [(14, 52), (0, 32), (14, 12)], [(34, 52), (20, 32), (34, 12)])
_G["»"] = (34.0, _miroir(_G["«"][1], 34))
_g("°", 16, _ellipse(8, 78, 8, 8))
_g("·", 0, [(0, 34)])
_g("…", 44, [(0, 0)], [(22, 0)], [(44, 0)])
_g("@", 88, _ellipse(44, 30, 18, 20), [(62, 50), (62, 18)] + _arc(72, 18, 10, 12, 180, 360) + _arc(44, 30, 38, 42, 0, 300))


# --- Accents -------------------------------------------------------------------------------------------------------
def _accent(nom, cx, capitale):
    y0 = CAP + 20 if capitale else XH + 26
    h = 12 if capitale else 15
    if nom == "aigu":
        return [[(cx - 6, y0), (cx + 8, y0 + h)]]
    if nom == "grave":
        return [[(cx + 6, y0), (cx - 8, y0 + h)]]
    if nom == "circ":
        return [[(cx - 14, y0), (cx, y0 + h), (cx + 14, y0)]]
    if nom == "trema":
        return [[(cx - 12, y0 + 5)], [(cx + 12, y0 + 5)]]
    if nom == "tilde":
        return [[(cx - 15 + 30 * k / 12, y0 + 5 + 5 * math.sin(math.pi * k / 6)) for k in range(13)]]
    if nom == "cedille":
        return [[(cx, 2), (cx, -6)] + _arc(cx, -14, 9, 8, 90, -140)]
    raise KeyError(nom)


_CENTRES = {"a": 26, "u": 25, "ı": 0, "I": 0, "C": 40, "c": 28, "e": 27, "o": 28, "y": 27}
_COMPOSES = {
    "à": ("a", "grave"), "â": ("a", "circ"), "ä": ("a", "trema"), "á": ("a", "aigu"), "ã": ("a", "tilde"),
    "é": ("e", "aigu"), "è": ("e", "grave"), "ê": ("e", "circ"), "ë": ("e", "trema"),
    "î": ("ı", "circ"), "ï": ("ı", "trema"), "í": ("ı", "aigu"), "ì": ("ı", "grave"),
    "ô": ("o", "circ"), "ö": ("o", "trema"), "ó": ("o", "aigu"), "ò": ("o", "grave"), "õ": ("o", "tilde"),
    "ù": ("u", "grave"), "û": ("u", "circ"), "ü": ("u", "trema"), "ú": ("u", "aigu"),
    "ÿ": ("y", "trema"), "ñ": ("n", "tilde"), "ç": ("c", "cedille"),
    "À": ("A", "grave"), "Â": ("A", "circ"), "Ä": ("A", "trema"), "Á": ("A", "aigu"),
    "É": ("E", "aigu"), "È": ("E", "grave"), "Ê": ("E", "circ"), "Ë": ("E", "trema"),
    "Î": ("I", "circ"), "Ï": ("I", "trema"), "Í": ("I", "aigu"),
    "Ô": ("O", "circ"), "Ö": ("O", "trema"), "Ó": ("O", "aigu"),
    "Ù": ("U", "grave"), "Û": ("U", "circ"), "Ü": ("U", "trema"), "Ú": ("U", "aigu"),
    "Ÿ": ("Y", "trema"), "Ñ": ("N", "tilde"), "Ç": ("C", "cedille"),
}
for _car, (_base, _nom) in _COMPOSES.items():
    _l, _t = _G[_base]
    _cx = _CENTRES.get(_base, _l / 2)
    if _base in ("ı", "I"):  # fût seul : chasse élargie pour que l'accent ne touche pas les voisins
        _l, _t, _cx = 12.0, _decaler(_t, 6), 6
    if _nom == "cedille" and _base == "C":
        _cx = 38
    _G[_car] = (_l, [list(t) for t in _t] + _accent(_nom, _cx, _base.isupper()))
_G["œ"] = (102.0, [list(t) for t in _G["o"][1]] + _decaler(_G["e"][1], 48))
_G["Œ"] = (128.0, [list(t) for t in _G["O"][1]] + _decaler([[(48, 86), (0, 86), (0, 0), (48, 0)], [(0, 44), (42, 44)]], 80))
_G["æ"] = (100.0, [list(t) for t in _G["a"][1]] + _decaler(_G["e"][1], 46))

CARACTERES = frozenset(_G)


def connait(car):
    return car in _G


def absents(texte):
    """Caractères du texte absents de la police (l'espace est toujours connu)."""
    vus = []
    for car in texte:
        if car not in _G and car not in vus and not car.isspace():
            vus.append(car)
    return vus


def _rayon(graisse):
    if isinstance(graisse, (int, float)):
        return float(graisse)
    return GRAISSES.get(str(graisse), GRAISSES["normale"])


def chasse_unites(car, graisse="normale"):
    """Avance du caractère (unités du dessin), approche comprise."""
    r = _rayon(graisse)
    largeur = _G.get(car, _G[" "])[0]
    return largeur + 2 * r + APPROCHE


def hauteur_capitales(graisse="normale"):
    """Hauteur des capitales du contour (unités du dessin)."""
    return CAP + 2 * _rayon(graisse)


def _segments(traits, dx=0.0, dy=0.0):
    """Segments (S, 4) : xa, ya, xb, yb ; un tracé d'un seul point donne un segment nul (un point rond)."""
    seg = []
    for t in traits:
        pts = [(x + dx, y + dy) for x, y in t]
        if len(pts) == 1:
            seg.append((pts[0][0], pts[0][1], pts[0][0], pts[0][1]))
        for a, b in zip(pts, pts[1:]):
            seg.append((a[0], a[1], b[0], b[1]))
    return np.asarray(seg, dtype=np.float64).reshape(-1, 4)


def distance_segments(px, py, seg, bloc=4096):
    """Distance minimale des points (px, py) (tableaux 1D) aux segments ``seg`` (S, 4), calcul par blocs."""
    out = np.full(px.shape[0], np.inf)
    if seg.shape[0] == 0:
        return out
    ax, ay, bx, by = (seg[:, k][None, :] for k in range(4))
    dx = bx - ax
    dy = by - ay
    l2 = dx * dx + dy * dy
    l2s = np.where(l2 > 0, l2, 1.0)
    taille = max(1, bloc * 64 // max(1, seg.shape[0]))
    for d in range(0, px.shape[0], taille):
        x = px[d:d + taille, None]
        y = py[d:d + taille, None]
        t = np.clip(((x - ax) * dx + (y - ay) * dy) / l2s, 0.0, 1.0)
        t = np.where(l2 > 0, t, 0.0)
        ex = x - (ax + t * dx)
        ey = y - (ay + t * dy)
        out[d:d + taille] = np.sqrt((ex * ex + ey * ey).min(axis=1))
    return out


# --- Marching squares orienté (matière à gauche), partagé avec le calcul du porte-clés -------------------------------
def contours_niveau(v, x0, y0, pas):
    """Contours au niveau 0 d'un champ ``v`` (ny, nx) échantillonné en (x0 + i·pas, y0 + j·pas), matière (v > 0) à
    gauche ; la grille est bordée de vide. Cas ambigus tranchés par la moyenne de la cellule. Rend des tableaux (n, 2)."""
    v = np.asarray(v, dtype=np.float64)
    ny, nx = v.shape
    p = np.full((ny + 2, nx + 2), -1.0)
    p[1:-1, 1:-1] = np.where(v == 0.0, -1e-7, v)
    W = nx + 2
    pos = p > 0
    # Cellules (J, I) du tableau bordé : coins BG (J,I), BD (J,I+1), HD (J+1,I+1), HG (J+1,I).
    c0, c1, c2, c3 = pos[:-1, :-1], pos[:-1, 1:], pos[1:, 1:], pos[1:, :-1]
    n_pos = c0.astype(np.int8) + c1 + c2 + c3
    J, I = np.nonzero((n_pos > 0) & (n_pos < 4))
    if J.size == 0:
        return []
    cv = np.stack([p[J, I], p[J, I + 1], p[J + 1, I + 1], p[J + 1, I]], axis=1)  # (M, 4)
    ci = np.stack([I, I + 1, I + 1, I], axis=1)
    cj = np.stack([J, J, J + 1, J + 1], axis=1)
    ids = np.stack([2 * (J * W + I), 2 * (J * W + I + 1) + 1, 2 * ((J + 1) * W + I), 2 * (J * W + I) + 1], axis=1)
    a = cv
    b = np.roll(cv, -1, axis=1)
    coupe = (a > 0) != (b > 0)
    debut = (a > 0) & (b <= 0)
    t = np.where(coupe, a / np.where(coupe, a - b, 1.0), 0.0)
    ci2 = np.roll(ci, -1, axis=1)
    cj2 = np.roll(cj, -1, axis=1)
    # Coordonnées réelles (le tableau bordé décale les indices de 1).
    xa = x0 + (ci - 1) * pas
    ya = y0 + (cj - 1) * pas
    xb = x0 + (ci2 - 1) * pas
    yb = y0 + (cj2 - 1) * pas
    px = xa + t * (xb - xa)
    py = ya + t * (yb - ya)
    points = {}
    sel = coupe.nonzero()
    for idv, x, y in zip(ids[sel].tolist(), px[sel].tolist(), py[sel].tolist()):
        points[idv] = (x, y)
    suivant = {}
    nb_debuts = debut.sum(axis=1)
    simples = nb_debuts == 1
    if simples.any():
        s = np.argmax(debut[simples], axis=1)
        e = np.argmax(coupe[simples] & ~debut[simples], axis=1)
        idx = ids[simples]
        r = np.arange(idx.shape[0])
        for d, f in zip(idx[r, s].tolist(), idx[r, e].tolist()):
            suivant[d] = f
    for m in np.nonzero(~simples)[0].tolist():
        centre = cv[m].mean()
        for k in range(4):
            if debut[m, k]:
                e = (k + 1) % 4 if centre > 0 else (k + 3) % 4
                suivant[int(ids[m, k])] = int(ids[m, e])
    vu = set()
    boucles = []
    for depart in suivant:
        if depart in vu:
            continue
        pts = []
        cle = depart
        while cle is not None and cle not in vu:
            vu.add(cle)
            pts.append(points[cle])
            cle = suivant.get(cle)
        if len(pts) >= 3:
            boucles.append(np.asarray(pts, dtype=np.float64))
    return boucles


def aire(b):
    x, y = b[:, 0], b[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))


def simplifier(b, tolerance, pas_min=0.0):
    """Douglas–Peucker fermé (itératif) après retrait des points à moins de ``pas_min`` du dernier gardé."""
    pts = b
    if pas_min > 0 and len(pts) > 3:
        garde = [0]
        dernier = pts[0]
        for k in range(1, len(pts)):
            if math.hypot(pts[k, 0] - dernier[0], pts[k, 1] - dernier[1]) >= pas_min:
                garde.append(k)
                dernier = pts[k]
        pts = pts[garde]
        if len(pts) > 3 and math.hypot(*(pts[-1] - pts[0])) < pas_min:
            pts = pts[:-1]
    n = len(pts)
    if n < 3:
        return None
    if n < 5:
        return pts
    loin = int(np.argmax(np.hypot(pts[:, 0] - pts[0, 0], pts[:, 1] - pts[0, 1])))
    garde = np.zeros(n, dtype=bool)
    garde[0] = garde[loin] = True
    pile = [(0, loin), (loin, n)]
    while pile:
        a, z = pile.pop()
        if z - a < 2:
            continue
        ax, ay = pts[a]
        bx, by = pts[z % n]
        seg = pts[a + 1:z]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        if l2 == 0:
            d = np.hypot(seg[:, 0] - ax, seg[:, 1] - ay)
        else:
            tt = np.clip(((seg[:, 0] - ax) * dx + (seg[:, 1] - ay) * dy) / l2, 0, 1)
            d = np.hypot(seg[:, 0] - (ax + tt * dx), seg[:, 1] - (ay + tt * dy))
        k = int(np.argmax(d))
        if d[k] > tolerance:
            i = a + 1 + k
            garde[i] = True
            pile.append((a, i))
            pile.append((i, z))
    r = pts[garde]
    return r if len(r) >= 3 else None


@lru_cache(maxsize=512)
def _contours_glyphe(car, rayon):
    """Contours d'un glyphe (unités du dessin), origine : bord gauche de la chasse, ligne de base y = 0."""
    largeur, traits = _G.get(car, _G[" "])
    if not traits:
        return ()
    ox = rayon + APPROCHE / 2
    seg = _segments(traits, ox, rayon)
    marge = rayon + 2 * PAS_DESSIN
    x0 = math.floor((min(seg[:, 0].min(), seg[:, 2].min()) - marge) / PAS_DESSIN) * PAS_DESSIN
    y0 = math.floor((min(seg[:, 1].min(), seg[:, 3].min()) - marge) / PAS_DESSIN) * PAS_DESSIN
    x1 = max(seg[:, 0].max(), seg[:, 2].max()) + marge
    y1 = max(seg[:, 1].max(), seg[:, 3].max()) + marge
    nx = int(math.ceil((x1 - x0) / PAS_DESSIN)) + 1
    ny = int(math.ceil((y1 - y0) / PAS_DESSIN)) + 1
    gx, gy = np.meshgrid(x0 + PAS_DESSIN * np.arange(nx), y0 + PAS_DESSIN * np.arange(ny))
    d = distance_segments(gx.ravel(), gy.ravel(), seg).reshape(ny, nx)
    resultat = []
    for b in contours_niveau(rayon - d, x0, y0, PAS_DESSIN):
        s = simplifier(b, TOLERANCE)
        if s is not None and abs(aire(s)) > 1.0:
            s.setflags(write=False)
            resultat.append(s)
    return tuple(resultat)


def ligne(texte, hauteur, interlettrage=0.0, graisse="normale"):
    """Ligne de texte en contours : hauteur des capitales = ``hauteur`` (mm ou px), ligne de base y = 0, début x = 0,
    y vers le haut. Rend (liste de tableaux (n, 2), chasse totale). Caractère absent : remplacé par une espace."""
    r = _rayon(graisse)
    s = hauteur / hauteur_capitales(r)
    x = 0.0
    boucles = []
    for car in texte:
        c = car if car in _G else " "
        for b in _contours_glyphe(c, r):
            boucles.append(b * s + np.array([x, 0.0]))
        x += chasse_unites(c, r) * s + interlettrage
    return boucles, x


def largeur(texte, hauteur, interlettrage=0.0, graisse="normale"):
    """Chasse d'une ligne (mêmes unités que ``hauteur``)."""
    r = _rayon(graisse)
    s = hauteur / hauteur_capitales(r)
    return sum(chasse_unites(c if c in _G else " ", r) * s + interlettrage for c in texte)


def masque(texte, hauteur, interlettrage=0.0, graisse="normale"):
    """Rendu matriciel anticrénelé d'une ligne (pixels) : rend (alpha float32 (h, w) en 0..1, ligne_de_base_px, chasse).
    ``hauteur`` = hauteur des capitales en pixels ; l'image couvre ascendantes, accents et descendantes."""
    r = _rayon(graisse)
    s = hauteur / hauteur_capitales(r)
    haut = (CAP + 20 + 12 + 2 * r + 4) * s     # accents des capitales
    bas = (-DESC + 4) * s
    w_total = largeur(texte, hauteur, interlettrage, r)
    h_px = int(math.ceil(haut + bas)) + 2
    w_px = int(math.ceil(w_total)) + 2
    alpha = np.zeros((h_px, w_px), dtype=np.float32)
    base = haut + 1
    rp = r * s
    x = 1.0
    for car in texte:
        c = car if car in _G else " "
        traits = _G[c][1]
        if traits:
            seg = _segments(traits)
            seg = seg * s
            seg[:, 0] += x + rp + APPROCHE / 2 * s
            seg[:, 2] += x + rp + APPROCHE / 2 * s
            seg[:, 1] = base - (seg[:, 1] + rp)
            seg[:, 3] = base - (seg[:, 3] + rp)
            gx0 = max(0, int(math.floor(min(seg[:, 0].min(), seg[:, 2].min()) - rp - 2)))
            gx1 = min(w_px, int(math.ceil(max(seg[:, 0].max(), seg[:, 2].max()) + rp + 2)))
            gy0 = max(0, int(math.floor(min(seg[:, 1].min(), seg[:, 3].min()) - rp - 2)))
            gy1 = min(h_px, int(math.ceil(max(seg[:, 1].max(), seg[:, 3].max()) + rp + 2)))
            if gx1 > gx0 and gy1 > gy0:
                gx, gy = np.meshgrid(np.arange(gx0, gx1) + 0.5, np.arange(gy0, gy1) + 0.5)
                d = distance_segments(gx.ravel(), gy.ravel(), seg).reshape(gy1 - gy0, gx1 - gx0)
                a = np.clip(rp - d + 0.5, 0.0, 1.0).astype(np.float32)
                np.maximum(alpha[gy0:gy1, gx0:gx1], a, out=alpha[gy0:gy1, gx0:gx1])
        x += chasse_unites(c, r) * s + interlettrage
    return alpha, base, w_total
