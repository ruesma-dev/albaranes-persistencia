# albaranes_persistence/infrastructure/database/sqlalchemy_albaran_repository.py
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Type

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from domain.models.extraction_models import (
    CabeceraAlbaran,
    DocumentoAlbaran,
    ExtractionEnvelope,
    LineaAlbaran,
    ProviderExtractionEnvelope,
)
from domain.models.persistence_models import ExistingDocument, StoredFile
from domain.ports.albaran_repository import AlbaranRepository
from infrastructure.database.orm_models import (
    AlbaranDocumentMergeOrm,
    AlbaranDocumentOrm,
    AlbaranLineMergeOrm,
    AlbaranLineOrm,
    Base,
)
from infrastructure.database.session_factory import SessionFactory

DocumentOrmType = Type[AlbaranDocumentOrm] | Type[AlbaranDocumentMergeOrm]
LineOrmType = Type[AlbaranLineOrm] | Type[AlbaranLineMergeOrm]

_CABECERA_FIELDS = (
    "proveedor_nombre",
    "proveedor_cif",
    "fecha",
    "numero_albaran",
    "forma_pago",
    "obra_codigo",
    "obra_nombre",
    "obra_direccion",
    "id",
)

_LINEA_FIELDS = (
    "id",
    "cabecera_id",
    "codigo",
    "cantidad",
    "concepto",
    "precio",
    "descuento",
    "precio_neto",
    "codigo_imputacion",
)


class SqlAlchemyAlbaranRepository(AlbaranRepository):
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._initialized_generation: int | None = None

    def initialize(self) -> None:
        self._session_factory.ensure_database_and_engine()
        current_generation = self._session_factory.generation
        if self._initialized_generation == current_generation:
            return

        self._rename_legacy_merge_tables()
        Base.metadata.create_all(self._session_factory.engine)
        self._ensure_compatible_schema()
        self._initialized_generation = current_generation

    def _rename_legacy_merge_tables(self) -> None:
        with self._session_factory.create_session() as session:
            gem_docs = session.scalar(
                text("SELECT to_regclass('public.albaran_documents_gem')")
            )
            merge_docs = session.scalar(
                text("SELECT to_regclass('public.albaran_documents_merge')")
            )
            gem_lines = session.scalar(
                text("SELECT to_regclass('public.albaran_lines_gem')")
            )
            merge_lines = session.scalar(
                text("SELECT to_regclass('public.albaran_lines_merge')")
            )

            if gem_docs and not merge_docs:
                session.execute(
                    text("ALTER TABLE albaran_documents_gem RENAME TO albaran_documents_merge")
                )
            if gem_lines and not merge_lines:
                session.execute(
                    text("ALTER TABLE albaran_lines_gem RENAME TO albaran_lines_merge")
                )
            session.commit()

    def _ensure_compatible_schema(self) -> None:
        alter_statements: list[str] = []

        for table_name, default_provider in (
            ("albaran_documents", "openai"),
            ("albaran_documents_merge", "gemini_filled"),
        ):
            alter_statements.extend(
                [
                    (
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN IF NOT EXISTS provider_origin VARCHAR(32)"
                    ),
                    (
                        f"UPDATE {table_name} "
                        f"SET provider_origin = '{default_provider}' "
                        "WHERE provider_origin IS NULL"
                    ),
                    (
                        f"ALTER TABLE {table_name} "
                        "ALTER COLUMN provider_origin SET NOT NULL"
                    ),
                    (
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN IF NOT EXISTS source_document_id VARCHAR(64)"
                    ),
                    (
                        f"UPDATE {table_name} "
                        "SET source_document_id = source_sha256 "
                        "WHERE source_document_id IS NULL"
                    ),
                    (
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN IF NOT EXISTS document_storage_ref VARCHAR(1024)"
                    ),
                    (
                        f"UPDATE {table_name} "
                        "SET document_storage_ref = COALESCE("
                        "document_storage_ref, sharepoint_relative_path, "
                        "sharepoint_web_url, sharepoint_item_id, source_sha256)"
                    ),
                    (
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN IF NOT EXISTS source_attachment_filename VARCHAR(255)"
                    ),
                    (
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN IF NOT EXISTS source_attachment_mime_type VARCHAR(255)"
                    ),
                    (
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN IF NOT EXISTS source_attachment_sha256 VARCHAR(64)"
                    ),
                    (
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN IF NOT EXISTS page_number INTEGER"
                    ),
                    (
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN IF NOT EXISTS page_count INTEGER"
                    ),
                    (
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN IF NOT EXISTS ia_input_json TEXT"
                    ),
                    (
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN IF NOT EXISTS ia_output_json TEXT"
                    ),
                    (
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN IF NOT EXISTS ia_input_relative_path VARCHAR(1024)"
                    ),
                    (
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN IF NOT EXISTS ia_input_web_url VARCHAR(1024)"
                    ),
                    (
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN IF NOT EXISTS ia_output_relative_path VARCHAR(1024)"
                    ),
                    (
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN IF NOT EXISTS ia_output_web_url VARCHAR(1024)"
                    ),
                    (
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN IF NOT EXISTS raw_context_json TEXT"
                    ),
                ]
            )

        for table_name, default_provider in (
            ("albaran_lines", "openai"),
            ("albaran_lines_merge", "gemini_filled"),
        ):
            alter_statements.extend(
                [
                    (
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN IF NOT EXISTS provider_origin VARCHAR(32)"
                    ),
                    (
                        f"UPDATE {table_name} "
                        f"SET provider_origin = '{default_provider}' "
                        "WHERE provider_origin IS NULL"
                    ),
                    (
                        f"ALTER TABLE {table_name} "
                        "ALTER COLUMN provider_origin SET NOT NULL"
                    ),
                ]
            )

        constraint_statements = [
            "ALTER TABLE albaran_documents DROP CONSTRAINT IF EXISTS albaran_documents_source_sha256_key",
            (
                "ALTER TABLE albaran_documents_merge "
                "DROP CONSTRAINT IF EXISTS albaran_documents_merge_source_sha256_key"
            ),
            (
                "ALTER TABLE albaran_documents_merge "
                "DROP CONSTRAINT IF EXISTS albaran_documents_gem_source_sha256_key"
            ),
            (
                "ALTER TABLE albaran_documents_merge "
                "DROP CONSTRAINT IF EXISTS uq_albaran_documents_gem_sha_provider"
            ),
            (
                "ALTER TABLE albaran_documents_merge "
                "DROP CONSTRAINT IF EXISTS uq_albaran_documents_gem_sha"
            ),
            "DROP INDEX IF EXISTS uq_albaran_documents_gem_sha_provider",
            "DROP INDEX IF EXISTS uq_albaran_documents_gem_sha",
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_albaran_documents_sha_provider "
                "ON albaran_documents (source_sha256, provider_origin)"
            ),
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_albaran_documents_merge_sha "
                "ON albaran_documents_merge (source_sha256)"
            ),
        ]

        with self._session_factory.create_session() as session:
            for ddl in alter_statements + constraint_statements:
                session.execute(text(ddl))
            session.commit()

    def get_by_sha256(self, source_sha256: str) -> ExistingDocument | None:
        self.initialize()
        with self._session_factory.create_session() as session:
            result_document = session.scalar(
                select(AlbaranDocumentMergeOrm).where(
                    AlbaranDocumentMergeOrm.source_sha256 == source_sha256,
                )
            )
            if result_document is None:
                return None

            stored_lines = session.scalar(
                select(func.count(AlbaranLineMergeOrm.id)).where(
                    AlbaranLineMergeOrm.document_id == result_document.id
                )
            )
            return ExistingDocument(
                document_id=result_document.id,
                source_sha256=result_document.source_sha256,
                sharepoint_url=(
                    result_document.sharepoint_share_url
                    or result_document.sharepoint_web_url
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

        openai_debug = envelope.debug if isinstance(envelope.debug, dict) else {}
        gemini_debug = (
            envelope.gemini.debug
            if envelope.gemini is not None and isinstance(envelope.gemini.debug, dict)
            else {}
        )

        openai_document_id = str(uuid.uuid4())
        openai_document = self._build_document_orm(
            orm_document_cls=AlbaranDocumentOrm,
            orm_line_cls=AlbaranLineOrm,
            document_id=openai_document_id,
            provider_origin="openai",
            provider_envelope=envelope,
            context=context,
            email_ctx=email_ctx,
            document_ctx=document_ctx,
            stored_file=stored_file,
            ia_input_payload=self._coerce_dict(openai_debug.get("openai_request")),
            ia_output_payload=self._coerce_dict(openai_debug.get("openai_response")),
            ia_input_relative_path=stored_file.ia_input_relative_path,
            ia_input_web_url=stored_file.ia_input_web_url,
            ia_output_relative_path=stored_file.ia_output_relative_path,
            ia_output_web_url=stored_file.ia_output_web_url,
        )

        gemini_document = None
        if envelope.gemini is not None:
            gemini_document = self._build_document_orm(
                orm_document_cls=AlbaranDocumentOrm,
                orm_line_cls=AlbaranLineOrm,
                document_id=str(uuid.uuid4()),
                provider_origin="gemini",
                provider_envelope=envelope.gemini,
                context=context,
                email_ctx=email_ctx,
                document_ctx=document_ctx,
                stored_file=stored_file,
                ia_input_payload=self._coerce_dict(gemini_debug.get("gemini_request")),
                ia_output_payload=self._coerce_dict(gemini_debug.get("gemini_response")),
                ia_input_relative_path=stored_file.gem_input_relative_path,
                ia_input_web_url=stored_file.gem_input_web_url,
                ia_output_relative_path=stored_file.gem_output_relative_path,
                ia_output_web_url=stored_file.gem_output_web_url,
            )

        result_envelope, result_provider_origin = self._build_result_envelope(
            openai=envelope,
            gemini=envelope.gemini,
        )
        result_debug = gemini_debug if envelope.gemini is not None else openai_debug
        result_input_relative_path = (
            stored_file.gem_input_relative_path
            if envelope.gemini is not None
            else stored_file.ia_input_relative_path
        )
        result_input_web_url = (
            stored_file.gem_input_web_url
            if envelope.gemini is not None
            else stored_file.ia_input_web_url
        )
        result_output_relative_path = (
            stored_file.gem_output_relative_path
            if envelope.gemini is not None
            else stored_file.ia_output_relative_path
        )
        result_output_web_url = (
            stored_file.gem_output_web_url
            if envelope.gemini is not None
            else stored_file.ia_output_web_url
        )
        result_document_id = str(uuid.uuid4())
        result_document = self._build_document_orm(
            orm_document_cls=AlbaranDocumentMergeOrm,
            orm_line_cls=AlbaranLineMergeOrm,
            document_id=result_document_id,
            provider_origin=result_provider_origin,
            provider_envelope=result_envelope,
            context=context,
            email_ctx=email_ctx,
            document_ctx=document_ctx,
            stored_file=stored_file,
            ia_input_payload=self._coerce_dict(
                result_debug.get("gemini_request") or result_debug.get("openai_request")
            ),
            ia_output_payload=self._coerce_dict(
                result_debug.get("gemini_response") or result_debug.get("openai_response")
            ),
            ia_input_relative_path=result_input_relative_path,
            ia_input_web_url=result_input_web_url,
            ia_output_relative_path=result_output_relative_path,
            ia_output_web_url=result_output_web_url,
        )

        with self._session_factory.create_session() as session:
            try:
                self._delete_existing_records(
                    session=session,
                    source_sha256=envelope.meta.source_sha256,
                )
                session.add(openai_document)
                if gemini_document is not None:
                    session.add(gemini_document)
                session.add(result_document)
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = self.get_by_sha256(envelope.meta.source_sha256)
                if existing is None:
                    raise
                return existing

        return ExistingDocument(
            document_id=result_document_id,
            source_sha256=envelope.meta.source_sha256,
            sharepoint_url=stored_file.share_url or stored_file.web_url,
            stored_lines=len(result_envelope.data.lineas),
        )

    def _delete_existing_records(self, *, session: Any, source_sha256: str) -> None:
        result_docs = session.scalars(
            select(AlbaranDocumentMergeOrm).where(
                AlbaranDocumentMergeOrm.source_sha256 == source_sha256,
            )
        ).all()
        for document in result_docs:
            session.delete(document)

        raw_docs = session.scalars(
            select(AlbaranDocumentOrm).where(
                AlbaranDocumentOrm.source_sha256 == source_sha256,
            )
        ).all()
        for document in raw_docs:
            session.delete(document)

        session.flush()

    def _build_result_envelope(
        self,
        *,
        openai: ExtractionEnvelope,
        gemini: ProviderExtractionEnvelope | None,
    ) -> tuple[ProviderExtractionEnvelope, str]:
        if gemini is None:
            return (
                ProviderExtractionEnvelope(
                    meta=openai.meta,
                    data=openai.data,
                    debug=openai.debug,
                ),
                "openai_fallback",
            )

        merged_document = DocumentoAlbaran(
            cabecera=self._merge_cabecera(
                primary=gemini.data.cabecera,
                fallback=openai.data.cabecera,
            ),
            lineas=self._merge_lines_by_index(
                primary_lines=gemini.data.lineas,
                fallback_lines=openai.data.lineas,
            ),
        )
        return (
            ProviderExtractionEnvelope(
                meta=gemini.meta,
                data=merged_document,
                debug=gemini.debug,
            ),
            "gemini_filled",
        )

    def _merge_cabecera(
        self,
        *,
        primary: CabeceraAlbaran,
        fallback: CabeceraAlbaran,
    ) -> CabeceraAlbaran:
        merged: dict[str, Any] = {}
        for field_name in _CABECERA_FIELDS:
            merged[field_name] = self._coalesce_value(
                getattr(primary, field_name),
                getattr(fallback, field_name),
            )
        return CabeceraAlbaran(**merged)

    def _merge_lines_by_index(
        self,
        *,
        primary_lines: list[LineaAlbaran],
        fallback_lines: list[LineaAlbaran],
    ) -> list[LineaAlbaran]:
        merged_lines: list[LineaAlbaran] = []
        for index, primary_line in enumerate(primary_lines):
            fallback_line = (
                fallback_lines[index] if index < len(fallback_lines) else None
            )
            merged_payload: dict[str, Any] = {}
            for field_name in _LINEA_FIELDS:
                fallback_value = (
                    getattr(fallback_line, field_name)
                    if fallback_line is not None
                    else None
                )
                merged_payload[field_name] = self._coalesce_value(
                    getattr(primary_line, field_name),
                    fallback_value,
                )

            merged_payload["confianza_pct"] = (
                getattr(fallback_line, "confianza_pct")
                if fallback_line is not None
                else None
            )
            merged_lines.append(LineaAlbaran(**merged_payload))

        if len(primary_lines) < len(fallback_lines):
            for fallback_line in fallback_lines[len(primary_lines) :]:
                merged_lines.append(
                    fallback_line.model_copy(
                        update={"confianza_pct": fallback_line.confianza_pct}
                    )
                )

        return merged_lines

    @staticmethod
    def _coalesce_value(primary: Any, fallback: Any) -> Any:
        if primary is None:
            return fallback
        if isinstance(primary, str) and not primary.strip():
            return fallback
        return primary

    @staticmethod
    def _coerce_dict(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _build_document_orm(
        self,
        *,
        orm_document_cls: DocumentOrmType,
        orm_line_cls: LineOrmType,
        document_id: str,
        provider_origin: str,
        provider_envelope: ExtractionEnvelope | ProviderExtractionEnvelope,
        context: Dict[str, Any],
        email_ctx: Dict[str, Any],
        document_ctx: Dict[str, Any],
        stored_file: StoredFile,
        ia_input_payload: Dict[str, Any],
        ia_output_payload: Dict[str, Any],
        ia_input_relative_path: str | None,
        ia_input_web_url: str | None,
        ia_output_relative_path: str | None,
        ia_output_web_url: str | None,
    ) -> AlbaranDocumentOrm | AlbaranDocumentMergeOrm:
        cabecera: CabeceraAlbaran = provider_envelope.data.cabecera
        payload = provider_envelope.model_dump(by_alias=True)
        lines = self._build_lines(
            orm_line_cls=orm_line_cls,
            document_id=document_id,
            provider_origin=provider_origin,
            source_lines=provider_envelope.data.lineas,
        )
        return orm_document_cls(
            id=document_id,
            provider_origin=provider_origin,
            source_document_id=provider_envelope.meta.source_sha256,
            document_storage_ref=(
                stored_file.relative_path
                or stored_file.web_url
                or stored_file.item_id
                or provider_envelope.meta.source_sha256
            ),
            source_filename=provider_envelope.meta.source_filename,
            source_mime_type=provider_envelope.meta.source_mime_type,
            source_sha256=provider_envelope.meta.source_sha256,
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
            prompt_key=provider_envelope.meta.prompt_key,
            schema_name=provider_envelope.meta.schema_name,
            model_name=provider_envelope.meta.model,
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
            ia_input_relative_path=ia_input_relative_path,
            ia_input_web_url=ia_input_web_url,
            ia_output_relative_path=ia_output_relative_path,
            ia_output_web_url=ia_output_web_url,
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
            created_at_utc=provider_envelope.meta.processed_at_utc,
            lines=lines,
        )

    @staticmethod
    def _build_lines(
        *,
        orm_line_cls: LineOrmType,
        document_id: str,
        provider_origin: str,
        source_lines: list[LineaAlbaran],
    ) -> list[AlbaranLineOrm | AlbaranLineMergeOrm]:
        return [
            orm_line_cls(
                document_id=document_id,
                provider_origin=provider_origin,
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
            for index, line in enumerate(source_lines, start=1)
        ]
