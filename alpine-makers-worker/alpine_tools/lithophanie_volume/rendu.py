"""Aperçus de la lithophanie en volume, portés d'Alpine Gift (ui RenduVolume.kt, LithoVolumeUi.kt).

- Vue 3D : triangles d'un maillage d'aperçu dessinés avec un tampon de profondeur, éclairés comme une lampe allumée
  (lumière chaude, faces tournées vers l'œil plus claires), vus de l'azimut donné, 20° au-dessus de l'horizontale.
  Même calcul pixel par pixel que l'application, vectorisé par lignes de balayage (numpy).
- Paroi déroulée rétroéclairée (PlanVolume.apercu) sous la vue, sur le fond de l'écran de l'application.
"""
import math

import numpy as np
from PIL import Image

F32 = np.float32
NUIT = (0x0D, 0x07, 0x10)
SURFACE_HAUTE = (0x2B, 0x1A, 0x30)
BRUN = (0x3A, 0x2A, 0x1A)
_LOT = 200_000          # triangles traités ensemble (mémoire bornée)
_TOL = 1e-9


def dessiner(tris, l, h, azimut_deg=30.0):
    """RenduVolume.dessiner : image RGBA (h, l, 4) uint8, fond transparent. tris : (n, 3, 3) sommets en mm."""
    px = np.zeros((h, l, 4), dtype=np.uint8)
    n = len(tris)
    if n == 0:
        return px
    az, el = math.radians(azimut_deg), math.radians(20.0)
    rx = (math.cos(az), math.sin(az))
    oeil = (math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el))
    up = (-math.sin(az) * math.sin(el), math.cos(az) * math.sin(el), math.cos(el))
    t = tris.reshape(-1, 3).astype(np.float64)
    x, y, z = t[:, 0], t[:, 1], t[:, 2]
    sx = x * rx[0] + y * rx[1]
    sy = x * up[0] + y * up[1] + z * up[2]
    sz = x * oeil[0] + y * oeil[1] + z * oeil[2]
    x0, x1, y0, y1 = sx.min(), sx.max(), sy.min(), sy.max()
    e = min((l - 16) / max(x1 - x0, 1e-12), (h - 16) / max(y1 - y0, 1e-12))
    ox, oy = l / 2.0 - (x0 + x1) / 2 * e, h / 2.0 + (y0 + y1) / 2 * e
    X = (ox + sx * e).reshape(-1, 3)
    Y = (oy - sy * e).reshape(-1, 3)
    S = np.stack([sx, sy, sz], -1).reshape(-1, 3, 3)
    Z = sz.reshape(-1, 3)
    zb = np.full(h * l, -np.inf, dtype=F32)
    couleur = np.zeros((h * l, 3), dtype=np.uint8)
    for d in range(0, n, _LOT):
        _lot(X[d:d + _LOT], Y[d:d + _LOT], Z[d:d + _LOT], S[d:d + _LOT], l, h, zb, couleur)
    plein = np.isfinite(zb)
    px.reshape(-1, 4)[plein, :3] = couleur[plein]
    px.reshape(-1, 4)[plein, 3] = 255
    return px


def _lot(X, Y, Z, S, l, h, zb, couleur):
    ax, ay, bx, by, cx, cy = X[:, 0], Y[:, 0], X[:, 1], Y[:, 1], X[:, 2], Y[:, 2]
    aire = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    min_x = np.maximum(0, np.floor(np.minimum(ax, np.minimum(bx, cx)))).astype(np.int64)
    max_x = np.minimum(l - 1, np.ceil(np.maximum(ax, np.maximum(bx, cx)))).astype(np.int64)
    min_y = np.maximum(0, np.floor(np.minimum(ay, np.minimum(by, cy)))).astype(np.int64)
    max_y = np.minimum(h - 1, np.ceil(np.maximum(ay, np.maximum(by, cy)))).astype(np.int64)
    garde = (aire < 0) & (np.abs(d) >= 1e-12) & (min_x <= max_x) & (min_y <= max_y)   # faces avant seulement
    q = np.nonzero(garde)[0]
    if q.size == 0:
        return
    # Lumière : composante de la normale (repère de la vue) vers l'œil.
    u = S[q, 1] - S[q, 0]
    v = S[q, 2] - S[q, 0]
    nrm = np.cross(u, v)
    nl = np.maximum(np.sqrt((nrm * nrm).sum(1)), 1e-12)
    f = np.clip(0.35 + 0.65 * np.abs(nrm[:, 2] / nl) + 0.12 * (nrm[:, 1] / nl), 0.0, 1.0)
    rgb = np.stack([np.clip((70 + 185 * f).astype(np.int64), 0, 255), np.clip((45 + 190 * f).astype(np.int64), 0, 255),
                    np.clip((25 + 150 * f * f).astype(np.int64), 0, 255)], -1).astype(np.uint8)
    ax, ay, bx, by, cx, cy, d = ax[q], ay[q], bx[q], by[q], cx[q], cy[q], d[q]
    min_x, max_x, min_y, max_y = min_x[q], max_x[q], min_y[q], max_y[q]
    za, zb_, zc = Z[q, 0], Z[q, 1], Z[q, 2]
    # Paires (triangle, ligne) puis intervalle de colonnes où les trois coordonnées barycentriques sont ≥ −1e−9.
    lignes = max_y - min_y + 1
    tq = np.repeat(np.arange(q.size), lignes)
    yy = min_y[tq] + (np.arange(tq.size) - np.repeat(np.cumsum(lignes) - lignes, lignes))
    pyc = yy + 0.5
    lo = np.full(tq.size, -np.inf)
    hi = np.full(tq.size, np.inf)
    # wa = (A1·(x − cx) + B1·(y − cy)) / d ; wb = (A2·(x − cx) + B2·(y − cy)) / d ; wc = 1 − wa − wb.
    a1, b1 = (by - cy)[tq] / d[tq], (cx - bx)[tq] / d[tq]
    a2, b2 = (cy - ay)[tq] / d[tq], (ax - cx)[tq] / d[tq]
    ccx, ccy = cx[tq], cy[tq]
    for pente, origine in ((a1, b1 * (pyc - ccy) - a1 * ccx), (a2, b2 * (pyc - ccy) - a2 * ccx),
                           (-(a1 + a2), 1.0 - (b1 + b2) * (pyc - ccy) + (a1 + a2) * ccx)):
        with np.errstate(divide="ignore", invalid="ignore"):
            borne = (-_TOL - origine) / pente
        lo = np.where(pente > 0, np.maximum(lo, borne), lo)
        hi = np.where(pente < 0, np.minimum(hi, borne), hi)
        vide = (pente == 0) & (origine < -_TOL)
        hi = np.where(vide, -np.inf, hi)
    # Une colonne de marge de chaque côté : le test exact ci-dessous tranche.
    xa = np.maximum(min_x[tq], np.floor(np.clip(lo, -1e9, 1e9) - 0.5).astype(np.int64) - 1)
    xb = np.minimum(max_x[tq], np.ceil(np.clip(hi, -1e9, 1e9) - 0.5).astype(np.int64) + 1)
    largeur = np.maximum(0, xb - xa + 1)
    if not largeur.any():
        return
    tp = np.repeat(tq, largeur)
    xx = np.repeat(xa, largeur) + (np.arange(tp.size) - np.repeat(np.cumsum(largeur) - largeur, largeur))
    yp = np.repeat(yy, largeur)
    pxc, pyc = xx + 0.5, yp + 0.5
    axp, ayp, bxp, byp, cxp, cyp, dp = ax[tp], ay[tp], bx[tp], by[tp], cx[tp], cy[tp], d[tp]
    wa = ((byp - cyp) * (pxc - cxp) + (cxp - bxp) * (pyc - cyp)) / dp
    wb = ((cyp - ayp) * (pxc - cxp) + (axp - cxp) * (pyc - cyp)) / dp
    wc = 1 - wa - wb
    dedans = (wa >= -_TOL) & (wb >= -_TOL) & (wc >= -_TOL)
    tp, xx, yp, wa, wb, wc = tp[dedans], xx[dedans], yp[dedans], wa[dedans], wb[dedans], wc[dedans]
    if tp.size == 0:
        return
    zz = (wa * za[tp] + wb * zb_[tp] + wc * zc[tp]).astype(F32)
    pix = yp * l + xx
    # Par pixel : profondeur maximale, à égalité le premier triangle (test strict « z > zb » de l'application).
    ordre = np.lexsort((tp, -zz, pix))
    pix, zz, tp = pix[ordre], zz[ordre], tp[ordre]
    premier = np.ones(pix.size, dtype=bool)
    premier[1:] = pix[1:] != pix[:-1]
    pix, zz, tp = pix[premier], zz[premier], tp[premier]
    mieux = zz > zb[pix]
    pix, zz, tp = pix[mieux], zz[mieux], tp[mieux]
    zb[pix] = zz
    couleur[pix] = rgb[tp]


def _degrade(l, h, centre, bord):
    """Dégradé radial (Brush.radialGradient de Compose : rayon = moitié du petit côté, bord prolongé)."""
    yy, xx = np.mgrid[0:h, 0:l].astype(np.float64)
    r = np.clip(np.hypot(xx + 0.5 - l / 2, yy + 0.5 - h / 2) / (min(l, h) / 2), 0.0, 1.0)[..., None]
    c, b = np.array(centre, float), np.array(bord, float)
    return np.floor(c * (1 - r) + b * r + 0.5).astype(np.uint8)


def composer(vue_rgba, bande_rgb, chemin, largeur=720, hauteur_bande_max=400, marge=14):
    """Aperçu final (PNG ≤ 1200 px) : vue 3D sur le fond de la vue « Forme 3D », paroi déroulée rétroéclairée dessous."""
    h3, l3 = vue_rgba.shape[:2]
    fond = Image.fromarray(_degrade(l3, h3, SURFACE_HAUTE, NUIT), "RGB").convert("RGBA")
    fond.alpha_composite(Image.fromarray(vue_rgba, "RGBA"))
    if l3 != largeur:
        h3 = max(1, int(round(h3 * largeur / l3)))
        fond = fond.resize((largeur, h3), Image.Resampling.BILINEAR)
    hb, lb = bande_rgb.shape[:2]
    utile_l = largeur - 2 * marge
    echelle = min(utile_l / lb, (hauteur_bande_max - 2 * marge) / hb)
    bl, bh = max(1, int(round(lb * echelle))), max(1, int(round(hb * echelle)))
    bande = Image.fromarray(bande_rgb, "RGB").resize((bl, bh), Image.Resampling.BILINEAR)
    boite_h = bh + 2 * marge
    cadre = Image.fromarray(_degrade(largeur, boite_h, BRUN, NUIT), "RGB")
    cadre.paste(bande, ((largeur - bl) // 2, marge))
    image = Image.new("RGB", (largeur, h3 + boite_h), NUIT)
    image.paste(fond.convert("RGB"), (0, 0))
    image.paste(cadre, (0, h3))
    image.save(chemin, "PNG", optimize=True)
    return image.size
