"""
Receipt OCR extraction.

Uses Tesseract OCR (via pytesseract) to read an uploaded receipt image and
heuristically pull out a merchant name, a date, and a total amount.

To swap in Google Gemini / the ngrok AI Gateway for higher-precision
extraction (as described in the project abstract), replace the body of
`extract_receipt_fields()` with a call to that vision model and keep the
same return shape — every other part of the app (routes, templates) only
depends on the dict shape returned here.
"""
import re
from datetime import datetime

import pytesseract
from PIL import Image

CATEGORY_KEYWORDS = {
    "Food": ["restaurant", "cafe", "food", "kitchen", "pizza", "burger", "coffee", "diner", "eatery", "bakery"],
    "Groceries": ["mart", "grocery", "supermarket", "market", "store", "bazaar"],
    "Bills": ["electricity", "water board", "utility", "broadband", "recharge", "bill"],
    "Transport": ["uber", "ola", "taxi", "fuel", "petrol", "diesel", "metro", "fastag", "parking"],
    "Shopping": ["mall", "fashion", "apparel", "electronics", "retail", "outlet"],
    "Health": ["pharmacy", "hospital", "clinic", "medical", "drug"],
    "Entertainment": ["cinema", "movie", "theatre", "theater", "multiplex", "games"],
}

AMOUNT_PATTERNS = [
    r"(?:grand\s*total|total\s*amount|total|amount\s*due|net\s*payable|balance\s*due)\s*[:\-]?\s*(?:rs\.?|inr|₹|\$)?\s*([\d,]+\.\d{2}|[\d,]+)",
]

DATE_PATTERNS = [
    r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
    r"(\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})",
]


def _guess_category(text: str) -> str:
    lower = text.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(k in lower for k in keywords):
            return category
    return "Other"


def _guess_merchant(lines: list[str]) -> str:
    # Heuristic: the first non-empty line with mostly letters is usually the
    # store/merchant header on a printed receipt.
    for line in lines[:6]:
        cleaned = line.strip()
        if len(cleaned) >= 3 and sum(c.isalpha() for c in cleaned) >= 3:
            return cleaned[:120]
    return "Unknown Merchant"


def _guess_amount(text: str):
    lower = text.lower()
    candidates = []
    for pattern in AMOUNT_PATTERNS:
        for m in re.finditer(pattern, lower):
            raw = m.group(1).replace(",", "")
            try:
                candidates.append(float(raw))
            except ValueError:
                continue
    if candidates:
        return max(candidates)
    # fallback 1: largest currency-looking number anywhere in the receipt
    all_nums = re.findall(r"(?:rs\.?|inr|₹|\$)\s*([\d,]+\.\d{2})", lower)
    nums = []
    for n in all_nums:
        try:
            nums.append(float(n.replace(",", "")))
        except ValueError:
            pass
    if nums:
        return max(nums)
    # fallback 2: any decimal-looking number in the whole receipt (OCR often
    # mangles currency symbols/labels but usually keeps digits intact)
    any_decimals = re.findall(r"\b(\d{1,6}\.\d{2})\b", lower)
    nums = [float(n) for n in any_decimals if float(n) > 0]
    return max(nums) if nums else None


def _guess_date(text: str):
    for pattern in DATE_PATTERNS:
        m = re.search(pattern, text)
        if m:
            raw = m.group(1)
            for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y",
                        "%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y"):
                try:
                    return datetime.strptime(raw, fmt).date()
                except ValueError:
                    continue
    return None


def extract_receipt_fields(image_path: str) -> dict:
    """Run OCR on the given image path and return best-effort structured fields."""
    try:
        image = Image.open(image_path)
        raw_text = pytesseract.image_to_string(image)
    except Exception as exc:  # pragma: no cover - environment dependent
        return {
            "ok": False,
            "error": f"OCR failed: {exc}",
            "raw_text": "",
            "merchant": None,
            "amount": None,
            "date": None,
            "category": "Other",
        }

    lines = [l for l in raw_text.splitlines() if l.strip()]
    merchant = _guess_merchant(lines)
    amount = _guess_amount(raw_text)
    receipt_date = _guess_date(raw_text)
    category = _guess_category(raw_text)

    return {
        "ok": amount is not None,
        "error": None if amount is not None else "Could not confidently detect a total. Please review and enter manually.",
        "raw_text": raw_text.strip(),
        "merchant": merchant,
        "amount": amount,
        "date": receipt_date.isoformat() if receipt_date else None,
        "category": category,
    }
