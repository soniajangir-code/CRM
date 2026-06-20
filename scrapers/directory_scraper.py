import time
import re
import urllib.parse
from playwright.sync_api import sync_playwright

def clean_phone(phone_str):
    if not phone_str:
        return ""
    cleaned = re.sub(r"[^\d+]", "", str(phone_str))
    return cleaned

def parse_justdial_page(page, department):
    """
    Parses JustDial listing cards.
    """
    results = []
    # JustDial listing card classes change frequently, so we look for common DOM structural elements:
    # 1. Look for lists (li), result boxes, or cards
    cards = page.locator('li[class*="cnt_"], div[class*="store-details"], div.result-box, div[class*="card"], div.store-box').all()
    
    # Fallback to finding headers and searching parent structures if specific card elements aren't found
    if not cards:
        # Grab all h2/h3 elements that look like business titles
        headers = page.locator('h2, h3').all()
        cards = []
        for h in headers:
            text = h.inner_text().strip()
            if text and len(text) < 100:
                # Use parent element as card
                parent = h.locator('xpath=../..')
                if parent.count() > 0:
                    cards.append(parent.first)

    print(f"[JustDial Parser] Found {len(cards)} potential card containers.")
    
    for card in cards:
        try:
            name = ""
            # Find name inside h2, h3, or elements with jcn/title class
            name_elem = card.locator('h2, h3, span[class*="jcn"], a[class*="jcn"], .store-name').first
            if name_elem.count() > 0:
                name = name_elem.inner_text().strip()
            
            if not name or len(name) > 100:
                continue
                
            # Clean name (remove ratings/votes text if it got merged)
            name = re.sub(r'\d+\.\d+\s*\d*\s*Ratings.*$', '', name).strip()
            
            phone = ""
            # Try tel link
            tel_link = card.locator('a[href^="tel:"]').first
            if tel_link.count() > 0:
                phone = tel_link.get_attribute("href").replace("tel:", "").strip()
            
            # Match standard phone regex in card text if not found in tel link
            if not phone:
                text = card.inner_text()
                phone_match = re.search(r'(?:\+91|0)?[-\s]?[6-9]\d{9}|\b0\d{2,4}[-\s]?\d{6,8}\b', text)
                if phone_match:
                    phone = phone_match.group(0)
            
            phone = clean_phone(phone)
            
            address = ""
            address_elem = card.locator('span[class*="cont_fl_addr"], .address, .cont_fl_addr').first
            if address_elem.count() > 0:
                address = address_elem.inner_text().strip()
            else:
                # Look for address-like text or location tags
                text = card.inner_text()
                addr_match = re.search(r'(?:Opp\.|Near|Behind|Floor|Road|Street|Avenue|Sector|Phase|Zone|Nagar|Colony|Naka|Building|Plot|Shop|No|Block|Chamber|Complex)[^\n]+', text, re.IGNORECASE)
                if addr_match:
                    address = addr_match.group(0).strip()
            
            # Clean address of trailing/leading punctuation
            address = re.sub(r'^[\s,]+|[\s,]+$', '', address)
            
            email = ""
            email_match = re.search(r'[\w.-]+@[\w.-]+\.\w+', card.inner_text())
            if email_match:
                email = email_match.group(0)
                
            results.append({
                "Details Received": time.strftime("%Y-%m-%d"),
                "Name": name,
                "Father's Name": "",
                "Phone": phone,
                "Email": email,
                "Address": address,
                "Hospital": name if "hospital" in name.lower() or "clinic" in name.lower() else "",
                "Specialization": department,
                "Class": "",
                "Data Source": "JustDial"
            })
        except Exception:
            continue
            
    return results

def scrape_justdial_via_google(page, department, location):
    """
    Fallback method: Scrapes JustDial search results indexing via Google.
    Bypasses Cloudflare block completely since it requests google.com instead of justdial.com.
    """
    print("[JustDial Google Fallback] Direct JustDial blocked or returned 0 results. Running Google search fallback...")
    query = f"site:justdial.com {department} in {location}"
    search_url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"
    
    results = []
    try:
        page.goto(search_url, timeout=30000)
        time.sleep(3.0)
        
        # Organic search result containers
        search_results = page.locator('div.g, div[data-ved]').all()
        print(f"[JustDial Google Fallback] Found {len(search_results)} search results on Google.")
        
        for res in search_results:
            try:
                title_elem = res.locator('h3').first
                if title_elem.count() == 0:
                    continue
                title_text = title_elem.inner_text().strip()
                
                # Check if it is actually a JustDial page
                link_elem = res.locator('a').first
                href = link_elem.get_attribute("href")
                if not href or "justdial.com" not in href:
                    continue
                    
                snippet_elem = res.locator('div[style*="webkit-line-clamp"], div.VwiC3b, span.aCOpbc').first
                snippet_text = snippet_elem.inner_text().strip() if snippet_elem.count() > 0 else ""
                
                # Parse Name from Title (e.g., "Dr. Krinita Motwani (Dentist) in Mumbai - Justdial")
                name = title_text.split("- Justdial")[0].split("| Justdial")[0].strip()
                # Clean up category/location suffix
                name = re.sub(r'\((?:Dentist|Doctor|Cardiologist|Clinic|Hospital|Specialist)\).*$', '', name, flags=re.IGNORECASE).strip()
                name = re.sub(r'in\s+[a-zA-Z\s]+$', '', name, flags=re.IGNORECASE).strip()
                
                # Extract Phone number from snippet
                phone = ""
                # Look for 10 digit numbers, mobile prefixes, or landlines
                phone_match = re.search(r'(?:\+91|0)?[-\s]?[6-9]\d{9}|\b0\d{2,4}[-\s]?\d{6,8}\b', snippet_text)
                if phone_match:
                    phone = phone_match.group(0)
                phone = clean_phone(phone)
                
                # Extract Address from snippet
                address = ""
                # Heuristic: address often follows keywords like "Address:", "Locality:", "at" or comes before "Phone"
                addr_match = re.search(r'(?:Address|Locality|At|Near):\s*([^\.]+)', snippet_text, re.IGNORECASE)
                if addr_match:
                    address = addr_match.group(1).strip()
                else:
                    # Look for road, street, layout text
                    road_match = re.search(r'([^\.]+Road[^\.]+)', snippet_text, re.IGNORECASE)
                    if road_match:
                        address = road_match.group(1).strip()
                        
                results.append({
                    "Details Received": time.strftime("%Y-%m-%d"),
                    "Name": name,
                    "Father's Name": "",
                    "Phone": phone,
                    "Email": "",
                    "Address": address if address else location,
                    "Hospital": name if "hospital" in name.lower() or "clinic" in name.lower() else "",
                    "Specialization": department,
                    "Class": "",
                    "Data Source": "JustDial"
                })
            except Exception:
                continue
    except Exception as e:
        print(f"[JustDial Google Fallback] Error running fallback search: {e}")
        
    return results

def scrape_directory(source, department, location, max_results=30):
    """
    Scrapes JustDial, Trade India, or IndiaMart for a given department and location.
    """
    query = f"{department} {location}"
    results = []
    
    # 1. Build search URL based on source
    if source == "JustDial":
        # Formulate direct category URL: https://www.justdial.com/{location}/{category}
        # e.g., if dept is "Dentist" -> "Dentists", location is "Mumbai" -> "Mumbai"
        clean_dept = department.strip().replace(" ", "-")
        if not clean_dept.lower().endswith('s') and not clean_dept.lower().endswith('y'):
            clean_dept = clean_dept + 's'
        elif clean_dept.lower().endswith('y'):
            clean_dept = clean_dept[:-1] + 'ies'
            
        clean_loc = location.strip().capitalize()
        search_url = f"https://www.justdial.com/{clean_loc}/{clean_dept}"
    elif source == "Trade India":
        search_url = f"https://www.tradeindia.com/search.html?keyword={urllib.parse.quote_plus(department)}&city={urllib.parse.quote_plus(location)}"
    elif source == "IndiaMart":
        search_url = f"https://dir.indiamart.com/search.mp?ss={urllib.parse.quote_plus(department)}&loc={urllib.parse.quote_plus(location)}"
    else:
        print(f"[Directory Scraper] Unknown source: {source}")
        return []
        
    print(f"[Directory Scraper] Navigating to {source}: {search_url}")
    print("[Directory Scraper] Note: Browser is running in HEADED mode. If you see a CAPTCHA or 'Verify you are human' screen, please complete it to let the scraper proceed.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        try:
            page.goto(search_url, timeout=60000)
            time.sleep(5.0)
            
            # Wait for user to bypass captcha if present
            if "challenge" in page.url or "cloudflare" in page.content().lower() or "blocked" in page.title().lower():
                print("[Directory Scraper] Cloudflare / CAPTCHA detected. Please solve it in the browser window...")
                # Wait up to 60 seconds
                for _ in range(30):
                    if "challenge" not in page.url and "cloudflare" not in page.content().lower() and "blocked" not in page.title().lower():
                        print("[Directory Scraper] CAPTCHA cleared!")
                        break
                    time.sleep(2.0)
            
            # Scroll down to load listings
            print("[Directory Scraper] Scrolling to load listings...")
            for _ in range(4):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(1.5)
                
            # Perform source-specific parsing
            if source == "JustDial":
                results = parse_justdial_page(page, department)
                # If direct extraction failed, use Google Search fallback
                if not results or len([r for r in results if r.get("Phone")]) == 0:
                    results = scrape_justdial_via_google(page, department, location)
            elif source == "Trade India":
                results = parse_tradeindia(page, department)
            elif source == "IndiaMart":
                results = parse_indiamart(page, department)
                
            # Filter out results where phone number is empty before returning
            results = [r for r in results if r.get("Phone")]
            results = results[:max_results]
            print(f"[Directory Scraper] Successfully extracted {len(results)} records from {source}.")
            
        except Exception as e:
            print(f"[Directory Scraper] Error scraping directory: {e}")
            # Fallback for Justdial if browser crashed/error in direct load
            if source == "JustDial":
                try:
                    results = scrape_justdial_via_google(page, department, location)
                except Exception:
                    pass
        finally:
            browser.close()
            
    return results

def parse_tradeindia(page, department):
    results = []
    cards = page.locator('div[class*="company-card"], div.company-card, div.co-card, div[class*="ProductCard"]').all()
    if not cards:
        cards = page.locator('div.card, div[class*="card"]').all()
        
    print(f"[Trade India Parser] Found {len(cards)} listing containers.")
    
    for card in cards:
        try:
            name = ""
            name_elem = card.locator('h2, h3, a[href*="/company/"], .company-name').first
            if name_elem.count() > 0:
                name = name_elem.inner_text().strip()
                
            if not name or len(name) > 100:
                continue
                
            phone = ""
            tel_link = card.locator('a[href^="tel:"]').first
            if tel_link.count() > 0:
                phone = tel_link.get_attribute("href").replace("tel:", "").strip()
            
            if not phone:
                text = card.inner_text()
                phone_match = re.search(r'(?:\+91|0)?[-\s]?[6-9]\d{9}|\b0\d{2,4}[-\s]?\d{6,8}\b', text)
                if phone_match:
                    phone = phone_match.group(0)
                    
            phone = clean_phone(phone)
            
            address = ""
            address_elem = card.locator('.location, .address, span[class*="location"]').first
            if address_elem.count() > 0:
                address = address_elem.inner_text().strip()
                
            email = ""
            email_match = re.search(r'[\w.-]+@[\w.-]+\.\w+', card.inner_text())
            if email_match:
                email = email_match.group(0)
                
            results.append({
                "Details Received": time.strftime("%Y-%m-%d"),
                "Name": name,
                "Father's Name": "",
                "Phone": phone,
                "Email": email,
                "Address": address,
                "Hospital": "",
                "Specialization": department,
                "Class": "",
                "Data Source": "Trade India"
            })
        except Exception:
            continue
            
    return results

def parse_indiamart(page, department):
    results = []
    cards = page.locator('div.lst_crd, div[class*="lst_crd"], div.m-card, div.card-listing').all()
    print(f"[IndiaMart Parser] Found {len(cards)} listing containers.")
    
    for card in cards:
        try:
            name = ""
            name_elem = card.locator('.companyname, .company-name, a[href*="indiamart.com/"]').first
            if name_elem.count() > 0:
                name = name_elem.inner_text().strip()
                
            if not name:
                continue
                
            phone = ""
            tel_link = card.locator('a[href^="tel:"]').first
            if tel_link.count() > 0:
                phone = tel_link.get_attribute("href").replace("tel:", "").strip()
                
            if not phone:
                text = card.inner_text()
                phone_match = re.search(r'(?:\+91|0)?[-\s]?[6-9]\d{9}|\b0\d{2,4}[-\s]?\d{6,8}\b', text)
                if phone_match:
                    phone = phone_match.group(0)
                    
            phone = clean_phone(phone)
            
            address = ""
            address_elem = card.locator('.city-link, .location, .address').first
            if address_elem.count() > 0:
                address = address_elem.inner_text().strip()
                
            email = ""
            email_match = re.search(r'[\w.-]+@[\w.-]+\.\w+', card.inner_text())
            if email_match:
                email = email_match.group(0)
                
            results.append({
                "Details Received": time.strftime("%Y-%m-%d"),
                "Name": name,
                "Father's Name": "",
                "Phone": phone,
                "Email": email,
                "Address": address,
                "Hospital": "",
                "Specialization": department,
                "Class": "",
                "Data Source": "IndiaMart"
            })
        except Exception:
            continue
            
    return results
