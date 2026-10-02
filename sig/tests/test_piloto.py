"""Piloto incompleto explícito y aislamiento de propuestas no vigentes."""
import json
from pathlib import Path

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import box

from sig.piloto import inventario, descargar
from sig.propuestas import cargar_tolten, ESTADO
from sig.render import ROOT
from sig.servidor import crear_app


def test_muestra_incompleta_no_descarga(monkeypatch, tmp_path):
    plan = inventario(ROOT/'samples/temuco/muestra.gpkg')
    assert [c['nombre'] for c in plan['comunas'] if not c['disponible']] == ['Loncoche', 'Toltén']
    assert plan['comunas'][1]['bbox_wsen']
    def prohibida(*args):
        pytest.fail('No debe iniciar ninguna descarga con una entrada incompleta')
    monkeypatch.setattr('sig.piloto.descargar_footprints', prohibida)
    with pytest.raises(ValueError, match='Loncoche, Toltén'):
        descargar(plan, tmp_path, '2026-09-23.1')


def test_inventario_sin_limites_y_descarga_version_unica(monkeypatch, tmp_path):
    # Geometrías exclusivamente sintéticas: verifica el contrato, no las comunas reales.
    capa = gpd.GeoDataFrame({'cut':['09109','09101','09118'], 'clase':['U1','R2','U2']},
                           geometry=[box(i,0,i+1,1) for i in range(3)], crs=4326)
    entrada = tmp_path/'producto.gpkg'
    capa.to_file(entrada, layer='capa_ipt', driver='GPKG')
    plan = inventario(entrada)
    assert all(c['disponible'] for c in plan['comunas'])
    llamadas = []
    def descargar_falso(bbox, destino, nombre, release):
        llamadas.append((nombre, release))
        return destino / (nombre + '.parquet')
    monkeypatch.setattr('sig.piloto.descargar_footprints', descargar_falso)
    descargar(plan, tmp_path, 'version_de_prueba')
    assert llamadas == [('09109','version_de_prueba'),('09101','version_de_prueba'),('09118','version_de_prueba')]
    with pytest.raises(ValueError, match='Faltan capas'):
        crear_app(entrada)


def propuesta_sintetica(tmp_path):
    archivo = tmp_path/'propuesta.geojson'
    gpd.GeoDataFrame({'ZONA':['FICTICIA'], 'atributo_no_publicable':['no exponer']},
                    geometry=[box(-73.2,-39.3,-73.1,-39.2)], crs=4326).to_file(archivo, driver='GeoJSON')
    meta = tmp_path/'propuesta.json'
    meta.write_text(json.dumps({'cut':'09118','estado':ESTADO, 'fuente_documental':'Prueba sintética',
                               'version_documental':'FICTICIA', 'campo_zona':'ZONA'}), encoding='utf-8')
    return archivo, meta


def test_propuesta_separada_de_consulta_y_capas(tmp_path):
    archivo, meta = propuesta_sintetica(tmp_path)
    with TestClient(crear_app()) as base, TestClient(crear_app(propuesta_tolten=archivo, metadatos_propuesta=meta)) as escenario:
        consulta = {'longitud':-72.6, 'latitud':-38.73}
        assert base.post('/api/consulta',json=consulta).json() == escenario.post('/api/consulta',json=consulta).json()
        assert base.get('/api/comunas/09101/capas/capa_ipt').json() == escenario.get('/api/comunas/09101/capas/capa_ipt').json()
        assert base.get('/api/propuestas/tolten').status_code == 404
        propiedades = escenario.get('/api/propuestas/tolten').json()['features'][0]['properties']
        assert propiedades['estado'] == ESTADO
        assert 'atributo_no_publicable' not in propiedades
        assert 'No acredita' in propiedades['aviso']
        assert escenario.get('/api/catalogo').json()['propuesta_tolten']['version_documental'] == 'FICTICIA'


def test_rechazar_propuesta_sin_fuente_o_como_vigente(tmp_path):
    archivo, meta = propuesta_sintetica(tmp_path)
    contenido = json.loads(meta.read_text())
    contenido['estado'] = 'vigente'
    meta.write_text(json.dumps(contenido))
    with pytest.raises(ValueError, match='estado'):
        cargar_tolten(archivo, meta)
    contenido['estado'] = ESTADO
    contenido['fuente_documental'] = ''
    meta.write_text(json.dumps(contenido))
    with pytest.raises(ValueError, match='fuente_documental'):
        cargar_tolten(archivo, meta)
