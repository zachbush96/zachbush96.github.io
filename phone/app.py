#!/usr/bin/env python3

import csv
import io
import os
import re
import time
import random
import secrets
import subprocess
from datetime import datetime
from typing import List, Dict, Any
from flask import (
    Flask, request, redirect, url_for, render_template_string,
    session, send_from_directory, flash
)
from jinja2 import Environment, StrictUndefined

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
    "business": {"business", "company", "business_name"},
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
on run {targetPhone, targetMessage}
    tell application "Messages"
        -- Prefer iMessage service
        set iService to first service whose service type = iMessage
        try
            set theChat to make new text chat with properties {service:iService, participants:{targetPhone}}
        on error errMsg number errNum
            -- Optional SMS fallback (uncomment to enable)
            -- set sService to first service whose service type = SMS
            -- set theChat to make new text chat with properties {service:sService, participants:{targetPhone}}
        end try
        send targetMessage to theChat
    end tell
end run
'''

def send_imessage(phone: str, message: str) -> None:
    """Send a single iMessage via AppleScript. Raises CalledProcessError on failure."""
    # Use osascript with inline script and pass arguments to avoid quoting issues
    completed = subprocess.run(
        ["osascript", "-", phone, message],
        input=APPLE_SCRIPT_SEND.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return

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
<label class="block text-sm font-medium mb-2" for="template">2) Message Template</label>
<textarea id="template" name="template" rows="6" class="w-full rounded-xl bg-black/30 border border-white/10 p-3" required>{{ default_template }}</textarea>
<p class="text-xs text-slate-400 mt-2">Use Jinja placeholders, e.g. <code>{{ "{{name}}" }}</code>, <code>{{ "{{business}}" }}</code>, <code>{{ "{{address}}" }}</code></p>
</div>
<div class="mt-6 flex items-center gap-3">
<button class="px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 font-semibold" type="submit">Upload & Preview</button>
</div>
</form>


<div class="bg-white/5 border border-white/10 rounded-2xl p-6">
<h2 class="text-xl font-semibold mb-2">Preview Example</h2>
<p class="text-sm text-slate-400 mb-3">(Live sample with dummy data)</p>
<div class="rounded-xl bg-black/40 border border-white/10 p-4" id="livePreview"></div>
</div>
</div>
<script>
const sample = {name: "Alex", business: "Rivera Tree Co.", address: "123 Walnut St, Pittsburgh"};
const textarea = document.getElementById('template');
const preview = document.getElementById('livePreview');
function render() {
let t = textarea.value;
// naive preview: just replace common handles for the demo box
preview.textContent = t.replaceAll("{{name}}", sample.name)
.replaceAll("{{business}}", sample.business)
.replaceAll("{{address}}", sample.address);
}
textarea.addEventListener('input', render);
render();
</script>
"""

PREVIEW_HTML_BODY = """
<form action="{{ url_for('preview') }}" method="post">
<input type="hidden" name="action" id="actionField" value="refresh">
<div class="grid md:grid-cols-3 gap-6">
<div class="md:col-span-2 bg-white/5 border border-white/10 rounded-2xl p-6">
<h2 class="text-xl font-semibold mb-4">Template & Live Preview</h2>
<label class="block text-sm font-medium mb-2" for="template">Message Template</label>
<textarea id="template" name="template" rows="6" class="w-full rounded-xl bg-black/30 border border-white/10 p-3" required>{{ template }}</textarea>
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
<th class="p-3 text-left">Name</th>
<th class="p-3 text-left">Status</th>
<th class="p-3 text-left">Message</th>
<th class="p-3 text-left">Error</th>
</tr>
</thead>
<tbody>
{% for r in results %}
<tr class="border-t border-white/10">
<td class="p-3 align-top">{{ r.phone }}</td>
<td class="p-3 align-top">{{ r.data.get('name','') }}</td>
<td class="p-3 align-top">{% if r.ok %}<span class="text-emerald-300">OK</span>{% else %}<span class="text-rose-300">FAIL</span>{% endif %}</td>
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
        default_template=(
            "Hey {{name}}, I see {{business or 'your business'}} at {{address}}. "
            "Just wondering how business is going?"
        ),
    )
    return render_template_string(BASE_HTML, body=body)

@app.post("/upload")
def upload():
    file = request.files.get("csv_file")
    template = request.form.get("template", "").strip()
    if not file or not template:
        flash("CSV and template are required.")
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
        "template": template,
    }
    return redirect(url_for("preview"))

@app.route("/preview", methods=["GET", "POST"])
def preview():
    data_id = session.get("data_id")
    data = DATA_STORE.get(data_id)
    if not data:
        flash("No CSV loaded yet.")
        return redirect(url_for("index"))

    template = (request.form.get("template") or data.get("template") or "").strip()
    action = request.form.get("action") or "refresh"
    rows_raw: List[Dict[str, Any]] = data.get("rows", [])

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
                "error": "Missing phone",
            })
            err_count += 1
            continue
        try:
            msg = jinja_render(template, context)
            preview_rows.append({
                "data": r,
                "phone": phone,
                "preview": msg,
                "error": None,
            })
            ok_count += 1
        except Exception as e:
            preview_rows.append({
                "data": r,
                "phone": phone,
                "preview": "",
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
            data_for_row = row["data"]
            if row["error"] or not phone or not msg:
                results.append({"ok": False, "phone": phone, "message": msg, "error": row["error"], "data": data_for_row})
                sent_failed += 1
                continue
            try:
                if not dry_run:
                    send_imessage(phone, msg)
                # throttle a bit to be polite / avoid rate limiting
                time.sleep(random.uniform(delay_min, delay_max))
                results.append({"ok": True, "phone": phone, "message": msg, "error": None, "data": data_for_row})
                sent_success += 1
            except subprocess.CalledProcessError as e:
                results.append({"ok": False, "phone": phone, "message": msg, "error": e.stderr.decode("utf-8", errors="ignore"), "data": data_for_row})
                sent_failed += 1
            except Exception as e:
                results.append({"ok": False, "phone": phone, "message": msg, "error": str(e), "data": data_for_row})
                sent_failed += 1

        # write log
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_filename = f"send_log_{ts}.csv"
        log_path = os.path.join(LOG_DIR, log_filename)
        with open(log_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["phone", "name", "business", "address", "ok", "error", "message"])
            for r in results:
                w.writerow([
                    r["phone"],
                    r["data"].get("name", ""),
                    r["data"].get("business", ""),
                    r["data"].get("address", ""),
                    "1" if r["ok"] else "0",
                    (r["error"] or "").replace("\n", " "),
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
    data["template"] = template
    DATA_STORE[data_id] = data
    body = render_template_string(
        PREVIEW_HTML_BODY,
        rows=preview_rows,
        template=template,
        total=total,
        ok_count=ok_count,
        err_count=err_count,
        default_cc=DEFAULT_COUNTRY_CODE,
    )
    return render_template_string(BASE_HTML, body=body)

@app.route("/logs/<path:filename>")
def download_log(filename: str):
    return send_from_directory(LOG_DIR, filename, as_attachment=True)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
