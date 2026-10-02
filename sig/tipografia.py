"""Registro de Verdana auténtica, instalada por el usuario fuera del repositorio."""
import os
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


def registrar_verdana():
    """No sustituir silenciosamente por otra familia si falta Verdana."""
    carpetas = [Path(os.environ['SIG_FUENTES'])] if os.environ.get('SIG_FUENTES') else [
        Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts',
        Path.home() / '.local/share/fonts/verdana',
        Path('/Library/Fonts'), Path('/System/Library/Fonts/Supplemental'),
    ]
    archivos = {p.name.lower(): p for carpeta in carpetas if carpeta.is_dir()
                for p in carpeta.iterdir() if p.is_file()}
    for nombre, archivo in [('Verdana', 'verdana.ttf'), ('Verdana-Bold', 'verdanab.ttf')]:
        if archivo not in archivos:
            raise ValueError('Falta Verdana original. Instale verdana.ttf y verdanab.ttf '
                             'o indique su carpeta con SIG_FUENTES. Consulte sig/README.md.')
        fuente = TTFont(nombre, str(archivos[archivo]))
        if fuente.face.name.decode('ascii') != nombre:
            raise ValueError(f'{archivo} no corresponde a la fuente original {nombre}')
        pdfmetrics.registerFont(fuente)
