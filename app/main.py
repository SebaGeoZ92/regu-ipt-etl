"""Regu Suelo local: FastAPI (docs/REGU_SUELO_LOCAL.md). Reusa etl/ sin duplicar lógica.

`crear_app(ajustes)` arma la aplicación; `python run.py app` la sirve en http://localhost:8000.
Solo local: puede mostrar normas BORRADOR, siempre marcadas "BORRADOR · uso interno".
"""
from __future__ import annotations

import time

from pathlib import Path

import geopandas as gpd
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

from etl import envolvente
from etl.ficha import ficha as calcular_ficha
from etl.normalize import norm_txt

from . import datos, edificios
from .ajustes import MARCA_BORRADOR, TEMAS_TESELAS, Ajustes

STATIC = Path(__file__).parent / "static"
CRS_AREA = "ESRI:102033"
MAX_PREDIO_M2 = 500_000.0       # 50 ha: más que eso no es un predio


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

    @app.get("/tiles/{tema}.pmtiles")
    def tiles(tema: str):
        """PMTiles locales. FileResponse atiende `Range` (206), que es lo que usa la librería pmtiles del navegador."""
        if tema not in TEMAS_TESELAS:
            raise HTTPException(404, f"Tema desconocido: {tema}")
        p = a.tiles / f"{tema}.pmtiles" if a.tiles else None
        if p is None or not p.exists():
            raise HTTPException(404, f"Falta la tesela {tema}: corre `python run.py teselas {tema}`")
        return FileResponse(p, media_type="application/octet-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    def inicio():
        return FileResponse(STATIC / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/ficha")
    def api_ficha_punto(lon: float = Query(..., ge=-180, le=180), lat: float = Query(..., ge=-90, le=90)):
        return _ficha(Point(lon, lat))

    @app.post("/api/ficha")
    def api_ficha_poligono(geojson: dict = Body(...)):
        return _ficha(geometria_de(geojson))

    @app.get("/api/edificios")
    def api_edificios(bbox: str = Query(..., description="oeste,sur,este,norte (EPSG:4326)"),
                      zoom: float | None = Query(None, ge=0, le=24),
                      limite: int = Query(edificios.LIMITE_DEF, ge=1, le=edificios.LIMITE_MAX)):
        try:
            w, s, e, n = (float(x) for x in bbox.split(","))
        except ValueError as ex:
            raise HTTPException(422, "bbox debe ser oeste,sur,este,norte") from ex
        if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
            raise HTTPException(422, "bbox fuera de rango")
        return edificios.edificios_bbox(a, (w, s, e, n), limite, zoom)

    @app.post("/api/volumen")
    def api_volumen(geojson: dict = Body(...)):
        """Volumen de un predio dibujado: existente (footprints) y posible fase 1 por cada norma de su zona."""
        g = geometria_de(geojson)
        if g.geom_type == "Point":
            raise HTTPException(422, "Dibuja un polígono (el predio)")
        area = gpd.GeoSeries([g], crs=4326).to_crs(CRS_AREA).iloc[0].area
        if area > MAX_PREDIO_M2:
            raise HTTPException(422, f"El polígono mide {area / 1e4:.1f} ha: se espera un predio (máximo {MAX_PREDIO_M2 / 1e4:.0f} ha)")
        t0 = time.perf_counter()
        f = calcular_ficha(g, a.gpkg, registrar=False)
        if not f["particion"]:
            raise HTTPException(422, f["motivo"] or "El polígono queda fuera de la cobertura")
        zona = f["particion"][0]
        simp = []
        if len(f["particion"]) > 1:
            simp.append("El predio cae en más de una zona (" + ", ".join(f"{p['zona'] or p['clase']} {p['pct']} %" for p in f["particion"])
                        + "): el volumen posible usa la de mayor área.")
        exist = envolvente.existente(g, edificios.edificios_en(a, g), a.altura_piso_ref_m)
        normas = datos.normas(a).get((norm_txt(zona["ipt"] or ""), norm_txt(zona["zona"] or "")), [])
        escenarios = []
        for n_ in normas:
            esc = envolvente.posible(g, n_, a.altura_piso_ref_m)
            escenarios.append({**esc, **envolvente.comparar(exist, esc)})
        if not normas:
            simp.append(f"La zona {zona['zona'] or zona['clase']} no tiene normas cargadas: solo se informa lo existente. "
                        "Las normas no se inventan.")
        if exist["cota_inferior"]:
            simp.append("Pisos sin dato en Overture: se asume 1 piso por edificio (cota inferior, confianza baja).")
        if exist["n_edificios"] == 0:
            simp.append("No hay edificios de Overture dentro del predio: se informa 0 m² existentes (puede ser un sitio "
                        "eriazo o un dato faltante).")
        marca = MARCA_BORRADOR if any(e.get("normas_estado") != "VALIDADO" for e in escenarios) else None
        return datos._sin_nan({
            "zona": {k: zona[k] for k in ("clase", "ipt", "zona", "pct")}, "comuna": f["comuna"], "cut": f["cut"],
            "existente": exist, "escenarios": escenarios, "simplificaciones": simp, "marca": marca, "aviso": f["aviso"],
            "atribucion": edificios.ATRIBUCION, "tiempo_ms": round((time.perf_counter() - t0) * 1000)})

    return app
