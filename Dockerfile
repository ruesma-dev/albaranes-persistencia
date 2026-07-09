# manifests/sv3/Dockerfile — code en raiz, comun[azure]
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 TZ=Europe/Madrid
WORKDIR /app
COPY comun/ /tmp/comun/
RUN pip install "/tmp/comun[azure]" && rm -rf /tmp/comun
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "main.py"]
