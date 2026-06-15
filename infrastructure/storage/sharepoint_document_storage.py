# infrastructure/storage/sharepoint_document_storage.py
"""Storage de albaranes + artefactos IA en SharePoint (servicio 3).

La mecánica Graph compartida (resolución de site/drive, carpetas, subida
por parent o por path, sharing link) vive ahora en el cliente común
``ruesma_comun.sharepoint.GraphSharePointClient``. Este adaptador añade
SOLO lo propio del servicio 3: la composición de nombres/paths con
prefijo sha256, la subida de los artefactos JSON de IA junto al fichero,
y los dos métodos públicos ``upload`` (albarán) y ``upload_contrato_pdf``
con sus tipos de dominio.

Implementa ``ContratoPdfStorage`` por duck-typing vía ``upload_contrato_pdf``.

Requiere: pip install -e ../comun
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from domain.models.persistence_models import StoredFile
from domain.ports.contrato_pdf_storage_port import StoredContratoPdf
from domain.ports.document_storage import DocumentStorage
from ruesma_comun.sharepoint import GraphSharePointClient, SharePointMode

logger = logging.getLogger(__name__)


class SharePointDocumentStorage(GraphSharePointClient, DocumentStorage):
    """Storage de albaranes + artefactos IA en SharePoint.

    Implementa también ``ContratoPdfStorage`` por duck-typing vía el
    método ``upload_contrato_pdf`` — reutiliza los helpers Graph del
    cliente base (``_ensure_child_folder``, ``_upload_file_by_parent``…).
    """

    def __init__(
        self,
        *,
        graph_key: str,
        timeout_s: int,
        mode: SharePointMode,
        hostname: str | None,
        site_path: str | None,
        drive_name: str,
        drive_id: str | None,
        folder_root: str,
        folder_url: str | None,
        link_type: str,
        link_scope: str,
        create_link: bool,
    ) -> None:
        super().__init__(
            graph_key=graph_key,
            timeout_s=timeout_s,
            mode=mode,
            hostname=hostname,
            site_path=site_path,
            drive_name=drive_name,
            drive_id=drive_id,
            folder_root=folder_root,
            folder_url=folder_url,
            link_type=link_type,
            link_scope=link_scope,
            create_link=create_link,
        )

    def _build_storage_parts(
        self,
        *,
        filename: str,
        source_sha256: str,
        folder_label: str | None,
    ) -> tuple[list[str], str, str]:
        now = datetime.now(timezone.utc)
        safe_name = self._safe_filename(filename)
        prefix = source_sha256[:8]
        final_name = f"{prefix}_{safe_name}"
        relative_parts = [
            self._base_folder_path(folder_label),
            now.strftime("%Y"),
            now.strftime("%m"),
            final_name,
        ]
        subfolders = [now.strftime("%Y"), now.strftime("%m")]
        return subfolders, final_name, str(PurePosixPath(*relative_parts))

    @staticmethod
    def _artifact_name(final_name: str, artifact_type: str) -> str:
        path = PurePosixPath(final_name)
        if artifact_type.startswith("gemini_"):
            return f"{path.stem}.{artifact_type}_GEM.json"
        if artifact_type.startswith("claude_"):
            return f"{path.stem}.{artifact_type}_CLA.json"
        return f"{path.stem}.{artifact_type}.json"

    @staticmethod
    def _json_bytes(payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    def _upload_artifact_by_parent(
        self,
        *,
        drive_id: str,
        parent_item_id: str,
        artifact_name: str,
        payload: dict[str, Any],
    ) -> tuple[str, str | None]:
        uploaded = self._upload_file_by_parent(
            drive_id=drive_id,
            parent_item_id=parent_item_id,
            filename=artifact_name,
            mime_type="application/json",
            file_bytes=self._json_bytes(payload),
        )
        return artifact_name, str(uploaded.get("webUrl") or "").strip() or None

    def _upload_artifact_by_relative_path(
        self,
        *,
        drive_id: str,
        relative_dir: str,
        artifact_name: str,
        payload: dict[str, Any],
    ) -> tuple[str, str | None]:
        artifact_relative_path = str(PurePosixPath(relative_dir) / artifact_name)
        uploaded = self._upload_file_by_relative_path(
            drive_id=drive_id,
            relative_path=artifact_relative_path,
            mime_type="application/json",
            file_bytes=self._json_bytes(payload),
        )
        return artifact_relative_path, str(uploaded.get("webUrl") or "").strip() or None

    # ================================================================ #
    # API pública: upload de albaranes (inalterada)
    # ================================================================ #
    def upload(
        self,
        *,
        filename: str,
        mime_type: str,
        file_bytes: bytes,
        source_sha256: str,
        ia_input_payload: dict[str, Any] | None = None,
        ia_output_payload: dict[str, Any] | None = None,
        gem_input_payload: dict[str, Any] | None = None,
        gem_output_payload: dict[str, Any] | None = None,
        cla_input_payload: dict[str, Any] | None = None,
        cla_output_payload: dict[str, Any] | None = None,
    ) -> StoredFile:
        ia_input_relative_path: str | None = None
        ia_input_web_url: str | None = None
        ia_output_relative_path: str | None = None
        ia_output_web_url: str | None = None
        gem_input_relative_path: str | None = None
        gem_input_web_url: str | None = None
        gem_output_relative_path: str | None = None
        gem_output_web_url: str | None = None
        cla_input_relative_path: str | None = None
        cla_input_web_url: str | None = None
        cla_output_relative_path: str | None = None
        cla_output_web_url: str | None = None

        if self._mode == "drive_id":
            assert self._drive_id is not None
            drive_id = self._drive_id
            base_parent_id = self._ensure_folder_path_from_root(
                drive_id=drive_id,
                folder_path=self._base_folder_path(None),
            )
            subfolders, final_name, relative_path = self._build_storage_parts(
                filename=filename,
                source_sha256=source_sha256,
                folder_label=None,
            )

            parent_id = base_parent_id
            for folder_name in subfolders:
                parent_id = self._ensure_child_folder(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    folder_name=folder_name,
                )

            uploaded = self._upload_file_by_parent(
                drive_id=drive_id,
                parent_item_id=parent_id,
                filename=final_name,
                mime_type=mime_type,
                file_bytes=file_bytes,
            )

            if ia_input_payload:
                ia_input_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "openai_request")
                )
                _, ia_input_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "openai_request"),
                    payload=ia_input_payload,
                )
            if ia_output_payload:
                ia_output_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "openai_response")
                )
                _, ia_output_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "openai_response"),
                    payload=ia_output_payload,
                )
            if gem_input_payload:
                gem_input_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "gemini_request")
                )
                _, gem_input_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "gemini_request"),
                    payload=gem_input_payload,
                )
            if gem_output_payload:
                gem_output_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "gemini_response")
                )
                _, gem_output_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "gemini_response"),
                    payload=gem_output_payload,
                )
            if cla_input_payload:
                cla_input_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "claude_request")
                )
                _, cla_input_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "claude_request"),
                    payload=cla_input_payload,
                )
            if cla_output_payload:
                cla_output_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "claude_response")
                )
                _, cla_output_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "claude_response"),
                    payload=cla_output_payload,
                )

        elif self._mode == "folder_url":
            base_folder = self._resolve_folder_from_share_url()
            subfolders, final_name, relative_path = self._build_storage_parts(
                filename=filename,
                source_sha256=source_sha256,
                folder_label=base_folder.folder_name,
            )

            parent_id = base_folder.item_id
            for folder_name in subfolders:
                parent_id = self._ensure_child_folder(
                    drive_id=base_folder.drive_id,
                    parent_item_id=parent_id,
                    folder_name=folder_name,
                )

            uploaded = self._upload_file_by_parent(
                drive_id=base_folder.drive_id,
                parent_item_id=parent_id,
                filename=final_name,
                mime_type=mime_type,
                file_bytes=file_bytes,
            )
            drive_id = base_folder.drive_id

            if ia_input_payload:
                ia_input_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "openai_request")
                )
                _, ia_input_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "openai_request"),
                    payload=ia_input_payload,
                )
            if ia_output_payload:
                ia_output_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "openai_response")
                )
                _, ia_output_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "openai_response"),
                    payload=ia_output_payload,
                )
            if gem_input_payload:
                gem_input_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "gemini_request")
                )
                _, gem_input_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "gemini_request"),
                    payload=gem_input_payload,
                )
            if gem_output_payload:
                gem_output_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "gemini_response")
                )
                _, gem_output_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "gemini_response"),
                    payload=gem_output_payload,
                )
            if cla_input_payload:
                cla_input_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "claude_request")
                )
                _, cla_input_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "claude_request"),
                    payload=cla_input_payload,
                )
            if cla_output_payload:
                cla_output_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "claude_response")
                )
                _, cla_output_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "claude_response"),
                    payload=cla_output_payload,
                )

        else:
            site_id = self._resolve_site_id()
            drive_id = self._resolve_drive_id(site_id)
            _, final_name, relative_path = self._build_storage_parts(
                filename=filename,
                source_sha256=source_sha256,
                folder_label=None,
            )
            uploaded = self._upload_file_by_relative_path(
                drive_id=drive_id,
                relative_path=relative_path,
                mime_type=mime_type,
                file_bytes=file_bytes,
            )
            relative_dir = str(PurePosixPath(relative_path).parent)
            if ia_input_payload:
                ia_input_relative_path, ia_input_web_url = (
                    self._upload_artifact_by_relative_path(
                        drive_id=drive_id,
                        relative_dir=relative_dir,
                        artifact_name=self._artifact_name(final_name, "openai_request"),
                        payload=ia_input_payload,
                    )
                )
            if ia_output_payload:
                ia_output_relative_path, ia_output_web_url = (
                    self._upload_artifact_by_relative_path(
                        drive_id=drive_id,
                        relative_dir=relative_dir,
                        artifact_name=self._artifact_name(final_name, "openai_response"),
                        payload=ia_output_payload,
                    )
                )
            if gem_input_payload:
                gem_input_relative_path, gem_input_web_url = (
                    self._upload_artifact_by_relative_path(
                        drive_id=drive_id,
                        relative_dir=relative_dir,
                        artifact_name=self._artifact_name(final_name, "gemini_request"),
                        payload=gem_input_payload,
                    )
                )
            if gem_output_payload:
                gem_output_relative_path, gem_output_web_url = (
                    self._upload_artifact_by_relative_path(
                        drive_id=drive_id,
                        relative_dir=relative_dir,
                        artifact_name=self._artifact_name(final_name, "gemini_response"),
                        payload=gem_output_payload,
                    )
                )
            if cla_input_payload:
                cla_input_relative_path, cla_input_web_url = (
                    self._upload_artifact_by_relative_path(
                        drive_id=drive_id,
                        relative_dir=relative_dir,
                        artifact_name=self._artifact_name(final_name, "claude_request"),
                        payload=cla_input_payload,
                    )
                )
            if cla_output_payload:
                cla_output_relative_path, cla_output_web_url = (
                    self._upload_artifact_by_relative_path(
                        drive_id=drive_id,
                        relative_dir=relative_dir,
                        artifact_name=self._artifact_name(final_name, "claude_response"),
                        payload=cla_output_payload,
                    )
                )

        item_id = str(uploaded.get("id") or "").strip()
        if not item_id:
            raise RuntimeError("SharePoint no devolvió driveItem.id.")

        web_url = str(uploaded.get("webUrl") or "").strip() or None
        share_url = None
        if self._create_link:
            share_url = self._create_share_link(
                drive_id=drive_id,
                item_id=item_id,
            )

        return StoredFile(
            drive_id=drive_id,
            item_id=item_id,
            relative_path=relative_path,
            web_url=web_url,
            share_url=share_url or web_url,
            ia_input_relative_path=ia_input_relative_path,
            ia_input_web_url=ia_input_web_url,
            ia_output_relative_path=ia_output_relative_path,
            ia_output_web_url=ia_output_web_url,
            gem_input_relative_path=gem_input_relative_path,
            gem_input_web_url=gem_input_web_url,
            gem_output_relative_path=gem_output_relative_path,
            gem_output_web_url=gem_output_web_url,
            cla_input_relative_path=cla_input_relative_path,
            cla_input_web_url=cla_input_web_url,
            cla_output_relative_path=cla_output_relative_path,
            cla_output_web_url=cla_output_web_url,
        )

    # ================================================================ #
    # API pública: upload de PDFs de contrato
    # ================================================================ #
    def upload_contrato_pdf(
        self,
        *,
        filename: str,
        file_bytes: bytes,
        codigo_contrato: str,
        gra_rep_ide: int,
    ) -> StoredContratoPdf:
        """Sube un PDF de contrato a ``<base>/<YYYY>/<MM>/contratos/``.

        El nombre final es ``<codigo_contrato>_<gra_rep_ide>_<safe_name>.pdf``
        — determinista para el par (codigo, ide). Si Sigrid cambia el
        ``gra_rep_ide`` del contrato (nueva versión del PDF) se sube un
        archivo nuevo con otro nombre, no se sobrescribe el antiguo.

        NO lanza en fallos internos: si algo del upload falla, propaga
        la excepción al orquestador. Este método solo hace el mecánico.
        """
        if not file_bytes:
            raise ValueError("upload_contrato_pdf: file_bytes vacío.")

        # Construcción del nombre final determinista.
        safe_codigo = self._safe_segment(codigo_contrato, fallback="contrato")
        safe_name = self._safe_filename(filename or f"contrato_{gra_rep_ide}.pdf")
        if not safe_name.lower().endswith(".pdf"):
            safe_name = f"{safe_name}.pdf"
        final_name = f"{safe_codigo}_{gra_rep_ide}_{safe_name}"

        # Carpeta destino: <base>/<YYYY>/<MM>/contratos/
        now = datetime.now(timezone.utc)
        year = now.strftime("%Y")
        month = now.strftime("%m")

        logger.info(
            "[contrato-pdf][sp] upload_contrato_pdf INICIO "
            "codigo=%s gra_rep_ide=%s bytes=%s final_name=%s",
            codigo_contrato,
            gra_rep_ide,
            len(file_bytes),
            final_name,
        )

        if self._mode == "drive_id":
            assert self._drive_id is not None
            drive_id = self._drive_id
            base_parent_id = self._ensure_folder_path_from_root(
                drive_id=drive_id,
                folder_path=self._base_folder_path(None),
            )
            year_id = self._ensure_child_folder(
                drive_id=drive_id,
                parent_item_id=base_parent_id,
                folder_name=year,
            )
            month_id = self._ensure_child_folder(
                drive_id=drive_id,
                parent_item_id=year_id,
                folder_name=month,
            )
            contratos_id = self._ensure_child_folder(
                drive_id=drive_id,
                parent_item_id=month_id,
                folder_name="contratos",
            )
            uploaded = self._upload_file_by_parent(
                drive_id=drive_id,
                parent_item_id=contratos_id,
                filename=final_name,
                mime_type="application/pdf",
                file_bytes=file_bytes,
            )
            relative_path = str(
                PurePosixPath(self._base_folder_path(None))
                / year
                / month
                / "contratos"
                / final_name
            )

        elif self._mode == "folder_url":
            base_folder = self._resolve_folder_from_share_url()
            drive_id = base_folder.drive_id
            year_id = self._ensure_child_folder(
                drive_id=drive_id,
                parent_item_id=base_folder.item_id,
                folder_name=year,
            )
            month_id = self._ensure_child_folder(
                drive_id=drive_id,
                parent_item_id=year_id,
                folder_name=month,
            )
            contratos_id = self._ensure_child_folder(
                drive_id=drive_id,
                parent_item_id=month_id,
                folder_name="contratos",
            )
            uploaded = self._upload_file_by_parent(
                drive_id=drive_id,
                parent_item_id=contratos_id,
                filename=final_name,
                mime_type="application/pdf",
                file_bytes=file_bytes,
            )
            relative_path = str(
                PurePosixPath(base_folder.folder_name or "albaranes")
                / year
                / month
                / "contratos"
                / final_name
            )

        else:
            site_id = self._resolve_site_id()
            drive_id = self._resolve_drive_id(site_id)
            relative_path = str(
                PurePosixPath(self._base_folder_path(None))
                / year
                / month
                / "contratos"
                / final_name
            )
            uploaded = self._upload_file_by_relative_path(
                drive_id=drive_id,
                relative_path=relative_path,
                mime_type="application/pdf",
                file_bytes=file_bytes,
            )

        web_url = str(uploaded.get("webUrl") or "").strip() or None
        logger.info(
            "[contrato-pdf][sp] upload_contrato_pdf OK relative_path=%s web_url=%s",
            relative_path,
            web_url,
        )
        return StoredContratoPdf(
            relative_path=relative_path,
            web_url=web_url,
        )

    # ================================================================ #
    # API pública: subida del Markdown del contrato (junto al PDF)
    # ================================================================ #
    def upload_contrato_md(
        self,
        *,
        markdown: str,
        codigo_contrato: str,
        gra_rep_ide: int,
    ) -> StoredContratoPdf:
        """Sube el Markdown del contrato a la MISMA carpeta que el PDF
        (``<base>/<YYYY>/<MM>/contratos/``), con nombre paralelo
        ``<codigo>_<ide>_contrato.md``. Reutiliza toda la mecánica de
        carpetas/subida del cliente base.

        Devuelve ``StoredContratoPdf`` (relative_path + web_url); el campo
        de tipo da igual: es un fichero almacenado con su ruta.
        """
        if not markdown:
            raise ValueError("upload_contrato_md: markdown vacío.")

        safe_codigo = self._safe_segment(codigo_contrato, fallback="contrato")
        final_name = f"{safe_codigo}_{gra_rep_ide}_contrato.md"
        file_bytes = markdown.encode("utf-8")
        now = datetime.now(timezone.utc)
        year = now.strftime("%Y")
        month = now.strftime("%m")

        if self._mode == "drive_id":
            assert self._drive_id is not None
            drive_id = self._drive_id
            base_parent_id = self._ensure_folder_path_from_root(
                drive_id=drive_id,
                folder_path=self._base_folder_path(None),
            )
            year_id = self._ensure_child_folder(
                drive_id=drive_id, parent_item_id=base_parent_id, folder_name=year
            )
            month_id = self._ensure_child_folder(
                drive_id=drive_id, parent_item_id=year_id, folder_name=month
            )
            contratos_id = self._ensure_child_folder(
                drive_id=drive_id, parent_item_id=month_id, folder_name="contratos"
            )
            uploaded = self._upload_file_by_parent(
                drive_id=drive_id,
                parent_item_id=contratos_id,
                filename=final_name,
                mime_type="text/markdown",
                file_bytes=file_bytes,
            )
            relative_path = str(
                PurePosixPath(self._base_folder_path(None))
                / year / month / "contratos" / final_name
            )

        elif self._mode == "folder_url":
            base_folder = self._resolve_folder_from_share_url()
            drive_id = base_folder.drive_id
            year_id = self._ensure_child_folder(
                drive_id=drive_id, parent_item_id=base_folder.item_id, folder_name=year
            )
            month_id = self._ensure_child_folder(
                drive_id=drive_id, parent_item_id=year_id, folder_name=month
            )
            contratos_id = self._ensure_child_folder(
                drive_id=drive_id, parent_item_id=month_id, folder_name="contratos"
            )
            uploaded = self._upload_file_by_parent(
                drive_id=drive_id,
                parent_item_id=contratos_id,
                filename=final_name,
                mime_type="text/markdown",
                file_bytes=file_bytes,
            )
            relative_path = str(
                PurePosixPath(base_folder.folder_name or "albaranes")
                / year / month / "contratos" / final_name
            )

        else:
            site_id = self._resolve_site_id()
            drive_id = self._resolve_drive_id(site_id)
            relative_path = str(
                PurePosixPath(self._base_folder_path(None))
                / year / month / "contratos" / final_name
            )
            uploaded = self._upload_file_by_relative_path(
                drive_id=drive_id,
                relative_path=relative_path,
                mime_type="text/markdown",
                file_bytes=file_bytes,
            )

        web_url = str(uploaded.get("webUrl") or "").strip() or None
        logger.info(
            "[contrato-md][sp] upload_contrato_md OK relative_path=%s web_url=%s",
            relative_path,
            web_url,
        )
        return StoredContratoPdf(relative_path=relative_path, web_url=web_url)
