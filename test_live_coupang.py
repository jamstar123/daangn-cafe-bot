import sys
import re
import json
import urllib.request
from bs4 import BeautifulSoup

test_urls = [
    ("Narangd_Item", "https://www.coupang.com/vp/products/9245972072?itemId=18865278276"),
    ("Narangd_Mobile", "https://m.coupang.com/vm/products/9245972072?itemId=18865278276"),
    ("Alive_Multi", "https://www.coupang.com/vp/products/7870193278"),
]

print("=== METHOD 1: curl_cffi with Chrome Impersonation ===")
try:
    from curl_cffi import requests as c_requests
    for label, url in test_urls:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.coupang.com/",
        }
        res = c_requests.get(url, headers=headers, impersonate="chrome124", timeout=15)
        print(f"[{label}] Status: {res.status_code}, Body length: {len(res.text)}")
        
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            
            # Check Title
            title_el = soup.select_one("h2.prod-buy-header__title") or soup.select_one(".prod-buy-header__title") or soup.select_one("h1.prod-buy-header__title")
            title = title_el.get_text(strip=True) if title_el else "NO_TITLE"
            
            # Check Price Selectors
            p_total = soup.select_one("span.total-price > strong")
            p_sale = soup.select_one("span.prod-sale-price strong") or soup.select_one(".prod-sale-price .price-value")
            p_coupon = soup.select_one("span.prod-coupon-price strong") or soup.select_one(".prod-coupon-price .price-value")
            p_origin = soup.select_one("span.prod-origin-price strong") or soup.select_one(".prod-origin-price")
            
            # Check JSON-LD
            ld_price = None
            for s in soup.select('script[type="application/ld+json"]'):
                try:
                    data = json.loads(s.string)
                    if isinstance(data, dict) and "offers" in data:
                        ld_price = data["offers"].get("price")
                except Exception:
                    pass
            
            print(f"  -> Title: {title[:40]}")
            print(f"  -> total-price: {p_total.get_text(strip=True) if p_total else None}")
            print(f"  -> sale-price: {p_sale.get_text(strip=True) if p_sale else None}")
            print(f"  -> coupon-price: {p_coupon.get_text(strip=True) if p_coupon else None}")
            print(f"  -> origin-price: {p_origin.get_text(strip=True) if p_origin else None}")
            print(f"  -> LD Schema Price: {ld_price}")
            
            # Look for regex prices in scripts
            prices_found = re.findall(r'"salePrice":\s*(\d+)', res.text)
            print(f"  -> regex salePrice: {prices_found[:5]}")
            
            coupon_prices = re.findall(r'"couponPrice":\s*(\d+)', res.text)
            print(f"  -> regex couponPrice: {coupon_prices[:5]}")
            
            final_prices = re.findall(r'"finalPrice":\s*(\d+)', res.text)
            print(f"  -> regex finalPrice: {final_prices[:5]}")
        else:
            print(f"  -> Snippet: {res.text[:300]}")
except Exception as e:
    print(f"curl_cffi failed: {e}")

print("\n=== METHOD 2: Standard requests (to compare) ===")
try:
    import requests
    r = requests.get("https://www.coupang.com/vp/products/9245972072?itemId=18865278276", 
                     headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=10)
    print(f"Standard requests status: {r.status_code}, len: {len(r.text)}")
except Exception as e:
    print(f"Standard requests error: {e}")
