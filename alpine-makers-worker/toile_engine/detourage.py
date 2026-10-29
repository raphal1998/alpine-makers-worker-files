# -*- coding: utf-8 -*-
"""Séparation premier plan / arrière-plan d'Alpine Toile Creator (module « toile-detourage »).

Modèle : BiRefNet lite (licence MIT, ZhengPeng7/BiRefNet ; conversion ONNX onnx-community), exécuté par ONNX Runtime
sur le CPU dans le Python isolé du module. Entrée 1024 × 1024 RGB, ``x / 255`` puis moyenne
``[0.485, 0.456, 0.406]`` et écart-type ``[0.229, 0.224, 0.225]``, interpolation bilinéaire ; sortie logits
``(1, 1, 1024, 1024)`` → sigmoïde → masque redimensionné à l'image. Le masque est progressif (0..255, 255 = premier
plan) : le navigateur choisit son seuil. Orientation EXIF appliquée, comme l'affiche le navigateur.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from . import ToileEngineError

TAILLE_MODELE = 1024
MOYENNE = np.array([0.485, 0.456, 0.406], np.float32)
ECART_TYPE = np.array([0.229, 0.224, 0.225], np.float32)
FORMATS = {"PNG", "JPEG", "WEBP"}
SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
MAX_OCTETS = 25 * 1024 * 1024
MAX_PIXELS = 40_000_000
COTE_MASQUE = 2048
SEUIL_PREMIER_PLAN = 128


def ouvrir_image(chemin):
    """Image du job vérifiée (format, poids, pixels) puis orientée et convertie en RGBA."""
    chemin = Path(chemin)
    if chemin.suffix.lower() not in SUFFIXES:
        raise ToileEngineError("Image : fichier .png, .jpg, .jpeg ou .webp attendu.", 400, "IMAGE_INVALIDE")
    if not chemin.is_file():
        raise ToileEngineError("Image introuvable sur le Worker.", 400, "IMAGE_ABSENTE")
    if chemin.stat().st_size > MAX_OCTETS:
        raise ToileEngineError("Image trop volumineuse (25 Mo maximum).", 413, "IMAGE_TROP_GRANDE")
    try:
        image = Image.open(chemin)
        if image.format not in FORMATS:
            raise ToileEngineError("Image : PNG, JPEG ou WebP attendu.", 400, "IMAGE_INVALIDE")
        largeur, hauteur = image.size
        if largeur < 1 or hauteur < 1 or largeur * hauteur > MAX_PIXELS:
            raise ToileEngineError("Image trop grande (40 millions de pixels maximum).", 413, "IMAGE_TROP_GRANDE")
        image = ImageOps.exif_transpose(image)
        return image.convert("RGBA")
    except ToileEngineError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise ToileEngineError(f"Image illisible : {error}", 400, "IMAGE_INVALIDE") from None


def taille_masque(largeur, hauteur):
    """Même taille que l'image jusqu'à 2048 px de côté, sinon réduite à 2048 px avec le même ratio."""
    plus_grand = max(largeur, hauteur)
    if plus_grand <= COTE_MASQUE:
        return int(largeur), int(hauteur)
    echelle = COTE_MASQUE / plus_grand
    return max(1, round(largeur * echelle)), max(1, round(hauteur * echelle))


def pretraiter(image):
    """Image RGBA → tenseur (1, 3, 1024, 1024) float32 normalisé ; la transparence est posée sur du blanc."""
    fond = Image.new("RGBA", image.size, (255, 255, 255, 255))
    rgb = Image.alpha_composite(fond, image).convert("RGB").resize((TAILLE_MODELE, TAILLE_MODELE), Image.BILINEAR)
    valeurs = np.asarray(rgb, np.float32) / 255.0
    valeurs = (valeurs - MOYENNE) / ECART_TYPE
    return np.ascontiguousarray(valeurs.transpose(2, 0, 1)[None, ...], dtype=np.float32)


def posttraiter(logits, taille, alpha=None):
    """Logits du modèle → masque PIL mode L à ``taille`` (sigmoïde, bilinéaire) ; la transparence d'origine reste vide."""
    valeurs = np.asarray(logits, np.float32)
    if valeurs.size != TAILLE_MODELE * TAILLE_MODELE:
        raise ToileEngineError(f"Sortie du modèle inattendue {tuple(valeurs.shape)} (1, 1, 1024, 1024 attendu).", 500, "MODELE_INVALIDE")
    valeurs = valeurs.reshape(TAILLE_MODELE, TAILLE_MODELE)
    probabilite = 1.0 / (1.0 + np.exp(-np.clip(valeurs, -60.0, 60.0)))
    masque = Image.fromarray(np.round(probabilite * 255.0).astype(np.uint8)).resize(taille, Image.BILINEAR)
    if alpha is not None:
        masque = Image.fromarray(np.minimum(np.asarray(masque), np.asarray(alpha.resize(taille, Image.BILINEAR))))
    return masque


def ouvrir_session(chemin_modele):
    """Session ONNX Runtime CPU ; threads alignés sur la part du processeur réservée au job (OMP_NUM_THREADS)."""
    import onnxruntime

    chemin = Path(chemin_modele)
    if not chemin.is_file():
        raise ToileEngineError("Modèle de détourage absent : installe le module dans Mes Workers → Installations.", 409, "MODULE_MISSING")
    options = onnxruntime.SessionOptions()
    try:
        threads = int(os.getenv("OMP_NUM_THREADS") or 0)
    except ValueError:
        threads = 0
    if threads > 0:
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
    # Chemin accentué (dossier « Prêt ») : le modèle est lu en mémoire plutôt que par la bibliothèque native.
    source = str(chemin) if str(chemin).isascii() else chemin.read_bytes()
    return onnxruntime.InferenceSession(source, sess_options=options, providers=["CPUExecutionProvider"])


def detourer(chemin_image, chemin_masque, session):
    """Détoure l'image et écrit ``masque.png`` (mode L) ; renvoie {largeur, hauteur, part_premier_plan}."""
    image = ouvrir_image(chemin_image)
    taille = taille_masque(*image.size)
    alpha = image.getchannel("A")
    alpha = None if alpha.getextrema() == (255, 255) else alpha
    entree = session.get_inputs()[0].name
    sorties = session.run(None, {entree: pretraiter(image)})
    if not sorties:
        raise ToileEngineError("Le modèle de détourage n'a rien renvoyé.", 500, "MODELE_INVALIDE")
    masque = posttraiter(sorties[0], taille, alpha)
    masque.save(chemin_masque, format="PNG", optimize=True)
    part = float((np.asarray(masque) >= SEUIL_PREMIER_PLAN).mean())
    return {"largeur": taille[0], "hauteur": taille[1], "part_premier_plan": round(part, 4)}
