"""Catálogo de mapas temáticos de referencia (docs/MAPAS_TEMATICOS.md): base del SIG propio.

Cada tema es un contrato `temas/<id>.yaml` validado contra `temas/_esquema.json` y contra reglas por estado:
    propuesta → mapeada (Paso 0 hecho y dato descargado) → activa (generado y servible).
- `cargar_catalogo()` y `validar()`: lectura y control del contrato.
- `@generador("nombre")`: registro de las funciones que producen la salida de un tema (ráster o vectorial).
- `estado()`: qué está generado, tamaño y fecha. `publico()`: lo que la aplicación puede mostrar de un tema.
- `publicable()`: un tema solo se publica con la licencia verificada y uso comercial permitido; local no exige nada.
Salidas en `<paths.out>/tiles/temas/<id>.pmtiles`; fuentes descargadas en `<paths.raw>/temas/<id>/`.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd
import yaml

RAIZ = Path(__file__).resolve().parents[1]
DIR_TEMAS = RAIZ / "temas"
ESTADOS = ["propuesta", "mapeada", "activa"]
GENERADORES: dict[str, Callable] = {}


def generador(nombre: str):
    """Registra `fn(contrato, dir_raw, destino, cfg) -> Path` como el generador `nombre`."""
    def deco(fn):
        GENERADORES[nombre] = fn
        return fn
    return deco


def ruta_salida(dir_out: Path, id_: str) -> Path:
    return Path(dir_out) / "tiles" / "temas" / f"{id_}.pmtiles"


def ruta_fuente(dir_raw: Path, id_: str) -> Path:
    return Path(dir_raw) / "temas" / id_


# ── validación ───────────────────────────────────────────────────────────────────────────────────────────────────────
def validar(c: dict, archivo: Path | None = None, esquema: dict | None = None) -> list[str]:
    """Errores del contrato: esquema y reglas por estado. Lista vacía = válido."""
    import jsonschema
    if esquema is None:
        esquema = json.loads((DIR_TEMAS / "_esquema.json").read_text(encoding="utf-8"))
    err = [f"{'/'.join(str(p) for p in e.absolute_path) or '/'}: {e.message}"
           for e in jsonschema.Draft7Validator(esquema).iter_errors(c)]
    if err:
        return err
    if archivo is not None and archivo.stem != c["id"]:
        err.append(f"el id «{c['id']}» no coincide con el nombre del archivo «{archivo.name}»")
    z = c["zoom"]
    if z[0] > z[1]:
        err.append(f"zoom {z}: el mínimo supera al máximo")
    est, f, e = c["estado"], c["fuente"], c["estilo"]
    if c["tipo"] == "raster":
        r = e.get("rampa")
        if not r:
            err.append("un tema ráster necesita estilo.rampa (pares [valor, color])")
        else:
            vals = [p[0] for p in r]
            if any(not isinstance(v, (int, float)) for v in vals) or vals != sorted(vals) or len(set(vals)) != len(vals):
                err.append("estilo.rampa: los valores deben ser números crecientes y distintos")
            if any(not isinstance(p[1], str) or len(p[1]) != 7 or not p[1].startswith("#") for p in r):
                err.append("estilo.rampa: los colores deben ser #rrggbb")
    elif est != "propuesta" and not (e.get("capa_origen") and e.get("paint") and e.get("tipo_capa")):
        err.append("un tema vectorial necesita estilo.capa_origen, estilo.tipo_capa y estilo.paint")
    if est != "propuesta":
        if not f.get("url") or not f.get("fecha_dato"):
            err.append(f"estado {est}: la fuente necesita url y fecha_dato")
        if not (f.get("acceso") or {}).get("verificado"):
            err.append(f"estado {est}: falta fuente.acceso.verificado: true (Paso 0)")
        g = (c.get("generacion") or {}).get("generador")
        if not g:
            err.append(f"estado {est}: falta generacion.generador")
    if f.get("licencia_verificada") and f.get("uso_comercial", "por_verificar") == "por_verificar":
        err.append("licencia_verificada: true exige indicar uso_comercial (si | no)")
    return err


def cargar_catalogo(dir_temas: Path = DIR_TEMAS, validar_al_cargar: bool = True) -> dict[str, dict]:
    """{id: contrato}. Con `validar_al_cargar`, un contrato inválido detiene la carga con todos sus errores."""
    cat, errores = {}, []
    for p in sorted(Path(dir_temas).glob("*.yaml")):
        c = yaml.safe_load(p.read_text(encoding="utf-8"))
        if validar_al_cargar:
            errores += [f"{p.name}: {e}" for e in validar(c, p)] if isinstance(c, dict) else [f"{p.name}: no es un contrato"]
        cat[(c or {}).get("id", p.stem)] = c
    if errores:
        raise ValueError("Contratos de temas inválidos:\n  " + "\n  ".join(errores))
    return cat


# ── estado y vista pública ───────────────────────────────────────────────────────────────────────────────────────────
def publicable(c: dict) -> bool:
    """Solo se publica con la licencia verificada y el uso comercial permitido (local no exige nada)."""
    f = c["fuente"]
    return bool(f.get("licencia_verificada")) and f.get("uso_comercial") == "si"


def estado(catalogo: dict[str, dict], dir_out: Path) -> pd.DataFrame:
    filas = []
    for id_, c in catalogo.items():
        p = ruta_salida(dir_out, id_)
        filas.append({"id": id_, "categoria": c["categoria"], "tipo": c["tipo"], "estado": c["estado"], "generado": p.exists(),
                      "mb": round(p.stat().st_size / 1e6, 1) if p.exists() else None,
                      "fecha": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d") if p.exists() else None,
                      "licencia_verificada": c["fuente"]["licencia_verificada"], "publicable": publicable(c)})
    return pd.DataFrame(filas)


def publico(c: dict, dir_out: Path) -> dict:
    """Lo que la aplicación muestra de un tema: estilo, procedencia y si su salida existe. Nunca rutas del disco."""
    f = c["fuente"]
    return {"id": c["id"], "nombre": c["nombre"], "categoria": c["categoria"], "tipo": c["tipo"], "estado": c["estado"],
            "zoom": c["zoom"], "estilo": c["estilo"], "nota": c.get("nota", ""),
            "fuente": {k: f.get(k) for k in ("nombre", "atribucion", "licencia", "licencia_verificada", "uso_comercial", "resolucion", "fecha_dato")},
            "disponible": ruta_salida(dir_out, c["id"]).exists(), "publicable": publicable(c)}


def generar(id_: str, catalogo: dict[str, dict], dir_raw: Path, dir_out: Path, cfg: dict) -> Path:
    """Genera la salida de un tema con su generador registrado. Un tema `propuesta` no se genera."""
    if id_ not in catalogo:
        raise KeyError(f"Tema desconocido: {id_}")
    c = catalogo[id_]
    if c["estado"] == "propuesta":
        raise ValueError(f"«{id_}» está en estado propuesta: falta el Paso 0 (acceso, licencia y fecha). No se genera.")
    g = c["generacion"]["generador"]
    if g not in GENERADORES:
        raise ValueError(f"«{id_}»: el generador «{g}» no está registrado (¿falta importar su módulo?)")
    destino = ruta_salida(dir_out, id_)
    destino.parent.mkdir(parents=True, exist_ok=True)
    return GENERADORES[g](c, ruta_fuente(dir_raw, id_), destino, cfg)
