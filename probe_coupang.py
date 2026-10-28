import sys
import json
import re
from curl_cffi import requests

headers_base = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

product_id = "9245972072"
item_id = "18865278276"

tests = [
    ("Desktop Chrome", f"https://www.coupang.com/vp/products/{product_id}?itemId={item_id}", {"impersonate": "chrome124"}),
    ("Desktop Chrome Impersonate 120", f"https://www.coupang.com/vp/products/{product_id}?itemId={item_id}", {"impersonate": "chrome120"}),
    ("Mobile Web Safari", f"https://m.coupang.com/vm/products/{product_id}?itemId={item_id}", {"impersonate": "safari15_5", "headers": {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"}}),
    ("App UserAgent", f"https://www.coupang.com/vp/products/{product_id}?itemId={item_id}", {"impersonate": "chrome124", "headers": {"User-Agent": "Coupang/Android 7.5.3 (SM-S918N; Android 14; ko_KR)"}}),
    ("Coupang ShortLink Resolve", f"https://link.coupang.com/re/AFFSDP?lptag=AF2017901&subid=daangncafe&pageKey={product_id}&itemId={item_id}", {"impersonate": "chrome124"}),
    ("Naver Search by Coupang ID", f"https://search.shopping.naver.com/search/all?query={product_id}", {"impersonate": "chrome124"}),
    ("Naver Search by Product Name", "https://search.shopping.naver.com/search/all?query=%EB%82%98%EB%9E%91%EB%93%9C%EC%82%AC%EC%9D%B4%EB%8B%A4%20%EC%A0%9C%EB%A1%9C%20%EA%B7%B8%EB%A6%B0%EC%95%A0%ED%94%8C%20500ml%2020", {"impersonate": "chrome124"}),
    ("Daum Search by Coupang ID", f"https://search.daum.net/search?w=tot&q={product_id}", {"impersonate": "chrome124"}),
]

print("=== STARTING COUPANG PROBE ===", flush=True)
for name, url, kwargs in tests:
    try:
        h = dict(headers_base)
        if "headers" in kwargs:
            h.update(kwargs["headers"])
        imp = kwargs.get("impersonate", None)
        
        r = requests.get(url, headers=h, impersonate=imp, timeout=12, allow_redirects=True)
            
        print(f"[{name}] Code: {r.status_code} | Len: {len(r.text)} | FinalUrl: {r.url[:60]}", flush=True)
        if r.status_code == 200:
            prices = re.findall(r'(\d{1,3}(?:,\d{3})+)\s*원', r.text)
            print(f"  -> Found prices: {prices[:5]}", flush=True)
            titles = re.findall(r'<title>(.*?)</title>', r.text, re.IGNORECASE)
            print(f"  -> Title: {titles[:1]}", flush=True)
        elif r.status_code == 403:
            print(f"  -> 403 Snippet: {r.text[:120].strip()}", flush=True)
    except Exception as e:
        print(f"[{name}] Error: {e}", flush=True)

print("=== FINISHED COUPANG PROBE ===", flush=True)
sys.exit(0)
