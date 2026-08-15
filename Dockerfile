FROM python:3.11-slim

WORKDIR /app

# Minimal dependencies for a RunPod serverless worker that returns a base64 JPEG.
COPY requirements-runpod.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy repo code (handler lives in src/runpod/)
COPY . /app

ENV PYTHONPATH=/app

CMD ["python", "-m", "src.runpod.image_worker"]

