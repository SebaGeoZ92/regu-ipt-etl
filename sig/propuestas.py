"""Carga de escenarios de estudio separados de la normativa base."""
import json
from pathlib import Path

import geopandas as gpd

ESTADO = 'propuesta_no_acreditada_como_vigente'
AVISO = 'Propuesta de PRC de Toltén · escenario de estudio. No acredita normativa vigente.'


def cargar_tolten(archivo, metadatos):
    meta = json.loads(Path(metadatos).read_text(encoding='utf-8'))
    if meta.get('cut') != '09118' or meta.get('estado') != ESTADO:
        raise ValueError('La propuesta debe identificar CUT 09118 y estado ' + ESTADO)
    for campo in ('fuente_documental', 'version_documental', 'campo_zona'):
        if not isinstance(meta.get(campo), str) or not meta[campo].strip():
            raise ValueError(f'Falta {campo} en los metadatos de la propuesta')
    if Path(archivo).suffix.lower() != '.geojson':
        raise ValueError('La propuesta debe ser un GeoJSON georreferenciado')
    g = gpd.read_file(archivo)
    if g.empty or g.crs is None or meta['campo_zona'] not in g.columns:
        raise ValueError('Propuesta vacía, sin CRS o sin el campo de zona declarado')
    if not g.geometry.geom_type.isin(['Polygon', 'MultiPolygon']).all() or not g.is_valid.all() or g.is_empty.any():
        raise ValueError('La propuesta requiere polígonos válidos y no vacíos; revisar la fuente')
    # Solo exponer zona y procedencia; nunca importar reglas ni atributos personales.
    salida = g[[meta['campo_zona'], 'geometry']].rename(columns={meta['campo_zona']: 'zona_propuesta'}).to_crs(4326)
    salida['estado'] = ESTADO
    salida['aviso'] = AVISO
    return {'datos': json.loads(salida.to_json(drop_id=True)), 'metadatos': {
        'cut': '09118', 'estado': ESTADO, 'aviso': AVISO,
        'fuente_documental': meta['fuente_documental'],
        'version_documental': meta['version_documental']}}
