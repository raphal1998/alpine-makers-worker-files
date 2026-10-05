# Surveillance IA des graveuses — provenance et licences

Le moteur local « fire-watch » (`fire_watch_engine/`) détecte des flammes et de la fumée sur les images de la
caméra qui filme une graveuse laser. Ce document recense ce qui est téléchargé, converti et exécuté sur le
Worker, et sous quelles licences.

## Avertissement

- La surveillance IA est une protection **supplémentaire**. Elle ne permet jamais de laisser une graveuse
  sans surveillance humaine et ne remplace ni l'opérateur, ni l'extincteur, ni les protections de la machine.
- Les modèles ci-dessous ont été entraînés sur des images d'incendies génériques, **pas sur des graveuses
  laser** (fumée normale de gravure, reflets, point laser). Leurs scores ne sont pas des garanties : un feu
  peut ne pas être détecté, une gravure normale peut être prise pour une flamme.
- Aucun réglage n'est validé par défaut. Chaque fiche laser se règle et se vérifie sur la machine, caméra en
  place (vidéos enregistrées, simulation, test d'arrêt réel avec opérateur présent).

## Modèles (vérifiés le 2026-10-01)

| Identifiant | Source épinglée | SHA-256 du checkpoint | Classes (ordre du checkpoint) | Licence des poids | Données d'entraînement |
|---|---|---|---|---|---|
| `rabahdev-yolov8n` (défaut) | [huggingface.co/rabahdev/fire-smoke-yolov8n](https://huggingface.co/rabahdev/fire-smoke-yolov8n), révision `13017fe8af477c25f5298d168e2dfede4b000753`, fichier `best.pt` (6 229 802 octets) | `b91633799ceb052c814b4f8b77a37efc9a40f002d528df97d74463585fa4f28f` | 0 smoke, 1 fire | AGPL-3.0 (fiche du modèle) | D-Fire ([gaiasd/DFireDataset](https://github.com/gaiasd/DFireDataset)), CC0 déclarée |
| `luminous0219-yolov8n` | [github.com/luminous0219/fire-and-smoke-detection-yolov8](https://github.com/luminous0219/fire-and-smoke-detection-yolov8), commit `c30923f568d915a53fcf85989be9fe5d4e030518`, fichier `weights/best.pt` (blob `d04713341a05cd40310468bd46647eddbecfb514`, 6 262 051 octets) | `ac0a10257b2bc1f20c9d957f8adeeb61dd6140322fc19d0b4a116cb491776d16` | 0 fire, 1 smoke | AGPL-3.0 (fichier LICENSE du dépôt) | Roboflow « fire-and-smoke » ([fire-rqbio/fire-and-smoke-yikzn](https://universe.roboflow.com/fire-rqbio/fire-and-smoke-yikzn)), CC BY 4.0 (non vérifiée directement) |

Les deux checkpoints sont des modèles YOLOv8n entraînés avec Ultralytics. L'ordre des classes diffère : le
moteur associe toujours les classes par leur **nom** lu dans le modèle (`fire`, `smoke`) et refuse un modèle
dont les noms ne sont pas exactement ceux-là.

## Distribution et conversion

- Les poids ne sont **jamais redistribués** par le site Alpine Makers. Chaque Worker les télécharge lui-même à
  la source épinglée ci-dessus (révision figée), vérifie la taille et l'empreinte SHA-256 **avant** tout
  chargement (un fichier `.pt` est un pickle Python, capable d'exécuter du code), puis les convertit en ONNX.
- La conversion en ONNX (`export_onnx.py` : opset 17, entrée 640, simplification, sans NMS) est une
  **modification** des poids au sens de l'AGPL-3.0. Elle est consignée dans `provenance.json`, à côté du
  modèle converti (source, révision, empreintes, outils et date de conversion) ; le texte de la licence est
  copié à côté (`LICENSE-AGPL-3.0.txt`). Les empreintes de l'ONNX dépendent de la date d'export (métadonnée
  `date`) : elles sont consignées, pas épinglées.
- L'environnement d'export (PyTorch CPU, Ultralytics — AGPL-3.0 — et leurs dépendances, liste figée dans
  `requirements-export.txt`) est jetable : il est supprimé après la conversion. L'analyse en service n'utilise
  que ONNX Runtime, NumPy et OpenCV.
- Texte officiel de l'AGPL-3.0 : [`licenses/AGPL-3.0.txt`](licenses/AGPL-3.0.txt)
  (copie de <https://www.gnu.org/licenses/agpl-3.0.txt>).

## Composants logiciels du runtime d'analyse

| Composant | Version épinglée | Licence |
|---|---|---|
| ONNX Runtime (`onnxruntime` / `onnxruntime-gpu`) | 1.30.0 | MIT |
| NVIDIA CUDA 13 (cuBLAS, cuFFT, cuRAND, NVRTC, nvJitLink, runtime) et cuDNN 9 — runtime GPU seulement | voir `requirements-runtime-gpu.txt` | Licences NVIDIA (CUDA Toolkit EULA, cuDNN SLA), redistribuables via PyPI |
| NumPy | 2.5.3 | BSD-3-Clause |
| OpenCV (`opencv-python-headless`) | 4.14.0.94 | Apache-2.0 (FFmpeg embarqué : LGPL) |
| FlatBuffers, packaging, protobuf | voir `requirements-runtime-*.txt` | Apache-2.0 ; Apache-2.0 / BSD-2-Clause ; BSD-3-Clause |

## Code du moteur

Le code de `fire_watch_engine/` (prétraitement, post-traitement, protocole, évaluation) est écrit pour
Alpine Makers et n'inclut pas de code Ultralytics ; le letterbox et la NMS reproduisent le comportement
documenté d'Ultralytics pour que les résultats de l'ONNX correspondent à ceux du modèle d'origine.
