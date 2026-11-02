"""Rendus PNG (port de Rendu.kt) : aperçu ≤ 1200 px et PNG de contrôle à 5 px/mm.

Texte : police à traits dessinée ici (grille 4 × 6), aucune police tierce.
"""
import math
import unicodedata

import numpy as np
from PIL import Image, ImageDraw

from .geometrie import boite, decimal_fr

# Tracés des glyphes : polylignes séparées par « | », points « x,y » (x 0..4, y 0 en haut .. 6 sur la ligne de base).
_GLYPHES = {
    "A": "0,6 0,2 2,0 4,2 4,6|0,3.5 4,3.5", "B": "0,6 0,0 3,0 4,1 4,2 3,3 0,3|3,3 4,4 4,5 3,6 0,6",
    "C": "4,1 3,0 1,0 0,1 0,5 1,6 3,6 4,5", "D": "0,0 0,6 2.5,6 4,4.5 4,1.5 2.5,0 0,0", "E": "4,0 0,0 0,6 4,6|0,3 3,3",
    "F": "4,0 0,0 0,6|0,3 3,3", "G": "4,1 3,0 1,0 0,1 0,5 1,6 3,6 4,5 4,3.5 2.5,3.5", "H": "0,0 0,6|4,0 4,6|0,3 4,3",
    "I": "1,0 3,0|2,0 2,6|1,6 3,6", "J": "4,0 4,5 3,6 1,6 0,5", "K": "0,0 0,6|4,0 0,3.5|1.3,2.7 4,6", "L": "0,0 0,6 4,6",
    "M": "0,6 0,0 2,3 4,0 4,6", "N": "0,6 0,0 4,6 4,0", "O": "1,0 3,0 4,1 4,5 3,6 1,6 0,5 0,1 1,0",
    "P": "0,6 0,0 3,0 4,1 4,2 3,3 0,3", "Q": "1,0 3,0 4,1 4,5 3,6 1,6 0,5 0,1 1,0|2.5,4.5 4,6.3",
    "R": "0,6 0,0 3,0 4,1 4,2 3,3 0,3|2,3 4,6", "S": "4,1 3,0 1,0 0,1 0,2 1,3 3,3 4,4 4,5 3,6 1,6 0,5",
    "T": "0,0 4,0|2,0 2,6", "U": "0,0 0,5 1,6 3,6 4,5 4,0", "V": "0,0 2,6 4,0", "W": "0,0 1,6 2,3 3,6 4,0",
    "X": "0,0 4,6|4,0 0,6", "Y": "0,0 2,3 4,0|2,3 2,6", "Z": "0,0 4,0 0,6 4,6",
    "0": "1,0 3,0 4,1 4,5 3,6 1,6 0,5 0,1 1,0", "1": "1,1 2,0 2,6|1,6 3,6", "2": "0,1 1,0 3,0 4,1 4,2 0,6 4,6",
    "3": "0,1 1,0 3,0 4,1 4,2 3,3 4,4 4,5 3,6 1,6 0,5|1.5,3 3,3", "4": "3,6 3,0 0,4 4,4", "5": "4,0 0,0 0,3 3,3 4,4 4,5 3,6 0,6",
    "6": "3,0 1,0 0,1 0,5 1,6 3,6 4,5 4,4 3,3 0,3", "7": "0,0 4,0 1.5,6", "8": "1,0 3,0 4,1 4,2 3,3 1,3 0,2 0,1 1,0|1,3 0,4 0,5 1,6 3,6 4,5 4,4 3,3",
    "9": "4,3 1,3 0,2 0,1 1,0 3,0 4,1 4,5 3,6 1,6", ".": "1.8,5.6 2.2,5.6 2.2,6 1.8,6 1.8,5.6", ",": "2.2,5.4 2.2,6 1.6,7",
    ":": "2,1.8 2,2.2|2,5.6 2,6", "-": "0.8,3 3.2,3", "+": "0.5,3 3.5,3|2,1.5 2,4.5", "×": "0.8,1.8 3.2,4.2|3.2,1.8 0.8,4.2",
    "/": "0.5,6 3.5,0", "(": "2.8,-0.3 1.5,1 1.2,3 1.5,5 2.8,6.3", ")": "1.2,-0.3 2.5,1 2.8,3 2.5,5 1.2,6.3",
    "'": "2,0 2,1.5", "%": "0,6 4,0|0.5,0.5 1,0.5|3,5.5 3.5,5.5", "=": "0.5,2.2 3.5,2.2|0.5,3.8 3.5,3.8",
    "²": "1,0.4 1.5,0 2.5,0 3,0.4 3,0.9 1,2.4 3,2.4", "³": "1,0.3 1.5,0 2.5,0 3,0.4 2.4,1.2 3,1.8 2.6,2.4 1.4,2.4 1,2.1",
    "Ø": "1,0 3,0 4,1 4,5 3,6 1,6 0,5 0,1 1,0|-0.3,6.5 4.3,-0.5", "·": "1.8,2.8 2.2,2.8 2.2,3.2 1.8,3.2 1.8,2.8",
    "«": "2,1.5 0.5,3 2,4.5|3.5,1.5 2,3 3.5,4.5", "»": "0.5,1.5 2,3 0.5,4.5|2,1.5 3.5,3 2,4.5", "?": "0,1 1,0 3,0 4,1 4,2 2,3.5 2,4.5|2,5.7 2,6",
    "!": "2,0 2,4.2|2,5.7 2,6", "#": "1,0.5 0.7,5.5|3.3,0.5 3,5.5|0,2 4,2|0,4 4,4", "&": "4,6 0.5,2 0.5,1 1.5,0 2.5,0 3,1 0,4.5 0,5 1,6 2.5,6 4,3.5",
}
_GLYPHES = {k: [[tuple(float(c) for c in p.split(",")) for p in trait.split()] for trait in v.split("|")] for k, v in _GLYPHES.items()}
_ETROITS = {".": 2.4, ",": 2.4, ":": 2.4, "'": 2.4, "!": 2.4, "I": 4.0, " ": 3.4, "1": 4.4}


def _normaliser(texte):
    sortie = []
    for c in texte:
        if c in _GLYPHES or c == " ":
            sortie.append(c)
            continue
        base = "".join(x for x in unicodedata.normalize("NFD", c) if not unicodedata.combining(x)).upper()
        if base in ("Œ", "Æ"):
            sortie.append("OE" if base == "Œ" else "AE")
        elif base == " " or base == "\xa0":
            sortie.append(" ")
        else:
            sortie.append(base if all(x in _GLYPHES for x in base) and base else "?")
    return "".join(sortie)


def largeur_texte(texte, taille):
    e = taille / 6.0
    return sum(_ETROITS.get(c, 5.2) * e for c in _normaliser(texte))


def ecrire(draw, x, y, texte, taille, couleur, epaisseur=None):
    """Texte à traits, coin haut-gauche (x, y), hauteur des capitales ``taille`` px."""
    e = taille / 6.0
    w = epaisseur or max(1, int(round(taille / 7)))
    for c in _normaliser(texte):
        for trait in _GLYPHES.get(c, []):
            pts = [(x + px * e, y + py * e) for px, py in trait]
            if len(pts) >= 2:
                draw.line(pts, fill=couleur, width=w, joint="curve")
        x += _ETROITS.get(c, 5.2) * e
    return x


def _polys(boucles, vx, vy):
    return [[(vx(px), vy(py)) for px, py in zip(b.x.tolist(), b.y.tolist())] for b in boucles if b.n >= 2]


def apercu(redressee, ppm_image, trace, masque, cote_max=1200):
    """Feuille redressée (≤ 1200 px), contours retenus en rouge, objets écartés en gris, numéros et cotes."""
    h_img, l_img = redressee.shape[:2]
    s = min(1.0, cote_max / max(h_img, l_img))
    L, H = max(1, int(l_img * s)), max(1, int(h_img * s))
    base = Image.fromarray(redressee).resize((L, H), Image.Resampling.BILINEAR)
    fond = Image.blend(base, Image.new("RGB", (L, H), (255, 255, 255)), 0.25)
    ech = ppm_image * s  # px/mm de l'aperçu
    img = fond.convert("RGBA")
    calque = Image.new("RGBA", (L, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(calque)
    hm = trace.hauteur_mm

    def vx(x):
        return x * ech

    def vy(y):
        return (hm - y) * ech

    retenus = {o.objet.id for o in trace.objets}
    for o in masque.objets:
        if o.id in retenus:
            continue
        x0, y0, x1, y1 = (v * ech for v in o.boite)
        d.rectangle([x0, y0, x1, y1], outline=(110, 120, 135, 230), width=2)
        ecrire(d, x0 + 3, y0 + 3, str(o.id), 14, (90, 100, 115, 255), 2)
    for o in trace.objets:
        for p in _polys(o.boucles, vx, vy):
            d.polygon(p, fill=(229, 38, 42, 40))
        for p in _polys(o.boucles, vx, vy):
            d.line(p + [p[0]], fill=(255, 255, 255, 170), width=5, joint="curve")
            d.line(p + [p[0]], fill=(229, 38, 42, 255), width=2, joint="curve")
    for o in trace.objets:
        b = boite(o.boucles)
        etiquette = f"{o.objet.id} : {decimal_fr(o.cotes.largeur)} × {decimal_fr(o.cotes.hauteur)} mm"
        tx, ty = vx(b[0]), vy(b[3]) - 22
        if ty < 2:
            ty = vy(b[1]) + 6
        lw = largeur_texte(etiquette, 13)
        d.rectangle([tx - 3, ty - 3, tx + lw + 3, ty + 17], fill=(255, 255, 255, 210))
        ecrire(d, tx, ty, etiquette, 13, (11, 21, 34, 255), 2)
    # Échelle de 10 mm.
    rx, ry = 12, H - 16
    d.line([(rx, ry), (rx + 10 * ech, ry)], fill=(255, 140, 0, 255), width=3)
    d.line([(rx, ry - 5), (rx, ry + 5)], fill=(255, 140, 0, 255), width=2)
    d.line([(rx + 10 * ech, ry - 5), (rx + 10 * ech, ry + 5)], fill=(255, 140, 0, 255), width=2)
    ecrire(d, rx + 10 * ech + 6, ry - 6, "10 mm", 12, (200, 100, 0, 255), 2)
    return Image.alpha_composite(img, calque).convert("RGB")


def controle(redressee, ppm_image, trace, titre, lignes, ppm=5.0):
    """PNG de contrôle : feuille à 5 px/mm, contours rouges, cotes, échelle de 10 mm et cartouche."""
    s = ppm
    l, h = int(math.floor(trace.largeur_mm * ppm + 0.5)), int(math.floor(trace.hauteur_mm * ppm + 0.5))
    cartouche = int(math.floor(34 * ppm + 0.5))
    img = Image.new("RGB", (l, h + cartouche), (255, 255, 255))
    hi, li = redressee.shape[:2]
    feuille = Image.fromarray(redressee).resize((l, max(1, int(round(hi / ppm_image * ppm)))), Image.Resampling.BILINEAR)
    img.paste(feuille, (0, 0))
    d = ImageDraw.Draw(img, "RGBA")

    def vx(x):
        return x * s

    def vy(y):
        return (trace.hauteur_mm - y) * s

    polys = _polys(trace.boucles, vx, vy)
    for p in polys:
        d.line(p + [p[0]], fill=(255, 255, 255, 85), width=4, joint="curve")
    for p in polys:
        d.line(p + [p[0]], fill=(229, 38, 42, 255), width=2, joint="curve")
    taille = 3.2 * s
    for o in trace.objets:
        b = boite(o.boucles)
        etiquette = f"{o.objet.id} : {decimal_fr(o.cotes.largeur)} × {decimal_fr(o.cotes.hauteur)} mm"
        x, y = vx(b[0]), vy(b[3]) - 1.5 * s - taille
        w = largeur_texte(etiquette, taille)
        d.rectangle([x - 0.5 * s, y - 0.4 * s, x + w + 0.5 * s, y + taille + 0.8 * s], fill=(255, 255, 255, 204))
        ecrire(d, x, y, etiquette, taille, (11, 21, 34), 2)
    haut = h
    d.rectangle([0, haut, l, haut + cartouche], fill=(11, 21, 34))
    ecrire(d, 6 * s, haut + 4 * s, titre[:48], 4 * s, (233, 242, 250), 3)
    for k, ligne in enumerate(lignes[:5]):
        ecrire(d, 6 * s, haut + (10.8 + 4.2 * k) * s, ligne[:90], 2.6 * s, (233, 242, 250), 1)
    rx, ry = l - 26 * s, haut + 9 * s
    d.line([(rx, ry), (rx + 10 * s, ry)], fill=(255, 181, 71), width=max(1, int(0.6 * s)))
    d.line([(rx, ry - 1.5 * s), (rx, ry + 1.5 * s)], fill=(255, 181, 71), width=2)
    d.line([(rx + 10 * s, ry - 1.5 * s), (rx + 10 * s, ry + 1.5 * s)], fill=(255, 181, 71), width=2)
    ecrire(d, rx + 11.5 * s, ry - 1.6 * s, "10 mm", 3.2 * s, (255, 181, 71), 2)
    pied = f"{decimal_fr(ppm, 0)} px/mm · Alpine Contour"
    ecrire(d, l - 4 * s - largeur_texte(pied, 2.6 * s), ry + 4 * s, pied, 2.6 * s, (157, 178, 200), 1)
    # Bord de la feuille (repère pour l'œil), pointillé.
    for x0 in range(0, l, 12):
        d.line([(x0, 1), (min(l, x0 + 6), 1)], fill=(63, 160, 192, 85))
        d.line([(x0, haut - 2), (min(l, x0 + 6), haut - 2)], fill=(63, 160, 192, 85))
    for y0 in range(0, haut, 12):
        d.line([(1, y0), (1, min(haut, y0 + 6))], fill=(63, 160, 192, 85))
        d.line([(l - 2, y0), (l - 2, min(haut, y0 + 6))], fill=(63, 160, 192, 85))
    return img


def reduire_image(img, cote):
    """Réduction lissée pour tenir dans ``cote`` px (photo source, aperçu)."""
    s = cote / max(img.size)
    if s >= 1:
        return img
    return img.resize((max(1, int(img.size[0] * s)), max(1, int(img.size[1] * s))), Image.Resampling.BILINEAR)


def tableau(img):
    return np.asarray(img.convert("RGB"), dtype=np.uint8)
