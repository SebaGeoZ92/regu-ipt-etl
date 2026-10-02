"""Inventario y descarga separada de footprints para las tres comunas piloto."""
import argparse
import json
from pathlib import Path

import geopandas as gpd

from etl.volumen import descargar_footprints
from etl import progreso
from sig.render import SIG

CONFIG = SIG/'pilotos/araucania.json'


def inventario(gpkg):
    cfg = json.loads(CONFIG.read_text(encoding='utf-8'))
    cortes = [c['cut'] for c in cfg['comunas']]
    # Solo leer las tres comunas, también desde un producto nacional sin 'comunas'.
    where = 'cut IN (' + ','.join("'" + c + "'" for c in cortes) + ')'
    datos = gpd.read_file(gpkg, layer='capa_ipt', where=where)
    if datos.crs is None:
        raise ValueError('La capa normativa no declara su CRS')
    datos = datos.to_crs(4326)
    comunas = []
    for objetivo in cfg['comunas']:
        seleccion = datos[datos.cut.astype(str).str.zfill(5) == objetivo['cut']]
        geometria = seleccion.geometry.dropna()
        geometria = geometria[~geometria.is_empty]
        disponible = not geometria.empty
        comunas.append({**objetivo, 'disponible': disponible,
            'piezas': len(seleccion),
            'clases': sorted(seleccion.clase.dropna().unique().tolist()),
            'bbox_wsen': geometria.total_bounds.tolist() if disponible else None,
            'alcance_bbox': 'Cobertura del producto por CUT; no es un nuevo límite administrativo',
            'footprints': None})
    return {'piloto': cfg['nombre'], 'gpkg': str(Path(gpkg).resolve()),
            'comunas': comunas, 'tolten': cfg['tolten'],
            'nota': 'La propuesta de Toltén no participa en la clasificación normativa ni acredita vigencia.'}


def descargar(plan, destino, release):
    faltantes = [c['nombre'] for c in plan['comunas'] if not c['disponible']]
    if faltantes:
        raise ValueError('No se descarga un piloto incompleto. Faltan: ' + ', '.join(faltantes))
    if not release:
        raise ValueError('Indique --release para usar la misma versión de Overture en las tres comunas')
    progreso.iniciar(SIG.parent/'data/out/progreso.log')
    for i, comuna in enumerate(plan['comunas'], 1):
        progreso.paso('La Araucanía', comuna['nombre'], i, len(plan['comunas']))
        # Cada comuna tiene su archivo; el bbox puede incluir huellas de comunas vecinas.
        ruta = descargar_footprints(tuple(comuna['bbox_wsen']), Path(destino), comuna['cut'], release)
        comuna['footprints'] = str(ruta)
    plan['release_footprints'] = release
    return plan


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--gpkg', type=Path, required=True)
    ap.add_argument('--salida', type=Path, required=True, help='Informe JSON fuera del repositorio público')
    ap.add_argument('--descargar-footprints', action='store_true')
    ap.add_argument('--release', help='Versión de Overture común a las tres comunas')
    ap.add_argument('--directorio-footprints', type=Path, default=Path('data/base/footprints/piloto_tres_comunas'))
    args = ap.parse_args()
    try:
        plan = inventario(args.gpkg)
        if args.descargar_footprints:
            plan = descargar(plan, args.directorio_footprints, args.release)
        args.salida.parent.mkdir(parents=True, exist_ok=True)
        args.salida.write_text(json.dumps(plan, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        for c in plan['comunas']:
            print(f"{c['nombre']}: {c['piezas']} piezas" if c['disponible'] else f"{c['nombre']}: falta en la entrada")
        print(args.salida)
    except (ValueError, FileNotFoundError) as exc:
        ap.error(str(exc))


if __name__ == '__main__':
    main()
