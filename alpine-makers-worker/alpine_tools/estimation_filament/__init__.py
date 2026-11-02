"""Estimation impression filament (atool-estimation-filament) : port de l'outil « Filament » d'Alpine Prism. Contrat : docs/ALPINE_TOOLS.md.

TOOL est un littéral (lu par le site sans importer le module) ; le calcul est dans alpine_tools.estimation.
"""

TOOL = {
    'id': 'estimation-filament',
    'name': 'Estimation impression filament',
    'machine': 'calcul',
    'description': "Poids, temps d'impression et prix d'une pièce imprimée en filament, depuis son modèle 3D.",
    'inputs': [
        {'key': 'modele', 'label': 'Modèle 3D (STL, 3MF, OBJ)', 'accept': ['.stl', '.3mf', '.obj'], 'required': False, 'max_mb': 40},
    ],
    'params': [
        {'key': 'type_filament', 'label': 'Type de filament', 'type': 'choice', 'default': 'PLA', 'options': [{'value': 'PLA', 'label': 'PLA (1,24 g/cm³)'}, {'value': 'PETG', 'label': 'PETG (1,27 g/cm³)'}, {'value': 'PCTG', 'label': 'PCTG (1,27 g/cm³)'}, {'value': 'ABS', 'label': 'ABS (1,04 g/cm³)'}, {'value': 'ASA', 'label': 'ASA (1,07 g/cm³)'}, {'value': 'HIPS', 'label': 'HIPS (1,04 g/cm³)'}, {'value': 'TPU', 'label': 'TPU (1,21 g/cm³)'}, {'value': 'PA', 'label': 'PA / nylon (1,14 g/cm³)'}, {'value': 'PC', 'label': 'PC (1,20 g/cm³)'}, {'value': 'PVA', 'label': 'PVA (1,23 g/cm³)'}, {'value': 'BVOH', 'label': 'BVOH (1,23 g/cm³)'}, {'value': 'PP', 'label': 'PP (0,90 g/cm³)'}, {'value': 'LW-PLA', 'label': 'LW-PLA (0,8 g/cm³)'}], 'help': 'Donne la densité du filament.'},
        {'key': 'prix_kg', 'label': 'Prix du filament', 'type': 'number', 'min': 0.0, 'max': 2000.0, 'step': 0.05, 'default': 25.0, 'unit': 'CHF/kg'},
        {'key': 'remplissage_pct', 'label': 'Remplissage', 'type': 'number', 'min': 0, 'max': 100, 'step': 5, 'default': 15, 'unit': '%', 'integer': True},
        {'key': 'hauteur_couche', 'label': 'Hauteur de couche', 'type': 'choice', 'default': '0.2', 'options': [{'value': '0.12', 'label': '0,12 mm'}, {'value': '0.16', 'label': '0,16 mm'}, {'value': '0.2', 'label': '0,2 mm'}, {'value': '0.24', 'label': '0,24 mm'}, {'value': '0.28', 'label': '0,28 mm'}]},
        {'key': 'parois', 'label': 'Parois', 'type': 'choice', 'default': '2', 'options': [{'value': '2', 'label': '2'}, {'value': '3', 'label': '3'}, {'value': '4', 'label': '4'}]},
        {'key': 'supports', 'label': 'Supports (surplombs > 45°)', 'type': 'bool', 'default': True},
        {'key': 'poids_g', 'label': 'Poids de la pièce', 'type': 'number', 'min': 0.0, 'max': 100000.0, 'step': 0.1, 'default': 0.0, 'unit': 'g', 'help': '0 = valeur tirée du fichier importé.'},
        {'key': 'temps_impression_min', 'label': "Temps d'impression", 'type': 'number', 'min': 0.0, 'max': 100000.0, 'step': 1.0, 'default': 0.0, 'unit': 'min', 'help': '0 = valeur tirée du fichier importé.'},
        {'key': 'quantite', 'label': 'Quantité', 'type': 'number', 'min': 1, 'max': 100000, 'step': 1, 'default': 1, 'unit': 'pcs', 'integer': True},
        {'key': 'marge_pct', 'label': 'Marge', 'type': 'number', 'min': 0.0, 'max': 500.0, 'step': 0.5, 'default': 40.0, 'unit': '%'},
        {'key': 'remise_pct', 'label': 'Remise client', 'type': 'number', 'min': 0.0, 'max': 100.0, 'step': 0.5, 'default': 0.0, 'unit': '%'},
        {'key': 'taux_echec_pct', 'label': "Taux d'échec", 'type': 'number', 'min': 0.0, 'max': 100.0, 'step': 0.5, 'default': 5.0, 'unit': '%'},
        {'key': 'dechets_pct', 'label': 'Déchets / chutes', 'type': 'number', 'min': 0.0, 'max': 300.0, 'step': 0.5, 'default': 8.0, 'unit': '%'},
        {'key': 'temps_assemblage_min', 'label': 'Assemblage', 'type': 'number', 'min': 0.0, 'max': 10000.0, 'step': 1.0, 'default': 0.0, 'unit': 'min'},
        {'key': 'temps_finition_min', 'label': 'Finition', 'type': 'number', 'min': 0.0, 'max': 10000.0, 'step': 1.0, 'default': 0.0, 'unit': 'min'},
        {'key': 'frais_fixes', 'label': 'Frais fixes commande', 'type': 'number', 'min': 0.0, 'max': 100000.0, 'step': 0.05, 'default': 0.0, 'unit': 'CHF'},
        {'key': 'tva_pct', 'label': 'TVA', 'type': 'number', 'min': 0.0, 'max': 100.0, 'step': 0.1, 'default': 8.1, 'unit': '%'},
    ],
}


def run(params, inputs, out_dir, progress):
    from alpine_tools.estimation import outils

    return outils.executer("filament", params, inputs, out_dir, progress)
