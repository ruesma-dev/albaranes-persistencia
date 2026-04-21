# domain/ports/contrato_enrichment_port.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from domain.models.contrato_models import ContratoEnrichmentResult


@dataclass(frozen=True)
class ContratoPdfPayload:
    """Binario + nombre original del PDF descargado de Sigrid.

    El nombre viene del header ``X-Document-Filename`` del endpoint
    ``/api/documents/read`` cuando existe, o del fallback que decida
    el adaptador (p.e. ``contrato_<codigo>.pdf``).
    """

    filename: str
    content: bytes
    content_type: str | None


class ContratoEnrichmentClient(Protocol):
    """Puerto para el cliente que consulta contratos en el ERP on-prem.

    Implementado por ``infrastructure.sigrid.sigrid_api_contrato_client``.
    """

    def fetch_contratos(
        self,
        *,
        cif_proveedor: str,
        codigo_obra_normalizado: str,
    ) -> list[ContratoEnrichmentResult]:
        """Devuelve la lista de contratos que casen, vacía si no hay."""
        ...

    def download_contrato_pdf(
        self,
        *,
        gra_rep_ide: int,
    ) -> ContratoPdfPayload | None:
        """Descarga el PDF del contrato en ``ruesma_rep.gra`` por ``ide``.

        Devuelve ``None`` si Sigrid no encuentra el documento o si el
        binario viene vacío. NO lanza excepción en errores de negocio;
        sí la lanza en errores de transporte (el orquestador es quien
        decide si continuar o abortar).
        """
        ...
