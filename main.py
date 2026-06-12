import csv
import io
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from flask import Flask, Response, jsonify, request, render_template_string
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from lead_classifier import LeadClassifier
from storage import LeadStore

app = Flask(__name__)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

REVIEW_TOKEN = os.getenv("REVIEW_TOKEN", "").strip()
if not REVIEW_TOKEN:
    logger.warning("REVIEW_TOKEN is not set; dashboard and save routes are open for local development.")

DEFAULT_TARGET_URL = os.getenv("DEFAULT_TARGET_URL", "https://example.com")
SCAN_DEFAULT_URLS = [
    value.strip()
    for value in os.getenv("SCAN_DEFAULT_URLS", DEFAULT_TARGET_URL).split(",")
    if value.strip()
]
MAX_POSTS_DEFAULT = int(os.getenv("MAX_POSTS_DEFAULT", "20"))
SCAN_DELAY_SECONDS = float(os.getenv("SCAN_DELAY_SECONDS", "0"))
VERIFY_PROXY = os.getenv("VERIFY_PROXY", "false").lower() == "true"
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
)

PROXY_SERVER = os.getenv("PROXY_SERVER", "").strip()
PROXY_USERNAME = os.getenv("PROXY_USERNAME", "").strip()
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD", "").strip()

CLASSIFIER = LeadClassifier()
STORE = LeadStore()


def build_proxy_config() -> Optional[Dict[str, str]]:
    if not PROXY_SERVER:
        return None

    proxy: Dict[str, str] = {"server": PROXY_SERVER}
    if PROXY_USERNAME:
        proxy["username"] = PROXY_USERNAME
    if PROXY_PASSWORD:
        proxy["password"] = PROXY_PASSWORD
    return proxy


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_urls(urls: Optional[Iterable[str]]) -> List[str]:
    if not urls:
        return list(SCAN_DEFAULT_URLS)
    if isinstance(urls, str):
        urls = [urls]
    return [url.strip() for url in urls if isinstance(url, str) and url.strip()]


def parse_date_filter(raw: str) -> Optional[str]:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def parse_min_score(raw: Any) -> int:
    try:
        return max(0, min(100, int(raw)))
    except (TypeError, ValueError):
        return 0


def parse_positive_int(raw: Any, default: int, minimum: int = 1, maximum: int = 1000) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def parse_bool(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return default


def extract_token() -> str:
    return (request.args.get("token") or request.headers.get("X-Review-Token") or "").strip()


def token_required() -> bool:
    if not REVIEW_TOKEN:
        return False
    return extract_token() == REVIEW_TOKEN


def require_review_token():
    if REVIEW_TOKEN and not token_required():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    return None


class LeadScraper:
    def __init__(self, user_agent: str, proxy_config: Optional[Dict[str, str]] = None) -> None:
        self.user_agent = user_agent
        self.proxy_config = proxy_config

    def _launch(self):
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1366, "height": 768},
            proxy=self.proxy_config,
        )
        page = context.new_page()
        return playwright, browser, context, page

    @staticmethod
    def _close(playwright, browser, context) -> None:
        try:
            context.close()
        finally:
            try:
                browser.close()
            finally:
                playwright.stop()

    @staticmethod
    def _dedupe(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        output: List[Dict[str, Any]] = []
        for item in items:
            text = clean_text(item.get("text", ""))
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            item["text"] = text
            output.append(item)
        return output

    def _extract_text_blocks(self, page, include_comments: bool) -> List[Dict[str, Any]]:
        selectors = [
            'div[role="article"]',
            "article",
            "main article",
            'div[dir="auto"]',
        ]
        if include_comments:
            selectors.extend(
                [
                    'div[role="comment"]',
                    '[data-ad-preview="message"]',
                    "li",
                ]
            )

        blocks: List[Dict[str, Any]] = []
        for selector in selectors:
            for node in page.query_selector_all(selector):
                try:
                    text = clean_text(node.inner_text())
                except Exception:
                    continue
                if len(text) < 20:
                    continue
                blocks.append({"selector": selector, "text": text})
        return self._dedupe(blocks)

    def scrape(self, urls: Sequence[str], max_posts: int, include_comments: bool) -> Dict[str, Any]:
        results: List[Dict[str, Any]] = []
        visited_urls: List[str] = []
        ip_info: Optional[Dict[str, Any]] = None
        errors: List[Dict[str, str]] = []
        playwright, browser, context, page = self._launch()

        if VERIFY_PROXY:
            try:
                ip_info = self._verify_proxy_ip(page)
            except Exception as exc:
                ip_info = {"error": str(exc)}

        try:
            for index, url in enumerate(urls, start=1):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(5000)
                    for _ in range(2):
                        page.mouse.wheel(0, 2200)
                        page.wait_for_timeout(2500)

                    blocks = self._extract_text_blocks(page, include_comments=include_comments)
                    visited_urls.append(url)
                    for block in blocks[:max_posts]:
                        results.append(
                            {
                                "source_url": url,
                                "text": block["text"],
                                "selector": block.get("selector"),
                                "include_comments": include_comments,
                                "position": index,
                            }
                        )

                    if SCAN_DELAY_SECONDS > 0:
                        time.sleep(SCAN_DELAY_SECONDS)
                except Exception as exc:
                    errors.append({"url": url, "error": str(exc)})
        finally:
            self._close(playwright, browser, context)

        return {"items": results[:max_posts], "visited_urls": visited_urls, "proxy_ip": ip_info, "errors": errors}

    @staticmethod
    def _verify_proxy_ip(page) -> Dict[str, Any]:
        page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=30000)
        content = clean_text(page.text_content("body") or "")
        ip_match = re.search(r'"ip"\s*:\s*"([^"]+)"', content)
        return {"response": content, "ip": ip_match.group(1) if ip_match else None}


SCRAPER = LeadScraper(user_agent=USER_AGENT, proxy_config=build_proxy_config())


def classify_and_enrich(items: Sequence[Dict[str, Any]], save: bool = False) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    for item in items:
        text = clean_text(item.get("text", ""))
        if not text:
            continue
        classification = CLASSIFIER.classify(text)
        if not classification["is_lead"] or classification["intent"] != "borrower":
            continue

        lead_record = {
            "id": f"lead_{uuid.uuid4().hex}",
            "created_at": now_iso(),
            "source_url": item.get("source_url", ""),
            "text": text,
            "score": classification["score"],
            "intent": classification["intent"],
            "reasons": classification["reasons"],
            "matched_keywords": classification["matched_keywords"],
            "suggested_comments": CLASSIFIER.suggest_comments(text),
        }
        enriched.append(lead_record)
    if save and enriched:
        STORE.append_leads(enriched)
    return enriched


def build_scan_response(urls: Sequence[str], max_posts: int, include_comments: bool, save: bool = False) -> Dict[str, Any]:
    scraped = SCRAPER.scrape(urls=urls, max_posts=max_posts, include_comments=include_comments)
    leads = classify_and_enrich(scraped["items"], save=save)
    response: Dict[str, Any] = {
        "status": "success",
        "visited_urls": scraped["visited_urls"],
        "proxy_ip": scraped["proxy_ip"],
        "errors": scraped["errors"],
        "scanned_count": len(scraped["items"]),
        "lead_count": len(leads),
        "leads": leads,
        "summary": {
            "borrower_leads": len(leads),
            "saved_leads": len(leads) if save else 0,
            "error_count": len(scraped["errors"]),
        },
    }
    if save:
        response["saved_count"] = len(leads)
    return response


def load_dashboard_leads() -> List[Dict[str, Any]]:
    leads = STORE.load_all_leads()
    return sorted(leads, key=lambda item: str(item.get("created_at", "")), reverse=True)


def filter_dashboard_leads(leads: Sequence[Dict[str, Any]], min_score: int, date_filter: Optional[str]) -> List[Dict[str, Any]]:
    filtered = []
    for lead in leads:
        try:
            score = int(lead.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        if score < min_score:
            continue
        created_at = str(lead.get("created_at", ""))
        if date_filter and not created_at.startswith(date_filter):
            continue
        filtered.append(lead)
    return sorted(filtered, key=lambda item: str(item.get("created_at", "")), reverse=True)


@app.route("/", methods=["GET"])
def root() -> Any:
    return jsonify(
        {
            "status": "ok",
            "dashboard": "/dashboard",
            "health": "/health",
            "scan": "/scan",
            "scan_save": "/scan-save",
            "export": "/export.csv",
            "proxy_enabled": bool(PROXY_SERVER),
        }
    )


@app.route("/health", methods=["GET"])
def health() -> Any:
    return jsonify({"status": "ok"})


@app.route("/scan", methods=["POST"])
def scan() -> Any:
    data = request.get_json(silent=True) or {}
    urls = normalize_urls(data.get("urls"))
    max_posts = parse_positive_int(data.get("max_posts", MAX_POSTS_DEFAULT), MAX_POSTS_DEFAULT)
    include_comments = parse_bool(data.get("include_comments", False))
    try:
        return jsonify(build_scan_response(urls, max_posts, include_comments, save=False))
    except PlaywrightTimeoutError:
        return jsonify({"status": "error", "message": "Timeout while loading page."}), 504
    except Exception as exc:
        logger.exception("scan failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/scan-save", methods=["POST"])
def scan_save() -> Any:
    auth = require_review_token()
    if auth:
        return auth

    data = request.get_json(silent=True) or {}
    urls = normalize_urls(data.get("urls"))
    max_posts = parse_positive_int(data.get("max_posts", MAX_POSTS_DEFAULT), MAX_POSTS_DEFAULT)
    include_comments = parse_bool(data.get("include_comments", False))

    try:
        response = build_scan_response(urls, max_posts, include_comments, save=True)
        return jsonify(response)
    except PlaywrightTimeoutError:
        return jsonify({"status": "error", "message": "Timeout while loading page."}), 504
    except Exception as exc:
        logger.exception("scan-save failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/suggest-comments", methods=["POST"])
def suggest_comments() -> Any:
    data = request.get_json(silent=True) or {}
    leads = data.get("leads", data.get("texts", []))

    if not isinstance(leads, list):
        return jsonify({"status": "error", "message": "leads must be a list."}), 400

    suggestions = []
    for index, lead in enumerate(leads, start=1):
        if isinstance(lead, dict):
            text = str(lead.get("text", "") or "").strip()
        else:
            text = str(lead or "").strip()
        if not text:
            continue
        suggestions.append(
            {
                "lead_index": index,
                "lead_preview": clean_text(text)[:240],
                "suggested_comments": CLASSIFIER.suggest_comments(text),
            }
        )

    return jsonify({"status": "success", "suggestions": suggestions})


@app.route("/auto-comment", methods=["POST"])
def auto_comment() -> Any:
    return jsonify(
        {
            "status": "disabled",
            "message": "Automatic Facebook commenting is disabled.",
        }
    ), 501


@app.route("/dashboard", methods=["GET"])
def dashboard() -> Response:
    auth = require_review_token()
    if auth:
        return auth

    min_score = parse_min_score(request.args.get("min_score", 0))
    date_filter = parse_date_filter(request.args.get("date", ""))
    all_leads = load_dashboard_leads()
    leads = filter_dashboard_leads(all_leads, min_score=min_score, date_filter=date_filter)

    export_params = {}
    token = extract_token()
    if token:
        export_params["token"] = token
    extra_params = {}
    if min_score:
        extra_params["min_score"] = min_score
    if date_filter:
        extra_params["date"] = date_filter
    export_link = "/export.csv"
    merged_params = {**extra_params, **export_params}
    if merged_params:
        export_link = "/export.csv?" + urlencode(merged_params)

    dashboard_html = render_template_string(
        """
        <!doctype html>
        <html>
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>Lead Scanner Dashboard</title>
          <style>
            :root {
              --bg: #0f172a;
              --panel: #111827;
              --card: #1f2937;
              --text: #e5e7eb;
              --muted: #94a3b8;
              --accent: #38bdf8;
              --accent-2: #22c55e;
              --border: #334155;
            }
            body {
              margin: 0;
              font-family: Arial, sans-serif;
              background: linear-gradient(180deg, #020617, #0f172a 35%, #111827);
              color: var(--text);
            }
            .wrap { max-width: 1200px; margin: 0 auto; padding: 24px; }
            .hero, .panel, .card {
              background: rgba(17, 24, 39, 0.92);
              border: 1px solid var(--border);
              border-radius: 16px;
              box-shadow: 0 12px 30px rgba(0,0,0,.25);
            }
            .hero { padding: 20px; margin-bottom: 18px; }
            .meta { color: var(--muted); margin-top: 6px; }
            .grid { display: grid; gap: 16px; }
            .filters { display: flex; gap: 12px; flex-wrap: wrap; align-items: end; }
            label { display: grid; gap: 6px; font-size: 14px; color: var(--muted); }
            input {
              background: #0b1220;
              color: var(--text);
              border: 1px solid var(--border);
              border-radius: 10px;
              padding: 10px 12px;
            }
            .btn, a.btn {
              display: inline-block;
              border: 0;
              border-radius: 10px;
              background: var(--accent);
              color: #021024;
              padding: 10px 14px;
              text-decoration: none;
              font-weight: 700;
              cursor: pointer;
            }
            .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 14px 0; }
            .stat { padding: 14px; background: var(--card); border: 1px solid var(--border); border-radius: 14px; }
            .stat .v { font-size: 28px; font-weight: 700; }
            .lead { padding: 16px; }
            .lead + .lead { margin-top: 14px; }
            .lead-head { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
            .pill { display: inline-block; padding: 4px 10px; border-radius: 999px; background: rgba(56,189,248,.15); color: #7dd3fc; }
            pre {
              white-space: pre-wrap;
              word-break: break-word;
              background: #0b1220;
              border: 1px solid var(--border);
              border-radius: 12px;
              padding: 12px;
              overflow: auto;
            }
            .comments { display: grid; gap: 8px; }
            .comment-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
            .copy { background: #22c55e; color: #052e16; }
            .muted { color: var(--muted); }
          </style>
        </head>
        <body>
          <div class="wrap">
            <div class="hero">
              <h1>Lead Scanner Dashboard</h1>
              <div class="meta">Manual review only. No Facebook posting actions are available.</div>
              <div class="stats">
                <div class="stat"><div class="v">{{ total }}</div><div class="muted">Total leads shown</div></div>
                <div class="stat"><div class="v">{{ all_total }}</div><div class="muted">All saved leads</div></div>
                <div class="stat"><div class="v">{{ min_score }}</div><div class="muted">Minimum score filter</div></div>
              </div>
              <form method="get" class="filters">
                {% if token %}<input type="hidden" name="token" value="{{ token }}">{% endif %}
                <label>Minimum score
                  <input type="number" name="min_score" min="0" max="100" value="{{ min_score }}">
                </label>
                <label>Date
                  <input type="date" name="date" value="{{ date_filter }}">
                </label>
                <button class="btn" type="submit">Apply</button>
                <a class="btn" href="{{ export_link }}">Export CSV</a>
              </form>
            </div>

            <div class="grid">
              {% for lead in leads %}
              <div class="lead card">
                <div class="lead-head">
                  <div>
                    <div><strong>{{ lead.created_at }}</strong></div>
                    <div class="muted">{{ lead.source_url }}</div>
                  </div>
                  <div>
                    <span class="pill">Score {{ lead.score }}</span>
                    <span class="pill">{{ lead.intent }}</span>
                  </div>
                </div>
                <p>{{ lead.text }}</p>
                <div class="muted">Reasons: {{ lead.reasons | join(", ") }}</div>
                <div class="muted">Matched: {{ lead.matched_keywords | join(", ") }}</div>
                <div class="comments">
                  {% for comment in lead.suggested_comments %}
                  <div class="comment-row">
                    <pre>{{ comment }}</pre>
                    <button class="btn copy" type="button" onclick="copyText({{ comment | tojson }}, this)">Copy</button>
                  </div>
                  {% endfor %}
                </div>
              </div>
              {% endfor %}
            </div>
          </div>

          <script>
            async function copyText(text, button) {
              try {
                await navigator.clipboard.writeText(text);
                const old = button.textContent;
                button.textContent = "Copied";
                setTimeout(() => button.textContent = old, 1200);
              } catch (err) {
                alert("Copy failed");
              }
            }
          </script>
        </body>
        </html>
        """,
        leads=leads,
        all_total=len(all_leads),
        total=len(leads),
        min_score=min_score,
        date_filter=date_filter or "",
        export_link=export_link,
        token=token,
    )
    return Response(dashboard_html, mimetype="text/html")


@app.route("/export.csv", methods=["GET"])
def export_csv() -> Response:
    auth = require_review_token()
    if auth:
        return auth

    min_score = parse_min_score(request.args.get("min_score", 0))
    date_filter = parse_date_filter(request.args.get("date", ""))
    leads = filter_dashboard_leads(load_dashboard_leads(), min_score=min_score, date_filter=date_filter)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "created_at", "source_url", "text", "score", "intent", "reasons", "matched_keywords", "suggested_comments"])
    for lead in leads:
        writer.writerow(
            [
                lead.get("id", ""),
                lead.get("created_at", ""),
                lead.get("source_url", ""),
                lead.get("text", ""),
                lead.get("score", ""),
                lead.get("intent", ""),
                " | ".join(lead.get("reasons", [])),
                " | ".join(lead.get("matched_keywords", [])),
                " || ".join(lead.get("suggested_comments", [])),
            ]
        )

    response = Response(output.getvalue(), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = "attachment; filename=leads.csv"
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=False)
