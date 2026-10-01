"""Lámina comunal vectorial; unidades físicas y proyección UTM métrica."""
import argparse
import hashlib
import json
import math
import unicodedata
from functools import lru_cache
from pathlib import Path

import geopandas as gpd
import jsonschema
from pyproj import Transformer
from reportlab.lib.colors import HexColor
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth
from shapely.geometry import box
from shapely import make_valid

from etl.mapa import COLORES
from sig.tipografia import registrar_verdana

ROOT = Path(__file__).resolve().parents[1]
SIG = Path(__file__).resolve().parent
CRS = 'EPSG:32718'


def plantilla(nombre):
    ruta = SIG / 'layouts' / f'{nombre}.json'
    cfg = json.loads(ruta.read_text(encoding='utf-8'))
    try:
        jsonschema.validate(cfg, json.loads((SIG / 'layouts/_esquema.json').read_text(encoding='utf-8')))
    except jsonschema.ValidationError as e:
        raise ValueError(f'Plantilla inválida ({e.json_path}): {e.message}') from e
    return cfg


def poligonos(g):
    if g.geom_type == 'Polygon':
        yield g
    elif hasattr(g, 'geoms'):
        for parte in g.geoms:
            yield from poligonos(parte)


def trazado(c, g, transform):
    p = c.beginPath()
    for pol in poligonos(g):
        for anillo in [pol.exterior, *pol.interiors]:
            xy = [transform(x, y) for x, y, *_ in anillo.coords]
            if not xy:
                continue
            p.moveTo(*xy[0])
            for coord in xy[1:]:
                p.lineTo(*coord)
            p.close()
    return p


def encuadre(bounds, rect, margen=1.10):
    x, y, w, h = [v * mm for v in rect]
    a, b, d, e = bounds
    factor = min(w / ((d-a)*margen), h / ((e-b)*margen))
    cx, cy = (a+d)/2, (b+e)/2
    return lambda u,v: (x+w/2+(u-cx)*factor, y+h/2+(v-cy)*factor), factor


def textura(c, p, rect, patron, riesgo=False):
    if patron == 'liso':
        return
    x,y,w,h = [v*mm for v in rect]
    c.saveState()
    c.clipPath(p, stroke=0, fill=0, fillMode=0)
    c.setStrokeColor(HexColor('#353c38' if riesgo else '#676a61'))
    c.setFillColor(HexColor('#676a61'))
    c.setLineWidth(0.5 if riesgo else 0.3)
    paso = (2 if riesgo else 3)*mm
    if patron in ('horizontal','cruz'):
        for i in range(math.ceil(h/paso)+1): c.line(x,y+(i+.5)*paso,x+w,y+(i+.5)*paso)
    if patron in ('vertical','cruz'):
        for i in range(math.ceil(w/paso)+1): c.line(x+(i+.5)*paso,y,x+(i+.5)*paso,y+h)
    if patron == 'diagonal':
        for i in range(-math.ceil(h/paso), math.ceil(w/paso)+1): c.line(x+i*paso,y,x+i*paso+h,y+h)
    if patron == 'puntos':
        for i in range(math.ceil(w/paso)+1):
            for j in range(math.ceil(h/paso)+1): c.circle(x+i*paso,y+j*paso,0.4,stroke=0,fill=1)
    c.restoreState()


def texto(c, x, y, s, size=8, bold=False):
    c.setFillColor(HexColor('#263e36'))
    c.setFont('Verdana-Bold' if bold else 'Verdana',size)
    c.drawString(x*mm,y*mm,str(s))


def parrafo(c, x, y, contenido, ancho, size=8):
    linea = ''
    for palabra in str(contenido).split():
        prueba = (linea+' '+palabra).strip()
        if stringWidth(prueba,'Verdana',size) > ancho*mm:
            texto(c,x,y,linea,size); y -= size*1.35/mm; linea=palabra
        else: linea=prueba
    if linea: texto(c,x,y,linea,size); y -= size*1.35/mm
    return y


@lru_cache(maxsize=4)
def regional(ruta):
    g = gpd.read_file(ruta, ZOOM_LEVEL='6').to_crs(CRS)
    return make_valid(g.geometry.make_valid().simplify(100).union_all())


def generar(cut, salida, gpkg, pmtiles, cfg):
    registrar_verdana()
    comunas = gpd.read_file(gpkg,layer='comunas').to_crs(CRS)
    seleccion = comunas[comunas.cut.astype(str).str.zfill(5)==cut]
    if len(seleccion)!=1: raise ValueError(f'CUT {cut}: se esperaba una comuna, hay {len(seleccion)}')
    nombre = seleccion.iloc[0].comuna
    limite = make_valid(seleccion.geometry.iloc[0])
    capa = gpd.read_file(gpkg,layer='capa_ipt')
    capa = capa[capa.cut.astype(str).str.zfill(5)==cut].copy()
    requeridos = {'clase','riesgo','area_m2','ipt_nombre'}
    if capa.empty or not requeridos.issubset(capa.columns): raise ValueError('Muestra vacía o contrato incompleto')
    # La vigencia es opcional en el contrato; nunca se infiere si falta.
    for campo in ('ipt_norma', 'ipt_fecha', 'ipt_ultmod', 'fecha_extraccion'):
        if campo not in capa.columns:
            capa[campo] = ''
    areas = capa.groupby('clase').area_m2.sum()
    if not set(areas.index).issubset(COLORES): raise ValueError('Clase normativa desconocida')
    capa = capa.to_crs(CRS)
    legal = json.loads((ROOT/'legal_refs.json').read_text(encoding='utf-8'))
    salida = Path(salida); salida.parent.mkdir(parents=True,exist_ok=True)
    c = canvas.Canvas(str(salida),pagesize=(420*mm,297*mm),pageCompression=1,invariant=1)
    c.setTitle(f'Situación normativa del suelo · Comuna de {nombre}')
    c.setAuthor(cfg['proyecto'])
    c.setFillColor(HexColor('#fafbf8')); c.rect(0,0,420*mm,297*mm,fill=1,stroke=0)
    tx,ty=cfg['titulo_mm']
    texto(c,tx,ty,cfg['titulo'],23,True)
    texto(c,tx,ty-9,f'Comuna de {nombre} · Región de La Araucanía · CUT {cut}',12)
    texto(c,350,ty,'LÁMINA COMUNAL / 01',9,True)
    rect=cfg['mapa_mm']; x,y,w,h=rect
    transform,factor=encuadre(limite.bounds,rect,1.16)
    escala=1000*mm/factor
    c.saveState()
    marco=c.beginPath(); marco.rect(x*mm,y*mm,w*mm,h*mm)
    c.clipPath(marco,stroke=0,fill=0)
    c.setFillColor(HexColor('#eef0e9')); c.rect(x*mm,y*mm,w*mm,h*mm,fill=1,stroke=0)
    for geom in comunas.geometry:
        c.setStrokeColor(HexColor('#a7afa6')); c.setLineWidth(.4)
        c.drawPath(trazado(c,geom,transform),stroke=1,fill=0)
    for clase in COLORES:
        grupo=capa[capa.clase==clase]
        if grupo.empty: continue
        p=trazado(c,make_valid(grupo.geometry.make_valid().simplify(2).union_all()),transform)
        c.setFillColor(HexColor(COLORES[clase])); c.drawPath(p,fill=1,stroke=0,fillMode=0)
        textura(c,p,rect,cfg['patrones'][clase])
    riesgo=capa[capa.riesgo.fillna(False).astype(bool)]
    if not riesgo.empty:
        textura(c,trazado(c,make_valid(riesgo.geometry.make_valid().simplify(2).union_all()),transform),rect,'diagonal',True)
    c.setStrokeColor(HexColor('#263e36')); c.setLineWidth(.9)
    c.drawPath(trazado(c,limite,transform),stroke=1,fill=0)
    # Solo etiquetas vecinas que caben completamente; evitar solapamientos.
    ocupadas=[]
    for fila in comunas.itertuples():
        if str(fila.cut)==cut: continue
        px,py=transform(*fila.geometry.representative_point().coords[0])
        ancho=stringWidth(fila.comuna,'Verdana',8)
        r=box(px-ancho/2-3,py-3,px+ancho/2+3,py+10)
        if box(x*mm+5,(y+22)*mm,(x+w)*mm-5,(y+h-22)*mm).contains(r) and not any(r.intersects(o) for o in ocupadas):
            texto(c,(px-ancho/2)/mm,py/mm,fila.comuna,8); ocupadas.append(r)
    c.restoreState()
    # Norte geográfico calculado por proyección de un meridiano local.
    centro=seleccion.to_crs(4326).geometry.iloc[0].centroid
    t=Transformer.from_crs(4326,CRS,always_xy=True)
    p0=t.transform(centro.x,centro.y); p1=t.transform(centro.x,centro.y+.01)
    ang=math.degrees(math.atan2(p1[0]-p0[0],p1[1]-p0[1]))
    nx,ny=cfg['norte_mm']; c.saveState(); c.translate(nx*mm,ny*mm); c.rotate(-ang)
    c.setStrokeColor(HexColor('#263e36')); c.setLineWidth(1); c.line(0,-8*mm,0,4*mm)
    p=c.beginPath(); p.moveTo(0,5*mm); p.lineTo(-1.5*mm,1*mm); p.lineTo(1.5*mm,1*mm); p.close()
    c.setFillColor(HexColor('#263e36')); c.drawPath(p,fill=1,stroke=0); c.restoreState(); texto(c,nx-1,ny+8,'N',10,True)
    sx,sy=cfg['escala_mm']; distancia=5000; largo=distancia*factor/mm
    c.setFillColor(HexColor('#fafbf8')); c.rect((sx-3)*mm,(sy-8)*mm,(largo+24)*mm,22*mm,fill=1,stroke=0)
    for i in range(5):
        c.setFillColor(HexColor('#263e36' if i%2==0 else '#ffffff'))
        c.setStrokeColor(HexColor('#263e36')); c.setLineWidth(.5)
        c.rect((sx+i*largo/5)*mm,sy*mm,largo/5*mm,2*mm,fill=1,stroke=1)
    texto(c,sx,sy-4,'0',8); texto(c,sx+largo-3,sy-4,'5 km',8)
    texto(c,sx,sy+6,f'1:{escala:,.0f}'.replace(',','.')+' · imprimir al 100 %',9,True)
    # Leyenda y estadísticas de la superficie del producto, sin doble conteo.
    px,py,pw,ph=cfg['panel_mm']; yy=py+ph-5
    texto(c,px,yy,'CLASES Y SUPERFICIE',10,True); yy-=9
    for clase in COLORES:
        if clase not in areas: continue
        sw=[px,yy-2,8,5]; p=c.beginPath(); p.rect(px*mm,(yy-2)*mm,8*mm,5*mm)
        c.setFillColor(HexColor(COLORES[clase])); c.drawPath(p,fill=1,stroke=0); textura(c,p,sw,cfg['patrones'][clase])
        texto(c,px+11,yy,f'{clase}   {areas[clase]/areas.sum()*100:.2f} %',10,True)
        yy=parrafo(c,px+11,yy-5,legal['clases'][clase]['titulo'],pw-12,cfg['tamano_texto_pt'])-5
    texto(c,px,yy,'////  Áreas de riesgo (superposición)',8); yy-=6
    texto(c,px,yy,'—  Límite comunal BCN',8); yy-=7
    texto(c,px,yy,f'Superficie clasificada: {areas.sum()/1e6:,.2f} km²',8); yy-=10
    texto(c,px,yy,'INSTRUMENTOS · CRUCE PORTAL IPT',9,True); yy-=6
    campos=['ipt_nombre','ipt_norma','ipt_fecha','ipt_ultmod']
    for fila in capa[campos].fillna('').drop_duplicates().itertuples(index=False):
        if not fila.ipt_nombre: continue
        yy=parrafo(c,px,yy,fila.ipt_nombre,pw,9)
        yy=parrafo(c,px,yy,f'{fila.ipt_norma or "Sin decreto en la muestra"} · {fila.ipt_fecha or "Sin fecha"}',pw,8)
        if fila.ipt_ultmod: yy=parrafo(c,px,yy,f'Última modificación: {fila.ipt_ultmod}',pw,8)
        yy-=3
    if yy < 119: raise ValueError('Instrumentos exceden el panel: ajustar plantilla; no se recorta texto')
    mx,my,mw,mh=cfg['minimapa_mm']
    texto(c,px,my+mh+6,'UBICACIÓN · LA ARAUCANÍA',9,True)
    reg=regional(str(pmtiles)); tr,_=encuadre(reg.bounds,[mx,my,mw,mh])
    c.setFillColor(HexColor('#dce1d7')); c.drawPath(trazado(c,reg,tr),fill=1,stroke=0,fillMode=0)
    c.setFillColor(HexColor('#b0442b')); c.setStrokeColor(HexColor('#263e36')); c.setLineWidth(.6)
    c.drawPath(trazado(c,limite,tr),fill=1,stroke=1,fillMode=0)
    texto(c,px+75,my+25,nombre,8,True)
    texto(c,px,my-3,'Cobertura regional generalizada del PMTiles.',7)
    rx,ry=cfg['rotulo_mm']; sha=hashlib.sha256(Path(gpkg).read_bytes()).hexdigest()[:12]
    texto(c,rx,ry,cfg['proyecto'],10,True)
    texto(c,rx,ry-5,f'Emisión: {cfg["fecha"]} · WGS 84 / UTM 18S (EPSG:32718) · '+cfg['fuentes'],8)
    texto(c,rx,ry-10,f'Datos: muestra {Path(gpkg).name} · SHA256 {sha} · extracción '+(str(capa.fecha_extraccion.dropna().max()) if capa.fecha_extraccion.fillna('').ne('').any() else 'Sin fecha en la muestra'),7)
    ax,ay=cfg['aviso_mm']; texto(c,ax,ay,cfg['aviso'],9,True)
    texto(c,ax,ay-5,'Textos legales en BORRADOR, pendientes de validación profesional. Porcentajes sobre area_m2 del ETL (ESRI:102033).',7)
    c.showPage(); c.save()
    meta={'cut':cut,'escala':escala,'barra_m':distancia,'barra_mm':largo,'barra_origen_mm':[sx,sy], 'sha256_datos':sha,'porcentajes':(areas/areas.sum()*100).to_dict()}
    salida.with_suffix('.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return salida


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--layout',default='lamina_comuna'); ap.add_argument('--cut')
    ap.add_argument('--formato',choices=['A3-h'],default='A3-h'); ap.add_argument('--salida',type=Path,required=True)
    ap.add_argument('--gpkg',type=Path,default=ROOT/'samples/temuco/muestra.gpkg')
    ap.add_argument('--pmtiles',type=Path,default=ROOT/'samples/temuco/capa_ipt_araucania.pmtiles')
    ap.add_argument('--todas-las-comunas',action='store_true'); ap.add_argument('--region',default='ARAUCANIA')
    args=ap.parse_args()
    try:
        cfg=plantilla(args.layout)
        if args.todas_las_comunas:
            comunas=gpd.read_file(args.gpkg,layer='comunas')
            normal=lambda s: ''.join(c for c in unicodedata.normalize('NFD',s.upper()) if not unicodedata.combining(c))
            cuts=comunas[comunas.region.map(normal).str.contains(normal(args.region),regex=False)].cut.astype(str).tolist()
            if not cuts: raise ValueError('No hay comunas para la región solicitada')
        elif args.cut: cuts=[args.cut]
        else: raise ValueError('Indicar --cut o --todas-las-comunas')
        for i,cut in enumerate(cuts,1):
            out=args.salida/f'{cut}.pdf' if args.todas_las_comunas else args.salida
            print(f'{i}/{len(cuts)} {generar(cut,out,args.gpkg,args.pmtiles,cfg)}',flush=True)
    except (ValueError,FileNotFoundError) as e: ap.error(str(e))

if __name__=='__main__': main()
