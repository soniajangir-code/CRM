import os
import sys
import json
import time
import queue
import threading
import datetime
import requests
import csv
import io
import hashlib
from pymongo import MongoClient
from functools import wraps
from flask import Flask, render_template, request, jsonify, Response, send_file, redirect, url_for, session
from werkzeug.utils import secure_filename

# Import local scraper engines and gateway functions
from scrapers.gmaps_scraper import scrape_google_maps
from scrapers.directory_scraper import scrape_directory
from scrapers.hospital_scraper import scrape_hospitals
from crm_gate import load_mapping_config, map_headers, deduplicate_and_save, clean_phone

app = Flask(__name__)
app.secret_key = 'crm-leads-gate-secret-key-12345'
app.config['UPLOAD_FOLDER'] = 'input_csv'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB limit

# MongoDB Connection
MONGO_URI = "mongodb+srv://livelong:9680796461@cluster0.2xwtrmi.mongodb.net/healthcare_app?retryWrites=true&w=majority&appName=Cluster0"
try:
    # Connect to MongoDB cluster using dnspython and pymongo
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = mongo_client["healthcare_app"]
    users_col = db["users"]
    # Test connection
    mongo_client.server_info()
    print("[MongoDB] Connected successfully to Cluster0: healthcare_app database.")
    
    # Auto-initialize default user if not exists
    if users_col.count_documents({"email": "yeshsharma123@gmail.com"}) == 0:
        default_pwd_hash = hashlib.sha256(b"123456").hexdigest()
        users_col.insert_one({
            "email": "yeshsharma123@gmail.com",
            "password": default_pwd_hash,
            "created_at": datetime.datetime.now()
        })
        print("[MongoDB] Registered default user: yeshsharma123@gmail.com / 123456")
except Exception as e:
    print(f"[MongoDB] Connection failed: {e}")
    db = None
    users_col = None

# Default credentials fallback
ADMIN_EMAIL = "yeshsharma123@gmail.com"
ADMIN_PASSWORD = "123456"

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Create upload folder if not exists
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

# Global log queue for streaming scraper logs to UI
log_queue = queue.Queue()

class LogCapture:
    def __init__(self, q, original_stdout):
        self.q = q
        self.original_stdout = original_stdout
        
    def write(self, message):
        self.original_stdout.write(message)
        self.original_stdout.flush()
        if message.strip():
            # Send message to queue
            self.q.put(message.strip())
            
    def flush(self):
        self.original_stdout.flush()

def sync_to_gsheet(records, gsheet_url):
    """
    Sends records to the deployed Google Apps Script Web App URL.
    """
    if not gsheet_url:
        print("[GSheet Sync] No Google Sheet URL configured. Skipping Google Sheet synchronization.")
        return False
        
    print(f"[GSheet Sync] Connecting to Google Sheet endpoint...")
    try:
        # Apps Script expects a JSON array of record dicts
        response = requests.post(gsheet_url, json=records, headers={"Content-Type": "application/json"}, timeout=15)
        if response.status_code == 200:
            print(f"[GSheet Sync] SUCCESS! Successfully sent {len(records)} records to your Google Sheet.")
            return True
        else:
            print(f"[GSheet Sync] ERROR: Status code {response.status_code}. Response: {response.text}")
            return False
    except Exception as e:
        print(f"[GSheet Sync] ERROR connecting to Google Sheet: {e}")
        return False

def run_scraper_thread(source, department, location, gsheet_url):
    """
    Runs the scraper in a separate thread, redirects stdout to stream logs,
    and syncs the results to Google Sheet / local CSV.
    """
    original_stdout = sys.stdout
    capture = LogCapture(log_queue, original_stdout)
    sys.stdout = capture
    
    try:
        # Load headless configuration from config.json
        config_path = 'config.json'
        headless = False
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    headless = json.load(f).get('headless', False)
            except Exception:
                pass
                
        print(f"[Scraper Thread] Initiating automated search: '{department}' in '{location}' from '{source}' (Headless: {headless})...")
        
        new_records = []
        if source == "Google Maps":
            new_records = scrape_google_maps(department, location, headless=headless)
        elif source in ["JustDial", "Trade India", "IndiaMart"]:
            new_records = scrape_directory(source, department, location, headless=headless)
        elif source == "Hospital Websites":
            new_records = scrape_hospitals(department, location, headless=headless)
        
        if new_records:
            print(f"[Scraper Thread] Extraction complete. Scraped {len(new_records)} records.")
            
            # Save to local CSV (deduplicated)
            print("[Scraper Thread] Saving records locally to master_dataset.csv...")
            deduplicate_and_save(new_records)
            
            # Sync to Google Sheets if URL provided
            if gsheet_url:
                sync_to_gsheet(new_records, gsheet_url)
            else:
                print("[Scraper Thread] Note: Google Sheet URL is not configured. Data is saved locally in master_dataset.csv.")
        else:
            print("[Scraper Thread] Scraping complete, but no valid records with phone numbers were found.")
            
    except Exception as e:
        print(f"[Scraper Thread] Unexpected critical error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Restore normal stdout
        sys.stdout = original_stdout
        # Signal to log stream that the thread is finished
        log_queue.put("=== SCRAPE JOB COMPLETED ===")

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        
        pwd_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()
        
        authenticated = False
        if users_col is not None:
            try:
                user = users_col.find_one({"email": email})
                if user and user.get("password") == pwd_hash:
                    authenticated = True
            except Exception as e:
                print(f"[MongoDB] Error checking credentials: {e}")
                
        # Fallback to local hardcoded check if DB is down
        if not authenticated and users_col is None:
            if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
                authenticated = True
                
        if authenticated:
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            error = "Invalid email or password. Please try again."
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/api/config', methods=['GET', 'POST'])
@login_required
def config():
    config_path = 'config.json'
    
    # Load config
    cfg = {"gsheet_url": ""}
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            try:
                cfg = json.load(f)
            except Exception:
                pass
                
    if request.method == 'POST':
        data = request.json or {}
        cfg['gsheet_url'] = data.get('gsheet_url', '').strip()
        with open(config_path, 'w') as f:
            json.dump(cfg, f, indent=2)
        return jsonify({"status": "success", "config": cfg})
        
    return jsonify(cfg)

@app.route('/api/scrape', methods=['POST'])
@login_required
def scrape():
    data = request.json or {}
    source = data.get('source')
    department = data.get('department')
    location = data.get('location')
    
    if not source or not department or not location:
        return jsonify({"status": "error", "message": "Missing required parameters (source, department, location)."})
        
    # Get the Google Sheet URL from config
    config_path = 'config.json'
    gsheet_url = ""
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            try:
                gsheet_url = json.load(f).get('gsheet_url', '')
            except Exception:
                pass
                
    # Start the scraper thread
    thread = threading.Thread(target=run_scraper_thread, args=(source, department, location, gsheet_url))
    thread.daemon = True
    thread.start()
    
    return jsonify({"status": "success", "message": "Scraper job started."})

@app.route('/api/upload', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file uploaded."}), 400
        
    file = request.files['file']
    source = request.form.get('source', 'Generic')
    department = request.form.get('department', 'General')
    location = request.form.get('location', 'India')
    
    if file.filename == '':
        return jsonify({"status": "error", "message": "No selected file."}), 400
        
    if file and file.filename.lower().endswith('.csv'):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        # Load config to get Google Sheet URL
        config_path = 'config.json'
        gsheet_url = ""
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                try:
                    gsheet_url = json.load(f).get('gsheet_url', '')
                except Exception:
                    pass
                    
        # Process the CSV
        mappings = load_mapping_config()
        new_records = []
        
        try:
            try:
                f = open(filepath, "r", encoding="utf-8")
                reader = csv.DictReader(f)
                rows = list(reader)
                f.close()
            except Exception:
                # Fallback to Latin-1
                import csv
                f = open(filepath, "r", encoding="latin-1")
                reader = csv.DictReader(f)
                rows = list(reader)
                f.close()
                
            for row in rows:
                mapped = map_headers(row, source, mappings, department, location)
                if source == "Generic":
                    mapped["Data Source"] = f"CSV: {filename}"
                new_records.append(mapped)
                
            if new_records:
                # Save locally (deduplicated)
                deduplicate_and_save(new_records)
                
                # Push to GSheet
                gsheet_success = False
                if gsheet_url:
                    # Filter out empty phone numbers before pushing
                    valid_records = [r for r in new_records if r.get("Phone")]
                    gsheet_success = sync_to_gsheet(valid_records, gsheet_url)
                    
                # Clean up uploaded file
                try:
                    os.remove(filepath)
                except Exception:
                    pass
                    
                return jsonify({
                    "status": "success", 
                    "message": f"Successfully parsed {len(new_records)} rows from your file, deduplicated, and updated the master dataset.",
                    "gsheet_synced": gsheet_success
                })
            else:
                return jsonify({"status": "error", "message": "CSV file was empty or had no rows."})
                
        except Exception as e:
            return jsonify({"status": "error", "message": f"Error parsing CSV: {str(e)}"}), 500
            
    return jsonify({"status": "error", "message": "Invalid file type. Please upload a CSV file."}), 400

@app.route('/api/download')
@login_required
def download_csv():
    master_file = "master_dataset.csv"
    if os.path.exists(master_file):
        return send_file(master_file, as_attachment=True, download_name="master_dataset.csv", mimetype="text/csv")
    else:
        return "Master dataset CSV file does not exist yet. Run a scrape campaign or upload a CSV first.", 404

@app.route('/api/download-new')
@login_required
def download_new_csv():
    master_file = "master_dataset.csv"
    downloaded_file = "downloaded_phones.json"
    
    if not os.path.exists(master_file):
        return "No leads found in database. Run a scrape campaign or upload a CSV first.", 404
        
    downloaded_phones = set()
    if os.path.exists(downloaded_file):
        try:
            with open(downloaded_file, "r") as f:
                downloaded_phones = set(json.load(f))
        except Exception:
            pass
            
    new_rows = []
    headers = []
    
    try:
        with open(master_file, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames
            if headers:
                for row in reader:
                    phone = clean_phone(row.get("Phone", ""))
                    if phone and phone not in downloaded_phones:
                        new_rows.append(row)
    except Exception as e:
        return f"Error reading master dataset: {e}", 500
        
    if not new_rows:
        return "All leads have already been downloaded! No new leads found.", 400
        
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()
    writer.writerows(new_rows)
    
    for row in new_rows:
        phone = clean_phone(row.get("Phone", ""))
        if phone:
            downloaded_phones.add(phone)
            
    try:
        with open(downloaded_file, "w") as f:
            json.dump(list(downloaded_phones), f, indent=2)
    except Exception as e:
        print(f"[CRM Gate] Warning: Could not update downloaded_phones.json: {e}")
        
    mem = io.BytesIO()
    mem.write(output.getvalue().encode('utf-8'))
    mem.seek(0)
    output.close()
    
    return send_file(
        mem, 
        as_attachment=True, 
        download_name=f"new_leads_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv", 
        mimetype="text/csv"
    )

@app.route('/api/stream')
@login_required
def stream_logs():
    """
    SSE Endpoint to stream stdout logs to the browser.
    """
    def event_stream():
        # Clear the queue first
        while not log_queue.empty():
            try:
                log_queue.get_nowait()
            except queue.Empty:
                break
                
        log_queue.put("=== CONNECTION ESTABLISHED ===")
        
        while True:
            try:
                message = log_queue.get(timeout=30)
                yield f"data: {message}\n\n"
                if message == "=== SCRAPE JOB COMPLETED ===":
                    break
            except queue.Empty:
                # Heartbeat to keep connection alive
                yield "data: [Heartbeat...]\n\n"
                
    return Response(event_stream(), mimetype="text/event-stream")

if __name__ == '__main__':
    from waitress import serve
    print("------------------------------------------------------------")
    print("         PRODUCTION SERVER RUNNING AT: http://127.0.0.1:5000")
    print("------------------------------------------------------------")
    serve(app, host='0.0.0.0', port=5000, threads=6)
