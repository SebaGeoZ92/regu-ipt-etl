"""V2 de VOLÚMENES (docs/VOLUMENES.md): volumen calculado (V_calc) por predio, según los registros.

V_calc = huella × pisos estimados, con
- huella = suma del área de los edificios de Overture que caen en más del 50 % dentro del predio;
- pisos = max(1, round(superficie construida SII / huella)); sin superficie construida SII se usa `num_floors`
  de Overture si existe y, si no, el predio queda `sin_dato` (no se inventa).

Se entrega `m2_equiv = huella × pisos` (m² construidos equivalentes). El volumen en m³ exige la altura de piso de
referencia, que la ordenanza de Temuco no fija: `v_calc_m3` queda vacío mientras no se entregue `altura_piso_ref_m`.
Cada valor lleva su fuente y su confianza.
"""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import shape

CRS_AREA = "ESRI:102033"
UMBRAL_DENTRO = 0.5
COLUMNAS = ["rol", "direccion", "zona", "sup_terreno_m2", "sup_construida_sii_m2", "n_edificios", "huella_m2", "pisos_est",
            "pisos_max_sii", "m2_equiv", "v_calc_m3", "ocupacion_predio", "estado", "fuente_huella", "fuente_pisos",
            "confianza_pisos", "avisos"]


def cargar_predios(path: Path) -> gpd.GeoDataFrame:
    """Predios guardados desde catastral.cl (JSON con `predios[]` y `geometry` GeoJSON en EPSG:4326)."""
    datos = json.loads(Path(path).read_text(encoding="utf-8"))["predios"]
    return gpd.GeoDataFrame(pd.DataFrame(datos).drop(columns="geometry"),
                            geometry=[shape(p["geometry"]) for p in datos], crs="EPSG:4326")


def calcular(predios: gpd.GeoDataFrame, edificios: gpd.GeoDataFrame, altura_piso_ref_m: float | None = None,
             zonas: gpd.GeoDataFrame | None = None) -> pd.DataFrame:
    """V_calc por predio. `zonas` (opcional) = piezas PRC con `zona` para etiquetar la zona de cada predio."""
    p = predios.to_crs(CRS_AREA).reset_index(drop=True)
    e = edificios.to_crs(CRS_AREA).reset_index(drop=True)
    e["geometry"] = shapely.make_valid(e.geometry.values)
    e["m2"] = e.geometry.area
    par = gpd.sjoin(e[["geometry", "m2"]], p[["geometry"]], how="inner", predicate="intersects")
    dentro = shapely.area(shapely.intersection(e.geometry.loc[par.index].values, p.geometry.iloc[par["index_right"].values].values))
    par = par.assign(frac=dentro / par["m2"].values)
    par = par[par["frac"] > UMBRAL_DENTRO]
    por_predio = par.groupby("index_right").agg(n=("m2", "size"), huella=("m2", "sum"))
    nf = edificios["num_floors"] if "num_floors" in edificios.columns else pd.Series(np.nan, index=edificios.index)
    pisos_ov = (pd.Series(nf.values, index=e.index).loc[par.index].groupby(par["index_right"]).max())

    zona = pd.Series("", index=p.index)
    if zonas is not None and len(zonas):
        z = zonas.to_crs(CRS_AREA)
        pts = gpd.GeoDataFrame(geometry=p.geometry.representative_point(), crs=CRS_AREA)
        zj = gpd.sjoin(pts, z[["geometry", "zona"]], how="left", predicate="within")
        zona = zj.groupby(level=0)["zona"].first().reindex(p.index).fillna("")

    filas = []
    for i, r in p.iterrows():
        n = int(por_predio["n"].get(i, 0))
        huella = float(por_predio["huella"].get(i, 0.0))
        sup_c = float(r.get("sup_construida_total") or 0) or np.nan
        avisos, estado, pisos, fuente, conf = [], "ok", np.nan, "", ""
        sup_t = float(r.get("m2_terreno") or r.geometry.area)
        if huella <= 0:
            estado = "sin_dato"
            avisos.append("sin huella de Overture dentro del predio")
        elif not np.isnan(sup_c):
            pisos, fuente, conf = max(1, round(sup_c / huella)), "sii", "media"
        elif pd.notna(pisos_ov.get(i, np.nan)):
            pisos, fuente, conf = int(pisos_ov[i]), "overture_num_floors", "media"
        else:
            estado = "sin_dato"
            avisos.append("sin superficie construida SII ni num_floors de Overture")
        if huella > 0 and not np.isnan(sup_c) and sup_c / huella < 0.5:
            avisos.append("la construida SII es menos de la mitad de la huella: puede haber construcción sin registrar")
        if sup_t and huella > sup_t * 1.05:
            avisos.append("la huella supera la superficie del predio")
        m2 = huella * pisos if estado == "ok" else np.nan
        pm = r.get("pisos_max")
        filas.append({
            "rol": r["rol"], "direccion": r.get("direccion", ""), "zona": zona.get(i, ""), "sup_terreno_m2": round(sup_t, 1),
            "sup_construida_sii_m2": sup_c, "n_edificios": n, "huella_m2": round(huella, 1), "pisos_est": pisos,
            "pisos_max_sii": pm if pd.notna(pm) else np.nan, "m2_equiv": round(m2, 1) if estado == "ok" else np.nan,
            "v_calc_m3": round(m2 * altura_piso_ref_m, 1) if (estado == "ok" and altura_piso_ref_m) else np.nan,
            "ocupacion_predio": round(huella / sup_t, 3) if sup_t else np.nan, "estado": estado,
            "fuente_huella": "overture" if n else "", "fuente_pisos": fuente, "confianza_pisos": conf, "avisos": "; ".join(avisos),
        })
    return pd.DataFrame(filas, columns=COLUMNAS)
