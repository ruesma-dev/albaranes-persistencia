# domain/ports/obra_enrichment_port.py
from __future__ import annotations

from typing import Protocol

from domain.models.obra_models import ObraEnrichmentResult


class ObraEnrichmentClient(Protocol):
    """Puerto para clientes que resuelven datos de obra desde sistemas externos.

    El adaptador de referencia es ``SigridApiObraClient``, que llama a la
    Function App ``sigrid-api`` contra SQL Server on-prem. Cualquier otra
    fuente (CRM, SAP, API interna) encaja aquí implementando este método.
    """

    def fetch_obra_by_codigo(
        self,
        *,
        codigo_obra_normalizado: str,
    ) -> ObraEnrichmentResult | None:
        """Devuelve los datos de la obra o ``None`` si no existe.

        Args:
            codigo_obra_normalizado: Código en formato canónico (4 dígitos
                con 0 inicial). Las implementaciones NO deben re-normalizar.

        Raises:
            Exception: Cualquier error de red, HTTP o parseo. El servicio
                llamador hace ``try/except`` y tolera fallos.
        """
        ...
