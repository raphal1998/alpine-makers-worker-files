# -*- coding: utf-8 -*-
"""Exécution d'une opération du moteur laser sur le Worker.

``python -m laser_engine.cli <request.json> <result.json> <op>`` (cwd = dossier du Worker) lit la
requête JSON — exactement le corps qu'envoyait Alpine Laser Studio aux anciennes routes
``/api/laser/<op>`` du site : ``svg``/``dpi`` ; ``layers``/``machine``/``order``/``images``/
``offset_mm``/``include_operations`` ; ``gcode`` ; ``image_b64``/``width_mm``/… ; ``speed_mm_min``/
``power_pct`` pour le cadrage — exécute l'opération et écrit ``result.json`` : l'ancien corps de
réponse sans la clé ``ok`` (parse → {document, layers} ; plan → {plan} ; gcode → {gcode, stats,
warnings} ; frame → {gcode, bounds} ; simulate → le dict de ``simulate()`` ; raster → {operation}).
Pour gcode et frame, le G-code est aussi écrit dans ``output.gcode`` à côté du résultat.

La dernière ligne de la sortie standard est ``RAPHAL_LASER_RESULT=<json>`` :
``{"ok": true, "bytes", "warnings", "artifacts"}`` ou ``{"ok": false, "error", "code", "status_code"}``.
Codes de sortie : 0 succès, 2 erreur métier ou requête invalide, 1 erreur inattendue du moteur.
``execute(op, data)`` est aussi utilisé par le blueprint d'aperçu du site (``laser_toolpath.py``).
Pillow n'est requis que pour ``raster`` (sinon erreur claire ``PILLOW_MISSING``).
Les réglages d'une image tramée sur le Worker (``raster_sources[].settings``) suivent le calque :
``image_mode`` prime sur ``mode``, et chaque sous-calque actif (``settings.sublayers``) ajoute une
opération image sur la même image. L'opération ``raster`` isolée ne renvoie que l'opération principale.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any

from .toolpath import (LayerSettings, MachineProfile, Operation, Plan, ToolpathError, frame_gcode, from_dict,
                       generate_gcode, layers_from_document, parse_svg, plan, raster_image, raster_operations,
                       simulate, to_dict)

OPERATIONS = ("parse", "plan", "gcode", "frame", "simulate", "raster")
# Assistant de code (agent 1.32.0) : réservées au Worker, jamais exposées par le blueprint d'aperçu du site
# (qui publie une route par entrée d'OPERATIONS) — elles lancent Claude Code / Codex et le bac à sable.
WORKER_OPERATIONS = ("script", "assistant", "assistant_status")
CANCEL_MARKER = "cancel.request"
GCODE_OPERATIONS = ("gcode", "frame")
GCODE_ARTIFACT = "output.gcode"
MAX_REQUEST_BYTES = 32 * 1024 * 1024
RESULT_MARKER = "RAPHAL_LASER_RESULT="
# Trame calculée sur le Worker (agent 1.28.0) : le studio envoie les images sources (« raster_sources »)
# au lieu de trames de dizaines de Mo ; un gros G-code reste un artefact (extrait + simulation allégée).
MAX_RASTER_SOURCES = 32
INLINE_GCODE_BYTES = 2 * 1024 * 1024
GCODE_EXCERPT_LINES = 200
DISPLAY_SEGMENTS_LIMIT = 120_000


# Import d'un très gros SVG (image vectorisée en millions de rectangles de pixels ou en escaliers) : au-delà de ce
# nombre de points, les morceaux fermés et disjoints d'une même forme sont fusionnés en surfaces (même rendu en
# remplissage evenodd), puis chaque contour est simplifié à 0,05 mm (la tolérance d'aplatissement des courbes,
# sous la finesse d'un spot). Les coordonnées sont toujours arrondies à 0,1 µm. Sans cela, l'analyse dépasse vite
# ce que le navigateur peut relire.
PARSE_LIGHTEN_ABOVE = 150_000
PARSE_SIMPLIFY_MM = 0.05


def _douglas_peucker(points: list[Any], tolerance: float) -> list[Any]:
    """Douglas-Peucker itératif (sans récursion) ; garde le premier et le dernier point."""
    count = len(points)
    if count < 3:
        return list(points)
    keep = bytearray(count)
    keep[0] = keep[-1] = 1
    limit = tolerance * tolerance
    stack = [(0, count - 1)]
    while stack:
        first, last = stack.pop()
        ax, ay = points[first]
        bx, by = points[last]
        dx, dy = bx - ax, by - ay
        length = dx * dx + dy * dy
        best, index = -1.0, -1
        for position in range(first + 1, last):
            px, py = points[position]
            if length == 0:
                distance = (px - ax) ** 2 + (py - ay) ** 2
            else:
                cross = (px - ax) * dy - (py - ay) * dx
                distance = cross * cross / length
            if distance > best:
                best, index = distance, position
        if best > limit:
            keep[index] = 1
            stack.append((first, index))
            stack.append((index, last))
    return [point for point, kept in zip(points, keep) if kept]


def _simplify_path(points: list[Any], tolerance: float) -> list[Any]:
    try:
        from shapely.geometry import LineString
    except ImportError:
        return _douglas_peucker(points, tolerance)
    simplified = list(LineString(points).simplify(tolerance, preserve_topology=False).coords) if len(points) > 2 else list(points)
    return simplified if len(simplified) >= 2 else list(points)


def _merge_disjoint(paths: list[Any], tolerance: float) -> list[Any] | None:
    """Morceaux fermés disjoints (ex. rectangles de pixels) → contours de leur union simplifiés, sinon None.

    L'union n'a le même rendu qu'un remplissage evenodd que si aucun morceau n'en recouvre un autre (un trou
    dessiné par un contour intérieur serait comblé) : couverture sans chevauchement vérifiée, puis aire de
    l'union égale à la somme des aires. Opérations vectorisées de shapely (des centaines de milliers de morceaux)."""
    try:
        import shapely
    except ImportError:
        return None
    rings = []
    for path in paths:
        if len(path) < 3:
            return None
        ring = [tuple(point) for point in path]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        if len(ring) < 4:
            return None
        rings.append(ring)
    if len(rings) < 2:
        return None
    try:
        polygons = shapely.polygons(rings)
    except (ValueError, TypeError, shapely.errors.GEOSException):
        return None
    areas = shapely.area(polygons)
    if not bool(shapely.is_valid(polygons).all()) or not bool((areas > 0).all()):
        return None
    total = float(areas.sum())
    try:
        # Couverture sans chevauchement (shapely 2.1) : union rapide qui dissout les bords communs.
        if not bool(shapely.coverage_is_valid(polygons)):
            return None
        merged = shapely.coverage_union_all(polygons)
    except AttributeError:
        merged = shapely.union_all(polygons)
    if merged.is_empty or abs(merged.area - total) > max(1e-9, total * 1e-6):
        return None
    merged = merged.simplify(tolerance, preserve_topology=True)
    result = []
    for polygon in shapely.get_parts(merged):
        if polygon.is_empty or polygon.geom_type != "Polygon":
            continue
        result.append(list(polygon.exterior.coords))
        result.extend(list(ring.coords) for ring in polygon.interiors)
    return result or None


def lighten_document(document: Any, tolerance: float = PARSE_SIMPLIFY_MM, above: int = PARSE_LIGHTEN_ABOVE) -> None:
    """Allège en place l'analyse d'un SVG : arrondi à 0,1 µm ; au-delà de ``above`` points, fusion des morceaux
    disjoints des formes fermées puis simplification des contours à ``tolerance`` mm."""
    total = sum(len(path) for shape in document.shapes for path in shape.paths)
    simplify = total > above
    after = merged_shapes = 0
    for shape in document.shapes:
        paths, merged = shape.paths, False
        if simplify and shape.closed and len(paths) > 1:
            rings = _merge_disjoint(paths, tolerance)
            if rings is not None:
                paths, merged, merged_shapes = rings, True, merged_shapes + 1
        rounded = []
        for path in paths:
            points = [(round(x, 4), round(y, 4)) for x, y in path]
            # Contours fusionnés : déjà simplifiés avec leur topologie ; un petit tracé (rectangle…) n'a rien à perdre.
            if simplify and not merged and len(points) > 8:
                closed = len(points) > 2 and points[0] == points[-1]
                reduced = _simplify_path(points, tolerance)
                points = reduced if len(reduced) >= (4 if closed else 2) else points
            after += len(points)
            rounded.append(points)
        shape.paths = rounded
    if simplify and after < total:
        count = lambda value: f"{value:,}".replace(",", " ")  # noqa: E731
        merged_text = f"morceaux jointifs fusionnés dans {merged_shapes} forme(s), " if merged_shapes else ""
        document.warnings.append(f"Tracés allégés à l'import : {count(total)} → {count(after)} points ({merged_text}écart de "
                                 "0,05 mm au plus, sous la finesse du laser).")


def _raster_operation(data: dict[str, Any]) -> Operation:
    """Image base64 + placement + réglages → opération « image » (même corps que l'opération raster)."""
    return raster_image(*_raster_arguments(data))


def _raster_arguments(data: dict[str, Any]) -> tuple[Any, ...]:
    """Arguments de raster_image() : octets de l'image, taille, pas, mode, inversion, réglages, placement.
    Le mode d'image des réglages (``settings.image_mode``) prime sur ``mode`` s'il est défini."""
    encoded = str(data.get("image_b64") or "")
    if "," in encoded and encoded.lstrip().startswith("data:"):
        encoded = encoded.split(",", 1)[1]
    if not encoded:
        raise ToolpathError("image_b64 manquant")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise ToolpathError("image_b64 illisible") from None
    settings = from_dict(LayerSettings, data.get("settings") or {}) if data.get("settings") else None
    offset = data.get("offset_mm") or (0.0, 0.0)
    if not isinstance(offset, (list, tuple)) or len(offset) != 2:
        raise ToolpathError("offset_mm : [x, y] attendu")
    return (raw, float(data.get("width_mm") or 0), float(data.get("height_mm") or 0),
            float(data.get("interval_mm") or 0.1), str(data.get("mode") or "grayscale"),
            bool(data.get("invert")), settings, (offset[0], offset[1]))


def _raster_sources(data: dict[str, Any]) -> list[Operation]:
    """Images à tramer ici même, dans le job du plan ou du G-code : rien de lourd ne transite."""
    sources = data.get("raster_sources") or []
    if not isinstance(sources, list) or len(sources) > MAX_RASTER_SOURCES:
        raise ToolpathError(f"raster_sources : liste de {MAX_RASTER_SOURCES} images au plus attendue", 400, "REQUEST_INVALID")
    operations = []
    for index, source in enumerate(sources, 1):
        if not isinstance(source, dict):
            raise ToolpathError(f"raster_sources : image {index} invalide (objet attendu)", 400, "REQUEST_INVALID")
        name = str(source.get("name") or "").strip()[:80]
        # Opération principale puis une par sous-calque actif (settings.sublayers), même image.
        operations.extend(raster_operations(*_raster_arguments(source), name=name))
    return operations


def _image_summary(operation: Operation) -> dict[str, Any]:
    """Opération image d'un aperçu : bornes machine et nombre de segments, sans les segments eux-mêmes."""
    xs = [point[0] for segment in operation.segments for point in segment]
    ys = [point[1] for segment in operation.segments for point in segment]
    bounds = {"min_x": round(min(xs), 3), "min_y": round(min(ys), 3), "max_x": round(max(xs), 3), "max_y": round(max(ys), 3)} if xs else None
    return {"layer": operation.layer, "mode": operation.mode, "passes": operation.passes, "name": operation.name,
            "segments": len(operation.segments), "bounds": bounds, "summary": True}


def _display_segments(segments: list[Any], limit: int = DISPLAY_SEGMENTS_LIMIT) -> dict[str, Any]:
    """Segments de simulation allégés pour l'affichage : pixels voisins d'une même ligne fusionnés
    (puissance la plus forte), puis échantillonnage régulier au-delà de ``limit`` (``truncated``)."""
    merged: list[list[float]] = []
    for segment in segments:
        x1, y1, x2, y2, power = (float(value) for value in segment[:5])
        if merged and power > 0:
            last = merged[-1]
            if last[4] > 0 and abs(last[2] - x1) < 1e-6 and abs(last[3] - y1) < 1e-6 and abs(last[1] - last[3]) < 1e-6 and abs(y1 - y2) < 1e-6:
                last[2], last[3], last[4] = x2, y2, max(last[4], power)
                continue
        merged.append([x1, y1, x2, y2, power])
    truncated = len(merged) > limit
    if truncated:
        step = len(merged) / limit
        merged = [merged[int(index * step)] for index in range(limit)]
    return {"segments": [[round(value, 3) for value in segment] for segment in merged], "display_segments": len(merged), "truncated": truncated}


def _compact_gcode_result(result: dict[str, Any], text: str) -> dict[str, Any]:
    """Gros G-code : il reste dans output.gcode ; result.json garde un extrait, ses tailles et une simulation allégée."""
    lines = text.splitlines()
    compact = {key: value for key, value in result.items() if key != "gcode"}
    compact.update(gcode_excerpt="\n".join(lines[:GCODE_EXCERPT_LINES]), gcode_lines=sum(1 for line in lines if line.strip()),
                   gcode_bytes=len(text.encode("utf-8")))
    try:
        simulated = simulate(text)
        compact["simulation"] = {**{key: value for key, value in simulated.items() if key != "segments"},
                                 **_display_segments(simulated.get("segments") or [])}
    except ToolpathError as error:
        compact["simulation_error"] = str(error)
    return compact


def _build_plan(data: dict[str, Any]) -> tuple[Plan, MachineProfile]:
    svg = data.get("svg")
    document = parse_svg(svg, float(data.get("dpi") or 96.0)) if svg else None
    layers_raw = data.get("layers") or {}
    if not isinstance(layers_raw, dict):
        raise ToolpathError("layers : objet {couleur: réglages} attendu")
    layers = {str(key): from_dict(LayerSettings, value) for key, value in layers_raw.items()}
    if document is not None and not layers:
        layers = layers_from_document(document)
    machine = from_dict(MachineProfile, data.get("machine") or {})
    images = [from_dict(Operation, item) for item in (data.get("images") or [])] + _raster_sources(data)
    offset = data.get("offset_mm") or (0.0, 0.0)
    if not isinstance(offset, (list, tuple)) or len(offset) != 2:
        raise ToolpathError("offset_mm : [x, y] attendu")
    result = plan(document, layers, machine, str(data.get("order") or "layer"), images, (offset[0], offset[1]))
    return result, machine


def execute(op: str, data: Any) -> dict[str, Any]:
    """Une opération → le corps de réponse (sans ``ok``). Lève ToolpathError pour toute erreur métier."""
    if op not in OPERATIONS:
        raise ToolpathError(f"Opération laser inconnue « {op} » (attendu : {', '.join(OPERATIONS)})", 400, "UNKNOWN_OPERATION")
    if not isinstance(data, dict):
        raise ToolpathError("Requête laser : objet JSON attendu", 400, "REQUEST_INVALID")
    if op == "parse":
        document = parse_svg(str(data.get("svg") or ""), float(data.get("dpi") or 96.0))
        lighten_document(document)
        return {"document": to_dict(document),
                "layers": {key: to_dict(value) for key, value in layers_from_document(document).items()}}
    if op == "plan":
        result, _ = _build_plan(data)
        payload = to_dict(result)
        if not data.get("include_operations"):
            payload["operations"] = [{"layer": item.layer, "mode": item.mode, "passes": item.passes,
                                      "segments": len(item.segments), "name": item.name} for item in result.operations]
        elif data.get("raster_sources"):
            # Trame calculée ici : l'aperçu reçoit les bornes des images, pas leurs centaines de milliers de segments.
            payload["operations"] = [_image_summary(item) if item.mode == "image" else entry
                                     for item, entry in zip(result.operations, payload["operations"])]
        return {"plan": payload}
    if op == "gcode":
        result, machine = _build_plan(data)
        return {"gcode": generate_gcode(result, machine), "stats": result.stats, "warnings": result.warnings}
    if op == "frame":
        result, machine = _build_plan(data)
        speed = float(data.get("speed_mm_min") or 3000.0)
        return {"gcode": frame_gcode(result, machine, speed, float(data.get("power_pct") or 0.0)),
                "bounds": result.stats.get("bounds")}
    if op == "simulate":
        return simulate(str(data.get("gcode") or ""))
    return {"operation": to_dict(_raster_operation(data))}


def warning_count(op: str, result: dict[str, Any]) -> int:
    """Nombre d'avertissements portés par le résultat (document, plan ou G-code)."""
    container: Any = result
    if op == "parse":
        container = result.get("document") or {}
    elif op == "plan":
        container = result.get("plan") or {}
    warnings = container.get("warnings") if isinstance(container, dict) else None
    return len(warnings) if isinstance(warnings, list) else 0


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


def execute_worker(op: str, data: Any, workdir: Path) -> dict[str, Any]:
    """Opérations de l'assistant de code : dossier du job, annulation par le fichier ``cancel.request``."""
    from . import assistant

    if not isinstance(data, dict):
        raise ToolpathError("Requête laser : objet JSON attendu", 400, "REQUEST_INVALID")
    cancelled = lambda: (workdir / CANCEL_MARKER).exists()  # noqa: E731
    if op == "assistant_status":
        return assistant.status(data)
    if op == "script":
        return assistant.run_script(data, workdir / "assistant", cancelled)
    return assistant.run_assistant(data, workdir / "assistant", cancelled)


def run(request_path: Path, result_path: Path, op: str) -> dict[str, Any]:
    """Lit la requête, exécute ``op`` et écrit les artefacts ; renvoie le compte rendu (sans l'imprimer)."""
    if request_path.stat().st_size > MAX_REQUEST_BYTES:
        raise ToolpathError("Requête laser trop volumineuse (32 Mo maximum)", 413, "REQUEST_TOO_LARGE")
    try:
        data = json.loads(request_path.read_text(encoding="utf-8-sig"))
    except ValueError:
        raise ToolpathError("Requête laser illisible (JSON invalide)", 400, "REQUEST_INVALID") from None
    result = execute_worker(op, data, result_path.parent) if op in WORKER_OPERATIONS else execute(op, data)
    artifacts = [result_path.name]
    if op in GCODE_OPERATIONS:
        gcode_text = str(result.get("gcode") or "")
        gcode_path = result_path.with_name(GCODE_ARTIFACT)
        _write_text(gcode_path, gcode_text)
        artifacts.append(gcode_path.name)
        if op == "gcode" and len(gcode_text.encode("utf-8")) > INLINE_GCODE_BYTES:
            result = _compact_gcode_result(result, gcode_text)
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    _write_text(result_path, payload)
    return {"ok": True, "op": op, "bytes": len(payload.encode("utf-8")), "warnings": warning_count(op, result),
            "artifacts": artifacts}


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 3:
        print("Usage : python -m laser_engine.cli <request.json> <result.json> <op>", file=sys.stderr)
        return 64
    request_path, result_path = Path(arguments[0]), Path(arguments[1])
    op = str(arguments[2]).strip().lower()
    try:
        _report(run(request_path, result_path, op))
        return 0
    except ToolpathError as error:
        _report({"ok": False, "error": str(error), "code": error.code, "status_code": error.status_code})
        return 2
    except OSError as error:
        _report({"ok": False, "error": f"Fichier de requête ou de résultat laser inaccessible : {error}",
                 "code": "REQUEST_IO", "status_code": 400})
        return 2
    except (TypeError, ValueError) as error:
        # Valeur non numérique (dpi, width_mm…) ou structure inattendue : la requête est en cause.
        _report({"ok": False, "error": f"Requête laser invalide : {error}", "code": "REQUEST_INVALID", "status_code": 400})
        return 2
    except Exception as error:  # noqa: BLE001 - le runner doit toujours recevoir un compte rendu
        traceback.print_exc()
        _report({"ok": False, "error": f"Erreur interne du moteur laser : {type(error).__name__}: {error}",
                 "code": "ENGINE_ERROR", "status_code": 500})
        return 1


if __name__ == "__main__":
    sys.exit(main())
