#!/usr/bin/env python3
"""Local helper to send scraped text into the Cloud Run Lead Scanner.

This does not log in to Facebook, does not use cookies, and does not post/comment/message.
It only forwards text that a local tool already collected to /ingest-text.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_SERVICE_URL = os.getenv(
    "SERVICE_URL",
    "https://fb-lead-scanner-638713993935.asia-southeast1.run.app",
).rstrip("/")


def post_json(service_url: str, token: str, payload: dict, timeout: int) -> tuple[int, str]:
    query = urllib.parse.urlencode({"token": token}) if token else ""
    endpoint = f"{service_url.rstrip('/')}/ingest-text"
    if query:
        endpoint = f"{endpoint}?{query}"

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Send one text lead candidate to Cloud Run /ingest-text.")
    parser.add_argument("--service-url", default=DEFAULT_SERVICE_URL)
    parser.add_argument("--token", default=os.getenv("REVIEW_TOKEN", ""))
    parser.add_argument("--url", default="")
    parser.add_argument("--author", default="")
    parser.add_argument("--content", default="")
    parser.add_argument("--text", default="", help="Alias for --content.")
    parser.add_argument("--min-score", type=int, default=int(os.getenv("INGEST_MIN_SCORE", "55")))
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    content = (args.content or args.text or sys.stdin.read()).strip()
    if not content:
        print(json.dumps({"status": "error", "message": "content is required."}, ensure_ascii=False))
        return 2

    payload = {
        "url": args.url,
        "author": args.author,
        "content": content,
        "min_score": args.min_score,
    }
    status, text = post_json(args.service_url, args.token, payload, args.timeout)
    print(text)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
