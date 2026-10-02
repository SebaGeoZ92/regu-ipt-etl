"""Pruebas del recorrido local contra la muestra real y el motor existente."""
from fastapi.testclient import TestClient
import geopandas as gpd
import pytest
from etl.ficha import ficha
from sig.render import ROOT
from sig.servidor import crear_app


@pytest.fixture(scope='module')
def cliente():
    with TestClient(crear_app()) as c:
        yield c


def test_catalogo_y_capas(cliente):
    catalogo = cliente.get('/api/catalogo').json()
    assert len(catalogo['comunas']) == 7
    assert 'No reemplaza el CIP' in catalogo['aviso']
    for capa in ['capa_ipt', 'comunas', 'afectaciones']:
        r = cliente.get(f'/api/comunas/09101/capas/{capa}')
        assert r.status_code == 200
        assert r.json()['features']
        assert all(f['properties']['cut'] == '09101' for f in r.json()['features'])
    assert cliente.get('/api/comunas/99999/capas/comunas').status_code == 404
    assert cliente.get('/api/comunas/09101/capas/privada').status_code == 404


def test_consulta_reutiliza_motor(cliente):
    gpkg = ROOT/'samples/temuco/muestra.gpkg'
    g = gpd.read_file(gpkg, layer='capa_ipt')
    punto = g[g.clase == 'U1'].geometry.iloc[0].representative_point()
    r = cliente.post('/api/consulta', json={'longitud': punto.x, 'latitud': punto.y})
    assert r.status_code == 200
    assert r.json() == ficha(punto, gpkg, registrar=False)
    assert r.json()['particion']
    assert cliente.post('/api/consulta', json={'longitud': 181, 'latitud': 0}).status_code == 422
    assert cliente.post('/api/consulta', json={'longitud': 0, 'latitud': 0}).json()['fuera_de_cobertura']


def test_lamina_y_estaticos(cliente):
    r = cliente.get('/api/comunas/09101/lamina.pdf')
    assert r.status_code == 200 and r.content.startswith(b'%PDF')
    assert cliente.get('/api/comunas/09101/lamina.pdf').content == r.content
    assert cliente.get('/api/comunas/99999/lamina.pdf').status_code == 404
    assert cliente.get('/').status_code == 200
    assert 'Verdana' in cliente.get('/estilos.css').text
