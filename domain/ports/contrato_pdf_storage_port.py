# domain/ports/contrato_pdf_storage_port.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class StoredContratoPdf:
    """Resultado de subir un PDF de contrato al storage.

    ``relative_path`` es la ruta dentro del drive/carpeta raíz — se
    guarda tal cual en BBDD para mostrar al usuario dónde está.
    ``web_url`` es el enlace directo de SharePoint (si hay), para abrir
    desde el portal.
    """

    relative_path: str
    web_url: str | None


class ContratoPdfStorage(Protocol):
    """Puerto para subir el PDF del contrato a un storage (SharePoint).

    Separado del storage principal de albaranes: aquí solo se sube un
    binario con un nombre determinista. No hay artefactos IA ni sha256
    del albarán; la identidad del PDF es (codigo_contrato, gra_rep_ide).
    """

    def upload_contrato_pdf(
        self,
        *,
        filename: str,
        file_bytes: bytes,
        codigo_contrato: str,
        gra_rep_ide: int,
    ) -> StoredContratoPdf:
        """Sube el PDF y devuelve (relative_path, web_url).

        La implementación decide la ruta final. Debe ser DETERMINISTA
        para el mismo par ``(codigo_contrato, gra_rep_ide)`` — así un
        re-upload reemplaza el archivo existente en lugar de crear
        duplicados.
        """
        ...
