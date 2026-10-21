#!/usr/bin/env python3
"""Download Project Gutenberg books and stage them in a public S3 bucket.

Usage:
    uv run python src/s3_lab/stage_books.py book_urls.txt index.html public-mst3k

Arguments (in order):
    1. path of the book list file (one URL per line)
    2. path of the index page to upload as index.html
    3. name of the bucket to create and make public

AWS credentials are read from ~/.aws/credentials (never put them in this file).
"""

import json
import logging
import os
import sys
import time

import boto3
import requests
from botocore.exceptions import ClientError

# Log messages with a timestamp and level
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# One shared, signed S3 client (uses credentials from ~/.aws/credentials)
s3 = boto3.client("s3", region_name="us-east-1")


def parse_args():
    """Read command line arguments.

    Returns:
        tuple: (list_file, index_path, bucket) as strings.
    """
    if len(sys.argv) < 4:
        log.error("Usage: stage_books.py BOOK_LIST_FILE INDEX_FILE BUCKET_NAME")
        sys.exit(1)
    return sys.argv[1], sys.argv[2], sys.argv[3]


def download_books(list_file, dest_dir):
    """Download every book URL in list_file into dest_dir.

    Books that already exist locally are skipped, so the script can be rerun.

    Args:
        list_file (str): path to a text file with one URL per line.
        dest_dir (str): local folder to save the books in.

    Returns:
        list: local paths of all books available in dest_dir.
    """
    os.makedirs(dest_dir, exist_ok=True)

    # Read the URLs, skipping blank lines
    try:
        with open(list_file, "r", encoding="utf-8") as f:
            urls = [line.strip() for line in f if line.strip()]
    except OSError as e:
        log.error(f"Could not read {list_file}: {e}")
        return []

    log.info(f"Found {len(urls)} URLs in {list_file}")
    paths = []

    for i, url in enumerate(urls, start=1):
        # Save under the last part of the URL, e.g. books/pg1342.txt
        filename = url.split("/")[-1]
        path = os.path.join(dest_dir, filename)

        # Skip books we already have
        if os.path.exists(path):
            log.info(f"[{i}/{len(urls)}] {filename} already downloaded, skipping")
            paths.append(path)
            continue

        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()  # raise an error for 404 etc.
            with open(path, "wb") as out:
                out.write(response.content)
            log.info(f"[{i}/{len(urls)}] Downloaded {filename}")
            paths.append(path)
        except requests.RequestException as e:
            log.error(f"Error downloading {url}: {e}")
        except OSError as e:
            log.error(f"Error saving {path}: {e}")

        # Be polite to Project Gutenberg's servers
        time.sleep(1)

    log.info(f"{len(paths)} of {len(urls)} books available locally")
    return paths


def create_bucket(bucket):
    """Create the bucket unless it already exists and is accessible.

    Args:
        bucket (str): bucket name.

    Returns:
        bool: True if the bucket exists afterwards, False otherwise.
    """
    # head_bucket succeeds only if the bucket exists and we can access it
    try:
        s3.head_bucket(Bucket=bucket)
        log.info(f"Bucket {bucket} already exists, reusing it")
        return True
    except ClientError:
        log.info(f"Bucket {bucket} not found, creating it")

    # In us-east-1 do NOT pass CreateBucketConfiguration
    try:
        s3.create_bucket(Bucket=bucket)
        log.info(f"Created bucket {bucket}")
        return True
    except ClientError as e:
        log.error(f"Could not create bucket {bucket}: {e}")
        return False


def make_public(bucket):
    """Make all objects in the bucket publicly readable.

    Turns off Block Public Access first, then attaches a bucket policy
    that allows anyone to s3:GetObject. The order matters.

    Args:
        bucket (str): bucket name.

    Returns:
        bool: True on success, False otherwise.
    """
    try:
        # Step 1: turn off Block Public Access for this bucket
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": False,
                "IgnorePublicAcls": False,
                "BlockPublicPolicy": False,
                "RestrictPublicBuckets": False,
            },
        )
        log.info(f"Turned off Block Public Access for {bucket}")

        # Step 2: attach a policy that lets anyone read objects
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": f"arn:aws:s3:::{bucket}/*",
                }
            ],
        }
        s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
        log.info(f"Attached public-read bucket policy to {bucket}")
        return True
    except ClientError as e:
        log.error(f"Could not make {bucket} public: {e}")
        return False


def upload_files(bucket, file_paths):
    """Upload each file to the top level of the bucket.

    Args:
        bucket (str): bucket name.
        file_paths (list): local paths returned by download_books().

    Returns:
        int: number of files uploaded successfully.
    """
    count = 0
    for i, path in enumerate(file_paths, start=1):
        # Key is just the file name (pg1342.txt), not books/pg1342.txt
        key = os.path.basename(path)
        try:
            with open(path, "rb") as f:
                s3.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=f,
                    ContentType="text/plain; charset=utf-8",
                )
            log.info(f"[{i}/{len(file_paths)}] Uploaded {key}")
            count += 1
        except (ClientError, OSError) as e:
            log.error(f"Error uploading {path}: {e}")
    return count


def upload_index(bucket, index_path):
    """Upload the index page as index.html.

    Args:
        bucket (str): bucket name.
        index_path (str): local path of the index page.

    Returns:
        bool: True on success, False otherwise.
    """
    try:
        with open(index_path, "rb") as f:
            s3.put_object(
                Bucket=bucket,
                Key="index.html",
                Body=f,
                ContentType="text/html",
            )
        log.info("Uploaded index.html")
        return True
    except FileNotFoundError:
        log.error(f"Index file not found: {index_path}")
        return False
    except (ClientError, OSError) as e:
        log.error(f"Error uploading {index_path}: {e}")
        return False


def main():
    """Run the full staging workflow."""
    list_file, index_path, bucket = parse_args()

    # 1. Download the books locally
    paths = download_books(list_file, "books")
    if not paths:
        log.error("No books were downloaded, stopping")
        sys.exit(1)

    # 2. Create the bucket
    if not create_bucket(bucket):
        sys.exit(1)

    # 3. Make it publicly readable
    if not make_public(bucket):
        sys.exit(1)

    # 4. Upload books and the index page
    uploaded = upload_files(bucket, paths)
    index_ok = upload_index(bucket, index_path)

    # 5. Report the result
    if uploaded == len(paths) and index_ok:
        log.info(f"Success: uploaded {uploaded} books and index.html")
    else:
        log.warning(
            f"Finished with problems: {uploaded}/{len(paths)} books uploaded, "
            f"index uploaded: {index_ok}"
        )
    log.info(f"Public URL: https://{bucket}.s3.amazonaws.com/index.html")


if __name__ == "__main__":
    main()
