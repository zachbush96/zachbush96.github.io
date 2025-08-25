from flask import Flask, render_template, request, redirect, url_for, session
import os
from dotenv import load_dotenv


app = Flask(__name__)
app.secret_key = 'change_this_secret_key'  # Needed for session
#airtable api key from .env
load_dotenv()
airtable_api_key = os.getenv('AIRTABLE_API_KEY')
airtable_base_id = os.getenv('AIRTABLE_BASE_ID')


@app.route('/')
def index():
    return render_template('index.html')


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

if __name__ == '__main__':
    app.run(debug=True)
