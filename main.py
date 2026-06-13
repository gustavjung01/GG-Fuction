import base64, csv, io, logging, os, re, time, uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

from flask import Flask, Response, jsonify, redirect, render_template_string, request
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

try:
    from zoneinfo import ZoneInfo
except ImportError:
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
SCAN_DEFAULT_URLS = [x.strip() for x in os.getenv("SCAN_DEFAULT_URLS", DEFAULT_TARGET_URL).split(",") if x.strip()]
MAX_POSTS_DEFAULT = int(os.getenv("MAX_POSTS_DEFAULT", "20"))
SCAN_DELAY_SECONDS = float(os.getenv("SCAN_DELAY_SECONDS", "0"))
VERIFY_PROXY = os.getenv("VERIFY_PROXY", "false").lower() == "true"
DEFAULT_TIMEZONE = os.getenv("SCAN_TIMEZONE_DEFAULT", "Asia/Ho_Chi_Minh")
INGEST_MIN_SCORE_DEFAULT = int(os.getenv("INGEST_MIN_SCORE", "55"))
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
PROXY_SERVER = os.getenv("PROXY_SERVER", "").strip()
PROXY_USERNAME = os.getenv("PROXY_USERNAME", "").strip()
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD", "").strip()
CLASSIFIER = LeadClassifier()
STORE = LeadStore()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def split_text_lines(raw: Any) -> List[str]:
    if raw is None:
        return []
    values = re.split(r"[\n,]+", raw) if isinstance(raw, str) else list(raw) if isinstance(raw, Iterable) else [raw]
    return [str(v).strip() for v in values if str(v or "").strip()]


def normalize_urls(urls: Optional[Iterable[str]]) -> List[str]:
    if not urls:
        return list(SCAN_DEFAULT_URLS)
    if isinstance(urls, str):
        urls = [urls]
    return [u.strip() for u in urls if isinstance(u, str) and u.strip()]


def normalize_group_value(raw: str) -> Optional[str]:
    value = clean_text(raw)
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    value = value.strip("/")
    m = re.search(r"(?:^|/)groups/([^/?#]+)", value)
    if m:
        value = m.group(1)
    if value.startswith("www.facebook.com/groups/") or value.startswith("facebook.com/groups/"):
        return "https://" + value
    value = value.split("?", 1)[0].split("#", 1)[0].strip("/")
    return f"https://www.facebook.com/groups/{value}" if value else None


def normalize_group_inputs(raw: Any) -> List[str]:
    out, seen = [], set()
    for value in split_text_lines(raw):
        url = normalize_group_value(value)
        key = (url or "").lower()
        if url and key not in seen:
            seen.add(key)
            out.append(url)
    return out


def parse_raw_cookie_string(cookie_str: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    cookies, user_agent = [], None
    for part in [p.strip() for p in (cookie_str or "").split(";") if p.strip()]:
        if "=" not in part:
            continue
        name, value = [x.strip() for x in part.split("=", 1)]
        if not name:
            continue
        if name.lower() == "useragent":
            try:
                user_agent = base64.b64decode(value).decode("utf-8")
            except Exception:
                logger.warning("Could not decode UserAgent value from account cookie string.")
            continue
        cookies.append({"name": name, "value": value, "domain": ".facebook.com", "path": "/", "secure": True, "httpOnly": False, "sameSite": "Lax"})
    return cookies, user_agent


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
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def parse_date_filter(raw: str) -> Optional[str]:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return None


def parse_time_windows(raw: Any) -> List[str]:
    return split_text_lines(raw)


def parse_time_window_minutes(window: str) -> Optional[Tuple[int, int]]:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", window or "")
    if not m:
        return None
    sh, sm, eh, em = [int(x) for x in m.groups()]
    if not (0 <= sh <= 23 and 0 <= eh <= 23 and 0 <= sm <= 59 and 0 <= em <= 59):
        return None
    return sh * 60 + sm, eh * 60 + em


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
    parsed = [(w, p) for w in time_windows if (p := parse_time_window_minutes(w))]
    tzinfo, resolved = get_timezone(timezone_name)
    now_local = datetime.now(tzinfo)
    now_minutes = now_local.hour * 60 + now_local.minute
    if not parsed:
        return False, "No valid time windows configured.", resolved
    for label, (start, end) in parsed:
        if (start <= end and start <= now_minutes <= end) or (start > end and (now_minutes >= start or now_minutes <= end)):
            return True, f"Inside time window {label}.", resolved
    return False, f"Outside configured time windows at {now_local.strftime('%H:%M')} {resolved}.", resolved


def build_proxy_config() -> Optional[Dict[str, str]]:
    if not PROXY_SERVER:
        return None
    proxy = {"server": PROXY_SERVER}
    if PROXY_USERNAME:
        proxy["username"] = PROXY_USERNAME
    if PROXY_PASSWORD:
        proxy["password"] = PROXY_PASSWORD
    return proxy


def extract_token() -> str:
    token = request.args.get("token") or request.headers.get("X-Review-Token") or request.form.get("token") or ""
    if not token and request.is_json:
        token = str((request.get_json(silent=True) or {}).get("token", "") or "")
    return token.strip()


def require_review_token():
    if REVIEW_TOKEN and extract_token() != REVIEW_TOKEN:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    return None


def dashboard_redirect(message: str, level: str = "success") -> Response:
    params = {k: v for k, v in {"token": extract_token(), "message": message, "level": level}.items() if v}
    return redirect("/dashboard" + (("?" + urlencode(params)) if params else ""))


def load_scan_settings() -> Dict[str, Any]:
    stored = STORE.load_settings() or {}
    return {
        "groups": normalize_group_inputs(stored.get("groups", [])),
        "time_windows": parse_time_windows(stored.get("time_windows", [])),
        "accounts": split_text_lines(stored.get("accounts", [])),
        "max_posts": parse_positive_int(stored.get("max_posts", MAX_POSTS_DEFAULT), MAX_POSTS_DEFAULT),
        "include_comments": parse_bool(stored.get("include_comments"), False),
        "enabled": parse_bool(stored.get("enabled"), False),
        "timezone": str(stored.get("timezone") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE,
    }


def normalize_classification_reason(classification: Dict[str, Any]) -> List[str]:
    reasons = classification.get("reasons")
    if isinstance(reasons, list):
        return [str(r) for r in reasons if str(r or "").strip()]
    reason = str(classification.get("reason", "") or "").strip()
    return [reason] if reason else []


def classification_category(classification: Dict[str, Any]) -> str:
    category = str(classification.get("category", "") or "").upper()
    if category in {"TARGET", "SPAM", "TRASH"}:
        return category
    intent = str(classification.get("intent", "") or "").lower()
    return "TARGET" if intent == "borrower" else "SPAM" if intent == "lender" else "TRASH"


def classification_reason_text(classification: Dict[str, Any]) -> str:
    return "; ".join(normalize_classification_reason(classification))


def preview_text(text: str, limit: int = 220) -> str:
    text = clean_text(text)
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def display_url(item: Dict[str, Any]) -> str:
    return str(item.get("post_url") or item.get("source_url") or item.get("group_url") or "")


def build_lead_record(item: Dict[str, Any], text: str, classification: Dict[str, Any], category: str, score: int) -> Dict[str, Any]:
    source_url = str(item.get("source_url", "") or "")
    group_url = str(item.get("group_url") or source_url or "")
    post_url = str(item.get("post_url", "") or "")
    return {
        "id": f"lead_{uuid.uuid4().hex}", "created_at": now_iso(), "saved": True,
        "source_url": source_url, "group_url": group_url, "post_url": post_url, "display_url": post_url or source_url or group_url,
        "author": str(item.get("author", "") or ""), "text": text, "preview": preview_text(text), "score": score,
        "intent": str(classification.get("intent", "borrower") or "borrower"), "ai_category": category, "category": category,
        "reasons": normalize_classification_reason(classification), "reason": str(classification.get("reason", "") or classification_reason_text(classification)),
        "ai_reason": str(classification.get("reason", "") or classification_reason_text(classification)),
        "matched_keywords": classification.get("matched_keywords", []), "suggested_comments": CLASSIFIER.suggest_comments(text),
        "classifier": str(classification.get("classifier", "unknown") or "unknown"), "selector": item.get("selector", ""), "ingested": False,
    }


def build_scan_debug_item(item: Dict[str, Any], text: str, classification: Dict[str, Any], category: str, score: int, saved: bool, lead: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    source_url = str(item.get("source_url") or (lead or {}).get("source_url") or "")
    group_url = str(item.get("group_url") or source_url)
    post_url = str(item.get("post_url") or (lead or {}).get("post_url") or "")
    return {
        "raw_text": text, "text": text, "preview": preview_text(text), "saved": saved, "lead_id": lead.get("id") if lead else "",
        "created_at": lead.get("created_at") if lead else now_iso(), "source_url": source_url, "group_url": group_url,
        "post_url": post_url, "display_url": post_url or source_url or group_url, "author": str(item.get("author") or (lead or {}).get("author") or ""),
        "selector": item.get("selector", ""), "ai_category": category, "category": category, "score": score,
        "reason": str(classification.get("reason", "") or classification_reason_text(classification)), "intent": str(classification.get("intent", "") or ""),
        "matched_keywords": classification.get("matched_keywords", []), "suggested_comments": CLASSIFIER.suggest_comments(text),
        "classifier": str(classification.get("classifier", "unknown") or "unknown"), "classification": classification,
    }


class LeadScraper:
    def __init__(self, user_agent: str, proxy_config: Optional[Dict[str, str]] = None) -> None:
        self.user_agent = user_agent
        self.proxy_config = proxy_config

    def _launch(self, cookie_str: Optional[str] = None):
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"])
        ua, init_cookies = self.user_agent, []
        if cookie_str:
            init_cookies, custom_ua = parse_raw_cookie_string(cookie_str)
            ua = custom_ua or ua
        context = browser.new_context(user_agent=ua, viewport={"width": 1366, "height": 768}, proxy=self.proxy_config)
        if init_cookies:
            context.add_cookies(init_cookies)
        return playwright, browser, context, context.new_page()

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
    def _extract_post_url(node) -> str:
        try:
            links = []
            for link in node.query_selector_all("a[href]"):
                href = str(link.get_attribute("href") or "").strip()
                if href.startswith("/"):
                    href = "https://www.facebook.com" + href
                if any(m in href for m in ["/posts/", "/permalink/", "story_fbid=", "multi_permalinks=", "/groups/"]):
                    links.append(href.split("?__cft__", 1)[0])
            for href in links:
                if any(m in href for m in ["/posts/", "/permalink/", "story_fbid=", "multi_permalinks="]):
                    return href
            return links[0] if links else ""
        except Exception:
            return ""

    @staticmethod
    def _extract_author(node) -> str:
        for selector in ["h2 strong", "h3 strong", "strong a", "h2 a", "h3 a"]:
            try:
                found = node.query_selector(selector)
                author = clean_text(found.inner_text()) if found else ""
                if author and len(author) <= 120:
                    return author
            except Exception:
                continue
        return ""

    @staticmethod
    def _dedupe(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen, out = set(), []
        for item in items:
            text = clean_text(item.get("text", ""))
            key = (item.get("post_url") or text).lower()
            if text and key not in seen:
                seen.add(key)
                item["text"] = text
                out.append(item)
        return out

    def _extract_text_blocks(self, page, include_comments: bool) -> List[Dict[str, Any]]:
        selectors = ['div[role="article"]', "article", "main article", 'div[dir="auto"]']
        if include_comments:
            selectors.extend(['div[role="comment"]', '[data-ad-preview="message"]', "li"])
        blocks: List[Dict[str, Any]] = []
        for selector in selectors:
            for node in page.query_selector_all(selector):
                try:
                    text = clean_text(node.inner_text())
                except Exception:
                    continue
                if len(text) >= 20:
                    blocks.append({"selector": selector, "text": text, "post_url": self._extract_post_url(node), "author": self._extract_author(node)})
        return self._dedupe(blocks)

    def scrape(self, urls: Sequence[str], max_posts: int, include_comments: bool, cookie_str: Optional[str] = None) -> Dict[str, Any]:
        results, visited_urls, errors, ip_info = [], [], [], None
        playwright, browser, context, page = self._launch(cookie_str=cookie_str)
        if VERIFY_PROXY:
            try:
                page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=30000)
                body = clean_text(page.text_content("body") or "")
                m = re.search(r'"ip"\s*:\s*"([^"]+)"', body)
                ip_info = {"response": body, "ip": m.group(1) if m else None}
            except Exception as exc:
                ip_info = {"error": str(exc)}
        try:
            for pos, url in enumerate(urls, start=1):
                try:
                    logger.info("Scraping URL: %s", url)
                    page.goto(url, wait_until="networkidle", timeout=60000)
                    for _ in range(2):
                        page.mouse.wheel(0, 2200)
                        page.wait_for_timeout(2500)
                    visited_urls.append(url)
                    for block in self._extract_text_blocks(page, include_comments)[:max_posts]:
                        results.append({"source_url": url, "group_url": url, "post_url": block.get("post_url", ""), "author": block.get("author", ""), "text": block["text"], "selector": block.get("selector"), "include_comments": include_comments, "position": pos})
                    if SCAN_DELAY_SECONDS > 0:
                        time.sleep(SCAN_DELAY_SECONDS)
                except Exception as exc:
                    errors.append({"url": url, "error": str(exc)})
        finally:
            self._close(playwright, browser, context)
        return {"items": results[:max_posts], "visited_urls": visited_urls, "proxy_ip": ip_info, "errors": errors}


SCRAPER = LeadScraper(USER_AGENT, build_proxy_config())


def classify_and_enrich(items: Sequence[Dict[str, Any]], save: bool = False) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    leads, debug_items = [], []
    for item in items:
        text = clean_text(item.get("text", ""))
        if not text:
            continue
        classification = CLASSIFIER.classify_for_ingest(text)
        category = classification_category(classification)
        score = parse_min_score(classification.get("score", 0))
        saved = category == "TARGET" and score >= INGEST_MIN_SCORE_DEFAULT
        lead = build_lead_record(item, text, classification, category, score) if saved else None
        if lead:
            leads.append(lead)
        debug_items.append(build_scan_debug_item(item, text, classification, category, score, saved, lead))
    if save and leads:
        STORE.append_leads(leads)
    return leads, debug_items


def build_scan_response(urls: Sequence[str], max_posts: int, include_comments: bool, save: bool = False, debug: bool = False, accounts: Optional[List[str]] = None) -> Dict[str, Any]:
    import random
    cookie_str = random.choice(accounts) if accounts else None
    scraped = SCRAPER.scrape(urls, max_posts, include_comments, cookie_str=cookie_str)
    leads, debug_items = classify_and_enrich(scraped["items"], save=save)
    rejected = [x for x in debug_items if not x.get("saved")]
    response = {"status": "success", "visited_urls": scraped["visited_urls"], "proxy_ip": scraped["proxy_ip"], "errors": scraped["errors"], "scanned_count": len(scraped["items"]), "lead_count": len(leads), "leads": leads, "scanned_items_debug": debug_items, "rejected_items": rejected, "summary": {"borrower_leads": len(leads), "saved_leads": len(leads) if save else 0, "rejected_items": len(rejected), "error_count": len(scraped["errors"])}}
    if debug:
        response["raw_scraped_items"] = scraped["items"]
        response["classification_debug"] = debug_items
    if save:
        response["saved_count"] = len(leads)
    return response


def load_dashboard_leads() -> List[Dict[str, Any]]:
    leads = STORE.load_all_leads()
    for lead in leads:
        text = str(lead.get("text", "") or "")
        lead.setdefault("saved", True); lead.setdefault("category", lead.get("ai_category") or lead.get("intent") or "TARGET")
        lead.setdefault("preview", preview_text(text)); lead.setdefault("reason", lead.get("ai_reason") or "; ".join(lead.get("reasons", [])))
        lead.setdefault("post_url", ""); lead.setdefault("group_url", lead.get("source_url", "")); lead.setdefault("display_url", display_url(lead))
        lead.setdefault("suggested_comments", []); lead.setdefault("matched_keywords", [])
    return sorted(leads, key=lambda x: str(x.get("created_at", "")), reverse=True)


def filter_dashboard_leads(leads: Sequence[Dict[str, Any]], min_score: int, date_filter: Optional[str]) -> List[Dict[str, Any]]:
    out = []
    for lead in leads:
        if parse_min_score(lead.get("score", 0)) < min_score:
            continue
        if date_filter and not str(lead.get("created_at", "")).startswith(date_filter):
            continue
        out.append(lead)
    return sorted(out, key=lambda x: str(x.get("created_at", "")), reverse=True)


@app.route("/")
def root() -> Any:
    return jsonify({"status": "ok", "dashboard": "/dashboard", "dashboard_scan": "/dashboard/scan", "dashboard_settings": "/dashboard/settings", "scheduled_scan": "/scheduled-scan", "ingest_text": "/ingest-text", "health": "/health", "scan": "/scan", "scan_save": "/scan-save", "export": "/export.csv", "proxy_enabled": bool(PROXY_SERVER)})


@app.route("/health")
def health() -> Any:
    return jsonify({"status": "ok"})


@app.route("/scan", methods=["POST"])
def scan() -> Any:
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(build_scan_response(normalize_urls(data.get("urls")), parse_positive_int(data.get("max_posts", MAX_POSTS_DEFAULT), MAX_POSTS_DEFAULT), parse_bool(data.get("include_comments", False)), debug=parse_bool(data.get("debug", request.args.get("debug")), False)))
    except PlaywrightTimeoutError:
        return jsonify({"status": "error", "message": "Timeout while loading page."}), 504
    except Exception as exc:
        logger.exception("scan failed"); return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/scan-save", methods=["POST"])
def scan_save() -> Any:
    auth = require_review_token()
    if auth: return auth
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(build_scan_response(normalize_urls(data.get("urls")), parse_positive_int(data.get("max_posts", MAX_POSTS_DEFAULT), MAX_POSTS_DEFAULT), parse_bool(data.get("include_comments", False)), save=True, debug=parse_bool(data.get("debug", request.args.get("debug")), False)))
    except PlaywrightTimeoutError:
        return jsonify({"status": "error", "message": "Timeout while loading page."}), 504
    except Exception as exc:
        logger.exception("scan-save failed"); return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/ingest-text", methods=["POST"])
def ingest_text() -> Any:
    auth = require_review_token()
    if auth: return auth
    data = request.get_json(silent=True) or {}
    content = clean_text(str(data.get("content", "") or ""))
    if not content:
        return jsonify({"status": "error", "message": "content is required."}), 400
    min_score = parse_positive_int(data.get("min_score", INGEST_MIN_SCORE_DEFAULT), INGEST_MIN_SCORE_DEFAULT, minimum=0, maximum=100)
    classification = CLASSIFIER.classify_for_ingest(content)
    category, score = classification_category(classification), parse_min_score(classification.get("score", 0))
    saved, lead = category == "TARGET" and score >= min_score, None
    if saved:
        source_url = str(data.get("url", "") or "").strip()
        item = {"source_url": source_url, "group_url": str(data.get("group_url", "") or source_url).strip(), "post_url": str(data.get("post_url", "") or source_url).strip(), "author": str(data.get("author", "") or "").strip(), "selector": "manual-ingest"}
        lead = build_lead_record(item, content, classification, category, score); lead["ingested"] = True; STORE.append_leads([lead])
    return jsonify({"status": "success", "saved": saved, "min_score": min_score, "classification": classification, "lead": lead})


@app.route("/dashboard/scan", methods=["POST"])
def dashboard_scan() -> Response:
    auth = require_review_token()
    if auth: return auth
    settings, urls = load_scan_settings(), normalize_group_inputs(request.form.get("groups", ""))
    if not urls: return dashboard_redirect("Please enter at least one Facebook group UID, slug, or link.", "error")
    max_posts = parse_positive_int(request.form.get("max_posts", MAX_POSTS_DEFAULT), MAX_POSTS_DEFAULT)
    include_comments = parse_bool(request.form.get("include_comments"), False)
    try:
        updated_settings = dict(settings)
        updated_settings.update({"groups": urls, "max_posts": max_posts, "include_comments": include_comments, "updated_at": now_iso()})
        STORE.save_settings(updated_settings)
    except Exception:
        logger.warning("Could not persist manual scan settings.", exc_info=True)
    try:
        response = build_scan_response(urls, max_posts, include_comments, save=True, debug=True, accounts=settings.get("accounts", []))
        app.config["LAST_SCAN_DEBUG"] = {"created_at": now_iso(), "visited_urls": response.get("visited_urls", []), "scanned_count": response.get("scanned_count", 0), "saved_count": response.get("saved_count", 0), "rejected_count": len(response.get("rejected_items", [])), "items": response.get("scanned_items_debug", [])}
        return dashboard_redirect(f"Scan complete: visited {len(response.get('visited_urls', []))} group(s), scanned {response.get('scanned_count', 0)} item(s), saved {response.get('saved_count', 0)} lead(s), rejected {len(response.get('rejected_items', []))} item(s).")
    except PlaywrightTimeoutError:
        return dashboard_redirect("Scan failed: timeout while loading a group.", "error")
    except Exception as exc:
        logger.exception("dashboard scan failed"); return dashboard_redirect(f"Scan failed: {exc}", "error")


@app.route("/dashboard/settings", methods=["POST"])
def dashboard_settings() -> Response:
    auth = require_review_token()
    if auth: return auth
    STORE.save_settings({"groups": normalize_group_inputs(request.form.get("groups", "")), "time_windows": parse_time_windows(request.form.get("time_windows", "")), "accounts": split_text_lines(request.form.get("accounts", "")), "max_posts": parse_positive_int(request.form.get("max_posts", MAX_POSTS_DEFAULT), MAX_POSTS_DEFAULT), "include_comments": parse_bool(request.form.get("include_comments"), False), "enabled": parse_bool(request.form.get("enabled"), False), "timezone": (request.form.get("timezone") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE, "updated_at": now_iso()})
    return dashboard_redirect("Scheduled scan settings saved.")


@app.route("/scheduled-scan", methods=["POST"])
def scheduled_scan() -> Any:
    auth = require_review_token()
    if auth: return auth
    settings = load_scan_settings()
    if not settings["enabled"]: return jsonify({"status": "skipped", "reason": "Scheduled scan is disabled.", "settings": settings})
    urls = normalize_group_inputs(settings.get("groups", []))
    if not urls: return jsonify({"status": "skipped", "reason": "No groups configured.", "settings": settings})
    ok, reason, tz = is_inside_time_windows(settings.get("time_windows", []), str(settings.get("timezone") or DEFAULT_TIMEZONE))
    if not ok: return jsonify({"status": "skipped", "reason": reason, "timezone": tz, "time_windows": settings.get("time_windows", [])})
    try:
        response = build_scan_response(urls, parse_positive_int(settings.get("max_posts", MAX_POSTS_DEFAULT), MAX_POSTS_DEFAULT), parse_bool(settings.get("include_comments"), False), save=True, debug=True, accounts=settings.get("accounts", []))
        response.update({"scheduled": True, "schedule_reason": reason, "timezone": tz})
        return jsonify(response)
    except PlaywrightTimeoutError:
        return jsonify({"status": "error", "message": "Timeout while loading page."}), 504
    except Exception as exc:
        logger.exception("scheduled scan failed"); return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/suggest-comments", methods=["POST"])
def suggest_comments() -> Any:
    data = request.get_json(silent=True) or {}; leads = data.get("leads", data.get("texts", []))
    if not isinstance(leads, list): return jsonify({"status": "error", "message": "leads must be a list."}), 400
    out = []
    for i, lead in enumerate(leads, 1):
        text = str(lead.get("text", "") if isinstance(lead, dict) else lead or "").strip()
        if text: out.append({"lead_index": i, "lead_preview": clean_text(text)[:240], "suggested_comments": CLASSIFIER.suggest_comments(text)})
    return jsonify({"status": "success", "suggestions": out})


@app.route("/auto-comment", methods=["POST"])
def auto_comment() -> Any:
    return jsonify({"status": "disabled", "message": "Automatic Facebook commenting is disabled."}), 501


DASHBOARD_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Lead Scanner Dashboard</title>
<style>:root{--bg:#07111f;--p:#0f172a;--r:#111827;--t:#e5e7eb;--m:#94a3b8;--b:#263349;--a:#38bdf8;--g:#22c55e;--d:#fb7185;--v:#a78bfa}*{box-sizing:border-box}body{margin:0;font-family:Arial,sans-serif;background:linear-gradient(180deg,#020617,#0f172a,#111827);color:var(--t)}a{color:#7dd3fc;text-decoration:none}.wrap{max-width:1440px;margin:auto;padding:24px}.hero,.panel{background:rgba(15,23,42,.94);border:1px solid var(--b);border-radius:20px;padding:20px;margin-bottom:18px}.meta,.muted{color:var(--m)}.stats,.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.stat{background:#1f2937;border:1px solid var(--b);border-radius:14px;padding:14px}.v{font-size:30px;font-weight:900}.filters,.form-row,.head,.copy-strip{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.head{justify-content:space-between}label{display:grid;gap:6px;color:var(--m);font-size:14px}input,textarea{width:100%;background:#0b1220;color:var(--t);border:1px solid var(--b);border-radius:12px;padding:11px}textarea{min-height:104px}.btn{border:0;border-radius:12px;padding:10px 14px;min-height:42px;background:var(--a);color:#021024;font-weight:800;cursor:pointer;display:inline-flex;align-items:center;justify-content:center}.btn.ghost{background:#1f2937;color:var(--t);border:1px solid var(--b)}.btn.copy{background:var(--g);color:#052e16}.btn.secondary{background:var(--v);color:#160b2f}.msg{padding:12px;border-radius:12px;border:1px solid var(--b);margin-bottom:16px;background:rgba(34,197,94,.13);color:#bbf7d0}.msg.error{background:rgba(251,113,133,.13);color:#fecdd3}.list{display:grid;gap:10px}.list-head,.row{display:grid;grid-template-columns:72px 96px 56px minmax(100px,.7fr) minmax(240px,1.7fr) minmax(190px,1fr) 140px 188px;gap:10px;align-items:center}.list-head{padding:0 12px;color:var(--m);font-size:11px;text-transform:uppercase;letter-spacing:.04em}.row{background:rgba(17,24,39,.9);border:1px solid var(--b);border-radius:15px;padding:12px;min-width:0}.cell{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.preview{white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;line-height:1.35;max-height:2.7em}.pill{display:inline-flex;width:max-content;padding:4px 9px;border-radius:999px;background:rgba(56,189,248,.15);color:#7dd3fc;font-size:12px;font-weight:800}.saved{background:rgba(34,197,94,.15);color:#86efac}.rejected{background:rgba(251,113,133,.15);color:#fecdd3}.actions{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;width:100%}.actions .btn{min-height:32px;padding:6px 8px;border-radius:999px;font-size:12px;line-height:1;background:#111827;color:#cbd5e1;border:1px solid #334155;box-shadow:none}.actions .btn:hover{background:#1e293b;border-color:#60a5fa;color:#fff}.actions .btn.copy{background:rgba(34,197,94,.1);border-color:rgba(34,197,94,.35);color:#bbf7d0}.actions .btn.copy:hover{background:rgba(34,197,94,.2)}.empty{padding:16px;border:1px dashed var(--b);border-radius:14px;color:var(--m)}.modal{display:none;position:fixed;inset:0;z-index:50;background:rgba(2,6,23,.78);padding:20px}.modal.open{display:grid;place-items:center}.box{width:min(980px,100%);max-height:92vh;overflow:auto;background:#0f172a;border:1px solid var(--b);border-radius:20px}.box-head{position:sticky;top:0;background:#0f172a;border-bottom:1px solid var(--b);padding:14px;display:flex;justify-content:space-between}.box-body{padding:14px;display:grid;gap:12px}.details{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.detail{background:#0b1220;border:1px solid var(--b);border-radius:12px;padding:10px;word-break:break-word}.detail-head{display:flex;justify-content:space-between;gap:8px;align-items:center}.mini-copy{border:1px solid var(--b);background:#1f2937;color:var(--t);border-radius:9px;padding:5px 8px;font-size:12px;cursor:pointer}.k{font-size:12px;color:var(--m);text-transform:uppercase}pre{white-space:pre-wrap;word-break:break-word;background:#0b1220;border:1px solid var(--b);border-radius:12px;padding:12px}.hidden-json{display:none}@media(max-width:900px){.wrap{padding:14px}.form-grid{grid-template-columns:1fr}.list-head{display:none}.row{grid-template-columns:1fr}.cell{white-space:normal}.cell:before{content:attr(data-label);display:block;color:var(--m);font-size:11px;text-transform:uppercase;margin-bottom:3px}.actions{grid-template-columns:repeat(3,1fr)}.copy-strip{display:grid;grid-template-columns:repeat(2,1fr)}.btn{width:100%;padding:12px}.modal{padding:0}.box{height:100%;max-height:100vh;border-radius:0}.details{grid-template-columns:1fr}}</style></head><body><div class="wrap">
{% if message %}<div class="msg {{ message_level }}">{{ message }}</div>{% endif %}<div class="hero"><h1>Lead Scanner Dashboard</h1><div class="meta">Manual review only. No Facebook posting actions are available.</div><div class="stats"><div class="stat"><div class="v">{{ total }}</div><div class="muted">Total leads shown</div></div><div class="stat"><div class="v">{{ all_total }}</div><div class="muted">All saved leads</div></div><div class="stat"><div class="v">{{ min_score }}</div><div class="muted">Minimum score</div></div><div class="stat"><div class="v">{{ rejected_items|length }}</div><div class="muted">Rejected last scan</div></div></div><form method="get" class="filters">{% if token %}<input type="hidden" name="token" value="{{ token }}">{% endif %}<label>Minimum score<input type="number" name="min_score" min="0" max="100" value="{{ min_score }}"></label><label>Date<input type="date" name="date" value="{{ date_filter }}"></label><button class="btn">Apply</button><a class="btn ghost" href="{{ export_link }}">Export CSV</a></form></div>
<div class="form-grid"><div class="panel"><h2>Manual Scan</h2><div class="meta">Paste one Facebook group UID, slug, or link per line.</div><form method="post" action="/dashboard/scan" id="manualScanForm">{% if token %}<input type="hidden" name="token" value="{{ token }}">{% endif %}<label>Groups<textarea name="groups" data-draft="manual_groups" placeholder="123456789012345&#10;vayvonkinhdoanh">{{ settings_groups_text }}</textarea></label><div class="form-row"><label>Max posts<input type="number" name="max_posts" min="1" max="1000" value="{{ settings.max_posts }}" data-draft="manual_max_posts"></label><label><input type="checkbox" name="include_comments" value="1" data-draft="manual_include_comments" {% if settings.include_comments %}checked{% endif %}> Include comments</label><button class="btn" type="submit">Scan &amp; Save</button></div></form></div><div class="panel"><h2>Scheduled Scan Settings</h2><div class="meta">Use Cloud Scheduler to POST /scheduled-scan with X-Review-Token.</div><form method="post" action="/dashboard/settings" id="settingsForm">{% if token %}<input type="hidden" name="token" value="{{ token }}">{% endif %}<label>Groups<textarea name="groups" data-draft="settings_groups">{{ settings_groups_text }}</textarea></label><label>Time windows<textarea name="time_windows" data-draft="settings_time_windows" placeholder="08:00-11:30&#10;13:30-17:00">{{ settings_time_windows_text }}</textarea></label><label>Accounts / Cookie Strings<textarea name="accounts" data-draft="settings_accounts" placeholder="c_user=...; xs=...; fr=...">{{ settings_accounts_text }}</textarea></label><div class="form-row"><label>Max posts<input type="number" name="max_posts" min="1" max="1000" value="{{ settings.max_posts }}" data-draft="settings_max_posts"></label><label>Timezone<input type="text" name="timezone" value="{{ settings.timezone }}" data-draft="settings_timezone"></label><label><input type="checkbox" name="include_comments" value="1" data-draft="settings_include_comments" {% if settings.include_comments %}checked{% endif %}> Include comments</label><label><input type="checkbox" name="enabled" value="1" data-draft="settings_enabled" {% if settings.enabled %}checked{% endif %}> Enabled</label><button class="btn secondary" type="submit">Save Settings</button></div></form></div></div>
{% macro render_list(title, desc, rows) %}<section class="panel"><div class="head"><div><h2>{{ title }}</h2><div class="meta">{{ desc }}</div></div><span class="pill">{{ rows|length }} item(s)</span></div>{% if rows %}<div class="list"><div class="list-head"><div>Lưu</div><div>Loại</div><div>Điểm</div><div>Người đăng</div><div>Xem trước</div><div>Link</div><div>Thời gian</div><div>Thao tác</div></div>{% for item in rows %}{% set link = item.display_url or item.post_url or item.source_url %}<div class="row"><div class="cell" data-label="Lưu">{% if item.saved %}<span class="pill saved">Có</span>{% else %}<span class="pill rejected">Không</span>{% endif %}</div><div class="cell" data-label="Loại"><span class="pill">{{ item.category or item.ai_category or item.intent }}</span></div><div class="cell" data-label="Điểm">{{ item.score }}</div><div class="cell" data-label="Người đăng">{{ item.author or '-' }}</div><div class="cell preview" data-label="Xem trước">{{ item.preview or item.text }}</div><div class="cell" data-label="Link">{% if link %}<a href="{{ link }}" target="_blank" rel="noopener">{{ link }}</a>{% else %}-{% endif %}</div><div class="cell" data-label="Thời gian">{{ item.created_at }}</div><div class="actions" data-label="Thao tác"><button class="btn ghost" type="button" onclick="openItemModal(this)">Xem</button><button class="btn copy" type="button" onclick="copyText({{ link|tojson }}, this)">Link</button><button class="btn copy" type="button" onclick="copyText({{ item.text|tojson }}, this)">Text</button></div><script type="application/json" class="hidden-json">{{ item|tojson }}</script></div>{% endfor %}</div>{% else %}<div class="empty">No items yet.</div>{% endif %}</section>{% endmacro %}
{{ render_list('Saved Leads','Saved leads from storage.',leads) }}{{ render_list('Debug / Rejected Items','Debug Scanned Items data from the latest dashboard scan.',scan_debug_items) }}
</div><div class="modal" id="itemModal"><div class="box"><div class="box-head"><strong>Chi tiết bài quét</strong><button class="btn ghost" onclick="closeItemModal()">Đóng</button></div><div class="box-body" id="modalBody"></div></div></div><script>async function copyText(t,b){try{await navigator.clipboard.writeText(t||'');let o=b.textContent;b.textContent='Đã copy';setTimeout(()=>b.textContent=o,1200)}catch(e){alert('Copy không thành công')}}function joinLines(v){return Array.isArray(v)?v.join(String.fromCharCode(10)):(v||'')}function esc(v){if(Array.isArray(v))v=joinLines(v);if(v===true)v='Có';if(v===false)v='Không';v=(v===0||v)?String(v):'-';return v.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function val(d,k){if(Object.prototype.hasOwnProperty.call(d,k)&&d[k]!==null&&d[k]!==undefined&&d[k]!=='')return d[k];if(k==='category')return d.ai_category||d.intent||'';if(k==='reason')return d.ai_reason||'';return ''}function detail(label,value,copyLabel){let raw=joinLines(value);let btn=raw&&raw!=='-'?`<button class="mini-copy" type="button" onclick="copyText(${JSON.stringify(raw)},this)">${copyLabel||'Copy'}</button>`:'';return `<div class="detail"><div class="detail-head"><div class="k">${label}</div>${btn}</div><div>${esc(value)}</div></div>`}function openItemModal(btn){let d=JSON.parse(btn.closest('.row').querySelector('.hidden-json').textContent);let suggested=val(d,'suggested_comments');let text=val(d,'text')||val(d,'raw_text');let fields=[['Trạng thái lưu',val(d,'saved')],['Phân loại',val(d,'category')],['Điểm',val(d,'score')],['Người đăng',val(d,'author')],['Link nhóm',val(d,'group_url'),'Copy nhóm'],['Link bài viết',val(d,'post_url'),'Copy bài'],['Link nguồn',val(d,'source_url'),'Copy nguồn'],['Vị trí lấy dữ liệu',val(d,'selector')],['Lý do phân loại',val(d,'reason')],['Từ khóa khớp',val(d,'matched_keywords')],['Gợi ý trả lời',suggested,'Copy gợi ý'],['Thời gian quét',val(d,'created_at')]];let quick=`<div class="copy-strip"><button class="btn copy" onclick="copyText(${JSON.stringify(val(d,'group_url'))},this)">Copy link nhóm</button><button class="btn copy" onclick="copyText(${JSON.stringify(val(d,'post_url'))},this)">Copy link bài</button><button class="btn copy" onclick="copyText(${JSON.stringify(text)},this)">Copy nội dung</button><button class="btn copy" onclick="copyText(${JSON.stringify(joinLines(suggested))},this)">Copy gợi ý</button></div>`;document.getElementById('modalBody').innerHTML=quick+`<div class="details">${fields.map(x=>detail(x[0],x[1],x[2])).join('')}</div><div class="detail"><div class="detail-head"><div class="k">Nội dung đầy đủ</div><button class="mini-copy" onclick="copyText(${JSON.stringify(text)},this)">Copy nội dung</button></div><pre>${esc(text)}</pre></div>`;document.getElementById('itemModal').classList.add('open')}function closeItemModal(){document.getElementById('itemModal').classList.remove('open')}function setupDrafts(){document.querySelectorAll('[data-draft]').forEach(el=>{let key='fbLeadDash:'+el.dataset.draft;let saved=localStorage.getItem(key);if(saved!==null){if(el.type==='checkbox'){el.checked=saved==='1'}else if(!el.value){el.value=saved}}let save=()=>localStorage.setItem(key,el.type==='checkbox'?(el.checked?'1':'0'):el.value);el.addEventListener('input',save);el.addEventListener('change',save)});document.querySelectorAll('form').forEach(f=>f.addEventListener('submit',()=>document.querySelectorAll('[data-draft]').forEach(el=>{let key='fbLeadDash:'+el.dataset.draft;localStorage.setItem(key,el.type==='checkbox'?(el.checked?'1':'0'):el.value)})))}document.addEventListener('keydown',e=>{if(e.key==='Escape')closeItemModal()});setupDrafts();</script></body></html>
"""


@app.route("/dashboard")
def dashboard() -> Response:
    auth = require_review_token()
    if auth: return auth
    min_score, date_filter = parse_min_score(request.args.get("min_score", 0)), parse_date_filter(request.args.get("date", ""))
    all_leads = load_dashboard_leads(); leads = filter_dashboard_leads(all_leads, min_score, date_filter); settings = load_scan_settings()
    last_scan_debug = app.config.get("LAST_SCAN_DEBUG") or {}; scan_debug_items = last_scan_debug.get("items", []) if isinstance(last_scan_debug, dict) else []
    rejected_items = [x for x in scan_debug_items if not x.get("saved")]
    token = extract_token(); params = {k: v for k, v in {"token": token, "min_score": min_score if min_score else None, "date": date_filter}.items() if v}
    export_link = "/export.csv" + (("?" + urlencode(params)) if params else "")
    html = render_template_string(DASHBOARD_TEMPLATE, leads=leads, scan_debug_items=scan_debug_items, rejected_items=rejected_items, all_total=len(all_leads), total=len(leads), min_score=min_score, date_filter=date_filter or "", export_link=export_link, token=token, settings=settings, settings_groups_text="\n".join(settings.get("groups", [])), settings_time_windows_text="\n".join(settings.get("time_windows", [])), settings_accounts_text="\n".join(settings.get("accounts", [])), message=request.args.get("message", ""), message_level=request.args.get("level", ""), last_scan_debug=last_scan_debug)
    return Response(html, mimetype="text/html")


@app.route("/export.csv")
def export_csv() -> Response:
    auth = require_review_token()
    if auth: return auth
    leads = filter_dashboard_leads(load_dashboard_leads(), parse_min_score(request.args.get("min_score", 0)), parse_date_filter(request.args.get("date", "")))
    output = io.StringIO(); writer = csv.writer(output)
    writer.writerow(["id", "created_at", "saved", "category", "source_url", "group_url", "post_url", "author", "preview", "text", "score", "intent", "reasons", "matched_keywords", "suggested_comments"])
    for lead in leads:
        writer.writerow([lead.get("id", ""), lead.get("created_at", ""), lead.get("saved", True), lead.get("category", lead.get("ai_category", "")), lead.get("source_url", ""), lead.get("group_url", ""), lead.get("post_url", ""), lead.get("author", ""), lead.get("preview", ""), lead.get("text", ""), lead.get("score", ""), lead.get("intent", ""), " | ".join(lead.get("reasons", [])), " | ".join(lead.get("matched_keywords", [])), " || ".join(lead.get("suggested_comments", []))])
    response = Response(output.getvalue(), mimetype="text/csv; charset=utf-8"); response.headers["Content-Disposition"] = "attachment; filename=leads.csv"; return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=False)
