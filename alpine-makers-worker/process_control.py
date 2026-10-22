"""Match a process creation identity before cancelling a job-owned child."""
import os
import signal
import time
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


def terminate_owned_process(pid, expected_identity, force=False):
    """Arrête ``pid`` seulement s'il a toujours l'identité (date de création) attendue. ``force`` : SIGKILL hors Windows."""
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
        signal.pidfd_send_signal(descriptor, signal.SIGKILL if force else signal.SIGTERM)
        return True
    finally:
        if descriptor is not None:
            os.close(descriptor)


# --- Arbre de processus (agent 1.50.33) -----------------------------------------------------------------------------

def _windows_pairs():
    """(pid, pid du parent) de chaque processus, par un instantané Toolhelp (aucun droit particulier)."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(0x2, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "Liste des processus Windows illisible.")
    pairs = []
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            pairs.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return pairs


def _process_pairs():
    if os.name == "nt":
        return _windows_pairs()
    pairs = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            pairs.append((int(entry.name), int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])))
        except (OSError, IndexError, ValueError):
            continue
    return pairs


def _identity_number(value):
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def descendants(pid, expected_identity, pairs=None):
    """Descendants vérifiés de ``pid`` : [(pid, identité)], les plus profonds d'abord.

    Un enfant n'est retenu que s'il est né APRÈS son parent (Windows réutilise les PID). Si ``pid`` désigne
    maintenant un AUTRE processus (même numéro, autre date), rien n'est retenu : ses enfants ne sont pas les nôtres.
    Un parent mort laisse ses enfants trouvables (le pid du parent reste noté chez eux)."""
    root_number = _identity_number(expected_identity)
    if root_number is None:
        return []
    try:
        current = process_identity(pid)
    except (PermissionError, OSError, ValueError):
        return []
    if current is not None and current != expected_identity:
        return []
    children = {}
    for child, parent in (pairs if pairs is not None else _process_pairs()):
        if child > 4 and child != parent:
            children.setdefault(parent, []).append(child)
    found, queue, seen = [], [(int(pid), root_number)], {int(pid)}
    while queue:
        parent, born = queue.pop(0)
        for child in children.get(parent, []):
            if child in seen or child == os.getpid():
                continue
            seen.add(child)
            try:
                identity = process_identity(child)
            except (PermissionError, OSError, ValueError):
                continue
            number = _identity_number(identity)
            if number is None or number < born:
                continue
            found.append((child, identity))
            queue.append((child, number))
    found.reverse()
    return found


def terminate_process_tree(pid, expected_identity, timeout=5.0):
    """Arrête un processus lancé par l'agent ET tous ses descendants vérifiés (les plus profonds d'abord).

    Rend {"stopped": bool, "killed": int, "survivors": [pid, ...]}. ``stopped`` : plus rien de l'arbre ne vit.
    Jamais un PID dont l'identité ne correspond plus ; jamais le processus courant."""
    report = {"stopped": False, "killed": 0, "survivors": []}
    if not expected_identity or int(pid) == os.getpid():
        return report
    tree = descendants(pid, expected_identity) + [(int(pid), str(expected_identity))]
    for _round in range(2):
        for member, identity in tree:
            try:
                if terminate_owned_process(member, identity, force=True):
                    report["killed"] += 1
            except (PermissionError, OSError, ValueError):
                continue
        deadline = time.monotonic() + max(0.0, timeout)
        alive = tree
        while True:
            alive = [(member, identity) for member, identity in alive if _still_same(member, identity)]
            if not alive or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        # Un enfant né pendant l'arrêt est repris au second tour.
        tree = alive + [item for item in descendants(pid, expected_identity) if item not in alive]
        if not tree:
            break
    report["survivors"] = [member for member, identity in tree if _still_same(member, identity)]
    report["stopped"] = not report["survivors"]
    return report


def _still_same(pid, identity):
    try:
        return process_identity(pid) == identity
    except PermissionError:
        return True        # illisible n'est pas une preuve d'arrêt
    except (OSError, ValueError):
        return False
