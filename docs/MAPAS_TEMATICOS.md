# Mapas temáticos de referencia · especificación

Base del **SIG propio**: un generador de mapas de referencia (ubicación, suelo, clima, relieve, cobertura…) que se produce **en el PC de Seba**, se sirve desde la aplicación local (`docs/REGU_SUELO_LOCAL.md`) y más adelante se puede mover a un servidor. Cada mapa es un *tema* con su fuente, licencia, atribución y estilo declarados: nunca se muestra un mapa sin decir de dónde sale.

## Principios

- **Local primero.** Nada se publica. Antes de publicar, cada tema pasa por la revisión de licencia de su contrato.
- **Reproducible.** `python run.py temas generar <id>` rehace el tema desde la fuente; la fuente descargada vive en `<paths.raw>/temas/<id>/` y la salida en `<paths.out>/tiles/temas/<id>.pmtiles`.
- **Procedencia visible.** Fuente, versión del dato, fecha de descarga, licencia y atribución van en el contrato y en el mapa.
- **Referencial.** Son mapas de contexto a escala nacional: no reemplazan estudios de suelo, climáticos ni de riesgo. Cada leyenda dice la resolución del dato.
- **No se activa una fuente sin Paso 0:** acceso real, licencia y fecha verificados (misma regla que `docs/FUENTES_BAJO_DEMANDA.md`).

## Contrato de un tema (`temas/<id>.yaml`, validado con `temas/_esquema.json`)

```yaml
id: clima_temperatura_media_anual
nombre: Temperatura media anual
categoria: clima            # ubicacion | relieve | suelo | clima | cobertura | agua | riesgo | infraestructura | socioeconomico
estado: propuesta           # propuesta → mapeada (Paso 0 hecho y dato descargado) → activa (generado y servible)
tipo: raster                # raster | vector
fuente: {nombre, url, version, licencia, atribucion, fecha_dato, resolucion, acceso}
generacion: {generador: <nombre registrado en etl/temas.py>, parametros: {...}}
estilo: {rampa: [[valor, "#hex"], ...], unidad: "°C", leyenda: [{etiqueta, color}]}   # raster; para vector, capa_origen + paint MapLibre
zoom: [3, 9]
nota: texto libre (límites del dato)
```

## Arquitectura

- `etl/temas.py`: carga y valida el catálogo, informa el estado (¿existe la salida?, tamaño, fecha) y despacha el generador de cada tema.
- **Ráster** (`etl/raster_tiles.py`): GeoTIFF → recorte a Chile → rampa de colores → teselas XYZ PNG (EPSG:3857) → MBTiles → PMTiles. Sin GDAL de línea de comandos: `rasterio` + `Pillow`, y `pmtiles` para el formato.
- **Vectorial**: GeoDataFrame → PMTiles con el driver de GDAL, como `etl/teselas.py`.
- **Aplicación**: `GET /api/temas` entrega el catálogo (solo los campos públicos y si la salida existe); `/tiles/temas/{id}.pmtiles` las sirve con *Range*; el selector «Mapa temático» agrupa por categoría, con leyenda, atribución y transparencia.
- **Después**: valor al clic (muestreo del ráster en el punto) y una *ficha de lugar* con suelo, clima y relieve; láminas de `sig/` con estos temas.

## Temas iniciales (Paso 0 del 4-oct-2026)

| Categoría | Tema | Fuente | Licencia | Estado del Paso 0 |
|---|---|---|---|---|
| Ubicación | Regiones y comunas | BCN SIIT (ya en `data/base`) | BCN | ya tenemos el dato |
| Clima | Temperatura media anual y precipitación anual | WorldClim 2.1 (2,5 min, ≈ 4,5 km; zip de 658 MB) | **por verificar** (el sitio no se pudo leer automáticamente) | responde (HEAD 200) |
| Suelo | Arcilla, carbono orgánico y pH | SoilGrids 250 m (ISRIC), VRT remoto | CC BY 4.0 | responde |
| Relieve | Altitud, sombreado y pendiente | Copernicus DEM GLO-30 (teselas de 1°, ≈ 46 MB c/u) | licencia gratuita con atribución | responde; piloto en La Araucanía |
| Cobertura | Uso y cobertura del suelo | ESA WorldCover 10 m | CC BY 4.0, sin restricción de uso | por confirmar el nombre de las teselas |
| Suelo (oficial) | Capacidad de uso | CIREN | por revisar | **no responde** desde este PC (portal caído o bloqueado) |
| Geología (oficial) | Mapa geológico | SERNAGEOMIN | por revisar | **no responde** desde este PC (error de SSL) |

SoilGrids y WorldClim son productos **modelados y globales**: sirven para contexto, no para decidir sobre un predio. Los oficiales (CIREN, SERNAGEOMIN) los reemplazan cuando el acceso funcione.

## Reglas

- Cada tema declara resolución y límites del dato en su `nota`.
- Las licencias con atribución obligatoria se muestran en el mapa, no solo en el contrato.
- Un tema con licencia no comercial o sin verificar queda marcado y no se publica.
- Commit por paso; el backlog se actualiza en `CLAUDE.md`.
