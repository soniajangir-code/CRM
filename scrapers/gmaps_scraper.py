import time
import re
import urllib.parse
from playwright.sync_api import sync_playwright

def clean_phone(phone_str):
    if not phone_str:
        return ""
    cleaned = re.sub(r"[^\d+]", "", phone_str)
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


def scrape_google_maps(department, location, max_results=30, headless=False):
    """
    Scrapes Google Maps for a given department and location.
    Returns a list of dictionaries with standard CRM fields.
    """
    query = f"{department} in {location}"
    search_url = f"https://www.google.com/maps/search/{urllib.parse.quote_plus(query)}"
    
    print(f"[GMaps Scraper] Navigating to: {search_url}")
    results = []

    with sync_playwright() as p:
        # Launch browser. Headless mode can be toggled via config.json
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        try:
            page.goto(search_url, timeout=60000)
            
            # Wait for either the results feed or "No results found"
            try:
                page.wait_for_selector('div[role="feed"]', timeout=15000)
            except Exception:
                print("[GMaps Scraper] Feed selector not found. Checking for single result direct load...")
                # Sometimes Google Maps redirects directly to a single business if there's only 1 match
                if "maps/place" in page.url:
                    print("[GMaps Scraper] Redirected directly to single listing detail view.")
                    single_item = extract_details_from_page(page)
                    if single_item:
                        single_item["Specialization"] = department
                        results.append(single_item)
                    return results
                else:
                    print("[GMaps Scraper] No results found or feed failed to load.")
                    return results

            # Scroll results pane to load items
            print("[GMaps Scraper] Scrolling results feed...")
            scrollable_div_selector = 'div[role="feed"]'
            
            # We'll scroll multiple times to load items
            last_height = 0
            scroll_attempts = 0
            max_scroll_attempts = 25 # Safeguard
            
            place_urls = set()
            
            while scroll_attempts < max_scroll_attempts and len(place_urls) < max_results:
                # Get place links currently visible
                links = page.locator('a[href*="/maps/place/"]').all()
                for link in links:
                    href = link.get_attribute("href")
                    if href:
                        # Normalize URL to keep it simple
                        clean_href = href.split("?")[0]
                        place_urls.add(clean_href)
                
                if len(place_urls) >= max_results:
                    break
                
                # Scroll the feed element down
                page.evaluate(
                    f"document.querySelector('{scrollable_div_selector}').scrollBy(0, 1000);"
                )
                time.sleep(1.5)
                
                # Check scroll height to see if we reached the end
                new_height = page.evaluate(
                    f"document.querySelector('{scrollable_div_selector}').scrollHeight"
                )
                if new_height == last_height:
                    # Let's try one more time with a longer wait
                    time.sleep(2.0)
                    new_height = page.evaluate(
                        f"document.querySelector('{scrollable_div_selector}').scrollHeight"
                    )
                    if new_height == last_height:
                        print("[GMaps Scraper] Reached the end of the scroll feed.")
                        break
                
                last_height = new_height
                scroll_attempts += 1

            # Limit the place URLs to the requested maximum
            target_urls = list(place_urls)[:max_results]
            print(f"[GMaps Scraper] Found {len(place_urls)} URLs. Processing top {len(target_urls)}...")

            # Visit each place page to extract detail view
            for i, url in enumerate(target_urls, 1):
                print(f"[GMaps Scraper] [{i}/{len(target_urls)}] Extracting from: {url}")
                try:
                    page.goto(url, timeout=30000)
                    time.sleep(2.0) # Wait for page to render detail pane
                    
                    data = extract_details_from_page(page)
                    if data:
                        if not data.get("Phone"):
                            print("    -> Skipped (No phone number found)")
                            continue
                        # Apply query values if not scraped
                        if not data.get("Specialization"):
                            data["Specialization"] = department
                        # Append source
                        data["Data Source"] = "Google Maps"
                        results.append(data)
                        print(f"    -> Extracted: {data['Name']} | Phone: {data['Phone']}")
                except Exception as e:
                    print(f"    -> Error loading listing {url}: {e}")
                    
        finally:
            browser.close()
            
    return results

def extract_details_from_page(page):
    """
    Extracts business details from an opened Google Maps place page.
    """
    # Wait for the name header to load
    try:
        page.wait_for_selector('h1', timeout=10000)
    except Exception:
        print("    -> Header 'h1' not found, skipping listing.")
        return None

    # 1. Business Name (h1 element)
    name = ""
    h1_elem = page.query_selector('h1')
    if h1_elem:
        name = h1_elem.inner_text().strip()
    
    if not name:
        return None

    # 2. Phone Number
    phone = ""
    # Try data-item-id first
    phone_button = page.query_selector('button[data-item-id^="phone:tel:"]')
    if phone_button:
        phone_attr = phone_button.get_attribute("data-item-id")
        # Example data-item-id: "phone:tel:+919876543210"
        phone = phone_attr.replace("phone:tel:", "").strip()
    
    # Fallback 1: Look for a link starting with tel:
    if not phone:
        tel_link = page.query_selector('a[href^="tel:"]')
        if tel_link:
            phone_href = tel_link.get_attribute("href")
            phone = phone_href.replace("tel:", "").strip()
            
    # Fallback 2: Look for button with aria-label beginning with "Phone:"
    if not phone:
        phone_aria = page.query_selector('button[aria-label^="Phone:"]')
        if phone_aria:
            phone = phone_aria.inner_text().strip()
            
    # Clean phone number
    phone = clean_phone(phone)

    # 3. Address
    address = ""
    address_button = page.query_selector('button[data-item-id="address"]')
    if address_button:
        address = address_button.inner_text().strip()
    
    # Fallback: Look for button with aria-label starting with "Address:"
    if not address:
        address_aria = page.query_selector('button[aria-label^="Address:"]')
        if address_aria:
            address = address_aria.inner_text().strip()

    # 4. Email (Not usually on Google Maps details, but check text in case)
    email = ""
    # Google Maps detail view doesn't directly show emails, but we can search for it in website url if needed,
    # or leave it empty for Google Maps.

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
        # Sometimes it points to google redirects
        website = ""

    # 6. Specialization (Category)
    specialization = ""
    # The category is usually a button immediately under the review rating/stars, or has fontBodyMedium class
    category_elem = page.query_selector('button[jsaction="pane.rating.category"]')
    if category_elem:
        specialization = category_elem.inner_text().strip()
    
    # Fallback: Let's check for any text next to stars/reviews that represents business type
    if not specialization:
        # Check standard selectors for category
        specialization_elem = page.query_selector('span.fontBodyMedium:nth-of-type(1)')
        if specialization_elem:
            text = specialization_elem.inner_text().strip()
            # Category text usually doesn't have digits/reviews count
            if text and not any(char.isdigit() for char in text) and len(text) < 40:
                specialization = text

    # 7. Hospital / Clinic
    # If the category contains "hospital", "clinic", "medical center", we use the business name as Hospital.
    # Otherwise, we default to the business name if it looks like a hospital/clinic.
    hospital = ""
    if name:
        name_lower = name.lower()
        if "hospital" in name_lower or "clinic" in name_lower or "nursing home" in name_lower or "medical center" in name_lower or "healthcare" in name_lower:
            hospital = name
        elif specialization and any(k in specialization.lower() for k in ["hospital", "clinic", "doctor", "dentist", "medical"]):
            hospital = name

    return {
        "Details Received": time.strftime("%Y-%m-%d"),
        "Name": clean_text(name),
        "Father's Name": "", # Scraped business details don't have father's name
        "Phone": phone,
        "Email": clean_text(email),
        "Address": clean_text(address),
        "Hospital": clean_text(hospital),
        "Specialization": clean_text(specialization),
        "Class": "",
        "Data Source": "Google Maps"
    }
