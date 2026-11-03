"""Dessin d'enfant (atool-dessin-enfant) : la photo d'un dessin d'enfant devient un emporte-pièce, un porte-clés, un magnet
ou un tampon imprimés en 3D.

Port d'Alpine Gift 1.0.11 (C:\\AlpineGift, cœur Kotlin DessinEnfant.kt, FeuilleReperes.kt, Champ.kt, Contours.kt,
Triangulation.kt) : feuille de repères A4 détectée et redressée (dessin à sa vraie taille), lecture des traits
(seuil d'Otsu, nettoyage, raccord, épaississement), silhouette, maillages fermés ; emporte-pièce en miroir (biscuit à
l'endroit une fois le moule retourné), tampon aux traits inversés. Contrat : docs/ALPINE_TOOLS.md. ``TOOL`` est un
littéral pur (le site le lit sans importer le module) ; les calculs (numpy, Pillow) sont importés dans ``run``.
"""

TOOL = {
    "id": "dessin-enfant",
    "name": "Dessin d'enfant",
    "machine": "impression",
    "description": "La photo d'un dessin d'enfant devient un emporte-pièce, un porte-clés, un magnet ou un tampon imprimés en 3D.",
    "inputs": [
        {"key": "photo", "label": "Photo du dessin (sur la feuille de repères A4)", "accept": [".png", ".jpg", ".jpeg", ".webp"],
         "required": False, "max_mb": 25},
    ],
    "params": [
        {"key": "objet", "label": "Objet", "type": "choice", "default": "emporte_piece",
         "options": [{"value": "emporte_piece", "label": "Emporte-pièce"}, {"value": "porte_cles", "label": "Porte-clés"},
                     {"value": "magnet", "label": "Magnet"}, {"value": "tampon", "label": "Tampon"}],
         "help": "Emporte-pièce : paroi coupante qui suit le contour, inversée (miroir) pour un biscuit à l'endroit. Porte-clés : "
                 "silhouette, traits en relief, anneau. Magnet : logement d'aimant au dos. Tampon : traits en relief inversés."},
        {"key": "cadrage", "label": "Cadrage", "type": "choice", "default": "auto",
         "options": [{"value": "auto", "label": "Automatique (feuille de repères si trouvée)"},
                     {"value": "feuille", "label": "Feuille de repères"},
                     {"value": "libre", "label": "Cadrage libre (sans feuille)"}],
         "help": "Sur la feuille de repères, le dessin est redressé et retrouvé à sa vraie taille. Sans feuille, recadre-le et indique sa largeur."},
        {"key": "coins", "label": "Centres des repères (facultatif)", "type": "text", "max": 120, "default": "",
         "help": "Si les repères ne sont pas trouvés : centres des 4 carrés noirs en % de la photo (x y), haut-gauche, "
                 "haut-droite, bas-droite, bas-gauche, ex. 12 8; 88 9; 90 95; 10 93."},
        {"key": "echelle", "label": "Échelle de l'objet", "type": "number", "min": 25, "max": 150, "step": 5, "default": 100, "unit": "%",
         "help": "100 % : l'objet a la taille du dessin sur la feuille (le dessin lu fait au plus 250 mm de côté)."},
        {"key": "largeur_libre_mm", "label": "Largeur du cadrage (cadrage libre)", "type": "number", "min": 20, "max": 200, "step": 1,
         "default": 80, "unit": "mm", "help": "Recadre au plus près du dessin : cette largeur devient la taille réelle de l'objet."},
        {"key": "recadrage", "label": "Recadrage (cadrage libre, facultatif)", "type": "text", "max": 60, "default": "",
         "help": "Gauche haut droite bas en % de la photo, ex. 10 5 90 80. Vide : toute la photo."},
        {"key": "seuil_auto", "label": "Seuil automatique", "type": "bool", "default": True,
         "help": "Sépare d'elle-même l'encre du papier."},
        {"key": "seuil", "label": "Seuil (sans seuil automatique)", "type": "number", "min": 10, "max": 90, "step": 1, "default": 50, "unit": "%",
         "help": "Gris plus sombre que ce seuil = trait."},
        {"key": "inverser", "label": "Traits clairs sur fond sombre", "type": "bool", "default": False,
         "help": "Craie sur ardoise, feutre blanc sur papier noir."},
        {"key": "nettoyage_mm2", "label": "Nettoyage des taches", "type": "number", "min": 0, "max": 10, "step": 0.5, "default": 1.0, "unit": "mm²",
         "help": "Taches et petits trous plus petits que cette aire supprimés."},
        {"key": "raccord_mm", "label": "Raccord des traits interrompus", "type": "number", "min": 0, "max": 2, "step": 0.1, "default": 0.3, "unit": "mm"},
        {"key": "trait_min_mm", "label": "Épaisseur minimale des traits", "type": "number", "min": 0, "max": 2.5, "step": 0.1, "default": 0.9, "unit": "mm",
         "help": "Les traits plus fins sont épaissis pour s'imprimer."},
        {"key": "hauteur_mm", "label": "Hauteur (emporte-pièce)", "type": "number", "min": 10, "max": 25, "step": 1, "default": 14, "unit": "mm"},
        {"key": "paroi_mm", "label": "Paroi (emporte-pièce)", "type": "number", "min": 1.2, "max": 2.4, "step": 0.1, "default": 1.6, "unit": "mm"},
        {"key": "rebord_mm", "label": "Rebord, appui des doigts (emporte-pièce)", "type": "number", "min": 2, "max": 8, "step": 0.5, "default": 4.0, "unit": "mm"},
        {"key": "socle_mm", "label": "Socle ou épaisseur de la plaque", "type": "number", "min": 2, "max": 8, "step": 0.2, "default": 3.0, "unit": "mm",
         "help": "Porte-clés, magnet et tampon."},
        {"key": "relief_mm", "label": "Relief des traits", "type": "number", "min": 0.6, "max": 3, "step": 0.2, "default": 1.2, "unit": "mm",
         "help": "Tampon : 0,8 mm au moins."},
        {"key": "marge_mm", "label": "Marge autour du dessin", "type": "number", "min": 1, "max": 6, "step": 0.5, "default": 2.5, "unit": "mm"},
        {"key": "anneau_mm", "label": "Diamètre du trou de l'anneau (porte-clés)", "type": "number", "min": 3, "max": 8, "step": 0.5, "default": 4.5, "unit": "mm"},
        {"key": "aimant_mm", "label": "Aimant (magnet)", "type": "choice", "default": 10,
         "options": [{"value": 8, "label": "Ø 8 mm"}, {"value": 10, "label": "Ø 10 mm"}, {"value": 12, "label": "Ø 12 mm"},
                     {"value": 15, "label": "Ø 15 mm"}, {"value": 20, "label": "Ø 20 mm"}]},
        {"key": "profondeur_aimant_mm", "label": "Profondeur du logement (magnet)", "type": "number", "min": 1, "max": 4, "step": 0.1, "default": 2.2, "unit": "mm"},
        {"key": "couleur_base", "label": "Couleur de l'objet", "type": "color", "default": "#F2EEE8"},
        {"key": "couleur_traits", "label": "Couleur des traits (porte-clés, magnet)", "type": "color", "default": "#D8333F"},
        {"key": "format", "label": "Format du fichier", "type": "choice", "default": "3mf",
         "options": [{"value": "3mf", "label": "3MF multicolore"}, {"value": "stl_unique", "label": "STL unique"},
                     {"value": "stl_couleurs", "label": "STL par couleur (ZIP)"}, {"value": "obj", "label": "OBJ avec couleurs (ZIP)"}],
         "help": "3MF : Bambu Studio, OrcaSlicer, Creality Print, chaque couleur arrive sur son filament."},
        {"key": "nom", "label": "Nom de la création", "type": "text", "max": 60, "default": ""},
        {"key": "feuille_reperes", "label": "Joindre la feuille de repères A4 (PDF et SVG)", "type": "bool", "default": False,
         "help": "À imprimer à 100 % : 4 carrés noirs et un cadre de dessin de 160 × 240 mm. Sans photo, seule la feuille est créée."},
    ],
}


def run(params, inputs, out_dir, progress):
    import os
    # Aucun calcul BLAS ici : un seul thread OpenBLAS évite ~750 Mo de tampons réservés à l'import de numpy (24 cœurs).
    for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
        os.environ.setdefault(variable, "1")
    from pathlib import Path

    from .travail import executer
    return executer(params, inputs, Path(out_dir), progress)
