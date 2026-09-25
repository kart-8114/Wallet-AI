"""
Bank Statement PDF Parser module.

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

# Date regex patterns common in bank statements
DATE_REGEX_PATTERNS = [
    (r"\b(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})\b", ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%Y-%m-%d"]),
    (r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})\b", ["%d %b %Y", "%d %B %Y", "%d %b %y", "%d %B %y"]),
]

CREDIT_KEYWORDS = ["credit", "cr", "deposit", "received", "refund", "salary", "cashback", "interest paid"]


def _guess_category(text: str) -> str:
    lower = text.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(k in lower for k in keywords):
            return category
    return "Other"


def parse_date_string(date_str: str) -> str:
    cleaned = date_str.strip()
    for pattern, formats in DATE_REGEX_PATTERNS:
        if re.search(pattern, cleaned, re.IGNORECASE):
            for fmt in formats:
                try:
                    parsed = datetime.strptime(cleaned, fmt).date()
                    return parsed.isoformat()
                except ValueError:
                    continue
    return date.today().isoformat()


def extract_transactions_from_pdf(pdf_path: str) -> dict:
    try:
        reader = PdfReader(pdf_path)
        full_text_lines = []
        for page in reader.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                if line.strip():
                    full_text_lines.append(line.strip())

        if not full_text_lines:
            return {
                "ok": False,
                "error": "Could not extract any text from the PDF statement. It might be scanned/image-based or password protected.",
                "transactions": [],
                "total_parsed": 0,
            }

        parsed_txns = []
        
        # Regex to scan for rows starting with dates or containing transaction amounts
        # Typical line: 15/09/2024 UPI/Swiggy/12345 Dr 450.00 or 15/09/2024 Transfer to X 1,500.00
        row_regex = re.compile(
            r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})\s+(.+?)\s+([+-]?\s*(?:rs\.?|inr|₹|\$)?\s*[\d,]+\.\d{2})(?:\s+(cr|dr|credit|debit))?",
            re.IGNORECASE,
        )

        for line in full_text_lines:
            match = row_regex.search(line)
            if match:
                raw_date, raw_desc, raw_amount, raw_indicator = match.groups()
                
                # Parse amount
                amount_clean = re.sub(r"[^\d.]", "", raw_amount)
                try:
                    amount_val = float(amount_clean)
                except ValueError:
                    continue

                if amount_val <= 0:
                    continue

                # Determine type (expense vs income)
                line_lower = line.lower()
                indicator = (raw_indicator or "").lower()
                
                if "cr" in indicator or "credit" in indicator or any(k in line_lower for k in CREDIT_KEYWORDS):
                    txn_type = "income"
                elif "dr" in indicator or "debit" in indicator or "-" in raw_amount:
                    txn_type = "expense"
                else:
                    txn_type = "expense"  # default assumption for bank debits

                # Clean description / merchant
                merchant_clean = re.sub(r"\s+", " ", raw_desc).strip()
                if len(merchant_clean) > 120:
                    merchant_clean = merchant_clean[:120]

                category = _guess_category(merchant_clean)
                iso_date = parse_date_string(raw_date)

                parsed_txns.append({
                    "date": iso_date,
                    "merchant": merchant_clean or "Bank Transaction",
                    "type": txn_type,
                    "amount": round(amount_val, 2),
                    "category": category,
                })

        if not parsed_txns:
            # Fallback parser: scan line by line for any line with date and amount
            date_prefix_regex = re.compile(r"^(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})\b", re.IGNORECASE)
            amount_fallback_regex = re.compile(r"([\d,]+\.\d{2})")

            for line in full_text_lines:
                d_match = date_prefix_regex.match(line)
                if d_match:
                    raw_date = d_match.group(1)
                    amounts = amount_fallback_regex.findall(line)
                    if amounts:
                        last_amount = amounts[0].replace(",", "")
                        try:
                            amount_val = float(last_amount)
                            if amount_val > 0:
                                desc = line[len(raw_date):].strip()
                                desc = amount_fallback_regex.sub("", desc).strip()
                                line_lower = line.lower()
                                txn_type = "income" if any(k in line_lower for k in CREDIT_KEYWORDS) else "expense"
                                parsed_txns.append({
                                    "date": parse_date_string(raw_date),
                                    "merchant": desc[:120] or "Bank Transaction",
                                    "type": txn_type,
                                    "amount": round(amount_val, 2),
                                    "category": _guess_category(desc),
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
