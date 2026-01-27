"""
SQS-driven worker that runs on the CPU EC2 instance.

It pulls jobs from SQS and runs submit_full_job() which:
  - uploads inputs
  - builds manifest + audio cache
  - submits GPU Batch jobs

This is intentionally simple (one-process worker). You can run multiple replicas behind the same queue later.
"""

import json
import os
import time
from pathlib import Path

import boto3

from .aws_pipeline import submit_full_job


def main():
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    queue_url = (os.environ.get("VPG_SQS_QUEUE_URL") or "").strip()
    if not queue_url:
        raise SystemExit("Missing VPG_SQS_QUEUE_URL")

    sqs = boto3.client("sqs", region_name=region)
    project_root = Path(__file__).resolve().parents[1]

    while True:
        resp = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=600,
        )
        msgs = resp.get("Messages") or []
        if not msgs:
            continue
        msg = msgs[0]
        receipt = msg["ReceiptHandle"]
        try:
            body = json.loads(msg.get("Body") or "{}")
            project_id = body["projectId"]
            job_id = body["jobId"]
            script_path = Path(body["localScriptPath"])
            gen_path = Path(body["localGeneratorInputsPath"])
            submit_full_job(project_root, project_id, job_id, script_path, gen_path)
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
        except Exception as ex:
            # Leave message for retry by letting visibility timeout expire.
            print("[worker] error:", ex)
            time.sleep(5)


if __name__ == "__main__":
    main()

