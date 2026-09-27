#!/usr/bin/env python3
"""regu-ipt-etl · capa nacional de situación normativa del suelo (IPT) para regu.cl

Uso:
  python run.py discover [--region ARAUCANIA]      # catálogo de servicios/capas MINVU
  python run.py download [--region ARAUCANIA] [--refresh]
  python run.py build    [--region ARAUCANIA] [--postgis]
  python run.py all      [--region ARAUCANIA] [--postgis]
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import yaml
from pyproj import Transformer

from etl.arcgis import ArcGISClient, ArcGISError, discover, download_entry, raw_path
from etl.classify import clasificar
from etl.export import anotar_legal, cargar_postgis, escribir
from etl.normalize import ComunaResolver, load_layer, norm_txt, tipo_capa

ROOT = Path(__file__).parent
log = logging.getLogger("regu-ipt")


def cargar_cfg() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def cargar_comunas(cfg: dict, region: str | None) -> gpd.GeoDataFrame:
    p = ROOT / cfg["paths"]["comunas"]
    if not p.exists():
        sys.exit(f"Falta la capa de comunas en {p}. Ver README (DPA nacional).")
    c = cfg["comunas"]
    gdf = gpd.read_file(p, layer=c["layer"]) if c["layer"] else gpd.read_file(p)
    for f in (c["field_cut"], c["field_nombre"], c["field_region"]):
        if f not in gdf.columns:
            sys.exit(f"Campo '{f}' no existe en comunas. Disponibles: {list(gdf.columns)}")
    gdf[c["field_cut"]] = gdf[c["field_cut"]].astype(str).str.zfill(5)
    if region:
        gdf = gdf[gdf[c["field_region"]].map(norm_txt).str.contains(norm_txt(region), regex=True)]
        if gdf.empty:
            sys.exit(f"Ninguna comuna coincide con región '{region}'")
    return gdf


def filtro_por_extension(comunas: gpd.GeoDataFrame):
    """Filtra servicios cuya extensión cruza el bbox de las comunas seleccionadas."""
    bb4326 = comunas.to_crs(4326).total_bounds

    def _f(name, sinfo):
        ext = sinfo.get("fullExtent") or sinfo.get("initialExtent")
        if not ext:
            return True
        sr = ext.get("spatialReference") or {}
        wkid = sr.get("latestWkid") or sr.get("wkid") or 4326
        try:
            t = Transformer.from_crs(4326, wkid, always_xy=True)
            xmin, ymin = t.transform(bb4326[0], bb4326[1])
            xmax, ymax = t.transform(bb4326[2], bb4326[3])
        except Exception:
            return True
        return not (ext["xmax"] < min(xmin, xmax) or ext["xmin"] > max(xmin, xmax)
                    or ext["ymax"] < min(ymin, ymax) or ext["ymin"] > max(ymin, ymax))
    return _f


def cliente(cfg) -> ArcGISClient:
    a = cfg["arcgis"]
    return ArcGISClient(a["base_url"], a["timeout"], a["retries"], a["backoff"],
                        a["pause_s"], a["geometry_precision"])


def catalogo_path(cfg) -> Path:
    return ROOT / cfg["paths"]["raw"] / "catalogo.json"


def cmd_discover(cfg, args):
    comunas = cargar_comunas(cfg, args.region) if args.region else None
    filtro = filtro_por_extension(comunas) if comunas is not None else None
    cat = discover(cliente(cfg), cfg["arcgis"]["folders"], filtro)
    for e in cat:
        e["tipo"] = tipo_capa(e, cfg["layer_rules"], cfg.get("overrides") or {})
    p = catalogo_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cat, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(p.with_suffix(".csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["service", "layer_id", "layer_name", "tipo", "url", "service_wkid", "service_type"])
        w.writeheader()
        w.writerows(cat)
    tipos = pd.Series([e["tipo"] for e in cat]).value_counts().to_dict()
    log.info("Catálogo: %d capas · %s · revisa %s", len(cat), tipos, p.with_suffix(".csv"))


def leer_catalogo(cfg) -> list[dict]:
    p = catalogo_path(cfg)
    if not p.exists():
        sys.exit("No hay catálogo. Corre primero: python run.py discover")
    cat = json.loads(p.read_text(encoding="utf-8"))
    # re-aplica reglas/overrides por si cambiaron en config.yaml
    for e in cat:
        e["tipo"] = tipo_capa(e, cfg["layer_rules"], cfg.get("overrides") or {})
    return [e for e in cat if e["tipo"] != "IGNORAR"]


def cmd_download(cfg, args):
    cli = cliente(cfg)
    raw = ROOT / cfg["paths"]["raw"]
    fallas = []
    for e in leer_catalogo(cfg):
        try:
            download_entry(cli, e, raw, refresh=args.refresh)
        except ArcGISError as ex:
            log.error("✗ %s/%s: %s", e["service"], e["layer_name"], ex)
            fallas.append({**e, "error": str(ex)})
    if fallas:
        (raw / "fallas_descarga.json").write_text(json.dumps(fallas, ensure_ascii=False, indent=2), encoding="utf-8")
        log.warning("%d capas fallaron · vuelve a correr download (lo ya bajado no se repite)", len(fallas))


def cmd_build(cfg, args):
    c = cfg["comunas"]
    comunas = cargar_comunas(cfg, args.region)
    resolver = ComunaResolver(comunas if not args.region else cargar_comunas(cfg, None),
                              c["field_cut"], c["field_nombre"])
    raw = ROOT / cfg["paths"]["raw"]
    capas, afect = [], []
    for e in leer_catalogo(cfg):
        p = raw_path(raw, e)
        if not p.exists():
            continue
        g = load_layer(p, e, cfg, resolver)
        if g is None or g.empty:
            continue
        (afect if e["tipo"] == "AFECTACION" else capas).append(g)
    if not capas:
        log.warning("No hay capas IPT descargadas: todo quedará como R2")
    fuentes = gpd.GeoDataFrame(pd.concat(capas, ignore_index=True), crs=4326) if capas else \
        gpd.GeoDataFrame(columns=["fuente", "cut_ipt", "geometry"], geometry="geometry", crs=4326)
    capa, qas = clasificar(comunas, fuentes, cfg, c["field_cut"], c["field_nombre"], c["field_region"])
    legal = json.loads((ROOT / "legal_refs.json").read_text(encoding="utf-8"))
    capa = anotar_legal(capa, legal)
    afect_gdf = gpd.GeoDataFrame(pd.concat(afect, ignore_index=True), crs=4326) if afect else None
    sufijo = norm_txt(args.region).lower().replace(" ", "_") if args.region else "nacional"
    prod = escribir(capa, afect_gdf, qas, ROOT / cfg["paths"]["out"], cfg, sufijo)
    log.info("Listo: %s", json.dumps(prod["resumen"], ensure_ascii=False))
    if args.postgis:
        cargar_postgis(capa, afect_gdf)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["discover", "download", "build", "all"])
    ap.add_argument("--region", help="regex sobre el nombre de región (ej. ARAUCANIA)")
    ap.add_argument("--refresh", action="store_true", help="vuelve a descargar aunque exista caché")
    ap.add_argument("--postgis", action="store_true", help="carga a PostGIS (requiere DATABASE_URL)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    cfg = cargar_cfg()
    if args.cmd in ("discover", "all"):
        cmd_discover(cfg, args)
    if args.cmd in ("download", "all"):
        cmd_download(cfg, args)
    if args.cmd in ("build", "all"):
        cmd_build(cfg, args)


if __name__ == "__main__":
    main()
