# CC Elasticsearch Analyzer — container image
# Small, self-contained FastAPI app served by uvicorn on port 8000 (internal).
FROM python:3.12-slim

# Don't buffer stdout/stderr so `docker logs` shows output immediately.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code (see .dockerignore for what is excluded).
COPY main.py config.py ./
COPY routers ./routers
COPY services ./services
COPY frontend ./frontend

# Runtime log directory (also used as a mount point in docker-compose).
RUN mkdir -p /app/logs

EXPOSE 8000

# Run through main.py (not uvicorn directly) so the __main__ block wires up
# HTTPS when SERVICE_SSL=true. --no-reload keeps it a single stable process.
CMD ["python", "main.py", "--no-reload"]

