"""V2 de VOLÚMENES (docs/VOLUMENES.md): volumen calculado (V_calc) por predio, según los registros.

V_calc = huella × pisos estimados, con
- huella = suma de la parte de cada edificio de Overture que cae dentro del predio (reparto proporcional al área de la
  intersección; se ignoran las astillas: partes menores al 10 % del edificio o del terreno). La regla original, el edificio entero si cae en más del 50 %
  dentro, sigue disponible como `regla="umbral50"`;
- pisos = max(1, round(superficie construida SII / huella)); sin superficie construida SII se usa `num_floors`
  de Overture si existe y, si no, el predio queda `sin_dato` (no se inventa).

Se entrega `m2_equiv = huella × pisos` (m² construidos equivalentes). El volumen en m³ exige la altura de piso de
referencia, que la ordenanza de Temuco no fija: `v_calc_m3` queda vacío mientras no se entregue `altura_piso_ref_m`.
Cada valor lleva su fuente y su confianza.

Los predios vienen de dos orígenes: el respaldo local de predios del SII (`cargar_predios_locales`, sin consultar catastral.cl; un
registro por terreno, copropiedades sumadas y solo roles con punto exacto) o un JSON guardado desde catastral.cl (`cargar_predios`).
"""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import shape

CRS_AREA = "ESRI:102033"
UMBRAL_DENTRO = 0.5          # regla `umbral50`: el edificio va entero al predio que lo contiene en más de esta fracción
UMBRAL_PARTE = 0.10          # regla `proporcional`: se ignora la parte de un edificio menor a esta fracción de su área...
UMBRAL_PARTE_TERRENO = 0.10  # ...o menor a esta fracción del terreno (astillas por el desfase entre Overture y los polígonos del SII)
COLUMNAS = ["rol", "direccion", "zona", "sup_terreno_m2", "sup_construida_sii_m2", "n_edificios", "huella_m2", "pisos_est",
            "pisos_max_sii", "m2_equiv", "v_calc_m3", "ocupacion_predio", "estado", "fuente_huella", "fuente_pisos",
            "confianza_pisos", "avisos", "n_unidades", "calidad_geometria", "origen"]


def cargar_predios(path: Path) -> gpd.GeoDataFrame:
    """Predios guardados desde catastral.cl (JSON con `predios[]` y `geometry` GeoJSON en EPSG:4326)."""
    datos = json.loads(Path(path).read_text(encoding="utf-8"))["predios"]
    return gpd.GeoDataFrame(pd.DataFrame(datos).drop(columns="geometry"),
                            geometry=[shape(p["geometry"]) for p in datos], crs="EPSG:4326")


def cargar_predios_locales(dir_predios: Path, cut: str, zonas: gpd.GeoDataFrame | None = None, zona: str | None = None,
                           solo_exactos: bool = True, limite: int | None = None, semilla: int = 1) -> gpd.GeoDataFrame:
    """Predios del respaldo local (`paths.predios`, GeoParquet de `etl/predios.py`), sin consultar catastral.cl. Devuelve el mismo esquema
    que `cargar_predios` (rol, direccion, m2_terreno, sup_construida_total, pisos_max, geometry), **un registro por terreno**: si varios
    roles comparten el polígono (copropiedad) se suman sus superficies construidas y la superficie del terreno es la del polígono.
    `solo_exactos` deja los roles cuyo punto del SII está dentro del polígono; los asignados por cercanía no se usan porque su polígono
    puede no ser el del predio. `zona` filtra por la zona normativa (de `zonas`, piezas PRC) del punto representativo del terreno."""
    g = gpd.read_parquet(Path(dir_predios) / f"{cut}.parquet")
    g = g[g["datos_sii"] & g["rol"].notna()]
    if solo_exactos:
        g = g[g["exacto"]]
    if zona and zonas is not None:
        z = zonas[zonas["zona"] == zona]
        if not len(z):
            raise ValueError(f"La zona «{zona}» no existe en las piezas dadas")
        pts = gpd.GeoDataFrame(geometry=g.geometry.representative_point().values, index=g.index, crs=4326)
        dentro = gpd.sjoin(pts, z.to_crs(4326)[["geometry"]], how="inner", predicate="within").index.unique()
        g = g.loc[g.index.intersection(dentro)]
    g = g.sort_values(["id_poligono", "rol"], kind="stable")
    uni = g.groupby("id_poligono", sort=False)
    t = uni.first().reset_index()
    t["n_unidades"] = uni["rol"].nunique().to_numpy()
    t["sup_construida_total"] = uni["sup_construida_m2"].sum(min_count=1).to_numpy()
    t["pisos_max"] = uni["pisos_max"].max().to_numpy()
    uno = t["n_unidades"] == 1
    t["m2_terreno"] = np.where(uno & (t["sup_terreno_m2"] > 0), t["sup_terreno_m2"], t["area_poligono_m2"])
    t["rol"] = np.where(uno, t["rol"], t["rol"] + " (+" + (t["n_unidades"] - 1).astype(str) + " unidades)")
    t["calidad_geometria"] = np.where(uni["exacto"].all().to_numpy(), "exacta", "aproximada")
    out = gpd.GeoDataFrame(t[["rol", "direccion", "m2_terreno", "sup_construida_total", "pisos_max", "n_unidades", "calidad_geometria", "origen"]],
                           geometry=t["geometry"], crs=4326)
    if limite and len(out) > limite:
        out = out.sample(limite, random_state=semilla).sort_index()
    return out.reset_index(drop=True)


def calcular(predios: gpd.GeoDataFrame, edificios: gpd.GeoDataFrame, altura_piso_ref_m: float | None = None,
             zonas: gpd.GeoDataFrame | None = None, regla: str = "proporcional") -> pd.DataFrame:
    """V_calc por predio. `zonas` (opcional) = piezas PRC con `zona` para etiquetar la zona de cada predio.

    `regla` decide qué huella recibe cada predio:
    - `proporcional` (por defecto): cada predio recibe **la parte de cada edificio que cae dentro de él** (el área de la intersección);
      se ignoran las astillas, partes menores al 10 % del edificio o al 10 % del terreno (desfase entre las huellas de Overture y
      los polígonos del SII), salvo que sean la mayoría del edificio. Así una casa pareada o continua que cubre varios lotes se
      reparte entre ellos en vez de descartarse en todos.
    - `umbral50` (la regla original): el edificio completo va al predio que lo contiene en más del 50 %; un edificio repartido por
      mitades queda sin dueño."""
    if regla not in ("proporcional", "umbral50"):
        raise ValueError(f"regla desconocida: {regla}")
    p = predios.to_crs(CRS_AREA).reset_index(drop=True)
    e = edificios.to_crs(CRS_AREA).reset_index(drop=True)
    e["geometry"] = shapely.make_valid(e.geometry.values)
    e["m2"] = e.geometry.area
    par = gpd.sjoin(e[["geometry", "m2"]], p[["geometry"]], how="inner", predicate="intersects")
    dentro = shapely.area(shapely.intersection(e.geometry.loc[par.index].values, p.geometry.iloc[par["index_right"].values].values))
    par = par.assign(frac=dentro / par["m2"].values, aporte=dentro)
    if regla == "umbral50":
        par = par[par["frac"] > UMBRAL_DENTRO].copy()
        par["aporte"] = par["m2"]
    else:      # la parte cuenta si es la mayoría del edificio o si no es una astilla: ≥ 10 % del edificio y ≥ 10 % del terreno
        frac_terreno = par["aporte"].values / p.geometry.area.to_numpy()[par["index_right"].to_numpy()]
        par = par[(par["frac"] > UMBRAL_DENTRO) | ((par["frac"] >= UMBRAL_PARTE) & (frac_terreno >= UMBRAL_PARTE_TERRENO))].copy()
    par["compartido"] = par.groupby(level=0)["index_right"].transform("size") > 1
    por_predio = par.groupby("index_right").agg(n=("m2", "size"), huella=("aporte", "sum"), n_comp=("compartido", "sum"))
    nf = edificios["num_floors"] if "num_floors" in edificios.columns else pd.Series(np.nan, index=edificios.index)
    pisos_ov = (pd.Series(nf.values, index=e.index).loc[par.index].groupby(par["index_right"]).max())

    zona = pd.Series("", index=p.index)
    if zonas is not None and len(zonas):
        z = zonas.to_crs(CRS_AREA)
        pts = gpd.GeoDataFrame(geometry=p.geometry.representative_point(), crs=CRS_AREA)
        zj = gpd.sjoin(pts, z[["geometry", "zona"]], how="left", predicate="within")
        zona = zj.groupby(level=0)["zona"].first().reindex(p.index).fillna("")

    filas = []
    for i, r in p.iterrows():
        n = int(por_predio["n"].get(i, 0))
        huella = float(por_predio["huella"].get(i, 0.0))
        sup_c = float(r.get("sup_construida_total") or 0) or np.nan
        avisos, estado, pisos, fuente, conf = [], "ok", np.nan, "", ""
        sup_t = float(r.get("m2_terreno") or r.geometry.area)
        if huella <= 0:
            estado = "sin_dato"
            avisos.append("sin huella de Overture dentro del predio")
        elif not np.isnan(sup_c):
            pisos, fuente, conf = max(1, round(sup_c / huella)), "sii", "media"
        elif pd.notna(pisos_ov.get(i, np.nan)):
            pisos, fuente, conf = int(pisos_ov[i]), "overture_num_floors", "media"
        else:
            estado = "sin_dato"
            avisos.append("sin superficie construida SII ni num_floors de Overture")
        if estado == "ok" and pisos >= 5:
            conf = "baja"
            avisos.append(f"{pisos} pisos estimados: la huella de Overture puede estar incompleta (la construida SII es mucho mayor que la huella)")
        ncomp = int(por_predio["n_comp"].get(i, 0))
        if ncomp:
            avisos.append(f"{ncomp} edificio{'s' if ncomp > 1 else ''} compartido{'s' if ncomp > 1 else ''} con otros terrenos (pareado o continuo): "
                          "se cuenta solo la parte que cae dentro de este terreno")
        if huella > 0 and not np.isnan(sup_c) and sup_c / huella < 0.5:
            avisos.append("la construida SII es menos de la mitad de la huella: puede haber construcción sin registrar")
        if sup_t and huella > sup_t * 1.05:
            avisos.append("la huella supera la superficie del predio")
        m2 = huella * pisos if estado == "ok" else np.nan
        pm = r.get("pisos_max")
        filas.append({
            "rol": r["rol"], "direccion": r.get("direccion", ""), "zona": zona.get(i, ""), "sup_terreno_m2": round(sup_t, 1),
            "sup_construida_sii_m2": sup_c, "n_edificios": n, "huella_m2": round(huella, 1), "pisos_est": pisos,
            "pisos_max_sii": pm if pd.notna(pm) else np.nan, "m2_equiv": round(m2, 1) if estado == "ok" else np.nan,
            "v_calc_m3": round(m2 * altura_piso_ref_m, 1) if (estado == "ok" and altura_piso_ref_m) else np.nan,
            "ocupacion_predio": round(huella / sup_t, 3) if sup_t else np.nan, "estado": estado,
            "fuente_huella": "overture" if n else "", "fuente_pisos": fuente, "confianza_pisos": conf, "avisos": "; ".join(avisos),
            "n_unidades": r.get("n_unidades", np.nan), "calidad_geometria": r.get("calidad_geometria", ""), "origen": r.get("origen", ""),
        })
    return pd.DataFrame(filas, columns=COLUMNAS)
