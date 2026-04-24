# application/services/contexto_linea_merger.py
"""Utilidad para fusionar los ``contexto_linea`` que vienen de los 3
proveedores LLM (OpenAI, Gemini, Claude) en un único contexto final
que se persiste en ``albaran_lines_merge``.

Regla: elegimos el contexto "más rico" (más campos rellenos). En caso
de empate, aplicamos el orden canónico del sistema
(openai → gemini → claude).

Preferimos seleccionar UN contexto íntegro (el del proveedor que mejor
lo ha capturado) en lugar de hacer merge campo-a-campo, porque los
campos están correlacionados semánticamente: un ``rol_linea`` puesto
por OpenAI encaja con la ``descripcion_extendida`` de OpenAI, no con
la de Claude. Fusionarlos podría producir contextos incoherentes.
"""
from __future__ import annotations

from typing import Optional

from domain.models.contexto_linea import ContextoLinea


def _score_contexto(ctx: Optional[ContextoLinea]) -> int:
    """Cuenta cuántos campos informativos tiene el contexto. 0 si None."""
    if ctx is None:
        return 0
    score = 0
    if ctx.tipo_familia is not None:
        score += 1
    if ctx.rol_linea is not None:
        score += 1
    if ctx.descripcion_extendida and ctx.descripcion_extendida.strip():
        score += 1
    if ctx.notas_tiempo and ctx.notas_tiempo.strip():
        score += 1
    if ctx.ref_linea_base is not None:
        score += 1
    return score


def pick_best_contexto_linea(
    *,
    openai_ctx: Optional[ContextoLinea] = None,
    gemini_ctx: Optional[ContextoLinea] = None,
    claude_ctx: Optional[ContextoLinea] = None,
) -> Optional[ContextoLinea]:
    """Elige el contexto_linea más completo de los tres proveedores.

    Devuelve ``None`` si ninguno aporta información útil. Tolera
    silenciosamente proveedores deshabilitados (basta con no pasar
    el argumento o pasar ``None``).
    """
    candidates: list[tuple[int, int, ContextoLinea]] = []

    for order, ctx in enumerate((openai_ctx, gemini_ctx, claude_ctx)):
        score = _score_contexto(ctx)
        if score > 0 and ctx is not None:
            candidates.append((score, order, ctx))

    if not candidates:
        return None

    # Score DESC, orden canónico ASC.
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2]
