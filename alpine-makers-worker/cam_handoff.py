# -*- coding: utf-8 -*-
"""Document de transfert « alpine-cam-handoff » v1 : de « Préparer pour une CNC » vers Alpine CNC Studio.

Contrat partagé site / CNC Studio (docs/cam_projet_v2.md § 12). Bibliothèque standard seulement ; le module
est importé par le site (route de transfert, T5) et livrable au Worker avec ``cam_payload`` / ``cam_migrate``.

- ``trajectory_fingerprint(project)`` : SHA-256 du projet normalisé sans ses chemins « neutres » (noms, notes,
  provenance, assistant, résultats). Tout changement qui peut modifier les trajectoires change l'empreinte ;
- ``effective_state(project)`` : état de calcul réel (« stale » dès que l'empreinte enregistrée diffère) ;
- ``validate_handoff(document)`` : refus de toute clé inconnue, d'un fichier sans ``sha256``, d'un projet non v2,
  d'un résultat périmé (empreinte ≠ projet) ou non calculé : jamais de G-code périmé transféré ;
- ``make_handoff(...)`` : assemble puis valide le document à partir du projet, des fichiers livrés et du résultat ;
- ``manual_steps(project, …)`` : pauses, changements d'outil (outil précédent / suivant) et consignes, dans l'ordre.
"""
import copy
import hashlib
import json
import re

try:
    from worker_agent import cam_payload  # dépôt (site, tests)
except ImportError:  # pragma: no cover - paquet Worker à plat
    import cam_payload

HANDOFF_FORMAT = "alpine-cam-handoff"
HANDOFF_VERSION = 1
MAX_HANDOFF_BYTES = 256 * 1024
MAX_FILES = 64
MAX_STEPS = 200
MAX_ISSUES = 200
MAX_NOT_SIMULATED = 40
MANUAL_KINDS = ("pause", "tool_change", "note")
SEQUENCE_STATES = ("computed", "skipped", "error")
ISSUE_SEVERITIES = ("info", "warning", "error")
# Chemins qui ne changent jamais une trajectoire : exclus de l'empreinte. « [] » = chaque élément d'une liste.
TRAJECTORY_NEUTRAL_PATHS = frozenset({
    "name", "assistant", "results", "machine.label", "machine.notes", "machine.provenance", "machine.firmware_snapshot",
    "machine.grbl.read_at", "stock.material_label", "tools[].name", "tools[].coating", "tools[].source_ref",
    "operations[].name", "operations[].description", "operations[].phase",
})

_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._\-()]{0,119}$")
_PROJECT_ID = re.compile(r"^cam-[a-f0-9]{12}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TEXT = re.compile(r"^[^\x00-\x1f\x7f]*$")


class CamHandoffError(ValueError):
    """Refus d'un document de transfert ; ``path`` désigne le champ fautif, ``code`` est stable."""

    def __init__(self, path, message, code="CAM_HANDOFF_INVALID"):
        self.path = path
        self.code = code
        super().__init__(f"{path} : {message}" if path else message)


def _fail(path, message, code="CAM_HANDOFF_INVALID"):
    raise CamHandoffError(path, message, code)


def _keys(value, path, required, optional=()):
    if not isinstance(value, dict):
        _fail(path, "objet attendu.")
    unknown = sorted(set(value) - set(required) - set(optional), key=str)
    if unknown:
        _fail(f"{path}.{unknown[0]}" if path else str(unknown[0]), "champ inconnu, refusé.")
    missing = [key for key in required if key not in value]
    if missing:
        _fail(f"{path}.{missing[0]}" if path else missing[0], "champ obligatoire manquant.")
    return value


def _int(value, path, low=0, high=10 ** 9):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not value.is_integer()):
        _fail(path, "nombre entier attendu.")
    if not low <= value <= high:
        _fail(path, f"valeur hors limites (entre {low} et {high}).")
    return int(value)


def _optional_int(value, path, **kwargs):
    return None if value is None else _int(value, path, **kwargs)


def _text(value, path, limit, allow_empty=False):
    if not isinstance(value, str):
        _fail(path, "texte attendu.")
    value = value.strip()
    if not value and not allow_empty:
        _fail(path, "texte vide.")
    if len(value) > limit or not _TEXT.fullmatch(value):
        _fail(path, f"texte invalide (caractères de contrôle ou plus de {limit} caractères).")
    return value


def _optional_text(value, path, limit):
    return None if value is None else _text(value, path, limit, allow_empty=True)


def _bool(value, path):
    if not isinstance(value, bool):
        _fail(path, "vrai ou faux attendu.")
    return value


def _choice(value, path, choices):
    if not isinstance(value, str) or value not in choices:
        _fail(path, f"valeur non admise (attendu : {', '.join(choices)}).")
    return value


def _file_name(value, path, allow_none=False):
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not _FILE_NAME.fullmatch(value) or "/" in value or "\\" in value or ".." in value:
        _fail(path, "nom de fichier seul attendu (jamais un chemin).")
    return value


def _sha256(value, path):
    if not isinstance(value, str) or not re.fullmatch(r"^[a-f0-9]{64}$", value.strip().lower()):
        _fail(path, "empreinte SHA-256 obligatoire (64 caractères hexadécimaux) : sans elle CNC Studio ne peut pas "
                    "vérifier qu'il charge le bon fichier.", "CAM_HANDOFF_SHA256_MISSING")
    return value.strip().lower()


def _iso(value, path, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not re.fullmatch(cam_payload.CAM_BOUNDS["iso_datetime_pattern"], value.strip()):
        _fail(path, "date ISO 8601 attendue.")
    return value.strip()


def _ref(value, path, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not re.fullmatch(cam_payload.CAM_BOUNDS["ref_pattern"], value):
        _fail(path, "référence invalide (lettres, chiffres, « _ » et « - », 64 caractères au plus).")
    return value


# ---------------------------------------------------------------------------------------------
# Empreinte des trajectoires et état de calcul
# ---------------------------------------------------------------------------------------------
def canonical_json(value):
    """JSON canonique (clés triées, séparateurs compacts, ASCII) : même contenu ⇒ même texte."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def file_sha256(data):
    """Empreinte SHA-256 (hexadécimale minuscule) d'un contenu de fichier (``bytes``)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def strip_neutral(project):
    """Copie du projet sans les chemins neutres (``TRAJECTORY_NEUTRAL_PATHS``)."""
    def walk(value, prefix):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                if path in TRAJECTORY_NEUTRAL_PATHS:
                    continue
                result[key] = walk(item, path)
            return result
        if isinstance(value, list):
            return [walk(item, f"{prefix}[]") for item in value]
        return value
    return walk(copy.deepcopy(project), "")


def trajectory_fingerprint(project):
    """SHA-256 du projet normalisé (même version) sans ses chemins neutres. Lève ``CamPayloadError`` s'il est invalide."""
    clean = cam_payload.validate_cam_project(project)
    return hashlib.sha256(canonical_json(strip_neutral(clean)).encode("utf-8")).hexdigest()


def effective_state(project):
    """État de calcul réel : « stale » dès que l'empreinte enregistrée ne correspond plus au projet."""
    results = project.get("results") if isinstance(project, dict) and isinstance(project.get("results"), dict) else None
    if not results:
        return "not_computed"
    state = results.get("state") or "not_computed"
    if state in ("computed", "stale"):
        fingerprint = results.get("fingerprint")
        if not fingerprint:
            return "stale"
        try:
            return "computed" if trajectory_fingerprint(project) == fingerprint else "stale"
        except ValueError:
            return "stale"
    return state if state in cam_payload.RESULT_STATES else "not_computed"


# ---------------------------------------------------------------------------------------------
# Étapes manuelles et séquences dérivées du projet
# ---------------------------------------------------------------------------------------------
def manual_steps(project, file_of_operation=None):
    """Pauses, changements d'outil et consignes dans l'ordre de la séquence (opérations activées seulement).

    ``file_of_operation`` : ``{id d'opération: nom du fichier G-code}`` (politique ``split_files``), sinon ``None``.
    L'outil précédent d'un changement est le dernier outil utilisé avant lui dans la séquence.
    """
    steps, current_tool = [], None
    machine = project.get("machine") if isinstance(project.get("machine"), dict) else {}
    position = machine.get("tool_change_position") if isinstance(machine.get("tool_change_position"), dict) else None
    verified = bool(position and position.get("verified"))
    for operation in project.get("operations") if isinstance(project.get("operations"), list) else []:
        if not isinstance(operation, dict) or not operation.get("enabled", True):
            continue
        kind = operation.get("type")
        file_name = (file_of_operation or {}).get(operation.get("id"))
        if operation.get("pause_before") and kind not in MANUAL_KINDS:
            steps.append({"index": len(steps), "kind": "pause", "operation_id": operation["id"],
                          "message": f"Pause avant « {operation.get('name') or operation['id']} » (M0).",
                          "tool_from": current_tool, "tool_to": current_tool, "stop_spindle": True, "position": None,
                          "file": file_name})
        if kind == "pause":
            steps.append({"index": len(steps), "kind": "pause", "operation_id": operation["id"],
                          "message": operation.get("operator_note") or "Pause (M0) : reprise par Cycle start.",
                          "tool_from": current_tool, "tool_to": current_tool,
                          "stop_spindle": bool(operation.get("stop_spindle", True)), "position": None, "file": file_name})
        elif kind == "tool_change":
            go = bool(operation.get("go_to_change_position")) and verified
            steps.append({"index": len(steps), "kind": "tool_change", "operation_id": operation["id"],
                          "message": operation.get("operator_note") or f"Monter l'outil n°{operation.get('tool')}.",
                          "tool_from": current_tool, "tool_to": operation.get("tool"), "stop_spindle": True,
                          "position": {key: position[key] for key in ("x", "y", "z")} if go else None, "file": file_name})
            current_tool = operation.get("tool")
        elif kind == "note":
            steps.append({"index": len(steps), "kind": "note", "operation_id": operation["id"],
                          "message": operation.get("operator_note") or "", "tool_from": current_tool,
                          "tool_to": current_tool, "stop_spindle": False, "position": None, "file": file_name})
        elif operation.get("tool") is not None:
            current_tool = operation.get("tool")
    return steps


def sequences(project, result=None, file_of_operation=None):
    """Une entrée par opération, dans l'ordre exact du projet : identifiant stable, type, état et références."""
    reports = {}
    if isinstance(result, dict):
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        for report in summary.get("operations") if isinstance(summary.get("operations"), list) else []:
            if isinstance(report, dict) and isinstance(report.get("id"), str):
                reports[report["id"]] = report
        for identifier, report in (result.get("operations") or {}).items() if isinstance(result.get("operations"), dict) else []:
            if isinstance(report, dict):
                reports.setdefault(identifier, report)
    items = []
    for index, operation in enumerate(project.get("operations") if isinstance(project.get("operations"), list) else []):
        if not isinstance(operation, dict):
            continue
        report = reports.get(operation.get("id"), {})
        if not operation.get("enabled", True):
            state = "skipped"
        elif report.get("ok") is False or report.get("state") == "error":
            state = "error"
        else:
            state = "computed"
        items.append({"index": index, "id": operation.get("id"), "type": operation.get("type"),
                      "name": operation.get("name") or operation.get("id"), "enabled": bool(operation.get("enabled", True)),
                      "tool": operation.get("tool"), "state": state,
                      "gcode_file": (file_of_operation or {}).get(operation.get("id")),
                      "estimated_s": report.get("estimated_s") if isinstance(report.get("estimated_s"), (int, float)) else None,
                      "segments": report.get("segments") if isinstance(report.get("segments"), int) else None})
    return items


def tool_summary(project):
    """Outils du projet avec la forme réellement utilisée par le moteur pour les opérations qui les emploient."""
    factor = cam_payload.project_mm_factor(project)
    usage = {}
    for operation in project.get("operations") if isinstance(project.get("operations"), list) else []:
        if isinstance(operation, dict) and operation.get("tool") is not None and operation.get("type") in cam_payload.MACHINING_TYPES_V2:
            usage.setdefault(operation["tool"], []).append(operation["type"])
    items = []
    for tool in project.get("tools") if isinstance(project.get("tools"), list) else []:
        if not isinstance(tool, dict):
            continue
        shapes = {}
        for kind in usage.get(tool.get("number"), []):
            shape = cam_payload.engine_tool_shape(kind, tool.get("family"))
            if shape:
                shapes[kind] = {"reads": shape["reads"], "faithful": shape["faithful"]}
        items.append({"number": tool.get("number"), "name": tool.get("name"), "family": tool.get("family"),
                      "diameter_mm": round(float(tool.get("diameter", 0)) * factor, 6),
                      "flute_length_mm": round(float(tool.get("flute_length", 0)) * factor, 6), "engine_shape": shapes})
    return items


# ---------------------------------------------------------------------------------------------
# Validation du document
# ---------------------------------------------------------------------------------------------
def _issues(value, path, with_line=False):
    items = value if isinstance(value, list) else None
    if items is None:
        _fail(path, "liste attendue.")
    if len(items) > MAX_ISSUES:
        _fail(path, f"liste trop longue ({MAX_ISSUES} au plus).")
    clean = []
    for index, item in enumerate(items):
        where = f"{path}[{index}]"
        _keys(item, where, ("code", "message"), ("line", "severity", "operation") if with_line else ("line",))
        if not isinstance(item["code"], str) or not _CODE.fullmatch(item["code"]):
            _fail(f"{where}.code", "code invalide.")
        entry = {"code": item["code"], "message": _text(item["message"], f"{where}.message", 400, allow_empty=True),
                 "line": _optional_int(item.get("line"), f"{where}.line")}
        if with_line:
            entry["severity"] = _choice(item.get("severity", "warning"), f"{where}.severity", ISSUE_SEVERITIES)
            entry["operation"] = _optional_text(item.get("operation"), f"{where}.operation", 32)
        clean.append(entry)
    return clean


def validate_handoff(document):
    """Valide un document ``alpine-cam-handoff`` v1 et rend une copie normalisée (``CamHandoffError`` sinon)."""
    if not isinstance(document, dict):
        _fail("", "document de transfert invalide : objet attendu.")
    if document.get("format") != HANDOFF_FORMAT:
        _fail("format", f"document « {HANDOFF_FORMAT} » attendu.")
    if isinstance(document.get("version"), bool) or document.get("version") != HANDOFF_VERSION:
        _fail("version", "version de document de transfert non prise en charge.")
    _keys(document, "", ("format", "version", "produced_at", "cam_job_id", "project", "gcode_files", "compute"),
          ("project_id", "project_version", "name", "source", "machine_snapshot", "units", "origin", "tools", "sequences",
           "manual_steps", "warnings_acknowledged", "validation", "manufacturability", "not_simulated", "setup_sheet_name",
           "cam_json_name"))
    project = document["project"]
    if not isinstance(project, dict) or project.get("version") != 2:
        _fail("project.version", "projet v2 attendu : migre le projet (cam_migrate.upgrade) avant le transfert.")
    try:
        clean_project = cam_payload.validate_cam_project(project)
    except cam_payload.CamPayloadError as error:
        _fail(f"project.{error.path}" if error.path else "project", str(error), "CAM_PROJECT_INVALID")
    fingerprint = hashlib.sha256(canonical_json(strip_neutral(clean_project)).encode("utf-8")).hexdigest()
    compute = _keys(document["compute"], "compute", ("state", "fingerprint", "result_version", "computed_at"),
                    ("worker_id", "freecad_version", "post_processor", "agent_version"))
    state = _choice(compute["state"], "compute.state", cam_payload.RESULT_STATES)
    if state != "computed":
        _fail("compute.state", f"résultats « {state} » : seul un calcul terminé (computed) peut être transféré.",
              "CAM_HANDOFF_NOT_COMPUTED")
    if _sha256(compute["fingerprint"], "compute.fingerprint") != fingerprint:
        _fail("compute.fingerprint", "résultats périmés : le projet a changé depuis le calcul (empreinte des trajectoires "
                                     "différente). Recalcule avant de transférer vers CNC Studio.", "CAM_HANDOFF_STALE")
    clean_compute = {"state": "computed", "fingerprint": fingerprint,
                     "result_version": _int(compute["result_version"], "compute.result_version", 0, 1000000),
                     "computed_at": _iso(compute["computed_at"], "compute.computed_at", required=True),
                     "worker_id": _ref(compute.get("worker_id"), "compute.worker_id"),
                     "freecad_version": _optional_text(compute.get("freecad_version"), "compute.freecad_version", 40),
                     "post_processor": None, "agent_version": _optional_text(compute.get("agent_version"), "compute.agent_version", 32)}
    post = compute.get("post_processor")
    if post is not None:
        if not isinstance(post, str) or not re.fullmatch(r"^[a-z][a-z0-9_]{0,47}$", post):
            _fail("compute.post_processor", "nom de post-processeur invalide.")
        clean_compute["post_processor"] = post
    operation_ids = [op["id"] for op in clean_project["operations"]]
    tool_numbers = {tool["number"] for tool in clean_project["tools"]}
    files = document["gcode_files"]
    if not isinstance(files, list) or not files:
        _fail("gcode_files", "au moins un fichier G-code livré est attendu.")
    if len(files) > MAX_FILES:
        _fail("gcode_files", f"trop de fichiers ({MAX_FILES} au plus).")
    clean_files, names = [], set()
    for index, item in enumerate(files):
        where = f"gcode_files[{index}]"
        _keys(item, where, ("name", "sha256"), ("bytes", "lines", "tool_numbers", "operations"))
        name = _file_name(item["name"], f"{where}.name")
        if name in names:
            _fail(f"{where}.name", "fichier en double.")
        names.add(name)
        numbers = item.get("tool_numbers", [])
        if not isinstance(numbers, list) or len(numbers) > 64:
            _fail(f"{where}.tool_numbers", "liste de numéros d'outils attendue.")
        for position, number in enumerate(numbers):
            if _int(number, f"{where}.tool_numbers[{position}]", 1, 999) not in tool_numbers:
                _fail(f"{where}.tool_numbers[{position}]", "numéro d'outil absent du projet.")
        operations = item.get("operations", [])
        if not isinstance(operations, list) or len(operations) > cam_payload.CAM_BOUNDS["operations_max"]:
            _fail(f"{where}.operations", "liste d'identifiants d'opérations attendue.")
        for position, identifier in enumerate(operations):
            if identifier not in operation_ids:
                _fail(f"{where}.operations[{position}]", "identifiant d'opération absent du projet.")
        clean_files.append({"name": name, "sha256": _sha256(item["sha256"], f"{where}.sha256"),
                            "bytes": _optional_int(item.get("bytes"), f"{where}.bytes"),
                            "lines": _optional_int(item.get("lines"), f"{where}.lines"),
                            "tool_numbers": [int(number) for number in numbers], "operations": list(operations)})
    clean = {"format": HANDOFF_FORMAT, "version": HANDOFF_VERSION,
             "produced_at": _iso(document["produced_at"], "produced_at", required=True),
             "cam_job_id": _ref(document["cam_job_id"], "cam_job_id", required=True),
             "project_id": None, "project_version": clean_compute["result_version"],
             "name": clean_project["name"], "source": copy.deepcopy(clean_project["source"]), "project": clean_project,
             "machine_snapshot": copy.deepcopy(clean_project["machine"]), "units": clean_project["units"],
             "origin": copy.deepcopy(clean_project["origin"]), "tools": tool_summary(clean_project),
             "gcode_files": clean_files, "compute": clean_compute}
    project_id = document.get("project_id")
    if project_id is not None:
        if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
            _fail("project_id", "identifiant de projet invalide.")
        clean["project_id"] = project_id
    if "project_version" in document and _int(document["project_version"], "project_version", 0, 1000000) != clean_compute["result_version"]:
        _fail("project_version", "doit être égal à compute.result_version.")
    if "name" in document and document["name"] != clean_project["name"]:
        _fail("name", "doit reprendre le nom du projet.")
    for key in ("source", "machine_snapshot", "units", "origin"):
        if key in document and document[key] != clean[key]:
            _fail(key, "doit être la copie exacte du bloc correspondant du projet normalisé.")
    if "tools" in document and document["tools"] != clean["tools"]:
        _fail("tools", "résumé des outils différent de celui dérivé du projet (cam_handoff.tool_summary).")
    # Séquences : ordre exact des opérations du projet.
    file_of_operation = {identifier: item["name"] for item in clean_files for identifier in item["operations"]}
    given = document.get("sequences")
    if given is not None:
        if not isinstance(given, list) or [item.get("id") if isinstance(item, dict) else None for item in given] != operation_ids:
            _fail("sequences", "une entrée par opération du projet, dans le même ordre et avec les mêmes identifiants.")
        for index, item in enumerate(given):
            where = f"sequences[{index}]"
            _keys(item, where, ("index", "id", "type", "state"), ("name", "enabled", "tool", "gcode_file", "estimated_s", "segments"))
            if _int(item["index"], f"{where}.index") != index or item["type"] != clean_project["operations"][index]["type"]:
                _fail(where, "index ou type incohérent avec le projet.")
            _choice(item["state"], f"{where}.state", SEQUENCE_STATES)
            _file_name(item.get("gcode_file"), f"{where}.gcode_file", allow_none=True)
            if item.get("gcode_file") is not None and item["gcode_file"] not in names:
                _fail(f"{where}.gcode_file", "fichier absent de gcode_files.")
            estimated = item.get("estimated_s")
            if estimated is not None and (isinstance(estimated, bool) or not isinstance(estimated, (int, float)) or not 0 <= estimated <= 1e7):
                _fail(f"{where}.estimated_s", "durée estimée invalide.")
            _optional_int(item.get("segments"), f"{where}.segments")
        clean["sequences"] = copy.deepcopy(given)
    else:
        clean["sequences"] = sequences(clean_project, None, file_of_operation)
    steps = document.get("manual_steps")
    if steps is not None:
        if not isinstance(steps, list) or len(steps) > MAX_STEPS:
            _fail("manual_steps", f"liste attendue ({MAX_STEPS} au plus).")
        clean_steps = []
        for index, item in enumerate(steps):
            where = f"manual_steps[{index}]"
            _keys(item, where, ("index", "kind", "message"), ("operation_id", "tool_from", "tool_to", "stop_spindle", "position", "file"))
            if _int(item["index"], f"{where}.index") != index:
                _fail(f"{where}.index", "index attendu dans l'ordre.")
            kind = _choice(item["kind"], f"{where}.kind", MANUAL_KINDS)
            operation_id = item.get("operation_id")
            if operation_id is not None and operation_id not in operation_ids:
                _fail(f"{where}.operation_id", "identifiant d'opération absent du projet.")
            for key in ("tool_from", "tool_to"):
                number = _optional_int(item.get(key), f"{where}.{key}", 1, 999)
                if number is not None and number not in tool_numbers:
                    _fail(f"{where}.{key}", "numéro d'outil absent du projet.")
            position = item.get("position")
            if position is not None:
                machine_position = clean_project["machine"].get("tool_change_position")
                if not machine_position or not machine_position.get("verified"):
                    _fail(f"{where}.position", "position de changement d'outil sans position machine vérifiée.")
                _keys(position, f"{where}.position", ("x", "y", "z"))
                for axis in ("x", "y", "z"):
                    if isinstance(position[axis], bool) or not isinstance(position[axis], (int, float)):
                        _fail(f"{where}.position.{axis}", "nombre attendu.")
            file_name = _file_name(item.get("file"), f"{where}.file", allow_none=True)
            if file_name is not None and file_name not in names:
                _fail(f"{where}.file", "fichier absent de gcode_files.")
            clean_steps.append({"index": index, "kind": kind, "operation_id": operation_id,
                                "message": _text(item["message"], f"{where}.message", 400, allow_empty=True),
                                "tool_from": item.get("tool_from"), "tool_to": item.get("tool_to"),
                                "stop_spindle": _bool(item.get("stop_spindle", kind != "note"), f"{where}.stop_spindle"),
                                "position": None if position is None else {axis: float(position[axis]) for axis in ("x", "y", "z")},
                                "file": file_name})
        clean["manual_steps"] = clean_steps
    else:
        clean["manual_steps"] = manual_steps(clean_project, file_of_operation)
    acknowledged = document.get("warnings_acknowledged", [])
    if not isinstance(acknowledged, list) or len(acknowledged) > MAX_ISSUES:
        _fail("warnings_acknowledged", "liste de codes attendue.")
    clean["warnings_acknowledged"] = []
    for index, code in enumerate(acknowledged):
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            _fail(f"warnings_acknowledged[{index}]", "code d'avertissement invalide.")
        if code not in clean["warnings_acknowledged"]:
            clean["warnings_acknowledged"].append(code)
    validation = document.get("validation", {"ok": True, "errors": [], "warnings": []})
    _keys(validation, "validation", ("ok",), ("errors", "warnings"))
    clean["validation"] = {"ok": _bool(validation["ok"], "validation.ok"),
                           "errors": _issues(validation.get("errors", []), "validation.errors"),
                           "warnings": _issues(validation.get("warnings", []), "validation.warnings")}
    if clean["validation"]["errors"] or not clean["validation"]["ok"]:
        _fail("validation", "le G-code livré porte des erreurs de contrôle : aucun transfert tant qu'elles ne sont pas levées.",
              "CAM_HANDOFF_BLOCKED")
    clean["manufacturability"] = _issues(document.get("manufacturability", []), "manufacturability", with_line=True)
    not_simulated = document.get("not_simulated", [])
    if not isinstance(not_simulated, list) or len(not_simulated) > MAX_NOT_SIMULATED:
        _fail("not_simulated", f"liste de textes attendue ({MAX_NOT_SIMULATED} au plus).")
    clean["not_simulated"] = [_text(text, f"not_simulated[{index}]", 160) for index, text in enumerate(not_simulated)]
    clean["setup_sheet_name"] = _file_name(document.get("setup_sheet_name"), "setup_sheet_name", allow_none=True)
    clean["cam_json_name"] = _file_name(document.get("cam_json_name"), "cam_json_name", allow_none=True)
    encoded = canonical_json(clean).encode("utf-8")
    if len(encoded) > MAX_HANDOFF_BYTES:
        _fail("", f"document trop volumineux ({len(encoded) // 1024} Kio, {MAX_HANDOFF_BYTES // 1024} Kio au plus).",
              "CAM_HANDOFF_TOO_LARGE")
    return clean


def make_handoff(project, cam_job_id, gcode_files, produced_at, *, result=None, project_id=None, result_version=0,
                 computed_at=None, worker_id=None, agent_version=None, warnings_acknowledged=(), validation=None,
                 manufacturability=(), not_simulated=(), setup_sheet_name=None, cam_json_name=None, fingerprint=None):
    """Assemble un document de transfert à partir d'un projet (v1 ou v2), des fichiers livrés et du résultat, puis le valide.

    ``gcode_files`` : ``[{name, sha256, bytes?, lines?, tool_numbers?, operations?}]`` (noms finaux de l'espace de travail).
    ``result`` : ``cam_result.json`` du moteur (``freecad_version``, ``post_processor``, ``summary.operations``…).
    ``fingerprint`` : empreinte calculée au moment du calcul ; par défaut celle du projet actuel (le document reste
    refusé si le projet a changé depuis).
    """
    clean_project = cam_payload.validate_cam_project(project, as_version=2)
    result = result if isinstance(result, dict) else {}
    file_of_operation = {}
    for item in gcode_files if isinstance(gcode_files, list) else []:
        for identifier in item.get("operations", []) if isinstance(item, dict) else []:
            file_of_operation[identifier] = item.get("name")
    document = {
        "format": HANDOFF_FORMAT, "version": HANDOFF_VERSION, "produced_at": produced_at, "cam_job_id": cam_job_id,
        "project_id": project_id, "project_version": result_version, "name": clean_project["name"],
        "source": copy.deepcopy(clean_project["source"]), "project": clean_project,
        "machine_snapshot": copy.deepcopy(clean_project["machine"]), "units": clean_project["units"],
        "origin": copy.deepcopy(clean_project["origin"]), "tools": tool_summary(clean_project),
        "sequences": sequences(clean_project, result, file_of_operation), "gcode_files": copy.deepcopy(gcode_files),
        "manual_steps": manual_steps(clean_project, file_of_operation), "warnings_acknowledged": list(warnings_acknowledged),
        "validation": copy.deepcopy(validation) if validation is not None else {"ok": True, "errors": [], "warnings": []},
        "manufacturability": list(copy.deepcopy(manufacturability)), "not_simulated": list(not_simulated),
        "setup_sheet_name": setup_sheet_name, "cam_json_name": cam_json_name,
        "compute": {"state": "computed",
                    "fingerprint": fingerprint or hashlib.sha256(canonical_json(strip_neutral(clean_project)).encode("utf-8")).hexdigest(),
                    "result_version": result_version, "computed_at": computed_at or produced_at, "worker_id": worker_id,
                    "freecad_version": result.get("freecad_version"), "post_processor": result.get("post_processor"),
                    "agent_version": agent_version},
    }
    return validate_handoff(document)
