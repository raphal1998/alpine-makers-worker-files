# -*- coding: utf-8 -*-
"""Alpine Tools — Ciel étoilé (atool-ciel-etoile) : la carte du ciel d'une date, d'une heure et d'un lieu, en gravure
laser (projet Alpine Laser Studio, SVG LightBurn, PNG) ou en plaque 3D multicolore (3MF, STL, OBJ).

Portage de l'application Alpine Gift 1.0.7 (C:\\AlpineGift) : core CielEtoile.kt, Astro.kt, Catalogue.kt, Trame.kt,
LaserStudio.kt, Png.kt, TableauMaillage.kt (TroisMf), ExportModele.kt et réglages de l'écran app/…/ui/CielUi.kt
(mêmes bornes et défauts). Contrat : docs/ALPINE_TOOLS.md. ``TOOL`` est un littéral pur (lu par le site sans importer
numpy) ; les calculs sont dans les sous-modules. Pas de recherche de lieu (réseau interdit) : latitude et longitude
se saisissent ; textes en police « Alpine Ronde » dessinée pour les Alpine Tools (aucune police TTF).
"""

TOOL = {
    "id": "ciel-etoile",
    "name": "Ciel étoilé",
    "machine": "laser",
    "description": "La carte du ciel d'une date et d'un lieu : gravure laser (Alpine Laser Studio, SVG, PNG) ou plaque 3D "
                   "multicolore (3MF, STL, OBJ).",
    "inputs": [],
    "params": [
        {"key": "date", "label": "Date", "type": "text", "max": 10, "default": "",
         "help": "AAAA-MM-JJ, de 1900 à 2100 (par exemple 2025-06-14). Vide : aujourd'hui."},
        {"key": "heure", "label": "Heure locale", "type": "text", "max": 5, "default": "22:00",
         "help": "HH:MM, à l'heure du lieu (heure d'été comprise)."},
        {"key": "fuseau", "label": "Fuseau horaire du lieu", "type": "choice", "default": "Europe/Zurich",
         "options": [
             {"value": "Europe/Zurich", "label": "Zurich — Suisse, France, Allemagne, Italie"},
             {"value": "Europe/London", "label": "London — Royaume-Uni, Portugal"},
             {"value": "Europe/Athens", "label": "Athens — Grèce, Finlande"},
             {"value": "America/New_York", "label": "New York — New York, Montréal"},
             {"value": "America/Los_Angeles", "label": "Los Angeles — Californie"},
             {"value": "Asia/Tokyo", "label": "Tokyo — Japon"},
             {"value": "Australia/Sydney", "label": "Sydney — Sydney"},
             {"value": "UTC", "label": "UTC — Temps universel"},
         ]},
        {"key": "lieu", "label": "Lieu", "type": "text", "max": 60, "default": "Sion",
         "help": "Nom écrit sous la carte (ville, village, lieu)."},
        {"key": "latitude", "label": "Latitude", "type": "number", "min": -90, "max": 90, "step": 0.0001,
         "default": 46.2331, "unit": "°", "help": "Degrés décimaux, nord positif (Sion : 46,2331)."},
        {"key": "longitude", "label": "Longitude", "type": "number", "min": -180, "max": 180, "step": 0.0001,
         "default": 7.3606, "unit": "°", "help": "Degrés décimaux, est positif, ouest négatif (Sion : 7,3606)."},
        {"key": "titre", "label": "Titre", "type": "text", "max": 40, "default": "Notre ciel"},
        {"key": "message", "label": "Message (facultatif)", "type": "text", "max": 60, "default": ""},
        {"key": "ligne_date", "label": "Date, heure et lieu", "type": "bool", "default": True,
         "help": "Sous la carte, avec les coordonnées."},
        {"key": "graisse_titre", "label": "Police du titre", "type": "choice", "default": "grasse",
         "help": "Police Alpine Ronde, dessinée pour les Alpine Tools.",
         "options": [{"value": "fine", "label": "Fine"}, {"value": "normale", "label": "Normale"},
                     {"value": "grasse", "label": "Grasse"}]},
        {"key": "forme", "label": "Forme", "type": "choice", "default": "affiche",
         "help": "Le disque ne porte que les points cardinaux : choisis l'affiche pour graver un titre et un message.",
         "options": [{"value": "affiche", "label": "Affiche avec texte"}, {"value": "disque", "label": "Disque"}]},
        {"key": "diametre_mm", "label": "Diamètre du ciel", "type": "number", "min": 80, "max": 300, "step": 1,
         "default": 160, "unit": "mm"},
        {"key": "magnitude", "label": "Étoiles jusqu'à la magnitude", "type": "number", "min": 3.5, "max": 6.5,
         "step": 0.1, "default": 5.5,
         "help": "5,5 : le ciel d'une campagne sans lune ; 6,5 : toutes les étoiles visibles à l'œil nu."},
        {"key": "taille_etoiles", "label": "Taille des étoiles", "type": "number", "min": 0.6, "max": 1.8,
         "step": 0.05, "default": 1.0, "help": "1 = 100 %."},
        {"key": "lignes", "label": "Lignes des constellations", "type": "bool", "default": True},
        {"key": "noms", "label": "Noms des constellations", "type": "bool", "default": False,
         "help": "En français, au centre de chaque figure."},
        {"key": "lune", "label": "Lune (avec sa phase)", "type": "bool", "default": True},
        {"key": "planetes", "label": "Planètes visibles", "type": "bool", "default": True,
         "help": "Mercure, Vénus, Mars, Jupiter et Saturne, avec leur nom."},
        {"key": "cardinaux", "label": "Points cardinaux", "type": "bool", "default": True,
         "help": "N, E, S, O autour de l'horizon (l'est à gauche, comme vu couché sur le dos)."},
        {"key": "sortie", "label": "Sortie", "type": "choice", "default": "laser",
         "options": [{"value": "laser", "label": "Gravure laser"}, {"value": "plaque", "label": "Plaque 3D multicolore"}]},
        {"key": "style", "label": "Gravure laser", "type": "choice", "default": "etoiles",
         "help": "Gravure laser seulement.",
         "options": [{"value": "etoiles", "label": "Étoiles gravées"}, {"value": "ciel", "label": "Ciel gravé, étoiles claires"}]},
        {"key": "materiau", "label": "Matériau", "type": "choice", "default": "bois_clair",
         "help": "Gravure laser : aperçu et nom du calque.",
         "options": [{"value": "bois_clair", "label": "Bois clair"}, {"value": "bois_fonce", "label": "Bois foncé"},
                     {"value": "ardoise", "label": "Ardoise"}, {"value": "verre", "label": "Verre"},
                     {"value": "cuir", "label": "Cuir"}, {"value": "acrylique_peint", "label": "Acrylique peint"},
                     {"value": "alu_anodise", "label": "Aluminium anodisé"}]},
        {"key": "resolution", "label": "Résolution", "type": "choice", "default": "10",
         "help": "Gravure laser : 10 px/mm = 254 ppp.",
         "options": [{"value": "10", "label": "10 px/mm"}, {"value": "12.7", "label": "12,7 px/mm"},
                     {"value": "20", "label": "20 px/mm"}]},
        {"key": "couleurs", "label": "Couleurs de la plaque", "type": "colors", "min": 4, "max": 4,
         "default": ["#13234A", "#F4F4F4", "#D3A63A", "#F0E2C6"],
         "help": "Plaque 3D, dans l'ordre des filaments : 1 fond du ciel, 2 étoiles, Lune, planètes, "
                 "3 constellations et noms, 4 textes et horizon."},
        {"key": "base_mm", "label": "Épaisseur de la plaque", "type": "number", "min": 1, "max": 4, "step": 0.1,
         "default": 1.6, "unit": "mm"},
        {"key": "couche_mm", "label": "Épaisseur de la couche couleur", "type": "number", "min": 0.4, "max": 1.2,
         "step": 0.05, "default": 0.6, "unit": "mm"},
        {"key": "face_dessous", "label": "Face contre le plateau", "type": "bool", "default": True,
         "help": "Couleurs imprimées en premier : ciel lisse et brillant une fois retourné."},
        {"key": "format", "label": "Format du fichier", "type": "choice", "default": "3mf",
         "help": "Plaque 3D. 3MF : chaque couleur arrive sur son filament (Bambu Studio, OrcaSlicer). STL par couleur : "
                 "un STL par couleur à importer ensemble. STL unique : une seule pièce. OBJ : couleurs gardées (OBJ + MTL).",
         "options": [{"value": "3mf", "label": "3MF multicolore"}, {"value": "stl_couleurs", "label": "STL par couleur (ZIP)"},
                     {"value": "stl_unique", "label": "STL unique"}, {"value": "obj", "label": "OBJ avec couleurs (ZIP)"}]},
        {"key": "nom", "label": "Nom de la création (facultatif)", "type": "text", "max": 60, "default": ""},
    ],
}


def run(params, inputs, out_dir, progress):
    from .outil import executer
    return executer(params, inputs, out_dir, progress)
