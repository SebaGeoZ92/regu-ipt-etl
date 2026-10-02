# Volúmenes y capacidad de ocupación · especificación

Extiende `docs/VOLUMEN_PILOTO.md`. Para cada predio (y agregado por manzana, zona y comuna) se calculan cuatro volúmenes y los índices que los comparan. Cada valor lleva su **fuente y confianza**: nunca se muestra un número sin decir de dónde sale.

## Los cuatro volúmenes

| Volumen | Pregunta que responde | Cómo se obtiene | Depende de |
|---|---|---|---|
| **Máximo** (V_max) | ¿Cuál es el techo teórico de la norma? | Superficie de ocupación permitida × altura máxima. Cota superior simple, sin rasantes ni constructibilidad | Normas de la zona |
| **Óptimo** (V_opt) | ¿Cuánto se puede construir de verdad cumpliendo todo a la vez? (la *cabida*) | Envolvente que cumple ocupación, constructibilidad, altura, rasantes, distanciamientos, antejardín, agrupamiento y densidad, con pisos enteros, altura de piso de referencia y la forma real del predio (se descartan franjas inútiles por ancho mínimo) | Normas validadas por el arquitecto; geometría del predio |
| **Calculado** (V_calc) | ¿Cuánto hay construido según los registros? | Huella × pisos estimados. Pisos = superficie construida SII / área de huella (o `num_floors` de Overture si existe) | Footprints + SII (catastral.cl) |
| **Real** (V_real) | ¿Cuánto hay construido según la medición? | Huella × altura medida con datos Z: nDSM = DSM − DTM (LiDAR o fotogrametría), `height` de Overture, o el modelo de terreno de Living DEM | Datos Z de calidad |

`V_max ≥ V_opt` siempre. Si el cálculo da lo contrario, hay un error de datos y se marca.

## Índices

- **Ocupación del volumen:** `IOV = V_existente / V_opt`, donde `V_existente = V_real` si existe y si no `V_calc`.
  - `< 0,8`: holgura (se puede ampliar).
  - `0,8 – 1,0`: al límite.
  - `> 1,0`: **excede la norma** (caso de regularización).
- **Potencial remanente:** `V_opt − V_existente`, en m³ y en m² equivalentes (dividido por la altura de piso de referencia).
- **Eficiencia normativa:** `V_opt / V_max`. Cuánto del techo teórico es aprovechable en ese predio. Los predios angostos o irregulares dan valores bajos.
- **Brecha de registro:** `V_real − V_calc`. Si lo medido supera a lo declarado, probablemente hay ampliaciones no registradas.

## Confianza

Cada volumen lleva `fuente` y `confianza`:
- Normas: `FICTICIO | BORRADOR | VALIDADO`. Al público solo se muestra `VALIDADO`.
- Pisos o altura: `sii` | `overture_num_floors` | `overture_height` | `ndsm_lidar` | `ndsm_fotogrametria` | `estimado`.
- Huella: `overture` (con su dataset de origen) | `osm`.

## Etapas

1. **Ahora, sin datos nuevos: ocupación real del suelo por zona.** `Σ área de huellas / área de la zona` para cada zona de cada PRC, a nivel nacional, con los footprints que ya están en D:. Es el **coeficiente de ocupación existente** y se compara con el normativo cuando haya tabla de normas. Ya sirve para ver dónde está densificada una ciudad y dónde no.
2. **V_calc por predio** donde haya datos SII (piloto Temuco con catastral.cl, cuidando la cuota).
3. **V_max y V_opt** en la zona piloto, cuando Mario llene `normas_zona.csv`. V_opt en fase 1 sin rasantes (declarado); las rasantes en fase 2, con geometría 3D.
4. **V_real** cuando haya datos Z: primero `height` de Overture donde exista; luego nDSM con LiDAR o fotogrametría (revisar disponibilidad para Temuco y conectar con Living DEM).

## Salidas

- Por predio: `rol, cut, zona, v_max, v_opt, v_calc, v_real, iov, remanente_m2, eficiencia, brecha_registro`, más `fuente_*` y `confianza_*` de cada valor.
- **Agregados** por manzana, zona y comuna: medianas, percentiles y % de predios que exceden. Los agregados se pueden publicar. Los datos por predio solo se muestran a quien consulta **su** predio.
- Mapa: color por IOV y extrusión 3D de V_existente (sólido) y V_opt (translúcido).

## Reglas

- Las normas no se inventan.
- La brecha de registro nunca se publica por predio ni se usa para prospección. Solo se muestra a quien consulta su propio predio, o en agregados.
- Cada número va con su fuente y su confianza.
- Siempre se muestra el aviso de que es información referencial y no reemplaza un anteproyecto, un permiso ni el CIP.
