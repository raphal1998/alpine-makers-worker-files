"""Registre des Alpine Tools (agent 1.53.0, cinq outils de plus en 1.53.2) : source unique des identifiants, lue par
l'agent, l'installateur et le site.

Bibliothèque standard seulement (le site l'importe sans numpy). Contrat : docs/ALPINE_TOOLS.md.
"""

# (tool_id du catalogue, paquet Python, nom de l'onglet, machine, description courte)
TOOLS = (
    ("atool-lithophanie", "lithophanie", "Lithophanie", "impression",
     "Ta photo devient un tableau de lumière imprimé en 3D (STL)."),
    ("atool-lithophanie-volume", "lithophanie_volume", "Lithophanie en volume", "impression",
     "Ta photo en lithophanie cylindre, abat-jour, globe, cœur ou boîte (STL, 3MF, OBJ)."),
    ("atool-dessin-enfant", "dessin_enfant", "Dessin d'enfant", "impression",
     "La photo d'un dessin devient emporte-pièce, porte-clés, magnet ou tampon (STL, 3MF)."),
    ("atool-puzzle-photo", "puzzle_photo", "Puzzle photo", "laser",
     "Ta photo en puzzle : gravure et découpe laser ou impression 3D, avec une pièce prénom."),
    ("atool-tableau-couleur", "tableau_couleur", "Tableau couleur", "impression",
     "Ta photo en tableau 3D multicolore pour l'AMS (3MF, STL, OBJ)."),
    ("atool-relief-souvenir", "relief_souvenir", "Relief souvenir", "impression",
     "Randonnée, via ferrata ou voie d'escalade en relief 3D multicolore (3MF, STL, OBJ)."),
    ("atool-gravure-photo", "gravure_photo", "Gravure photo laser", "laser",
     "Photo tramée à la taille réelle, prête pour Alpine Laser Studio (.alsproj, PNG)."),
    ("atool-porte-cles", "porte_cles", "Porte-clés et plaques", "impression",
     "Prénom ou message en relief : STL bicolores et SVG laser."),
    ("atool-image-annonce", "image_annonce", "Image d'annonce", "calcul",
     "Un visuel carré soigné pour vendre tes créations (PNG)."),
    ("atool-ciel-etoile", "ciel_etoile", "Ciel étoilé", "laser",
     "La carte du ciel d'une date et d'un lieu, en gravure laser ou en plaque 3D multicolore."),
    ("atool-contour", "contour", "Alpine Contour", "cnc",
     "Photo d'un objet sur une feuille → contour à l'échelle (SVG, DXF, STL, insert)."),
    ("atool-chutes", "chutes", "Alpine Chutes", "laser",
     "Photo d'une chute de matière → pièces placées automatiquement (SVG, DXF, Alpine Laser Studio, G-code)."),
    ("atool-estimation-filament", "estimation_filament", "Estimation impression filament", "calcul",
     "Matière, temps et prix d'une impression 3D filament."),
    ("atool-estimation-resine", "estimation_resine", "Estimation impression résine", "calcul",
     "Matière, temps et prix d'une impression 3D résine."),
    ("atool-estimation-gravure", "estimation_gravure", "Estimation gravure laser", "calcul",
     "Surface, temps et prix d'une gravure laser."),
    ("atool-estimation-decoupe", "estimation_decoupe", "Estimation découpe laser", "calcul",
     "Longueur de coupe, temps et prix d'une découpe laser."),
    ("atool-estimation-cnc", "estimation_cnc", "Estimation fraisage CNC", "calcul",
     "Temps et prix d'un fraisage CNC."),
)

TOOL_IDS = tuple(item[0] for item in TOOLS)
PACKAGES = {item[0]: item[1] for item in TOOLS}
NAMES = {item[0]: item[2] for item in TOOLS}
COMPONENT_PREFIX = "atool-"
# Version de base du cadre Alpine Tools (capacité alpine_tools_v1). Le code d'un outil arrive avec le paquet Worker :
# un outil ajouté plus tard demande la version d'agent qui l'apporte (sinon son paquet manque sur le Worker).
MIN_AGENT_VERSION = "1.53.0"
MIN_AGENT_VERSIONS = {
    "atool-lithophanie-volume": "1.53.2", "atool-dessin-enfant": "1.53.2", "atool-puzzle-photo": "1.53.2",
    "atool-ciel-etoile": "1.53.2", "atool-chutes": "1.53.2",
}
CAPABILITY = "alpine_tools_v1"
# Python isolé de chaque outil : mêmes versions que le moteur de toile (déjà éprouvées sur Python 3.12).
PACKAGES_PIP = ("numpy==2.5.3", "Pillow==11.0.0")
LICENSE = "Code Alpine Makers ; numpy BSD-3-Clause ; Pillow MIT-CMU (aucun programme ni modèle tiers)"


def component_of(tool_id):
    """Composant à installer pour un outil (même identifiant que l'outil)."""
    return tool_id if tool_id in PACKAGES else ""


def package_of(tool_id):
    return PACKAGES.get(str(tool_id or ""), "")


def min_agent_version(tool_id=None):
    """Version d'agent minimale d'un outil (version de base du cadre pour un outil inconnu ou sans précision)."""
    return MIN_AGENT_VERSIONS.get(str(tool_id or ""), MIN_AGENT_VERSION)
