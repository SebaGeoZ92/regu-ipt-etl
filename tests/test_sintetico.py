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
    t = C.calcular(predios, edif, zonas=zonas).set_index("rol")
    a, b, c, d = t.loc["A"], t.loc["B"], t.loc["C"], t.loc["D"]
    assert a.n_edificios == 2 and abs(a.huella_m2 - 160) < 1e-6 and a.pisos_est == 2 and abs(a.m2_equiv - 320) < 1e-6 and a.zona == "ZH2", a
    assert b.pisos_est == 1 and abs(b.huella_m2 - 60) < 1e-6 and b.fuente_pisos == "sii" and b.confianza_pisos
    assert c.estado == "sin_dato" and c.n_edificios == 0 and np.isnan(c.m2_equiv) and c.zona == "" and "sin huella" in c.avisos
    assert d.pisos_est == 1 and d.zona == "" and "menos de la mitad" in d.avisos, d
    assert t.v_calc_m3.isna().all(), "sin altura de piso de referencia no hay m³"
    assert abs(C.calcular(predios, edif, altura_piso_ref_m=2.5).set_index("rol").loc["A", "v_calc_m3"] - 800) < 1e-6
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
    test_rescate_geos()
    test_paginacion_arcgis()
    print("OK")
