import time
import re
import urllib.parse
from playwright.sync_api import sync_playwright

def clean_phone(phone_str):
    if not phone_str:
        return ""
    cleaned = re.sub(r"[^\d+]", "", phone_str)
    return cleaned

def extract_phones(text):
    """
    Extracts phone numbers from text using common formats.
    """
    # Matches:
    # 1. Indian mobiles: +91 9999999999, 9999999999, 09999999999
    # 2. Landlines: 022-26272829, 011 2627 2829
    # 3. General international numbers
    indian_mobile = re.findall(r'(?:\+91|0)?[-\s]?[6-9]\d{9}', text)
    landline = re.findall(r'\b0\d{2,4}[-\s]?\d{6,8}\b', text)
    general = re.findall(r'\+?\d{1,4}[-\s]?\(?\d{1,3}\)?([-\s]?\d{2,4}){2,4}', text)
    
    all_phones = []
    # Process Indian Mobiles
    for p in indian_mobile:
        cleaned = clean_phone(p)
        if len(cleaned) >= 10:
            all_phones.append(p.strip())
            
    # Process Landlines
    for p in landline:
        cleaned = clean_phone(p)
        if len(cleaned) >= 8:
            all_phones.append(p.strip())
            
    # Deduplicate and return
    seen = set()
    unique_phones = []
    for p in all_phones:
        cleaned = clean_phone(p)
        if cleaned not in seen:
            seen.add(cleaned)
            unique_phones.append(p)
            
    return unique_phones

def extract_emails(text):
    """
    Extracts emails from text.
    """
    emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
    return list(set(emails))

def scrape_hospitals(department, location, max_results=10):
    """
    Discovers hospital websites in the location via Google Search and crawls their contact info.
    """
    query = f"{department} hospitals in {location}"
    search_url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"
    
    print(f"[Hospital Scraper] Searching Google for websites: {query}")
    results = []

    # Social networks and business directories to ignore
    ignored_domains = [
        "google.com", "google.co.in", "wikipedia.org", "justdial.com", 
        "facebook.com", "linkedin.com", "instagram.com", "twitter.com", 
        "indiamart.com", "tradeindia.com", "practo.com", "lybrate.com", 
        "youtube.com", "tripadvisor", "yelp.com", "mapsofindia.com", 
        "sulekha.com", "justdial", "indiamart", "tradeindia"
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        try:
            page.goto(search_url, timeout=60000)
            time.sleep(3.0)
            
            # Wait for search results
            try:
                page.wait_for_selector('a[href]', timeout=15000)
            except Exception:
                print("[Hospital Scraper] Search results did not load. Checking for blocking...")
                return []
                
            # Extract organic search result links
            links = page.locator('a[href]').all()
            hospital_urls = []
            
            for link in links:
                href = link.get_attribute("href")
                if href and href.startswith("http") and not any(domain in href for domain in ignored_domains):
                    # Clean the URL to get the homepage domain
                    parsed_url = urllib.parse.urlparse(href)
                    domain_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
                    if domain_url not in hospital_urls:
                        hospital_urls.append(domain_url)
                        
            # Limit the number of websites to crawl
            target_urls = hospital_urls[:max_results]
            print(f"[Hospital Scraper] Discovered {len(target_urls)} hospital websites to crawl.")
            
            for i, url in enumerate(target_urls, 1):
                print(f"[Hospital Scraper] [{i}/{len(target_urls)}] Crawling: {url}")
                try:
                    # 1. Load Homepage
                    page.goto(url, timeout=20000, wait_until="domcontentloaded")
                    time.sleep(2.0)
                    
                    # Extract Hospital Name
                    hospital_name = page.title()
                    # Clean title (e.g. remove "Home - ", "| Best Hospital", etc.)
                    hospital_name = re.sub(r'Home\s*-\s*|Welcome\s*-\s*|\|\s*.*$', '', hospital_name).strip()
                    if not hospital_name:
                        # Fallback to domain name
                        hospital_name = urllib.parse.urlparse(url).netloc.replace("www.", "")
                        
                    homepage_text = page.locator('body').inner_text()
                    
                    phones = extract_phones(homepage_text)
                    emails = extract_emails(homepage_text)
                    
                    contact_url = None
                    # 2. Look for Contact Page link
                    contact_links = page.locator('a[href]').all()
                    for c_link in contact_links:
                        c_href = c_link.get_attribute("href")
                        c_text = c_link.inner_text().lower()
                        if c_href and any(kw in c_text or kw in c_href.lower() for kw in ["contact", "about", "reach-us", "reach_us"]):
                            if c_href.startswith("http"):
                                contact_url = c_href
                            else:
                                contact_url = urllib.parse.urljoin(url, c_href)
                            break
                            
                    # 3. Load Contact Page if found and we need more info
                    if contact_url and contact_url != url:
                        print(f"    -> Loading contact page: {contact_url}")
                        page.goto(contact_url, timeout=20000, wait_until="domcontentloaded")
                        time.sleep(2.0)
                        contact_text = page.locator('body').inner_text()
                        
                        # Add newly found phones/emails
                        phones.extend(extract_phones(contact_text))
                        emails.extend(extract_emails(contact_text))
                        
                    # Clean lists and deduplicate
                    unique_phones = []
                    seen_phones = set()
                    for p_num in phones:
                        cleaned = clean_phone(p_num)
                        if cleaned and cleaned not in seen_phones:
                            seen_phones.add(cleaned)
                            unique_phones.append(p_num)
                            
                    unique_emails = list(set(emails))
                    
                    if unique_phones:
                        # Add a record for each phone number found (or consolidate them)
                        # We will add one record with primary phone and one with secondary if multiple, 
                        # or join them with a comma. Let's create a record for the primary phone
                        primary_phone = unique_phones[0]
                        primary_email = unique_emails[0] if unique_emails else ""
                        
                        results.append({
                            "Details Received": time.strftime("%Y-%m-%d"),
                            "Name": hospital_name,
                            "Father's Name": "",
                            "Phone": clean_phone(primary_phone),
                            "Email": primary_email,
                            "Address": url, # Website acts as address/source if address is complex to extract
                            "Hospital": hospital_name,
                            "Specialization": department,
                            "Class": "",
                            "Data Source": "Hospital Websites"
                        })
                        print(f"    -> Success: {hospital_name} | Phone: {primary_phone} | Email: {primary_email}")
                    else:
                        print("    -> No phone numbers found.")
                        
                except Exception as e:
                    print(f"    -> Error crawling {url}: {e}")
                    
        finally:
            browser.close()
            
    return results
