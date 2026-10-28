import sys
import os

print("--- TESTING COUPANG ON GITHUB ACTIONS ---")
try:
    from curl_cffi import requests
    from bs4 import BeautifulSoup
    
    url = "https://www.coupang.com/vp/products/9245972072?itemId=18865278276"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        "Referer": "https://www.coupang.com/",
    }
    r = requests.get(url, headers=headers, impersonate="chrome124", timeout=10)
    print(f"CURL_CFFI_STATUS: {r.status_code}")
    print(f"TEXT_LEN: {len(r.text)}")
    soup = BeautifulSoup(r.text, "html.parser")
    price = soup.select_one("span.total-price > strong")
    title = soup.select_one("h2.prod-buy-header__title")
    print(f"COUPANG_LIVE_PRICE: {price.get_text() if price else 'NONE'}")
    print(f"COUPANG_LIVE_TITLE: {title.get_text().strip() if title else 'NONE'}")
except Exception as e:
    print(f"CURL_CFFI_ERR: {e}")
