"""Catalogue du moteur local flamme/fumée : modèles épinglés, paquets des environnements, fichiers du composant.

Importable SANS numpy ni ONNX Runtime (bibliothèque standard seulement) : l'installateur de composants, l'agent
et le site le lisent pour télécharger, vérifier et afficher les modèles.

Les poids ne sont jamais redistribués par le site : chaque Worker les télécharge à la source épinglée (révision
figée, empreinte SHA-256 vérifiée avant tout chargement, car un .pt est un pickle) puis les convertit lui-même en
ONNX. Les deux modèles sont sous AGPL-3.0 ; la conversion est une modification consignée dans provenance.json.
Ils ont été entraînés sur des images d'incendies génériques, pas sur des graveuses laser : leurs scores ne sont
pas des garanties et la surveillance reste une protection supplémentaire, jamais un remplacement de l'opérateur.
"""
import datetime
from pathlib import PurePath

COMPONENT_ID = "fire-watch"
TOOL_ID = "laser-studio"
DEFAULT_MODEL = "rabahdev-yolov8n"
CLASSES = ("fire", "smoke")
AGPL_URL = "https://www.gnu.org/licenses/agpl-3.0.txt"

# Disposition du composant sur le Worker : <racine>/components/fire-watch/…
COMPONENT_DIR = PurePath("components") / COMPONENT_ID
MODELS_DIR = "models"
SOURCE_FILENAME = "source.pt"
MODEL_FILENAME = "model.onnx"
PROVENANCE_FILENAME = "provenance.json"
LICENSE_FILENAME = "LICENSE-AGPL-3.0.txt"
READY_MARKER = ".raphal-ready"
ENGINE_MODULE = "fire_watch_engine.server"
EVALUATE_MODULE = "fire_watch_engine.evaluate"
EXPORT_MODULE = "fire_watch_engine.export_onnx"

# Modèles vérifiés le 2026-10-01 (révision, taille et empreinte du checkpoint). L'ordre des classes diffère d'un
# checkpoint à l'autre : le moteur associe TOUJOURS les classes par leur nom lu dans le modèle, jamais par indice.
MODELS = (
    {
        "id": "rabahdev-yolov8n",
        "label": "YOLOv8n flamme/fumée (rabahdev, D-Fire)",
        "url": "https://huggingface.co/rabahdev/fire-smoke-yolov8n/resolve/"
               "13017fe8af477c25f5298d168e2dfede4b000753/best.pt",
        "sha256": "b91633799ceb052c814b4f8b77a37efc9a40f002d528df97d74463585fa4f28f",
        "size": 6229802,
        "license": "AGPL-3.0",
        "license_url": "https://huggingface.co/rabahdev/fire-smoke-yolov8n/blob/"
                       "13017fe8af477c25f5298d168e2dfede4b000753/README.md",
        "license_note": "Licence déclarée par la fiche du modèle (Hugging Face).",
        "repository": "https://huggingface.co/rabahdev/fire-smoke-yolov8n",
        "revision": "13017fe8af477c25f5298d168e2dfede4b000753",
        "dataset": "D-Fire (https://github.com/gaiasd/DFireDataset)",
        "dataset_license": "CC0 1.0 (déclarée)",
        "expected_names": {0: "smoke", 1: "fire"},
    },
    {
        "id": "luminous0219-yolov8n",
        "label": "YOLOv8n flamme/fumée (luminous0219, Roboflow)",
        "url": "https://raw.githubusercontent.com/luminous0219/fire-and-smoke-detection-yolov8/"
               "c30923f568d915a53fcf85989be9fe5d4e030518/weights/best.pt",
        "sha256": "ac0a10257b2bc1f20c9d957f8adeeb61dd6140322fc19d0b4a116cb491776d16",
        "size": 6262051,
        "license": "AGPL-3.0",
        "license_url": "https://github.com/luminous0219/fire-and-smoke-detection-yolov8/blob/"
                       "c30923f568d915a53fcf85989be9fe5d4e030518/LICENSE",
        "license_note": "Fichier LICENSE du dépôt ; blob git d04713341a05cd40310468bd46647eddbecfb514 pour les poids.",
        "repository": "https://github.com/luminous0219/fire-and-smoke-detection-yolov8",
        "revision": "c30923f568d915a53fcf85989be9fe5d4e030518",
        "dataset": "Roboflow fire-and-smoke (https://universe.roboflow.com/fire-rqbio/fire-and-smoke-yikzn)",
        "dataset_license": "CC BY 4.0 (non vérifiée directement)",
        "expected_names": {0: "fire", 1: "smoke"},
    },
)
MODEL_IDS = tuple(model["id"] for model in MODELS)

# Environnements épinglés (testés sur le PC de référence, Python 3.12). Le runtime GPU embarque CUDA 13 et cuDNN
# (≈ 1,3 Go) et exige un pilote NVIDIA ≥ 580 ; sans lui, le moteur se replie sur le processeur en le disant.
RUNTIME_PYTHON = "3.12"
GPU_MIN_DRIVER = 580
RUNTIME_GPU_PACKAGES = (
    "onnxruntime-gpu[cuda,cudnn]==1.30.0",
    "nvidia-cublas==13.8.0.4",
    "nvidia-cuda-nvrtc==13.4.92",
    "nvidia-cuda-runtime==13.4.92",
    "nvidia-cudnn-cu13==9.27.0.42",
    "nvidia-cufft==12.4.0.43",
    "nvidia-curand==10.4.4.72",
    "nvidia-nvjitlink==13.4.92",
    "numpy==2.5.3",
    "opencv-python-headless==4.14.0.94",
    "flatbuffers==25.12.19",
    "packaging==26.3",
    "protobuf==7.36.2",
)
RUNTIME_CPU_PACKAGES = (
    "onnxruntime==1.30.0",
    "numpy==2.5.3",
    "opencv-python-headless==4.14.0.94",
    "flatbuffers==25.12.19",
    "packaging==26.3",
    "protobuf==7.36.2",
)
RUNTIME_GPU_REQUIREMENTS = "requirements-runtime-gpu.txt"
RUNTIME_CPU_REQUIREMENTS = "requirements-runtime-cpu.txt"
# Environnement d'export jetable (torch CPU + ultralytics), supprimé après la conversion.
EXPORT_REQUIREMENTS = "requirements-export.txt"


def model_by_id(model_id):
    """Fiche du catalogue (copie) ou ``None``."""
    for model in MODELS:
        if model["id"] == model_id:
            return {**model, "expected_names": dict(model["expected_names"])}
    return None


def runtime_packages(gpu):
    return RUNTIME_GPU_PACKAGES if gpu else RUNTIME_CPU_PACKAGES


def public_models():
    """Catalogue affichable (site, interface) : uniquement des données publiques, clés JSON en texte."""
    result = []
    for model in MODELS:
        names = model["expected_names"]
        result.append({
            "id": model["id"], "label": model["label"], "default": model["id"] == DEFAULT_MODEL,
            "license": model["license"], "license_url": model["license_url"], "repository": model["repository"],
            "revision": model["revision"], "dataset": model["dataset"], "dataset_license": model["dataset_license"],
            "url": model["url"], "sha256": model["sha256"], "size": model["size"],
            "classes": [names[index] for index in sorted(names)],
        })
    return result


def model_paths(worker_root, model_id):
    """Chemins d'un modèle dans le composant du Worker (``worker_root`` : pathlib.Path)."""
    if model_by_id(model_id) is None:
        raise ValueError("Modèle inconnu.")
    folder = worker_root / COMPONENT_DIR / MODELS_DIR / model_id
    return {"dir": folder, "source": folder / SOURCE_FILENAME, "onnx": folder / MODEL_FILENAME,
            "provenance": folder / PROVENANCE_FILENAME, "license": folder / LICENSE_FILENAME}


def build_provenance(model_id, export_report, converted_at=None):
    """Contenu de provenance.json : source épinglée, empreintes, outils de conversion (modification AGPL-3.0).

    ``export_report`` est le dict imprimé par ``export_onnx`` (ligne ``FIRE_WATCH_EXPORT=``). Les empreintes de
    l'ONNX dépendent de la date d'export (métadonnée ``date``) : elles sont consignées ici, jamais épinglées.
    """
    model = model_by_id(model_id)
    if model is None:
        raise ValueError("Modèle inconnu.")
    report = export_report if isinstance(export_report, dict) else {}
    if converted_at is None:
        converted_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    return {
        "model_id": model_id,
        "label": model["label"],
        "source": {"url": model["url"], "repository": model["repository"], "revision": model["revision"],
                   "sha256": model["sha256"], "size": model["size"]},
        "license": model["license"], "license_url": model["license_url"], "license_text": LICENSE_FILENAME,
        "dataset": model["dataset"], "dataset_license": model["dataset_license"],
        "expected_names": {str(key): value for key, value in model["expected_names"].items()},
        "conversion": {
            "notice": "Poids convertis localement de PyTorch (.pt) en ONNX sur ce Worker : modification au sens de "
                      "l'AGPL-3.0. Poids non redistribués par Alpine Makers.",
            "converted_at": converted_at,
            "onnx_sha256": report.get("onnx_sha256"), "onnx_bytes": report.get("onnx_bytes"),
            "names": report.get("names"), "imgsz": report.get("imgsz"), "opset": report.get("opset"),
            "simplify": report.get("simplify"), "ultralytics": report.get("ultralytics"),
            "torch": report.get("torch"), "onnx": report.get("onnx"),
        },
    }
