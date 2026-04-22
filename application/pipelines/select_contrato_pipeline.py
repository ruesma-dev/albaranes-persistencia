# application/pipelines/select_contrato_pipeline.py
from __future__ import annotations

import logging
from dataclasses import dataclass

from domain.ports.valuation_trigger_port import (
    ValuationSyncResult,
    ValuationTrigger,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SelectContratoRequest:
    document_id: str
    codigo_contrato: str | None   # None = deseleccionar
    trigger_valuation: bool = True
    wait_for_valuation: bool = False   # True → bloqueante sync


@dataclass(frozen=True)
class SelectContratoResult:
    ok: bool
    document_id: str
    previous_selected_contrato_codigo: str | None
    selected_contrato_codigo: str | None
    valuation_triggered: bool
    valuation_mode: str | None          # 'async' | 'sync' | None
    valuation_async_accepted: bool | None
    valuation_sync: ValuationSyncResult | None


class SelectContratoPipeline:
    """Cambia ``selected_contrato_codigo`` y (opcional) dispara valoración.

    Usado por ``PATCH /v1/albaranes/{document_id}/selected-contrato``.

    Flujo:
      1. Lee el valor actual (para devolverlo en la respuesta y poder
         detectar si realmente hay un cambio).
      2. Valida que el código nuevo, si no es null, corresponde a un
         contrato existente para ese documento
         (``albaran_contratos_merge``).
      3. Hace UPDATE.
      4. Si ``trigger_valuation`` y el nuevo código no es null, dispara
         la valoración en el modo pedido (async o sync).

    Las excepciones se propagan al endpoint para que pueda devolver
    404/400/500 en función del tipo. La valoración SÍ es best-effort:
    si falla, se devuelve ``valuation_triggered=False`` pero el
    UPDATE ya está commiteado.
    """

    def __init__(
        self,
        *,
        repository,
        valuation_trigger: ValuationTrigger | None,
    ) -> None:
        self._repo = repository
        self._trigger = valuation_trigger

    def run(self, request: SelectContratoRequest) -> SelectContratoResult:
        document_id = request.document_id
        new_codigo = (
            request.codigo_contrato.strip()
            if request.codigo_contrato
            else None
        ) or None

        # 1. Leer estado actual
        previous = self._repo.get_selected_contrato_codigo(
            document_id=document_id,
        )

        # 2. Validar que el contrato existe para este documento
        if new_codigo is not None:
            if not self._repo.contrato_exists_for_document(
                document_id=document_id,
                codigo_contrato=new_codigo,
            ):
                raise ValueError(
                    f"El contrato '{new_codigo}' no existe para el "
                    f"documento {document_id}."
                )

        # 3. Persistir cambio
        self._repo.set_selected_contrato(
            document_id=document_id,
            codigo_contrato=new_codigo,
        )
        logger.info(
            "[select-contrato] document_id=%s %s -> %s",
            document_id, previous, new_codigo,
        )

        # 4. Disparo de valoración
        if (
            not request.trigger_valuation
            or new_codigo is None
            or self._trigger is None
        ):
            logger.info(
                "[select-contrato] SKIP valoración. trigger=%s new=%s "
                "service=%s",
                request.trigger_valuation,
                new_codigo,
                "present" if self._trigger is not None else "None",
            )
            return SelectContratoResult(
                ok=True,
                document_id=document_id,
                previous_selected_contrato_codigo=previous,
                selected_contrato_codigo=new_codigo,
                valuation_triggered=False,
                valuation_mode=None,
                valuation_async_accepted=None,
                valuation_sync=None,
            )

        if request.wait_for_valuation:
            sync_result = self._trigger.trigger_sync(
                document_id=document_id,
                codigo_contrato=new_codigo,
            )
            return SelectContratoResult(
                ok=True,
                document_id=document_id,
                previous_selected_contrato_codigo=previous,
                selected_contrato_codigo=new_codigo,
                valuation_triggered=sync_result.accepted,
                valuation_mode="sync",
                valuation_async_accepted=None,
                valuation_sync=sync_result,
            )

        accepted = self._trigger.trigger_async(
            document_id=document_id,
            codigo_contrato=new_codigo,
            force=True,   # cambio de contrato ⇒ re-valorar siempre
        )
        return SelectContratoResult(
            ok=True,
            document_id=document_id,
            previous_selected_contrato_codigo=previous,
            selected_contrato_codigo=new_codigo,
            valuation_triggered=accepted,
            valuation_mode="async",
            valuation_async_accepted=accepted,
            valuation_sync=None,
        )
