# domain/models/contrato_refetch_models.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContratoRefetchOutcome:
    """Resultado de un re-fetch manual de contratos lanzado desde el front.

    Antes este DTO vivía en el sv4 porque era el sv4 quien llamaba a
    Sigrid directamente. Con el refactor "el sv3 es dueño de los
    contratos" pasa a vivir aquí. El sv4 lo recibe como JSON por HTTP
    y lo reenvía al front sin tocarlo.

    Códigos de ``status`` (estables; los conoce el front):

      * ``skipped_missing_data`` — Faltan CIF/obra o no validan. No se
        ha llamado a Sigrid (se ahorra una llamada tonta).
      * ``no_results`` — Sigrid respondió OK pero con 0 contratos para
        la combinación (CIF, obra) actual.
      * ``found_single`` — 1 contrato encontrado → auto-seleccionado.
      * ``found_multiple`` — >1 contratos encontrados → el usuario
        debe elegir en el portal.
      * ``sigrid_error`` — Error de red / 5xx / SQL / etc. en la
        llamada a Sigrid. Los contratos previos (si los había) se
        mantienen intactos.

    ``message`` es texto corto pensado para pintarlo en el portal
    directamente (al lado del banner de "0 contratos encontrados").

    ``cif`` y ``obra_codigo`` son los valores normalizados con los
    que finalmente se buscó (útil para feedback al usuario y para
    diagnosticar problemas de normalización).
    """

    status: str
    count: int
    selected_contrato_codigo: str | None
    message: str
    cif: str | None
    obra_codigo: str | None
