"""Etapa 1 de VOLÚMENES: ocupación real del suelo por zona de PRC.

Cruza las piezas PRC de `capa_ipt` con las huellas de edificios (Overture, ODbL) y entrega, por (ipt, zona):
hectáreas de la zona, m² de huella, coeficiente de ocupación existente y n.º de edificios.

Criterio de medición (cuidado al interpretar):
- La huella es la parte de cada edificio que cae DENTRO de la zona (recorte, no centroide): un edificio sobre el borde
  de dos zonas reparte su superficie entre ambas.
- El n.º de edificios cuenta cada edificio una sola vez, en la zona que contiene su punto representativo.
- El coeficiente es huella / área BRUTA de la zona (incluye calles, plazas y áreas verdes). **No es** el "coeficiente de
  ocupación de suelo" de la OGUC, que se mide sobre el predio neto; sirve para comparar zonas y ver cuánto suelo
  está ocupado de hecho, no para verificar la norma.
- Las piezas con `riesgo=True` siguen sumando a su zona: el riesgo es una superposición, no otra zona.
"""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

COLUMNAS = ["ipt", "zona", "ha", "m2_huella", "coef_ocupacion", "n_edificios", "region", "comunas"]

# Tramos del coeficiente para colorear el mapa (límite superior excluyente, etiqueta, color)
TRAMOS = [
    (0.02, "< 2 %", "#ffffcc"),
    (0.05, "2 – 5 %", "#c7e9b4"),
    (0.10, "5 – 10 %", "#7fcdbb"),
    (0.20, "10 – 20 %", "#41b6c4"),
    (0.30, "20 – 30 %", "#2c7fb8"),
    (np.inf, "≥ 30 %", "#253494"),
]
SIN_DATOS = ("sin edificios", "#d9d9d9")
SIN_ZONA = "(sin zona)"


def tramo(coef: float, n: int) -> tuple[str, str]:
    """(etiqueta, color) del coeficiente; las zonas sin ningún edificio van aparte (suele ser dato faltante)."""
    if n == 0:
        return SIN_DATOS
    for tope, etq, col in TRAMOS:
        if coef < tope:
            return etq, col
    return TRAMOS[-1][1:]


def regiones_con_footprints(dir_fp: Path) -> dict[str, Path]:
    """{región: parquet} de las regiones con descarga completa (manifiesto `completa: true`)."""
    res = {}
    for m in sorted(Path(dir_fp).glob("*.manifest.json")):
        info = json.loads(m.read_text(encoding="utf-8"))
        pq = m.with_name(m.name.replace(".manifest.json", ".parquet"))
        if info.get("completa") and pq.exists():
            res[info["region"]] = pq
    return res


def calcular(capa: gpd.GeoDataFrame, edificios: gpd.GeoDataFrame, crs_area: str = "ESRI:102033") -> pd.DataFrame:
    """Ocupación por (ipt, zona) de las piezas PRC de `capa` con los `edificios` dados (ambos con CRS definido)."""
    z = capa[capa["fuente"] == "PRC"].copy()
    if z.empty:
        return pd.DataFrame(columns=COLUMNAS)
    z = z.to_crs(crs_area).reset_index(drop=True)
    z["geometry"] = shapely.make_valid(z.geometry.values)
    z["zona"] = z["zona"].fillna(SIN_ZONA).astype(str).str.strip().replace("", SIN_ZONA)
    z["m2_pieza"] = z.geometry.area

    e = edificios[["geometry"]].to_crs(crs_area)
    e["geometry"] = shapely.make_valid(e.geometry.values)

    pares = gpd.sjoin(e, z[["geometry"]], how="inner", predicate="intersects")
    ge = e.geometry.loc[pares.index].values
    gz = z.geometry.iloc[pares["index_right"].values].values
    huella = shapely.area(shapely.intersection(ge, gz))
    dentro = shapely.contains(gz, shapely.point_on_surface(ge))
    pz = pd.DataFrame({"pieza": pares["index_right"].values, "huella": huella, "n": dentro.astype(int)})
    por_pieza = pz.groupby("pieza").agg(m2_huella=("huella", "sum"), n_edificios=("n", "sum"))
    z = z.join(por_pieza, how="left").fillna({"m2_huella": 0.0, "n_edificios": 0})

    g = z.groupby(["ipt_nombre", "zona"], dropna=False).agg(
        m2=("m2_pieza", "sum"), m2_huella=("m2_huella", "sum"), n_edificios=("n_edificios", "sum"),
        region=("region", lambda s: "; ".join(sorted(set(s.dropna())))),
        comunas=("comuna", lambda s: "; ".join(sorted(set(s.dropna())))),
    ).reset_index()
    g["ha"] = g["m2"] / 1e4
    g["coef_ocupacion"] = np.where(g["m2"] > 0, g["m2_huella"] / g["m2"], np.nan)
    g = g.rename(columns={"ipt_nombre": "ipt"})
    g["n_edificios"] = g["n_edificios"].astype(int)
    return g[COLUMNAS].sort_values(["ipt", "zona"]).reset_index(drop=True)


def combinar(partes: list[pd.DataFrame]) -> pd.DataFrame:
    """Une resultados regionales; una misma (ipt, zona) que cruce regiones se suma y el coeficiente se recalcula."""
    df = pd.concat(partes, ignore_index=True)
    df["m2"] = df["ha"] * 1e4
    g = df.groupby(["ipt", "zona"], dropna=False).agg(
        m2=("m2", "sum"), m2_huella=("m2_huella", "sum"), n_edificios=("n_edificios", "sum"),
        region=("region", lambda s: "; ".join(sorted({x for v in s for x in v.split("; ") if x}))),
        comunas=("comunas", lambda s: "; ".join(sorted({x for v in s for x in v.split("; ") if x}))),
    ).reset_index()
    g["ha"] = g["m2"] / 1e4
    g["coef_ocupacion"] = np.where(g["m2"] > 0, g["m2_huella"] / g["m2"], np.nan)
    return g[COLUMNAS].sort_values(["ipt", "zona"]).reset_index(drop=True)


def formato_csv(df: pd.DataFrame) -> pd.DataFrame:
    """Redondea para el CSV (ha 2 decimales, m² enteros, coeficiente con 4 decimales)."""
    o = df.copy()
    o["ha"] = o["ha"].round(2)
    o["m2_huella"] = o["m2_huella"].round(0).astype(int)
    o["coef_ocupacion"] = o["coef_ocupacion"].round(4)
    return o


def capa_mapa(capa: gpd.GeoDataFrame, tabla: pd.DataFrame, crs_salida: str = "EPSG:4326") -> gpd.GeoDataFrame:
    """Zonas PRC disueltas por (ipt, zona) con el coeficiente y su color, para pintar el mapa."""
    z = capa[capa["fuente"] == "PRC"].copy()
    z["zona"] = z["zona"].fillna(SIN_ZONA).astype(str).str.strip().replace("", SIN_ZONA)
    d = z.dissolve(by=["ipt_nombre", "zona"], as_index=False)[["ipt_nombre", "zona", "geometry"]]
    d = d.rename(columns={"ipt_nombre": "ipt"})
    d = d.merge(tabla[["ipt", "zona", "ha", "m2_huella", "coef_ocupacion", "n_edificios"]], on=["ipt", "zona"], how="inner")
    tr = [tramo(c, n) for c, n in zip(d["coef_ocupacion"], d["n_edificios"])]
    d["tramo"] = [t[0] for t in tr]
    d["color"] = [t[1] for t in tr]
    d["ha"] = d["ha"].round(2)
    d["m2_huella"] = d["m2_huella"].round(0)
    d["coef_ocupacion"] = d["coef_ocupacion"].round(4)
    return gpd.GeoDataFrame(d, geometry="geometry", crs=z.crs).to_crs(crs_salida)


def qa(tabla: pd.DataFrame) -> dict:
    """Resumen de control: coeficientes imposibles (> 100 %), zonas sin edificios y totales."""
    return {
        "zonas": int(len(tabla)),
        "ha": round(float(tabla["ha"].sum()), 1),
        "m2_huella": int(tabla["m2_huella"].sum()),
        "n_edificios": int(tabla["n_edificios"].sum()),
        "coef_global": round(float(tabla["m2_huella"].sum() / (tabla["ha"].sum() * 1e4)), 4),
        "coef_max": round(float(tabla["coef_ocupacion"].max()), 4),
        "zonas_coef_mayor_100": int((tabla["coef_ocupacion"] > 1).sum()),
        "zonas_sin_edificios": int((tabla["n_edificios"] == 0).sum()),
    }
