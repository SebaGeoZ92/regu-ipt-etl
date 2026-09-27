"""Cliente mínimo y tolerante a fallos para ArcGIS REST (geoide.minvu.cl).

- Reintentos con backoff ante 5xx/429 y ante la página HTML de error del Web Adaptor.
- Paginación por resultOffset si la capa la soporta; si no, por lotes de objectIds.
- Toda descarga queda en caché en disco: el servidor es inestable, no se consulta en vivo.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)


class ArcGISError(RuntimeError):
    pass


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")


class ArcGISClient:
    def __init__(self, base_url: str, timeout: int = 90, retries: int = 6,
                 backoff: float = 2.0, pause_s: float = 0.5, geometry_precision: int = 7):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.pause = pause_s
        self.retries = retries
        self.backoff = backoff
        self.geometry_precision = geometry_precision
        s = requests.Session()
        retry = Retry(total=2, backoff_factor=1.0,  # el loop propio maneja el resto
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET", "POST"))
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://", HTTPAdapter(max_retries=retry))
        s.headers["User-Agent"] = "regu-ipt-etl/0.1 (+https://www.regu.cl)"
        self.session = s

    # ── bajo nivel ──────────────────────────────────────────
    def _request(self, url: str, params: dict | None = None, post: bool = False) -> dict:
        params = dict(params or {})
        params.setdefault("f", "json")
        last_exc: Exception | None = None
        for intento in range(1, self.retries + 1):
            try:
                if post:
                    resp = self.session.post(url, data=params, timeout=self.timeout)
                else:
                    resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                try:
                    data = resp.json()
                except ValueError as e:  # HTML del Web Adaptor ("Could not access any server machines")
                    raise ArcGISError(f"Respuesta no JSON ({resp.status_code})") from e
                if isinstance(data, dict) and "error" in data:
                    raise ArcGISError(f"{data['error'].get('code')}: {data['error'].get('message')}")
                time.sleep(self.pause)
                return data
            except (requests.RequestException, ArcGISError) as e:
                last_exc = e
                espera = self.backoff ** intento
                log.warning("Intento %s/%s falló en %s: %s · reintento en %.0fs",
                            intento, self.retries, url, e, espera)
                time.sleep(espera)
        raise ArcGISError(f"Sin respuesta válida de {url}: {last_exc}")

    # ── catálogo ────────────────────────────────────────────
    def list_services(self, folder: str) -> list[dict]:
        data = self._request(f"{self.base}/{folder}")
        return [s for s in data.get("services", []) if s.get("type") in ("MapServer", "FeatureServer")]

    def service_url(self, name: str, stype: str) -> str:
        return f"{self.base}/{name}/{stype}"

    def service_info(self, name: str, stype: str) -> dict:
        return self._request(self.service_url(name, stype))

    def layer_info(self, layer_url: str) -> dict:
        return self._request(layer_url)

    # ── descarga ────────────────────────────────────────────
    def fetch_layer(self, layer_url: str, out_sr: int = 4326) -> dict:
        """Devuelve un FeatureCollection GeoJSON completo (paginado)."""
        info = self.layer_info(layer_url)
        if info.get("type") != "Feature Layer":
            raise ArcGISError(f"{layer_url} no es Feature Layer ({info.get('type')})")
        page = int(info.get("maxRecordCount") or 1000)
        paginable = bool(info.get("advancedQueryCapabilities", {}).get("supportsPagination"))
        base_q = {
            "where": "1=1", "outFields": "*", "returnGeometry": "true",
            "outSR": out_sr, "geometryPrecision": self.geometry_precision, "f": "geojson",
        }
        features: list[dict] = []
        if paginable:
            offset = 0
            while True:
                fc = self._request(f"{layer_url}/query",
                                   {**base_q, "resultOffset": offset, "resultRecordCount": page}, post=True)
                feats = fc.get("features", [])
                features.extend(feats)
                exceeded = fc.get("exceededTransferLimit") or fc.get("properties", {}).get("exceededTransferLimit")
                if not feats or (len(feats) < page and not exceeded):
                    break
                offset += len(feats)
        else:
            ids = self._request(f"{layer_url}/query",
                                {"where": "1=1", "returnIdsOnly": "true", "f": "json"}, post=True)
            oids = sorted(ids.get("objectIds") or [])
            for i in range(0, len(oids), page):
                lote = oids[i:i + page]
                fc = self._request(f"{layer_url}/query",
                                   {**base_q, "where": "", "objectIds": ",".join(map(str, lote))}, post=True)
                features.extend(fc.get("features", []))
        return {"type": "FeatureCollection", "features": features,
                "_meta": {"source": layer_url, "count": len(features),
                          "fields": [f.get("name") for f in info.get("fields") or []],
                          "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}}


def discover(client: ArcGISClient, folders: list[str], service_filter=None) -> list[dict]:
    """Recorre servicios y capas hoja. service_filter: callable(service_name, service_info) -> bool."""
    catalogo = []
    for folder in folders:
        for s in client.list_services(folder):
            name, stype = s["name"], s["type"]
            try:
                sinfo = client.service_info(name, stype)
            except ArcGISError as e:
                log.error("No se pudo leer %s: %s", name, e)
                continue
            if service_filter and not service_filter(name, sinfo):
                continue
            for lyr in sinfo.get("layers", []):
                if lyr.get("subLayerIds"):  # grupo, no capa hoja
                    continue
                catalogo.append({
                    "service": name, "service_type": stype,
                    "layer_id": lyr["id"], "layer_name": lyr["name"],
                    "url": f"{client.service_url(name, stype)}/{lyr['id']}",
                    "service_wkid": (sinfo.get("spatialReference") or {}).get("latestWkid")
                                    or (sinfo.get("spatialReference") or {}).get("wkid"),
                })
            log.info("%s · %d capas", name, len(sinfo.get("layers", [])))
    return catalogo


def raw_path(raw_dir: Path, entry: dict) -> Path:
    return raw_dir / slug(entry["service"]) / f"{entry['layer_id']:03d}_{slug(entry['layer_name'])}.geojson"


def download_entry(client: ArcGISClient, entry: dict, raw_dir: Path, refresh: bool = False) -> Path | None:
    p = raw_path(raw_dir, entry)
    if p.exists() and not refresh:
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    fc = client.fetch_layer(entry["url"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    log.info("↓ %s/%s · %d features", entry["service"], entry["layer_name"], fc["_meta"]["count"])
    return p
