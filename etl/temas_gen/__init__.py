"""Generadores de los temas de `temas/*.yaml` (docs/MAPAS_TEMATICOS.md).

Importar este paquete registra los generadores (`@etl.temas.generador("nombre")`). Cada módulo produce el PMTiles de un
tipo de fuente: `division` (vectorial, BCN), y los de ráster `worldclim`, `soilgrids`, `glo30` y `worldcover`.
"""
from . import division  # noqa: F401
