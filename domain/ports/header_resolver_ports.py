# domain/ports/header_resolver_ports.py
"""Puertos del HeaderResolverService.

El servicio (capa application) depende SOLO de estos Protocols, no de la
infraestructura. Los adaptadores reales:
  - ObraReverseLookupClient   -> SigridApiObraClient.search_obras
  - ProveedorReverseLookupClient -> SigridApiContratoClient.search_proveedores
  - HeaderMergeRepository      -> SqlAlchemyAlbaranRepository
cumplen estos puertos por duck-typing.
"""
from __future__ import annotations

from typing import Protocol

from domain.models.header_resolution_models import MergeHeaderForResolution
from domain.models.obra_models import ObraEnrichmentResult


class ObraReverseLookupClient(Protocol):
    def search_obras(self) -> list[ObraEnrichmentResult]: ...


class ProveedorReverseLookupClient(Protocol):
    def search_proveedores(self) -> list[tuple[str | None, str | None]]: ...


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
    ) -> None: ...
