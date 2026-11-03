"""Feuille de repères A4 d'Alpine Chutes en PDF vectoriel (port de Pdf.kt) : page 210 × 297 mm exacts (impression à
l'échelle demandée au lecteur par /PrintScaling /None), 4 repères aux coins, cadre pointillé de la zone utile, consignes
et règle de contrôle de 100 mm. Polices standard du PDF (Helvetica, WinAnsi), non incorporées : aucune police tierce.
"""
from .geometrie import arrondi
from .image import Reperes

PT = 72.0 / 25.4


def _n(v):
    t = format(arrondi(v, 3), "f")
    return t.rstrip("0").rstrip(".")


def _x(mm):
    return _n(mm * PT)


def _y(mm):
    return _n((297.0 - mm) * PT)


def _texte(t):
    """Texte PDF littéral (parenthèses et barres obliques échappées), encodé WinAnsi."""
    o = bytearray(b"(")
    for c in t.encode("cp1252"):
        if c in (0x28, 0x29, 0x5C):
            o.append(0x5C)
        o.append(c)
    o.append(0x29)
    return bytes(o)


def contenu_feuille():
    s = bytearray()

    def w(t):
        s.extend(t.encode("ascii"))

    def rect(x0, y0, l, h):
        w(f"{_x(x0)} {_y(y0 + h)} {_n(l * PT)} {_n(h * PT)} re f\n")

    m = Reperes.MODULE
    for cx, cy in Reperes.centres:
        w("0 g\n")
        rect(cx - 3.5 * m, cy - 3.5 * m, 7 * m, 7 * m)
        w("1 g\n")
        rect(cx - 2.5 * m, cy - 2.5 * m, 5 * m, 5 * m)
        w("0 g\n")
        rect(cx - 1.5 * m, cy - 1.5 * m, 3 * m, 3 * m)
    cx0, cy0 = Reperes.CADRE_X0, Reperes.CADRE_Y0
    w(f"q 0.62 G 0.6 w [3 3] 0 d {_x(cx0)} {_y(297.0 - cy0)} {_n((210.0 - 2 * cx0) * PT)} {_n((297.0 - 2 * cy0) * PT)} re S Q\n")

    def ligne(police, taille, xmm, ymm, t):
        w(f"BT /{police} {_n(taille)} Tf {_x(xmm)} {_y(ymm)} Td ")
        s.extend(_texte(t))
        w(" Tj ET\n")

    w("0.1 g\n")
    ligne("F2", 12.0, 30.0, 12.0, "ALPINE CHUTES - FEUILLE DE REPÈRES A4")
    w("0.25 g\n")
    ligne("F1", 7.6, 30.0, 18.0, "Imprime à 100 % (taille réelle, sans « ajuster à la page »), sur papier blanc mat.")
    ligne("F1", 7.6, 30.0, 22.0, "Petite chute : pose-la dans le cadre pointillé. Grande chute : pose la feuille à plat SUR la chute.")
    ligne("F1", 7.6, 30.0, 26.0, "Photographie bien de dessus, les quatre repères et toute la chute visibles, sans ombre forte.")
    r0, ry = 55.0, 279.0
    w(f"0.1 G 0.5 w {_x(r0)} {_y(ry)} m {_x(r0 + 100.0)} {_y(ry)} l S\n")
    for k in range(21):
        xx = r0 + k * 5.0
        hauteur = 3.0 if k % 2 == 0 else 1.8
        w(f"{_x(xx)} {_y(ry)} m {_x(xx)} {_y(ry - hauteur)} l S\n")
    w("0.1 g\n")
    ligne("F1", 6.5, r0 - 0.9, ry + 3.2, "0")
    ligne("F1", 6.5, r0 + 100.0 - 4.2, ry + 3.2, "100 mm")
    w("0.25 g\n")
    ligne("F1", 7.0, r0, ry + 7.5, "Contrôle : cette règle doit mesurer exactement 100 mm.")
    ligne("F1", 7.0, r0, ry + 11.0, "Sinon, réimprime en taille réelle. alpine-makers.ch")
    return bytes(s)


def feuille_reperes():
    sortie = bytearray()
    decalages = []

    def w(t):
        sortie.extend(t.encode("latin-1"))

    def objet(num, corps):
        decalages.append(len(sortie))
        w(f"{num} 0 obj\n")
        corps()
        w("\nendobj\n")

    w("%PDF-1.6\n%âãÏÓ\n")
    objet(1, lambda: w("<< /Type /Catalog /Pages 2 0 R /ViewerPreferences << /PrintScaling /None >> >>"))
    objet(2, lambda: w("<< /Type /Pages /Kids [3 0 R] /Count 1 >>"))
    objet(3, lambda: w(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_n(210.0 * PT)} {_n(297.0 * PT)}] "
                       "/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>"))
    contenu = contenu_feuille()

    def flux():
        w(f"<< /Length {len(contenu)} >>\nstream\n")
        sortie.extend(contenu)
        w("endstream")

    objet(4, flux)
    objet(5, lambda: w("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"))
    objet(6, lambda: w("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"))

    def info():
        w("<< /Title ")
        sortie.extend(_texte("Alpine Chutes - feuille de repères A4"))
        w(" /Producer (Alpine Chutes) >>")

    objet(7, info)
    xref = len(sortie)
    w("xref\n0 8\n0000000000 65535 f \n")
    for d in decalages:
        w(f"{d:010d} 00000 n \n")
    w(f"trailer\n<< /Size 8 /Root 1 0 R /Info 7 0 R >>\nstartxref\n{xref}\n%%EOF\n")
    return bytes(sortie)
