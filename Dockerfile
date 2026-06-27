FROM python:3.11-slim

WORKDIR /app

# Install uv
RUN pip install uv --quiet

# Copy dependency files first (layer cache)
COPY pyproject.toml .
COPY uv.lock* .

# Install dependencies
RUN uv sync --no-dev 2>/dev/null || uv pip install -r pyproject.toml --system

# Copy source
COPY . .

# Expose port
EXPOSE 8080

# Start FastAPI server
CMD ["python", "-m", "uvicorn", "fast_api_app:fastapi_app", "--host", "0.0.0.0", "--port", "8080"]