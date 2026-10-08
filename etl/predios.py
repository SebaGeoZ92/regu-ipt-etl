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

import hashlib
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
        "supTerreno", "sup_construida_total", "pisos_max", "anio_construccion_min", "anio_construccion_max", "_match_method", "periodo", "lat", "lon"]
COLUMNAS = ["id", "cut", "cod_sii", "manzana", "predio", "rol", "comuna", "direccion", "destino_cod", "destino", "ubicacion", "sup_terreno_m2",
            "sup_construida_m2", "pisos_max", "anio_construccion", "periodo_sii", "area_poligono_m2", "metodo", "exacto", "datos_sii",
            "lat_sii", "lon_sii", "id_poligono", "n_unidades", "n_asignados", "origen", "geometry"]
METODO_RELLENO = "relleno_respaldo1"      # rol conocido sin polígono por el respaldo nuevo; el polígono viene del Respaldo 1 (aproximado)
RE_PERIODO =re.compile(r"^\s*(PRIMER|SEGUNDO)\s+SEMESTRE\s+DE\s+(20\d{2})\s*$", re.IGNORECASE)


def normalizar_periodo(valor) -> str | None:
    """Semestre del avalúo del SII → «2026-1» o «2026-2»; None si el texto no es un periodo (columna corrida, número suelto, nulo)."""
    m = RE_PERIODO.match(str(valor)) if valor is not None else None
    return f"{m.group(2)}-{1 if m.group(1).upper() == 'PRIMER' else 2}" if m else None


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


def _coord(s: pd.Series | None, index, minimo: float, maximo: float) -> pd.Series:
    """Coordenada del punto que el SII publica para el rol (grados); nula si falta o cae fuera de Chile (columna corrida)."""
    if s is None:
        return pd.Series(np.nan, index=index, dtype="float64")
    v = _num(s)
    return v.where((v >= minimo) & (v <= maximo)).round(6)


def _direccion(s: pd.Series) -> pd.Series:
    """Dirección del SII; nulo si es vacía, un número suelto (columna corrida) o un mensaje de error de conexión."""
    t = s.astype("string").str.strip()
    malo = t.isna() | (t == "") | t.str.fullmatch(r"[\d.,\- ]+") | t.str.contains("Error|Exception|Retry", case=False, regex=True) | (t.str.len() > 90)
    return t.where(~malo).astype(object)


def _id_poligono(geoms) -> list[str]:
    """Huella corta de la geometría exacta: los roles de un mismo terreno (copropiedad) comparten el polígono y, por tanto, la huella."""
    return [hashlib.blake2b(w, digest_size=6).hexdigest() for w in shapely.to_wkb(np.asarray(geoms))]


def agrupar_unidades(g: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, int]:
    """Quita los duplicados exactos (mismo rol y mismo polígono) y cuenta, por polígono, los roles con datos que lo comparten.
    `n_unidades` cuenta solo los roles **asignados con certeza** (el polígono contiene su punto del SII, `exacto`): es la copropiedad
    confiable (>1). `n_asignados` cuenta todos los roles con datos, también los asignados por coordenadas o cercanía (un polígono con miles
    de roles así es un artefacto del emparejamiento). Ambos valen 0 donde no hay roles; devuelve (tabla, duplicados quitados)."""
    dup = g["rol"].notna() & g.duplicated(["rol", "id_poligono"], keep="first")
    g = g[~dup].copy()
    con_rol = g[g["datos_sii"] & g["rol"].notna()]
    n = con_rol[con_rol["exacto"]].groupby("id_poligono")["rol"].nunique()
    g["n_unidades"] = g["id_poligono"].map(n).fillna(0).astype("int32")
    g["n_asignados"] = g["id_poligono"].map(con_rol.groupby("id_poligono")["rol"].nunique()).fillna(0).astype("int32")
    return g, int(dup.sum())


def limpiar(raw: gpd.GeoDataFrame, cut: str, comuna: str, cod_sii: str | None = None, origen: str | None = None) -> gpd.GeoDataFrame:
    """GeoDataFrame crudo de un respaldo (todo texto) → tabla tipada con `COLUMNAS`. `cod_sii` (el de la capa, confiable) reemplaza
    al de las filas, que en algunas viene corrupto (p. ej. «105» en el respaldo 9103). `origen` rotula de qué respaldo viene cada fila."""
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
        "periodo_sii": (g["periodo"].map(normalizar_periodo) if "periodo" in g else pd.Series(None, index=g.index, dtype=object)).where(datos),
        "area_poligono_m2": area.round(1).to_numpy(), "metodo": metodo, "exacto": metodo == "contiene", "datos_sii": datos,
        "lat_sii": _coord(g["lat"] if "lat" in g else None, g.index, -56, -17).where(datos),
        "lon_sii": _coord(g["lon"] if "lon" in g else None, g.index, -110, -66).where(datos),
        "id_poligono": _id_poligono(g.geometry.values), "n_unidades": 0, "n_asignados": 0, "origen": origen,
    }, geometry=g.geometry.values, crs=4326)
    out["destino"] = out["destino_cod"].map(DESTINOS)
    out["destino_cod"] = out["destino_cod"].astype(object)
    for c in ("cod_sii", "manzana", "predio", "rol", "direccion", "ubicacion", "destino", "periodo_sii"):
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
                   protegidos: dict | None = None, etiqueta: str | None = None) -> dict:
    """Convierte un respaldo (`.gpkg` o `.parquet`) a `<destino_dir>/<cut>.parquet` y devuelve su fila de manifiesto (con las cifras de
    calidad). `protegidos` es {cut: archivo} de lo ya convertido desde otro respaldo: no se sobrescribe. `etiqueta` rotula el origen de
    cada fila (por defecto, el nombre de la carpeta del archivo)."""
    gpkg = Path(gpkg)
    raw, cod_archivo = _leer(gpkg)
    sin_geom = raw.geometry.isna() | raw.geometry.is_empty                      # filas con datos del SII pero sin polígono
    n_sin_geom = int(sin_geom.sum())
    rol_txt = raw["rol"].astype("string").str.strip()
    roles_sin = sorted(set(rol_txt[sin_geom & rol_txt.str.fullmatch(RE_ROL).fillna(False)]))
    raw = raw[~sin_geom].copy()
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
    limpio = limpiar(raw, a["cut"], a["comuna"], cod if re.fullmatch(r"\d{4,5}", cod) else None, etiqueta or gpkg.parent.name)
    limpio, n_dup = agrupar_unidades(limpio)
    destino_dir = Path(destino_dir)
    destino_dir.mkdir(parents=True, exist_ok=True)
    p = destino_dir / f"{a['cut']}.parquet"
    _escribir(limpio, p)
    # Roles del respaldo que no traen polígono: no entran al parquet, pero se anotan para que la unión con un respaldo anterior no los
    # dé por ausentes (un rol que el respaldo nuevo conoce sin polígono no se rellena con el polígono aproximado de otro).
    (destino_dir / "sin_poligono").mkdir(exist_ok=True)
    pd.DataFrame({"rol": roles_sin}).to_csv(destino_dir / "sin_poligono" / f"{a['cut']}.csv", index=False)
    return {**fila, "n_duplicados": n_dup, "n_roles_sin_poligono": len(roles_sin), **_estadisticas(limpio, p)}


METODO_PUNTO = "punto_en_poligono"       # el punto del SII del rol cae dentro del polígono asignado (certeza)
METODO_CERCANO = "cercano_50m"            # el punto cae fuera de todo polígono: el más cercano, a no más de 50 m (aproximado)
CAMPOS_ROL = ["id", "manzana", "predio", "rol", "direccion", "destino_cod", "destino", "ubicacion", "sup_terreno_m2", "sup_construida_m2",
              "pisos_max", "anio_construccion", "periodo_sii", "lat_sii", "lon_sii"]


def reasignar_por_punto(g: gpd.GeoDataFrame, max_m: float = 50.0) -> tuple[gpd.GeoDataFrame, dict, pd.DataFrame]:
    """Reasigna cada rol con datos al polígono que **contiene su propio punto del SII** (`lat_sii`, `lon_sii`), entre todos los polígonos
    de la comuna. Si el punto no cae en ninguno, usa el más cercano a no más de `max_m` metros y lo marca aproximado (`cercano_50m`,
    no exacto). Un rol sin punto conserva su asignación; uno con punto pero sin polígono a `max_m` queda sin polígono (se devuelve en
    la lista). Un polígono que se queda sin roles pasa a huérfano. Entre polígonos casi iguales gana el más pequeño y, en empate,
    el que no viene del respaldo archivado. Devuelve (tabla, cifras, filas de los roles que quedaron sin polígono)."""
    g = g.reset_index(drop=True)
    datos = g["datos_sii"] & g["rol"].notna()
    pool = g.drop_duplicates("id_poligono")[["id_poligono", "geometry", "area_poligono_m2", "origen"]].reset_index(drop=True)
    pid = {v: i for i, v in enumerate(pool["id_poligono"])}
    proj = gpd.GeoSeries(pool.geometry.values, crs=4326).to_crs(CRS_AREA)
    tree = shapely.STRtree(proj.values)
    prio = (pool["origen"].eq("respaldo1").astype(int) * 1e12 + pool["area_poligono_m2"].fillna(1e11)).to_numpy()
    roles = g[datos].sort_values("exacto", ascending=False, kind="stable").drop_duplicates("rol").copy()
    roles["_orig"] = roles["id_poligono"].map(pid)
    tiene = roles["lat_sii"].notna() & roles["lon_sii"].notna()
    pts = gpd.GeoSeries(gpd.points_from_xy(roles.loc[tiene, "lon_sii"], roles.loc[tiene, "lat_sii"]), index=roles.index[tiene], crs=4326).to_crs(CRS_AREA)
    asignado = pd.Series(-1, index=roles.index, dtype="int64")
    tipo = pd.Series("sin_punto", index=roles.index, dtype=object)
    if len(pts):
        pp, tt = tree.query(pts.values, predicate="within")
        if len(pp):
            df = pd.DataFrame({"p": pp, "t": tt, "prio": prio[tt]}).sort_values(["p", "prio"], kind="stable").drop_duplicates("p")
            asignado.loc[pts.index[df["p"].to_numpy()]] = df["t"].to_numpy()
            tipo.loc[pts.index[df["p"].to_numpy()]] = "dentro"
        resto = pts[tipo.loc[pts.index] == "sin_punto"]
        tipo.loc[resto.index] = "ninguno"
        if len(resto):
            (pn, tn), dist = tree.query_nearest(resto.values, max_distance=max_m, return_distance=True, all_matches=False)
            if len(pn):
                asignado.loc[resto.index[pn]] = tn
                tipo.loc[resto.index[pn]] = "cercano"
    sin_punto = tipo == "sin_punto"
    asignado[sin_punto] = roles.loc[sin_punto, "_orig"]
    perdidos = roles.index[tipo == "ninguno"]
    ok = roles.loc[asignado >= 0].copy()
    a = asignado.loc[ok.index].to_numpy()
    ok["geometry"] = pool.geometry.values[a]
    ok["id_poligono"] = pool["id_poligono"].values[a]
    ok["area_poligono_m2"] = pool["area_poligono_m2"].values[a]
    t = tipo.loc[ok.index]
    ok.loc[t == "dentro", "exacto"] = True
    ok.loc[t == "dentro", "metodo"] = METODO_PUNTO
    ok.loc[t == "cercano", "exacto"] = False
    ok.loc[t == "cercano", "metodo"] = METODO_CERCANO
    usados = set(ok["id_poligono"])
    huerf = g[~g["id_poligono"].isin(usados) & (g["origen"] != "respaldo1")].drop_duplicates("id_poligono").copy()
    for c in CAMPOS_ROL:
        huerf[c] = None
    huerf["datos_sii"], huerf["exacto"], huerf["metodo"] = False, False, "huerfano"
    out = pd.concat([ok.drop(columns="_orig"), huerf], ignore_index=True)
    out = gpd.GeoDataFrame(out[COLUMNAS], geometry="geometry", crs=4326)
    cambiaron = int((ok["id_poligono"].values != roles.loc[ok.index, "id_poligono"].values).sum())
    cifras = {"roles": len(roles), "dentro": int((tipo == "dentro").sum()), "cercano_50m": int((tipo == "cercano").sum()),
              "sin_punto_conservados": int(sin_punto.sum()), "sin_poligono": int(len(perdidos)), "cambiaron_de_poligono": cambiaron}
    perd = pd.DataFrame(roles.loc[perdidos, ["cut", "cod_sii", "comuna", *CAMPOS_ROL, "origen", "metodo"]]).reset_index(drop=True)
    return out, cifras, perd


def _escribir(limpio: gpd.GeoDataFrame, p: Path) -> gpd.GeoDataFrame:
    """Escribe el parquet de una comuna: orden espacial (Hilbert) y grupos de 2.000 filas con la caja envolvente de cada predio (`bbox`);
    DuckDB salta casi todos los grupos al buscar un punto (de ≈ 1.500 ms a ≈ 15 ms en Temuco, 137.728 polígonos)."""
    tmp = p.with_suffix(".tmp")
    limpio = limpio.iloc[limpio.geometry.hilbert_distance().argsort()]
    limpio.to_parquet(tmp, compression="zstd", write_covering_bbox=True, row_group_size=2000)
    tmp.replace(p)
    return limpio


def _estadisticas(limpio: gpd.GeoDataFrame, p: Path) -> dict:
    """Cifras de calidad de una comuna para el manifiesto."""
    ids = limpio["id"].dropna()
    per = limpio["periodo_sii"].dropna()
    datos = limpio[limpio["datos_sii"] & limpio["rol"].notna()]
    unidades = limpio[limpio["n_unidades"] > 1]
    return {"n": len(limpio), "periodo": per.value_counts().index[0] if len(per) else "",       # semestre más frecuente de la comuna
            "pct_periodo": round(float(len(per)) / max(1, int(limpio["datos_sii"].sum())), 3),   # fracción de predios con periodo legible
            "n_datos_sii": int(limpio["datos_sii"].sum()), "n_roles": int(datos["rol"].nunique()), "n_exactos": int(limpio["exacto"].sum()),
            "n_huerfanos": int((limpio["metodo"] == "huerfano").sum()), "n_ids_repetidos": int(ids.duplicated().sum()),
            "n_terrenos_copropiedad": int(unidades["id_poligono"].nunique()), "n_roles_copropiedad": int(unidades["rol"].nunique()),
            "mb": round(p.stat().st_size / 1e6, 1)}


def completar_con_archivo(destino_dir: Path, archivo_dir: Path) -> pd.DataFrame:
    """Suma a cada comuna de `destino_dir` los roles con datos que solo están en el respaldo archivado de `archivo_dir` (los parquet de
    un respaldo anterior). Unión por rol: **gana lo que ya está en `destino_dir`**; del archivo solo se conservan los roles ausentes (su
    polígono incluido) y las comunas que el respaldo nuevo no cubre. Un rol que el respaldo nuevo conoce pero sin polígono
    (`sin_poligono/<cut>.csv`) se rellena con el del archivado, marcado `relleno_respaldo1` y no exacto. No toca `archivo_dir`. Actualiza el manifiesto y devuelve, por
    comuna, cuántos roles se conservaron."""
    destino_dir, archivo_dir = Path(destino_dir), Path(archivo_dir)
    mf = destino_dir / "manifiesto_predios.csv"
    m = pd.read_csv(mf, encoding="utf-8-sig", dtype={"cut": str, "cod_sii": str})
    filas = []
    for ap in sorted(archivo_dir.glob("*.parquet")):
        viejo = gpd.read_parquet(ap)
        for c in COLUMNAS:       # un archivo de un esquema anterior: se completan las columnas nuevas
            if c not in viejo.columns:
                viejo[c] = None
        viejo["id_poligono"] = _id_poligono(viejo.geometry.values)
        viejo["origen"] = viejo["origen"].fillna("respaldo1") if viejo["origen"].notna().any() else "respaldo1"
        p = destino_dir / ap.name
        if p.exists():
            nuevo = gpd.read_parquet(p)
            sp = destino_dir / "sin_poligono" / f"{ap.stem}.csv"
            sin_pol = set(pd.read_csv(sp, dtype=str)["rol"].dropna()) if sp.exists() else set()
            falta = viejo[viejo["datos_sii"] & viejo["rol"].notna() & ~viejo["rol"].isin(set(nuevo["rol"].dropna()))].copy()
            # Los roles que el respaldo nuevo conoce SIN polígono se rellenan con el polígono del archivado, pero como aproximado: no
            # exacto y con método propio (no se agrupan en copropiedad y la API avisa de dónde sale el polígono).
            rel = falta["rol"].isin(sin_pol)
            falta.loc[rel, "exacto"] = False
            falta.loc[rel, "metodo"] = METODO_RELLENO
            if falta.empty:
                filas.append({"cut": ap.stem, "roles_conservados": 0, "accion": "sin cambios"})
                continue
            total = pd.concat([nuevo, falta[COLUMNAS]], ignore_index=True)
            accion = "unión por rol"
        else:
            total, falta, accion = viejo[COLUMNAS], viejo[viejo["datos_sii"]], "comuna solo del respaldo anterior"
        total, _ = agrupar_unidades(gpd.GeoDataFrame(total, geometry="geometry", crs=4326))
        _escribir(total, p)
        n_rel = int((falta["metodo"] == METODO_RELLENO).sum())
        filas.append({"cut": ap.stem, "roles_conservados": int(falta["rol"].nunique()), "roles_rellenados": n_rel, "accion": accion})
        i = m.index[(m["cut"] == ap.stem) & (m["estado"] == "ok")]
        if len(i) == 1:
            for k, v in _estadisticas(total, p).items():
                m.loc[i[0], k] = v
            m.loc[i[0], "roles_conservados_respaldo1"] = int(falta["rol"].nunique())
            m.loc[i[0], "roles_rellenados_respaldo1"] = n_rel
    m.to_csv(mf, index=False, encoding="utf-8-sig")
    return pd.DataFrame(filas)


def reasignar_todo(destino_dir: Path, max_m: float = 50.0, progreso=None) -> pd.DataFrame:
    """`reasignar_por_punto` en cada comuna de `destino_dir`. Reescribe los parquet, actualiza el manifiesto y guarda los roles que
    quedan sin polígono (con sus datos y su punto) en `sin_poligono/<cut>_roles.parquet`, para no perder su información. Devuelve, por
    comuna, las cifras de antes y después."""
    destino_dir = Path(destino_dir)
    mf = destino_dir / "manifiesto_predios.csv"
    m = pd.read_csv(mf, encoding="utf-8-sig", dtype={"cut": str, "cod_sii": str})
    (destino_dir / "sin_poligono").mkdir(exist_ok=True)
    archivos = sorted(destino_dir.glob("*.parquet"))
    filas = []
    for i, p in enumerate(archivos, 1):
        if progreso:
            progreso("predios", p.stem, i, len(archivos))
        g = gpd.read_parquet(p)
        d0 = g[g["datos_sii"] & g["rol"].notna()].drop_duplicates("rol")
        out, c, perd = reasignar_por_punto(g, max_m)
        out, _ = agrupar_unidades(out)
        out = _escribir(out, p)
        side = destino_dir / "sin_poligono" / f"{p.stem}_roles.parquet"
        if len(perd) or side.exists():          # se suman a los de corridas anteriores, salvo los que ya tienen polígono
            prev = pd.read_parquet(side) if side.exists() else perd.iloc[0:0]
            todos = pd.concat([prev[~prev["rol"].isin(out["rol"].dropna())], perd], ignore_index=True).drop_duplicates("rol", keep="last")
            todos.to_parquet(side, index=False) if len(todos) else side.unlink(missing_ok=True)
        d1 = out[out["datos_sii"] & out["rol"].notna()].drop_duplicates("rol")
        filas.append({"cut": p.stem, "comuna": out["comuna"].iloc[0] if len(out) else "", "roles": c["roles"],
                      "exactos_antes": int(d0["exacto"].sum()), "exactos_despues": int(d1["exacto"].sum()),
                      "aprox_antes": int((~d0["exacto"]).sum()), "aprox_despues": int((~d1["exacto"]).sum()),
                      "dentro": c["dentro"], "cercano_50m": c["cercano_50m"], "sin_punto_conservados": c["sin_punto_conservados"],
                      "sin_poligono": c["sin_poligono"], "cambiaron_de_poligono": c["cambiaron_de_poligono"],
                      "max_roles_exactos_en_un_terreno": int(out["n_unidades"].max()), "max_roles_asignados_en_un_terreno": int(out["n_asignados"].max())})
        j = m.index[(m["cut"] == p.stem) & (m["estado"] == "ok")]
        if len(j) == 1:
            for k, v in _estadisticas(out, p).items():
                m.loc[j[0], k] = v
            m.loc[j[0], ["roles_reasignados_punto", "roles_cercano_50m", "roles_sin_poligono_50m"]] = [c["dentro"], c["cercano_50m"], c["sin_poligono"]]
    m.to_csv(mf, index=False, encoding="utf-8-sig")
    return pd.DataFrame(filas)


def convertir_todo(origen: Path, destino_dir: Path, comunas_bcn: gpd.GeoDataFrame, f_cut: str, f_nombre: str, progreso=None,
                   min_participacion: float = 0.9, patron: str = "*.gpkg", etiqueta: str | None = None) -> pd.DataFrame:
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
            f = convertir_gpkg(p, comunas_bcn, f_cut, f_nombre, destino_dir, min_participacion, protegidos, etiqueta)
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
