"""Photo → niveaux de gris et réglages d'image, portés d'Alpine Gift (core Image.kt, data/Photos.kt).

Copie propre à cet outil (un outil Alpine Tools n'importe aucun autre outil ; base : lithophanie/image.py). Les calculs
gardent les flottants 32 bits de l'application (Float de Kotlin) là où ils comptent. ``redimensionner`` refait la
moyenne des pixels couverts dans l'ordre exact de Gris.redimensionner (mêmes valeurs au bit près).
Images : tableaux numpy float32 (hauteur, largeur), 0 = noir, 1 = blanc, ligne du haut d'abord.
"""
import math

import numpy as np
from PIL import Image

F32 = np.float32
# Côté maximal gardé en mémoire (Photos.COTE_MAX) ; l'export de l'application lit les photos à 2400 px.
COTE_MAX = 2400
# Photo reçue : 24 Mpx au plus (refus explicite au-delà, jamais de décodage géant).
PIXELS_SOURCE_MAX = 24_000_000

# Orientation EXIF → transposition Pillow (mêmes rotations que Photos.charger, plus les deux miroirs diagonaux).
_EXIF = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
         5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
         8: Image.Transpose.ROTATE_90}


def arrondi(x):
    """roundToInt de Kotlin (demi vers le haut), pas l'arrondi bancaire de Python."""
    return int(math.floor(x + 0.5))


def charger_photo(chemin, cote_max=COTE_MAX):
    """Photo RGBA réduite comme dans l'application : sous-échantillonnage par 2ⁿ, orientation EXIF, côté ≤ cote_max."""
    try:
        img = Image.open(chemin)
    except (OSError, ValueError, Image.DecompressionBombError):
        raise ValueError("Ce fichier n'est pas une image lisible.") from None
    with img:
        largeur, hauteur = img.size
        if largeur <= 0 or hauteur <= 0:
            raise ValueError("Ce fichier n'est pas une image lisible.")
        if largeur * hauteur > PIXELS_SOURCE_MAX:
            raise ValueError(f"Photo trop grande ({largeur} × {hauteur} px) : 24 mégapixels au plus.")
        try:
            orientation = int(img.getexif().get(0x0112, 1) or 1)
        except Exception:  # noqa: BLE001 - EXIF abîmé : orientation normale, comme l'application
            orientation = 1
        try:
            img.load()
            rgba = img.convert("RGBA")
        except (OSError, ValueError, SyntaxError):
            raise ValueError("Ce fichier n'est pas une image lisible (fichier abîmé ?).") from None
    echantillon = 1
    while max(largeur, hauteur) // (echantillon * 2) >= cote_max:
        echantillon *= 2
    if echantillon > 1:
        rgba = rgba.reduce(echantillon)
    if orientation in _EXIF:
        rgba = rgba.transpose(_EXIF[orientation])
    s = cote_max / max(rgba.size)
    if s < 1.0:
        rgba = rgba.resize((max(1, arrondi(rgba.width * s)), max(1, arrondi(rgba.height * s))), Image.Resampling.BILINEAR)
    return rgba


def cadre_centre(largeur_photo, hauteur_photo, ratio):
    """Cadre du recadreur sans zoom ni déplacement (zoom 1) : le plus grand rectangle centré au format ; None = tout."""
    if ratio is None:
        return (0.0, 0.0, 1.0, 1.0)
    t_l, t_h = ratio, 1.0
    s = max(t_l / largeur_photo, t_h / hauteur_photo)
    lw, lh = largeur_photo * s, hauteur_photo * s
    gauche, haut = t_l / 2 - lw / 2, t_h / 2 - lh / 2

    def borne(v):
        return min(1.0, max(0.0, v))
    return (borne(-gauche / lw), borne(-haut / lh), borne((t_l - gauche) / lw), borne((t_h - haut) / lh))


def gris(photo, cadre, largeur_max, hauteur_max=None):
    """Photos.gris : partie recadrée, réduite (jamais agrandie) pour tenir dans largeur_max × hauteur_max, en gris."""
    w, h = photo.size
    u0, v0, u1, v1 = cadre
    x0 = min(max(arrondi(u0 * w), 0), w - 1)
    y0 = min(max(arrondi(v0 * h), 0), h - 1)
    lc = min(max(arrondi((u1 - u0) * w), 1), w - x0)
    hc = min(max(arrondi((v1 - v0) * h), 1), h - y0)
    s = min(1.0, largeur_max / lc, (hauteur_max / hc) if hauteur_max else math.inf)
    lr, hr = max(1, arrondi(lc * s)), max(1, arrondi(hc * s))
    partie = photo.crop((x0, y0, x0 + lc, y0 + hc))
    if (lr, hr) != (lc, hc):
        partie = partie.resize((lr, hr), Image.Resampling.BILINEAR)
    return depuis_rgba(np.asarray(partie, dtype=np.uint8))


def depuis_rgba(px):
    """Gris.depuisArgb : luminance Rec. 709, transparent → blanc (fond de papier)."""
    c = px.astype(F32) / F32(255)
    r, g, b, a = c[..., 0], c[..., 1], c[..., 2], c[..., 3]
    return ((F32(0.2126) * r + F32(0.7152) * g + F32(0.0722) * b) * a + (F32(1) - a)).astype(F32)


def _axe_bilineaire(coord, n):
    f = np.clip(coord * n - 0.5, 0.0, n - 1.0)
    i0 = np.floor(f).astype(np.int64)
    i1 = np.minimum(i0 + 1, n - 1)
    return i0, i1, (f - i0).astype(F32)


def echantillon_grille(v, us, ws, bloc=512):
    """Gris.echantillon (bilinéaire, bords prolongés) sur la grille us × ws (coordonnées normalisées) → (len(ws), len(us))."""
    hauteur, largeur = v.shape
    x0, x1, tx = _axe_bilineaire(np.asarray(us, dtype=np.float64), largeur)
    y0, y1, ty = _axe_bilineaire(np.asarray(ws, dtype=np.float64), hauteur)
    sortie = np.empty((len(y0), len(x0)), dtype=F32)
    for d in range(0, len(y0), bloc):
        r0, r1, t = v[y0[d:d + bloc]], v[y1[d:d + bloc]], ty[d:d + bloc, None]
        a = r0[:, x0] + (r0[:, x1] - r0[:, x0]) * tx
        b = r1[:, x0] + (r1[:, x1] - r1[:, x0]) * tx
        sortie[d:d + bloc] = a + (b - a) * t
    return sortie


def _fenetres(n_entree, n_sortie):
    """Pixels couverts par chaque pixel de sortie le long d'un axe : (indices, poids) par décalage, comme la boucle
    « while (p < x1 && p < n) » de Gris.redimensionner (poids nul hors fenêtre)."""
    s = n_entree / n_sortie
    k = np.arange(n_sortie)
    a, b = k * s, (k + 1) * s
    p0 = np.floor(a).astype(np.int64)
    largeur = int(np.max(np.minimum(np.ceil(b), n_entree) - p0)) + 1
    sortie = []
    for d in range(max(1, largeur)):
        p = p0 + d
        dedans = (p < b) & (p < n_entree)
        w = np.where(dedans, np.minimum(b, p + 1.0) - np.maximum(a, p.astype(np.float64)), 0.0)
        if not dedans.any():
            break
        sortie.append((np.minimum(p, n_entree - 1), w))
    return sortie


def redimensionner(v, largeur, hauteur):
    """Gris.redimensionner : moyenne des pixels couverts en réduction (sommes doubles dans l'ordre de l'application),
    bilinéaire quand les deux côtés s'agrandissent."""
    h0, l0 = v.shape
    if (largeur, hauteur) == (l0, h0):
        return v.copy()
    sx, sy = l0 / largeur, h0 / hauteur
    if sx <= 1.0 and sy <= 1.0:
        return echantillon_grille(v, (np.arange(largeur) + 0.5) / largeur, (np.arange(hauteur) + 0.5) / hauteur)
    fx, fy = _fenetres(l0, largeur), _fenetres(h0, hauteur)
    somme = np.zeros((hauteur, largeur), dtype=np.float64)
    poids = np.zeros((hauteur, largeur), dtype=np.float64)
    for py, wy in fy:
        rangees = v[py].astype(np.float64)
        for px, wx in fx:
            w = wy[:, None] * wx[None, :]
            somme += rangees[:, px] * w
            poids += w
    return np.where(poids > 0, somme / np.where(poids > 0, poids, 1.0), 0.0).astype(F32)


def nettete(s, k):
    """Masque flou : g + k·(g − flou 3×3), bords prolongés, borné à 0..1."""
    p = np.pad(s, 1, mode="edge")
    h, l = s.shape
    somme = np.zeros_like(s, dtype=F32)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            somme += p[1 + dy:1 + dy + h, 1 + dx:1 + dx + l]
    flou = somme / F32(9)
    return np.clip(s + F32(k) * (s - flou), F32(0), F32(1)).astype(F32)


def appliquer(source, luminosite=0.0, contraste=0.0, gamma=1.0, nettete_k=0.0, negatif=False):
    """Traitement.appliquer : netteté, luminosité, contraste, gamma, négatif (table de 1024 valeurs). Nouvelle image."""
    g = nettete(source, nettete_k) if nettete_k > 0.001 else source.astype(F32, copy=True)
    c = 1.0 + contraste * 3.0 if contraste >= 0 else 1.0 + contraste
    inv_gamma = 1.0 / min(3.0, max(0.2, gamma))
    x = np.arange(1024, dtype=np.float64) / 1023.0
    x = x + luminosite * 0.5
    x = (x - 0.5) * c + 0.5
    x = np.clip(x, 0.0, 1.0) ** inv_gamma
    if negatif:
        x = 1.0 - x
    table = x.astype(F32)
    index = np.floor((np.clip(g, F32(0), F32(1)) * F32(1023)).astype(np.float64) + 0.5).astype(np.int64)
    return table[index]
