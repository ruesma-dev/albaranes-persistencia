# extraer_lineas_proveedor_anio.py
"""Script standalone: TODAS las lineas de FACTURA DE COMPRA de un proveedor
(por CIF/NIF) en un año concreto, a CSV.

Saca de Sigrid (via sigrid-api, POST /api/sql/read) todas las lineas de
dbo.dcfpro (Productos en Facturas de compra) del proveedor indicado, filtrando
por el CIF/NIF de la cabecera dbo.dcf (entcif, normalizado: sin guiones/puntos/
espacios y en mayusculas) y por año (dcf.ano, con respaldo fecdoc/10000).
Sin filtro de descripcion: salen todas las lineas.

Devuelve por linea: nº factura (con.cod del dcf), nº factura del proveedor
(dcf.entref), año, fecha doc, obra (cod + nombre), codigo de producto (con.cod
del pro), descripcion, unidades (can), precio/ud (pre) e importe (tot).

Campos VERIFICADOS contra tablas_sigrid.pdf (v.20240618):
    dcf  "Factura de compra" (extiende con por ide): entcif, obride, ano, fecdoc, entref
    dcfpro "Productos en Facturas de compra": docide(->dcf), proide(->pro), can, pre, tot, res
    con (padre de dcf, obr y pro): cod, res

Lanzar desde la consola de un servicio (p.ej. sv3), SIN tocar nada:

    python extraer_lineas_proveedor_anio.py
    python extraer_lineas_proveedor_anio.py --nif A28733558 --anio 2026
    python extraer_lineas_proveedor_anio.py --anio 2025 --out salida_2025.csv

Config del .env del directorio actual (o variables de entorno):
    SIGRID_API_BASE_URL, SIGRID_API_FUNCTION_KEY, SIGRID_API_DATABASE, SIGRID_API_TIMEOUT_S
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date

import requests

# ----------------------------- Config editable ----------------------------- #
BASE_URL_DEFAULT = "https://func-sigridapi-dev-huyke.azurewebsites.net"
DATABASE_DEFAULT = "ruesma"
TIMEOUT_DEFAULT = 30
MAX_ROWS = 10000

# Parametros de busqueda por defecto (sobreescribibles por --nif / --anio).
NIF_DEFAULT = "A28733558"                                   # FEYMACO
ANIO_DEFAULT = 2026
CSV_DEFAULT = "lineas_proveedor_anio.csv"
# --------------------------------------------------------------------------- #

BASE_URL = BASE_URL_DEFAULT
KEY = ""
DATABASE = DATABASE_DEFAULT
TIMEOUT = TIMEOUT_DEFAULT

# Todas las lineas de factura de compra (dcfpro) del proveedor (dcf.entcif) en un año.
_SQL_LINEAS = """
SELECT
    fc.cod                                  AS num_factura,
    dcf.entref                              AS num_factura_proveedor,
    COALESCE(NULLIF(dcf.ano, 0), dcf.fecdoc / 10000) AS anio,
    dcf.fecdoc                              AS fecha_doc,
    oc.cod                                  AS obra_cod,
    oc.res                                  AS obra_nombre,
    pc.cod                                  AS producto_cod,
    lp.res                                  AS descripcion,
    lp.can                                  AS unidades,
    lp.pre                                  AS precio_ud,
    lp.tot                                  AS importe
FROM dbo.dcfpro           AS lp
INNER JOIN dbo.dcf        AS dcf ON dcf.ide = lp.docide
INNER JOIN dbo.con        AS fc  ON fc.ide  = dcf.ide
LEFT  JOIN dbo.con        AS oc  ON oc.ide  = dcf.obride
LEFT  JOIN dbo.con        AS pc  ON pc.ide  = lp.proide
WHERE
    REPLACE(REPLACE(REPLACE(UPPER(LTRIM(RTRIM(dcf.entcif))),
            '-', ''), '.', ''), ' ', '') = ?
    AND COALESCE(NULLIF(dcf.ano, 0), dcf.fecdoc / 10000) = ?
ORDER BY dcf.fecdoc DESC, fc.cod, lp.pos
"""


def _leer_dotenv(ruta: str = ".env") -> dict[str, str]:
    valores: dict[str, str] = {}
    if not os.path.exists(ruta):
        return valores
    with open(ruta, "r", encoding="utf-8") as fh:
        for linea in fh:
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            clave, _, valor = linea.partition("=")
            valores[clave.strip()] = valor.strip().strip('"').strip("'")
    return valores


def cargar_config(args: argparse.Namespace) -> None:
    global BASE_URL, KEY, DATABASE, TIMEOUT
    env = _leer_dotenv()

    def pick(*nombres: str, default: str = "") -> str:
        for n in nombres:
            if os.environ.get(n):
                return os.environ[n]
            if env.get(n):
                return env[n]
        return default

    BASE_URL = args.base_url or pick("SIGRID_API_BASE_URL", default=BASE_URL_DEFAULT)
    DATABASE = args.db or pick("SIGRID_API_DATABASE", default=DATABASE_DEFAULT)
    KEY = args.key or pick(
        "SIGRID_API_FUNCTION_KEY", "SIGRID_API_KEY",
        "SIGRID_API_CODE", "SIGRID_FUNCTION_KEY",
    )
    try:
        TIMEOUT = int(pick("SIGRID_API_TIMEOUT_S", default=str(TIMEOUT_DEFAULT)))
    except ValueError:
        TIMEOUT = TIMEOUT_DEFAULT

    if not KEY:
        sys.exit("[ERROR] Falta la key. Define SIGRID_API_FUNCTION_KEY en el .env, "
                 "o pasala con --key.")


def sql_read(sql: str, params: list) -> tuple[list[dict], bool]:
    resp = requests.post(
        f"{BASE_URL.rstrip('/')}/api/sql/read",
        headers={"x-functions-key": KEY, "Content-Type": "application/json"},
        json={"database": DATABASE, "sql": sql, "parameters": params,
              "timeout_seconds": TIMEOUT, "max_rows": MAX_ROWS},
        timeout=TIMEOUT + 60,
    )
    data = resp.json()
    if resp.status_code != 200 or not data.get("ok", False):
        sys.exit(f"[ERROR sigrid-api] HTTP {resp.status_code}: "
                 f"{data.get('error')} {data.get('details')}")
    cols = data.get("columns", [])
    filas = [dict(zip(cols, row)) for row in data.get("rows", [])]
    return filas, bool(data.get("truncated", False))


def norm_nif(valor: str) -> str:
    """Normaliza el CIF/NIF (mayusculas, sin guiones/puntos/espacios)."""
    return (valor or "").upper().strip().replace("-", "").replace(".", "").replace(" ", "")


def parse_fecha(valor) -> date | None:
    try:
        n = int(valor)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    try:
        return date(n // 10000, (n // 100) % 100, n % 100)
    except ValueError:
        return None


def _num(valor) -> float:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Todas las lineas facturadas a un proveedor en un año (a CSV).")
    ap.add_argument("--key", help="Function key (si no, del .env/entorno).")
    ap.add_argument("--base-url", help="URL base de sigrid-api (si no, del .env).")
    ap.add_argument("--db", help="Base de datos (si no, del .env; por defecto ruesma).")
    ap.add_argument("--nif", default=NIF_DEFAULT,
                    help=f"CIF/NIF del proveedor (def. {NIF_DEFAULT}).")
    ap.add_argument("--anio", type=int, default=ANIO_DEFAULT,
                    help=f"Año a extraer (def. {ANIO_DEFAULT}).")
    ap.add_argument("--out", default=CSV_DEFAULT, help="Ruta del CSV de salida.")
    args = ap.parse_args()

    cargar_config(args)
    print(f"  base_url={BASE_URL}  database={DATABASE}  timeout={TIMEOUT}s")

    nif = norm_nif(args.nif)
    print(f"1) Consultando lineas: nif={nif}  anio={args.anio}")
    filas, truncado = sql_read(_SQL_LINEAS, [nif, args.anio])
    if truncado:
        print("  AVISO: resultado truncado por MAX_ROWS (10000 lineas).")
    print(f"  lineas encontradas: {len(filas)}")

    if not filas:
        print("[OK] Sin resultados para el proveedor / año indicados.")
        return 0

    print(f"2) Escribiendo CSV -> {args.out}")
    cabecera = ["num_factura", "num_factura_proveedor", "anio",
                "fecha_doc", "obra_cod", "obra_nombre", "producto_cod",
                "descripcion", "unidades", "precio_ud", "importe"]
    total_uds = 0.0
    total_imp = 0.0
    with open(args.out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(cabecera)
        for f in filas:
            uds = _num(f.get("unidades"))
            imp = _num(f.get("importe"))
            total_uds += uds
            total_imp += imp
            fdoc = parse_fecha(f.get("fecha_doc"))
            w.writerow([
                f.get("num_factura") or "",
                f.get("num_factura_proveedor") or "",
                f.get("anio") if f.get("anio") is not None else "",
                fdoc.isoformat() if fdoc else "",
                f.get("obra_cod") or "",
                f.get("obra_nombre") or "",
                f.get("producto_cod") or "",
                f.get("descripcion") or "",
                uds, f.get("precio_ud") if f.get("precio_ud") is not None else "",
                imp,
            ])

    print(f"[OK] lineas={len(filas)}  total_uds={total_uds:.3f}  "
          f"total_importe={total_imp:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())