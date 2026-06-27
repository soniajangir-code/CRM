import time
import os
import re
import json
import datetime
import urllib.parse
from playwright.sync_api import sync_playwright
import sys

def safe_print(*args, **kwargs):
    sep = kwargs.get('sep', ' ')
    end = kwargs.get('end', '\n')
    file = kwargs.get('file', sys.stdout)
    flush = kwargs.get('flush', False)
    
    text = sep.join(str(arg) for arg in args)
    try:
        file.write(text + end)
    except UnicodeEncodeError:
        encoding = getattr(file, 'encoding', 'utf-8') or 'utf-8'
        try:
            safe_text = text.encode(encoding, errors='replace').decode(encoding)
        except Exception:
            safe_text = text.encode('ascii', errors='replace').decode('ascii')
        file.write(safe_text + end)
    if flush or getattr(file, 'flush', None):
        try:
            file.flush()
        except Exception:
            pass

print = safe_print

def clean_phone(phone_str):
    if not phone_str:
        return ""
    cleaned = re.sub(r"[^\d+]", "", str(phone_str))
    return cleaned

def clean_text(text):
    if not text:
        return ""
    # Remove private use area characters (often icon glyphs like \ue0c8)
    text = "".join(c for c in text if not (0xE000 <= ord(c) <= 0xF8FF))
    # Replace curly quotes and backticks with standard straight quotes
    text = text.replace("’", "'").replace("‘", "'").replace("`", "'").replace("´", "'")
    # Replace newlines with commas for address structure
    text = text.replace("\n", ", ")
    # Clean up double commas or spaces
    text = re.sub(r',\s*,', ',', text)
    # Remove leading/trailing whitespace and commas
    text = re.sub(r'^[\s,]+|[\s,]+$', '', text)
    return text.strip()

def scrape_google_maps(department, location, max_results=None):
    """
    Exhaustively scrapes Google Maps for a given department and location without hard limits.
    Saves state in gmaps_session.json to support pause/resume.
    """
    current_query = f"{department} in {location}"
    search_url = f"https://www.google.com/maps/search/{urllib.parse.quote_plus(current_query)}"
    
    session_file = "gmaps_session.json"
    session = None
    
    # 1. Try to load existing session to resume
    if os.path.exists(session_file):
        try:
            with open(session_file, "r", encoding="utf-8") as f:
                saved_session = json.load(f)
                if saved_session.get("query") == current_query and saved_session.get("pending_urls"):
                    session = saved_session
                    print(f"[GMaps Scraper] Found active session for query '{current_query}'. Resuming with {len(session['pending_urls'])} pending URLs.")
        except Exception as e:
            print(f"[GMaps Scraper] Warning: Failed to load session file: {e}")

    results = []

    with sync_playwright() as p:
        is_headless = os.environ.get("RENDER") == "true" or os.path.exists("/opt/render")
        browser = p.chromium.launch(headless=is_headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        try:
            # Phase 1: URL Gathering (only if not resuming)
            if not session:
                print(f"[GMaps Scraper] Phase 1: Navigating to search: {search_url}")
                page.goto(search_url, timeout=60000)
                
                # Check direct single place load redirect
                try:
                    page.wait_for_selector('div[role="feed"]', timeout=15000)
                except Exception:
                    if "maps/place" in page.url:
                        print("[GMaps Scraper] Redirected directly to single listing detail view.")
                        single_item = extract_details_from_page(page)
                        if single_item:
                            if not single_item.get("Phone"):
                                print("[GMaps Scraper] Single item has no phone number, skipping.")
                                return []
                            single_item["Specialization"] = department
                            single_item["Google Maps URL"] = page.url
                            from crm_gate import deduplicate_and_save
                            deduplicate_and_save([single_item])
                        return [single_item] if (single_item and single_item.get("Phone")) else []
                    else:
                        print("[GMaps Scraper] No results found or feed failed to load.")
                        return []

                print("[GMaps Scraper] Scrolling results feed exhaustively...")
                scrollable_div_selector = 'div[role="feed"]'
                place_urls = set()
                consecutive_zero_new = 0
                scroll_number = 0
                
                while True:
                    scroll_number += 1
                    before_count = len(place_urls)
                    
                    # Collect URLs currently visible on the screen
                    links = page.locator('a[href*="/maps/place/"]').all()
                    for link in links:
                        href = link.get_attribute("href")
                        if href:
                            clean_href = href.split("?")[0]
                            place_urls.add(clean_href)
                    
                    after_count = len(place_urls)
                    new_urls_added = after_count - before_count
                    
                    # Retrieve scroll information via JS
                    scroll_info = page.evaluate(
                        f"(() => {{"
                        f"  var feed = document.querySelector('{scrollable_div_selector}');"
                        f"  return feed ? {{ height: feed.scrollHeight, top: feed.scrollTop }} : {{ height: 0, top: 0 }};"
                        f"}})()"
                    )
                    feed_height = scroll_info["height"]
                    scroll_position = scroll_info["top"]
                    
                    # Detect end of list message
                    feed_text = page.locator(scrollable_div_selector).inner_text()
                    end_of_list = "reached the end of the list" in feed_text.lower() or "reached the end of the page" in feed_text.lower()
                    
                    # Print detailed scroll logs
                    print(f"Current Scroll Number: {scroll_number}")
                    print(f"Discovered URL Count: {after_count}")
                    print(f"New URLs Added This Scroll: {new_urls_added}")
                    print(f"Current Feed Height: {feed_height}")
                    print(f"Current Scroll Position: {scroll_position}")
                    print(f"End-of-list Detected: {end_of_list}")
                    print(f"----------------------------------------")
                    
                    if new_urls_added == 0:
                        consecutive_zero_new += 1
                    else:
                        consecutive_zero_new = 0
                        
                    if end_of_list:
                        print("[GMaps Scraper] Scrolling complete: End of list detected.")
                        break
                        
                    if consecutive_zero_new >= 7:
                        print("[GMaps Scraper] 7 scrolls with 0 new URLs. Running recovery bounce scroll...")
                        page.evaluate(
                            f"var feed = document.querySelector('{scrollable_div_selector}');"
                            f"if (feed) {{ feed.scrollTop = feed.scrollTop - 400; }}"
                        )
                        time.sleep(1.0)
                        page.evaluate(
                            f"var feed = document.querySelector('{scrollable_div_selector}');"
                            f"if (feed) {{ feed.scrollTop = feed.scrollHeight; }}"
                        )
                        time.sleep(2.0)
                        
                        # Collect post-recovery
                        links = page.locator('a[href*="/maps/place/"]').all()
                        for link in links:
                            href = link.get_attribute("href")
                            if href:
                                clean_href = href.split("?")[0]
                                place_urls.add(clean_href)
                                
                        if len(place_urls) == after_count:
                            print("[GMaps Scraper] Scrolling complete: No new listings after scroll recovery.")
                            break
                        else:
                            print(f"[GMaps Scraper] Recovered! Found {len(place_urls) - after_count} new listings.")
                            consecutive_zero_new = 0

                    # Focus, hover, and human-like scroll wheel actions
                    try:
                        feed_locator = page.locator(scrollable_div_selector)
                        feed_locator.focus()
                        box = feed_locator.bounding_box()
                        if box:
                            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        page.mouse.wheel(0, 1500)
                    except Exception:
                        pass
                        
                    # Backup JS scroll
                    page.evaluate(
                        f"var feed = document.querySelector('{scrollable_div_selector}');"
                        f"if (feed) {{ feed.scrollTop = feed.scrollHeight; }}"
                    )
                    time.sleep(2.0) # Wait for DOM mutation and new listings to render

                print(f"[GMaps Scraper] Scrolling complete. Total unique URLs discovered: {len(place_urls)}")
                
                session = {
                    "query": current_query,
                    "pending_urls": list(place_urls),
                    "processed_urls": [],
                    "failed_urls": [],
                    "last_index": 0,
                    "timestamp": time.time(),
                    "pending_gsheet_sync": []
                }
                with open(session_file, "w", encoding="utf-8") as f:
                    json.dump(session, f, indent=2)

            # Phase 2: Processing Queue
            pending_queue = list(session["pending_urls"])
            processed_list = list(session["processed_urls"])
            failed_list = list(session["failed_urls"])
            pending_gsheet_sync = list(session.get("pending_gsheet_sync", []))
            
            # Load config and sync pending sheets first
            config_path = "config.json"
            gsheet_url = ""
            if os.path.exists(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        gsheet_url = json.load(f).get("gsheet_url", "")
                except Exception:
                    pass
            
            if pending_gsheet_sync and gsheet_url:
                print(f"[GSheet Sync] Attempting to sync {len(pending_gsheet_sync)} pending records from previous runs...")
                from crm_gate import sync_to_gsheet
                success = sync_to_gsheet(pending_gsheet_sync, gsheet_url)
                if success:
                    pending_gsheet_sync = []
                    session["pending_gsheet_sync"] = []
            
            total_to_process = len(pending_queue) + len(processed_list) + len(failed_list)
            print(f"[GMaps Scraper] Phase 2: Processing {len(pending_queue)} URLs...")
            
            from crm_gate import deduplicate_and_save, sync_to_gsheet
            
            processed_batch_count = 0
            newly_scraped_records = []
            
            while pending_queue:
                url = pending_queue.pop(0)
                current_idx = len(processed_list) + len(failed_list) + 1
                remaining_count = len(pending_queue)
                
                # Estimate ETA (approx 5.5s per listing)
                eta_seconds = remaining_count * 5.5
                eta_str = str(datetime.timedelta(seconds=int(eta_seconds)))
                
                print(f"Processing URL {current_idx} / {total_to_process} | Remaining: {remaining_count} | Failed: {len(failed_list)} | ETA: {eta_str}")
                print(f"    -> Extracting: {url}")
                
                data = None
                try:
                    page.goto(url, timeout=30000)
                    time.sleep(2.0)
                    data = extract_details_from_page(page)
                except Exception as e:
                    print(f"    -> Error loading URL: {e}. Retrying once...")
                    try:
                        time.sleep(2.0)
                        page.goto(url, timeout=30000)
                        time.sleep(2.0)
                        data = extract_details_from_page(page)
                    except Exception as re:
                        print(f"    -> Retry failed: {re}")
                
                if data:
                    if not data.get("Phone"):
                        print("    -> Skipped (No phone number found)")
                        processed_list.append(url)
                    else:
                        if not data.get("Specialization"):
                            data["Specialization"] = department
                        data["Data Source"] = "Google Maps"
                        data["Google Maps URL"] = url
                        
                        newly_scraped_records.append(data)
                        results.append(data)
                        processed_list.append(url)
                        processed_batch_count += 1
                        print(f"    -> Extracted: {data['Name']} | Phone: {data['Phone']}")
                else:
                    print("    -> Failed to extract listing details.")
                    failed_list.append(url)
                    
                # Save progress every 5 listings
                if processed_batch_count >= 5 or not pending_queue:
                    if newly_scraped_records:
                        print(f"[GMaps Scraper] Saving progress batch of {len(newly_scraped_records)} listings...")
                        deduplicate_and_save(newly_scraped_records)
                        
                        if gsheet_url:
                            success = sync_to_gsheet(newly_scraped_records, gsheet_url)
                            if not success:
                                print("[GSheet Sync] Sync failed. Queuing records for retry...")
                                pending_gsheet_sync.extend(newly_scraped_records)
                                
                        newly_scraped_records = []
                        processed_batch_count = 0
                        
                    # Update session state
                    session["pending_urls"] = pending_queue
                    session["processed_urls"] = processed_list
                    session["failed_urls"] = failed_list
                    session["last_index"] = len(processed_list)
                    session["timestamp"] = time.time()
                    session["pending_gsheet_sync"] = pending_gsheet_sync
                    
                    with open(session_file, "w", encoding="utf-8") as f:
                        json.dump(session, f, indent=2)
                    print("[GMaps Scraper] Session progress saved.")
                    
            # Cleanup session file on complete
            if not pending_queue:
                print("[GMaps Scraper] Scraping finished successfully. Cleaning session cache.")
                if os.path.exists(session_file):
                    try:
                        os.remove(session_file)
                    except Exception:
                        pass
            
            # Print execution metrics summary
            total_collected = len(session.get("pending_urls", [])) + len(session.get("processed_urls", [])) + len(session.get("failed_urls", []))
            total_processed = len(session.get("processed_urls", [])) + len(session.get("failed_urls", []))
            
            print(f"\n[GMaps Scraper] --- Final Crawl Execution Summary ---")
            print(f"Total URLs Collected: {total_collected}")
            print(f"Total URLs Processed: {total_processed}")
            if total_collected != total_processed:
                print(f"Explanation: The collected and processed numbers differ because the scraping job "
                      f"was either stopped/paused midway, or interrupted. The remaining {total_collected - total_processed} "
                      f"URLs are saved in '{session_file}' and will be processed on the next resume run.")
            else:
                print(f"Explanation: All collected URLs have been fully processed successfully in this run.")
            print(f"---------------------------------------------------\n")
                        
        finally:
            browser.close()
            
    return results

def extract_details_from_page(page):
    """
    Extracts maximum available data fields from Google Maps detail panel.
    """
    try:
        page.wait_for_selector('h1', timeout=10000)
    except Exception:
        return None

    # 1. Business Name
    name = ""
    h1_elem = page.query_selector('h1')
    if h1_elem:
        name = h1_elem.inner_text().strip()
    if not name:
        return None

    # 2. Phone Numbers
    phone = ""
    additional_phones = []
    
    # Extract primary phone
    phone_button = page.query_selector('button[data-item-id^="phone:tel:"]')
    if phone_button:
        phone_attr = phone_button.get_attribute("data-item-id")
        phone = phone_attr.replace("phone:tel:", "").strip()
        
    if not phone:
        tel_link = page.query_selector('a[href^="tel:"]')
        if tel_link:
            phone = tel_link.get_attribute("href").replace("tel:", "").strip()
            
    if not phone:
        phone_aria = page.query_selector('button[aria-label^="Phone:"]')
        if phone_aria:
            phone = phone_aria.inner_text().strip()
            
    phone = clean_phone(phone)
    
    # Scan for any additional phone numbers in details card text
    panel_text = page.locator('div[role="main"]').inner_text()
    phone_matches = re.findall(r'(?:\+91|0)?[-\s]?[6-9]\d{9}|\b0\d{2,4}[-\s]?\d{6,8}\b', panel_text)
    
    seen_phones = {phone} if phone else set()
    for p_num in phone_matches:
        cleaned = clean_phone(p_num)
        if cleaned and len(cleaned) >= 8 and cleaned not in seen_phones:
            seen_phones.add(cleaned)
            additional_phones.append(p_num.strip())

    # 3. Address
    address = ""
    address_button = page.query_selector('button[data-item-id="address"]')
    if address_button:
        address = address_button.inner_text().strip()
    if not address:
        address_aria = page.query_selector('button[aria-label^="Address:"]')
        if address_aria:
            address = address_aria.inner_text().strip()
    address = clean_text(address)

    # 4. Email
    email = ""
    email_match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', panel_text)
    if email_match:
        email = email_match.group(0).strip()

    # 5. Website
    website = ""
    website_link = page.query_selector('a[data-item-id="authority"]')
    if website_link:
        website = website_link.get_attribute("href")
    if not website:
        website_aria = page.query_selector('a[aria-label^="Website:"]')
        if website_aria:
            website = website_aria.get_attribute("href")
    if website and "google.com" in website:
        website = ""

    # 6. Category & Specialization
    category = ""
    category_elem = page.query_selector('button[jsaction="pane.rating.category"]')
    if category_elem:
        category = category_elem.inner_text().strip()
    if not category:
        category_span = page.query_selector('span.fontBodyMedium:nth-of-type(1)')
        if category_span:
            txt = category_span.inner_text().strip()
            if txt and not any(char.isdigit() for char in txt) and len(txt) < 40:
                category = txt
    category = clean_text(category)

    # 7. Rating & Review Count
    rating = ""
    review_count = ""
    rating_container = page.query_selector('div.F7nice')
    if rating_container:
        rating_span = rating_container.query_selector('span[aria-hidden="true"]')
        if rating_span:
            rating = rating_span.inner_text().strip()
            
        rev_elems = rating_container.query_selector_all('span, button')
        for elem in rev_elems:
            txt = elem.inner_text().strip()
            if txt and txt != rating and not txt.replace(".", "").isdigit():
                rev_match = re.search(r'\(?(\d+)\)?', txt)
                if rev_match:
                    review_count = rev_match.group(1)
                    break

    # 8. Working Hours & Status
    working_hours = ""
    open_status = ""
    hours_button = page.query_selector('button[data-item-id="oh"]')
    if not hours_button:
        hours_button = page.query_selector('button[aria-label*="Hours"]')
    if not hours_button:
        hours_button = page.query_selector('div[jsaction*="pane.info.hours"] button')
        
    if hours_button:
        try:
            open_status = hours_button.inner_text().strip().split("\n")[0]
        except Exception:
            pass
        try:
            hours_button.click()
            time.sleep(1.0)
            hours_table = page.query_selector('table')
            if hours_table:
                tbl_text = hours_table.inner_text()
                if any(day in tbl_text for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]):
                    working_hours = tbl_text.replace("\n", "; ").strip()
        except Exception:
            pass

    # 9. Services & Service Options
    services = ""
    service_options_elem = page.query_selector('div[aria-label^="Service options"]')
    if service_options_elem:
        services = service_options_elem.get_attribute("aria-label").replace("Service options: ", "").strip()

    # 10. Appointment Link
    appointment_link = ""
    appointment_elem = page.query_selector('a[data-item-id="action:booking"]')
    if appointment_elem:
        appointment_link = appointment_elem.get_attribute("href")
    if not appointment_link:
        appointment_aria = page.query_selector('a[aria-label^="Appointments:"]')
        if appointment_aria:
            appointment_link = appointment_aria.get_attribute("href")

    # 11. Coordinates (Latitude & Longitude)
    latitude = ""
    longitude = ""
    coord_match = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', page.url)
    if coord_match:
        latitude = coord_match.group(1)
        longitude = coord_match.group(2)
    else:
        coord_match2 = re.search(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', page.url)
        if coord_match2:
            latitude = coord_match2.group(1)
            longitude = coord_match2.group(2)

    # 12. Plus Code
    plus_code = ""
    plus_code_btn = page.query_selector('button[data-item-id="oloc"]')
    if plus_code_btn:
        plus_code = plus_code_btn.inner_text().replace("oloc", "").strip()
    plus_code = clean_text(plus_code)

    # 13. Business Description
    description = ""
    desc_elem = page.query_selector('div.PYv55, div.fontBodyMedium[role="presentation"]')
    if desc_elem:
        description = desc_elem.inner_text().strip()
    description = clean_text(description)

    # 14. Owner Claimed
    owner_claimed = "Claimed"
    if "claim this business" in panel_text.lower() or "own this business" in panel_text.lower():
        owner_claimed = "Unclaimed"

    # 15. Hospital & Clinic Name Heuristics
    hospital_name = ""
    name_lower = name.lower()
    if any(k in name_lower for k in ["hospital", "clinic", "nursing home", "medical center", "healthcare"]):
        hospital_name = name
    elif category and any(k in category.lower() for k in ["hospital", "clinic", "medical"]):
        hospital_name = name

    # 16. Action Links (Directions & Reviews)
    directions_url = f"https://www.google.com/maps/dir//{urllib.parse.quote_plus(name)}/@{latitude},{longitude}" if latitude else ""
    reviews_url = f"{page.url.split('?')[0]}reviews"

    # 17. Photos Count
    photos_count = ""
    photos_btn = page.query_selector('button[jsaction*="pane.heroHeader.showPhotos"]')
    if photos_btn:
        photo_txt = photos_btn.inner_text()
        photo_match = re.search(r'\d+', photo_txt)
        if photo_match:
            photos_count = photo_match.group(0)
            
    if not photos_count:
        try:
            photo_elems = page.query_selector_all('button')
            for btn in photo_elems:
                txt = btn.inner_text().lower()
                if "photo" in txt:
                    photo_match = re.search(r'([\d,]+)', txt)
                    if photo_match:
                        photos_count = photo_match.group(1).replace(",", "")
                        break
        except Exception:
            pass

    return {
        "Details Received": time.strftime("%Y-%m-%d"),
        "Name": clean_text(name),
        "Father's Name": "",
        "Phone": phone,
        "Email": email,
        "Address": address,
        "Hospital": clean_text(hospital_name),
        "Specialization": category,
        "Class": "",
        "Data Source": "Google Maps",
        # Internal Rich Fields
        "Additional Phones": ", ".join(additional_phones),
        "Website": website,
        "Category": category,
        "Rating": rating,
        "Review Count": review_count,
        "Working Hours": working_hours,
        "Open / Closed Status": open_status,
        "Services": clean_text(services),
        "Appointment Link": appointment_link,
        "Google Maps URL": page.url,
        "Latitude": latitude,
        "Longitude": longitude,
        "Plus Code": plus_code,
        "Business Description": description,
        "Owner Claimed": owner_claimed,
        "Photos Count": photos_count,
        "Reviews URL": reviews_url,
        "Directions URL": directions_url
    }
