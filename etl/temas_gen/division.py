"""Tema de ubicación: regiones coloreadas y límites comunales (BCN SIIT) en PMTiles vectorial."""
from __future__ import annotations

import geopandas as gpd

from .. import teselas
from ..temas import generador
from .comun import ruta_cfg

# Código de región (los dos primeros dígitos del CUT), de norte a sur, con un color categórico por región
ORDEN = ["15", "01", "02", "03", "04", "05", "13", "06", "07", "16", "08", "09", "14", "10", "11", "12"]
COLORES = ["#8dd3c7", "#ffffb3", "#bebada", "#fb8072", "#80b1d3", "#fdb462", "#b3de69", "#fccde5", "#d9d9d9", "#bc80bd",
           "#ccebc5", "#ffed6f", "#a6cee3", "#fdbf6f", "#cab2d6", "#b2df8a"]
SIN_DEMARCAR = "#e0e0e0"


def color_region(cod: str) -> str:
    return COLORES[ORDEN.index(cod)] if cod in ORDEN else SIN_DEMARCAR


@generador("division")
def division(c, dir_raw, destino, cfg):
    k = cfg["comunas"]
    g = gpd.read_file(ruta_cfg(cfg, "comunas"))
    g["cut"] = [str(int(x)).zfill(5) for x in g[k["field_cut"]]]
    g["color"] = [color_region(x[:2]) for x in g["cut"]]
    g = g.rename(columns={k["field_nombre"]: "nombre", k["field_region"]: "region"})[["cut", "nombre", "region", "color", "geometry"]]
    z0, z1 = c["zoom"]
    return teselas.escribir(g, destino, c["estilo"]["capa_origen"], z0, z1, c["nombre"])
