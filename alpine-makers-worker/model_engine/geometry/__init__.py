"""Noyau géométrique 2D d'Alpine Model Engine (§3 de docs/MODEL_ENGINE_ARCHITECTURE.md).

Unités : micromètres entiers (``numpy.int64``), ``SCALE = 1000`` (1 mm = 1000 µm).
Ring : tableau int64 ``(n, 2)`` fermé implicitement, aire signée > 0 = contour extérieur (CCW), < 0 = trou (CW).
Paths : ``list[Ring]`` en règle de remplissage positive (indice d'enroulement > 0 = plein).
Polygon : ``(outer, holes)`` ; PolyTree : ``list[Polygon]``. Segments : int64 ``(m, 2, 2)``.
"""

SCALE = 1000
EPS_UM = 1
RESOLUTION_UM = 12

__all__ = ["SCALE", "EPS_UM", "RESOLUTION_UM"]
