"""Gravure photo laser du puzzle : copie propre à cet outil de ``gravure_photo/gravure.py`` (portage d'Alpine Gift :
Gravure.kt, Tramage.kt, Png.kt, LaserStudio.kt ; ui Rendu.gravure), avec en plus les tracés de découpe du projet.

Image à la taille réelle (px = mm × px/mm), réglée puis tramée ; PNG en niveaux de gris avec sa résolution physique
(bloc pHYs) ; projet Alpine Laser Studio portable (.alsproj) avec la trame sur un calque Image en mode Relais et les
tracés de découpe sur le calque Ligne #ff0000 (LaserStudio.projet avec découpes, Puzzle.projet).
Les diffusions d'erreur reprennent l'arithmétique float32 de l'application (même ordre des additions) : la trame
obtenue est celle de l'application pour une même image d'entrée.
"""
import base64
import json
import math
import struct
import zlib
from array import array

import numpy as np

F32 = np.float32
RESOLUTIONS = (10.0, 12.7, 20.0)
PIXELS_MAX = 16_000_000
MARGE_MM = 10.0
COULEUR = "#000000"

# (libellé, détail) des méthodes de tramage.
METHODES = {
    "floyd_steinberg": ("Floyd–Steinberg", "Diffusion d'erreur classique, fine et fidèle."),
    "jarvis": ("Jarvis", "Diffusion large : grain doux, idéal sur bois."),
    "stucki": ("Stucki", "Diffusion large et nette : cuir, ardoise."),
    "atkinson": ("Atkinson", "Contraste élevé, hautes lumières claires : verre, métal."),
    "bayer": ("Bayer", "Trame ordonnée 8×8, régulière."),
    "seuil": ("Seuil", "Noir ou blanc à 50 % : logos, textes."),
    "niveaux": ("Niveaux de gris", "Puissance variable (mode « Grayscale » du logiciel laser)."),
}

# Préréglages de matériau : réglages conseillés, tramage, couleurs de la simulation (matière brute, trace du laser).
# négatif : le laser éclaircit la matière (ardoise, verre, métal anodisé, peinture retirée).
MATERIAUX = {
    "bois_clair": {"libelle": "Bois clair", "reglages": {"contraste": 0.15, "nettete": 0.6}, "methode": "jarvis",
                   "matiere": 0xE6C89A, "trace": 0x3B2414,
                   "conseil": "Contreplaqué bouleau, érable, peuplier : 254–318 ppp, puissance modérée."},
    "bois_fonce": {"libelle": "Bois foncé", "reglages": {"luminosite": 0.18, "contraste": 0.3, "gamma": 1.2, "nettete": 0.7},
                   "methode": "jarvis", "matiere": 0x9C6B43, "trace": 0x24140A,
                   "conseil": "Noyer, cerisier : éclaircir l'image, le contraste du bois est faible."},
    "ardoise": {"libelle": "Ardoise", "reglages": {"contraste": 0.2, "nettete": 0.5, "negatif": True}, "methode": "stucki",
                "matiere": 0x2E3135, "trace": 0xC9CDD2, "conseil": "Le laser blanchit l'ardoise : image en négatif."},
    "verre": {"libelle": "Verre", "reglages": {"luminosite": 0.05, "contraste": 0.25, "negatif": True}, "methode": "atkinson",
              "matiere": 0x1B2B33, "trace": 0xE8F1F4,
              "conseil": "Le verre se dépolit en blanc : négatif ; papier humide ou savon pour limiter les éclats."},
    "cuir": {"libelle": "Cuir", "reglages": {"contraste": 0.1, "nettete": 0.4}, "methode": "stucki",
             "matiere": 0xB07A4C, "trace": 0x3A2010,
             "conseil": "Cuir végétal : puissance faible, plusieurs passes plutôt qu'une forte."},
    "acrylique_peint": {"libelle": "Acrylique peint", "reglages": {"contraste": 0.2, "negatif": True},
                        "methode": "floyd_steinberg", "matiere": 0x141416, "trace": 0xF2F2F2,
                        "conseil": "Le laser retire la peinture et révèle le clair : négatif."},
    "alu_anodise": {"libelle": "Aluminium anodisé", "reglages": {"contraste": 0.25, "nettete": 0.8, "negatif": True},
                    "methode": "jarvis", "matiere": 0x23262B, "trace": 0xDCDDE0,
                    "conseil": "L'anodisation devient blanche sous le laser : négatif, diode 5 W et plus."},
}

REGLAGES_NEUTRES = {"luminosite": 0.0, "contraste": 0.0, "gamma": 1.0, "nettete": 0.0, "negatif": False}


def reglages_materiau(cle):
    return {**REGLAGES_NEUTRES, **MATERIAUX[cle]["reglages"]}


def ppp(px_par_mm):
    return int(math.floor(px_par_mm * 25.4 + 0.5))


# --- Tramage ---------------------------------------------------------------------------------------------------------

# Noyaux (dx, dy, poids) dans l'ordre de l'application, et diviseur.
NOYAUX = {
    "floyd_steinberg": (16, ((1, 0, 7), (-1, 1, 3), (0, 1, 5), (1, 1, 1))),
    "jarvis": (48, ((1, 0, 7), (2, 0, 5), (-2, 1, 3), (-1, 1, 5), (0, 1, 7), (1, 1, 5), (2, 1, 3),
                    (-2, 2, 1), (-1, 2, 3), (0, 2, 5), (1, 2, 3), (2, 2, 1))),
    "stucki": (42, ((1, 0, 8), (2, 0, 4), (-2, 1, 2), (-1, 1, 4), (0, 1, 8), (1, 1, 4), (2, 1, 2),
                    (-2, 2, 1), (-1, 2, 2), (0, 2, 4), (1, 2, 2), (2, 2, 1))),
    # Atkinson ne diffuse que 6/8 de l'erreur (hautes lumières et ombres plus franches).
    "atkinson": (8, ((1, 0, 1), (2, 0, 1), (-1, 1, 1), (0, 1, 1), (1, 1, 1), (0, 2, 1))),
}


def bayer(n):
    """Matrice de Bayer n×n (n puissance de 2) : M(2n) = [[4M, 4M+2], [4M+3, 4M+1]], à plat ligne par ligne."""
    if n == 1:
        return [0]
    demi = n // 2
    m = bayer(demi)
    r = [0] * (n * n)
    for y in range(n):
        for x in range(n):
            base = 4 * m[(y % demi) * demi + (x % demi)]
            r[y * n + x] = base + (0 if y < demi and x < demi else 2 if y < demi else 3 if x < demi else 1)
    return r


BAYER8 = np.array(bayer(8), dtype=np.int64).reshape(8, 8)


def _diffusion(source, methode, progress=None):
    """Diffusion d'erreur, balayage gauche → droite. Chaque opération est arrondie en float32 comme en Kotlin
    (array('f') arrondit à l'écriture ; un calcul double suivi d'un arrondi float32 est exact pour + − × ÷)."""
    diviseur, points = NOYAUX[methode]
    div = float(diviseur)
    h, l = source.shape
    v = source.astype(F32, copy=True)
    sortie = np.empty((h, l), dtype=np.uint8)
    en_ligne = [(dx, float(w)) for dx, dy, w in points if dy == 0]
    # Rangées suivantes : à chaque cible, les contributions arrivent des sources de x croissant (dx décroissant).
    dessous = {dy: sorted([(dx, F32(w)) for dx, ddy, w in points if ddy == dy], key=lambda t: -t[0]) for dy in (1, 2)}
    tampon = array("f", [0.0])
    (d1, w1), (d2, w2) = (en_ligne + [(0, 0.0)])[:2]
    deux = len(en_ligne) > 1
    for y in range(h):
        if progress and y % 64 == 0:
            progress(y / h)
        rang = array("f", v[y].tobytes())
        err = array("f", bytes(4 * l))
        o = bytearray(l)
        for x in range(l):
            a = rang[x]
            if a < 0.5:
                e = a
            else:
                o[x] = 255
                tampon[0] = a - 1.0
                e = tampon[0]
            if e == 0.0:
                continue
            err[x] = e
            n1 = x + d1
            if n1 < l:
                tampon[0] = e * w1
                tampon[0] = tampon[0] / div
                rang[n1] = rang[n1] + tampon[0]
            if deux:
                n2 = x + d2
                if n2 < l:
                    tampon[0] = e * w2
                    tampon[0] = tampon[0] / div
                    rang[n2] = rang[n2] + tampon[0]
        sortie[y] = np.frombuffer(bytes(o), dtype=np.uint8)
        e = np.frombuffer(err.tobytes(), dtype=F32)
        for dy in (1, 2):
            if y + dy >= h:
                continue
            cible = v[y + dy]
            for dx, w in dessous[dy]:
                c = (e * w) / F32(div)
                if dx >= 0:
                    cible[dx:] += c[:l - dx]
                else:
                    cible[:l + dx] += c[-dx:]
    return sortie


def tramer(source, methode, progress=None):
    """Entrée : gris float32 0 (noir = le laser brûle) .. 1 (blanc). Sortie uint8 : 0 ou 255 ; « niveaux » garde 256 niveaux."""
    if methode in NOYAUX:
        return _diffusion(source, methode, progress)
    if methode == "bayer":
        h, l = source.shape
        seuil = (BAYER8.astype(F32) + F32(0.5)) / F32(64)
        s = np.tile(seuil, (h // 8 + 1, l // 8 + 1))[:h, :l]
        return np.where(source >= s, 255, 0).astype(np.uint8)
    if methode == "seuil":
        return np.where(source >= F32(0.5), 255, 0).astype(np.uint8)
    if methode == "niveaux":
        return (np.clip(source, F32(0), F32(1)) * F32(255) + F32(0.5)).astype(np.int64).astype(np.uint8)
    raise ValueError("Méthode de tramage inconnue.")


def part_noire(pixels):
    return float(np.count_nonzero(pixels < 128)) / pixels.size


# --- Gravure ---------------------------------------------------------------------------------------------------------

def taille_px(largeur_mm, px_par_mm, image):
    h_img, l_img = image.shape
    hauteur_mm = largeur_mm * h_img / l_img
    return max(1, int(math.floor(largeur_mm * px_par_mm + 0.5))), max(1, int(math.floor(hauteur_mm * px_par_mm + 0.5)))


def preparer(image, largeur_mm, px_par_mm, reglages, methode, progress=None):
    """Gravure.preparer : image à la taille réelle, réglée et tramée → dict (pixels, tailles px et mm, résolution)."""
    from .image import appliquer, redimensionner
    if not 5.0 <= largeur_mm <= 600.0:
        raise ValueError("Largeur de 5 à 600 mm.")
    if px_par_mm not in RESOLUTIONS:
        raise ValueError("Résolution : 10, 12,7 ou 20 px/mm.")
    l, h = taille_px(largeur_mm, px_par_mm, image)
    if l * h > PIXELS_MAX:
        raise ValueError(f"Image trop grande ({l} × {h} px) : réduis la taille ou la résolution.")
    redim = redimensionner(image, l, h)
    if progress:
        progress(0.2)
    reglee = appliquer(redim, luminosite=reglages["luminosite"], contraste=reglages["contraste"], gamma=reglages["gamma"],
                       nettete_k=reglages["nettete"], negatif=reglages["negatif"])
    if progress:
        progress(0.35)
    pixels = tramer(reglee, methode, (lambda f: progress(0.35 + 0.6 * f)) if progress else None)
    return {"largeur_px": l, "hauteur_px": h, "pixels": pixels, "largeur_mm": largeur_mm, "hauteur_mm": h / px_par_mm,
            "px_par_mm": px_par_mm}


# --- PNG avec résolution physique ------------------------------------------------------------------------------------

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
    lignes[:, 0] = 0  # filtre « aucun » : idéal pour une trame noir/blanc
    lignes[:, 1:] = pixels
    return (b"\x89PNG\r\n\x1a\n"
            + _bloc("IHDR", struct.pack(">IIBBBBB", l, h, 8, 0, 0, 0, 0))
            + _bloc("pHYs", struct.pack(">IIB", ppm, ppm, 1))
            + _bloc("tEXt", b"Software\x00Alpine Makers - Alpine Tools")
            + _bloc("IDAT", zlib.compress(lignes.tobytes(), 6))
            + _bloc("IEND", b""))


# --- Projet Alpine Laser Studio --------------------------------------------------------------------------------------

def _arrondi(v, decimales):
    f = 10.0 ** decimales
    x = v * f
    n = math.floor(x)                      # roundToLong : demi vers le haut, sans erreur d'addition
    return (n + (1 if x - n >= 0.5 else 0)) / f


DECOUPE = "#ff0000"


def projet(nom, r, png, materiau, methode=None, ident="atool-puzzle", nom_calque=None, decoupes=(), fermes=None,
           outil="atool-puzzle-photo"):
    """Texte JSON du projet .alsproj (contrat de LaserStudio.kt) : image à la taille réelle, calque #000000 en mode
    image « Relais » (pass_through : la trame est gravée telle quelle), projet portable (ni machine ni optimizer).
    ``decoupes`` : polylignes en mm dans le repère de l'image (origine en haut à gauche, y vers le bas) posées sur le
    calque rouge #ff0000 en mode Ligne (découpe), dans la boîte de l'image ; ``fermes`` : drapeau ``closed`` de chacune
    (toutes fermées par défaut, comme LaserStudio.projet ; Puzzle.projet ouvre les lignes communes)."""
    if len(png) < 8 or png[:2] != b"\x89P":
        raise ValueError("PNG attendu.")
    titre = (nom.strip() or "Gravure photo")[:60]
    image = {"id": ident, "type": "image", "name": titre, "x": MARGE_MM, "y": MARGE_MM,
             "w": _arrondi(r["largeur_mm"], 3), "h": _arrondi(r["hauteur_mm"], 3), "rotation": 0,
             "src": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
             "rasterMode": "dither", "invert": False, "layer": COULEUR}
    calque = {"name": (nom_calque or f"Photo {materiau} (Alpine Tools)")[:60], "color": COULEUR, "mode": "image",
              # Relais : image envoyée telle quelle (déjà tramée et, si besoin, déjà en négatif).
              "pass_through": True, "image_mode": "threshold", "negative": False,
              "interval_mm": _arrondi(1.0 / r["px_par_mm"], 4), "overscan_pct": 2.5, "bidirectional": True,
              "enabled": True, "visible": True, "auto_optimize": True}
    objets = [image]
    calques = {COULEUR: calque}
    gardees = [(k, t) for k, t in enumerate(decoupes) if len(t) >= 2]
    if gardees:
        lm, hm = r["largeur_mm"], r["hauteur_mm"]
        chemins = [{"closed": True if fermes is None else bool(fermes[k]),
                    "points": [[_arrondi(px / lm, 6), _arrondi(py / hm, 6)] for px, py in t]} for k, t in gardees]
        objets.append({"id": f"{ident}-decoupe", "type": "path", "name": "Découpe", "x": MARGE_MM, "y": MARGE_MM,
                       "w": _arrondi(lm, 3), "h": _arrondi(hm, 3), "rotation": 0, "layer": DECOUPE, "paths": chemins})
        calques[DECOUPE] = {"name": "Découpe (Alpine Tools)", "color": DECOUPE, "mode": "line", "line_operation": "cut",
                            "enabled": True, "visible": True, "auto_optimize": True}
    source = {"app": "alpine-tools", "outil": outil, "materiau": materiau, "px_par_mm": r["px_par_mm"],
              "largeur_px": r["largeur_px"], "hauteur_px": r["hauteur_px"]}
    if methode:
        source["methode"] = methode
    document = {"version": 1, "app": "alpine-laser-studio", "name": titre, "objects": objets,
                "layers": calques, "activeLayer": COULEUR, "order": "layer", "offset": [0, 0],
                "source": source}
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


# --- Aperçu sur le matériau ------------------------------------------------------------------------------------------

class _JavaRandom:
    """java.util.Random (même suite que l'application pour la texture du bois)."""

    def __init__(self, graine):
        self.s = (graine ^ 0x5DEECE66D) & ((1 << 48) - 1)

    def _suivant(self, bits):
        self.s = (self.s * 0x5DEECE66D + 0xB) & ((1 << 48) - 1)
        return self.s >> (48 - bits)

    def next_int(self, borne):
        if borne & -borne == borne:
            return (borne * self._suivant(31)) >> 31
        while True:
            bits = self._suivant(31)
            val = bits % borne
            if bits - val + (borne - 1) < (1 << 31):
                return val

    def next_float(self):
        return self._suivant(24) / float(1 << 24)


def _rvb(c):
    return np.array([(c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF], dtype=np.float64)


def rendu_materiau(pixels, materiau):
    """Rendu.gravure : pixel noir → trace du laser, blanc → matière ; fibres légères sur le bois. Image RGB Pillow."""
    from PIL import Image, ImageDraw
    m = MATERIAUX[materiau]
    h, l = pixels.shape
    t = (F32(1) - pixels.astype(F32) / F32(255)).astype(np.float64)[..., None]  # 1 = brûlé
    px = np.floor(_rvb(m["matiere"]) * (1 - t) + _rvb(m["trace"]) * t + 0.5).clip(0, 255).astype(np.uint8)
    image = Image.fromarray(px, "RGB").convert("RGBA")
    if materiau in ("bois_clair", "bois_fonce"):
        calque = Image.new("RGBA", image.size, (0, 0, 0, 0))
        trait = ImageDraw.Draw(calque)
        r = _JavaRandom(4)
        epaisseur = max(1, round(l / 400))
        for k in range(0, h, max(2, h // 90)):
            alpha = 8 + r.next_int(18)
            y0 = k + r.next_float() * 3
            y1 = k + r.next_float() * 6
            trait.line([(0, y0), (l, y1)], fill=(0, 0, 0, alpha), width=epaisseur)
        image.alpha_composite(calque)
    return image.convert("RGB")


def apercu_trame(pixels, materiau, largeur_max=900, hauteur_max=1200):
    """apercuTrame : moyenne par blocs (au-delà de 900 px de large) puis teinte du matériau."""
    from .image import redimensionner
    h, l = pixels.shape
    cible = min(largeur_max, int(hauteur_max * l / h))
    if l <= cible:
        return rendu_materiau(pixels, materiau)
    h2 = max(1, int(math.floor(cible * h / l + 0.5)))
    r = redimensionner(pixels.astype(F32) / F32(255), cible, h2)
    return rendu_materiau(np.clip(np.floor((r * F32(255)).astype(np.float64) + 0.5), 0, 255).astype(np.uint8), materiau)
