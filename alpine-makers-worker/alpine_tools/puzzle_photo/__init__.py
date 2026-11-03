# -*- coding: utf-8 -*-
"""Alpine Tools — Puzzle photo (atool-puzzle-photo), portage d'Alpine Gift 1.0.10 (Puzzle.kt, PuzzleUi.kt ; Gravure,
Tramage, LaserStudio, Champ, Contours, Triangulation, Maillage, TroisMfPieces, ExportModele).

La photo devient un puzzle : gravure et découpe laser (SVG LightBurn, projet Alpine Laser Studio, PNG) ou pièces
imprimées en 3D (3MF à pièces, STL, OBJ) avec plateau de rangement ; pièce prénom facultative (police « Alpine Ronde »
dessinée dans ``police.py``, aucune police tierce). Découpe, tenons, SVG de découpe et maillages identiques à Gift
pour les mêmes réglages. Contrat : docs/ALPINE_TOOLS.md. ``TOOL`` est un littéral pur (lu par ``ast.literal_eval``) ;
les imports lourds sont faits dans ``run``.
"""

TOOL = {
    "id": "puzzle-photo",
    "name": "Puzzle photo",
    "machine": "laser",
    "description": "Ta photo devient un puzzle : gravée et découpée au laser, ou imprimée en 3D, avec une pièce prénom.",
    "inputs": [
        {"key": "photo", "label": "Photo", "accept": [".png", ".jpg", ".jpeg", ".webp"], "required": True, "max_mb": 25,
         "help": "Une photo de famille, d'un animal ou d'un paysage, recadrée au centre au format choisi. 24 Mpx au plus."},
    ],
    "params": [
        {"key": "sortie", "label": "Sortie", "type": "choice", "default": "laser",
         "options": [{"value": "laser", "label": "Gravure et découpe laser"},
                     {"value": "impression", "label": "Impression 3D"}]},
        {"key": "format", "label": "Format du puzzle", "type": "choice", "default": "4:3",
         "help": "Largeur / hauteur ; la photo est recadrée au centre à ce format.",
         "options": [{"value": "4:3", "label": "4:3"}, {"value": "3:4", "label": "3:4"}, {"value": "1:1", "label": "1:1"},
                     {"value": "16:9", "label": "16:9"}]},
        {"key": "largeur_mm", "label": "Largeur du puzzle", "type": "number", "min": 80, "max": 400, "step": 5,
         "default": 200, "unit": "mm"},
        {"key": "age", "label": "Âge", "type": "choice", "default": "6-7",
         "help": "Nombre de pièces visé et taille minimale des pièces.",
         "options": [{"value": "2-3", "label": "2–3 ans · 4"}, {"value": "4-5", "label": "4–5 ans · 12"},
                     {"value": "6-7", "label": "6–7 ans · 24"}, {"value": "8-10", "label": "8–10 ans · 48"},
                     {"value": "11+", "label": "11 ans et plus · 96"}, {"value": "adulte", "label": "Adulte · 192"}]},
        {"key": "pieces_choisies", "label": "Nombre de pièces choisi", "type": "bool", "default": False,
         "help": "Colonnes et lignes au choix, au lieu de l'âge."},
        {"key": "colonnes", "label": "Colonnes", "type": "number", "min": 2, "max": 30, "step": 1, "default": 6,
         "help": "Si « Nombre de pièces choisi » est coché."},
        {"key": "lignes", "label": "Lignes", "type": "number", "min": 2, "max": 30, "step": 1, "default": 4,
         "help": "Si « Nombre de pièces choisi » est coché."},
        {"key": "irregularite", "label": "Irrégularité des pièces", "type": "number", "min": 0, "max": 100, "step": 5,
         "default": 50, "unit": "%", "help": "0 % : pièces régulières ; 100 % : tenons et coins très variés."},
        {"key": "graine", "label": "Tirage des formes", "type": "number", "min": 1, "max": 999999, "step": 1,
         "default": 1, "help": "Change ce numéro pour tirer d'autres formes de pièces ; même numéro, mêmes pièces."},
        {"key": "prenom", "label": "Prénom (facultatif)", "type": "text", "max": 24, "default": "",
         "help": "Sur une pièce : gravé sur une plaque claire (laser) ou en relief (impression 3D). Police Alpine Ronde."},
        {"key": "colonne_prenom", "label": "Pièce du prénom : colonne", "type": "number", "min": 0, "max": 40, "step": 1,
         "default": 0, "help": "Depuis la gauche ; 0 : pièce du centre."},
        {"key": "ligne_prenom", "label": "Pièce du prénom : ligne", "type": "number", "min": 0, "max": 40, "step": 1,
         "default": 0, "help": "Depuis le haut ; 0 : pièce du centre."},
        {"key": "materiau", "label": "Matériau (laser)", "type": "choice", "default": "bois_clair",
         "options": [{"value": "bois_clair", "label": "Bois clair"}, {"value": "bois_fonce", "label": "Bois foncé"},
                     {"value": "ardoise", "label": "Ardoise"}, {"value": "verre", "label": "Verre"},
                     {"value": "cuir", "label": "Cuir"}, {"value": "acrylique_peint", "label": "Acrylique peint"},
                     {"value": "alu_anodise", "label": "Aluminium anodisé"}]},
        {"key": "resolution", "label": "Résolution (laser)", "type": "choice", "default": "10",
         "help": "Image limitée à 16 millions de pixels.",
         "options": [{"value": "10", "label": "10 px/mm · 254 ppp"}, {"value": "12.7", "label": "12,7 px/mm · 323 ppp"},
                     {"value": "20", "label": "20 px/mm · 508 ppp"}]},
        {"key": "mode", "label": "Tracés de découpe (laser)", "type": "choice", "default": "lignes",
         "options": [{"value": "lignes", "label": "Lignes communes (une passe par bord)"},
                     {"value": "pieces", "label": "Contour de chaque pièce"}]},
        {"key": "epaisseur_mm", "label": "Épaisseur des pièces (3D)", "type": "number", "min": 2, "max": 8, "step": 0.5,
         "default": 4.0, "unit": "mm"},
        {"key": "jeu_mm", "label": "Jeu par côté (3D)", "type": "number", "min": 0.05, "max": 0.4, "step": 0.01,
         "default": 0.15, "unit": "mm", "help": "À augmenter (0,2–0,25) si les pièces s'emboîtent trop serré."},
        {"key": "plateau", "label": "Plateau de rangement (3D)", "type": "bool", "default": True,
         "help": "Un cadre à bord, imprimé à côté des pièces."},
        {"key": "couleur_pieces", "label": "Couleur des pièces (3D)", "type": "color", "default": "#F2EEE8"},
        {"key": "couleur_prenom", "label": "Couleur du prénom (3D)", "type": "color", "default": "#F27A98"},
        {"key": "format_fichier", "label": "Format du fichier (3D)", "type": "choice", "default": "3mf",
         "options": [{"value": "3mf", "label": "3MF multicolore"}, {"value": "stl_couleurs", "label": "STL par couleur (ZIP)"},
                     {"value": "stl_unique", "label": "STL unique"}, {"value": "obj", "label": "OBJ avec couleurs (ZIP)"}]},
        {"key": "nom", "label": "Nom de la création (facultatif)", "type": "text", "max": 60, "default": ""},
    ],
}


def run(params, inputs, out_dir, progress):
    from .outil import executer
    return executer(params, inputs, out_dir, progress)
