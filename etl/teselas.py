"""Teselas PMTiles locales de Regu Suelo (docs/REGU_SUELO_LOCAL.md): normativa, ocupación y comunas.

`python run.py teselas [--tema normativa|ocupacion|comunas]` las escribe en `<paths.out>/tiles/<tema>.pmtiles` y la
aplicación las sirve en `/tiles/<tema>.pmtiles`. Se generan con el driver PMTiles de GDAL (como `etl/mapa.py`); los
edificios NO se tesela: se piden por bbox con DuckDB (`app/edificios.py`).

Capas de origen (el nombre que usa MapLibre en `source-layer`):
- normativa → `ipt` (clase, ipt_tipo, ipt_nombre, zona, norma_titulo, riesgo, comuna, cut y la vigencia del Portal IPT);
- ocupacion → `ocupacion` (ipt, zona, ha, m2_huella, coef_ocupacion, n_edificios, tramo, color);
- comunas → `comunas` (cut, nombre, region), simplificadas.
"""
from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import pyogrio

from . import mapa

log = logging.getLogger(__name__)

TEMAS = ("normativa", "ocupacion", "comunas")
CAPA_ORIGEN = {"normativa": "ipt", "ocupacion": "ocupacion", "comunas": "comunas"}
ZOOM = {"normativa": (5, 14), "ocupacion": (5, 14), "comunas": (3, 12)}


def escribir(g: gpd.GeoDataFrame, destino: Path, capa: str, minzoom: int, maxzoom: int, nombre: str) -> Path:
    """GeoDataFrame → PMTiles (UTF-8 obligatorio: sin él pyogrio escribe cp1252 en Windows y el MVT exige UTF-8)."""
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.unlink(missing_ok=True)
    pyogrio.write_dataframe(g.to_crs(4326), destino, driver="PMTiles", layer=capa, encoding="UTF-8",
                            dataset_options={"MINZOOM": str(minzoom), "MAXZOOM": str(maxzoom), "MAX_SIZE": "4000000",
                                             "MAX_FEATURES": "500000", "SIMPLIFICATION": "1", "NAME": nombre,
                                             "DESCRIPTION": "Regu Suelo local (regu-ipt-etl)"})
    return destino


def normativa(gpkg: Path, destino: Path) -> Path:
    """Partición IPT con la vigencia del Portal IPT (norma, fecha y ordenanza) en cada pieza."""
    capa = mapa.unir_vigencia(gpd.read_file(gpkg, layer="capa_ipt"), Path(gpkg).parent / "vigencia_match.csv")
    mn, mx = ZOOM["normativa"]
    Path(destino).parent.mkdir(parents=True, exist_ok=True)
    return mapa.generar_pmtiles(capa, Path(destino), minzoom=mn, maxzoom=mx)


def ocupacion(gpkg_ocupacion: Path, destino: Path) -> Path:
    """Zonas PRC coloreadas por coeficiente de ocupación (salida de `run.py ocupacion`, capa `ocupacion_zonas`)."""
    g = gpd.read_file(gpkg_ocupacion, layer="ocupacion_zonas")
    mn, mx = ZOOM["ocupacion"]
    return escribir(g, destino, CAPA_ORIGEN["ocupacion"], mn, mx, "Ocupación por zona")


def comunas(shp: Path, f_cut: str, f_nombre: str, f_region: str, destino: Path) -> Path:
    """Límites comunales simplificados (≈ 40 m) para el mapa base y el buscador."""
    c = gpd.read_file(shp)
    g = c[[f_cut, f_nombre, f_region, "geometry"]].to_crs("ESRI:102033")
    g["geometry"] = g.geometry.simplify(40)
    g = g.rename(columns={f_cut: "cut", f_nombre: "nombre", f_region: "region"})
    g["cut"] = [str(int(x)).zfill(5) if str(x).strip().isdigit() else str(x) for x in g["cut"]]
    mn, mx = ZOOM["comunas"]
    return escribir(g, destino, CAPA_ORIGEN["comunas"], mn, mx, "Comunas")
