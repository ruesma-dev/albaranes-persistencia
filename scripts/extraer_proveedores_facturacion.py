# extraer_proveedores_facturacion.py
"""Script de prueba (2): proveedores de Sigrid + facturado SIN IVA por año, a CSV.

Igual que extraer_proveedores.py (una fila por CIF, deduplicada y con datos del
proveedor) y ademas anade una columna por ano con lo FACTURADO SIN IVA, mas un
total. La facturacion sale de dbo.dcf (Factura de compra): base sin IVA = totbas,
ano = fecdoc/10000, proveedor = entcif. Se agrupa por CIF para consolidar aunque
haya varios conceptos-proveedor con el mismo CIF.

Lanzar desde la consola de un servicio (p.ej. sv3), SIN tocar nada:

    python extraer_proveedores_facturacion.py
    python extraer_proveedores_facturacion.py --desde-anio 2020

Config del .env del directorio actual (o variables de entorno):
    SIGRID_API_BASE_URL, SIGRID_API_FUNCTION_KEY, SIGRID_API_DATABASE, SIGRID_API_TIMEOUT_S
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import date

import requests

# ----------------------------- Config editable ----------------------------- #
BASE_URL_DEFAULT = "https://func-sigridapi-dev-huyke.azurewebsites.net"
DATABASE_DEFAULT = "ruesma"
TIMEOUT_DEFAULT = 30
MAX_ROWS = 10000
DESC_ACTUALIZADO = "PROVEEDOR ACTUALIZADO"
DESC_FECHA = "FECHA ACTUALIZACION FICHA"
COD_ACTUALIZADO = None
COD_FECHA = None
MODO_ACTUALIZADO = "flag"        # "flag" (valn==1) | "existencia" | "fecha"
ANIO_MIN_DEFAULT = 0             # 0 = todos los anos; sube el filtro si trunca
CSV_DEFAULT = "proveedores_facturacion.csv"
# --------------------------------------------------------------------------- #

BASE_URL = BASE_URL_DEFAULT
KEY = ""
DATABASE = DATABASE_DEFAULT
TIMEOUT = TIMEOUT_DEFAULT

_SQL_DEFEXT = (
    "SELECT cod, res, camtip FROM dbo.defext "
    "WHERE res LIKE ? OR res LIKE ? ORDER BY res"
)

# Proveedores: una fila por CIF (la mas reciente). Igual que el script 1.
_SQL_PROVEEDORES = """
SELECT cod, ide, nombre, razon_social, cif, tipo_doc, pais_cif,
       sector, clasificacion, delegacion,
       es_subcontratista, es_distribuidor, limite_credito,
       fecha_alta, fecha_baja, act_valn, act_valf, fec_valf
FROM (
    SELECT
        c.cod AS cod, c.ide AS ide, c.res AS nombre, c.fec AS fecha_alta,
        c.fecbaj AS fecha_baja,
        p.cif AS cif, p.tipcif AS tipo_doc, p.cifpai AS pais_cif,
        p.raz AS razon_social, p.tipsub AS es_subcontratista,
        p.tipdis AS es_distribuidor, p.crelim AS limite_credito,
        sec.res AS sector, tar.res AS clasificacion, del.res AS delegacion,
        a.valn AS act_valn, a.valf AS act_valf, f.valf AS fec_valf,
        ROW_NUMBER() OVER (
            PARTITION BY (CASE WHEN p.cif IS NULL OR p.cif = ''
                               THEN CAST(c.ide AS varchar(20)) ELSE p.cif END)
            ORDER BY COALESCE(f.valf, a.valf, 0) DESC, c.ide DESC
        ) AS rn_cif
    FROM dbo.con AS c
    JOIN dbo.prv AS p ON p.ide = c.ide
    LEFT JOIN dbo.auxsec    AS sec ON sec.ide = p.secide
    LEFT JOIN dbo.auxtarprv AS tar ON tar.ide = p.taride
    LEFT JOIN dbo.auxdel    AS del ON del.ide = p.delide
    LEFT JOIN (
        SELECT conide, valn, valf,
               ROW_NUMBER() OVER (PARTITION BY conide ORDER BY ide DESC) AS rn
        FROM dbo.conext WHERE cod = ?
    ) AS a ON a.conide = c.ide AND a.rn = 1
    LEFT JOIN (
        SELECT conide, valf,
               ROW_NUMBER() OVER (PARTITION BY conide ORDER BY ide DESC) AS rn
        FROM dbo.conext WHERE cod = ?
    ) AS f ON f.conide = c.ide AND f.rn = 1
) AS q
WHERE q.rn_cif = 1
ORDER BY q.cod
"""

# Facturado SIN IVA por CIF y ano (dcf = Factura de compra; totbas = base imponible).
_SQL_FACTURACION = """
SELECT f.entcif AS cif, (f.fecdoc / 10000) AS anio,
       SUM(f.totbas) AS base_sin_iva, COUNT(*) AS num_facturas
FROM dbo.dcf AS f
WHERE f.fecdoc > 0 AND f.entcif IS NOT NULL AND f.entcif <> ''
  AND (f.fecdoc / 10000) >= ?
GROUP BY f.entcif, (f.fecdoc / 10000)
ORDER BY f.entcif, anio
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


def descubrir_cods() -> tuple[str | None, str | None]:
    if COD_ACTUALIZADO or COD_FECHA:
        return COD_ACTUALIZADO, COD_FECHA
    filas, _ = sql_read(_SQL_DEFEXT, [f"%{DESC_ACTUALIZADO}%", f"%{DESC_FECHA}%"])
    print(f"  campos extendidos candidatos: {len(filas)}")
    for f in filas:
        print(f"    cod={f.get('cod')!r:>10}  camtip={f.get('camtip')}  res={f.get('res')!r}")
    cod_act = cod_fec = None
    for f in filas:
        res = (f.get("res") or "").upper()
        if DESC_FECHA.upper() in res and cod_fec is None:
            cod_fec = f.get("cod")
        elif DESC_ACTUALIZADO.upper() in res and "FECHA" not in res and cod_act is None:
            cod_act = f.get("cod")
    if cod_act is None and cod_fec is None:
        sys.exit("[ERROR] No se resolvieron los campos. Fija COD_ACTUALIZADO/COD_FECHA "
                 "arriba con alguno de los cod listados.")
    print(f"  resueltos -> actualizado={cod_act!r}, fecha={cod_fec!r}")
    return cod_act, cod_fec


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


def es_actualizado(cod_act, fila, fecha) -> bool:
    if cod_act is None:
        return fecha is not None
    if MODO_ACTUALIZADO == "existencia":
        return fila.get("act_valn") is not None or fila.get("act_valf") not in (None, 0)
    if MODO_ACTUALIZADO == "fecha":
        return fecha is not None
    try:
        return int(fila.get("act_valn")) == 1
    except (TypeError, ValueError):
        return False


def _sino(valor) -> str:
    try:
        return "SI" if int(valor) != 0 else "NO"
    except (TypeError, ValueError):
        return "NO"


def norm_cif(valor) -> str:
    """Normaliza el CIF para casar factura<->ficha (sin espacios, mayusculas)."""
    return (valor or "").strip().upper().replace(" ", "")


def dedup_por_cif(filas: list[dict], cod_fec) -> list[dict]:
    mejor: dict[str, tuple] = {}
    for f in filas:
        cif = norm_cif(f.get("cif"))
        clave = cif if cif else f"__ide_{f.get('ide')}"
        fecha = parse_fecha(f.get("fec_valf") if cod_fec else f.get("act_valf"))
        recencia = (fecha or date.min, int(f.get("ide") or 0))
        if clave not in mejor or recencia > mejor[clave][0]:
            mejor[clave] = (recencia, f)
    filas_unicas = [v[1] for v in mejor.values()]
    filas_unicas.sort(key=lambda f: (f.get("cod") or ""))
    return filas_unicas


def extraer_facturacion(anio_min: int) -> tuple[dict[str, dict[int, float]], list[int]]:
    """Devuelve {cif_normalizado: {anio: base_sin_iva}} y la lista de anos ordenada."""
    filas, truncado = sql_read(_SQL_FACTURACION, [anio_min])
    if truncado:
        print("  AVISO: facturacion truncada por MAX_ROWS; usa --desde-anio para acotar.")
    fact: dict[str, dict[int, float]] = defaultdict(dict)
    anios: set[int] = set()
    for f in filas:
        cif = norm_cif(f.get("cif"))
        try:
            anio = int(f.get("anio"))
            base = round(float(f.get("base_sin_iva") or 0), 2)
        except (TypeError, ValueError):
            continue
        if not cif or anio <= 0:
            continue
        fact[cif][anio] = base
        anios.add(anio)
    return fact, sorted(anios)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extrae proveedores + facturado sin IVA por ano a CSV.")
    ap.add_argument("--key", help="Function key (si no, del .env/entorno).")
    ap.add_argument("--base-url", help="URL base de sigrid-api (si no, del .env).")
    ap.add_argument("--db", help="Base de datos (si no, del .env; por defecto ruesma).")
    ap.add_argument("--desde-anio", type=int, default=ANIO_MIN_DEFAULT,
                    help="Ano minimo de facturacion (0 = todos).")
    ap.add_argument("--out", default=CSV_DEFAULT, help="Ruta del CSV de salida.")
    args = ap.parse_args()

    cargar_config(args)
    print(f"  base_url={BASE_URL}  database={DATABASE}  timeout={TIMEOUT}s")

    print("1) Descubriendo campos extendidos...")
    cod_act, cod_fec = descubrir_cods()

    print("2) Extrayendo proveedores (deduplicando por CIF)...")
    filas, truncado = sql_read(_SQL_PROVEEDORES, [cod_act or "", cod_fec or ""])
    if truncado:
        print("  AVISO: proveedores truncados por MAX_ROWS.")
    filas = dedup_por_cif(filas, cod_fec)
    print(f"  proveedores unicos (por CIF): {len(filas)}")

    print("3) Extrayendo facturado sin IVA por ano (dcf.totbas)...")
    fact, anios = extraer_facturacion(args.desde_anio)
    print(f"  anos con facturacion: {anios}")

    print(f"4) Escribiendo CSV -> {args.out}")
    col_anios = [f"fact_sin_iva_{a}" for a in anios]
    cabecera = ["cod", "ide", "nombre", "razon_social", "cif", "tipo_doc",
                "pais_cif", "sector", "clasificacion", "delegacion",
                "es_subcontratista", "es_distribuidor", "limite_credito",
                "fecha_alta", "fecha_baja", "actualizado", "fecha_actualizacion"
                ] + col_anios + ["fact_sin_iva_total"]

    n_act = 0
    with open(args.out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(cabecera)
        for f in filas:
            fecha_act = parse_fecha(f.get("fec_valf") if cod_fec else f.get("act_valf"))
            act = es_actualizado(cod_act, f, fecha_act)
            n_act += int(act)
            f_alta = parse_fecha(f.get("fecha_alta"))
            f_baja = parse_fecha(f.get("fecha_baja"))
            por_anio = fact.get(norm_cif(f.get("cif")), {})
            valores_anio = [por_anio.get(a, "") for a in anios]
            total = round(sum(v for v in por_anio.values()), 2) if por_anio else ""
            w.writerow([
                f.get("cod") or "", f.get("ide"), f.get("nombre") or "",
                f.get("razon_social") or "", f.get("cif") or "",
                f.get("tipo_doc") or "", f.get("pais_cif") or "",
                f.get("sector") or "", f.get("clasificacion") or "",
                f.get("delegacion") or "",
                _sino(f.get("es_subcontratista")), _sino(f.get("es_distribuidor")),
                f.get("limite_credito") if f.get("limite_credito") is not None else "",
                f_alta.isoformat() if f_alta else "",
                f_baja.isoformat() if f_baja else "",
                "SI" if act else "NO",
                fecha_act.isoformat() if fecha_act else "",
            ] + valores_anio + [total])

    print(f"[OK] proveedores={len(filas)}  actualizados={n_act}  anos={len(anios)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
