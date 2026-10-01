"""
Smart AI Assistant & Automated Budget Creation Engine.

This module answers user questions about spending history and executes automated
budget creation and management actions in the database.
"""
import os
import re
from collections import defaultdict
from datetime import date, timedelta

from extensions import db
from models import Transaction, Budget

# Optional: Google GenAI integration
try:
    from google import genai
    GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
    if GEMINI_KEY and not GEMINI_KEY.strip().startswith("Paste your"):
        client = genai.Client(api_key=GEMINI_KEY.strip())
    else:
        client = None
except Exception:
    client = None

STANDARD_CATEGORIES = ["Food", "Groceries", "Bills", "Transport", "Shopping", "Health", "Entertainment", "Other"]


def build_context_summary(user) -> dict:
    today = date.today()
    last_30 = today - timedelta(days=30)
    prev_30_start = today - timedelta(days=60)

    txns = Transaction.query.filter_by(user_id=user.id).all()
    recent = [t for t in txns if t.date >= last_30]
    prev = [t for t in txns if prev_30_start <= t.date < last_30]

    def totals(rows, ttype):
        return sum(t.amount for t in rows if t.type == ttype)

    by_category = defaultdict(float)
    for t in recent:
        if t.type == "expense":
            by_category[t.category] += t.amount

    top_category = max(by_category.items(), key=lambda kv: kv[1]) if by_category else None

    return {
        "expense_30d": round(totals(recent, "expense"), 2),
        "income_30d": round(totals(recent, "income"), 2),
        "expense_prev_30d": round(totals(prev, "expense"), 2),
        "by_category": dict(by_category),
        "top_category": top_category,
        "txn_count_30d": len(recent),
    }


def _pct_change(new, old):
    if old == 0:
        return None
    return round(((new - old) / old) * 100, 1)


def _round_up_budget(amount: float) -> float:
    """Formula: 15% buffer above 30-day actual spend, rounded up to standard intervals."""
    if amount <= 0:
        return 1000.0
    suggested = amount * 1.15
    if suggested <= 500:
        return 500.0
    elif suggested <= 2000:
        return float(int((suggested + 499) // 500) * 500)
    else:
        return float(int((suggested + 999) // 1000) * 1000)


def auto_create_smart_budgets(user, ctx: dict) -> str:
    """Analyze actual 30-day spending per category and save smart budget limits in DB."""
    by_category = ctx.get("by_category", {})
    created_budgets = []
    total_budget_sum = 0.0

    target_categories = list(by_category.keys()) if by_category else ["Food", "Groceries", "Bills", "Transport", "Shopping"]

    for cat in target_categories:
        spend = by_category.get(cat, 0.0)
        suggested_limit = _round_up_budget(spend)

        existing = Budget.query.filter_by(user_id=user.id, category=cat).first()
        if existing:
            existing.monthly_limit = suggested_limit
        else:
            b = Budget(user_id=user.id, category=cat, monthly_limit=suggested_limit)
            db.session.add(b)

        created_budgets.append((cat, suggested_limit, spend))
        total_budget_sum += suggested_limit

    db.session.commit()

    lines = ["Done! I created your monthly budgets based on your recent spending:\n"]
    for cat, limit, spend in created_budgets:
        spend_str = f" (spent: ₹{spend:,.2f})" if spend > 0 else ""
        lines.append(f"• **{cat}** — ₹{limit:,.0f}{spend_str}")

    lines.append(f"\n**Total monthly budget** — ₹{total_budget_sum:,.0f}.")
    lines.append("\nYou can edit these limits anytime on the Budgets page!")
    return "\n".join(lines)


def set_explicit_category_budgets(user, message: str) -> str:
    """Extract explicit category and amount pairs like 'set food to 5000 and groceries to 3000'."""
    pattern = re.compile(
        r"(food|groceries|bills|transport|shopping|health|entertainment|other)\s*(?:budget)?\s*(?:to|=|\:)?\s*(?:rs\.?|inr|₹|\$)?\s*(\d+)",
        re.IGNORECASE,
    )
    matches = pattern.findall(message)
    if not matches:
        return ""

    updated_items = []
    for cat_raw, amt_str in matches:
        try:
            amt_val = float(amt_str)
            if amt_val <= 0:
                continue
            cat_clean = cat_raw.strip().title()

            existing = Budget.query.filter_by(user_id=user.id, category=cat_clean).first()
            if existing:
                existing.monthly_limit = amt_val
            else:
                b = Budget(user_id=user.id, category=cat_clean, monthly_limit=amt_val)
                db.session.add(b)

            updated_items.append((cat_clean, amt_val))
        except ValueError:
            continue

    if not updated_items:
        return ""

    db.session.commit()
    summary_str = ", ".join([f"**{c}**: ₹{a:,.0f}" for c, a in updated_items])
    return f"Done! I updated your monthly budget limits: {summary_str}. You can view them on the Budgets page."


def check_budget_performance(user, ctx: dict) -> str:
    """Report actual spending vs budget limits for the user."""
    budgets = Budget.query.filter_by(user_id=user.id).all()
    if not budgets:
        return ("You don't have any budgets set yet. Just say **\"make my budgets\"** and "
                "I'll automatically generate smart limits based on your spending history!")

    by_cat = ctx.get("by_category", {})
    lines = ["Here's how your budgets are doing this month:\n"]
    for b in budgets:
        spent = by_cat.get(b.category, 0.0)
        pct = min(100, round((spent / b.monthly_limit) * 100, 1)) if b.monthly_limit else 0
        status = "OVER BUDGET 🚨" if spent > b.monthly_limit else ("Warning ⚠️" if pct >= 80 else "On Track ✅")
        lines.append(f"• **{b.category}**: ₹{spent:,.2f} of ₹{b.monthly_limit:,.0f} ({pct}% used - {status})")

    return "\n".join(lines)


def generate_reply(user, message: str) -> str:
    msg = message.lower().strip()
    ctx = build_context_summary(user)

    # 1. Check for explicit budget creation requests ("set food to 5000")
    explicit_res = set_explicit_category_budgets(user, message)
    if explicit_res:
        return explicit_res

    # 2. Check for automatic budget generation intent ("make my budgets", "create budgets")
    make_budget_keywords = [
        "make my budget", "make my budgets", "create my budget", "create my budgets",
        "set my budget", "set my budgets", "create budget", "create budgets",
        "make budget", "make budgets", "auto budget", "set budgets for me",
        "create budgets based on my spending", "make a budget for me", "set limits for my categories"
    ]
    if any(k in msg for k in make_budget_keywords):
        return auto_create_smart_budgets(user, ctx)

    # 3. Check for budget status queries ("how are my budgets doing?", "check my budgets")
    if any(k in msg for k in ["how are my budgets", "check my budget", "check budgets", "budget status"]):
        return check_budget_performance(user, ctx)

    # If Gemini client is available and query is general, use Gemini
    if client:
        try:
            prompt = f"""
            You are a helpful personal finance AI assistant for {user.first_name}.
            User spending summary (last 30 days):
            - Total Spent: ₹{ctx['expense_30d']:,.2f}
            - Total Income: ₹{ctx['income_30d']:,.2f}
            - Top Category: {ctx['top_category'][0] if ctx['top_category'] else 'N/A'} (₹{ctx['top_category'][1] if ctx['top_category'] else 0:,.2f})
            - Prev 30d Spending: ₹{ctx['expense_prev_30d']:,.2f}
            - Full Breakdown: {ctx['by_category']}

            User message: "{message}"

            Provide a concise, supportive, and data-driven response in 2-3 sentences. 
            Use ₹ for currency. If they ask for tips, suggest specific ways to save in their top categories.
            """
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt
            )
            return response.text.strip()
        except Exception:
            pass

    # ---------- Rule-based Fallback ----------
    if any(k in msg for k in ["save", "saving", "tip", "reduce", "cut"]):
        if ctx["top_category"]:
            cat, amt = ctx["top_category"]
            return (f"Your biggest spend in the last 30 days is **{cat}** at ₹{amt:,.2f}. "
                     f"Trimming that category by even 15% would free up roughly "
                     f"₹{amt * 0.15:,.2f}/month. Say **\"make my budgets\"** and I'll "
                     f"automatically set smart limits for you!")
        return "Log a few more transactions and I'll point out where you can realistically cut back."

    if any(k in msg for k in ["summary", "summarize", "overview", "how am i doing", "spending"]):
        change = _pct_change(ctx["expense_30d"], ctx["expense_prev_30d"])
        trend = ""
        if change is not None:
            direction = "up" if change > 0 else "down"
            trend = f" That's {direction} {abs(change)}% versus the previous 30 days."
        return (f"In the last 30 days you spent ₹{ctx['expense_30d']:,.2f} against "
                 f"₹{ctx['income_30d']:,.2f} of income across {ctx['txn_count_30d']} transactions."
                 f"{trend}")

    if any(k in msg for k in ["budget"]):
        return ("Just say **\"make my budgets\"** and I'll automatically generate smart monthly "
                 "category limits based on your actual spending history!")

    if any(k in msg for k in ["goal", "target"]):
        return ("You can create savings goals like an Emergency Fund or Vacation on the Goals page. "
                 "Log contributions there and I'll track your progress bar automatically.")

    if any(k in msg for k in ["hello", "hi", "hey"]):
        return "Hey! Ask me things like \"make my budgets\", \"summarize my spending\", or \"give me saving tips\"."

    # default fallback
    if ctx["top_category"]:
        cat, amt = ctx["top_category"]
        return (f"Here's a quick snapshot: ₹{ctx['expense_30d']:,.2f} spent in the last 30 days, "
                 f"with {cat} (₹{amt:,.2f}) as your top category. Say **\"make my budgets\"** "
                 f"to set auto-budget limits!")
    return "Ask me to \"make my budgets\", summarize your spending, or check your savings goals."
