"""Relief souvenir (atool-relief-souvenir) : randonnée, via ferrata ou voie d'escalade en relief 3D multicolore.

Port de l'outil « Relief souvenir » d'Alpine Gift (Relief.kt, ReliefFace.kt, Relief3D.kt, Geo.kt, Gpx.kt,
TableauMaillage.kt, ExportModele.kt, Terrain.kt). Contrat : docs/ALPINE_TOOLS.md. TOOL est un littéral pur (lu par le
site sans importer numpy) ; le calcul est dans moteur.py, importé par run().
Réseau autorisé pour cet outil seulement : swisstopo, Terrain Tiles (AWS Open Data), Overpass (OpenStreetMap).
"""

TOOL = {
    "id": "relief-souvenir",
    "name": "Relief souvenir",
    "machine": "impression",
    "description": "Randonnée, via ferrata ou voie d'escalade en relief 3D multicolore (3MF, STL, OBJ).",
    "inputs": [
        {"key": "gpx", "label": "Tracé GPX (facultatif)", "accept": [".gpx"], "required": False, "max_mb": 30,
         "help": "Le cadre suit le tracé. Sans GPX, indique le centre (latitude, longitude) et la taille de la zone."},
    ],
    "params": [
        {"key": "type", "label": "Souvenir", "type": "choice", "default": "randonnee",
         "options": [{"value": "randonnee", "label": "Randonnée"}, {"value": "via_ferrata", "label": "Via ferrata"},
                     {"value": "escalade", "label": "Voie d'escalade"}]},
        {"key": "vue", "label": "Vue", "type": "choice", "default": "auto",
         "help": "Automatique : Carte pour une randonnée, Montagne 3D pour une via ferrata ou une voie d'escalade.",
         "options": [{"value": "auto", "label": "Automatique"}, {"value": "carte", "label": "Carte (vu de dessus)"},
                     {"value": "montagne", "label": "Montagne 3D"}, {"value": "face", "label": "Face (la paroi de face)"}]},
        {"key": "latitude", "label": "Latitude du centre", "type": "number", "min": -85, "max": 85, "step": 0.0001, "default": None,
         "unit": "°", "help": "Sans GPX : centre de la zone (WGS84, ex. 46.5582)."},
        {"key": "longitude", "label": "Longitude du centre", "type": "number", "min": -180, "max": 180, "step": 0.0001, "default": None,
         "unit": "°", "help": "Sans GPX : centre de la zone (WGS84, ex. 7.9658)."},
        {"key": "taille_km", "label": "Taille de la zone", "type": "number", "min": 0.5, "max": 30, "step": 0.5, "default": 6, "unit": "km",
         "help": "Sans GPX. En Montagne 3D ou Face, 1 à 3 km autour de la paroi donnent le plus de détail."},
        {"key": "voie", "label": "Via ferrata : nom ou numéro de la voie", "type": "text", "max": 60, "default": "",
         "help": "Parmi les via ferrata d'OpenStreetMap trouvées autour du centre (numéros listés dans les détails). Vide : la plus proche."},
        {"key": "cadrer_voie", "label": "Cadrer sur la via ferrata", "type": "bool", "default": True,
         "help": "Montagne 3D et Face : altitudes rechargées au plus près de la voie choisie (plus de détail sur la paroi)."},
        {"key": "forme", "label": "Forme (vue Carte)", "type": "choice", "default": "rectangle",
         "options": [{"value": "rectangle", "label": "Rectangle"}, {"value": "cercle", "label": "Cercle"},
                     {"value": "hexagone", "label": "Hexagone"}]},
        {"key": "largeur_mm", "label": "Largeur", "type": "number", "min": 40, "max": 250, "step": 1, "default": 150, "unit": "mm"},
        {"key": "exageration", "label": "Exagération verticale", "type": "number", "min": 0.5, "max": 5, "step": 0.1, "default": 1.5,
         "help": "En Montagne 3D, la valeur par défaut ×1,5 devient ×1 : la paroi garde sa vraie forme. Sans effet en vue Face."},
        {"key": "finesse_mm", "label": "Finesse du maillage", "type": "choice", "default": "0.5",
         "options": [{"value": "0.4", "label": "0,4 mm"}, {"value": "0.5", "label": "0,5 mm"}, {"value": "0.8", "label": "0,8 mm"},
                     {"value": "1.0", "label": "1,0 mm"}]},
        {"key": "palette", "label": "Couleurs par altitude", "type": "choice", "default": "auto",
         "help": "Automatique : forêt · gris · blanc pour une randonnée, ardoise · gris · calcaire pour une paroi.",
         "options": [{"value": "auto", "label": "Automatique"}, {"value": "perso", "label": "Mes couleurs (ci-dessous)"},
                     {"value": "foret-neige", "label": "Forêt · neige"}, {"value": "alpage", "label": "Alpage · roche · neige"},
                     {"value": "paroi", "label": "Paroi · roche"}, {"value": "hypsometrie", "label": "Carte hypsométrique"},
                     {"value": "glacier", "label": "Glacier"}, {"value": "desert", "label": "Désert"},
                     {"value": "noir-gris-blanc", "label": "Noir · gris · blanc"}, {"value": "noir-vert-anis-blanc", "label": "Noir · vert · anis · blanc"},
                     {"value": "noir-bleu-glacier-blanc", "label": "Noir · bleu · glacier · blanc"}]},
        {"key": "couleurs", "label": "Mes couleurs (de la plus basse à la plus haute)", "type": "colors", "min": 2, "max": 8,
         "default": ["#1D3B2A", "#8C8C8C", "#F4F4F4"]},
        {"key": "repartition", "label": "Répartition des couleurs", "type": "choice", "default": "surface",
         "options": [{"value": "surface", "label": "Surfaces égales"}, {"value": "altitude", "label": "Altitudes égales"}]},
        {"key": "lacs", "label": "Lacs plats", "type": "bool", "default": False},
        {"key": "niveau_lac_m", "label": "Altitude de l'eau", "type": "number", "min": -500, "max": 9000, "step": 1, "default": None,
         "unit": "m", "help": "Vide : point le plus bas + 1 m."},
        {"key": "couleur_lac", "label": "Couleur des lacs", "type": "color", "default": "#1E5EBE"},
        {"key": "trace", "label": "Tracé en relief", "type": "bool", "default": True,
         "help": "Carte : ruban posé sur le terrain. Montagne 3D : tube posé sur la paroi, qui en ressort de côté."},
        {"key": "couleur_trace", "label": "Couleur du tracé", "type": "color", "default": "#BE202E"},
        {"key": "largeur_trace_mm", "label": "Largeur du tracé", "type": "number", "min": 0.6, "max": 4, "step": 0.1, "default": 1.4, "unit": "mm"},
        {"key": "hauteur_trace_mm", "label": "Hauteur du tracé", "type": "number", "min": 0.2, "max": 3, "step": 0.1, "default": 0.8, "unit": "mm"},
        {"key": "reperes", "label": "Repères départ / relais / sommet", "type": "choice", "default": "auto",
         "help": "Automatique : oui pour une via ferrata ou une voie d'escalade.",
         "options": [{"value": "auto", "label": "Automatique"}, {"value": "oui", "label": "Oui"}, {"value": "non", "label": "Non"}]},
        {"key": "relais", "label": "Relais", "type": "number", "min": 0, "max": 12, "step": 1, "default": 0},
        {"key": "couleur_reperes", "label": "Couleur des repères", "type": "color", "default": "#F4F4F4"},
        {"key": "socle", "label": "Socle plat", "type": "bool", "default": True, "help": "Décoché : relief seul, sans socle ni texte."},
        {"key": "couleur_socle", "label": "Couleur du socle", "type": "color", "default": "#1D3B2A"},
        {"key": "texte1", "label": "Ligne 1 (sommet, randonnée, nom de la voie)", "type": "text", "max": 40, "default": ""},
        {"key": "texte2", "label": "Ligne 2 (date, distance, cotation)", "type": "text", "max": 60, "default": ""},
        {"key": "texte_auto", "label": "Compléter le texte vide", "type": "bool", "default": True,
         "help": "Nom du GPX ou de la via ferrata ; date, distance et dénivelé du GPX."},
        {"key": "couleur_texte", "label": "Couleur du texte", "type": "color", "default": "#F4F4F4"},
        {"key": "bandeau_mm", "label": "Hauteur du bandeau de texte", "type": "number", "min": 0, "max": 30, "step": 1, "default": 14,
         "unit": "mm", "help": "0 : sans bandeau ni texte ; sinon 8 mm au moins."},
        {"key": "orientation_face", "label": "Vue Face : orientation de la paroi", "type": "choice", "default": "auto",
         "help": "Direction vers laquelle la paroi regarde. Automatique : d'après le tracé (sinon face sud).",
         "options": [{"value": "auto", "label": "Automatique"}, {"value": "0", "label": "Face nord"}, {"value": "45", "label": "Face nord-est"},
                     {"value": "90", "label": "Face est"}, {"value": "135", "label": "Face sud-est"}, {"value": "180", "label": "Face sud"},
                     {"value": "225", "label": "Face sud-ouest"}, {"value": "270", "label": "Face ouest"}, {"value": "315", "label": "Face nord-ouest"}]},
        {"key": "relief_face_mm", "label": "Vue Face : épaisseur du relief", "type": "number", "min": 4, "max": 40, "step": 1, "default": 16, "unit": "mm"},
        {"key": "profondeur_face", "label": "Vue Face : profondeur", "type": "number", "min": 0.3, "max": 3, "step": 0.1, "default": 1.0},
        {"key": "couleur_ciel", "label": "Vue Face : couleur du ciel", "type": "color", "default": "#9ED2EC"},
        {"key": "alt_bas_m", "label": "Vue Face : bas du cadre", "type": "number", "min": -500, "max": 9000, "step": 1, "default": None,
         "unit": "m", "help": "Vide : automatique (pied visible de la paroi)."},
        {"key": "alt_haut_m", "label": "Vue Face : haut du cadre", "type": "number", "min": -500, "max": 9000, "step": 1, "default": None,
         "unit": "m", "help": "Vide : automatique (sommet le plus haut)."},
        {"key": "vue_depuis", "label": "Montagne 3D : aperçu vu depuis", "type": "choice", "default": "auto",
         "help": "Automatique : devant la face que parcourt la voie.",
         "options": [{"value": "auto", "label": "Automatique"}, {"value": "0", "label": "Le nord"}, {"value": "45", "label": "Le nord-est"},
                     {"value": "90", "label": "L'est"}, {"value": "135", "label": "Le sud-est"}, {"value": "180", "label": "Le sud"},
                     {"value": "225", "label": "Le sud-ouest"}, {"value": "270", "label": "L'ouest"}, {"value": "315", "label": "Le nord-ouest"}]},
        {"key": "hauteur_vue", "label": "Montagne 3D : hauteur de vue", "type": "number", "min": 10, "max": 70, "step": 1, "default": 28, "unit": "°"},
        {"key": "format", "label": "Format du fichier", "type": "choice", "default": "3mf",
         "options": [{"value": "3mf", "label": "3MF multicolore"}, {"value": "stl_couleurs", "label": "STL par couleur (ZIP)"},
                     {"value": "stl", "label": "STL unique"}, {"value": "obj", "label": "OBJ avec couleurs (ZIP)"}]},
    ],
}

# Téléchargeur de remplacement (tests) : fonction (url, max_octets) -> bytes ; None = réseau réel (urllib, 20 s).
TELECHARGER = None


def run(params, inputs, out_dir, progress, telecharger=None):
    """Calcule le relief sur le Worker ; voir docs/ALPINE_TOOLS.md pour le format du résultat."""
    from pathlib import Path

    from . import moteur
    return moteur.executer(params, inputs, Path(out_dir), progress, telecharger or TELECHARGER)
