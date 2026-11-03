# -*- coding: utf-8 -*-
"""Sorties laser du Ciel étoilé : PNG gris avec la résolution physique (Gift Png.ecrireGris) et projet Alpine Laser
Studio avec tracé de découpe (Gift LaserStudio.projet, variante « découpes »). Repris de alpine_tools/gravure_photo
(un outil n'importe aucun autre outil) et complété du calque de découpe rouge."""
import base64
import json
import math
import struct
import zlib

import numpy as np

MARGE_MM = 10.0
COULEUR = "#000000"
DECOUPE = "#ff0000"   # calque de découpe : rouge, comme le calque 02 de LightBurn et la palette du studio
RESOLUTIONS = {"10": 10.0, "12.7": 12.7, "20": 20.0}

# Matériaux de l'aperçu (Gift Materiau) : libellé, couleur de la matière brute, couleur de la trace du laser.
MATERIAUX = {
    "bois_clair": ("Bois clair", 0xE6C89A, 0x3B2414),
    "bois_fonce": ("Bois foncé", 0x9C6B43, 0x24140A),
    "ardoise": ("Ardoise", 0x2E3135, 0xC9CDD2),
    "verre": ("Verre", 0x1B2B33, 0xE8F1F4),
    "cuir": ("Cuir", 0xB07A4C, 0x3A2010),
    "acrylique_peint": ("Acrylique peint", 0x141416, 0xF2F2F2),
    "alu_anodise": ("Aluminium anodisé", 0x23262B, 0xDCDDE0),
}


def ppp(px_par_mm):
    """Points par pouce affichés : (px/mm × 25,4) arrondi (roundToInt)."""
    return int(math.floor(px_par_mm * 25.4 + 0.5))


def pixels_par_metre(px_par_mm):
    return int(math.floor(px_par_mm * 1000.0 + 0.5))


def _bloc(type_, contenu):
    t = type_.encode("ascii")
    return struct.pack(">I", len(contenu)) + t + contenu + struct.pack(">I", zlib.crc32(t + contenu) & 0xFFFFFFFF)


def png_gris(pixels, px_par_mm):
    """PNG 8 bits en niveaux de gris, bloc pHYs (pixels par mètre) avant les données, filtre « aucun » par ligne."""
    h, l = pixels.shape
    ppm = pixels_par_metre(px_par_mm)
    lignes = np.empty((h, l + 1), dtype=np.uint8)
    lignes[:, 0] = 0
    lignes[:, 1:] = pixels
    return (b"\x89PNG\r\n\x1a\n"
            + _bloc("IHDR", struct.pack(">IIBBBBB", l, h, 8, 0, 0, 0, 0))
            + _bloc("pHYs", struct.pack(">IIB", ppm, ppm, 1))
            + _bloc("tEXt", b"Software\x00Alpine Makers - Alpine Tools")
            + _bloc("IDAT", zlib.compress(lignes.tobytes(), 6))
            + _bloc("IEND", b""))


def _arrondi(v, decimales):
    f = 10.0 ** decimales
    return math.floor(v * f + 0.5) / f


def projet(nom, r, png, materiau, nom_calque, decoupes, ident="atool-ciel"):
    """Texte JSON du projet .alsproj : image tramée à la taille réelle (calque #000000 en mode image « Relais ») et
    tracés de découpe (polylignes fermées en mm, repère de l'image, origine en haut à gauche) sur le calque rouge
    #ff0000 en mode Ligne (découpe). ``r`` : {largeur_px, hauteur_px, largeur_mm, hauteur_mm, px_par_mm}."""
    if len(png) < 8 or png[:2] != b"\x89P":
        raise ValueError("PNG attendu.")
    titre = (nom.strip() or "Gravure photo")[:60]
    image = {"id": ident, "type": "image", "name": titre, "x": MARGE_MM, "y": MARGE_MM,
             "w": _arrondi(r["largeur_mm"], 3), "h": _arrondi(r["hauteur_mm"], 3), "rotation": 0,
             "src": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
             "rasterMode": "dither", "invert": False, "layer": COULEUR}
    calque = {"name": nom_calque[:60], "color": COULEUR, "mode": "image",
              # Relais : image envoyée telle quelle (déjà tramée).
              "pass_through": True, "image_mode": "threshold", "negative": False,
              "interval_mm": _arrondi(1.0 / r["px_par_mm"], 4), "overscan_pct": 2.5, "bidirectional": True,
              "enabled": True, "visible": True, "auto_optimize": True}
    traces = [t for t in decoupes if len(t) >= 2]
    objets = [image]
    calques = {COULEUR: calque}
    if traces:
        objets.append({
            "id": f"{ident}-decoupe", "type": "path", "name": "Découpe", "x": MARGE_MM, "y": MARGE_MM,
            "w": _arrondi(r["largeur_mm"], 3), "h": _arrondi(r["hauteur_mm"], 3), "rotation": 0, "layer": DECOUPE,
            "paths": [{"closed": True,
                       "points": [[_arrondi(x / r["largeur_mm"], 6), _arrondi(y / r["hauteur_mm"], 6)] for x, y in t]}
                      for t in traces]})
        calques[DECOUPE] = {"name": "Découpe (Alpine Tools)", "color": DECOUPE, "mode": "line", "line_operation": "cut",
                            "enabled": True, "visible": True, "auto_optimize": True}
    document = {"version": 1, "app": "alpine-laser-studio", "name": titre, "objects": objets, "layers": calques,
                "activeLayer": COULEUR, "order": "layer", "offset": [0, 0],
                "source": {"app": "alpine-tools", "outil": "atool-ciel-etoile", "materiau": materiau,
                           "px_par_mm": r["px_par_mm"], "largeur_px": r["largeur_px"], "hauteur_px": r["hauteur_px"]}}
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


def _rvb(c):
    return np.array([(c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF], dtype=np.uint8)
