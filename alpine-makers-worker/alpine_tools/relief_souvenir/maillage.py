"""Relief souvenir — maillages indexés (sommets (n, 3) en mm, triangles (m, 3) directs = normale sortante).

Solides simples (prisme convexe, sphère), assemblage, volume, contrôle de fermeture, et écrivains de fichiers :
3MF multicolore (port de TroisMfPieces : basematerials + filament de chaque pièce dans Metadata/model_settings.config),
STL binaire par couleur (ZIP), STL unique, OBJ + MTL (ZIP) — port d'ExportModele.kt.
"""
import math
import re
import zipfile

import numpy as np

DENSITE_PLA = 1.24
VIDE = (np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64))


def assembler(morceaux):
    """Réunit des maillages (corps séparés) en un seul."""
    morceaux = [m for m in morceaux if len(m[1])]
    if not morceaux:
        return VIDE
    v, t, decalage = [], [], 0
    for s, tr in morceaux:
        v.append(np.asarray(s, dtype=float))
        t.append(np.asarray(tr, dtype=np.int64) + decalage)
        decalage += len(s)
    return np.vstack(v), np.vstack(t)


def prisme_convexe(xs, ys, z0, z1):
    """Prisme droit d'un polygone convexe direct (xs, ys) entre z0 et z1."""
    n = len(xs)
    s = np.zeros((2 * n, 3))
    s[:n, 0] = s[n:, 0] = xs
    s[:n, 1] = s[n:, 1] = ys
    s[:n, 2] = z0
    s[n:, 2] = z1
    t = []
    for k in range(1, n - 1):
        t.append((0, k + 1, k))              # dessous
        t.append((n, n + k, n + k + 1))      # dessus
    for i in range(n):
        j = (i + 1) % n
        t.append((i, j, n + j))
        t.append((i, n + j, n + i))
    return s, np.asarray(t, dtype=np.int64)


def boite(x0, y0, x1, y1, z0, z1):
    return prisme_convexe([x0, x1, x1, x0], [y0, y0, y1, y1], z0, z1)


def sphere(cx, cy, cz, r, bas, meridiens=12, paralleles=7):
    """Sphère fermée (méridiens × parallèles), z bornée par [bas] (port de Relief3D.sphere)."""
    s = [(cx, cy, max(bas, cz - r)), (cx, cy, cz + r)]
    anneaux = []
    for k in range(paralleles - 1):
        phi = -math.pi / 2 + math.pi * (k + 1) / paralleles
        ids = []
        for j in range(meridiens):
            t = 2 * math.pi * j / meridiens
            ids.append(len(s))
            s.append((cx + r * math.cos(phi) * math.cos(t), cy + r * math.cos(phi) * math.sin(t), max(bas + 1e-3 * (k + 1), cz + r * math.sin(phi))))
        anneaux.append(ids)
    tri = []
    for j in range(meridiens):
        j2 = (j + 1) % meridiens
        tri.append((0, anneaux[0][j2], anneaux[0][j]))
        for k in range(paralleles - 2):
            tri.append((anneaux[k][j], anneaux[k][j2], anneaux[k + 1][j2]))
            tri.append((anneaux[k][j], anneaux[k + 1][j2], anneaux[k + 1][j]))
        tri.append((1, anneaux[-1][j], anneaux[-1][j2]))
    return np.asarray(s, dtype=float), np.asarray(tri, dtype=np.int64)


def volume(s, t):
    """Volume signé (mm³), positif si les normales sont sortantes."""
    if not len(t):
        return 0.0
    a, b, c = s[t[:, 0]], s[t[:, 1]], s[t[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


def controle(t):
    """Arêtes libres, multiples et d'orientation incohérente (indices) : fermé si les trois sont nulles."""
    t = np.asarray(t, dtype=np.int64)
    if not len(t):
        return {"triangles": 0, "libres": 0, "multiples": 0, "incoherentes": 0, "ferme": False}
    a = np.concatenate([t[:, 0], t[:, 1], t[:, 2]])
    b = np.concatenate([t[:, 1], t[:, 2], t[:, 0]])
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    n = int(t.max()) + 1
    cle = lo * n + hi
    sens = np.where(a < b, 1, -1)
    uniques, inv, compte = np.unique(cle, return_inverse=True, return_counts=True)
    somme = np.zeros(len(uniques), dtype=np.int64)
    np.add.at(somme, inv, sens)
    libres = int((compte == 1).sum())
    multiples = int((compte > 2).sum())
    incoherentes = int(((compte == 2) & (somme != 0)).sum())
    return {"triangles": len(t), "libres": libres, "multiples": multiples, "incoherentes": incoherentes,
            "ferme": libres == 0 and multiples == 0 and incoherentes == 0}


# ================================================================================================ écrivains
NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
REL_MODEL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
REL_VIGNETTE = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"
MODELE = "3D/3dmodel.model"
REGLAGES = "Metadata/model_settings.config"
VIGNETTE = "Metadata/thumbnail.png"
IDENTITE = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"
APPLICATION = "Alpine Makers Worker — Relief souvenir"


def echapper(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")


def _lignes(fmt, tableau, bloc=20000):
    """Texte formaté par blocs (une seule opération % par bloc : rapide)."""
    for k in range(0, len(tableau), bloc):
        part = tableau[k:k + bloc]
        yield (fmt * len(part)) % tuple(part.ravel().tolist())


def ecrire_3mf(chemin, titre, filaments, pieces, vignette=None, progress=None):
    """filaments : [(hex, nom)] ; pieces : [{"nom", "filament", "sommets", "triangles"}] (indice de filament 0..).
    Rend [{"nom", "filament", "triangles", "volume_mm3"}]."""
    infos = []
    with zipfile.ZipFile(chemin, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        types = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
                 '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>',
                 '<Default Extension="config" ContentType="text/xml"/>']
        if vignette:
            types.append('<Default Extension="png" ContentType="image/png"/>')
        z.writestr("[Content_Types].xml", "\n".join(types + ["</Types>"]))
        rels = ['<?xml version="1.0" encoding="UTF-8"?>',
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
                f'<Relationship Target="/{MODELE}" Id="rel0" Type="{REL_MODEL}"/>']
        if vignette:
            rels.append(f'<Relationship Target="/{VIGNETTE}" Id="rel1" Type="{REL_VIGNETTE}"/>')
        z.writestr("_rels/.rels", "\n".join(rels + ["</Relationships>"]))
        ids = []
        with z.open(MODELE, "w", force_zip64=True) as f:
            def w(t):
                f.write(t.encode("utf-8"))
            w(f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="fr-FR" xmlns="{NS_CORE}">\n')
            w(f'<metadata name="Title">{echapper(titre)}</metadata>\n<metadata name="Application">{echapper(APPLICATION)}</metadata>\n')
            w('<resources>\n<basematerials id="1">')
            for hexa, nom in filaments:
                w(f'<base name="{echapper(nom)}" displaycolor="{hexa.upper()}FF"/>')
            w("</basematerials>\n")
            for n, pc in enumerate(pieces):
                s, t = pc["sommets"], pc["triangles"]
                if not len(t):
                    continue
                ident = 2 + len(ids)
                ids.append(ident)
                w(f'<object id="{ident}" type="model" name="{echapper(pc["nom"])}" pid="1" pindex="{pc["filament"]}"><mesh><vertices>')
                for bloc in _lignes('<vertex x="%.4f" y="%.4f" z="%.4f"/>', np.asarray(s, dtype=float)):
                    w(bloc)
                w("</vertices><triangles>")
                for bloc in _lignes('<triangle v1="%d" v2="%d" v3="%d"/>', np.asarray(t, dtype=np.int64)):
                    w(bloc)
                w("</triangles></mesh></object>\n")
                infos.append({"nom": pc["nom"], "filament": pc["filament"], "triangles": len(t), "volume_mm3": volume(s, t)})
                if progress:
                    progress((n + 1) / len(pieces))
            if not ids:
                raise ValueError("Rien à imprimer : la zone ne contient aucun relief.")
            assemblage = 2 + len(ids)
            w(f'<object id="{assemblage}" type="model" name="{echapper(titre)}"><components>')
            w("".join(f'<component objectid="{i}"/>' for i in ids))
            w(f'</components></object>\n</resources>\n<build><item objectid="{assemblage}"/></build>\n</model>\n')
        r = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>", f'  <object id="{assemblage}">',
             f'    <metadata key="name" value="{echapper(titre)}"/>', f'    <metadata key="extruder" value="{infos[0]["filament"] + 1}"/>']
        for k, info in enumerate(infos):
            r += [f'    <part id="{ids[k]}" subtype="normal_part">', f'      <metadata key="name" value="{echapper(info["nom"])}"/>',
                  f'      <metadata key="matrix" value="{IDENTITE}"/>', f'      <metadata key="extruder" value="{info["filament"] + 1}"/>', "    </part>"]
        r += ["  </object>", "</config>"]
        z.writestr(REGLAGES, "\n".join(r))
        if vignette:
            z.writestr(VIGNETTE, vignette)
    return infos


def stl_octets(titre, s, t):
    """STL binaire d'un maillage."""
    s = np.asarray(s, dtype=float)
    t = np.asarray(t, dtype=np.int64)
    entete = re.sub(r"[^\x20-\x7E]", "_", f"Alpine Makers - {titre}").encode("ascii")[:80].ljust(80, b"\0")
    a, b, c = s[t[:, 0]], s[t[:, 1]], s[t[:, 2]]
    n = np.cross(b - a, c - a)
    l = np.linalg.norm(n, axis=1)
    n = np.where(l[:, None] > 0, n / np.where(l > 0, l, 1)[:, None], 0)
    rec = np.zeros(len(t), dtype=[("n", "<f4", 3), ("a", "<f4", 3), ("b", "<f4", 3), ("c", "<f4", 3), ("attr", "<u2")])
    rec["n"], rec["a"], rec["b"], rec["c"] = n, a, b, c
    return entete + np.uint32(len(t)).tobytes() + rec.tobytes()


def nom_sur(nom):
    t = re.sub(r"[^A-Za-z0-9À-ÿ ._+-]", " ", str(nom))
    return re.sub(r"\s+", " ", t).strip()[:60] or "piece"


def _lisez_moi(titre, lignes, conseil):
    return "\r\n".join([f"{titre} — Alpine Makers, Relief souvenir", ""] + lignes + ["", conseil])


def ecrire_stl_couleurs(chemin, titre, pieces):
    """ZIP d'un STL par pièce (mêmes coordonnées : ils se superposent exactement) ; rend le nombre de triangles."""
    total, lignes, pris = 0, [], set()
    with zipfile.ZipFile(chemin, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for i, pc in enumerate(pieces):
            if not len(pc["triangles"]):
                continue
            nom = "%02d-%s.stl" % (i + 1, nom_sur(pc["nom"]))
            if nom.lower() in pris:
                nom = "%02d-%s-%d.stl" % (i + 1, nom_sur(pc["nom"]), i)
            pris.add(nom.lower())
            z.writestr(nom, stl_octets(pc["nom"], pc["sommets"], pc["triangles"]))
            total += len(pc["triangles"])
            lignes.append(f"{nom} : {pc['teinte'][1]} {pc['teinte'][0]}")
        z.writestr("LISEZMOI.txt", _lisez_moi(titre, lignes, "Importe tous les STL en une fois comme pièces d'un même objet "
                                                           "(ils se superposent exactement), puis attribue à chacun le filament de sa couleur."))
    return total


def ecrire_stl_unique(chemin, titre, pieces):
    s, t = assembler([(pc["sommets"], pc["triangles"]) for pc in pieces])
    with open(chemin, "wb") as f:
        f.write(stl_octets(titre, s, t))
    return len(t)


def ecrire_obj(chemin, titre, pieces):
    """ZIP d'un OBJ (un objet par pièce) et de son MTL (une matière par couleur) ; rend le nombre de triangles."""
    couleurs = []
    for pc in pieces:
        if pc["teinte"][0] not in couleurs:
            couleurs.append(pc["teinte"][0])
    mtl = [f"# {titre} — Alpine Makers"]
    for k, hexa in enumerate(couleurs):
        r, g, b = (int(hexa[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
        mtl += [f"newmtl couleur_{k + 1}", f"Kd {r:.4f} {g:.4f} {b:.4f}", "Ka 0 0 0", "Ks 0 0 0", "d 1", "illum 1", ""]
    total = 0
    with zipfile.ZipFile(chemin, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("modele.mtl", "\r\n".join(mtl))
        with z.open("modele.obj", "w", force_zip64=True) as f:
            f.write(f"# {titre} — Alpine Makers (mm)\r\nmtllib modele.mtl\r\n".encode("utf-8"))
            decalage = 0
            for pc in pieces:
                s, t = pc["sommets"], pc["triangles"]
                if not len(t):
                    continue
                f.write(f"o {nom_sur(pc['nom']).replace(' ', '_')}\r\nusemtl couleur_{couleurs.index(pc['teinte'][0]) + 1}\r\n".encode("utf-8"))
                for bloc in _lignes("v %.4f %.4f %.4f\r\n", np.asarray(s, dtype=float)):
                    f.write(bloc.encode("ascii"))
                for bloc in _lignes("f %d %d %d\r\n", np.asarray(t, dtype=np.int64) + 1 + decalage):
                    f.write(bloc.encode("ascii"))
                decalage += len(s)
                total += len(t)
        z.writestr("LISEZMOI.txt", _lisez_moi(titre, [f"{pc['nom']} : {pc['teinte'][1]} {pc['teinte'][0]}" for pc in pieces],
                                              "Ouvre modele.obj (garde modele.mtl à côté) : chaque objet a sa couleur."))
    return total
