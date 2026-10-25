"""Stabilité du PC qui héberge le Worker (agent 1.50.41, capacité host_stability_v1).

Constat du 2026-10-25 sur un PC qui héberge aussi le site, la boutique et le courrier : quatre blocages complets
(arrêts forcés au bouton), sans écran bleu ni erreur du pilote graphique, et un fichier d'échange monté à 19 Go juste
après un démarrage. ComfyUI épingle par défaut jusqu'à 40 % de la RAM sous Windows (« Enabled pinned memory 19600 »,
mémoire non paginable) ; quand plusieurs moteurs chargent en même temps, Windows n'a plus que le disque pour respirer.

Ce module, en lecture seule et sans réseau hors de 127.0.0.1 :

- ``comfyui_stability_arguments`` : ``--disable-pinned-memory`` quand le ComfyUI installé connaît l'option (jamais une
  option inconnue, qui l'empêcherait de démarrer) ; ``comfyui_pinned_memory: true`` dans config.json la remet ;
- ``engine_priority_flag`` : moteurs IA et capture des caméras en priorité « inférieure à la normale » sous Windows :
  le bureau et les services du PC gardent la main quand un moteur charge ou calcule ;
- ``gpu_process_memory_mb`` : VRAM dédiée par processus lue par les compteurs « GPU Process Memory » en noms anglais
  (PdhAddEnglishCounterW) : valable quelle que soit la langue de Windows, sans lancer PowerShell ;
- ``commit_mb`` : mémoire engagée (RAM + fichier d'échange) et sa limite ;
- ``ResourceJournal`` : une ligne toutes les 20 s dans logs/ressources.log (RAM, mémoire engagée, processeur, VRAM, RAM
  de chaque moteur), 2 Mio au plus puis un fichier .1 : si le PC se fige encore, les dernières lignes disent pourquoi ;
- ``MemoryRelief`` : RAM libre sous le seuil et aucun job en cours → les moteurs résidents (ComfyUI, Hunyuan3D)
  rendent leurs modèles, une fois toutes les 5 minutes au plus ; jamais pendant un calcul.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CAPABILITY = "host_stability_v1"
PINNED_FLAG = "--disable-pinned-memory"
CLI_ARGS_MAX_BYTES = 1024 * 1024
JOURNAL_RELATIVE = ("logs", "ressources.log")
JOURNAL_MAX_BYTES = 2 * 1024 * 1024
# RAM libre sous laquelle les moteurs inactifs rendent leurs modèles : 12 % de la RAM, entre 3 et 8 Gio.
RELIEF_FREE_FRACTION = 0.12
RELIEF_MIN_FREE_MB = 3 * 1024
RELIEF_MAX_FREE_MB = 8 * 1024
RELIEF_COOLDOWN_SECONDS = 300
PRESSURE_LOG_SECONDS = 600
MIB = 1024 * 1024


def comfyui_supports(root, flag):
    """Le ComfyUI installé dans ``root`` connaît-il ``flag`` ? Lu dans comfy/cli_args.py, sans suivre de lien."""
    path = Path(root) / "comfy" / "cli_args.py"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > CLI_ARGS_MAX_BYTES:
            return False
        return f'"{flag}"' in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def comfyui_stability_arguments(root, config=None):
    """Options de ComfyUI pour un PC partagé : sans mémoire épinglée, sauf choix contraire du propriétaire."""
    if isinstance(config, dict) and config.get("comfyui_pinned_memory") is True:
        return []
    return [PINNED_FLAG] if comfyui_supports(root, PINNED_FLAG) else []


def engine_priority_flag():
    """Indicateur de création de processus « priorité inférieure à la normale » (Windows), 0 ailleurs."""
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000))


def commit_mb():
    """(limite, utilisée) de la mémoire engagée en Mio, ou (None, None). Windows : GlobalMemoryStatusEx."""
    if os.name != "nt":
        return None, None
    try:
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
        limit = int(status.ullTotalPageFile) // MIB
        return limit, limit - int(status.ullAvailPageFile) // MIB
    except Exception:  # noqa: BLE001 - mesure indisponible : rien n'est inventé
        return None, None


_PID_INSTANCE = re.compile(r"pid_(\d+)_", re.IGNORECASE)


def parse_gpu_instances(rows):
    """[(nom d'instance, octets)] → {pid: Mio} (une instance par carte et par processus, additionnées)."""
    found = {}
    for name, value in rows:
        match = _PID_INSTANCE.match(str(name or ""))
        if not match:
            continue
        pid = int(match.group(1))
        found[pid] = found.get(pid, 0) + max(0, int(value or 0)) // MIB
    return found


def gpu_process_memory_mb():
    """VRAM dédiée par processus {pid: Mio} (Windows), ou None si les compteurs sont illisibles.

    nvidia-smi --query-compute-apps rend [N/A] sous WDDM : seuls ces compteurs donnent la VRAM de chaque processus.
    Noms anglais via PdhAddEnglishCounterW : un Windows en français trouve le même compteur, sans PowerShell."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        pdh = ctypes.WinDLL("pdh")
    except (OSError, AttributeError, ImportError):
        return None

    class Value(ctypes.Structure):          # PDH_FMT_COUNTERVALUE (union alignée sur 8 octets)
        _fields_ = [("CStatus", wintypes.DWORD), ("largeValue", ctypes.c_longlong)]

    class Item(ctypes.Structure):           # PDH_FMT_COUNTERVALUE_ITEM_W
        _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", Value)]

    pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
    pdh.PdhOpenQueryW.restype = wintypes.DWORD
    pdh.PdhAddEnglishCounterW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
    pdh.PdhAddEnglishCounterW.restype = wintypes.DWORD
    pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
    pdh.PdhCollectQueryData.restype = wintypes.DWORD
    pdh.PdhGetFormattedCounterArrayW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    pdh.PdhGetFormattedCounterArrayW.restype = wintypes.DWORD
    pdh.PdhCloseQuery.argtypes = [wintypes.HANDLE]
    pdh.PdhCloseQuery.restype = wintypes.DWORD
    fmt_large, more_data = 0x00000400, 0x800007D2
    query, counter = wintypes.HANDLE(), wintypes.HANDLE()
    if pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) != 0:
        return None
    try:
        if pdh.PdhAddEnglishCounterW(query, "\\GPU Process Memory(*)\\Dedicated Usage", 0, ctypes.byref(counter)) != 0:
            return None
        if pdh.PdhCollectQueryData(query) != 0:
            return None
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        status = pdh.PdhGetFormattedCounterArrayW(counter, fmt_large, ctypes.byref(size), ctypes.byref(count), None)
        if status == 0:
            return {}
        if status != more_data or not size.value:
            return None
        buffer = (ctypes.c_byte * size.value)()
        if pdh.PdhGetFormattedCounterArrayW(counter, fmt_large, ctypes.byref(size), ctypes.byref(count), buffer) != 0:
            return None
        items = ctypes.cast(buffer, ctypes.POINTER(Item))
        rows = [(items[index].szName, items[index].FmtValue.largeValue) for index in range(count.value)
                if items[index].FmtValue.CStatus in (0, 1)]
        return parse_gpu_instances(rows)
    except Exception:  # noqa: BLE001 - compteurs absents ou format inattendu : pas de mesure par processus
        return None
    finally:
        pdh.PdhCloseQuery(query)


def _gib(mb):
    return "?" if mb is None else f"{float(mb) / 1024:.1f}"


def journal_line(system, commit, engines, at=None):
    """Une ligne lisible : « 2026-10-25 22:48:13  RAM 22.3/47.8 Go · engagée 24.3/74.0 Go · CPU 12 % · VRAM … »."""
    system = system if isinstance(system, dict) else {}
    limit, used = commit if isinstance(commit, (tuple, list)) and len(commit) == 2 else (None, None)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() if at is None else at))
    cpu = system.get("cpu_percent")
    parts = [f"RAM {_gib(system.get('ram_used_mb'))}/{_gib(system.get('ram_total_mb'))} Go",
             f"engagée {_gib(used)}/{_gib(limit)} Go",
             "CPU " + ("?" if cpu is None else f"{float(cpu):.0f}") + " %",
             f"VRAM {_gib(system.get('vram_used_mb'))}/{_gib(system.get('vram_total_mb'))} Go"]
    for name, mb in engines or ():
        parts.append(f"{name} {_gib(mb)} Go")
    return stamp + "  " + " · ".join(parts)


class ResourceJournal:
    """logs/ressources.log du Worker : écrit par le fil de mesure, jamais à travers un lien, taille bornée."""

    def __init__(self, root, max_bytes=JOURNAL_MAX_BYTES):
        self.path = Path(root).joinpath(*JOURNAL_RELATIVE)
        self.max_bytes = max(4096, int(max_bytes))
        self.lock = threading.Lock()

    def append(self, line):
        with self.lock:
            try:
                folder = self.path.parent
                if folder.is_symlink() or self.path.is_symlink():
                    return False
                folder.mkdir(parents=True, exist_ok=True)
                if self.path.is_file() and self.path.stat().st_size >= self.max_bytes:
                    previous = self.path.with_name(self.path.name + ".1")
                    if previous.is_symlink():
                        return False
                    os.replace(self.path, previous)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(str(line).replace("\r", " ").replace("\n", " ")[:600] + "\n")
                return True
            except OSError:
                return False


def relief_threshold_mb(total_mb):
    """RAM libre minimale avant de demander aux moteurs inactifs de rendre leurs modèles."""
    if not total_mb:
        return None
    return int(min(RELIEF_MAX_FREE_MB, max(RELIEF_MIN_FREE_MB, float(total_mb) * RELIEF_FREE_FRACTION)))


def _local_json(url, *, data=None, headers=None, timeout=10):
    request = urllib.request.Request(url, data=data, headers=dict(headers or {}), method="POST" if data is not None else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(1024 * 1024) or b"{}")


def unload_ai3d(root, port, timeout=10):
    """Hunyuan3D rend son modèle s'il en garde un et ne calcule pas. True si un modèle a été rendu."""
    try:
        token_path = Path(root) / "config" / "hunyuan-token"
        if token_path.is_symlink() or not token_path.is_file():
            return False
        token = token_path.read_text(encoding="utf-8").strip()
        headers = {"X-Raphal-Local-Token": token}
        base = f"http://127.0.0.1:{int(port)}"
        health = _local_json(base + "/health", headers=headers, timeout=timeout)
        if not isinstance(health, dict) or health.get("state") != "READY" or not health.get("model_loaded"):
            return False
        _local_json(base + "/unload", data=b"{}", headers={**headers, "Content-Type": "application/json"}, timeout=timeout)
        return True
    except (urllib.error.URLError, OSError, ValueError, TypeError):
        return False


class MemoryRelief:
    """Moteurs inactifs déchargés quand la RAM du PC devient rare. Appelé par le fil de mesure (toutes les 20 s)."""

    def __init__(self, agent, *, cooldown=RELIEF_COOLDOWN_SECONDS):
        self.agent = agent
        self.cooldown = cooldown
        self.last_relief = 0.0
        self.last_pressure_log = 0.0

    def _jobs_running(self):
        threads = getattr(self.agent, "job_threads", None) or {}
        return any(thread.is_alive() for thread in list(threads.values()))

    def check(self, system, now=None):
        """Rend la liste des moteurs déchargés (vide si rien à faire)."""
        now = time.monotonic() if now is None else now
        system = system if isinstance(system, dict) else {}
        total, used = system.get("ram_total_mb"), system.get("ram_used_mb")
        threshold = relief_threshold_mb(total)
        if threshold is None or used is None or total - used >= threshold:
            return []
        free = total - used
        amounts = f"{free / 1024:.1f} Gio libres sur {total / 1024:.1f} Gio".replace(".", ",")
        if now - self.last_pressure_log >= PRESSURE_LOG_SECONDS:
            self.last_pressure_log = now
            print(f"Mémoire du PC presque pleine : {amounts}.", flush=True)
        if now - self.last_relief < self.cooldown or self._jobs_running():
            return []
        self.last_relief = now
        released = []
        agent = self.agent
        try:
            running = getattr(agent, "_runtime_running", lambda _tool: False)
            if running("image_generation"):
                if __package__:
                    from .safety import comfyui_queue_busy, release_comfyui_vram
                else:
                    from safety import comfyui_queue_busy, release_comfyui_vram
                if comfyui_queue_busy() is False and release_comfyui_vram():
                    released.append("génération d'images")
            if running("ai3d"):
                port = getattr(agent, "_runtime_port", lambda _tool: None)("ai3d")
                if port and unload_ai3d(getattr(agent, "root", "."), port):
                    released.append("3D")
        except Exception as error:  # noqa: BLE001 - un déchargement raté ne coupe jamais la mesure
            print(f"Déchargement des moteurs inactifs impossible ({type(error).__name__}).", flush=True)
        if released and callable(getattr(agent, "log", None)):
            agent.log("runtime.memory_relieved",
                      f"Mémoire du PC presque pleine ({amounts}) : modèles rendus par "
                      + " et ".join(released) + " (aucun job en cours).", level="warning")
        return released


__all__ = ["CAPABILITY", "MemoryRelief", "PINNED_FLAG", "ResourceJournal", "commit_mb", "comfyui_stability_arguments",
           "comfyui_supports", "engine_priority_flag", "gpu_process_memory_mb", "journal_line", "parse_gpu_instances",
           "relief_threshold_mb", "unload_ai3d"]
