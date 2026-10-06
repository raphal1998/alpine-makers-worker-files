"""Local allowlisted component installer used by the worker agent only."""
import hashlib
import http.client
import importlib.metadata as importlib_metadata
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
import uuid
from pathlib import Path

if __package__:
    from .transfer_progress import progress, TransferProgress
    from .python_runtime import ensure_engine_python
else:
    from transfer_progress import progress, TransferProgress
    from python_runtime import ensure_engine_python

if __package__ and "." in __package__:
    from .. import worker_legal as legal_compliance, local_sandbox
    from ..installer_process import installation_commit
    from ..storage_paths import WorkerStorage, ascii_path
    from ..safety import (is_link, protect_credentials, remove_worker_file,
                          remove_worker_tree, validate_archive, validate_server_url)
    from ..hardware_profiles import detect_hardware, fire_watch_profile, runtime_profile
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import worker_legal as legal_compliance
    import local_sandbox
    from installer_process import installation_commit
    from storage_paths import WorkerStorage, ascii_path
    from safety import (is_link, protect_credentials, remove_worker_file,
                        remove_worker_tree, validate_archive, validate_server_url)
    from hardware_profiles import detect_hardware, fire_watch_profile, runtime_profile


CATALOG = {
    "comfyui": {
        "kind": "git",
        "url": "https://github.com/Comfy-Org/ComfyUI.git",
        "ref": "9c90340bc26ea69c2029ee6e882acbbb06e157b4",
    },
    "hunyuan3d": {
        "kind": "git",
        "url": "https://github.com/Tencent-Hunyuan/Hunyuan3D-2.git",
        "ref": "f8db63096c8282cb27354314d896feba5ba6ff8a",
    },
    "orca": {"kind": "orca-portable"},
    "orca-bridge": {"kind": "dashboard-file"},
    "cuda-toolkit": {"kind": "cuda-toolkit"},
    "freecad": {"kind": "system-freecad"},
    "printguard": {"kind": "upstream-package", "version": "2.4.0",
                   "url": "https://github.com/oliverbravery/PrintGuard/releases/download/v2.4.0/PrintGuard-windows-x64.zip",
                   "size": 168620029,
                   "sha256": "47e8aea53356f151fe404f80f49a626da668bda5047e305d17befa6ab97dab9f"},
    # Moteur d'Alpine Laser Studio : le code (laser_engine/) arrive avec le paquet Worker ; le
    # composant n'installe que son Python isolé (.venv) avec Pillow épinglé pour la trame d'image.
    # shapely/svgwrite/segno (agent 1.32.0) : bibliothèques de dessin des scripts de l'assistant de code.
    "laser-engine": {"kind": "python-venv", "packages": ("Pillow==11.0.0", "shapely==2.1.2", "svgwrite==1.4.3", "segno==1.6.6")},
    # Surveillance IA des graveuses (agent 1.34.0) : module du tool laser-studio, indépendant du moteur
    # laser. Python isolé + ONNX Runtime (GPU NVIDIA si compatible, sinon CPU) ; le code d'analyse
    # (fire_watch_engine/) arrive avec le paquet Worker, les poids sont convertis sur place.
    "fire-watch": {"kind": "fire-watch"},
    # 3D backend modules: pinned upstream sources in their own environment, driven
    # by a shipped runner (ai3d_backends/) from the Hunyuan3D engine service.
    "hunyuan3d-21": {
        "kind": "git-backend",
        "repos": {".": ("https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1.git", "82920d643c0dc2f7bfd7255f45f62d386edfe60c")},
        "requirements": "requirements-hunyuan3d-21.txt", "runner": "hunyuan3d_21_runner.py", "shims": (),
    },
    "open3d-lab": {
        "kind": "git-backend",
        "repos": {
            "vendor/TripoSG": ("https://github.com/VAST-AI-Research/TripoSG.git", "fc5c40990181e2a756c4e0b1c2f4d6b5202faf8c"),
            "vendor/PartCrafter": ("https://github.com/wgsxm/PartCrafter.git", "3d773bf02fad51c7ab31a5615573fec93b287b30"),
            "vendor/TripoSR": ("https://github.com/VAST-AI-Research/TripoSR.git", "107cefdc244c39106fa830359024f6a2f1c78871"),
        },
        "requirements": "requirements-open3d-lab.txt", "runner": "open3d_lab_runner.py", "shims": ("diso", "torchmcubes"),
    },
}
BACKEND_MODULES = ("hunyuan3d-21", "open3d-lab")
# Installed only with the PBR paint model, never for shape-only users.
HUNYUAN3D_21_PAINT_PACKAGES = ("realesrgan==0.3.0", "basicsr==1.4.2", "pytorch-lightning", "timm", "open3d", "cupy-cuda12x")
# The dashboard's Hunyuan3D adapter loads the HunyuanDiT tokenizer, which the
# upstream requirements file does not pin. Keep this aligned with the engine
# requirements shipped in local-ai/requirements-dashboard.txt.
HUNYUAN_ADAPTER_PACKAGES = ("tiktoken==0.14.0", "sentencepiece==0.2.2")
# Un Worker n'a ni compilateur Rust ni toolchain C++ : pip doit préférer les roues
# publiées et ne jamais compiler les paquets natifs depuis leurs sources. Sans ces
# options, un repli du résolveur sur une vieille version de tokenizers (sans roue
# cp312 Windows) tentait un build_rust et échouait (« can't find Rust compiler »,
# incident Hunyuan3D 04/10). Avec --only-binary, pip choisit une version livrée en
# roue ou échoue avec une erreur de résolution lisible, jamais une compilation.
NATIVE_WHEEL_ONLY_PACKAGES = ("tokenizers", "safetensors", "sentencepiece", "tiktoken", "numpy", "opencv-python",
                              "pymeshlab", "xatlas", "scikit-image", "onnxruntime", "onnxruntime-gpu", "Pillow")
PIP_WHEEL_PREFERENCE = ("--prefer-binary", "--only-binary", ",".join(NATIVE_WHEEL_ONLY_PACKAGES))
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024
DOWNLOAD_RESERVE_BYTES = 512 * 1024 * 1024


def _installation_output(line):
    """Never forward pip index credentials or signed URLs to the dashboard."""
    line = re.sub(r"https?://[^\s<>]+", "[URL masquée]", str(line))
    line = re.sub(r"(?i)(authorization\s*[:=]\s*).*", r"\1[masqué]", line)
    line = re.sub(r"(?i)((?:token|password|api[_-]?key)\s*[:=]\s*)\S+", r"\1[masqué]", line)
    return line.rstrip("\r\n")[-8192:]


class _PipProgress:
    def __init__(self, value, label):
        self.value, self.label = value, label
        self.message = label
        self.started = self.last = time.monotonic()
        self.last_output = 0
        self.transfer = None
        progress(value, label, phase="installing")

    def consume(self, line):
        measurement = re.fullmatch(r"Progress (\d+) of (\d+|None)", line.strip())
        if measurement:
            done = int(measurement[1])
            total = int(measurement[2]) if measurement[2] != "None" else 0
            if self.transfer is None or done < getattr(self, "previous_done", 0):
                self.transfer = TransferProgress(self.message, initial=done, start=self.value, span=0)
            self.previous_done = done
            self.transfer.update(done, total, force=bool(total and done == total))
            self.last = time.monotonic()
            return
        text = line.strip()
        if text.startswith(("Collecting ", "Downloading ", "Using cached ", "Installing collected packages:",
                            "Building wheel for ", "Preparing metadata ", "Getting requirements ",
                            "Successfully installed ", "Attempting uninstall:", "ERROR:", "WARNING:")):
            self.message = f"{self.label} · {text[:320]}"
            self.transfer = TransferProgress(self.message, start=self.value, span=0) if text.startswith("Downloading ") else None
            self.previous_done = 0
            now = time.monotonic()
            # A dependency resolver can print hundreds of lines at once. Do
            # not turn each line into a blocking Worker/backend HTTP exchange.
            if now - self.last_output >= 1 or text.startswith(("ERROR:", "Successfully installed ", "Installing collected packages:")):
                self.last = self.last_output = now
                progress(self.value, self.message, phase="installing")

    def heartbeat(self):
        now = time.monotonic()
        if now - self.last >= 5:
            # A running process does not prove a transfer: no invented speed/ETA.
            progress(self.value, f"{self.message} — opération en cours ({int(now - self.started)} s).", phase="installing")
            self.last = now


def _pip_progress_arguments(args, cwd):
    args = list(args)
    if args[1:4] not in (["-m", "pip", "install"], ["-m", "pip", "download"]):
        return args
    # Older pip versions have no machine-readable download bar. The first
    # bootstrap upgrades pip, but repair can encounter an older existing venv.
    try:
        help_result = subprocess.run([args[0], "-m", "pip", "install", "--help"], cwd=cwd,
                                     capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, check=False)
        raw_supported = help_result.returncode == 0 and re.search(r"\[[^\]\n]*\braw\b[^\]\n]*\]", help_result.stdout)
    except (OSError, subprocess.SubprocessError):
        raw_supported = False
    return [*args, "--disable-pip-version-check", "--no-input", "--progress-bar", "raw" if raw_supported else "off"]


def _process_options(env=None, low_priority=False):
    """Options de lancement : environnement propre et, si demandé, priorité basse.

    La priorité basse (surveillance IA : pip et conversion des modèles) laisse le processeur au fil
    d'envoi G-code d'une graveuse qui travaillerait pendant l'installation.
    """
    options = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    if env is not None:
        options["env"] = env
    if low_priority:
        if os.name == "nt":
            options["creationflags"] |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        else:
            options["preexec_fn"] = lambda: os.nice(10)
    return options


def _call_with_progress(args, cwd, timeout, value, label, env=None, low_priority=False):
    reporter = _PipProgress(value, label)
    args = _pip_progress_arguments(args, cwd)
    messages = queue.Queue(maxsize=64)
    finished, stop = threading.Event(), threading.Event()
    output = ""
    process = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                               **_process_options(env, low_priority))

    def read_output():
        try:
            with process.stdout:
                while not stop.is_set():
                    line = process.stdout.readline(8192)
                    if not line:
                        break
                    while not stop.is_set():
                        try:
                            messages.put(_installation_output(line), timeout=0.2)
                            break
                        except queue.Full:
                            pass
        finally:
            finished.set()

    reader = threading.Thread(target=read_output, name="worker-pip-output", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        while not (process.poll() is not None and finished.is_set() and messages.empty()):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Délai de {timeout} s dépassé : {label}. L’installation n’est pas validée ; les modèles existants sont conservés.")
            try:
                line = messages.get(timeout=0.2)
                output = (output + line + "\n")[-12000:]
                reporter.consume(line)
            except queue.Empty:
                pass
            reporter.heartbeat()
        if process.wait():
            raise RuntimeError(output[-2500:] or "Commande d’installation échouée.")
        return output
    finally:
        stop.set()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        # Descendants remain inside the enclosing InstallerScope. On timeout
        # its cleanup kills that private group; never kill by executable name.
        reader.join(timeout=1)


def call(args, cwd=None, timeout=3600, *, progress_value=None, progress_label="Installation des dépendances",
         env=None, low_priority=False):
    if progress_value is not None:
        return _call_with_progress(args, cwd, timeout, progress_value, progress_label, env=env, low_priority=low_priority)
    options = {}
    if env is not None or low_priority:
        # Environnement explicite (PYTHONIOENCODING=utf-8) : sortie décodée en UTF-8, sans exception.
        options = {**_process_options(env, low_priority), "encoding": "utf-8", "errors": "replace"}
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False, **options)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-2500:] or "Commande d’installation échouée.")
    return result.stdout


def python_in(root):
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _expected_torch_versions(profile):
    """Return the exact allowlisted PyTorch distribution versions for a profile."""
    expected = {}
    for requirement in profile.get("packages") or []:
        match = re.fullmatch(r"(torch|torchvision|torchaudio)==([^\s;]+)", str(requirement))
        if match:
            expected[match.group(1)] = match.group(2)
    return expected


def installed_torch_versions(root):
    """Inspect another venv's metadata without importing or executing its packages."""
    environment = Path(root) / ".venv"
    candidates = [environment / "Lib" / "site-packages"]
    candidates.extend(environment.glob("lib/python*/site-packages"))
    versions = {}
    for site_packages in candidates:
        if not site_packages.is_dir() or is_link(site_packages):
            continue
        try:
            distributions = importlib_metadata.distributions(path=[str(site_packages)])
            for distribution in distributions:
                name = str(distribution.metadata.get("Name") or "").strip().casefold().replace("_", "-")
                if name in {"torch", "torchvision", "torchaudio"}:
                    versions[name] = str(distribution.version or "").strip()
        except (OSError, ValueError, importlib_metadata.PackageNotFoundError):
            return {}
    return versions


def runtime_profile_health(root, expected):
    """Compare the signed-code policy with marker and actual venv metadata.

    This is an inventory fact only. It never imports Torch, runs a GPU kernel or
    trusts a value supplied by the dashboard/browser.
    """
    root = Path(root)
    marker = root / ".alpine-runtime-profile.json"
    if not marker.is_file() or is_link(marker):
        return {"repair_required": True, "reason": "Profil d’installation absent ou non vérifiable.", "installed_profile": {}}
    try:
        stored = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"repair_required": True, "reason": "Profil d’installation illisible.", "installed_profile": {}}
    if not isinstance(stored, dict):
        return {"repair_required": True, "reason": "Profil d’installation invalide.", "installed_profile": {}}
    safe = {key: stored.get(key) for key in ("label", "cuda", "python", "packages")}
    expected_versions = _expected_torch_versions(expected)
    actual_versions = installed_torch_versions(root)
    mismatches = []
    if str(stored.get("cuda") or "") != str(expected.get("cuda") or ""):
        mismatches.append("version CUDA/PyTorch différente")
    if str(stored.get("python") or "") != str(expected.get("python") or ""):
        mismatches.append("version Python différente")
    if _expected_torch_versions(stored) != expected_versions:
        mismatches.append("profil de paquets différent")
    if actual_versions != expected_versions:
        mismatches.append("paquets PyTorch installés différents du profil attendu")
    return {
        "repair_required": bool(mismatches),
        "reason": "; ".join(dict.fromkeys(mismatches)),
        "installed_profile": safe,
    }


def _assert_runtime_profile(root, profile):
    expected = _expected_torch_versions(profile)
    actual = installed_torch_versions(root)
    if actual != expected:
        raise RuntimeError(
            "Les versions PyTorch installées ne correspondent pas au profil matériel sélectionné "
            f"(attendu : {expected or 'indisponible'} ; détecté : {actual or 'indisponible'})."
        )


def nvidia_available():
    executable = shutil.which("nvidia-smi")
    if not executable:
        return False
    try:
        result = subprocess.run(
            [executable, "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


CUDA_TORCH_PROBE = (
    "import torch, torchvision, torchaudio, json; "
    "assert torch.version.cuda and torch.cuda.is_available(), 'PyTorch CUDA indisponible'; "
    "x=torch.ones((32,32),device='cuda',dtype=torch.float32); "
    "assert (x@x).cpu()[0,0].item()==32; torch.cuda.synchronize(); "
    "print(json.dumps({'torch':torch.__version__,'cuda':torch.version.cuda,'gpu':torch.cuda.get_device_name(0)}))"
)


def selected_profile(component):
    tool = {"comfyui": "image_generation", "hunyuan3d": "ai3d", "cuda-toolkit": "ai3d", "hunyuan3d-21": "ai3d", "open3d-lab": "ai3d",
            "orca": "model-studio", "orca-bridge": "model-studio", "freecad": "converter",
            "laser-engine": "laser-studio", "fire-watch": "laser-studio"}.get(component, component)
    profile = runtime_profile(detect_hardware(), tool)
    if profile["status"] != "supported":
        raise RuntimeError(profile["reason"] or "Matériel du Worker à vérifier avant installation.")
    if component == "fire-watch":
        # Même système/architecture que Laser Studio ; le choix GPU/CPU se fait à l'installation.
        profile = {**profile, "label": "Surveillance IA graveuse · ONNX Runtime (GPU NVIDIA si compatible, sinon CPU)"}
    return profile


def activate_gpu_profile(profile):
    if profile.get("selected_gpu_index") is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(profile["selected_gpu_index"])
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ["TORCH_CUDA_ARCH_LIST"] = profile["compute_capability"]


def ensure_cuda_torch(python, root, profile=None):
    """Select wheels for the actual GPU; preserve an already working build."""
    profile = profile or selected_profile(root.name)
    activate_gpu_profile(profile)
    versions_match = installed_torch_versions(root) == _expected_torch_versions(profile)
    try:
        call([python, "-c", CUDA_TORCH_PROBE], cwd=root, timeout=60)
        if versions_match:
            progress(50, f"PyTorch déjà opérationnel et adapté à {profile['gpu_name']} ; environnement conservé.")
            return
        progress(50, f"PyTorch fonctionne mais son profil ne correspond pas à {profile['gpu_name']} ; adaptation en cours…")
    except RuntimeError:
        pass
    progress(50, f"{profile['gpu_name']} · capacité CUDA {profile['compute_capability']} : installation PyTorch CUDA {profile['cuda']} adapté…")
    call(
        [python, "-m", "pip", "install", "--upgrade", "--force-reinstall",
         *profile["packages"], "--index-url", profile["index_url"]],
        cwd=root, timeout=7200, progress_value=50, progress_label=f"Installation PyTorch CUDA {profile['cuda']}",
    )
    try:
        call([python, "-c", CUDA_TORCH_PROBE], cwd=root, timeout=60)
    except RuntimeError as error:
        raise RuntimeError("PyTorch CUDA installé mais le test GPU a échoué. Vérifie le pilote NVIDIA du Worker. " + str(error)[-1200:]) from error
    _assert_runtime_profile(root, profile)


def engine_environment(root):
    """Preserve old environments; never create a 3.12 venv with Python 3.14."""
    environment = root / ".venv"
    if is_link(environment):
        raise RuntimeError("Environnement Python lié : réparation automatique refusée.")
    if python_in(environment).is_file():
        try:
            version = call([str(python_in(environment)), "-I", "-c", "import sys,struct; print('%s.%s/%s' % (*sys.version_info[:2],struct.calcsize('P')*8))"], timeout=20).strip()
            if version == "3.12/64":
                return str(python_in(environment))
        except RuntimeError:
            pass
    base_python = ensure_engine_python(root.parent.parent)
    previous = None
    if environment.exists():
        if environment.resolve().parent != root.resolve():
            raise RuntimeError("L’environnement Python sort du moteur géré.")
        previous = root / (".venv-preserved-" + uuid.uuid4().hex)
        os.replace(environment, previous)
        progress(43, f"Ancien environnement Python conservé dans {previous.name} ; modèles inchangés.")
    try:
        call([str(base_python), "-m", "venv", str(environment)], timeout=180)
    except Exception:
        if previous is not None:
            if environment.exists():
                os.replace(environment, root / (".venv-incomplete-" + uuid.uuid4().hex))
            os.replace(previous, environment)
        raise
    return str(python_in(environment))


def torch_constraints(python, root):
    # pip lui-même suggère cette mise à jour dans son propre message d'erreur :
    # un pip trop ancien ne reconnaît pas la roue précompilée de certains paquets
    # récents (ex. tokenizers pour cp312-win_amd64) et tente de les compiler depuis
    # les sources, échouant sans compilateur Rust sur le Worker (incident 04/10).
    # Un environnement Python réutilisé (engine_environment) garde sinon son pip
    # d'origine indéfiniment ; cet appel s'exécute avant chaque pip install -r.
    call([python, "-m", "pip", "install", "--upgrade", "pip"], timeout=120)
    # Keep whichever installed build passed the probe, including a healthy older
    # build; dependency resolution must not replace it with the PyPI default.
    value = json.loads(call([python, "-c", "import importlib.metadata as m,json; print(json.dumps({n:m.version(n) for n in ('torch','torchvision','torchaudio')}))"], timeout=30))
    if set(value) != {"torch", "torchvision", "torchaudio"} or any(not re.fullmatch(r"[0-9][A-Za-z0-9.+_-]{0,70}", str(v)) for v in value.values()):
        raise RuntimeError("Versions PyTorch incohérentes après installation.")
    constraints = root / ".alpine-torch-constraints.txt"
    if is_link(constraints):
        raise RuntimeError("Fichier de contraintes lié : installation refusée.")
    lines = [f"{name}=={version}" for name, version in value.items()]
    # requirements.txt des moteurs 3D (hunyuan3d, hunyuan3d-21, open3d-lab) ne fixe
    # jamais transformers : pip prend la dernière version publiée, dont la refonte
    # majeure (5.x) a renommé les sous-modules d'attention de Dinov2 (q_proj/k_proj/
    # v_proj au lieu de attention.{query,key,value}) — incompatible avec les poids
    # mis en cache (facebook/dinov2-large/-giant) chargés par le code figé de ces
    # composants. RuntimeError "Error(s) in loading state_dict" (incident 03/10).
    lines.append("transformers<5")
    constraints.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ["-c", str(constraints)]


def install_hunyuan_worker(root):
    # The upstream API returns binary meshes and is not the authenticated JSON
    # service expected by our runner. Ship the existing dashboard adapter.
    source = Path(__file__).resolve().parents[1] / "hunyuan_worker.py"
    if not source.is_file():
        raise RuntimeError("Le service Hunyuan3D du dashboard manque au paquet. Mets à jour le Worker puis répare le moteur.")
    shutil.copy2(source, root / "worker.py")


def _write_managed_file(target, data):
    if is_link(target):
        raise RuntimeError(f"{target.name} est un lien : actualisation refusée.")
    if target.is_file() and target.read_bytes() == data:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".agent-update")
    temporary.write_bytes(data)
    os.replace(temporary, target)
    return True


def install_backend_files(component, root):
    """Copy the shipped runner and pure-Python shims of a backend module into its root.

    Called at module installation and before each engine start, so an agent
    update reaches the modules without reinstalling their environments.
    """
    spec = CATALOG.get(component) or {}
    if spec.get("kind") != "git-backend":
        raise RuntimeError("Module 3D inconnu.")
    shipped = Path(__file__).resolve().parents[1] / "ai3d_backends"
    runner = shipped / spec["runner"]
    if not runner.is_file():
        raise RuntimeError("Le runner du module 3D manque au paquet Worker. Mets à jour le Worker puis répare le module.")
    changed = _write_managed_file(root / "alpine_backend.py", runner.read_bytes())
    for shim in spec.get("shims", ()):
        source = shipped / f"shim_{shim}.py"
        if not source.is_file():
            raise RuntimeError(f"Le remplaçant {shim} manque au paquet Worker.")
        changed = _write_managed_file(root / "shims" / shim / "__init__.py", source.read_bytes()) or changed
    return changed


def install_git_backend(component, root, spec):
    profile = selected_profile(component)
    if shutil.disk_usage(root.parent).free < 8 * 1024**3:
        raise RuntimeError("Au moins 8 Gio libres sont nécessaires sur le disque du moteur pour Python, PyTorch et ses dépendances, hors modèles.")
    activate_gpu_profile(profile)
    progress(8, f"Préparation du module {component}…")
    root.mkdir(parents=True, exist_ok=True)
    ready = root / ".raphal-ready"
    ready.unlink(missing_ok=True)
    repos = spec["repos"]
    for index, (relative, (url, ref)) in enumerate(repos.items()):
        target = (root / relative).resolve()
        if target != root.resolve() and root.resolve() not in target.parents:
            raise RuntimeError("Dépôt du module hors de son répertoire : installation refusée.")
        if is_link(target):
            raise RuntimeError("Le dossier de sources du module est un lien : installation refusée.")
        target.mkdir(parents=True, exist_ok=True)
        if not (target / ".git").exists():
            call(["git", "init", str(target)])
            call(["git", "remote", "add", "origin", url], cwd=target)
        progress(12 + index * 6, f"Sources {Path(relative).name if relative != '.' else component} : téléchargement de la révision vérifiée…")
        call(["git", "fetch", "--depth", "1", "origin", ref], cwd=target)
        _free_checkout_path(target, ref, 12 + index * 6)
        call(["git", "checkout", "--detach", ref], cwd=target)
    py = engine_environment(root)
    progress(45, "Environnement Python prêt, installation des dépendances…")
    call([py, "-m", "pip", "install", "--upgrade", "pip", "wheel", "setuptools"], cwd=root,
         progress_value=45, progress_label="Préparation de pip, wheel et setuptools")
    ensure_cuda_torch(py, root, profile)
    constraints = torch_constraints(py, root)
    requirements = Path(__file__).resolve().with_name(spec["requirements"])
    if not requirements.is_file():
        raise RuntimeError("La liste de dépendances du module manque au paquet Worker. Mets à jour le Worker.")
    call([py, "-m", "pip", "install", *PIP_WHEEL_PREFERENCE, *constraints, "--extra-index-url", profile["index_url"], "-r", str(requirements)], cwd=root, timeout=7200,
         progress_value=60, progress_label=f"Installation des dépendances de {component}")
    progress(88, "Dépendances installées, finalisation…")
    install_backend_files(component, root)
    # Dependency resolution must not silently replace the GPU build.
    call([py, "-c", CUDA_TORCH_PROBE], cwd=root, timeout=60)
    _assert_runtime_profile(root, profile)
    profile_path = root / ".alpine-runtime-profile.json"
    if is_link(profile_path):
        raise RuntimeError("Profil moteur lié : validation refusée.")
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    ready.write_text(json.dumps({"component": component, "refs": {relative: ref for relative, (_url, ref) in repos.items()}}), encoding="utf-8")
    return f"Module {component} installé sur les révisions vérifiées."


def download(url, destination, headers=None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent).free < DOWNLOAD_RESERVE_BYTES:
        raise RuntimeError("Espace disque insuffisant pour préparer ce téléchargement dans le Worker. Aucun fichier existant n’a été supprimé.")
    request = protect_credentials(urllib.request.Request(url, headers=headers or {"User-Agent": "Raphal-Worker-Installer/1.0"}))
    try:
        with urllib.request.urlopen(request, timeout=90) as response, destination.open("wb") as output:
            advertised = int(response.headers.get("Content-Length") or 0)
            if advertised > MAX_ARCHIVE_BYTES:
                raise RuntimeError("Le paquet distant est trop volumineux.")
            if advertised and shutil.disk_usage(destination.parent).free < advertised + DOWNLOAD_RESERVE_BYTES:
                raise RuntimeError("Espace disque insuffisant pour ce téléchargement et son extraction. Aucun composant existant n’a été supprimé.")
            total = 0
            progress(12, "Téléchargement du composant…")
            transfer = TransferProgress("Téléchargement du composant…", start=12, span=58)
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise RuntimeError("Le paquet distant dépasse la limite autorisée.")
                output.write(chunk)
                transfer.update(total, advertised)
            transfer.update(total, advertised, force=True)
            if advertised and total != advertised:
                raise RuntimeError("Le téléchargement du composant est incomplet. Réessaie l’installation.")
    except urllib.error.HTTPError as error:
        try:
            message = json.loads(error.read().decode("utf-8", errors="replace")).get("error")
        except Exception:
            message = ""
        raise RuntimeError(message or f"Téléchargement refusé (HTTP {error.code}).") from error


def safe_extract(archive_path, destination):
    destination = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        try:
            validate_archive(archive, destination, MAX_EXTRACTED_BYTES)
        except ValueError as error:
            raise RuntimeError(str(error)) from error
        archive.extractall(destination)


def replace_component(root, payload_root):
    components = root.parent.resolve()
    payload_root = payload_root.resolve()
    if components.parent not in payload_root.parents:
        raise RuntimeError("Le répertoire préparé est hors de l’espace Worker.")
    if is_link(root) or root.resolve().parent != components:
        raise RuntimeError("Le répertoire du composant est un lien ou sort de l’espace Worker.")
    operation_id = uuid.uuid4().hex
    staging = components / f".{root.name}-staging-{operation_id}"
    previous = components / f".{root.name}-previous-{operation_id}"
    shutil.move(str(payload_root), str(staging))
    try:
        with installation_commit():
            if root.exists():
                os.replace(root, previous)
            try:
                os.replace(staging, root)
                (root / ".alpine-managed.json").write_text(json.dumps({"owner": "Alpine Makers", "component": root.name}), encoding="utf-8")
            except OSError:
                if previous.exists() and not root.exists():
                    os.replace(previous, root)
                raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    if previous.exists():
        try:
            shutil.rmtree(previous)
        except OSError:
            progress(95, f"Moteur installé ; ancienne copie conservée dans {previous.name} (fichier occupé).")


QUARANTINE_DIRNAME = ".alpine-ecartes"


def _checkout_conflicts(root, ref):
    """Fichiers présents sur disque, inconnus de git, que la révision cible fournit.

    git refuse un « checkout --detach » dès qu'un seul fichier non suivi serait
    écrasé, et abandonne tout le checkout. Plutôt que de deviner ces noms, on
    demande à git ce que la révision contient (git ls-tree) et ce qu'il suit déjà
    (git ls-files) : l'intersection avec le disque est exactement ce qui bloque.
    """
    try:
        fournis = call(["git", "ls-tree", "-r", "-z", "--name-only", ref], cwd=root) or ""
        suivis = call(["git", "ls-files", "-z"], cwd=root) or ""
    except RuntimeError:
        return []
    connus = {chemin for chemin in suivis.split("\0") if chemin}
    collisions = []
    for chemin in fournis.split("\0"):
        if not chemin or chemin in connus or ".." in chemin.split("/"):
            continue
        cible = root / chemin
        if cible.is_symlink() or cible.exists():
            collisions.append(chemin)
    return collisions


def _free_checkout_path(root, ref, valeur=30):
    """Écarte, sans rien détruire, ce qui empêche le checkout de la révision cible.

    La désinstallation conserve volontairement models/, input/, output/ et user/
    (modèles téléchargés et données de l'utilisateur). Or le moteur livre lui-même
    des fichiers dans ces dossiers — input/example.png, models/configs/*.yaml,
    les marqueurs put_X_here — qui survivent donc à la désinstallation et bloquent
    la réinstallation suivante (incidents tool.install 22/09, tool.repair 23/09,
    réinstallation complète 23/09 : le moteur est resté sans aucun fichier, HEAD
    non né, et « Réparer » rejouait le même échec).

    Les fichiers concernés sont déplacés dans .alpine-ecartes/<horodatage>/ au lieu
    d'être supprimés : si l'un d'eux était une configuration personnelle de
    l'utilisateur portant le même nom qu'un fichier du dépôt, elle reste récupérable.
    """
    collisions = _checkout_conflicts(root, ref)
    if not collisions:
        return []
    ecarts = root / QUARANTINE_DIRNAME / time.strftime("%Y%m%d-%H%M%S")
    deplaces = []
    for chemin in collisions:
        source = root / chemin
        destination = ecarts / chemin
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        deplaces.append(chemin)
    progress(valeur, f"{len(deplaces)} fichier(s) d'une installation précédente écartés dans "
                   f"{QUARANTINE_DIRNAME}/{ecarts.name} pour libérer la mise à jour des sources.")
    return deplaces


def install_git(component, root, spec):
    profile = selected_profile(component)
    if shutil.disk_usage(root.parent).free < 8 * 1024**3:
        raise RuntimeError("Au moins 8 Gio libres sont nécessaires sur le disque du moteur pour Python, PyTorch et ses dépendances, hors modèles.")
    activate_gpu_profile(profile)
    progress(8, f"Préparation de {component}…")
    # A model download or an interrupted installation may have created this
    # directory already. Keep its models and initialise the missing repository
    # instead of treating any directory as a complete engine installation.
    root.mkdir(parents=True, exist_ok=True)
    ready = root / ".raphal-ready"
    ready.unlink(missing_ok=True)
    if not (root / ".git").exists():
        call(["git", "init", str(root)])
        call(["git", "remote", "add", "origin", spec["url"]], cwd=root)
    progress(30, "Sources téléchargées, vérification de la version…")
    call(["git", "fetch", "--depth", "1", "origin", spec["ref"]], cwd=root)
    ecartes = _free_checkout_path(root, spec["ref"], 32)
    call(["git", "checkout", "--detach", spec["ref"]], cwd=root)
    py = engine_environment(root)
    progress(48, "Environnement Python prêt, installation des dépendances…")
    call([py, "-m", "pip", "install", "--upgrade", "pip", "wheel", "setuptools"], cwd=root,
         progress_value=48, progress_label="Préparation de pip, wheel et setuptools")
    ensure_cuda_torch(py, root, profile)
    constraints = torch_constraints(py, root)
    requirements = root / "requirements.txt"
    if requirements.is_file():
        call([py, "-m", "pip", "install", *PIP_WHEEL_PREFERENCE, *constraints, "--extra-index-url", profile["index_url"], "-r", str(requirements)], cwd=root, timeout=7200,
             progress_value=60, progress_label=f"Installation des dépendances de {component}")
    progress(88, "Dépendances installées, finalisation…")
    if component == "hunyuan3d":
        call([py, "-m", "pip", "install", *PIP_WHEEL_PREFERENCE, *constraints, "--extra-index-url", profile["index_url"], *HUNYUAN_ADAPTER_PACKAGES], cwd=root, timeout=3600,
             progress_value=84, progress_label="Installation du tokenizer HunyuanDiT")
        call([py, "-m", "pip", "install", *PIP_WHEEL_PREFERENCE, *constraints, "--extra-index-url", profile["index_url"], "--no-build-isolation", "-e", "."], cwd=root, timeout=7200,
             progress_value=88, progress_label="Finalisation du moteur Hunyuan3D")
        install_hunyuan_worker(root)
    # Dependency resolution must not silently replace the GPU build.
    call([py, "-c", CUDA_TORCH_PROBE], cwd=root, timeout=60)
    _assert_runtime_profile(root, profile)
    profile_path = root / ".alpine-runtime-profile.json"
    if is_link(profile_path):
        raise RuntimeError("Profil moteur lié : validation refusée.")
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    ready.write_text(json.dumps({"component": component, "ref": spec["ref"]}), encoding="utf-8")
    ecart = (f" {len(ecartes)} fichier(s) d'une installation précédente ont été écartés dans "
             f"{QUARANTINE_DIRNAME}/.") if ecartes else ""
    return f"{component} installé sur la révision vérifiée {spec['ref']}.{ecart}"


def install_orca(worker_root, root):
    progress(5, "Recherche de la dernière version d’OrcaSlicer…")
    if os.name != "nt":
        raise RuntimeError("Le paquet OrcaSlicer Worker est actuellement disponible uniquement sous Windows.")
    architecture = "arm64" if "arm" in platform.machine().lower() else "x64"
    api_request = urllib.request.Request(
        "https://api.github.com/repos/SoftFever/OrcaSlicer/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Raphal-Worker-Orca-Installer/1.0"},
    )
    with urllib.request.urlopen(api_request, timeout=45) as response:
        release = json.loads(response.read(10 * 1024 * 1024).decode("utf-8"))
    assets = release.get("assets") if isinstance(release, dict) else []
    asset = next((item for item in assets or [] if isinstance(item, dict)
                  and str(item.get("name") or "").lower().startswith("orcaslicer_windows_")
                  and f"_{architecture}_portable.zip" in str(item.get("name") or "").lower()), None)
    if not asset or not str(asset.get("browser_download_url") or "").startswith("https://"):
        raise RuntimeError(f"La distribution officielle OrcaSlicer {architecture} est introuvable.")
    with tempfile.TemporaryDirectory(prefix="orca-install-", dir=WorkerStorage(worker_root).path("temp")) as temporary:
        temporary_path = Path(temporary)
        archive = temporary_path / "orca.zip"
        extracted = temporary_path / "extracted"
        extracted.mkdir()
        download(asset["browser_download_url"], archive, {"User-Agent": "Raphal-Worker-Orca-Installer/1.0"})
        safe_extract(archive, extracted)
        progress(82, "Archive téléchargée, vérification du moteur…")
        executable = next(extracted.rglob("orca-slicer.exe"), None)
        if not executable or not (executable.parent / "resources" / "profiles" / "BBL").is_dir():
            raise RuntimeError("Le paquet OrcaSlicer ne contient pas le moteur et les profils BBL attendus.")
        replace_component(root, executable.parent)
    return f"OrcaSlicer {release.get('tag_name') or ''} installé. Le bridge est un module séparé."


def worker_server_config(worker_root):
    try:
        config = json.loads((worker_root / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError("Configuration du Worker introuvable.") from error
    if __package__ and "." in __package__:
        from ..identity import configure as configure_identity
    else:
        from identity import configure as configure_identity
    configure_identity(worker_root, config)
    try:
        server = validate_server_url(config.get("server_url"))
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    headers = {
        "Authorization": f"Bearer {config.get('token', '')}",
        "X-Worker-ID": str(config.get("worker_id") or ""),
        "User-Agent": "Alpine-Worker-Component-Installer/1.0",
    }
    return server, headers


def install_orca_bridge(worker_root, root):
    progress(12, "Téléchargement du bridge Orca depuis le dashboard…")
    server, headers = worker_server_config(worker_root)
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / "orca_core_bridge.ps1.download"
    destination = worker_root / "orca_core_bridge.ps1"
    download(f"{server}/api/worker-protocol/components/orca-bridge", temporary, headers)
    if temporary.stat().st_size < 1000:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Le bridge Orca téléchargé est incomplet.")
    os.replace(temporary, destination)
    (root / ".raphal-ready").write_text(str(destination), encoding="utf-8")
    progress(99, "Bridge Orca installé et vérifié.")
    return "Bridge Orca installé depuis le dashboard authentifié."


def find_cuda(version="12.8"):
    candidates = []
    for variable in ("CUDA_PATH_V" + version.replace(".", "_"), "CUDA_PATH"):
        if os.getenv(variable): candidates.append(Path(os.environ[variable]))
    program_files = os.getenv("ProgramFiles")
    if program_files:
        candidates.extend(Path(program_files).glob("NVIDIA GPU Computing Toolkit/CUDA/v" + version + "*"))
    nvcc = shutil.which("nvcc")
    if nvcc: candidates.append(Path(nvcc).resolve().parent.parent)
    for path in candidates:
        executable = path / "bin" / ("nvcc.exe" if os.name == "nt" else "nvcc")
        if executable.is_file():
            try:
                output = call([str(executable), "--version"], timeout=20)
                if re.search(r"release\s+" + re.escape(version) + r"(?:,|\s|$)", output):
                    return path.resolve()
            except RuntimeError:
                pass
    return None


def install_cuda_toolkit(worker_root, root):
    profile = selected_profile("cuda-toolkit")
    version = profile["cuda"]
    progress(5, f"{profile['gpu_name']} : recherche de CUDA Toolkit {version}…")
    cuda_root = find_cuda(version)
    if not cuda_root and os.name == "nt":
        winget = shutil.which("winget")
        if not winget:
            raise RuntimeError(f"CUDA Toolkit {version} est absent et winget n’est pas disponible sur ce Worker.")
        versions = call([winget, "show", "--id", "Nvidia.CUDA", "-e", "--versions",
                         "--accept-source-agreements", "--disable-interactivity"], timeout=120)
        available = [line.strip() for line in versions.splitlines() if re.fullmatch(re.escape(version) + r"(?:\.\d+)*", line.strip())]
        if not available:
            raise RuntimeError(f"CUDA Toolkit {version} n’est pas disponible dans la source winget de ce Worker.")
        progress(15, f"Téléchargement et installation de CUDA {available[0]}…")
        call([winget, "install", "--id", "Nvidia.CUDA", "-e", "--version", available[0], "--silent",
              "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"], timeout=14400)
        progress(92, "CUDA installé, vérification de nvcc…")
        cuda_root = find_cuda(version)
    if not cuda_root:
        raise RuntimeError(f"CUDA Toolkit {version} avec nvcc est introuvable. Vérifie les droits et le résultat de l’installation système sur ce Worker.")
    root.mkdir(parents=True, exist_ok=True)
    (root / ".disabled").unlink(missing_ok=True)
    (root / "cuda-path.txt").write_text(str(cuda_root), encoding="utf-8")
    progress(99, f"CUDA Toolkit {version} détecté. Paint nécessite aussi ses modèles, extensions et suffisamment de VRAM.")
    return f"CUDA Toolkit Worker prêt : {cuda_root}"


def install_printguard(worker_root, root):
    progress(5, "Préparation de PrintGuard 2.4.0 depuis son éditeur…")
    if os.name != "nt":
        raise RuntimeError("Le paquet PrintGuard Worker est actuellement disponible uniquement sous Windows.")
    spec = CATALOG["printguard"]
    with tempfile.TemporaryDirectory(prefix="printguard-install-", dir=WorkerStorage(worker_root).path("temp")) as temporary:
        temporary_path = Path(temporary)
        archive = temporary_path / "printguard.zip"
        extracted = temporary_path / "extracted"
        extracted.mkdir()
        # Same upstream 2.4.0 archive as the former dashboard package. No Alpine
        # token goes to GitHub; verify exact bytes before extraction/replacement.
        download_pinned(spec["url"], archive, spec["size"], spec["sha256"], "PrintGuard officiel", value=15)
        safe_extract(archive, extracted)
        progress(82, "Paquet téléchargé, vérification de PrintGuard…")
        executable = next(extracted.rglob("PrintGuard.exe"), None)
        if not executable:
            raise RuntimeError("Le paquet PrintGuard ne contient pas l’exécutable attendu.")
        replace_component(root, executable.parent)
    return "PrintGuard 2.4.0 installé depuis l’éditeur officiel, archive SHA-256 vérifiée."


def find_freecad(worker_root):
    marker = worker_root / "components" / "freecad" / "freecad-path.txt"
    candidates = []
    if marker.is_file():
        try: candidates.append(Path(marker.read_text(encoding="utf-8").strip()))
        except OSError: pass
    try:
        config = json.loads((worker_root / "config.json").read_text(encoding="utf-8"))
        configured = (config.get("component_paths") or {}).get("freecad")
        if configured: candidates.append(Path(str(configured)))
    except (OSError, ValueError, AttributeError): pass
    for executable in (shutil.which("FreeCADCmd"), shutil.which("freecadcmd")):
        if executable: candidates.append(Path(executable))
    for base in filter(None, (os.getenv("LOCALAPPDATA"), os.getenv("ProgramFiles"), os.getenv("ProgramW6432"))):
        folder = Path(base)
        for pattern in ("Programs/FreeCAD*/bin/FreeCADCmd.exe", "FreeCAD*/bin/FreeCADCmd.exe"):
            candidates.extend(folder.glob(pattern))
    return next((path.resolve() for path in candidates if path.is_file()), None)


def install_freecad(worker_root, root):
    progress(8, "Recherche de FreeCAD sur le Worker…")
    executable = find_freecad(worker_root)
    if not executable and os.name == "nt":
        winget = shutil.which("winget")
        if not winget:
            raise RuntimeError("FreeCAD est absent et winget n’est pas disponible. Installe FreeCAD puis clique sur Réparer.")
        call([winget, "install", "--id", "FreeCAD.FreeCAD", "-e", "--silent",
              "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"], timeout=7200)
        executable = find_freecad(worker_root)
        progress(88, "Installation Windows terminée, vérification de FreeCAD…")
    if not executable:
        raise RuntimeError("FreeCADCmd est introuvable. Installe FreeCAD sur ce Worker puis clique sur Réparer.")
    root.mkdir(parents=True, exist_ok=True)
    (root / ".disabled").unlink(missing_ok=True)
    (root / "freecad-path.txt").write_text(str(executable), encoding="utf-8")
    return f"FreeCAD associé au Worker : {executable}"


# Sonde exécutée dans le .venv du composant : Pillow importable et le moteur livré (laser_engine/)
# analyse un SVG minimal. Elle prouve que le Worker peut calculer, pas seulement que pip a réussi.
LASER_ENGINE_PROBE = (
    "import json, PIL, shapely, svgwrite, segno, laser_engine.cli as cli, laser_engine.assist as assist; "
    "result = cli.execute('parse', {'svg': '<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"10mm\" height=\"10mm\">"
    "<rect x=\"1\" y=\"1\" width=\"5\" height=\"5\" stroke=\"#ff0000\" fill=\"none\"/></svg>'}); "
    "drawing = assist.Drawing(10, 10); drawing.cut(assist.rect(1, 1, 5, 5)); "
    "print(json.dumps({'pillow': PIL.__version__, 'shapely': shapely.__version__, 'shapes': len(result['document']['shapes']), "
    "'assist': len(assist._finalize(drawing)['layers'])}))"
)


def laser_engine_outdated(root, spec):
    """Moteur prêt mais installé avant les bibliothèques actuelles (ex. sans shapely) : à compléter."""
    try:
        marker = json.loads((root / ".raphal-ready").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    packages = marker.get("packages") if isinstance(marker, dict) else None
    return not isinstance(packages, list) or not set(spec["packages"]) <= {str(item) for item in packages}


def install_laser_engine(worker_root, root, spec, upgrade=False):
    """Python isolé + Pillow et bibliothèques de dessin pour le moteur laser livré avec l'agent.

    ``upgrade`` : moteur déjà prêt que l'on complète ; son marqueur reste en place jusqu'à la vérification,
    si bien qu'un échec (réseau…) laisse le moteur laser utilisable comme avant."""
    progress(5, "Préparation du moteur laser (Python isolé, Pillow et bibliothèques de dessin)…")
    worker_dir = Path(__file__).resolve().parents[1]
    engine = worker_dir / "laser_engine"
    shipped = [engine / name for name in ("__init__.py", "toolpath.py", "cli.py", "assist.py", "script_sandbox.py", "assistant.py")]
    shipped.append(worker_dir / "runners" / "laser-engine.py")
    if not all(path.is_file() and not is_link(path) for path in shipped):
        raise RuntimeError("Le moteur laser manque au paquet Worker. Mets à jour le Worker puis relance l’installation.")
    if shutil.disk_usage(root.parent).free < 1024**3:
        raise RuntimeError("Au moins 1 Gio libre est nécessaire sur le disque du Worker pour le Python isolé et Pillow.")
    root.mkdir(parents=True, exist_ok=True)
    ready = root / ".raphal-ready"
    if is_link(ready):
        raise RuntimeError("Marqueur du moteur laser sous forme de lien ; installation refusée.")
    if not upgrade:
        ready.unlink(missing_ok=True)
    py = engine_environment(root)
    progress(45, "Environnement Python prêt, installation de Pillow, shapely, svgwrite et segno…")
    call([py, "-m", "pip", "install", "--upgrade", "pip"], cwd=root, timeout=900,
         progress_value=45, progress_label="Préparation de pip")
    call([py, "-m", "pip", "install", "--upgrade", *spec["packages"]], cwd=root, timeout=1800,
         progress_value=60, progress_label="Installation de Pillow (trame d’image) et des bibliothèques de dessin")
    progress(88, "Vérification du moteur laser dans son environnement…")
    try:
        report = json.loads(call([py, "-c", LASER_ENGINE_PROBE], cwd=worker_dir, timeout=120).strip().splitlines()[-1])
    except (RuntimeError, ValueError, IndexError) as error:
        raise RuntimeError("Le moteur laser ne démarre pas dans son environnement Python : " + str(error)[-800:]) from error
    if not isinstance(report, dict) or not report.get("pillow") or report.get("shapes") != 1 or report.get("assist") != 1:
        raise RuntimeError("Vérification du moteur laser échouée : Pillow, l’analyse SVG ou la bibliothèque de dessin "
                           "ne répond pas comme attendu.")
    ready.write_text(json.dumps({"component": "laser-engine", "packages": list(spec["packages"]),
                                 "pillow": str(report["pillow"])}), encoding="utf-8")
    progress(99, f"Moteur laser prêt (Pillow {report['pillow']}, shapely {report.get('shapely')}).")
    return (f"Moteur laser installé : Python isolé, Pillow {report['pillow']} et shapely {report.get('shapely')} "
            "prêts sur ce Worker.")


# ---------------------------------------------------------------- surveillance IA des graveuses (fire-watch)
# Composant components/fire-watch/ : .venv (ONNX Runtime, NumPy, OpenCV), models/<id>/{source.pt, model.onnx,
# provenance.json, LICENSE-AGPL-3.0.txt} et le marqueur .raphal-ready. Les poids ne viennent jamais du site :
# chaque Worker les télécharge à leur source épinglée, vérifie taille et SHA-256, puis les convertit lui-même
# dans un environnement jetable (.export-venv, supprimé après la conversion).
FIRE_WATCH_SHIPPED = ("__init__.py", "catalog.py", "engine.py", "server.py", "export_onnx.py",
                      "requirements-runtime-gpu.txt", "requirements-runtime-cpu.txt", "requirements-export.txt",
                      "licenses/AGPL-3.0.txt")
FIRE_WATCH_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# Environnement de conversion jetable : .export-venv, ou .export-venv-<id> si un reste de l'ancien est encore occupé.
FIRE_WATCH_EXPORT_VENV = re.compile(r"\.export-venv(-[0-9a-f]{8})?")
# Roues du runtime téléchargées avant de retirer l'autre variante d'ONNX Runtime ou d'en changer la version
# (supprimées après l'installation).
FIRE_WATCH_WHEELS = ".runtime-wheels"
# Dans FIRE_WATCH_WHEELS : écrit seulement quand pip download a terminé (roues lues et vérifiées par pip) ; un cache
# sans lui (téléchargement tué par une annulation, un délai, un arrêt brutal) est vidé avant la tentative suivante.
FIRE_WATCH_WHEELS_COMPLETE = ".download-complete.json"
# Paquet épinglé tel que le marqueur le consigne (ex. onnxruntime-gpu[cuda,cudnn]==1.30.0) : jamais une option pip.
FIRE_WATCH_PIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}(\[[A-Za-z0-9._,-]{1,100}\])?==[A-Za-z0-9][A-Za-z0-9.+!_-]{0,63}")
FIRE_WATCH_RUNTIME_BYTES = 2 * 1024**3       # ONNX Runtime GPU et ses bibliothèques CUDA : ≈ 1,3 Go
FIRE_WATCH_EXPORT_BYTES = 3 * 1024**3        # PyTorch CPU + Ultralytics, le temps de la conversion
FIRE_WATCH_EXPORT_TIMEOUT = 1800
FIRE_WATCH_TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
FIRE_WATCH_INPUT = [1, 3, 640, 640]
FIRE_WATCH_OUTPUT = [1, 6, 8400]
FIRE_WATCH_AGPL_NOTICE = (
    "Poids publiés par leur auteur sous licence AGPL-3.0 (texte joint : LICENSE-AGPL-3.0.txt). La conversion en ONNX "
    "réalisée sur ce Worker est une modification de ces poids au sens de la licence ; source.pt est le fichier "
    "d’origine, téléchargé à la source épinglée et vérifié. Le site Alpine Makers ne redistribue pas ces poids : "
    "chaque Worker les télécharge et les convertit lui-même. Toute redistribution de source.pt ou de model.onnx "
    "doit respecter l’AGPL-3.0. Modèles non validés pour une graveuse : protection supplémentaire, jamais une "
    "raison de laisser la machine sans surveillance humaine."
)
# Sonde exécutée dans le .venv d'analyse : charge le modèle sur le CPU, lit ses classes (métadonnée « names »,
# un dict Python sous forme de texte) et vérifie les formes d'entrée et de sortie sur une image nulle.
FIRE_WATCH_PROBE = (
    "import ast, json, sys, numpy, cv2, onnxruntime as ort; "
    "options = ort.SessionOptions(); options.intra_op_num_threads = 1; options.inter_op_num_threads = 1; "
    "session = ort.InferenceSession(sys.argv[1], sess_options=options, providers=['CPUExecutionProvider']); "
    "names = ast.literal_eval(session.get_modelmeta().custom_metadata_map.get('names', '{}')); "
    "entry = session.get_inputs()[0]; "
    "result = session.run(None, {entry.name: numpy.zeros((1, 3, 640, 640), dtype=numpy.float32)})[0]; "
    "print(json.dumps({'names': {str(key): str(value) for key, value in dict(names).items()}, "
    "'input': [value if isinstance(value, int) else str(value) for value in entry.shape], "
    "'output': [int(value) for value in result.shape], 'providers': list(ort.get_available_providers()), "
    "'onnxruntime': ort.__version__, 'numpy': numpy.__version__, 'opencv': cv2.__version__}))"
)


def _fire_watch_engine_dir():
    """Code d'analyse livré avec l'agent (worker_agent/fire_watch_engine/)."""
    return Path(__file__).resolve().parents[1] / "fire_watch_engine"


def _fire_watch_catalog():
    """Catalogue des modèles (importable sans NumPy), chargé depuis le dossier de l'agent."""
    try:
        if __package__ and "." in __package__:
            from ..fire_watch_engine import catalog
        else:
            from fire_watch_engine import catalog
    except ImportError as error:
        raise RuntimeError("Le catalogue de la surveillance IA manque au paquet Worker. Mets à jour le Worker puis "
                           "relance l’installation.") from error
    return catalog


def fire_watch_models(catalog=None):
    """Modèles épinglés du catalogue, contrôlés (identifiant, URL HTTPS, SHA-256, taille)."""
    catalog = catalog if catalog is not None else _fire_watch_catalog()
    raw = getattr(catalog, "MODELS", None)
    if isinstance(raw, dict):
        entries = [dict(value, id=value.get("id") or key) if isinstance(value, dict) else value for key, value in raw.items()]
    else:
        entries = list(raw or ())
    models = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("Catalogue de la surveillance IA invalide : mets à jour le Worker.")
        model_id = str(entry.get("id") or "")
        url = str(entry.get("url") or "")
        digest = str(entry.get("sha256") or "").lower()
        size = entry.get("size", entry.get("size_bytes"))
        if (not FIRE_WATCH_MODEL_ID.fullmatch(model_id) or not url.startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE_BYTES):
            raise RuntimeError("Catalogue de la surveillance IA invalide : mets à jour le Worker.")
        models.append({**entry, "id": model_id, "url": url, "sha256": digest, "size": size})
    if not models:
        raise RuntimeError("Catalogue de la surveillance IA vide : mets à jour le Worker.")
    return models


def _fire_watch_packages(catalog, profile, requirements=None):
    """Paquets épinglés du profil (catalogue ; à défaut, lignes du fichier de dépendances)."""
    packages = tuple(getattr(catalog, "RUNTIME_GPU_PACKAGES" if profile == "gpu" else "RUNTIME_CPU_PACKAGES", ()) or ())
    if not packages and requirements is not None and Path(requirements).is_file():
        packages = tuple(line.strip() for line in Path(requirements).read_text(encoding="utf-8").splitlines()
                         if line.strip() and not line.strip().startswith(("#", "-")))
    return [str(item) for item in packages]


def _fire_watch_requirements(engine, catalog, profile):
    """Fichier de dépendances épinglées du profil, livré avec le moteur (nom lu dans le catalogue)."""
    name = str(getattr(catalog, "RUNTIME_GPU_REQUIREMENTS" if profile == "gpu" else "RUNTIME_CPU_REQUIREMENTS", "")
               or f"requirements-runtime-{profile}.txt")
    if Path(name).name != name:
        raise RuntimeError("Catalogue de la surveillance IA invalide : mets à jour le Worker.")
    return engine / name


def fire_watch_marker(root):
    """Marqueur de la dernière installation réussie, ou None."""
    try:
        value = json.loads((Path(root) / ".raphal-ready").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("component") == "fire-watch" else None


def _fire_watch_marker_pins(marker):
    """Paquets épinglés consignés par un marqueur, s'ils sont tous de la forme nom==version ; sinon None."""
    listed = marker.get("packages") if isinstance(marker, dict) else None
    if not isinstance(listed, list) or not listed:
        return None
    if not all(isinstance(item, str) and FIRE_WATCH_PIN.fullmatch(item) for item in listed):
        return None
    return list(listed)


def fire_watch_installed_models(root):
    """Modèles consignés par le marqueur et présents sur disque : {model_id: chemin de model.onnx}."""
    root = Path(root)
    marker = fire_watch_marker(root)
    listed = marker.get("models") if marker else None
    result = {}
    if not isinstance(listed, dict):
        return result
    for model_id, facts in listed.items():
        if not isinstance(model_id, str) or not FIRE_WATCH_MODEL_ID.fullmatch(model_id) or not isinstance(facts, dict):
            continue
        path = root / "models" / model_id / "model.onnx"
        if path.is_file() and not is_link(path) and path.stat().st_size > 0:
            result[model_id] = path
    return result


def fire_watch_ready(root):
    """Prêt = marqueur valide, Python d'analyse présent et chaque modèle consigné présent sur disque."""
    root = Path(root)
    marker = fire_watch_marker(root)
    if marker is None or not python_in(root / ".venv").is_file():
        return False
    listed = marker.get("models") if isinstance(marker.get("models"), dict) else {}
    installed = fire_watch_installed_models(root)
    return bool(installed) and set(installed) == set(listed)


def fire_watch_status(root):
    """État du module pour l'inventaire : installed, incomplete (traces d'installation) ou missing."""
    root = Path(root)
    if fire_watch_ready(root):
        return "installed"
    if root.is_dir() and (any((root / name).exists() for name in (".venv", ".raphal-ready", ".alpine-managed.json"))
                          or any(FIRE_WATCH_EXPORT_VENV.fullmatch(child.name) for child in root.iterdir())):
        return "incomplete"
    return "missing"


def fire_watch_outdated(root, catalog=None, hardware=None):
    """Installation prête mais qui ne couvre plus le catalogue (paquets, modèles) ou restée sur le CPU
    alors que le Worker a maintenant un GPU compatible : « Installer » la complète."""
    marker = fire_watch_marker(root)
    if marker is None or marker.get("profile") not in {"gpu", "cpu"}:
        return True
    try:
        catalog = catalog if catalog is not None else _fire_watch_catalog()
        models = fire_watch_models(catalog)
        requirements = _fire_watch_requirements(_fire_watch_engine_dir(), catalog, marker["profile"])
    except RuntimeError:
        return True
    packages = marker.get("packages") if isinstance(marker.get("packages"), list) else []
    if not set(_fire_watch_packages(catalog, marker["profile"], requirements)) <= {str(item) for item in packages}:
        return True
    listed = marker.get("models") if isinstance(marker.get("models"), dict) else {}
    for model in models:
        facts = listed.get(model["id"])
        if not isinstance(facts, dict) or facts.get("pt_sha256") != model["sha256"]:
            return True
    if marker["profile"] == "cpu":
        return fire_watch_profile(hardware if hardware is not None else detect_hardware())["profile"] == "gpu"
    return False


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_pinned(url, destination, expected_size, expected_sha256, label, value=50):
    """Téléchargement d'un fichier épinglé : reprise (.download), taille bornée, SHA-256 vérifié.

    Un fichier déjà présent et conforme n'est pas retéléchargé (renvoie False). Un fichier dont l'empreinte
    diffère est supprimé, jamais utilisé.
    """
    destination = Path(destination)
    partial = destination.with_name(destination.name + ".download")
    for path in (destination.parent, destination, partial):
        if is_link(path):
            raise RuntimeError(f"{path.name} est un lien : téléchargement refusé.")
    if destination.is_file() and destination.stat().st_size == expected_size and sha256_file(destination) == expected_sha256:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    attempts = max(1, min(10, int(os.getenv("ALPINE_DL_RETRY_ATTEMPTS") or 4)))
    backoff = max(0.0, float(os.getenv("ALPINE_DL_RETRY_BACKOFF") or 3))
    for attempt in range(1, attempts + 1):
        offset = partial.stat().st_size if partial.is_file() else 0
        if offset > expected_size:
            partial.unlink()
            offset = 0
        if offset < expected_size:
            if shutil.disk_usage(destination.parent).free < expected_size - offset + DOWNLOAD_RESERVE_BYTES:
                raise RuntimeError("Espace disque insuffisant pour télécharger le modèle. Aucun fichier existant n’a été supprimé.")
            headers = {"User-Agent": "Raphal-Worker-Installer/1.0", "Accept": "application/octet-stream"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=90) as response:
                    resumed = bool(offset) and response.status == 206 and \
                        str(response.headers.get("Content-Range") or "").startswith(f"bytes {offset}-")
                    done = offset if resumed else 0
                    transfer = TransferProgress(label, initial=done, start=value, span=0)
                    with partial.open("ab" if resumed else "wb") as output:
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            done += len(chunk)
                            if done > expected_size:
                                raise RuntimeError(f"{label} : la source renvoie plus de données que la taille épinglée ; téléchargement refusé.")
                            output.write(chunk)
                            transfer.update(done, expected_size)
                    transfer.update(done, expected_size, force=True)
            except urllib.error.HTTPError as error:
                code = error.code
                error.close()
                if code != 416 or offset != expected_size:
                    if code in {408, 425, 429, 500, 502, 503, 504} and attempt < attempts:
                        progress(value, f"{label} : source occupée (HTTP {code}), nouvelle tentative…")
                        time.sleep(backoff * attempt)
                        continue
                    raise RuntimeError(f"{label} : téléchargement refusé par la source épinglée (HTTP {code}).") from error
            except (urllib.error.URLError, http.client.IncompleteRead, OSError) as error:
                if attempt < attempts:
                    progress(value, f"{label} : coupure réseau, reprise du téléchargement…")
                    time.sleep(backoff * attempt)
                    continue
                raise RuntimeError(f"{label} : téléchargement interrompu (réseau) ; la partie reçue est conservée pour "
                                   "la prochaine tentative.") from error
        if partial.is_file() and partial.stat().st_size == expected_size:
            break
        if attempt >= attempts:
            raise RuntimeError(f"{label} : téléchargement incomplet ; la partie reçue est conservée pour la prochaine tentative.")
    if sha256_file(partial) != expected_sha256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"{label} : empreinte SHA-256 différente de celle épinglée ; fichier refusé et supprimé.")
    os.replace(partial, destination)
    return True


def _short_path(path):
    """Forme 8.3 d'un chemin accentué (dossier utilisateur « Prêt ») pour les bibliothèques natives de la
    conversion et de l'analyse ; le fichier final peut ne pas exister encore (seul son dossier est converti)."""
    path = Path(path)
    return ascii_path(path) if path.exists() else str(Path(ascii_path(path.parent)) / path.name)


def _fire_watch_env(worker_dir, extra=None):
    """Environnement des processus du composant : le dossier de l'agent sur le chemin, rien d'hérité d'un venv."""
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "PYTHONSTARTUP", "__PYVENV_LAUNCHER__"}}
    environment.update({"PYTHONPATH": _short_path(worker_dir), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", **(extra or {})})
    return environment


def _write_json_file(target, value):
    return _write_managed_file(Path(target), json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def _read_json_file(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _reusable_fire_watch_model(folder, model):
    """Conversion précédente encore valable (même .pt épinglé, ONNX intact) : pas de nouvelle conversion."""
    onnx = folder / "model.onnx"
    provenance = _read_json_file(folder / "provenance.json")
    if not provenance or is_link(folder) or is_link(onnx) or not onnx.is_file():
        return None
    if provenance.get("weights_sha256") != model["sha256"] or provenance.get("onnx_sha256") != sha256_file(onnx):
        return None
    return provenance


def _export_report(output):
    for line in reversed(str(output or "").splitlines()):
        if line.startswith("FIRE_WATCH_EXPORT="):
            try:
                report = json.loads(line.split("=", 1)[1])
            except ValueError:
                break
            if isinstance(report, dict):
                return report
    raise RuntimeError("La conversion ONNX n’a pas rendu de rapport exploitable.")


def _remove_temporary_tree(root, target):
    """Dossier temporaire du composant (environnement de conversion, roues téléchargées) ; jamais au travers d'un lien."""
    if is_link(target):
        raise RuntimeError(f"{target.name} est un lien : suppression refusée.")
    if target.exists():
        remove_worker_tree(target, root)


def _remove_export_venvs(root):
    """Supprime les environnements de conversion restés d'une conversion interrompue (.export-venv et
    .export-venv-<id>) ; renvoie les noms de ceux encore occupés (fichier verrouillé), laissés en place."""
    busy = []
    if not root.is_dir():
        return busy
    for target in sorted(root.iterdir()):
        if not FIRE_WATCH_EXPORT_VENV.fullmatch(target.name):
            continue
        try:
            _remove_temporary_tree(root, target)
        except (OSError, ValueError):
            busy.append(target.name)
    return busy


def _export_fire_watch_models(worker_root, root, engine, catalog, pending, span_start=45, span_end=84):
    """Télécharge et convertit les modèles dans un venv jetable (PyTorch CPU + Ultralytics), supprimé ensuite."""
    worker_dir = engine.parent
    requirements_name = str(getattr(catalog, "EXPORT_REQUIREMENTS", "requirements-export.txt"))
    requirements = engine / requirements_name
    if Path(requirements_name).name != requirements_name or not requirements.is_file():
        raise RuntimeError("La liste des dépendances de conversion manque au paquet Worker. Mets à jour le Worker.")
    # Reste d'une conversion interrompue : supprimé s'il est libre. Un reste encore occupé (processus d'export
    # en fin d'arrêt, antivirus) n'est jamais fatal : la conversion se fait dans un environnement distinct.
    busy = _remove_export_venvs(root)
    export_root = root / ".export-venv"
    if export_root.name in busy:
        export_root = root / f".export-venv-{uuid.uuid4().hex[:8]}"
        progress(span_start, "Ancien environnement de conversion encore occupé : conversion dans un environnement temporaire "
                             "distinct ; l’ancien sera supprimé à une prochaine installation.")
    if shutil.disk_usage(root.parent).free < FIRE_WATCH_EXPORT_BYTES:
        raise RuntimeError("Au moins 3 Gio libres sont nécessaires pour l’environnement de conversion temporaire.")
    reports = {}
    progress(span_start, "Préparation de l’environnement de conversion temporaire (PyTorch CPU, Ultralytics) ; il sera supprimé ensuite…")
    try:
        base_python = ensure_engine_python(worker_root)
        call([str(base_python), "-m", "venv", str(export_root)], timeout=300, env=_fire_watch_env(worker_dir), low_priority=True)
        (export_root / "ultralytics").mkdir(parents=True, exist_ok=True)
        environment = _fire_watch_env(worker_dir, {
            # Jamais d'installation automatique ni de téléchargement implicite par Ultralytics ; conversion sur le CPU,
            # deux fils au plus, réglages Ultralytics rangés dans l'environnement jetable.
            "YOLO_AUTOINSTALL": "False", "YOLO_OFFLINE": "True", "YOLO_CONFIG_DIR": _short_path(export_root / "ultralytics"),
            "CUDA_VISIBLE_DEVICES": "", "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2",
        })
        export_python = _short_path(python_in(export_root))
        call([export_python, "-m", "pip", "install", "--upgrade", "pip"], cwd=root, timeout=900, env=environment, low_priority=True,
             progress_value=span_start, progress_label="Environnement de conversion : préparation de pip")
        # Index CPU de PyTorch : les roues « +cpu » testées, jamais la variante CUDA (plusieurs Go sous Linux).
        call([export_python, "-m", "pip", "install", "--extra-index-url", FIRE_WATCH_TORCH_CPU_INDEX, "-r", str(requirements)],
             cwd=root, timeout=3600, env=environment, low_priority=True,
             progress_value=span_start + 2, progress_label="Environnement de conversion : PyTorch CPU et Ultralytics (temporaire)")
        step = (span_end - span_start - 6) / max(1, len(pending))
        for index, model in enumerate(pending):
            label = str(model.get("label") or model["id"])
            folder = root / "models" / model["id"]
            value = span_start + 6 + index * step
            progress(value, f"{label} : téléchargement depuis la source épinglée…")
            download_pinned(model["url"], folder / "source.pt", model["size"], model["sha256"], label, value=value)
            output = folder / "model.onnx.partial"
            if is_link(output):
                raise RuntimeError("Le fichier de conversion est un lien : conversion refusée.")
            output.unlink(missing_ok=True)
            text = call([export_python, "-m", "fire_watch_engine.export_onnx", "--weights", _short_path(folder / "source.pt"),
                         "--output", _short_path(output), "--expect-sha256", model["sha256"], "--threads", "2"],
                        cwd=_short_path(worker_dir), timeout=FIRE_WATCH_EXPORT_TIMEOUT, env=environment, low_priority=True,
                        progress_value=value + step / 2, progress_label=f"{label} : conversion ONNX sur le CPU (priorité basse)")
            report = _export_report(text)
            if not output.is_file() or report.get("weights_sha256") != model["sha256"] or report.get("onnx_sha256") != sha256_file(output):
                raise RuntimeError(f"{label} : la conversion ONNX ne correspond pas au modèle épinglé ; rien n’a été installé.")
            reports[model["id"]] = report
    finally:
        try:
            _remove_temporary_tree(root, export_root)
        except (OSError, ValueError, RuntimeError):
            progress(span_end, "Environnement de conversion temporaire non supprimé (fichier occupé) ; il le sera à la prochaine installation.")
    return reports


def _probe_fire_watch_model(python, onnx, model, gpu, environment, root):
    label = str(model.get("label") or model["id"])
    try:
        report = json.loads(call([_short_path(python), "-c", FIRE_WATCH_PROBE, _short_path(onnx)], cwd=_short_path(root), timeout=180,
                                 env=environment, low_priority=True).strip().splitlines()[-1])
    except (RuntimeError, ValueError, IndexError) as error:
        raise RuntimeError(f"{label} : le modèle ne se charge pas dans l’environnement d’analyse : " + str(error)[-800:]) from error
    names = report.get("names") if isinstance(report, dict) else None
    if not isinstance(names, dict) or sorted(str(value).casefold() for value in names.values()) != ["fire", "smoke"]:
        raise RuntimeError(f"{label} : classes du modèle inattendues ({names!r}) ; seules « fire » et « smoke » sont acceptées.")
    expected = model.get("expected_names")
    if isinstance(expected, (list, tuple)):
        expected = dict(enumerate(expected))
    if isinstance(expected, dict) and {str(key): str(value).casefold() for key, value in expected.items()} != \
            {str(key): str(value).casefold() for key, value in names.items()}:
        raise RuntimeError(f"{label} : ordre des classes différent de celui du catalogue ; modèle refusé.")
    if report.get("input") != FIRE_WATCH_INPUT or report.get("output") != FIRE_WATCH_OUTPUT:
        raise RuntimeError(f"{label} : formes du modèle inattendues (entrée {report.get('input')}, sortie {report.get('output')}).")
    if gpu and "CUDAExecutionProvider" not in (report.get("providers") or []):
        raise RuntimeError("ONNX Runtime GPU installé sans fournisseur CUDA : relance l’installation (Réparer).")
    return report


def _write_fire_watch_provenance(folder, model, report, probe, catalog=None):
    agpl = "AGPL" in str(model.get("license") or "").upper()
    # Base commune du catalogue (clé « conversion » lue aussi par le service), complétée par les contrôles faits ici.
    base = {}
    builder = getattr(catalog, "build_provenance", None)
    if callable(builder):
        try:
            base = builder(model["id"], report)
        except (ValueError, KeyError, TypeError):
            base = {}
    provenance = {
        **(base if isinstance(base, dict) else {}),
        "model_id": model["id"], "label": model.get("label") or model["id"],
        "source": {key: model.get(key) for key in ("url", "repository", "revision", "sha256", "size")},
        "license": model.get("license"), "license_url": model.get("license_url"),
        "dataset": model.get("dataset"), "dataset_license": model.get("dataset_license"),
        "weights_sha256": report["weights_sha256"], "onnx_sha256": report["onnx_sha256"],
        "onnx_bytes": report.get("onnx_bytes"), "names": probe["names"],
        "input": probe["input"], "output": probe["output"],
        "export": {key: report.get(key) for key in ("ultralytics", "torch", "onnx", "opset", "imgsz", "simplify", "seconds")},
        "runtime_check": {key: probe.get(key) for key in ("onnxruntime", "numpy", "opencv")},
        "converted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "converted_by": "Worker Alpine Makers · composant fire-watch",
        "notice": FIRE_WATCH_AGPL_NOTICE if agpl else f"Licence : {model.get('license') or 'à vérifier'} ; consulte license_url.",
    }
    _write_json_file(folder / "provenance.json", provenance)
    return provenance


def _fire_watch_distributions(root):
    """Distributions présentes dans le .venv du composant (métadonnées lues, rien n'est importé ni exécuté)."""
    environment = Path(root) / ".venv"
    names = set()
    for site_packages in [environment / "Lib" / "site-packages", *environment.glob("lib/python*/site-packages")]:
        if not site_packages.is_dir() or is_link(site_packages):
            continue
        try:
            for distribution in importlib_metadata.distributions(path=[str(site_packages)]):
                names.add(str(distribution.metadata.get("Name") or "").strip().casefold().replace("_", "-"))
        except (OSError, ValueError):
            continue
    return names


def _onnxruntime_variant(profile):
    return "onnxruntime-gpu" if profile == "gpu" else "onnxruntime"


def _install_fire_watch_runtime(python, root, requirements, profile, swap, environment, restore=None, replacing=None):
    """ONNX Runtime du profil (+ NumPy, OpenCV). onnxruntime et onnxruntime-gpu fournissent le même module : l'autre
    variante part avant l'installation.

    ``swap`` (l'autre variante est en place, ou les versions épinglées du runtime en place changent ; ``replacing`` =
    nom de la distribution remplacée si ce n'est pas l'autre variante) : les roues du profil sont d'abord téléchargées
    dans un cache du composant, sans rien retirer tant que le réseau peut échouer ; le retrait et l'installation se
    font ensuite hors ligne. Un téléchargement qui échoue vide ce cache (pip réutiliserait sans les vérifier des roues
    tronquées) ; un cache que pip n'a pas fini de remplir (processus tué) est vidé avant la tentative suivante.
    ``restore`` = (fichier de dépendances ou liste de paquets épinglés, profil) de l'installation prête que l'on
    complète : pendant le remplacement son marqueur est retiré (un arrêt brutal laisse l'état « incomplete », jamais
    une surveillance annoncée prête sans runtime) ; un échec réinstalle ce runtime et remet le marqueur, sinon l'état
    reste « incomplete ». L'erreur est toujours relevée.
    """
    gpu = profile == "gpu"
    kind = "GPU" if gpu else "CPU"
    label = ("Installation d’ONNX Runtime GPU (CUDA 13, ≈ 1,3 Go), NumPy et OpenCV" if gpu
             else "Installation d’ONNX Runtime CPU, NumPy et OpenCV")
    removed = _onnxruntime_variant("cpu" if gpu else "gpu")
    current = replacing or removed
    uninstall = [python, "-m", "pip", "uninstall", "-y", removed]
    if not swap:
        # Autre variante absente : la commande de retrait est sans effet, l'installation se fait en ligne
        # (pip télécharge tout avant de remplacer quoi que ce soit).
        call(uninstall, cwd=root, timeout=600, env=environment, low_priority=True)
        call([python, "-m", "pip", "install", "-r", str(requirements)], cwd=root, timeout=7200, env=environment, low_priority=True,
             progress_value=20, progress_label=label)
        return
    wheels = root / FIRE_WATCH_WHEELS
    if is_link(wheels):
        raise RuntimeError("Le cache des roues du runtime est un lien : installation de la surveillance refusée.")
    complete = {"requirements_sha256": hashlib.sha256(Path(requirements).read_bytes()).hexdigest()}
    if wheels.exists() and _read_json_file(wheels / FIRE_WATCH_WHEELS_COMPLETE) != complete:
        # Reste d'un téléchargement tué (annulation, délai, arrêt brutal : aucun nettoyage n'a pu tourner) ou roues
        # d'autres dépendances : pip réutiliserait sans le vérifier un fichier tronqué (« File was already downloaded »).
        try:
            _remove_temporary_tree(root, wheels)
        except (OSError, ValueError, RuntimeError) as error:
            raise RuntimeError(f"Cache des roues d’un téléchargement interrompu impossible à vider (fichier occupé) : rien n’a été "
                               f"désinstallé ({current} reste en place). Réessaie dans un instant. ({type(error).__name__})") from error
    wheels.mkdir(exist_ok=True)
    try:
        call([python, "-m", "pip", "download", "-r", str(requirements), "-d", str(wheels)], cwd=root, timeout=7200, env=environment,
             low_priority=True, progress_value=20,
             progress_label=f"Téléchargement d’ONNX Runtime {kind}, NumPy et OpenCV avant le remplacement de {current}")
    except (RuntimeError, subprocess.SubprocessError) as error:
        # Une roue copiée à moitié (annulation, disque plein) serait reprise telle quelle à chaque tentative.
        try:
            _remove_temporary_tree(root, wheels)
            cache = "les fichiers partiels sont supprimés, la prochaine tentative repart de zéro"
        except (OSError, ValueError, RuntimeError):
            cache = "les fichiers partiels (fichier occupé) seront supprimés avant la prochaine tentative"
        raise RuntimeError(f"Téléchargement d’ONNX Runtime {kind} interrompu : rien n’a été désinstallé ({current} reste en place) ; "
                           f"{cache}. " + str(error)[-800:]) from error
    # Téléchargement terminé : pip a lu et vérifié chaque roue, le cache peut servir à une reprise hors ligne.
    _write_json_file(wheels / FIRE_WATCH_WHEELS_COMPLETE, complete)
    ready = root / ".raphal-ready"
    saved = fire_watch_marker(root) if restore is not None else None
    if saved is not None:
        ready.unlink(missing_ok=True)
    try:
        call(uninstall, cwd=root, timeout=600, env=environment, low_priority=True)
        call([python, "-m", "pip", "install", "--no-index", "--find-links", str(wheels), "-r", str(requirements)], cwd=root,
             timeout=3600, env=environment, low_priority=True, progress_value=34, progress_label=label + " (roues téléchargées, hors ligne)")
    except (RuntimeError, subprocess.SubprocessError) as error:
        detail = str(error)[-800:]
        if saved is None:
            raise RuntimeError(f"Installation d’ONNX Runtime {kind} échouée après le retrait de {current} : surveillance à "
                               "réinstaller (Réparer) ; les roues téléchargées sont conservées. " + detail) from error
        previous_runtime, previous_profile = restore
        # Liste de paquets épinglés (versions exactes du marqueur précédent) ou fichier de dépendances du profil.
        previous_source = (list(previous_runtime) if isinstance(previous_runtime, (list, tuple))
                           else ["-r", str(previous_runtime)])
        progress(35, f"Remplacement d’ONNX Runtime échoué : réinstallation du runtime précédent ({previous_profile.upper()})…")
        try:
            call([python, "-m", "pip", "uninstall", "-y", _onnxruntime_variant("cpu" if previous_profile == "gpu" else "gpu")],
                 cwd=root, timeout=600, env=environment, low_priority=True)
            call([python, "-m", "pip", "install", *previous_source], cwd=root, timeout=3600, env=environment,
                 low_priority=True, progress_value=36, progress_label="Réinstallation du runtime précédent")
        except (RuntimeError, subprocess.SubprocessError):
            raise RuntimeError(f"Installation d’ONNX Runtime {kind} échouée et runtime précédent non réinstallé : surveillance IA "
                               "hors service sur ce Worker jusqu’à une nouvelle installation (Réparer). " + detail) from error
        _write_json_file(ready, saved)
        raise RuntimeError(f"Installation d’ONNX Runtime {kind} échouée ; le runtime précédent ({previous_profile.upper()}) a été "
                           "réinstallé : la surveillance reste utilisable comme avant. " + detail) from error
    if saved is not None:
        # Runtime remplacé ; l'ancien marqueur (profil précédent) reste jusqu'à la vérification finale : un échec
        # plus loin laisse l'installation signalée à compléter (« Installer ») et le cache des roues sert à la reprise.
        _write_json_file(ready, saved)


def install_fire_watch(worker_root, root, upgrade=False):
    """Moteur local de la surveillance IA des graveuses : Python isolé + ONNX Runtime, modèles convertis sur place.

    GPU NVIDIA (CUDA 13, pilote 580+, Turing ou plus récent) si le Worker en a un, sinon CPU. ``upgrade`` :
    installation prête que l'on complète ; son marqueur reste en place jusqu'à la vérification finale, si bien
    qu'un échec (réseau…) laisse la surveillance utilisable comme avant. Passage CPU → GPU : les roues GPU sont
    téléchargées avant de retirer le runtime CPU, qui est réinstallé si le remplacement échoue ; s'il ne peut
    pas l'être, le marqueur est retiré (état « incomplete ») plutôt que d'annoncer une surveillance sans runtime.
    Même chemin quand une mise à niveau change les versions épinglées du runtime sans changer de variante (les
    versions exactes du marqueur précédent sont alors celles que l'on restaure).
    """
    progress(3, "Préparation de la surveillance IA des graveuses (analyse locale de la caméra, aucune image envoyée à un tiers)…")
    catalog = _fire_watch_catalog()
    models = fire_watch_models(catalog)
    engine = _fire_watch_engine_dir()
    missing = [name for name in FIRE_WATCH_SHIPPED if not (engine / name).is_file() or is_link(engine / name)]
    if missing:
        raise RuntimeError("Le moteur de surveillance manque au paquet Worker (" + ", ".join(missing[:3]) + "). "
                           "Mets à jour le Worker puis relance l’installation.")
    choice = fire_watch_profile(detect_hardware())
    gpu = choice["profile"] == "gpu"
    requirements = _fire_watch_requirements(engine, catalog, choice["profile"])
    packages = _fire_watch_packages(catalog, choice["profile"], requirements)
    root.mkdir(parents=True, exist_ok=True)
    ready = root / ".raphal-ready"
    for path in (ready, root / "models", root / FIRE_WATCH_WHEELS):
        if is_link(path):
            raise RuntimeError(f"{path.name} est un lien : installation de la surveillance refusée.")
    # Restes d'une conversion interrompue : place rendue avant le contrôle d'espace. Un reste encore occupé est
    # laissé en place (jamais fatal) ; la conversion utilisera alors un environnement temporaire distinct.
    if _remove_export_venvs(root):
        progress(4, "Ancien environnement de conversion encore occupé (fichier verrouillé) : laissé en place, il sera supprimé "
                    "à une prochaine installation.")
    previous = fire_watch_marker(root)
    previous_profile = previous.get("profile") if previous is not None else None
    # L'autre variante d'ONNX Runtime est en place (ou l'installation précédente était de l'autre profil) : ses roues
    # sont remplacées par téléchargement préalable puis installation hors ligne.
    swap = (_onnxruntime_variant("cpu" if gpu else "gpu") in _fire_watch_distributions(root)
            or (previous_profile in {"gpu", "cpu"} and previous_profile != choice["profile"]))
    # Même variante mais versions épinglées changées (ex. onnxruntime-gpu 1.30 → 1.31, autre cuDNN) : pip retirerait
    # l'ancienne version avant d'écrire la nouvelle, marqueur « prête » en place. Même chemin que le changement de
    # variante : téléchargement préalable, marqueur retiré pendant le remplacement, installation hors ligne, marqueur
    # remis après succès ou restauration des versions précédentes.
    previous_pins = _fire_watch_marker_pins(previous)
    previous_packages = previous.get("packages") if previous is not None else None
    repinned = (upgrade and not swap and previous_profile == choice["profile"]
                and set(packages) != {str(item) for item in (previous_packages if isinstance(previous_packages, list) else [])})
    reusable = {model["id"]: _reusable_fire_watch_model(root / "models" / model["id"], model) for model in models}
    pending = [model for model in models if not reusable[model["id"]]]
    runtime_bytes = FIRE_WATCH_RUNTIME_BYTES * (2 if swap or repinned else 1)
    needed = runtime_bytes + (FIRE_WATCH_EXPORT_BYTES if pending else 0)
    if shutil.disk_usage(root.parent).free < needed:
        raise RuntimeError(f"Au moins {needed // 1024**3} Gio libres sont nécessaires sur le disque du Worker "
                           f"({runtime_bytes // 1024**3} Gio pour ONNX Runtime"
                           + (" et ses roues téléchargées avant le remplacement" if swap or repinned else "")
                           + (", 3 Gio pour l’environnement de conversion temporaire" if pending else "")
                           + "). Aucun fichier existant n’a été supprimé.")
    if not upgrade:
        ready.unlink(missing_ok=True)
    progress(6, f"{choice['label']} · {choice['gpu_name']}" if gpu else f"{choice['label']} — {choice['reason']}")
    worker_dir = engine.parent
    environment = _fire_watch_env(worker_dir)
    python = engine_environment(root)
    call([python, "-m", "pip", "install", "--upgrade", "pip"], cwd=root, timeout=900, env=environment, low_priority=True,
         progress_value=15, progress_label="Préparation de pip")
    restore = None
    if upgrade and previous_profile in {"gpu", "cpu"}:
        # Versions changées sans changement de variante : le runtime précédent, ce sont les versions exactes de son
        # marqueur (le fichier de dépendances livré porte déjà les nouvelles).
        restore = ((previous_pins, previous_profile) if repinned and previous_pins
                   else (_fire_watch_requirements(engine, catalog, previous_profile), previous_profile))
    _install_fire_watch_runtime(python, root, requirements, choice["profile"], swap or repinned, environment, restore,
                                replacing=_onnxruntime_variant(choice["profile"]) if repinned else None)
    reports = _export_fire_watch_models(worker_root, root, engine, catalog, pending) if pending else {}
    installed, versions = {}, {}
    for index, model in enumerate(models):
        label = str(model.get("label") or model["id"])
        folder = root / "models" / model["id"]
        progress(86 + index, f"{label} : vérification dans l’environnement d’analyse…")
        converted = model["id"] in reports
        probe = _probe_fire_watch_model(python, folder / ("model.onnx.partial" if converted else "model.onnx"), model, gpu, environment, root)
        versions = {key: probe.get(key) for key in ("onnxruntime", "numpy", "opencv")}
        if converted:
            os.replace(folder / "model.onnx.partial", folder / "model.onnx")
            provenance = _write_fire_watch_provenance(folder, model, reports[model["id"]], probe, catalog)
        else:
            provenance = reusable[model["id"]]
        if "AGPL" in str(model.get("license") or "").upper():
            _write_managed_file(folder / "LICENSE-AGPL-3.0.txt", (engine / "licenses" / "AGPL-3.0.txt").read_bytes())
        installed[model["id"]] = {"pt_sha256": model["sha256"], "onnx_sha256": provenance["onnx_sha256"]}
    _write_json_file(ready, {"component": "fire-watch", "profile": choice["profile"], "packages": packages, "models": installed})
    try:
        _remove_temporary_tree(root, root / FIRE_WATCH_WHEELS)   # roues du remplacement : inutiles une fois l'installation vérifiée
    except (OSError, ValueError, RuntimeError):
        progress(98, "Roues téléchargées du runtime non supprimées (fichier occupé) ; elles le seront à la prochaine installation.")
    names = ", ".join(installed)
    progress(99, f"Surveillance IA prête : ONNX Runtime {versions.get('onnxruntime') or ''} ({'GPU' if gpu else 'CPU'}), modèles {names}.")
    return (f"Surveillance IA installée sur ce Worker : ONNX Runtime {'GPU (CUDA)' if gpu else 'CPU'}, "
            f"{len(installed)} modèle(s) converti(s) localement ({names}). "
            + ("" if gpu else choice["reason"] + " ")
            + "Protection supplémentaire seulement : elle ne remplace jamais la présence d’un opérateur.")


def component_ready(component, root):
    if component == "laser-engine":
        return (root / ".raphal-ready").is_file() and python_in(root / ".venv").is_file()
    if component == "fire-watch":
        return fire_watch_ready(root)
    if component in {"comfyui", "hunyuan3d"}:
        entrypoints = ("main.py",) if component == "comfyui" else ("worker.py",)
        return (
            (root / ".raphal-ready").is_file()
            and python_in(root / ".venv").is_file()
            and any((root / name).is_file() for name in entrypoints)
        )
    if component in BACKEND_MODULES:
        return (root / ".raphal-ready").is_file() and python_in(root / ".venv").is_file() and (root / "alpine_backend.py").is_file()
    if component == "orca-bridge":
        worker_root = root.parent.parent
        return (worker_root / "orca_core_bridge.ps1").is_file() and (root / ".raphal-ready").is_file()
    if component == "cuda-toolkit":
        marker = root / "cuda-path.txt"
        if not marker.is_file() or (root / ".disabled").exists():
            return False
        cuda_root = Path(marker.read_text(encoding="utf-8").strip())
        return cuda_root.is_dir() and (cuda_root / "bin" / ("nvcc.exe" if os.name == "nt" else "nvcc")).is_file()
    if component == "freecad":
        marker = root / "freecad-path.txt"
        return marker.is_file() and Path(marker.read_text(encoding="utf-8").strip()).is_file() and not (root / ".disabled").exists()
    if component == "orca":
        return (root / "orca-slicer.exe").is_file() and (root / "resources" / "profiles" / "BBL").is_dir()
    if component == "printguard":
        model_root = root / "models"
        if not model_root.is_dir():
            model_root = root / "_internal" / "models"
        required = ("encoder_float32.onnx", "encoder_float32.tflite", "metadata.json", "prototypes.json")
        return (root / "PrintGuard.exe").is_file() and all(
            (model_root / name).is_file() and (model_root / name).stat().st_size > 0 for name in required
        )
    return root.is_dir()


def manage(component, action):
    if component not in CATALOG or action not in {"install", "update", "repair", "uninstall"}:
        raise RuntimeError("Composant ou action non autorisé.")
    worker_root = Path(os.environ["ALPINE_WORKER_ROOT"]).resolve()
    local_sandbox.activate_from_config(worker_root)
    local_sandbox.validate_root(worker_root)
    payload = {"component_id": component}
    local_sandbox.refuse_unsafe_operation("component." + action, payload)
    decision = legal_compliance.check_operation(worker_root, "component." + action, payload)
    result = _manage(component, action)
    if action != "uninstall":
        legal_compliance.record_notices(worker_root, decision)
    return result


def _manage(component, action):
    if component not in CATALOG or action not in {"install", "update", "repair", "uninstall"}:
        raise RuntimeError("Composant ou action non autorisé.")
    worker_root = Path(os.environ["ALPINE_WORKER_ROOT"]).resolve()
    storage = WorkerStorage(worker_root)
    storage.initialize()
    # This installer is an isolated process: subprocesses (git/pip/builds) inherit
    # the same storage policy as the engines, including direct CLI installations.
    os.environ.update(storage.environment())
    tempfile.tempdir = None
    components = (worker_root / "components").resolve()
    if components.parent != worker_root:
        raise RuntimeError("Répertoire de composants Worker invalide.")
    components.mkdir(parents=True, exist_ok=True)
    root = components / component
    if is_link(root) or root.resolve().parent != components:
        raise RuntimeError("Le répertoire du composant est un lien ou sort de l’espace Worker.")
    if action != "uninstall":
        # Detect the actual target machine, not a profile supplied by a browser.
        profile = selected_profile(component)
        progress(1, profile["label"])
        root.mkdir(parents=True, exist_ok=True)
        (root / ".disabled").unlink(missing_ok=True)
        if is_link(root / ".alpine-managed.json"):
            raise RuntimeError("Marqueur de composant sous forme de lien ; installation annulee.")
        # Written BEFORE installation: failed/partial installations remain
        # identifiable without treating an arbitrary folder as Worker-owned.
        (root / ".alpine-managed.json").write_text(json.dumps({"owner": "Alpine Makers", "component": component}), encoding="utf-8")
    progress(2, f"{action.capitalize()} · {component}")
    if action == "uninstall":
        if component == "orca-bridge":
            remove_worker_file(worker_root / "orca_core_bridge.ps1", worker_root)
            if root.is_dir():
                remove_worker_tree(root, components)
            return "Bridge Orca désinstallé du Worker."
        if component == "cuda-toolkit":
            root.mkdir(parents=True, exist_ok=True)
            remove_worker_file(root / "cuda-path.txt", root)
            (root / ".disabled").write_text("disabled", encoding="utf-8")
            return "CUDA dissocié du Worker (le Toolkit système est conservé)."
        if component == "freecad":
            root.mkdir(parents=True, exist_ok=True)
            remove_worker_file(root / "freecad-path.txt", root)
            (root / ".disabled").write_text("disabled", encoding="utf-8")
            return "FreeCAD dissocié du Worker (l’application reste installée sur Windows)."
        if root.is_dir():
            # Models are independent components. Never cascade-delete weights or user data.
            # .alpine-ecartes contient des fichiers écartés d'un checkout : ils peuvent
            # appartenir à l'utilisateur, une désinstallation ne doit pas les emporter.
            preserved = {"models", ".cache", "input", "output", "user", QUARANTINE_DIRNAME}
            for child in root.iterdir():
                if child.name in preserved:
                    continue
                if is_link(child):
                    if child.is_symlink(): child.unlink()
                    else: child.rmdir()  # Windows junction: remove the link, not its target.
                elif child.is_dir():
                    if child.name == "_internal" and (child / "models").is_dir():
                        # PrintGuard bundles mandatory models under _internal on some releases.
                        for internal in child.iterdir():
                            if internal.name == "models": continue
                            if is_link(internal):
                                if internal.is_symlink(): internal.unlink()
                                else: internal.rmdir()
                            elif internal.is_dir():
                                remove_worker_tree(internal, child)
                            else:
                                remove_worker_file(internal, child)
                    else:
                        remove_worker_tree(child, root)
                else:
                    remove_worker_file(child, root)
            if not any(root.iterdir()):
                root.rmdir()
        return "Moteur désinstallé ; modèles et données utilisateur conservés."
    if action == "install" and component_ready(component, root):
        if component == "laser-engine" and laser_engine_outdated(root, CATALOG[component]):
            # Installé avant l'assistant de code : on complète le Python isolé (rien d'autre n'est touché).
            return install_laser_engine(worker_root, root, CATALOG[component], upgrade=True)
        if component == "fire-watch" and fire_watch_outdated(root):
            # Nouveau modèle ou paquet au catalogue, ou GPU devenu compatible : on complète, marqueur conservé.
            return install_fire_watch(worker_root, root, upgrade=True)
        if component == "cuda-toolkit":
            # A 12.8 marker from a previous universal installer is not evidence
            # that the compiler matches the newly selected Pascal profile.
            return install_cuda_toolkit(worker_root, root)
        if component in {"comfyui", "hunyuan3d", *BACKEND_MODULES}:
            try:
                ensure_cuda_torch(str(python_in(root / ".venv")), root, profile)
                profile_path = root / ".alpine-runtime-profile.json"
                if is_link(profile_path):
                    raise RuntimeError("Profil moteur lié : validation refusée.")
                profile_path.write_text(json.dumps(profile), encoding="utf-8")
                if component in BACKEND_MODULES:
                    install_backend_files(component, root)
            except Exception:
                (root / ".raphal-ready").unlink(missing_ok=True)
                raise
        progress(99, "Composant déjà installé et opérationnel.")
        return "Composant déjà installé."
    spec = CATALOG[component]
    if spec["kind"] == "git":
        return install_git(component, root, spec)
    if spec["kind"] == "git-backend":
        return install_git_backend(component, root, spec)
    if spec["kind"] == "orca-portable":
        return install_orca(worker_root, root)
    if spec["kind"] == "dashboard-file":
        return install_orca_bridge(worker_root, root)
    if spec["kind"] == "cuda-toolkit":
        return install_cuda_toolkit(worker_root, root)
    if spec["kind"] == "system-freecad":
        return install_freecad(worker_root, root)
    if spec["kind"] == "python-venv":
        return install_laser_engine(worker_root, root, spec)
    if spec["kind"] == "fire-watch":
        return install_fire_watch(worker_root, root)
    return install_printguard(worker_root, root)


if __name__ == "__main__":
    print(manage(sys.argv[1], sys.argv[2]))
