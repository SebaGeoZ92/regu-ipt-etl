// Prueba de interfaz (opcional, manual) del selector de mapas temáticos de Regu Suelo local (M4 de docs/MAPAS_TEMATICOS.md).
// Mismos requisitos que prueba_ui.js: servidor en marcha (python run.py app --no-abrir), Node, Chrome y puppeteer-core
// (NODE_PATH puede apuntar a donde se instaló) y los temas activos generados (python run.py temas generar --tema <id>).
// Uso: node tests/ui/prueba_temas.js <carpeta de capturas>. Variables: CHROME y URL_APP.
const puppeteer = require("puppeteer-core");
const SP = process.argv[2];
const URL = process.env.URL_APP || "http://127.0.0.1:8000/";
const dormir = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const b = await puppeteer.launch({
    executablePath: process.env.CHROME || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", headless: "new",
    args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist", "--window-size=1500,900", "--no-first-run"],
    defaultViewport: {width: 1500, height: 900}
  });
  const p = await b.newPage();
  const consola = [], red = [];
  p.on("console", (m) => { if (["error", "warning"].includes(m.type())) consola.push(m.type() + ": " + m.text().slice(0, 200)); });
  p.on("pageerror", (e) => consola.push("PAGEERROR: " + String(e).slice(0, 300)));
  p.on("response", (r) => { if (r.status() >= 400) red.push(r.status() + " " + r.url().replace(URL, "/").slice(0, 100)); });
  const foto = (n) => p.screenshot({path: `${SP}\\${n}.png`});
  const esperarMapa = async (t = 60000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < t) {
      if (await p.evaluate(() => mapa.loaded() && mapa.areTilesLoaded())) { await dormir(500); if (await p.evaluate(() => mapa.areTilesLoaded())) return true; }
      await dormir(400);
    }
    return false;
  };
  const out = {};

  await p.goto(URL + "#4.3/-35.5/-71", {waitUntil: "domcontentloaded"});
  await p.evaluate(() => { try { localStorage.clear(); } catch (e) {} });
  await p.reload({waitUntil: "domcontentloaded"});
  await esperarMapa();
  await p.waitForFunction(() => document.querySelectorAll("#tema option").length > 1, {timeout: 20000}).catch(() => {});
  out.opciones = await p.evaluate(() => [...document.querySelectorAll("#tema optgroup")].map((g) => g.label + ": " + [...g.querySelectorAll("option")].map((o) => o.textContent + (o.disabled ? " [deshabilitada]" : "")).join(", ")));
  out.sinTema = await p.evaluate(() => ({capa: !!mapa.getLayer("tema"), sliderOculto: document.getElementById("op-tema").hidden, leyenda: document.getElementById("ley-tema").innerHTML.length}));

  // 1) ráster continuo (temperatura): capa, leyenda con degradado, atribución y transparencia
  await p.select("#tema", "clima_temperatura_media_anual");
  await esperarMapa(); await dormir(800);
  out.temperatura = await p.evaluate(() => ({tipo: mapa.getLayer("tema").type, opacidad: mapa.getPaintProperty("tema", "raster-opacity"), remuestreo: mapa.getPaintProperty("tema", "raster-resampling"),
    degradado: !!document.querySelector("#ley-tema .barra"), etiquetas: document.querySelector("#ley-tema .barra-etq").innerText.replace(/\s+/g, " "),
    licencia: document.getElementById("ley-tema").innerText.includes("por verificar"), atribucion: document.getElementById("atrib").innerText.slice(0, 80),
    sliderVisible: !document.getElementById("op-tema").hidden, capaDebajoDeNormativa: mapa.getStyle().layers.map((l) => l.id).indexOf("tema") < mapa.getStyle().layers.map((l) => l.id).indexOf("normativa")}));
  await foto("tema_temperatura");
  await p.evaluate(() => { const s = document.querySelector('input[data-opacidad="tema"]'); s.value = 40; s.dispatchEvent(new Event("input", {bubbles: true})); });
  out.opacidad = await p.evaluate(() => ({capa: mapa.getPaintProperty("tema", "raster-opacity"), etiqueta: document.querySelector('input[data-opacidad="tema"]').nextElementSibling.textContent, guardado: localStorage.getItem("regu.opacidad")}));

  // 2) con un mapa base: la atribución combina los dos
  await p.click('#fondos input[value="osm"]'); await dormir(1500);
  out.atribucionCombinada = await p.evaluate(() => document.getElementById("atrib").innerText);
  await p.click('#fondos input[value="ninguno"]');

  // 3) ráster con otra rampa (precipitación) y vector (ubicación, con leyenda de categorías)
  await p.select("#tema", "clima_precipitacion_anual"); await esperarMapa();
  out.precipitacion = await p.evaluate(() => ({unidad: document.querySelector("#ley-tema .barra-etq").innerText.includes("mm/año"), opacidadConservada: mapa.getPaintProperty("tema", "raster-opacity")}));
  await foto("tema_precipitacion");
  await p.select("#tema", "ubicacion_division"); await esperarMapa(); await dormir(1000);
  out.ubicacion = await p.evaluate(() => ({tipo: mapa.getLayer("tema").type, leyenda: document.querySelectorAll("#ley-tema i").length, opacidad: mapa.getPaintProperty("tema", "fill-opacity"),
    dibujados: mapa.queryRenderedFeatures({layers: ["tema"]}).length, ejemplo: (mapa.queryRenderedFeatures({layers: ["tema"]})[0] || {properties: {}}).properties.region}));
  await foto("tema_ubicacion");

  // 4) recargar: recuerda el tema y su transparencia; «Ninguno» lo quita
  await p.reload({waitUntil: "domcontentloaded"}); await esperarMapa();
  await p.waitForFunction(() => !!mapa.getLayer("tema"), {timeout: 20000}).catch(() => {});
  out.tras_recarga = await p.evaluate(() => ({tema: document.getElementById("tema").value, capa: !!mapa.getLayer("tema"), opacidad: mapa.getPaintProperty("tema", "fill-opacity"),
    slider: document.querySelector('input[data-opacidad="tema"]').value}));
  await p.select("#tema", "");
  out.ninguno = await p.evaluate(() => ({capa: !!mapa.getLayer("tema"), fuente: !!mapa.getSource("tema"), slider: document.getElementById("op-tema").hidden, atrib: document.getElementById("atrib").innerText}));

  out.errores_http = red;
  out.consola = [...new Set(consola)].slice(0, 10);
  console.log(JSON.stringify(out, null, 2));
  await b.close();
})().catch((e) => { console.error("FALLO", e); process.exit(1); });
