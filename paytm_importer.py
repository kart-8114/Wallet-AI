"""
Paytm Statement PDF Importer module.

Uses PyMuPDF (fitz) to extract and parse Paytm UPI PDF statements:
- Extracts dates, times, descriptions, merchants/recipients, amounts, debit/credit signs.
- Map Paytm '# Tags' to Wallet AI categories.
- Ignores 'Self Transfer' transactions.
- Extracts UPI Ref Nos for strict duplicate protection.
"""
import re
from datetime import datetime, date
import fitz  # PyMuPDF


CATEGORY_MAP = {
    "food": "Food",
    "groceries": "Groceries",
    "shopping": "Shopping",
    "travel": "Transport",
    "transport": "Transport",
    "fuel": "Transport",
    "entertainment": "Entertainment",
    "education": "Other",
    "medical": "Health",
    "health": "Health",
    "bills": "Bills",
    "utilities": "Bills",
    "financial services": "Bills",
    "money transfer": "Other",
    "money received": "Salary",
    "self transfer": "Self Transfer",
    "recharge": "Bills",
    "insurance": "Bills",
}


def extract_pdf_text(pdf_path: str) -> str:
    """Extract text from every page of the Paytm PDF using fitz."""
    doc = fitz.open(pdf_path)
    pages = []
    for page in doc:
        pages.append(page.get_text("text") or "")
    doc.close()
    return "\n".join(pages)


def get_statement_years(text: str):
    """Determine start and end years from Paytm statement header."""
    pattern = re.search(
        r"Paytm Statement for\s+\d{1,2}\s+[A-Z]{3}'(\d{2})\s*-\s*\d{1,2}\s+[A-Z]{3}'(\d{2})",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not pattern:
        curr_yr = datetime.now().year
        return curr_yr, curr_yr

    start_yr = 2000 + int(pattern.group(1))
    end_yr = 2000 + int(pattern.group(2))
    return start_yr, end_yr


def convert_date(date_text: str, start_year: int, end_year: int):
    """Convert Paytm date strings like '24 Sep' to ISO date strings."""
    try:
        parts = date_text.strip().split()
        if len(parts) < 2:
            return None
        day = int(parts[0])
        month_str = parts[1][:3]
        month_num = datetime.strptime(month_str, "%b").month

        if start_year != end_year:
            year = start_year if month_num >= 9 else end_year
        else:
            year = start_year

        return datetime(year, month_num, day).date().isoformat()
    except Exception:
        return None


def normalize_category(tag: str) -> str:
    if not tag:
        return "Other"
    clean_tag = tag.replace("#", "").strip().lower()
    return CATEGORY_MAP.get(clean_tag, clean_tag.title() if clean_tag else "Other")


def detect_transaction_type(description: str, tag: str, amount_str: str) -> str:
    combined = f"{description or ''} {tag or ''}".lower()

    if "self transfer" in combined:
        return "self_transfer"
    if amount_str.startswith("+") or "received from" in combined or "money received" in combined:
        return "income"
    if amount_str.startswith("-") or "paid to" in combined or "money sent to" in combined:
        return "expense"
    return "expense"


def clean_amount(amount_str: str) -> float:
    if not amount_str:
        return 0.0
    cleaned = amount_str.replace("Rs.", "").replace("Rs", "").replace(",", "").replace("+", "").replace("-", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def extract_merchant(description: str) -> str:
    desc = (description or "").strip()
    patterns = [
        r"Paid to\s+(.+)",
        r"Money sent to\s+(.+)",
        r"Received from\s+(.+)",
        r"Transferred to\s+(.+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, desc, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return desc


def parse_transaction_block(block: str, start_year: int, end_year: int):
    lines = [l.strip() for l in block.splitlines() if l.strip()]
    if not lines:
        return None

    # Date
    date_match = re.search(r"\b(\d{1,2}\s+[A-Z][a-z]{2})\b", block)
    if not date_match:
        return None

    iso_date = convert_date(date_match.group(1), start_year, end_year)
    if not iso_date:
        return None

    # Time
    time_match = re.search(r"\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b", block, re.IGNORECASE)
    transaction_time = time_match.group(1) if time_match else ""

    # Amount
    amount_match = re.search(r"([+-])\s*Rs\.?\s*([\d,]+(?:\.\d+)?)", block, re.IGNORECASE)
    if not amount_match:
        return None

    sign = amount_match.group(1)
    raw_num = amount_match.group(2)
    amount_val = clean_amount(sign + raw_num)
    if amount_val <= 0:
        return None

    # Description
    description = ""
    for line in lines:
        if any(line.startswith(prefix) for prefix in ["Paid to ", "Money sent to ", "Received from ", "Transferred to "]):
            description = line
            break

    if not description:
        description = lines[0] if lines else "Paytm Transaction"

    # UPI Ref No
    ref_match = re.search(r"UPI Ref No:\s*([0-9]+)", block, re.IGNORECASE)
    reference = ref_match.group(1).strip() if ref_match else ""

    # Tag
    tag_match = re.search(r"Tag:\s*#\s*([^\r\n]+)", block, re.IGNORECASE)
    tag = tag_match.group(1).strip() if tag_match else ""

    # Category & Type
    category = normalize_category(tag)
    txn_kind = detect_transaction_type(description, tag, sign + raw_num)

    # Ignore Self Transfers
    if txn_kind == "self_transfer":
        return None

    merchant = extract_merchant(description)

    return {
        "date": iso_date,
        "time": transaction_time,
        "merchant": merchant[:160] or "Paytm Transaction",
        "amount": round(amount_val, 2),
        "type": txn_kind,
        "category": category,
        "tag": tag,
        "reference": reference,
        "note": f"Paytm Statement {f'(Tag: #{tag})' if tag else ''} {f'[UPI Ref: {reference}]' if reference else ''}".strip(),
    }


def parse_paytm_statement(pdf_path: str) -> dict:
    try:
        text = extract_pdf_text(pdf_path)
        if not text or "paytm" not in text.lower():
            return {
                "ok": False,
                "error": "Not a valid Paytm PDF statement.",
                "transactions": [],
                "total_parsed": 0,
            }

        start_yr, end_yr = get_statement_years(text)
        date_pattern = re.compile(r"(?m)^\s*(\d{1,2}\s+[A-Z][a-z]{2})\s*$")
        matches = list(date_pattern.finditer(text))

        parsed_txns = []
        for idx, match in enumerate(matches):
            start_pos = match.start()
            end_pos = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            block = text[start_pos:end_pos]
            txn = parse_transaction_block(block, start_yr, end_yr)
            if txn:
                parsed_txns.append(txn)

        # Deduplicate inside PDF using reference number
        unique = {}
        for t in parsed_txns:
            ref = t.get("reference")
            key = ref if ref else f"{t['date']}_{t['merchant']}_{t['amount']}_{t['type']}"
            unique[key] = t

        txns_list = list(unique.values())

        return {
            "ok": len(txns_list) > 0,
            "error": None if txns_list else "No readable transactions found in this Paytm PDF statement.",
            "transactions": txns_list,
            "total_parsed": len(txns_list),
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to parse Paytm statement: {str(exc)}",
            "transactions": [],
            "total_parsed": 0,
        }
