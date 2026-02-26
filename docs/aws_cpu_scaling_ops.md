# AWS CPU Scaling Ops Guide

This guide hardens the CPU-only pipeline around SQS, EC2 Auto Scaling, and S3.

## 1) SQS + DLQ

- Create a DLQ (for example `vpg-jobs-dlq`).
- Attach a redrive policy to the main queue (for example `maxReceiveCount=3`).
- Keep long polling enabled (`ReceiveMessageWaitTimeSeconds=20`).
- Set message retention to at least 4 days for replay safety.

## 2) Worker Autoscaling Policy

- Run worker containers on an EC2 Auto Scaling Group.
- Scale on queue depth and age:
  - Scale out when `ApproximateNumberOfMessagesVisible` increases.
  - Scale out aggressively if `ApproximateAgeOfOldestMessage` exceeds SLA.
  - Scale in when both queue depth and oldest age are near zero.
- Use mixed instances with Spot preference and an On-Demand base capacity.

## 3) CloudWatch Alarms

Create alarms for:

- `SQS ApproximateAgeOfOldestMessage` (warning + critical).
- `SQS ApproximateNumberOfMessagesVisible` (backlog growth).
- Worker host CPU and memory saturation.
- Application-level job failures by status writes in S3 (`failed` count).

## 4) S3 Lifecycle Policy

Apply lifecycle rules by prefix:

- Expire frame shards under `projects/*/jobs/*/frames/` after short retention (for example 7-14 days).
- Keep final outputs under `projects/*/jobs/*/out/` longer (for example 90 days).
- Expire transient manifests and temp artifacts sooner than final MP4 outputs.

## 5) Runtime Knobs

- `VPG_WORKER_CONCURRENCY`: per-instance parallel job workers.
- `VPG_SQS_VISIBILITY_TIMEOUT`: must exceed worst-case single job runtime.
- `VPG_RENDER_SHARDS`: within-job shard fan-out.
- `VPG_S3_UPLOAD_WORKERS` / `VPG_S3_DOWNLOAD_WORKERS`: tune transfer concurrency.

Start conservative, then increase one knob at a time while tracking p95 completion time and error rate.

