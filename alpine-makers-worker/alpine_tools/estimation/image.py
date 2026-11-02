"""Image matricielle à graver (port de Import.image d'Alpine Prism) : taille en pixels et part des pixels sombres.

Image réduite par puissances de deux à 2048 px au plus ; pixel sombre = alpha ≥ 128 et luminance < 50 %.
"""
import numpy as np
from PIL import Image, UnidentifiedImageError

MAX_PIXELS = 120_000_000


def mesurer(chemin):
    """{"largeurPx", "hauteurPx", "ratioSombre"} ; ValueError en français si l'image est illisible ou trop grande."""
    try:
        with Image.open(chemin) as img:
            largeur, hauteur = img.size
            if largeur <= 0 or hauteur <= 0:
                raise ValueError("Image illisible.")
            if largeur * hauteur > MAX_PIXELS:
                raise ValueError("Image trop grande (120 millions de pixels au plus).")
            echantillon = 1
            while largeur // echantillon > 2048 or hauteur // echantillon > 2048:
                echantillon *= 2
            if echantillon > 1:
                img.draft("RGB", (largeur // echantillon, hauteur // echantillon))
            img.load()
            rgba = img.convert("RGBA")
            if echantillon > 1:
                rgba = rgba.resize((max(1, largeur // echantillon), max(1, hauteur // echantillon)), Image.Resampling.BOX)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("Image illisible.") from None
    px = np.asarray(rgba, dtype=np.int32)
    lum = (px[..., 0] * 299 + px[..., 1] * 587 + px[..., 2] * 114) // 1000
    sombres = int(np.count_nonzero((px[..., 3] >= 128) & (lum < 128)))
    return {"largeurPx": largeur, "hauteurPx": hauteur, "ratioSombre": sombres / max(1, px.shape[0] * px.shape[1])}
