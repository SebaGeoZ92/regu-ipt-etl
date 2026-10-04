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
6. R1: zona rural de PRI/PRM (+ art. 55 LGUC); después, `PRI_ENV` (contorno PRI sin zonificación)
7. R2: remanente rural sin IPT (art. 55 LGUC + DL 3.516)

`legal_refs.json` está en **BORRADOR**: el arquitecto debe validarlo antes de publicar.

## Decisiones de modelado (27-sep-2026, validadas por Seba)

- **Traslapes dentro de una fuente**: resta secuencial con `intersects` (no `overlaps`, que no ve contención ni igualdad). Orden: `rango` y luego menor área. El `rango` es: subclase explícita o `zone_override` (0) > `revisar` (1) > `pri_default` (2) > zona de riesgo (3) > envolvente (4).
- **Umbral de traslape** por comuna: `traslape_m2 < max(1, 1e-6 × área de la comuna)`, igual en el QA (`traslape_ok`) y en el test.
- **Envolventes PRI** (`pri_envolvente` en config: `PRI_Area_Rural`, "Unidad Territorial A/B/C" de `PRI_Araucanía`): fuente `PRI_ENV`, clase R1, ordenada después de `PRI_R`. Solo llenan lo que la zonificación PRI no cubre.
- **Riesgo como superposición, no como competidor** (`zona_riesgo`, regex sobre ZONA + descripción o sobre el nombre de la capa):
  - Las zonas de riesgo van con rango 3 y `revisar=False`, y se copian a `afectaciones`.
  - Después de la partición de cada comuna, cada pieza se corta contra la unión de todos los polígonos de riesgo de la comuna, incluidas las capas AFECTACION de riesgo. La parte interior sale con `riesgo=True` y conserva la clase y la zona base. Nunca deben producir R2 dentro de un PRC o PRI.
- **`zone_overrides`** `"<ipt_nombre>|<zona>": E|U|R|AFECTACION`: ganan sobre `pri_subclase`. AFECTACION saca la zona de la partición. Flujo con el arquitecto:
  - `build` genera `data/out/revision_arquitecto_<nacional|región>.csv` (uno por alcance, para que un build regional no pise el nacional), una fila por zona con `revisar=True` y la columna `decision` vacía; conserva las decisiones ya llenas.
  - `python run.py importar-revision <csv>` las lleva a `zone_overrides`.
- **`layer_rules_prioritarias`** se evalúan antes de `service_rules`, salvo en servicios IGNORAR. Solo patrones inequívocos: riesgo, patrimonio, zona típica y vialidad → AFECTACION; límite urbano → LU.
- **Comuna de instrumentos comunales, en cascada**: `comuna_fields` (COM…) → ADMIN sin "Municipalidad de " → NOM sin "Límite urbano de " (ambos solo si traen el prefijo) → `comuna_alias` → nombre de capa. Lo que no resuelve queda en `qa_sin_comuna_<tag>.csv`, porque sin CUT el instrumento puede normar la comuna vecina.
- **Afectaciones**: `build` las recorta a las comunas procesadas y les asigna `cut` y `comuna`. Las de alcance comunal (`afectacion_comunal: "/PRC_"`, que cubre `PRC_<Región>` e `IPT_AREA_RIESGO/PRC_Area_de_Riesgo`) resuelven `cut_ipt` con la misma cascada y **solo se aplican en su comuna** (28-sep-2026). Las de PRI/PRM y Patrimonio no se filtran.
- **APP no es riesgo**: `zona_riesgo_excluir: "^APP\b"` gana sobre `zona_riesgo`. Las zonas APP siguen en afectaciones, pero no marcan riesgo (28-sep-2026).
- **Capas superpuestas de PRC** (ZNE, ICH, ZCH, AR, restricción) se tipifican como AFECTACION, porque como PRC extendían U1.

## Arquitectura

- `run.py`: CLI con los comandos `discover | catalogo | download | build | all`, más `--region`, `--refresh` y `--postgis`. `--region` filtra `discover` y `download` por la extensión del servicio (`service_extent`), y `build` por comunas.
- `etl/arcgis.py`: cliente de geoide.minvu.cl con reintentos, paginación por offset u objectIds, y caché en disco.
- `etl/normalize.py`: reglas de tipificación (override > service_rules > layer_rules), dedupe MapServer/FeatureServer y homologación de campos. También ComunaResolver con cascada (`cut_por_cascada`), `pri_envolvente`, `zona_riesgo`, `zone_overrides` y `separar_afectaciones`.
- `etl/classify.py`: partición por comuna. Overlays en ESRI:102033, sin snapping (`grid_m: 0`), porque el snapping generaba astillas entre comunas. Los índices de `sindex.query` se ordenan para respetar el orden rango/área de las fuentes. Además: `recortar_afectaciones` e `instrumentos_sin_comuna`.
- `etl/export.py`: GPKG (`capa_ipt` con `riesgo`, más `afectaciones` con `cut`), GeoJSON RFC7946 nacional y por región, CSV de QA (`qa_comunas` con `traslape_m2`, y `qa_sin_comuna`), resumen JSON (cobertura mín/máx, traslape máx, instrumentos sin comuna) y carga opcional a PostGIS.
- `etl/ficha.py`: `ficha(geom_4326, gpkg) -> dict` (MVP: partición + afectaciones; `condicionantes` vacía). Contrato en `docs/CONDICIONANTES.md`. CLI: `python run.py ficha --lon X --lat Y | --wkt ...`.
- `etl/mapa.py`: `python run.py mapa --region X` genera `data/out/mapa_<region>/`, con `index.html` autocontenido (PMTiles embebido en base64, servido desde memoria a MapLibre 4.7.1 + pmtiles 3.2.1) y `artifact.html` (fragmento para publicar). Los tiles se hacen con el driver PMTiles de GDAL 3.12 (no hay tippecanoe en Windows). Hay que usar `encoding="UTF-8"` en pyogrio y `Protocol.tilev4` de pmtiles.
- `etl/portal.py` y `etl/vigencia.py`: cliente del Portal IPT y cruce portal ↔ servidor (`python run.py vigencia`). Genera `vigencia_<tag>.csv`, `vigencia_resumen_<tag>.json` y `vigencia_match.csv`; este último lo usan `ficha()` y el mapa para mostrar la norma, la fecha y la ordenanza. `build` escribe `inventario_servidor_<tag>.csv`.
- **Fuentes bajo demanda** (`docs/FUENTES_BAJO_DEMANDA.md`):
  - `fuentes/<id>.yaml` guarda un contrato por fuente; hoy hay seis, todas en estado **propuesta**. `fuentes/_esquema.json` los valida.
  - `etl/fuentes.py` contiene `validar()`, `cargar_contratos()`, `relevantes()`, `registrar_demanda()` y `estado()`.
  - `ficha()` registra cada consulta en `data/demanda/consultas.jsonl`, **sin coordenadas**, y devuelve `fuentes_pendientes`.
  - Comandos: `python run.py fuentes estado|validar`. **`activar` no está implementado: ninguna fuente está activa.**
- `etl/ocupacion.py`: ocupación del suelo por zona PRC con las huellas de Overture (`python run.py ocupacion`). Detalle y criterio de medición en el Backlog, sección Volúmenes.
- `etl/vcalc.py`: V_calc por predio (huella de Overture × pisos del SII); `python run.py volumen vcalc`. Detalle en el Backlog, sección Volúmenes.
- `tests/test_sintetico.py`: escenario Temuco / Padre Las Casas / Carahue con LU duplicado, zona contenida, PRI traslapados, envolvente PRI, zonas de riesgo en PRC y PRI, COM mal escrito y afectaciones recortadas, más paginación simulada. Exige cobertura de 100% ± 0,01 y traslape < 1 m² en **cada** comuna. **Debe pasar siempre.**

## Hechos verificados del servidor MINVU (26-sep-2026)

- Carpeta `IPT` con servicios `PRC_<Región>` (una capa por plan, con capas de riesgo y vialidad mezcladas), `PRI_*`, `PRMS`, `PRMC`, `PREMVAL`, `PRDU_*`, `Limites_Urbanos`, `IPT_AREA_RIESGO` y `Patrimonio`.
- Cada servicio está publicado como MapServer y como FeatureServer, a veces con distinta cantidad de capas. Se deduplica por (servicio, nombre de capa) y gana MapServer.
- `PRC_Nuble` y `PRC_Ñuble` son **duplicados exactos** (verificado el 28-sep-2026). Se ignora `PRC_Nuble` y se usa `PRC_Ñuble`. Evidencia:
  - Ambos tienen 25 capas con los mismos nombres y el mismo número de features por capa, en MapServer y FeatureServer.
  - Un hash MD5 de atributos (sin OBJECTID ni SHAPE) y de geometrías (EPSG:4326, 6 decimales, ordenadas) coincide en las **25/25** capas.
  - Las fechas de decreto (`P_DO`) son iguales. Ninguno publica `editingInfo`/`lastEditDate`, e `info/iteminfo` no trae `modified`/`created`, así que no se puede saber cuál es más antiguo.
  - Solo difieren en el GUID (`PRC_Nuble` 9D9E2392…, `PRC_Ñuble` E92287FC…) y en el título (`PRC_Nuble` frente a `PRC Ñuble`). Se usa `PRC_Ñuble` por ser el nombre canónico con Ñ; con contenido idéntico, la elección no cambia el resultado.
  - En `PRC_Portezuelo` (id 15), `orderByFields=OBJECTID` da error 400 en ambos servicios; se comparó sin ordenar.
- El servidor es inestable (errores del Web Adaptor): se trabaja siempre sobre el caché `data/raw`.
- MaxRecordCount 2000. Los SRID de origen varían por servicio; se pide `outSR=4326`.
- Esquema reciente de capas: REG/COM/LOC/ZONA/NOM. `COM` trae errores de tipeo ("Padre de Las Casas", "Teodoro Schmitdt"); ADMIN y NOM vienen bien.
- El mismo LU está publicado en `Limites_Urbanos/0` y en `PRC_<Región>` (geometría idéntica).
- Hay extensiones de servicio infladas: PRC_OHiggins, PRC_Valparaíso, PRI_Antofagasta y PRI_Coquimbo cruzan el bbox de Araucanía.
- `PRC_Valparaíso/72` (`PRC_LosAndes_ICH`) trae datos de Limache.
- Varias capas de servicios PRI/PRMS son de riesgo, LU o vialidad. `layer_rules_prioritarias` corrige 11 de ellas. `PRMS_LU`, `PRMS_Resguardo_*` y `PRI_Valparaiso/Área Protección cultural_pto` siguen como PRI/PRM: **PENDIENTE**.
- `PRC_Temuco_Areas_de_proteccion_y_riesgo` mezcla zonas de protección (APP 1, APP 3) con zonas de riesgo (ARC, ARI, ARP, ARRI). Las zonas de riesgo también están en `IPT_AREA_RIESGO/PRC_Area_de_Riesgo`. Un área de riesgo del PRC de Temuco se desbordaba 88 ha hacia Padre Las Casas. Resuelto con `zona_riesgo_excluir` y `afectacion_comunal`.
- `IPT_AREA_RIESGO/PRC_Area_de_Riesgo` trae COM mal escrito o con la localidad ("Pitufquén", "Puerto Saavedra"): se resuelve con `comuna_alias`.
- Capas publicadas vacías (0 features, 27-sep-2026): `PRC_OHiggins/25` Palmilla–San José del Carmen, `PRC_Valparaíso/2` Calle Larga y `PRC_Valparaíso/32` San Esteban, todas de riesgo. **PENDIENTE** volver a pedirlas.

## Portal IPT de MINVU (verificado el 28-sep-2026)

- API pública del portal (la usa su propio frontend Nuxt; la base sale de `__NUXT__.config.public.apiURL`): `https://portalipt-api.minvu.cl`. Cliente en `etl/portal.py` (`PortalIPT`), con caché en `data/raw/portal/`, 1 s de pausa entre solicitudes y reintentos con backoff. **Usar siempre el caché**; `--refresh` solo cuando haga falta.
  - `GET /instrumentos?estado=Vigente`: lista de unos 11 MB con 2.026 instrumentos (PRC 1.467, PRM 324, LU 125, PS 77, PRI 29, PRDU 4, incluidas modificaciones). Campos útiles:
    - `id`, `codigo`, `denominacion`, `planificacion` (Comunal/Intercomunal), `tipo` (PRC, PS, LU, PRI, PRM, PRDU) y `comunas` (lista de CUT INE).
    - `clasificacion` ("Instrumento de origen", "Modificación"…), `numeroDocumento`, `fechaInicioVigencia` e `instrumentosDescendientesIds` (texto "1058, 1059").
    - `documentos`: lista de `{tipo, nombre, url}`.
    - Fechas de publicación y derogación.
  - `GET /instrumentos/{id}`: lo mismo, más `instrumentosBase`, `relacionesBase`/`relacionesDescendientes` y `plantilla`.
  - `GET /comunas`: `{idComuna, idProvincia, nombre, codigoCompuestoComunaINE, activo}`.
  - `GET /regiones`: `{idRegion, nombre, ordinal, ordenGeografico, codigoRegionINE, activo}`.
  - **Ordenanzas**: 4.006 documentos. De ellos, 792 son ordenanzas: 101 de tipo "Ordenanza" y 691 "Publicación D. O. con Ordenanza". Los demás son planos (1.697, JPG), "Publicación D. O." (553), decretos (355), memorias (292), estudios (175) y otros. Casi todos son PDF en `instrumentosdeplanificacion.minvu.cl/files/maps/<n>/<archivo>.pdf`; un HEAD de prueba dio 200 `application/pdf`. Algunos enlazan a SharePoint, `portaltransparencia.cl` o sitios municipales. El número y tipo de norma (Decreto o Resolución N°) viene en `documentos[].nombre`. **No descargar en masa todavía.**
- Cruce con la capa nacional: 327 de 345 comunas tienen IPT comunal vigente según el portal. Discrepancias:
  - **Lumaco** (LU de 1939) y **María Elena** (LU de Quillagua, 1944) tienen LU vigente en el portal, pero el servidor no publica su geometría.
  - **Huara**: el instrumento comunal vigente es el **PRC de Pisagua (1966)**, que es costero. La capa `PRC_Huara` casi no toca la comuna BCN (problema de línea de costa).
  - **Cochrane**: tiene LU en el servidor, pero el portal no registra IPT comunal vigente.
- Las otras 14 comunas `sin_urbano` no tienen IPT comunal en el portal, así que su clasificación es correcta. Camarones y General Lagos solo tienen PRDU, que es indicativo.

## Base comunal

BCN SIIT, División comunal: `data/base/comunas_bcn/comunas.shp`, con 346 comunas en EPSG:3857 y campos `cod_comuna`, `Comuna` y `Region`. Datos de 2014 a 2018 según la BCN. Se descarga con `descargar_comunas.py` o desde https://www.bcn.cl/obtienearchivo?id=repositorio/10221/10396/5/comunas_final.zip

### Alternativas de DPA con mejor línea de costa (evaluadas el 29-sep-2026, sin adoptar)

| Opción | Qué es | Costa | Acceso | Evaluación |
|---|---|---|---|---|
| **DPA 2023 SUBDERE** (con IGM, DIFROL e INE) | Polígonos de comunas, provincias y regiones. Límites interiores de SUBDERE, límite internacional de DIFROL y **costa e islas del IGM** | 1:50.000, SIRGAS-Chile | `https://ide.subdere.gov.cl/descargas/SHP/Limite_DPA_03082023.rar` (262 MB, HEAD 200, publicado el 21-feb-2024). También en geoportal.cl y datos.gob.cl | **Recomendada.** Es oficial, trae la mejor costa disponible y SUBDERE declara actualización anual. Hay que verificar que tenga 346 comunas y códigos compatibles con el CUT. |
| DPA de Chile 2026 (hub ArcGIS del Observatorio de Ciudades UC) | Republicación en ArcGIS de la DPA oficial | Igual que su fuente | ArcGIS Hub (servicio REST) | Útil si se quiere consumir por REST, pero no es la fuente primaria. Confirmar versión y licencia. |
| Cartografía del Censo 2024 (INE) | Cartografía censal país (GeoParquet) y base cartográfica APC 2023 (GDB/SHP) | Base APC 2023, compatible con la DPA 2023 | ine.gob.cl, Geodatos abiertos | Buena para cruces estadísticos. Para límites comunales conviene la DPA SUBDERE. |
| BCN SIIT (la actual) | División comunal de 2014 a 2018 | Generalizada: 3.416 ha de IPT quedan fuera | Ya en `data/base` | Se mantiene con `extension_costera` mientras no se migre. |

Si se migra, basta con cambiar `paths.comunas` y los tres `field_*` en config. Antes hay que comparar comuna por comuna el área y el QA de pérdida costera contra la BCN, y medir con los predios SII (GEOSAL) cuántos quedan fuera de cada DPA.

## Estado y próximos pasos

1. [x] `python run.py catalogo`: ajustadas las capas IGNORADAS. Quedan ignorados a propósito `PRC_Nuble` y los PRDU.
2. [x] Piloto: `python run.py download --region ARAUCANIA` y luego `python run.py build --region ARAUCANIA`.
3. [ ] QA piloto (28-sep-2026): 32 comunas, cobertura 100% en todas, traslape máx 0,4 m² (bajo el umbral relativo en todas), 0 instrumentos sin comuna. Pendiente:
   - **Lumaco** sale `sin_urbano=True`: MINVU no publica PRC ni LU. Confirmar con el arquitecto o la DOM.
   - 8 zonas del PRI Lago Villarrica con `revisar=True` (Zona de vivienda, hoteleras, camping, etc.; hoy quedan en R1 por defecto). El arquitecto debe llenar `data/out/revision_arquitecto_araucania.csv` (o el nacional) y luego se corre `importar-revision`.
4. [x] Nombres PRC/LU que no resuelven comuna: se resuelven con la cascada. Revisar `qa_sin_comuna_<tag>.csv` en cada región.
5. [ ] Escalar a nivel nacional. `discover` + `download` listos (27-sep-2026): 579 capas en el catálogo y las 539 a descargar en caché (117.030 features). Las 3 capas vacías están vacías en origen (`download --vacias`, 28-sep).
   `build` nacional (28-sep-2026): 25,4 min, 346 comunas, 74.833 piezas, cobertura 100,0% en todas, traslape relativo máx 2,1e-7 (Pedro Aguirre Cerda), 0 piezas inválidas y 2 rescates GEOS (La Pintana, Chañaral). Pendiente:
   - **17 comunas + "Zona sin demarcar" (cut 0) con `sin_urbano=True`** (ver `qa_comunas_nacional_*.csv` y la sección Portal IPT). **Huara**: el 97,8% del PRC (Pisagua, costero) queda fuera de la DPA BCN.
   - **Pérdida costera** (`qa_fuera_dpa_*.csv`, build del 28-sep-2026): 3.416 ha de IPT comunal (PRC/LU/seccional) quedan fuera de TODAS las comunas BCN, en 77 comunas, todas costeras o insulares (máximos: Caldera 466 ha, Antofagasta 312 ha, Puerto Montt 269 ha, Iquique 212 ha en 2.500 fragmentos). La causa es que la línea de costa de la BCN está generalizada. **PENDIENTE decidir** entre (a) extender cada comuna con la huella de su propio IPT fuera de la DPA (asignada por `cut_ipt`, no por cercanía) y (b) cambiar de DPA, idealmente medido antes contra los predios SII (GEOSAL).
   - **7 capas sin comuna**:
     - COM que no calzan con la BCN: Paiguano/Paihuano, Guaticas/Guaitecas, Entre Lagos (Puyehue), Llay Llay/Llaillay, Trehuaco/Treguaco, La Calera/Calera y Puerto Natales/Natales. Se resuelven con `comuna_alias`.
     - `PRI_Valparaiso/Límite Urbano` (id 6) es del Plan Metropolitano de Valparaíso, Satélite Aconcagua (San Felipe y Los Andes, 11 comunas). **PROVISORIO** (28-sep-2026, hasta que lo confirme el arquitecto): override `{tipo: PRI, pri_default: E}` en vez de LU. La zona se lee de `NOM` (`campo_zona` en config) y se separa por polígono con `zone_overrides`, ambos **PROVISORIOS**: "Límite de Extensión Urbana" (94,6 km²) → E y "Límite Urbano Vigente" (74,6 km²) → U (U3).
   - `revision_arquitecto_nacional.csv`: 137 zonas (el 29-sep, antes de este cambio, el build regional pisó el archivo nacional: regenerarlo con el próximo build nacional). Incluye las `PRMS_Resguardo_*` tipificadas como PRM (pendiente).
6. [ ] **MVP (prioridad actual)**: `ficha()` solo con partición + afectaciones (sin condicionantes), y un mapa HTML de La Araucanía con PMTiles (clic → clase, instrumento, zona, norma_titulo, aviso) que se pueda enviar a una persona para probarlo.
7. [ ] **Fase siguiente: condicionantes territoriales, como fuentes bajo demanda.** Especificación en `docs/FUENTES_BAJO_DEMANDA.md`, y en `docs/CONDICIONANTES.md` para la ficha.
   - Hecho (29-sep-2026): esquema de contrato, seis contratos en estado `propuesta` (CONADI tierras indígenas y ADI, SAG subdivisiones, CIREN capacidad de uso, CONAF bosque nativo, MMA humedales urbanos), registro de demanda en `ficha()` y `run.py fuentes estado|validar`.
   - Pendiente:
     - El "Paso 0" de cada fuente: acceso real, licencia y fecha.
     - Decidir si SNASPE, que venía de CONDICIONANTES.md, tiene su propio contrato.
     - Implementar `fuentes activar`, con el test `mapeada → latente → activa` y la regla de que `excluir_siempre` nunca llega a la salida.
     - Crear `fuentes/CREDITOS.md`.
   - **No activar ninguna fuente sin decisión de Seba.**
8. [ ] **Piloto de volumen** (`docs/VOLUMEN_PILOTO.md`). Paso 0 hecho el 30-sep-2026: `etl/volumen.py` y `run.py volumen footprints|candidatas|plantilla`.
   - Footprints de Overture, release `2026-09-23.1`: 181.475 edificios en el bbox del PRC de Temuco, en `data/base/footprints/` (ODbL, capa aparte). **Casi ninguno trae `height`**, así que los pisos saldrán de la superficie construida del SII.
   - `data/out/volumen_candidatas_temuco.csv` tiene 28 zonas; las 14 residenciales (ZH, ZHE, ZHR) van primero. El Portal IPT enlaza la ordenanza. La descripción de zona viene vacía en la capa del PRC de Temuco.
   - Pendiente:
     - Que Mario elija la zona; luego `run.py volumen plantilla --ipt Temuco --zona <Z>`.
     - La ruta del GeoParquet catastral de GEOSAL para `--predios`; si no existe, usar la API de catastral.cl con `CATASTRAL_API_KEY`.
     - Los pasos siguientes (cálculo, salidas, mapa y ficha) solo con normas llenadas por el arquitecto.
9. [ ] Cruce con predios SII (proyecto GEOSAL de Seba, GeoParquet catastral) → endpoint pre-CIP (FastAPI + PostGIS).

## Backlog

Tareas compartidas entre agentes. Protocolo en `AGENTS.md`: marcar `en curso (<agente>, <fecha>)` con commit antes de
empezar, no tomar lo que esté en curso por otro y, al terminar, `hecho (<commit>)` más una línea de traspaso. Codex
no tiene red: solo toma tareas con "Requiere red: No".

### Footprints nacionales (`docs/FOOTPRINTS_NACIONAL.md`)

Huellas de Overture (ODbL) de todo Chile, por comuna, en `data/base/footprints/` y **separadas del Atlas**.

| # | Tarea | Responsable | Requiere red | Estado |
|---|---|---|---|---|
| S0 | Herramienta `run.py footprints descargar --region X` + `footprints estado`, test sintético (recorte, asignación por mayor área, columnas, manifiesto) | Claude Code (o Codex: el código y los tests no necesitan red) | No para los tests | hecho (`ccb95a9`) |
| S1 | La Araucanía: descargar, validar contra los 181.475 de Temuco y medir tiempo y MB | Claude Code | Sí | hecho (`79adca0`) |
| S2 | Norte: Arica, Tarapacá, Antofagasta, Atacama y Coquimbo | **Seba** (comando en lote) | Sí | hecho (2-oct-2026; manifiestos en `docs/footprints/`) |
| S3 | Centro: Valparaíso, Metropolitana, O'Higgins, Maule y Ñuble | **Seba** | Sí | hecho (2-oct-2026) |
| S4 | Sur: Biobío, Los Ríos, Los Lagos, Aysén y Magallanes | **Seba** | Sí | hecho (2-oct-2026) |
| S5 | QA nacional: conteo y área por comuna, duplicados en bordes regionales, edificios fuera de la DPA, comunas con cobertura sospechosamente baja | Claude Code o Codex | No | pendiente |
| S6 | Integración: la ficha predial informa n.º de edificios, m² de huella y pisos estimados; el volumen usa los footprints nacionales | Claude Code | No | pendiente |
| S7 | Teselas: PMTiles de edificios por región, como capa aparte en el mapa, con atribución | Codex | No | pendiente |

Traspaso S0 (`ccb95a9`): `etl/footprints.py` y `run.py footprints descargar|estado` listos, con test sintético en `tests/test_sintetico.py` (`test_footprints_sintetico`). Para seguir: S1 corre la herramienta real en La Araucanía.

Traspaso S1 (`79adca0`): La Araucanía tiene **1.091.394 edificios, 117,7 MB y tardó 136 s** con el release `2026-09-23.1`.
- Validación: los 181.475 del archivo de Temuco están todos y en la misma comuna.
- Fuentes: Microsoft 48 %, Google 45 % y OSM 6 %. **Casi ninguno trae altura** (height 0 %, num_floors 0,56 %), así que los pisos saldrán del SII.
- El bbox trajo 1.366.800 edificios: 260.067 eran de regiones vecinas y 15.339 quedaban fuera de la DPA; ninguno de los dos se guarda.
- Estimación nacional: unos 1,1 a 1,5 GB y entre 40 y 60 min, con la Metropolitana como la más pesada (vigilar la RAM).
- Para seguir: Seba corre S2 a S4 con el comando de `docs/FOOTPRINTS_NACIONAL.md`, usando `--release 2026-09-23.1` para que todo el país quede en el mismo release.
Traspaso S2 a S4: están las 16 regiones y "Zona sin demarcar", todas con `completa: true` y el release `2026-09-23.1`: **10.733.719 edificios, 1.177,6 MB**. Pendiente S5 (QA nacional), S6 y S7.
Disco: desde el 2-oct-2026 los footprints viven en `D:\regu-data\footprints` (unos 207 GB libres); `footprints estado` informa la carpeta y el disco libre.

### Volúmenes (`docs/VOLUMENES.md`)

Cuatro volúmenes por predio (V_max, V_opt, V_calc, V_real), sus índices (IOV, remanente, eficiencia, brecha de registro) y la confianza de cada valor: ver `docs/VOLUMENES.md`. Reglas duras de ese documento, que valen para todas las etapas:
- Las normas no se inventan: al público solo se muestran las `VALIDADO` por el arquitecto.
- La brecha de registro nunca se publica por predio ni se usa para prospección; solo la ve quien consulta su propio predio, o en agregados.
- Cada número lleva su `fuente` y su `confianza`, y siempre el aviso de información referencial.
- `V_max ≥ V_opt`: si da lo contrario, es un error de datos y se marca.

| # | Tarea | Responsable | Requiere red | Estado |
|---|---|---|---|---|
| V1 | Ocupación real del suelo por zona PRC, a nivel nacional con los footprints de `paths.footprints`: CSV por zona (ipt, zona, ha, m² de huella, coeficiente de ocupación existente, n.º de edificios) y capa para el mapa coloreada por coeficiente | Claude Code | No | **hecho** (`efe37e0`, `215e42e`; corrida nacional del 4-oct-2026) |
| V2 | V_calc por predio donde haya datos SII (piloto Temuco con catastral.cl, cuidando la cuota de 100/día y 20/min; nunca escribir la clave en el repo ni en logs). Pisos = superficie construida SII / área de huella, o `num_floors` de Overture | Claude Code | Sí | **piloto hecho con 3 predios de ZH2** (4-oct-2026); falta ampliar la muestra |
| V3 | V_max y V_opt en la zona piloto, cuando Mario llene `normas_zona.csv` (V_opt de la fase 1 sin rasantes, declarado; rasantes en la fase 2 con geometría 3D) | Claude Code | No | pendiente: las normas de ZH2 y ZHR5 de Temuco están en BORRADOR (4-oct-2026); falta que Mario las valide |
| V4 | V_real con datos Z: primero `height` de Overture donde exista; luego nDSM (LiDAR o fotogrametría), revisando disponibilidad para Temuco y conectando con Living DEM | Claude Code | Sí | pendiente |
| V5 | Salidas: CSV por predio (`rol, cut, zona, v_max, v_opt, v_calc, v_real, iov, remanente_m2, eficiencia, brecha_registro` más `fuente_*` y `confianza_*`), agregados por manzana, zona y comuna, y mapa con color por IOV y extrusión 3D | Claude Code | No | pendiente: después de V2 y V3 |

Nota: V1 ya produce el coeficiente existente por zona; se compara con el normativo cuando haya tabla de normas validada.

Traspaso V1: `etl/ocupacion.py` y `python run.py ocupacion [--region X] [--gpkg G]`, con test `test_ocupacion_sintetico`.
- Procesa las regiones con manifiesto de footprints `completa: true` y escribe en `paths.out`: `ocupacion_zonas_<tag>_<fecha>.csv` (utf-8-sig), `ocupacion_<tag>_<fecha>.gpkg` (capa `ocupacion_zonas`, con `tramo` y `color`) y `ocupacion_qa_<tag>_<fecha>.json` (con la leyenda). `<tag>` es `nacional` solo cuando hay footprints de todas las regiones del GPKG. Corrida nacional del 4-oct-2026 (GPKG de build del 29-sep): **13 min**, proceso de unos 0,7 GB de RAM y sin problemas con la Metropolitana.
- **Resultado nacional**: 5.668 zonas de 286 PRC, 489.577 ha, 4.736.092 edificios, 539,1 millones de m² de huella, coeficiente global 11,0 %, máximo 82 % (Independencia P-2-1-1, 0,41 ha) y ninguna zona sobre 100 %. Hay 285 zonas sin edificios (a revisar en S5: pueden ser áreas verdes y cauces, o comunas con huellas faltantes). "Zona sin demarcar" no tiene piezas PRC.
- Temuco, para comparar con la norma BORRADOR (`normas_zona.csv`): ZH2 26,0 % existente frente a 0,5 (aislado) o 0,65 (pareado/continuo) normativo, y ZHR5 40,5 % frente a 0,7. El existente es sobre el área bruta, así que no es comparable de forma directa con el normativo, que se mide sobre el predio.
- **Cómo se mide**: la huella es el edificio recortado contra la zona; el n.º de edificios cuenta cada uno una vez, por su punto representativo; las piezas con `riesgo=True` suman a su zona. El coeficiente es huella / área **bruta** de la zona (incluye calles y áreas verdes), por lo que **no es el coeficiente de ocupación de suelo de la OGUC** (que es sobre el predio neto). Sirve para comparar zonas, no para verificar la norma.
- La Araucanía (GPKG nacional del 29-sep): 297 zonas, 23.142 ha, 278.576 edificios, 26,1 millones de m² de huella, coeficiente global 11,3 %, máximo 70 % (Temuco ZE1) y ninguna zona sobre 100 %. Hay 7 zonas sin edificios, todas agrícolas, de cauce o áreas verdes (Carahue Z-R9 y Z-R10, cauces de Freire y Loncoche, Cajón y Victoria).
- Temuco: ZH3 33 %, ZHR6 37 %, ZM2 22 %; las extensivas ZE6 y ZHE5, cerca de 1 %.
- Pendiente: la capa aún **no está en el HTML del mapa** (hoy tiene una sola fuente PMTiles). Para eso hay que decidir un selector de capas o un PMTiles aparte.

Traspaso V2 (4-oct-2026): `etl/vcalc.py` y `python run.py volumen vcalc --ipt Temuco --predios <json>`, con test `test_vcalc_sintetico`. Lee un JSON de predios ya guardado (no consulta la API) y escribe `vcalc_<ipt>_piloto.csv` en `paths.out`.
- Regla: huella = suma de los edificios de Overture con más del 50 % dentro del predio; pisos = `max(1, round(construida SII / huella))`, con `num_floors` de Overture como alternativa; sin ninguno, `sin_dato`. Entrega `m2_equiv = huella × pisos`; **`v_calc_m3` queda vacío** porque la ordenanza de Temuco no fija la altura de piso de referencia (no se inventa). Cada fila lleva `fuente_*`, `confianza_pisos` y `avisos`.
- Acceso a catastral.cl: se usó el **MCP catastral** (ya configurado con `CATASTRAL_API_KEY`), porque la documentación del repo no trae la URL ni la autenticación de la API REST y no se adivinan con la clave de por medio. El código de `vcalc` no toca la clave. Los datos de terceros van a `D:\regu-data\raw\catastral\` (no al repo), con su registro de consultas `consultas_piloto_v2.jsonl`.
- **Consumo de consultas**: 5 llamadas para 3 predios. Una `predios_cerca` (radio 100, límite 3) devolvió los 3 predios con punto pero **sin polígono**; luego una `ficha_predio` con geometría por predio. Eso da 3 consultas de ficha, más 2 de descubrimiento (una dio **HTTP 422** con `radio=40`: parece haber un radio mínimo, probablemente 100). Son 1,7 por predio, o 1 por predio si ya se conocen los roles. **No se pudo leer el contador del servidor**: el MCP no devuelve cabeceras de cuota, así que no se sabe si el 422 gastó cuota, y no se debe llamar `estadisticas_base` (consulta cara).
- Código de comuna SII: Temuco es **9201** en catastral.cl, no 9101 (que es el CUT).
- Resultado (los 3 predios son de Villa Antukuyen, manzana 5073, ZH2, 100 m² de terreno, que es menos que el mínimo predial de 150 m² de ZH2): construida SII 26 m² cada uno, 1 piso. Huella de Overture: 51,8 m² (2 edificios), 32,8 y 32,7 m². Hay más huella que superficie registrada, algo habitual en estas villas por ampliaciones; esa brecha **no se publica por predio** (regla de `docs/VOLUMENES.md`).
- La muestra no es representativa: son vecinos de una misma villa de vivienda social. Para el piloto conviene elegir predios de varios tamaños y, si se puede, de manzanas distintas.

Traspaso normas ZH2 y ZHR5 de Temuco (4-oct-2026, **BORRADOR**): `normas/normas_zona.csv` (**versionado en el repo**: es dato curado por el arquitecto y no puede vivir solo en un disco; ruta `paths.normas`) con 3 filas, una por agrupamiento (ZH2 aislado, ZH2 pareado/continuo y ZHR5 continuo). Origen: Ordenanza Local del PRC Temuco-Labranza (Res. N° 149/2010, actualizada a nov-2015), Art. 16 (tablas B 2 y B 8) y Art. 4 (antejardín). El PDF está en `D:\regu-data\raw\ordenanzas\`. El esquema ganó las columnas `fuente_por_valor` y `notas` (ver `docs/VOLUMEN_PILOTO.md`).
- Vacíos anotados en `notas`: `altura_max_pisos` y `altura_piso_ref_m` (la ordenanza fija la altura solo en metros) y `distanciamiento_m` (solo se carga el de 4 m de la nota `*6` de ZH2 continuo/pareado).
- **Discrepancia en la ordenanza, sin resolver: lo debe aclarar el arquitecto.** ZHR5 con altura adicional `*5`: el Caso Especial 3 sube la constructibilidad "en 2 puntos" (2,5 → 4,5), pero la tabla B 8 dice 3,5.
- Por verificar con Mario: que no haya modificaciones posteriores a nov-2015 (el Portal IPT registra 3 modificaciones, la última del 13-jun-2015).

### Regu Suelo local (`docs/REGU_SUELO_LOCAL.md`)

Aplicación local (`python run.py app`, `http://localhost:8000`): FastAPI + DuckDB spatial + MapLibre/PMTiles servidos localmente. **Solo local; muestra normas BORRADOR marcadas "BORRADOR · uso interno"; no se publica.** Reglas: las normas no se inventan, no se muestran propietarios ni brechas de registro por predio, y las claves van solo en variables de entorno.

| # | Tarea | Responsable | Requiere red | Estado |
|---|---|---|---|---|
| A1 | Dependencias (fastapi, uvicorn, duckdb) y esqueleto `app/`, con `/api/comunas` y `/api/ficha` (punto y polígono), más tests con GPKG sintético | Claude Code | Sí (instalar) | hecho (`40f9f6c`) |
| A2 | `/api/edificios` (DuckDB, bbox, tope y `altura_est`) y `/api/volumen` (huella, pisos, V_max/V_opt fase 1 con norma, fuente y confianza) | Claude Code | No | hecho (`8e953b3`) |
| A3 | PMTiles locales de normativa y ocupación, `/tiles/{tema}.pmtiles` con *range requests*, y MapLibre/PMTiles servidos desde `app/static/` | Claude Code | Sí (bajar JS una vez) | hecho (`7b6acc3`); teselas verificadas por HTTP |
| A4 | Frontend: mapa base, buscador, selector de capas, panel de ficha, edificios 3D | Claude Code | No | hecho (`39219e7`); **verificado en Chrome real** el 4-oct-2026 con `tests/ui/prueba_ui.js` |
| A5 | Dibujo de predio y volumen 3D (existente sólido y envolvente V_opt translúcida) con marca BORRADOR | Claude Code | No | pendiente |
| A6 | `/api/lamina` con `sig/` y botón "Lámina PDF"; `python run.py app`; criterios de aceptación y cierre | Claude Code | No | `/api/lamina`, botón y `python run.py app` hechos y verificados (`39219e7`); faltan los criterios de A5 y el PR de `sig/` |
| A7 | Mapas base: selector con sin fondo, OSM, Esri World Imagery y Sentinel-2 cloudless 2016 (EOX), atribución visible y transparencia por capa temática | Claude Code | Sí (los mapas base) | hecho (`d941b41`, `69e6315`) |
| A8 | **Migrar el fondo OSM raster a un basemap vectorial Protomaps de Chile en PMTiles local**: sin depender de `tile.openstreetmap.org`, sin internet y publicable (ODbL, con atribución). Extraer Chile de la compilación diaria de Protomaps con `pmtiles extract` y servirlo por `/tiles/`, con estilo MapLibre propio y las etiquetas de calles y comunas (hoy no hay *glyphs* locales: habría que empaquetar fuentes) | Claude Code | Sí (descargar el extracto una vez) | pendiente; **necesario antes de publicar**: el servidor de teselas de OSM es comunitario, de uso moderado y sin garantías |

Traspaso A3, A4 y A6 (4-oct-2026):
- **Teselas** (`python run.py teselas [normativa|ocupacion|comunas]`, en `<paths.out>/tiles/`): comunas 9,9 MB, ocupación 22,2 MB (35 s) y **normativa nacional 129 MB (22 min, 75.275 piezas)**. El servidor las entrega con *Range* (206) y los tres temas devuelven la cabecera PMTiles v3 y su directorio raíz por HTTP. MapLibre 4.7.1 y pmtiles 3.2.1 van en `app/static/vendor/` con sus licencias (BSD-3); la página no carga nada de internet.
- **`python run.py app [--puerto N] [--no-abrir]`**: escucha solo en `127.0.0.1`. Avisa si faltan teselas y abre igual.
- **Verificación de la interfaz (4-oct-2026).** La extensión de Chrome «Browser 1» no pudo abrir `localhost:8000` (probablemente corre en otra máquina), así que se probó con **Chrome local sin ventana y `puppeteer-core`**: `tests/ui/prueba_ui.js` (opcional y manual; necesita el servidor en marcha, Node y `npm i puppeteer-core` en una carpeta temporal, no es dependencia del repo). Resultado con datos reales: carga completa, ficha de Temuco en 113 ms, 2.552 edificios en 3D a zoom 16,5, capa de ocupación, buscador, transparencias aplicadas y **recordadas tras recargar**, los tres mapas base con su atribución, sin ninguna petición externa con «Sin fondo», lámina descargada y **cero errores de consola y de red**. Pendientes de esa prueba: el dibujo de predio y el volumen en 3D (A5).

Mapas base (A7, 4-oct-2026). Elección y transparencias se guardan en `localStorage` (`regu.fondo`, `regu.opacidad`; con `try/catch`, la página funciona sin almacenamiento). **«Sin fondo» es el valor por defecto** y no pide nada a internet.
- **OpenStreetMap**: política de teselas (https://operations.osmfoundation.org/policies/tiles/) respetada para **uso local**: User-Agent identificable (`app.user_agent` en `config.yaml`; el navegador no puede fijarlo, por eso las teselas pasan por `/basemap/osm/{z}/{x}/{y}.png`, que las guarda en `<paths.out>/cache_basemap/osm/` 14 días, sirve la copia vencida sin internet y limita a 2 conexiones), atribución visible y sin descargas masivas. Los datos son ODbL. El UA no lleva correo; si OSM debe poder contactar, agregar un contacto a `app.user_agent`. **No sirve para publicar** (ver A8).
- **Esri World Imagery**: se pide directo desde el navegador, sin proxy ni caché en disco. **Esri exige revisar sus términos antes de publicar:** el uso en un producto o servicio público o comercial normalmente requiere licencia o cuenta de desarrollador, y las teselas tienen restricciones de almacenamiento y reutilización. Atribución: «Esri, Maxar, Earthstar Geographics y la comunidad de usuarios de GIS», con enlace a los términos. **Pendiente antes de publicar: leer el acuerdo de Esri o reemplazar este fondo.**
- **Sentinel-2 cloudless de EOX**: la capa «2016» se llama `s2cloudless_3857` en su WMTS (`s2cloudless-2016_3857` no existe: da 404). Según sus capacidades WMTS (4-oct-2026) esa capa va con **CC BY 4.0**, que permite uso comercial con atribución; **las capas de 2019 en adelante son CC BY-NC-SA (no comerciales)**, así que no cambiar de año sin revisar la licencia. Atribución: «EOxCloudless © EOX IT Services GmbH (contiene datos Copernicus Sentinel modificados, 2016)». Resolución nativa de unos 10 m: `maxzoom` 13 y se sobre-amplía.
- **Lámina** (`POST /api/lamina?cut=09101`): Temuco 14,6 s la primera vez (1,9 MB) y 1,2 s desde caché (`<out>/laminas/`). Arma un GPKG temporal por comuna porque `sig.render.generar()` pide una capa `comunas` que el GPKG nacional no trae. **Con el `sig/` de master la lámina sale mal fuera de La Araucanía** (probado con Santiago: el rótulo dice «Región de La Araucanía» y el minimapa muestra todo Chile sin resaltar la comuna; 66 s). Ya está arreglado en la rama `codex/sig-layouts` (7 commits, unas 190 líneas en `sig/`), **pendiente de PR y de tu revisión**; no se mezcló. Mientras tanto `app.lamina_regiones` (config.yaml) limita la lámina a La Araucanía y otras regiones devuelven 409 con ese motivo; poner `todas` cuando se integre. El minimapa pide `mapa_<región>/capa_ipt.pmtiles` (`run.py mapa --region`); hoy solo existe el de La Araucanía.

Traspaso A1 y A2 (`40f9f6c`, `8e953b3`): `app/` (`main.py`, `ajustes.py`, `datos.py`, `edificios.py`) y `etl/envolvente.py`. Endpoints listos: `GET /api/comunas`, `GET|POST /api/ficha`, `GET /api/edificios?bbox=&zoom=&limite=` y `POST /api/volumen`. Se crea con `crear_app(Ajustes.desde_cfg(cfg, run.ruta))`; los tests usan un GPKG y un parquet sintéticos (`_app_fixture` en `tests/test_sintetico.py`).
- **Medido con datos reales** (GPKG nacional del 29-sep, footprints de La Araucanía): ficha de un punto 80 ms (205 ms la primera vez); edificios del centro de Temuco 320 ms (941 ms en frío, con DuckDB cargando) y 1 a 2 MB de JSON; volumen de un predio 340 a 450 ms. Criterio de los 300 ms cumplido para la ficha.
- **DuckDB**: `INSTALL spatial` necesita red **una sola vez**; después carga desde su caché. Filtra por `cut` de las comunas que cruza el bbox (sin eso, cada consulta escanea el millón de edificios de la región, unos 285 ms). `/api/edificios` pone tope de 5.000 (máx. 20.000) y avisa; sobre 0,15° de lado pide acercarse.
- **`altura_piso_ref_m` es un parámetro del modelo, NO una norma** (`config.yaml`, bloque `volumen`, 3,5 m, el módulo de piso que usa la ordenanza de Temuco en el Art. 16 `*5`). Una norma de zona que traiga el valor en `normas/normas_zona.csv` lo reemplaza. Cada resultado lo declara en `simplificaciones`. **Pendiente: que el arquitecto lo confirme.**
- Reglas del cálculo (fase 1, `docs/VOLUMEN_PILOTO.md`): retranqueo uniforme `max(antejardín, distanciamiento)`; si el antejardín trae varios valores según la vía, se usa el menor porque el frente no se identifica; sin rasantes. Un predio angosto sin base edificable devuelve `sin_base` (no un 0 engañoso). Una zona sin normas devuelve solo lo existente. Sin edificios en Overture, el existente es 0 y se avisa. Los pisos sin dato (casi todos: `num_floors` es 0,56 %) se asumen 1 y se marcan cota inferior, confianza baja.
- Cuidado: con el retranqueo uniforme de 3 a 4 m, los predios chicos (p. ej. los de 100 m² de Villa Antukuyen) quedan sin base edificable; es una limitación declarada de la fase 1.

## Entorno

- Windows + PowerShell, venv en `.venv` (`.venv\Scripts\activate`), Python 3.12, pandas 3.x, geopandas 1.1.
- **Dónde viven los datos** (2-oct-2026): las rutas salen de `paths` en `config.yaml` (por defecto `data/...`, relativas al repo, así que un clon sin D: funciona igual). `config.local.yaml` (no versionado) sobrescribe solo `paths`; el modelo está en `config.local.yaml.ejemplo` (`D:/regu-data/{raw,out,footprints,export,demanda}`). En el código, usar siempre `run.ruta(cfg, "<clave>")`, nunca `ROOT / cfg["paths"][...]`.
  - En el PC de Seba **los datos viven en `D:\regu-data\`** (`raw`, `out`, `footprints`, `export`, `demanda`; unos 4,8 GB), con `config.local.yaml` activo. Migración del 2-oct-2026: copia con robocopy, 634 archivos con igual número, bytes y hash MD5; test, ficha de Temuco y `footprints estado` verificados con las carpetas originales ocultas, y después eliminadas del disco C. Cuando este archivo dice `data/out`, `data/raw`, etc., en este PC significa la carpeta equivalente en D:.
  - En el repo (livianos) quedan solo `data/base/comunas_bcn` y `data/base/comunas_final.zip`; `paths.comunas` no se movió.
- Tests: `python tests\test_sintetico.py`
- Todo trabajo ocurre en el PC personal de Seba, fuera de su empleo. No usar datos ni recursos institucionales.

## Convenciones

- Código y comentarios en español.
- **Avance de procesos largos**: `build`, `download`, `discover` y cualquier script largo escriben su avance en `<paths.out>/progreso.log` (en este PC, `D:\regu-data\out\progreso.log`), una línea por comuna o capa con el formato `HH:MM:SS <región> <comuna> i/total` (para capas: `HH:MM:SS <servicio> <capa> i/total`), y además la imprimen con `flush=True`. Se usa `etl/progreso.py`: `progreso.iniciar(path)` al partir, que reescribe el archivo, y `progreso.paso(grupo, nombre, i, total)` por unidad. Los scripts sueltos (en el scratchpad, por ejemplo) siguen la misma convención. Para seguirlo en PowerShell 5.1: `Get-Content D:\regu-data\out\progreso.log -Wait -Tail 5 -Encoding utf8` (sin `-Encoding utf8` las tildes salen como "RegiÃ³n").
- No hacer commit de `data/` (caché y salidas pesan GB). Las muestras versionables van en `samples/` y se generan con `python run.py muestra`, siempre sin datos de terceros (Overture, catastral.cl) ni de usuarios.
- **Repo remoto** (privado, 30-sep-2026): `origin` = https://github.com/SebaGeoZ92/regu-ipt-etl.git. Claude Code trabaja en `master` (`etl/`, `run.py`, `tests/`, config, `fuentes/`, `docs/`). **Codex** trabaja solo en la rama `codex/sig-layouts`, en `sig/` y `samples/`, según `AGENTS.md` y `docs/SIG_LAYOUTS.md`. No tocar `sig/` salvo para integrar un PR revisado por Seba.
- Cualquier cambio en `classify.py` debe mantener cobertura del 100% y cero traslapes en el test.
- Antes de "arreglar" una clasificación legal, preguntar: el criterio legal lo valida el arquitecto, no el código.
