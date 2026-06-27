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
from flask import Flask, render_template, request, jsonify, Response, send_file, make_response
from werkzeug.utils import secure_filename

# Import local scraper engines and gateway functions
from scrapers.gmaps_scraper import scrape_google_maps
from scrapers.directory_scraper import scrape_directory
from scrapers.hospital_scraper import scrape_hospitals
from crm_gate import load_mapping_config, map_headers, deduplicate_and_save, clean_phone, sync_to_gsheet

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'input_csv'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB limit

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



def run_scraper_thread(source, department, location, gsheet_url):
    """
    Runs the scraper in a separate thread, redirects stdout to stream logs,
    and syncs the results to Google Sheet / local CSV.
    """
    original_stdout = sys.stdout
    capture = LogCapture(log_queue, original_stdout)
    sys.stdout = capture
    
    try:
        print(f"[Scraper Thread] Initiating automated search: '{department}' in '{location}' from '{source}'...")
        
        new_records = []
        if source == "Google Maps":
            new_records = scrape_google_maps(department, location)
        elif source in ["JustDial", "Trade India", "IndiaMart"]:
            new_records = scrape_directory(source, department, location)
        elif source == "Hospital Websites":
            new_records = scrape_hospitals(department, location)
        
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

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/config', methods=['GET', 'POST'])
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
def download_csv():
    filter_type = request.args.get('filter', 'all')
    master_file = "master_dataset.csv"
    
    if not os.path.exists(master_file):
        return "Master dataset CSV file does not exist yet. Run a scrape campaign or upload a CSV first.", 404
        
    crm_fields = [
        "Details Received", "Name", "Father's Name", "Phone", 
        "Email", "Address", "Hospital", "Specialization", "Class", "Data Source"
    ]
    
    rows = []
    # Read files with utf-8-sig to preserve encoding/accents
    with open(master_file, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames:
            for row in reader:
                rows.append(row)
                
    filtered_rows = []
    
    if filter_type == "today":
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        filtered_rows = [r for r in rows if r.get("Details Received") == today_str]
    elif filter_type == "new":
        # Load already exported phone numbers
        exported_phones = set()
        exported_file = "exported_phones.json"
        if os.path.exists(exported_file):
            with open(exported_file, "r") as f:
                try:
                    exported_phones = set(json.load(f))
                except Exception:
                    pass
                    
        # Filter rows
        filtered_rows = [r for r in rows if r.get("Phone") not in exported_phones]
        
        # Save newly exported phone numbers to prevent future duplicate exports
        newly_exported = [r.get("Phone") for r in filtered_rows if r.get("Phone")]
        if newly_exported:
            exported_phones.update(newly_exported)
            with open(exported_file, "w") as f:
                json.dump(list(exported_phones), f)
    else:
        filtered_rows = rows
        
    if not filtered_rows:
        return "No records match the selected filter (either no new leads or no leads scraped today).", 400
        
    # Generate CSV in memory
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=crm_fields)
    writer.writeheader()
    writer.writerows(filtered_rows)
    
    response = make_response(output.getvalue())
    filename = f"leads_{filter_type}_{datetime.datetime.now().strftime('%Y%m%d')}.csv"
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    response.headers["Content-type"] = "text/csv"
    return response

@app.route('/api/stream')
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
