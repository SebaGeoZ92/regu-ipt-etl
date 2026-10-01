"""Primer espacio de trabajo local: capas, consulta normativa y lámina comunal."""
import argparse
from functools import lru_cache
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from contextlib import asynccontextmanager

import geopandas as gpd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from shapely.geometry import Point

from etl.ficha import ficha
from etl.mapa import COLORES
from sig.render import ROOT, SIG, generar, plantilla


class Consulta(BaseModel):
    longitud: float = Field(ge=-180, le=180, allow_inf_nan=False)
    latitud: float = Field(ge=-90, le=90, allow_inf_nan=False)


def crear_app(gpkg=ROOT/'samples/temuco/muestra.gpkg',
              pmtiles=ROOT/'samples/temuco/capa_ipt_araucania.pmtiles'):
    gpkg, pmtiles = Path(gpkg), Path(pmtiles)
    temporales = TemporaryDirectory(prefix='regu-sig-')
    bloqueo_pdf = Lock()

    @asynccontextmanager
    async def ciclo(app):
        yield
        temporales.cleanup()

    app = FastAPI(title='Atlas Normativo · Regu', lifespan=ciclo)

    @lru_cache(maxsize=3)
    def leer(capa):
        return gpd.read_file(gpkg, layer=capa).to_crs(4326)

    def validar_cut(cut):
        if cut not in set(leer('comunas').cut.astype(str).str.zfill(5)):
            raise HTTPException(404, 'Comuna no disponible en esta muestra')

    @app.get('/api/catalogo')
    def catalogo():
        legal = json.loads((ROOT/'legal_refs.json').read_text(encoding='utf-8'))
        return {
            'comunas': sorted([{'cut': str(f.cut).zfill(5), 'nombre': f.comuna}
                               for f in leer('comunas').itertuples()], key=lambda f: f['nombre']),
            'clases': [{'codigo': c, 'color': color, 'titulo': legal['clases'][c]['titulo']}
                       for c, color in COLORES.items()],
            'aviso': plantilla('lamina_comuna')['aviso'],
        }

    @app.get('/api/comunas/{cut}/capas/{capa}')
    def capa_comunal(cut: str, capa: str):
        validar_cut(cut)
        if capa not in {'capa_ipt', 'afectaciones', 'comunas'}:
            raise HTTPException(404, 'Capa no disponible')
        datos = leer(capa)
        datos = datos[datos.cut.astype(str).str.zfill(5) == cut]
        return json.loads(datos.to_json(drop_id=True))

    @app.post('/api/consulta')
    def consultar(consulta: Consulta):
        # No registrar coordenadas, ni consultas en disco, en este prototipo local.
        return ficha(Point(consulta.longitud, consulta.latitud), gpkg, registrar=False)

    @app.get('/api/comunas/{cut}/lamina.pdf')
    def lamina(cut: str):
        validar_cut(cut)
        destino = Path(temporales.name)/f'{cut}.pdf'
        with bloqueo_pdf:
            if not destino.exists():
                try:
                    generar(cut, destino, gpkg, pmtiles, plantilla('lamina_comuna'))
                except ValueError as exc:
                    destino.unlink(missing_ok=True)
                    raise HTTPException(422, str(exc)) from exc
        return FileResponse(destino, media_type='application/pdf', filename=f'lamina-{cut}.pdf')

    app.mount('/', StaticFiles(directory=SIG/'web', html=True), name='interfaz')
    return app


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--puerto', type=int, default=8000)
    args = parser.parse_args()
    # Solo equipo local; un despliegue multiusuario necesita autenticación y límites.
    uvicorn.run(crear_app(), host='127.0.0.1', port=args.puerto, access_log=False)


if __name__ == '__main__':
    main()
