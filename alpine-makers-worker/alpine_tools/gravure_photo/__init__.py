"""Alpine Tools — Gravure photo laser (atool-gravure-photo), portage d'Alpine Gift (Gravure.kt, Tramage.kt, Png.kt,
LaserStudio.kt, GravureUi.kt).

Contrat : docs/ALPINE_TOOLS.md. TOOL est un littéral pur (lu par le site sans importer numpy) ; les imports lourds
sont faits dans run().
"""

TOOL = {
    "id": "gravure-photo",
    "name": "Gravure photo laser",
    "machine": "laser",
    "description": "Ta photo tramée à la taille réelle de la pièce, prête pour Alpine Laser Studio (.alsproj) ou tout logiciel laser (PNG).",
    "inputs": [
        {"key": "photo", "label": "Photo", "accept": [".png", ".jpg", ".jpeg", ".webp"], "required": True, "max_mb": 25,
         "help": "Un portrait net et bien éclairé donne la meilleure gravure. 24 Mpx au plus."},
    ],
    "params": [
        {"key": "format", "label": "Recadrage", "type": "choice", "default": "4:3",
         "help": "Cadre centré au format choisi, ou la photo entière.",
         "options": [{"value": "4:3", "label": "4:3"}, {"value": "3:4", "label": "3:4"}, {"value": "1:1", "label": "1:1"},
                     {"value": "16:9", "label": "16:9"}, {"value": "libre", "label": "Libre"},
                     {"value": "entiere", "label": "Image entière"}]},
        {"key": "ratio_libre", "label": "Proportion (largeur / hauteur)", "type": "number", "min": 0.4, "max": 2.5,
         "step": 0.01, "default": 1.3, "help": "Utilisée avec le recadrage « Libre »."},
        {"key": "materiau", "label": "Matériau", "type": "choice", "default": "bois_clair",
         "options": [{"value": "bois_clair", "label": "Bois clair"}, {"value": "bois_fonce", "label": "Bois foncé"},
                     {"value": "ardoise", "label": "Ardoise"}, {"value": "verre", "label": "Verre"},
                     {"value": "cuir", "label": "Cuir"}, {"value": "acrylique_peint", "label": "Acrylique peint"},
                     {"value": "alu_anodise", "label": "Aluminium anodisé"}]},
        {"key": "largeur_mm", "label": "Largeur", "type": "number", "min": 20, "max": 300, "step": 1, "default": 80,
         "unit": "mm"},
        {"key": "resolution", "label": "Résolution", "type": "choice", "default": "10",
         "help": "Image limitée à 16 millions de pixels.",
         "options": [{"value": "10", "label": "10 px/mm · 254 ppp"}, {"value": "12.7", "label": "12,7 px/mm · 323 ppp"},
                     {"value": "20", "label": "20 px/mm · 508 ppp"}]},
        {"key": "reglages_materiau", "label": "Réglages conseillés du matériau", "type": "bool", "default": True,
         "help": "Luminosité, contraste, gamma, netteté et négatif du matériau choisi ; décoche pour régler toi-même."},
        {"key": "luminosite", "label": "Luminosité", "type": "number", "min": -60, "max": 60, "step": 1, "default": 0,
         "unit": "%", "help": "Si les réglages conseillés sont décochés."},
        {"key": "contraste", "label": "Contraste", "type": "number", "min": -50, "max": 80, "step": 1, "default": 0,
         "unit": "%", "help": "Si les réglages conseillés sont décochés."},
        {"key": "gamma", "label": "Gamma", "type": "number", "min": 0.5, "max": 2.2, "step": 0.01, "default": 1.0,
         "help": "Si les réglages conseillés sont décochés."},
        {"key": "nettete", "label": "Netteté", "type": "number", "min": 0.0, "max": 2.0, "step": 0.1, "default": 0.0,
         "help": "Si les réglages conseillés sont décochés."},
        {"key": "negatif", "label": "Négatif", "type": "bool", "default": False,
         "help": "Pour les matières que le laser éclaircit (ardoise, verre, anodisé) ; si les réglages conseillés sont décochés."},
        {"key": "methode", "label": "Tramage", "type": "choice", "default": "materiau",
         "options": [{"value": "materiau", "label": "Selon le matériau"},
                     {"value": "floyd_steinberg", "label": "Floyd–Steinberg"}, {"value": "jarvis", "label": "Jarvis"},
                     {"value": "stucki", "label": "Stucki"}, {"value": "atkinson", "label": "Atkinson"},
                     {"value": "bayer", "label": "Bayer"}, {"value": "seuil", "label": "Seuil"},
                     {"value": "niveaux", "label": "Niveaux de gris"}]},
        {"key": "nom", "label": "Nom de la création (facultatif)", "type": "text", "max": 60, "default": ""},
    ],
}

_DEFAUTS = {spec["key"]: spec["default"] for spec in TOOL["params"]}
_BORNES = {spec["key"]: (spec["min"], spec["max"]) for spec in TOOL["params"] if spec["type"] == "number"}
_CHOIX = {spec["key"]: [o["value"] for o in spec["options"]] for spec in TOOL["params"] if spec["type"] == "choice"}
_LIBELLES = {spec["key"]: spec["label"] for spec in TOOL["params"]}


def _booleen(v):
    return v.strip().lower() in ("1", "true", "oui", "on", "vrai") if isinstance(v, str) else bool(v)


def lire_params(params):
    """Paramètres revérifiés (bornes, choix) ; largeur entière comme le curseur de l'application."""
    p = dict(_DEFAUTS)
    p.update({k: v for k, v in (params or {}).items() if v is not None})
    inconnues = set(p) - set(_DEFAUTS)
    if inconnues:
        raise ValueError("Paramètre inconnu : " + ", ".join(sorted(inconnues)) + ".")
    for cle, valeurs in _CHOIX.items():
        if str(p[cle]) not in valeurs:
            raise ValueError(f"« {_LIBELLES[cle]} » : choix inconnu.")
        p[cle] = str(p[cle])
    for cle, (lo, hi) in _BORNES.items():
        try:
            v = float(p[cle])
        except (TypeError, ValueError):
            raise ValueError(f"« {_LIBELLES[cle]} » : nombre attendu.") from None
        if not (v == v and lo <= v <= hi):
            bornes = [str(b).replace(".", ",") for b in (lo, hi)]
            raise ValueError(f"« {_LIBELLES[cle]} » : entre {bornes[0]} et {bornes[1]}.")
        p[cle] = v
    p["largeur_mm"] = float(int(p["largeur_mm"] + 0.5))
    p["negatif"] = _booleen(p["negatif"])
    p["reglages_materiau"] = _booleen(p["reglages_materiau"])
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


def _fr(v, decimales):
    return f"{v:.{decimales}f}".replace(".", ",")


def _mm(v):
    return (_fr(v, 1) + " mm").replace(",0 mm", " mm")


def run(params, inputs, out_dir, progress):
    from pathlib import Path

    from . import gravure as gv
    from . import image as img

    p = lire_params(params)
    photo_chemin = (inputs or {}).get("photo")
    if not photo_chemin:
        raise ValueError("Photo manquante.")
    out_dir = Path(out_dir)
    mat = p["materiau"]
    m = gv.MATERIAUX[mat]
    if p["reglages_materiau"]:
        reglages = gv.reglages_materiau(mat)
    else:
        reglages = {"luminosite": p["luminosite"] / 100.0, "contraste": p["contraste"] / 100.0, "gamma": p["gamma"],
                    "nettete": p["nettete"], "negatif": p["negatif"]}
    methode = m["methode"] if p["methode"] == "materiau" else p["methode"]
    resolution = float(p["resolution"])
    largeur = p["largeur_mm"]

    progress(0.02, "Lecture de la photo")
    photo = img.charger_photo(Path(photo_chemin))
    ratio = img.ratio_format(p["format"], p["ratio_libre"], photo.width, photo.height)
    cadre = img.cadre_centre(photo.width, photo.height, ratio)
    if ratio is None:
        ratio = photo.width / photo.height
    l_px = img.arrondi(largeur * resolution)
    h_px = img.arrondi(largeur / ratio * resolution)
    if l_px * h_px > gv.PIXELS_MAX:
        raise ValueError(f"Image trop grande ({l_px} × {h_px} px) : réduis la largeur ou la résolution.")
    g = img.gris(photo, cadre, l_px, h_px)
    progress(0.05, "Tramage")
    r = gv.preparer(g, largeur, resolution, reglages, methode, lambda f: progress(0.05 + 0.7 * f, "Tramage"))

    titre = p["nom"].strip() or f"Gravure {m['libelle'].lower()}"
    base = nom_sur(titre)
    dpi = gv.ppp(resolution)
    progress(0.8, "PNG et projet Laser Studio")
    png = gv.png_gris(r["pixels"], resolution)
    texte = gv.projet(titre, r, png, m["libelle"], methode=methode)
    nom_projet, nom_png = f"{base}.alsproj", f"{base}-{dpi}ppp.png"
    (out_dir / nom_projet).write_text(texte, encoding="utf-8")
    (out_dir / nom_png).write_bytes(png)
    gv.apercu_trame(r["pixels"], mat).save(out_dir / "apercu.png", "PNG", optimize=True)

    noirs = gv.part_noire(r["pixels"])
    details = [
        f"{_mm(r['largeur_mm'])} × {_mm(r['hauteur_mm'])} · {_fr(resolution, 1)} px/mm ({dpi} ppp)",
        f"{r['largeur_px']} × {r['hauteur_px']} px · {gv.METHODES[methode][0]} · {m['libelle']}"
        f"{' · négatif' if reglages['negatif'] else ''}",
        "Projet Alpine Laser Studio : photo à la taille réelle sur un calque Image en mode Relais (déjà tramée, gravée "
        "telle quelle). Ouvre-le dans Laser Studio, choisis la machine et le matériau : puissance et vitesse se règlent seules.",
        m["conseil"],
    ]
    progress(1.0, "Gravure prête")
    return {
        "files": [{"name": nom_projet, "label": "Projet Alpine Laser Studio", "library": "laser"},
                  {"name": nom_png, "label": f"Image seule (PNG {dpi} ppp)", "library": "laser"}],
        "preview": "apercu.png",
        "details": details,
        "summary": {"largeur_mm": r["largeur_mm"], "hauteur_mm": round(r["hauteur_mm"], 3), "largeur_px": r["largeur_px"],
                    "hauteur_px": r["hauteur_px"], "px_par_mm": resolution, "ppp": dpi, "materiau": mat,
                    "methode": methode, "negatif": bool(reglages["negatif"]), "part_noire": round(noirs, 4),
                    "octets_png": len(png)},
    }
