# Lámina normativa comunal

Primer entregable: [Temuco · A3 horizontal](salidas/temuco.pdf). Generación sin red, desde la muestra versionada. Todo el mapa, los achurados y los textos del PDF son vectoriales.

## Uso

Desde la raíz del repositorio, con Python 3.12:

```bash
python -m pip install -r sig/requirements.txt
python -m sig.render --layout lamina_comuna --cut 09101 --formato A3-h --salida sig/salidas/temuco.pdf
python -m sig.render --todas-las-comunas --region ARAUCANIA --salida /tmp/laminas
python -m pytest sig/tests -q
python tests/test_sintetico.py
```

`--gpkg` y `--pmtiles` permiten seleccionar otras entradas con el mismo contrato. El lote recorre las comunas disponibles: la muestra contiene siete, no las 32 regionales. No se crean comunas ni geometrías faltantes. Cada PDF tiene un JSON de auditoría con escala, barra, porcentajes y huella de los datos. La fecha de emisión se controla desde la plantilla para que el resultado sea reproducible.

## Decisiones cartográficas

- ReportLab dibuja geometrías directamente en PDF, en vez de capturar un lienzo WebGL rasterizado. El mismo PDF se puede visualizar en pantalla e imprimir. No se añade un visor web en este entregable.
- A3 horizontal: 420 × 297 mm. Imprimir a tamaño real / 100 %, sin ajustar a página. Escala de cuadrícula UTM 18S (EPSG:32718); las distancias de terreno presentan la pequeña distorsión propia de esta proyección. La barra de 5 km y el denominador se calculan con la misma transformación métrica que el mapa. Norte geográfico calculado en el centro comunal.
- Colores importados de `etl/mapa.py`, títulos literales de `legal_refs.json`. Tramas redundantes por clase permiten lectura sin depender del color. Riesgo: diagonales más densas, según `riesgo` del producto; no se confunden todas las afectaciones con riesgo.
- Estadísticas sobre `area_m2` del ETL (proyección equivalente ESRI:102033), incluyendo su tratamiento costero. El dibujo no recalcula la clasificación legal. Se reparan geometrías inválidas en memoria y se simplifican 2 m para representación; el GPKG no se modifica.
- Minimapa: cobertura de todas las clases del PMTiles regional a zoom 6, generalizada 100 m. Se identifica expresamente como cobertura generalizada, no como una nueva delimitación administrativa. Comuna destacada a partir de BCN.
- Decreto, fecha y última modificación se reproducen tal como aparecen en la muestra del cruce Portal IPT. La tabla no certifica vigencia actual; los metadatos ausentes se indican, incluso cuando la entrada no contiene las columnas opcionales de vigencia o fecha de extracción. R2 no se presenta como instrumento.
- Solo se rotulan comunas vecinas cuando el texto cabe en el marco sin colisión. No se inventan nombres de localidades ni normas de zona.

## Plantilla y comprobación

`layouts/lamina_comuna.json` configura título, proyecto, fecha, fuentes, aviso, tamaños, tramas y posiciones en mm del mapa, panel, minimapa, escala, norte y rótulo. Se valida con `layouts/_esquema.json`; texto menor de 7 pt y aviso alterado se rechazan. Este primer formato es A3 horizontal; no se anuncian otros tamaños aún. Un panel de instrumentos que exceda el espacio produce error, no texto truncado.

Lectura de plantillas y textos legales, y escritura de auditoría, con UTF-8 explícito para conservar tildes también en Windows.

Pruebas: entrada sin metadatos opcionales, tamaño físico A3, ausencia de imágenes rasterizadas, peso menor de 10 MB, tamaño tipográfico mínimo, aviso extraído del PDF, cinco segmentos de escala medidos en el PDF, plantilla inválida y snapshot con tolerancia. La referencia `tests/lamina_comuna.png` fue revisada visualmente; no se actualiza automáticamente en los tests. Revisión adicional de la suite sintética del ETL sin modificarla.

Lote de las siete comunas: 19,69 s en la última verificación del entorno cloud (2026-09-30). El requisito de 32 comunas en menos de cinco minutos queda pendiente de validación con una entrada completa de 32 comunas; no se extrapola como resultado medido.

Las dependencias están aisladas en `sig/requirements.txt`. Las salidas de ejemplo se guardan en `sig/salidas/`; los lotes de trabajo deben ir fuera del repositorio o a `data/`, que ya está ignorado.

## Espacio de trabajo local

```bash
python -m sig.servidor
```

Abrir `http://127.0.0.1:8000` en el mismo equipo. Permite elegir comuna, activar capas, consultar un punto y descargar su lámina A3. No requiere Internet al ejecutarse: Leaflet está incluido con su licencia en `web/vendor/`. El mapa usa las geometrías de la muestra, sin mapa base externo.

La exportación conserva la plantilla comunal: no refleja el zoom ni las capas desactivadas en el visor. El proyecto aún no permite importar capas propias, guardar proyectos, seleccionar por atributos ni editar simbología. La [arquitectura y próximos recorridos](ARQUITECTURA.md) separa lo implementado de esas etapas. Reiniciar el servidor si cambian los datos o la plantilla; la caché de lectura y de PDFs dura únicamente la sesión. El servicio escucha solo en el equipo local y no registra el contenido de consultas.

## Verdana

La familia elegida es **Verdana original**, regular y negrita, para toda la lámina. Las medidas y saltos de línea se calculan con esa fuente y el PDF incrusta los subconjuntos utilizados. No se sustituye silenciosamente por Helvetica cuando falta.

El render busca `verdana.ttf` y `verdanab.ttf` (sin distinguir mayúsculas en los nombres de archivo) en la carpeta indicada por `SIG_FUENTES`, o en las ubicaciones habituales de Windows/macOS y `~/.local/share/fonts/verdana` en Linux. En Windows suele bastar la instalación del sistema. Para una carpeta propia, en PowerShell:

```powershell
$env:SIG_FUENTES = 'C:\MisFuentes\Verdana'
python -m sig.render --cut 09101 --salida sig/salidas/temuco.pdf
```

Los archivos originales deben estar instalados con su licencia correspondiente; no se incluyen fuentes ni instaladores en Git. Esta verificación usó Verdana de Microsoft Core Fonts, obtenida del paquete `verdan32.exe` del proyecto Corefonts en SourceForge. Diferentes versiones de Verdana pueden cambiar ligeramente el snapshot; revisar visualmente antes de aceptar una nueva referencia.

La interfaz usa Verdana instalada en el equipo del navegador. Si no existe, el navegador puede usar su sans serif de respaldo; para evaluar la tipografía elegida, instalar Verdana también en ese equipo. No se sirve ni transforma la fuente como webfont.
