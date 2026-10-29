# -*- coding: utf-8 -*-
"""Moteur d'Alpine Toile Creator exécuté sur le Worker (composant « toile-engine », agent 1.51.0).

Transforme la carte des couleurs préparée par le navigateur (PNG 8 bits : index de couleur 0..N-1, 255 = vide) en
toile multicolore imprimable : contours au pixel près, triangulation par balayage (numpy, ``mesh``), une pièce fermée
par couleur et la plaque de base, écrites dans un 3MF multi-pièces (``troismf``). Le module facultatif
« toile-detourage » sépare premier plan et arrière-plan (BiRefNet lite, ONNX Runtime CPU, ``detourage``).
Aucune bibliothèque n'est importée ici : le détourage tourne dans son propre Python isolé.
"""


class ToileEngineError(ValueError):
    """Erreur métier du moteur de toile (requête, carte ou opération), avec un code lisible par le site."""

    def __init__(self, message, status_code=400, code="REQUEST_INVALID"):
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = str(code)


__all__ = ["ToileEngineError"]
