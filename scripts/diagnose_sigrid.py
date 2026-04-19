# scripts/diagnose_sigrid.py
"""Diagnóstico standalone para la integración con sigrid-api.

Ejecuta desde la terminal de PyCharm, en la raíz del proyecto del servicio 3:

    python scripts/diagnose_sigrid.py

Este script NO depende de que el servicio 3 arranque ni de que hayas
aplicado ningún parche. Solo necesita el `.env` del servicio y acceso
a internet para alcanzar la Function App.

Hace tres comprobaciones en orden:
  1. Lee el `.env` y muestra qué variables de Sigrid tiene configuradas.
  2. Normaliza un código de obra de prueba (por defecto '0695').
  3. Hace un POST a /api/sql/read y muestra la respuesta completa.

Uso avanzado:
    python scripts/diagnose_sigrid.py 0123        # prueba con otro código
    python scripts/diagnose_sigrid.py 695         # probará la normalización

Salida esperada SI TODO OK:
    ✅ .env cargado
    ✅ Variables Sigrid presentes
    ✅ Código normalizado: 0695
    ✅ POST a sigrid-api ... 200 OK
    ✅ Respuesta ok=true, 1 fila(s)
    → nombre_obra = 'EDIFICIO XYZ'
    → direccion   = 'Calle ... 28001 Madrid (Madrid)'
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Paso 0: localizar y cargar el .env
# ---------------------------------------------------------------------------
def load_dotenv_manually(env_path: Path) -> dict[str, str]:
    """Carga un .env sin depender de python-dotenv."""
    if not env_path.exists():
        return {}
    result: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Quitar comillas envolventes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


# Busca el .env en la raíz del proyecto (un nivel por encima de scripts/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"

print("=" * 70)
print(" DIAGNÓSTICO SIGRID-API")
print("=" * 70)
print(f"Raíz detectada : {_PROJECT_ROOT}")
print(f"Ruta del .env  : {_ENV_PATH}")
print(f"¿Existe?       : {_ENV_PATH.exists()}")
print("-" * 70)

env = load_dotenv_manually(_ENV_PATH)

# Las variables también pueden venir del entorno del sistema.
def get_cfg(name: str) -> str | None:
    return os.environ.get(name) or env.get(name)


base_url = get_cfg("SIGRID_API_BASE_URL")
function_key = get_cfg("SIGRID_API_FUNCTION_KEY")
database = get_cfg("SIGRID_API_DATABASE") or "ruesma_rep"
timeout_s = float(get_cfg("SIGRID_API_TIMEOUT_S") or "30")
enabled_flag = (get_cfg("OBRA_ENRICHMENT_ENABLED") or "true").lower()

print("Variables detectadas:")
print(f"  SIGRID_API_BASE_URL     = {base_url!r}")
if function_key:
    print(
        f"  SIGRID_API_FUNCTION_KEY = <presente, len={len(function_key)}, "
        f"últimos 4={function_key[-4:]!r}>"
    )
else:
    print(f"  SIGRID_API_FUNCTION_KEY = {function_key!r}")
print(f"  SIGRID_API_DATABASE     = {database!r}")
print(f"  SIGRID_API_TIMEOUT_S    = {timeout_s}")
print(f"  OBRA_ENRICHMENT_ENABLED = {enabled_flag!r}")
print("-" * 70)

# Validación básica
problems: list[str] = []
if not base_url:
    problems.append("❌ SIGRID_API_BASE_URL vacío o no definido")
elif not base_url.startswith(("http://", "https://")):
    problems.append(
        f"❌ SIGRID_API_BASE_URL no empieza por http(s):// (valor={base_url!r})"
    )
if not function_key:
    problems.append("❌ SIGRID_API_FUNCTION_KEY vacío o no definido")
if not database:
    problems.append("❌ SIGRID_API_DATABASE vacío")
if enabled_flag in ("false", "0", "no", "off"):
    problems.append(
        f"⚠️  OBRA_ENRICHMENT_ENABLED={enabled_flag!r} — el servicio lo "
        "interpretaría como DESHABILITADO (aquí seguimos la prueba igualmente)"
    )

if any(p.startswith("❌") for p in problems):
    print("PROBLEMAS DETECTADOS EN .env:")
    for problem in problems:
        print(f"  {problem}")
    print()
    print("Arréglalo en el .env del proyecto del servicio 3 y vuelve a ejecutar.")
    sys.exit(2)
else:
    print("✅ .env cargado y variables básicas presentes")
    for problem in problems:
        print(f"  {problem}")
    print()


# ---------------------------------------------------------------------------
# Paso 1: normalizar el código de obra
# ---------------------------------------------------------------------------
def normalize_obra_code(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    cleaned = str(raw_value).strip()
    if not cleaned or not cleaned.isdigit():
        return None
    if len(cleaned) == 3:
        return "0" + cleaned
    if len(cleaned) == 4 and cleaned.startswith("0"):
        return cleaned
    return None


raw_code = sys.argv[1] if len(sys.argv) > 1 else "0695"
normalized = normalize_obra_code(raw_code)
print(f"Código de prueba: raw={raw_code!r} -> normalizado={normalized!r}")
if normalized is None:
    print(
        "❌ El código introducido no supera la normalización "
        "(regla: 4 dígitos con 0 inicial, o 3 dígitos que se rellenan con 0)."
    )
    print("   Ejecuta con un código válido, p.e.:")
    print("       python scripts/diagnose_sigrid.py 0695")
    sys.exit(3)
print("-" * 70)


# ---------------------------------------------------------------------------
# Paso 2: POST a sigrid-api
# ---------------------------------------------------------------------------
try:
    import httpx
except ImportError:
    print("❌ Falta el paquete 'httpx'. Instálalo con:")
    print("     pip install httpx")
    sys.exit(4)

SQL_QUERY = """\
SELECT
    con.cod        AS codigo_obra,
    obr.res        AS nombre_obra,
    obr.dir1       AS direccion_linea1,
    obr.dir2       AS direccion_linea2,
    obr.dircpo     AS codigo_postal,
    mun.res        AS municipio,
    pro.res        AS provincia
FROM obr
JOIN con ON obr.ide = con.ide
LEFT JOIN auxmun mun ON obr.munide = mun.ide
LEFT JOIN auxpro pro ON obr.proide = pro.ide
WHERE con.cod = ?
"""

url = f"{base_url.rstrip('/')}/api/sql/read"
payload = {
    "database": database,
    "sql": SQL_QUERY,
    "parameters": [normalized],
    "timeout_seconds": int(timeout_s),
    "max_rows": 5,
}
headers = {
    "x-functions-key": function_key,
    "Content-Type": "application/json",
}

print(f"Enviando: POST {url}")
print(f"  database   = {database}")
print(f"  parameters = [{normalized!r}]")
print(f"  timeout    = {timeout_s}s")
print()

try:
    with httpx.Client(timeout=timeout_s) as client:
        response = client.post(url, json=payload, headers=headers)
except httpx.ConnectTimeout:
    print("❌ TIMEOUT al conectar. Posibles causas:")
    print("   • La URL es incorrecta o la Function App no está arrancada.")
    print("   • Problema de red entre tu equipo y Azure.")
    sys.exit(5)
except httpx.ConnectError as exc:
    print(f"❌ ERROR DE CONEXIÓN: {exc!r}")
    print("   La URL parece no resolverse. Revisa SIGRID_API_BASE_URL.")
    sys.exit(5)
except Exception as exc:
    print(f"❌ EXCEPCIÓN inesperada: {exc!r}")
    sys.exit(5)

print(f"HTTP status: {response.status_code}")
print(f"Body (primeros 1500 chars):")
print("-" * 70)
print(response.text[:1500])
print("-" * 70)

if response.status_code == 401:
    print("❌ 401 Unauthorized: SIGRID_API_FUNCTION_KEY es incorrecta o expiró.")
    print("   Obtén una nueva con:")
    print(
        "     az functionapp keys list --name <func-name> "
        "--resource-group <rg> --query functionKeys"
    )
    sys.exit(6)

if response.status_code == 404:
    print("❌ 404 Not Found: la ruta /api/sql/read no existe en esa Function App.")
    print("   Confirma que SIGRID_API_BASE_URL apunta a la app correcta.")
    sys.exit(6)

if response.status_code >= 500:
    print(f"❌ {response.status_code} error de servidor. Mira los logs de la Function App:")
    print("     az functionapp log tail --name <func-name> --resource-group <rg>")
    sys.exit(6)

if response.status_code != 200:
    print(f"❌ Código HTTP inesperado: {response.status_code}")
    sys.exit(6)

# Parsear JSON
try:
    body = response.json()
except json.JSONDecodeError:
    print("❌ La respuesta no es JSON válido.")
    sys.exit(6)

print()
print("Respuesta parseada:")
print(f"  ok         = {body.get('ok')}")
print(f"  database   = {body.get('database')}")
print(f"  columns    = {body.get('columns')}")
print(f"  row_count  = {body.get('row_count')}")
print(f"  truncated  = {body.get('truncated')}")
print()

if not body.get("ok"):
    print("❌ sigrid-api respondió ok=false")
    sys.exit(6)

rows = body.get("rows") or []
if not rows:
    print(f"⚠️  0 filas: la obra con con.cod='{normalized}' NO EXISTE en {database}.")
    print("   Prueba con otro código real:")
    print(f"       python scripts/diagnose_sigrid.py <otro_codigo>")
    print()
    print("   O verifica en SSMS / Azure Data Studio:")
    print(
        f"       SELECT TOP 10 con.cod FROM obr JOIN con ON obr.ide=con.ide ORDER BY con.cod"
    )
    sys.exit(0)

print(f"✅ {len(rows)} fila(s) devuelta(s)")
print()
columns = body.get("columns") or []
for i, row in enumerate(rows, start=1):
    row_map = dict(zip(columns, row))
    print(f"  Fila {i}:")
    for key, value in row_map.items():
        print(f"    {key:<20} = {value!r}")
    print()

print("=" * 70)
print(" ✅ TODO OK — la Function App responde y devuelve datos.")
print(" Si a pesar de esto el servicio 3 no actualiza el merge,")
print(" el problema está en los parches del servicio 3, no en Sigrid.")
print("=" * 70)