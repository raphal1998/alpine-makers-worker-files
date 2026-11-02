"""Commun des cinq estimations d'Alpine Prism (seule exception permise à l'indépendance des Alpine Tools).

- calcul.py : prix d'une pièce (port de Calcul.kt = utils/estimation3d.js de la boutique) ;
- maillage.py : STL/3MF/OBJ, volume, surface, supports, estimation filament/résine (Maillage.kt) ;
- vectoriel.py : SVG/DXF, longueur de coupe, surface (Vectoriel.kt) ;
- image.py : part gravée d'une image (Import.kt) ;
- outils.py : exécution d'un outil (fichier → champs → prix → détails, résumé, devis CSV, aperçu).

Les modules lourds (numpy, Pillow) ne sont importés qu'à l'exécution.
"""
