"""Temas de suelo con SoilGrids 2.0 (ISRIC, CC BY 4.0, 250 m): un mosaico global en proyección Goode homolosina.

El VRT es remoto (`/vsicurl/`): no se baja el planeta. `recortar_ventana()` lo lee por bandas de latitud, solo donde hay
territorio chileno, lo reproyecta a lat/lon (≈ 250 m) y lo guarda en un GeoTIFF local comprimido en
`<raw>/temas/<id>/`; de ahí se tesela. La descarga se hace una vez (si el GeoTIFF ya existe no se repite).
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

import shapely
from shapely.geometry import box

from .. import raster_tiles  # noqa: F401  (corrige PROJ_LIB/GDAL_DATA antes de usar rasterio)
from ..temas import generador
from .comun import mascara_chile

import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from rasterio.enums import Resampling  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402
from rasterio.warp import reproject  # noqa: E402
from rasterio.windows import Window  # noqa: E402

log = logging.getLogger(__name__)
NODATA = -32768
RES_GRADOS = 0.00225                       # ≈ 250 m en latitud


def recortar_ventana(origen: str | Path, destino: Path, mascara, *, resolucion: float = RES_GRADOS, banda_grados: float = 2.0,
                     margen: float = 0.05) -> Path:
    """Lee `origen` (cualquier ráster que GDAL abra, p. ej. un VRT remoto) por bandas de latitud, solo dentro de `mascara`
    (EPSG:4326), y escribe un GeoTIFF int16 en lat/lon. Un archivo a medias (`.parte`) se rehace entero."""
    destino = Path(destino)
    if destino.exists():
        return destino
    destino.parent.mkdir(parents=True, exist_ok=True)
    w, s, e, n = mascara.bounds
    w, e = math.floor(w / resolucion) * resolucion, math.ceil(e / resolucion) * resolucion
    s, n = math.floor(s / resolucion) * resolucion, math.ceil(n / resolucion) * resolucion
    ancho, alto = int(round((e - w) / resolucion)), int(round((n - s) / resolucion))
    parte = destino.with_suffix(".parte")
    parte.unlink(missing_ok=True)
    entorno = dict(GDAL_HTTP_MAX_RETRY="4", GDAL_HTTP_RETRY_DELAY="3", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", VSI_CACHE="TRUE",
                   CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".vrt,.tif")
    with rasterio.Env(**entorno), rasterio.open(origen) as src, rasterio.open(
            parte, "w", driver="GTiff", height=alto, width=ancho, count=1, dtype="int16", crs="EPSG:4326",
            transform=from_origin(w, n, resolucion, resolucion), nodata=NODATA, tiled=True, blockxsize=256, blockysize=256,
            compress="deflate", predictor=2) as dst:
        nd_src = src.nodata if src.nodata is not None else NODATA
        filas_banda = max(1, int(round(banda_grados / resolucion)))
        for fila0 in range(0, alto, filas_banda):
            filas = min(filas_banda, alto - fila0)
            norte, sur = n - fila0 * resolucion, n - (fila0 + filas) * resolucion
            zona = mascara.intersection(box(w, sur, e, norte))
            if zona.is_empty:
                continue
            zw, _, ze, _ = zona.bounds
            col0 = max(0, int(math.floor((zw - margen - w) / resolucion)))
            col1 = min(ancho, int(math.ceil((ze + margen - w) / resolucion)))
            arr = np.full((filas, col1 - col0), NODATA, dtype="int16")
            reproject(source=rasterio.band(src, 1), destination=arr, src_nodata=nd_src, dst_nodata=NODATA,
                      dst_transform=from_origin(w + col0 * resolucion, norte, resolucion, resolucion), dst_crs="EPSG:4326",
                      resampling=Resampling.bilinear, init_dest_nodata=True)
            dst.write(arr, 1, window=Window(col0, fila0, col1 - col0, filas))
            log.info("banda lat %.1f..%.1f: %d columnas", sur, norte, col1 - col0)
    parte.replace(destino)
    return destino


@generador("soilgrids")
def soilgrids(c, dir_raw, destino, cfg):
    f, e, p = c["fuente"], c["estilo"], c["generacion"]["parametros"]
    mascara = mascara_chile(cfg, continental=True)
    tif = recortar_ventana("/vsicurl/" + f["url"], Path(dir_raw) / f"{p['variable']}_{p['profundidad']}_chile.tif", mascara)
    r = raster_tiles.teselar_raster(tif, destino, e["rampa"], interpolacion=e.get("interpolacion", "lineal"), factor=e.get("factor", 1.0),
                                    nodata=NODATA, zoom=tuple(c["zoom"]), mascara=mascara, nombre=c["nombre"], atribucion=f["atribucion"])
    log.info("%s: %d teselas, %.1f MB", c["id"], r["tiles"], r["mb"])
    return r["destino"]
