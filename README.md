# regu-ipt-etl

Capa nacional de **situación normativa del suelo** para regu.cl: cada metro cuadrado de Chile queda clasificado según qué instrumento (o qué ley, si no hay instrumento) manda sobre él. Es la base del informe normativo preliminar ("pre-CIP").

> Información referencial. No reemplaza el Certificado de Informaciones Previas que emite la DOM (OGUC art. 1.4.4).

## Clases

| Clase | Fuente | Prioridad |
|---|---|---|
| U1 | Plan Seccional > Plan Regulador Comunal | 1–2 |
| U2 | Límite Urbano (sin PRC) | 3 |
| U3 | Zona urbana de PRI/PRM | 4 |
| E  | Extensión urbana de PRI/PRM | 5 |
| R1 | Zona rural de PRI/PRM (+ art. 55 LGUC) | 6 |
| R2 | Rural sin IPT: art. 55 LGUC + DL 3.516 (remanente) | 7 |

El resultado es una **partición planar exacta por comuna**: sin traslapes y con 100% de cobertura (se verifica en el QA). Los textos legales por clase están en `legal_refs.json` y están en estado **borrador**: deben validarse con el arquitecto antes de publicar.

## Instalación (PC de la casa)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/Mac: source .venv/bin/activate)
pip install -r requirements.txt
```

Necesitas una capa de **comunas nacional (DPA)** en `data/base/comunas.gpkg` (BCN o IDE Chile). Ajusta en `config.yaml` los nombres de campo (`field_cut`, `field_nombre`, `field_region`). El CUT se normaliza a 5 dígitos.

## Flujo

```bash
# 1. Catálogo de servicios y capas de geoide.minvu.cl (filtrado por extensión de la región)
python run.py discover --region ARAUCANIA

# 2. REVISAR data/raw/catalogo.csv: ¿cada capa quedó con el tipo correcto?
#    Corrige con service_rules / layer_rules / overrides en config.yaml y re-aplica sin red:
python run.py catalogo

# 3. Descarga con caché (si el servidor se cae, vuelve a correr: lo bajado no se repite)
python run.py download --region ARAUCANIA

# 4. Construcción de la capa
python run.py build --region ARAUCANIA

# Nacional: lo mismo sin --region. Recarga forzada: download --refresh
# PostGIS: set DATABASE_URL=postgresql://user:pass@host/db  y  build --postgis
```

## Productos (`data/out/`)

- `regu_ipt_<ambito>_<fecha>.gpkg`: maestro, capas `capa_ipt` y `afectaciones` (riesgo, patrimonio), con `attrs_raw` (atributos originales en JSON).
- `regu_ipt_<ambito>_<fecha>.geojson`: RFC 7946, WGS84, 6 decimales. Además `por_region/`.
- `qa_comunas_<ambito>_<fecha>.csv`: por comuna, instrumentos encontrados, % de área por clase, cobertura y alerta `sin_urbano`.
- `resumen_<ambito>_<fecha>.json`.

### Campos de `capa_ipt`

`id, cut, comuna, region, clase, fuente, ipt_tipo, ipt_nombre, zona, zona_desc, revisar, area_m2, norma_titulo, norma_resumen, norma_refs, aviso, fuente_url, fecha_extraccion, attrs_raw`

## Qué revisar en el QA (lo que el código no puede saber)

1. **`sin_urbano = True` en una comuna que sí tiene PRC**: falta la capa en MINVU o quedó mal tipificada. Buscar en Portal IPT y cargar la capa a mano si es necesario.
2. **`revisar = True`**: zonas PRI/PRM cuya sigla o descripción no calzó con los patrones de `pri_subclase`. Por defecto quedan como R1. Ajusta los regex o agrega overrides.
3. **Instrumento sin comuna** (`cut_ipt` vacío en PRC/LU/Seccional): el nombre de la capa no calzó con la DPA. Aplica donde intersecta, pero conviene corregirlo.
4. **Vigencia**: MINVU publica "interpretaciones" de los planos oficiales. Cruzar fechas con Portal IPT antes de la puesta en producción.

## Reglas de diseño

- Un PRC solo norma **su** comuna: los desbordes de digitalización hacia la comuna vecina se descartan.
- Dentro de una misma fuente, en traslapes gana el polígono más pequeño (zona especial sobre zona general).
- Overlays en ESRI:102033 (Albers Sudamérica), equivalente de área de Arica a Magallanes.

## Tests

```bash
python tests/test_sintetico.py     # sin red: escenario Temuco / Padre Las Casas / Carahue + paginación ArcGIS
```
