# infrastructure/database/phase2_ddl.py
"""DDL idempotente para soportar la persistencia de metadatos
de la revisión de fase 2 (sv2 phase-2).

Se aplica al arrancar sv3, después del DDL principal del repositorio.
Es 100% idempotente — todos los ALTER usan IF NOT EXISTS.

Columnas añadidas:

  En albaran_documents_merge:
    - review_phase2_status         VARCHAR(32)
        Resultado global de la fase 2: 'ok', 'ok_with_changes',
        'inconsistent', o NULL si fase 2 no se ejecutó.
    - review_phase2_summary        TEXT
        Explicación global breve devuelta por la fase 2.
    - review_phase2_changes_count  INTEGER
        Número de cambios propuestos por la fase 2 (no
        necesariamente todos aplicados).
    - review_phase2_payload_json   TEXT
        Payload completo en JSON: review_status, explicacion_global,
        cambios[], apply_summary, provider, model. Auditable para el
        algoritmo de confianza futuro.

  En albaran_lines_merge:
    - source_phase                 VARCHAR(16) DEFAULT 'phase_1'
        'phase_1' si la línea proviene de la extracción inicial.
        'phase_2' si la línea fue modificada o introducida por la
        fase 2 (revisión).
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from infrastructure.database.session_factory import SessionFactory

logger = logging.getLogger(__name__)


_PHASE2_DDL: tuple[str, ...] = (
    # albaran_documents_merge
    "ALTER TABLE albaran_documents_merge "
    "ADD COLUMN IF NOT EXISTS review_phase2_status VARCHAR(32)",
    "ALTER TABLE albaran_documents_merge "
    "ADD COLUMN IF NOT EXISTS review_phase2_summary TEXT",
    "ALTER TABLE albaran_documents_merge "
    "ADD COLUMN IF NOT EXISTS review_phase2_changes_count INTEGER",
    "ALTER TABLE albaran_documents_merge "
    "ADD COLUMN IF NOT EXISTS review_phase2_payload_json TEXT",
    "CREATE INDEX IF NOT EXISTS ix_albaran_documents_merge_review_phase2_status "
    "ON albaran_documents_merge(review_phase2_status)",

    # Origen determinista de cabecera (obra_codigo / proveedor_cif).
    "ALTER TABLE albaran_documents_merge "
    "ADD COLUMN IF NOT EXISTS obra_codigo_origen VARCHAR(24)",
    "ALTER TABLE albaran_documents_merge "
    "ADD COLUMN IF NOT EXISTS proveedor_cif_origen VARCHAR(24)",

    # albaran_lines_merge
    "ALTER TABLE albaran_lines_merge "
    "ADD COLUMN IF NOT EXISTS source_phase VARCHAR(16) "
    "NOT NULL DEFAULT 'phase_1'",
    "CREATE INDEX IF NOT EXISTS ix_albaran_lines_merge_source_phase "
    "ON albaran_lines_merge(source_phase)",
)


def apply_phase2_ddl(session_factory: SessionFactory) -> None:
    """Ejecuta los ALTER de fase 2. Llamar después del initialize()
    principal del repositorio (que ya creó las tablas merge)."""
    with session_factory.create_session() as session:
        try:
            logger.info(
                "[svc3-ddl][phase2] Ejecutando %d sentencias DDL fase 2…",
                len(_PHASE2_DDL),
            )
            for stmt in _PHASE2_DDL:
                session.execute(text(stmt))
            session.commit()
            logger.info(
                "[svc3-ddl][phase2] DDL fase 2 OK. Columnas: "
                "albaran_documents_merge.review_phase2_*, "
                "albaran_lines_merge.source_phase"
            )
        except Exception:
            session.rollback()
            logger.exception(
                "[svc3-ddl][phase2] Fallo aplicando DDL fase 2; "
                "se continúa pero los UPDATE post-persist pueden fallar."
            )
