"""OBS helper for fullchain verification — runs INSIDE the image container.

The image ships esdk-obs-python, so the host needs nothing but Docker.
Subcommands:
    info                 print object status/size (exit 1 if >=300)
    backup  --file PATH  download current object to PATH; prints EXISTS/MISSING
    replace --file PATH  deleteObject + putFile (two-step, obsfs-safe)
    delete               deleteObject

Credentials come from AccessKeyID / SecretAccessKey / OBS_BUCKET /
OBS_ENDPOINT environment variables.
"""
import argparse
import os
import sys

BUCKET_ENV = "OBS_BUCKET"
KEY_ENV = "OBS_KEY"


def _client():
    from obs import ObsClient
    return ObsClient(
        access_key_id=os.environ["AccessKeyID"],
        secret_access_key=os.environ["SecretAccessKey"],
        server=os.environ.get(
            "OBS_ENDPOINT", "https://obs.cn-north-4.myhuaweicloud.com"
        ),
        timeout=30,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "cmd", choices=["info", "backup", "replace", "delete"]
    )
    parser.add_argument("--bucket", default=os.environ.get(BUCKET_ENV, "xgb-bc-bucket"))
    parser.add_argument(
        "--key",
        default=os.environ.get(KEY_ENV, "models/xgboost_breast_cancer.json"),
    )
    parser.add_argument("--file", help="container-side path for backup/replace")
    args = parser.parse_args()

    client = _client()

    if args.cmd == "info":
        resp = client.getObjectMetadata(args.bucket, args.key)
        print(f"status={resp.status} size={getattr(resp, 'contentLength', None)}")
        sys.exit(0 if resp.status < 300 else 1)

    if args.cmd == "backup":
        resp = client.getObject(args.bucket, args.key, downloadPath=args.file)
        if resp.status == 404:
            if os.path.exists(args.file):
                os.remove(args.file)  # esdk may leave an empty file
            print("MISSING")
            sys.exit(0)
        if resp.status >= 300:
            print(f"FAILED status={resp.status}")
            sys.exit(1)
        print(f"EXISTS size={os.path.getsize(args.file)}")
        sys.exit(0)

    if args.cmd == "delete":
        client.deleteObject(args.bucket, args.key)
        print("deleted")
        sys.exit(0)

    # replace: delete first, then put (avoids obsfs mtime staleness)
    client.deleteObject(args.bucket, args.key)
    resp = client.putFile(args.bucket, args.key, args.file)
    print(f"put status={resp.status}")
    sys.exit(0 if resp.status < 300 else 1)


if __name__ == "__main__":
    main()
