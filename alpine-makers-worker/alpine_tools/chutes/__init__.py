"""Alpine Chutes (atool-chutes) : la photo d'une chute de matière devient un plan de découpe.

Port de l'application Android Alpine Chutes 1.0.1 (C:\\AlpineChutes, cœur Kotlin) : mesure de la chute sur la feuille de
repères (homographie, redressement, détourage, bord au sous-pixel), pièces (SVG / DXF importés, rectangles, disques, texte
à la police au trait d'Alpine Makers), placement automatique sur grille fine (glouton « premier coin », essais,
amélioration progressive à graine fixe), exports SVG, DXF R12, projet Alpine Laser Studio (.alsproj) et G-code GRBL.
Contrat : docs/ALPINE_TOOLS.md. ``TOOL`` est un littéral pur (le site le lit sans importer le module) ; les calculs
(numpy, Pillow) sont importés dans ``run``. Les modules repris d'Alpine Contour (champ, detection, detourage, image, trace,
géométrie) sont des copies : un outil n'importe jamais un autre outil.
"""

TOOL = {
    "id": "chutes",
    "name": "Alpine Chutes",
    "machine": "laser",
    "description": "Photo d'une chute de matière → pièces placées automatiquement (SVG, DXF, Alpine Laser Studio, G-code).",
    "inputs": [
        {"key": "photo", "label": "Photo de la chute sur la feuille (facultative en saisie à la main)",
         "accept": [".png", ".jpg", ".jpeg", ".webp"], "required": False, "max_mb": 25},
        {"key": "pieces", "label": "Pièces à découper (SVG ou DXF)", "accept": [".svg", ".dxf"], "required": False, "max_mb": 5},
        {"key": "pieces2", "label": "Autres pièces (SVG ou DXF)", "accept": [".svg", ".dxf"], "required": False, "max_mb": 5},
    ],
    "params": [
        # ---------------------------------------------------------------- la chute
        {"key": "mode", "label": "Mesure de la chute", "type": "choice", "default": "sur_feuille",
         "options": [{"value": "sur_feuille", "label": "Petite chute posée sur la feuille (le plus précis)"},
                     {"value": "feuille_sur_chute", "label": "Grande chute : feuille posée à plat dessus"},
                     {"value": "saisie", "label": "Chute rectangulaire saisie à la main (sans photo)"}],
         "help": "Photo bien de dessus, la feuille entière visible, sur un fond plus sombre ou d'une autre couleur que la chute."},
        {"key": "format", "label": "Feuille de référence", "type": "choice", "default": "reperes_a4",
         "options": [{"value": "reperes_a4", "label": "Feuille de repères Alpine Chutes A4 (plus précis)"},
                     {"value": "a4", "label": "A4 blanche (210 × 297 mm)"},
                     {"value": "letter", "label": "Letter blanche (215,9 × 279,4 mm)"}],
         "help": "La feuille de repères s'imprime à 100 % (case « Joindre la feuille de repères »)."},
        {"key": "coins", "label": "Coins de la feuille (facultatif)", "type": "text", "max": 120, "default": "",
         "help": "Si la feuille n'est pas trouvée : 4 points en % de la photo (x y), haut-gauche, haut-droite, bas-droite, bas-gauche, "
                 "ex. 12 8; 88 9; 90 95; 10 93. Feuille de repères : centres des repères."},
        {"key": "origine_180", "label": "Feuille tournée de 180°", "type": "bool", "default": False,
         "help": "Le coin haut-gauche de la feuille est l'origine des fichiers ; coche si la feuille blanche est à l'envers."},
        {"key": "sensibilite", "label": "Sensibilité du détourage", "type": "number", "min": -1, "max": 1, "step": 0.1, "default": 0,
         "help": "À droite : les nuances proches du papier (ou de la matière) comptent aussi."},
        {"key": "ombres", "label": "Atténuer les ombres", "type": "bool", "default": True},
        {"key": "largeur_mm", "label": "Largeur (saisie à la main)", "type": "number", "min": 5, "max": 3000, "step": 0.5, "default": 300, "unit": "mm"},
        {"key": "hauteur_mm", "label": "Hauteur (saisie à la main)", "type": "number", "min": 5, "max": 3000, "step": 0.5, "default": 200, "unit": "mm"},
        {"key": "rayon_coins_mm", "label": "Rayon des coins (saisie à la main)", "type": "number", "min": 0, "max": 1500, "step": 0.5, "default": 0, "unit": "mm"},
        {"key": "matiere", "label": "Matière", "type": "text", "max": 60, "default": "", "help": "Ex. MDF, contreplaqué de bouleau, acrylique coulé."},
        {"key": "epaisseur_mm", "label": "Épaisseur", "type": "number", "min": 0.1, "max": 200, "step": 0.1, "default": 3, "unit": "mm"},
        {"key": "marge_mm", "label": "Marge au bord de la chute", "type": "number", "min": 0, "max": 15, "step": 0.5, "default": 2, "unit": "mm",
         "help": "Bande laissée libre le long du bord : bord abîmé ou brûlé, petite imprécision de la photo (3 mm pour une chute épaisse)."},
        {"key": "fil", "label": "Sens du fil (bois)", "type": "choice", "default": "aucun",
         "options": [{"value": "aucun", "label": "Sans fil"}, {"value": "horizontal", "label": "Horizontal"}, {"value": "vertical", "label": "Vertical"}]},
        {"key": "interdits", "label": "Zones interdites (facultatif)", "type": "text", "max": 400, "default": "",
         "help": "Nœud, trou, brûlure : « x y rayon » (disque) ou « x1 y1 x2 y2 rayon » (trait), en mm depuis le coin haut-gauche de la "
                 "feuille (de la chute en saisie à la main), séparées par « ; ». Rayon de 1 à 25 mm."},
        # ---------------------------------------------------------------- les pièces
        {"key": "fichier_quantite", "label": "Exemplaires du fichier de pièces", "type": "number", "min": 0, "max": 500, "step": 1, "default": 1, "integer": True},
        {"key": "fichier_echelle", "label": "Échelle du fichier", "type": "number", "min": 0.02, "max": 100, "step": 0.01, "default": 1},
        {"key": "fichier_miroir", "label": "Fichier en miroir", "type": "bool", "default": False},
        {"key": "fichier_rotation", "label": "Rotation du fichier", "type": "choice", "default": "quart",
         "options": [{"value": "quart", "label": "Par 90°"}, {"value": "libre", "label": "Libre"}, {"value": "aucune", "label": "Aucune"}]},
        {"key": "fichier2_quantite", "label": "Exemplaires des autres pièces", "type": "number", "min": 0, "max": 500, "step": 1, "default": 1, "integer": True},
        {"key": "fichier2_echelle", "label": "Échelle des autres pièces", "type": "number", "min": 0.02, "max": 100, "step": 0.01, "default": 1},
        {"key": "fichier2_miroir", "label": "Autres pièces en miroir", "type": "bool", "default": False},
        {"key": "fichier2_rotation", "label": "Rotation des autres pièces", "type": "choice", "default": "quart",
         "options": [{"value": "quart", "label": "Par 90°"}, {"value": "libre", "label": "Libre"}, {"value": "aucune", "label": "Aucune"}]},
        {"key": "rect_quantite", "label": "Rectangles : exemplaires", "type": "number", "min": 0, "max": 500, "step": 1, "default": 0, "integer": True},
        {"key": "rect_largeur", "label": "Largeur du rectangle", "type": "number", "min": 0.5, "max": 3000, "step": 0.5, "default": 80, "unit": "mm"},
        {"key": "rect_hauteur", "label": "Hauteur du rectangle", "type": "number", "min": 0.5, "max": 3000, "step": 0.5, "default": 50, "unit": "mm"},
        {"key": "rect_rayon", "label": "Rayon des coins du rectangle", "type": "number", "min": 0, "max": 1500, "step": 0.5, "default": 0, "unit": "mm"},
        {"key": "rect_trou", "label": "Trou central du rectangle (Ø)", "type": "number", "min": 0, "max": 3000, "step": 0.5, "default": 0, "unit": "mm"},
        {"key": "liste_rectangles", "label": "Autres rectangles (facultatif)", "type": "text", "max": 400, "default": "",
         "help": "Liste de débit : « largeur x hauteur x quantité » séparés par « ; », ex. 120 x 40 x 2; 60 x 60 x 4."},
        {"key": "rect_rotation", "label": "Rotation des rectangles", "type": "choice", "default": "quart",
         "options": [{"value": "quart", "label": "Par 90°"}, {"value": "libre", "label": "Libre"}, {"value": "aucune", "label": "Aucune"}]},
        {"key": "cercle_quantite", "label": "Disques : exemplaires", "type": "number", "min": 0, "max": 500, "step": 1, "default": 0, "integer": True},
        {"key": "cercle_diametre", "label": "Diamètre du disque", "type": "number", "min": 1, "max": 3000, "step": 0.5, "default": 60, "unit": "mm"},
        {"key": "cercle_trou", "label": "Trou central du disque (Ø)", "type": "number", "min": 0, "max": 3000, "step": 0.5, "default": 0, "unit": "mm"},
        {"key": "texte", "label": "Texte à découper (facultatif)", "type": "text", "max": 60, "default": "",
         "help": "Police au trait d'Alpine Makers (capitales, chiffres, accents, ponctuation courante)."},
        {"key": "texte_quantite", "label": "Texte : exemplaires", "type": "number", "min": 0, "max": 500, "step": 1, "default": 1, "integer": True},
        {"key": "texte_hauteur", "label": "Hauteur des lettres", "type": "number", "min": 4, "max": 3000, "step": 0.5, "default": 25, "unit": "mm"},
        {"key": "texte_trait", "label": "Épaisseur du trait des lettres", "type": "number", "min": 0.8, "max": 1000, "step": 0.1, "default": 2.5, "unit": "mm",
         "help": "Au plus un tiers de la hauteur des lettres."},
        {"key": "texte_pochoir", "label": "Texte en pochoir", "type": "bool", "default": False,
         "help": "Ponts qui retiennent l'intérieur des lettres (O, A, B…)."},
        {"key": "texte_rotation", "label": "Rotation du texte", "type": "choice", "default": "aucune",
         "options": [{"value": "aucune", "label": "Aucune"}, {"value": "quart", "label": "Par 90°"}, {"value": "libre", "label": "Libre"}]},
        {"key": "suit_fil", "label": "Les pièces suivent le fil", "type": "bool", "default": False,
         "help": "Pièces posées dans le sens du fil de la chute (à 0° ou 180°)."},
        # ---------------------------------------------------------------- le placement
        {"key": "kerf_mm", "label": "Trait de coupe (kerf)", "type": "number", "min": 0, "max": 1.5, "step": 0.05, "default": 0.15, "unit": "mm"},
        {"key": "espacement_mm", "label": "Espacement entre pièces", "type": "number", "min": 0, "max": 10, "step": 0.5, "default": 1, "unit": "mm"},
        {"key": "pas_angle_deg", "label": "Pas des angles (rotation libre)", "type": "number", "min": 5, "max": 45, "step": 5, "default": 15, "unit": "°"},
        {"key": "pas_grille_mm", "label": "Pas de la grille (0 = automatique)", "type": "number", "min": 0, "max": 2, "step": 0.25, "default": 0, "unit": "mm",
         "help": "0,5 mm jusqu'à environ 300 × 300 mm, plus grossier au-delà."},
        {"key": "essais", "label": "Essais d'amélioration", "type": "number", "min": 0, "max": 400, "step": 10, "default": 60, "integer": True,
         "help": "Après les essais systématiques (20 au plus), échanges aléatoires de l'ordre des pièces, graine fixe : "
                 "même résultat à chaque calcul. Plus d'essais = plan parfois meilleur, calcul plus long."},
        # ---------------------------------------------------------------- les fichiers
        {"key": "origine", "label": "Origine des fichiers", "type": "choice", "default": "feuille",
         "options": [{"value": "feuille", "label": "Coin de la feuille (la chute reste sur la feuille, feuille calée sur l'origine)"},
                     {"value": "chute", "label": "Coin de la chute (alignement sur le bord de la chute)"}]},
        {"key": "svg", "label": "SVG (mm) pour la découpe laser", "type": "bool", "default": True,
         "help": "LightBurn, Inkscape, Illustrator : 1 unité = 1 mm, calques de couleur."},
        {"key": "dxf", "label": "DXF R12", "type": "bool", "default": True, "help": "CAO / FAO, LightBurn, Estlcam, VCarve : Y vers le haut."},
        {"key": "laser_studio", "label": "Projet Alpine Laser Studio (.alsproj)", "type": "bool", "default": True},
        {"key": "gcode", "label": "G-code GRBL (laser)", "type": "bool", "default": False, "help": "Découpe directe : trous d'abord, puis contours."},
        {"key": "reperes", "label": "Repères dans les fichiers", "type": "bool", "default": True,
         "help": "Bord de la chute (bleu) et zones interdites (gris) sur des calques à ne pas découper."},
        {"key": "compenser_kerf", "label": "Compenser le trait de coupe", "type": "bool", "default": False,
         "help": "Décale chaque contour d'un demi-trait hors de la pièce (sinon règle le kerf dans ton logiciel, jamais les deux)."},
        {"key": "puissance_pct", "label": "G-code : puissance", "type": "number", "min": 5, "max": 100, "step": 5, "default": 80, "unit": "%"},
        {"key": "vitesse_mm_min", "label": "G-code : vitesse", "type": "number", "min": 50, "max": 3000, "step": 50, "default": 300, "unit": "mm/min"},
        {"key": "passes", "label": "G-code : passes", "type": "number", "min": 1, "max": 10, "step": 1, "default": 1, "integer": True},
        {"key": "s_max", "label": "G-code : S max", "type": "choice", "default": 1000,
         "options": [{"value": 1000, "label": "S max 1000"}, {"value": 255, "label": "S max 255"}]},
        {"key": "air_assist", "label": "G-code : air assist (M8 / M9)", "type": "bool", "default": False},
        {"key": "feuille_reperes", "label": "Joindre la feuille de repères A4 (PDF)", "type": "bool", "default": False,
         "help": "À imprimer à 100 % : 4 repères et une règle de contrôle de 100 mm."},
        {"key": "titre", "label": "Nom du plan", "type": "text", "max": 60, "default": ""},
    ],
}


def run(params, inputs, out_dir, progress):
    import os
    # Aucun calcul BLAS ici : un seul thread OpenBLAS évite ~750 Mo de tampons réservés à l'import de numpy (24 cœurs).
    for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
        os.environ.setdefault(variable, "1")
    from .travail import executer
    return executer(params, inputs, out_dir, progress)
