"""Utilidades compartidas por los generadores de temas: rutas de la configuración, descarga con reanudación y máscara de Chile."""
from __future__ import annotations

import logging
import time
from functools import lru_cache
from pathlib import Path

import geopandas as gpd
import requests
import shapely
from shapely.geometry import box

RAIZ = Path(__file__).resolve().parents[2]
log = logging.getLogger(__name__)
UA = {"User-Agent": "ReguSueloLocal/1.0 (descarga de datos abiertos para mapas tematicos)"}
BBOX_CHILE_CONTINENTAL = (-76.0, -56.5, -66.0, -17.0)     # lon/lat; sin islas ni el Territorio Chileno Antártico


def ruta_cfg(cfg: dict, clave: str) -> Path:
    """Ruta de `paths.<clave>` de config.yaml: absoluta o relativa a la raíz del repo (igual que run.ruta)."""
    p = Path(cfg["paths"][clave])
    return p if p.is_absolute() else RAIZ / p


def descargar(url: str, destino: Path, *, esperado_mb: float | None = None, reintentos: int = 4) -> Path:
    """Descarga `url` a `destino` por streaming y con reanudación (Range) si quedó una descarga a medias (`.parte`).
    Verifica el tamaño contra Content-Length. Devuelve `destino` (sin volver a bajar si ya está completo)."""
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    total = None
    try:
        h = requests.head(url, headers=UA, timeout=30, allow_redirects=True)
        total = int(h.headers["content-length"]) if h.ok and "content-length" in h.headers else None
    except requests.RequestException:
        pass
    if destino.exists() and (total is None or destino.stat().st_size == total):
        return destino
    parte = destino.with_suffix(destino.suffix + ".parte")
    for intento in range(1, reintentos + 1):
        ya = parte.stat().st_size if parte.exists() else 0
        if total is not None and ya > total:
            parte.unlink()
            ya = 0
        try:
            with requests.get(url, headers={**UA, **({"Range": f"bytes={ya}-"} if ya else {})}, stream=True, timeout=60) as r:
                if r.status_code == 416 and total is not None and ya == total:
                    break
                r.raise_for_status()
                modo = "ab" if ya and r.status_code == 206 else "wb"
                if modo == "wb":
                    ya = 0
                hecho, t0, ult = ya, time.time(), 0.0
                with open(parte, modo) as f:
                    for trozo in r.iter_content(1 << 20):
                        f.write(trozo)
                        hecho += len(trozo)
                        if time.time() - ult > 10:
                            ult = time.time()
                            pct = f" ({100 * hecho / total:.0f} %)" if total else ""
                            log.info("descargando %s: %.0f MB%s", destino.name, hecho / 1e6, pct)
            break
        except requests.RequestException as ex:
            log.warning("descarga interrumpida (intento %d/%d): %s", intento, reintentos, ex)
            time.sleep(3 * intento)
    else:
        raise RuntimeError(f"No se pudo descargar {url}")
    if total is not None and parte.stat().st_size != total:
        raise RuntimeError(f"Descarga incompleta de {url}: {parte.stat().st_size} de {total} bytes")
    parte.replace(destino)
    return destino


@lru_cache(maxsize=2)
def _comunas_4326(ruta_shp: str) -> gpd.GeoDataFrame:
    return gpd.read_file(ruta_shp).to_crs(4326)


def mascara_chile(cfg: dict, *, buffer_grados: float = 0.05, simplificar: float = 0.004, continental: bool = False,
                  region: str | None = None):
    """Geometría (EPSG:4326) del territorio chileno según la división comunal de la BCN, con un margen de `buffer_grados`
    (≈ 5 km) para no cortar la costa generalizada. `continental` deja solo la parte continental (sin islas lejanas ni
    Antártica); `region` restringe a una región (campo `Region`). Sirve de máscara para teselar rásters globales."""
    g = _comunas_4326(str(ruta_cfg(cfg, "comunas")))
    c = cfg["comunas"]
    if region:
        g = g[g[c["field_region"]].astype(str).str.contains(region, case=False, regex=False)]
    geom = shapely.union_all(g.geometry.values)
    if continental:
        geom = geom.intersection(box(*BBOX_CHILE_CONTINENTAL))
    return geom.simplify(simplificar).buffer(buffer_grados)
