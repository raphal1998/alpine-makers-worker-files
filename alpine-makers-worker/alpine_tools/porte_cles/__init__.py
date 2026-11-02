# -*- coding: utf-8 -*-
"""Alpine Tools — Porte-clés et plaques (portage d'Alpine Gift : PorteCles.kt, Extrusion.kt, Svg.kt ;
triangulation des faces écrite pour les Alpine Tools dans ``triangulation.py``).

Prénom ou message en relief (ou gravé) sur un socle de forme choisie, anneau, bordure, deux couleurs. Sorties : STL
complet, STL socle, STL texte, SVG laser, aperçu PNG. Police « Alpine Ronde » dessinée dans ``police.py`` (aucune
police tierce). ``TOOL`` est un littéral pur : le site le lit par ``ast.literal_eval`` sans importer numpy.
"""

TOOL = {
    "id": "porte-cles",
    "name": "Porte-clés et plaques",
    "machine": "impression",
    "description": "Un prénom ou un message en relief sur un porte-clés ou une plaque, prêt à imprimer en deux couleurs ou à découper au laser.",
    "inputs": [],
    "params": [
        {"key": "ligne1", "label": "Ligne 1", "type": "text", "max": 24, "default": "Alpine"},
        {"key": "ligne2", "label": "Ligne 2 (facultative)", "type": "text", "max": 32, "default": ""},
        {"key": "graisse", "label": "Graisse du texte", "type": "choice", "default": "normale",
         "options": [{"value": "fine", "label": "Fine"}, {"value": "normale", "label": "Normale"},
                     {"value": "grasse", "label": "Grasse"}]},
        {"key": "hauteur_texte", "label": "Hauteur du texte", "type": "number", "min": 4, "max": 40, "step": 1,
         "default": 10, "unit": "mm", "help": "Hauteur des majuscules de la ligne 1."},
        {"key": "taille_ligne2", "label": "Taille de la ligne 2", "type": "number", "min": 40, "max": 100, "step": 5,
         "default": 60, "unit": "%"},
        {"key": "interlettrage", "label": "Interlettrage", "type": "number", "min": -1, "max": 3, "step": 0.1,
         "default": 0, "unit": "mm"},
        {"key": "forme", "label": "Forme", "type": "choice", "default": "arrondi",
         "options": [{"value": "arrondi", "label": "Arrondi"}, {"value": "capsule", "label": "Capsule"},
                     {"value": "cercle", "label": "Cercle"}, {"value": "coeur", "label": "Cœur"},
                     {"value": "plaque", "label": "Plaque rectangulaire"},
                     {"value": "contour", "label": "Contour du texte"}]},
        {"key": "marge", "label": "Marge autour du texte", "type": "number", "min": 1.5, "max": 8, "step": 0.5,
         "default": 3, "unit": "mm"},
        {"key": "bordure", "label": "Bordure", "type": "bool", "default": False,
         "help": "Un liseré en relief suit le bord."},
        {"key": "largeur_bordure", "label": "Largeur de la bordure", "type": "number", "min": 0.6, "max": 4,
         "step": 0.2, "default": 1.2, "unit": "mm"},
        {"key": "anneau", "label": "Anneau", "type": "choice", "default": "gauche",
         "options": [{"value": "gauche", "label": "À gauche"}, {"value": "haut", "label": "En haut"},
                     {"value": "aucun", "label": "Sans anneau"}]},
        {"key": "diametre_anneau", "label": "Diamètre du trou", "type": "number", "min": 3, "max": 8, "step": 0.5,
         "default": 4.5, "unit": "mm"},
        {"key": "paroi_anneau", "label": "Paroi de l'anneau", "type": "number", "min": 1.2, "max": 6, "step": 0.1,
         "default": 2.5, "unit": "mm"},
        {"key": "grave", "label": "Texte gravé", "type": "bool", "default": False,
         "help": "Le texte est creusé dans le socle (incrustation possible en deux couleurs)."},
        {"key": "socle", "label": "Épaisseur du socle", "type": "number", "min": 1.5, "max": 6, "step": 0.2,
         "default": 3, "unit": "mm"},
        {"key": "relief", "label": "Relief (ou profondeur gravée)", "type": "number", "min": 0.6, "max": 3,
         "step": 0.2, "default": 1.2, "unit": "mm",
         "help": "Texte gravé : au moins 0,6 mm doivent rester sous le texte."},
        {"key": "couleur_socle", "label": "Couleur du socle (aperçu)", "type": "color", "default": "#F2EEE8"},
        {"key": "couleur_texte", "label": "Couleur du texte (aperçu)", "type": "color", "default": "#F27A98"},
    ],
}


def _mm(v):
    return (f"{v:.1f}".rstrip("0").rstrip(".") + " mm").replace(".", ",")


def _nom_fichier(titre):
    import re
    import unicodedata
    t = unicodedata.normalize("NFKD", titre).encode("ascii", "ignore").decode("ascii").lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:32].strip("-")
    return t or "porte-cles"


def run(params, inputs, out_dir, progress):
    from pathlib import Path

    from .apercu import couleur, dessiner
    from .calcul import calculer
    from .maillage import ecrire_stl

    out_dir = Path(out_dir)
    valeurs = {p["key"]: p["default"] for p in TOOL["params"]}
    valeurs.update({k: v for k, v in dict(params or {}).items() if k in valeurs and v is not None})
    params = valeurs
    r = calculer(params, progress)
    base = _nom_fichier(r.titre)
    fichiers = []
    progress(0.92, "écriture des STL")
    ecrire_stl(out_dir / f"{base}-complet.stl", r.tout, r.titre)
    fichiers.append({"name": f"{base}-complet.stl", "label": "STL complet", "library": "impression"})
    ecrire_stl(out_dir / f"{base}-socle.stl", r.socle, r.titre + " socle")
    fichiers.append({"name": f"{base}-socle.stl", "label": "STL socle", "library": "impression"})
    if r.texte.nb_triangles:
        ecrire_stl(out_dir / f"{base}-texte.stl", r.texte, r.titre + " texte")
        fichiers.append({"name": f"{base}-texte.stl", "label": "STL texte", "library": "impression"})
    (out_dir / f"{base}-laser.svg").write_text(r.svg, encoding="utf-8")
    fichiers.append({"name": f"{base}-laser.svg", "label": "SVG laser", "library": "laser"})
    progress(0.96, "aperçu")
    dessiner(r, out_dir / "apercu.png", couleur(params.get("couleur_socle"), "#F2EEE8"),
             couleur(params.get("couleur_texte"), "#F27A98"))
    graisses = {"fine": "fine", "normale": "normale", "grasse": "grasse"}
    details = [
        f"{_mm(r.largeur)} × {_mm(r.hauteur)} · épaisseur {_mm(r.epaisseur)} · {r.forme}",
        f"Police Alpine Ronde {graisses.get(r.graisse, '')} · texte "
        + (f"gravé de {_mm(r.relief)}" if r.grave else f"en relief de {_mm(r.relief)}") + f" · socle {_mm(r.socle_mm)}",
    ]
    if r.changement_couleur is not None:
        details.append(f"Changement de filament à {r.changement_couleur:.1f} mm (début de la couche à cette hauteur).".replace(".", ",", 1))
    details.append("Deux couleurs : charge le socle et le texte comme pièces d'un même objet (AMS) : le texte remplit le creux."
                   if r.grave else
                   "Deux couleurs avec l'AMS : socle + texte comme pièces d'un même objet, ou un seul STL et un changement de filament.")
    details.append("SVG laser : découpe en rouge, gravure en noir, en millimètres.")
    details.append(f"{r.tout.nb_triangles:,} triangles ({'maillage fermé' if r.ferme else 'maillage NON fermé'}).".replace(",", " "))
    details += r.avertissements
    progress(1.0, "terminé")
    return {
        "files": fichiers,
        "preview": "apercu.png",
        "details": details,
        "summary": {
            "largeur_mm": round(r.largeur, 2), "hauteur_mm": round(r.hauteur, 2), "epaisseur_mm": round(r.epaisseur, 2),
            "triangles": r.tout.nb_triangles, "ferme": bool(r.ferme),
            "changement_couleur_mm": None if r.changement_couleur is None else round(r.changement_couleur, 2),
            "avertissements": len(r.avertissements),
        },
    }
