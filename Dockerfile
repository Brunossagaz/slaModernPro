# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements-lock.txt ./
RUN pip install --no-deps -r requirements-lock.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps . \
    # pip só é necessário no build; fora da imagem final ele não pode ser explorado
    && pip uninstall -y pip


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Remove também o pip do Python base (pip-audit aponta CVEs nele e a app não o usa)
RUN python -m pip uninstall -y pip \
    && groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid appuser --no-create-home --shell /usr/sbin/nologin appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

CMD ["python", "-m", "uvicorn", "sla_modern_pro.web:app", "--host", "0.0.0.0", "--port", "8000"]
