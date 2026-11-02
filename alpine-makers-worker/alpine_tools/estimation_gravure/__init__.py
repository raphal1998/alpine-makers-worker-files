"""Estimation gravure laser (atool-estimation-gravure) : port de l'outil « Gravure » d'Alpine Prism. Contrat : docs/ALPINE_TOOLS.md.

TOOL est un littéral (lu par le site sans importer le module) ; le calcul est dans alpine_tools.estimation.
"""

TOOL = {
    'id': 'estimation-gravure',
    'name': 'Estimation gravure laser',
    'machine': 'calcul',
    'description': "Surface gravée, temps machine et prix d'une gravure laser, depuis un dessin ou une image.",
    'inputs': [
        {'key': 'fichier', 'label': 'Dessin (SVG, DXF) ou image (PNG, JPG, WEBP)', 'accept': ['.svg', '.dxf', '.png', '.jpg', '.jpeg', '.webp'], 'required': False, 'max_mb': 40},
    ],
    'params': [
        {'key': 'famille', 'label': 'Famille du matériau', 'type': 'choice', 'default': '', 'options': [{'value': '', 'label': 'Non précisée (vitesse générale)'}, {'value': 'LAMINÉS & FILMS', 'label': 'Laminés & films'}, {'value': 'BOIS', 'label': 'Bois'}, {'value': 'PLASTIQUES', 'label': 'Plastiques'}, {'value': 'MÉTAUX', 'label': 'Métaux'}, {'value': 'MINÉRAUX & VERRE', 'label': 'Minéraux & verre'}, {'value': 'CUIR, TEXTILES & PAPIER', 'label': 'Cuir, textiles & papier'}, {'value': 'SUPPORTS À GRAVER', 'label': 'Supports à graver'}, {'value': 'MOUSSES & CIRES', 'label': 'Mousses & cires'}], 'help': 'Choisit la vitesse de gravure préréglée.'},
        {'key': 'prix_m2', 'label': 'Prix de la plaque', 'type': 'number', 'min': 0.0, 'max': 100000.0, 'step': 0.05, 'default': 60.0, 'unit': 'CHF/m²'},
        {'key': 'matiere_client', 'label': 'Matière fournie par le client', 'type': 'bool', 'default': False},
        {'key': 'largeur_reelle_mm', 'label': 'Largeur réelle', 'type': 'number', 'min': 0.0, 'max': 5000.0, 'step': 0.1, 'default': 0.0, 'unit': 'mm', 'help': "Dessin : 0 = à l'échelle du fichier. Image : 0 = 100 mm."},
        {'key': 'longueur_mm', 'label': 'Longueur de la plaque', 'type': 'number', 'min': 0.0, 'max': 5000.0, 'step': 0.1, 'default': 0.0, 'unit': 'mm', 'help': '0 = valeur tirée du fichier importé.'},
        {'key': 'largeur_mm', 'label': 'Largeur de la plaque', 'type': 'number', 'min': 0.0, 'max': 5000.0, 'step': 0.1, 'default': 0.0, 'unit': 'mm', 'help': '0 = valeur tirée du fichier importé.'},
        {'key': 'epaisseur_mm', 'label': 'Épaisseur', 'type': 'number', 'min': 0.0, 'max': 200.0, 'step': 0.1, 'default': 3.0, 'unit': 'mm'},
        {'key': 'surface_gravee_cm2', 'label': 'Surface gravée', 'type': 'number', 'min': 0.0, 'max': 1000000.0, 'step': 0.1, 'default': 0.0, 'unit': 'cm²', 'help': '0 = valeur tirée du fichier importé.'},
        {'key': 'passes', 'label': 'Passes', 'type': 'number', 'min': 1, 'max': 20, 'step': 1, 'default': 1, 'integer': True},
        {'key': 'vitesse_gravure_cm2_min', 'label': 'Vitesse de gravure', 'type': 'number', 'min': 0.0, 'max': 10000.0, 'step': 0.1, 'default': 0.0, 'unit': 'cm²/min', 'help': "0 = préréglage de la famille, sinon vitesse générale de l'atelier."},
        {'key': 'temps_machine_min', 'label': 'Temps machine', 'type': 'number', 'min': 0.0, 'max': 100000.0, 'step': 1.0, 'default': 0.0, 'unit': 'min', 'help': '0 = temps estimé à partir des vitesses.'},
        {'key': 'consommables', 'label': 'Consommables / pièce', 'type': 'number', 'min': 0.0, 'max': 1000.0, 'step': 0.05, 'default': 0.3, 'unit': 'CHF'},
        {'key': 'quantite', 'label': 'Quantité', 'type': 'number', 'min': 1, 'max': 100000, 'step': 1, 'default': 1, 'unit': 'pcs', 'integer': True},
        {'key': 'marge_pct', 'label': 'Marge', 'type': 'number', 'min': 0.0, 'max': 500.0, 'step': 0.5, 'default': 40.0, 'unit': '%'},
        {'key': 'remise_pct', 'label': 'Remise client', 'type': 'number', 'min': 0.0, 'max': 100.0, 'step': 0.5, 'default': 0.0, 'unit': '%'},
        {'key': 'taux_echec_pct', 'label': "Taux d'échec", 'type': 'number', 'min': 0.0, 'max': 100.0, 'step': 0.5, 'default': 3.0, 'unit': '%'},
        {'key': 'dechets_pct', 'label': 'Déchets / chutes', 'type': 'number', 'min': 0.0, 'max': 300.0, 'step': 0.5, 'default': 15.0, 'unit': '%'},
        {'key': 'temps_assemblage_min', 'label': 'Assemblage', 'type': 'number', 'min': 0.0, 'max': 10000.0, 'step': 1.0, 'default': 0.0, 'unit': 'min'},
        {'key': 'temps_finition_min', 'label': 'Finition', 'type': 'number', 'min': 0.0, 'max': 10000.0, 'step': 1.0, 'default': 0.0, 'unit': 'min'},
        {'key': 'frais_fixes', 'label': 'Frais fixes commande', 'type': 'number', 'min': 0.0, 'max': 100000.0, 'step': 0.05, 'default': 0.0, 'unit': 'CHF'},
        {'key': 'tva_pct', 'label': 'TVA', 'type': 'number', 'min': 0.0, 'max': 100.0, 'step': 0.1, 'default': 8.1, 'unit': '%'},
    ],
}


def run(params, inputs, out_dir, progress):
    from alpine_tools.estimation import outils

    return outils.executer("gravure", params, inputs, out_dir, progress)
