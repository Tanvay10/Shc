# syntax=docker/dockerfile:1

# Stage 1: build/install Python dependencies into a virtual environment.
FROM python:3.12-slim AS builder

ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv "${VIRTUAL_ENV}"
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt


# Stage 2: minimal runtime image, running as an unprivileged user.
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    INPUT_CSV="/data/patient_data.csv" \
    CLEANED_CSV="/output/cleaned_patient_data.csv" \
    FAILED_FILE="/output/failed_records.json" \
    BATCH_SIZE="100"

RUN groupadd --system app && useradd --system --gid app --home-dir /app app \
    && mkdir -p /app /data /output \
    && chown -R app:app /app /data /output

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=app:app shc_patient_pipeline.py /app/shc_patient_pipeline.py

USER app:app

# Token is supplied at runtime, never baked into the image.
ENTRYPOINT ["python", "/app/shc_patient_pipeline.py"]
CMD ["/data/patient_data.csv"]
