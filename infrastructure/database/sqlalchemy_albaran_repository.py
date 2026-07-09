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
from domain.models.header_resolution_models import MergeHeaderForResolution
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


def _dump_contexto_linea(ctx) -> str | None:
    """Serializa un ``ContextoLinea`` (o equivalente) a JSON string.

    Usado al persistir líneas en ``albaran_lines`` y
    ``albaran_lines_merge``. Tolera:
      - ``None`` (la línea no es de familia compleja) → devuelve None.
      - Un ``BaseModel`` Pydantic con ``model_dump()`` → usa exclude_none.
      - Un dict (defensivo) → serializa filtrando Nones.
      - Cualquier otra cosa → None (nunca lanza).

    No guardamos ``{}`` — si el modelo no aporta nada, devolvemos None
    para que la columna quede limpia.
    """
    if ctx is None:
        return None
    try:
        data = ctx.model_dump(exclude_none=True)
    except AttributeError:
        if isinstance(ctx, dict):
            data = {k: v for k, v in ctx.items() if v is not None}
        else:
            return None
    except Exception:
        return None
    if not data:
        return None
    return json.dumps(data, ensure_ascii=False)


# =============================================================================
# NOTA HISTÓRICA — DDL de valoración eliminado.
#
# Antes existía aquí ``_VALUATION_DDL`` con las tablas de valoración
# (albaran_valuations, albaran_line_valuations, contrato_lines_derived)
# REPLICADAS desde el servicio 6. El servicio 3 las creaba al arrancar
# como "doble red de seguridad".
#
# Esa duplicación fue la causa del bug del 2026-05: cuando el sv6 añadió
# una columna nueva (``descuento_albaran_aplicado``), el sv3 la creaba
# con el schema viejo y el sv6 ya no podía actualizarla.
#
# Refactor "schema contributors" (mayo 2026):
#   * El sv6 es ahora la ÚNICA fuente de verdad para las tablas de
#     valoración. Expone ``GET /schema/ddl`` con su DDL.
#   * El sv7 (orquestador) descubre los contributors de sv3 y sv6 y
#     aplica todo el DDL en orden topológico al arrancar.
#   * El sv3 contribuye solo lo SUYO (ver ``schema_contribution.py``).
# =============================================================================


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
                    # Soft-delete (jun 2026): borrado lógico + auditoría.
                    # is_active NOT NULL DEFAULT true marca como activas las
                    # filas existentes al migrar.
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS deleted_at_utc VARCHAR(64)",
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(255)",
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
                    # Unidad de medida por línea. Antes no se persistía
                    # (el svc5 leía NULL::text y el prefilter la
                    # clasificaba como 'unknown'). Ahora la guardamos
                    # para que el valorador pueda trabajar con la
                    # unidad real.
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS unidad_medida VARCHAR(32)",
                    # Contexto estructural de la línea (familia hormigón /
                    # combustible / alquiler_maquinaria / otro). Ver
                    # domain/models/contexto_linea.py. Idempotente.
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS contexto_linea_json TEXT",
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
                "ALTER TABLE albaran_contratos_merge "
                "ADD COLUMN IF NOT EXISTS md_sharepoint_relative_path VARCHAR(1024)",
                "ALTER TABLE albaran_contratos_merge "
                "ADD COLUMN IF NOT EXISTS md_sharepoint_web_url VARCHAR(1024)",
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
            # ------------------------------------------------------------ #
            # Soft-delete (jun 2026): unicidad por source_sha256 como ÍNDICE
            # PARCIAL «WHERE is_active».
            #
            # Migramos los UNIQUE globales a índices parciales: un albarán
            # borrado (is_active=false) deja de ocupar el "slot único", de
            # modo que el mismo PDF puede re-ingerirse creando uno nuevo
            # activo sin violar integridad. Cubrimos las dos formas posibles
            # (DROP CONSTRAINT y DROP INDEX) porque, según la versión, la
            # unicidad podía existir como constraint del ORM o como índice
            # creado a mano con el mismo nombre.
            # ------------------------------------------------------------ #
            # Tabla cruda por-proveedor (source_sha256, provider_origin).
            (
                "ALTER TABLE albaran_documents "
                "DROP CONSTRAINT IF EXISTS uq_albaran_documents_sha_provider"
            ),
            "DROP INDEX IF EXISTS uq_albaran_documents_sha_provider",
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_albaran_documents_sha_provider_active "
                "ON albaran_documents (source_sha256, provider_origin) "
                "WHERE is_active"
            ),
            # Tabla merge (la que consume sv4 y el dedup get_by_sha256).
            (
                "ALTER TABLE albaran_documents_merge "
                "DROP CONSTRAINT IF EXISTS uq_albaran_documents_merge_sha"
            ),
            "DROP INDEX IF EXISTS uq_albaran_documents_merge_sha",
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_albaran_documents_merge_sha_active "
                "ON albaran_documents_merge (source_sha256) "
                "WHERE is_active"
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
            # ------------------------------------------------------------ #
            # Índices únicos PARCIALES que respaldan el UPSERT de contratos:
            #   - cabecera: ON CONFLICT (document_id, sigrid_ide)
            #   - líneas:   ON CONFLICT (contrato_id, sigrid_ide)
            # Sin ellos, el ON CONFLICT lanza "no unique or exclusion
            # constraint matching" y (antes de los savepoints) abortaba el
            # lote entero — causa raíz del e2e. Parciales (WHERE sigrid_ide
            # IS NOT NULL) para no chocar con filas legacy sin sigrid_ide.
            # IF NOT EXISTS → idempotente y seguro si ya se crearon a mano.
            # ------------------------------------------------------------ #
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_albaran_contratos_merge_doc_sigrid "
                "ON albaran_contratos_merge (document_id, sigrid_ide) "
                "WHERE sigrid_ide IS NOT NULL"
            ),
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_albaran_contrato_lines_merge_ctr_sigrid "
                "ON albaran_contrato_lines_merge (contrato_id, sigrid_ide) "
                "WHERE sigrid_ide IS NOT NULL"
            ),
        ]

        with self._session_factory.create_session() as session:
            for ddl in alter_statements + constraint_statements:
                session.execute(text(ddl))
            session.commit()

        # NOTA — El antiguo bloque que ejecutaba _VALUATION_DDL aquí
        # (creando las tablas de valoración del sv6) se ha eliminado.
        # Esa responsabilidad ahora vive en el sv6 (single source of
        # truth) y la aplica el orquestador sv7 vía su contributor.
        # Ver schema_contribution.py para los detalles.

    def get_by_sha256(self, source_sha256: str) -> ExistingDocument | None:
        self.initialize()
        with self._session_factory.create_session() as session:
            result_document = session.scalar(
                select(AlbaranDocumentMergeOrm).where(
                    AlbaranDocumentMergeOrm.source_sha256 == source_sha256,
                    # Soft-delete (jun 2026): un albarán borrado (papelera)
                    # NO cuenta como "ya existe", así que el mismo PDF se
                    # re-procesa creando uno nuevo activo. El índice único
                    # parcial «WHERE is_active» garantiza que no haya choque
                    # de claves con el borrado que sigue en la tabla.
                    AlbaranDocumentMergeOrm.is_active.is_(True),
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

    def get_merge_fecha(self, *, document_id: str) -> str | None:
        """Devuelve la fecha del albarán del merge en formato ISO."""
        self.initialize()
        with self._session_factory.create_session() as session:
            document = session.get(AlbaranDocumentMergeOrm, document_id)
            if document is None:
                return None
            return document.fecha

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
    # Puerto HeaderMergeRepository (resolucion determinista de cabecera)
    # ================================================================== #
    def get_merge_header_for_resolution(
        self,
        *,
        document_id: str,
    ) -> MergeHeaderForResolution | None:
        self.initialize()
        with self._session_factory.create_session() as session:
            document = session.get(AlbaranDocumentMergeOrm, document_id)
            if document is None:
                return None
            return MergeHeaderForResolution(
                obra_codigo=document.obra_codigo,
                obra_nombre=document.obra_nombre,
                obra_direccion=document.obra_direccion,
                proveedor_cif=document.proveedor_cif,
                proveedor_nombre=document.proveedor_nombre,
            )

    def update_merge_resolved_header(
        self,
        *,
        document_id: str,
        obra_codigo_det: str | None,
        proveedor_cif_det: str | None,
    ) -> None:
        """Persiste la resolucion determinista de cabecera de forma
        CONSERVADORA:
          - obra_codigo: si esta vacio y hay deduccion -> se fija y
            origen='deterministic'. Si ya habia codigo y el origen estaba
            sin marcar -> origen='ia'. Nunca pisa un origen 'manual' ni un
            codigo existente.
          - proveedor_cif: misma logica.
        """
        self.initialize()
        with self._session_factory.create_session() as session:
            document = session.get(AlbaranDocumentMergeOrm, document_id)
            if document is None:
                raise KeyError(f"Documento merge no encontrado: {document_id}")

            # El resolver SOLO devuelve *_det cuando decidio que el dato
            # actual falta o no es valido. Por eso, si llega un *_det, se
            # aplica (salvo que el revisor lo hubiera fijado a 'manual').
            # Si no llega *_det pero ya habia dato sin marcar -> 'ia'.
            cur_code = (document.obra_codigo or "").strip()
            cur_code_origen = (document.obra_codigo_origen or "").strip()
            if obra_codigo_det and cur_code_origen != "manual":
                document.obra_codigo = obra_codigo_det
                document.obra_codigo_origen = "deterministic"
            elif cur_code and not cur_code_origen:
                document.obra_codigo_origen = "ia"

            cur_cif = (document.proveedor_cif or "").strip()
            cur_cif_origen = (document.proveedor_cif_origen or "").strip()
            if proveedor_cif_det and cur_cif_origen != "manual":
                document.proveedor_cif = proveedor_cif_det
                document.proveedor_cif_origen = "deterministic"
            elif cur_cif and not cur_cif_origen:
                document.proveedor_cif_origen = "ia"

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
        """Mapa de PDFs ya subidos para los contratos de un documento."""
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

    def has_selected_contrato_with_lines(
        self,
        *,
        document_id: str,
    ) -> tuple[bool, str | None]:
        """Devuelve (hay_contrato_con_lineas, codigo_contrato_seleccionado).

        Se considera que el documento está "listo para valorar" si:
          1. ``albaran_documents_merge.selected_contrato_codigo`` no es NULL.
          2. Existe al menos una línea en ``albaran_contrato_lines_merge``
             asociada a ese contrato del documento.

        Este chequeo lo usa ``PersistAlbaranPipeline`` justo después del
        enrichment de contratos para decidir si dispara o no el trigger
        automático de valoración (servicio 6).
        """
        self.initialize()
        with self._session_factory.create_session() as session:
            row = session.execute(
                text(
                    "SELECT d.selected_contrato_codigo AS codigo "
                    "FROM albaran_documents_merge d "
                    "WHERE d.id = :document_id "
                    "LIMIT 1"
                ),
                {"document_id": document_id},
            ).mappings().first()
            if row is None:
                return False, None

            codigo = row.get("codigo")
            if not codigo:
                return False, None

            has_lines = session.execute(
                text(
                    "SELECT 1 "
                    "FROM albaran_contratos_merge ch "
                    "JOIN albaran_contrato_lines_merge cl "
                    "  ON cl.contrato_id = ch.id "
                    "WHERE ch.document_id = :document_id "
                    "  AND ch.codigo_contrato = :codigo "
                    "LIMIT 1"
                ),
                {"document_id": document_id, "codigo": codigo},
            ).first()

            return (has_lines is not None, str(codigo))

    def get_selected_contrato_codigo(
        self,
        *,
        document_id: str,
    ) -> str | None:
        """Devuelve selected_contrato_codigo del merge. KeyError si el
        documento no existe (para que el endpoint PATCH responda 404)."""
        self.initialize()
        with self._session_factory.create_session() as session:
            row = session.execute(
                text(
                    "SELECT selected_contrato_codigo AS codigo "
                    "FROM albaran_documents_merge "
                    "WHERE id = :document_id "
                    "LIMIT 1"
                ),
                {"document_id": document_id},
            ).mappings().first()
            if row is None:
                raise KeyError(
                    f"Documento merge no encontrado: {document_id}"
                )
            codigo = row.get("codigo")
            return str(codigo) if codigo else None

    def contrato_exists_for_document(
        self,
        *,
        document_id: str,
        codigo_contrato: str,
    ) -> bool:
        """True si existe ``albaran_contratos_merge`` con ese par
        (document_id, codigo_contrato). Usado por el endpoint PATCH para
        evitar meter en BBDD un ``selected_contrato_codigo`` que apunte
        a un contrato que el enrichment no encontró."""
        self.initialize()
        with self._session_factory.create_session() as session:
            row = session.execute(
                text(
                    "SELECT 1 "
                    "FROM albaran_contratos_merge "
                    "WHERE document_id = :document_id "
                    "  AND codigo_contrato = :codigo "
                    "LIMIT 1"
                ),
                {
                    "document_id": document_id,
                    "codigo": codigo_contrato,
                },
            ).first()
            return row is not None

    def upsert_contratos(
        self,
        *,
        document_id: str,
        contratos: list[ContratoEnrichmentResult],
    ) -> None:
        """UPSERT de cabeceras + líneas de contrato por ``sigrid_ide``.

        Antes este método se llamaba ``replace_contratos`` y hacía
        DELETE+INSERT por ``document_id``. El problema: si dos albaranes
        traían el mismo contrato del ERP, en BBDD aparecían dos filas
        distintas con el mismo ``codigo_contrato`` (una por cada
        ``document_id``). Eso impedía la conciliación correcta entre
        valoraciones y contrato.

        Ahora cada cabecera y cada línea tiene un ``sigrid_ide`` (=
        ``ctr.ide`` y ``ctrpro.ide`` en el ERP, ambos INDICE PRIMARIO
        únicos y estables). Hacemos ``INSERT ... ON CONFLICT
        (sigrid_ide) DO UPDATE``: cuando el contrato ya existe se
        actualiza con los datos más frescos; cuando no, se inserta.

        Comportamiento clave (opción B — una fila de contrato POR
        DOCUMENTO):
          * La clave de UPSERT de la cabecera es ``(document_id,
            sigrid_ide)`` y la de las líneas ``(contrato_id,
            sigrid_ide)``: cada albarán tiene su PROPIA cabecera/líneas
            del contrato, con su ``document_id`` correcto. Antes la clave
            era solo ``sigrid_ide`` (una fila compartida) y el
            ``document_id`` quedaba con el del último albarán, rompiendo
            las búsquedas por documento.
          * Las líneas existentes que NO aparecen ya en la respuesta
            de Sigrid de hoy NO se borran. Si Sigrid quitara una línea
            (no debería pasar), preferimos conservarla histórica que
            eliminarla en silencio.
          * Filas SIN ``sigrid_ide`` (registros legacy o respuestas
            de Sigrid mal formadas) caen al modo INSERT clásico — se
            crean filas nuevas. Estas filas perderán el beneficio del
            UPSERT pero el sistema sigue funcionando.

        El método antiguo ``replace_contratos`` se mantiene como alias
        deprecado por compatibilidad con código que aún lo llamara.
        """
        self.initialize()
        now = datetime.now(timezone.utc).isoformat()
        with self._session_factory.create_session() as session:
            merge_doc = session.get(AlbaranDocumentMergeOrm, document_id)
            if merge_doc is None:
                raise KeyError(f"Documento merge no encontrado: {document_id}")

            cabeceras_upserted = 0
            cabeceras_inserted_legacy = 0
            cabeceras_failed = 0
            total_lines_upserted = 0
            total_lines_inserted_legacy = 0
            total_lines_failed = 0

            for contrato in contratos:
                # Savepoint por CABECERA: si el upsert de la cabecera falla
                # (p. ej. ON CONFLICT sin índice, dato corrupto), se revierte
                # SOLO este contrato y la transacción del lote sigue viva.
                cabecera_id = None
                was_upsert = False
                try:
                    with session.begin_nested():
                        cabecera_id, was_upsert = self._upsert_contrato_cabecera(
                            session=session,
                            contrato=contrato,
                            document_id=document_id,
                            now=now,
                        )
                except Exception:  # noqa: BLE001 — aislar el fallo de cabecera
                    cabecera_id = None
                    cabeceras_failed += 1
                    logger.exception(
                        "[contrato-enrichment][repo] upsert_contratos: "
                        "cabecera codigo=%s falló; se revierte su savepoint "
                        "y se continúa con el resto del lote.",
                        contrato.codigo_contrato,
                    )
                if cabecera_id is None:
                    logger.warning(
                        "[contrato-enrichment][repo] upsert_contratos: "
                        "no se pudo upsertar cabecera codigo=%s; "
                        "saltamos sus líneas.",
                        contrato.codigo_contrato,
                    )
                    continue
                if was_upsert:
                    cabeceras_upserted += 1
                else:
                    cabeceras_inserted_legacy += 1

                for line in (contrato.lines or []):
                    # Savepoint por LÍNEA: una línea defectuosa se omite sin
                    # arrastrar al resto (antes un único fallo abortaba la
                    # transacción y se perdían las 429 líneas en el commit).
                    try:
                        with session.begin_nested():
                            line_was_upsert = self._upsert_contrato_linea(
                                session=session,
                                line=line,
                                contrato_id=cabecera_id,
                                codigo_contrato=contrato.codigo_contrato,
                                now=now,
                            )
                    except Exception:  # noqa: BLE001 — aislar el fallo de línea
                        total_lines_failed += 1
                        logger.exception(
                            "[contrato-enrichment][repo] upsert_contratos: "
                            "línea de contrato codigo=%s falló; se revierte "
                            "su savepoint y se continúa.",
                            contrato.codigo_contrato,
                        )
                        continue
                    if line_was_upsert:
                        total_lines_upserted += 1
                    else:
                        total_lines_inserted_legacy += 1

            session.commit()
            logger.info(
                "[contrato-enrichment][repo] upsert_contratos: "
                "document_id=%s contratos=%s "
                "cabeceras_upsert=%s cabeceras_legacy_insert=%s "
                "cabeceras_failed=%s "
                "lineas_upsert=%s lineas_legacy_insert=%s "
                "lineas_failed=%s "
                "pdfs_con_path_inicial=%s",
                document_id,
                len(contratos),
                cabeceras_upserted,
                cabeceras_inserted_legacy,
                cabeceras_failed,
                total_lines_upserted,
                total_lines_inserted_legacy,
                total_lines_failed,
                sum(
                    1
                    for c in contratos
                    if c.pdf_sharepoint_relative_path is not None
                ),
            )

    def replace_contratos(
        self,
        *,
        document_id: str,
        contratos: list[ContratoEnrichmentResult],
    ) -> None:
        """Alias DEPRECADO de :meth:`upsert_contratos`.

        Se mantiene solo por compatibilidad con código antiguo (o tests
        que aún lo invocan). En cuanto todo el código llame a
        ``upsert_contratos``, este alias se podrá borrar.

        OJO: el comportamiento ya NO es DELETE+INSERT. Si por algún
        motivo necesitas el comportamiento antiguo de "limpiar todo lo
        del document_id y re-insertar", hablemos antes — probablemente
        es síntoma de otro problema.
        """
        logger.warning(
            "[contrato-enrichment][repo] replace_contratos() está DEPRECADO; "
            "usa upsert_contratos(). Se delega automáticamente."
        )
        return self.upsert_contratos(
            document_id=document_id,
            contratos=contratos,
        )

    @staticmethod
    def _upsert_contrato_cabecera(
        *,
        session: Any,
        contrato: ContratoEnrichmentResult,
        document_id: str,
        now: str,
    ) -> tuple[int | None, bool]:
        """Inserta o actualiza una cabecera de contrato.

        Returns
        -------
        tuple[int | None, bool]
            Par ``(cabecera_id, was_upsert)``. ``was_upsert`` es True
            si se hizo UPSERT por sigrid_ide; False si la fila no tenía
            sigrid_ide y se hizo INSERT clásico (modo legacy).
            ``cabecera_id`` es ``None`` solo si ambos caminos fallan.
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        # Camino legacy: sin sigrid_ide no podemos UPSERTear → INSERT.
        if contrato.sigrid_ide is None:
            header_orm = AlbaranContratoMergeOrm(
                document_id=document_id,
                sigrid_ide=None,
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
            return int(header_orm.id), False

        # Camino normal: UPSERT por sigrid_ide.
        values = {
            "document_id": document_id,
            "sigrid_ide": contrato.sigrid_ide,
            "codigo_contrato": contrato.codigo_contrato,
            "nombre_contrato": contrato.nombre_contrato,
            "fecha_alta_contrato": contrato.fecha_alta_contrato,
            "fecha_contrato": contrato.fecha_contrato,
            "vigencia_desde": contrato.vigencia_desde,
            "vigencia_hasta": contrato.vigencia_hasta,
            "importe_total": contrato.importe_total,
            "cif_proveedor": contrato.cif_proveedor,
            "nombre_proveedor": contrato.nombre_proveedor,
            "codigo_obra": contrato.codigo_obra,
            "nombre_obra": contrato.nombre_obra,
            "gra_rep_ide": contrato.gra_rep_ide,
            "pdf_sharepoint_relative_path":
                contrato.pdf_sharepoint_relative_path,
            "pdf_sharepoint_web_url": contrato.pdf_sharepoint_web_url,
            "fetched_at_utc": now,
        }
        stmt = pg_insert(AlbaranContratoMergeOrm).values(**values)
        # Columnas a refrescar en conflicto: TODAS menos la CLAVE, que
        # ahora es (document_id, sigrid_ide). document_id forma parte de
        # la clave (opción B: una fila de contrato POR DOCUMENTO), así
        # que no se actualiza aquí.
        update_columns = {
            col: getattr(stmt.excluded, col)
            for col in (
                "codigo_contrato",
                "nombre_contrato",
                "fecha_alta_contrato",
                "fecha_contrato",
                "vigencia_desde",
                "vigencia_hasta",
                "importe_total",
                "cif_proveedor",
                "nombre_proveedor",
                "codigo_obra",
                "nombre_obra",
                "fetched_at_utc",
            )
        }
        # ------------------------------------------------------------ #
        # FIX (jun 2026) — los paths del PDF NUNCA regresan a NULL.
        #
        # Antes ``pdf_sharepoint_*`` y ``gra_rep_ide`` se machacaban con
        # EXCLUDED tal cual. El enrichment solo rellena esos campos en el
        # DTO cuando pudo REUTILIZAR un PDF previo (mismo gra_rep_ide);
        # en cualquier otro caso llegan a None y el UPSERT BORRABA los
        # paths ya guardados. Combinado con el filtro de descarga
        # "solo el contrato seleccionado" (Paso 5-bis del enrichment),
        # el resultado era: PDF subido a SharePoint pero fila sin URL →
        # el botón "Abrir contrato en SharePoint" desaparecía del
        # portal (bug reportado).
        #
        # Con COALESCE(EXCLUDED.x, tabla.x):
        #   - Si el DTO trae paths (reutilización o re-descarga ya
        #     resuelta) → se actualizan.
        #   - Si el DTO trae None → se CONSERVAN los de BBDD. Si el
        #     gra_rep_ide cambió de verdad, el Paso 7 del enrichment
        #     re-descarga y ``update_contrato_pdf_paths`` pisa los
        #     paths con los nuevos; aquí solo evitamos la regresión.
        # ------------------------------------------------------------ #
        update_columns["gra_rep_ide"] = func.coalesce(
            stmt.excluded.gra_rep_ide,
            AlbaranContratoMergeOrm.gra_rep_ide,
        )
        update_columns["pdf_sharepoint_relative_path"] = func.coalesce(
            stmt.excluded.pdf_sharepoint_relative_path,
            AlbaranContratoMergeOrm.pdf_sharepoint_relative_path,
        )
        update_columns["pdf_sharepoint_web_url"] = func.coalesce(
            stmt.excluded.pdf_sharepoint_web_url,
            AlbaranContratoMergeOrm.pdf_sharepoint_web_url,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["document_id", "sigrid_ide"],
            index_where=text("sigrid_ide IS NOT NULL"),
            set_=update_columns,
        ).returning(AlbaranContratoMergeOrm.id)
        try:
            cabecera_id = session.execute(stmt).scalar_one()
        except Exception:
            logger.exception(
                "[contrato-enrichment][repo] FALLO upsert cabecera "
                "sigrid_ide=%s codigo=%s",
                contrato.sigrid_ide,
                contrato.codigo_contrato,
            )
            return None, True
        return int(cabecera_id), True

    @staticmethod
    def _upsert_contrato_linea(
        *,
        session: Any,
        line,  # ContratoLineFromSigrid
        contrato_id: int,
        codigo_contrato: str,
        now: str,
    ) -> bool:
        """Inserta o actualiza una línea de contrato.

        Returns
        -------
        bool
            True si se hizo UPSERT por sigrid_ide; False si la línea no
            tenía sigrid_ide y se hizo INSERT clásico (legacy).
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        line_sigrid_ide = getattr(line, "sigrid_ide", None)

        if line_sigrid_ide is None:
            session.add(
                AlbaranContratoLineMergeOrm(
                    contrato_id=contrato_id,
                    sigrid_ide=None,
                    codigo_contrato=codigo_contrato,
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
            return False

        values = {
            "contrato_id": contrato_id,
            "sigrid_ide": line_sigrid_ide,
            "codigo_contrato": codigo_contrato,
            "linea": line.linea,
            "numero_linea": line.numero_linea,
            "codigo_producto": line.codigo_producto,
            "codigo_alternativo": line.codigo_alternativo,
            "unidad_medida": line.unidad_medida,
            "descripcion_linea": line.descripcion_linea,
            "uds": line.uds,
            "cantidad_servida": line.cantidad_servida,
            "cantidad_facturada": line.cantidad_facturada,
            "pendiente_servir": line.pendiente_servir,
            "precio_unitario": line.precio_unitario,
            "precio_bruto": line.precio_bruto,
            "descuentos": line.descuentos,
            "importe_linea": line.importe_linea,
            "cuota_iva": line.cuota_iva,
            "doc_origen": line.doc_origen,
            "codigo_partida": line.codigo_partida,
            "descripcion_partida": line.descripcion_partida,
            "fetched_at_utc": now,
        }
        stmt = pg_insert(AlbaranContratoLineMergeOrm).values(**values)
        update_columns = {
            col: getattr(stmt.excluded, col)
            for col in (
                # contrato_id forma parte de la CLAVE (contrato_id,
                # sigrid_ide): cada cabecera por-documento tiene su propia
                # copia de la línea, así que NO se actualiza aquí.
                "codigo_contrato",
                "linea",
                "numero_linea",
                "codigo_producto",
                "codigo_alternativo",
                "unidad_medida",
                "descripcion_linea",
                "uds",
                "cantidad_servida",
                "cantidad_facturada",
                "pendiente_servir",
                "precio_unitario",
                "precio_bruto",
                "descuentos",
                "importe_linea",
                "cuota_iva",
                "doc_origen",
                "codigo_partida",
                "descripcion_partida",
                "fetched_at_utc",
            )
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["contrato_id", "sigrid_ide"],
            index_where=text("sigrid_ide IS NOT NULL"),
            set_=update_columns,
        )
        try:
            session.execute(stmt)
        except Exception:
            logger.exception(
                "[contrato-enrichment][repo] FALLO upsert linea "
                "sigrid_ide=%s contrato_id=%s codigo_contrato=%s",
                line_sigrid_ide,
                contrato_id,
                codigo_contrato,
            )
            return False
        return True

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

    def update_contrato_md_paths(
        self,
        *,
        document_id: str,
        codigo_contrato: str,
        relative_path: str | None,
        web_url: str | None,
    ) -> None:
        """Actualiza los paths del MARKDOWN para un contrato concreto.

        El sv5 prefiere el MD del contrato (lo manda como texto y no
        adjunta el PDF); para que lo encuentre, el enrichment persiste
        aquí el ``md_sharepoint_relative_path`` tras subirlo a SharePoint.
        """
        self.initialize()
        with self._session_factory.create_session() as session:
            session.execute(
                text(
                    "UPDATE albaran_contratos_merge "
                    "SET md_sharepoint_relative_path = :rel, "
                    "    md_sharepoint_web_url = :url "
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
                "[contrato-enrichment][repo] update_contrato_md_paths "
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

    def update_merge_proveedor_nombre(
        self,
        *,
        document_id: str,
        nombre_proveedor: str,
    ) -> bool:
        """Sobrescribe ``proveedor_nombre`` con la razon social canonica
        de Sigrid (``prv.raz``).

        Implementa el puerto declarado en
        ``domain/ports/contrato_merge_repository_port.py`` que hasta
        ahora NO tenia implementacion (la canonizacion del nombre de
        proveedor "existia" en el contrato del puerto pero nadie la
        ejecutaba — bug reportado: al elegir un proveedor existente, el
        nombre no se actualizaba con el de Sigrid).

        Devuelve True si cambio el valor; False si fue no-op (nombre
        vacio o identico). KeyError si el documento no existe.
        """
        nombre_clean = (nombre_proveedor or "").strip()
        if not nombre_clean:
            return False
        self.initialize()
        with self._session_factory.create_session() as session:
            document = session.get(AlbaranDocumentMergeOrm, document_id)
            if document is None:
                raise KeyError(
                    f"Documento merge no encontrado: {document_id}"
                )
            actual = (document.proveedor_nombre or "").strip()
            if actual == nombre_clean:
                return False
            document.proveedor_nombre = nombre_clean
            session.commit()
            logger.info(
                "[contrato-enrichment][repo] proveedor_nombre canonizado "
                "doc=%s %r -> %r",
                document_id,
                actual or None,
                nombre_clean,
            )
            return True

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
                    unidad_medida=line.unidad_medida,
                    precio=line.precio,
                    descuento=line.descuento,
                    precio_neto=line.precio_neto,
                    codigo_imputacion=line.codigo_imputacion,
                    confianza_pct=line.confianza_pct,
                    confidence_pct_calc=None,
                    line_match_score=None,
                    comparison_status_json=None,
                    field_scores_json=None,
                    contexto_linea_json=_dump_contexto_linea(
                        getattr(line, "contexto_linea", None)
                    ),
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
                unidad_medida=result.merged_line.unidad_medida,
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
                contexto_linea_json=_dump_contexto_linea(
                    getattr(result.merged_line, "contexto_linea", None)
                ),
            )
            for index, result in enumerate(line_results, start=1)
        ]