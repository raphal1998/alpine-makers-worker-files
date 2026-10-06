# -*- coding: utf-8 -*-
"""Runner Worker du Labo image : module « Outils de cartes du Labo » (agent 1.46.0).

Lancé par l'agent comme les autres runners (``python image_maps.py <job.json> <workdir>``, Python de l'agent) pour un job
du moteur image dont ``parameters.lab_tool`` est l'un des huit calculs du module (propriétés et palette, normales, trait,
gribouillage, tuile, grille de couleurs, niveaux de gris, masque). Ce runner :

1. revalide la demande (``safety.validate_image_maps_parameters`` : type de calcul fermé, réglages bornés) ;
2. vérifie que le module est installé (marqueur écrit par l'installateur après contrôle de Pillow et numpy) et localise
   le Python de l'environnement du moteur image (``<ComfyUI>/.venv``) ;
3. lance ``image_maps_script.py`` en sous-processus (``-I -B`` : ni variables PYTHON*, ni site utilisateur, ni .pyc),
   demande et résultats dans le dossier du job, délai maximal, arrêt sur annulation ;
4. rend ``image-1.png`` (carte) ou ``properties.json`` (propriétés réécrites ici, champ par champ).

Les messages d'échec sont ceux de ce fichier ; la sortie d'erreur du script n'est reprise que tronquée et sans chemin.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER_DIR))
from safety import IMAGE_LAB_MAPS_MODULE, validate_image_maps_parameters  # noqa: E402
from job_state import RunnerLease, RunnerState, TERMINAL, atomic_json  # noqa: E402
from process_control import process_identity, terminate_owned_process  # noqa: E402
from installers.model_catalog import COMFYUI_LAB_MODULES  # noqa: E402

ENGINE = "image-maps"
SCRIPT = Path(__file__).with_name("image_maps_script.py")
REQUEST_NAME = "image_maps_request.json"
RAW_REPORT_NAME = "image_maps_raw.json"
PROPERTIES_ARTIFACT = "properties.json"
IMAGE_ARTIFACT = "image-1.png"
STDERR_NAME = "image_maps.stderr"
DEFAULT_TIMEOUT = 300
STOP_GRACE_SECONDS = 15
RAW_REPORT_MAX_BYTES = 64 * 1024
MISSING_ENGINE = ("Le moteur image (ComfyUI) n’est pas installé sur ce Worker : les outils de cartes du Labo utilisent son "
                  "environnement Python. Installe-le dans Mes Workers → Installations.")
MISSING_MODULE = ("Installe le module « Outils de cartes du Labo » dans Mes Workers → Installations (Labo image) : "
                  "ces calculs se font sur le Worker.")
# Codes du script → message rédigé ici (le texte du script n'est jamais transmis tel quel).
SCRIPT_ERRORS = {
    "IMAGE_TOO_LARGE": "Image trop grande : au plus 16 mégapixels et 8192 px de côté.",
    "IMAGE_INVALID": "Image source illisible sur le Worker.",
    "DEPTH_REQUIRED": "Cette image n’est pas une carte de profondeur : lance d’abord « Profondeur », puis « Normales » sur son résultat.",
    "ALPHA_REQUIRED": "Cette image n’a pas de transparence : lance d’abord « Détourer le sujet », puis « Masque » sur son résultat.",
    "RESULT_TOO_LARGE": "La carte obtenue dépasse 32 Mo.",
    "OUT_OF_MEMORY": "Mémoire insuffisante sur le Worker pour ce calcul.",
    "PILLOW_MISSING": "Pillow manque dans l’environnement du moteur image : répare le moteur ComfyUI, puis réinstalle le module « Outils de cartes du Labo ».",
    "REQUEST_INVALID": "Demande de calcul refusée par le Worker.",
}
_QUOTED_PATH_PATTERN = re.compile(r"([\"'])[^\"'\n]*[\\/][^\"'\n]*\1")
_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|/(?:home|Users|root|tmp|var|mnt|opt)/)[^\s\"'<>|]*"
                           r"(?:\s+[^\s\"'<>|\\/]*[\\/][^\s\"'<>|]*)*")
_HEX_COLOR = re.compile(r"^#[0-9a-f]{6}$")
_DHASH = re.compile(r"^[0-9a-f]{16}$")
_RATIO = re.compile(r"^(?:≈ )?[0-9]{1,5}(?::[0-9]{1,5}|,[0-9]{2}:1)$")


class MapsRunnerError(ValueError):
    """Échec définitif et expliqué du calcul (l'état du job devient « failed »)."""


def sanitized_tail(text, limit=300):
    """Fin de la sortie d'erreur, sans chemin (dossier personnel, nom d'utilisateur), sur une ligne."""
    flat = " ".join(str(text or "").split())
    return _PATH_PATTERN.sub("<chemin>", _QUOTED_PATH_PATTERN.sub("<chemin>", flat))[-limit:]


def engine_root(workdir):
    return Path(os.getenv("COMFYUI_ROOT") or workdir.parent.parent / "components" / "comfyui").resolve()


def engine_python(root):
    """Python de l'environnement du moteur image (Pillow y est) ; jamais celui de l'agent."""
    relative = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    for folder in (".venv", "venv"):
        candidate = root.joinpath(folder, *relative)
        if candidate.is_file():
            return candidate
    raise MapsRunnerError(MISSING_ENGINE)


def module_marker(root):
    """Chemin du marqueur du module dans le moteur image (installers/model_catalog.py, COMFYUI_LAB_MODULES)."""
    subdir, filename = COMFYUI_LAB_MODULES[("image_generation", IMAGE_LAB_MAPS_MODULE)]["marker"]
    models = (root / "models").resolve()
    path = (models / subdir / filename).resolve()
    if models not in path.parents:
        raise MapsRunnerError("Emplacement du module non autorisé.")
    return path


def module_installed(root):
    """Vrai si l'installateur a vérifié Pillow et numpy et posé son marqueur (JSON lisible, module attendu)."""
    path = module_marker(root)
    try:
        if not path.is_file() or path.stat().st_size > 16 * 1024:
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and value.get("module") == IMAGE_LAB_MAPS_MODULE and value.get("verified") is True


def script_environment(workdir):
    """Environnement du script : sans variables PYTHON*, sortie en UTF-8."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("PYTHON") and key.upper() not in {"VIRTUAL_ENV"}}
    environment.update(PYTHONIOENCODING="utf-8")
    return environment


def clean_properties(raw, width, height):
    """Propriétés publiées : valeurs connues et bornées, réécrites ici ; dimensions égales à celles relevées."""
    if not isinstance(raw, dict):
        raise MapsRunnerError("Compte rendu des propriétés invalide.")
    palette = raw.get("palette")
    if not isinstance(palette, list) or len(palette) > 16:
        raise MapsRunnerError("Compte rendu des propriétés invalide.")
    colors = []
    for item in palette:
        percent = item.get("percent") if isinstance(item, dict) else None
        if (not isinstance(item, dict) or set(item) != {"hex", "percent"} or not isinstance(item["hex"], str) or not _HEX_COLOR.match(item["hex"])
                or type(percent) not in (int, float) or not 0 <= percent <= 100):
            raise MapsRunnerError("Compte rendu des propriétés invalide.")
        colors.append({"hex": item["hex"], "percent": round(float(percent), 1)})
    megapixels = raw.get("megapixels")
    values = {"width": raw.get("width"), "height": raw.get("height")}
    if values != {"width": width, "height": height} or type(megapixels) not in (int, float) or not 0 <= megapixels <= 16.8:
        raise MapsRunnerError("Compte rendu des propriétés invalide.")
    ratio, orientation = raw.get("ratio"), raw.get("orientation")
    if not isinstance(ratio, str) or len(ratio) > 24 or not _RATIO.match(ratio) or orientation not in ("paysage", "portrait", "carré"):
        raise MapsRunnerError("Compte rendu des propriétés invalide.")
    if (type(raw.get("has_alpha")) is not bool or type(raw.get("grayscale")) is not bool
            or not isinstance(raw.get("average_color"), str) or not _HEX_COLOR.match(raw["average_color"])
            or not isinstance(raw.get("dhash"), str) or not _DHASH.match(raw["dhash"])):
        raise MapsRunnerError("Compte rendu des propriétés invalide.")
    return {"width": width, "height": height, "megapixels": round(float(megapixels), 2), "ratio": ratio, "orientation": orientation,
            "has_alpha": raw["has_alpha"], "grayscale": raw["grayscale"], "average_color": raw["average_color"],
            "dhash": raw["dhash"], "palette": colors}


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


def _stop(process, pid, identity):
    """Arrête le script (processus de ce runner ou, après reprise, processus d'identité vérifiée) ; True si confirmé."""
    if process is not None:
        process.terminate()
        try:
            process.wait(STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(STOP_GRACE_SECONDS)
        return True
    return bool(identity) and terminate_owned_process(pid, identity)


def _run(job, workdir, state):
    if state.value["state"] == "completed":
        return state.value["result"]
    if state.value["state"] in {"failed", "cancelled"}:
        raise ValueError(state.value.get("error") or "Exécution déjà terminée.")
    try:
        parameters = validate_image_maps_parameters(job.get("parameters"))
    except ValueError as error:
        raise MapsRunnerError(str(error)) from None
    tool = parameters["lab_tool"]
    source = Path(str(parameters["input_files"][0].get("path") or "")).resolve()
    if not source.is_file() or workdir not in source.parents:
        raise MapsRunnerError("L’image source de ce job n’a pas été copiée sur le Worker.")
    output, raw_report = workdir / IMAGE_ARTIFACT, workdir / RAW_REPORT_NAME
    process = None
    if state.value["state"] == "preparing":
        root = engine_root(workdir)
        python = engine_python(root)
        if not module_installed(root):
            raise MapsRunnerError(MISSING_MODULE)
        if not SCRIPT.is_file():
            raise MapsRunnerError("Le script des outils de cartes manque sur ce Worker : mets à jour le Worker puis relance.")
        if state.cancellation_requested():
            state.finish("cancelled", error="Calcul annulé avant lancement.")
            raise ValueError("Calcul du Labo annulé.")
        for stale in (output, raw_report):
            stale.unlink(missing_ok=True)
        atomic_json(workdir / REQUEST_NAME, {"workdir": str(workdir), "input": str(source), "output": str(output),
                                             "report": str(raw_report), "tool": tool, "settings": parameters["maps"]})
        state.submit_intent(engine=ENGINE)
        with (workdir / STDERR_NAME).open("w", encoding="utf-8") as stderr:
            try:
                process = subprocess.Popen([str(python), "-I", "-B", str(SCRIPT), str(workdir / REQUEST_NAME)],
                                           cwd=str(workdir), env=script_environment(workdir), stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=stderr,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except OSError:
                state.finish("failed", error="Lancement du calcul impossible sur ce Worker.")
                raise
        identity = process_identity(process.pid)
        state.update(state="running", pid=process.pid, process_identity=identity, started_at=time.time(), progress=10,
                     message="Calcul du Labo en cours sur le Worker…")
    pid, identity = state.value.get("pid"), state.value.get("process_identity")
    if not pid:
        raise ValueError("Lancement du calcul incertain ; aucun nouveau calcul ne sera lancé.")
    # Après une reprise (agent redémarré), le délai court toujours depuis le lancement réel.
    deadline = float(state.value.get("started_at") or time.time()) + int(parameters.get("timeout_seconds") or DEFAULT_TIMEOUT)
    cancelled = timed_out = False
    while (process.poll() is None) if process is not None else (identity is not None and process_identity(pid) == identity):
        if state.cancellation_requested() and not cancelled:
            state.update(state="cancel_requested")
            cancelled = True
        if cancelled or time.time() > deadline:
            timed_out = not cancelled
            if not _stop(process, pid, identity):
                raise ValueError("Impossible de confirmer l’arrêt du calcul ; aucun autre processus arrêté.")
            break
        time.sleep(0.1)
    if process is None and identity is None:
        raise ValueError("Identité du processus de calcul inconnue ; arrêt non confirmé.")
    if process is not None:
        process.wait()
    state.update(engine_stop_confirmed=True)
    if cancelled or state.cancellation_requested():
        output.unlink(missing_ok=True)
        state.finish("cancelled", error="Arrêt du calcul confirmé.")
        raise ValueError("Calcul du Labo annulé.")
    if timed_out:
        output.unlink(missing_ok=True)
        raise MapsRunnerError("Le calcul a dépassé le délai maximal ; calcul arrêté.")
    raw = {}
    if raw_report.is_file() and raw_report.stat().st_size <= RAW_REPORT_MAX_BYTES:
        try:
            value = json.loads(raw_report.read_text(encoding="utf-8"))
            raw = value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            raw = {}
    if raw.get("ok") is not True:
        output.unlink(missing_ok=True)
        code = str(raw.get("code") or "")
        if code in SCRIPT_ERRORS:
            raise MapsRunnerError(SCRIPT_ERRORS[code])
        try:
            tail = sanitized_tail((workdir / STDERR_NAME).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            tail = ""
        exit_code = process.returncode if process is not None else None
        detail = f" (code {exit_code})" if exit_code is not None else ""
        raise MapsRunnerError(f"Le calcul du Labo a échoué{detail}." + (f" Détail : {tail}" if tail else ""))
    width, height = raw.get("width"), raw.get("height")
    if type(width) is not int or type(height) is not int or not 1 <= width <= 8192 or not 1 <= height <= 8192 or raw.get("tool") != tool:
        output.unlink(missing_ok=True)
        raise MapsRunnerError("Compte rendu du calcul invalide.")
    if tool == "properties":
        output.unlink(missing_ok=True)
        atomic_json(workdir / PROPERTIES_ARTIFACT, clean_properties(raw.get("properties"), width, height))
        artifacts = [PROPERTIES_ARTIFACT]
    else:
        with output.open("rb") as handle:
            if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                raise MapsRunnerError("Le calcul n’a pas rendu d’image PNG.")
        artifacts = [IMAGE_ARTIFACT]
    result = {"artifacts": artifacts}
    state.update(progress=100, message="Calcul du Labo terminé.")
    state.finish("completed", result=result)
    return result


if __name__ == "__main__":
    try:
        job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        with RunnerLease(Path(sys.argv[2])):
            print(json.dumps(run_job(job, Path(sys.argv[2])), ensure_ascii=False))
    except ValueError as error:
        raise SystemExit(str(error)) from None
