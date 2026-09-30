"""Vigencia: cruce Portal IPT (instrumentos vigentes) ↔ servidor MINVU (lo que quedó en la partición).

- familias(): agrupa cada instrumento de origen con sus modificaciones vigentes (norma y fecha del origen,
  fecha de la última modificación y URL de la ordenanza).
- tabla_vigencia(): una fila por comuna y tipo (PRC, Seccional, LU, PRI/PRM) con ¿en portal? y ¿en servidor?
- emparejar(): para cada instrumento de la capa (ipt_tipo, ipt_nombre, cut) elige la familia del portal más
  parecida por nombre (Jaccard de tokens significativos); lo usan ficha() y el mapa para mostrar norma y fecha.
"""
from __future__ import annotations

import pandas as pd

from .normalize import norm_txt
from .portal import norma, url_ordenanza

# tipo de la capa (servidor) → grupo, y tipos del portal que le corresponden (en orden de preferencia)
GRUPOS = {"PRC": ("PRC", ["PRC"]), "SECCIONAL": ("Seccional", ["PS", "PRC"]), "LU": ("LU", ["LU", "PRC"]),
          "PRI": ("PRI/PRM", ["PRI", "PRM"]), "PRM": ("PRI/PRM", ["PRM", "PRI"])}
GRUPO_PORTAL = {"PRC": "PRC", "PS": "Seccional", "LU": "LU", "PRI": "PRI/PRM", "PRM": "PRI/PRM"}
_VACIAS = set("""PLAN PLANO PLANOS REGULADOR REGULADORES COMUNAL COMUNALES INTERCOMUNAL METROPOLITANO SECCIONAL
SECCIONALES APRUEBA APRUEBAN MODIFICACION MODIFICA ENMIENDA ENMIENDAS DE DEL LA LAS LOS EL Y EN A AL POR PARA CON
LIMITE LIMITES URBANO URBANA URBANOS ORDENANZA LOCAL PRC PRI PRM PS LU ZONA ZONAS SECTOR COMUNA VIA QUE INDICA N
NUEVO NUEVA ACTUALIZACION REGION TERRITORIO AREA AREAS""".split())


def _tokens(s) -> set[str]:
    return {t for t in norm_txt(s).replace(".", " ").replace(",", " ").split() if t not in _VACIAS and len(t) > 1}


def _ids(v) -> list[int]:
    if isinstance(v, list):
        return [int(x) for x in v]
    return [int(x) for x in str(v or "").replace(" ", "").split(",") if x.isdigit()]


def familias(vigentes: list[dict]) -> list[dict]:
    por_id = {i["id"]: i for i in vigentes}
    hijos_de = {}
    for i in vigentes:
        for h in _ids(i.get("instrumentosDescendientesIds")):
            if h in por_id:
                hijos_de.setdefault(i["id"], []).append(h)
    alcanzados = {h for hs in hijos_de.values() for h in hs}
    raices = [i for i in vigentes if i.get("clasificacion") == "Instrumento de origen" or i["id"] not in alcanzados]
    out = []
    for r in raices:
        if r.get("tipo") == "PRDU":        # indicativo: no norma el suelo
            continue
        fam, pend = [r], list(hijos_de.get(r["id"], []))
        vistos = {r["id"]}
        while pend:
            h = pend.pop()
            if h in vistos:
                continue
            vistos.add(h)
            fam.append(por_id[h])
            pend += hijos_de.get(h, [])
        mods = [x for x in fam[1:] if x.get("fechaInicioVigencia")]
        ult = max(mods, key=lambda x: x["fechaInicioVigencia"]) if mods else None
        out.append({"id": r["id"], "tipo": r.get("tipo"), "planificacion": r.get("planificacion"),
                    "denominacion": (r.get("denominacion") or "").strip(), "comunas": set(r.get("comunas") or []),
                    "norma": norma(r), "fecha": r.get("fechaInicioVigencia"),
                    "ultima_modificacion": ult["fechaInicioVigencia"] if ult else None,
                    "norma_ultima_modificacion": norma(ult) if ult else None,
                    "n_modificaciones": len(fam) - 1,
                    "ordenanza_url": url_ordenanza(r) or next((url_ordenanza(x) for x in reversed(fam) if url_ordenanza(x)), None)})
    return out


def _texto_norma(f: dict) -> str:
    return " · ".join(x for x in (f["norma"], f["fecha"]) if x)


def tabla_vigencia(fams: list[dict], serv: pd.DataFrame, comunas: pd.DataFrame) -> pd.DataFrame:
    """serv: columnas cut, ipt_tipo, ipt_nombre (instrumentos presentes en la partición).
    comunas: cut, comuna, region. Una fila por comuna × grupo con presencia en portal y/o servidor."""
    filas = []
    serv = serv.assign(grupo=serv["ipt_tipo"].map(lambda t: GRUPOS.get(t, (None,))[0]))
    for c in comunas.itertuples():
        for grupo in ("PRC", "Seccional", "LU", "PRI/PRM"):
            fp = [f for f in fams if c.cut in f["comunas"] and GRUPO_PORTAL.get(f["tipo"]) == grupo]
            ss = sorted(set(serv.loc[(serv.cut == c.cut) & (serv.grupo == grupo), "ipt_nombre"].dropna()))
            nota = ""
            en_portal = bool(fp)
            if grupo == "LU" and not fp and any(c.cut in f["comunas"] and f["tipo"] == "PRC" for f in fams):
                en_portal, nota = True, "LU definido por el PRC vigente"
            if not fp and not ss and not nota:
                continue
            fp = sorted(fp, key=lambda f: f["fecha"] or "", reverse=True)
            filas.append({"region": c.region, "cut": c.cut, "comuna": c.comuna, "tipo": grupo,
                          "instrumento": " | ".join(f["denominacion"] for f in fp) or "; ".join(ss),
                          "norma_fecha": " | ".join(_texto_norma(f) for f in fp),
                          "ultima_modificacion": max((f["ultima_modificacion"] or "" for f in fp), default="") or "",
                          "en_portal": en_portal, "en_servidor": bool(ss), "nombre_servidor": "; ".join(ss),
                          "ordenanza_url": next((f["ordenanza_url"] for f in fp if f["ordenanza_url"]), ""),
                          "nota": nota})
    df = pd.DataFrame(filas)
    if len(df):
        df["estado"] = df.apply(lambda r: "ambos" if r.en_portal and r.en_servidor else
                                ("solo_portal" if r.en_portal else "solo_servidor"), axis=1)
    return df


def resumen_brechas(t: pd.DataFrame) -> dict:
    if t.empty:
        return {}
    r = {"filas": len(t), "por_tipo_y_estado": {f"{k[0]}|{k[1]}": int(v) for k, v in t.groupby(["tipo", "estado"]).size().items()}}
    falta = t[(t.estado == "solo_portal") & t.tipo.isin(["PRC", "LU", "Seccional"])]
    r["comunas_con_ipt_comunal_sin_geometria"] = sorted(set(
        falta[falta.tipo.isin(["PRC", "LU"])].comuna + " (" + falta[falta.tipo.isin(["PRC", "LU"])].tipo + ")"))
    r["seccionales_sin_geometria"] = int((falta.tipo == "Seccional").sum())
    r["solo_servidor"] = sorted(set(t[t.estado == "solo_servidor"].comuna + " (" + t[t.estado == "solo_servidor"].tipo + ")"))
    return r


def emparejar(serv: pd.DataFrame, fams: list[dict]) -> pd.DataFrame:
    """Para cada (ipt_tipo, ipt_nombre, cut) de la capa, la familia del portal más parecida por nombre.
    confianza: 'alta' (score > 0), 'unica' (un solo candidato del tipo en la comuna) o 'baja' (se descarta)."""
    filas = []
    for (tipo, nombre, cut), _ in serv.groupby(["ipt_tipo", "ipt_nombre", "cut"], dropna=True):
        if tipo not in GRUPOS:
            continue
        cand = []
        for tp in GRUPOS[tipo][1]:
            cand = [f for f in fams if f["tipo"] == tp and cut in f["comunas"]]
            if cand:
                break
        if not cand:
            continue
        a = _tokens(nombre)
        puntaje = [(len(a & _tokens(f["denominacion"])) / max(1, len(a | _tokens(f["denominacion"]))), f["fecha"] or "", f)
                   for f in cand]
        s, _, f = max(puntaje, key=lambda x: (x[0], x[1]))
        confianza = "alta" if s > 0 else ("unica" if len(cand) == 1 else "baja")
        if confianza == "baja":
            continue
        filas.append({"ipt_tipo": tipo, "ipt_nombre": nombre, "cut": cut, "portal_id": f["id"], "portal_tipo": f["tipo"],
                      "denominacion": f["denominacion"], "norma": f["norma"], "fecha_vigencia": f["fecha"],
                      "ultima_modificacion": f["ultima_modificacion"], "n_modificaciones": f["n_modificaciones"],
                      "ordenanza_url": f["ordenanza_url"], "score": round(s, 3), "confianza": confianza})
    return pd.DataFrame(filas)
