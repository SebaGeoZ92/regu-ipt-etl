"""Mapa HTML autocontenido de una región: partición IPT en PMTiles + clic → ficha de la pieza.

Genera UN archivo .html que se puede enviar a una persona: los tiles PMTiles van embebidos en base64 y se leen
desde memoria (sin servidor ni HTTP Range); MapLibre y pmtiles se cargan desde jsDelivr (requiere internet).
Los tiles se generan con el driver PMTiles de GDAL (≥3.8), equivalente a tippecanoe para este volumen.
También produce una variante "fragmento" (sin <html>/<head>/<body>) para publicarla como página compartible.
"""
from __future__ import annotations

import base64
import html
import json
import logging
from datetime import date
from pathlib import Path

import geopandas as gpd
import pyogrio
import requests

log = logging.getLogger(__name__)

MAPLIBRE = "4.7.1"
PMTILES = "3.2.1"
URL_MAPLIBRE_JS = f"https://cdn.jsdelivr.net/npm/maplibre-gl@{MAPLIBRE}/dist/maplibre-gl.js"
URL_MAPLIBRE_CSS = f"https://cdn.jsdelivr.net/npm/maplibre-gl@{MAPLIBRE}/dist/maplibre-gl.css"
URL_PMTILES_JS = f"https://cdn.jsdelivr.net/npm/pmtiles@{PMTILES}/dist/pmtiles.js"
CAMPOS = ["clase", "ipt_tipo", "ipt_nombre", "zona", "norma_titulo", "riesgo", "comuna", "cut"]
# Colores de clase (fijos en ambos temas: son el dato del mapa)
COLORES = {"U1": "#b0442b", "U2": "#e08a3c", "U3": "#8c5aa0", "E": "#d8bf3f", "R1": "#5c8f52", "R2": "#cfd2bf"}
EJEMPLO = {"lon": -72.5904, "lat": -38.7359, "texto": "centro de Temuco"}   # ficha inicial (Araucanía)


def generar_pmtiles(capa: gpd.GeoDataFrame, destino: Path, minzoom: int = 6, maxzoom: int = 14) -> Path:
    g = capa[CAMPOS + ["geometry"]].copy()
    g["riesgo"] = g["riesgo"].fillna(False).astype(int)
    g["zona"] = g["zona"].astype(object).where(g["zona"].notna(), None).map(lambda z: z[:90] if isinstance(z, str) else z)
    destino.unlink(missing_ok=True)
    # encoding="UTF-8" obligatorio: sin él, pyogrio en Windows escribe los textos en cp1252 y el MVT exige UTF-8
    # (el navegador mostraba "Puc�n").
    pyogrio.write_dataframe(g.to_crs(4326), destino, driver="PMTiles", layer="ipt", encoding="UTF-8",
                            dataset_options={"MINZOOM": str(minzoom), "MAXZOOM": str(maxzoom), "MAX_SIZE": "4000000",
                                             "MAX_FEATURES": "500000", "SIMPLIFICATION": "1",
                                             "NAME": "Atlas Normativo", "DESCRIPTION": "Partición IPT (regu-ipt-etl)"})
    return destino


def comunas_geojson(comunas: gpd.GeoDataFrame, f_cut: str, f_nom: str) -> dict:
    c = comunas[[f_cut, f_nom, "geometry"]].to_crs("ESRI:102033")
    c["geometry"] = c.geometry.simplify(40)
    c = c.to_crs(4326).rename(columns={f_cut: "cut", f_nom: "nombre"})
    c["bbox"] = [[round(v, 4) for v in g.bounds] for g in c.geometry]
    gj = json.loads(c.to_json(drop_id=True))
    for f in gj["features"]:   # redondeo a ~1 m para aliviar el HTML
        f["geometry"] = json.loads(json.dumps(f["geometry"]), parse_float=lambda x: round(float(x), 5))
    return gj


def _get(url: str) -> str:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.text


def generar_html(pmtiles: Path, comunas_gj: dict, legal: dict, region: str, fecha: str, destino: Path,
                 fragmento: bool = False) -> Path:
    css_maplibre = _get(URL_MAPLIBRE_CSS)
    for u in (URL_MAPLIBRE_JS, URL_PMTILES_JS):    # versiones fijadas deben existir
        requests.head(u, timeout=60, allow_redirects=True).raise_for_status()
    clases = {k: {"titulo": v["titulo"], "resumen": v["resumen"], "normas": v.get("normas", []),
                  "color": COLORES[k]} for k, v in legal["clases"].items()}
    datos = {"clases": clases, "aviso": legal.get("_aviso", ""), "borrador": legal.get("_estado", ""),
             "region": region, "fecha": fecha, "ejemplo": EJEMPLO}
    b64 = base64.b64encode(pmtiles.read_bytes()).decode("ascii")
    cuerpo = (PLANTILLA
              .replace("__TITULO__", html.escape(f"Atlas Normativo {region.replace('Región de ', '').replace('Región del ', '')}"))
              .replace("__REGION__", html.escape(region))
              .replace("__CSS_MAPLIBRE__", css_maplibre)
              .replace("__URL_MAPLIBRE__", URL_MAPLIBRE_JS)
              .replace("__URL_PMTILES__", URL_PMTILES_JS)
              .replace("__DATOS__", json.dumps(datos, ensure_ascii=False).replace("</", "<\\/"))
              .replace("__COMUNAS__", json.dumps(comunas_gj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))
              .replace("__PMTILES_B64__", b64))
    if not fragmento:
        cuerpo = ('<!doctype html>\n<html lang="es">\n<head>\n<meta charset="utf-8">\n'
                  '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                  + cuerpo.replace("<!--FIN_HEAD-->", "</head>\n<body>", 1) + "\n</body>\n</html>\n")
    else:
        cuerpo = cuerpo.replace("<!--FIN_HEAD-->", "", 1)
    destino.write_text(cuerpo, encoding="utf-8")
    return destino


def generar_mapa(gpkg: Path, comunas: gpd.GeoDataFrame, f_cut: str, f_nom: str, legal: dict, region: str,
                 out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    capa = gpd.read_file(gpkg, layer="capa_ipt")
    pm = generar_pmtiles(capa, out_dir / "capa_ipt.pmtiles")
    gj = comunas_geojson(comunas, f_cut, f_nom)
    fecha = date.today().isoformat()
    completo = generar_html(pm, gj, legal, region, fecha, out_dir / "index.html")
    frag = generar_html(pm, gj, legal, region, fecha, out_dir / "artifact.html", fragmento=True)
    return {"pmtiles": pm, "html": completo, "fragmento": frag,
            "mb_pmtiles": round(pm.stat().st_size / 1e6, 2), "mb_html": round(completo.stat().st_size / 1e6, 2)}


PLANTILLA = r"""<title>__TITULO__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>__CSS_MAPLIBRE__</style>
<style>
/* Mapa a pantalla completa con panel de ficha a la derecha; en teléfono el panel pasa bajo el mapa. */
:root {
  --bg: #f3f4ef; --surface: #fbfbf8; --fg: #1f2622; --muted: #5d665f; --line: #d5d9cf;
  --accent: #1f5f73; --accent-fg: #ffffff; --warn-bg: #fbf1d9; --warn-fg: #6b4a07; --riesgo: #b3261e;
  --map-bg: #e6e8df; --comuna: #2b332d;
  --f-ui: "Public Sans", "Segoe UI", system-ui, sans-serif;
  --f-mono: "IBM Plex Mono", ui-monospace, "Cascadia Mono", Consolas, monospace;
  --panel-w: 380px;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #161a18; --surface: #1d2220; --fg: #e5e9e3; --muted: #9aa39c; --line: #313833;
  --accent: #7cc2d6; --accent-fg: #0f1a1d; --warn-bg: #3a3017; --warn-fg: #f1d68a; --riesgo: #ff8a80;
  --map-bg: #252b27; --comuna: #e5e9e3; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #161a18; --surface: #1d2220; --fg: #e5e9e3; --muted: #9aa39c; --line: #313833;
  --accent: #7cc2d6; --accent-fg: #0f1a1d; --warn-bg: #3a3017; --warn-fg: #f1d68a; --riesgo: #ff8a80;
  --map-bg: #252b27; --comuna: #e5e9e3; color-scheme: dark }
html, body { height: 100%; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 14px/1.5 var(--f-ui); }
.app { height: 100%; display: flex; }
#mapa { flex: 1 1 auto; min-width: 0; position: relative; background: var(--map-bg); }
.panel { width: var(--panel-w); flex: 0 0 var(--panel-w); overflow-y: auto; background: var(--surface);
  border-left: 1px solid var(--line); padding-block: 20px 28px; padding-inline: 20px; display: flex; flex-direction: column; gap: 18px; }
.eyebrow { font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); font-weight: 600; }
h1 { font-size: 22px; line-height: 1.2; margin: 2px 0 0; font-weight: 700; text-wrap: balance; }
h2 { font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); margin: 0 0 8px; font-weight: 600; }
.aviso { background: var(--warn-bg); color: var(--warn-fg); border-radius: 6px; padding: 10px 12px; font-size: 13px; }
.aviso strong { display: block; margin-bottom: 2px; }
.ficha { display: flex; flex-direction: column; gap: 10px; }
.ficha .nota { font-size: 12px; color: var(--muted); }
.clase { display: flex; gap: 10px; align-items: flex-start; }
.chip { flex: 0 0 auto; min-width: 34px; padding: 2px 6px; border-radius: 4px; font: 600 13px/1.4 var(--f-mono);
  text-align: center; color: #1b1b1b; }
.clase .titulo { font-weight: 600; font-size: 15px; line-height: 1.35; }
dl { margin: 0; display: grid; grid-template-columns: max-content 1fr; gap: 6px 14px; }
dt { color: var(--muted); font-size: 12px; padding-top: 1px; }
dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.mono { font-family: var(--f-mono); font-size: 13px; }
.riesgo { display: inline-flex; gap: 6px; align-items: center; color: var(--riesgo); font-weight: 600; font-size: 13px; }
.riesgo::before { content: ""; width: 10px; height: 10px; border: 2px solid currentColor; border-radius: 2px;
  background: repeating-linear-gradient(45deg, currentColor 0 2px, transparent 2px 5px); }
.resumen { font-size: 13px; color: var(--fg); max-width: 65ch; }
.normas { margin: 0; padding-left: 18px; font-size: 12px; color: var(--muted); }
.leyenda { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
.leyenda li { display: grid; grid-template-columns: 34px 1fr; gap: 10px; align-items: center; font-size: 13px; }
.leyenda .chip { font-size: 12px; }
.controles { display: flex; flex-direction: column; gap: 10px; }
label.fila { display: flex; gap: 8px; align-items: center; font-size: 13px; cursor: pointer; }
select { width: 100%; font: inherit; padding: 7px 8px; border-radius: 6px; border: 1px solid var(--line);
  background: var(--bg); color: var(--fg); }
select:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.pie { font-size: 11px; color: var(--muted); margin-top: auto; }
.estado { position: absolute; left: 12px; top: 12px; background: var(--surface); color: var(--fg);
  border: 1px solid var(--line); border-radius: 6px; padding: 6px 10px; font-size: 12px; z-index: 2; }
.maplibregl-ctrl-group { background: var(--surface); }
.maplibregl-ctrl-group button { background-color: var(--surface); }
.maplibregl-ctrl-attrib { background: var(--surface) !important; color: var(--muted); }
@media (max-width: 760px) {
  .app { flex-direction: column; }
  #mapa { flex: 0 0 58%; }
  .panel { width: auto; flex: 1 1 auto; border-left: 0; border-top: 1px solid var(--line); padding-inline: 16px; }
}
@media (prefers-reduced-motion: reduce) { * { scroll-behavior: auto !important; } }
</style>
<!--FIN_HEAD-->
<div class="app">
  <div id="mapa" role="application" aria-label="Mapa de situación normativa del suelo">
    <div class="estado" id="estado">Cargando mapa…</div>
  </div>
  <aside class="panel">
    <header>
      <div class="eyebrow">Atlas Normativo · piloto</div>
      <h1>__REGION__</h1>
    </header>
    <div class="aviso" id="aviso"></div>
    <section class="ficha" id="ficha" aria-live="polite"></section>
    <section class="controles">
      <h2>Ir a comuna</h2>
      <select id="comuna" aria-label="Ir a comuna"><option value="">Toda la región</option></select>
      <label class="fila"><input type="checkbox" id="ver-riesgo" checked> Mostrar áreas de riesgo (achurado)</label>
    </section>
    <section>
      <h2>Situación normativa</h2>
      <ul class="leyenda" id="leyenda"></ul>
    </section>
    <p class="pie" id="pie"></p>
  </aside>
</div>
<script src="__URL_MAPLIBRE__"></script>
<script src="__URL_PMTILES__"></script>
<script id="datos" type="application/json">__DATOS__</script>
<script id="comunas" type="application/json">__COMUNAS__</script>
<script id="pmtiles-b64" type="application/octet-stream">__PMTILES_B64__</script>
<script>
(function () {
  const D = JSON.parse(document.getElementById("datos").textContent);
  const COMUNAS = JSON.parse(document.getElementById("comunas").textContent);
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const estado = $("estado");

  // Aviso, leyenda y pie
  $("aviso").innerHTML = "<strong>Información referencial</strong>" + esc(D.aviso) +
    " Clasificación en borrador: la valida el arquitecto de Regu.";
  $("leyenda").innerHTML = Object.entries(D.clases).map(([k, c]) =>
    `<li><span class="chip" style="background:${c.color}">${k}</span><span>${esc(c.titulo)}</span></li>`).join("") +
    `<li><span class="chip riesgo" aria-hidden="true"></span><span>Área de riesgo (superpuesta a la clase)</span></li>`;
  $("pie").textContent = `Fuentes: IDE MINVU (geoide.minvu.cl), División comunal BCN. Generado el ${D.fecha} con regu-ipt-etl.`;
  const sel = $("comuna");
  COMUNAS.features.slice().sort((a, b) => a.properties.nombre.localeCompare(b.properties.nombre, "es"))
    .forEach((f) => { const o = document.createElement("option"); o.value = f.properties.cut; o.textContent = f.properties.nombre; sel.appendChild(o); });

  function pintarFicha(p, lngLat, esEjemplo) {
    const c = D.clases[p.clase] || { titulo: p.norma_titulo || p.clase, resumen: "", normas: [], color: "#999" };
    const instrumento = p.ipt_tipo ? `${esc(p.ipt_tipo)} ${esc(p.ipt_nombre || "")}` : "Ninguno (sin instrumento de planificación)";
    $("ficha").innerHTML = `
      <h2>${esEjemplo ? "Ejemplo · " + esc(D.ejemplo.texto) : "Punto consultado"}</h2>
      <div class="clase"><span class="chip" style="background:${c.color}">${esc(p.clase)}</span>
        <span class="titulo">${esc(p.norma_titulo || c.titulo)}</span></div>
      ${Number(p.riesgo) ? '<span class="riesgo">Dentro de un área de riesgo</span>' : ""}
      <dl>
        <dt>Instrumento</dt><dd>${instrumento}</dd>
        <dt>Zona</dt><dd class="mono">${esc(p.zona || "—")}</dd>
        <dt>Comuna</dt><dd>${esc(p.comuna || "—")} <span class="mono">${esc(p.cut || "")}</span></dd>
        <dt>Coordenadas</dt><dd class="mono">${lngLat.lat.toFixed(5)}, ${lngLat.lng.toFixed(5)}</dd>
      </dl>
      <p class="resumen">${esc(c.resumen)}</p>
      ${c.normas.length ? `<ul class="normas">${c.normas.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
      <p class="nota">${esEjemplo ? "Haz clic en cualquier punto del mapa para consultar su situación normativa." : "Haz clic en otro punto para consultarlo."}</p>`;
  }
  $("ficha").innerHTML = '<p class="nota">Cargando la ficha de ejemplo…</p>';

  if (!window.maplibregl || !window.pmtiles) {
    estado.textContent = "No se pudo cargar la biblioteca del mapa. Revisa la conexión a internet y recarga la página.";
    return;
  }

  // PMTiles embebido: se decodifica una vez y se sirve desde memoria (sin HTTP Range)
  const b64 = document.getElementById("pmtiles-b64").textContent.trim();
  const bin = atob(b64); const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  const fuente = { getKey: () => "capa", getBytes: async (off, len) => ({ data: buf.buffer.slice(off, off + len) }) };
  const protocolo = new pmtiles.Protocol();
  protocolo.add(new pmtiles.PMTiles(fuente));
  maplibregl.addProtocol("pmtiles", protocolo.tilev4);   // pmtiles 3.x: tilev4 = API de MapLibre 4 (tile es v3)

  const css = getComputedStyle(document.documentElement);
  const tok = (n) => css.getPropertyValue(n).trim();
  const colorClase = ["match", ["get", "clase"]];
  Object.entries(D.clases).forEach(([k, c]) => colorClase.push(k, c.color));
  colorClase.push("#999999");
  const bboxRegion = COMUNAS.features.reduce((b, f) => [Math.min(b[0], f.properties.bbox[0]), Math.min(b[1], f.properties.bbox[1]),
    Math.max(b[2], f.properties.bbox[2]), Math.max(b[3], f.properties.bbox[3])], [180, 90, -180, -90]);

  const map = new maplibregl.Map({
    container: "mapa", bounds: [[bboxRegion[0], bboxRegion[1]], [bboxRegion[2], bboxRegion[3]]],
    fitBoundsOptions: { padding: 20 }, maxZoom: 18, attributionControl: { compact: true },
    style: { version: 8, sources: {
        ipt: { type: "vector", url: "pmtiles://capa", attribution: "IDE MINVU · BCN" },
        comunas: { type: "geojson", data: COMUNAS },
        sel: { type: "geojson", data: { type: "FeatureCollection", features: [] } } },
      layers: [
        { id: "fondo", type: "background", paint: { "background-color": tok("--map-bg") } },
        { id: "ipt", type: "fill", source: "ipt", "source-layer": "ipt", paint: { "fill-color": colorClase, "fill-opacity": 0.85 } },
        { id: "ipt-borde", type: "line", source: "ipt", "source-layer": "ipt", minzoom: 11,
          paint: { "line-color": "#ffffff", "line-opacity": 0.55, "line-width": 0.6 } },
        { id: "comunas", type: "line", source: "comunas", paint: { "line-color": tok("--comuna"), "line-width": 1.2, "line-opacity": 0.7 } },
        { id: "sel", type: "line", source: "sel", paint: { "line-color": tok("--accent"), "line-width": 3 } } ] }
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");

  // Achurado de riesgo (patrón dibujado en canvas: no requiere recursos externos)
  function patron() {
    const s = 12, cv = document.createElement("canvas"); cv.width = cv.height = s;
    const x = cv.getContext("2d"); x.strokeStyle = tok("--riesgo"); x.lineWidth = 2;
    x.beginPath(); x.moveTo(0, s); x.lineTo(s, 0); x.moveTo(-s / 2, s / 2); x.lineTo(s / 2, -s / 2); x.moveTo(s / 2, s * 1.5); x.lineTo(s * 1.5, s / 2); x.stroke();
    return x.getImageData(0, 0, s, s);
  }
  const marca = new maplibregl.Marker({ color: tok("--accent") });

  function consultar(lngLat, esEjemplo) {
    const pt = map.project(lngLat);
    const f = map.queryRenderedFeatures(pt, { layers: ["ipt"] })[0];
    marca.setLngLat(lngLat).addTo(map);
    if (!f) {
      $("ficha").innerHTML = `<h2>Punto consultado</h2>
        <div class="clase"><span class="chip" style="background:var(--line)">—</span><span class="titulo">Fuera de cobertura DPA</span></div>
        <p class="resumen">El punto no cae en ninguna comuna de esta región (División Político-Administrativa BCN) ni en la
        extensión costera de un instrumento comunal. Puede ser mar, otra región o un borde costero mal representado.</p>
        <dl><dt>Coordenadas</dt><dd class="mono">${lngLat.lat.toFixed(5)}, ${lngLat.lng.toFixed(5)}</dd></dl>`;
      map.getSource("sel").setData({ type: "FeatureCollection", features: [] });
      return false;
    }
    pintarFicha(f.properties, lngLat, esEjemplo);
    map.getSource("sel").setData({ type: "Feature", geometry: f.geometry, properties: {} });
    return true;
  }

  map.on("load", () => {
    map.addImage("rayado", patron());
    map.addLayer({ id: "riesgo", type: "fill", source: "ipt", "source-layer": "ipt", filter: ["==", ["get", "riesgo"], 1],
      paint: { "fill-pattern": "rayado", "fill-opacity": 0.9 } }, "comunas");
    estado.hidden = true;
    map.once("idle", () => consultar(new maplibregl.LngLat(D.ejemplo.lon, D.ejemplo.lat), true));
  });
  map.on("error", (e) => { estado.hidden = false; estado.textContent = "Error del mapa: " + (e.error && e.error.message || e); });
  map.on("click", (e) => consultar(e.lngLat, false));
  map.on("mousemove", "ipt", () => { map.getCanvas().style.cursor = "crosshair"; });
  $("ver-riesgo").addEventListener("change", (e) => map.setLayoutProperty("riesgo", "visibility", e.target.checked ? "visible" : "none"));
  sel.addEventListener("change", () => {
    const f = COMUNAS.features.find((x) => x.properties.cut === sel.value);
    const b = f ? f.properties.bbox : bboxRegion;
    map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 24, duration: matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 800 });
  });
})();
</script>
"""
