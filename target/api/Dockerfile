# Target API image. Built twice by target/up.sh:
#   ir-target-api:good    normal build
#   ir-target-api:broken  BROKEN=1, exits at startup (used by the deploy fault)
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ARG BROKEN=0
ENV BROKEN_BUILD=${BROKEN} \
    PYTHONUNBUFFERED=1

# Run as a non-root user: the target is meant to look like a normal production service.
RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
