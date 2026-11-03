"""Détection de la feuille de référence (port de Detection.kt) : feuille blanche ou feuille de repères.

Points rendus en pixels, dans l'ordre haut-gauche, haut-droite, bas-droite, bas-gauche de la feuille en portrait.
"""
import math

import numpy as np

from . import image as im
from .geometrie import aire_points, enveloppe_convexe

F32 = np.float32


def convexe(points):
    signe = 0
    for k in range(4):
        a, b, c = points[k], points[(k + 1) % 4], points[(k + 2) % 4]
        z = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        s = 1 if z > 0 else (-1 if z < 0 else 0)
        if s == 0:
            return False
        if signe == 0:
            signe = s
        elif s != signe:
            return False
    return True


def ordonner(pts):
    """Sens horaire à l'écran, premier côté = côté court ; départ le plus proche du coin haut-gauche de la photo."""
    cx = sum(p[0] for p in pts) / 4
    cy = sum(p[1] for p in pts) / 4
    p = sorted(pts, key=lambda q: math.atan2(q[1] - cy, q[0] - cx))
    cotes = [math.hypot(p[i][0] - p[(i + 1) % 4][0], p[i][1] - p[(i + 1) % 4][1]) for i in range(4)]
    debut = 1 if cotes[0] + cotes[2] > cotes[1] + cotes[3] else 0
    a = [p[(debut + i) % 4] for i in range(4)]
    b = [p[(debut + 2 + i) % 4] for i in range(4)]
    return a if a[0][0] + a[0][1] <= b[0][0] + b[0][1] else b


def reduire_a_quatre(env):
    p = list(env)

    def perte(i):
        a, b, c = p[(i - 1) % len(p)], p[i], p[(i + 1) % len(p)]
        return abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) / 2

    while len(p) > 4:
        meilleur, vmin = 0, math.inf
        for i in range(len(p)):
            v = perte(i)
            if v < vmin:
                vmin, meilleur = v, i
        del p[meilleur]
    return p


def detecter(rgb, fmt):
    return reperes(rgb) if fmt.reperes else feuille(rgb)


# ------------------------------------------------------------------ feuille blanche

def feuille(rgb):
    h, l = rgb.shape[:2]
    lum = im.luminance(rgb)
    t = im.otsu_valeurs(lum)
    et, n = im.etiqueter(lum > t, False)
    if n == 0:
        return None
    a = im.aires(et, n)
    grande = int(np.argmax(a[1:])) + 1
    f = et == grande
    im.boucher_trous(f, lambda s: np.ones(s.shape, dtype=bool))
    aire = int(np.count_nonzero(f))
    if aire < 0.04 * l * h:
        return None
    # Enveloppe convexe : les extrémités de chaque ligne suffisent (même enveloppe que tous les pixels du bord).
    lignes = np.nonzero(f.any(axis=1))[0]
    gauche = np.argmax(f[lignes], axis=1)
    droite = l - 1 - np.argmax(f[lignes, ::-1], axis=1)
    pts = [(float(x) + 0.5, float(y) + 0.5) for x, y in zip(gauche.tolist(), lignes.tolist())]
    pts += [(float(x) + 0.5, float(y) + 0.5) for x, y in zip(droite.tolist(), lignes.tolist())]
    env = enveloppe_convexe(pts)
    if len(env) < 4:
        return None
    quad = reduire_a_quatre(env)
    aq = abs(aire_points(quad))
    if aq < 0.75 * aire or aq > 1.3 * aire:
        return None
    for _ in range(2):
        quad = _affiner(lum, quad) or quad
    q = ordonner(quad)
    return q if convexe(q) else None


def _ajuster(px, py):
    if px.size < 2:
        return None
    cx, cy = float(px.mean()), float(py.mean())
    dx, dy = px - cx, py - cy
    sxx, sxy, syy = float((dx * dx).sum()), float((dx * dy).sum()), float((dy * dy).sum())
    angle = 0.5 * math.atan2(2 * sxy, sxx - syy)
    return (cx, cy), (math.cos(angle), math.sin(angle))


def _intersection(a, b):
    (ap, au), (bp, bu) = a, b
    det = au[0] * bu[1] - au[1] * bu[0]
    if abs(det) < 1e-9:
        return None
    dx, dy = bp[0] - ap[0], bp[1] - ap[1]
    t = (dx * bu[1] - dy * bu[0]) / det
    return (ap[0] + t * au[0], ap[1] + t * au[1])


def _affiner(lum, quad):
    """Recale les 4 côtés au sous-pixel (passage à mi-hauteur du profil de luminance sur la normale)."""
    h, l = lum.shape
    cx = sum(p[0] for p in quad) / 4
    cy = sum(p[1] for p in quad) / 4
    r = max(6.0, 0.012 * max(l, h))
    pas = 0.25
    nb_pas = int(2 * r / pas)
    quart = nb_pas // 4
    t = -r + np.arange(nb_pas + 1) * pas
    e = np.array([-1.0, 0.0, 1.0])
    f = 0.1 + 0.8 * np.arange(91) / 90
    droites = []
    for k in range(4):
        a, b = quad[k], quad[(k + 1) % 4]
        lg = math.hypot(b[0] - a[0], b[1] - a[1])
        if lg < 8:
            return None
        u = ((b[0] - a[0]) / lg, (b[1] - a[1]) / lg)
        nrm = (u[1], -u[0])
        milieu = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        if (milieu[0] - cx) * nrm[0] + (milieu[1] - cy) * nrm[1] < 0:
            nrm = (-nrm[0], -nrm[1])
        px = a[0] + (b[0] - a[0]) * f
        py = a[1] + (b[1] - a[1]) * f
        X = px[:, None, None] + nrm[0] * t[None, :, None] + u[0] * e[None, None, :]
        Y = py[:, None, None] + nrm[1] * t[None, :, None] + u[1] * e[None, None, :]
        v = im.bilineaire(lum, X, Y)
        profil = (v[..., 0] + v[..., 1] + v[..., 2]) / F32(3)
        dedans = profil[:, :quart + 1].sum(axis=1, dtype=F32) / F32(quart + 1)
        dehors = profil[:, nb_pas - quart:].sum(axis=1, dtype=F32) / F32(quart + 1)
        valide = (dedans - dehors) >= F32(0.06)
        niveau = (dedans + dehors) / F32(2)
        p0, p1 = profil[:, :-1], profil[:, 1:]
        croise = (p0 >= niveau[:, None]) & (p1 < niveau[:, None])
        with np.errstate(divide="ignore", invalid="ignore"):
            frac = (p0 - niveau[:, None]) / (p0 - p1)
        tq = -r + pas * (np.arange(nb_pas)[None, :] + frac.astype(np.float64))
        tq = np.where(croise, tq, np.inf)
        meilleur = np.argmin(np.abs(tq), axis=1)
        tm = tq[np.arange(tq.shape[0]), meilleur]
        ok = valide & np.isfinite(tm)
        qx = px[ok] + nrm[0] * tm[ok]
        qy = py[ok] + nrm[1] * tm[ok]
        d = _ajuster(qx, qy) if qx.size >= 12 else None
        if d is not None:
            (dpx, dpy), (dux, duy) = d
            proches = np.abs((qx - dpx) * duy - (qy - dpy) * dux) <= 1.5
            if np.count_nonzero(proches) >= 10:
                d = _ajuster(qx[proches], qy[proches])
        droites.append(d if d is not None else (a, u))
    coins = []
    for k in range(4):
        c = _intersection(droites[(k + 3) % 4], droites[k])
        if c is None:
            return None
        if math.hypot(c[0] - quad[k][0], c[1] - quad[k][1]) > 0.04 * max(l, h):
            return None
        coins.append(c)
    return coins


# ------------------------------------------------------------------ feuille de repères

def reperes(rgb):
    h, l = rgb.shape[:2]
    lum = im.luminance(rgb)
    integ = np.zeros((h + 1, l + 1), dtype=np.float64)
    integ[1:, 1:] = np.cumsum(np.cumsum(lum.astype(np.float64), axis=0), axis=1)
    r = max(8, max(l, h) // 14)
    ys, xs = np.arange(h), np.arange(l)
    y0, y1 = np.maximum(0, ys - r), np.minimum(h, ys + r + 1)
    x0, x1 = np.maximum(0, xs - r), np.minimum(l, xs + r + 1)
    somme = integ[np.ix_(y1, x1)] - integ[np.ix_(y0, x1)] - integ[np.ix_(y1, x0)] + integ[np.ix_(y0, x0)]
    moyenne = somme / ((y1 - y0)[:, None] * (x1 - x0)[None, :])
    del somme, integ
    sombre = (lum < 0.7 * moyenne) & (lum < 0.55)
    del moyenne
    et, n = im.etiqueter(sombre, True)
    if n == 0:
        return None
    idx = np.flatnonzero(et)
    lab = et.ravel()[idx]
    px, py = idx % l, idx // l
    aire = np.bincount(lab, minlength=n + 1).astype(np.int64)
    sx = np.bincount(lab, weights=px + 0.5, minlength=n + 1)
    sy = np.bincount(lab, weights=py + 0.5, minlength=n + 1)
    bx0 = np.full(n + 1, np.iinfo(np.int64).max)
    by0 = bx0.copy()
    bx1 = np.full(n + 1, -1, dtype=np.int64)
    by1 = bx1.copy()
    np.minimum.at(bx0, lab, px)
    np.minimum.at(by0, lab, py)
    np.maximum.at(bx1, lab, px)
    np.maximum.at(by1, lab, py)
    utiles = np.nonzero(aire >= 4)[0]
    utiles = utiles[utiles > 0]
    bw = bx1 - bx0 + 1
    bh = by1 - by0 + 1
    trouves = []
    with np.errstate(divide="ignore", invalid="ignore"):
        remplissage = aire / (bw * bh)
    for e in utiles.tolist():
        if not (bw[e] >= 6 and bh[e] >= 6 and 0.25 <= bw[e] / bh[e] <= 4.0 and 0.18 <= remplissage[e] <= 0.8
                and bw[e] < l // 3 and bh[e] < h // 3):
            continue
        ce = (sx[e] / aire[e], sy[e] / aire[e])
        i = utiles
        ok = (i != e) & (bx0[i] > bx0[e]) & (bx1[i] < bx1[e]) & (by0[i] > by0[e]) & (by1[i] < by1[e])
        i = i[ok]
        if i.size == 0:
            continue
        rapport = aire[i] / aire[e]
        i = i[(rapport >= 0.15) & (rapport <= 0.9) & (remplissage[i] >= 0.55)]
        if i.size == 0:
            continue
        d = np.hypot(sx[i] / aire[i] - ce[0], sy[i] / aire[i] - ce[1])
        proche = d < 0.15 * max(bw[e], bh[e])
        if not proche.any():
            continue
        m = int(i[proche][np.argmin(d[proche])])
        a = aire[e] + aire[m]
        trouves.append(((float((sx[e] + sx[m]) / a), float((sy[e] + sy[m]) / a)), math.sqrt(a)))
    if len(trouves) < 4:
        return None
    candidats = sorted(trouves, key=lambda c: -c[1])[:12]
    choix, meilleure = None, 0.0
    k = len(candidats)
    for a in range(k):
        for b in range(a + 1, k):
            for c in range(b + 1, k):
                for d in range(c + 1, k):
                    q = (candidats[a], candidats[b], candidats[c], candidats[d])
                    tailles = [x[1] for x in q]
                    if max(tailles) / min(tailles) > 2.2:
                        continue
                    pts = ordonner([x[0] for x in q])
                    if not convexe(pts):
                        continue
                    s = abs(aire_points(pts))
                    if s > meilleure:
                        meilleure, choix = s, q
    if choix is None or meilleure < 0.03 * l * h:
        return None
    return ordonner([x[0] for x in choix])
