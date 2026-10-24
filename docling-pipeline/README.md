# docling → DoclingDocument JSON on AWS Lambda (ECR container)

An SQS message triggers the function. It downloads the referenced S3 object, converts it with [docling](https://github.com/docling-project/docling), writes `<name>.docling.json` to S3, and returns the output location. It can also publish the location to a results queue.

```
S3 upload ──(notification)──▶ SQS jobs ──▶ Lambda (this image) ──▶ s3://<out>/converted/<key>.docling.json
                                                           └──▶ SQS results (optional): {"output": "s3://..."}
```

All docling model weights (layout, TableFormer, RapidOCR, code/formula, figure classifier) are **baked into the image** at `/opt/docling-models`, and the container runs with `HF_HUB_OFFLINE=1`. Cold starts never download anything. Converters are built once per container (one with OCR, one without, created on first use), so warm invocations reuse the loaded models.

## Message formats

The SQS message body can be either of these:

```jsonc
// 1. S3 event notification (S3 -> SQS, or S3 -> SNS -> SQS)
{"Records": [{"s3": {"bucket": {"name": "in"}, "object": {"key": "docs/a.pdf"}}}]}

// 2. Direct request (everything but bucket+key / s3_uri optional). This is what the uploader sends.
{"bucket": "state", "key": "workspaces/s/uploader/originals/K/report.pdf",
 "output_bucket": "state", "output_key": "workspaces/s/uploader/converted/K/report.docling.json",
 "source_url": "https://example.edu/report.pdf",   // cited as origin.uri
 "scraped": false,                                  // true: a fetched web page, see below
 "content_type": "application/pdf",                 // scraped pages only, optional
 "context": {"workspace_id": "s", "key": "K"}}      // opaque, echoed back in the result
{"s3_uri": "s3://in/docs/a.pdf"}
```

`"scraped": true` marks a web page the uploader fetched and stored without a file extension.
The Lambda detects a PDF by content (`%PDF-`, or `content_type` containing `application/pdf`) and
converts it as `<url-stem>.pdf`. Anything else is treated as HTML and cleaned exactly like the
uploader's scraper did (`convert_page` in `uploader/decling_conversion.py`): header/footer/nav/
script/style/aside removed, then forms, loaders and breadcrumbs unless they wrap the main content
(e.g. ASP.NET's page-level `<form>`). A page with under 50 characters of text fails with "Page
returned no usable content". Either way origin.uri is `source_url`. Without `scraped`, the file
converts by its extension as before.

Return value:

```json
{"batchItemFailures": [],
 "results": [{"status": "success", "source": "s3://in/docs/a.pdf",
              "output": "s3://out/converted/docs/a.docling.json", "output_bucket": "out",
              "output_key": "converted/docs/a.docling.json", "seconds": 12.3, "message_id": "..."}]}
```

When an SQS event source invokes a Lambda, the return value goes nowhere except `batchItemFailures`. To consume results downstream, set `RESULT_QUEUE_URL` (**required for the uploader integration**: the uploader learns about finished and failed jobs only from this queue). The output object also carries `x-amz-meta-source` with the source URI.

Messages sent to `RESULT_QUEUE_URL`:

```jsonc
// every success: the result above plus
{"status": "success", "source": "s3://...", "output": "s3://...", "output_bucket": "...", "output_key": "...",
 "ocr": false, "seconds": 12.3, "message_id": "...",
 "content_hash": "<sha256 hex of the exact bytes written>", "context": {/* from the job, {} if none */}}

// a failure on the message's final attempt only
{"status": "error", "source": "s3://b/k", "error": "<message>", "context": {...}, "message_id": "..."}
```

A failure whose SQS `ApproximateReceiveCount` is below `MAX_RECEIVE_COUNT` publishes nothing: it is
reported in `batchItemFailures` and SQS retries it. Keep `MAX_RECEIVE_COUNT` equal to the jobs queue's
redrive `maxReceiveCount`, so the error result goes out on the attempt before the message moves to the
DLQ. A body that isn't valid JSON / a job has no context to report, so it is only logged and failed.
Skipped objects (folder markers, outputs under `OUTPUT_PREFIX`) publish nothing.

With the Infochat uploader, the original is temporary: the uploader deletes it once it
has applied the result (success, or the final failure). Only the `.docling.json` is kept,
so the Lambda role needs just `s3:GetObject` on `workspaces/*/uploader/originals/*` and
`s3:PutObject` on `workspaces/*/uploader/converted/*` in the state bucket.

## Direct invocation (universal-chat attachments)

Invoking the function directly (`lambda.invoke`, no SQS `Records`) converts one object and
returns Markdown in the reply instead of writing to S3:

```jsonc
// request
{"bucket": "<state bucket>", "key": "workspaces/<slug>/chat/uploads/<id>/report.pdf", "max_chars": 200000}
// reply
{"status": "success", "markdown": "...", "truncated": false, "ocr": false, "seconds": 21.4}
{"status": "error", "error": "Document converted to empty content"}
```

Lambda caps a synchronous reply at 6 MB, so Markdown is cut at `max_chars` (default 1,000,000).
universal-chat uploads the attachment to that key, invokes the function and deletes the object
afterwards, so the role also needs `s3:GetObject` on `workspaces/*/chat/uploads/*`, and the chat
servers need `lambda:InvokeFunction` plus Put/Delete on that prefix.

## Output: what llm-backend expects

The output is the same DoclingDocument JSON the Infochat uploader produces itself, so
llm-backend's `/embed` chunks it by section and cites it:

- named `<name>.docling.json` (the suffix is how the backend recognises it), schema 1.10.0
- embedded images stripped and keys sorted, so the same input always gives the same bytes
- `origin.uri` set to the link the backend cites, taken from, in order: `source_url` in a direct
  message, the source object's `source-url` metadata (`aws s3 cp ... --metadata source-url=https://...`),
  or the canonical / og:url / "saved from" address inside a saved HTML page
- a document that converts to no text and no tables fails instead of writing an empty file

llm-backend only loads documents whose schema version is no newer than its own `docling-core`,
so `docling-core` is pinned in `requirements.txt`. Upgrade it here only after the backend.
`OUTPUT_FORMAT=markdown` restores the old `.md` output.

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `OUTPUT_BUCKET` | source bucket | Where output is written |
| `OUTPUT_PREFIX` | `converted/` | Output key prefix (`<prefix><source key>.docling.json`) |
| `OUTPUT_FORMAT` | `docling` | `docling`: DoclingDocument JSON; `markdown`: `.md` |
| `RESULT_QUEUE_URL` | unset | Publish results to this SQS queue (required for the uploader) |
| `MAX_RECEIVE_COUNT` | `3` | Attempt number on which a failure is published as an error result; match the queue's redrive `maxReceiveCount` |
| `OCR_MODE` | `auto` | `auto`: OCR only PDFs where >10% of pages lack a text layer (scans), plus images. `always` / `never` force it. Skipping OCR is ~4× faster on text PDFs but drops text that exists only inside embedded pictures |
| `PRELOAD_MODELS` | `false` | Load PDF models during init instead of first PDF |
| `LOG_LEVEL` | `INFO` | |

If the source and output buckets are the same, objects already under `OUTPUT_PREFIX` are skipped. This prevents an S3 notification loop.

## Current deployment (us-east-1, account 727646498592)

| Piece | Name |
|---|---|
| Workspace prefix | `it-bot-temp-yrqyppvuby8lv1qq/` (in both buckets) |
| Input bucket | `docling-upload-bucket`. `s3:ObjectCreated:*` under the workspace prefix → SQS |
| Output bucket | `docling-md-workspace-bucket` (`OUTPUT_PREFIX=""`, so `<ws>/file.pdf` → `<ws>/file.md` with v3, `<ws>/file.docling.json` once this code is deployed) |
| Queue | `docling-md-queue` (visibility 5400 s; after 3 failed receives → `docling-md-queue-dlq`) |
| Lambda | `docling-md-converter` (arm64, 10240 MB, 900 s, 4 GB /tmp, batch size 1, ReportBatchItemFailures) |
| IAM role | `docling-md-converter-role` (basic logs + Get on upload, Put on md, consume queue) |
| ECR | `docling-conversion-image` (tags `v1`–`v3`; Lambda runs `v3`, which still writes Markdown) |

```bash
aws s3 cp report.pdf s3://docling-upload-bucket/it-bot-temp-yrqyppvuby8lv1qq/report.pdf
# ~1-2 min later:
aws s3 cp s3://docling-md-workspace-bucket/it-bot-temp-yrqyppvuby8lv1qq/report.md -   # report.docling.json after redeploy
aws logs tail /aws/lambda/docling-md-converter --follow
```

## Managing it from here

All commands run from this folder with AWS CLI credentials for account 727646498592.

```bash
./scripts/status.sh                          # Lambda state, queue + DLQ depth, errors (24 h), newest outputs
PREFIX=<workspace>/ ./scripts/status.sh      # outputs for one workspace prefix
aws logs tail /aws/lambda/docling-md-converter --follow

# Poison files land in the DLQ after 3 failed attempts. Inspect, then send them back for another try:
aws sqs receive-message --queue-url https://sqs.us-east-1.amazonaws.com/727646498592/docling-md-queue-dlq --max-number-of-messages 10
aws sqs start-message-move-task --source-arn arn:aws:sqs:us-east-1:727646498592:docling-md-queue-dlq

# Redeploy after code changes (bump the tag; the Lambda currently runs v3)
TAG=v4 FUNCTION_NAME=docling-md-converter ./scripts/push_to_ecr.sh
```

The AWS resources in the table above were created by hand, not by code in this folder.
`rapidocr` is pinned in `requirements.txt`: 3.10.0 breaks docling 2.133.0's model download, so
an unpinned rebuild fails.

## Local testing

You need Docker only. LocalStack provides S3 and SQS. `sqs-poller` stands in for Lambda's SQS event source mapping. The Lambda container runs under the Runtime Interface Emulator, which is built into the AWS base image.

```bash
docker compose up -d --build                 # first build ~5-10 min (image ~5.5 GB)
./scripts/local_test.sh samples/sample.pdf   # upload -> S3 notif -> SQS -> Lambda -> S3
./scripts/local_test.sh path/to/any.docx

# Check the output works in llm-backend: embed it into a backend test workspace, ask a
# question, print the cited sources, then remove the document again
WORKSPACE=<test-slug> ./scripts/check_backend.sh samples/sample.pdf "What is Docling?"

# Invoke the Lambda directly with an SQS-shaped event (object must exist in LocalStack)
./scripts/invoke_direct.sh events/sqs_s3_notification.json

docker compose logs -f docling-lambda sqs-poller
docker compose exec localstack awslocal s3 ls s3://docling-output/converted/ --recursive
docker compose down
```

LocalStack only sends S3 notifications for `uploads/` in `docling-input` (what `local_test.sh`
writes), so other objects there, such as uploader state files in an end-to-end test, don't trigger
conversions. To test a direct job, upload an original elsewhere and send the job yourself:

```bash
docker compose exec -T localstack awslocal sqs send-message \
  --queue-url http://localhost:4566/000000000000/docling-jobs \
  --message-body '{"bucket":"docling-input","key":"workspaces/s/uploader/originals/K/page","output_bucket":"docling-input","output_key":"workspaces/s/uploader/converted/K/page.docling.json","source_url":"https://example.edu/page","scraped":true,"context":{"key":"K"}}'
docker compose exec -T localstack awslocal sqs receive-message --wait-time-seconds 20 \
  --queue-url http://localhost:4566/000000000000/docling-results
```

After changing `app/handler.py`, run `docker compose up -d --build`. Only the last layer rebuilds.

### Tests

`tests/` runs inside the image against fake S3/SQS clients (no network apart from installing
pytest into the throwaway container; the Lambda image itself doesn't include pytest):

```bash
docker compose build docling-lambda
docker compose run --rm --no-deps -v ./tests:/var/task/tests --entrypoint sh docling-lambda \
  -c 'pip install -q --root-user-action=ignore --disable-pip-version-check pytest && python -m pytest tests -q -p no:cacheprovider'
```

## Deploy to AWS

1. **Push the image** (needs AWS CLI credentials):
   ```bash
   AWS_REGION=us-east-1 ./scripts/push_to_ecr.sh
   ```
   The default is `PLATFORM=linux/arm64`, so create the function with the **arm64** architecture. For x86_64, set `PLATFORM=linux/amd64`. That build is slow under emulation on Apple Silicon.

2. **Create the Lambda** from the ECR image. Recommended settings:
   - Memory **8192–10240 MB**. vCPUs scale with memory, and docling is CPU-bound.
   - Timeout **900 s**
   - Ephemeral storage (`/tmp`) sized for your largest input, e.g. 2048 MB+
   - Env: `OUTPUT_BUCKET`, optional `RESULT_QUEUE_URL`

3. **IAM role**: `AWSLambdaBasicExecutionRole` plus:
   - `s3:GetObject` on input, `s3:PutObject` on output
   - `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` on the jobs queue
   - `sqs:SendMessage` on the results queue (if used)

4. **SQS trigger**: add the jobs queue as an event source with **batch size 1** and **ReportBatchItemFailures** enabled. Set the queue's visibility timeout ≥ the function timeout (AWS recommends 6×). Add a DLQ for poison files.

5. **S3 → SQS** (optional): configure an `s3:ObjectCreated:*` notification on the input bucket targeting the jobs queue.

To redeploy later: `FUNCTION_NAME=<name> ./scripts/push_to_ecr.sh`.

## Performance notes (measured locally, 8 vCPU arm64)

- Init (imports): ~5.5 s, under Lambda's 10 s init limit.
- HTML/DOCX/MD-style formats: milliseconds.
- PDF: ~10 s/page with OCR + table structure (the 9-page docling paper took ~90 s). `OCR_MODE=auto` (default) skips OCR on born-digital PDFs: ~20–25 s instead of ~80–95 s for the same file. A single invocation is capped at 15 min, so very long PDFs need splitting or another compute target (ECS/Batch).

## Supported formats

Formats docling handles natively: PDF, DOCX, PPTX, XLSX, HTML, images (PNG/JPG/TIFF/…), Markdown, AsciiDoc, CSV, LaTeX, EPUB, email, and several XML schemas. Legacy `.doc/.ppt/.rtf` and audio/video need extra tools (LibreOffice / Whisper) that aren't installed. Those formats fail and are reported in `batchItemFailures`.

## Layout

```
Dockerfile               Lambda base image + CPU torch + docling + baked models
requirements.txt         pinned docling / boto3
app/handler.py           Lambda handler
tests/                   pytest suite (run inside the image, see Local testing)
docker-compose.yml       LocalStack + Lambda RIE + SQS poller
scripts/localstack-init.sh   creates buckets/queues/notification locally
scripts/sqs_poller.py    local event-source-mapping emulator
scripts/local_test.sh    end-to-end local test
scripts/invoke_direct.sh POST an event file to the Lambda RIE
scripts/push_to_ecr.sh   build + push to ECR (+ optional function update)
scripts/status.sh        read-only health check of the deployed pipeline
scripts/check_backend.sh embed a local conversion in llm-backend and check the citation
events/                  sample SQS events
samples/                 sample inputs
```
