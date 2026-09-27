"""
Local viewer application for Amazon Connect caller intake records.
Fetches JSON files from S3 and presents them in a searchable, downloadable web interface.

Usage:
    pip install flask boto3
    python viewer/app.py --bucket connect-caller-intake
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config

from flask import Flask, Response, abort, render_template, request
import boto3
from botocore.exceptions import ClientError, NoCredentialsError

app = Flask(__name__)


def _s3_client():
    return boto3.client("s3", region_name=config.AWS_REGION)


def list_all_records(bucket, s3):
    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=config.S3_PREFIX)
    keys = []
    for page in pages:
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".json"):
                keys.append(obj["Key"])
    return keys


def get_record(bucket, key, s3):
    resp = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(resp["Body"].read().decode("utf-8"))


def search_records(records, query):
    q = query.lower()
    return [r for r in records if any(q in str(v).lower() for v in r.values())]


@app.route("/")
def index():
    s3 = _s3_client()
    try:
        keys = list_all_records(config.S3_BUCKET, s3)
    except NoCredentialsError:
        return render_template("error.html", message="AWS credentials not configured."), 500
    except ClientError as e:
        return render_template("error.html", message=f"S3 access error: {e.response['Error']['Message']}"), 500

    records = []
    for key in sorted(keys, reverse=True):
        try:
            rec = get_record(config.S3_BUCKET, key, s3)
            rec["_s3_key"] = key
            records.append(rec)
        except Exception:
            continue

    query = request.args.get("q", "").strip()
    if query:
        records = search_records(records, query)

    return render_template("index.html", records=records, query=query, bucket=config.S3_BUCKET, total=len(records))


@app.route("/record/<path:key>")
def view_record(key):
    s3 = _s3_client()
    try:
        rec = get_record(config.S3_BUCKET, key, s3)
    except ClientError:
        abort(404)
    return render_template("detail.html", record=rec, key=key)


@app.route("/download/<path:key>")
def download_record(key):
    s3 = _s3_client()
    try:
        obj = s3.get_object(Bucket=config.S3_BUCKET, Key=key)
        body = obj["Body"].read()
    except ClientError:
        abort(404)
    filename = key.split("/")[-1]
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/download-all")
def download_all():
    s3 = _s3_client()
    keys = list_all_records(config.S3_BUCKET, s3)
    all_records = []
    for key in sorted(keys, reverse=True):
        try:
            rec = get_record(config.S3_BUCKET, key, s3)
            all_records.append(rec)
        except Exception:
            continue
    body = json.dumps(all_records, indent=2)
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": 'attachment; filename="all_calls.json"'},
    )


def main():
    parser = argparse.ArgumentParser(description="Local viewer for Connect caller intake records")
    parser.add_argument("--bucket", default=config.S3_BUCKET, help=f"S3 bucket name (default: {config.S3_BUCKET})")
    parser.add_argument("--port", type=int, default=config.VIEWER_PORT, help=f"Port (default: {config.VIEWER_PORT})")
    parser.add_argument("--region", default=config.AWS_REGION, help=f"AWS region (default: {config.AWS_REGION})")
    parser.add_argument("--host", default=config.VIEWER_HOST, help=f"Host (default: {config.VIEWER_HOST})")
    parser.add_argument("--debug", action="store_true", default=config.VIEWER_DEBUG, help="Flask debug mode")
    args = parser.parse_args()
    config.S3_BUCKET = args.bucket
    config.AWS_REGION = args.region
    print(f"Starting viewer on http://{args.host}:{args.port} for bucket s3://{config.S3_BUCKET}")
    print("Press Ctrl+C to stop.\n")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
