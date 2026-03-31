# infrastructure/database/sqlalchemy_albaran_repository.py
from __future__ import annotations

import json
import uuid
from typing import Any, Dict

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from domain.models.extraction_models import ExtractionEnvelope
from domain.models.persistence_models import ExistingDocument, StoredFile
from domain.ports.albaran_repository import AlbaranRepository
from infrastructure.database.orm_models import (
    AlbaranDocumentOrm,
    AlbaranLineOrm,
    Base,
)
from infrastructure.database.session_factory import SessionFactory


class SqlAlchemyAlbaranRepository(AlbaranRepository):
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._initialized_generation: int | None = None

    def initialize(self) -> None:
        self._session_factory.ensure_database_and_engine()
        current_generation = self._session_factory.generation
        if self._initialized_generation == current_generation:
            return

        Base.metadata.create_all(self._session_factory.engine)
        self._ensure_compatible_schema()
        self._initialized_generation = current_generation

    def _ensure_compatible_schema(self) -> None:
        alter_statements = [
            (
                "source_attachment_filename",
                "ALTER TABLE albaran_documents "
                "ADD COLUMN IF NOT EXISTS source_attachment_filename VARCHAR(255)",
            ),
            (
                "source_attachment_mime_type",
                "ALTER TABLE albaran_documents "
                "ADD COLUMN IF NOT EXISTS source_attachment_mime_type VARCHAR(255)",
            ),
            (
                "source_attachment_sha256",
                "ALTER TABLE albaran_documents "
                "ADD COLUMN IF NOT EXISTS source_attachment_sha256 VARCHAR(64)",
            ),
            (
                "page_number",
                "ALTER TABLE albaran_documents "
                "ADD COLUMN IF NOT EXISTS page_number INTEGER",
            ),
            (
                "page_count",
                "ALTER TABLE albaran_documents "
                "ADD COLUMN IF NOT EXISTS page_count INTEGER",
            ),
            (
                "ia_input_json",
                "ALTER TABLE albaran_documents "
                "ADD COLUMN IF NOT EXISTS ia_input_json TEXT",
            ),
            (
                "ia_output_json",
                "ALTER TABLE albaran_documents "
                "ADD COLUMN IF NOT EXISTS ia_output_json TEXT",
            ),
            (
                "ia_input_relative_path",
                "ALTER TABLE albaran_documents "
                "ADD COLUMN IF NOT EXISTS ia_input_relative_path VARCHAR(1024)",
            ),
            (
                "ia_input_web_url",
                "ALTER TABLE albaran_documents "
                "ADD COLUMN IF NOT EXISTS ia_input_web_url VARCHAR(1024)",
            ),
            (
                "ia_output_relative_path",
                "ALTER TABLE albaran_documents "
                "ADD COLUMN IF NOT EXISTS ia_output_relative_path VARCHAR(1024)",
            ),
            (
                "ia_output_web_url",
                "ALTER TABLE albaran_documents "
                "ADD COLUMN IF NOT EXISTS ia_output_web_url VARCHAR(1024)",
            ),
            (
                "raw_context_json",
                "ALTER TABLE albaran_documents "
                "ADD COLUMN IF NOT EXISTS raw_context_json TEXT",
            ),
        ]
        with self._session_factory.create_session() as session:
            for _, ddl in alter_statements:
                session.execute(text(ddl))
            session.commit()

    def get_by_sha256(self, source_sha256: str) -> ExistingDocument | None:
        self.initialize()
        with self._session_factory.create_session() as session:
            document = session.scalar(
                select(AlbaranDocumentOrm).where(
                    AlbaranDocumentOrm.source_sha256 == source_sha256
                )
            )
            if document is None:
                return None

            stored_lines = session.scalar(
                select(func.count(AlbaranLineOrm.id)).where(
                    AlbaranLineOrm.document_id == document.id
                )
            )
            return ExistingDocument(
                document_id=document.id,
                source_sha256=document.source_sha256,
                sharepoint_url=(
                    document.sharepoint_share_url or document.sharepoint_web_url
                ),
                stored_lines=int(stored_lines or 0),
            )

    def save(
        self,
        *,
        envelope: ExtractionEnvelope,
        context: Dict[str, Any],
        stored_file: StoredFile,
    ) -> ExistingDocument:
        self.initialize()

        email_ctx = context.get("email") or {}
        if not isinstance(email_ctx, dict):
            email_ctx = {}

        document_ctx = context.get("document") or {}
        if not isinstance(document_ctx, dict):
            document_ctx = {}

        debug_ctx = envelope.debug or {}
        if not isinstance(debug_ctx, dict):
            debug_ctx = {}

        ia_input_payload = debug_ctx.get("openai_request") or {}
        if not isinstance(ia_input_payload, dict):
            ia_input_payload = {}

        ia_output_payload = debug_ctx.get("openai_response") or {}
        if not isinstance(ia_output_payload, dict):
            ia_output_payload = {}

        document_id = str(uuid.uuid4())
        cabecera = envelope.data.cabecera
        payload = envelope.model_dump(by_alias=True)

        lines = [
            AlbaranLineOrm(
                document_id=document_id,
                line_index=index,
                external_line_id=line.id,
                cabecera_id=line.cabecera_id,
                codigo=line.codigo,
                cantidad=line.cantidad,
                concepto=line.concepto,
                precio=line.precio,
                descuento=line.descuento,
                precio_neto=line.precio_neto,
                codigo_imputacion=line.codigo_imputacion,
                confianza_pct=line.confianza_pct,
            )
            for index, line in enumerate(envelope.data.lineas, start=1)
        ]

        document = AlbaranDocumentOrm(
            id=document_id,
            source_filename=envelope.meta.source_filename,
            source_mime_type=envelope.meta.source_mime_type,
            source_sha256=envelope.meta.source_sha256,
            source_attachment_filename=(
                str(document_ctx.get("source_attachment_filename") or "") or None
            ),
            source_attachment_mime_type=(
                str(document_ctx.get("source_attachment_mime_type") or "") or None
            ),
            source_attachment_sha256=(
                str(document_ctx.get("source_attachment_sha256") or "") or None
            ),
            page_number=(
                int(document_ctx["page_number"])
                if document_ctx.get("page_number") is not None
                else None
            ),
            page_count=(
                int(document_ctx["page_count"])
                if document_ctx.get("page_count") is not None
                else None
            ),
            prompt_key=envelope.meta.prompt_key,
            schema_name=envelope.meta.schema_name,
            model_name=envelope.meta.model,
            proveedor_nombre=cabecera.proveedor_nombre,
            proveedor_cif=cabecera.proveedor_cif,
            fecha=cabecera.fecha,
            numero_albaran=cabecera.numero_albaran,
            forma_pago=cabecera.forma_pago,
            obra_codigo=cabecera.obra_codigo,
            obra_nombre=cabecera.obra_nombre,
            obra_direccion=cabecera.obra_direccion,
            sharepoint_drive_id=stored_file.drive_id,
            sharepoint_item_id=stored_file.item_id,
            sharepoint_relative_path=stored_file.relative_path,
            sharepoint_web_url=stored_file.web_url,
            sharepoint_share_url=stored_file.share_url,
            ia_input_json=(
                json.dumps(ia_input_payload, ensure_ascii=False, indent=2)
                if ia_input_payload
                else None
            ),
            ia_output_json=(
                json.dumps(ia_output_payload, ensure_ascii=False, indent=2)
                if ia_output_payload
                else None
            ),
            ia_input_relative_path=stored_file.ia_input_relative_path,
            ia_input_web_url=stored_file.ia_input_web_url,
            ia_output_relative_path=stored_file.ia_output_relative_path,
            ia_output_web_url=stored_file.ia_output_web_url,
            email_id=str(email_ctx.get("id") or "") or None,
            email_subject=str(email_ctx.get("subject") or "") or None,
            email_sender=str(email_ctx.get("sender") or "") or None,
            email_received_datetime=(
                str(email_ctx.get("receivedDateTime") or "") or None
            ),
            raw_context_json=json.dumps(context, ensure_ascii=False, indent=2),
            raw_extraction_json=json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            ),
            created_at_utc=envelope.meta.processed_at_utc,
            lines=lines,
        )

        with self._session_factory.create_session() as session:
            try:
                session.add(document)
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = self.get_by_sha256(envelope.meta.source_sha256)
                if existing is None:
                    raise
                return existing

        return ExistingDocument(
            document_id=document_id,
            source_sha256=envelope.meta.source_sha256,
            sharepoint_url=stored_file.share_url or stored_file.web_url,
            stored_lines=len(lines),
        )
