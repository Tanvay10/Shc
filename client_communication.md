# Email to GlobalHealth Corp IT Director

**Subject:** SHC patient data migration container and export recommendations

Dear GlobalHealth Corp IT Director,

The patient ingestion container is ready to build and run in your on-premise Docker environment. To build the Docker image, run the following command from the project directory:

"docker build -t shc-patient-ingestion:1.0 ."

Place the input CSV file in the data/patient_data.csv location and create an output directory. Please configure the bearer token as SHC_API_TOKEN using your approved secret-management process. You can then run the container using:

"
docker run --rm `
  -e SHC_API_TOKEN `
  -v "${PWD}/data:/data:ro" `
  -v "${PWD}/output:/output:rw" `
  shc-patient-ingestion:1.0
  "

The script cleans and normalizes the patient data, handles missing email and phone values, standardizes supported date formats, converts account balances into numeric values, and keeps the most recently updated record for duplicate patient IDs. It also sends the data to the API in batches and logs failed requests in output/failed_records.json.

For future exports, I recommend keeping the CSV format consistent, especially for dates and values containing commas. This will help avoid parsing issues and make the data migration process more reliable.
Before proceeding with the actual migration, please confirm the API endpoint and provide an approved test token so we can validate the integration.

Please let me know if any changes are required.

Regards,
SHC Technologies — Forward Deployed Engineering
