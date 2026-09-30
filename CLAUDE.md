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
  - `build` genera `data/out/revision_arquitecto.csv`, una fila por zona con `revisar=True` y la columna `decision` vacía; conserva las decisiones ya llenas.
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
   - 8 zonas del PRI Lago Villarrica con `revisar=True` (Zona de vivienda, hoteleras, camping, etc.; hoy quedan en R1 por defecto). El arquitecto debe llenar `data/out/revision_arquitecto.csv` y luego se corre `importar-revision`.
4. [x] Nombres PRC/LU que no resuelven comuna: se resuelven con la cascada. Revisar `qa_sin_comuna_<tag>.csv` en cada región.
5. [ ] Escalar a nivel nacional. `discover` + `download` listos (27-sep-2026): 579 capas en el catálogo y las 539 a descargar en caché (117.030 features). Las 3 capas vacías están vacías en origen (`download --vacias`, 28-sep).
   `build` nacional (28-sep-2026): 25,4 min, 346 comunas, 74.833 piezas, cobertura 100,0% en todas, traslape relativo máx 2,1e-7 (Pedro Aguirre Cerda), 0 piezas inválidas y 2 rescates GEOS (La Pintana, Chañaral). Pendiente:
   - **17 comunas + "Zona sin demarcar" (cut 0) con `sin_urbano=True`** (ver `qa_comunas_nacional_*.csv` y la sección Portal IPT). **Huara**: el 97,8% del PRC (Pisagua, costero) queda fuera de la DPA BCN.
   - **Pérdida costera** (`qa_fuera_dpa_*.csv`, build del 28-sep-2026): 3.416 ha de IPT comunal (PRC/LU/seccional) quedan fuera de TODAS las comunas BCN, en 77 comunas, todas costeras o insulares (máximos: Caldera 466 ha, Antofagasta 312 ha, Puerto Montt 269 ha, Iquique 212 ha en 2.500 fragmentos). La causa es que la línea de costa de la BCN está generalizada. **PENDIENTE decidir** entre (a) extender cada comuna con la huella de su propio IPT fuera de la DPA (asignada por `cut_ipt`, no por cercanía) y (b) cambiar de DPA, idealmente medido antes contra los predios SII (GEOSAL).
   - **7 capas sin comuna**:
     - COM que no calzan con la BCN: Paiguano/Paihuano, Guaticas/Guaitecas, Entre Lagos (Puyehue), Llay Llay/Llaillay, Trehuaco/Treguaco, La Calera/Calera y Puerto Natales/Natales. Se resuelven con `comuna_alias`.
     - `PRI_Valparaiso/Límite Urbano` (id 6) es del Plan Metropolitano de Valparaíso, Satélite Aconcagua (San Felipe y Los Andes, 11 comunas). **PROVISORIO** (28-sep-2026, hasta que lo confirme el arquitecto): override `{tipo: PRI, pri_default: E}` en vez de LU. La zona se lee de `NOM` (`campo_zona` en config) y se separa por polígono con `zone_overrides`, ambos **PROVISORIOS**: "Límite de Extensión Urbana" (94,6 km²) → E y "Límite Urbano Vigente" (74,6 km²) → U (U3).
   - `revision_arquitecto.csv` nacional: 137 zonas. Incluye las `PRMS_Resguardo_*` tipificadas como PRM (pendiente).
6. [ ] **MVP (prioridad actual)**: `ficha()` solo con partición + afectaciones (sin condicionantes), y un mapa HTML de La Araucanía con PMTiles (clic → clase, instrumento, zona, norma_titulo, aviso) que se pueda enviar a una persona para probarlo.
7. [ ] **Fase siguiente: condicionantes territoriales** (humedales urbanos, capacidad de uso CIREN, bosque nativo CONAF, SNASPE). Especificación en `docs/CONDICIONANTES.md`. **No implementar todavía.** Empieza con su "Paso 0" (acceso, licencia y fecha de cada fuente, documentados antes de escribir código). El contrato de `ficha()` del MVP debe dejar espacio para la lista `condicionantes`.
8. [ ] Cruce con predios SII (proyecto GEOSAL de Seba, GeoParquet catastral) → endpoint pre-CIP (FastAPI + PostGIS).

## Entorno

- Windows + PowerShell, venv en `.venv` (`.venv\Scripts\activate`), Python 3.12, pandas 3.x, geopandas 1.1.
- Tests: `python tests\test_sintetico.py`
- Todo trabajo ocurre en el PC personal de Seba, fuera de su empleo. No usar datos ni recursos institucionales.

## Convenciones

- Código y comentarios en español.
- **Avance de procesos largos**: `build`, `download`, `discover` y cualquier script largo escriben su avance en `data/out/progreso.log`, una línea por comuna o capa con el formato `HH:MM:SS <región> <comuna> i/total` (para capas: `HH:MM:SS <servicio> <capa> i/total`), y además la imprimen con `flush=True`. Se usa `etl/progreso.py`: `progreso.iniciar(path)` al partir, que reescribe el archivo, y `progreso.paso(grupo, nombre, i, total)` por unidad. Los scripts sueltos (en el scratchpad, por ejemplo) siguen la misma convención. Para seguirlo en PowerShell 5.1: `Get-Content data\out\progreso.log -Wait -Tail 5 -Encoding utf8` (sin `-Encoding utf8` las tildes salen como "RegiÃ³n").
- No hacer commit de `data/` (caché y salidas pesan GB).
- Cualquier cambio en `classify.py` debe mantener cobertura del 100% y cero traslapes en el test.
- Antes de "arreglar" una clasificación legal, preguntar: el criterio legal lo valida el arquitecto, no el código.
