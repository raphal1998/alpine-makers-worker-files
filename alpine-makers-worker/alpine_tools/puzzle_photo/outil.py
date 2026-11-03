# -*- coding: utf-8 -*-
"""Exécution de l'outil Puzzle photo (écran PuzzleUi.kt d'Alpine Gift) : réglages revérifiés, découpe, puis sortie
laser (projet Alpine Laser Studio, SVG LightBurn, PNG) ou impression 3D (3MF, STL, OBJ) et aperçu."""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from . import TOOL
from . import decoupe as D
from . import gravure as gv
from . import image as img
from . import impression as I
from . import kotlin as K
from . import maillage as M
from . import police

F32 = np.float32
# FormatCadre de l'application : proportions en Float (4f / 3f…), reprises telles quelles.
RATIOS = {"4:3": float(F32(4) / F32(3)), "3:4": float(F32(3) / F32(4)), "1:1": 1.0, "16:9": float(F32(16) / F32(9))}
# FILAMENTS de l'application (nom de la teinte dans le 3MF quand la couleur choisie en fait partie).
FILAMENTS = {"#F2EEE8": "Blanc", "#26232A": "Noir", "#F27A98": "Rose", "#D8333F": "Rouge", "#E2B54F": "Or",
             "#2F6FD6": "Bleu", "#2FA36B": "Vert", "#B98552": "Bois"}
# FormatModele : libellé, suffixe du fichier.
FORMATS = {"3mf": ("3MF multicolore", ".3mf"), "stl_couleurs": ("STL par couleur (ZIP)", "-stl.zip"),
           "stl_unique": ("STL unique", ".stl"), "obj": ("OBJ avec couleurs (ZIP)", "-obj.zip")}
APPLICATION = "Alpine Makers — Puzzle photo"
RELIEF_PRENOM_MM, BORD_PLATEAU_MM, FOND_PLATEAU_MM = 0.8, 5.0, 1.2      # ParamPuzzle3D par défaut
CONSEILS_3D = [
    "Pièces à plat sur le plateau, sans support ; PLA, 0,2 mm, 2 parois.",
    "Jeu de 0,15 mm par côté : à augmenter (0,2–0,25) si les pièces s'emboîtent trop serré.",
    "Le prénom en relief peut prendre une autre couleur (AMS, ou changement de filament à sa hauteur).",
]

_SPECS = {s["key"]: s for s in TOOL["params"]}


def _booleen(v):
    return v.strip().lower() in ("1", "true", "oui", "on", "vrai") if isinstance(v, str) else bool(v)


def lire_params(params):
    """Réglages revérifiés (le cadre les a déjà validés) et arrondis comme les curseurs de l'application."""
    p = {k: s["default"] for k, s in _SPECS.items()}
    p.update({k: v for k, v in (params or {}).items() if v is not None})
    inconnues = sorted(set(p) - set(_SPECS))
    if inconnues:
        raise ValueError("Paramètre inconnu : " + ", ".join(inconnues) + ".")
    for cle, s in _SPECS.items():
        t = s["type"]
        if t == "choice":
            if str(p[cle]) not in [o["value"] for o in s["options"]]:
                raise ValueError(f"« {s['label']} » : choix inconnu.")
            p[cle] = str(p[cle])
        elif t == "number":
            try:
                v = float(p[cle])
            except (TypeError, ValueError):
                raise ValueError(f"« {s['label']} » : nombre attendu.") from None
            if not (v == v and s["min"] <= v <= s["max"]):
                bornes = [str(b).replace(".", ",") for b in (s["min"], s["max"])]
                raise ValueError(f"« {s['label']} » : entre {bornes[0]} et {bornes[1]}.")
            p[cle] = v
        elif t == "bool":
            p[cle] = _booleen(p[cle])
        elif t == "text":
            texte = str(p[cle] or "")
            if len(texte) > s["max"] or any(ord(ch) < 32 for ch in texte):
                raise ValueError(f"« {s['label']} » : {s['max']} caractères au plus, sans caractère de contrôle.")
            p[cle] = texte
        elif t == "color":
            v = str(p[cle] or "").strip().upper()
            if len(v) != 7 or v[0] != "#" or any(ch not in "0123456789ABCDEF" for ch in v[1:]):
                raise ValueError(f"« {s['label']} » : couleur #RRVVBB attendue.")
            p[cle] = v
    # Pas des curseurs de PuzzleUi.kt
    p["largeur_mm"] = K.arrondi(p["largeur_mm"] / 5) * 5.0
    p["alea"] = K.arrondi(p["irregularite"] / 5) / 20.0
    p["epaisseur_mm"] = K.arrondi(p["epaisseur_mm"] * 2) / 2.0
    p["jeu_mm"] = K.arrondi(p["jeu_mm"] * 100) / 100.0
    for cle in ("colonnes", "lignes", "graine", "colonne_prenom", "ligne_prenom"):
        p[cle] = K.arrondi(p[cle])
    return p


def nom_sur(titre):
    """IndexCreations.nomSur : sans accents, [a-z0-9-], 40 caractères au plus."""
    import re
    import unicodedata
    sans = "".join(ch for ch in unicodedata.normalize("NFD", titre) if not unicodedata.combining(ch))
    return (re.sub(r"[^A-Za-z0-9]+", "-", sans).strip("-")[:40].strip("-") or "puzzle").lower()


def param_puzzle(p):
    """ParamPuzzle de l'écran : hauteur = largeur / proportion arrondie au mm, colonnes et lignes si choisies."""
    largeur = p["largeur_mm"]
    return {"largeur_mm": largeur, "hauteur_mm": float(K.arrondi(largeur / RATIOS[p["format"]])), "age": p["age"],
            "colonnes": max(2, p["colonnes"]) if p["pieces_choisies"] else 0,
            "lignes": max(2, p["lignes"]) if p["pieces_choisies"] else 0,
            "graine": p["graine"], "alea": p["alea"], "prenom": p["prenom"],
            "colonne_prenom": p["colonne_prenom"] - 1, "ligne_prenom": p["ligne_prenom"] - 1}


def gravure_puzzle(d, photo, px_par_mm, reglages, methode, progress=None):
    """Puzzle.gravure : photo à la taille du puzzle (étirée à sa proportion), prénom sur sa plaque claire, réglages
    et tramage de la gravure photo."""
    l = max(1, K.arrondi(d.largeur_mm * px_par_mm))
    h = max(1, K.arrondi(d.hauteur_mm * px_par_mm))
    if l * h > gv.PIXELS_MAX:
        raise ValueError(f"Image trop grande ({l} × {h} px) : réduis la taille ou la résolution.")
    image = img.redimensionner(photo, l, h)
    if d.prenom:
        D.inscrire(d, image, px_par_mm)
    return gv.preparer(image, d.largeur_mm, px_par_mm, reglages, methode, progress)


def details_puzzle(d):
    libelle = D.AGES[d.p["age"]][0]
    r = [f"{K.mm(d.largeur_mm)} × {K.mm(d.hauteur_mm)} · {d.colonnes * d.lignes} pièces ({d.colonnes} × {d.lignes}) · {libelle}"]
    prenom = (d.p.get("prenom") or "").strip()
    if prenom:
        r.append(f"Pièce prénom : {prenom} (police Alpine Ronde)")
    return r + list(d.avertissements)


def _rvb(hex_):
    return tuple(int(hex_[k:k + 2], 16) for k in (1, 3, 5))


def _tracer(image, d, couleur, largeur):
    """Tracés des lignes communes (cadre compris) sur l'aperçu."""
    s = image.width / d.largeur_mm
    dessin = ImageDraw.Draw(image)
    for t in d.traces("lignes"):
        pts = [(x * s, (d.hauteur_mm - y) * s) for x, y in zip(t.x, t.y)]
        if t.ferme:
            pts.append(pts[0])
        dessin.line(pts, fill=couleur, width=largeur, joint="curve")
    return image


def apercu_impression(d, photo, cadre, couleur_prenom):
    """rendrePuzzle (impression 3D) : photo en gris, tracés blancs, plaque blanche et prénom à sa couleur."""
    l = 900
    h = max(1, K.arrondi(l * d.hauteur_mm / d.largeur_mm))
    g = img.redimensionner(img.gris(photo, cadre, l, h), l, h)
    v = np.clip(np.floor(g.astype(np.float64) * 255 + 0.5), 0, 255).astype(np.uint8)
    rgb = np.repeat(v[..., None], 3, axis=2)
    if d.prenom:
        m = D.masques_prenom(d, l, h, l / d.largeur_mm)
        if m is not None:
            sy, sx, plaque, lettres = m
            zone = rgb[sy, sx]
            zone[plaque & ~lettres] = (255, 255, 255)
            zone[plaque & lettres] = _rvb(couleur_prenom)
    return _tracer(Image.fromarray(rgb, "RGB"), d, (255, 255, 255), 2)


def executer(params, inputs, out_dir, progress):
    p = lire_params(params)
    chemin_photo = (inputs or {}).get("photo")
    if not chemin_photo:
        raise ValueError("Photo manquante.")
    out_dir = Path(out_dir)
    prm = param_puzzle(p)
    progress(0.02, "Découpe des pièces")
    d = D.decouper(prm, police if prm["prenom"].strip() else None)
    progress(0.05, "Lecture de la photo")
    photo = img.charger_photo(Path(chemin_photo))
    cadre = img.cadre_centre(photo.width, photo.height, RATIOS[p["format"]])
    titre = p["nom"].strip() or (f"Puzzle de {prm['prenom'].strip()}" if prm["prenom"].strip() else "Puzzle photo")
    base = nom_sur(titre)
    if p["sortie"] == "laser":
        return _laser(p, d, photo, cadre, titre, base, out_dir, progress)
    return _impression(p, d, photo, cadre, titre, base, out_dir, progress)


def _laser(p, d, photo, cadre, titre, base, out_dir, progress):
    res = float(p["resolution"])
    mat = p["materiau"]
    m = gv.MATERIAUX[mat]
    methode = m["methode"]
    mode = p["mode"]
    l_px, h_px = K.arrondi(d.largeur_mm * res), K.arrondi(d.hauteur_mm * res)
    if l_px * h_px > gv.PIXELS_MAX:
        raise ValueError(f"Image trop grande ({l_px} × {h_px} px) : réduis la taille ou la résolution.")
    g = img.gris(photo, cadre, l_px, h_px)
    progress(0.1, "Gravure de la photo")
    r = gravure_puzzle(d, g, res, gv.reglages_materiau(mat), methode, lambda f: progress(0.1 + 0.65 * f, "Tramage"))
    progress(0.8, "PNG, SVG et projet Laser Studio")
    png = gv.png_gris(r["pixels"], res)
    traces = d.traces(mode)
    sx, sy = r["largeur_mm"] / d.largeur_mm, r["hauteur_mm"] / d.hauteur_mm
    decoupes = [[(t.x[k] * sx, (d.hauteur_mm - t.y[k]) * sy) for k in range(t.n)] for t in traces]
    texte = gv.projet(titre, r, png, m["libelle"], methode=methode, nom_calque=f"Photo du puzzle {m['libelle']} (Alpine Tools)",
                      decoupes=decoupes, fermes=[t.ferme for t in traces])
    dpi = gv.ppp(res)
    noms = (f"{base}.alsproj", f"{base}-laser.svg", f"{base}-{dpi}ppp.png")
    # Octets UTF-8 tels quels (write_text passerait le SVG en CRLF sous Windows : il ne serait plus celui de Gift).
    (out_dir / noms[0]).write_bytes(texte.encode("utf-8"))
    (out_dir / noms[1]).write_bytes(D.svg(d, mode, titre, png).encode("utf-8"))
    (out_dir / noms[2]).write_bytes(png)
    progress(0.92, "Aperçu")
    _tracer(gv.apercu_trame(r["pixels"], mat), d, (0xE0, 0x24, 0x2C), 2).save(out_dir / "apercu.png", "PNG", optimize=True)
    longueur = d.longueur(mode)
    details = details_puzzle(d) + [
        f"{r['largeur_px']} × {r['hauteur_px']} px · {K.fr(res, 1)} px/mm ({dpi} ppp) · {m['libelle']} · {D.MODES[mode]}",
        f"Découpe : {K.entier(K.arrondi(longueur))} mm de tracés (calque rouge).",
        "Projet Alpine Laser Studio : photo tramée sur le calque Image (Relais) et découpe sur le calque rouge, à la "
        "taille réelle ; SVG pour LightBurn (calque gravure noir, calque découpe rouge).",
    ] + D.conseils(mode) + [m["conseil"]]
    progress(1.0, "Puzzle prêt")
    return {
        "files": [{"name": noms[0], "label": "Projet Alpine Laser Studio", "library": "laser"},
                  {"name": noms[1], "label": "SVG laser (LightBurn)", "library": "laser"},
                  {"name": noms[2], "label": f"Image seule (PNG {dpi} ppp)", "library": "laser"}],
        "preview": "apercu.png",
        "details": details,
        "summary": {"sortie": "laser", "largeur_mm": d.largeur_mm, "hauteur_mm": d.hauteur_mm, "colonnes": d.colonnes,
                    "lignes": d.lignes, "pieces": len(d.pieces), "graine": p["graine"], "alea": p["alea"],
                    "prenom": bool(d.prenom), "mode": mode, "materiau": mat, "methode": methode,
                    "longueur_decoupe_mm": round(longueur, 1), "traces": len(traces), "largeur_px": r["largeur_px"],
                    "hauteur_px": r["hauteur_px"], "px_par_mm": res, "ppp": dpi,
                    "avertissements": len(d.avertissements)},
    }


def _impression(p, d, photo, cadre, titre, base, out_dir, progress):
    p3 = {"epaisseur_mm": p["epaisseur_mm"], "jeu_mm": p["jeu_mm"], "relief_prenom_mm": RELIEF_PRENOM_MM,
          "plateau": p["plateau"], "bord_plateau_mm": BORD_PLATEAU_MM, "fond_plateau_mm": FOND_PLATEAU_MM}
    progress(0.08, "Pièces imprimées")
    r = I.pieces3d(d, p3, lambda f, msg="": progress(0.08 + 0.72 * f, msg or "Pièces imprimées"))
    teintes = [(p["couleur_pieces"], FILAMENTS.get(p["couleur_pieces"], "Pièces")),
               (p["couleur_prenom"], FILAMENTS.get(p["couleur_prenom"], "Prénom"))]
    t = teintes if d.prenom else teintes[:1]

    def filament(nom):
        return min(I.Resultat3D.filament(nom), len(t) - 1)

    fmt = p["format_fichier"]
    libelle, suffixe = FORMATS[fmt]
    fichier = base + suffixe
    progress(0.82, f"Écriture du {libelle}")
    chemin = out_dir / fichier
    if fmt == "3mf":
        M.ecrire_3mf(chemin, titre, APPLICATION, t, [(nom, filament(nom), m) for nom, m in r.pieces])
    elif fmt == "stl_unique":
        M.ecrire_stl_unique(chemin, r.tout, titre)
    elif fmt == "stl_couleurs":
        M.ecrire_stl_pieces(chemin, titre, [(nom, t[filament(nom)], m) for nom, m in r.pieces])
    else:
        M.ecrire_obj(chemin, titre, [(nom, t[filament(nom)], m) for nom, m in r.pieces])
    progress(0.94, "Aperçu")
    apercu_impression(d, photo, cadre, p["couleur_prenom"]).save(out_dir / "apercu.png", "PNG", optimize=True)
    ferme = r.tout.controle()["ferme"]
    deja = set(d.avertissements)
    details = details_puzzle(d) + [
        f"Pièces de {K.mm(p['epaisseur_mm'])} · jeu {K.mm2(p['jeu_mm'])} par côté"
        + (" · plateau de rangement" if p["plateau"] else ""),
        f"{K.entier(r.triangles)} triangles (une pièce fermée par corps)"
        + ("" if ferme else " — maillage NON fermé"),
    ] + [a for a in r.avertissements if a not in deja]
    if fmt != "3mf":
        details.append(f"Format : {libelle}")
    details += CONSEILS_3D + ["La photo sert d'aperçu : les pièces imprimées sont unies, prénom en relief "
                              f"de {K.mm(RELIEF_PRENOM_MM)}." if d.prenom else
                              "La photo sert d'aperçu : les pièces imprimées sont unies."]
    progress(1.0, "Puzzle prêt")
    return {
        "files": [{"name": fichier, "label": libelle, "library": "impression"}],
        "preview": "apercu.png",
        "details": details,
        "summary": {"sortie": "impression", "largeur_mm": d.largeur_mm, "hauteur_mm": d.hauteur_mm,
                    "colonnes": d.colonnes, "lignes": d.lignes, "pieces": len(d.pieces), "graine": p["graine"],
                    "alea": p["alea"], "prenom": bool(d.prenom), "corps": len(r.pieces), "triangles": r.triangles,
                    "ferme": bool(ferme), "epaisseur_mm": p["epaisseur_mm"], "jeu_mm": p["jeu_mm"],
                    "plateau": bool(p["plateau"]), "format": fmt, "avertissements": len(r.avertissements)},
    }
