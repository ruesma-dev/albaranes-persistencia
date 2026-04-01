# infrastructure/storage/sharepoint_document_storage.py
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import quote

import httpx

from domain.models.persistence_models import StoredFile
from domain.ports.document_storage import DocumentStorage
from infrastructure.graph.token_provider import GraphTokenProvider

logger = logging.getLogger(__name__)

SharePointMode = Literal["drive_id", "folder_url", "site_path"]


@dataclass(frozen=True)
class ResolvedFolder:
    drive_id: str
    item_id: str
    web_url: str | None
    folder_name: str | None


class SharePointDocumentStorage(DocumentStorage):
    def __init__(
        self,
        *,
        graph_key: str,
        timeout_s: int,
        mode: SharePointMode,
        hostname: str | None,
        site_path: str | None,
        drive_name: str,
        drive_id: str | None,
        folder_root: str,
        folder_url: str | None,
        link_type: str,
        link_scope: str,
        create_link: bool,
    ) -> None:
        self._token_provider = GraphTokenProvider(graph_key, timeout_s)
        self._client = httpx.Client(timeout=timeout_s)
        self._base = "https://graph.microsoft.com/v1.0"
        self._mode: SharePointMode = mode
        self._hostname = (hostname or "").strip() or None
        self._site_path = (site_path or "").strip() or None
        self._drive_name = drive_name.strip()
        self._drive_id = (drive_id or "").strip() or None
        self._folder_root = (folder_root or "").replace("\\", "/").strip().strip("/")
        self._folder_url = (folder_url or "").strip() or None
        self._link_type = link_type.strip() or "view"
        self._link_scope = link_scope.strip() or "organization"
        self._create_link = bool(create_link)
        self._site_id_cache: str | None = None
        self._resolved_folder_cache: ResolvedFolder | None = None

        self._validate_mode_config()

    def _validate_mode_config(self) -> None:
        if self._mode == "drive_id" and not self._drive_id:
            raise RuntimeError(
                "SHAREPOINT_MODE=drive_id exige SHAREPOINT_DRIVE_ID."
            )
        if self._mode == "folder_url" and not self._folder_url:
            raise RuntimeError(
                "SHAREPOINT_MODE=folder_url exige SHAREPOINT_FOLDER_URL."
            )
        if self._mode == "site_path":
            if not self._hostname or not self._site_path:
                raise RuntimeError(
                    "SHAREPOINT_MODE=site_path exige SHAREPOINT_HOSTNAME "
                    "y SHAREPOINT_SITE_PATH."
                )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_provider.get_token()}"}

    @staticmethod
    def _safe_filename(filename: str) -> str:
        cleaned = "".join(
            char if char not in '<>:"\\|?*' else "_"
            for char in (filename or "document.bin")
        )
        return cleaned.strip().strip(".") or "document.bin"

    @staticmethod
    def _encode_sharing_url(url: str) -> str:
        raw = base64.b64encode(url.encode("utf-8")).decode("ascii")
        token = raw.rstrip("=").replace("/", "_").replace("+", "-")
        return f"u!{token}"

    def _base_folder_path(self, folder_label: str | None) -> str:
        return (self._folder_root or folder_label or "albaranes").strip().strip("/")

    def _resolve_site_id(self) -> str:
        if self._site_id_cache:
            return self._site_id_cache
        if not self._hostname or not self._site_path:
            raise RuntimeError(
                "Faltan SHAREPOINT_HOSTNAME/SHAREPOINT_SITE_PATH para resolver "
                "el sitio por path."
            )

        relative_path = self._site_path.lstrip("/")
        url = f"{self._base}/sites/{self._hostname}:/{relative_path}"
        response = self._client.get(url, headers=self._headers())
        if response.status_code >= 300:
            raise RuntimeError(
                f"Graph get site by path {response.status_code}: "
                f"{response.text[:500]}"
            )

        site_id = str((response.json() or {}).get("id") or "").strip()
        if not site_id:
            raise RuntimeError("Graph no devolvió site.id para SharePoint.")
        self._site_id_cache = site_id
        return site_id

    def _resolve_drive_id(self, site_id: str) -> str:
        if self._drive_id:
            return self._drive_id

        url = f"{self._base}/sites/{site_id}/drives"
        response = self._client.get(url, headers=self._headers())
        if response.status_code >= 300:
            raise RuntimeError(
                f"Graph list drives {response.status_code}: "
                f"{response.text[:500]}"
            )

        items = (response.json() or {}).get("value") or []
        for item in items:
            if str(item.get("name") or "").strip() == self._drive_name:
                self._drive_id = str(item["id"])
                return self._drive_id

        available = ", ".join(
            sorted(
                str(item.get("name") or "").strip()
                for item in items
                if str(item.get("name") or "").strip()
            )
        )
        raise RuntimeError(
            f"No se encontró la biblioteca SharePoint '{self._drive_name}'. "
            f"Disponibles: {available or '(none)'}"
        )

    def _resolve_folder_from_share_url(self) -> ResolvedFolder:
        if self._resolved_folder_cache:
            return self._resolved_folder_cache
        if not self._folder_url:
            raise RuntimeError("No se ha configurado SHAREPOINT_FOLDER_URL.")

        token = self._encode_sharing_url(self._folder_url)
        url = f"{self._base}/shares/{token}/driveItem"
        response = self._client.get(url, headers=self._headers())
        if response.status_code >= 300:
            raise RuntimeError(
                f"Graph get share driveItem {response.status_code}: "
                f"{response.text[:500]}"
            )

        payload = response.json() or {}
        if not isinstance(payload.get("folder"), dict):
            raise RuntimeError(
                "La URL configurada en SHAREPOINT_FOLDER_URL no apunta a una carpeta."
            )

        item_id = str(payload.get("id") or "").strip()
        parent_reference = payload.get("parentReference") or {}
        drive_id = str(parent_reference.get("driveId") or "").strip()
        web_url = str(payload.get("webUrl") or "").strip() or None
        folder_name = str(payload.get("name") or "").strip() or None

        if not item_id or not drive_id:
            raise RuntimeError(
                "Graph no devolvió id/driveId al resolver SHAREPOINT_FOLDER_URL."
            )

        resolved = ResolvedFolder(
            drive_id=drive_id,
            item_id=item_id,
            web_url=web_url,
            folder_name=folder_name,
        )
        self._resolved_folder_cache = resolved
        return resolved

    def _build_storage_parts(
        self,
        *,
        filename: str,
        source_sha256: str,
        folder_label: str | None,
    ) -> tuple[list[str], str, str]:
        now = datetime.now(timezone.utc)
        safe_name = self._safe_filename(filename)
        prefix = source_sha256[:8]
        final_name = f"{prefix}_{safe_name}"
        relative_parts = [
            self._base_folder_path(folder_label),
            now.strftime("%Y"),
            now.strftime("%m"),
            final_name,
        ]
        subfolders = [now.strftime("%Y"), now.strftime("%m")]
        return subfolders, final_name, str(PurePosixPath(*relative_parts))

    def _children_endpoint(self, *, drive_id: str, parent_item_id: str) -> str:
        if parent_item_id == "root":
            return f"{self._base}/drives/{drive_id}/root/children"
        return f"{self._base}/drives/{drive_id}/items/{parent_item_id}/children"

    def _list_children(
        self,
        *,
        drive_id: str,
        parent_item_id: str,
    ) -> list[dict]:
        url = self._children_endpoint(
            drive_id=drive_id,
            parent_item_id=parent_item_id,
        )
        params = {"$select": "id,name,folder"}
        items: list[dict] = []

        while url:
            response = self._client.get(url, headers=self._headers(), params=params)
            params = None
            if response.status_code >= 300:
                raise RuntimeError(
                    f"Graph list children {response.status_code}: "
                    f"{response.text[:500]}"
                )
            payload = response.json() or {}
            values = payload.get("value") or []
            if isinstance(values, list):
                items.extend(item for item in values if isinstance(item, dict))
            url = str(payload.get("@odata.nextLink") or "").strip() or None

        return items

    def _find_child_folder(
        self,
        *,
        drive_id: str,
        parent_item_id: str,
        folder_name: str,
    ) -> dict | None:
        for item in self._list_children(
            drive_id=drive_id,
            parent_item_id=parent_item_id,
        ):
            if str(item.get("name") or "").strip() != folder_name:
                continue
            if not isinstance(item.get("folder"), dict):
                continue
            return item
        return None

    def _create_folder(
        self,
        *,
        drive_id: str,
        parent_item_id: str,
        folder_name: str,
    ) -> str:
        url = self._children_endpoint(
            drive_id=drive_id,
            parent_item_id=parent_item_id,
        )
        payload = {
            "name": folder_name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        }
        response = self._client.post(
            url,
            headers=self._headers(),
            json=payload,
        )
        if response.status_code == 409:
            existing = self._find_child_folder(
                drive_id=drive_id,
                parent_item_id=parent_item_id,
                folder_name=folder_name,
            )
            if existing:
                existing_id = str(existing.get("id") or "").strip()
                if existing_id:
                    return existing_id
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(
                f"Graph create folder {response.status_code}: {response.text[:500]}"
            )
        folder_id = str((response.json() or {}).get("id") or "").strip()
        if not folder_id:
            raise RuntimeError("Graph no devolvió id al crear carpeta.")
        return folder_id

    def _ensure_child_folder(
        self,
        *,
        drive_id: str,
        parent_item_id: str,
        folder_name: str,
    ) -> str:
        existing = self._find_child_folder(
            drive_id=drive_id,
            parent_item_id=parent_item_id,
            folder_name=folder_name,
        )
        if existing:
            folder_id = str(existing.get("id") or "").strip()
            if folder_id:
                return folder_id
        return self._create_folder(
            drive_id=drive_id,
            parent_item_id=parent_item_id,
            folder_name=folder_name,
        )

    def _ensure_folder_path_from_root(
        self,
        *,
        drive_id: str,
        folder_path: str,
    ) -> str:
        parts = [
            part
            for part in PurePosixPath(folder_path).parts
            if part and part != "/"
        ]
        parent_id = "root"
        for folder_name in parts:
            parent_id = self._ensure_child_folder(
                drive_id=drive_id,
                parent_item_id=parent_id,
                folder_name=folder_name,
            )
        return parent_id

    def _upload_file_by_parent(
        self,
        *,
        drive_id: str,
        parent_item_id: str,
        filename: str,
        mime_type: str,
        file_bytes: bytes,
    ) -> dict:
        encoded_name = quote(filename, safe="")
        if parent_item_id == "root":
            url = f"{self._base}/drives/{drive_id}/root:/{encoded_name}:/content"
        else:
            url = (
                f"{self._base}/drives/{drive_id}/items/{parent_item_id}:/"
                f"{encoded_name}:/content"
            )

        headers = self._headers()
        headers["Content-Type"] = mime_type or "application/octet-stream"
        response = self._client.put(url, headers=headers, content=file_bytes)
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(
                f"Graph upload file {response.status_code}: {response.text[:500]}"
            )
        return response.json() or {}

    def _upload_file_by_relative_path(
        self,
        *,
        drive_id: str,
        relative_path: str,
        mime_type: str,
        file_bytes: bytes,
    ) -> dict:
        encoded_path = quote(relative_path, safe="/")
        url = f"{self._base}/drives/{drive_id}/root:/{encoded_path}:/content"
        headers = self._headers()
        headers["Content-Type"] = mime_type or "application/octet-stream"
        response = self._client.put(url, headers=headers, content=file_bytes)
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(
                f"Graph upload file {response.status_code}: {response.text[:500]}"
            )
        return response.json() or {}

    def _create_share_link(
        self,
        *,
        drive_id: str,
        item_id: str,
    ) -> str | None:
        url = f"{self._base}/drives/{drive_id}/items/{item_id}/createLink"
        payload = {
            "type": self._link_type,
            "scope": self._link_scope,
        }
        response = self._client.post(
            url,
            headers=self._headers(),
            json=payload,
        )
        if response.status_code < 200 or response.status_code >= 300:
            logger.warning(
                "No se pudo crear createLink en SharePoint. status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            return None

        data = response.json() or {}
        link = data.get("link") or {}
        return str(link.get("webUrl") or "").strip() or None

    @staticmethod
    def _artifact_name(final_name: str, artifact_type: str) -> str:
        path = PurePosixPath(final_name)
        if artifact_type.startswith("gemini_"):
            return f"{path.stem}.{artifact_type}_GEM.json"
        return f"{path.stem}.{artifact_type}.json"

    @staticmethod
    def _json_bytes(payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    def _upload_artifact_by_parent(
        self,
        *,
        drive_id: str,
        parent_item_id: str,
        artifact_name: str,
        payload: dict[str, Any],
    ) -> tuple[str, str | None]:
        uploaded = self._upload_file_by_parent(
            drive_id=drive_id,
            parent_item_id=parent_item_id,
            filename=artifact_name,
            mime_type="application/json",
            file_bytes=self._json_bytes(payload),
        )
        return artifact_name, str(uploaded.get("webUrl") or "").strip() or None

    def _upload_artifact_by_relative_path(
        self,
        *,
        drive_id: str,
        relative_dir: str,
        artifact_name: str,
        payload: dict[str, Any],
    ) -> tuple[str, str | None]:
        artifact_relative_path = str(PurePosixPath(relative_dir) / artifact_name)
        uploaded = self._upload_file_by_relative_path(
            drive_id=drive_id,
            relative_path=artifact_relative_path,
            mime_type="application/json",
            file_bytes=self._json_bytes(payload),
        )
        return artifact_relative_path, str(uploaded.get("webUrl") or "").strip() or None

    def upload(
        self,
        *,
        filename: str,
        mime_type: str,
        file_bytes: bytes,
        source_sha256: str,
        ia_input_payload: dict[str, Any] | None = None,
        ia_output_payload: dict[str, Any] | None = None,
        gem_input_payload: dict[str, Any] | None = None,
        gem_output_payload: dict[str, Any] | None = None,
    ) -> StoredFile:
        ia_input_relative_path: str | None = None
        ia_input_web_url: str | None = None
        ia_output_relative_path: str | None = None
        ia_output_web_url: str | None = None
        gem_input_relative_path: str | None = None
        gem_input_web_url: str | None = None
        gem_output_relative_path: str | None = None
        gem_output_web_url: str | None = None

        if self._mode == "drive_id":
            assert self._drive_id is not None
            drive_id = self._drive_id
            base_parent_id = self._ensure_folder_path_from_root(
                drive_id=drive_id,
                folder_path=self._base_folder_path(None),
            )
            subfolders, final_name, relative_path = self._build_storage_parts(
                filename=filename,
                source_sha256=source_sha256,
                folder_label=None,
            )

            parent_id = base_parent_id
            for folder_name in subfolders:
                parent_id = self._ensure_child_folder(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    folder_name=folder_name,
                )

            uploaded = self._upload_file_by_parent(
                drive_id=drive_id,
                parent_item_id=parent_id,
                filename=final_name,
                mime_type=mime_type,
                file_bytes=file_bytes,
            )

            if ia_input_payload:
                ia_input_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "openai_request")
                )
                _, ia_input_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "openai_request"),
                    payload=ia_input_payload,
                )
            if ia_output_payload:
                ia_output_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "openai_response")
                )
                _, ia_output_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "openai_response"),
                    payload=ia_output_payload,
                )
            if gem_input_payload:
                gem_input_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "gemini_request")
                )
                _, gem_input_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "gemini_request"),
                    payload=gem_input_payload,
                )
            if gem_output_payload:
                gem_output_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "gemini_response")
                )
                _, gem_output_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "gemini_response"),
                    payload=gem_output_payload,
                )

        elif self._mode == "folder_url":
            base_folder = self._resolve_folder_from_share_url()
            subfolders, final_name, relative_path = self._build_storage_parts(
                filename=filename,
                source_sha256=source_sha256,
                folder_label=base_folder.folder_name,
            )

            parent_id = base_folder.item_id
            for folder_name in subfolders:
                parent_id = self._ensure_child_folder(
                    drive_id=base_folder.drive_id,
                    parent_item_id=parent_id,
                    folder_name=folder_name,
                )

            uploaded = self._upload_file_by_parent(
                drive_id=base_folder.drive_id,
                parent_item_id=parent_id,
                filename=final_name,
                mime_type=mime_type,
                file_bytes=file_bytes,
            )
            drive_id = base_folder.drive_id

            if ia_input_payload:
                ia_input_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "openai_request")
                )
                _, ia_input_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "openai_request"),
                    payload=ia_input_payload,
                )
            if ia_output_payload:
                ia_output_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "openai_response")
                )
                _, ia_output_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "openai_response"),
                    payload=ia_output_payload,
                )
            if gem_input_payload:
                gem_input_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "gemini_request")
                )
                _, gem_input_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "gemini_request"),
                    payload=gem_input_payload,
                )
            if gem_output_payload:
                gem_output_relative_path = str(
                    PurePosixPath(relative_path).parent
                    / self._artifact_name(final_name, "gemini_response")
                )
                _, gem_output_web_url = self._upload_artifact_by_parent(
                    drive_id=drive_id,
                    parent_item_id=parent_id,
                    artifact_name=self._artifact_name(final_name, "gemini_response"),
                    payload=gem_output_payload,
                )

        else:
            site_id = self._resolve_site_id()
            drive_id = self._resolve_drive_id(site_id)
            _, final_name, relative_path = self._build_storage_parts(
                filename=filename,
                source_sha256=source_sha256,
                folder_label=None,
            )
            uploaded = self._upload_file_by_relative_path(
                drive_id=drive_id,
                relative_path=relative_path,
                mime_type=mime_type,
                file_bytes=file_bytes,
            )
            relative_dir = str(PurePosixPath(relative_path).parent)
            if ia_input_payload:
                ia_input_relative_path, ia_input_web_url = (
                    self._upload_artifact_by_relative_path(
                        drive_id=drive_id,
                        relative_dir=relative_dir,
                        artifact_name=self._artifact_name(final_name, "openai_request"),
                        payload=ia_input_payload,
                    )
                )
            if ia_output_payload:
                ia_output_relative_path, ia_output_web_url = (
                    self._upload_artifact_by_relative_path(
                        drive_id=drive_id,
                        relative_dir=relative_dir,
                        artifact_name=self._artifact_name(final_name, "openai_response"),
                        payload=ia_output_payload,
                    )
                )
            if gem_input_payload:
                gem_input_relative_path, gem_input_web_url = (
                    self._upload_artifact_by_relative_path(
                        drive_id=drive_id,
                        relative_dir=relative_dir,
                        artifact_name=self._artifact_name(final_name, "gemini_request"),
                        payload=gem_input_payload,
                    )
                )
            if gem_output_payload:
                gem_output_relative_path, gem_output_web_url = (
                    self._upload_artifact_by_relative_path(
                        drive_id=drive_id,
                        relative_dir=relative_dir,
                        artifact_name=self._artifact_name(final_name, "gemini_response"),
                        payload=gem_output_payload,
                    )
                )

        item_id = str(uploaded.get("id") or "").strip()
        if not item_id:
            raise RuntimeError("SharePoint no devolvió driveItem.id.")

        web_url = str(uploaded.get("webUrl") or "").strip() or None
        share_url = None
        if self._create_link:
            share_url = self._create_share_link(
                drive_id=drive_id,
                item_id=item_id,
            )

        return StoredFile(
            drive_id=drive_id,
            item_id=item_id,
            relative_path=relative_path,
            web_url=web_url,
            share_url=share_url or web_url,
            ia_input_relative_path=ia_input_relative_path,
            ia_input_web_url=ia_input_web_url,
            ia_output_relative_path=ia_output_relative_path,
            ia_output_web_url=ia_output_web_url,
            gem_input_relative_path=gem_input_relative_path,
            gem_input_web_url=gem_input_web_url,
            gem_output_relative_path=gem_output_relative_path,
            gem_output_web_url=gem_output_web_url,
        )
