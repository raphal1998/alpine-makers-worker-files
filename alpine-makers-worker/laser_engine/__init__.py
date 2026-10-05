# -*- coding: utf-8 -*-
"""Moteur de parcours laser/CNC d'Alpine Laser Studio, embarqué dans le paquet Worker.

- ``toolpath`` : le moteur pur (bibliothèque standard ; Pillow seulement pour la trame d'image).
- ``cli`` : exécution d'une opération sur le Worker (``python -m laser_engine.cli``), lancée par
  ``runners/laser-engine.py`` avec le Python isolé du composant « laser-engine ».

Le site importe ce paquet à travers ``laser_toolpath.py`` (shim de compatibilité) pour ses aperçus ;
les calculs des jobs (analyse SVG, plan, G-code, cadrage, simulation, trame) se font sur le Worker.
Aucun import ici : le paquet reste léger pour l'agent, qui n'a pas besoin de Pillow.
"""
