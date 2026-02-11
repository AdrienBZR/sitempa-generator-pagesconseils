FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .

# Expose port (default for Coolify/FastAPI is usually 8000)
EXPOSE 8000

# Command to run (reading credentials from env var GOOGLE_CREDENTIALS_JSON)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
