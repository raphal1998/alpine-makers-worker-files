"""Alpine Makers outbound worker agent; Ed25519 identity on Windows and Linux."""

from __future__ import annotations

import argparse
import ast
import base64
import ctypes
import json
import hashlib
import os
import re
import platform
import queue
import secrets
import shutil
import socket
import struct
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
import zipfile
from collections import deque
from pathlib import Path

if __package__:
    from . import worker_legal as legal_compliance, local_sandbox
    from .identity import Identity, configure as configure_identity
    from .local_control import local_loop, network_info
    from .storage_paths import WorkerStorage
    from .safety import comfyui_queue_busy, is_link, protect_credentials, release_comfyui_vram, release_comfyui_vram_when_idle, safe_relative_name, validate_archive, validate_server_url
    from .job_state import DurableJournal, TERMINAL, atomic_json, read_json, runner_active
    from .process_control import process_identity
    from .installer_process import InstallerScope, installation_commit, scope_record
    from .hardware_profiles import detect_hardware, runtime_profile
    from .equipment_runtime import EquipmentRelay
    from .installers.model_catalog import COMFYUI_CATALOG, COMFYUI_LORA_CATALOG, COMFYUI_BUNDLES, COMFYUI_UTILITY_MODELS, COMFYUI_REQUIREMENTS, HUNYUAN_CATALOG, HUNYUAN_REQUIREMENTS, HUNYUAN_SNAPSHOT_OPTIONS, AI3D_BACKENDS, AI3D_SNAPSHOTS
    from .installers import environment_maintenance
    from . import comfyui_assets
    from .checkpoint_baseline import checkpoint_architecture
else:
    import worker_legal as legal_compliance
    import local_sandbox
    from identity import Identity, configure as configure_identity
    from local_control import local_loop, network_info
    from storage_paths import WorkerStorage
    from safety import comfyui_queue_busy, is_link, protect_credentials, release_comfyui_vram, release_comfyui_vram_when_idle, safe_relative_name, validate_archive, validate_server_url
    from job_state import DurableJournal, TERMINAL, atomic_json, read_json, runner_active
    from process_control import process_identity
    from installer_process import InstallerScope, installation_commit, scope_record
    from hardware_profiles import detect_hardware, runtime_profile
    from equipment_runtime import EquipmentRelay
    from installers.model_catalog import COMFYUI_CATALOG, COMFYUI_LORA_CATALOG, COMFYUI_BUNDLES, COMFYUI_UTILITY_MODELS, COMFYUI_REQUIREMENTS, HUNYUAN_CATALOG, HUNYUAN_REQUIREMENTS, HUNYUAN_SNAPSHOT_OPTIONS, AI3D_BACKENDS, AI3D_SNAPSHOTS
    from installers import environment_maintenance
    import comfyui_assets
    from checkpoint_baseline import checkpoint_architecture

AGENT_VERSION = "1.41.1"
AGENT_CAPABILITIES = ("equipment_v2", "installation_inventory_v1", "model_catalog_v2", "installation_cancel_v1", "managed_engine_install_v1", "hardware_profiles_v1", "runtime_profile_inventory_v1", "printguard_camera_v1", "printguard_settings_v1", "printguard_autopause_v1", "comfyui_sampler_choice_v1", "comfyui_dit_v1", "worker_logs_v1", "ai3d_backends_v1", "equipment_http_printers_v1", "agent_disconnect_v1", "storage_audit_cancel_v1", "comfyui_extended_sampling_v1", "equipment_grbl_v1", "equipment_grbl_frame_loop_v1", "equipment_grbl_frame_laser_v1", "equipment_grbl_frame_laser_open_v1", "equipment_usb_camera_v1", "mesh_repair_v1", "laser_engine_v1", "laser_engine_raster_v1", "laser_engine_v2", "laser_assistant_v1", "laser_assistant_v2", "equipment_grbl_v2", "fire_watch_v1", "ai_accounts_v1")
AGENT_CAPABILITIES += ("legal_compliance_v1",)
# Maintenance des environnements (agent 1.35.17) : Python isolé et pip/wheel/setuptools des .venv gérés rafraîchis
# après « Mettre à jour Worker », jamais pendant un calcul (voir installers/environment_maintenance.py).
AGENT_CAPABILITIES += ("environment_maintenance_v1",)
AGENT_CAPABILITIES += ("comfyui_lora_v1",)
# Agent 1.36.0 : ressources ComfyUI ajoutées par le propriétaire (LoRA, embeddings, ControlNet, VAE : comfyui_assets.py)
# et chaînes LoRA étendues (jusqu'à 8, LoraLoaderModelOnly pour les familles DiT : safety.py).
AGENT_CAPABILITIES += ("comfyui_assets_v1", "comfyui_lora_v2")  # littéral : le site lit ces lignes sans importer l'agent
# Agent 1.37.0 : import d'un fichier envoyé par le propriétaire depuis son navigateur (rapatrié du dashboard par le canal
# signé du Worker, puis vérifié comme un téléchargement).
AGENT_CAPABILITIES += ("comfyui_asset_upload_v1",)
# Agent 1.41.0 : Stable Cascade (graphe à deux étages, safety._comfyui_cascade_graph) et architecture réelle de chaque
# checkpoint lue dans son en-tête (checkpoint_baseline.py), jointe au relevé : « base:<famille> », « ckpt:<nom> ».
AGENT_CAPABILITIES += ("comfyui_cascade_v1", "image_baselines_v1")
# Agent 1.37.0 : ControlNet / Control-LoRA dans une génération SD 1.5 / SDXL (image de contrôle du job, contours Canny
# facultatifs) ; les ControlNet installés voyagent avec le relevé comme les LoRA.
AGENT_CAPABILITIES += ("comfyui_controlnet_v1",)
# Agent 1.39.0 : Labo image — agrandissement, cartes de contrôle (contours, profondeur, pose) et détourage par les
# nœuds natifs de ComfyUI, graphes fixes validés par safety.validate_image_lab_parameters, modèles utilitaires
# épinglés (installers/model_catalog.py, COMFYUI_UTILITY_MODELS).
AGENT_CAPABILITIES += ("comfyui_image_lab_v1",)  # littéral : le site lit ces lignes sans importer l'agent
# Agent 1.40.0 : Assistant IA du site (site_assistant.py). Une question posée, sans outil ni exécution de code, avec le
# compte Claude / ChatGPT que le propriétaire a connecté sur ce Worker ; rien n'en reste dans le journal durable.
AGENT_CAPABILITIES += ("site_assistant_v1",)  # littéral : le site lit ces lignes sans importer l'agent
# La libération de mémoire s'appuie sur un script PowerShell : Windows seulement.
if os.name == "nt":
    AGENT_CAPABILITIES += ("resource_release_v1",)
# Fermer des dizaines d'applications puis mesurer la mémoire prend quelques dizaines
# de secondes ; au-delà, le script s'arrête de lui-même et rend son rapport partiel.
FREE_RESOURCES_TIMEOUT = 420
HEARTBEAT_SECONDS = 20
POLL_SECONDS = 3
# Maintenance des environnements demandée par « Mettre à jour Worker » : réessayée toutes les cinq minutes tant
# qu'un calcul, une installation ou un moteur l'empêche, pendant au plus 24 h, puis notée « reportée ».
ENVIRONMENT_MAINTENANCE_RETRY = 300
ENVIRONMENT_MAINTENANCE_DEADLINE = 24 * 3600
ENVIRONMENT_MAINTENANCE_PREFIX = "environment-maintenance-"   # identifiant interne, jamais une commande serveur
# Code de sortie d'une déconnexion demandée depuis le dashboard : un superviseur
# à jour (run_worker.ps1, unité systemd) s'arrête au lieu de relancer l'agent.
DISCONNECT_EXIT_CODE = 64
# Moteurs qui possèdent un processus à arrêter. Le convertisseur FreeCAD est un
# simple interrupteur logique (un processus par conversion) : le couper
# changerait son réglage au lieu d'arrêter quoi que ce soit.
DISCONNECT_RUNTIMES = ("image_generation", "ai3d", "printguard", "model-studio")
# Un moteur démarré depuis le site et dont le processus disparaît (plantage du
# pilote graphique, mémoire) est relancé par l'agent : au plus trois fois par
# heure, jamais dans les 30 s qui suivent (le pilote peut encore se remettre).
RUNTIME_RESTART_DELAY = 30
# Filet pour la VRAM : un moteur image qui n'a plus rien calculé depuis dix minutes
# rend le modèle qu'il garde en mémoire vidéo (le runner le fait déjà en fin de job).
IMAGE_ENGINE_IDLE_RELEASE_SECONDS = 600
RUNTIME_RESTART_LIMIT = 3
RUNTIME_RESTART_WINDOW = 3600
ALLOWED_COMMANDS = {
    "tool.install", "tool.uninstall", "tool.update", "tool.repair", "tool.configure",
    "tool.start", "tool.stop", "equipment.install", "equipment.command",
    "equipment.test", "equipment.relay.start", "equipment.relay.stop", "printguard.camera", "printguard.settings",
    "printguard.autopause",
    "model.install", "model.uninstall", "model.update", "model.repair",
    "component.install", "component.uninstall", "component.update", "component.repair",
    "agent.update", "agent.restart", "agent.disconnect", "worker.purge", "job.run", "job.cancel",
    "storage.audit", "system.free_resources", "agent.maintain_environments",
    # Ressources ComfyUI du propriétaire (agent 1.36.0) : descripteur strictement validé par installers/asset_installer.py.
    "asset.install", "asset.uninstall", "asset.scan",
    # Comptes IA (agent 1.35.0) : comptes Claude Code / Codex de ce Worker, voir ai_accounts.py.
    "ai_accounts.status", "ai_accounts.add", "ai_accounts.remove", "ai_accounts.logout", "ai_accounts.login",
    "ai_accounts.login_input", "ai_accounts.login_cancel",
    # Assistant IA du site (agent 1.40.0) : charge exacte {provider, account_id, system, prompt}, voir site_assistant.py.
    "site_assistant.ask",
}
# Saisie d'un code, annulation et état d'une connexion : jamais derrière une file (connexion en attente,
# mise à jour ou redémarrage de l'agent demandé entre-temps).
AI_ACCOUNTS_UNQUEUED = {"ai_accounts.status", "ai_accounts.login_input", "ai_accounts.login_cancel"}
AI_ACCOUNTS_QUEUED = {"ai_accounts.login", "ai_accounts.logout", "ai_accounts.remove", "ai_accounts.add"}
# Cycle de vie de l'agent : attend les commandes en cours ; annule les connexions de comptes IA une fois accepté.
AGENT_LIFECYCLE_COMMANDS = {"agent.update", "agent.restart", "agent.disconnect"}


def exit_process(code):
    """Fin immédiate de l'agent (mise à jour, redémarrage, déconnexion, suppression) avec le code ``code``.

    Sous Windows, ``os._exit`` passe par ExitProcess, qui prévient chaque DLL chargée après avoir tué les autres fils.
    Le pilote de la caméra USB (DirectShow, lecture continue en MJPEG) peut alors attendre pour toujours un fil déjà tué :
    constaté le 2 octobre 2026, agent 1.35.5 figé à un seul fil après ``agent.update``, superviseur en attente et
    Worker hors ligne jusqu'à un arrêt forcé. TerminateProcess ne prévient aucune DLL ; le système ferme quand même
    les fichiers, le port série et la caméra. ``os._exit`` reste la voie ailleurs et si l'appel échoue."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 - console absente : rien à vider
            pass
    if os.name == "nt":
        try:
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)   # prototypes propres à cet appel
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
            kernel32.TerminateProcess.restype = wintypes.BOOL
            kernel32.TerminateProcess(kernel32.GetCurrentProcess(), int(code))
        except Exception:  # noqa: BLE001 - os._exit ci-dessous
            pass
    os._exit(code)


def ensure_normal_process_priority():
    """Remonte l'agent en priorité NORMALE s'il a été lancé en dessous (Windows ; ailleurs rien).

    Une tâche planifiée créée sans ``-Priority`` (configure_autostart.ps1) lance l'agent en BELOW_NORMAL : tous ses
    fils (envoi GRBL, caméra USB, boucle de la surveillance IA) et les processus qu'il lance sans classe explicite
    passaient alors derrière n'importe quel travail du PC. Ne baisse jamais une classe plus haute. Renvoie
    ``(avant, après)`` (codes Windows) ou ``None``."""
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)   # prototypes propres à cet appel
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetPriorityClass.argtypes = (wintypes.HANDLE,)
        kernel32.GetPriorityClass.restype = wintypes.DWORD
        kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.SetPriorityClass.restype = wintypes.BOOL
        handle = kernel32.GetCurrentProcess()
        before = int(kernel32.GetPriorityClass(handle))
        if before not in (0x40, 0x4000):        # IDLE, BELOW_NORMAL : seuls cas relevés
            return before, before
        if not kernel32.SetPriorityClass(handle, 0x20):   # NORMAL_PRIORITY_CLASS
            return before, before
        return before, int(kernel32.GetPriorityClass(handle))
    except Exception:  # noqa: BLE001 - priorité inchangée : l'agent fonctionne comme avant
        return None


def ai_accounts_queue_key(command, payload):
    """File d'un compte IA (connexion, déconnexion, suppression, ajout) : « ai_accounts:<fournisseur>:<compte|new> »."""
    if command not in AI_ACCOUNTS_QUEUED:
        return None
    payload = payload if isinstance(payload, dict) else {}
    account = "new" if command == "ai_accounts.add" else str(payload.get("account_id") or "")[:40]
    return f"ai_accounts:{str(payload.get('provider') or '')[:16]}:{account}"


SITE_ASSISTANT_COMMAND = "site_assistant.ask"


def site_assistant_queue_key(command, payload):
    """File d'une question de l'Assistant IA du site : une CLI à la fois par compte, « site_assistant:<fournisseur>:<compte> »."""
    if command != SITE_ASSISTANT_COMMAND:
        return None
    payload = payload if isinstance(payload, dict) else {}
    return f"site_assistant:{str(payload.get('provider') or '')[:16]}:{str(payload.get('account_id') or '')[:40]}"


def site_assistant_journal_response(response):
    """Reçu durable d'une question à l'Assistant IA : ni question ni réponse sur le disque du Worker.

    Le journal (worker-journal.sqlite3) n'est jamais purgé : la vraie réponse reste en mémoire jusqu'à sa remise au
    site. Si l'agent redémarre avant cette remise, seul ce reçu part : le site annonce alors de renvoyer la question."""
    if isinstance(response, dict) and response.get("ok"):
        return {"ok": False, "error": "Réponse non conservée par le Worker (redémarrage avant sa remise) : renvoie ta question."}
    return {"ok": False, "error": "Question à l’assistant sans réponse (détail non conservé par le Worker) : renvoie-la."}


TOOL_HANDLERS = {
    "image-generation": "comfyui", "image_generation": "comfyui",
    "3d-generation": "hunyuan3d", "ai3d": "hunyuan3d",
    "converter": "freecad", "printguard": "printguard", "model-studio": "orca",
    # Alpine Laser Studio : moteur de parcours (runners/laser-engine.py), un processus par job.
    "laser-studio": "laser-engine",
}
# Outils « à la demande » : un interrupteur logique (fiche runtime enabled), un processus par calcul,
# aucun service résident à démarrer, surveiller ou arrêter.
ON_DEMAND_TOOLS = ("converter", "laser-studio")
HUNYUAN_MODELS = {model_id: (folder, *HUNYUAN_REQUIREMENTS[model_id]) for model_id, (_, folder) in HUNYUAN_CATALOG.items()}


class AgentError(RuntimeError): pass


def _ascii_path(path):
    """Windows 8.3 form of a path when it contains non-ASCII characters (engines with narrow C paths)."""
    text = str(path)
    if os.name != "nt" or text.isascii():
        return text
    import ctypes
    buffer = ctypes.create_unicode_buffer(len(text) + 260)
    if ctypes.windll.kernel32.GetShortPathNameW(text, buffer, len(buffer)) and buffer.value.isascii():
        return buffer.value
    return text


class InstallationCancelled(AgentError):
    """Raised only before launch or after the entire private group has exited."""


CANCELLABLE_INSTALL_COMMANDS = {f"{kind}.{action}" for kind in ("tool", "component", "model") for action in ("install", "repair", "update")} | {"asset.install"}
ASSET_REPORT_LIMIT = 400
# LoRA annoncés par le relevé périodique (le site garde 500 modèles par Worker, tous moteurs confondus).
LORA_INVENTORY_LIMIT = 300


def run(command, timeout=20, env=None):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, env=env)


def nvidia_inventory():
    query = "index,name,uuid,memory.total,memory.free,utilization.gpu,temperature.gpu,driver_version"
    result = run(["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"])
    if result.returncode:
        raise AgentError("nvidia-smi indisponible : un GPU NVIDIA et son pilote sont obligatoires.")
    gpus = []
    for line in result.stdout.splitlines():
        values = [value.strip() for value in line.split(",")]
        if len(values) < 8: continue
        gpus.append({"index": int(values[0]), "name": values[1], "uuid": values[2], "vendor": "NVIDIA",
                     "vram_total_mb": int(values[3]), "vram_free_mb": int(values[4]),
                     "utilization_percent": int(values[5]), "temperature_c": int(values[6]), "driver_version": values[7]})
    if not gpus: raise AgentError("Aucun GPU NVIDIA détecté.")
    detected = {gpu["index"]: gpu for gpu in detect_hardware().get("gpus", [])}
    for gpu in gpus:
        # Unknown compute capability stays unknown; do not infer it from a name
        # or the driver's advertised maximum CUDA version.
        gpu["compute_capability"] = detected.get(gpu["index"], {}).get("compute_capability")
    cuda = run(["nvidia-smi"]).stdout
    import re
    match = re.search(r"CUDA Version:\s*([\d.]+)", cuda)
    return gpus, match.group(1) if match else ""


def memory_info():
    if os.name == "nt":
        result = run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize,FreePhysicalMemory)|ConvertTo-Json -Compress"])
        try:
            value = json.loads(result.stdout); return {"total_mb": int(value["TotalVisibleMemorySize"]) // 1024, "free_mb": int(value["FreePhysicalMemory"]) // 1024}
        except Exception: return {}
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, raw = line.split(":", 1); values[key] = int(raw.strip().split()[0]) // 1024
        return {"total_mb": values.get("MemTotal", 0), "free_mb": values.get("MemAvailable", 0)}
    except Exception: return {}


class FireGuardFallback:
    """Garde de départ GRBL quand le service de surveillance IA n'a pas pu être créé.

    Elle relit seulement ce que le service a persisté dans data/fire_watch/ : un verrou « incendie » non
    acquitté reste appliqué, et une machine réglée en arrêt automatique avec « départ exigeant une
    surveillance opérationnelle » ne démarre pas sans surveillance. Un fichier illisible n'est jamais
    interprété comme une situation sûre.
    """

    def __init__(self, root, error=""):
        self.directory = Path(root) / "data" / "fire_watch"
        self.error = str(error or "")

    def _read(self, name):
        try:
            value = json.loads((self.directory / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def is_latched(self, equipment_id):
        latches = self._read("latches.json")
        if latches is None:
            return True
        latches = latches.get("latches") if isinstance(latches.get("latches"), dict) else latches
        # « * » : verrou global posé par le service quand ses verrous étaient illisibles.
        return bool(latches.get(str(equipment_id)) or latches.get("*"))

    def start_refusal(self, equipment_id, command, laser_on):
        suffix = " Le service de surveillance IA de ce Worker est indisponible : mets à jour ou redémarre le Worker."
        if self.is_latched(equipment_id):
            return ("Arrêt incendie non acquitté sur cette machine (ou état des verrous illisible) : tout départ est refusé "
                    "jusqu’à l’acquittement par un opérateur." + suffix)
        stored = self._read("config.json")
        if stored is None:
            return ("Surveillance incendie indisponible : réglages illisibles, départ laser refusé." + suffix
                    if command == "stream" or laser_on else None)
        configs = stored.get("configs") if isinstance(stored.get("configs"), dict) else {}
        config = configs.get(str(equipment_id)) if isinstance(configs.get(str(equipment_id)), dict) else {}
        if (config.get("mode") == "auto_stop" and config.get("require_healthy_to_start") is not False
                and (command == "stream" or laser_on)):
            return "Surveillance incendie indisponible : cette machine exige une surveillance opérationnelle pour démarrer." + suffix
        return None


class Agent:
    def __init__(self, config_path: Path):
        # The supervisor starts the agent with a relative config path.  Resolve
        # it once so every generated path remains absolute even when a runtime
        # changes its own working directory.
        self.config_path = config_path.resolve()
        self.root = self.config_path.parent
        local_sandbox.activate_from_config(self.root)
        local_sandbox.validate_root(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = self._read_config()
        configure_identity(self.root, self.config)
        WorkerStorage(self.root, self.config).initialize()
        self.cancelled = set()
        self.command_threads = set()
        self.command_lock = threading.Lock()
        self.storage_audit_lock = threading.Lock()
        self.free_resources_lock = threading.Lock()
        # Maintenance des environnements (Python isolé, pip des .venv gérés) : une seule à la fois, lancée en
        # arrière-plan après une mise à jour de l'agent ou par la commande agent.maintain_environments.
        self.environment_maintenance_lock = threading.Lock()
        self.environment_maintenance_running = False
        self.environment_maintenance_refreshing_python = False
        self.environment_maintenance_deferred = ""
        self.environment_maintenance_thread = None
        self.environment_maintenance_retry = ENVIRONMENT_MAINTENANCE_RETRY
        self.environment_maintenance_deadline = ENVIRONMENT_MAINTENANCE_DEADLINE
        self.restart_requested = threading.Event()
        self.exit_code_pending = None   # 75 (mise à jour, redémarrage), 64 (déconnexion), 0 (suppression)
        self.lifecycle_commands = 0     # mise à jour, redémarrage, déconnexion ou suppression acceptés, en cours
        self.stop_requested = threading.Event()
        # Vrai dès que les moteurs sont arrêtés pour une déconnexion : plus aucun
        # heartbeat ni équipement, le temps de livrer le résultat puis de sortir.
        self.disconnecting = False
        # Suivi des moteurs tombés alors qu'ils devaient tourner (voir _runtime_watchdog).
        self.runtime_watch = {}
        # Inactivité du moteur image (voir _image_engine_idle_release).
        self.image_engine_idle_since = 0.0
        self.image_engine_released = False
        self.main_loop_seen = 0.0
        self.reconnect_requested = threading.Event()
        self.local_cleanup_active = False
        self.config_lock = threading.RLock()
        self.inventory_lock = threading.Lock()
        self.inventory_snapshot = {}
        self.inventory_updated_at = 0.0
        self.inventory_thread = None
        self.config_stamp = None
        self.component_locks = {}
        self.command_tails = {}
        self.lifecycle_tail = None
        # agent.update / restart / disconnect → files de comptes IA reçues avant elles : attendues seulement APRÈS les
        # contrôles de refus et l'annulation des connexions (1.35.1), pour qu'un refus ne coupe aucune connexion.
        self.lifecycle_ai_tails = {}
        self.inventory_revision = 0
        self.runtime_command_context = threading.local()
        self.equipment_relay = EquipmentRelay(self)
        self.printguard_live = {}
        self.command_burst_until = 0.0
        self.log_cursors = {}
        self.log_pending = []
        self.log_recent = {}
        self.log_watch_thread = None
        self.printguard_live_lock = threading.Lock()
        self.printguard_live_thread = None
        self.inflight_commands = set()
        self.completed_commands = deque(maxlen=4096)
        self.pending_command_results = {}
        self.install_cancel_events = {}
        self.result_delivery_lock = threading.Lock()
        self.maintenance_lock = threading.RLock()
        self.maintenance_active = False
        self.maintenance_count = 0
        self.maintenance_recovery = {}
        self.job_threads = {}
        # Comptes IA (agent 1.35.0) : connexions en cours, commande → session (processus de la CLI), pour que
        # ai_accounts.login_input / login_cancel les atteignent. Gestionnaire créé à la première commande.
        self.ai_logins = {}
        self.ai_logins_lock = threading.Lock()
        self.ai_accounts = None
        self.ai_accounts_lock = threading.Lock()
        # Assistant IA du site (agent 1.40.0) : gestionnaire créé à la première question.
        self.site_assistant = None
        self.site_assistant_lock = threading.Lock()
        self.journal = DurableJournal(self.root / "runtime" / "worker-journal.sqlite3")
        for command_id, entry in self.journal.records("commands").items():
            if entry.get("cancel_requested") and self._command_identity_matches(entry):
                self.install_cancel_events[command_id] = threading.Event()
                self.install_cancel_events[command_id].set()
            if entry.get("installer_launch_intent") and not entry.get("installer_exit_confirmed"):
                self.maintenance_recovery[command_id] = entry
                self.maintenance_active = True
            if self._is_local_receipt(command_id, entry):
                # Reçu interne (maintenance d'arrière-plan) : aucune commande serveur à conclure, jamais renvoyé.
                if entry.get("state") != "acked" and command_id not in self.maintenance_recovery:
                    self.journal.update("commands", command_id, state="acked")
                continue
            if entry.get("state") == "acked":
                self.completed_commands.append(command_id)
            elif entry.get("response") is not None:
                self.pending_command_results[command_id] = (entry["response"], entry.get("command", ""))
            elif entry.get("state") == "executing":
                # We cannot know whether an installer acted before the crash.
                # Do not repeat it on a redelivery. Jobs have their own receipt.
                response = {"ok": False, "error": "Exécution interrompue : résultat incertain, aucune répétition automatique."}
                self.journal.update("commands", command_id, state="uncertain", response=response)
                self.pending_command_results[command_id] = (response, entry.get("command", ""))
        # Surveillance IA des graveuses (flammes, fumée) : service optionnel qui tourne dans CE processus,
        # celui qui tient la liaison série des machines GRBL. Sa création ne lance aucun fil (voir loop())
        # et son échec n'empêche jamais l'agent de démarrer.
        self.agent_version = AGENT_VERSION
        self.fire_watch = None
        self.fire_watch_error = ""
        self.fire_watch_wanted = False
        self._create_fire_watch()

    # ------------------------------------------------------------ surveillance IA des graveuses
    def _create_fire_watch(self):
        """Crée le service de surveillance et le pose comme garde de départ du relais d'équipements."""
        try:
            if __package__:
                from .fire_watch import FireWatch
            else:
                from fire_watch import FireWatch
            watch = FireWatch(self)
            try:
                watch.agent_version = AGENT_VERSION   # aussi lisible par le service via agent.agent_version
            except AttributeError:
                pass
        except Exception as error:
            self.fire_watch = None
            self.fire_watch_error = f"{type(error).__name__} : {str(error)[:300]}"
            # Sans service, les verrous « incendie » et les réglages enregistrés restent appliqués aux départs.
            self.equipment_relay.fire_guard = FireGuardFallback(self.root, self.fire_watch_error)
            print(f"Surveillance IA des graveuses indisponible ({type(error).__name__}) ; l’agent continue sans elle.", flush=True)
            return None
        self.fire_watch, self.fire_watch_error = watch, ""
        self.equipment_relay.fire_guard = watch
        return watch

    def _start_fire_watch(self):
        self.fire_watch_wanted = True
        watch = self.fire_watch
        if watch is None:
            return False
        try:
            watch.start()
            return True
        except Exception as error:
            self.fire_watch_error = f"{type(error).__name__} : {str(error)[:300]}"
            print(f"Surveillance IA des graveuses non démarrée ({type(error).__name__}) ; l’agent continue sans elle.", flush=True)
            return False

    def _stop_fire_watch(self, timeout=5.0):
        """Arrête les fils et le moteur d'analyse (borné) ; jamais d'exception vers l'appelant."""
        watch = self.fire_watch
        if watch is None:
            return
        try:
            watch.stop(timeout=timeout)
        except Exception as error:
            print(f"Arrêt de la surveillance IA incomplet ({type(error).__name__}).", flush=True)

    def _suspend_fire_watch(self, reason):
        """Opération sur le composant fire-watch : seuls les moteurs d'analyse s'arrêtent (fichiers du .venv et des
        modèles libérés, sans redémarrage pendant l'opération). La boucle d'analyse continue : motif « maintenance »,
        réaction de perte et garde de départ restent appliquées à un job GRBL qui démarrerait entre-temps.

        Renvoie « engines » (moteurs suspendus), « service » (service sans suspend_engines : arrêt complet, ancien
        comportement) ou None (aucun service).
        """
        watch = self.fire_watch
        if watch is None:
            return None
        suspend = getattr(watch, "suspend_engines", None)
        if callable(suspend) and callable(getattr(watch, "resume_engines", None)):
            try:
                suspend(reason)
                return "engines"
            except Exception as error:
                print(f"Suspension des moteurs de surveillance IA impossible ({type(error).__name__}) ; "
                      "service arrêté le temps de l’opération.", flush=True)
        self._stop_fire_watch()
        return "service"

    def _resume_fire_watch(self, paused):
        """Fin de l'opération sur le composant : moteurs relancés (ils relisent le composant à jour) ; jamais d'exception."""
        watch = self.fire_watch
        if paused == "engines" and watch is not None:
            try:
                watch.resume_engines()
                return
            except Exception as error:
                print(f"Reprise des moteurs de surveillance IA impossible ({type(error).__name__}) ; redémarrage du service.",
                      flush=True)
                self._stop_fire_watch()
                paused = "service"
        if paused == "service" and self.fire_watch_wanted and not self.stop_requested.is_set():
            self._start_fire_watch()

    def active_grbl_jobs(self):
        """Noms des machines GRBL de ce Worker dont un job (gravure, cadrage, test) est en cours."""
        relay = getattr(self, "equipment_relay", None)
        devices = getattr(relay, "devices", None)
        if not isinstance(devices, dict):
            return []
        with relay.lock:
            items = list(devices.items())
        active = []
        for identifier, device in items:
            job = getattr(device, "grbl_job", None)
            thread = getattr(device, "grbl_thread", None)
            if (isinstance(job, dict) and job.get("active")) or (thread is not None and thread.is_alive()):
                spec = getattr(device, "spec", None)
                name = spec.get("name") if isinstance(spec, dict) else None
                active.append(str(name or identifier)[:80])
        return active

    def _assert_no_grbl_job(self, refusal):
        """Refuse ce qui couperait la liaison série ou la surveillance pendant un job GRBL (``refusal`` : début du message)."""
        machines = self.active_grbl_jobs()
        if machines:
            raise AgentError(f"{refusal} : un job GRBL est en cours sur {', '.join(machines)} ; l’opération couperait la "
                             "liaison série et la surveillance IA en pleine gravure. Attends la fin du job ou arrête-le, puis réessaie.")

    @staticmethod
    def _is_local_receipt(command_id, entry):
        """Reçu du journal sans commande serveur (maintenance des environnements lancée par l'agent lui-même) :
        jamais de progression, de résultat ni de réconciliation envoyés pour lui."""
        return bool((entry or {}).get("local")) or str(command_id or "").startswith(ENVIRONMENT_MAINTENANCE_PREFIX)

    def _command_identity(self):
        return {"worker_id": str(self.config.get("worker_id") or ""), "server_url": str(self.config.get("server_url") or "")}

    def _command_identity_matches(self, entry):
        return all(entry.get(key) == value for key, value in self._command_identity().items())

    def cancel_installation(self, command_id):
        if not self._valid_execution_id(command_id):
            return
        with self.command_lock:
            entry = self.journal.get("commands", command_id) or {}
            if entry and (not self._command_identity_matches(entry) or entry.get("state") in {"acked", "completed", "failed", "cancelled"}):
                return
            if entry.get("command") and entry["command"] not in CANCELLABLE_INSTALL_COMMANDS and entry["command"] != "storage.audit":
                return
            self.journal.update("commands", command_id, **self._command_identity(),
                                state=entry.get("state", "received"), cancel_requested=True)
            self.install_cancel_events.setdefault(command_id, threading.Event()).set()

    def _install_cancel_requested(self, command_id):
        event = self.install_cancel_events.get(str(command_id))
        if not event or not event.is_set():
            return False
        entry = self.journal.get("commands", str(command_id)) or {}
        return self._command_identity_matches(entry) and entry.get("cancel_requested") is True

    def _check_install_cancel(self, item):
        if item.get("command") in CANCELLABLE_INSTALL_COMMANDS and self._install_cancel_requested(item.get("command_id")):
            raise InstallationCancelled("Installation annulée avant son démarrage ; composants existants conservés.")

    def _handle_heartbeat_commands(self, response, identity):
        if identity != self._command_identity():
            return  # A response for the previous association must not cancel new work.
        for command_id in response.get("commands_cancel_requested") or []:
            self.cancel_installation(str(command_id))
        for job_id in response.get("jobs_cancel_requested") or []:
            self.cancel_job(str(job_id))
        for item in response.get("commands_pending") or []:
            self._dispatch_command(item)

    def reload_config(self):
        """Pick up a successful re-pair without retaining a revoked token.

        Ignore partial/invalid files: pairing replaces config.json atomically,
        but a user may also be editing it while the supervisor is running.
        """
        with self.config_lock:
            try:
                info = self.config_path.stat()
                stamp = (info.st_mtime_ns, info.st_size)
                if stamp == self.config_stamp:
                    return
                value = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                return
            if not isinstance(value, dict) or not all(value.get(key) for key in ("server_url", "worker_id", "token")):
                return
            self.config = value
            self.config_stamp = stamp

    def _read_config(self):
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError): return {}

    def save(self):
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.config, indent=2), encoding="utf-8")
        os.replace(temporary, self.config_path)
        if os.name != "nt": os.chmod(self.config_path, 0o600)

    @property
    def detached_marker(self):
        return self.root / ".worker-detached"

    @property
    def disconnected_marker(self):
        return self.root / ".worker-disconnected"

    @staticmethod
    def _boot_marker():
        """Identifie le démarrage courant de la machine, sans secret ni réseau."""
        try:
            if os.name == "nt":
                # Instance propre : le prototype par défaut de ctypes (c_long, 32 bits signés) tronquait la valeur après
                # 24,8 jours sans redémarrage, et le marqueur de déconnexion expirait à tort.
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.GetTickCount64.restype = ctypes.c_uint64
                uptime = kernel32.GetTickCount64() / 1000.0
                return {"boot_time": round(time.time() - uptime)}
            return {"boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()}
        except (OSError, AttributeError, ValueError):
            return {}

    def disconnect_still_active(self):
        """Une déconnexion ne vaut que jusqu'au prochain démarrage de la machine."""
        try:
            saved = json.loads(self.disconnected_marker.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            saved = {}
        current = self._boot_marker()
        if isinstance(saved, dict) and saved.get("boot_id") and saved.get("boot_id") == current.get("boot_id"):
            return True
        if isinstance(saved, dict) and isinstance(saved.get("boot_time"), (int, float)) and isinstance(current.get("boot_time"), (int, float)):
            # L'heure de démarrage recalculée varie de quelques secondes.
            if abs(saved["boot_time"] - current["boot_time"]) <= 120:
                return True
        self.disconnected_marker.unlink(missing_ok=True)
        return False

    @property
    def server(self):
        try:
            return validate_server_url(self.config.get("server_url"))
        except ValueError as error:
            raise AgentError(str(error)) from error

    def request(self, path, method="GET", payload=None, authenticated=True):
        if authenticated:
            self.reload_config()
        config = self.config
        if authenticated:
            configure_identity(self.root, config)
        server = self.server
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Accept": "application/json", "User-Agent": f"AlpineWorker/{AGENT_VERSION}"}
        if data is not None: headers["Content-Type"] = "application/json"
        if authenticated:
            headers["Authorization"] = f"Bearer {config.get('token','')}"
            headers["X-Worker-ID"] = str(config.get("worker_id") or "")
        request = protect_credentials(urllib.request.Request(server + path, data=data, headers=headers, method=method))
        context = None
        parsed = urllib.parse.urlsplit(server)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
            raise AgentError("HTTPS est obligatoire hors connexion locale.")
        try:
            with urllib.request.urlopen(request, timeout=15 if path == legal_compliance.CHECK_PATH else 45, context=context) as response:
                content = response.read(legal_compliance.MAX_NOTICE_BYTES + 1) if path == legal_compliance.CHECK_PATH else response.read()
                if path == legal_compliance.CHECK_PATH and len(content) > legal_compliance.MAX_NOTICE_BYTES:
                    raise AgentError("Réponse de conformité trop volumineuse.")
                return json.loads(content.decode("utf-8"))
        except urllib.error.HTTPError as error:
            try: message = json.loads(error.read().decode()).get("error")
            except Exception: message = str(error)
            failure = AgentError(message or str(error))
            failure.code = error.code
            raise failure from error

    def download_agent_update(self):
        """Replace only shipped agent files while preserving local state."""
        self.reload_config()
        headers = {
            "Accept": "application/zip",
            "User-Agent": f"AlpineWorker/{AGENT_VERSION}",
            "Authorization": f"Bearer {self.config.get('token','')}",
            "X-Worker-ID": str(self.config.get("worker_id") or ""),
        }
        request = protect_credentials(urllib.request.Request(
            self.server + "/api/worker-protocol/agent-package",
            headers=headers,
            method="GET",
        ))
        update_temp = WorkerStorage(self.root).path("temp")
        update_temp.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="alpine-worker-update-", dir=str(update_temp)) as temporary:
            temporary_root = Path(temporary)
            archive_path = temporary_root / "agent.zip"
            with urllib.request.urlopen(request, timeout=60) as response:
                content = response.read(16 * 1024 * 1024 + 1)
            if len(content) > 16 * 1024 * 1024:
                raise AgentError("Le paquet de mise à jour Worker est trop volumineux.")
            archive_path.write_bytes(content)
            with zipfile.ZipFile(archive_path) as archive:
                try:
                    validate_archive(archive, temporary_root / "unpacked", 128 * 1024 * 1024)
                except ValueError as error:
                    raise AgentError(str(error)) from error
                archive.extractall(temporary_root / "unpacked")
            source = temporary_root / "unpacked" / "alpine-makers-worker"
            if not (source / "agent.py").is_file():
                raise AgentError("Le paquet de mise à jour Worker est incomplet.")
            try:
                tree = ast.parse((source / "agent.py").read_text(encoding="utf-8-sig"))
                installed_version = next(ast.literal_eval(node.value) for node in tree.body
                                         if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "AGENT_VERSION" for target in node.targets))
                if not isinstance(installed_version, str) or len(installed_version.split(".")) != 3 or not all(part.isdigit() for part in installed_version.split(".")):
                    raise ValueError("invalid version")
                version_parts = tuple(map(int, installed_version.split(".")))
                if version_parts < tuple(map(int, AGENT_VERSION.split("."))):
                    raise ValueError("older version")
                if version_parts >= (1, 9, 0):
                    manifest = json.loads((source / "agent_manifest.json").read_text(encoding="utf-8"))
                    if manifest.get("agent_version") != installed_version or "equipment_v2" not in manifest.get("agent_capabilities", []):
                        raise ValueError("manifest version mismatch")
                    files = manifest.get("files")
                    required = {"agent.py", "safety.py", "job_state.py", "process_control.py", "equipment_runtime.py", "equipment_signing.py", "equipment_video_relay.py"}
                    if version_parts >= (1, 12, 0):
                        required.update({"identity.py", "identity_setup.py"})
                    if version_parts >= (1, 13, 0):
                        required.update({"installers/model_catalog.py", "hunyuan_worker.py"})
                    if version_parts >= (1, 14, 0):
                        required.update({"installer_process.py", "storage_paths.py", "installers/component_installer.py"})
                        required_capabilities = {"installation_cancel_v1", "managed_engine_install_v1"}
                        if not required_capabilities.issubset(manifest.get("agent_capabilities", [])):
                            raise ValueError("manifest installation capabilities missing")
                    if not isinstance(files, dict) or not required.issubset(files):
                        raise ValueError("manifest incomplete")
                    if version_parts >= (1, 15, 0):
                        if not {"hardware_profiles.py", "installers/python_runtime.py"}.issubset(files) or "hardware_profiles_v1" not in manifest.get("agent_capabilities", []):
                            raise ValueError("manifest hardware profiles incomplete")
                    if version_parts >= (1, 17, 0):
                        backend_files = {"ai3d_backends/open3d_lab_runner.py", "ai3d_backends/hunyuan3d_21_runner.py", "ai3d_backends/shim_diso.py",
                                         "ai3d_backends/shim_torchmcubes.py", "installers/requirements-open3d-lab.txt", "installers/requirements-hunyuan3d-21.txt"}
                        if not backend_files.issubset(files) or "ai3d_backends_v1" not in manifest.get("agent_capabilities", []):
                            raise ValueError("manifest 3D backends incomplete")
                    if version_parts >= (1, 35, 10):
                        if not {"worker_legal.py", "legal_policy.json", "local_sandbox.py"}.issubset(files) or "legal_compliance_v1" not in manifest.get("agent_capabilities", []):
                            raise ValueError("manifest legal preflight incomplete")
                    if version_parts >= (1, 35, 11):
                        if not {"legal_evidence.json", "legal_sources/INDEX.json"}.issubset(files):
                            raise ValueError("manifest documentary notices incomplete")
                    if version_parts >= (1, 35, 17):
                        if "installers/environment_maintenance.py" not in files or "environment_maintenance_v1" not in manifest.get("agent_capabilities", []):
                            raise ValueError("manifest environment maintenance incomplete")
                    for name, digest in files.items():
                        member = source / name
                        if not member.resolve().is_relative_to(source.resolve()) or not member.is_file() or hashlib.sha256(member.read_bytes()).hexdigest() != digest:
                            raise ValueError("manifest file mismatch")
            except (OSError, SyntaxError, ValueError, TypeError, StopIteration) as error:
                raise AgentError("Paquet Worker invalide ou incomplet. Redémarrez le dashboard à jour avant de réessayer la mise à jour.") from error
            preserved = {"config.json", "storage.json", "cache", "temp", "components", "runtime", "logs", "data", "outputs", "workspace",
                         "jobs", "config", "licenses", "python_path.txt", "git_path.txt", "supervisor.lock", ".worker-detached", "equipment-transfers"}
            # Files the previous package shipped and the new one does not: they
            # are removed after the copy so the installation mirrors the
            # dashboard's worker_agent folder. Only files listed by the former
            # manifest are candidates: anything the user added is never touched.
            previous_files = set()
            try:
                previous = json.loads((self.root / "agent_manifest.json").read_text(encoding="utf-8"))
                if isinstance(previous, dict) and isinstance(previous.get("files"), dict):
                    previous_files = {str(name) for name in previous["files"]}
            except (OSError, ValueError):
                previous_files = set()
            shipped_files = set(files) if isinstance(files, dict) else set()
            for item in source.iterdir():
                if item.name.casefold() in preserved:
                    continue
                destination = self.root / item.name
                if item.is_dir():
                    shutil.copytree(item, destination, dirs_exist_ok=True)
                else:
                    staged = destination.with_name(destination.name + ".update")
                    shutil.copy2(item, staged)
                    os.replace(staged, destination)
            removed = self._remove_stale_agent_files(previous_files - shipped_files, preserved) if shipped_files else []
        # Les programmes dont le Worker dépend (Python isolé, pip des environnements) sont rafraîchis par
        # l'agent redémarré, quand aucun calcul ne tourne : la demande survit dans runtime/ (dossier préservé),
        # y compris quand la version installée est déjà la plus récente.
        maintenance_requested = True
        try:
            environment_maintenance.write_request(self.root, AGENT_VERSION, installed_version)
        except OSError as error:
            maintenance_requested = False
            print(f"Demande de maintenance des environnements non enregistrée ({type(error).__name__}).", flush=True)
        # This process still runs its previously imported code. Only the next
        # heartbeat after supervised restart proves the new version is active.
        return {"updated": True, "version": installed_version, "running_version": AGENT_VERSION,
                "restart_required": True, "activation_pending": True, "removed_files": removed,
                "environment_maintenance_requested": maintenance_requested}

    def _remove_stale_agent_files(self, names, preserved):
        """Delete shipped files dropped from the package; never local state, never outside the root."""
        removed = []
        root = self.root.resolve()
        for name in sorted(names):
            relative = Path(str(name).replace("\\", "/"))
            if not relative.parts or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                continue
            if relative.parts[0].casefold() in preserved or relative.name == "agent_manifest.json":
                continue
            target = (self.root / relative)
            try:
                resolved = target.resolve()
                if not resolved.is_relative_to(root) or target.is_symlink() or not target.is_file():
                    continue
                target.unlink()
                removed.append(relative.as_posix())
                parent = target.parent
                while parent != self.root and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
            except OSError:
                continue
        return removed

    def upload_artifact(self, job_id, path):
        self.reload_config()
        path = Path(path).resolve()
        headers = {"Authorization": f"Bearer {self.config.get('token','')}", "X-Worker-ID": str(self.config.get("worker_id") or ""),
                   "Content-Type": "application/octet-stream", "Content-Length": str(path.stat().st_size),
                   "Accept": "application/json", "User-Agent": f"AlpineWorker/{AGENT_VERSION}"}
        target = f"{self.server}/api/worker-protocol/jobs/{urllib.parse.quote(job_id)}/artifacts/{urllib.parse.quote(path.name)}"
        with path.open("rb") as source:
            request = protect_credentials(urllib.request.Request(target, data=source, headers=headers, method="POST"))
            with urllib.request.urlopen(request, timeout=3600) as response:
                return json.loads(response.read().decode("utf-8"))["artifact"]

    def download_job_input(self, job_id, name, destination):
        self.reload_config()
        safe_name = Path(str(name)).name
        if safe_name != name:
            raise AgentError("Nom du fichier source invalide.")
        headers = {"Authorization": f"Bearer {self.config.get('token','')}",
                   "X-Worker-ID": str(self.config.get("worker_id") or ""),
                   "User-Agent": f"AlpineWorker/{AGENT_VERSION}"}
        target = f"{self.server}/api/worker-protocol/jobs/{urllib.parse.quote(job_id)}/inputs/{urllib.parse.quote(safe_name)}"
        request = protect_credentials(urllib.request.Request(target, headers=headers, method="GET"))
        total = 0
        with urllib.request.urlopen(request, timeout=3600) as response, Path(destination).open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk: break
                total += len(chunk)
                if total > 512 * 1024 * 1024:
                    raise AgentError("Fichier source trop volumineux.")
                output.write(chunk)
        if not total:
            raise AgentError("Le fichier source téléchargé est vide.")
        return Path(destination)

    def inventory(self):
        gpu_optional = self.config.get("gpu_optional") is True and platform.system() == "Linux"
        try:
            gpus, cuda = nvidia_inventory()
        except (AgentError, OSError, subprocess.SubprocessError, ValueError):
            if not gpu_optional:
                raise
            gpus, cuda = [], ""
        disk = shutil.disk_usage(self.root)
        hardware = {"os_name": platform.system(), "architecture": platform.machine(), "gpus": gpus,
                    "cuda_version": cuda, "driver_version": gpus[0]["driver_version"] if gpus else ""}
        tools, models = self.discover_components(hardware)
        return {"name": self.config.get("name") or socket.gethostname(), "hostname": socket.gethostname(),
                "os_name": platform.system(), "os_version": platform.version(), "architecture": platform.machine(),
                "agent_version": AGENT_VERSION, "agent_capabilities": list(AGENT_CAPABILITIES),
                "cpu": {"name": platform.processor(), "cores": os.cpu_count() or 1, "network": network_info()},
                "ram": memory_info(), "disk": {"total_bytes": disk.total, "free_bytes": disk.free},
                "gpus": gpus, "cuda_version": cuda, "driver_version": gpus[0]["driver_version"] if gpus else "",
                "gpu_optional": gpu_optional,
                "max_concurrent_jobs": int(self.config.get("max_concurrent_jobs") or 1),
                "current_jobs": 0, "status": "online", "tools": tools, "models": models}

    def _refresh_inventory(self):
        try:
            revision = self.inventory_revision
            snapshot = self.inventory()
            with self.inventory_lock:
                if revision == self.inventory_revision:
                    self.inventory_snapshot = snapshot
                    self.inventory_updated_at = time.monotonic()
        except Exception as error:
            # A disappearing model file, busy GPU driver or inaccessible engine
            # directory is NOT a lost network connection.
            print(f"Inventaire Worker indisponible ({type(error).__name__}); connexion maintenue.", flush=True)
        try:
            self._runtime_watchdog()
        except Exception as error:
            print(f"Surveillance des moteurs indisponible ({type(error).__name__}).", flush=True)
        try:
            self._image_engine_idle_release()
        except Exception as error:
            print(f"Libération de VRAM du moteur image reportée ({type(error).__name__}).", flush=True)

    def _image_engine_idle_release(self, now=None):
        """Rendre la VRAM d'un moteur image resté chargé alors que plus rien ne calcule.

        Le runner libère déjà la carte à la fin de chaque génération ; ce filet couvre
        ce qui lui échappe : agent redémarré, runner interrompu, ou modèle chargé hors
        du dashboard. Rien n'est demandé tant qu'un job tourne sur ce Worker, tant que
        le moteur a quelque chose dans sa file, ou tant que son état reste inconnu ;
        une seule demande par période d'inactivité.
        """
        now = time.time() if now is None else now
        if self.disconnecting or any(thread.is_alive() for thread in list(self.job_threads.values())):
            self.image_engine_idle_since, self.image_engine_released = 0.0, False
            return False
        if not self._runtime_running("image_generation"):
            self.image_engine_idle_since, self.image_engine_released = 0.0, False
            return False
        busy = comfyui_queue_busy()
        if busy:
            self.image_engine_idle_since, self.image_engine_released = 0.0, False
            return False
        if busy is None or self.image_engine_released:
            return False
        if not self.image_engine_idle_since:
            self.image_engine_idle_since = now
            return False
        if now - self.image_engine_idle_since < IMAGE_ENGINE_IDLE_RELEASE_SECONDS:
            return False
        self.image_engine_released = release_comfyui_vram()
        if self.image_engine_released:
            self.log("runtime.vram_released",
                     f"Moteur image sans calcul depuis {IMAGE_ENGINE_IDLE_RELEASE_SECONDS // 60} min : "
                     "VRAM de son modèle rendue ; la prochaine génération le rechargera.")
        return self.image_engine_released

    def _runtime_watchdog(self, now=None):
        """Relance un moteur dont le processus a disparu alors qu'il devait tourner.

        Exécuté sur le fil d'inventaire, jamais sur la boucle réseau. Un moteur
        arrêté depuis le site (intention à False) n'est jamais relancé ; un job
        actif repousse la relance au passage suivant.
        """
        now = time.time() if now is None else now
        if self.disconnecting:
            return
        with self.inventory_lock:
            installed = {str(tool.get("tool_id")): str(tool.get("status") or "") for tool in self.inventory_snapshot.get("tools", [])}
        for tool_id in DISCONNECT_RUNTIMES:
            intent = self.journal.get("runtime_intent", tool_id)
            watch = self.runtime_watch.setdefault(tool_id, {"down_since": None, "restarts": [], "exhausted": False})
            # L'intention « démarré depuis le site » vaut aussi après un redémarrage
            # du PC ou une reconnexion : un moteur installé mais absent est relancé.
            if not intent or intent.get("enabled") is not True or installed.get(tool_id) not in {"ready", "online"}:
                watch.update(down_since=None, exhausted=False)
                continue
            if self._runtime_running(tool_id):
                if watch["down_since"]:
                    print(f"Moteur {tool_id} de nouveau en service.", flush=True)
                watch.update(down_since=None, exhausted=False)
                continue
            if not watch["down_since"]:
                watch["down_since"] = now
                print(f"Moteur {tool_id} arrêté de façon inattendue ; relance dans {RUNTIME_RESTART_DELAY} s.", flush=True)
                continue
            if now - watch["down_since"] < RUNTIME_RESTART_DELAY:
                continue
            watch["restarts"] = [stamp for stamp in watch["restarts"] if now - stamp < RUNTIME_RESTART_WINDOW]
            if len(watch["restarts"]) >= RUNTIME_RESTART_LIMIT:
                if not watch["exhausted"]:
                    watch["exhausted"] = True
                    self.log("runtime.crashed", f"Moteur {tool_id} arrêté de façon inattendue {RUNTIME_RESTART_LIMIT} fois en une heure ; "
                             "plus de relance automatique. Vérifie le pilote graphique et la mémoire, puis démarre-le dans Mes Workers.", level="error")
                continue
            try:
                self.control_runtime(tool_id, "start")
            except AgentError as error:
                # Typiquement un job encore actif : on réessaie au passage suivant.
                print(f"Relance du moteur {tool_id} reportée ({error}).", flush=True)
                continue
            watch["restarts"].append(now)
            watch["down_since"] = None
            with self.inventory_lock:
                self.inventory_revision += 1
                self.inventory_updated_at = 0
            self.log("runtime.restarted", f"Moteur {tool_id} relancé après un arrêt inattendu ({len(watch['restarts'])}/{RUNTIME_RESTART_LIMIT} en une heure).", level="warning")
            self.reconnect_requested.set()

    def heartbeat_payload(self):
        """Do not run GPU, filesystem or process probes on the network loop."""
        with self.inventory_lock:
            if self.inventory_thread is None or not self.inventory_thread.is_alive():
                self.inventory_thread = threading.Thread(target=self._refresh_inventory, name="worker-inventory", daemon=True)
                self.inventory_thread.start()
            payload = dict(self.inventory_snapshot)
            stale = time.monotonic() - self.inventory_updated_at > HEARTBEAT_SECONDS * 2
        if stale:
            for field in ("tools", "models"):
                if field in payload:
                    payload[field] = [dict(item, status="unknown") for item in payload[field]]
        payload.update({"name": self.config.get("name") or socket.gethostname(),
                        "hostname": socket.gethostname(), "agent_version": AGENT_VERSION,
                        "agent_capabilities": list(AGENT_CAPABILITIES), "status": "online"})
        if self.config.get("identity_enabled"):
            own_temp = False
            try:
                try:
                    from .safety import is_link
                except ImportError:
                    from safety import is_link
                if is_link(self.root / "temp"):
                    raise OSError("Le temporaire du diagnostic ne doit pas être un lien.")
                with tempfile.TemporaryFile(dir=self.root / "temp") as probe:
                    probe.write(b"Alpine Worker health\n")
                    probe.flush()
                    probe.seek(0)
                    own_temp = probe.read() == b"Alpine Worker health\n"
            except OSError:
                pass
            payload.update(worker_id=self.config.get("worker_id"), timestamp=time.time())
            payload["security"] = {"health": {"main_loop": not self.stop_requested.is_set() and time.monotonic() - self.main_loop_seen < 90,
                                               "own_temp": own_temp, "inventory_fresh": not stale}}
        entries = self.journal.records("jobs")
        jobs = [{"job_id": job_id, "execution_id": item["execution_id"], "state": item.get("state", "uncertain"),
                 "engine_stop_confirmed": item.get("engine_stop_confirmed") is True,
                 **({"result": item["response"].get("result", {}), "error": item["response"].get("error", ""),
                     "message": item["response"].get("message", "")} if item.get("response") else {})}
                for job_id, item in entries.items() if not item.get("acked") and self._job_identity_matches(item)]
        payload.update(job_protocol_version=1, jobs_reconciliation=jobs[:128],
                       current_jobs=sum(1 for item in entries.values() if self._job_active(item)),
                       maintenance_active=self.maintenance_active)
        summary = self.environment_maintenance_summary()
        if summary:
            payload["environment_maintenance"] = summary
        payload["commands_reconciliation"] = [
            {"command_id": command_id, "state": item.get("state"),
             **({"result": item["response"]} if item.get("response") is not None else {})}
            for command_id, item in self.journal.records("commands").items()
            if item.get("state") != "acked" and not self._is_local_receipt(command_id, item)
        ][:128]
        return payload

    def _heartbeat_loop(self):
        delay = POLL_SECONDS
        disconnected = False
        self._start_printguard_live()
        self._start_log_watch()
        while not self.stop_requested.is_set():
            if self.disconnecting:
                # Moteurs déjà arrêtés : un heartbeat remettrait le Worker en ligne.
                self.stop_requested.wait(1)
                continue
            try:
                identity = self._command_identity()
                payload = self.heartbeat_payload()
                if self.config.get("identity_enabled"):
                    challenge = self.request("/api/worker-protocol/identity/challenge", "POST", {})
                    payload["security"]["challenge"] = challenge["nonce"]
                response = self.request("/api/worker-protocol/heartbeat", "POST", payload)
                if isinstance(response, dict):
                    self._handle_heartbeat_commands(response, identity)
                if disconnected:
                    print("Worker reconnecté au dashboard.", flush=True)
                disconnected = False
                delay = HEARTBEAT_SECONDS
            except Exception as error:
                print(f"Connexion Worker interrompue ({type(error).__name__}); nouvelle tentative automatique.", flush=True)
                disconnected = True
                delay = min(max(POLL_SECONDS, delay * 2), 30)
            self.reconnect_requested.wait(delay)
            self.reconnect_requested.clear()

    @staticmethod
    def _executable_from_path(value, names):
        if not value:
            return None
        candidate = Path(str(value)).expanduser()
        if candidate.is_file() and candidate.name.lower() in names:
            return candidate.resolve()
        if candidate.is_dir():
            for name in names:
                direct = candidate / name
                if direct.is_file():
                    return direct.resolve()
            for name in names:
                nested = next(candidate.glob(f"*/{name}"), None)
                if nested and nested.is_file():
                    return nested.resolve()
        return None

    def _orca_executable(self):
        if (self.root / "components/orca/.disabled").is_file():
            return None
        roots = self.config.get("component_paths") if isinstance(self.config.get("component_paths"), dict) else {}
        managed = self.root / "components" / "orca"
        if local_sandbox.enabled():
            return self._executable_from_path(WorkerStorage(self.root).component("orca"), {"orca-slicer.exe", "orcaslicer.exe"})
        return (
            self._executable_from_path(roots.get("orca"), {"orca-slicer.exe", "orcaslicer.exe"})
            or self._executable_from_path(managed, {"orca-slicer.exe", "orcaslicer.exe"})
            or next((Path(value).resolve() for value in (shutil.which("orca-slicer"), shutil.which("OrcaSlicer")) if value), None)
        )

    def _printguard_executable(self):
        if (self.root / "components/printguard/.disabled").is_file():
            return None
        roots = self.config.get("component_paths") if isinstance(self.config.get("component_paths"), dict) else {}
        managed = self.root / "components" / "printguard"
        if local_sandbox.enabled():
            return self._executable_from_path(WorkerStorage(self.root).component("printguard"), {"printguard.exe"})
        return (
            # Prefer the Worker-managed copy. Besides making updates reproducible,
            # its ASCII-only default path avoids native ML runtimes that still
            # mishandle non-ASCII Windows paths.
            self._executable_from_path(managed, {"printguard.exe"})
            or self._executable_from_path(roots.get("printguard") or os.getenv("PRINTGUARD_EXE"), {"printguard.exe"})
        )

    # ------------------------------------------------------------ tailles installées
    # Parcourir un venv (des dizaines de milliers de fichiers) prend quelques
    # secondes : la mesure est mise en cache et refaite au plus toutes les quinze
    # minutes, ou après une installation ou une désinstallation. Elle ne tourne
    # que sur le fil d'inventaire, jamais sur la boucle réseau.
    SIZE_CACHE_SECONDS = 900

    def _taille_dossier(self, path, exclure=()):
        """Octets occupés par un dossier ; `exclure` = sous-chemins relatifs laissés de côté."""
        path = Path(path)
        cle = (str(path), tuple(sorted(exclure)))
        cache = getattr(self, "_tailles", None)
        if cache is None:
            cache = self._tailles = {}
        maintenant = time.monotonic()
        connu = cache.get(cle)
        if connu and maintenant - connu[0] < self.SIZE_CACHE_SECONDS:
            return connu[1]
        total = 0
        if path.is_dir():
            racine = str(path)
            pile = [racine]
            while pile:
                courant = pile.pop()
                try:
                    with os.scandir(courant) as entrees:
                        for entree in entrees:
                            try:
                                if entree.is_symlink():
                                    continue
                                if entree.is_dir(follow_symlinks=False):
                                    relatif = os.path.relpath(entree.path, racine).replace("\\", "/")
                                    if relatif in exclure:
                                        continue
                                    pile.append(entree.path)
                                elif entree.is_file(follow_symlinks=False):
                                    total += entree.stat(follow_symlinks=False).st_size
                            except OSError:
                                continue
                except OSError:
                    continue
        cache[cle] = (maintenant, total)
        return total

    def _oublier_tailles(self):
        """Après une installation ou une suppression, la prochaine mesure repart de zéro."""
        self._tailles = {}

    def discover_components(self, hardware=None):
        """Detect supported engines from local, administrator-owned paths."""
        if __package__:
            from .installers.component_installer import component_ready, fire_watch_status, runtime_profile_health
        else:
            from installers.component_installer import component_ready, fire_watch_status, runtime_profile_health
        hardware = hardware if isinstance(hardware, dict) else detect_hardware()
        tools = {str(item.get("tool_id")): {**item, "status": "missing"} for item in self.config.get("tools", []) if isinstance(item, dict) and item.get("tool_id")}
        models = {(str(item.get("tool_id")), str(item.get("model_id"))): {**item, "status": "missing"} for item in self.config.get("models", []) if isinstance(item, dict) and item.get("tool_id") and item.get("model_id")}
        roots = self.config.get("component_paths") if isinstance(self.config.get("component_paths"), dict) else {}
        storage = WorkerStorage(self.root, self.config)
        comfy_root = storage.component("comfyui")
        comfy_ready = component_ready("comfyui", comfy_root)
        comfy_partial = comfy_root.is_dir() and any((comfy_root / name).exists() for name in ("main.py", ".venv", ".raphal-ready", ".alpine-managed.json"))
        if comfy_ready:
            tools["image_generation"] = {"tool_id": "image_generation", "engine_id": "comfyui", "status": "online" if self._runtime_running("image_generation") else "ready", "version": "local"}
        elif comfy_partial:
            tools["image_generation"] = {"tool_id": "image_generation", "engine_id": "comfyui", "status": "incomplete", "version": "local"}
        checkpoint_root = comfy_root / "models" / "checkpoints"
        known_models = {spec[0]: (model_id, *COMFYUI_REQUIREMENTS[model_id]) for (_, model_id), spec in COMFYUI_CATALOG.items()}
        # Les fichiers d'un ensemble rangés dans checkpoints/ (étages C et B de Stable Cascade) appartiennent à
        # l'ensemble relevé plus bas ; un modèle utilitaire du Labo image rangé là (SDPose) n'est pas un modèle de
        # génération : ni l'un ni l'autre ne sont des modèles à part.
        bundle_checkpoints = {filename for files in COMFYUI_BUNDLES.values() for subdir, filename, *_ in files if subdir == "checkpoints"}
        utility_checkpoints = {filename for spec in COMFYUI_UTILITY_MODELS.values() for subdir, filename, *_ in spec["files"] if subdir == "checkpoints"}
        for pattern in ("*.safetensors", "*.ckpt"):
            for path in checkpoint_root.glob(pattern):
                if path.name in utility_checkpoints or path.name in bundle_checkpoints:
                    continue
                model_id, minimum, recommended = known_models.get(path.name, (path.stem, 4096, 8192))
                models[("image_generation", model_id)] = {"tool_id": "image_generation", "model_id": model_id, "status": "ready" if path.stat().st_size > 0 else "incomplete", "size_bytes": path.stat().st_size, "min_vram_mb": minimum, "recommended_vram_mb": recommended,
                                                           "capabilities": ["text-to-image", "image-to-image", "base:" + (checkpoint_architecture(path) or "unknown"), "ckpt:" + path.name[:200]]}
        # LoRA (dossier principal et dossiers de extra_model_paths.yaml) : le modèle de base est lu dans l'en-tête
        # du fichier (mis en cache par taille et date), le nom exact du fichier voyage avec l'inventaire.
        known_loras = {spec[0]: model_id for (_, model_id), spec in COMFYUI_LORA_CATALOG.items()}
        try:
            lora_files = comfyui_assets.scan_assets(self.root, comfy_root, kinds={"lora", "controlnet"}) if comfy_root.is_dir() else []
        except Exception:  # noqa: BLE001 - un dossier de LoRA illisible ne fait jamais tomber le relevé du Worker
            lora_files = []
        # Les LoRA sont ajoutés en fin de relevé (voir plus bas) : leur nombre est borné et aucun ne peut prendre
        # la place d'un modèle, d'un ensemble ou d'un moteur dans l'inventaire.
        lora_models = []
        for item in lora_files:
            name = str(item.get("name") or "")
            if item.get("format") == "pickle" or not name or len(name) > 240:
                continue
            kind = "controlnet" if item.get("kind") == "controlnet" else "lora"
            model_id = known_loras.get(name, Path(name).stem[:150]) if kind == "lora" else Path(name).stem[:150]
            minimum, recommended = COMFYUI_REQUIREMENTS.get(model_id, (0, 0)) if kind == "lora" else (0, 0)
            lora_models.append({
                "tool_id": "image_generation", "model_id": model_id,
                "status": "ready" if item.get("format") == "safetensors" and item.get("size_bytes", 0) > 0 else "incomplete",
                "size_bytes": item.get("size_bytes", 0), "min_vram_mb": minimum,
                "recommended_vram_mb": recommended,
                "capabilities": [kind, "base:" + str(item.get("base_family") or "unknown"), "file:" + name],
            })
        # Ensembles multi-fichiers des familles DiT (agent 1.24.0) : prêts quand chaque fichier
        # (diffusion_models/, text_encoders/, vae/) est présent. Le fichier propre de l'ensemble
        # (le premier : diffusion_models/<modèle>) décide s'il est installé : encodeurs et VAE sont
        # partagés entre ensembles, et leur seule présence ne fait pas un ensemble « incomplet »
        # (1.25.1 : HiDream était annoncé tronqué à cause du T5 et du VAE posés par Chroma).
        for (_, model_id), files in COMFYUI_BUNDLES.items():
            paths = [comfy_root / "models" / subdir / filename for subdir, filename, *_ in files]
            present = [path for path in paths if path.is_file() and path.stat().st_size > 0]
            if not paths or paths[0] not in present:
                continue
            minimum, recommended = COMFYUI_REQUIREMENTS[model_id]
            models[("image_generation", model_id)] = {
                "tool_id": "image_generation", "model_id": model_id,
                "status": "ready" if len(present) == len(paths) else "incomplete",
                "size_bytes": sum(path.stat().st_size for path in present),
                "files_present": len(present), "files_total": len(paths), "primary_present": True,
                "min_vram_mb": minimum, "recommended_vram_mb": recommended,
                "capabilities": ["text-to-image", "image-to-image"],
            }
        # Modèles utilitaires du Labo image (agent 1.39.0) : leurs propres capacités, jamais « text-to-image ».
        for (_, model_id), spec in COMFYUI_UTILITY_MODELS.items():
            paths = [comfy_root / "models" / subdir / filename for subdir, filename, *_ in spec["files"]]
            present = [path for path in paths if path.is_file() and path.stat().st_size > 0]
            if not present:
                continue
            minimum, recommended = spec["vram"]
            models[("image_generation", model_id)] = {
                "tool_id": "image_generation", "model_id": model_id,
                "status": "ready" if len(present) == len(paths) else "incomplete",
                "size_bytes": sum(path.stat().st_size for path in present),
                "min_vram_mb": minimum, "recommended_vram_mb": recommended,
                "capabilities": ["utility", *spec["capabilities"]],
            }
        hunyuan_root = storage.component("hunyuan3d")
        hunyuan_ready = component_ready("hunyuan3d", hunyuan_root)
        hunyuan_partial = hunyuan_root.is_dir() and any((hunyuan_root / name).exists() for name in ("worker.py", "hy3dgen", "api_server.py", ".venv", ".raphal-ready", ".alpine-managed.json"))
        if hunyuan_ready:
            tools["ai3d"] = {"tool_id": "ai3d", "engine_id": "hunyuan3d", "status": "online" if self._runtime_running("ai3d") else "ready", "version": "local"}
        elif hunyuan_partial:
            tools["ai3d"] = {"tool_id": "ai3d", "engine_id": "hunyuan3d", "status": "incomplete", "version": "local"}
        for model_id, (folder, minimum, recommended, capabilities) in HUNYUAN_MODELS.items():
            marker = hunyuan_root / "models" / folder / ".raphal-ready"
            if marker.is_file():
                has_weights = any(path.stat().st_size > 0 for pattern in ("*.safetensors", "*.bin", "*.ckpt", "*.pt") for path in marker.parent.glob("**/" + pattern) if path.is_file())
                if model_id in HUNYUAN_SNAPSHOT_OPTIONS:
                    subfolder = HUNYUAN_SNAPSHOT_OPTIONS[model_id][1]
                    checkpoint = marker.parent / subfolder / "model.fp16.safetensors"
                    has_weights = checkpoint.is_file() and checkpoint.stat().st_size > 0 and (checkpoint.parent / "config.yaml").is_file()
                elif model_id in AI3D_SNAPSHOTS:
                    # Multi-file snapshots are ready only when every pinned file is present.
                    expected = [marker.parent / relative for relative in AI3D_SNAPSHOTS[model_id]["checks"]]
                    expected += [marker.parent / extra["path"] for extra in AI3D_SNAPSHOTS[model_id].get("extras", [])]
                    has_weights = bool(expected) and all(path.is_file() and path.stat().st_size > 0 for path in expected)
                    if has_weights and model_id in AI3D_BACKENDS:
                        has_weights = component_ready(AI3D_BACKENDS[model_id], self.root / "components" / AI3D_BACKENDS[model_id])
                models[("ai3d", model_id)] = {
                    "tool_id": "ai3d", "model_id": model_id, "status": "ready" if has_weights else "incomplete",
                    "min_vram_mb": minimum, "recommended_vram_mb": recommended,
                    "capabilities": capabilities,
                    # Place réellement occupée sur le disque : le site l'additionne
                    # pour afficher ce que l'IA locale prend sur ce Worker.
                    "size_bytes": self._taille_dossier(marker.parent),
                }
        freecad = self._freecad_executable()
        if freecad: tools["converter"] = {"tool_id": "converter", "engine_id": "freecad", "status": "online" if self._runtime_running("converter") else "ready", "version": "installed"}
        orca = self._orca_executable()
        orca_root = orca.parent if orca else self.root / "components" / "orca"
        if orca and component_ready("orca", orca_root):
            tools["model-studio"] = {"tool_id": "model-studio", "engine_id": "orca", "status": "online" if self._runtime_running("model-studio") else "ready", "version": "installed"}
        elif orca_root.is_dir() and any((orca_root / name).exists() for name in ("orca-slicer.exe", "resources", ".alpine-managed.json")):
            tools["model-studio"] = {"tool_id": "model-studio", "engine_id": "orca", "status": "incomplete", "version": "local"}
        printguard = self._printguard_executable()
        printguard_root = printguard.parent if printguard else self.root / "components" / "printguard"
        if printguard and component_ready("printguard", printguard_root):
            tools["printguard"] = {"tool_id": "printguard", "engine_id": "printguard", "status": "online" if self._runtime_running("printguard") else "ready", "version": "installed"}
        elif printguard_root.is_dir() and any((printguard_root / name).exists() for name in ("PrintGuard.exe", "models", "_internal", ".alpine-managed.json")):
            tools["printguard"] = {"tool_id": "printguard", "engine_id": "printguard", "status": "incomplete", "version": "local"}
        # Moteur laser : prêt = Python isolé du composant + marqueur ; « online » quand l'intention à la
        # demande est activée (comme le convertisseur) ; « incomplete » si l'installation a laissé des traces.
        laser_root = self.root / "components" / "laser-engine"
        if component_ready("laser-engine", laser_root):
            tools["laser-studio"] = {"tool_id": "laser-studio", "engine_id": "laser-engine", "status": "online" if self._runtime_running("laser-studio") else "ready", "version": "local"}
        elif laser_root.is_dir() and any((laser_root / name).exists() for name in (".venv", ".raphal-ready", ".alpine-managed.json")):
            tools["laser-studio"] = {"tool_id": "laser-studio", "engine_id": "laser-engine", "status": "incomplete", "version": "local"}
        for tool_id, engine in TOOL_HANDLERS.items():
            if tool_id in {"image-generation", "3d-generation"}:
                continue
            item = tools.setdefault(tool_id, {"tool_id": tool_id, "engine_id": engine, "status": "missing"})
            managed_root = self.root / "components" / engine
            engine_root = comfy_root if engine == "comfyui" else hunyuan_root if engine == "hunyuan3d" else managed_root
            ownership = None
            if engine in {"orca", "printguard"}:
                ownership = WorkerStorage(self.root, self.config).component_ownership(engine, executable=orca if engine == "orca" else printguard)
                engine_root = Path(ownership["engine_root"])
            installed = component_ready(engine, engine_root) or (engine == "freecad" and bool(freecad))
            # Files on disk, installed dependencies and runtime state are separate facts.
            engine_status = "installed" if installed else "incomplete" if item["status"] in {"ready", "online", "incomplete"} else "missing"
            modules = {}
            module_sizes = {}
            for module in ({"ai3d": ("cuda-toolkit", "hunyuan3d-21", "open3d-lab"), "model-studio": ("orca-bridge",),
                            "laser-studio": ("fire-watch",)}.get(tool_id, ())):
                module_root = self.root / "components" / module
                if module == "fire-watch":
                    # Surveillance IA (agent 1.34.0) : une installation commencée ou interrompue se voit.
                    modules[module] = fire_watch_status(module_root)
                else:
                    modules[module] = "installed" if component_ready(module, module_root) else "missing"
                if modules[module] in {"installed", "incomplete"}:
                    module_sizes[module] = self._taille_dossier(module_root)
            # Le moteur seul : ses modèles sont mesurés à part, sinon ils compteraient deux fois.
            hors_modeles = {"comfyui": ("models/checkpoints",), "hunyuan3d": ("models",)}.get(engine, ())
            installation = {"checked_at": time.time(), "engine_status": engine_status, "modules": modules,
                            "module_sizes": module_sizes,
                            "size_bytes": self._taille_dossier(engine_root, hors_modeles) if engine_status != "missing" else 0,
                            "managed": ownership["managed"] if ownership else engine_root.resolve() == managed_root.resolve(),
                            "managed_install_available": bool(ownership and ownership["managed_install_available"])}
            if engine in {"comfyui", "hunyuan3d"} and installed:
                expected_profile = runtime_profile(hardware, tool_id)
                if expected_profile.get("status") == "supported":
                    profile_health = runtime_profile_health(engine_root, expected_profile)
                    installation.update(
                        repair_required=profile_health["repair_required"],
                        repair_reason=profile_health["reason"],
                        installed_profile=profile_health["installed_profile"],
                    )
                else:
                    installation.update(repair_required=None, repair_reason=expected_profile.get("reason") or "Profil matériel à vérifier.")
            item["config"] = {**(item.get("config") or {}), "installation": installation}
            if tool_id == "printguard":
                with self.printguard_live_lock:
                    if self.printguard_live:
                        item["config"]["printguard"] = dict(self.printguard_live)
            intent = self.journal.get("runtime_intent", tool_id)
            if intent is not None:
                item["config"]["runtime_enabled"] = intent.get("enabled") is True
            watch = self.runtime_watch.get(tool_id) or {}
            if watch.get("down_since") or watch.get("restarts"):
                # Le site affiche ainsi qu'un moteur attendu en marche est tombé,
                # et combien de fois l'agent l'a relancé.
                item["config"]["runtime_watch"] = {
                    "down": bool(watch.get("down_since")), "restarts": len(watch.get("restarts") or []),
                    "exhausted": bool(watch.get("exhausted")), "limit": RUNTIME_RESTART_LIMIT,
                }
        # LoRA en dernier. Un identifiant déjà pris (modèle du même nom, LoRA homonyme d'un autre sous-dossier)
        # reçoit une forme propre au fichier : chaque LoRA reste visible, aucun modèle n'est remplacé.
        taken = {model_id for (tool_id, model_id) in models if tool_id == "image_generation"}
        for entry in lora_models[:LORA_INVENTORY_LIMIT]:
            if entry["model_id"] in taken:
                file_name = next(tag[5:] for tag in entry["capabilities"] if tag.startswith("file:"))
                entry["model_id"] = (entry["capabilities"][0] + ":" + file_name.rsplit(".", 1)[0])[:160]
                if entry["model_id"] in taken:
                    continue
            taken.add(entry["model_id"])
            models[("image_generation", entry["model_id"])] = entry
        return list(tools.values()), list(models.values())

    def _freecad_executable(self):
        managed = self.root / "components" / "freecad"
        if (managed / ".disabled").exists():
            return None
        values = []
        marker = managed / "freecad-path.txt"
        if marker.is_file():
            try: values.append(marker.read_text(encoding="utf-8").strip())
            except OSError: pass
        if local_sandbox.enabled():
            # The explicit installation/repair records a user-selected system
            # executable. Never adopt an unrelated PATH/config executable here.
            return next((str(Path(str(value)).resolve()) for value in values if value and Path(str(value)).is_file()), None)
        roots = self.config.get("component_paths") if isinstance(self.config.get("component_paths"), dict) else {}
        values.append(roots.get("freecad"))
        values.extend((shutil.which("FreeCADCmd"), shutil.which("freecadcmd")))
        return next((str(Path(str(value)).resolve()) for value in values if value and Path(str(value)).is_file()), None)

    def _laser_engine_python(self):
        """Python isolé du composant laser-engine quand le moteur laser est prêt, sinon None."""
        if __package__:
            from .installers.component_installer import component_ready, python_in
        else:
            from installers.component_installer import component_ready, python_in
        root = self.root / "components" / "laser-engine"
        if not component_ready("laser-engine", root):
            return None
        return str(python_in(root / ".venv").resolve())

    def _runtime_record_path(self, tool_id):
        runtime = self.root / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        return runtime / f"{tool_id}.json"

    def _runtime_record(self, tool_id):
        try:
            value = json.loads(self._runtime_record_path(tool_id).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _pid_alive(pid):
        try:
            # process_identity checks the process creation time and, on
            # Windows, also WaitForSingleObject. OpenProcess alone remains
            # successful for a terminated process while a handle still exists.
            return process_identity(pid) is not None
        except (PermissionError, OSError):
            # Unknown/denied is not proof that a process is stopped.
            return True
        except (TypeError, ValueError):
            return False

    def _runtime_running(self, tool_id):
        record = self._runtime_record(tool_id)
        if tool_id == "converter":
            return record.get("enabled") is True and bool(self._freecad_executable())
        if tool_id == "laser-studio":
            return record.get("enabled") is True and bool(self._laser_engine_python())
        pid = record.get("pid")
        root = str(record.get("root") or "").strip()
        expected_identity = str(record.get("process_identity") or "").strip()
        try:
            identity_matches = not expected_identity or process_identity(pid) == expected_identity
        except (PermissionError, OSError, TypeError, ValueError):
            identity_matches = False
        process_matches = bool(root and identity_matches and self._pid_alive(pid) and self._runtime_matches(pid, Path(root).resolve()))
        return process_matches and self._runtime_endpoint_ready(tool_id)

    def _runtime_port(self, tool_id):
        if local_sandbox.enabled():
            return local_sandbox.PORTS.get(tool_id)
        ports = {
            "image_generation": 8188,
            "ai3d": 8189,
            "printguard": 8000,
            "model-studio": int(self.config.get("orca_port") or 15064),
        }
        return ports.get(tool_id)

    def _runtime_endpoint_ready(self, tool_id):
        port = self._runtime_port(tool_id)
        if port is None:
            return True
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.35):
                return True
        except OSError:
            return False

    @staticmethod
    def _component_python(root):
        candidates = [
            root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
            root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
        ]
        return next((path for path in candidates if path.is_file()), Path(sys.executable))

    def _runtime_spec(self, tool_id):
        roots = self.config.get("component_paths") if isinstance(self.config.get("component_paths"), dict) else {}
        if tool_id == "image_generation":
            root = WorkerStorage(self.root, self.config).component("comfyui")
            entry = root / "main.py"
            if not entry.is_file():
                raise AgentError("ComfyUI n’est pas installé sur ce worker.")
            return root, [str(self._component_python(root)), str(entry), "--listen", "127.0.0.1", "--port", str(self._runtime_port(tool_id))]
        if tool_id == "ai3d":
            root = WorkerStorage(self.root, self.config).component("hunyuan3d")
            python = self._component_python(root)
            worker = root / "worker.py"
            if worker.is_file():
                # The adapter is part of the verified agent package. Refresh it
                # before a controlled start so an agent update also enables
                # model selection without reinstalling Hunyuan dependencies.
                shipped_worker = Path(__file__).with_name("hunyuan_worker.py")
                if shipped_worker.is_file() and shipped_worker.read_bytes() != worker.read_bytes():
                    from_source = shipped_worker.read_bytes()
                    temporary = worker.with_suffix(".py.agent-update")
                    if is_link(temporary) or is_link(worker):
                        raise AgentError("Le service Hunyuan3D est un lien ; actualisation automatique refusée.")
                    temporary.write_bytes(from_source)
                    os.replace(temporary, worker)
                # Backend module runners ship with the agent too: refresh them in
                # every installed module so an agent update reaches the engine.
                if __package__:
                    from .installers.component_installer import BACKEND_MODULES, install_backend_files
                else:
                    from installers.component_installer import BACKEND_MODULES, install_backend_files
                for module in BACKEND_MODULES:
                    module_root = self.root / "components" / module
                    if module_root.is_dir() and not is_link(module_root):
                        install_backend_files(module, module_root)
                token_file = self.root / "config" / "hunyuan-token"
                token_file.parent.mkdir(parents=True, exist_ok=True)
                if not token_file.is_file() or len(token_file.read_text(encoding="utf-8").strip()) < 32:
                    token_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
                return root, [str(python), str(worker), "serve", "--host", "127.0.0.1", "--port", str(self._runtime_port(tool_id)), "--token-file", str(token_file), "--root", str(root), "--generated-root", str(self.root / "outputs"), "--idle-timeout", "900"]
            api_server = root / "api_server.py"
            if api_server.is_file():
                return root, [str(python), str(api_server), "--host", "127.0.0.1", "--port", str(self._runtime_port(tool_id))]
            raise AgentError("Le service Hunyuan3D contrôlé est absent. Répare le composant sur ce worker.")
        if tool_id == "printguard":
            if os.name != "nt":
                raise AgentError("Le service PrintGuard Worker est actuellement disponible uniquement sous Windows.")
            executable = self._printguard_executable()
            if not executable:
                raise AgentError("PrintGuard n’est pas installé sur ce worker.")
            return executable.parent.resolve(), [str(executable)]
        if tool_id == "model-studio":
            if os.name != "nt":
                raise AgentError("Le bridge OrcaSlicer Worker est actuellement disponible uniquement sous Windows.")
            executable = self._orca_executable()
            bridge = self.root / "orca_core_bridge.ps1"
            if not executable:
                raise AgentError("OrcaSlicer n’est pas installé sur ce worker.")
            if not bridge.is_file():
                raise AgentError("Le bridge Alpine Model Studio est absent. Mets à jour l’agent Worker.")
            workspace = self.root / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            try:
                port = int(self._runtime_port(tool_id))
            except (TypeError, ValueError):
                port = 15064
            if not 1024 <= port <= 65535:
                raise AgentError("Le port du bridge OrcaSlicer est invalide.")
            return executable.parent.resolve(), [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(bridge),
                "-Workspace", str(workspace), "-OrcaDir", str(executable.parent), "-Port", str(port),
            ]
        raise AgentError("Ce moteur ne possède pas de commande marche/arrêt contrôlée.")

    def _runtime_matches(self, pid, root):
        marker = str(root).casefold()
        try:
            if os.name == "nt":
                script = (
                    f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}' -ErrorAction SilentlyContinue;"
                    "if($p){[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$p.CommandLine))}"
                )
                result = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=8)
                if result.returncode != 0 or not str(result.stdout or "").strip():
                    return False
                command_line = base64.b64decode(str(result.stdout).strip()).decode("utf-8")
                return marker in command_line.casefold()
            raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            return marker in raw.casefold()
        except Exception:
            return False

    def _runtime_progress(self, percent, message):
        command_id = getattr(self.runtime_command_context, "command_id", None)
        if command_id:
            self.command_progress(command_id, percent, message)

    def _wait_runtime_ready(self, process, tool_id):
        port = self._runtime_port(tool_id)
        deadline = time.time() + (180 if tool_id in {"ai3d", "image_generation"} else 60 if port is not None else 8)
        next_report = 0
        while time.time() < deadline:
            if time.monotonic() >= next_report:
                self._runtime_progress(65, "Processus lancé — attente du service local et vérification de sa disponibilité…")
                next_report = time.monotonic() + 5
            if process.poll() is not None:
                return False
            if port is None:
                time.sleep(0.4)
                continue
            if self._runtime_endpoint_ready(tool_id):
                # A stale process may already own the port. Require the newly
                # spawned parent to survive after the probe too.
                time.sleep(1.0)
                return process.poll() is None
            time.sleep(0.4)
        return port is None and process.poll() is None

    def configure_printguard_camera(self, payload, command_id):
        """Bind one of the account's cameras (an equipment of this Worker) to
        PrintGuard: the Worker serves the frames it already decodes on a
        loopback MJPEG relay (or reuses an external MJPEG URL), registers that
        source plus a single monitor in PrintGuard's profile, and restarts the
        hub when it is running so the change is loaded. Credentials never
        leave the Worker; PrintGuard only ever sees 127.0.0.1."""
        equipment_id = str(payload.get("equipment_id") or "").strip()
        label = str(payload.get("name") or "Caméra").strip()[:80] or "Caméra"
        if not equipment_id:
            raise AgentError("Caméra non précisée.")
        equipment = self.request("/api/worker-protocol/equipment").get("equipment", [])
        spec = next((item for item in equipment if item.get("id") == equipment_id), None)
        if not spec or spec.get("kind") != "camera":
            raise AgentError("Cette caméra n’est pas rattachée à ce Worker.")
        if not spec.get("enabled"):
            raise AgentError("Connecte d’abord cette caméra dans Mes équipements.")
        self.command_progress(command_id, 10, "Préparation de la source vidéo sur le Worker…")
        url = self.equipment_relay.loopback_stream(spec, command_id)
        host = urllib.parse.urlsplit(url).hostname or ""
        if host not in {"127.0.0.1", "localhost"} and spec.get("settings", {}).get("connector") != "mjpeg_http":
            raise AgentError("La source vidéo doit rester sur l’interface locale du Worker.")
        data_dir = self.root / "data" / "printguard"
        data_dir.mkdir(parents=True, exist_ok=True)
        state_path = data_dir / "state.json"
        marker_path = data_dir / "alpine_camera.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        try:
            previous = json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.is_file() else {}
        except (OSError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        camera_id = str(previous.get("camera_id") or uuid.uuid4().hex[:8])
        monitor_id = str(previous.get("monitor_id") or uuid.uuid4().hex[:8])
        cameras = [item for item in state.get("cameras", []) if isinstance(item, dict) and item.get("id") != camera_id]
        monitors = [item for item in state.get("monitors", []) if isinstance(item, dict)]
        kept = next((item for item in monitors if item.get("id") == monitor_id), {})
        monitors = [item for item in monitors if item.get("id") != monitor_id]
        cameras.append({"id": camera_id, "name": f"Alpine · {label}", "source": {"kind": "url", "url": url}, "printer_id": None,
                        "max_fps": 15.0, "brightness": 1.0, "contrast": 1.0, "sharpness": 0.0, "crop": None, "rotation": 0})
        monitor = {"name": f"Alpine · {label}", "enabled": True, "threshold": 0.75, "sensitivity": 1.0, "consecutive": 3,
                   "notify": False, "on_defect": "none", "cooldown_s": 60}
        for key in ("threshold", "sensitivity", "consecutive", "notify", "cooldown_s"):
            if key in kept:
                monitor[key] = kept[key]
        monitors.append({**monitor, "id": monitor_id, "camera_id": camera_id, "printer_id": ""})
        state.update(cameras=cameras, monitors=monitors)
        state.setdefault("printers", [])
        state.setdefault("settings", {"inference_runtime": "litert"})
        for key in ("tokens", "plugins"):
            state.setdefault(key, [])
        atomic_json(state_path, state)
        atomic_json(marker_path, {"camera_id": camera_id, "monitor_id": monitor_id, "equipment_id": equipment_id, "stream_url": url, "updated_at": time.time()})
        restarted = False
        if self._runtime_running("printguard"):
            self.command_progress(command_id, 60, "Redémarrage de PrintGuard pour charger la caméra…")
            self.control_runtime("printguard", "stop")
            self.control_runtime("printguard", "start")
            restarted = True
        self.command_progress(command_id, 100, "Caméra enregistrée dans PrintGuard.")
        return {"camera_id": camera_id, "monitor_id": monitor_id, "equipment_id": equipment_id, "stream_url": url, "restarted": restarted,
                "message": ("PrintGuard redémarré avec cette caméra." if restarted else "Caméra enregistrée ; elle sera lue au prochain démarrage de PrintGuard.")}

    # ------------------------------------------------------------ PrintGuard hub
    PRINTGUARD_NOTIFIERS = ("native", "ntfy", "telegram")
    PRINTGUARD_MONITOR_LIMITS = {"threshold": (0.05, 1.0), "sensitivity": (0.2, 5.0), "consecutive": (1, 30), "cooldown_s": (0, 600)}

    def _printguard_base_url(self):
        return f"http://127.0.0.1:{self._runtime_port('printguard') or 8000}"

    def _printguard_fetch(self, path, timeout=1.5):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(self._printguard_base_url() + path, timeout=timeout) as response:
            return json.loads(response.read(4 * 1024 * 1024).decode("utf-8", errors="replace") or "null")

    def _printguard_marker(self):
        marker_path = self.root / "data" / "printguard" / "alpine_camera.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.is_file() else {}
        except (OSError, ValueError):
            marker = {}
        return marker if isinstance(marker, dict) else {}

    # ------------------------------------------------------------ pause automatique locale
    AUTOPAUSE_DEFAUT = {"enabled": False, "min_score": 0.75, "monitor_id": "", "equipment_id": "",
                        "last_alert_key": "", "action": "", "message": "", "at": 0.0, "sent": False, "score": None}

    def _autopause_path(self):
        return self.root / "data" / "printguard_autopause.json"

    def _autopause_config(self):
        try:
            stocke = json.loads(self._autopause_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stocke = {}
        config = dict(self.AUTOPAUSE_DEFAUT)
        if isinstance(stocke, dict):
            config.update({cle: stocke[cle] for cle in config if cle in stocke})
        try:
            config["min_score"] = min(1.0, max(0.05, float(config["min_score"])))
        except (TypeError, ValueError):
            config["min_score"] = self.AUTOPAUSE_DEFAUT["min_score"]
        config["enabled"] = bool(config["enabled"])
        return config

    def _autopause_write(self, config):
        chemin = self._autopause_path()
        chemin.parent.mkdir(parents=True, exist_ok=True)
        temporaire = chemin.with_suffix(".tmp")
        temporaire.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporaire, chemin)

    def configure_printguard_autopause(self, payload):
        """Régler la pause automatique que le Worker applique lui-même.

        La décision vivait dans le processus du site : PC du site éteint ou
        réseau coupé, une impression qui partait de travers continuait alors que
        le Worker voyait l'alerte. Le site garde le réglage, le journal et la
        vérification de ce qui s'est réellement passé ; l'envoi, lui, part de la
        machine qui voit la caméra et tient la liaison imprimante.
        """
        config = self._autopause_config()
        if "enabled" in payload:
            if not isinstance(payload["enabled"], bool):
                raise AgentError("enabled doit être booléen.")
            config["enabled"] = payload["enabled"]
        if "min_score" in payload:
            try:
                valeur = float(payload["min_score"])
            except (TypeError, ValueError):
                raise AgentError("min_score doit être numérique.")
            if valeur != valeur or not 0.05 <= valeur <= 1.0:
                raise AgentError("min_score doit être compris entre 0.05 et 1.")
            config["min_score"] = valeur
        for cle in ("monitor_id", "equipment_id"):
            if cle in payload:
                valeur = str(payload[cle] or "")
                if len(valeur) > 64 or not re.fullmatch(r"[A-Za-z0-9._:-]*", valeur):
                    raise AgentError(f"{cle} invalide.")
                config[cle] = valeur
        self._autopause_write(config)
        return {"autopause": {cle: config[cle] for cle in ("enabled", "min_score", "monitor_id", "equipment_id")}}

    def _autopause_rapport(self, config):
        return {cle: config[cle] for cle in ("enabled", "min_score", "monitor_id", "equipment_id",
                                             "last_alert_key", "action", "message", "at", "sent", "score")}

    def _autopause_locale(self, snapshot):
        """Décider et envoyer la pause depuis le Worker, une seule fois par alerte."""
        config = self._autopause_config()
        alertes = [item for item in (snapshot.get("alerts") or []) if isinstance(item, dict)]
        if config["monitor_id"]:
            alertes = [item for item in alertes if str(item.get("monitor_id") or "") == config["monitor_id"]]
        if not alertes:
            return
        alerte = max(alertes, key=lambda item: float(item.get("ts") or 0.0))
        horodatage = float(alerte.get("ts") or 0.0)
        cle = f"{alerte.get('monitor_id')}:{horodatage:.6f}"
        if not horodatage or cle == config.get("last_alert_key"):
            return
        score = alerte.get("score")

        def conclure(action, message, envoyee=False):
            # L'alerte est marquée vue AVANT le moindre envoi : un plantage ou un
            # redémarrage de l'agent ne peut jamais provoquer une seconde pause.
            config.update(last_alert_key=cle, action=action, message=message, at=time.time(), sent=envoyee,
                          score=float(score) if isinstance(score, (int, float)) else None)
            try:
                self._autopause_write(config)
            except OSError:
                pass

        if not config["enabled"]:
            return conclure("disabled", "Pause automatique désactivée sur ce Worker : aucune pause.")
        if not isinstance(score, (int, float)):
            return conclure("invalid_score", "Alerte sans score exploitable : aucune pause.")
        if float(score) < config["min_score"]:
            return conclure("below_threshold",
                            f"Score sous le seuil de sécurité de {config['min_score'] * 100:.0f} % : aucune pause.")
        if not config["equipment_id"]:
            return conclure("no_printer", "Aucune imprimante associée à la pause automatique : aucune pause.")
        conclure("sending", "Envoi de la pause à l’imprimante depuis le Worker, issue à suivre.")
        try:
            resultat = self.equipment_relay.pause_locale(config["equipment_id"])
        except Exception as error:
            # Le message de l'équipement est explicite (état, connexion) et ne
            # contient ni chemin ni secret : il aide à comprendre sans rien exposer.
            return conclure("failed", f"Pause non envoyée par le Worker : {error}")
        conclure("paused" if resultat.get("device_confirmed") else "sent",
                 resultat.get("message") or "Pause envoyée par le Worker.", envoyee=True)

    def _printguard_live_snapshot(self):
        """Read-only view of the hub for the dashboard: the Alpine monitor, its
        camera, the configured notifier ids (never their secrets) and the last
        alerts. Nothing here can identify a token or a webhook."""
        state = self._printguard_fetch("/api/v1/state")
        if not isinstance(state, dict):
            return {}
        marker = self._printguard_marker()
        monitors = [item for item in state.get("monitors", []) if isinstance(item, dict)]
        monitor = next((item for item in monitors if item.get("id") == marker.get("monitor_id")), monitors[0] if monitors else None)
        cameras = {item.get("id"): item for item in state.get("cameras", []) if isinstance(item, dict)}
        settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
        notifiers = settings.get("notifiers") if isinstance(settings.get("notifiers"), dict) else {}
        snapshot = {"at": time.time(), "notifiers": sorted(key for key in notifiers if isinstance(key, str) and key in self.PRINTGUARD_NOTIFIERS)}
        if monitor:
            camera = cameras.get(monitor.get("camera_id")) or {}
            result = monitor.get("result") if isinstance(monitor.get("result"), dict) else {}
            snapshot["monitor"] = {
                "id": str(monitor.get("id") or ""), "camera_id": str(monitor.get("camera_id") or ""),
                "camera_online": bool(camera.get("online")), "watching": bool(monitor.get("watching")),
                "enabled": bool(monitor.get("enabled")), "notify": bool(monitor.get("notify")),
                "threshold": float(monitor.get("threshold") or 0.75), "sensitivity": float(monitor.get("sensitivity") or 1.0),
                "consecutive": int(monitor.get("consecutive") or 3),
                "score": float(result["score"]) if isinstance(result.get("score"), (int, float)) else None,
                "score_ts": float(result["ts"]) if isinstance(result.get("ts"), (int, float)) else None,
            }
        try:
            events = self._printguard_fetch("/api/v1/events")
        except (OSError, ValueError):
            events = []
        alerts = [item for item in (events if isinstance(events, list) else []) if isinstance(item, dict) and item.get("event") == "alert"]
        snapshot["alerts"] = [{"monitor_id": str(item.get("monitor_id") or ""), "ts": float(item.get("ts") or 0.0),
                               "score": float(item["score"]) if isinstance(item.get("score"), (int, float)) else None,
                               "message": str(item.get("message") or "")[:200]} for item in alerts[-5:]]
        return snapshot

    def _start_printguard_live(self):
        if self.printguard_live_thread and self.printguard_live_thread.is_alive():
            return
        self.printguard_live_thread = threading.Thread(target=self._printguard_live_loop, name="printguard-live", daemon=True)
        self.printguard_live_thread.start()

    def _printguard_live_loop(self):
        """Every few seconds, mirror the hub into the heartbeat; a new alert
        triggers a heartbeat right away so the dashboard can pause the print."""
        last_alert = 0.0
        while not self.stop_requested.is_set():
            snapshot = {}
            try:
                if self._runtime_running("printguard") and self._runtime_endpoint_ready("printguard"):
                    snapshot = self._printguard_live_snapshot()
            except Exception:
                snapshot = {}
            if snapshot:
                # Décider avant de publier : le heartbeat qui suit porte déjà l'issue,
                # et le site n'a plus qu'à la constater au lieu d'envoyer sa propre pause.
                try:
                    self._autopause_locale(snapshot)
                except Exception as error:
                    print(f"AUTOPAUSE_LOCALE: {type(error).__name__}", flush=True)
                snapshot["autopause"] = self._autopause_rapport(self._autopause_config())
            self._set_printguard_live(snapshot)
            newest = max((item.get("ts") or 0.0 for item in snapshot.get("alerts", [])), default=0.0)
            if newest > last_alert:
                if last_alert:
                    self.reconnect_requested.set()
                last_alert = newest
            self.stop_requested.wait(3)

    def _set_printguard_live(self, snapshot):
        """Store the hub mirror and patch the inventory in place so the very next heartbeat carries it."""
        with self.printguard_live_lock:
            self.printguard_live = dict(snapshot) if snapshot else {}
        with self.inventory_lock:
            for tool in self.inventory_snapshot.get("tools", []):
                if tool.get("tool_id") == "printguard":
                    config = dict(tool.get("config") or {})
                    if snapshot:
                        config["printguard"] = dict(snapshot)
                    else:
                        config.pop("printguard", None)
                    tool["config"] = config

    # ------------------------------------------------------------ relais des journaux moteurs
    LOG_ERROR_PATTERN = re.compile(r"(?i)\b(traceback|exception|error|critical|fatal|out of memory|failed|échec|erreur)\b")
    # outils/liberer_memoire.ps1 always reports "N erreur(s)", "0" included: a clean
    # run of this local admin tool must not be relayed as a Worker log error.
    # outils/etat_worker.ps1 and journaux.ps1 write a per-file tally "N ligne(s)
    # d'erreur sur M lues" into the very logs they just scanned ; that count line
    # is itself classified "info" below 20 by the tool and never the error text
    # itself, so it must not be relayed as a Worker log error (incident 05/10).
    LOG_NOISE_PATTERN = re.compile(r"(?:\bINFO\b|\bDEBUG\b|uvicorn\.error:|\[INFO\]|it/s\]|s/it\]|\b0 erreur\(s\)|"
                                    r"ligne\(s\) d'erreur sur \d+ lues)")
    LOG_WATCH_INTERVAL = 10

    # Une ligne de journal relayée au site porte souvent un chemin local, donc le
    # nom de session Windows du propriétaire du Worker (« C:\\Users\\Prénom\\… »).
    # Le diagnostic n'a besoin que du nom du fichier : le reste ne quitte pas la
    # machine. Le dossier personnel est masqué en premier, littéralement, car il
    # contient souvent une espace qu'aucune expression de chemin ne suivrait.
    # Un dossier contient souvent une espace (« C:\\Users\\Prénom Nom\\… ») : les
    # segments intermédiaires l'acceptent, le nom de fichier final s'arrête au premier
    # blanc, et la ponctuation coupe le chemin pour ne pas avaler la fin de la phrase.
    LOG_PATH_PATTERNS = (
        re.compile(r"""(?i)[A-Z]:[\\/](?:[^\\/\r\n"'<>|,;:*?]+[\\/])*[^\\/\r\n\s"'<>|,;]*"""),
        re.compile(r"""\\\\(?:[^\\\r\n"'<>|,;]+\\)*[^\\\r\n\s"'<>|,;]*"""),
        re.compile(r"""(?<![\w.~])/(?:home|Users|root|mnt|media|opt|srv|var)/[^\s"'<>|,;]*"""),
    )

    def _log_racines_privees(self):
        if getattr(self, "_log_racines", None) is None:
            racines = []
            for valeur in (str(self.root), os.path.expanduser("~")):
                valeur = (valeur or "").rstrip("\\/")
                if len(valeur) > 3 and valeur not in racines:
                    racines.append(valeur)
            # Le plus long d'abord : masquer le dossier personnel avant sa racine.
            self._log_racines = sorted(racines, key=len, reverse=True)
        return self._log_racines

    def _log_sans_chemin(self, line):
        for racine in self._log_racines_privees():
            line = re.sub(re.escape(racine), "…", line, flags=re.IGNORECASE)

        def dernier(match):
            nom = re.split(r"[\\/]", match.group(0).rstrip("\\/"))[-1]
            return f"…/{nom}" if nom else "…"

        for motif in self.LOG_PATH_PATTERNS:
            line = motif.sub(dernier, line)
        return line

    def _log_watch_files(self):
        logs = self.root / "logs"
        files = sorted(logs.glob("*.log")) if logs.is_dir() else []
        bridge = self.root / "workspace" / "orca_bridge.log"
        if bridge.is_file():
            files.append(bridge)
        return files

    def _log_watch_once(self):
        """Relay the new error lines of the engine logs to the dashboard journal.

        Only lines matching an error pattern travel (never whole files), each
        file is followed from its end on first sight (history is not replayed),
        identical lines are sent once per five minutes and a batch that could
        not be delivered is kept for the next pass."""
        now = time.time()
        events = []
        for path in self._log_watch_files():
            try:
                size = path.stat().st_size
            except OSError:
                continue
            key = str(path)
            offset = self.log_cursors.get(key)
            if offset is None or offset > size:
                self.log_cursors[key] = size
                continue
            if size == offset:
                continue
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read(256 * 1024)
            except OSError:
                continue
            self.log_cursors[key] = offset + len(chunk)
            count = 0
            for line in chunk.decode("utf-8", errors="replace").replace("\r", "\n").split("\n"):
                line = line.strip()
                if not line or len(line) > 2000 or not self.LOG_ERROR_PATTERN.search(line) or self.LOG_NOISE_PATTERN.search(line):
                    continue
                signature = re.sub(r"\d+", "#", line)[:200]
                if now - self.log_recent.get(signature, 0) < 300:
                    continue
                self.log_recent[signature] = now
                events.append({"file": path.name, "line": self._log_sans_chemin(line)[:500], "ts": now})
                count += 1
                if count >= 40:
                    break
        self.log_recent = {key: value for key, value in self.log_recent.items() if now - value < 600}
        pending = (self.log_pending + events)[-200:]
        self.log_pending = []
        sent = 0
        while pending:
            batch, pending = pending[:50], pending[50:]
            try:
                self.request("/api/worker-protocol/events", "POST", {"events": batch})
                sent += len(batch)
            except Exception:
                self.log_pending = (batch + pending)[-200:]
                break
        return sent

    def _start_log_watch(self):
        if self.log_watch_thread and self.log_watch_thread.is_alive():
            return
        self.log_watch_thread = threading.Thread(target=self._log_watch_loop, name="worker-log-watch", daemon=True)
        self.log_watch_thread.start()

    def _log_watch_loop(self):
        while not self.stop_requested.is_set():
            try:
                self._log_watch_once()
            except Exception:
                pass
            self.stop_requested.wait(self.LOG_WATCH_INTERVAL)

    def _printguard_ws_exchange(self, messages, wait_for=None, timeout=8.0):
        """Send protocol commands to the hub over its loopback WebSocket and,
        when asked, wait for one event (``wait_for`` returns True on it)."""
        target = urllib.parse.urlsplit(self._printguard_base_url())
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (f"GET /api/ws HTTP/1.1\r\nHost: {target.hostname}:{target.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode("ascii")

        def frame(message):
            raw = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if len(raw) > 65_535:
                raise AgentError("Commande PrintGuard trop volumineuse.")
            mask = os.urandom(4)
            header = bytes((0x81, 0x80 | len(raw))) if len(raw) < 126 else bytes((0x81, 0x80 | 126)) + struct.pack("!H", len(raw))
            return header + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(raw))

        def receive(sock, size):
            chunks = []
            while size > 0:
                chunk = sock.recv(size)
                if not chunk:
                    raise ConnectionError("canal fermé")
                chunks.append(chunk)
                size -= len(chunk)
            return b"".join(chunks)

        with socket.create_connection((target.hostname, target.port), timeout=3.0) as sock:
            sock.settimeout(max(3.0, timeout))
            sock.sendall(handshake)
            response = bytearray()
            while b"\r\n\r\n" not in response and len(response) < 8_192:
                block = sock.recv(1_024)
                if not block:
                    break
                response.extend(block)
            if not bytes(response).startswith(b"HTTP/1.1 101"):
                raise ConnectionError("Le canal de configuration PrintGuard est indisponible.")
            for message in messages:
                sock.sendall(frame(message))
            if wait_for is None:
                return None
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                prefix = receive(sock, 2)
                opcode, size = prefix[0] & 0x0F, prefix[1] & 0x7F
                if size == 126:
                    size = struct.unpack("!H", receive(sock, 2))[0]
                elif size == 127:
                    size = struct.unpack("!Q", receive(sock, 8))[0]
                if size > 4 * 1024 * 1024:
                    raise ValueError("Réponse PrintGuard trop volumineuse.")
                payload = receive(sock, size)
                if opcode == 0x8:
                    break
                if opcode != 0x1:
                    continue
                try:
                    event = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if isinstance(event, dict) and wait_for(event):
                    return event
        raise TimeoutError("PrintGuard n’a pas répondu à temps.")

    def configure_printguard_settings(self, payload, command_id):
        """Apply the dashboard's detection/notification settings to PrintGuard:
        persisted in its profile (so they survive a restart) and pushed live
        through the loopback WebSocket when the hub runs. Notifier secrets
        (ntfy token, Telegram bot token) only ever travel to this Worker."""
        monitor_patch = payload.get("monitor") if isinstance(payload.get("monitor"), dict) else {}
        notifier_patch = payload.get("notifiers") if isinstance(payload.get("notifiers"), dict) else {}
        test_provider = str(payload.get("test") or "").strip()
        if test_provider and test_provider not in self.PRINTGUARD_NOTIFIERS:
            raise AgentError("Canal d’alerte non pris en charge sur le Worker.")
        clean_monitor = {}
        for key, (low, high) in self.PRINTGUARD_MONITOR_LIMITS.items():
            if key in monitor_patch:
                try:
                    value = float(monitor_patch[key])
                except (TypeError, ValueError):
                    raise AgentError(f"Réglage {key} invalide.")
                value = max(low, min(high, value))
                clean_monitor[key] = int(value) if key in {"consecutive", "cooldown_s"} else value
        for key in ("notify", "enabled"):
            if key in monitor_patch:
                clean_monitor[key] = bool(monitor_patch[key])
        clean_notifiers = {}
        for provider, config in notifier_patch.items():
            if provider not in self.PRINTGUARD_NOTIFIERS:
                raise AgentError("Canal d’alerte non pris en charge sur le Worker.")
            if config is None:
                clean_notifiers[provider] = None
            elif isinstance(config, dict) and all(isinstance(k, str) and isinstance(v, str) and len(v) <= 500 for k, v in config.items()):
                clean_notifiers[provider] = {k: v for k, v in config.items() if k in {"url", "token", "bot_token", "chat_id"}}
            else:
                raise AgentError("Configuration de canal invalide.")
        data_dir = self.root / "data" / "printguard"
        data_dir.mkdir(parents=True, exist_ok=True)
        state_path = data_dir / "state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        monitors = [item for item in state.get("monitors", []) if isinstance(item, dict)]
        marker = self._printguard_marker()
        monitor = next((item for item in monitors if item.get("id") == marker.get("monitor_id")), monitors[0] if monitors else None)
        if clean_monitor and monitor is None:
            raise AgentError("Choisis d’abord une caméra de surveillance : PrintGuard n’a pas encore de moniteur.")
        if monitor is not None:
            monitor.update(clean_monitor)
        settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
        notifiers = settings.get("notifiers") if isinstance(settings.get("notifiers"), dict) else {}
        for provider, config in clean_notifiers.items():
            if config is None:
                notifiers.pop(provider, None)
            else:
                notifiers[provider] = {**(notifiers.get(provider) if isinstance(notifiers.get(provider), dict) else {}), **config}
        settings["notifiers"] = notifiers
        state["settings"] = settings
        state["monitors"] = monitors
        atomic_json(state_path, state)
        running = self._runtime_running("printguard") and self._runtime_endpoint_ready("printguard")
        test_result = None
        if running:
            messages = []
            if clean_monitor and monitor is not None:
                messages.append({"cmd": "monitor.update", "id": monitor["id"], "patch": clean_monitor})
            if clean_notifiers:
                messages.append({"cmd": "settings.update", "patch": {"notifiers": notifiers}})
            self.command_progress(command_id, 50, "Application des réglages dans PrintGuard…")
            try:
                if messages:
                    self._printguard_ws_exchange(messages)
                if test_provider:
                    config = notifiers.get(test_provider) if test_provider != "native" else {}
                    if config is None:
                        raise AgentError("Ce canal doit être configuré avant d’être testé.")
                    event = self._printguard_ws_exchange([{"cmd": "notify.test", "provider": test_provider, "config": config}],
                                                         wait_for=lambda item: item.get("event") == "notify_test" and item.get("provider") == test_provider)
                    test_result = {"ok": bool(event.get("ok")), "error": str(event.get("error") or "")[:240]}
            except (OSError, ValueError, ConnectionError, TimeoutError) as error:
                raise AgentError(f"PrintGuard n’a pas accepté la commande ({type(error).__name__}).")
        elif test_provider:
            raise AgentError("Démarre PrintGuard sur ce Worker avant de tester un canal.")
        try:
            snapshot = self._printguard_live_snapshot() if running else {}
        except (OSError, ValueError):
            snapshot = {}
        if snapshot:
            self._set_printguard_live(snapshot)
        self.reconnect_requested.set()
        self.command_progress(command_id, 100, "Réglages PrintGuard enregistrés.")
        return {"applied": True, "live": running, "monitor": (snapshot.get("monitor") if snapshot else None),
                "notifiers": snapshot.get("notifiers") if snapshot else sorted(notifiers), "test": test_result,
                "message": "Réglages appliqués dans PrintGuard." if running else "Réglages enregistrés ; PrintGuard les lira à son prochain démarrage."}

    def control_runtime(self, tool_id, action):
        self._assert_no_active_jobs()
        tool_id = str(tool_id or "").strip()
        action = str(action or "").strip().lower()
        if tool_id not in {"image_generation", "ai3d", "printguard", "model-studio", *ON_DEMAND_TOOLS} or action not in {"start", "stop"}:
            raise AgentError("Commande de moteur local invalide.")
        if action == "start":
            if self.environment_maintenance_refreshing_python:
                raise AgentError("Démarrage reporté : le Python isolé des moteurs est en cours de remplacement "
                                 "(maintenance des environnements) ; réessaie dans quelques minutes.")
            self._check_legal_operation("tool.start", {"tool_id": tool_id})
        record_path = self._runtime_record_path(tool_id)
        if tool_id in ON_DEMAND_TOOLS:
            if action == "start" and tool_id == "converter" and not self._freecad_executable():
                raise AgentError("FreeCAD n’est pas installé. Installe ou répare le convertisseur.")
            if action == "start" and tool_id == "laser-studio" and not self._laser_engine_python():
                raise AgentError("Le moteur laser n’est pas installé. Installe ou répare Alpine Laser Studio (Mes Workers → Installations).")
            # FreeCADCmd and the laser engine are per-job processes, not persistent servers.
            atomic_json(record_path, {"enabled": action == "start", "updated_at": time.time()})
            return {"started": action == "start", "stopped": action == "stop", "on_demand": True}
        record = self._runtime_record(tool_id)
        pid = record.get("pid")
        if action == "stop":
            self._runtime_progress(25, "Vérification du processus appartenant à ce Worker…")
            if not self._pid_alive(pid):
                record_path.unlink(missing_ok=True)
                return {"stopped": False, "already_stopped": True}
            root_value = str(record.get("root") or "").strip()
            expected_identity = str(record.get("process_identity") or "").strip()
            if expected_identity:
                try:
                    current_identity = process_identity(pid)
                except (PermissionError, OSError, TypeError, ValueError) as error:
                    raise AgentError("L’identité du processus moteur ne peut pas être vérifiée ; aucun arrêt ni fichier supprimé.") from error
                if current_identity != expected_identity:
                    # The launcher can disappear while a child still owns the
                    # engine port.  Only discard the record once the endpoint
                    # is also gone; never hide a still-running service.
                    if not self._runtime_endpoint_ready(tool_id):
                        self._runtime_progress(40, "Ancienne identité de processus supprimée ; moteur déjà arrêté.")
                        record_path.unlink(missing_ok=True)
                        return {"stopped": False, "already_stopped": True, "stale_record": True}
                    raise AgentError("Le service répond avec une autre identité de processus ; aucun arrêt ni fichier supprimé.")
            if not root_value or not self._runtime_matches(pid, Path(root_value).resolve()):
                # A process can exit between the first liveness probe and the
                # command-line check, or Windows can reuse its old PID. Never
                # kill that unrelated process. If the engine endpoint is also
                # absent, the record is safely stale and must not prevent a
                # model/component uninstall forever.
                if not self._pid_alive(pid) or not self._runtime_endpoint_ready(tool_id):
                    self._runtime_progress(40, "Ancienne fiche de processus supprimée ; moteur déjà arrêté.")
                    record_path.unlink(missing_ok=True)
                    return {"stopped": False, "already_stopped": True, "stale_record": True}
                raise AgentError("Le service répond encore mais son processus ne correspond pas à la fiche contrôlée ; aucun arrêt ni fichier supprimé.")
            self._runtime_progress(50, "Arrêt du moteur et de ses processus enfants…")
            if os.name == "nt":
                result = run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], timeout=20)
                if result.returncode and self._pid_alive(pid):
                    raise AgentError("Impossible d’arrêter le moteur sur ce worker.")
            else:
                try: os.killpg(int(pid), 15)
                except OSError: os.kill(int(pid), 15)
            self._runtime_progress(90, "Vérification de l’arrêt effectif du processus…")

            def recorded_process_alive():
                if not expected_identity:
                    return self._pid_alive(pid)
                try:
                    return process_identity(pid) == expected_identity
                except (PermissionError, OSError, TypeError, ValueError):
                    return True

            deadline = time.monotonic() + 15
            while (recorded_process_alive() or self._runtime_endpoint_ready(tool_id)) and time.monotonic() < deadline:
                time.sleep(.2)
            if recorded_process_alive():
                raise AgentError("Arrêt non confirmé : le processus est encore actif.")
            if self._runtime_endpoint_ready(tool_id):
                raise AgentError("Arrêt non confirmé : le service du moteur répond encore.")
            record_path.unlink(missing_ok=True)
            return {"stopped": True}
        root_value = str(record.get("root") or "").strip()
        if self._runtime_running(tool_id):
            return {"started": False, "already_running": True, "pid": int(pid)}
        if record:
            # A launcher can remain alive after its HTTP child failed. Remove
            # that stale managed tree before attempting a clean restart.
            if self._pid_alive(pid) and root_value and self._runtime_matches(pid, Path(root_value).resolve()):
                self.control_runtime(tool_id, "stop")
            record_path.unlink(missing_ok=True)
        self._runtime_progress(25, "Vérification de l’installation et préparation de la configuration locale…")
        root, command = self._runtime_spec(tool_id)
        if tool_id == "image_generation":
            self._install_comfy_control(root)
        logs = self.root / "logs"; logs.mkdir(parents=True, exist_ok=True)
        log_path = logs / f"{tool_id}.log"
        environment = WorkerStorage(self.root, self.config).environment()
        environment["ALPINE_WORKER_ROOT"] = str(self.root)
        environment["ALPINE_WORKER_CONTROL_TOKEN_FILE"] = str(self.root / "config" / "engine-control-token")
        if tool_id in {"image_generation", "ai3d"}:
            profile = runtime_profile(detect_hardware(), tool_id)
            if profile["status"] != "supported":
                raise AgentError(profile["reason"])
            environment["CUDA_VISIBLE_DEVICES"] = str(profile["selected_gpu_index"])
            environment["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
            environment["TORCH_CUDA_ARCH_LIST"] = profile["compute_capability"]
            self._runtime_progress(35, profile["label"])
            if tool_id == "image_generation":
                if float(profile["compute_capability"]) < 7.5:
                    command.extend(["--force-fp32", "--use-split-cross-attention", "--disable-xformers", "--disable-dynamic-vram"])
                if profile["vram_total_mb"] is not None and profile["vram_total_mb"] < 10000:
                    command.append("--lowvram")
        if tool_id == "ai3d":
            cuda_marker = self.root / "components" / "cuda-toolkit" / "cuda-path.txt"
            if cuda_marker.is_file():
                cuda_root = Path(cuda_marker.read_text(encoding="utf-8").strip())
                if cuda_root.is_dir():
                    environment["CUDA_PATH"] = str(cuda_root)
                    environment["CUDA_HOME"] = str(cuda_root)
                    environment["PATH"] = str(cuda_root / "bin") + os.pathsep + environment.get("PATH", "")
        elif tool_id == "printguard":
            try:
                environment = local_sandbox.printguard_environment(self.root, root, environment)
            except ValueError as error:
                raise AgentError(str(error)) from error
            data_dir = self.root / "data" / "printguard"
            data_dir.mkdir(parents=True, exist_ok=True)
            state_path = data_dir / "state.json"
            try:
                state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
            except (OSError, ValueError):
                state = {}
            if not isinstance(state, dict):
                state = {}
            settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
            # WindowsML/DirectML can terminate the entire packaged process on
            # some NVIDIA driver revisions. LiteRT is CPU-only, very fast for
            # PrintGuard's small encoder, and avoids a false-online crash loop.
            if settings.get("inference_runtime") in {None, "", "auto"}:
                state["settings"] = {**settings, "inference_runtime": "litert"}
                temporary = state_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
                os.replace(temporary, state_path)
            model_dir = root / "models"
            if not model_dir.is_dir() and (root / "_internal" / "models").is_dir():
                model_dir = root / "_internal" / "models"
            # The packaged LiteRT runtime opens the model with a narrow C path:
            # a non-ASCII install folder makes it fail, the 8.3 name never does.
            environment["MODEL_DIR"] = _ascii_path(model_dir)
            environment.setdefault("DATA_DIR", _ascii_path(data_dir))
            environment.setdefault("PRINTGUARD_MAX_WORKERS", "2")
            environment.setdefault("PRINTGUARD_DISABLE_MEDIA_PUBLISH", "1")
        self._runtime_progress(45, "Démarrage du processus moteur sur le Worker…")
        with log_path.open("a", encoding="utf-8") as output:
            process = subprocess.Popen(
                command, cwd=str(root), stdout=output, stderr=subprocess.STDOUT, env=environment,
                creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
        try:
            identity = process_identity(process.pid)
        except (PermissionError, OSError, TypeError, ValueError):
            identity = None
        record_path.write_text(json.dumps({"pid": process.pid, "root": str(root), "started_at": time.time(),
                                           "process_identity": identity}), encoding="utf-8")
        ready = self._wait_runtime_ready(process, tool_id)
        exit_code = process.poll()
        if exit_code is not None or not ready:
            if exit_code is None:
                self.control_runtime(tool_id, "stop")
            else:
                record_path.unlink(missing_ok=True)
            # The child's own stdout buffer is lost on an abrupt kill (GPU driver
            # reset, OOM kill…), so the log often has no traceback at all: the exit
            # code is then the only fact left to point a diagnosis at.
            detail = f" (code de sortie {exit_code})" if exit_code is not None else ""
            raise AgentError(f"Le moteur s’est arrêté au démarrage{detail}. Consulte {log_path.name}.")
        return {"started": True, "pid": process.pid}

    @staticmethod
    def _remove_readonly(function, path, _error):
        os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
        function(path)

    def purge_managed_components(self):
        """Stop engines and remove only components installed below the agent root."""
        for tool_id in ("image_generation", "ai3d", "printguard", "model-studio"):
            self.control_runtime(tool_id, "stop")
        # Le moteur de surveillance IA tient des fichiers de components/fire-watch : arrêté avant la suppression.
        self._stop_fire_watch()
        try:
            components = (self.root / "components").resolve()
            if components.parent != self.root.resolve():
                raise AgentError("Répertoire de composants Worker invalide ; nettoyage annulé.")
            removed = sorted(path.name for path in components.iterdir()) if components.is_dir() else []
            if components.is_dir():
                shutil.rmtree(components, onerror=self._remove_readonly)
            runtime = self.root / "runtime"
            if runtime.is_dir():
                shutil.rmtree(runtime, onerror=self._remove_readonly)
            # Comptes IA (1.35.0) : connexions en cours annulées, profils et secrets collés depuis le site effacés,
            # pour qu'un nouveau compte du site qui réassocie ce PC n'hérite d'aucun accès.
            if self.ai_accounts is not None:
                self.ai_accounts.cancel_all("Connexion annulée : Worker supprimé du dashboard.")
            data = (self.root / "data").resolve()
            if data.parent != self.root.resolve():
                raise AgentError("Répertoire de données Worker invalide ; nettoyage annulé.")
            profiles = data / "ai-accounts"
            if profiles.is_dir() and not profiles.is_symlink():
                shutil.rmtree(profiles, onerror=self._remove_readonly)
            (data / "ai-accounts.json").unlink(missing_ok=True)
        except Exception:
            # Nettoyage échoué : le Worker reste en service, sa surveillance aussi.
            if self.fire_watch_wanted and not self.stop_requested.is_set():
                self._start_fire_watch()
            raise
        return {"purged": True, "removed_components": removed}

    def _detach_after_purge(self):
        self.exit_code_pending = 0
        self.config.pop("token", None)
        self.config.pop("worker_id", None)
        self.config.pop("tools", None)
        self.config.pop("models", None)
        self.save()
        self.detached_marker.write_text(
            "Worker supprimé du dashboard. Réassociez-le avec un nouveau code pour le réactiver.\n",
            encoding="utf-8",
        )
        # Défense en profondeur : un job GRBL lancé après le contrôle de worker.purge (equipment.command n'est pas
        # sérialisée avec la maintenance) reçoit l'arrêt de protection de la déconnexion des équipements, au lieu
        # d'un port série fermé par le système en pleine gravure.
        try:
            self.equipment_relay.sync([])
        except Exception as error:
            print(f"Fermeture des équipements incomplète ({type(error).__name__}).", flush=True)
        time.sleep(0.5)
        exit_process(0)

    def pair(self, server_url, pairing_code, name="", *, gpu_optional=False):
        if gpu_optional and platform.system() != "Linux":
            raise AgentError("La variante sans GPU obligatoire est réservée à Linux.")
        self.config.update({"server_url": server_url.rstrip("/"), "name": name or socket.gethostname(),
                            "gpu_optional": bool(gpu_optional)})
        if local_sandbox.enabled():
            self.config["local_sandbox"] = True
        payload = self.inventory() | {"pairing_code": pairing_code}
        identity = Identity(self.root, create=True)
        payload["identity_public_key"] = identity.public
        payload["identity_signature"] = identity.sign(payload)
        response = self.request("/api/worker-protocol/register", "POST", payload, authenticated=False)
        # Keyed requests never use the old bearer as authentication. Do not keep
        # the newly issued bearer secret in plaintext (or at all).
        self.config.update({"worker_id": response["worker_id"], "token": "identity-v1", "identity_enabled": True, "identity_server_id": response["identity_server_id"], "identity_epoch": response["identity_epoch"]}); self.save()
        configure_identity(self.root, self.config)
        self.detached_marker.unlink(missing_ok=True)
        return response

    def log(self, event, message, level="info", details=None):
        try: self.request("/api/worker-protocol/logs", "POST", {"event": event, "message": message, "level": level, "details": details or {}})
        except Exception: pass

    def command_progress(self, command_id, progress, message, **metrics):
        try:
            self.request(
                f"/api/worker-protocol/commands/{urllib.parse.quote(str(command_id))}/progress",
                "POST", {"progress": float(progress), "message": str(message)[:500], **{key: value for key, value in metrics.items() if key in {"bytes_done", "bytes_total", "speed_bps", "eta_seconds", "phase", "login_url", "user_code", "awaiting_code"}}},
            )
        except Exception:
            pass

    def run_installer(self, arguments, environment, command_id, local=False):
        """``local`` : reçu interne (maintenance d'arrière-plan sans commande serveur) ; sa progression reste dans le
        journal local et son reçu n'est jamais renvoyé au serveur (voir _is_local_receipt)."""
        command_id = str(command_id)
        environment = WorkerStorage(self.root, self.config).environment(environment)
        environment["PYTHONUTF8"] = "1"
        record = scope_record()
        commit_path = self.root / "runtime" / ("install-" + hashlib.sha256(command_id.encode()).hexdigest() + ".lock")
        environment["ALPINE_INSTALL_COMMIT_LOCK"] = str(commit_path)
        # Windows system installers may delegate to MSI/winget services outside
        # our job. Do not kill those services or claim their work has stopped.
        deferred = Path(arguments[1]).name == "component_installer.py" and arguments[2] in {"cuda-toolkit", "freecad"}
        self.journal.update("commands", command_id, installer_launch_intent=True,
                            installer_exit_confirmed=False, installer_scope=record,
                            installer_started=False, installer_system_phase=deferred, local=bool(local))
        report_progress = (lambda *args, **kwargs: None) if local else self.command_progress
        scope, process, started, confirmed = None, None, False, False
        lines, messages = [], queue.Queue(maxsize=256)
        reader_done = threading.Event()
        reader_stop = threading.Event()

        def read_output():
            try:
                with process.stdout:
                    for raw in process.stdout:
                        while not reader_stop.is_set():
                            try:
                                messages.put(raw, timeout=0.2)
                                break
                            except queue.Full:
                                pass
                        if reader_stop.is_set():
                            break
            finally:
                reader_done.set()

        def consume(raw):
            if raw:
                line = raw.rstrip("\r\n")
                if line.startswith("ALPINE_PROGRESS="):
                    try:
                        update = json.loads(line.split("=", 1)[1])
                        report_progress(command_id, update.get("progress", 0), update.get("message") or "Installation en cours…", **{key: update[key] for key in ("bytes_done", "bytes_total", "speed_bps", "eta_seconds", "phase") if key in update})
                    except (ValueError, TypeError):
                        pass
                elif line:
                    lines.append(line)
                    del lines[:-120]

        try:
            scope = InstallerScope(record, create=True)
            bootstrap = Path(__file__).with_name("installer_process.py")
            process = subprocess.Popen(
                [arguments[0], str(bootstrap), "--run", *arguments[1:]],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            record = scope.attach(process)
            self.journal.update("commands", command_id, installer_scope=record,
                                installer_pid=process.pid, installer_identity=record["identity"])
            if self._install_cancel_requested(command_id):
                raise InstallationCancelled("Installation annulée avant son démarrage ; composants existants conservés.")
            # Commit receipt before opening the gate; a crash never causes a replay.
            self.journal.update("commands", command_id, installer_started=True)
            process.stdin.write("start\n"); process.stdin.flush(); process.stdin.close()
            started = True
            threading.Thread(target=read_output, name="worker-install-output", daemon=True).start()
            deferred_reported = False
            while True:
                try:
                    consume(messages.get(timeout=0.2))
                except queue.Empty:
                    pass
                if process.poll() is not None and scope.stopped():
                    confirmed = True
                    break
                if self._install_cancel_requested(command_id):
                    if deferred:
                        if not deferred_reported:
                            report_progress(command_id, 0, "Annulation demandée : attente du point d’arrêt sûr de l’installateur système.", phase="cancel_requested")
                            deferred_reported = True
                        continue
                    with installation_commit(commit_path, blocking=False) as allowed:
                        if not allowed:
                            continue  # Finish the short atomic component replacement.
                        scope.terminate()
                        confirmed = scope.wait_stopped()
                    if not confirmed:
                        raise AgentError("Annulation demandée, mais l’arrêt de l’installation n’est pas encore confirmé.")
                    raise InstallationCancelled("Installation annulée ; composants déjà présents conservés.")
            reader_done.wait(1)
            while not messages.empty():
                consume(messages.get_nowait())
            output = "\n".join(lines)
            if process.wait():
                if self._install_cancel_requested(command_id):
                    raise InstallationCancelled("Installation arrêtée après la demande d’annulation ; composants présents conservés.")
                raise AgentError(output[-2000:] or "Échec de l’installateur.")
            # A successful install that won the race stays completed, not cancelled.
            return {"output": output[-4000:], "progress": 100, "message": "Installation terminée.", "installer_stop_confirmed": True}
        finally:
            reader_stop.set()
            if not started:
                # Still behind the stdin gate: terminate only our own bootstrap.
                if process is not None:
                    try:
                        if process.stdin and not process.stdin.closed:
                            process.stdin.close()
                        process.wait(timeout=5)
                        confirmed = scope is None or scope.stopped()
                    except (OSError, subprocess.TimeoutExpired):
                        confirmed = False
                else:
                    confirmed = True
            if scope is not None:
                scope.close()  # Windows crash/failure cleanup remains confined to this job.
            self.journal.update("commands", command_id, installer_exit_confirmed=confirmed)
            if not confirmed:
                with self.maintenance_lock:
                    self.maintenance_recovery[command_id] = self.journal.get("commands", command_id)
                    self.maintenance_active = True

    def storage_audit_roots(self):
        """Audit only locally selected engine/model locations; never a remote path."""
        roots = {}
        configured = self.config.get("component_paths")
        configured = configured if isinstance(configured, dict) else {}

        def include(label, value, configured_path=False):
            if not value:
                return
            path = Path(os.path.abspath(os.path.normpath(os.path.expanduser(str(value)))))
            try:
                info = path.lstat()
            except FileNotFoundError:
                info = None
            except OSError:
                # Keep inaccessible locations in the scanner's input so they
                # produce an incomplete report rather than silently disappear.
                info = None
                configured_path = True
            linked = info is not None and (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400)
            if info is not None and stat.S_ISREG(info.st_mode) and not linked:
                path = path.parent
                if path.name.casefold() == "bin":
                    path = path.parent
            # Reject drive roots and a whole user profile even if a local
            # configuration accidentally names one as an engine directory.
            if path == Path(path.anchor) or path == Path(os.path.abspath(os.fspath(Path.home()))):
                return
            if configured_path or linked or (info is not None and stat.S_ISDIR(info.st_mode)):
                roots[label] = str(path)

        include("Composants gérés du Worker", self.root / "components")
        storage = WorkerStorage(self.root, self.config)
        include("Caches gérés du Worker", storage.path("cache"))
        include("Temporaires du Worker", storage.path("temp"))
        for variable, value in storage.settings()["environment"].items():
            # Join relative values to the Worker root, but never resolve: a
            # configured cache that is a link must be reported, not followed.
            candidate = Path(os.path.expandvars(os.path.expanduser(str(value))))
            include(f"Stockage · {variable}", candidate if candidate.is_absolute() else storage.root / candidate, configured_path=True)
        for component in ("comfyui", "hunyuan3d", "orca", "freecad", "printguard"):
            include(f"Chemin configuré · {component}", configured.get(component), configured_path=True)
        for variable in ("COMFYUI_ROOT", "HUNYUAN3D_ROOT", "PRINTGUARD_EXE", "HF_HOME", "HUGGINGFACE_HUB_CACHE"):
            include(f"Chemin local · {variable}", os.getenv(variable), configured_path=True)
        include("Cache modèles Hugging Face du compte Windows/Linux", Path.home() / ".cache" / "huggingface")
        for component, filename in (("freecad", "freecad-path.txt"), ("cuda-toolkit", "cuda-path.txt")):
            marker = self.root / "components" / component / filename
            if marker.is_file():
                try:
                    include(f"Installation référencée · {component}", marker.read_text(encoding="utf-8").strip(), configured_path=True)
                except (OSError, UnicodeError):
                    pass
        return roots

    def free_resources(self, dry_run=False, command_id=None):
        """Ferme les applications inutiles de ce PC et rend la mémoire libérée.

        Le dashboard n'envoie jamais de nom de programme : la liste de ce qui est
        fermé — et surtout de ce qui est gardé (Windows, sécurité, watercooling,
        Claude, et tout ce qui fait tourner les sites Alpine Makers) — est écrite
        dans free_resources.ps1, à côté de l'agent.
        """
        if os.name != "nt":
            raise AgentError("La libération de mémoire n’est disponible que sur un Worker Windows.")
        script = self.root / "free_resources.ps1"
        if not script.is_file():
            raise AgentError("Le script de libération de mémoire est absent. Mets à jour l’agent Worker.")
        if not self.free_resources_lock.acquire(blocking=False):
            raise AgentError("Une libération de mémoire est déjà en cours sur ce Worker.")
        try:
            self.command_progress(command_id, 10, "Inventaire des programmes ouverts…")
            command = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                       "-File", _ascii_path(script)]
            if dry_run:
                command.append("-DryRun")
            result = run(command, timeout=FREE_RESOURCES_TIMEOUT)
            sortie = (result.stdout or "").strip()
            # Le rapport est une seule ligne JSON compressée : repartir de la dernière
            # accolade tomberait sur un objet imbriqué du rapport lui-même.
            ligne = next((item for item in reversed(sortie.splitlines()) if item.lstrip().startswith("{")), "")
            if result.returncode != 0 and not ligne:
                detail = (result.stderr or sortie or "").strip().splitlines()
                raise AgentError(f"Libération de mémoire impossible : {detail[-1][:200] if detail else 'code ' + str(result.returncode)}")
            try:
                rapport = json.loads(ligne) if ligne else {}
            except (ValueError, json.JSONDecodeError):
                raise AgentError("Le script de libération n’a pas rendu de rapport lisible.") from None
            if not isinstance(rapport, dict) or not rapport.get("ok"):
                raise AgentError("Le script de libération n’a pas confirmé son exécution.")
            fermes = rapport.get("closed_count") or 0
            libere = rapport.get("freed_ram_mb")
            # Le script garde ComfyUI ouvert (c'est un moteur Alpine Makers) : sans cette
            # demande, son checkpoint garderait la VRAM et le bouton ne tiendrait pas son nom.
            # Une commande de maintenance n'arrive jamais pendant un calcul.
            rapport["image_engine_vram_released"] = False if dry_run else release_comfyui_vram_when_idle()
            rapport["message"] = (
                f"Simulation : {fermes} programme(s) seraient fermés." if dry_run else
                f"{fermes} programme(s) fermés" + (f", {int(libere)} Mo de RAM libérés." if isinstance(libere, (int, float)) else ".")
                + (" VRAM du moteur image rendue." if rapport["image_engine_vram_released"] else "")
            )
            rapport["progress"] = 100
            self.log("system.free_resources", rapport["message"])
            return rapport
        finally:
            self.free_resources_lock.release()

    def audit_storage(self, command_id):
        if not self.storage_audit_lock.acquire(blocking=False):
            raise AgentError("Un contrôle des doublons est déjà en cours sur ce Worker.")
        try:
            if __package__:
                from .storage_audit import scan_roots
            else:
                from storage_audit import scan_roots
            def progress(percent, message):
                # « Arrêter les jobs » : point d'arrêt sûr, le contrôle ne modifie aucun fichier.
                if self._install_cancel_requested(command_id):
                    raise InstallationCancelled("Contrôle des doublons annulé ; aucun fichier modifié.")
                self.command_progress(command_id, percent, message)
            progress(0, "Inventaire des moteurs, modules et modèles, sans modification…")
            result = scan_roots(self.storage_audit_roots(), progress=progress)
            result["progress"] = 100
            result["message"] = "Contrôle terminé, aucun fichier modifié." if result.get("complete") else "Contrôle incomplet : consulte les erreurs. Aucun fichier modifié."
            return result
        finally:
            self.storage_audit_lock.release()

    # ------------------------------------------------------------ maintenance des environnements
    def _engine_runtimes_running(self):
        """Moteurs qui tiennent des fichiers du Python isolé : tant qu'un seul tourne, le runtime n'est pas échangé."""
        running = []
        for tool_id in ("image_generation", "ai3d", "printguard", "model-studio"):
            try:
                # Un moteur qui démarre (processus vivant, port pas encore ouvert) charge déjà ce Python :
                # le processus enregistré compte, pas seulement le port prêt.
                record = self._runtime_record(tool_id)
                pid, root = record.get("pid"), str(record.get("root") or "").strip()
                process_alive = bool(pid and root and self._pid_alive(pid) and self._runtime_matches(pid, Path(root).resolve()))
                if process_alive or self._runtime_running(tool_id):
                    running.append(tool_id)
            except Exception:  # noqa: BLE001 - un état incertain compte comme « en cours »
                running.append(tool_id)
        watch = self.fire_watch
        try:
            if watch is not None and getattr(watch, "_engines", None):
                running.append("fire-watch")
        except Exception:  # noqa: BLE001
            running.append("fire-watch")
        return running

    def _environment_maintenance_safe(self):
        """(sûr, motif) : aucune installation incertaine ou en cours, aucun job actif, aucun nettoyage local."""
        with self.maintenance_lock:
            if self.local_cleanup_active:
                return False, "nettoyage IA local en cours"
            if self.maintenance_recovery:
                return False, "installation précédente incertaine"
            if self.maintenance_count:
                return False, "installation ou commande de maintenance en cours"
        if self.disconnecting:
            return False, "déconnexion en cours"
        try:
            self._assert_no_active_jobs()
        except AgentError:
            return False, "calcul en cours"
        try:
            self._assert_no_grbl_job("Maintenance des environnements reportée")
        except AgentError:
            return False, "gravure en cours"
        request = environment_maintenance.read_request(self.root) or {}
        if request.get("python_only"):
            # Les pip sont faits ; il ne reste que l'échange du Python isolé, inutile tant qu'un moteur le tient
            # (les moteurs fire-watch, eux, se suspendent hors gravure : voir maintain_environments).
            running = [tool for tool in self._engine_runtimes_running() if tool != "fire-watch"]
            if running:
                return False, "moteur en cours d'exécution : " + ", ".join(running)
        return True, ""

    def environment_maintenance_summary(self):
        """Résumé compact pour le heartbeat : état, dates, Python, environnements mis à niveau, dernière erreur."""
        try:
            request = environment_maintenance.read_request(self.root)
            report = environment_maintenance.read_report(self.root)
        except Exception:  # noqa: BLE001 - jamais d'échec du heartbeat pour ce champ
            return {}
        if not request and not report:
            return {}
        summary = {}
        if self.environment_maintenance_running:
            summary["status"] = "running"
        elif request:
            summary["status"] = "deferred" if self.environment_maintenance_deferred or request.get("python_only") else "pending"
            if self.environment_maintenance_deferred:
                summary["reason"] = self.environment_maintenance_deferred[:200]
        else:
            status = str((report or {}).get("status") or "")
            summary["status"] = {"terminé": "done", "reporté": "deferred", "échec": "failed"}.get(status, "done" if status else "pending")
        if request and isinstance(request.get("requested_at"), (int, float)):
            summary["requested_at"] = float(request["requested_at"])
        if report:
            if isinstance(report.get("finished_at"), (int, float)):
                summary["finished_at"] = float(report["finished_at"])
            python = report.get("python") if isinstance(report.get("python"), dict) else {}
            environments = [item for item in (report.get("environments") or []) if isinstance(item, dict)]
            summary["environments_total"] = len(environments)
            summary["environments_updated"] = sum(1 for item in environments if item.get("status") in {environment_maintenance.STATUS_UPDATED, environment_maintenance.STATUS_CURRENT})
            errors = [str(item.get("error") or "") for item in (python, *environments) if item.get("error") and item.get("status") not in {environment_maintenance.STATUS_CURRENT, environment_maintenance.STATUS_ABSENT}]
            if errors:
                summary["last_error"] = errors[0][:300]
            if summary["status"] == "deferred" and python.get("status") == environment_maintenance.STATUS_DEFERRED and "reason" not in summary:
                summary["reason"] = str(python.get("error") or "")[:200]
        python_version = environment_maintenance.runtime_marker_version(self.root)
        if python_version:
            summary["python_version"] = python_version[:20]
        return summary

    def maintain_environments(self, command_id=None, background=False):
        """Lance installers/environment_maintenance.py dans le périmètre des installateurs et rend son rapport.

        Le Python isolé n'est échangé que si aucun moteur ne tourne ; les .venv en cours d'installation ou de
        réparation sont laissés de côté. Les moteurs, modèles, PyTorch et données ne sont jamais modifiés.
        """
        if not self.environment_maintenance_lock.acquire(blocking=False):
            raise AgentError("Une maintenance des environnements est déjà en cours sur ce Worker.")
        script = Path(__file__).with_name("installers") / "environment_maintenance.py"
        synthetic = command_id is None
        command_id = str(command_id or (ENVIRONMENT_MAINTENANCE_PREFIX + secrets.token_hex(6)))
        finished, paused_fire_watch = threading.Event(), None
        try:
            if not script.is_file():
                raise AgentError("Script de maintenance des environnements absent ; mets à jour le Worker.")
            if background:
                with self.maintenance_lock:
                    self.maintenance_count += 1
                    self.maintenance_active = True
                with self.command_lock:
                    # Même file que la commande serveur : agent.update / restart / disconnect attendent la fin de la
                    # course au lieu de tuer pip ou l'échange du Python au milieu (voir _dispatch_command).
                    self.command_tails["agent.maintain_environments"] = finished
            self.environment_maintenance_running = True
            self.environment_maintenance_deferred = ""
            running = self._engine_runtimes_running()
            # Les moteurs d'analyse fire-watch se suspendent le temps de l'échange (comme pour une opération sur le
            # composant) ; seuls ils ne bloquent l'échange que pendant une gravure, où la surveillance doit rester entière.
            blocking = [tool for tool in running if tool != "fire-watch"]
            if not blocking and "fire-watch" in running and self.active_grbl_jobs():
                blocking = ["fire-watch"]
            arguments = [sys.executable, str(script), "--agent-version", AGENT_VERSION]
            if not blocking:
                arguments.append("--refresh-python")
                # Le temps du téléchargement puis de l'échange : les moteurs d'analyse fire-watch (qui chargent ce
                # Python) sont suspendus et tool.start est refusé ; la boucle de surveillance, elle, continue.
                self.environment_maintenance_refreshing_python = True
                paused_fire_watch = self._suspend_fire_watch("Maintenance des environnements : échange du Python isolé")
            with self.command_lock:
                busy = {key for key, lock in self.component_locks.items() if lock.locked()}
            for component in environment_maintenance.MANAGED_COMPONENTS:
                # Les modules 3D (hunyuan3d-21, open3d-lab) s'installent sous le verrou de leur moteur hunyuan3d.
                if component in busy or (component in {"hunyuan3d-21", "open3d-lab"} and "hunyuan3d" in busy):
                    arguments.extend(("--skip", component))
            environment = dict(os.environ)
            environment["ALPINE_WORKER_ROOT"] = str(self.root)
            if synthetic:
                # Reçu interne marqué avant le lancement : jamais de progression, de résultat ni de réconciliation serveur.
                self.journal.update("commands", command_id, local=True, command="agent.maintain_environments", state="executing")
            else:
                self.command_progress(command_id, 1, "Maintenance des environnements : Python isolé et pip des moteurs IA…")
            try:
                self.run_installer(arguments, environment, command_id, local=synthetic)
            except InstallationCancelled:
                raise
            except AgentError as error:
                environment_maintenance.failure_report(self.root, AGENT_VERSION, environment_maintenance.STATUS_FAILED,
                                                       f"Maintenance interrompue : {str(error)[:300]}", clear_request=True)
                raise
            report = environment_maintenance.read_report(self.root) or {}
            report.setdefault("status", environment_maintenance.STATUS_FAILED)
            report["progress"] = 100
            report["message"] = environment_maintenance.summary_line(report)
            report["engines_running"] = running
            self.log("agent.maintain_environments", report["message"], level="info" if report["status"] == "terminé" else "warning")
            return report
        finally:
            self.environment_maintenance_running = False
            self.environment_maintenance_refreshing_python = False
            if paused_fire_watch:
                self._resume_fire_watch(paused_fire_watch)
            if background:
                with self.maintenance_lock:
                    self.maintenance_count -= 1
                    self.maintenance_active = self.maintenance_count > 0 or bool(self.maintenance_recovery)
                with self.command_lock:
                    if self.command_tails.get("agent.maintain_environments") is finished:
                        self.command_tails.pop("agent.maintain_environments", None)
            if synthetic:
                # Reçu interne : il n'a pas de commande serveur à conclure et ne doit pas être renvoyé à chaque heartbeat.
                try:
                    receipt = self.journal.get("commands", command_id) or {}
                    if receipt.get("installer_exit_confirmed") is not False or command_id not in self.maintenance_recovery:
                        self.journal.update("commands", command_id, state="acked")
                except Exception:  # noqa: BLE001
                    pass
            finished.set()
            self.environment_maintenance_lock.release()
            with self.inventory_lock:
                self.inventory_revision += 1
                self.inventory_updated_at = 0
            self.reconnect_requested.set()

    def _environment_maintenance_loop(self):
        """Fil d'arrière-plan : attend un moment sûr, lance la maintenance, puis s'arrête quand la demande est servie.
        Le délai de 24 h court depuis la demande (requested_at), pas depuis le dernier redémarrage de l'agent ; au-delà,
        « reportée ». Une demande qui reste après la course (échange du Python en attente) est réessayée."""
        while not self.stop_requested.is_set():
            request = environment_maintenance.read_request(self.root)
            if request is None:
                self.environment_maintenance_deferred = ""
                return
            requested_at = request.get("requested_at")
            if not isinstance(requested_at, (int, float)) or not 0 < float(requested_at) <= time.time() + 3600:
                requested_at = time.time()
            deadline = float(requested_at) + self.environment_maintenance_deadline
            safe, reason = self._environment_maintenance_safe()
            if safe:
                try:
                    self.maintain_environments(background=True)
                    if environment_maintenance.read_request(self.root) is None:
                        return
                    reason = str(((environment_maintenance.read_report(self.root) or {}).get("python") or {}).get("error") or "échange du Python isolé en attente")[:200]
                except AgentError as error:
                    reason = str(error)[:200]
                    print(f"Maintenance des environnements reportée ({reason}).", flush=True)
                except Exception as error:  # noqa: BLE001 - jamais un fil mort en silence
                    try:
                        environment_maintenance.failure_report(self.root, AGENT_VERSION, environment_maintenance.STATUS_FAILED,
                                                               f"Maintenance interrompue ({type(error).__name__}) : {str(error)[:300]}", clear_request=True)
                    except OSError:
                        pass
                    print(f"Maintenance des environnements interrompue ({type(error).__name__}).", flush=True)
                    return
            self.environment_maintenance_deferred = reason
            if time.time() >= deadline:
                try:
                    environment_maintenance.failure_report(self.root, AGENT_VERSION, environment_maintenance.STATUS_DEFERRED,
                                                           f"reportée : {reason} pendant 24 h ; relance « Mettre à jour » ou la maintenance quand le Worker est libre.",
                                                           clear_request=True)
                except OSError:
                    pass
                self.environment_maintenance_deferred = ""
                return
            self.stop_requested.wait(self.environment_maintenance_retry)

    def _start_environment_maintenance(self):
        """Au démarrage : une demande laissée par la mise à jour lance le fil d'attente, sinon rien."""
        if environment_maintenance.read_request(self.root) is None:
            return None
        thread = self.environment_maintenance_thread
        if thread is not None and thread.is_alive():
            return thread
        thread = threading.Thread(target=self._environment_maintenance_loop, name="worker-environment-maintenance", daemon=True)
        self.environment_maintenance_thread = thread
        thread.start()
        return thread

    def _job_identity_matches(self, item):
        return item.get("worker_id", "") == str(self.config.get("worker_id") or "") and item.get("server_url", "") == str(self.config.get("server_url") or "")

    @staticmethod
    def _job_active(item):
        return item.get("state") not in TERMINAL or item.get("engine_stop_confirmed") is not True

    def _assert_no_active_jobs(self):
        if self.maintenance_recovery:
            raise AgentError("Une installation précédente est encore active ou incertaine ; maintenance et nouveaux jobs restent bloqués jusqu’à confirmation de son arrêt.")
        # Un job laissé par une identité PRÉCÉDENTE (Worker réassocié après
        # révocation ou suppression de sa fiche) ne peut plus être conclu : le
        # serveur ne le connaît pas et n'enverra jamais la confirmation d'arrêt
        # attendue. Sans ce filtre il bloquerait la maintenance à vie, y compris
        # la relance du moteur qui vient de tomber. `_assert_no_live_runner`
        # appliquait déjà le même filtre ; cette dissymétrie était le défaut.
        if any(self._job_active(item) for item in self.journal.records("jobs").values() if self._job_identity_matches(item)):
            raise AgentError("Maintenance bloquée : un job est actif ou son arrêt n’est pas confirmé. Annule-le puis attends la confirmation du moteur.")

    def _assert_no_live_runner(self):
        """Mise à jour ou redémarrage de l'agent : refusés seulement pendant un calcul réel.

        Un job « incertain » ou en attente d'arrêt sans runner vivant ne bloque
        pas : le nouvel agent le reprend et, souvent, c'est lui qui saura le
        conclure. Une installation incertaine, elle, reste bloquante.
        """
        if self.maintenance_recovery:
            raise AgentError("Une installation précédente est encore active ou incertaine ; maintenance et nouveaux jobs restent bloqués jusqu’à confirmation de son arrêt.")
        for item in self.journal.records("jobs").values():
            if not self._job_active(item) or not self._job_identity_matches(item):
                continue
            workdir = str(item.get("workdir") or "")
            if workdir and runner_active(Path(workdir)):
                raise AgentError("Mise à jour reportée : un calcul est réellement en cours sur ce Worker. Attends sa fin ou arrête-le, puis réessaie.")

    def _install_comfy_control(self, root):
        self._assert_no_active_jobs()
        source = Path(__file__).with_name("comfyui_control_extension.py")
        if not source.is_file():
            raise AgentError("Extension d’annulation ComfyUI absente du paquet Worker.")
        target_root = Path(root).resolve() / "custom_nodes" / "alpine_worker_control"
        if Path(root).resolve() not in target_root.resolve().parents:
            raise AgentError("Dossier d’extension ComfyUI non autorisé.")
        target_root.mkdir(parents=True, exist_ok=True)
        target = target_root / "__init__.py"
        staged = target.with_name(".control-" + secrets.token_hex(16) + ".update")
        with source.open("rb") as original, staged.open("xb") as output:
            shutil.copyfileobj(original, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(staged, target)
        token_path = self.root / "config" / "engine-control-token"
        token_path.parent.mkdir(parents=True, exist_ok=True)
        if not token_path.is_file():
            descriptor = os.open(token_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(secrets.token_urlsafe(48))
                handle.flush()
                os.fsync(handle.fileno())

    def execute(self, item):
        self._check_install_cancel(item)
        command = str(item.get("command") or "")
        maintenance = command.startswith(("tool.", "component.", "model.")) or command in {"agent.update", "agent.restart", "agent.disconnect", "worker.purge", "equipment.install", "printguard.camera", "printguard.settings", "agent.maintain_environments"}
        if maintenance:
            with self.maintenance_lock:
                if self.local_cleanup_active:
                    raise AgentError("Nettoyage IA local en cours ; reessaie apres sa fin.")
                if command in {"agent.update", "agent.restart", "agent.disconnect", "worker.purge"} and self.environment_maintenance_running:
                    # La maintenance d'arrière-plan échange le Python isolé ou remplace pip : quitter maintenant
                    # laisserait un runtime à moitié renommé ou un .venv sans pip (la file _dispatch_command attend
                    # déjà sa fin ; ce contrôle couvre une course lancée après la mise en file).
                    raise AgentError("Mise à jour reportée : maintenance des environnements en cours, réessaie dans quelques minutes."
                                     if command == "agent.update" else
                                     "Opération reportée : maintenance des environnements en cours, réessaie dans quelques minutes.")
                if command in {"agent.update", "agent.restart"}:
                    # Le journal durable reprend les jobs après le redémarrage :
                    # seul un runner encore vivant (calcul réel) fait attendre.
                    self._assert_no_live_runner()
                    # Un job GRBL, lui, ne se reprend pas : redémarrer l'agent fermerait le port série
                    # et la surveillance IA au milieu de la gravure.
                    self._assert_no_grbl_job("Mise à jour de l’agent refusée" if command == "agent.update"
                                             else "Redémarrage de l’agent refusé")
                else:
                    self._assert_no_active_jobs()
                    if command == "worker.purge":
                        # La suppression arrête la surveillance IA pendant le nettoyage puis quitte l'agent : jamais
                        # en pleine gravure (le serveur ne voit pas les jobs GRBL lancés par equipment.command).
                        self._assert_no_grbl_job("Suppression du Worker refusée")
                self.maintenance_count += 1
                self.maintenance_active = True
                lifecycle = command in AGENT_LIFECYCLE_COMMANDS or command == "worker.purge"
                if lifecycle:
                    self.lifecycle_commands += 1   # même verrou que le contrôle « aucun job GRBL » ci-dessus
            try:
                if command in AGENT_LIFECYCLE_COMMANDS:
                    self._end_ai_logins_for_lifecycle(item)
                return self._execute_serialized(item)
            finally:
                with self.maintenance_lock:
                    self.maintenance_count -= 1
                    self.maintenance_active = self.maintenance_count > 0 or bool(self.maintenance_recovery)
                    if lifecycle:
                        self.lifecycle_commands -= 1
        return self._execute_serialized(item)

    def _end_ai_logins_for_lifecycle(self, item):
        """Mise à jour, redémarrage ou déconnexion acceptés (contrôles de refus passés) : les connexions de comptes IA
        en attente ou en cours sont annulées avec un message clair, puis leurs files sont attendues (fin rapide)."""
        with self.command_lock:
            tails, logins = self.lifecycle_ai_tails.pop(str(item.get("command_id") or ""), ([], None))
        if self.ai_accounts is not None:
            self.ai_accounts.cancel_all("Connexion annulée : mise à jour, redémarrage ou déconnexion de l’agent demandé.",
                                        command_ids=logins)
        for tail in tails:
            tail.wait()

    def _execute_serialized(self, item):
        command = str(item.get("command") or "")
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if command == "equipment.install":
            with self.command_lock:
                lock = self.component_locks.setdefault("equipment:installation", threading.Lock())
            with lock:
                return self._execute(item)
        if command.startswith(("tool.", "component.", "model.", "asset.")) or command in {"printguard.camera", "printguard.settings"}:
            # Installing a checkpoint and starting/repairing its engine must
            # not race over the same directory. Other engines and heartbeats
            # remain independent while this operation waits its turn.
            identifier = "printguard" if command.startswith("printguard.") else str(payload.get("tool_id") or payload.get("component_id") or "")
            key = TOOL_HANDLERS.get(identifier, identifier)
            if key == "orca-bridge":
                key = "orca"
            with self.command_lock:
                lock = self.component_locks.setdefault(key, threading.Lock())
            with lock:
                try:
                    return self._execute(item)
                finally:
                    # Ce qui vient d'être installé ou retiré change ce que le disque
                    # porte : la prochaine mesure ne doit pas resservir l'ancienne.
                    if command.startswith(("tool.", "component.", "model.", "asset.")):
                        self._oublier_tailles()
        return self._execute(item)

    def _fetch_asset_upload(self, asset, command_id):
        """Rapatrie un fichier que le propriétaire a envoyé au dashboard depuis son navigateur (canal authentifié du
        Worker, jamais une adresse libre). Il arrive dans le dossier temporaire de sa destination ; l'installateur le
        vérifie ensuite exactement comme un téléchargement (empreinte, format, nature du contenu)."""
        if __package__:
            from .installers import asset_installer
        else:
            from installers import asset_installer
        descriptor, partial, expected = asset_installer.upload_target(self.root, asset)
        if partial.is_file() and partial.stat().st_size == expected:
            return                                    # déjà rapatrié lors d'un essai précédent : la vérification décide
        self.reload_config()
        headers = {"Authorization": f"Bearer {self.config.get('token','')}",
                   "X-Worker-ID": str(self.config.get("worker_id") or ""),
                   "User-Agent": f"AlpineWorker/{AGENT_VERSION}"}
        target = f"{self.server}/api/worker-protocol/assets/uploads/{urllib.parse.quote(descriptor['upload_id'])}"
        request = protect_credentials(urllib.request.Request(target, headers=headers, method="GET"))
        done, reported = 0, 0.0
        self.command_progress(command_id, 3, f"Réception de {descriptor['filename']} depuis le dashboard…")
        try:
            with urllib.request.urlopen(request, timeout=3600) as response, partial.open("wb") as output:
                while True:
                    if self._install_cancel_requested(command_id):
                        raise InstallationCancelled("Import annulé ; rien n’a été installé.")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > expected:
                        raise AgentError("Le fichier reçu est plus volumineux qu’annoncé ; import refusé.")
                    output.write(chunk)
                    if time.monotonic() - reported >= 1:
                        reported = time.monotonic()
                        self.command_progress(command_id, 3 + 80 * done / max(1, expected), f"Réception de {descriptor['filename']}…",
                                              bytes_done=done, bytes_total=expected)
            if done != expected:
                raise AgentError("Fichier importé incomplet ; relance l’import.")
        except urllib.error.HTTPError as error:
            self._discard_partial(partial)
            raise AgentError("Le dashboard ne propose plus ce fichier (import expiré ou déjà installé) ; renvoie-le depuis le LoRA Manager."
                             if error.code in (404, 410) else f"Le dashboard a refusé de remettre le fichier importé (HTTP {error.code}).") from None
        except (OSError, AgentError):
            self._discard_partial(partial)
            raise

    @staticmethod
    def _discard_partial(partial):
        try:
            Path(partial).unlink()
        except OSError:
            pass

    def manage_asset(self, command, payload, command_id):
        """LoRA, embeddings, ControlNet et VAE ajoutés par le propriétaire du Worker (agent 1.36.0).

        Le site ne fournit ni chemin ni ligne de commande : un type, un nom de fichier simple et, pour une
        installation, une source https que installers/asset_installer.py revérifie entièrement (hôte public,
        format safetensors, taille, SHA-256). La clé d'accès éventuelle n'est jamais écrite sur disque.
        """
        if str(payload.get("tool_id") or "") != "image_generation":
            raise AgentError("Ressource non gérée : seul le moteur d’images en accepte.")
        comfy_root = WorkerStorage(self.root, self.config).component("comfyui")
        try:
            if command == "asset.scan":
                if set(payload) - {"tool_id"}:
                    raise AgentError("L’inventaire des ressources n’accepte aucun chemin ni paramètre distant.")
                self.command_progress(command_id, 5, "Inventaire des ressources ComfyUI…")
                found = comfyui_assets.scan_assets(self.root, comfy_root, hashes=True,
                                                   progress=lambda message: self.command_progress(command_id, 40, message))
                return {"assets": found[:ASSET_REPORT_LIMIT], "truncated": len(found) > ASSET_REPORT_LIMIT,
                        "diagnostic": comfyui_assets.diagnostic(self.root, comfy_root),
                        "progress": 100, "message": f"{len(found)} ressource(s) inventoriée(s)."}
            if command == "asset.uninstall":
                if set(payload) - {"tool_id", "kind", "name", "installation_label"}:
                    raise AgentError("Suppression de ressource : paramètre inconnu refusé.")
                removed = comfyui_assets.remove_asset(self.root, comfy_root, payload.get("kind"), payload.get("name"))
                return {"removed": removed, "progress": 100, "message": "Ressource supprimée."}
            if command == "asset.install":
                if set(payload) - {"tool_id", "asset", "auth_token", "installation_label"}:
                    raise AgentError("Installation de ressource : paramètre inconnu refusé.")
                script = Path(__file__).with_name("installers") / "asset_installer.py"
                if not script.is_file():
                    raise AgentError("Installateur de ressources indisponible : mets à jour le Worker.")
                result_file = self.root / "temp" / ("asset-" + hashlib.sha256(str(command_id).encode()).hexdigest()[:24] + ".json")
                result_file.parent.mkdir(parents=True, exist_ok=True)
                environment = dict(os.environ)
                environment["ALPINE_WORKER_ROOT"] = str(self.root)
                environment["ALPINE_ASSET_DESCRIPTOR"] = json.dumps(payload.get("asset"), ensure_ascii=True)
                environment["ALPINE_ASSET_RESULT"] = str(result_file)
                if isinstance(payload.get("asset"), dict) and payload["asset"].get("provider") == "upload":
                    self._fetch_asset_upload(payload["asset"], command_id)
                token = payload.get("auth_token")
                if isinstance(token, str) and token and token != "«redacted»":
                    environment["ALPINE_ASSET_TOKEN"] = token
                self.command_progress(command_id, 1, "Préparation du téléchargement sur le Worker…")
                try:
                    result = self.run_installer([sys.executable, str(script), "install"], environment, command_id)
                    detail = read_json(result_file, {})
                finally:
                    try:
                        result_file.unlink()
                    except OSError:
                        pass
                return {**result, **(detail if isinstance(detail, dict) else {})}
        except comfyui_assets.AssetError as error:
            raise AgentError(str(error)) from error
        raise AgentError("Commande de ressource inconnue.")

    def _check_legal_operation(self, command, payload):
        try:
            local_sandbox.refuse_unsafe_operation(command, payload)
            return legal_compliance.check_operation(self.root, command, payload, request=self.request)
        except (ValueError, legal_compliance.LegalCheckError) as error:
            raise AgentError(str(error)) from error

    def _execute(self, item):
        self._check_install_cancel(item)
        command = str(item.get("command") or "")
        if command not in ALLOWED_COMMANDS: raise AgentError("Commande non autorisée par l’agent.")
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        # Must precede engine stops, component adoption and any installer writes.
        if command != "tool.start":  # checked centrally by control_runtime, including local starts
            self._check_legal_operation(command, payload)
        equipment_handlers = {"equipment.install": "install", "equipment.test": "test",
                              "equipment.relay.start": "start_relay", "equipment.relay.stop": "stop_relay"}
        if command in equipment_handlers:
            return getattr(self.equipment_relay, equipment_handlers[command])(payload, item.get("command_id"))
        if command == "equipment.command":
            return self.equipment_relay.command(payload, item.get("command_id"))
        if command == "printguard.camera":
            return self.configure_printguard_camera(payload, item.get("command_id"))
        if command == "printguard.settings":
            return self.configure_printguard_settings(payload, item.get("command_id"))
        if command == "printguard.autopause":
            return self.configure_printguard_autopause(payload)
        if command == "storage.audit":
            if payload:
                raise AgentError("Le contrôle des doublons n’accepte aucun chemin ni paramètre distant.")
            return self.audit_storage(item.get("command_id"))
        if command == "system.free_resources":
            if set(payload) - {"dry_run"}:
                raise AgentError("La libération de mémoire n’accepte aucun chemin ni programme distant.")
            return self.free_resources(bool(payload.get("dry_run")), item.get("command_id"))
        if command == "agent.maintain_environments":
            if payload:
                raise AgentError("La maintenance des environnements n’accepte aucun chemin ni paramètre distant.")
            return self.maintain_environments(item.get("command_id"))
        if command.startswith("asset."):
            return self.manage_asset(command, payload, item.get("command_id"))
        if command.startswith("ai_accounts."):
            # Paramètres vérifiés strictement par ai_accounts.py ; jamais journalisés (seule la réponse l'est).
            if item.get("payload") is not None and not isinstance(item.get("payload"), dict):
                raise AgentError("Paramètres de comptes IA invalides (objet attendu).")
            return self.ai_account_manager().execute(command, payload, item.get("command_id"))
        if command == SITE_ASSISTANT_COMMAND:
            # Charge vérifiée strictement par site_assistant.py ; la question et la réponse ne sont jamais journalisées.
            if not isinstance(item.get("payload"), dict):
                raise AgentError("Paramètres de la question à l’assistant invalides (objet attendu).")
            return self.site_assistant_manager().execute(command, item["payload"], item.get("command_id"))
        if command == "job.run":
            self._start_job(payload)
            return {"accepted": True}
        if command == "job.cancel":
            return self.cancel_job(str(payload.get("job_id") or ""))
        if command == "agent.update":
            return self.download_agent_update()
        if command == "agent.restart":
            # The result must reach the dashboard before this process exits.
            # _process_command starts the replacement only after that response.
            return {"restarting": True}
        if command == "agent.disconnect":
            return self.prepare_disconnect(item.get("command_id"))
        if command == "worker.purge":
            return self.purge_managed_components()
        if command in {"tool.start", "tool.stop"}:
            tool_id = str(payload.get("tool_id") or "")
            self.command_progress(item.get("command_id"), 10, "Vérification des tâches et du moteur…")
            self.runtime_command_context.command_id = item.get("command_id")
            try:
                result = self.control_runtime(tool_id, command.split(".", 1)[1])
            finally:
                self.runtime_command_context.command_id = None
            self.journal.update("runtime_intent", tool_id, enabled=command == "tool.start")
            # The runtime state just confirmed is the truth for this tool: patch
            # the snapshot, keep it fresh (marking it stale made every tool
            # "unknown" for a heartbeat or two, so the dashboard showed nothing
            # running right after a start), discard any refresh already in
            # flight, and send a heartbeat now instead of up to 20 s later.
            with self.inventory_lock:
                self.inventory_revision += 1
                self.inventory_updated_at = time.monotonic()
                for tool in self.inventory_snapshot.get("tools", []):
                    if tool.get("tool_id") == tool_id:
                        tool["status"] = "online" if command == "tool.start" else "ready"
                        tool["config"] = {**(tool.get("config") or {}), "runtime_enabled": command == "tool.start"}
            self.command_progress(item.get("command_id"), 100, "Moteur prêt." if command == "tool.start" else "Arrêt confirmé.")
            self.reconnect_requested.set()
            return result
        parts = command.split(".", 1); kind, action = parts
        identifier = str(payload.get(f"{kind}_id") or "")
        known = TOOL_HANDLERS.get(identifier, identifier) if kind == "tool" else identifier
        manual_model_removal = kind == "model" and action == "uninstall" and payload.get("tool_id") == "image_generation"
        if not known or len(known) > 160 or (not manual_model_removal and any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for ch in known)):
            raise AgentError("Identifiant de composant invalide.")
        tool_id = str(payload.get("tool_id") or "")
        engine = TOOL_HANDLERS.get(tool_id)
        storage = WorkerStorage(self.root, self.config)
        ownership = storage.component_ownership(engine) if engine else None
        adopt_managed = False
        if kind == "tool" and ownership and not ownership["managed"]:
            if action == "install" and ownership["managed_install_available"]:
                if __package__:
                    from .installers.component_installer import component_ready
                else:
                    from installers.component_installer import component_ready
                if component_ready(engine, Path(ownership["engine_root"])):
                    return {"already_installed": True, "message": "Moteur externe déjà opérationnel ; aucune copie créée."}
                adopt_managed = True
            else:
                raise AgentError("Moteur externe : modification automatique refusée pour protéger les données hors de l’espace Worker.")
        refresh_shape_adapter = kind == "model" and action == "install" and known in HUNYUAN_SNAPSHOT_OPTIONS
        # Le module de surveillance IA ne dépend pas du moteur laser : le réparer ou le retirer ne coupe pas
        # l'interrupteur d'Alpine Laser Studio, et son installation n'exige pas le moteur laser.
        fire_watch_module = kind == "component" and known == "fire-watch"
        if fire_watch_module:
            self._assert_no_grbl_job("Opération sur la surveillance IA refusée")
        if (action in {"uninstall", "update", "repair"} or refresh_shape_adapter) and engine and not fire_watch_module:
            # Persist the stop intent, including model/module removals, so supervision cannot restart it.
            self.control_runtime(tool_id, "stop")
            self.journal.update("runtime_intent", tool_id, enabled=False)
        if kind == "model" and action != "uninstall":
            detected, _ = self.discover_components()
            facts = next((t.get("config", {}).get("installation", {}) for t in detected if t["tool_id"] == tool_id), {})
            if facts.get("engine_status") != "installed":
                raise AgentError("Installe d’abord le moteur depuis Mes Workers → Installations.")
            if known in {"hunyuan3d-2-paint", "hunyuan3d-2-paint-turbo", "hunyuan3d-21-paint-pbr"} and facts.get("modules", {}).get("cuda-toolkit") != "installed":
                raise AgentError("Installe d’abord le module CUDA Toolkit pour Paint/Textures.")
            backend = AI3D_BACKENDS.get(known)
            if backend and facts.get("modules", {}).get(backend) != "installed":
                raise AgentError(f"Installe d’abord le module {backend} depuis Mes Workers → Installations.")
        # Installers are local, shipped and selected by ID.  No server-provided
        # command line, URL or executable is ever accepted.
        if kind == "model":
            script = Path(__file__).with_name("installers") / "model_installer.py"
            arguments = [sys.executable, str(script), str(payload.get("tool_id") or ""), known, action]
        else:
            tool_id = str(payload.get("tool_id") or "")
            script = Path(__file__).with_name("installers") / "component_installer.py"
            arguments = [sys.executable, str(script), known, action]
        if not script.is_file(): raise AgentError(f"Installateur contrôlé indisponible : {script.name}")
        environment = dict(os.environ); environment["ALPINE_WORKER_ROOT"] = str(self.root)
        self.command_progress(item.get("command_id"), 1, "Préparation de l’opération sur le Worker…")
        paused_fire_watch = None
        try:
            self._check_install_cancel(item)
            if fire_watch_module and self.fire_watch is not None:
                # Le moteur d'analyse tient les fichiers de son .venv et de ses modèles (verrous Windows) : seuls
                # les moteurs sont suspendus pendant l'opération puis relancés (ils relisent le composant à jour).
                # La boucle d'analyse continue (motif « maintenance ») : un job GRBL lancé pendant l'installation
                # reste soumis à la garde de départ et à la réaction de perte. Ancien service : arrêt complet.
                operation = {"install": "Installation", "update": "Mise à jour", "repair": "Réparation",
                             "uninstall": "Désinstallation"}.get(action, "Opération")
                paused_fire_watch = self._suspend_fire_watch(f"{operation} du moteur de surveillance en cours sur ce Worker")
            result = self.run_installer(arguments, environment, item.get("command_id"))
            if adopt_managed:
                storage.activate_managed_component(engine, ownership["configured_path"])
                self.reload_config()
            return result
        finally:
            if paused_fire_watch:
                self._resume_fire_watch(paused_fire_watch)
            with self.inventory_lock:
                self.inventory_revision += 1
                self.inventory_updated_at = 0
                for entry in self.inventory_snapshot.get("tools", []) + self.inventory_snapshot.get("models", []):
                    if entry.get("tool_id") == tool_id:
                        entry["status"] = "unknown"

    def prepare_disconnect(self, command_id):
        """Arrête tout ce que ce Worker a lancé, avant de confirmer la déconnexion.

        Si un moteur ne peut pas être arrêté de façon vérifiée, le Worker reste en
        ligne : le mettre hors ligne cacherait un processus encore actif. Les
        intentions de démarrage sont conservées pour la prochaine connexion.
        """
        stopped = []
        self.runtime_command_context.command_id = command_id
        try:
            for index, tool_id in enumerate(DISCONNECT_RUNTIMES):
                self.command_progress(command_id, 10 + index * 15, f"Arrêt du moteur {tool_id}…")
                try:
                    result = self.control_runtime(tool_id, "stop")
                except AgentError as error:
                    raise AgentError(f"Déconnexion annulée : le moteur {tool_id} n’a pas pu être arrêté ({error}). Le Worker reste en ligne.") from error
                if result.get("stopped"):
                    stopped.append(tool_id)
        finally:
            self.runtime_command_context.command_id = None
        self.disconnecting = True
        self.command_progress(command_id, 80, "Fermeture des liaisons imprimante, caméra et relais vidéo…")
        self.equipment_relay.sync([])
        self.command_progress(command_id, 95, "Envoi de la confirmation, puis arrêt de l’agent.")
        return {"disconnecting": True, "stopped_runtimes": stopped,
                "message": "Moteurs et liaisons arrêtés ; le Worker se met hors ligne."}

    def _supervisor_stops_on_disconnect(self):
        codes = str(os.environ.get("ALPINE_WORKER_SUPERVISOR_EXIT_CODES") or "").split(",")
        return str(DISCONNECT_EXIT_CODE) in {code.strip() for code in codes}

    def _stop_legacy_supervisor(self):
        """Arrête un ancien run_worker.ps1 qui relancerait l'agent en boucle.

        Seul le processus parent dont la ligne de commande désigne le
        run_worker.ps1 de CE Worker est visé ; un superviseur à jour s'arrête
        de lui-même sur DISCONNECT_EXIT_CODE.
        """
        if os.name != "nt" or self._supervisor_stops_on_disconnect():
            return False
        parent = os.getppid()
        if not parent or not self._runtime_matches(parent, self.root / "run_worker.ps1"):
            self.log("runtime.supervisor", f"Ancien superviseur non identifié (parent {parent}) ; l'agent attendra hors ligne s'il est relancé.", level="warning")
            return False
        result = run(["taskkill", "/PID", str(int(parent)), "/F"], timeout=10)
        if result.returncode != 0:
            # taskkill peut être refusé selon la façon dont la tâche planifiée a
            # lancé le superviseur ; Stop-Process passe par une autre voie.
            result = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                          f"Stop-Process -Id {int(parent)} -Force -ErrorAction Stop"], timeout=15)
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        stopped = result.returncode == 0
        self.log("runtime.supervisor", ("Ancien superviseur arrêté ; relance-le avec MENU-WORKER.bat (5 · Reconnecter le Worker)." if stopped else
                 f"Ancien superviseur non arrêté ({detail[-1][:120] if detail else 'code ' + str(result.returncode)}) ; l'agent attendra hors ligne."),
                 level="info" if stopped else "warning")
        return stopped

    def _disconnect_after_result(self):
        if self.restart_requested.is_set():
            return
        self.exit_code_pending = DISCONNECT_EXIT_CODE
        self.restart_requested.set()
        self.disconnecting = True
        atomic_json(self.disconnected_marker, {
            **self._boot_marker(), "at": time.time(),
            "message": "Worker déconnecté depuis le dashboard. Relance MENU-WORKER.bat (5 · Reconnecter le Worker) ou redémarre le PC pour le reconnecter.",
        })
        self.stop_requested.set()
        self._stop_fire_watch(timeout=3.0)
        try:
            self.equipment_relay.sync([])
        except Exception as error:
            print(f"Fermeture des équipements incomplète ({type(error).__name__}).", flush=True)
        try:
            self._stop_legacy_supervisor()
        except Exception as error:
            print(f"Ancien superviseur non arrêté ({type(error).__name__}) ; l'agent attendra hors ligne.", flush=True)
        time.sleep(0.5)
        exit_process(DISCONNECT_EXIT_CODE)

    def finish_requested_exit(self, timeout=30.0):
        """Après la boucle principale : si une sortie a été demandée (déconnexion surtout, qui arrête la boucle), le fil
        de commande qui la réalise termine le processus. Sans cette attente, ``main()`` finissait d'abord, avec le code 0
        au lieu de 64 (le superviseur relançait l'agent) et par la fin normale de Python, celle qui prévient les DLL.
        Au-delà de ``timeout``, la sortie est faite ici avec le code demandé."""
        code = self.exit_code_pending
        if code is None:
            return
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            time.sleep(0.2)
        exit_process(code)

    def lifecycle_exit_pending(self):
        """Vrai quand l'agent va quitter (mise à jour, redémarrage, déconnexion, suppression acceptés ou en cours) :
        aucun job GRBL ne doit alors commencer, la sortie couperait la liaison série en pleine gravure."""
        return self.restart_requested.is_set() or self.lifecycle_commands > 0

    def _close_equipment_before_exit(self):
        """Arrêt de protection des équipements avant de quitter : job GRBL annulé, laser coupé, liaisons et caméras
        fermées par l'agent plutôt que par le système (comme la déconnexion et la suppression)."""
        try:
            self.equipment_relay.sync([])
        except Exception as error:
            print(f"Fermeture des équipements incomplète ({type(error).__name__}).", flush=True)

    def _restart_after_result(self):
        if self.restart_requested.is_set():
            return
        self.exit_code_pending = 75
        self.restart_requested.set()
        # Moteur d'analyse fermé proprement (il s'arrêterait aussi sur la fin de son entrée standard).
        self._stop_fire_watch(timeout=3.0)
        self._close_equipment_before_exit()
        # The platform installer always runs the agent under run_worker.ps1 (or
        # systemd on Linux), which recreates it after this clean exit.
        time.sleep(1)
        exit_process(75)

    def ai_account_manager(self):
        """Gestionnaire des comptes IA (ai_accounts.py), créé à la première commande : un échec d'import ne touche
        que ces commandes, jamais le démarrage de l'agent."""
        with self.ai_accounts_lock:
            if self.ai_accounts is None:
                try:
                    if __package__:
                        from .ai_accounts import AiAccounts
                    else:
                        from ai_accounts import AiAccounts
                except Exception as error:
                    raise AgentError(f"Gestion des comptes IA indisponible sur ce Worker ({type(error).__name__}) : "
                                     "mets à jour le Worker.") from None
                self.ai_accounts = AiAccounts(self.root, progress=lambda *args, **extra: self.command_progress(*args, **extra),
                                              logins=self.ai_logins, logins_lock=self.ai_logins_lock, error=AgentError)
            return self.ai_accounts

    def site_assistant_manager(self):
        """Assistant IA du site (site_assistant.py), créé à la première question : un échec d'import ne touche que
        cette commande, jamais le démarrage de l'agent."""
        with self.site_assistant_lock:
            if self.site_assistant is None:
                try:
                    if __package__:
                        from .site_assistant import SiteAssistant
                    else:
                        from site_assistant import SiteAssistant
                except Exception as error:
                    raise AgentError(f"Assistant IA indisponible sur ce Worker ({type(error).__name__}) : "
                                     "mets à jour le Worker.") from None
                self.site_assistant = SiteAssistant(self.root, error=AgentError)
            return self.site_assistant

    def _process_command(self, item):
        command_id = str(item.get("command_id") or "")
        try:
            entry = self.journal.get("commands", command_id) or {}
            identity = {key: entry.get(key, value) for key, value in self._command_identity().items()}
            self.journal.update("commands", command_id, state="executing", command=str(item.get("command") or ""), **identity)
            try:
                self._check_install_cancel(item)
                result = self.execute(item)
                response = {"ok": True, "result": result}
            except InstallationCancelled as error:
                receipt = self.journal.get("commands", command_id) or {}
                confirmed = not receipt.get("installer_launch_intent") or receipt.get("installer_exit_confirmed") is True
                response = {"ok": False, "error": str(error), "result": {"cancelled": confirmed, "installer_stop_confirmed": confirmed}}
            except Exception as error:
                response = {"ok": False, "error": str(error)}
            receipt = self.journal.get("commands", command_id) or {}
            uncertain = receipt.get("installer_launch_intent") and receipt.get("installer_exit_confirmed") is not True
            if uncertain:
                response = {"ok": False, "error": "Arrêt de l’installation non confirmé ; aucune répétition automatique.",
                            "result": {"cancelled": False, "installer_stop_confirmed": False}}
            elif item.get("command") in CANCELLABLE_INSTALL_COMMANDS:
                response["result"] = {**response.get("result", {}), "installer_stop_confirmed": True}
            with self.command_lock:
                self.pending_command_results[command_id] = (response, str(item.get("command") or ""))
            state = "uncertain" if uncertain else "completed" if response["ok"] else "cancelled" if response.get("result", {}).get("cancelled") else "failed"
            # Assistant IA du site : le journal durable ne reçoit qu'un reçu sans texte ; la réponse reste en mémoire.
            self.journal.update("commands", command_id, state=state,
                                response=site_assistant_journal_response(response) if item.get("command") == SITE_ASSISTANT_COMMAND else response)
            self._send_command_result(command_id)
        finally:
            with self.command_lock:
                self.command_threads.discard(threading.current_thread())
                self.inflight_commands.discard(command_id)

    def _send_command_result(self, command_id):
        # Retry delivery, never repeat installation/generation just because a
        # response was lost. One sender prevents duplicate restart/purge hooks.
        if not self.result_delivery_lock.acquire(blocking=False):
            return
        try:
            with self.command_lock:
                pending = self.pending_command_results.get(command_id)
            if pending is None:
                return
            response, command = pending
            receipt = self.journal.get("commands", command_id) or {}
            if self._is_local_receipt(command_id, receipt):
                # Le serveur n'a jamais émis cette commande : rien à lui envoyer, le reçu est simplement clos.
                with self.command_lock:
                    self.pending_command_results.pop(command_id, None)
                self.journal.update("commands", command_id, state="acked")
                return
            if receipt.get("installer_launch_intent") and receipt.get("installer_exit_confirmed") is not True:
                return  # Do not release the server reservation while descendants are uncertain.
            if receipt.get("worker_id") and not self._command_identity_matches(receipt):
                return  # A re-pair must not upload a previous account's command receipt.
            try:
                self.request(f"/api/worker-protocol/commands/{urllib.parse.quote(command_id, safe='')}/result", "POST", response)
            except Exception as error:
                print(f"Résultat Worker en attente ({type(error).__name__}); nouvel envoi à la reconnexion.", flush=True)
                return
            with self.command_lock:
                self.pending_command_results.pop(command_id, None)
                self.completed_commands.append(command_id)
            self.journal.update("commands", command_id, state="acked")
            if response["ok"] and command in {"agent.restart", "agent.update"}:
                self._restart_after_result()
            if response["ok"] and command == "agent.disconnect":
                self._disconnect_after_result()
            if response["ok"] and command == "worker.purge":
                self._detach_after_purge()
        finally:
            self.result_delivery_lock.release()

    def _dispatch_command(self, item):
        command_id = str(item.get("command_id") or "")
        if not command_id or len(command_id) > 128 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in command_id):
            return
        finished = threading.Event()
        previous = []
        login = item.get("command") == "ai_accounts.login"
        def ordered_command():
            try:
                for predecessor in previous:
                    predecessor.wait()
                self._process_command(item)
            finally:
                if login and self.ai_accounts is not None:
                    self.ai_accounts.release_login(command_id)
                with self.command_lock:
                    self.lifecycle_ai_tails.pop(command_id, None)
                finished.set()
        thread = threading.Thread(
            target=ordered_command,
            name=f"worker-command-{str(item.get('command_id') or '')[:8]}", daemon=True,
        )
        with self.command_lock:
            existing = self.journal.get("commands", command_id)
            if command_id in self.inflight_commands or command_id in self.pending_command_results or command_id in self.completed_commands or (existing and existing.get("state") != "received"):
                return
            self.journal.update("commands", command_id, state="received", command=str(item.get("command") or ""), **self._command_identity())
            if self.lifecycle_tail is not None and item.get("command") not in AI_ACCOUNTS_UNQUEUED:
                previous.append(self.lifecycle_tail)
            if item.get("command") in AGENT_LIFECYCLE_COMMANDS:
                # Do not replace modules or exit while an installer, camera
                # test or printer command is still executing. Later actions
                # remain durable received commands for the restarted process.
                # Files de comptes IA (connexion jusqu'à 15 min) : pas attendues ici. Une fois les contrôles de refus
                # passés, execute() annule les connexions avec un message clair puis attend ces files ; une commande
                # refusée (job GRBL, calcul en cours…) ne coupe ainsi aucune connexion (1.35.1).
                # Seules les connexions reçues AVANT cette commande sont annulées (réservées sous ce même verrou).
                ai_tails = []
                for key, tail in self.command_tails.items():
                    if tail in previous or tail in ai_tails:
                        continue
                    (ai_tails if str(key).startswith("ai_accounts:") else previous).append(tail)
                logins = self.ai_accounts.login_ids() if self.ai_accounts is not None else set()
                self.lifecycle_ai_tails[command_id] = (ai_tails, logins)
                self.lifecycle_tail = finished
            if str(item.get("command") or "").startswith(("tool.", "model.", "component.", "equipment.", "asset.")) or item.get("command") in {"system.free_resources", "agent.maintain_environments"}:
                payload = item.get("payload") or {}
                if item.get("command") in {"system.free_resources", "agent.maintain_environments"}:
                    # Same lifecycle-blocking treatment as an engine/component command,
                    # under its own fixed key: an agent.update/restart landing mid-run
                    # kills the process and leaves the command "uncertain" instead of
                    # a real result (incident: 17/09 23:07).
                    engine = str(item.get("command"))
                else:
                    engine = TOOL_HANDLERS.get(str(payload.get("tool_id") or ""), str(payload.get("tool_id") or ""))
                    if str(item.get("command") or "").startswith("equipment."):
                        engine = "equipment:"+str(payload.get("equipment_id") or "unknown")
                tail = self.command_tails.get(engine)
                if tail is not None and tail not in previous:
                    previous.append(tail)
                self.command_tails[engine] = finished
            engine = (ai_accounts_queue_key(item.get("command"), item.get("payload"))
                      or site_assistant_queue_key(item.get("command"), item.get("payload")))
            if engine:
                # Connexion, déconnexion, suppression et ajout : un compte à la fois (file propre au compte).
                # Question à l'Assistant IA du site : une CLI à la fois par compte (file « site_assistant:… »).
                tail = self.command_tails.get(engine)
                if tail is not None and tail not in previous:
                    previous.append(tail)
                self.command_tails[engine] = finished
            if login:
                # Connexion enregistrée dès sa réception, avant toute attente de file : « Annuler » et une mise à jour
                # acceptée l'atteignent même si sa CLI n'est pas encore lancée (1.35.1).
                try:
                    self.ai_account_manager().reserve_login(command_id)
                except Exception:  # noqa: BLE001 - gestionnaire indisponible : la commande elle-même l'expliquera
                    pass
            self.inflight_commands.add(command_id)
            self.command_threads.add(thread)
        try:
            thread.start()
        except Exception:
            finished.set()
            if login and self.ai_accounts is not None:
                self.ai_accounts.release_login(command_id)
            with self.command_lock:
                self.inflight_commands.discard(command_id)
                self.command_threads.discard(thread)
                self.lifecycle_ai_tails.pop(command_id, None)
            raise

    @staticmethod
    def _valid_execution_id(value):
        return isinstance(value, str) and 0 < len(value) <= 128 and all(ch in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in value)

    def _accept_job(self, payload):
        job_id = str(payload.get("job_id") or "")
        tool_id = str(payload.get("tool_id") or "")
        if not self._valid_execution_id(job_id):
            raise AgentError("Identifiant du job invalide.")
        if tool_id not in TOOL_HANDLERS or TOOL_HANDLERS[tool_id] not in {"comfyui", "hunyuan3d", "freecad", "orca", "laser-engine"}:
            raise AgentError("Outil de job non autorisé par le Worker.")
        with self.maintenance_lock:
            existing = self.journal.get("jobs", job_id)
            if existing:
                if payload.get("execution_id") and payload["execution_id"] != existing["execution_id"]:
                    raise AgentError("Une autre tentative de ce job est déjà connue ; aucune répétition.")
                if not self._job_identity_matches(existing):
                    raise AgentError("Ce job appartient à une ancienne association Worker.")
                return existing
            if self.maintenance_count or self.maintenance_recovery:
                raise AgentError("Une installation ou maintenance est en cours ; aucun nouveau job ne peut démarrer.")
            self._check_legal_operation("job.run", payload)
            intent = self.journal.get("runtime_intent", tool_id)
            if intent is not None and intent.get("enabled") is False:
                raise AgentError("Ce moteur a été arrêté explicitement. Démarre-le avant de lancer un nouveau job.")
            execution_id = str(payload.get("execution_id") or secrets.token_hex(16))
            if not self._valid_execution_id(execution_id):
                raise AgentError("Identité d’exécution Worker invalide.")
            name = hashlib.sha256((job_id + ":" + execution_id).encode()).hexdigest()[:32]
            workdir = self.root / "jobs" / name
            workdir.mkdir(parents=True, exist_ok=True)
            if (self.root / "jobs").resolve() not in workdir.resolve().parents:
                raise AgentError("Dossier de job invalide.")
            atomic_json(workdir / "job.json", {**payload, "execution_id": execution_id})
            entry = self.journal.update("jobs", job_id, execution_id=execution_id, tool_id=tool_id,
                workdir=str(workdir.resolve()), state="preparing", engine_stop_confirmed=True, attempts=0,
                worker_id=str(self.config.get("worker_id") or ""), server_url=str(self.config.get("server_url") or ""))
            if self.journal.get("cancels", job_id):
                self.cancel_job(job_id)
                entry = self.journal.get("jobs", job_id)
            return entry

    def _start_job(self, payload):
        record = self._accept_job(payload)
        job_id = payload["job_id"]
        with self.command_lock:
            active = self.job_threads.get(job_id)
            if active is not None and active.is_alive():
                return
            if record.get("acked"):
                return
            thread = threading.Thread(target=self.run_job, args=(payload,), name=f"worker-job-{job_id[:8]}", daemon=True)
            self.job_threads[job_id] = thread
            thread.start()

    def cancel_job(self, job_id):
        entry = self.journal.get("jobs", job_id)
        if not entry:
            self.journal.update("cancels", job_id, cancel_requested=True)
            return {"cancel_requested": True, "engine_stop_confirmed": False, "message": "Job pas encore reçu par cet agent."}
        if not self._job_active(entry):
            return {"cancel_requested": True, "engine_stop_confirmed": True}
        self.cancelled.add(job_id)
        atomic_json(Path(entry["workdir"]) / ".cancel.json", {"execution_id": entry["execution_id"]})
        self.journal.update("jobs", job_id, cancel_requested=True, state="cancel_requested")
        return {"cancel_requested": True, "engine_stop_confirmed": False}

    def _job_status(self, job_id, response):
        entry = self.journal.get("jobs", job_id)
        response = {**response, "execution_id": entry["execution_id"]}
        self.journal.update("jobs", job_id, response=response, state=response["status"],
                            engine_stop_confirmed=response.get("engine_stop_confirmed") is True)
        try:
            self.request(f"/api/worker-protocol/jobs/{job_id}/status", "POST", response)
        except Exception:
            return False
        if response["status"] in TERMINAL and response.get("engine_stop_confirmed") is True:
            self.journal.update("jobs", job_id, acked=True)
        return True

    def run_job(self, payload):
        job_id = str(payload.get("job_id") or "")
        try:
            entry = self._accept_job(payload)
            workdir = Path(entry["workdir"])
            if entry.get("response", {}).get("status") in TERMINAL:
                self._job_status(job_id, entry["response"])
                return
            if runner_active(workdir):
                return  # The old runner outlived an agent restart.
            state = read_json(workdir / ".runner-state.json", {})
            if not state and entry.get("cancel_requested"):
                self._job_status(job_id, {"status": "cancelled", "progress": 100, "engine_stop_confirmed": True,
                                         "message": "Annulé avant le lancement du moteur."})
                return
            tool_id = entry["tool_id"]
            payload = read_json(workdir / "job.json")
            parameters = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
            if not entry.get("prepared"):
                single = parameters.get("input_file") if isinstance(parameters.get("input_file"), dict) else {}
                sources = [single] + (parameters.get("input_files") if isinstance(parameters.get("input_files"), list) else [])
                for source in sources:
                    if not isinstance(source, dict) or not source.get("name"):
                        continue
                    name = safe_relative_name(source["name"], basename=True)
                    source["path"] = str(self.download_job_input(job_id, name, workdir / name))
                atomic_json(workdir / "job.json", payload)
                entry = self.journal.update("jobs", job_id, prepared=True)
            if state.get("state") not in TERMINAL:
                self._job_status(job_id, {"status": "cancel_requested" if entry.get("cancel_requested") else "running",
                    "progress": 1, "engine_stop_confirmed": False, "message": "Exécution ou reprise du statut sur le Worker."})
                environment = WorkerStorage(self.root, self.config).environment()
                environment["ALPINE_WORKER_ROOT"] = str(self.root)
                environment["ALPINE_WORKER_CONTROL_TOKEN_FILE"] = str(self.root / "config" / "engine-control-token")
                if tool_id in {"image_generation", "image-generation"}:
                    roots = self.config.get("component_paths") if isinstance(self.config.get("component_paths"), dict) else {}
                    environment["COMFYUI_ROOT"] = str(WorkerStorage(self.root, self.config).component("comfyui"))
                # Réparation de maillage : job « converter » calculé en Python pur (CPU), sans FreeCAD.
                mesh_repair = tool_id == "converter" and parameters.get("task") == "mesh_repair"
                if tool_id == "converter" and not mesh_repair:
                    freecad = self._freecad_executable()
                    if not freecad:
                        raise AgentError("FreeCAD n’est pas installé sur ce Worker.")
                    environment["FREECAD_CMD"] = freecad
                if tool_id == "model-studio":
                    environment["WORKER_ORCA_PORT"] = str(self._runtime_port(tool_id))
                if tool_id == "laser-studio":
                    # Le calcul tourne dans le Python isolé du composant (Pillow), jamais dans celui de l'agent.
                    laser_python = self._laser_engine_python()
                    if not laser_python:
                        raise AgentError("Le moteur laser n’est pas installé sur ce Worker.")
                    environment["LASER_ENGINE_PYTHON"] = laser_python
                runner_name = "mesh_repair" if mesh_repair else TOOL_HANDLERS[tool_id]
                handler = Path(__file__).with_name("runners") / f"{runner_name}.py"
                if not handler.is_file():
                    raise AgentError("Runner Worker indisponible.")
                with (workdir / "runner.log").open("a", encoding="utf-8") as log:
                    self.journal.update("jobs", job_id, launch_intent=True, launch_failed=False)
                    try:
                        process = subprocess.Popen([sys.executable, str(handler), str(workdir / "job.json"), str(workdir)],
                            stdout=log, stderr=subprocess.STDOUT, env=environment,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    except OSError:
                        self.journal.update("jobs", job_id, launch_failed=True)
                        raise
                    self.journal.update("jobs", job_id, runner_pid=process.pid, attempts=entry.get("attempts", 0) + 1)
                    # Do not kill a client on timeout: its engine may still compute.
                    process.wait()
                state = read_json(workdir / ".runner-state.json", {})
            if not state or state.get("state") not in TERMINAL or state.get("engine_stop_confirmed") is not True:
                self._job_status(job_id, {"status": "cancel_requested" if entry.get("cancel_requested") else "uncertain",
                    "engine_stop_confirmed": False, "progress": 1, "message": state.get("error") or "Statut du moteur incertain ; aucune nouvelle soumission."})
                return
            output = dict(state.get("result") or {})
            if state["state"] == "completed":
                uploaded = dict(entry.get("uploaded") or {})
                for candidate in output.get("artifacts", []):
                    candidate_path = (workdir / safe_relative_name(candidate)).resolve()
                    if workdir.resolve() not in candidate_path.parents or not candidate_path.is_file():
                        raise AgentError("Le runner a déclaré un artefact invalide.")
                    if candidate not in uploaded:
                        uploaded[candidate] = self.upload_artifact(job_id, candidate_path)
                        self.journal.update("jobs", job_id, uploaded=uploaded)
                output["artifacts"] = list(uploaded.values())
            self._job_status(job_id, {"status": state["state"], "engine_stop_confirmed": True, "progress": 100,
                                     "message": "Moteur terminé ; résultat confirmé.", "error": state.get("error", ""), "result": output})
        except Exception as error:
            if not self._valid_execution_id(job_id):
                print("Job Worker refusé : identifiant invalide.", flush=True)
                return
            entry = self.journal.get("jobs", job_id)
            if entry:
                try:
                    state = read_json(Path(entry["workdir"]) / ".runner-state.json", {})
                except (OSError, ValueError):
                    state = {"state": "uncertain"}
                safe_failure = not state and (not entry.get("launch_intent") or entry.get("launch_failed") is True)
                self._job_status(job_id, {"status": "failed" if safe_failure else "uncertain", "progress": 1,
                                         "engine_stop_confirmed": safe_failure, "error": str(error)})
            else:
                try:
                    self.request(f"/api/worker-protocol/jobs/{job_id}/status", "POST", {"status": "failed", "engine_stop_confirmed": True, "execution_id": payload.get("execution_id", ""), "error": str(error)})
                except Exception:
                    pass

    def _resume_jobs(self):
        for job_id, entry in self.journal.records("jobs").items():
            if entry.get("acked") or not self._job_identity_matches(entry):
                continue
            with self.command_lock:
                active = self.job_threads.get(job_id)
            if active is not None and active.is_alive():
                continue
            if time.time() - float(entry.get("last_resume_at") or 0) < 10:
                continue
            self.journal.update("jobs", job_id, last_resume_at=time.time())
            self._start_job({"job_id": job_id, "execution_id": entry["execution_id"], "tool_id": entry["tool_id"]})

    def _reconcile_maintenance(self):
        for command_id, item in list(self.maintenance_recovery.items()):
            if item.get("installer_system_phase") and item.get("installer_started"):
                # MSI/winget may outlive the scoped parent after an agent crash.
                # Only the uninterrupted installer return can confirm that phase;
                # an empty private job is not evidence about system services.
                continue
            scope_recorded = item.get("installer_scope")
            if not scope_recorded:
                # Old receipts know only the parent PID, not its descendants.
                # A vanished parent cannot prove that pip/MSI also stopped.
                continue
            scope = None
            try:
                scope = InstallerScope(scope_recorded)
                stopped = scope.stopped()
                if not stopped and not item.get("installer_system_phase") and self._install_cancel_requested(command_id):
                    commit_path = self.root / "runtime" / ("install-" + hashlib.sha256(command_id.encode()).hexdigest() + ".lock")
                    with installation_commit(commit_path, blocking=False) as allowed:
                        if allowed:
                            scope.terminate()
                            stopped = scope.wait_stopped()
            except (OSError, ValueError):
                continue
            finally:
                if scope is not None:
                    scope.close()
            if stopped and self._is_local_receipt(command_id, item):
                # Maintenance d'arrière-plan interrompue : arrêt confirmé, reçu clos sans résultat à livrer. La demande
                # (runtime/.environment-maintenance-request.json), si elle reste, relance la maintenance plus tard.
                self.journal.update("commands", command_id, installer_exit_confirmed=True, state="acked")
                with self.maintenance_lock:
                    self.maintenance_recovery.pop(command_id, None)
                    self.maintenance_active = self.maintenance_count > 0 or bool(self.maintenance_recovery)
                self._start_environment_maintenance()
                continue
            if stopped:
                receipt = self.journal.get("commands", command_id) or item
                cancelled = self._install_cancel_requested(command_id)
                response = ({"ok": False, "error": "Installation annulée ; arrêt confirmé après reconnexion du Worker.",
                             "result": {"cancelled": True, "installer_stop_confirmed": True}} if cancelled else
                            {"ok": False, "error": "Installation interrompue ; processus arrêtés, aucune répétition automatique.",
                             "result": {"installer_stop_confirmed": True}})
                self.journal.update("commands", command_id, installer_exit_confirmed=True,
                                    state="cancelled" if cancelled else "failed", response=response)
                with self.command_lock:
                    self.pending_command_results[command_id] = (response, receipt.get("command", ""))
                with self.maintenance_lock:
                    self.maintenance_recovery.pop(command_id, None)
                    self.maintenance_active = self.maintenance_count > 0 or bool(self.maintenance_recovery)

    def loop(self):
        delay = POLL_SECONDS
        self.main_loop_seen = time.monotonic()
        threading.Thread(target=local_loop, args=(self,), name="worker-local-control", daemon=True).start()
        heartbeat = threading.Thread(target=self._heartbeat_loop, name="worker-heartbeat", daemon=True)
        heartbeat.start()
        threading.Thread(target=self.equipment_relay.loop, name="worker-equipment", daemon=True).start()
        # Après le relais d'équipements : la surveillance lit ses caméras et arrête ses machines GRBL.
        self._start_fire_watch()
        self._start_environment_maintenance()
        try:
            while not self.stop_requested.is_set():
                self.main_loop_seen = time.monotonic()
                try:
                    with self.command_lock:
                        pending = list(self.pending_command_results)[:10]
                    for command_id in pending:
                        self._send_command_result(command_id)
                    self._reconcile_maintenance()
                    self._resume_jobs()
                    commands = self.request("/api/worker-protocol/commands").get("commands", [])
                    for item in commands:
                        self._dispatch_command(item)
                    # Burst mode: right after a command (or while results are being
                    # returned) poll again almost immediately, so chained printer
                    # moves from the dashboard flow in about a second instead of
                    # waiting a full poll period each.
                    if commands or pending:
                        self.command_burst_until = time.monotonic() + 12
                    delay = 0.3 if time.monotonic() < self.command_burst_until else POLL_SECONDS
                except Exception as error:
                    print(f"Commandes Worker momentanément indisponibles ({type(error).__name__}); nouvelle tentative automatique.", flush=True)
                    delay = min(max(POLL_SECONDS, delay * 2), 30)
                self.stop_requested.wait(delay)
        finally:
            self.stop_requested.set()
            self._stop_fire_watch()


def main():
    parser = argparse.ArgumentParser(description="Agent GPU Alpine Makers")
    parser.add_argument("--config", default=str(Path.home() / ".alpine-makers-worker" / "config.json"))
    parser.add_argument("--pair", nargs=2, metavar=("SERVER_URL", "CODE")); parser.add_argument("--name", default="")
    parser.add_argument("--gpu-optional", action="store_true", help="Association Linux sans GPU obligatoire avec le code de ton compte")
    parser.add_argument("--identity-info", action="store_true", help="Afficher uniquement la clé publique et son empreinte pour la migration")
    parser.add_argument("--enroll-identity", metavar="CODE", help="Consommer l'autorisation de migration/rotation du propriétaire")
    args = parser.parse_args()
    if args.identity_info or args.enroll_identity:
        if __package__:
            from .identity_setup import prepare
        else:
            from identity_setup import prepare
        prepare()
        config_path = Path(args.config).resolve()
        identity = Identity(config_path.parent, create=True)
        if args.identity_info:
            print(json.dumps({"public_key": identity.public, "fingerprint": identity.fingerprint}, indent=2))
            return 0
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        payload = {"worker_id": config.get("worker_id"), "enrollment_code": args.enroll_identity,
                   "identity_public_key": identity.public}
        payload["identity_signature"] = identity.sign(payload)
        outgoing = protect_credentials(urllib.request.Request(validate_server_url(config.get("server_url")) + "/api/worker-protocol/identity/enroll",
                                      data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": f"AlpineWorker/{AGENT_VERSION}"}, method="POST"))
        with urllib.request.urlopen(outgoing, timeout=45) as connection:
            response = json.loads(connection.read())
        config.update(token="identity-v1", identity_enabled=True, identity_server_id=response["identity_server_id"], identity_epoch=response["identity_epoch"])
        atomic_json(config_path, config)
        if os.name != "nt":
            os.chmod(config_path, 0o600)
        print("Identité migrée ; ID et associations conservés. Relance le Worker.")
        return 0
    agent = Agent(Path(args.config))
    if args.gpu_optional and not args.pair:
        parser.error("--gpu-optional s’utilise uniquement avec --pair")
    if args.pair:
        try:
            response = agent.pair(args.pair[0], args.pair[1], args.name, gpu_optional=args.gpu_optional)
        except AgentError as error:
            print(f"Association impossible : {error}", file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "worker_id": response.get("worker_id")}, indent=2))
    else:
        if agent.disconnect_still_active():
            # Ancien superviseur ou unité systemd qui relance l'agent malgré la
            # déconnexion : rester hors ligne, sans moteur, jusqu'à reconnect
            # worker.bat (qui supprime le marqueur) ou au prochain démarrage.
            print("Worker déconnecté depuis le dashboard ; relance MENU-WORKER.bat (5 · Reconnecter le Worker) pour le reconnecter.", flush=True)
            while agent.disconnect_still_active():
                time.sleep(5)
        if agent.detached_marker.is_file():
            print("Worker retiré du dashboard ; en attente d’une nouvelle association.", flush=True)
            while agent.detached_marker.is_file():
                time.sleep(5)
        if not agent.config.get("token"): raise SystemExit("Associe d’abord le worker avec --pair URL CODE")
        priority = ensure_normal_process_priority()
        if priority and priority[0] != priority[1]:
            print("Agent lancé sous la priorité normale (tâche planifiée) : remis en priorité normale.", flush=True)
        agent.loop()
        agent.finish_requested_exit()
    return 0


if __name__ == "__main__": raise SystemExit(main())
