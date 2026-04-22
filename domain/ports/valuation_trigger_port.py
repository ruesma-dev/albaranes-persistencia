# domain/ports/valuation_trigger_port.py
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ValuationSyncResult:
    """Resultado de una valoración síncrona vía /re-run.

    Refleja el ``RunValuationResult`` que devuelve el servicio 6. Los
    campos son opcionales por robustez: si el svc 6 cambia su schema
    nos limitamos a devolver lo que hemos podido parsear.
    """

    accepted: bool
    http_status: int
    valuation_id: str | None = None
    status: str | None = None
    total_valorado: float | None = None
    total_lines: int | None = None
    review_required: bool | None = None
    error: str | None = None


class ValuationTrigger(ABC):
    """Puerto para disparar la valoración tras la persistencia.

    Dos modos:
      - ``trigger_async``: fire-and-forget. Llama a
        ``/v1/valuation/run-async`` del svc 6, que devuelve 202 y
        procesa en background. Ideal para el trigger automático del
        servicio 3 al terminar /persist.
      - ``trigger_sync``: bloqueante. Llama a
        ``/v1/valuation/{doc}/re-run`` del svc 6 y espera a que acabe.
        Ideal cuando el front pulsa 'Valorar' con wait_for_valuation=True.

    Ninguno propaga excepciones de red: las capturan, loguean y
    devuelven un resultado que permite al orquestador decidir.
    """

    @abstractmethod
    def trigger_async(
        self,
        *,
        document_id: str,
        codigo_contrato: str | None,
        force: bool = False,
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    def trigger_sync(
        self,
        *,
        document_id: str,
        codigo_contrato: str | None,
    ) -> ValuationSyncResult:
        raise NotImplementedError
