from typing import Dict, List


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


def _match_keywords(text: str, keywords: List[str]) -> List[str]:
    lowered = text.lower()
    return [keyword for keyword in keywords if keyword in lowered]


class LeadClassifier:
    def __init__(self) -> None:
        self.borrower_keywords = BORROWER_KEYWORDS
        self.negative_keywords = NEGATIVE_LENDER_KEYWORDS

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

    @staticmethod
    def suggest_comments(text: str) -> List[str]:
        preview = " ".join((text or "").split())[:120]
        return [
            f"Em thấy bài này khá phù hợp, anh/chị cho em xin thêm thông tin với ạ. ({preview}...)",
            "Mình đang quan tâm nội dung này, bạn có thể chia sẻ rõ hơn giúp mình không?",
            "Nếu tiện, bạn nhắn thêm chi tiết để mình xem mức độ phù hợp nhé.",
        ]
