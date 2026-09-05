# Wallet AI

An intelligent, multi-layered personal finance manager: OCR receipt scanning,
an AI chat assistant that reads your real spending, budgets, goals, and
Chart.js analytics — built with Flask, SQLAlchemy, and SQLite.

## Features implemented

- **Auth suite**: hashed passwords (Werkzeug), 6-digit OTP email verification
  (simulated in-app — the code is flashed on screen instead of emailed),
  4-digit MPIN for quick sign-in.
- **OCR receipt scanner**: upload a photo of a receipt; Tesseract OCR reads it
  and the app guesses merchant, date, total, and category. Review and confirm
  before it's saved as a transaction. See `ocr.py`.
- **AI chat assistant**: answers questions like "summarize my spending" or
  "give me saving tips" using your real transaction history. Runs fully
  offline with a rule-based engine — see the docstring in `ai_assistant.py`
  for how to wire in Gemini 2.0 / the ngrok AI Gateway instead.
- **Analytics**: 30-day trend line, category doughnut chart, income vs.
  expense bar chart (Chart.js).
- **Budgets**: per-category monthly limits with live progress bars.
- **Goals**: savings goals (Emergency Fund, Vacation, etc.) with contributions
  and progress bars.
- **Export**: download your full transaction history as CSV or XLSX.
- **Light/dark theme**, saved per-user.

## Run locally

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Tesseract OCR must be installed on the machine (not just the Python package):

```bash
# Debian/Ubuntu
sudo apt-get install tesseract-ocr
# macOS
brew install tesseract
```

Then:

```bash
python app.py
```

Visit `http://localhost:5000`.

## Run with Docker

```bash
docker build -t wallet-ai .
docker run -p 5000:5000 -v $(pwd)/instance:/app/instance wallet-ai
```

## Project structure

```
wallet_ai/
├── app.py              # Flask app + all routes
├── models.py            # SQLAlchemy models: User, Transaction, Budget, Goal
├── ocr.py                # Tesseract-based receipt field extraction
├── ai_assistant.py       # Rule-based financial chat assistant
├── extensions.py         # db = SQLAlchemy()
├── requirements.txt
├── Dockerfile
├── templates/            # Jinja2 templates
└── static/
    ├── css/style.css     # Light/dark theme
    └── js/main.js
```

## Future scope (per project abstract)

- Direct bank API integration for automatic transaction sync.
- Predictive analytics to forecast future balances from historical trends.
- Swap the rule-based assistant for a live Gemini 2.0 / ngrok AI Gateway call
  (hook point documented in `ai_assistant.py`).
- Swap heuristic OCR parsing for a vision-model-based extractor for
  higher accuracy on messy receipts (hook point documented in `ocr.py`).
