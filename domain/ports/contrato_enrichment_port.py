# domain/ports/contrato_enrichment_port.py
from __future__ import annotations

from typing import Protocol

from domain.models.contrato_models import ContratoEnrichmentResult


class ContratoEnrichmentClient(Protocol):
    """Puerto para clientes que buscan contratos proveedor×obra.

    Devuelve TODOS los contratos que casen. El servicio llamador se
    encarga de decidir cuál persistir/auto-seleccionar.
    """

    def fetch_contratos(
        self,
        *,
        cif_proveedor: str,
        codigo_obra_normalizado: str,
    ) -> list[ContratoEnrichmentResult]:
        """Devuelve la lista (vacía si no hay coincidencias)."""
        ...
