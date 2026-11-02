# -*- coding: utf-8 -*-
"""Maillage indexé, extrusion de contours et STL binaire (portage de Maillage.kt et Extrusion.kt d'Alpine Gift ; triangulation propre dans triangulation.py).

Les sommets de mêmes coordonnées (arrondies en float32, comme dans le STL) sont fusionnés : faces triangulées et
parois verticales partagent exactement les mêmes sommets, donc le solide est fermé (chaque arête entre deux
triangles, parcourue une fois dans chaque sens). Les contours doivent avoir la matière à gauche.
"""
import struct

import numpy as np

from .triangulation import trianguler_polygone
from .geometrie import boucles_de, regrouper


class Maillage:
    def __init__(self):
        self._index = {}
        self.xyz = []
        self.tri = []

    @property
    def nb_triangles(self):
        return len(self.tri)

    def sommet(self, x, y, z):
        cle = (float(np.float32(x)), float(np.float32(y)), float(np.float32(z)))
        i = self._index.get(cle)
        if i is None:
            i = len(self.xyz)
            self._index[cle] = i
            self.xyz.append(cle)
        return i

    def ajouter(self, a, b, c):
        if a == b or b == c or a == c:
            return
        self.tri.append((a, b, c))

    def tableaux(self):
        return np.asarray(self.xyz, dtype=np.float32).reshape(-1, 3), np.asarray(self.tri, dtype=np.int64).reshape(-1, 3)

    def controle(self):
        return controle(*self.tableaux())

    def boite(self):
        v, _ = self.tableaux()
        return v.min(axis=0).tolist() + v.max(axis=0).tolist()


def controle(sommets, triangles):
    """Fermeture : arêtes libres (1 triangle), multiples (> 2), d'orientation incohérente. ``ferme`` si aucune."""
    if len(triangles) == 0:
        return {"triangles": 0, "aretes": 0, "libres": 0, "multiples": 0, "incoherentes": 0, "ferme": False}
    a = triangles.reshape(-1)
    b = np.roll(triangles, -1, axis=1).reshape(-1)
    n = int(max(a.max(), b.max())) + 1
    orient = a * n + b
    non_orient = np.minimum(a, b) * n + np.maximum(a, b)
    cles, total = np.unique(non_orient, return_counts=True)
    _, directs = np.unique(orient, return_counts=True)
    # Pour chaque arête non orientée : nombre de passages dans le sens (min → max).
    sens = (a < b)
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
    p = sommets.astype(np.float64)[triangles]
    return float(np.einsum("ij,ij->i", p[:, 0], np.cross(p[:, 1], p[:, 2])).sum() / 6.0)


def face(m, polygones, z, dessus):
    for ext, trous in polygones:
        boucles = [ext] + list(trous)
        ids = [m.sommet(x, y, z) for b in boucles for x, y in b.tolist()]
        t = trianguler_polygone(ext, trous)
        for k in range(0, len(t), 3):
            if dessus:
                m.ajouter(ids[t[k]], ids[t[k + 1]], ids[t[k + 2]])
            else:
                m.ajouter(ids[t[k]], ids[t[k + 2]], ids[t[k + 1]])


def paroi(m, b, z_bas, z_haut, matiere_a_gauche):
    pts = b.tolist()
    n = len(pts)
    bas = [m.sommet(x, y, z_bas) for x, y in pts]
    haut = [m.sommet(x, y, z_haut) for x, y in pts]
    for i in range(n):
        j = (i + 1) % n
        if matiere_a_gauche:
            m.ajouter(bas[i], bas[j], haut[j])
            m.ajouter(bas[i], haut[j], haut[i])
        else:
            m.ajouter(bas[i], haut[j], bas[j])
            m.ajouter(bas[i], haut[i], haut[j])


def prisme(m, polygones, z0, z1):
    face(m, polygones, z0, False)
    face(m, polygones, z1, True)
    for b in boucles_de(polygones):
        paroi(m, b, z0, z1, True)


def socle_avec_motif(m, socle, motif, z_socle, z_motif):
    """Socle (0..z_socle) avec un motif strictement intérieur, en relief (z_motif > z_socle) ou creusé : un seul
    solide fermé."""
    face(m, socle, 0.0, False)
    for b in boucles_de(socle):
        paroi(m, b, 0.0, z_socle, True)
    face(m, regrouper(boucles_de(socle) + boucles_de(motif)), z_socle, True)
    if z_motif > z_socle:
        for b in boucles_de(motif):
            paroi(m, b, z_socle, z_motif, True)
    else:
        for b in boucles_de(motif):
            paroi(m, b, z_motif, z_socle, False)
    face(m, motif, z_motif, True)


def ecrire_stl(chemin, m, titre):
    """STL binaire (normales calculées)."""
    v, t = m.tableaux()
    p = v[t] if len(t) else np.zeros((0, 3, 3), dtype=np.float32)
    nrm = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]).astype(np.float64) if len(t) else np.zeros((0, 3))
    l = np.linalg.norm(nrm, axis=1, keepdims=True) if len(t) else np.zeros((0, 1))
    nrm = np.where(l > 0, nrm / np.where(l > 0, l, 1), 0).astype(np.float32)
    enreg = np.zeros(len(t), dtype=np.dtype([("n", "<f4", 3), ("p", "<f4", (3, 3)), ("a", "<u2")]))
    enreg["n"] = nrm
    enreg["p"] = p
    entete = ("Alpine Tools - " + titre).encode("ascii", "replace")[:80].ljust(80, b" ")
    with open(chemin, "wb") as f:
        f.write(entete)
        f.write(struct.pack("<I", len(t)))
        f.write(enreg.tobytes())
