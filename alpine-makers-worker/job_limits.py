"""Plafonds durs de RAM et de processeur des jobs d'un Worker Windows (capacité resource_limits_v1).

Deux niveaux de groupes Windows (Job Objects imbriqués, Windows 8 et suivants), créés par installer_process.WindowsJob :

- le groupe CUMULÉ du Worker (nommé d'après le dossier du Worker) : plafond de la mémoire engagée et de la part du
  processeur de TOUS les jobs en cours ;
- un groupe PAR JOB, imbriqué dans le premier : plafonds de ce job seul.

Aucun des deux n'est tué à la fermeture de son handle : un job continue si l'agent redémarre (reprise durable des jobs),
et Windows garde ses plafonds tant qu'un de ses processus vit ; l'agent relancé rouvre le groupe cumulé par son nom.
L'amorce d'installer_process (« --run », départ sur « start » reçu par l'entrée standard) garantit qu'aucun descendant
ne naît avant l'entrée du lanceur dans ses deux groupes.

La VRAM n'a pas de plafond matériel sous Windows : elle relève de l'admission et des réglages des moteurs
(resource_limits.py). Hors Windows, rien ici n'est utilisé (admission seulement).
"""
import hashlib
import os
import time
from pathlib import Path

if __package__:
    from .installer_process import WindowsJob
else:
    from installer_process import WindowsJob

BOOTSTRAP = Path(__file__).with_name("installer_process.py")
MIB = 1024 * 1024


def supported():
    return os.name == "nt"


def parent_name(root):
    """Nom du groupe cumulé : un par dossier de Worker (deux Workers sur un PC n'en partagent jamais)."""
    digest = hashlib.sha256(str(Path(root).resolve()).casefold().encode("utf-8")).hexdigest()[:24]
    return "Local\\AlpineWorkerJobs_" + digest


def job_name(job_id, execution_id):
    return "Local\\AlpineWorkerJob_" + hashlib.sha256(f"{job_id}:{execution_id}".encode("utf-8")).hexdigest()[:24]


def gated_command(command):
    """[python, lanceur, *arguments] → la même commande derrière l'amorce qui attend « start »."""
    return [command[0], str(BOOTSTRAP), "--run", *[str(part) for part in command[1:]]]


def _mebibytes(value):
    return int(value) * MIB if value else None


def private_memory_mb(pids):
    """Mémoire privée (engagée) cumulée de ces processus, en Mio ; None si rien n'est lisible."""
    if os.name != "nt" or not pids:
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):   # PROCESS_MEMORY_COUNTERS_EX
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t), ("PrivateUsage", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    total, read = 0, False
    for pid in pids:
        handle = kernel.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            if kernel.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                total += int(counters.PrivateUsage)
                read = True
        finally:
            kernel.CloseHandle(handle)
    return total // MIB if read else None


class _Measured:
    """Part du processeur d'un groupe entre deux relevés (% de tout le processeur)."""

    def __init__(self):
        self.mark = None

    def cpu_percent(self, cpu_seconds):
        now = time.monotonic()
        previous, self.mark = self.mark, (cpu_seconds, now)
        if previous is None or now <= previous[1]:
            return None
        cores = os.cpu_count() or 1
        return max(0.0, min(100.0, 100.0 * (cpu_seconds - previous[0]) / ((now - previous[1]) * cores)))


class WorkerJobs(_Measured):
    """Groupe cumulé du Worker, ouvert (ou repris après un redémarrage de l'agent) et gardé par l'agent."""

    def __init__(self, root):
        super().__init__()
        self.job = WindowsJob(parent_name(root), create=True, kill_on_close=False, reuse=True)
        self.limits = None

    def apply(self, memory_mb=None, cpu_percent=None):
        wanted = (int(memory_mb) if memory_mb else None, float(cpu_percent) if cpu_percent else None)
        if wanted != self.limits:
            self.job.set_limits(_mebibytes(wanted[0]), wanted[1])
            self.limits = wanted

    def usage(self):
        accounting = self.job.accounting()
        pids = self.job.process_ids()
        return {"active": accounting["active"], "cpu_percent": self.cpu_percent(accounting["cpu_seconds"]),
                "ram_mb": private_memory_mb(pids), "pids": pids}

    def close(self):
        self.job.close()


class JobScope(_Measured):
    """Groupe d'UN job, imbriqué dans le groupe cumulé : plafonds de ce job (RAM engagée, part du processeur)."""

    def __init__(self, parent, name, memory_mb=None, cpu_percent=None):
        super().__init__()
        self.parent = parent
        self.job = WindowsJob(name, create=True, kill_on_close=False)
        try:
            self.job.set_limits(_mebibytes(memory_mb), cpu_percent)
        except OSError:
            self.job.close()
            raise
        self.limits = {"ram_mb": int(memory_mb) if memory_mb else None, "cpu_percent": float(cpu_percent) if cpu_percent else None}

    def attach(self, process):
        # D'abord le groupe cumulé (il rejoint la hiérarchie où se trouve déjà le processus), puis celui du job, qui
        # s'imbrique sous le groupe cumulé : le job reste borné par ses deux plafonds.
        self.parent.job.attach(process)
        self.job.attach(process)

    def usage(self):
        accounting = self.job.accounting()
        pids = self.job.process_ids()
        try:
            peak = self.job.peak_memory() // MIB
        except OSError:
            peak = None
        return {"active": accounting["active"], "cpu_seconds": accounting["cpu_seconds"],
                "cpu_percent": self.cpu_percent(accounting["cpu_seconds"]), "ram_mb": private_memory_mb(pids),
                "peak_ram_mb": peak, "pids": pids}

    def close(self):
        self.job.close()
