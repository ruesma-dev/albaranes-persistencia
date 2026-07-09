# infrastructure/clients/cola_valuation_trigger.py
"""Trigger de valoración por COLA (sustituye al HttpValuationTrigger).

El pipeline de persistencia llama a ``valuation_trigger.trigger_async`` cuando
decide que hay que valorar (p. ej. contrato único auto-seleccionado). En el
modelo de colas eso es publicar un mensaje en ``q-valoracion`` en vez de llamar
a sv6 por HTTP. El pipeline no cambia: solo se le inyecta este adaptador.
"""
from __future__ import annotations

import logging

from domain.ports.valuation_trigger_port import (
    ValuationSyncResult,
    ValuationTrigger,
)
from ruesma_comun.colas import COLA_VALORACION, MensajeValoracion, PublicadorColas

logger = logging.getLogger(__name__)


class ColaValuationTrigger(ValuationTrigger):
    """Publica ``MensajeValoracion`` en ``q-valoracion``."""

    def __init__(self, publicador: PublicadorColas) -> None:
        self._publicador = publicador

    def trigger_async(
        self,
        *,
        document_id: str,
        codigo_contrato: str | None,
        force: bool = False,
    ) -> bool:
        try:
            self._publicador.publicar(
                COLA_VALORACION,
                MensajeValoracion(
                    document_id=document_id,
                    codigo_contrato=codigo_contrato,
                    force=force,
                ),
            )
            return True
        except Exception:  # noqa: BLE001 — no propagar (igual que el HTTP)
            logger.exception(
                "[cola-trigger] no se pudo publicar valoracion document_id=%s",
                document_id,
            )
            return False

    def trigger_sync(
        self,
        *,
        document_id: str,
        codigo_contrato: str | None,
    ) -> ValuationSyncResult:
        # En modo cola la valoración es asíncrona; no hay equivalente
        # síncrono. El front que necesite "valorar y esperar" debe sondear
        # el estado del workflow, no bloquear aquí.
        logger.warning(
            "[cola-trigger] trigger_sync no soportado en modo cola "
            "(document_id=%s); usa trigger_async.",
            document_id,
        )
        return ValuationSyncResult(
            accepted=False,
            http_status=0,
            error="trigger_sync no soportado en modo cola",
        )
