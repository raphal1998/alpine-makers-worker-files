# -*- coding: utf-8 -*-
"""Image en niveaux de gris (portage de Gris, Image.kt d'Alpine Gift) et lecture de la photo (Photos.kt).

Une image : tableau float32 (hauteur, largeur), 0 = noir, 1 = blanc, ligne 0 en haut.
"""
import math

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

F32 = np.float32
COTE_MAX = 2400          # Photos.COTE_MAX : côté gardé en mémoire
COTE_FEUILLE = 1600      # Photos.gris(…, 1600, 1600) : détection des repères et redressement
COTE_LIBRE = 1000        # Photos.gris(…, 1000, 1000) : cadrage libre
MAX_PIXELS = 60_000_000
MIN_COTE = 300


def _axe(u, n):
    """Indices et poids bilinéaires d'Gris.echantillon le long d'un axe (coordonnées normalisées 0..1)."""
    f = np.clip(np.asarray(u, dtype=np.float64) * n - 0.5, 0.0, n - 1.0)
    i0 = np.floor(f).astype(np.int64)
    i1 = np.minimum(i0 + 1, n - 1)
    t = (f - i0).astype(F32)
    return i0, i1, t


def echantillon_grille(g, u, w):
    """Gris.echantillon aux points (u[i], w[j]) d'une grille séparable : tableau float32 (len(w), len(u))."""
    h, l = g.shape
    x0, x1, tx = _axe(u, l)
    y0, y1, ty = _axe(w, h)
    a = g[y0[:, None], x0[None, :]] + (g[y0[:, None], x1[None, :]] - g[y0[:, None], x0[None, :]]) * tx[None, :]
    b = g[y1[:, None], x0[None, :]] + (g[y1[:, None], x1[None, :]] - g[y1[:, None], x0[None, :]]) * tx[None, :]
    return a + (b - a) * ty[:, None]


def echantillon_points(g, u, w):
    """Gris.echantillon aux points (u[k], w[k]) quelconques (tableaux de même forme)."""
    h, l = g.shape
    x0, x1, tx = _axe(u, l)
    y0, y1, ty = _axe(w, h)
    a = g[y0, x0] + (g[y0, x1] - g[y0, x0]) * tx
    b = g[y1, x0] + (g[y1, x1] - g[y1, x0]) * tx
    return a + (b - a) * ty


def _poids(n_src, n_dst):
    """Recouvrement des pixels source par chaque pixel de destination (moyenne des pixels couverts)."""
    s = n_src / n_dst
    k = int(math.ceil(s)) + 1
    d = np.arange(n_dst)
    a, b = d * s, (d + 1) * s
    p0 = np.floor(a).astype(np.int64)
    idx = p0[:, None] + np.arange(k)[None, :]
    w = np.minimum(b[:, None], idx + 1.0) - np.maximum(a[:, None], idx.astype(np.float64))
    w = np.where((idx < n_src) & (idx < b[:, None]), np.maximum(w, 0.0), 0.0)
    return np.minimum(idx, n_src - 1), w


def redimensionner(g, l, h):
    """Gris.redimensionner : moyenne des pixels couverts en réduction, bilinéaire en agrandissement."""
    hs, ls = g.shape
    if (l, h) == (ls, hs):
        return g.copy()
    if ls / l <= 1.0 and hs / h <= 1.0:
        return echantillon_grille(g, (np.arange(l) + 0.5) / l, (np.arange(h) + 0.5) / h)
    ix, wx = _poids(ls, l)
    iy, wy = _poids(hs, h)
    src = g.astype(np.float64)
    col = (src[:, ix] * wx[None, :, :]).sum(axis=2)          # (hs, l)
    som = (col[iy, :] * wy[:, :, None]).sum(axis=1)           # (h, l)
    poids = wy.sum(axis=1)[:, None] * wx.sum(axis=1)[None, :]
    return np.where(poids > 0, som / np.where(poids > 0, poids, 1.0), 0.0).astype(F32)


def depuis_rgba(rgba):
    """Gris.depuisArgb : luminance Rec. 709 des valeurs sRGB, transparent → blanc (papier)."""
    a = rgba[..., 3].astype(F32) / F32(255)
    r = rgba[..., 0].astype(F32) / F32(255)
    v = rgba[..., 1].astype(F32) / F32(255)
    b = rgba[..., 2].astype(F32) / F32(255)
    return (F32(0.2126) * r + F32(0.7152) * v + F32(0.0722) * b) * a + (F32(1) - a)


def otsu_feuille(g):
    """FeuilleReperes.otsu : seuil (0..1, float32) qui sépare les repères du papier."""
    q = np.floor((np.clip(g, F32(0), F32(1)) * F32(255)).astype(np.float64) + 0.5).astype(np.int64)
    hist = np.bincount(q.ravel(), minlength=256)[:256].tolist()
    total = float(g.size)
    somme = float(sum(i * hist[i] for i in range(256)))
    sb = wb = meilleur = 0.0
    seuil = 128
    for i in range(256):
        wb += hist[i]
        if wb == 0.0:
            continue
        wf = total - wb
        if wf == 0.0:
            break
        sb += i * float(hist[i])
        mb = sb / wb
        mf = (somme - sb) / wf
        entre = wb * wf * (mb - mf) * (mb - mf)
        if entre > meilleur:
            meilleur, seuil = entre, i
    return float((F32(seuil) + F32(0.5)) / F32(255))


# ------------------------------------------------------------------ photo
def charger_photo(chemin):
    """Photo orientée (EXIF), réduite à 2400 px de côté au plus (Photos.charger), en RGBA uint8."""
    if chemin is None or not chemin.is_file():
        raise ValueError("Photo du dessin manquante.")
    try:
        img = Image.open(chemin)
    except Image.DecompressionBombError:
        raise ValueError("Photo trop grande : 60 Mpx au plus.") from None
    except (UnidentifiedImageError, OSError, ValueError):
        raise ValueError("Ce fichier n'est pas une image lisible.") from None
    with img:
        l, h = img.size
        if l <= 0 or h <= 0:
            raise ValueError("Ce fichier n'est pas une image lisible.")
        if l * h > MAX_PIXELS:
            raise ValueError(f"Photo trop grande ({l} × {h} px) : 60 Mpx au plus.")
        if max(l, h) < MIN_COTE:
            raise ValueError(f"Photo trop petite ({l} × {h} px) : il faut au moins {MIN_COTE} px de côté.")
        try:
            if img.format == "JPEG" and max(l, h) > COTE_MAX:
                # Décodage réduit (comme inSampleSize) : jamais la pleine résolution d'un capteur en mémoire.
                m = max(l, h)
                img.draft("RGB", (max(1, l * COTE_MAX // m), max(1, h * COTE_MAX // m)))
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGBA")
        except (OSError, ValueError, Image.DecompressionBombError):
            raise ValueError("Photo illisible (fichier tronqué ou abîmé).") from None
    s = COTE_MAX / max(img.size)
    if s < 1:
        img = img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.BILINEAR)
    return img


def gris(photo, cadre=(0.0, 0.0, 1.0, 1.0), cote_max=COTE_FEUILLE):
    """Photos.gris : partie recadrée (fractions u0, v0, u1, v1), réduite pour tenir dans cote_max × cote_max."""
    l0, h0 = photo.size
    x0 = min(max(round(cadre[0] * l0), 0), l0 - 1)
    y0 = min(max(round(cadre[1] * h0), 0), h0 - 1)
    l = min(max(round((cadre[2] - cadre[0]) * l0), 1), l0 - x0)
    h = min(max(round((cadre[3] - cadre[1]) * h0), 1), h0 - y0)
    s = min(1.0, cote_max / l, cote_max / h)
    lc, hc = max(1, round(l * s)), max(1, round(h * s))
    part = photo.crop((x0, y0, x0 + l, y0 + h))
    if (lc, hc) != (l, h):
        part = part.resize((lc, hc), Image.BILINEAR)
    return depuis_rgba(np.asarray(part, dtype=np.uint8))
