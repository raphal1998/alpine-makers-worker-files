"""Triangulation d'un anneau simple (fonds plats de la lithophanie en volume), portée d'Alpine Gift (Triangulation.kt,
appel ``trianguler(xs, ys, intArrayOf(0))`` : une seule boucle, sans trou). Code propre à Alpine Makers, sans Earcut.

Découpage d'oreilles : un sommet convexe dont le triangle (précédent, sommet, suivant) ne contient aucun autre sommet
est retiré ; les sommets réflexes (seuls candidats possibles) sont rangés dans une grille. Chaque retrait consomme les
deux arêtes de l'anneau qui touchent le sommet et en crée une : chaque arête du contour appartient à exactement un
triangle et chaque sommet est utilisé (fond et parois partagent leurs arêtes). Triangles en sens direct (normale +z).
Même ordre de parcours que l'application : mêmes triangles, dans le même ordre.
"""
import math


def _aire2(xs, ys, idx):
    a = 0.0
    n = len(idx)
    for i in range(n):
        p, q = idx[i], idx[(i + 1) % n]
        a += xs[p] * ys[q] - xs[q] * ys[p]
    return a


def trianguler(xs, ys):
    """Indices (liste plate, 3 par triangle) d'un polygone simple donné par ses sommets ; sens corrigé si indirect."""
    xs = [float(v) for v in xs]
    ys = [float(v) for v in ys]
    idx = list(range(len(xs)))
    a = _aire2(xs, ys, idx)
    if a < 0:
        idx.reverse()
    if len(idx) < 3 or a == 0.0:
        return []
    return _oreilles(xs, ys, idx)


def _oreilles(xs, ys, anneau):
    n = len(anneau)
    if n < 3:
        return []
    X = [xs[i] for i in anneau]
    Y = [ys[i] for i in anneau]
    prv = [(i - 1 + n) % n for i in range(n)]
    nxt = [(i + 1) % n for i in range(n)]
    vivant = [True] * n
    out = []

    def croix(a, b, c):
        return (X[b] - X[a]) * (Y[c] - Y[b]) - (Y[b] - Y[a]) * (X[c] - X[b])

    x0, y0 = min(X), min(Y)
    cote = max(max(max(X) - x0, max(Y) - y0), 1e-9) / max(1.0, math.sqrt(n) / 2)

    def case(v, o):
        return int((v - o) / cote)

    # Grille des sommets réflexes (un sommet convexe ne redevient jamais réflexe).
    grille = {}
    for i in range(n):
        if croix(prv[i], i, nxt[i]) < 0:
            grille.setdefault((case(X[i], x0), case(Y[i], y0)), []).append(i)

    def oreille(p):
        a, c = prv[p], nxt[p]
        if croix(a, p, c) <= 0:
            return False
        if not grille:
            return True
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
