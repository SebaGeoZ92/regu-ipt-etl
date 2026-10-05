"""Tema de cobertura y uso del suelo con ESA WorldCover 2021 v200 (10 m, CC BY 4.0, 11 clases).

Las teselas son COG de 3° × 3° (≈ 36.000 × 36.000 px) con miniaturas internas. No se baja nada completo: cada tesela chilena se
lee ya reducida por un factor entero (por defecto 10 → ≈ 90 m) con el valor más frecuente (`mode`), de modo que nunca aparecen
clases intermedias que no existen, y se arma un mosaico local en lat/lon (`<raw>/temas/<id>/cobertura_chile.tif`).
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

from .. import raster_tiles  # noqa: F401  (corrige PROJ_LIB/GDAL_DATA antes de usar rasterio)
from ..temas import generador
from .comun import mascara_chile

import numpy as np  # noqa: E402
import rasterio  # noqa: E402
import requests  # noqa: E402
import shapely  # noqa: E402
from rasterio.enums import Resampling  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402
from rasterio.windows import Window  # noqa: E402
from shapely.geometry import box  # noqa: E402

log = logging.getLogger(__name__)
BASE = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map"
PREFIJO = "/vsicurl/"                         # las pruebas lo vacían para usar archivos locales
PASO = 3                                       # las teselas son de 3° y su nombre usa la esquina suroeste (múltiplo de 3)
FACTOR = 10


def tesela_url(lat0: int, lon0: int) -> str:
    ns, ew = ("S" if lat0 < 0 else "N"), ("W" if lon0 < 0 else "E")
    return f"{BASE}/ESA_WorldCover_10m_2021_v200_{ns}{abs(lat0):02d}{ew}{abs(lon0):03d}_Map.tif"


def teselas_necesarias(mascara) -> list[tuple[int, int]]:
    """Esquinas suroeste (lat, lon) de las teselas de 3° que tocan la máscara."""
    w, s, e, n = mascara.bounds
    res = []
    for lat0 in range(math.floor(s / PASO) * PASO, math.ceil(n / PASO) * PASO, PASO):
        for lon0 in range(math.floor(w / PASO) * PASO, math.ceil(e / PASO) * PASO, PASO):
            if shapely.intersects(mascara, box(lon0, lat0, lon0 + PASO, lat0 + PASO)):
                res.append((lat0, lon0))
    return res


def mosaico_decimado(fuentes: dict[tuple[int, int], str | Path], destino: Path, *, factor: int = FACTOR) -> Path:
    """Arma un GeoTIFF uint8 (0 = sin dato) con las teselas `fuentes` ({(lat0, lon0): ruta o URL}), cada una leída reducida
    `factor` veces con `mode`. Una fuente que no existe (404 u océano) se omite. Un archivo a medias se rehace entero."""
    destino = Path(destino)
    if destino.exists():
        return destino
    destino.parent.mkdir(parents=True, exist_ok=True)
    lats = [k[0] for k in fuentes]
    lons = [k[1] for k in fuentes]
    w, s, e, n = min(lons), min(lats), max(lons) + PASO, max(lats) + PASO
    # resolución de salida: la de origen × factor (se toma de la primera tesela que abra)
    parte = destino.with_suffix(".parte")
    dst = None
    leidas = 0
    entorno = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif", GDAL_HTTP_MAX_RETRY="4", GDAL_HTTP_RETRY_DELAY="3")
    try:
        with rasterio.Env(**entorno):
            for (lat0, lon0), ruta in sorted(fuentes.items()):
                try:
                    src = rasterio.open(ruta)
                except rasterio.errors.RasterioIOError as ex:
                    log.warning("sin tesela %s,%s (%s): se omite", lat0, lon0, str(ex)[:60])
                    continue
                with src:
                    if dst is None:
                        res = src.res[0] * factor
                        ancho, alto = int(round((e - w) / res)), int(round((n - s) / res))
                        dst = rasterio.open(parte, "w", driver="GTiff", height=alto, width=ancho, count=1, dtype="uint8", crs="EPSG:4326",
                                            transform=from_origin(w, n, res, res), nodata=0, tiled=True, blockxsize=256, blockysize=256,
                                            compress="deflate", predictor=2)
                    out_w, out_h = src.width // factor, src.height // factor
                    datos = src.read(1, out_shape=(out_h, out_w), resampling=Resampling.mode)
                    col0 = int(round((src.bounds.left - w) / dst.res[0]))
                    fila0 = int(round((n - src.bounds.top) / dst.res[1]))
                    dst.write(datos, 1, window=Window(col0, fila0, out_w, out_h))
                    leidas += 1
                    log.info("tesela %s,%s leída (%d de %d)", lat0, lon0, leidas, len(fuentes))
    finally:
        if dst is not None:
            dst.close()
    if dst is None:
        parte.unlink(missing_ok=True)
        raise RuntimeError("Ninguna tesela de WorldCover disponible")
    parte.replace(destino)
    return destino


@generador("worldcover")
def worldcover(c, dir_raw, destino, cfg):
    f, e = c["fuente"], c["estilo"]
    mascara = mascara_chile(cfg, continental=True)
    nec = teselas_necesarias(mascara)
    log.info("WorldCover: %d teselas de 3° tocan Chile continental", len(nec))
    tif = mosaico_decimado({k: PREFIJO + tesela_url(*k) for k in nec}, Path(dir_raw) / "cobertura_chile.tif",
                           factor=(c["generacion"].get("parametros") or {}).get("factor", FACTOR))
    r = raster_tiles.teselar_raster(tif, destino, e["rampa"], interpolacion="escalon", nodata=0, zoom=tuple(c["zoom"]), mascara=mascara,
                                    nombre=c["nombre"], atribucion=f["atribucion"])
    log.info("%s: %d teselas, %.1f MB", c["id"], r["tiles"], r["mb"])
    return r["destino"]
