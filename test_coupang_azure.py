import sys
import os
from pathlib import Path

log_dir = Path("logs")
log_dir.mkdir(parents=True, exist_ok=True)
out_file = log_dir / "test_coupang_result.txt"

results = []
results.append("--- TESTING COUPANG DIRECT SCRAPING ON GITHUB ACTIONS ---")

try:
    from curl_cffi import requests
    from bs4 import BeautifulSoup
    
    urls = [
        ("Narangd", "https://www.coupang.com/vp/products/9245972072?itemId=18865278276"),
        ("ShinRamyun", "https://www.coupang.com/vp/products/7958974"),
        ("Alive", "https://www.coupang.com/vp/products/7870193278"),
    ]
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        "Referer": "https://www.coupang.com/",
    }
    
    for name, u in urls:
        try:
            r = requests.get(u, headers=headers, impersonate="chrome124", timeout=12)
            soup = BeautifulSoup(r.text, "html.parser")
            price_el = soup.select_one("span.total-price > strong") or soup.select_one(".prod-sale-price strong") or soup.select_one(".price-value")
            title_el = soup.select_one("h2.prod-buy-header__title") or soup.select_one(".prod-product-name")
            
            p_text = price_el.get_text().strip() if price_el else "NOT_FOUND"
            t_text = title_el.get_text().strip() if title_el else "NOT_FOUND"
            
            res = f"[{name}] STATUS: {r.status_code} | PRICE: {p_text} | TITLE: {t_text[:30]} | LEN: {len(r.text)}"
            print(res)
            results.append(res)
        except Exception as ex:
            err = f"[{name}] EXCEPTION: {ex}"
            print(err)
            results.append(err)

except Exception as e:
    err = f"GLOBAL_ERR: {e}"
    print(err)
    results.append(err)

out_file.write_text("\n".join(results), encoding="utf-8")
print(f"Saved to {out_file}")
