"""Footprints de Overture (ODbL) leídos directo de los GeoParquet de `paths.footprints` con DuckDB spatial.

No se tesela el país: se piden los edificios del área visible, con tope de N y filtro por comuna (`cut`), que es lo que
permite a DuckDB saltarse casi todo el archivo. Offline: la extensión spatial se instala una vez (necesita red) y luego
se carga desde la caché de DuckDB.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import duckdb
import geopandas as gpd
import shapely
from shapely.geometry.base import BaseGeometry

from etl.ocupacion import regiones_con_footprints

from . import datos

ATRIBUCION = "© OpenStreetMap contributors, Overture Maps Foundation (ODbL)"
LIMITE_DEF, LIMITE_MAX = 5000, 20000
SPAN_MAX_GRADOS = 0.15          # área visible máxima (≈ 12 km): más grande se pide acercarse
_RE_CUT = re.compile(r"^\d{4,5}$")
_LOCK = threading.Lock()
_CON: dict = {}


def _conexion() -> duckdb.DuckDBPyConnection:
    with _LOCK:
        if "con" not in _CON:
            con = duckdb.connect()
            try:
                con.execute("LOAD spatial")
            except Exception:
                con.execute("INSTALL spatial")
                con.execute("LOAD spatial")
            _CON["con"] = con
        return _CON["con"].cursor()


def tolerancia(zoom: float | None) -> float:
    """Tolerancia de simplificación en grados según el zoom (0 desde el zoom 16: la forma real)."""
    if zoom is None or zoom >= 16:
        return 0.0
    return 1.2e-5 * 2 ** (16 - zoom - 1)


def _archivos(a, bbox: tuple[float, float, float, float]) -> list[tuple[Path, list[str] | None]]:
    """[(parquet, cuts)] de las regiones con footprints cuyas comunas cruzan el bbox. cuts=None si no hay lista de comunas."""
    regiones = regiones_con_footprints(a.dir_footprints)
    w, s, e, n = bbox
    por_region: dict[str, list[str]] = {}
    for c in datos.comunas(a):
        cw, cs, ce, cn = c["bbox"]
        if cw <= e and ce >= w and cs <= n and cn >= s and _RE_CUT.match(c["cut"]):
            por_region.setdefault(c["region"], []).append(c["cut"])
    if not datos.comunas(a):
        return [(p, None) for p in regiones.values()]
    return [(regiones[r], cuts) for r, cuts in por_region.items() if r in regiones]


def _filtro_cut(cuts: list[str] | None) -> str:
    return "" if not cuts else "cut IN (" + ",".join(f"'{c}'" for c in cuts) + ") AND "


def edificios_bbox(a, bbox: tuple[float, float, float, float], limite: int = LIMITE_DEF, zoom: float | None = None) -> dict:
    """FeatureCollection de los edificios del bbox (w, s, e, n) con `altura_est`; `recortado` si hay más que `limite`."""
    limite = max(1, min(int(limite), LIMITE_MAX))
    w, s, e, n = bbox
    base = {"type": "FeatureCollection", "features": [], "recortado": False, "limite": limite, "atribucion": ATRIBUCION,
            "aviso": None}
    if (e - w) > SPAN_MAX_GRADOS or (n - s) > SPAN_MAX_GRADOS:
        return {**base, "aviso": "El área visible es muy grande: acércate para ver los edificios."}
    tol = tolerancia(zoom)
    geom = f"ST_SimplifyPreserveTopology(geometry, {tol})" if tol > 0 else "geometry"
    con, filas = _conexion(), []
    for path, cuts in _archivos(a, bbox):
        sql = (f"select id, height, num_floors, area_m2, fuente, ST_AsGeoJSON({geom}) from read_parquet(?) "
               f"where {_filtro_cut(cuts)}ST_Intersects(geometry, ST_MakeEnvelope(?, ?, ?, ?)) limit ?")
        filas += con.execute(sql, [str(path), w, s, e, n, limite + 1 - len(filas)]).fetchall()
        if len(filas) > limite:
            break
    recortado = len(filas) > limite
    h_ref = a.altura_piso_ref_m
    feats = []
    for id_, h, nf, area, fuente, g in filas[:limite]:
        if h is not None and h == h:
            alt, fa = float(h), "overture_height"
        elif nf is not None and nf == nf:
            alt, fa = float(nf) * h_ref, "overture_num_floors"
        else:
            alt, fa = h_ref, "estimado"
        feats.append({"type": "Feature", "geometry": json.loads(g), "properties": {
            "id": id_, "altura_est": round(alt, 1), "altura_fuente": fa, "area_m2": round(float(area), 1) if area else None,
            "fuente": fuente}})
    aviso = (f"Se muestran {limite} edificios; hay más en el área: acércate para ver todos." if recortado else None)
    return {**base, "features": feats, "recortado": recortado,
            "nota_altura": (f"altura_est: height de Overture si existe; si no, num_floors × {h_ref} m; si no, 1 piso ({h_ref} m, "
                            "estimado). Solo para visualizar."), "aviso": aviso}


def edificios_en(a, geom_4326: BaseGeometry) -> gpd.GeoDataFrame:
    """Edificios que intersectan el polígono (EPSG:4326), con id, height, num_floors, area_m2 y fuente."""
    con, partes = _conexion(), []
    for path, cuts in _archivos(a, geom_4326.bounds):
        sql = (f"select id, height, num_floors, area_m2, fuente, ST_AsText(geometry) from read_parquet(?) "
               f"where {_filtro_cut(cuts)}ST_Intersects(geometry, ST_GeomFromText(?))")
        partes += con.execute(sql, [str(path), geom_4326.wkt]).fetchall()
    cols = ["id", "height", "num_floors", "area_m2", "fuente"]
    if not partes:
        return gpd.GeoDataFrame({c: [] for c in cols}, geometry=[], crs=4326)
    df = {c: [r[i] for r in partes] for i, c in enumerate(cols)}
    return gpd.GeoDataFrame(df, geometry=shapely.from_wkt([r[5] for r in partes]), crs=4326)
