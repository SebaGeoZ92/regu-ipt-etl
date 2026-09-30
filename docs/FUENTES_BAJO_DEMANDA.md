# Fuentes bajo demanda · especificación para el agente

Idea heredada de *Universo extendido*: el mundo no se construye completo de una vez. Crece por parches validados que se integran al mundo vivo. Aplicada al Atlas Normativo: **tenemos la estructura para recibir cualquier fuente, y cada fuente se activa cuando alguien nos da su contrato y el público la pide**.

## TL;DR

1. Cada fuente externa (CONADI, SAG, CIREN, MMA…) tiene un **contrato de datos** en `fuentes/<id>.yaml`: acceso, licencia, diccionario de campos y **qué columnas tomamos**.
2. Una fuente pasa por varios estados: `propuesta → contrato → mapeada → latente → activa → mantenida`.
3. La **demanda** se mide: cada consulta de la ficha que tocaría una fuente no activa suma un voto por región. Al cruzar el umbral, `run.py fuentes activar` descarga e integra.
4. **Nunca tomamos más columnas de las que el producto necesita.** Los datos personales no entran.
5. El "alguien que nos pasa la estructura" puede ser un funcionario, un usuario experto o una **solicitud de Ley de Transparencia**.

## Estados

| Estado | Qué significa | Qué falta para avanzar |
|---|---|---|
| `propuesta` | Sabemos que la fuente existe y para qué serviría | Conseguir estructura y diccionario |
| `contrato` | Tenemos campos, tipos, dominios (diccionario) y licencia | Decidir qué columnas tomamos y a qué estándar las llevamos |
| `mapeada` | Mapeo columna→estándar escrito y probado con una muestra | Acceso automatizable o procedimiento manual documentado |
| `latente` | Todo listo; se puede descargar con un comando | Demanda ≥ umbral, o decisión manual |
| `activa` | Descargada, normalizada y visible en la ficha | — |
| `mantenida` | Tiene frecuencia de refresco y QA de cambios | — |

## Contrato de datos (`fuentes/<id>.yaml`)

```yaml
id: conadi_tierras_indigenas
nombre: Tierras indígenas (títulos de merced, compras Fondo de Tierras, ADI)
institucion: CONADI
estado: propuesta
responsable_contacto: null          # quién nos pasó la estructura (institución/rol; sin datos personales si no autoriza)
obtenido_via: null                   # funcionario | usuario | transparencia | publicación
acceso:
  tipo: null                         # arcgis | wfs | archivo | api | manual
  url: null
  frecuencia_refresco: null
licencia: null                       # sin licencia clara → solo consulta, no redistribución
escala: null
fecha_datos: null
diccionario:                         # tal como lo entrega la fuente
  - {campo: ..., tipo: ..., descripcion: ..., dominio: ...}
mapeo:                               # SOLO lo que el producto necesita
  - {origen: ..., destino: condicionante_detalle, transformacion: ...}
excluir_siempre: [RUT, NOMBRE, NOMBRE_TITULAR, DIRECCION, TELEFONO]   # datos personales
condicionante:
  nivel: restriccion                 # restriccion | alerta | informativo
  efecto: "BORRADOR: ..."            # lo valida el arquitecto (y un abogado si corresponde)
demanda:
  preguntas_que_responde: ["¿Mi terreno es tierra indígena?", "¿Puedo comprar/subdividir este predio?"]
  umbral_activacion: 25              # consultas por región antes de activar
notas_eticas: >
  Información sensible para comunidades mapuche. Se publica solo el hecho territorial
  (el polígono es tierra indígena y qué ley aplica), nunca los titulares. Antes de publicar,
  revisarlo con el arquitecto y, idealmente, con alguien vinculado a CONADI o a comunidades.
```

## Medición de demanda

- `ficha()` registra en `data/demanda/consultas.jsonl` una línea por consulta: `fecha, region, comuna, clase, fuentes_relevantes_no_activas[]`. **Sin coordenadas exactas**: se guarda la comuna, por privacidad del usuario.
- La relevancia sale de reglas simples en cada contrato. Por ejemplo, `conadi_tierras_indigenas` es relevante si la clase es R1 o R2 y la región está entre Biobío y Los Lagos.
- La web también puede registrar preguntas libres ("¿mi terreno es indígena?") con la etiqueta de la fuente que las respondería.
- `run.py fuentes estado`: tabla `fuente | estado | votos por región | umbral | ¿lista para activar?`.
- `run.py fuentes activar <id> [--region X]`: descarga, normaliza según `mapeo`, escribe en `condicionantes` y hace commit del cambio de estado en el YAML.

## Protocolo de contribución (el "parche" de Universo extendido)

Un aporte externo es **un contrato + una muestra**, nunca una base completa:

1. El colaborador (funcionario, usuario experto) entrega: diccionario de campos, licencia o condiciones de uso, y una muestra de 10 a 50 registros **anonimizada**.
2. Nosotros escribimos el `mapeo` y un test con la muestra (`tests/fuentes/test_<id>.py`).
3. Si el test pasa, la fuente queda `mapeada` o `latente`. Se agradece en `fuentes/CREDITOS.md` si el colaborador lo autoriza.

## Solicitud por Ley de Transparencia (plantilla)

Cuando no hay contacto, la Ley 20.285 obliga a los organismos a responder solicitudes de información pública. Lo que se pide es **la estructura, no los datos**:

> Solicito, en formato digital: (1) el diccionario de datos o la descripción de campos de la capa/base [nombre], incluyendo nombre, tipo, descripción y dominios de valores de cada campo; (2) la fecha de la última actualización y la frecuencia de mantención; (3) las condiciones de uso o la licencia aplicable a su reutilización; y (4) si existe, el servicio web (WMS/WFS/ArcGIS REST) por el cual se publica. No solicito datos personales.

Se registra en el contrato con `obtenido_via: transparencia` y el número de la solicitud.

## Primeras fuentes candidatas

| id | Institución | Por qué | Estado inicial |
|---|---|---|---|
| `conadi_tierras_indigenas` | CONADI | Ley 19.253: restricciones de enajenación y de subdivisión. Crítico en La Araucanía y en el segmento R2 | propuesta |
| `conadi_adi` | CONADI | Áreas de Desarrollo Indígena: contexto territorial | propuesta |
| `sag_subdivisiones` | SAG | Certificados DL 3.516: historial de subdivisión rural | propuesta |
| `ciren_capacidad_uso` | CIREN / IDE MINAGRI | Ya especificada en CONDICIONANTES.md | propuesta |
| `conaf_bosque_nativo` | CONAF / IDE MINAGRI | Ya especificada en CONDICIONANTES.md | propuesta |
| `mma_humedales_urbanos` | MMA | Ya especificada en CONDICIONANTES.md | propuesta |

Las de CONDICIONANTES.md se migran a este formato de contrato: una sola forma de declarar fuentes.

## Código

- `fuentes/` con un YAML por fuente, más `fuentes/_esquema.json` (JSON Schema del contrato) para validar.
- `etl/fuentes.py`: `cargar_contratos()`, `validar(contrato)`, `relevantes(ficha)`, `registrar_demanda(ficha)`, `activar(id, region)`.
- `run.py fuentes {estado|validar|activar}`.
- Test: un contrato de ejemplo con una muestra sintética recorre `mapeada → latente → activa`, y **las columnas de `excluir_siempre` nunca llegan a la salida**.

## Convenciones

- Una fuente, un commit, un YAML.
- El `efecto` legal de cada fuente queda en BORRADOR hasta que lo valide el arquitecto.
- Avance en `progreso.log`.
