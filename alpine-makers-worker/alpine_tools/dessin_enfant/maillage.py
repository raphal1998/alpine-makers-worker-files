# -*- coding: utf-8 -*-
"""Maillage indexé, prismes tirés de contours 2D et STL binaire (portage de Maillage.kt et de Prismes,
Triangulation.kt d'Alpine Gift).

Les sommets de mêmes coordonnées float32 sont fusionnés : faces triangulées et parois verticales partagent exactement
les mêmes sommets, donc le solide est fermé (chaque arête entre deux triangles, parcourue une fois dans chaque sens).
Les contours ont la matière à gauche (extérieurs directs, trous indirects).
"""
import re
import struct

import numpy as np

from .contours import boucles_de, regrouper
from .triangulation import trianguler_polygone

F32 = np.float32


class Maillage:
    def __init__(self):
        self._index = {}
        self._xyz = []
        self.tri = []

    @property
    def nb_triangles(self):
        return len(self.tri)

    @property
    def nb_sommets(self):
        return len(self._xyz)

    def sommet(self, x, y, z):
        """x, y, z : valeurs float32 (déjà arrondies) en float Python ; −0 et +0 confondus comme l'application."""
        cle = (x + 0.0, y + 0.0, z + 0.0)
        i = self._index.get(cle)
        if i is None:
            i = len(self._xyz)
            self._index[cle] = i
            self._xyz.append(cle)
        return i

    def ajouter(self, a, b, c):
        if a == b or b == c or a == c:
            return
        self.tri.append((a, b, c))

    def sommets(self):
        return np.asarray(self._xyz, dtype=F32).reshape(-1, 3)

    def triangles(self):
        return np.asarray(self.tri, dtype=np.int64).reshape(-1, 3)

    def boite(self):
        """[xmin, ymin, zmin, xmax, ymax, zmax] en float32."""
        v = self.sommets()
        return [float(c) for c in v.min(axis=0)] + [float(c) for c in v.max(axis=0)]

    def controle(self):
        return controle(self.triangles())

    def volume(self):
        return volume(self.sommets(), self.triangles())


def controle(triangles):
    """Fermeture : arêtes libres (1 triangle), multiples (> 2), d'orientation incohérente ; ``ferme`` si aucune."""
    if len(triangles) == 0:
        return {"triangles": 0, "aretes": 0, "libres": 0, "multiples": 0, "incoherentes": 0, "ferme": False}
    a = triangles.reshape(-1)
    b = np.roll(triangles, -1, axis=1).reshape(-1)
    n = int(max(a.max(), b.max())) + 1
    non_orient = np.minimum(a, b) * n + np.maximum(a, b)
    cles, total = np.unique(non_orient, return_counts=True)
    sens = a < b
    cles_s, n_sens = np.unique(non_orient[sens], return_counts=True)
    avant = np.zeros_like(total)
    avant[np.searchsorted(cles, cles_s)] = n_sens
    arriere = total - avant
    libres = int(np.count_nonzero(total == 1))
    multiples = int(np.count_nonzero(total > 2))
    incoherentes = int(np.count_nonzero((total == 2) & ((avant != 1) | (arriere != 1))))
    return {"triangles": int(len(triangles)), "aretes": int(len(cles)), "libres": libres, "multiples": multiples,
            "incoherentes": incoherentes, "ferme": libres == 0 and multiples == 0 and incoherentes == 0}


def volume(sommets, triangles):
    if len(triangles) == 0:
        return 0.0
    p = sommets.astype(np.float64)[triangles]
    return float(np.einsum("ij,ij->i", p[:, 0], np.cross(p[:, 1], p[:, 2])).sum() / 6.0)


def miroir(m, sym):
    """Miroir gauche-droite (x → sym − x, en float32) ; sens des triangles inversé (a, b, c) → (a, c, b) pour garder
    les faces vers l'extérieur (Fabrication.miroir)."""
    v = m.sommets()
    xm = (F32(sym) - v[:, 0]).astype(F32).tolist()
    ys, zs = v[:, 1].tolist(), v[:, 2].tolist()
    r = Maillage()
    for a, b, c in m.tri:
        ia = r.sommet(xm[a], ys[a], zs[a])
        ic = r.sommet(xm[c], ys[c], zs[c])
        ib = r.sommet(xm[b], ys[b], zs[b])
        r.ajouter(ia, ic, ib)
    return r


# ------------------------------------------------------------------ prismes (Prismes)
def _f32(b):
    return np.asarray(b, dtype=np.float64).astype(F32).tolist()


_TRIANGULATIONS = {}


def _trianguler(ext, trous):
    """Triangulation d'un polygone, gardée pour ses autres faces (dessus et dessous d'un même prisme, pièces du 3MF)."""
    cle = (len(ext), tuple(len(t) for t in trous), b"".join([ext.tobytes()] + [t.tobytes() for t in trous]))
    t = _TRIANGULATIONS.get(cle)
    if t is None:
        t = trianguler_polygone(ext, trous)
        if len(_TRIANGULATIONS) >= 64:
            _TRIANGULATIONS.pop(next(iter(_TRIANGULATIONS)))
        _TRIANGULATIONS[cle] = t
    return t


def face(m, polygones, z, dessus):
    """Face horizontale à ``z`` ; ``dessus`` : normale +z, sinon −z."""
    zf = float(F32(z))
    for ext, trous in polygones:
        ids = []
        for b in [ext] + list(trous):
            for x, y in _f32(b):
                ids.append(m.sommet(x, y, zf))
        t = _trianguler(ext, trous)
        for k in range(0, len(t) - 2, 3):
            if dessus:
                m.ajouter(ids[t[k]], ids[t[k + 1]], ids[t[k + 2]])
            else:
                m.ajouter(ids[t[k]], ids[t[k + 2]], ids[t[k + 1]])


def paroi(m, b, z_bas, z_haut, matiere_a_gauche):
    """Paroi verticale le long d'une boucle ; ``matiere_a_gauche`` : la normale sort à droite du sens de parcours."""
    zb, zh = float(F32(z_bas)), float(F32(z_haut))
    pts = _f32(b)
    n = len(pts)
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % n]
        a_b = m.sommet(ax, ay, zb)
        b_b = m.sommet(bx, by, zb)
        b_h = m.sommet(bx, by, zh)
        a_h = m.sommet(ax, ay, zh)
        if matiere_a_gauche:
            m.ajouter(a_b, b_b, b_h)
            m.ajouter(a_b, b_h, a_h)
        else:
            m.ajouter(a_b, b_h, b_b)
            m.ajouter(a_b, a_h, b_h)


def prisme(m, polygones, z0, z1):
    face(m, polygones, z0, False)
    face(m, polygones, z1, True)
    for b in boucles_de(polygones):
        paroi(m, b, z0, z1, True)


def socle_avec_motif(m, socle, motif, z_socle, z_motif, poche=(), z_poche=0.0):
    """Socle (0..z_socle) avec un motif strictement intérieur, en relief jusqu'à z_motif > z_socle (ou creusé) ;
    ``poche`` (logement d'aimant) est une cavité ouverte par-dessous jusqu'à z_poche. Un seul solide fermé."""
    poche = list(poche)
    if not poche:
        face(m, socle, 0.0, False)
    else:
        face(m, regrouper(boucles_de(socle) + boucles_de(poche)), 0.0, False)
        for b in boucles_de(poche):
            paroi(m, b, 0.0, z_poche, False)
        face(m, poche, z_poche, False)
    for b in boucles_de(socle):
        paroi(m, b, 0.0, z_socle, True)
    if not motif:
        face(m, socle, z_socle, True)
        return
    face(m, regrouper(boucles_de(socle) + boucles_de(motif)), z_socle, True)
    if z_motif > z_socle:
        for b in boucles_de(motif):
            paroi(m, b, z_socle, z_motif, True)
    else:
        for b in boucles_de(motif):
            paroi(m, b, z_motif, z_socle, False)
    face(m, motif, z_motif, True)


# ------------------------------------------------------------------ STL binaire (StlBinaire)
_STL = np.dtype([("n", "<f4", (3,)), ("v", "<f4", (3, 3)), ("attr", "<u2")])


def entete_stl(titre):
    """En-tête de 80 octets, qui ne commence pas par « solid »."""
    return re.sub(r"[^\x20-\x7E]", "_", "Alpine Makers - " + titre).encode("ascii")[:80].ljust(80, b"\0")


def stl_octets(sommets, triangles, titre):
    """STL binaire, normales calculées en float32 comme l'application."""
    v = np.asarray(sommets, dtype=F32)
    t = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    u, w = b - a, c - a
    n = np.stack([u[:, 1] * w[:, 2] - u[:, 2] * w[:, 1], u[:, 2] * w[:, 0] - u[:, 0] * w[:, 2],
                  u[:, 0] * w[:, 1] - u[:, 1] * w[:, 0]], axis=1).astype(F32)
    lg = np.sqrt(n[:, 0] * n[:, 0] + n[:, 1] * n[:, 1] + n[:, 2] * n[:, 2]).astype(F32)
    n = np.where(lg[:, None] > 0, n / np.where(lg > 0, lg, F32(1))[:, None], F32(0)).astype(F32)
    rec = np.zeros(len(t), _STL)
    rec["n"] = n
    rec["v"] = np.stack((a, b, c), axis=1)
    return entete_stl(titre) + struct.pack("<I", len(t)) + rec.tobytes()


def lire_stl(octets):
    """Relecture d'un STL binaire : sommets fusionnés (float32 exacts) et triangles."""
    if len(octets) < 84:
        raise ValueError("STL trop court.")
    n = struct.unpack_from("<I", octets, 80)[0]
    if len(octets) != 84 + 50 * n:
        raise ValueError("Taille du STL incohérente.")
    rec = np.frombuffer(octets, dtype=_STL, count=n, offset=84)
    p = rec["v"].reshape(-1, 3)
    uniques, inv = np.unique(p, axis=0, return_inverse=True)
    return uniques, inv.reshape(-1, 3)
