"""Rutas y opciones de Regu Suelo local (docs/REGU_SUELO_LOCAL.md). Todo sale de config.yaml / config.local.yaml."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from etl.ficha import ultimo_gpkg

MARCA_BORRADOR = "BORRADOR · uso interno"
TEMAS_TESELAS = ("normativa", "ocupacion", "comunas")


def _regiones_lamina(cfg: dict) -> tuple[str, ...] | None:
    """`app.lamina_regiones` de config.yaml: lista de regiones, o `todas` (None) cuando el sig/ ya sirve para cualquier región."""
    v = (cfg.get("app") or {}).get("lamina_regiones", ["Región de La Araucanía"])
    return None if v in ("todas", None) else tuple(v)


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
    altura_piso_ref_m: float = 3.5         # parámetro del modelo (NO es norma): ver config.yaml, bloque `volumen`
    dir_tiles: Path | None = None          # PMTiles locales (python run.py teselas); por defecto <dir_out>/tiles
    lamina_regiones: tuple[str, ...] | None = ("Región de La Araucanía",)   # None = todas (config.yaml: app.lamina_regiones)
    user_agent: str = "ReguSueloLocal/1.0 (uso local; +https://github.com/SebaGeoZ92/regu-ipt-etl)"   # app.user_agent
    osm_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    osm_ttl_dias: int = 14                 # una tesela OSM en disco se reutiliza este tiempo antes de pedirla otra vez

    @classmethod
    def desde_cfg(cls, cfg: dict, ruta: Callable[[dict, str], Path], gpkg: Path | None = None) -> "Ajustes":
        out = ruta(cfg, "out")
        g = gpkg or ultimo_gpkg(out)
        if g is None:
            raise FileNotFoundError(f"No hay GPKG de build en {out}: corre `python run.py build`")
        c = cfg["comunas"]
        return cls(gpkg=Path(g), dir_footprints=ruta(cfg, "footprints"), normas=ruta(cfg, "normas"), dir_out=out,
                   dir_demanda=ruta(cfg, "demanda"), comunas=ruta(cfg, "comunas"),
                   f_cut=c["field_cut"], f_nombre=c["field_nombre"], f_region=c["field_region"],
                   altura_piso_ref_m=float((cfg.get("volumen") or {}).get("altura_piso_ref_m", 3.5)),
                   dir_tiles=out / "tiles", lamina_regiones=_regiones_lamina(cfg),
                   user_agent=(cfg.get("app") or {}).get("user_agent") or cls.user_agent)

    @property
    def cache_basemap(self) -> Path:
        """Teselas OSM guardadas en disco (app/basemap.py): <dir_out>/cache_basemap."""
        return Path(self.dir_out) / "cache_basemap"

    @property
    def tiles(self) -> Path | None:
        """Carpeta de teselas: dir_tiles, o <dir_out>/tiles si no se indicó."""
        return self.dir_tiles or (Path(self.dir_out) / "tiles" if self.dir_out else None)
