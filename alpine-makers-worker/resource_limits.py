# -*- coding: utf-8 -*-
"""Jobs et ressources d'un Worker (capacité ``resource_limits_v1``).

Réglages, dont le site est la source de vérité (``workers.max_concurrent_jobs`` et ``workers.resource_limits_json``) ;
l'agent en garde une copie (``runtime/resource-limits.json``) pour les appliquer même hors ligne :

- jobs simultanés au maximum (1 à 16) ;
- limites PAR JOB et limites CUMULÉES sur tous les jobs en cours, pour la VRAM, la RAM et le processeur.
  VRAM et RAM en Gio (``{"unit": "gib", "value": 6}``) ou en % du total détecté (``{"unit": "percent", "value": 50}``),
  processeur en % de tout le processeur (``{"unit": "percent", "value": 50}``). ``None`` = pas de limite.

Validation : par job ≤ cumulé ≤ matériel détecté ; tout réglage hors de ces bornes est refusé avec sa raison.

Ce qui est un plafond dur et ce qui est une admission :

- plafond dur (Windows, groupes de processus imbriqués, job_limits.py) : RAM engagée et part du processeur des
  processus que l'agent lance pour un job (FreeCAD, réparation de maillage, Labo image, laser, CNC, et le client de
  chaque job), par job et cumulés ;
- admission (le site à la répartition, l'agent avant le lancement) : VRAM et RAM estimées de chaque job. Au-delà de la
  limite par job, il est refusé ; au-delà du cumul, il attend qu'une place se libère, sans bloquer les autres ;
- réglages des moteurs lancés par l'agent : ComfyUI ``--reserve-vram`` (total − limite), PyTorch
  ``set_per_process_memory_fraction`` (Hunyuan3D, Labo image), threads (OMP, MKL, torch) alignés sur la part du
  processeur. Un moteur déjà lancé les garde jusqu'à son redémarrage : signalé, jamais forcé pendant un job.

La première partie (réglages, estimation, admission, réglages des moteurs) est commune et n'importe rien de l'agent :
le site l'importe. La seconde (``ResourceRuntime``, actions locales) ne sert qu'à l'agent.
"""
import hashlib
import json
import math
import os
import re
import subprocess
import threading
import time
from pathlib import Path

if __package__:
    from . import host_stability
    from .safety import IMAGE_LAB_MAPS_TOOLS
else:
    import host_stability
    from safety import IMAGE_LAB_MAPS_TOOLS

CAPABILITY = "resource_limits_v1"
RESOURCES = ("vram", "ram", "cpu")
SCOPES = ("per_job", "total")
MEMORY_UNITS = ("gib", "percent")
MAX_CONCURRENT_JOBS = 16
GIB_MIN = 0.25
GIB_MAX = 4096
MB_MIN = 256
LABELS = {"vram": "VRAM", "ram": "RAM", "cpu": "processeur"}
SCOPE_LABELS = {"per_job": "par job", "total": "cumulée"}
KEYS = {"vram": "vram_mb", "ram": "ram_mb", "cpu": "cpu_percent"}
_REVISION = re.compile(r"[0-9a-f]{16}")

# Explication courte reprise par le site, worker.exe et le menu (mêmes mots partout).
EXPLANATION = {
    "hard": ("Plafond dur (Windows) : RAM et processeur des calculs lancés à chaque job (FreeCAD, réparation de maillage, "
             "Labo image, laser, CNC), par job et cumulés."),
    "admission": ("Admission : la VRAM, et la RAM des moteurs résidents (ComfyUI, Hunyuan3D, OrcaSlicer), sont estimées "
                  "avant chaque job ; au-delà de la limite par job il est refusé, au-delà du cumul il attend son tour."),
    "engines": ("Moteurs : ComfyUI réserve le reste de la VRAM (--reserve-vram), PyTorch reçoit une fraction de la carte, "
                "les threads suivent la part du processeur ; un moteur déjà lancé doit être redémarré pour les appliquer."),
}


class ResourceLimitError(ValueError):
    """Réglage refusé : le message est montré tel quel."""


# --------------------------------------------------------------------------------------------------- réglages

def empty_limits():
    return {scope: {name: None for name in RESOURCES} for scope in SCOPES}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _positive_int(value):
    number = _number(value)
    return int(number) if number is not None and number > 0 and number < 1 << 53 else None


def gib_text(mb):
    """« 8,5 Gio », « 16 Gio », « 0,25 Gio »."""
    value = float(mb or 0) / 1024
    text = f"{value:.{2 if value < 1 else 1}f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") + " Gio"


def percent_text(value):
    return f"{float(value):g}".replace(".", ",") + " %"


def limit_text(name, value):
    return percent_text(value) if name == "cpu" else gib_text(value)


def normalize_limits(value):
    """Forme enregistrée des limites, stricte : ``{per_job: {vram, ram, cpu}, total: {...}}``, chaque limite ``None``
    ou ``{"unit", "value"}``. Lève ResourceLimitError avec la raison."""
    if value is None:
        return empty_limits()
    if not isinstance(value, dict):
        raise ResourceLimitError("Limites de ressources invalides : un objet {per_job, total} est attendu.")
    unknown = sorted(str(key)[:40] for key in set(value) - set(SCOPES))
    if unknown:
        raise ResourceLimitError(f"Limites de ressources : champ inconnu « {unknown[0]} » (per_job ou total attendus).")
    result = empty_limits()
    for scope in SCOPES:
        block = value.get(scope)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ResourceLimitError(f"Limites {SCOPE_LABELS[scope]} invalides : un objet {{vram, ram, cpu}} est attendu.")
        unknown = sorted(str(key)[:40] for key in set(block) - set(RESOURCES))
        if unknown:
            raise ResourceLimitError(f"Limites {SCOPE_LABELS[scope]} : ressource inconnue « {unknown[0]} » (vram, ram ou cpu).")
        for name in RESOURCES:
            item = block.get(name)
            if item is None:
                continue
            label = f"Limite {LABELS[name]} {SCOPE_LABELS[scope]}"
            if not isinstance(item, dict) or set(item) != {"unit", "value"}:
                raise ResourceLimitError(f"{label} invalide : {{unit, value}} attendu.")
            unit = item["unit"]
            allowed = ("percent",) if name == "cpu" else MEMORY_UNITS
            if unit not in allowed:
                raise ResourceLimitError(f"{label} : unité « {str(unit)[:20]} » inconnue (" + " ou ".join(
                    "Gio" if choice == "gib" else "%" for choice in allowed) + ").")
            number = _number(item["value"])
            if number is None:
                raise ResourceLimitError(f"{label} : nombre attendu.")
            if unit == "percent":
                if not 1 <= number <= 100:
                    raise ResourceLimitError(f"{label} : pourcentage entre 1 et 100.")
                number = round(float(number), 1)
            else:
                if not GIB_MIN <= number <= GIB_MAX:
                    raise ResourceLimitError(f"{label} : entre {GIB_MIN:g} et {GIB_MAX} Gio.".replace(".", ","))
                number = round(float(number), 2)
            result[scope][name] = {"unit": unit, "value": number}
    return result


def tolerant_limits(value):
    """Limites enregistrées relues sans lever (valeur abîmée = aucune limite)."""
    try:
        return normalize_limits(value)
    except ResourceLimitError:
        return empty_limits()


def limits_set(stored):
    return any(stored.get(scope, {}).get(name) for scope in SCOPES for name in RESOURCES) if isinstance(stored, dict) else False


def hardware(gpus=None, ram=None, cpu=None):
    """Matériel détecté, tel que les limites s'y rapportent : VRAM de la plus grande carte (jamais additionnée),
    RAM totale, cœurs logiques."""
    vram, name = None, ""
    for gpu in gpus if isinstance(gpus, list) else []:
        total = _positive_int(gpu.get("vram_total_mb")) if isinstance(gpu, dict) else None
        if total and (vram is None or total > vram):
            vram, name = total, str(gpu.get("name") or "")[:80]
    ram = ram if isinstance(ram, dict) else {}
    cpu = cpu if isinstance(cpu, dict) else {}
    return {"vram_total_mb": vram, "ram_total_mb": _positive_int(ram.get("total_mb")),
            "cpu_cores": _positive_int(cpu.get("cores")), "gpu_name": name}


def _memory_mb(item, total_mb):
    if not item:
        return None
    if item["unit"] == "gib":
        return int(round(item["value"] * 1024))
    if not total_mb:
        return None
    return int(total_mb * item["value"] / 100)


def effective_limits(stored, hw):
    """Limites en Mio et en % du processeur, calculées sur le matériel détecté ; ``None`` = pas de limite."""
    stored = tolerant_limits(stored)
    hw = hw if isinstance(hw, dict) else {}
    result = {}
    for scope in SCOPES:
        block = stored[scope]
        result[scope] = {"vram_mb": _memory_mb(block["vram"], hw.get("vram_total_mb")),
                         "ram_mb": _memory_mb(block["ram"], hw.get("ram_total_mb")),
                         "cpu_percent": block["cpu"]["value"] if block["cpu"] else None}
    return result


def validate_limits(value, hw):
    """Réglage demandé → forme enregistrée, ou ResourceLimitError : par job ≤ cumulé ≤ matériel détecté."""
    stored = normalize_limits(value)
    hw = hw if isinstance(hw, dict) else {}
    totals = {"vram": hw.get("vram_total_mb"), "ram": hw.get("ram_total_mb")}
    for scope in SCOPES:
        for name in ("vram", "ram"):
            item = stored[scope][name]
            if not item:
                continue
            total = totals[name]
            label = f"limite {LABELS[name]} {SCOPE_LABELS[scope]}"
            if not total:
                raise ResourceLimitError(f"{LABELS[name]} de ce Worker non détectée : impossible de régler une {label} "
                                         "(actualise l'inventaire du Worker, ou laisse le champ vide).")
            wanted = _memory_mb(item, total)
            if wanted > total + 1:
                raise ResourceLimitError(f"{label[0].upper()}{label[1:]} de {gib_text(wanted)} : plus que les "
                                         f"{gib_text(total)} détectés sur ce Worker.")
            if wanted < MB_MIN:
                raise ResourceLimitError(f"{label[0].upper()}{label[1:]} trop basse ({gib_text(wanted)}) : au moins 0,25 Gio.")
    effective = effective_limits(stored, hw)
    for name in RESOURCES:
        key = KEYS[name]
        per, total = effective["per_job"][key], effective["total"][key]
        if per is not None and total is not None and per > total:
            raise ResourceLimitError(f"Limite {LABELS[name]} par job ({limit_text(name, per)}) supérieure à la limite "
                                     f"cumulée ({limit_text(name, total)}) : un job seul ne peut pas dépasser le cumul.")
    return stored


def validate_max_jobs(value):
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        value = int(value)
    if type(value) is not int or not 1 <= value <= MAX_CONCURRENT_JOBS:
        raise ResourceLimitError(f"Jobs simultanés : entier entre 1 et {MAX_CONCURRENT_JOBS}.")
    return value


def settings_revision(max_jobs, stored):
    text = json.dumps([int(max_jobs or 1), tolerant_limits(stored)], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def settings_message(max_jobs, stored):
    """Réglages tels que le site les remet à l'agent (réponse du heartbeat)."""
    stored = tolerant_limits(stored)
    return {"revision": settings_revision(max_jobs, stored), "max_concurrent_jobs": int(max_jobs or 1), "limits": stored}


def settings_summary(max_jobs, stored, hw):
    """Une phrase pour le journal et les notifications : « 2 jobs simultanés ; par job : VRAM 6 Gio… »."""
    effective = effective_limits(stored, hw)
    parts = [f"{int(max_jobs or 1)} job(s) simultané(s)"]
    for scope in SCOPES:
        items = [f"{LABELS[name]} {limit_text(name, effective[scope][KEYS[name]])}"
                 for name in RESOURCES if effective[scope][KEYS[name]] is not None]
        parts.append(f"{SCOPE_LABELS[scope].replace('cumulée', 'cumulé')} : " + (", ".join(items) if items else "sans limite"))
    return " ; ".join(parts)


# --------------------------------------------------------------------------------------------------- estimation

ENGINE_KINDS = ("comfyui", "hunyuan3d", "orca")
IN_TREE_KINDS = ("freecad", "mesh_repair", "face_restore", "image_maps", "laser-engine", "cnc-engine", "toile-engine")
# Besoins prudents par type de calcul, en Mio : (VRAM, RAM).
DEFAULT_NEEDS = {"comfyui": (6144, 8192), "hunyuan3d": (12288, 12288), "orca": (0, 4096), "freecad": (0, 3072),
                 "mesh_repair": (0, 2048), "face_restore": (1536, 3072), "image_maps": (0, 2048),
                 "laser-engine": (0, 1536), "cnc-engine": (0, 1536),
                 "toile-engine": (0, 3072), "other": (0, 2048)}
# Avec la taille du modèle : (VRAM ajoutée, RAM ajoutée, VRAM minimale, RAM minimale), en Mio.
MODEL_NEEDS = {"comfyui": (1536, 2048, 2048, 4096), "hunyuan3d": (3072, 4096, 6144, 8192)}
TEXTURE_VRAM_MB = 12288
MEASURE_MARGIN = 1.1
PROFILE_SAMPLES = 5
PROFILES_MAX = 128
BASIS_LABELS = {"default": "valeur prudente de l'outil", "model": "taille du modèle", "measured": "mesure des jobs passés"}


def job_kind(tool_id, parameters=None):
    """Type de calcul d'un job, comme l'agent choisit son lanceur (runners/<type>.py)."""
    parameters = parameters if isinstance(parameters, dict) else {}
    tool_id = str(tool_id or "")
    if tool_id in {"image_generation", "image-generation"}:
        lab = str(parameters.get("lab_tool") or "")
        if lab == "face_restore":
            return "face_restore"
        if lab in IMAGE_LAB_MAPS_TOOLS:
            return "image_maps"
        return "comfyui"
    if tool_id in {"ai3d", "3d-generation"}:
        return "hunyuan3d"
    if tool_id == "converter":
        return "mesh_repair" if parameters.get("task") == "mesh_repair" else "freecad"
    return {"model-studio": "orca", "laser-studio": "laser-engine", "cnc-studio": "cnc-engine",
            "toile-studio": "toile-engine"}.get(tool_id, "other")


def requests_texture(parameters):
    task = parameters.get("task") if isinstance(parameters, dict) else None
    inner = task.get("parameters") if isinstance(task, dict) else None
    return isinstance(inner, dict) and inner.get("texture") is True


def profile_key(kind, model_id=""):
    return (str(kind) + ":" + str(model_id or ""))[:120]


def estimate_job(tool_id, parameters=None, model=None, measured=None):
    """Besoins estimés d'un job : ``{"kind", "vram_mb", "ram_mb", "basis"}``.

    Ordre : mesure des jobs passés (pic des derniers jobs + 10 %, jamais sous les poids du modèle), sinon taille du
    modèle installé (poids + marge), sinon valeur prudente de l'outil."""
    kind = job_kind(tool_id, parameters)
    vram, ram = DEFAULT_NEEDS.get(kind, DEFAULT_NEEDS["other"])
    basis, weights = "default", 0
    if kind in MODEL_NEEDS and isinstance(model, dict):
        size = _positive_int(model.get("size_bytes"))
        if size:
            weights = size // (1024 * 1024)
            add_vram, add_ram, floor_vram, floor_ram = MODEL_NEEDS[kind]
            minimum = _positive_int(model.get("min_vram_mb")) or 0 if kind == "hunyuan3d" else 0
            vram = max(floor_vram, weights + add_vram, minimum)
            ram = max(floor_ram, weights + add_ram)
            basis = "model"
    if kind == "hunyuan3d" and requests_texture(parameters):
        vram = max(vram, TEXTURE_VRAM_MB)
    if isinstance(measured, dict) and _positive_int(measured.get("samples")):
        peak_vram, peak_ram = _positive_int(measured.get("vram_mb")), _positive_int(measured.get("ram_mb"))
        if peak_vram and vram:
            vram, basis = max(int(peak_vram * MEASURE_MARGIN), weights), "measured"
        if peak_ram and kind in IN_TREE_KINDS:
            ram, basis = max(512, int(peak_ram * MEASURE_MARGIN)), "measured"
    return {"kind": kind, "vram_mb": int(vram), "ram_mb": int(ram), "basis": basis}


def admission(estimate, effective, reserved=None, running=0, max_jobs=None):
    """Verdict pour un job : ``{"verdict": "ok" | "refuse" | "wait", "message", "resource"}``.

    ``refuse`` : le job dépasse à lui seul une limite (par job, ou le cumul entier) ; ``wait`` : le cumul des jobs en
    cours (``reserved``) ou le nombre de jobs (``running`` / ``max_jobs``) est atteint."""
    reserved = reserved if isinstance(reserved, dict) else {}
    for name in ("vram", "ram"):
        key, need = KEYS[name], int(estimate.get(KEYS[name]) or 0)
        if need <= 0:
            continue
        per, total = effective["per_job"][key], effective["total"][key]
        if per is not None and need > per:
            return {"verdict": "refuse", "resource": name,
                    "message": f"ce job demande ~{gib_text(need)} de {LABELS[name]}, au-delà de la limite par job de {gib_text(per)}"}
        if total is not None and need > total:
            return {"verdict": "refuse", "resource": name,
                    "message": f"ce job demande ~{gib_text(need)} de {LABELS[name]}, au-delà de la limite cumulée de {gib_text(total)}"}
    if max_jobs and running >= max_jobs:
        return {"verdict": "wait", "resource": "jobs",
                "message": f"{running} job(s) en cours sur {max_jobs} au maximum : il démarre dès qu'un job se termine"}
    for name in ("vram", "ram"):
        key, need = KEYS[name], int(estimate.get(KEYS[name]) or 0)
        total, used = effective["total"][key], int(reserved.get(key) or 0)
        if need > 0 and total is not None and used + need > total:
            return {"verdict": "wait", "resource": name,
                    "message": (f"ce job demande ~{gib_text(need)} de {LABELS[name]} ; {gib_text(used)} déjà réservés par les "
                                f"jobs en cours, limite cumulée {gib_text(total)} : il démarre dès qu'une place se libère")}
    return {"verdict": "ok", "resource": "", "message": ""}


# --------------------------------------------------------------------------------------------------- moteurs

ENGINE_TOOLS = ("image_generation", "ai3d")
ENGINE_NAMES = {"image_generation": "ComfyUI", "ai3d": "Hunyuan3D"}
THREAD_VARIABLES = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_MAX_THREADS", "ALPINE_TORCH_THREADS")


def cpu_share(effective):
    """Part du processeur d'UN job : la limite par job, sinon le cumul."""
    per, total = effective["per_job"]["cpu_percent"], effective["total"]["cpu_percent"]
    return per if per is not None else total


def thread_count(effective, cores):
    share = cpu_share(effective)
    if share is None or not cores:
        return None
    return max(1, int(cores * share / 100))


def vram_share(effective):
    """VRAM d'UN job : la plus petite des limites par job et cumulée."""
    values = [value for value in (effective["per_job"]["vram_mb"], effective["total"]["vram_mb"]) if value]
    return min(values) if values else None


def engine_settings(tool_id, effective, vram_total_mb, cores):
    """Ce que le moteur ``tool_id`` reçoit à son démarrage : ``vram_limit_mb``, ``reserve_vram_gb`` (ComfyUI),
    ``torch_fraction`` (Hunyuan3D), ``threads``. Tout à ``None`` : moteur lancé comme avant."""
    settings = {"vram_limit_mb": None, "reserve_vram_gb": None, "torch_fraction": None, "threads": None}
    if tool_id not in ENGINE_TOOLS:
        return settings
    settings["threads"] = thread_count(effective, cores)
    limit = vram_share(effective)
    total = _positive_int(vram_total_mb)
    if limit and total and limit < total:
        settings["vram_limit_mb"] = int(limit)
        if tool_id == "image_generation":
            settings["reserve_vram_gb"] = round((total - limit) / 1024, 2)
        else:
            settings["torch_fraction"] = round(max(0.05, min(1.0, limit / total)), 3)
    return settings


def comfyui_arguments(settings):
    reserve = (settings or {}).get("reserve_vram_gb")
    return ["--reserve-vram", f"{float(reserve):.2f}"] if reserve else []


def thread_environment(threads):
    return {name: str(int(threads)) for name in THREAD_VARIABLES} if threads else {}


def torch_environment(fraction):
    return {"ALPINE_TORCH_VRAM_FRACTION": f"{float(fraction):.3f}"} if fraction else {}


def settings_differ(applied, wanted):
    """Le moteur lancé avec ``applied`` doit-il redémarrer pour appliquer ``wanted`` ? (champs absents = aucun)"""
    keys = ("vram_limit_mb", "reserve_vram_gb", "torch_fraction", "threads")
    applied = applied if isinstance(applied, dict) else {}
    wanted = wanted if isinstance(wanted, dict) else {}
    return any(applied.get(key) != wanted.get(key) for key in keys)


# =================================================================================================== agent

SETTINGS_FILE = "runtime/resource-limits.json"
PROFILES_FILE = "runtime/resource-profiles.json"
SAMPLE_SECONDS = 20
VRAM_BUSY_SECONDS = 20
VRAM_IDLE_SECONDS = 120
WAIT_RECHECK_SECONDS = 5
AGING_SECONDS = 600


class ResourceRefusal(RuntimeError):
    """Job refusé par l'admission : le message est rendu au demandeur."""


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def system_memory_mb():
    """(total, utilisée) en Mio, ou (None, None)."""
    try:
        if os.name == "nt":
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            status = Status()
            status.dwLength = ctypes.sizeof(Status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None, None
            total, free = int(status.ullTotalPhys) // 1048576, int(status.ullAvailPhys) // 1048576
            return total, total - free
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) // 1024
        total = values.get("MemTotal")
        return total, (total - values.get("MemAvailable", 0)) if total else None
    except Exception:  # noqa: BLE001 - mesure indisponible : rien n'est inventé
        return None, None


class _CpuMeter:
    """Charge de tout le processeur entre deux relevés (%)."""

    def __init__(self):
        self.previous = None

    @staticmethod
    def _times():
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                return None
            value = lambda item: (item.dwHighDateTime << 32) | item.dwLowDateTime  # noqa: E731
            return value(idle), value(kernel) + value(user)   # le temps noyau Windows comprend le temps libre
        fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        numbers = [int(item) for item in fields]
        return numbers[3] + (numbers[4] if len(numbers) > 4 else 0), sum(numbers)

    def percent(self):
        try:
            current = self._times()
        except Exception:  # noqa: BLE001
            return None
        previous, self.previous = self.previous, current
        if not current or not previous or current[1] <= previous[1]:
            return None
        busy = (current[1] - previous[1]) - (current[0] - previous[0])
        return round(max(0.0, min(100.0, 100.0 * busy / (current[1] - previous[1]))), 1)


def gpu_process_memory_mb(timeout=15):
    """VRAM dédiée par processus (Windows, compteurs « GPU Process Memory ») : {pid: Mio}, ou None si illisible.

    nvidia-smi --query-compute-apps rend [N/A] sous WDDM : seuls ces compteurs donnent la VRAM de chaque processus.
    Lus par PDH en noms anglais (host_stability) : sans PowerShell, et valables sur un Windows en français, où
    « Get-Counter '\\GPU Process Memory…' » échoue faute de compteur à ce nom. ``timeout`` est gardé pour les appelants."""
    del timeout
    return host_stability.gpu_process_memory_mb()


class ResourceRuntime:
    """Côté agent : copie des réglages, admission et file locale, plafonds des jobs, réglages des moteurs, mesures."""

    def __init__(self, agent):
        self.agent = agent
        self.lock = threading.Condition(threading.RLock())
        self.reservations = {}     # job_id -> {"kind", "vram_mb", "ram_mb", "since", "model_id", "tool_id"}
        self.waiting = {}          # job_id -> {"since", "message", "vram_mb", "ram_mb"}
        self.scopes = {}           # job_id -> job_limits.JobScope
        self.peaks = {}            # job_id -> {"vram_mb", "ram_mb"} relevés pendant le job
        self.parent = None
        self.parent_error = ""
        self.hard_caps = {"state": "none", "message": ""}
        self.usage = {}
        self.engines = []
        self.vram_by_pid = None
        self.vram_measured_at = 0.0
        self.cpu = _CpuMeter()
        self.sampler = None
        # Stabilité du PC hôte (host_stability_v1) : journal des ressources et déchargement des moteurs inactifs.
        self.journal = host_stability.ResourceJournal(getattr(agent, "root", "."))
        self.relief = host_stability.MemoryRelief(agent)
        self.settings = self._load_settings()
        self.profiles = self._load_profiles()

    # ------------------------------------------------------------------ réglages

    def _worker_id(self):
        config = getattr(self.agent, "config", None)
        return str((config or {}).get("worker_id") or "") if isinstance(config, dict) else ""

    def _load_settings(self):
        value = _read_json(Path(self.agent.root) / SETTINGS_FILE)
        if not value or value.get("worker_id") != self._worker_id() or not _REVISION.fullmatch(str(value.get("revision") or "")):
            return {"known": False, "revision": "", "max_concurrent_jobs": None, "limits": empty_limits(), "received_at": None}
        try:
            max_jobs = validate_max_jobs(value.get("max_concurrent_jobs"))
        except ResourceLimitError:
            max_jobs = None
        return {"known": True, "revision": value["revision"], "max_concurrent_jobs": max_jobs,
                "limits": tolerant_limits(value.get("limits")), "received_at": value.get("received_at")}

    def apply_site_settings(self, value):
        """Réglages reçus du site (réponse du heartbeat) : gardés en cache si leur révision change. Rend True s'ils changent."""
        if not isinstance(value, dict):
            return False
        revision = str(value.get("revision") or "")
        try:
            max_jobs = validate_max_jobs(value.get("max_concurrent_jobs"))
            limits = normalize_limits(value.get("limits"))
        except ResourceLimitError:
            return False
        if not _REVISION.fullmatch(revision) or revision != settings_revision(max_jobs, limits):
            return False
        with self.lock:
            if self.settings.get("known") and self.settings.get("revision") == revision:
                return False
            settings = {"known": True, "revision": revision, "max_concurrent_jobs": max_jobs, "limits": limits,
                        "received_at": time.time()}
            try:
                _atomic_json(Path(self.agent.root) / SETTINGS_FILE, {**settings, "worker_id": self._worker_id()})
            except OSError:
                pass   # la copie en mémoire s'applique quand même ; le cache sera réécrit au prochain changement
            self.settings = settings
            self.lock.notify_all()   # les jobs en attente réévaluent leur place
        self._apply_parent_limits()
        return True

    def hardware(self):
        lock = getattr(self.agent, "inventory_lock", None)
        snapshot = {}
        if lock is not None:
            with lock:
                snapshot = dict(getattr(self.agent, "inventory_snapshot", None) or {})
        total, _used = system_memory_mb()
        ram = {"total_mb": total} if total else snapshot.get("ram")
        return hardware(snapshot.get("gpus"), ram, {"cores": os.cpu_count() or (snapshot.get("cpu") or {}).get("cores")})

    def effective(self):
        return effective_limits(self.settings.get("limits"), self.hardware())

    # ------------------------------------------------------------------ admission et file locale

    def _model_info(self, tool_id, model_id):
        if not model_id:
            return None
        lock = getattr(self.agent, "inventory_lock", None)
        if lock is None:
            return None
        with lock:
            models = list((getattr(self.agent, "inventory_snapshot", None) or {}).get("models") or [])
        tools = {"image_generation", "image-generation"} if tool_id in {"image_generation", "image-generation"} else {tool_id}
        return next((item for item in models if isinstance(item, dict) and item.get("tool_id") in tools
                     and item.get("model_id") == model_id), None)

    def estimate(self, tool_id, model_id, parameters):
        kind = job_kind(tool_id, parameters)
        return estimate_job(tool_id, parameters, self._model_info(tool_id, model_id), self.measured(kind, model_id))

    def _reserved(self, exclude=""):
        totals = {"vram_mb": 0, "ram_mb": 0}
        for job_id, item in self.reservations.items():
            if job_id != exclude:
                totals["vram_mb"] += int(item.get("vram_mb") or 0)
                totals["ram_mb"] += int(item.get("ram_mb") or 0)
        return totals

    def _older_waiting(self, job_id):
        """Un job qui attend depuis plus de AGING_SECONDS passe avant les plus récents : jamais de famine."""
        mine = self.waiting.get(job_id, {}).get("since", time.time())
        return any(other != job_id and item["since"] < mine and time.time() - item["since"] > AGING_SECONDS
                   for other, item in self.waiting.items())

    def admit(self, job_id, tool_id, model_id, parameters, cancelled, report):
        """Admission avant le lancement : rend l'estimation réservée, ``None`` si le job est annulé pendant l'attente,
        ou lève ResourceRefusal. ``report(message)`` est appelé à chaque nouveau motif d'attente."""
        estimate = {**self.estimate(tool_id, model_id, parameters), "tool_id": str(tool_id), "model_id": str(model_id or "")}
        reported = None
        while True:
            with self.lock:
                if cancelled():
                    self.waiting.pop(job_id, None)
                    self.lock.notify_all()
                    return None
                if job_id in self.reservations:
                    return self.reservations[job_id]
                verdict = admission(estimate, self.effective(), self._reserved(job_id), len(self.reservations),
                                    self.settings.get("max_concurrent_jobs"))
                if verdict["verdict"] == "ok" and self._older_waiting(job_id):
                    verdict = {"verdict": "wait", "resource": "", "message": "un job plus ancien attend ces ressources : il passe avant"}
                if verdict["verdict"] == "ok":
                    self.waiting.pop(job_id, None)
                    self.reservations[job_id] = {**estimate, "since": time.time()}
                    return self.reservations[job_id]
                if verdict["verdict"] == "refuse":
                    self.waiting.pop(job_id, None)
                    raise ResourceRefusal("Refusé par le Worker : " + verdict["message"] + " (Mes Workers → Jobs et ressources).")
                waiting = self.waiting.setdefault(job_id, {"since": time.time()})
                waiting.update(message=verdict["message"], vram_mb=estimate["vram_mb"], ram_mb=estimate["ram_mb"])
            if verdict["message"] != reported:
                reported = verdict["message"]
                try:
                    report("En file sur le Worker : " + reported + ".")
                except Exception:  # noqa: BLE001 - un compte rendu perdu ne bloque jamais l'attente
                    pass
            stop = getattr(self.agent, "stop_requested", None)
            if stop is not None and stop.is_set():
                with self.lock:
                    self.waiting.pop(job_id, None)
                return None
            with self.lock:
                self.lock.wait(WAIT_RECHECK_SECONDS)

    def register(self, job_id, tool_id, model_id, parameters):
        """Job déjà lancé (reprise après un redémarrage de l'agent) : sa place est comptée, sans admission."""
        with self.lock:
            if job_id not in self.reservations:
                estimate = self.estimate(tool_id, model_id, parameters)
                self.reservations[job_id] = {**estimate, "tool_id": str(tool_id), "model_id": str(model_id or ""), "since": time.time()}

    def release(self, job_id):
        with self.lock:
            self.reservations.pop(job_id, None)
            self.waiting.pop(job_id, None)
            self.lock.notify_all()

    # ------------------------------------------------------------------ plafonds durs (Windows)

    def _job_limits(self):
        if __package__:
            from . import job_limits
        else:
            import job_limits
        return job_limits

    def _apply_parent_limits(self):
        effective = self.effective()
        with self.lock:
            parent = self.parent
        if parent is None:
            return
        try:
            parent.apply(effective["total"]["ram_mb"], effective["total"]["cpu_percent"])
            self.parent_error = ""
        except OSError as error:
            self.parent_error = str(error)[:200]

    def job_scope(self, job_id, execution_id):
        """Groupe Windows du job avec ses plafonds par job et cumulés, ou None si aucun plafond RAM/processeur n'est
        réglé (ou hors Windows : admission seulement). Lève OSError si Windows refuse : le job n'est pas lancé."""
        effective = self.effective()
        wanted = any(effective[scope][key] is not None for scope in SCOPES for key in ("ram_mb", "cpu_percent"))
        if not wanted:
            self.hard_caps = {"state": "none", "message": ""}
            return None
        job_limits = self._job_limits()
        if not job_limits.supported():
            self.hard_caps = {"state": "unsupported", "message": "Plafonds durs disponibles sous Windows seulement : ici, admission seule."}
            return None
        try:
            with self.lock:
                if self.parent is None:
                    self.parent = job_limits.WorkerJobs(self.agent.root)
                parent = self.parent
            parent.apply(effective["total"]["ram_mb"], effective["total"]["cpu_percent"])
            scope = job_limits.JobScope(parent, job_limits.job_name(job_id, execution_id),
                                        effective["per_job"]["ram_mb"], effective["per_job"]["cpu_percent"])
        except OSError as error:
            self.hard_caps = {"state": "unavailable", "message": "Plafonds durs refusés par Windows : " + str(error)[:160]}
            raise
        self.hard_caps = {"state": "active", "message": ""}
        return scope

    def job_environment(self, kind):
        """Variables d'un lanceur de job : threads alignés sur la part du processeur, fraction de VRAM pour PyTorch."""
        effective, hw = self.effective(), self.hardware()
        environment = thread_environment(thread_count(effective, hw["cpu_cores"]))
        limit, total = vram_share(effective), hw["vram_total_mb"]
        if kind == "face_restore" and limit and total and limit < total:
            environment.update(torch_environment(max(0.05, min(1.0, limit / total))))
        return environment

    def attach(self, job_id, scope, process):
        """Le lanceur entre dans ses groupes ; refus de Windows : OSError (le lanceur n'a encore rien exécuté)."""
        scope.attach(process)
        with self.lock:
            self.scopes[job_id] = scope

    def finish(self, job_id, completed, release=True):
        """Fin du job : pic mesuré gardé (job réussi), groupe fermé, place libérée (``release``)."""
        with self.lock:
            scope = self.scopes.pop(job_id, None)
            peaks = self.peaks.pop(job_id, {})
            reservation = self.reservations.get(job_id) or {}
        if scope is not None:
            try:
                usage = scope.usage()
                if usage.get("peak_ram_mb"):
                    peaks["ram_mb"] = max(int(peaks.get("ram_mb") or 0), int(usage["peak_ram_mb"]))
            except OSError:
                pass
            try:
                scope.close()
            except OSError:
                pass
        if completed and reservation and (peaks.get("vram_mb") or peaks.get("ram_mb")):
            self._record_profile(reservation.get("kind"), reservation.get("model_id"), peaks)
        if release:
            self.release(job_id)

    # ------------------------------------------------------------------ moteurs

    def engine_settings(self, tool_id, vram_total_mb=None):
        effective, hw = self.effective(), self.hardware()
        return engine_settings(tool_id, effective, vram_total_mb or hw["vram_total_mb"], hw["cpu_cores"])

    def engine_environment(self, settings):
        environment = thread_environment((settings or {}).get("threads"))
        environment.update(torch_environment((settings or {}).get("torch_fraction")))
        return environment

    # ------------------------------------------------------------------ mesures des jobs passés

    def _load_profiles(self):
        value = _read_json(Path(self.agent.root) / PROFILES_FILE) or {}
        profiles = value.get("profiles") if isinstance(value.get("profiles"), dict) else {}
        clean = {}
        for key, item in list(profiles.items())[:PROFILES_MAX]:
            if isinstance(key, str) and isinstance(item, dict):
                clean[key[:120]] = {name: [int(v) for v in item.get(name) or [] if _positive_int(v)][-PROFILE_SAMPLES:]
                                    for name in ("vram_mb", "ram_mb")}
        return clean

    def measured(self, kind, model_id):
        item = self.profiles.get(profile_key(kind, model_id))
        if not item:
            return None
        return {"vram_mb": max(item.get("vram_mb") or [0]) or None, "ram_mb": max(item.get("ram_mb") or [0]) or None,
                "samples": max(len(item.get("vram_mb") or []), len(item.get("ram_mb") or []))}

    def _record_profile(self, kind, model_id, peaks):
        key = profile_key(kind, model_id)
        with self.lock:
            item = self.profiles.setdefault(key, {"vram_mb": [], "ram_mb": []})
            for name in ("vram_mb", "ram_mb"):
                value = _positive_int(peaks.get(name))
                if value:
                    item[name] = (item.get(name) or [])[-(PROFILE_SAMPLES - 1):] + [value]
            while len(self.profiles) > PROFILES_MAX:
                self.profiles.pop(next(iter(self.profiles)))
            snapshot = {"profiles": dict(self.profiles), "updated_at": time.time()}
        try:
            _atomic_json(Path(self.agent.root) / PROFILES_FILE, snapshot)
        except OSError:
            pass

    def profiles_summary(self, limit=64):
        with self.lock:
            items = list(self.profiles.items())[-limit:]
        return {key: {"vram_mb": max(item.get("vram_mb") or [0]) or None, "ram_mb": max(item.get("ram_mb") or [0]) or None,
                      "samples": max(len(item.get("vram_mb") or []), len(item.get("ram_mb") or []))} for key, item in items}

    # ------------------------------------------------------------------ mesures en continu

    def ensure_sampler(self):
        with self.lock:
            if self.sampler is not None and self.sampler.is_alive():
                return
            self.sampler = threading.Thread(target=self._sample_loop, name="worker-resource-usage", daemon=True)
            self.sampler.start()

    def _sample_loop(self):
        stop = getattr(self.agent, "stop_requested", None)
        while stop is None or not stop.is_set():
            try:
                self.sample()
            except Exception as error:  # noqa: BLE001 - une mesure ratée ne coupe jamais le fil
                print(f"Mesure des ressources indisponible ({type(error).__name__}).", flush=True)
            if stop is None or stop.wait(SAMPLE_SECONDS):
                return

    def _engine_processes(self):
        """{tool_id: (pid, identité, réglages appliqués)} des moteurs lancés par cet agent."""
        if __package__:
            from .process_control import descendants
        else:
            from process_control import descendants
        found = {}
        for tool_id in ("image_generation", "ai3d", "model-studio"):
            try:
                record = self.agent._runtime_record(tool_id)
            except Exception:  # noqa: BLE001
                continue
            pid, identity = record.get("pid"), str(record.get("process_identity") or "")
            if not isinstance(pid, int) or isinstance(pid, bool) or not identity:
                continue
            try:
                if not self.agent._pid_alive(pid):
                    continue
                pids = [pid] + [child for child, _identity in descendants(pid, identity)]
            except Exception:  # noqa: BLE001
                continue
            found[tool_id] = (pids, record.get("resource_settings"))
        return found

    def _drop_stale(self):
        """Places et groupes de jobs qui ne sont plus actifs dans le journal (filet de sécurité)."""
        journal = getattr(self.agent, "journal", None)
        if journal is None:
            return
        try:
            records = journal.records("jobs")
        except Exception:  # noqa: BLE001
            return
        with self.lock:
            stale = [job_id for job_id in self.reservations
                     if job_id not in records or not self.agent._job_active(records[job_id])]
        for job_id in stale:
            self.finish(job_id, False)

    def sample(self):
        self._drop_stale()
        total_ram, used_ram = system_memory_mb()
        cpu_percent = self.cpu.percent()
        lock = getattr(self.agent, "inventory_lock", None)
        gpus = []
        if lock is not None:
            with lock:
                gpus = [dict(item) for item in (getattr(self.agent, "inventory_snapshot", None) or {}).get("gpus") or [] if isinstance(item, dict)]
        gpu = max(gpus, key=lambda item: _positive_int(item.get("vram_total_mb")) or 0) if gpus else {}
        vram_total = _positive_int(gpu.get("vram_total_mb"))
        vram_free = _number(gpu.get("vram_free_mb"))
        engines = self._engine_processes()
        with self.lock:
            scopes = dict(self.scopes)
            reservations = {job_id: dict(item) for job_id, item in self.reservations.items()}
        # VRAM par processus : au rythme des jobs seulement ; un moteur résident sans calcul ne la change pas.
        busy = bool(reservations)
        due = VRAM_BUSY_SECONDS if busy else VRAM_IDLE_SECONDS
        stop = getattr(self.agent, "stop_requested", None)
        if os.name == "nt" and time.monotonic() - self.vram_measured_at >= due and not (stop is not None and stop.is_set()):
            self.vram_measured_at = time.monotonic()
            self.vram_by_pid = gpu_process_memory_mb()
        per_pid = self.vram_by_pid
        jobs_view, measured_vram, measured_ram, measured_cpu = [], 0, 0, 0.0
        for job_id, scope in scopes.items():
            try:
                usage = scope.usage()
            except OSError:
                continue
            vram = sum(per_pid.get(pid, 0) for pid in usage["pids"]) if isinstance(per_pid, dict) else None
            item = {"vram_mb": vram, "ram_mb": usage.get("ram_mb"), "cpu_percent": usage.get("cpu_percent")}
            measured_vram += vram or 0
            measured_ram += usage.get("ram_mb") or 0
            measured_cpu += usage.get("cpu_percent") or 0
            with self.lock:
                peak = self.peaks.setdefault(job_id, {})
                for name in ("vram_mb", "ram_mb"):
                    if item.get(name):
                        peak[name] = max(int(peak.get(name) or 0), int(item[name]))
            jobs_view.append(item)
        engine_rows = []
        for tool_id in ("image_generation", "ai3d", "model-studio"):
            if tool_id not in engines:
                continue
            pids, applied = engines[tool_id]
            vram = sum(per_pid.get(pid, 0) for pid in pids) if isinstance(per_pid, dict) else None
            # VRAM d'un moteur résident pendant un job : attribuée au job seulement s'il est seul sur ce moteur.
            users = [job_id for job_id, item in reservations.items()
                     if {"image_generation": "comfyui", "ai3d": "hunyuan3d", "model-studio": "orca"}[tool_id] == item.get("kind")]
            if vram and len(users) == 1:
                with self.lock:
                    peak = self.peaks.setdefault(users[0], {})
                    peak["vram_mb"] = max(int(peak.get("vram_mb") or 0), int(vram))
            row = {"tool_id": tool_id, "name": ENGINE_NAMES.get(tool_id, "OrcaSlicer"), "running": True, "vram_mb": vram}
            if tool_id in ENGINE_TOOLS:
                wanted = self.engine_settings(tool_id, vram_total)
                row.update(applied={key: (applied or {}).get(key) for key in ("vram_limit_mb", "reserve_vram_gb", "torch_fraction", "threads")},
                           wanted=wanted, restart_required=settings_differ(applied, wanted))
            engine_rows.append(row)
        with self.lock:
            waiting = [{"since": item["since"], "message": item.get("message", ""), "vram_mb": item.get("vram_mb"),
                        "ram_mb": item.get("ram_mb")} for item in self.waiting.values()]
            reserved = self._reserved()
            self.engines = engine_rows
            self.usage = {
                "measured_at": time.time(),
                "system": {"ram_total_mb": total_ram, "ram_used_mb": used_ram, "cpu_percent": cpu_percent,
                           "vram_total_mb": vram_total,
                           "vram_used_mb": (vram_total - int(vram_free)) if vram_total and vram_free is not None else None},
                "jobs": {"running": len(reservations), "waiting": len(waiting), "reserved_vram_mb": reserved["vram_mb"],
                         "reserved_ram_mb": reserved["ram_mb"], "vram_mb": measured_vram if isinstance(per_pid, dict) else None,
                         "ram_mb": measured_ram if scopes else None, "cpu_percent": round(measured_cpu, 1) if scopes else None},
                "waiting": waiting[:16],
                "engines": engine_rows,
                "vram_per_process": isinstance(per_pid, dict),
            }
            system = dict(self.usage["system"])
        self._host_stability(system, engines)

    def _host_stability(self, system, engines):
        """Journal local des ressources (logs/ressources.log) et filet contre la saturation de la RAM du PC. Ne lève jamais."""
        try:
            if __package__:
                from .job_limits import private_memory_mb
            else:
                from job_limits import private_memory_mb
            rows = []
            for tool_id in ("image_generation", "ai3d", "model-studio"):
                if tool_id in engines:
                    rows.append((ENGINE_NAMES.get(tool_id, "OrcaSlicer"), private_memory_mb(list(engines[tool_id][0]))))
            self.journal.append(host_stability.journal_line(system, host_stability.commit_mb(), rows))
            self.relief.check(system)
        except Exception as error:  # noqa: BLE001 - le journal et le filet ne coupent jamais la mesure
            print(f"Journal des ressources indisponible ({type(error).__name__}).", flush=True)

    def heartbeat_view(self):
        """Champ « resource_usage » du heartbeat : dernière mesure (faite par le fil lancé dans Agent.loop, jamais
        ici), réglage appliqué, pics des jobs passés."""
        with self.lock:
            usage = json.loads(json.dumps(self.usage)) if self.usage else {}
            usage.update(limits_revision=self.settings.get("revision") or "", hard_caps=dict(self.hard_caps),
                         parent_error=self.parent_error)
        usage["profiles"] = self.profiles_summary()
        return usage

    def local_view(self):
        """Action locale « resources » (menu, worker.exe) : réglages en cache, limites effectives, mesures."""
        hw = self.hardware()
        with self.lock:
            settings = {key: self.settings.get(key) for key in ("known", "revision", "max_concurrent_jobs", "received_at")}
            settings["limits"] = tolerant_limits(self.settings.get("limits"))
            usage = json.loads(json.dumps(self.usage)) if self.usage else {}
            hard_caps = dict(self.hard_caps)
        return {"capability": CAPABILITY, "settings": settings, "hardware": hw,
                "effective": effective_limits(settings["limits"], hw), "usage": usage, "hard_caps": hard_caps,
                "windows_hard_caps": os.name == "nt", "explanation": dict(EXPLANATION)}


# --------------------------------------------------------------------------------------------------- actions locales

SELF_PATH = "/api/worker-protocol/self/resources"
LOCAL_PARAMS_LIMIT = 2048


def _runtime(agent):
    runtime = getattr(agent, "resources", None)
    if not isinstance(runtime, ResourceRuntime):
        raise ValueError("Jobs et ressources indisponibles : redémarre l'agent dans sa nouvelle version.")
    return runtime


def v_max_jobs(value):
    try:
        return validate_max_jobs(value)
    except ResourceLimitError as error:
        raise ValueError(str(error)) from None


def v_limits(value):
    try:
        return normalize_limits(value)
    except ResourceLimitError as error:
        raise ValueError(str(error)) from None


def _v_hex32(value):
    if type(value) is not str or len(value) != 32 or any(char not in "0123456789abcdefABCDEF" for char in value):
        raise ValueError("Paramètre local invalide : 32 caractères hexadécimaux sont attendus.")
    return value.lower()


def resources_view(agent, params):
    """Lecture seule, sans réseau : ce que l'agent applique et mesure."""
    return _runtime(agent).local_view()


def resources_set(agent, params):
    """Relaie au site (route signée self/resources) la modification demandée depuis le menu ; session exigée par le site.
    Rien n'est appliqué ici : le site valide, enregistre, et renvoie les réglages par le heartbeat suivant."""
    if __package__:
        from . import self_service
    else:
        import self_service
    params = dict(params or {})
    body = {}
    if "max_concurrent_jobs" in params:
        body["max_concurrent_jobs"] = v_max_jobs(params["max_concurrent_jobs"])
    if "limits" in params:
        body["resource_limits"] = v_limits(params["limits"])
    body = self_service._with_session(agent, params, body)  # fichier de session lu (et effacé) avant tout refus
    if "max_concurrent_jobs" not in body and "resource_limits" not in body:
        raise ValueError("Réglage vide : rien n'est demandé au site.")

    def work():
        outcome = self_service._call(agent, SELF_PATH, body)
        if outcome.get("state") == "done":
            agent.reconnect_requested.set()   # le heartbeat suivant rapporte les nouveaux réglages tout de suite
        return outcome
    return self_service._relay(agent, "resources-set", work)


LOCAL_ACTIONS = {
    "resources": {"handler": resources_view, "params": {}, "read": True},
    "resources-set": {"handler": resources_set, "read": False,
                      "params": {"max_concurrent_jobs": v_max_jobs, "limits": v_limits, "args_file": _v_hex32}},
}
