#!/usr/bin/env python3
"""regu-ipt-etl · capa nacional de situación normativa del suelo (IPT) para regu.cl

Uso:
  python run.py discover [--region ARAUCANIA]      # catálogo de servicios/capas MINVU
  python run.py catalogo                           # re-aplica reglas de config.yaml al catálogo (sin red)
  python run.py download [--region ARAUCANIA] [--refresh] [--capas IPT/PRC_Maule/3,...] [--vacias]
  python run.py build    [--region ARAUCANIA] [--postgis]
  python run.py all      [--region ARAUCANIA] [--postgis]
  python run.py importar-revision data/out/revision_arquitecto.csv   # decisiones del arquitecto → zone_overrides
  python run.py ficha --lon -72.59 --lat -38.74 [--gpkg ...]       # ficha normativa preliminar (JSON)
  python run.py ficha --wkt "POLYGON((...))"
  python run.py mapa --region ARAUCANIA                             # HTML autocontenido con PMTiles (para enviar)
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
from etl.classify import clasificar, instrumentos_sin_comuna, ipt_fuera_de_dpa, recortar_afectaciones
from etl.export import anotar_legal, cargar_postgis, escribir
from etl.normalize import ComunaResolver, load_layer, norm_txt, normalizar_catalogo, separar_afectaciones
from etl.revision import generar_revision, importar_revision
from etl import progreso

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
    p = catalogo_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.with_name("catalogo_bruto.json").write_text(json.dumps(cat, ensure_ascii=False, indent=2), encoding="utf-8")
    guardar_catalogo(cfg, cat)


def guardar_catalogo(cfg, bruto: list[dict]) -> list[dict]:
    cat = normalizar_catalogo(bruto, cfg)
    p = catalogo_path(cfg)
    p.write_text(json.dumps(cat, ensure_ascii=False, indent=2), encoding="utf-8")
    campos = ["service", "layer_id", "layer_name", "tipo", "pri_default", "url", "service_wkid", "service_type"]
    with open(p.with_suffix(".csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(cat)
    tipos = pd.Series([e["tipo"] for e in cat]).value_counts().to_dict()
    log.info("Catálogo: %d capas (de %d brutas) · %s · %s", len(cat), len(bruto), tipos, p.with_suffix(".csv"))
    return cat


def cmd_catalogo(cfg, args):
    p = catalogo_path(cfg).with_name("catalogo_bruto.json")
    if not p.exists():
        p = catalogo_path(cfg)  # catálogo de la versión anterior
    if not p.exists():
        sys.exit("No hay catálogo. Corre primero: python run.py discover")
    cat = guardar_catalogo(cfg, json.loads(p.read_text(encoding="utf-8")))
    ign = [e for e in cat if e["tipo"] == "IGNORAR"]
    if ign:
        print("\nCapas IGNORADAS (revisar si alguna debería entrar):")
        for e in ign:
            print(f"  {e['service']}/{e['layer_id']:<4} {e['layer_name']}")


def leer_catalogo(cfg) -> list[dict]:
    p = catalogo_path(cfg)
    if not p.exists():
        sys.exit("No hay catálogo. Corre primero: python run.py discover")
    # re-aplica reglas/overrides por si cambiaron en config.yaml
    cat = normalizar_catalogo(json.loads(p.read_text(encoding="utf-8")), cfg)
    return [e for e in cat if e["tipo"] != "IGNORAR"]


def filtrar_catalogo_region(cli: ArcGISClient, cat: list[dict], filtro) -> list[dict]:
    """Mismo filtro por extensión de servicio que discover. Catálogos antiguos no traen
    service_extent: se pide la info del servicio una vez; si falla, la capa se incluye."""
    extents: dict[tuple, dict | None] = {}
    out = []
    for e in cat:
        k = (e["service"], e.get("service_type") or "MapServer")
        if k not in extents:
            ext = e.get("service_extent")
            if ext is None:
                try:
                    sinfo = cli.service_info(*k)
                    ext = sinfo.get("fullExtent") or sinfo.get("initialExtent")
                except ArcGISError as ex:
                    log.warning("Sin extensión para %s (%s): se incluye", k[0], ex)
            extents[k] = ext
        if extents[k] is None or filtro(e["service"], {"fullExtent": extents[k]}):
            out.append(e)
    log.info("--region: %d de %d capas en %d servicios", len(out), len(cat), len({x["service"] for x in out}))
    return out


def features_en_cache(p: Path) -> int | None:
    """Número de features de una capa en caché (None si no está o no se puede leer)."""
    if not p.exists():
        return None
    try:
        return int(json.loads(p.read_text(encoding="utf-8")).get("_meta", {}).get("count", 0))
    except (ValueError, OSError):
        return None


def cmd_download(cfg, args):
    cli = cliente(cfg)
    raw = ROOT / cfg["paths"]["raw"]
    fallas = []
    cat = leer_catalogo(cfg)
    if args.region:
        cat = filtrar_catalogo_region(cli, cat, filtro_por_extension(cargar_comunas(cfg, args.region)))
    refresh = args.refresh
    # --capas / --vacias: solo esas capas, y siempre se vuelven a pedir (ignoran el caché)
    if args.capas:
        pedidas = {c.strip() for c in args.capas.split(",") if c.strip()}
        cat = [e for e in cat if f"{e['service']}/{e['layer_id']}" in pedidas]
        faltan = pedidas - {f"{e['service']}/{e['layer_id']}" for e in cat}
        if faltan:
            log.warning("--capas no están en el catálogo (o están IGNORADAS): %s", sorted(faltan))
        refresh = True
    if args.vacias:
        cat = [e for e in cat if features_en_cache(raw_path(raw, e)) == 0]
        refresh = True
    if args.capas or args.vacias:
        log.info("Se vuelven a pedir %d capas: %s", len(cat), [f"{e['service']}/{e['layer_id']}" for e in cat])
    for i, e in enumerate(cat, 1):
        progreso.paso(e["service"], e["layer_name"], i, len(cat))
        try:
            antes = features_en_cache(raw_path(raw, e))
            p = download_entry(cli, e, raw, refresh=refresh)
            if args.capas or args.vacias:
                log.info("%s/%s %s: %s → %s features", e["service"], e["layer_id"], e["layer_name"],
                         antes, features_en_cache(p))
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
    cat = leer_catalogo(cfg)
    for i, e in enumerate(cat, 1):
        progreso.paso(e["service"], e["layer_name"], i, len(cat))
        p = raw_path(raw, e)
        if not p.exists():
            continue
        g = load_layer(p, e, cfg, resolver)
        if g is None or g.empty:
            continue
        part, af = separar_afectaciones(g, e["tipo"])
        if part is not None:
            capas.append(part)
        if af is not None:
            afect.append(af)
    if not capas:
        log.warning("No hay capas IPT descargadas: todo quedará como R2")
    fuentes = gpd.GeoDataFrame(pd.concat(capas, ignore_index=True), crs=4326) if capas else \
        gpd.GeoDataFrame(columns=["fuente", "cut_ipt", "geometry"], geometry="geometry", crs=4326)
    afect_crudas = gpd.GeoDataFrame(pd.concat(afect, ignore_index=True), crs=4326) if afect else None
    afect_gdf = None
    if afect_crudas is not None:
        afect_gdf = recortar_afectaciones(afect_crudas, comunas, cfg, c["field_cut"], c["field_nombre"])
    riesgos = afect_gdf[afect_gdf["riesgo"].fillna(False).astype(bool)] if afect_gdf is not None else None
    capa, qas = clasificar(comunas, fuentes, cfg, c["field_cut"], c["field_nombre"], c["field_region"], riesgos)
    # QA sin comuna: zonificación + capas AFECTACION (las copias de zonas de riesgo ya están en fuentes)
    qa_com = fuentes if afect_crudas is None else gpd.GeoDataFrame(pd.concat(
        [fuentes, afect_crudas[afect_crudas["ipt_tipo"] == "AFECTACION"]], ignore_index=True), crs=4326)
    sin_comuna = instrumentos_sin_comuna(qa_com, comunas, c["field_nombre"])
    for _, r in sin_comuna.iterrows():
        log.warning("Sin comuna: %s/%s (%d features) toca %s", r["servicio"], r["capa"], r["features"], r["comunas_tocadas"])
    fuera_dpa = ipt_fuera_de_dpa(fuentes, cargar_comunas(cfg, None), comunas, cfg,
                                 c["field_cut"], c["field_nombre"], c["field_region"])
    if not fuera_dpa.empty:
        log.warning("IPT comunal fuera de la DPA BCN: %.1f ha en %d comunas (máx %s %.1f ha)", fuera_dpa.ha_fuera.sum(),
                    len(fuera_dpa), fuera_dpa.comuna.iloc[0], fuera_dpa.ha_fuera.iloc[0])
    legal = json.loads((ROOT / "legal_refs.json").read_text(encoding="utf-8"))
    capa = anotar_legal(capa, legal)
    sufijo = norm_txt(args.region).lower().replace(" ", "_") if args.region else "nacional"
    prod = escribir(capa, afect_gdf, qas, ROOT / cfg["paths"]["out"], cfg, sufijo, sin_comuna, fuera_dpa)
    rev = generar_revision(capa, ROOT / cfg["paths"]["out"] / "revision_arquitecto.csv")
    log.info("revision_arquitecto.csv: %d zonas con revisar=True (%d ya decididas)",
             len(rev), int((rev["decision"] != "").sum()))
    log.info("Listo: %s", json.dumps(prod["resumen"], ensure_ascii=False))
    if args.postgis:
        cargar_postgis(capa, afect_gdf)


def cmd_ficha(cfg, args):
    from shapely import wkt as _wkt
    from shapely.geometry import Point

    from etl.ficha import a_json, ficha, ultimo_gpkg
    if args.wkt:
        geom = _wkt.loads(args.wkt)
    elif args.lon is not None and args.lat is not None:
        geom = Point(args.lon, args.lat)
    else:
        sys.exit("Uso: python run.py ficha --lon X --lat Y  |  --wkt \"POLYGON((...))\"  [--gpkg archivo]")
    gpkg = Path(args.gpkg) if args.gpkg else ultimo_gpkg(ROOT / cfg["paths"]["out"])
    if not gpkg or not gpkg.exists():
        sys.exit("No hay GPKG de build. Corre primero: python run.py build")
    print(a_json(ficha(geom, gpkg)))


def cmd_mapa(cfg, args):
    from etl.mapa import generar_mapa
    if not args.region:
        sys.exit("Uso: python run.py mapa --region ARAUCANIA [--gpkg archivo]  (un mapa por región)")
    c = cfg["comunas"]
    comunas = cargar_comunas(cfg, args.region)
    sufijo = norm_txt(args.region).lower().replace(" ", "_")
    out = ROOT / cfg["paths"]["out"]
    gpkg = Path(args.gpkg) if args.gpkg else (sorted(out.glob(f"regu_ipt_{sufijo}_*.gpkg")) or [None])[-1]
    if not gpkg or not gpkg.exists():
        sys.exit(f"No hay GPKG de la región. Corre primero: python run.py build --region {args.region}")
    legal = json.loads((ROOT / "legal_refs.json").read_text(encoding="utf-8"))
    region = str(comunas[c["field_region"]].mode().iloc[0])
    r = generar_mapa(gpkg, comunas, c["field_cut"], c["field_nombre"], legal, region, out / f"mapa_{sufijo}")
    log.info("Mapa: %s (%.1f MB; PMTiles %.1f MB) · variante para publicar: %s", r["html"], r["mb_html"], r["mb_pmtiles"], r["fragmento"])


def cmd_importar_revision(cfg, args):
    if not args.archivo:
        sys.exit("Uso: python run.py importar-revision <csv>")
    try:
        fusion = importar_revision(Path(args.archivo), ROOT / "config.yaml")
    except (ValueError, FileNotFoundError) as ex:
        sys.exit(str(ex))
    print(f"zone_overrides en config.yaml: {len(fusion)} zonas. Corre build para aplicarlas.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["discover", "catalogo", "download", "build", "all", "importar-revision", "ficha", "mapa"])
    ap.add_argument("--lon", type=float, help="ficha: longitud (EPSG:4326)")
    ap.add_argument("--lat", type=float, help="ficha: latitud (EPSG:4326)")
    ap.add_argument("--wkt", help="ficha: geometría WKT en EPSG:4326 (punto o polígono)")
    ap.add_argument("--gpkg", help="ficha: GPKG de build (por defecto el más reciente, preferente nacional)")
    ap.add_argument("archivo", nargs="?", help="importar-revision: CSV revision_arquitecto con 'decision' llena")
    ap.add_argument("--region", help="regex sobre el nombre de región (ej. ARAUCANIA)")
    ap.add_argument("--refresh", action="store_true", help="vuelve a descargar aunque exista caché")
    ap.add_argument("--capas", help="download: solo estas capas '<servicio>/<id>,...' (ignora el caché)")
    ap.add_argument("--vacias", action="store_true", help="download: vuelve a pedir solo las capas con 0 features en caché")
    ap.add_argument("--postgis", action="store_true", help="carga a PostGIS (requiere DATABASE_URL)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    cfg = cargar_cfg()
    if args.cmd in ("discover", "download", "build", "all"):
        # convención: procesos largos dejan su avance en data/out/progreso.log (ver etl/progreso.py)
        progreso.iniciar(ROOT / cfg["paths"]["out"] / "progreso.log")
    if args.cmd in ("discover", "all"):
        cmd_discover(cfg, args)
    if args.cmd == "catalogo":
        cmd_catalogo(cfg, args)
    if args.cmd in ("download", "all"):
        cmd_download(cfg, args)
    if args.cmd in ("build", "all"):
        cmd_build(cfg, args)
    if args.cmd == "importar-revision":
        cmd_importar_revision(cfg, args)
    if args.cmd == "ficha":
        cmd_ficha(cfg, args)
    if args.cmd == "mapa":
        cmd_mapa(cfg, args)


if __name__ == "__main__":
    main()
