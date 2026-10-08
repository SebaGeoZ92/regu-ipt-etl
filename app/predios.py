"""Consulta de predios del SII (respaldo personal) para /api/predio: GeoParquet por comuna leídos con DuckDB spatial.

Búsquedas: por rol dentro de una comuna (CUT, código SII o nombre) o por punto. No devuelve avalúo ni propietarios (el conversor no los
guarda). Si el rol tiene varios polígonos se unen; entre varios polígonos de un punto se prefiere el que tiene datos del SII y es exacto.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date
from pathlib import Path

import pandas as pd
import shapely
from shapely.geometry import mapping, shape

from etl.normalize import norm_txt
from etl.predios import normalizar_rol

from . import datos
from .edificios import _conexion

CAMPOS = ["id", "cut", "cod_sii", "manzana", "predio", "rol", "comuna", "direccion", "destino_cod", "destino", "ubicacion", "sup_terreno_m2",
          "sup_construida_m2", "pisos_max", "anio_construccion", "periodo_sii", "area_poligono_m2", "metodo", "exacto", "datos_sii", "id_poligono",
          "n_unidades", "n_asignados", "lat_sii", "lon_sii"]
TIPO_COPROPIEDAD = "copropiedad / varias unidades"
CAMPOS_UNIDAD = ["rol", "direccion", "destino_cod", "destino", "ubicacion", "sup_terreno_m2", "sup_construida_m2", "pisos_max", "anio_construccion",
                 "periodo_sii", "metodo"]
MAX_UNIDADES = 2000        # tope de unidades detalladas en una respuesta (hay terrenos con casi 800)
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


def _indice_semestre(periodo: str | None) -> int | None:
    """«2026-1» → 4052 (año × 2 + semestre − 1): permite restar semestres."""
    m = re.fullmatch(r"(20\d{2})-([12])", periodo or "")
    return int(m.group(1)) * 2 + int(m.group(2)) - 1 if m else None


def _texto_periodo(periodo: str) -> str:
    return f"{'primer' if periodo.endswith('1') else 'segundo'} semestre de {periodo[:4]}"


def fecha_dato(periodo_sii: str | None, periodo_comuna: str | None, hoy: date | None = None, con_datos: bool = False) -> dict:
    """Fecha (semestre del avalúo del SII) del dato de un predio y cuánto se atrasa frente al semestre en curso.
    Con datos pero sin periodo legible (columna corrida en el respaldo) se usa el de la comuna y se marca `periodo_inferido`: en todas
    las comunas respaldadas el único periodo legible es el mismo. Sin datos propios del SII (polígono huérfano) se informa el semestre
    del respaldo de la comuna, que es solo una cota: un predio creado después (subdivisión, loteo nuevo) no aparece o figura sin datos."""
    hoy = hoy or date.today()
    actual = f"{hoy.year}-{1 if hoy.month <= 6 else 2}"
    inferido = bool(con_datos and not periodo_sii and periodo_comuna)
    if inferido:
        periodo_sii = periodo_comuna
    base = periodo_sii or periodo_comuna
    atraso = (_indice_semestre(actual) - _indice_semestre(base)) if _indice_semestre(base) is not None else None
    if not periodo_sii:
        aviso = ("Sin datos del SII para este polígono: no tiene fecha propia, y un predio creado después del respaldo no aparece o figura sin datos."
                 + (f" El respaldo de la comuna es del {_texto_periodo(periodo_comuna)}." if periodo_comuna else ""))
        estado = "sin_dato"
    elif atraso and atraso > 0:
        aviso = f"Dato del {_texto_periodo(periodo_sii)}: {atraso} semestre{'s' if atraso > 1 else ''} de diferencia con el semestre en curso ({_texto_periodo(actual)})."
        estado = "atrasado"
    else:
        aviso, estado = f"Dato del {_texto_periodo(periodo_sii)}, el semestre en curso.", "al_dia"
    if inferido:
        aviso += " El periodo del predio no se lee en el respaldo; se usa el de la comuna."
    return {"periodo_sii": periodo_sii, "periodo_inferido": inferido, "periodo_respaldo_comuna": periodo_comuna, "semestre_actual": actual,
            "atraso_semestres": atraso, "estado": estado, "aviso": aviso}


def _periodo_comuna(a, cut: str) -> str | None:
    m = manifiesto(a)
    if "periodo" not in m.columns or not len(m):
        return None
    f = m[(m["cut"] == cut) & ((m["estado"] == "ok") if "estado" in m.columns else True)]
    return (f["periodo"].iloc[0] or None) if len(f) else None


def _respuesta(filas: list[dict], periodo_comuna: str | None = None) -> dict:
    """Une las filas de un mismo predio en una respuesta con calidad y avisos (sin avalúo ni propietarios)."""
    filas = sorted(filas, key=lambda f: (not f["datos_sii"], not f["exacto"], f["area_poligono_m2"] or 0))
    f = filas[0]
    geom = shapely.union_all([shape(json.loads(x["geom"])) for x in filas]) if len(filas) > 1 else shape(json.loads(f["geom"]))
    avisos = []
    if not f["datos_sii"]:
        avisos.append("El respaldo no trae datos del SII para este polígono: solo se conoce su forma.")
    elif f["metodo"] == "relleno_respaldo1":
        avisos.append("Asignación aproximada: el rol no tiene polígono en el respaldo principal; el polígono es el aproximado del Respaldo 1.")
    elif not f["exacto"]:
        avisos.append("Asignación aproximada: el respaldo asignó los datos de este rol al polígono por cercanía o por coordenadas (no porque el "
                      "polígono contenga su punto del SII): el polígono puede no ser el del predio.")
    if f["datos_sii"] and (f["n_asignados"] or 0) > 1:
        otros = (f["n_asignados"] or 0) - 1
        avisos.append(f"Otros {otros} roles quedaron asignados a este mismo polígono" + (" por cercanía o coordenadas; no se agrupan como "
                      "copropiedad (no hay certeza de que compartan el terreno)." if not f["exacto"] or (f["n_unidades"] or 0) <= 1 else "."))
    if len(filas) > 1:
        avisos.append(f"El rol tiene {len(filas)} polígonos en el respaldo; se unieron.")
    fecha = fecha_dato(_json(f["periodo_sii"]) if f["datos_sii"] else None, periodo_comuna, con_datos=bool(f["datos_sii"]))
    if fecha["estado"] != "sin_dato":      # para los huérfanos el aviso sobre la falta de datos ya está arriba
        avisos.append(fecha["aviso"])
    avisos.append("Respaldo de catastral.cl anterior a su API: puede estar desactualizado frente al SII.")
    predio = {k: _json(f[k]) for k in CAMPOS if k not in ("exacto", "datos_sii", "metodo", "id_poligono", "n_unidades", "n_asignados", "lat_sii", "lon_sii")}
    roles = sorted({x["rol"] for x in filas if x["rol"] and x["datos_sii"]})
    return {"predio": predio, "geometria": mapping(geom), "n_poligonos": len(filas), "tipo_predio": "predio", "copropiedad": False,
            "n_unidades": len(roles), "roles": roles, "fecha_dato": fecha,
            "calidad": {"datos_sii": bool(f["datos_sii"]), "metodo": f["metodo"], "geometria": "sin_datos" if not f["datos_sii"] else
                        ("exacta" if f["exacto"] else "aproximada"), "avisos": avisos},
            "fuente": FUENTE}


def _respuesta_terreno(filas: list[dict], periodo_comuna: str | None = None, rol_consultado: str | None = None) -> dict:
    """Terreno con varios roles sobre el mismo polígono (copropiedad, condominios, edificios): la lista de roles y el número de unidades,
    no un solo rol. Solo con **confianza alta**: `filas` son los roles cuyo punto del SII está dentro del polígono (`exacto`); los
    asignados por coordenadas o cercanía no se agrupan."""
    vistos, unidades = set(), []
    for x in sorted(filas, key=lambda x: (not x["exacto"], x["rol"] or "")):
        if x["rol"] and x["rol"] not in vistos:
            vistos.add(x["rol"])
            unidades.append(x)
    unidades.sort(key=lambda x: x["rol"])
    ref = unidades[0]
    for x in unidades:
        if x["exacto"]:
            ref = x
            break

    def comun(k):
        v = {_json(x[k]) for x in unidades}
        return v.pop() if len(v) == 1 else None
    predio = {k: None for k in CAMPOS if k not in ("exacto", "datos_sii", "metodo", "id_poligono", "n_unidades", "n_asignados", "lat_sii", "lon_sii")}
    predio.update(id=f"{ref['cut']}-T-{ref['id_poligono']}", cut=ref["cut"], cod_sii=_json(ref["cod_sii"]), comuna=_json(ref["comuna"]),
                  manzana=comun("manzana"), ubicacion=comun("ubicacion"), area_poligono_m2=_json(ref["area_poligono_m2"]))
    fecha = fecha_dato(_json(ref["periodo_sii"]), periodo_comuna, con_datos=True)
    avisos = [f"Copropiedad / varias unidades: {len(unidades)} roles comparten este polígono (el respaldo no distingue dónde está cada unidad "
              "dentro del terreno). La superficie de terreno de cada rol puede ser el terreno completo o su cuota: no se suman."]
    omitidas = max(0, int(_json(ref["n_asignados"]) or 0) - len(unidades))
    if omitidas:
        avisos.append(f"Otros {omitidas} roles quedaron asignados a este polígono por cercanía o coordenadas, sin certeza; no se incluyen.")
    if len(unidades) > MAX_UNIDADES:
        avisos.append(f"Se detallan las primeras {MAX_UNIDADES} unidades de {len(unidades)}; la lista de roles está completa.")
    avisos.append(fecha["aviso"])
    avisos.append("Respaldo de catastral.cl anterior a su API: puede estar desactualizado frente al SII.")
    r = {"predio": predio, "geometria": json.loads(ref["geom"]), "n_poligonos": 1, "tipo_predio": TIPO_COPROPIEDAD, "copropiedad": True,
         "n_unidades": len(unidades), "confianza_copropiedad": "alta", "asignaciones_aproximadas_omitidas": omitidas,
         "roles": [x["rol"] for x in unidades],
         "unidades": [{k: _json(x[k]) for k in CAMPOS_UNIDAD} for x in unidades[:MAX_UNIDADES]], "fecha_dato": fecha,
         "calidad": {"datos_sii": True, "metodo": ref["metodo"], "geometria": "exacta" if ref["exacto"] else "aproximada", "avisos": avisos},
         "fuente": FUENTE}
    if rol_consultado:
        r["rol_consultado"] = rol_consultado
    return r


def _con_unidades(a, cut: str, filas: list[dict], periodo_comuna: str | None, rol_consultado: str | None = None) -> dict:
    """`_respuesta` del predio, o `_respuesta_terreno` si otros roles comparten el polígono (n_unidades > 1)."""
    pols = {f["id_poligono"] for f in filas if f["datos_sii"] and f["exacto"] and f["id_poligono"] and (f["n_unidades"] or 0) > 1}
    if pols:
        ph = ",".join("?" * len(pols))
        otras = [x for x in _filas(a, cut, f"id_poligono IN ({ph}) AND datos_sii AND exacto AND rol IS NOT NULL", sorted(pols))]
        if len({x["rol"] for x in otras}) > 1:
            return _respuesta_terreno(otras, periodo_comuna, rol_consultado)
    return _respuesta(filas, periodo_comuna)


def _distancia_m(f: dict, lon: float, lat: float) -> float:
    """Distancia aproximada (m) entre el clic y el punto que el SII publica para el rol; infinita si el rol no trae punto."""
    x, y = _json(f["lon_sii"]), _json(f["lat_sii"])
    if x is None or y is None:
        return float("inf")
    return float(((x - lon) * 111320 * math.cos(math.radians(lat))) ** 2 + ((y - lat) * 110574) ** 2) ** 0.5


def por_rol(a, cut: str, rol: str) -> dict | None:
    n = normalizar_rol(rol)
    if n is None:
        raise ValueError(f"Rol inválido: «{rol}». Formato manzana-predio, p. ej. 1733-22 o 01733-00022")
    filas = _filas(a, cut, "rol = ?", [n])
    if not filas:
        return _solo_punto(a, cut, n)
    return _con_unidades(a, cut, filas, _periodo_comuna(a, cut), n)


def _solo_punto(a, cut: str, rol: str) -> dict | None:
    """Rol con datos del SII pero sin polígono a 50 m de su punto (`sin_poligono/<cut>_roles.parquet`): se devuelve el punto del SII."""
    p = _carpeta(a) / "sin_poligono" / f"{cut}_roles.parquet"
    if not p.exists():
        return None
    cur = _conexion().execute("select * from read_parquet(?) where rol = ?", [str(p), rol])
    cols = [d[0] for d in cur.description]
    filas = [dict(zip(cols, r)) for r in cur.fetchall()]
    if not filas:
        return None
    f = filas[0]
    predio = {k: _json(f.get(k)) for k in CAMPOS if k not in ("exacto", "datos_sii", "metodo", "id_poligono", "n_unidades", "n_asignados", "lat_sii", "lon_sii")}
    fecha = fecha_dato(_json(f.get("periodo_sii")), _periodo_comuna(a, cut), con_datos=True)
    punto = ({"type": "Point", "coordinates": [_json(f["lon_sii"]), _json(f["lat_sii"])]}
             if _json(f.get("lon_sii")) is not None and _json(f.get("lat_sii")) is not None else None)
    avisos = ["Sin polígono: el rol tiene datos del SII, pero ningún polígono del respaldo contiene su punto ni está a menos de 50 m. "
              "Se conoce solo el punto que publica el SII." if punto else "Sin polígono ni punto en el respaldo.", fecha["aviso"],
              "Respaldo de catastral.cl anterior a su API: puede estar desactualizado frente al SII."]
    return {"predio": predio, "geometria": punto, "n_poligonos": 0, "tipo_predio": "predio", "copropiedad": False, "n_unidades": 1, "roles": [rol],
            "fecha_dato": fecha, "calidad": {"datos_sii": True, "metodo": "sin_poligono", "geometria": "solo_punto", "avisos": avisos}, "fuente": FUENTE}


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
            con_datos = [f for f in filas if f["datos_sii"] and f["rol"]]
            exactos = [f for f in con_datos if f["exacto"]]
            copro = [f for f in exactos if (f["n_unidades"] or 0) > 1]
            if copro:      # varios roles con certeza sobre el mismo polígono: el terreno con todas sus unidades
                return _con_unidades(a, cut, [copro[0]], _periodo_comuna(a, cut))
            if con_datos:      # sin certeza de copropiedad: solo el rol cuyo punto del SII está más cerca del clic
                mejor = min(exactos or con_datos, key=lambda f: (_distancia_m(f, lon, lat), f["area_poligono_m2"] or 0))
            else:
                mejor = sorted(filas, key=lambda f: f["area_poligono_m2"] or 0)[0]
            if mejor["id"]:      # el predio completo (puede tener varios polígonos con el mismo rol)
                return _respuesta(_filas(a, cut, "id = ?", [mejor["id"]]), _periodo_comuna(a, cut))
            return _respuesta([mejor], _periodo_comuna(a, cut))
    return None
