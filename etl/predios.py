"""Predios del SII (respaldo personal de catastral.cl, antes de su API): GeoPackage sucios → GeoParquet limpios por comuna.

Los respaldos (`Respaldo 1/<código SII>_<código SII>.gpkg`, capa `comuna=<código SII>`) tienen estos problemas, medidos el
7-oct-2026 sobre 43 archivos y 736.410 registros:
- el código del archivo es el **código de comuna del SII**, no el CUT (9201 = Temuco, CUT 09101) y varios nombres de archivo no
  corresponden a su contenido (`Puerto_Montt_10101` es Valdivia): el CUT se asigna **cruzando con la geometría de las comunas BCN**;
- todas las columnas son texto;
- el 34,6 % son «polígonos huérfanos» (sin datos del SII) y a otros 128.576 les quedaron corridas las columnas de texto
  (`destinoDescripcion`, `ubicacion`, `periodo`), pero los códigos `dc_cod_destino` y `dc_cod_ubicacion` y los numéricos están bien:
  se usan los códigos, no el texto;
- tres archivos traen mensajes de error de conexión al SII guardados como dato.

Se conserva solo lo necesario para la ficha de un predio. **No se guarda el avalúo ni ningún dato de propietarios** (el respaldo no
trae propietarios). Los datos son de terceros (catastral.cl) y de uso personal: nunca van al repo ni se publican.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely

from .normalize import norm_txt

log = logging.getLogger(__name__)
CRS_AREA = "ESRI:102033"
RE_ROL = re.compile(r"^\d{5}-\d{5}$")
# Equivalencias del código de destino del SII, aprendidas de los datos (código × descripción, 364.974 filas coherentes)
DESTINOS = {"A": "AGRICOLA", "B": "AGRICOLA POR ASIMILACION", "C": "COMERCIO", "D": "DEPORTE Y RECREACION", "E": "EDUCACION Y CULTURA",
            "F": "FORESTAL", "G": "HOTEL, MOTEL", "H": "HABITACIONAL", "I": "INDUSTRIA", "L": "BODEGA Y ALMACENAJE", "O": "OFICINA",
            "P": "ADM. PUBLICA Y DEFENSA", "Q": "CULTO", "S": "SALUD", "T": "TRANSPORTE Y TELEC.", "V": "OTROS NO CONSIDERADOS",
            "W": "SITIO ERIAZO", "Z": "ESTACIONAMIENTO"}
UBICACIONES = {"U": "URBANA", "R": "RURAL", "E": "EXTENSION URBANA"}
METODOS = (("1_contains", "contiene"), ("2_nearest", "cercano"), ("4_manzana", "manzana"), ("6_manzana_any", "manzana_cualquiera"),
           ("orphan_polygon", "huerfano"),
           # vocabulario del segundo proceso de emparejamiento (parquet de Valparaíso, Segundo y Tercer respaldo)
           ("point_in_polygon", "contiene"), ("nearest", "cercano"), ("unmatched_polygon", "huerfano"),
           ("address_inheritance", "herencia_direccion"), ("ah_utm", "utm"), ("csa_utm", "utm"))
LEER = ["_ok", "comuna", "manzana", "predio", "rol", "nombreComuna", "direccion_sii", "dc_cod_destino", "dc_cod_ubicacion", "dc_sup_terreno",
        "supTerreno", "sup_construida_total", "pisos_max", "anio_construccion_min", "anio_construccion_max", "_match_method"]
COLUMNAS = ["id", "cut", "cod_sii", "manzana", "predio", "rol", "comuna", "direccion", "destino_cod", "destino", "ubicacion", "sup_terreno_m2",
            "sup_construida_m2", "pisos_max", "anio_construccion", "area_poligono_m2", "metodo", "exacto", "datos_sii", "geometry"]


def normalizar_rol(valor: str) -> str | None:
    """«1733-22», «01733-00022», «1733 22» o «1733/22» → «01733-00022»; None si no calza."""
    m = re.fullmatch(r"\s*0*(\d{1,5})\s*[-/ ]\s*0*(\d{1,5})\s*", str(valor))
    return f"{int(m.group(1)):05d}-{int(m.group(2)):05d}" if m else None


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _metodo(s: pd.Series) -> pd.Series:
    t = s.astype("string").fillna("")
    out = pd.Series("desconocido", index=s.index, dtype="object")
    for prefijo, nombre in METODOS:
        out[t.str.startswith(prefijo)] = nombre
    return out


def _direccion(s: pd.Series) -> pd.Series:
    """Dirección del SII; nulo si es vacía, un número suelto (columna corrida) o un mensaje de error de conexión."""
    t = s.astype("string").str.strip()
    malo = t.isna() | (t == "") | t.str.fullmatch(r"[\d.,\- ]+") | t.str.contains("Error|Exception|Retry", case=False, regex=True) | (t.str.len() > 90)
    return t.where(~malo).astype(object)


def limpiar(raw: gpd.GeoDataFrame, cut: str, comuna: str, cod_sii: str | None = None) -> gpd.GeoDataFrame:
    """GeoDataFrame crudo de un respaldo (todo texto) → tabla tipada con `COLUMNAS`. `cod_sii` (el de la capa, confiable) reemplaza
    al de las filas, que en algunas viene corrupto (p. ej. «105» en el respaldo 9103)."""
    g = raw.copy()
    rol = g["rol"].astype("string").str.strip()
    rol_ok = rol.str.fullmatch(RE_ROL).fillna(False)
    datos = ((g["_ok"].astype("string").str.strip() == "True") & rol_ok).fillna(False).astype(bool)
    man, pre = rol.str[:5], rol.str[6:]
    cod_destino = g["dc_cod_destino"].astype("string").str.strip().str.upper()
    cod_ubic = g["dc_cod_ubicacion"].astype("string").str.strip().str.upper()
    terreno = _num(g["dc_sup_terreno"]).where(lambda x: x > 0, _num(g["supTerreno"]).where(lambda x: x > 0))
    anio = _num(g["anio_construccion_min"]).where(lambda x: (x > 1500) & (x < 2101))
    area = gpd.GeoSeries(g.geometry.values, crs=4326).to_crs(CRS_AREA).area
    metodo = _metodo(g["_match_method"])
    out = gpd.GeoDataFrame({
        "id": np.where(rol_ok, f"{cut}-" + rol.fillna(""), None), "cut": cut, "cod_sii": cod_sii if cod_sii else g["comuna"].astype("string").str.strip(),
        "manzana": man.where(rol_ok), "predio": pre.where(rol_ok), "rol": rol.where(rol_ok), "comuna": comuna,
        "direccion": _direccion(g["direccion_sii"]).where(datos),
        "destino_cod": cod_destino.where(datos & cod_destino.isin(DESTINOS)),
        "ubicacion": cod_ubic.map(UBICACIONES).where(datos),
        "sup_terreno_m2": terreno.where(datos).round(1), "sup_construida_m2": _num(g["sup_construida_total"]).where(datos),
        "pisos_max": _num(g["pisos_max"]).where(datos), "anio_construccion": anio.where(datos),
        "area_poligono_m2": area.round(1).to_numpy(), "metodo": metodo, "exacto": metodo == "contiene", "datos_sii": datos,
    }, geometry=g.geometry.values, crs=4326)
    out["destino"] = out["destino_cod"].map(DESTINOS)
    out["destino_cod"] = out["destino_cod"].astype(object)
    for c in ("cod_sii", "manzana", "predio", "rol", "direccion", "ubicacion", "destino"):
        out[c] = out[c].astype(object).where(out[c].notna(), None)
    return out[COLUMNAS]


def asignar_cut(raw: gpd.GeoDataFrame, comunas_bcn: gpd.GeoDataFrame, f_cut: str, f_nombre: str, muestra: int = 3000) -> dict:
    """CUT de un respaldo por el voto de los puntos representativos de una muestra de sus polígonos contra las comunas BCN."""
    m = raw.sample(min(muestra, len(raw)), random_state=1) if len(raw) > muestra else raw
    pts = gpd.GeoDataFrame(geometry=m.geometry.representative_point().values, crs=4326)
    j = gpd.sjoin(pts, comunas_bcn[[f_cut, f_nombre, "geometry"]], how="left", predicate="within")
    votos = j[f_cut].dropna().astype(int).astype(str).str.zfill(5).value_counts()
    if votos.empty:
        return {"cut": None, "comuna": None, "participacion": 0.0}
    cut = votos.index[0]
    nombre = str(comunas_bcn.loc[comunas_bcn[f_cut].astype(int).astype(str).str.zfill(5) == cut, f_nombre].iloc[0])
    return {"cut": cut, "comuna": nombre, "participacion": round(float(votos.iloc[0]) / len(j), 3)}


def _leer(archivo: Path) -> tuple[gpd.GeoDataFrame, str]:
    """Respaldo crudo (GeoPackage o GeoParquet) → (GeoDataFrame con las columnas de `LEER`, código SII según el archivo)."""
    if archivo.suffix.lower() == ".parquet":
        import pyarrow.parquet as pq
        campos = pq.ParquetFile(archivo).schema_arrow.names
        raw = gpd.read_parquet(archivo, columns=[c for c in LEER if c in campos] + ["geometry"])
        m = re.search(r"_(\d{4,5})$", archivo.stem)      # los nombres de archivo traen el código SII al final
        return raw, m.group(1) if m else ""
    capa = pyogrio.list_layers(archivo)[0][0]
    campos = pyogrio.read_info(archivo, layer=capa)["fields"]
    raw = pyogrio.read_dataframe(archivo, layer=capa, columns=[c for c in LEER if c in campos])
    return raw, capa.split("=")[-1] if re.fullmatch(r"comuna=\d{4,5}", capa) else ""


def convertir_gpkg(gpkg: Path, comunas_bcn: gpd.GeoDataFrame, f_cut: str, f_nombre: str, destino_dir: Path, min_participacion: float = 0.9,
                   protegidos: dict | None = None) -> dict:
    """Convierte un respaldo (`.gpkg` o `.parquet`) a `<destino_dir>/<cut>.parquet` y devuelve su fila de manifiesto (con las cifras de
    calidad). `protegidos` es {cut: archivo} de lo ya convertido desde otro respaldo: no se sobrescribe."""
    gpkg = Path(gpkg)
    raw, cod_archivo = _leer(gpkg)
    n_sin_geom = int((raw.geometry.isna() | raw.geometry.is_empty).sum())      # filas con datos del SII pero sin polígono
    raw = raw[raw.geometry.notna() & ~raw.geometry.is_empty].copy()
    raw["geometry"] = shapely.make_valid(raw.geometry.values)
    raw = raw[raw.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    a = asignar_cut(raw, comunas_bcn, f_cut, f_nombre)
    cod = cod_archivo or ",".join(sorted(raw["comuna"].dropna().astype(str).unique())[:2])
    nombres = raw["nombreComuna"].dropna().astype(str)
    nombre_datos = nombres.value_counts().index[0] if len(nombres) else ""
    fila = {"archivo": gpkg.name, "cod_sii": cod, "cut": a["cut"], "comuna_bcn": a["comuna"], "participacion_cut": a["participacion"],
            "nombre_en_datos": nombre_datos[:40], "coincide_nombre": bool(a["comuna"] and norm_txt(a["comuna"]) == norm_txt(nombre_datos)),
            "n": len(raw), "n_sin_poligono": n_sin_geom, "n_datos_sii": 0, "n_exactos": 0, "n_huerfanos": 0, "n_ids_repetidos": 0, "mb": None,
            "estado": "ok"}
    if a["cut"] is None or a["participacion"] < min_participacion:
        return {**fila, "estado": f"sin CUT claro (participación {a['participacion']})"}
    if protegidos and protegidos.get(a["cut"], gpkg.name) != gpkg.name:
        return {**fila, "estado": f"CUT {a['cut']} ya cubierto por {protegidos[a['cut']]}: no se sobrescribe"}
    limpio = limpiar(raw, a["cut"], a["comuna"], cod if re.fullmatch(r"\d{4,5}", cod) else None)
    destino_dir = Path(destino_dir)
    destino_dir.mkdir(parents=True, exist_ok=True)
    p = destino_dir / f"{a['cut']}.parquet"
    tmp = p.with_suffix(".tmp")
    # Orden espacial (Hilbert) y grupos de 2.000 filas con la caja envolvente de cada predio (`bbox`): DuckDB salta casi todos los
    # grupos al buscar un punto (de ≈ 1.500 ms a ≈ 15 ms en Temuco, 137.728 polígonos)
    limpio = limpio.iloc[limpio.geometry.hilbert_distance().argsort()]
    limpio.to_parquet(tmp, compression="zstd", write_covering_bbox=True, row_group_size=2000)
    tmp.replace(p)
    ids = limpio["id"].dropna()
    return {**fila, "n_datos_sii": int(limpio["datos_sii"].sum()), "n_exactos": int(limpio["exacto"].sum()),
            "n_huerfanos": int((limpio["metodo"] == "huerfano").sum()), "n_ids_repetidos": int(ids.duplicated().sum()),
            "mb": round(p.stat().st_size / 1e6, 1)}


def convertir_todo(origen: Path, destino_dir: Path, comunas_bcn: gpd.GeoDataFrame, f_cut: str, f_nombre: str, progreso=None,
                   min_participacion: float = 0.9, patron: str = "*.gpkg") -> pd.DataFrame:
    """Convierte todos los archivos de `origen` que calcen con `patron` (recursivo; `*.gpkg` o `*.parquet`). Escribe
    `manifiesto_predios.csv` en `destino_dir` **sumando** a lo ya convertido: las filas de otros archivos se conservan y un CUT ya
    cubierto por otro archivo no se sobrescribe (se marca en el manifiesto). Dentro de una misma corrida, un CUT repetido sí pisa."""
    destino_dir = Path(destino_dir)
    destino_dir.mkdir(parents=True, exist_ok=True)
    mf = destino_dir / "manifiesto_predios.csv"
    previo = pd.read_csv(mf, encoding="utf-8-sig", dtype=str, keep_default_na=False) if mf.exists() else pd.DataFrame()
    archivos = sorted(Path(origen).rglob(patron))
    nuevos = {p.name for p in archivos}
    protegidos = {r["cut"]: r["archivo"] for _, r in previo.iterrows() if r.get("estado") == "ok" and r.get("cut") and r["archivo"] not in nuevos}
    filas, vistos = [], {}
    for i, p in enumerate(archivos, 1):
        if progreso:
            progreso("predios", p.stem, i, len(archivos))
        try:
            f = convertir_gpkg(p, comunas_bcn, f_cut, f_nombre, destino_dir, min_participacion, protegidos)
        except Exception as ex:   # un archivo roto no detiene el resto
            f = {"archivo": p.name, "estado": f"error: {str(ex)[:120]}"}
        if f.get("cut") in vistos and f["estado"] == "ok":
            f["estado"] = f"CUT repetido con {vistos[f['cut']]}: sobrescribió su parquet"
        if f.get("cut") and f["estado"] == "ok":
            vistos[f["cut"]] = p.name
        filas.append(f)
    m = pd.DataFrame(filas)
    total = pd.concat([previo[~previo["archivo"].isin(nuevos)] if len(previo) else previo, m], ignore_index=True)
    total.to_csv(mf, index=False, encoding="utf-8-sig")
    return pd.read_csv(mf, encoding="utf-8-sig", dtype={"cut": str, "cod_sii": str})      # con los tipos numéricos de vuelta
