# domain/ports/contrato_merge_repository_port.py
from __future__ import annotations

from typing import Protocol

from domain.models.contrato_models import ContratoEnrichmentResult


class ContratoMergeRepository(Protocol):
    """Puerto mínimo que necesita ``ContratoEnrichmentService``.

    ``SqlAlchemyAlbaranRepository`` lo cumple por duck-typing al exponer
    estos métodos. El enrichment es idempotente: cada ejecución borra
    los contratos anteriores del documento y vuelve a insertarlos.

    Para soporte de PDFs del contrato, incluye dos operaciones que el
    orquestador usa para hacer reutilización de PDFs ya subidos:

      1. ``get_existing_pdf_paths`` — ANTES del replace, para saber qué
         PDFs ya teníamos y evitar descarga+subida si ``gra_rep_ide`` no
         cambió.
      2. ``update_contrato_pdf_paths`` — DESPUÉS del replace + subida,
         para persistir ``pdf_sharepoint_relative_path`` y
         ``pdf_sharepoint_web_url`` en la cabecera del contrato.
    """

    def get_merge_cif_and_obra(
        self,
        *,
        document_id: str,
    ) -> tuple[str | None, str | None]:
        """Devuelve (proveedor_cif, obra_codigo) del merge, o (None, None)."""
        ...

    def get_existing_pdf_paths(
        self,
        *,
        document_id: str,
    ) -> dict[str, tuple[int | None, str | None, str | None]]:
        """Mapa ``codigo_contrato → (gra_rep_ide, relative_path, web_url)``.

        Usado para evitar re-subir el mismo PDF si la versión no ha
        cambiado. Si el documento no tiene contratos devuelve ``{}``.
        """
        ...

    def replace_contratos(
        self,
        *,
        document_id: str,
        contratos: list[ContratoEnrichmentResult],
    ) -> None:
        """Borra los contratos existentes del doc e inserta los nuevos.

        Debe commitear dentro del método. Persiste también ``gra_rep_ide``
        pero NO los paths de SharePoint — esos se actualizan después
        vía ``update_contrato_pdf_paths`` para que la subida del PDF
        (lenta y fallable) quede fuera de la transacción de replace.
        """
        ...

    def update_contrato_pdf_paths(
        self,
        *,
        document_id: str,
        codigo_contrato: str,
        relative_path: str | None,
        web_url: str | None,
    ) -> None:
        """Actualiza los paths del PDF para un contrato concreto."""
        ...

    def set_selected_contrato(
        self,
        *,
        document_id: str,
        codigo_contrato: str | None,
    ) -> None:
        """Fija selected_contrato_codigo en el merge. None = deseleccionar."""
        ...
