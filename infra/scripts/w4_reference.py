"""Download pinned official reference files in an Import Pod and invoke the existing job."""

import argparse
import hashlib
import json
import os
import sys
from contextlib import closing
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

MODULES = {
    "places": ["jobs.import_place_boundaries"],
    "roads": ["jobs.import_topis_geometry", "--allow-excluded-link", "1000020300"],
    "sensors": ["jobs.import_sensor_locations"],
}


def prepare(manifest_path, directory):
    manifest = json.loads(manifest_path.read_text())
    directory.mkdir(parents=True, exist_ok=True)
    with closing(
        boto3.client(
            "s3",
            region_name=manifest["region"],
            config=Config(
                connect_timeout=5,
                read_timeout=30,
                retries={"mode": "standard", "total_max_attempts": 3},
            ),
        )
    ) as client:
        for file in manifest["files"]:
            name = file["name"]
            if Path(name).name != name or not file["key"].startswith("reference/"):
                raise ValueError("invalid_reference_path")
            path = directory / name
            client.download_file(manifest["bucket"], file["key"], str(path))
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if path.stat().st_size != file["bytes"] or digest != file["sha256"]:
                raise ValueError("reference_integrity_failed")
            print(
                json.dumps(
                    {
                        "check": "reference_download",
                        "name": name,
                        "sha256": digest,
                        "status": "pass",
                    }
                ),
                flush=True,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=MODULES)
    parser.add_argument("--manifest", type=Path, default=Path("/work/reference-manifest.json"))
    args = parser.parse_args()
    try:
        prepare(args.manifest, Path("/app/data"))
    except Exception as error:
        print(
            json.dumps(
                {
                    "check": "reference_download",
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "aws_code": error.response["Error"]["Code"]
                    if isinstance(error, ClientError)
                    else None,
                }
            ),
            flush=True,
        )
        return 1
    os.execv(sys.executable, [sys.executable, "-u", "-m", *MODULES[args.kind]])


if __name__ == "__main__":
    sys.exit(main())
