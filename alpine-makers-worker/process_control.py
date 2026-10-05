"""Match a process creation identity before cancelling a job-owned child."""
import os
import signal
from pathlib import Path


def _windows_handle(pid, terminate=False):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    handle = kernel.OpenProcess(0x1000 | 0x100000 | (1 if terminate else 0), False, int(pid))
    if not handle:
        error = ctypes.get_last_error()
        if error == 5:
            raise PermissionError("L’identité du processus ne peut pas être vérifiée.")
        if error == 87:  # A nonexistent PID; not an access/inspection failure.
            return kernel, None, None
        raise OSError(error, "L’état du processus Windows est inconnu.")
    values = [wintypes.FILETIME() for _ in range(4)]
    wait = kernel.WaitForSingleObject(handle, 0)
    if wait == 0:
        kernel.CloseHandle(handle)
        return kernel, None, None
    if wait != 258 or not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
        error = ctypes.get_last_error()
        kernel.CloseHandle(handle)
        raise OSError(error, "Impossible de confirmer l’identité du processus Windows.")
    identity = str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
    return kernel, handle, identity


def process_identity(pid):
    try:
        if os.name == "nt":
            kernel, handle, identity = _windows_handle(pid)
            if handle:
                kernel.CloseHandle(handle)
            return identity
        fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None
    except IndexError as error:
        raise ValueError("Réponse d’état du processus invalide.") from error


def terminate_owned_process(pid, expected_identity):
    if not expected_identity:
        return False
    if os.name == "nt":
        kernel, handle, identity = _windows_handle(pid, terminate=True)
        if not handle:
            return False
        try:
            return identity == expected_identity and bool(kernel.TerminateProcess(handle, 1))
        finally:
            kernel.CloseHandle(handle)
    descriptor = None
    try:
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            return False  # No PID-only kill when identity cannot be held atomically.
        descriptor = os.pidfd_open(int(pid))
        if process_identity(pid) != expected_identity:
            return False
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        return True
    finally:
        if descriptor is not None:
            os.close(descriptor)
