# -*- coding: utf-8 -*-
"""Moteur d'Alpine CNC Studio exécuté sur le Worker (composant « cnc-engine »).

Analyse et simulation d'un programme de fraisage (G-code) : validation par ``cam_gcode_check``
(livré avec l'agent), modes et plans, arcs, étendue, longueurs, durée estimée avec accélération
simple, outils, pauses, broche, arrosage, lignes douteuses et parcours échantillonné pour l'aperçu.
Bibliothèque standard seulement ; aucune commande n'est jamais envoyée à une machine.
"""


class CncEngineError(ValueError):
    """Erreur métier du moteur CNC (requête, fichier ou opération), avec un code lisible par le site."""

    def __init__(self, message, status_code=400, code="REQUEST_INVALID"):
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = str(code)


__all__ = ["CncEngineError"]
