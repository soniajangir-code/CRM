import os
import csv
import json
import re
import datetime
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
    Filters out empty phones and deduplicates based on phone number,
    keeping the record with the most complete information.
    """
    master_file = "master_dataset.csv"
    crm_fields = [
        "Details Received", "Name", "Father's Name", "Phone", 
        "Email", "Address", "Hospital", "Specialization", "Class", "Data Source"
    ]
    
    existing_records = []
    if os.path.exists(master_file):
        with open(master_file, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            # Ensure the file actually has headers and data
            if reader.fieldnames:
                for row in reader:
                    # Clean and format phone for existing rows just in case
                    row["Phone"] = clean_phone(row.get("Phone", ""))
                    existing_records.append(row)

    print(f"[CRM Gate] Loaded {len(existing_records)} existing records from master dataset.")

    # Deduplication map
    # Key: cleaned phone, Value: record dict
    dedup_map = {}
    
    # Process existing records first
    for rec in existing_records:
        phone = rec["Phone"]
        if not phone:
            continue
        dedup_map[phone] = rec
        
    # Process new records
    added_count = 0
    updated_count = 0
    skipped_empty_phone = 0
    
    for rec in new_records:
        phone = rec["Phone"]
        if not phone:
            skipped_empty_phone += 1
            continue
            
        # Count non-empty values to measure data richness
        non_empty_count = sum(1 for v in rec.values() if v)
        
        if phone not in dedup_map:
            dedup_map[phone] = rec
            added_count += 1
        else:
            # Compare richness of data
            existing_rec = dedup_map[phone]
            existing_richness = sum(1 for v in existing_rec.values() if v)
            if non_empty_count > existing_richness:
                dedup_map[phone] = rec
                updated_count += 1
                
    # Save back to CSV
    final_records = list(dedup_map.values())
    
    with open(master_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=crm_fields)
        writer.writeheader()
        writer.writerows(final_records)
        
    print(f"\n[CRM Gate] Save complete:")
    print(f"  - Total records in master dataset: {len(final_records)}")
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
