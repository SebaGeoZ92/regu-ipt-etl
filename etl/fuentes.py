"""Fuentes bajo demanda (docs/FUENTES_BAJO_DEMANDA.md): contratos de datos, relevancia y registro de demanda.

- Cada fuente externa tiene un contrato en fuentes/<id>.yaml, validado contra fuentes/_esquema.json y contra las
  reglas por estado: propuesta → contrato → mapeada → latente → activa → mantenida.
- ficha() llama a registrar_demanda(): una línea por consulta en data/demanda/consultas.jsonl con fecha, región,
  comuna, clase y las fuentes relevantes que aún no están activas. SIN coordenadas (privacidad del usuario).
- estado(): votos por región frente al umbral de cada contrato (`run.py fuentes estado`).
`activar()` NO está implementado todavía: ninguna fuente se activa hasta que se decida (y exista su mapeo).
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

RAIZ = Path(__file__).resolve().parents[1]
DIR_FUENTES = RAIZ / "fuentes"
ESTADOS = ["propuesta", "contrato", "mapeada", "latente", "activa", "mantenida"]
ACTIVOS = {"activa", "mantenida"}
PERSONALES_MINIMOS = {"RUT", "NOMBRE"}


# ── validación ───────────────────────────────────────────────────────────────
def _tipo_ok(v, t) -> bool:
    tipos = t if isinstance(t, list) else [t]
    mapa = {"string": str, "object": dict, "array": list, "boolean": bool, "null": type(None)}
    for x in tipos:
        if x == "integer" and isinstance(v, int) and not isinstance(v, bool):
            return True
        if x == "number" and isinstance(v, (int, float)) and not isinstance(v, bool):
            return True
        if x in mapa and isinstance(v, mapa[x]):
            return True
    return False


def _validar_esquema(v, s: dict, ruta: str = "") -> list[str]:
    """Subconjunto de JSON Schema (type, enum, pattern, required, properties, items, minItems, minimum) para no
    depender de jsonschema; si está instalado se usa ese."""
    err = []
    if "type" in s and not _tipo_ok(v, s["type"]):
        return [f"{ruta or '/'}: tipo {type(v).__name__}, se esperaba {s['type']}"]
    if "enum" in s and v not in s["enum"]:
        err.append(f"{ruta}: {v!r} no está en {s['enum']}")
    if "pattern" in s and isinstance(v, str) and not re.search(s["pattern"], v):
        err.append(f"{ruta}: {v!r} no calza con {s['pattern']}")
    if "minimum" in s and isinstance(v, (int, float)) and v < s["minimum"]:
        err.append(f"{ruta}: {v} < {s['minimum']}")
    if isinstance(v, dict):
        for k in s.get("required", []):
            if k not in v:
                err.append(f"{ruta}/{k}: falta")
        for k, sub in (s.get("properties") or {}).items():
            if k in v:
                err += _validar_esquema(v[k], sub, f"{ruta}/{k}")
    if isinstance(v, list):
        if len(v) < s.get("minItems", 0):
            err.append(f"{ruta}: se esperaban al menos {s['minItems']} elementos")
        if "items" in s:
            for i, x in enumerate(v):
                err += _validar_esquema(x, s["items"], f"{ruta}[{i}]")
    return err


def validar(c: dict, esquema: dict | None = None, archivo: Path | None = None) -> list[str]:
    """Errores del contrato: esquema + reglas del documento (datos personales y requisitos por estado)."""
    esquema = esquema or json.loads((DIR_FUENTES / "_esquema.json").read_text(encoding="utf-8"))
    try:
        import jsonschema
        err = [f"{'/'.join(map(str, e.path)) or '/'}: {e.message}"
               for e in jsonschema.Draft7Validator(esquema).iter_errors(c)]
    except ImportError:
        err = _validar_esquema(c, esquema)
    if err:
        return err
    if archivo is not None and archivo.stem != c["id"]:
        err.append(f"id {c['id']!r} distinto del nombre de archivo {archivo.name!r}")
    excl = {x.upper() for x in c["excluir_siempre"]}
    if not PERSONALES_MINIMOS <= excl:
        err.append(f"excluir_siempre debe incluir al menos {sorted(PERSONALES_MINIMOS)}")
    for m in c["mapeo"]:
        if m["origen"].upper() in excl:
            err.append(f"mapeo toma {m['origen']!r}, que está en excluir_siempre (dato personal)")
    n = ESTADOS.index(c["estado"])
    if n >= ESTADOS.index("contrato") and (not c["diccionario"] or not c["licencia"]):
        err.append("estado >= contrato exige diccionario y licencia")
    if n >= ESTADOS.index("mapeada") and not c["mapeo"]:
        err.append("estado >= mapeada exige mapeo")
    if n >= ESTADOS.index("latente") and (not c["acceso"]["tipo"] or (c["acceso"]["tipo"] != "manual" and not c["acceso"]["url"])):
        err.append("estado >= latente exige acceso.tipo y acceso.url (o tipo manual)")
    return err


def cargar_contratos(dir_fuentes: Path = DIR_FUENTES, validar_al_cargar: bool = True) -> dict[str, dict]:
    esquema = json.loads((dir_fuentes / "_esquema.json").read_text(encoding="utf-8"))
    out = {}
    for p in sorted(dir_fuentes.glob("*.yaml")):
        c = yaml.safe_load(p.read_text(encoding="utf-8"))
        if validar_al_cargar:
            e = validar(c, esquema, p)
            if e:
                raise ValueError(f"Contrato inválido {p.name}: " + "; ".join(e))
        out[c["id"]] = c
    return out


# ── relevancia y demanda ─────────────────────────────────────────────────────
def relevantes(ficha: dict, contratos: dict) -> list[str]:
    """Fuentes NO activas cuya regla de relevancia calza con la ficha (clases presentes y región por CUT)."""
    clases = {p["clase"] for p in ficha.get("particion") or []}
    reg = (ficha.get("cut") or "")[:2]
    out = []
    for fid, c in contratos.items():
        if c["estado"] in ACTIVOS:
            continue
        r = c["demanda"]["relevante_si"]
        if clases & set(r["clases"]) and (not r.get("regiones_cut") or reg in r["regiones_cut"]):
            out.append(fid)
    return out


def registrar_demanda(ficha: dict, dir_demanda: Path, contratos: dict) -> dict:
    """Agrega una línea a consultas.jsonl. Nunca guarda coordenadas ni geometría: solo comuna y región."""
    fuentes = relevantes(ficha, contratos)
    linea = {"fecha": date.today().isoformat(), "region": ficha.get("region"), "cut": ficha.get("cut"),
             "comuna": ficha.get("comuna"),
             "clase": (ficha.get("particion") or [{}])[0].get("clase"),
             "cobertura": ficha.get("cobertura"), "fuentes_relevantes_no_activas": fuentes}
    dir_demanda.mkdir(parents=True, exist_ok=True)
    with open(dir_demanda / "consultas.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(linea, ensure_ascii=False) + "\n")
    return linea


def leer_demanda(dir_demanda: Path) -> pd.DataFrame:
    p = dir_demanda / "consultas.jsonl"
    if not p.exists():
        return pd.DataFrame(columns=["fecha", "region", "cut", "comuna", "clase", "fuente"])
    filas = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    df = pd.DataFrame(filas)
    return df.explode("fuentes_relevantes_no_activas").rename(columns={"fuentes_relevantes_no_activas": "fuente"}) \
             .dropna(subset=["fuente"])


def estado(contratos: dict, dir_demanda: Path) -> pd.DataFrame:
    """fuente | estado | votos por región | umbral | ¿lista para activar? (latente y una región ≥ umbral)."""
    d = leer_demanda(dir_demanda)
    filas = []
    for fid, c in contratos.items():
        v = d[d.fuente == fid].groupby("region").size().sort_values(ascending=False) if len(d) else pd.Series(dtype=int)
        umbral = c["demanda"]["umbral_activacion"]
        filas.append({"fuente": fid, "estado": c["estado"], "institucion": c["institucion"],
                      "votos_total": int(v.sum()),
                      "votos_por_region": "; ".join(f"{k}: {n}" for k, n in v.items()) or "—",
                      "umbral": umbral, "region_max": int(v.max()) if len(v) else 0,
                      "lista_para_activar": bool(c["estado"] == "latente" and len(v) and v.max() >= umbral),
                      "falta": _falta(c)})
    return pd.DataFrame(filas)


def _falta(c: dict) -> str:
    return {"propuesta": "conseguir estructura y diccionario", "contrato": "decidir columnas y mapeo",
            "mapeada": "acceso automatizable o procedimiento manual", "latente": "demanda >= umbral o decisión manual",
            "activa": "—", "mantenida": "—"}[c["estado"]]
