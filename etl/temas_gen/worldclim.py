"""Temas de clima con WorldClim 2.1 (variables bioclimáticas, promedio 1970-2000, 2,5 minutos de arco).

El zip trae las 19 variables (658 MB): se baja una vez a `<raw>/temas/_compartido/` y cada tema extrae solo su GeoTIFF
(`parametros.archivo`, p. ej. `wc2.1_2.5m_bio_1.tif` = temperatura media anual en °C, `..._12.tif` = precipitación anual en mm).
"""
from __future__ import annotations

import logging
import zipfile
from pathlib import Path

from .. import raster_tiles
from ..temas import generador
from .comun import descargar, mascara_chile, ruta_cfg

log = logging.getLogger(__name__)


def extraer(zip_path: Path, nombre: str, destino_dir: Path) -> Path:
    """Extrae `nombre` del zip a `destino_dir` (si no está ya) y devuelve su ruta."""
    destino_dir.mkdir(parents=True, exist_ok=True)
    tif = destino_dir / nombre
    if not tif.exists():
        with zipfile.ZipFile(zip_path) as z:
            if nombre not in z.namelist():
                raise FileNotFoundError(f"{nombre} no está en {zip_path.name}: hay {', '.join(sorted(z.namelist())[:5])}…")
            with z.open(nombre) as f, open(tif.with_suffix(".parte"), "wb") as o:
                while trozo := f.read(1 << 20):
                    o.write(trozo)
        tif.with_suffix(".parte").replace(tif)
    return tif


@generador("worldclim_bio")
def worldclim_bio(c, dir_raw, destino, cfg):
    f, e = c["fuente"], c["estilo"]
    zip_path = descargar(f["url"], ruta_cfg(cfg, "raw") / "temas" / "_compartido" / Path(f["url"]).name)
    tif = extraer(zip_path, c["generacion"]["parametros"]["archivo"], Path(dir_raw))
    r = raster_tiles.teselar_raster(tif, destino, e["rampa"], interpolacion=e.get("interpolacion", "lineal"), factor=e.get("factor", 1.0),
                                    zoom=tuple(c["zoom"]), mascara=mascara_chile(cfg), nombre=c["nombre"], atribucion=f["atribucion"])
    log.info("%s: %d teselas, %.1f MB", c["id"], r["tiles"], r["mb"])
    return r["destino"]
