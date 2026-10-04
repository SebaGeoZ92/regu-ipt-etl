"""Regu Suelo local: FastAPI (docs/REGU_SUELO_LOCAL.md). Reusa etl/ sin duplicar lógica.

`crear_app(ajustes)` arma la aplicación; `python run.py app` la sirve en http://localhost:8000.
Solo local: puede mostrar normas BORRADOR, siempre marcadas "BORRADOR · uso interno".
"""
from __future__ import annotations

import time

from fastapi import Body, FastAPI, HTTPException, Query
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

from etl.ficha import ficha as calcular_ficha

from . import datos
from .ajustes import Ajustes


def geometria_de(geojson: dict) -> BaseGeometry:
    """Geometría shapely desde un GeoJSON (Geometry, Feature o FeatureCollection: se toma el primer elemento)."""
    t = geojson.get("type")
    if t == "FeatureCollection":
        feats = geojson.get("features") or []
        if not feats:
            raise HTTPException(422, "FeatureCollection vacía")
        geojson = feats[0]
        t = geojson.get("type")
    if t == "Feature":
        geojson = geojson.get("geometry") or {}
    try:
        g = shape(geojson)
    except Exception as ex:
        raise HTTPException(422, f"GeoJSON inválido: {ex}") from ex
    if g.is_empty:
        raise HTTPException(422, "Geometría vacía")
    if g.geom_type not in ("Point", "Polygon", "MultiPolygon"):
        raise HTTPException(422, f"Se espera un punto o un polígono, no {g.geom_type}")
    if not g.is_valid:
        g = g.buffer(0)
    return g


def crear_app(a: Ajustes) -> FastAPI:
    app = FastAPI(title="Regu Suelo · local", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.ajustes = a

    def _ficha(g: BaseGeometry) -> dict:
        t0 = time.perf_counter()
        f = calcular_ficha(g, a.gpkg, registrar=a.registrar, dir_demanda=a.dir_demanda)
        f = datos.enriquecer(f, a)
        f["tiempo_ms"] = round((time.perf_counter() - t0) * 1000)
        return f

    @app.get("/api/comunas")
    def api_comunas():
        return datos.comunas(a)

    @app.get("/api/ficha")
    def api_ficha_punto(lon: float = Query(..., ge=-180, le=180), lat: float = Query(..., ge=-90, le=90)):
        return _ficha(Point(lon, lat))

    @app.post("/api/ficha")
    def api_ficha_poligono(geojson: dict = Body(...)):
        return _ficha(geometria_de(geojson))

    return app
