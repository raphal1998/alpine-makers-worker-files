"""Fichiers de la lithophanie en volume : STL binaire (Gift StlBinaire, écrit en flux), 3MF d'une pièce blanche
(Gift TroisMfPieces) et OBJ + MTL dans un ZIP (Gift ExportModele.objCouleurs).

3MF et OBJ passent par un maillage indexé (Gift Maillage : sommets de mêmes coordonnées float32 fusionnés, dans
l'ordre de première apparition ; triangles dégénérés écartés), borné à 900 000 triangles comme dans l'application.
Mêmes coordonnées (mm) dans tous les formats. Copie propre à cet outil (base : tableau_couleur/exports.py).
"""
import re
import zipfile

import numpy as np

F32 = np.float32
SIGNATURE = "Alpine Makers"
APPLICATION = "Alpine Makers — Lithophanie en volume"
TRIANGLES_INDEXES_MAX = 900_000
BLANC = ("#F4F4F4", "Blanc")
NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
REL_MODEL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
MODELE = "3D/3dmodel.model"
REGLAGES = "Metadata/model_settings.config"
_IDENTITE = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"
_MORCEAU = 1 << 16
_STL = np.dtype([("n", "<f4", (3,)), ("v", "<f4", (9,)), ("a", "<u2")])

# Formats proposés (FORMATS_VOLUME de LithoVolumeUi) : libellé (FormatModele.libelle), suffixe, explication.
FORMATS = {
    "stl": ("STL unique", ".stl", "STL : lu par tous les trancheurs, même pour les volumes les plus fins."),
    "3mf": ("3MF multicolore", ".3mf", "3MF : Bambu Studio, OrcaSlicer, PrusaSlicer (filament blanc attribué)."),
    "obj": ("OBJ avec couleurs (ZIP)", "-obj.zip", "OBJ : Blender et la plupart des logiciels 3D."),
}
COURT = {"stl": "STL", "3mf": "3MF", "obj": "OBJ"}


def echapper(t):
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            .replace("'", "&apos;"))


def _arrondi_long(x):
    """Math.round(double) de Java (entier le plus proche, demi vers +∞), exact pour |x| < 2⁵²."""
    x = np.asarray(x, dtype=np.float64)
    bas = np.floor(x)
    return (bas + ((x - bas) >= 0.5)).astype(np.int64)


def _textes_3mf(valeurs):
    """TroisMf.nombre (6 décimales au plus, sans zéros inutiles) de chaque valeur, par valeurs distinctes."""
    n = _arrondi_long(np.asarray(valeurs, dtype=np.float64) * 1e6)
    uniques, inverse = np.unique(n, return_inverse=True)
    textes = []
    for v in uniques.tolist():
        signe = "-" if v < 0 else ""
        v = abs(v)
        f = v % 1_000_000
        textes.append(f"{signe}{v // 1_000_000}" + ("." + f"{f:06d}".rstrip("0") if f else ""))
    return np.array(textes, dtype=object)[inverse.reshape(-1)]


def _texte_obj(r):
    """Double.toString de Java pour r = k / 10 000 (ExportModele.nombre)."""
    if r == round(r):
        return str(int(r))
    if abs(r) < 1e-3:
        return ("-" if r < 0 else "") + f"{int(round(abs(r) * 10_000))}.0E-4"
    return repr(r)


def _textes_obj(valeurs):
    k = _arrondi_long(np.asarray(valeurs, dtype=np.float64) * 10_000)
    uniques, inverse = np.unique(k, return_inverse=True)
    textes = [_texte_obj(v / 10_000.0) for v in uniques.tolist()]
    return np.array(textes, dtype=object)[inverse.reshape(-1)]


class _Flux:
    """Texte UTF-8 écrit par morceaux dans une entrée de ZIP ouverte."""

    def __init__(self, sortie):
        self.sortie, self.morceaux, self.taille = sortie, [], 0

    def ecrire(self, t):
        self.morceaux.append(t)
        self.taille += len(t)
        if self.taille > _MORCEAU * 16:
            self.vider()

    def vider(self):
        if self.morceaux:
            self.sortie.write("".join(self.morceaux).encode("utf-8"))
        self.morceaux, self.taille = [], 0


# --- STL ------------------------------------------------------------------------------------------------------------

def entete_stl(titre):
    """En-tête de 80 octets, ASCII imprimable, ne commence jamais par « solid »."""
    propre = re.sub(r"[^\x20-\x7E]", "_", f"{SIGNATURE} - {titre}")
    return propre.encode("ascii")[:80].ljust(80, b"\0")


def ecrire_stl(sommets, tris, chemin, titre, progress=None):
    """STL binaire écrit par blocs (normales unitaires calculées en float32 comme StlBinaire) ; rend le nombre."""
    n = len(tris)
    if n > 0xFFFFFFFF:
        raise ValueError("Trop de triangles pour un STL binaire.")
    with open(chemin, "wb") as f:
        f.write(entete_stl(titre))
        f.write(np.uint32(n).tobytes())
        for d in range(0, n, _MORCEAU):
            tri = sommets[tris[d:d + _MORCEAU]]
            u = tri[:, 1] - tri[:, 0]
            w = tri[:, 2] - tri[:, 0]
            nrm = np.cross(u, w).astype(F32)
            lg = np.sqrt(nrm[:, 0] * nrm[:, 0] + nrm[:, 1] * nrm[:, 1] + nrm[:, 2] * nrm[:, 2])
            nrm = np.where(lg[:, None] > 0, nrm / np.where(lg > 0, lg, F32(1))[:, None], F32(0)).astype(F32)
            rec = np.zeros(len(tri), dtype=_STL)
            rec["n"] = nrm
            rec["v"] = tri.reshape(-1, 9)
            f.write(rec.tobytes())
            if progress:
                progress(min(1.0, (d + len(tri)) / max(1, n)), "Écriture du STL")
    return n


def taille_stl(triangles):
    return 84 + 50 * triangles


# --- Maillage indexé (Gift Maillage) --------------------------------------------------------------------------------

def maillage_indexe(sommets, tris):
    """Sommets fusionnés par coordonnées float32 exactes (−0 = +0), numérotés dans l'ordre de première apparition
    dans la suite des triangles ; triangles dégénérés (deux sommets confondus) écartés. Rend (sommets, triangles)."""
    cles = np.ascontiguousarray((sommets + F32(0)).astype(F32)).view(np.uint32).reshape(-1, 3)
    _, etiquette = np.unique(cles, axis=0, return_inverse=True)
    etiquette = etiquette.reshape(-1)
    plat = etiquette[tris.reshape(-1)]
    uniques, premier = np.unique(plat, return_index=True)
    ordre = np.argsort(premier, kind="stable")
    nouveau = np.empty(int(uniques.max()) + 1 if uniques.size else 0, dtype=np.int64)
    nouveau[uniques[ordre]] = np.arange(len(uniques))
    v = sommets[tris.reshape(-1)[premier[ordre]]]
    t = nouveau[plat].reshape(-1, 3)
    garde = (t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 0] != t[:, 2])
    return v, t[garde]


# --- 3MF ------------------------------------------------------------------------------------------------------------

def ecrire_3mf(chemin, titre, sommets, tris, progress=None):
    """3MF d'une pièce « Lithophanie » en filament blanc (TroisMfPieces) ; rend le nombre de triangles."""
    if not len(tris):
        raise ValueError("Rien à imprimer.")
    with zipfile.ZipFile(chemin, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("[Content_Types].xml", "\n".join((
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
            '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>',
            '<Default Extension="config" ContentType="text/xml"/>',
            "</Types>")))
        z.writestr("_rels/.rels", "\n".join((
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
            f'<Relationship Target="/{MODELE}" Id="rel0" Type="{REL_MODEL}"/>',
            "</Relationships>")))
        with z.open(MODELE, "w") as sortie:
            w = _Flux(sortie)
            w.ecrire(f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="fr-FR" xmlns="{NS_CORE}">\n')
            w.ecrire(f'<metadata name="Title">{echapper(titre)}</metadata>\n'
                     f'<metadata name="Application">{echapper(APPLICATION)}</metadata>\n')
            w.ecrire('<resources>\n<basematerials id="1">'
                     f'<base name="{echapper(BLANC[1])}" displaycolor="{BLANC[0]}FF"/></basematerials>\n')
            w.ecrire('<object id="2" type="model" name="Lithophanie" pid="1" pindex="0"><mesh><vertices>')
            textes = _textes_3mf(sommets.astype(np.float64).reshape(-1)).reshape(-1, 3)
            for d in range(0, len(textes), _MORCEAU):
                w.ecrire("".join(f'<vertex x="{x}" y="{y}" z="{zz}"/>' for x, y, zz in textes[d:d + _MORCEAU].tolist()))
            if progress:
                progress(0.5, "Écriture du 3MF")
            w.ecrire("</vertices><triangles>")
            for d in range(0, len(tris), _MORCEAU):
                w.ecrire("".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris[d:d + _MORCEAU].tolist()))
            w.ecrire("</triangles></mesh></object>\n")
            w.ecrire(f'<object id="3" type="model" name="{echapper(titre)}"><components><component objectid="2"/>'
                     '</components></object>\n</resources>\n<build><item objectid="3"/></build>\n</model>\n')
            w.vider()
        z.writestr(REGLAGES, "".join((
            '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <object id="3">\n',
            f'    <metadata key="name" value="{echapper(titre)}"/>\n',
            '    <metadata key="extruder" value="1"/>\n',
            '    <part id="2" subtype="normal_part">\n',
            '      <metadata key="name" value="Lithophanie"/>\n',
            f'      <metadata key="matrix" value="{_IDENTITE}"/>\n',
            '      <metadata key="extruder" value="1"/>\n    </part>\n',
            "  </object>\n</config>")))
    if progress:
        progress(1.0, "3MF écrit")
    return len(tris)


# --- OBJ ------------------------------------------------------------------------------------------------------------

def _lisez_moi(titre, lignes, conseil):
    return "\r\n".join([f"{titre} — {SIGNATURE}", ""] + list(lignes) + ["", conseil])


def ecrire_obj(chemin, titre, sommets, tris, progress=None):
    """ZIP d'un OBJ (objet « Lithophanie »), de son MTL (blanc) et d'un LISEZMOI ; rend le nombre de triangles."""
    hex_, nom = BLANC
    r, g, b = (int(hex_[i:i + 2], 16) for i in (1, 3, 5))
    with zipfile.ZipFile(chemin, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("modele.mtl", f"# {titre} — {SIGNATURE}\r\n"
                                 f"newmtl couleur_1\r\nKd {r / 255:.4f} {g / 255:.4f} {b / 255:.4f}\r\n"
                                 "Ka 0 0 0\r\nKs 0 0 0\r\nd 1\r\nillum 1\r\n\r\n")
        with z.open("modele.obj", "w") as sortie:
            w = _Flux(sortie)
            w.ecrire(f"# {titre} — {SIGNATURE} (mm)\r\nmtllib modele.mtl\r\n")
            if len(tris):
                w.ecrire("o Lithophanie\r\nusemtl couleur_1\r\n")
                textes = _textes_obj(sommets.astype(np.float64).reshape(-1)).reshape(-1, 3)
                for d in range(0, len(textes), _MORCEAU):
                    w.ecrire("".join(f"v {x} {y} {zz}\r\n" for x, y, zz in textes[d:d + _MORCEAU].tolist()))
                if progress:
                    progress(0.5, "Écriture de l'OBJ")
                for d in range(0, len(tris), _MORCEAU):
                    w.ecrire("".join(f"f {a + 1} {b_ + 1} {c + 1}\r\n" for a, b_, c in tris[d:d + _MORCEAU].tolist()))
            w.vider()
        z.writestr("LISEZMOI.txt", _lisez_moi(titre, [f"Lithophanie : {nom} {hex_}"],
                                              "Ouvre modele.obj (garde modele.mtl à côté) : chaque objet a sa couleur."))
    if progress:
        progress(1.0, "OBJ écrit")
    return len(tris)
