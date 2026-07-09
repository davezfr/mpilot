FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY app ./app
COPY mcp_server ./mcp_server
COPY babelarr ./babelarr
COPY media_workflow_runtime ./media_workflow_runtime
COPY media_subtitle_translator ./media_subtitle_translator
RUN pip install --no-cache-dir ".[download]"

RUN addgroup --system qbitlarr \
    && adduser --system --ingroup qbitlarr qbitlarr \
    && mkdir -p /app/data \
    && chown -R qbitlarr:qbitlarr /app

EXPOSE 8000

USER qbitlarr

CMD ["uvicorn", "mpilot.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
