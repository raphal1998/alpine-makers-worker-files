"""Lithophanie, portée d'Alpine Gift (core Lithophanie.kt, Formes.kt, Champ.kt, Maillage.kt ; ui Rendu.retroeclaire).

La photo devient un relief dont l'épaisseur laisse passer plus ou moins la lumière (sombre = épais). Le maillage est un
champ de hauteur sur une grille carrée, découpé par la forme (cellule gardée si la moyenne du champ de distance à ses
quatre coins est positive), fermé par un fond plat et des parois verticales : chaque arête est partagée par deux
triangles. Relief lisible vu de dessus (+z), fond à z = 0. Tout est vectorisé avec numpy.
"""
import math
import struct

import numpy as np

F32 = np.float32
PAS_MIN = 0.15
TRIANGLES_MAX = 1_500_000
ENTETE_STL = 80
OCTETS_TRIANGLE = 50
MU_PLA = 1.35  # atténuation du PLA blanc, par mm (ordre de grandeur)

# forme → (libellé, taux de remplissage de la boîte pour l'estimation)
FORMES = {"rectangle": ("Rectangle", 1.0), "arrondi": ("Arrondi", 0.98), "cercle": ("Cercle", math.pi / 4),
          "coeur": ("Cœur", 0.62)}

_STL = np.dtype([("n", "<f4", (3,)), ("v", "<f4", (9,)), ("a", "<u2")])


def _arrondi(x):
    return int(math.floor(x + 0.5))


def estimation(forme, largeur, hauteur, pas):
    """Triangles estimés avant calcul pour ce pas (les parois sont négligeables)."""
    return int(4.0 * FORMES[forme][1] * largeur * hauteur / (pas * pas))


# --- Formes : champ de distance signée (mm, positif dedans), float32, indexé [j, i] ---------------------------------

def _champ_rect_arrondi(xs, ys, cx, cy, largeur, hauteur, rayon):
    r = min(max(rayon, 0.0), min(largeur, hauteur) / 2)
    bx, by = largeur / 2 - r, hauteur / 2 - r
    qx = np.abs(xs[None, :] - cx) - bx
    qy = np.abs(ys[:, None] - cy) - by
    dehors = np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0)) + np.minimum(np.maximum(qx, qy), 0.0) - r
    return (-dehors).astype(F32)


def _champ_ellipse(xs, ys, cx, cy, rx, ry):
    m = min(rx, ry)
    return ((1.0 - np.hypot((xs[None, :] - cx) / rx, (ys[:, None] - cy) / ry)) * m).astype(F32)


def contour_coeur(cx, cy, largeur, hauteur, n=360):
    """Contour du cœur (x = 16 sin³t, y = 13 cos t − 5 cos 2t − 2 cos 3t − cos 4t), sens direct, inscrit dans la boîte."""
    t = 2 * math.pi * (n - np.arange(n)) / n
    px = 16 * np.sin(t) ** 3
    py = 13 * np.cos(t) - 5 * np.cos(2 * t) - 2 * np.cos(3 * t) - np.cos(4 * t)
    px = cx + ((px - px.min()) / (px.max() - px.min()) - 0.5) * largeur
    py = cy + ((py - py.min()) / (py.max() - py.min()) - 0.5) * hauteur
    aire = 0.5 * float(np.sum(px * np.roll(py, -1) - np.roll(px, -1) * py))
    return (px[::-1].copy(), py[::-1].copy()) if aire < 0 else (px, py)


def _masque_polygone(xs, ys, px, py, pas):
    """Champ.rasteriser : nœuds dedans (règle non nulle), balayage ligne par ligne comme l'application."""
    masque = np.zeros((len(ys), len(xs)), dtype=bool)
    ax, ay = np.roll(px, 1), np.roll(py, 1)  # sommet précédent (k = i − 1)
    nx = len(xs)
    for j, y in enumerate(ys):
        coupe = (ay <= y) != (py <= y)
        if not coupe.any():
            continue
        a_x, a_y, b_x, b_y = ax[coupe], ay[coupe], px[coupe], py[coupe]
        xc = a_x + (y - a_y) / (b_y - a_y) * (b_x - a_x)
        sens = np.where(b_y > a_y, 1, -1)
        ordre = np.argsort(xc, kind="stable")
        xc, sens = xc[ordre], sens[ordre]
        enroulement = 0
        for q in range(len(xc) - 1):
            enroulement += int(sens[q])
            if enroulement == 0:
                continue
            ia = max(0, math.ceil(xc[q] / pas))
            ib = min(nx - 1, math.floor(xc[q + 1] / pas))
            if ib >= ia:
                masque[j, ia:ib + 1] = True
    return masque


def _distance_segments(xs, ys, px, py, bloc=2048):
    """Distance exacte de chaque nœud au contour fermé (px, py), par blocs de nœuds."""
    ax, ay = px, py
    bx, by = np.roll(px, -1), np.roll(py, -1)
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    l2 = np.where(l2 > 0, l2, 1.0)
    gx, gy = np.meshgrid(xs, ys)
    gx, gy = gx.ravel(), gy.ravel()
    d = np.empty(gx.size, dtype=np.float64)
    for k in range(0, gx.size, bloc):
        x = gx[k:k + bloc, None]
        y = gy[k:k + bloc, None]
        t = np.clip(((x - ax) * dx + (y - ay) * dy) / l2, 0.0, 1.0)
        ex = x - (ax + t * dx)
        ey = y - (ay + t * dy)
        d[k:k + bloc] = np.sqrt(np.min(ex * ex + ey * ey, axis=1))
    return d.reshape(len(ys), len(xs))


def _champ_coeur(xs, ys, cx, cy, largeur, hauteur, pas):
    """Forme.Coeur : signe par le remplissage du contour, valeur = distance exacte au contour (jamais nulle)."""
    px, py = contour_coeur(cx, cy, largeur, hauteur)
    masque = _masque_polygone(xs, ys, px, py, pas)
    d = np.maximum(_distance_segments(xs, ys, px, py), 1e-6)
    return np.where(masque, d, -d).astype(F32)


# --- Plan ------------------------------------------------------------------------------------------------------------

def nettoyer(cellule):
    """Supprime les contacts par un seul coin (deux cellules en diagonale sans voisine commune) en ajoutant une
    cellule : sinon une arête verticale serait partagée par quatre triangles. Même balayage que l'application."""
    c = cellule
    while True:
        a, b, d, e = c[:-1, :-1], c[:-1, 1:], c[1:, :-1], c[1:, 1:]
        candidats = (a & e & ~b & ~d) | (b & d & ~a & ~e)
        if not candidats.any():
            return c
        for j, i in zip(*np.nonzero(candidats)):
            ca, cb, cd, ce = c[j, i], c[j, i + 1], c[j + 1, i], c[j + 1, i + 1]
            if ca and ce and not cb and not cd:
                c[j, i + 1] = True
            elif cb and cd and not ca and not ce:
                c[j, i] = True


class Plan:
    """Grille calculée : hauteurs aux nœuds z[j, i] (float32), cellules gardées cellule[j, i], nœuds du cadre."""

    def __init__(self, nx, ny, pas, largeur, hauteur, z, cellule, cadre, avertissement):
        self.nx, self.ny, self.pas, self.largeur, self.hauteur = nx, ny, pas, largeur, hauteur
        self.z, self.cellule, self.cadre, self.avertissement = z, cellule, cadre, avertissement
        k = np.zeros((ny + 1, nx + 1), dtype=bool)  # cellules bordées de vide
        k[1:-1, 1:-1] = cellule
        self._k = k

    @property
    def nb_cellules(self):
        return int(self.cellule.sum())

    def _voisins(self):
        k = self._k
        g = k[1:-1, 1:-1]
        return g, ~k[:-2, 1:-1], ~k[1:-1, 2:], ~k[2:, 1:-1], ~k[1:-1, :-2]  # bas (j−1), droite, haut (j+1), gauche

    def triangles(self):
        """Nombre exact de triangles du STL."""
        g, bas, droite, haut, gauche = self._voisins()
        bords = int((g & bas).sum() + (g & droite).sum() + (g & haut).sum() + (g & gauche).sum())
        return 4 * self.nb_cellules + 2 * bords

    def blocs_triangles(self, lignes=64):
        """Triangles du maillage fermé par blocs de rangées : tableaux (m, 3, 3) float32 (sommets en mm, sens direct)."""
        p = F32(self.pas)
        z = self.z
        g, bas, droite, haut, gauche = self._voisins()
        for j0 in range(0, self.ny - 1, lignes):
            jj, ii = np.nonzero(g[j0:j0 + lignes])
            if jj.size == 0:
                continue
            jj = jj + j0
            x0, x1 = ii.astype(F32) * p, (ii + 1).astype(F32) * p
            y0, y1 = jj.astype(F32) * p, (jj + 1).astype(F32) * p
            z00, z10, z11, z01 = z[jj, ii], z[jj, ii + 1], z[jj + 1, ii + 1], z[jj + 1, ii]
            zero = np.zeros_like(x0)
            a = np.stack([x0, y0, z00], -1)
            b = np.stack([x1, y0, z10], -1)
            c = np.stack([x1, y1, z11], -1)
            d = np.stack([x0, y1, z01], -1)
            # Dessus (sens direct vu de +z) ; la diagonale suit le relief (la plus courte en hauteur).
            court = (np.abs(z00 - z11) <= np.abs(z10 - z01))[:, None]
            t1 = np.stack([a, b, np.where(court, c, d)], 1)
            t2 = np.stack([np.where(court, a, b), c, d], 1)
            # Fond (z = 0, normale −z).
            a0, b0 = np.stack([x0, y0, zero], -1), np.stack([x1, y0, zero], -1)
            c0, d0 = np.stack([x1, y1, zero], -1), np.stack([x0, y1, zero], -1)
            morceaux = [t1, t2, np.stack([a0, c0, b0], 1), np.stack([a0, d0, c0], 1)]
            # Parois sur les bords de la forme (matière à gauche du sens direct de la cellule).
            for libre, (h1, h2, s1, s2) in ((bas, (a, b, a0, b0)), (droite, (b, c, b0, c0)),
                                            (haut, (c, d, c0, d0)), (gauche, (d, a, d0, a0))):
                m = libre[jj, ii]
                if m.any():
                    ha, hb, sa, sb = h1[m], h2[m], s1[m], s2[m]
                    morceaux.append(np.stack([sa, sb, hb], 1))
                    morceaux.append(np.stack([sa, hb, ha], 1))
            yield np.concatenate(morceaux, 0), (jj[-1] + 1) / (self.ny - 1)

    def transmission(self, ep_min, ep_max):
        """Aperçu « rétroéclairé » : lumière transmise par nœud (Beer–Lambert), 0 hors forme ; ligne du haut d'abord."""
        k = self._k
        dedans = k[1:, 1:] | k[1:, :-1] | k[:-1, 1:] | k[:-1, :-1]
        i_max, i_min = math.exp(-MU_PLA * ep_min), math.exp(-MU_PLA * ep_max)
        t = self.z.astype(np.float64)
        v = (((np.exp(-MU_PLA * t) - i_min) / (i_max - i_min)) * 0.92 + 0.04).astype(F32)
        v = np.where(dedans, np.clip(v, F32(0), F32(1)), F32(0)).astype(F32)
        return v[::-1].copy()


def planifier(image, largeur_mm=100.0, forme="rectangle", epaisseur_min=0.8, epaisseur_max=3.2, cadre_mm=3.0,
              pas_mm=0.2, negatif=False, rayon_arrondi_mm=8.0, triangles_max=TRIANGLES_MAX, progress=None):
    """Prépare la grille. image : gris float32 déjà recadré et réglé ; sa proportion fixe la hauteur. Si le pas demandé
    dépasse triangles_max, le pas est élargi et un avertissement est rendu."""
    if not 10.0 <= largeur_mm <= 400.0:
        raise ValueError("Largeur de 10 à 400 mm.")
    if not (epaisseur_min > 0 and epaisseur_max > epaisseur_min):
        raise ValueError("L'épaisseur maximale doit dépasser la minimale.")
    if forme not in FORMES:
        raise ValueError("Forme inconnue.")
    h_img, l_img = image.shape
    hauteur_voulue = largeur_mm * h_img / l_img
    pas = max(pas_mm, PAS_MIN)
    avertissement = None
    if estimation(forme, largeur_mm, hauteur_voulue, pas) > triangles_max:
        pas = math.sqrt(4.0 * FORMES[forme][1] * largeur_mm * hauteur_voulue / triangles_max) * 1.02
        avertissement = (f"Détail réduit à {_fr(pas, 2)} mm pour garder un fichier raisonnable "
                         f"({_entier(triangles_max)} triangles au plus).")
    nx = max(3, _arrondi(largeur_mm / pas) + 1)
    pas = largeur_mm / (nx - 1)
    ny = max(3, _arrondi(hauteur_voulue / pas) + 1)
    largeur, hauteur = largeur_mm, (ny - 1) * pas
    xs = np.arange(nx, dtype=np.float64) * pas
    ys = np.arange(ny, dtype=np.float64) * pas
    cx, cy = largeur / 2, hauteur / 2
    if forme == "rectangle":
        champ = _champ_rect_arrondi(xs, ys, cx, cy, largeur, hauteur, 0.0)
    elif forme == "arrondi":
        champ = _champ_rect_arrondi(xs, ys, cx, cy, largeur, hauteur, min(rayon_arrondi_mm, min(largeur, hauteur) / 2))
    elif forme == "cercle":
        champ = _champ_ellipse(xs, ys, cx, cy, largeur / 2, hauteur / 2)
    else:
        champ = _champ_coeur(xs, ys, cx, cy, largeur, hauteur, pas)
    if progress:
        progress(0.4, "Forme découpée")
    # Le bord du champ tombe exactement sur le bord de la grille : on rétrécit d'un demi-pas pour garder les cellules
    # de bord (le centre d'une cellule de bord est à pas/2 du bord).
    champ = (champ + F32(pas * 0.5 + 1e-6)).astype(F32)
    moyenne = (champ[:-1, :-1] + champ[:-1, 1:] + champ[1:, :-1] + champ[1:, 1:]) / F32(4)
    cellule = nettoyer(moyenne > F32(0))
    # Hauteurs : cadre à l'épaisseur maximale, relief ailleurs. Image : rangée 0 en haut ; le maillage a y vers le haut.
    from .image import echantillon_grille
    g = echantillon_grille(image, np.arange(nx) / (nx - 1), 1.0 - np.arange(ny) / (ny - 1))
    g = np.clip(g.astype(np.float64), 0.0, 1.0)
    sombre = g if negatif else 1.0 - g
    z = (epaisseur_min + (epaisseur_max - epaisseur_min) * sombre).astype(F32)
    if cadre_mm > 0:
        cadre = champ.astype(np.float64) < max(cadre_mm, 0.0) + pas * 0.5
        z[cadre] = F32(epaisseur_max)
    else:
        cadre = np.zeros_like(z, dtype=bool)
    return Plan(nx, ny, pas, largeur, hauteur, z, cellule, cadre, avertissement)


def _fr(v, decimales):
    return f"{v:.{decimales}f}".replace(".", ",")


def _entier(n):
    return f"{int(n):,}".replace(",", " ")


def mm(v):
    """mm() de l'application : « 100 mm », « 74,8 mm »."""
    return (_fr(v, 1) + " mm").replace(",0 mm", " mm")


def mm2(v):
    return _fr(v, 2) + " mm"


def entete_stl(titre):
    """En-tête de 80 octets, ASCII imprimable, ne commence jamais par « solid »."""
    propre = "".join(ch if 0x20 <= ord(ch) <= 0x7E else "_" for ch in "Alpine Makers - " + titre)
    return propre.encode("ascii")[:ENTETE_STL].ljust(ENTETE_STL, b"\0")


def ecrire_stl(plan, chemin, titre, progress=None):
    """STL binaire écrit en flux (normales unitaires), puis nombre de triangles inscrit ; rend ce nombre."""
    n = 0
    with open(chemin, "wb") as f:
        f.write(entete_stl(titre))
        f.write(b"\0\0\0\0")
        for tri, avance in plan.blocs_triangles():
            u = tri[:, 1] - tri[:, 0]
            w = tri[:, 2] - tri[:, 0]
            nrm = np.cross(u, w).astype(F32)
            l = np.sqrt(nrm[:, 0] * nrm[:, 0] + nrm[:, 1] * nrm[:, 1] + nrm[:, 2] * nrm[:, 2])
            nrm = np.where(l[:, None] > 0, nrm / np.where(l > 0, l, F32(1))[:, None], F32(0)).astype(F32)
            rec = np.zeros(len(tri), dtype=_STL)
            rec["n"] = nrm
            rec["v"] = tri.reshape(-1, 9)
            f.write(rec.tobytes())
            n += len(tri)
            if progress:
                progress(avance, "Écriture du STL")
        if n > 0xFFFFFFFF:
            raise ValueError("Trop de triangles pour un STL binaire.")
        f.seek(ENTETE_STL)
        f.write(struct.pack("<I", n))
    return n


def taille_stl(triangles):
    return ENTETE_STL + 4 + OCTETS_TRIANGLE * triangles


def retroeclaire(t):
    """Rendu.retroeclaire : lumière chaude transmise (brun profond épais → crème fin), transparent hors forme. RGBA."""
    v = t.astype(np.float64)
    w = np.power(np.maximum(v, 0.0), 0.8).astype(F32).astype(np.float64)
    r = np.clip(np.floor(34 + 221 * w + 0.5), 0, 255)
    g = np.clip(np.floor(18 + 226 * np.power(w, 1.15).astype(F32) + 0.5), 0, 255)
    b = np.clip(np.floor(10 + 214 * np.power(w, 1.7).astype(F32) + 0.5), 0, 255)
    a = np.where(v > 0, 255, 0)
    px = np.stack([r, g, b, a], -1).astype(np.uint8)
    px[v <= 0] = 0
    return px


def conseils(epaisseur_min, epaisseur_max):
    return [
        "Imprime debout (le grand côté sur le plateau), avec une bordure (brim) de 5 mm.",
        "Remplissage 100 % (ou 99 % rectiligne) : la lumière doit traverser une matière pleine.",
        "Hauteur de couche 0,12 mm, buse 0,4 mm, vitesse réduite pour les détails.",
        "PLA blanc (ou blanc naturel) : la couleur et l'opacité changent beaucoup le rendu.",
        f"Épaisseurs {_fr(epaisseur_min, 1)}–{_fr(epaisseur_max, 1)} mm : à regarder devant une lampe ou une LED blanche.",
    ]


def nb_couches(epaisseur_max):
    return math.ceil(epaisseur_max / 0.12)
