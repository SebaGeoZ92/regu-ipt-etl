"""Verificación de la salida impresa, no solo de la configuración."""
import json
from pathlib import Path
import subprocess
import sys

import jsonschema
import geopandas as gpd
import numpy as np
import pymupdf
import pytest
from PIL import Image

from sig.render import ROOT, SIG, generar, plantilla


@pytest.fixture(scope='module')
def pdf(tmp_path_factory):
    p=tmp_path_factory.mktemp('lamina')/'temuco.pdf'
    generar('09101',p,ROOT/'samples/temuco/muestra.gpkg',ROOT/'samples/temuco/capa_ipt_araucania.pmtiles',plantilla('lamina_comuna'))
    return p


def test_pdf_vectorial_y_aviso(pdf):
    with pymupdf.open(pdf) as doc:
        assert len(doc)==1
        p=doc[0]
        assert p.rect.width == pytest.approx(420*72/25.4,abs=.01)
        assert p.rect.height == pytest.approx(297*72/25.4,abs=.01)
        assert not p.get_images()
        assert all('Verdana' in s['font'] for b in p.get_text('dict')['blocks'] if 'lines' in b for l in b['lines'] for s in l['spans'])
        assert len(p.get_drawings())>20
        texto=p.get_text()
        assert plantilla('lamina_comuna')['aviso'] in texto
        assert 'Resolución N° 149' in texto and '2010-02-02' in texto
        assert all(s['size']>=7 for b in p.get_text('dict')['blocks'] if 'lines' in b for l in b['lines'] for s in l['spans'])
    assert pdf.stat().st_size<10_000_000


def test_escala_medida_en_pdf(pdf):
    meta=json.loads(pdf.with_suffix('.json').read_text(encoding='utf-8'))
    with pymupdf.open(pdf) as doc:
        p=doc[0]; sx,sy=meta['barra_origen_mm']; pt=72/25.4
        # Medir los cinco rectángulos dibujados de la barra, en coordenadas PDF.
        segmentos=[]
        for dibujo in p.get_drawings():
            for item in dibujo['items']:
                if item[0]=='re':
                    r=item[1]
                    if abs(r.y1-(p.rect.height-sy*pt))<.01 and abs(r.height-2*pt)<.01:
                        segmentos.append(r)
        assert len(segmentos)==5
        ancho=max(r.x1 for r in segmentos)-min(r.x0 for r in segmentos)
        assert min(r.x0 for r in segmentos)==pytest.approx(sx*pt,abs=.01)
        assert ancho/pt*meta['escala']/1000==pytest.approx(5000,abs=.1)
        assert sum(meta['porcentajes'].values())==pytest.approx(100)
        escala_impresa=f"1:{meta['escala']:,.0f}".replace(',','.')
        assert escala_impresa in p.get_text()
        # Una distancia física de 1 cm expresa el denominador / 100 metros.
        assert 10*meta['escala']/1000 == pytest.approx(meta['escala']/100)


def test_snapshot(pdf):
    with pymupdf.open(pdf) as doc:
        pix=doc[0].get_pixmap(dpi=72)
        actual=np.asarray(Image.frombytes('RGB',[pix.width,pix.height],pix.samples)).astype(float)
    referencia=np.asarray(Image.open(SIG/'tests/lamina_comuna.png').convert('RGB')).astype(float)
    assert actual.shape==referencia.shape
    assert np.abs(actual-referencia).mean()<1.0
    assert (np.abs(actual-referencia).max(axis=2)>30).mean()<.01


def test_plantilla_invalida():
    cfg=plantilla('lamina_comuna'); cfg['tamano_texto_pt']=6
    with pytest.raises(jsonschema.ValidationError,match='minimum'):
        jsonschema.validate(cfg,json.loads((SIG/'layouts/_esquema.json').read_text(encoding='utf-8')))
    r=subprocess.run([sys.executable,'-m','sig.render','--layout','no_existe','--cut','09101','--salida','/tmp/no.pdf'],cwd=ROOT,capture_output=True,text=True)
    assert r.returncode!=0 and 'error:' in r.stderr


def test_muestra_sin_vigencia_opcional(tmp_path):
    origen = ROOT/'samples/temuco/muestra.gpkg'
    muestra = tmp_path/'sin_vigencia.gpkg'
    comunas = gpd.read_file(origen, layer='comunas')
    comunas.to_file(muestra, layer='comunas', driver='GPKG')
    capa = gpd.read_file(origen, layer='capa_ipt').drop(
        columns=['ipt_norma', 'ipt_fecha', 'ipt_ultmod', 'ord_url', 'fecha_extraccion'],
        errors='ignore',
    )
    capa.to_file(muestra, layer='capa_ipt', driver='GPKG')
    salida = tmp_path/'sin_vigencia.pdf'
    generar('09101', salida, muestra, ROOT/'samples/temuco/capa_ipt_araucania.pmtiles', plantilla('lamina_comuna'))
    with pymupdf.open(salida) as doc:
        texto_pdf = doc[0].get_text()
        assert 'Sin decreto en la muestra' in texto_pdf
        assert 'Sin fecha en la muestra' in texto_pdf
        assert plantilla('lamina_comuna')['aviso'] in texto_pdf


def test_verdana_ausente_no_se_sustituye(monkeypatch, tmp_path):
    from sig.tipografia import registrar_verdana
    monkeypatch.setenv('SIG_FUENTES', str(tmp_path))
    with pytest.raises(ValueError, match='Falta Verdana original'):
        registrar_verdana()
