# infrastructure/database/sqlalchemy_albaran_repository.py
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Type

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from application.services.albaran_confidence_service import (
    AlbaranConfidenceService,
    LineMergeResult,
)
from domain.models.contrato_models import ContratoEnrichmentResult
from domain.models.extraction_models import (
    CabeceraAlbaran,
    ExtractionEnvelope,
    LineaAlbaran,
    ProviderExtractionEnvelope,
)
from domain.models.persistence_models import ExistingDocument, StoredFile
from domain.ports.albaran_repository import AlbaranRepository
from infrastructure.database.orm_contrato_models import (
    AlbaranContratoLineMergeOrm,
    AlbaranContratoMergeOrm,
)
from infrastructure.database.orm_models import (
    AlbaranDocumentMergeOrm,
    AlbaranDocumentOrm,
    AlbaranLineMergeOrm,
    AlbaranLineOrm,
    Base,
)
from infrastructure.database.session_factory import SessionFactory

logger = logging.getLogger(__name__)

DocumentOrmType = Type[AlbaranDocumentOrm] | Type[AlbaranDocumentMergeOrm]
LineOrmType = Type[AlbaranLineOrm] | Type[AlbaranLineMergeOrm]


@dataclass(frozen=True)
class RawProviderSpec:
    provider_origin: str
    provider_envelope: ProviderExtractionEnvelope
    ia_input_payload: Dict[str, Any]
    ia_output_payload: Dict[str, Any]
    ia_input_relative_path: str | None
    ia_input_web_url: str | None
    ia_output_relative_path: str | None
    ia_output_web_url: str | None
    document_confidence_pct: float | None


class SqlAlchemyAlbaranRepository(AlbaranRepository):
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._initialized_generation: int | None = None
        self._confidence_service = AlbaranConfidenceService()

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
        statements = [
            (
                "ALTER TABLE IF EXISTS albaran_documents_gem "
                "RENAME TO albaran_documents_merge"
            ),
            (
                "ALTER TABLE IF EXISTS albaran_lines_gem "
                "RENAME TO albaran_lines_merge"
            ),
        ]
        with self._session_factory.create_session() as session:
            merge_exists = session.scalar(
                text(
                    "SELECT to_regclass('public.albaran_documents_merge') IS NOT NULL"
                )
            )
            if merge_exists:
                session.rollback()
                return
            for ddl in statements:
                session.execute(text(ddl))
            session.commit()

    def _ensure_compatible_schema(self) -> None:
        document_tables = (
            ("albaran_documents", "openai"),
            ("albaran_documents_merge", "gemini_filled"),
        )
        line_tables = (
            ("albaran_lines", "openai"),
            ("albaran_lines_merge", "gemini_filled"),
        )
        alter_statements: list[str] = []

        for table_name, default_provider in document_tables:
            alter_statements.extend(
                [
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS provider_origin VARCHAR(32)",
                    (
                        f"UPDATE {table_name} SET provider_origin = '{default_provider}' "
                        "WHERE provider_origin IS NULL"
                    ),
                    f"ALTER TABLE {table_name} ALTER COLUMN provider_origin SET NOT NULL",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS source_document_id VARCHAR(64)",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS document_storage_ref VARCHAR(1024)",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS source_attachment_filename VARCHAR(255)",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS source_attachment_mime_type VARCHAR(255)",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS source_attachment_sha256 VARCHAR(64)",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS page_number INTEGER",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS page_count INTEGER",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS ia_input_json TEXT",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS ia_output_json TEXT",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS ia_input_relative_path VARCHAR(1024)",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS ia_input_web_url VARCHAR(1024)",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS ia_output_relative_path VARCHAR(1024)",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS ia_output_web_url VARCHAR(1024)",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS raw_context_json TEXT",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS confidence_pct_calc DOUBLE PRECISION",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS review_required BOOLEAN",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS review_reasons_json TEXT",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS comparison_summary_json TEXT",
                    (
                        f"UPDATE {table_name} SET source_document_id = source_sha256 "
                        "WHERE source_document_id IS NULL"
                    ),
                    (
                        f"UPDATE {table_name} SET document_storage_ref = sharepoint_relative_path "
                        "WHERE document_storage_ref IS NULL"
                    ),
                ]
            )

        for table_name, default_provider in line_tables:
            alter_statements.extend(
                [
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS provider_origin VARCHAR(32)",
                    (
                        f"UPDATE {table_name} SET provider_origin = '{default_provider}' "
                        "WHERE provider_origin IS NULL"
                    ),
                    f"ALTER TABLE {table_name} ALTER COLUMN provider_origin SET NOT NULL",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS confidence_pct_calc DOUBLE PRECISION",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS line_match_score DOUBLE PRECISION",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS comparison_status_json TEXT",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS field_scores_json TEXT",
                ]
            )

        alter_statements.append(
            "ALTER TABLE albaran_documents_merge "
            "ADD COLUMN IF NOT EXISTS selected_contrato_codigo VARCHAR(64)"
        )

        # Columnas nuevas en albaran_contratos_merge.
        alter_statements.extend(
            [
                "ALTER TABLE albaran_contratos_merge "
                "ADD COLUMN IF NOT EXISTS gra_rep_ide INTEGER",
                "ALTER TABLE albaran_contratos_merge "
                "ADD COLUMN IF NOT EXISTS pdf_sharepoint_relative_path VARCHAR(1024)",
                "ALTER TABLE albaran_contratos_merge "
                "ADD COLUMN IF NOT EXISTS pdf_sharepoint_web_url VARCHAR(1024)",
            ]
        )

        alter_statements.extend(
            [
                (
                    "ALTER TABLE albaran_contrato_lines_merge "
                    "ADD COLUMN IF NOT EXISTS codigo_partida VARCHAR(64)"
                ),
                (
                    "ALTER TABLE albaran_contrato_lines_merge "
                    "ADD COLUMN IF NOT EXISTS descripcion_partida TEXT"
                ),
            ]
        )

        constraint_statements = [
            (
                "ALTER TABLE albaran_documents "
                "DROP CONSTRAINT IF EXISTS albaran_documents_source_sha256_key"
            ),
            (
                "ALTER TABLE albaran_documents_merge "
                "DROP CONSTRAINT IF EXISTS uq_albaran_documents_gem_sha_provider"
            ),
            (
                "ALTER TABLE albaran_documents_merge "
                "DROP CONSTRAINT IF EXISTS albaran_documents_gem_source_sha256_key"
            ),
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_albaran_documents_sha_provider "
                "ON albaran_documents (source_sha256, provider_origin)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS ix_albaran_contratos_merge_document "
                "ON albaran_contratos_merge (document_id)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS ix_albaran_contrato_lines_merge_contrato "
                "ON albaran_contrato_lines_merge (contrato_id)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS ix_albaran_contrato_lines_merge_partida "
                "ON albaran_contrato_lines_merge (codigo_partida)"
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
        claude_debug = (
            envelope.claude.debug
            if envelope.claude is not None and isinstance(envelope.claude.debug, dict)
            else {}
        )
        google_debug = (
            envelope.google_document_ai.debug
            if envelope.google_document_ai is not None
            and isinstance(envelope.google_document_ai.debug, dict)
            else {}
        )
        azure_debug = (
            envelope.azure_document_intelligence.debug
            if envelope.azure_document_intelligence is not None
            and isinstance(envelope.azure_document_intelligence.debug, dict)
            else {}
        )

        merge_analysis = self._confidence_service.build_merge_analysis(
            openai=envelope,
            gemini=envelope.gemini,
            claude=envelope.claude,
        )

        raw_provider_specs: list[RawProviderSpec] = [
            RawProviderSpec(
                provider_origin="openai",
                provider_envelope=envelope,
                ia_input_payload=self._coerce_dict(
                    openai_debug.get("openai_request")
                ),
                ia_output_payload=self._coerce_dict(
                    openai_debug.get("openai_response")
                ),
                ia_input_relative_path=stored_file.ia_input_relative_path,
                ia_input_web_url=stored_file.ia_input_web_url,
                ia_output_relative_path=stored_file.ia_output_relative_path,
                ia_output_web_url=stored_file.ia_output_web_url,
                document_confidence_pct=merge_analysis.openai_raw_confidence_pct,
            )
        ]
        if envelope.gemini is not None:
            raw_provider_specs.append(
                RawProviderSpec(
                    provider_origin="gemini",
                    provider_envelope=envelope.gemini,
                    ia_input_payload=self._coerce_dict(
                        gemini_debug.get("gemini_request")
                    ),
                    ia_output_payload=self._coerce_dict(
                        gemini_debug.get("gemini_response")
                    ),
                    ia_input_relative_path=stored_file.gem_input_relative_path,
                    ia_input_web_url=stored_file.gem_input_web_url,
                    ia_output_relative_path=stored_file.gem_output_relative_path,
                    ia_output_web_url=stored_file.gem_output_web_url,
                    document_confidence_pct=self._average_confidence(
                        envelope.gemini.data.lineas
                    ),
                )
            )
        if envelope.claude is not None:
            raw_provider_specs.append(
                RawProviderSpec(
                    provider_origin="claude",
                    provider_envelope=envelope.claude,
                    ia_input_payload=self._coerce_dict(
                        claude_debug.get("claude_request")
                    ),
                    ia_output_payload=self._coerce_dict(
                        claude_debug.get("claude_response")
                    ),
                    ia_input_relative_path=stored_file.cla_input_relative_path,
                    ia_input_web_url=stored_file.cla_input_web_url,
                    ia_output_relative_path=stored_file.cla_output_relative_path,
                    ia_output_web_url=stored_file.cla_output_web_url,
                    document_confidence_pct=self._average_confidence(
                        envelope.claude.data.lineas
                    ),
                )
            )
        if envelope.google_document_ai is not None:
            raw_provider_specs.append(
                RawProviderSpec(
                    provider_origin="google_document_ai",
                    provider_envelope=envelope.google_document_ai,
                    ia_input_payload=self._coerce_dict(
                        google_debug.get("google_document_ai_request")
                    ),
                    ia_output_payload=self._coerce_dict(
                        google_debug.get("google_document_ai_response")
                    ),
                    ia_input_relative_path=None,
                    ia_input_web_url=None,
                    ia_output_relative_path=None,
                    ia_output_web_url=None,
                    document_confidence_pct=self._average_confidence(
                        envelope.google_document_ai.data.lineas
                    ),
                )
            )
        if envelope.azure_document_intelligence is not None:
            raw_provider_specs.append(
                RawProviderSpec(
                    provider_origin="azure_document_intelligence",
                    provider_envelope=envelope.azure_document_intelligence,
                    ia_input_payload=self._coerce_dict(
                        azure_debug.get("azure_document_intelligence_request")
                    ),
                    ia_output_payload=self._coerce_dict(
                        azure_debug.get("azure_document_intelligence_response")
                    ),
                    ia_input_relative_path=None,
                    ia_input_web_url=None,
                    ia_output_relative_path=None,
                    ia_output_web_url=None,
                    document_confidence_pct=self._average_confidence(
                        envelope.azure_document_intelligence.data.lineas
                    ),
                )
            )

        raw_documents = [
            self._build_document_orm(
                orm_document_cls=AlbaranDocumentOrm,
                orm_line_cls=AlbaranLineOrm,
                document_id=str(uuid.uuid4()),
                provider_origin=provider_spec.provider_origin,
                provider_envelope=provider_spec.provider_envelope,
                context=context,
                email_ctx=email_ctx,
                document_ctx=document_ctx,
                stored_file=stored_file,
                ia_input_payload=provider_spec.ia_input_payload,
                ia_output_payload=provider_spec.ia_output_payload,
                ia_input_relative_path=provider_spec.ia_input_relative_path,
                ia_input_web_url=provider_spec.ia_input_web_url,
                ia_output_relative_path=provider_spec.ia_output_relative_path,
                ia_output_web_url=provider_spec.ia_output_web_url,
                raw_lines=provider_spec.provider_envelope.data.lineas,
                document_confidence_pct=provider_spec.document_confidence_pct,
                review_required=None,
                review_reasons=None,
                comparison_summary=None,
                line_results=None,
            )
            for provider_spec in raw_provider_specs
        ]

        if envelope.gemini is not None:
            merge_debug = gemini_debug
            merge_input_rel = stored_file.gem_input_relative_path
            merge_input_url = stored_file.gem_input_web_url
            merge_output_rel = stored_file.gem_output_relative_path
            merge_output_url = stored_file.gem_output_web_url
            merge_input_key = "gemini_request"
            merge_output_key = "gemini_response"
        elif envelope.claude is not None:
            merge_debug = claude_debug
            merge_input_rel = stored_file.cla_input_relative_path
            merge_input_url = stored_file.cla_input_web_url
            merge_output_rel = stored_file.cla_output_relative_path
            merge_output_url = stored_file.cla_output_web_url
            merge_input_key = "claude_request"
            merge_output_key = "claude_response"
        else:
            merge_debug = openai_debug
            merge_input_rel = stored_file.ia_input_relative_path
            merge_input_url = stored_file.ia_input_web_url
            merge_output_rel = stored_file.ia_output_relative_path
            merge_output_url = stored_file.ia_output_web_url
            merge_input_key = "openai_request"
            merge_output_key = "openai_response"

        merge_document_id = str(uuid.uuid4())
        merge_document = self._build_document_orm(
            orm_document_cls=AlbaranDocumentMergeOrm,
            orm_line_cls=AlbaranLineMergeOrm,
            document_id=merge_document_id,
            provider_origin=merge_analysis.provider_origin,
            provider_envelope=merge_analysis.merged_envelope,
            context=context,
            email_ctx=email_ctx,
            document_ctx=document_ctx,
            stored_file=stored_file,
            ia_input_payload=self._coerce_dict(merge_debug.get(merge_input_key)),
            ia_output_payload=self._coerce_dict(merge_debug.get(merge_output_key)),
            ia_input_relative_path=merge_input_rel,
            ia_input_web_url=merge_input_url,
            ia_output_relative_path=merge_output_rel,
            ia_output_web_url=merge_output_url,
            raw_lines=[item.merged_line for item in merge_analysis.line_results],
            document_confidence_pct=merge_analysis.document_confidence_pct,
            review_required=merge_analysis.review_required,
            review_reasons=merge_analysis.review_reasons,
            comparison_summary=merge_analysis.comparison_summary,
            line_results=merge_analysis.line_results,
        )

        with self._session_factory.create_session() as session:
            try:
                self._delete_existing_records(
                    session=session,
                    source_sha256=envelope.meta.source_sha256,
                )
                for document in raw_documents:
                    session.add(document)
                session.add(merge_document)
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = self.get_by_sha256(envelope.meta.source_sha256)
                if existing is None:
                    raise
                return existing

        return ExistingDocument(
            document_id=merge_document_id,
            source_sha256=envelope.meta.source_sha256,
            sharepoint_url=stored_file.share_url or stored_file.web_url,
            stored_lines=len(merge_analysis.line_results),
        )

    # ================================================================== #
    # Puerto ObraMergeRepository (cumplido por duck-typing)
    # ================================================================== #
    def get_merge_obra_codigo(self, *, document_id: str) -> str | None:
        self.initialize()
        with self._session_factory.create_session() as session:
            document = session.get(AlbaranDocumentMergeOrm, document_id)
            if document is None:
                return None
            return document.obra_codigo

    def update_merge_obra_fields(
        self,
        *,
        document_id: str,
        obra_nombre: str | None,
        obra_direccion: str | None,
    ) -> None:
        self.initialize()
        with self._session_factory.create_session() as session:
            document = session.get(AlbaranDocumentMergeOrm, document_id)
            if document is None:
                raise KeyError(f"Documento merge no encontrado: {document_id}")
            document.obra_nombre = obra_nombre
            document.obra_direccion = obra_direccion
            session.commit()

    # ================================================================== #
    # Puerto ContratoMergeRepository (cumplido por duck-typing)
    # ================================================================== #
    def get_merge_cif_and_obra(
        self,
        *,
        document_id: str,
    ) -> tuple[str | None, str | None]:
        self.initialize()
        with self._session_factory.create_session() as session:
            document = session.get(AlbaranDocumentMergeOrm, document_id)
            if document is None:
                return None, None
            return document.proveedor_cif, document.obra_codigo

    def get_existing_pdf_paths(
        self,
        *,
        document_id: str,
    ) -> dict[str, tuple[int | None, str | None, str | None]]:
        """Mapa de PDFs ya subidos para los contratos de un documento.

        Se llama ANTES del replace_contratos para saber qué ``gra_rep_ide``
        y ``pdf_*`` teníamos, y poder evitar la descarga+subida si la
        versión del PDF no cambió.
        """
        self.initialize()
        result: dict[str, tuple[int | None, str | None, str | None]] = {}
        with self._session_factory.create_session() as session:
            rows = session.execute(
                select(
                    AlbaranContratoMergeOrm.codigo_contrato,
                    AlbaranContratoMergeOrm.gra_rep_ide,
                    AlbaranContratoMergeOrm.pdf_sharepoint_relative_path,
                    AlbaranContratoMergeOrm.pdf_sharepoint_web_url,
                ).where(AlbaranContratoMergeOrm.document_id == document_id)
            ).all()
            for codigo, ide, rel_path, web_url in rows:
                result[codigo] = (ide, rel_path, web_url)
        return result

    def replace_contratos(
        self,
        *,
        document_id: str,
        contratos: list[ContratoEnrichmentResult],
    ) -> None:
        """Reemplaza cabeceras + líneas de contrato atómicamente.

        Persiste ``gra_rep_ide`` + ``pdf_*`` si el DTO los trae (caso de
        reutilización). Para los contratos nuevos, los ``pdf_*`` van
        ``None`` y el orquestador los actualiza luego vía
        ``update_contrato_pdf_paths``.
        """
        self.initialize()
        now = datetime.now(timezone.utc).isoformat()
        with self._session_factory.create_session() as session:
            merge_doc = session.get(AlbaranDocumentMergeOrm, document_id)
            if merge_doc is None:
                raise KeyError(f"Documento merge no encontrado: {document_id}")

            deleted = session.execute(
                delete(AlbaranContratoMergeOrm).where(
                    AlbaranContratoMergeOrm.document_id == document_id
                )
            )
            session.flush()

            total_lines_inserted = 0
            for contrato in contratos:
                header_orm = AlbaranContratoMergeOrm(
                    document_id=document_id,
                    codigo_contrato=contrato.codigo_contrato,
                    nombre_contrato=contrato.nombre_contrato,
                    fecha_alta_contrato=contrato.fecha_alta_contrato,
                    fecha_contrato=contrato.fecha_contrato,
                    vigencia_desde=contrato.vigencia_desde,
                    vigencia_hasta=contrato.vigencia_hasta,
                    importe_total=contrato.importe_total,
                    cif_proveedor=contrato.cif_proveedor,
                    nombre_proveedor=contrato.nombre_proveedor,
                    codigo_obra=contrato.codigo_obra,
                    nombre_obra=contrato.nombre_obra,
                    gra_rep_ide=contrato.gra_rep_ide,
                    pdf_sharepoint_relative_path=contrato.pdf_sharepoint_relative_path,
                    pdf_sharepoint_web_url=contrato.pdf_sharepoint_web_url,
                    fetched_at_utc=now,
                )
                session.add(header_orm)
                session.flush()

                for line in (contrato.lines or []):
                    session.add(
                        AlbaranContratoLineMergeOrm(
                            contrato_id=header_orm.id,
                            codigo_contrato=contrato.codigo_contrato,
                            linea=line.linea,
                            numero_linea=line.numero_linea,
                            codigo_producto=line.codigo_producto,
                            codigo_alternativo=line.codigo_alternativo,
                            unidad_medida=line.unidad_medida,
                            descripcion_linea=line.descripcion_linea,
                            uds=line.uds,
                            cantidad_servida=line.cantidad_servida,
                            cantidad_facturada=line.cantidad_facturada,
                            pendiente_servir=line.pendiente_servir,
                            precio_unitario=line.precio_unitario,
                            precio_bruto=line.precio_bruto,
                            descuentos=line.descuentos,
                            importe_linea=line.importe_linea,
                            cuota_iva=line.cuota_iva,
                            doc_origen=line.doc_origen,
                            codigo_partida=line.codigo_partida,
                            descripcion_partida=line.descripcion_partida,
                            fetched_at_utc=now,
                        )
                    )
                    total_lines_inserted += 1

            session.commit()
            logger.info(
                "[contrato-enrichment][repo] replace_contratos: "
                "document_id=%s borrados=%s contratos=%s lineas=%s "
                "pdfs_con_path_inicial=%s",
                document_id,
                deleted.rowcount if hasattr(deleted, "rowcount") else "?",
                len(contratos),
                total_lines_inserted,
                sum(
                    1
                    for c in contratos
                    if c.pdf_sharepoint_relative_path is not None
                ),
            )

    def update_contrato_pdf_paths(
        self,
        *,
        document_id: str,
        codigo_contrato: str,
        relative_path: str | None,
        web_url: str | None,
    ) -> None:
        """Actualiza los paths del PDF para un contrato concreto."""
        self.initialize()
        with self._session_factory.create_session() as session:
            session.execute(
                text(
                    "UPDATE albaran_contratos_merge "
                    "SET pdf_sharepoint_relative_path = :rel, "
                    "    pdf_sharepoint_web_url = :url "
                    "WHERE document_id = :doc_id "
                    "  AND codigo_contrato = :codigo"
                ),
                {
                    "rel": relative_path,
                    "url": web_url,
                    "doc_id": document_id,
                    "codigo": codigo_contrato,
                },
            )
            session.commit()
            logger.info(
                "[contrato-enrichment][repo] update_contrato_pdf_paths "
                "doc=%s codigo=%s rel=%s url=%s",
                document_id,
                codigo_contrato,
                relative_path,
                web_url,
            )

    def set_selected_contrato(
        self,
        *,
        document_id: str,
        codigo_contrato: str | None,
    ) -> None:
        self.initialize()
        with self._session_factory.create_session() as session:
            session.execute(
                text(
                    "UPDATE albaran_documents_merge "
                    "SET selected_contrato_codigo = :codigo "
                    "WHERE id = :doc_id"
                ),
                {"codigo": codigo_contrato, "doc_id": document_id},
            )
            session.commit()

    def _delete_existing_records(self, *, session: Any, source_sha256: str) -> None:
        merge_docs = session.scalars(
            select(AlbaranDocumentMergeOrm).where(
                AlbaranDocumentMergeOrm.source_sha256 == source_sha256,
            )
        ).all()
        for document in merge_docs:
            session.execute(
                delete(AlbaranContratoMergeOrm).where(
                    AlbaranContratoMergeOrm.document_id == document.id
                )
            )
            session.delete(document)

        raw_docs = session.scalars(
            select(AlbaranDocumentOrm).where(
                AlbaranDocumentOrm.source_sha256 == source_sha256,
            )
        ).all()
        for document in raw_docs:
            session.delete(document)

        session.flush()

    @staticmethod
    def _coerce_dict(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _average_confidence(lines: list[LineaAlbaran]) -> float | None:
        values = [
            float(line.confianza_pct)
            for line in lines
            if line.confianza_pct is not None
        ]
        if not values:
            return None
        return round(sum(values) / len(values), 2)

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
        raw_lines: list[LineaAlbaran],
        document_confidence_pct: float | None,
        review_required: bool | None,
        review_reasons: list[str] | None,
        comparison_summary: Dict[str, Any] | None,
        line_results: list[LineMergeResult] | None,
    ) -> AlbaranDocumentOrm | AlbaranDocumentMergeOrm:
        cabecera: CabeceraAlbaran = provider_envelope.data.cabecera
        payload = provider_envelope.model_dump(by_alias=True)
        lines = self._build_lines(
            orm_line_cls=orm_line_cls,
            document_id=document_id,
            provider_origin=provider_origin,
            source_lines=raw_lines,
            line_results=line_results,
        )
        return orm_document_cls(
            id=document_id,
            provider_origin=provider_origin,
            source_document_id=provider_envelope.meta.source_sha256,
            document_storage_ref=stored_file.relative_path,
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
            raw_extraction_json=json.dumps(payload, ensure_ascii=False, indent=2),
            confidence_pct_calc=document_confidence_pct,
            review_required=review_required,
            review_reasons_json=(
                json.dumps(review_reasons, ensure_ascii=False, indent=2)
                if review_reasons is not None
                else None
            ),
            comparison_summary_json=(
                json.dumps(comparison_summary, ensure_ascii=False, indent=2)
                if comparison_summary is not None
                else None
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
        line_results: list[LineMergeResult] | None,
    ) -> list[AlbaranLineOrm | AlbaranLineMergeOrm]:
        if line_results is None:
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
                    confidence_pct_calc=None,
                    line_match_score=None,
                    comparison_status_json=None,
                    field_scores_json=None,
                )
                for index, line in enumerate(source_lines, start=1)
            ]

        return [
            orm_line_cls(
                document_id=document_id,
                provider_origin=result.provider_origin,
                line_index=index,
                external_line_id=result.merged_line.id,
                cabecera_id=result.merged_line.cabecera_id,
                codigo=result.merged_line.codigo,
                cantidad=result.merged_line.cantidad,
                concepto=result.merged_line.concepto,
                precio=result.merged_line.precio,
                descuento=result.merged_line.descuento,
                precio_neto=result.merged_line.precio_neto,
                codigo_imputacion=result.merged_line.codigo_imputacion,
                confianza_pct=result.raw_openai_confidence_pct,
                confidence_pct_calc=result.confidence_pct_calc,
                line_match_score=result.line_match_score,
                comparison_status_json=json.dumps(
                    result.comparison_status,
                    ensure_ascii=False,
                    indent=2,
                ),
                field_scores_json=json.dumps(
                    result.field_scores,
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            for index, result in enumerate(line_results, start=1)
        ]
