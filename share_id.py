from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import httpx


@dataclass(frozen=True)
class GraphCreds:
    tenant_id: str
    client_id: str
    client_secret: str


def load_env_file(env_path: str | Path) -> dict[str, str]:
    env_path = Path(env_path)

    if not env_path.exists():
        raise FileNotFoundError(f"No existe el .env: {env_path}")

    values: dict[str, str] = {}

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]

        values[key] = value

    return values


def try_json(value: str) -> Optional[dict]:
    try:
        return json.loads(value)
    except Exception:
        return None


def try_b64_json(value: str) -> Optional[dict]:
    try:
        raw = base64.b64decode(value).decode("utf-8")
        return json.loads(raw)
    except Exception:
        return None


def parse_graph_key(graph_key: str) -> GraphCreds:
    data = try_json(graph_key) or try_b64_json(graph_key)

    if not isinstance(data, dict):
        raise ValueError(
            "GRAPH_KEY no es JSON válido ni base64 JSON válido."
        )

    required = ("tenant_id", "client_id", "client_secret")
    missing = [key for key in required if key not in data]

    if missing:
        raise ValueError(
            f"GRAPH_KEY incompleto. Faltan claves: {', '.join(missing)}"
        )

    return GraphCreds(
        tenant_id=str(data["tenant_id"]).strip(),
        client_id=str(data["client_id"]).strip(),
        client_secret=str(data["client_secret"]).strip(),
    )


def get_app_token(graph_key: str, timeout_s: int = 30) -> str:
    creds = parse_graph_key(graph_key)

    token_url = (
        f"https://login.microsoftonline.com/"
        f"{creds.tenant_id}/oauth2/v2.0/token"
    )
    data = {
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default",
    }

    with httpx.Client(timeout=timeout_s) as client:
        response = client.post(token_url, data=data)
        response.raise_for_status()
        payload = response.json()

    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Azure AD no devolvió access_token.")

    return str(token)


def graph_get(url: str, token: str, timeout_s: int = 30) -> dict:
    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(timeout=timeout_s) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()


def derive_sharepoint_context(
    env_values: dict[str, str],
) -> tuple[str, str, str]:
    hostname = env_values.get("SHAREPOINT_HOSTNAME", "").strip()
    site_path = env_values.get("SHAREPOINT_SITE_PATH", "").strip()
    drive_name = env_values.get("SHAREPOINT_DRIVE_NAME", "").strip()

    if hostname and site_path:
        return hostname, site_path, drive_name or "Documentos compartidos"

    folder_url = env_values.get("SHAREPOINT_FOLDER_URL", "").strip()
    if not folder_url:
        raise ValueError(
            "Faltan SHAREPOINT_HOSTNAME/SHAREPOINT_SITE_PATH y "
            "tampoco existe SHAREPOINT_FOLDER_URL en el .env."
        )

    parsed = urlparse(folder_url)
    hostname = parsed.netloc.strip()
    raw_path = unquote(parsed.path)

    marker = "/r/"
    if marker not in raw_path:
        raise ValueError(
            "No se puede extraer site y drive desde SHAREPOINT_FOLDER_URL. "
            f"URL recibida: {folder_url}"
        )

    tail = raw_path.split(marker, 1)[1].strip("/")
    parts = tail.split("/")

    if len(parts) < 3:
        raise ValueError(
            "SHAREPOINT_FOLDER_URL no contiene suficientes segmentos para "
            "inferir site y biblioteca."
        )

    if parts[0] not in {"sites", "teams", "personal"}:
        raise ValueError(
            "SHAREPOINT_FOLDER_URL no tiene un path esperado. "
            f"Primer segmento encontrado: {parts[0]!r}"
        )

    site_path = "/" + "/".join(parts[:2])
    drive_name = drive_name or parts[2]

    return hostname, site_path, drive_name


def main() -> None:
    project_root = Path(__file__).resolve().parent
    env_path = project_root / ".env"
    env_values = load_env_file(env_path)

    graph_key = env_values.get("GRAPH_KEY", "").strip()
    if not graph_key:
        raise ValueError("No existe GRAPH_KEY en el .env.")

    hostname, site_path, target_drive_name = derive_sharepoint_context(
        env_values
    )

    print(f".env cargado desde: {env_path}")
    print(f"Hostname: {hostname}")
    print(f"Site path: {site_path}")
    print(f"Drive buscado: {target_drive_name}")

    token = get_app_token(graph_key=graph_key)

    site_url = (
        f"https://graph.microsoft.com/v1.0/"
        f"sites/{hostname}:{site_path}?$select=id,webUrl,displayName"
    )
    site = graph_get(site_url, token=token)
    site_id = str(site["id"])

    drives_url = (
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives"
        f"?$select=id,name,webUrl,driveType"
    )
    drives = graph_get(drives_url, token=token)

    print("\nSITE")
    print(json.dumps(site, indent=2, ensure_ascii=False))

    print("\nDRIVES ENCONTRADOS")
    for drive in drives.get("value", []):
        print("-" * 60)
        print(json.dumps(drive, indent=2, ensure_ascii=False))

    target_drive = None
    for drive in drives.get("value", []):
        drive_name = str(drive.get("name") or "").strip().lower()
        if drive_name == target_drive_name.lower():
            target_drive = drive
            break

    if target_drive:
        print("\nDRIVE_ID_ENCONTRADO")
        print(target_drive["id"])
    else:
        print(
            "\nNo se encontró un drive con ese nombre. "
            "Revisa el listado anterior."
        )


if __name__ == "__main__":
    main()