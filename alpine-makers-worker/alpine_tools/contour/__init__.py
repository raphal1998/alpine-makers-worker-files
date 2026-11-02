"""Alpine Contour (atool-contour) : photo d'un objet posé sur une feuille → contour à l'échelle réelle.

Port de l'application Android Alpine Contour (C:\\AlpineContour, cœur Kotlin) : détection de la feuille, redressement,
détourage, contour au sous-pixel, lissage, jeu, simplification, exports SVG (laser), DXF (CNC), STL pièce et insert
(impression), PNG de contrôle, feuille de repères PDF. Contrat : docs/ALPINE_TOOLS.md. ``TOOL`` est un littéral pur
(le site le lit sans importer le module) ; les calculs (numpy, Pillow) sont importés dans ``run``.
"""

TOOL = {
    "id": "contour",
    "name": "Alpine Contour",
    "machine": "cnc",
    "description": "Photo d'un objet posé sur une feuille A4 ou Letter : contour à l'échelle réelle en SVG, DXF, STL de la pièce et insert de rangement.",
    "inputs": [
        {"key": "photo", "label": "Photo de l'objet sur la feuille", "accept": [".png", ".jpg", ".jpeg", ".webp"], "required": True, "max_mb": 25},
    ],
    "params": [
        {"key": "format", "label": "Feuille de référence", "type": "choice", "default": "a4",
         "options": [{"value": "a4", "label": "A4 blanche (210 × 297 mm)"},
                     {"value": "letter", "label": "Letter blanche (215,9 × 279,4 mm)"},
                     {"value": "reperes_a4", "label": "Feuille de repères A4 (plus précis)"}],
         "help": "Feuille posée sous l'objet, entièrement visible sur la photo, sur un fond plus sombre."},
        {"key": "sensibilite", "label": "Sensibilité", "type": "number", "min": -1, "max": 1, "step": 0.05, "default": 0,
         "help": "À gauche : seules les zones nettement différentes du papier. À droite : les nuances claires comptent aussi."},
        {"key": "ombres", "label": "Atténuer les ombres", "type": "bool", "default": False,
         "help": "Ignore les gris peu foncés (ombre portée). Pour un objet foncé ou coloré."},
        {"key": "remplir_trous", "label": "Silhouette pleine", "type": "bool", "default": False,
         "help": "Bouche tous les trous de l'objet (poche d'insert, découpe extérieure seule)."},
        {"key": "taille_min_mm2", "label": "Taille minimale d'un objet", "type": "number", "min": 5, "max": 400, "step": 5, "default": 25, "unit": "mm²"},
        {"key": "trous_min_mm2", "label": "Boucher les trous plus petits que", "type": "number", "min": 0, "max": 200, "step": 1, "default": 6, "unit": "mm²"},
        {"key": "exclure", "label": "Objets à écarter", "type": "text", "max": 60, "default": "",
         "help": "Numéros des objets à ne pas tracer, séparés par des virgules (ex. 2, 3) : voir les numéros sur l'aperçu."},
        {"key": "coins", "label": "Coins de la feuille (facultatif)", "type": "text", "max": 120, "default": "",
         "help": "Si la feuille n'est pas trouvée : 4 points en % de la photo (x y), haut-gauche, haut-droite, bas-droite, bas-gauche, ex. 12 8; 88 9; 90 95; 10 93. Feuille de repères : centres des repères."},
        {"key": "jeu_mm", "label": "Jeu (décalage du contour)", "type": "number", "min": -3, "max": 10, "step": 0.1, "default": 0, "unit": "mm",
         "help": "+ agrandit (poche d'insert, mousse), − rétrécit (pièce qui doit entrer dans un logement)."},
        {"key": "lissage_mm", "label": "Lissage", "type": "number", "min": 0, "max": 1.5, "step": 0.05, "default": 0.3, "unit": "mm"},
        {"key": "tolerance_mm", "label": "Tolérance de simplification", "type": "number", "min": 0.02, "max": 0.5, "step": 0.01, "default": 0.05, "unit": "mm",
         "help": "Écart maximal entre le contour simplifié et le contour mesuré."},
        {"key": "svg", "label": "SVG en millimètres (laser)", "type": "bool", "default": True,
         "help": "Découpe laser (LightBurn, Inkscape) : traits rouges de 0,1 mm, un groupe par objet."},
        {"key": "dxf", "label": "DXF R12 (CNC)", "type": "bool", "default": True,
         "help": "CNC et CAO (Fusion, FreeCAD, Estlcam) : polylignes fermées en mm."},
        {"key": "png", "label": "PNG de contrôle", "type": "bool", "default": True,
         "help": "La feuille redressée avec le contour et les cotes, à 5 px/mm."},
        {"key": "stl_piece", "label": "STL pièce extrudée", "type": "bool", "default": False,
         "help": "Pièce de remplacement plate à imprimer : le contour monté sur une épaisseur, trous conservés."},
        {"key": "epaisseur_mm", "label": "Épaisseur de la pièce", "type": "number", "min": 0.6, "max": 20, "step": 0.1, "default": 3, "unit": "mm"},
        {"key": "stl_insert", "label": "STL insert de rangement", "type": "bool", "default": False,
         "help": "Bloc creusé d'une poche à la forme de chaque objet, à poser dans un bac ou un tiroir (pas un bac Gridfinity complet)."},
        {"key": "insert_taille", "label": "Taille du bloc", "type": "choice", "default": "auto",
         "options": [{"value": "auto", "label": "Ajustée"}, {"value": "sur_mesure", "label": "Sur mesure"}, {"value": "grille_42", "label": "Grille 42 mm"}]},
        {"key": "marge_mm", "label": "Marge autour des poches", "type": "number", "min": 2, "max": 30, "step": 1, "default": 8, "unit": "mm"},
        {"key": "largeur_mm", "label": "Largeur du bloc (sur mesure)", "type": "number", "min": 10, "max": 600, "step": 0.5, "default": 126, "unit": "mm"},
        {"key": "longueur_mm", "label": "Longueur du bloc (sur mesure)", "type": "number", "min": 10, "max": 600, "step": 0.5, "default": 84, "unit": "mm"},
        {"key": "unites_x", "label": "Unités en largeur (grille)", "type": "number", "min": 1, "max": 8, "step": 1, "default": 3, "integer": True, "unit": "× 42 mm"},
        {"key": "unites_y", "label": "Unités en longueur (grille)", "type": "number", "min": 1, "max": 8, "step": 1, "default": 2, "integer": True, "unit": "× 42 mm"},
        {"key": "jeu_grille_mm", "label": "Jeu sur les côtés (grille)", "type": "number", "min": 0, "max": 2, "step": 0.1, "default": 0.5, "unit": "mm"},
        {"key": "hauteur_mm", "label": "Hauteur du bloc", "type": "number", "min": 4, "max": 80, "step": 1, "default": 20, "unit": "mm"},
        {"key": "profondeur_mm", "label": "Profondeur des poches", "type": "number", "min": 1, "max": 79, "step": 0.5, "default": 14, "unit": "mm",
         "help": "Laisse au moins 0,8 mm de fond sous les objets."},
        {"key": "rayon_coin_mm", "label": "Rayon des coins du bloc", "type": "number", "min": 0, "max": 10, "step": 0.5, "default": 4, "unit": "mm"},
        {"key": "encoche", "label": "Encoche pour les doigts", "type": "bool", "default": True,
         "help": "Un creux rond au bord de chaque poche, pour saisir l'objet."},
        {"key": "diametre_encoche_mm", "label": "Diamètre de l'encoche", "type": "number", "min": 12, "max": 40, "step": 1, "default": 22, "unit": "mm"},
        {"key": "cote_encoche", "label": "Côté de l'encoche", "type": "choice", "default": "auto",
         "options": [{"value": "auto", "label": "Auto"}, {"value": "haut", "label": "Haut"}, {"value": "bas", "label": "Bas"},
                     {"value": "gauche", "label": "Gauche"}, {"value": "droite", "label": "Droite"}]},
        {"key": "cadre_dans_plans", "label": "Cadre de l'insert dans le SVG et le DXF", "type": "bool", "default": False,
         "help": "Ajoute le contour du bloc (calque « cadre ») : découpe d'une mousse à la taille du bac."},
        {"key": "feuille_reperes", "label": "Joindre la feuille de repères A4 (PDF)", "type": "bool", "default": False,
         "help": "PDF à imprimer à 100 % : 4 repères et une règle de contrôle de 100 mm, pour des contours plus précis."},
        {"key": "titre", "label": "Nom du contour", "type": "text", "max": 60, "default": ""},
    ],
}


def run(params, inputs, out_dir, progress):
    import os
    # Aucun calcul BLAS ici : un seul thread OpenBLAS évite ~750 Mo de tampons réservés à l'import de numpy (24 cœurs).
    for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
        os.environ.setdefault(variable, "1")
    from .travail import executer
    return executer(params, inputs, out_dir, progress)
