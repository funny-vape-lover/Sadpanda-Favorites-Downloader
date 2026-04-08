import requests
from bs4 import BeautifulSoup
import re
import json
import time
import os

CACHE_FILE = "exhentai_cache.json"

# Changed back to the name you already put in the UI
def fetch_exhentai_favorites_and_torrents(source_url, cookies_dict):
    session = requests.Session()
    session.cookies.update(cookies_dict)

    # --- 1. LOAD LOCAL CACHE ---
    cache = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            try:
                cache = json.load(f)
                print(f"Loaded {len(cache)} items from local cache.")
            except json.JSONDecodeError:
                print("Cache file is empty or corrupted. Starting fresh.")

    # --- 2. SCRAPE FOR NEW FAVORITES ---
    print(f"Checking for new favorites at: {source_url}")
    current_url = source_url
    new_galleries_to_fetch = []

    while current_url:
        print(f"Scraping page: {current_url}")
        fav_response = session.get(current_url)
        soup = BeautifulSoup(fav_response.text, 'html.parser')

        page_links = list(set(re.findall(r'https://exhentai\.org/g/(\d+)/([a-f0-9]+)', fav_response.text)))
        hit_known_item = False

        for gid, token in page_links:
            # Check if this gallery is already saved in our cache
            if str(gid) not in cache:
                new_galleries_to_fetch.append([int(gid), token])
            else:
                hit_known_item = True

        # Stop scraping if we hit items we already know about
        if hit_known_item:
            print("Found items already in cache. Stopping the page scrape!")
            break

        # Move to next page
        next_button = soup.find('a', id='pnext')
        if not next_button:
            next_button = soup.find('a', href=re.compile(r'\?next='))

        if next_button and next_button.get('href') and next_button['href'] != current_url:
            current_url = next_button['href']
            time.sleep(3)
        else:
            break

    # --- 3. FETCH API FOR NEW ITEMS ---
    if new_galleries_to_fetch:
        print(f"Found {len(new_galleries_to_fetch)} new galleries. Hitting API...")
        chunks = [new_galleries_to_fetch[i:i + 25] for i in range(0, len(new_galleries_to_fetch), 25)]

        for i, chunk in enumerate(chunks):
            api_payload = {
                "method": "gdata",
                "gidlist": chunk,
                "namespace": 1
            }
            api_response = requests.post("https://api.e-hentai.org/api.php", json=api_payload)

            try:
                api_data = api_response.json()
                if "gmetadata" in api_data:
                    for gallery in api_data["gmetadata"]:
                        if "error" not in gallery:
                            # Save to cache dictionary
                            cache[str(gallery["gid"])] = gallery
            except json.JSONDecodeError:
                print(f"Failed API response on batch {i+1}.")

            time.sleep(2)

        # Save the updated cache file
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=4)
        print("Local cache updated and saved.")
    else:
        print("No new galleries found. Dashboard is fully up to date!")

    # --- 4. FORMAT DATA FOR APP ---
    streamlit_items = []

    for gid_str, gallery in cache.items():
        gid = gallery.get("gid")
        token = gallery.get("token")
        title = gallery.get("title")
        filecount = gallery.get("filecount", 0)
        filesize_bytes = gallery.get("filesize", 0)

        # Added category extraction
        category = gallery.get("category", "Unknown")

        torrents = gallery.get("torrents", [])
        has_torrent = len(torrents) > 0
        torrent_label = f"{len(torrents)} torrents available" if has_torrent else "No torrents"

        best_torrent_url = ""
        best_torrent_name = ""

        if has_torrent:
            best_torrent = torrents[-1]
            torrent_hash = best_torrent.get("hash")
            best_torrent_name = best_torrent.get("name")
            best_torrent_url = f"https://e-hentai.org/torrent/{torrent_hash}/[whatever].torrent"

        item_dict = {
            "item_id": f"g-{gid}",
            "title": title,
            "category": category,
            "size_bytes": filesize_bytes,
            "has_torrent": has_torrent,
            "torrent_label": torrent_label,
            "torrent_info": best_torrent_name,
            "progress_pct": 0,
            "status": "queued",
            "item_url": f"https://exhentai.org/g/{gid}/{token}/",
            # Added Category to the Notes so you can search/filter by it in the UI
            "notes": f"{filecount} files",
            "secondary_url": best_torrent_url
        }

        streamlit_items.append(item_dict)

    return list(reversed(streamlit_items))