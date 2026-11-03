# -*- coding: utf-8 -*-
"""Composantes connexes d'un masque (ordre de découverte de l'application : premier pixel en balayage ligne par ligne).

Segments horizontaux reliés d'une ligne à la suivante (connexité 4 ou 8), puis composantes du graphe par accrochage
des racines et compression des chemins (repris d'Alpine Contour).
"""
import numpy as np


def _runs(masque):
    h, w = masque.shape
    p = np.zeros((h, w + 2), dtype=np.int8)
    p[:, 1:-1] = masque
    d = np.diff(p, axis=1)
    rs, s = np.nonzero(d == 1)
    _, e = np.nonzero(d == -1)
    return rs.astype(np.int64), s.astype(np.int64), e.astype(np.int64)


def etiqueter(masque, huit):
    """Étiquettes 1..n (0 = hors masque), numérotées dans l'ordre de balayage ; rend (étiquettes, n)."""
    masque = np.asarray(masque, dtype=bool)
    h, w = masque.shape
    rs, s, e = _runs(masque)
    nr = rs.size
    et = np.zeros((h, w), dtype=np.int32)
    if nr == 0:
        return et, 0
    c = 1 if huit else 0
    w2 = w + 3
    ks = rs * w2 + s
    ke = rs * w2 + e
    lo = np.searchsorted(ke, (rs + 1) * w2 + s - c, side="right")
    hi = np.searchsorted(ks, (rs + 1) * w2 + e + c, side="left")
    nb = np.maximum(hi - lo, 0)
    total = int(nb.sum())
    lab = np.arange(nr, dtype=np.int64)
    if total:
        a = np.repeat(np.arange(nr, dtype=np.int64), nb)
        debut = np.cumsum(nb) - nb
        b = np.repeat(lo - debut, nb) + np.arange(total, dtype=np.int64)
        for _ in range(nr + 1):
            la, lb = lab[a], lab[b]
            diff = la != lb
            if not diff.any():
                break
            a, b, la, lb = a[diff], b[diff], la[diff], lb[diff]
            np.minimum.at(lab, np.maximum(la, lb), np.minimum(la, lb))
            for _ in range(64):
                nl = lab[lab]
                if np.array_equal(nl, lab):
                    break
                lab = nl
    racines, inv = np.unique(lab, return_inverse=True)
    num = (inv + 1).astype(np.int32)
    longueurs = e - s
    debut = np.cumsum(longueurs) - longueurs
    idx = np.repeat(rs * w + s - debut, longueurs) + np.arange(int(longueurs.sum()), dtype=np.int64)
    et.ravel()[idx] = np.repeat(num, longueurs)
    return et, int(racines.size)


def tailles(et, n):
    """Nombre de pixels de chaque étiquette (indice 0 : hors masque)."""
    return np.bincount(et.ravel(), minlength=n + 1)


def touche_bord(et, n):
    bord = np.zeros(n + 1, dtype=bool)
    for ligne in (et[0, :], et[-1, :], et[:, 0], et[:, -1]):
        bord[ligne] = True
    bord[0] = False
    return bord
