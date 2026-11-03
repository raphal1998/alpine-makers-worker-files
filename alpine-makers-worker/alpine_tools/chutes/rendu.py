"""Aperçu PNG du plan de découpe (≤ 1200 px) : la chute (photo redressée ou teinte de matière), son bord (bleu), les
zones interdites (gris), les pièces posées (rouge) et un cartouche écrit avec la police au trait d'Alpine Chutes.

Remplissages pair / impair par la rastérisation exacte du placement, dessin à 2 × puis réduction (bords adoucis).
"""
import numpy as np
from PIL import Image, ImageDraw

from . import police
from .placement import remplir

COTE_MAX = 1200
SUR = 2
CARTOUCHE = 64
FOND = np.array([30, 27, 24], dtype=np.float64)
MATIERE = np.array([196, 160, 118], dtype=np.float64)
PIECE = np.array([255, 92, 66], dtype=np.float64)
BORD = (70, 130, 255)
TRAIT = (232, 52, 36)
INTERDIT = (150, 150, 150)
ORIGINE = (40, 190, 90)


def reduire_image(img, cote):
    """Réduction lissée pour tenir dans ``cote`` px (photo source, aperçu)."""
    s = cote / max(img.size)
    if s >= 1:
        return img
    return img.resize((max(1, int(img.size[0] * s)), max(1, int(img.size[1] * s))), Image.Resampling.BILINEAR)


def _texte(draw, x, y, t, hauteur, couleur, epaisseur):
    """Texte au trait (police d'Alpine Chutes) en pixels, coin haut-gauche (x, y)."""
    try:
        traits, _ = police.traits(t, hauteur)
    except ValueError:
        return
    for tr in traits:
        pts = [(x + px, y + py) for px, py in tr]
        if len(pts) == 1:
            r = epaisseur / 2
            draw.ellipse([pts[0][0] - r, pts[0][1] - r, pts[0][0] + r, pts[0][1] + r], fill=couleur)
        else:
            draw.line(pts, fill=couleur, width=max(1, int(round(epaisseur))), joint="curve")


def apercu(chute, formes, resultat=None, lignes=(), origine=(0.0, 0.0)):
    """Image PIL RGB. ``formes`` : [(nom, boucles mm)] des pièces posées ; ``resultat`` : mesure par photo (image redressée
    de sa zone) ou None ; ``lignes`` : 1 ou 2 lignes du cartouche ; ``origine`` : point (0, 0) des exports (mm)."""
    b = chute.boite
    x0, y0, x1, y1 = b[0] - 6.0, b[1] - 6.0, b[2] + 6.0, b[3] + 6.0
    lmm, hmm = max(1.0, x1 - x0), max(1.0, y1 - y0)
    s = min(COTE_MAX / lmm, (COTE_MAX - CARTOUCHE) / hmm)
    W, H = max(2, int(lmm * s)), max(2, int(hmm * s))
    S = s * SUR
    WS, HS = W * SUR, H * SUR
    pas = 1.0 / S
    toile = np.empty((HS, WS, 3), dtype=np.float64)
    toile[:] = FOND
    if resultat is not None:
        z, ppm = resultat.zone, resultat.ppm
        # Pixels de l'image redressée sous chaque pixel de l'aperçu (plus proche voisin, image déjà au bon pas).
        cx = ((x0 + (np.arange(WS) + 0.5) * pas - z.x0) * ppm).astype(np.int64)
        cy = ((y0 + (np.arange(HS) + 0.5) * pas - z.y0) * ppm).astype(np.int64)
        okx = (cx >= 0) & (cx < resultat.image.shape[1])
        oky = (cy >= 0) & (cy < resultat.image.shape[0])
        sous = resultat.image[np.clip(cy, 0, resultat.image.shape[0] - 1)][:, np.clip(cx, 0, resultat.image.shape[1] - 1)]
        vu = oky[:, None] & okx[None, :]
        toile[vu] = sous[vu] * 0.55 + FOND * 0.45
    m_chute = np.zeros((HS, WS), dtype=bool)
    remplir(m_chute, x0, y0, pas, chute.bords)
    if resultat is not None:
        toile[m_chute & vu] = sous[m_chute & vu]
    else:
        toile[m_chute] = MATIERE
    m_pieces = np.zeros((HS, WS), dtype=bool)
    remplir(m_pieces, x0, y0, pas, [c for _, f in formes for c in f])
    toile[m_pieces] = toile[m_pieces] * 0.35 + PIECE * 0.65
    img = Image.fromarray(np.clip(toile, 0, 255).astype(np.uint8), "RGB")
    d = ImageDraw.Draw(img)

    def px(x, y):
        return ((x - x0) * S, (y - y0) * S)

    for t in chute.interdits:
        r = t.rayon_mm * S
        pts = [px(x, y) for x, y in t.points]
        if len(pts) >= 2:
            d.line(pts, fill=INTERDIT, width=max(1, int(round(2 * r))), joint="curve")
        for qx, qy in (pts[:1] + pts[-1:]):
            d.ellipse([qx - r, qy - r, qx + r, qy + r], fill=INTERDIT)
    epais = max(2, int(round(SUR * 1.6)))
    for c in chute.bords:
        pts = [px(x, y) for x, y in zip(c.x.tolist(), c.y.tolist())]
        d.line(pts + pts[:1], fill=BORD, width=epais, joint="curve")
    for _, f in formes:
        for c in f:
            pts = [px(x, y) for x, y in zip(c.x.tolist(), c.y.tolist())]
            d.line(pts + pts[:1], fill=TRAIT, width=epais, joint="curve")
    ox, oy = px(*origine)
    lg = 8.0 * S
    d.line([(ox + lg, oy), (ox, oy), (ox, oy + lg)], fill=ORIGINE, width=epais + 1, joint="curve")
    img = img.resize((W, H), Image.Resampling.LANCZOS)
    lignes = [l for l in lignes if l][:2]
    if not lignes:
        return img
    sortie = Image.new("RGB", (W, H + CARTOUCHE), (22, 20, 18))
    sortie.paste(img, (0, 0))
    dd = ImageDraw.Draw(sortie)
    for k, l in enumerate(lignes):
        hauteur = 15.0 if k == 0 else 11.0
        _texte(dd, 14, H + 12 + k * 26, l, hauteur, (240, 236, 228) if k == 0 else (190, 184, 175), 2.0 if k == 0 else 1.5)
    return sortie
