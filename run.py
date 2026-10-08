#!/usr/bin/env python3
"""regu-ipt-etl · capa nacional de situación normativa del suelo (IPT) para regu.cl

Uso:
  python run.py discover [--region ARAUCANIA]      # catálogo de servicios/capas MINVU
  python run.py catalogo                           # re-aplica reglas de config.yaml al catálogo (sin red)
  python run.py download [--region ARAUCANIA] [--refresh] [--capas IPT/PRC_Maule/3,...] [--vacias]
  python run.py build    [--region ARAUCANIA] [--postgis]
  python run.py all      [--region ARAUCANIA] [--postgis]
  python run.py importar-revision data/out/revision_arquitecto_nacional.csv   # decisiones del arquitecto → zone_overrides
  python run.py ficha --lon -72.59 --lat -38.74 [--gpkg ...]       # ficha normativa preliminar (JSON)
  python run.py ficha --wkt "POLYGON((...))"
  python run.py mapa --region ARAUCANIA                             # HTML autocontenido con PMTiles (para enviar)
  python run.py vigencia [--region X] [--refresh]                   # cruce Portal IPT ↔ servidor (brechas)
  python run.py fuentes estado|validar                              # fuentes bajo demanda (docs/FUENTES_BAJO_DEMANDA.md)
  python run.py volumen footprints|candidatas --ipt "Temuco" --region ARAUCANIA   # piloto de volumen, paso 0
  python run.py footprints descargar --region "ARICA|TARAPACA"      # footprints de Overture por región (reanudable)
  python run.py footprints estado                                   # región | edificios | MB | release | fecha | completa
  python run.py volumen plantilla --ipt "Temuco" --zona "ZH-1"      # fila vacía en normas/normas_zona.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import yaml
from pyproj import Transformer

from etl.arcgis import ArcGISClient, ArcGISError, discover, download_entry, raw_path
from etl.classify import (ampliar_comunas, clasificar, extension_costera, instrumentos_sin_comuna,
                          recortar_afectaciones)
from etl.export import anotar_legal, cargar_postgis, escribir
from etl.normalize import ComunaResolver, load_layer, norm_txt, normalizar_catalogo, separar_afectaciones
from etl.revision import generar_revision, importar_revision
from etl import progreso

ROOT = Path(__file__).parent
log = logging.getLogger("regu-ipt")


def cargar_cfg() -> dict:
    """config.yaml + config.local.yaml (opcional, no versionado), que solo sobrescribe `paths` (p.ej. datos en D:)."""
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    local = ROOT / "config.local.yaml"
    if local.exists():
        extra = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
        cfg["paths"] = {**cfg["paths"], **(extra.get("paths") or {})}
    return cfg


def ruta(cfg: dict, clave: str) -> Path:
    """Ruta de datos de config: absoluta tal cual (p.ej. D:/regu-data/out) o relativa al repo (data/out)."""
    p = Path(cfg["paths"][clave])
    return p if p.is_absolute() else ROOT / p


def cargar_comunas(cfg: dict, region: str | None) -> gpd.GeoDataFrame:
    p = ruta(cfg, "comunas")
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
    return ruta(cfg, "raw") / "catalogo.json"


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
    raw = ruta(cfg, "raw")
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
    raw = ruta(cfg, "raw")
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
    # Pérdida costera: cada comuna se amplía con la huella de su propio IPT que queda fuera de la DPA BCN
    fuera_dpa, ext = extension_costera(fuentes, cargar_comunas(cfg, None), comunas, cfg,
                                       c["field_cut"], c["field_nombre"], c["field_region"])
    if ext:
        comunas = ampliar_comunas(comunas, ext, cfg, c["field_cut"])
        log.warning("Extensión costera: %.1f ha de IPT comunal fuera de la DPA BCN en %d comunas (máx %s %.1f ha)",
                    fuera_dpa.ha_extension.sum(), len(ext), fuera_dpa.comuna.iloc[0], fuera_dpa.ha_fuera.iloc[0])
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
    legal =json.loads((ROOT / "legal_refs.json").read_text(encoding="utf-8"))
    capa = anotar_legal(capa, legal)
    sufijo = norm_txt(args.region).lower().replace(" ", "_") if args.region else "nacional"
    prod = escribir(capa, afect_gdf, qas, ruta(cfg, "out"), cfg, sufijo, sin_comuna, fuera_dpa)
    # Inventario del servidor: instrumentos cargados (aunque la partición los tape), para `run.py vigencia`
    inv = (fuentes[fuentes["ipt_tipo"].isin(["PRC", "SECCIONAL", "LU", "PRI", "PRM"])]
           .groupby(["servicio", "capa", "ipt_tipo", "ipt_nombre", "cut_ipt"], dropna=False).size()
           .rename("features").reset_index())
    inv.to_csv(Path(prod["gpkg"]).with_name(Path(prod["gpkg"]).stem.replace("regu_ipt_", "inventario_servidor_") + ".csv"),
               index=False, encoding="utf-8-sig")
    # Un archivo por alcance (nacional o región): un build regional no pisa las decisiones del nacional
    p_rev = ruta(cfg, "out") / f"revision_arquitecto_{sufijo}.csv"
    rev = generar_revision(capa, p_rev)
    log.info("%s: %d zonas con revisar=True (%d ya decididas)", p_rev.name, len(rev), int((rev["decision"] != "").sum()))
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
    gpkg = Path(args.gpkg) if args.gpkg else ultimo_gpkg(ruta(cfg, "out"))
    if not gpkg or not gpkg.exists():
        sys.exit("No hay GPKG de build. Corre primero: python run.py build")
    print(a_json(ficha(geom, gpkg, dir_demanda=ruta(cfg, "demanda"))))


def cmd_mapa(cfg, args):
    from etl.mapa import generar_mapa
    if not args.region:
        sys.exit("Uso: python run.py mapa --region ARAUCANIA [--gpkg archivo]  (un mapa por región)")
    c = cfg["comunas"]
    comunas = cargar_comunas(cfg, args.region)
    sufijo = norm_txt(args.region).lower().replace(" ", "_")
    out = ruta(cfg, "out")
    gpkg = Path(args.gpkg) if args.gpkg else (sorted(out.glob(f"regu_ipt_{sufijo}_*.gpkg")) or [None])[-1]
    if not gpkg or not gpkg.exists():
        sys.exit(f"No hay GPKG de la región. Corre primero: python run.py build --region {args.region}")
    legal = json.loads((ROOT / "legal_refs.json").read_text(encoding="utf-8"))
    region = str(comunas[c["field_region"]].mode().iloc[0])
    r = generar_mapa(gpkg, comunas, c["field_cut"], c["field_nombre"], legal, region, out / f"mapa_{sufijo}")
    log.info("Mapa: %s (%.1f MB; PMTiles %.1f MB) · variante para publicar: %s", r["html"], r["mb_html"], r["mb_pmtiles"], r["fragmento"])


def cmd_vigencia(cfg, args):
    """Cruce Portal IPT ↔ servidor: tabla por comuna y tipo, resumen de brechas y emparejamiento para ficha/mapa."""
    from etl.ficha import ultimo_gpkg
    from etl.portal import PortalIPT
    from etl.vigencia import emparejar, familias, resumen_brechas, tabla_vigencia
    out = ruta(cfg, "out")
    if args.gpkg:
        gpkg = Path(args.gpkg)
    elif args.region:
        suf = norm_txt(args.region).lower().replace(" ", "_")
        gpkg = (sorted(out.glob(f"regu_ipt_{suf}_*.gpkg")) or [None])[-1]
    else:
        gpkg = ultimo_gpkg(out)
    if not gpkg or not gpkg.exists():
        sys.exit("No hay GPKG de build. Corre primero: python run.py build")
    portal = PortalIPT(ruta(cfg, "raw") / "portal", pause_s=float(cfg["arcgis"].get("pause_s", 0.5)) * 2)
    fams = familias(portal.vigentes(refresh=args.refresh))
    serv = pyogrio.read_dataframe(gpkg, layer="capa_ipt", columns=["cut", "comuna", "region", "ipt_tipo", "ipt_nombre"],
                                  read_geometry=False).drop_duplicates()
    comunas = serv[["cut", "comuna", "region"]].drop_duplicates().sort_values(["region", "comuna"])
    # Presencia en servidor: instrumentos comunales desde el inventario del build (incluye los que la partición
    # tapa, p.ej. un LU cubierto por el PRC); PRI/PRM desde la partición (no tienen comuna propia).
    inv_p = gpkg.with_name(gpkg.stem.replace("regu_ipt_", "inventario_servidor_") + ".csv")
    presencia = serv[serv.ipt_tipo.notna()]
    if inv_p.exists():
        inv = pd.read_csv(inv_p, encoding="utf-8-sig", dtype=str).rename(columns={"cut_ipt": "cut"})
        inv = inv[inv.ipt_tipo.isin(["PRC", "SECCIONAL", "LU"]) & inv.cut.notna()][["cut", "ipt_tipo", "ipt_nombre"]]
        presencia = pd.concat([inv, presencia[presencia.ipt_tipo.isin(["PRI", "PRM"])][["cut", "ipt_tipo", "ipt_nombre"]]])
    else:
        log.warning("Sin %s (build anterior): presencia en servidor según la partición", inv_p.name)
    t = tabla_vigencia(fams, presencia, comunas)
    tag = gpkg.stem.removeprefix("regu_ipt_")
    t.to_csv(out / f"vigencia_{tag}.csv", index=False, encoding="utf-8-sig")
    res = resumen_brechas(t)
    (out / f"vigencia_resumen_{tag}.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    m = emparejar(serv[serv.ipt_tipo.notna()], fams)
    m.to_csv(gpkg.parent / "vigencia_match.csv", index=False, encoding="utf-8-sig")
    log.info("Vigencia (%s): %d filas · %s · emparejados %d instrumentos (%s)", gpkg.name, len(t),
             res.get("por_tipo_y_estado"), len(m), m.confianza.value_counts().to_dict() if len(m) else {})
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_fuentes(cfg, args):
    """Fuentes bajo demanda (docs/FUENTES_BAJO_DEMANDA.md): estado | validar. 'activar' aún no existe."""
    from etl.fuentes import DIR_FUENTES, cargar_contratos, estado, validar
    accion = args.archivo or "estado"
    dir_demanda = ruta(cfg, "demanda")
    if accion == "validar":
        esquema = json.loads((DIR_FUENTES / "_esquema.json").read_text(encoding="utf-8"))
        malos = 0
        for p in sorted(DIR_FUENTES.glob("*.yaml")):
            e = validar(yaml.safe_load(p.read_text(encoding="utf-8")), esquema, p)
            malos += bool(e)
            print(f"{'OK ' if not e else 'ERR'} {p.name}" + ("" if not e else "\n    " + "\n    ".join(e)))
        sys.exit(1 if malos else 0)
    if accion == "estado":
        t = estado(cargar_contratos(), dir_demanda)
        consultas = dir_demanda / "consultas.jsonl"
        n = sum(1 for _ in open(consultas, encoding="utf-8")) if consultas.exists() else 0
        print(f"Consultas registradas: {n} ({consultas})\n")
        with pd.option_context("display.width", 200, "display.max_colwidth", 60):
            print(t[["fuente", "estado", "votos_total", "votos_por_region", "umbral", "lista_para_activar", "falta"]]
                  .to_string(index=False))
        return
    if accion == "activar":
        sys.exit("'fuentes activar' todavía no está implementado: ninguna fuente se activa por ahora.")
    sys.exit("Uso: python run.py fuentes estado|validar")


def cmd_ocupacion(cfg, args):
    """Etapa 1 de VOLÚMENES: ocupación real del suelo por zona de PRC (huellas de Overture en paths.footprints).

    Procesa las regiones con descarga completa de footprints (`--region` filtra) y escribe en paths.out:
    ocupacion_zonas_<tag>_<fecha>.csv, ocupacion_<tag>_<fecha>.gpkg (capa `ocupacion_zonas` coloreada) y su QA en JSON.
    """
    import datetime as dt
    from etl import ocupacion as O
    from etl.ficha import ultimo_gpkg
    out = ruta(cfg, "out")
    gpkg = Path(args.gpkg) if args.gpkg else ultimo_gpkg(out)
    if not gpkg or not gpkg.exists():
        sys.exit("No hay GPKG de build (usa --gpkg)")
    disponibles = O.regiones_con_footprints(ruta(cfg, "footprints"))
    if args.region:
        disponibles = {r: p for r, p in disponibles.items() if re.search(args.region, norm_txt(r), re.I)}
    if not disponibles:
        sys.exit("Sin footprints completos para esa selección: corre `python run.py footprints descargar --region X`")
    crs = cfg["crs"]["area"]
    partes, capas = [], []
    for i, (reg, pq) in enumerate(sorted(disponibles.items()), 1):
        progreso.paso("ocupacion", reg, i, len(disponibles))
        capa = gpd.read_file(gpkg, layer="capa_ipt", where=f"fuente = 'PRC' AND region = '{reg.replace(chr(39), chr(39) * 2)}'")
        if capa.empty:
            log.warning("%s: sin piezas PRC en %s", reg, gpkg.name)
            continue
        edif = gpd.read_parquet(pq, columns=["id", "geometry"])
        tabla = O.calcular(capa, edif, crs)
        partes.append(tabla)
        capas.append(O.capa_mapa(capa, tabla, cfg["crs"]["salida"]))
        log.info("%s: %d zonas, %d edificios, %.1f ha de PRC", reg, len(tabla), tabla.n_edificios.sum(), tabla.ha.sum())
    if not partes:
        sys.exit("Ninguna región con piezas PRC")
    tabla = O.combinar(partes)
    mapa = gpd.GeoDataFrame(pd.concat(capas, ignore_index=True), geometry="geometry", crs=cfg["crs"]["salida"])
    todas = gpd.read_file(gpkg, layer="capa_ipt", columns=["region"], ignore_geometry=True)["region"].dropna().unique()
    tag = "nacional" if set(todas) <= set(disponibles) else (
        norm_txt(next(iter(disponibles))).lower().replace(" ", "_") if len(disponibles) == 1 else "parcial")
    fecha = dt.date.today().strftime("%Y%m%d")
    csv = out / f"ocupacion_zonas_{tag}_{fecha}.csv"
    O.formato_csv(tabla).to_csv(csv, index=False, encoding="utf-8-sig")
    gp = out / f"ocupacion_{tag}_{fecha}.gpkg"
    gp.unlink(missing_ok=True)
    mapa.to_file(gp, layer="ocupacion_zonas", driver="GPKG")
    qa = {**O.qa(tabla), "regiones": sorted(disponibles), "gpkg_base": gpkg.name,
          "leyenda": [{"tramo": e, "color": c} for _, e, c in O.TRAMOS] + [{"tramo": O.SIN_DATOS[0], "color": O.SIN_DATOS[1]}]}
    (out / f"ocupacion_qa_{tag}_{fecha}.json").write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("CSV: %s · capa: %s", csv, gp)
    print(json.dumps({k: v for k, v in qa.items() if k != "leyenda"}, ensure_ascii=False, indent=2))


def cmd_app(cfg, args):
    """Regu Suelo local (docs/REGU_SUELO_LOCAL.md): http://localhost:8000. Solo escucha en 127.0.0.1 (no se publica)."""
    import threading
    import webbrowser
    import uvicorn
    from app.ajustes import Ajustes
    from app.main import crear_app
    a = Ajustes.desde_cfg(cfg, ruta, Path(args.gpkg) if args.gpkg else None)
    faltan = [t for t in ("normativa", "ocupacion", "comunas") if not (a.tiles / f"{t}.pmtiles").exists()]
    if faltan:
        log.warning("Faltan teselas (%s): corre `python run.py teselas`. La aplicación abre igual, sin esas capas.", ", ".join(faltan))
    url = f"http://localhost:{args.puerto}"
    if not args.no_abrir:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    log.info("Regu Suelo local en %s · GPKG %s · Ctrl+C para salir", url, a.gpkg.name)
    uvicorn.run(crear_app(a), host="127.0.0.1", port=args.puerto, log_level="warning")


def cmd_temas(cfg, args):
    """Mapas temáticos de referencia (docs/MAPAS_TEMATICOS.md): estado | validar | generar --tema ID."""
    from etl import temas as T
    accion = args.archivo or "estado"
    if accion == "validar":
        try:
            cat = T.cargar_catalogo()
        except ValueError as ex:
            sys.exit(str(ex))
        print(f"{len(cat)} contratos válidos en {T.DIR_TEMAS.name}/")
        return
    cat = T.cargar_catalogo()
    if accion == "estado":
        t = T.estado(cat, ruta(cfg, "out"))
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            print(t.to_string(index=False))
        print(f"\n{len(t)} temas · {int(t.generado.sum())} generados · {int(t.publicable.sum())} publicables (licencia verificada y uso comercial)")
        return
    if accion == "generar":
        if not args.tema:
            sys.exit("Uso: python run.py temas generar --tema <id>")
        import etl.temas_gen  # noqa: F401  (registra los generadores)
        try:
            p = T.generar(args.tema, cat, ruta(cfg, "raw"), ruta(cfg, "out"), cfg)
        except (KeyError, ValueError) as ex:
            sys.exit(str(ex))
        log.info("%s: %s (%.1f MB)", args.tema, p, Path(p).stat().st_size / 1e6)
        return
    sys.exit("Uso: python run.py temas [estado|validar|generar --tema <id>]")


def cmd_predios(cfg, args):
    """Predios del SII (respaldo personal de catastral.cl): `convertir --origen DIR` | `estado`. Ver etl/predios.py."""
    from etl import predios as P
    accion = args.archivo or "estado"
    destino = Path(args.destino) if getattr(args, "destino", None) else ruta(cfg, "predios")
    if accion == "estado":
        m = destino / "manifiesto_predios.csv"
        if not m.exists():
            sys.exit(f"Sin manifiesto en {destino}: corre `python run.py predios convertir --origen <carpeta con los .gpkg>`")
        df = pd.read_csv(m, encoding="utf-8-sig", dtype={"cut": str, "cod_sii": str})
        with pd.option_context("display.width", 220, "display.max_columns", 20):
            print(df.drop(columns=["nombre_en_datos"]).to_string(index=False))
        print(f"\n{len(df)} archivos · {int(df.n.sum()):,} polígonos · {int(df.n_datos_sii.sum()):,} con datos del SII · {int(df.n_huerfanos.sum()):,} huérfanos")
        return
    if accion == "convertir":
        if not args.origen:
            sys.exit("Uso: python run.py predios convertir --origen <carpeta con los GeoPackage>")
        c = cfg["comunas"]
        bcn = gpd.read_file(ruta(cfg, "comunas")).to_crs(4326)
        patron = "*.parquet" if getattr(args, "formato", None) == "parquet" else "*.gpkg"
        m = P.convertir_todo(Path(args.origen), destino, bcn, c["field_cut"], c["field_nombre"], progreso.paso, patron=patron,
                             etiqueta=getattr(args, "etiqueta", None))
        print(m.drop(columns=["nombre_en_datos"], errors="ignore").to_string(index=False))
        print(f"\nManifiesto: {destino / 'manifiesto_predios.csv'} · {int(m.n.sum()):,} polígonos")
        return
    if accion == "completar":
        if not args.origen:
            sys.exit("Uso: python run.py predios completar --origen <carpeta con los parquet del respaldo anterior archivado>")
        r = P.completar_con_archivo(destino, Path(args.origen))
        print(r.to_string(index=False))
        print(f"\n{int(r.roles_conservados.sum()):,} roles conservados del respaldo anterior en {int((r.roles_conservados > 0).sum())} comunas")
        return
    sys.exit("Uso: python run.py predios [estado|convertir --origen DIR [--formato parquet] [--etiqueta X]|completar --origen DIR]")


def cmd_teselas(cfg, args):
    """Teselas PMTiles locales de Regu Suelo en <paths.out>/tiles/: normativa | ocupacion | comunas (por defecto, las tres)."""
    import time
    from etl import teselas as T
    from etl.ficha import ultimo_gpkg
    out = ruta(cfg, "out")
    temas = [args.archivo] if args.archivo else list(T.TEMAS)
    if any(t not in T.TEMAS for t in temas):
        sys.exit(f"Tema desconocido. Uso: python run.py teselas [{'|'.join(T.TEMAS)}]")
    c = cfg["comunas"]
    for i, tema in enumerate(temas, 1):
        progreso.paso("teselas", tema, i, len(temas))
        t0 = time.perf_counter()
        destino = out / "tiles" / f"{tema}.pmtiles"
        if tema == "normativa":
            gpkg = Path(args.gpkg) if args.gpkg else ultimo_gpkg(out)
            if not gpkg:
                sys.exit("No hay GPKG de build (usa --gpkg)")
            T.normativa(gpkg, destino)
        elif tema == "ocupacion":
            g = (sorted(out.glob("ocupacion_nacional_*.gpkg")) or sorted(out.glob("ocupacion_*.gpkg")) or [None])[-1]
            if not g:
                sys.exit("No hay capa de ocupación: corre `python run.py ocupacion`")
            T.ocupacion(g, destino)
        else:
            T.comunas(ruta(cfg, "comunas"), c["field_cut"], c["field_nombre"], c["field_region"], destino)
        log.info("%s: %s (%.1f MB, %.0f s)", tema, destino, destino.stat().st_size / 1e6, time.perf_counter() - t0)


def cmd_volumen(cfg, args):
    """Piloto de volumen (docs/VOLUMEN_PILOTO.md), paso 0: candidatas | footprints | plantilla."""
    from etl import volumen as V
    from etl.ficha import ultimo_gpkg
    accion = args.archivo or "candidatas"
    if not args.ipt:
        sys.exit('Uso: python run.py volumen candidatas|footprints|plantilla --ipt "Temuco" [--region X] [--zona Z]')
    out = ruta(cfg, "out")
    slug = norm_txt(args.ipt).lower().replace(" ", "_")
    dir_fp = ruta(cfg, "footprints")
    if accion == "plantilla":
        if not args.zona:
            sys.exit("Falta --zona (la que elija el arquitecto)")
        df = V.plantilla_normas(ruta(cfg, "normas"), args.ipt, args.zona)
        print(f"{ruta(cfg, 'normas')}: {len(df)} filas; la de {args.ipt} | {args.zona} queda con las normas vacías "
              f"para que las llene el arquitecto (estado FICTICIO | BORRADOR | VALIDADO).")
        return
    if args.gpkg:
        gpkg = Path(args.gpkg)
    elif args.region:
        suf = norm_txt(args.region).lower().replace(" ", "_")
        gpkg = (sorted(out.glob(f"regu_ipt_{suf}_*.gpkg")) or [None])[-1]
    else:
        gpkg = ultimo_gpkg(out)
    if not gpkg or not gpkg.exists():
        sys.exit("No hay GPKG de build (usa --region o --gpkg)")
    if accion == "vcalc":
        # V2 de VOLÚMENES: V_calc de los predios de un JSON guardado desde catastral.cl (--predios), sin nuevas consultas
        from etl import ocupacion as O
        from etl import vcalc as C
        if not args.predios:
            sys.exit("Falta --predios (JSON de predios guardado desde catastral.cl, ver etl/vcalc.py)")
        predios = C.cargar_predios(Path(args.predios))
        zonas = gpd.read_file(gpkg, layer="capa_ipt", where=f"fuente = 'PRC' AND ipt_nombre = '{args.ipt}'")
        fps = O.regiones_con_footprints(dir_fp)
        reg = zonas["region"].dropna().iloc[0] if len(zonas) else None
        if reg not in fps:
            sys.exit(f"Sin footprints completos para la región de {args.ipt} ({reg}): corre `footprints descargar`")
        x0, y0, x1, y1 = predios.total_bounds
        edif = gpd.read_parquet(fps[reg]).cx[x0 - 0.002:x1 + 0.002, y0 - 0.002:y1 + 0.002]
        tabla = C.calcular(predios, edif, zonas=zonas)
        destino = out / f"vcalc_{slug}_piloto.csv"
        tabla.to_csv(destino, index=False, encoding="utf-8-sig")
        with pd.option_context("display.width", 220, "display.max_columns", 30, "display.max_colwidth", 60):
            print(tabla.to_string(index=False))
        log.info("V_calc: %s (%d predios, %d edificios cercanos)", destino, len(tabla), len(edif))
        return
    capa = gpd.read_file(gpkg, layer="capa_ipt", where="ipt_tipo = 'PRC'")
    if accion == "footprints":
        bb = V.bbox_ipt(capa, args.ipt)
        p = V.descargar_footprints(bb, dir_fp, slug)
        log.info("Footprints Overture: %s (%.1f MB) · bbox %s", p, p.stat().st_size / 1e6, bb)
        return
    if accion == "candidatas":
        fp_path = Path(args.footprints) if args.footprints else (sorted(dir_fp.glob(f"overture_building_*_{slug}.parquet")) or [None])[-1]
        fp = gpd.read_parquet(fp_path) if fp_path and fp_path.exists() else None
        if fp is None:
            log.warning("Sin footprints: corre primero `python run.py volumen footprints --ipt %r`", args.ipt)
        pr = gpd.read_parquet(args.predios) if args.predios else None
        mp = gpkg.parent / "vigencia_match.csv"
        match = pd.read_csv(mp, dtype=str, keep_default_na=False, encoding="utf-8-sig") if mp.exists() else None
        t = V.candidatas(capa, args.ipt, fp, pr, match)
        dest = out / f"volumen_candidatas_{slug}.csv"
        t.to_csv(dest, index=False, encoding="utf-8-sig")
        with pd.option_context("display.width", 220, "display.max_colwidth", 45):
            print(t.drop(columns=["ordenanza_url", "cut"]).to_string(index=False))
        print(f"\n{dest}" + (f"\nFootprints: {fp_path.name} ({V.ATRIBUCION_OVERTURE})" if fp is not None else ""))
        return
    sys.exit("Uso: python run.py volumen candidatas|footprints|plantilla --ipt ...")


def cmd_muestra(cfg, args):
    """Muestra liviana y versionable (samples/<nombre>/): comuna(s) + vecinas, sin attrs_raw ni datos de terceros
    (nada de Overture, catastral.cl ni data/demanda). Para que otros agentes (Codex) trabajen sin data/."""
    import shutil
    c = cfg["comunas"]
    if not args.region or not args.cut:
        sys.exit("Uso: python run.py muestra --region ARAUCANIA --cut 09101 [--nombre temuco]")
    suf = norm_txt(args.region).lower().replace(" ", "_")
    out = ruta(cfg, "out")
    gpkg = Path(args.gpkg) if args.gpkg else (sorted(out.glob(f"regu_ipt_{suf}_*.gpkg")) or [None])[-1]
    if not gpkg or not gpkg.exists():
        sys.exit(f"No hay GPKG de la región. Corre primero: python run.py build --region {args.region}")
    com = cargar_comunas(cfg, args.region).to_crs(4326)
    centro = com[com[c["field_cut"]].isin(args.cut.split(","))]
    sel = com[com.intersects(centro.union_all().buffer(1e-4))]          # la(s) comuna(s) y sus vecinas
    cuts = set(sel[c["field_cut"]])
    dest = ROOT / "samples" / (args.nombre or norm_txt(centro[c["field_nombre"]].iloc[0]).lower().replace(" ", "_"))
    dest.mkdir(parents=True, exist_ok=True)
    g = dest / "muestra.gpkg"
    g.unlink(missing_ok=True)
    from etl.mapa import unir_vigencia
    for capa in ("capa_ipt", "afectaciones"):
        d = gpd.read_file(gpkg, layer=capa)
        d = d[d["cut"].isin(cuts)].drop(columns=[x for x in ("attrs_raw",) if x in d])
        if capa == "capa_ipt":   # contrato de docs/SIG_LAYOUTS.md: incluye la vigencia (ipt_norma, ipt_fecha, …)
            d = unir_vigencia(d, gpkg.parent / "vigencia_match.csv")
        d.to_file(g, layer=capa, driver="GPKG")
    sel.rename(columns={c["field_cut"]: "cut", c["field_nombre"]: "comuna", c["field_region"]: "region"}) \
       [["cut", "comuna", "region", "geometry"]].to_file(g, layer="comunas", driver="GPKG")
    pm = out / f"mapa_{suf}" / "capa_ipt.pmtiles"
    if pm.exists() and pm.stat().st_size < 50e6:
        shutil.copy2(pm, dest / f"capa_ipt_{suf}.pmtiles")
    (dest / "README.md").write_text(README_MUESTRA.format(
        comunas=", ".join(sorted(sel[c["field_nombre"]])), gpkg=gpkg.name, region=args.region,
        pmtiles=f"capa_ipt_{suf}.pmtiles" if pm.exists() else "(no generado)", fecha=date.today().isoformat()), encoding="utf-8")
    log.info("Muestra: %s (%.1f MB) · %d comunas: %s", dest, sum(f.stat().st_size for f in dest.iterdir()) / 1e6,
             len(cuts), ", ".join(sorted(sel[c["field_nombre"]])))


README_MUESTRA = """# Muestra {comunas}

Muestra liviana y versionable del Atlas Normativo, generada con `python run.py muestra` el {fecha}
desde `{gpkg}` (build de {region}). Sirve para trabajar sin `data/` (que no se versiona).

- `muestra.gpkg`: capas `capa_ipt` (partición U1…R2), `afectaciones` y `comunas`, en EPSG:4326, recortadas a:
  {comunas}. Sin `attrs_raw`.
- `{pmtiles}`: la partición de toda la región en PMTiles (lo que usa el mapa).

Fuentes: IDE MINVU (geoide.minvu.cl), Portal IPT MINVU y División comunal BCN. **No** incluye datos de
catastral.cl, Overture Maps ni registros de demanda.

**Información referencial.** No reemplaza el Certificado de Informaciones Previas (CIP), que emite solo la
Dirección de Obras Municipales (OGUC art. 1.4.4). `legal_refs.json` está en BORRADOR.
"""


def cmd_footprints(cfg, args):
    """Footprints nacionales de Overture por región (docs/FOOTPRINTS_NACIONAL.md): descargar | estado."""
    from etl import footprints as F
    accion = args.archivo or "estado"
    dir_fp = ruta(cfg, "footprints")
    if accion == "estado":
        t = F.estado(dir_fp)
        with pd.option_context("display.width", 200):
            print(t.to_string(index=False) if len(t) else f"Sin manifiestos todavía en {dir_fp}")
        libre = __import__("shutil").disk_usage(dir_fp if dir_fp.exists() else ROOT).free / 1e9
        print(f"\nTotal: {int(t.edificios.fillna(0).sum()) if len(t) else 0} edificios · "
              f"{t.MB.fillna(0).sum() if len(t) else 0:.1f} MB · {dir_fp} · disco libre {libre:.1f} GB · {F.ATRIBUCION}")
        return
    if accion == "descargar":
        if not args.region:
            sys.exit('Uso: python run.py footprints descargar --region "ARAUCANIA" (regex: "ARICA|TARAPACA")')
        c = cfg["comunas"]
        comunas = cargar_comunas(cfg, None)
        res = F.descargar_regiones(args.region, comunas, c["field_cut"], c["field_nombre"], c["field_region"], dir_fp,
                                   release=args.release, refresh=args.refresh, conservar_crudo=args.conservar_crudo,
                                   dir_manifiestos_repo=ROOT / "docs" / "footprints")
        for m in res:
            log.info("%s: %s edificios · %s MB · %s s%s", m["region"], m["n_edificios"], m["mb"], m.get("duracion_s"),
                     " (ya estaba completa)" if m.get("saltada") else "")
        return
    sys.exit("Uso: python run.py footprints descargar --region X | estado")


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
    ap.add_argument("cmd", choices=["discover", "catalogo", "download", "build", "all", "importar-revision", "ficha",
                                    "mapa", "vigencia", "fuentes", "volumen", "muestra", "footprints", "ocupacion", "teselas", "app", "temas", "predios"])
    ap.add_argument("--release", help="footprints: release de Overture (por defecto, el último)")
    ap.add_argument("--conservar-crudo", action="store_true", help="footprints: no borrar el parquet crudo de Overture")
    ap.add_argument("--cut", help="muestra: CUT(s) centrales separados por coma, p.ej. 09101")
    ap.add_argument("--nombre", help="muestra: carpeta en samples/ (por defecto, la comuna central)")
    ap.add_argument("--ipt", help='volumen: nombre del PRC (ipt_nombre), p.ej. "Temuco"')
    ap.add_argument("--zona", help="volumen plantilla: zona elegida por el arquitecto")
    ap.add_argument("--predios", help="volumen candidatas: GeoParquet catastral local (GEOSAL), opcional")
    ap.add_argument("--footprints", help="volumen candidatas: GeoParquet de footprints (por defecto el de data/base/footprints)")
    ap.add_argument("--lon", type=float, help="ficha: longitud (EPSG:4326)")
    ap.add_argument("--lat", type=float, help="ficha: latitud (EPSG:4326)")
    ap.add_argument("--wkt", help="ficha: geometría WKT en EPSG:4326 (punto o polígono)")
    ap.add_argument("--gpkg", help="ficha: GPKG de build (por defecto el más reciente, preferente nacional)")
    ap.add_argument("archivo", nargs="?", help="importar-revision: CSV con 'decision' llena · fuentes: estado|validar")
    ap.add_argument("--origen", help="predios convertir: carpeta con los GeoPackage de respaldo")
    ap.add_argument("--destino", help="predios: carpeta de destino distinta de paths.predios (p. ej. la del respaldo archivado)")
    ap.add_argument("--etiqueta", help="predios convertir: rótulo del origen de las filas (por defecto, el nombre de la carpeta)")
    ap.add_argument("--formato", choices=["gpkg", "parquet"], default="gpkg", help="predios convertir: formato de los respaldos (por defecto gpkg)")
    ap.add_argument("--tema", help="temas generar: id del tema (temas/<id>.yaml)")
    ap.add_argument("--puerto", type=int, default=8000, help="app: puerto local (por defecto 8000)")
    ap.add_argument("--no-abrir", action="store_true", help="app: no abrir el navegador")
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
    if args.cmd in ("discover", "download", "build", "all") or args.cmd in ("ocupacion", "teselas", "predios") or (args.cmd == "footprints" and args.archivo == "descargar"):
        # convención: procesos largos dejan su avance en data/out/progreso.log (ver etl/progreso.py)
        progreso.iniciar(ruta(cfg, "out") / "progreso.log")
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
    if args.cmd == "vigencia":
        cmd_vigencia(cfg, args)
    if args.cmd == "fuentes":
        cmd_fuentes(cfg, args)
    if args.cmd == "volumen":
        cmd_volumen(cfg, args)
    if args.cmd == "muestra":
        cmd_muestra(cfg, args)
    if args.cmd == "footprints":
        cmd_footprints(cfg, args)
    if args.cmd == "ocupacion":
        cmd_ocupacion(cfg, args)
    if args.cmd == "teselas":
        cmd_teselas(cfg, args)
    if args.cmd == "app":
        cmd_app(cfg, args)
    if args.cmd == "temas":
        cmd_temas(cfg, args)
    if args.cmd == "predios":
        cmd_predios(cfg, args)


if __name__ == "__main__":
    main()
