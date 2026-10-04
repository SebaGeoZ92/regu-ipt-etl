// Prueba de interfaz (opcional, manual) del dibujo de predio y el volumen 3D de Regu Suelo local (A5).
// Mismos requisitos que prueba_ui.js: servidor en marcha (python run.py app --no-abrir), Node, Chrome y puppeteer-core
// (NODE_PATH puede apuntar a la carpeta temporal donde se instaló). Uso: node tests/ui/prueba_dibujo.js <carpeta de capturas> <lon> <lat>
// con un punto dentro de una zona con normas cargadas (p. ej. una casa de ZH2 de Temuco). Variables: CHROME y URL_APP.
const puppeteer = require("puppeteer-core");
const SP = process.argv[2], LON = parseFloat(process.argv[3]), LAT = parseFloat(process.argv[4]);
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
  p.on("response", (r) => { if (r.status() >= 400) red.push(r.status() + " " + r.url().replace(URL, "/").slice(0, 100)); });
  const foto = (n) => p.screenshot({path: `${SP}\\${n}.png`});
  const esperarMapa = async (t = 90000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < t) {
      if (await p.evaluate(() => mapa.loaded() && mapa.areTilesLoaded())) { await dormir(600); if (await p.evaluate(() => mapa.areTilesLoaded())) return true; }
      await dormir(400);
    }
    return false;
  };
  const px = async (lon, lat) => {      // coordenadas de pantalla de un punto del mapa
    const c = await (await p.$("#mapa")).boundingBox(), q = await p.evaluate((a, b) => { const r = mapa.project([a, b]); return [r.x, r.y]; }, lon, lat);
    return [c.x + q[0], c.y + q[1]];
  };
  const dibujar = async (puntos, cierre) => {
    for (const [lo, la] of puntos) { const [x, y] = await px(lo, la); await p.mouse.move(x, y); await p.mouse.click(x, y); await dormir(120); }
    if (cierre === "enter") await p.keyboard.press("Enter");
    else if (cierre === "doble") { const [x, y] = await px(...puntos[puntos.length - 1]); await p.mouse.click(x, y, {count: 2}); }
    else if (cierre === "boton") await p.click("#bdibujar");
  };
  const out = {};
  const dx = 0.00013, dy = 0.000135;        // ≈ 22 × 30 m
  const rect = (lo, la, kx = 1, ky = 1) => [[lo - dx * kx, la - dy * ky], [lo + dx * kx, la - dy * ky], [lo + dx * kx, la + dy * ky], [lo - dx * kx, la + dy * ky]];

  await p.goto(URL + `#18.3/${LAT}/${LON}`, {waitUntil: "domcontentloaded"});
  await p.evaluate(() => { try { localStorage.clear(); } catch (e) {} });
  await p.reload({waitUntil: "domcontentloaded"});
  out.carga = await esperarMapa();

  // 1) dibujar con Retroceso y cerrar con Enter
  await p.click("#bdibujar");
  out.modoDibujo = await p.evaluate(() => ({activo: document.body.classList.contains("dibujando"), boton: document.getElementById("bdibujar").textContent, bandera: window.__dibujando}));
  const r = rect(LON, LAT);
  await dibujar([r[0], r[1], [LON, LAT + 0.0005]], null);
  await p.keyboard.press("Backspace");                       // borra el vértice de prueba
  out.tras_backspace = await p.evaluate(() => dib.pts.length);
  await dibujar([r[2], r[3]], "enter");
  await p.waitForFunction(() => /Volumen del predio/.test(document.getElementById("panel").innerText), {timeout: 30000}).catch(() => {});
  await esperarMapa(); await dormir(2500);
  out.panel = (await p.evaluate(() => document.getElementById("panel").innerText)).slice(0, 1500);
  out.capas = await p.evaluate(() => ({existente: mapa.getSource("existente")._data.features.length, envolvente: mapa.getSource("envolvente")._data.features.length,
    alturaEnv: (mapa.getSource("envolvente")._data.features[0] || {properties: {}}).properties.altura, filtroEdificios: JSON.stringify(mapa.getFilter("edificios")).slice(0, 80),
    pitch: Math.round(mapa.getPitch()), marca: document.getElementById("marca").classList.contains("on"), limpiarVisible: !document.getElementById("blimpiar").hidden,
    vertices: dib.pts.length, modo: document.body.classList.contains("dibujando")}));
  await foto("dib1_volumen3d");

  // 2) cambiar de escenario (si hay más de uno)
  const radios = await p.$$('input[name="esc"]');
  out.escenarios = radios.length;
  if (radios.length > 1) {
    const antes = await p.evaluate(() => mapa.getSource("envolvente")._data.features[0].properties.altura + "/" + document.querySelector(".kpis").innerText.replace(/\s+/g, " ").slice(0, 120));
    await radios[1].click(); await dormir(1200);
    out.cambioEscenario = {antes, despues: await p.evaluate(() => (mapa.getSource("envolvente")._data.features[0] || {properties: {}}).properties.altura + "/" + (document.querySelector(".kpis") || {innerText: ""}).innerText.replace(/\s+/g, " ").slice(0, 120)),
      radioMarcado: await p.evaluate(() => document.querySelector('input[name="esc"]:checked').value)};
    await foto("dib2_escenario2");
  }

  // 3) limpiar
  await p.click("#blimpiar"); await dormir(600);
  out.tras_limpiar = await p.evaluate(() => ({existente: mapa.getSource("existente")._data.features.length, envolvente: mapa.getSource("envolvente")._data.features.length,
    predio: mapa.getSource("predio")._data.features ? mapa.getSource("predio")._data.features.length : 1, filtro: mapa.getFilter("edificios"),
    marca: document.getElementById("marca").classList.contains("on"), panel: document.getElementById("panel").innerText.slice(0, 60)}));

  // 4) cerrar con doble clic (no debe duplicar el último vértice) y con el botón «Terminar predio»
  await p.click("#bdibujar");
  await dibujar(rect(LON, LAT, 0.9, 0.9), "doble");
  await p.waitForFunction(() => /Volumen del predio|Predio dibujado/.test(document.getElementById("panel").innerText) && !/Calculando/.test(document.getElementById("panel").innerText), {timeout: 30000}).catch(() => {});
  out.doble_clic = await p.evaluate(() => ({vertices: mapa.getSource("predio")._data.geometry.coordinates[0].length - 1, panel: document.getElementById("panel").innerText.slice(0, 40)}));
  await p.click("#blimpiar");
  await p.click("#bdibujar");
  await dibujar(rect(LON, LAT, 0.8, 0.8), null);
  out.botonTerminar = await p.evaluate(() => document.getElementById("bdibujar").textContent);
  await p.click("#bdibujar");
  await p.waitForFunction(() => !/Calculando/.test(document.getElementById("panel").innerText), {timeout: 30000}).catch(() => {});
  out.cerrado_con_boton = await p.evaluate(() => document.getElementById("panel").innerText.slice(0, 40));
  await p.click("#blimpiar");

  // 5) Esc cancela
  await p.click("#bdibujar"); await dibujar(rect(LON, LAT).slice(0, 2), null); await p.keyboard.press("Escape");
  out.escape = await p.evaluate(() => ({activo: dib.activo, modo: document.body.classList.contains("dibujando"), dibujo: mapa.getSource("dibujo")._data.features.length, boton: document.getElementById("bdibujar").textContent}));

  // 6) fuera de cobertura y demasiado grande: mensaje claro, sin romper
  await p.evaluate(() => mapa.jumpTo({center: [-74.5, -38.0], zoom: 16}));
  await esperarMapa(30000);
  await p.click("#bdibujar"); await dibujar(rect(-74.5, -38.0), "enter");
  await p.waitForFunction(() => !/Calculando/.test(document.getElementById("panel").innerText), {timeout: 30000}).catch(() => {});
  out.fuera = await p.evaluate(() => document.getElementById("panel").innerText.replace(/\s+/g, " ").slice(0, 220));
  await p.click("#blimpiar");
  await p.evaluate((a, b) => mapa.jumpTo({center: [a, b], zoom: 13}), LON, LAT);
  await esperarMapa(30000);
  await p.click("#bdibujar"); await dibujar(rect(LON, LAT, 40, 40), "enter");
  await p.waitForFunction(() => !/Calculando/.test(document.getElementById("panel").innerText), {timeout: 30000}).catch(() => {});
  out.grande = await p.evaluate(() => document.getElementById("panel").innerText.replace(/\s+/g, " ").slice(0, 220));

  out.errores_http = red;
  out.consola = [...new Set(consola)].slice(0, 12);
  console.log(JSON.stringify(out, null, 2));
  await b.close();
})().catch((e) => { console.error("FALLO", e); process.exit(1); });
