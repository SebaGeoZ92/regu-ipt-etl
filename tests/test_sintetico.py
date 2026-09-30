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
    test_rescate_geos()
    test_paginacion_arcgis()
    print("OK")
