# -*- coding: utf-8 -*-
"""Arithmétique d'Alpine Gift (Kotlin sur la JVM / Android) reproduite à l'identique en Python.

Le découpage du puzzle doit donner exactement les mêmes pièces que l'application : mêmes tirages aléatoires
(``kotlin.random.Random(graine)``, générateur XorWow), même ``hypot`` (fdlibm, celui de ``java.lang.Math`` et de la
libm d'Android), mêmes arrondis (``roundToInt`` : demi vers le haut) et mêmes nombres écrits (``String.format`` de
Java arrondit au demi supérieur sur les chiffres décimaux les plus courts du double).
"""
import math
import struct
from decimal import ROUND_HALF_UP, Decimal

import numpy as np

_MASQUE32 = 0xFFFFFFFF


def _i32(v):
    """Entier signé 32 bits (débordement comme un Int de Kotlin)."""
    v &= _MASQUE32
    return v - (1 << 32) if v & 0x80000000 else v


class AleaKotlin:
    """``kotlin.random.Random(graine: Int)`` : XorWowRandom(graine, graine shr 31), 64 tirages écartés au départ."""

    def __init__(self, graine):
        graine = _i32(int(graine))
        seed2 = graine >> 31
        self.x, self.y, self.z, self.w = graine, seed2, 0, 0
        self.v = _i32(~graine)
        self.addend = _i32((graine << 10) ^ ((seed2 & _MASQUE32) >> 4))
        if (self.x | self.y | self.z | self.w | self.v) == 0:
            raise ValueError("Graine invalide.")
        for _ in range(64):
            self.next_int()

    def next_int(self):
        t = self.x
        t = t ^ ((t & _MASQUE32) >> 2)
        self.x, self.y, self.z = self.y, self.z, self.w
        v0 = self.v
        self.w = v0
        t = _i32((t ^ (t << 1)) ^ v0 ^ (v0 << 4))
        self.v = t
        self.addend = _i32(self.addend + 362437)
        return _i32(t + self.addend)

    def next_bits(self, n):
        # takeUpperBits : (this ushr (32 − n)) and (−n shr 31)
        return ((self.next_int() & _MASQUE32) >> (32 - n)) if n > 0 else 0

    def next_double(self):
        hi, lo = self.next_bits(26), self.next_bits(27)
        return float((hi << 27) + lo) / float(1 << 53)

    def next_boolean(self):
        return self.next_bits(1) != 0


# --- hypot de fdlibm (e_hypot.c), celui de java.lang.StrictMath / Math.hypot ------------------------------------------
def _bits(x):
    return struct.unpack("<Q", struct.pack("<d", x))[0]


def _double(bits):
    return struct.unpack("<d", struct.pack("<Q", bits & 0xFFFFFFFFFFFFFFFF))[0]


def _avec_haut(x, haut):
    return _double(((haut & _MASQUE32) << 32) | (_bits(x) & _MASQUE32))


def hypot(x, y):
    """√(x² + y²) bit à bit comme fdlibm (erreur < 1 ulp, pas toujours l'arrondi correct de math.hypot)."""
    ha = (_bits(x) >> 32) & 0x7FFFFFFF
    hb = (_bits(y) >> 32) & 0x7FFFFFFF
    if hb > ha:
        a, b = y, x
        ha, hb = hb, ha
    else:
        a, b = x, y
    a = _avec_haut(a, ha)
    b = _avec_haut(b, hb)
    if ha - hb > 0x3C00000:
        return a + b
    k = 0
    if ha > 0x5F300000:
        if ha >= 0x7FF00000:
            w = a + b
            if ((ha & 0xFFFFF) | (_bits(a) & _MASQUE32)) == 0:
                w = a
            if ((hb ^ 0x7FF00000) | (_bits(b) & _MASQUE32)) == 0:
                w = b
            return w
        ha -= 0x25800000
        hb -= 0x25800000
        k += 600
        a = _avec_haut(a, ha)
        b = _avec_haut(b, hb)
    if hb < 0x20B00000:
        if hb <= 0x000FFFFF:
            if (hb | (_bits(b) & _MASQUE32)) == 0:
                return a
            t1 = _double(0x7FD00000 << 32)
            b *= t1
            a *= t1
            k -= 1022  # comme FdLibm.Hypot de Java : mots hauts non relus (cas sans objet ici, b sous-normal)
        else:
            ha += 0x25800000
            hb += 0x25800000
            k -= 600
            a = _avec_haut(a, ha)
            b = _avec_haut(b, hb)
    w = a - b
    if w > b:
        t1 = _double(ha << 32)
        t2 = a - t1
        w = math.sqrt(t1 * t1 - (b * (-b) - t2 * (a + t1)))
    else:
        a = a + a
        y1 = _double(hb << 32)
        y2 = b - y1
        t1 = _double((ha + 0x00100000) << 32)
        t2 = a - t1
        w = math.sqrt(t1 * y1 - (w * (-w) - (t1 * y2 + t2 * b)))
    if k != 0:
        t1 = _double(((0x3FF00000 + (k << 20)) & _MASQUE32) << 32)
        return t1 * w
    return w


_U32 = np.uint64(32)
_BAS = np.uint64(0xFFFFFFFF)


def hypot_vec(x, y):
    """``hypot`` de fdlibm sur des tableaux numpy (même résultat bit à bit que :func:`hypot`)."""
    x, y = np.broadcast_arrays(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))
    forme = x.shape
    x = np.ascontiguousarray(x).ravel()
    y = np.ascontiguousarray(y).ravel()
    bx, by = x.view(np.uint64), y.view(np.uint64)
    hx = ((bx >> _U32) & np.uint64(0x7FFFFFFF)).astype(np.int64)
    hy = ((by >> _U32) & np.uint64(0x7FFFFFFF)).astype(np.int64)
    echange = hy > hx
    ha = np.where(echange, hy, hx)
    hb = np.where(echange, hx, hy)
    lo_a = np.where(echange, by, bx) & _BAS
    lo_b = np.where(echange, bx, by) & _BAS
    a = ((ha.astype(np.uint64) << _U32) | lo_a).view(np.float64)
    b = ((hb.astype(np.uint64) << _U32) | lo_b).view(np.float64)
    loin = (ha - hb) > 0x3C00000
    special = ~loin & ((ha > 0x5F300000) | (hb < 0x20B00000))
    with np.errstate(invalid="ignore", over="ignore"):
        w = a - b
        t1 = (ha.astype(np.uint64) << _U32).view(np.float64)
        t2 = a - t1
        r1 = np.sqrt(t1 * t1 - (b * (-b) - t2 * (a + t1)))
        a2 = a + a
        y1 = (hb.astype(np.uint64) << _U32).view(np.float64)
        y2 = b - y1
        t1b = ((ha + 0x00100000).astype(np.uint64) << _U32).view(np.float64)
        t2b = a2 - t1b
        r2 = np.sqrt(t1b * y1 - (w * (-w) - (t1b * y2 + t2b * b)))
    r = np.where(w > b, r1, r2)
    r = np.where(loin, a + b, r)
    if special.any():
        for i in np.nonzero(special)[0].tolist():
            r[i] = hypot(float(x[i]), float(y[i]))
    return r.reshape(forme)


def distance_segment(px, py, ax, ay, bx, by):
    """Contours.distanceSegment (Kotlin), scalaire."""
    dx = bx - ax
    dy = by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0.0:
        return hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / l2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return hypot(px - (ax + t * dx), py - (ay + t * dy))


def distance_segment_vec(px, py, ax, ay, bx, by):
    """distanceSegment sur des tableaux (diffusion numpy), mêmes opérations que la version scalaire."""
    dx = bx - ax
    dy = by - ay
    l2 = dx * dx + dy * dy
    with np.errstate(invalid="ignore", divide="ignore"):
        t = ((px - ax) * dx + (py - ay) * dy) / l2
    t = np.where(l2 == 0.0, 0.0, np.clip(t, 0.0, 1.0))  # segment nul : t·d = 0, donc hypot(px − ax, py − ay)
    return hypot_vec(px - (ax + t * dx), py - (ay + t * dy))


# --- Arrondis et nombres écrits ---------------------------------------------------------------------------------------
def arrondi(x):
    """``Double.roundToInt()`` / ``Math.round`` : entier le plus proche, demi vers +∞ (calcul exact, sans x + 0,5)."""
    r = math.floor(x)
    return int(r) + (1 if x - r >= 0.5 else 0)


def _decimales(v, n):
    """``String.format("%.nf")`` de Java : demi vers le haut appliqué aux chiffres décimaux les plus courts du double."""
    t = f"{v:.{n}f}"
    r = repr(float(v))
    if "e" in r or "E" in r or "inf" in r or "nan" in r:
        return t
    entier, _, frac = r.partition(".")
    if len(frac) == n + 1 and frac.endswith("5"):  # égalité exacte sur les chiffres courts : Java arrondit vers le haut
        return str(Decimal(r).quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP))
    return t


def nombre_svg(v):
    """Puzzle.n : ``String.format(Locale.US, "%.3f", v).trimEnd('0').trimEnd('.')``."""
    return _decimales(v, 3).rstrip("0").rstrip(".")


def fr(v, decimales=1):
    """``String.format(Locale.FRANCE, "%.nf", v)`` (virgule décimale)."""
    return _decimales(v, decimales).replace(".", ",")


def mm(v):
    """mm() de l'application : « 12,5 mm », « 200 mm »."""
    return (fr(v, 1) + " mm").replace(",0 mm", " mm")


def mm2(v):
    """mm2() de l'application : deux décimales."""
    return fr(v, 2) + " mm"


def entier(v):
    """Formats.entier : milliers séparés par une espace."""
    return f"{int(v):,}".replace(",", " ")
