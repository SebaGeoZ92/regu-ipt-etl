"""Tipificación de capas, homologación de atributos y limpieza geométrica."""
from __future__ import annotations

import json
import re
import unicodedata

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry.base import BaseGeometry

COMUNALES = {"PRC", "SECCIONAL", "LU"}
INTERCOMUNALES = {"PRI", "PRM"}
_PREFIJO = re.compile(r"^(prc|prms|prm|pri|seccional|ps|lu|plan[_ ]regulador|limite[_ ]urbano)[_ ]*", re.I)


def norm_txt(s) -> str:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[_\-]+", " ", s)).strip().upper()


def tipo_capa(entry: dict, rules: list[dict], overrides: dict) -> str:
    key = f"{entry['service']}/{entry['layer_id']}"
    if key in overrides:
        return overrides[key]
    for r in rules:
        if re.search(r["pattern"], entry["layer_name"], re.I):
            return r["tipo"]
    return "IGNORAR"


def aplicar_reglas(entry: dict, cfg: dict) -> dict:
    """Asigna tipo (y pri_default) a una capa: override > regla de servicio > regla de capa."""
    key = f"{entry['service']}/{entry['layer_id']}"
    entry["pri_default"] = None
    overrides = cfg.get("overrides") or {}
    if key in overrides:
        entry["tipo"] = overrides[key]
        return entry
    for r in cfg.get("service_rules") or []:
        if re.search(r["pattern"], entry["service"], re.I):
            entry["tipo"] = r["tipo"]
            entry["pri_default"] = r.get("pri_default")
            return entry
    entry["tipo"] = tipo_capa(entry, cfg["layer_rules"], {})
    return entry


def normalizar_catalogo(cat: list[dict], cfg: dict) -> list[dict]:
    """Aplica reglas y elimina duplicados MapServer/FeatureServer (misma capa en ambos: gana MapServer)."""
    vistos, out = {}, []
    for e in sorted(cat, key=lambda e: (e["service"], e["layer_name"], e.get("service_type") != "MapServer")):
        k = (e["service"], e["layer_name"])
        if k in vistos:
            continue
        vistos[k] = True
        out.append(aplicar_reglas(dict(e), cfg))
    return out


def nombre_ipt(layer_name: str) -> str:
    return _PREFIJO.sub("", layer_name).replace("_", " ").strip() or layer_name


def pick_field(columns, candidates, excluir=None) -> str | None:
    up = {c.upper(): c for c in columns}
    for cand in candidates:
        c = up.get(cand.upper())
        if c and c != excluir:
            return c
    return None


def polygonal(geom: BaseGeometry | None) -> BaseGeometry | None:
    """make_valid + quedarse solo con la parte poligonal."""
    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        geom = shapely.make_valid(geom)
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        return shapely.union_all(polys) if polys else None
    return None


def pri_subclase(zona: str, desc: str, patrones: dict) -> str | None:
    txt = f"{norm_txt(zona)} {norm_txt(desc)}"
    for sub, pat in patrones.items():
        if re.search(pat, txt):
            return sub
    return None


class ComunaResolver:
    """Resuelve el CUT de una comuna a partir de un nombre o código libre."""

    def __init__(self, comunas: gpd.GeoDataFrame, f_cut: str, f_nom: str):
        self.por_nombre = {norm_txt(n): str(c) for c, n in zip(comunas[f_cut], comunas[f_nom])}
        self.cuts = set(self.por_nombre.values())
        self.nombre = {str(c): n for c, n in zip(comunas[f_cut], comunas[f_nom])}

    def resolve(self, valor) -> str | None:
        if valor is None or (isinstance(valor, float) and pd.isna(valor)):
            return None
        s = str(valor).strip()
        if s.isdigit():
            s5 = s.zfill(5)
            return s5 if s5 in self.cuts else (s if s in self.cuts else None)
        tokens = norm_txt(s).split()
        # "CHANARAL FLAMENCO" -> "CHANARAL FLAMENCO", "CHANARAL"
        for n in range(len(tokens), 0, -1):
            cut = self.por_nombre.get(" ".join(tokens[:n]))
            if cut:
                return cut
        return None


def _texto(gdf, campo):
    if not campo:
        return [None] * len(gdf)
    return [None if (v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "")
            else str(v).strip() for v in gdf[campo]]


def load_layer(path, entry: dict, cfg: dict, resolver: ComunaResolver) -> gpd.GeoDataFrame | None:
    raw = json.loads(open(path, encoding="utf-8").read())
    meta = raw.pop("_meta", {})
    if not raw.get("features"):
        return None
    gdf = gpd.GeoDataFrame.from_features(raw["features"], crs="EPSG:4326")
    props = [c for c in gdf.columns if c != "geometry"]
    zf = pick_field(props, cfg["zone_fields"])
    df = pick_field(props, cfg["zone_desc_fields"], excluir=zf)
    cf = pick_field(props, cfg["comuna_fields"])

    tipo = entry["tipo"]
    nombre = nombre_ipt(entry["layer_name"])
    n = len(gdf)
    out = gpd.GeoDataFrame({
        "ipt_tipo": [tipo] * n,
        "ipt_nombre": [nombre] * n,
        "servicio": [entry["service"]] * n,
        "capa": [entry["layer_name"]] * n,
        "zona": _texto(gdf, zf),
        "zona_desc": _texto(gdf, df),
        "attrs_raw": [json.dumps({k: v for k, v in r.items()}, ensure_ascii=False, default=str)
                      for r in gdf[props].to_dict("records")],
        "fuente_url": [entry["url"]] * n,
        "fecha_extraccion": [meta.get("fetched_at")] * n,
    }, geometry=[polygonal(g) for g in gdf.geometry], crs="EPSG:4326")
    out = out[out.geometry.notna()].copy()

    # CUT del instrumento: atributo por feature > nombre de la capa. PRI/PRM no se amarran a comuna.
    if tipo in COMUNALES:
        cut_capa = resolver.resolve(nombre)
        if cf:
            out["cut_ipt"] = [resolver.resolve(v) or cut_capa for v in gdf.loc[out.index, cf]]
        else:
            out["cut_ipt"] = cut_capa
    else:
        out["cut_ipt"] = None
    if tipo == "LU":
        out["ipt_nombre"] = [f"Límite Urbano {resolver.nombre.get(c, '')}".strip() for c in out["cut_ipt"]]

    # Fuente de clasificación
    if tipo in INTERCOMUNALES:
        subs = [pri_subclase(z, d, cfg["pri_subclase"]) for z, d in zip(out["zona"], out["zona_desc"])]
        defecto = entry.get("pri_default")
        out["fuente"] = [f"PRI_{s or defecto or 'R'}" for s in subs]
        out["revisar"] = [s is None and not defecto for s in subs]
    else:
        out["fuente"] = tipo
        out["revisar"] = False

    # zone_overrides "<ipt_nombre>|<zona>" (validados por el arquitecto) ganan sobre pri_subclase
    zov = {_clave_zona(*k.split("|", 1)): v for k, v in (cfg.get("zone_overrides") or {}).items()}
    if zov:
        for i, (nom, z) in zip(out.index, zip(out["ipt_nombre"], out["zona"])):
            v = zov.get(_clave_zona(nom, z))
            if v is None:
                continue
            if v == "AFECTACION":
                out.at[i, "fuente"] = "AFECTACION"
            elif tipo in INTERCOMUNALES:
                out.at[i, "fuente"] = f"PRI_{v}"
            else:
                continue
            out.at[i, "revisar"] = False
    return out


def _clave_zona(nombre, zona) -> str:
    return f"{norm_txt(nombre)}|{norm_txt(zona)}"


def separar_afectaciones(g: gpd.GeoDataFrame, tipo: str) -> tuple[gpd.GeoDataFrame | None, gpd.GeoDataFrame | None]:
    """(partición, afectaciones): capas AFECTACION completas, o filas redirigidas a AFECTACION."""
    if tipo == "AFECTACION":
        return None, g
    m = g["fuente"] == "AFECTACION"
    return (g[~m] if (~m).any() else None), (g[m] if m.any() else None)
