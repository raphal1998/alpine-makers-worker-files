"""Groupe Windows de tout le Worker (agent 1.54.3) : rien de ce qu'il lance ne survit à son arrêt.

Constat du 04.11.2026 : après « Éteindre le Worker », un PowerShell lancé par le Worker le 01.11 vivait encore en
session 0, sans parent (orphelin). Il tenait le dossier components\\orca, que le propriétaire ne pouvait plus supprimer.
Le service (nssm) n'arrête que l'arbre de processus qu'il voit, et le menu, sans droits administrateur, ne peut ni lire
ni arrêter un processus du service : il annonçait pourtant « tout fermé ».

Désormais, sous Windows :
- l'agent entre au démarrage dans un groupe de processus nommé (Job Object) « tout fermer à la fermeture ». Tout ce
  qu'il lance y naît, et les orphelins y restent : l'appartenance à un groupe ne dépend pas du parent ;
- le superviseur run_worker.ps1 en garde une copie du handle. Le groupe survit donc aux redémarrages de l'agent
  (mise à jour, code 75), et les moteurs avec lui. Quand le superviseur ET l'agent s'arrêtent (service arrêté,
  « Éteindre le Worker », déconnexion), Windows arrête tout ce qui reste dans le groupe ;
- chaque moteur (ComfyUI, Hunyuan3D, PrintGuard) a en plus son propre groupe, imbriqué. « Arrêter » un module coupe
  aussi les descendants que taskkill /T ne voit plus (parent déjà mort).

Linux : systemd arrête déjà tout le groupe de contrôle du service (KillMode=control-group) ; rien à faire ici.
Ce module ne tue jamais un processus d'après son nom : seulement les membres de groupes créés par ce Worker.
"""
import hashlib
import os
import time
from pathlib import Path

if __package__:
    from .installer_process import JOB_LIMIT_KILL_ON_CLOSE, WindowsJob, _structures
else:
    from installer_process import JOB_LIMIT_KILL_ON_CLOSE, WindowsJob, _structures

# Posée par run_worker.ps1 : l'agent a un superviseur qui le relance et peut garder le groupe ouvert.
SUPERVISOR_ENV = "ALPINE_WORKER_SUPERVISOR_EXIT_CODES"
SUPERVISOR_IMAGES = frozenset({"powershell.exe", "pwsh.exe"})
ERROR_ALREADY_EXISTS = 183
PROCESS_DUP_HANDLE = 0x0040
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DUPLICATE_SAME_ACCESS = 0x2
EXTENDED_LIMITS = 9

_STATE = {"status": "inactive", "detail": "", "name": "", "created": False, "supervisor_handle": 0}
_HANDLE = {"job": None}
_ENGINES = {}


def _digest(*parts):
    text = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()[:20]


def scope_name(root):
    """Nom du groupe de tout le Worker : un par dossier Worker (deux Workers sur un PC ne se mélangent pas)."""
    return "Local\\AlpineWorkerAll_" + _digest(Path(root).resolve())


def engine_scope_name(root, tool_id):
    return "Local\\AlpineWorkerEngine_" + _digest(Path(root).resolve(), tool_id)


def status():
    """État pour le journal et le diagnostic : active | no_supervisor | unavailable | unsupported | inactive."""
    return {key: _STATE[key] for key in ("status", "detail", "name", "created")}


def _set(state, detail=""):
    _STATE.update(status=state, detail=str(detail)[:300])
    return status()


def _api():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE),
                                       wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel, ctypes, wintypes


def _ensure_kill_on_close(kernel, ctypes, job):
    limits = _structures()["ExtendedLimits"]()
    if not kernel.QueryInformationJobObject(job, EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits), None):
        raise OSError(ctypes.get_last_error(), "réglages du groupe illisibles")
    if limits.Basic.Flags & JOB_LIMIT_KILL_ON_CLOSE:
        return
    limits.Basic.Flags |= JOB_LIMIT_KILL_ON_CLOSE
    if not kernel.SetInformationJobObject(job, EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise OSError(ctypes.get_last_error(), "fermeture automatique du groupe refusée")


def _hand_to_supervisor(kernel, ctypes, wintypes, job, parent_pid, images):
    """Copie du handle du groupe dans le superviseur (parent) : le groupe vit aussi longtemps que lui."""
    parent = kernel.OpenProcess(PROCESS_DUP_HANDLE | PROCESS_QUERY_LIMITED_INFORMATION, False, int(parent_pid))
    if not parent:
        raise OSError(ctypes.get_last_error(), "superviseur inaccessible")
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buffer))
        if not kernel.QueryFullProcessImageNameW(parent, 0, buffer, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "programme du superviseur illisible")
        image = Path(buffer.value).name.casefold()
        if image not in images:
            # Un handle confié à un autre programme garderait le groupe ouvert sans fin : rien n'est créé.
            raise OSError(0, f"le parent de l'agent ({image}) n'est pas le superviseur run_worker.ps1")
        target = wintypes.HANDLE()
        if not kernel.DuplicateHandle(kernel.GetCurrentProcess(), job, parent, ctypes.byref(target), 0, False,
                                      DUPLICATE_SAME_ACCESS):
            raise OSError(ctypes.get_last_error(), "copie du groupe vers le superviseur refusée")
        _STATE["supervisor_handle"] = int(target.value or 0)
    finally:
        kernel.CloseHandle(parent)


def adopt(root, *, environ=None, parent_pid=None, supervisor_images=SUPERVISOR_IMAGES):
    """Fait entrer l'agent dans le groupe de tout le Worker (créé au premier démarrage du superviseur).

    À appeler au démarrage, avant tout lancement de processus. N'échoue jamais : un refus de Windows laisse l'agent
    tourner comme avant (status() dit pourquoi)."""
    if os.name != "nt":
        return _set("unsupported", "Windows seulement : systemd arrête déjà tout le service.")
    if _HANDLE["job"]:
        return status()
    environ = os.environ if environ is None else environ
    if not environ.get(SUPERVISOR_ENV):
        return _set("no_supervisor", "agent lancé hors de run_worker.ps1 : groupe non créé.")
    try:
        kernel, ctypes, wintypes = _api()
    except (OSError, AttributeError) as error:
        return _set("unavailable", f"API Windows indisponible ({type(error).__name__}).")
    name = scope_name(root)
    job = kernel.CreateJobObjectW(None, name)
    error = ctypes.get_last_error()
    if not job:
        return _set("unavailable", f"groupe refusé par Windows (erreur {error}).")
    created = error != ERROR_ALREADY_EXISTS
    try:
        _ensure_kill_on_close(kernel, ctypes, job)
        if created:
            _hand_to_supervisor(kernel, ctypes, wintypes, job, parent_pid or os.getppid(), supervisor_images)
        inside = wintypes.BOOL(False)
        if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), job, ctypes.byref(inside)):
            raise OSError(ctypes.get_last_error(), "appartenance au groupe illisible")
        if not inside.value and not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
            raise OSError(ctypes.get_last_error(), "agent non rattaché au groupe du Worker")
    except OSError as failure:
        kernel.CloseHandle(job)
        code = failure.args[0] if failure.args and isinstance(failure.args[0], int) else 0
        reason = failure.args[1] if len(failure.args) > 1 else str(failure)
        return _set("unavailable", f"{reason} (erreur {code})." if code else f"{reason}.")
    _HANDLE["job"] = job
    _STATE.update(name=name, created=created)
    return _set("active", "groupe créé par ce superviseur" if created else "groupe repris après un redémarrage de l'agent")


def attach_engine(root, tool_id, process):
    """Groupe propre à un moteur, imbriqué dans celui du Worker. Renvoie True s'il est en place."""
    if os.name != "nt":
        return False
    try:
        job = WindowsJob(engine_scope_name(root, tool_id), create=True, kill_on_close=False, reuse=True)
    except OSError:
        return False
    try:
        job.attach(process)
    except (OSError, TypeError, ValueError, AttributeError):
        # Le groupe est un filet de sécurité : son refus n'empêche jamais un moteur de démarrer.
        job.close()
        return False
    previous = _ENGINES.pop(str(tool_id), None)
    if previous is not None:
        previous.close()
    _ENGINES[str(tool_id)] = job
    return True


def stop_engine(root, tool_id, timeout=8.0):
    """Arrête ce qui reste dans le groupe de ce moteur (descendants orphelins compris).

    Renvoie le nombre de processus encore présents avant l'arrêt (0 : groupe vide ou disparu). OSError si Windows
    refuse ou si des processus restent après ``timeout`` secondes."""
    if os.name != "nt":
        return 0
    job = _ENGINES.pop(str(tool_id), None) or WindowsJob(engine_scope_name(root, tool_id))
    try:
        if not job.handle:
            return 0
        active = int(job.accounting()["active"])
        if active:
            job.terminate()
            deadline = time.monotonic() + max(0.5, float(timeout))
            while not job.stopped():
                if time.monotonic() >= deadline:
                    raise OSError(0, "des processus du moteur restent actifs après l'arrêt de son groupe")
                time.sleep(0.1)
        return active
    finally:
        job.close()
