import io
import csv
import os
import re
import random
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                    session, flash, jsonify, send_file)
from werkzeug.utils import secure_filename
from sqlalchemy import text

from extensions import db
from models import User, Transaction, Budget, Goal, BankAccount
from ocr import extract_receipt_fields
from statement_parser import extract_transactions_from_pdf
from ai_assistant import generate_reply, build_context_summary, auto_create_smart_budgets

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

CATEGORIES = ["Food", "Groceries", "Bills", "Transport", "Shopping",
              "Health", "Entertainment", "Salary", "Other"]


def create_app():
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or os.environ.get("WALLET_AI_SECRET", "dev-secret-change-me")
    
    # Smarter Database Configuration
    # We check for multiple common names Render uses for Postgres
    database_url = (
        os.environ.get("DATABASE_URL") or 
        os.environ.get("DATABASE_PRIVATE_URL") or 
        os.environ.get("DATABASE_PUBLIC_URL")
    )
    
    if database_url:
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql+psycopg2://", 1)
        elif database_url.startswith("postgresql://") and not database_url.startswith("postgresql+"):
            database_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = database_url or f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'wallet_ai.db')}"
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    flask_app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8MB uploads
    os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)

    db.init_app(flask_app)

    with flask_app.app_context():
        try:
            # Diagnostic Log
            engine_name = db.engine.url.drivername
            print(f"DATABASE DIAGNOSTIC: Using engine {engine_name}")
            if "sqlite" in engine_name:
                print(f"WARNING: App is using SQLite. Data will NOT persist on Render restarts.")
            else:
                print(f"SUCCESS: App is using {engine_name}. Data will persist.")

            db.create_all()

            # Auto-migrate missing columns for existing PostgreSQL / SQLite tables
            try:
                if "postgres" in engine_name:
                    db.session.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS reference VARCHAR(100);"))
                    db.session.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS running_balance FLOAT;"))
                    db.session.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS mpin_hash VARCHAR(255);"))
                    db.session.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS theme VARCHAR(10) DEFAULT 'light';"))
                    db.session.commit()
                elif "sqlite" in engine_name:
                    try:
                        db.session.execute(text("ALTER TABLE transactions ADD COLUMN reference VARCHAR(100);"))
                    except Exception:
                        pass
                    try:
                        db.session.execute(text("ALTER TABLE transactions ADD COLUMN running_balance FLOAT;"))
                    except Exception:
                        pass
                    db.session.commit()
            except Exception as mig_err:
                print(f"AUTO-MIGRATION NOTICE: {mig_err}")
                db.session.rollback()

            # Seed Admin User
            admin_email = "admin@wallet.ai"
            if not User.query.filter_by(email=admin_email).first():
                admin = User(
                    first_name="System",
                    last_name="Admin",
                    email=admin_email,
                    otp_verified=True,
                    is_admin=True
                )
                admin.set_password("Admin@123")
                db.session.add(admin)
                db.session.commit()
        except Exception as err:
            print(f"DATABASE INITIALIZATION WARNING: Could not auto-initialize DB on boot: {err}")

    @flask_app.errorhandler(405)
    def method_not_allowed(e):
        flash("Action not allowed or session expired. Redirecting...", "warning")
        return redirect(url_for("home"))

    @flask_app.errorhandler(404)
    def page_not_found(e):
        flash("The requested page was not found. Redirecting to home.", "info")
        return redirect(url_for("home"))

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


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user or not user.is_admin:
            flash("Access denied. Administrator privileges required.", "danger")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


def current_user():
    uid = session.get("user_id")
    return User.query.get(uid) if uid else None


def register_routes(flask_app):

    @flask_app.context_processor
    def inject_globals():
        return {
            "current_user": current_user(),
            "categories": CATEGORIES,
            "GA_MEASUREMENT_ID": os.environ.get("GA_MEASUREMENT_ID")
        }

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

    def _get_user_bank_balance(user, txns):
        acc = BankAccount.query.filter_by(user_id=user.id).first()
        
        stmt_txn = Transaction.query.filter_by(user_id=user.id).filter(Transaction.running_balance.isnot(None)).order_by(Transaction.date.desc(), Transaction.id.desc()).first()
        if stmt_txn and stmt_txn.running_balance is not None:
            base_bal = stmt_txn.running_balance
            subsequent_income = sum(t.amount for t in txns if t.type == "income" and (t.date > stmt_txn.date or (t.date == stmt_txn.date and t.id > stmt_txn.id)))
            subsequent_expense = sum(t.amount for t in txns if t.type == "expense" and (t.date > stmt_txn.date or (t.date == stmt_txn.date and t.id > stmt_txn.id)))
            current_bal = round(base_bal + subsequent_income - subsequent_expense, 2)
            if acc:
                acc.current_balance = current_bal
                db.session.commit()
            return current_bal

        if acc and acc.current_balance is not None and acc.current_balance != 0:
            return acc.current_balance

        all_income = sum(t.amount for t in txns if t.type == "income")
        all_expense = sum(t.amount for t in txns if t.type == "expense")
        return round(all_income - all_expense, 2)

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
        balance = _get_user_bank_balance(user, txns)

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

    @flask_app.route("/api/dashboard/stats")
    @login_required
    def api_dashboard_stats():
        user = current_user()
        today = date.today()
        last_30 = today - timedelta(days=30)

        txns = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.date.desc()).all()
        recent = [t for t in txns if t.date >= last_30]
        total_expense = sum(t.amount for t in recent if t.type == "expense")
        total_income = sum(t.amount for t in recent if t.type == "income")
        balance = _get_user_bank_balance(user, txns)

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

        recent_data = [
            {
                "id": t.id,
                "date": t.date.strftime('%d %b'),
                "merchant": t.merchant or t.category,
                "category": t.category,
                "type": t.type,
                "amount": t.amount,
                "reference": t.reference or "",
            }
            for t in txns[:8]
        ]

        goals_data = [
            {
                "id": g.id,
                "title": g.title,
                "saved_amount": g.saved_amount,
                "target_amount": g.target_amount,
                "progress_pct": g.progress_pct,
            }
            for g in user_goals
        ]

        return jsonify({
            "ok": True,
            "balance": round(balance, 2),
            "total_expense": round(total_expense, 2),
            "total_income": round(total_income, 2),
            "by_category": {k: round(v, 2) for k, v in by_category.items()},
            "recent_txns": recent_data,
            "budget_status": budget_status,
            "goals": goals_data,
            "updated_at": datetime.utcnow().isoformat(),
        })

    # ---------- REST APIs for Mobile Integration ----------
    @flask_app.route("/api/v1/dashboard", methods=["GET"])
    @login_required
    def api_v1_dashboard():
        return api_dashboard_stats()

    @flask_app.route("/api/v1/transactions", methods=["GET", "POST"])
    @login_required
    def api_v1_transactions():
        user = current_user()
        if request.method == "POST":
            data = request.json or request.form
            try:
                amount = float(data.get("amount", 0))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "Invalid transaction amount"}), 400

            if amount <= 0:
                return jsonify({"ok": False, "error": "Amount must be greater than zero"}), 400

            raw_date = data.get("date") or date.today().isoformat()
            try:
                txn_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                txn_date = date.today()

            merchant = (data.get("merchant") or data.get("description") or "API Transaction").strip()
            category = data.get("category") or "Other"
            type_val = data.get("type") or "expense"
            reference = (data.get("reference") or data.get("upi_ref") or "").strip() or None

            # Automatic Duplicate Detection
            duplicate = None
            if reference:
                duplicate = Transaction.query.filter_by(user_id=user.id, reference=reference).first()
            if not duplicate:
                duplicate = Transaction.query.filter_by(
                    user_id=user.id,
                    date=txn_date,
                    amount=amount,
                    merchant=merchant[:160],
                    type=type_val if type_val in ["income", "expense"] else "expense"
                ).first()

            if duplicate:
                return jsonify({
                    "ok": False,
                    "duplicate": True,
                    "message": "Duplicate transaction detected and skipped.",
                    "transaction": duplicate.to_dict()
                }), 409

            t = Transaction(
                user_id=user.id,
                type=type_val if type_val in ["income", "expense"] else "expense",
                category=category if category in CATEGORIES else "Other",
                merchant=merchant[:160],
                amount=amount,
                note=(data.get("note") or "Added via Mobile API")[:255],
                date=txn_date,
                source=data.get("source") or "api",
                reference=reference,
            )
            db.session.add(t)
            db.session.commit()

            return jsonify({
                "ok": True,
                "message": "Transaction added successfully.",
                "transaction": t.to_dict()
            }), 201

        # GET method: Return user transactions
        txns = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.date.desc()).all()
        return jsonify({
            "ok": True,
            "count": len(txns),
            "transactions": [t.to_dict() for t in txns]
        })

    @flask_app.route("/api/v1/sms/webhook", methods=["POST"])
    @login_required
    def api_v1_sms_webhook():
        user = current_user()
        data = request.json or request.form or {}
        sms_text = (data.get("sms_body") or data.get("text") or "").strip()
        sender = (data.get("sender") or "").strip()

        if not sms_text:
            return jsonify({"ok": False, "error": "sms_body parameter is required"}), 400

        # Extract amount from SMS
        amt_match = re.search(r"(?:rs\.?|inr|₹|\$)\s*([\d,]+(?:\.\d{2})?)", sms_text, re.IGNORECASE)
        if not amt_match:
            amt_match = re.search(r"\b([\d,]+\.\d{2})\b", sms_text)

        if not amt_match:
            return jsonify({"ok": False, "error": "Could not parse monetary amount from SMS body"}), 400

        try:
            amount_val = float(amt_match.group(1).replace(",", ""))
        except ValueError:
            return jsonify({"ok": False, "error": "Invalid amount in SMS"}), 400

        if amount_val <= 0:
            return jsonify({"ok": False, "error": "Amount must be positive"}), 400

        # Determine type
        sms_lower = sms_text.lower()
        if any(k in sms_lower for k in ["credited", "received", "deposited", "added"]):
            txn_type = "income"
        else:
            txn_type = "expense"

        # Extract UPI Ref No if present
        upi_match = re.search(r"\b(\d{12})\b", sms_text)
        reference = upi_match.group(1) if upi_match else None

        # Extract Merchant
        merchant = f"SMS: {sender}" if sender else "SMS Transaction"
        m_match = re.search(r"(?:to|at|vpa|paid to|from)\s+([A-Za-z0-9\s\-]{3,30})", sms_text, re.IGNORECASE)
        if m_match:
            merchant = m_match.group(1).strip()

        # Category
        category = "Other"
        for cat, keywords in {
            "Food": ["swiggy", "zomato", "restaurant", "food", "kitchen", "cafe"],
            "Groceries": ["blinkit", "zepto", "instamart", "bigbasket", "dmart", "mart"],
            "Bills": ["electricity", "recharge", "bill", "airtel", "jio", "vi", "utility"],
            "Transport": ["uber", "ola", "rapido", "fuel", "petrol", "fastag", "metro"],
            "Shopping": ["amazon", "flipkart", "myntra", "meesho", "mall"],
            "Health": ["pharmacy", "hospital", "clinic", "apollo", "1mg"],
        }.items():
            if any(k in sms_lower for k in keywords):
                category = cat
                break

        # Duplicate Check
        duplicate = None
        if reference:
            duplicate = Transaction.query.filter_by(user_id=user.id, reference=reference).first()

        if not duplicate:
            duplicate = Transaction.query.filter_by(
                user_id=user.id,
                date=date.today(),
                amount=amount_val,
                merchant=merchant[:160],
                type=txn_type
            ).first()

        if duplicate:
            return jsonify({
                "ok": False,
                "duplicate": True,
                "message": "Duplicate SMS transaction skipped.",
                "transaction": duplicate.to_dict()
            }), 409

        t = Transaction(
            user_id=user.id,
            type=txn_type,
            category=category,
            merchant=merchant[:160],
            amount=amount_val,
            note=f"Auto-logged from Android SMS ({sender})",
            date=date.today(),
            source="sms",
            reference=reference,
        )
        db.session.add(t)

        # Update BankAccount balance
        acc = BankAccount.query.filter_by(user_id=user.id).first()
        if acc and acc.current_balance is not None:
            if txn_type == "income":
                acc.current_balance += amount_val
            else:
                acc.current_balance -= amount_val

        db.session.commit()

        return jsonify({
            "ok": True,
            "message": "SMS transaction auto-logged successfully.",
            "transaction": t.to_dict()
        }), 201

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

    @flask_app.route("/transactions/<int:txn_id>/delete", methods=["GET", "POST"])
    @login_required
    def delete_transaction(txn_id):
        user = current_user()
        if request.method == "GET":
            return redirect(url_for("transactions"))
        t = Transaction.query.filter_by(id=txn_id, user_id=user.id).first_or_404()
        db.session.delete(t)
        db.session.commit()
        flash("Transaction deleted.", "info")
        return redirect(url_for("transactions"))

    @flask_app.route("/transactions/clear-all", methods=["GET", "POST"])
    @login_required
    def clear_all_transactions():
        user = current_user()
        if request.method == "GET":
            return redirect(url_for("transactions"))
        deleted_count = Transaction.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        db.session.commit()
        flash(f"All transactions cleared ({deleted_count} deleted).", "info")
        return redirect(url_for("transactions"))

    @flask_app.route("/transactions/clear-statement-imports", methods=["GET", "POST"])
    @login_required
    def clear_statement_imports():
        user = current_user()
        if request.method == "GET":
            return redirect(url_for("transactions"))
        deleted_count = Transaction.query.filter_by(user_id=user.id, source="statement").delete(synchronize_session=False)
        db.session.commit()
        flash(f"Purged {deleted_count} statement-imported transaction records.", "info")
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

    @flask_app.route("/scan-receipt/confirm", methods=["GET", "POST"])
    @login_required
    def confirm_receipt():
        user = current_user()
        if request.method == "GET":
            return redirect(url_for("scan_receipt"))
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

    # ---------- Bank Statement PDF Reader ----------
    @flask_app.route("/upload-statement", methods=["GET", "POST"])
    @login_required
    def upload_statement():
        if request.method == "POST":
            file = request.files.get("statement")
            if not file or file.filename == "":
                flash("Please select a bank statement PDF file.", "danger")
                return redirect(url_for("upload_statement"))
            
            if not file.filename.lower().endswith(".pdf"):
                flash("Only PDF files (.pdf) are supported.", "danger")
                return redirect(url_for("upload_statement"))

            pdf_password = request.form.get("pdf_password", "").strip()

            filename = secure_filename(file.filename)
            path = os.path.join(UPLOAD_DIR, f"{session['user_id']}_stmt_{int(datetime.utcnow().timestamp())}_{filename}")
            file.save(path)

            try:
                res = extract_transactions_from_pdf(path, password=pdf_password)
            finally:
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception:
                        pass
            if not res["ok"] or not res["transactions"]:
                flash(res.get("error") or "No readable transactions found in this PDF statement.", "danger")
                return redirect(url_for("upload_statement"))

            txns = res["transactions"]
            total_income = sum(t["amount"] for t in txns if t["type"] == "income")
            total_expense = sum(t["amount"] for t in txns if t["type"] == "expense")

            return render_template(
                "confirm_statement.html",
                transactions=txns,
                total_income=round(total_income, 2),
                total_expense=round(total_expense, 2),
                categories=CATEGORIES,
            )

        return render_template("upload_statement.html")

    @flask_app.route("/confirm-statement", methods=["GET", "POST"])
    @login_required
    def confirm_statement():
        user = current_user()
        if request.method == "GET":
            return redirect(url_for("upload_statement"))
        try:
            total_count = int(request.form.get("total_count", 0))
        except ValueError:
            total_count = 0

        txns_to_add = []
        duplicate_count = 0

        for i in range(total_count):
            if request.form.get(f"include_{i}") == "1":
                try:
                    amount = float(request.form.get(f"amount_{i}", 0))
                except (TypeError, ValueError):
                    continue

                if amount <= 0:
                    continue

                raw_date = request.form.get(f"date_{i}") or date.today().isoformat()
                try:
                    txn_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
                except ValueError:
                    txn_date = date.today()

                merchant = request.form.get(f"merchant_{i}", "").strip() or "Bank Statement Transaction"
                category = request.form.get(f"category_{i}") or "Other"
                type_val = request.form.get(f"type_{i}") or "expense"
                reference = (request.form.get(f"reference_{i}") or "").strip() or None
                tag = (request.form.get(f"tag_{i}") or "").strip()
                raw_bal = request.form.get(f"running_balance_{i}")
                try:
                    running_bal = float(raw_bal) if raw_bal is not None and raw_bal != "" else None
                except ValueError:
                    running_bal = None

                # Duplicate Check
                duplicate = None
                if reference:
                    duplicate = Transaction.query.filter_by(user_id=user.id, reference=reference).first()
                
                if not duplicate:
                    duplicate = Transaction.query.filter_by(
                        user_id=user.id,
                        date=txn_date,
                        amount=amount,
                        merchant=merchant[:160],
                        type=type_val if type_val in ["income", "expense"] else "expense"
                    ).first()

                if duplicate:
                    duplicate_count += 1
                    continue

                note_text = f"Imported from Statement {f'(Tag: #{tag})' if tag else ''} {f'[UPI Ref: {reference}]' if reference else ''}".strip()

                txns_to_add.append(Transaction(
                    user_id=user.id,
                    type=type_val if type_val in ["income", "expense"] else "expense",
                    category=category if category in CATEGORIES else "Other",
                    merchant=merchant[:160],
                    amount=amount,
                    note=note_text[:255],
                    date=txn_date,
                    source="statement",
                    reference=reference,
                    running_balance=running_bal,
                ))

        if txns_to_add:
            db.session.add_all(txns_to_add)

            # Update BankAccount balance if running balance is available
            latest_with_bal = next((t for t in sorted(txns_to_add, key=lambda x: x.date, reverse=True) if t.running_balance is not None), None)
            if latest_with_bal:
                acc = BankAccount.query.filter_by(user_id=user.id).first()
                if not acc:
                    acc = BankAccount(user_id=user.id, current_balance=latest_with_bal.running_balance)
                    db.session.add(acc)
                else:
                    acc.current_balance = latest_with_bal.running_balance

            db.session.commit()
        
        imported_count = len(txns_to_add)
        if duplicate_count > 0:
            flash(f"Import completed! {imported_count} transactions added, {duplicate_count} duplicate(s) skipped.", "success")
        else:
            flash(f"Successfully imported {imported_count} transactions from bank statement.", "success")
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
    @flask_app.route("/api/budgets/create-from-ai", methods=["POST"])
    @login_required
    def api_budgets_create_from_ai():
        user = current_user()
        data = request.json or request.form or {}
        items = data.get("budgets") or []

        if not items and ("category" in data and ("limit_amount" in data or "limit" in data)):
            items = [{"category": data.get("category"), "limit": data.get("limit_amount") or data.get("limit")}]

        if not items:
            ctx = build_context_summary(user)
            auto_create_smart_budgets(user, ctx)
            user_budgets = Budget.query.filter_by(user_id=user.id).all()
            return jsonify({
                "success": True,
                "budgets": [{"category": b.category, "limit": b.monthly_limit} for b in user_budgets]
            })

        saved_budgets = []
        for item in items:
            cat = (item.get("category") or "").strip()
            try:
                limit_val = float(item.get("limit") or item.get("limit_amount") or 0)
            except (TypeError, ValueError):
                continue

            if not cat or limit_val <= 0:
                continue

            existing = Budget.query.filter_by(user_id=user.id, category=cat).first()
            if existing:
                existing.monthly_limit = limit_val
            else:
                b = Budget(user_id=user.id, category=cat, monthly_limit=limit_val)
                db.session.add(b)

            saved_budgets.append({"category": cat, "limit": limit_val})

        db.session.commit()

        return jsonify({
            "success": True,
            "budgets": saved_budgets
        })

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

    @flask_app.route("/budgets/<int:budget_id>/delete", methods=["GET", "POST"])
    @login_required
    def delete_budget(budget_id):
        user = current_user()
        if request.method == "GET":
            return redirect(url_for("budgets"))
        b = Budget.query.filter_by(id=budget_id, user_id=user.id).first_or_404()
        db.session.delete(b)
        db.session.commit()
        flash("Budget deleted.", "info")
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

    @flask_app.route("/goals/<int:goal_id>/contribute", methods=["GET", "POST"])
    @login_required
    def contribute_goal(goal_id):
        user = current_user()
        if request.method == "GET":
            return redirect(url_for("goals"))
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

    @flask_app.route("/goals/<int:goal_id>/delete", methods=["GET", "POST"])
    @login_required
    def delete_goal(goal_id):
        user = current_user()
        if request.method == "GET":
            return redirect(url_for("goals"))
        g = Goal.query.filter_by(id=goal_id, user_id=user.id).first_or_404()
        db.session.delete(g)
        db.session.commit()
        flash("Goal deleted.", "info")
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

    @flask_app.route("/admin")
    @admin_required
    def admin_dashboard():
        users = User.query.all()
        txn_count = Transaction.query.count()
        db_engine = db.engine.url.drivername
        db_path = db.engine.url.database
        
        # Check for presence of env vars
        db_active = bool(os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PRIVATE_URL") or os.environ.get("DATABASE_PUBLIC_URL"))
        gemini_raw = (os.environ.get("GEMINI_API_KEY") or "").strip()
        gemini_active = bool(gemini_raw and not gemini_raw.startswith("Paste your"))

        env_vars = {
            "DATABASE_URL": {
                "label": "FOUND" if os.environ.get("DATABASE_URL") else "MISSING",
                "status": "success" if os.environ.get("DATABASE_URL") else "danger"
            },
            "DATABASE_PRIVATE_URL": {
                "label": "FOUND" if os.environ.get("DATABASE_PRIVATE_URL") else ("OPTIONAL" if db_active else "MISSING"),
                "status": "success" if os.environ.get("DATABASE_PRIVATE_URL") else ("info" if db_active else "danger")
            },
            "DATABASE_PUBLIC_URL": {
                "label": "FOUND" if os.environ.get("DATABASE_PUBLIC_URL") else ("OPTIONAL" if db_active else "MISSING"),
                "status": "success" if os.environ.get("DATABASE_PUBLIC_URL") else ("info" if db_active else "danger")
            },
            "SECRET_KEY": {
                "label": "FOUND" if os.environ.get("SECRET_KEY") else "MISSING",
                "status": "success" if os.environ.get("SECRET_KEY") else "danger"
            },
            "GEMINI_API_KEY": {
                "label": "FOUND" if gemini_active else "OPTIONAL",
                "status": "success" if gemini_active else "info"
            }
        }
        
        return render_template("admin.html", 
                               users=users, 
                               txn_count=txn_count,
                               db_engine=db_engine,
                               db_path=db_path,
                               env_vars=env_vars)

    @flask_app.route("/admin/export-users")
    @admin_required
    def export_users_xlsx():
        import openpyxl
        users = User.query.all()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Users"
        ws.append(["Email", "First Name", "Last Name", "Phone", "New Password"])
        for u in users:
            ws.append([u.email, u.first_name, u.last_name, u.phone_number, ""])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                          as_attachment=True,
                          download_name=f"wallet_ai_users_{date.today().isoformat()}.xlsx")

    @flask_app.route("/admin/import-users", methods=["POST"])
    @admin_required
    def import_users_xlsx():
        import openpyxl
        file = request.files.get("file")
        if not file or file.filename == "":
            flash("Please choose an Excel file.", "danger")
            return redirect(url_for("admin_dashboard"))
        
        try:
            wb = openpyxl.load_workbook(file)
            ws = wb.active
            updated_count = 0
            # Skip header row
            for row in ws.iter_rows(min_row=2, values_only=True):
                email = row[0]
                new_pw = row[4]
                if email and new_pw:
                    user = User.query.filter_by(email=email).first()
                    if user:
                        user.set_password(str(new_pw))
                        updated_count += 1
            
            db.session.commit()
            flash(f"Successfully updated passwords for {updated_count} users.", "success")
        except Exception as e:
            flash(f"Error processing file: {str(e)}", "danger")
            
        return redirect(url_for("admin_dashboard"))

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
