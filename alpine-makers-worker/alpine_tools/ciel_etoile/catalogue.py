# -*- coding: utf-8 -*-
"""Catalogue d'étoiles et figures des constellations : portage de C:\\AlpineGift core Catalogue.kt.

Étoiles du Yale Bright Star Catalogue (5e éd., Hoffleit & Warren 1991, domaine public), fichier ``etoiles-bsc5.txt``
copié de la ressource d'Alpine Gift (8 404 étoiles jusqu'à la magnitude 6,5) : position J2000 (degrés), magnitude
visuelle, indice B−V, mouvement propre (arcsec/an), désignation (Bayer ou Flamsteed). Les figures sont dessinées pour
Alpine Gift (tracés traditionnels simplifiés), étoile par étoile, d'après leur désignation : aucune donnée tierce sous
licence.
"""
from functools import lru_cache
from pathlib import Path

ATTRIBUTION = ("Étoiles : Yale Bright Star Catalogue, 5e édition (Hoffleit & Warren, 1991, NASA ADC / CDS), domaine public. "
               "Planètes : éléments approchés du JPL (Standish). Lune et Soleil : formules de J. Meeus. "
               "Figures des constellations dessinées par Alpine Gift.")
FICHIER = Path(__file__).with_name("etoiles-bsc5.txt")


class Etoile:
    """Étoile du catalogue (identité d'objet : sert de clé, comme la classe Kotlin)."""
    __slots__ = ("hr", "ra", "dec", "mag", "bv", "pm_ra", "pm_dec", "bayer", "flamsteed", "constellation")

    def __init__(self, hr, ra, dec, mag, bv, pm_ra, pm_dec, bayer, flamsteed, constellation):
        self.hr, self.ra, self.dec, self.mag, self.bv = hr, ra, dec, mag, bv
        self.pm_ra, self.pm_dec = pm_ra, pm_dec
        self.bayer, self.flamsteed, self.constellation = bayer, flamsteed, constellation


class Constellation:
    __slots__ = ("abr", "nom", "segments")

    def __init__(self, abr, nom, segments):
        self.abr, self.nom, self.segments = abr, nom, segments


def lire(texte):
    """Lignes « HR ra dec mag bv pmRa pmDec [désignations…] constellation » (Catalogue.lire)."""
    r = []
    for ligne in texte.splitlines():
        if not ligne.strip() or ligne.startswith("#"):
            continue
        c = ligne.strip().split(" ")
        if len(c) < 7:
            continue
        bayer = flam = cons = None
        if len(c) >= 8:
            cons = c[-1]
            for x in c[7:-1]:
                if x.startswith("F") and x[1:].isdigit():
                    flam = int(x[1:])
                else:
                    bayer = x
        r.append(Etoile(int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5]), float(c[6]),
                        bayer, flam, cons))
    return r


@lru_cache(maxsize=1)
def etoiles():
    try:
        texte = FICHIER.read_text(encoding="utf-8")
    except OSError:
        raise RuntimeError("Catalogue d'étoiles absent : mets le Worker à jour.") from None
    return tuple(lire(texte))


@lru_cache(maxsize=1)
def _index():
    """Désignations → étoile la plus brillante qui la porte (« Alp UMa », « Gam1 And », « F58 Ori »)."""
    m = {}

    def mettre(k, e):
        a = m.get(k)
        if a is None or e.mag < a.mag:
            m[k] = e

    for e in etoiles():
        c = e.constellation
        if c is None:
            continue
        if e.bayer is not None:
            mettre(f"{e.bayer} {c}", e)
        if e.flamsteed is not None:
            mettre(f"F{e.flamsteed} {c}", e)
    return m


def trouver(designation):
    return _index().get(designation.strip())


# Noms français des constellations dessinées.
NOMS = {
    "And": "Andromède", "Aql": "Aigle", "Aqr": "Verseau", "Ari": "Bélier", "Aur": "Cocher", "Boo": "Bouvier",
    "Cnc": "Cancer", "CVn": "Chiens de chasse", "CMa": "Grand Chien", "CMi": "Petit Chien", "Cap": "Capricorne",
    "Cas": "Cassiopée", "Cen": "Centaure", "Cep": "Céphée", "Cet": "Baleine", "Cru": "Croix du Sud",
    "CrB": "Couronne boréale", "Crv": "Corbeau", "Crt": "Coupe", "Cyg": "Cygne", "Del": "Dauphin", "Dra": "Dragon",
    "Gem": "Gémeaux", "Her": "Hercule", "Hya": "Hydre", "Leo": "Lion", "Lep": "Lièvre", "Lib": "Balance",
    "Lyr": "Lyre", "Oph": "Ophiuchus", "Ori": "Orion", "Peg": "Pégase", "Per": "Persée", "Psc": "Poissons",
    "Sgr": "Sagittaire", "Sco": "Scorpion", "Ser": "Serpent", "Tau": "Taureau", "Tri": "Triangle", "Sge": "Flèche",
    "UMa": "Grande Ourse", "UMi": "Petite Ourse", "Vir": "Vierge", "Lyn": "Lynx",
    "Col": "Colombe", "Pup": "Poupe", "Vel": "Voiles", "Car": "Carène", "PsA": "Poisson austral", "Gru": "Grue",
    "Cam": "Girafe", "Lac": "Lézard", "Vul": "Petit Renard", "Sct": "Écu de Sobieski", "Equ": "Petit Cheval",
}

# Figures : chaînes d'étoiles « A-B-C » (segments A–B et B–C), désignations complètes (ordre de Gift).
FIGURES = {
    "UMa": ("Eta UMa-Zet UMa-Eps UMa-Del UMa-Alp UMa-Bet UMa-Gam UMa-Del UMa", "Gam UMa-Chi UMa-Nu UMa", "Chi UMa-Psi UMa-Mu UMa",
            "Alp UMa-F23 UMa-Omi UMa", "Bet UMa-Ups UMa-The UMa-Kap UMa"),
    "UMi": ("Alp UMi-Del UMi-Eps UMi-Zet UMi-Bet UMi-Gam UMi-Eta UMi-Zet UMi",),
    "Cas": ("Bet Cas-Alp Cas-Gam Cas-Del Cas-Eps Cas",),
    "Cep": ("Alp Cep-Bet Cep-Gam Cep-Iot Cep-Zet Cep-Alp Cep", "Zet Cep-Del Cep"),
    "Dra": ("Lam Dra-Kap Dra-Alp Dra-Iot Dra-The Dra-Eta Dra-Zet Dra-Chi Dra-Eps Dra-Del Dra-Xi Dra-Gam Dra-Bet Dra-Nu2 Dra-Xi Dra",),
    "Cyg": ("Alp Cyg-Gam Cyg-Eta Cyg-Bet1 Cyg", "Del Cyg-Gam Cyg-Eps Cyg-Zet Cyg", "Del Cyg-Iot2 Cyg-Kap Cyg"),
    "Lyr": ("Alp Lyr-Zet1 Lyr-Bet Lyr-Gam Lyr-Del2 Lyr-Zet1 Lyr", "Alp Lyr-Eps1 Lyr-Zet1 Lyr"),
    "Aql": ("Zet Aql-Del Aql-Lam Aql", "Gam Aql-Alp Aql-Bet Aql", "Alp Aql-Del Aql-The Aql", "Zet Aql-Gam Aql"),
    "Sge": ("Gam Sge-Del Sge-Alp Sge", "Del Sge-Bet Sge"),
    "Del": ("Alp Del-Bet Del-Del Del-Gam2 Del-Alp Del", "Bet Del-Eps Del"),
    "Her": ("Eps Her-Zet Her-Eta Her-Pi Her-Eps Her", "Zet Her-Bet Her-Gam Her", "Eps Her-Del Her-Alp1 Her", "Pi Her-Rho Her-The Her-Iot Her",
            "Eta Her-Sig Her-Tau Her-Phi Her"),
    "CrB": ("The CrB-Bet CrB-Alp CrB-Gam CrB-Del CrB-Eps CrB-Iot CrB",),
    "Boo": ("Alp Boo-Eps Boo-Del Boo-Bet Boo-Gam Boo-Rho Boo-Alp Boo", "Alp Boo-Eta Boo-Tau Boo", "Alp Boo-Zet Boo"),
    "CVn": ("Alp2 CVn-Bet CVn",),
    "Leo": ("Eps Leo-Mu Leo-Zet Leo-Gam1 Leo-Eta Leo-Alp Leo", "Alp Leo-The Leo-Bet Leo-Del Leo-Gam1 Leo", "The Leo-Del Leo"),
    "Cnc": ("Alp Cnc-Del Cnc-Bet Cnc", "Del Cnc-Gam Cnc-Iot Cnc"),
    "Gem": ("Alp Gem-Tau Gem-Eps Gem-Mu Gem-Eta Gem", "Bet Gem-Ups Gem-Del Gem-Zet Gem-Gam Gem", "Tau Gem-Iot Gem-Ups Gem", "Eps Gem-Nu Gem"),
    "Aur": ("Alp Aur-Bet Aur-The Aur-Bet Tau-Iot Aur-Eps Aur-Alp Aur",),
    "Tau": ("Zet Tau-Alp Tau-The2 Tau-Gam Tau-Del1 Tau-Eps Tau-Bet Tau", "Gam Tau-Lam Tau-Omi Tau", "Gam Tau-Eta Tau"),
    "Ori": ("Alp Ori-Lam Ori-Gam Ori", "Alp Ori-Zet Ori-Eps Ori-Del Ori-Gam Ori", "Zet Ori-Kap Ori", "Del Ori-Bet Ori",
            "Gam Ori-Pi3 Ori-Pi4 Ori", "Pi3 Ori-Pi2 Ori-Omi2 Ori", "Alp Ori-Mu Ori-Xi Ori-Chi1 Ori"),
    "CMa": ("Bet CMa-Alp CMa-Omi2 CMa-Del CMa-Eps CMa", "Del CMa-Eta CMa", "Alp CMa-Iot CMa-Gam CMa", "Eps CMa-Zet CMa"),
    "CMi": ("Alp CMi-Bet CMi",),
    "Lep": ("Alp Lep-Bet Lep-Eps Lep-Mu Lep-Alp Lep", "Alp Lep-Zet Lep-Eta Lep", "Bet Lep-Gam Lep-Del Lep"),
    "Vir": ("Bet Vir-Eta Vir-Gam Vir-Del Vir-Eps Vir", "Gam Vir-The Vir-Alp Vir", "Del Vir-Zet Vir-Alp Vir", "Zet Vir-Tau Vir"),
    "Lib": ("Alp2 Lib-Bet Lib-Gam Lib-Alp2 Lib", "Alp2 Lib-Sig Lib", "Gam Lib-Ups Lib"),
    "Sco": ("Bet1 Sco-Del Sco-Pi Sco", "Del Sco-Sig Sco-Alp Sco-Tau Sco-Eps Sco-Mu1 Sco-Zet2 Sco-Eta Sco-The Sco-Iot1 Sco-Kap Sco-Lam Sco-Ups Sco"),
    "Oph": ("Alp Oph-Kap Oph-Del Oph-Eps Oph-Zet Oph-Eta Oph-Bet Oph-Alp Oph", "Eta Oph-Xi Oph", "Zet Oph-Phi Oph"),
    "Ser": ("Gam Ser-Bet Ser-Del Ser-Alp Ser-Eps Ser-Mu Ser", "Bet Ser-Kap Ser", "Eta Ser-The1 Ser"),
    "Sgr": ("Gam2 Sgr-Del Sgr-Eps Sgr-Gam2 Sgr", "Del Sgr-Lam Sgr-Phi Sgr-Del Sgr", "Phi Sgr-Sig Sgr-Tau Sgr-Zet Sgr-Phi Sgr", "Zet Sgr-Eps Sgr",
            "Lam Sgr-Mu Sgr"),
    "Cap": ("Alp2 Cap-Bet Cap-Psi Cap-Ome Cap-Zet Cap-Del Cap-Gam Cap-Iot Cap-The Cap-Alp2 Cap",),
    "Aqr": ("Eps Aqr-Bet Aqr-Alp Aqr-Gam Aqr-Zet1 Aqr-Eta Aqr", "Alp Aqr-The Aqr-Lam Aqr-Tau2 Aqr-Del Aqr", "Lam Aqr-Phi Aqr-Psi1 Aqr"),
    "Psc": ("Gam Psc-Kap Psc-Lam Psc-Iot Psc-The Psc-Gam Psc", "Iot Psc-Ome Psc-Del Psc-Eps Psc-Zet Psc-Mu Psc-Nu Psc-Alp Psc",
            "Alp Psc-Xi Psc", "Alp Psc-Omi Psc-Eta Psc-Rho Psc-Phi Psc-Ups Psc-Tau Psc"),
    "Peg": ("Alp Peg-Bet Peg-Alp And-Gam Peg-Alp Peg", "Alp Peg-Xi Peg-Zet Peg-The Peg-Eps Peg", "Bet Peg-Eta Peg-Pi2 Peg",
            "Bet Peg-Mu Peg-Lam Peg-Iot Peg-Kap Peg"),
    "And": ("Alp And-Del And-Bet And-Gam1 And", "Bet And-Mu And-Nu And", "Del And-Pi And", "Alp And-Pi And"),
    "Tri": ("Alp Tri-Bet Tri-Gam Tri-Alp Tri",),
    "Ari": ("F41 Ari-Alp Ari-Bet Ari-Gam2 Ari",),
    "Per": ("Eta Per-Gam Per-Alp Per-Del Per-Eps Per-Zet Per", "Alp Per-Kap Per-Bet Per-Rho Per", "Eps Per-Xi Per", "Alp Per-Psi Per"),
    "Cet": ("Alp Cet-Gam Cet-Del Cet-Omi Cet-Zet Cet-Tau Cet-Bet Cet-Iot Cet", "Alp Cet-Lam Cet-Mu Cet-Xi2 Cet-Nu Cet-Gam Cet",
            "Zet Cet-The Cet-Eta Cet-Bet Cet"),
    "Crv": ("Alp Crv-Eps Crv-Gam Crv-Del Crv-Bet Crv-Eps Crv",),
    "Crt": ("Alp Crt-Bet Crt-Gam Crt-Del Crt-Alp Crt", "Gam Crt-Zet Crt-Eta Crt", "Del Crt-Eps Crt-The Crt"),
    "Hya": ("Del Hya-Sig Hya-Eta Hya-Eps Hya-Del Hya", "Eps Hya-Zet Hya-Ome Hya-The Hya-Iot Hya-Alp Hya",
            "Alp Hya-Ups1 Hya-Lam Hya-Mu Hya-Nu Hya"),
    "Cen": ("Alp1 Cen-Bet Cen-Eps Cen-Zet Cen-Eta Cen", "Eps Cen-Gam Cen-Del Cen", "Zet Cen-Iot Cen"),
    "Cru": ("Alp1 Cru-Gam Cru", "Bet Cru-Del Cru"),
}


@lru_cache(maxsize=1)
def constellations():
    """Constellations dessinées, étoiles résolues (une désignation inconnue est ignorée)."""
    r = []
    for abr, chaines in FIGURES.items():
        seg = []
        for ch in chaines:
            e = [trouver(x) for x in ch.split("-")]
            for k in range(1, len(e)):
                a, b = e[k - 1], e[k]
                if a is not None and b is not None and a is not b:
                    seg.append((a, b))
        r.append(Constellation(abr, NOMS.get(abr, abr), tuple(seg)))
    return tuple(r)


def inconnues():
    """Désignations des figures absentes du catalogue (contrôle des données)."""
    vues, r = set(), []
    for chaines in FIGURES.values():
        for ch in chaines:
            for x in ch.split("-"):
                if x not in vues:
                    vues.add(x)
                    if trouver(x) is None:
                        r.append(x)
    return r
