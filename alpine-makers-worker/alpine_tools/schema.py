"""Validation des paramètres d'un outil Alpine Tools selon son schéma TOOL["params"] (docs/ALPINE_TOOLS.md).

Bibliothèque standard seulement : utilisée par le site (avant l'envoi au Worker) et par le Worker (avant le calcul).
"""
import math
import re

TYPES = ("number", "choice", "bool", "text", "color", "colors")
_COULEUR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class ParamError(ValueError):
    """Paramètre refusé, message en français."""


def _num(spec, value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ParamError(f"« {spec.get('label') or spec['key']} » : nombre attendu.") from None
    if not math.isfinite(v):
        raise ParamError(f"« {spec.get('label') or spec['key']} » : nombre attendu.")
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and v < float(lo) or hi is not None and v > float(hi):
        raise ParamError(f"« {spec.get('label') or spec['key']} » : entre {lo} et {hi}.")
    if spec.get("integer") or (isinstance(spec.get("step"), int) and isinstance(spec.get("default"), int)):
        return int(round(v))
    return v


def _couleur(spec, value):
    v = str(value or "").strip()
    if not _COULEUR.match(v):
        raise ParamError(f"« {spec.get('label') or spec['key']} » : couleur #RRVVBB attendue.")
    return v.upper()


def valider(tool, raw):
    """Paramètres nettoyés (valeurs par défaut pour ce qui manque). Rejette les clés inconnues."""
    raw = raw if isinstance(raw, dict) else {}
    specs = {spec["key"]: spec for spec in tool.get("params", [])}
    inconnues = sorted(set(raw) - set(specs))
    if inconnues:
        raise ParamError("Paramètre inconnu : " + ", ".join(inconnues[:5]) + ".")
    out = {}
    for key, spec in specs.items():
        kind = spec.get("type")
        value = raw.get(key, spec.get("default"))
        if value is None:
            if spec.get("required"):
                raise ParamError(f"« {spec.get('label') or key} » est obligatoire.")
            out[key] = None
            continue
        if kind == "number":
            out[key] = _num(spec, value)
        elif kind == "choice":
            valeurs = [str(option["value"]) for option in spec.get("options", [])]
            if str(value) not in valeurs:
                raise ParamError(f"« {spec.get('label') or key} » : choix inconnu.")
            out[key] = next(option["value"] for option in spec["options"] if str(option["value"]) == str(value))
        elif kind == "bool":
            if isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "oui", "on", "vrai")
            out[key] = bool(value)
        elif kind == "text":
            texte = str(value)
            if len(texte) > int(spec.get("max") or 200):
                raise ParamError(f"« {spec.get('label') or key} » : {spec.get('max') or 200} caractères au plus.")
            if any(ord(ch) < 32 and ch not in "\n\t" for ch in texte):
                raise ParamError(f"« {spec.get('label') or key} » : caractère de contrôle refusé.")
            out[key] = texte
        elif kind == "color":
            out[key] = _couleur(spec, value)
        elif kind == "colors":
            if not isinstance(value, (list, tuple)):
                raise ParamError(f"« {spec.get('label') or key} » : liste de couleurs attendue.")
            couleurs = [_couleur(spec, v) for v in value]
            lo, hi = int(spec.get("min") or 1), int(spec.get("max") or 16)
            if not lo <= len(couleurs) <= hi:
                raise ParamError(f"« {spec.get('label') or key} » : de {lo} à {hi} couleurs.")
            out[key] = couleurs
        else:
            raise ParamError(f"Type de paramètre inconnu pour « {key} ».")
    return out


def public(tool):
    """Schéma publié au site et aux applications (sans fonction)."""
    return {key: tool.get(key) for key in ("id", "name", "machine", "description", "inputs", "params") if key in tool}
