# Piloto: Loncoche, Temuco y Toltén

Alcance acordado: ampliar el estudio de huellas de edificaciones a las tres comunas. El archivo `pilotos/araucania.json` registra CUT 09109 (Loncoche), 09101 (Temuco) y 09118 (Toltén). El caso de Toltén incorpora un escenario de propuesta de PRC, separado de la normativa base.

## Estado real de las entradas

- La muestra versionada incluye Temuco, pero **no Loncoche ni Toltén**. El visor indica esas ausencias.
- El usuario informó que subió el paquete nacional/regional a Drive; el enlace aún no está disponible en esta conversación y la búsqueda en la conexión no encontró el paquete. No se han procesado sus datos.
- El usuario trabajó anteriormente en el PRC propuesto de Toltén. No tiene ahora disponible el archivo en Drive. La referencia a Contraloría desde 2012 es un antecedente del usuario; no se ha verificado el expediente ni su estado actual. No se asigna esa fecha a una geometría o versión documental desconocida.
- Todavía no se han descargado ni contado huellas reales para Loncoche y Toltén en este entorno.

## Inventario y footprints

Funciona con el GPKG de producción: solo requiere `capa_ipt` con CUT, clase y CRS. No exige `comunas` ni asume que el nombre del PRC coincide con el nombre de la comuna. Incluye todas las clases del producto; es importante para no excluir sectores rurales o áreas de una propuesta fuera del PRC actual.

```powershell
python -m sig.piloto --gpkg data/out/regu_ipt_araucania_20260929.gpkg --salida data/out/piloto_tres_comunas.json
```

El inventario describe disponibilidad, piezas, clases y bbox por CUT. No es todavía el cruce con edificaciones. Para descargar, fijar una misma versión de Overture disponible para las tres comunas (el ejemplo conserva la versión del piloto original, no afirma que sea la última):

```powershell
python -m sig.piloto --gpkg data/out/regu_ipt_araucania_20260929.gpkg --salida data/out/piloto_tres_comunas.json --descargar-footprints --release 2026-09-23.1
```

Si falta alguna comuna, falla **antes de iniciar descargas**. Reutiliza el descargador del ETL sin modificarlo. Los archivos quedan en `data/base/footprints/piloto_tres_comunas`, separados del Atlas y fuera de Git. Las descargas por bbox pueden contener edificios externos a la comuna; no deben contarse sin recorte/cruce posterior. La procedencia y licencia se conservan en los metadatos del descargador.

El bbox deriva de la cobertura del producto por CUT: no representa una nueva delimitación administrativa. Si el PRC propuesto incluye sectores fuera de esa cobertura, se revisará el área de descarga antes de comparar.

## Entrada del visor

El GPKG de producción no trae la capa `comunas`; el contrato de la muestra agrega límites y vigencia. No se inventan límites disolviendo la cobertura normativa. La ampliación de la muestra se solicita en `PEDIDOS.md` y se genera con el comando existente del ETL desde el equipo que tiene la base BCN.

Una vez disponible la muestra ampliada:

```powershell
python -m sig.servidor --gpkg samples/piloto_tres_comunas/muestra.gpkg --pmtiles samples/temuco/capa_ipt_araucania.pmtiles
```

## Carga del PRC propuesto de Toltén

El visor admite una capa local **GeoJSON de polígonos georreferenciados** y un JSON de metadatos. No admite PDF/CAD directamente: si solo se recuperan planos, primero hay que identificar versión, CRS, escala y georreferenciar/digitalizar con control de precisión. El cargador exige geometrías válidas; no intenta corregir silenciosamente una fuente desconocida.

Metadatos a completar con información real (no cargar este ejemplo como fuente documental):

```json
{
  "cut": "09118",
  "estado": "propuesta_no_acreditada_como_vigente",
  "fuente_documental": "Referencia o enlace del documento recuperado",
  "version_documental": "Identificador real de la versión del plano",
  "campo_zona": "ZONA"
}
```

```powershell
python -m sig.servidor --gpkg samples/piloto_tres_comunas/muestra.gpkg --propuesta-tolten data/base/propuestas/tolten.geojson --metadatos-propuesta data/base/propuestas/tolten.json
```

Al elegir Toltén aparece una capa apagada inicialmente, con línea magenta discontinua y aviso de escenario de estudio. El nombre de la zona y la procedencia son independientes. No entra en `ficha()`, la clasificación, el cálculo de derechos ni la exportación comunal base. El contrato exige metadatos, pero no certifica autenticidad documental ni exactitud espacial: ambas deben revisarse contra los originales.

## Comparación que sigue

1. Revisar geometrías, cobertura, duplicados y versión de footprints por comuna.
2. Cruzar huellas con la normativa base: conteo de edificios únicos, m² de huella y porcentaje intersectado por zona/riesgo. Separar conteos únicos de fragmentos; un edificio puede cruzar zonas.
3. Para Toltén, repetir el cruce con la propuesta y comparar por separado, registrando edificios fuera de la cobertura propuesta. Una misma huella y versión de Overture deben alimentar ambos escenarios.
4. Mario revisa parámetros urbanísticos antes de calcular capacidad edificatoria. Huella no equivale a superficie construida total, y una propuesta no se presenta como derecho vigente.
