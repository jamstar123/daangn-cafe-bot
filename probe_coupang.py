import sys
import json
import re
import urllib.request
from curl_cffi import requests

headers_base = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

product_id = "9245972072"
item_id = "18865278276"

tests = [
    ("Desktop Chrome", f"https://www.coupang.com/vp/products/{product_id}?itemId={item_id}", {"impersonate": "chrome124"}),
    ("Desktop Safari", f"https://www.coupang.com/vp/products/{product_id}?itemId={item_id}", {"impersonate": "safari15_5"}),
    ("Mobile Web", f"https://m.coupang.com/vm/products/{product_id}?itemId={item_id}", {"impersonate": "chrome124", "headers": {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"}}),
    ("App UserAgent", f"https://www.coupang.com/vp/products/{product_id}?itemId={item_id}", {"headers": {"User-Agent": "Coupang/Android 7.5.3 (SM-S918N; Android 14; ko_KR)"}}),
    ("Coupang ShortLink Resolve", f"https://link.coupang.com/re/AFFSDP?lptag=AF2017901&subid=daangncafe&pageKey={product_id}&itemId={item_id}", {"impersonate": "chrome124"}),
    ("Google Cache", f"https://webcache.googleusercontent.com/search?q=cache:https://www.coupang.com/vp/products/{product_id}", {"impersonate": "chrome124"}),
    ("Naver Search Shopping", f"https://search.shopping.naver.com/search/all?query={product_id}", {"impersonate": "chrome124"}),
    ("Coupang Product Review/Data API", f"https://www.coupang.com/vp/products/{product_id}/summary", {"impersonate": "chrome124"}),
]

print("=== STARTING COUPANG PROBE ===")
for name, url, kwargs in tests:
    try:
        h = dict(headers_base)
        if "headers" in kwargs:
            h.update(kwargs["headers"])
        imp = kwargs.get("impersonate", None)
        
        if imp:
            r = requests.get(url, headers=h, impersonate=imp, timeout=10, follow_redirects=True)
        else:
            r = requests.get(url, headers=h, timeout=10, follow_redirects=True)
            
        print(f"[{name}] Code: {r.status_code} | Len: {len(r.text)} | FinalUrl: {r.url[:60]}")
        if r.status_code == 200:
            # Check if actual product content is returned
            price_m = re.findall(r'(\d{1,3}(?:,\d{3})+)\s*원', r.text)
            print(f"  -> Found prices: {price_m[:5]}")
            title_m = re.findall(r'<title>(.*?)</title>', r.text, re.IGNORECASE)
            print(f"  -> Title: {title_m[:1]}")
        elif r.status_code == 403:
            print(f"  -> 403 Snippet: {r.text[:150].strip()}")
    except Exception as e:
        print(f"[{name}] Error: {e}")

print("=== FINISHED COUPANG PROBE ===")
