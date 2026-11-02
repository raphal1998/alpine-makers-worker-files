"""Redressement et détourage (port de Redressement.kt et Detourage.kt), vectorisés avec numpy."""
import math

import numpy as np

from . import image as im
from .geometrie import Homographie, resoudre

F32 = np.float32
SEUIL_OMBRE = 115  # 0,45 × 255


def arrondi_entier(v):
    """roundToInt de Kotlin (moitié vers le haut)."""
    return int(math.floor(v + 0.5))


# ------------------------------------------------------------------ redressement

def homographie(quad_px, fmt):
    """mm de la feuille → pixels de la photo."""
    return Homographie.depuis4(fmt.points_reference(), quad_px)


def taille(fmt, ppm):
    return arrondi_entier(fmt.largeur * ppm), arrondi_entier(fmt.hauteur * ppm)


def redresser(photo, mm_vers_photo, fmt, ppm, progress=None):
    """Feuille entière vue de face à ``ppm`` px/mm ; pixel (c, r) centré en ((c + 0,5)/ppm ; (r + 0,5)/ppm) mm."""
    l, h = taille(fmt, ppm)
    sortie = np.empty((h, l, 3), dtype=np.uint8)
    finesse = mm_vers_photo.echelle_locale(fmt.largeur / 2, fmt.hauteur / 2) / ppm
    ss = 2 if finesse > 1.6 else 1
    cols = np.arange(l, dtype=np.float64)
    bande = max(1, 400_000 // max(1, l * ss * ss))
    for r0 in range(0, h, bande):
        r1 = min(h, r0 + bande)
        rows = np.arange(r0, r1, dtype=np.float64)
        if ss == 1:
            X = np.broadcast_to((cols + 0.5) / ppm, (r1 - r0, l)).ravel()
            Y = np.broadcast_to(((rows + 0.5) / ppm)[:, None], (r1 - r0, l)).ravel()
            px, py = mm_vers_photo.appliquer(X, Y)
            sortie[r0:r1] = im.echantillonner(photo, px, py, 255).reshape(r1 - r0, l, 3).astype(np.uint8)
        else:
            acc = np.zeros(((r1 - r0) * l, 3), dtype=np.int32)
            for sy in range(2):
                for sx in range(2):
                    X = np.broadcast_to((cols + 0.25 + 0.5 * sx) / ppm, (r1 - r0, l)).ravel()
                    Y = np.broadcast_to(((rows + 0.25 + 0.5 * sy) / ppm)[:, None], (r1 - r0, l)).ravel()
                    px, py = mm_vers_photo.appliquer(X, Y)
                    acc += im.echantillonner(photo, px, py, 255)
            sortie[r0:r1] = np.clip((acc + 2) // 4, 0, 255).reshape(r1 - r0, l, 3).astype(np.uint8)
        if progress:
            progress(r1 / h)
    return sortie


# ------------------------------------------------------------------ réglages et résultats

class ReglagesDetourage:
    def __init__(self, sensibilite=0.0, ombres=False, taille_min_mm2=25.0, trous_min_mm2=6.0, remplir_trous=False):
        self.sensibilite = float(sensibilite)
        self.ombres = bool(ombres)
        self.taille_min_mm2 = float(taille_min_mm2)
        self.trous_min_mm2 = float(trous_min_mm2)
        self.remplir_trous = bool(remplir_trous)


class Objet:
    """Objet trouvé : boîte (mm, y vers le bas), graine (mm), coupé par le bord de la zone utile, retenu."""
    __slots__ = ("id", "aire_mm2", "boite", "graine", "touche_bord", "retenu")

    def __init__(self, id, aire_mm2, boite, graine, touche_bord, retenu=True):
        self.id, self.aire_mm2, self.boite, self.graine = id, aire_mm2, boite, graine
        self.touche_bord, self.retenu = touche_bord, retenu


class Analyse:
    def __init__(self, fmt, ppm, largeur, hauteur, score, gris, zone, otsu):
        self.format, self.ppm, self.largeur, self.hauteur = fmt, ppm, largeur, hauteur
        self.score, self.gris, self.zone, self.otsu = score, gris, zone, otsu


class Masque:
    def __init__(self, largeur, hauteur, ppm, fmt, etiquettes, objets, seuil):
        self.largeur, self.hauteur, self.ppm, self.format = largeur, hauteur, ppm, fmt
        self.etiquettes, self.objets, self.seuil = etiquettes, objets, seuil

    @property
    def retenus(self):
        return [o for o in self.objets if o.retenu]


# ------------------------------------------------------------------ analyse

def zone_utile(fmt, l, h, ppm):
    z = fmt.zone_utile
    xs = (np.arange(l) + 0.5) / ppm
    ys = (np.arange(h) + 0.5) / ppm
    return ((ys >= z[1]) & (ys <= z[3]))[:, None] & ((xs >= z[0]) & (xs <= z[2]))[None, :]


def _base(u, v):
    return np.stack([np.ones_like(u), u, v, u * u, u * v, v * v], axis=-1)


def _eval(c, u, v):
    return c[0] + c[1] * u + c[2] * v + c[3] * u * u + c[4] * u * v + c[5] * v * v


def analyser(img, fmt, ppm):
    """Éclairage du papier (surface du second degré), balance des blancs, écart au papier 0..255 et seuil d'Otsu."""
    h, l = img.shape[:2]
    zone = zone_utile(fmt, l, h, ppm)
    lum = im.luminance(img)
    bloc = max(6, arrondi_entier(8.0 * ppm))
    bu, bv, bval, coul = [], [], [], []
    q64 = np.clip((lum * F32(63.999)).astype(np.int64), 0, 63)
    for by in range(0, h, bloc):
        ymax = min(h, by + bloc)
        for bx in range(0, l, bloc):
            xmax = min(l, bx + bloc)
            z = zone[by:ymax, bx:xmax]
            compte = int(np.count_nonzero(z))
            if compte < (bloc * bloc) // 2:
                continue
            histo = np.bincount(q64[by:ymax, bx:xmax][z], minlength=64)
            cible = int(compte * 0.1)
            cumul, k = 0, 63
            while k > 0 and cumul + histo[k] <= cible:
                cumul += int(histo[k])
                k -= 1
            p90 = (k + 0.5) / 64.0
            clairs = z & (lum[by:ymax, bx:xmax].astype(np.float64) >= p90 - 2.0 / 64)
            m = int(np.count_nonzero(clairs))
            if m > 0:
                c = img[by:ymax, bx:xmax][clairs].astype(np.float64).sum(axis=0) / m
                bu.append(((bx + xmax) / 2.0 / l - 0.5) * 2)
                bv.append(((by + ymax) / 2.0 / h - 0.5) * 2)
                bval.append(p90)
                coul.append(c)
    nb = len(bval)
    bu, bv, bval = np.array(bu), np.array(bv), np.array(bval)
    garde = np.ones(nb, dtype=bool)
    coef = [0.0] * 6
    mediane = float(np.sort(bval)[nb // 2]) if nb > 0 else 0.9
    coef[0] = mediane
    if nb >= 12:
        F = _base(bu, bv)
        for _ in range(5):
            if np.count_nonzero(garde) >= 12:
                Fg = F[garde]
                # Sommes explicites (pas de BLAS : ses tampons par thread gonflent la mémoire du job).
                A = (Fg[:, :, None] * Fg[:, None, :]).sum(axis=0)
                B = (Fg * bval[garde][:, None]).sum(axis=0)
                sol = resoudre([list(A[i]) + [B[i]] for i in range(6)])
                if sol is not None:
                    coef = sol
            garde = bval >= 0.9 * _eval(coef, bu, bv)
    else:
        garde = bval >= 0.9 * mediane
    gr = gg = gb = F32(1.0)
    if nb and garde.any():
        mr, mg, mb = np.array(coul)[garde].mean(axis=0)
        if mr > 0 and mg > 0 and mb > 0:
            lp = 0.2126 * mr + 0.7152 * mg + 0.0722 * mb
            gr, gg, gb = F32(lp / mr), F32(lp / mg), F32(lp / mb)
    # Écart au papier.
    u = (((np.arange(l) + 0.5) / l - 0.5) * 2)[None, :]
    v = (((np.arange(h) + 0.5) / h - 0.5) * 2)[:, None]
    fond = np.clip(_eval(coef, u, v), 0.05, 1.3).astype(F32)
    rr = img[..., 0].astype(F32) * gr
    vv = img[..., 1].astype(F32) * gg
    bl = img[..., 2].astype(F32) * gb
    sombre = np.clip(F32(1) - lum / fond, F32(0), F32(1))
    chroma = (np.maximum(rr, np.maximum(vv, bl)) - np.minimum(rr, np.minimum(vv, bl))) / F32(255) / np.maximum(fond, F32(0.25))
    s = np.clip(np.maximum(sombre, F32(1.25) * chroma), F32(0), F32(1))
    q = (s * F32(255) + F32(0.5)).astype(np.int32)
    q[~zone] = 0
    score = q.astype(np.uint8)
    gris = (chroma < F32(0.07)) & zone
    histo = np.bincount(q[zone], minlength=256)
    return Analyse(fmt, ppm, l, h, score, gris, zone, im.otsu_histo(histo))


def seuil(a, r):
    base = float(max(a.otsu, 28))
    return min(240, max(14, arrondi_entier(base * (1.0 - 0.5 * max(-1.0, min(1.0, r.sensibilite))))))


def ouvrir(m):
    """Ouverture 3 × 3 (érosion puis dilatation)."""
    h, l = m.shape
    t = np.zeros_like(m)
    t[:, 1:-1] = m[:, 1:-1] & m[:, :-2] & m[:, 2:]
    e = np.zeros_like(m)
    e[1:-1, :] = t[1:-1, :] & t[:-2, :] & t[2:, :]
    t = e.copy()
    t[:, 1:] |= e[:, :-1]
    t[:, :-1] |= e[:, 1:]
    d = t.copy()
    d[1:, :] |= t[:-1, :]
    d[:-1, :] |= t[1:, :]
    return d


def _etiquette_proche(et, ppm, p, rayon_mm):
    h, l = et.shape
    c, r = int(p[0] * ppm), int(p[1] * ppm)
    if 0 <= c < l and 0 <= r < h and et[r, c] != 0:
        return int(et[r, c])
    rp = math.ceil(rayon_mm * ppm)
    y0, y1 = max(0, r - rp), min(h - 1, r + rp)
    x0, x1 = max(0, c - rp), min(l - 1, c + rp)
    if y0 > y1 or x0 > x1:
        return None
    fen = et[y0:y1 + 1, x0:x1 + 1]
    yy, xx = np.nonzero(fen)
    if yy.size == 0:
        return None
    d = np.hypot((xx + x0 - c).astype(float), (yy + y0 - r).astype(float))
    ok = d <= rp
    if not ok.any():
        return None
    k = np.flatnonzero(ok)[np.argmin(d[ok])]
    return int(fen[yy[k], xx[k]])


def objet_a(m, p, rayon_mm=3.0):
    """Objet touché au point p (mm), ou le plus proche à moins de ``rayon_mm``."""
    e = _etiquette_proche(m.etiquettes, m.ppm, p, rayon_mm)
    return next((o for o in m.objets if o.id == e), None) if e is not None else None


def masque(a, r, exclus=(), seuil_impose=None):
    """Masque et objets ; ``seuil_impose`` : seuil de l'aperçu réutilisé ; ``exclus`` : graines (mm) écartées."""
    l, h, ppm = a.largeur, a.hauteur, a.ppm
    t = seuil_impose if seuil_impose is not None else seuil(a, r)
    ombre = max(t, SEUIL_OMBRE)
    s = a.score.astype(np.int32)
    m = a.zone & (s > t)
    if r.ombres:
        m &= ~(a.gris & (s <= ombre))
    m = ouvrir(m)
    trou_min = r.trous_min_mm2 * ppm * ppm
    im.boucher_trous(m, lambda aires: np.full(aires.shape, True) if r.remplir_trous else aires < trou_min)
    et, nb = im.etiqueter(m, True)
    aires = im.aires(et, nb)
    min_px = r.taille_min_mm2 * ppm * ppm
    petits = aires < min_px
    petits[0] = False
    et[petits[et]] = 0
    idx = np.flatnonzero(et)
    lab = et.ravel()[idx]
    xs, ys = idx % l, idx // l
    gardes = [e for e in range(1, nb + 1) if aires[e] >= min_px]
    x0 = np.full(nb + 1, np.iinfo(np.int64).max)
    y0 = x0.copy()
    x1 = np.full(nb + 1, -1, dtype=np.int64)
    y1 = x1.copy()
    if idx.size:
        np.minimum.at(x0, lab, xs)
        np.minimum.at(y0, lab, ys)
        np.maximum.at(x1, lab, xs)
        np.maximum.at(y1, lab, ys)
    sx = np.bincount(lab, weights=xs, minlength=nb + 1)
    sy = np.bincount(lab, weights=ys, minlength=nb + 1)
    # Bord : pixel au bord de l'image ou dont un voisin est hors de la zone utile.
    z = a.zone
    bord_px = np.zeros_like(z)
    bord_px[0, :] = bord_px[-1, :] = True
    bord_px[:, 0] = bord_px[:, -1] = True
    bord_px[:, 1:] |= ~z[:, :-1]
    bord_px[:, :-1] |= ~z[:, 1:]
    bord_px[1:, :] |= ~z[:-1, :]
    bord_px[:-1, :] |= ~z[1:, :]
    bord = np.zeros(nb + 1, dtype=bool)
    bord[lab[bord_px.ravel()[idx]]] = True
    with np.errstate(divide="ignore", invalid="ignore"):
        cx = np.where(aires > 0, sx / np.maximum(aires, 1), 0.0)
        cy = np.where(aires > 0, sy / np.maximum(aires, 1), 0.0)
    graine = np.full(nb + 1, -1, dtype=np.int64)
    if idx.size:
        d = np.hypot(xs - cx[lab], ys - cy[lab])
        ordre = np.lexsort((idx, d, lab))
        premiers = ordre[np.r_[True, lab[ordre][1:] != lab[ordre][:-1]]]
        graine[lab[premiers]] = idx[premiers]
    ordre = sorted(gardes, key=lambda e: (int(cy[e] / ppm / 20.0), cx[e]))
    nouveau = np.zeros(nb + 1, dtype=np.int32)
    for k, e in enumerate(ordre):
        nouveau[e] = k + 1
    et = nouveau[et]
    exclus_ids = set()
    for p in exclus:
        e = _etiquette_proche(et, ppm, p, 1.5)
        if e is not None:
            exclus_ids.add(e)
    objets = []
    for k, e in enumerate(ordre):
        i = k + 1
        g = int(graine[e])
        objets.append(Objet(i, float(aires[e]) / (ppm * ppm),
                            [x0[e] / ppm, y0[e] / ppm, (x1[e] + 1) / ppm, (y1[e] + 1) / ppm],
                            ((g % l + 0.5) / ppm, (g // l + 0.5) / ppm), bool(bord[e]), i not in exclus_ids))
    return Masque(l, h, ppm, a.format, et, objets, t)
