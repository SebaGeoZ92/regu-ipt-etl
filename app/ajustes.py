"""Rutas y opciones de Regu Suelo local (docs/REGU_SUELO_LOCAL.md). Todo sale de config.yaml / config.local.yaml."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from etl.ficha import ultimo_gpkg

MARCA_BORRADOR = "BORRADOR · uso interno"


@dataclass
class Ajustes:
    gpkg: Path
    dir_footprints: Path
    normas: Path
    dir_out: Path | None = None
    dir_demanda: Path | None = None
    comunas: Path | None = None            # shapefile/gpkg de la división comunal (para /api/comunas)
    f_cut: str = "cod_comuna"
    f_nombre: str = "Comuna"
    f_region: str = "Region"
    registrar: bool = True                 # anotar la consulta en data/demanda (sin coordenadas)

    @classmethod
    def desde_cfg(cls, cfg: dict, ruta: Callable[[dict, str], Path], gpkg: Path | None = None) -> "Ajustes":
        out = ruta(cfg, "out")
        g = gpkg or ultimo_gpkg(out)
        if g is None:
            raise FileNotFoundError(f"No hay GPKG de build en {out}: corre `python run.py build`")
        c = cfg["comunas"]
        return cls(gpkg=Path(g), dir_footprints=ruta(cfg, "footprints"), normas=ruta(cfg, "normas"), dir_out=out,
                   dir_demanda=ruta(cfg, "demanda"), comunas=ruta(cfg, "comunas"),
                   f_cut=c["field_cut"], f_nombre=c["field_nombre"], f_region=c["field_region"])
