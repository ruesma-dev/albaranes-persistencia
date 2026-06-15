# Dockerfile — sv3 (persistencia + enriquecimiento Sigrid)
#
# CONVENCIÓN DE CONTEXTO: el build NO se lanza desde la carpeta del
# servicio, sino desde un contexto preparado que contiene:
#     comun/      → paquete ruesma-albaranes-comun
#     servicio/   → este servicio (sin .env, .venv, logs, .git, .idea)
# Lo prepara comun/local/preparar_contexto.ps1 y lo construye
# 'az acr build' (sin Docker Desktop). Ejemplo:
#     .\preparar_contexto.ps1 -Servicio sv3 -Registro acralbaranesruesma
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    API_HOST=0.0.0.0

WORKDIR /app

# Libs del sistema que necesita xhtml2pdf (render DOCX→PDF del contrato)
# sobre python:slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libfreetype6 libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

# 1) Paquete común (capa estable, se cachea salvo que cambie comun/).
COPY comun/ /tmp/comun/
RUN pip install /tmp/comun && rm -rf /tmp/comun

# 2) Dependencias del servicio (capa cacheada salvo que cambie requirements).
COPY servicio/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# 3) Código del servicio (los scripts de diagnóstico no van a la imagen:
#    preparar_contexto.ps1 excluye sv3/scripts).
COPY servicio/ /app/

# Usuario sin privilegios.
RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

# El puerto real lo fija API_PORT (.env/ACA target_port); EXPOSE es informativo.
EXPOSE 8000

CMD ["python", "main.py"]
