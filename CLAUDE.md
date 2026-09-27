# regu-ipt-etl · contexto para agentes

Proyecto personal de Seba (geógrafo SIG, Temuco) en colaboración con **regu.cl**, plataforma de regularizaciones de un amigo arquitecto. Seba trabaja con autonomía técnica: prefiere archivos completos, comunicación directa en español y delegar decisiones de arquitectura cuando busca velocidad.

## Objetivo

Capa nacional (GeoJSON / GPKG / PostGIS) de **situación normativa del suelo**: cada m² de Chile queda clasificado según qué instrumento de planificación territorial (IPT), o qué ley si no hay IPT, lo norma. Es la base de un **informe normativo preliminar ("pre-CIP")** en regu.cl, cuyo flujo es ROL → predio SII → cruce con la capa. Después, Regu gestiona el CIP oficial ante la DOM.

**Regla legal dura:** el CIP lo emite solo la DOM (OGUC art. 1.4.4). Regu nunca "entrega" un CIP. Todo producto lleva el aviso de que es información referencial.

## Clases (partición planar exacta por comuna, gana la prioridad más alta)

1. U1: Plan Seccional
2. U1: Plan Regulador Comunal (PRC)
3. U2: Límite Urbano sin PRC
4. U3: zona urbana de PRI/PRM
5. E: extensión urbana de PRI/PRM
6. R1: zona rural de PRI/PRM (+ art. 55 LGUC)
7. R2: remanente rural sin IPT (art. 55 LGUC + DL 3.516)

`legal_refs.json` está en **BORRADOR**: el arquitecto debe validarlo antes de publicar.

## Arquitectura

- `run.py`: CLI con los comandos `discover | catalogo | download | build | all`, más `--region`, `--refresh` y `--postgis`.
- `etl/arcgis.py`: cliente de geoide.minvu.cl con reintentos, paginación por offset u objectIds, y caché en disco.
- `etl/normalize.py`: reglas de tipificación (override > service_rules > layer_rules), dedupe MapServer/FeatureServer, homologación de campos y ComunaResolver (nombre de plan → CUT).
- `etl/classify.py`: partición por comuna. Overlays en ESRI:102033, sin snapping (`grid_m: 0`), porque el snapping generaba astillas entre comunas.
- `etl/export.py`: GPKG (`capa_ipt` + `afectaciones`), GeoJSON RFC7946 nacional y por región, CSV de QA, resumen JSON y carga opcional a PostGIS.
- `tests/test_sintetico.py`: escenario Temuco / Padre Las Casas / Carahue + paginación simulada. **Debe pasar siempre.**

## Hechos verificados del servidor MINVU (26-sep-2026)

- Carpeta `IPT` con servicios `PRC_<Región>` (una capa por plan, con capas de riesgo y vialidad mezcladas), `PRI_*`, `PRMS`, `PRMC`, `PREMVAL`, `PRDU_*`, `Limites_Urbanos`, `IPT_AREA_RIESGO` y `Patrimonio`.
- Cada servicio está publicado como MapServer y como FeatureServer, a veces con distinta cantidad de capas. Se deduplica por (servicio, nombre de capa) y gana MapServer.
- `PRC_Nuble` y `PRC_Ñuble` están duplicados. Hoy se ignora `PRC_Nuble`. **PENDIENTE verificar cuál está vigente.**
- El servidor es inestable (errores del Web Adaptor): se trabaja siempre sobre el caché `data/raw`.
- MaxRecordCount 2000. Los SRID de origen varían por servicio; se pide `outSR=4326`.

## Base comunal

BCN SIIT, División comunal: `data/base/comunas_bcn/comunas.shp`, con 346 comunas en EPSG:3857 y campos `cod_comuna`, `Comuna` y `Region`. Datos de 2014 a 2018 según la BCN. Se descarga con `descargar_comunas.py` o desde https://www.bcn.cl/obtienearchivo?id=repositorio/10221/10396/5/comunas_final.zip

## Estado y próximos pasos

1. [ ] `python run.py catalogo`: revisar la lista de capas IGNORADAS y ajustar `service_rules`, `layer_rules` y `overrides`.
2. [ ] Piloto: `python run.py download --region ARAUCANIA` y luego `python run.py build --region ARAUCANIA`.
3. [ ] QA piloto: ninguna comuna con PRC conocido debe salir `sin_urbano=True`, la cobertura debe ser 100% y hay que revisar las zonas PRI con `revisar=True` y sus patrones `pri_subclase`.
4. [ ] Revisar a mano los nombres de capa PRC que no resuelven comuna (`cut_ipt` vacío).
5. [ ] Escalar a nivel nacional.
6. [ ] Siguiente fase: cruce con predios SII (proyecto GEOSAL de Seba, GeoParquet catastral) → endpoint pre-CIP (FastAPI + PostGIS).

## Entorno

- Windows + PowerShell, venv en `.venv` (`.venv\Scripts\activate`), Python 3.12, pandas 3.x, geopandas 1.1.
- Tests: `python tests\test_sintetico.py`
- Todo trabajo ocurre en el PC personal de Seba, fuera de su empleo. No usar datos ni recursos institucionales.

## Convenciones

- Código y comentarios en español.
- No hacer commit de `data/` (caché y salidas pesan GB).
- Cualquier cambio en `classify.py` debe mantener cobertura del 100% y cero traslapes en el test.
- Antes de "arreglar" una clasificación legal, preguntar: el criterio legal lo valida el arquitecto, no el código.
