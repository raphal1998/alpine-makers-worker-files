"""Moteur local de surveillance IA des graveuses (flammes, fumée), embarqué dans le paquet Worker.

- ``catalog`` : modèles épinglés, paquets et fichiers du composant « fire-watch » (bibliothèque standard seule,
  importé par l'agent, l'installateur et le site).
- ``engine`` : prétraitement, inférence ONNX Runtime (GPU CUDA avec contrôle de cohérence, sinon CPU) et
  post-traitement (fonctions pures + ``Detector``).
- ``server`` : processus d'analyse lancé par le service ``fire_watch.py`` avec le Python isolé du composant
  (``python -u -m fire_watch_engine.server``), protocole ligne JSON + octets JPEG sur stdin/stdout.
- ``evaluate`` : comparaison de modèles sur une vidéo enregistrée ; ``bench`` : mesures de performances.
- ``export_onnx`` : conversion locale .pt → ONNX, dans l'environnement d'export jetable seulement.

Aucun import ici : l'agent n'a ni numpy, ni OpenCV, ni ONNX Runtime.
"""
