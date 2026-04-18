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
    gem_input_relative_path: str | None = None
    gem_input_web_url: str | None = None
    gem_output_relative_path: str | None = None
    gem_output_web_url: str | None = None
    cla_input_relative_path: str | None = None
    cla_input_web_url: str | None = None
    cla_output_relative_path: str | None = None
    cla_output_web_url: str | None = None

    @property
    def openai_input_relative_path(self) -> str | None:
        return self.ia_input_relative_path

    @property
    def openai_input_web_url(self) -> str | None:
        return self.ia_input_web_url

    @property
    def openai_output_relative_path(self) -> str | None:
        return self.ia_output_relative_path

    @property
    def openai_output_web_url(self) -> str | None:
        return self.ia_output_web_url

    @property
    def gemini_input_relative_path(self) -> str | None:
        return self.gem_input_relative_path

    @property
    def gemini_input_web_url(self) -> str | None:
        return self.gem_input_web_url

    @property
    def gemini_output_relative_path(self) -> str | None:
        return self.gem_output_relative_path

    @property
    def gemini_output_web_url(self) -> str | None:
        return self.gem_output_web_url

    @property
    def claude_input_relative_path(self) -> str | None:
        return self.cla_input_relative_path

    @property
    def claude_input_web_url(self) -> str | None:
        return self.cla_input_web_url

    @property
    def claude_output_relative_path(self) -> str | None:
        return self.cla_output_relative_path

    @property
    def claude_output_web_url(self) -> str | None:
        return self.cla_output_web_url


@dataclass(frozen=True)
class ExistingDocument:
    document_id: str
    source_sha256: str
    sharepoint_url: str | None
    stored_lines: int
