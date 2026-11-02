"""Relief souvenir — altitudes et via ferrata par les services publics (port de Terrain.kt de l'application).

Seul fichier de l'outil qui touche au réseau. Sources ouvertes et sans clé : swisstopo (profil api3.geo.admin.ch) en
Suisse, Terrain Tiles (AWS Open Data) ailleurs, Overpass (OpenStreetMap) pour les via ferrata. Volume borné : au plus
12 requêtes swisstopo ou 16 tuiles, une requête Overpass, et MAX_REQUETES en tout par job ; délai d'attente 20 s,
User-Agent « AlpineMakersWorker ». Le téléchargeur est injectable : telecharger(url, max_octets) -> bytes (b"" = 404).
"""
import io
import urllib.error
import urllib.request

import numpy as np

from . import geo

USER_AGENT = "AlpineMakersWorker"
DELAI_S = 20
DELAI_OVERPASS_S = 40  # le serveur public Overpass répond souvent en 28 à 40 s
MAX_REQUETES = 40
ESSAIS_OVERPASS = 2
HORS_LIGNE = ("Pas de connexion : les altitudes viennent des services publics (swisstopo, Terrain Tiles). "
              "Vérifie la connexion du Worker, puis réessaie.")
OSM_HORS_LIGNE = ("OpenStreetMap (Overpass) ne répond pas : vérifie la connexion du Worker ou réessaie plus tard. "
                  "Tu peux aussi envoyer le GPX de la via ferrata.")


def telecharger_http(url, max_octets=8_000_000, message=HORS_LIGNE, service="Service d'altitudes"):
    """Téléchargement réel (urllib) : délai 20 s, User-Agent du Worker, taille bornée ; 404 → b""."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "fr"})
    try:
        delai = DELAI_OVERPASS_S if "Overpass" in service else DELAI_S
        with urllib.request.urlopen(req, timeout=delai) as r:  # noqa: S310 - URL construite par l'outil (https fixes)
            octets = r.read(max_octets + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return b""
        raise ValueError(f"{service} indisponible (HTTP {e.code}). Réessaie plus tard.") from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise ValueError(message) from None
    if len(octets) > max_octets:
        raise ValueError("Réponse trop grosse du service public.")
    return octets


class Reseau:
    """Compte les requêtes d'un job et appelle le téléchargeur (réel ou simulé)."""

    def __init__(self, telecharger=None, max_requetes=MAX_REQUETES):
        self.telecharger = telecharger
        self.max_requetes = max_requetes
        self.requetes = 0

    def lire(self, url, max_octets=8_000_000, message=HORS_LIGNE, service="Service d'altitudes"):
        if self.requetes >= self.max_requetes:
            raise ValueError("Trop de requêtes aux services publics pour ce relief : réduis la zone.")
        self.requetes += 1
        if not url.startswith("https://"):
            raise ValueError("Adresse de service refusée.")
        if self.telecharger is None:
            return telecharger_http(url, max_octets, message, service)
        octets = self.telecharger(url, max_octets)
        if octets is None:
            return b""
        if len(octets) > max_octets:
            raise ValueError("Réponse trop grosse du service public.")
        return bytes(octets)


def _decoder_tuile(octets):
    from PIL import Image
    try:
        with Image.open(io.BytesIO(octets)) as im:
            if im.size != (256, 256):
                return None
            rgb = np.asarray(im.convert("RGB"))
    except Exception:  # noqa: BLE001 - tuile illisible : traitée comme absente (trou bouché ensuite)
        return None
    return geo.terrarium_altitude(rgb)


def altitudes(zone, reseau, cote=geo.COTE_MAX, progress=None):
    """Altitudes de la zone : swisstopo si elle est en Suisse (repli sur Terrain Tiles hors couverture).
    Rend (grille, source)."""
    def avancer(f, msg):
        if progress:
            progress(f, msg)

    if geo.en_suisse(zone.lat, zone.lon):
        p = geo.ProjectionSuisse(zone.lat, zone.lon)
        g = geo.Grille.pour(zone, cote)
        req = geo.swisstopo_requetes(g, p)
        for i, (_, _, url) in enumerate(req):
            texte = reseau.lire(url).decode("utf-8", "replace")
            if texte:
                geo.swisstopo_lire(texte, g, p)
            avancer(0.05 + 0.25 * (i + 1) / len(req), "Altitudes swisstopo")
        if g.boucher():
            return g, geo.SWISSTOPO
    p = zone.projection(geo.MONDE)
    g = geo.Grille.pour(zone, cote)
    z, tuiles = geo.terrarium_plan(g, p, zone.lat)
    pixels = {}
    for i, t in enumerate(tuiles):
        o = reseau.lire(geo.terrarium_url(t))
        if o:
            alt = _decoder_tuile(o)
            if alt is not None:
                pixels[t] = alt
        avancer(0.05 + 0.25 * (i + 1) / len(tuiles), "Altitudes Terrain Tiles")
    geo.terrarium_echantillonner(g, p, z, pixels)
    if not g.boucher():
        raise ValueError("Altitudes incomplètes pour cette zone : réessaie plus tard ou choisis une autre zone.")
    return g, geo.MONDE


def via_ferrata(lat, lon, demi_cote_m, reseau):
    """Via ferrata d'OpenStreetMap autour du centre (boîte de 12 km au plus, 60 voies au plus, réponse ≤ 4 Mo)."""
    url = geo.osm_url(geo.osm_boite(lat, lon, demi_cote_m))
    octets = b""
    for essai in range(ESSAIS_OVERPASS):  # serveur public parfois chargé : un seul nouvel essai
        try:
            octets = reseau.lire(url, 4_000_000, OSM_HORS_LIGNE, "Service OpenStreetMap (Overpass)")
            break
        except ValueError:
            if essai + 1 >= ESSAIS_OVERPASS:
                raise
    if not octets:
        raise ValueError(OSM_HORS_LIGNE)
    return geo.osm_lire(octets.decode("utf-8", "replace"))
