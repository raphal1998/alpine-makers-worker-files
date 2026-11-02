"""Estimation fraisage CNC (atool-estimation-cnc) : port de l'outil « CNC » d'Alpine Prism. Contrat : docs/ALPINE_TOOLS.md.

TOOL est un littéral (lu par le site sans importer le module) ; le calcul est dans alpine_tools.estimation.
"""

TOOL = {
    'id': 'estimation-cnc',
    'name': 'Estimation fraisage CNC',
    'machine': 'calcul',
    'description': "Volume enlevé, changements d'outil, temps machine et prix d'un fraisage CNC, depuis le modèle 3D de la pièce.",
    'inputs': [
        {'key': 'modele', 'label': 'Modèle 3D de la pièce (STL, 3MF, OBJ)', 'accept': ['.stl', '.3mf', '.obj'], 'required': False, 'max_mb': 40},
    ],
    'params': [
        {'key': 'famille', 'label': 'Famille du matériau', 'type': 'choice', 'default': '', 'options': [{'value': '', 'label': 'Non précisée (vitesse générale)'}, {'value': 'MÉTAUX', 'label': 'Métaux'}, {'value': 'PLASTIQUES', 'label': 'Plastiques'}, {'value': 'MOUSSES & CIRES', 'label': 'Mousses & cires'}, {'value': 'BOIS', 'label': 'Bois'}], 'help': 'Choisit le débit de fraisage préréglé.'},
        {'key': 'prix_m2', 'label': 'Prix du brut', 'type': 'number', 'min': 0.0, 'max': 100000.0, 'step': 0.05, 'default': 150.0, 'unit': 'CHF/m²'},
        {'key': 'matiere_client', 'label': 'Matière fournie par le client', 'type': 'bool', 'default': False},
        {'key': 'longueur_mm', 'label': 'Longueur du brut', 'type': 'number', 'min': 0.0, 'max': 5000.0, 'step': 0.1, 'default': 0.0, 'unit': 'mm', 'help': '0 = valeur tirée du fichier importé.'},
        {'key': 'largeur_mm', 'label': 'Largeur du brut', 'type': 'number', 'min': 0.0, 'max': 5000.0, 'step': 0.1, 'default': 0.0, 'unit': 'mm', 'help': '0 = valeur tirée du fichier importé.'},
        {'key': 'epaisseur_mm', 'label': 'Épaisseur du brut', 'type': 'number', 'min': 0.0, 'max': 1000.0, 'step': 0.1, 'default': 0.0, 'unit': 'mm', 'help': '0 = valeur tirée du fichier importé.'},
        {'key': 'volume_enleve_cm3', 'label': 'Volume enlevé', 'type': 'number', 'min': 0.0, 'max': 1000000.0, 'step': 0.1, 'default': 0.0, 'unit': 'cm³', 'help': '0 = brut moins la pièce du modèle importé.'},
        {'key': 'nb_outils', 'label': "Changements d'outil", 'type': 'number', 'min': 0, 'max': 100, 'step': 1, 'default': 2, 'integer': True},
        {'key': 'debit_cm3_min', 'label': 'Débit', 'type': 'number', 'min': 0.0, 'max': 10000.0, 'step': 0.1, 'default': 0.0, 'unit': 'cm³/min', 'help': "0 = préréglage de la famille, sinon vitesse générale de l'atelier."},
        {'key': 'temps_machine_min', 'label': 'Temps machine', 'type': 'number', 'min': 0.0, 'max': 100000.0, 'step': 1.0, 'default': 0.0, 'unit': 'min', 'help': '0 = temps estimé à partir des vitesses.'},
        {'key': 'consommables', 'label': 'Consommables / pièce', 'type': 'number', 'min': 0.0, 'max': 1000.0, 'step': 0.05, 'default': 0.5, 'unit': 'CHF'},
        {'key': 'quantite', 'label': 'Quantité', 'type': 'number', 'min': 1, 'max': 100000, 'step': 1, 'default': 1, 'unit': 'pcs', 'integer': True},
        {'key': 'marge_pct', 'label': 'Marge', 'type': 'number', 'min': 0.0, 'max': 500.0, 'step': 0.5, 'default': 40.0, 'unit': '%'},
        {'key': 'remise_pct', 'label': 'Remise client', 'type': 'number', 'min': 0.0, 'max': 100.0, 'step': 0.5, 'default': 0.0, 'unit': '%'},
        {'key': 'taux_echec_pct', 'label': "Taux d'échec", 'type': 'number', 'min': 0.0, 'max': 100.0, 'step': 0.5, 'default': 5.0, 'unit': '%'},
        {'key': 'dechets_pct', 'label': 'Déchets / chutes', 'type': 'number', 'min': 0.0, 'max': 300.0, 'step': 0.5, 'default': 30.0, 'unit': '%'},
        {'key': 'temps_assemblage_min', 'label': 'Assemblage', 'type': 'number', 'min': 0.0, 'max': 10000.0, 'step': 1.0, 'default': 0.0, 'unit': 'min'},
        {'key': 'temps_finition_min', 'label': 'Finition', 'type': 'number', 'min': 0.0, 'max': 10000.0, 'step': 1.0, 'default': 0.0, 'unit': 'min'},
        {'key': 'frais_fixes', 'label': 'Frais fixes commande', 'type': 'number', 'min': 0.0, 'max': 100000.0, 'step': 0.05, 'default': 0.0, 'unit': 'CHF'},
        {'key': 'tva_pct', 'label': 'TVA', 'type': 'number', 'min': 0.0, 'max': 100.0, 'step': 0.1, 'default': 8.1, 'unit': '%'},
    ],
}


def run(params, inputs, out_dir, progress):
    from alpine_tools.estimation import outils

    return outils.executer("cnc", params, inputs, out_dir, progress)
