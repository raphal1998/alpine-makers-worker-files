# -*- coding: utf-8 -*-
"""Déroulé d'un job Tableau couleur (EcranTableau.exporter de Gift) : photo → aperçu → carte d'impression →
fichier au format choisi, aperçu PNG et détails (taille, couleurs, grammes par filament, conseils)."""
import io
import math
from dataclasses import replace
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from . import carte as K
from . import couleurs as C
from . import exports as E
from . import maillage as M

COTE_PHOTO_MAX = 2400               # Photos.COTE_MAX de Gift : photo réduite à ce côté avant le calcul
PIXELS_PHOTO_MAX = 60_000_000       # au-delà, photo refusée avant décodage
APERCU_COTE_MAX = 1200
# Cadres de l'écran (FormatCadre) : proportion largeur / hauteur en Float, comme l'application.
CADRES = {"f43": 4.0 / 3.0, "f34": 3.0 / 4.0, "f11": 1.0, "f169": 16.0 / 9.0}


def _f32(v):
    return float(np.float32(v))


def _pas(v, k):
    """Valeur d'un curseur ramenée à son pas comme TableauUi : (it * k).toInt() / k, calcul en Float."""
    return int(np.float32(v) * np.float32(k)) / float(k)


def _f1(v):
    return f"{v:.1f}".replace(".", ",").replace(",0", "")


def _mm(v):
    return f"{v:.1f} mm".replace(".", ",").replace(",0 mm", " mm")


def _mm2(v):
    return f"{_f32(v):.2f} mm".replace(".", ",")


def _choix_int(params, cle, defaut):
    v = params.get(cle, defaut)
    try:
        return int(v)
    except (TypeError, ValueError):
        return defaut


def _nombre(params, cle, defaut, lo, hi):
    try:
        v = float(params.get(cle, defaut))
    except (TypeError, ValueError):
        raise ValueError(f"Réglage « {cle} » : nombre attendu.") from None
    if not math.isfinite(v):
        raise ValueError(f"Réglage « {cle} » : nombre attendu.")
    if v < lo or v > hi:
        raise ValueError(f"Réglage « {cle} » : entre {lo} et {hi}.")
    return v


def lire_photo(chemin):
    """Photo → tableau RGBA uint8, orientation EXIF appliquée, réduite à COTE_PHOTO_MAX ; refus explicite sinon."""
    try:
        with Image.open(chemin) as im:
            l, h = im.size
            if l * h > PIXELS_PHOTO_MAX:
                raise ValueError(f"Photo trop grande ({l} × {h} pixels) : {PIXELS_PHOTO_MAX // 1_000_000} millions de pixels au plus.")
            im = ImageOps.exif_transpose(im).convert("RGBA")
    except ValueError:
        raise
    except Image.DecompressionBombError:
        raise ValueError("Photo trop grande : réduis-la avant de l'envoyer.") from None
    except Exception:  # noqa: BLE001 - fichier illisible : message pour l'utilisateur
        raise ValueError("Photo illisible : envoie une image PNG, JPEG ou WebP.") from None
    if max(im.size) > COTE_PHOTO_MAX:
        s = COTE_PHOTO_MAX / max(im.size)
        im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.LANCZOS)
    return np.asarray(im, np.uint8)


def recadrage_centre(sw, sh, ratio):
    """Plus grand cadre centré de proportion ``ratio`` dans la photo (Recadreur de Gift sans zoom ni décalage)."""
    rp = sw / sh
    if rp > ratio:
        l = ratio / rp
        return K.Recadrage((1 - l) / 2, 0.0, max(1e-4, l), 1.0)
    h = rp / ratio
    return K.Recadrage(0.0, (1 - h) / 2, 1.0, max(1e-4, h))


def lire_couleurs(params):
    """Palette choisie (ou « Mes couleurs »), puis nombre de couleurs ajusté comme l'écran de Gift."""
    pid = str(params.get("palette") or C.PALETTE_DEFAUT.id)
    if pid == "perso":
        brutes = params.get("couleurs") or []
        teintes = []
        for v in brutes:
            h = C.normaliser_hex(v)
            if not h:
                raise ValueError(f"Couleur invalide : {str(v)[:12]!r} (#RRVVBB attendu).")
            teintes.append(C.Teinte(h, C.nom_connu(h)))
    elif pid in C.PALETTES_PAR_ID:
        teintes = list(C.PALETTES_PAR_ID[pid].couleurs)
    else:
        raise ValueError("Palette inconnue.")
    n = str(params.get("nombre_couleurs") or "palette")
    if n != "palette":
        try:
            n = int(n)
        except ValueError:
            raise ValueError("Nombre de couleurs : de 2 à 8.") from None
        if not C.COULEURS_MIN <= n <= C.COULEURS_MAX:
            raise ValueError("Nombre de couleurs : de 2 à 8.")
        teintes = teintes[:n]
        while len(teintes) < n:
            teintes.append(C.couleur_suivante(teintes))
    if not C.COULEURS_MIN <= len(teintes) <= C.COULEURS_MAX:
        raise ValueError("Choisis de 2 à 8 couleurs.")
    return tuple(teintes)


def lire_reglages(params, sw, sh):
    """Réglages de l'écran (mêmes pas que les curseurs de TableauUi) → (ParamTableau borné, recadrage)."""
    cadre = str(params.get("cadre") or "f11")
    if cadre in CADRES:
        ratio = _f32(CADRES[cadre])
    elif cadre == "libre":
        ratio = _f32(_nombre(params, "proportion", 1.3, 0.4, 2.5))
    elif cadre == "photo":
        ratio = sw / sh
    else:
        raise ValueError("Cadre inconnu.")
    forme = str(params.get("forme") or "rectangle")
    if forme not in K.FORMES:
        raise ValueError("Forme inconnue.")
    try:
        pixel = float(params.get("pixel_mm", 0.2))
    except (TypeError, ValueError):
        raise ValueError("Finesse du pixel inconnue.") from None
    if pixel not in K.PIXELS:
        raise ValueError("Finesse du pixel : 0,1 · 0,15 · 0,2 · 0,3 ou 0,4 mm.")
    largeur = float(int(_nombre(params, "largeur_mm", 150, 30, 300)))
    base = str(params.get("couleur_base") or "auto")
    p = K.ParamTableau(
        couleurs=lire_couleurs(params),
        ordre_auto=bool(params.get("ordre_auto", True)),
        luminosite=float(int(_nombre(params, "luminosite", 0, -100, 100))),
        contraste=float(int(_nombre(params, "contraste", 0, -100, 100))),
        gamma=_pas(_nombre(params, "gamma", 1.0, 0.4, 2.5), 20),
        repartition_egale=str(params.get("repartition") or "egale") != "paliers",
        largeur_mm=largeur,
        hauteur_mm=K.arrondi(largeur / ratio, 1),
        forme=forme,
        rayon_mm=float(int(_nombre(params, "rayon_mm", 8, 2, 40))),
        base_mm=_pas(_nombre(params, "base_mm", 2.0, 0.8, 4.0), 10),
        couche_mm=_pas(_nombre(params, "couche_mm", 0.6, 0.2, 1.6), 20),
        pixel_mm=pixel,
        face_dessous=bool(params.get("face_dessous", False)),
        couleur_base=-1 if base == "auto" else _choix_int(params, "couleur_base", 0) - 1,
    )
    return K.borner(p), recadrage_centre(sw, sh, ratio)


def _png(rgba, cote_max, filtre):
    im = Image.fromarray(rgba, "RGBA")
    if max(im.size) > cote_max:
        s = cote_max / max(im.size)
        im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), filtre)
    return im


def executer(params, inputs, out_dir, progress):
    progress = progress or (lambda *_: None)
    out_dir = Path(out_dir)
    params = dict(params or {})
    fmt = str(params.get("format") or "3mf")
    if fmt not in E.FORMATS:
        raise ValueError("Format de fichier inconnu.")
    photo = (inputs or {}).get("photo")
    if not photo or not Path(photo).is_file():
        raise ValueError("Photo manquante.")
    progress(0.02, "Lecture de la photo")
    src = lire_photo(photo)
    sh, sw = src.shape[:2]
    p, cadre = lire_reglages(params, sw, sh)
    dims = K.dimensions(p.largeur_mm, p.hauteur_mm, p.pixel_mm)
    plateau = K.plateau(dims.largeur_mm, dims.hauteur_mm)
    progress(0.06, "Aperçu des couleurs")
    apercu = K.carte_apercu(src, cadre, p, dims)
    progress(0.12, "Carte d'impression")
    prep = M.preparer(src, cadre, p, apercu.seuils, progress=lambda k: progress(0.12 + 0.02 * k, "Carte d'impression"))
    c = prep.carte
    titre = str(params.get("nom") or "").strip()[:60] or "Tableau couleur"
    rgba = K.rendre(c)
    _png(rgba, APERCU_COTE_MAX, Image.NEAREST).save(out_dir / "apercu.png", optimize=True)
    image = _png(rgba, 900, Image.NEAREST)
    s = 256 / max(image.size)
    tampon = io.BytesIO()
    image.resize((max(1, int(image.width * s)), max(1, int(image.height * s))), Image.BILINEAR).save(tampon, "PNG")
    vignette = tampon.getvalue()

    aire = c.pas_x * c.pas_y
    grammes_plaque = c.pixels_pleins * aire * p.base_mm / 1000 * K.DENSITE_PLA
    base = K.index_base(p, len(c.couleurs))
    lignes, filaments = [], []
    for i, t in enumerate(c.couleurs):
        g = c.comptes[i] * aire * p.couche_mm / 1000 * K.DENSITE_PLA
        lignes.append(f"Filament {i + 1} · {t.nom} {t.hex} : {_f1(g)} g" + (f" + plaque {_f1(grammes_plaque)} g" if i == base else ""))
        filaments.append({"filament": i + 1, "nom": t.nom, "hex": t.hex, "pixels": c.comptes[i],
                          "part": round(100.0 * c.comptes[i] / max(1, c.pixels_pleins), 1),
                          "grammes": round(g + (grammes_plaque if i == base else 0.0), 2), "plaque": i == base})
    total = sum(c.comptes) * aire * p.couche_mm / 1000 * K.DENSITE_PLA + grammes_plaque
    details = [
        f"{_mm(c.largeur_mm)} × {_mm(c.hauteur_mm)} · {K.FORMES[p.forme]} · {len(c.couleurs)} couleurs",
        f"Plaque {_mm2(p.base_mm)} + couleurs {_mm2(p.couche_mm)} · pixel {_mm2(c.pixel_mm)}"
        + (" · face contre le plateau" if p.face_dessous else ""),
        f"{K.entier_fr(prep.triangles)} triangles · ≈ {_f1(total)} g de PLA",
    ] + lignes + [x for x in (prep.avertissement, plateau.message if (not plateau.tient or plateau.tourne) else None) if x]
    details += K.conseils(p)
    libelle, suffixe, _ = E.FORMATS[fmt]
    if fmt != "3mf":
        details.append(f"Format : {libelle}")

    nom_base = E.nom_fichier(titre)
    if "tableau" not in nom_base:
        nom_base += "-tableau"
    nom = nom_base + suffixe
    chemin = out_dir / nom
    ecrire = lambda f, m: progress(0.3 + 0.68 * f, m)  # noqa: E731
    if fmt == "3mf":
        bilan = E.ecrire_3mf(chemin, titre, c, p, vignette, progress=ecrire)
        triangles = bilan.triangles
    elif fmt == "stl_couleurs":
        triangles = E.ecrire_stl_couleurs(chemin, titre, c, p, progress=ecrire)
    elif fmt == "stl_unique":
        triangles = E.ecrire_stl_unique(chemin, titre, c, p, progress=ecrire)
    else:
        triangles = E.ecrire_obj(chemin, titre, c, p, progress=ecrire)
    progress(1.0, "Terminé")
    return {
        "files": [{"name": nom, "label": libelle, "library": "impression"}],
        "preview": "apercu.png",
        "details": details,
        "summary": {
            "largeur_mm": c.largeur_mm, "hauteur_mm": c.hauteur_mm, "pixel_mm": c.pixel_mm,
            "pixels": [c.l, c.h], "forme": p.forme, "couleurs": len(c.couleurs), "filaments": filaments,
            "grammes_total": round(total, 2), "triangles": int(triangles), "format": fmt,
            "epaisseur_mm": round(p.base_mm + p.couche_mm, 3), "face_dessous": p.face_dessous,
            "plateau_ok": plateau.tient, "plateau_tourne": plateau.tourne, "pixel_elargi": prep.avertissement is not None,
        },
    }
