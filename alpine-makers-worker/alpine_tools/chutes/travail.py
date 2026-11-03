"""Exécution d'un job Alpine Chutes sur le Worker : une chute (photo ou saisie) + des pièces → un plan de découpe.

Même chaîne que l'application (EtatCapture, PlanUi, ExportsUi) : détection de la feuille sur la photo réduite, aperçu à
3 px/mm, mesure à 8 px/mm avec le seuil de l'aperçu, pièces, placement automatique, exports.
"""
import math
import re
import time

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from . import exports as ex, import_dxf, import_svg, mesure as me, modele as md, rendu
from .detection import convexe
from .geometrie import Refus, decimal_fr, dimensions, mm2, nom_fichier, nombre, pourcent
from .image import FORMATS
from .placement import COINS, Placement, ReglagesPlacement

MAX_PIXELS = 24_000_000
MIN_COTE = 640
COTE_SOURCE = 3200
COTE_APERCU = 1600
MAX_OCTETS_DESSIN = 5 * 1024 * 1024
LIMITE_PLACEMENT_S = 150.0
MAX_POSES_RESUME = 500
FILS = {"aucun": None, "horizontal": 0.0, "vertical": 90.0}
LIBELLES_MODE = {"sur_feuille": "petite chute posée sur la feuille", "feuille_sur_chute": "grande chute, feuille posée dessus",
                 "saisie": "chute rectangulaire saisie à la main"}


def _num(params, cle, lo, hi, defaut):
    try:
        v = float(params.get(cle, defaut))
    except (TypeError, ValueError):
        raise ValueError(f"Réglage « {cle} » : nombre attendu.") from None
    if not math.isfinite(v) or v < lo or v > hi:
        raise ValueError(f"Réglage « {cle} » hors limites ({lo} à {hi}).")
    return v


def _choix(params, cle, valeurs, defaut):
    v = params.get(cle, defaut)
    if v not in valeurs:
        raise ValueError(f"Réglage « {cle} » inconnu.")
    return v


def _nombres(texte):
    return [float(m.replace(",", ".")) for m in texte.split()]


def charger_photo(chemin):
    """Photo orientée (EXIF), refusée au-delà de 24 Mpx ou sous 640 px de côté."""
    if chemin is None or not chemin.is_file():
        raise ValueError("Photo de la chute manquante : ajoute la photo, ou choisis « Chute rectangulaire saisie à la main ».")
    try:
        img = Image.open(chemin)
    except (UnidentifiedImageError, OSError, ValueError):
        raise ValueError("Ce fichier n'est pas une image lisible.") from None
    with img:
        l, h = img.size
        if l <= 0 or h <= 0:
            raise ValueError("Ce fichier n'est pas une image lisible.")
        if l * h > MAX_PIXELS:
            raise ValueError(f"Photo trop grande ({l} × {h} px, {decimal_fr(l * h / 1e6, 1)} Mpx) : 24 Mpx au plus.")
        if max(l, h) < MIN_COTE:
            raise ValueError(f"Photo trop petite ({l} × {h} px) : il faut au moins {MIN_COTE} px de côté.")
        try:
            return ImageOps.exif_transpose(img).convert("RGB")
        except (OSError, ValueError, Image.DecompressionBombError):
            raise ValueError("Photo illisible (fichier tronqué ou abîmé).") from None


def lire_coins(texte):
    """« x y; x y; x y; x y » en % de la photo (virgule décimale acceptée) → 4 fractions, ou None si vide."""
    t = (texte or "").strip()
    if not t:
        return None
    try:
        v = [float(m.replace(",", ".")) for m in re.split(r"[;\s]+", t) if m]
    except ValueError:
        raise ValueError("Coins : 8 nombres attendus (x y de chaque coin, en % de la photo).") from None
    if len(v) != 8 or any(not math.isfinite(x) or x < 0 or x > 100 for x in v):
        raise ValueError("Coins : 8 nombres entre 0 et 100 attendus (x y de chaque coin, en % de la photo).")
    return [(v[2 * k] / 100.0, v[2 * k + 1] / 100.0) for k in range(4)]


def lire_interdits(texte):
    """Zones interdites : « x y rayon » (disque) ou « x1 y1 x2 y2 rayon » (trait), séparées par « ; », en mm."""
    zones = []
    for morceau in re.split(r"[;\n]+", (texte or "").strip()):
        if not morceau.strip():
            continue
        try:
            v = _nombres(morceau)
        except ValueError:
            raise ValueError(f"Zone interdite « {morceau.strip()} » : nombres attendus (x y rayon, ou x1 y1 x2 y2 rayon).") from None
        if len(v) not in (3, 5) or any(not math.isfinite(x) or abs(x) > 5000 for x in v):
            raise ValueError(f"Zone interdite « {morceau.strip()} » : 3 nombres (x y rayon) ou 5 (x1 y1 x2 y2 rayon) attendus.")
        if not 1.0 <= v[-1] <= 25.0:
            raise ValueError(f"Zone interdite « {morceau.strip()} » : rayon de 1 à 25 mm.")
        pts = [(v[0], v[1])] if len(v) == 3 else [(v[0], v[1]), (v[2], v[3])]
        zones.append(md.Retouche(v[-1], pts))
        if len(zones) > 50:
            raise ValueError("50 zones interdites au plus.")
    return zones


_RECT = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*[x×*]\s*([0-9]+(?:\.[0-9]+)?)\s*(?:[x×*]\s*([0-9]+))?\s*$", re.IGNORECASE)


def lire_liste(texte):
    """Liste de rectangles « L x H » ou « L x H x quantité », séparés par « ; » → [(L, H, Q)]."""
    sortie = []
    for morceau in re.split(r"[;\n]+", (texte or "").strip()):
        if not morceau.strip():
            continue
        m = _RECT.match(morceau.replace(",", "."))
        if not m:
            raise ValueError(f"Liste de rectangles : « {morceau.strip()} » n'est pas de la forme 120 x 40 ou 120 x 40 x 2.")
        q = int(m.group(3)) if m.group(3) else 1
        if not 1 <= q <= 500:
            raise ValueError(f"Liste de rectangles, « {morceau.strip()} » : quantité de 1 à 500.")
        sortie.append((float(m.group(1)), float(m.group(2)), q, morceau.strip()))
        if len(sortie) > 30:
            raise ValueError("Liste de rectangles : 30 tailles au plus.")
    return sortie


def lire_dessin(chemin):
    """Fichier de pièces : UTF-8, ou Windows-1252 si l'UTF-8 n'est pas valide (vieux DXF) ; SVG ou DXF comme l'application."""
    octets = chemin.read_bytes()
    if not octets:
        raise ValueError("Fichier de pièces vide.")
    if len(octets) > MAX_OCTETS_DESSIN:
        raise ValueError("Fichier de pièces trop volumineux (5 Mo au plus).")
    try:
        texte = octets.decode("utf-8")
    except UnicodeDecodeError:
        texte = octets.decode("cp1252", errors="replace")
    texte = texte.removeprefix("﻿")
    ext = chemin.suffix.lower().lstrip(".")
    if ext == "svg" or texte.lstrip().startswith("<"):
        return import_svg.lire(texte), "SVG"
    if ext == "dxf" or "SECTION" in texte:
        return import_dxf.lire(texte), "DXF"
    raise ValueError("Format non reconnu : choisis un fichier .svg ou .dxf.")


def _nom_dessin(chemin, rang):
    stem = chemin.stem
    if re.search(r"[A-Za-zÀ-ÿ]", stem) and not stem.lower().startswith(("entree", "atool")):
        return stem[:60]
    return "Dessin importé" + ("" if rang == 1 else f" {rang}")


def construire_pieces(params, inputs, rotation_par_defaut, avertissements):
    pieces = []

    def ajouter(p, quantite, rotation):
        if quantite <= 0:
            return
        pieces.append(p.copie(id=len(pieces) + 1, quantite=quantite, rotation=rotation, ordre=len(pieces)))

    for rang, (cle, pref) in enumerate((("pieces", "fichier"), ("pieces2", "fichier2")), start=1):
        chemin = inputs.get(cle)
        q = int(round(_num(params, f"{pref}_quantite", 0, 500, 1)))
        if chemin is None or q == 0:
            continue
        dessin, sorte = lire_dessin(chemin)
        nom = _nom_dessin(chemin, rang)
        try:
            p = md.importee(nom, dessin, chemin.name, _num(params, f"{pref}_echelle", 0.02, 100, 1.0), bool(params.get(f"{pref}_miroir", False)))
        except Refus as e:
            raise ValueError(f"Fichier « {nom} » ({sorte}) : {e}") from None
        if dessin.ouvertes:
            avertissements.append(f"Fichier « {nom} » : {dessin.ouvertes} tracé(s) ouvert(s) ignoré(s) (seules les formes fermées se découpent).")
        for a in dessin.avertissements[:5]:
            avertissements.append(f"Fichier « {nom} » : {a}")
        ajouter(p, q, _choix(params, f"{pref}_rotation", md.ROTATIONS, "quart"))
    rot_rect = _choix(params, "rect_rotation", md.ROTATIONS, rotation_par_defaut)
    q = int(round(_num(params, "rect_quantite", 0, 500, 0)))
    if q:
        ajouter(md.rectangle("", _num(params, "rect_largeur", 0.5, 3000, 80), _num(params, "rect_hauteur", 0.5, 3000, 50),
                             _num(params, "rect_rayon", 0, 1500, 0), _num(params, "rect_trou", 0, 3000, 0)), q, rot_rect)
    for l, h, ql, texte in lire_liste(params.get("liste_rectangles")):
        try:
            ajouter(md.rectangle("", l, h), ql, rot_rect)
        except Refus as e:
            raise ValueError(f"Liste de rectangles, « {texte} » : {e}") from None
    q = int(round(_num(params, "cercle_quantite", 0, 500, 0)))
    if q:
        ajouter(md.cercle("", _num(params, "cercle_diametre", 1, 3000, 60), _num(params, "cercle_trou", 0, 3000, 0)), q, "aucune")
    contenu = str(params.get("texte") or "")
    q = int(round(_num(params, "texte_quantite", 0, 500, 1)))
    if contenu.strip() and q:
        ajouter(md.texte("", contenu, _num(params, "texte_hauteur", 4, 3000, 25), _num(params, "texte_trait", 0.8, 1000, 2.5),
                         bool(params.get("texte_pochoir", False))), q, _choix(params, "texte_rotation", md.ROTATIONS, "aucune"))
    if not pieces:
        raise ValueError("Ajoute au moins une pièce : un fichier SVG ou DXF, un rectangle, une liste de rectangles, un disque ou un texte.")
    if bool(params.get("suit_fil", False)):
        for p in pieces:
            p.fil = True
    return pieces


def mesurer(params, inputs, progress, mode):
    """Chute photographiée : (Resultat, format, coins en fractions, feuille détectée)."""
    fmt = FORMATS.get(str(params.get("format", "reperes_a4")))
    if fmt is None:
        raise ValueError("Feuille de référence inconnue.")
    coins = lire_coins(params.get("coins"))
    progress(0.02, "Lecture de la photo")
    photo = charger_photo(inputs.get("photo"))
    source_img = rendu.reduire_image(photo, COTE_SOURCE)
    apercu_img = rendu.reduire_image(source_img, COTE_APERCU)
    del photo
    source = np.asarray(source_img, dtype=np.uint8)
    apercu = np.asarray(apercu_img, dtype=np.uint8)
    ha, la = apercu.shape[:2]
    hs, ls = source.shape[:2]
    if coins is not None:
        px = [(x * ls, y * hs) for x, y in coins]
        if not convexe(px):
            raise ValueError("Les 4 coins indiqués se croisent : donne-les dans l'ordre haut-gauche, haut-droite, bas-droite, bas-gauche "
                             "de la feuille.")
        fractions, detectee = coins, False
    else:
        progress(0.06, "Recherche des repères" if fmt.reperes else "Recherche de la feuille")
        q = me.detecter(apercu, fmt)
        if q is None:
            quoi = "les 4 repères de la feuille de repères" if fmt.reperes else f"la feuille {fmt.libelle} blanche"
            raise ValueError(f"Feuille introuvable : {quoi} n'apparaît pas nettement sur la photo. Photographie la feuille entière, "
                             "bien de dessus, ou indique ses 4 coins (réglage « Coins de la feuille »).")
        fractions, detectee = [(x / la, y / ha) for x, y in q], True
    if bool(params.get("origine_180", False)):
        fractions = fractions[2:] + fractions[:2]
    r = me.ReglagesMesure(mode, fmt, _num(params, "sensibilite", -1, 1, 0.0), bool(params.get("ombres", True)))
    progress(0.12, "Détourage de l'aperçu")
    try:
        a = me.calculer(apercu, [(x * la, y * ha) for x, y in fractions], r, me.PX_MM_APERCU)
        progress(0.22, "Mesure en pleine résolution")
        r.seuil = a.seuil
        if mode == "feuille_sur_chute":
            r.zone = a.zone.liste()
        res = me.calculer(source, [(x * ls, y * hs) for x, y in fractions], r, me.PX_MM_FINAL)
    except ValueError as e:
        if isinstance(e, Refus):
            raise
        raise ValueError(f"Redressement impossible : {e}") from None
    if not res.bords:
        raise ValueError("Aucun bord de chute trouvé : vérifie le cadrage, la sensibilité ou le mode de mesure.")
    return res, fmt, fractions, detectee


def executer(params, inputs, out_dir, progress):
    debut = time.monotonic()
    mode = _choix(params, "mode", ("sur_feuille", "feuille_sur_chute", "saisie"), "sur_feuille")
    interdits = lire_interdits(params.get("interdits"))
    marge = _num(params, "marge_mm", 0, 15, 2.0)
    fil = FILS.get(params.get("fil", "aucun"), "?")
    if fil == "?":
        raise ValueError("Sens du fil inconnu.")
    matiere = str(params.get("matiere") or "").strip()[:60]
    epaisseur = _num(params, "epaisseur_mm", 0.1, 200, 3.0)
    kerf = _num(params, "kerf_mm", 0, 1.5, 0.15)
    espacement = _num(params, "espacement_mm", 0, 10, 1.0)
    reglages = ReglagesPlacement(kerf, espacement, _num(params, "pas_grille_mm", 0, 2, 0.0), _num(params, "pas_angle_deg", 5, 45, 15.0))
    amelioration = int(round(_num(params, "essais", 0, 400, 60)))
    origine = _choix(params, "origine", tuple(ex.ORIGINES), "feuille")
    r_export = ex.ReglagesExport(
        svg=bool(params.get("svg", True)), dxf=bool(params.get("dxf", True)), laser_studio=bool(params.get("laser_studio", True)),
        gcode=bool(params.get("gcode", False)), origine=origine, compenser_kerf=bool(params.get("compenser_kerf", False)),
        repere_chute=bool(params.get("reperes", True)), puissance_pct=_num(params, "puissance_pct", 5, 100, 80.0),
        vitesse_mm_min=_num(params, "vitesse_mm_min", 50, 3000, 300.0), passes=int(round(_num(params, "passes", 1, 10, 1))),
        s_max=int(_choix(params, "s_max", (255, 1000), 1000)), air_assist=bool(params.get("air_assist", False)))
    if not (r_export.svg or r_export.dxf or r_export.laser_studio or r_export.gcode):
        raise ValueError("Choisis au moins un fichier à créer (SVG, DXF, Alpine Laser Studio ou G-code).")
    lignes = []
    avertissements = []
    nom_chute = f"Chute de {matiere[:1].lower() + matiere[1:]}" if matiere else "Chute"

    # ------------------------------------------------------------------ chute
    res, fmt, fractions, detectee = None, None, None, False
    if mode == "saisie":
        l = _num(params, "largeur_mm", 5, 3000, 300.0)
        h = _num(params, "hauteur_mm", 5, 3000, 200.0)
        rc = _num(params, "rayon_coins_mm", 0, 1500, 0.0)
        if rc > min(l, h) / 2:
            raise ValueError(f"Rayon des coins : de 0 à {nombre(min(l, h) / 2)} mm.")
        chute = md.Chute(md.Chute.rectangulaire(l, h, rc), interdits, marge, fil, nom_chute, matiere, epaisseur, hauteur_feuille=None)
        mesure_txt = f"Chute saisie à la main : {dimensions(l, h)}" + (f", coins arrondis de {nombre(rc)} mm" if rc > 0 else "")
        if inputs.get("photo") is not None:
            avertissements.append("Photo ignorée : la chute est saisie à la main (choisis une mesure par photo pour l'utiliser).")
    else:
        res, fmt, fractions, detectee = mesurer(params, inputs, progress, mode)
        chute = md.Chute(res.bords, interdits, marge, fil, nom_chute, matiere, epaisseur, hauteur_feuille=fmt.hauteur)
        mesure_txt = (f"Mesure par photo ({LIBELLES_MODE[mode]}, {fmt.libelle}) : "
                      + ("repères détectés" if fmt.reperes and detectee else "feuille détectée" if detectee else "coins indiqués à la main")
                      + f", {nombre(res.ppm)} px/mm")
    lignes.append(mesure_txt)
    lignes.append(f"Chute : {dimensions(chute.largeur, chute.hauteur)}, {mm2(chute.aire)} de matière"
                  + (f", {chute.trous} trou(s)" if chute.trous else "") + f", marge au bord {nombre(marge)} mm"
                  + (f", {len(interdits)} zone(s) interdite(s)" if interdits else ""))
    if res is not None and res.touche_bord:
        avertissements.append("Attention : la chute touche le bord de la zone analysée, elle est peut-être coupée. "
                              + ("Pose-la bien dans le cadre de la feuille." if mode == "sur_feuille" else "Recule pour que toute la chute soit dans la photo."))
    if res is not None and mode == "sur_feuille" and res.autres > 0:
        avertissements.append(f"{res.autres} autre(s) objet(s) trouvé(s) sur la feuille : seule la plus grande chute est gardée.")

    # ------------------------------------------------------------------ pièces et placement
    progress(0.4, "Pièces")
    pieces = construire_pieces(params, inputs, "quart", avertissements)
    total = sum(p.quantite for p in pieces)
    progress(0.45, "Placement automatique")
    pl = Placement(chute, pieces, reglages)
    systematiques = len(pl.ordres()) * len(COINS)
    restant = max(10.0, LIMITE_PLACEMENT_S - (time.monotonic() - debut))
    plan = pl.placer(max_essais=systematiques + amelioration, limite_s=restant,
                     progress=lambda f: progress(0.45 + 0.43 * f, "Placement automatique"))
    if not plan.poses:
        raise ValueError(f"Aucune pièce ne tient dans la chute (surface utile {mm2(pl.aire_libre)}) : réduis la marge ou l'espacement, "
                         "autorise la rotation libre, ou choisis une chute plus grande.")
    par_id = {p.id: p for p in pieces}
    lignes.append(f"Pièces posées : {len(plan.poses)} / {total}, utilisation de la chute {pourcent(100 * plan.utilisation)}")
    for pid, n in plan.manques.items():
        avertissements.append(f"Non placée(s) : {n} × « {par_id[pid].nom} »")
    lignes.append(f"Placement : {plan.essais} essais, grille de {nombre(plan.pas_mm)} mm, trait de coupe {nombre(kerf)} mm, "
                  f"espacement {nombre(espacement)} mm")
    if not plan.complet_apres:
        avertissements.append(f"Recherche arrêtée après {plan.essais} essais (durée maximale atteinte) : meilleur plan trouvé gardé.")

    # ------------------------------------------------------------------ exports
    progress(0.9, "Écriture des fichiers")
    titre = str(params.get("titre") or "").strip()[:60]
    nom_plan = titre or f"Plan {nom_chute.lower()}"
    base = nom_fichier(nom_plan, "plan")
    p_export = ex.PlanExport.de(nom_plan, chute, pieces, plan, kerf)
    out_dir.mkdir(parents=True, exist_ok=True)
    fichiers = []
    if r_export.svg:
        (out_dir / f"{base}.svg").write_text(ex.svg(p_export, r_export), encoding="utf-8", newline="")
        fichiers.append({"name": f"{base}.svg", "label": "SVG (mm) pour la découpe laser", "library": "laser"})
    if r_export.dxf:
        (out_dir / f"{base}.dxf").write_bytes(ex.dxf(p_export, r_export).encode("utf-8"))
        fichiers.append({"name": f"{base}.dxf", "label": "DXF R12 (CNC, CAO, LightBurn)", "library": "cnc"})
    if r_export.laser_studio:
        horodatage = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (out_dir / f"{base}.alsproj").write_text(ex.laser_studio(p_export, r_export, horodatage), encoding="utf-8", newline="")
        fichiers.append({"name": f"{base}.alsproj", "label": "Projet Alpine Laser Studio", "library": "laser"})
    if r_export.gcode:
        (out_dir / f"{base}.gcode").write_text(ex.gcode(p_export, r_export), encoding="utf-8", newline="")
        fichiers.append({"name": f"{base}.gcode", "label": "G-code GRBL (laser)", "library": None})
        avertissements.append("G-code : teste la puissance et la vitesse sur un morceau de la même matière ; "
                              "ne laisse jamais le laser sans surveillance.")
    if bool(params.get("feuille_reperes", False)):
        from . import pdf
        (out_dir / "feuille-reperes-a4.pdf").write_bytes(pdf.feuille_reperes())
        fichiers.append({"name": "feuille-reperes-a4.pdf", "label": "Feuille de repères A4 (PDF à imprimer à 100 %)", "library": None})
    if origine == "feuille" and mode == "saisie":
        lignes.append("Origine des fichiers : coin de la chute (chute saisie à la main, sans feuille).")
    else:
        lignes.append(f"Origine des fichiers : {ex.ORIGINES[origine].lower()} — SVG et Laser Studio en haut à gauche, "
                      "DXF et G-code en bas à gauche (Y vers l'arrière de la machine).")
    if r_export.compenser_kerf and kerf > 0:
        lignes.append(f"Trait de coupe compensé dans les fichiers : contours décalés de {nombre(kerf / 2)} mm hors des pièces "
                      "(ne règle pas de kerf dans ton logiciel).")

    progress(0.95, "Aperçu")
    origine_mm = (0.0, 0.0) if origine == "feuille" else (chute.boite[0], chute.boite[1])
    cartouche = [f"{len(plan.poses)} / {total} pièces - utilisation {pourcent(100 * plan.utilisation)}",
                 f"Chute {dimensions(chute.largeur, chute.hauteur).replace('×', 'x')} - trait {nombre(kerf)} mm - "
                 f"espacement {nombre(espacement)} mm"]
    rendu.apercu(chute, p_export.pieces, res, cartouche, origine_mm).save(out_dir / "apercu.png", optimize=True)

    places = {}
    for q in plan.poses:
        places[q.piece] = places.get(q.piece, 0) + 1
    summary = {
        "mode": mode, "format": fmt.code if fmt else None, "feuille_detectee": detectee,
        "coins_pct": [[round(x * 100, 2), round(y * 100, 2)] for x, y in fractions] if fractions else None,
        "seuil": res.seuil if res else None, "px_par_mm": round(res.ppm, 3) if res else None,
        "chute": {"largeur_mm": round(chute.largeur, 2), "hauteur_mm": round(chute.hauteur, 2), "aire_mm2": round(chute.aire, 1),
                  "aire_utile_mm2": round(pl.aire_libre, 1), "trous": chute.trous, "marge_mm": marge, "zones_interdites": len(interdits),
                  "matiere": matiere, "epaisseur_mm": epaisseur, "fil": params.get("fil", "aucun")},
        "pieces_total": total, "pieces_placees": len(plan.poses), "pieces_non_placees": plan.non_placees,
        "utilisation": round(plan.utilisation, 4), "utilisation_pct": round(100 * plan.utilisation, 1),
        "manques": [{"piece": par_id[k].nom, "manque": v} for k, v in plan.manques.items()],
        "pieces": [{"nom": p.nom, "quantite": p.quantite, "placees": places.get(p.id, 0), "largeur_mm": round(p.largeur, 2),
                    "hauteur_mm": round(p.hauteur, 2), "aire_mm2": round(p.aire, 1), "rotation": p.rotation, "suit_fil": p.fil} for p in pieces],
        "essais": plan.essais, "pas_grille_mm": plan.pas_mm, "kerf_mm": kerf, "espacement_mm": espacement, "origine": origine,
        "poses": [{"piece": par_id[q.piece].nom, "exemplaire": q.exemplaire + 1, "angle": q.angle, "x_mm": round(q.x, 4), "y_mm": round(q.y, 4)}
                  for q in plan.poses[:MAX_POSES_RESUME]],
    }
    progress(1.0, "Terminé")
    return {"files": fichiers, "preview": "apercu.png", "details": lignes + avertissements, "summary": summary}
