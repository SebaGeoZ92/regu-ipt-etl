"""Avance de procesos largos (convención del proyecto).

Una línea por comuna o capa, con el formato "HH:MM:SS <región|servicio> <comuna|capa> i/total", que se escribe
en data/out/progreso.log y además se imprime con flush=True. Cada comando largo llama a iniciar() al partir
(el archivo se reescribe) y a paso() por cada unidad procesada.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

_archivo: Path | None = None


def iniciar(path: Path | None) -> None:
    """Fija el archivo de avance y lo deja vacío. path=None: solo imprime."""
    global _archivo
    _archivo = path
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")


def paso(grupo: str, nombre: str, i: int, total: int) -> str:
    """Registra una unidad procesada: grupo = región (comunas) o servicio (capas)."""
    linea = f"{datetime.now():%H:%M:%S} {grupo} {nombre} {i}/{total}"
    print(linea, flush=True)
    if _archivo is not None:
        with open(_archivo, "a", encoding="utf-8") as fh:
            fh.write(linea + "\n")
            fh.flush()
    return linea
