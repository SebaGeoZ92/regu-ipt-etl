# Footprints nacionales · plan por sprints

Objetivo: tener las huellas de edificación (Overture Maps, `type=building`) de todo Chile, recortadas a las comunas, con su CUT y en una capa **separada del Atlas** por la licencia ODbL. Son la base del volumen existente, de la ficha predial y del mapa 3D.

## Idea clave: el agente escribe la herramienta una vez y Seba la corre

La descarga es mecánica. El sprint 0 lo hace un agente: escribe y prueba `run.py footprints`. Después, cada región se descarga con un comando que corre Seba **sin gastar cuota**. Los agentes solo vuelven para el QA y la integración.

## Reglas

- **Licencia:** ODbL. Los footprints van en `data/base/footprints/`, nunca dentro de `capa_ipt` ni de la base del Atlas. Atribución obligatoria: "© OpenStreetMap contributors, Overture Maps Foundation (ODbL)".
- **No se hace commit de los datos** (pesan GB). Solo el código, los tests y los manifiestos.
- **Columnas mínimas:** `id` (de Overture), `cut`, `comuna`, `region`, `height`, `num_floors`, `class`, `subtype`, `fuente` (dataset de origen dentro de Overture), `area_m2`, `geometry`.
- **Se recorta por comuna.** El bbox de una región de Chile toca Argentina y regiones vecinas; se descargan con margen y se recortan contra la DPA.
- **Un edificio que cruza un límite comunal** se asigna a la comuna donde tiene más área. No se parte.
- **Todo es reanudable:** si se corta (por un reinicio, por ejemplo), al volver a correr salta las regiones ya completas según su manifiesto.

## Sprints

| # | Tarea | Responsable | Requiere red | Estado |
|---|---|---|---|---|
| S0 | Herramienta `run.py footprints descargar --region X` + `footprints estado`, test sintético (recorte, asignación por mayor área, columnas, manifiesto) | Claude Code (o Codex: el código y los tests no necesitan red) | No para los tests | pendiente |
| S1 | La Araucanía: descargar, validar contra los 181.475 de Temuco y medir tiempo y MB | Claude Code | Sí | pendiente |
| S2 | Norte: Arica, Tarapacá, Antofagasta, Atacama y Coquimbo | **Seba** (comando en lote) | Sí | pendiente |
| S3 | Centro: Valparaíso, Metropolitana, O'Higgins, Maule y Ñuble | **Seba** | Sí | pendiente |
| S4 | Sur: Biobío, Los Ríos, Los Lagos, Aysén y Magallanes | **Seba** | Sí | pendiente |
| S5 | QA nacional: conteo y área por comuna, duplicados en bordes regionales, edificios fuera de la DPA, comunas con cobertura sospechosamente baja | Claude Code o Codex | No | pendiente |
| S6 | Integración: la ficha predial informa n.º de edificios, m² de huella y pisos estimados; el volumen usa los footprints nacionales | Claude Code | No | pendiente |
| S7 | Teselas: PMTiles de edificios por región, como capa aparte en el mapa, con atribución | Codex | No | pendiente |

### Comando para Seba (S2 a S4)

```powershell
cd C:\Users\Semar\Downloads\regu-ipt-etl\regu-ipt-etl
.venv\Scripts\activate
python run.py footprints descargar --region "ARICA|TARAPACA|ANTOFAGASTA|ATACAMA|COQUIMBO"
python run.py footprints estado
```

`estado` muestra `región | edificios | MB | release | fecha | completa`.

### Manifiesto por región

`data/base/footprints/<region>.manifest.json` lleva: `release`, `bbox`, `n_edificios`, `mb`, `fecha_descarga`, `comunas` y `completa: true/false`. Se puede hacer commit de los manifiestos en `docs/footprints/` para que los agentes sepan qué existe sin tener los datos.

## Protocolo entre agentes

1. El backlog vive en **CLAUDE.md**, en la sección "Backlog". AGENTS.md apunta a esa sección.
2. Antes de empezar una tarea, el agente la marca `en curso (<agente>, <fecha>)` y hace commit de ese cambio. Ningún agente toma una tarea que esté `en curso` por otro.
3. Al terminar, la marca `hecho (<commit>)` y deja una línea de traspaso: qué quedó, qué falta y cómo seguir.
4. **Codex no tiene red** en su entorno (solo gestores de paquetes). Toma tareas marcadas "Requiere red: No" y trabaja con muestras sintéticas o `samples/`.
5. Si un agente se queda sin cuota a mitad de camino, primero deja el estado exacto en el backlog.
6. Seba aprueba el merge de cualquier PR hacia `master`.

## Estimación

En Overture, Chile tiene varios millones de edificios. En GeoParquet, el total nacional debería rondar unos pocos GB; S1 da la medida real con La Araucanía. Antes de S2, revisa el espacio libre en disco (`Get-PSDrive C`).
