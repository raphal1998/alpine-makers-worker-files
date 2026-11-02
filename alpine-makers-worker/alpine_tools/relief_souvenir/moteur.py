"""Relief souvenir — déroulé d'un job (équivalent de l'écran ReliefUi.kt d'Alpine Gift, sans interface).

GPX facultatif OU centre (latitude, longitude) + taille de zone ; via ferrata d'OpenStreetMap autour du centre ;
altitudes swisstopo / Terrain Tiles ; vues Carte, Montagne 3D et Face ; 3MF multicolore, STL par couleur, STL unique
ou OBJ avec couleurs ; aperçu PNG ; détails (tailles, altitudes, filaments, attributions).
"""
import dataclasses
import io
import math
import unicodedata

import numpy as np

from . import face as face_mod
from . import geo, gpx, maillage, relief, relief3d, terrain

PALETTES = {
    "paroi": [("#4A5560", "Ardoise"), ("#8C8C8C", "Gris"), ("#D9D4CC", "Calcaire")],
    "alpage": [("#1D3B2A", "Vert forêt"), ("#7F9C7A", "Sauge"), ("#8C8C8C", "Gris"), ("#F4F4F4", "Blanc")],
    "foret-neige": [("#1D3B2A", "Vert forêt"), ("#8C8C8C", "Gris"), ("#F4F4F4", "Blanc")],
    "desert": [("#A8673A", "Caramel"), ("#C8B38A", "Sable"), ("#F0E2C6", "Crème")],
    "hypsometrie": [("#1E7838", "Vert"), ("#B4D234", "Anis"), ("#F8D84A", "Jaune"), ("#A8673A", "Caramel"), ("#F4F4F4", "Blanc")],
    "glacier": [("#1E2D5A", "Bleu nuit"), ("#1E5EBE", "Bleu"), ("#9ED2EC", "Glacier"), ("#F4F8FB", "Blanc")],
    "noir-gris-blanc": [("#141414", "Noir"), ("#8C8C8C", "Gris"), ("#F4F4F4", "Blanc")],
    "noir-vert-anis-blanc": [("#141414", "Noir"), ("#1E7838", "Vert"), ("#B4D234", "Anis"), ("#F4F4F4", "Blanc")],
    "noir-bleu-glacier-blanc": [("#141414", "Noir"), ("#1E5EBE", "Bleu"), ("#9ED2EC", "Glacier"), ("#F4F8FB", "Blanc")],
}
NOMS_COULEURS = {
    "#BE202E": "Rouge", "#1E5EBE": "Bleu", "#F4F4F4": "Blanc", "#141414": "Noir", "#9ED2EC": "Glacier", "#F07A1A": "Orange",
    "#F8D84A": "Jaune", "#1E7838": "Vert", "#5A2A88": "Violet", "#D3A63A": "Or", "#E66AA5": "Rose",
}
for _pal in PALETTES.values():
    for _h, _n in _pal:
        NOMS_COULEURS.setdefault(_h, _n)
FORMATS = {
    "3mf": (".3mf", "3MF multicolore", "Bambu Studio, OrcaSlicer, Creality Print : chaque couleur arrive sur son filament."),
    "stl_couleurs": ("-stl.zip", "STL par couleur (ZIP)", "Un STL par couleur : importe-les ensemble dans ton trancheur, puis un filament par pièce."),
    "stl": (".stl", "STL unique", "Tout le modèle en une seule pièce : impression d'une seule couleur, ou à peindre."),
    "obj": ("-obj.zip", "OBJ avec couleurs (ZIP)", "OBJ + MTL : couleurs gardées, pour Blender, PrusaSlicer, Bambu Studio et la plupart des logiciels 3D."),
}
LIBELLES_TYPE = {"randonnee": "Randonnée", "via_ferrata": "Via ferrata", "escalade": "Voie d'escalade"}
CONSEILS = [
    "Ouvre le 3MF dans Bambu Studio ou OrcaSlicer : chaque pièce arrive sur son filament (N° 1 = la tranche la plus basse).",
    "Les tranches d'altitude sont horizontales : l'AMS ne change de couleur qu'aux seuils, sauf pour le tracé et le texte.",
    "Hauteur de couche 0,12 à 0,16 mm pour des courbes de niveau fines ; remplissage 10 à 15 % ; pas de supports.",
]


def teinte(hexa):
    h = str(hexa).upper()
    return h, NOMS_COULEURS.get(h, h)


def _sans_accents(t):
    return "".join(c for c in unicodedata.normalize("NFD", str(t).lower()) if not unicodedata.category(c).startswith("M")).strip()


def _fr(v, dec=1):
    return (f"%.{dec}f" % v).replace(".", ",")


def _entier(v):
    return f"{int(round(v)):,}".replace(",", " ")


def choisir_voie(voies, demande, lat, lon):
    """Voie demandée par son numéro (1 = la plus longue, ordre des détails) ou son nom ; sinon la plus proche du centre."""
    demande = str(demande or "").strip()
    if demande:
        if demande.isdigit():
            k = int(demande)
            if 1 <= k <= len(voies):
                return voies[k - 1]
        cle = _sans_accents(demande)
        for v in voies:
            if _sans_accents(v["nom"]) == cle:
                return v
        for v in voies:
            if cle in _sans_accents(v["nom"]):
                return v
        noms = " ; ".join(f"{i + 1}. {v['nom']}" for i, v in enumerate(voies[:12]))
        raise ValueError(f"Via ferrata « {demande[:60]} » introuvable autour du centre. Trouvées : {noms}.")
    def dmin(v):
        a = np.asarray(v["points"])
        return float(geo.distance(lat, lon, a[:, 0], a[:, 1]).min())
    return min(voies, key=dmin)


def _lire_gpx(chemin):
    if chemin.stat().st_size > gpx.TEXTE_MAX:
        raise ValueError("Fichier GPX trop gros (30 Mo au plus).")
    octets = chemin.read_bytes()
    try:
        texte = octets.decode("utf-8-sig")
    except UnicodeDecodeError:
        texte = octets.decode("latin-1")
    return gpx.lire(texte)


def _png(img, maxi=None):
    from PIL import Image
    im = Image.fromarray(img)
    if maxi and max(im.size) > maxi:
        s = maxi / max(im.size)
        im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "PNG", optimize=True)
    return b.getvalue()


def executer(params, inputs, out_dir, progress, telecharger=None):
    def avancer(f, msg=""):
        if progress:
            progress(f, msg)

    reseau = terrain.Reseau(telecharger)
    typ = params.get("type") or "randonnee"
    vue = params.get("vue") or "auto"
    if vue == "auto":
        vue = "carte" if typ == "randonnee" else "montagne"
    forme = params.get("forme") or geo.RECTANGLE
    if vue != "carte":
        forme = geo.RECTANGLE
    trace_gpx = _lire_gpx(inputs["gpx"]) if inputs.get("gpx") else None
    avancer(0.02, "Préparation")

    # ------------------------------------------------------------------ réglages
    palette = params.get("palette") or "auto"
    if palette == "perso":
        couleurs = [teinte(h) for h in params.get("couleurs") or []]
    elif palette == "auto":
        couleurs = list(relief.COULEURS_DEFAUT if typ == "randonnee" else PALETTES["paroi"])
    elif palette in PALETTES:
        couleurs = list(PALETTES[palette])
    else:
        raise ValueError("Palette inconnue.")
    if not relief.COULEURS_MIN <= len(couleurs) <= relief.COULEURS_MAX:
        raise ValueError("Choisis de 2 à 8 couleurs.")
    exageration = float(params.get("exageration") or 1.5)
    if vue == "montagne" and abs(exageration - 1.5) < 1e-9:
        exageration = 1.0
    reperes = params.get("reperes") or "auto"
    reperes = (typ != "randonnee") if reperes == "auto" else reperes == "oui"
    p = relief.ParamRelief(
        forme=forme, largeur_mm=float(params.get("largeur_mm") or 150), sans_socle=not params.get("socle", True),
        exageration=exageration, couleurs=couleurs, repartition=params.get("repartition") or "surface",
        lacs=bool(params.get("lacs")), niveau_lac_m=params.get("niveau_lac_m"), couleur_lac=teinte(params.get("couleur_lac") or "#1E5EBE"),
        trace=bool(params.get("trace", True)), couleur_trace=teinte(params.get("couleur_trace") or "#BE202E"),
        largeur_trace_mm=float(params.get("largeur_trace_mm") or 1.4), hauteur_trace_mm=float(params.get("hauteur_trace_mm") or 0.8),
        reperes=reperes, relais=int(params.get("relais") or 0), couleur_reperes=teinte(params.get("couleur_reperes") or "#F4F4F4"),
        texte1=str(params.get("texte1") or ""), texte2=str(params.get("texte2") or ""),
        couleur_texte=teinte(params.get("couleur_texte") or "#F4F4F4"), bandeau_mm=float(params.get("bandeau_mm") if params.get("bandeau_mm") is not None else 14),
        couleur_socle=teinte(params.get("couleur_socle") or "#1D3B2A"), finesse_mm=float(params.get("finesse_mm") or 0.5))

    # ------------------------------------------------------------------ zone et voie
    voie, voies = None, []
    lat, lon = params.get("latitude"), params.get("longitude")
    if trace_gpx is not None:
        pts = trace_gpx["points"]
        lats, lons = [q[0] for q in pts], [q[1] for q in pts]
        if vue == "face":
            z = geo.zone_autour(lats, lons, geo.RECTANGLE)
            s = min(geo.COTE_MAX_M, max(geo.COTE_MIN_M, max(z.largeur_m, z.hauteur_m) / 0.7))
            zone = geo.Zone(z.lat, z.lon, s, s, geo.RECTANGLE)
        else:
            zone = geo.zone_autour(lats, lons, forme)
    else:
        if lat is None or lon is None:
            raise ValueError("Indique la latitude et la longitude du centre de la zone, ou envoie le fichier GPX du tracé.")
        s = min(geo.COTE_MAX_M, max(geo.COTE_MIN_M, float(params.get("taille_km") or 6) * 1000.0))
        zone = geo.Zone(float(lat), float(lon), s, s if (vue != "carte" or forme != geo.RECTANGLE) else s * 0.8, forme)
        if typ == "via_ferrata":
            avancer(0.04, "Via ferrata dans OpenStreetMap")
            voies = terrain.via_ferrata(zone.lat, zone.lon, min(zone.boite_m) / 2, reseau)
            if not voies:
                raise ValueError("Aucune via ferrata cartographiée dans OpenStreetMap autour de ce centre : agrandis la zone ou envoie le GPX de la voie.")
            voie = choisir_voie(voies, params.get("voie"), zone.lat, zone.lon)
            if vue != "carte" and params.get("cadrer_voie", True):
                a = np.asarray(voie["points"])
                la0, la1, lo0, lo1 = a[:, 0].min(), a[:, 0].max(), a[:, 1].min(), a[:, 1].max()
                clat, clon = (la0 + la1) / 2, (lo0 + lo1) / 2
                etendue = max((la1 - la0) * 111_320.0, (lo1 - lo0) * 111_320.0 * math.cos(math.radians(clat)))
                cote = min(4000.0, max(600.0, etendue * 1.6 + 500))
                zone = geo.Zone(float(clat), float(clon), cote, cote, geo.RECTANGLE)

    # ------------------------------------------------------------------ altitudes
    avancer(0.05, "Altitudes en téléchargement")
    g, source = terrain.altitudes(zone, reseau, geo.COTE_FACE if vue != "carte" else geo.COTE_MAX, avancer)
    proj = zone.projection(source)
    trace_xyz = None
    if trace_gpx is not None:
        pts = trace_gpx["points"]
        x, y = proj.vers_local([q[0] for q in pts], [q[1] for q in pts])
        trace_xyz = (np.asarray(x), np.asarray(y), np.asarray([q[2] if q[2] is not None else np.nan for q in pts], dtype=float))
    elif voie is not None:
        a = np.asarray(voie["points"])
        x, y = proj.vers_local(a[:, 0], a[:, 1])
        trace_xyz = (np.asarray(x), np.asarray(y), np.full(len(a), np.nan))

    # ------------------------------------------------------------------ textes automatiques
    titre_auto = ""
    if trace_gpx is not None:
        titre_auto = trace_gpx["nom"]
    elif voie is not None:
        titre_auto = voie["nom"]
    if params.get("texte_auto", True):
        t1, t2 = p.texte1, p.texte2
        if not t1.strip():
            t1 = titre_auto[:40]
        if not t2.strip():
            if trace_gpx is not None:
                pts = trace_gpx["points"]
                morceaux = [gpx.date(pts)]
                if typ == "randonnee":
                    morceaux.append(gpx.km(gpx.distance_totale(pts)))
                    if gpx.a_des_altitudes(pts):
                        morceaux.append(f"D+ {_entier(gpx.denivele([q[2] for q in pts if q[2] is not None]))} m")
                t2 = " · ".join(m for m in morceaux if m)
            elif voie is not None:
                t2 = " · ".join(m for m in ("Via ferrata", gpx.km(voie["longueur_m"]), f"niveau {voie['niveau']}" if voie["niveau"] else "") if m)
        p = dataclasses.replace(p, texte1=t1[:40], texte2=t2[:60])
    avancer(0.32, "Analyse du relief")

    # ------------------------------------------------------------------ vue
    face = None
    azimut = None
    if vue == "face":
        orientation = params.get("orientation_face") or "auto"
        if orientation == "auto":
            orientation = 180.0
            if trace_xyz is not None:
                pc = dataclasses.replace(p, exageration=1.0)
                a0 = relief.analyser(g, zone, pc)
                c0 = relief.Contexte(g, zone, pc, a0)
                xs, ys = c0.vers_modele(trace_xyz[0], trace_xyz[1])
                orientation = relief3d.azimut_face(xs, ys, c0.zT) or 180.0
        face = face_mod.construire(g, zone, float(orientation), p.largeur_mm, float(params.get("relief_face_mm") or 16),
                                   float(params.get("profondeur_face") or 1.0), params.get("alt_bas_m"), params.get("alt_haut_m"),
                                   teinte(params.get("couleur_ciel") or "#9ED2EC"))
        pp = face.reglages(p)
        g_v, zone_v = face.grille, face.zone
        trace_v = None
        if trace_xyz is not None:
            alt = np.where(np.isfinite(trace_xyz[2]), trace_xyz[2], g.altitude(trace_xyz[0], trace_xyz[1]))
            trace_v = face.projeter(trace_xyz[0], trace_xyz[1], alt)
    else:
        pp = dataclasses.replace(p, trace_surface=(vue == "montagne"))
        g_v, zone_v = g, zone
        trace_v = (trace_xyz[0], trace_xyz[1]) if trace_xyz is not None else None
    a = relief.analyser(g_v, zone_v, pp)
    avancer(0.38, "Pièces du relief")
    modele = relief.modele(g_v, zone_v, pp, a, trace_v, progress=lambda f, m="": avancer(0.38 + 0.4 * f, m))

    # ------------------------------------------------------------------ aperçu
    avancer(0.8, "Aperçu")
    if vue == "montagne":
        depuis = params.get("vue_depuis") or "auto"
        if depuis == "auto":
            azimut = 200.0
            if trace_v is not None:
                c = relief.Contexte(g_v, zone_v, pp, a)
                xs, ys = c.vers_modele(trace_v[0], trace_v[1])
                azimut = relief3d.azimut_face(xs, ys, c.zT) or 200.0
        else:
            azimut = float(depuis)
        img = relief3d.rendre(g_v, zone_v, pp, a, trace_v, 960, 720, azimut, float(params.get("hauteur_vue") or 28))
    else:
        img = relief.apercu(g_v, zone_v, pp, a, trace_v, 900)
    png = _png(img)
    (out_dir / "apercu.png").write_bytes(png)
    vignette = _png(img, 256)

    # ------------------------------------------------------------------ fichier
    fmt = params.get("format") or "3mf"
    suffixe, libelle, description = FORMATS[fmt]
    nom = f"relief-{vue}{suffixe}"
    chemin = out_dir / nom
    titre = (pp.texte1.strip() or titre_auto or "Relief souvenir")[:60]
    pieces = modele["pieces"]
    for pc in pieces:
        pc["teinte"] = modele["filaments"][pc["filament"]]
    avancer(0.86, "Écriture du fichier")
    if fmt == "3mf":
        infos = maillage.ecrire_3mf(chemin, titre, modele["filaments"], pieces, vignette)
        triangles = sum(i["triangles"] for i in infos)
    elif fmt == "stl_couleurs":
        triangles = maillage.ecrire_stl_couleurs(chemin, titre, pieces)
    elif fmt == "stl":
        triangles = maillage.ecrire_stl_unique(chemin, titre, pieces)
    else:
        triangles = maillage.ecrire_obj(chemin, titre, pieces)
    grammes = [0.0] * len(modele["filaments"])
    for pc in pieces:
        grammes[pc["filament"]] += maillage.volume(pc["sommets"], pc["triangles"]) / 1000.0 * maillage.DENSITE_PLA
    avancer(1.0, "Terminé")

    # ------------------------------------------------------------------ détails
    L, P_, H = modele["largeur_mm"], modele["profondeur_mm"], modele["hauteur_mm"]
    forme_lib = "plaque Face" if face else "bloc Montagne 3D" if vue == "montagne" else {"rectangle": "Rectangle", "cercle": "Cercle", "hexagone": "Hexagone"}[pp.forme]
    details = [f"{_fr(L)} × {_fr(P_)} × {_fr(H)} mm · {forme_lib} · {len(modele['filaments'])} filaments"]
    if face:
        details.append(f"{LIBELLES_TYPE[typ]} · {face_mod.libelle_orientation(face.orientation_deg)} · altitudes {_entier(face.alt_bas)} à "
                       f"{_entier(face.alt_haut)} m · relief {_entier(face.relief_mm)} mm · maillage {_fr(modele['finesse_mm'], 2)} mm")
    else:
        details.append(("Montagne 3D · tracé posé sur la paroi · " if vue == "montagne" else "") +
                       f"Altitudes {_entier(a.alt_min)} à {_entier(a.alt_max)} m · exagération ×{_fr(pp.exageration)} · maillage {_fr(modele['finesse_mm'], 2)} mm")
        bornes = [a.alt_min] + list(a.seuils)
        details.append("Tranches : " + " ; ".join(f"{t[1]} dès {_entier(bornes[i])} m" for i, t in enumerate(pp.couleurs) if i < len(bornes)))
    details.append(f"{_entier(triangles)} triangles · {libelle} · ≈ {_fr(sum(grammes), 0)} g de PLA")
    details += [f"Filament {i + 1} · {f[1]} {f[0]} : {_fr(grammes[i])} g" for i, f in enumerate(modele["filaments"])]
    if vue == "montagne":
        details.append(f"Vue de l'aperçu depuis le {face_mod.libelle_orientation(azimut).replace('face ', '')}")
    if trace_gpx is not None:
        pts = trace_gpx["points"]
        details.append(f"GPX : {trace_gpx['nom']} · {gpx.km(gpx.distance_totale(pts))}" + (
            f" · D+ {_entier(gpx.denivele([q[2] for q in pts if q[2] is not None]))} m" if gpx.a_des_altitudes(pts) else ""))
    if voie is not None:
        details.append(f"Voie : {voie['nom']} · OpenStreetMap · {gpx.km(voie['longueur_m'])}" + (f" · niveau OSM {voie['niveau']}" if voie["niveau"] else ""))
        if len(voies) > 1:
            details.append("Via ferrata trouvées : " + " ; ".join(f"{i + 1}. {v['nom']}" for i, v in enumerate(voies[:12])))
    details += modele["avertissements"]
    if not modele["plateau"]["tient"]:
        details.append(modele["plateau"]["message"])
    if face:
        details += face_mod.limites(source, g.pas)
    details.append(geo.ATTRIBUTIONS[source])
    if voie is not None:
        details.append(geo.OSM_ATTRIBUTION)
    details += CONSEILS[:1] if fmt == "3mf" else [description]
    details += CONSEILS[1:]
    summary = {
        "type": typ, "vue": vue, "format": fmt, "source": source, "largeur_mm": round(L, 1), "profondeur_mm": round(P_, 1),
        "hauteur_mm": round(H, 1), "triangles": int(triangles), "alt_min_m": round(a.alt_min, 1), "alt_max_m": round(a.alt_max, 1),
        "filaments": [{"hex": f[0], "nom": f[1], "grammes": round(grammes[i], 1)} for i, f in enumerate(modele["filaments"])],
        "pieces": [pc["nom"] for pc in pieces], "voie": voie["nom"] if voie else None, "requetes": reseau.requetes,
        "zone": {"lat": round(zone.lat, 6), "lon": round(zone.lon, 6), "largeur_m": round(zone.boite_m[0]), "hauteur_m": round(zone.boite_m[1])},
    }
    return {"files": [{"name": nom, "label": libelle, "library": "impression"}], "preview": "apercu.png",
            "details": details, "summary": summary}
