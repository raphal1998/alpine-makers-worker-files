"""Exports (port d'Exports.kt et Pdf.kt) : SVG en mm (découpe laser), DXF R12 (CNC, CAO), feuille de repères PDF."""
from .geometrie import arrondi, boite
from .image import Reperes


def nombre(v, decimales=3):
    t = format(arrondi(v, decimales), "f")
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return "0" if t == "-0" else t


class Calque:
    def __init__(self, nom, boucles, couleur_svg="#FF0000", couleur_dxf=1):
        self.nom, self.boucles, self.couleur_svg, self.couleur_dxf = nom, boucles, couleur_svg, couleur_dxf


def _chemin(boucles, min_x, max_y):
    morceaux = []
    for b in boucles:
        for k, (x, y) in enumerate(zip(b.x.tolist(), b.y.tolist())):
            morceaux.append(("M" if k == 0 else "L") + nombre(x - min_x) + "," + nombre(max_y - y))
        morceaux.append("Z")
    return "".join(morceaux)


def svg(calques, titre, marge=2.0):
    """SVG : width/height et viewBox en mm (1 unité = 1 mm), traits rouges de 0,1 mm, un groupe par calque."""
    tout = [b for c in calques for b in c.boucles]
    if not tout:
        raise ValueError("Aucun contour à exporter.")
    b = boite(tout)
    min_x, max_y = b[0] - marge, b[3] + marge
    l, h = b[2] - b[0] + 2 * marge, b[3] - b[1] + 2 * marge
    propre = titre.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    s = ['<?xml version="1.0" encoding="UTF-8"?>\n',
         f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" width="{nombre(l)}mm" height="{nombre(h)}mm" viewBox="0 0 {nombre(l)} {nombre(h)}">\n',
         f"  <title>{propre} — Alpine Contour</title>\n",
         "  <desc>Contours en millimètres (1 unité = 1 mm). Découpe : trait rouge de 0,1 mm, sans remplissage.</desc>\n"]
    for c in calques:
        if c.boucles:
            s.append(f'  <g id="{c.nom}" fill="none" stroke="{c.couleur_svg}" stroke-width="0.1" stroke-linejoin="round">\n')
            s.append(f'    <path fill-rule="evenodd" d="{_chemin(c.boucles, min_x, max_y)}"/>\n')
            s.append("  </g>\n")
    s.append("</svg>\n")
    return "".join(s)


def dxf(calques, marge=0.0):
    """DXF ASCII R12 (AC1009) en mm : une POLYLINE fermée (70 = 1) par boucle, sommets VERTEX, calques nommés."""
    tout = [b for c in calques for b in c.boucles]
    if not tout:
        raise ValueError("Aucun contour à exporter.")
    b = boite(tout)
    ox, oy = b[0] - marge, b[1] - marge
    s = []

    def g(code, valeur):
        s.append(str(code).rjust(3) + "\r\n" + valeur + "\r\n")

    g(0, "SECTION"); g(2, "HEADER")
    g(9, "$ACADVER"); g(1, "AC1009")
    g(9, "$INSUNITS"); g(70, "4")
    g(9, "$EXTMIN"); g(10, nombre(0.0)); g(20, nombre(0.0)); g(30, "0")
    g(9, "$EXTMAX"); g(10, nombre(b[2] - ox + marge)); g(20, nombre(b[3] - oy + marge)); g(30, "0")
    g(0, "ENDSEC")
    g(0, "SECTION"); g(2, "TABLES")
    g(0, "TABLE"); g(2, "LTYPE"); g(70, "1")
    g(0, "LTYPE"); g(2, "CONTINUOUS"); g(70, "0"); g(3, "Solid line"); g(72, "65"); g(73, "0"); g(40, "0.0")
    g(0, "ENDTAB")
    g(0, "TABLE"); g(2, "LAYER"); g(70, str(len(calques)))
    for c in calques:
        g(0, "LAYER"); g(2, c.nom); g(70, "0"); g(62, str(c.couleur_dxf)); g(6, "CONTINUOUS")
    g(0, "ENDTAB")
    g(0, "ENDSEC")
    g(0, "SECTION"); g(2, "ENTITIES")
    for c in calques:
        for boucle in c.boucles:
            g(0, "POLYLINE"); g(8, c.nom); g(66, "1"); g(10, "0.0"); g(20, "0.0"); g(30, "0.0"); g(70, "1")
            for x, y in zip(boucle.x.tolist(), boucle.y.tolist()):
                g(0, "VERTEX"); g(8, c.nom); g(10, nombre(x - ox, 4)); g(20, nombre(y - oy, 4)); g(30, "0.0")
            g(0, "SEQEND"); g(8, c.nom)
    g(0, "ENDSEC")
    g(0, "EOF")
    return "".join(s)


# ------------------------------------------------------------------ feuille de repères A4 (Pdf.kt)

PT = 72.0 / 25.4


def _n(v):
    return nombre(v, 3)


def _x(v):
    return _n(v * PT)


def _y(v):
    return _n((297.0 - v) * PT)


def _texte(t):
    o = bytearray(b"(")
    for c in t.encode("cp1252", errors="replace"):
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

    M = Reperes.MODULE
    for cx, cy in Reperes.centres:
        w("0 g\n"); rect(cx - 3.5 * M, cy - 3.5 * M, 7 * M, 7 * M)
        w("1 g\n"); rect(cx - 2.5 * M, cy - 2.5 * M, 5 * M, 5 * M)
        w("0 g\n"); rect(cx - 1.5 * M, cy - 1.5 * M, 3 * M, 3 * M)
    cx0, cy0 = Reperes.CADRE_X0, Reperes.CADRE_Y0
    w(f"q 0.62 G 0.6 w [3 3] 0 d {_x(cx0)} {_y(297.0 - cy0)} {_n((210.0 - 2 * cx0) * PT)} {_n((297.0 - 2 * cy0) * PT)} re S Q\n")

    def ligne(police, taille, xmm, ymm, t):
        w(f"BT /{police} {_n(taille)} Tf {_x(xmm)} {_y(ymm)} Td ")
        s.extend(_texte(t))
        w(" Tj ET\n")

    w("0.1 g\n")
    ligne("F2", 12.0, 30.0, 12.0, "ALPINE CONTOUR - FEUILLE DE REPÈRES A4")
    w("0.25 g\n")
    ligne("F1", 7.6, 30.0, 18.0, "Imprime à 100 % (taille réelle, sans « ajuster à la page »), sur papier blanc mat.")
    ligne("F1", 7.6, 30.0, 22.0, "Pose la feuille sur un fond plus sombre et l'objet dans le cadre pointillé, sans le toucher.")
    ligne("F1", 7.6, 30.0, 26.0, "Photographie bien de dessus, les quatre repères visibles, sans ombre forte.")
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


def feuille_reperes_pdf():
    """PDF 1.6 vectoriel, page A4 exacte, /PrintScaling /None, polices standard Helvetica (WinAnsi)."""
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
        sortie.extend(_texte("Alpine Contour - feuille de repères A4"))
        w(" /Producer (Alpine Contour) >>")

    objet(7, info)
    xref = len(sortie)
    w("xref\n0 8\n0000000000 65535 f \n")
    for d in decalages:
        w(f"{d:010d} 00000 n \n")
    w(f"trailer\n<< /Size 8 /Root 1 0 R /Info 7 0 R >>\nstartxref\n{xref}\n%%EOF\n")
    return bytes(sortie)
