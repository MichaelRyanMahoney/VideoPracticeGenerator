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
from botocore.exceptions import NoCredentialsError

from .aws_pipeline import submit_full_job


def main():
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    queue_url = (os.environ.get("VPG_SQS_QUEUE_URL") or "").strip()
    if not queue_url:
        raise SystemExit("Missing VPG_SQS_QUEUE_URL")

    sqs = boto3.client("sqs", region_name=region)
    project_root = Path(__file__).resolve().parents[1]

    while True:
        try:
            resp = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=20,
                VisibilityTimeout=600,
            )
        except NoCredentialsError:
            # Common first-run misconfig: instance has no IAM role / metadata not reachable from container.
            print("[worker] No AWS credentials available (NoCredentialsError). Attach an IAM role to the EC2 instance and/or enable IMDS access. Retrying in 10s...")
            time.sleep(10)
            continue
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
            # Hard failure: record it and delete the message to avoid infinite re-processing loops.
            # (Retries should be handled via explicit re-submit or an SQS DLQ policy.)
            try:
                from .aws_pipeline import load_aws_config, job_paths, write_status

                cfg = load_aws_config()
                s3 = boto3.client("s3", region_name=cfg.region)
                paths = job_paths(cfg, project_id, job_id)
                write_status(s3, paths["status"], job_id, "failed", {"error": str(ex)})
            except Exception:
                pass
            print("[worker] error:", ex)
            try:
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
            except Exception:
                pass
            time.sleep(2)


if __name__ == "__main__":
    main()

