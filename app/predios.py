"""Consulta de predios del SII (respaldo personal) para /api/predio: GeoParquet por comuna leídos con DuckDB spatial.

Búsquedas: por rol dentro de una comuna (CUT, código SII o nombre) o por punto. No devuelve avalúo ni propietarios (el conversor no los
guarda). Si el rol tiene varios polígonos se unen; entre varios polígonos de un punto se prefiere el que tiene datos del SII y es exacto.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import shapely
from shapely.geometry import mapping, shape

from etl.normalize import norm_txt
from etl.predios import normalizar_rol

from . import datos
from .edificios import _conexion

CAMPOS = ["id", "cut", "cod_sii", "manzana", "predio", "rol", "comuna", "direccion", "destino_cod", "destino", "ubicacion", "sup_terreno_m2",
          "sup_construida_m2", "pisos_max", "anio_construccion", "area_poligono_m2", "metodo", "exacto", "datos_sii"]
FUENTE = {"nombre": "SII, catastro de bienes raíces, a través de catastral.cl", "tipo": "respaldo personal anterior a la API de catastral.cl",
          "uso": "personal; no se publica ni se versiona"}
_MANIFIESTO: dict = {}


class PrediosNoDisponibles(Exception):
    """No hay parquet de predios (falta `python run.py predios convertir`)."""


def _carpeta(a) -> Path:
    p = Path(a.dir_predios) if a.dir_predios else None
    if p is None or not p.exists() or not any(p.glob("*.parquet")):
        raise PrediosNoDisponibles("No hay predios convertidos: corre `python run.py predios convertir --origen <carpeta con los GeoPackage>`")
    return p


def manifiesto(a) -> pd.DataFrame:
    m = _carpeta(a) / "manifiesto_predios.csv"
    clave = (str(m), m.stat().st_mtime) if m.exists() else None
    if clave is None:
        return pd.DataFrame(columns=["cod_sii", "cut", "comuna_bcn"])
    if _MANIFIESTO.get("clave") != clave:
        _MANIFIESTO.update(clave=clave, df=pd.read_csv(m, encoding="utf-8-sig", dtype=str, keep_default_na=False))
    return _MANIFIESTO["df"]


def resolver_comuna(a, cut: str | None = None, cod_sii: str | None = None, comuna: str | None = None) -> str | None:
    """CUT de la comuna pedida por CUT, código de comuna del SII o nombre. None si no se reconoce. (Un número de 4 dígitos es
    ambiguo —9101 es Angol en el SII y Temuco en el CUT sin cero—, por eso son parámetros distintos.)"""
    carpeta = _carpeta(a)
    if cut:
        return cut if (carpeta / f"{cut}.parquet").exists() else None
    m = manifiesto(a)
    if cod_sii:
        f = m[m["cod_sii"].str.split(",").map(lambda xs: str(int(cod_sii)) in [x.strip() for x in xs])] if len(m) else m
        return f["cut"].iloc[0] if len(f) and f["cut"].iloc[0] else None
    if comuna:
        n = norm_txt(comuna)
        c = [x["cut"] for x in datos.comunas(a) if norm_txt(x["nombre"]) == n and (carpeta / f"{x['cut']}.parquet").exists()]
        return c[0] if len(c) == 1 else None
    return None


def _filas(a, cut: str, where: str, params: list) -> list[dict]:
    sql = f"select {', '.join(CAMPOS)}, ST_AsGeoJSON(geometry) as geom from read_parquet(?) where {where}"
    cur = _conexion().execute(sql, [str(_carpeta(a) / f"{cut}.parquet"), *params])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _json(v):
    if v is None or (isinstance(v, float) and v != v):
        return None
    return v.item() if hasattr(v, "item") else v


def _respuesta(filas: list[dict]) -> dict:
    """Une las filas de un mismo predio en una respuesta con calidad y avisos (sin avalúo ni propietarios)."""
    filas = sorted(filas, key=lambda f: (not f["datos_sii"], not f["exacto"], f["area_poligono_m2"] or 0))
    f = filas[0]
    geom = shapely.union_all([shape(json.loads(x["geom"])) for x in filas]) if len(filas) > 1 else shape(json.loads(f["geom"]))
    avisos = []
    if not f["datos_sii"]:
        avisos.append("El respaldo no trae datos del SII para este polígono: solo se conoce su forma.")
    elif not f["exacto"]:
        avisos.append("El respaldo asignó los datos de este rol al polígono por cercanía a la manzana (no por contener el punto del SII): "
                      "el polígono puede no ser el del predio.")
    if len(filas) > 1:
        avisos.append(f"El rol tiene {len(filas)} polígonos en el respaldo; se unieron.")
    avisos.append("Respaldo de catastral.cl anterior a su API: puede estar desactualizado frente al SII.")
    predio = {k: _json(f[k]) for k in CAMPOS if k not in ("exacto", "datos_sii", "metodo")}
    return {"predio": predio, "geometria": mapping(geom), "n_poligonos": len(filas),
            "calidad": {"datos_sii": bool(f["datos_sii"]), "metodo": f["metodo"], "geometria": "sin_datos" if not f["datos_sii"] else
                        ("exacta" if f["exacto"] else "aproximada"), "avisos": avisos},
            "fuente": FUENTE}


def por_rol(a, cut: str, rol: str) -> dict | None:
    n = normalizar_rol(rol)
    if n is None:
        raise ValueError(f"Rol inválido: «{rol}». Formato manzana-predio, p. ej. 1733-22 o 01733-00022")
    filas = _filas(a, cut, "rol = ?", [n])
    return _respuesta(filas) if filas else None


MARGEN_BBOX = 0.1          # grados: la línea comunal de la BCN está generalizada y algunos predios caen un poco fuera de su caja
_BBOX_ARCHIVO: dict = {}


def bbox_archivo(p: Path) -> tuple[float, float, float, float]:
    """Caja real (lon/lat) de los predios de un parquet, desde sus columnas `bbox`; se calcula una vez por versión del archivo."""
    clave = (str(p), p.stat().st_mtime)
    if clave not in _BBOX_ARCHIVO:
        r = _conexion().execute("select min(bbox.xmin), min(bbox.ymin), max(bbox.xmax), max(bbox.ymax) from read_parquet(?)", [str(p)]).fetchone()
        _BBOX_ARCHIVO[clave] = tuple(float(x) for x in r)
    return _BBOX_ARCHIVO[clave]


def en_punto(a, lon: float, lat: float) -> dict | None:
    """Predio que contiene el punto. Prueba las comunas cercanas (caja BCN con margen) cuyo parquet realmente lo abarca."""
    carpeta = _carpeta(a)
    m = MARGEN_BBOX
    cercanas = [c["cut"] for c in datos.comunas(a) if c["bbox"][0] - m <= lon <= c["bbox"][2] + m and c["bbox"][1] - m <= lat <= c["bbox"][3] + m
                and (carpeta / f"{c['cut']}.parquet").exists()]
    candidatas = [c for c in cercanas if (b := bbox_archivo(carpeta / f"{c}.parquet"))[0] <= lon <= b[2] and b[1] <= lat <= b[3]]
    for cut in candidatas:
        filas = _filas(a, cut, "bbox.xmin <= ? AND bbox.xmax >= ? AND bbox.ymin <= ? AND bbox.ymax >= ? AND ST_Intersects(geometry, ST_Point(?, ?))",
                       [lon, lon, lat, lat, lon, lat])      # la caja primero: con el orden espacial del parquet salta casi todos los grupos
        if filas:
            filas = sorted(filas, key=lambda f: (not f["datos_sii"], not f["exacto"], f["area_poligono_m2"] or 0))
            mejor = filas[0]
            if mejor["id"]:      # el predio completo (puede tener varios polígonos con el mismo rol)
                return _respuesta(_filas(a, cut, "id = ?", [mejor["id"]]))
            return _respuesta([mejor])
    return None
