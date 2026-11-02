"""Exécution d'un outil Alpine Tools dans le Python isolé de son composant (agent 1.53.0).

    <python du composant> -m alpine_tools.cli <requete.json> <resultat.json>

La requête porte {"paquet", "params", "inputs": {clé: chemin}, "out_dir"}. Le compte rendu est écrit dans
<resultat.json> et rappelé sur la sortie standard (``RAPHAL_ATOOL_RESULT=<json>``) ; l'avancement par
``RAPHAL_ATOOL_PROGRESS=<fraction> <message>``. Une erreur de l'utilisateur (ValueError) donne ok=false et son message.
"""
import importlib
import json
import re
import sys
import traceback
from pathlib import Path

RESULT_MARKER = "RAPHAL_ATOOL_RESULT="
PROGRESS_MARKER = "RAPHAL_ATOOL_PROGRESS="
_NOM = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")


def charger(paquet):
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", str(paquet or "")):
        raise ValueError("Outil inconnu.")
    module = importlib.import_module(f"alpine_tools.{paquet}")
    if not isinstance(getattr(module, "TOOL", None), dict) or not callable(getattr(module, "run", None)):
        raise ValueError("Outil incomplet sur ce Worker : mets le Worker à jour.")
    return module


def probe(paquet):
    """Sonde de l'installation : l'outil se charge et son schéma est valable."""
    import numpy
    import PIL
    from alpine_tools import schema
    module = charger(paquet)
    schema.valider(module.TOOL, {})
    return {"ok": True, "tool": module.TOOL.get("id"), "numpy": numpy.__version__, "pillow": PIL.__version__}


def executer(requete):
    from alpine_tools import schema
    module = charger(requete.get("paquet"))
    params = schema.valider(module.TOOL, requete.get("params"))
    out_dir = Path(requete["out_dir"]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    inputs = {}
    for spec in module.TOOL.get("inputs", []):
        chemin = (requete.get("inputs") or {}).get(spec["key"])
        if chemin:
            inputs[spec["key"]] = Path(chemin)
        elif spec.get("required"):
            raise ValueError(f"Fichier « {spec.get('label') or spec['key']} » manquant.")

    def progress(fraction, message=""):
        try:
            f = max(0.0, min(1.0, float(fraction)))
        except (TypeError, ValueError):
            f = 0.0
        print(f"{PROGRESS_MARKER}{f:.3f} {str(message)[:200]}", flush=True)

    resultat = module.run(params, inputs, out_dir, progress) or {}
    fichiers = []
    for item in resultat.get("files") or []:
        nom = str(item.get("name") or "")
        if not _NOM.match(nom) or not (out_dir / nom).is_file():
            raise RuntimeError(f"Résultat annoncé introuvable : {nom!r}.")
        fichiers.append({"name": nom, "label": str(item.get("label") or nom)[:80],
                         "library": item.get("library") if item.get("library") in ("impression", "laser", "cnc") else None,
                         "bytes": (out_dir / nom).stat().st_size})
    apercu = resultat.get("preview")
    if apercu and (not _NOM.match(str(apercu)) or not (out_dir / str(apercu)).is_file()):
        apercu = None
    return {"ok": True, "tool": module.TOOL.get("id"), "files": fichiers, "preview": apercu,
            "details": [str(line)[:300] for line in (resultat.get("details") or [])][:60],
            "summary": resultat.get("summary") if isinstance(resultat.get("summary"), dict) else {}}


def main(argv):
    requete_path, resultat_path = Path(argv[1]), Path(argv[2])
    try:
        compte = executer(json.loads(requete_path.read_text(encoding="utf-8")))
    except ValueError as error:
        compte = {"ok": False, "error": str(error)[:600], "code": "TOOL_INPUT_REFUSED"}
    except Exception as error:  # noqa: BLE001 - message lisible pour le site, trace en sortie d'erreur
        traceback.print_exc()
        compte = {"ok": False, "error": f"Erreur de l'outil : {type(error).__name__}: {error}"[:600], "code": "TOOL_FAILED"}
    resultat_path.write_text(json.dumps(compte, ensure_ascii=False), encoding="utf-8")
    print(RESULT_MARKER + json.dumps({k: compte.get(k) for k in ("ok", "error", "code")}, ensure_ascii=False), flush=True)
    return 0 if compte.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
