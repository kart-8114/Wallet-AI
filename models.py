import datetime as dt
from extensions import db
from werkzeug.security import generate_password_hash, check_password_hash


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(60), nullable=False)
    last_name = db.Column(db.String(60), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    mpin_hash = db.Column(db.String(255), nullable=True)  # 4-digit quick-access PIN
    phone_number = db.Column(db.String(20), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    city = db.Column(db.String(100), nullable=True)
    state = db.Column(db.String(100), nullable=True)
    zip_code = db.Column(db.String(20), nullable=True)
    otp_code = db.Column(db.String(6), nullable=True)
    otp_verified = db.Column(db.Boolean, default=False)
    theme = db.Column(db.String(10), default="light")
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

    transactions = db.relationship("Transaction", backref="user", lazy=True, cascade="all, delete-orphan")
    budgets = db.relationship("Budget", backref="user", lazy=True, cascade="all, delete-orphan")
    goals = db.relationship("Goal", backref="user", lazy=True, cascade="all, delete-orphan")

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    def set_mpin(self, raw):
        self.mpin_hash = generate_password_hash(raw)

    def check_mpin(self, raw):
        return self.mpin_hash and check_password_hash(self.mpin_hash, raw)


class Transaction(db.Model):
    __tablename__ = "transactions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    type = db.Column(db.String(10), nullable=False)  # 'income' or 'expense'
    category = db.Column(db.String(60), nullable=False)
    merchant = db.Column(db.String(160), nullable=True)
    amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(255), nullable=True)
    date = db.Column(db.Date, default=dt.date.today, nullable=False)
    source = db.Column(db.String(20), default="manual")  # manual | ocr | ai
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "type": self.type,
            "category": self.category,
            "merchant": self.merchant,
            "amount": self.amount,
            "note": self.note,
            "date": self.date.isoformat(),
            "source": self.source,
        }


class Budget(db.Model):
    __tablename__ = "budgets"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    category = db.Column(db.String(60), nullable=False)
    monthly_limit = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)


class Goal(db.Model):
    __tablename__ = "goals"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(120), nullable=False)
    target_amount = db.Column(db.Float, nullable=False)
    saved_amount = db.Column(db.Float, default=0.0)
    target_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

    @property
    def progress_pct(self):
        if self.target_amount <= 0:
            return 0
        return min(100, round((self.saved_amount / self.target_amount) * 100, 1))
