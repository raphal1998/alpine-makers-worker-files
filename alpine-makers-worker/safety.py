"""Small shared safeguards for the outbound agent and its local installers."""
import copy
import math
import os
import re
import shutil
import stat
import time
import urllib.parse
from pathlib import Path, PureWindowsPath


def safe_relative_name(value, *, basename=False):
    """A portable relative file name, never a Windows device or ADS path."""
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ValueError("Nom de fichier Worker invalide.")
    name = value.replace("\\", "/")
    parts = name.split("/")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    if (PureWindowsPath(name).drive or (basename and len(parts) != 1)
            or any(not part or part in {".", ".."} or part.endswith((" ", "."))
                   or part.split(".")[0].casefold() in reserved
                   or any(ord(char) < 32 or char in ':<>"|?*' for char in part) for part in parts)):
        raise ValueError("Chemin de fichier Worker non autorisé.")
    return name


SOURCE_IMAGE_NAME = "source.png"

# The dashboard builds a small, fixed set of graphs: the historical SD1.5/SDXL pair and,
# since comfyui_dit_v1, one pair per transformer family (FLUX, SD 3.5, Lumina 2, Z-Image,
# Qwen-Image, Chroma, HiDream). Each graph is described field by field from the same recipe
# as the site's image_workflows/*.json: an arbitrary ComfyUI node is an executable capability,
# not a harmless generation setting, so anything outside these shapes is refused.
# Échantillonneur et planificateur du KSampler : un choix parmi ceux de ComfyUI, plus
# le seul couple « euler / normal » d'avant comfyui_sampler_choice_v1. Un nom hors de
# ces listes n'est pas un réglage, c'est une entrée inconnue : refusée.
COMFYUI_SAMPLERS = frozenset({"euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_sde",
                              "dpmpp_3m_sde", "ddim", "uni_pc", "lcm", "heun", "dpm_2", "dpm_2_ancestral"})
COMFYUI_SCHEDULERS = frozenset({"normal", "karras", "exponential", "sgm_uniform", "simple", "ddim_uniform", "beta"})
# Précisions de chargement de l'UNETLoader (poids bf16 castés en fp8 sur les cartes de 16 Go).
COMFYUI_WEIGHT_DTYPES = frozenset({"default", "fp8_e4m3fn", "fp8_e4m3fn_fast", "fp8_e5m2"})
COMFYUI_MODEL_SUFFIXES = frozenset({".safetensors", ".ckpt", ".sft"})
# Plafonds du KSampler de ComfyUI lui-même. Chaque propriétaire règle ensuite,
# Worker par Worker, des limites plus basses depuis Mes Workers (100 / 30 par défaut).
COMFYUI_MAX_STEPS = 10000
COMFYUI_MAX_CFG = 100
# Plafonds des agents antérieurs à 1.20.0 : ils refusent un workflow qui les
# dépasse, donc seul un Worker annonçant comfyui_extended_sampling_v1 le reçoit.
COMFYUI_LEGACY_MAX_STEPS = 100
COMFYUI_LEGACY_MAX_CFG = 30

# Familles de graphes. loader : "checkpoint" (tout-en-un, CheckpointLoaderSimple) ou "split"
# (UNETLoader + chargeur de texte + VAELoader) ; sampling : nœud de rééchelonnage ; guidance :
# nœud FluxGuidance sur le prompt positif ; clip : (classe du chargeur de texte, type) ;
# latent : nœud de latent vide en texte → image. La même table construit les fichiers du site.
COMFYUI_FAMILIES = {
    "sd":      {"loader": "checkpoint", "sampling": None,                     "guidance": False, "clip": None, "latent": "EmptyLatentImage"},
    "flux":    {"loader": "checkpoint", "sampling": None,                     "guidance": True,  "clip": None, "latent": "EmptySD3LatentImage"},
    "sd35":    {"loader": "checkpoint", "sampling": "ModelSamplingSD3",       "guidance": False, "clip": None, "latent": "EmptySD3LatentImage"},
    "lumina2": {"loader": "checkpoint", "sampling": "ModelSamplingAuraFlow",  "guidance": False, "clip": None, "latent": "EmptySD3LatentImage"},
    "zimage":  {"loader": "split",      "sampling": "ModelSamplingAuraFlow",  "guidance": False, "clip": ("CLIPLoader", "lumina2"), "latent": "EmptySD3LatentImage"},
    "qwen":    {"loader": "split",      "sampling": "ModelSamplingAuraFlow",  "guidance": False, "clip": ("CLIPLoader", "qwen_image"), "latent": "EmptySD3LatentImage"},
    "chroma":  {"loader": "split",      "sampling": None,                     "guidance": False, "clip": ("CLIPLoader", "chroma"), "latent": "EmptySD3LatentImage"},
    "hidream": {"loader": "split",      "sampling": "ModelSamplingSD3",       "guidance": False, "clip": ("QuadrupleCLIPLoader", None), "latent": "EmptySD3LatentImage"},
}
COMFYUI_TASKS = ("text_to_image", "image_to_image")
# comfyui_controlnet_v1 (agent 1.37.0) : texte + image de contrôle → image, familles SD 1.5 / SDXL. L'image de contrôle
# est l'unique image source du job ; « controlnet_canny » en tire d'abord les contours (nœud Canny natif de ComfyUI).
COMFYUI_CONTROL_TASKS = ("controlnet", "controlnet_canny")
COMFYUI_LORA_MAX = 8
_FREE = object()  # valeur fournie par le site (prompt, réglage, nom de fichier) : contrôlée par type et bornes


def comfyui_server_url():
    port = "28188" if os.getenv("ALPINE_LOCAL_SANDBOX") == "1" else os.getenv("COMFYUI_PORT", "8188")
    return "http://127.0.0.1:" + str(int(port))


def release_comfyui_vram(timeout=20):
    """Demander au moteur image de ce Worker de rendre la VRAM de son modèle.

    ComfyUI garde son checkpoint résident entre deux générations : sans cette
    demande, la carte reste prise (mesuré : 6 845 Mo encore détenus après une
    génération SDXL, 206 Mo une fois la demande faite) et le dashboard refuse la
    suivante — image ou 3D — avec « VRAM libre insuffisante ». Meilleur effort :
    si aucun moteur image ne répond sur ce PC, il n'y a rien à libérer.
    L'appelant s'assure qu'aucun calcul n'est en cours.
    """
    import json
    import urllib.error
    import urllib.request

    payload = json.dumps({"unload_models": True, "free_memory": True}).encode("utf-8")
    request = urllib.request.Request(comfyui_server_url() + "/free", data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def comfyui_queue_busy(timeout=10):
    """Le moteur image calcule-t-il ou a-t-il quelque chose en attente ? None si on ne sait pas.

    « On ne sait pas » (moteur muet, réponse illisible) n'est jamais traité comme
    « libre » : on ne décharge pas un modèle sur une supposition.
    """
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(comfyui_server_url() + "/queue", timeout=timeout) as response:
            queue = json.loads(response.read(8 * 1024 * 1024))
        running, pending = queue["queue_running"], queue["queue_pending"]
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(running, list) or not isinstance(pending, list):
        return None
    return bool(running or pending)


def release_comfyui_vram_when_idle(timeout=20):
    """Rendre la carte seulement si le moteur image n'a plus rien à calculer.

    Une série d'images à la suite ne doit pas recharger son checkpoint entre deux
    calculs, et le calcul d'un autre job ne doit jamais perdre sa mémoire. Appelée
    depuis un « finally » de fin de job : elle ne doit jamais remplacer le résultat
    — ni l'erreur — du calcul qui vient de se terminer.
    """
    try:
        if comfyui_queue_busy() is not False:
            return False
        return release_comfyui_vram(timeout)
    except Exception:
        return False


def _comfyui_graph(family, task, lora_count=0):
    """(schéma, liaisons fixes, bornes) du graphe d'une famille pour une tâche."""
    spec = COMFYUI_FAMILIES[family]
    nodes = {}
    if spec["loader"] == "checkpoint":
        nodes["1"] = ("CheckpointLoaderSimple", {"ckpt_name": _FREE})
        model_ref, clip_ref, vae_ref = ["1", 0], ["1", 1], ["1", 2]
    else:
        nodes["1"] = ("UNETLoader", {"unet_name": _FREE, "weight_dtype": _FREE})
        clip_class, clip_type = spec["clip"]
        if clip_class == "CLIPLoader":
            nodes["11"] = ("CLIPLoader", {"clip_name": _FREE, "type": clip_type})
        elif clip_class == "DualCLIPLoader":
            nodes["11"] = ("DualCLIPLoader", {"clip_name1": _FREE, "clip_name2": _FREE, "type": clip_type})
        else:
            nodes["11"] = ("QuadrupleCLIPLoader", {"clip_name1": _FREE, "clip_name2": _FREE, "clip_name3": _FREE, "clip_name4": _FREE})
        nodes["12"] = ("VAELoader", {"vae_name": _FREE})
        model_ref, clip_ref, vae_ref = ["1", 0], ["11", 0], ["12", 0]
    if spec["sampling"]:
        nodes["10"] = (spec["sampling"], {"model": model_ref, "shift": _FREE})
        model_ref = ["10", 0]
    if lora_count:
        if not 1 <= lora_count <= COMFYUI_LORA_MAX:
            raise ValueError("Chaîne LoRA ComfyUI non autorisée.")
        for index in range(lora_count):
            node_id = str(20 + index)
            if family == "sd":
                nodes[node_id] = ("LoraLoader", {
                    "model": model_ref, "clip": clip_ref, "lora_name": _FREE,
                    "strength_model": _FREE, "strength_clip": _FREE,
                })
                model_ref, clip_ref = [node_id, 0], [node_id, 1]
            else:
                # Familles DiT (comfyui_lora_v2) : le LoRA ne touche que le modèle de diffusion, comme dans les
                # workflows officiels FLUX et Qwen-Image ; l'encodeur de texte garde son chargeur propre.
                nodes[node_id] = ("LoraLoaderModelOnly", {"model": model_ref, "lora_name": _FREE, "strength_model": _FREE})
                model_ref = [node_id, 0]
    nodes["2"] = ("CLIPTextEncode", {"text": _FREE, "clip": clip_ref})
    nodes["3"] = ("CLIPTextEncode", {"text": _FREE, "clip": clip_ref})
    positive_ref = ["2", 0]
    if spec["guidance"]:
        nodes["13"] = ("FluxGuidance", {"conditioning": ["2", 0], "guidance": _FREE})
        positive_ref = ["13", 0]
    negative_ref = ["3", 0]
    control = task in COMFYUI_CONTROL_TASKS
    if control:
        if family != "sd":
            raise ValueError("ControlNet : graphes SD 1.5 et SDXL seulement.")
        nodes["4"] = (spec["latent"], {"width": _FREE, "height": _FREE, "batch_size": _FREE})
        latent_ref, denoise = ["4", 0], 1
        nodes["30"] = ("LoadImage", {"image": SOURCE_IMAGE_NAME})
        nodes["31"] = ("ImageScale", {"image": ["30", 0], "upscale_method": "lanczos", "width": _FREE, "height": _FREE, "crop": "center"})
        hint_ref = ["31", 0]
        if task == "controlnet_canny":
            nodes["32"] = ("Canny", {"image": ["31", 0], "low_threshold": _FREE, "high_threshold": _FREE})
            hint_ref = ["32", 0]
        nodes["33"] = ("ControlNetLoader", {"control_net_name": _FREE})
        nodes["34"] = ("ControlNetApplyAdvanced", {"positive": positive_ref, "negative": ["3", 0], "control_net": ["33", 0], "image": hint_ref,
                                                   "strength": _FREE, "start_percent": _FREE, "end_percent": _FREE})
        positive_ref, negative_ref = ["34", 0], ["34", 1]
    elif task == "text_to_image":
        nodes["4"] = (spec["latent"], {"width": _FREE, "height": _FREE, "batch_size": _FREE})
        latent_ref, denoise = ["4", 0], 1
    else:
        nodes["4"] = ("LoadImage", {"image": SOURCE_IMAGE_NAME})
        nodes["8"] = ("ImageScale", {"image": ["4", 0], "upscale_method": "lanczos", "width": _FREE, "height": _FREE, "crop": "center"})
        nodes["9"] = ("VAEEncode", {"pixels": ["8", 0], "vae": vae_ref})
        latent_ref, denoise = ["9", 0], _FREE
    nodes["5"] = ("KSampler", {"seed": _FREE, "steps": _FREE, "cfg": _FREE, "sampler_name": _FREE, "scheduler": _FREE,
                               "denoise": denoise, "model": model_ref, "positive": positive_ref, "negative": negative_ref,
                               "latent_image": latent_ref})
    nodes["6"] = ("VAEDecode", {"samples": ["5", 0], "vae": vae_ref})
    nodes["7"] = ("SaveImage", {"filename_prefix": _FREE, "images": ["6", 0]})
    schema = {key: (cls, set(inputs)) for key, (cls, inputs) in nodes.items()}
    links = {}
    for key, (_, inputs) in nodes.items():
        fixed = {name: value for name, value in inputs.items() if value is not _FREE}
        if fixed:
            links[key] = fixed
    bounds = {"5": {"seed": (0, (1 << 63) - 1), "steps": (1, COMFYUI_MAX_STEPS), "cfg": (0, COMFYUI_MAX_CFG)}}
    if task == "text_to_image" or control:
        bounds["4"] = {"width": (256, 2048), "height": (256, 2048), "batch_size": (1, 4)}
    else:
        bounds["8"] = {"width": (256, 2048), "height": (256, 2048)}
        bounds["5"]["denoise"] = (0.05, 1.0)
    if control:
        bounds["31"] = {"width": (256, 2048), "height": (256, 2048)}
        bounds["34"] = {"strength": (0, 2), "start_percent": (0, 1), "end_percent": (0, 1)}
        if "32" in nodes:
            bounds["32"] = {"low_threshold": (0.01, 0.99), "high_threshold": (0.01, 0.99)}
    if "10" in nodes:
        bounds["10"] = {"shift": (0, 100)}
    if "13" in nodes:
        bounds["13"] = {"guidance": (0, 100)}
    for index in range(lora_count):
        bounds[str(20 + index)] = ({"strength_model": (-2, 2), "strength_clip": (-2, 2)} if family == "sd"
                                   else {"strength_model": (-2, 2)})
    return schema, links, bounds


COMFYUI_GRAPHS = {(family, task): _comfyui_graph(family, task) for family in COMFYUI_FAMILIES for task in COMFYUI_TASKS}
# comfyui_lora_v1 : 1 à 4 LoRA sur les graphes SD. comfyui_lora_v2 (agent 1.36.0) : jusqu'à COMFYUI_LORA_MAX,
# toutes familles (LoraLoaderModelOnly pour les familles DiT).
COMFYUI_LORA_GRAPHS = {(family, task, count): _comfyui_graph(family, task, count)
                       for family in COMFYUI_FAMILIES for task in COMFYUI_TASKS for count in range(1, COMFYUI_LORA_MAX + 1)}
COMFYUI_GRAPHS.update({("sd", task): _comfyui_graph("sd", task) for task in COMFYUI_CONTROL_TASKS})
COMFYUI_LORA_GRAPHS.update({("sd", task, count): _comfyui_graph("sd", task, count)
                            for task in COMFYUI_CONTROL_TASKS for count in range(1, COMFYUI_LORA_MAX + 1)})


def _comfyui_cascade_graph():
    """Stable Cascade (comfyui_cascade_v1, agent 1.41.0) : texte → image en deux étages, comme le workflow officiel.

    Étage C (checkpoint « stage_c » : modèle + encodeur de texte) échantillonne un latent très compressé ;
    l'étage B (checkpoint « stage_b » : modèle + VAE) le décompresse, conditionné par le résultat de C, puis
    son VAE décode. Ce n'est pas un checkpoint SD : deux fichiers, deux KSampler, un latent propre.
    """
    nodes = {
        "1": ("CheckpointLoaderSimple", {"ckpt_name": _FREE}),
        "14": ("CheckpointLoaderSimple", {"ckpt_name": _FREE}),
        "2": ("CLIPTextEncode", {"text": _FREE, "clip": ["1", 1]}),
        "3": ("CLIPTextEncode", {"text": _FREE, "clip": ["1", 1]}),
        "4": ("StableCascade_EmptyLatentImage", {"width": _FREE, "height": _FREE, "compression": _FREE, "batch_size": _FREE}),
        "5": ("KSampler", {"seed": _FREE, "steps": _FREE, "cfg": _FREE, "sampler_name": _FREE, "scheduler": _FREE, "denoise": 1,
                           "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0]}),
        "15": ("StableCascade_StageB_Conditioning", {"conditioning": ["2", 0], "stage_c": ["5", 0]}),
        "16": ("ConditioningZeroOut", {"conditioning": ["2", 0]}),
        "17": ("KSampler", {"seed": _FREE, "steps": _FREE, "cfg": _FREE, "sampler_name": _FREE, "scheduler": _FREE, "denoise": 1,
                            "model": ["14", 0], "positive": ["15", 0], "negative": ["16", 0], "latent_image": ["4", 1]}),
        "6": ("VAEDecode", {"samples": ["17", 0], "vae": ["14", 2]}),
        "7": ("SaveImage", {"filename_prefix": _FREE, "images": ["6", 0]}),
    }
    schema = {key: (cls, set(inputs)) for key, (cls, inputs) in nodes.items()}
    links = {key: {name: value for name, value in inputs.items() if value is not _FREE} for key, (_, inputs) in nodes.items()}
    links = {key: fixed for key, fixed in links.items() if fixed}
    sampling = {"seed": (0, (1 << 63) - 1), "steps": (1, COMFYUI_MAX_STEPS), "cfg": (0, COMFYUI_MAX_CFG)}
    bounds = {"4": {"width": (256, 2048), "height": (256, 2048), "compression": (4, 128), "batch_size": (1, 4)},
              "5": dict(sampling), "17": dict(sampling)}
    return schema, links, bounds


COMFYUI_GRAPHS[("cascade", "text_to_image")] = _comfyui_cascade_graph()
# Les deux graphes historiques gardent leur nom.
_TEXT_TO_IMAGE_SCHEMA, _TEXT_TO_IMAGE_LINKS, _TEXT_TO_IMAGE_BOUNDS = COMFYUI_GRAPHS[("sd", "text_to_image")]
_IMAGE_TO_IMAGE_SCHEMA, _IMAGE_TO_IMAGE_LINKS, _IMAGE_TO_IMAGE_BOUNDS = COMFYUI_GRAPHS[("sd", "image_to_image")]
# Entrées qui nomment un fichier de poids : un nom sûr et un suffixe de modèle, jamais un chemin.
_MODEL_FILE_FIELDS = frozenset({"ckpt_name", "unet_name", "clip_name", "clip_name1", "clip_name2", "clip_name3", "clip_name4", "vae_name", "lora_name",
                                "control_net_name"})


def comfyui_sampling_settings(parameters):
    """(étapes, CFG) du KSampler d'un workflow du dashboard, ou None s'il n'en a pas de lisible."""
    workflow = parameters.get("workflow") if isinstance(parameters, dict) else None
    sampler = workflow.get("5") if isinstance(workflow, dict) else None
    inputs = sampler.get("inputs") if isinstance(sampler, dict) else None
    if not isinstance(inputs, dict):
        return None
    steps, cfg = inputs.get("steps"), inputs.get("cfg")
    if type(steps) is not int or type(cfg) not in (int, float) or not math.isfinite(cfg):
        return None
    return steps, float(cfg)


def _validated_source_inputs(parameters):
    """The single source picture an image-to-image job is allowed to read.

    The name is fixed and separator-free, and the worker resolves it inside
    this job's own input directory, so a workflow cannot reach another job or
    another account's files.
    """
    if parameters.get("input_images") or parameters.get("input_file"):
        raise ValueError("Le Worker ComfyUI lit uniquement l’image source copiée pour ce job.")
    sources = parameters.get("input_files")
    if not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict):
        raise ValueError("Une génération à partir d’une image attend exactement une image source.")
    entry = sources[0]
    if set(entry) - {"name", "path"} or entry.get("name") != SOURCE_IMAGE_NAME:
        raise ValueError("Image source du job ComfyUI non autorisée.")
    if "path" in entry:
        safe_relative_name(str(entry["path"]).replace("\\", "/").rsplit("/", 1)[-1], basename=True)
    return entry


def _comfyui_graph_match(workflow):
    """Return ((family, task), schema tuple), accepting at most four fixed LoRA loaders."""
    if not isinstance(workflow, dict):
        return None
    structural = None
    graphs = list(COMFYUI_GRAPHS.items()) + list(COMFYUI_LORA_GRAPHS.items())
    for graph_key, graph in graphs:
        schema, links, _ = graph
        key = graph_key[:2]
        if set(workflow) != set(schema):
            continue
        if not all(isinstance(workflow[node_id], dict) and workflow[node_id].get("class_type") == class_type
                   for node_id, (class_type, _) in schema.items()):
            continue
        structural = structural or (key, graph)
        # Deux familles peuvent partager la même forme (Z-Image et Qwen-Image : seul le type du
        # chargeur de texte diffère) : la famille est celle dont toutes les constantes correspondent.
        try:
            if all(workflow[node_id]["inputs"].get(name) == expected for node_id, fixed in links.items() for name, expected in fixed.items()):
                return key, graph
        except (AttributeError, TypeError):
            continue
    # Aucune famille ne correspond entièrement : la validation se fait contre le graphe de même forme (avec ses
    # nœuds LoRA), qui refusera la liaison fautive — jamais contre un graphe plus petit qui ignorerait des nœuds.
    return structural


def comfyui_graph_kind(workflow):
    """(famille, tâche) du graphe, ou None : mêmes nœuds, mêmes classes, rien de plus."""
    matched = _comfyui_graph_match(workflow)
    return matched[0] if matched else None


def validate_comfyui_parameters(parameters, *, output_prefix=None):
    """Accept only the dashboard's fixed image graphs (SD 1.x/2.x/SDXL, Stable Cascade and the DiT families).

    For each family two shapes exist: text to image, and image to image reading the one
    source picture copied into this job. Everything else is refused.
    """
    if not isinstance(parameters, dict):
        raise ValueError("Paramètres ComfyUI invalides.")
    workflow = parameters.get("workflow")
    matched = _comfyui_graph_match(workflow)
    if matched is None:
        raise ValueError("Workflow ComfyUI non autorisé : utilise le générateur d’images du dashboard.")
    kind, graph = matched
    family, task = kind
    schema, links, bounds = graph
    if task == "image_to_image":
        _validated_source_inputs(parameters)
        prompt_required = ()
    elif task in COMFYUI_CONTROL_TASKS:
        _validated_source_inputs(parameters)      # l'image de contrôle : la seule image copiée pour ce job
        prompt_required = ("2",)
    else:
        if any(parameters.get(key) for key in ("input_images", "input_file", "input_files")):
            raise ValueError("Le Worker ComfyUI accepte uniquement le workflow texte vers image du dashboard.")
        prompt_required = ("2",)
    for key, (class_type, fields) in schema.items():
        node = workflow[key]
        if (not isinstance(node, dict) or set(node) != {"class_type", "inputs"}
                or node.get("class_type") != class_type or not isinstance(node.get("inputs"), dict)
                or set(node["inputs"]) != fields):
            raise ValueError("Nœud ou entrée du workflow ComfyUI non autorisé.")
    for node_id, fields in links.items():
        for name, expected in fields.items():
            actual = workflow[node_id]["inputs"][name]
            if actual != expected or isinstance(actual, bool) or (isinstance(expected, list) and
                    (not isinstance(actual, list) or any(type(a) is not type(b) for a, b in zip(actual, expected)))):
                raise ValueError("Liaison du workflow ComfyUI non autorisée.")
    for node_id, (class_type, _) in schema.items():
        if class_type != "KSampler":
            continue      # Stable Cascade a deux KSampler (étages C et B) : chacun est contrôlé
        sampler = workflow[node_id]["inputs"]["sampler_name"]
        scheduler = workflow[node_id]["inputs"]["scheduler"]
        if not isinstance(sampler, str) or sampler not in COMFYUI_SAMPLERS or not isinstance(scheduler, str) or scheduler not in COMFYUI_SCHEDULERS:
            raise ValueError("Échantillonneur ou planificateur ComfyUI non autorisé.")
    for node_id in ("2", "3"):
        prompt = workflow[node_id]["inputs"]["text"]
        if not isinstance(prompt, str) or len(prompt) > 2048 or (node_id in prompt_required and not prompt.strip()):
            raise ValueError("Prompt ComfyUI invalide.")
    for node_id, fields in bounds.items():
        for name, (low, high) in fields.items():
            value = workflow[node_id]["inputs"][name]
            permitted = (int, float) if name in {"cfg", "denoise", "shift", "guidance", "strength_model", "strength_clip", "strength",
                                                 "start_percent", "end_percent", "low_threshold", "high_threshold"} else (int,)
            if type(value) not in permitted or not low <= value <= high or (isinstance(value, float) and not math.isfinite(value)):
                raise ValueError("Valeur du workflow ComfyUI hors limites.")
    clean = copy.deepcopy(parameters)
    for node_id, (_, fields) in schema.items():
        for name in fields & _MODEL_FILE_FIELDS:
            value = workflow[node_id]["inputs"][name]
            if not isinstance(value, str):
                raise ValueError("Checkpoint ComfyUI non autorisé.")
            safe = safe_relative_name(value)
            if Path(safe).suffix.lower() not in COMFYUI_MODEL_SUFFIXES:
                raise ValueError("Checkpoint ComfyUI non autorisé.")
            if name in ("lora_name", "control_net_name") and Path(safe).suffix.lower() not in (".safetensors", ".sft"):
                raise ValueError("LoRA ou ControlNet ComfyUI non autorisé : format safetensors attendu.")
            # ComfyUI nomme un fichier rangé dans un sous-dossier avec le séparateur du système (« styles\x » sous
            # Windows) et refuse l'autre forme : le nom validé lui est remis tel qu'il le liste.
            clean["workflow"][node_id]["inputs"][name] = safe.replace("/", os.sep)
    if "34" in schema and workflow["34"]["inputs"]["start_percent"] > workflow["34"]["inputs"]["end_percent"]:
        raise ValueError("Plage d’application du ControlNet invalide.")
    if "32" in schema and workflow["32"]["inputs"]["low_threshold"] > workflow["32"]["inputs"]["high_threshold"]:
        raise ValueError("Seuils de contours invalides.")
    if schema["1"][0] == "UNETLoader" and workflow["1"]["inputs"]["weight_dtype"] not in COMFYUI_WEIGHT_DTYPES:
        raise ValueError("Précision de chargement ComfyUI non autorisée.")
    safe_relative_name(workflow["7"]["inputs"]["filename_prefix"])
    if output_prefix is not None:
        clean["workflow"]["7"]["inputs"]["filename_prefix"] = safe_relative_name(output_prefix)
    return clean


# Labo image (comfyui_image_lab_v1, agent 1.39.0) : outils sur UNE image (agrandir, cartes de contrôle, détourer),
# faits par les nœuds natifs de ComfyUI — aucun nœud tiers. Même règle que pour la génération : chaque outil est un
# graphe FIXE décrit champ par champ ; un nœud, une entrée, une liaison ou un fichier hors de cette forme est refusé.
# L'image du job est l'unique image lue (nœud « 30 »), le résultat sort par le nœud « 7 » comme pour une génération.
# Cette description est indépendante du constructeur du site (image_lab.py) : les deux doivent s'accorder.
IMAGE_LAB_CAPABILITY = "comfyui_image_lab_v1"
IMAGE_LAB_PARAMETER_KEYS = frozenset({"workflow", "lab_tool", "input_files", "timeout_seconds"})
# Fichiers de poids admis par outil : ceux du catalogue épinglé des modèles utilitaires (installers/model_catalog.py,
# COMFYUI_UTILITY_MODELS), tous en safetensors. Un autre nom — même sûr — n'est pas un réglage : refusé.
IMAGE_LAB_MODEL_FILES = {
    "upscale": frozenset({"RealESRGAN_x4plus.safetensors"}),
    "control_depth": frozenset({"depth_anything_3_small.safetensors", "depth_anything_3_base.safetensors"}),
    "control_pose": frozenset({"sdpose_wholebody_fp16.safetensors"}),
    "remove_background": frozenset({"birefnet.safetensors"}),
    "caption": frozenset({"qwen3.5_2b_bf16.safetensors"}),
}
# Outils dont la sortie est un TEXTE (nœud natif PreviewAny en « 7 ») : aucun fichier écrit par le moteur ; le lanceur
# relit le texte dans l'historique de ComfyUI et le rend borné dans un artefact « description.txt ».
IMAGE_LAB_TEXT_TOOLS = frozenset({"caption"})
IMAGE_LAB_TEXT_MAX_CHARS = 8000
# Description : consignes FIXES (le navigateur choisit une forme et une langue, jamais le texte de la consigne).
# Le validateur n'accepte que ces chaînes exactes ; le site construit le graphe à partir de cette même table.
IMAGE_LAB_CAPTION_PROMPTS = {
    ("sentence", "fr"): "Décris cette image en une seule phrase, en français. Réponds uniquement par la phrase.",
    ("detailed", "fr"): ("Décris cette image en français, en un paragraphe de quatre à six phrases : sujet principal, décor, "
                         "couleurs, lumière, cadrage et style. Réponds uniquement par la description."),
    ("tags", "fr"): ("Donne de 10 à 20 mots-clés en français qui décrivent cette image (sujet, objets, décor, style, couleurs), "
                     "séparés par des virgules. Réponds uniquement par la liste."),
    ("sentence", "en"): "Describe this image in one sentence. Reply with the sentence only.",
    ("detailed", "en"): ("Describe this image in one paragraph of four to six sentences: main subject, setting, colours, "
                         "lighting, framing and style. Reply with the description only."),
    ("tags", "en"): ("List 10 to 20 comma-separated keywords describing this image (subject, objects, setting, style, colours). "
                     "Reply with the list only."),
}
IMAGE_LAB_CAPTION_LENGTHS = {"sentence": 120, "detailed": 400, "tags": 160}


def _image_lab_graphs():
    """{outil: (nœuds, bornes, champ de fichier)} — nœuds : {id: (classe, {entrée: valeur fixe | _FREE})}."""
    source = ("LoadImage", {"image": SOURCE_IMAGE_NAME})

    def save(reference):
        return ("SaveImage", {"filename_prefix": _FREE, "images": reference})

    graphs = {
        # Agrandissement : le modèle fixe le facteur (×4) ; « scale_by » ramène le résultat à ×1 … ×4.
        "upscale": ({
            "1": ("UpscaleModelLoader", {"model_name": _FREE}),
            "30": source,
            "40": ("ImageUpscaleWithModel", {"upscale_model": ["1", 0], "image": ["30", 0]}),
            "41": ("ImageScaleBy", {"image": ["40", 0], "upscale_method": "lanczos", "scale_by": _FREE}),
            "7": save(["41", 0]),
        }, {"41": {"scale_by": (float, 0.25, 1.0)}}, ("1", "model_name")),
        # Contours : nœud Canny natif, aucun modèle.
        "control_canny": ({
            "30": source,
            "40": ("Canny", {"image": ["30", 0], "low_threshold": _FREE, "high_threshold": _FREE}),
            "7": save(["40", 0]),
        }, {"40": {"low_threshold": (float, 0.01, 0.99), "high_threshold": (float, 0.01, 0.99)}}, None),
        # Profondeur : Depth Anything 3 en vue unique, rendu en niveaux de gris normalisés.
        "control_depth": ({
            "1": ("LoadDA3Model", {"model_name": _FREE, "weight_dtype": "default"}),
            "30": source,
            "40": ("DA3Inference", {"da3_model": ["1", 0], "image": ["30", 0], "resolution": _FREE,
                                    "resize_method": "upper_bound_resize", "mode": "mono"}),
            "41": ("DA3Render", {"da3_geometry": ["40", 0], "output": "depth", "output.normalization": "v2_style",
                                 "output.apply_sky_clip": False}),
            "7": save(["41", 0]),
        }, {"40": {"resolution": (int, 140, 2520)}}, ("1", "model_name")),
        # Pose : SDPose (points au format OpenPose) puis dessin du squelette.
        "control_pose": ({
            "1": ("CheckpointLoaderSimple", {"ckpt_name": _FREE}),
            "30": source,
            "40": ("SDPoseKeypointExtractor", {"model": ["1", 0], "vae": ["1", 2], "image": ["30", 0], "batch_size": 16}),
            "41": ("SDPoseDrawKeypoints", {"keypoints": ["40", 0], "draw_body": True, "draw_hands": _FREE, "draw_face": _FREE,
                                           "draw_feet": False, "stick_width": _FREE, "face_point_size": 3,
                                           "score_threshold": _FREE, "draw_head": True}),
            "7": save(["41", 0]),
        }, {"41": {"draw_hands": (bool, None, None), "draw_face": (bool, None, None), "stick_width": (int, 1, 10),
                   "score_threshold": (float, 0.0, 1.0)}}, ("1", "ckpt_name")),
        # Détourage : BiRefNet rend un masque du sujet ; JoinImageWithAlpha inverse son entrée, d'où InvertMask.
        "remove_background": ({
            "1": ("LoadBackgroundRemovalModel", {"bg_removal_name": _FREE}),
            "30": source,
            "40": ("RemoveBackground", {"bg_removal_model": ["1", 0], "image": ["30", 0]}),
            "41": ("InvertMask", {"mask": ["40", 0]}),
            "42": ("JoinImageWithAlpha", {"image": ["30", 0], "alpha": ["41", 0]}),
            "7": save(["42", 0]),
        }, {}, ("1", "bg_removal_name")),
        # Description : encodeur de texte Qwen3.5 (reconnu par ComfyUI à son contenu, quel que soit « type »), image
        # ramenée à 1 Mpx pour borner la mémoire, génération sans échantillonnage, texte rendu par PreviewAny.
        "caption": ({
            "1": ("CLIPLoader", {"clip_name": _FREE, "type": "stable_diffusion", "device": "default"}),
            "30": source,
            "31": ("ImageScaleToTotalPixels", {"image": ["30", 0], "upscale_method": "area", "megapixels": 1.0, "resolution_steps": 1}),
            "40": ("TextGenerate", {"clip": ["1", 0], "prompt": _FREE, "image": ["31", 0], "max_length": _FREE,
                                    "sampling_mode": "off", "thinking": False, "use_default_template": True}),
            "7": ("PreviewAny", {"source": ["40", 0]}),
        }, {"40": {"prompt": (str, frozenset(IMAGE_LAB_CAPTION_PROMPTS.values()), None), "max_length": (int, 32, 512)}}, ("1", "clip_name")),
    }
    return graphs


IMAGE_LAB_GRAPHS = _image_lab_graphs()
IMAGE_LAB_TOOLS = tuple(IMAGE_LAB_GRAPHS)


def is_image_lab_job(parameters):
    """Un job du Labo image s'annonce par « lab_tool » ; il est alors validé contre le graphe de cet outil, jamais
    contre ceux de la génération."""
    return isinstance(parameters, dict) and "lab_tool" in parameters


def _same_fixed_value(actual, expected):
    """Égalité stricte, type compris : True n'est pas 1, ["1", 0] n'est pas ["1", 0.0]."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_same_fixed_value(a, b) for a, b in zip(actual, expected))
    return actual == expected


def validate_image_lab_parameters(parameters, *, output_prefix=None):
    """N'accepte que les graphes fixes du Labo image ; rend une copie dont le préfixe de sortie est celui du Worker."""
    if not isinstance(parameters, dict) or set(parameters) - IMAGE_LAB_PARAMETER_KEYS:
        raise ValueError("Paramètres du Labo image invalides.")
    tool = parameters.get("lab_tool")
    if not isinstance(tool, str) or tool not in IMAGE_LAB_GRAPHS:
        raise ValueError("Outil du Labo image inconnu de ce Worker.")
    nodes, bounds, model_field = IMAGE_LAB_GRAPHS[tool]
    workflow = parameters.get("workflow")
    if not isinstance(workflow, dict) or set(workflow) != set(nodes):
        raise ValueError("Workflow du Labo image non autorisé : utilise le Labo image du dashboard.")
    _validated_source_inputs(parameters)      # l'image du job : la seule image copiée pour ce job
    timeout = parameters.get("timeout_seconds")
    if timeout is not None and (type(timeout) is not int or not 60 <= timeout <= 3600):
        raise ValueError("Délai du Labo image hors limites.")
    for node_id, (class_type, inputs) in nodes.items():
        node = workflow[node_id]
        if (not isinstance(node, dict) or set(node) != {"class_type", "inputs"} or node.get("class_type") != class_type
                or not isinstance(node.get("inputs"), dict) or set(node["inputs"]) != set(inputs)):
            raise ValueError("Nœud ou entrée du workflow du Labo image non autorisé.")
        for name, expected in inputs.items():
            if expected is not _FREE and not _same_fixed_value(node["inputs"][name], expected):
                raise ValueError("Liaison du workflow du Labo image non autorisée.")
    for node_id, fields in bounds.items():
        for name, (kind, low, high) in fields.items():
            value = workflow[node_id]["inputs"][name]
            if kind is bool:
                if type(value) is not bool:
                    raise ValueError("Valeur du workflow du Labo image hors limites.")
                continue
            if kind is str:
                # Texte libre interdit : seule une des consignes fixes de la table est admise.
                if type(value) is not str or value not in low:
                    raise ValueError("Consigne du Labo image non autorisée.")
                continue
            permitted = (int, float) if kind is float else (int,)
            if type(value) not in permitted or (isinstance(value, float) and not math.isfinite(value)) or not low <= value <= high:
                raise ValueError("Valeur du workflow du Labo image hors limites.")
    if tool == "control_canny" and workflow["40"]["inputs"]["low_threshold"] > workflow["40"]["inputs"]["high_threshold"]:
        raise ValueError("Seuils de contours invalides.")
    if tool == "control_depth" and workflow["40"]["inputs"]["resolution"] % 14:
        raise ValueError("Résolution de la carte de profondeur invalide (multiple de 14 attendu).")
    clean = copy.deepcopy(parameters)
    if model_field is not None:
        node_id, name = model_field
        value = workflow[node_id]["inputs"][name]
        if not isinstance(value, str):
            raise ValueError("Modèle du Labo image non autorisé.")
        safe = safe_relative_name(value, basename=True)
        if Path(safe).suffix.lower() != ".safetensors" or safe not in IMAGE_LAB_MODEL_FILES[tool]:
            raise ValueError("Modèle du Labo image non autorisé : fichier safetensors du catalogue attendu.")
    if tool in IMAGE_LAB_TEXT_TOOLS:
        return clean  # sortie texte : aucun préfixe de fichier à réécrire
    prefix = workflow["7"]["inputs"]["filename_prefix"]
    if not isinstance(prefix, str):
        raise ValueError("Préfixe de sortie du Labo image invalide.")
    safe_relative_name(prefix)
    if output_prefix is not None:
        clean["workflow"]["7"]["inputs"]["filename_prefix"] = safe_relative_name(output_prefix)
    return clean


def validate_comfyui_job_parameters(parameters, *, output_prefix=None):
    """Aiguillage d'un job du moteur image : graphes du Labo image ou graphes de génération, jamais un mélange."""
    if is_image_lab_job(parameters):
        return validate_image_lab_parameters(parameters, output_prefix=output_prefix)
    return validate_comfyui_parameters(parameters, output_prefix=output_prefix)


# Parametric creation: the browser sends a scene, never a script. A FreeCAD
# script is arbitrary code, so the accepted shape is described here field by
# field exactly like the ComfyUI graphs above: an unknown key is a refusal, not
# an ignored extra. Dimensions are bounded to a printable envelope.
_SCENE_PRIMITIVES = {
    "box": ({"size"}, {"size": (0.1, 300.0)}),
    "cylinder": ({"radius", "height"}, {"radius": (0.05, 150.0), "height": (0.1, 300.0)}),
    "sphere": ({"radius"}, {"radius": (0.05, 150.0)}),
    "cone": ({"radius1", "radius2", "height"}, {"radius1": (0.0, 150.0), "radius2": (0.0, 150.0), "height": (0.1, 300.0)}),
    "torus": ({"radius1", "radius2"}, {"radius1": (0.1, 150.0), "radius2": (0.05, 75.0)}),
}
_SCENE_BOOLEANS = {"union", "difference", "intersection"}
_SCENE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_SCENE_MAX_NODES = 64
_SCENE_BUILD_FORMATS = {"stl", "step", "stp"}


def _scene_number(value, bounds, label):
    """One finite, in-range number. ``True`` is not 1 here: bool is not a size."""
    low, high = bounds
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"Valeur hors limites pour « {label} » : attendu un nombre entre {low:g} et {high:g} mm.")
    return float(value)


def _scene_triple(value, label):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"« {label} » attend trois nombres (X, Y, Z).")
    return value


def _scene_placement(value):
    if not isinstance(value, dict) or set(value) - {"position", "rotation"} or "position" not in value:
        raise ValueError("Placement de pièce non autorisé.")
    clean = {"position": [_scene_number(item, (-300.0, 300.0), "position") for item in _scene_triple(value["position"], "position")]}
    if "rotation" in value:
        rotation = value["rotation"]
        if not isinstance(rotation, list) or len(rotation) != 4:
            raise ValueError("Rotation de pièce non autorisée : attendu un axe (X, Y, Z) et un angle.")
        axis = [_scene_number(item, (-1.0, 1.0), "axe de rotation") for item in rotation[:3]]
        if math.sqrt(sum(item * item for item in axis)) < 1e-6:
            raise ValueError("Axe de rotation nul : impossible de tourner la pièce.")
        clean["rotation"] = axis + [_scene_number(rotation[3], (-360.0, 360.0), "angle de rotation")]
    return clean


def validate_freecad_scene(parameters):
    """Accept only the dashboard's declarative parametric scene.

    Primitives and booleans, bounded, acyclic by construction: an operand may
    only name a node defined before it, so no cycle walk is needed. Anything
    that is not described above is refused rather than ignored, which is what
    keeps a smuggled ``script``/``file``/``font`` field from ever reaching
    FreeCAD.
    """
    if not isinstance(parameters, dict) or not isinstance(parameters.get("scene"), dict):
        raise ValueError("Scène paramétrique invalide.")
    scene = parameters["scene"]
    if set(scene) - {"version", "nodes", "result", "name"} or not {"version", "nodes", "result"} <= set(scene):
        raise ValueError("Scène paramétrique invalide : champs inattendus.")
    if scene["version"] != 1 or isinstance(scene["version"], bool):
        raise ValueError("Version de scène non prise en charge.")
    nodes = scene["nodes"]
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= _SCENE_MAX_NODES:
        raise ValueError(f"Une scène contient de 1 à {_SCENE_MAX_NODES} éléments.")
    clean_nodes, defined = [], []
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("op"), str):
            raise ValueError("Élément de scène invalide.")
        operation = node["op"]
        if operation in _SCENE_PRIMITIVES:
            required, bounds = _SCENE_PRIMITIVES[operation]
        elif operation in _SCENE_BOOLEANS:
            required, bounds = {"operands"}, {}
        else:
            raise ValueError("Opération de scène non autorisée.")
        if set(node) - ({"id", "op", "placement"} | required) or not ({"id", "op"} | required) <= set(node):
            raise ValueError(f"Champs non autorisés pour l’opération « {operation} ».")
        identifier = node["id"]
        if not isinstance(identifier, str) or not _SCENE_ID.fullmatch(identifier):
            raise ValueError("Identifiant d’élément invalide.")
        if identifier in defined:
            raise ValueError("Deux éléments portent le même identifiant.")
        entry = {"id": identifier, "op": operation}
        if operation in _SCENE_BOOLEANS:
            operands = node["operands"]
            if not isinstance(operands, list) or not 2 <= len(operands) <= 8:
                raise ValueError("Une opération booléenne combine de 2 à 8 éléments.")
            if len(set(operands)) != len(operands):
                raise ValueError("Une opération booléenne ne peut pas utiliser deux fois le même élément.")
            for operand in operands:
                # Backward reference only: this is what makes the graph a DAG.
                if not isinstance(operand, str) or operand not in defined:
                    raise ValueError("Une opération booléenne ne peut utiliser qu’un élément défini avant elle.")
            entry["operands"] = list(operands)
        elif operation == "box":
            entry["size"] = [_scene_number(item, bounds["size"], "taille") for item in _scene_triple(node["size"], "taille")]
        else:
            for field, limits in bounds.items():
                entry[field] = _scene_number(node[field], limits, field)
        if operation == "cone" and entry["radius1"] == entry["radius2"] == 0.0:
            raise ValueError("Un cône a besoin d’au moins un rayon non nul.")
        if "placement" in node:
            entry["placement"] = _scene_placement(node["placement"])
        clean_nodes.append(entry)
        defined.append(identifier)
    if scene["result"] not in defined:
        raise ValueError("La scène ne désigne aucun élément final valide.")
    # Every node must feed the result: an unreachable node is dead weight the
    # engine would still build, so it is refused rather than silently dropped.
    reachable, by_id = {scene["result"]}, {entry["id"]: entry for entry in clean_nodes}
    for entry in reversed(clean_nodes):
        if entry["id"] in reachable:
            reachable.update(entry.get("operands") or [])
    if len(reachable) != len(clean_nodes):
        raise ValueError("La scène contient des éléments inutilisés : relie-les au résultat ou supprime-les.")
    clean = copy.deepcopy(parameters)
    clean["scene"] = {"version": 1, "nodes": clean_nodes, "result": scene["result"]}
    if "name" in scene:
        if not isinstance(scene["name"], str) or len(scene["name"]) > 120:
            raise ValueError("Nom de scène invalide.")
        clean["scene"]["name"] = scene["name"]
    return clean


def validate_freecad_build_name(value, default="piece.stl"):
    """The build output name, reduced to a bare file name with an allowed extension."""
    name = safe_relative_name(str(value or default), basename=True)
    suffix = Path(name).suffix.lower().lstrip(".")
    if suffix not in _SCENE_BUILD_FORMATS:
        raise ValueError("Format de sortie non autorisé pour une création paramétrique.")
    return name


def validate_server_url(value):
    value = str(value or "").strip().rstrip("/")
    if __package__:
        from .local_sandbox import validate_server
    else:
        from local_sandbox import validate_server
    validate_server(value)
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Adresse du dashboard invalide.") from error
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("Adresse du dashboard invalide.")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("HTTPS est obligatoire hors connexion locale.")
    return value


def protect_credentials(request):
    """urllib must not forward Worker credentials to redirected hosts."""
    try:
        from .identity import sign_request
    except ImportError:
        from identity import sign_request
    request = sign_request(request)
    for name, value in list(request.header_items()):
        if name.lower() in {"authorization", "x-worker-id", "x-worker-proof", "x-worker-signature"}:
            request.remove_header(name)
            request.add_unredirected_header(name, value)
    return request


def is_link(path):
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _deletion_target(path, allowed_root):
    """Resolve one deletion target without accepting the boundary itself."""
    boundary = Path(allowed_root).resolve()
    target = Path(path)
    if is_link(boundary) or is_link(target):
        raise ValueError("Suppression refusée : le chemin Worker est un lien.")
    resolved = target.resolve()
    if resolved == boundary or not resolved.is_relative_to(boundary):
        raise ValueError("Suppression refusée : le chemin sort du périmètre Worker autorisé.")
    return resolved


def _retry_readonly(function, path, error):
    """Retry only a permission failure, without following a link to its target."""
    failure = error[1] if isinstance(error, tuple) else error
    if not isinstance(failure, PermissionError) or is_link(path):
        raise failure
    try:
        info = os.stat(path, follow_symlinks=False)
        # A read-only hard link shares its attributes with the file outside the
        # Worker.  Refuse to change it; unlinking a normal writable hard link is
        # harmless, but clearing its read-only bit would mutate the other name.
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise ValueError("Suppression refusée : fichier en lecture seule partagé par lien dur.")
        if os.chmod in os.supports_follow_symlinks:
            os.chmod(path, info.st_mode | stat.S_IWRITE, follow_symlinks=False)
        else:
            # Python 3.12 on Windows does not implement follow_symlinks for
            # chmod. is_link() and the lstat above have already rejected links.
            os.chmod(path, info.st_mode | stat.S_IWRITE)
    except FileNotFoundError:
        return
    function(path)


def remove_worker_tree(path, allowed_root, *, attempts=5):
    """Remove a verified Worker tree, including read-only Git/package files."""
    target = _deletion_target(path, allowed_root)
    tries = max(1, attempts)
    for attempt in range(tries):
        try:
            shutil.rmtree(target, onerror=_retry_readonly)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt + 1 >= tries:
                raise
            # Antivirus/indexer handles can linger briefly after engine stop.
            time.sleep(0.2 * (attempt + 1))


def remove_worker_file(path, allowed_root, *, missing_ok=True, attempts=5):
    """Remove one verified Worker file with bounded read-only/lock retries."""
    target = _deletion_target(path, allowed_root)
    tries = max(1, attempts)
    for attempt in range(tries):
        try:
            target.unlink(missing_ok=missing_ok)
            return
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        except PermissionError:
            if is_link(target):
                raise
            try:
                info = os.stat(target, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                    raise ValueError("Suppression refusée : fichier en lecture seule partagé par lien dur.")
                if os.chmod in os.supports_follow_symlinks:
                    os.chmod(target, info.st_mode | stat.S_IWRITE, follow_symlinks=False)
                else:
                    os.chmod(target, info.st_mode | stat.S_IWRITE)
            except FileNotFoundError:
                if missing_ok:
                    return
                raise
            if attempt + 1 >= tries:
                target.unlink(missing_ok=missing_ok)
                return
            time.sleep(0.2 * (attempt + 1))


def validate_archive(archive, destination, max_bytes, max_files=20000):
    """Validate the entire ZIP before extracting any file, on both OSes."""
    destination = Path(destination).resolve()
    total = 0
    seen = set()
    members = archive.infolist()
    if len(members) > max_files:
        raise ValueError("Le paquet contient trop de fichiers.")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    for member in members:
        name = member.filename.replace("\\", "/")
        parts = name.rstrip("/").split("/")
        if (not name or name.startswith("/") or PureWindowsPath(name).drive
                or any(not part or part in {".", ".."} or ":" in part or "\0" in part
                       or part.endswith((" ", ".")) or part.split(".")[0].casefold() in reserved for part in parts)):
            raise ValueError("Le paquet contient un chemin non autorisé.")
        folded = "/".join(parts).casefold()
        if folded in seen:
            raise ValueError("Le paquet contient des chemins en double.")
        seen.add(folded)
        if stat.S_ISLNK(member.external_attr >> 16):
            raise ValueError("Les liens symboliques ne sont pas autorisés dans le paquet.")
        total += max(0, int(member.file_size))
        if total > max_bytes:
            raise ValueError("Le contenu décompressé dépasse la limite autorisée.")
        target = (destination / Path(*parts)).resolve()
        if destination not in target.parents:
            raise ValueError("Le paquet contient un chemin non autorisé.")
    return members
