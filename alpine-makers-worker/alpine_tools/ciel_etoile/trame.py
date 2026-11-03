# -*- coding: utf-8 -*-
"""Trame d'index (un octet par pixel, ligne 0 en haut) sur laquelle on peint des formes en mm (repère y vers le haut,
origine en bas à gauche) : disques, traits à bouts ronds, anneaux, polygones pair-impair. Un pixel prend la valeur de
la dernière forme qui couvre son centre.

Portage de C:\\AlpineGift core Trame.kt : mêmes fenêtres de pixels, mêmes centres de pixel et mêmes comparaisons en
double précision, calculées par numpy sur la fenêtre de chaque forme (résultat identique pixel par pixel).
"""
import math

import numpy as np

VIDE = 255


class Trame:
    def __init__(self, l, h, pas, remplissage=VIDE):
        self.l, self.h, self.pas = int(l), int(h), float(pas)
        self.v = np.full((self.h, self.l), remplissage, dtype=np.uint8)

    @property
    def largeur_mm(self):
        return self.l * self.pas

    @property
    def hauteur_mm(self):
        return self.h * self.pas

    @classmethod
    def pour(cls, largeur_mm, hauteur_mm, pas, remplissage=VIDE):
        """Trame couvrant largeur × hauteur au pas ``pas`` (côtés arrondis au pixel, Trame.pour)."""
        l = int(largeur_mm / pas)
        if l * pas < largeur_mm - 1e-9:
            l += 1
        h = int(hauteur_mm / pas)
        if h * pas < hauteur_mm - 1e-9:
            h += 1
        return cls(max(1, l), max(1, h), pas, remplissage)

    # Centres de pixel : cx(i) = (i + 0.5) · pas ; cy(j) = hauteur − (j + 0.5) · pas.
    def _cx(self, i0, i1):
        return (np.arange(i0, i1 + 1, dtype=np.float64) + 0.5) * self.pas

    def _cy(self, j0, j1):
        return self.hauteur_mm - (np.arange(j0, j1 + 1, dtype=np.float64) + 0.5) * self.pas

    def _colonnes(self, x0, x1):
        return max(0, math.floor(x0 / self.pas - 0.5)), min(self.l - 1, math.ceil(x1 / self.pas - 0.5))

    def _lignes(self, y0, y1):
        hm = self.hauteur_mm
        return max(0, math.floor((hm - y1) / self.pas - 0.5)), min(self.h - 1, math.ceil((hm - y0) / self.pas - 0.5))

    def disque(self, x, y, r, valeur):
        i0, i1 = self._colonnes(x - r, x + r)
        j0, j1 = self._lignes(y - r, y + r)
        if i0 > i1 or j0 > j1:
            return
        dx = self._cx(i0, i1)[None, :] - x
        dy = self._cy(j0, j1)[:, None] - y
        sous = self.v[j0:j1 + 1, i0:i1 + 1]
        sous[dx * dx + dy * dy <= r * r] = valeur

    def trait(self, xa, ya, xb, yb, largeur, valeur):
        """Trait de largeur ``largeur`` à bouts ronds."""
        r = largeur / 2
        i0, i1 = self._colonnes(min(xa, xb) - r, max(xa, xb) + r)
        j0, j1 = self._lignes(min(ya, yb) - r, max(ya, yb) + r)
        if i0 > i1 or j0 > j1:
            return
        dx, dy = xb - xa, yb - ya
        l2 = dx * dx + dy * dy
        px = self._cx(i0, i1)[None, :]
        py = self._cy(j0, j1)[:, None]
        if l2 <= 0:
            t = np.zeros((j1 - j0 + 1, i1 - i0 + 1))
        else:
            t = np.clip(((px - xa) * dx + (py - ya) * dy) / l2, 0.0, 1.0)
        ex = px - (xa + t * dx)
        ey = py - (ya + t * dy)
        sous = self.v[j0:j1 + 1, i0:i1 + 1]
        sous[ex * ex + ey * ey <= r * r] = valeur

    def anneau(self, x, y, r0, r1, valeur):
        """Anneau de rayons r0..r1."""
        i0, i1 = self._colonnes(x - r1, x + r1)
        j0, j1 = self._lignes(y - r1, y + r1)
        if i0 > i1 or j0 > j1:
            return
        dx = self._cx(i0, i1)[None, :] - x
        dy = self._cy(j0, j1)[:, None] - y
        d = dx * dx + dy * dy
        sous = self.v[j0:j1 + 1, i0:i1 + 1]
        sous[(d >= r0 * r0) & (d <= r1 * r1)] = valeur

    def polygones(self, boucles, valeur):
        """Polygones (règle pair-impair) par balayage des lignes de pixels."""
        if not boucles:
            return
        xa = np.concatenate([np.asarray(b.x, dtype=np.float64) for b in boucles])
        ya = np.concatenate([np.asarray(b.y, dtype=np.float64) for b in boucles])
        xc = np.concatenate([np.roll(np.asarray(b.x, dtype=np.float64), -1) for b in boucles])
        yc = np.concatenate([np.roll(np.asarray(b.y, dtype=np.float64), -1) for b in boucles])
        j0, j1 = self._lignes(float(ya.min()), float(ya.max()))
        for j in range(j0, j1 + 1):
            py = self.hauteur_mm - (j + 0.5) * self.pas
            coupe = (ya <= py) != (yc <= py)
            if not coupe.any():
                continue
            a = np.flatnonzero(coupe)
            xs = np.sort(xa[a] + (py - ya[a]) / (yc[a] - ya[a]) * (xc[a] - xa[a]))
            for m in range(0, len(xs) - 1, 2):
                g, d = float(xs[m]), float(xs[m + 1])
                i0, i1 = self._colonnes(g, d)
                if i0 > i1:
                    continue
                px = self._cx(i0, i1)
                ligne = self.v[j, i0:i1 + 1]
                ligne[(px >= g) & (px < d)] = valeur

    def masquer(self, valeur, boucle, dy=0.0):
        """Valeur ``valeur`` pour les pixels dont le centre (x, y − dy) n'est PAS dans ``boucle`` (Boucle.contient)."""
        bx = np.asarray(boucle.x, dtype=np.float64)
        by = np.asarray(boucle.y, dtype=np.float64)
        jx, jy = np.roll(bx, 1), np.roll(by, 1)          # sommet précédent (j = i − 1, j = n − 1 pour i = 0)
        px = self._cx(0, self.l - 1)
        for j in range(self.h):
            py = (self.hauteur_mm - (j + 0.5) * self.pas) - dy
            coupe = (by > py) != (jy > py)
            if not coupe.any():
                self.v[j, :] = valeur
                continue
            k = np.flatnonzero(coupe)
            xint = np.sort((jx[k] - bx[k]) * (py - by[k]) / (jy[k] - by[k]) + bx[k])
            # Boucle.contient : bascule pour chaque arête dont l'intersection est strictement à droite du point.
            dedans = (len(xint) - np.searchsorted(xint, px, side="right")) % 2 == 1
            self.v[j, ~dedans] = valeur

    def comptes(self, n):
        """Nombre de pixels par valeur 0..n-1."""
        c = np.bincount(self.v.ravel(), minlength=256)
        return [int(x) for x in c[:n]]
