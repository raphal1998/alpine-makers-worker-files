# -*- coding: utf-8 -*-
"""Alpine Tools — Tableau couleur (atool-tableau-couleur) : la photo devient un tableau 3D multicolore (plaque de base +
une couche de 2 à 8 couleurs, imprimée avec l'AMS).

Portage de l'application Alpine Gift (C:\\AlpineGift) : core TableauCouleur.kt, TableauMaillage.kt (TroisMf),
ExportModele.kt, et réglages de l'écran app/…/ui/TableauUi.kt (mêmes bornes et défauts). Contrat : docs/ALPINE_TOOLS.md.
``TOOL`` est un littéral pur (lu par le site sans importer numpy) ; les calculs sont dans les sous-modules.
"""

TOOL = {
    "id": "tableau-couleur",
    "name": "Tableau couleur",
    "machine": "impression",
    "description": "Ta photo devient un tableau 3D multicolore imprimé avec l'AMS (3MF, STL ou OBJ).",
    "inputs": [
        {"key": "photo", "label": "Photo", "accept": [".png", ".jpg", ".jpeg", ".webp"], "required": True, "max_mb": 25},
    ],
    "params": [
        {"key": "palette", "label": "Palette", "type": "choice", "default": "noir-rouge-or-blanc",
         "help": "Couleurs rangées du plus sombre au plus clair. « Mes couleurs » utilise la liste ci-dessous.",
         "options": [
             {"value": "noir-gris-blanc", "label": "Noir · Gris · Blanc"},
             {"value": "noir-blanc", "label": "Noir · Blanc"},
             {"value": "noir-olive-sable-blanc", "label": "Noir · Olive · Sable · Blanc"},
             {"value": "noir-nuit-gris-blanc", "label": "Noir · Bleu nuit · Gris · Blanc"},
             {"value": "noir-violet-azur-blanc", "label": "Noir · Violet · Azur · Blanc"},
             {"value": "violet-rose-orange-jaune", "label": "Violet · Rose · Orange · Jaune"},
             {"value": "noir-violet-rose-blanc", "label": "Noir · Violet · Rose · Blanc"},
             {"value": "noir-rouge-or-blanc", "label": "Noir · Rouge · Or · Blanc"},
             {"value": "noir-rouge-orange-jaune", "label": "Noir · Rouge · Orange · Jaune"},
             {"value": "noir-vert-anis-blanc", "label": "Noir · Vert · Anis · Blanc"},
             {"value": "noir-bleu-glacier-blanc", "label": "Noir · Bleu · Glacier · Blanc"},
             {"value": "prune-violet-lavande-blanc", "label": "Prune · Violet · Lavande · Blanc"},
             {"value": "noir-rouge-blanc", "label": "Noir · Rouge · Blanc"},
             {"value": "noir-jaune-blanc", "label": "Noir · Jaune · Blanc"},
             {"value": "noir-bleu-blanc", "label": "Noir · Bleu · Blanc"},
             {"value": "noir-vert-blanc", "label": "Noir · Vert · Blanc"},
             {"value": "noir-orange-blanc", "label": "Noir · Orange · Blanc"},
             {"value": "noir-rose-blanc", "label": "Noir · Rose · Blanc"},
             {"value": "noir-anthracite-perle-blanc", "label": "Noir · Anthracite · Perle · Blanc"},
             {"value": "marron-caramel-creme", "label": "Marron · Caramel · Crème"},
             {"value": "nuit-turquoise-blanc", "label": "Bleu nuit · Turquoise · Blanc"},
             {"value": "foret-sauge-creme", "label": "Vert forêt · Sauge · Crème"},
             {"value": "perso", "label": "Mes couleurs"},
         ]},
        {"key": "couleurs", "label": "Mes couleurs", "type": "colors", "min": 2, "max": 8,
         "default": ["#141414", "#B21F2A", "#D3A63A", "#F7F3EA"],
         "help": "Utilisées avec la palette « Mes couleurs » : une couleur par filament (noms du nuancier si connus)."},
        {"key": "nombre_couleurs", "label": "Nombre de couleurs", "type": "choice", "default": "palette",
         "help": "Moins : les dernières couleurs sont retirées ; plus : des couleurs du nuancier sont ajoutées.",
         "options": [{"value": "palette", "label": "Celui de la palette"}, {"value": "2", "label": "2"},
                     {"value": "3", "label": "3"}, {"value": "4", "label": "4"}, {"value": "5", "label": "5"},
                     {"value": "6", "label": "6"}, {"value": "7", "label": "7"}, {"value": "8", "label": "8"}]},
        {"key": "ordre_auto", "label": "Ordre automatique", "type": "bool", "default": True,
         "help": "Du plus sombre au plus clair : les zones sombres de la photo prennent la couleur la plus sombre. "
                 "Décoché : la 1re couleur remplit les zones les plus sombres."},
        {"key": "couleur_base", "label": "Couleur de la plaque de base", "type": "choice", "default": "auto",
         "help": "Numéro de la couleur (rangée du plus sombre au plus clair).",
         "options": [{"value": "auto", "label": "La plus claire"}, {"value": "1", "label": "1"},
                     {"value": "2", "label": "2"}, {"value": "3", "label": "3"}, {"value": "4", "label": "4"},
                     {"value": "5", "label": "5"}, {"value": "6", "label": "6"}, {"value": "7", "label": "7"},
                     {"value": "8", "label": "8"}]},
        {"key": "repartition", "label": "Répartition", "type": "choice", "default": "egale",
         "help": "Parts égales : chaque couleur couvre à peu près la même surface. Paliers réguliers : même plage de gris "
                 "(photos très claires ou très sombres).",
         "options": [{"value": "egale", "label": "Parts égales"}, {"value": "paliers", "label": "Paliers réguliers"}]},
        {"key": "luminosite", "label": "Luminosité", "type": "number", "min": -100, "max": 100, "step": 1, "default": 0},
        {"key": "contraste", "label": "Contraste", "type": "number", "min": -100, "max": 100, "step": 1, "default": 0},
        {"key": "gamma", "label": "Gamma", "type": "number", "min": 0.4, "max": 2.5, "step": 0.05, "default": 1.0},
        {"key": "forme", "label": "Forme", "type": "choice", "default": "rectangle",
         "options": [{"value": "rectangle", "label": "Rectangle"}, {"value": "arrondi", "label": "Arrondi"},
                     {"value": "cercle", "label": "Cercle"}, {"value": "coeur", "label": "Cœur"}]},
        {"key": "rayon_mm", "label": "Rayon des coins", "type": "number", "min": 2, "max": 40, "step": 1, "default": 8,
         "unit": "mm", "help": "Forme « Arrondi » seulement."},
        {"key": "cadre", "label": "Cadre", "type": "choice", "default": "f11",
         "help": "Recadrage centré dans la photo. Cercle : choisis 1:1 pour un rond parfait.",
         "options": [{"value": "f43", "label": "4:3"}, {"value": "f34", "label": "3:4"}, {"value": "f11", "label": "1:1"},
                     {"value": "f169", "label": "16:9"}, {"value": "libre", "label": "Libre"},
                     {"value": "photo", "label": "Photo entière"}]},
        {"key": "proportion", "label": "Proportion (largeur / hauteur)", "type": "number", "min": 0.4, "max": 2.5,
         "step": 0.01, "default": 1.3, "help": "Cadre « Libre » seulement."},
        {"key": "largeur_mm", "label": "Largeur", "type": "number", "min": 30, "max": 300, "step": 1, "default": 150,
         "unit": "mm"},
        {"key": "base_mm", "label": "Épaisseur de la plaque de base", "type": "number", "min": 0.8, "max": 4.0,
         "step": 0.1, "default": 2.0, "unit": "mm"},
        {"key": "couche_mm", "label": "Épaisseur de la couche couleur", "type": "number", "min": 0.2, "max": 1.6,
         "step": 0.05, "default": 0.6, "unit": "mm"},
        {"key": "pixel_mm", "label": "Finesse du pixel", "type": "choice", "default": "0.2",
         "options": [{"value": "0.1", "label": "0,10 mm"}, {"value": "0.15", "label": "0,15 mm"},
                     {"value": "0.2", "label": "0,20 mm"}, {"value": "0.3", "label": "0,30 mm"},
                     {"value": "0.4", "label": "0,40 mm"}]},
        {"key": "face_dessous", "label": "Face contre le plateau", "type": "bool", "default": False,
         "help": "Couche couleur imprimée en premier, image en miroir : surface lisse et brillante une fois retourné."},
        {"key": "nom", "label": "Nom de la création", "type": "text", "max": 60, "default": ""},
        {"key": "format", "label": "Format du fichier", "type": "choice", "default": "3mf",
         "help": "3MF : chaque couleur arrive sur son filament (Bambu Studio, OrcaSlicer). STL par couleur : un STL par "
                 "couleur à importer ensemble. STL unique : une seule pièce. OBJ : couleurs gardées (OBJ + MTL).",
         "options": [{"value": "3mf", "label": "3MF multicolore"}, {"value": "stl_couleurs", "label": "STL par couleur (ZIP)"},
                     {"value": "stl_unique", "label": "STL unique"}, {"value": "obj", "label": "OBJ avec couleurs (ZIP)"}]},
    ],
}


def run(params, inputs, out_dir, progress):
    from .outil import executer
    return executer(params, inputs, out_dir, progress)
