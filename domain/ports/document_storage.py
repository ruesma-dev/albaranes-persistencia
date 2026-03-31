# domain/ports/document_storage.py
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict

from domain.models.persistence_models import StoredFile


class DocumentStorage(ABC):
    @abstractmethod
    def upload(
        self,
        *,
        filename: str,
        mime_type: str,
        file_bytes: bytes,
        source_sha256: str,
        ia_input_payload: Dict[str, Any] | None = None,
        ia_output_payload: Dict[str, Any] | None = None,
    ) -> StoredFile:
        raise NotImplementedError
