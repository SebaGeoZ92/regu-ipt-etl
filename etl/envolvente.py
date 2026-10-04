"""Volumen existente y posible (fase 1) de un predio, según docs/VOLUMEN_PILOTO.md y docs/VOLUMENES.md.

Funciones puras (sin I/O), usadas por `app/` y testeables con geometrías sintéticas:

- `existente()`: huella de los edificios con más del 50 % dentro del predio, pisos y m² existentes.
- `posible()`: envolvente simplificada fase 1. `base` = predio reducido por max(antejardín, distanciamiento) de forma
  UNIFORME (simplificación declarada: no distingue frente y deslindes); `area_primer_piso = min(area(base),
  ocupacion_max × terreno)`; `pisos_max = altura_max_pisos` (o `floor(altura_max_m / altura_piso_ref_m)`);
  `m2_max = min(area_primer_piso × pisos_max, constructibilidad_max × terreno)`. **Sin rasantes** (fase 2).
- `V_max = ocupacion_max × terreno × altura_max_m` (cota superior simple) y `V_opt = m2_max × altura_piso_ref_m`.
  Si V_max < V_opt hay un error de datos y se marca.

La altura de piso de referencia NO es una norma: es un parámetro del modelo. Si la norma de la zona no la trae se usa el
valor de `config.yaml: volumen.altura_piso_ref_m` y queda declarado en `simplificaciones`. Las normas no se inventan:
si falta un valor necesario, el escenario se devuelve `sin_dato` con el motivo.
"""
from __future__ import annotations

import math
import re

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry.base import BaseGeometry

CRS_AREA = "ESRI:102033"
UMBRAL_DENTRO = 0.5
ALTURA_PISO_REF_DEF = 3.5
_RE_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def numeros(texto) -> list[float]:
    """Todos los números de un texto de norma ("5.0 frente a vías...; 3.0 frente a ..."), con coma o punto decimal."""
    return [float(x.replace(",", ".")) for x in _RE_NUM.findall(str(texto or ""))]


def _num(v) -> float | None:
    n = numeros(v)
    return n[0] if n else None


def estado_iov(iov: float | None) -> str:
    """Estado del índice de ocupación del volumen (docs/VOLUMENES.md): <0,8 holgura, 0,8-1,0 al límite, >1,0 excede."""
    if iov is None or (isinstance(iov, float) and math.isnan(iov)):
        return "sin_dato"
    return "holgura" if iov < 0.8 else ("al_limite" if iov <= 1.0 else "excede")


def existente(predio_4326: BaseGeometry, edificios: gpd.GeoDataFrame, altura_piso_ref_m: float = ALTURA_PISO_REF_DEF) -> dict:
    """Huella, pisos y m² existentes de los edificios (EPSG:4326, columnas `height`, `num_floors`, `area_m2`)
    que caen en más del 50 % dentro del predio. `edificios` ya viene acotado por bbox."""
    p = gpd.GeoSeries([predio_4326], crs=4326).to_crs(CRS_AREA).iloc[0]
    e = edificios.to_crs(CRS_AREA).reset_index(drop=True) if len(edificios) else edificios
    filas, feats = [], []
    if len(e):
        g = shapely.make_valid(e.geometry.values)
        a = shapely.area(g)
        frac = shapely.area(shapely.intersection(g, p)) / a.clip(min=1e-9)
        geo = edificios.to_crs(4326).reset_index(drop=True).geometry
        for i in e.index[frac > UMBRAL_DENTRO]:
            h, nf = e.get("height", pd.Series(dtype=float)).get(i), e.get("num_floors", pd.Series(dtype=float)).get(i)
            if pd.notna(nf):
                pisos, fuente = max(1, int(round(nf))), "overture_num_floors"
            elif pd.notna(h):
                pisos, fuente = max(1, int(round(h / altura_piso_ref_m))), "overture_height"
            else:
                pisos, fuente = 1, "estimado"
            id_ = e["id"].get(i) if "id" in e else i
            filas.append({"id": id_, "huella_m2": float(a[i]), "pisos": pisos, "fuente_pisos": fuente})
            # geometría para dibujar el existente en sólido: altura medida si existe, si no pisos × piso de referencia
            feats.append({"type": "Feature", "geometry": shapely.geometry.mapping(geo[i]), "properties": {
                "id": id_, "pisos": pisos, "fuente_pisos": fuente, "huella_m2": round(float(a[i]), 1),
                "altura_m": round(float(h), 1) if pd.notna(h) else round(pisos * altura_piso_ref_m, 1)}})
    huella = sum(f["huella_m2"] for f in filas)
    m2 = sum(f["huella_m2"] * f["pisos"] for f in filas)
    fuentes = {f["fuente_pisos"] for f in filas}
    estimado = "estimado" in fuentes
    return {
        # sin edificios en Overture el existente es 0 m² (sitio eriazo o dato faltante: el aviso lo dice la API)
        "n_edificios": len(filas), "huella_m2": round(huella, 1), "m2_existente": round(m2, 1),
        "pisos_est": round(m2 / huella, 2) if huella else None,
        "fuente_huella": "overture",
        "fuente_pisos": (next(iter(fuentes)) if len(fuentes) == 1 else "mixto") if filas else None,
        "confianza_pisos": ("baja" if estimado else "media") if filas else None,
        "cota_inferior": estimado,
        "sup_terreno_m2": round(float(p.area), 1),
        "edificios": feats,                 # FeatureCollection.features de los que cuentan (con altura_m), para dibujarlos
    }


def posible(predio_4326: BaseGeometry, norma: dict, altura_piso_ref_m: float | None = None) -> dict:
    """Envolvente fase 1 para UNA fila de norma (una por sistema de agrupamiento). Devuelve el escenario con su
    geometría (EPSG:4326), m², volúmenes, simplificaciones y el estado de la norma; `sin_dato` si falta un valor."""
    h_norma = _num(norma.get("altura_piso_ref_m"))
    h_ref = h_norma or altura_piso_ref_m or ALTURA_PISO_REF_DEF
    simp = ["Fase 1: sin rasantes (fase 2, con geometría 3D)."]
    if not h_norma:
        simp.append(f"Altura de piso de referencia {h_ref} m: parámetro del modelo, no es norma; confirmar con el arquitecto.")
    esc = {"agrupamiento": norma.get("agrupamiento") or None, "normas_estado": norma.get("estado"),
           "articulo_fuente": norma.get("articulo_fuente") or None, "fuente_por_valor": norma.get("fuente_por_valor") or None,
           "altura_piso_ref_m": h_ref, "estado": "ok", "motivo": None, "simplificaciones": simp, "envolvente": None}
    occ, cons = _num(norma.get("ocupacion_max")), _num(norma.get("constructibilidad_max"))
    alt_m, alt_p = _num(norma.get("altura_max_m")), _num(norma.get("altura_max_pisos"))
    faltan = [n for n, v in (("ocupacion_max", occ), ("constructibilidad_max", cons)) if v is None]
    if alt_m is None and alt_p is None:
        faltan.append("altura_max_m o altura_max_pisos")
    if faltan:
        return {**esc, "estado": "sin_dato", "motivo": "La norma no trae: " + ", ".join(faltan) + " (no se completan)."}

    p = gpd.GeoSeries([predio_4326], crs=4326).to_crs(CRS_AREA).iloc[0]
    terreno = float(p.area)
    ante = numeros(norma.get("antejardin_m"))
    dist = _num(norma.get("distanciamiento_m"))
    retr = max(min(ante) if ante else 0.0, dist or 0.0)
    if len(ante) > 1:
        simp.append(f"Antejardín según clase de vía ({', '.join(str(a) for a in ante)} m): se usa el menor ({min(ante)} m), "
                    "porque el frente no se identifica.")
    if retr:
        simp.append(f"Retranqueo uniforme de {retr} m en todos los deslindes (el antejardín rige solo al frente y el "
                    "distanciamiento solo con ciertos vecinos).")
    base = p.buffer(-retr, join_style="mitre") if retr else p
    area_base = float(base.area) if not base.is_empty else 0.0
    primer_piso = min(area_base, occ * terreno)
    pisos_max = int(alt_p) if alt_p is not None else int(math.floor(alt_m / h_ref + 1e-9))
    altura_env = pisos_max * h_ref
    m2_max = min(primer_piso * pisos_max, cons * terreno)
    limita = min((("ocupación × altura", primer_piso * pisos_max), ("constructibilidad", cons * terreno)), key=lambda x: x[1])[0]
    v_max = occ * terreno * (alt_m if alt_m is not None else altura_env)
    v_opt = m2_max * h_ref
    if v_max + 1e-6 < v_opt:
        simp.append("ERROR DE DATOS: V_max < V_opt.")
        esc["estado"] = "error_datos"
    if area_base <= 0:
        simp.append("El predio es demasiado angosto para el retranqueo uniforme de la fase 1: no queda base edificable. "
                    "La simplificación no aplica a este predio (la fase 2 distingue frente y deslindes).")
        esc["estado"] = "sin_base"
        esc["motivo"] = "Con retranqueo uniforme no queda base edificable; la fase 1 no es aplicable a un predio tan angosto."
    geom = gpd.GeoSeries([base], crs=CRS_AREA).to_crs(4326).iloc[0] if area_base > 0 else None
    esc.update(sup_terreno_m2=round(terreno, 1), ocupacion_max=occ, constructibilidad_max=cons, altura_max_m=alt_m,
               retranqueo_m=retr, area_base_m2=round(area_base, 1), area_primer_piso_m2=round(primer_piso, 1), pisos_max=pisos_max,
               altura_envolvente_m=round(altura_env, 1), m2_max=round(m2_max, 1), limita=limita,
               v_max_m3=round(v_max, 1), v_opt_m3=round(v_opt, 1),
               eficiencia=round(v_opt / v_max, 3) if v_max else None,
               envolvente=shapely.geometry.mapping(geom) if geom is not None else None)
    return esc


def comparar(existente_: dict, esc: dict) -> dict:
    """m² remanente e IOV (= m² existente / m² máximo, equivalente a V_existente / V_opt con el mismo piso de referencia)."""
    if esc.get("estado") not in ("ok",) or existente_.get("m2_existente") is None or not esc.get("m2_max"):
        return {"iov": None, "remanente_m2": None, "estado_iov": "sin_dato"}
    iov = existente_["m2_existente"] / esc["m2_max"]
    return {"iov": round(iov, 3), "remanente_m2": round(esc["m2_max"] - existente_["m2_existente"], 1), "estado_iov": estado_iov(iov)}
