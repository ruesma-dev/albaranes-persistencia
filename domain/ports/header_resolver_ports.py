# domain/ports/header_resolver_ports.py
"""Puertos del HeaderResolverService.

El servicio (capa application) depende SOLO de estos Protocols, no de la
infraestructura. Los adaptadores reales:
  - ObraReverseLookupClient      -> SigridApiObraClient.search_obras
  - ProveedorReverseLookupClient -> SigridApiContratoClient
        (search_proveedores + fetch_contratos_resumen_por_obra)
  - HeaderMergeRepository        -> SqlAlchemyAlbaranRepository
cumplen estos puertos por duck-typing.

(jul 2026) Ampliados para la deduccion de proveedor por FAMILIA de
producto + proveedores con contrato en la OBRA:
  - el cliente aporta ``fetch_contratos_resumen_por_obra`` (candidatos
    con el texto de sus contratos para clasificar familia);
  - el repositorio aporta las lineas del albaran para detectar su
    familia (``get_merge_lines_for_scoring``, el mismo metodo que usa el
    selector de contratos) y las notas de revision para avisar al
    revisor (``append_review_note`` / ``remove_review_note_prefix``);
  - ``update_merge_resolved_header`` acepta el ORIGEN del CIF deducido
    ('deterministic' clasico o 'det_familia_obra'), que sv4 usa para
    penalizar la confianza.
"""
from __future__ import annotations

from typing import Protocol

from domain.models.header_resolution_models import (
    MergeHeaderForResolution,
    ProveedorObraResumen,
)
from domain.models.obra_models import ObraEnrichmentResult


class ObraReverseLookupClient(Protocol):
    def search_obras(self) -> list[ObraEnrichmentResult]: ...


class ProveedorReverseLookupClient(Protocol):
    def search_proveedores(self) -> list[tuple[str | None, str | None]]: ...

    def fetch_contratos_resumen_por_obra(
        self, *, codigo_obra: str,
    ) -> list[ProveedorObraResumen]: ...


class HeaderMergeRepository(Protocol):
    def get_merge_header_for_resolution(
        self, *, document_id: str,
    ) -> MergeHeaderForResolution | None: ...

    def update_merge_resolved_header(
        self,
        *,
        document_id: str,
        obra_codigo_det: str | None,
        proveedor_cif_det: str | None,
        proveedor_origen: str = "deterministic",
    ) -> None: ...

    def get_merge_lines_for_scoring(
        self, *, document_id: str,
    ) -> list[dict]: ...

    def append_review_note(
        self, *, document_id: str, nota: str,
    ) -> None: ...

    def remove_review_note_prefix(
        self, *, document_id: str, prefijo: str,
    ) -> None: ...
