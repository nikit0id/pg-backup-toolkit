"""Back up portfolio-api and publish an S3 manifest only after the dump is durable."""
import argparse
import gzip
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import boto3
import psycopg


def retention(keys):
    """Keep the newest entry in seven days, four ISO weeks and three months."""
    days, weeks, months = set(), set(), set()
    keep = set()
    for key in sorted(keys, reverse=True):
        stamp = datetime.strptime(Path(key).stem.split("_")[0], "%Y%m%dT%H%M%S%fZ")
        for buckets, period, limit in (
            (days, stamp.date(), 7),
            (weeks, stamp.isocalendar()[:2], 4),
            (months, (stamp.year, stamp.month), 3),
        ):
            if len(buckets) < limit and period not in buckets:
                buckets.add(period)
                keep.add(key)
    return keep


def manifests(s3, bucket, prefix):
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix + "/")
    return sorted(obj["Key"] for page in pages for obj in page.get("Contents", [])
                  if obj["Key"].endswith(".json"))


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def measure_success(now):
    if dest := os.getenv("METRICS_DIR"):
        directory = Path(dest)
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=directory, delete=False) as staged:
            staged.write(f"portfolio_pg_backup_last_success_timestamp_seconds {int(now.timestamp())}\n")
        os.chmod(staged.name, 0o644)
        os.replace(staged.name, directory / "pg_backup.prom")


def backup(s3, bucket, prefix, directory):
    now = datetime.now(timezone.utc)
    base = f"{prefix}/{now:%Y%m%dT%H%M%S%fZ}_{uuid4().hex}"
    archive = directory / "dump.sql.gz"
    with psycopg.connect("") as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot, rows = db.execute(
            "SELECT pg_export_snapshot(), (SELECT count(*) FROM public.tasks)"
        ).fetchone()
        with gzip.open(archive, "wb") as compressed:
            with subprocess.Popen(["pg_dump", "--no-owner", "--no-acl", "--snapshot", snapshot],
                                  stdout=subprocess.PIPE) as dump:
                shutil.copyfileobj(dump.stdout, compressed)
                if dump.wait():
                    raise RuntimeError("pg_dump failed")
    manifest = {"rows": rows, "sha256": checksum(archive)}
    s3.upload_file(str(archive), bucket, base + ".sql.gz")
    s3.put_object(Bucket=bucket, Key=base + ".json", Body=json.dumps(manifest).encode())
    keys = manifests(s3, bucket, prefix)
    for stale in set(keys) - retention(keys):
        s3.delete_object(Bucket=bucket, Key=stale)
        s3.delete_object(Bucket=bucket, Key=stale.removesuffix(".json") + ".sql.gz")
    measure_success(now)
    return f"backup {base}: rows={rows} sha256={manifest['sha256']}"


def restore(s3, bucket, prefix, directory):
    keys = manifests(s3, bucket, prefix)
    if not keys:
        raise RuntimeError("No completed backup in the configured prefix")
    key = keys[-1]
    manifest = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    archive = directory / "restore.sql.gz"
    s3.download_file(bucket, key.removesuffix(".json") + ".sql.gz", str(archive))
    if checksum(archive) != manifest["sha256"]:
        raise ValueError("Archive checksum mismatch")
    name = "portfolio-restore-" + uuid4().hex[:12]
    env = {**os.environ, "POSTGRES_PASSWORD": secrets.token_hex(32)}
    subprocess.run(["docker", "run", "--rm", "-d", "--name", name, "--network", "none",
                    "-e", "POSTGRES_PASSWORD", "postgres:17-alpine"], check=True, env=env,
                   stdout=subprocess.DEVNULL)
    try:
        subprocess.run(["docker", "exec", name, "sh", "-ec",
                        "for i in $(seq 1 60); do pg_isready -h 127.0.0.1 -U postgres && exit 0; sleep 1; done; exit 1"],
                       check=True, stdout=subprocess.DEVNULL)
        psql = ["docker", "exec", "-i", name, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "postgres"]
        with gzip.open(archive, "rb") as data, subprocess.Popen(psql, stdin=subprocess.PIPE,
                                                                stdout=subprocess.DEVNULL) as client:
            with client.stdin:
                shutil.copyfileobj(data, client.stdin)
            if client.wait():
                raise RuntimeError("SQL restore failed")
        rows = int(subprocess.check_output(psql + ["-Atc", "SELECT count(*) FROM public.tasks"], text=True))
        if rows != manifest["rows"]:
            raise ValueError(f"Restored {rows} rows, expected {manifest['rows']}")
        return f"restore verified: rows={rows} expected={manifest['rows']} sha256={manifest['sha256']}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=True, stdout=subprocess.DEVNULL)


def notify(message):
    if not (token := os.getenv("TELEGRAM_BOT_TOKEN")):
        return
    from urllib.parse import urlencode
    from urllib.request import urlopen
    body = urlencode({"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": message}).encode()
    with urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=10) as response:
        if not json.load(response).get("ok"):
            raise RuntimeError("Telegram delivery failed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["backup", "restore"])
    args = parser.parse_args()
    store = boto3.client("s3", endpoint_url=os.getenv("S3_ENDPOINT_URL") or None)
    try:
        with tempfile.TemporaryDirectory() as scratch:
            action = backup if args.action == "backup" else restore
            result = action(store, os.environ["S3_BUCKET"], os.getenv("S3_PREFIX", "portfolio"), Path(scratch))
    except Exception as exc:
        notify(f"portfolio {args.action} failed: {type(exc).__name__}")
        raise
    print(result)
    notify(result)


if __name__ == "__main__":
    main()
