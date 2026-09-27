"""Prueba end-to-end con datos sintéticos (sin red). Ejecutar: python -m pytest tests -q  (o python tests/test_sintetico.py)"""
import json
import sys
import tempfile
from pathlib import Path

import geopandas as gpd
import shapely
import yaml
from shapely.geometry import box, mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etl.arcgis import raw_path  # noqa: E402
from etl.classify import clasificar  # noqa: E402
from etl.export import anotar_legal, escribir  # noqa: E402
from etl.normalize import ComunaResolver, load_layer, separar_afectaciones, tipo_capa  # noqa: E402

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
        ],
        # Mismo LU publicado en Limites_Urbanos y en PRC_Araucania (geometría idéntica)
        ("IPT/Limites_Urbanos", 0, "Limite_Urbano_Padre_Las_Casas"): [
            ({"COMUNA": "Padre Las Casas"}, box(-72.49, -38.76, -72.42, -38.70)),
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
        ("IPT/PRC_Araucania", 3, "PRC_Temuco_Riesgo"): [
            ({"ZONA": "AR-1", "DESCRIPCION": "Riesgo inundación"}, box(-72.62, -38.74, -72.60, -38.72)),
        ],
    }
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw = tmp / "raw"
    catalogo = []
    for (srv, lid, nombre), feats in capas.items():
        e = {"service": srv, "layer_id": lid, "layer_name": nombre, "url": f"https://x/{srv}/MapServer/{lid}"}
        e["tipo"] = tipo_capa(e, cfg["layer_rules"], {})
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
                         "Limite_Urbano": "LU", "Limite_Urbano_Padre_Las_Casas": "LU",
                         "PRI_Temuco_PLC": "PRI", "PRC_Temuco_Riesgo": "AFECTACION"}

        res = ComunaResolver(comunas, "CUT_COM", "COMUNA")
        assert res.resolve("Temuco Labranza") == "09101"
        assert res.resolve("Padre_Las_Casas") == "09112"

        fuentes, afect = [], []
        for e, p in catalogo:
            part, af = separar_afectaciones(load_layer(p, e, cfg, res), e["tipo"])
            if part is not None:
                fuentes.append(part)
            if af is not None:
                afect.append(af)
        import pandas as pd
        fuentes = gpd.GeoDataFrame(pd.concat(fuentes, ignore_index=True), crs=4326)
        assert set(fuentes["fuente"]) == {"PRC", "SECCIONAL", "LU", "PRI_E", "PRI_R"}
        assert fuentes.loc[fuentes.zona == "ZX", "revisar"].item() is True
        # Zonas de RIESGO dentro de PRC y PRI van a afectaciones, no a la partición
        zonas_af = set(pd.concat(afect).zona)
        assert {"AR-2", "Riesgo aluvión"} <= zonas_af
        assert not ({"AR-2", "Riesgo aluvión"} & set(fuentes.zona))

        # zone_overrides gana sobre pri_subclase; AFECTACION saca la zona de la partición
        e_pri, p_pri = next((e, p) for e, p in catalogo if e["layer_name"] == "PRI_Temuco_PLC")
        cfg_zo = {**cfg, "zone_overrides": {"Temuco PLC|ZX": "E", "Temuco_PLC|zr-3": "AFECTACION"}}
        part, af = separar_afectaciones(load_layer(p_pri, e_pri, cfg_zo, res), e_pri["tipo"])
        zx = part[part.zona == "ZX"].iloc[0]
        assert zx["fuente"] == "PRI_E" and not zx["revisar"]
        assert set(af.zona) == {"ZR-3", "Riesgo aluvión"} and "ZR-3" not in set(part.zona)

        capa, qas = clasificar(comunas, fuentes, cfg, "CUT_COM", "COMUNA", "REGION")

        # 1. Cobertura de 100% ± 0,01 en TODAS las comunas y sin traslapes
        #    ('intersects' y no 'overlaps': este último no ve contención ni igualdad)
        for qa in qas:
            assert abs(qa["cobertura_pct"] - 100) <= 0.01, qa
            assert qa["traslape_m2"] < 1.0, qa
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

        legal = json.loads((ROOT / "legal_refs.json").read_text(encoding="utf-8"))
        capa = anotar_legal(capa, legal)
        afect_gdf = gpd.GeoDataFrame(pd.concat(afect, ignore_index=True), crs=4326)
        prod = escribir(capa, afect_gdf, qas, tmp / "out", cfg, "test")
        assert Path(prod["geojson"]).exists() and Path(prod["gpkg"]).exists()
        gj = json.loads(Path(prod["geojson"]).read_text(encoding="utf-8"))
        assert gj["features"][0]["properties"]["norma_titulo"]
        print(json.dumps(prod["resumen"], ensure_ascii=False, indent=1))
        print(capa[["id", "comuna", "clase", "fuente", "ipt_nombre", "zona", "area_m2"]].to_string())




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
    test_paginacion_arcgis()
    print("OK")
