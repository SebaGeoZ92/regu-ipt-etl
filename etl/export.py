"""Escritura de productos: GeoPackage (maestro), GeoJSON (nacional y por región), QA y PostGIS opcional."""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd

from .normalize import norm_txt, polygonal

log = logging.getLogger(__name__)


def anotar_legal(capa: gpd.GeoDataFrame, legal: dict) -> gpd.GeoDataFrame:
    clases = legal["clases"]
    capa["norma_titulo"] = capa["clase"].map(lambda c: clases.get(c, {}).get("titulo"))
    capa["norma_resumen"] = capa["clase"].map(lambda c: clases.get(c, {}).get("resumen"))
    capa["norma_refs"] = capa["clase"].map(lambda c: "; ".join(clases.get(c, {}).get("normas", [])))
    capa["aviso"] = legal.get("_aviso")
    return capa


def _validar(g: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, int, int]:
    """Reproyectar puede invalidar polígonos casi degenerados (autointersección, anillos de <4 puntos).
    Corrige con make_valid y se queda con la parte poligonal; elimina las que quedan vacías (astillas)."""
    inv = ~g.is_valid
    if not inv.any():
        return g, 0, 0
    g = g.copy()
    g.loc[inv, "geometry"] = [polygonal(x) for x in g.loc[inv, "geometry"]]
    vacias = g.geometry.isna() | g.geometry.is_empty
    return g[~vacias], int(inv.sum() - vacias.sum()), int(vacias.sum())


def escribir(capa: gpd.GeoDataFrame, afect: gpd.GeoDataFrame | None, qas: list[dict],
             out_dir: Path, cfg: dict, sufijo: str = "nacional",
             sin_comuna: pd.DataFrame | None = None, fuera_dpa: pd.DataFrame | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    crs_out = cfg["crs"]["salida"]
    prec = int(cfg["build"]["precision_geojson"])
    capa, n_corr, n_elim = _validar(capa.to_crs(crs_out))
    if n_corr or n_elim:
        log.warning("Exportación: %d piezas inválidas tras reproyectar a %s corregidas con make_valid, "
                    "%d degeneradas eliminadas", n_corr, crs_out, n_elim)
    tag = f"{sufijo}_{date.today():%Y%m%d}"
    productos = {}

    gpkg = out_dir / f"regu_ipt_{tag}.gpkg"
    capa.to_file(gpkg, layer="capa_ipt", driver="GPKG")
    if afect is not None and not afect.empty:
        _validar(afect.to_crs(crs_out))[0].to_file(gpkg, layer="afectaciones", driver="GPKG")
    qa_df = pd.DataFrame(qas)
    qa_df.to_csv(out_dir / f"qa_comunas_{tag}.csv", index=False, encoding="utf-8-sig")
    if sin_comuna is not None:
        sin_comuna.to_csv(out_dir / f"qa_sin_comuna_{tag}.csv", index=False, encoding="utf-8-sig")
    if fuera_dpa is not None:
        fuera_dpa.to_csv(out_dir / f"qa_fuera_dpa_{tag}.csv", index=False, encoding="utf-8-sig")
    productos["gpkg"] = str(gpkg)

    cols = [c for c in capa.columns if cfg["build"]["incluir_attrs_raw_geojson"] or c != "attrs_raw"]
    gj = out_dir / f"regu_ipt_{tag}.geojson"
    capa[cols].to_file(gj, driver="GeoJSON", COORDINATE_PRECISION=prec, RFC7946="YES")
    productos["geojson"] = str(gj)

    reg_dir = out_dir / "por_region"
    reg_dir.mkdir(exist_ok=True)
    for reg, sub in capa[cols].groupby("region"):
        p = reg_dir / f"regu_ipt_{norm_txt(reg).lower().replace(' ', '_')}.geojson"
        sub.to_file(p, driver="GeoJSON", COORDINATE_PRECISION=prec, RFC7946="YES")

    resumen = {
        "fecha": date.today().isoformat(),
        "comunas": len(qa_df),
        "piezas": len(capa),
        "comunas_sin_urbano": int(qa_df["sin_urbano"].sum()) if "sin_urbano" in qa_df else None,
        "cobertura_min_pct": float(qa_df["cobertura_pct"].min()) if len(qa_df) else None,
        "cobertura_max_pct": float(qa_df["cobertura_pct"].max()) if len(qa_df) else None,
        "traslape_max_m2": float(qa_df["traslape_m2"].max()) if "traslape_m2" in qa_df else None,
        "comunas_traslape_sobre_umbral": int((~qa_df["traslape_ok"].astype(bool)).sum()) if "traslape_ok" in qa_df else None,
        "piezas_por_clase": capa["clase"].value_counts().to_dict(),
        "zonas_pri_a_revisar": int(capa["revisar"].fillna(False).astype(bool).sum()),
        "instrumentos_sin_comuna": len(sin_comuna) if sin_comuna is not None else None,
        "ipt_fuera_dpa_ha": round(float(fuera_dpa["ha_fuera"].sum()), 1) if fuera_dpa is not None and len(fuera_dpa) else 0.0,
        "piezas_invalidas": int((~capa.is_valid).sum()),
        "piezas_corregidas_export": n_corr,
        "piezas_degeneradas_eliminadas": n_elim,
        "rescates_geos": {k: int(qa_df[k].sum()) for k in ("particion_rescates", "riesgo_rescates") if k in qa_df},
    }
    (out_dir / f"resumen_{tag}.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")
    productos["resumen"] = resumen
    return productos


def cargar_postgis(capa: gpd.GeoDataFrame, afect: gpd.GeoDataFrame | None, schema: str = "regu") -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        log.info("DATABASE_URL no definida: se omite PostGIS")
        return
    from sqlalchemy import create_engine, text  # opcional
    eng = create_engine(url)
    with eng.begin() as con:
        con.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
    capa.to_crs(4326).to_postgis("capa_ipt", eng, schema=schema, if_exists="replace", index=False)
    if afect is not None and not afect.empty:
        afect.to_crs(4326).to_postgis("afectaciones", eng, schema=schema, if_exists="replace", index=False)
    with eng.begin() as con:
        con.execute(text(f"CREATE INDEX IF NOT EXISTS capa_ipt_gix ON {schema}.capa_ipt USING GIST (geometry)"))
        con.execute(text(f"CREATE INDEX IF NOT EXISTS capa_ipt_cut ON {schema}.capa_ipt (cut)"))
    log.info("Cargado a PostGIS en esquema %s", schema)
