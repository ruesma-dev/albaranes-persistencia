# domain/models/persistence_models.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StoredFile:
    drive_id: str
    item_id: str
    relative_path: str
    web_url: str | None
    share_url: str | None
    ia_input_relative_path: str | None = None
    ia_input_web_url: str | None = None
    ia_output_relative_path: str | None = None
    ia_output_web_url: str | None = None


@dataclass(frozen=True)
class ExistingDocument:
    document_id: str
    source_sha256: str
    sharepoint_url: str | None
    stored_lines: int
