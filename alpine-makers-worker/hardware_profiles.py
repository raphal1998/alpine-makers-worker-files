"""Shared, deterministic hardware policy; no engine import, install or GPU work.

Wheel versions: https://pytorch.org/get-started/previous-versions/#v2100
Architecture policy: https://github.com/pytorch/pytorch/issues/157517
Driver floors deliberately use the CUDA 12.6/12.8 GA release-note versions,
not the more permissive CUDA minor-version compatibility minimums.
"""
import csv
import io
import math
import platform
import re
import shutil
import subprocess

if __package__:
    from .installers.model_catalog import COMFYUI_REQUIREMENTS, HUNYUAN_REQUIREMENTS
else:
    from installers.model_catalog import COMFYUI_REQUIREMENTS, HUNYUAN_REQUIREMENTS


CAPABILITY = "hardware_profiles_v1"
MIN_AGENT_VERSION = "1.15.0"
ENGINE_PYTHON = "3.12"
GPU_TOOLS = frozenset({"image_generation", "ai3d"})
CPU_LABELS = {"converter": "FreeCAD · CPU", "model-studio": "OrcaSlicer · CPU", "printguard": "PrintGuard · LiteRT CPU",
              "laser-studio": "Moteur laser · CPU"}
# Paquets distribués uniquement sous Windows ; les autres outils (FreeCAD, moteur laser, GPU) vont aussi sur Linux.
WINDOWS_ONLY_TOOLS = frozenset({"model-studio", "printguard"})
DRIVER_MINIMUMS = {
    "12.6": {"windows": "560.76", "linux": "560.28.03"},
    "12.8": {"windows": "570.65", "linux": "570.26"},
}
# Surveillance IA des graveuses (agent 1.34.0) : ONNX Runtime GPU épinglé est construit pour CUDA 13,
# qui exige un pilote NVIDIA 580 ou plus et une carte Turing (7.5) ou plus récente. Sinon : CPU.
FIRE_WATCH_MIN_DRIVER = "580.0"
FIRE_WATCH_CAPABILITIES = ((7, 5), (12, 1))


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _compute_capability(value):
    if isinstance(value, bool):
        return None
    text = str(value).strip() if isinstance(value, (str, int, float)) else ""
    if not re.fullmatch(r"\d{1,2}(?:\.\d)?", text):
        return None
    major, _, minor = text.partition(".")
    return int(major), int(minor or 0)


def _driver_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,4}\.\d{1,3}(?:\.\d{1,3})?", value.strip()):
        return None
    parts = tuple(map(int, value.strip().split(".")))
    return parts + (0,) * (3 - len(parts))


def _base_profile():
    return {"status": "unknown", "reason": "", "label": "Profil matériel à vérifier", "index_url": "",
            "cuda": "", "packages": [], "compute_capability": None, "gpu_name": "",
            "selected_gpu_index": None, "python": ENGINE_PYTHON, "min_driver": "",
            "vram_total_mb": None, "warnings": []}


def runtime_profile(worker, tool_id):
    """Select the largest usable NVIDIA GPU, never infer its CC from its name.

    This is an installation policy, not a proof of successful model execution.
    Unknown reports never authorize a GPU installation; CPU tools are separate.
    Every result is a fresh dictionary and the supplied inventory is untouched.
    """
    result = _base_profile()
    worker = worker if isinstance(worker, dict) else {}

    def refuse(status, reason):
        result.update(status=status, reason=reason)
        return result

    if tool_id not in GPU_TOOLS and tool_id not in CPU_LABELS:
        return refuse("unsupported", "Cet outil n’a pas de profil matériel pris en charge.")
    operating_system = str(worker.get("os_name") or "").strip().casefold()
    if not operating_system:
        return refuse("unknown", "Système du Worker inconnu : actualise son inventaire.")
    allowed_os = {"windows"} if tool_id in WINDOWS_ONLY_TOOLS else {"windows", "linux"}
    if operating_system not in allowed_os:
        return refuse("unsupported", "Ce moteur Worker n’est pas distribué pour ce système.")
    architecture = str(worker.get("architecture") or "").strip().casefold()
    if not architecture:
        return refuse("unknown", "Architecture du Worker inconnue : un système x64 est requis.")
    if architecture not in {"amd64", "x86_64", "x64"}:
        return refuse("unsupported", "Les paquets de ce moteur nécessitent un Worker x64 (Windows ou Linux pris en charge).")
    if tool_id in CPU_LABELS:
        result.update(status="supported", label=CPU_LABELS[tool_id], python="")
        return result

    gpus = worker.get("gpus")
    if not isinstance(gpus, list):
        return refuse("unknown", "Inventaire GPU indisponible : aucune installation CUDA automatique.")
    nvidia = [gpu for gpu in gpus if isinstance(gpu, dict) and str(gpu.get("vendor") or "").casefold() == "nvidia"]
    if not nvidia:
        if worker.get("gpu_detection") == "unknown" or any(not isinstance(gpu, dict) or not gpu.get("vendor") for gpu in gpus):
            return refuse("unknown", "Détection NVIDIA incomplète : vérifie le pilote puis actualise le Worker.")
        return refuse("unsupported", "Aucun GPU NVIDIA détecté : ce moteur ne bascule pas automatiquement sur le CPU.")

    candidates, unknown, rejected = [], [], []
    for gpu in nvidia:
        index = gpu.get("index")
        capability = _compute_capability(gpu.get("compute_capability"))
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or capability is None:
            unknown.append("Indice GPU ou compute capability non renseigné : actualise le diagnostic du Worker.")
            continue
        if capability < (5, 0):
            rejected.append("GPU NVIDIA trop ancien : les paquets Worker nécessitent une compute capability d’au moins 5.0.")
            continue
        if capability > (12, 1) or (7, 2) < capability < (7, 5):
            unknown.append("Cette compute capability NVIDIA n’a pas de profil Worker validé.")
            continue
        cuda = "12.6" if capability <= (7, 2) else "12.8"
        minimum = DRIVER_MINIMUMS[cuda][operating_system]
        raw_driver = gpu.get("driver_version") or worker.get("driver_version")
        driver = _driver_version(raw_driver)
        if driver is None:
            unknown.append("Version du pilote NVIDIA inconnue : actualise le diagnostic avant l’installation.")
            continue
        if driver < _driver_version(minimum):
            rejected.append(f"Pilote NVIDIA trop ancien pour CUDA {cuda} : version {minimum} minimum requise sur {worker.get('os_name')}.")
            continue
        candidates.append((gpu, capability, cuda, minimum))
    if not candidates:
        return refuse("unknown" if unknown else "unsupported", (unknown or rejected)[0])

    gpu, capability, cuda, minimum = max(candidates, key=lambda entry: (_number(entry[0].get("vram_total_mb")) or 0, -entry[0]["index"]))
    suffix = "cu" + cuda.replace(".", "")
    result.update(status="supported", cuda=cuda, index_url="https://download.pytorch.org/whl/" + suffix,
                  packages=["torch==2.10.0+" + suffix, "torchvision==0.25.0+" + suffix, "torchaudio==2.10.0+" + suffix],
                  compute_capability=f"{capability[0]}.{capability[1]}", gpu_name=str(gpu.get("name") or "GPU NVIDIA")[:160],
                  selected_gpu_index=gpu["index"], min_driver=minimum, vram_total_mb=_number(gpu.get("vram_total_mb")))
    family = "Maxwell / Pascal / Volta" if cuda == "12.6" else "Turing et générations suivantes"
    result["label"] = f"PyTorch 2.10 · CUDA {cuda} · {family}"
    if result["vram_total_mb"] is None:
        result["warnings"].append("VRAM totale inconnue : la capacité nécessaire au modèle reste à vérifier.")
    if len(nvidia) > 1:
        result["warnings"].append(f"GPU {gpu['index']} sélectionné ; la VRAM des cartes n’est pas additionnée.")
    if unknown or rejected:
        result["warnings"].append("Certaines cartes ont été écartées du choix : matériel ou pilote incompatible/non renseigné.")
    if tool_id == "ai3d" and capability < (7, 5):
        result["warnings"].append("Hunyuan3D sur Maxwell/Pascal/Volta reste à valider en génération ; la présence de CUDA ne garantit ni la précision ni la mémoire nécessaire.")
    return result


def fire_watch_profile(worker):
    """Profil d'installation du moteur de surveillance flamme/fumée : « gpu » (ONNX Runtime CUDA) ou « cpu ».

    Politique d'installation seulement, jamais une preuve : au démarrage, le moteur compare lui-même le
    calcul CUDA au calcul CPU et repasse sur le CPU (raison affichée) si le GPU échoue ou diverge. Un
    inventaire incomplet choisit le CPU, qui fonctionne partout (plus lent).
    """
    result = {"profile": "cpu", "reason": "", "label": "ONNX Runtime · CPU", "gpu_name": "", "gpu_index": None,
              "compute_capability": None, "driver_version": ""}
    worker = worker if isinstance(worker, dict) else {}
    gpus = worker.get("gpus") if isinstance(worker.get("gpus"), list) else []
    nvidia = [gpu for gpu in gpus if isinstance(gpu, dict) and str(gpu.get("vendor") or "").casefold() == "nvidia"]
    if not nvidia:
        result["reason"] = "Aucun GPU NVIDIA détecté : l’analyse tournera sur le CPU (plus lente)."
        return result
    low, high = FIRE_WATCH_CAPABILITIES
    candidates, refused = [], []
    for gpu in nvidia:
        name = str(gpu.get("name") or "GPU NVIDIA")[:160]
        capability = _compute_capability(gpu.get("compute_capability"))
        raw_driver = str(gpu.get("driver_version") or worker.get("driver_version") or "")
        driver = _driver_version(raw_driver)
        if capability is None:
            refused.append(f"{name} : compute capability non renseignée")
        elif not low <= capability <= high:
            refused.append(f"{name} : compute capability {capability[0]}.{capability[1]} hors du profil CUDA 13 (7.5 à 12.1)")
        elif driver is None:
            refused.append(f"{name} : version du pilote NVIDIA inconnue")
        elif driver < _driver_version(FIRE_WATCH_MIN_DRIVER):
            refused.append(f"{name} : pilote {raw_driver} trop ancien (580 minimum pour CUDA 13)")
        else:
            candidates.append((gpu, name, capability, raw_driver))
    if not candidates:
        result["reason"] = refused[0] + " ; l’analyse tournera sur le CPU (plus lente). Mets à jour le pilote NVIDIA " \
                                        "puis relance l’installation pour utiliser le GPU."
        return result
    def index_of(gpu):
        index = gpu.get("index")
        return index if isinstance(index, int) and not isinstance(index, bool) and index >= 0 else None

    # Comme runtime_profile : la carte de plus grande VRAM, la VRAM des cartes n'étant jamais additionnée.
    gpu, name, capability, raw_driver = max(candidates, key=lambda entry: (_number(entry[0].get("vram_total_mb")) or 0,
                                                                           -(index_of(entry[0]) or 0)))
    result.update(profile="gpu", label="ONNX Runtime · CUDA 13 (GPU NVIDIA)", gpu_name=name, gpu_index=index_of(gpu),
                  compute_capability=f"{capability[0]}.{capability[1]}", driver_version=raw_driver)
    return result


def model_hardware_error(tool_id, model_id, worker):
    """Return the blocking runtime/VRAM reason for a centrally catalogued model.

    Manual checkpoints have no invented memory threshold; their architecture
    still receives the engine profile checks. Disk budgeting is separate.
    """
    profile = runtime_profile(worker, tool_id)
    if profile["status"] != "supported":
        return profile["reason"]
    requirements = COMFYUI_REQUIREMENTS if tool_id == "image_generation" else HUNYUAN_REQUIREMENTS if tool_id == "ai3d" else {}
    requirement = requirements.get(model_id)
    if requirement is None:
        return ""
    memory = profile["vram_total_mb"]
    if memory is None:
        return "VRAM totale du GPU sélectionné inconnue : actualise le diagnostic avant d’installer ce modèle."
    if memory < requirement[0]:
        return f"VRAM insuffisante pour ce modèle : {memory / 1000:g} Go sur le GPU sélectionné, {requirement[0] / 1000:g} Go minimum estimés."
    return ""


def _query_rows(output, with_capability):
    rows, malformed = [], False
    expected = 6 if with_capability else 5
    for values in csv.reader(io.StringIO(output), skipinitialspace=True):
        if not values or not any(part.strip() for part in values):
            continue
        values = [part.strip() for part in values]
        try:
            if len(values) != expected or not re.fullmatch(r"\d+", values[0]):
                raise ValueError()
            index, name = int(values[0]), values[1]
            capability = values[2] if with_capability else None
            driver, total, free = values[3:] if with_capability else values[2:]
            numeric = []
            for value in (total, free):
                numeric.append(int(value) if re.fullmatch(r"\d+", value) else None)
            normalized = _compute_capability(capability)
            rows.append({"index": index, "vendor": "NVIDIA", "name": name[:160],
                         "compute_capability": f"{normalized[0]}.{normalized[1]}" if normalized else None,
                         "driver_version": driver if _driver_version(driver) else "",
                         "vram_total_mb": numeric[0], "vram_free_mb": numeric[1]})
        except (ValueError, TypeError):
            malformed = True
    return rows, malformed


def detect_hardware():
    """Read only bounded nvidia-smi inventory; never import Torch or run kernels.

    Old nvidia-smi versions may not expose compute_cap. Their fallback inventory
    remains explicitly incomplete; GPU names are never used as architecture.
    """
    result = {"os_name": platform.system(), "architecture": platform.machine(), "gpus": [],
              "gpu_detection": "unknown", "driver_version": "", "cuda_version": ""}
    executable = shutil.which("nvidia-smi")
    if not executable:
        return result
    for with_capability in (True, False):
        columns = "index,name," + ("compute_cap," if with_capability else "") + "driver_version,memory.total,memory.free"
        try:
            response = subprocess.run([executable, "--query-gpu=" + columns, "--format=csv,noheader,nounits"],
                                      capture_output=True, text=True, timeout=10, check=False,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            continue
        if response.returncode != 0:
            continue
        gpus, malformed = _query_rows(response.stdout, with_capability)
        if malformed and not gpus:
            continue
        result.update(gpus=gpus, gpu_detection="unknown" if malformed or not with_capability else "ok")
        result["driver_version"] = gpus[0]["driver_version"] if gpus else ""
        return result
    return result
