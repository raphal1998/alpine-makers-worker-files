"""Private process containment for one installer, never a runtime or printer.

The bootstrap waits for its parent before importing an installer. On Windows
this closes the spawn/AssignProcessToJobObject race: no descendant can escape
the job before ownership has been established. Killing by executable name or
by an unverified, reusable PID is deliberately unsupported.
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


class WindowsJob:
    def __init__(self, name, *, create=False):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
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
            class BasicLimits(ctypes.Structure):
                _fields_ = [("ProcessTime", ctypes.c_longlong), ("JobTime", ctypes.c_longlong),
                            ("Flags", wintypes.DWORD), ("MinWorking", ctypes.c_size_t),
                            ("MaxWorking", ctypes.c_size_t), ("ActiveLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("Priority", wintypes.DWORD), ("Scheduling", wintypes.DWORD)]
            class ExtendedLimits(ctypes.Structure):
                _fields_ = [("Basic", BasicLimits), ("IO", ctypes.c_ulonglong * 6),
                            ("ProcessMemory", ctypes.c_size_t), ("JobMemory", ctypes.c_size_t),
                            ("PeakProcessMemory", ctypes.c_size_t), ("PeakJobMemory", ctypes.c_size_t)]
            value = ExtendedLimits()
            value.Basic.Flags = 0x2000  # KILL_ON_JOB_CLOSE, no breakaway allowed.
            if error == 183 or not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(value), ctypes.sizeof(value)):
                self.close()
                raise OSError("Impossible d’isoler l’installation dans un nouveau groupe privé.")

    def attach(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError(self.ctypes.get_last_error(), "L’installation ne peut pas être isolée ; elle n’a pas été exécutée.")

    def stopped(self):
        if not self.handle:
            return True
        class Accounting(self.ctypes.Structure):
            _fields_ = [("Times", self.ctypes.c_longlong * 4), ("PageFaults", self.ctypes.c_uint32),
                        ("Total", self.ctypes.c_uint32), ("Active", self.ctypes.c_uint32), ("Terminated", self.ctypes.c_uint32)]
        value = Accounting()
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
