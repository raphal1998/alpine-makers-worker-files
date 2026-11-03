"""Socle commun des importeurs SVG et DXF d'Alpine Chutes (objet ``ImportCommun`` d'ImportSvg.kt).

Portage de C:\\AlpineChutes\\core\\...\\ImportSvg.kt : transformation affine, aplatissement adaptatif des courbes en
millimètres, comptage des points, finition (nettoyage, contrôles de taille, recadrage en (0, 0)).

Les calculs suivent l'ordre des opérations flottantes du Kotlin. Les fonctions ``j*`` reproduisent la sémantique Java là
où Python diffère : Math.max/min (NaN contagieux, ±0), division par zéro (±∞ ou NaN au lieu d'une exception), cos, sin,
tan, acos et sqrt hors domaine (NaN), « % » sur des Double, Double.toLong saturé, String.toDoubleOrNull/toIntOrNull,
Char.isWhitespace / trim et equals(ignoreCase = true). Le texte est traité en unités UTF-16 comme une String Java.
"""
import math
import re
import struct
import sys
import unicodedata
from array import array
from dataclasses import dataclass

import numpy as np

from .geometrie import Boucle, Refus, decimal_fr

MAX_POINTS = 200_000
MAX_COTE_MM = 3000.0
JOINT = 0.05  # écart en mm sous lequel les extrémités d'un tracé ouvert sont jointes
PROFONDEUR_MAX = 32

PI = math.pi
DEG = 0.017453292519943295  # Math.toRadians (JDK 9+) : angdeg × DEGREES_TO_RADIANS
NAN = math.nan
INF = math.inf
LONG_MAX = (1 << 63) - 1
LONG_MIN = -(1 << 63)

TROP_DE_POINTS = "Dessin trop détaillé : plus de 200 000 points. Simplifie les tracés avant l'import."


@dataclass
class Dessin:
    """Dessin importé : boucles fermées en mm, y vers le BAS, boîte englobante ramenée en (0, 0)."""
    boucles: list
    ouvertes: int
    avertissements: list


# ------------------------------------------------------------------ sémantique Java / Kotlin

def jmax(a, b):
    """Math.max : NaN si l'un des deux l'est ; max(-0.0, 0.0) = 0.0."""
    if a > b:
        return a
    if b > a:
        return b
    if a != a:
        return a
    if b != b:
        return b
    if a == 0.0 and math.copysign(1.0, a) < 0.0:
        return b
    return a


def jmin(a, b):
    """Math.min : NaN si l'un des deux l'est ; min(0.0, -0.0) = -0.0."""
    if a < b:
        return a
    if b < a:
        return b
    if a != a:
        return a
    if b != b:
        return b
    if a == 0.0 and math.copysign(1.0, a) > 0.0:
        return b
    return a


def jdiv(a, b):
    """Division IEEE : x / ±0 = ±∞, 0 / 0 = NaN (Python lèverait ZeroDivisionError)."""
    try:
        return a / b
    except ZeroDivisionError:
        if a != a or a == 0.0:
            return NAN
        return INF if (math.copysign(1.0, a) > 0.0) == (math.copysign(1.0, b) > 0.0) else -INF


# ------------------------------------------------------------------ trigonométrie identique à la JVM
#
# Math.sin/cos/tan de la JVM (intrinsèques HotSpot) sont correctement arrondis dans plus de 99,8 % des cas, alors que la
# bibliothèque C de Windows (module math) s'en écarte d'un ulp dans ~3 % des cas : on calcule donc sin/cos/tan
# correctement arrondis (réduction de Cody-Waite en quatre parties, table de pas 1/64, double-double). Math.hypot, atan,
# atan2 et acos sont ceux de fdlibm (StrictMath) : portage exact ci-dessous.

_D = struct.Struct("<d")
_Q = struct.Struct("<Q")


def _bits(x):
    return _Q.unpack(_D.pack(x))[0]


def _de_bits(b):
    return _D.unpack(_Q.pack(b & 0xFFFFFFFFFFFFFFFF))[0]


def _mots(hi, lo):
    return _de_bits((hi << 32) | lo)


def _hi_signe(x):
    h = _bits(x) >> 32
    return h - 0x100000000 if h & 0x80000000 else h


_TWOOPI = 0.6366197723675814  # 2/π
_P1 = _mots(0x3FF921FB, 0x54400000)  # π/2 en quatre morceaux (fdlibm pio2_1, pio2_2, pio2_3, pio2_3t)
_P2 = _mots(0x3DD0B461, 0x1A600000)
_P3 = _mots(0x3BA3198A, 0x2E000000)
_P4 = _mots(0x397B839A, 0x252049C1)
_SPLIT = 134217729.0  # 2^27 + 1 (Veltkamp)
_LIMITE_TRIG = 1e5  # au-delà (fichiers aberrants seulement) : module math
_TABLE = None
_S3, _S5, _S7, _S9 = -1.0 / 6, 1.0 / 120, -1.0 / 5040, 1.0 / 362880
_C2, _C4, _C6, _C8 = -0.5, 1.0 / 24, -1.0 / 720, 1.0 / 40320


def _table():
    """sin et cos de j/64 (j = −51…51) en double-double, calculés une fois en décimal (50 chiffres)."""
    global _TABLE
    if _TABLE is None:
        from decimal import Decimal, localcontext
        sh, sl, ch, cl = [], [], [], []
        with localcontext() as ctx:
            ctx.prec = 50
            seuil = Decimal("1e-48")
            for j in range(-51, 52):
                v = Decimal(j) / 64
                s = Decimal(0)
                t = v
                k = 1
                while abs(t) > seuil:
                    s += t
                    t = -t * v * v / ((k + 1) * (k + 2))
                    k += 2
                c = Decimal(0)
                t = Decimal(1)
                k = 0
                while abs(t) > seuil:
                    c += t
                    t = -t * v * v / ((k + 1) * (k + 2))
                    k += 2
                h = float(s)
                sh.append(h)
                sl.append(float(s - Decimal(h)))
                h = float(c)
                ch.append(h)
                cl.append(float(c - Decimal(h)))
        _TABLE = (sh, sl, ch, cl, np.array(sh), np.array(sl), np.array(ch), np.array(cl))
    return _TABLE


def _sin_cos_dd(x):
    """(sin, cos) de x (fini, |x| < 1e5) en double-double : (sh, sl, ch, cl)."""
    sh_t, sl_t, ch_t, cl_t = _table()[:4]
    k = round(x * _TWOOPI)
    t = x - k * _P1  # exact
    a = -(k * _P2)
    s = t + a
    bb = s - t
    e = (t - (s - bb)) + (a - bb)
    a = -(k * _P3)
    s2 = s + a
    bb = s2 - s
    e2 = (s - (s2 - bb)) + (a - bb)
    lo = (e + e2) - k * _P4
    rh = s2 + lo
    rl = lo - (rh - s2)
    j = round(rh * 64)
    dh = rh - j / 64  # exact
    i = j + 51
    sh, sl, ch, cl = sh_t[i], sl_t[i], ch_t[i], cl_t[i]
    d2 = dh * dh
    sc = dh * (d2 * (_S3 + d2 * (_S5 + d2 * (_S7 + d2 * _S9))))
    cc = d2 * (_C2 + d2 * (_C4 + d2 * (_C6 + d2 * _C8))) - dh * rl
    u = rl + sc
    # produits exacts (Dekker) ch·dh et sh·dh
    c_ = _SPLIT * dh
    dhh = c_ - (c_ - dh)
    dhl = dh - dhh
    c_ = _SPLIT * ch
    chh = c_ - (c_ - ch)
    chl = ch - chh
    c_ = _SPLIT * sh
    shh = c_ - (c_ - sh)
    shl = sh - shh
    p = ch * dh
    pe = ((chh * dhh - p) + chh * dhl + chl * dhh) + chl * dhl
    q = sh * dh
    qe = ((shh * dhh - q) + shh * dhl + shl * dhh) + shl * dhl
    # sin(r) = S + S·cc + C·dh + C·(rl + sc)
    s1 = sh + p
    bb = s1 - sh
    e1 = (sh - (s1 - bb)) + (p - bb)
    w = sh * cc
    s2 = s1 + w
    bb = s2 - s1
    e2 = (s1 - (s2 - bb)) + (w - bb)
    lo = sl + e1 + e2 + pe + cl * dh + ch * u
    sin_h = s2 + lo
    sin_l = lo - (sin_h - s2)
    # cos(r) = C + C·cc − S·dh − S·(rl + sc)
    s1 = ch - q
    bb = s1 - ch
    e1 = (ch - (s1 - bb)) + (-q - bb)
    w = ch * cc
    s2 = s1 + w
    bb = s2 - s1
    e2 = (s1 - (s2 - bb)) + (w - bb)
    lo = cl + e1 + e2 - qe - sl * dh - sh * u
    cos_h = s2 + lo
    cos_l = lo - (cos_h - s2)
    quadrant = k & 3
    if quadrant == 0:
        return sin_h, sin_l, cos_h, cos_l
    if quadrant == 1:
        return cos_h, cos_l, -sin_h, -sin_l
    if quadrant == 2:
        return -sin_h, -sin_l, -cos_h, -cos_l
    return -cos_h, -cos_l, sin_h, sin_l


def jsin(x):
    """Math.sin (correctement arrondi)."""
    if not (-_LIMITE_TRIG < x < _LIMITE_TRIG):
        return math.sin(x) if -INF < x < INF else NAN
    if x == 0.0:
        return x
    return _sin_cos_dd(x)[0]


def jcos(x):
    """Math.cos (correctement arrondi)."""
    if not (-_LIMITE_TRIG < x < _LIMITE_TRIG):
        return math.cos(x) if -INF < x < INF else NAN
    if x == 0.0:
        return 1.0
    return _sin_cos_dd(x)[2]


def jtan(x):
    """Math.tan (correctement arrondi : quotient double-double de sin et cos)."""
    if not (-_LIMITE_TRIG < x < _LIMITE_TRIG):
        return math.tan(x) if -INF < x < INF else NAN
    if x == 0.0:
        return x
    sh, sl, ch, cl = _sin_cos_dd(x)
    q1 = sh / ch
    c_ = _SPLIT * q1
    qh = c_ - (c_ - q1)
    ql = q1 - qh
    c_ = _SPLIT * ch
    chh = c_ - (c_ - ch)
    chl = ch - chh
    p = q1 * ch
    pe = ((qh * chh - p) + qh * chl + ql * chh) + ql * chl
    r = ((sh - p) - pe) + sl - q1 * cl
    return q1 + r / ch


def cos_sin_vec(a):
    """cos et sin (correctement arrondis) d'un tableau d'angles finis : mêmes opérations que _sin_cos_dd."""
    a = np.asarray(a, dtype=np.float64)
    if a.size == 0:
        return a.copy(), a.copy()
    if not (np.abs(a) < _LIMITE_TRIG).all():
        c = np.array([jcos(float(v)) for v in a])
        s = np.array([jsin(float(v)) for v in a])
        return c, s
    t_ = _table()
    sh_t, sl_t, ch_t, cl_t = t_[4], t_[5], t_[6], t_[7]
    k = np.rint(a * _TWOOPI)
    t = a - k * _P1
    m = -(k * _P2)
    s = t + m
    bb = s - t
    e = (t - (s - bb)) + (m - bb)
    m = -(k * _P3)
    s2 = s + m
    bb = s2 - s
    e2 = (s - (s2 - bb)) + (m - bb)
    lo = (e + e2) - k * _P4
    rh = s2 + lo
    rl = lo - (rh - s2)
    j = np.rint(rh * 64)
    dh = rh - j / 64
    i = j.astype(np.int64) + 51
    sh, sl, ch, cl = sh_t[i], sl_t[i], ch_t[i], cl_t[i]
    d2 = dh * dh
    sc = dh * (d2 * (_S3 + d2 * (_S5 + d2 * (_S7 + d2 * _S9))))
    cc = d2 * (_C2 + d2 * (_C4 + d2 * (_C6 + d2 * _C8))) - dh * rl
    u = rl + sc
    c_ = _SPLIT * dh
    dhh = c_ - (c_ - dh)
    dhl = dh - dhh
    c_ = _SPLIT * ch
    chh = c_ - (c_ - ch)
    chl = ch - chh
    c_ = _SPLIT * sh
    shh = c_ - (c_ - sh)
    shl = sh - shh
    p = ch * dh
    pe = ((chh * dhh - p) + chh * dhl + chl * dhh) + chl * dhl
    q = sh * dh
    qe = ((shh * dhh - q) + shh * dhl + shl * dhh) + shl * dhl
    s1 = sh + p
    bb = s1 - sh
    e1 = (sh - (s1 - bb)) + (p - bb)
    w = sh * cc
    s2 = s1 + w
    bb = s2 - s1
    e2 = (s1 - (s2 - bb)) + (w - bb)
    lo = sl + e1 + e2 + pe + cl * dh + ch * u
    sin_h = s2 + lo
    s1 = ch - q
    bb = s1 - ch
    e1 = (ch - (s1 - bb)) + (-q - bb)
    w = ch * cc
    s2 = s1 + w
    bb = s2 - s1
    e2 = (s1 - (s2 - bb)) + (w - bb)
    lo = cl + e1 + e2 - qe - sl * dh - sh * u
    cos_h = s2 + lo
    quadrant = k.astype(np.int64) & 3
    sin_x = np.choose(quadrant, [sin_h, cos_h, -sin_h, -cos_h])
    cos_x = np.choose(quadrant, [cos_h, -sin_h, -cos_h, sin_h])
    zero = a == 0.0
    if zero.any():
        sin_x[zero] = a[zero]
        cos_x[zero] = 1.0
    return cos_x, sin_x


# --- fdlibm (StrictMath) : hypot, atan, atan2, acos

_A500 = float.fromhex("0x1.00000ffffffffp500")
_2M600 = 2.0 ** -600
_2P600 = 2.0 ** 600
_2M500 = 2.0 ** -500
_2P1022 = 2.0 ** 1022
_MIN_NORMAL = 2.2250738585072014e-308


def fd_hypot(x, y):
    """StrictMath.hypot (FdLibm.Hypot, JDK 17)."""
    a = abs(x)
    b = abs(y)
    if not (a < INF and b < INF):
        if a == INF or b == INF:
            return INF
        return a + b
    if b > a:
        a, b = b, a
    ha = _bits(a) >> 32
    hb = _bits(b) >> 32
    if ha - hb > 0x3C00000:
        return a + b  # x / y > 2**60
    k = 0
    if a > _A500:
        ha -= 0x25800000
        hb -= 0x25800000
        a = a * _2M600
        b = b * _2M600
        k += 600
    if b < _2M500:
        if b < _MIN_NORMAL:
            if b == 0.0:
                return a
            t1 = _2P1022
            b *= t1
            a *= t1
            k -= 1022
        else:
            ha += 0x25800000
            hb += 0x25800000
            a = a * _2P600
            b = b * _2P600
            k -= 600
    w = a - b
    if w > b:
        t1 = _de_bits(ha << 32)
        t2 = a - t1
        w = math.sqrt(t1 * t1 - (b * (-b) - t2 * (a + t1)))
    else:
        a = a + a
        y1 = _de_bits(hb << 32)
        y2 = b - y1
        t1 = _de_bits((ha + 0x00100000) << 32)
        t2 = a - t1
        w = math.sqrt(t1 * y1 - (w * (-w) - (t1 * y2 + t2 * b)))
    if k != 0:
        return math.ldexp(1.0, k) * w
    return w


def hypot_seuil(dx, dy, seuil):
    """fd_hypot(dx, dy) s'il peut être ≤ ``seuil`` (sinon None) : math.hypot (correctement arrondi) et fdlibm
    diffèrent d'un ulp au plus, le calcul exact n'est fait que près du seuil."""
    d = math.hypot(dx, dy)
    if d > seuil * 1.0000000000000009 + 5e-324:
        return None
    return fd_hypot(dx, dy)


_ATANHI = (_mots(0x3FDDAC67, 0x0561BB4F), _mots(0x3FE921FB, 0x54442D18), _mots(0x3FEF730B, 0xD281F69B),
           _mots(0x3FF921FB, 0x54442D18))
_ATANLO = (_mots(0x3C7A2B7F, 0x222F65E2), _mots(0x3C81A626, 0x33145C07), _mots(0x3C700788, 0x7AF0CBBD),
           _mots(0x3C91A626, 0x33145C07))
_AT = (_mots(0x3FD55555, 0x5555550D), _mots(0xBFC99999, 0x9998EBC4), _mots(0x3FC24924, 0x920083FF),
       _mots(0xBFBC71C6, 0xFE231671), _mots(0x3FB745CD, 0xC54C206E), _mots(0xBFB3B0F2, 0xAF749A6D),
       _mots(0x3FB10D66, 0xA0D03D51), _mots(0xBFADDE2D, 0x52DEFD9A), _mots(0x3FA97B4B, 0x24760DEB),
       _mots(0xBFA2B444, 0x2C6A6C2F), _mots(0x3F90AD3A, 0xE322DA11))


def fd_atan(x):
    """StrictMath.atan (fdlibm s_atan.c)."""
    b = _bits(x)
    hx = b >> 32
    if hx & 0x80000000:
        hx -= 0x100000000
    ix = hx & 0x7FFFFFFF
    if ix >= 0x44100000:  # |x| >= 2^66
        if ix > 0x7FF00000 or (ix == 0x7FF00000 and (b & 0xFFFFFFFF) != 0):
            return x + x  # NaN
        return _ATANHI[3] + _ATANLO[3] if hx > 0 else -_ATANHI[3] - _ATANLO[3]
    if ix < 0x3FDC0000:  # |x| < 0.4375
        if ix < 0x3E200000:  # |x| < 2^-29
            return x
        ident = -1
    else:
        x = abs(x)
        if ix < 0x3FF30000:  # |x| < 1.1875
            if ix < 0x3FE60000:  # 7/16 <= |x| < 11/16
                ident = 0
                x = (2.0 * x - 1.0) / (2.0 + x)
            else:  # 11/16 <= |x| < 19/16
                ident = 1
                x = (x - 1.0) / (x + 1.0)
        elif ix < 0x40038000:  # |x| < 2.4375
            ident = 2
            x = (x - 1.5) / (1.0 + 1.5 * x)
        else:  # 2.4375 <= |x| < 2^66
            ident = 3
            x = -1.0 / x
    z = x * x
    w = z * z
    at = _AT
    s1 = z * (at[0] + w * (at[2] + w * (at[4] + w * (at[6] + w * (at[8] + w * at[10])))))
    s2 = w * (at[1] + w * (at[3] + w * (at[5] + w * (at[7] + w * at[9]))))
    if ident < 0:
        return x - x * (s1 + s2)
    z = _ATANHI[ident] - ((x * (s1 + s2) - _ATANLO[ident]) - x)
    return -z if hx < 0 else z


_PI_O_4 = _mots(0x3FE921FB, 0x54442D18)
_PI_O_2 = _mots(0x3FF921FB, 0x54442D18)
_PI = _mots(0x400921FB, 0x54442D18)
_PI_LO = _mots(0x3CA1A626, 0x33145C07)
_TINY = 1.0e-300


def fd_atan2(y, x):
    """StrictMath.atan2 (fdlibm e_atan2.c)."""
    bx = _bits(x)
    by = _bits(y)
    hx = bx >> 32
    lx = bx & 0xFFFFFFFF
    hy = by >> 32
    ly = by & 0xFFFFFFFF
    sx = hx >> 31
    sy = hy >> 31
    ix = hx & 0x7FFFFFFF
    iy = hy & 0x7FFFFFFF
    if (ix | (1 if lx else 0)) > 0x7FF00000 or (iy | (1 if ly else 0)) > 0x7FF00000:
        return x + y  # NaN
    if hx == 0x3FF00000 and lx == 0:
        return fd_atan(y)  # x = 1.0
    m = sy | (sx << 1)  # 2·signe(x) + signe(y)
    if (iy | ly) == 0:  # y = 0
        if m < 2:
            return y
        return _PI + _TINY if m == 2 else -_PI - _TINY
    if (ix | lx) == 0:  # x = 0
        return -_PI_O_2 - _TINY if sy else _PI_O_2 + _TINY
    if ix == 0x7FF00000:  # x infini
        if iy == 0x7FF00000:
            return (_PI_O_4 + _TINY, -_PI_O_4 - _TINY, 3.0 * _PI_O_4 + _TINY, -3.0 * _PI_O_4 - _TINY)[m]
        return (0.0, -0.0, _PI + _TINY, -_PI - _TINY)[m]
    if iy == 0x7FF00000:
        return -_PI_O_2 - _TINY if sy else _PI_O_2 + _TINY
    k = (iy - ix) >> 20
    if k > 60:
        z = _PI_O_2 + 0.5 * _PI_LO  # |y/x| > 2**60
    elif sx and k < -60:
        z = 0.0  # |y|/x < -2**60
    else:
        z = fd_atan(abs(y / x))
    if m == 0:
        return z
    if m == 1:
        return -z
    if m == 2:
        return _PI - (z - _PI_LO)
    return (z - _PI_LO) - _PI


_PIO2_HI = _mots(0x3FF921FB, 0x54442D18)
_PIO2_LO = _mots(0x3C91A626, 0x33145C07)
_PS = (_mots(0x3FC55555, 0x55555555), _mots(0xBFD4D612, 0x03EB6F7D), _mots(0x3FC9C155, 0x0E884455),
       _mots(0xBFA48228, 0xB5688F3B), _mots(0x3F49EFE0, 0x7501B288), _mots(0x3F023DE1, 0x0DFDF709))
_QS = (_mots(0xC0033A27, 0x1C8A2D4B), _mots(0x40002AE5, 0x9C598AC8), _mots(0xBFE6066C, 0x1B8D0159),
       _mots(0x3FB3B8C5, 0xB12E9282))


def jacos(x):
    """Math.acos = StrictMath.acos (fdlibm e_acos.c)."""
    b = _bits(x)
    hx = b >> 32
    neg = bool(hx & 0x80000000)
    ix = hx & 0x7FFFFFFF
    if ix >= 0x3FF00000:  # |x| >= 1
        if ((ix - 0x3FF00000) | (b & 0xFFFFFFFF)) == 0:  # |x| == 1
            return 0.0 if not neg else _PI + 2.0 * _PIO2_LO
        return NAN  # |x| > 1 ou NaN
    ps, qs = _PS, _QS
    if ix < 0x3FE00000:  # |x| < 0.5
        if ix <= 0x3C600000:
            return _PIO2_HI + _PIO2_LO
        z = x * x
        p = z * (ps[0] + z * (ps[1] + z * (ps[2] + z * (ps[3] + z * (ps[4] + z * ps[5])))))
        q = 1.0 + z * (qs[0] + z * (qs[1] + z * (qs[2] + z * qs[3])))
        r = p / q
        return _PIO2_HI - (x - (_PIO2_LO - x * r))
    if neg:  # x < -0.5
        z = (1.0 + x) * 0.5
        p = z * (ps[0] + z * (ps[1] + z * (ps[2] + z * (ps[3] + z * (ps[4] + z * ps[5])))))
        q = 1.0 + z * (qs[0] + z * (qs[1] + z * (qs[2] + z * qs[3])))
        s = math.sqrt(z)
        r = p / q
        w = r * s - _PIO2_LO
        return _PI - 2.0 * (s + w)
    z = (1.0 - x) * 0.5  # x > 0.5
    s = math.sqrt(z)
    df = _de_bits(_bits(s) & 0xFFFFFFFF00000000)
    c = (z - df * df) / (s + df)
    p = z * (ps[0] + z * (ps[1] + z * (ps[2] + z * (ps[3] + z * (ps[4] + z * ps[5])))))
    q = 1.0 + z * (qs[0] + z * (qs[1] + z * (qs[2] + z * qs[3])))
    r = p / q
    w = r * s + c
    return 2.0 * (df + w)


def jsqrt(x):
    return math.sqrt(x) if x >= 0.0 else NAN


def jfmod(a, b):
    """« a % b » sur des Double (reste tronqué, NaN pour a infini ou b nul)."""
    if a != a or b != b or b == 0.0 or a == INF or a == -INF:
        return NAN
    return math.fmod(a, b)


def jlong(v):
    """Double.toLong() : troncature, saturée aux bornes de Long, NaN → 0."""
    if v != v:
        return 0
    if v >= 9.223372036854775807e18:
        return LONG_MAX
    if v <= -9.223372036854775808e18:
        return LONG_MIN
    return int(v)


def jfloor_long(v):
    """floor(v).toLong()."""
    if -INF < v < INF:
        f = math.floor(v)
        return LONG_MAX if f > LONG_MAX else (LONG_MIN if f < LONG_MIN else f)
    return jlong(v)


# Char.isWhitespace() de Kotlin (JVM) : Character.isWhitespace || Character.isSpaceChar (Zs, Zl, Zp, Java 17).
BLANCS = ("\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \xa0\u1680" + "".join(chr(c) for c in range(0x2000, 0x200B))
          + "\u2028\u2029\u202f\u205f\u3000")
_BLANCS_SET = frozenset(BLANCS)
CLASSE_BLANCS = BLANCS  # contenu de classe de caractères pour les expressions régulières (aucun caractère spécial)
_RE_ASTRAL = re.compile("[\U00010000-\U0010FFFF]")


def blanc(c):
    return c in _BLANCS_SET


def trim(s):
    """String.trim() de Kotlin (Char.isWhitespace)."""
    return s.strip(BLANCS)


def vide(s):
    """String.isBlank()."""
    return not s.strip(BLANCS)


def utf16(s):
    """Texte en unités UTF-16 (paires de substitution séparées), comme une String Java : mêmes indices et longueurs."""
    if _RE_ASTRAL.search(s) is None:
        return s
    a = array("H")
    a.frombytes(s.encode("utf-16-le", "surrogatepass"))
    if sys.byteorder != "little":
        a.byteswap()
    return "".join(map(chr, a))


def maj_char(c):
    """Char.uppercaseChar() (correspondance simple : un seul caractère)."""
    u = c.upper()
    return u if len(u) == 1 else c


def min_char(c):
    u = c.lower()
    return u if len(u) == 1 else c


def egal_ic(a, b):
    """String.equals(autre, ignoreCase = true) (String.equalsIgnoreCase de Java)."""
    if a == b:
        return True
    if len(a) != len(b):
        return False
    if a.isascii() and b.isascii():
        return a.lower() == b.lower()
    for c1, c2 in zip(a, b):
        if c1 == c2:
            continue
        u1, u2 = maj_char(c1), maj_char(c2)
        if u1 == u2 or min_char(u1) == min_char(u2):
            continue
        return False
    return True


_EXP = r"(?:[eE][+-]?[0-9]+)"
_RE_DBL_SIMPLE = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)" + _EXP + "?")
_RE_DBL_JAVA = re.compile(
    r"[\x00-\x20]*[+-]?(?:NaN|Infinity|(?:(?:[0-9]+\.?[0-9]*" + _EXP + r"?|\.[0-9]+" + _EXP + r"?|"
    r"(?:0[xX][0-9a-fA-F]+\.?|0[xX][0-9a-fA-F]*\.[0-9a-fA-F]+)[pP][+-]?[0-9]+)[fFdD]?))[\x00-\x20]*")
_CTRL = "".join(chr(c) for c in range(0x21))


def to_double(s):
    """String.toDoubleOrNull() (grammaire de Double.parseDouble : suffixes f/d, hexadécimal, NaN, Infinity)."""
    if _RE_DBL_SIMPLE.fullmatch(s):
        return float(s)
    if not _RE_DBL_JAVA.fullmatch(s):
        return None
    t = s.strip(_CTRL)
    neg = t[0] == "-"
    if t[0] in "+-":
        t = t[1:]
    if t == "NaN":
        return NAN
    if t == "Infinity":
        return -INF if neg else INF
    if t[-1] in "fFdD":
        t = t[:-1]
    if t[:2] in ("0x", "0X"):
        try:
            v = float.fromhex(t)
        except OverflowError:
            v = INF
    else:
        v = float(t)
    return -v if neg else v


def _chiffre(c, base):
    """Character.digit(c, base)."""
    o = ord(c)
    if 48 <= o <= 57:
        v = o - 48
    elif 65 <= o <= 90:
        v = o - 55
    elif 97 <= o <= 122:
        v = o - 87
    elif 0xFF21 <= o <= 0xFF3A:
        v = o - 0xFF21 + 10
    elif 0xFF41 <= o <= 0xFF5A:
        v = o - 0xFF41 + 10
    elif o < 128:
        return -1
    else:
        v = unicodedata.decimal(c, -1)
    return v if v < base else -1


def to_int(s, base=10):
    """String.toIntOrNull(base) : signe facultatif puis chiffres (Character.digit), null hors de l'intervalle Int."""
    n = len(s)
    if n == 0:
        return None
    debut = 0
    neg = False
    if s[0] < "0":
        if n == 1:
            return None
        debut = 1
        if s[0] == "-":
            neg = True
        elif s[0] != "+":
            return None
    if base == 10 and s.isascii() and s[debut:].isdigit():
        r = int(s[debut:])
    else:
        r = 0
        for c in s[debut:]:
            d = _chiffre(c, base)
            if d < 0:
                return None
            r = r * base + d
            if r > 2147483648:
                return None
    if neg:
        r = -r
    return r if -2147483648 <= r <= 2147483647 else None


# ------------------------------------------------------------------ transformation affine

class Affine:
    """Transformation affine du plan, notation SVG : x' = a·x + c·y + e, y' = b·x + d·y + f."""
    __slots__ = ("a", "b", "c", "d", "e", "f")

    def __init__(self, a, b, c, d, e, f):
        self.a, self.b, self.c, self.d, self.e, self.f = a, b, c, d, e, f

    def x(self, px, py):
        return self.a * px + self.c * py + self.e

    def y(self, px, py):
        return self.b * px + self.d * py + self.f

    def fois(self, o):
        """Composition : d'abord ``o``, puis self."""
        a, b, c, d = self.a, self.b, self.c, self.d
        return Affine(a * o.a + c * o.b, b * o.a + d * o.b,
                      a * o.c + c * o.d, b * o.c + d * o.d,
                      a * o.e + c * o.f + self.e, b * o.e + d * o.f + self.f)

    def agrandissement(self):
        """Plus grande valeur singulière : facteur d'agrandissement maximal d'une longueur."""
        a, b, c, d = self.a, self.b, self.c, self.d
        p = a * a + b * b + c * c + d * d
        det = a * d - b * c
        q = jsqrt(jmax(0.0, p * p - 4 * det * det))
        return jsqrt((p + q) / 2)

    @staticmethod
    def translation(tx, ty):
        return Affine(1.0, 0.0, 0.0, 1.0, tx, ty)

    @staticmethod
    def echelle(sx, sy):
        return Affine(sx, 0.0, 0.0, sy, 0.0, 0.0)

    @staticmethod
    def rotation(rad):
        co = jcos(rad)
        si = jsin(rad)
        return Affine(co, si, -si, co, 0.0, 0.0)


Affine.IDENTITE = Affine(1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


# ------------------------------------------------------------------ tracés, avertissements, sortie

class TraceBrut:
    """Tracé aplati en mm (avant recadrage) : listes de flottants."""
    __slots__ = ("x", "y", "ferme")

    def __init__(self, x, y, ferme):
        self.x, self.y, self.ferme = x, y, ferme


class Avertissements:
    """Messages pour l'utilisateur, sans doublon, 20 au plus (LinkedHashSet)."""
    __slots__ = ("_liste", "_vus")

    def __init__(self):
        self._liste = []
        self._vus = set()

    def ajouter(self, m):
        if len(self._liste) < 20 and m not in self._vus:
            self._vus.add(m)
            self._liste.append(m)

    def tous(self):
        return list(self._liste)


class SortieImport:
    """Résultat brut d'un importeur : tracés fermés, tracés ouverts et compteur global de points."""
    __slots__ = ("tol", "fermes", "ouverts", "points")

    def __init__(self, tolerance):
        t = float(tolerance)
        self.tol = min(max(t, 1e-3), 5.0) if (math.isfinite(t) and t > 0) else 0.05
        self.fermes = []
        self.ouverts = []
        self.points = 0

    def compter(self, k):
        self.points += k
        if self.points > MAX_POINTS:
            raise Refus(TROP_DE_POINTS)


def _segments(v):
    if not (-INF < v < INF) or v < 1:
        return 1
    return int(min(math.ceil(v), 20000))


class Plume:
    """Reçoit des commandes en coordonnées locales, applique la matrice ``m`` et produit des points en mm.

    Les courbes sont aplaties avec un écart maximal de ``sortie.tol`` mm, mesuré APRÈS transformation.
    """
    __slots__ = ("sortie", "m", "tx", "ty", "cx", "cy", "_dx", "_dy", "actif")

    def __init__(self, sortie):
        self.sortie = sortie
        self.m = Affine.IDENTITE
        self.tx = []
        self.ty = []
        self.cx = 0.0  # point courant (coordonnées locales)
        self.cy = 0.0
        self._dx = 0.0  # départ du sous-tracé en cours (coordonnées locales)
        self._dy = 0.0
        self.actif = False

    def _brut(self, xmm, ymm):
        s = self.sortie
        s.points += 1
        if s.points > MAX_POINTS:
            raise Refus(TROP_DE_POINTS)
        self.tx.append(xmm)
        self.ty.append(ymm)

    def _poser(self, x, y):
        m = self.m
        self._brut(m.a * x + m.c * y + m.e, m.b * x + m.d * y + m.f)

    def deplacer(self, x, y):
        if self.actif:
            self._emettre(False)
        self.cx = x
        self.cy = y
        self._dx = x
        self._dy = y
        self.actif = True
        self._poser(x, y)

    def ligne(self, x, y):
        if not self.actif:
            self.deplacer(self.cx, self.cy)
        self._poser(x, y)
        self.cx = x
        self.cy = y

    def cubique(self, x1, y1, x2, y2, x, y):
        """Bézier cubique : invariante par affinité, on aplatit les points de contrôle transformés."""
        if not self.actif:
            self.deplacer(self.cx, self.cy)
        m = self.m
        p0x = self.tx[-1]
        p0y = self.ty[-1]
        p1x = m.x(x1, y1)
        p1y = m.y(x1, y1)
        p2x = m.x(x2, y2)
        p2y = m.y(x2, y2)
        p3x = m.x(x, y)
        p3y = m.y(x, y)
        # Écart corde / courbe ≤ max|B''| / (8 n²), avec |B''| ≤ 6 · max des différences secondes.
        d2 = jmax(fd_hypot(p0x - 2 * p1x + p2x, p0y - 2 * p1y + p2y), fd_hypot(p1x - 2 * p2x + p3x, p1y - 2 * p2y + p3y))
        n = _segments(jsqrt(0.75 * d2 / self.sortie.tol))
        for i in range(1, n):
            u = i / n
            v = 1 - u
            b0 = v * v * v
            b1 = 3 * v * v * u
            b2 = 3 * v * u * u
            b3 = u * u * u
            self._brut(b0 * p0x + b1 * p1x + b2 * p2x + b3 * p3x, b0 * p0y + b1 * p1y + b2 * p2y + b3 * p3y)
        self._brut(p3x, p3y)
        self.cx = x
        self.cy = y

    def quadratique(self, x1, y1, x, y):
        if not self.actif:
            self.deplacer(self.cx, self.cy)
        m = self.m
        p0x = self.tx[-1]
        p0y = self.ty[-1]
        p1x = m.x(x1, y1)
        p1y = m.y(x1, y1)
        p2x = m.x(x, y)
        p2y = m.y(x, y)
        d2 = fd_hypot(p0x - 2 * p1x + p2x, p0y - 2 * p1y + p2y)
        n = _segments(jsqrt(d2 / (4 * self.sortie.tol)))
        for i in range(1, n):
            u = i / n
            v = 1 - u
            self._brut(v * v * p0x + 2 * v * u * p1x + u * u * p2x, v * v * p0y + 2 * v * u * p1y + u * u * p2y)
        self._brut(p2x, p2y)
        self.cx = x
        self.cy = y

    def arc(self, ccx, ccy, rx, ry, phi, t0, dt, fin_x=NAN, fin_y=NAN):
        """Arc d'ellipse en coordonnées locales (centre, demi-axes, rotation ``phi``, départ ``t0``, balayage ``dt``).

        Le point courant doit être le départ de l'arc ; ``fin_x``/``fin_y`` : arrivée exacte (NaN pour la calculer).
        """
        if not self.actif:
            self.deplacer(self.cx, self.cy)
        tol = self.sortie.tol
        r = jmax(abs(rx), abs(ry)) * self.m.agrandissement()
        pas = 2 * jacos(1 - tol / r) if r > tol else PI / 2
        n = _segments(jdiv(abs(dt), jmin(pas, PI / 4)))
        cp = jcos(phi)
        sp = jsin(phi)
        lx = self.cx
        ly = self.cy
        fin = (-INF < fin_x < INF) and (-INF < fin_y < INF)
        k = n - 1 if fin else n
        if k > 0:
            # Points 1…k vectorisés : mêmes opérations élément par élément que la boucle Kotlin.
            s = self.sortie
            s.points += k
            if s.points > MAX_POINTS:
                raise Refus(TROP_DE_POINTS)
            m = self.m
            with np.errstate(all="ignore"):
                a = t0 + dt * np.arange(1, k + 1, dtype=np.float64) / n
                c, sn = cos_sin_vec(a)
                ex = rx * c
                ey = ry * sn
                lxs = ccx + cp * ex - sp * ey
                lys = ccy + sp * ex + cp * ey
                self.tx.extend((m.a * lxs + m.c * lys + m.e).tolist())
                self.ty.extend((m.b * lxs + m.d * lys + m.f).tolist())
            lx = float(lxs[-1])
            ly = float(lys[-1])
        if fin:
            lx = fin_x
            ly = fin_y
            self._poser(lx, ly)
        self.cx = lx
        self.cy = ly

    def arc_seul(self, ccx, ccy, rx, ry, phi, t0, dt):
        """Arc d'ellipse isolé (cercle, arc DXF…) : se place au départ, trace, ferme si le balayage fait un tour."""
        cp = jcos(phi)
        sp = jsin(phi)
        ex = rx * jcos(t0)
        ey = ry * jsin(t0)
        self.deplacer(ccx + cp * ex - sp * ey, ccy + sp * ex + cp * ey)
        self.arc(ccx, ccy, rx, ry, phi, t0, dt)
        if abs(dt) >= 2 * PI - 1e-9:
            self.fermer()
        else:
            self.terminer()

    def depart_mm(self, xmm, ymm):
        """Points déjà en mm (splines évaluées après transformation)."""
        self.terminer()
        self.actif = True
        self._brut(xmm, ymm)

    def ligne_mm(self, xmm, ymm):
        if self.actif:
            self._brut(xmm, ymm)

    def fermer(self):
        """Ferme le sous-tracé en cours ; le point courant revient au départ du sous-tracé."""
        if self.actif:
            self._emettre(True)
        self.cx = self._dx
        self.cy = self._dy

    def terminer(self):
        """Termine le sous-tracé en cours comme ouvert (sauf si ses extrémités se touchent à JOINT près)."""
        if self.actif:
            self._emettre(False)

    def _emettre(self, ferme):
        self.actif = False
        tx, ty = self.tx, self.ty
        if len(tx) >= 2:
            if ferme:
                boucle = True
            else:
                d = hypot_seuil(tx[-1] - tx[0], ty[-1] - ty[0], JOINT)
                boucle = d is not None and d <= JOINT
            tr = TraceBrut(tx, ty, boucle)
            (self.sortie.fermes if boucle else self.sortie.ouverts).append(tr)
            self.tx = []
            self.ty = []
        else:
            tx.clear()
            ty.clear()


# ------------------------------------------------------------------ lecteur de nombres SVG

_RE_NOMBRE = re.compile(r"[ ,\t\n\r\x0c]*([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)?")
_RE_SEP = re.compile(r"[ ,\t\n\r\x0c]*")


class Lecteur:
    """Lecteur de nombres SVG : « 1-2.5.5e-3 » donne 1, -2.5, 0.5e-3 ; virgules et blancs séparent."""
    __slots__ = ("s", "i")

    def __init__(self, s):
        self.s = s
        self.i = 0

    def espaces(self):
        self.i = _RE_SEP.match(self.s, self.i).end()

    def fin(self):
        self.i = _RE_SEP.match(self.s, self.i).end()
        return self.i >= len(self.s)

    def nombre(self):
        """Nombre suivant, ou None (position après les séparateurs) s'il n'y en a pas de valide et fini."""
        m = _RE_NOMBRE.match(self.s, self.i)
        t = m.group(1)
        if t is None:
            self.i = m.end()
            return None
        v = float(t)
        if not (-INF < v < INF):
            self.i = m.start(1)
            return None
        self.i = m.end()
        return v

    def drapeau(self):
        """Drapeau d'arc : un seul caractère 0 ou 1 (« 011 » = 0, 1, puis 1…)."""
        s = self.s
        i = _RE_SEP.match(s, self.i).end()
        self.i = i
        if i < len(s) and (s[i] == "0" or s[i] == "1"):
            self.i = i + 1
            return s[i] == "1"
        return None


# ------------------------------------------------------------------ finition

def _cote(v):
    return decimal_fr(v, 0) + " mm" if (math.isfinite(v) and v < 1e7) else "plus de 10 km"


def nettoyer(x, y):
    """Retire points non finis, doublons consécutifs et point final répété ; None si < 3 sommets ou sans aire."""
    n = len(x)
    if n >= 48:
        X = np.asarray(x, dtype=np.float64)
        Y = np.asarray(y, dtype=np.float64)
        if not (np.isfinite(X).all() and np.isfinite(Y).all()):
            return None
        proches = (np.abs(X[1:] - X[:-1]) < 1e-9) & (np.abs(Y[1:] - Y[:-1]) < 1e-9)
        if not proches.any():
            k = n
            while k > 1 and abs(x[k - 1] - x[0]) < 1e-9 and abs(y[k - 1] - y[0]) < 1e-9:
                k -= 1
            if k < 3:
                return None
            return _avec_aire(Boucle(X[:k], Y[:k]))
    tx = []
    ty = []
    for i in range(n):
        xi = x[i]
        yi = y[i]
        if not (-INF < xi < INF and -INF < yi < INF):
            return None
        if tx and abs(xi - tx[-1]) < 1e-9 and abs(yi - ty[-1]) < 1e-9:
            continue
        tx.append(xi)
        ty.append(yi)
    while len(tx) > 1 and abs(tx[-1] - tx[0]) < 1e-9 and abs(ty[-1] - ty[0]) < 1e-9:
        tx.pop()
        ty.pop()
    if len(tx) < 3:
        return None
    return _avec_aire(Boucle(tx, ty))


def _avec_aire(b):
    with np.errstate(all="ignore"):  # aire infinie d'un dessin aberrant : rejetée sans avertissement numpy
        a = b.aire
    return b if (math.isfinite(a) and abs(a) > 1e-9) else None


def _min_java(v):
    m = float(v.min())
    if m == 0.0:
        m = -0.0 if np.signbit(v[v == 0.0]).any() else 0.0
    return m


def _max_java(v):
    m = float(v.max())
    if m == 0.0:
        m = 0.0 if (~np.signbit(v[v == 0.0])).any() else -0.0
    return m


def finir(fermes, ouvertes, avert, y_vers_le_haut):
    """Nettoie les tracés fermés, contrôle la taille et recadre la boîte en (0, 0).

    ``y_vers_le_haut`` : le repère source a y vers le haut (DXF) ; on retourne en y vers le bas (y' = maxY − y).
    """
    propres = []
    degenerees = 0
    for tr in fermes:
        b = nettoyer(tr.x, tr.y)
        if b is None:
            degenerees += 1
        else:
            propres.append(b)
    if degenerees > 0:
        avert.ajouter(f"{degenerees} forme(s) fermée(s) sans surface ignorée(s).")
    if not propres:
        if ouvertes > 0:
            raise Refus(f"Aucune forme fermée trouvée : le fichier ne contient que {ouvertes} tracé(s) ouvert(s). "
                        "Ferme les contours dans ton logiciel de dessin.")
        raise Refus("Aucune forme fermée trouvée dans le fichier.")
    xs = np.concatenate([b.x for b in propres])
    ys = np.concatenate([b.y for b in propres])
    x0, x1 = _min_java(xs), _max_java(xs)
    y0, y1 = _min_java(ys), _max_java(ys)
    larg = x1 - x0
    haut = y1 - y0
    if not (larg <= MAX_COTE_MM and haut <= MAX_COTE_MM):
        raise Refus(f"Dessin démesuré : {_cote(larg)} × {_cote(haut)} (3000 mm au plus par côté). "
                    "Vérifie les unités du fichier.")
    if y_vers_le_haut:
        boucles = [Boucle(b.x - x0, y1 - b.y) for b in propres]
    else:
        boucles = [Boucle(b.x - x0, b.y - y0) for b in propres]
    return Dessin(boucles, ouvertes, avert.tous())
