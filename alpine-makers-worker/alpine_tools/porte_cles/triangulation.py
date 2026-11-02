# -*- coding: utf-8 -*-
"""Triangulation d'un polygone à trous, écrite pour les Alpine Tools.

1. Raccordement : chaque trou est relié à l'anneau extérieur par un pont (segment sommet du trou → sommet de
   l'anneau) qui ne coupe aucun bord et part vers l'intérieur de la matière aux deux bouts ; l'anneau devient une
   seule boucle  … V, M, trou…, M, V, …  (le pont est parcouru dans les deux sens).
2. Découpage d'oreilles : un sommet convexe dont le triangle (précédent, sommet, suivant) ne contient aucun autre
   sommet est retiré ; les sommets réflexes candidats sont rangés dans une grille pour que le test reste local.

Garantie de fermeture : chaque retrait utilise les deux arêtes de l'anneau qui touchent le sommet et en crée une
nouvelle, donc chaque arête du contour appartient à exactement un triangle, chaque sommet du contour est utilisé
(pas de jointure en T avec les parois) et un sommet aligné donne un triangle plat. Triangles en sens direct
(normale +z) ; indices dans l'ordre : extérieur puis trous.
"""
import math

import numpy as np


def _aire2(x, y):
    return float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))


def trianguler_polygone(exterieur, trous):
    """``exterieur`` et ``trous`` : tableaux (n, 2). Rend une liste plate d'indices (3 par triangle)."""
    blocs = [np.asarray(exterieur, dtype=np.float64)] + [np.asarray(t, dtype=np.float64) for t in trous]
    pts = np.vstack(blocs)
    debut = 0
    boucles = []
    for k, b in enumerate(blocs):
        idx = list(range(debut, debut + len(b)))
        debut += len(b)
        a = _aire2(b[:, 0], b[:, 1])
        # Extérieur en sens direct, trous en sens indirect : la matière est toujours à gauche.
        if (k == 0 and a < 0) or (k > 0 and a > 0):
            idx.reverse()
        if len(idx) >= 3 and a != 0:
            boucles.append(idx)
        elif k == 0:
            return []
    anneau = _raccorder(pts, boucles[0], boucles[1:])
    return _oreilles(pts, anneau)


# --- 1. Raccordement des trous ---------------------------------------------------------------------------------------
def _angle(ux, uy, vx, vy):
    """Angle de u vers v, dans [0, 2π)."""
    a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
    return a + 2 * math.pi if a < 0 else a


def _vers_interieur(pts, prec, som, suiv, cible):
    """La direction som → cible part-elle dans la matière (à gauche de prec → som → suiv) ?"""
    sx, sy = pts[som]
    ex, ey = pts[suiv] - pts[som]
    ax, ay = pts[prec] - pts[som]
    dx, dy = pts[cible] - pts[som]
    if dx == 0 and dy == 0:
        return False
    ouverture = _angle(ex, ey, ax, ay)
    d = _angle(ex, ey, dx, dy)
    return 0 < d < ouverture


def _bloque(p, q, a, b):
    """Masque des arêtes (a, b) que le segment p–q coupe ou touche (hors arêtes qui partagent un bout avec p ou q)."""
    rx, ry = q[0] - p[0], q[1] - p[1]
    o1 = rx * (a[:, 1] - p[1]) - ry * (a[:, 0] - p[0])
    o2 = rx * (b[:, 1] - p[1]) - ry * (b[:, 0] - p[0])
    sx, sy = b[:, 0] - a[:, 0], b[:, 1] - a[:, 1]
    o3 = sx * (p[1] - a[:, 1]) - sy * (p[0] - a[:, 0])
    o4 = sx * (q[1] - a[:, 1]) - sy * (q[0] - a[:, 0])
    touche = (o1 * o2 <= 0) & (o3 * o4 <= 0)
    # Les deux segments sur une même droite mais disjoints ne se touchent pas.
    aligne = (o1 == 0) & (o2 == 0)
    if aligne.any():
        lo = np.minimum(p, q)
        hi = np.maximum(p, q)
        amin = np.minimum(a, b)
        amax = np.maximum(a, b)
        recouvre = (amin[:, 0] <= hi[0]) & (amax[:, 0] >= lo[0]) & (amin[:, 1] <= hi[1]) & (amax[:, 1] >= lo[1])
        touche &= ~aligne | recouvre
    partage = ((a == p).all(axis=1) | (a == q).all(axis=1) | (b == p).all(axis=1) | (b == q).all(axis=1))
    return touche & ~partage


def _aretes(pts, boucles):
    a = np.vstack([pts[np.asarray(b)] for b in boucles])
    b = np.vstack([pts[np.roll(np.asarray(b), -1)] for b in boucles])
    return a, b


def _raccorder(pts, anneau, trous):
    """Relie les trous à l'anneau, du plus à droite au plus à gauche ; rend la boucle unique."""
    restants = sorted(trous, key=lambda t: -float(pts[t, 0].max()))
    while restants:
        trou = restants.pop(0)
        a, b = _aretes(pts, [anneau, trou] + restants)
        n = len(anneau)
        pos_anneau = np.asarray(anneau)
        m_ordre = sorted(range(len(trou)), key=lambda k: -pts[trou[k], 0])
        choix = None
        for limite_m, limite_v in ((8, 48), (len(trou), n)):
            for km in m_ordre[:limite_m]:
                m = trou[km]
                pm = pts[m]
                m_prec, m_suiv = trou[km - 1], trou[(km + 1) % len(trou)]
                dist = np.hypot(pts[pos_anneau, 0] - pm[0], pts[pos_anneau, 1] - pm[1])
                for pos in np.argsort(dist, kind="stable")[:limite_v].tolist():
                    v = anneau[pos]
                    if not _vers_interieur(pts, anneau[pos - 1], v, anneau[(pos + 1) % n], m):
                        continue
                    if not _vers_interieur(pts, m_prec, m, m_suiv, v):
                        continue
                    if _bloque(pm, pts[v], a, b).any():
                        continue
                    choix = (km, pos)
                    break
                if choix:
                    break
            if choix:
                break
        if choix is None:
            # Cas dégénéré (contours qui se touchent) : pont le plus court, la fermeture reste garantie.
            km = m_ordre[0]
            pm = pts[trou[km]]
            choix = (km, int(np.argmin(np.hypot(pts[pos_anneau, 0] - pm[0], pts[pos_anneau, 1] - pm[1]))))
        km, pos = choix
        tour = trou[km:] + trou[:km]
        anneau = anneau[:pos + 1] + tour + [tour[0], anneau[pos]] + anneau[pos + 1:]
    return anneau


# --- 2. Découpage d'oreilles -----------------------------------------------------------------------------------------
def _oreilles(pts, anneau):
    n = len(anneau)
    if n < 3:
        return []
    X = pts[anneau, 0].tolist()
    Y = pts[anneau, 1].tolist()
    prv = [(i - 1) % n for i in range(n)]
    nxt = [(i + 1) % n for i in range(n)]
    vivant = [True] * n
    out = []

    def croix(a, b, c):
        return (X[b] - X[a]) * (Y[c] - Y[b]) - (Y[b] - Y[a]) * (X[c] - X[b])

    # Grille des sommets réflexes (un sommet convexe ne redevient jamais réflexe).
    x0, x1, y0, y1 = min(X), max(X), min(Y), max(Y)
    cote = max(x1 - x0, y1 - y0, 1e-9) / max(1.0, math.sqrt(n) / 2)
    grille = {}
    for i in range(n):
        if croix(prv[i], i, nxt[i]) < 0:
            grille.setdefault((int((X[i] - x0) / cote), int((Y[i] - y0) / cote)), []).append(i)

    def oreille(p):
        a, c = prv[p], nxt[p]
        if croix(a, p, c) <= 0:
            return False
        ax, ay, bx, by, cx, cy = X[a], Y[a], X[p], Y[p], X[c], Y[c]
        gx0, gx1 = int((min(ax, bx, cx) - x0) / cote), int((max(ax, bx, cx) - x0) / cote)
        gy0, gy1 = int((min(ay, by, cy) - y0) / cote), int((max(ay, by, cy) - y0) / cote)
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
        if croix(a, p, c) == 0 or oreille(p):
            # Sommet aligné : triangle plat, les arêtes restent appariées.
            retirer(p)
            reste -= 1
            p = a
            echecs = 0
            continue
        p = c
        echecs += 1
        if echecs > reste:
            # Blocage numérique : on retire le sommet le plus convexe (la topologie reste fermée).
            meilleur, valeur = p, -float("inf")
            q = p
            for _ in range(reste):
                v = croix(prv[q], q, nxt[q])
                if v > valeur:
                    meilleur, valeur = q, v
                q = nxt[q]
            retirer(meilleur)
            reste -= 1
            p = nxt[meilleur]
            echecs = 0
    out.extend((anneau[prv[p]], anneau[p], anneau[nxt[p]]))
    return out
