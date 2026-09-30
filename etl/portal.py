"""Cliente de la API pública del Portal IPT de MINVU (https://portalipt-api.minvu.cl), la que usa su frontend.

Endpoints (verificados el 28-sep-2026, ver CLAUDE.md):
  GET /instrumentos?estado=Vigente → lista (≈2.000 instrumentos, ≈11 MB): id, codigo, denominacion,
      planificacion (Comunal|Intercomunal), tipo (PRC|PS|LU|PRI|PRM|PRDU), comunas [CUT INE], clasificacion
      (Instrumento de origen|Modificación|…), numeroDocumento, fechaInicioVigencia, documentos [{tipo, nombre, url}],
      instrumentosDescendientesIds, fechas de publicación/derogación, etc.
  GET /instrumentos/{id} → lo mismo + instrumentosBase, relacionesBase/Descendientes, plantilla.
  GET /comunas → [{idComuna, idProvincia, nombre, codigoCompuestoComunaINE, activo}]
  GET /regiones → [{idRegion, nombre, ordinal, ordenGeografico, codigoRegionINE, activo}]
Cortesía: caché en disco (data/raw/portal/), pausa entre solicitudes y reintentos con backoff.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)
BASE = "https://portalipt-api.minvu.cl"
DOC_ORDENANZA = ("Ordenanza", "Publicación D. O. con Ordenanza")
_RE_NORMA = re.compile(r"(Decreto|Resoluci[oó]n|D\.?\s*S\.?)\s*(?:Exento\s*)?N[°º]?\s*([\w\-/]+)", re.I)


class PortalIPT:
    def __init__(self, cache_dir: Path, pause_s: float = 1.0, retries: int = 4, timeout: int = 180):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.pause_s, self.retries, self.timeout = pause_s, retries, timeout
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json", "User-Agent": "regu-ipt-etl (uso personal, con caché)"})
        self._ultimo = 0.0

    def _ruta_cache(self, ruta: str) -> Path:
        return self.cache / (re.sub(r"[^A-Za-z0-9]+", "_", ruta).strip("_") + ".json")

    def get(self, ruta: str, refresh: bool = False):
        p = self._ruta_cache(ruta)
        if p.exists() and not refresh:
            return json.loads(p.read_text(encoding="utf-8"))["datos"]
        for intento in range(self.retries):
            espera = self.pause_s - (time.time() - self._ultimo)
            if espera > 0:
                time.sleep(espera)
            self._ultimo = time.time()
            try:
                r = self.s.get(f"{BASE}/{ruta.lstrip('/')}", timeout=self.timeout)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                datos = r.json()
                p.write_text(json.dumps({"url": f"{BASE}/{ruta.lstrip('/')}",
                                         "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                         "datos": datos}, ensure_ascii=False), encoding="utf-8")
                return datos
            except (requests.RequestException, ValueError) as ex:
                log.warning("Portal IPT %s: %s (intento %d/%d)", ruta, ex, intento + 1, self.retries)
                time.sleep(2 ** intento * self.pause_s)
        raise RuntimeError(f"Portal IPT: no se pudo obtener {ruta}")

    def vigentes(self, refresh=False) -> list[dict]:
        return self.get("instrumentos?estado=Vigente", refresh)

    def comunas(self, refresh=False) -> list[dict]:
        return self.get("comunas", refresh)

    def regiones(self, refresh=False) -> list[dict]:
        return self.get("regiones", refresh)

    def instrumento(self, iid: int, refresh=False) -> dict:
        return self.get(f"instrumentos/{iid}", refresh)


def norma(inst: dict) -> str:
    """'Decreto N° 540' / 'Resolución N° 149' desde el nombre de sus documentos; si no, 'N° <numeroDocumento>'."""
    for d in inst.get("documentos") or []:
        m = _RE_NORMA.search(d.get("nombre") or "")
        if m:
            tipo = m.group(1).capitalize().replace("Resolucion", "Resolución")
            return f"{'D.S.' if tipo.upper().startswith('D.') else tipo} N° {m.group(2)}"
    n = inst.get("numeroDocumento")
    return f"N° {n}" if n else ""


def url_ordenanza(inst: dict) -> str | None:
    """PDF de la ordenanza (o publicación en el Diario Oficial con ordenanza), si el portal lo enlaza."""
    for tipo in DOC_ORDENANZA:
        for d in inst.get("documentos") or []:
            if d.get("tipo") == tipo and d.get("url"):
                return d["url"]
    return None
