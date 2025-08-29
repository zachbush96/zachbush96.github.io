#!/usr/bin/env python3

import csv
import io
import os
import re
import time
import random
import hashlib
import secrets
import subprocess
from datetime import datetime
from typing import List, Dict, Any
from flask import (
    Flask, request, redirect, url_for, render_template_string,
    session, send_from_directory, flash
)
from jinja2 import Environment, StrictUndefined
import sqlite3
from pathlib import Path

# ------------------------------
# Config
# ------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(16))

# Default country code for phone normalization
DEFAULT_COUNTRY_CODE = "+1"

# In-memory store for uploaded data (per-session)
DATA_STORE: Dict[str, Dict[str, Any]] = {}

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# ------------------------------
# Utilities
# ------------------------------

HEADER_ALIASES = {
    "phone": {"phone", "phone_number", "number", "mobile", "cell"},
    "name": {"name", "full_name", "first_name"},
    "business": {"business", "company", "business_name", "name"},
    "address": {"address", "addr", "street"},
}

def normalize_headers(headers: List[str]) -> Dict[str, str]:
    """Map CSV headers to canonical names when possible.

    Be tolerant of empty/missing header cells by normalizing None->"".
    """
    mapping = {}
    lower_headers = [((h or "").strip().lower()) for h in headers]
    for canon, aliases in HEADER_ALIASES.items():
        for h in lower_headers:
            if h in aliases:
                mapping[h] = canon
    # Also map any headers not recognized to themselves (accessible in template)
    for h in lower_headers:
        mapping.setdefault(h, h)
    return mapping

_phone_digits = re.compile(r"\D+")

def normalize_phone(raw: str, default_cc: str = DEFAULT_COUNTRY_CODE) -> str:
    if not raw:
        return ""
    digits = _phone_digits.sub("", raw)
    # Basic US length handling; adapt as needed for other countries
    if digits.startswith("1") and len(digits) == 11:
        return "+" + digits
    if len(digits) == 10:
        return default_cc + digits
    if raw.startswith("+"):
        # Already looks like E.164
        return raw
    # Fallback: return digits (Messages can sometimes handle it)
    return digits

def jinja_render(template_str: str, context: Dict[str, Any]) -> str:
    env = Environment(undefined=StrictUndefined, autoescape=False,
                      trim_blocks=True, lstrip_blocks=True)
    template = env.from_string(template_str)
    return template.render(**context)

APPLE_SCRIPT_SEND = r'''
on stripNonDigits(s)
    set outT to ""
    repeat with c in s
        set ch to (c as text)
        if ch is in "0123456789" then set outT to outT & ch
    end repeat
    return outT
end stripNonDigits

on last10(s)
    set d to stripNonDigits(s)
    if (length of d) > 10 then
        return text -10 thru -1 of d
    else
        return d
    end if
end last10

on chatMatchesTarget(aChat, target10)
    try
        set plist to participants of aChat
    on error
        return false
    end try
    repeat with p in plist
        set p10 to last10(p as text)
        if p10 is not "" and p10 is equal to target10 then return true
    end repeat
    return false
end chatMatchesTarget

on run {targetPhone, targetMessage}
    set targetPhone to targetPhone as text
    set want10 to last10(targetPhone)

    tell application "Messages"
        if it is not running then activate

        set iService to missing value
        set sService to missing value
        set serviceListDesc to {}

        -- enumerate services for debugging
        repeat with svc in services
            try
                set stype to service type of svc
            on error
                set stype to "unknown"
            end try
            try
                set sid to id of svc
            on error
                set sid to "no-id"
            end try
            set end of serviceListDesc to ("type=" & stype & ", id=" & sid)
            if stype is iMessage and iService is missing value then set iService to svc
            if stype is SMS and sService is missing value then set sService to svc
        end repeat

        -- prefer iMessage
        set targetService to iService
        if targetService is missing value then set targetService to sService

        if targetService is missing value then
            error "No iMessage or SMS service available. Services seen: " & (serviceListDesc as text)
        end if

        -- 1) try to find an existing chat whose participants match (last 10)
        set theChat to missing value
        try
            set allChats to chats
            repeat with c in allChats
                if chatMatchesTarget(c, want10) then
                    set theChat to c
                    exit repeat
                end if
            end repeat
        end try

        -- 2) if not found, try a buddy on the chosen service
        if theChat is missing value then
            try
                set theBuddy to buddy targetPhone of targetService
                -- Sometimes buddy lookup works even if not in Contacts; if it does, send directly.
                send targetMessage to theBuddy
                return
            end try
        end if

        -- 3) if still missing, attempt new chat creation
        if theChat is missing value then
            try
                set theChat to make new text chat with properties {service:targetService, participants:{targetPhone}}
            end try
        end if

        if theChat is missing value then
            error "Could not create/resolve a chat for " & targetPhone & " via " & (service type of targetService as text) & ". Services: " & (serviceListDesc as text) & ". Check iMessage sign-in and, for SMS, iPhone Text Message Forwarding."
        end if

        send targetMessage to theChat
    end tell
end run
'''


def send_imessage(phone: str, message: str) -> float:
    """Send a single iMessage via AppleScript. Returns unix timestamp just before send."""
    send_start = time.time()
    completed = subprocess.run(
        ["osascript", "-", phone, message],
        input=APPLE_SCRIPT_SEND.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return send_start


# ------------------------------
# Delivery / Failure detection via Messages chat.db
# ------------------------------

CHAT_DB_PATHS = [
    Path.home() / "Library/Messages/chat.db",                       # standard
    Path.home() / "Library/Messages/chat.db-wal",                   # WAL mode (presence check)
]

def _chatdb_path() -> Path:
    # Prefer main db path; existence check
    p = CHAT_DB_PATHS[0]
    if p.exists():
        return p
    # As a fallback, still return the canonical path (caller will handle errors)
    return p

def _last10_digits(s: str) -> str:
    return re.sub(r"\D+", "", s)[-10:] if s else ""

def _apple_epoch_ns_to_unix_s(v: int) -> float:
    """
    Messages 'date' columns are Apple epoch (Jan 1, 2001), usually in nanoseconds.
    Some macOS releases store in seconds; handle both heuristically.
    """
    if v is None:
        return 0.0
    # Apple epoch offset between 1970 and 2001:
    APPLE_EPOCH_OFFSET = 978307200  # seconds
    # Heuristic: if it's a huge number, it's ns; if smaller, seconds
    if v > 10_000_000_000:  # definitely ns
        return (v / 1_000_000_000.0) + APPLE_EPOCH_OFFSET
    else:
        # already seconds since 2001
        return float(v) + APPLE_EPOCH_OFFSET

def _unix_s_to_apple_epoch_ns(unix_s: float) -> int:
    APPLE_EPOCH_OFFSET = 978307200
    # Store as nanoseconds since 2001-01-01
    return int((unix_s - APPLE_EPOCH_OFFSET) * 1_000_000_000)

def _open_chatdb():
    dbp = _chatdb_path()
    conn = sqlite3.connect(str(dbp))
    conn.row_factory = sqlite3.Row
    return conn

def _lookup_latest_outbound_message(phone_e164: str, text: str, since_unix_s: float, within_seconds: float = 120.0):
    """
    Find the most recent outbound message to the given phone (matching last10) whose
    text matches exactly (or fuzzy prefix) after since_unix_s. Returns a dict or None.
    """
    want10 = _last10_digits(phone_e164)

    # Build a time window in Apple epoch units (nanoseconds)
    apple_since_ns = _unix_s_to_apple_epoch_ns(since_unix_s - 5.0)  # 5s headroom
    apple_until_ns = _unix_s_to_apple_epoch_ns(since_unix_s + within_seconds)

    # We’ll try exact match first, then a fuzzy fallback on prefix if needed.
    sql_base = """
    SELECT
        m.ROWID as message_rowid,
        m.guid,
        m.text,
        m.date,
        m.date_delivered,
        m.is_from_me,
        m.is_sent,
        m.is_delivered,
        m.error,
        m.service,
        h.id as handle_id_str
    FROM message m
    JOIN handle h ON m.handle_id = h.ROWID
    WHERE m.is_from_me = 1
      AND m.date BETWEEN ? AND ?
      AND h.id IS NOT NULL
    ORDER BY m.date DESC
    LIMIT 50
    """
    try:
        with _open_chatdb() as conn:
            rows = conn.execute(sql_base, (apple_since_ns, apple_until_ns)).fetchall()
    except Exception as e:
        return {"status": "UNKNOWN", "reason": f"chat.db access error: {e}", "raw": None}

    # Filter to matching handle (last10) and text match
    def _row_status(row):
        delivered = (row["is_delivered"] == 1) or (row["date_delivered"] and row["date_delivered"] > 0)
        failed = (row["error"] or 0) > 0
        svc = (row["service"] or "").upper()
        return delivered, failed, svc

    # Try exact text match first
    for r in rows:
        if _last10_digits(r["handle_id_str"]) == want10 and (r["text"] or "") == text:
            delivered, failed, svc = _row_status(r)
            if failed:
                return {"status": "FAILED", "service": svc, "reason": "message.error > 0", "raw": dict(r)}
            if delivered:
                return {"status": "DELIVERED", "service": svc, "reason": "is_delivered/date_delivered", "raw": dict(r)}
            return {"status": "SENT", "service": svc, "reason": "sent but not (delivered/failed) yet", "raw": dict(r)}

    # Fallback: fuzzy prefix match (first 120 chars) to handle trimmed/templated changes
    prefix = (text or "")[:120]
    for r in rows:
        if _last10_digits(r["handle_id_str"]) == want10 and (r["text"] or "").startswith(prefix):
            delivered, failed, svc = _row_status(r)
            if failed:
                return {"status": "FAILED", "service": svc, "reason": "message.error > 0 (fuzzy match)", "raw": dict(r)}
            if delivered:
                return {"status": "DELIVERED", "service": svc, "reason": "is_delivered/date_delivered (fuzzy)", "raw": dict(r)}
            return {"status": "SENT", "service": svc, "reason": "sent but not (delivered/failed) yet (fuzzy)", "raw": dict(r)}

    return None  # not found in window

def poll_message_delivery(phone: str, message: str, start_unix_s: float, max_wait_s: float = 60.0, interval_s: float = 2.0):
    """
    Poll chat.db for up to max_wait_s to resolve status. Returns a dict:
    {status: DELIVERED|FAILED|SENT|UNKNOWN, service: 'IMESSAGE'|'SMS'|..., reason: str, likely_landline: bool}
    """
    deadline = time.time() + max_wait_s
    last_seen = None
    while time.time() < deadline:
        res = _lookup_latest_outbound_message(phone, message, since_unix_s=start_unix_s, within_seconds=max_wait_s + 10)
        if isinstance(res, dict):
            last_seen = res
            if res["status"] in ("DELIVERED", "FAILED"):
                # early exit on decisive outcome
                break
        time.sleep(interval_s)

    if not last_seen:
        return {"status": "UNKNOWN", "service": None, "reason": "no matching row found", "likely_landline": False}

    svc = last_seen.get("service") or ""
    raw = last_seen.get("raw") or {}
    likely_landline = (svc == "SMS" and last_seen["status"] == "FAILED")

    return {
        "status": last_seen["status"],
        "service": svc,
        "reason": last_seen.get("reason", ""),
        "likely_landline": likely_landline,
        "raw": raw,
    }


# ------------------------------
# Templates (inline for a single-file app)
# ------------------------------

BASE_HTML = """ <!doctype html>
<html lang="en">
<head>
 <meta charset="utf-8" />
 <meta name="viewport" content="width=device-width, initial-scale=1" />
 <title>iMessage Bulk Sender</title>
 <script src="https://cdn.tailwindcss.com"></script>
 <style>
 :root { color-scheme: dark; }
 body { background: radial-gradient(900px 480px at 0% -10%, #22d3ee22,
transparent 60%),
 radial-gradient(800px 520px at 100% 10%, #86efac22,
transparent 60%),
 linear-gradient(180deg,#0b1220 0%,#0b1220 60%,#0b1220
100%); }
 </style>
</head>
<body class="text-slate-100">
 <div class="max-w-6xl mx-auto p-6">
 <header class="mb-6 flex items-center justify-between">
 <h1 class="text-2xl font-bold">iMessage Bulk Sender</h1>
 <a href="{{ url_for('index') }}" class="text-sky-300 hover:textsky-200">Home</a>
 </header>
 {% with messages = get_flashed_messages() %}
 {% if messages %}
 <div class="mb-4 space-y-2">
 {% for m in messages %}
 <div class="bg-amber-500/20 border border-amber-500/40 rounded
p-3">{{ m }}</div>
 {% endfor %}
 </div>
 {% endif %}
 {% endwith %}
 {{ body|safe }}
 </div>
</body>
</html>"""

INDEX_HTML_BODY = """
<div class="grid gap-6 md:grid-cols-2">
<form class="bg-white/5 border border-white/10 rounded-2xl p-6 backdrop-blur" action="{{ url_for('upload') }}" method="post" enctype="multipart/form-data">
<h2 class="text-xl font-semibold mb-4">1) Upload CSV</h2>
<input class="block w-full text-sm file:mr-4 file:py-2 file:px-4 file:rounded-xl file:border-0 file:text-sm file:font-semibold file:bg-sky-600 file:text-white hover:file:bg-sky-500 cursor-pointer" type="file" name="csv_file" accept=".csv" required>
<p class="text-sm text-slate-400 mt-2">Headers like <code>phone</code>, <code>name</code>, <code>business</code>, <code>address</code> are recognized automatically.</p>
<div class="mt-6">
<label class="block text-sm font-medium mb-2" for="template_a">2) Message Template A</label>
<textarea id="template_a" name="template_a" rows="6" class="w-full rounded-xl bg-black/30 border border-white/10 p-3" required>{{ default_template_a }}</textarea>
</div>
<div class="mt-6">
<label class="block text-sm font-medium mb-2" for="template_b">3) Message Template B</label>
<textarea id="template_b" name="template_b" rows="6" class="w-full rounded-xl bg-black/30 border border-white/10 p-3" required>{{ default_template_b }}</textarea>
<p class="text-xs text-slate-400 mt-2">Use Jinja placeholders, e.g. <code>{{ "{{name}}" }}</code>, <code>{{ "{{business}}" }}</code>, <code>{{ "{{address}}" }}</code></p>
</div>
<div class="mt-6 flex items-center gap-3">
<button class="px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 font-semibold" type="submit">Upload & Preview</button>
</div>
</form>


<div class="bg-white/5 border border-white/10 rounded-2xl p-6">
<h2 class="text-xl font-semibold mb-2">Preview Example</h2>
<p class="text-sm text-slate-400 mb-3">(Live sample with dummy data)</p>
<p class="text-xs text-slate-400 mb-1">Variant A</p>
<div class="rounded-xl bg-black/40 border border-white/10 p-4 mb-4" id="livePreviewA"></div>
<p class="text-xs text-slate-400 mb-1">Variant B</p>
<div class="rounded-xl bg-black/40 border border-white/10 p-4" id="livePreviewB"></div>
</div>
</div>
<script>
const sample = {name: "Alex", business: "Rivera Tree Co.", address: "123 Walnut St, Pittsburgh"};
const textareaA = document.getElementById('template_a');
const textareaB = document.getElementById('template_b');
const previewA = document.getElementById('livePreviewA');
const previewB = document.getElementById('livePreviewB');
function render() {
  function repl(t) {
    return t.replaceAll("{{name}}", sample.name)
            .replaceAll("{{business}}", sample.business)
            .replaceAll("{{address}}", sample.address);
  }
  previewA.textContent = repl(textareaA.value);
  previewB.textContent = repl(textareaB.value);
}
textareaA.addEventListener('input', render);
textareaB.addEventListener('input', render);
render();
</script>
"""

PREVIEW_HTML_BODY = """
<form action="{{ url_for('preview') }}" method="post">
<input type="hidden" name="action" id="actionField" value="refresh">
<div class="grid md:grid-cols-3 gap-6">
<div class="md:col-span-2 bg-white/5 border border-white/10 rounded-2xl p-6">
<h2 class="text-xl font-semibold mb-4">Templates & Live Preview</h2>
<label class="block text-sm font-medium mb-2" for="template_a">Message Template A</label>
<textarea id="template_a" name="template_a" rows="6" class="w-full rounded-xl bg-black/30 border border-white/10 p-3" required>{{ template_a }}</textarea>
<label class="block text-sm font-medium mb-2 mt-4" for="template_b">Message Template B</label>
<textarea id="template_b" name="template_b" rows="6" class="w-full rounded-xl bg-black/30 border border-white/10 p-3" required>{{ template_b }}</textarea>
<p class="text-xs text-slate-400 mt-2">Use placeholders like <code>{{ "{{name}}" }}</code>, <code>{{ "{{business}}" }}</code>, <code>{{ "{{address}}" }}</code>. Unknown placeholders will show an error per row.</p>
<div class="mt-4 flex items-center gap-3">
<button class="px-4 py-2 rounded-xl bg-sky-600 hover:bg-sky-500 font-semibold" onclick="document.getElementById('actionField').value='refresh'">Refresh Preview</button>
<button class="px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 font-semibold" onclick="document.getElementById('actionField').value='send'">Send Selected</button>
<label class="ml-auto text-sm inline-flex items-center gap-2"><input type="checkbox" name="dry_run" value="1" class="scale-125"> Dry run (no send)</label>
<label class="text-sm inline-flex items-center gap-2"><input type="number" name="delay_min" min="0" step="0.1" value="1.0" class="w-20 rounded bg-black/30 border border-white/10 p-1">–<input type="number" name="delay_max" min="0" step="0.1" value="2.5" class="w-20 rounded bg-black/30 border border-white/10 p-1"> sec delay</label>
</div>
</div>


<div class="bg-white/5 border border-white/10 rounded-2xl p-6">
<h2 class="text-xl font-semibold mb-2">Stats</h2>
<ul class="text-sm text-slate-300 space-y-1">
<li>Total rows: <strong>{{ total }}</strong></li>
<li>Okay: <strong class="text-emerald-300">{{ ok_count }}</strong></li>
<li>Errors: <strong class="text-rose-300">{{ err_count }}</strong></li>
</ul>
<div class="mt-4 text-xs text-slate-400">Phone normalization default: <code>{{ default_cc }}</code>. You can change this in <code>app.py</code>.</div>
</div>
</div>


<div class="mt-8">
<h3 class="text-lg font-semibold mb-3">Rows</h3>
<div class="overflow-x-auto rounded-2xl border border-white/10">
<table class="min-w-full text-sm">
<thead class="bg-white/5">
<tr>
<th class="p-3 text-left"><input type="checkbox" id="checkAll"></th>
<th class="p-3 text-left">Phone</th>
<th class="p-3 text-left">Name</th>
<th class="p-3 text-left">Business</th>
<th class="p-3 text-left">Address</th>
<th class="p-3 text-left">Variant</th>
<th class="p-3 text-left">Preview</th>
</tr>
</thead>
<tbody>
{% for row in rows %}
<tr class="border-t border-white/10 {{ 'bg-rose-900/10' if row.error else 'bg-transparent' }}">
<td class="p-3 align-top"><input type="checkbox" name="sel" value="{{ loop.index0 }}" {% if not row.error %}checked{% endif %}></td>
<td class="p-3 align-top text-slate-200">{{ row.phone }}</td>
<td class="p-3 align-top text-slate-200">{{ row.data.get('name','') }}</td>
<td class="p-3 align-top text-slate-200">{{ row.data.get('business','') }}</td>
<td class="p-3 align-top text-slate-200">{{ row.data.get('address','') }}</td>
<td class="p-3 align-top text-slate-200">{{ row.variant }}</td>
<td class="p-3 align-top">
{% if row.error %}
<div class="text-rose-300">⚠️ {{ row.error }}</div>
{% else %}
<div class="whitespace-pre-wrap text-slate-100">{{ row.preview }}</div>
{% endif %}
</td>
</tr>
{% endfor %}
</tbody>
</table>
</div>
</div>
</form>
<script>
const checkAll = document.getElementById('checkAll');
checkAll?.addEventListener('change', (e) => {
document.querySelectorAll('input[name="sel"]').forEach(cb => cb.checked = e.target.checked);
});
</script>
"""
RESULTS_HTML_BODY = """
<div class="bg-white/5 border border-white/10 rounded-2xl p-6">
<h2 class="text-xl font-semibold mb-2">Send Results</h2>
<p class="text-sm text-slate-300 mb-4">{{ sent_success }} succeeded, {{ sent_failed }} failed. {% if dry_run %}<span class="text-amber-300">(Dry run — no messages actually sent)</span>{% endif %}</p>
{% if log_filename %}
<a class="inline-block px-4 py-2 rounded-xl bg-sky-600 hover:bg-sky-500 font-semibold" href="{{ url_for('download_log', filename=log_filename) }}">Download log CSV</a>
{% endif %}
<div class="mt-6 overflow-x-auto rounded-2xl border border-white/10">
<table class="min-w-full text-sm">
<thead class="bg-white/5">
<tr>
  <th class="p-3 text-left">Phone</th>
  <th class="p-3 text-left">Variant</th>
  <th class="p-3 text-left">Name</th>
  <th class="p-3 text-left">OK?</th>
  <th class="p-3 text-left">Status</th>
  <th class="p-3 text-left">Channel</th>
  <th class="p-3 text-left">Likely Landline</th>
  <th class="p-3 text-left">Message</th>
  <th class="p-3 text-left">Error</th>
 </tr>
</thead>
<tbody>
{% for r in results %}
<tr class="border-t border-white/10">
  <td class="p-3 align-top">{{ r.phone }}</td>
  <td class="p-3 align-top">{{ r.variant }}</td>
  <td class="p-3 align-top">{{ r.data.get('name','') }}</td>
  <td class="p-3 align-top">{% if r.ok %}<span class="text-emerald-300">YES</span>{% else %}<span class="text-rose-300">NO</span>{% endif %}</td>
  <td class="p-3 align-top">{{ r.status or '' }}</td>
  <td class="p-3 align-top">{{ r.service or '' }}</td>
  <td class="p-3 align-top">{% if r.likely_landline %}✅{% else %}—{% endif %}</td>
  <td class="p-3 align-top whitespace-pre-wrap">{{ r.message }}</td>
  <td class="p-3 align-top text-rose-300">{{ r.error or '' }}</td>
</tr>
{% endfor %}
</tbody>

</table>
</div>
</div>
"""
# ------------------------------
# Routes
# ------------------------------

@app.route("/")
def index():
    body = render_template_string(
        INDEX_HTML_BODY,
        default_template_a=(
            "Hey {{name}}, I see {{business or 'your business'}} at {{address}}. "
            "Just wondering how business is going?"
        ),
        default_template_b=(
            "Hi {{name}}, do you need more jobs around {{address}}? "
            "We can send qualified tree leads."
        ),
    )
    return render_template_string(BASE_HTML, body=body)

@app.post("/upload")
def upload():
    file = request.files.get("csv_file")
    template_a = request.form.get("template_a", "").strip()
    template_b = request.form.get("template_b", "").strip()
    if not file or not template_a or not template_b:
        flash("CSV and both templates are required.")
        return redirect(url_for("index"))

    content = file.read().decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(content))
    headers = reader.fieldnames or []
    if not headers:
        flash("Could not read CSV headers.")
        return redirect(url_for("index"))

    mapping = normalize_headers(headers)
    rows = []
    for raw_row in reader:
        # normalize key casing and strip; skip keys that are None (extra fields)
        norm = {}
        for k, v in raw_row.items():
            if k is None:
                # Extra, unmatched values due to unquoted commas, etc. Ignore safely.
                # If desired, these could be joined to a specific field.
                continue
            lk = ((k or "").strip().lower())
            if not lk:
                # Skip empty header names
                continue
            norm_key = mapping.get(lk, lk)
            norm[norm_key] = (str(v).strip() if v is not None else "")
        norm_phone = normalize_phone(norm.get("phone", ""))
        norm["phone"] = norm_phone
        rows.append(norm)

    data_id = session.get("data_id") or secrets.token_hex(8)
    session["data_id"] = data_id
    DATA_STORE[data_id] = {
        "rows": rows,
        "template_a": template_a,
        "template_b": template_b,
    }
    return redirect(url_for("preview"))

@app.route("/preview", methods=["GET", "POST"])
def preview():
    data_id = session.get("data_id")
    data = DATA_STORE.get(data_id)
    if not data:
        flash("No CSV loaded yet.")
        return redirect(url_for("index"))

    template_a = (request.form.get("template_a") or data.get("template_a") or "").strip()
    template_b = (request.form.get("template_b") or data.get("template_b") or "").strip()
    action = request.form.get("action") or "refresh"
    rows_raw: List[Dict[str, Any]] = data.get("rows", [])
    data["template_a"] = template_a
    data["template_b"] = template_b
    DATA_STORE[data_id] = data

    # Build previews with error capture
    preview_rows = []
    ok_count = 0
    err_count = 0
    for r in rows_raw:
        context = dict(r)
        phone = context.get("phone", "")
        if not phone:
            preview_rows.append({
                "data": r,
                "phone": phone,
                "preview": "",
                "variant": "A",
                "error": "Missing phone",
            })
            err_count += 1
            continue
        variant = "A" if int(hashlib.md5(phone.encode()).hexdigest(), 16) % 2 == 0 else "B"
        tmpl = template_a if variant == "A" else template_b
        try:
            msg = jinja_render(tmpl, context)
            preview_rows.append({
                "data": r,
                "phone": phone,
                "preview": msg,
                "variant": variant,
                "error": None,
            })
            ok_count += 1
        except Exception as e:
            preview_rows.append({
                "data": r,
                "phone": phone,
                "preview": "",
                "variant": variant,
                "error": f"Template error: {e}",
            })
            err_count += 1

    total = len(rows_raw)

    # If sending
    if action == "send":
        dry_run = bool(request.form.get("dry_run"))
        selected_indices = request.form.getlist("sel")
        chosen = [preview_rows[int(i)] for i in selected_indices if i.isdigit() and int(i) < len(preview_rows)]
        delay_min = float(request.form.get("delay_min", 1.0))
        delay_max = float(request.form.get("delay_max", 2.5))
        if delay_max < delay_min:
            delay_max = delay_min

        results = []
        sent_success = 0
        sent_failed = 0

        for row in chosen:
            phone = row["phone"]
            msg = row["preview"]
            variant = row.get("variant", "A")
            data_for_row = row["data"]
            if row["error"] or not phone or not msg:
                results.append({
                    "ok": False,
                    "phone": phone,
                    "variant": variant,
                    "message": msg,
                    "error": row["error"],
                    "data": data_for_row,
                    "status": "SKIPPED",
                    "service": None,
                    "likely_landline": False,
                })
                sent_failed += 1
                continue

            try:
                send_start = None
                if not dry_run:
                    send_start = send_imessage(phone, msg)
                else:
                    send_start = time.time()

                # throttle a bit to be polite / avoid rate limiting
                time.sleep(random.uniform(delay_min, delay_max))

                # Poll chat.db for delivery/failure (even in dry_run we skip real lookup)
                if not dry_run:
                    status_info = poll_message_delivery(phone, msg, start_unix_s=send_start, max_wait_s=60.0, interval_s=2.0)
                else:
                    status_info = {"status": "DRY_RUN", "service": None, "likely_landline": False, "reason": "no send"}

                status = status_info.get("status")
                service = status_info.get("service")
                likely_landline = status_info.get("likely_landline", False)

                ok = (status == "DELIVERED") or (status == "SENT")  # treat SENT (no failure yet) as tentatively OK

                results.append({
                    "ok": ok,
                    "phone": phone,
                    "variant": variant,
                    "message": msg,
                    "error": None if ok else status_info.get("reason", "failed"),
                    "data": data_for_row,
                    "status": status,
                    "service": service,
                    "likely_landline": likely_landline,
                })
                if ok:
                    sent_success += 1
                else:
                    sent_failed += 1

            except subprocess.CalledProcessError as e:
                results.append({
                    "ok": False,
                    "phone": phone,
                    "variant": variant,
                    "message": msg,
                    "error": e.stderr.decode("utf-8", errors="ignore"),
                    "data": data_for_row,
                    "status": "FAILED",
                    "service": None,
                    "likely_landline": False,
                })
                sent_failed += 1
            except Exception as e:
                results.append({
                    "ok": False,
                    "phone": phone,
                    "variant": variant,
                    "message": msg,
                    "error": str(e),
                    "data": data_for_row,
                    "status": "FAILED",
                    "service": None,
                    "likely_landline": False,
                })
                sent_failed += 1


        # write log
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_filename = f"send_log_{ts}.csv"
        log_path = os.path.join(LOG_DIR, log_filename)
        with open(log_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "phone", "variant", "name", "business", "address",
                "ok", "status", "service", "likely_landline",
                "error", "message"
            ])
            for r in results:
                w.writerow([
                    r["phone"],
                    r.get("variant", ""),
                    r["data"].get("name", ""),
                    r["data"].get("business", ""),
                    r["data"].get("address", ""),
                    "1" if r["ok"] else "0",
                    r.get("status", ""),
                    r.get("service", "") or "",
                    "1" if r.get("likely_landline") else "0",
                    (r.get("error") or "").replace("\n", " "),
                    r["message"],
                ])


        body = render_template_string(
            RESULTS_HTML_BODY,
            results=results,
            sent_success=sent_success,
            sent_failed=sent_failed,
            log_filename=log_filename,
            dry_run=dry_run,
        )
        return render_template_string(BASE_HTML, body=body)

    # Otherwise show preview table
    body = render_template_string(
        PREVIEW_HTML_BODY,
        rows=preview_rows,
        template_a=template_a,
        template_b=template_b,
        total=total,
        ok_count=ok_count,
        err_count=err_count,
        default_cc=DEFAULT_COUNTRY_CODE,
    )
    return render_template_string(BASE_HTML, body=body)

@app.route("/logs/<path:filename>")
def download_log(filename: str):
    return send_from_directory(LOG_DIR, filename, as_attachment=True)

@app.post("/api/send")
def api_send():
    data = request.get_json(force=True)
    phone = normalize_phone(data.get("phone"))
    template = data.get("template", "")
    context = data.get("context", {})

    if not phone or not template:
        return {"ok": False, "error": "phone and template required"}, 400

    try:
        msg = jinja_render(template, context)
        send_start = send_imessage(phone, msg)
        status_info = poll_message_delivery(phone, msg, start_unix_s=send_start)

        return {
            "ok": status_info["status"] in ("DELIVERED", "SENT"),
            "phone": phone,
            "message": msg,
            **status_info
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
