FROM python:3.12-slim

WORKDIR /app

ARG FABRIC_KG_QUERY_SCHEMA_SOURCE=""
COPY . .
COPY ${FABRIC_KG_QUERY_SCHEMA_SOURCE} /app/runtime-authority/persisted-query-schema.json

RUN pip install --no-cache-dir ".[app]"

ENV PYTHONUNBUFFERED=1
ENV FABRIC_KG_QUERY_SCHEMA_SOURCE=${FABRIC_KG_QUERY_SCHEMA_SOURCE}

EXPOSE 8000
CMD ["uvicorn", "fabric_kg_builder.app.api:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]
