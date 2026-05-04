# application/services/phase2_persistence_service.py
"""Persiste metadatos de revisión de fase 2 sobre las tablas merge.

Diseño:
  - Se invoca DESPUÉS del save() normal del PersistAlbaranPipeline.
  - Lee los bloques `phase_2` y `review_phase2_metadata` del raw
    envelope que llega a sv3 (los inyecta sv7 al hacer el merge fase 1
    + fase 2).
  - Hace dos UPDATE simples:
       1. albaran_documents_merge: review_phase2_status,
          review_phase2_summary, review_phase2_changes_count,
          review_phase2_payload_json.
       2. albaran_lines_merge: source_phase = 'phase_2' para las
          líneas cuyo `data.lines[N].source_phase == 'phase_2'`.

Best-effort: si algo falla, log + continuar. No queremos que un
error en metadatos de fase 2 rompa el persist principal.

NOTA IMPORTANTE: este servicio NO toca la lógica multi-proveedor del
repo (openai/gemini/claude). Trabaja exclusivamente sobre las
columnas nuevas añadidas por phase2_ddl.py.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from sqlalchemy import text

from infrastructure.database.session_factory import SessionFactory

logger = logging.getLogger(__name__)


class Phase2PersistenceService:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    def persist_metadata(
        self,
        *,
        document_id: str,
        raw_envelope: Dict[str, Any],
    ) -> None:
        """Punto de entrada principal: persiste todo lo de fase 2."""
        review_metadata = raw_envelope.get("review_phase2_metadata")
        phase_1_data = raw_envelope.get("data") or {}

        if not isinstance(review_metadata, dict):
            logger.info(
                "[phase2-persist] document_id=%s sin review_phase2_metadata "
                "en el envelope (fase 2 no se ejecutó); skip",
                document_id,
            )
            return

        try:
            self._update_document_metadata(
                document_id=document_id,
                review_metadata=review_metadata,
            )
        except Exception:
            logger.exception(
                "[phase2-persist] error UPDATE albaran_documents_merge "
                "doc=%s; se continúa con líneas",
                document_id,
            )

        try:
            self._update_lines_source_phase(
                document_id=document_id,
                phase_1_data=phase_1_data,
            )
        except Exception:
            logger.exception(
                "[phase2-persist] error UPDATE albaran_lines_merge "
                "doc=%s",
                document_id,
            )

    # ---------------------------------------------------------- #
    # albaran_documents_merge.review_phase2_*
    # ---------------------------------------------------------- #
    def _update_document_metadata(
        self,
        *,
        document_id: str,
        review_metadata: Dict[str, Any],
    ) -> None:
        status = review_metadata.get("review_phase2_status")
        summary = review_metadata.get("review_phase2_summary")
        changes_count = review_metadata.get("review_phase2_changes_count")
        payload = review_metadata.get("review_phase2_payload_json")
        payload_str = (
            json.dumps(payload, ensure_ascii=False)
            if isinstance(payload, (dict, list))
            else (payload if payload is not None else None)
        )

        with self._sf.create_session() as session:
            session.execute(
                text(
                    "UPDATE albaran_documents_merge "
                    "SET review_phase2_status = :status, "
                    "    review_phase2_summary = :summary, "
                    "    review_phase2_changes_count = :changes_count, "
                    "    review_phase2_payload_json = :payload "
                    "WHERE id = :document_id"
                ),
                {
                    "status": status,
                    "summary": summary,
                    "changes_count": changes_count,
                    "payload": payload_str,
                    "document_id": document_id,
                },
            )
            session.commit()
        logger.info(
            "[phase2-persist] document_id=%s review_phase2_status=%s "
            "changes_count=%s",
            document_id, status, changes_count,
        )

    # ---------------------------------------------------------- #
    # albaran_lines_merge.source_phase = 'phase_2' (las modificadas)
    # ---------------------------------------------------------- #
    def _update_lines_source_phase(
        self,
        *,
        document_id: str,
        phase_1_data: Dict[str, Any],
    ) -> None:
        lines = phase_1_data.get("lines") if isinstance(phase_1_data, dict) else None
        if not isinstance(lines, list):
            return

        # Recopilamos los `id` de línea (si vienen en el envelope) o
        # los `numero_linea` para localizarlas.
        # El envelope puede traer line.id (UUID) o line.numero_linea
        # (entero correlativo asignado por el extractor). Probamos
        # ambos para máxima robustez.
        phase2_line_ids: List[str] = []
        phase2_line_indices: List[int] = []

        for idx, line in enumerate(lines):
            if not isinstance(line, dict):
                continue
            if line.get("source_phase") != "phase_2":
                continue
            line_id = line.get("id")
            if isinstance(line_id, str) and line_id:
                phase2_line_ids.append(line_id)
            else:
                phase2_line_indices.append(idx)

        if not phase2_line_ids and not phase2_line_indices:
            logger.info(
                "[phase2-persist] document_id=%s sin líneas marcadas "
                "como phase_2 (skip UPDATE)",
                document_id,
            )
            return

        with self._sf.create_session() as session:
            # Estrategia 1: por id de línea (lo más fiable).
            if phase2_line_ids:
                session.execute(
                    text(
                        "UPDATE albaran_lines_merge "
                        "SET source_phase = 'phase_2' "
                        "WHERE document_id = :document_id "
                        "AND id::text = ANY(:ids)"
                    ),
                    {
                        "document_id": document_id,
                        "ids": phase2_line_ids,
                    },
                )

            # Estrategia 2: por orden (numero_linea o ROW_NUMBER por
            # orden de inserción). Como no podemos asumir un campo
            # numero_linea en todos los esquemas, usamos un trick con
            # ctid descartable y nos basamos en orden por id ascendente.
            if phase2_line_indices:
                session.execute(
                    text(
                        "WITH ordered AS ("
                        "  SELECT id, "
                        "         ROW_NUMBER() OVER (ORDER BY id ASC) - 1 AS rn "
                        "  FROM albaran_lines_merge "
                        "  WHERE document_id = :document_id"
                        ") "
                        "UPDATE albaran_lines_merge AS lm "
                        "SET source_phase = 'phase_2' "
                        "FROM ordered "
                        "WHERE lm.id = ordered.id "
                        "  AND ordered.rn = ANY(:idxs)"
                    ),
                    {
                        "document_id": document_id,
                        "idxs": phase2_line_indices,
                    },
                )
            session.commit()

        logger.info(
            "[phase2-persist] document_id=%s líneas marcadas phase_2: "
            "by_id=%d by_index=%d",
            document_id, len(phase2_line_ids), len(phase2_line_indices),
        )
