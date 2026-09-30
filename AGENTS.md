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

## Codex: layouts SIG

- Trabaja **solo** en la rama `codex/sig-layouts`, creada desde `master`. No hagas push a `master` ni lo rebasees.
- Toca **solo** `sig/` (lo que produzcas) y `samples/` (si necesitas otra muestra, documenta en su README cómo se
  generó; los datos se regeneran con `python run.py muestra`, que es de Claude Code).
- Sigue la especificación de **`docs/SIG_LAYOUTS.md`**. **PENDIENTE (30-sep-2026): ese archivo todavía no está
  en el repo.** No empieces a producir layouts hasta que exista en `master`; mientras tanto, solo explora las
  muestras.
- Datos de trabajo: `samples/temuco/muestra.gpkg` (capas `capa_ipt`, `afectaciones`, `comunas`, EPSG:4326) y
  `samples/temuco/capa_ipt_araucania.pmtiles`. Campos y clases: ver "Clases" y "Arquitectura" en `CLAUDE.md`.
- Todo layout muestra el aviso de información referencial y cita las fuentes (IDE MINVU, Portal IPT MINVU,
  BCN). Colores de clase: los de `etl/mapa.py` (`COLORES`), para que mapa y layouts coincidan.
- Si necesitas un cambio en `etl/` o en los datos de la muestra, **no lo hagas**: descríbelo en el PR o en
  `sig/PEDIDOS.md` y lo resuelve Claude Code en `master`.
- La integración a `master` es por Pull Request que revisa Seba.
