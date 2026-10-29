# -*- coding: utf-8 -*-
"""Exécution d'une opération d'Alpine Toile Creator sur le Worker.

``python -m toile_engine.cli <request.json> <result.json>`` (cwd = dossier du Worker) lit la requête écrite par le
runner (runners/toile-engine.py) :

- ``{"op": "generer", "carte_path": <carte.png>, "parametres": {...}}`` → ``toile.3mf`` et ``apercu.png`` à côté de
  ``result.json`` ``{ok, op, dimensions_mm, triangles, pieces, grammes_total, avertissements}`` ;
- ``{"op": "detourer", "image_path": <image>, "modele_path": <model.onnx>}`` → ``masque.png`` et ``result.json``
  ``{ok, op, largeur, hauteur, part_premier_plan}``.

Paramètres et bornes : docs/ALPINE_TOILE_CREATOR.md (validés ici, jamais supposés valides). Dernière ligne de la
sortie standard : ``RAPHAL_TOILE_RESULT=<json>`` — ``{"ok": true, "op", "artifacts", ...}`` ou
``{"ok": false, "error", "code", "status_code"}``. Codes de sortie : 0 succès, 2 requête ou fichier invalide,
1 erreur interne. Les bibliothèques (numpy, Pillow, ONNX Runtime) ne sont importées que par
l'opération qui en a besoin : le détourage tourne dans le Python isolé de son module.
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any

from . import ToileEngineError

RESULT_MARKER = "RAPHAL_TOILE_RESULT="
OPERATIONS = ("generer", "detourer")
ARTEFACT_3MF = "toile.3mf"
ARTEFACT_APERCU = "apercu.png"
ARTEFACT_MASQUE = "masque.png"
MAX_REQUEST_BYTES = 1024 * 1024
MAX_CARTE_OCTETS = 64 * 1024 * 1024
MAX_COTE_CARTE = 4000
COTE_APERCU = 1024
COTE_VIGNETTE = 256
DENSITE_PLA = 1.24          # g/cm³, indicatif
VIDE = 255
BORNES = {"largeur_mm": (5.0, 400.0), "hauteur_mm": (5.0, 400.0), "pixel_mm": (0.08, 1.0),
          "epaisseur_base_mm": (0.4, 10.0), "epaisseur_image_mm": (0.08, 3.0)}
LIBELLES = {"largeur_mm": "Largeur", "hauteur_mm": "Hauteur", "pixel_mm": "Finesse (taille du pixel)",
            "epaisseur_base_mm": "Épaisseur de la plaque de base", "epaisseur_image_mm": "Épaisseur de la couche image"}
MAX_COULEURS = 16
MAX_NOM_COULEUR = 40
MAX_NOM = 120
HEX = re.compile(r"#[0-9A-Fa-f]{6}")
CONTROLE = re.compile(r"[\x00-\x1f\x7f]")


def _invalide(message):
    return ToileEngineError(message, 400, "PARAMETRES_INVALIDES")


def _nombre(parametres, cle):
    valeur = parametres.get(cle)
    minimum, maximum = BORNES[cle]
    if isinstance(valeur, bool) or not isinstance(valeur, (int, float)) or not math.isfinite(float(valeur)):
        raise _invalide(f"{LIBELLES[cle]} : nombre attendu ({cle}).")
    if not minimum <= float(valeur) <= maximum:
        raise _invalide(f"{LIBELLES[cle]} hors bornes : {minimum:g} à {maximum:g} mm ({cle} = {valeur}).")
    return float(valeur)


def _texte(valeur, maximum, defaut, libelle):
    if valeur is None:
        return defaut
    if not isinstance(valeur, str):
        raise _invalide(f"{libelle} : texte attendu.")
    valeur = CONTROLE.sub("", valeur).strip()
    if len(valeur) > maximum:
        raise _invalide(f"{libelle} trop long ({maximum} caractères au plus).")
    return valeur or defaut


def valider_generer(parametres: Any) -> dict[str, Any]:
    """Paramètres d'une génération, revérifiés à la lettre du contrat (bornes, couleurs, couleur de base)."""
    if not isinstance(parametres, dict):
        raise _invalide("Paramètres de la toile : objet JSON attendu.")
    valeurs = {cle: _nombre(parametres, cle) for cle in BORNES}
    couleurs = parametres.get("couleurs")
    if not isinstance(couleurs, list) or not 1 <= len(couleurs) <= MAX_COULEURS:
        raise _invalide(f"Couleurs : 1 à {MAX_COULEURS} couleurs attendues.")
    propres = []
    for index, couleur in enumerate(couleurs):
        if not isinstance(couleur, dict) or not isinstance(couleur.get("hex"), str) or not HEX.fullmatch(couleur["hex"]):
            raise _invalide(f"Couleur {index + 1} : code #RRGGBB attendu.")
        nom = _texte(couleur.get("nom"), MAX_NOM_COULEUR, f"Couleur {index + 1}", f"Nom de la couleur {index + 1}")
        propres.append({"hex": couleur["hex"].upper(), "nom": nom})
    base = parametres.get("couleur_base")
    if isinstance(base, bool) or not isinstance(base, int) or not 0 <= base < len(propres):
        raise _invalide("Couleur de la plaque de base : index d'une des couleurs attendu (couleur_base).")
    face = parametres.get("face_dessous", False)
    if not isinstance(face, bool):
        raise _invalide("Face contre le plateau : vrai ou faux attendu (face_dessous).")
    nom = _texte(parametres.get("nom"), MAX_NOM, "Toile", "Nom de la toile")
    return {**valeurs, "couleurs": propres, "couleur_base": base, "face_dessous": face, "nom": nom}


def lire_carte(chemin, parametres):
    """Carte des couleurs : PNG 8 bits mode L, taille attendue, valeurs 0..N-1 ou 255."""
    import numpy as np
    from PIL import Image

    chemin = Path(str(chemin))
    if not chemin.is_file():
        raise ToileEngineError("Carte des couleurs introuvable sur le Worker.", 400, "CARTE_ABSENTE")
    if chemin.stat().st_size > MAX_CARTE_OCTETS:
        raise ToileEngineError("Carte des couleurs trop volumineuse.", 413, "CARTE_TROP_GRANDE")
    try:
        with Image.open(chemin) as image:
            if image.format != "PNG" or image.mode != "L":
                raise ToileEngineError("Carte des couleurs : PNG 8 bits en niveaux de gris (mode L) attendu.", 400, "CARTE_INVALIDE")
            largeur, hauteur = image.size
            if not (1 <= largeur <= MAX_COTE_CARTE and 1 <= hauteur <= MAX_COTE_CARTE):
                raise ToileEngineError(f"Carte des couleurs trop grande ({MAX_COTE_CARTE} × {MAX_COTE_CARTE} px au plus).", 413, "CARTE_TROP_GRANDE")
            carte = np.array(image, dtype=np.uint8)
    except ToileEngineError:
        raise
    except (OSError, ValueError) as error:
        raise ToileEngineError(f"Carte des couleurs illisible : {error}", 400, "CARTE_INVALIDE") from None
    attendu = (round(parametres["largeur_mm"] / parametres["pixel_mm"]), round(parametres["hauteur_mm"] / parametres["pixel_mm"]))
    # Tolérance d'un pixel : arrondi du navigateur sur les dimensions recadrées.
    if abs(largeur - attendu[0]) > 1 or abs(hauteur - attendu[1]) > 1:
        raise ToileEngineError(f"Carte de {largeur} × {hauteur} px : {attendu[0]} × {attendu[1]} px attendus pour "
                               f"{parametres['largeur_mm']:g} × {parametres['hauteur_mm']:g} mm au pas de "
                               f"{parametres['pixel_mm']:g} mm.", 400, "CARTE_TAILLE")
    valeurs = np.unique(carte)
    hors = [int(valeur) for valeur in valeurs if valeur != VIDE and valeur >= len(parametres["couleurs"])]
    if hors:
        raise ToileEngineError(f"Carte des couleurs : index {hors[0]} sans couleur correspondante "
                               f"({len(parametres['couleurs'])} couleur(s) déclarée(s)).", 400, "CARTE_INVALIDE")
    return carte


def _png(image) -> bytes:
    sortie = io.BytesIO()
    image.save(sortie, format="PNG", optimize=True)
    return sortie.getvalue()


def apercu(carte, couleurs, cote=COTE_APERCU):
    """Image RGBA des couleurs (vide transparent), côté max ``cote`` px, plus proche voisin."""
    import numpy as np
    from PIL import Image

    palette = np.zeros((256, 4), np.uint8)
    for index, couleur in enumerate(couleurs):
        valeur = couleur["hex"].lstrip("#")
        palette[index] = (int(valeur[0:2], 16), int(valeur[2:4], 16), int(valeur[4:6], 16), 255)
    image = Image.fromarray(palette[carte])  # (H, W, 4) uint8 → RGBA
    hauteur, largeur = carte.shape
    echelle = min(1.0, cote / max(largeur, hauteur))
    if echelle < 1.0:
        image = image.resize((max(1, round(largeur * echelle)), max(1, round(hauteur * echelle))), Image.NEAREST)
    return image


def _arrondi(valeur, chiffres=2):
    return round(float(valeur), chiffres)


def generer(data, result_path):
    from . import mesh, troismf

    parametres = valider_generer(data.get("parametres"))
    carte = lire_carte(data.get("carte_path") or "", parametres)
    toile = mesh.construire(carte, parametres["largeur_mm"], parametres["hauteur_mm"], parametres["epaisseur_base_mm"],
                            parametres["epaisseur_image_mm"], parametres["couleurs"], parametres["couleur_base"],
                            parametres["face_dessous"])
    image = apercu(carte, parametres["couleurs"])
    vignette = image.copy()
    vignette.thumbnail((COTE_VIGNETTE, COTE_VIGNETTE))
    dossier = result_path.parent
    _ecrire_octets(dossier / ARTEFACT_APERCU, _png(image))
    temporaire = dossier / (ARTEFACT_3MF + "." + uuid.uuid4().hex + ".tmp")
    try:
        troismf.ecrire(temporaire, toile, parametres["couleurs"], parametres["nom"], _png(vignette))
        os.replace(temporaire, dossier / ARTEFACT_3MF)
    finally:
        temporaire.unlink(missing_ok=True)
    import numpy as np

    lignes, colonnes = np.nonzero(carte != VIDE)
    surface_pixel = toile.pas_x * toile.pas_y
    pieces, total = [], 0.0
    for piece in toile.pieces:
        volume = piece.pixels * surface_pixel * piece.epaisseur
        grammes = volume / 1000.0 * DENSITE_PLA
        total += grammes
        fiche = {"nom": piece.nom, "couleur": piece.couleur, "hex": piece.hex}
        if not piece.plaque:
            fiche["surface_mm2"] = _arrondi(piece.pixels * surface_pixel)
        fiche.update(volume_mm3=_arrondi(volume), grammes=_arrondi(grammes))
        pieces.append(fiche)
    image_mm = parametres["epaisseur_image_mm"] if len(toile.pieces) > 1 else 0.0
    result = {"ok": True, "op": "generer",
              "dimensions_mm": {"x": _arrondi((colonnes.max() + 1 - colonnes.min()) * toile.pas_x),
                                "y": _arrondi((lignes.max() + 1 - lignes.min()) * toile.pas_y),
                                "z": _arrondi(parametres["epaisseur_base_mm"] + image_mm)},
              "triangles": toile.triangles, "pieces": pieces, "grammes_total": _arrondi(total),
              "avertissements": list(toile.avertissements)}
    return result, [ARTEFACT_3MF, ARTEFACT_APERCU]


def detourer(data, result_path):
    from . import detourage

    modele = data.get("modele_path")
    if not modele or not Path(str(modele)).is_file():
        raise ToileEngineError("Le module de détourage n'est pas installé sur ce Worker : installe-le depuis "
                               "Mes Workers → Installations.", 409, "MODULE_MISSING")
    session = detourage.ouvrir_session(modele)
    mesures = detourage.detourer(data.get("image_path") or "", result_path.parent / ARTEFACT_MASQUE, session)
    return {"ok": True, "op": "detourer", **mesures}, [ARTEFACT_MASQUE]


def execute(op, data, result_path):
    if op not in OPERATIONS:
        raise ToileEngineError(f"Opération inconnue « {op} » (attendu : {', '.join(OPERATIONS)}).", 400, "UNKNOWN_OPERATION")
    if not isinstance(data, dict):
        raise ToileEngineError("Requête : objet JSON attendu.", 400, "REQUEST_INVALID")
    return (generer if op == "generer" else detourer)(data, Path(result_path))


def probe() -> dict[str, Any]:
    """Sonde d'installation du moteur : une petite carte (2 couleurs, un trou) maillée et écrite en 3MF en mémoire."""
    import numpy as np
    import PIL

    from . import mesh, troismf

    carte = np.full((6, 6), 1, np.uint8)
    carte[1:5, 1:5] = 0
    carte[2:4, 2:4] = VIDE
    couleurs = [{"hex": "#111111", "nom": "Noir"}, {"hex": "#F5F5F5", "nom": "Blanc"}]
    toile = mesh.construire(carte, 6.0, 6.0, 1.0, 0.5, couleurs, 1)
    sortie = io.BytesIO()
    objets = troismf.ecrire(sortie, toile, couleurs, "Sonde")
    return {"ok": objets == 3 and sortie.tell() > 0, "pieces": objets, "triangles": toile.triangles,
            "numpy": np.__version__, "pillow": PIL.__version__}


def probe_detourage(modele) -> dict[str, Any]:
    """Sonde du module : le modèle s'ouvre sur le CPU et rend un masque de la bonne taille."""
    import numpy as np
    import onnxruntime

    from . import detourage

    session = detourage.ouvrir_session(modele)
    entree = session.get_inputs()[0]
    sortie = session.run(None, {entree.name: np.zeros((1, 3, detourage.TAILLE_MODELE, detourage.TAILLE_MODELE), np.float32)})[0]
    return {"ok": int(np.asarray(sortie).size) == detourage.TAILLE_MODELE ** 2, "onnxruntime": onnxruntime.__version__,
            "output": [int(value) for value in np.asarray(sortie).shape]}


def _ecrire_octets(path: Path, contenu: bytes) -> None:
    """Écriture atomique : un artefact partiel n'est jamais pris pour un artefact complet."""
    temporaire = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporaire.open("xb") as sortie:
            sortie.write(contenu)
            sortie.flush()
            os.fsync(sortie.fileno())
        os.replace(temporaire, path)
    finally:
        temporaire.unlink(missing_ok=True)


def _report(payload: dict[str, Any]) -> None:
    print(RESULT_MARKER + json.dumps(payload, ensure_ascii=False), flush=True)


def run(request_path: Path, result_path: Path) -> dict[str, Any]:
    """Lit la requête, exécute l'opération et écrit les artefacts ; renvoie le compte rendu (sans l'imprimer)."""
    if request_path.stat().st_size > MAX_REQUEST_BYTES:
        raise ToileEngineError("Requête trop volumineuse (1 Mo maximum).", 413, "REQUEST_TOO_LARGE")
    try:
        data = json.loads(request_path.read_text(encoding="utf-8-sig"))
    except ValueError:
        raise ToileEngineError("Requête illisible (JSON invalide).", 400, "REQUEST_INVALID") from None
    if not isinstance(data, dict):
        raise ToileEngineError("Requête : objet JSON attendu.", 400, "REQUEST_INVALID")
    op = str(data.get("op") or "").strip().lower()
    result, artefacts = execute(op, data, result_path)
    contenu = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _ecrire_octets(result_path, contenu)
    compte_rendu = {"ok": True, "op": op, "bytes": len(contenu), "artifacts": [result_path.name] + artefacts,
                    "avertissements": len(result.get("avertissements") or [])}
    if op == "generer":
        compte_rendu.update(triangles=result["triangles"], pieces=len(result["pieces"]), grammes_total=result["grammes_total"])
    else:
        compte_rendu.update(part_premier_plan=result["part_premier_plan"])
    return compte_rendu


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:
        print("Usage : python -m toile_engine.cli <request.json> <result.json>", file=sys.stderr)
        return 64
    request_path, result_path = Path(arguments[0]), Path(arguments[1])
    try:
        _report(run(request_path, result_path))
        return 0
    except ToileEngineError as error:
        _report({"ok": False, "error": str(error), "code": error.code, "status_code": error.status_code})
        return 2
    except ImportError as error:
        _report({"ok": False, "error": f"Bibliothèque du moteur de toile absente ({error.name or error}) : répare le "
                 "composant dans Mes Workers → Installations.", "code": "ENGINE_INCOMPLETE", "status_code": 409})
        return 1
    except MemoryError:
        _report({"ok": False, "error": "Mémoire insuffisante sur le Worker pour cette toile : réduis la taille ou la finesse.",
                 "code": "MEMOIRE_INSUFFISANTE", "status_code": 507})
        return 1
    except OSError as error:
        _report({"ok": False, "error": f"Fichier de requête ou de résultat inaccessible : {error}",
                 "code": "REQUEST_IO", "status_code": 400})
        return 2
    except (TypeError, ValueError) as error:
        _report({"ok": False, "error": f"Requête invalide : {error}", "code": "REQUEST_INVALID", "status_code": 400})
        return 2
    except Exception as error:  # noqa: BLE001 - le runner doit toujours recevoir un compte rendu
        traceback.print_exc()
        _report({"ok": False, "error": f"Erreur interne du moteur de toile : {type(error).__name__}: {error}",
                 "code": "ENGINE_ERROR", "status_code": 500})
        return 1


if __name__ == "__main__":
    sys.exit(main())
