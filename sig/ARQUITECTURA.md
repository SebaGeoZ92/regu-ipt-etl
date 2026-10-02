# Arquitectura del SIG · primera aproximación

Objetivo: que un usuario SIG como Elías pueda cargar sus capas, revisar atributos y fuentes, seleccionar elementos, ajustar simbología y exportar una salida cartográfica. La lámina comunal ya ofrece una primera salida evaluable; el espacio de trabajo conecta ahora los componentes existentes.

## Separación de responsabilidades

```text
Interfaz de proyectos, capas, atributos y salidas
                  │ API de operaciones geográficas
                  ▼
Servicio Python (sig/servidor.py)
    ├── Lectura de capas y catálogo (GeoPandas / GDAL)
    ├── Consulta normativa (etl.ficha, sin duplicar reglas)
    └── Láminas vectoriales (sig.render / ReportLab)
                  │
     Muestra GPKG + PMTiles + contratos del ETL
```

Python concentra lectura, análisis y composición para reutilizarlos desde una web, un cliente de escritorio o procesos por lote. La interfaz usa JavaScript; Python por sí solo no determina la capacidad de competir con ArcGIS o QGIS. El rendimiento, la interoperabilidad, el etiquetado y la facilidad de edición deberán medirse con usuarios y datos reales.

## Implementado en este incremento

- Aplicación **local**, limitada al equipo donde se ejecuta; sin cuentas ni publicación.
- Catálogo de las siete comunas de la muestra, partición normativa, límite comunal y superposición de riesgo. Las demás afectaciones se detallan al consultar.
- Consulta puntual que reutiliza `etl.ficha.ficha`, sin registrar coordenadas ni solicitudes en disco.
- Descarga de la lámina comunal A3 con Verdana incrustada.
- Navegación y escala gráfica con Leaflet 1.9.4, distribuido localmente con su licencia BSD. Geometrías SVG; no requiere un mapa base externo. La vista es exploratoria: no reproduce los achurados ni la escala de impresión del PDF.
- Verdana para interfaz y etiquetas, usando la instalación del equipo; el servidor PDF exige archivos originales. No se distribuyen archivos de fuente con el repositorio.

Leaflet permite validar este primer recorrido sin infraestructura externa. Para volúmenes regionales/nacionales, se evaluará MapLibre + PMTiles, ya usado por el mapa del ETL. El contrato de datos y el análisis Python quedan separados del componente de visualización para permitir ese cambio.

## Siguiente recorrido a construir

1. **Proyecto y capas propias:** importar GeoPackage/GeoJSON, identificar CRS, límites y campos; guardar referencias y estilos sin duplicar datos. La carga de archivos requiere límites de tamaño, validación y aislamiento antes de abrir acceso a varios usuarios.
2. **Atributos y selección:** tabla vinculada al mapa; filtros tipados por campo, selección persistente y exportación de seleccionados. No ejecutar expresiones Python o SQL arbitrarias del usuario.
3. **Simbología y etiquetas:** color único, categorías y rangos; nombres de localidades con jerarquía, halo, tamaño, colisiones y Verdana. Las etiquetas deben evaluarse a escala real, con nombres proporcionados por las capas.
4. **Fuentes oficiales:** catálogo explícito de servicios MINVU con procedencia, fecha, CRS, capacidades y caché. Hoy se usa la muestra procesada; no hay conexión en vivo desde esta interfaz ni actualización automática.
5. **Composición:** un documento de proyecto compartido por visor y exportador, con extensión, orden de capas, estilos, etiquetas y plantilla. Hoy el PDF es comunal y no refleja cambios de visibilidad o encuadre hechos en pantalla.
6. **Análisis ampliable:** operaciones Python con entradas y salidas explícitas, ejecución fuera de la interfaz, progreso, cancelación e historial reproducible.

## Evaluación con Elías

Probar con una comuna conocida: encontrar un área, consultar normativa y procedencia, activar y desactivar riesgos y obtener la lámina. Registrar por tarea si pudo completarla y qué información faltó. La opinión favorable sobre la lámina no se interpreta como validación del SIG completo.

Antes de publicar para usuarios remotos: autenticación, aislamiento de proyectos, límites de recursos, cola de exportaciones y política de conservación de datos. Por ahora la dirección de escucha se mantiene local.
