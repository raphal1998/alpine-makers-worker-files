"""Alpine Tools — Lithophanie en volume (atool-lithophanie-volume), portage d'Alpine Gift 1.0.8 (core LithoVolume.kt,
Triangulation.kt, Lithophanie.kt, Maillage.kt, ExportModele.kt ; ui LithoVolumeUi.kt, RenduVolume.kt).

Contrat : docs/ALPINE_TOOLS.md. TOOL est un littéral pur (lu par le site sans importer numpy) ; les imports lourds
sont faits dans run(). Une seule photo par job : la boîte « une photo par face » reçoit la même photo sur ses 4 faces.
"""

TOOL = {
    "id": "lithophanie-volume",
    "name": "Lithophanie en volume",
    "machine": "impression",
    "description": "Ta photo enroulée autour d'une lampe imprimée en 3D : cylindre, abat-jour, globe, cœur ou boîte, "
                   "allumée de l'intérieur (STL, 3MF, OBJ).",
    "inputs": [
        {"key": "photo", "label": "Photo", "accept": [".png", ".jpg", ".jpeg", ".webp"], "required": True, "max_mb": 25,
         "help": "Paysage, panorama, portrait de famille : la photo s'enroule autour de la forme et s'allume de "
                 "l'intérieur. 24 Mpx au plus."},
    ],
    "params": [
        {"key": "forme", "label": "Forme", "type": "choice", "default": "cylindre",
         "help": "Cylindre : photophore ou lampe. Abat-jour : cylindre ou cône tronqué, plaque de douille en option. "
                 "Globe : sphère posée sur son ouverture. Cœur creux : lanterne en cœur. Boîte 4 faces.",
         "options": [{"value": "cylindre", "label": "Cylindre"}, {"value": "abat_jour", "label": "Abat-jour"},
                     {"value": "globe", "label": "Globe"}, {"value": "coeur", "label": "Cœur creux"},
                     {"value": "boite", "label": "Boîte 4 faces"}]},
        {"key": "diametre_mm", "label": "Diamètre (cylindre, globe) ou diamètre du bas (abat-jour)", "type": "number",
         "min": 40, "max": 240, "step": 1, "default": 80, "unit": "mm",
         "help": "Cylindre 40 à 200 mm, abat-jour 60 à 240 mm, globe 60 à 200 mm."},
        {"key": "diametre_haut_mm", "label": "Diamètre du haut (abat-jour)", "type": "number", "min": 40, "max": 240,
         "step": 1, "default": 60, "unit": "mm", "help": "Égal au diamètre du bas : abat-jour droit."},
        {"key": "douille_mm", "label": "Plaque de douille (abat-jour)", "type": "choice", "default": "0",
         "help": "Plaque percée en haut pour la douille de l'ampoule.",
         "options": [{"value": "0", "label": "Sans plaque"}, {"value": "29", "label": "E14 · 29 mm"},
                     {"value": "42", "label": "E27 · 42 mm"}]},
        {"key": "ouverture_bas_mm", "label": "Ouverture du bas (globe, lumière)", "type": "number", "min": 20,
         "max": 120, "step": 1, "default": 40, "unit": "mm"},
        {"key": "ouverture_haut_mm", "label": "Ouverture du haut (globe)", "type": "number", "min": 0, "max": 80,
         "step": 1, "default": 0, "unit": "mm", "help": "Moins de 5 mm : calotte fermée."},
        {"key": "largeur_mm", "label": "Largeur du cœur ou côté de la boîte", "type": "number", "min": 50, "max": 220,
         "step": 1, "default": 80, "unit": "mm", "help": "Cœur 60 à 220 mm, boîte 50 à 200 mm."},
        {"key": "boite_photo", "label": "Photo de la boîte", "type": "choice", "default": "tour",
         "options": [{"value": "tour", "label": "Une photo qui fait le tour"},
                     {"value": "faces", "label": "La même photo sur chaque face"}]},
        {"key": "hauteur_mm", "label": "Hauteur (sauf globe)", "type": "number", "min": 40, "max": 220, "step": 1,
         "default": 100, "unit": "mm"},
        {"key": "fond", "label": "Fond plein (sauf globe)", "type": "bool", "default": False,
         "help": "Un fond fermé (photophore, boîte) ; sans fond : une lanterne à poser sur la lumière."},
        {"key": "epaisseur_min", "label": "Épaisseur minimale (zones claires)", "type": "number", "min": 0.5,
         "max": 1.6, "step": 0.05, "default": 0.8, "unit": "mm"},
        {"key": "epaisseur_max", "label": "Épaisseur maximale (zones sombres)", "type": "number", "min": 2.0,
         "max": 5.0, "step": 0.05, "default": 3.0, "unit": "mm"},
        {"key": "cadre_mm", "label": "Bandes pleines en haut et en bas", "type": "number", "min": 0.0, "max": 12.0,
         "step": 0.5, "default": 3.0, "unit": "mm",
         "help": "Bandes à l'épaisseur maximale (cadre de chaque face pour la boîte une photo par face) ; 0 = sans."},
        {"key": "pas_mm", "label": "Détail du relief", "type": "choice", "default": "0.25",
         "help": "Plus fin = plus de détails et un fichier plus gros (1 500 000 triangles au plus, le détail s'élargit "
                 "seul au-delà).",
         "options": [{"value": "0.25", "label": "0,25 mm"}, {"value": "0.3", "label": "0,30 mm"},
                     {"value": "0.4", "label": "0,40 mm"}, {"value": "0.6", "label": "0,60 mm"}]},
        {"key": "negatif", "label": "Négatif", "type": "bool", "default": False,
         "help": "Les zones claires deviennent épaisses (image inversée en lumière)."},
        {"key": "recadrage", "label": "Recadrage", "type": "choice", "default": "paroi",
         "help": "Cadre centré au format de la paroi (comme l'application), ou la photo entière étirée sur la paroi.",
         "options": [{"value": "paroi", "label": "Au format de la paroi (centré)"},
                     {"value": "entiere", "label": "Image entière (étirée)"}]},
        {"key": "luminosite", "label": "Luminosité", "type": "number", "min": -60, "max": 60, "step": 1, "default": 0,
         "unit": "%"},
        {"key": "contraste", "label": "Contraste", "type": "number", "min": -50, "max": 80, "step": 1, "default": 0,
         "unit": "%"},
        {"key": "gamma", "label": "Gamma", "type": "number", "min": 0.5, "max": 2.2, "step": 0.01, "default": 1.0},
        {"key": "format", "label": "Format du fichier", "type": "choice", "default": "stl",
         "help": "STL : lu par tous les trancheurs. 3MF (Bambu Studio, OrcaSlicer, PrusaSlicer) et OBJ (Blender) : "
                 "900 000 triangles au plus, choisis un détail de 0,4 mm et plus pour les grands volumes.",
         "options": [{"value": "stl", "label": "STL"}, {"value": "3mf", "label": "3MF"},
                     {"value": "obj", "label": "OBJ (ZIP avec MTL)"}]},
        {"key": "nom", "label": "Nom de la création (facultatif)", "type": "text", "max": 60, "default": ""},
    ],
}

_DEFAUTS = {spec["key"]: spec["default"] for spec in TOOL["params"]}
_BORNES = {spec["key"]: (spec["min"], spec["max"]) for spec in TOOL["params"] if spec["type"] == "number"}
_CHOIX = {spec["key"]: [o["value"] for o in spec["options"]] for spec in TOOL["params"] if spec["type"] == "choice"}
_LIBELLES = {spec["key"]: spec["label"] for spec in TOOL["params"]}
# Curseurs de l'application par forme (LithoVolumeUi.PanneauFormeVolume) : clé → (min, max, nom lisible).
_BORNES_FORME = {
    "cylindre": {"diametre_mm": (40, 200, "Diamètre du cylindre")},
    "abat_jour": {"diametre_mm": (60, 240, "Diamètre du bas de l'abat-jour"),
                  "diametre_haut_mm": (40, 240, "Diamètre du haut de l'abat-jour")},
    "globe": {"diametre_mm": (60, 200, "Diamètre du globe"), "ouverture_bas_mm": (20, 120, "Ouverture du bas"),
              "ouverture_haut_mm": (0, 80, "Ouverture du haut")},
    "coeur": {"largeur_mm": (60, 220, "Largeur du cœur")},
    "boite": {"largeur_mm": (50, 200, "Côté de la boîte")},
}
_VUE, _VUE_AZIMUT = 720, 30.0
_COTE_EXPORT, _COTE_APERCU, _TRIANGLES_APERCU = 2400, 420, 400_000


def _fr(v):
    return str(v).replace(".", ",")


def _nombre(p, cle):
    try:
        v = float(p[cle])
    except (TypeError, ValueError):
        raise ValueError(f"« {_LIBELLES[cle]} » : nombre attendu.") from None
    lo, hi = _BORNES[cle]
    if not (v == v and lo <= v <= hi):
        raise ValueError(f"« {_LIBELLES[cle]} » : entre {_fr(lo)} et {_fr(hi)}.")
    return v


def _booleen(v):
    return v.strip().lower() in ("1", "true", "oui", "on", "vrai") if isinstance(v, str) else bool(v)


def lire_params(params):
    """Paramètres revérifiés (bornes, choix, bornes de chaque forme) et quantifiés comme les curseurs de l'application.
    Rend (volume.Param, options)."""
    import math

    from . import volume

    p = dict(_DEFAUTS)
    p.update({k: v for k, v in (params or {}).items() if v is not None})
    inconnues = set(p) - set(_DEFAUTS)
    if inconnues:
        raise ValueError("Paramètre inconnu : " + ", ".join(sorted(inconnues)) + ".")
    for cle, valeurs in _CHOIX.items():
        if str(p[cle]) not in valeurs:
            raise ValueError(f"« {_LIBELLES[cle]} » : choix inconnu.")
        p[cle] = str(p[cle])
    for cle in _BORNES:
        p[cle] = _nombre(p, cle)
    forme = p["forme"]
    for cle, (lo, hi, nom) in _BORNES_FORME[forme].items():
        if not lo <= p[cle] <= hi:
            raise ValueError(f"{nom} de {lo} à {hi} mm.")

    def entier(cle):
        return float(math.floor(p[cle] + 0.5))

    ouverture_haut = entier("ouverture_haut_mm")
    nom = str(p["nom"] or "")
    if len(nom) > 60 or any(ord(ch) < 32 for ch in nom):
        raise ValueError("Nom : 60 caractères au plus, sans caractère de contrôle.")
    param = volume.Param(
        forme=forme,
        diametre=entier("diametre_mm"),
        diametre_haut=entier("diametre_haut_mm"),
        largeur=entier("largeur_mm"),
        hauteur=entier("hauteur_mm"),
        ep_min=math.floor(p["epaisseur_min"] * 20 + 0.5) / 20.0,
        ep_max=math.floor(p["epaisseur_max"] * 20 + 0.5) / 20.0,
        pas=float(p["pas_mm"]),
        negatif=_booleen(p["negatif"]),
        cadre=math.floor(p["cadre_mm"] * 2 + 0.5) / 2.0,
        fond=_booleen(p["fond"]) and forme != "globe",
        douille=float(p["douille_mm"]) if forme == "abat_jour" else 0.0,
        ouverture_bas=entier("ouverture_bas_mm"),
        ouverture_haut=0.0 if ouverture_haut < 5 else ouverture_haut,
    )
    if param.ep_max <= param.ep_min:
        raise ValueError("L'épaisseur maximale doit dépasser la minimale.")
    options = {
        "nb_images": 4 if (forme == "boite" and p["boite_photo"] == "faces") else 1,
        "recadrage": p["recadrage"],
        "luminosite": p["luminosite"] / 100.0,
        "contraste": p["contraste"] / 100.0,
        "gamma": p["gamma"],
        "format": p["format"],
        "nom": nom,
    }
    return param, options


def nom_sur(titre):
    """IndexCreations.nomSur : sans accents, [a-z0-9-], 40 caractères au plus (œ, æ, ß écrits oe, ae, ss : « cœur »
    donne « coeur » et non « c-ur » comme dans l'application)."""
    import re
    import unicodedata
    for lettre, ecrite in (("œ", "oe"), ("Œ", "OE"), ("æ", "ae"), ("Æ", "AE"), ("ß", "ss")):
        titre = titre.replace(lettre, ecrite)
    sans = "".join(ch for ch in unicodedata.normalize("NFD", titre) if not unicodedata.combining(ch))
    return (re.sub(r"[^A-Za-z0-9]+", "-", sans).strip("-")[:40] or "creation").lower()


def mm(v):
    """mm() de l'application : « 100 mm », « 74,8 mm »."""
    from .volume import fixe
    return (fixe(v, 1) + " mm").replace(",0 mm", " mm")


def mm2(v):
    """mm2(Float) de l'application : « 0,80 mm »."""
    import numpy as np

    from .volume import fixe
    return fixe(float(np.float32(v)), 2) + " mm"


def run(params, inputs, out_dir, progress):
    import dataclasses
    from pathlib import Path

    from . import exports
    from . import image as img
    from . import rendu
    from . import volume

    p, o = lire_params(params)
    nb = o["nb_images"]
    photo_chemin = (inputs or {}).get("photo")
    if not photo_chemin:
        raise ValueError("Photo manquante.")
    out_dir = Path(out_dir)
    volume.valider(p, nb)
    progress(0.02, "Lecture de la photo")
    photo = img.charger_photo(Path(photo_chemin))
    ratio = volume.proportion(p, nb) if o["recadrage"] == "paroi" else None
    cadre = img.cadre_centre(photo.width, photo.height, ratio)

    def gris(cote):
        g = img.gris(photo, cadre, cote, cote)
        return img.appliquer(g, luminosite=o["luminosite"], contraste=o["contraste"], gamma=o["gamma"])

    g = gris(_COTE_EXPORT)
    progress(0.05, "Préparation du volume")
    plan = volume.planifier(p, [g] * nb)
    n = plan.triangles()
    fmt = o["format"]
    if fmt != "stl" and n > exports.TRIANGLES_INDEXES_MAX:
        raise ValueError(f"{volume.entier(n)} triangles : trop lourd pour le {exports.COURT[fmt]} "
                         f"({volume.entier(exports.TRIANGLES_INDEXES_MAX)} triangles au plus). "
                         "Choisis STL, ou un détail de 0,4 mm et plus.")
    sommets, tris = plan.sommets(), plan.indices()
    if len(tris) != n:
        raise RuntimeError(f"Maillage incohérent : {len(tris)} triangles pour {n} annoncés.")
    progress(0.3, "Maillage fermé prêt")

    def sous(a, b):
        return lambda f, m="": progress(a + (b - a) * max(0.0, min(1.0, f)), m)

    libelle_forme = volume.FORMES[p.forme][0]
    titre = o["nom"].strip() or f"Lithophanie {libelle_forme.lower()}"
    base = nom_sur(titre)
    if "lithophanie" not in base:
        base += "-lithophanie"
    nom_fichier = base + exports.FORMATS[fmt][1]
    chemin = out_dir / nom_fichier
    sommets_fichier = None
    if fmt == "stl":
        ecrits = exports.ecrire_stl(sommets, tris, chemin, titre, progress=sous(0.3, 0.85))
    else:
        v_idx, t_idx = exports.maillage_indexe(sommets, tris)
        if len(t_idx) != n:
            raise RuntimeError(f"Maillage incohérent : {len(t_idx)} triangles indexés pour {n} annoncés.")
        sommets_fichier = len(v_idx)
        ecrire = exports.ecrire_3mf if fmt == "3mf" else exports.ecrire_obj
        ecrits = ecrire(chemin, titre, v_idx, t_idx, progress=sous(0.35, 0.85))
        del v_idx, t_idx
    if ecrits != n:
        raise RuntimeError(f"Maillage incohérent : {ecrits} triangles écrits pour {n} annoncés.")
    del sommets, tris

    # Aperçu : volume au pas d'aperçu (≈ 360 nœuds sur le tour, photo à 420 px, 400 000 triangles), vue 3D à 30°
    # et paroi déroulée rétroéclairée, comme l'écran de l'application.
    progress(0.88, "Aperçu")
    g_ap = gris(_COTE_APERCU)
    pa = dataclasses.replace(p, pas=volume.pas_apercu(p))
    try:
        plan_ap = volume.planifier(pa, [g_ap] * nb, _TRIANGLES_APERCU)
    except ValueError:
        plan_ap = volume.planifier(pa, [g_ap] * nb, _TRIANGLES_APERCU, apercu=True)
    vue = rendu.dessiner(plan_ap.sommets()[plan_ap.indices()], _VUE, _VUE, _VUE_AZIMUT)
    rendu.composer(vue, plan_ap.apercu_argb(), out_dir / "apercu.png")

    octets = chemin.stat().st_size
    details = [
        f"{libelle_forme} · {mm(plan.largeur_mm)} × {mm(plan.profondeur_mm)} × {mm(plan.hauteur_mm)}"
        + (f" · douille {int(p.douille + 0.5)} mm" if p.forme == "abat_jour" and p.douille > 0 else "")
        + (" · fond plein" if p.fond else ""),
        f"Épaisseur {mm2(p.ep_min)} à {mm2(p.ep_max)} · bandes {mm(p.cadre)} · {'négatif' if p.negatif else 'positif'}"
        + (" · une photo par face" if nb == 4 else ""),
        f"{volume.entier(n)} triangles (maillage fermé) · zone photo {mm(plan.perimetre_mm)} × {mm(plan.hauteur_image_mm)}",
    ] + plan.avertissements + volume.conseils(p) + ([f"Format : {exports.FORMATS[fmt][0]}"] if fmt != "stl" else [])
    progress(1.0, "Lithophanie en volume prête")
    return {
        "files": [{"name": nom_fichier, "label": {"stl": "STL", "3mf": "3MF", "obj": "OBJ (ZIP)"}[fmt],
                   "library": "impression"}],
        "preview": "apercu.png",
        "details": details,
        "summary": {"forme": p.forme, "format": fmt, "largeur_mm": round(plan.largeur_mm, 3),
                    "profondeur_mm": round(plan.profondeur_mm, 3), "hauteur_mm": round(plan.hauteur_mm, 3),
                    "triangles": n, "sommets": sommets_fichier, "octets": octets,
                    "pas_mm": round(plan.pas_reel, 4), "epaisseur_min_mm": p.ep_min, "epaisseur_max_mm": p.ep_max,
                    "bandes_mm": p.cadre, "fond": p.fond, "negatif": p.negatif, "douille_mm": p.douille,
                    "photos_par_face": nb == 4, "zone_photo_mm": [round(plan.perimetre_mm, 3),
                                                                  round(plan.hauteur_image_mm, 3)],
                    "anneaux": len(plan.anneaux), "sommets_par_anneau": plan.nu,
                    "avertissements": plan.avertissements},
    }
