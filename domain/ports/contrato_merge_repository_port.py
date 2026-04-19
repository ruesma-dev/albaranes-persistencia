# domain/ports/contrato_merge_repository_port.py
from __future__ import annotations

from typing import Protocol

from domain.models.contrato_models import ContratoEnrichmentResult


class ContratoMergeRepository(Protocol):
    """Puerto mínimo que necesita ``ContratoEnrichmentService``.

    ``SqlAlchemyAlbaranRepository`` lo cumple por duck-typing al exponer
    estos tres métodos. El enrichment es idempotente: cada ejecución
    borra los contratos anteriores del documento y vuelve a insertarlos.
    """

    def get_merge_cif_and_obra(
        self,
        *,
        document_id: str,
    ) -> tuple[str | None, str | None]:
        """Devuelve (proveedor_cif, obra_codigo) del merge, o (None, None).

        El servicio llamador se encarga de normalizar el obra_codigo y
        de validar que ambos valores son no vacíos antes de llamar a
        Sigrid.
        """
        ...

    def replace_contratos(
        self,
        *,
        document_id: str,
        contratos: list[ContratoEnrichmentResult],
    ) -> None:
        """Borra los contratos existentes del doc e inserta los nuevos.

        Debe commitear dentro del método.
        """
        ...

    def set_selected_contrato(
        self,
        *,
        document_id: str,
        codigo_contrato: str | None,
    ) -> None:
        """Fija selected_contrato_codigo en el merge. None = deseleccionar."""
        ...
