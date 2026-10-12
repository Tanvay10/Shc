#!/usr/bin/env python3
import argparse
import csv
import json
import logging
import math
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests


API_URL = "https://api.mock-shc.com/v1/patients"
MAX_BATCH_SIZE = 100
MIN_REQUEST_INTERVAL_SECONDS = 0.2 
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 30

DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",  
    "%d-%m-%Y",
    "%d/%m/%Y",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("shc_patient_pipeline")


def normalize_dob(value):
    if pd.isna(value) or not str(value).strip():
        return ""
    value = str(value).strip()
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            pass
    return ""


def parse_balance(value):
    if pd.isna(value) or not str(value).strip():
        return None

    text = str(value).strip()
    parenthesized_negative = text.startswith("(") and text.endswith(")")
    if parenthesized_negative:
        text = text[1:-1]

    text = re.sub(r"[^\d,.\-+]", "", text).replace(",", "")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", text):
        return None

    try:
        number = float(text)
        if not math.isfinite(number):
            return None
        return -abs(number) if parenthesized_negative else number
    except ValueError:
        return None


def load_and_clean_csv(input_path):
    """Load CSV, normalize fields, and retain latest record per patient_id."""
    with open(input_path, "r", encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source)
        try:
            columns = next(reader)
        except StopIteration:
            raise ValueError("Input CSV is empty.")

        rows = []
        for line_number, row in enumerate(reader, start=2):
            if len(row) == len(columns) + 1:
                row[6:8] = [row[6] + "," + row[7]]
            if len(row) != len(columns):
                raise ValueError(
                    f"CSV row {line_number} has {len(row)} fields; expected {len(columns)}."
                )
            rows.append(row)
    df = pd.DataFrame(rows, columns=columns, dtype=str).fillna("")

    required_columns = {
        "patient_id", "first_name", "last_name", "dob",
        "email", "phone", "account_balance", "last_modified",
    }
    missing = required_columns - set(df.columns)
    if missing:
        raise ValueError("Missing required CSV columns: " + ", ".join(sorted(missing)))

    for column in df.columns:
        df[column] = df[column].map(
            lambda value: value.strip() if isinstance(value, str) else value
        )

    df["dob"] = df["dob"].map(normalize_dob)
    df["account_balance"] = df["account_balance"].map(parse_balance)

    for column in ("email", "phone"):
        df[column] = df[column].replace("", None)

    df["_parsed_last_modified"] = pd.to_datetime(
        df["last_modified"], errors="coerce", utc=True
    )
    df["_input_order"] = range(len(df))
    df = df.sort_values(
        ["_parsed_last_modified", "_input_order"],
        kind="stable",
        na_position="first",
    )
    df = df.drop_duplicates(subset=["patient_id"], keep="last")
    df = df.drop(columns=["_parsed_last_modified", "_input_order"])
    return df


class RateLimiter:
    """Simple synchronous limiter spacing request starts by 0.2 seconds."""

    def __init__(self, min_interval=MIN_REQUEST_INTERVAL_SECONDS):
        self.min_interval = min_interval
        self.last_request_start = None

    def wait(self):
        now = time.monotonic()
        if self.last_request_start is not None:
            elapsed = now - self.last_request_start
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        self.last_request_start = time.monotonic()


def append_failed_batch(failed_file, batch, status_code, response_text, reason):
    """Append a failed request and its records to a JSON array on disk."""
    entry = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "status_code": status_code,
        "reason": reason,
        "response": (response_text or "")[:4000],
        "records": batch,
    }

    failed_path = Path(failed_file)
    existing = []
    if failed_path.exists():
        try:
            with failed_path.open("r", encoding="utf-8") as file:
                existing = json.load(file)
            if not isinstance(existing, list):
                existing = []
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read existing %s; starting a new JSON list", failed_file)

    existing.append(entry)
    temp_path = failed_path.with_suffix(failed_path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(existing, file, ensure_ascii=False, indent=2)
    temp_path.replace(failed_path)


def send_batch(session, limiter, batch, token, failed_file, batch_number):
    """Send one batch, retrying 429/5xx with exponential backoff."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    for attempt in range(MAX_RETRIES + 1):
        limiter.wait()
        try:
            response = session.post(
                API_URL,
                headers=headers,
                json={"patients": batch},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES:
                delay = INITIAL_BACKOFF_SECONDS * (2 ** attempt) + random.uniform(0, 0.25)
                logger.warning(
                    "Batch %s network error (%s); retrying in %.2fs",
                    batch_number, exc, delay,
                )
                time.sleep(delay)
                continue

            logger.error("Batch %s failed after network retries: %s", batch_number, exc)
            append_failed_batch(
                failed_file, batch, None, str(exc), "network_error_after_retries"
            )
            return False

        if 200 <= response.status_code < 300:
            logger.info(
                "Batch %s accepted (%s records, HTTP %s)",
                batch_number, len(batch), response.status_code,
            )
            return True

        should_retry = response.status_code == 429 or 500 <= response.status_code <= 599
        if should_retry:
            if attempt < MAX_RETRIES:
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = max(0.0, float(retry_after))
                    except ValueError:
                        delay = INITIAL_BACKOFF_SECONDS * (2 ** attempt)
                else:
                    delay = INITIAL_BACKOFF_SECONDS * (2 ** attempt)
                delay += random.uniform(0, 0.25)
                logger.warning(
                    "Batch %s received HTTP %s; retry %s/%s in %.2fs",
                    batch_number, response.status_code, attempt + 1, MAX_RETRIES, delay,
                )
                time.sleep(delay)
                continue

            logger.error(
                "Batch %s exhausted retries after HTTP %s",
                batch_number, response.status_code,
            )
            append_failed_batch(
                failed_file, batch, response.status_code, response.text,
                "retry_limit_exceeded",
            )
            return False

        if response.status_code == 400:
            logger.error("Batch %s rejected with HTTP 400; logging batch and continuing", batch_number)
            append_failed_batch(
                failed_file, batch, response.status_code, response.text, "bad_request"
            )
            return False

        logger.error("Batch %s failed with non-retryable HTTP %s", batch_number, response.status_code)
        append_failed_batch(
            failed_file, batch, response.status_code, response.text,
            "non_retryable_http_error",
        )
        return False

    return False


def main():
    parser = argparse.ArgumentParser(
        description="Normalize patient CSV data and ingest it into the SHC Platform API."
    )
    parser.add_argument("input_csv", nargs="?", default=os.getenv("INPUT_CSV", "/data/patient_data.csv"), help="Path to source patient CSV")
    parser.add_argument(
        "--failed-file",
        default=os.getenv("FAILED_FILE", "/output/failed_records.json"),
        help="JSON file used to log rejected/failed batches (default: failed_records.json)",
    )
    parser.add_argument(
        "--cleaned-csv",
        default=os.getenv("CLEANED_CSV", "/output/cleaned_patient_data.csv"),
        help="Write normalized and deduplicated data here (default: cleaned_patient_data.csv)",
    )
    args = parser.parse_args()

    token = os.getenv("SHC_API_TOKEN")
    if not token:
        parser.error("Set SHC_API_TOKEN in the environment before running this script.")

    df = load_and_clean_csv(args.input_csv)
    df.to_csv(args.cleaned_csv, index=False, na_rep="")
    logger.info("Normalized %s unique patients; saved %s", len(df), args.cleaned_csv)

    records = json.loads(df.to_json(orient="records", date_format="iso"))
    for record in records:
        if record.get("account_balance") is not None:
            record["account_balance"] = float(record["account_balance"])

    requested_batch_size = int(os.getenv("BATCH_SIZE", str(MAX_BATCH_SIZE)))
    if requested_batch_size < 1:
        parser.error("BATCH_SIZE must be at least 1.")
    batch_size = min(requested_batch_size, MAX_BATCH_SIZE)
    if requested_batch_size > MAX_BATCH_SIZE:
        logger.warning("BATCH_SIZE capped at API maximum of %s", MAX_BATCH_SIZE)

    batches = [
        records[index:index + batch_size]
        for index in range(0, len(records), batch_size)
    ]
    if not batches:
        logger.info("No patient records to ingest.")
        return

    limiter = RateLimiter()
    succeeded = 0
    failed = 0

    with requests.Session() as session:
        for batch_number, batch in enumerate(batches, start=1):
            if send_batch(
                session, limiter, batch, token, args.failed_file, batch_number
            ):
                succeeded += 1
            else:
                failed += 1

    logger.info(
        "Ingestion complete: %s batches succeeded, %s batches failed, %s total records",
        succeeded, failed, len(records),
    )


if __name__ == "__main__":
    main()
