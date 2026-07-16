# application/services/familia_detector.py
"""Deteccion DETERMINISTA de familias/tipologias por texto.

Extraido de ``contrato_selector`` (jul 2026) para compartirlo con el
``HeaderResolverService`` (deduccion de proveedor por familia+obra) sin
duplicar las palabras clave. Una familia nueva se anade AQUI y la ven
todos los consumidores:

  - contrato_selector: puntua contratos candidatos contra el albaran.
  - header_resolver_service: puntua PROVEEDORES candidatos de la obra
    contra la familia del albaran cuando la IA no fijo el CIF.

Todo es puro (sin I/O) y barato: strings + regex.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

# Familias detectables por palabras clave / patrones en el texto.
# El orden importa poco: se puntua cada una por separado.
FAMILIAS: dict[str, tuple[str, ...]] = {
    "hormigon": ("hormigon", "hormigón", "ha-", "hm-", "hl-", "hne-"),
    "mortero": ("mortero", "m-5", "m-7", "m-10", "m-12", "m-15", "m-20"),
    "residuos": ("residuo", "contenedor", "rcd", "ler", "escombro"),
    "acero": ("acero", "corrugado", "b500", "ferralla", "mallazo"),
    "combustible": ("gasoleo", "gasóleo", "gasolina", "adblue", "carburante"),
    "maquinaria": ("alquiler", "maquina", "máquina", "retro", "grua", "grúa"),
}

# Patron de designacion de hormigon (HA-25/B/20, HM-20...) y mortero (M-5).
RE_HORMIGON = re.compile(r"\bH[ALMN]E?\s?-?\s?\d{2,3}\b", re.IGNORECASE)
RE_MORTERO = re.compile(r"\bM-\s?\d{1,2}(?:[.,]\d)?\b", re.IGNORECASE)

# ``contexto_linea.tipo_familia`` (sv2) usa 'alquiler_maquinaria';
# la clave de FAMILIAS es 'maquinaria'. Mapeo de normalizacion.
_MAPEO_TIPO_FAMILIA: dict[str, str] = {
    "alquiler_maquinaria": "maquinaria",
}


def _compilar_familias() -> dict[str, re.Pattern[str]]:
    """Compila cada familia como alternancia de claves con frontera de
    palabra INICIAL (``\\b``). (jul 2026) Sin la frontera, 'ler' casaba
    dentro de 'taller' y de 'alquiler' y disparaba residuos en falso."""
    out: dict[str, re.Pattern[str]] = {}
    for fam, claves in FAMILIAS.items():
        alt = "|".join(re.escape(c) for c in claves)
        out[fam] = re.compile(r"\b(?:" + alt + r")", re.IGNORECASE)
    return out


_FAMILIAS_RE: dict[str, re.Pattern[str]] = _compilar_familias()


def norm_texto(texto: Any) -> str:
    return str(texto or "").lower()


def familias_de_texto(texto: str) -> set[str]:
    """Familias detectadas en un texto (por palabras clave y patrones).

    (jul 2026) Normaliza INTERNAMENTE: los albaranes llegan casi siempre
    en MAYUSCULAS y las claves estan en minuscula; sin esto, solo las
    regex IGNORECASE (hormigon/mortero) casaban.
    """
    t = norm_texto(texto)
    fams: set[str] = {fam for fam, rx in _FAMILIAS_RE.items() if rx.search(t)}
    if RE_HORMIGON.search(t):
        fams.add("hormigon")
    if RE_MORTERO.search(t):
        fams.add("mortero")
    return fams


def tokens_tecnicos(texto: str) -> set[str]:
    """Designaciones tecnicas presentes (HA-25, M-5...), para refuerzos
    de puntuacion cuando la MISMA designacion aparece en ambos lados."""
    t = norm_texto(texto)
    return set(RE_HORMIGON.findall(t)) | set(RE_MORTERO.findall(t))


def normaliza_tipo_familia(valor: Any) -> str | None:
    """Normaliza un ``tipo_familia`` del contexto de linea a la clave de
    FAMILIAS. Devuelve None para vacios y para 'otro'/'generico'."""
    fam = str(valor or "").strip().lower()
    if not fam or fam in ("otro", "generico"):
        return None
    return _MAPEO_TIPO_FAMILIA.get(fam, fam)


def familias_de_filas_merge(rows: Iterable[dict] | None) -> set[str]:
    """Familias del albaran a partir de filas de ``albaran_lines_merge``
    (dicts con ``codigo``, ``concepto``, ``contexto_linea_json``).

    El ``tipo_familia`` declarado en el contexto MANDA (senal de la IA de
    fase 2); el resto se deduce por texto (codigo + concepto +
    descripcion_extendida) con las mismas reglas que el selector de
    contratos.
    """
    fams: set[str] = set()
    partes: list[str] = []
    for r in rows or []:
        partes.append(norm_texto(r.get("codigo")))
        partes.append(norm_texto(r.get("concepto")))
        raw = r.get("contexto_linea_json")
        if not raw:
            continue
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:  # noqa: BLE001 — contexto corrupto: se ignora
            data = None
        if isinstance(data, dict):
            fam = normaliza_tipo_familia(data.get("tipo_familia"))
            if fam:
                fams.add(fam)
            partes.append(norm_texto(data.get("descripcion_extendida")))
    fams |= familias_de_texto(" ".join(p for p in partes if p))
    return fams
