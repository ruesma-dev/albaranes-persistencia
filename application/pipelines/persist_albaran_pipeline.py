# application/pipelines/persist_albaran_pipeline.py
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict

from application.services.albaran_normalizer import AlbaranNormalizer
from domain.models.extraction_models import ExtractionEnvelope
from domain.ports.albaran_repository import AlbaranRepository
from domain.ports.document_storage import DocumentStorage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PersistAlbaranRequest:
    filename: str
    mime_type: str
    file_bytes: bytes
    extraction_envelope: Dict[str, Any]
    context: Dict[str, Any]


@dataclass(frozen=True)
class PersistAlbaranResult:
    ok: bool
    document_id: str
    sharepoint_url: str | None
    duplicate: bool
    stored_lines: int


class PersistAlbaranPipeline:
    def __init__(
        self,
        *,
        repository: AlbaranRepository,
        document_storage: DocumentStorage,
        normalizer: AlbaranNormalizer,
    ) -> None:
        self._repository = repository
        self._document_storage = document_storage
        self._normalizer = normalizer

    def run(self, request: PersistAlbaranRequest) -> PersistAlbaranResult:
        if not request.file_bytes:
            raise ValueError("Archivo vacío.")

        envelope = ExtractionEnvelope.model_validate(request.extraction_envelope)
        sha256 = hashlib.sha256(request.file_bytes).hexdigest()
        expected_sha = envelope.meta.source_sha256.strip().lower()
        if expected_sha and expected_sha != sha256:
            raise ValueError(
                "El sha256 del adjunto no coincide con el sha256 del envelope."
            )

        existing = self._repository.get_by_sha256(sha256)
        if existing is not None:
            logger.info(
                "Documento duplicado detectado. sha256=%s document_id=%s",
                sha256,
                existing.document_id,
            )
            return PersistAlbaranResult(
                ok=True,
                document_id=existing.document_id,
                sharepoint_url=existing.sharepoint_url,
                duplicate=True,
                stored_lines=existing.stored_lines,
            )

        cabecera = envelope.data.cabecera
        fecha_iso = self._normalizer.normalize_date(cabecera.fecha)
        if fecha_iso and cabecera.fecha != fecha_iso:
            envelope = envelope.model_copy(
                update={
                    "data": envelope.data.model_copy(
                        update={
                            "cabecera": cabecera.model_copy(
                                update={"fecha": fecha_iso}
                            )
                        }
                    )
                }
            )

        debug = envelope.debug or {}
        if not isinstance(debug, dict):
            debug = {}

        ia_input_payload = debug.get("openai_request")
        if not isinstance(ia_input_payload, dict):
            ia_input_payload = None

        ia_output_payload = debug.get("openai_response")
        if not isinstance(ia_output_payload, dict):
            ia_output_payload = None

        stored_file = self._document_storage.upload(
            filename=request.filename,
            mime_type=request.mime_type,
            file_bytes=request.file_bytes,
            source_sha256=sha256,
            ia_input_payload=ia_input_payload,
            ia_output_payload=ia_output_payload,
        )
        saved = self._repository.save(
            envelope=envelope,
            context=request.context,
            stored_file=stored_file,
        )
        return PersistAlbaranResult(
            ok=True,
            document_id=saved.document_id,
            sharepoint_url=saved.sharepoint_url,
            duplicate=False,
            stored_lines=saved.stored_lines,
        )
