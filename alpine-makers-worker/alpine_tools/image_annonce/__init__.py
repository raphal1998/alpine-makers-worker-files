# -*- coding: utf-8 -*-
"""Alpine Tools — Image d'annonce (portage de l'écran « Image d'annonce » et de Rendu.annonce d'Alpine Gift).

La photo d'une création mise en scène sur un visuel carré soigné pour la vendre : fond d'ambiance, halo, ombre,
titre, sous-titre, prix en pastille, mention « Fait main ». Texte écrit avec la police « Alpine Ronde » dessinée dans
``police.py`` (aucune police tierce). ``TOOL`` est un littéral pur : le site le lit par ``ast.literal_eval``.
"""

TOOL = {
    "id": "image-annonce",
    "name": "Image d'annonce",
    "machine": "calcul",
    "description": "Un visuel carré soigné pour vendre ta création : photo mise en scène, titre, prix et ambiance au choix.",
    "inputs": [
        {"key": "photo", "label": "Photo de la création", "accept": [".png", ".jpg", ".jpeg", ".webp"],
         "required": True, "max_mb": 25},
    ],
    "params": [
        {"key": "titre", "label": "Titre", "type": "text", "max": 60, "default": "",
         "help": "Vide : « Cadeau personnalisé »."},
        {"key": "sous_titre", "label": "Sous-titre", "type": "text", "max": 110,
         "default": "Création personnalisée · prénom ou message au choix"},
        {"key": "prix", "label": "Prix (facultatif)", "type": "text", "max": 24, "default": "",
         "help": "Affiché dans une pastille, par exemple « CHF 24.– »."},
        {"key": "style", "label": "Ambiance", "type": "choice", "default": "nuit",
         "options": [{"value": "nuit", "label": "Nuit dorée"}, {"value": "creme", "label": "Crème"},
                     {"value": "sapin", "label": "Sapin"}, {"value": "glacier", "label": "Glacier"}]},
        {"key": "lumineuse", "label": "Halo lumineux", "type": "bool", "default": False,
         "help": "Pour une lithophanie ou une lampe : halo chaud derrière la création."},
        {"key": "coins_arrondis", "label": "Coins arrondis", "type": "bool", "default": True,
         "help": "Arrondit les coins d'une photo rectangulaire (sans effet sur une image détourée)."},
        {"key": "mention", "label": "Mention", "type": "text", "max": 40, "default": "Fait main · personnalisable"},
        {"key": "cote", "label": "Taille de l'image", "type": "choice", "default": "1080",
         "options": [{"value": "1080", "label": "1080 × 1080 px"}, {"value": "1440", "label": "1440 × 1440 px"},
                     {"value": "2048", "label": "2048 × 2048 px"}]},
    ],
}


def _nom_fichier(titre):
    import re
    import unicodedata
    t = unicodedata.normalize("NFKD", titre).encode("ascii", "ignore").decode("ascii").lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:32].strip("-")
    return t or "creation"


def run(params, inputs, out_dir, progress):
    from pathlib import Path

    from PIL import Image

    from . import police
    from .rendu import STYLES, annonce, charger_photo

    out_dir = Path(out_dir)
    valeurs = {p["key"]: p["default"] for p in TOOL["params"]}
    valeurs.update({k: v for k, v in dict(params or {}).items() if k in valeurs and v is not None})
    style = str(valeurs["style"])
    if style not in STYLES:
        raise ValueError("Ambiance inconnue.")
    cote = str(valeurs["cote"])
    if cote not in ("1080", "1440", "2048"):
        raise ValueError("Taille d'image inconnue.")
    titre = str(valeurs["titre"] or "")[:60]
    sous_titre = str(valeurs["sous_titre"] or "")[:110]
    prix = str(valeurs["prix"] or "")[:24]
    mention = str(valeurs["mention"] or "")[:40]
    chemin = (inputs or {}).get("photo")
    if not chemin or not Path(chemin).is_file():
        raise ValueError("Ajoute la photo de ta création.")
    progress(0.05, "lecture de la photo")
    photo = charger_photo(chemin)
    progress(0.2, "mise en scène")
    image, n_titre, n_sous = annonce(photo, titre, sous_titre, prix, style, bool(valeurs["lumineuse"]),
                                     bool(valeurs["coins_arrondis"]), mention, int(cote))
    progress(0.85, "enregistrement")
    nom = f"annonce-{_nom_fichier(titre)}.png"
    image.save(out_dir / nom, optimize=True)
    image.resize((600, 600), Image.LANCZOS).save(out_dir / "apercu.png", optimize=True)
    details = [f"{cote} × {cote} px · ambiance {STYLES[style][0]}",
               f"Titre : « {titre.strip() or 'Cadeau personnalisé'} »" + (f" · prix {prix.strip()}" if prix.strip() else "")]
    manquants = police.absents(titre + sous_titre + prix + mention.upper())
    if manquants:
        details.append("Caractères absents de la police, remplacés par une espace : " + " ".join(manquants) + ".")
    progress(1.0, "terminé")
    return {
        "files": [{"name": nom, "label": f"PNG {cote} px", "library": None}],
        "preview": "apercu.png",
        "details": details,
        "summary": {"cote_px": int(cote), "style": style, "lignes_titre": n_titre, "lignes_sous_titre": n_sous,
                    "prix": bool(prix.strip())},
    }
