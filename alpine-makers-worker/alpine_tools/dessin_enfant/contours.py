# -*- coding: utf-8 -*-
"""Boucles et polygones à trous (portage exact de Contours.kt d'Alpine Gift).

Une boucle est un tableau numpy (n, 2) float64 en mm, y vers le haut, matière à gauche (extérieur en sens direct,
trou en sens indirect). Un polygone est un couple (extérieur, [trous]). Les calculs suivent l'ordre des opérations de
l'application (sommes séquentielles, premier maximum) pour rendre les mêmes sommets.
"""
import math

import numpy as np


def boucle(x, y):
    return np.column_stack([np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)])


def aire(b):
    """Aire signée (positive si sens direct), somme dans l'ordre des sommets comme Boucle.aire."""
    x, y = b[:, 0], b[:, 1]
    t = x * np.roll(y, -1) - np.roll(x, -1) * y
    return float(np.cumsum(t)[-1]) / 2 if len(t) else 0.0


def inverse(b):
    return b[::-1].copy()


def contient(b, px, py):
    x, y = b[:, 0], b[:, 1]
    xj, yj = np.roll(x, 1), np.roll(y, 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        traverse = ((y > py) != (yj > py)) & (px < (xj - x) * (py - y) / (yj - y) + x)
    return bool(np.count_nonzero(traverse) % 2)


def boite_boucle(b):
    return [float(b[:, 0].min()), float(b[:, 1].min()), float(b[:, 0].max()), float(b[:, 1].max())]


def boite(boucles):
    r = [math.inf, math.inf, -math.inf, -math.inf]
    for b in boucles:
        c = boite_boucle(b)
        r = [min(r[0], c[0]), min(r[1], c[1]), max(r[2], c[2]), max(r[3], c[3])]
    return r


def regrouper(boucles):
    """Boucles sans croisement → polygones d'après l'imbrication : profondeur paire → extérieur (sens direct),
    impaire → trou de la boucle qui la contient directement (sens indirect)."""
    valides = [b for b in boucles if len(b) >= 3 and abs(aire(b)) > 1e-9]
    aires = [abs(aire(b)) for b in valides]
    boites = [boite_boucle(b) for b in valides]
    n = len(valides)
    parent = [-1] * n
    profondeur = [0] * n
    for i in range(n):
        px, py = float(valides[i][0, 0]), float(valides[i][0, 1])
        meilleur = -1
        for j in range(n):
            if i == j or aires[j] <= aires[i]:
                continue
            bj = boites[j]
            if px < bj[0] or px > bj[2] or py < bj[1] or py > bj[3]:
                continue
            if contient(valides[j], px, py):
                profondeur[i] += 1
                if meilleur < 0 or aires[j] < aires[meilleur]:
                    meilleur = j
        parent[i] = meilleur
    polys = []
    for e in range(n):
        if profondeur[e] % 2:
            continue
        ext = inverse(valides[e]) if aire(valides[e]) < 0 else valides[e]
        trous = [inverse(valides[t]) if aire(valides[t]) > 0 else valides[t]
                 for t in range(n) if profondeur[t] % 2 == 1 and parent[t] == e]
        polys.append((ext, trous))
    return polys


def boucles_de(polys):
    return [b for ext, trous in polys for b in [ext] + list(trous)]


def distance_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0.0:
        return math.hypot(px - ax, py - ay)
    t = min(1.0, max(0.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _distances(xs, ys, ax, ay, bx, by):
    """distance_segment vectorisée (mêmes opérations, dans le même ordre)."""
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0.0:
        return np.hypot(xs - ax, ys - ay)
    t = np.clip(((xs - ax) * dx + (ys - ay) * dy) / l2, 0.0, 1.0)
    return np.hypot(xs - (ax + t * dx), ys - (ay + t * dy))


def simplifier(b, tolerance, pas):
    """Retire les points à moins de ``pas`` du dernier gardé, puis Douglas–Peucker fermé à ``tolerance`` ; les sommets
    restants sont des sommets d'origine. None si la boucle disparaît."""
    if len(b) < 4:
        return b
    xs, ys = b[:, 0].tolist(), b[:, 1].tolist()
    gx, gy = [xs[0]], [ys[0]]
    for i in range(1, len(xs)):
        if math.hypot(xs[i] - gx[-1], ys[i] - gy[-1]) >= pas:
            gx.append(xs[i])
            gy.append(ys[i])
    if len(gx) > 3 and math.hypot(gx[-1] - gx[0], gy[-1] - gy[0]) < pas:
        gx.pop()
        gy.pop()
    if len(gx) < 3:
        return None
    n = len(gx)
    X, Y = np.asarray(gx), np.asarray(gy)
    d0 = np.hypot(X[1:] - X[0], Y[1:] - Y[0])
    loin = 1 + int(np.argmax(d0)) if n > 1 else 0
    garde = np.zeros(n, dtype=bool)
    garde[0] = garde[loin] = True
    pile = [(0, loin), (loin, n)]
    while pile:
        a, z = pile.pop()
        if z - a < 2:
            continue
        d = _distances(X[a + 1:z], Y[a + 1:z], gx[a], gy[a], gx[z % n], gy[z % n])
        k = int(np.argmax(d))
        if d[k] > tolerance:
            i = a + 1 + k
            garde[i] = True
            pile.append((i, z))
            pile.append((a, i))
    if int(garde.sum()) < 3:
        return None
    return sans_alignes(np.column_stack([X[garde], Y[garde]]))


def sans_alignes(b):
    """Retire les sommets doublons et alignés (la triangulation n'a ainsi que de vrais sommets)."""
    xs = b[:, 0].tolist()
    ys = b[:, 1].tolist()
    change = True
    while change and len(xs) >= 3:
        change = False
        i = 0
        while i < len(xs) and len(xs) >= 3:
            n = len(xs)
            p = (i - 1 + n) % n
            s = (i + 1) % n
            croix = (xs[i] - xs[p]) * (ys[s] - ys[p]) - (ys[i] - ys[p]) * (xs[s] - xs[p])
            longueur = math.hypot(xs[s] - xs[p], ys[s] - ys[p])
            doublon = xs[i] == xs[p] and ys[i] == ys[p]
            if doublon or abs(croix) <= 1e-9 * max(longueur, 1e-9) * max(longueur, 1.0):
                del xs[i]
                del ys[i]
                change = True
            else:
                i += 1
    return np.column_stack([xs, ys]) if len(xs) >= 3 else None


def miroir_x(b, sym):
    """Boucle retournée gauche-droite (x → sym − x) puis remise dans son sens : extérieur positif, trou négatif."""
    return inverse(np.column_stack([sym - b[:, 0], b[:, 1]]))
