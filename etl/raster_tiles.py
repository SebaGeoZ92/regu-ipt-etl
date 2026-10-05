"""Teselador ráster para los mapas temáticos (docs/MAPAS_TEMATICOS.md): GeoTIFF → rampa de colores → PMTiles.

Sin GDAL de línea de comandos: `rasterio` reproyecta por ventanas a Web Mercator, `numpy` aplica la rampa, `Pillow`
escribe los PNG de 256 px, un MBTiles intermedio los reúne y `pmtiles` lo convierte a PMTiles (tipo PNG), que MapLibre
lee como fuente `raster`.
- `interpolacion="lineal"`: continuo (temperatura, altitud); remuestreo bilineal.
- `interpolacion="escalon"`: categórico (cobertura del suelo); cada valor toma el color de la rampa con valor ≤ él, y el
  remuestreo es del vecino más cercano para no inventar clases intermedias.
- `factor`: multiplica el dato antes de colorear (p. ej. SoilGrids g/kg → %).
- `mascara`: geometría en EPSG:4326 (p. ej. Chile). Fuera de ella, y donde el dato es nodata, el píxel queda transparente;
  las teselas que no tocan la máscara no se escriben.
"""
from __future__ import annotations

import gc
import importlib.util
import io
import logging
import os
import sqlite3
from pathlib import Path


def _entorno_proj() -> None:
    """Apunta PROJ y GDAL a los datos que trae el propio `rasterio` (solo en este proceso).

    Con PostgreSQL/PostGIS instalado, las variables de máquina PROJ_LIB y GDAL_DATA apuntan a una base de PROJ más
    antigua y `rasterio` falla al reconocer cualquier EPSG («DATABASE.LAYOUT.VERSION.MINOR = 2 whereas >= 6 is expected»).
    Hay que hacerlo antes de importar `rasterio`."""
    spec = importlib.util.find_spec("rasterio")
    if spec is None or not spec.submodule_search_locations:
        return
    base = Path(list(spec.submodule_search_locations)[0])
    for var, sub in (("PROJ_DATA", "proj_data"), ("PROJ_LIB", "proj_data"), ("GDAL_DATA", "gdal_data")):
        if (base / sub).exists():
            os.environ[var] = str(base / sub)


_entorno_proj()

import mercantile  # noqa: E402
import numpy as np  # noqa: E402
import rasterio  # noqa: E402
import rasterio.warp  # noqa: E402
import shapely  # noqa: E402
from PIL import Image  # noqa: E402
from pmtiles.convert import mbtiles_to_pmtiles  # noqa: E402
from pmtiles.reader import MmapSource, Reader  # noqa: E402
from pyproj import Transformer  # noqa: E402
from rasterio.enums import Resampling  # noqa: E402
from rasterio.features import rasterize  # noqa: E402
from rasterio.transform import from_bounds  # noqa: E402
from rasterio.warp import reproject  # noqa: E402
from shapely.ops import transform as shp_transform  # noqa: E402

log = logging.getLogger(__name__)
LAT_MAX = 85.0511


def _rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def colorear(v: np.ndarray, valido: np.ndarray, rampa: list, interpolacion: str = "lineal") -> np.ndarray:
    """Matriz (alto, ancho) de valores → imagen RGBA uint8. `valido` False = transparente."""
    vals = np.array([p[0] for p in rampa], dtype="float64")
    cols = np.array([_rgb(p[1]) for p in rampa], dtype="float64")
    out = np.zeros(v.shape + (4,), dtype="uint8")
    x = np.where(valido, v, vals[0]).astype("float64")
    if interpolacion == "escalon":
        idx = np.searchsorted(vals, x, side="right") - 1
        ok = valido & (idx >= 0)
        rgb = cols[np.clip(idx, 0, len(vals) - 1)]
    else:
        ok = valido
        rgb = np.stack([np.interp(x, vals, cols[:, c]) for c in range(3)], axis=-1)
    out[..., :3] = np.rint(rgb).astype("uint8")
    out[..., 3] = np.where(ok, 255, 0)
    return out


def _png(rgba: np.ndarray) -> bytes:
    b = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(b, format="PNG", optimize=True)
    return b.getvalue()


def _mbtiles(path: Path, meta: dict) -> sqlite3.Connection:
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    con.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    con.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    con.executemany("INSERT INTO metadata VALUES (?, ?)", [(k, str(v)) for k, v in meta.items()])
    return con


def teselar_raster(tif: Path, destino: Path, rampa: list, *, interpolacion: str = "lineal", factor: float = 1.0,
                   nodata: float | None = None, zoom: tuple[int, int] = (3, 9), mascara=None, nombre: str = "",
                   atribucion: str = "", tam: int = 256, sombreado: Path | None = None, intensidad: float = 0.5) -> dict:
    """GeoTIFF → PMTiles ráster coloreado. Devuelve {destino, tiles, mb, zoom}.

    `sombreado`: segundo GeoTIFF (float, 0 = sombra, 1 = luz; el mismo territorio) que oscurece el color por relieve:
    color × ((1 − intensidad) + intensidad × sombreado). Donde no hay sombreado, el color queda sin modificar."""
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    remuestreo = Resampling.nearest if interpolacion == "escalon" else Resampling.bilinear
    a3857 = Transformer.from_crs(4326, 3857, always_xy=True)
    mask3857 = shp_transform(a3857.transform, mascara) if mascara is not None else None
    if mask3857 is not None:
        shapely.prepare(mask3857)
    mb = destino.with_suffix(".mbtiles.tmp")
    n = 0
    sombra = rasterio.open(sombreado) if sombreado else None
    with rasterio.open(tif) as src:
        nd = nodata if nodata is not None else src.nodata
        b = src.bounds if src.crs and src.crs.to_epsg() == 4326 else rasterio.warp.transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        w, s, e, nn = max(b[0], -180.0), max(b[1], -LAT_MAX), min(b[2], 180.0), min(b[3], LAT_MAX)
        if mascara is not None:
            mw, ms, me, mn = mascara.bounds
            w, s, e, nn = max(w, mw), max(s, ms), min(e, me), min(nn, mn)
        con = _mbtiles(mb, {"name": nombre or destino.stem, "format": "png", "type": "overlay", "version": "1.0",
                            "minzoom": zoom[0], "maxzoom": zoom[1], "bounds": f"{w},{s},{e},{nn}",
                            "attribution": atribucion, "description": "Regu Suelo local: mapa temático (regu-ipt-etl)"})
        try:
            for z in range(zoom[0], zoom[1] + 1):
                for t in mercantile.tiles(w, s, e, nn, z):
                    x0, y0, x1, y1 = mercantile.xy_bounds(t)
                    caja = shapely.box(x0, y0, x1, y1)
                    if mask3857 is not None and not shapely.intersects(mask3857, caja):
                        continue
                    destino_px = np.full((tam, tam), np.nan, dtype="float32")
                    reproject(source=rasterio.band(src, 1), destination=destino_px, dst_transform=from_bounds(x0, y0, x1, y1, tam, tam),
                              dst_crs="EPSG:3857", src_nodata=nd, dst_nodata=np.nan, resampling=remuestreo, init_dest_nodata=True)
                    valido = np.isfinite(destino_px)
                    if mask3857 is not None:
                        recorte = shapely.clip_by_rect(mask3857, x0, y0, x1, y1)
                        if recorte.is_empty:
                            continue
                        dentro = rasterize([(recorte, 1)], out_shape=(tam, tam), transform=from_bounds(x0, y0, x1, y1, tam, tam), fill=0)
                        valido &= dentro.astype(bool)
                    if not valido.any():
                        continue
                    img = colorear(np.nan_to_num(destino_px).astype("float64") * factor, valido, rampa, interpolacion)
                    if sombra is not None:
                        luz = np.full((tam, tam), np.nan, dtype="float32")
                        reproject(source=rasterio.band(sombra, 1), destination=luz, dst_transform=from_bounds(x0, y0, x1, y1, tam, tam),
                                  dst_crs="EPSG:3857", src_nodata=sombra.nodata, dst_nodata=np.nan, resampling=Resampling.bilinear,
                                  init_dest_nodata=True)
                        k = np.where(np.isfinite(luz), (1 - intensidad) + intensidad * np.clip(luz, 0, 1), 1.0)
                        img[..., :3] = np.clip(np.rint(img[..., :3] * k[..., None]), 0, 255).astype("uint8")
                    con.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)", (z, t.x, (1 << z) - 1 - t.y, _png(img)))
                    n += 1
                con.commit()
                log.info("zoom %d: %d teselas hasta ahora", z, n)
        finally:
            con.close()
            if sombra is not None:
                sombra.close()
    if n == 0:
        mb.unlink(missing_ok=True)
        raise ValueError("Ninguna tesela con datos: ¿la máscara y el ráster se solapan?")
    destino.unlink(missing_ok=True)
    mbtiles_to_pmtiles(str(mb), str(destino), zoom[1])
    gc.collect()                      # la conversión deja abierta su conexión SQLite: en Windows bloquea el borrado
    try:
        mb.unlink(missing_ok=True)
    except PermissionError:
        log.warning("No se pudo borrar %s (en uso); se puede borrar a mano", mb)
    return {"destino": destino, "tiles": n, "mb": round(destino.stat().st_size / 1e6, 2), "zoom": zoom}


def leer_tesela(pmtiles: Path, z: int, x: int, y: int) -> np.ndarray | None:
    """RGBA de una tesela de un PMTiles ráster (para pruebas y para muestrear valores); None si no existe."""
    with open(pmtiles, "rb") as f:
        datos = Reader(MmapSource(f)).get(z, x, y)
    return None if datos is None else np.array(Image.open(io.BytesIO(datos)).convert("RGBA"))


def encabezado(pmtiles: Path) -> dict:
    with open(pmtiles, "rb") as f:
        r = Reader(MmapSource(f))
        h = r.header()
        return {"min_zoom": h["min_zoom"], "max_zoom": h["max_zoom"], "tile_type": str(h["tile_type"]), "metadata": r.metadata()}
