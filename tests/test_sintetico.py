"""Prueba end-to-end con datos sintéticos (sin red). Ejecutar: python -m pytest tests -q  (o python tests/test_sintetico.py)"""
import json
import re
import sys
import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
import yaml
from shapely.geometry import box, mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etl.arcgis import raw_path  # noqa: E402
from etl.classify import clasificar  # noqa: E402
from etl.export import anotar_legal, escribir  # noqa: E402
from etl.classify import (ampliar_comunas, extension_costera, instrumentos_sin_comuna,  # noqa: E402
                          recortar_afectaciones, umbral_traslape)
from etl.normalize import ComunaResolver, aplicar_reglas, cut_por_cascada, load_layer, separar_afectaciones  # noqa: E402
from etl.revision import generar_revision, importar_revision  # noqa: E402
from etl.ficha import ficha  # noqa: E402
from etl import progreso  # noqa: E402
from etl import raster_tiles  # noqa: E402,F401  (corrige PROJ_LIB/GDAL_DATA antes de que una prueba importe rasterio)

REG = "Región de La Araucanía"


def _fc(feats):
    return {"type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": p, "geometry": mapping(g)} for p, g in feats],
            "_meta": {"fetched_at": "2026-09-26T12:00:00+00:00"}}


def _escenario(tmp: Path):
    comunas = gpd.GeoDataFrame({
        "CUT_COM": ["09101", "09112", "09102"],
        "COMUNA": ["Temuco", "Padre Las Casas", "Carahue"],
        "REGION": [REG] * 3,
    }, geometry=[box(-72.7, -38.8, -72.5, -38.6), box(-72.5, -38.8, -72.3, -38.6),
                 box(-72.9, -38.8, -72.7, -38.6)], crs=4326)

    capas = {
        ("IPT/PRC_Araucania", 0, "PRC_Temuco"): [
            ({"ZONA": "ZU-1", "DESCRIPCION": "Zona urbana central"}, box(-72.65, -38.75, -72.58, -38.68)),
            ({"ZONA": "ZU-2", "DESCRIPCION": "Zona mixta"}, box(-72.60, -38.72, -72.48, -38.66)),  # traslapa ZU-1 y desborda a PLC
            ({"ZONA": "ZU-3", "DESCRIPCION": "Zona contenida"}, box(-72.64, -38.74, -72.62, -38.72)),  # contenida en ZU-1
            ({"ZONA": "AR-2", "DESCRIPCION": "Área de riesgo por remoción en masa"}, box(-72.59, -38.71, -72.585, -38.705)),
            # zona que sale de TODAS las comunas (como un PRC costero con mejor línea de costa que la DPA)
            ({"ZONA": "ZU-4", "DESCRIPCION": "Borde costero"}, box(-72.60, -38.62, -72.55, -38.58)),
        ],
        # Mismo LU publicado en Limites_Urbanos y en PRC_Araucania (geometría idéntica)
        # COM con error de tipeo (como en MINVU): se resuelve en cascada por ADMIN
        ("IPT/Limites_Urbanos", 0, "Limites_Urbanos_PRC"): [
            ({"COM": "Padre de Las Casas", "ADMIN": "Municipalidad de Padre Las Casas",
              "NOM": "Límite urbano de Padre Las Casas"}, box(-72.49, -38.76, -72.42, -38.70)),
        ],
        ("IPT/PRC_Araucania", 1, "Seccional_Temuco_Labranza"): [
            ({"ZONA": "ZS-A"}, box(-72.69, -38.70, -72.63, -38.65)),
        ],
        ("IPT/PRC_Araucania", 2, "Limite_Urbano"): [
            ({"COMUNA": "Padre Las Casas"}, box(-72.49, -38.76, -72.42, -38.70)),
        ],
        ("IPT/PRI_Araucania", 0, "PRI_Temuco_PLC"): [
            ({"ZONA": "ZEU-1", "DESCRIPCION": "Zona de extensión urbana"}, box(-72.70, -38.80, -72.60, -38.76)),
            ({"ZONA": "ZR-2", "DESCRIPCION": "Zona rural silvoagropecuaria"}, box(-72.9, -38.8, -72.8, -38.7)),
            ({"ZONA": "ZR-3", "DESCRIPCION": "Zona rural de protección"}, box(-72.85, -38.75, -72.75, -38.65)),  # traslapa ZR-2
            ({"ZONA": "ZX", "DESCRIPCION": "Zona especial"}, box(-72.45, -38.66, -72.35, -38.62)),
            ({"ZONA": "Riesgo aluvión", "DESCRIPCION": None}, box(-72.88, -38.79, -72.86, -38.77)),  # dentro de ZR-2
        ],
        # Contorno del área rural PRI (envolvente): no debe tapar la zonificación PRI de Carahue
        ("IPT/PRI_Area_Rural", 0, "Límite área rural PRI"): [
            ({"ZONA": "UNIDAD TERRITORIAL C"}, box(-72.9, -38.8, -72.72, -38.62)),
        ],
        ("IPT/PRC_Araucania", 3, "PRC_Temuco_Riesgo"): [
            ({"ZONA": "AR-1", "DESCRIPCION": "Riesgo inundación"}, box(-72.62, -38.74, -72.60, -38.72)),
            # riesgo del PRC de Temuco que se desborda a Padre Las Casas: solo afecta a Temuco
            ({"ZONA": "AR-3", "DESCRIPCION": "Riesgo inundación"}, box(-72.52, -38.78, -72.47, -38.765)),
        ],
        # Capa que mezcla protección (APP, no es riesgo) con riesgo (ARRI, riesgo por el nombre de la capa)
        ("IPT/PRC_Araucania", 4, "PRC_Temuco_Areas_de_proteccion_y_riesgo"): [
            ({"ZONA": "APP 1"}, box(-72.57, -38.70, -72.55, -38.68)),
            ({"ZONA": "ARRI"}, box(-72.57, -38.72, -72.55, -38.705)),
        ],
    }
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw = tmp / "raw"
    catalogo = []
    for (srv, lid, nombre), feats in capas.items():
        e = {"service": srv, "layer_id": lid, "layer_name": nombre, "url": f"https://x/{srv}/MapServer/{lid}"}
        e = aplicar_reglas(e, cfg)   # override > service_rules > layer_rules, como en el catálogo real
        p = raw_path(raw, e)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_fc(feats)), encoding="utf-8")
        catalogo.append((e, p))
    return comunas, catalogo, cfg


def test_end_to_end():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        comunas, catalogo, cfg = _escenario(tmp)
        tipos = {e["layer_name"]: e["tipo"] for e, _ in catalogo}
        assert tipos == {"PRC_Temuco": "PRC", "Seccional_Temuco_Labranza": "SECCIONAL",
                         "Limite_Urbano": "LU", "Limites_Urbanos_PRC": "LU",
                         "PRI_Temuco_PLC": "PRI", "Límite área rural PRI": "PRI", "PRC_Temuco_Riesgo": "AFECTACION",
                         "PRC_Temuco_Areas_de_proteccion_y_riesgo": "AFECTACION"}

        # layer_rules_prioritarias ganan sobre service_rules, salvo servicios IGNORAR
        tipo = lambda s, n: aplicar_reglas({"service": s, "layer_id": 0, "layer_name": n}, cfg)["tipo"]  # noqa: E731
        assert tipo("IPT/PRMS", "PRMS_Riesgo_Quebradas") == "AFECTACION"
        assert tipo("IPT/PRI_Valparaiso", "Vialida estructurante") == "AFECTACION"
        assert tipo("IPT/PRI_Valparaiso", "Límite Urbano") == "LU"
        assert tipo("IPT/PRMS", "PRMS_USO_Suelo") == "PRM"
        assert tipo("IPT/PRC_Nuble", "PRC_Bulnes_Area_de_riesgo") == "IGNORAR"
        # override con pri_default (gana sobre layer_rules_prioritarias, que diría LU)
        e_ov = aplicar_reglas({"service": "IPT/PRI_Valparaiso", "layer_id": 6, "layer_name": "Límite Urbano"}, cfg)
        assert (e_ov["tipo"], e_ov["pri_default"]) == ("PRI", "E")

        res = ComunaResolver(comunas, "CUT_COM", "COMUNA")
        assert res.resolve("Temuco Labranza") == "09101"
        assert res.resolve("Padre_Las_Casas") == "09112"
        # Cascada COM → ADMIN → NOM → comuna_alias; ADMIN/NOM solo si traen el prefijo esperado
        cas = lambda props, alias=None: cut_por_cascada(props, "COM", {**cfg, "comuna_alias": alias or {}}, res)  # noqa: E731
        assert cas({"COM": "Temuco"}) == "09101"
        assert cas({"COM": "Padre de Las Casas", "ADMIN": "Municipalidad de Padre Las Casas"}) == "09112"
        assert cas({"COM": "Tmuco", "NOM": "Límite urbano de Temuco"}) == "09101"
        assert cas({"COM": "Tmuco", "NOM": "Temuco centro"}) is None
        assert cas({"COM": "Tmuco"}, {"Tmuco": "Temuco"}) == "09101"

        fuentes, afect = [], []
        for e, p in catalogo:
            part, af = separar_afectaciones(load_layer(p, e, cfg, res), e["tipo"])
            if part is not None:
                fuentes.append(part)
            if af is not None:
                afect.append(af)
        import pandas as pd
        fuentes = gpd.GeoDataFrame(pd.concat(fuentes, ignore_index=True), crs=4326)
        assert set(fuentes["fuente"]) == {"PRC", "SECCIONAL", "LU", "PRI_E", "PRI_R", "PRI_ENV"}
        assert fuentes.loc[fuentes.zona == "ZX", "revisar"].item() is True
        # Zonas de RIESGO dentro de PRC y PRI: siguen en la partición con riesgo=True y se copian a afectaciones
        zonas_af = set(pd.concat(afect).zona)
        assert {"AR-2", "Riesgo aluvión"} <= zonas_af
        assert fuentes.loc[fuentes.zona.isin(["AR-2", "Riesgo aluvión"]), "riesgo"].tolist() == [True, True]

        # zone_overrides gana sobre pri_subclase; AFECTACION saca la zona de la partición
        e_pri, p_pri = next((e, p) for e, p in catalogo if e["layer_name"] == "PRI_Temuco_PLC")
        cfg_zo = {**cfg, "zone_overrides": {"Temuco PLC|ZX": "E", "Temuco_PLC|zr-3": "AFECTACION"}}
        part, af = separar_afectaciones(load_layer(p_pri, e_pri, cfg_zo, res), e_pri["tipo"])
        zx = part[part.zona == "ZX"].iloc[0]
        assert zx["fuente"] == "PRI_E" and not zx["revisar"]
        assert set(af.zona) == {"ZR-3", "Riesgo aluvión"} and "ZR-3" not in set(part.zona)

        assert set(fuentes.loc[fuentes.ipt_tipo == "LU", "cut_ipt"]) == {"09112"}
        assert instrumentos_sin_comuna(fuentes, comunas, "COMUNA").empty
        # QA de pérdida costera: solo la parte de ZU-4 al norte de -38.6 queda fuera de todas las comunas
        fd, ext = extension_costera(fuentes, comunas, comunas, cfg, "CUT_COM", "COMUNA", "REGION")
        esperado = gpd.GeoSeries([box(-72.60, -38.60, -72.55, -38.58)], crs=4326).to_crs(cfg["crs"]["area"]).area[0] / 1e4
        # (tolerancia 1%: las aristas de las cajas reproyectadas no siguen exactamente el paralelo -38,6)
        assert fd.cut.tolist() == ["09101"] and abs(fd.ha_fuera.iloc[0] / esperado - 1) < 0.01, (fd.to_dict("records"), esperado)
        # Extensión costera: Temuco se amplía con la huella de su PRC fuera de la DPA; el resto de comunas no cambia
        comunas_bcn = comunas
        comunas = ampliar_comunas(comunas_bcn, ext, cfg, "CUT_COM")
        assert list(ext) == ["09101"] and abs(comunas.set_index("CUT_COM").ha_extension_costera["09101"] / esperado - 1) < 0.01

        afect_todas = gpd.GeoDataFrame(pd.concat(afect, ignore_index=True), crs=4326)
        riesgos = afect_todas[afect_todas.riesgo.astype(bool)]
        # capas AFECTACION de riesgo + zonas de riesgo de PRC y PRI; APP (protección) excluida aunque la capa sea de riesgo
        assert {"AR-1", "AR-2", "AR-3", "Riesgo aluvión", "ARRI"} == set(riesgos.zona)
        assert "APP 1" in set(afect_todas.zona)
        # Afectaciones de PRC llevan CUT (por nombre de capa) y solo se aplican en su comuna
        assert set(afect_todas.loc[afect_todas.capa == "PRC_Temuco_Riesgo", "cut_ipt"]) == {"09101"}
        assert afect_todas.loc[afect_todas.zona == "Riesgo aluvión", "cut_ipt"].isna().all()   # PRI: sin comuna
        afect_rec = recortar_afectaciones(afect_todas, comunas, cfg, "CUT_COM", "COMUNA")
        assert afect_rec.loc[afect_rec.zona == "AR-3", "cut"].tolist() == ["09101"]   # el desborde a PLC se descarta
        riesgos_rec = afect_rec[afect_rec.riesgo.astype(bool)]
        progreso.iniciar(tmp / "out" / "progreso.log")
        capa, qas = clasificar(comunas, fuentes, cfg, "CUT_COM", "COMUNA", "REGION", riesgos_rec)
        # Convención de avance: una línea por comuna "HH:MM:SS región comuna i/total"
        lineas = (tmp / "out" / "progreso.log").read_text(encoding="utf-8").splitlines()
        assert [re.fullmatch(rf"\d\d:\d\d:\d\d {REG} (.+) (\d)/3", x).group(1, 2) for x in lineas] == \
            [("Temuco", "1"), ("Padre Las Casas", "2"), ("Carahue", "3")], lineas
        progreso.iniciar(None)

        # 1. Cobertura de 100% ± 0,01 en TODAS las comunas y sin traslapes
        #    ('intersects' y no 'overlaps': este último no ve contención ni igualdad)
        assert capa.is_valid.all()
        for qa in qas:
            assert qa["particion_rescates"] == 0 and qa["riesgo_rescates"] == 0, qa
            assert abs(qa["cobertura_pct"] - 100) <= 0.01, qa
            assert qa["traslape_m2"] < umbral_traslape(qa["area_comuna_m2"]) and qa["traslape_ok"], qa
        geoms = list(capa.geometry)
        tree = shapely.STRtree(geoms)
        ovl = [(i, j) for i, j in zip(*tree.query(geoms, predicate="intersects")) if i < j
               and geoms[i].intersection(geoms[j]).area > 1.0]
        assert not ovl, ovl

        q = {x["cut"]: x for x in qas}
        # 2. Temuco: seccional, PRC, extensión y rural remanente
        tem = set(capa[capa.cut == "09101"].clase)
        assert {"U1", "E", "R2"} <= tem
        assert "SECCIONAL:Temuco Labranza" in q["09101"]["instrumentos"]
        # 3. El desborde del PRC de Temuco NO norma Padre Las Casas
        plc = capa[capa.cut == "09112"]
        assert not ((plc.clase == "U1") & (plc.ipt_nombre == "Temuco")).any()
        assert "U2" in set(plc.clase)
        # 4. Carahue: rural PRI + rural sin IPT; sin urbano
        assert {"R1", "R2"} == set(capa[capa.cut == "09102"].clase)
        assert q["09102"]["sin_urbano"] is True
        # 5. Riesgo como superposición, no como competidor: la zona base conserva clase y nombre,
        #    y la parte que cae dentro del riesgo sale con riesgo=True (nunca R2)
        rg = riesgos.to_crs(capa.crs).set_index("zona").geometry
        assert capa.riesgo.notna().all()
        for z, base in (("AR-2", {"ZU-1", "ZU-2"}), ("AR-1", {"ZU-1", "ZU-2"}), ("Riesgo aluvión", {"ZR-2"})):
            toca = capa[capa.intersection(rg[z]).area > 1.0]
            assert set(toca.zona) <= base and toca.riesgo.all(), (z, toca[["zona", "clase", "riesgo"]])
            assert abs(toca.intersection(rg[z]).area.sum() - rg[z].area) < 1.0
        assert "U1" in set(capa[capa.intersection(rg["AR-2"]).area > 1.0].clase)
        assert not {"AR-2", "Riesgo aluvión"} & set(capa.zona)
        assert not capa[capa.zona == "ZEU-1"].riesgo.any()
        app = afect_todas.to_crs(capa.crs).set_index("zona").geometry["APP 1"]
        assert not capa[capa.intersection(app).area > 1.0].riesgo.any()
        # El riesgo del PRC de Temuco marca Temuco, no Padre Las Casas
        toca_ar3 = capa[capa.intersection(rg["AR-3"]).area > 1.0]
        assert toca_ar3[toca_ar3.cut == "09101"].riesgo.all() and (toca_ar3.cut == "09101").any()
        assert not toca_ar3[toca_ar3.cut == "09112"].riesgo.any() and (toca_ar3.cut == "09112").any()
        # Zona de riesgo contenida en una zona PRI con subclase explícita: ZR-2 conserva clase y área total
        car = capa[capa.cut == "09102"]
        zr2 = car[car.zona == "ZR-2"]
        assert set(zr2.fuente) == {"PRI_R"} and set(zr2.clase) == {"R1"}
        assert abs(zr2.area_m2.sum() - 96502624.8) < 1
        assert sorted(zr2.riesgo.tolist()) == [False, True]
        # 6. La envolvente PRI solo llena lo que la zonificación PRI no cubre
        assert "PRI_ENV" in set(car.fuente) and not car[car.fuente == "PRI_ENV"].revisar.any()

        legal = json.loads((ROOT / "legal_refs.json").read_text(encoding="utf-8"))
        capa = anotar_legal(capa, legal)
        # Afectaciones recortadas a las comunas procesadas y con cut: solo Temuco, sin Carahue
        afect_gdf = recortar_afectaciones(afect_todas, comunas[comunas.CUT_COM != "09102"], cfg, "CUT_COM", "COMUNA")
        assert sorted(zip(afect_gdf.zona, afect_gdf.cut)) == sorted([
            ("AR-1", "09101"), ("AR-2", "09101"), ("AR-3", "09101"), ("APP 1", "09101"), ("ARRI", "09101")])
        prod = escribir(capa, afect_gdf, qas, tmp / "out", cfg, "test")
        assert Path(prod["geojson"]).exists() and Path(prod["gpkg"]).exists()
        assert prod["resumen"]["piezas_invalidas"] == 0

        # ficha(): punto → una sola clase; polígono U1+R2 → % suman 100; riesgo; fuera de cobertura
        from shapely.geometry import Point
        f = ficha(Point(-72.52, -38.69), prod["gpkg"])
        assert f["cut"] == "09101" and [(p["clase"], p["zona"], p["pct"]) for p in f["particion"]] == [("U1", "ZU-2", 100.0)]
        assert f["riesgo_pct"] == 0 and not f["fuera_de_cobertura"] and f["condicionantes"] == [] and f["aviso"]
        f = ficha(Point(-72.5875, -38.7075), prod["gpkg"])            # dentro de AR-2 (riesgo en el PRC)
        assert f["particion"][0]["clase"] == "U1" and f["riesgo_pct"] == 100.0
        assert "AR-2" in {a["zona"] for a in f["afectaciones"]}
        f = ficha(box(-72.53, -38.75, -72.51, -38.70), prod["gpkg"])  # cruza ZU-2 (U1) y R2
        assert {p["clase"] for p in f["particion"]} == {"U1", "R2"}
        assert abs(sum(p["pct"] for p in f["particion"]) - 100) <= 0.01, f["particion"]
        f = ficha(Point(-72.8, -38.5), prod["gpkg"])                   # mar: ninguna comuna ni extensión
        assert f["cobertura"] == "fuera_dpa" and f["fuera_de_cobertura"] is True and "DPA" in f["motivo"]
        f = ficha(Point(-72.575, -38.59), prod["gpkg"])                # extensión costera de Temuco (fuera de la BCN)
        assert f["cobertura"] == "completa" and f["cut"] == "09101"
        assert [(p["clase"], p["zona"]) for p in f["particion"]] == [("U1", "ZU-4")]
        assert q["09101"]["ha_extension_costera"] > 0 and q["09102"]["ha_extension_costera"] == 0

        # Demanda (FUENTES_BAJO_DEMANDA.md): cada ficha deja una línea en <tmp>/demanda/consultas.jsonl, SIN coordenadas
        reg = (tmp / "demanda" / "consultas.jsonl").read_text(encoding="utf-8").splitlines()
        lineas = [json.loads(x) for x in reg]
        assert len(lineas) == 5, len(lineas)                           # las 5 fichas de arriba
        assert all(set(x) == {"fecha", "region", "cut", "comuna", "clase", "cobertura", "fuentes_relevantes_no_activas"}
                   for x in lineas)
        assert not any(re.search(r"-7[0-9]\.\d{3}|-3[0-9]\.\d{3}", x) for x in reg)   # ninguna coordenada
        cruce = next(x for x in lineas if x["cut"] == "09101" and "conadi_tierras_indigenas" in x["fuentes_relevantes_no_activas"])
        assert "mma_humedales_urbanos" in cruce["fuentes_relevantes_no_activas"]   # el polígono U1+R2 toca ambas
        mar = next(x for x in lineas if x["cobertura"] == "fuera_dpa")
        assert mar["fuentes_relevantes_no_activas"] == [] and mar["cut"] is None
        fp = ficha(Point(-72.52, -38.69), prod["gpkg"], registrar=False)
        assert {x["id"] for x in fp["fuentes_pendientes"]} == {"conadi_adi", "mma_humedales_urbanos"}   # U1 en Temuco
        assert len((tmp / "demanda" / "consultas.jsonl").read_text(encoding="utf-8").splitlines()) == 5  # registrar=False
        from etl.fuentes import cargar_contratos, estado
        contratos = cargar_contratos()
        assert len(contratos) == 6 and {c["estado"] for c in contratos.values()} == {"propuesta"}   # ninguna activa
        est = estado(contratos, tmp / "demanda").set_index("fuente")
        assert est.loc["conadi_tierras_indigenas", "votos_por_region"].startswith(REG)
        assert not est.lista_para_activar.any()                        # propuesta: nunca lista, aunque haya votos

        # vigencia (Portal IPT) en ficha y mapa, vía vigencia_match.csv junto al GPKG
        assert f["particion"][0]["vigencia"] is None                   # sin match: no se inventa
        pd.DataFrame([{"ipt_tipo": "PRC", "ipt_nombre": "Temuco", "cut": "09101", "portal_id": 1055, "portal_tipo": "PRC",
                       "denominacion": "Plan Regulador Comunal de Temuco-Labranza", "norma": "Resolución N° 149",
                       "fecha_vigencia": "2010-02-02", "ultima_modificacion": "2012-07-07", "n_modificaciones": 2,
                       "ordenanza_url": "https://instrumentosdeplanificacion.minvu.cl/x.pdf", "score": 0.5,
                       "confianza": "alta"}]).to_csv(Path(prod["gpkg"]).parent / "vigencia_match.csv", index=False,
                                                     encoding="utf-8-sig")
        v = ficha(Point(-72.52, -38.69), prod["gpkg"])["particion"][0]["vigencia"]
        assert v["norma"] == "Resolución N° 149" and v["fecha_vigencia"] == "2010-02-02" and v["ordenanza_url"]

        # mapa: PMTiles con los campos de la ficha (el HTML necesita red para MapLibre y no se prueba aquí)
        from etl.mapa import CAMPOS, generar_pmtiles, unir_vigencia
        capa_v = unir_vigencia(gpd.read_file(prod["gpkg"], layer="capa_ipt"), Path(prod["gpkg"]).parent / "vigencia_match.csv")
        pm = generar_pmtiles(capa_v, tmp / "out" / "capa.pmtiles", 6, 10)
        leido = gpd.read_file(pm, layer="ipt")
        assert set(leido.loc[leido.ipt_nombre == "Temuco", "ipt_norma"]) == {"Resolución N° 149"}
        assert pm.stat().st_size > 0 and len(leido) > 0 and set(CAMPOS) - {"cut"} <= set(leido.columns)
        assert "Límite Urbano Padre Las Casas" in set(leido.ipt_nombre)   # textos en UTF-8 (no cp1252)
        assert gpd.read_file(prod["gpkg"], layer="capa_ipt").is_valid.all()
        gj = json.loads(Path(prod["geojson"]).read_text(encoding="utf-8"))
        assert gj["features"][0]["properties"]["norma_titulo"]
        print(json.dumps(prod["resumen"], ensure_ascii=False, indent=1))

        # Revisión del arquitecto: CSV de zonas revisar=True → importar-revision → zone_overrides
        csv_rev = tmp / "out" / "revision_arquitecto.csv"
        rev = generar_revision(capa, csv_rev)
        assert list(rev.columns) == ["region", "ipt_nombre", "zona", "zona_desc", "comunas", "ha",
                                     "subclase_actual", "decision"]
        assert rev[["region", "ipt_nombre", "zona", "comunas", "subclase_actual"]].values.tolist() == \
            [[REG, "Temuco PLC", "ZX", "Padre Las Casas", "R"]]
        # orden: región y luego ha descendente
        falsa = pd.DataFrame({"region": ["B", "A", "A"], "ipt_nombre": ["x", "y", "z"], "zona": ["1", "2", "3"],
                              "zona_desc": None, "comuna": ["c1", "c2", "c3"], "area_m2": [5e4, 1e4, 9e4],
                              "fuente": "PRI_R", "revisar": True})
        orden = generar_revision(falsa, tmp / "orden.csv")
        assert orden[["region", "zona", "ha"]].values.tolist() == [["A", "3", 9.0], ["A", "2", 1.0], ["B", "1", 5.0]]
        rev.loc[0, "decision"] = "e"
        rev.to_csv(csv_rev, index=False, encoding="utf-8-sig")
        assert generar_revision(capa, csv_rev).loc[0, "decision"] == "e"   # rebuild no borra decisiones
        cfg_tmp = tmp / "config.yaml"
        cfg_tmp.write_text((ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        previos = cfg.get("zone_overrides") or {}
        fusion = importar_revision(csv_rev, cfg_tmp)
        assert fusion == {**previos, "Temuco PLC|ZX": "E"}   # se fusiona con los overrides existentes
        cfg2 = yaml.safe_load(cfg_tmp.read_text(encoding="utf-8"))
        assert cfg2["zone_overrides"] == fusion and cfg2["pri_subclase"] == cfg["pri_subclase"]
        assert cfg2["campo_zona"] == cfg["campo_zona"]       # el resto del config.yaml queda intacto
        e_pri, p_pri = next((e, p) for e, p in catalogo if e["layer_name"] == "PRI_Temuco_PLC")
        zx = load_layer(p_pri, e_pri, cfg2, res).set_index("zona").loc["ZX"]
        assert zx["fuente"] == "PRI_E" and not zx["revisar"]
        rev.loc[0, "decision"] = "X"
        rev.to_csv(csv_rev, index=False, encoding="utf-8-sig")
        try:
            importar_revision(csv_rev, cfg_tmp)
            raise AssertionError("debió rechazar decision inválida")
        except ValueError:
            pass
        print(capa[["id", "comuna", "clase", "fuente", "ipt_nombre", "zona", "area_m2"]].to_string())




def test_vigencia():
    """Cruce Portal IPT ↔ servidor sin red: familias (origen + modificaciones), tabla y emparejamiento por nombre."""
    import pandas as pd
    from etl.vigencia import emparejar, familias, resumen_brechas, tabla_vigencia
    doc = lambda n, t="Publicación D. O. con Ordenanza": {"tipo": t, "nombre": n, "url": f"https://x/{n}.pdf"}  # noqa: E731
    vig = [
        {"id": 1, "tipo": "PRC", "planificacion": "Comunal", "denominacion": "Plan Regulador Comunal de Temuco-Labranza",
         "comunas": ["09101"], "clasificacion": "Instrumento de origen", "numeroDocumento": "149",
         "fechaInicioVigencia": "2010-02-02", "documentos": [doc("Publicación D.O. con Ordenanza - Resolución N° 149")],
         "instrumentosDescendientesIds": "3, 4"},
        {"id": 2, "tipo": "PRC", "planificacion": "Comunal", "denominacion": "Aprueba Plano Regulador y límite urbano de Cajón",
         "comunas": ["09101"], "clasificacion": "Instrumento de origen", "numeroDocumento": "540",
         "fechaInicioVigencia": "1966-10-18", "documentos": [doc("Plano - Decreto N° 540", "Plano")],
         "instrumentosDescendientesIds": ""},
        {"id": 3, "tipo": "PRC", "planificacion": "Comunal", "denominacion": "Modificación Temuco", "comunas": ["09101"],
         "clasificacion": "Modificación", "numeroDocumento": "1462", "fechaInicioVigencia": "2011-06-26",
         "documentos": [doc("Publicación D.O. con Ordenanza - Decreto N° 1462")], "instrumentosDescendientesIds": ""},
        {"id": 5, "tipo": "LU", "planificacion": "Comunal", "denominacion": "Límite urbano de Lumaco", "comunas": ["09207"],
         "clasificacion": "Instrumento de origen", "numeroDocumento": "12", "fechaInicioVigencia": "1939-01-31",
         "documentos": [], "instrumentosDescendientesIds": ""},
        {"id": 6, "tipo": "PRDU", "planificacion": "Intercomunal", "denominacion": "PRDU", "comunas": ["09207"],
         "clasificacion": "Instrumento de origen", "documentos": [], "instrumentosDescendientesIds": ""},
    ]
    fams = familias(vig)
    f1 = next(f for f in fams if f["id"] == 1)
    assert {f["id"] for f in fams} == {1, 2, 5}                           # modificación dentro de su familia; sin PRDU
    assert f1["norma"] == "Resolución N° 149" and f1["ultima_modificacion"] == "2011-06-26" and f1["n_modificaciones"] == 1
    assert f1["ordenanza_url"].endswith("149.pdf")
    assert next(f for f in fams if f["id"] == 2)["norma"] == "Decreto N° 540"
    serv = pd.DataFrame({"cut": ["09101", "09101", "09101", "09112"], "ipt_tipo": ["PRC", "PRC", "LU", "LU"],
                         "ipt_nombre": ["Temuco", "Temuco Cajon", "Límite Urbano Temuco", "Límite Urbano Padre Las Casas"]})
    comunas = pd.DataFrame({"cut": ["09101", "09112", "09207"], "comuna": ["Temuco", "Padre Las Casas", "Lumaco"],
                            "region": ["Araucanía"] * 3})
    t = tabla_vigencia(fams, serv, comunas).set_index(["comuna", "tipo"])
    assert t.loc[("Temuco", "PRC"), "estado"] == "ambos"
    assert t.loc[("Temuco", "LU"), "estado"] == "ambos" and t.loc[("Temuco", "LU"), "nota"]   # LU definido por el PRC
    assert t.loc[("Lumaco", "LU"), "estado"] == "solo_portal"
    assert t.loc[("Padre Las Casas", "LU"), "estado"] == "solo_servidor"
    assert "Lumaco (LU)" in resumen_brechas(t.reset_index())["comunas_con_ipt_comunal_sin_geometria"]
    m = emparejar(serv, fams).set_index("ipt_nombre")
    assert m.loc["Temuco", "portal_id"] == 1 and m.loc["Temuco Cajon", "portal_id"] == 2    # Jaccard desempata
    assert "Límite Urbano Padre Las Casas" not in m.index                                     # sin candidato: no inventa


def _contrato_ejemplo(**cambios):
    c = {"id": "conadi_tierras_indigenas", "nombre": "Tierras indígenas", "institucion": "CONADI",
         "estado": "propuesta", "responsable_contacto": None, "obtenido_via": None,
         "acceso": {"tipo": None, "url": None, "frecuencia_refresco": None}, "licencia": None,
         "escala": None, "fecha_datos": None, "diccionario": [], "mapeo": [],
         "excluir_siempre": ["RUT", "NOMBRE", "NOMBRE_TITULAR", "DIRECCION", "TELEFONO"],
         "condicionante": {"nivel": "restriccion", "efecto": "BORRADOR: Ley 19.253."},
         "demanda": {"preguntas_que_responde": ["¿Mi terreno es tierra indígena?"], "umbral_activacion": 25,
                     "relevante_si": {"clases": ["R1", "R2"], "regiones_cut": ["08", "09", "14", "10"]}}}
    for k, v in cambios.items():
        c[k] = v
    return c


def test_fuentes_contratos():
    """Contratos de fuentes bajo demanda: esquema + reglas por estado; los datos personales nunca se mapean."""
    from etl.fuentes import validar
    assert validar(_contrato_ejemplo()) == []
    e = validar(_contrato_ejemplo(estado="mapeada", licencia="CC BY 4.0",
                                  diccionario=[{"campo": "RUT"}, {"campo": "COMUNIDAD"}],
                                  mapeo=[{"origen": "rut", "destino": "condicionante_detalle"}]))
    assert any("dato personal" in x for x in e), e
    assert any("latente exige acceso" in x for x in validar(_contrato_ejemplo(
        estado="latente", licencia="x", diccionario=[{"campo": "A"}],
        mapeo=[{"origen": "A", "destino": "condicionante_detalle"}])))
    assert validar(_contrato_ejemplo(condicionante={"nivel": "restriccion", "efecto": "Ley 19.253"}))   # sin BORRADOR
    assert validar(_contrato_ejemplo(estado="aprobada"))                                              # estado inválido
    assert any("excluir_siempre" in x for x in validar(_contrato_ejemplo(excluir_siempre=["TELEFONO"])))


def test_volumen_paso0():
    """Piloto de volumen, paso 0: candidatas por zona y plantilla de normas (vacía, sin inventar valores)."""
    import pandas as pd
    from etl.volumen import COLUMNAS_NORMAS, candidatas, plantilla_normas
    capa = gpd.GeoDataFrame({
        "ipt_tipo": ["PRC", "PRC", "PRC", "LU"], "ipt_nombre": ["Temuco", "Temuco", "Temuco", "Límite Urbano Temuco"],
        "zona": ["ZH-1", "ZH-1", "ZC", None], "zona_desc": ["Zona habitacional mixta", None, "Zona centro", None],
        "riesgo": [False, True, False, False], "cut": ["09101"] * 4},
        geometry=[box(-72.60, -38.74, -72.59, -38.73), box(-72.59, -38.74, -72.58, -38.73),
                  box(-72.60, -38.73, -72.59, -38.72), box(-72.7, -38.8, -72.5, -38.6)], crs=4326)
    fp = gpd.GeoDataFrame({"height": [6.0, None, None]},
                          geometry=[box(-72.5951, -38.7351, -72.5949, -38.7349), box(-72.5851, -38.7351, -72.5849, -38.7349),
                                    box(-72.5951, -38.7251, -72.5949, -38.7249)], crs=4326)
    match = pd.DataFrame([{"ipt_tipo": "PRC", "ipt_nombre": "Temuco", "cut": "09101", "ordenanza_url": "https://x/o.pdf"}])
    t = candidatas(capa, "temuco", fp, None, match).set_index("zona")
    assert list(t.index) == ["ZH-1", "ZC"]                        # residencial primero
    assert t.loc["ZH-1", "n_footprints"] == 2 and t.loc["ZC", "n_footprints"] == 1
    assert t.loc["ZH-1", "pct_con_altura"] == 50.0 and t.loc["ZH-1", "residencial"] and not t.loc["ZC", "residencial"]
    assert t.loc["ZH-1", "n_predios"] == "sin datos" and t.loc["ZH-1", "ordenanza_portal"] == "sí"
    assert 45 < t.loc["ZH-1", "pct_riesgo"] < 55                     # la mitad de ZH-1 es riesgo
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "normas_zona.csv"
        plantilla_normas(p, "Temuco", "ZH-1")
        df = plantilla_normas(p, "TEMUCO", "zh-1")                 # no duplica
        assert list(df.columns) == COLUMNAS_NORMAS and len(df) == 1
        assert (df.drop(columns=["ipt_nombre", "zona"]).iloc[0] == "").all()   # normas vacías: no se inventan


def test_footprints_sintetico():
    """Footprints por región (docs/FOOTPRINTS_NACIONAL.md, S0) sin red: recorte contra la DPA, asignación a la
    comuna de mayor área sin partir, borde regional sin duplicados, columnas mínimas, manifiesto y reanudación."""
    import pandas as pd
    from etl import footprints as F
    comunas = gpd.GeoDataFrame({"cut": ["01001", "01002", "02001"], "nombre": ["Uno", "Dos", "Tres"],
                                "reg": ["Región Alfa", "Región Alfa", "Región Beta"]},
                               geometry=[box(-72.10, -38.10, -72.00, -38.00), box(-72.00, -38.10, -71.90, -38.00),
                                         box(-71.90, -38.10, -71.80, -38.00)], crs=4326)
    src = lambda d: [{"property": "", "dataset": d, "record_id": "x", "confidence": None}]  # noqa: E731
    crudo = gpd.GeoDataFrame({
        "id": ["b1", "b2", "b3", "b4", "b5"],
        "names": [{"primary": "Nombre que no se toma"}, None, None, None, None],
        "sources": [src("OpenStreetMap"), src("Microsoft ML Buildings"), src("OpenStreetMap"), src("OpenStreetMap"),
                    src("OpenStreetMap")],
        "height": [6.0, None, None, None, None], "num_floors": [2, None, None, None, None],
        "class": ["house", None, None, None, None], "subtype": ["residential", None, None, None, None],
        "roof_color": ["red", None, None, None, None]},
        geometry=[box(-72.05, -38.05, -72.049, -38.049),        # b1: dentro de Uno
                  box(-72.0003, -38.05, -71.9993, -38.049),     # b2: 30% Uno / 70% Dos -> Dos, sin partir
                  box(-71.9004, -38.05, -71.8994, -38.049),     # b3: 40% Dos / 60% Tres (Beta) -> no es de Alfa
                  box(-72.115, -38.05, -72.114, -38.049),       # b4: fuera de la DPA (en el margen del bbox)
                  shapely.Point(-72.05, -38.06)], crs=4326)    # b5: punto, no es huella
    llamadas = []

    def falsa(bbox, destino, release):
        llamadas.append((bbox, release))
        crudo.to_parquet(destino, index=False)

    with tempfile.TemporaryDirectory() as d:
        dfp, repo = Path(d) / "footprints", Path(d) / "docs_footprints"
        kw = dict(descargar=falsa, dir_manifiestos_repo=repo)
        assert F.regiones_que_calzan(["Región Alfa", "Región Beta"], "alfa|GAMMA") == ["Región Alfa"]
        m = F.descargar_regiones("ALFA", comunas, "cut", "nombre", "reg", dfp, release="2026-09-23.1", **kw)[0]
        assert len(llamadas) == 1 and llamadas[0][0][0] == -72.12   # bbox de Alfa con margen
        assert m["completa"] and m["n_edificios"] == 2 and m["fuera_de_la_dpa"] == 1 and m["de_otras_regiones"] == 1
        assert {c["cut"]: c["n"] for c in m["comunas"]} == {"01001": 1, "01002": 1}
        g = gpd.read_parquet(dfp / "region_alfa.parquet")
        assert list(g.columns) == F.COLUMNAS                     # sin names ni atributos de techo/fachada
        b2 = g.set_index("id").loc["b2"]
        assert b2.cut == "01002" and b2.fuente == "Microsoft ML Buildings"
        completa = gpd.GeoSeries([box(-72.0003, -38.05, -71.9993, -38.049)], crs=4326).to_crs(F.CRS_AREA).area[0]
        assert abs(b2.area_m2 - completa) < 1                     # el edificio no se parte
        b1 = g.set_index("id").loc["b1"]
        assert (b1.height, b1.num_floors, b1["class"], b1.fuente) == (6.0, 2, "house", "OpenStreetMap")
        assert not (dfp / "_crudo" / "region_alfa_2026-09-23.1.parquet").exists()   # crudo borrado
        assert json.loads((repo / "region_alfa.manifest.json").read_text(encoding="utf-8"))["n_edificios"] == 2
        # Reanudable: completa con el mismo release -> se salta; --refresh o release nuevo -> se vuelve a pedir
        assert F.descargar_regiones("ALFA", comunas, "cut", "nombre", "reg", dfp, release="2026-09-23.1", **kw)[0]["saltada"]
        assert len(llamadas) == 1
        F.descargar_regiones("ALFA", comunas, "cut", "nombre", "reg", dfp, release="2026-10-21.0", **kw)
        assert len(llamadas) == 2
        # Corte a mitad: manifiesto con completa=false -> se rehace
        man = json.loads((dfp / "region_alfa.manifest.json").read_text(encoding="utf-8")) | {"completa": False}
        (dfp / "region_alfa.manifest.json").write_text(json.dumps(man), encoding="utf-8")
        F.descargar_regiones("ALFA", comunas, "cut", "nombre", "reg", dfp, release="2026-10-21.0", **kw)
        assert len(llamadas) == 3
        e = F.estado(dfp)
        assert e.to_dict("records") == [{"region": "Región Alfa", "edificios": 2, "MB": e.MB[0],
                                         "release": "2026-10-21.0", "fecha": e.fecha[0], "completa": True}]
        # Beta no duplica el edificio de borde de Alfa (y viceversa)
        mb = F.descargar_regiones("BETA", comunas, "cut", "nombre", "reg", dfp, release="2026-10-21.0", **kw)[0]
        ids_b = set(gpd.read_parquet(dfp / "region_beta.parquet").id)
        assert ids_b == {"b3"} and mb["n_edificios"] == 1
        assert not ids_b & set(gpd.read_parquet(dfp / "region_alfa.parquet").id)


def test_ocupacion_sintetico():
    """Etapa 1 de VOLÚMENES: huella recortada por zona, edificio contado una vez, riesgo suma a su zona."""
    from etl import ocupacion as O
    crs = "ESRI:102033"
    piezas = gpd.GeoDataFrame({
        "ipt_nombre": ["PRC X", "PRC X", "PRC X", "PRC X", "LU Y"],
        "zona": ["A", "A", "B", None, None],
        "fuente": ["PRC", "PRC", "PRC", "PRC", "LU"],
        "riesgo": [True, False, False, False, False],
        "comuna": ["Uno"] * 4 + ["Dos"], "region": ["Región R"] * 5,
    }, geometry=[box(0, 0, 50, 100), box(50, 0, 100, 100), box(100, 0, 200, 100), box(300, 0, 400, 100), box(0, 200, 100, 300)], crs=crs)
    edif = gpd.GeoDataFrame({"id": list("abcd")}, geometry=[
        box(10, 10, 30, 30),      # 400 m² dentro de A (pieza de riesgo)
        box(60, 10, 90, 30),      # 600 m² dentro de A
        box(95, 50, 110, 60),     # 150 m²: 50 en A, 100 en B; su punto cae en B
        box(500, 500, 510, 510),  # fuera de toda zona
    ], crs=crs)
    t = O.calcular(piezas, edif).set_index(["ipt", "zona"])
    a, b, s = t.loc[("PRC X", "A")], t.loc[("PRC X", "B")], t.loc[("PRC X", "(sin zona)")]
    assert abs(a.ha - 1.0) < 1e-9 and abs(a.m2_huella - 1050) < 1e-6 and a.n_edificios == 2, a
    assert abs(a.coef_ocupacion - 0.105) < 1e-9
    assert abs(b.ha - 1.0) < 1e-9 and abs(b.m2_huella - 100) < 1e-6 and b.n_edificios == 1 and abs(b.coef_ocupacion - 0.01) < 1e-9, b
    assert s.n_edificios == 0 and s.m2_huella == 0 and abs(s.ha - 1.0) < 1e-9, s
    assert ("LU Y", "(sin zona)") not in t.index, "solo cuentan las piezas PRC"
    # la huella total nunca supera la de los edificios, y cada edificio se cuenta una sola vez
    assert t.m2_huella.sum() <= 1150 + 1e-6 and t.n_edificios.sum() == 3
    # combinar suma una misma (ipt, zona) que cruce regiones y recalcula el coeficiente
    c = O.combinar([O.calcular(piezas, edif), O.calcular(piezas, edif)]).set_index(["ipt", "zona"])
    assert abs(c.loc[("PRC X", "A")].ha - 2.0) < 1e-9 and abs(c.loc[("PRC X", "A")].coef_ocupacion - 0.105) < 1e-9
    # capa de mapa: una geometría por zona, color según el tramo y "sin edificios" aparte
    m = O.capa_mapa(piezas, O.calcular(piezas, edif)).set_index("zona")
    assert len(m) == 3 and m.crs.to_epsg() == 4326
    assert m.loc["A", "tramo"] == "10 – 20 %" and m.loc["B", "tramo"] == "< 2 %" and m.loc["(sin zona)", "tramo"] == "sin edificios"
    assert O.qa(O.calcular(piezas, edif))["zonas_coef_mayor_100"] == 0
    print("  ocupacion: OK")


def test_vcalc_sintetico():
    """V2 de VOLÚMENES: huella con >50 % dentro del predio, pisos desde la construida SII, sin inventar."""
    import pandas as pd
    from etl import vcalc as C
    crs = "ESRI:102033"
    predios = gpd.GeoDataFrame({
        "rol": ["A", "B", "C", "D"], "m2_terreno": [200.0, 200.0, 200.0, 100.0],
        "sup_construida_total": [240.0, 60.0, np.nan, 20.0], "pisos_max": [3, 1, np.nan, 1],
    }, geometry=[box(0, 0, 20, 10), box(30, 0, 50, 10), box(60, 0, 80, 10), box(90, 0, 100, 10)], crs=crs)
    edif = gpd.GeoDataFrame({"num_floors": [np.nan, np.nan, np.nan, np.nan, np.nan]}, geometry=[
        box(2, 2, 12, 10),      # A: 80 m² dentro del predio
        box(14, 2, 24, 10),     # A: 60 % dentro (6 de 10 de ancho)  -> cuenta, 80 m²
        box(32, 2, 42, 8),      # B: 60 m²
        box(18.5, 2, 28.5, 8),  # 5 % en A, 5 % fuera: <50 % dentro de cualquier predio -> no cuenta
        box(90.5, 2, 99.5, 8),  # D: 54 m²
    ], crs=crs)
    zonas = gpd.GeoDataFrame({"zona": ["ZH2"]}, geometry=[box(-5, -5, 55, 15)], crs=crs)
    t = C.calcular(predios, edif, zonas=zonas, regla="umbral50").set_index("rol")      # la regla original
    a, b, c, d = t.loc["A"], t.loc["B"], t.loc["C"], t.loc["D"]
    assert a.n_edificios == 2 and abs(a.huella_m2 - 160) < 1e-6 and a.pisos_est == 2 and abs(a.m2_equiv - 320) < 1e-6 and a.zona == "ZH2", a
    assert b.pisos_est == 1 and abs(b.huella_m2 - 60) < 1e-6 and b.fuente_pisos == "sii" and b.confianza_pisos
    assert c.estado == "sin_dato" and c.n_edificios == 0 and np.isnan(c.m2_equiv) and c.zona == "" and "sin huella" in c.avisos
    assert d.pisos_est == 1 and d.zona == "" and "menos de la mitad" in d.avisos, d
    assert t.v_calc_m3.isna().all(), "sin altura de piso de referencia no hay m³"
    assert abs(C.calcular(predios, edif, altura_piso_ref_m=2.5, regla="umbral50").set_index("rol").loc["A", "v_calc_m3"] - 800) < 1e-6
    # casas pareadas: un edificio repartido entre dos lotes (60 % / 40 %) y una astilla sobre un tercero
    lotes = gpd.GeoDataFrame({"rol": ["P", "Q", "R"], "m2_terreno": [200.0, 200.0, 200.0], "sup_construida_total": [192.0, 64.0, 50.0],
                              "pisos_max": [2, 1, 1]}, geometry=[box(0, 0, 10, 20), box(10, 0, 20, 20), box(40, 0, 50, 20)], crs=crs)
    casas = gpd.GeoDataFrame({"num_floors": [np.nan, np.nan]}, geometry=[
        box(4, 2, 14, 18),          # 160 m²: 96 m² en P (60 %), 64 m² en Q (40 %)
        box(38.5, 5, 40.5, 6),      # 2 m²: solo 0,5 m² en R (25 % del edificio, 0,25 % del terreno): astilla
    ], crs=crs)
    old = C.calcular(lotes, casas, regla="umbral50").set_index("rol")
    assert abs(old.loc["P", "huella_m2"] - 160) < 1e-6 and old.loc["Q", "estado"] == "sin_dato" and "sin huella" in old.loc["Q", "avisos"], "50 %: P se lleva todo y Q queda sin huella"
    new = C.calcular(lotes, casas).set_index("rol")
    p, q, r = new.loc["P"], new.loc["Q"], new.loc["R"]
    assert abs(p.huella_m2 - 96) < 1e-6 and abs(q.huella_m2 - 64) < 1e-6, "reparto por área de intersección"
    assert p.pisos_est == 2 and q.pisos_est == 1 and q.estado == "ok" and "compartido" in p.avisos and "compartido" in q.avisos
    assert r.estado == "sin_dato" and r.n_edificios == 0 and "sin huella" in r.avisos, "la astilla no cuenta"
    assert abs(new.huella_m2.sum() - 160) < 1e-6, "no se crea ni se pierde huella al repartir"
    print("  vcalc: OK")


def _app_fixture(tmp: Path):
    """Mundo mínimo para Regu Suelo: GPKG de build, vigencia, ocupación, normas, comunas y footprints (EPSG:4326)."""
    import pandas as pd
    from app.ajustes import Ajustes
    from etl import volumen as V
    out = tmp / "out"
    out.mkdir(parents=True)
    base = dict(comuna="Temuco", cut="09101", region="Región de La Araucanía", ipt_tipo="PRC", ipt_nombre="Temuco",
                clase="U1", norma_titulo="Plan Regulador Comunal", aviso="Información referencial.")
    zh2 = box(-72.60, -38.74, -72.59, -38.73)
    zhr5 = box(-72.59, -38.74, -72.58, -38.73)
    capa = gpd.GeoDataFrame([
        {**base, "zona": "ZH2", "riesgo": False, "id": "a1"},
        {**base, "zona": "ZHR5", "riesgo": True, "id": "a2"},
    ], geometry=[zh2, zhr5], crs=4326)
    capa["area_m2"] = capa.to_crs("ESRI:102033").area.round(1)
    afec = gpd.GeoDataFrame([{"ipt_tipo": "PRC", "capa": "Areas_protec", "zona": "APP 1", "zona_desc": "Área de protección",
                              "riesgo": False}], geometry=[box(-72.60, -38.74, -72.595, -38.735)], crs=4326)
    gpkg = out / "regu_ipt_nacional_20261004.gpkg"
    capa.to_file(gpkg, layer="capa_ipt", driver="GPKG")
    afec.to_file(gpkg, layer="afectaciones", driver="GPKG")
    pd.DataFrame([{"ipt_tipo": "PRC", "ipt_nombre": "Temuco", "cut": "09101", "portal_id": "1055", "portal_tipo": "PRC",
                   "denominacion": "Plan Regulador Comunal de Temuco-Labranza", "norma": "Resolución N° 149",
                   "fecha_vigencia": "2010-02-02", "ultima_modificacion": "2015-06-13", "n_modificaciones": "3",
                   "ordenanza_url": "https://ejemplo.cl/ord.pdf", "score": "0.5", "confianza": "alta"}]
                 ).to_csv(out / "vigencia_match.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"ipt": "Temuco", "zona": "ZH2", "ha": 100.0, "m2_huella": 26000, "coef_ocupacion": 0.26, "n_edificios": 500,
                   "region": "Región de La Araucanía", "comunas": "Temuco"}]
                 ).to_csv(out / "ocupacion_zonas_nacional_20261004.csv", index=False, encoding="utf-8-sig")
    normas = tmp / "normas_zona.csv"
    fila = {c: "" for c in V.COLUMNAS_NORMAS} | {
        "ipt_nombre": "Temuco", "zona": "ZH2", "agrupamiento": "Aislado", "ocupacion_max": "0.5", "constructibilidad_max": "1.5",
        "altura_max_m": "17.5", "articulo_fuente": "Art. 16, tabla B 2", "estado": "BORRADOR"}
    pd.DataFrame([fila]).to_csv(normas, index=False, encoding="utf-8-sig")
    com = gpd.GeoDataFrame([{"cod_comuna": 9101, "Comuna": "Temuco", "Region": "Región de La Araucanía"}],
                           geometry=[box(-72.62, -38.76, -72.56, -38.70)], crs=4326)
    com.to_file(tmp / "comunas.gpkg", driver="GPKG")
    fp = tmp / "fp"
    fp.mkdir()
    # Footprints (formato de etl/footprints.py): A cae dentro del predio de la prueba de volumen y tiene num_floors; B no
    ed = gpd.GeoDataFrame({
        "id": ["A", "B", "C"], "cut": ["09101"] * 3, "comuna": ["Temuco"] * 3, "region": ["Región de La Araucanía"] * 3,
        "height": [np.nan, np.nan, 12.0], "num_floors": [3.0, np.nan, np.nan], "class": [None] * 3, "subtype": [None] * 3,
        "fuente": ["microsoft", "osm", "google"], "area_m2": [56.0, 100.0, 80.0],
    }, geometry=[box(-72.59495, -38.73490, -72.59486, -38.73483), box(-72.5990, -38.7390, -72.5989, -38.7389),
                 box(-72.5880, -38.7350, -72.5879, -38.7349)], crs=4326)
    ed.to_parquet(fp / "region_de_la_araucania.parquet")
    (fp / "region_de_la_araucania.manifest.json").write_text(
        json.dumps({"region": "Región de La Araucanía", "completa": True}), encoding="utf-8")
    return Ajustes(gpkg=gpkg, dir_footprints=fp, normas=normas, dir_out=out, dir_demanda=tmp / "demanda",
                   comunas=tmp / "comunas.gpkg")


def test_app_sintetico():
    """A1 de Regu Suelo: /api/comunas y /api/ficha (punto y polígono) con GPKG sintético, normas BORRADOR marcadas."""
    from fastapi.testclient import TestClient
    from app.main import crear_app
    with tempfile.TemporaryDirectory() as d:
        a = _app_fixture(Path(d))
        c = TestClient(crear_app(a))
        com = c.get("/api/comunas").json()
        assert com[0]["cut"] == "09101" and com[0]["nombre"] == "Temuco" and len(com[0]["bbox"]) == 4, com
        f = c.get("/api/ficha", params={"lon": -72.595, "lat": -38.738}).json()
        p = f["particion"][0]
        assert p["clase"] == "U1" and p["ipt"] == "Temuco" and p["zona"] == "ZH2" and p["pct"] == 100.0, p
        assert p["vigencia"]["norma"] == "Resolución N° 149" and p["vigencia"]["ordenanza_url"].endswith(".pdf")
        assert p["ocupacion"]["coef_ocupacion"] == 0.26 and p["ocupacion"]["n_edificios"] == 500
        assert p["normas"][0]["estado"] == "BORRADOR" and p["normas"][0]["articulo_fuente"] == "Art. 16, tabla B 2"
        assert p["normas"][0]["marca"] == "BORRADOR · uso interno" and f["marca"] == "BORRADOR · uso interno"
        assert "ocupacion_max" in p["normas"][0] and "distanciamiento_m" not in p["normas"][0], "solo columnas con valor"
        assert any(x["zona"] == "APP 1" for x in f["afectaciones"]) and f["aviso"] and isinstance(f["tiempo_ms"], int)
        r = c.get("/api/ficha", params={"lon": -72.585, "lat": -38.735}).json()      # ZHR5: riesgo y sin norma ni ocupación
        assert r["particion"][0]["zona"] == "ZHR5" and r["riesgo_pct"] == 100.0
        assert r["particion"][0]["normas"] == [] and r["particion"][0]["ocupacion"] is None and r["marca"] is None
        fuera = c.get("/api/ficha", params={"lon": -70.0, "lat": -33.0}).json()
        assert fuera["cobertura"] == "fuera_dpa" and fuera["particion"] == []
        poli = {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[
            [-72.595, -38.736], [-72.585, -38.736], [-72.585, -38.734], [-72.595, -38.734], [-72.595, -38.736]]]}}
        pf = c.post("/api/ficha", json=poli).json()
        assert {x["zona"] for x in pf["particion"]} == {"ZH2", "ZHR5"} and abs(sum(x["pct"] for x in pf["particion"]) - 100) < 0.1
        assert pf["marca"] == "BORRADOR · uso interno" and c.post("/api/ficha", json={"type": "Polygon", "coordinates": []}).status_code == 422
        assert c.post("/api/ficha", json={"type": "LineString", "coordinates": [[0, 0], [1, 1]]}).status_code == 422
        assert c.get("/api/ficha", params={"lon": 500, "lat": 0}).status_code == 422
        log = (Path(d) / "demanda" / "consultas.jsonl").read_text(encoding="utf-8")
        assert "-72.5" not in log and "-38.7" not in log, "el registro de demanda no lleva coordenadas"
    print("  app: OK")


def _predio_4326(ancho=20.0, alto=30.0):
    """Predio rectangular de ancho × alto metros, construido en ESRI:102033 y llevado a EPSG:4326."""
    return gpd.GeoSeries([box(0, 0, ancho, alto)], crs="ESRI:102033").to_crs(4326).iloc[0]


def test_envolvente_sintetico():
    """Fase 1 de volumen, tres casos a mano (docs/VOLUMEN_PILOTO.md), más retranqueo, sin_dato, excede y edificio compartido."""
    from etl import envolvente as E
    pred = _predio_4326()                       # 20 × 30 = 600 m²
    n = lambda **k: {"agrupamiento": "Aislado", "estado": "BORRADOR", "ocupacion_max": "0.4", "constructibilidad_max": "3",  # noqa: E731
                     "altura_max_m": "14", **{a: str(b) for a, b in k.items()}}
    ap = lambda v: abs(v) < 0.01  # noqa: E731
    e1 = E.posible(pred, n(), 3.5)              # 14 m / 3,5 = 4 pisos; 0,4 × 600 = 240; limita ocupación × altura
    assert e1["pisos_max"] == 4 and abs(e1["m2_max"] - 960) < 960 * 0.01 and e1["limita"] == "ocupación × altura", e1
    e2 = E.posible(pred, n(ocupacion_max=0.6, constructibilidad_max=1), 3.5)   # min(360 × 4, 1 × 600) = 600
    assert abs(e2["m2_max"] - 600) < 6 and e2["limita"] == "constructibilidad", e2
    e3 = E.posible(pred, n(ocupacion_max=0.5, altura_max_m=7), 3.5)            # 2 pisos: 300 × 2 = 600 (limita la altura)
    assert e3["pisos_max"] == 2 and abs(e3["m2_max"] - 600) < 6 and abs(e3["v_max_m3"] - 0.5 * 600 * 7) < 21, e3
    assert e1["v_max_m3"] >= e1["v_opt_m3"] and e1["eficiencia"] <= 1 and e1["estado"] == "ok"
    e4 = E.posible(pred, n(ocupacion_max=0.5, antejardin_m="5.0 frente a vías colectoras; 3.0 frente a vías locales"), 3.5)
    assert e4["retranqueo_m"] == 3.0 and abs(e4["area_base_m2"] - 14 * 24) < 4 and abs(e4["area_primer_piso_m2"] - 300) < 3, e4
    e5 = E.posible(pred, n(ocupacion_max=0.9, antejardin_m="3.0", distanciamiento_m="4.0"), 3.5)
    assert e5["retranqueo_m"] == 4.0 and abs(e5["area_base_m2"] - 12 * 22) < 4 and any("Retranqueo uniforme" in s for s in e5["simplificaciones"])
    assert E.posible(pred, n(altura_max_m=""), 3.5)["estado"] == "sin_dato"
    chica = E.posible(_predio_4326(5, 5), n(antejardin_m="3.0"), 3.5)
    assert chica["envolvente"] is None and chica["estado"] == "sin_base" and "no es aplicable" in chica["motivo"], "sin base edificable"
    assert E.comparar({"m2_existente": 0.0}, chica)["estado_iov"] == "sin_dato"
    assert any("no es norma" in s for s in e1["simplificaciones"]) and E.posible(pred, n(altura_piso_ref_m="3"), 3.5)["altura_piso_ref_m"] == 3.0
    # existente: A (3 pisos, 100 % dentro), B (70 % dentro, sin dato) cuenta; C (30 % dentro) no cuenta; D (100 % dentro) con height
    def b(x0, y0, w, h): return gpd.GeoSeries([box(x0, y0, x0 + w, y0 + h)], crs="ESRI:102033").to_crs(4326).iloc[0]
    ed = gpd.GeoDataFrame({"id": list("ABCD"), "height": [np.nan, np.nan, np.nan, 14.0], "num_floors": [3.0, np.nan, np.nan, np.nan]},
                          geometry=[b(2, 2, 10, 10), b(14, 20, 10, 10), b(17, 2, 10, 10), b(2, 15, 5, 5)], crs=4326)
    ex = E.existente(pred, ed, 3.5)
    assert ex["n_edificios"] == 3 and abs(ex["huella_m2"] - (100 + 100 + 25)) < 3, ex
    assert abs(ex["m2_existente"] - (100 * 3 + 100 * 1 + 25 * 4)) < 5 and ex["fuente_pisos"] == "mixto" and ex["cota_inferior"], ex
    assert abs(ex["sup_terreno_m2"] - 600) < 6
    fe = {f["properties"]["id"]: f["properties"] for f in ex["edificios"]}
    assert set(fe) == {"A", "B", "D"} and fe["A"]["altura_m"] == 10.5 and fe["D"]["altura_m"] == 14.0 and fe["B"]["altura_m"] == 3.5, fe
    assert all(f["geometry"]["type"] == "Polygon" and -180 < f["geometry"]["coordinates"][0][0][0] < 180 and abs(f["geometry"]["coordinates"][0][0][1]) < 90 for f in ex["edificios"]), "en grados (EPSG:4326)"
    solo = E.existente(pred, ed.iloc[[1]].assign(num_floors=np.nan), 3.5)       # una sola fuente: estimado → cota inferior, baja
    assert solo["fuente_pisos"] == "estimado" and solo["cota_inferior"] and solo["confianza_pisos"] == "baja", solo
    vacio = E.existente(pred, ed.iloc[[2]], 3.5)                                # C: 30 % dentro → no cuenta: sitio sin edificios
    assert vacio["n_edificios"] == 0 and vacio["m2_existente"] == 0 and vacio["fuente_pisos"] is None and not vacio["cota_inferior"]
    assert E.comparar(vacio, e1)["iov"] == 0 and E.comparar(vacio, e1)["estado_iov"] == "holgura"
    # excede: lo existente (500 m²) supera lo posible (e1: 960 → no excede) y un caso con m2_max chico sí
    chico = E.posible(pred, n(ocupacion_max=0.2, constructibilidad_max=0.5, altura_max_m=3.5), 3.5)   # 1 piso: 120 m² (< 300)
    assert abs(chico["m2_max"] - 120) < 2
    c = E.comparar(ex, chico)
    assert c["estado_iov"] == "excede" and c["iov"] > 1 and c["remanente_m2"] < 0, c
    assert E.comparar(ex, e1)["estado_iov"] == "holgura" and E.comparar(ex, E.posible(pred, n(altura_max_m=""), 3.5))["estado_iov"] == "sin_dato"
    assert [E.estado_iov(x) for x in (0.79, 0.8, 1.0, 1.01)] == ["holgura", "al_limite", "al_limite", "excede"]
    assert E.numeros("5,0 m; 3.5") == [5.0, 3.5]
    print("  envolvente: OK")


def test_app_edificios_volumen():
    """A2 de Regu Suelo: /api/edificios (DuckDB sobre un parquet sintético) y /api/volumen con normas BORRADOR."""
    from fastapi.testclient import TestClient
    from app.main import crear_app
    with tempfile.TemporaryDirectory() as d:
        a = _app_fixture(Path(d))
        a.registrar = False
        c = TestClient(crear_app(a))
        r = c.get("/api/edificios", params={"bbox": "-72.600,-38.745,-72.590,-38.730", "zoom": 17}).json()
        ids = {f["properties"]["id"]: f["properties"] for f in r["features"]}
        assert set(ids) == {"A", "B"} and not r["recortado"] and "Overture" in r["atribucion"], r
        assert ids["A"]["altura_est"] == 10.5 and ids["A"]["altura_fuente"] == "overture_num_floors" and ids["A"]["fuente"] == "microsoft"
        assert ids["B"]["altura_est"] == 3.5 and ids["B"]["altura_fuente"] == "estimado"
        c2 = c.get("/api/edificios", params={"bbox": "-72.60,-38.745,-72.58,-38.73"}).json()
        assert {f["properties"]["id"]: f["properties"] for f in c2["features"]}["C"]["altura_est"] == 12.0, "usa height de Overture"
        r1 = c.get("/api/edificios", params={"bbox": "-72.600,-38.745,-72.590,-38.730", "limite": 1}).json()
        assert r1["recortado"] and len(r1["features"]) == 1 and "acércate" in r1["aviso"]
        grande = c.get("/api/edificios", params={"bbox": "-73,-39,-72,-38"}).json()
        assert grande["features"] == [] and "acércate" in grande["aviso"]
        assert c.get("/api/edificios", params={"bbox": "1,2,3"}).status_code == 422
        assert c.get("/api/edificios", params={"bbox": "-72.6,-38.7,-72.7,-38.8"}).status_code == 422
        predio = {"type": "Polygon", "coordinates": [[[-72.5950, -38.7350], [-72.59477, -38.7350], [-72.59477, -38.73473],
                                                        [-72.5950, -38.73473], [-72.5950, -38.7350]]]}
        v = c.post("/api/volumen", json=predio).json()
        assert v["zona"]["zona"] == "ZH2" and v["marca"] == "BORRADOR · uso interno" and "Overture" in v["atribucion"]
        assert v["existente"]["n_edificios"] == 1 and v["existente"]["fuente_pisos"] == "overture_num_floors"
        assert [f["properties"]["id"] for f in v["existente"]["edificios"]] == ["A"] and v["existente"]["edificios"][0]["properties"]["altura_m"] == 10.5
        assert abs(v["existente"]["huella_m2"] - 61) < 3 and abs(v["existente"]["m2_existente"] - 3 * v["existente"]["huella_m2"]) < 1 and abs(v["existente"]["sup_terreno_m2"] - 600) < 40
        e = v["escenarios"][0]
        assert e["normas_estado"] == "BORRADOR" and e["pisos_max"] == 5 and e["limita"] == "constructibilidad", e
        assert abs(e["m2_max"] - 1.5 * v["existente"]["sup_terreno_m2"]) < 1 and e["v_max_m3"] >= e["v_opt_m3"]
        assert e["envolvente"]["type"] == "Polygon" and e["articulo_fuente"] == "Art. 16, tabla B 2"
        assert e["estado_iov"] == "holgura" and 0 < e["iov"] < 0.8 and e["remanente_m2"] > 0
        assert any("no es norma" in s for s in e["simplificaciones"]), "declara el parámetro del modelo"
        assert "brecha" not in str(v).lower() and "propietario" not in str(v).lower()
        sin_norma = c.post("/api/volumen", json={"type": "Polygon", "coordinates": [[[-72.585, -38.735], [-72.5848, -38.735],
                                                  [-72.5848, -38.7348], [-72.585, -38.7348], [-72.585, -38.735]]]}).json()   # ZHR5
        assert sin_norma["escenarios"] == [] and sin_norma["marca"] is None and any("no tiene normas" in s for s in sin_norma["simplificaciones"])
        assert c.post("/api/volumen", json={"type": "Point", "coordinates": [-72.595, -38.735]}).status_code == 422
        assert c.post("/api/volumen", json={"type": "Polygon", "coordinates": [[[-70, -33], [-70.001, -33], [-70.001, -33.001], [-70, -33]]]}).status_code == 422
        enorme = {"type": "Polygon", "coordinates": [[[-72.60, -38.74], [-72.58, -38.74], [-72.58, -38.73], [-72.60, -38.73], [-72.60, -38.74]]]}
        assert c.post("/api/volumen", json=enorme).status_code == 422
    print("  app edificios/volumen: OK")


def test_app_teselas():
    """A3 de Regu Suelo: teselas PMTiles de los tres temas, /tiles con Range (206), estáticos locales y errores claros."""
    import pyogrio
    from fastapi.testclient import TestClient
    from app.main import crear_app
    from etl import teselas as T
    with tempfile.TemporaryDirectory() as d:
        a = _app_fixture(Path(d))
        c = TestClient(crear_app(a))
        tiles = a.tiles
        assert tiles == Path(a.dir_out) / "tiles"
        # generación real con GDAL sobre los datos sintéticos
        T.normativa(a.gpkg, tiles / "normativa.pmtiles")
        T.comunas(a.comunas, "cod_comuna", "Comuna", "Region", tiles / "comunas.pmtiles")
        oc = gpd.GeoDataFrame({"ipt": ["Temuco"], "zona": ["ZH2"], "ha": [100.0], "m2_huella": [26000.0], "coef_ocupacion": [0.26],
                               "n_edificios": [500], "tramo": ["20 – 30 %"], "color": ["#2c7fb8"]},
                              geometry=[box(-72.60, -38.74, -72.59, -38.73)], crs=4326)
        oc.to_file(Path(d) / "ocupacion_nacional_20261004.gpkg", layer="ocupacion_zonas", driver="GPKG")
        T.ocupacion(Path(d) / "ocupacion_nacional_20261004.gpkg", tiles / "ocupacion.pmtiles")
        for tema, capa in (("normativa", "ipt"), ("ocupacion", "ocupacion"), ("comunas", "comunas")):
            p = tiles / f"{tema}.pmtiles"
            assert p.read_bytes()[:7] == b"PMTiles", tema
            assert capa in [x[0] for x in pyogrio.list_layers(p)], (tema, pyogrio.list_layers(p))
        # servicio: completo, con Range (lo que pide pmtiles.js) y con rango inválido
        total = (tiles / "comunas.pmtiles").read_bytes()
        r = c.get("/tiles/comunas.pmtiles")
        assert r.status_code == 200 and r.content == total and r.headers.get("accept-ranges") == "bytes"
        r = c.get("/tiles/comunas.pmtiles", headers={"Range": "bytes=0-126"})
        assert r.status_code == 206 and r.content == total[:127] and r.headers["content-range"] == f"bytes 0-126/{len(total)}", r.headers
        r = c.get("/tiles/comunas.pmtiles", headers={"Range": f"bytes={len(total) - 10}-"})
        assert r.status_code == 206 and r.content == total[-10:]
        assert c.get("/tiles/comunas.pmtiles", headers={"Range": f"bytes={len(total) + 5}-"}).status_code == 416
        assert c.get("/tiles/otro.pmtiles").status_code == 404
        (tiles / "ocupacion.pmtiles").unlink()
        r = c.get("/tiles/ocupacion.pmtiles")
        assert r.status_code == 404 and "run.py teselas ocupacion" in r.json()["detail"], r.text
        # aplicación y librerías locales (sin CDN)
        r = c.get("/")
        assert r.status_code == 200 and "text/html" in r.headers["content-type"] and "/static/vendor/maplibre-gl.js" in r.text
        assert "cdn." not in r.text and "unpkg" not in r.text, "la página no debe cargar nada de internet"
        # mapas base: sin fondo por defecto, OSM por el proxy local, Esri y EOX directos, cada uno con su atribución visible
        for clave in ('ninguno: {nombre: "Sin fondo', "/basemap/osm/{z}/{x}/{y}.png", "World_Imagery/MapServer/tile/{z}/{y}/{x}",
                      "s2cloudless_3857/default/g/{z}/{y}/{x}.jpg", "OpenStreetMap</a> contributors", "Esri, Maxar, Earthstar Geographics",
                      "EOxCloudless", "CC BY 4.0", 'id="atrib"', "attributionControl: false"):
            assert clave in r.text, clave
        assert "tile.openstreetmap.org" not in r.text, "OSM solo por el proxy (User-Agent identificable)"
        assert 'let fondoActual = FONDOS[guardado("regu.fondo")] ? guardado("regu.fondo") : "ninguno"' in r.text
        # transparencia por capa temática, con valor recordado
        for tema in ("normativa", "ocupacion", "edificios"):
            assert f'data-opacidad="{tema}"' in r.text, tema
        assert "regu.opacidad" in r.text and "localStorage" in r.text and "catch (e)" in r.text, "recuerda con try/catch"
        # M4: selector de mapas temáticos con leyenda, transparencia propia, atribución y memoria
        for clave in ('id="tema"', 'fetch("/api/temas")', "/tiles/temas/", '"regu.tema"', '"raster-opacity"', "licencia por verificar", "refrescarAtrib"):
            assert clave in r.text, clave
        # M5: contexto del lugar (valor de cada tema al clic y en el centro de un predio dibujado)
        for clave in ("/api/lugar", "Contexto del lugar", "cargarLugar(", "lugarHTML"):
            assert clave in r.text, clave
        # A5: dibujo de predio, volumen 3D (existente sólido y envolvente translúcida) y marca BORRADOR
        for clave in ('id="bdibujar"', 'id="blimpiar"', '"/api/volumen"', '"/api/ficha"', 'id: "existente3d"', 'id: "envolvente3d"',
                      '"fill-extrusion-opacity": 0.38', "Envolvente posible, fase 1 (sin rasantes)", "Simplificaciones y supuestos",
                      "uso interno", "mapa.doubleClickZoom.disable()"):
            assert clave in r.text, clave
        for f in ("maplibre-gl.js", "maplibre-gl.css", "pmtiles.js"):
            assert c.get(f"/static/vendor/{f}").status_code == 200, f
        assert c.get("/static/../main.py").status_code in (400, 404)
    print("  app teselas: OK")


def test_app_temas():
    """M4 de mapas temáticos: /api/temas (sin rutas del disco), /tiles/temas con Range, recarga y contrato inválido."""
    import copy
    import os
    import yaml
    from fastapi.testclient import TestClient
    from app.main import crear_app
    from etl import temas as T

    base = copy.deepcopy(T.cargar_catalogo()["clima_temperatura_media_anual"])
    base.pop("muestreo", None)            # cada prueba declara el suyo
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        a = _app_fixture(d)
        a.dir_temas = d / "temas"
        a.dir_temas.mkdir()

        def escribir(id_, estado="activa", **extra):
            c = {**copy.deepcopy(base), "id": id_, "estado": estado, **extra}
            (a.dir_temas / f"{id_}.yaml").write_text(yaml.safe_dump(c, allow_unicode=True), encoding="utf-8")

        escribir("tema_a")
        escribir("tema_b", nombre="Tema B sin tesela")
        escribir("tema_c", estado="propuesta")
        (Path(a.dir_out) / "tiles" / "temas").mkdir(parents=True)
        (Path(a.dir_out) / "tiles" / "temas" / "tema_a.pmtiles").write_bytes(b"PMTiles" + bytes(range(200)))
        (Path(a.dir_out) / "tiles" / "temas" / "tema_c.pmtiles").write_bytes(b"PMTiles-no-debe-servirse")
        c = TestClient(crear_app(a))
        r = c.get("/api/temas").json()
        ids = {t["id"]: t for t in r["temas"]}
        assert set(ids) == {"tema_a", "tema_b"} and r["error"] is None, "las propuestas no se muestran"
        assert ids["tema_a"]["disponible"] is True and ids["tema_b"]["disponible"] is False
        t = ids["tema_a"]
        assert t["estilo"]["rampa"] and t["fuente"]["atribucion"] and t["zoom"] == [3, 8] and t["tipo"] == "raster" and not t["publicable"]
        assert "generacion" not in t and str(d) not in json.dumps(r), "sin rutas del disco ni detalles de generación"
        # teselas: completa, con Range, tema sin tesela, propuesta y desconocido
        total = (Path(a.dir_out) / "tiles" / "temas" / "tema_a.pmtiles").read_bytes()
        assert c.get("/tiles/temas/tema_a.pmtiles").content == total
        rr = c.get("/tiles/temas/tema_a.pmtiles", headers={"Range": "bytes=0-6"})
        assert rr.status_code == 206 and rr.content == b"PMTiles" and rr.headers["content-range"] == f"bytes 0-6/{len(total)}"
        e = c.get("/tiles/temas/tema_b.pmtiles")
        assert e.status_code == 404 and "temas generar --tema tema_b" in e.json()["detail"]
        assert c.get("/tiles/temas/tema_c.pmtiles").status_code == 404, "una propuesta no se sirve aunque exista el archivo"
        assert c.get("/tiles/temas/no_existe.pmtiles").status_code == 404
        # recarga: un tema nuevo aparece sin reiniciar; un contrato inválido no tumba la app y se informa
        escribir("tema_d")
        assert "tema_d" in {x["id"] for x in c.get("/api/temas").json()["temas"]}
        (a.dir_temas / "roto.yaml").write_text("id: roto\nzoom: [9, 3]\n", encoding="utf-8")
        os.utime(a.dir_temas / "roto.yaml", (1_900_000_000, 1_900_000_000))
        r = c.get("/api/temas").json()
        assert r["temas"] == [] and "roto.yaml" in r["error"], "con un contrato inválido se informa y no se cae"
        assert c.get("/api/ficha", params={"lon": -72.595, "lat": -38.738}).status_code == 200, "el resto de la app sigue funcionando"
        (a.dir_temas / "roto.yaml").unlink()
        assert {x["id"] for x in c.get("/api/temas").json()["temas"]} == {"tema_a", "tema_b", "tema_d"} and c.get("/api/temas").json()["error"] is None

        # M5: valor al clic. Rásters sintéticos en <raw>/temas/<id>/: continuo (×0,1) con un hueco nodata, y categórico con leyenda
        import rasterio
        from rasterio.transform import from_bounds
        a.dir_raw = d / "raw"
        for id_ in ("tema_a", "tema_b", "tema_d"):
            (a.dir_temas / f"{id_}.yaml").unlink()
        escribir("tema_t", nombre="Temperatura de prueba", muestreo={"archivo": "t.tif", "decimales": 1},
                 estilo={"unidad": "°C", "factor": 0.1, "interpolacion": "lineal", "rampa": [[0, "#000000"], [30, "#ff0000"]]})
        escribir("tema_cat", nombre="Cobertura de prueba", muestreo={"archivo": "cat.tif"},
                 estilo={"unidad": "clase", "interpolacion": "escalon", "rampa": [[10, "#006400"], [20, "#ffbb22"]],
                         "leyenda": [{"etiqueta": "Árboles", "color": "#006400"}, {"etiqueta": "Matorral", "color": "#ffbb22"}]})
        escribir("tema_sin", nombre="Sin muestreo", estilo={"unidad": "x", "interpolacion": "lineal", "rampa": [[0, "#000000"], [1, "#ffffff"]]})
        escribir("tema_falta", nombre="Sin descargar", muestreo={"archivo": "no_esta.tif"})
        escribir("tema_prop", estado="propuesta", muestreo={"archivo": "t.tif"})
        for id_ in ("tema_t", "tema_cat", "tema_falta"):
            (d / "raw" / "temas" / id_).mkdir(parents=True)
        t = np.full((50, 50), 123.0, dtype="float32")
        t[20:30, 20:30] = -9999
        for ruta, datos, nd in (("tema_t/t.tif", t, -9999), ("tema_cat/cat.tif", np.full((50, 50), 20, dtype="uint8"), 0)):
            with rasterio.open(d / "raw" / "temas" / ruta, "w", driver="GTiff", height=50, width=50, count=1, dtype=str(datos.dtype), crs="EPSG:4326",
                               transform=from_bounds(-73, -39, -71, -38, 50, 50), nodata=nd) as dst:
                dst.write(datos, 1)
        r = c.get("/api/lugar", params={"lon": -72.9, "lat": -38.1}).json()
        f = {x["id"]: x for x in r["temas"]}
        assert set(f) == {"tema_t", "tema_cat", "tema_falta"}, "sin muestreo y propuestas no aparecen"
        assert f["tema_t"]["valor"] == 12.3 and f["tema_t"]["unidad"] == "°C" and f["tema_t"]["motivo"] is None and f["tema_t"]["fuente"] and f["tema_t"]["resolucion"]
        assert f["tema_cat"]["valor"] == 20 and f["tema_cat"]["etiqueta"] == "Matorral"
        assert f["tema_falta"]["valor"] is None and "no está descargado" in f["tema_falta"]["motivo"]
        assert r["aviso"] and "no reemplaza" in r["aviso"] and isinstance(r["tiempo_ms"], int) and str(d) not in json.dumps(r)
        hueco = {x["id"]: x for x in c.get("/api/lugar", params={"lon": -72.0, "lat": -38.5}).json()["temas"]}
        assert hueco["tema_t"]["valor"] is None and hueco["tema_t"]["motivo"] == "sin dato en este punto" and hueco["tema_cat"]["valor"] == 20
        fuera = {x["id"]: x for x in c.get("/api/lugar", params={"lon": -60.0, "lat": -30.0}).json()["temas"]}
        assert fuera["tema_t"]["motivo"] == "fuera del área generada de este tema"
        assert c.get("/api/lugar", params={"lon": 500, "lat": 0}).status_code == 422
        # una clase que no está en la leyenda no rompe: se informa como «clase N»
        with rasterio.open(d / "raw" / "temas" / "tema_cat" / "cat.tif", "r+") as dst:
            dst.write(np.full((50, 50), 99, dtype="uint8"), 1)
        assert {x["id"]: x for x in c.get("/api/lugar", params={"lon": -72.9, "lat": -38.1}).json()["temas"]}["tema_cat"]["etiqueta"] == "clase 99"
    print("  app temas: OK")


def test_app_basemap():
    """Mapa base OSM: proxy con User-Agent identificable, caché en disco, copia vencida sin internet y respuestas inválidas."""
    import os
    import time
    import requests
    from fastapi.testclient import TestClient
    from app import basemap
    from app.main import crear_app

    class Resp:
        def __init__(self, codigo=200, tipo="image/png", cuerpo=b"\x89PNG\r\n\x1a\nXXXX"):
            self.status_code, self.headers, self.content = codigo, {"content-type": tipo}, cuerpo

    llamadas, respuestas = [], []
    original = basemap.requests.get

    def falso(url, headers=None, timeout=None):
        llamadas.append({"url": url, "ua": (headers or {}).get("User-Agent")})
        r = respuestas.pop(0) if respuestas else Resp()
        if isinstance(r, Exception):
            raise r
        return r

    basemap.requests.get = falso
    try:
        with tempfile.TemporaryDirectory() as d:
            a = _app_fixture(Path(d))
            c = TestClient(crear_app(a))
            r = c.get("/basemap/osm/12/1222/2526.png")
            assert r.status_code == 200 and r.content[:4] == b"\x89PNG" and r.headers["content-type"] == "image/png"
            assert llamadas[0]["url"] == "https://tile.openstreetmap.org/12/1222/2526.png", llamadas
            assert llamadas[0]["ua"].startswith("ReguSueloLocal/") and "github.com" in llamadas[0]["ua"], "User-Agent identificable"
            assert (Path(a.dir_out) / "cache_basemap" / "osm" / "12" / "1222" / "2526.png").exists()
            assert c.get("/basemap/osm/12/1222/2526.png").status_code == 200 and len(llamadas) == 1, "la segunda sale de disco"
            # copia vencida + sin internet → se sirve la vieja; copia vencida + internet → se vuelve a pedir
            p = Path(a.dir_out) / "cache_basemap" / "osm" / "12" / "1222" / "2526.png"
            viejo = time.time() - 30 * 86400
            os.utime(p, (viejo, viejo))
            respuestas.append(requests.ConnectionError("sin internet"))
            assert c.get("/basemap/osm/12/1222/2526.png").status_code == 200 and len(llamadas) == 2, "usa la copia vencida"
            os.utime(p, (viejo, viejo))
            respuestas.append(Resp(cuerpo=b"\x89PNG\r\n\x1a\nNUEVA"))
            assert c.get("/basemap/osm/12/1222/2526.png").content.endswith(b"NUEVA") and len(llamadas) == 3
            # sin copia: sin internet o respuesta inválida → 502 y nada queda en disco
            respuestas.append(requests.ConnectionError("sin internet"))
            r = c.get("/basemap/osm/10/300/600.png")
            assert r.status_code == 502 and "OSM" in r.json()["detail"]
            respuestas.append(Resp(tipo="text/html", cuerpo=b"<html>bloqueado</html>"))
            assert c.get("/basemap/osm/10/301/600.png").status_code == 502
            respuestas.append(Resp(codigo=429))
            assert c.get("/basemap/osm/10/302/600.png").status_code == 502
            respuestas.append(Resp(cuerpo=b"0" * (basemap.MAX_BYTES + 1)))
            assert c.get("/basemap/osm/10/303/600.png").status_code == 502
            assert not any((Path(a.dir_out) / "cache_basemap" / "osm" / "10" / str(x) / "600.png").exists() for x in (300, 301, 302, 303))
            n = len(llamadas)
            for z, x, y in ((20, 0, 0), (3, 8, 0), (3, 0, 9)):               # fuera de rango: ni se pide a OSM
                assert c.get(f"/basemap/osm/{z}/{x}/{y}.png").status_code == 422, (z, x, y)
            assert len(llamadas) == n
            # el User-Agent sale de la configuración
            a.user_agent = "OtraApp/2.0 (contacto)"
            respuestas.append(Resp())
            c.get("/basemap/osm/9/150/300.png")
            assert llamadas[-1]["ua"] == "OtraApp/2.0 (contacto)"
    finally:
        basemap.requests.get = original
    print("  app basemap: OK")


def test_app_lamina():
    """A6 de Regu Suelo: POST /api/lamina devuelve el PDF de sig/, lo reutiliza, y avisa si no se puede ofrecer."""
    import pymupdf
    from fastapi.testclient import TestClient
    from app.main import crear_app
    from etl import teselas as T
    with tempfile.TemporaryDirectory() as d:
        a = _app_fixture(Path(d))
        c = TestClient(crear_app(a))
        r = c.post("/api/lamina", params={"cut": "09101"})
        assert r.status_code == 409 and "run.py mapa" in r.json()["detail"], r.text      # sin PMTiles regional del minimapa
        T.normativa(a.gpkg, Path(a.dir_out) / "mapa_araucania" / "capa_ipt.pmtiles")
        r = c.post("/api/lamina", params={"cut": "09101"})
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content[:4] == b"%PDF", r.text[:300]
        assert "lamina_comunal_09101.pdf" in r.headers["content-disposition"]
        txt = pymupdf.open(stream=r.content, filetype="pdf")[0].get_text()
        assert "Comuna de Temuco" in txt and "Información referencial" in txt and a.gpkg.stem in txt, txt[:400]
        pdf = Path(a.dir_out) / "laminas" / f"09101_{a.gpkg.stem}.pdf"
        t0 = pdf.stat().st_mtime
        assert c.post("/api/lamina", params={"cut": "09101"}).status_code == 200 and pdf.stat().st_mtime == t0, "se reutiliza"
        assert c.post("/api/lamina", params={"cut": "99999"}).status_code == 404
        assert c.post("/api/lamina", params={"cut": "abc"}).status_code == 422
        a.lamina_regiones = ("Región de Los Lagos",)                       # región no ofrecida mientras sig/ no esté integrado
        r = c.post("/api/lamina", params={"cut": "09101"})
        assert r.status_code == 409 and "codex/sig-layouts" in r.json()["detail"], r.text
        a.lamina_regiones = None
        assert c.post("/api/lamina", params={"cut": "09101"}).status_code == 200
    print("  app lamina: OK")


def test_temas_catalogo():
    """M1 de mapas temáticos: el catálogo real es válido y los contratos mal formados se rechazan con su motivo."""
    import copy
    import yaml
    from etl import temas as T
    real = T.cargar_catalogo()
    assert {"ubicacion_division", "clima_temperatura_media_anual", "suelo_arcilla_superficial", "relieve_altitud"} <= set(real)
    assert all(c["estado"] == "propuesta" or c["fuente"]["acceso"]["verificado"] for c in real.values()), "ninguno sale de propuesta sin Paso 0"
    assert not T.publicable(real["clima_temperatura_media_anual"]) and T.publicable(real["suelo_arcilla_superficial"])

    base = copy.deepcopy(real["clima_temperatura_media_anual"])
    base["estado"] = "propuesta"          # la base de las pruebas no depende de lo que esté activo en el catálogo real

    def con(**cambios):
        c = copy.deepcopy(base)
        for k, v in cambios.items():
            partes = k.split("__")
            d = c
            for p in partes[:-1]:
                d = d[p]
            if v == "__borrar__":
                d.pop(partes[-1], None)
            else:
                d[partes[-1]] = v
        return c

    def errores(c, archivo=None):
        return " | ".join(T.validar(c, archivo))

    assert T.validar(base, Path("clima_temperatura_media_anual.yaml")) == []
    assert "no coincide con el nombre del archivo" in errores(base, Path("otro.yaml"))
    assert "categoria" in errores(con(categoria="astronomia"))
    assert "additional properties" in errores(con(inventado=1)).lower() or "inventado" in errores(con(inventado=1))
    assert "estilo.rampa" in errores(con(estilo__rampa="__borrar__"))
    assert "crecientes" in errores(con(estilo__rampa=[[10, "#000000"], [0, "#ffffff"]]))
    assert "#rrggbb" in errores(con(estilo__rampa=[[0, "rojo"], [1, "#ffffff"]]))
    assert "mínimo supera" in errores(con(zoom=[9, 3]))
    assert "licencia_verificada: true exige" in errores(con(fuente__licencia_verificada=True, fuente__uso_comercial="por_verificar"))
    # reglas por estado: mapeada/activa exigen url, fecha, acceso verificado y generador
    m = con(estado="mapeada", fuente__acceso={"verificado": False}, generacion="__borrar__")
    e = errores(m)
    assert "acceso.verificado" in e and "generacion.generador" in e, e
    assert "url y fecha_dato" in errores(con(estado="activa", fuente__url="__borrar__", fuente__fecha_dato="__borrar__"))
    assert errores(con(estado="activa")) == "", "con todo lo exigido, activa es válida"
    vect = con(tipo="vector", estado="activa", estilo={"capa_origen": "x"})
    assert "capa_origen, estilo.tipo_capa y estilo.paint" in errores(vect)

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "temas").mkdir()
        for id_, c in (("a_tema", con(id="a_tema")), ("b_tema", con(id="b_tema", estado="activa"))):
            (d / "temas" / f"{id_}.yaml").write_text(yaml.safe_dump(c, allow_unicode=True), encoding="utf-8")
        cat = T.cargar_catalogo(d / "temas")
        assert list(cat) == ["a_tema", "b_tema"]
        (d / "temas" / "malo.yaml").write_text(yaml.safe_dump(con(id="malo", zoom=[9, 3])), encoding="utf-8")
        try:
            T.cargar_catalogo(d / "temas")
            raise AssertionError("debía rechazar el contrato inválido")
        except ValueError as ex:
            assert "malo.yaml" in str(ex) and "mínimo supera" in str(ex)
        (d / "temas" / "malo.yaml").unlink()
        # estado, vista pública y generación
        out, raw = d / "out", d / "raw"
        assert not T.estado(cat, out).generado.any()
        try:
            T.generar("a_tema", cat, raw, out, {})
            raise AssertionError("una propuesta no se genera")
        except ValueError as ex:
            assert "Paso 0" in str(ex)
        try:
            T.generar("b_tema", cat, raw, out, {})
            raise AssertionError("sin generador registrado no se genera")
        except ValueError as ex:
            assert "worldclim_bio" in str(ex) and "no está registrado" in str(ex)
        llamadas = []

        @T.generador("worldclim_bio")
        def _falso(c, dir_raw, destino, cfg):
            llamadas.append((c["id"], dir_raw, destino))
            destino.write_bytes(b"PMTiles-falso")
            return destino
        try:
            p = T.generar("b_tema", cat, raw, out, {})
        finally:
            T.GENERADORES.pop("worldclim_bio", None)
        assert p == out / "tiles" / "temas" / "b_tema.pmtiles" and llamadas[0][1] == raw / "temas" / "b_tema"
        est = T.estado(cat, out).set_index("id")
        assert bool(est.loc["b_tema", "generado"]) and not bool(est.loc["a_tema", "generado"]) and est.loc["b_tema", "mb"] is not None
        pub = T.publico(cat["b_tema"], out)
        assert pub["disponible"] and not pub["publicable"] and pub["fuente"]["atribucion"] and "generacion" not in pub
        assert str(d) not in json.dumps(pub), "la vista pública no revela rutas del disco"
        try:
            T.generar("no_existe", cat, raw, out, {})
            raise AssertionError("tema desconocido")
        except KeyError:
            pass
    print("  temas catálogo: OK")


def test_raster_tiles():
    """M2 de mapas temáticos: GeoTIFF → rampa → PMTiles. Colores por píxel, nodata, máscara, categorías, factor y UTM."""
    import mercantile
    import rasterio
    from rasterio.transform import from_bounds
    from etl import raster_tiles as R

    def px(lon, lat, z):                      # (x, y, fila, columna) del píxel de un punto en su tesela
        t = mercantile.tile(lon, lat, z)
        x0, y0, x1, y1 = mercantile.xy_bounds(t)
        x, y = mercantile.xy(lon, lat)
        return t.x, t.y, int((y1 - y) / (y1 - y0) * 256), int((x - x0) / (x1 - x0) * 256)

    def color(pm, lon, lat, z):
        x, y, i, j = px(lon, lat, z)
        img = R.leer_tesela(pm, z, x, y)
        return None if img is None else img[i, j]

    def escribir(ruta, datos, bounds, crs="EPSG:4326", nodata=None, dtype="float32"):
        h, w = datos.shape
        with rasterio.open(ruta, "w", driver="GTiff", height=h, width=w, count=1, dtype=dtype, crs=crs,
                           transform=from_bounds(*bounds, w, h), nodata=nodata) as dst:
            dst.write(datos.astype(dtype), 1)

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        rampa = [[0, "#000000"], [100, "#ff0000"]]
        # 1) gradiente lineal oeste→este de 0 a 100 en lon -75..-70, con un hueco nodata
        g = np.linspace(0, 100, 100)[None, :].repeat(100, 0).astype("float32")
        g[40:50, 40:50] = -9999                                           # hueco: lon -73,0..-72,5 y lat -37,0..-37,5
        escribir(d / "g.tif", g, (-75, -40, -70, -35), nodata=-9999)
        r = R.teselar_raster(d / "g.tif", d / "g.pmtiles", rampa, zoom=(3, 7), nombre="grad", atribucion="atrib X")
        h = R.encabezado(r["destino"])
        assert h["min_zoom"] == 3 and h["max_zoom"] == 7 and "PNG" in h["tile_type"] and h["metadata"]["attribution"] == "atrib X", h
        assert r["tiles"] > 5 and r["mb"] >= 0
        for lon in (-74.5, -73.0, -72.0, -70.6):
            c = color(r["destino"], lon, -38.5, 7)
            esperado = (lon + 75) / 5 * 100 * 2.55
            assert c[3] == 255 and abs(int(c[0]) - esperado) < 9 and c[1] == 0 and c[2] == 0, (lon, c, esperado)
        assert color(r["destino"], -72.75, -37.2, 7)[3] == 0, "nodata transparente"
        assert color(r["destino"], -80.0, -38.5, 7) is None or color(r["destino"], -80.0, -38.5, 7)[3] == 0, "fuera del ráster"
        # 2) máscara: solo la mitad oeste (lon < -72,5)
        r2 = R.teselar_raster(d / "g.tif", d / "g_m.pmtiles", rampa, zoom=(3, 7), mascara=box(-76, -41, -72.5, -34))
        assert color(r2["destino"], -74.0, -38.5, 7)[3] == 255 and (color(r2["destino"], -71.0, -38.5, 7) is None or color(r2["destino"], -71.0, -38.5, 7)[3] == 0)
        assert r2["tiles"] < r["tiles"], "las teselas fuera de la máscara no se escriben"
        # 3) categórico (escalón, vecino más cercano): clases 10, 20 y 30 en bloques; 0 = sin dato
        cat = np.zeros((90, 90), dtype="uint8")
        cat[:, :30], cat[:, 30:60], cat[:, 60:] = 10, 20, 30
        escribir(d / "c.tif", cat, (-75, -40, -72, -37), dtype="uint8", nodata=0)
        rampa_c = [[10, "#006400"], [20, "#ffbb22"], [30, "#ffff4c"]]
        r3 = R.teselar_raster(d / "c.tif", d / "c.pmtiles", rampa_c, interpolacion="escalon", zoom=(5, 7))
        assert list(color(r3["destino"], -74.5, -38.5, 7)[:3]) == [0, 100, 0] and list(color(r3["destino"], -73.5, -38.5, 7)[:3]) == [255, 187, 34]
        assert list(color(r3["destino"], -72.5, -38.5, 7)[:3]) == [255, 255, 76], "sin colores intermedios inventados"
        # 4) factor de unidades: 0..1000 con factor 0,1 equivale a 0..100
        escribir(d / "f.tif", g * 10, (-75, -40, -70, -35))
        r4 = R.teselar_raster(d / "f.tif", d / "f.pmtiles", rampa, factor=0.1, zoom=(5, 7))
        assert abs(int(color(r4["destino"], -72.5, -38.5, 7)[0]) - 127) < 9
        # 5) fuente en otra proyección (UTM 18S): valor constante 50 → rojo medio
        escribir(d / "u.tif", np.full((60, 60), 50, dtype="float32"), (700000, 5700000, 760000, 5760000), crs="EPSG:32718")
        r5 = R.teselar_raster(d / "u.tif", d / "u.pmtiles", rampa, zoom=(7, 8))
        c = color(r5["destino"], -72.1, -38.7, 8)
        assert c is not None and c[3] == 255 and abs(int(c[0]) - 127) < 3, c
        # 6) sin solapamiento entre la máscara y el ráster: error claro y sin archivos a medias
        try:
            R.teselar_raster(d / "g.tif", d / "x.pmtiles", rampa, zoom=(3, 5), mascara=box(10, 10, 11, 11))
            raise AssertionError("debía fallar")
        except ValueError as ex:
            assert "Ninguna tesela" in str(ex) or "solap" in str(ex).lower()
        assert not (d / "x.pmtiles").exists() and not list(d.glob("*.tmp"))
        # colorear: rampa por escalones y valores fuera de rango
        v = np.array([[5.0, 10.0, 15.0, 20.0, 99.0]])
        im = R.colorear(v, np.ones_like(v, dtype=bool), rampa_c, "escalon")
        assert [int(x) for x in im[0, :, 3]] == [0, 255, 255, 255, 255] and list(im[0, 4, :3]) == [255, 255, 76]
        im = R.colorear(np.array([[-50.0, 200.0]]), np.ones((1, 2), dtype=bool), rampa, "lineal")
        assert list(im[0, 0, :3]) == [0, 0, 0] and list(im[0, 1, :3]) == [255, 0, 0], "lineal se detiene en los extremos"
    print("  raster tiles: OK")


def test_temas_generadores_comunes():
    """M3: descarga con reanudación (servidor local con Range), máscara de Chile y generador de ubicación."""
    import http.server
    import socketserver
    import threading
    import pyogrio
    from etl import temas as T
    from etl.temas_gen import comun, division  # noqa: F401  (registra el generador)

    contenido = bytes(range(256)) * 4000                       # ≈ 1 MB
    visitas = {"get": 0, "rangos": []}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_HEAD(self):
            self.send_response(200 if self.path == "/dato.bin" else 404)
            self.send_header("Content-Length", str(len(contenido)))
            self.end_headers()

        def do_GET(self):
            visitas["get"] += 1
            if self.path != "/dato.bin":
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            rango = self.headers.get("Range")
            visitas["rangos"].append(rango)
            ini = int(rango.split("=")[1].split("-")[0]) if rango else 0
            self.send_response(206 if rango else 200)
            if rango:
                self.send_header("Content-Range", f"bytes {ini}-{len(contenido) - 1}/{len(contenido)}")
            self.send_header("Content-Length", str(len(contenido) - ini))
            self.end_headers()
            self.wfile.write(contenido[ini:])

    with socketserver.TCPServer(("127.0.0.1", 0), H) as srv:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/dato.bin"
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = comun.descargar(url, d / "sub" / "dato.bin")
            assert p.read_bytes() == contenido and visitas["get"] == 1 and visitas["rangos"] == [None]
            assert comun.descargar(url, p) == p and visitas["get"] == 1, "completo: no se vuelve a bajar"
            p.unlink()
            (d / "sub" / "dato.bin.parte").write_bytes(contenido[:300000])           # una descarga a medias
            p = comun.descargar(url, d / "sub" / "dato.bin")
            assert p.read_bytes() == contenido and visitas["rangos"][-1] == "bytes=300000-", visitas["rangos"]
            assert not (d / "sub" / "dato.bin.parte").exists()
            p.write_bytes(contenido[:10])                                              # archivo truncado: se rehace
            assert comun.descargar(url, p).read_bytes() == contenido
            g0 = visitas["get"]
            try:
                comun.descargar(url.replace("dato.bin", "no_existe.bin"), d / "x.bin")
                raise AssertionError("un 404 debe fallar")
            except FileNotFoundError as ex:
                assert "404" in str(ex)
            assert visitas["get"] == g0 and not (d / "x.bin").exists(), "un 404 no se reintenta ni deja archivos"
        srv.shutdown()

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        com = gpd.GeoDataFrame({"cod_comuna": [9101, 9102, 13101, 99], "Comuna": ["A", "B", "C", "Isla"],
                                "Region": ["Región de La Araucanía", "Región de La Araucanía", "Región Metropolitana de Santiago", "Región de Valparaíso"]},
                               geometry=[box(-72.6, -38.8, -72.4, -38.6), box(-72.4, -38.8, -72.2, -38.6), box(-70.8, -33.6, -70.5, -33.3),
                                         box(-109.5, -27.2, -109.3, -27.0)], crs=4326)
        com.to_file(d / "c.gpkg", driver="GPKG")
        cfg = {"paths": {"comunas": str(d / "c.gpkg")}, "comunas": {"field_cut": "cod_comuna", "field_nombre": "Comuna", "field_region": "Region"}}
        m = comun.mascara_chile(cfg)
        assert m.contains(shapely.geometry.Point(-72.5, -38.7)) and m.contains(shapely.geometry.Point(-109.4, -27.1))
        assert m.contains(shapely.geometry.Point(-72.43, -38.7)), "el margen cubre la costa generalizada"
        assert not m.contains(shapely.geometry.Point(-68.0, -38.7))
        cont = comun.mascara_chile(cfg, continental=True)
        assert not cont.contains(shapely.geometry.Point(-109.4, -27.1)) and cont.contains(shapely.geometry.Point(-70.6, -33.4)), "sin islas lejanas"
        reg = comun.mascara_chile(cfg, region="Araucan")
        assert reg.contains(shapely.geometry.Point(-72.5, -38.7)) and not reg.contains(shapely.geometry.Point(-70.6, -33.4))
        # el generador de ubicación: cut a 5 dígitos, un color por región, PMTiles vectorial con la capa pedida
        c = {"id": "u", "nombre": "Regiones y comunas", "estilo": {"capa_origen": "comunas"}, "zoom": [3, 8]}
        out = division.division(c, d, d / "u.pmtiles", cfg)
        assert out.read_bytes()[:7] == b"PMTiles" and "comunas" in [x[0] for x in pyogrio.list_layers(out)]
        assert division.color_region("09") != division.color_region("13") and division.color_region("00") == division.SIN_DEMARCAR
        assert len(set(division.COLORES)) == 16 == len(division.ORDEN)
    print("  temas generadores comunes: OK")


def test_temas_worldclim():
    """El generador de clima: baja el zip (aquí, uno falso con un GeoTIFF), extrae solo su variable y la tesela con la máscara."""
    import http.server
    import io
    import socketserver
    import threading
    import zipfile
    import rasterio
    from rasterio.transform import from_bounds
    from etl import raster_tiles as R
    from etl import temas as T
    from etl.temas_gen import worldclim

    def tif_bytes(valor):
        b = io.BytesIO()
        with rasterio.io.MemoryFile() as mf:
            with mf.open(driver="GTiff", height=40, width=40, count=1, dtype="float32", crs="EPSG:4326", transform=from_bounds(-73, -40, -71, -38, 40, 40), nodata=-3.4e38) as dst:
                dst.write(np.full((40, 40), valor, dtype="float32"), 1)
            b.write(mf.read())
        return b.getvalue()

    zbytes = io.BytesIO()
    with zipfile.ZipFile(zbytes, "w") as z:
        z.writestr("wc2.1_2.5m_bio_1.tif", tif_bytes(10.0))
        z.writestr("wc2.1_2.5m_bio_12.tif", tif_bytes(1000.0))
    cuerpo = zbytes.getvalue()
    visitas = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()

        def do_GET(self):
            visitas.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)

    with socketserver.TCPServer(("127.0.0.1", 0), H) as srv:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/wc2.1_2.5m_bio.zip"
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            gpd.GeoDataFrame({"cod_comuna": [9101], "Comuna": ["A"], "Region": ["Región de La Araucanía"]},
                             geometry=[box(-72.9, -39.9, -71.1, -38.1)], crs=4326).to_file(d / "c.gpkg", driver="GPKG")
            cfg = {"paths": {"comunas": str(d / "c.gpkg"), "raw": str(d / "raw"), "out": str(d / "out")},
                   "comunas": {"field_cut": "cod_comuna", "field_nombre": "Comuna", "field_region": "Region"}}
            base = {"nombre": "Prueba", "categoria": "clima", "estado": "mapeada", "tipo": "raster", "zoom": [5, 7],
                    "fuente": {"nombre": "WorldClim de prueba", "url": url, "fecha_dato": "1970-2000", "licencia": "Licencia de prueba", "licencia_verificada": False,
                               "atribucion": "Atribución de prueba", "acceso": {"verificado": True}},
                    "generacion": {"generador": "worldclim_bio"},
                    "estilo": {"interpolacion": "lineal", "rampa": [[0, "#000000"], [20, "#ff0000"]]}}
            t1 = {**base, "id": "t_bio1", "generacion": {"generador": "worldclim_bio", "parametros": {"archivo": "wc2.1_2.5m_bio_1.tif"}}}
            t2 = {**base, "id": "t_bio12", "estilo": {"interpolacion": "lineal", "factor": 0.02, "rampa": [[0, "#000000"], [20, "#ff0000"]]},
                  "generacion": {"generador": "worldclim_bio", "parametros": {"archivo": "wc2.1_2.5m_bio_12.tif"}}}
            cat = {"t_bio1": t1, "t_bio12": t2}
            assert all(T.validar(c) == [] for c in cat.values()), [T.validar(c) for c in cat.values()]
            p1 = T.generar("t_bio1", cat, d / "raw", d / "out", cfg)
            p2 = T.generar("t_bio12", cat, d / "raw", d / "out", cfg)
            assert len(visitas) == 1, "el zip compartido se baja una sola vez"
            assert (d / "raw" / "temas" / "_compartido" / "wc2.1_2.5m_bio.zip").exists() and (d / "raw" / "temas" / "t_bio1" / "wc2.1_2.5m_bio_1.tif").exists()
            assert not (d / "raw" / "temas" / "t_bio1" / "wc2.1_2.5m_bio_12.tif").exists(), "cada tema extrae solo su variable"
            assert R.encabezado(p1)["metadata"]["attribution"] == "Atribución de prueba"
            rojo = lambda p: int(R.leer_tesela(p, 7, *[getattr(__import__("mercantile").tile(-72.0, -39.0, 7), k) for k in ("x", "y")])[100, 100][0])  # noqa: E731
            assert abs(rojo(p1) - 128) < 4, "10 °C en una rampa 0..20 → rojo medio"
            assert rojo(p2) > 250, "1000 × factor 0,02 = 20: rojo pleno en la rampa 0..20"
            try:
                worldclim.extraer(d / "raw" / "temas" / "_compartido" / "wc2.1_2.5m_bio.zip", "no_existe.tif", d / "x")
                raise AssertionError("debía fallar")
            except FileNotFoundError as ex:
                assert "no_existe.tif" in str(ex)
        srv.shutdown()
    print("  temas worldclim: OK")


def test_temas_soilgrids():
    """Suelo: recorte por bandas de un ráster en otra proyección (como el VRT remoto) solo dentro de la máscara, y generador."""
    import mercantile
    import rasterio
    from pyproj import Transformer
    from rasterio.transform import from_bounds
    from etl import raster_tiles as R
    from etl.temas_gen import soilgrids as S

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # «Remoto»: UTM 18S, valor creciente hacia el este (0..999 g/kg), con un hueco nodata
        w = h = 100
        datos = np.tile(np.linspace(0, 999, w), (h, 1)).astype("int16")
        datos[40:60, 40:60] = S.NODATA                       # hueco nodata dentro de la máscara (≈ lon -72,0..-71,8; lat -39,1..-39,2)
        with rasterio.open(d / "origen.tif", "w", driver="GTiff", height=h, width=w, count=1, dtype="int16", crs="EPSG:32718",
                           transform=from_bounds(700000, 5600000, 800000, 5700000, w, h), nodata=S.NODATA) as dst:
            dst.write(datos, 1)
        mascara = box(-72.4, -39.4, -71.7, -38.75)
        out = S.recortar_ventana(d / "origen.tif", d / "raw" / "chile.tif", mascara, resolucion=0.01, banda_grados=0.2)
        assert out.exists() and not (d / "raw" / "chile.parte").exists()
        t0 = out.stat().st_mtime
        assert S.recortar_ventana(d / "origen.tif", out, mascara, resolucion=0.01, banda_grados=0.2).stat().st_mtime == t0, "no se repite"
        a_utm = Transformer.from_crs(4326, 32718, always_xy=True)
        with rasterio.open(out) as r, rasterio.open(d / "origen.tif") as o:
            assert r.crs.to_epsg() == 4326 and r.nodata == S.NODATA and r.dtypes[0] == "int16" and abs(r.res[0] - 0.01) < 1e-9
            ancho_ok = 0
            for lon, lat in ((-72.2, -38.9), (-71.9, -39.3), (-72.3, -39.2)):
                x, y = a_utm.transform(lon, lat)
                esperado = next(o.sample([(x, y)]))[0]
                obtenido = next(r.sample([(lon, lat)]))[0]
                if esperado != S.NODATA:
                    assert abs(int(obtenido) - int(esperado)) < 25, (lon, lat, obtenido, esperado)
                    ancho_ok += 1
            assert ancho_ok >= 2
            # fuera de la máscara no se escribe nada: queda nodata (aunque el origen sí tenga datos)
            assert next(r.sample([(-72.0, -38.72)]))[0] == S.NODATA and next(r.sample([(-72.6, -39.0)]))[0] == S.NODATA, "dentro del origen pero fuera de la máscara"
            # el hueco nodata del origen sigue siendo nodata
            hx, hy = o.xy(50, 50)
            lo, la = Transformer.from_crs(32718, 4326, always_xy=True).transform(hx, hy)
            assert mascara.contains(shapely.geometry.Point(lo, la)) and next(r.sample([(lo, la)]))[0] == S.NODATA
        # generador completo: factor 0,1 (g/kg → %), rampa 0..100, atribución en el PMTiles
        orig = S.recortar_ventana
        S.recortar_ventana = lambda origen, destino, mascara_, **kw: orig(d / "origen.tif", destino, mascara_, resolucion=0.01, banda_grados=0.2)
        try:
            gpd.GeoDataFrame({"cod_comuna": [9101], "Comuna": ["A"], "Region": ["Región de La Araucanía"]},
                             geometry=[mascara], crs=4326).to_file(d / "c.gpkg", driver="GPKG")
            cfg = {"paths": {"comunas": str(d / "c.gpkg")}, "comunas": {"field_cut": "cod_comuna", "field_nombre": "Comuna", "field_region": "Region"}}
            c = {"id": "s", "nombre": "Arcilla", "zoom": [6, 9], "fuente": {"url": "https://ejemplo.cl/clay.vrt", "atribucion": "ISRIC prueba"},
                 "generacion": {"parametros": {"variable": "clay", "profundidad": "0-5cm"}},
                 "estilo": {"factor": 0.1, "interpolacion": "lineal", "rampa": [[0, "#000000"], [100, "#ff0000"]]}}
            pm = S.soilgrids(c, d / "raw2", d / "s.pmtiles", cfg)
        finally:
            S.recortar_ventana = orig
        assert R.encabezado(pm)["metadata"]["attribution"] == "ISRIC prueba"
        lon, lat = -72.2, -38.9
        x, y = a_utm.transform(lon, lat)
        with rasterio.open(d / "origen.tif") as o:
            v = int(next(o.sample([(x, y)]))[0])
        t = mercantile.tile(lon, lat, 9)
        x0, y0, x1, y1 = mercantile.xy_bounds(t)
        mx, my = mercantile.xy(lon, lat)
        img = R.leer_tesela(pm, 9, t.x, t.y)
        px = img[int((y1 - my) / (y1 - y0) * 256), int((mx - x0) / (x1 - x0) * 256)]
        assert px[3] == 255 and abs(int(px[0]) - v * 0.1 * 2.55) < 12, (px, v)
    print("  temas soilgrids: OK")


def test_temas_relieve():
    """Relieve: nombres de teselas GLO-30, sombreado de Horn (valores a mano), por bloques, parámetro del teselador y generador."""
    import http.server
    import math
    import mercantile
    import rasterio
    import socketserver
    import threading
    from rasterio.transform import from_bounds
    from etl import raster_tiles as R
    from etl.temas_gen import relieve as V

    # 1) nombres de teselas: la esquina suroeste da el nombre
    n = V.nombres_teselas(-73.5, -39.7, -70.8, -37.5)
    assert len(n) == 12 and "Copernicus_DSM_COG_10_S39_00_W073_00_DEM" in n and "Copernicus_DSM_COG_10_S40_00_W074_00_DEM" in n
    assert "Copernicus_DSM_COG_10_S38_00_W071_00_DEM" in n and not any("W070" in x for x in n)
    assert V.nombres_teselas(10.2, 45.1, 11.5, 46.0) == ["Copernicus_DSM_COG_10_N45_00_E010_00_DEM", "Copernicus_DSM_COG_10_N45_00_E011_00_DEM"]

    # 2) sombreado: plano horizontal = sin(altura); plano que sube al este (30°) con sol del oeste (270°) o del este (90°)
    dx = np.full(20, 30.0)
    plano = np.zeros((20, 20))
    assert np.allclose(V.sombreado(plano, dx, 30.0, altura=45.0), math.sin(math.radians(45)), atol=1e-6)
    rampa_e = np.tile(np.arange(20) * 30.0 * math.tan(math.radians(30)), (20, 1))
    oeste = V.sombreado(rampa_e, dx, 30.0, azimut=270.0, altura=45.0)[10, 10]
    este = V.sombreado(rampa_e, dx, 30.0, azimut=90.0, altura=45.0)[10, 10]
    assert abs(oeste - 0.7071 * (math.cos(math.radians(30)) + math.sin(math.radians(30)))) < 1e-3 and abs(oeste - 0.966) < 2e-3, oeste
    assert abs(este - 0.7071 * (math.cos(math.radians(30)) - math.sin(math.radians(30)))) < 1e-3 and abs(este - 0.259) < 2e-3, este
    sube_norte = np.tile((np.arange(20) * 30.0 * math.tan(math.radians(30)))[::-1, None], (1, 20))     # fila 0 = norte = más alto
    assert V.sombreado(sube_norte, dx, 30.0, azimut=180.0, altura=45.0)[10, 10] > 0.95, "ladera que mira al sur con sol del sur"
    con_hueco = rampa_e.copy()
    con_hueco[5, 5] = np.nan
    s2 = V.sombreado(con_hueco, dx, 30.0)
    assert np.isnan(s2[5, 5]) and np.isfinite(s2[10, 10]) and s2.dtype == np.float32 and 0 <= np.nanmin(s2) and np.nanmax(s2) <= 1

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # 3) por bloques = de una vez (el traslape de una fila evita costuras)
        z = (np.add.outer(np.arange(60) * 3.0, np.sin(np.arange(60) / 4.0) * 40)).astype("int16")
        with rasterio.open(d / "dem.tif", "w", driver="GTiff", height=60, width=60, count=1, dtype="int16", crs="EPSG:4326",
                           transform=from_bounds(-72.5, -38.6, -72.38, -38.48, 60, 60), nodata=V.NODATA) as dst:
            dst.write(z, 1)
        uno = V.generar_sombreado(d / "dem.tif", d / "s1.tif", bloque=1000)
        varios = V.generar_sombreado(d / "dem.tif", d / "s2.tif", bloque=16)
        with rasterio.open(uno) as a, rasterio.open(varios) as b:
            assert np.allclose(a.read(1), b.read(1), atol=1e-6) and a.nodata == -1.0
        assert V.generar_sombreado(d / "dem.tif", uno).stat().st_mtime == uno.stat().st_mtime, "no se repite"

        # 4) teselador con sombreado: elevación constante (rojo medio) con sombra 0 al oeste y luz 1 al este (intensidad 0,5)
        for nombre, datos in (("e.tif", np.full((40, 40), 50.0, dtype="float32")), ("l.tif", np.tile(np.array([0.0] * 20 + [1.0] * 20, dtype="float32"), (40, 1)))):
            with rasterio.open(d / nombre, "w", driver="GTiff", height=40, width=40, count=1, dtype="float32", crs="EPSG:4326",
                               transform=from_bounds(-73, -39, -72, -38, 40, 40), nodata=-1.0 if nombre == "l.tif" else None) as dst:
                dst.write(datos, 1)
        rampa = [[0, "#000000"], [100, "#ff0000"]]
        r = R.teselar_raster(d / "e.tif", d / "e.pmtiles", rampa, zoom=(7, 7), sombreado=d / "l.tif", intensidad=0.5)
        r0 = R.teselar_raster(d / "e.tif", d / "e0.pmtiles", rampa, zoom=(7, 7))

        def rojo(pm, lon):
            t = mercantile.tile(lon, -38.5, 7)
            x0, y0, x1, y1 = mercantile.xy_bounds(t)
            mx, my = mercantile.xy(lon, -38.5)
            return int(R.leer_tesela(pm, 7, t.x, t.y)[int((y1 - my) / (y1 - y0) * 256), int((mx - x0) / (x1 - x0) * 256)][0])
        assert abs(rojo(r0["destino"], -72.75) - 127) < 3 and abs(rojo(r["destino"], -72.75) - 64) < 4, "sombra 0 × intensidad 0,5 → mitad"
        assert abs(rojo(r["destino"], -72.25) - 127) < 3, "luz 1 → sin oscurecer"

        # 5) generador completo con un servidor de teselas falso: faltan las filas S40 y S38 (404 → se omiten)
        def tile_bytes(lon0):
            with rasterio.io.MemoryFile() as mf:
                with mf.open(driver="GTiff", height=100, width=100, count=1, dtype="int16", crs="EPSG:4326", transform=from_bounds(lon0, -39, lon0 + 1, -38, 100, 100), nodata=V.NODATA) as dst:
                    dst.write((np.tile(np.arange(100) * 10, (100, 1)) + 500).astype("int16"), 1)      # sube hacia el este: 500..1490 m
                return mf.read()
        sirve = {"Copernicus_DSM_COG_10_S39_00_W073_00_DEM": tile_bytes(-73), "Copernicus_DSM_COG_10_S39_00_W072_00_DEM": tile_bytes(-72)}
        pedidos = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _nombre(self):
                return self.path.strip("/").split("/")[0]

            def do_HEAD(self):
                c = sirve.get(self._nombre())
                self.send_response(200 if c else 404)
                if c:
                    self.send_header("Content-Length", str(len(c)))
                self.end_headers()

            def do_GET(self):
                pedidos.append(self.path)
                c = sirve.get(self._nombre())
                self.send_response(200 if c else 404)
                self.send_header("Content-Length", str(len(c) if c else 0))
                self.end_headers()
                if c:
                    self.wfile.write(c)

        with socketserver.TCPServer(("127.0.0.1", 0), H) as srv:
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base0, res0 = V.BASE, V.RES
            V.BASE, V.RES = f"http://127.0.0.1:{srv.server_address[1]}", 0.01
            try:
                gpd.GeoDataFrame({"cod_comuna": [9101], "Comuna": ["A"], "Region": ["Región de La Araucanía"]},
                                 geometry=[box(-72.9, -39.3, -71.1, -37.7)], crs=4326).to_file(d / "c.gpkg", driver="GPKG")
                cfg = {"paths": {"comunas": str(d / "c.gpkg"), "raw": str(d / "raw")}, "comunas": {"field_cut": "cod_comuna", "field_nombre": "Comuna", "field_region": "Region"}}
                c = {"id": "rel", "nombre": "Relieve", "zoom": [8, 9], "fuente": {"atribucion": "Copernicus de prueba"},
                     "generacion": {"parametros": {"region": "Araucan", "azimut": 315, "altura_sol": 45, "exageracion": 20.0, "intensidad": 0.6}},
                     "estilo": {"interpolacion": "lineal", "rampa": [[0, "#000000"], [2000, "#ff0000"]]}}
                pm = V.glo30(c, d / "rawtema", d / "rel.pmtiles", cfg)
            finally:
                V.BASE, V.RES = base0, res0
            srv.shutdown()
        assert R.encabezado(pm)["metadata"]["attribution"] == "Copernicus de prueba"
        assert (d / "rawtema" / "dem.tif").exists() and (d / "rawtema" / "sombreado.tif").exists()
        assert (d / "raw" / "temas" / "_compartido" / "glo30" / "Copernicus_DSM_COG_10_S39_00_W073_00_DEM.tif").exists()
        assert not list((d / "raw" / "temas" / "_compartido" / "glo30").glob("*S40*")), "las teselas que no existen se omiten"
        t = mercantile.tile(-72.0, -38.5, 9)
        img = R.leer_tesela(pm, 9, t.x, t.y)
        assert img is not None and img[100, 100][3] == 255
        # la ladera sube al este (mira al oeste) y el sol viene del noroeste: más luz que un plano → el color queda cerca del de la rampa
        assert int(img[100, 100][0]) > 0
    print("  temas relieve: OK")


def test_temas_worldcover():
    """Cobertura: nombres y selección de teselas de 3°, mosaico reducido con `mode` (sin clases inventadas) y generador."""
    import mercantile
    import rasterio
    from rasterio.transform import from_bounds
    from etl import raster_tiles as R
    from etl.temas_gen import worldcover as W

    assert W.tesela_url(-39, -72).endswith("ESA_WorldCover_10m_2021_v200_S39W072_Map.tif") and W.tesela_url(3, 6).endswith("N03E006_Map.tif")
    assert set(W.teselas_necesarias(box(-73.5, -39.7, -70.8, -37.5))) == {(-42, -75), (-42, -72), (-39, -75), (-39, -72)}
    assert W.teselas_necesarias(box(-74.9, -38.9, -73.0, -37.0)) == [(-39, -75)], "solo las que tocan la máscara"
    assert set(W.teselas_necesarias(box(-72.1, -39.1, -71.9, -38.9))) == {(-42, -75), (-42, -72), (-39, -75), (-39, -72)}, "cruza la esquina común de cuatro teselas"
    assert W.teselas_necesarias(box(-71.9, -38.9, -71.5, -38.5)) == [(-39, -72)], "dentro de una sola tesela"

    def tesela(ruta, lat0, lon0, patron):
        """Tesela de 3° con 60×60 px (0,05°): cada bloque de 2×2 lleva una clase mayoritaria (3 de 4) y una distinta."""
        a = np.zeros((60, 60), dtype="uint8")
        for i in range(0, 60, 2):
            for j in range(0, 60, 2):
                mayor, menor = patron(i // 2, j // 2)
                a[i:i + 2, j:j + 2] = mayor
                a[i + 1, j + 1] = menor
        with rasterio.open(ruta, "w", driver="GTiff", height=60, width=60, count=1, dtype="uint8", crs="EPSG:4326",
                           transform=from_bounds(lon0, lat0, lon0 + 3, lat0 + 3, 60, 60), nodata=0) as dst:
            dst.write(a, 1)

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        tesela(d / "a.tif", -39, -72, lambda i, j: (10, 80))            # bosque, con un pixel de agua por bloque
        tesela(d / "b.tif", -39, -75, lambda i, j: (40, 50))            # cultivos, con un pixel construido
        fuentes = {(-39, -72): d / "a.tif", (-39, -75): d / "b.tif", (-42, -72): d / "no_existe.tif"}
        out = W.mosaico_decimado(fuentes, d / "m.tif", factor=2)
        with rasterio.open(out) as m:
            assert m.crs.to_epsg() == 4326 and m.dtypes[0] == "uint8" and m.nodata == 0 and abs(m.res[0] - 0.1) < 1e-9
            assert m.bounds.left == -75 and m.bounds.right == -69 and m.bounds.top == -36 and m.bounds.bottom == -42
            assert set(np.unique(m.read(1)).tolist()) <= {0, 10, 40}, "mode: nunca aparecen 80 ni 50 ni valores intermedios"
            assert next(m.sample([(-70.5, -37.5)]))[0] == 10 and next(m.sample([(-73.5, -37.5)]))[0] == 40
            assert next(m.sample([(-70.5, -40.5)]))[0] == 0, "la tesela faltante queda sin dato"
        assert W.mosaico_decimado(fuentes, out, factor=2).stat().st_mtime == out.stat().st_mtime, "no se repite"
        try:
            W.mosaico_decimado({(-39, -72): d / "nada.tif"}, d / "z.tif", factor=2)
            raise AssertionError("sin teselas debe fallar")
        except RuntimeError as ex:
            assert "Ninguna tesela" in str(ex)
        assert not (d / "z.parte").exists() and not (d / "z.tif").exists()

        # generador completo: máscara de un cuadro dentro de la tesela a; clases con la rampa categórica
        gpd.GeoDataFrame({"cod_comuna": [9101], "Comuna": ["A"], "Region": ["Región de La Araucanía"]},
                         geometry=[box(-71.5, -38.5, -70.5, -37.5)], crs=4326).to_file(d / "c.gpkg", driver="GPKG")
        cfg = {"paths": {"comunas": str(d / "c.gpkg")}, "comunas": {"field_cut": "cod_comuna", "field_nombre": "Comuna", "field_region": "Region"}}
        c = {"id": "cob", "nombre": "Cobertura", "zoom": [6, 8], "fuente": {"atribucion": "ESA de prueba"},
             "generacion": {"parametros": {"factor": 2}},
             "estilo": {"rampa": [[10, "#006400"], [20, "#ffbb22"], [40, "#f096ff"]], "interpolacion": "escalon"}}
        pref, url0 = W.PREFIJO, W.tesela_url
        W.PREFIJO = ""
        W.tesela_url = lambda lat0, lon0: str({(-39, -72): d / "a.tif", (-39, -75): d / "b.tif"}.get((lat0, lon0), d / "no.tif"))
        try:
            pm = W.worldcover(c, d / "raw", d / "cob.pmtiles", cfg)
        finally:
            W.PREFIJO, W.tesela_url = pref, url0
        assert R.encabezado(pm)["metadata"]["attribution"] == "ESA de prueba"
        t = mercantile.tile(-71.0, -38.0, 8)
        x0, y0, x1, y1 = mercantile.xy_bounds(t)
        mx, my = mercantile.xy(-71.0, -38.0)
        px = R.leer_tesela(pm, 8, t.x, t.y)[int((y1 - my) / (y1 - y0) * 256), int((mx - x0) / (x1 - x0) * 256)]
        assert list(px) == [0, 100, 0, 255], px
    print("  temas worldcover: OK")


def _predios_sucios(origen: Path):
    """Respaldos sintéticos «sucios» como los de catastral.cl: todo texto, código SII ≠ CUT, huérfanos, texto corrido y errores."""
    from shapely.geometry import Polygon
    origen.mkdir(parents=True, exist_ok=True)
    lon0, lat0, dx, dy = -72.5950, -38.7350, 0.00023, 0.00027

    def caja(i, j, ancho=1, alto=1):
        return box(lon0 + i * dx, lat0 + j * dy, lon0 + (i + ancho) * dx, lat0 + (j + alto) * dy)

    def fila(geom, rol, cod_dest="H", cod_ubic="U", terreno="250.0", sup0="0.0", direccion="YELCHO 01670", metodo="1_contains", ok="True", **extra):
        man, pre = (rol.split("-") if rol else (None, None))
        return {"_ok": ok, "comuna": "9201" if rol else None, "manzana": man, "predio": pre, "rol": rol, "nombreComuna": "TEMUCO" if rol else None,
                "direccion_sii": direccion, "dc_cod_destino": cod_dest, "dc_cod_ubicacion": cod_ubic, "dc_sup_terreno": terreno, "supTerreno": sup0,
                "sup_construida_total": "80", "pisos_max": "2", "anio_construccion_min": "1990", "anio_construccion_max": "1995",
                "_match_method": metodo, "valorTotal": "12345678", "destinoDescripcion": "1", "ubicacion": None, "periodo": "327",
                "geometry": geom, **extra}
    bow = Polygon([(lon0 + 6 * dx, lat0), (lon0 + 7 * dx, lat0 + dy), (lon0 + 7 * dx, lat0), (lon0 + 6 * dx, lat0 + dy)])   # corbata: inválida
    filas = [
        fila(caja(0, 0), "01733-00022", periodo="PRIMER SEMESTRE DE 2026"),                                    # A: limpio (texto corrido)
        fila(caja(1, 0), "01733-00023", cod_dest="W", cod_ubic="R", terreno="nan", sup0="300.5", direccion="12345", metodo="4_manzana_d3"),  # B
        fila(caja(2, 0), None, cod_dest=None, cod_ubic=None, terreno=None, sup0=None, direccion=None, metodo="orphan_polygon", ok=None),  # C: huérfano
        fila(caja(3, 0), "01734-00001", ok="False", direccion="ConnectionError(MaxRetryError('www4.sii.cl'))"),    # D: _ok falso con rol
        {**fila(caja(0, 2), "01737-00001"), "comuna": "105"},                                                  # G: código SII corrupto en la fila
        fila(caja(0, 1), "01735-00005"),                                                                       # E1: rol repetido, exacto
        fila(caja(1, 1), "01735-00005", metodo="6_manzana_any"),                                               # E2: mismo rol, por manzana
        fila(bow, "01736-00001", cod_dest="Z"),                                                                # F: geometría inválida
        fila(box(-72.5595, -38.7200, -72.5593, -38.7198), "01738-00001"),                                      # H: fuera de la caja BCN de la comuna (costa generalizada)
        fila(caja(2, 2), "01740-00001", direccion="EDIFICIO 1 DEPTO 1"),                                       # I1 e I2: copropiedad (dos roles, un polígono)
        fila(caja(2, 2), "01740-00002", direccion="EDIFICIO 1 DEPTO 2", terreno="900.0"),
        fila(caja(2, 2), "01740-00001", direccion="EDIFICIO 1 DEPTO 1"),                                       # I3: duplicado exacto de I1 (mismo rol y polígono)
        # J: un rol exacto y dos asignados por cercanía al mismo polígono; K: dos roles solo por cercanía (no hay certeza de copropiedad)
        fila(caja(4, 2), "01741-00001", lat="-38.73425", lon="-72.59395"),
        fila(caja(4, 2), "01741-00002", metodo="2_nearest", lat="-38.73430", lon="-72.59390"),
        fila(caja(4, 2), "01741-00003", metodo="2_nearest", lat="-38.73421", lon="-72.59388"),
        fila(caja(5, 2), "01742-00001", metodo="2_nearest", lat="-38.73430", lon="-72.59380"),
        fila(caja(5, 2), "01742-00002", metodo="2_nearest", lat="-38.73430", lon="-72.59366"),
    ]
    gdf = gpd.GeoDataFrame(filas, geometry="geometry", crs=4326)
    gdf.to_file(origen / "9201_9201.gpkg", layer="comuna=9201", driver="GPKG")                                 # código SII 9201 = CUT 09101
    # un archivo con nombre engañoso: su contenido está en la comuna 09112 aunque dice «Puerto_Montt_10101» y «VALDIVIA»
    otro = gpd.GeoDataFrame([{**fila(box(-72.60, -38.80, -72.5995, -38.7995), "00100-00001"), "comuna": "10101", "nombreComuna": "VALDIVIA"}],
                            geometry="geometry", crs=4326)
    otro.to_file(origen / "Puerto_Montt_10101.gpkg", layer="comuna=10101", driver="GPKG")
    fuera = gpd.GeoDataFrame([fila(box(-60, -30, -59.9, -29.9), "00001-00001")], geometry="geometry", crs=4326)
    fuera.to_file(origen / "Lejos_9999.gpkg", layer="comuna=9999", driver="GPKG")
    return origen


def _bcn_predios():
    return gpd.GeoDataFrame({"cod_comuna": [9101, 9112], "Comuna": ["Temuco", "Padre las Casas"]},
                            geometry=[box(-72.62, -38.76, -72.56, -38.70), box(-72.62, -38.82, -72.56, -38.76)], crs=4326)


def test_predios_conversion():
    """Respaldo del SII sucio → GeoParquet limpio: CUT por geometría, códigos (no texto corrido), sin avalúo, calidad marcada."""
    import pandas as pd
    from etl import predios as P
    assert P.normalizar_rol("1733-22") == "01733-00022" == P.normalizar_rol("01733-00022") == P.normalizar_rol(" 1733 / 22 ") == P.normalizar_rol("1733 22")
    assert P.normalizar_rol("abc") is None and P.normalizar_rol("1733") is None and P.normalizar_rol("123456-1") is None
    assert set(P.DESTINOS) >= {"H", "A", "W", "Z", "L", "B"} and P.DESTINOS["B"] == "AGRICOLA POR ASIMILACION" and P.DESTINOS["L"] == "BODEGA Y ALMACENAJE"
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        origen = _predios_sucios(d / "Respaldo")
        m = P.convertir_todo(origen, d / "pq", _bcn_predios(), "cod_comuna", "Comuna", min_participacion=0.8).set_index("archivo")
        t = m.loc["9201_9201.gpkg"]
        assert t.cut == "09101" and t.comuna_bcn == "Temuco" and t.cod_sii == "9201" and t.participacion_cut == 0.941 and t.coincide_nombre, "el CUT sale de la geometría"
        assert t.n == 16 and t.n_datos_sii == 14 and t.n_huerfanos == 1 and t.n_exactos == 9 and t.n_ids_repetidos == 1 and t.estado == "ok", t.to_dict()
        assert t.n_duplicados == 1 and t.n_roles == 13 and t.n_terrenos_copropiedad == 1 and t.n_roles_copropiedad == 2, "duplicado exacto fuera; 1 terreno con 2 roles"
        v = m.loc["Puerto_Montt_10101.gpkg"]
        assert v.cut == "09112" and v.cod_sii == "10101" and not v.coincide_nombre and v.nombre_en_datos == "VALDIVIA", "nombre engañoso: manda la geometría"
        m2 = P.convertir_todo(origen, d / "pq2", _bcn_predios(), "cod_comuna", "Comuna", min_participacion=0.95).set_index("archivo")
        assert m2.loc["9201_9201.gpkg", "estado"].startswith("sin CUT claro") and not (d / "pq2" / "09101.parquet").exists(), "bajo el umbral de participación no se asigna CUT"
        assert m.loc["Lejos_9999.gpkg", "estado"].startswith("sin CUT claro") and not (d / "pq" / "9999.parquet").exists()
        assert (d / "pq" / "09101.parquet").exists() and (d / "pq" / "manifiesto_predios.csv").exists()
        g = gpd.read_parquet(d / "pq" / "09101.parquet").set_index("rol", drop=False)
        assert list(g.columns) == P.COLUMNAS or set(g.columns) == set(P.COLUMNAS)
        assert not any("valor" in c.lower() or "avaluo" in c.lower() or "propiet" in c.lower() for c in g.columns), "ni avalúo ni propietarios"
        a = g.loc["01733-00022"]
        assert a.id == "09101-01733-00022" and a.cut == "09101" and a.cod_sii == "9201" and a.manzana == "01733" and a.predio == "00022"
        assert a.destino == "HABITACIONAL" and a.destino_cod == "H" and a.ubicacion == "URBANA", "salen de los códigos, no del texto corrido"
        assert a.sup_terreno_m2 == 250.0 and a.sup_construida_m2 == 80 and a.pisos_max == 2 and a.anio_construccion == 1990
        assert a.direccion == "YELCHO 01670" and a.metodo == "contiene" and a.exacto and a.datos_sii and 500 < a.area_poligono_m2 < 800
        assert a.periodo_sii == "2026-1" and pd.isna(g.loc["01733-00023"].periodo_sii), "periodo legible → «2026-1»; el corrido («327») → nulo"
        assert t.periodo == "2026-1" and 0 < t.pct_periodo < 1, "el manifiesto trae el semestre más frecuente de la comuna"
        assert [P.normalizar_periodo(x) for x in ("SEGUNDO SEMESTRE DE 2025", " primer semestre de 2026 ", "327", None, "PRIMER SEMESTRE DE 1999")] == ["2025-2", "2026-1", None, None, None]
        b = g.loc["01733-00023"]
        assert b.destino == "SITIO ERIAZO" and b.ubicacion == "RURAL" and b.sup_terreno_m2 == 300.5, "terreno: usa supTerreno si dc_sup_terreno falta"
        assert pd.isna(b.direccion) and b.metodo == "manzana" and not b.exacto and b.datos_sii, "dirección numérica = columna corrida → nula"
        dd = g.loc["01734-00001"]
        assert not dd.datos_sii and pd.isna(dd.direccion) and pd.isna(dd.destino) and dd.rol == "01734-00001", "_ok falso: sin atributos, conserva el rol"
        h = g[g["metodo"] == "huerfano"].iloc[0]
        assert pd.isna(h.id) and pd.isna(h.rol) and not h.datos_sii and pd.isna(h.destino) and h.area_poligono_m2 > 0
        assert len(g.loc[["01735-00005"]]) == 2 and g.loc["01736-00001"].geometry.is_valid, "la geometría inválida se repara"
        assert g.loc["01736-00001"].destino == "ESTACIONAMIENTO"
        assert (g["cod_sii"] == "9201").all() and g.loc["01737-00001"].cod_sii == "9201", "el código SII es el de la capa, no el de la fila"
        assert g.crs.to_epsg() == 4326
        i1, i2 = g.loc["01740-00001"], g.loc["01740-00002"]
        assert not isinstance(i1, gpd.GeoDataFrame), "el duplicado exacto (mismo rol y polígono) se quitó"
        assert i1.id_poligono == i2.id_poligono and i1.n_unidades == i2.n_unidades == 2, "dos roles, un polígono = copropiedad"
        assert g.loc["01733-00022"].n_unidades == 1 and (g[g["metodo"] == "huerfano"].n_unidades == 0).all()
        j = g[g.rol.str.startswith("01741", na=False)].set_index("rol")
        assert (j.n_asignados == 3).all() and (j.n_unidades == 1).all(), "n_unidades del polígono cuenta solo los roles exactos (1); n_asignados, todos (3)"
        assert g.loc["01733-00023"].n_unidades == 0 and g.loc["01733-00023"].n_asignados == 1, "el asignado por cercanía no es unidad"
        assert abs(j.loc["01741-00002", "lat_sii"] + 38.7343) < 1e-6 and abs(j.loc["01741-00002", "lon_sii"] + 72.5939) < 1e-6 and pd.isna(g.loc["01733-00022"].lat_sii)
        assert g.loc["01733-00022"].id_poligono != i1.id_poligono and (g["origen"] == "Respaldo").all(), "origen = carpeta del respaldo"
    print("  predios conversión: OK")


def test_predios_parquet():
    """Respaldos en GeoParquet (segundo proceso de emparejamiento): otro vocabulario de métodos, filas sin polígono, y se SUMAN a lo
    ya convertido sin pisarlo."""
    import pandas as pd
    from etl import predios as P
    lon0, lat0, dx, dy = -72.5950, -38.7350, 0.00023, 0.00027
    cols = {"_ok": "True", "comuna": "5301", "manzana": "00001", "predio": "00001", "nombreComuna": "TEMUCO", "direccion_sii": "ALTO 10",
            "dc_cod_destino": "H", "dc_cod_ubicacion": "U", "dc_sup_terreno": "200", "supTerreno": "0", "sup_construida_total": "60",
            "pisos_max": "1", "anio_construccion_min": "2001", "anio_construccion_max": "2001", "periodo": "PRIMER SEMESTRE DE 2026"}

    def fila(i, rol, metodo, geom=True):
        return {**cols, "rol": rol, "_match_method": metodo, "geometry": box(lon0 + i * dx, lat0, lon0 + (i + 1) * dx, lat0 + dy) if geom else None}
    filas = [fila(0, "00001-00001", "point_in_polygon"), fila(1, "00001-00002", "nearest_10m"), fila(2, "00001-00003", "ah_utm_pip"),
             fila(3, "00001-00004", "address_inheritance"), fila(4, "", "unmatched_polygon"), fila(5, "00001-00006", "", geom=False)]
    filas[4].update(_ok=None, dc_cod_destino=None, direccion_sii=None)
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        origen = _predios_sucios(d / "Respaldo")
        P.convertir_todo(origen, d / "pq", _bcn_predios(), "cod_comuna", "Comuna", min_participacion=0.8)      # el respaldo en gpkg
        (d / "Valpo").mkdir()
        gpd.GeoDataFrame(filas, geometry="geometry", crs=4326).to_parquet(d / "Valpo" / "Cualquiera_9777.parquet")
        gpd.GeoDataFrame(filas[:1], geometry="geometry", crs=4326).to_parquet(d / "Valpo" / "Otro_9778.parquet")   # mismo CUT que el anterior
        m = P.convertir_todo(d / "Valpo", d / "pq", _bcn_predios(), "cod_comuna", "Comuna", min_participacion=0.8, patron="*.parquet").set_index("archivo")
        assert {"9201_9201.gpkg", "Cualquiera_9777.parquet"} <= set(m.index), "suma al manifiesto, no lo reemplaza"
        assert m.loc["9201_9201.gpkg", "estado"] == "ok" and m.loc["9201_9201.gpkg", "n"] == 16, "lo anterior queda intacto"
        v = m.loc["Cualquiera_9777.parquet"]
        assert str(v.cut).zfill(5) == "09101" or v.estado.startswith("CUT 09101 ya cubierto"), v.to_dict()
        assert m.loc["Otro_9778.parquet", "estado"].startswith("CUT 09101 ya cubierto por 9201_9201.gpkg"), "no pisa lo ya convertido"
        g = gpd.read_parquet(d / "pq" / "09101.parquet")
        assert len(g) == 16 and set(g.cod_sii) == {"9201"}, "el parquet del respaldo anterior sigue igual"
        # el mismo archivo en un destino limpio: vocabulario y filas sin polígono
        P.convertir_todo(d / "Valpo", d / "pq2", _bcn_predios(), "cod_comuna", "Comuna", min_participacion=0.8, patron="Cualquiera*.parquet")
        g2 = gpd.read_parquet(d / "pq2" / "09101.parquet").set_index("metodo", drop=False)
        assert sorted(g2.metodo) == ["cercano", "contiene", "herencia_direccion", "huerfano", "utm"], sorted(g2.metodo)
        assert g2.loc["contiene", "exacto"] and not g2.loc["utm", "exacto"] and not g2.loc["huerfano", "datos_sii"], "solo «contiene» es exacto"
        m2 = pd.read_csv(d / "pq2" / "manifiesto_predios.csv", encoding="utf-8-sig", dtype={"cut": str})
        assert int(m2.n_sin_poligono.iloc[0]) == 1 and int(m2.n.iloc[0]) == 5 and m2.cod_sii.astype(str).iloc[0] == "9777", m2.to_dict("records")
    print("  predios parquet: OK")


def test_predios_union():
    """Unión por rol de un respaldo nuevo con uno archivado: gana el nuevo, se conservan los roles que solo tiene el archivado, y el
    archivo no se toca."""
    import shutil
    import pandas as pd
    from etl import predios as P
    lon0, lat0, dx, dy = -72.5950, -38.7350, 0.00023, 0.00027
    base = {"_ok": "True", "comuna": "9201", "manzana": "01733", "predio": "00001", "nombreComuna": "TEMUCO", "dc_cod_destino": "H", "dc_cod_ubicacion": "U",
            "dc_sup_terreno": "300", "supTerreno": "0", "sup_construida_total": "70", "pisos_max": "2", "anio_construccion_min": "2000",
            "anio_construccion_max": "2000", "periodo": "PRIMER SEMESTRE DE 2026"}

    def fila(i, rol, direccion, j=0, metodo="point_in_polygon"):
        return {**base, "rol": rol, "direccion_sii": direccion, "_match_method": metodo,
                "geometry": box(lon0 + i * dx, lat0 + j * dy, lon0 + (i + 1) * dx, lat0 + (j + 1) * dy)}
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        origen = _predios_sucios(d / "Respaldo1")
        P.convertir_todo(origen, d / "pq", _bcn_predios(), "cod_comuna", "Comuna", min_participacion=0.8)
        archivo = d / "archivo"
        shutil.copytree(d / "pq", archivo)                       # el respaldo anterior, archivado
        antes = gpd.read_parquet(archivo / "09101.parquet")
        (d / "Nuevo").mkdir()
        nuevo = [fila(0, "01733-00022", "NUEVA 1"), fila(1, "09999-00001", "OTRA 2", j=5), fila(1, "09999-00002", "OTRA 3", j=5),   # 2 roles en un polígono
                 {**fila(0, "01733-00023", "SIN POLIGONO"), "geometry": None}]    # lo conoce sin polígono: se rellena con el del archivado, aproximado
        gpd.GeoDataFrame(nuevo, geometry="geometry", crs=4326).to_parquet(d / "Nuevo" / "Segundo_9201.parquet")
        shutil.rmtree(d / "pq")                                  # destino limpio: solo el respaldo nuevo
        P.convertir_todo(d / "Nuevo", d / "pq", _bcn_predios(), "cod_comuna", "Comuna", min_participacion=0.8, patron="*.parquet")
        g0 = gpd.read_parquet(d / "pq" / "09101.parquet")
        assert len(g0) == 3 and set(g0.origen) == {"Nuevo"}
        r = P.completar_con_archivo(d / "pq", archivo).set_index("cut")
        datos_viejos = antes[antes.datos_sii & antes.rol.notna()]
        esperados = set(datos_viejos.rol) - {"01733-00022"}
        assert int(r.loc["09101", "roles_conservados"]) == len(esperados) == 12, (r, sorted(esperados))
        assert int(r.loc["09101", "roles_rellenados"]) == 1
        g = gpd.read_parquet(d / "pq" / "09101.parquet")
        a = g[g.rol == "01733-00022"].iloc[0]
        assert a.direccion == "NUEVA 1" and a.origen == "Nuevo", "el respaldo nuevo manda sobre el archivado en el mismo rol"
        assert set(g[g.origen == "Respaldo1"].rol) == esperados, "se conservan los roles que solo estaban en el archivado, con su origen"
        assert g[g.rol == "09999-00001"].n_unidades.iloc[0] == 2 and g[g.rol == "09999-00002"].id_poligono.iloc[0] == g[g.rol == "09999-00001"].id_poligono.iloc[0]
        assert g[g.rol == "01740-00001"].n_unidades.iloc[0] == 2, "las unidades se recalculan tras la unión"
        m = pd.read_csv(d / "pq" / "manifiesto_predios.csv", encoding="utf-8-sig", dtype={"cut": str}).set_index("cut")
        assert m.loc["09101", "roles_conservados_respaldo1"] == 12 and m.loc["09101", "roles_rellenados_respaldo1"] == 1 and m.loc["09101", "n"] == len(g) and m.loc["09101", "n_roles"] == g.rol.nunique()
        assert (gpd.read_parquet(archivo / "09101.parquet").shape == antes.shape), "el archivo no se modifica"
        rel = g[g.rol == "01733-00023"].iloc[0]
        assert rel.metodo == "relleno_respaldo1" and not rel.exacto and rel.datos_sii and rel.origen == "Respaldo1", "relleno: aproximado y con su origen"
        assert (g[g.metodo == "relleno_respaldo1"].shape[0]) == 1, "solo se marca el que el nuevo conocía sin polígono"
        # una segunda unión no repite nada
        assert int(P.completar_con_archivo(d / "pq", archivo).set_index("cut").loc["09101", "roles_conservados"]) == 0
    print("  predios unión: OK")


def test_predios_reasignar():
    """Cada rol va al polígono que contiene su propio punto del SII; si no cae en ninguno, al más cercano a ≤ 50 m (aproximado); sin
    polígono a 50 m, queda aparte con su punto; sin punto, conserva el suyo. La API lo refleja."""
    import pandas as pd
    from fastapi.testclient import TestClient
    from app.main import crear_app
    from etl import predios as P
    lon0, lat0, dx, dy = -72.5950, -38.7350, 0.00023, 0.00027

    def caja(i):
        return box(lon0 + i * dx, lat0, lon0 + (i + 1) * dx, lat0 + dy)
    base = {"_ok": "True", "comuna": "9201", "nombreComuna": "TEMUCO", "direccion_sii": "ALTO 10", "dc_cod_destino": "H", "dc_cod_ubicacion": "U",
            "dc_sup_terreno": "200", "supTerreno": "0", "sup_construida_total": "60", "pisos_max": "1", "anio_construccion_min": "2001",
            "anio_construccion_max": "2001", "periodo": "PRIMER SEMESTRE DE 2026", "_match_method": "point_in_polygon"}

    def fila(n, geom, lon=None, lat=None, **x):
        return {**base, "rol": f"00001-0000{n}", "manzana": "00001", "predio": f"0000{n}", "geometry": geom,
                "lon": None if lon is None else str(lon), "lat": None if lat is None else str(lat), **x}
    filas = [fila(1, caja(0), lon0 + 3.5 * dx, lat0 + 0.5 * dy),                       # ra: viene en P1, su punto está en P2
             fila(2, caja(0), lon0 - 0.0001, lat0 + 0.5 * dy, _match_method="ah_utm_nearest"),   # rb: a ~9 m de P1
             fila(3, caja(0), lon0 + 0.5 * dx, lat0 + 0.006),                           # rc: a ~660 m de todo
             fila(4, caja(3)),                                                           # rd: sin punto
             fila(5, caja(0), lon0 + 6.5 * dx, lat0 + 0.5 * dy, _match_method="ah_utm_nearest"),  # re: su punto está en P3 (huérfano)
             {**base, "rol": "", "manzana": None, "predio": None, "_ok": None, "_match_method": "unmatched_polygon", "geometry": caja(6), "lon": None, "lat": None}]
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "o").mkdir()
        gpd.GeoDataFrame(filas, geometry="geometry", crs=4326).to_parquet(d / "o" / "Prueba_9201.parquet")
        P.convertir_todo(d / "o", d / "pq", _bcn_predios(), "cod_comuna", "Comuna", min_participacion=0.8, patron="*.parquet")
        r = P.reasignar_todo(d / "pq").iloc[0]
        assert (r.roles, r.dentro, r.cercano_50m, r.sin_punto_conservados, r.sin_poligono) == (5, 2, 1, 1, 1), r.to_dict()
        g = gpd.read_parquet(d / "pq" / "09101.parquet").set_index("rol", drop=False)
        assert "00001-00003" not in g.index, "sin polígono a 50 m: no está en el parquet"
        ra, rb, rd, re = g.loc["00001-00001"], g.loc["00001-00002"], g.loc["00001-00004"], g.loc["00001-00005"]
        assert ra.exacto and ra.metodo == "punto_en_poligono" and ra.id_poligono == rd.id_poligono, "ra pasa a P2, donde está su punto"
        assert not rb.exacto and rb.metodo == "cercano_50m" and rb.id_poligono != ra.id_poligono and rb.datos_sii, "rb: el más cercano, aproximado"
        assert re.exacto and re.metodo == "punto_en_poligono" and re.id_poligono not in (ra.id_poligono, rb.id_poligono), "re toma el polígono que era huérfano"
        assert rd.exacto and rd.metodo == "contiene", "sin punto conserva su asignación"
        assert ra.n_unidades == rd.n_unidades == 2 and rb.n_unidades == 0 and rb.n_asignados == 1
        assert len(g) == 4 and int((g.metodo == "huerfano").sum()) == 0, "P1 conserva a rb; P3 ya no es huérfano"
        side = pd.read_parquet(d / "pq" / "sin_poligono" / "09101_roles.parquet")
        assert list(side.rol) == ["00001-00003"] and abs(side.lat_sii.iloc[0] - (lat0 + 0.006)) < 1e-6 and side.direccion.iloc[0] == "ALTO 10"
        again = P.reasignar_todo(d / "pq").iloc[0]
        assert (again.dentro, again.cercano_50m, again.sin_poligono) == (2, 1, 0) and len(pd.read_parquet(d / "pq" / "sin_poligono" / "09101_roles.parquet")) == 1, "idempotente"
        a = _app_fixture(d / "app")
        a.registrar = False
        a.dir_predios = d / "pq"
        c = TestClient(crear_app(a))
        sp = c.get("/api/predio", params={"cut": "09101", "rol": "1-3"}).json()
        assert sp["geometria"]["type"] == "Point" and sp["calidad"]["geometria"] == "solo_punto" and sp["predio"]["rol"] == "00001-00003"
        assert sp["predio"]["direccion"] == "ALTO 10" and any("menos de 50 m" in x for x in sp["calidad"]["avisos"])
        cp = c.get("/api/predio", params={"lon": lon0 + 3.5 * dx, "lat": lat0 + 0.5 * dy, "ficha": "false"}).json()
        assert cp["copropiedad"] and cp["roles"] == ["00001-00001", "00001-00004"], cp["roles"]
        ap = c.get("/api/predio", params={"lon": lon0 + 0.5 * dx, "lat": lat0 + 0.5 * dy, "ficha": "false"}).json()
        assert ap["roles"] == ["00001-00002"] and ap["calidad"]["geometria"] == "aproximada" and ap["calidad"]["metodo"] == "cercano_50m"
        assert any("Asignación aproximada" in x for x in ap["calidad"]["avisos"])
        assert c.get("/api/predio", params={"cut": "09101", "rol": "9-9"}).status_code == 404
        # V2 con los predios locales (sin catastral.cl): un registro por terreno, copropiedad sumada, solo roles con punto exacto
        from etl import vcalc as C
        t = C.cargar_predios_locales(d / "pq", "09101").set_index("rol")
        assert sorted(t.index) == ["00001-00001 (+1 unidades)", "00001-00005"], list(t.index)
        co = t.loc["00001-00001 (+1 unidades)"]
        assert co.n_unidades == 2 and co.sup_construida_total == 120 and co.calidad_geometria == "exacta" and co.origen == "o"
        assert abs(co.m2_terreno - gpd.GeoSeries([caja(3)], crs=4326).to_crs("ESRI:102033").area.iloc[0]) < 1, "copropiedad: el terreno es el polígono"
        assert t.loc["00001-00005", "m2_terreno"] == 200.0 and t.loc["00001-00005", "n_unidades"] == 1, "predio normal: la superficie del SII"
        assert sorted(C.cargar_predios_locales(d / "pq", "09101", solo_exactos=False).rol) == ["00001-00001 (+1 unidades)", "00001-00002", "00001-00005"]
        zonas = gpd.GeoDataFrame({"zona": ["ZH2", "ZH3"]}, geometry=[caja(3).buffer(0.00005), caja(6).buffer(0.00005)], crs=4326)
        assert list(C.cargar_predios_locales(d / "pq", "09101", zonas, "ZH2").rol) == ["00001-00001 (+1 unidades)"]
        assert len(C.cargar_predios_locales(d / "pq", "09101", limite=1)) == 1
    print("  predios reasignación: OK")


def test_app_predios():
    """/api/predio: por rol (cut, código SII o nombre) y por punto, con ficha; huérfanos, errores y sin romper lo demás."""
    from fastapi.testclient import TestClient
    from app.main import crear_app
    from etl import predios as P
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        a = _app_fixture(d)
        a.registrar = False
        c = TestClient(crear_app(a))
        r = c.get("/api/predio", params={"cut": "09101", "rol": "1733-22"})
        assert r.status_code == 503 and "predios convertir" in r.json()["detail"], "sin predios convertidos: mensaje claro"
        a.dir_predios = d / "pq"
        P.convertir_todo(_predios_sucios(d / "Respaldo"), a.dir_predios, _bcn_predios(), "cod_comuna", "Comuna", min_participacion=0.8)
        # por rol, con la comuna dada de tres maneras (la ficha cae en ZH2 de la maqueta)
        r1 = c.get("/api/predio", params={"cut": "09101", "rol": "1733-22"}).json()
        r2 = c.get("/api/predio", params={"cod_sii": "9201", "rol": "01733-00022"}).json()
        r3 = c.get("/api/predio", params={"comuna": "TEMUCO", "rol": "1733 22"}).json()
        assert r1["predio"] == r2["predio"] == r3["predio"] and r1["predio"]["id"] == "09101-01733-00022"
        p = r1["predio"]
        assert p["destino"] == "HABITACIONAL" and p["ubicacion"] == "URBANA" and p["sup_terreno_m2"] == 250.0 and p["direccion"] == "YELCHO 01670"
        assert r1["geometria"]["type"] == "Polygon" and r1["n_poligonos"] == 1 and r1["calidad"]["geometria"] == "exacta" and r1["calidad"]["datos_sii"]
        assert r1["ficha"]["particion"][0]["zona"] == "ZH2" and r1["ficha"]["particion"][0]["clase"] == "U1", "ficha normativa del polígono"
        assert "anterior a su API" in " ".join(r1["calidad"]["avisos"]) and "respaldo" in r1["fuente"]["tipo"]
        texto = json.dumps(r1).lower()
        assert "valortotal" not in texto and "avaluo" not in texto and "12345678" not in texto and "propiet" not in texto, "sin avalúo ni propietarios"
        assert "ficha" not in c.get("/api/predio", params={"cut": "09101", "rol": "1733-22", "ficha": "false"}).json()
        fd = r1["fecha_dato"]      # el semestre en curso depende del día: se valida el cálculo con fechas fijas más abajo
        assert fd["periodo_sii"] == "2026-1" and fd["periodo_respaldo_comuna"] == "2026-1" and fd["estado"] in ("al_dia", "atrasado") and fd["aviso"]
        from datetime import date
        from app import predios as AP
        f1 = AP.fecha_dato("2026-1", "2026-1", date(2026, 10, 7))
        assert f1["estado"] == "atrasado" and f1["atraso_semestres"] == 1 and f1["semestre_actual"] == "2026-2" and "1 semestre de diferencia" in f1["aviso"]
        assert AP.fecha_dato("2026-1", "2026-1", date(2026, 3, 1))["estado"] == "al_dia"
        assert AP.fecha_dato("2024-2", None, date(2026, 10, 7))["atraso_semestres"] == 4 and "4 semestres" in AP.fecha_dato("2024-2", None, date(2026, 10, 7))["aviso"]
        f3 = AP.fecha_dato(None, "2026-1", date(2026, 10, 7))     # huérfano: sin fecha propia (caso Villa Antukuyen), solo la del respaldo
        assert f3["estado"] == "sin_dato" and f3["periodo_sii"] is None and "no tiene fecha propia" in f3["aviso"] and "primer semestre de 2026" in f3["aviso"]
        assert AP.fecha_dato(None, None, date(2026, 10, 7))["atraso_semestres"] is None
        f4 = AP.fecha_dato(None, "2026-1", date(2026, 10, 7), con_datos=True)      # con datos pero periodo corrido: se infiere el de la comuna
        assert f4["periodo_inferido"] and f4["periodo_sii"] == "2026-1" and f4["estado"] == "atrasado" and "se usa el de la comuna" in f4["aviso"]
        assert not f1["periodo_inferido"] and not f3["periodo_inferido"]
        # calidad aproximada, varios polígonos (se unen) y huérfano
        ap = c.get("/api/predio", params={"cut": "09101", "rol": "1733-23"}).json()
        assert ap["fecha_dato"]["periodo_inferido"] and ap["fecha_dato"]["periodo_sii"] == "2026-1", "periodo corrido (B): se infiere el de la comuna"
        assert ap["calidad"]["geometria"] == "aproximada" and any("cercanía" in x for x in ap["calidad"]["avisos"])
        rep = c.get("/api/predio", params={"cut": "09101", "rol": "1735-5", "ficha": "false"}).json()
        assert rep["n_poligonos"] == 2 and rep["calidad"]["geometria"] == "exacta" and rep["geometria"]["type"] in ("Polygon", "MultiPolygon")
        assert any("2 polígonos" in x for x in rep["calidad"]["avisos"])
        sd = c.get("/api/predio", params={"cut": "09101", "rol": "1734-1", "ficha": "false"}).json()
        assert sd["calidad"]["geometria"] == "sin_datos" and not sd["calidad"]["datos_sii"] and sd["predio"]["destino"] is None
        # varios roles sobre un mismo polígono: el terreno con la lista de roles y el número de unidades (por rol y por punto)
        assert r1["tipo_predio"] == "predio" and not r1["copropiedad"] and r1["n_unidades"] == 1 and r1["roles"] == ["01733-00022"]
        co = c.get("/api/predio", params={"cut": "09101", "rol": "1740-2", "ficha": "false"}).json()
        assert co["tipo_predio"] == "copropiedad / varias unidades" and co["copropiedad"] and co["n_unidades"] == 2, co["tipo_predio"]
        assert co["roles"] == ["01740-00001", "01740-00002"] and co["rol_consultado"] == "01740-00002" and co["predio"]["rol"] is None
        assert co["predio"]["id"].startswith("09101-T-") and co["predio"]["manzana"] == "01740" and co["predio"]["area_poligono_m2"] > 0
        assert [u["rol"] for u in co["unidades"]] == co["roles"] and co["unidades"][1]["sup_terreno_m2"] == 900.0 and co["unidades"][0]["direccion"] == "EDIFICIO 1 DEPTO 1"
        assert any("Copropiedad / varias unidades: 2 roles" in x for x in co["calidad"]["avisos"]) and co["geometria"]["type"] == "Polygon"
        assert co["confianza_copropiedad"] == "alta" and co["asignaciones_aproximadas_omitidas"] == 0 and co["unidades"][0]["metodo"] == "contiene"
        # solo hay copropiedad con certeza (roles cuyo punto del SII está en el polígono). Un exacto con dos asignados por cercanía al mismo
        # polígono no se agrupa: se devuelve el exacto, y se avisa de los demás
        j = c.get("/api/predio", params={"lon": -72.593965, "lat": -38.734325, "ficha": "false"}).json()
        assert not j["copropiedad"] and j["roles"] == ["01741-00001"] and j["n_unidades"] == 1 and j["calidad"]["geometria"] == "exacta"
        assert any("Otros 2 roles" in x and "no se agrupan" in x for x in j["calidad"]["avisos"]), j["calidad"]["avisos"]
        assert c.get("/api/predio", params={"cut": "09101", "rol": "1741-3", "ficha": "false"}).json()["roles"] == ["01741-00003"], "por rol: solo ese rol"
        # dos roles solo por cercanía en el mismo polígono: el rol cuyo punto está más cerca del clic, con «asignación aproximada»
        for lon_, esperado in ((-72.59367, "01742-00002"), (-72.59379, "01742-00001")):
            k = c.get("/api/predio", params={"lon": lon_, "lat": -38.73430, "ficha": "false"}).json()
            assert not k["copropiedad"] and k["roles"] == [esperado] and k["calidad"]["geometria"] == "aproximada", (lon_, k["roles"])
            assert any("Asignación aproximada" in x for x in k["calidad"]["avisos"]) and any("Otros 1 roles" in x for x in k["calidad"]["avisos"])
        cp = c.get("/api/predio", params={"lon": -72.5950 + 2.5 * 0.00023, "lat": -38.7350 + 2.5 * 0.00027}).json()
        assert cp["copropiedad"] and cp["roles"] == co["roles"] and cp["predio"]["id"] == co["predio"]["id"] and "rol_consultado" not in cp
        assert cp["ficha"]["particion"], "la ficha normativa también sale en una copropiedad"
        # por punto: dentro del predio A, dentro de un huérfano, en la calle y fuera de toda comuna
        lon, lat = -72.5950 + 0.00023 / 2, -38.7350 + 0.00027 / 2
        pt = c.get("/api/predio", params={"lon": lon, "lat": lat}).json()
        assert pt["predio"]["id"] == "09101-01733-00022" and pt["ficha"]["particion"][0]["zona"] == "ZH2"
        huer = c.get("/api/predio", params={"lon": -72.5950 + 2.5 * 0.00023, "lat": lat, "ficha": "false"}).json()
        assert huer["predio"]["id"] is None and huer["calidad"]["geometria"] == "sin_datos" and huer["predio"]["area_poligono_m2"] > 0
        assert huer["fecha_dato"]["estado"] == "sin_dato" and huer["fecha_dato"]["periodo_sii"] is None and huer["fecha_dato"]["periodo_respaldo_comuna"] == "2026-1"
        fuera = c.get("/api/predio", params={"lon": -72.5594, "lat": -38.7199, "ficha": "false"}).json()
        assert c.get("/api/predio", params={"lon": -70.0, "lat": -33.0}).status_code == 404
        # errores
        assert c.get("/api/predio", params={"cut": "09101", "rol": "9999-1"}).status_code == 404
        assert c.get("/api/predio", params={"cut": "09101", "rol": "abc"}).status_code == 422
        assert c.get("/api/predio", params={"rol": "1733-22"}).status_code == 422, "rol sin comuna"
        assert c.get("/api/predio", params={"cut": "09101", "comuna": "Temuco", "rol": "1733-22"}).status_code == 422, "dos comunas a la vez"
        assert c.get("/api/predio", params={"lon": -72.59}).status_code == 422 and c.get("/api/predio").status_code == 422
        assert c.get("/api/predio", params={"lon": -72.59, "lat": -38.73, "rol": "1-1", "cut": "09101"}).status_code == 422, "punto y rol a la vez"
        assert c.get("/api/predio", params={"cut": "9101", "rol": "1733-22"}).status_code == 422, "el CUT lleva 5 dígitos"
        assert c.get("/api/predio", params={"cut": "09999", "rol": "1-1"}).status_code == 404
        assert c.get("/api/predio", params={"comuna": "Inexistente", "rol": "1-1"}).status_code == 404
        # 9101 como código SII es Angol (no Temuco): no hay respaldo con ese código → 404, y no se confunde con el CUT 09101
        assert c.get("/api/predio", params={"cod_sii": "9101", "rol": "1733-22"}).status_code == 404
        # lo que ya funcionaba sigue igual
        assert c.get("/api/ficha", params={"lon": -72.595, "lat": -38.738}).json()["particion"][0]["zona"] == "ZH2"
        assert c.get("/api/comunas").status_code == 200 and c.get("/api/temas").status_code == 200 and c.get("/").status_code == 200
    print("  app predios: OK")


def test_rescate_geos():
    """Si GEOS falla con precisión flotante (non-noded intersection), la superposición de riesgo
    reintenta con make_valid + GRID_RESCATE y cuenta el rescate (caso real: Chañaral)."""
    from etl.classify import GRID_RESCATE, _Rescates

    def fragil(a, b, grid_size=None):
        if grid_size != GRID_RESCATE:
            raise shapely.errors.GEOSException("TopologyException: found non-noded intersection")
        return shapely.intersection(a, b, grid_size=grid_size)

    r = _Rescates(None)
    out = r.op(fragil, box(0, 0, 10, 10), box(5, 5, 15, 15))
    assert r.n == 1 and abs(out.area - 25) < 1e-6
    assert r.op(shapely.intersection, box(0, 0, 2, 2), box(1, 1, 3, 3)).area == 1 and r.n == 1

    # Resultado erróneo sin excepción: inválido, o VÁLIDO pero fuera de las entradas (caso real La Pintana:
    # la intersección devolvía la zona completa, fuera de la comuna). Se detecta por validez y contenido,
    # también en la versión vectorizada.
    corbata = shapely.Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])   # autointersectado

    def devuelve(malo):
        def fn(a, b, grid_size=None):
            if grid_size == GRID_RESCATE:
                return shapely.intersection(a, b, grid_size=grid_size)
            if isinstance(a, np.ndarray):
                out = shapely.intersection(a, b)
                out[1] = malo if malo is not None else a[1]
                return out
            return malo if malo is not None else a
        fn.__name__ = "intersection"
        return fn

    for malo in (corbata, None):   # None: devuelve la entrada completa, válida pero fuera de b
        f = devuelve(malo)
        r = _Rescates(None)
        assert not r._bien(shapely.intersection, box(0, 0, 10, 10), box(0, 0, 10, 10), box(20, 20, 30, 30))
        r = _Rescates(None)
        arr = np.array([box(0, 0, 2, 2), box(20, 20, 30, 30)], dtype=object)
        # op_vec verifica contenido para cualquier fn vectorizada; se usa con shapely.intersection
        out = r.op_vec(lambda a, b, grid_size=None, _f=f: _f(a, b, grid_size), arr, box(1, 1, 3, 3))
        assert r.n == 1 and out[0].area == 1 and out[1].is_empty, (malo, r.n, list(out))


def test_paginacion_arcgis():
    from etl.arcgis import ArcGISClient
    feats = [{"type": "Feature", "properties": {"i": i}, "geometry": None} for i in range(4500)]

    class Fake(ArcGISClient):
        def _request(self, url, params=None, post=False):
            if not url.endswith("/query"):
                return {"type": "Feature Layer", "maxRecordCount": 2000,
                        "advancedQueryCapabilities": {"supportsPagination": self.pag}, "fields": []}
            if params.get("returnIdsOnly"):
                return {"objectIds": list(range(4500))}
            if "resultOffset" in params:
                o, n = params["resultOffset"], params["resultRecordCount"]
                return {"features": feats[o:o + n], "exceededTransferLimit": o + n < 4500}
            ids = list(map(int, params["objectIds"].split(",")))
            return {"features": [feats[i] for i in ids]}

    for pag in (True, False):
        c = Fake("https://x", pause_s=0)
        c.pag = pag
        assert c.fetch_layer("https://x/L/0")["_meta"]["count"] == 4500


if __name__ == "__main__":
    test_end_to_end()
    test_vigencia()
    test_fuentes_contratos()
    test_volumen_paso0()
    test_footprints_sintetico()
    test_ocupacion_sintetico()
    test_vcalc_sintetico()
    test_app_sintetico()
    test_envolvente_sintetico()
    test_app_edificios_volumen()
    test_app_teselas()
    test_app_temas()
    test_app_basemap()
    test_app_lamina()
    test_temas_catalogo()
    test_predios_conversion()
    test_predios_parquet()
    test_predios_union()
    test_predios_reasignar()
    test_app_predios()
    test_raster_tiles()
    test_temas_generadores_comunes()
    test_temas_worldclim()
    test_temas_soilgrids()
    test_temas_relieve()
    test_temas_worldcover()
    test_rescate_geos()
    test_paginacion_arcgis()
    print("OK")
