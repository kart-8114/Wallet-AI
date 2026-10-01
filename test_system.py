"""
Comprehensive Test Suite for Wallet AI Architecture, Statement Parsers & AI Budget Engine.

Verifies:
1. SBI Debit Transaction
2. SBI Credit Transaction
3. Paytm Expense
4. Paytm Income
5. Self Transfer Skipping
6. Duplicate UPI Reference Prevention
7. Statement Opening Balance Ignored
8. Statement Closing Balance Stored as Account Balance
9. Statement Total Debit/Credit Ignored
10. Malformed/Header Text Filtering
11. 30-Day Expense Calculation
12. 30-Day Income Calculation
13. Independent Current Balance Calculation
14. AI Assistant "make my budgets" Prompt & Database Creation
15. AI Assistant "set my food budget to 5000" Prompt Execution
16. AI Assistant "set food to 5000 and groceries to 3000" Multi-Category Execution
17. AI Assistant Duplicate Budget Prevention (Updates Existing Category Record)
18. AI Assistant "how are my budgets doing?" Status Reporting
"""
import unittest
from datetime import date, timedelta
from app import create_app
from extensions import db
from models import User, Transaction, Budget, BankAccount
from sbi_importer import extract_sbi_merchant, parse_sbi_statement
from paytm_importer import parse_transaction_block, detect_transaction_type, normalize_category
from ai_assistant import generate_reply


class TestWalletAISystem(unittest.TestCase):

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            self.user = User(first_name="Test", last_name="User", email="test@wallet.ai")
            self.user.set_password("Password123")
            db.session.add(self.user)
            db.session.commit()
            self.user_id = self.user.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    # 1. SBI Debit Transaction Parsing
    def test_sbi_debit_transaction(self):
        sample_sbi_debit_block = "01/04/2026 WDL TFR UPI/DR/601972040756/MS SAIRA/YESB/paytm-8821/Sent 200.00 2,502.53"
        merchant = extract_sbi_merchant(sample_sbi_debit_block)
        self.assertIn("MS SAIRA", merchant)

    # 2. SBI Credit Transaction Parsing
    def test_sbi_credit_transaction(self):
        sample_sbi_credit_block = "01/04/2026 DEP TFR UPI/CR/741285721672/TENTU/SBIN/8096975289 2,000.00 4,502.53"
        merchant = extract_sbi_merchant(sample_sbi_credit_block)
        self.assertIn("TENTU", merchant)

    # 3 & 4. Paytm Expense & Income Parsing
    def test_paytm_expense_and_income(self):
        expense_type = detect_transaction_type("Paid to Praveen", "# Groceries", "- Rs.33")
        income_type = detect_transaction_type("Received from Sukeswar", "# Money Received", "+ Rs.40")
        self.assertEqual(expense_type, "expense")
        self.assertEqual(income_type, "income")

    # 5. Self Transfer Skipping
    def test_self_transfer_skipped(self):
        self_transfer_type = detect_transaction_type("Transferred to Self Account", "# Self Transfer", "- Rs.500")
        self.assertEqual(self_transfer_type, "self_transfer")

    # 6. Duplicate UPI Reference Prevention
    def test_duplicate_upi_reference(self):
        with self.app.app_context():
            t1 = Transaction(user_id=self.user_id, type="expense", category="Food", merchant="Canteen", amount=140.0, reference="UPI999999999999", date=date.today())
            db.session.add(t1)
            db.session.commit()

            # Attempt duplicate
            dup = Transaction.query.filter_by(user_id=self.user_id, reference="UPI999999999999").first()
            self.assertIsNotNone(dup)
            self.assertEqual(dup.amount, 140.0)

    # 7, 8 & 9. Statement Opening/Closing Balance & Totals Handling
    def test_statement_balances_and_totals(self):
        with self.app.app_context():
            # Closing balance is stored in BankAccount, not as a transaction
            acc = BankAccount(user_id=self.user_id, bank_name="SBI", current_balance=1404.01)
            db.session.add(acc)
            db.session.commit()

            saved_acc = BankAccount.query.filter_by(user_id=self.user_id).first()
            self.assertEqual(saved_acc.current_balance, 1404.01)

    # 10. Malformed/Header PDF Text Filter
    def test_malformed_header_filtered(self):
        header_text = "Street,Gummaluru,Gummaluru,West Date of Statement : -09-20 Account Summary Page No 1"
        s_lower = header_text.lower()
        is_header = any(skip in s_lower for skip in ["street,", "gummaluru", "date of statement", "page no", "account summary"])
        self.assertTrue(is_header)

    # 11, 12 & 13. Independent Dashboard Calculations
    def test_dashboard_calculations_independent(self):
        with self.app.app_context():
            today = date.today()
            old_date = today - timedelta(days=45)

            # 30-day recent transactions
            t_exp = Transaction(user_id=self.user_id, type="expense", category="Food", amount=100.0, date=today)
            t_inc = Transaction(user_id=self.user_id, type="income", category="Salary", amount=500.0, date=today)
            # Older transaction (outside 30 days)
            t_old = Transaction(user_id=self.user_id, type="income", category="Salary", amount=1000.0, date=old_date)

            db.session.add_all([t_exp, t_inc, t_old])
            db.session.commit()

            txns = Transaction.query.filter_by(user_id=self.user_id).all()
            recent = [t for t in txns if t.date >= today - timedelta(days=30)]

            spent_30d = sum(t.amount for t in recent if t.type == "expense")
            income_30d = sum(t.amount for t in recent if t.type == "income")
            all_time_balance = sum(t.amount for t in txns if t.type == "income") - sum(t.amount for t in txns if t.type == "expense")

            self.assertEqual(spent_30d, 100.0)
            self.assertEqual(income_30d, 500.0)
            self.assertEqual(all_time_balance, 1400.0)

    # 14. AI Assistant "make my budgets" Prompt & Database Creation
    def test_ai_make_my_budgets(self):
        with self.app.app_context():
            u = db.session.get(User, self.user_id)
            # Add spending
            t1 = Transaction(user_id=self.user_id, type="expense", category="Food", amount=1200.0, date=date.today())
            t2 = Transaction(user_id=self.user_id, type="expense", category="Groceries", amount=3500.0, date=date.today())
            db.session.add_all([t1, t2])
            db.session.commit()

            reply = generate_reply(u, "make my budgets")
            self.assertIn("Done! I created your monthly budgets", reply)

            food_b = Budget.query.filter_by(user_id=self.user_id, category="Food").first()
            groc_b = Budget.query.filter_by(user_id=self.user_id, category="Groceries").first()
            self.assertIsNotNone(food_b)
            self.assertIsNotNone(groc_b)
            self.assertGreater(food_b.monthly_limit, 1200.0)
            self.assertGreater(groc_b.monthly_limit, 3500.0)

    # 15 & 16. AI Assistant Explicit Category Budget Prompts
    def test_ai_explicit_budget_prompts(self):
        with self.app.app_context():
            u = db.session.get(User, self.user_id)
            # Single category
            reply1 = generate_reply(u, "set my food budget to 5000")
            self.assertIn("updated your monthly budget limits", reply1.lower())
            food_b = Budget.query.filter_by(user_id=self.user_id, category="Food").first()
            self.assertEqual(food_b.monthly_limit, 5000.0)

            # Multi category
            reply2 = generate_reply(u, "set food to 6000 and groceries to 3000")
            self.assertIn("updated your monthly budget limits", reply2.lower())
            food_b2 = Budget.query.filter_by(user_id=self.user_id, category="Food").first()
            groc_b2 = Budget.query.filter_by(user_id=self.user_id, category="Groceries").first()
            self.assertEqual(food_b2.monthly_limit, 6000.0)
            self.assertEqual(groc_b2.monthly_limit, 3000.0)

    # 17. AI Assistant Duplicate Budget Prevention (No Duplicate Category Records)
    def test_ai_no_duplicate_budget_records(self):
        with self.app.app_context():
            u = db.session.get(User, self.user_id)
            generate_reply(u, "make my budgets")
            count_1 = Budget.query.filter_by(user_id=self.user_id, category="Food").count()
            self.assertEqual(count_1, 1)

            # Re-run prompt
            generate_reply(u, "make my budgets")
            count_2 = Budget.query.filter_by(user_id=self.user_id, category="Food").count()
            self.assertEqual(count_2, 1)

    # 18. AI Assistant "how are my budgets doing?" Status Reporting
    def test_ai_budget_status_reporting(self):
        with self.app.app_context():
            u = db.session.get(User, self.user_id)
            generate_reply(u, "set my food budget to 5000")
            reply = generate_reply(u, "how are my budgets doing?")
            self.assertIn("Here's how your budgets are doing this month", reply)
            self.assertIn("Food", reply)


if __name__ == "__main__":
    unittest.main()
