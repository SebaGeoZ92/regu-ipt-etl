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


def test_region_y_rotulo_minimapa_desde_datos(pdf):
    """La región sale de la capa 'comunas' (no está escrita en el código) y el nombre de la comuna del minimapa
    queda dentro del marco del minimapa, junto a su silueta."""
    cfg=plantilla('lamina_comuna'); pt=72/25.4
    with pymupdf.open(pdf) as doc:
        p=doc[0]; texto_pdf=p.get_text()
        assert 'Comuna de Temuco · Región de La Araucanía · CUT 09101' in texto_pdf
        assert 'UBICACIÓN · LA ARAUCANÍA' in texto_pdf
        mx,my,mw,mh=cfg['minimapa_mm']
        marco=pymupdf.Rect(mx*pt,p.rect.height-(my+mh)*pt,(mx+mw)*pt,p.rect.height-my*pt)
        rotulos=[r for r in p.search_for('Temuco') if r.intersects(marco)]
        assert rotulos and all(marco.contains(r) for r in rotulos)


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
        assert ancho/pt*meta['escala']/1000==pytest.approx(meta['barra_m'],abs=.1)
        assert 25 <= meta['barra_mm'] <= 60
        # Escala estándar de la serie 1/1,5/2/2,5/5/7,5 × 10^n (Temuco: 1:150.000)
        assert meta['escala'] == 150000
        assert sum(meta['porcentajes'].values())==pytest.approx(100)
        escala_impresa=f"1:{meta['escala']:,.0f}".replace(',','.')
        assert escala_impresa in p.get_text()
        # Una distancia física de 1 cm expresa el denominador / 100 metros.
        assert 10*meta['escala']/1000 == pytest.approx(meta['escala']/100)


def test_escala_no_tapa_la_comuna(pdf):
    """El recuadro de escala no se superpone al límite comunal (Temuco llega a la esquina inferior izquierda)."""
    from shapely.ops import transform as shp_transform
    from sig.render import CRS, caja_escala, encuadre_a_escala
    meta=json.loads(pdf.with_suffix('.json').read_text(encoding='utf-8'))
    cfg=plantilla('lamina_comuna')
    lim=gpd.read_file(ROOT/'samples/temuco/muestra.gpkg',layer='comunas').to_crs(CRS)
    lim=lim[lim.cut=='09101'].geometry.iloc[0]
    tr,_=encuadre_a_escala(lim.bounds,cfg['mapa_mm'],meta['escala'])
    comuna=shp_transform(lambda u,v,z=None: tuple(t/(72/25.4) for t in tr(u,v)),lim)
    assert caja_escala(*meta['barra_origen_mm'],meta['barra_mm']).intersection(comuna).area < 1.0


def test_escala_estandar_y_barra():
    from sig.render import barra_escala, escala_estandar, etiqueta_distancia
    assert escala_estandar(142539.4) == 150000
    assert escala_estandar(18000) == 20000 and escala_estandar(20000) == 20000 and escala_estandar(76000) == 100000
    assert escala_estandar(258785) == 300000 and escala_estandar(313212) == 400000
    assert barra_escala(150000) == 5000 and barra_escala(20000) == 1000 and barra_escala(5000) == 250
    assert etiqueta_distancia(5000) == '5 km' and etiqueta_distancia(2500) == '2,5 km' and etiqueta_distancia(250) == '250 m'


def test_comunas_de_la_muestra_llenan_el_marco():
    """Con la escala estándar, cada comuna de la muestra ocupa al menos el 55 % del área que ocuparía a la escala
    exacta del encuadre (regresión: sin 1:300.000 y 1:400.000 algunas quedaban en 27-39 %)."""
    from reportlab.lib.units import mm
    from sig.render import CRS, encuadre, escala_estandar
    cfg=plantilla('lamina_comuna')
    for g in gpd.read_file(ROOT/'samples/temuco/muestra.gpkg',layer='comunas').to_crs(CRS).geometry:
        _,f=encuadre(g.bounds,cfg['mapa_mm'],1.16); d=1000*mm/f
        assert (d/escala_estandar(d))**2 >= .55, d


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
    cfg=plantilla('lamina_comuna'); cfg['codigo_mm']=[350]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(cfg,json.loads((SIG/'layouts/_esquema.json').read_text(encoding='utf-8')))
    r=subprocess.run([sys.executable,'-m','sig.render','--layout','no_existe','--cut','09101','--salida','/tmp/no.pdf'],cwd=ROOT,capture_output=True,text=True)
    assert r.returncode!=0 and 'error:' in r.stderr


def test_panel_desborda_segun_minimapa(tmp_path):
    """El límite del panel se deriva del minimapa de la plantilla: si el minimapa sube, los instrumentos no caben
    y se levanta un error claro (no se trunca texto)."""
    cfg=plantilla('lamina_comuna'); mx,my,mw,mh=cfg['minimapa_mm']; cfg['minimapa_mm']=[mx,my+80,mw,mh]
    with pytest.raises(ValueError,match='exceden el panel'):
        generar('09101',tmp_path/'x.pdf',ROOT/'samples/temuco/muestra.gpkg',ROOT/'samples/temuco/capa_ipt_araucania.pmtiles',cfg)


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
