/* Interfaz local: la normativa la calcula el motor Python existente. */
const $ = id => document.getElementById(id);
const mapa = L.map('mapa', {preferCanvas: false});
L.control.scale({imperial: false}).addTo(mapa);
const capas = {};
let catalogo, limite, revision = 0, consultaActual = 0, cutListo = null;
async function obtener(url, opciones) {
  const r = await fetch(url, opciones);
  if (!r.ok) {
    const error = await r.json().catch(() => ({}));
    throw new Error(typeof error.detail === 'string' ? error.detail : 'No fue posible completar la operación.');
  }
  return r.json();
}
function texto(etiqueta, contenido, clase) {
  const elemento = document.createElement(etiqueta);
  elemento.textContent = contenido;
  if (clase) elemento.className = clase;
  return elemento;
}
function visibilidad() {
  for (const [id, capa] of Object.entries(capas)) {
    if ($(id).checked) capa.addTo(mapa); else mapa.removeLayer(capa);
  }
}
async function cargarComuna() {
  const turno = ++revision;
  ++consultaActual;
  cutListo = null;
  const cut = $('comuna').value;
  $('estado').textContent = 'Cargando capas…';
  $('consulta').textContent = 'Selecciona un lugar del mapa.';
  try {
    const [particion, afectaciones, comunas] = await Promise.all(
      ['capa_ipt', 'afectaciones', 'comunas'].map(c => obtener(`/api/comunas/${cut}/capas/${c}`))
    );
    if (turno !== revision) return;
    Object.values(capas).forEach(c => mapa.removeLayer(c));
    delete capas.propuesta_tolten;
    const colores = Object.fromEntries(catalogo.clases.map(c => [c.codigo, c.color]));
    capas.capa_ipt = L.geoJSON(particion, {style: f => ({color: colores[f.properties.clase], weight: .3, fillOpacity: .8})});
    // El campo riesgo del ETL incluye la superposición ya resuelta por comuna.
    capas.afectaciones = L.geoJSON({...particion, features: particion.features.filter(f => f.properties.riesgo)}, {
      style: {color: '#333b35', weight: 1, dashArray: '3 3', fillColor: '#343b36', fillOpacity: .17}
    });
    capas.comunas = L.geoJSON(comunas, {style: {color: '#263e36', weight: 2, fill: false},
      onEachFeature: (f, capa) => capa.bindTooltip(texto('span', f.properties.comuna), {permanent: true, direction: 'center', className: 'nombre-comuna'})});
    limite = capas.comunas.getBounds();
    visibilidad(); mapa.fitBounds(limite, {padding: [30, 30]});
    const presentes = new Set(particion.features.map(f => f.properties.clase));
    $('leyenda').replaceChildren();
    for (const clase of catalogo.clases.filter(c => presentes.has(c.codigo))) {
      const fila = texto('div', '', 'leyenda');
      const muestra = texto('span', '', 'muestra'); muestra.style.backgroundColor = clase.color;
      fila.append(muestra, texto('span', `${clase.codigo} · ${clase.titulo}`)); $('leyenda').append(fila);
    }
    const mostrarPropuesta = cut === '09118' && catalogo.propuesta_tolten;
    $('control-propuesta').hidden = !mostrarPropuesta;
    $('aviso-propuesta').hidden = !mostrarPropuesta;
    if (mostrarPropuesta) {
      const propuesta = await obtener('/api/propuestas/tolten');
      if (turno !== revision) return;
      capas.propuesta_tolten = L.geoJSON(propuesta, {style: {color: '#8b2677', weight: 2, dashArray: '7 5', fillOpacity: .08},
        onEachFeature: (f, capa) => capa.bindTooltip(texto('span', `Propuesta: ${f.properties.zona_propuesta} · no acredita vigencia`))});
      $('aviso-propuesta').textContent = `${catalogo.propuesta_tolten.aviso} Versión: ${catalogo.propuesta_tolten.version_documental}. Fuente: ${catalogo.propuesta_tolten.fuente_documental}`;
      visibilidad();
    } else { delete capas.propuesta_tolten; }
    cutListo = cut;
    $('estado').textContent = `${$('comuna').selectedOptions[0].textContent} · ${particion.features.length} piezas · ${afectaciones.features.length} afectaciones en la muestra`;
  } catch (e) { if (turno === revision) $('estado').textContent = e.message; }
}
mapa.on('click', async e => {
  if (!cutListo) return;
  const turno = ++consultaActual;
  $('consulta').textContent = 'Consultando normativa…';
  try {
    const resultado = await obtener('/api/consulta', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({longitud: e.latlng.lng, latitud: e.latlng.lat})});
    if (turno !== consultaActual) return;
    $('consulta').replaceChildren(texto('strong', resultado.comuna || 'Fuera de cobertura'));
    if (resultado.motivo) $('consulta').append(texto('p', resultado.motivo));
    for (const parte of resultado.particion) {
      const fila = texto('div', '', 'dato');
      fila.append(texto('strong', `${parte.clase} · ${parte.ipt || 'Sin instrumento en la muestra'}`));
      fila.append(texto('p', parte.norma_titulo || ''));
      if (parte.zona) fila.append(texto('p', `Zona: ${parte.zona}`));
      if (parte.vigencia) fila.append(texto('p', [parte.vigencia.norma, parte.vigencia.fecha_vigencia].filter(Boolean).join(' · ')));
      $('consulta').append(fila);
    }
    $('consulta').append(texto('p', `Riesgo: ${resultado.riesgo_pct.toFixed(1)} %`));
    for (const afectacion of resultado.afectaciones) {
      $('consulta').append(texto('p', `Afectación: ${afectacion.capa || afectacion.tipo || 'Sin nombre'}${afectacion.zona ? ' · ' + afectacion.zona : ''}`));
    }
    $('consulta').append(texto('p', 'Textos legales en BORRADOR, pendientes de validación profesional.', 'tenue'));
  } catch (e) { if (turno === consultaActual) $('consulta').textContent = e.message; }
});
$('comuna').addEventListener('change', cargarComuna);
for (const id of ['capa_ipt', 'afectaciones', 'comunas', 'propuesta_tolten']) $(id).addEventListener('change', visibilidad);
$('encuadrar').onclick = () => { if (limite) mapa.fitBounds(limite, {padding: [30, 30]}); };
$('pdf').onclick = async e => {
  e.preventDefault();
  if (!cutListo) return;
  const cutExportado = cutListo;
  $('estado').textContent = 'Preparando lámina A3…';
  try {
    const r = await fetch(`/api/comunas/${cutExportado}/lamina.pdf`);
    if (!r.ok) { const error = await r.json(); throw new Error(error.detail || 'No se pudo generar la lámina'); }
    const url = URL.createObjectURL(await r.blob());
    const enlace = document.createElement('a'); enlace.href = url; enlace.download = `lamina-${cutExportado}.pdf`; enlace.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    $('estado').textContent = 'Lámina lista. Imprimir al 100 %.';
  } catch (e) { $('estado').textContent = e.message; }
};
(async () => {
  try {
    catalogo = await obtener('/api/catalogo');
    $('aviso').textContent = catalogo.aviso;
    $('cobertura').textContent = `${catalogo.comunas.length} comunas disponibles en la entrada`;
    const disponibles = new Set(catalogo.comunas.map(c => c.cut));
    $('piloto').textContent = catalogo.piloto.comunas.map(c => `${c.nombre}: ${disponibles.has(c.cut) ? 'disponible' : 'pendiente de datos'}`).join(' · ');
    for (const comuna of catalogo.comunas) { const o = texto('option', comuna.nombre); o.value = comuna.cut; $('comuna').append(o); }
    $('comuna').value = catalogo.comunas.some(c => c.cut === '09101') ? '09101' : catalogo.comunas[0].cut;
    $('comuna').disabled = false; await cargarComuna();
  } catch (e) { $('estado').textContent = e.message; }
})();
