# -*- coding: utf-8 -*-
"""Heure locale ↔ instant UTC pour les 8 fuseaux de l'écran Ciel étoilé d'Alpine Gift, sans tzdata.

Les Workers Windows n'ont pas la base des fuseaux (``zoneinfo`` échoue sans le paquet tzdata, qui n'est pas installé :
numpy et Pillow seulement). La table ``fuseaux_table.ZONES`` reprend les changements d'heure de la base IANA relevés
avec java.time ; les règles de conversion sont celles de ``LocalDateTime.atZone(zone)`` (Java) :
- heure normale : un seul décalage possible ;
- heure sautée (passage à l'heure d'été) : l'heure est avancée de la durée du saut ;
- heure doublée (retour à l'heure d'hiver) : le premier des deux instants (décalage d'avant, heure d'été).
Dates acceptées : du 1er janvier 1900 au 31 décembre 2100 (plage du sélecteur de date de l'application).
"""
import bisect
import calendar
import datetime as dt
import re

from .fuseaux_table import ZONES

# Fuseaux proposés (l'heure saisie est l'heure locale du lieu) : CielUi.FUSEAUX.
FUSEAUX = (
    ("Europe/Zurich", "Suisse, France, Allemagne, Italie"),
    ("Europe/London", "Royaume-Uni, Portugal"),
    ("Europe/Athens", "Grèce, Finlande"),
    ("America/New_York", "New York, Montréal"),
    ("America/Los_Angeles", "Californie"),
    ("Asia/Tokyo", "Japon"),
    ("Australia/Sydney", "Sydney"),
    ("UTC", "Temps universel"),
)
ANNEE_MIN, ANNEE_MAX = 1900, 2100
MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre",
        "décembre")
_DATE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")
_HEURE = re.compile(r"^\s*(\d{1,2})\s*(?:[:hH]\s*(\d{1,2})?)?\s*$")


class _Zone:
    def __init__(self, initial, plat):
        self.initial = initial
        self.instants = list(plat[0::2])
        self.apres = list(plat[1::2])
        self.decalages = sorted({initial, *self.apres})

    def decalage(self, instant_s):
        """Décalage (secondes) en vigueur à l'instant UTC ``instant_s`` (un changement vaut dès son instant)."""
        k = bisect.bisect_right(self.instants, instant_s) - 1
        return self.initial if k < 0 else self.apres[k]

    def vers_utc(self, local_s):
        """Instant UTC (secondes) de l'heure locale ``local_s`` (secondes « comme si UTC »), règles de Java."""
        valides = [o for o in self.decalages if self.decalage(local_s - o) == o]
        if valides:
            return local_s - max(valides)        # heure doublée : le décalage d'avant (le plus grand)
        # Heure sautée : changement dont le trou contient l'heure ; Java avance de la durée du saut.
        k = bisect.bisect_right(self.instants, local_s - min(self.decalages)) - 1
        while k >= 0:
            avant = self.initial if k == 0 else self.apres[k - 1]
            t, apres = self.instants[k], self.apres[k]
            if apres > avant and t + avant <= local_s < t + apres:
                return local_s - avant
            k -= 1
        return local_s - self.initial


_ZONES = {nom: _Zone(initial, plat) for nom, (initial, plat) in ZONES.items()}


def zone(nom):
    z = _ZONES.get(nom)
    if z is None:
        raise ValueError("Fuseau horaire inconnu.")
    return z


def lire_date(texte, aujourdhui=None):
    """« AAAA-MM-JJ » → (année, mois, jour) ; vide → ``aujourdhui`` (date du jour dans le fuseau)."""
    t = str(texte or "").strip()
    if not t:
        if aujourdhui is None:
            raise ValueError("Date manquante (AAAA-MM-JJ).")
        return aujourdhui
    m = _DATE.match(t)
    if not m:
        raise ValueError("Date invalide : écris-la AAAA-MM-JJ (par exemple 2025-06-14).")
    a, mo, j = (int(x) for x in m.groups())
    try:
        dt.date(a, mo, j)
    except ValueError:
        raise ValueError(f"Date inexistante : {t}.") from None
    if not ANNEE_MIN <= a <= ANNEE_MAX:
        raise ValueError(f"Date de {ANNEE_MIN} à {ANNEE_MAX}.")
    return a, mo, j


def lire_heure(texte):
    """« HH:MM » (ou « 22 h 30 », « 22h », « 22 ») → (heure, minute)."""
    t = str(texte or "").strip()
    m = _HEURE.match(t)
    if not m:
        raise ValueError("Heure invalide : écris-la HH:MM (par exemple 22:30).")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ValueError("Heure invalide : de 00:00 à 23:59.")
    return h, mi


def instant_ms(annee, mois, jour, heure, minute, nom_zone):
    """``date.atTime(heure).atZone(zone).toInstant().toEpochMilli()`` de l'écran Ciel étoilé."""
    local = calendar.timegm((annee, mois, jour, heure, minute, 0))
    return zone(nom_zone).vers_utc(local) * 1000


def locale(instant_ms_, nom_zone):
    """Instant (ms UTC) → datetime naïf à l'heure locale du fuseau."""
    s = instant_ms_ // 1000
    return dt.datetime(1970, 1, 1) + dt.timedelta(seconds=s + zone(nom_zone).decalage(s))


def aujourdhui(nom_zone, maintenant_ms):
    d = locale(maintenant_ms, nom_zone)
    return d.year, d.month, d.day


def date_lisible(instant_ms_, nom_zone):
    """« 14 juin 2025 · 22 h 30 » (motifs « d MMMM yyyy » et « H 'h' mm », Locale.FRANCE)."""
    d = locale(instant_ms_, nom_zone)
    return f"{d.day} {MOIS[d.month - 1]} {d.year} · {d.hour} h {d.minute:02d}"
