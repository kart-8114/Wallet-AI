import io
import csv
import os
import random
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                    session, flash, jsonify, send_file)
from werkzeug.utils import secure_filename

from extensions import db
from models import User, Transaction, Budget, Goal
from ocr import extract_receipt_fields
from ai_assistant import generate_reply

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

CATEGORIES = ["Food", "Groceries", "Bills", "Transport", "Shopping",
              "Health", "Entertainment", "Salary", "Other"]


def create_app():
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = os.environ.get("WALLET_AI_SECRET", "dev-secret-change-me")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'wallet_ai.db')}"
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    flask_app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8MB uploads
    os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)

    db.init_app(flask_app)

    with flask_app.app_context():
        db.create_all()

    register_routes(flask_app)
    return flask_app


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def current_user():
    uid = session.get("user_id")
    return User.query.get(uid) if uid else None


def register_routes(flask_app):

    @flask_app.context_processor
    def inject_globals():
        return {"current_user": current_user(), "categories": CATEGORIES}

    # ---------- Auth ----------
    @flask_app.route("/")
    def home():
        if "user_id" in session:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @flask_app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            first_name = request.form.get("first_name", "").strip()
            last_name = request.form.get("last_name", "").strip()
            email = request.form.get("email", "").strip().lower()
            phone = request.form.get("phone", "").strip()
            address = request.form.get("address", "").strip()
            city = request.form.get("city", "").strip()
            state = request.form.get("state", "").strip()
            zip_code = request.form.get("zip_code", "").strip()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm", "")

            if not first_name or not last_name or not email or not password:
                flash("First name, last name, email, and password are required.", "danger")
                return redirect(url_for("register"))
            if password != confirm:
                flash("Passwords do not match.", "danger")
                return redirect(url_for("register"))
            if len(password) < 6:
                flash("Password must be at least 6 characters.", "danger")
                return redirect(url_for("register"))
            if User.query.filter_by(email=email).first():
                flash("An account with that email already exists.", "danger")
                return redirect(url_for("register"))

            user = User(
                first_name=first_name, 
                last_name=last_name, 
                email=email, 
                phone_number=phone,
                address=address,
                city=city,
                state=state,
                zip_code=zip_code
            )
            user.set_password(password)
            user.otp_code = f"{random.randint(0, 999999):06d}"
            db.session.add(user)
            db.session.commit()

            session["pending_otp_user"] = user.id
            flash(f"Account created! Your 6-digit OTP is {user.otp_code} (simulated — normally emailed).", "info")
            return redirect(url_for("verify_otp"))
        return render_template("register.html")

    @flask_app.route("/verify-otp", methods=["GET", "POST"])
    def verify_otp():
        uid = session.get("pending_otp_user")
        if not uid:
            return redirect(url_for("login"))
        user = User.query.get(uid)
        if request.method == "POST":
            code = request.form.get("otp", "").strip()
            if code == user.otp_code:
                user.otp_verified = True
                db.session.commit()
                session.pop("pending_otp_user", None)
                flash("Email verified! Please log in.", "success")
                return redirect(url_for("login"))
            flash("Incorrect OTP. Please try again.", "danger")
        return render_template("verify_otp.html", user=user)

    @flask_app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = User.query.filter_by(email=email).first()
            if not user or not user.check_password(password):
                flash("Invalid email or password.", "danger")
                return redirect(url_for("login"))
            if not user.otp_verified:
                session["pending_otp_user"] = user.id
                flash("Please verify your email with the OTP first.", "warning")
                return redirect(url_for("verify_otp"))
            session["user_id"] = user.id
            flash(f"Welcome back, {user.first_name} {user.last_name}!", "success")
            return redirect(url_for("dashboard"))
        return render_template("login.html")

    @flask_app.route("/logout")
    def logout():
        session.clear()
        flash("Logged out successfully.", "info")
        return redirect(url_for("login"))

    @flask_app.route("/mpin", methods=["GET", "POST"])
    @login_required
    def setup_mpin():
        user = current_user()
        if request.method == "POST":
            pin = request.form.get("mpin", "").strip()
            confirm = request.form.get("confirm_mpin", "").strip()
            if len(pin) != 4 or not pin.isdigit():
                flash("MPIN must be exactly 4 digits.", "danger")
            elif pin != confirm:
                flash("MPINs do not match.", "danger")
            else:
                user.set_mpin(pin)
                db.session.commit()
                flash("MPIN set! You can now use it for quick access.", "success")
                return redirect(url_for("dashboard"))
        return render_template("mpin.html")

    @flask_app.route("/mpin-login", methods=["GET", "POST"])
    def mpin_login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            pin = request.form.get("mpin", "").strip()
            user = User.query.filter_by(email=email).first()
            if user and user.check_mpin(pin):
                session["user_id"] = user.id
                flash("Quick access granted.", "success")
                return redirect(url_for("dashboard"))
            flash("Invalid email or MPIN.", "danger")
        return render_template("mpin_login.html")

    # ---------- Dashboard ----------
    @flask_app.route("/dashboard")
    @login_required
    def dashboard():
        user = current_user()
        today = date.today()
        last_30 = today - timedelta(days=30)

        txns = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.date.desc()).all()
        recent = [t for t in txns if t.date >= last_30]
        total_expense = sum(t.amount for t in recent if t.type == "expense")
        total_income = sum(t.amount for t in recent if t.type == "income")
        balance = sum(t.amount for t in txns if t.type == "income") - sum(t.amount for t in txns if t.type == "expense")

        by_category = {}
        for t in recent:
            if t.type == "expense":
                by_category[t.category] = by_category.get(t.category, 0) + t.amount

        user_goals = Goal.query.filter_by(user_id=user.id).all()
        user_budgets = Budget.query.filter_by(user_id=user.id).all()
        budget_status = []
        for b in user_budgets:
            spent = sum(t.amount for t in recent if t.type == "expense" and t.category == b.category)
            budget_status.append({
                "category": b.category,
                "limit": b.monthly_limit,
                "spent": spent,
                "pct": min(100, round((spent / b.monthly_limit) * 100, 1)) if b.monthly_limit else 0,
            })

        return render_template(
            "dashboard.html",
            recent_txns=txns[:8],
            total_expense=round(total_expense, 2),
            total_income=round(total_income, 2),
            balance=round(balance, 2),
            by_category=by_category,
            goals=user_goals,
            budget_status=budget_status,
        )

    # ---------- Transactions ----------
    @flask_app.route("/transactions")
    @login_required
    def transactions():
        user = current_user()
        txns = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.date.desc()).all()
        return render_template("transactions.html", txns=txns)

    @flask_app.route("/add-expense", methods=["GET", "POST"])
    @login_required
    def add_expense():
        user = current_user()
        if request.method == "POST":
            try:
                amount = float(request.form.get("amount"))
            except (TypeError, ValueError):
                flash("Enter a valid amount.", "danger")
                return redirect(url_for("add_expense"))

            txn_date = request.form.get("date") or date.today().isoformat()
            t = Transaction(
                user_id=user.id,
                type=request.form.get("type", "expense"),
                category=request.form.get("category", "Other"),
                merchant=request.form.get("merchant", "").strip() or None,
                amount=amount,
                note=request.form.get("note", "").strip() or None,
                date=datetime.strptime(txn_date, "%Y-%m-%d").date(),
                source="manual",
            )
            db.session.add(t)
            db.session.commit()
            flash("Transaction added.", "success")
            return redirect(url_for("transactions"))
        return render_template("add_expense.html", today=date.today().isoformat())

    @flask_app.route("/transactions/<int:txn_id>/delete", methods=["POST"])
    @login_required
    def delete_transaction(txn_id):
        user = current_user()
        t = Transaction.query.filter_by(id=txn_id, user_id=user.id).first_or_404()
        db.session.delete(t)
        db.session.commit()
        flash("Transaction deleted.", "info")
        return redirect(url_for("transactions"))

    # ---------- OCR Receipt Scanner ----------
    @flask_app.route("/scan-receipt", methods=["GET", "POST"])
    @login_required
    def scan_receipt():
        result = None
        if request.method == "POST":
            file = request.files.get("receipt")
            if not file or file.filename == "":
                flash("Please choose a receipt image.", "danger")
                return redirect(url_for("scan_receipt"))
            filename = secure_filename(file.filename)
            path = os.path.join(UPLOAD_DIR, f"{session['user_id']}_{int(datetime.utcnow().timestamp())}_{filename}")
            file.save(path)
            result = extract_receipt_fields(path)
        return render_template("scan_receipt.html", result=result, today=date.today().isoformat())

    @flask_app.route("/scan-receipt/confirm", methods=["POST"])
    @login_required
    def confirm_receipt():
        user = current_user()
        try:
            amount = float(request.form.get("amount"))
        except (TypeError, ValueError):
            flash("Enter a valid amount before confirming.", "danger")
            return redirect(url_for("scan_receipt"))

        txn_date = request.form.get("date") or date.today().isoformat()
        t = Transaction(
            user_id=user.id,
            type="expense",
            category=request.form.get("category", "Other"),
            merchant=request.form.get("merchant", "").strip() or "Unknown Merchant",
            amount=amount,
            note="Logged via OCR receipt scan",
            date=datetime.strptime(txn_date, "%Y-%m-%d").date(),
            source="ocr",
        )
        db.session.add(t)
        db.session.commit()
        flash("Receipt logged to your transactions.", "success")
        return redirect(url_for("transactions"))

    # ---------- Analytics ----------
    @flask_app.route("/analytics")
    @login_required
    def analytics():
        user = current_user()
        today = date.today()
        start = today - timedelta(days=29)
        txns = Transaction.query.filter_by(user_id=user.id).filter(Transaction.date >= start).all()

        daily = {}
        d = start
        while d <= today:
            daily[d.isoformat()] = 0.0
            d += timedelta(days=1)
        for t in txns:
            if t.type == "expense":
                daily[t.date.isoformat()] = daily.get(t.date.isoformat(), 0) + t.amount

        by_category = {}
        income_total = 0.0
        expense_total = 0.0
        for t in txns:
            if t.type == "expense":
                by_category[t.category] = by_category.get(t.category, 0) + t.amount
                expense_total += t.amount
            else:
                income_total += t.amount

        return render_template(
            "analytics.html",
            daily_labels=list(daily.keys()),
            daily_values=[round(v, 2) for v in daily.values()],
            cat_labels=list(by_category.keys()),
            cat_values=[round(v, 2) for v in by_category.values()],
            income_total=round(income_total, 2),
            expense_total=round(expense_total, 2),
        )

    # ---------- Budgets ----------
    @flask_app.route("/budgets", methods=["GET", "POST"])
    @login_required
    def budgets():
        user = current_user()
        if request.method == "POST":
            category = request.form.get("category")
            try:
                limit = float(request.form.get("monthly_limit"))
            except (TypeError, ValueError):
                flash("Enter a valid limit.", "danger")
                return redirect(url_for("budgets"))
            existing = Budget.query.filter_by(user_id=user.id, category=category).first()
            if existing:
                existing.monthly_limit = limit
            else:
                db.session.add(Budget(user_id=user.id, category=category, monthly_limit=limit))
            db.session.commit()
            flash("Budget saved.", "success")
            return redirect(url_for("budgets"))

        today = date.today()
        last_30 = today - timedelta(days=30)
        recent = Transaction.query.filter_by(user_id=user.id, type="expense").filter(Transaction.date >= last_30).all()
        rows = []
        for b in Budget.query.filter_by(user_id=user.id).all():
            spent = sum(t.amount for t in recent if t.category == b.category)
            rows.append({
                "id": b.id, "category": b.category, "limit": b.monthly_limit,
                "spent": round(spent, 2),
                "pct": min(100, round((spent / b.monthly_limit) * 100, 1)) if b.monthly_limit else 0,
            })
        return render_template("budgets.html", rows=rows)

    @flask_app.route("/budgets/<int:budget_id>/delete", methods=["POST"])
    @login_required
    def delete_budget(budget_id):
        user = current_user()
        b = Budget.query.filter_by(id=budget_id, user_id=user.id).first_or_404()
        db.session.delete(b)
        db.session.commit()
        return redirect(url_for("budgets"))

    # ---------- Goals ----------
    @flask_app.route("/goals", methods=["GET", "POST"])
    @login_required
    def goals():
        user = current_user()
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            try:
                target = float(request.form.get("target_amount"))
            except (TypeError, ValueError):
                flash("Enter a valid target amount.", "danger")
                return redirect(url_for("goals"))
            target_date = request.form.get("target_date") or None
            g = Goal(
                user_id=user.id, title=title, target_amount=target,
                target_date=datetime.strptime(target_date, "%Y-%m-%d").date() if target_date else None,
            )
            db.session.add(g)
            db.session.commit()
            flash("Goal created.", "success")
            return redirect(url_for("goals"))
        rows = Goal.query.filter_by(user_id=user.id).all()
        return render_template("goals.html", goals=rows)

    @flask_app.route("/goals/<int:goal_id>/contribute", methods=["POST"])
    @login_required
    def contribute_goal(goal_id):
        user = current_user()
        g = Goal.query.filter_by(id=goal_id, user_id=user.id).first_or_404()
        try:
            amt = float(request.form.get("amount"))
        except (TypeError, ValueError):
            flash("Enter a valid amount.", "danger")
            return redirect(url_for("goals"))
        g.saved_amount = (g.saved_amount or 0) + amt
        db.session.commit()
        flash(f"Added ₹{amt:,.2f} to {g.title}.", "success")
        return redirect(url_for("goals"))

    @flask_app.route("/goals/<int:goal_id>/delete", methods=["POST"])
    @login_required
    def delete_goal(goal_id):
        user = current_user()
        g = Goal.query.filter_by(id=goal_id, user_id=user.id).first_or_404()
        db.session.delete(g)
        db.session.commit()
        return redirect(url_for("goals"))

    # ---------- AI Chat Assistant ----------
    @flask_app.route("/chat")
    @login_required
    def chat():
        return render_template("chat.html")

    @flask_app.route("/api/chat", methods=["POST"])
    @login_required
    def api_chat():
        user = current_user()
        message = (request.json or {}).get("message", "")
        reply = generate_reply(user, message)
        return jsonify({"reply": reply})

    # ---------- Export ----------
    @flask_app.route("/export")
    @login_required
    def export_page():
        return render_template("export.html")

    @flask_app.route("/export/csv")
    @login_required
    def export_csv():
        user = current_user()
        txns = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.date.desc()).all()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Date", "Type", "Category", "Merchant", "Amount", "Note", "Source"])
        for t in txns:
            writer.writerow([t.date.isoformat(), t.type, t.category, t.merchant or "", t.amount, t.note or "", t.source])
        mem = io.BytesIO(buf.getvalue().encode("utf-8"))
        return send_file(mem, mimetype="text/csv", as_attachment=True,
                          download_name=f"wallet_ai_export_{date.today().isoformat()}.csv")

    @flask_app.route("/export/xlsx")
    @login_required
    def export_xlsx():
        import openpyxl
        user = current_user()
        txns = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.date.desc()).all()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Transactions"
        ws.append(["ID", "Type", "Category", "Merchant", "Amount", "Note", "Date", "Source"])
        for t in txns:
            ws.append([t.id, t.type, t.category, t.merchant or "", t.amount, t.note or "", t.date.isoformat(), t.source])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                          as_attachment=True,
                          download_name=f"wallet_ai_export_{date.today().isoformat()}.xlsx")

    # ---------- Settings ----------
    @flask_app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        user = current_user()
        if request.method == "POST":
            user.theme = "dark" if request.form.get("theme") == "dark" else "light"
            user.first_name = request.form.get("first_name", "").strip()
            user.last_name = request.form.get("last_name", "").strip()
            user.phone_number = request.form.get("phone", "").strip()
            user.address = request.form.get("address", "").strip()
            user.city = request.form.get("city", "").strip()
            user.state = request.form.get("state", "").strip()
            user.zip_code = request.form.get("zip_code", "").strip()
            db.session.commit()
            flash("Preferences saved.", "success")
            return redirect(url_for("settings"))
        return render_template("settings.html")


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
