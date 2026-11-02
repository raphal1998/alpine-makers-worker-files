"""Triangulation d'une face plane à trous, écrite pour Alpine Contour.

1. Chaque trou est relié au contour extérieur par un pont : du sommet le plus à droite du trou vers le sommet visible le
   plus proche de l'anneau courant (le pont ne coupe aucune arête, il part dans l'angle intérieur du sommet). L'anneau
   devient un seul polygone faiblement simple (le pont est parcouru dans les deux sens).
2. Découpage des oreilles : un sommet convexe dont le triangle ne contient aucun autre sommet de l'anneau (bord compris)
   et dont la diagonale ne coupe aucune arête est retiré avec son triangle. Un sommet aligné donne un triangle plat :
   tous les sommets du contour restent dans la face, chaque arête de l'anneau appartient à un seul triangle, la face
   se raccorde donc exactement aux parois (maillage fermé).

Rend des triangles (indices dans l'ordre : extérieur puis trous) en sens direct (normale +z).
"""
import numpy as np


def trianguler_polygone(p):
    boucles = [p.exterieur] + list(p.trous)
    xs = np.concatenate([b.x for b in boucles])
    ys = np.concatenate([b.y for b in boucles])
    debuts, k = [], 0
    for b in boucles:
        debuts.append(k)
        k += b.n
    debuts.append(k)
    anneaux = [list(range(debuts[i], debuts[i + 1])) for i in range(len(boucles))]
    return trianguler(xs, ys, anneaux[0], anneaux[1:])


def _aire(xs, ys, idx):
    x, y = xs[idx], ys[idx]
    return float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y) / 2)


def _orient(ax, ay, bx, by, cx, cy):
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _croise(ax, ay, bx, by, cx, cy, dx, dy):
    """Le segment [a, b] touche-t-il les segments [c, d] (tableaux) ? Contact et recouvrement compris."""
    o1 = _orient(ax, ay, bx, by, cx, cy)
    o2 = _orient(ax, ay, bx, by, dx, dy)
    o3 = _orient(cx, cy, dx, dy, ax, ay)
    o4 = _orient(cx, cy, dx, dy, bx, by)

    def sur(px, py, qx, qy, rx, ry):  # r sur [p, q] sachant r aligné
        return (np.minimum(px, qx) <= rx) & (rx <= np.maximum(px, qx)) & (np.minimum(py, qy) <= ry) & (ry <= np.maximum(py, qy))

    net = ((o1 > 0) & (o2 < 0) | (o1 < 0) & (o2 > 0)) & ((o3 > 0) & (o4 < 0) | (o3 < 0) & (o4 > 0))
    return (net | ((o1 == 0) & sur(ax, ay, bx, by, cx, cy)) | ((o2 == 0) & sur(ax, ay, bx, by, dx, dy))
            | ((o3 == 0) & sur(cx, cy, dx, dy, ax, ay)) | ((o4 == 0) & sur(cx, cy, dx, dy, bx, by)))


def _dans_angle(xs, ys, p, v, n, h):
    """La direction v → h part-elle dans l'angle intérieur en v (anneau direct, matière à gauche) ?"""
    gauche_sortie = _orient(xs[v], ys[v], xs[n], ys[n], xs[h], ys[h]) > 0
    gauche_entree = _orient(xs[p], ys[p], xs[v], ys[v], xs[h], ys[h]) > 0
    if _orient(xs[p], ys[p], xs[v], ys[v], xs[n], ys[n]) > 0:
        return gauche_sortie and gauche_entree
    return gauche_sortie or gauche_entree


def _relier(xs, ys, anneau, trous):
    """Raccorde les trous à l'anneau extérieur par des ponts ; rend l'anneau unique (liste d'indices)."""
    restants = sorted(trous, key=lambda t: -float(xs[t].max()))
    while restants:
        trou = restants.pop(0)
        k = int(np.argmax(xs[trou]))
        # Le plus à droite, puis le plus bas en cas d'égalité.
        egaux = [i for i, s in enumerate(trou) if xs[s] == xs[trou[k]]]
        k = min(egaux, key=lambda i: ys[trou[i]])
        h = trou[k]
        tour = trou[k:] + trou[:k]
        a = np.array(anneau)
        autres = [a] + [np.array(t) for t in restants] + [np.array(trou)]
        cx = np.concatenate([xs[o] for o in autres])
        cy = np.concatenate([ys[o] for o in autres])
        dx = np.concatenate([xs[np.roll(o, -1)] for o in autres])
        dy = np.concatenate([ys[np.roll(o, -1)] for o in autres])
        hx, hy = xs[h], ys[h]
        dist = np.hypot(xs[a] - hx, ys[a] - hy)
        choisi = None
        for pos in np.argsort(dist, kind="stable").tolist():
            v = anneau[pos]
            vx, vy = xs[v], ys[v]
            if vx == hx and vy == hy:
                continue
            if not _dans_angle(xs, ys, anneau[pos - 1], v, anneau[(pos + 1) % len(anneau)], h):
                continue
            # Arêtes qui touchent une extrémité du pont : ignorées (elles partent de v ou de h).
            libre = ~(((cx == vx) & (cy == vy)) | ((dx == vx) & (dy == vy)) | ((cx == hx) & (cy == hy)) | ((dx == hx) & (dy == hy)))
            if np.any(_croise(vx, vy, hx, hy, cx[libre], cy[libre], dx[libre], dy[libre])):
                continue
            choisi = pos
            break
        if choisi is None:
            raise ValueError("Face impossible à trianguler : un trou touche le contour extérieur.")
        anneau = anneau[:choisi + 1] + tour + [h, anneau[choisi]] + anneau[choisi + 1:]
    return anneau


def trianguler(xs, ys, exterieur, trous=()):
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    ext = list(exterieur)
    if len(ext) < 3:
        return []
    if _aire(xs, ys, ext) < 0:
        ext.reverse()
    sens_trous = []
    for t in trous:
        t = list(t)
        if len(t) < 3:
            continue
        if _aire(xs, ys, t) > 0:
            t.reverse()
        sens_trous.append(t)
    anneau = _relier(xs, ys, ext, sens_trous)
    return _oreilles(xs, ys, anneau)


def _oreilles(xs, ys, anneau):
    n = len(anneau)
    ri = np.array(anneau, dtype=np.int64)
    X, Y = xs[ri], ys[ri]
    suiv = np.roll(np.arange(n), -1)
    prec = np.roll(np.arange(n), 1)
    actif = np.ones(n, dtype=bool)
    triangles = []
    reste = n
    i = 0
    echecs = 0
    while reste > 3:
        p, s = int(prec[i]), int(suiv[i])
        c = _orient(X[p], Y[p], X[i], Y[i], X[s], Y[s])
        if c == 0:
            coupe = True  # sommet aligné ou doublon : triangle plat, aucune arête perdue
        else:
            coupe = c > 0 and _est_oreille(X, Y, actif, suiv, p, i, s)
        # Polygone dégénéré (arrondis flottants) : après un tour complet sans oreille, on retire un sommet convexe,
        # puis n'importe lequel ; chaque arête reste couverte une fois, la face reste fermée.
        if not coupe and echecs > 2 * reste:
            coupe = c > 0 or echecs > 3 * reste
        if coupe:
            triangles.append((int(ri[p]), int(ri[i]), int(ri[s])))
            actif[i] = False
            suiv[p], prec[s] = s, p
            reste -= 1
            i = p
            echecs = 0
        else:
            i = s
            echecs += 1
    if reste == 3:
        p, s = int(prec[i]), int(suiv[i])
        triangles.append((int(ri[p]), int(ri[i]), int(ri[s])))
    return [t for t in triangles if t[0] != t[1] and t[1] != t[2] and t[0] != t[2]]


def _est_oreille(X, Y, actif, suiv, p, i, s):
    ax, ay, bx, by, cx, cy = X[p], Y[p], X[i], Y[i], X[s], Y[s]
    idx = np.flatnonzero(actif)
    px, py = X[idx], Y[idx]
    coin = ((px == ax) & (py == ay)) | ((px == bx) & (py == by)) | ((px == cx) & (py == cy))
    dedans = (_orient(ax, ay, bx, by, px, py) >= 0) & (_orient(bx, by, cx, cy, px, py) >= 0) & (_orient(cx, cy, ax, ay, px, py) >= 0)
    if np.any(dedans & ~coin):
        return False
    # La diagonale p → s ne doit couper aucune arête de l'anneau (hors arêtes qui la touchent par ses extrémités).
    j = idx
    k = suiv[j]
    ex, ey, fx, fy = X[j], Y[j], X[k], Y[k]
    libre = ~(((ex == ax) & (ey == ay)) | ((fx == ax) & (fy == ay)) | ((ex == cx) & (ey == cy)) | ((fx == cx) & (fy == cy)))
    return not np.any(_croise(ax, ay, cx, cy, ex[libre], ey[libre], fx[libre], fy[libre]))
