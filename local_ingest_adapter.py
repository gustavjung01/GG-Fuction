#!/usr/bin/env python3
"""Adapter for local scraping tools.

Reads JSON from stdin or a file and sends it to Cloud Run /ingest-text.
It accepts common local-tool field names and maps them to the Cloud Run format.
No Facebook cookies, login, commenting, or messaging is implemented here.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

CONTENT_KEYS = ("content", "text", "message", "post_text", "postText", "body", "caption", "raw_text")
URL_KEYS = ("url", "source_url", "sourceUrl", "post_url", "postUrl", "link", "href")
AUTHOR_KEYS = ("author", "user", "username", "author_name", "authorName", "name", "poster")


def first_string(data, keys):
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        value = str(value).strip()
        if value:
            return value
    return ""


def normalize_payload(data, min_score):
    return {
        "url": first_string(data, URL_KEYS),
        "author": first_string(data, AUTHOR_KEYS),
        "content": first_string(data, CONTENT_KEYS),
        "min_score": int(data.get("min_score", min_score) or min_score),
    }


def send(service_url, token, payload, timeout):
    url = service_url.rstrip("/") + "/ingest-text"
    if token:
        url += "?" + urllib.parse.urlencode({"token": token})
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-url", default=os.getenv("SERVICE_URL", "https://fb-lead-scanner-638713993935.asia-southeast1.run.app"))
    parser.add_argument("--token", default=os.getenv("REVIEW_TOKEN", ""))
    parser.add_argument("--file", default="")
    parser.add_argument("--min-score", type=int, default=int(os.getenv("INGEST_MIN_SCORE", "55")))
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    raw = open(args.file, "r", encoding="utf-8").read() if args.file else sys.stdin.read()
    data = json.loads(raw)
    if isinstance(data, list):
        results = []
        exit_code = 0
        for item in data:
            payload = normalize_payload(item, args.min_score)
            status, body = send(args.service_url, args.token, payload, args.timeout)
            results.append({"http_status": status, "response": json.loads(body) if body.strip().startswith("{") else body})
            if not (200 <= status < 300):
                exit_code = 1
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return exit_code

    payload = normalize_payload(data, args.min_score)
    status, body = send(args.service_url, args.token, payload, args.timeout)
    print(body)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
