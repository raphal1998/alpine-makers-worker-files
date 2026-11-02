"""Estimation découpe laser (atool-estimation-decoupe) : port de l'outil « Découpe » d'Alpine Prism. Contrat : docs/ALPINE_TOOLS.md.

TOOL est un littéral (lu par le site sans importer le module) ; le calcul est dans alpine_tools.estimation.
"""

TOOL = {
    'id': 'estimation-decoupe',
    'name': 'Estimation découpe laser',
    'machine': 'calcul',
    'description': "Longueur de coupe, perçages, temps machine et prix d'une découpe laser, depuis un dessin.",
    'inputs': [
        {'key': 'dessin', 'label': 'Dessin de découpe (SVG, DXF)', 'accept': ['.svg', '.dxf'], 'required': False, 'max_mb': 40},
    ],
    'params': [
        {'key': 'famille', 'label': 'Famille du matériau', 'type': 'choice', 'default': '', 'options': [{'value': '', 'label': 'Non précisée (vitesse générale)'}, {'value': 'BOIS', 'label': 'Bois'}, {'value': 'PLASTIQUES', 'label': 'Plastiques'}, {'value': 'CUIR, TEXTILES & PAPIER', 'label': 'Cuir, textiles & papier'}, {'value': 'MOUSSES & CIRES', 'label': 'Mousses & cires'}, {'value': 'LAMINÉS & FILMS', 'label': 'Laminés & films'}], 'help': 'Choisit les vitesses de coupe et de marquage préréglées.'},
        {'key': 'prix_m2', 'label': 'Prix de la plaque', 'type': 'number', 'min': 0.0, 'max': 100000.0, 'step': 0.05, 'default': 45.0, 'unit': 'CHF/m²'},
        {'key': 'matiere_client', 'label': 'Matière fournie par le client', 'type': 'bool', 'default': False},
        {'key': 'epaisseur_mm', 'label': 'Épaisseur', 'type': 'number', 'min': 0.0, 'max': 200.0, 'step': 0.1, 'default': 3.0, 'unit': 'mm', 'help': 'Au-delà de 3 mm, la vitesse de coupe baisse en proportion.'},
        {'key': 'largeur_reelle_mm', 'label': 'Largeur réelle', 'type': 'number', 'min': 0.0, 'max': 5000.0, 'step': 0.1, 'default': 0.0, 'unit': 'mm', 'help': "0 = à l'échelle du fichier."},
        {'key': 'longueur_mm', 'label': 'Longueur de la plaque', 'type': 'number', 'min': 0.0, 'max': 5000.0, 'step': 0.1, 'default': 0.0, 'unit': 'mm', 'help': '0 = valeur tirée du fichier importé.'},
        {'key': 'largeur_mm', 'label': 'Largeur de la plaque', 'type': 'number', 'min': 0.0, 'max': 5000.0, 'step': 0.1, 'default': 0.0, 'unit': 'mm', 'help': '0 = valeur tirée du fichier importé.'},
        {'key': 'longueur_coupe_m', 'label': 'Longueur de coupe', 'type': 'number', 'min': 0.0, 'max': 100000.0, 'step': 0.01, 'default': 0.0, 'unit': 'm', 'help': '0 = valeur tirée du fichier importé.'},
        {'key': 'nb_percages', 'label': 'Perçages', 'type': 'number', 'min': 0, 'max': 100000, 'step': 1, 'default': 0, 'integer': True, 'help': '0 = un perçage par tracé du dessin.'},
        {'key': 'surface_gravee_cm2', 'label': 'Marquage gravé', 'type': 'number', 'min': 0.0, 'max': 1000000.0, 'step': 0.1, 'default': 0.0, 'unit': 'cm²'},
        {'key': 'vitesse_coupe_mm_min', 'label': 'Vitesse de coupe', 'type': 'number', 'min': 0.0, 'max': 100000.0, 'step': 1.0, 'default': 0.0, 'unit': 'mm/min', 'help': "0 = préréglage de la famille, sinon vitesse générale de l'atelier."},
        {'key': 'vitesse_gravure_cm2_min', 'label': 'Vitesse de marquage', 'type': 'number', 'min': 0.0, 'max': 10000.0, 'step': 0.1, 'default': 0.0, 'unit': 'cm²/min', 'help': "0 = préréglage de la famille, sinon vitesse générale de l'atelier."},
        {'key': 'temps_machine_min', 'label': 'Temps machine', 'type': 'number', 'min': 0.0, 'max': 100000.0, 'step': 1.0, 'default': 0.0, 'unit': 'min', 'help': '0 = temps estimé à partir des vitesses.'},
        {'key': 'consommables', 'label': 'Consommables / pièce', 'type': 'number', 'min': 0.0, 'max': 1000.0, 'step': 0.05, 'default': 0.3, 'unit': 'CHF'},
        {'key': 'quantite', 'label': 'Quantité', 'type': 'number', 'min': 1, 'max': 100000, 'step': 1, 'default': 1, 'unit': 'pcs', 'integer': True},
        {'key': 'marge_pct', 'label': 'Marge', 'type': 'number', 'min': 0.0, 'max': 500.0, 'step': 0.5, 'default': 40.0, 'unit': '%'},
        {'key': 'remise_pct', 'label': 'Remise client', 'type': 'number', 'min': 0.0, 'max': 100.0, 'step': 0.5, 'default': 0.0, 'unit': '%'},
        {'key': 'taux_echec_pct', 'label': "Taux d'échec", 'type': 'number', 'min': 0.0, 'max': 100.0, 'step': 0.5, 'default': 4.0, 'unit': '%'},
        {'key': 'dechets_pct', 'label': 'Déchets / chutes', 'type': 'number', 'min': 0.0, 'max': 300.0, 'step': 0.5, 'default': 25.0, 'unit': '%'},
        {'key': 'temps_assemblage_min', 'label': 'Assemblage', 'type': 'number', 'min': 0.0, 'max': 10000.0, 'step': 1.0, 'default': 0.0, 'unit': 'min'},
        {'key': 'temps_finition_min', 'label': 'Finition', 'type': 'number', 'min': 0.0, 'max': 10000.0, 'step': 1.0, 'default': 0.0, 'unit': 'min'},
        {'key': 'frais_fixes', 'label': 'Frais fixes commande', 'type': 'number', 'min': 0.0, 'max': 100000.0, 'step': 0.05, 'default': 0.0, 'unit': 'CHF'},
        {'key': 'tva_pct', 'label': 'TVA', 'type': 'number', 'min': 0.0, 'max': 100.0, 'step': 0.1, 'default': 8.1, 'unit': '%'},
    ],
}


def run(params, inputs, out_dir, progress):
    from alpine_tools.estimation import outils

    return outils.executer("decoupe", params, inputs, out_dir, progress)
