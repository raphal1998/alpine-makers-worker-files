"""Modèles 3D d'Alpine Prism (port de Maillage.kt) : lecture STL (binaire ou texte), OBJ et 3MF, mesures
(volume, surface, encombrement, étanchéité, surplombs, colonnes de support) et estimation d'impression
filament ou résine. Unités : millimètres pour STL/OBJ, attribut ``unit`` pour le 3MF.

numpy pour les calculs ; bibliothèque standard pour la lecture.
"""
import io
import math
import re
import zipfile

import numpy as np

TAILLE_MAX = 40 * 1024 * 1024
MAX_TRIANGLES = 6_000_000
MAX_ETANCHEITE = 300_000
_MAX_3MF = 400 * 1024 * 1024
_MAX_ECHANTILLONS = 1 << 25  # capacité du tampon Kotlin (doublé depuis 65 536) au-delà de 24 millions


class MaillageException(ValueError):
    """Modèle refusé ou illisible (message en français)."""


def lire(octets, nom):
    """Triangles du modèle en mm : tableau float32 (n, 9) — x, y, z des trois sommets."""
    if len(octets) > TAILLE_MAX:
        raise MaillageException("Fichier trop volumineux (40 Mo au plus).")
    ext = nom.rsplit(".", 1)[-1].lower() if "." in nom else ""
    if ext == "stl":
        t = _stl(octets)
    elif ext == "obj":
        t = _obj(octets.decode("utf-8", errors="replace"))
    elif ext == "3mf":
        t = _trois_mf(octets)
    else:
        raise MaillageException("Format non pris en charge : STL, 3MF ou OBJ.")
    if len(t) == 0:
        raise MaillageException("Aucun triangle lisible dans ce modèle.")
    return t


def _stl(o):
    if len(o) >= 84:
        n = int.from_bytes(o[80:84], "little")
        if 84 + n * 50 == len(o):
            if n > MAX_TRIANGLES:
                raise MaillageException("Modèle trop détaillé (plus de 6 millions de triangles).")
            dt = np.dtype([("n", "<f4", (3,)), ("v", "<f4", (9,)), ("a", "<u2")])
            return np.frombuffer(o, dtype=dt, count=n, offset=84)["v"].astype(np.float32)
    texte = o.decode("ascii", errors="replace")
    valeurs = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", texte, flags=re.IGNORECASE)
    if len(valeurs) * 3 > MAX_TRIANGLES * 9:
        raise MaillageException("Modèle trop détaillé.")
    try:
        v = np.array([float(x) for triple in valeurs for x in triple], dtype=np.float32)
    except ValueError:
        raise MaillageException("STL texte illisible.") from None
    if len(v) % 9 != 0:
        raise MaillageException("STL texte incomplet.")
    return v.reshape(-1, 9)


def _flottant(t):
    try:
        return float(t)
    except ValueError:
        return 0.0


def _obj(texte):
    v = []
    faces = []
    for ligne in texte.splitlines():
        l = ligne.strip()
        if l.startswith("v "):
            morceaux = l[2:].split()[:3]
            v.append([_flottant(x) for x in morceaux] + [0.0] * (3 - len(morceaux)))
        elif l.startswith("f "):
            nb = len(v)
            idx = []
            for morceau in l[2:].split():
                tete = morceau.split("/", 1)[0]
                if re.fullmatch(r"[+-]?\d+", tete):
                    i = int(tete)
                    idx.append(nb + i if i < 0 else i - 1)
            for k in range(1, len(idx) - 1):
                tri = (idx[0], idx[k], idx[k + 1])
                if any(not 0 <= i < nb for i in tri):
                    raise MaillageException("OBJ : face qui renvoie à un sommet absent.")
                faces.append(tri)
            if len(faces) > MAX_TRIANGLES:
                raise MaillageException("Modèle trop détaillé.")
    if not faces:
        return np.zeros((0, 9), dtype=np.float32)
    sommets = np.array(v, dtype=np.float32)
    return sommets[np.array(faces, dtype=np.int64)].reshape(-1, 9)


# ------------------------------------------------------------------------------------------------ 3MF
_TAG = re.compile(r"<([^>]*)>")
_ATTR = re.compile(r'([\w:]+)\s*=\s*"([^"]*)"')
_IDENTITE = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
_UNITES = {"micron": 0.001, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}


def _matrice(t):
    if t is None:
        return _IDENTITE
    n = []
    for x in t.strip().split():
        try:
            n.append(float(x))
        except ValueError:
            pass
    return tuple(n) if len(n) == 12 else _IDENTITE


def _composer(a, b):
    """a puis b (3MF : point × M, matrices 4×3 en lignes)."""
    r = [0.0] * 12
    for i in range(4):
        for j in range(3):
            s = b[9 + j] if i == 3 else 0.0
            for k in range(3):
                s += a[i * 3 + k] * b[k * 3 + j]
            r[i * 3 + j] = s
    return tuple(r)


def _attrs(tag):
    return {nom.split(":", 1)[-1].lower(): valeur for nom, valeur in _ATTR.findall(tag)}


def _entier(t):
    return int(t) if t is not None and re.fullmatch(r"[+-]?\d+", t) else 0


def _trois_mf(o):
    fichiers = {}
    try:
        with zipfile.ZipFile(io.BytesIO(o)) as z:
            total = 0
            for info in z.infolist():
                if info.is_dir() or not info.filename.lower().endswith(".model"):
                    continue
                with z.open(info) as flux:
                    morceaux = []
                    while True:
                        bloc = flux.read(1 << 20)
                        if not bloc:
                            break
                        total += len(bloc)
                        if total > _MAX_3MF:
                            raise MaillageException("3MF trop volumineux une fois décompressé.")
                        morceaux.append(bloc)
                fichiers["/" + info.filename.lstrip("/")] = b"".join(morceaux).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, EOFError, OSError, RuntimeError, NotImplementedError):
        raise MaillageException("3MF illisible : archive endommagée.") from None
    if not fichiers:
        raise MaillageException("3MF sans modèle lisible.")
    racine = next((k for k in fichiers if k.lower() == "/3d/3dmodel.model"), next(iter(fichiers)))
    objets = {}
    items = []
    echelle = 1.0
    nb_triangles = 0
    for chemin, xml in fichiers.items():
        ident = None
        sv, st, comps = [], [], []
        for correspondance in _TAG.finditer(xml):
            tag = correspondance.group(1)
            if tag.startswith("?") or tag.startswith("!"):
                continue
            ferme = tag.startswith("/")
            nom = tag.lstrip("/").split(" ", 1)[0].split("/", 1)[0]
            nom = (nom.split(":", 1)[1] if ":" in nom else nom).lower()
            if nom == "vertex":
                a = _attrs(tag)
                sv.append((_flottant(a.get("x", "0")), _flottant(a.get("y", "0")), _flottant(a.get("z", "0"))))
            elif nom == "triangle":
                a = _attrs(tag)
                st.append((_entier(a.get("v1")), _entier(a.get("v2")), _entier(a.get("v3"))))
                nb_triangles += 1
                if nb_triangles > MAX_TRIANGLES:
                    raise MaillageException("Modèle trop détaillé.")
            elif nom == "model" and not ferme and chemin == racine:
                echelle = _UNITES.get((_attrs(tag).get("unit") or "").lower(), 1.0)
            elif nom == "object" and not ferme:
                ident = _attrs(tag).get("id")
                sv, st, comps = [], [], []
            elif nom == "object" and ferme:
                if ident is not None:
                    objets[f"{chemin}#{ident}"] = (np.array(sv, dtype=np.float32).reshape(-1, 3),
                                                  np.array(st, dtype=np.int64).reshape(-1, 3), list(comps))
                ident = None
            elif nom == "component":
                a = _attrs(tag)
                comps.append((a.get("path") or chemin, a.get("objectid") or "", _matrice(a.get("transform"))))
            elif nom == "item" and chemin == racine:
                a = _attrs(tag)
                items.append((a.get("objectid") or "", _matrice(a.get("transform"))))
    sortie = []

    def ajouter(cle, m, profondeur):
        if profondeur > 8 or cle not in objets:
            return
        s, tri, comps = objets[cle]
        if len(tri):
            if tri.min() < 0 or tri.max() >= len(s):
                raise MaillageException("3MF : triangle qui renvoie à un sommet absent.")
            p = s[tri.reshape(-1)].astype(np.float64)
            mat = np.array(m, dtype=np.float64)
            x, y, z = p[:, 0], p[:, 1], p[:, 2]
            out = np.empty_like(p)
            for j in range(3):
                out[:, j] = (x * mat[j] + y * mat[3 + j] + z * mat[6 + j] + mat[9 + j]) * echelle
            sortie.append(out.astype(np.float32).reshape(-1, 9))
        for sous_chemin, ident, t in comps:
            ajouter(f"{sous_chemin}#{ident}", _composer(t, m), profondeur + 1)

    if items:
        for ident, t in items:
            ajouter(f"{racine}#{ident}", t, 0)
    else:
        for cle in list(objets):
            ajouter(cle, _IDENTITE, 0)
    if not sortie:
        return np.zeros((0, 9), dtype=np.float32)
    t = np.concatenate(sortie)
    if len(t) > MAX_TRIANGLES:
        raise MaillageException("Modèle trop détaillé.")
    return t


# ------------------------------------------------------------------------------------------------ mesures

def mesurer(triangles, angle_surplomb=45.0):
    """Mesure3D de Maillage.kt sous forme de dict (mm, mm², mm³)."""
    s = np.asarray(triangles, dtype=np.float32).reshape(-1, 9)
    n = len(s)
    p = s.astype(np.float64).reshape(n, 3, 3)
    mins = p.reshape(-1, 3).min(axis=0)
    maxs = p.reshape(-1, 3).max(axis=0)
    volume = 0.0
    surface = 0.0
    for debut in range(0, n, 500_000):
        q = p[debut:debut + 500_000] - mins
        a, b, c = q[:, 0], q[:, 1], q[:, 2]
        volume += float(np.sum(a[:, 0] * (b[:, 1] * c[:, 2] - b[:, 2] * c[:, 1])
                               - a[:, 1] * (b[:, 0] * c[:, 2] - b[:, 2] * c[:, 0])
                               + a[:, 2] * (b[:, 0] * c[:, 1] - b[:, 1] * c[:, 0])) / 6.0)
        surface += float(np.sum(np.linalg.norm(np.cross(b - a, c - a), axis=1)) / 2)
    surplomb, colonnes, emprise = _supports(p, mins, maxs, angle_surplomb)
    return {
        "nbTriangles": n, "volumeMm3": abs(volume), "surfaceMm2": surface,
        "dx": float(maxs[0] - mins[0]), "dy": float(maxs[1] - mins[1]), "dz": float(maxs[2] - mins[2]),
        "empriseMm2": emprise, "etanche": _etancheite(s), "surplombMm2": surplomb, "colonnesSupportMm3": colonnes,
    }


def _etancheite(s):
    """Chaque arête partagée par exactement deux triangles (sommets fusionnés au centième de mm) ; None au-delà."""
    n = len(s)
    if n > MAX_ETANCHEITE:
        return None
    r = np.floor(s.reshape(-1, 3).astype(np.float64) * 100.0 + 0.5).astype(np.int64)
    masque = np.int64(0x1FFFFF)
    cles = ((r[:, 0] & masque) << 42) | ((r[:, 1] & masque) << 21) | (r[:, 2] & masque)
    _, ids = np.unique(cles, return_inverse=True)
    ids = ids.reshape(n, 3)
    p = np.concatenate([ids[:, 0], ids[:, 1], ids[:, 2]])
    q = np.concatenate([ids[:, 1], ids[:, 2], ids[:, 0]])
    garde = p != q
    p, q = p[garde], q[garde]
    aretes = (np.minimum(p, q).astype(np.int64) << 32) | np.maximum(p, q).astype(np.int64)
    if len(aretes) == 0:
        return True
    _, nombres = np.unique(aretes, return_counts=True)
    return bool(np.all(nombres == 2))


def _supports(p, mins, maxs, angle):
    """Carte de hauteurs : sous chaque face en surplomb, la colonne de support descend jusqu'à la surface tournée vers
    le haut la plus proche, ou jusqu'au plateau. Renvoie (surface en surplomb, volume des colonnes, emprise au sol)."""
    min_x, min_y, min_z = float(mins[0]), float(mins[1]), float(mins[2])
    largeur = max(float(maxs[0]) - min_x, 0.01)
    profondeur = max(float(maxs[1]) - min_y, 0.01)
    pas = max(0.5, math.sqrt(largeur * profondeur / 120_000.0))
    seuil = -math.cos(math.radians(angle))
    a, b, c = p[:, 0], p[:, 1], p[:, 2]
    u, v = b - a, c - a
    nn = np.cross(u, v)
    longueur = np.linalg.norm(nn, axis=1)
    valide = longueur != 0.0
    cos_z = np.zeros(len(p))
    cos_z[valide] = nn[valide, 2] / longueur[valide]
    valide &= np.abs(cos_z) >= 0.02
    genre = np.where(cos_z > 0, 1, np.where(cos_z < seuil, 3, 2)).astype(np.int8)
    z_bas = np.minimum(a[:, 2], np.minimum(b[:, 2], c[:, 2]))
    en_l_air = valide & (genre == 3) & (z_bas > min_z + 0.2)
    surplomb = float(np.sum(np.abs(nn[en_l_air, 2]) / 2))
    det = u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0]
    valide &= np.abs(det) >= 1e-12
    idx = np.nonzero(valide)[0]
    xs = p[idx, :, 0]
    ys = p[idx, :, 1]
    while True:
        nx = int(math.ceil(largeur / pas)) + 1
        ny = int(math.ceil(profondeur / pas)) + 1
        i0 = np.maximum(0, ((xs.min(axis=1) - min_x) / pas).astype(np.int64))
        i1 = np.minimum(nx - 1, ((xs.max(axis=1) - min_x) / pas).astype(np.int64) + 1)
        j0 = np.maximum(0, ((ys.min(axis=1) - min_y) / pas).astype(np.int64))
        j1 = np.minimum(ny - 1, ((ys.max(axis=1) - min_y) / pas).astype(np.int64) + 1)
        ci = np.maximum(0, i1 - i0 + 1)
        cj = np.maximum(0, j1 - j0 + 1)
        compte = ci * cj
        cellules, hauteurs, genres, rangs = [], [], [], []
        total = 0
        depasse = False
        debut = 0
        while debut < len(idx):
            # Lots d'environ un million de cases candidates (mémoire bornée).
            cumul = np.cumsum(compte[debut:])
            fin = debut + max(1, int(np.searchsorted(cumul, 1_000_000, side="right")))
            sel = slice(debut, fin)
            k = compte[sel]
            nb = int(k.sum())
            if nb:
                local_t = np.repeat(np.arange(fin - debut), k)
                depart = np.repeat(np.cumsum(k) - k, k)
                local = np.arange(nb) - depart
                largeur_t = ci[sel][local_t]
                ii = i0[sel][local_t] + local % largeur_t
                jj = j0[sel][local_t] + local // largeur_t
                t = idx[sel][local_t]
                ax, ay, az = a[t, 0], a[t, 1], a[t, 2]
                px = min_x + ii * pas - ax
                py = min_y + jj * pas - ay
                d = det[t]
                l1 = (px * v[t, 1] - py * v[t, 0]) / d
                l2 = (u[t, 0] * py - u[t, 1] * px) / d
                dedans = (l1 >= -1e-9) & (l2 >= -1e-9) & (l1 + l2 <= 1 + 1e-9)
                t, ii, jj, l1, l2 = t[dedans], ii[dedans], jj[dedans], l1[dedans], l2[dedans]
                total += len(t)
                if total > _MAX_ECHANTILLONS:
                    depasse = True
                    break
                cellules.append(jj * nx + ii)
                hauteurs.append((a[t, 2] + l1 * u[t, 2] + l2 * v[t, 2]).astype(np.float32))
                genres.append(genre[t])
                rangs.append(t)
            debut = fin
        if depasse:
            pas *= 1.6
            continue
        break
    if not cellules:
        return surplomb, 0.0, 0.0
    cel = np.concatenate(cellules)
    z = np.concatenate(hauteurs)
    g = np.concatenate(genres)
    rang = np.concatenate(rangs)
    uniques, par_cellule = np.unique(cel, return_counts=True)
    emprise = len(uniques) * pas * pas
    if par_cellule.max() > 4096:
        # Kotlin lit au plus 4096 hauteurs par case, les plus récemment ajoutées.
        ordre = np.lexsort((-rang, cel))
        cel, z, g, rang = cel[ordre], z[ordre], g[ordre], rang[ordre]
        depart = np.repeat(np.cumsum(par_cellule) - par_cellule, par_cellule)
        garde = (np.arange(len(cel)) - depart) < 4096
        cel, z, g, rang = cel[garde], z[garde], g[garde], rang[garde]
    # Seules les cases qui ont un surplomb comptent ; tri par hauteur (à égalité : dernier triangle ajouté d'abord).
    cases_surplomb = np.unique(cel[g == 3])
    garde = np.isin(cel, cases_surplomb) & (g != 2)
    cel, z, g, rang = cel[garde], z[garde], g[garde], rang[garde]
    ordre = np.lexsort((-rang, z, cel))
    cel, z, g = cel[ordre].tolist(), z[ordre].astype(np.float64).tolist(), g[ordre].tolist()
    colonnes = 0.0
    precedente = None
    dessous = min_z
    aire = pas * pas
    for cellule, h, genre_k in zip(cel, z, g):
        if cellule != precedente:
            precedente = cellule
            dessous = min_z
        if genre_k == 1:
            dessous = max(dessous, h)
        elif h - dessous > 0.3:
            colonnes += (h - dessous) * aire
            dessous = h
    return surplomb, colonnes, emprise


# ------------------------------------------------------------------------------------------------ estimation

def densite(type_filament):
    """Densité courante d'un type de filament (g/cm³)."""
    t = (type_filament or "").upper()
    if t.startswith("PETG") or t.startswith("PCTG"):
        return 1.27
    if t.startswith("ABS") or t.startswith("HIPS"):
        return 1.04
    if t.startswith("ASA"):
        return 1.07
    if t.startswith("TPU"):
        return 1.21
    if t.startswith("PA"):
        return 1.14
    if t.startswith("PC"):
        return 1.20
    if t.startswith("PVA") or t.startswith("BVOH"):
        return 1.23
    if t.startswith("PP"):
        return 0.90
    if t.startswith("LW-PLA"):
        return 0.8
    return 1.24


def _borner(v, lo, hi):
    return max(lo, min(hi, v))


def filament(m, remplissage_pct=15, hauteur_couche=0.2, parois=2, supports=True, densite_g_cm3=1.24):
    """Coque (parois et couches pleines ≈ surface × épaisseur des parois), remplissage du reste, supports (colonnes ×
    densité de support 15 %). Temps : débits volumiques proportionnels à la hauteur de couche, changements de couche
    et préparation (chauffe, calibrage)."""
    epaisseur = _borner(int(parois), 1, 8) * 0.42
    coque = min(m["volumeMm3"], m["surfaceMm2"] * epaisseur)
    remplissage = max(0.0, m["volumeMm3"] - coque) * _borner(int(remplissage_pct), 0, 100) / 100.0
    sup = m["colonnesSupportMm3"] * 0.15 + m["surplombMm2"] * 0.4 if supports else 0.0
    f = _borner(hauteur_couche / 0.2, 0.3, 1.6)
    couches = int(math.ceil(m["dz"] / max(hauteur_couche, 0.04)))
    secondes = coque / (7.0 * f) + remplissage / min(20.0, 14.0 * f) + sup / (10.0 * f) + couches * 2.5 + 300.0

    def g(mm3):
        return mm3 * densite_g_cm3 / 1000.0

    return {"pieceG": g(coque + remplissage), "supportsG": g(sup), "totalG": g(coque + remplissage + sup),
            "volumeMl": (coque + remplissage + sup) / 1000.0, "minutes": secondes / 60.0, "couches": couches}


def resine(m, hauteur_couche=0.05, supports=True, secondes_par_couche=8.0):
    """Pièce pleine, supports fins et radeau ; temps = couches × (exposition + levée), pièce surélevée de 5 mm."""
    sup = m["colonnesSupportMm3"] * 0.05 + m["empriseMm2"] * 0.5 if supports else 0.0
    couches = int(math.ceil((m["dz"] + (5.0 if supports else 0.0)) / max(hauteur_couche, 0.01)))
    ml = (m["volumeMm3"] + sup) / 1000.0
    return {"pieceG": m["volumeMm3"] / 1000.0 * 1.1, "supportsG": sup / 1000.0 * 1.1, "totalG": ml * 1.1,
            "volumeMl": ml, "minutes": couches * secondes_par_couche / 60.0, "couches": couches}
