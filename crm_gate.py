import os
import csv
import json
import re
import datetime
import requests
# from scrapers.gmaps_scraper import clean_phone # Wait, we can just define clean_phone locally or import it

# Define clean_phone locally to keep it robust and simple
def clean_phone(phone_str):
    if not phone_str:
        return ""
    # Remove everything except digits and leading plus
    cleaned = re.sub(r"[^\d+]", "", str(phone_str))
    # If it is a 10 digit Indian number without country code, we can optionally prepend +91 or keep it clean.
    # Let's keep it clean as extracted.
    return cleaned

def load_mapping_config():
    config_path = "mapping_config.json"
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"sources": {}}

def map_headers(row_dict, source_name, mappings, default_dept, default_loc):
    """
    Maps headers of a raw row_dict to the target CRM fields.
    """
    crm_fields = [
        "Details Received", "Name", "Father's Name", "Phone", 
        "Email", "Address", "Hospital", "Specialization", "Class", "Data Source"
    ]
    mapped_row = {field: "" for field in crm_fields}
    
    # Default metadata
    mapped_row["Details Received"] = datetime.datetime.now().strftime("%Y-%m-%d")
    mapped_row["Data Source"] = source_name
    mapped_row["Specialization"] = default_dept
    mapped_row["Address"] = default_loc
    
    source_mappings = mappings.get("sources", {}).get(source_name, {})
    
    # Normalize row keys to lowercase for flexible matching
    row_lower = {k.lower().strip(): v for k, v in row_dict.items() if k}
    
    for crm_field in crm_fields:
        # Check if we have specific mapping aliases for this CRM field
        aliases = source_mappings.get(crm_field, [])
        # Also include the lowercase version of the CRM field name itself
        aliases_lower = [a.lower() for a in aliases] + [crm_field.lower()]
        
        # Try to find a matching key in our row
        for alias in aliases_lower:
            if alias in row_lower and row_lower[alias]:
                mapped_row[crm_field] = str(row_lower[alias]).strip()
                break
                
    # Clean phone number
    mapped_row["Phone"] = clean_phone(mapped_row["Phone"])
    
    # Handle Hospital field logic
    if not mapped_row["Hospital"] and mapped_row["Name"]:
        name_lower = mapped_row["Name"].lower()
        if any(k in name_lower for k in ["hospital", "clinic", "medical center", "healthcare"]):
            mapped_row["Hospital"] = mapped_row["Name"]
            
    return mapped_row

def deduplicate_and_save(new_records):
    """
    Consolidates new records with existing master records.
    Saves the rich data to master_dataset_rich.json (Internal Master Schema)
    and exports/generates master_dataset.csv (CRM Export Schema).
    """
    rich_file = "master_dataset_rich.json"
    crm_file = "master_dataset.csv"
    
    crm_fields = [
        "Details Received", "Name", "Father's Name", "Phone", 
        "Email", "Address", "Hospital", "Specialization", "Class", "Data Source"
    ]
    
    existing_rich_records = {}
    
    # 1. Load existing records (prefer rich JSON first)
    if os.path.exists(rich_file):
        with open(rich_file, "r", encoding="utf-8") as f:
            try:
                data_list = json.load(f)
                for rec in data_list:
                    phone = clean_phone(rec.get("Phone", ""))
                    if phone:
                        existing_rich_records[phone] = rec
            except Exception as e:
                print(f"[CRM Gate] Warning: Failed to load rich JSON database: {e}")
                
    # 2. Migration: If JSON doesn't exist but CSV does, import the CSV
    if not existing_rich_records and os.path.exists(crm_file):
        print("[CRM Gate] Migrating existing master_dataset.csv leads to internal rich database...")
        with open(crm_file, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                for row in reader:
                    phone = clean_phone(row.get("Phone", ""))
                    if phone:
                        existing_rich_records[phone] = dict(row)

    print(f"[CRM Gate] Loaded {len(existing_rich_records)} existing records from master dataset.")

    added_count = 0
    updated_count = 0
    skipped_empty_phone = 0
    
    # 3. Process new records
    for rec in new_records:
        phone = clean_phone(rec.get("Phone", ""))
        if not phone:
            skipped_empty_phone += 1
            continue
            
        # Count non-empty values to measure data richness
        non_empty_count = sum(1 for v in rec.values() if v)
        
        # Ensure standard keys exist in rec
        name = rec.get("Name", rec.get("Business Name", ""))
        address = rec.get("Address", "")
        hospital = rec.get("Hospital", "")
        email = rec.get("Email", "")
        specialization = rec.get("Specialization", rec.get("Category", ""))
        data_source = rec.get("Data Source", "")
        details_received = rec.get("Details Received", datetime.datetime.now().strftime("%Y-%m-%d"))
        
        rec["Name"] = name
        rec["Address"] = address
        rec["Hospital"] = hospital
        rec["Email"] = email
        rec["Specialization"] = specialization
        rec["Phone"] = phone
        rec["Data Source"] = data_source
        rec["Details Received"] = details_received
        if "Father's Name" not in rec:
            rec["Father's Name"] = rec.get("Father's Name", "")
        if "Class" not in rec:
            rec["Class"] = rec.get("Class", "")

        if phone not in existing_rich_records:
            existing_rich_records[phone] = rec
            added_count += 1
        else:
            # Compare richness of data
            existing_rec = existing_rich_records[phone]
            existing_richness = sum(1 for v in existing_rec.values() if v)
            if non_empty_count > existing_richness:
                # Merge fields
                merged = existing_rec.copy()
                merged.update(rec)
                existing_rich_records[phone] = merged
                updated_count += 1
                
    # 4. Save consolidated rich records to master_dataset_rich.json
    final_rich_list = list(existing_rich_records.values())
    with open(rich_file, "w", encoding="utf-8") as f:
        json.dump(final_rich_list, f, indent=2)
        
    # 5. Export CRM Schema to master_dataset.csv
    final_crm_records = []
    for rec in final_rich_list:
        mapped_crm = {}
        for field in crm_fields:
            mapped_crm[field] = rec.get(field, "")
        final_crm_records.append(mapped_crm)
        
    with open(crm_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=crm_fields)
        writer.writeheader()
        writer.writerows(final_crm_records)
        
    print(f"\n[CRM Gate] Save complete:")
    print(f"  - Total records in master dataset: {len(final_rich_list)}")
    print(f"  - New unique records added: {added_count}")
    print(f"  - Existing records enriched: {updated_count}")
    print(f"  - Rows skipped due to empty phone: {skipped_empty_phone}")

def process_local_csv_files(mappings, default_dept, default_loc):
    """
    Scans input_csv/ directory, maps headers, and processes records.
    """
    input_dir = "input_csv"
    if not os.path.exists(input_dir):
        os.makedirs(input_dir)
        print(f"\n[CRM Gate] Created '{input_dir}' directory.")
        print(f"Please copy your scraper CSV files into '{input_dir}' and run this option again.")
        return
        
    csv_files = [f for f in os.listdir(input_dir) if f.lower().endswith(".csv")]
    if not csv_files:
        print(f"\n[CRM Gate] No CSV files found in '{input_dir}' folder.")
        print("Please upload/copy your CSV files there.")
        return
        
    print(f"\n[CRM Gate] Found {len(csv_files)} CSV files in '{input_dir}/'. Select mapping source:")
    sources = list(mappings.get("sources", {}).keys())
    for idx, src in enumerate(sources, 1):
        print(f"  {idx}. {src}")
    print(f"  {len(sources)+1}. Generic (Use default headers)")
    
    choice = input("\nSelect source mapping layout (number): ").strip()
    try:
        choice_idx = int(choice) - 1
        if 0 <= choice_idx < len(sources):
            selected_source = sources[choice_idx]
        else:
            selected_source = "Generic"
    except ValueError:
        selected_source = "Generic"
        
    new_records = []
    
    for file in csv_files:
        filepath = os.path.join(input_dir, file)
        print(f"[CRM Gate] Processing file: {file}")
        
        try:
            # Detect encoding (try utf-8, fallback to latin-1)
            try:
                f = open(filepath, "r", encoding="utf-8")
                reader = csv.DictReader(f)
                rows = list(reader)
                f.close()
            except UnicodeDecodeError:
                f = open(filepath, "r", encoding="latin-1")
                reader = csv.DictReader(f)
                rows = list(reader)
                f.close()
                
            for row in rows:
                mapped = map_headers(row, selected_source, mappings, default_dept, default_loc)
                # Ensure the filename is stored in Data Source if generic
                if selected_source == "Generic":
                    mapped["Data Source"] = f"CSV: {file}"
                new_records.append(mapped)
                
        except Exception as e:
            print(f"[CRM Gate] Error reading {file}: {e}")
            
    if new_records:
        deduplicate_and_save(new_records)
    else:
        print("[CRM Gate] No records were successfully read.")

def main():
    print("=" * 60)
    print("           CRM MASTER DATASET GATEWAY & SCRAPER")
    print("=" * 60)
    
    mappings = load_mapping_config()
    
    # 1. Ask user for target Department / Specialization
    department = input("Enter Target Department / Specialization (e.g. Cardiologist): ").strip()
    if not department:
        department = "General"
        
    # 2. Ask user for target Location
    location = input("Enter Target Location (e.g. Mumbai): ").strip()
    if not location:
        location = "India"
        
    print("\nSelect Data Collection Method:")
    print("  1. Auto Search & Scrape: Google Maps")
    print("  2. Auto Search & Scrape: JustDial")
    print("  3. Auto Search & Scrape: Trade India")
    print("  4. Auto Search & Scrape: IndiaMart")
    print("  5. Auto Search & Scrape: Hospital Websites (Google Discovery)")
    print("  6. Import Local CSV files (from Instant Scraper)")
    print("  7. Exit")
    
    method = input("\nEnter your choice (1-7): ").strip()
    
    new_records = []
    
    if method == "1":
        from scrapers.gmaps_scraper import scrape_google_maps
        new_records = scrape_google_maps(department, location)
    elif method == "2":
        from scrapers.directory_scraper import scrape_directory
        new_records = scrape_directory("JustDial", department, location)
    elif method == "3":
        from scrapers.directory_scraper import scrape_directory
        new_records = scrape_directory("Trade India", department, location)
    elif method == "4":
        from scrapers.directory_scraper import scrape_directory
        new_records = scrape_directory("IndiaMart", department, location)
    elif method == "5":
        from scrapers.hospital_scraper import scrape_hospitals
        new_records = scrape_hospitals(department, location)
    elif method == "6":
        process_local_csv_files(mappings, department, location)
        return
    elif method == "7":
        print("Exiting.")
        return
    else:
        print("Invalid choice.")
        return
        
    if new_records:
        deduplicate_and_save(new_records)
    else:
        print("\n[CRM Gate] No data scraped or task was interrupted.")

if __name__ == "__main__":
    # Create input directory if it doesn't exist
    if not os.path.exists("input_csv"):
        os.makedirs("input_csv")
    main()

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
