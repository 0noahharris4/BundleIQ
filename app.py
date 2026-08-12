import io
import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict

import pandas as pd
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file

from rules_engine import analyze

load_dotenv()  # pulls OPENAI_API_KEY etc. from a local .env file if present;
                # a no-op if the file doesn't exist (e.g. on Render, where
                # env vars are set directly in the dashboard instead).

app = Flask(__name__)

REQUIRED_COLUMNS = ["order_id", "product_name"]
OPTIONAL_COLUMNS = ["category", "quantity", "unit_price", "order_date"]
MAX_ROWS = 250_000

# ---------------------------------------------------------------- chatbot --
# "Chat with your data" lets a user chat with their own analysis results in
# plain English. The key lives ONLY on the server -- in a .env file (or a
# real env var on Render) -- and there's no key-entry UI anywhere; visitors
# never see or handle it. (The standalone, no-backend preview build doesn't
# get this feature at all: with no server to hold a secret, the only way to
# offer it there would be asking each visitor for their own key or
# embedding a real key in a static HTML file anyone can view-source -- both
# worse than just not shipping chat in that build.)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
CHAT_MAX_QUESTION_LEN = 800
CHAT_HISTORY_TURNS = 6

# Since one server-held key now pays for every visitor's questions (rather
# than each visitor bringing their own), a light per-IP rate limit guards
# against one visitor running up the bill. This is intentionally simple --
# in-memory, resets on restart, and not shared across worker processes --
# good enough for a single-instance demo deploy, not a substitute for a
# real distributed rate limiter if this ever needs to scale.
CHAT_RATE_LIMIT = 20
CHAT_RATE_WINDOW_SECONDS = 3600
_chat_hits_by_ip = defaultdict(list)


def _chat_rate_limited(ip):
    now = time.time()
    hits = _chat_hits_by_ip[ip]
    while hits and hits[0] < now - CHAT_RATE_WINDOW_SECONDS:
        hits.pop(0)
    if len(hits) >= CHAT_RATE_LIMIT:
        return True
    hits.append(now)
    return False

CHAT_SYSTEM_PROMPT = (
    "You are BundleIQ's data analyst assistant. Answer the user's question using ONLY the "
    "market-basket analysis data provided below as JSON -- it comes from counting real "
    "co-purchases in the user's own transaction file. support/confidence/lift describe how "
    "often products are bought together (lift > 1 means more often than chance). "
    "seasonal_index_1_0_is_typical is a ratio where 1.0 = typical for that product and above "
    "1 means it over-indexes that season. Be concise (2-5 sentences unless a list is clearly "
    "better), speak in plain business terms a merchandiser would use, and cite specific numbers "
    "from the data when relevant. If the data doesn't contain enough information to answer "
    "confidently, say so plainly instead of guessing or inventing numbers. Don't mention "
    "'JSON' or that you were handed a data dump -- just talk about 'your data' naturally."
)

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "sample_data", "template.csv")
SAMPLE_PATH = os.path.join(os.path.dirname(__file__), "sample_data", "sample_transactions.csv")

# Common alternate spellings for each field, used to auto-detect column
# mapping so most real-world exports "just work" without asking the user
# anything. Checked in order; the first column whose normalized name matches
# an alias wins. Required fields are matched before optional ones so a
# column can't be claimed by an optional field before a required one needs it.
ALIASES = {
    "order_id": [
        "order_id", "orderid", "order", "order_number", "ordernumber", "order_no", "orderno",
        "transaction_id", "transactionid", "transaction", "invoice_id", "invoice_number", "invoice",
        "receipt_id", "receipt_number", "receipt", "basket_id", "cart_id", "purchase_id",
    ],
    "product_name": [
        "product_name", "productname", "product", "item", "item_name", "itemname",
        "sku_name", "description", "product_description", "item_description", "product_title",
        "product", "name",
    ],
    "category": [
        "category", "product_category", "department", "dept", "product_type", "type", "product_dept",
    ],
    "quantity": [
        "quantity", "qty", "units", "unit_count", "item_count", "count", "amount_purchased", "qty_ordered",
    ],
    "unit_price": [
        "unit_price", "unitprice", "price", "unit_cost", "item_price", "cost", "price_per_unit", "unit_amount",
    ],
    "order_date": [
        "order_date", "orderdate", "date", "purchase_date", "transaction_date", "sale_date",
        "order_datetime", "created_at", "timestamp", "datetime",
    ],
}


def _alias_key(s):
    return str(s).strip().lower().replace(" ", "_")


def detect_columns(columns):
    """Best-guess field -> source-column mapping from a file's own column
    names, using ALIASES. Required fields are resolved first so they get
    first pick of an ambiguous column name."""
    available = list(columns)
    guess = {}
    for field in REQUIRED_COLUMNS + OPTIONAL_COLUMNS:
        aliases = {_alias_key(a) for a in ALIASES.get(field, [field])}
        match = next((c for c in available if _alias_key(c) in aliases), None)
        if match:
            guess[field] = match
            available.remove(match)
        else:
            guess[field] = None
    return guess


def _load_dataframe(file_source, filename=""):
    """Reads a CSV or Excel file into a DataFrame with normalized column
    names, but does NOT check for required columns yet -- that happens after
    column mapping is resolved (auto-detected or supplied by the user)."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "csv"

    if ext in ("xlsx", "xlsm"):
        try:
            df = pd.read_excel(file_source, engine="openpyxl")
        except Exception as e:
            raise ValueError(f"Couldn't read that as an Excel file ({e}). Please check the file and try again.")
    elif ext == "xls":
        raise ValueError("Old-style .xls files aren't supported -- please re-save as .xlsx or .csv and try again.")
    elif ext in ("csv", "txt"):
        try:
            df = pd.read_csv(file_source)
        except Exception as e:
            raise ValueError(f"Couldn't read that as a CSV file ({e}). Please check the file and try again.")
    else:
        raise ValueError("Please upload a .csv or .xlsx file.")

    if df.empty:
        raise ValueError("That file has no rows.")

    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    if len(df) > MAX_ROWS:
        raise ValueError(f"That file has {len(df):,} rows -- this demo supports up to {MAX_ROWS:,}.")

    return df


def _apply_mapping_and_validate(df, mapping):
    """mapping: field -> source column name. Renames the mapped columns onto
    the fixed field names, checks required fields are present, and drops
    rows missing them."""
    rename = {v: k for k, v in mapping.items() if v and v in df.columns}
    df = df.rename(columns=rename)

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Missing required column{'s' if len(missing) > 1 else ''}: {', '.join(missing)}. "
            f"Required columns are: {', '.join(REQUIRED_COLUMNS)}. "
            f"Download the template below to see the expected format."
        )

    df = df.dropna(subset=REQUIRED_COLUMNS)
    if df.empty:
        raise ValueError("No rows had both an order ID and a product name filled in.")

    if "order_date" in df.columns:
        # Normalize whatever date format the file used down to plain
        # 'YYYY-MM-DD' strings -- rules_engine.py only needs the month, and
        # this keeps date parsing (which varies a lot across real-world
        # exports) confined to one place. Unparseable dates just become
        # empty, so those rows still count everywhere except seasonality.
        parsed = pd.to_datetime(df["order_date"], errors="coerce")
        df["order_date"] = parsed.dt.strftime("%Y-%m-%d")
        df["order_date"] = df["order_date"].where(parsed.notna(), None)

    keep_cols = REQUIRED_COLUMNS + [c for c in OPTIONAL_COLUMNS if c in df.columns]
    return df[keep_cols]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/meta")
def meta():
    return jsonify({
        "required_columns": REQUIRED_COLUMNS,
        "optional_columns": OPTIONAL_COLUMNS,
        "chat_enabled": bool(OPENAI_API_KEY),
    })


@app.route("/api/template")
def template():
    return send_file(TEMPLATE_PATH, as_attachment=True, download_name="bundleiq_template.csv", mimetype="text/csv")


@app.route("/api/analyze", methods=["POST"])
def analyze_upload():
    if "file" not in request.files:
        return jsonify({"error": "No file was uploaded."}), 400
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error": "No file was selected."}), 400

    try:
        df_raw = _load_dataframe(io.BytesIO(f.read()), f.filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Something went wrong reading that file: {e}"}), 400

    # If the client already resolved a mapping (either from its own
    # auto-detection or from a user-filled mapping form), use it directly.
    mapping = {field: request.form.get(f"map_{field}") for field in REQUIRED_COLUMNS + OPTIONAL_COLUMNS}
    mapping = {k: v for k, v in mapping.items() if v}

    if not mapping:
        guess = detect_columns(list(df_raw.columns))
        if guess.get("order_id") and guess.get("product_name"):
            mapping = guess
        else:
            return jsonify({
                "needs_mapping": True,
                "columns": list(df_raw.columns),
                "guess": guess,
            }), 422

    try:
        df = _apply_mapping_and_validate(df_raw, mapping)
        result = analyze(df.to_dict("records"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Something went wrong analyzing that file: {e}"}), 400

    result["source_filename"] = f.filename
    return jsonify(result)


@app.route("/api/analyze-sample")
def analyze_sample():
    df_raw = _load_dataframe(SAMPLE_PATH, "sample_transactions.csv")
    guess = detect_columns(list(df_raw.columns))
    df = _apply_mapping_and_validate(df_raw, guess)
    result = analyze(df.to_dict("records"))
    result["source_filename"] = "sample_transactions.csv"
    return jsonify(result)


@app.route("/api/chat", methods=["POST"])
def chat():
    if not OPENAI_API_KEY:
        return jsonify({"error": "The chatbot isn't configured on this server yet."}), 503

    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")).split(",")[0].strip()
    if _chat_rate_limited(ip):
        return jsonify({"error": "You've hit the chat limit for now -- please try again in a bit."}), 429

    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question") or "").strip()
    context = payload.get("context")
    history = payload.get("history") or []

    if not question:
        return jsonify({"error": "Ask a question first."}), 400
    if len(question) > CHAT_MAX_QUESTION_LEN:
        return jsonify({"error": "That question is too long -- try to keep it to a couple of sentences."}), 400
    if not context:
        return jsonify({"error": "Analyze a file before asking questions about it."}), 400

    messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT + "\n\nDATA:\n" + json.dumps(context)}]
    if isinstance(history, list):
        for turn in history[-CHAT_HISTORY_TURNS:]:
            if not isinstance(turn, dict):
                continue
            role = turn.get("role")
            content = turn.get("content")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": str(content)[:2000]})
    messages.append({"role": "user", "content": question})

    body = json.dumps({
        "model": OPENAI_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 500,
    }).encode("utf-8")

    req = urllib.request.Request(
        OPENAI_CHAT_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return jsonify({"error": "OpenAI rejected that API key. Double check it and try again."}), 401
        if e.code == 429:
            return jsonify({"error": "OpenAI rate-limited that request. Wait a moment and try again."}), 429
        return jsonify({"error": f"OpenAI returned an error ({e.code})."}), 502
    except (urllib.error.URLError, TimeoutError):
        return jsonify({"error": "Couldn't reach OpenAI. Please try again."}), 502

    try:
        answer = resp_data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        return jsonify({"error": "Got an unexpected response from OpenAI."}), 502

    return jsonify({"answer": answer})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("FLASK_DEBUG")))
