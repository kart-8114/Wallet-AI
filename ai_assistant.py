"""
Smart AI Assistant.

This module answers user questions about their own spending history. It runs
entirely on-device using the user's transaction data plus a rule-based intent
matcher, so the app works with zero external API keys out of the box.

To connect the ngrok AI Gateway / Gemini 2.0 ("gpt-5.6-luna") backend
described in the project abstract, add your endpoint + key to config and
replace `generate_reply()`'s fallback branch with an HTTP call that passes
`build_context_summary(user)` as system context alongside the user's message.
"""
from collections import defaultdict
from datetime import date, timedelta

from models import Transaction


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


def generate_reply(user, message: str) -> str:
    msg = message.lower().strip()
    ctx = build_context_summary(user)

    if not ctx["txn_count_30d"]:
        return ("I don't see any transactions logged in the last 30 days yet. "
                "Add a few expenses or scan a receipt, and I'll be able to give you "
                "real insights on your spending.")

    if any(k in msg for k in ["save", "saving", "tip", "reduce", "cut"]):
        if ctx["top_category"]:
            cat, amt = ctx["top_category"]
            return (f"Your biggest spend in the last 30 days is **{cat}** at ₹{amt:,.2f}. "
                     f"Trimming that category by even 15% would free up roughly "
                     f"₹{amt * 0.15:,.2f}/month. Consider setting a Budget limit for "
                     f"{cat} on the Budgets page so you get an alert before you overspend.")
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
        return ("Head to the Budgets page to set a monthly limit per category — I'll track your "
                 "actual spend against it and flag anything trending over.")

    if any(k in msg for k in ["goal", "target"]):
        return ("You can create savings goals like an Emergency Fund or Vacation on the Goals page. "
                 "Log contributions there and I'll track your progress bar automatically.")

    if any(k in msg for k in ["hello", "hi", "hey"]):
        return "Hey! Ask me things like \"summarize my spending\" or \"how can I save more this month?\""

    # default fallback
    if ctx["top_category"]:
        cat, amt = ctx["top_category"]
        return (f"Here's a quick snapshot: ₹{ctx['expense_30d']:,.2f} spent in the last 30 days, "
                 f"with {cat} (₹{amt:,.2f}) as your top category. Ask me to \"summarize my spending\" "
                 f"or \"give me saving tips\" for more detail.")
    return "Ask me to summarize your spending, suggest savings tips, or check your budgets."
