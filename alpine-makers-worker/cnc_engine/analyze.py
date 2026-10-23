# -*- coding: utf-8 -*-
"""Analyse d'un programme G-code CNC pour Alpine CNC Studio.

``analyze(text, machine)`` relit le programme avec ``cam_gcode_check`` (erreurs structurelles et
avertissements, jamais dupliqués ici) puis complète : modes de distance et d'unités, plans de travail,
arcs (I/J/K et R), étendue X/Y/Z, longueurs usinée et rapide, durée estimée avec une accélération
simple, changements d'outil (T/M6), pauses (M0/M1), broche (M3/M4/M5, S maximal), arrosage
(M7/M8/M9), lignes douteuses (G53/G92/G10/G28/G30) et parcours échantillonné pour l'aperçu
(polylignes XY + Z, 20 000 points au plus).

``simulate(text, machine)`` ajoute le contrôle course par course (chaque segment contre la zone de
travail, relativement au point le plus bas du programme) et par outil. Rien d'autre en v1.

La « machine » est le dictionnaire envoyé par le studio : ``work_area_mm`` ([x, y, z] ou {x, y, z}),
``max_feed_mm_min``, ``spindle_max_rpm``, ``safe_z_mm``, ``controller`` ; tout est facultatif.
"""
from __future__ import annotations

import math
import re
from typing import Any

try:  # worker_agent.cnc_engine sous les tests du dépôt
    from .. import cam_gcode_check
except (ImportError, ValueError):  # ``python -m cnc_engine.cli`` depuis le dossier du Worker
    import cam_gcode_check  # type: ignore[no-redef]

from . import CncEngineError

OPERATIONS = ("analyze", "simulate")
PREVIEW_POINTS_LIMIT = 20_000
# Segments relus pour la durée et le contrôle des courses (jamais tronqués en dessous de cette borne).
SEGMENTS_LIMIT = 2_000_000
MAX_GCODE_BYTES = 64 * 1024 * 1024
DEFAULT_RAPID_MM_MIN = 3000.0
DEFAULT_FEED_MM_MIN = 600.0
# Accélération d'une petite fraiseuse (GRBL : 100 à 1 000 mm/s²) ; chaque segment part et s'arrête à zéro.
DEFAULT_ACCEL_MM_S2 = 300.0
QUESTIONABLE = {
    "G53": "mouvement en coordonnées machine (ignore les décalages pièce)",
    "G92": "redéfinition de l'origine pièce en cours de programme",
    "G10": "écriture directe des décalages ou des paramètres",
    "G28": "retour à la position de référence (position machine)",
    "G30": "retour à la seconde position de référence",
}
_TOKEN = re.compile(r"([A-Z])\s*([-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+))")


def _words(line: str) -> list[tuple[str, float]]:
    """Mots (lettre, valeur) d'une ligne sans commentaire ; « G01 » devient G1."""
    text = line.split(";", 1)[0]
    text = re.sub(r"\([^)]*\)?", " ", text).strip().upper()
    if text.startswith("/"):
        text = text[1:]
    if not text or text.startswith("%"):
        return []
    # Même lecture des nombres que le contrôle (« X0. », « .5 », « Z-3.5. » réparé) ; repli tolérant sinon.
    words, _notes = cam_gcode_check.read_words(text)
    if words is not None:
        return [(letter, float(value)) for letter, value in words if value is not None]
    return [(letter, float(value)) for letter, value in _TOKEN.findall(text)]


def _axis_values(raw: Any) -> dict[str, float | None]:
    if isinstance(raw, dict):
        return {axis: _positive(raw.get(axis, raw.get(axis.upper()))) for axis in "xyz"}
    if isinstance(raw, (list, tuple)):
        values = list(raw) + [None] * 3
        return {axis: _positive(values[index]) for index, axis in enumerate("xyz")}
    return {"x": None, "y": None, "z": None}


def _positive(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
    return float(value) if math.isfinite(value) and value > 0 else None


def machine_profile(machine: Any) -> dict[str, Any]:
    """Profil machine normalisé en mm : zone de travail, avance maximale, broche, Z de sécurité, contrôleur."""
    if machine is None:
        machine = {}
    if not isinstance(machine, dict):
        raise CncEngineError("machine : objet attendu", 400, "REQUEST_INVALID")
    area = _axis_values(machine.get("work_area_mm") or machine.get("travel"))
    accel = _positive(machine.get("accel_mm_s2")) or DEFAULT_ACCEL_MM_S2
    return {
        "work_area_mm": area,
        "max_feed_mm_min": _positive(machine.get("max_feed_mm_min")),
        "spindle_max_rpm": _positive(machine.get("spindle_max_rpm")),
        "safe_z_mm": machine.get("safe_z_mm") if isinstance(machine.get("safe_z_mm"), (int, float))
        and not isinstance(machine.get("safe_z_mm"), bool) and math.isfinite(machine.get("safe_z_mm")) else None,
        "controller": str(machine.get("controller") or "grbl").strip().lower()[:32],
        "accel_mm_s2": accel,
    }


def _check_machine(profile: dict[str, Any]) -> dict[str, Any]:
    """Forme attendue par cam_gcode_check.check_gcode(machine=...)."""
    travel = {axis: value for axis, value in profile["work_area_mm"].items() if value}
    checked: dict[str, Any] = {"travel": travel}
    if profile["max_feed_mm_min"]:
        checked["max_feed"] = profile["max_feed_mm_min"]
        checked["max_rapid"] = profile["max_feed_mm_min"]
    if profile["spindle_max_rpm"]:
        checked["spindle"] = {"max_rpm": profile["spindle_max_rpm"]}
    return checked


def _scan(text: str) -> dict[str, Any]:
    """Relecture ligne par ligne : modes, plans, arcs, outils, pauses, broche, arrosage, lignes douteuses."""
    modes = {"distance": [], "units": [], "planes": []}
    arcs = {"cw": 0, "ccw": 0, "center_format": 0, "radius_format": 0}
    tool_changes: list[dict[str, Any]] = []
    pauses: list[dict[str, Any]] = []
    spindle = {"starts": 0, "stops": 0, "directions": [], "max_rpm": 0.0, "commands": 0}
    coolant = {"flood": 0, "mist": 0, "stops": 0}
    questionable: list[dict[str, Any]] = []
    current_tool = None
    lines = 0
    dwell_s = 0.0
    for number, raw in enumerate(text.splitlines(), start=1):
        lines += 1
        words = _words(raw)
        if not words:
            continue
        codes = {f"{letter}{value:g}" for letter, value in words if letter in ("G", "M")}
        values = {letter: value for letter, value in words if letter not in ("G", "M", "N")}
        for code, target in (("G90", "distance"), ("G91", "distance"), ("G20", "units"), ("G21", "units"),
                             ("G17", "planes"), ("G18", "planes"), ("G19", "planes")):
            if code in codes and code not in modes[target]:
                modes[target].append(code)
        if "G2" in codes or "G3" in codes:
            arcs["cw" if "G2" in codes else "ccw"] += 1
            if "R" in values:
                arcs["radius_format"] += 1
            elif any(key in values for key in "IJK"):
                arcs["center_format"] += 1
        if "T" in values:
            current_tool = int(values["T"])
        if "M6" in codes:
            tool_changes.append({"line": number, "tool": current_tool})
        for code in ("M0", "M1"):
            if code in codes:
                pauses.append({"line": number, "code": code, "optional": code == "M1"})
        if "S" in values:
            spindle["max_rpm"] = max(spindle["max_rpm"], float(values["S"]))
        for code, direction in (("M3", "cw"), ("M4", "ccw")):
            if code in codes:
                spindle["starts"] += 1
                spindle["commands"] += 1
                if direction not in spindle["directions"]:
                    spindle["directions"].append(direction)
        if "M5" in codes:
            spindle["stops"] += 1
            spindle["commands"] += 1
        if "M7" in codes:
            coolant["mist"] += 1
        if "M8" in codes:
            coolant["flood"] += 1
        if "M9" in codes:
            coolant["stops"] += 1
        if "G4" in codes and "P" in values:
            dwell_s += max(0.0, float(values["P"]))   # GRBL : P en secondes
        for code, reason in QUESTIONABLE.items():
            if code in codes:
                questionable.append({"line": number, "code": code, "reason": reason, "text": raw.strip()[:80]})
    return {"lines": lines, "modes": modes, "arcs": arcs, "tool_changes": tool_changes, "pauses": pauses,
            "spindle": spindle, "coolant": coolant, "questionable": questionable, "dwell_s": dwell_s}


def _segment_duration(length: float, speed_mm_min: float, accel: float) -> float:
    """Profil trapèze (ou triangle) partant de zéro et s'arrêtant à zéro, en secondes."""
    if length <= 0:
        return 0.0
    speed = max(speed_mm_min, 1e-6) / 60.0
    ramp = speed * speed / accel
    if length >= ramp:
        return length / speed + speed / accel
    return 2.0 * math.sqrt(length / accel)


def _polylines(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Segments consécutifs de même nature (et même outil) enchaînés en polylignes [x, y, z]."""
    polylines: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for segment in segments:
        kind = segment["k"]
        if kind == "tool":
            polylines.append({"kind": "tool", "tool": segment["t"], "points": [list(segment["a"])]})
            current = None
            continue
        a, b = list(segment["a"]), list(segment["b"])
        if current is not None and current["kind"] == kind and current["tool"] == segment["t"] and current["points"][-1] == a:
            current["points"].append(b)
            continue
        current = {"kind": kind, "tool": segment["t"], "feed": segment.get("f"), "points": [a, b]}
        polylines.append(current)
    return polylines


def _sample(polylines: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], bool, int]:
    """Au-delà de ``limit`` points, chaque polyligne garde un point sur n (premier et dernier conservés)."""
    total = sum(len(item["points"]) for item in polylines)
    if total <= limit:
        return polylines, False, total
    step = math.ceil(total / limit)
    sampled = []
    kept = 0
    for item in polylines:
        points = item["points"]
        if len(points) <= 2:
            chosen = points
        else:
            chosen = points[:-1:step]
            if chosen[-1] != points[-1]:
                chosen.append(points[-1])
        sampled.append({**item, "points": chosen})
        kept += len(chosen)
    return sampled, True, kept


def _travel_check(segments: list[dict[str, Any]], bounds: dict[str, Any] | None, profile: dict[str, Any]) -> dict[str, Any]:
    """Chaque course (segment) contre la zone de travail, relativement au minimum du programme ; bilan par outil."""
    area = profile["work_area_mm"]
    violations: list[dict[str, Any]] = []
    by_tool: dict[str, dict[str, Any]] = {}
    origin = bounds["min"] if bounds else [0.0, 0.0, 0.0]
    for index, segment in enumerate(segments):
        if segment["k"] == "tool":
            continue
        key = str(segment["t"]) if segment["t"] is not None else "none"
        entry = by_tool.setdefault(key, {"tool": segment["t"], "segments": 0, "outside": 0,
                                         "min": [math.inf] * 3, "max": [-math.inf] * 3})
        entry["segments"] += 1
        outside = False
        for point in (segment["a"], segment["b"]):
            for axis_index, axis in enumerate("xyz"):
                value = float(point[axis_index])
                entry["min"][axis_index] = min(entry["min"][axis_index], value)
                entry["max"][axis_index] = max(entry["max"][axis_index], value)
                limit = area.get(axis)
                if limit and value - origin[axis_index] > limit + 0.01:
                    outside = True
                    if len(violations) < 200:
                        violations.append({"segment": index, "tool": segment["t"], "axis": axis, "kind": segment["k"],
                                           "value_mm": round(value, 3), "extent_mm": round(value - origin[axis_index], 3),
                                           "limit_mm": limit})
        if outside:
            entry["outside"] += 1
    for entry in by_tool.values():
        entry["min"] = [round(value, 3) for value in entry["min"]] if entry["segments"] else None
        entry["max"] = [round(value, 3) for value in entry["max"]] if entry["segments"] else None
    return {"ok": not violations, "violations": violations, "by_tool": by_tool,
            "checked_axes": [axis for axis in "xyz" if area.get(axis)]}


def analyze(text: Any, machine: Any = None, *, preview_limit: int = PREVIEW_POINTS_LIMIT) -> dict[str, Any]:
    """Analyse complète ; ``ok`` reflète l'absence d'erreur structurelle (cam_gcode_check)."""
    if not isinstance(text, str):
        raise CncEngineError("Programme G-code : texte attendu", 400, "REQUEST_INVALID")
    if len(text.encode("utf-8", errors="replace")) > MAX_GCODE_BYTES:
        raise CncEngineError("Programme G-code trop volumineux (64 Mo maximum)", 413, "GCODE_TOO_LARGE")
    if not text.strip():
        raise CncEngineError("Programme G-code vide", 400, "GCODE_EMPTY")
    profile = machine_profile(machine)
    report = cam_gcode_check.check_gcode(text, machine=_check_machine(profile))
    scan = _scan(text)
    simulation = cam_gcode_check.simulation_segments(text, limit=SEGMENTS_LIMIT)
    segments = simulation["segments"]
    rapid_speed = profile["max_feed_mm_min"] or DEFAULT_RAPID_MM_MIN
    accel = profile["accel_mm_s2"]
    duration = {"rapid_s": 0.0, "cut_s": 0.0}
    rapid_below_safe = 0
    for segment in segments:
        if segment["k"] == "tool":
            continue
        a, b = segment["a"], segment["b"]
        length = math.dist(a, b)
        if segment["k"] == "rapid":
            duration["rapid_s"] += _segment_duration(length, rapid_speed, accel)
            if (profile["safe_z_mm"] is not None and math.hypot(b[0] - a[0], b[1] - a[1]) > 1e-6
                    and min(a[2], b[2]) < float(profile["safe_z_mm"]) - 0.01):
                rapid_below_safe += 1
        else:
            duration["cut_s"] += _segment_duration(length, segment.get("f") or DEFAULT_FEED_MM_MIN, accel)
    duration["dwell_s"] = round(scan["dwell_s"], 1)
    duration["rapid_s"], duration["cut_s"] = round(duration["rapid_s"], 1), round(duration["cut_s"], 1)
    duration["total_s"] = round(duration["rapid_s"] + duration["cut_s"] + duration["dwell_s"], 1)
    duration["accel_mm_s2"] = accel
    duration["rapid_mm_min"] = rapid_speed
    duration["linear_s"] = report["stats"]["time_estimate_s"]
    warnings = list(report["warnings"])
    if rapid_below_safe:
        warnings.append({"code": "rapid_below_safe_z", "line": 0, "count": rapid_below_safe, "ackable": True,
                         "message": f"{rapid_below_safe} déplacement(s) rapide(s) horizontaux sous le Z de sécurité "
                                    f"({profile['safe_z_mm']:g} mm)."})
    if scan["questionable"]:
        warnings.append({"code": "questionable_lines", "line": scan["questionable"][0]["line"], "count": len(scan["questionable"]),
                         "ackable": True, "message": "Lignes à relire avant usinage (coordonnées machine, origine ou décalages modifiés)."})
    if len(scan["modes"]["distance"]) > 1:
        warnings.append({"code": "mixed_distance_modes", "line": 0, "count": 1, "ackable": True,
                         "message": "Le programme mélange G90 (absolu) et G91 (relatif)."})
    if len(scan["modes"]["units"]) > 1:
        warnings.append({"code": "mixed_units", "line": 0, "count": 1, "ackable": True,
                         "message": "Le programme mélange G20 (pouces) et G21 (mm)."})
    polylines = _polylines(segments)
    polylines, truncated, points = _sample(polylines, max(1, int(preview_limit)))
    bounds = report["stats"]["bounds"]
    outside_travel = any(error["code"] == "outside_travel" for error in report["errors"])
    extent = None
    if bounds:
        extent = {axis: round(bounds["max"][index] - bounds["min"][index], 3) for index, axis in enumerate("xyz")}
    summary = {
        "lines": scan["lines"],
        "moves": report["stats"]["moves"],
        "program_units": report["stats"]["program_units"],
        "modes": scan["modes"],
        "arcs": scan["arcs"],
        "tools_used": report["stats"]["tools_used"],
        "tool_changes": scan["tool_changes"],
        "pauses": scan["pauses"],
        "spindle": {**scan["spindle"], "max_rpm": round(scan["spindle"]["max_rpm"], 1)},
        "coolant": scan["coolant"],
        "length_mm": report["stats"]["length_mm"],
        "extent_mm": extent,
        "questionable": scan["questionable"],
    }
    return {
        "ok": bool(report["ok"]),
        "summary": summary,
        "errors": report["errors"],
        "warnings": warnings,
        "bounds": bounds,
        "outside_travel": outside_travel,
        "duration_s": duration["total_s"],
        "duration": duration,
        "machine": profile,
        "toolpath": polylines,
        "toolpath_points": points,
        "toolpath_truncated": truncated or bool(simulation["truncated"]),
        "units": "mm",
    }


def simulate(text: Any, machine: Any = None, *, preview_limit: int = PREVIEW_POINTS_LIMIT) -> dict[str, Any]:
    """Analyse + contrôle course par course et par outil contre la zone de travail (v1)."""
    result = analyze(text, machine, preview_limit=preview_limit)
    segments = cam_gcode_check.simulation_segments(text, limit=SEGMENTS_LIMIT)["segments"]
    travel = _travel_check(segments, result["bounds"], result["machine"])
    result["travel_check"] = travel
    if not travel["ok"]:
        result["outside_travel"] = True
        if not any(error["code"] == "outside_travel" for error in result["errors"]):
            first = travel["violations"][0]
            result["errors"].append({"code": "outside_travel", "line": 0, "count": len(travel["violations"]),
                                     "message": f"Course hors zone de travail sur {first['axis'].upper()} "
                                                f"(outil T{first['tool']}, {first['extent_mm']:g} mm pour {first['limit_mm']:g} mm)."})
            result["ok"] = False
    return result
