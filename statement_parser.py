"""
High-Performance Bank Statement PDF Parser module.

Uses pypdf to extract text from bank statement PDFs (SBI, HDFC, ICICI, Axis, PayTM, generic statements)
and parses individual transaction entries (date, description, amount, credit/debit type, auto-assigned category).
"""
import re
from datetime import datetime, date
from pypdf import PdfReader

CATEGORY_KEYWORDS = {
    "Food": ["swiggy", "zomato", "restaurant", "cafe", "food", "kitchen", "pizza", "burger", "coffee", "diner", "eatery", "bakery", "mcdonald", "starbucks", "kfc", "domino"],
    "Groceries": ["blinkit", "zepto", "instamart", "bigbasket", "dmart", "mart", "grocery", "supermarket", "market", "bazaar", "provision"],
    "Bills": ["electricity", "water", "utility", "broadband", "recharge", "bill", "airtel", "jio", "vodafone", "bescom", "tata play", "gas", "dth"],
    "Transport": ["uber", "ola", "rapido", "fuel", "petrol", "diesel", "metro", "fastag", "parking", "hpcl", "iocl", "bpcl", "shell", "irctc"],
    "Shopping": ["amazon", "flipkart", "myntra", "meesho", "mall", "fashion", "apparel", "electronics", "retail", "outlet", "zara", "h&m", "trends", "croma", "reliance"],
    "Health": ["pharmacy", "hospital", "clinic", "medical", "drug", "apollo", "1mg", "pharmeasy", "medplus", "pathology", "lab"],
    "Entertainment": ["cinema", "movie", "theatre", "theater", "multiplex", "games", "netflix", "spotify", "hotstar", "bookmyshow", "steam"],
    "Salary": ["salary", "stipend", "payroll", "employer", "bonus", "dividend", "interest credit", "neft cr", "rtgs cr", "imps cr"],
}

# Pre-compiled Regex patterns for maximal parsing speed
DATE_REGEX_PATTERNS = [
    (re.compile(r"\b(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})\b", re.IGNORECASE), ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%Y-%m-%d"]),
    (re.compile(r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})\b", re.IGNORECASE), ["%d %b %Y", "%d %B %Y", "%d %b %y", "%d %B %y"]),
]

ROW_REGEX = re.compile(
    r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})\s+(.+?)\s+([+-]?\s*(?:rs\.?|inr|₹|\$)?\s*[\d,]+\.\d{2})(?:\s+(cr|dr|credit|debit))?",
    re.IGNORECASE,
)

DATE_PREFIX_REGEX = re.compile(r"^(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})\b", re.IGNORECASE)
AMOUNT_FALLBACK_REGEX = re.compile(r"([\d,]+\.\d{2})")
CLEAN_AMOUNT_REGEX = re.compile(r"[^\d.]")
SPACE_REGEX = re.compile(r"\s+")

CREDIT_KEYWORDS = ["credit", "cr", "deposit", "received", "refund", "salary", "cashback", "interest paid"]

_date_cache = {}


def _guess_category(text_lower: str) -> str:
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(k in text_lower for k in keywords):
            return category
    return "Other"


def parse_date_string(date_str: str) -> str:
    cleaned = date_str.strip()
    if cleaned in _date_cache:
        return _date_cache[cleaned]

    for rx, formats in DATE_REGEX_PATTERNS:
        if rx.search(cleaned):
            for fmt in formats:
                try:
                    parsed = datetime.strptime(cleaned, fmt).date().isoformat()
                    _date_cache[cleaned] = parsed
                    return parsed
                except ValueError:
                    continue
    
    fallback = date.today().isoformat()
    _date_cache[cleaned] = fallback
    return fallback


def extract_transactions_from_pdf(pdf_path: str) -> dict:
    try:
        reader = PdfReader(pdf_path)
        full_text_lines = []
        for page in reader.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                stripped = line.strip()
                if stripped:
                    full_text_lines.append(stripped)

        if not full_text_lines:
            return {
                "ok": False,
                "error": "Could not extract text from the PDF statement. It might be scanned/image-based or password protected.",
                "transactions": [],
                "total_parsed": 0,
            }

        parsed_txns = []

        for line in full_text_lines:
            match = ROW_REGEX.search(line)
            if match:
                raw_date, raw_desc, raw_amount, raw_indicator = match.groups()
                
                amount_clean = CLEAN_AMOUNT_REGEX.sub("", raw_amount)
                try:
                    amount_val = float(amount_clean)
                except ValueError:
                    continue

                if amount_val <= 0:
                    continue

                line_lower = line.lower()
                indicator = (raw_indicator or "").lower()
                
                if "cr" in indicator or "credit" in indicator or any(k in line_lower for k in CREDIT_KEYWORDS):
                    txn_type = "income"
                else:
                    txn_type = "expense"

                merchant_clean = SPACE_REGEX.sub(" ", raw_desc).strip()
                if len(merchant_clean) > 120:
                    merchant_clean = merchant_clean[:120]

                category = _guess_category(merchant_clean.lower())
                iso_date = parse_date_string(raw_date)

                parsed_txns.append({
                    "date": iso_date,
                    "merchant": merchant_clean or "Bank Transaction",
                    "type": txn_type,
                    "amount": round(amount_val, 2),
                    "category": category,
                })

        if not parsed_txns:
            # Fallback parser
            for line in full_text_lines:
                d_match = DATE_PREFIX_REGEX.match(line)
                if d_match:
                    raw_date = d_match.group(1)
                    amounts = AMOUNT_FALLBACK_REGEX.findall(line)
                    if amounts:
                        last_amount = amounts[0].replace(",", "")
                        try:
                            amount_val = float(last_amount)
                            if amount_val > 0:
                                desc = line[len(raw_date):].strip()
                                desc = AMOUNT_FALLBACK_REGEX.sub("", desc).strip()
                                line_lower = line.lower()
                                txn_type = "income" if any(k in line_lower for k in CREDIT_KEYWORDS) else "expense"
                                parsed_txns.append({
                                    "date": parse_date_string(raw_date),
                                    "merchant": desc[:120] or "Bank Transaction",
                                    "type": txn_type,
                                    "amount": round(amount_val, 2),
                                    "category": _guess_category(desc.lower()),
                                })
                        except ValueError:
                            pass

        return {
            "ok": len(parsed_txns) > 0,
            "error": None if parsed_txns else "No readable transactions found in this PDF. Please ensure it is a valid bank statement.",
            "transactions": parsed_txns,
            "total_parsed": len(parsed_txns),
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to process PDF statement: {str(exc)}",
            "transactions": [],
            "total_parsed": 0,
        }
