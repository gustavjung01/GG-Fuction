#!/usr/bin/env python3
"""Normalize local scraper JSON records for /ingest-text.

Usage:
  python tools/local_payload_mapper.py < raw_local_record.json
"""

import json
import sys

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


def normalize(data):
    return {
        "url": first_string(data, URL_KEYS),
        "author": first_string(data, AUTHOR_KEYS),
        "content": first_string(data, CONTENT_KEYS),
        "min_score": int(data.get("min_score", 55) or 55),
    }


raw = json.load(sys.stdin)
if isinstance(raw, list):
    print(json.dumps([normalize(item) for item in raw], ensure_ascii=False, indent=2))
else:
    print(json.dumps(normalize(raw), ensure_ascii=False, indent=2))
