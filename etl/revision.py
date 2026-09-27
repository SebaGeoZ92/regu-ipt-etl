"""Ida y vuelta con el arquitecto: zonas con revisar=True → CSV → zone_overrides en config.yaml."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import geopandas as gpd
import pandas as pd
import yaml

from .normalize import norm_txt

log = logging.getLogger(__name__)

COLUMNAS = ["ipt_nombre", "zona", "zona_desc", "comunas", "ha", "subclase_actual", "decision"]
DECISIONES = {"E", "U", "R", "AFECTACION"}


def _clave(nombre, zona) -> str:
    return f"{norm_txt(nombre)}|{norm_txt(zona)}"


def generar_revision(capa: gpd.GeoDataFrame, path: Path) -> pd.DataFrame:
    """Una fila por (ipt_nombre, zona) con revisar=True. Si el CSV ya existe, conserva las decisiones llenas."""
    r = capa[capa["revisar"].fillna(False).astype(bool)]
    if r.empty:
        df = pd.DataFrame(columns=COLUMNAS)
    else:
        df = (r.groupby(["ipt_nombre", "zona"], dropna=False)
               .agg(zona_desc=("zona_desc", "first"),
                    comunas=("comuna", lambda x: "; ".join(sorted(set(x)))),
                    ha=("area_m2", lambda x: round(x.sum() / 1e4, 2)),
                    subclase_actual=("fuente", lambda x: "; ".join(sorted({f.removeprefix("PRI_") for f in x}))))
               .reset_index())
        df["decision"] = ""
    if path.exists():
        prev = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
        previas = {_clave(a, b): d for a, b, d in zip(prev["ipt_nombre"], prev["zona"], prev["decision"]) if d.strip()}
        df["decision"] = [previas.get(_clave(a, b), "") for a, b in zip(df["ipt_nombre"], df["zona"])]
    df = df[COLUMNAS].sort_values(["ipt_nombre", "ha"], ascending=[True, False])
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return df


def leer_decisiones(csv_path: Path) -> dict[str, str]:
    """{'<ipt_nombre>|<zona>': decisión} de las filas con decision llena. Falla si hay valores inválidos."""
    df = pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    faltan = set(COLUMNAS[:2] + ["decision"]) - set(df.columns)
    if faltan:
        raise ValueError(f"Faltan columnas en {csv_path}: {sorted(faltan)}")
    df["decision"] = df["decision"].str.strip().str.upper()
    llenas = df[df["decision"] != ""]
    malas = llenas[~llenas["decision"].isin(DECISIONES)]
    if not malas.empty:
        filas = "; ".join(f"{a}|{b} = {d!r}" for a, b, d in zip(malas.ipt_nombre, malas.zona, malas.decision))
        raise ValueError(f"Decisiones inválidas (válidas: {sorted(DECISIONES)}): {filas}")
    return {f"{a}|{b}": d for a, b, d in zip(llenas["ipt_nombre"], llenas["zona"], llenas["decision"])}


def importar_revision(csv_path: Path, config_path: Path) -> dict[str, str]:
    """Fusiona las decisiones del CSV en zone_overrides de config.yaml (el CSV gana ante la misma zona).
    Reescribe solo el bloque zone_overrides y conserva comentarios y el resto del archivo."""
    nuevas = leer_decisiones(csv_path)
    texto = config_path.read_text(encoding="utf-8")
    actuales = yaml.safe_load(texto).get("zone_overrides") or {}
    por_clave = {_clave(*k.split("|", 1)): k for k in actuales}
    fusion = dict(actuales)
    for k, v in nuevas.items():
        fusion.pop(por_clave.get(_clave(*k.split("|", 1)), k), None)
        fusion[k] = v
    if fusion:
        bloque = "zone_overrides:\n" + "".join(
            f"  {json.dumps(k, ensure_ascii=False)}: {v}\n" for k, v in sorted(fusion.items()))
    else:
        bloque = "zone_overrides: {}\n"
    # bloque actual: la línea 'zone_overrides:' y sus entradas indentadas (no los comentarios que siguen)
    patron = re.compile(r"^zone_overrides:.*\n(?:[ \t]+\S.*\n)*", re.M)
    if not patron.search(texto):
        raise ValueError(f"No encontré 'zone_overrides:' en {config_path}")
    nuevo = patron.sub(lambda _: bloque, texto, count=1)
    assert (yaml.safe_load(nuevo).get("zone_overrides") or {}) == fusion
    config_path.write_text(nuevo, encoding="utf-8")
    log.info("zone_overrides: %d decisiones importadas, %d en total", len(nuevas), len(fusion))
    return fusion
