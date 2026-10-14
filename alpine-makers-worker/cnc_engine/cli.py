# -*- coding: utf-8 -*-
"""Exécution d'une opération du moteur CNC sur le Worker.

``python -m cnc_engine.cli <request.json> <result.json>`` (cwd = dossier du Worker) lit la requête
``{"op": "analyze"|"simulate", "gcode_path": <fichier transféré>, "machine": {...}}`` (``gcode``
en ligne accepté à la place de ``gcode_path``), exécute l'opération et écrit ``result.json`` :
``{ok, summary, warnings, errors, bounds, duration_s, duration, toolpath, outside_travel, ...}``
(voir ``analyze.py``). Un aperçu allégé ``preview.json`` ({bounds, toolpath, points, truncated})
est écrit à côté pour le canvas du studio.

Dernière ligne de la sortie standard : ``RAPHAL_CNC_RESULT=<json>`` —
``{"ok": true, "op", "bytes", "warnings", "errors", "program_ok", "artifacts"}`` ou
``{"ok": false, "error", "code", "status_code"}``. Codes de sortie : 0 succès (même si le programme
analysé contient des erreurs : l'analyse a abouti), 2 requête ou fichier invalide, 1 erreur interne.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any

from . import CncEngineError
from .analyze import MAX_GCODE_BYTES, OPERATIONS, analyze, simulate

RESULT_MARKER = "RAPHAL_CNC_RESULT="
PREVIEW_ARTIFACT = "preview.json"
MAX_REQUEST_BYTES = 4 * 1024 * 1024


def execute(op: str, data: Any) -> dict[str, Any]:
    """Une opération → le résultat complet. Lève CncEngineError pour toute erreur métier."""
    if op not in OPERATIONS:
        raise CncEngineError(f"Opération CNC inconnue « {op} » (attendu : {', '.join(OPERATIONS)})", 400, "UNKNOWN_OPERATION")
    if not isinstance(data, dict):
        raise CncEngineError("Requête CNC : objet JSON attendu", 400, "REQUEST_INVALID")
    text = data.get("gcode")
    path = data.get("gcode_path")
    if path:
        source = Path(str(path))
        if not source.is_file():
            raise CncEngineError("Programme G-code introuvable sur le Worker", 400, "GCODE_MISSING")
        if source.stat().st_size > MAX_GCODE_BYTES:
            raise CncEngineError("Programme G-code trop volumineux (64 Mo maximum)", 413, "GCODE_TOO_LARGE")
        text = source.read_text(encoding="utf-8-sig", errors="replace")
    if not isinstance(text, str):
        raise CncEngineError("Requête CNC : gcode_path ou gcode requis", 400, "REQUEST_INVALID")
    function = simulate if op == "simulate" else analyze
    return function(text, data.get("machine"))


def probe() -> dict[str, Any]:
    """Sonde d'installation : un programme minimal est analysé dans l'environnement du composant."""
    result = analyze("G21 G90\nM3 S1000\nG0 Z5\nG1 X10 Y0 F300\nM5\nM30\n", {"work_area_mm": [100, 100, 50]})
    return {"ok": result["ok"], "moves": result["summary"]["moves"], "operations": list(OPERATIONS)}


def _write_text(path: Path, text: str) -> None:
    """Écriture atomique : un résultat partiel n'est jamais pris pour un résultat complet."""
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _report(payload: dict[str, Any]) -> None:
    print(RESULT_MARKER + json.dumps(payload, ensure_ascii=False), flush=True)


def run(request_path: Path, result_path: Path) -> dict[str, Any]:
    """Lit la requête, exécute l'opération et écrit les artefacts ; renvoie le compte rendu (sans l'imprimer)."""
    if request_path.stat().st_size > MAX_REQUEST_BYTES:
        raise CncEngineError("Requête CNC trop volumineuse (4 Mo maximum)", 413, "REQUEST_TOO_LARGE")
    try:
        data = json.loads(request_path.read_text(encoding="utf-8-sig"))
    except ValueError:
        raise CncEngineError("Requête CNC illisible (JSON invalide)", 400, "REQUEST_INVALID") from None
    if not isinstance(data, dict):
        raise CncEngineError("Requête CNC : objet JSON attendu", 400, "REQUEST_INVALID")
    op = str(data.get("op") or "").strip().lower()
    result = execute(op, data)
    preview = {"units": "mm", "bounds": result["bounds"], "toolpath": result["toolpath"],
               "points": result["toolpath_points"], "truncated": result["toolpath_truncated"]}
    preview_path = result_path.with_name(PREVIEW_ARTIFACT)
    _write_text(preview_path, json.dumps(preview, ensure_ascii=False, separators=(",", ":")))
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    _write_text(result_path, payload)
    return {"ok": True, "op": op, "bytes": len(payload.encode("utf-8")), "warnings": len(result["warnings"]),
            "errors": len(result["errors"]), "program_ok": bool(result["ok"]),
            "artifacts": [result_path.name, preview_path.name]}


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:
        print("Usage : python -m cnc_engine.cli <request.json> <result.json>", file=sys.stderr)
        return 64
    request_path, result_path = Path(arguments[0]), Path(arguments[1])
    try:
        _report(run(request_path, result_path))
        return 0
    except CncEngineError as error:
        _report({"ok": False, "error": str(error), "code": error.code, "status_code": error.status_code})
        return 2
    except OSError as error:
        _report({"ok": False, "error": f"Fichier de requête ou de résultat CNC inaccessible : {error}",
                 "code": "REQUEST_IO", "status_code": 400})
        return 2
    except (TypeError, ValueError) as error:
        _report({"ok": False, "error": f"Requête CNC invalide : {error}", "code": "REQUEST_INVALID", "status_code": 400})
        return 2
    except Exception as error:  # noqa: BLE001 - le runner doit toujours recevoir un compte rendu
        traceback.print_exc()
        _report({"ok": False, "error": f"Erreur interne du moteur CNC : {type(error).__name__}: {error}",
                 "code": "ENGINE_ERROR", "status_code": 500})
        return 1


if __name__ == "__main__":
    sys.exit(main())
