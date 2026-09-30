# Muestra Cholchol, Galvarino, Lautaro, Nueva Imperial, Padre Las Casas, Temuco, Vilcún

Muestra liviana y versionable del Atlas Normativo, generada con `python run.py muestra` el 2026-09-30
desde `regu_ipt_araucania_20260929.gpkg` (build de ARAUCANIA). Sirve para trabajar sin `data/` (que no se versiona).

- `muestra.gpkg`: capas `capa_ipt` (partición U1…R2), `afectaciones` y `comunas`, en EPSG:4326, recortadas a:
  Cholchol, Galvarino, Lautaro, Nueva Imperial, Padre Las Casas, Temuco, Vilcún. Sin `attrs_raw`.
- `capa_ipt_araucania.pmtiles`: la partición de toda la región en PMTiles (lo que usa el mapa).

Fuentes: IDE MINVU (geoide.minvu.cl), Portal IPT MINVU y División comunal BCN. **No** incluye datos de
catastral.cl, Overture Maps ni registros de demanda.

**Información referencial.** No reemplaza el Certificado de Informaciones Previas (CIP), que emite solo la
Dirección de Obras Municipales (OGUC art. 1.4.4). `legal_refs.json` está en BORRADOR.
