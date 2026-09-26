"""
Universal Bank Statement PDF Parser module.

Supports SBI, HDFC, ICICI, Axis, PayTM, Kotak, PNB, Canara, Bank of Baroda, and generic bank statement PDFs.
Uses flexible token extraction to handle varying table layouts, value dates, running balances, credit/debit indicators,
and integer/decimal amount formats.
"""
import re
from datetime import datetime, date
from pypdf import PdfReader

CATEGORY_KEYWORDS = {
    "Food": ["swiggy", "zomato", "restaurant", "cafe", "food", "kitchen", "pizza", "burger", "coffee", "diner", "eatery", "bakery", "mcdonald", "starbucks", "kfc", "domino", "hotel"],
    "Groceries": ["blinkit", "zepto", "instamart", "bigbasket", "dmart", "mart", "grocery", "supermarket", "market", "bazaar", "provision", "store"],
    "Bills": ["electricity", "water", "utility", "broadband", "recharge", "bill", "airtel", "jio", "vodafone", "vi", "bescom", "tata play", "gas", "dth", "electricity bill"],
    "Transport": ["uber", "ola", "rapido", "fuel", "petrol", "diesel", "metro", "fastag", "parking", "hpcl", "iocl", "bpcl", "shell", "irctc", "railway"],
    "Shopping": ["amazon", "flipkart", "myntra", "meesho", "mall", "fashion", "apparel", "electronics", "retail", "outlet", "zara", "h&m", "trends", "croma", "reliance", "nykaa"],
    "Health": ["pharmacy", "hospital", "clinic", "medical", "drug", "apollo", "1mg", "pharmeasy", "medplus", "pathology", "lab", "health"],
    "Entertainment": ["cinema", "movie", "theatre", "theater", "multiplex", "games", "netflix", "spotify", "hotstar", "bookmyshow", "steam", "gaming"],
    "Salary": ["salary", "stipend", "payroll", "employer", "bonus", "dividend", "interest credit", "neft cr", "rtgs cr", "imps cr", "ach cr"],
}

# Date regexes matching all common global & Indian statement date formats
DATE_PATTERNS = [
    # 15/09/2024, 15-09-2024, 15.09.2024, 15/09/24, 2024-09-15
    (re.compile(r"\b(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})\b"), ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y", "%Y-%m-%d", "%m/%d/%Y"]),
    # 15-SEP-2024, 15-Sep-24, 15 SEP 2024, 15 Sep 24
    (re.compile(r"\b(\d{1,2}[\/\-\.\s]+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\/\-\.\s]+\d{2,4})\b", re.IGNORECASE), ["%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d %b %y", "%d-%B-%Y", "%d %B %Y"]),
]

# Amount matching regex: numbers like 1,500.00 or 1500.00 or 450.00
AMOUNT_REGEX = re.compile(r"(?:rs\.?|inr|₹|\$)?\s*([+-]?\s*[\d,]+\.\d{2})\b", re.IGNORECASE)
# Fallback integer amount regex for integers >= 10: e.g. 500 or 1200 (excluding 10-12 digit account/ref numbers)
INT_AMOUNT_REGEX = re.compile(r"\b([1-9]\d{1,5})\b")

CREDIT_INDICATORS = [" credit", " cr", " deposit", "received", "refund", "salary", "cashback", "interest paid", "by transfer", "neft cr", "rtgs cr", "imps cr", "ach cr", "credit card refund"]
DEBIT_INDICATORS = [" debit", " dr", " withdrawal", "pos", "upi/", "to transfer", "paytm", "charges", "atm", "purchase", "debited"]

CLEAN_AMOUNT_REGEX = re.compile(r"[^\d.]")
SPACE_REGEX = re.compile(r"\s+")

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

    for rx, formats in DATE_PATTERNS:
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


def _find_date_in_text(text: str):
    for rx, _ in DATE_PATTERNS:
        match = rx.search(text)
        if match:
            return match.group(1), match.start(), match.end()
    return None, -1, -1


def extract_transactions_from_pdf(pdf_path: str) -> dict:
    try:
        reader = PdfReader(pdf_path)
        full_text_lines = []
        for page in reader.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                stripped = line.strip()
                if stripped and not stripped.lower().startswith(("page ", "statement of account", "account summary", "opening balance", "closing balance")):
                    full_text_lines.append(stripped)

        if not full_text_lines:
            return {
                "ok": False,
                "error": "Could not extract readable text from this PDF. It appears to be a scanned image or password-protected statement. Please upload a standard digital PDF bank statement.",
                "transactions": [],
                "total_parsed": 0,
            }

        parsed_txns = []

        # Strategy 1 & 2: Single-line and Adjacent-line Token Scanner
        i = 0
        n = len(full_text_lines)
        while i < n:
            line = full_text_lines[i]
            date_str, d_start, d_end = _find_date_in_text(line)

            if not date_str and i + 1 < n:
                # Check sliding window: maybe line i has date and line i+1 has transaction description/amount
                combined_line = f"{line} {full_text_lines[i+1]}"
                date_str, d_start, d_end = _find_date_in_text(combined_line)
                if date_str:
                    line = combined_line
                    i += 1  # consume next line

            if date_str:
                # Find all decimal amounts in the line
                amount_matches = AMOUNT_REGEX.findall(line)
                
                if not amount_matches:
                    # Fallback to integer amounts if decimal amounts not found
                    int_matches = INT_AMOUNT_REGEX.findall(line)
                    # Filter out integers that look like years or small day/month numbers or long account numbers
                    valid_ints = [m for m in int_matches if m != date_str and len(m) <= 6 and float(m) >= 10]
                    if valid_ints:
                        amount_matches = valid_ints

                if amount_matches:
                    # If multiple amounts exist on the same line (e.g., Transaction Amount + Running Balance)
                    # In 3-column bank statements: [Date] [Description] [Amount] [Balance]
                    # The first or second number is the transaction amount.
                    raw_amount = amount_matches[0]
                    
                    try:
                        clean_amt = CLEAN_AMOUNT_REGEX.sub("", raw_amount)
                        amount_val = float(clean_amt)
                    except ValueError:
                        amount_val = 0.0

                    if amount_val > 0:
                        line_lower = line.lower()
                        
                        # Determine Credit vs Debit
                        if any(k in line_lower for k in CREDIT_INDICATORS):
                            txn_type = "income"
                        elif any(k in line_lower for k in DEBIT_INDICATORS) or "-" in raw_amount:
                            txn_type = "expense"
                        else:
                            txn_type = "expense"  # Default assumption for bank debits

                        # Extract description / merchant by stripping date & amount matches
                        desc = line
                        for am in amount_matches:
                            desc = desc.replace(am, " ")
                        desc = desc.replace(date_str, " ")
                        
                        # Strip common clutter words like 'Dr', 'Cr', 'INR', 'Rs.'
                        desc = re.sub(r"\b(?:dr|cr|inr|rs\.?|balance|bal)\b", "", desc, flags=re.IGNORECASE)
                        merchant_clean = SPACE_REGEX.sub(" ", desc).strip()
                        
                        if len(merchant_clean) > 120:
                            merchant_clean = merchant_clean[:120]

                        category = _guess_category(merchant_clean.lower())
                        iso_date = parse_date_string(date_str)

                        parsed_txns.append({
                            "date": iso_date,
                            "merchant": merchant_clean or "Bank Transaction",
                            "type": txn_type,
                            "amount": round(amount_val, 2),
                            "category": category,
                        })

            i += 1

        return {
            "ok": len(parsed_txns) > 0,
            "error": None if parsed_txns else "No readable transactions found in this PDF. Please ensure it is a valid bank statement with text columns.",
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
