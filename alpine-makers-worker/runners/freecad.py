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
        state.submit_intent(engine="freecad")
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            try:
                target = os.path.join(engine_path(destination.parent), destination.name)
                command = ([freecad, engine_path(engine), engine_path(scene_path), target] if scene is not None else
                           [freecad, engine_path(engine), engine_path(source), target, str(linear), "0.523599", str(facets)])
                process = subprocess.Popen(command,
                    stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except OSError as error:
                state.finish("failed", error=str(error))
                raise
        identity = process_identity(process.pid)
        state.update(state="running", pid=process.pid, process_identity=identity)
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
    if (process is not None and process.returncode) or not details.get("ok") or not destination.is_file():
        raise ValueError(str(details.get("error") or "Conversion FreeCAD échouée ou sortie incomplète."))
    details.pop("ok", None)
    details["artifacts"] = [destination.name]
    state.finish("completed", result=details)
    return details


if __name__ == "__main__":
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    with RunnerLease(Path(sys.argv[2])):
        print(json.dumps(run_job(job, Path(sys.argv[2]))))
