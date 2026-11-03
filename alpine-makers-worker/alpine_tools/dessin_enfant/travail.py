# -*- coding: utf-8 -*-
"""Exécution d'un job « Dessin d'enfant » sur le Worker (équivalent de l'écran Dessin d'enfant d'Alpine Gift :
photo → cadrage → lecture → objet → fichiers)."""
import math
import re

from . import apercu, exports, feuille, image
from . import contours as G
from .fabrication import EMPORTE_PIECE, SORTIES, TAMPON, ParamFabrication, conseils, fabriquer
from .lecture import TAILLE_MAX, TAILLE_MIN, ParamDessin, analyser, format_fr

LARGEUR_CADRE = feuille.CADRE[2] - feuille.CADRE[0]
HAUTEUR_CADRE = feuille.CADRE[3] - feuille.CADRE[1]


def _arrondi(v):
    return int(math.floor(v + 0.5))


def _pas(v, k):
    """Valeur posée sur le pas du curseur de l'application ((v · k).roundToInt() / k)."""
    return _arrondi(float(v) * k) / k


def _num(params, cle, lo, hi, defaut):
    try:
        v = float(params.get(cle, defaut) if params.get(cle) is not None else defaut)
    except (TypeError, ValueError):
        raise ValueError(f"Réglage « {cle} » : nombre attendu.") from None
    if not math.isfinite(v) or v < lo or v > hi:
        raise ValueError(f"Réglage « {cle} » hors limites ({lo} à {hi}).")
    return v


def _nombres(texte, n, quoi):
    morceaux = [m for m in re.split(r"[;\s]+", (texte or "").strip()) if m]
    try:
        v = [float(m.replace(",", ".")) for m in morceaux]
    except ValueError:
        raise ValueError(f"{quoi} : {n} nombres attendus, en % de la photo.") from None
    if len(v) != n or any(not math.isfinite(x) or x < 0 or x > 100 for x in v):
        raise ValueError(f"{quoi} : {n} nombres entre 0 et 100 attendus, en % de la photo.")
    return [x / 100.0 for x in v]


def lire_coins(texte):
    """« x y; x y; x y; x y » (centres des repères en % de la photo : haut gauche, haut droite, bas droite, bas
    gauche) → 4 fractions, ou None si vide."""
    if not (texte or "").strip():
        return None
    v = _nombres(texte, 8, "Coins")
    return [(v[2 * k], v[2 * k + 1]) for k in range(4)]


def lire_recadrage(texte):
    """« gauche haut droite bas » en % de la photo → (u0, v0, u1, v1), ou toute la photo si vide."""
    if not (texte or "").strip():
        return (0.0, 0.0, 1.0, 1.0)
    u0, v0, u1, v1 = _nombres(texte, 4, "Recadrage")
    if u1 - u0 < 0.02 or v1 - v0 < 0.02:
        raise ValueError("Recadrage : la droite doit dépasser la gauche et le bas le haut (2 % au moins).")
    return (u0, v0, u1, v1)


def mm(v):
    return (format_fr(v, 1) + " mm").replace(",0 mm", " mm")


def mm2(v):
    return format_fr(v, 1) + " mm²"


def entier(n):
    return f"{int(n):,}".replace(",", " ")


def _image_dessin(photo, params, lecture, progress):
    """Image du dessin prête à lire et taille réelle (mm) ; rend aussi le mode et la ligne d'explication."""
    cadrage = str(params.get("cadrage", "auto"))
    if cadrage not in ("auto", "feuille", "libre"):
        raise ValueError("Cadrage inconnu.")
    coins = lire_coins(params.get("coins")) if cadrage != "libre" else None
    echelle = _pas(_num(params, "echelle", 25, 150, 100) / 5, 1) * 5 / 100.0
    largeur_libre = float(_arrondi(_num(params, "largeur_libre_mm", 20, 200, 80)))
    if cadrage != "libre":
        g = image.gris(photo, cote_max=image.COTE_FEUILLE)
        h, l = g.shape
        if coins:
            centres, mode = [(u * l, v * h) for u, v in coins], "coins"
        else:
            progress(0.04, "Recherche des repères de la feuille")
            centres, mode = feuille.detecter(g), "feuille"
        if centres is not None:
            progress(0.08, "Redressement de la feuille")
            r = feuille.redresser(g, centres, feuille.PAS_REDRESSE)
            p = ParamDessin(LARGEUR_CADRE * echelle, HAUTEUR_CADRE * echelle, **lecture)
            texte = ("Feuille de repères trouvée" if mode == "feuille" else "Coins de la feuille donnés à la main") + \
                f" : dessin redressé à sa vraie taille (cadre 160 × 240 mm, échelle {_arrondi(echelle * 100)} %)."
            return r, p, mode, texte
        if cadrage == "feuille":
            raise ValueError("Repères de la feuille non trouvés : photographie la feuille entière (les quatre carrés noirs "
                             "visibles), indique les coins à la main ou choisis le cadrage libre.")
    cadre = lire_recadrage(params.get("recadrage"))
    g = image.gris(photo, cadre, image.COTE_LIBRE)
    h, l = g.shape
    hauteur = min(max(largeur_libre * h / l, TAILLE_MIN), TAILLE_MAX)
    p = ParamDessin(largeur_libre, hauteur, **lecture)
    texte = f"Cadrage libre : la photo{' recadrée' if cadre != (0.0, 0.0, 1.0, 1.0) else ''} mesure {mm(largeur_libre)} de large."
    if cadrage == "auto":
        texte = ("Repères de la feuille non trouvés : " + texte[0].lower() + texte[1:] +
                 " Pour la vraie taille, photographie le dessin sur la feuille de repères.")
    return g, p, "libre", texte


def _feuille(out_dir, fichiers):
    (out_dir / "feuille-reperes-a4.svg").write_text(feuille.feuille_svg(), encoding="utf-8")
    (out_dir / "feuille-reperes-a4.pdf").write_bytes(feuille.feuille_pdf())
    fichiers.append({"name": "feuille-reperes-a4.pdf", "label": "Feuille de repères A4 (PDF)", "library": None})
    fichiers.append({"name": "feuille-reperes-a4.svg", "label": "Feuille de repères A4 (SVG)", "library": None})
    return "Feuille de repères A4 jointe : imprime-la à 100 % (sans « ajuster à la page »), dessine au feutre noir dans " \
           "le cadre, puis photographie la feuille entière, à plat."


def _dans(boucles, x, y):
    d = False
    for b in boucles:
        if G.contient(b, x, y):
            d = not d
    return d


def executer(params, inputs, out_dir, progress):
    params = dict(params or {})
    joindre = bool(params.get("feuille_reperes", False))
    fichiers, details = [], []
    chemin = (inputs or {}).get("photo")
    if chemin is None:
        if not joindre:
            raise ValueError("Ajoute la photo du dessin (ou coche « Joindre la feuille de repères » pour l'obtenir seule).")
        details.append(_feuille(out_dir, fichiers))
        progress(1.0, "Feuille de repères prête")
        return {"files": fichiers, "preview": None, "details": details, "summary": {"feuille_seule": True}}

    sortie = str(params.get("objet", EMPORTE_PIECE))
    if sortie not in SORTIES:
        raise ValueError("Objet inconnu.")
    fmt = str(params.get("format", "3mf"))
    if fmt not in exports.FORMATS:
        raise ValueError("Format de fichier inconnu.")
    seuil_auto = bool(params.get("seuil_auto", True))
    lecture = {
        "seuil": -1.0 if seuil_auto else _arrondi(_num(params, "seuil", 10, 90, 50)) / 100.0,
        "inverser": bool(params.get("inverser", False)),
        "nettoyage_mm2": _pas(_num(params, "nettoyage_mm2", 0, 10, 1.0), 2),
        "lissage_mm": _pas(_num(params, "raccord_mm", 0, 2, 0.3), 10),
        "trait_min_mm": _pas(_num(params, "trait_min_mm", 0, 2.5, 0.9), 10),
    }
    aimant = _num(params, "aimant_mm", 4, 30, 10)
    f = ParamFabrication(
        sortie=sortie,
        hauteur_coupe_mm=float(_arrondi(_num(params, "hauteur_mm", 10, 25, 14))),
        paroi_mm=_pas(_num(params, "paroi_mm", 1.2, 2.4, 1.6), 10),
        rebord_mm=_pas(_num(params, "rebord_mm", 2, 8, 4.0), 2),
        socle_mm=_pas(_num(params, "socle_mm", 2, 8, 3.0), 5),
        relief_mm=_pas(_num(params, "relief_mm", 0.6, 3, 1.2), 5),
        marge_mm=_pas(_num(params, "marge_mm", 1, 6, 2.5), 2),
        diametre_anneau_mm=_pas(_num(params, "anneau_mm", 3, 8, 4.5), 2),
        diametre_aimant_mm=aimant,
        profondeur_aimant_mm=_pas(_num(params, "profondeur_aimant_mm", 1, 4, 2.2), 10))
    base_hex = apercu.couleur(params.get("couleur_base"), "#F2EEE8")
    traits_hex = apercu.couleur(params.get("couleur_traits"), "#D8333F")
    hexa = ["#%02X%02X%02X" % tuple(int(c) for c in base_hex), "#%02X%02X%02X" % tuple(int(c) for c in traits_hex)]

    progress(0.02, "Lecture de la photo")
    photo = image.charger_photo(chemin)
    dessin, p, mode, texte_cadrage = _image_dessin(photo, params, lecture, progress)
    del photo
    a = analyser(dessin, p, lambda x, m="": progress(0.1 + 0.45 * x, m))
    r = fabriquer(a, f, lambda x, m="": progress(0.55 + 0.3 * x, m))

    libelle = SORTIES[sortie][0]
    titre = str(params.get("nom") or "").strip()[:60] or f"{libelle} d'après un dessin"
    base = exports.nom_fichier(titre)
    noms = ["Socle", "Traits"] if len(r.pieces) >= 2 else [r.pieces[0][0]]
    teintes = [(noms[i], hexa[i]) for i in range(len(r.pieces))] if len(r.pieces) >= 2 else [(noms[0], hexa[0])]
    progress(0.88, "Écriture du fichier")
    fmt_libelle, suffixe, _ = exports.FORMATS[fmt]
    nom = base + suffixe
    if fmt == "3mf":
        exports.ecrire_3mf(out_dir / nom, titre, teintes, r.pieces)
    elif fmt == "stl_unique":
        exports.ecrire_stl(out_dir / nom, titre, r.tout)
    elif fmt == "stl_couleurs":
        exports.ecrire_stl_couleurs(out_dir / nom, titre, teintes, r.pieces)
    else:
        exports.ecrire_obj(out_dir / nom, titre, teintes, r.pieces)
    fichiers.append({"name": nom, "label": fmt_libelle, "library": "impression"})

    progress(0.94, "Aperçus")
    if sortie == EMPORTE_PIECE:
        traits = []
    elif sortie == TAMPON:
        b = G.boite(r.contours)
        sym = b[0] + b[2]
        traits = [G.boucle(sym - t[:, 0], t[:, 1]) for t in a.contours_traits]
    else:
        traits = list(a.contours_traits)
    traits = [t for t in traits if _dans(r.contours, float(t[0, 0]), float(t[0, 1]))]
    apercu.objet(out_dir / "apercu.png", r.contours, traits, r.hauteur_mm, base_hex, traits_hex)
    apercu.lecture(out_dir / "lecture.png", a, 600)
    fichiers.append({"name": "lecture.png", "label": "Lecture du dessin (PNG)", "library": None})

    details.append(f"{libelle} · {mm(r.largeur_mm)} × {mm(r.profondeur_mm)} × {mm(r.hauteur_mm)}")
    details.append(texte_cadrage)
    details.append(f"Seuil {'automatique' if seuil_auto else 'choisi'} : {_arrondi(a.seuil * 100)} % · traits {mm2(a.aire_traits)}"
                   f" · silhouette {mm2(a.aire_silhouette)}")
    details.append(" · ".join(f"{nom_piece} ({teintes[min(i, len(teintes) - 1)][1]})" for i, (nom_piece, _) in enumerate(r.pieces)))
    if r.changement_couleur is not None:
        details.append(f"Changement de filament à {format_fr(r.changement_couleur)} mm (une seule buse).")
    ferme = bool(r.controle["ferme"])
    details.append(f"{entier(r.tout.nb_triangles)} triangles ({'maillage fermé' if ferme else 'maillage NON fermé'})")
    details += r.avertissements
    details += conseils(f)
    if fmt != "3mf":
        details.append(f"Format : {fmt_libelle}")
    if joindre:
        details.append(_feuille(out_dir, fichiers))
    progress(1.0, "Terminé")
    return {
        "files": fichiers,
        "preview": "apercu.png",
        "details": details,
        "summary": {
            "objet": sortie, "largeur_mm": round(r.largeur_mm, 2), "profondeur_mm": round(r.profondeur_mm, 2),
            "hauteur_mm": round(r.hauteur_mm, 2), "triangles": r.tout.nb_triangles, "ferme": ferme,
            "pieces": [n for n, _ in r.pieces], "changement_couleur_mm": None if r.changement_couleur is None else round(r.changement_couleur, 2),
            "cadrage": mode, "dessin_mm": [round(p.largeur_mm, 2), round(p.hauteur_mm, 2)], "seuil": round(a.seuil, 4),
            "aire_traits_mm2": round(a.aire_traits, 2), "aire_silhouette_mm2": round(a.aire_silhouette, 2),
            "miroir": sortie in (EMPORTE_PIECE, TAMPON), "avertissements": len(r.avertissements), "format": fmt,
        },
    }

