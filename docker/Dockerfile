FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[download]"

RUN addgroup --system mpilot \
    && adduser --system --ingroup mpilot mpilot \
    && mkdir -p /app/data \
    && chown -R mpilot:mpilot /app

EXPOSE 8000

USER mpilot

CMD ["uvicorn", "mpilot.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
