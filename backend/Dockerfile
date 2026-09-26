FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
COPY ledgerloop ./ledgerloop
COPY scripts ./scripts
RUN pip install --no-cache-dir .
EXPOSE 8000
CMD ["uvicorn", "ledgerloop.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
