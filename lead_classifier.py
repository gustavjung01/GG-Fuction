import json
import os
import re
from typing import Dict, List

try:
    import vertexai
    from vertexai.generative_models import GenerationConfig, GenerativeModel
except ImportError:  # pragma: no cover - optional until dependency is installed
    vertexai = None  # type: ignore
    GenerationConfig = None  # type: ignore
    GenerativeModel = None  # type: ignore


BORROWER_KEYWORDS = [
    "cần vay",
    "muốn vay",
    "vay gấp",
    "cần tiền",
    "kẹt tiền",
    "đang cần vốn",
    "cần xoay tiền",
    "vay tín chấp",
    "vay nhanh",
    "ai hỗ trợ vay",
    "cần vay ngân hàng",
    "cần vay trả góp",
    "thiếu vốn",
    "cần vốn kinh doanh",
]

NEGATIVE_LENDER_KEYWORDS = [
    "cho vay",
    "hỗ trợ vay",
    "giải ngân",
    "lãi suất",
    "duyệt hồ sơ",
    "inbox tư vấn",
    "cam kết",
    "bao nợ xấu",
    "vay chỉ cần cccd",
    "dịch vụ vay",
    "tư vấn vay",
    "cầm đồ",
    "bốc bát họ",
    "nhận làm hồ sơ",
    "hỗ trợ nợ xấu",
]


VERTEX_CATEGORY_TO_INTENT = {
    "TARGET": "borrower",
    "SPAM": "lender",
    "TRASH": "trash",
}


PROMPT_TEMPLATE = """
Phân loại văn bản này thành TARGET (Khách vay tiền), SPAM (Cò tín dụng/App), hoặc TRASH (Rác).
Chỉ trả về JSON gồm: category, reason, score (từ 0-100).

Văn bản:
{text}
""".strip()


def _match_keywords(text: str, keywords: List[str]) -> List[str]:
    lowered = text.lower()
    return [keyword for keyword in keywords if keyword in lowered]


def _parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _clamp_score(value: object) -> int:
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError):
        return 0


def _extract_json_payload(raw: str) -> Dict[str, object]:
    text = (raw or "").strip()
    if not text:
        return {}

    code_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if code_match:
        text = code_match.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


class LeadClassifier:
    def __init__(self) -> None:
        self.borrower_keywords = BORROWER_KEYWORDS
        self.negative_keywords = NEGATIVE_LENDER_KEYWORDS
        self.use_vertex_ai = _parse_bool_env("USE_VERTEX_AI", False)
        self.vertex_project_id = (
            os.getenv("VERTEX_PROJECT_ID")
            or os.getenv("GOOGLE_CLOUD_PROJECT")
            or ""
        ).strip()
        self.vertex_location = os.getenv("VERTEX_LOCATION", "asia-southeast1").strip() or "asia-southeast1"
        self.vertex_model_name = os.getenv("VERTEX_MODEL", "gemini-1.5-flash").strip() or "gemini-1.5-flash"
        self._vertex_model = None

    def classify(self, text: str) -> Dict[str, object]:
        normalized = " ".join((text or "").split()).lower()
        borrower_matches = _match_keywords(normalized, self.borrower_keywords)
        negative_matches = _match_keywords(normalized, self.negative_keywords)

        borrower_score = len(borrower_matches) * 28
        negative_score = len(negative_matches) * 34
        score = max(0, min(100, borrower_score - negative_score + (12 if borrower_matches else 0)))

        reasons: List[str] = []
        intent = "unknown"
        is_lead = False
        matched_keywords: List[str] = []

        if negative_matches and negative_score >= borrower_score:
            intent = "lender"
            reasons.append("Matched lender/spam keywords more strongly than borrower keywords.")
            reasons.extend(f"negative:{keyword}" for keyword in negative_matches)
            matched_keywords.extend(negative_matches)
            score = min(score, 20)
        elif borrower_matches:
            intent = "borrower"
            matched_keywords.extend(borrower_matches)
            if negative_matches:
                reasons.append("Borrower intent is present, but some lender-like phrases also appear.")
                reasons.extend(f"negative:{keyword}" for keyword in negative_matches)
                matched_keywords.extend(negative_matches)
            else:
                reasons.append("Strong borrower intent detected.")
            is_lead = score >= 35 and len(borrower_matches) >= 1 and len(negative_matches) == 0
        else:
            if negative_matches:
                intent = "lender"
                reasons.append("Matched lender/spam keywords.")
                reasons.extend(f"negative:{keyword}" for keyword in negative_matches)
                matched_keywords.extend(negative_matches)
            else:
                reasons.append("No strong borrower intent detected.")

        if intent == "borrower" and negative_matches and negative_score > 0:
            score = min(score, 60)
            is_lead = False

        return {
            "is_lead": is_lead,
            "intent": intent,
            "score": score,
            "reasons": reasons,
            "matched_keywords": matched_keywords,
        }

    def _legacy_to_ingest_result(self, text: str, classifier_name: str = "keyword") -> Dict[str, object]:
        result = self.classify(text)
        intent = str(result.get("intent", "unknown"))
        if intent == "borrower":
            category = "TARGET"
        elif intent == "lender":
            category = "SPAM"
        else:
            category = "TRASH"

        reasons = result.get("reasons", [])
        if isinstance(reasons, list):
            reason = "; ".join(str(item) for item in reasons if str(item).strip())
        else:
            reason = str(reasons or "")

        return {
            "category": category,
            "reason": reason or "Keyword fallback classifier result.",
            "score": _clamp_score(result.get("score", 0)),
            "is_lead": bool(result.get("is_lead", False)),
            "intent": intent if intent in {"borrower", "lender", "trash"} else "trash",
            "matched_keywords": result.get("matched_keywords", []),
            "classifier": classifier_name,
        }

    def _get_vertex_model(self):
        if vertexai is None or GenerativeModel is None or GenerationConfig is None:
            raise RuntimeError("google-cloud-aiplatform is not installed.")
        if not self.vertex_project_id:
            raise RuntimeError("VERTEX_PROJECT_ID or GOOGLE_CLOUD_PROJECT is required for Vertex AI classification.")
        if self._vertex_model is None:
            vertexai.init(project=self.vertex_project_id, location=self.vertex_location)
            self._vertex_model = GenerativeModel(self.vertex_model_name)
        return self._vertex_model

    def classify_with_vertex(self, text: str) -> Dict[str, object]:
        model = self._get_vertex_model()
        prompt = PROMPT_TEMPLATE.format(text=(text or "").strip())
        response = model.generate_content(
            prompt,
            generation_config=GenerationConfig(
                temperature=0,
                max_output_tokens=512,
                response_mime_type="application/json",
            ),
        )
        parsed = _extract_json_payload(getattr(response, "text", "") or "")
        category = str(parsed.get("category", "TRASH") or "TRASH").strip().upper()
        if category not in {"TARGET", "SPAM", "TRASH"}:
            category = "TRASH"

        score = _clamp_score(parsed.get("score", 0))
        reason = str(parsed.get("reason", "") or "").strip()
        intent = VERTEX_CATEGORY_TO_INTENT[category]

        return {
            "category": category,
            "reason": reason or "Vertex AI classifier result.",
            "score": score,
            "is_lead": category == "TARGET",
            "intent": intent,
            "matched_keywords": [],
            "classifier": "vertex_ai",
        }

    def classify_for_ingest(self, text: str) -> Dict[str, object]:
        if self.use_vertex_ai:
            try:
                return self.classify_with_vertex(text)
            except Exception as exc:
                result = self._legacy_to_ingest_result(text, classifier_name="keyword_fallback")
                result["vertex_error"] = str(exc)
                if result.get("reason"):
                    result["reason"] = f"{result['reason']} Vertex fallback reason: {exc}"
                return result
        return self._legacy_to_ingest_result(text, classifier_name="keyword")

    @staticmethod
    def suggest_comments(text: str) -> List[str]:
        preview = " ".join((text or "").split())[:120]
        return [
            f"Em thấy bài này khá phù hợp, anh/chị cho em xin thêm thông tin với ạ. ({preview}...)",
            "Mình đang quan tâm nội dung này, bạn có thể chia sẻ rõ hơn giúp mình không?",
            "Nếu tiện, bạn nhắn thêm chi tiết để mình xem mức độ phù hợp nhé.",
        ]
