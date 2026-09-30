# SIG propio · layouts cartográficos · especificación para Codex

Rama: `codex/sig-layouts`. Carpeta: `sig/`. **No se toca nada fuera de `sig/`**, salvo lo que se pida explícitamente. El ETL (`etl/`, `run.py`) lo mantiene otro agente en `master`.

## Visión

Un SIG web propio para el Atlas Normativo y Regu Suelo, que empieza por lo que QGIS hace con fricción: **salidas cartográficas reproducibles y automáticas**. Los layouts se escriben como código (plantillas versionables), se renderizan igual en pantalla y en PDF, y se generan en lote (una lámina por comuna o por predio) sin abrir ninguna aplicación de escritorio.

No se trata de clonar QGIS. Se trata de ganarle en un nicho: la **lámina normativa**, publicable, correcta y generada en segundos.

## Datos (contrato de entrada)

- Muestra en el repo: `samples/temuco/` con un GPKG pequeño (capas `capa_ipt`, `afectaciones` y `comunas`) y el PMTiles de La Araucanía, si pesa menos de 50 MB.
- Campos de `capa_ipt`: `id, cut, comuna, region, clase (U1|U2|U3|E|R1|R2), fuente, ipt_tipo, ipt_nombre, zona, zona_desc, revisar, riesgo, area_m2, norma_titulo, norma_resumen, norma_refs, aviso`, más la vigencia (`ipt_norma, ipt_fecha, ipt_ultmod, ord_url`) cuando exista.
- Los colores por clase y los textos legales salen de `legal_refs.json` y del mapa actual (`etl/mapa.py`). **No se inventan textos legales.**
- El `aviso` legal debe aparecer en toda lámina: "Información referencial. No reemplaza el CIP que emite la DOM (OGUC art. 1.4.4)."

## Stack sugerido

- **Mapa:** MapLibre GL JS + PMTiles (el mismo del mapa actual).
- **Layout:** HTML/CSS con unidades físicas (mm) y `@page`, para A4 y A3 en vertical y horizontal.
- **PDF:** Playwright (Chromium headless) → PDF vectorial. Alternativa: Paged.js para paginación.
- **Plantillas:** un JSON/YAML por layout, validado con JSON Schema (`sig/layouts/_esquema.json`).
- **CLI:** `python -m sig.render --layout lamina_comuna --cut 09101 --formato A3-h --salida out/temuco.pdf`, y en lote con `--todas-las-comunas --region ARAUCANIA`.

## Primer entregable: "Lámina normativa comunal"

Elementos obligatorios, todos configurables desde la plantilla:

1. **Mapa principal** con la partición por clase, las áreas de riesgo achuradas y los límites comunales.
2. **Título y subtítulo:** "Situación normativa del suelo · Comuna de X".
3. **Leyenda** generada desde los datos: solo las clases presentes, con su título.
4. **Escala gráfica y numérica,** correctas a la escala real de impresión.
5. **Norte.**
6. **Minimapa de ubicación** (región, con la comuna destacada).
7. **Cuadro de rótulo:** proyecto (Atlas Normativo · Regu), fecha, sistema de referencia, fuentes (IDE MINVU, Portal IPT, BCN) y versión de los datos.
8. **Tabla resumen:** % de superficie por clase e instrumentos vigentes con su decreto y fecha.
9. **Aviso legal** visible.

Segundo entregable: la **"Ficha predial"** en A4 vertical, con el mapa del predio, la tabla de la partición en %, las afectaciones y el aviso. Por ahora usa un polígono de ejemplo; después se conecta con `ficha()`.

## Calidad cartográfica (criterios de aceptación)

- La escala impresa se verifica midiendo en el PDF: 1 cm debe coincidir con lo que dice la escala.
- La tipografía queda legible impresa (mínimo 7 pt) y la jerarquía visual es clara.
- La paleta distingue las clases también en escala de grises y para daltonismo. El achurado de riesgo sirve sin color.
- Las etiquetas no se superponen. Las comunas llevan etiqueta solo si caben.
- El PDF es **vectorial**, sin rasterizar el mapa completo, y pesa menos de 10 MB por lámina A3.
- Las 32 comunas de La Araucanía se generan en lote en menos de 5 minutos.

## Tests

- Test de snapshot por layout: se renderiza y se compara contra una imagen de referencia con tolerancia.
- Test de escala: se mide la barra de escala en el PDF.
- Test de plantilla: una plantilla inválida falla con un mensaje claro.
- El aviso legal existe en cada PDF (se extrae el texto del PDF y se busca).

## Reglas

- Trabaja solo en la rama `codex/sig-layouts` y en `sig/` y `samples/`.
- No hagas commit de datos pesados; `samples/` debe pesar menos de 50 MB en total.
- No subas claves ni datos personales. Los predios de ejemplo son sintéticos o sin propietario.
- Documenta las decisiones en `sig/README.md`. Un PR por entregable hacia `master`, que revisa Seba.
