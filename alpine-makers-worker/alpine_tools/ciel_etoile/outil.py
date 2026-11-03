# -*- coding: utf-8 -*-
"""Déroulé d'un job Ciel étoilé (EcranCiel de Gift : exporterCielLaser / exporterCielPlaque) : réglages → carte du
ciel → projet Alpine Laser Studio + SVG + PNG (gravure laser) ou 3MF / STL / OBJ (plaque 3D multicolore), aperçu PNG
et détails (dimensions, date, étoiles visibles, astres, conseils, attribution du catalogue)."""
import io
import math
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import numpy as np
from PIL import Image

from . import catalogue as K
from . import ciel as C
from . import fuseaux as Z
from . import laser as L
from .carte import ParamTableau, entier_fr, rendre
from .couleurs import Teinte, nom_connu, normaliser_hex, rgb
from .exports import FORMATS, ecrire_3mf, ecrire_obj, ecrire_stl_couleurs, ecrire_stl_unique, nom_fichier
from .maillage import TRIANGLES_MAX, compter_tout

# Couleurs par défaut de la plaque 3D : fond, étoiles, constellations, textes (CielUi.COULEURS_CIEL).
COULEURS_CIEL = (("#13234A", "Bleu nuit"), ("#F4F4F4", "Blanc"), ("#D3A63A", "Or"), ("#F0E2C6", "Crème"))
NOMS_FILAMENTS = ("fond", "étoiles", "constellations", "textes")
GRAISSES = ("fine", "normale", "grasse")
APERCU_COTE_MAX = 1200


def _nombre(params, cle, defaut, lo, hi):
    try:
        v = float(params.get(cle, defaut))
    except (TypeError, ValueError):
        raise ValueError(f"Réglage « {cle} » : nombre attendu.") from None
    if not math.isfinite(v) or v < lo or v > hi:
        raise ValueError(f"Réglage « {cle} » : entre {lo} et {hi}.")
    return v


def _arrondi_pas(v, k):
    """(it · k).roundToInt() / k des curseurs de CielUi."""
    return math.floor(np.float32(v) * np.float32(k) + 0.5) / k


def _fr(v, decimales):
    return format(Decimal(repr(float(v))).quantize(Decimal(1).scaleb(-decimales), rounding=ROUND_HALF_UP), "f").replace(".", ",")


def mm(v):
    """mm() de Gift : « 160 mm », « 181,5 mm »."""
    return (_fr(v, 1) + " mm").replace(",0 mm", " mm")


def mm2(v):
    """mm2(Float) de Gift : « 1,60 mm »."""
    return format(Decimal(str(np.float32(v))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f").replace(".", ",") + " mm"


def lire_couleurs(params):
    brutes = params.get("couleurs") or [h for h, _ in COULEURS_CIEL]
    if len(brutes) != 4:
        raise ValueError("Quatre couleurs attendues : fond, étoiles, constellations, textes.")
    teintes = []
    for i, v in enumerate(brutes):
        h = normaliser_hex(v)
        if not h:
            raise ValueError(f"Couleur invalide : {str(v)[:12]!r} (#RRVVBB attendu).")
        nom = COULEURS_CIEL[i][1] if h == COULEURS_CIEL[i][0] else nom_connu(h)
        teintes.append(Teinte(h, nom))
    return teintes


def lire_param(params, maintenant_ms=None):
    """Réglages de l'écran (mêmes bornes et pas que CielUi) → ParamCiel."""
    zone = str(params.get("fuseau") or "Europe/Zurich")
    if zone not in dict(Z.FUSEAUX):
        raise ValueError("Fuseau horaire inconnu.")
    if maintenant_ms is None:
        maintenant_ms = int(time.time() * 1000)
    annee, mois, jour = Z.lire_date(params.get("date"), Z.aujourdhui(zone, maintenant_ms))
    heure, minute = Z.lire_heure(params.get("heure") or "22:00")
    forme = str(params.get("forme") or "affiche")
    if forme not in C.FORMES:
        raise ValueError("Forme inconnue.")
    lat = _nombre(params, "latitude", 46.2331, -90.0, 90.0)
    lon = _nombre(params, "longitude", 7.3606, -180.0, 180.0)
    return C.ParamCiel(
        instant_ms=Z.instant_ms(annee, mois, jour, heure, minute, zone), zone=zone, lat=lat, lon=lon,
        lieu=str(params.get("lieu") or "").strip()[:60],
        diametre_mm=float(math.floor(_nombre(params, "diametre_mm", 160, 80, 300) + 0.5)),
        forme=forme,
        mag_limite=_arrondi_pas(_nombre(params, "magnitude", 5.5, 3.5, 6.5), 10),
        taille_etoiles=_arrondi_pas(_nombre(params, "taille_etoiles", 1.0, 0.6, 1.8), 20),
        lignes=bool(params.get("lignes", True)), noms=bool(params.get("noms", False)),
        cardinaux=bool(params.get("cardinaux", True)), planetes=bool(params.get("planetes", True)),
        lune=bool(params.get("lune", True)),
        titre=str(params.get("titre") if params.get("titre") is not None else "Notre ciel")[:40],
        ligne_date=bool(params.get("ligne_date", True)), message=str(params.get("message") or "")[:60],
    )


def polices(params):
    graisse = str(params.get("graisse_titre") or "grasse")
    if graisse not in GRAISSES:
        raise ValueError("Graisse du titre inconnue.")
    return C.Police("normale"), C.Police(graisse)


def details_ciel(c, p):
    """detailsCiel de Gift."""
    astres = ", ".join(a.nom for a in c.astres)
    return [
        f"{mm(c.largeur_mm)} × {mm(c.hauteur_mm)} · ciel de {mm(p.diametre_mm)} · {C.FORMES[p.forme]}",
        f"{C.date_lisible(p)} · {p.lieu} ({C.coordonnees(p.lat, p.lon)})",
        f"{entier_fr(c.etoiles_visibles)} étoiles jusqu'à la magnitude {_fr(p.mag_limite, 1)}" + (f" · {astres}" if astres else ""),
    ]


def _image_rgba(c, sortie, style, materiau, couleurs):
    """rendreCiel de Gift : trame en couleurs (plaque) ou sur le matériau (laser), plaque détourée (transparent)."""
    largeur_px = max(1, min(900, int(APERCU_COTE_MAX * c.largeur_mm / c.hauteur_mm)))
    pas = c.largeur_mm / largeur_px
    t = C.trame(c, pas)
    rgba = np.zeros((t.h, t.l, 4), np.uint8)
    plein = t.v != C.VIDE
    if sortie == "plaque":
        for i, teinte in enumerate(couleurs):
            rgba[t.v == i, :3] = rgb(teinte.hex)
    else:
        _lib, matiere, trace = L.MATERIAUX[materiau]
        px, _l, _h = C.gravure(c, 1.0 / pas, style, t)
        rgba[..., :3] = L._rvb(matiere)
        rgba[px == 0, :3] = L._rvb(trace)
    rgba[plein, 3] = 255
    return Image.fromarray(rgba, "RGBA")


def _sauver_apercu(image, chemin):
    if max(image.size) > APERCU_COTE_MAX:
        s = APERCU_COTE_MAX / max(image.size)
        image = image.resize((max(1, int(image.width * s)), max(1, int(image.height * s))), Image.NEAREST)
    image.save(chemin, optimize=True)


def _resume(c, p, sortie):
    return {
        "sortie": sortie, "largeur_mm": round(c.largeur_mm, 3), "hauteur_mm": round(c.hauteur_mm, 3),
        "diametre_mm": p.diametre_mm, "forme": p.forme, "etoiles_visibles": c.etoiles_visibles,
        "astres": [{"nom": a.nom, "hauteur": round(a.hauteur, 2), "azimut": round(a.azimut, 2)} for a in c.astres],
        "instant_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(p.instant_ms // 1000)) if p.instant_ms >= 0 else None,
        "instant_ms": p.instant_ms, "date": C.date_lisible(p), "fuseau": p.zone, "lieu": p.lieu,
        "latitude": p.lat, "longitude": p.lon, "magnitude": p.mag_limite, "lignes_texte": list(c.lignes_texte),
    }


def executer(params, inputs, out_dir, progress, maintenant_ms=None):
    progress = progress or (lambda *_: None)
    out_dir = Path(out_dir)
    params = dict(params or {})
    sortie = str(params.get("sortie") or "laser")
    if sortie not in ("laser", "plaque"):
        raise ValueError("Sortie inconnue.")
    p = lire_param(params, maintenant_ms)
    police, police_titre = polices(params)
    nom = str(params.get("nom") or "").strip()[:60]
    titre = nom or p.titre.strip() or "Ciel étoilé"
    base = nom_fichier(titre)
    progress(0.03, "Carte du ciel")
    if sortie == "laser":
        return _laser(params, p, police, police_titre, titre, base, out_dir, progress)
    return _plaque(params, p, police, police_titre, titre, base, out_dir, progress)


def _laser(params, p, police, police_titre, titre, base, out_dir, progress):
    style = str(params.get("style") or "etoiles")
    if style not in C.STYLES:
        raise ValueError("Style de gravure inconnu.")
    materiau = str(params.get("materiau") or "bois_clair")
    if materiau not in L.MATERIAUX:
        raise ValueError("Matériau inconnu.")
    cle = str(params.get("resolution") or "10")
    if cle not in L.RESOLUTIONS:
        raise ValueError("Résolution : 10, 12,7 ou 20 px/mm.")
    res = L.RESOLUTIONS[cle]
    c = C.calculer(p, police, police_titre, False, lambda f: progress(0.03 + 0.25 * f, "Carte du ciel"))
    progress(0.32, "Gravure")
    px, l, h = C.gravure(c, res, style)
    r = {"largeur_px": l, "hauteur_px": h, "largeur_mm": l / res, "hauteur_mm": h / res, "px_par_mm": res}
    progress(0.6, "Image PNG")
    png = L.png_gris(px, res)
    libelle = L.MATERIAUX[materiau][0]
    progress(0.75, "Projet Alpine Laser Studio")
    projet = L.projet(titre, r, png, libelle, "Ciel étoilé (Alpine Tools)", [C.decoupe_document(c)], "atool-ciel")
    texte_svg = C.svg(c, titre)
    part_gravee = float(np.count_nonzero(px == 0)) / px.size
    del px
    ppp = L.ppp(res)
    noms = (f"{base}.alsproj", f"{base}-laser.svg", f"{base}-{ppp}ppp.png")
    (out_dir / noms[0]).write_text(projet, encoding="utf-8")
    (out_dir / noms[1]).write_text(texte_svg, encoding="utf-8")
    (out_dir / noms[2]).write_bytes(png)
    progress(0.9, "Aperçu")
    _sauver_apercu(_image_rgba(c, "laser", style, materiau, None), out_dir / "apercu.png")
    details = details_ciel(c, p) + [
        f"{l} × {h} px · {_fr(res, 1)} px/mm ({ppp} ppp) · {C.STYLES[style]} · {libelle}"
    ] + C.conseils_laser(style) + list(c.avertissements) + [K.ATTRIBUTION]
    resume = _resume(c, p, "laser")
    resume.update({"style": style, "materiau": materiau, "px_par_mm": res, "ppp": ppp, "pixels": [l, h],
                   "part_gravee": round(part_gravee, 4)})
    progress(1.0, "Terminé")
    return {
        "files": [{"name": noms[0], "label": "Projet Alpine Laser Studio", "library": "laser"},
                  {"name": noms[1], "label": "SVG laser (LightBurn)", "library": "laser"},
                  {"name": noms[2], "label": f"Image seule (PNG {ppp} ppp)", "library": "laser"}],
        "preview": "apercu.png", "details": details, "summary": resume,
    }


def _plaque(params, p, police, police_titre, titre, base, out_dir, progress):
    fmt = str(params.get("format") or "3mf")
    if fmt not in FORMATS:
        raise ValueError("Format de fichier inconnu.")
    couleurs = lire_couleurs(params)
    base_mm = _arrondi_pas(_nombre(params, "base_mm", 1.6, 1.0, 4.0), 10)
    couche_mm = _arrondi_pas(_nombre(params, "couche_mm", 0.6, 0.4, 1.2), 20)
    face_dessous = bool(params.get("face_dessous", True))
    c = C.calculer(p, police, police_titre, True, lambda f: progress(0.03 + 0.15 * f, "Carte du ciel"))
    progress(0.2, "Carte des couleurs")
    pixel = 0.2
    carte = C.carte_3d(c, pixel, couleurs)
    triangles = compter_tout(carte)
    while triangles > TRIANGLES_MAX and pixel < 0.6:
        pixel += 0.05
        carte = C.carte_3d(c, pixel, couleurs)
        triangles = compter_tout(carte)
    if triangles > TRIANGLES_MAX:
        raise ValueError("Carte trop chargée : réduis le diamètre ou le nombre d'étoiles.")
    pt = ParamTableau(couleurs=tuple(couleurs), ordre_auto=False, base_mm=base_mm, couche_mm=couche_mm, pixel_mm=pixel,
                      face_dessous=face_dessous, couleur_base=0)
    image = Image.fromarray(rendre(carte), "RGBA")
    if image.width > 900:
        image = image.resize((900, max(1, image.height * 900 // image.width)), Image.NEAREST)
    s = 256 / max(image.size)
    tampon = io.BytesIO()
    image.resize((max(1, int(image.width * s)), max(1, int(image.height * s))), Image.BILINEAR).save(tampon, "PNG")
    _sauver_apercu(_image_rgba(c, "plaque", None, None, couleurs), out_dir / "apercu.png")
    libelle, suffixe, _desc = FORMATS[fmt]
    nom = base + suffixe
    chemin = out_dir / nom
    ecrire = lambda f, m: progress(0.3 + 0.68 * f, m)  # noqa: E731
    if fmt == "3mf":
        ecrire_3mf(chemin, titre, carte, pt, tampon.getvalue(), progress=ecrire)
    elif fmt == "stl_couleurs":
        ecrire_stl_couleurs(chemin, titre, carte, pt, progress=ecrire)
    elif fmt == "stl_unique":
        ecrire_stl_unique(chemin, titre, carte, pt, progress=ecrire)
    else:
        ecrire_obj(chemin, titre, carte, pt, progress=ecrire)
    details = details_ciel(c, p) + [
        f"Plaque {mm2(base_mm)} + couleurs {mm2(couche_mm)} · pixel {mm2(pixel)} · {entier_fr(triangles)} triangles"
        + (" · face contre le plateau" if face_dessous else ""),
    ] + [f"Filament {i + 1} · {NOMS_FILAMENTS[i]} : {t.nom} {t.hex}" for i, t in enumerate(couleurs)]
    if fmt != "3mf":
        details.append(f"Format : {libelle}")
    details += C.conseils_3d() + list(c.avertissements) + [K.ATTRIBUTION]
    resume = _resume(c, p, "plaque")
    resume.update({"format": fmt, "pixel_mm": round(pixel, 3), "pixels": [carte.l, carte.h], "triangles": int(triangles),
                   "comptes": list(carte.comptes), "epaisseur_mm": round(base_mm + couche_mm, 3), "face_dessous": face_dessous,
                   "couleurs": [{"filament": i + 1, "role": NOMS_FILAMENTS[i], "nom": t.nom, "hex": t.hex}
                                for i, t in enumerate(couleurs)]})
    progress(1.0, "Terminé")
    return {"files": [{"name": nom, "label": libelle, "library": "impression"}], "preview": "apercu.png",
            "details": details, "summary": resume}
