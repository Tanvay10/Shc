# SHC Patient Ingestion

This pipeline reads a legacy patient CSV, normalizes its fields, keeps the most recently modified row for each patient ID, saves a cleaned CSV, and posts the records to the SHC API.

## Design decisions

- `pandas` handles tabular cleanup and timestamp sorting; `requests` sends JSON payloads.
- DOBs are tried in this order: ISO (`YYYY-MM-DD`), US slash format (`MM/DD/YYYY`), day-first hyphen format (`DD-MM-YYYY`), then day-first slash format (`DD/MM/YYYY`). Ambiguous slash dates are interpreted as month-first when both interpretations are possible. Invalid or missing dates become empty strings.
- Blank email and phone values are sent as JSON `null`.
- Balances accept currency symbols and thousands separators; invalid, non-finite, and blank amounts become JSON `null`. Parenthesized amounts are treated as negative.
- The CSV reader repairs a row with one extra field by joining the two balance fields. This supports the supplied sample's unquoted `1,250.00`; other incorrect row widths produce a clear error.
- Duplicate IDs are sorted by `last_modified` as UTC timestamps. The latest timestamp wins; ties keep the later row in the input file. Rows with invalid timestamps sort before valid ones.
- Requests are spaced by 0.2 seconds (at most five starts per second), batches are capped at 100, and 429/5xx/network errors use bounded exponential retries. Failed batches are appended to the configured JSON log so processing can continue.
- The Docker image uses a multi-stage build and runs as an unprivileged user. The API token is injected at runtime.

## Build

From this directory:

```bash
docker build -t shc-patient-ingestion:1.0 .
```

## Run (PowerShell)

Place the source file at `data/patient_data.csv`, then:

```powershell
New-Item -ItemType Directory -Force data, output | Out-Null
$env:SHC_API_TOKEN = "your-real-token"

docker run --rm `
  --read-only `
  --cap-drop=ALL `
  --security-opt=no-new-privileges `
  -e SHC_API_TOKEN `
  -e INPUT_CSV=/data/patient_data.csv `
  -e CLEANED_CSV=/output/cleaned_patient_data.csv `
  -e FAILED_FILE=/output/failed_records.json `
  -e BATCH_SIZE=100 `
  -v "${PWD}/data:/data:ro" `
  -v "${PWD}/output:/output:rw" `
  shc-patient-ingestion:1.0
```

The cleaned file and failed batch log are saved under `output/`. Use a secret manager for production credentials; do not commit or bake the token into the image. The configured endpoint is `https://api.mock-shc.com/v1/patients`.

## Configuration

- `SHC_API_TOKEN`: required bearer token.
- `INPUT_CSV`: source CSV path (default `/data/patient_data.csv`).
- `CLEANED_CSV`: normalized CSV destination (default `/output/cleaned_patient_data.csv`).
- `FAILED_FILE`: failed batch JSON destination (default `/output/failed_records.json`).
- `BATCH_SIZE`: requested batch size, capped at 100 (default `100`).

## Tests

No automated test suite is included. Before production use, validate the parser with representative exports (especially ambiguous DOBs and malformed rows) and run the ingestion against an approved test endpoint and token.
