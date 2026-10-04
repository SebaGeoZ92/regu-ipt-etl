"""Lectura (con caché) de lo que la aplicación muestra además de `ficha()`: comunas, ocupación por zona y normas."""
from __future__ import annotations

import math
from pathlib import Path

import geopandas as gpd
import pandas as pd

from etl.normalize import norm_txt

from .ajustes import MARCA_BORRADOR

_CACHE: dict = {}


def _con_mtime(p: Path):
    return (str(p), p.stat().st_mtime if p.exists() else None)


def comunas(a) -> list[dict]:
    """Lista para el buscador: [{cut, nombre, region, bbox: [minx, miny, maxx, maxy] en EPSG:4326}]."""
    if a.comunas is None or not Path(a.comunas).exists():
        return []
    clave = ("comunas",) + _con_mtime(Path(a.comunas))
    if clave not in _CACHE:
        g = gpd.read_file(a.comunas)
        if g.crs is not None:
            g = g.to_crs(4326)
        b = g.geometry.bounds
        lista = []
        for i, r in g.iterrows():
            cut = str(int(r[a.f_cut])).zfill(5) if str(r[a.f_cut]).strip().isdigit() else str(r[a.f_cut])
            lista.append({"cut": cut, "nombre": str(r[a.f_nombre]), "region": str(r[a.f_region]),
                          "bbox": [round(float(v), 6) for v in (b.minx[i], b.miny[i], b.maxx[i], b.maxy[i])]})
        _CACHE[clave] = sorted(lista, key=lambda c: norm_txt(c["nombre"]))
    return _CACHE[clave]


def _ultimo(out: Path | None, patrones: tuple[str, ...]) -> Path | None:
    if out is None:
        return None
    for pat in patrones:
        c = sorted(Path(out).glob(pat))
        if c:
            return c[-1]
    return None


def ocupacion(a) -> dict:
    """{(norm(ipt), norm(zona)): {ha, m2_huella, coef_ocupacion, n_edificios}} desde el último ocupacion_zonas_*.csv."""
    p = _ultimo(a.dir_out, ("ocupacion_zonas_nacional_*.csv", "ocupacion_zonas_*.csv"))
    if p is None:
        return {}
    clave = ("ocupacion",) + _con_mtime(p)
    if clave not in _CACHE:
        d = pd.read_csv(p, encoding="utf-8-sig", dtype={"zona": str, "ipt": str}, keep_default_na=False)
        _CACHE[clave] = {(norm_txt(r.ipt), norm_txt(r.zona)): {
            "ha": float(r.ha), "m2_huella": int(r.m2_huella), "coef_ocupacion": float(r.coef_ocupacion),
            "n_edificios": int(r.n_edificios), "fuente": "Overture Maps (ODbL) sobre la partición IPT",
            "nota": "Huella / área bruta de la zona: no es el coeficiente de ocupación de suelo de la OGUC."}
            for r in d.itertuples()}
    return _CACHE[clave]


def normas(a) -> dict:
    """{(norm(ipt), norm(zona)): [fila, ...]} desde normas/normas_zona.csv, solo con las columnas no vacías.
    Una zona puede tener varias filas (una por sistema de agrupamiento)."""
    p = Path(a.normas)
    if not p.exists():
        return {}
    clave = ("normas",) + _con_mtime(p)
    if clave not in _CACHE:
        d = pd.read_csv(p, encoding="utf-8-sig", dtype=str, keep_default_na=False)
        res: dict = {}
        for r in d.to_dict("records"):
            fila = {k: v for k, v in r.items() if v != "" and k not in ("ipt_nombre", "zona")}
            fila["marca"] = MARCA_BORRADOR if r.get("estado") != "VALIDADO" else None
            res.setdefault((norm_txt(r["ipt_nombre"]), norm_txt(r["zona"])), []).append(fila)
        _CACHE[clave] = res
    return _CACHE[clave]


def enriquecer(ficha: dict, a) -> dict:
    """Agrega a cada pieza de la partición la ocupación existente de su zona y las normas con su artículo de origen.
    Si alguna norma no está VALIDADA, el resultado lleva `marca` (solo uso interno, nunca se publica)."""
    oc, nm = ocupacion(a), normas(a)
    hay_borrador = False
    for p in ficha.get("particion", []):
        k = (norm_txt(p.get("ipt") or ""), norm_txt(p.get("zona") or ""))
        p["ocupacion"] = oc.get(k)
        p["normas"] = nm.get(k, [])
        hay_borrador = hay_borrador or any(n.get("marca") for n in p["normas"])
    ficha["marca"] = MARCA_BORRADOR if hay_borrador else None
    return _sin_nan(ficha)


def _sin_nan(v):
    """NaN/inf no son JSON válido: se reemplazan por None."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, dict):
        return {k: _sin_nan(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_sin_nan(x) for x in v]
    return v
