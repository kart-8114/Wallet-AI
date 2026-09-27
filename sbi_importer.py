"""
Dedicated State Bank of India (SBI) PDF Statement Importer module.

Parses SBI multi-line tabular statements:
- Filters out SBI header/address metadata (e.g. 'Street,Gummaluru', 'Clear Balance').
- Group lines into transaction blocks starting with dates.
- Accurately classifies 'WDL TFR' / 'UPI/DR/' as Expense (debit) and 'DEP TFR' / 'UPI/CR/' as Income (credit).
- Extracts clean Merchant names (e.g. 'MS SAIRA', 'RAMAKRISHNA', 'TENTU AS'), 12-digit UPI Reference Numbers, transaction amounts, and running balances.
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
    "Salary": ["salary", "stipend", "payroll", "employer", "bonus", "dividend", "interest credit", "neft cr", "rtgs cr", "imps cr", "ach cr", "dep tfr"],
}

# Date regexes matching SBI date formats (e.g. 01/04/2026, 01-04-2026, 01 Apr 2026)
DATE_REGEX = re.compile(r"^\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})\b", re.IGNORECASE)
DATE_PARSE_PATTERNS = [
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%Y-%m-%d",
    "%d %b %Y", "%d %B %Y", "%d %b %y"
]

AMOUNT_DECIMAL_REGEX = re.compile(r"([\d,]+\.\d{2})")
UPI_REF_REGEX = re.compile(r"\b(\d{12})\b")
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

    for fmt in DATE_PARSE_PATTERNS:
        try:
            parsed = datetime.strptime(cleaned, fmt).date().isoformat()
            _date_cache[cleaned] = parsed
            return parsed
        except ValueError:
            continue

    fallback = date.today().isoformat()
    _date_cache[cleaned] = fallback
    return fallback


def extract_sbi_merchant(text: str) -> str:
    """Extract clean merchant or recipient name from SBI transaction text."""
    # Pattern for UPI: UPI/DR/601972040756/MS SAIRA/YESB/... or UPI/CR/741285721672/TENTU/SBIN/...
    upi_match = re.search(r"UPI\/(?:DR|CR)\/\d+\/([^\/]+)", text, re.IGNORECASE)
    if upi_match:
        raw_name = upi_match.group(1).strip()
        if len(raw_name) >= 2:
            return raw_name[:120]

    # Pattern for ATM Withdrawal
    if "atm" in text.lower() and "wdl" in text.lower():
        return "ATM Cash Withdrawal"

    # Fallback: clean clutter
    cleaned = re.sub(r"\b(?:wdl|tfr|dep|upi|dr|cr|sbin|punb|yesb|paytm|at|\d{12}|\d{10})\b", "", text, flags=re.IGNORECASE)
    cleaned = SPACE_REGEX.sub(" ", cleaned).strip()
    return cleaned[:120] if cleaned else "SBI Transaction"


def parse_sbi_statement(pdf_path: str, password: str = "") -> dict:
    try:
        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            if password:
                dec_res = reader.decrypt(password)
                if dec_res == 0:
                    return {
                        "ok": False,
                        "error": "Incorrect PDF password for SBI statement.",
                        "transactions": [],
                        "total_parsed": 0,
                    }
            else:
                return {
                    "ok": False,
                    "error": "This SBI bank statement is password-protected. Please enter your PDF password.",
                    "transactions": [],
                    "total_parsed": 0,
                }

        raw_lines = []
        for page in reader.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                s = line.strip()
                if s:
                    # Ignore SBI header/address clutter lines
                    s_lower = s.lower()
                    if any(skip in s_lower for skip in [
                        "street,", "gummaluru", "date of statement", "account name",
                        "clear balance", "opening balance", "closing balance",
                        "statement of account", "page no", "branch name", "ifs code"
                    ]):
                        continue
                    raw_lines.append(s)

        if not raw_lines:
            return {
                "ok": False,
                "error": "No readable text lines found in SBI statement.",
                "transactions": [],
                "total_parsed": 0,
            }

        # Group lines into transaction blocks starting with dates
        txn_blocks = []
        curr_block = []

        for line in raw_lines:
            if DATE_REGEX.match(line):
                if curr_block:
                    txn_blocks.append(" ".join(curr_block))
                    curr_block = []
            curr_block.append(line)

        if curr_block:
            txn_blocks.append(" ".join(curr_block))

        parsed_txns = []

        for block in txn_blocks:
            d_match = DATE_REGEX.match(block)
            if not d_match:
                continue

            raw_date = d_match.group(1)
            amounts = AMOUNT_DECIMAL_REGEX.findall(block)

            if amounts:
                # In SBI multi-column rows: First amount = Txn Amount, Last amount = Running Balance
                try:
                    amount_val = float(CLEAN_AMOUNT_REGEX.sub("", amounts[0]))
                except ValueError:
                    amount_val = 0.0

                if amount_val <= 0:
                    continue

                running_bal = None
                if len(amounts) >= 2:
                    try:
                        running_bal = round(float(CLEAN_AMOUNT_REGEX.sub("", amounts[-1])), 2)
                    except ValueError:
                        running_bal = None

                block_lower = block.lower()

                # Determine Type (Credit / DEP TFR = Income, WDL TFR / DR = Expense)
                if any(k in block_lower for k in ["dep tfr", "upi/cr/", "credit", "by transfer", "deposit"]):
                    txn_type = "income"
                elif any(k in block_lower for k in ["wdl tfr", "upi/dr/", "atm wdl", "debit", "withdrawal"]):
                    txn_type = "expense"
                else:
                    txn_type = "expense"

                # Extract 12-digit UPI Ref No
                ref_match = UPI_REF_REGEX.search(block)
                ref_no = ref_match.group(1) if ref_match else ""

                merchant = extract_sbi_merchant(block)
                category = _guess_category(f"{merchant} {block}".lower())
                iso_date = parse_date_string(raw_date)

                parsed_txns.append({
                    "date": iso_date,
                    "merchant": merchant,
                    "type": txn_type,
                    "amount": round(amount_val, 2),
                    "category": category,
                    "reference": ref_no,
                    "running_balance": running_bal,
                    "note": f"SBI Statement {f'[UPI Ref: {ref_no}]' if ref_no else ''}".strip(),
                })

        return {
            "ok": len(parsed_txns) > 0,
            "error": None if parsed_txns else "No readable SBI transactions found in this PDF.",
            "transactions": parsed_txns,
            "total_parsed": len(parsed_txns),
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to parse SBI statement: {str(exc)}",
            "transactions": [],
            "total_parsed": 0,
        }
