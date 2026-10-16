FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY pyproject.toml README.md ./
COPY backend ./backend
COPY migrations ./migrations
COPY scripts ./scripts
COPY alembic.ini ./alembic.ini
RUN pip install --no-cache-dir . \
    && groupadd --system workflow \
    && useradd --system --gid workflow --home-dir /app workflow \
    && chown -R workflow:workflow /app
USER workflow
EXPOSE 8000
CMD ["python", "-m", "backend.app"]
