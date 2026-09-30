# Condicionantes territoriales · especificación para el agente

Segunda capa del Atlas Normativo. La **partición** (U1…R2) responde *qué instrumento manda*. Las **condicionantes** responden *qué más pesa sobre ese suelo*: ambiente, suelo físico, bosque. Nunca entran a la partición. Se superponen y se cruzan con el predio al consultar.

Producto de cara al usuario: **Regu Suelo**, botón **"¿Qué puedo construir?"**.
Producto de datos: **Atlas Normativo** (partición + afectaciones + condicionantes).

## Principios

1. **Cada condicionante declara su peso legal.** Hay tres niveles:
   - `restriccion`: tiene efecto legal directo (por ejemplo, humedal urbano declarado).
   - `alerta`: puede exigir un trámite adicional (plan de manejo CONAF, informe SAG).
   - `informativo`: contexto útil sin efecto directo.
2. **Escala honesta.** Cada fuente guarda su escala y su fecha. Una capa regional nunca se presenta como dato predial: el texto al usuario dice "posible" o "verificar".
3. **Licencia por fuente.** Se registra la licencia de cada capa. Si no es abierta o no está clara, no se redistribuye; solo se usa para la consulta.
4. **Mismo patrón que la partición:** descarga con caché → normalización → recorte por comuna con `cut` → GPKG. Se reusan `etl/arcgis.py` (si la fuente es ArcGIS REST), `ComunaResolver` y `recortar_afectaciones`.
5. **Criterio legal:** los textos de `efecto` quedan en BORRADOR hasta que los valide el arquitecto.

## Fuentes (en este orden)

> **Migrado (29-sep-2026) al formato de contrato de `docs/FUENTES_BAJO_DEMANDA.md`.** Cada fuente se declara
> en `fuentes/<id>.yaml` (acceso, licencia, diccionario, mapeo, `excluir_siempre`, condicionante y demanda),
> validado contra `fuentes/_esquema.json`. Esta tabla queda como índice; el contrato manda.

| # | Condicionante | Contrato | Nivel | Estado |
|---|---|---|---|---|
| 1 | Humedales urbanos declarados (Ley 21.202) | `fuentes/mma_humedales_urbanos.yaml` | restriccion | propuesta |
| 2 | Capacidad de uso y series de suelo (CIREN) | `fuentes/ciren_capacidad_uso.yaml` | alerta en R1/R2/E, informativo en U | propuesta |
| 3 | Bosque nativo (catastro CONAF) | `fuentes/conaf_bosque_nativo.yaml` | alerta | propuesta |
| 4 | Áreas silvestres protegidas (SNASPE), IDE MINAGRI | **sin contrato todavía**: no está entre las seis candidatas de FUENTES_BAJO_DEMANDA.md (pendiente decidir si se agrega) | restriccion | — |

Además, FUENTES_BAJO_DEMANDA.md agrega `conadi_tierras_indigenas`, `conadi_adi` y `sag_subdivisiones`.

**Paso 0 para el agente:** encontrar el acceso real de cada fuente (ArcGIS REST, WFS, shapefile descargable), su licencia y su fecha. Documentarlo en CLAUDE.md bajo "Hechos verificados" **antes** de escribir código. Si una fuente no tiene descarga automatizable, dejar el procedimiento manual documentado y seguir con la siguiente.

## Configuración

**Reemplazado por los contratos en `fuentes/<id>.yaml`** (una sola forma de declarar fuentes). El bloque que
sigue era la propuesta original para `config.yaml` y se conserva solo como referencia:

```yaml
condicionantes:
  humedales_urbanos:
    nivel: restriccion
    acceso: {tipo: arcgis|wfs|archivo, url: ..., capa: ...}
    campos: {nombre: ..., estado: ..., fecha: ...}
    licencia: ...
    escala: ...
    efecto: "BORRADOR: Humedal urbano declarado (Ley 21.202). Puede postergar permisos de subdivisión, loteo y construcción; aplica la ordenanza municipal de humedales."
  capacidad_uso: ...
  bosque_nativo:
    filtro: "<expresión sobre el campo de uso/subuso que deja solo bosque nativo>"
  snaspe: ...
```

## Código

- La descarga e integración de cada fuente irá por `run.py fuentes activar <id>` (FUENTES_BAJO_DEMANDA.md), según su `mapeo`. Hoy existen `etl/fuentes.py` (contratos, relevancia, registro de demanda) y `run.py fuentes estado|validar`; **ninguna fuente está activa**.
- `etl/condicionantes.py`: `descargar(nombre)`, `normalizar(nombre) -> GeoDataFrame` con columnas estándar `condicionante, nivel, nombre, detalle, fecha_fuente, escala, licencia, fuente_url, attrs_raw, geometry`, y `recortar(gdf, comunas)` para asignar `cut` y `comuna`.
- `run.py condicionantes [--region X] [--solo nombre]`: descarga, normaliza y escribe la capa `condicionantes` en el mismo GPKG, más `qa_condicionantes_<tag>.csv` (por comuna: cobertura % de cada condicionante y marca `sin_cobertura` donde la fuente no llega).
- `etl/ficha.py`: **`ficha(geom_4326) -> dict`**, el germen del pre-CIP. Recibe un polígono (predio) o un punto y devuelve:
  ```json
  {
    "comuna": "...", "cut": "...",
    "particion": [{"clase": "R2", "pct": 100.0, "ipt": null, "zona": null, "norma_titulo": "..."}],
    "riesgo_pct": 0.0,
    "afectaciones": [...],
    "condicionantes": [{"condicionante": "capacidad_uso", "nivel": "alerta", "detalle": "Clase III", "pct": 72.4, "escala": "...", "fecha_fuente": "..."}],
    "aviso": "Información referencial. No reemplaza el CIP que emite la DOM (OGUC art. 1.4.4)."
  }
  ```
  Debe funcionar leyendo el GPKG (con índice espacial). Después se conecta a PostGIS y FastAPI sin cambiar el contrato.
- `run.py ficha --lon X --lat Y` o `--wkt "..."`: imprime la ficha en JSON. Sirve para probar a mano.

## Tests (se agregan a `tests/test_sintetico.py`)

- Una condicionante sintética de cada nivel en el escenario Temuco / Padre Las Casas / Carahue.
- `ficha()` sobre un predio que cruza U1 y R2 y un humedal: los porcentajes de la partición suman 100 ± 0,01 y la condicionante reporta su % correcto.
- `ficha()` sobre un punto devuelve una sola clase.
- Una condicionante **nunca** altera la partición: el hash de `capa_ipt` es idéntico con y sin condicionantes.

## Convenciones

- Avance en `data/out/progreso.log` (ya es convención).
- Un commit por fuente integrada y otro por `ficha`.
- Piloto en La Araucanía primero; nacional después.
- Actualizar CLAUDE.md: la sección de arquitectura, los hechos verificados de cada fuente y el estado.
