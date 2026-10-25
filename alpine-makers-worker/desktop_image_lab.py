# -*- coding: utf-8 -*-
"""Alpine Labo image pour Alpine Makers Desktop (worker.exe), outil « image-lab » de ``desktop_actions``.

Même Labo que la page du dashboard (``image_lab.py`` côté site, ``docs/labo_image.md``) — quinze outils en cinq
familles, mêmes réglages et mêmes bornes —, mais exécuté **directement sur ce PC** par les moteurs déjà installés du
Worker, sans passer par la file du site :

- graphes ComfyUI fixes (agrandir, contours, profondeur, pose, détourer, décrire) → ``runners/comfyui.py`` ;
- restauration des visages (GFPGAN + YuNet) → ``runners/face_restore.py`` ;
- propriétés et palette et sept cartes (module « Outils de cartes du Labo ») → ``runners/image_maps.py``.

Ce sont les MÊMES lanceurs que pour un job du site, avec la même demande (revalidée par ``safety``), le même dossier de
travail privé, le même marqueur d'annulation et le même précontrôle des conditions d'utilisation
(``Agent._check_legal_operation("job.run", …)`` : registre juridique local, puis le site quand la règle l'exige).
Aucun modèle n'est téléchargé ici : un fichier absent est signalé, avec l'écran qui l'installe (Mes Workers →
Installations).

L'image source arrive par un dépôt de worker.exe (``runtime/desktop/inbox/<hex>/<nom>``, fichier local ou fichier de la
Bibliothèque image du compte téléchargé par l'outil « library ») et devient une copie de travail sans métadonnées
(``desktop_image_lab_normalize.py``, lancé avec le Python du moteur image). Sources, résultats et dossiers de travail :
``runtime/desktop/image-lab/{sources,results,jobs}``. L'enregistrement dans la Bibliothèque image du compte se fait par
l'outil « library » (import signé ``self/library/upload`` puis collection « Labo image »).

Différences assumées avec le site : l'interrupteur « ON/OFF Labo image » (réglage du site, inconnu de l'agent) n'est pas
lu ; les résultats ne vont pas dans la galerie du générateur ; aucun Alpine Coin n'est compté.
"""
import copy
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

if __package__:
    from . import desktop_actions as desk
    from .desktop_actions import TaskCancelled, TaskError, v_bool, v_choice, v_hex, v_int, v_object, v_text
    from .desktop_library import inbox_file, v_file_name
    from .installers.model_catalog import COMFYUI_LAB_MODULES, COMFYUI_UTILITY_MODELS
    from .job_state import atomic_json
    from .local_control import checked
    from .safety import (IMAGE_LAB_CAPTION_LENGTHS, IMAGE_LAB_CAPTION_PROMPTS, IMAGE_LAB_MAPS_MODULE, is_link,
                         validate_comfyui_job_parameters)
    from .storage_paths import WorkerStorage
else:
    import desktop_actions as desk
    from desktop_actions import TaskCancelled, TaskError, v_bool, v_choice, v_hex, v_int, v_object, v_text
    from desktop_library import inbox_file, v_file_name
    from installers.model_catalog import COMFYUI_LAB_MODULES, COMFYUI_UTILITY_MODELS
    from job_state import atomic_json
    from local_control import checked
    from safety import (IMAGE_LAB_CAPTION_LENGTHS, IMAGE_LAB_CAPTION_PROMPTS, IMAGE_LAB_MAPS_MODULE, is_link,
                        validate_comfyui_job_parameters)
    from storage_paths import WorkerStorage

TOOL = "image-lab"
ENGINE_TOOL = "image_generation"
PACKAGE = Path(__file__).resolve().parent
RUNNERS = PACKAGE / "runners"
NORMALIZE_SCRIPT = PACKAGE / "desktop_image_lab_normalize.py"
MEBIBYTE = 1024 * 1024
SOURCE_MAX_BYTES = 10 * MEBIBYTE          # comme un dépôt sur le site (image_lab.SOURCE_MAX_BYTES)
RESULT_SOURCE_MAX_BYTES = 32 * MEBIBYTE   # un résultat repris comme source (image_lab.RESULT_MAX_BYTES)
RESULT_MAX_BYTES = 32 * MEBIBYTE
RESULT_MAX_SIDE = 8192 * 4
UPSCALE_MAX_OUTPUT_PIXELS = 16_000_000
TEXT_RESULT_MAX_BYTES = 64 * 1024
TEXT_RESULT_MAX_CHARS = 4000
PROPERTIES_MAX_BYTES = 16 * 1024
FACE_REPORT_MAX_BYTES = 16 * 1024
SOURCES_KEPT = 12
RESULTS_KEPT = 60
RUNS_MAX = 3                              # comme ACTIVE_JOBS_PER_USER du site
NORMALIZE_TIMEOUT = 120
TIMEOUTS = {"graph": 1800, "face": 900, "maps": 300}
OUTER_GRACE = 300                         # au-delà du délai du lanceur : arrêt demandé, jamais un processus tué
STOP_WAIT = 120
COLLECTION = "Labo image"                 # collection de la Bibliothèque image, comme les résultats du site
ANALYSIS_KEYS = ("megapixels", "ratio", "orientation", "grayscale", "average_color", "dhash", "palette")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
NO_FACE_MESSAGE = ("Aucun visage détecté sur cette image : rien n’a été modifié. L’outil reconnaît les visages assez grands "
                   "(au moins 24 px), plutôt de face.")
MISSING_ENGINE = ("Le moteur image (ComfyUI) n’est pas installé sur ce Worker : le Labo image utilise son environnement. "
                  "Installe-le dans Mes Workers → Installations (rien n’est téléchargé depuis Desktop).")
NORMALIZE_ERRORS = {
    "SOURCE_EMPTY": "Le fichier est vide.",
    "SOURCE_TOO_LARGE": "L’image dépasse la taille admise (10 Mo pour un fichier, 32 Mo pour un résultat repris).",
    "SOURCE_FORMAT": "Format non pris en charge : fournis une image PNG, JPEG ou WEBP.",
    "SOURCE_TOO_MANY_PIXELS": "Dimensions refusées : au plus 16 mégapixels et 8192 px de côté.",
    "SOURCE_UNREADABLE": "Cette image est illisible ou endommagée.",
    "SOURCE_TOO_SMALL": "Image trop petite : au moins 16 px de côté.",
    "ALPHA_TOO_LARGE": "Image transparente trop lourde une fois décodée : réduis ses dimensions.",
    "PILLOW_MISSING": "Pillow manque dans l’environnement du moteur image : répare le moteur ComfyUI (Mes Workers → Installations).",
    "OUT_OF_MEMORY": "Mémoire insuffisante pour lire cette image.",
}

# Noms et licences affichés (mêmes que distributed_workers.IMAGE_LAB_MODEL_CATALOG ; un test compare les deux).
MODEL_NAMES = {
    "lab-realesrgan-x4plus": ("Real-ESRGAN x4plus · agrandissement ×4", "BSD-3-Clause"),
    "lab-birefnet": ("BiRefNet · suppression de l’arrière-plan", "MIT"),
    "lab-depth-anything-3-small": ("Depth Anything 3 Small · carte de profondeur", "Apache-2.0"),
    "lab-depth-anything-3-base": ("Depth Anything 3 Base · carte de profondeur", "Apache-2.0"),
    "lab-sdpose-wholebody": ("SDPose Wholebody · carte de pose", "MIT"),
    "lab-qwen35-2b": ("Qwen3.5 2B · description d’image", "Apache-2.0"),
    "lab-gfpgan-v14": ("GFPGAN v1.4 · restauration des visages (usage non commercial)", "Usage non commercial"),
    "lab-yunet-face": ("YuNet · détection des visages", "MIT / BSD-3-Clause"),
    "lab-maps-tools": ("Outils de cartes du Labo", "Code d’Alpine Makers (Pillow MIT-CMU, numpy BSD-3-Clause)"),
}

FAMILIES = (
    {"id": "describe", "label": "Description", "summary": "Ce que contient l’image : propriétés et couleurs dominantes, puis une description rédigée par un modèle de vision."},
    {"id": "control", "label": "Cartes de contrôle", "summary": "Une image de structure (contours, traits, relief, posture, couleurs) qui guide ensuite une génération ControlNet."},
    {"id": "upscale", "label": "Agrandissement", "summary": "Plus de pixels, avec des détails recréés par un modèle."},
    {"id": "face", "label": "Restauration du visage", "summary": "Redonner de la netteté aux visages d’une photo."},
    {"id": "background", "label": "Suppression de l’arrière-plan", "summary": "Garder le sujet, rendre le fond transparent, puis en tirer un masque d’inpainting."},
)

# Copie de image_lab.TOOLS (site) : familles, libellés, réglages et bornes identiques (tests.test_worker_desktop_image_lab
# compare les deux tables champ par champ). Le site garde la répartition entre Workers ; ici, ce PC calcule.
TOOLS = {'properties': {'family': 'describe',
                'label': 'Propriétés et palette',
                'summary': 'Format, dimensions, rapport largeur / hauteur, transparence, niveaux de gris, empreinte '
                           'visuelle et couleurs dominantes : « Analyser » les calcule sur le Worker et les joint à l’image '
                           'source.',
                'result': 'Propriétés et palette',
                'output': 'properties',
                'settings': [],
                'models': ('lab-maps-tools',),
                'runner': 'maps',
                'notice': 'Calculé sur le Worker par le module Outils de cartes du Labo.'},
 'caption': {'family': 'describe',
             'label': 'Décrire l’image (Qwen3.5 2B)',
             'summary': 'Une phrase, un paragraphe ou des mots-clés rédigés par un modèle de vision sur le Worker. C’est '
                        'une estimation : relis-la avant de t’en servir.',
             'result': 'Description',
             'output': 'text',
             'settings': [{'id': 'style',
                           'label': 'Forme',
                           'type': 'select',
                           'default': 'sentence',
                           'options': [{'value': 'sentence', 'label': 'Une phrase'},
                                       {'value': 'detailed', 'label': 'Un paragraphe détaillé'},
                                       {'value': 'tags', 'label': 'Mots-clés'}]},
                          {'id': 'language',
                           'label': 'Langue',
                           'type': 'select',
                           'default': 'fr',
                           'options': [{'value': 'fr', 'label': 'Français'},
                                       {'value': 'en', 'label': 'Anglais (pour un prompt)'}],
                           'help': 'Les modèles de génération comprennent souvent mieux un prompt en anglais.'}],
             'models': ('lab-qwen35-2b',)},
 'control_canny': {'family': 'control',
                   'label': 'Contours (Canny)',
                   'summary': 'Trace les arêtes de l’image en blanc sur noir. Aucun modèle à installer ; sert d’entrée à un '
                              'ControlNet « canny ».',
                   'result': 'Carte de contours',
                   'settings': [{'id': 'low_threshold',
                                 'label': 'Seuil bas',
                                 'type': 'number',
                                 'min': 0.01,
                                 'max': 0.99,
                                 'step': 0.01,
                                 'default': 0.4,
                                 'help': 'Plus il est bas, plus les arêtes faibles sont gardées.'},
                                {'id': 'high_threshold',
                                 'label': 'Seuil haut',
                                 'type': 'number',
                                 'min': 0.01,
                                 'max': 0.99,
                                 'step': 0.01,
                                 'default': 0.8,
                                 'help': 'Au-dessus, une arête est toujours gardée. Doit rester supérieur ou égal au seuil '
                                         'bas.'}],
                   'models': (),
                   'control_hint': 'canny'},
 'control_depth': {'family': 'control',
                   'label': 'Profondeur (Depth Anything 3)',
                   'summary': 'Estime le relief de la scène et le rend en niveaux de gris, pour un ControlNet « depth ».',
                   'result': 'Carte de profondeur',
                   'settings': [{'id': 'resolution',
                                 'label': 'Finesse de l’analyse',
                                 'type': 'select',
                                 'default': 756,
                                 'options': [{'value': 504, 'label': 'Rapide (504 px)'},
                                             {'value': 756, 'label': 'Équilibrée (756 px)'},
                                             {'value': 1008, 'label': 'Fine (1008 px)'}],
                                 'help': 'Côté long analysé par le modèle : plus fin = plus lent et plus de mémoire '
                                         'graphique.'}],
                   'models': ('lab-depth-anything-3-base', 'lab-depth-anything-3-small'),
                   'control_hint': 'depth'},
 'control_pose': {'family': 'control',
                  'label': 'Pose (SDPose)',
                  'summary': 'Repère le corps, les mains et le visage d’une personne et dessine son squelette, pour un '
                             'ControlNet « openpose ».',
                  'result': 'Carte de pose',
                  'settings': [{'id': 'draw_hands', 'label': 'Dessiner les mains', 'type': 'boolean', 'default': True},
                               {'id': 'draw_face', 'label': 'Dessiner le visage', 'type': 'boolean', 'default': True},
                               {'id': 'stick_width',
                                'label': 'Épaisseur du trait',
                                'type': 'number',
                                'min': 1,
                                'max': 10,
                                'step': 1,
                                'default': 4,
                                'integer': True},
                               {'id': 'score_threshold',
                                'label': 'Confiance minimale',
                                'type': 'number',
                                'min': 0,
                                'max': 1,
                                'step': 0.05,
                                'default': 0.3,
                                'help': 'Les points dont le modèle est moins sûr ne sont pas dessinés.'}],
                  'models': ('lab-sdpose-wholebody',),
                  'control_hint': 'openpose'},
 'control_normal': {'family': 'control',
                    'label': 'Normales (depuis la profondeur)',
                    'summary': 'Calcule l’orientation des surfaces à partir d’une carte de profondeur du Labo, pour un '
                               'ControlNet « normal » (convention Normal BAE : bleu = face à toi, rouge = gauche, vert = '
                               'haut). Approximation dérivée de la profondeur, pas le modèle Normal BAE. Lance d’abord « '
                               'Profondeur », puis « Normales » sur son résultat.',
                    'result': 'Carte de normales',
                    'settings': [{'id': 'relief',
                                  'label': 'Relief',
                                  'type': 'number',
                                  'min': 0.1,
                                  'max': 10,
                                  'step': 0.1,
                                  'default': 1.0,
                                  'help': 'Accentue (plus haut) ou adoucit (plus bas) les pentes tirées de la profondeur.'},
                                 {'id': 'resolution',
                                  'label': 'Finesse du calcul',
                                  'type': 'select',
                                  'default': 512,
                                  'options': [{'value': 256, 'label': 'Douce (256 px)'},
                                              {'value': 512, 'label': 'Équilibrée (512 px)'},
                                              {'value': 1024, 'label': 'Fine (1024 px)'}],
                                  'help': 'Côté long sur lequel les pentes sont mesurées, puis la carte est remise à la '
                                          'taille de l’image. Plus fin = plus de détails, et plus de marches visibles sur '
                                          'une profondeur peu nuancée.'},
                                 {'id': 'background',
                                  'label': 'Fond plat sous',
                                  'type': 'number',
                                  'min': 0,
                                  'max': 0.5,
                                  'step': 0.01,
                                  'default': 0.05,
                                  'help': 'Les zones plus lointaines que ce seuil (0 = le plus loin, 1 = le plus proche) '
                                          'restent plates, face à l’objectif.'}],
                    'models': ('lab-maps-tools',),
                    'runner': 'maps',
                    'input': 'depth',
                    'chain_from': ('control_depth',),
                    'notice': 'Calculé sur le Worker par le module Outils de cartes du Labo.',
                    'control_hint': 'normalbae'},
 'control_lineart': {'family': 'control',
                     'label': 'Trait (lineart approché)',
                     'summary': 'Extrait les traits de l’image par différence de gaussiennes, pour un ControlNet « lineart '
                                '» (blanc sur noir) ou pour un dessin à reprendre (noir sur blanc). Approximation : pas le '
                                'réseau des préprocesseurs lineart d’origine.',
                     'result': 'Carte de traits',
                     'settings': [{'id': 'detail',
                                   'label': 'Finesse du trait',
                                   'type': 'number',
                                   'min': 0.5,
                                   'max': 4,
                                   'step': 0.1,
                                   'default': 1.2,
                                   'help': 'Petit = traits fins et nombreux ; grand = seulement les contours principaux. '
                                           'Donné pour 1024 px de côté, adapté à la taille de l’image.'},
                                  {'id': 'intensity',
                                   'label': 'Intensité',
                                   'type': 'number',
                                   'min': 1,
                                   'max': 40,
                                   'step': 1,
                                   'default': 12,
                                   'integer': True,
                                   'help': 'Assombrit ou éclaircit les traits obtenus.'},
                                  {'id': 'cleanup',
                                   'label': 'Nettoyage',
                                   'type': 'number',
                                   'min': 0,
                                   'max': 0.5,
                                   'step': 0.01,
                                   'default': 0.08,
                                   'help': 'Efface les traits trop faibles (grain, bruit, textures).'},
                                  {'id': 'polarity',
                                   'label': 'Rendu',
                                   'type': 'select',
                                   'default': 'white_on_black',
                                   'options': [{'value': 'white_on_black', 'label': 'Blanc sur noir (ControlNet lineart)'},
                                               {'value': 'black_on_white', 'label': 'Noir sur blanc (dessin)'}],
                                   'help': 'Les ControlNet lineart attendent un trait blanc sur fond noir.'}],
                     'models': ('lab-maps-tools',),
                     'runner': 'maps',
                     'notice': 'Calculé sur le Worker par le module Outils de cartes du Labo.',
                     'control_hint': 'lineart'},
 'control_scribble': {'family': 'control',
                      'label': 'Gribouillage (scribble approché)',
                      'summary': 'Contours simplifiés, épaissis puis seuillés : un trait blanc sur noir pour un ControlNet '
                                 '« scribble ». Approximation par différence de gaussiennes, pas HED ni PiDiNet.',
                      'result': 'Carte de gribouillage',
                      'settings': [{'id': 'simplify',
                                    'label': 'Simplification',
                                    'type': 'number',
                                    'min': 1,
                                    'max': 8,
                                    'step': 0.5,
                                    'default': 3,
                                    'help': 'Plus haut = moins de détails, seulement les grandes formes.'},
                                   {'id': 'thickness',
                                    'label': 'Épaisseur du trait (px)',
                                    'type': 'number',
                                    'min': 1,
                                    'max': 24,
                                    'step': 1,
                                    'default': 4,
                                    'integer': True,
                                    'help': 'Donnée pour 1024 px de côté, adaptée à la taille de l’image.'},
                                   {'id': 'threshold',
                                    'label': 'Seuil',
                                    'type': 'number',
                                    'min': 0.02,
                                    'max': 0.5,
                                    'step': 0.01,
                                    'default': 0.12,
                                    'help': 'Plus haut = moins de traits gardés.'}],
                      'models': ('lab-maps-tools',),
                      'runner': 'maps',
                      'notice': 'Calculé sur le Worker par le module Outils de cartes du Labo.',
                      'control_hint': 'scribble'},
 'control_tile': {'family': 'control',
                  'label': 'Tuile / flou (tile)',
                  'summary': 'Réduit puis agrandit l’image et la floute un peu : garde couleurs et composition, oublie les '
                             'détails, pour un ControlNet « tile » (recréer des détails, agrandir). Même principe que le '
                             'prétraitement « tile » habituel.',
                  'result': 'Carte tuile',
                  'settings': [{'id': 'factor',
                                'label': 'Réduction',
                                'type': 'select',
                                'default': 4,
                                'options': [{'value': 2, 'label': '÷2 (léger)'},
                                            {'value': 4, 'label': '÷4'},
                                            {'value': 8, 'label': '÷8 (fort)'}]},
                               {'id': 'blur',
                                'label': 'Flou (px)',
                                'type': 'number',
                                'min': 0,
                                'max': 8,
                                'step': 0.5,
                                'default': 1,
                                'help': '0 = sans flou ajouté.'}],
                  'models': ('lab-maps-tools',),
                  'runner': 'maps',
                  'notice': 'Calculé sur le Worker par le module Outils de cartes du Labo.',
                  'control_hint': 'tile'},
 'control_color': {'family': 'control',
                   'label': 'Grille de couleurs (color)',
                   'summary': 'Moyenne des couleurs par blocs, en grands pixels : guide les couleurs d’une génération avec '
                              'un T2I-Adapter ou un ControlNet « color » (SD 1.5). Même principe que le prétraitement « '
                              'color » d’origine (blocs de 64 px).',
                   'result': 'Grille de couleurs',
                   'settings': [{'id': 'block',
                                 'label': 'Taille d’un bloc (px)',
                                 'type': 'number',
                                 'min': 4,
                                 'max': 256,
                                 'step': 1,
                                 'default': 64,
                                 'integer': True}],
                   'models': ('lab-maps-tools',),
                   'runner': 'maps',
                   'notice': 'Calculé sur le Worker par le module Outils de cartes du Labo.',
                   'control_hint': 'color'},
 'control_gray': {'family': 'control',
                  'label': 'Niveaux de gris',
                  'summary': 'L’image en niveaux de gris (luminance), pour un ControlNet « recolor » qui la recolorie, ou '
                             'comme base d’un autre outil.',
                  'result': 'Image en niveaux de gris',
                  'settings': [{'id': 'autocontrast', 'label': 'Étirer le contraste', 'type': 'boolean', 'default': False}],
                  'models': ('lab-maps-tools',),
                  'runner': 'maps',
                  'notice': 'Calculé sur le Worker par le module Outils de cartes du Labo.',
                  'control_hint': 'recolor'},
 'upscale': {'family': 'upscale',
             'label': 'Agrandir (Real-ESRGAN x4plus)',
             'summary': 'Multiplie la taille de l’image. Le modèle invente des détails plausibles : il ne retrouve pas ceux '
                        'qui manquent.',
             'result': 'Image agrandie',
             'settings': [{'id': 'factor',
                           'label': 'Facteur',
                           'type': 'select',
                           'default': 4,
                           'options': [{'value': 2, 'label': '×2'},
                                       {'value': 3, 'label': '×3'},
                                       {'value': 4, 'label': '×4'}],
                           'help': 'Le résultat ne peut pas dépasser 16 mégapixels.'}],
             'models': ('lab-realesrgan-x4plus',)},
 'face_restore': {'family': 'face',
                  'label': 'Restaurer les visages (GFPGAN v1.4)',
                  'summary': 'Repère les visages de la photo (détecteur YuNet), les reconstruit un par un avec GFPGAN v1.4 '
                             'sur le Worker, puis les recolle dans l’image d’origine ; le reste de l’image ne change pas.',
                  'result': 'Visages restaurés',
                  'settings': [{'id': 'strength',
                                'label': 'Force (%)',
                                'type': 'number',
                                'min': 0,
                                'max': 100,
                                'step': 5,
                                'default': 80,
                                'integer': True,
                                'help': 'Part du visage reconstruit mêlée à l’original : 100 = visage du modèle, plus bas = '
                                        'plus proche de la photo.'},
                               {'id': 'max_faces',
                                'label': 'Visages au plus',
                                'type': 'number',
                                'min': 1,
                                'max': 8,
                                'step': 1,
                                'default': 4,
                                'integer': True,
                                'help': 'Les plus grands visages passent en premier.'}],
                  'models': ('lab-gfpgan-v14', 'lab-yunet-face'),
                  'all_models': True,
                  'notice': 'Meilleur résultat sur des visages de face et nets, même petits ou compressés. C’est une '
                            'reconstruction plausible inventée par le modèle, pas la vérité de la photo : ne t’en sers pas '
                            'pour tromper sur l’identité d’une personne. GFPGAN est réservé à un usage non commercial.',
                  'badge': 'Usage non commercial'},
 'remove_background': {'family': 'background',
                       'label': 'Détourer le sujet (BiRefNet)',
                       'summary': 'Sépare le sujet principal du fond et rend un PNG transparent.',
                       'result': 'Image détourée',
                       'settings': [],
                       'models': ('lab-birefnet',)},
 'mask_alpha': {'family': 'background',
                'label': 'Masque depuis le détourage',
                'summary': 'Transforme la transparence d’une image détourée en masque noir et blanc, à déposer comme « '
                           'Masque d’inpainting » du générateur (le blanc est la zone à régénérer). Lance d’abord « '
                           'Détourer le sujet », puis « Masque » sur son résultat.',
                'result': 'Masque',
                'settings': [{'id': 'white',
                              'label': 'Zone en blanc',
                              'type': 'select',
                              'default': 'background',
                              'options': [{'value': 'background', 'label': 'L’arrière-plan (refaire le fond)'},
                                          {'value': 'subject', 'label': 'Le sujet (refaire le sujet)'}]},
                             {'id': 'edge',
                              'label': 'Bords',
                              'type': 'select',
                              'default': 'soft',
                              'options': [{'value': 'soft', 'label': 'Adoucis (suivent la transparence)'},
                                          {'value': 'hard', 'label': 'Nets (noir ou blanc)'}]},
                             {'id': 'grow',
                              'label': 'Élargir le blanc (px)',
                              'type': 'number',
                              'min': 0,
                              'max': 64,
                              'step': 1,
                              'default': 0,
                              'integer': True,
                              'help': 'Déborde un peu sur le reste pour cacher le liseré du détourage.'}],
                'models': ('lab-maps-tools',),
                'runner': 'maps',
                'input': 'alpha',
                'chain_from': ('remove_background',),
                'to_generator': False,
                'notice': 'Calculé sur le Worker par le module Outils de cartes du Labo.'}}

FACE_TOOL = "face_restore"
MAPS_TOOLS = tuple(key for key, tool in TOOLS.items() if tool.get("runner") == "maps")
GRAPH_TOOLS = tuple(key for key in TOOLS if key not in MAPS_TOOLS and key != FACE_TOOL)


# --- réglages, graphes et demandes (mêmes que image_lab.py du site) -------------------------------------------------

def clean_settings(tool_id, raw):
    """Réglages d'un outil complétés par leurs valeurs par défaut ; champ inconnu ou hors bornes = refus."""
    tool = TOOLS.get(tool_id)
    if not tool:
        raise ValueError("Outil du Labo image inconnu.")
    raw = raw if isinstance(raw, dict) else {}
    known = {item["id"]: item for item in tool["settings"]}
    if set(raw) - set(known):
        raise ValueError("Réglage inconnu pour cet outil.")
    clean = {}
    for key, spec in known.items():
        value = raw.get(key, spec["default"])
        if spec["type"] == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"« {spec['label']} » attend oui ou non.")
        elif spec["type"] == "select":
            if isinstance(value, bool) or value not in [option["value"] for option in spec["options"]]:
                raise ValueError(f"« {spec['label']} » : choix non proposé.")
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not spec["min"] <= value <= spec["max"]:
                raise ValueError(f"« {spec['label']} » doit être entre {spec['min']} et {spec['max']}.")
            if spec.get("integer"):
                if float(value) != int(value):
                    raise ValueError(f"« {spec['label']} » attend un nombre entier.")
                value = int(value)
            else:
                value = round(float(value), 4)
        clean[key] = value
    if tool_id == "control_canny" and clean["low_threshold"] > clean["high_threshold"]:
        raise ValueError("Le seuil bas doit rester inférieur ou égal au seuil haut.")
    return clean


def build_workflow(tool_id, settings, model_file=""):
    """Graphe ComfyUI fixe d'un outil (même construction que image_lab.build_workflow du site)."""
    source = {"class_type": "LoadImage", "inputs": {"image": "source.png"}}

    def save(reference):
        return {"class_type": "SaveImage", "inputs": {"filename_prefix": "alpine-worker/lab/image", "images": reference}}

    if tool_id == "upscale":
        return {
            "1": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model_file}},
            "30": source,
            "40": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["1", 0], "image": ["30", 0]}},
            "41": {"class_type": "ImageScaleBy", "inputs": {"image": ["40", 0], "upscale_method": "lanczos", "scale_by": float(settings["factor"]) / 4.0}},
            "7": save(["41", 0]),
        }
    if tool_id == "control_canny":
        return {
            "30": source,
            "40": {"class_type": "Canny", "inputs": {"image": ["30", 0], "low_threshold": float(settings["low_threshold"]),
                                                     "high_threshold": float(settings["high_threshold"])}},
            "7": save(["40", 0]),
        }
    if tool_id == "control_depth":
        return {
            "1": {"class_type": "LoadDA3Model", "inputs": {"model_name": model_file, "weight_dtype": "default"}},
            "30": source,
            "40": {"class_type": "DA3Inference", "inputs": {"da3_model": ["1", 0], "image": ["30", 0], "resolution": int(settings["resolution"]),
                                                            "resize_method": "upper_bound_resize", "mode": "mono"}},
            "41": {"class_type": "DA3Render", "inputs": {"da3_geometry": ["40", 0], "output": "depth", "output.normalization": "v2_style",
                                                         "output.apply_sky_clip": False}},
            "7": save(["41", 0]),
        }
    if tool_id == "control_pose":
        return {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model_file}},
            "30": source,
            "40": {"class_type": "SDPoseKeypointExtractor", "inputs": {"model": ["1", 0], "vae": ["1", 2], "image": ["30", 0], "batch_size": 16}},
            "41": {"class_type": "SDPoseDrawKeypoints", "inputs": {
                "keypoints": ["40", 0], "draw_body": True, "draw_hands": bool(settings["draw_hands"]), "draw_face": bool(settings["draw_face"]),
                "draw_feet": False, "stick_width": int(settings["stick_width"]), "face_point_size": 3,
                "score_threshold": float(settings["score_threshold"]), "draw_head": True}},
            "7": save(["41", 0]),
        }
    if tool_id == "remove_background":
        return {
            "1": {"class_type": "LoadBackgroundRemovalModel", "inputs": {"bg_removal_name": model_file}},
            "30": source,
            "40": {"class_type": "RemoveBackground", "inputs": {"bg_removal_model": ["1", 0], "image": ["30", 0]}},
            "41": {"class_type": "InvertMask", "inputs": {"mask": ["40", 0]}},
            "42": {"class_type": "JoinImageWithAlpha", "inputs": {"image": ["30", 0], "alpha": ["41", 0]}},
            "7": save(["42", 0]),
        }
    if tool_id == "caption":
        return {
            "1": {"class_type": "CLIPLoader", "inputs": {"clip_name": model_file, "type": "stable_diffusion", "device": "default"}},
            "30": source,
            "31": {"class_type": "ImageScaleToTotalPixels", "inputs": {"image": ["30", 0], "upscale_method": "area", "megapixels": 1.0,
                                                                        "resolution_steps": 1}},
            "40": {"class_type": "TextGenerate", "inputs": {
                "clip": ["1", 0], "prompt": IMAGE_LAB_CAPTION_PROMPTS[(settings["style"], settings["language"])], "image": ["31", 0],
                "max_length": IMAGE_LAB_CAPTION_LENGTHS[settings["style"]], "sampling_mode": "off", "thinking": False,
                "use_default_template": True}},
            "7": {"class_type": "PreviewAny", "inputs": {"source": ["40", 0]}},
        }
    raise ValueError("Outil du Labo image inconnu.")


def build_parameters(tool_id, settings, model_file=""):
    """Demande remise au lanceur, de la même forme que celle d'un job du site (avant la copie de l'image)."""
    inputs = [{"name": "source.png"}]
    if tool_id == FACE_TOOL:
        return {"lab_tool": FACE_TOOL, "face_restore": {"strength": round(int(settings["strength"]) / 100.0, 4),
                                                         "max_faces": int(settings["max_faces"])},
                "timeout_seconds": TIMEOUTS["face"], "input_files": inputs}
    if tool_id in MAPS_TOOLS:
        return {"lab_tool": tool_id, "maps": dict(settings), "timeout_seconds": TIMEOUTS["maps"], "input_files": inputs}
    return {"lab_tool": tool_id, "workflow": build_workflow(tool_id, settings, model_file), "timeout_seconds": TIMEOUTS["graph"],
            "input_files": inputs}


def runner_name(tool_id):
    return "face_restore" if tool_id == FACE_TOOL else "image_maps" if tool_id in MAPS_TOOLS else "comfyui"


def settings_summary(tool_id, settings):
    if tool_id == "upscale":
        return f"×{settings['factor']}"
    if tool_id == "control_canny":
        return f"seuils {settings['low_threshold']:g} / {settings['high_threshold']:g}".replace(".", ",")
    if tool_id == "control_depth":
        return f"analyse {settings['resolution']} px"
    if tool_id == "control_pose":
        parts = [name for name, on in (("mains", settings["draw_hands"]), ("visage", settings["draw_face"])) if on]
        return "corps" + ("".join(" + " + part for part in parts))
    if tool_id == "caption":
        forms = {"sentence": "une phrase", "detailed": "paragraphe", "tags": "mots-clés"}
        return f"{forms[settings['style']]}, {'français' if settings['language'] == 'fr' else 'anglais'}"
    if tool_id == FACE_TOOL:
        faces = int(settings["max_faces"])
        return f"force {settings['strength']} %, {faces} visage{'s' if faces > 1 else ''} au plus"
    if tool_id == "control_normal":
        return f"relief {settings['relief']:g}, {settings['resolution']} px".replace(".", ",")
    if tool_id == "control_lineart":
        return (f"finesse {settings['detail']:g}, " + ("blanc sur noir" if settings["polarity"] == "white_on_black" else "noir sur blanc")).replace(".", ",")
    if tool_id == "control_scribble":
        return f"trait {settings['thickness']} px"
    if tool_id == "control_tile":
        return f"÷{settings['factor']}" + (f", flou {settings['blur']:g} px".replace(".", ",") if settings["blur"] else "")
    if tool_id == "control_color":
        return f"blocs de {settings['block']} px"
    if tool_id == "control_gray":
        return "contraste étiré" if settings["autocontrast"] else ""
    if tool_id == "mask_alpha":
        parts = ["blanc = " + ("arrière-plan" if settings["white"] == "background" else "sujet"), "bords " + ("adoucis" if settings["edge"] == "soft" else "nets")]
        return ", ".join(parts + ([f"+{settings['grow']} px"] if settings["grow"] else []))
    return ""


def clean_text_result(data):
    """Texte du modèle : sans bloc de réflexion ni caractère de contrôle, borné (comme image_lab.clean_text_result)."""
    if not data or len(data) > TEXT_RESULT_MAX_BYTES:
        raise TaskError("La description rendue est vide ou trop longue.", "RESULT_INVALID")
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"<think>.*?(?:</think>|$)", "", text, flags=re.DOTALL)
    text = "".join(char for char in text if char in "\n\t" or (ord(char) >= 32 and ord(char) != 127 and not 0x80 <= ord(char) < 0xA0))
    text = re.sub(r"\n{3,}", "\n\n", text.replace("\t", " ")).strip()
    if not text:
        raise TaskError("Le modèle n’a rendu aucune description.", "RESULT_INVALID")
    return text[:TEXT_RESULT_MAX_CHARS].rstrip()


def clean_png(data):
    """(PNG sans blocs de texte, largeur, hauteur) : ComfyUI inscrit son graphe dans tEXt ; il ne sort pas d'ici."""
    if not data or len(data) > RESULT_MAX_BYTES or not data.startswith(PNG_SIGNATURE):
        raise TaskError("Le moteur n’a pas rendu une image PNG valable.", "RESULT_INVALID")
    parts, position, width, height, ended = [PNG_SIGNATURE], 8, 0, 0, False
    while position + 12 <= len(data):
        length = int.from_bytes(data[position:position + 4], "big")
        kind = data[position + 4:position + 8]
        end = position + 12 + length
        if end > len(data):
            raise TaskError("Image PNG tronquée.", "RESULT_INVALID")
        if kind == b"IHDR":
            width, height = int.from_bytes(data[position + 8:position + 12], "big"), int.from_bytes(data[position + 12:position + 16], "big")
        if kind not in (b"tEXt", b"zTXt", b"iTXt"):
            parts.append(data[position:end])
        position = end
        if kind == b"IEND":
            ended = True
            break
    if not ended or not 1 <= width <= RESULT_MAX_SIDE or not 1 <= height <= RESULT_MAX_SIDE:
        raise TaskError("Image PNG illisible.", "RESULT_INVALID")
    return b"".join(parts), width, height


# --- moteur image local -----------------------------------------------------------------------------------------

def engine_root(agent):
    return Path(WorkerStorage(agent.root, agent.config).component("comfyui")).resolve()


def engine_python(root):
    """Python de l'environnement du moteur image (Pillow, torch…) ; jamais celui de l'agent."""
    relative = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    for folder in (".venv", "venv"):
        candidate = root.joinpath(folder, *relative)
        if candidate.is_file():
            return candidate
    return None


def model_state(root, model_id):
    """« installed », « incomplete » ou « missing », lu sur le disque (taille du catalogue épinglé, aucun calcul d'empreinte)."""
    models = (root / "models").resolve()
    if model_id == IMAGE_LAB_MAPS_MODULE:
        subdir, filename = COMFYUI_LAB_MODULES[(ENGINE_TOOL, model_id)]["marker"]
        path = (models / subdir / filename).resolve()
        try:
            if models not in path.parents or not path.is_file() or path.stat().st_size > 16 * 1024:
                return "missing"
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "incomplete"
        return "installed" if isinstance(value, dict) and value.get("module") == model_id and value.get("verified") is True else "incomplete"
    spec = COMFYUI_UTILITY_MODELS.get((ENGINE_TOOL, model_id))
    if not spec:
        return "missing"
    subdir, filename, size = spec["files"][0][:3]
    path = (models / subdir / filename).resolve()
    if models not in path.parents or not path.is_file():
        return "missing"
    return "installed" if path.stat().st_size == size else "incomplete"


def model_file(model_id):
    spec = COMFYUI_UTILITY_MODELS.get((ENGINE_TOOL, model_id))
    return spec["files"][0][1] if spec else ""


def engine_running(agent):
    probe = getattr(agent, "_runtime_running", None)
    try:
        return bool(probe(ENGINE_TOOL)) if callable(probe) else None
    except Exception:  # noqa: BLE001 - état inconnu, pas une panne
        return None


def stopped_by_owner(agent):
    try:
        intent = agent.journal.get("runtime_intent", ENGINE_TOOL)
    except Exception:  # noqa: BLE001
        return False
    return isinstance(intent, dict) and intent.get("enabled") is False


def tool_state(tool_id, root, python, running):
    """Disponibilité d'un outil sur CE PC : {ready, code, reason, models: {id: état}}."""
    tool = TOOLS[tool_id]
    models = list(tool.get("models", ()))
    states = {model_id: model_state(root, model_id) for model_id in models} if python is not None else {m: "missing" for m in models}
    view = {"models": states}
    if python is None:
        return {**view, "ready": False, "code": "engine_missing", "reason": MISSING_ENGINE}
    installed = [model_id for model_id in models if states[model_id] == "installed"]
    missing = [model_id for model_id in models if model_id not in installed] if tool.get("all_models") else (
        models if models and not installed else [])
    if missing:
        first = missing[0]
        name = MODEL_NAMES.get(first, (first, ""))[0]
        if states[first] == "incomplete":
            return {**view, "ready": False, "code": "model_incomplete",
                    "reason": f"« {name} » est incomplet ou altéré : répare-le dans Mes Workers → Installations (catégorie Alpine Labo image)."}
        return {**view, "ready": False, "code": "model_missing",
                "reason": f"Installe « {name} » dans Mes Workers → Installations (catégorie Alpine Labo image). Desktop ne télécharge aucun modèle."}
    if tool_id in GRAPH_TOOLS and running is False:
        return {**view, "ready": False, "code": "engine_stopped",
                "reason": "Le moteur ComfyUI de ce PC est arrêté : démarre-le (Gestionnaire → moteurs IA, ou « ON/OFF Labo image » du dashboard), puis Actualiser."}
    return {**view, "ready": True, "code": "ready", "reason": ""}


# --- rangement local --------------------------------------------------------------------------------------------

def _folder(agent, *parts):
    return desk.runtime_folder(agent, "image-lab", *parts)


def _source_dir(agent, source_id):
    return checked(agent.root, f"runtime/desktop/image-lab/sources/{source_id}")


def _result_dir(agent, result_id):
    return checked(agent.root, f"runtime/desktop/image-lab/results/{result_id}")


def _read_meta(folder):
    path = folder / "meta.json"
    try:
        if is_link(folder) or is_link(path) or not path.is_file() or path.stat().st_size > 256 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("id") == folder.name else None


def _listed(agent, kind):
    folder = _folder(agent, kind)
    metas = []
    for path in folder.iterdir():
        if path.is_dir() and re.fullmatch(r"[0-9a-f]{32}", path.name):
            meta = _read_meta(path)
            if meta is not None:
                metas.append(meta)
    return sorted(metas, key=lambda item: float(item.get("created_at") or 0), reverse=True)


def _purge(agent, kind, keep):
    for meta in _listed(agent, kind)[keep:]:
        shutil.rmtree(checked(agent.root, f"runtime/desktop/image-lab/{kind}/{meta['id']}"), ignore_errors=True)


def _source_meta(agent, source_id):
    folder = _source_dir(agent, source_id)
    meta = _read_meta(folder) if folder.is_dir() else None
    return meta if meta is not None and (folder / "source.png").is_file() else None


def _public_source(agent, meta):
    folder = _source_dir(agent, meta["id"])
    return {"id": meta["id"], "created_at": meta.get("created_at"), "origin": meta.get("origin", "file"), "name": meta.get("name", ""),
            "properties": meta.get("properties", {}), "derived_tool": meta.get("derived_tool", ""), "analyzed": bool(meta.get("analyzed")),
            "path": str(folder / "source.png")}


def _public_result(agent, meta):
    folder = _result_dir(agent, meta["id"])
    view = {key: meta.get(key) for key in ("id", "created_at", "tool", "label", "settings", "settings_summary", "model", "source", "kind",
                                            "width", "height", "transparent", "text", "faces", "message", "properties", "duration_s",
                                            "library", "file_name")}
    view["path"] = str(folder / meta["file"]) if meta.get("file") else ""
    tool = TOOLS.get(meta.get("tool"), {})
    view["control_hint"] = tool.get("control_hint", "")
    view["to_generator"] = tool.get("to_generator", True)
    view["chain"] = [key for key, other in TOOLS.items() if meta.get("tool") in other.get("chain_from", ())] if meta.get("kind") == "image" else []
    source = meta.get("source") or {}
    origin = _source_meta(agent, str(source.get("id") or "")) if source.get("id") else None
    view["source_path"] = str(_source_dir(agent, origin["id"]) / "source.png") if origin else ""
    return view


# --- copie de travail (script Pillow du moteur image) -------------------------------------------------------------

def _script_environment():
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("PYTHON") and key.upper() not in {"VIRTUAL_ENV"}}
    environment.update(PYTHONIOENCODING="utf-8")
    return environment


def _normalize(agent, task, original, folder, max_bytes):
    root = engine_root(agent)
    python = engine_python(root)
    if python is None:
        raise TaskError(MISSING_ENGINE, "ENGINE_MISSING")
    request, report = folder / "normalize.json", folder / "normalize-report.json"
    report.unlink(missing_ok=True)
    atomic_json(request, {"input": str(original), "output": str(folder / "source.png"), "report": str(report), "max_bytes": int(max_bytes)})
    with (folder / "normalize.stderr").open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen([str(python), "-I", "-B", str(NORMALIZE_SCRIPT), str(request)], cwd=str(folder),
                                   env=_script_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + NORMALIZE_TIMEOUT
        while process.poll() is None:
            if task.cancelled() or time.monotonic() > deadline:
                process.kill()          # lecture d'image de ce seul processus : rien d'autre à préserver
                process.wait(30)
                if task.cancelled():
                    raise TaskCancelled()
                raise TaskError("La lecture de l’image a dépassé le délai.", "SOURCE_TIMEOUT")
            time.sleep(0.05)
    for name in ("normalize.json", "normalize.stderr"):
        (folder / name).unlink(missing_ok=True)
    try:
        value = json.loads(report.read_text(encoding="utf-8")) if report.is_file() and report.stat().st_size <= 64 * 1024 else {}
    except (OSError, ValueError):
        value = {}
    finally:
        report.unlink(missing_ok=True)
    if value.get("ok") is not True or not isinstance(value.get("properties"), dict) or not (folder / "source.png").is_file():
        code = str(value.get("code") or "SOURCE_UNREADABLE")
        raise TaskError(NORMALIZE_ERRORS.get(code, "Cette image n’a pas pu être lue."), code)
    return value["properties"]


def _add_source(agent, task, original_path, name, origin, max_bytes, derived=None):
    source_id = secrets.token_hex(16)
    folder = _folder(agent, "sources", source_id)
    try:
        original = folder / "original.bin"
        shutil.copyfile(original_path, original)
        task.update(30, "Copie de travail sans métadonnées…")
        properties = _normalize(agent, task, original, folder, max_bytes)
        original.unlink(missing_ok=True)
        meta = {"id": source_id, "created_at": time.time(), "origin": origin, "name": str(name)[:200], "properties": properties}
        if derived:
            meta.update(derived)
        atomic_json(folder / "meta.json", meta)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    _purge(agent, "sources", SOURCES_KEPT)
    return {"source": _public_source(agent, meta)}


# --- actions ----------------------------------------------------------------------------------------------------

def _catalog(agent, params):
    root = engine_root(agent)
    python = engine_python(root)
    running = engine_running(agent)
    tools = []
    for tool_id, tool in TOOLS.items():
        state = tool_state(tool_id, root, python, running)
        tools.append({
            "id": tool_id, "family": tool["family"], "label": tool["label"], "summary": tool["summary"], "settings": tool["settings"],
            "badge": tool.get("badge", ""), "notice": tool.get("notice", ""), "result_label": tool.get("result", ""),
            "output": tool.get("output", "image"), "runner": runner_name(tool_id), "control_hint": tool.get("control_hint", ""),
            "input": tool.get("input", ""), "chain_from": list(tool.get("chain_from", ())), "to_generator": tool.get("to_generator", True),
            "all_models": bool(tool.get("all_models")),
            "models": [{"id": model_id, "name": MODEL_NAMES.get(model_id, (model_id, ""))[0], "license": MODEL_NAMES.get(model_id, ("", ""))[1],
                        "state": state["models"].get(model_id, "missing")} for model_id in tool.get("models", ())],
            "availability": {"ready": state["ready"], "code": state["code"], "reason": state["reason"]},
        })
    return {"families": list(FAMILIES), "tools": tools,
            "engine": {"installed": python is not None, "running": running, "stopped_by_owner": stopped_by_owner(agent)},
            "limits": {"source_max_bytes": SOURCE_MAX_BYTES, "source_max_pixels": 16_000_000, "source_max_side": 8192,
                       "upscale_max_output_pixels": UPSCALE_MAX_OUTPUT_PIXELS, "runs": RUNS_MAX, "formats": ["JPEG", "PNG", "WEBP"]},
            "folder": str(_folder(agent)), "collection": COLLECTION, "running_runs": _running_runs(agent)}


def _sources(agent, params):
    return {"items": [_public_source(agent, meta) for meta in _listed(agent, "sources")
                      if (_source_dir(agent, meta["id"]) / "source.png").is_file()]}


def _source_add(agent, params):
    path = inbox_file(agent, params["staged"], params["name"])   # vérifié tout de suite : un refus n'ouvre pas de tâche
    if path.stat().st_size > SOURCE_MAX_BYTES:
        shutil.rmtree(path.parent, ignore_errors=True)
        raise ValueError("L’image dépasse 10 Mo.")
    origin = params.get("origin", "file")

    def work(task):
        try:
            return _add_source(agent, task, path, params["name"], origin, SOURCE_MAX_BYTES)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)       # dépôt à usage unique
    return desk.start_task(agent, TOOL, "Image source : " + params["name"], work)


def _source_delete(agent, params):
    if _source_meta(agent, params["source"]) is None:
        raise ValueError("Image source introuvable.")
    shutil.rmtree(_source_dir(agent, params["source"]), ignore_errors=True)
    return {"removed": True}


def _results(agent, params):
    metas = _listed(agent, "results")
    per_page = params.get("per_page", 12)
    pages = max(1, (len(metas) + per_page - 1) // per_page)
    page = min(params.get("page", 1), pages)
    items = [_public_result(agent, meta) for meta in metas[(page - 1) * per_page:page * per_page]]
    return desk.result_file(agent, {"items": items, "page": page, "pages": pages, "total": len(metas)}) \
        if len(json.dumps(items, ensure_ascii=False)) > 200_000 else {"items": items, "page": page, "pages": pages, "total": len(metas)}


def _result_meta(agent, result_id):
    folder = _result_dir(agent, result_id)
    return _read_meta(folder) if folder.is_dir() else None


def _result_delete(agent, params):
    if params.get("confirm") is not True:
        raise ValueError("Suppression non confirmée : confirme-la dans l'interface.")
    if _result_meta(agent, params["result"]) is None:
        raise ValueError("Résultat introuvable.")
    shutil.rmtree(_result_dir(agent, params["result"]), ignore_errors=True)
    return {"removed": True}


def _result_saved(agent, params):
    """Note le nom donné par la Bibliothèque du compte après l'import (fait par l'outil « library »)."""
    meta = _result_meta(agent, params["result"])
    if meta is None:
        raise ValueError("Résultat introuvable.")
    meta["library"] = {"name": params["name"], "saved_at": time.time(), "collection": params.get("collection", "")}
    atomic_json(_result_dir(agent, params["result"]) / "meta.json", meta)
    return {"result": _public_result(agent, meta)}


def _result_to_source(agent, params):
    meta = _result_meta(agent, params["result"])
    if meta is None or meta.get("kind") != "image" or not meta.get("file"):
        raise ValueError("Ce résultat n’a pas d’image à réutiliser.")
    path = _result_dir(agent, meta["id"]) / meta["file"]
    if not path.is_file():
        raise ValueError("L’image de ce résultat n’existe plus.")
    derived = {"derived_tool": meta.get("tool", ""), "derived_result": meta["id"]}
    return desk.start_task(agent, TOOL, "Résultat repris comme source",
                           lambda task: _add_source(agent, task, path, meta.get("label") or "résultat", "result", RESULT_SOURCE_MAX_BYTES, derived))


def _running_runs(agent):
    tasks = desk._tasks(agent)
    with desk._GUARD:
        return sum(1 for task in tasks.values() if task.tool == TOOL and task.state == "running" and task.label.startswith("Labo · "))


def _choose_model(tool_id, states, wanted):
    tool = TOOLS[tool_id]
    models = list(tool.get("models", ()))
    if not models:
        if wanted:
            raise ValueError("Cet outil n’utilise aucun modèle.")
        return ""
    if wanted and wanted not in models:
        raise ValueError("Ce modèle ne sert pas à cet outil.")
    if tool.get("all_models"):
        return models[0]
    for candidate in ([wanted] if wanted else models):
        if states.get(candidate) == "installed":
            return candidate
    name = MODEL_NAMES.get(wanted or models[0], (wanted or models[0], ""))[0]
    raise ValueError(f"« {name} » n’est pas installé sur ce PC : installe-le dans Mes Workers → Installations.")


def _run(agent, params):
    tool_id = params["tool"]
    tool = TOOLS[tool_id]
    settings = clean_settings(tool_id, params.get("settings", {}))
    meta = _source_meta(agent, params["source"])
    if meta is None:
        raise ValueError("Image source introuvable : choisis-la de nouveau.")
    properties = meta.get("properties") or {}
    width, height = int(properties.get("width") or 0), int(properties.get("height") or 0)
    if tool.get("input") == "alpha" and not properties.get("has_alpha"):
        raise ValueError("Cette image n’a pas de transparence : lance d’abord « Détourer le sujet », puis « Masque » sur son résultat.")
    if tool.get("input") == "depth" and meta.get("derived_tool") != "control_depth" and properties.get("grayscale") is False:
        raise ValueError("Cette image n’est pas une carte de profondeur : lance d’abord « Profondeur », puis « Normales » sur son résultat.")
    if tool_id == "upscale" and width * height * settings["factor"] ** 2 > UPSCALE_MAX_OUTPUT_PIXELS:
        largest = max((factor for factor in (2, 3) if width * height * factor ** 2 <= UPSCALE_MAX_OUTPUT_PIXELS and factor < settings["factor"]), default=0)
        advice = f" Essaie ×{largest}." if largest else " Réduis d’abord l’image."
        raise ValueError(f"Le résultat dépasserait 16 mégapixels ({width * settings['factor']}×{height * settings['factor']} px)." + advice)
    root = engine_root(agent)
    state = tool_state(tool_id, root, engine_python(root), engine_running(agent))
    if not state["ready"]:
        raise ValueError(state["reason"])
    model_id = _choose_model(tool_id, state["models"], params.get("model", ""))
    if getattr(agent, "maintenance_count", 0) or getattr(agent, "maintenance_recovery", None):
        raise ValueError("Une installation ou maintenance est en cours sur ce Worker : attends sa fin.")
    if stopped_by_owner(agent):
        raise ValueError("Le moteur image a été arrêté explicitement sur ce Worker : démarre-le avant de lancer un calcul.")
    if _running_runs(agent) >= RUNS_MAX:
        raise ValueError(f"{RUNS_MAX} calculs du Labo sont déjà en cours : attends la fin de l’un d’eux.")
    label = "Labo · " + (tool.get("result") or tool["label"])
    return desk.start_task(agent, TOOL, label, lambda task: _run_work(agent, task, tool_id, settings, meta, model_id),
                           journal=True)


def _runner_state(workdir):
    try:
        value = json.loads((workdir / ".runner-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


PHASES = {"preparing": "Préparation du calcul", "submitting": "Envoi au moteur", "running": "Calcul en cours sur ce PC",
          "cancel_requested": "Arrêt demandé au moteur", "uncertain": "État du moteur incertain"}


def _launch(command, environment, workdir, log):  # remplacé par les tests (faux lanceur)
    return subprocess.Popen(command, cwd=str(workdir), env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def _environment(agent):
    storage = WorkerStorage(agent.root, agent.config)
    environment = storage.environment()
    environment["ALPINE_WORKER_ROOT"] = str(agent.root)
    environment["ALPINE_WORKER_CONTROL_TOKEN_FILE"] = str(Path(agent.root) / "config" / "engine-control-token")
    environment["COMFYUI_ROOT"] = str(storage.component("comfyui"))
    return environment


def _run_work(agent, task, tool_id, settings, meta, model_id):
    tool = TOOLS[tool_id]
    started = time.monotonic()
    run_id = secrets.token_hex(16)
    workdir = _folder(agent, "jobs", run_id)
    keep_workdir = False
    try:
        source = _source_dir(agent, meta["id"]) / "source.png"
        shutil.copyfile(source, workdir / "source.png")
        parameters = build_parameters(tool_id, settings, model_file(model_id))
        payload = {"tool_id": ENGINE_TOOL, "model_id": model_id, "parameters": copy.deepcopy(parameters)}
        task.update(3, "Vérification des conditions d’utilisation…")
        try:
            agent._check_legal_operation("job.run", payload)   # même précontrôle qu'un job du site (registre, puis site)
        except Exception as error:  # noqa: BLE001 - AgentError : message déjà rédigé pour l'utilisateur
            raise TaskError(str(error)[:500] or "Conditions d’utilisation non vérifiées : aucun calcul lancé.", "LEGAL_REFUSED") from None
        if task.cancelled():
            raise TaskCancelled()
        parameters["input_files"][0]["path"] = str(workdir / "source.png")
        try:
            validate_comfyui_job_parameters(copy.deepcopy(parameters))   # revalidation complète, comme le lanceur
        except ValueError as error:
            raise TaskError(str(error), "REQUEST_INVALID") from None
        job = {"job_id": "desktop-" + run_id, "execution_id": run_id, "tool_id": ENGINE_TOOL, "model_id": model_id, "parameters": parameters}
        atomic_json(workdir / "job.json", job)
        runner = RUNNERS / (runner_name(tool_id) + ".py")
        if not runner.is_file():
            raise TaskError("Lanceur du Labo absent de cet agent : répare le Worker.", "RUNNER_MISSING")
        task.update(5, "Lancement du calcul…")
        keep_workdir = True               # tant que l'arrêt du moteur n'est pas confirmé, le dossier reste
        with (workdir / "runner.log").open("a", encoding="utf-8") as log:
            process = _launch([sys.executable, str(runner), str(workdir / "job.json"), str(workdir)], _environment(agent), workdir, log)
            deadline = time.monotonic() + int(parameters.get("timeout_seconds") or 1800) + OUTER_GRACE
            cancel_written = stop_deadline = None
            while process.poll() is None:
                state = _runner_state(workdir)
                progress = state.get("progress")
                task.update(progress if isinstance(progress, int) and 5 <= progress <= 99 else None,
                            str(state.get("message") or PHASES.get(state.get("state"), "Calcul en cours sur ce PC")))
                overdue = time.monotonic() > deadline
                if (task.cancelled() or overdue) and not cancel_written:
                    atomic_json(workdir / ".cancel.json", {"execution_id": run_id})
                    cancel_written = True
                    stop_deadline = time.monotonic() + STOP_WAIT
                    task.update(None, "Arrêt demandé au moteur…")
                if stop_deadline and time.monotonic() > stop_deadline:
                    raise TaskError("Le calcul ne confirme pas son arrêt : il est laissé au moteur, aucun autre calcul n’est relancé.",
                                    "STOP_UNCONFIRMED")
                time.sleep(0.25)
        state = _runner_state(workdir)
        if state.get("state") not in ("completed", "failed", "cancelled") or state.get("engine_stop_confirmed") is not True:
            raise TaskError("Statut du moteur incertain ; aucune nouvelle soumission. " + str(state.get("error") or "")[:300], "UNCERTAIN")
        keep_workdir = False
        if state["state"] == "cancelled" or task.cancelled():
            raise TaskCancelled()
        if state["state"] == "failed":
            raise TaskError(str(state.get("error") or "Le calcul a échoué.")[:500], "RUN_FAILED")
        artifacts = [str(name) for name in (state.get("result") or {}).get("artifacts", []) if isinstance(name, str)]
        result = _collect(agent, tool_id, settings, meta, model_id, workdir, artifacts)
        result["duration_s"] = round(time.monotonic() - started, 1)
        atomic_json(_result_dir(agent, result["id"]) / "meta.json", result)
        _purge(agent, "results", RESULTS_KEPT)
        return {"result": _public_result(agent, result)}
    finally:
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def _artifact(workdir, name, limit):
    path = (workdir / name).resolve()
    if path.parent != workdir.resolve() or not path.is_file() or is_link(path):
        raise TaskError("Le lanceur a déclaré un fichier invalide.", "RESULT_INVALID")
    if path.stat().st_size > limit:
        raise TaskError("Le résultat rendu est trop volumineux.", "RESULT_INVALID")
    return path.read_bytes()


def _collect(agent, tool_id, settings, meta, model_id, workdir, artifacts):
    """Résultat du lanceur → résultat du Labo (mêmes contrôles que l'import du site), rangé dans results/<id>."""
    tool = TOOLS[tool_id]
    source = meta.get("properties") or {}
    result_id = secrets.token_hex(16)
    record = {"id": result_id, "created_at": time.time(), "tool": tool_id, "label": tool.get("result") or tool["label"],
              "settings": settings, "settings_summary": settings_summary(tool_id, settings),
              "model": {"id": model_id, "name": MODEL_NAMES.get(model_id, (model_id, ""))[0]} if model_id else None,
              "source": {"id": meta["id"], "width": source.get("width"), "height": source.get("height"), "name": meta.get("name", "")},
              "kind": "image", "file": "", "library": None}
    output = tool.get("output", "image")
    if output == "text":
        if artifacts != ["description.txt"]:
            raise TaskError("Le moteur n’a pas rendu la description attendue.", "RESULT_INVALID")
        record.update(kind="text", text=clean_text_result(_artifact(workdir, "description.txt", TEXT_RESULT_MAX_BYTES)))
    elif output == "properties":
        if artifacts != ["properties.json"]:
            raise TaskError("Le module n’a pas rendu les propriétés attendues.", "RESULT_INVALID")
        try:
            analysis = json.loads(_artifact(workdir, "properties.json", PROPERTIES_MAX_BYTES).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            analysis = None
        if not isinstance(analysis, dict) or analysis.get("width") != source.get("width") or analysis.get("height") != source.get("height"):
            raise TaskError("Les propriétés rendues sont illisibles.", "RESULT_INVALID")
        record.update(kind="properties", properties={key: analysis.get(key) for key in ("width", "height", "has_alpha") + ANALYSIS_KEYS})
        current = _source_meta(agent, meta["id"])
        if current is not None and bool(current["properties"].get("has_alpha")) == bool(analysis.get("has_alpha")):
            current["properties"].update({key: analysis[key] for key in ANALYSIS_KEYS if key in analysis})
            current["analyzed"] = True
            atomic_json(_source_dir(agent, meta["id"]) / "meta.json", current)
    else:
        faces = None
        if tool_id == FACE_TOOL:
            if "face_restore.json" not in artifacts:
                raise TaskError("Le lanceur n’a pas rendu le compte rendu de la restauration.", "RESULT_INVALID")
            try:
                report = json.loads(_artifact(workdir, "face_restore.json", FACE_REPORT_MAX_BYTES).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                report = None
            faces = report.get("faces_restored") if isinstance(report, dict) else None
            if type(faces) is not int or not 0 <= faces <= 8:
                raise TaskError("Compte rendu de la restauration illisible.", "RESULT_INVALID")
            if faces == 0:
                if any(name.lower().endswith(".png") for name in artifacts):
                    raise TaskError("Une image a été rendue alors qu’aucun visage n’a été restauré.", "RESULT_INVALID")
                record.update(kind="no_face", faces=0, message=NO_FACE_MESSAGE)
        if record["kind"] == "image":
            images = [name for name in artifacts if name.lower().endswith(".png")]
            if len(images) != 1:
                raise TaskError("Le moteur n’a pas rendu l’image attendue.", "RESULT_INVALID")
            data, width, height = clean_png(_artifact(workdir, images[0], RESULT_MAX_BYTES))
            if (faces is not None or tool_id in MAPS_TOOLS) and (width, height) != (source.get("width"), source.get("height")):
                raise TaskError("L’image rendue n’a pas les dimensions de l’image source.", "RESULT_INVALID")
            stamp = time.strftime("%Y%m%d-%H%M%S")
            record.update(file="result.png", width=width, height=height, transparent=tool_id == "remove_background",
                          file_name=f"labo-image_{tool_id}_{stamp}_{result_id[:6]}.png")
            if faces is not None:
                record["faces"] = faces
            folder = _folder(agent, "results", result_id)
            temporary = folder / "result.png.part"
            temporary.write_bytes(data)
            os.replace(temporary, folder / "result.png")
            return record
    _folder(agent, "results", result_id)
    return record


SOURCE_ORIGINS = ("file", "library")
DESKTOP_ACTIONS = {
    "catalog": {"handler": _catalog, "read": True, "params": {}},
    "sources": {"handler": _sources, "read": True, "params": {}},
    "source-add": {"handler": _source_add, "read": False,
                   "params": {"staged": v_hex(32), "name": v_file_name, "origin": v_choice(*SOURCE_ORIGINS)}},
    "source-delete": {"handler": _source_delete, "read": False, "params": {"source": v_hex(32)}},
    "run": {"handler": _run, "read": False, "params": {
        "source": v_hex(32), "tool": v_choice(*TOOLS), "settings": v_object(depth=2, keys=12), "model": v_text(60)}},
    "results": {"handler": _results, "read": True, "params": {"page": v_int(1, 1000), "per_page": v_int(1, 48)}},
    "result-delete": {"handler": _result_delete, "read": False, "params": {"result": v_hex(32), "confirm": v_bool}},
    "result-saved": {"handler": _result_saved, "read": False, "params": {"result": v_hex(32), "name": v_file_name, "collection": v_text(200)}},
    "result-to-source": {"handler": _result_to_source, "read": False, "params": {"result": v_hex(32)}},
}
REQUIRED = {"catalog": (), "sources": (), "source-add": ("staged", "name"), "source-delete": ("source",), "run": ("source", "tool"),
            "results": (), "result-delete": ("result", "confirm"), "result-saved": ("result", "name"), "result-to-source": ("result",)}


def _required(name, handler):
    def run(agent, params):
        missing = [field for field in REQUIRED[name] if field not in params]
        if missing:
            raise ValueError("Paramètre manquant : " + missing[0])
        return handler(agent, dict(params))
    return run


for _name, _spec in DESKTOP_ACTIONS.items():
    _spec["handler"] = _required(_name, _spec["handler"])
del _name, _spec
