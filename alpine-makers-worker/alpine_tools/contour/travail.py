"""Exécution d'un job Alpine Contour sur le Worker (équivalent d'EtatAssistant : photo → aperçu → export)."""
import math
import re

from PIL import Image, ImageOps, UnidentifiedImageError

from . import chaine, detourage as dt, exports, rendu, solides
from .detection import convexe, ordonner
from .geometrie import decimal_fr, mm
from .image import FORMATS
from .trace import ReglagesContour

MAX_PIXELS = 24_000_000
MIN_COTE = 640


def _num(params, cle, lo, hi, defaut):
    try:
        v = float(params.get(cle, defaut))
    except (TypeError, ValueError):
        raise ValueError(f"Réglage « {cle} » : nombre attendu.") from None
    if not math.isfinite(v) or v < lo or v > hi:
        raise ValueError(f"Réglage « {cle} » hors limites ({lo} à {hi}).")
    return v


def charger_photo(chemin):
    """Photo orientée (EXIF), refusée au-delà de 24 Mpx ou sous 640 px de côté."""
    if chemin is None or not chemin.is_file():
        raise ValueError("Photo manquante.")
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
    morceaux = [m for m in re.split(r"[;\s]+", t) if m]
    try:
        v = [float(m.replace(",", ".")) for m in morceaux]
    except ValueError:
        raise ValueError("Coins : 8 nombres attendus (x y de chaque coin, en % de la photo).") from None
    if len(v) != 8 or any(not math.isfinite(x) or x < 0 or x > 100 for x in v):
        raise ValueError("Coins : 8 nombres entre 0 et 100 attendus (x y de chaque coin, en % de la photo).")
    return [(v[2 * k] / 100.0, v[2 * k + 1] / 100.0) for k in range(4)]


def lire_exclus(texte):
    t = (texte or "").strip()
    if not t:
        return []
    ids = []
    for m in re.split(r"[,;\s]+", t):
        if not m:
            continue
        if not m.isdigit() or not 1 <= int(m) <= 999:
            raise ValueError("Objets à écarter : numéros entiers séparés par des virgules (ex. 2, 3).")
        ids.append(int(m))
    return sorted(set(ids))


def executer(params, inputs, out_dir, progress):
    fmt = FORMATS.get(str(params.get("format", "a4")))
    if fmt is None:
        raise ValueError("Feuille de référence inconnue.")
    det = dt.ReglagesDetourage(
        sensibilite=_num(params, "sensibilite", -1, 1, 0.0), ombres=bool(params.get("ombres", False)),
        taille_min_mm2=_num(params, "taille_min_mm2", 5, 400, 25.0), trous_min_mm2=_num(params, "trous_min_mm2", 0, 200, 6.0),
        remplir_trous=bool(params.get("remplir_trous", False)))
    rc = ReglagesContour(lissage_mm=_num(params, "lissage_mm", 0, 1.5, 0.3), tolerance_mm=_num(params, "tolerance_mm", 0.02, 0.5, 0.05),
                         jeu_mm=_num(params, "jeu_mm", -3, 10, 0.0))
    veut = {k: bool(params.get(k, d)) for k, d in (("svg", True), ("dxf", True), ("png", True), ("stl_piece", False), ("stl_insert", False))}
    cadre_plans = bool(params.get("cadre_dans_plans", False))
    if not any(veut.values()):
        raise ValueError("Choisis au moins un fichier à créer.")
    epaisseur = _num(params, "epaisseur_mm", 0.6, 20, 3.0)
    taille = str(params.get("insert_taille", "auto"))
    cote = str(params.get("cote_encoche", "auto"))
    if taille not in ("auto", "sur_mesure", "grille_42") or cote not in ("auto", "haut", "bas", "gauche", "droite"):
        raise ValueError("Réglage d'insert inconnu.")
    ri = solides.ReglagesInsert(
        taille=taille, marge_mm=_num(params, "marge_mm", 2, 30, 8.0), largeur_mm=_num(params, "largeur_mm", 10, 600, 126.0),
        longueur_mm=_num(params, "longueur_mm", 10, 600, 84.0), unites_x=int(round(_num(params, "unites_x", 1, 8, 3))),
        unites_y=int(round(_num(params, "unites_y", 1, 8, 2))), jeu_grille_mm=_num(params, "jeu_grille_mm", 0, 2, 0.5),
        hauteur_mm=_num(params, "hauteur_mm", 4, 80, 20.0), profondeur_mm=_num(params, "profondeur_mm", 1, 79, 14.0),
        rayon_coin_mm=_num(params, "rayon_coin_mm", 0, 10, 4.0), encoche=bool(params.get("encoche", True)),
        diametre_encoche_mm=_num(params, "diametre_encoche_mm", 12, 40, 22.0), cote_encoche=cote)
    titre = str(params.get("titre") or "").strip()[:60] or chaine.titre_par_defaut()
    nom = chaine.nom_sur(titre)
    exclus_ids = lire_exclus(params.get("exclure"))
    coins = lire_coins(params.get("coins"))

    progress(0.02, "Lecture de la photo")
    photo = charger_photo(inputs.get("photo"))
    source_img = rendu.reduire_image(photo, chaine.COTE_SOURCE)
    apercu_img = rendu.reduire_image(source_img, chaine.COTE_APERCU)
    del photo
    source = rendu.tableau(source_img)
    apercu = rendu.tableau(apercu_img)
    ha, la = apercu.shape[:2]
    hs, ls = source.shape[:2]

    # Points de référence (fractions de la photo).
    if coins is not None:
        # Ordre et convexité jugés en pixels (les fractions déforment les côtés d'une photo non carrée).
        px = ordonner([(x * ls, y * hs) for x, y in coins])
        fractions = [(x / ls, y / hs) for x, y in px]
        if not convexe(px):
            raise ValueError("Les 4 coins indiqués ne forment pas un quadrilatère convexe.")
        detectee = False
    else:
        progress(0.06, "Recherche des repères" if fmt.reperes else "Recherche de la feuille")
        q = chaine.detecter(apercu, fmt)
        if q is None:
            quoi = ("les 4 repères de la feuille de repères" if fmt.reperes
                    else f"la feuille {fmt.libelle} blanche")
            raise ValueError(f"Feuille introuvable : {quoi} n'apparaît pas nettement sur la photo. Photographie la feuille "
                             "entière, bien de dessus, sur un fond plus sombre, ou indique ses 4 coins (réglage « Coins »).")
        fractions = [(x / la, y / ha) for x, y in q]
        detectee = True

    # Aperçu à 4 px/mm : objets, numéros et seuil (comme l'écran « Détourer »).
    progress(0.12, "Détourage de l'aperçu")
    quad_a = [(x * la, y * ha) for x, y in fractions]
    h_a = dt.homographie(quad_a, fmt)
    red_a = dt.redresser(apercu, h_a, fmt, chaine.PX_MM_APERCU)
    an_a = dt.analyser(red_a, fmt, chaine.PX_MM_APERCU)
    m_a = dt.masque(an_a, det)
    if not m_a.objets:
        raise ValueError("Aucun objet trouvé sur la feuille : vérifie que l'objet est bien dans la zone utile, "
                         "ou augmente la sensibilité.")
    inconnus = [i for i in exclus_ids if i > len(m_a.objets)]
    if inconnus:
        raise ValueError(f"Objet {inconnus[0]} inconnu : la feuille compte {len(m_a.objets)} objet"
                         f"{'s' if len(m_a.objets) > 1 else ''} (numéros sur l'aperçu).")
    graines = [o.graine for o in m_a.objets if o.id in exclus_ids]
    if len(graines) == len(m_a.objets):
        raise ValueError("Tous les objets sont écartés : garde au moins un objet.")

    # Pleine résolution à 8 px/mm, seuil de l'aperçu.
    progress(0.2, "Contour en pleine résolution")
    quad_s = [(x * ls, y * hs) for x, y in fractions]
    r = chaine.calculer(source, quad_s, fmt, det, rc, chaine.PX_MM_FINAL, graines, m_a.seuil,
                        lambda f: progress(0.2 + 0.45 * f, "Contour en pleine résolution"))
    t = r.trace
    if not t.objets:
        raise ValueError("Aucun objet retenu en pleine résolution : revois la sélection ou la sensibilité.")

    lignes = chaine.details(t, fmt, rc)
    insert = None
    if veut["stl_insert"] or (cadre_plans and (veut["svg"] or veut["dxf"])):
        progress(0.7, "Insert de rangement")
        insert = solides.insert([o.boucles for o in t.objets], ri, 0.1)
        lignes.append(f"Insert {mm(insert.largeur)} × {mm(insert.longueur)} × {mm(insert.hauteur)}, poches de {mm(ri.profondeur_mm)}")
    for o in r.masque.objets:
        if o.retenu and o.touche_bord:
            lignes.append(f"Attention : l'objet {o.id} touche le bord de la zone analysée, il est coupé. "
                          "Repose-le au centre de la feuille et reprends la photo.")
    if not detectee:
        lignes.append("Points de référence indiqués à la main.")

    out_dir.mkdir(parents=True, exist_ok=True)
    fichiers = []
    progress(0.82, "Écriture des fichiers")
    cadre = [exports.Calque("cadre", [insert.cadre], "#0000FF", 5)] if insert is not None and cadre_plans else []
    if veut["svg"]:
        calques = [exports.Calque(f"objet-{o.objet.id}", o.boucles) for o in t.objets] + cadre
        (out_dir / f"{nom}.svg").write_text(exports.svg(calques, titre), encoding="utf-8", newline="")
        fichiers.append({"name": f"{nom}.svg", "label": "SVG (mm) pour la découpe laser", "library": "laser"})
    if veut["dxf"]:
        calques = [exports.Calque("CONTOUR", t.boucles)]
        if cadre:
            calques.append(exports.Calque("CADRE", [insert.cadre], couleur_dxf=5))
        (out_dir / f"{nom}.dxf").write_bytes(exports.dxf(calques).encode("ascii"))
        fichiers.append({"name": f"{nom}.dxf", "label": "DXF R12 pour la CNC", "library": "cnc"})
    resume_piece = None
    if veut["stl_piece"]:
        m = solides.piece(t.boucles, epaisseur)
        c = m.controle()
        if not c["ferme"]:
            raise RuntimeError("Maillage de la pièce non fermé.")
        (out_dir / f"{nom}-piece.stl").write_bytes(solides.stl_octets(m, f"{titre} - piece"))
        fichiers.append({"name": f"{nom}-piece.stl", "label": f"STL pièce {decimal_fr(epaisseur)} mm", "library": "impression"})
        resume_piece = {"triangles": c["triangles"], "volume_mm3": round(m.volume(), 1), "epaisseur_mm": epaisseur}
        lignes.append(f"Pièce extrudée de {mm(epaisseur)} : {c['triangles']} triangles, maillage fermé")
    if insert is not None and veut["stl_insert"]:
        c = insert.maillage.controle()
        if not c["ferme"]:
            raise RuntimeError("Maillage de l'insert non fermé.")
        (out_dir / f"{nom}-insert.stl").write_bytes(solides.stl_octets(insert.maillage, f"{titre} - insert"))
        fichiers.append({"name": f"{nom}-insert.stl", "label": "STL insert de rangement", "library": "impression"})
    if veut["png"]:
        rendu.controle(r.redressee, chaine.PX_MM_FINAL, t, titre, lignes).save(out_dir / f"{nom}-controle.png", optimize=True)
        fichiers.append({"name": f"{nom}-controle.png", "label": "PNG de contrôle (5 px/mm)", "library": None})
    if bool(params.get("feuille_reperes", False)):
        (out_dir / "feuille-reperes-a4.pdf").write_bytes(exports.feuille_reperes_pdf())
        fichiers.append({"name": "feuille-reperes-a4.pdf", "label": "Feuille de repères A4 (PDF à imprimer à 100 %)", "library": None})
    progress(0.95, "Aperçu")
    rendu.apercu(r.redressee, chaine.PX_MM_FINAL, t, r.masque).save(out_dir / "apercu.png", optimize=True)

    c = t.cotes
    summary = {
        "format": fmt.code, "objets": len(t.objets), "objets_trouves": len(r.masque.objets), "exclus": exclus_ids,
        "seuil": r.masque.seuil, "px_par_mm": chaine.PX_MM_FINAL, "feuille_detectee": detectee,
        "coins_pct": [[round(x * 100, 2), round(y * 100, 2)] for x, y in fractions],
        "largeur_mm": round(c.largeur, 2), "hauteur_mm": round(c.hauteur, 2),
        "contours": [{"id": o.objet.id, **o.cotes.dict(), "coupe_par_le_bord": o.objet.touche_bord} for o in t.objets],
    }
    if insert is not None:
        summary["insert"] = {"largeur_mm": insert.largeur, "longueur_mm": insert.longueur, "hauteur_mm": insert.hauteur,
                             "poches": len(insert.poches)}
    if resume_piece:
        summary["piece"] = resume_piece
    progress(1.0, "Terminé")
    return {"files": fichiers, "preview": "apercu.png", "details": lignes, "summary": summary}
