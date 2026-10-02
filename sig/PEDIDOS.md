# Entradas pendientes del piloto de tres comunas

Para Claude Code / mantenimiento del ETL, sin cambiar clasificaciones:

- Generar una muestra que incluya Loncoche, Temuco y Toltén con el contrato de `docs/SIG_LAYOUTS.md`: `capa_ipt`, `afectaciones`, `comunas`, vigencia donde exista y PMTiles regional. El producto regional de producción no trae `comunas`; no debe usarse directamente como entrada de láminas.
- El comando actual permite múltiples CUT y agrega vecinas:

```powershell
python run.py muestra --region ARAUCANIA --cut 09109,09101,09118 --nombre piloto_tres_comunas
```

- Ejecutar en el equipo con el GPKG regional y la base BCN. Revisar que `samples/` completo no supere 50 MB antes de versionar; si los excede, compartir la muestra fuera de Git, sin borrar la anterior.
- No incorporar Overture ni geometrías de la propuesta al Atlas. El informe y descargador del piloto viven en `sig/piloto.py`.
- Recuperar o identificar planos/versiones del PRC propuesto de Toltén. La situación administrativa que refiere el usuario no se ha verificado; se documenta como antecedente, no como vigencia.
