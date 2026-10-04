"""Lámina comunal en PDF (A3 horizontal) con `sig/`, desde Regu Suelo (POST /api/lamina?cut=).

`sig.render.generar()` espera un GPKG con las capas `comunas` y `capa_ipt` de UNA comuna; el GPKG nacional de build no
trae `comunas`, así que se arma uno temporal por comuna (con la vigencia del Portal IPT) y se borra al terminar. El PDF
queda en `<paths.out>/laminas/<cut>_<gpkg>.pdf` y se reutiliza mientras el GPKG de build no cambie.

Limitación vigente: el `sig/` de `master` tiene fijos el rótulo de región y el minimapa de La Araucanía (arreglado en la
rama `codex/sig-layouts`, pendiente de PR). Hasta integrarla, `app.lamina_regiones` (config.yaml) limita las regiones
donde se ofrece la lámina; con `todas` se ofrece en todas.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import geopandas as gpd

from etl import mapa
from etl.normalize import norm_txt


class LaminaNoDisponible(Exception):
    """La lámina no se puede generar (región no soportada, falta el minimapa…): el mensaje se muestra al usuario."""


def pmtiles_minimapa(a, region: str) -> Path:
    """PMTiles de la región para el minimapa: `<out>/mapa_<región>/capa_ipt.pmtiles` (lo genera `run.py mapa --region`).
    La carpeta `mapa_araucania` sirve a «Región de La Araucanía» (el nombre de la carpeta está contenido en el de la región)."""
    reg = norm_txt(region)
    for p in sorted(Path(a.dir_out).glob("mapa_*/capa_ipt.pmtiles")):
        if norm_txt(p.parent.name.removeprefix("mapa_").replace("_", " ")) in reg:
            return p
    raise LaminaNoDisponible(f"Falta el PMTiles regional del minimapa de {region}: corre `python run.py mapa --region <región>`.")


def lamina_pdf(a, cut: str) -> Path:
    """Genera (o reutiliza) la lámina PDF de la comuna `cut` y devuelve su ruta."""
    from sig import render
    comuna = next((c for c in _comunas(a) if c["cut"] == cut), None)
    if comuna is None:
        raise KeyError(cut)
    if a.lamina_regiones is not None and comuna["region"] not in a.lamina_regiones:
        raise LaminaNoDisponible(
            f"La lámina aún solo se ofrece para {', '.join(a.lamina_regiones)}: el sig/ de master tiene fijos el rótulo y el "
            "minimapa de La Araucanía (arreglado en la rama codex/sig-layouts, pendiente de PR).")
    destino = Path(a.dir_out) / "laminas" / f"{cut}_{a.gpkg.stem}.pdf"
    if destino.exists() and destino.stat().st_mtime >= a.gpkg.stat().st_mtime:
        return destino
    pm = pmtiles_minimapa(a, comuna["region"])
    with tempfile.TemporaryDirectory() as d:
        # el nombre del GPKG sale impreso en el pie de la lámina: se deja el del build, no uno temporal
        tmp = Path(d) / f"{a.gpkg.stem}_{cut}.gpkg"
        com = gpd.read_file(a.comunas)
        com["cut"] = [str(int(x)).zfill(5) if str(x).strip().isdigit() else str(x) for x in com[a.f_cut]]
        com = com[com["cut"] == cut].rename(columns={a.f_nombre: "comuna", a.f_region: "region"})[["cut", "comuna", "region", "geometry"]]
        capa = gpd.read_file(a.gpkg, layer="capa_ipt", where=f"cut = '{cut}'")
        capa = mapa.unir_vigencia(capa, Path(a.gpkg).parent / "vigencia_match.csv")
        com.to_crs(4326).to_file(tmp, layer="comunas", driver="GPKG")
        capa.to_file(tmp, layer="capa_ipt", driver="GPKG")
        destino.parent.mkdir(parents=True, exist_ok=True)
        try:
            render.generar(cut, destino, tmp, pm, render.plantilla("lamina_comuna"))
        except ValueError as ex:
            raise LaminaNoDisponible(str(ex)) from ex
    return destino


def _comunas(a):
    from . import datos
    return datos.comunas(a)
