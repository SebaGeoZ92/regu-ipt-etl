"""Partición planar comuna por comuna, por prioridad normativa.

Orden (gana el primero que cubre el suelo):
  U1  Plan Seccional  >  U1 PRC  >  U2 Límite Urbano  >  U3 urbano PRI/PRM
  >  E extensión urbana PRI/PRM  >  R1 rural PRI/PRM  >  R2 rural sin IPT (remanente)

Resultado: polígonos sin traslapes que cubren el 100% de cada comuna.
"""
from __future__ import annotations

import logging

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from .normalize import COMUNALES

log = logging.getLogger(__name__)

ORDEN = [
    ("U1", "SECCIONAL"),
    ("U1", "PRC"),
    ("U2", "LU"),
    ("U3", "PRI_U"),
    ("E", "PRI_E"),
    ("R1", "PRI_R"),
    ("R1", "PRI_ENV"),   # contorno PRI sin zonificación: solo llena lo que la zonificación PRI no cubre
]
CAMPOS = ["ipt_tipo", "ipt_nombre", "servicio", "capa", "zona", "zona_desc",
          "attrs_raw", "fuente_url", "fecha_extraccion", "revisar", "riesgo"]


def _resolver_traslapes(geoms: np.ndarray, grid: float) -> tuple[np.ndarray, int]:
    """Dentro de una misma fuente, el primero en el arreglo conserva el área disputada.

    Resta secuencial: a cada geometría se le quita la unión de las anteriores que la intersectan
    (ya recortadas). Usa 'intersects' y no 'overlaps', que excluye contención e igualdad
    (p.ej. un LU publicado idéntico en dos capas o una zona contenida en otra).
    """
    if len(geoms) < 2:
        return geoms, 0
    tree = shapely.STRtree(geoms)  # sobre las originales: las recortadas son subconjuntos
    n = 0
    for j in range(1, len(geoms)):
        if geoms[j].is_empty:
            continue
        previas = [geoms[i] for i in tree.query(geoms[j], predicate="intersects")
                   if i < j and not geoms[i].is_empty]
        if not previas:
            continue
        tapa = shapely.union_all(previas, grid_size=grid)
        if shapely.intersection(geoms[j], tapa, grid_size=grid).area > 0:
            geoms[j] = _solo_poligonos(shapely.difference(geoms[j], tapa, grid_size=grid)) or shapely.Polygon()
            n += 1
    return geoms, n


def _solo_poligonos(g):
    if g is None or g.is_empty:
        return None
    if g.geom_type in ("Polygon", "MultiPolygon"):
        return g
    if g.geom_type == "GeometryCollection":
        polys = [p for p in g.geoms if p.geom_type in ("Polygon", "MultiPolygon")]
        return shapely.union_all(polys) if polys else None
    return None


def clasificar_comuna(cut: str, nombre: str, region: str, geom, fuentes: gpd.GeoDataFrame,
                      grid: float, min_area: float) -> tuple[list[dict], dict]:
    restante = _solo_poligonos(shapely.make_valid(geom)) or shapely.Polygon()
    if grid:
        restante = shapely.set_precision(restante, grid)
    filas: list[dict] = []
    qa = {"cut": cut, "comuna": nombre, "region": region, "traslapes_resueltos": 0,
          "instrumentos": set(), "revisar": 0}

    # sindex.query devuelve índices en orden del árbol: se reordenan para respetar rango/área de 'fuentes'
    idx = np.sort(fuentes.sindex.query(geom, predicate="intersects"))
    cand = fuentes.iloc[idx]

    for clase, clave in ORDEN:
        if restante.is_empty:
            break
        sub = cand[cand["fuente"] == clave]
        if clave in COMUNALES:
            # Un PRC digitalizado con desborde no debe "normar" la comuna vecina
            sub = sub[(sub["cut_ipt"] == cut) | (sub["cut_ipt"].isna())]
        if sub.empty:
            continue
        geoms = shapely.intersection(np.asarray(sub.geometry.values, dtype=object), restante, grid_size=grid)
        geoms = np.array([_solo_poligonos(g) or shapely.Polygon() for g in geoms], dtype=object)
        geoms, n = _resolver_traslapes(geoms, grid)
        qa["traslapes_resueltos"] += n

        tomadas = []
        for g, (_, r) in zip(geoms, sub.iterrows()):
            g = _solo_poligonos(g)
            if g is None or g.area < min_area:
                continue
            fila = {c: r.get(c) for c in CAMPOS}
            fila.update({"cut": cut, "comuna": nombre, "region": region,
                         "clase": clase, "fuente": clave, "geometry": g})
            filas.append(fila)
            tomadas.append(g)
            qa["instrumentos"].add(f"{r['ipt_tipo']}:{r['ipt_nombre']}")
            qa["revisar"] += int(bool(r.get("revisar")))
        if tomadas:
            restante = shapely.difference(restante, shapely.union_all(tomadas, grid_size=grid), grid_size=grid)
            restante = _solo_poligonos(restante) or shapely.Polygon()

    if not restante.is_empty and restante.area >= min_area:
        filas.append({c: None for c in CAMPOS} | {
            "revisar": False, "riesgo": False, "cut": cut, "comuna": nombre, "region": region,
            "clase": "R2", "fuente": "SIN_IPT", "geometry": restante})

    qa["instrumentos"] = "; ".join(sorted(qa["instrumentos"]))
    return filas, qa


def instrumentos_sin_comuna(fuentes: gpd.GeoDataFrame, comunas: gpd.GeoDataFrame, f_nom: str) -> pd.DataFrame:
    """QA: features de instrumentos comunales (PRC/seccional/LU) sin cut_ipt que tocan las comunas procesadas.
    Sin CUT no se filtran por comuna y pueden normar la comuna vecina."""
    cols = ["servicio", "capa", "ipt_tipo", "ipt_nombre", "features", "comunas_tocadas", "ejemplo_attrs_raw"]
    if fuentes.empty or "cut_ipt" not in fuentes:
        return pd.DataFrame(columns=cols)
    s = fuentes[fuentes["ipt_tipo"].isin(COMUNALES) & fuentes["cut_ipt"].isna()]
    if s.empty:
        return pd.DataFrame(columns=cols)
    j = gpd.sjoin(s, comunas[[f_nom, "geometry"]].to_crs(s.crs), predicate="intersects")
    if j.empty:
        return pd.DataFrame(columns=cols)
    return (j.groupby(["servicio", "capa", "ipt_tipo", "ipt_nombre"], dropna=False)
             .agg(features=("attrs_raw", lambda x: x.index.nunique()),
                  comunas_tocadas=(f_nom, lambda x: "; ".join(sorted(set(x)))),
                  ejemplo_attrs_raw=("attrs_raw", "first"))
             .reset_index()[cols])


def clasificar(comunas: gpd.GeoDataFrame, fuentes: gpd.GeoDataFrame, cfg: dict,
               f_cut: str, f_nom: str, f_reg: str) -> tuple[gpd.GeoDataFrame, list[dict]]:
    crs_t = cfg["crs"]["trabajo"]
    grid = float(cfg["build"]["grid_m"]) or None
    min_area = float(cfg["build"]["min_area_m2"])
    comunas = comunas.to_crs(crs_t)
    fuentes = fuentes.to_crs(crs_t).reset_index(drop=True)
    # Traslapes internos de una fuente: primero 'rango' (PRI: subclase explícita > revisar > pri_default),
    # luego lo más específico (menor área)
    rango = fuentes["rango"].fillna(0) if "rango" in fuentes else 0
    fuentes = (fuentes.assign(_r=rango, _a=fuentes.area).sort_values(["_r", "_a"])
               .drop(columns=["_r", "_a"]).reset_index(drop=True))

    todas, qas = [], []
    for _, c in comunas.iterrows():
        filas, qa = clasificar_comuna(str(c[f_cut]), c[f_nom], c[f_reg], c.geometry,
                                      fuentes, grid, min_area)
        todas.extend(filas)
        qas.append(qa)
        log.info("%s %-22s %3d piezas · %s", c[f_cut], c[f_nom], len(filas), qa["instrumentos"] or "sin IPT")

    capa = gpd.GeoDataFrame(todas, geometry="geometry", crs=crs_t)
    # QA de cobertura y área por clase (en equivalente de área)
    area = capa.to_crs(cfg["crs"]["area"]).area
    capa["area_m2"] = area.round(1)
    area_com = comunas.set_index(comunas[f_cut].astype(str)).to_crs(cfg["crs"]["area"]).area
    resumen = capa.groupby(["cut", "clase"])["area_m2"].sum().unstack(fill_value=0)
    # Traslape = suma de piezas − área de su unión (debe ser ~0)
    capa_a = capa.to_crs(cfg["crs"]["area"])
    union_com = {cut: shapely.union_all(g.values).area for cut, g in capa_a.groupby("cut").geometry}
    suma_com = capa.groupby("cut")["area_m2"].sum()
    for qa in qas:
        tot = float(area_com.get(qa["cut"], np.nan))
        fila = resumen.loc[qa["cut"]] if qa["cut"] in resumen.index else None
        for cl in ["U1", "U2", "U3", "E", "R1", "R2"]:
            v = float(fila[cl]) if fila is not None and cl in fila else 0.0
            qa[f"pct_{cl}"] = round(100 * v / tot, 3) if tot else None
        cubierto = float(fila.sum()) if fila is not None else 0.0
        qa["cobertura_pct"] = round(100 * cubierto / tot, 3) if tot else None
        qa["traslape_m2"] = round(max(0.0, float(suma_com.get(qa["cut"], 0.0)) - union_com.get(qa["cut"], 0.0)), 1)
        qa["sin_urbano"] = (qa["pct_U1"] or 0) + (qa["pct_U2"] or 0) + (qa["pct_U3"] or 0) == 0
    capa.insert(0, "id", [f"{c}-{i:05d}" for i, c in enumerate(capa["cut"])])
    return capa, qas
