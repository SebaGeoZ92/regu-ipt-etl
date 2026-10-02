"""Footprints nacionales de Overture Maps (type=building), por región y comuna (docs/FOOTPRINTS_NACIONAL.md).

- Licencia ODbL: capa APARTE en data/base/footprints/, nunca dentro de capa_ipt ni del Atlas.
  Atribución: "© OpenStreetMap contributors, Overture Maps Foundation (ODbL)".
- Por región: se descarga el bbox de sus comunas con margen y se recorta contra la DPA. Cada edificio va a la
  comuna donde tiene MÁS área (no se parte). Las candidatas incluyen las comunas de regiones vecinas que tocan el
  bbox, así un edificio de borde regional queda en una sola región (sin duplicados).
- Reanudable: un manifiesto por región (completa true/false); al volver a correr se saltan las regiones completas
  del mismo release. Las escrituras van a un archivo temporal y se renombran al final.
- El archivo crudo de Overture se borra al terminar bien (disco justo), salvo conservar_crudo=True.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from . import progreso
from .normalize import norm_txt

log = logging.getLogger(__name__)

COLUMNAS = ["id", "cut", "comuna", "region", "height", "num_floors", "class", "subtype", "fuente", "area_m2",
            "geometry"]
ATRIBUCION = "© OpenStreetMap contributors, Overture Maps Foundation (ODbL)"
CRS_AREA = "ESRI:102033"
MARGEN_GRADOS = 0.02   # ~2 km: los edificios que cruzan el borde del bbox entran completos


def slug(region: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", norm_txt(region).lower()).strip("_")


def regiones_que_calzan(regiones: list[str], patron: str) -> list[str]:
    """Regiones cuyo nombre normalizado calza con el patrón (regex, sin tildes ni mayúsculas): 'ARICA|TARAPACA'."""
    p = re.compile(norm_txt(patron), re.I)
    return [r for r in regiones if p.search(norm_txt(r))]


def bbox_con_margen(geoms: gpd.GeoSeries, margen: float = MARGEN_GRADOS) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = geoms.to_crs(4326).total_bounds
    return round(x0 - margen, 6), round(y0 - margen, 6), round(x1 + margen, 6), round(y1 + margen, 6)


def descargar_overture(bbox: tuple, destino: Path, release: str) -> None:
    """Descarga real con la CLI de overturemaps (requiere red). Reemplazable en los tests."""
    cli = Path(sys.executable).with_name("overturemaps.exe")
    cmd = [str(cli) if cli.exists() else "overturemaps", "download", "--bbox=" + ",".join(map(str, bbox)),
           "-f", "geoparquet", "--type=building", "-r", release, "-o", str(destino)]
    subprocess.run(cmd, check=True)


def ultimo_release() -> str:
    from overturemaps import core
    return core.get_latest_release()


def _fuente(sources) -> str | None:
    """Dataset de origen dentro de Overture (p.ej. 'OpenStreetMap', 'Microsoft ML Buildings'): sources[0].dataset."""
    if sources is None or (isinstance(sources, float) and pd.isna(sources)):
        return None
    try:
        lista = list(sources)
    except TypeError:
        return None
    if not lista:
        return None
    s = lista[0]
    return s.get("dataset") if isinstance(s, dict) else None


def normalizar(crudo: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Solo las columnas que el producto necesita (sin nombres ni atributos de fachada/techo); solo polígonos."""
    g = crudo.to_crs(4326) if crudo.crs else crudo.set_crs(4326)
    out = gpd.GeoDataFrame({
        "id": g["id"].astype(str),
        "height": pd.to_numeric(g["height"], errors="coerce") if "height" in g else np.nan,
        "num_floors": pd.to_numeric(g["num_floors"], errors="coerce") if "num_floors" in g else np.nan,
        "class": g["class"] if "class" in g else None,
        "subtype": g["subtype"] if "subtype" in g else None,
        "fuente": g["sources"].map(_fuente) if "sources" in g else None,
    }, geometry=g.geometry.values, crs=4326)
    out = out[out.geometry.notna() & out.geom_type.isin(["Polygon", "MultiPolygon"])]
    return out.drop_duplicates("id").reset_index(drop=True)


def asignar_comunas(edif: gpd.GeoDataFrame, comunas: gpd.GeoDataFrame, f_cut: str, f_nom: str,
                    f_reg: str) -> tuple[gpd.GeoDataFrame, int]:
    """Cada edificio a la comuna donde tiene más área (no se parte). Devuelve (asignados, n_fuera_de_la_dpa)."""
    ea = edif.to_crs(CRS_AREA)
    ca = comunas[[f_cut, f_nom, f_reg, "geometry"]].to_crs(CRS_AREA).reset_index(drop=True)
    ca["geometry"] = shapely.make_valid(ca.geometry.values)
    i_e, i_c = ca.sindex.query(ea.geometry.values, predicate="intersects")
    if len(i_e) == 0:
        return gpd.GeoDataFrame(columns=COLUMNAS, geometry="geometry", crs=4326), len(edif)
    pares = pd.DataFrame({"e": i_e, "c": i_c})
    multi = pares.e.duplicated(keep=False)
    pares["a"] = 1.0
    if multi.any():   # solo se calcula el área para los edificios que tocan más de una comuna
        sub = pares[multi]
        pares.loc[multi, "a"] = shapely.area(shapely.intersection(
            shapely.make_valid(ea.geometry.values[sub.e.values]), ca.geometry.values[sub.c.values]))
    mejor = pares.sort_values(["e", "a"], ascending=[True, False]).drop_duplicates("e")
    mejor = mejor[mejor.a > 0]
    out = edif.iloc[mejor.e.values].copy()
    out["cut"] = ca[f_cut].astype(str).str.zfill(5).values[mejor.c.values]
    out["comuna"] = ca[f_nom].values[mejor.c.values]
    out["region"] = ca[f_reg].values[mejor.c.values]
    out["area_m2"] = np.round(shapely.area(ea.geometry.values[mejor.e.values]), 1)
    return out[COLUMNAS].reset_index(drop=True), len(edif) - len(out)


def _escribir_json(p: Path, d: dict) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def procesar_region(region: str, comunas_todas: gpd.GeoDataFrame, f_cut: str, f_nom: str, f_reg: str,
                    dir_fp: Path, release: str, descargar: Callable = descargar_overture, refresh: bool = False,
                    conservar_crudo: bool = False, dir_manifiestos_repo: Path | None = None) -> dict:
    """Descarga, recorta y asigna los edificios de una región. Devuelve su manifiesto."""
    s = slug(region)
    dir_fp.mkdir(parents=True, exist_ok=True)
    p_man = dir_fp / f"{s}.manifest.json"
    if p_man.exists() and not refresh:
        m = json.loads(p_man.read_text(encoding="utf-8"))
        if m.get("completa") and m.get("release") == release:
            log.info("Footprints %s: completa (%s edificios, release %s) · se salta", region, m["n_edificios"], release)
            return m | {"saltada": True}
    t0 = time.time()
    com_reg = comunas_todas[comunas_todas[f_reg] == region]
    bbox = bbox_con_margen(com_reg.geometry)
    _escribir_json(p_man, {"region": region, "release": release, "bbox_wsen": list(bbox), "completa": False,
                           "inicio": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    crudo = dir_fp / "_crudo" / f"{s}_{release}.parquet"
    crudo.parent.mkdir(parents=True, exist_ok=True)
    if refresh or not crudo.exists():
        tmp = crudo.with_name(crudo.stem + ".descargando.parquet")
        tmp.unlink(missing_ok=True)
        descargar(bbox, tmp, release)
        tmp.replace(crudo)
        for st in crudo.parent.glob(tmp.name + "*.state"):   # estado de reanudación de la CLI de overturemaps
            st.unlink(missing_ok=True)
    edif = normalizar(gpd.read_parquet(crudo))
    caja = shapely.box(*bbox)
    candidatas = comunas_todas[comunas_todas.to_crs(4326).intersects(caja)]   # incluye vecinas de otras regiones
    asignados, fuera = asignar_comunas(edif, candidatas, f_cut, f_nom, f_reg)
    propios = asignados[asignados["region"] == region].reset_index(drop=True)
    otras = len(asignados) - len(propios)
    destino = dir_fp / f"{s}.parquet"
    tmp = destino.with_name(destino.stem + ".escribiendo.parquet")
    propios.to_parquet(tmp, index=False)
    tmp.replace(destino)
    por_comuna = propios.groupby(["cut", "comuna"]).size().rename("n").reset_index()
    m = {"region": region, "release": release, "bbox_wsen": list(bbox), "n_edificios": int(len(propios)),
         "mb": round(destino.stat().st_size / 1e6, 1), "fecha_descarga": datetime.now(timezone.utc).date().isoformat(),
         "comunas": [{"cut": r.cut, "comuna": r.comuna, "n": int(r.n)} for r in por_comuna.itertuples()],
         "n_en_bbox": int(len(edif)), "fuera_de_la_dpa": int(fuera), "de_otras_regiones": int(otras),
         "duracion_s": round(time.time() - t0, 1), "archivo": destino.name, "licencia": "ODbL",
         "atribucion": ATRIBUCION, "completa": True}
    _escribir_json(p_man, m)
    if dir_manifiestos_repo is not None:   # copia versionable (sin datos) para que los agentes sepan qué existe
        dir_manifiestos_repo.mkdir(parents=True, exist_ok=True)
        _escribir_json(dir_manifiestos_repo / p_man.name, m)
    if not conservar_crudo:
        crudo.unlink(missing_ok=True)
    return m


def descargar_regiones(patron: str, comunas_todas: gpd.GeoDataFrame, f_cut: str, f_nom: str, f_reg: str,
                       dir_fp: Path, release: str | None = None, **kw) -> list[dict]:
    regiones = regiones_que_calzan(sorted(comunas_todas[f_reg].dropna().unique()), patron)
    if not regiones:
        raise ValueError(f"Ninguna región calza con {patron!r}")
    release = release or ultimo_release()
    out = []
    for i, r in enumerate(regiones, 1):
        progreso.paso("footprints", r, i, len(regiones))
        out.append(procesar_region(r, comunas_todas, f_cut, f_nom, f_reg, dir_fp, release, **kw))
    return out


def estado(dir_fp: Path) -> pd.DataFrame:
    """región | edificios | MB | release | fecha | completa (desde los manifiestos)."""
    filas = []
    for p in sorted(dir_fp.glob("*.manifest.json")):
        m = json.loads(p.read_text(encoding="utf-8"))
        filas.append({"region": m.get("region"), "edificios": m.get("n_edificios"), "MB": m.get("mb"),
                      "release": m.get("release"), "fecha": m.get("fecha_descarga") or m.get("inicio"),
                      "completa": bool(m.get("completa"))})
    return pd.DataFrame(filas, columns=["region", "edificios", "MB", "release", "fecha", "completa"])
