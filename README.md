# Cloud Run Lead Scanner

A small Flask + Playwright service for manually reviewing borrower leads in a browser dashboard.

## Features

- `GET /` status JSON
- `GET /health` health check
- `POST /scan` scrape and classify leads without saving
- `POST /scan-save` scrape, classify, and save borrower leads
- `GET /dashboard` browser dashboard for saved leads
- `GET /export.csv` CSV export of saved leads
- `POST /suggest-comments` generate polite Vietnamese manual comment suggestions
- `POST /auto-comment` disabled with HTTP 501

## Local run

```bash
pip install -r requirements.txt
python -m playwright install chromium
python main.py
```

Then open:

- `http://localhost:8080/`
- `http://localhost:8080/dashboard`

## Environment variables

- `REVIEW_TOKEN`: required for `/dashboard`, `/export.csv`, and `/scan-save`
- `LEADS_BUCKET`: GCS bucket name for production storage
- `LEADS_LOCAL_PATH`: local JSONL fallback path, default `data/leads.jsonl`
- `SCAN_DEFAULT_URLS`: comma-separated default URLs
- `MAX_POSTS_DEFAULT`: default max posts per scan
- `SCAN_DELAY_SECONDS`: pause between URL scans
- `VERIFY_PROXY`: set `true` to verify outbound IP via `https://api.ipify.org?format=json`
- `PROXY_SERVER`: proxy server URL
- `PROXY_USERNAME`: proxy username
- `PROXY_PASSWORD`: proxy password
- `USER_AGENT`: optional custom user agent

## Storage

If `LEADS_BUCKET` is set, the app stores leads in GCS as JSONL:

- `leads/YYYY-MM-DD.jsonl`

If `LEADS_BUCKET` is not set, it falls back to local storage:

- `data/leads.jsonl`

## GCS bucket creation

```bash
gsutil mb -l asia-southeast1 gs://YOUR_BUCKET_NAME
```

Grant the Cloud Run service account permission to write and read from the bucket.

## Cloud Run deploy

Build the container and deploy it to Cloud Run with your env vars:

```bash
gcloud run deploy lead-scanner \
  --source . \
  --region asia-southeast1 \
  --allow-unauthenticated \
  --set-env-vars REVIEW_TOKEN=your_token,LEADS_BUCKET=your_bucket,SCAN_DEFAULT_URLS=https://example.com \
  --memory 1Gi \
  --cpu 1
```

If you want the dashboard protected, keep `REVIEW_TOKEN` set and do not expose the service publicly.

## API examples

### Scan

```bash
curl -X POST http://localhost:8080/scan \
  -H "Content-Type: application/json" \
  -d '{"urls":["https://example.com"],"max_posts":20,"include_comments":false}'
```

### Scan and save

```bash
curl -X POST "http://localhost:8080/scan-save?token=YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"urls":["https://example.com"],"max_posts":20,"include_comments":false}'
```

### Suggest comments

```bash
curl -X POST http://localhost:8080/suggest-comments \
  -H "Content-Type: application/json" \
  -d '{"leads":[{"text":"mình đang cần vay gấp"}]}'
```

### Open dashboard

```text
/dashboard?token=YOUR_TOKEN
```

## Cloud Scheduler every 30 minutes

Create a scheduler job that calls `/scan-save`:

```bash
gcloud scheduler jobs create http lead-scanner-save \
  --schedule="*/30 * * * *" \
  --uri="https://YOUR_CLOUD_RUN_URL/scan-save?token=YOUR_TOKEN" \
  --http-method=POST \
  --headers="Content-Type=application/json" \
  --message-body='{"urls":["https://example.com"],"max_posts":20,"include_comments":false}'
```

## Notes

- No Facebook login automation is implemented.
- No automatic posting, messaging, account rotation, or proxy rotation is implemented.
- The dashboard only supports manual review and copy-to-clipboard actions.
