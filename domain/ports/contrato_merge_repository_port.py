# domain/ports/contrato_merge_repository_port.py
from __future__ import annotations

from typing import Protocol

from domain.models.contrato_models import ContratoEnrichmentResult


class ContratoMergeRepository(Protocol):
    """Puerto mínimo que necesitan los servicios de
    enrichment/refetch de contratos.

    ``SqlAlchemyAlbaranRepository`` lo cumple por duck-typing al exponer
    estos métodos. Cubre dos casos de uso:

      A) Enrichment automático (pipeline): se ejecuta cuando llega un
         albarán nuevo y lee CIF + obra del envelope.
      B) Refetch manual (portal): se ejecuta cuando el revisor cambia
         CIF u obra y pulsa "Volver a buscar".

    Ambos casos comparten primitiva: ``upsert_contratos`` (UPSERT por
    ``sigrid_ide``, idempotente). La auto-selección post-refetch usa
    ``set_selected_contrato`` y se consulta el resultado con
    ``get_selected_contrato_codigo``.
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

    def upsert_contratos(
        self,
        *,
        document_id: str,
        contratos: list[ContratoEnrichmentResult],
    ) -> None:
        """UPSERT de cabeceras + líneas por ``sigrid_ide``.

        Cuando un contrato ya existe en BBDD con el mismo ``sigrid_ide``,
        se actualiza con los datos frescos (incluido ``document_id``,
        que pasa al del albarán más reciente). Cuando no existe, se
        inserta. NUNCA se borra: si Sigrid quita una línea de un
        contrato, en BBDD se conserva por seguridad.

        Persiste ``gra_rep_ide`` pero NO los paths de SharePoint —
        esos se actualizan después vía ``update_contrato_pdf_paths``.
        """
        ...

    def replace_contratos(
        self,
        *,
        document_id: str,
        contratos: list[ContratoEnrichmentResult],
    ) -> None:
        """Alias DEPRECADO de :meth:`upsert_contratos`.

        Se mantiene en el port por compatibilidad con código antiguo
        que aún lo invoque. La implementación debe delegar en
        ``upsert_contratos`` (eso es lo que hace el repo concreto).
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

    def get_selected_contrato_codigo(
        self,
        *,
        document_id: str,
    ) -> str | None:
        """Devuelve el código del contrato actualmente seleccionado.

        ``None`` si no hay selección (ej. el albarán tiene 0 ó >1
        contratos y aún no se ha elegido manualmente). Levanta ``KeyError``
        si el documento no existe en ``albaran_documents_merge``.
        """
        ...

    def update_merge_proveedor_nombre(
        self,
        *,
        document_id: str,
        nombre_proveedor: str,
    ) -> bool:
        """Sobrescribe ``proveedor_nombre`` de la cabecera con la razón
        social canónica de Sigrid (``prv.raz``) obtenida al resolver el
        contrato por CIF. Devuelve True si cambió el valor, False si fue
        no-op (nombre vacío o idéntico). Levanta ``KeyError`` si el
        documento no existe.
        """
        ...
