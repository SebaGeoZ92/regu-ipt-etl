#!/usr/bin/env python3
"""Descarga la División Comunal de la BCN (SIIT) a data/base/ y muestra sus campos.

Uso (desde la raíz de regu-ipt-etl):  python descargar_comunas.py
Fuente: Biblioteca del Congreso Nacional de Chile · https://www.bcn.cl/siit/mapas_vectoriales
"""
import io
import zipfile
from pathlib import Path

import geopandas as gpd
import requests

URL = "https://www.bcn.cl/obtienearchivo?id=repositorio/10221/10396/5/comunas_final.zip"
DEST = Path(__file__).parent / "data" / "base" / "comunas_bcn"

DEST.mkdir(parents=True, exist_ok=True)
print(f"Descargando {URL} ...")
r = requests.get(URL, timeout=300, headers={"User-Agent": "regu-ipt-etl/0.1"})
r.raise_for_status()
zipfile.ZipFile(io.BytesIO(r.content)).extractall(DEST)
print(f"OK · {len(r.content) / 1e6:.1f} MB extraídos en {DEST}")

for shp in DEST.rglob("*.shp"):
    d = gpd.read_file(shp)
    print(f"\n== {shp.relative_to(Path(__file__).parent).as_posix()}")
    print("filas:", len(d), "| crs:", d.crs)
    print("campos:", [c for c in d.columns if c != "geometry"])
    print(d.drop(columns="geometry").head(3).to_string())
