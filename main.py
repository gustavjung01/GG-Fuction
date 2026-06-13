import base64
import csv
import io
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from flask import Flask, Response, jsonify, redirect, request, render_template_string
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 fallback
    ZoneInfo = None  # type: ignore

from lead_classifier import LeadClassifier
from storage import LeadStore

app = Flask(__name__)
app.config["LAST_SCAN_DEBUG"] = None

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
DEFAULT_TIMEZONE = os.getenv("SCAN_TIMEZONE_DEFAULT", "Asia/Ho_Chi_Minh")
INGEST_MIN_SCORE_DEFAULT = int(os.getenv("INGEST_MIN_SCORE", "55"))
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


def split_text_lines(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = re.split(r"[\n,]+", raw)
    elif isinstance(raw, Iterable):
        values = list(raw)
    else:
        values = [raw]
    return [str(value).strip() for value in values if str(value or "").strip()]


def normalize_urls(urls: Optional[Iterable[str]]) -> List[str]:
    if not urls:
        return list(SCAN_DEFAULT_URLS)
    if isinstance(urls, str):
        urls = [urls]
    return [url.strip() for url in urls if isinstance(url, str) and url.strip()]


def normalize_group_value(raw: str) -> Optional[str]:
    value = clean_text(raw)
    if not value:
        return None

    if value.startswith(("http://", "https://")):
        return value

    value = value.strip("/")
    group_match = re.search(r"(?:^|/)groups/([^/?#]+)", value)
    if group_match:
        value = group_match.group(1)

    if value.startswith("www.facebook.com/groups/") or value.startswith("facebook.com/groups/"):
        return "https://" + value

    value = value.split("?", 1)[0].split("#", 1)[0].strip("/")
    if not value:
        return None
    return f"https://www.facebook.com/groups/{value}"


def normalize_group_inputs(raw: Any) -> List[str]:
    urls: List[str] = []
    seen = set()
    for value in split_text_lines(raw):
        url = normalize_group_value(value)
        if not url:
            continue
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        urls.append(url)
    return urls


def parse_raw_cookie_string(cookie_str: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Parse a raw Facebook cookie string into Playwright cookie objects."""
    cookies: List[Dict[str, Any]] = []
    user_agent = None

    parts = [part.strip() for part in (cookie_str or "").split(";") if part.strip()]
    for part in parts:
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        if name.lower() == "useragent":
            try:
                user_agent = base64.b64decode(value).decode("utf-8")
            except Exception:
                logger.warning("Could not decode UserAgent value from account cookie string.")
            continue

        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": ".facebook.com",
                "path": "/",
                "secure": True,
                "httpOnly": False,
                "sameSite": "Lax",
            }
        )
    return cookies, user_agent


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


def parse_time_windows(raw: Any) -> List[str]:
    return split_text_lines(raw)


def parse_time_window_minutes(window: str) -> Optional[Tuple[int, int]]:
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", window or "")
    if not match:
        return None

    start_hour, start_minute, end_hour, end_minute = [int(part) for part in match.groups()]
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23 and 0 <= start_minute <= 59 and 0 <= end_minute <= 59):
        return None

    return start_hour * 60 + start_minute, end_hour * 60 + end_minute


def get_timezone(timezone_name: str):
    timezone_name = (timezone_name or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    if ZoneInfo is None:
        return timezone.utc, "UTC"
    try:
        return ZoneInfo(timezone_name), timezone_name
    except Exception:
        logger.warning("Invalid timezone %s; falling back to %s", timezone_name, DEFAULT_TIMEZONE)
        try:
            return ZoneInfo(DEFAULT_TIMEZONE), DEFAULT_TIMEZONE
        except Exception:
            return timezone.utc, "UTC"


def is_inside_time_windows(time_windows: Sequence[str], timezone_name: str) -> Tuple[bool, str, str]:
    parsed_windows = []
    for window in time_windows:
        parsed = parse_time_window_minutes(window)
        if parsed:
            parsed_windows.append((window, parsed))

    tzinfo, resolved_timezone = get_timezone(timezone_name)
    now_local = datetime.now(tzinfo)
    now_minutes = now_local.hour * 60 + now_local.minute

    if not parsed_windows:
        return False, "No valid time windows configured.", resolved_timezone

    for label, (start_minutes, end_minutes) in parsed_windows:
        if start_minutes <= end_minutes:
            if start_minutes <= now_minutes <= end_minutes:
                return True, f"Inside time window {label}.", resolved_timezone
        else:
            if now_minutes >= start_minutes or now_minutes <= end_minutes:
                return True, f"Inside overnight time window {label}.", resolved_timezone

    return False, f"Outside configured time windows at {now_local.strftime('%H:%M')} {resolved_timezone}.", resolved_timezone


def extract_token() -> str:
    token = (
        request.args.get("token")
        or request.headers.get("X-Review-Token")
        or request.form.get("token")
        or ""
    )
    if not token and request.is_json:
        data = request.get_json(silent=True) or {}
        token = str(data.get("token", "") or "")
    return str(token).strip()


def token_required() -> bool:
    if not REVIEW_TOKEN:
        return False
    return extract_token() == REVIEW_TOKEN


def require_review_token():
    if REVIEW_TOKEN and not token_required():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    return None


def dashboard_redirect(message: str, level: str = "success") -> Response:
    params = {}
    token = extract_token()
    if token:
        params["token"] = token
    if message:
        params["message"] = message
    if level:
        params["level"] = level
    location = "/dashboard"
    if params:
        location += "?" + urlencode(params)
    return redirect(location)


def load_scan_settings() -> Dict[str, Any]:
    stored = STORE.load_settings() or {}
    groups = stored.get("groups", [])
    time_windows = stored.get("time_windows", [])
    return {
        "groups": normalize_group_inputs(groups),
        "time_windows": parse_time_windows(time_windows),
        "accounts": split_text_lines(stored.get("accounts", [])),
        "max_posts": parse_positive_int(stored.get("max_posts", MAX_POSTS_DEFAULT), MAX_POSTS_DEFAULT),
        "include_comments": parse_bool(stored.get("include_comments"), False),
        "enabled": parse_bool(stored.get("enabled"), False),
        "timezone": str(stored.get("timezone") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE,
    }


def normalize_classification_reason(classification: Dict[str, Any]) -> List[str]:
    reasons = classification.get("reasons")
    if isinstance(reasons, list):
        return [str(reason) for reason in reasons if str(reason or "").strip()]
    reason = str(classification.get("reason", "") or "").strip()
    return [reason] if reason else []


def classification_category(classification: Dict[str, Any]) -> str:
    category = str(classification.get("category", "") or "").upper()
    if category in {"TARGET", "SPAM", "TRASH"}:
        return category
    intent = str(classification.get("intent", "") or "").lower()
    if intent == "borrower":
        return "TARGET"
    if intent == "lender":
        return "SPAM"
    return "TRASH"


def classification_reason_text(classification: Dict[str, Any]) -> str:
    reasons = normalize_classification_reason(classification)
    return "; ".join(reasons)


def build_lead_record(item: Dict[str, Any], text: str, classification: Dict[str, Any], category: str, score: int) -> Dict[str, Any]:
    return {
        "id": f"lead_{uuid.uuid4().hex}",
        "created_at": now_iso(),
        "source_url": item.get("source_url", ""),
        "author": item.get("author", ""),
        "text": text,
        "score": score,
        "intent": str(classification.get("intent", "borrower") or "borrower"),
        "reasons": normalize_classification_reason(classification),
        "matched_keywords": classification.get("matched_keywords", []),
        "suggested_comments": CLASSIFIER.suggest_comments(text),
        "ai_category": category,
        "ai_reason": str(classification.get("reason", "") or classification_reason_text(classification)),
        "classifier": str(classification.get("classifier", "unknown") or "unknown"),
        "selector": item.get("selector", ""),
        "ingested": False,
    }


def build_scan_debug_item(
    item: Dict[str, Any],
    text: str,
    classification: Dict[str, Any],
    category: str,
    score: int,
    saved: bool,
    lead: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "raw_text": text,
        "text": text,
        "source_url": item.get("source_url", ""),
        "selector": item.get("selector", ""),
        "ai_category": category,
        "category": category,
        "score": score,
        "reason": str(classification.get("reason", "") or classification_reason_text(classification)),
        "intent": str(classification.get("intent", "") or ""),
        "matched_keywords": classification.get("matched_keywords", []),
        "classifier": str(classification.get("classifier", "unknown") or "unknown"),
        "saved": saved,
        "lead_id": lead.get("id") if lead else "",
        "classification": classification,
    }


class LeadScraper:
    def __init__(self, user_agent: str, proxy_config: Optional[Dict[str, str]] = None) -> None:
        self.user_agent = user_agent
        self.proxy_config = proxy_config

    def _launch(self, cookie_str: Optional[str] = None):
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )

        ua = self.user_agent
        init_cookies = []
        if cookie_str:
            parsed_cookies, custom_ua = parse_raw_cookie_string(cookie_str)
            init_cookies = parsed_cookies
            if custom_ua:
                ua = custom_ua

        context = browser.new_context(
            user_agent=ua,
            viewport={"width": 1366, "height": 768},
            proxy=self.proxy_config,
        )
        if init_cookies:
            context.add_cookies(init_cookies)

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

    def scrape(self, urls: Sequence[str], max_posts: int, include_comments: bool, cookie_str: Optional[str] = None) -> Dict[str, Any]:
        results: List[Dict[str, Any]] = []
        visited_urls: List[str] = []
        ip_info: Optional[Dict[str, Any]] = None
        errors: List[Dict[str, str]] = []
        playwright, browser, context, page = self._launch(cookie_str=cookie_str)

        if VERIFY_PROXY:
            try:
                ip_info = self._verify_proxy_ip(page)
            except Exception as exc:
                ip_info = {"error": str(exc)}

        try:
            for index, url in enumerate(urls, start=1):
                try:
                    logger.info("Scraping URL: %s", url)
                    page.goto(url, wait_until="networkidle", timeout=60000)
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


def classify_and_enrich(items: Sequence[Dict[str, Any]], save: bool = False) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    enriched: List[Dict[str, Any]] = []
    scanned_items_debug: List[Dict[str, Any]] = []
    for item in items:
        text = clean_text(item.get("text", ""))
        if not text:
            continue

        classification = CLASSIFIER.classify_for_ingest(text)
        category = classification_category(classification)
        score = parse_min_score(classification.get("score", 0))
        saved = category == "TARGET" and score >= INGEST_MIN_SCORE_DEFAULT
        lead_record = None

        if saved:
            lead_record = build_lead_record(item, text, classification, category, score)
            enriched.append(lead_record)

        scanned_items_debug.append(build_scan_debug_item(item, text, classification, category, score, saved, lead_record))

    if save and enriched:
        STORE.append_leads(enriched)
    return enriched, scanned_items_debug


def build_scan_response(
    urls: Sequence[str],
    max_posts: int,
    include_comments: bool,
    save: bool = False,
    debug: bool = False,
    accounts: Optional[List[str]] = None,
) -> Dict[str, Any]:
    import random

    cookie_str = None
    if accounts:
        cookie_str = random.choice(accounts)
        logger.info("Using a random account from the configured account list.")

    scraped = SCRAPER.scrape(urls=urls, max_posts=max_posts, include_comments=include_comments, cookie_str=cookie_str)
    leads, scanned_items_debug = classify_and_enrich(scraped["items"], save=save)
    rejected_items = [item for item in scanned_items_debug if not item.get("saved")]
    response: Dict[str, Any] = {
        "status": "success",
        "visited_urls": scraped["visited_urls"],
        "proxy_ip": scraped["proxy_ip"],
        "errors": scraped["errors"],
        "scanned_count": len(scraped["items"]),
        "lead_count": len(leads),
        "leads": leads,
        "scanned_items_debug": scanned_items_debug,
        "rejected_items": rejected_items,
        "summary": {
            "borrower_leads": len(leads),
            "saved_leads": len(leads) if save else 0,
            "rejected_items": len(rejected_items),
            "error_count": len(scraped["errors"]),
        },
    }
    if debug:
        response["raw_scraped_items"] = scraped["items"]
        response["classification_debug"] = scanned_items_debug
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
            "dashboard_scan": "/dashboard/scan",
            "dashboard_settings": "/dashboard/settings",
            "scheduled_scan": "/scheduled-scan",
            "ingest_text": "/ingest-text",
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
    debug = parse_bool(data.get("debug", request.args.get("debug")), False)
    try:
        return jsonify(build_scan_response(urls, max_posts, include_comments, save=False, debug=debug))
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
    debug = parse_bool(data.get("debug", request.args.get("debug")), False)

    try:
        response = build_scan_response(urls, max_posts, include_comments, save=True, debug=debug)
        return jsonify(response)
    except PlaywrightTimeoutError:
        return jsonify({"status": "error", "message": "Timeout while loading page."}), 504
    except Exception as exc:
        logger.exception("scan-save failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/ingest-text", methods=["POST"])
def ingest_text() -> Any:
    auth = require_review_token()
    if auth:
        return auth

    data = request.get_json(silent=True) or {}
    content = clean_text(str(data.get("content", "") or ""))
    if not content:
        return jsonify({"status": "error", "message": "content is required."}), 400

    min_score = parse_positive_int(
        data.get("min_score", INGEST_MIN_SCORE_DEFAULT),
        INGEST_MIN_SCORE_DEFAULT,
        minimum=0,
        maximum=100,
    )
    classification = CLASSIFIER.classify_for_ingest(content)
    category = str(classification.get("category", "") or "").upper()
    score = parse_min_score(classification.get("score", 0))
    saved = category == "TARGET" and score >= min_score
    lead = None

    if saved:
        lead = {
            "id": f"lead_{uuid.uuid4().hex}",
            "created_at": now_iso(),
            "source_url": str(data.get("url", "") or "").strip(),
            "author": str(data.get("author", "") or "").strip(),
            "text": content,
            "score": score,
            "intent": str(classification.get("intent", "borrower") or "borrower"),
            "reasons": normalize_classification_reason(classification),
            "matched_keywords": classification.get("matched_keywords", []),
            "suggested_comments": CLASSIFIER.suggest_comments(content),
            "ai_category": category,
            "ai_reason": str(classification.get("reason", "") or ""),
            "classifier": str(classification.get("classifier", "unknown") or "unknown"),
            "ingested": True,
        }
        STORE.append_leads([lead])

    return jsonify(
        {
            "status": "success",
            "saved": saved,
            "min_score": min_score,
            "classification": classification,
            "lead": lead,
        }
    )


@app.route("/dashboard/scan", methods=["POST"])
def dashboard_scan() -> Response:
    auth = require_review_token()
    if auth:
        return auth

    settings = load_scan_settings()
    urls = normalize_group_inputs(request.form.get("groups", ""))
    if not urls:
        return dashboard_redirect("Please enter at least one Facebook group UID, slug, or link.", "error")

    accounts = settings.get("accounts", [])
    max_posts = parse_positive_int(request.form.get("max_posts", MAX_POSTS_DEFAULT), MAX_POSTS_DEFAULT)
    include_comments = parse_bool(request.form.get("include_comments"), False)

    try:
        response = build_scan_response(urls, max_posts, include_comments, save=True, debug=True, accounts=accounts)
        app.config["LAST_SCAN_DEBUG"] = {
            "created_at": now_iso(),
            "visited_urls": response.get("visited_urls", []),
            "scanned_count": response.get("scanned_count", 0),
            "saved_count": response.get("saved_count", 0),
            "rejected_count": len(response.get("rejected_items", [])),
            "items": response.get("scanned_items_debug", []),
        }
        message = (
            f"Scan complete: visited {len(response.get('visited_urls', []))} group(s), "
            f"scanned {response.get('scanned_count', 0)} item(s), "
            f"saved {response.get('saved_count', 0)} lead(s), "
            f"rejected {len(response.get('rejected_items', []))} item(s)."
        )
        return dashboard_redirect(message, "success")
    except PlaywrightTimeoutError:
        return dashboard_redirect("Scan failed: timeout while loading a group.", "error")
    except Exception as exc:
        logger.exception("dashboard scan failed")
        return dashboard_redirect(f"Scan failed: {exc}", "error")


@app.route("/dashboard/settings", methods=["POST"])
def dashboard_settings() -> Response:
    auth = require_review_token()
    if auth:
        return auth

    settings = {
        "groups": normalize_group_inputs(request.form.get("groups", "")),
        "time_windows": parse_time_windows(request.form.get("time_windows", "")),
        "accounts": split_text_lines(request.form.get("accounts", "")),
        "max_posts": parse_positive_int(request.form.get("max_posts", MAX_POSTS_DEFAULT), MAX_POSTS_DEFAULT),
        "include_comments": parse_bool(request.form.get("include_comments"), False),
        "enabled": parse_bool(request.form.get("enabled"), False),
        "timezone": (request.form.get("timezone") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE,
        "updated_at": now_iso(),
    }
    STORE.save_settings(settings)
    return dashboard_redirect("Scheduled scan settings saved.", "success")


@app.route("/scheduled-scan", methods=["POST"])
def scheduled_scan() -> Any:
    auth = require_review_token()
    if auth:
        return auth

    settings = load_scan_settings()
    if not settings["enabled"]:
        return jsonify({"status": "skipped", "reason": "Scheduled scan is disabled.", "settings": settings})

    urls = normalize_group_inputs(settings.get("groups", []))
    if not urls:
        return jsonify({"status": "skipped", "reason": "No groups configured.", "settings": settings})

    inside_window, reason, resolved_timezone = is_inside_time_windows(
        settings.get("time_windows", []),
        str(settings.get("timezone") or DEFAULT_TIMEZONE),
    )
    if not inside_window:
        return jsonify(
            {
                "status": "skipped",
                "reason": reason,
                "timezone": resolved_timezone,
                "time_windows": settings.get("time_windows", []),
            }
        )

    max_posts = parse_positive_int(settings.get("max_posts", MAX_POSTS_DEFAULT), MAX_POSTS_DEFAULT)
    include_comments = parse_bool(settings.get("include_comments"), False)

    try:
        response = build_scan_response(
            urls,
            max_posts,
            include_comments,
            save=True,
            debug=True,
            accounts=settings.get("accounts", []),
        )
        response["scheduled"] = True
        response["schedule_reason"] = reason
        response["timezone"] = resolved_timezone
        return jsonify(response)
    except PlaywrightTimeoutError:
        return jsonify({"status": "error", "message": "Timeout while loading page."}), 504
    except Exception as exc:
        logger.exception("scheduled scan failed")
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
    settings = load_scan_settings()
    last_scan_debug = app.config.get("LAST_SCAN_DEBUG") or {}
    scan_debug_items = last_scan_debug.get("items", []) if isinstance(last_scan_debug, dict) else []

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
              --danger: #fb7185;
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
            .hero, .panel { padding: 20px; margin-bottom: 18px; }
            .meta { color: var(--muted); margin-top: 6px; }
            .grid { display: grid; gap: 16px; }
            .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
            .filters, .form-row { display: flex; gap: 12px; flex-wrap: wrap; align-items: end; }
            label { display: grid; gap: 6px; font-size: 14px; color: var(--muted); }
            input, textarea, select {
              background: #0b1220;
              color: var(--text);
              border: 1px solid var(--border);
              border-radius: 10px;
              padding: 10px 12px;
            }
            textarea { min-height: 104px; resize: vertical; }
            .wide { width: 100%; }
            .check { display: flex; align-items: center; gap: 8px; color: var(--muted); }
            .check input { width: auto; }
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
            .btn.secondary { background: #a78bfa; color: #160b2f; }
            .message {
              padding: 12px 14px;
              border-radius: 12px;
              border: 1px solid var(--border);
              margin-bottom: 18px;
              background: rgba(34, 197, 94, .12);
              color: #bbf7d0;
            }
            .message.error {
              background: rgba(251, 113, 133, .12);
              color: #fecdd3;
            }
            .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 14px 0; }
            .stat { padding: 14px; background: var(--card); border: 1px solid var(--border); border-radius: 14px; }
            .stat .v { font-size: 28px; font-weight: 700; }
            .lead { padding: 16px; }
            .lead + .lead { margin-top: 14px; }
            .lead-head { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
            .pill { display: inline-block; padding: 4px 10px; border-radius: 999px; background: rgba(56,189,248,.15); color: #7dd3fc; }
            .pill.saved { background: rgba(34,197,94,.15); color: #86efac; }
            .pill.rejected { background: rgba(251,113,133,.15); color: #fecdd3; }
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
            .debug-wrap { margin-top: 18px; overflow-x: auto; }
            .debug-table { width: 100%; border-collapse: collapse; min-width: 980px; }
            .debug-table th, .debug-table td { border-top: 1px solid var(--border); padding: 10px; vertical-align: top; text-align: left; }
            .debug-table th { color: var(--muted); font-size: 13px; }
            .debug-table pre { max-height: 180px; margin: 0; }
          </style>
        </head>
        <body>
          <div class="wrap">
            {% if message %}
            <div class="message {{ message_level }}">{{ message }}</div>
            {% endif %}

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

            <div class="form-grid">
              <div class="panel">
                <h2>Manual Scan</h2>
                <div class="meta">Paste one Facebook group UID, slug, or link per line. Results below show every scanned item, saved or rejected.</div>
                <form method="post" action="/dashboard/scan" class="grid">
                  {% if token %}<input type="hidden" name="token" value="{{ token }}">{% endif %}
                  <label>Groups
                    <textarea name="groups" class="wide" placeholder="123456789012345&#10;vayvonkinhdoanh">{{ settings_groups_text }}</textarea>
                  </label>
                  <div class="form-row">
                    <label>Max posts
                      <input type="number" name="max_posts" min="1" max="1000" value="{{ settings.max_posts }}">
                    </label>
                    <label class="check">
                      <input type="checkbox" name="include_comments" value="1" {% if settings.include_comments %}checked{% endif %}>
                      Include comments
                    </label>
                    <button class="btn" type="submit">Scan &amp; Save</button>
                  </div>
                </form>

                {% if scan_debug_items %}
                <div class="debug-wrap">
                  <h3>Debug Scanned Items</h3>
                  <div class="meta">
                    Last scan: scanned {{ last_scan_debug.scanned_count }}, saved {{ last_scan_debug.saved_count }}, rejected {{ last_scan_debug.rejected_count }}.
                  </div>
                  <table class="debug-table">
                    <thead>
                      <tr>
                        <th>Saved</th>
                        <th>AI Category</th>
                        <th>Score</th>
                        <th>Reason</th>
                        <th>Source URL</th>
                        <th>Selector</th>
                        <th>Raw Text</th>
                      </tr>
                    </thead>
                    <tbody>
                      {% for item in scan_debug_items %}
                      <tr>
                        <td>
                          {% if item.saved %}
                          <span class="pill saved">true</span>
                          {% else %}
                          <span class="pill rejected">false</span>
                          {% endif %}
                        </td>
                        <td>{{ item.ai_category }}</td>
                        <td>{{ item.score }}</td>
                        <td>{{ item.reason }}</td>
                        <td>{{ item.source_url }}</td>
                        <td>{{ item.selector }}</td>
                        <td><pre>{{ item.raw_text }}</pre></td>
                      </tr>
                      {% endfor %}
                    </tbody>
                  </table>
                </div>
                {% endif %}
              </div>

              <div class="panel">
                <h2>Scheduled Scan Settings</h2>
                <div class="meta">Use Cloud Scheduler to POST /scheduled-scan with X-Review-Token.</div>
                <form method="post" action="/dashboard/settings" class="grid">
                  {% if token %}<input type="hidden" name="token" value="{{ token }}">{% endif %}
                  <label>Groups
                    <textarea name="groups" class="wide" placeholder="123456789012345&#10;https://www.facebook.com/groups/my-group">{{ settings_groups_text }}</textarea>
                  </label>
                  <label>Time windows
                    <textarea name="time_windows" class="wide" placeholder="08:00-11:30&#10;13:30-17:00">{{ settings_time_windows_text }}</textarea>
                  </label>
                  <label>Accounts / Cookie Strings
                    <textarea name="accounts" class="wide" placeholder="c_user=...; xs=...; fr=...&#10;c_user=...; xs=...; fr=...">{{ settings_accounts_text }}</textarea>
                  </label>
                  <div class="form-row">
                    <label>Max posts
                      <input type="number" name="max_posts" min="1" max="1000" value="{{ settings.max_posts }}">
                    </label>
                    <label>Timezone
                      <input type="text" name="timezone" value="{{ settings.timezone }}" placeholder="Asia/Ho_Chi_Minh">
                    </label>
                    <label class="check">
                      <input type="checkbox" name="include_comments" value="1" {% if settings.include_comments %}checked{% endif %}>
                      Include comments
                    </label>
                    <label class="check">
                      <input type="checkbox" name="enabled" value="1" {% if settings.enabled %}checked{% endif %}>
                      Enabled
                    </label>
                    <button class="btn secondary" type="submit">Save Settings</button>
                  </div>
                </form>
              </div>
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
        settings=settings,
        settings_groups_text="\n".join(settings.get("groups", [])),
        settings_time_windows_text="\n".join(settings.get("time_windows", [])),
        settings_accounts_text="\n".join(settings.get("accounts", [])),
        message=request.args.get("message", ""),
        message_level=request.args.get("level", ""),
        last_scan_debug=last_scan_debug,
        scan_debug_items=scan_debug_items,
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
