"""Moteur d'analyse d'Alpine Guard (composant « guard-engine ») : vision classique OpenCV + modèle facultatif.

Lancé dans le Python isolé du composant par l'agent (``guard.py``), piloté par tubes (``server.py``). Aucun code ni
modèle PrintGuard. Licences : OpenCV Apache-2.0, NumPy BSD-3-Clause ; modèle facultatif Obico (AGPL-3.0, téléchargé
par chaque Worker à sa source officielle, jamais redistribué) exécuté par ONNX Runtime (MIT).
"""
