"""Local stand-in for Lambda's SQS event source mapping.

Long-polls an SQS queue, wraps messages in the same event shape Lambda
delivers, invokes the Lambda RIE container, and deletes messages that
weren't reported in batchItemFailures. Not used in AWS.
"""

import json
import os
import time
import urllib.request

import boto3

QUEUE_URL = os.environ["QUEUE_URL"]
LAMBDA_URL = os.environ["LAMBDA_URL"]
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "1"))
REGION = os.environ.get("AWS_REGION", "us-east-1")
QUEUE_ARN = f"arn:aws:sqs:{REGION}:000000000000:{QUEUE_URL.rsplit('/', 1)[-1]}"

sqs = boto3.client("sqs")


def to_lambda_record(msg: dict) -> dict:
    return {
        "messageId": msg["MessageId"],
        "receiptHandle": msg["ReceiptHandle"],
        "body": msg["Body"],
        "attributes": msg.get("Attributes", {}),
        "messageAttributes": msg.get("MessageAttributes", {}),
        "md5OfBody": msg["MD5OfBody"],
        "eventSource": "aws:sqs",
        "eventSourceARN": QUEUE_ARN,
        "awsRegion": REGION,
    }


def invoke(event: dict) -> dict:
    req = urllib.request.Request(
        LAMBDA_URL, data=json.dumps(event).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=960) as resp:
        return json.loads(resp.read() or b"{}")


def main() -> None:
    print(f"Polling {QUEUE_URL} -> {LAMBDA_URL}", flush=True)
    while True:
        try:
            msgs = sqs.receive_message(
                QueueUrl=QUEUE_URL, MaxNumberOfMessages=BATCH_SIZE, WaitTimeSeconds=10,
                AttributeNames=["All"],
            ).get("Messages", [])
        except Exception as exc:
            print(f"receive failed: {exc}", flush=True)
            time.sleep(3)
            continue
        if not msgs:
            continue

        print(f"Invoking Lambda with {len(msgs)} message(s)", flush=True)
        try:
            response = invoke({"Records": [to_lambda_record(m) for m in msgs]})
        except Exception as exc:
            print(f"invoke failed, messages will be retried: {exc}", flush=True)
            continue
        print(json.dumps(response, indent=2), flush=True)

        if "errorMessage" in response:
            continue  # whole batch failed; let visibility timeout redeliver
        failed = {f["itemIdentifier"] for f in response.get("batchItemFailures", [])}
        for m in msgs:
            if m["MessageId"] not in failed:
                sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=m["ReceiptHandle"])


if __name__ == "__main__":
    main()
