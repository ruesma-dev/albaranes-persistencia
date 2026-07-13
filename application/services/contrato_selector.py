# application/services/contrato_selector.py
"""Seleccion DETERMINISTA del contrato mas probable.

Cuando Sigrid devuelve VARIOS contratos para el mismo proveedor+obra, hasta
ahora no se auto-seleccionaba ninguno (el humano elegia). Este selector
puntua cada contrato contra el albaran y elige el mas probable cuando la
diferencia es clara; si hay empate o nadie puntua, devuelve None y se
mantiene el comportamiento actual (que elija el humano).

Criterio (sin IA, por palabras):
  - Se construye el "texto del albaran" (codigos + conceptos de sus lineas).
  - Se construye el "texto de cada contrato" (nombre + descripciones de sus
    lineas).
  - Se puntua por FAMILIA/tipologia (hormigon, mortero, residuos...): es la
    senal mas fuerte. Un albaran de hormigon debe ir al contrato de
    hormigon, no al de mortero.
  - Se refuerza con tokens tecnicos comunes (HA-25, M-5, LER, etc.).

Ejemplo real: proveedor con 2 contratos (uno de HORMIGON y otro de
MORTERO); el albaran trae "HA-25/B/20" -> gana el de hormigon.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Familias detectables por palabras clave / patrones en el texto.
# El orden importa poco: se puntua cada una por separado.
_FAMILIAS: dict[str, tuple[str, ...]] = {
    "hormigon": ("hormigon", "hormigón", "ha-", "hm-", "hl-", "hne-"),
    "mortero": ("mortero", "m-5", "m-7", "m-10", "m-12", "m-15", "m-20"),
    "residuos": ("residuo", "contenedor", "rcd", "ler", "escombro"),
    "acero": ("acero", "corrugado", "b500", "ferralla", "mallazo"),
    "combustible": ("gasoleo", "gasóleo", "gasolina", "adblue", "carburante"),
    "maquinaria": ("alquiler", "maquina", "máquina", "retro", "grua", "grúa"),
}

# Patron de designacion de hormigon (HA-25/B/20, HM-20...) y mortero (M-5).
_RE_HORMIGON = re.compile(r"\bH[ALMN]E?\s?-?\s?\d{2,3}\b", re.IGNORECASE)
_RE_MORTERO = re.compile(r"\bM-\s?\d{1,2}(?:[.,]\d)?\b", re.IGNORECASE)

# Diferencia minima de puntos sobre el segundo para auto-seleccionar.
_MARGEN_MINIMO = 2
# Puntos minimos del ganador para considerarlo fiable.
_PUNTOS_MINIMOS = 2


def _norm(texto: Any) -> str:
    return str(texto or "").lower()


def _familias_de(texto: str) -> set[str]:
    """Familias detectadas en un texto (por palabras clave y patrones)."""
    fams: set[str] = set()
    for fam, claves in _FAMILIAS.items():
        if any(c in texto for c in claves):
            fams.add(fam)
    if _RE_HORMIGON.search(texto):
        fams.add("hormigon")
    if _RE_MORTERO.search(texto):
        fams.add("mortero")
    return fams


def _texto_albaran(lineas_albaran: Iterable[Any]) -> str:
    partes = []
    for l in lineas_albaran or []:
        partes.append(_norm(getattr(l, "codigo", None) or ""))
        partes.append(_norm(getattr(l, "concepto", None) or ""))
        ctx = getattr(l, "contexto_linea", None)
        if ctx is not None:
            partes.append(_norm(getattr(ctx, "tipo_familia", None) or ""))
            partes.append(
                _norm(getattr(ctx, "descripcion_extendida", None) or "")
            )
    return " ".join(p for p in partes if p)


def _lineas_de(contrato: Any) -> list:
    """Lineas del contrato. OJO: el DTO de Sigrid las llama ``lines``
    (ingles), no ``lineas``. Aceptamos ambos por robustez: si nos
    equivocamos de nombre, el texto del contrato sale VACIO, no se
    detecta ninguna familia y el selector nunca elige (bug jul 2026)."""
    for attr in ("lines", "lineas"):
        valor = getattr(contrato, attr, None)
        if valor:
            return list(valor)
    return []


def _texto_contrato(contrato: Any) -> str:
    partes = [_norm(getattr(contrato, "nombre_contrato", None) or "")]
    for l in _lineas_de(contrato):
        partes.append(_norm(getattr(l, "descripcion_linea", None) or ""))
        partes.append(_norm(getattr(l, "codigo_producto", None) or ""))
    return " ".join(p for p in partes if p)


def elegir_contrato_probable(
    *,
    contratos: list[Any],
    lineas_albaran: Iterable[Any],
    tipologia: Optional[str] = None,
) -> Optional[str]:
    """Devuelve el ``codigo_contrato`` mas probable, o None si no esta claro.

    ``tipologia`` (de sv2: hormigon | mortero | residuos | generico) es la
    senal mas fuerte si viene; si no, se deduce del texto del albaran.
    """
    if not contratos:
        return None
    if len(contratos) == 1:
        return getattr(contratos[0], "codigo_contrato", None)

    txt_alb = _texto_albaran(lineas_albaran)
    fams_alb = _familias_de(txt_alb)
    tip = (tipologia or "").strip().lower()
    if tip and tip not in ("generico", "otro"):
        fams_alb.add(tip)

    if not fams_alb:
        logger.info(
            "[contrato-selector] sin familia detectable en el albaran; "
            "no se auto-selecciona (decide el humano)."
        )
        return None

    puntuados: list[tuple[int, str]] = []
    for c in contratos:
        codigo = getattr(c, "codigo_contrato", None)
        if not codigo:
            continue
        txt_con = _texto_contrato(c)
        fams_con = _familias_de(txt_con)

        # Puntuacion: +3 por cada familia compartida (senal fuerte);
        # -2 si el contrato es claramente de OTRA familia y no comparte
        # ninguna (p.ej. albaran hormigon vs contrato mortero).
        comunes = fams_alb & fams_con
        puntos = 3 * len(comunes)
        if not comunes and fams_con:
            puntos -= 2

        # Refuerzo: designacion tecnica exacta del albaran presente en el
        # contrato (p.ej. "ha-25" aparece en alguna descripcion).
        for token in set(_RE_HORMIGON.findall(txt_alb)) | set(
            _RE_MORTERO.findall(txt_alb)
        ):
            if _norm(token) in txt_con:
                puntos += 2

        puntuados.append((puntos, codigo))
        logger.info(
            "[contrato-selector] %s -> %s puntos (familias contrato=%s, "
            "albaran=%s)",
            codigo,
            puntos,
            sorted(fams_con) or "-",
            sorted(fams_alb) or "-",
        )

    if not puntuados:
        return None

    puntuados.sort(reverse=True)
    mejor_puntos, mejor_codigo = puntuados[0]
    segundo_puntos = puntuados[1][0] if len(puntuados) > 1 else -999

    if mejor_puntos < _PUNTOS_MINIMOS:
        logger.info(
            "[contrato-selector] mejor candidato %s con %s puntos (<%s): "
            "no se auto-selecciona.",
            mejor_codigo, mejor_puntos, _PUNTOS_MINIMOS,
        )
        return None
    if (mejor_puntos - segundo_puntos) < _MARGEN_MINIMO:
        logger.info(
            "[contrato-selector] empate (%s vs %s puntos): no se "
            "auto-selecciona; decide el humano.",
            mejor_puntos, segundo_puntos,
        )
        return None

    logger.info(
        "[contrato-selector] AUTO-SELECCIONADO %s (%s puntos, segundo=%s)",
        mejor_codigo, mejor_puntos, segundo_puntos,
    )
    return mejor_codigo
