"""Tema de relieve con Copernicus DEM GLO-30 (30 m): altitud con la rampa hipsométrica y sombreado de relieve.

Piloto por región (`parametros.region`, p. ej. La Araucanía): el país entero son cientos de teselas de 1° (varios GB).
Las teselas COG de 1° se bajan una vez a `<raw>/temas/_compartido/glo30/`, se arma un mosaico int16 de la región, se calcula el
sombreado (método de Horn, por bloques para no llenar la memoria) y se tesela la altitud oscurecida por el sombreado.
Es un modelo de SUPERFICIE: incluye edificios y vegetación.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

from .. import raster_tiles  # noqa: F401  (corrige PROJ_LIB/GDAL_DATA antes de usar rasterio)
from ..temas import generador
from .comun import descargar, mascara_chile, ruta_cfg

import numpy as np  # noqa: E402
import rasterio  # noqa: E402
import requests  # noqa: E402
from rasterio.enums import Resampling  # noqa: E402
from rasterio.merge import merge  # noqa: E402
from rasterio.windows import Window  # noqa: E402

log = logging.getLogger(__name__)
BASE = "https://copernicus-dem-30m.s3.amazonaws.com"
NODATA = -32768
RES = 1 / 3600                                 # 1 segundo de arco ≈ 30 m


def nombres_teselas(w: float, s: float, e: float, n: float) -> list[str]:
    """Nombres de las teselas COG de 1° de GLO-30 que cubren el bbox (la esquina suroeste da el nombre: S39 W073 = lat -39..-38)."""
    nombres = []
    for lat in range(math.floor(s), math.ceil(n)):
        for lon in range(math.floor(w), math.ceil(e)):
            ns, ew = ("S" if lat < 0 else "N"), ("W" if lon < 0 else "E")
            nombres.append(f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM")
    return nombres


def sombreado(z: np.ndarray, dx_m: np.ndarray, dy_m: float, *, azimut: float = 315.0, altura: float = 45.0,
              exageracion: float = 1.0) -> np.ndarray:
    """Sombreado de relieve (Horn) de una matriz de alturas en metros. `dx_m` = ancho de píxel en metros por fila,
    `dy_m` = alto de píxel. Devuelve float32 en [0, 1] (NaN donde no hay dato). Un plano horizontal da sin(altura)."""
    zz = np.where(np.isfinite(z), z, np.nan).astype("float64")
    p = np.pad(zz, 1, mode="edge")
    a, b, c = p[:-2, :-2], p[:-2, 1:-1], p[:-2, 2:]
    d, f = p[1:-1, :-2], p[1:-1, 2:]
    g, h, i = p[2:, :-2], p[2:, 1:-1], p[2:, 2:]
    dzdx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * dx_m[:, None])
    dzdy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8 * dy_m)
    pendiente = np.arctan(exageracion * np.hypot(dzdx, dzdy))
    aspecto = np.arctan2(dzdy, -dzdx)
    cenit = np.radians(90.0 - altura)
    az = np.radians((360.0 - azimut + 90.0) % 360.0)           # de rumbo de brújula a ángulo matemático
    luz = np.cos(cenit) * np.cos(pendiente) + np.sin(cenit) * np.sin(pendiente) * np.cos(az - aspecto)
    luz = np.clip(luz, 0, 1)
    luz[~np.isfinite(zz)] = np.nan
    return luz.astype("float32")


def generar_sombreado(dem: Path, destino: Path, *, bloque: int = 1024, **kw) -> Path:
    """Sombreado de un MDE en lat/lon (EPSG:4326), por bloques de filas con una fila de traslape. float32, nodata -1."""
    destino = Path(destino)
    if destino.exists():
        return destino
    destino.parent.mkdir(parents=True, exist_ok=True)
    parte = destino.with_suffix(".parte")
    with rasterio.open(dem) as src, rasterio.open(parte, "w", driver="GTiff", height=src.height, width=src.width, count=1, dtype="float32",
                                                  crs=src.crs, transform=src.transform, nodata=-1.0, tiled=True, blockxsize=256, blockysize=256,
                                                  compress="deflate", predictor=3) as dst:
        res_lon, res_lat = abs(src.transform.a), abs(src.transform.e)
        dy = res_lat * 110574.0
        for fila0 in range(0, src.height, bloque):
            f0, f1 = max(0, fila0 - 1), min(src.height, fila0 + bloque + 1)
            z = src.read(1, window=Window(0, f0, src.width, f1 - f0)).astype("float64")
            z[z == src.nodata] = np.nan
            lat = src.transform.f - (np.arange(f0, f1) + 0.5) * res_lat
            dx = res_lon * 111320.0 * np.cos(np.radians(lat))
            luz = sombreado(z, dx, dy, **kw)
            luz = luz[fila0 - f0: fila0 - f0 + min(bloque, src.height - fila0)]
            dst.write(np.nan_to_num(luz, nan=-1.0), 1, window=Window(0, fila0, src.width, luz.shape[0]))
    parte.replace(destino)
    return destino


def mosaico(nombres: list[str], dir_cache: Path, destino: Path, bounds: tuple) -> Path:
    """Baja las teselas (las que no existen —océano— se omiten) y arma un GeoTIFF int16 con `bounds` (lon/lat)."""
    destino = Path(destino)
    if destino.exists():
        return destino
    destino.parent.mkdir(parents=True, exist_ok=True)
    rutas = []
    for nom in nombres:
        try:
            rutas.append(descargar(f"{BASE}/{nom}/{nom}.tif", dir_cache / f"{nom}.tif"))
        except (FileNotFoundError, RuntimeError, requests.RequestException) as ex:
            log.warning("sin tesela %s (¿océano?): %s", nom, str(ex)[:80])
    if not rutas:
        raise RuntimeError("Ninguna tesela de GLO-30 disponible para la región")
    fuentes = [rasterio.open(p) for p in rutas]
    try:
        arr, tr = merge(fuentes, bounds=bounds, res=(RES, RES), nodata=NODATA, dtype="int16", resampling=Resampling.bilinear)
    finally:
        for f in fuentes:
            f.close()
    parte = destino.with_suffix(".parte")
    with rasterio.open(parte, "w", driver="GTiff", height=arr.shape[1], width=arr.shape[2], count=1, dtype="int16", crs="EPSG:4326",
                       transform=tr, nodata=NODATA, tiled=True, blockxsize=256, blockysize=256, compress="deflate", predictor=2) as dst:
        dst.write(arr[0], 1)
    parte.replace(destino)
    return destino


@generador("glo30")
def glo30(c, dir_raw, destino, cfg):
    f, e, p = c["fuente"], c["estilo"], c["generacion"]["parametros"]
    mascara = mascara_chile(cfg, continental=True, region=p.get("region"))
    w, s, ee, n = mascara.bounds
    nombres = nombres_teselas(w, s, ee, n)
    log.info("GLO-30: %d teselas de 1° para %s", len(nombres), p.get("region") or "Chile")
    dem = mosaico(nombres, ruta_cfg(cfg, "raw") / "temas" / "_compartido" / "glo30", Path(dir_raw) / "dem.tif",
                  (math.floor(w * 100) / 100, math.floor(s * 100) / 100, math.ceil(ee * 100) / 100, math.ceil(n * 100) / 100))
    sombra = generar_sombreado(dem, Path(dir_raw) / "sombreado.tif", azimut=p.get("azimut", 315.0), altura=p.get("altura_sol", 45.0),
                               exageracion=p.get("exageracion", 1.0))
    r = raster_tiles.teselar_raster(dem, destino, e["rampa"], interpolacion=e.get("interpolacion", "lineal"), factor=e.get("factor", 1.0),
                                    nodata=NODATA, zoom=tuple(c["zoom"]), mascara=mascara, nombre=c["nombre"], atribucion=f["atribucion"],
                                    sombreado=sombra, intensidad=p.get("intensidad", 0.55))
    log.info("%s: %d teselas, %.1f MB", c["id"], r["tiles"], r["mb"])
    return r["destino"]
