# regu-ipt-etl · instrucciones para agentes (Codex y otros)

**La fuente de verdad del proyecto es `CLAUDE.md`**: objetivo, clases U1…R2, decisiones de modelado, hechos
verificados del servidor MINVU, estado y convenciones. Léelo completo antes de trabajar. Este archivo solo agrega
el reparto de trabajo entre agentes y las reglas que no se pueden romper.

## Reglas duras (resumen de CLAUDE.md)

- El CIP lo emite solo la DOM (OGUC art. 1.4.4). Regu nunca "entrega" un CIP: todo producto lleva el aviso de
  información referencial.
- El criterio legal lo valida el arquitecto, no el código. `legal_refs.json` y los efectos de `fuentes/*.yaml`
  están en BORRADOR. Las normas de zona (`normas_zona.csv`) **nunca se inventan**.
- `data/` no se versiona. Nada de datos de terceros ni de usuarios en el repo: ni catastral.cl, ni Overture Maps
  (ODbL, capa aparte), ni `data/demanda`, ni claves (la de catastral.cl va en la variable `CATASTRAL_API_KEY`).
- Privacidad: sin nombres de propietarios, RUT ni coordenadas de consultas.
- Código, comentarios y textos en español. `python tests\test_sintetico.py` debe pasar siempre.

## Reparto de trabajo

| Quién | Rama | Puede tocar | No toca |
|---|---|---|---|
| **Claude Code** | `master` | `etl/`, `run.py`, `tests/`, `config.yaml`, `fuentes/`, `legal_refs.json`, `docs/`, `CLAUDE.md`, `AGENTS.md` | `sig/` (salvo para integrar un PR revisado) |
| **Codex** | `codex/sig-layouts` | **solo** `sig/` y `samples/` | `master`, `etl/` y todo lo demás |

## Protocolo entre agentes

1. El backlog vive en **`CLAUDE.md`, sección "Backlog"**. Léelo antes de elegir trabajo.
2. Antes de empezar una tarea, márcala `en curso (<agente>, <fecha>)` y haz commit de ese cambio. **No tomes una
   tarea que esté `en curso` por otro agente.**
3. Al terminar, márcala `hecho (<commit>)` y deja una línea de traspaso: qué quedó, qué falta y cómo seguir.
4. **Codex no tiene red** en su entorno (solo gestores de paquetes): toma tareas con "Requiere red: No" y trabaja
   con muestras sintéticas o con `samples/`. Las tareas que descargan datos (por ejemplo, los footprints por
   región) las hace Claude Code o las corre Seba con el comando documentado.
5. Si te quedas sin cuota a mitad de camino, **primero** deja el estado exacto en el backlog.
6. Seba aprueba el merge de cualquier PR hacia `master`.
7. Antes de empezar, `git fetch origin` y `git pull`: otro agente puede haber marcado tareas o avanzado la rama.

## Codex: layouts SIG

- Trabaja **solo** en la rama `codex/sig-layouts`, creada desde `master`. No hagas push a `master` ni lo rebasees.
- Toca **solo** `sig/` (lo que produzcas) y `samples/` (si necesitas otra muestra, documenta en su README cómo se
  generó; los datos se regeneran con `python run.py muestra`, que es de Claude Code).
- Sigue la especificación de **`docs/SIG_LAYOUTS.md`** (primer entregable: "Lámina normativa comunal"; segundo:
  "Ficha predial" con un polígono sintético). La muestra cumple su contrato de entrada, incluida la vigencia
  (`ipt_norma`, `ipt_fecha`, `ipt_ultmod`, `ord_url`) donde existe.
- Datos de trabajo: `samples/temuco/muestra.gpkg` (capas `capa_ipt`, `afectaciones`, `comunas`, EPSG:4326) y
  `samples/temuco/capa_ipt_araucania.pmtiles`. Campos y clases: ver "Clases" y "Arquitectura" en `CLAUDE.md`.
- Todo layout muestra el aviso de información referencial y cita las fuentes (IDE MINVU, Portal IPT MINVU,
  BCN). Colores de clase: los de `etl/mapa.py` (`COLORES`), para que mapa y layouts coincidan.
- Si necesitas un cambio en `etl/` o en los datos de la muestra, **no lo hagas**: descríbelo en el PR o en
  `sig/PEDIDOS.md` y lo resuelve Claude Code en `master`.
- La integración a `master` es por Pull Request que revisa Seba.
