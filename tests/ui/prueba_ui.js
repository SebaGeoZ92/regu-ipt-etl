// Prueba de interfaz de Regu Suelo local (opcional, manual): abre la página real en Chrome sin ventana y revisa carga,
// ficha, edificios 3D, transparencias, mapas base (con atribución), recuerdo tras recargar, ocupación, buscador y lámina PDF.
// Requiere el servidor en marcha (python run.py app --no-abrir), Node, Chrome y puppeteer-core (npm i puppeteer-core en una
// carpeta temporal; no es dependencia del repo). Uso: node tests/ui/prueba_ui.js <carpeta de capturas>
// Variables: CHROME (ruta de chrome.exe) y URL_APP (por defecto http://127.0.0.1:8000/). Borra el localStorage al empezar.
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
  p.on("console", (m) => { if (["error", "warning"].includes(m.type())) consola.push(m.type() + ": " + m.text().slice(0, 220)); });
  p.on("pageerror", (e) => consola.push("PAGEERROR: " + String(e).slice(0, 300)));
  p.on("response", (r) => { const u = r.url(); if (!u.startsWith("data:")) red.push({s: r.status(), u: u.replace(URL, "/").slice(0, 120)}); });
  p.on("requestfailed", (r) => red.push({s: "FALLA", u: r.url().slice(0, 120), e: r.failure() && r.failure().errorText}));

  const esperarMapa = async (t = 90000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < t) {
      const ok = await p.evaluate(() => typeof mapa !== "undefined" && mapa.loaded() && mapa.areTilesLoaded());
      if (ok) { await dormir(600); if (await p.evaluate(() => mapa.areTilesLoaded())) return true; }
      await dormir(500);
    }
    return false;
  };
  const foto = (n) => p.screenshot({path: `${SP}\\${n}.png`});
  const out = {};

  await p.goto(URL + "#14/-38.7359/-72.5904", {waitUntil: "domcontentloaded"});
  await p.evaluate(() => { try { localStorage.clear(); } catch (e) {} });
  await p.reload({waitUntil: "domcontentloaded"});
  out.cargaInicial = await esperarMapa();
  await foto("ui1_inicial");

  // clic en el centro del mapa -> ficha
  const caja = await (await p.$("#mapa")).boundingBox();
  const t0 = Date.now();
  await p.mouse.click(caja.x + caja.width / 2, caja.y + caja.height / 2);
  await p.waitForFunction(() => /Resoluci/.test(document.getElementById("panel").innerText), {timeout: 20000}).catch(() => {});
  out.fichaMs = Date.now() - t0;
  out.panel = (await p.evaluate(() => document.getElementById("panel").innerText)).slice(0, 700);
  await foto("ui2_ficha");

  // edificios a zoom 16
  await p.evaluate(() => mapa.jumpTo({center: [-72.5904, -38.7359], zoom: 16.5, pitch: 55, bearing: -20}));
  await esperarMapa(); await dormir(2500);
  out.edificios = await p.evaluate(() => mapa.getSource("edificios")._data.features ? mapa.getSource("edificios")._data.features.length : "n/d");
  out.estadoTxt = await p.evaluate(() => document.getElementById("estado").innerText);
  await foto("ui3_edificios3d");

  // transparencia: mover sliders y comprobar que se aplica y se recuerda
  await p.evaluate(() => mapa.jumpTo({center: [-72.5904, -38.7359], zoom: 13, pitch: 0, bearing: 0}));
  await p.evaluate(() => {
    const s = document.querySelector('input[data-opacidad="normativa"]'); s.value = 30; s.dispatchEvent(new Event("input", {bubbles: true}));
    const e = document.querySelector('input[data-opacidad="edificios"]'); e.value = 55; e.dispatchEvent(new Event("input", {bubbles: true}));
  });
  out.opacidadAplicada = await p.evaluate(() => ({normativa: mapa.getPaintProperty("normativa", "fill-opacity"), riesgo: mapa.getPaintProperty("riesgo", "fill-opacity"),
    edificios: mapa.getPaintProperty("edificios", "fill-extrusion-opacity"), guardado: localStorage.getItem("regu.opacidad"), etiqueta: document.querySelector('input[data-opacidad="normativa"]').nextElementSibling.textContent}));

  // mapas base: OSM, Esri, EOX y volver a ninguno; atribución visible y peticiones externas
  const base = {};
  for (const id of ["osm", "esri", "eox", "ninguno"]) {
    red.length = 0;
    await p.click(`#fondos input[value="${id}"]`);
    await esperarMapa(40000); await dormir(1200);
    const externas = red.filter((r) => !/^\/(?!basemap)|^\/?$/.test(r.u) && !r.u.startsWith("/") ).map((r) => r.s);
    base[id] = {
      visible: await p.evaluate((k) => mapa.getLayoutProperty("base_" + k, "visibility"), id).catch(() => "n/a"),
      atribucion: (await p.evaluate(() => { const a = document.getElementById("atrib"); return a.classList.contains("on") ? a.innerText : ""; })).slice(0, 160),
      peticionesBase: red.filter((r) => /basemap|arcgisonline|eox\.at/.test(r.u)).length,
      estados: [...new Set(red.filter((r) => /basemap|arcgisonline|eox\.at/.test(r.u)).map((r) => r.s))],
      externas: externas.length
    };
    if (id !== "ninguno") await foto("ui_base_" + id);
  }
  out.base = base;

  // recarga: ¿recuerda fondo y transparencias?
  await p.evaluate(() => mapa.getContainer()); // sin efecto
  await p.click('#fondos input[value="osm"]');
  await dormir(500);
  await p.reload({waitUntil: "domcontentloaded"}); await esperarMapa();
  out.tras_recarga = await p.evaluate(() => ({fondo: document.querySelector('#fondos input:checked').value, normativa: document.querySelector('input[data-opacidad="normativa"]').value,
    edificios: document.querySelector('input[data-opacidad="edificios"]').value, fillOpacity: mapa.getPaintProperty("normativa", "fill-opacity"), atrib: document.getElementById("atrib").innerText.slice(0, 40)}));
  await foto("ui_recarga");

  // ocupación: encender la capa, comprobar el slider propio y fotografiar
  await p.click('#fondos input[value="ninguno"]');
  await p.evaluate(() => mapa.jumpTo({center: [-72.5904, -38.7359], zoom: 12.5, pitch: 0, bearing: 0}));
  await p.click('input[data-capa="ocupacion"]'); await p.click('input[data-capa="normativa"]');
  await esperarMapa(); await dormir(800);
  out.ocupacion = await p.evaluate(() => ({vis: mapa.getLayoutProperty("ocupacion", "visibility"), op: mapa.getPaintProperty("ocupacion", "fill-opacity"),
    leyenda: document.getElementById("ley-ocupacion").classList.contains("on"), feats: mapa.queryRenderedFeatures({layers: ["ocupacion"]}).length}));
  await foto("ui_ocupacion");
  // buscador de comuna
  await p.click("#q"); await p.keyboard.type("padre las");
  await dormir(600);
  out.sugerencias = await p.evaluate(() => document.getElementById("sugerencias").innerText);
  await p.keyboard.press("Enter"); await dormir(2500);
  out.tras_buscar = await p.evaluate(() => ({centro: mapa.getCenter(), zoom: +mapa.getZoom().toFixed(1)}));
  // lámina PDF de la comuna al centro (Temuco): POST /api/lamina
  await p.evaluate(() => mapa.jumpTo({center: [-72.5904, -38.7359], zoom: 11}));
  red.length = 0;
  await p.click("#blamina");
  await p.waitForFunction(() => /descargada|No se pudo/.test(document.getElementById("estado").innerText), {timeout: 120000}).catch(() => {});
  out.lamina = {estado: await p.evaluate(() => document.getElementById("estado").innerText), http: red.filter((r) => /api\/lamina/.test(r.u)).map((r) => r.s)};
  // respuestas 4xx/5xx de toda la sesión
  out.errores_http = red.filter((r) => r.s === "FALLA" || r.s >= 400);
  out.consola = [...new Set(consola)].slice(0, 15);
  console.log(JSON.stringify(out, null, 2));
  await b.close();
})().catch((e) => { console.error("FALLO", e); process.exit(1); });
