# albaranes_persistence/application/pipelines/persist_albaran_pipeline.py
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict

from application.services.albaran_normalizer import AlbaranNormalizer
from domain.models.extraction_models import ExtractionEnvelope, LineaAlbaran
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

        if envelope.gemini is not None:
            gem_expected_sha = envelope.gemini.meta.source_sha256.strip().lower()
            if gem_expected_sha and gem_expected_sha != sha256:
                raise ValueError(
                    "El sha256 del adjunto no coincide con el sha256 del bloque gemini."
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

        envelope = self._normalize(envelope)

        openai_debug = envelope.debug if isinstance(envelope.debug, dict) else {}
        gemini_debug = (
            envelope.gemini.debug
            if envelope.gemini is not None and isinstance(envelope.gemini.debug, dict)
            else {}
        )

        stored_file = self._document_storage.upload(
            filename=request.filename,
            mime_type=request.mime_type,
            file_bytes=request.file_bytes,
            source_sha256=sha256,
            ia_input_payload=self._coerce_dict(openai_debug.get("openai_request")),
            ia_output_payload=self._coerce_dict(openai_debug.get("openai_response")),
            gem_input_payload=self._coerce_dict(gemini_debug.get("gemini_request")),
            gem_output_payload=self._coerce_dict(gemini_debug.get("gemini_response")),
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

    def _normalize(self, envelope: ExtractionEnvelope) -> ExtractionEnvelope:
        openai_envelope = envelope.model_copy(
            update={"data": self._normalize_document(envelope.data)}
        )

        gemini_envelope = openai_envelope.gemini
        if gemini_envelope is not None:
            gemini_envelope = gemini_envelope.model_copy(
                update={"data": self._normalize_document(gemini_envelope.data)}
            )
            openai_envelope = openai_envelope.model_copy(
                update={"gemini": gemini_envelope}
            )

        return openai_envelope

    def _normalize_document(self, document):
        cabecera = document.cabecera
        fecha_iso = self._normalizer.normalize_date(cabecera.fecha)
        normalized_cabecera = cabecera
        if fecha_iso and cabecera.fecha != fecha_iso:
            normalized_cabecera = cabecera.model_copy(update={"fecha": fecha_iso})

        normalized_lines = [
            self._normalize_line_confidence(line) for line in document.lineas
        ]
        return document.model_copy(
            update={
                "cabecera": normalized_cabecera,
                "lineas": normalized_lines,
            }
        )

    @staticmethod
    def _normalize_line_confidence(line: LineaAlbaran) -> LineaAlbaran:
        confidence = line.confianza_pct
        if confidence is None:
            return line

        normalized = float(confidence)
        if 0.0 <= normalized <= 1.0:
            normalized *= 100.0
        normalized = max(0.0, min(100.0, normalized))
        if normalized == confidence:
            return line
        return line.model_copy(update={"confianza_pct": normalized})

    @staticmethod
    def _coerce_dict(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}
