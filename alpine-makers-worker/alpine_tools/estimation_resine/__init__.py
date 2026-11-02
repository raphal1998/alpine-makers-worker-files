"""Estimation impression résine (atool-estimation-resine) : port de l'outil « Résine » d'Alpine Prism. Contrat : docs/ALPINE_TOOLS.md.

TOOL est un littéral (lu par le site sans importer le module) ; le calcul est dans alpine_tools.estimation.
"""

TOOL = {
    'id': 'estimation-resine',
    'name': 'Estimation impression résine',
    'machine': 'calcul',
    'description': "Volume de résine, temps d'impression, cuisson UV et prix d'une pièce, depuis son modèle 3D.",
    'inputs': [
        {'key': 'modele', 'label': 'Modèle 3D (STL, 3MF, OBJ)', 'accept': ['.stl', '.3mf', '.obj'], 'required': False, 'max_mb': 40},
    ],
    'params': [
        {'key': 'prix_kg', 'label': 'Prix de la résine', 'type': 'number', 'min': 0.0, 'max': 2000.0, 'step': 0.05, 'default': 60.0, 'unit': 'CHF/kg'},
        {'key': 'hauteur_couche', 'label': 'Hauteur de couche', 'type': 'choice', 'default': '0.05', 'options': [{'value': '0.03', 'label': '0,03 mm'}, {'value': '0.05', 'label': '0,05 mm'}, {'value': '0.1', 'label': '0,1 mm'}]},
        {'key': 'supports', 'label': 'Supports et radeau', 'type': 'bool', 'default': True},
        {'key': 'volume_ml', 'label': 'Volume de résine', 'type': 'number', 'min': 0.0, 'max': 100000.0, 'step': 0.1, 'default': 0.0, 'unit': 'ml', 'help': '0 = valeur tirée du fichier importé.'},
        {'key': 'temps_impression_min', 'label': "Temps d'impression", 'type': 'number', 'min': 0.0, 'max': 100000.0, 'step': 1.0, 'default': 0.0, 'unit': 'min', 'help': '0 = valeur tirée du fichier importé.'},
        {'key': 'duree_cure_min', 'label': 'Cuisson UV', 'type': 'number', 'min': 0.0, 'max': 1000.0, 'step': 0.5, 'default': 10.0, 'unit': 'min'},
        {'key': 'consommables', 'label': 'Consommables / pièce', 'type': 'number', 'min': 0.0, 'max': 1000.0, 'step': 0.05, 'default': 0.6, 'unit': 'CHF', 'help': 'Alcool, gants, papier…'},
        {'key': 'quantite', 'label': 'Quantité', 'type': 'number', 'min': 1, 'max': 100000, 'step': 1, 'default': 1, 'unit': 'pcs', 'integer': True},
        {'key': 'marge_pct', 'label': 'Marge', 'type': 'number', 'min': 0.0, 'max': 500.0, 'step': 0.5, 'default': 40.0, 'unit': '%'},
        {'key': 'remise_pct', 'label': 'Remise client', 'type': 'number', 'min': 0.0, 'max': 100.0, 'step': 0.5, 'default': 0.0, 'unit': '%'},
        {'key': 'taux_echec_pct', 'label': "Taux d'échec", 'type': 'number', 'min': 0.0, 'max': 100.0, 'step': 0.5, 'default': 8.0, 'unit': '%'},
        {'key': 'dechets_pct', 'label': 'Déchets / chutes', 'type': 'number', 'min': 0.0, 'max': 300.0, 'step': 0.5, 'default': 15.0, 'unit': '%'},
        {'key': 'temps_assemblage_min', 'label': 'Assemblage', 'type': 'number', 'min': 0.0, 'max': 10000.0, 'step': 1.0, 'default': 0.0, 'unit': 'min'},
        {'key': 'temps_finition_min', 'label': 'Finition', 'type': 'number', 'min': 0.0, 'max': 10000.0, 'step': 1.0, 'default': 0.0, 'unit': 'min'},
        {'key': 'frais_fixes', 'label': 'Frais fixes commande', 'type': 'number', 'min': 0.0, 'max': 100000.0, 'step': 0.05, 'default': 0.0, 'unit': 'CHF'},
        {'key': 'tva_pct', 'label': 'TVA', 'type': 'number', 'min': 0.0, 'max': 100.0, 'step': 0.1, 'default': 8.1, 'unit': '%'},
    ],
}


def run(params, inputs, out_dir, progress):
    from alpine_tools.estimation import outils

    return outils.executer("resine", params, inputs, out_dir, progress)
