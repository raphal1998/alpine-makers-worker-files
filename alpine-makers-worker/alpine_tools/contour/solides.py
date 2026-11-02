"""Solides pour l'impression 3D (port de Maillage.kt, Extrusion.kt, Solides.kt) : maillages fermés, STL binaire.

- pièce extrudée : le contour (trous conservés) monté sur l'épaisseur choisie ;
- insert de rangement : bloc à coins arrondis creusé d'une poche par objet, encoche optionnelle.
"""
import math

import numpy as np

from . import champ as ch
from . import trace as tr
from .triangulation import trianguler_polygone
from .geometrie import Boucle, Polygone, boite, mm, decimal_fr, regrouper, sans_alignes

F32 = np.float32
PAROI_MIN = 1.2
FOND_MIN = 0.8
MAX_TRIANGLES = 5_000_000


class Maillage:
    """Triangles accumulés (float32), sommets fusionnés à l'identique au moment du calcul (comme l'application)."""

    def __init__(self):
        self._blocs = []
        self._fige = None

    def ajouter(self, tri):
        """tri : tableau (n, 3, 3) de coordonnées en mm (ordre direct = normale sortante)."""
        tri = np.asarray(tri, dtype=F32).reshape(-1, 3, 3)
        if tri.size:
            self._blocs.append(tri)
            self._fige = None

    def fusionner(self, autre):
        for b in autre._blocs:
            self.ajouter(b)

    def _figer(self):
        if self._fige is None:
            if self._blocs:
                t = np.concatenate(self._blocs) + F32(0.0)  # −0 → +0, comme « x + 0f »
            else:
                t = np.zeros((0, 3, 3), dtype=F32)
            if t.shape[0] > MAX_TRIANGLES:
                raise ValueError("Maillage trop lourd (plus de 5 millions de triangles) : simplifie le contour.")
            sommets, inv = np.unique(t.reshape(-1, 3), axis=0, return_inverse=True)
            idx = inv.reshape(-1, 3)
            ok = (idx[:, 0] != idx[:, 1]) & (idx[:, 1] != idx[:, 2]) & (idx[:, 0] != idx[:, 2])
            self._fige = (sommets, idx[ok])
        return self._fige

    @property
    def nb_triangles(self):
        return int(self._figer()[1].shape[0])

    @property
    def nb_sommets(self):
        return int(self._figer()[0].shape[0])

    def triangles(self):
        s, idx = self._figer()
        return s[idx]

    def controle(self):
        """Arêtes libres, multiples et d'orientation incohérente ; fermé si tout est à zéro."""
        s, idx = self._figer()
        n = idx.shape[0]
        a = np.concatenate([idx[:, 0], idx[:, 1], idx[:, 2]]).astype(np.int64)
        b = np.concatenate([idx[:, 1], idx[:, 2], idx[:, 0]]).astype(np.int64)
        V = max(1, s.shape[0])
        cles, nb = np.unique(a * V + b, return_counts=True)
        bas, haut = np.minimum(a, b), np.maximum(a, b)
        non_orientees = np.unique(bas * V + haut)
        def compte(c):
            pos = np.searchsorted(cles, c)
            pos = np.minimum(pos, cles.size - 1)
            return np.where(cles[pos] == c, nb[pos], 0) if cles.size else np.zeros_like(c)
        lo, hi = non_orientees // V, non_orientees % V
        n_lh, n_hl = compte(lo * V + hi), compte(hi * V + lo)
        total = n_lh + n_hl
        libres = int(np.count_nonzero(total == 1))
        multiples = int(np.count_nonzero(total > 2))
        incoherentes = int(np.count_nonzero((total == 2) & ((n_lh != 1) | (n_hl != 1))))
        return {"triangles": n, "sommets": int(s.shape[0]), "aretes": int(non_orientees.size), "aretes_libres": libres,
                "aretes_multiples": multiples, "orientation_incoherente": incoherentes,
                "ferme": libres == 0 and multiples == 0 and incoherentes == 0 and n > 0}

    def boite(self):
        s, _ = self._figer()
        return np.concatenate([s.min(axis=0), s.max(axis=0)]).astype(np.float64)

    def volume(self):
        t = self.triangles().astype(np.float64)
        a, b, c = t[:, 0], t[:, 1], t[:, 2]
        return float(np.sum(np.einsum("ij,ij->i", a, np.cross(b, c))) / 6.0)


def etendue(m):
    b = m.boite()
    return [b[3] - b[0], b[4] - b[1], b[5] - b[2]]


# ------------------------------------------------------------------ STL binaire

def stl_octets(m, titre):
    """En-tête de 80 octets (jamais « solid »), nombre de triangles, normale + 3 sommets float32, attribut 0."""
    t = m.triangles().astype(F32)
    n = t.shape[0]
    u = t[:, 1] - t[:, 0]
    v = t[:, 2] - t[:, 0]
    nrm = np.cross(u, v).astype(F32)
    l = np.sqrt((nrm * nrm).sum(axis=1))
    with np.errstate(invalid="ignore", divide="ignore"):
        nrm = np.where(l[:, None] > 0, nrm / l[:, None], F32(0)).astype(F32)
    texte = "".join(c if 0x20 <= ord(c) <= 0x7E else "_" for c in "Alpine Contour - " + titre)
    entete = texte.encode("ascii")[:80].ljust(80, b"\0")
    dt = np.dtype([("n", "<f4", (3,)), ("p", "<f4", (9,)), ("a", "<u2")])
    data = np.zeros(n, dtype=dt)
    data["n"] = nrm
    data["p"] = t.reshape(n, 9)
    return entete + np.uint32(n).astype("<u4").tobytes() + data.tobytes()


def lire_stl(octets):
    """Relit un STL binaire → Maillage (contrôle et tests)."""
    n = int(np.frombuffer(octets, dtype="<u4", count=1, offset=80)[0])
    if len(octets) != 84 + 50 * n:
        raise ValueError("STL binaire de taille incohérente.")
    dt = np.dtype([("n", "<f4", (3,)), ("p", "<f4", (9,)), ("a", "<u2")])
    data = np.frombuffer(octets, dtype=dt, count=n, offset=84)
    m = Maillage()
    m.ajouter(data["p"].reshape(n, 3, 3))
    return m


# ------------------------------------------------------------------ extrusion

def _face(m, polygones, z, dessus):
    for p in polygones:
        bcl = [p.exterieur] + p.trous
        xs = np.concatenate([b.x for b in bcl])
        ys = np.concatenate([b.y for b in bcl])
        tri = trianguler_polygone(p)
        if not tri:
            continue
        idx = np.array(tri, dtype=np.int64)
        if not dessus:
            idx = idx[:, [0, 2, 1]]
        pts = np.stack([xs.astype(F32), ys.astype(F32), np.full(xs.size, F32(z))], axis=1)
        m.ajouter(pts[idx])


def _paroi(m, b, z_bas, z_haut, matiere_a_gauche):
    ax, ay = b.x.astype(F32), b.y.astype(F32)
    bx, by = np.roll(ax, -1), np.roll(ay, -1)
    zb = np.full(ax.size, F32(z_bas))
    zh = np.full(ax.size, F32(z_haut))
    aB = np.stack([ax, ay, zb], 1)
    bB = np.stack([bx, by, zb], 1)
    bH = np.stack([bx, by, zh], 1)
    aH = np.stack([ax, ay, zh], 1)
    if matiere_a_gauche:
        t1, t2 = np.stack([aB, bB, bH], 1), np.stack([aB, bH, aH], 1)
    else:
        t1, t2 = np.stack([aB, bH, bB], 1), np.stack([aB, aH, bH], 1)
    m.ajouter(t1)
    m.ajouter(t2)


def _boucles(polygones):
    return [b for p in polygones for b in [p.exterieur] + p.trous]


def prisme(m, polygones, z0, z1):
    _face(m, polygones, z0, False)
    _face(m, polygones, z1, True)
    for b in _boucles(polygones):
        _paroi(m, b, z0, z1, True)


def socle_avec_motif(m, socle, motif, z_socle, z_motif):
    _face(m, socle, 0.0, False)
    for b in _boucles(socle):
        _paroi(m, b, 0.0, z_socle, True)
    _face(m, regrouper(_boucles(socle) + _boucles(motif)), z_socle, True)
    if z_motif > z_socle:
        for b in _boucles(motif):
            _paroi(m, b, z_socle, z_motif, True)
    else:
        for b in _boucles(motif):
            _paroi(m, b, z_motif, z_socle, False)
    _face(m, motif, z_motif, True)


# ------------------------------------------------------------------ pièce et insert

def piece(boucles, epaisseur):
    if not 0.2 <= epaisseur <= 200.0:
        raise ValueError("Épaisseur hors limites (0,2 à 200 mm).")
    if not regrouper(boucles):
        raise ValueError("Aucun contour fermé à extruder.")
    b = boite(boucles)
    deplaces = regrouper([l.translater(-b[0], -b[1]) for l in boucles])
    m = Maillage()
    prisme(m, deplaces, 0.0, epaisseur)
    return m


def rectangle(cx, cy, l, h, rayon, segments=10):
    r = min(max(rayon, 0.0), min(l, h) / 2 - 0.01)
    if r < 0.05:
        return Boucle([cx - l / 2, cx + l / 2, cx + l / 2, cx - l / 2], [cy - h / 2, cy - h / 2, cy + h / 2, cy + h / 2])
    xs, ys = [], []
    centres = [(cx + l / 2 - r, cy - h / 2 + r), (cx + l / 2 - r, cy + h / 2 - r), (cx - l / 2 + r, cy + h / 2 - r), (cx - l / 2 + r, cy - h / 2 + r)]
    for k, c in enumerate(centres):
        a0 = -math.pi / 2 + k * math.pi / 2
        for s in range(segments + 1):
            a = a0 + s * (math.pi / 2) / segments
            xs.append(c[0] + r * math.cos(a))
            ys.append(c[1] + r * math.sin(a))
    return sans_alignes(Boucle(xs, ys))


def cercle(cx, cy, r, segments=72):
    a = 2 * math.pi * np.arange(segments) / segments
    return Boucle(cx + r * np.cos(a), cy + r * np.sin(a))


class ReglagesInsert:
    def __init__(self, taille="auto", marge_mm=8.0, largeur_mm=126.0, longueur_mm=84.0, unites_x=3, unites_y=2,
                 jeu_grille_mm=0.5, hauteur_mm=20.0, profondeur_mm=14.0, rayon_coin_mm=4.0, encoche=True,
                 diametre_encoche_mm=22.0, cote_encoche="auto"):
        self.taille, self.marge_mm, self.largeur_mm, self.longueur_mm = taille, float(marge_mm), float(largeur_mm), float(longueur_mm)
        self.unites_x, self.unites_y, self.jeu_grille_mm = int(unites_x), int(unites_y), float(jeu_grille_mm)
        self.hauteur_mm, self.profondeur_mm, self.rayon_coin_mm = float(hauteur_mm), float(profondeur_mm), float(rayon_coin_mm)
        self.encoche, self.diametre_encoche_mm, self.cote_encoche = bool(encoche), float(diametre_encoche_mm), cote_encoche


class Insert:
    def __init__(self, maillage, largeur, longueur, hauteur, poches, cadre):
        self.maillage, self.largeur, self.longueur, self.hauteur = maillage, largeur, longueur, hauteur
        self.poches, self.cadre = poches, cadre


def centre_encoche(poche, cote):
    """Là où la demi-droite partie du centre de gravité de la poche, vers ``cote``, sort de la poche."""
    x, y = poche.x, poche.y
    xj, yj = np.roll(x, -1), np.roll(y, -1)
    c = x * yj - xj * y
    a = float(c.sum())
    if abs(a) > 1e-9:
        g = (float(((x + xj) * c).sum()) / (3 * a), float(((y + yj) * c).sum()) / (3 * a))
    else:
        g = (float(x.mean()), float(y.mean()))
    b = poche.boite()
    sens = cote if cote != "auto" else ("droite" if b[3] - b[1] > b[2] - b[0] else "haut")
    coupes = []
    for i in range(poche.n):
        j = (i + 1) % poche.n
        if sens in ("haut", "bas"):
            x0, x1 = x[i], x[j]
            if (x0 <= g[0]) != (x1 <= g[0]):
                coupes.append(float(y[i] + (g[0] - x0) / (x1 - x0) * (y[j] - y[i])))
        else:
            y0, y1 = y[i], y[j]
            if (y0 <= g[1]) != (y1 <= g[1]):
                coupes.append(float(x[i] + (g[1] - y0) / (y1 - y0) * (x[j] - x[i])))
    if sens == "haut":
        return g[0], min((c for c in coupes if c > g[1]), default=b[3])
    if sens == "bas":
        return g[0], max((c for c in coupes if c < g[1]), default=b[1])
    if sens == "gauche":
        return max((c for c in coupes if c < g[0]), default=b[0]), g[1]
    return min((c for c in coupes if c > g[0]), default=b[2]), g[1]


def insert(objets, r, pas=0.1):
    """Insert : ``objets`` = contours finaux (jeu compris) de chaque objet ; seuls les extérieurs comptent."""
    if not 2.0 <= r.hauteur_mm <= 150.0:
        raise ValueError("Hauteur du bloc hors limites (2 à 150 mm).")
    if not (r.profondeur_mm >= 0.6 and r.profondeur_mm <= r.hauteur_mm - FOND_MIN):
        raise ValueError(f"La profondeur des poches doit laisser au moins {decimal_fr(FOND_MIN)} mm de fond sous les objets.")
    exterieurs = [e for e in ([b for b in o if b.aire > 0] for o in objets) if e]
    if not exterieurs:
        raise ValueError("Aucun objet retenu pour l'insert.")
    formes = []
    for o in exterieurs:
        formes += o
        if r.encoche:
            principale = max(o, key=lambda b: b.aire)
            c = centre_encoche(principale, r.cote_encoche)
            formes.append(cercle(c[0], c[1], r.diametre_encoche_mm / 2))
    g = ch.Grille.sur(boite(formes), pas, 3 * pas)
    v = ch.distance_signee(g, formes)
    poches = [p for p in tr.simplifier(ch.contours(v, g, 4 * pas * pas), 0.03) if p.aire > 0]
    if not poches:
        raise ValueError("Les poches de l'insert sont vides.")
    bp = boite(poches)
    cx, cy = (bp[0] + bp[2]) / 2, (bp[1] + bp[3]) / 2
    if r.taille == "auto":
        l, h = math.ceil(bp[2] - bp[0] + 2 * r.marge_mm), math.ceil(bp[3] - bp[1] + 2 * r.marge_mm)
    elif r.taille == "sur_mesure":
        l, h = r.largeur_mm, r.longueur_mm
    else:
        l, h = 42.0 * r.unites_x - r.jeu_grille_mm, 42.0 * r.unites_y - r.jeu_grille_mm
    l, h = float(l), float(h)
    if not (10.0 <= l <= 600.0 and 10.0 <= h <= 600.0):
        raise ValueError("Dimensions du bloc hors limites (10 à 600 mm).")
    cadre = rectangle(cx, cy, l, h, r.rayon_coin_mm)
    paroi_x, paroi_y = (l - (bp[2] - bp[0])) / 2, (h - (bp[3] - bp[1])) / 2
    if paroi_x < PAROI_MIN or paroi_y < PAROI_MIN:
        besoin_l, besoin_h = math.ceil(bp[2] - bp[0] + 2 * PAROI_MIN), math.ceil(bp[3] - bp[1] + 2 * PAROI_MIN)
        raise ValueError(f"Les poches ({mm(bp[2] - bp[0])} × {mm(bp[3] - bp[1])}) ne tiennent pas dans un bloc de "
                         f"{mm(l)} × {mm(h)} : il faut au moins {mm(besoin_l, 0)} × {mm(besoin_h, 0)}.")
    for p in poches:
        for px, py in zip(p.x.tolist(), p.y.tolist()):
            if not cadre.contient(px, py):
                raise ValueError("Une poche touche un coin arrondi du bloc : réduis le rayon des coins ou agrandis le bloc.")
    ox, oy = cx - l / 2, cy - h / 2
    m = Maillage()
    socle_avec_motif(m, [Polygone(cadre.translater(-ox, -oy), [])], [Polygone(p.translater(-ox, -oy), []) for p in poches],
                     r.hauteur_mm, r.hauteur_mm - r.profondeur_mm)
    return Insert(m, l, h, r.hauteur_mm, poches, cadre)


def volume_attendu(i, r):
    return i.cadre.aire * i.hauteur - sum(p.aire for p in i.poches) * r.profondeur_mm
