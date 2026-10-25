"""Private process containment for one installer, never a runtime or printer.

The bootstrap waits for its parent before importing an installer. On Windows
this closes the spawn/AssignProcessToJobObject race: no descendant can escape
the job before ownership has been established. Killing by executable name or
by an unverified, reusable PID is deliberately unsupported.

Le même groupe Windows (WindowsJob) et la même amorce (« --run ») servent aussi aux plafonds de RAM et de processeur
des jobs (job_limits.py, capacité resource_limits_v1) : groupe par job imbriqué dans le groupe cumulé du Worker.
"""
import os
import runpy
import signal
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

if __package__:
    from .process_control import process_identity
else:
    from process_control import process_identity


def scope_record():
    return {"platform": os.name, "name": "Local\\AlpineWorkerInstall_" + uuid.uuid4().hex}


_STRUCTURES = {}
JOB_LIMIT_JOB_MEMORY = 0x200        # JOB_OBJECT_LIMIT_JOB_MEMORY : mémoire engagée de tout le groupe
JOB_LIMIT_KILL_ON_CLOSE = 0x2000    # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
CPU_RATE_ENABLE, CPU_RATE_HARD_CAP = 0x1, 0x4   # JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | _HARD_CAP


def _structures():
    """Structures Windows des groupes de processus (Job Objects), créées une fois."""
    if not _STRUCTURES:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_longlong), ("JobTime", ctypes.c_longlong),
                        ("Flags", wintypes.DWORD), ("MinWorking", ctypes.c_size_t),
                        ("MaxWorking", ctypes.c_size_t), ("ActiveLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("Priority", wintypes.DWORD), ("Scheduling", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("Basic", BasicLimits), ("IO", ctypes.c_ulonglong * 6),
                        ("ProcessMemory", ctypes.c_size_t), ("JobMemory", ctypes.c_size_t),
                        ("PeakProcessMemory", ctypes.c_size_t), ("PeakJobMemory", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [("Times", ctypes.c_longlong * 4), ("PageFaults", ctypes.c_uint32),
                        ("Total", ctypes.c_uint32), ("Active", ctypes.c_uint32), ("Terminated", ctypes.c_uint32)]

        class CpuRate(ctypes.Structure):   # JOBOBJECT_CPU_RATE_CONTROL_INFORMATION (union réduite à CpuRate)
            _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]

        _STRUCTURES.update(BasicLimits=BasicLimits, ExtendedLimits=ExtendedLimits, Accounting=Accounting, CpuRate=CpuRate)
    return _STRUCTURES


class WindowsJob:
    """Groupe de processus Windows (Job Object).

    Installateurs (défaut) : groupe NEUF, tué à la fermeture de son dernier handle. ``kill_on_close=False`` : groupe
    des jobs (job_limits.py), qui survit à un redémarrage de l'agent tant qu'un de ses processus vit ; ``reuse=True`` :
    rouvre le groupe de ce nom s'il existe déjà (groupe cumulé du Worker), au lieu de refuser."""

    def __init__(self, name, *, create=False, kill_on_close=True, reuse=False):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.kill_on_close = bool(kill_on_close)
        self.reused = False
        self.cpu_rate_set = False if create else None   # None : état du contrôle du débit inconnu (groupe repris)
        self.kernel = kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.OpenJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = kernel.CreateJobObjectW(None, name) if create else kernel.OpenJobObjectW(0x4 | 0x8, False, name)
        error = ctypes.get_last_error()
        if not self.handle:
            if not create and error == 2:
                return  # The last process/handle of this unique job has gone.
            raise OSError(error, "Impossible de vérifier le groupe privé de l’installation.")
        if create:
            if error == 183 and reuse:
                self.reused = True   # groupe déjà là (processus d'un job lancé avant un redémarrage de l'agent)
                self.cpu_rate_set = None
                return
            value = _structures()["ExtendedLimits"]()
            value.Basic.Flags = JOB_LIMIT_KILL_ON_CLOSE if self.kill_on_close else 0  # no breakaway allowed.
            if error == 183 or not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(value), ctypes.sizeof(value)):
                self.close()
                raise OSError("Impossible d’isoler l’installation dans un nouveau groupe privé.")

    def attach(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError(self.ctypes.get_last_error(), "L’installation ne peut pas être isolée ; elle n’a pas été exécutée.")

    def set_limits(self, memory_bytes=None, cpu_percent=None):
        """Plafonds durs du groupe : mémoire engagée de tout le groupe (``memory_bytes``) et part du processeur
        (``cpu_percent`` de 0,01 à 100, plafond strict par intervalle de planification). ``None`` retire le plafond."""
        structures = _structures()
        value = structures["ExtendedLimits"]()
        flags = JOB_LIMIT_KILL_ON_CLOSE if self.kill_on_close else 0
        if memory_bytes:
            flags |= JOB_LIMIT_JOB_MEMORY
            value.JobMemory = int(memory_bytes)
        value.Basic.Flags = flags
        if not self.kernel.SetInformationJobObject(self.handle, 9, self.ctypes.byref(value), self.ctypes.sizeof(value)):
            raise OSError(self.ctypes.get_last_error(), "Plafond de mémoire du groupe de processus refusé par Windows.")
        # Contrôle du débit : posé seulement s'il est demandé, ou retiré s'il avait été posé (un groupe neuf qui n'en a
        # jamais eu refuse sa désactivation, erreur 87). Un groupe repris après un redémarrage de l'agent : inconnu,
        # retiré par prudence (erreur 87 tolérée : il n'y en avait pas).
        rate = structures["CpuRate"]()
        if cpu_percent:
            rate.ControlFlags = CPU_RATE_ENABLE | CPU_RATE_HARD_CAP
            rate.CpuRate = max(1, min(10000, int(round(float(cpu_percent) * 100))))
        if cpu_percent or self.cpu_rate_set is not False:
            if not self.kernel.SetInformationJobObject(self.handle, 15, self.ctypes.byref(rate), self.ctypes.sizeof(rate)):
                error = self.ctypes.get_last_error()
                if cpu_percent or error != 87:
                    raise OSError(error, "Plafond du processeur du groupe de processus refusé par Windows.")
        self.cpu_rate_set = bool(cpu_percent)

    def accounting(self):
        """Temps processeur cumulé (s) et processus actifs du groupe, enfants imbriqués compris."""
        value = _structures()["Accounting"]()
        if not self.handle or not self.kernel.QueryInformationJobObject(self.handle, 1, self.ctypes.byref(value), self.ctypes.sizeof(value), None):
            raise OSError("Comptes du groupe de processus illisibles.")
        return {"cpu_seconds": (int(value.Times[0]) + int(value.Times[1])) / 1e7, "active": int(value.Active)}

    def peak_memory(self):
        """Pic de mémoire engagée du groupe (octets) depuis sa création."""
        value = _structures()["ExtendedLimits"]()
        if not self.handle or not self.kernel.QueryInformationJobObject(self.handle, 9, self.ctypes.byref(value), self.ctypes.sizeof(value), None):
            raise OSError("Mémoire du groupe de processus illisible.")
        return int(value.PeakJobMemory)

    def process_ids(self, maximum=512):
        """PID des processus du groupe (ceux de ses groupes imbriqués compris)."""
        ctypes = self.ctypes

        class ProcessList(ctypes.Structure):
            _fields_ = [("Assigned", ctypes.c_uint32), ("Listed", ctypes.c_uint32), ("Ids", ctypes.c_size_t * maximum)]
        value = ProcessList()
        if not self.handle or not self.kernel.QueryInformationJobObject(self.handle, 3, ctypes.byref(value), ctypes.sizeof(value), None):
            error = ctypes.get_last_error()
            if error != 234:   # ERROR_MORE_DATA : liste tronquée, déjà remplie jusqu'à ``maximum``
                raise OSError(error, "Processus du groupe illisibles.")
        return [int(value.Ids[index]) for index in range(min(int(value.Listed), maximum))]

    def stopped(self):
        if not self.handle:
            return True
        value = _structures()["Accounting"]()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, self.ctypes.byref(value), self.ctypes.sizeof(value), None):
            raise OSError("L’arrêt des sous-processus de l’installation n’est pas confirmé.")
        return value.Active == 0

    def terminate(self):
        if self.handle and not self.kernel.TerminateJobObject(self.handle, 130):
            raise OSError("Impossible d’arrêter le groupe privé de l’installation.")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _session_members(scope):
    """Linux-only session membership, with process creation identities."""
    pid, identity = scope.get("pid"), scope.get("identity")
    if not pid or not identity or not Path("/proc").is_dir():
        raise OSError("Identité de la session d’installation inconnue.")
    current = process_identity(pid)
    if current is not None and current != identity:
        raise OSError("L’identité de la session d’installation a changé.")
    members = []
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(fields[3]) == int(pid) and fields[0] != "Z":
            members.append((int(directory.name), fields[19]))
    if current is None and members:
        # Without the leader creation identity, a reused numeric session ID
        # cannot safely be attributed to this command after a long outage.
        raise OSError("Session sans lanceur identifié ; arrêt des descendants non confirmé.")
    return members


class InstallerScope:
    def __init__(self, record, *, create=False):
        self.record = dict(record)
        if self.record.get("platform") != os.name:
            raise OSError("Le groupe d’installation appartient à un autre système.")
        self.job = WindowsJob(self.record["name"], create=create) if os.name == "nt" else None

    def attach(self, process):
        if self.job:
            self.job.attach(process)
        identity = process_identity(process.pid)
        if not identity:
            raise OSError("Le lanceur de l’installation s’est arrêté avant son identification.")
        self.record.update(pid=process.pid, identity=identity)
        return dict(self.record)

    def stopped(self):
        return self.job.stopped() if self.job else not _session_members(self.record)

    def terminate(self):
        if self.job:
            self.job.terminate()
            return
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise OSError("Ce système ne permet pas l’arrêt ciblé sûr de cette installation.")
        # SIGSTOP prevents observed descendants from forking while the next
        # scan finds any children born just before suspension. No PID-only kill.
        held = {}
        try:
            for _ in range(8):
                members = _session_members(self.record)
                added = False
                for pid, identity in members:
                    if (pid, identity) in held:
                        continue
                    try:
                        descriptor = os.pidfd_open(pid)
                    except ProcessLookupError:
                        continue
                    if process_identity(pid) != identity:
                        os.close(descriptor)
                        continue
                    held[(pid, identity)] = descriptor
                    signal.pidfd_send_signal(descriptor, signal.SIGSTOP)
                    added = True
                if not added:
                    break
            for descriptor in held.values():
                try:
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            # If inspection failed, do not strand a process suspended forever.
            for descriptor in held.values():
                try:
                    signal.pidfd_send_signal(descriptor, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                os.close(descriptor)

    def wait_stopped(self, timeout=8):
        deadline = time.monotonic() + timeout
        while not self.stopped():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def close(self):
        if self.job:
            self.job.close()


@contextmanager
def installation_commit(path=None, *, blocking=True):
    """A short file lock prevents cancellation between the two swap renames."""
    path = path or os.environ.get("ALPINE_INSTALL_COMMIT_LOCK")
    if not path:
        yield True
        return
    handle = Path(path).open("a+b")
    acquired = False
    try:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"1"); handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                if blocking:
                    raise
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                acquired = True
            except BlockingIOError:
                pass
        yield acquired
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


if __name__ == "__main__":
    # Parent owns arguments, environment and stdin. EOF (parent died before
    # assignment/journal commit) exits without importing or running installer.
    if len(sys.argv) < 3 or sys.argv[1] != "--run" or sys.stdin.readline().strip() != "start":
        raise SystemExit(130)
    script = Path(sys.argv[2]).resolve()
    sys.argv = [str(script), *sys.argv[3:]]
    sys.path.insert(0, str(script.parent))
    runpy.run_path(str(script), run_name="__main__")
