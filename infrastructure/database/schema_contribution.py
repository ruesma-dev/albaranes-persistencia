# infrastructure/database/schema_contribution.py
"""Contrato público de schema del servicio 3 (albaranes-persistence-api).

Expone el DDL de TODAS las tablas que sv3 escribe, en formato de
sentencias SQL idempotentes que el orquestador (sv7) puede aplicar
contra la BBDD compartida.

Pertenencia (qué tablas son propias de sv3 / schema ``albaran_persist``):

    - albaran_documents
    - albaran_lines
    - albaran_documents_merge
    - albaran_lines_merge
    - albaran_contratos_merge
    - albaran_contrato_lines_merge
    - contratos_cache
    - contrato_cache_lines

NO incluye las tablas de valoración (``albaran_valuations``,
``albaran_line_valuations``, ``contrato_lines_derived``): esas son
propiedad del servicio 6 (schema ``valuation``). Si el sv3 las
"replicaba" antes en su ``_VALUATION_DDL``, ese pedazo se elimina del
sv3 con este refactor.

Implementación (Opción C — exporter mixto)
------------------------------------------
El sv3 mantiene su mecanismo interno de creación de tablas
(``Base.metadata.create_all`` + ``_ensure_compatible_schema`` +
``apply_phase2_ddl``) tal cual está hoy. Este archivo es solo una
**vista exportable** del schema:

  * Para las tablas iniciales: usamos el DDL compiler de SQLAlchemy
    contra ``orm_models.Base`` para generar las sentencias
    ``CREATE TABLE`` y ``CREATE INDEX``. Esto las saca directamente
    del ORM declarativo, así que NO hay riesgo de divergencia entre
    "lo que sv3 crea" y "lo que sv7 cree que sv3 crea".

  * Para los ALTERs evolutivos: lista manual con la misma SQL que
    el sv3 ejecuta hoy en ``_ensure_compatible_schema()``.
    Idempotentes (todos con ``IF NOT EXISTS``).

  * Para las columnas de fase 2: misma lista que ``phase2_ddl.py``
    del sv3. Sigue viva ahí; aquí simplemente la importamos para no
    duplicar literales.

Por qué esta opción (mixta y NO "todo desde Base.metadata"):
los ALTERs son una historia de evolución (DROP NOT NULL, UPDATE,
SET DEFAULT, etc.) que no se puede reconstruir desde el ORM
final — solo desde la secuencia ordenada de cambios.
"""
from __future__ import annotations

import logging
from typing import List, Tuple

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import (
    CreateIndex,
    CreateTable,
    DropConstraint,
    Table,
)

# Importamos el Base con TODOS los modelos registrados. Asegurarse de
# importar también los modelos hijos para que se registren en
# Base.metadata aunque no se usen aquí explícitamente.
from infrastructure.database.orm_models import (
    Base,
    AlbaranDocumentMergeOrm,  # noqa: F401  (registra metadata)
    AlbaranDocumentOrm,       # noqa: F401
    AlbaranLineMergeOrm,      # noqa: F401
    AlbaranLineOrm,           # noqa: F401
)
from infrastructure.database.orm_contrato_models import (  # noqa: F401
    AlbaranContratoLineMergeOrm,
    AlbaranContratoMergeOrm,
)
from infrastructure.database.orm_contrato_cache_models import (  # noqa: F401
    ContratoCacheLineOrm,
    ContratoCacheOrm,
)
from infrastructure.database.phase2_ddl import _PHASE2_DDL

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Identidad del schema (consumido por el sv7).
# --------------------------------------------------------------------------- #

SCHEMA_NAME: str = "albaran_persist"
"""Nombre lógico único del schema (consumido por sv7 como clave de
dependencia y como label en los logs)."""

SCHEMA_VERSION: int = 1
"""Versión informativa. Súbela al añadir / cambiar / eliminar tablas
o columnas. NO se usa para migraciones (no hay histórico aquí), solo
para diagnóstico ("aplicando albaran_persist v1") y detección de
deploys desincronizados."""

SCHEMA_DEPENDS_ON: List[str] = []
"""Sv3 es la base — no depende de ningún otro contributor."""


# --------------------------------------------------------------------------- #
# Tablas que este schema CREA (ownership).
# --------------------------------------------------------------------------- #

_OWNED_TABLE_NAMES: Tuple[str, ...] = (
    "albaran_documents",
    "albaran_lines",
    "albaran_documents_merge",
    "albaran_lines_merge",
    "albaran_contratos_merge",
    "albaran_contrato_lines_merge",
    "contratos_cache",
    "contrato_cache_lines",
)


# --------------------------------------------------------------------------- #
# ALTERs evolutivos (compatibilidad con BBDDs existentes).
#
# Misma SQL que ``_ensure_compatible_schema()`` del repositorio. Los
# replicamos aquí porque ``Base.metadata.create_all()`` solo crea
# tablas — no añade columnas a tablas que ya existen.
#
# Si sv3 corre primero contra una BBDD nueva, esto no hace nada
# (CREATE TABLE ya las puso bien). Si sv3 corre contra una BBDD
# antigua, los ALTERs la actualizan al estado deseado.
#
# Reglas estrictas:
#   - Toda sentencia debe ser idempotente.
#   - Ningún DROP / RENAME que pierda datos.
#   - Si necesitas un cambio NO idempotente, eso es Alembic. Avísame.
# --------------------------------------------------------------------------- #

def _alter_columns_for_document_tables() -> List[Tuple[str, str]]:
    """ALTERs aplicados a ``albaran_documents`` y ``albaran_documents_merge``.

    Mantiene paridad con ``_ensure_compatible_schema``. Para evitar
    duplicar 22 sentencias por tabla, lo generamos en bucle.
    """
    statements: List[Tuple[str, str]] = []
    for table_name, default_provider in (
        ("albaran_documents", "openai"),
        ("albaran_documents_merge", "gemini_filled"),
    ):
        statements.extend([
            (f"ALTER {table_name}.provider_origin add",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS provider_origin VARCHAR(32)"),
            (f"UPDATE {table_name}.provider_origin default",
             f"UPDATE {table_name} SET provider_origin = '{default_provider}' "
             f"WHERE provider_origin IS NULL"),
            (f"ALTER {table_name}.provider_origin not_null",
             f"ALTER TABLE {table_name} "
             f"ALTER COLUMN provider_origin SET NOT NULL"),
            (f"ALTER {table_name}.source_document_id",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS source_document_id VARCHAR(64)"),
            (f"ALTER {table_name}.document_storage_ref",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS document_storage_ref VARCHAR(1024)"),
            (f"ALTER {table_name}.source_attachment_filename",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS source_attachment_filename VARCHAR(255)"),
            (f"ALTER {table_name}.source_attachment_mime_type",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS source_attachment_mime_type VARCHAR(255)"),
            (f"ALTER {table_name}.source_attachment_sha256",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS source_attachment_sha256 VARCHAR(64)"),
            (f"ALTER {table_name}.page_number",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS page_number INTEGER"),
            (f"ALTER {table_name}.page_count",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS page_count INTEGER"),
            (f"ALTER {table_name}.ia_input_json",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS ia_input_json TEXT"),
            (f"ALTER {table_name}.ia_output_json",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS ia_output_json TEXT"),
            (f"ALTER {table_name}.ia_input_relative_path",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS ia_input_relative_path VARCHAR(1024)"),
            (f"ALTER {table_name}.ia_input_web_url",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS ia_input_web_url VARCHAR(1024)"),
            (f"ALTER {table_name}.ia_output_relative_path",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS ia_output_relative_path VARCHAR(1024)"),
            (f"ALTER {table_name}.ia_output_web_url",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS ia_output_web_url VARCHAR(1024)"),
            (f"ALTER {table_name}.raw_context_json",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS raw_context_json TEXT"),
            (f"ALTER {table_name}.confidence_pct_calc",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS confidence_pct_calc DOUBLE PRECISION"),
            (f"ALTER {table_name}.review_required",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS review_required BOOLEAN"),
            (f"ALTER {table_name}.review_reasons_json",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS review_reasons_json TEXT"),
            (f"ALTER {table_name}.comparison_summary_json",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS comparison_summary_json TEXT"),
            (f"UPDATE {table_name}.source_document_id backfill",
             f"UPDATE {table_name} SET source_document_id = source_sha256 "
             f"WHERE source_document_id IS NULL"),
            (f"UPDATE {table_name}.document_storage_ref backfill",
             f"UPDATE {table_name} SET document_storage_ref = sharepoint_relative_path "
             f"WHERE document_storage_ref IS NULL"),
        ])
    return statements


def _alter_columns_for_line_tables() -> List[Tuple[str, str]]:
    """ALTERs aplicados a ``albaran_lines`` y ``albaran_lines_merge``."""
    statements: List[Tuple[str, str]] = []
    for table_name, default_provider in (
        ("albaran_lines", "openai"),
        ("albaran_lines_merge", "gemini_filled"),
    ):
        statements.extend([
            (f"ALTER {table_name}.provider_origin add",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS provider_origin VARCHAR(32)"),
            (f"UPDATE {table_name}.provider_origin default",
             f"UPDATE {table_name} SET provider_origin = '{default_provider}' "
             f"WHERE provider_origin IS NULL"),
            (f"ALTER {table_name}.provider_origin not_null",
             f"ALTER TABLE {table_name} "
             f"ALTER COLUMN provider_origin SET NOT NULL"),
            (f"ALTER {table_name}.confidence_pct_calc",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS confidence_pct_calc DOUBLE PRECISION"),
            (f"ALTER {table_name}.line_match_score",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS line_match_score DOUBLE PRECISION"),
            (f"ALTER {table_name}.comparison_status_json",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS comparison_status_json TEXT"),
            (f"ALTER {table_name}.field_scores_json",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS field_scores_json TEXT"),
            (f"ALTER {table_name}.unidad_medida",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS unidad_medida VARCHAR(32)"),
            (f"ALTER {table_name}.contexto_linea_json",
             f"ALTER TABLE {table_name} "
             f"ADD COLUMN IF NOT EXISTS contexto_linea_json TEXT"),
        ])
    return statements


def _alter_columns_for_contrato_tables() -> List[Tuple[str, str]]:
    """ALTERs sobre ``albaran_contratos_merge`` y ``albaran_contrato_lines_merge``."""
    return [
        ("ALTER albaran_documents_merge.selected_contrato_codigo",
         "ALTER TABLE albaran_documents_merge "
         "ADD COLUMN IF NOT EXISTS selected_contrato_codigo VARCHAR(64)"),
        ("ALTER albaran_contratos_merge.gra_rep_ide",
         "ALTER TABLE albaran_contratos_merge "
         "ADD COLUMN IF NOT EXISTS gra_rep_ide INTEGER"),
        ("ALTER albaran_contratos_merge.pdf_sharepoint_relative_path",
         "ALTER TABLE albaran_contratos_merge "
         "ADD COLUMN IF NOT EXISTS pdf_sharepoint_relative_path VARCHAR(1024)"),
        ("ALTER albaran_contratos_merge.pdf_sharepoint_web_url",
         "ALTER TABLE albaran_contratos_merge "
         "ADD COLUMN IF NOT EXISTS pdf_sharepoint_web_url VARCHAR(1024)"),
        ("ALTER albaran_contrato_lines_merge.codigo_partida",
         "ALTER TABLE albaran_contrato_lines_merge "
         "ADD COLUMN IF NOT EXISTS codigo_partida VARCHAR(64)"),
        ("ALTER albaran_contrato_lines_merge.descripcion_partida",
         "ALTER TABLE albaran_contrato_lines_merge "
         "ADD COLUMN IF NOT EXISTS descripcion_partida TEXT"),
    ]


def _constraint_cleanups() -> List[Tuple[str, str]]:
    """DROP CONSTRAINT IF EXISTS y CREATE INDEX IF NOT EXISTS.

    Los DROP CONSTRAINT son de migraciones antiguas (constraints con
    nombres legacy). Idempotentes con IF EXISTS.
    """
    return [
        ("DROP albaran_documents.source_sha256_key",
         "ALTER TABLE albaran_documents "
         "DROP CONSTRAINT IF EXISTS albaran_documents_source_sha256_key"),
        ("DROP uq_albaran_documents_gem_sha_provider",
         "ALTER TABLE albaran_documents_merge "
         "DROP CONSTRAINT IF EXISTS uq_albaran_documents_gem_sha_provider"),
        ("DROP albaran_documents_gem_source_sha256_key",
         "ALTER TABLE albaran_documents_merge "
         "DROP CONSTRAINT IF EXISTS albaran_documents_gem_source_sha256_key"),
        ("INDEX uq_albaran_documents_sha_provider",
         "CREATE UNIQUE INDEX IF NOT EXISTS uq_albaran_documents_sha_provider "
         "ON albaran_documents (source_sha256, provider_origin)"),
        ("INDEX ix_albaran_contratos_merge_document",
         "CREATE INDEX IF NOT EXISTS ix_albaran_contratos_merge_document "
         "ON albaran_contratos_merge (document_id)"),
        ("INDEX ix_albaran_contrato_lines_merge_contrato",
         "CREATE INDEX IF NOT EXISTS ix_albaran_contrato_lines_merge_contrato "
         "ON albaran_contrato_lines_merge (contrato_id)"),
        ("INDEX ix_albaran_contrato_lines_merge_partida",
         "CREATE INDEX IF NOT EXISTS ix_albaran_contrato_lines_merge_partida "
         "ON albaran_contrato_lines_merge (codigo_partida)"),
    ]


def _phase2_alters() -> List[Tuple[str, str]]:
    """Reusa la lista canónica que vive en ``phase2_ddl.py``.

    Mantenemos la fuente única ahí — aquí solo le ponemos label.
    """
    return [
        (f"PHASE2[{idx + 1}/{len(_PHASE2_DDL)}]", sql)
        for idx, sql in enumerate(_PHASE2_DDL)
    ]


# --------------------------------------------------------------------------- #
# CREATE TABLE / CREATE INDEX desde Base.metadata.
# --------------------------------------------------------------------------- #

def _create_statements_from_metadata() -> List[Tuple[str, str]]:
    """Genera ``CREATE TABLE IF NOT EXISTS`` para cada tabla ORM y sus
    índices, en orden topológico (FKs respetadas).

    Usamos el dialecto PostgreSQL para que los tipos / sintaxis sean
    los correctos (DOUBLE PRECISION, SERIAL, etc.).
    """
    dialect = postgresql.dialect()
    statements: List[Tuple[str, str]] = []

    # Base.metadata.sorted_tables ya respeta el orden por FKs.
    for table in Base.metadata.sorted_tables:
        # CREATE TABLE — añadimos IF NOT EXISTS para hacerlo idempotente.
        create_ddl = str(
            CreateTable(table, if_not_exists=True).compile(dialect=dialect)
        ).strip()
        statements.append(
            (f"CREATE {table.name}", create_ddl)
        )
        # Índices declarados en __table_args__.
        for index in table.indexes:
            index_ddl = str(
                CreateIndex(index, if_not_exists=True).compile(dialect=dialect)
            ).strip()
            statements.append(
                (f"INDEX {table.name}.{index.name}", index_ddl),
            )

    return statements


# --------------------------------------------------------------------------- #
# API pública (consumida por sv7 y por el endpoint /schema/ddl).
# --------------------------------------------------------------------------- #

def get_ddl_statements() -> List[Tuple[str, str]]:
    """Devuelve la lista ordenada de sentencias DDL idempotentes.

    Orden:
      1. CREATE TABLE / CREATE INDEX desde el ORM (sv3 base).
      2. ALTERs evolutivos sobre tablas de documentos / líneas.
      3. ALTERs sobre tablas de contratos / partidas.
      4. Limpieza de constraints + índices.
      5. Columnas de fase 2 (review_phase2_*, source_phase).

    Returns
    -------
    list[tuple[str, str]]
        Pares ``(label, sql)``.
    """
    statements: List[Tuple[str, str]] = []
    statements.extend(_create_statements_from_metadata())
    statements.extend(_alter_columns_for_document_tables())
    statements.extend(_alter_columns_for_line_tables())
    statements.extend(_alter_columns_for_contrato_tables())
    statements.extend(_constraint_cleanups())
    statements.extend(_phase2_alters())
    return statements


def get_owned_table_names() -> List[str]:
    """Tablas que este schema CREA (no las que solo referencia)."""
    return list(_OWNED_TABLE_NAMES)


def get_external_table_dependencies() -> List[str]:
    """Tablas de OTROS schemas que este schema referencia por FK.

    Sv3 es la base — no referencia a nadie. Vacío.
    """
    return []
