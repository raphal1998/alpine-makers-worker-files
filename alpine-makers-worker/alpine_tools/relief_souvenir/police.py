"""Relief souvenir — police « Alpine Trait » dessinée pour l'outil (aucune police TTF tierce).

Chaque glyphe est une suite de traits (polylignes) dans une boîte de 4 unités de large (5 pour M et W) et 6 de haut
(capitales, ligne de base à 0). Le texte est écrit en capitales ; les accents (aigu, grave, circonflexe, tréma, cédille,
tilde, rond) sont dessinés au-dessus de la lettre de base d'après la décomposition Unicode (NFD).
Le texte devient une matière : chaque cellule d'une grille fine (≈ 0,1 mm) à moins d'une demi-épaisseur d'un trait est
pleine ; les contacts en diagonale seule sont comblés, ce qui donne un solide extrudé fermé (chaque arête entre deux
triangles).
"""
import unicodedata

import numpy as np

_O = "1,0 0,1 0,5 1,6 3,6 4,5 4,1 3,0 1,0"
_P = "0,0 0,6 3,6 4,5 4,4 3,3 0,3"
GLYPHES = {
    "A": "0,0 2,6 4,0; 0.7,2 3.3,2", "B": "0,0 0,6 3,6 4,5 4,4 3,3 0,3; 3,3 4,2 4,1 3,0 0,0",
    "C": "4,5 3,6 1,6 0,5 0,1 1,0 3,0 4,1", "D": "0,0 0,6 2.5,6 4,4.5 4,1.5 2.5,0 0,0",
    "E": "4,6 0,6 0,0 4,0; 0,3 3,3", "F": "4,6 0,6 0,0; 0,3 3,3",
    "G": "4,5 3,6 1,6 0,5 0,1 1,0 3,0 4,1 4,3 2,3", "H": "0,0 0,6; 4,0 4,6; 0,3 4,3",
    "I": "1,0 3,0; 2,0 2,6; 1,6 3,6", "J": "1,6 4,6; 3,6 3,1 2,0 1,0 0,1",
    "K": "0,0 0,6; 4,6 0,2; 1.3,3.3 4,0", "L": "0,6 0,0 4,0", "M": "0,0 0,6 2.5,2.5 5,6 5,0",
    "N": "0,0 0,6 4,0 4,6", "O": _O, "P": _P, "Q": _O + "; 2.5,1.5 4.3,-0.4", "R": _P + "; 2,3 4,0",
    "S": "4,5 3,6 1,6 0,5 0,4 1,3 3,3 4,2 4,1 3,0 1,0 0,1", "T": "0,6 4,6; 2,6 2,0",
    "U": "0,6 0,1 1,0 3,0 4,1 4,6", "V": "0,6 2,0 4,6", "W": "0,6 1.25,0 2.5,4 3.75,0 5,6",
    "X": "0,0 4,6; 0,6 4,0", "Y": "0,6 2,3 4,6; 2,3 2,0", "Z": "0,6 4,6 0,0 4,0",
    "0": _O + "; 1,1.5 3,4.5", "1": "1,5 2,6 2,0; 1,0 3,0", "2": "0,5 1,6 3,6 4,5 4,4 0,0 4,0",
    "3": "0,5 1,6 3,6 4,5 4,4 3,3 1.5,3; 3,3 4,2 4,1 3,0 1,0 0,1", "4": "3,0 3,6 0,2 4,2",
    "5": "4,6 0,6 0,3.5 3,3.5 4,2.5 4,1 3,0 1,0 0,1", "6": "4,5 3,6 1,6 0,5 0,1 1,0 3,0 4,1 4,2.5 3,3.5 0,3.5",
    "7": "0,6 4,6 1.5,0", "8": "1,3 0,4 0,5 1,6 3,6 4,5 4,4 3,3 1,3 0,2 0,1 1,0 3,0 4,1 4,2 3,3",
    "9": "0,1 1,0 3,0 4,1 4,5 3,6 1,6 0,5 0,3.5 1,2.5 4,2.5",
    ".": "0.5,0 0.5,0", ",": "0.7,0.3 0.2,-0.9", ":": "0.5,0.6 0.5,0.6; 0.5,4 0.5,4", ";": "0.5,4 0.5,4; 0.7,0.3 0.2,-0.9",
    "-": "0.3,3 3.2,3", "+": "0,3 4,3; 2,1 2,5", "/": "0,0 3,6", "'": "0.5,6 0.5,4.6", "\"": "0.5,6 0.5,4.6; 1.9,6 1.9,4.6",
    "·": "0.5,3 0.5,3", "(": "2,6.5 1,5.5 0.6,3 1,0.5 2,-0.5", ")": "0,6.5 1,5.5 1.4,3 1,0.5 0,-0.5",
    "!": "0.5,6 0.5,2; 0.5,0 0.5,0", "?": "0,5 1,6 3,6 4,5 4,4 2,3 2,2; 2,0 2,0",
    "&": "4,0 0.6,4.4 0.6,5.4 1.5,6 2.5,5.4 2.5,4.5 0,2 0,1 1,0 2.6,0 4,2.6",
    "#": "1,0 1.6,6; 2.6,0 3.2,6; 0,2 4,2; 0,4 4,4", "°": "1,6 0.4,5.4 1,4.8 1.6,5.4 1,6",
    "×": "0.5,1.5 3.5,4.5; 0.5,4.5 3.5,1.5", "_": "0,-0.6 4,-0.6", "=": "0.5,2 3.5,2; 0.5,4 3.5,4",
    "%": "0,0 4,6; 0.5,5.5 0.5,5.5; 3.5,0.5 3.5,0.5", "*": "2,2 2,6; 0.3,3.2 3.7,4.8; 0.3,4.8 3.7,3.2",
    "<": "4,5 0,3 4,1", ">": "0,5 4,3 0,1", "@": "3,2 3,4 1.5,4 1,3 1.5,2 3,2 4,3 3.5,5.5 1,6 0,4 0.5,1 2.5,0 4,0.5",
    "’": "0.5,6 0.5,4.6", "–": "0,3 4,3", "—": "0,3 5,3",
}
ACCENTS = {
    "́": "1.6,6.9 2.9,8", "̀": "1.1,8 2.4,6.9", "̂": "0.9,6.9 2,8 3.1,6.9",
    "̈": "1,7.5 1,7.5; 3,7.5 3,7.5", "̃": "0.6,7.1 1.4,7.8 2.6,7.2 3.4,7.9", "̊": "2,8 1.5,7.5 2,7 2.5,7.5 2,8",
    "̧": "2,0 2,-0.7 1.3,-1.2",
}
LARGEURS = {"M": 5.0, "W": 5.0, "—": 5.0}
ESPACE = 2.6
INTERLETTRE = 1.6
TRAIT = 0.95          # épaisseur du trait (unités)
BAS, HAUT = -1.4, 8.4  # étendue verticale (descente de la cédille → accents)


def _traits(spec):
    out = []
    for morceau in spec.split(";"):
        pts = [tuple(float(v) for v in p.split(",")) for p in morceau.split()]
        if pts:
            out.append(pts)
    return out


def _etendue(spec):
    xs = [x for tr in _traits(spec) for x, _ in tr]
    return (min(xs), max(xs)) if xs else (0.0, 0.0)


def segments_ligne(texte):
    """Segments (x0, y0, x1, y1) d'une ligne en unités, ligne de base y = 0 ; rend (tableau (n, 4), chasse)."""
    segs, x = [], 0.0
    base_accent = None  # origine de la boîte de 4 unités centrée sur la dernière lettre
    texte = texte.replace("Œ", "OE").replace("œ", "OE").replace("Æ", "AE").replace("æ", "AE").replace("ß", "SS")
    for ch in unicodedata.normalize("NFD", texte.upper()):
        if ch in ACCENTS:
            if base_accent is not None:
                for tr in _traits(ACCENTS[ch]):
                    if len(tr) == 1:
                        tr = tr * 2
                    for (ax, ay), (bx, by) in zip(tr, tr[1:]):
                        segs.append((base_accent + ax, ay, base_accent + bx, by))
            continue
        if ch.isspace():
            x += ESPACE
            base_accent = None
            continue
        spec = GLYPHES.get(ch)
        if spec is None:
            if unicodedata.category(ch).startswith("M"):
                continue
            spec = GLYPHES["?"]
        x_min, x_max = _etendue(spec)
        larg = LARGEURS.get(ch, max(x_max - x_min, 1.0))
        decale = x - x_min + (larg - (x_max - x_min)) / 2  # dessin recadré et centré dans sa chasse
        base_accent = x + (larg - 4.0) / 2
        for tr in _traits(spec):
            if len(tr) == 1:
                tr = tr * 2
            for (ax, ay), (bx, by) in zip(tr, tr[1:]):
                segs.append((decale + ax, ay, decale + bx, by))
        x += larg + INTERLETTRE
    chasse = max(0.0, x - INTERLETTRE) + TRAIT
    return np.asarray(segs, dtype=float).reshape(-1, 4), chasse


def bloc_texte(l1, l2, x0, x1, y0, y1):
    """Segments (mm) des deux lignes centrées dans le bandeau [x0, x1] × [y0, y1] et demi-épaisseurs par segment.
    Mêmes proportions que Relief.texte de l'application : 0,36 / 0,22 de la hauteur (0,46 pour une ligne)."""
    lignes = [t for t in (l1.strip(), l2.strip()) if t]
    if not lignes:
        return np.zeros((0, 4)), np.zeros(0)
    haut, large = y1 - y0, x1 - x0
    tailles = [0.36 * haut, 0.22 * haut] if len(lignes) == 2 else [0.46 * haut]
    ecart = 0.12 * haut if len(lignes) == 2 else 0.0
    total = sum(tailles) + ecart
    sommet = y0 + (haut + total) / 2
    tous, demis = [], []
    for i, t in enumerate(lignes):
        segs, chasse = segments_ligne(t)
        taille = tailles[i]
        echelle = taille / (HAUT - BAS)
        if chasse * echelle > large * 0.94:
            echelle = large * 0.94 / chasse
        sommet -= tailles[i]
        hauteur_reelle = (HAUT - BAS) * echelle
        dx = x0 + (large - chasse * echelle) / 2 + TRAIT / 2 * echelle
        base = sommet + (tailles[i] - hauteur_reelle) / 2 - BAS * echelle
        if len(segs):
            s = segs * echelle
            s[:, [0, 2]] += dx
            s[:, [1, 3]] += base
            tous.append(s)
            demi = max(0.25, TRAIT * echelle / 2)
            demis.append(np.full(len(s), demi))
        sommet -= ecart
    if not tous:
        return np.zeros((0, 4)), np.zeros(0)
    return np.vstack(tous), np.concatenate(demis)


def raster(segs, demis, x0, y0, cellule, nx, ny):
    """Grille booléenne (ny, nx) : cellule pleine si son centre est à moins d'une demi-épaisseur d'un trait."""
    plein = np.zeros((ny, nx), dtype=bool)
    for (ax, ay, bx, by), r in zip(segs, demis):
        i0 = max(0, int((min(ax, bx) - r - x0) / cellule) - 1)
        i1 = min(nx, int((max(ax, bx) + r - x0) / cellule) + 2)
        j0 = max(0, int((min(ay, by) - r - y0) / cellule) - 1)
        j1 = min(ny, int((max(ay, by) + r - y0) / cellule) + 2)
        if i0 >= i1 or j0 >= j1:
            continue
        cx = x0 + (np.arange(i0, i1) + 0.5) * cellule
        cy = y0 + (np.arange(j0, j1) + 0.5) * cellule
        px, py = np.meshgrid(cx, cy)
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = np.zeros_like(px) if l2 <= 0 else np.clip(((px - ax) * dx + (py - ay) * dy) / l2, 0.0, 1.0)
        d2 = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
        plein[j0:j1, i0:i1] |= d2 <= r * r
    return plein


def combler_diagonales(plein):
    """Supprime les contacts par un coin seul (2 × 2 en damier) en remplissant une case : maillage sans arête multiple."""
    for _ in range(64):
        a = plein[:-1, :-1]
        b = plein[:-1, 1:]
        c = plein[1:, :-1]
        d = plein[1:, 1:]
        cas1 = a & d & ~b & ~c
        cas2 = b & c & ~a & ~d
        if not (cas1.any() or cas2.any()):
            return plein
        j, i = np.nonzero(cas1)
        plein[j, i + 1] = True
        j, i = np.nonzero(cas2)
        plein[j, i] = True
    return plein


def maillage_cellules(plein, x0, y0, cellule, z0, z1):
    """Prisme fermé de la région des cellules pleines, entre z0 et z1 : (sommets (n, 3), triangles (m, 3))."""
    ny, nx = plein.shape
    if not plein.any():
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    w = nx + 1

    def coin(i, j, haut):
        return (j * w + i) * 2 + haut

    j, i = np.nonzero(plein)
    a, b, c, d = (i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1)
    tris = []
    # Dessus (normale +z) et dessous (normale −z).
    tris.append(np.stack([coin(*a, 1), coin(*b, 1), coin(*c, 1)], 1))
    tris.append(np.stack([coin(*a, 1), coin(*c, 1), coin(*d, 1)], 1))
    tris.append(np.stack([coin(*a, 0), coin(*c, 0), coin(*b, 0)], 1))
    tris.append(np.stack([coin(*a, 0), coin(*d, 0), coin(*c, 0)], 1))
    p = np.pad(plein, 1)
    # Parois : arête de la cellule parcourue dans le sens direct (matière à gauche) quand la voisine est vide.
    for (di, dj), (u, v) in (((0, -1), (a, b)), ((1, 0), (b, c)), ((0, 1), (c, d)), ((-1, 0), (d, a))):
        vide = ~p[j + 1 + dj, i + 1 + di]
        if not vide.any():
            continue
        ua = (u[0][vide], u[1][vide])
        va = (v[0][vide], v[1][vide])
        tris.append(np.stack([coin(*ua, 0), coin(*va, 0), coin(*va, 1)], 1))
        tris.append(np.stack([coin(*ua, 0), coin(*va, 1), coin(*ua, 1)], 1))
    t = np.vstack(tris)
    cles, inv = np.unique(t.ravel(), return_inverse=True)
    haut = cles % 2
    k = cles // 2
    ci, cj = k % w, k // w
    sommets = np.stack([x0 + ci * cellule, y0 + cj * cellule, np.where(haut == 1, z1, z0)], 1).astype(float)
    return sommets, inv.reshape(-1, 3).astype(np.int64)


def maillage_texte(l1, l2, x0, x1, y0, y1, z0, z1, cellule=0.1, cellules_max=6_000_000):
    """Texte en relief dans le bandeau : rend (sommets, triangles) ou des tableaux vides."""
    segs, demis = bloc_texte(l1, l2, x0, x1, y0, y1)
    if not len(segs):
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    nx = int(np.ceil((x1 - x0) / cellule)) + 2
    ny = int(np.ceil((y1 - y0 + 2) / cellule)) + 2
    if nx * ny > cellules_max:
        cellule = float(np.sqrt((x1 - x0) * (y1 - y0 + 2) / cellules_max)) * 1.05
        nx = int(np.ceil((x1 - x0) / cellule)) + 2
        ny = int(np.ceil((y1 - y0 + 2) / cellule)) + 2
    ox, oy = x0 - cellule, y0 - 1.0
    plein = combler_diagonales(raster(segs, demis, ox, oy, cellule, nx, ny))
    return maillage_cellules(plein, ox, oy, cellule, z0, z1)
