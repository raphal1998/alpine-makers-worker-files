# -*- coding: utf-8 -*-
"""Image d'annonce carrée (portage de Rendu.annonce et de l'éditeur d'annonce d'Alpine Gift).

Fond en dégradé avec lumière d'ambiance et étincelles, création mise en scène (halo, ombre qui suit la silhouette,
coins arrondis), titre et sous-titre centrés, filet, mention « Fait main », pastille de prix en option. Le texte est
écrit avec la police « Alpine Ronde » dessinée dans ``police.py`` (aucune police tierce). Numpy + Pillow seulement.
"""
import math
import random
import zlib

import numpy as np
from PIL import Image, ImageFilter

from . import police

# (libellé, fond haut, fond bas, texte, accent) — trois ambiances d'Alpine Gift + « Glacier » aux couleurs d'Alpine Makers.
STYLES = {
    "nuit": ("Nuit dorée", "#2A1430", "#0D0710", "#FFF4EC", "#FFD58A"),
    "creme": ("Crème", "#FFF6EE", "#F3DCCB", "#3A2430", "#E86A8A"),
    "sapin": ("Sapin", "#1D3A33", "#0B1814", "#F2F7F1", "#FFC76B"),
    "glacier": ("Glacier", "#12304F", "#060F1A", "#EEF6FF", "#7FD3FF"),
}
MAX_PIXELS = 50_000_000


def rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[k:k + 2], 16) for k in (0, 2, 4)], dtype=np.float32)


def _peindre(img, alpha, couleur, x0=0, y0=0):
    """Mélange ``couleur`` dans ``img`` (H, W, 3) avec la couverture ``alpha`` placée en (x0, y0), découpée au cadre."""
    h, w = alpha.shape
    H, W = img.shape[:2]
    ax0, ay0 = max(0, -x0), max(0, -y0)
    bx0, by0 = max(0, x0), max(0, y0)
    lw = min(w - ax0, W - bx0)
    lh = min(h - ay0, H - by0)
    if lw <= 0 or lh <= 0:
        return
    a = alpha[ay0:ay0 + lh, ax0:ax0 + lw, None]
    zone = img[by0:by0 + lh, bx0:bx0 + lw]
    c = couleur if np.ndim(couleur) == 1 else couleur[ay0:ay0 + lh, ax0:ax0 + lw]
    zone *= (1 - a)
    zone += a * c


def _disque(rayon):
    r = max(0.5, rayon)
    n = int(math.ceil(r)) + 1
    yy, xx = np.mgrid[-n:n + 1, -n:n + 1].astype(np.float32)
    return np.clip(r - np.hypot(xx, yy) + 0.5, 0, 1), n


def _rect_arrondi(w, h, r):
    """Couverture anticrénelée d'un rectangle w × h à coins de rayon r."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    qx = np.abs(xx + 0.5 - w / 2) - (w / 2 - r)
    qy = np.abs(yy + 0.5 - h / 2) - (h / 2 - r)
    d = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r
    return np.clip(0.5 - d, 0, 1)


def couper_lignes(texte, cap, largeur_max, max_lignes, graisse, interlettrage=0.0):
    """Coupe aux espaces (mots trop longs coupés à la lettre), au plus ``max_lignes`` ; dernière ligne tronquée par « … »."""
    mots = texte.split()
    lignes, courante = [], ""
    for mot in mots:
        essai = (courante + " " + mot).strip()
        if police.largeur(essai, cap, interlettrage, graisse) <= largeur_max:
            courante = essai
            continue
        if courante:
            lignes.append(courante)
        courante = mot
        while police.largeur(courante, cap, interlettrage, graisse) > largeur_max and len(courante) > 1:
            k = len(courante)
            while k > 1 and police.largeur(courante[:k], cap, interlettrage, graisse) > largeur_max:
                k -= 1
            lignes.append(courante[:k])
            courante = courante[k:]
    if courante:
        lignes.append(courante)
    if len(lignes) > max_lignes:
        lignes = lignes[:max_lignes]
        lignes[-1] = lignes[-1] + "…"
    if lignes and police.largeur(lignes[-1], cap, interlettrage, graisse) > largeur_max:
        derniere = lignes[-1].rstrip("…")
        while derniere and police.largeur(derniere.rstrip() + "…", cap, interlettrage, graisse) > largeur_max:
            derniere = derniere[:-1]
        lignes[-1] = derniere.rstrip() + "…"
    return lignes


def _ecrire(img, ligne, cap, centre_x, ligne_de_base, couleur, graisse, opacite=1.0, interlettrage=0.0):
    alpha, base, w = police.masque(ligne, cap, interlettrage, graisse)
    x0 = int(round(centre_x - w / 2)) - 1
    y0 = int(round(ligne_de_base - base))
    _peindre(img, alpha * opacite, couleur, x0, y0)
    return w


def charger_photo(chemin):
    from PIL import ImageOps, UnidentifiedImageError
    try:
        with Image.open(chemin) as im:
            l, h = im.size
            if l * h > MAX_PIXELS:
                raise ValueError(f"Photo trop grande ({l} × {h} px) : 50 mégapixels au plus.")
            if min(l, h) < 16:
                raise ValueError("Photo trop petite (16 px au moins).")
            im = ImageOps.exif_transpose(im)
            return im.convert("RGBA")
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as erreur:
        raise ValueError("Photo illisible : envoie un PNG, un JPEG ou un WebP.") from erreur


def annonce(photo, titre, sous_titre, prix, style, lumineuse, coins, mention, cote=1080):
    """Rend une image PIL RGB carrée ``cote`` × ``cote``."""
    _, f0, f1, c_texte, c_accent = STYLES[style]
    fond0, fond1, texte, accent = rgb(f0), rgb(f1), rgb(c_texte), rgb(c_accent)
    f = float(cote)
    k = f / 1080.0
    yy = (np.arange(cote, dtype=np.float32) / max(1, cote - 1))[:, None, None]
    img = np.broadcast_to(fond0 * (1 - yy) + fond1 * yy, (cote, cote, 3)).copy()
    # Lumière d'ambiance.
    gy, gx = np.mgrid[0:cote, 0:cote].astype(np.float32)
    d = np.hypot(gx - f * 0.5, gy - f * 0.4) / (f * 0.55)
    _peindre(img, np.clip(1 - d, 0, 1) * (0x40 / 255), accent)
    del d
    # Étincelles (graine tirée du titre : même titre, même ciel).
    rnd = random.Random(zlib.crc32(titre.encode("utf-8")))
    for _ in range(26):
        a = (30 + rnd.randrange(90)) / 255
        x, y = rnd.random() * f, rnd.random() * f * 0.7
        disque, n = _disque((2 + rnd.random() * 5) * k)
        _peindre(img, disque * a, accent, int(round(x)) - n, int(round(y)) - n)
    # Textes mis en page depuis le bas : mention, filet, sous-titre, titre ; la création occupe le reste.
    largeur_texte = f * 0.84
    t = titre.strip() or "Cadeau personnalisé"
    cap_t, lh_t = f * 0.047, f * 0.078
    cap_s, lh_s = f * 0.0225, f * 0.04
    lignes_t = couper_lignes(t, cap_t, largeur_texte, 2, "grasse")
    lignes_s = couper_lignes(sous_titre.strip(), cap_s, largeur_texte, 2, "normale") if sous_titre.strip() else []
    y_mention = f * 0.955
    filet = f * 0.905
    bas_sous = filet - f * 0.03
    haut_sous = bas_sous - len(lignes_s) * lh_s
    haut_titre = haut_sous - f * 0.012 - len(lignes_t) * lh_t
    # Création ajustée dans la zone restante.
    zx0, zy0, zx1, zy1 = f * 0.12, f * 0.07, f * 0.88, haut_titre - f * 0.04
    zw, zh = zx1 - zx0, zy1 - zy0
    if zh < f * 0.2:
        raise ValueError("Titre et sous-titre trop longs : la photo n'a plus de place.")
    s = min(zw / photo.width, zh / photo.height)
    w, h = max(1, int(round(photo.width * s))), max(1, int(round(photo.height * s)))
    cx, cy = (zx0 + zx1) / 2, (zy0 + zy1) / 2
    px0, py0 = int(round(cx - w / 2)), int(round(cy - h / 2))
    image = photo.resize((w, h), Image.LANCZOS)
    tableau = np.asarray(image, dtype=np.float32)
    a_photo = tableau[..., 3] / 255
    if coins and a_photo.min() > 0.99:
        a_photo = a_photo * _rect_arrondi(w, h, min(w, h) * 0.04)
    if lumineuse:
        rh = max(w, h) * 0.75
        dh = np.hypot(gx - cx, gy - cy) / rh
        _peindre(img, np.clip(1 - dh, 0, 1) * (0x88 / 255), rgb("#FFE6B0"))
        del dh
    # Ombre portée qui suit la silhouette.
    marge = int(math.ceil(f * 0.04))
    sil = np.zeros((h + 2 * marge, w + 2 * marge), dtype=np.uint8)
    sil[marge:marge + h, marge:marge + w] = (a_photo * 255).astype(np.uint8)
    flou = np.asarray(Image.fromarray(sil).filter(ImageFilter.GaussianBlur(f * 0.011)), dtype=np.float32) / 255
    _peindre(img, flou * ((0x40 if lumineuse else 0x80) / 255), np.zeros(3, np.float32),
             px0 - marge + int(round(f * 0.008)), py0 - marge + int(round(f * 0.02)))
    _peindre(img, a_photo, tableau[..., :3], px0, py0)
    del gx, gy
    # Titre, sous-titre, filet, mention.
    for i, ligne in enumerate(lignes_t):
        _ecrire(img, ligne, cap_t, f / 2, haut_titre + i * lh_t + cap_t + (lh_t - cap_t) * 0.42, texte, "grasse")
    for i, ligne in enumerate(lignes_s):
        _ecrire(img, ligne, cap_s, f / 2, haut_sous + i * lh_s + cap_s + (lh_s - cap_s) * 0.42, texte, "normale",
                opacite=0xC8 / 255)
    epais = max(1, int(round(f * 0.004)))
    trait = np.ones((epais, int(round(f * 0.12))), dtype=np.float32)
    _peindre(img, trait, accent, int(round(f * 0.44)), int(round(filet - epais / 2)))
    if mention.strip():
        cap_m = f * 0.0175
        _ecrire(img, mention.strip().upper(), cap_m, f / 2, y_mention, accent, "grasse", interlettrage=f * 0.006)
    # Pastille de prix, en haut à droite.
    if prix.strip():
        cap_p = f * 0.032
        etiquette = prix.strip()
        largeur_p = police.largeur(etiquette, cap_p, 0, "grasse")
        bw, bh = int(round(largeur_p + cap_p * 1.6)), int(round(cap_p * 2.3))
        bx0, by0 = int(round(f * 0.95 - bw)), int(round(f * 0.045))
        ombre = np.zeros((bh + 2 * marge, bw + 2 * marge), dtype=np.uint8)
        ombre[marge:marge + bh, marge:marge + bw] = (_rect_arrondi(bw, bh, bh / 2) * 255).astype(np.uint8)
        ombre = np.asarray(Image.fromarray(ombre).filter(ImageFilter.GaussianBlur(f * 0.008)), dtype=np.float32) / 255
        _peindre(img, ombre * 0.45, np.zeros(3, np.float32), bx0 - marge, by0 - marge + int(round(f * 0.008)))
        _peindre(img, _rect_arrondi(bw, bh, bh / 2), accent, bx0, by0)
        clair = float(np.dot(accent, [0.299, 0.587, 0.114])) > 150
        _ecrire(img, etiquette, cap_p, bx0 + bw / 2, by0 + bh / 2 + cap_p / 2, rgb("#1A1014") if clair else rgb("#FFFFFF"),
                "grasse")
    return Image.fromarray(np.clip(img + 0.5, 0, 255).astype(np.uint8), "RGB"), len(lignes_t), len(lignes_s)
