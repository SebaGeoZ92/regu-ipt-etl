"""Ficha normativa preliminar de un punto o polígono (germen del pre-CIP).

Contrato (docs/CONDICIONANTES.md), MVP sin condicionantes:
    {
      "comuna": "...", "cut": "...", "region": "...",
      "particion": [{"clase", "pct", "ipt_tipo", "ipt", "zona", "norma_titulo", "riesgo_pct"}],
      "riesgo_pct": float,
      "afectaciones": [{"tipo", "capa", "zona", "detalle", "riesgo", "pct"}],
      "condicionantes": [],            # fase siguiente
      "cobertura": "completa" | "parcial" | "fuera_dpa",
      "fuera_de_cobertura": bool,      # cobertura != "completa"
      "motivo": str | None,            # texto para el usuario cuando no es "completa"
      "aviso": "..."
    }
Lee el GPKG de build (capas capa_ipt y afectaciones) con filtro por bbox, que usa el índice espacial del GPKG.
Para un punto, pct = 100 en la pieza que lo contiene; para un polígono, % del área del polígono (ESRI:102033).
"""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry.base import BaseGeometry

CRS_AREA = "ESRI:102033"
MOTIVO_FUERA_DPA = ("Fuera de cobertura DPA: el punto no cae en ninguna comuna de la División Político-Administrativa "
                    "(BCN) ni en la extensión costera de un instrumento comunal. Puede ser mar o un borde costero "
                    "mal representado.")
AVISO_DEF = ("Información referencial. No reemplaza el Certificado de Informaciones Previas (CIP), "
             "que emite solo la Dirección de Obras Municipales (OGUC art. 1.4.4).")


def _leer(gpkg: Path, capa: str, geom: BaseGeometry) -> gpd.GeoDataFrame:
    try:
        g = gpd.read_file(gpkg, layer=capa, bbox=tuple(geom.bounds))
    except Exception:   # capa inexistente (p.ej. sin afectaciones)
        return gpd.GeoDataFrame(geometry=[], crs=4326)
    return g[g.intersects(geom)] if len(g) else g


def _txt(v):
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else v


def ficha(geom_4326: BaseGeometry, gpkg: str | Path) -> dict:
    """Ficha de un punto o polígono en EPSG:4326 contra un GPKG de build."""
    gpkg = Path(gpkg)
    es_punto = geom_4326.geom_type in ("Point", "MultiPoint")
    capa = _leer(gpkg, "capa_ipt", geom_4326)
    afec = _leer(gpkg, "afectaciones", geom_4326)
    aviso = _txt(capa["aviso"].iloc[0]) if len(capa) and "aviso" in capa else None
    out = {"comuna": None, "cut": None, "region": None, "particion": [], "riesgo_pct": 0.0,
           "afectaciones": [], "condicionantes": [], "cobertura": "completa", "fuera_de_cobertura": False,
           "motivo": None, "aviso": aviso or AVISO_DEF}

    def _fuera_dpa(o):
        o.update(cobertura="fuera_dpa", fuera_de_cobertura=True, motivo=MOTIVO_FUERA_DPA)
        return o

    if capa.empty:
        return _fuera_dpa(out)

    g_area = gpd.GeoSeries([geom_4326], crs=4326).to_crs(CRS_AREA).iloc[0]
    capa = capa.to_crs(CRS_AREA)
    if es_punto:
        capa = capa[capa.intersects(g_area)].head(1)     # el punto cae en una sola pieza (partición planar)
        capa["a"] = 1.0
        total = 1.0
    else:
        capa["a"] = shapely.area(shapely.intersection(capa.geometry.values, g_area))
        capa = capa[capa["a"] > 0]
        total = float(g_area.area)
    cubierto = float(capa["a"].sum())
    if capa.empty or cubierto == 0:
        return _fuera_dpa(out)

    principal = capa.loc[capa["a"].idxmax()]
    out.update(comuna=_txt(principal.get("comuna")), cut=_txt(principal.get("cut")), region=_txt(principal.get("region")))
    capa["riesgo"] = capa["riesgo"].fillna(False).astype(bool)
    claves = ["clase", "ipt_tipo", "ipt_nombre", "zona", "norma_titulo"]
    part = (capa.assign(**{k: capa[k].astype(object).where(capa[k].notna(), None) for k in claves},
                        a_riesgo=capa["a"].where(capa["riesgo"], 0.0))
                .groupby(claves, dropna=False)[["a", "a_riesgo"]].sum().reset_index()
                .sort_values("a", ascending=False))
    out["particion"] = [{"clase": r.clase, "pct": round(100 * r.a / total, 2), "ipt_tipo": _txt(r.ipt_tipo),
                         "ipt": _txt(r.ipt_nombre), "zona": _txt(r.zona), "norma_titulo": _txt(r.norma_titulo),
                         "riesgo_pct": round(100 * r.a_riesgo / total, 2)} for r in part.itertuples()]
    out["riesgo_pct"] = round(100 * float(capa.loc[capa["riesgo"], "a"].sum()) / total, 2)
    if cubierto < total * 0.9999 and not es_punto:   # parte del polígono cae fuera de la partición
        out.update(cobertura="parcial", fuera_de_cobertura=True,
                   motivo=f"{round(100 * (1 - cubierto / total), 2)}% del polígono queda fuera de cobertura DPA "
                          f"(no cae en ninguna comuna).")

    if len(afec):
        afec = afec.to_crs(CRS_AREA)
        afec["a"] = 1.0 if es_punto else shapely.area(shapely.intersection(afec.geometry.values, g_area))
        afec = afec[afec["a"] > 0]
        for r in afec.sort_values("a", ascending=False).itertuples():
            det = _txt(getattr(r, "zona_desc", None))
            out["afectaciones"].append({
                "tipo": _txt(r.ipt_tipo), "capa": _txt(r.capa), "zona": _txt(r.zona),
                "detalle": det[:200] if isinstance(det, str) else det, "riesgo": bool(getattr(r, "riesgo", False)),
                "pct": 100.0 if es_punto else round(100 * r.a / total, 2)})
    return out


def ultimo_gpkg(out_dir: str | Path) -> Path | None:
    """GPKG de build más reciente (prefiere el nacional)."""
    out_dir = Path(out_dir)
    for patron in ("regu_ipt_nacional_*.gpkg", "regu_ipt_*.gpkg"):
        c = sorted(out_dir.glob(patron))
        if c:
            return c[-1]
    return None


def a_json(f: dict) -> str:
    return json.dumps(f, ensure_ascii=False, indent=2)
