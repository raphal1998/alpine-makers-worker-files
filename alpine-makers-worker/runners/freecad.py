"""Durable conversion adapter. Only the exact job-owned FreeCAD process is stopped."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from safety import safe_relative_name, validate_freecad_scene
from job_state import RunnerLease, RunnerState, TERMINAL
from process_control import process_identity, terminate_owned_process


def engine_path(path):
    # FreeCADCmd (1.1) aborts with "Application unexpectedly terminated" when a
    # script or file argument contains a non-ASCII character (an accented user
    # folder, for instance). The Windows 8.3 short name is pure ASCII.
    text = str(path)
    if os.name != "nt" or text.isascii():
        return text
    import ctypes
    buffer = ctypes.create_unicode_buffer(len(text) + 260)
    if ctypes.windll.kernel32.GetShortPathNameW(text, buffer, len(buffer)) and buffer.value.isascii():
        return buffer.value
    return text


def run_job(job, workdir):
    workdir = Path(workdir).resolve()
    state = RunnerState(workdir, job)
    try:
        return _run(job, workdir, state)
    except Exception as error:
        if state.value["state"] not in TERMINAL:
            if state.value.get("engine_stop_confirmed"):
                state.finish("failed", error=str(error))
            else:
                state.uncertain(error)
        raise


def _run(job, workdir, state):
    if state.value["state"] == "completed":
        return state.value["result"]
    if state.value["state"] in {"failed", "cancelled"}:
        raise ValueError(state.value.get("error") or "Exécution déjà terminée.")
    parameters = job.get("parameters") if isinstance(job.get("parameters"), dict) else {}
    if isinstance(parameters.get("cam"), dict):
        return _run_cam(job, workdir, state, parameters)
    # Creation mode: the job carries a declarative scene instead of a source
    # file. It is re-validated here even though the server already did it —
    # a runner never trusts the payload it is handed.
    scene = validate_freecad_scene(parameters)["scene"] if isinstance(parameters.get("scene"), dict) else None
    source = None
    if scene is None:
        source_info = parameters.get("input_file") if isinstance(parameters.get("input_file"), dict) else {}
        source = Path(str(source_info.get("path") or "")).resolve()
        if not source.is_file() or workdir not in source.parents:
            raise ValueError("Fichier source absent ou non autorisé.")
        if source.stat().st_size > 512 * 1024 * 1024:
            raise ValueError("Fichier source trop volumineux.")
    formats = ({"stl", "step", "stp"} if scene is not None else
               {"fcstd", "step", "stp", "iges", "igs", "brep", "brp", "stl", "obj",
                "ply", "off", "amf", "dxf", "svg", "dae", "iv", "wrl", "vrml"})
    output_format = str(parameters.get("output_format") or "stl").lower().lstrip(".")
    if output_format not in formats:
        raise ValueError("Format FreeCAD non autorisé.")
    default_name = f"piece_{scene['result']}.{output_format}" if scene is not None else f"{source.stem}_converted.{output_format}"
    requested_name = safe_relative_name(str(parameters.get("output_name") or default_name), basename=True)
    if not requested_name.lower().endswith(f".{output_format}"):
        requested_name += f".{output_format}"
    destination = workdir / requested_name
    if destination.resolve().parent != workdir:
        raise ValueError("Destination FreeCAD non autorisée.")
    stdout_path, stderr_path = workdir / "freecad.stdout", workdir / "freecad.stderr"
    process = None
    if state.value["state"] == "preparing":
        freecad = os.getenv("FREECAD_CMD")
        engine = Path(__file__).resolve().parents[1] / ("freecad_builder.py" if scene is not None else "freecad_converter.py")
        if not freecad or not Path(freecad).is_file():
            raise ValueError("FreeCADCmd est introuvable.")
        if not engine.is_file():
            raise ValueError("Le moteur FreeCAD du Worker est incomplet.")
        linear = max(0.001, min(10.0, float(parameters.get("linear_deflection") or 0.10)))
        facets = max(25000, min(1000000, int(parameters.get("max_cad_facets") or 250000)))
        if state.cancellation_requested():
            state.finish("cancelled", error="Conversion annulée avant lancement.")
            raise ValueError("Conversion annulée.")
        scene_path = workdir / "scene.json"
        if scene is not None:
            # The scene travels as a file: no geometry ever reaches FreeCADCmd
            # through a command line that an accented path could break.
            scene_path.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
        target = os.path.join(engine_path(destination.parent), destination.name)
        command = ([freecad, engine_path(engine), engine_path(scene_path), target] if scene is not None else
                   [freecad, engine_path(engine), engine_path(source), target, str(linear), "0.523599", str(facets)])
        process = _launch(state, command, stdout_path, stderr_path)
    details = _supervise(state, process, stdout_path)
    if not details.get("ok") or not destination.is_file():
        # FreeCADCmd peut s'arrêter nativement (entité DXF/IGES mal formée, par exemple) avant
        # d'écrire sa ligne RAPHAL_FREECAD_RESULT= : aucune exception Python à rapporter, mais sa
        # sortie d'erreur garde souvent la trace utile (incident 13/10, conversion muette).
        raise ValueError(engine_failure(details, stdout_path, stderr_path, "Conversion FreeCAD échouée ou sortie incomplète."))
    _clean(details)
    details["artifacts"] = [destination.name]
    state.finish("completed", result=details)
    return details


def _supervise(state, process, stdout_path):
    """Attend le seul processus FreeCAD de ce job (annulation ciblée, délai 3600 s) ; rend sa ligne de résultat."""
    pid, identity = state.value.get("pid"), state.value.get("process_identity")
    if not pid:
        raise ValueError("Lancement FreeCAD incertain ; aucune nouvelle conversion ne sera lancée.")
    deadline = time.time() + 3600
    while process.poll() is None if process is not None else process_identity(pid) == identity and identity is not None:
        if state.cancellation_requested():
            state.update(state="cancel_requested")
            if process is not None:
                process.terminate()
            elif not terminate_owned_process(pid, identity):
                raise ValueError("Impossible de confirmer l’identité du processus FreeCAD ; aucun autre processus arrêté.")
        if time.time() > deadline:
            raise ValueError("Délai de conversion dépassé ; état du processus à réconcilier.")
        time.sleep(0.3)
    if process is not None:
        process.wait()
    elif identity is None:
        raise ValueError("Identité du processus FreeCAD inconnue ; arrêt non confirmé.")
    state.update(engine_stop_confirmed=True)
    if state.cancellation_requested():
        state.finish("cancelled", error="Arrêt du processus FreeCAD confirmé.")
        raise ValueError("Conversion annulée.")
    lines = stdout_path.read_text(encoding="utf-8", errors="replace").splitlines() if stdout_path.is_file() else []
    reported = next((line.split("=", 1)[1] for line in reversed(lines) if line.startswith("RAPHAL_FREECAD_RESULT=")), "")
    details = json.loads(reported) if reported else {}
    if not isinstance(details, dict):
        details = {}
    returncode = getattr(process, "returncode", None) if process is not None else None
    if process is not None and returncode:
        details["ok"] = False
    # Code de sortie et présence de la ligne de résultat : de quoi expliquer un arrêt muet (voir engine_failure).
    details["_returncode"] = returncode if isinstance(returncode, int) else None
    details["_reported"] = bool(reported)
    return details


# Codes de sortie Windows (NTSTATUS) d'un FreeCADCmd qui n'a pas pu démarrer ou s'est arrêté brutalement.
_START_FAILURES = {0xC0000135: "bibliothèque Windows (DLL) introuvable", 0xC0000139: "point d'entrée de DLL introuvable",
                   0xC000007B: "bibliothèque Windows incompatible (32/64 bits)", 0xC0000142: "initialisation d'une DLL échouée"}
_CRASHES = {0xC0000005: "violation d'accès mémoire", 0xC0000409: "dépassement de pile détecté",
            0xC0000374: "corruption du tas mémoire", 0x80000003: "point d'arrêt (abandon interne)",
            0xC000001D: "instruction non prise en charge par ce processeur", 0xC00000FD: "pile saturée",
            0xC0000017: "mémoire insuffisante", 0xC000013A: "interrompu (Ctrl+C ou fermeture de session)"}


def _redacted_tail(text, limit=500):
    """Fin d'une sortie, sans le nom du compte Windows (le job d'un Worker partagé est vu par un autre compte)."""
    import re
    text = "\n".join(line for line in str(text or "").splitlines() if not line.startswith("RAPHAL_FREECAD_RESULT="))
    text = re.sub(r"(?i)([a-z]:[\\/]+users[\\/]+)[^\\/\r\n]+", r"\1…", text)
    text = re.sub(r"(?i)(/home/)[^/\s]+", r"\1…", text)
    text = " ".join(text.split())
    return text[-limit:]


def engine_failure(details, stdout_path, stderr_path, generic):
    """Message d'échec d'un FreeCADCmd muet : cause classée [CODE], code de sortie et fin de sa sortie d'erreur.

    Avant (incident DESKTOP-2OFS575, 22-23/10) : « Calcul FreeCAD CAM échoué. » sans rien d'autre, alors que la
    sortie d'erreur et le code de sortie de FreeCADCmd disaient pourquoi. Le code entre crochets est lu par le site
    (tool_availability.py) pour proposer la marche à suivre.
    """
    message = str(details.get("error") or "").strip()
    if message:
        return message
    returncode = details.get("_returncode")
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
    tail = _redacted_tail(stderr) or _redacted_tail(stdout)
    lowered = (stderr + "\n" + stdout).lower()
    code = returncode & 0xFFFFFFFF if isinstance(returncode, int) else None
    shown = f"0x{code:08X}" if code is not None and code > 0xFFFF else str(returncode)
    if "no module named 'path" in lowered or "no module named 'cam" in lowered:
        text = "[FREECAD_CAM_MISSING] L'atelier CAM (module Path) de FreeCAD est absent ou cassé sur ce Worker."
    elif "unexpectedly terminated" in lowered:
        text = ("[FREECAD_PATH_REFUSED] FreeCADCmd s'est arrêté (« Application unexpectedly terminated ») : un chemin "
                "du Worker contient sans doute un caractère accentué que FreeCAD refuse.")
    elif code in _START_FAILURES:
        text = f"[FREECAD_START_FAILED] FreeCADCmd n'a pas pu démarrer : {_START_FAILURES[code]} (code {shown})."
    elif code in _CRASHES:
        text = f"[FREECAD_CRASHED] FreeCADCmd s'est arrêté brutalement : {_CRASHES[code]} (code {shown})."
    elif code == 0 and not details.get("_reported"):
        text = ("[FREECAD_NO_RESULT] FreeCADCmd s'est terminé sans exécuter le moteur du Worker (aucune ligne de "
                "résultat, code 0).")
    elif code is not None and code != 0:
        text = f"[FREECAD_EXIT] FreeCADCmd s'est arrêté avec le code {shown} sans message."
    else:
        text = f"[FREECAD_NO_RESULT] {generic}"
    if details.get("_reported") and code:
        text += " Le moteur avait pourtant écrit son résultat avant cet arrêt."
    return (text + (f" Sortie : {tail}" if tail else ""))[:900]


def _clean(details):
    for key in ("ok", "_returncode", "_reported"):
        details.pop(key, None)
    return details


def _launch(state, command, stdout_path, stderr_path):
    state.submit_intent(engine="freecad")
    # Sans PYTHONIOENCODING, FreeCADCmd encode son stdout selon la page de code de la console
    # Windows (pas UTF-8) ; lu ensuite en UTF-8 ici, tout accent devenait "�" (incident 14/10).
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        try:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr, env=environment,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError as error:
            state.finish("failed", error=str(error))
            raise
    state.update(state="running", pid=process.pid, process_identity=process_identity(process.pid))
    return process


CAM_ARTIFACT_SUFFIXES = (".nc", ".ngc", ".tap", ".gcode", ".fcstd", ".cam.json", ".setup.html")


def _run_cam(job, workdir, state, parameters):
    """Préparer pour une CNC : FreeCAD CAM sur un projet DÉCLARATIF revalidé ici, jamais un script."""
    try:
        from cam_payload import validate_cam_job_parameters
    except ImportError:
        raise ValueError("Le moteur CNC manque sur ce Worker : mets le Worker à jour.") from None
    clean = validate_cam_job_parameters(parameters)
    cam = clean["cam"]
    request = {"op": cam["op"]}
    if cam["op"] == "compute":
        project = cam["project"]
        source_info = parameters.get("input_file") if isinstance(parameters.get("input_file"), dict) else {}
        source = Path(str(source_info.get("path") or "")).resolve()
        if not source.is_file() or source.parent != workdir:
            raise ValueError("Fichier source absent ou non autorisé.")
        if source.stat().st_size > 512 * 1024 * 1024:
            raise ValueError("Fichier source trop volumineux.")
        expected = project["source"]["file"]
        if source.name != expected or str(source_info.get("name") or source.name) != expected:
            raise ValueError("Le fichier joint ne correspond pas au fichier source du projet CNC.")
        request.update(project=project, source=expected)
    stdout_path, stderr_path = workdir / "freecad.stdout", workdir / "freecad.stderr"
    process = None
    if state.value["state"] == "preparing":
        freecad = os.getenv("FREECAD_CMD")
        engine = Path(__file__).resolve().parents[1] / "freecad_cam.py"
        if not freecad or not Path(freecad).is_file():
            raise ValueError("FreeCADCmd est introuvable.")
        if not engine.is_file():
            raise ValueError("Le moteur CNC manque sur ce Worker : mets le Worker à jour.")
        if state.cancellation_requested():
            state.finish("cancelled", error="Calcul CNC annulé avant lancement.")
            raise ValueError("Calcul CNC annulé.")
        request_path = workdir / "cam_request.json"
        request_path.write_text(json.dumps(request, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        command = [freecad, engine_path(engine), engine_path(request_path), engine_path(workdir)]
        process = _launch(state, command, stdout_path, stderr_path)
    details = _supervise(state, process, stdout_path)
    if not details.get("ok"):
        raise ValueError(engine_failure(details, stdout_path, stderr_path, "Calcul FreeCAD CAM échoué."))
    _clean(details)
    wanted = ["cam_capabilities.json"] if cam["op"] == "capabilities" else details.get("artifacts")
    if not isinstance(wanted, list) or not wanted:
        raise ValueError("Le moteur CNC n'a annoncé aucun fichier.")
    artifacts = []
    for name in wanted:
        name = safe_relative_name(str(name), basename=True)
        allowed = name in ("cam_result.json", "cam_capabilities.json") or name.lower().endswith(CAM_ARTIFACT_SUFFIXES)
        path = workdir / name
        if not allowed or path.resolve().parent != workdir or not path.is_file() or path.stat().st_size == 0:
            raise ValueError("Sortie du moteur CNC incomplète ou non autorisée.")
        artifacts.append(name)
    if cam["op"] == "compute" and ("cam_result.json" not in artifacts or details.get("gcode_file") not in artifacts):
        raise ValueError("Sortie du moteur CNC incomplète (G-code ou résultat absent).")
    details["artifacts"] = artifacts
    state.finish("completed", result=details)
    return details


if __name__ == "__main__":
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    with RunnerLease(Path(sys.argv[2])):
        print(json.dumps(run_job(job, Path(sys.argv[2]))))
