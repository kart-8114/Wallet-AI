"""
Universal Bank Statement PDF Parser module.

Supports SBI, HDFC, ICICI, Axis, PayTM, Kotak, PNB, Canara, Bank of Baroda, and generic bank statement PDFs.
Uses flexible multi-column token extraction to handle Value/Post dates, details, UPI Ref Nos, Debit/Credit columns,
and Running Balances.
"""
import re
from datetime import datetime, date
from pypdf import PdfReader
from paytm_importer import parse_paytm_statement

CATEGORY_KEYWORDS = {
    "Food": ["swiggy", "zomato", "restaurant", "cafe", "food", "kitchen", "pizza", "burger", "coffee", "diner", "eatery", "bakery", "mcdonald", "starbucks", "kfc", "domino", "hotel"],
    "Groceries": ["blinkit", "zepto", "instamart", "bigbasket", "dmart", "mart", "grocery", "supermarket", "market", "bazaar", "provision", "store"],
    "Bills": ["electricity", "water", "utility", "broadband", "recharge", "bill", "airtel", "jio", "vodafone", "vi", "bescom", "tata play", "gas", "dth", "electricity bill"],
    "Transport": ["uber", "ola", "rapido", "fuel", "petrol", "diesel", "metro", "fastag", "parking", "hpcl", "iocl", "bpcl", "shell", "irctc", "railway"],
    "Shopping": ["amazon", "flipkart", "myntra", "meesho", "mall", "fashion", "apparel", "electronics", "retail", "outlet", "zara", "h&m", "trends", "croma", "reliance", "nykaa"],
    "Health": ["pharmacy", "hospital", "clinic", "medical", "drug", "apollo", "1mg", "pharmeasy", "medplus", "pathology", "lab", "health"],
    "Entertainment": ["cinema", "movie", "theatre", "theater", "multiplex", "games", "netflix", "spotify", "hotstar", "bookmyshow", "steam", "gaming"],
    "Salary": ["salary", "stipend", "payroll", "employer", "bonus", "dividend", "interest credit", "neft cr", "rtgs cr", "imps cr", "ach cr", "dep tfr"],
}

# Date regexes matching all common global & Indian statement date formats
DATE_PATTERNS = [
    (re.compile(r"\b(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})\b"), ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y", "%Y-%m-%d", "%m/%d/%Y"]),
    (re.compile(r"\b(\d{1,2}[\/\-\.\s]+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\/\-\.\s]+\d{2,4})\b", re.IGNORECASE), ["%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d %b %y", "%d-%B-%Y", "%d %B %Y"]),
]

# Decimal amount matching regex
AMOUNT_REGEX = re.compile(r"(?:rs\.?|inr|₹|\$)?\s*([+-]?\s*[\d,]+\.\d{2})\b", re.IGNORECASE)
INT_AMOUNT_REGEX = re.compile(r"\b([1-9]\d{1,5})\b")
UPI_REF_REGEX = re.compile(r"\b(\d{12})\b")

CREDIT_INDICATORS = ["dep tfr", "upi/cr", " credit", " cr", " deposit", "received", "refund", "salary", "cashback", "interest paid", "by transfer", "neft cr", "rtgs cr", "imps cr", "ach cr"]
DEBIT_INDICATORS = ["wdl tfr", "upi/dr", "atm wdl", " debit", " dr", " withdrawal", "pos", "to transfer", "paytm", "charges", "atm", "purchase", "debited"]

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


def extract_transactions_from_pdf(pdf_path: str, password: str = "") -> dict:
    try:
        # First check if it is a Paytm statement
        try:
            paytm_res = parse_paytm_statement(pdf_path, password=password)
            if paytm_res and paytm_res.get("ok") and paytm_res.get("total_parsed", 0) > 0:
                return paytm_res
        except ValueError as val_err:
            msg = str(val_err)
            if "PASSWORD_REQUIRED" in msg:
                return {
                    "ok": False,
                    "error": "This bank statement PDF is password-protected. Please enter your PDF password in the password field below and try again.",
                    "transactions": [],
                    "total_parsed": 0,
                }
            if "INCORRECT_PASSWORD" in msg:
                return {
                    "ok": False,
                    "error": "Incorrect PDF password. Please check your password and try again.",
                    "transactions": [],
                    "total_parsed": 0,
                }
        except Exception:
            pass

        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            if password:
                dec_res = reader.decrypt(password)
                if dec_res == 0:
                    return {
                        "ok": False,
                        "error": "Incorrect PDF password. Please check your password and try again.",
                        "transactions": [],
                        "total_parsed": 0,
                    }
            else:
                return {
                    "ok": False,
                    "error": "This bank statement PDF is password-protected. Please enter your PDF password in the password field below and try again.",
                    "transactions": [],
                    "total_parsed": 0,
                }

        full_text_lines = []
        for page in reader.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                stripped = line.strip()
                if stripped and not stripped.lower().startswith(("page ", "value date", "statement of account", "account summary", "opening balance", "closing balance")):
                    full_text_lines.append(stripped)

        if not full_text_lines:
            return {
                "ok": False,
                "error": "Could not extract readable text from this PDF. It appears to be a scanned image or password-protected statement. Please upload a standard digital PDF bank statement.",
                "transactions": [],
                "total_parsed": 0,
            }

        parsed_txns = []

        i = 0
        n = len(full_text_lines)
        while i < n:
            line = full_text_lines[i]
            date_str, d_start, d_end = _find_date_in_text(line)

            if not date_str and i + 1 < n:
                combined_line = f"{line} {full_text_lines[i+1]}"
                date_str, d_start, d_end = _find_date_in_text(combined_line)
                if date_str:
                    line = combined_line
                    i += 1

            if date_str:
                amount_matches = AMOUNT_REGEX.findall(line)
                
                if not amount_matches:
                    int_matches = INT_AMOUNT_REGEX.findall(line)
                    valid_ints = [m for m in int_matches if m != date_str and len(m) <= 6 and float(m) >= 10]
                    if valid_ints:
                        amount_matches = valid_ints

                if amount_matches:
                    running_balance = None
                    if len(amount_matches) >= 2:
                        try:
                            running_balance = round(float(CLEAN_AMOUNT_REGEX.sub("", amount_matches[-1])), 2)
                        except ValueError:
                            running_balance = None
                        raw_amount = amount_matches[-2]
                    else:
                        raw_amount = amount_matches[0]

                    try:
                        clean_amt = CLEAN_AMOUNT_REGEX.sub("", raw_amount)
                        amount_val = float(clean_amt)
                    except ValueError:
                        amount_val = 0.0

                    if amount_val > 0:
                        line_lower = line.lower()
                        
                        if any(k in line_lower for k in CREDIT_INDICATORS):
                            txn_type = "income"
                        elif any(k in line_lower for k in DEBIT_INDICATORS) or "-" in raw_amount:
                            txn_type = "expense"
                        else:
                            txn_type = "expense"

                        # Extract UPI Reference Number if present
                        upi_ref_match = UPI_REF_REGEX.search(line)
                        ref_no = upi_ref_match.group(1) if upi_ref_match else ""

                        # Clean merchant / details text
                        desc = line
                        for am in amount_matches:
                            desc = desc.replace(am, " ")
                        desc = desc.replace(date_str, " ")
                        desc = re.sub(r"\b(?:dr|cr|inr|rs\.?|balance|bal)\b", "", desc, flags=re.IGNORECASE)
                        merchant_clean = SPACE_REGEX.sub(" ", desc).strip()
                        
                        if len(merchant_clean) > 160:
                            merchant_clean = merchant_clean[:160]

                        category = _guess_category(merchant_clean.lower())
                        iso_date = parse_date_string(date_str)

                        parsed_txns.append({
                            "date": iso_date,
                            "merchant": merchant_clean or "Bank Transaction",
                            "type": txn_type,
                            "amount": round(amount_val, 2),
                            "category": category,
                            "reference": ref_no,
                            "running_balance": running_balance,
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
