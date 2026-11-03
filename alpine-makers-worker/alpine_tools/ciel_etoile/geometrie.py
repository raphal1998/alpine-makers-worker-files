# -*- coding: utf-8 -*-
"""Contours fermés en mm (y vers le haut) : portage de la classe Boucle et de Contours.sansAlignes / boite
(C:\\AlpineGift core Contours.kt). Orientation : matière à gauche (extérieur direct, trou indirect)."""
import math


class Boucle:
    __slots__ = ("x", "y", "_aire")

    def __init__(self, x, y):
        self.x = [float(v) for v in x]
        self.y = [float(v) for v in y]
        if len(self.x) != len(self.y):
            raise ValueError("Boucle incohérente.")
        self._aire = None

    @property
    def n(self):
        return len(self.x)

    @property
    def aire(self):
        """Aire signée (positive si sens direct)."""
        if self._aire is None:
            a = 0.0
            x, y, n = self.x, self.y, len(self.x)
            for i in range(n):
                j = (i + 1) % n
                a += x[i] * y[j] - x[j] * y[i]
            self._aire = a / 2
        return self._aire

    def inverse(self):
        return Boucle(self.x[::-1], self.y[::-1])

    def contient(self, px, py):
        dedans = False
        x, y = self.x, self.y
        j = len(x) - 1
        for i in range(len(x)):
            if (y[i] > py) != (y[j] > py) and px < (x[j] - x[i]) * (py - y[i]) / (y[j] - y[i]) + x[i]:
                dedans = not dedans
            j = i
        return dedans

    def boite(self):
        return [min(self.x), min(self.y), max(self.x), max(self.y)]

    def translater(self, dx, dy):
        return Boucle([v + dx for v in self.x], [v + dy for v in self.y])


def sans_alignes(b):
    """Retire les sommets doublons et alignés (Contours.sansAlignes) ; None s'il reste moins de 3 sommets."""
    xs, ys = list(b.x), list(b.y)
    change = True
    while change and len(xs) >= 3:
        change = False
        i = 0
        while i < len(xs) and len(xs) >= 3:
            p = (i - 1 + len(xs)) % len(xs)
            s = (i + 1) % len(xs)
            croix = (xs[i] - xs[p]) * (ys[s] - ys[p]) - (ys[i] - ys[p]) * (xs[s] - xs[p])
            l = math.hypot(xs[s] - xs[p], ys[s] - ys[p])
            doublon = xs[i] == xs[p] and ys[i] == ys[p]
            if doublon or abs(croix) <= 1e-9 * max(l, 1e-9) * max(l, 1.0):
                del xs[i]
                del ys[i]
                change = True
            else:
                i += 1
    return Boucle(xs, ys) if len(xs) >= 3 else None


def boite(boucles):
    b = [math.inf, math.inf, -math.inf, -math.inf]
    for l in boucles:
        c = l.boite()
        b = [min(b[0], c[0]), min(b[1], c[1]), max(b[2], c[2]), max(b[3], c[3])]
    return b
