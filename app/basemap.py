"""Mapa base OSM para Regu Suelo local: proxy con User-Agent identificable y caché en disco.

La política de teselas de OpenStreetMap (https://operations.osmfoundation.org/policies/tiles/) exige identificar la
aplicación con un User-Agent propio, y un navegador no puede fijarlo en las teselas de un mapa. Por eso el navegador
pide `/basemap/osm/{z}/{x}/{y}.png` a este servidor, que las trae de openstreetmap.org con `app.user_agent`, las guarda
en `<paths.out>/cache_basemap/osm/` (la política pide cachear) y limita a 2 conexiones simultáneas. Uso local: es el
mapa de trabajo de una persona, no una descarga masiva ni un servicio público.

Los fondos satelitales (Esri y EOX) NO pasan por aquí: el navegador los pide directo, sin guardarlos en disco.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import requests

URL_OSM = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
UA_DEF = "ReguSueloLocal/1.0 (uso local; +https://github.com/SebaGeoZ92/regu-ipt-etl)"
ZOOM_MAX = 19
MAX_BYTES = 500_000
_SEM = threading.Semaphore(2)


class TeselaNoDisponible(Exception):
    """No hay tesela (sin internet y sin copia en disco, o la respuesta no es una imagen)."""


def _valida(z: int, x: int, y: int) -> None:
    if not (0 <= z <= ZOOM_MAX and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise ValueError(f"tesela fuera de rango: {z}/{x}/{y}")


def tesela_osm(a, z: int, x: int, y: int) -> Path:
    """Ruta del PNG de la tesela OSM z/x/y: de la caché si es reciente, si no de openstreetmap.org.
    Si no hay internet y existe una copia vencida, se usa esa (trabajo sin conexión)."""
    _valida(z, x, y)
    destino = Path(a.cache_basemap) / "osm" / str(z) / str(x) / f"{y}.png"
    ttl = a.osm_ttl_dias * 86400
    if destino.exists() and time.time() - destino.stat().st_mtime < ttl:
        return destino
    try:
        with _SEM:
            r = requests.get(a.osm_url.format(z=z, x=x, y=y), headers={"User-Agent": a.user_agent}, timeout=15)
        ok = r.status_code == 200 and r.headers.get("content-type", "").startswith("image/") and len(r.content) <= MAX_BYTES
        if not ok:
            raise TeselaNoDisponible(f"OSM respondió {r.status_code} ({r.headers.get('content-type')}) para {z}/{x}/{y}")
    except (requests.RequestException, TeselaNoDisponible) as ex:
        if destino.exists():
            return destino                      # copia vencida: mejor eso que dejar el mapa vacío
        raise TeselaNoDisponible(str(ex)) from ex
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(".tmp")
    tmp.write_bytes(r.content)
    os.replace(tmp, destino)
    return destino
