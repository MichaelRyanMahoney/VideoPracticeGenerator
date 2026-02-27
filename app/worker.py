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
import concurrent.futures
from pathlib import Path

import boto3
from botocore.exceptions import NoCredentialsError

from .aws_pipeline import submit_full_job


def main():
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    queue_url = (os.environ.get("VPG_SQS_QUEUE_URL") or "").strip()
    worker_concurrency = max(1, int(os.environ.get("VPG_WORKER_CONCURRENCY") or "1"))
    visibility_timeout = max(60, int(os.environ.get("VPG_SQS_VISIBILITY_TIMEOUT") or "3600"))
    max_msgs = min(10, worker_concurrency)
    if not queue_url:
        raise SystemExit("Missing VPG_SQS_QUEUE_URL")

    sqs = boto3.client("sqs", region_name=region)
    s3 = boto3.client("s3", region_name=region)
    project_root = Path(__file__).resolve().parents[1]
    data_dir = Path(os.environ.get("VPG_DATA_DIR", "./data")).resolve()
    (data_dir / "jobs").mkdir(parents=True, exist_ok=True)

    def _s3_parse(uri: str) -> tuple[str, str]:
        if not (isinstance(uri, str) and uri.startswith("s3://")):
            raise ValueError(f"Expected s3:// URI, got: {uri}")
        no = uri[5:]
        bucket, key = no.split("/", 1)
        return bucket, key

    def _download_s3_to_path(uri: str, dst: Path) -> Path:
        b, k = _s3_parse(uri)
        dst.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(b, k, str(dst))
        return dst

    def _process_message(msg: dict) -> None:
        body = json.loads(msg.get("Body") or "{}")
        project_id = body["projectId"]
        job_id = body["jobId"]

        # Preferred portable payload (S3-first): allows fan-out across many workers.
        script_s3 = (body.get("scriptS3") or "").strip()
        gen_s3 = (body.get("generatorInputsS3") or "").strip()
        if script_s3 and gen_s3:
            work_dir = data_dir / "jobs" / job_id / "_queue_inputs"
            script_path = _download_s3_to_path(script_s3, work_dir / "script.txt")
            gen_path = _download_s3_to_path(gen_s3, work_dir / "generator_inputs.json")
        elif (body.get("localScriptPath") and body.get("localGeneratorInputsPath")):
            # Backward-compatible fallback for older queue payloads.
            script_path = Path(str(body["localScriptPath"]))
            gen_path = Path(str(body["localGeneratorInputsPath"]))
        else:
            raise RuntimeError(
                "Invalid SQS message body: expected scriptS3+generatorInputsS3 (preferred) "
                "or localScriptPath+localGeneratorInputsPath (legacy). "
                f"Got keys={sorted(list(body.keys()))}"
            )
        submit_full_job(project_root, project_id, job_id, script_path, gen_path)

    while True:
        try:
            resp = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=max_msgs,
                WaitTimeSeconds=20,
                VisibilityTimeout=visibility_timeout,
            )
        except NoCredentialsError:
            # Common first-run misconfig: instance has no IAM role / metadata not reachable from container.
            print("[worker] No AWS credentials available (NoCredentialsError). Attach an IAM role to the EC2 instance and/or enable IMDS access. Retrying in 10s...")
            time.sleep(10)
            continue
        msgs = resp.get("Messages") or []
        if not msgs:
            continue
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_concurrency) as ex:
            fut_to_msg = {ex.submit(_process_message, m): m for m in msgs}
            for fut in concurrent.futures.as_completed(fut_to_msg):
                msg = fut_to_msg[fut]
                receipt = msg["ReceiptHandle"]
                project_id = ""
                job_id = ""
                try:
                    body = json.loads(msg.get("Body") or "{}")
                    project_id = body.get("projectId") or ""
                    job_id = body.get("jobId") or ""
                    fut.result()
                    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
                except Exception as exn:
                    # Hard failure: record it and delete the message to avoid infinite re-processing loops.
                    # (Retries should be handled via explicit re-submit or an SQS DLQ policy.)
                    try:
                        from .aws_pipeline import load_aws_config, job_paths, write_status

                        if project_id and job_id:
                            cfg = load_aws_config()
                            paths = job_paths(cfg, project_id, job_id)
                            write_status(s3, paths["status"], job_id, "failed", {"error": str(exn)})
                    except Exception:
                        pass
                    print("[worker] error:", exn)
                    try:
                        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
                    except Exception:
                        pass
                    time.sleep(1)


if __name__ == "__main__":
    main()

