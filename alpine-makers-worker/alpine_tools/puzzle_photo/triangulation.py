# -*- coding: utf-8 -*-
"""Triangulation d'un polygone à trous, code propre à Alpine Makers (sans Earcut), alignée sur Triangulation.kt
d'Alpine Gift (lui-même porté de ``porte_cles/triangulation.py``) : mêmes ponts, même ordre des oreilles.

1. Raccordement : chaque trou est relié à l'anneau extérieur par un pont (sommet du trou → sommet de l'anneau) qui ne
   coupe aucun bord et part vers la matière aux deux bouts ; l'anneau devient une seule boucle … V, M, trou…, M, V, …
2. Découpage d'oreilles : un sommet convexe dont le triangle (précédent, sommet, suivant) ne contient aucun autre sommet
   est retiré ; les sommets réflexes (seuls candidats possibles) sont rangés dans une grille.

Chaque arête du contour appartient à exactement un triangle et chaque sommet est utilisé : faces et parois forment un
maillage fermé. Triangles en sens direct ; indices dans l'ordre des sommets : extérieur puis trous.
"""
import math

import numpy as np

from . import kotlin as K


def _aire2(xs, ys, idx):
    a = 0.0
    n = len(idx)
    for i in range(n):
        p, q = idx[i], idx[(i + 1) % n]
        a += xs[p] * ys[q] - xs[q] * ys[p]
    return a


def trianguler(exterieur, trous):
    """``exterieur``, ``trous`` : tableaux (n, 2). Rend une liste plate d'indices (3 par triangle)."""
    blocs = [exterieur] + list(trous)
    xs = [float(v) for b in blocs for v in b[:, 0].tolist()]
    ys = [float(v) for b in blocs for v in b[:, 1].tolist()]
    boucles = []
    debut = 0
    for k, b in enumerate(blocs):
        idx = list(range(debut, debut + len(b)))
        debut += len(b)
        a = _aire2(xs, ys, idx)
        # Extérieur en sens direct, trous en sens indirect : la matière est toujours à gauche.
        if (k == 0 and a < 0) or (k > 0 and a > 0):
            idx.reverse()
        if len(idx) >= 3 and a != 0.0:
            boucles.append(idx)
        elif k == 0:
            return []
    if not boucles:
        return []
    anneau = _Raccord(xs, ys).raccorder(boucles[0], boucles[1:])
    return _oreilles(xs, ys, anneau)


class _Raccord:
    def __init__(self, xs, ys):
        self.xs, self.ys = xs, ys
        self.X = np.asarray(xs, dtype=np.float64)
        self.Y = np.asarray(ys, dtype=np.float64)

    @staticmethod
    def _angle(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a + 2 * math.pi if a < 0 else a

    def _vers_interieur(self, prec, som, suiv, cible):
        xs, ys = self.xs, self.ys
        ex, ey = xs[suiv] - xs[som], ys[suiv] - ys[som]
        ax, ay = xs[prec] - xs[som], ys[prec] - ys[som]
        dx, dy = xs[cible] - xs[som], ys[cible] - ys[som]
        if dx == 0.0 and dy == 0.0:
            return False
        ouverture = self._angle(ex, ey, ax, ay)
        d = self._angle(ex, ey, dx, dy)
        return 0 < d < ouverture

    def _bloque(self, p, q, aretes_a, aretes_c):
        """Le segment p–q coupe-t-il ou touche-t-il une arête (hors arêtes qui partagent un bout avec p ou q) ?"""
        X, Y = self.X, self.Y
        px, py, qx, qy = X[p], Y[p], X[q], Y[q]
        rx, ry = qx - px, qy - py
        ax, ay, cx, cy = X[aretes_a], Y[aretes_a], X[aretes_c], Y[aretes_c]
        lox, hix, loy, hiy = min(px, qx), max(px, qx), min(py, qy), max(py, qy)
        dans = ~((np.maximum(ax, cx) < lox) | (np.minimum(ax, cx) > hix) | (np.maximum(ay, cy) < loy) | (np.minimum(ay, cy) > hiy))
        partage = ((ax == px) & (ay == py)) | ((ax == qx) & (ay == qy)) | ((cx == px) & (cy == py)) | ((cx == qx) & (cy == qy))
        o1 = rx * (ay - py) - ry * (ax - px)
        o2 = rx * (cy - py) - ry * (cx - px)
        sx, sy = cx - ax, cy - ay
        o3 = sx * (py - ay) - sy * (px - ax)
        o4 = sx * (qy - ay) - sy * (qx - ax)
        return bool(np.any(dans & ~partage & (o1 * o2 <= 0) & (o3 * o4 <= 0)))

    @staticmethod
    def _aretes(boucles):
        a = np.concatenate([np.asarray(b, dtype=np.int64) for b in boucles])
        c = np.concatenate([np.roll(np.asarray(b, dtype=np.int64), -1) for b in boucles])
        return a, c

    def raccorder(self, exterieur, trous):
        xs = self.xs
        anneau = list(exterieur)
        restants = sorted(trous, key=lambda t: -max(xs[i] for i in t))     # tri stable, du plus à droite
        while restants:
            trou = restants.pop(0)
            aretes = self._aretes([anneau, trou] + restants)
            km, pos = self._choisir(anneau, trou, aretes)
            tour = trou[km:] + trou[:km]
            anneau = anneau[:pos + 1] + tour + [tour[0], anneau[pos]] + anneau[pos + 1:]
        return anneau

    def _choisir(self, anneau, trou, aretes):
        xs, ys, X, Y = self.xs, self.ys, self.X, self.Y
        n = len(anneau)
        m_ordre = sorted(range(len(trou)), key=lambda k: -xs[trou[k]])
        pos_anneau = np.asarray(anneau, dtype=np.int64)
        for etape in (0, 1):
            limite_m = min(8, len(trou)) if etape == 0 else len(trou)
            limite_v = min(48, n) if etape == 0 else n
            for q in range(limite_m):
                km = m_ordre[q]
                m = trou[km]
                m_prec, m_suiv = trou[(km - 1) % len(trou)], trou[(km + 1) % len(trou)]
                dist = K.hypot_vec(X[pos_anneau] - xs[m], Y[pos_anneau] - ys[m])
                ordre = np.argsort(dist, kind="stable")
                for pos in ordre[:limite_v].tolist():
                    v = anneau[pos]
                    if not self._vers_interieur(anneau[(pos - 1) % n], v, anneau[(pos + 1) % n], m):
                        continue
                    if not self._vers_interieur(m_prec, m, m_suiv, v):
                        continue
                    if self._bloque(m, v, *aretes):
                        continue
                    return km, pos
        # Cas dégénéré (contours qui se touchent) : pont le plus court, la fermeture reste garantie.
        km = m_ordre[0]
        m = trou[km]
        dist = K.hypot_vec(X[pos_anneau] - xs[m], Y[pos_anneau] - ys[m])
        return km, int(np.argmin(dist))


def _oreilles(xs, ys, anneau):
    n = len(anneau)
    if n < 3:
        return []
    X = [xs[i] for i in anneau]
    Y = [ys[i] for i in anneau]
    prv = [(i - 1) % n for i in range(n)]
    nxt = [(i + 1) % n for i in range(n)]
    vivant = [True] * n
    out = []
    x0, y0 = min(X), min(Y)
    cote = max(max(max(X) - x0, max(Y) - y0), 1e-9) / max(1.0, math.sqrt(n) / 2)

    def croix(a, b, c):
        return (X[b] - X[a]) * (Y[c] - Y[b]) - (Y[b] - Y[a]) * (X[c] - X[b])

    def case(v, o):
        return int((v - o) / cote)

    grille = {}
    for i in range(n):
        if croix(prv[i], i, nxt[i]) < 0:
            grille.setdefault((case(X[i], x0), case(Y[i], y0)), []).append(i)

    def oreille(p):
        a, c = prv[p], nxt[p]
        if croix(a, p, c) <= 0:
            return False
        ax, ay, bx, by, cx, cy = X[a], Y[a], X[p], Y[p], X[c], Y[c]
        gx0, gx1 = case(min(ax, min(bx, cx)), x0), case(max(ax, max(bx, cx)), x0)
        gy0, gy1 = case(min(ay, min(by, cy)), y0), case(max(ay, max(by, cy)), y0)
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                for q in grille.get((gx, gy), ()):
                    if not vivant[q] or q == a or q == p or q == c:
                        continue
                    qx, qy = X[q], Y[q]
                    if (qx == ax and qy == ay) or (qx == bx and qy == by) or (qx == cx and qy == cy):
                        continue
                    if ((bx - ax) * (qy - ay) - (by - ay) * (qx - ax) >= 0 and
                            (cx - bx) * (qy - by) - (cy - by) * (qx - bx) >= 0 and
                            (ax - cx) * (qy - cy) - (ay - cy) * (qx - cx) >= 0):
                        return False
        return True

    def retirer(p):
        a, c = prv[p], nxt[p]
        out.extend((anneau[a], anneau[p], anneau[c]))
        nxt[a] = c
        prv[c] = a
        vivant[p] = False

    reste = n
    p = 0
    echecs = 0
    while reste > 3:
        a, c = prv[p], nxt[p]
        if croix(a, p, c) == 0.0 or oreille(p):
            # Sommet aligné : triangle plat, les arêtes restent appariées.
            retirer(p)
            reste -= 1
            p = a
            echecs = 0
            continue
        p = c
        echecs += 1
        if echecs > reste:
            # Blocage numérique : retire le sommet le plus convexe (la topologie reste fermée).
            meilleur, valeur = p, -math.inf
            q = p
            for _ in range(reste):
                v = croix(prv[q], q, nxt[q])
                if v > valeur:
                    meilleur, valeur = q, v
                q = nxt[q]
            suivant = nxt[meilleur]
            retirer(meilleur)
            reste -= 1
            p = suivant
            echecs = 0
    out.extend((anneau[prv[p]], anneau[p], anneau[nxt[p]]))
    return out
