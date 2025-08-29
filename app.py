from flask import Flask, render_template, request, redirect, url_for, session
import os
import requests
import stripe
import smtplib
from email.message import EmailMessage
from dotenv import load_dotenv
from datetime import datetime


app = Flask(__name__)
app.secret_key = 'change_this_secret_key'  # Needed for session
#airtable api key from .env
load_dotenv()
airtable_api_key = os.getenv('AIRTABLE_API_KEY')
airtable_base_id = os.getenv('AIRTABLE_BASE_ID')
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
stripe_public_key = os.getenv('STRIPE_PUBLISHABLE_KEY')
smtp_server = os.getenv('SMTP_SERVER')
smtp_port = int(os.getenv('SMTP_PORT', 587))
smtp_user = os.getenv('SMTP_USER')
smtp_password = os.getenv('SMTP_PASSWORD')


def fetch_lead(uuid):
    """Fetch a single lead from Airtable by UUID."""
    url = f"https://api.airtable.com/v0/{airtable_base_id}/Leads"
    headers = {
        "Authorization": f"Bearer {airtable_api_key}"
    }
    params = {
        "filterByFormula": f"{{Lead ID}}='{uuid}'"
    }
    resp = requests.get(url, headers=headers, params=params)
    records = resp.json().get('records', [])
    print(records)
    if not records:
        return None
    return records[0]['fields']


def mask_customer_details(fields):
    exclude = [
        'Customer Name', 'Customer Email', 'Customer Phone', 'Customer Contact', 'Seller', 
        'Lead Summary (AI)', 'Lead Category (AI)', 'Lead ID', 'Status', 'Sold Price ($)', 
        'Admin Fee 1% ($)', 'Interest Count', 'Total Payouts', 'Contact Name', 'Contact Email']
    return {k: v for k, v in fields.items() if k not in exclude}


def send_lead_email(to_email, lead_fields):
    if not (smtp_server and smtp_user and smtp_password):
        return
    msg = EmailMessage()
    msg['Subject'] = 'Lead Details'
    msg['From'] = smtp_user
    msg['To'] = to_email
    body = '\n'.join(f"{k}: {v}" for k, v in lead_fields.items())
    msg.set_content(body)
    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)


def fetch_all_leads():
    """Fetch all leads from Airtable."""
    url = f"https://api.airtable.com/v0/{airtable_base_id}/Leads"
    headers = {"Authorization": f"Bearer {airtable_api_key}"}
    resp = requests.get(url, headers=headers)
    records = resp.json().get("records", [])
    return [rec.get("fields", {}) for rec in records]


def format_date_time(date_time_str):
    # Translate date time string to a more readable format
    # example : 2025-08-25T02:28:58.000Z to 08-25-2025 14:28:58
    date_time_obj = datetime.strptime(date_time_str, "%Y-%m-%dT%H:%M:%S.%fZ")
    return date_time_obj.strftime("%m-%d-%Y %H:%M:%S")

@app.route('/')
def index():
    return render_template('index.html')



@app.route('/leads')
def public_leads():
    fields = fetch_all_leads()
    leads = []
    for f in fields:
        lead = {
            'uuid': f.get('Lead ID', ''),
            'Category': f.get('Category', ''),
            'Lead Age': f.get('Lead Age', ''),
            'City/ZIP': f.get('City/ZIP', ''),
            'Description': f.get('Description', ''),
            'Asking Price ($)': f.get('Asking Price ($)', ''),
            'Created 2': format_date_time(f.get('Created 2', ''))
        }
        leads.append(lead)
    categories = sorted({l['Category'] for l in leads if l['Category']})
    return render_template('leads.html', leads=leads, categories=categories, publishable_key=stripe_public_key)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == 'admin' and password == 'password':
            session['logged_in'] = True
            return redirect(url_for('admin'))
        else:
            return render_template('login.html', error='Invalid credentials')
    if session.get('logged_in'):
        return redirect(url_for('admin'))
    return render_template('login.html')

@app.route('/admin')
def admin():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return render_template('admin.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))


@app.route('/admin/leads')
def leads():
    # An endpoint to return all the leads and details about them
    # This information should be pulled from airtable
    # This information should be returned in a json format
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    
    #get all the leads from airtable
    url = f'https://api.airtable.com/v0/{airtable_base_id}/Leads'
    headers = {
        'Authorization': f'Bearer {airtable_api_key}'
    }
    response = requests.get(url, headers=headers)
    return response.json()

@app.route('/admin/businesses')
def businesses():
    # An endpoint to return all the businesses and details about them
    # This information should be pulled from airtable
    # This information should be returned in a json format
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    url = f'https://api.airtable.com/v0/{airtable_base_id}/Businesses'
    headers = {
        'Authorization': f'Bearer {airtable_api_key}'
    }
    response = requests.get(url, headers=headers)
    return response.json()


@app.route('/lead/<uuid>')
def lead_detail(uuid):
    fields = fetch_lead(uuid)
    if not fields:
        return "Lead not found", 404
    display = mask_customer_details(fields)
    return render_template('lead.html', lead=display, uuid=uuid, publishable_key=stripe_public_key)


@app.route('/create-checkout-session/<uuid>', methods=['POST'])
def create_checkout_session(uuid):
    fields = fetch_lead(uuid)
    if not fields:
        return {"error": "Lead not found"}, 404
    amount = fields.get('Price') or fields.get('Asking Price ($)', 0)
    price = int(float(amount) * 100)
    session = stripe.checkout.Session.create(
        mode='payment',
        line_items=[{
            'price_data': {
                'currency': 'usd',
                'product_data': {'name': fields.get('Description', 'Lead')},
                'unit_amount': price
            },
            'quantity': 1
        }],
        success_url=url_for('lead_success', uuid=uuid, _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
        cancel_url=url_for('lead_detail', uuid=uuid, _external=True)
    )
    return {'id': session.id}


@app.route('/lead/<uuid>/success')
def lead_success(uuid):
    session_id = request.args.get('session_id')
    if not session_id:
        return redirect(url_for('lead_detail', uuid=uuid))
    checkout_session = stripe.checkout.Session.retrieve(session_id)
    customer_email = checkout_session.get('customer_details', {}).get('email')
    fields = fetch_lead(uuid)
    if customer_email and fields:
        send_lead_email(customer_email, fields)
    return render_template('lead_success.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
