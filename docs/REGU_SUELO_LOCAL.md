# Regu Suelo · local · especificación

Una aplicación que corre en el PC de Seba (`http://localhost:8000`) y junta todo lo construido: normativa, vigencia, afectaciones, ocupación, footprints y volumen en 3D. Es la demo para Mario y la base del producto.

**Solo local.** Puede mostrar normas en estado BORRADOR, siempre con una marca visible "BORRADOR · uso interno". No se publica.

## Stack

- **Backend:** FastAPI + Uvicorn, en `app/`. Reusa `etl/` (`ficha()`, `vigencias()`, la lectura de normas) sin duplicar lógica.
- **Footprints:** DuckDB con la extensión spatial, leyendo directo los GeoParquet de `paths.footprints` con filtro por bbox. No hace falta teselar los 10,7 M de edificios: se piden los del área visible.
- **Frontend:** un solo HTML con MapLibre GL + PMTiles, servido por FastAPI. Sin build de Node.
- **Arranque:** `python run.py app` (abre el navegador en localhost:8000).

## Endpoints

| Ruta | Devuelve |
|---|---|
| `GET /` | La aplicación |
| `GET /tiles/{tema}.pmtiles` | PMTiles locales: normativa y ocupación (con soporte de *range requests*) |
| `GET /api/ficha?lon=&lat=` | La ficha de un punto (contrato actual de `ficha()`) |
| `POST /api/ficha` (GeoJSON) | La ficha de un polígono dibujado: % por clase, afectaciones, riesgo y vigencia |
| `GET /api/edificios?bbox=` | Footprints del área visible (GeoJSON, con tope de N y aviso si se recorta), con `altura_est` |
| `POST /api/volumen` (GeoJSON) | Para un polígono: huella existente, pisos estimados, y V_max/V_opt fase 1 si la zona tiene normas (BORRADOR o VALIDADO), con su fuente y confianza |
| `GET /api/comunas` | Lista para el buscador |
| `POST /api/lamina?cut=` | Genera la lámina comunal con `sig/` y devuelve el PDF |

## Pantalla

1. **Mapa base:** límites comunales y buscador de comuna.
2. **Selector de capas:** normativa (por clase, con riesgo achurado), ocupación por zona (por coeficiente) y edificios (en 3D desde el zoom 15).
3. **Clic en el mapa → panel con la ficha:** clase, instrumento, zona, vigencia (norma, fecha y enlace a la ordenanza en PDF), afectaciones, la ocupación existente de esa zona y, si existen, las normas de la zona con su artículo de origen.
4. **Dibujar un predio** (herramienta de polígono) → ficha del polígono + **volumen en 3D:** edificios existentes en sólido y la envolvente posible (V_opt fase 1) translúcida, con su altura. Panel con m² existentes, m² máximos, remanente e IOV. Marca BORRADOR si las normas no están validadas.
5. **Botón "Lámina PDF"** de la comuna visible.
6. **Siempre visible:** el aviso legal y las fuentes con su atribución (IDE MINVU, Portal IPT, BCN, Overture/OSM con ODbL).

## Criterios de aceptación

- En Temuco centro, el clic devuelve U1, PRC Temuco, la zona y la Resolución N° 149, con enlace a la ordenanza, en menos de 300 ms.
- Al acercarse a Temuco aparecen los edificios en 3D sin trabar el navegador (con tope y simplificación según zoom).
- Un polígono dibujado en ZH2 muestra la envolvente con las normas BORRADOR y la marca correspondiente.
- La lámina PDF de Temuco se descarga desde la aplicación.
- Funciona sin internet, salvo las fuentes tipográficas: MapLibre y PMTiles se sirven localmente.
- Test: `ficha`, `edificios` y `volumen` responden con un GPKG y un parquet sintéticos.

## Reglas

- Las normas no se inventan. BORRADOR solo se muestra en local y marcado.
- No se muestran propietarios ni brechas de registro por predio.
- Las claves van solo en variables de entorno.
- Commit por paso. El backlog se actualiza en CLAUDE.md.
