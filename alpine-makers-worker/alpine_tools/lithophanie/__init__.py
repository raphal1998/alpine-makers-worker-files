"""Alpine Tools — Lithophanie (atool-lithophanie), portage d'Alpine Gift (Lithophanie.kt, Image.kt, LithophanieUi.kt).

Contrat : docs/ALPINE_TOOLS.md. TOOL est un littéral pur (lu par le site sans importer numpy) ; les imports lourds
sont faits dans run().
"""

TOOL = {
    "id": "lithophanie",
    "name": "Lithophanie",
    "machine": "impression",
    "description": "Ta photo devient un tableau de lumière imprimé en 3D : plus c'est sombre, plus c'est épais (STL).",
    "inputs": [
        {"key": "photo", "label": "Photo", "accept": [".png", ".jpg", ".jpeg", ".webp"], "required": True, "max_mb": 25,
         "help": "Portrait, paysage, animal : les visages bien éclairés et contrastés donnent les plus belles lithophanies. 24 Mpx au plus."},
    ],
    "params": [
        {"key": "format", "label": "Recadrage", "type": "choice", "default": "auto",
         "help": "Cadre centré au format choisi, ou la photo entière.",
         "options": [{"value": "auto", "label": "Automatique (4:3, 1:1 pour un cercle)"},
                     {"value": "4:3", "label": "4:3"}, {"value": "3:4", "label": "3:4"},
                     {"value": "1:1", "label": "1:1"}, {"value": "16:9", "label": "16:9"},
                     {"value": "libre", "label": "Libre"}, {"value": "entiere", "label": "Image entière"}]},
        {"key": "ratio_libre", "label": "Proportion (largeur / hauteur)", "type": "number", "min": 0.4, "max": 2.5,
         "step": 0.01, "default": 1.3, "help": "Utilisée avec le recadrage « Libre »."},
        {"key": "forme", "label": "Forme", "type": "choice", "default": "rectangle",
         "options": [{"value": "rectangle", "label": "Rectangle"}, {"value": "arrondi", "label": "Arrondi"},
                     {"value": "cercle", "label": "Cercle"}, {"value": "coeur", "label": "Cœur"}]},
        {"key": "largeur_mm", "label": "Largeur", "type": "number", "min": 40, "max": 220, "step": 1, "default": 100,
         "unit": "mm"},
        {"key": "epaisseur_min", "label": "Épaisseur minimale (zones claires)", "type": "number", "min": 0.4, "max": 1.6,
         "step": 0.05, "default": 0.8, "unit": "mm"},
        {"key": "epaisseur_max", "label": "Épaisseur maximale (zones sombres)", "type": "number", "min": 2.0, "max": 5.0,
         "step": 0.05, "default": 3.2, "unit": "mm"},
        {"key": "cadre_mm", "label": "Cadre", "type": "number", "min": 0.0, "max": 8.0, "step": 0.25, "default": 3.0,
         "unit": "mm", "help": "Bord plein à l'épaisseur maximale ; 0 = sans cadre."},
        {"key": "pas_mm", "label": "Détail du relief", "type": "choice", "default": "0.2",
         "help": "Plus fin = plus de détails et un fichier plus gros (1 500 000 triangles au plus, le détail s'élargit seul au-delà).",
         "options": [{"value": "0.15", "label": "0,15 mm"}, {"value": "0.2", "label": "0,20 mm"},
                     {"value": "0.3", "label": "0,30 mm"}, {"value": "0.4", "label": "0,40 mm"}]},
        {"key": "luminosite", "label": "Luminosité", "type": "number", "min": -60, "max": 60, "step": 1, "default": 0,
         "unit": "%"},
        {"key": "contraste", "label": "Contraste", "type": "number", "min": -50, "max": 80, "step": 1, "default": 0,
         "unit": "%"},
        {"key": "gamma", "label": "Gamma", "type": "number", "min": 0.5, "max": 2.2, "step": 0.01, "default": 1.0},
        {"key": "negatif", "label": "Négatif", "type": "bool", "default": False,
         "help": "Les zones claires deviennent épaisses (image inversée en lumière)."},
        {"key": "nom", "label": "Nom de la création (facultatif)", "type": "text", "max": 60, "default": ""},
    ],
}

_DEFAUTS = {spec["key"]: spec["default"] for spec in TOOL["params"]}
_BORNES = {spec["key"]: (spec["min"], spec["max"]) for spec in TOOL["params"] if spec["type"] == "number"}
_CHOIX = {spec["key"]: [o["value"] for o in spec["options"]] for spec in TOOL["params"] if spec["type"] == "choice"}
_APERCU_LARGEUR, _APERCU_MAX = 900, 1200


_LIBELLES = {spec["key"]: spec["label"] for spec in TOOL["params"]}


def _nombre(p, cle):
    try:
        v = float(p[cle])
    except (TypeError, ValueError):
        raise ValueError(f"« {_LIBELLES[cle]} » : nombre attendu.") from None
    lo, hi = _BORNES[cle]
    if not (v == v and lo <= v <= hi):
        bornes = [str(b).replace(".", ",") for b in (lo, hi)]
        raise ValueError(f"« {_LIBELLES[cle]} » : entre {bornes[0]} et {bornes[1]}.")
    return v


def lire_params(params):
    """Paramètres revérifiés (bornes, choix) et quantifiés comme les curseurs de l'application."""
    p = dict(_DEFAUTS)
    p.update({k: v for k, v in (params or {}).items() if v is not None})
    inconnues = set(p) - set(_DEFAUTS)
    if inconnues:
        raise ValueError("Paramètre inconnu : " + ", ".join(sorted(inconnues)) + ".")
    for cle, valeurs in _CHOIX.items():
        if str(p[cle]) not in valeurs:
            raise ValueError(f"« {cle} » : choix inconnu.")
        p[cle] = str(p[cle])
    for cle in _BORNES:
        p[cle] = _nombre(p, cle)
    # Curseurs de l'application : largeur entière, épaisseurs au 1/20 mm, cadre au 1/4 mm (« sans » sous 0,25).
    p["largeur_mm"] = float(int(round(p["largeur_mm"], 6)))
    p["epaisseur_min"] = int(round(p["epaisseur_min"] * 20, 6)) / 20.0
    p["epaisseur_max"] = int(round(p["epaisseur_max"] * 20, 6)) / 20.0
    p["cadre_mm"] = 0.0 if p["cadre_mm"] < 0.25 else int(round(p["cadre_mm"] * 4, 6)) / 4.0
    if p["epaisseur_max"] <= p["epaisseur_min"]:
        raise ValueError("L'épaisseur maximale doit dépasser la minimale.")
    neg = p["negatif"]
    p["negatif"] = neg.strip().lower() in ("1", "true", "oui", "on", "vrai") if isinstance(neg, str) else bool(neg)
    nom = str(p["nom"] or "")
    if len(nom) > 60 or any(ord(ch) < 32 for ch in nom):
        raise ValueError("Nom : 60 caractères au plus, sans caractère de contrôle.")
    p["nom"] = nom
    return p


def nom_sur(titre):
    """IndexCreations.nomSur : sans accents, [a-z0-9-], 40 caractères au plus."""
    import re
    import unicodedata
    sans = "".join(ch for ch in unicodedata.normalize("NFD", titre) if not unicodedata.combining(ch))
    return (re.sub(r"[^A-Za-z0-9]+", "-", sans).strip("-")[:40] or "creation").lower()


def _apercu(rgba, chemin):
    """Aperçu sur le fond de l'écran de l'application (dégradé radial brun chaud → nuit)."""
    import numpy as np
    from PIL import Image
    h, l = rgba.shape[:2]
    marge = max(8, round(0.05 * max(l, h)))
    L, H = l + 2 * marge, h + 2 * marge
    yy, xx = np.mgrid[0:H, 0:L].astype(np.float64)
    r = np.clip(np.hypot((xx - L / 2) / (L / 2), (yy - H / 2) / (H / 2)) / 1.2, 0, 1)[..., None]
    centre, bord = np.array([0x3A, 0x2A, 0x1A], float), np.array([0x0D, 0x07, 0x10], float)
    fond = np.concatenate([centre * (1 - r) + bord * r, np.full((H, L, 1), 255.0)], -1).astype(np.uint8)
    image = Image.fromarray(fond, "RGBA")
    image.alpha_composite(Image.fromarray(rgba, "RGBA"), (marge, marge))
    image.convert("RGB").save(chemin, "PNG", optimize=True)


def run(params, inputs, out_dir, progress):
    from pathlib import Path

    from . import image as img
    from . import litho

    p = lire_params(params)
    photo_chemin = (inputs or {}).get("photo")
    if not photo_chemin:
        raise ValueError("Photo manquante.")
    out_dir = Path(out_dir)
    progress(0.02, "Lecture de la photo")
    photo = img.charger_photo(Path(photo_chemin))
    fmt = p["format"]
    if fmt == "auto":
        fmt = "1:1" if p["forme"] == "cercle" else "4:3"
    ratio = img.ratio_format(fmt, p["ratio_libre"], photo.width, photo.height)
    cadre = img.cadre_centre(photo.width, photo.height, ratio)
    pas_demande = float(p["pas_mm"])
    noeuds = int(p["largeur_mm"] / max(pas_demande, litho.PAS_MIN)) + 1
    g = img.gris(photo, cadre, min(noeuds * 2, img.COTE_MAX))
    g = img.appliquer(g, luminosite=p["luminosite"] / 100.0, contraste=p["contraste"] / 100.0, gamma=p["gamma"])
    progress(0.05, "Préparation du relief")

    def sous(a, b):
        return lambda f, m="": progress(a + (b - a) * max(0.0, min(1.0, f)), m)

    plan = litho.planifier(g, largeur_mm=p["largeur_mm"], forme=p["forme"], epaisseur_min=p["epaisseur_min"],
                           epaisseur_max=p["epaisseur_max"], cadre_mm=p["cadre_mm"], pas_mm=pas_demande,
                           negatif=p["negatif"], progress=sous(0.05, 0.2))
    attendus = plan.triangles()

    # Aperçu rétroéclairé (900 px de large comme l'application, 1200 px au plus de haut).
    t = plan.transmission(p["epaisseur_min"], p["epaisseur_max"])
    th, tl = t.shape
    cible = min(_APERCU_LARGEUR, int(_APERCU_MAX * tl / th))
    if tl > cible:
        t = img.redimensionner(t, cible, max(1, int(cible * th / tl)))
    _apercu(litho.retroeclaire(t), out_dir / "apercu.png")

    titre = p["nom"].strip() or "Lithophanie"
    base = nom_sur(titre)
    nom_fichier = f"{base}.stl" if "lithophanie" in base else f"{base}-lithophanie.stl"
    n = litho.ecrire_stl(plan, out_dir / nom_fichier, titre, progress=sous(0.2, 0.98))
    if n != attendus:
        raise RuntimeError(f"Maillage incohérent : {n} triangles écrits pour {attendus} annoncés.")
    libelle = litho.FORMES[p["forme"]][0]
    details = [
        f"{litho.mm(plan.largeur)} × {litho.mm(plan.hauteur)} · {libelle}",
        f"Épaisseur {litho.mm2(p['epaisseur_min'])} à {litho.mm2(p['epaisseur_max'])} · cadre {litho.mm(p['cadre_mm'])} · "
        f"{'négatif' if p['negatif'] else 'positif'}",
        f"{litho._entier(n)} triangles · détail {litho.mm2(plan.pas)} · STL {litho._entier(litho.taille_stl(n))} octets",
    ] + ([plan.avertissement] if plan.avertissement else []) + litho.conseils(p["epaisseur_min"], p["epaisseur_max"])
    progress(1.0, "Lithophanie prête")
    return {
        "files": [{"name": nom_fichier, "label": "STL", "library": "impression"}],
        "preview": "apercu.png",
        "details": details,
        "summary": {"largeur_mm": round(plan.largeur, 3), "hauteur_mm": round(plan.hauteur, 3), "forme": p["forme"],
                    "triangles": n, "octets": litho.taille_stl(n), "pas_mm": round(plan.pas, 4),
                    "epaisseur_min_mm": p["epaisseur_min"], "epaisseur_max_mm": p["epaisseur_max"],
                    "cadre_mm": p["cadre_mm"], "negatif": p["negatif"], "couches_012": litho.nb_couches(p["epaisseur_max"]),
                    "avertissement": plan.avertissement},
    }
