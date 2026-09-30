"""Piloto de volumen (docs/VOLUMEN_PILOTO.md). Por ahora solo el PASO 0: ayudar a Mario a elegir la zona.

- candidatas(): zonas de un PRC con ha, n_predios (si hay catastro local), n_footprints (Overture) y si el Portal
  IPT enlaza la ordenanza. `python run.py volumen candidatas --ipt "Temuco"`.
- descargar_footprints(): edificios de Overture Maps (ODbL) para el bbox del PRC, en data/base/footprints/, con
  release, fecha y atribución. Capa APARTE: nunca se fusiona con capa_ipt ni con el Atlas.
- plantilla_normas(): agrega a data/base/normas_zona.csv una fila vacía para la zona elegida. Las normas NO se
  inventan: las llena el arquitecto (estado FICTICIO | BORRADOR | VALIDADO; solo VALIDADO se muestra).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd

from .normalize import norm_txt

COLUMNAS_NORMAS = ["ipt_nombre", "zona", "ocupacion_max", "constructibilidad_max", "altura_max_pisos", "altura_max_m",
                   "altura_piso_ref_m", "antejardin_m", "distanciamiento_m", "agrupamiento", "rasante_grados",
                   "densidad_max", "articulo_fuente", "validado_por", "fecha_validacion", "estado"]
ESTADOS_NORMA = {"FICTICIO", "BORRADOR", "VALIDADO"}
ATRIBUCION_OVERTURE = "© OpenStreetMap contributors, Overture Maps Foundation (ODbL)"
_RE_RESIDENCIAL = re.compile(r"RESIDENC|HABITAC|VIVIEND|\bH\s?\d|\bZH|\bZR\d", re.I)
CRS_AREA = "ESRI:102033"


def zonas_prc(capa: gpd.GeoDataFrame, ipt: str) -> gpd.GeoDataFrame:
    """Piezas de la partición que pertenecen al PRC `ipt` (comparación sin tildes ni mayúsculas)."""
    m = (capa["ipt_tipo"] == "PRC") & (capa["ipt_nombre"].map(norm_txt) == norm_txt(ipt))
    return capa[m].copy()


def bbox_ipt(capa: gpd.GeoDataFrame, ipt: str, margen_m: float = 200.0) -> tuple[float, float, float, float]:
    z = zonas_prc(capa, ipt)
    if z.empty:
        raise ValueError(f"No hay zonas PRC con ipt_nombre {ipt!r}")
    b = z.to_crs(CRS_AREA).total_bounds
    caja = gpd.GeoSeries.from_xy([b[0] - margen_m, b[2] + margen_m], [b[1] - margen_m, b[3] + margen_m], crs=CRS_AREA)
    x0, y0, x1, y1 = caja.to_crs(4326).total_bounds
    return round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6)


def descargar_footprints(bbox: tuple, destino_dir: Path, nombre: str, release: str | None = None) -> Path:
    """Overture 'building' en GeoParquet para bbox (W,S,E,N) + metadatos (release, fecha, atribución)."""
    from overturemaps import core
    release = release or core.get_latest_release()
    destino_dir.mkdir(parents=True, exist_ok=True)
    out = destino_dir / f"overture_building_{release}_{nombre}.parquet"
    if not out.exists():
        cli = Path(sys.executable).with_name("overturemaps.exe")
        cmd = [str(cli) if cli.exists() else "overturemaps", "download", "--bbox=" + ",".join(map(str, bbox)),
               "-f", "geoparquet", "--type=building", "-r", release, "-o", str(out)]
        subprocess.run(cmd, check=True)
    meta = {"fuente": "Overture Maps Foundation", "tipo": "building", "release": release, "bbox_wsen": list(bbox),
            "descargado": datetime.now(timezone.utc).isoformat(timespec="seconds"), "licencia": "ODbL",
            "atribucion": ATRIBUCION_OVERTURE, "archivo": out.name,
            "nota": "Capa aparte: no se fusiona con capa_ipt ni con el Atlas (docs/VOLUMEN_PILOTO.md, regla 2)."}
    out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _conteo_en_zonas(z: gpd.GeoDataFrame, puntos: gpd.GeoDataFrame) -> pd.Series:
    """Cuántos objetos (por su punto representativo) caen en cada zona."""
    if puntos is None or puntos.empty:
        return pd.Series(dtype=int)
    p = gpd.GeoDataFrame(geometry=puntos.to_crs(z.crs).geometry.representative_point(), crs=z.crs)
    j = gpd.sjoin(p, z[["zona", "geometry"]], predicate="within")
    return j.groupby("zona").size()


def candidatas(capa: gpd.GeoDataFrame, ipt: str, footprints: gpd.GeoDataFrame | None = None,
               predios: gpd.GeoDataFrame | None = None, match: pd.DataFrame | None = None) -> pd.DataFrame:
    """zona | descripción | ha | n_predios | n_footprints | % con altura | % riesgo | residencial | ordenanza."""
    z = zonas_prc(capa, ipt)
    if z.empty:
        raise ValueError(f"No hay zonas PRC con ipt_nombre {ipt!r}")
    z = z.to_crs(CRS_AREA)
    z["a"] = z.area
    z["a_riesgo"] = z["a"].where(z["riesgo"].fillna(False).astype(bool), 0.0)
    g = z.groupby("zona", dropna=False).agg(descripcion=("zona_desc", "first"), m2=("a", "sum"), m2_riesgo=("a_riesgo", "sum"),
                                            piezas=("a", "size"), cut=("cut", "first")).reset_index()
    g["ha"] = (g.m2 / 1e4).round(2)
    g["pct_riesgo"] = (100 * g.m2_riesgo / g.m2).round(1)
    g["residencial"] = [bool(_RE_RESIDENCIAL.search(f"{a} {b}")) for a, b in zip(g.zona, g.descripcion.fillna(""))]
    g["n_footprints"] = g.zona.map(_conteo_en_zonas(z, footprints)).fillna(0).astype(int) if footprints is not None else None
    if footprints is not None and "height" in footprints:
        fz = gpd.sjoin(gpd.GeoDataFrame(footprints[["height"]], geometry=footprints.to_crs(z.crs).geometry.representative_point(),
                                        crs=z.crs), z[["zona", "geometry"]], predicate="within")
        g["pct_con_altura"] = g.zona.map(fz.groupby("zona")["height"].apply(lambda s: round(100 * s.notna().mean(), 1)))
    g["n_predios"] = g.zona.map(_conteo_en_zonas(z, predios)).fillna(0).astype(int) if predios is not None else "sin datos"
    orden = None
    if match is not None and len(match):
        m = match[(match.ipt_tipo == "PRC") & (match.ipt_nombre.map(norm_txt) == norm_txt(ipt))]
        orden = next((u for u in m.ordenanza_url if isinstance(u, str) and u), None)
    g["ordenanza_portal"] = "sí" if orden else "no"
    g["ordenanza_url"] = orden or ""
    cols = ["zona", "descripcion", "ha", "n_predios", "n_footprints", "pct_con_altura", "pct_riesgo", "residencial",
            "piezas", "ordenanza_portal", "ordenanza_url", "cut"]
    return g.reindex(columns=cols).sort_values(["residencial", "ha"], ascending=[False, False]).reset_index(drop=True)


def plantilla_normas(path: Path, ipt_nombre: str, zona: str) -> pd.DataFrame:
    """Agrega (sin duplicar) una fila con las columnas vacías para (ipt_nombre, zona)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    df = (pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig") if path.exists()
          else pd.DataFrame(columns=COLUMNAS_NORMAS))
    faltan = [c for c in COLUMNAS_NORMAS if c not in df.columns]
    if faltan:
        raise ValueError(f"{path} no tiene las columnas {faltan}")
    ya = ((df.ipt_nombre.map(norm_txt) == norm_txt(ipt_nombre)) & (df.zona.map(norm_txt) == norm_txt(zona))).any()
    if not ya:
        fila = {c: "" for c in COLUMNAS_NORMAS} | {"ipt_nombre": ipt_nombre, "zona": zona}
        df = pd.concat([df, pd.DataFrame([fila])], ignore_index=True)
        df.to_csv(path, index=False, encoding="utf-8-sig")
    return df
