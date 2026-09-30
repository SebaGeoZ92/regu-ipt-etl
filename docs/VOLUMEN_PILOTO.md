# Piloto de volumen · especificación para el agente

Objetivo: para 10 predios reales de **una zona del PRC de Temuco**, calcular y mostrar en 3D:

- **Volumen existente:** lo construido hoy (huella × pisos estimados).
- **Volumen posible:** la envolvente máxima que permite la norma de la zona.
- **Remanente:** cuántos m² más se podrían construir, o cuánto excede lo existente (caso de regularización).

Siempre es una **estimación referencial**. No es un anteproyecto ni un permiso.

## Reglas duras

1. **Las normas NO se inventan.** Los valores reales los llena el arquitecto (Mario) en `data/base/normas_zona.csv`. Para desarrollo y tests se usan valores marcados `FICTICIO`, que nunca se muestran al público.
2. **Footprints en capa aparte.** Overture Maps es ODbL: va a `data/base/footprints/`, con atribución, y nunca se fusiona dentro de `capa_ipt` ni del Atlas.
3. **Las claves de API no van al repo.** La clave de catastral.cl va en la variable de entorno `CATASTRAL_API_KEY`.
4. **Privacidad:** no se guardan ni se muestran nombres de propietarios. La comparación entre lo existente y lo declarado es para quien consulta **su** predio; no se generan listados de predios con "posible construcción irregular".
5. **Los cálculos se hacen con la geometría original.** La simplificada solo se usa para mostrar.

## Paso 0 · Ayudar a Mario a elegir la zona

`run.py volumen candidatas --ipt "Temuco"`: tabla de zonas del PRC de Temuco con `zona | ha | n_predios (si hay datos) | n_footprints | ¿ordenanza en Portal IPT?`. Mario elige una zona que conozca bien, idealmente residencial con variedad de predios.

Luego genera `data/base/normas_zona.csv` con una fila para esa zona y las columnas vacías:

```
ipt_nombre, zona, ocupacion_max, constructibilidad_max, altura_max_pisos, altura_max_m,
altura_piso_ref_m, antejardin_m, distanciamiento_m, agrupamiento, rasante_grados,
densidad_max, articulo_fuente, validado_por, fecha_validacion, estado
```

`estado`: `FICTICIO` | `BORRADOR` | `VALIDADO`. Solo `VALIDADO` se muestra al público.

## Datos de entrada

- **Predios:** primero, buscar si GEOSAL tiene en local el GeoParquet catastral de Temuco (preguntarle la ruta a Seba). Si no, usar la API gratuita de catastral.cl (100 consultas/día; alcanza para 10 predios). Campos necesarios: `rol`, superficie de terreno, superficie construida, destino y geometría. Solo lo necesario.
- **Footprints:** Overture Maps, tipo `building`, con bbox de la zona elegida y algo de margen (`pip install overturemaps`; `overturemaps download --bbox=... -f geoparquet --type=building`). Guardar la fecha del release y la atribución.

## Cálculo (`etl/volumen.py`)

**Volumen existente, por predio:**
- Footprints que caen en más del 50% dentro del predio: `area_huella` = suma.
- `pisos_est = max(1, round(sup_construida_SII / area_huella))`. Si el SII no tiene superficie construida, se marca `sin_dato`.
- `altura_est = pisos_est × altura_piso_ref_m`.
- Si existe altura de Overture (`height`), se usa y se marca la fuente.

**Volumen posible (envolvente simplificada, fase 1):**
- `base` = predio reducido hacia adentro por `max(antejardin_m, distanciamiento_m)`. **Simplificación declarada:** retranqueo uniforme, sin distinguir frente y deslindes. En la fase 2 se detecta el frente como el lado que da a la calle.
- `area_primer_piso = min(area(base), ocupacion_max × sup_terreno)`.
- `pisos_max = altura_max_pisos` (o `floor(altura_max_m / altura_piso_ref_m)`).
- `m2_max = min(area_primer_piso × pisos_max, constructibilidad_max × sup_terreno)`.
- Sólido para mostrar: `base` extruida a `pisos_max × altura_piso_ref_m`.
- **Rasantes: fase 2**, con geometría 3D real (Three.js o CesiumJS). En la fase 1 la ficha lo dice explícitamente.

**Resultado por predio:** `rol, zona, sup_terreno, m2_existente, pisos_est, m2_max, pisos_max, m2_remanente = m2_max − m2_existente, estado ∈ {holgura, al_limite, excede, sin_dato}, normas_estado, simplificaciones[]`.

## Salidas

- `data/out/volumen_piloto.gpkg` con las capas `predios`, `existente` (huellas con `altura_est`) y `posible` (bases con `altura_max`).
- GeoJSON para el mapa (propiedades mínimas).
- **Mapa:** un toggle "Volumen (piloto)" que inclina la vista y usa `fill-extrusion` de MapLibre. Lo existente va sólido, lo posible translúcido y con borde. Al hacer clic en un predio se ve el remanente. Se publica **solo** si hay normas `VALIDADO`; con `FICTICIO`, se genera localmente para revisión.
- **Ficha:** una sección `volumen` que aparece solo cuando la zona tiene normas `VALIDADO`.

## Tests

- Predio rectangular sintético de 20×30 m con normas ficticias. Verificar `m2_max` a mano en tres casos: limita la ocupación, limita la constructibilidad y limita la altura.
- Un caso `excede`: lo existente supera lo posible.
- Un footprint compartido entre dos predios solo cuenta donde tiene más del 50%.
- `FICTICIO` nunca aparece en la ficha pública.

## Orden

Después de terminar la cadena actual (build, vigencia, mapa) y los contratos de FUENTES_BAJO_DEMANDA. Commit por paso. Actualizar CLAUDE.md.
