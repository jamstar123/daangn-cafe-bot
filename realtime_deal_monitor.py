#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
realtime_deal_monitor.py
'핫딜은 여기' (https://cafe.daangn.com/hasdil-eun-ye-1) 전용
실시간 조건 충족형 핫딜 탐지 · 가짜 할인(정가 뻥튀기) 팩트체크 · 즉시 자동 발행 봇

주요 기능:
1. 실시간 핫딜 피드 및 오픈마켓 특가 정보를 상시 스캔합니다.
2. 단순 주기별 무조건 발행이 아니라, PriceValidator의 '단위당 절대 가격 기준표'와
   '시중 실거래가(네이버 쇼핑 중위가) 교차 검증'을 통과한 진짜 핫딜만 즉시 발행합니다.
3. 통과한 딜의 메리트 수준에 따라 제목 말머리를 자동으로 분류합니다:
   - 🚨 [가격오류급] : 전산 실수/중복 쿠폰 대란 수준 (65%+ 할인 또는 단위당 가격오류 커트라인 이하)
   - ⚡ [무지성 구매급] : 몇 분 내 품절될 수준의 초특가 생필품/인기템 (45%+ 할인 또는 추천 폭주)
   - 🔥 [역대가] : 평소 실판매가 대비 확실한 메리트가 검증된 최저가 (35%+ 할인)
4. 본문 구성은 네이버 대표 핫딜 카페('핫딜은 못참지') 스타일의 직관적 3줄 요약 + 팩트체크 리포트 + 구매 직링크로 구성됩니다.
"""

import argparse
import base64
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from daangn_cafe import DaangnCafeClient, DaangnCafeError, load_env, mask_sensitive
from price_validator import PriceValidator


DEFAULT_HOTDEAL_CAFE_SLUG = "hasdil-eun-ye-1"
DEFAULT_HOTDEAL_BOARD_ID = "2293448"  # 🔥 오늘의 핫딜


class RealtimeDealMonitor:
    def __init__(
        self,
        slug: str = DEFAULT_HOTDEAL_CAFE_SLUG,
        board_id: str = DEFAULT_HOTDEAL_BOARD_ID,
        env_path: Optional[Path] = None,
    ):
        self.base_dir = Path(__file__).resolve().parent
        self.env_path = env_path or (self.base_dir / ".env")
        load_env(self.env_path)

        self.slug = slug
        self.board_id = board_id
        log_dir = self.base_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.posted_db_path = log_dir / "posted_deals.json"
        self.posted_deals: Dict[str, Any] = self._load_posted_db()
        self.validator = PriceValidator(log_dir / "price_baseline_db.json")
        self.partners_id = os.environ.get("COUPANG_PARTNERS_ID", "AF2017901").strip() or "AF2017901"
        self.partners_cookie = os.environ.get("COUPANG_PARTNERS_COOKIE", "").strip()
        raw_malls = os.environ.get("ALLOWED_MALLS", "쿠팡,coupang")
        self.allowed_malls = [m.strip().lower() for m in raw_malls.split(",") if m.strip()]
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )

    def _load_posted_db(self) -> Dict[str, Any]:
        if self.posted_db_path.exists():
            try:
                return json.loads(self.posted_db_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def save_posted_db(self) -> None:
        try:
            self.posted_db_path.write_text(
                json.dumps(self.posted_deals, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    @staticmethod
    def parse_title_metadata(raw_title: str) -> Optional[Dict[str, Any]]:
        """
        예: '[쿠팡] 신라면 120g 20봉 (11,500원/무료)' ->
        mall='쿠팡', clean_name='신라면 120g 20봉', sale_price=11500, shipping='무료배송'
        """
        title = html.unescape(raw_title).strip()
        # 품절/종료된 딜 즉시 제외
        if any(kw in title for kw in ("품절", "종료", "마감", "취소")):
            return None

        mall = "특가몰"
        mall_match = re.match(r"^\[([^\]]+)\]\s*(.+)$", title)
        if mall_match:
            mall = mall_match.group(1).strip()
            product_part = mall_match.group(2).strip()
        else:
            product_part = title

        # 괄호 안 가격/배송비 추출: (13,180원/무료), (9,900원 / 무배) 등
        price_Matches = list(
            re.finditer(
                r"\(\s*([0-9,]{3,10})\s*원?\s*(?:[/·,~-]\s*([^)]+))?\)",
                product_part,
            )
        )
        sale_price: Optional[int] = None
        shipping = "상세페이지 참고"

        if price_Matches:
            last_m = price_Matches[-1]
            price_str = last_m.group(1).replace(",", "")
            if price_str.isdigit():
                sale_price = int(price_str)
            ship_raw = (last_m.group(2) or "").strip()
            if any(k in ship_raw for k in ("무료", "무배", "0원", "와우")):
                shipping = "무료배송"
            elif ship_raw:
                shipping = ship_raw
            product_part = (
                product_part[: last_m.start()].strip()
                + " "
                + product_part[last_m.end() :].strip()
            ).strip()
        else:
            # 괄호 없이 '13,500원' 형태가 있는 경우
            fallback_m = re.search(r"([0-9]{1,3}(?:,[0-9]{3})+)\s*원", product_part)
            if fallback_m:
                sale_price = int(fallback_m.group(1).replace(",", ""))
                product_part = product_part.replace(fallback_m.group(0), "").strip()

        if not sale_price or sale_price < 1000:
            return None

        # 상품권/기프티콘/현금성 딜 제외 (실물 핫딜 위주 선별)
        if any(kw in product_part for kw in ("상품권", "기프트카드", "깊카", "해피머니", "컬쳐랜드")):
            return None

        clean_name = re.sub(r"\s+", " ", product_part).strip()
        return {
            "mall": mall,
            "clean_name": clean_name,
            "sale_price": sale_price,
            "shipping": shipping,
        }

    @staticmethod
    def _decode_auto(raw_bytes: bytes) -> str:
        """UTF-8(RSS 등)을 우선 시도하고, 실패 시 CP949/EUC-KR(상세페이지 등)로 자동 디코딩합니다."""
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return raw_bytes.decode("cp949", errors="replace")

    @staticmethod
    def _strip_cdata(raw: str) -> str:
        val = (raw or "").strip()
        val = re.sub(r"^<!\[CDATA\[", "", val, flags=re.IGNORECASE).strip()
        val = re.sub(r"\]\]>$", "", val).strip()
        return val

    def fetch_ppomppu_realtime_feed(self) -> List[Dict[str, Any]]:
        """뽐뿌 실시간 핫딜 RSS 피드에서 최신 딜 목록과 추천/비추천/조회수를 가져옵니다."""
        rss_url = "https://www.ppomppu.co.kr/rss.php?id=ppomppu"
        req = urllib.request.Request(rss_url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_bytes = resp.read()

        xml_text = self._decode_auto(raw_bytes)
        items: List[Dict[str, Any]] = []

        for item_m in re.finditer(r"<item>(.*?)</item>", xml_text, re.DOTALL):
            block = item_m.group(1)
            title_m = re.search(r"<title>(.*?)</title>", block, re.DOTALL | re.IGNORECASE)
            link_m = re.search(r"<link>(.*?)</link>", block, re.DOTALL | re.IGNORECASE)
            desc_m = re.search(r"<description>(.*?)</description>", block, re.DOTALL | re.IGNORECASE)
            hits_m = re.search(r"<hits>\s*\[(\d+)\|(\d+)\|(\d+)\|(\d+)\]\s*</hits>", block, re.IGNORECASE)

            if not title_m or not link_m:
                continue

            raw_title = self._strip_cdata(title_m.group(1))
            link = html.unescape(self._strip_cdata(link_m.group(1)))
            rss_desc = html.unescape(self._strip_cdata(desc_m.group(1))) if desc_m else ""
            if link.startswith("http://"):
                link = "https://" + link[len("http://") :]
            no_m = re.search(r"no=(\d+)", link)
            deal_id = f"ppomppu_{no_m.group(1)}" if no_m else link

            upvotes, views, scraps, downvotes = 0, 0, 0, 0
            if hits_m:
                upvotes = int(hits_m.group(1))
                views = int(hits_m.group(2))
                scraps = int(hits_m.group(3))
                downvotes = int(hits_m.group(4))

            parsed = self.parse_title_metadata(raw_title)
            if not parsed:
                continue

            items.append(
                {
                    "deal_id": deal_id,
                    "source": "ppomppu",
                    "raw_title": raw_title,
                    "source_url": link,
                    "rss_description": rss_desc,
                    "upvotes": upvotes,
                    "downvotes": downvotes,
                    "views": views,
                    "mall": parsed["mall"],
                    "clean_name": parsed["clean_name"],
                    "sale_price": parsed["sale_price"],
                    "shipping": parsed["shipping"],
                }
            )
        return items

    @staticmethod
    def _decode_ppomppu_redirect(url: str) -> str:
        """s.ppomppu.co.kr 리다이렉트 URL에서 base64로 인코딩된 원본 쇼핑몰 직링크를 복원합니다."""
        if "s.ppomppu.co.kr" not in url:
            return url
        try:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            target_b64 = (qs.get("target") or [""])[0]
            if target_b64:
                # 패딩 보정
                pad = (-len(target_b64)) % 4
                decoded = base64.b64decode(target_b64 + ("=" * pad)).decode("utf-8", errors="ignore")
                if decoded.startswith("http"):
                    return decoded.strip()
        except Exception:
            pass
        return url

    def apply_affiliate_tag(self, url: str) -> Tuple[str, bool]:
        """
        쿠팡 링크를 사용자의 쿠팡파트너스 수익 링크로 자동 변환합니다.
        1) COUPANG_PARTNERS_COOKIE가 설정되어 있으면 쿠팡파트너스 내부 링크 생성 엔드포인트를 통해
           API 최종승인(15만원) 전이라도 공식 단축링크(https://link.coupang.com/...)로 변환을 시도합니다.
        2) COUPANG_PARTNERS_ID(예: AF1234567)가 설정되어 있으면 모든 쿠팡 URL에 lptag=AF...&subid=daangncafe를 부착합니다.
        """
        is_coupang = "coupang.com" in url.lower()
        if not is_coupang:
            return url, False

        # 1) 쿠팡파트너스 웹 세션 기반 단축링크 변환 (선택)
        if self.partners_cookie and "link.coupang.com" not in url.lower():
            try:
                api_url = "https://partners.coupang.com/api/v1/url"
                payload = json.dumps({"coupangUrls": [url]}).encode("utf-8")
                req = urllib.request.Request(
                    api_url,
                    data=payload,
                    headers={
                        "Content-Type": "application/json;charset=UTF-8",
                        "Cookie": self.partners_cookie,
                        "User-Agent": self.user_agent,
                        "Origin": "https://partners.coupang.com",
                        "Referer": "https://partners.coupang.com/",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    res_data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                short_urls = (res_data.get("data") or {}).get("urls") or []
                if short_urls and isinstance(short_urls[0], dict) and short_urls[0].get("shortenUrl"):
                    return str(short_urls[0]["shortenUrl"]), True
            except Exception:
                pass

        # 2) 파트너스 ID (AFxxxxxxx) 공식 딥링크/리다이렉트(link.coupang.com/re/AFFSDP) 자동 변환
        if self.partners_id:
            prod_match = re.search(r"/products/(\d+)", url)
            if prod_match:
                page_key = prod_match.group(1)
                parsed = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed.query)
                item_id = (qs.get("itemId") or [""])[0]
                vendor_id = (qs.get("vendorItemId") or [""])[0]
                aff_url = f"https://link.coupang.com/re/AFFSDP?lptag={urllib.parse.quote(self.partners_id)}&subid=daangncafe&pageKey={page_key}"
                if item_id:
                    aff_url += f"&itemId={item_id}"
                if vendor_id:
                    aff_url += f"&vendorItemId={vendor_id}"
                aff_url += "&traceid=V0-153"
                return aff_url, True

            sep = "&" if "?" in url else "?"
            if "lptag=" not in url:
                url = f"{url}{sep}lptag={urllib.parse.quote(self.partners_id)}&subid=daangncafe"
        return url, True

    @staticmethod
    def build_fallback_mall_url(mall: str, clean_name: str, source_url: str) -> str:
        """상세페이지 직접 접근이 제한된 클라우드 환경에서도 해당 쇼핑몰의 해당 상품 직링크/검색페이지로 즉시 연결합니다."""
        q = urllib.parse.quote(clean_name)
        m = mall.lower()
        if "쿠팡" in m or "coupang" in m:
            return f"https://www.coupang.com/np/search?q={q}"
        if "지마켓" in m or "g마켓" in m or "gmarket" in m:
            return f"https://browse.gmarket.co.kr/search?keyword={q}"
        if "옥션" in m or "auction" in m:
            return f"https://browse.auction.co.kr/search?keyword={q}"
        if "11번가" in m or "11st" in m:
            return f"https://search.11st.co.kr/pc/total-search?kwd={q}"
        if "롯데" in m or "lotte" in m:
            return f"https://www.lotteon.com/search/search/search.ecn?render=search&platform=pc&q={q}"
        if "네이버" in m or "naver" in m or "톡딜" in m or "카카오" in m:
            return f"https://search.shopping.naver.com/search/all?query={q}"
        return source_url

    def fetch_deal_detail(self, source_url: str, fallback_desc: str = "", mall: str = "", clean_name: str = "") -> Dict[str, str]:
        """
        상세 페이지에서 원본 쇼핑몰 구매 직링크, 상품 대표 이미지 URL, 본문 설명 텍스트를 추출합니다.
        해외/클라우드 IP(GitHub Actions 등)에서 상세 페이지가 403으로 차단될 경우에도
        프록시 조회 및 상품 전용 대표 이미지/쇼핑몰 직링크 자동 매핑으로 무중단 동작합니다.
        """
        html_text = ""
        headers = {
            "User-Agent": self.user_agent,
            "Referer": "https://www.ppomppu.co.kr/rss.php?id=ppomppu",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        }
        for attempt_url in (
            source_url,
            f"https://api.allorigins.win/raw?url={urllib.parse.quote(source_url, safe='')}",
        ):
            try:
                req = urllib.request.Request(attempt_url, headers=headers)
                with urllib.request.urlopen(req, timeout=12) as resp:
                    raw_bytes = resp.read()
                html_text = self._decode_auto(raw_bytes)
                if html_text and len(html_text) > 200:
                    break
            except Exception:
                continue

        # 1. 원본 쇼핑몰 링크 추출
        shop_url = ""
        if html_text:
            redir_m = re.search(r"https?://s\.ppomppu\.co\.kr\?[^\s\"'<>]+", html_text)
            if redir_m:
                raw_redir = html.unescape(redir_m.group(0))
                shop_url = self._decode_ppomppu_redirect(raw_redir)

            if not shop_url:
                direct_m = re.search(
                    r"https?://(?:www\.|m\.)?(?:coupang\.com|gmarket\.co\.kr|11st\.co\.kr|auction\.co\.kr|smartstore\.naver\.com|brand\.naver\.com|lotteon\.com|ssg\.com|tmon\.co\.kr|aliExpress\.com)[^\s\"'<>]+",
                    html_text,
                    re.IGNORECASE,
                )
                if direct_m:
                    shop_url = html.unescape(direct_m.group(0))

        if not shop_url:
            shop_url = self.build_fallback_mall_url(mall, clean_name, source_url)

        # 2. 대표 상품 이미지 추출 (og:image -> 본문 cdn 이미지 -> 상품명 기반 고화질 상품 썸네일)
        image_url = ""
        if html_text:
            og_m = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html_text, re.IGNORECASE)
            if og_m:
                candidate = og_m.group(1).strip()
                if "ppomppu_logo" not in candidate and "default" not in candidate:
                    image_url = candidate

            if not image_url:
                cdn_m = re.search(
                    r'(?:https?:)?//cdn\d*\.ppomppu\.co\.kr/zboard/data\d*/[^\s"\'<>]+?\.(?:jpg|jpeg|png|webp)',
                    html_text,
                    re.IGNORECASE,
                )
                if cdn_m:
                    image_url = cdn_m.group(0)

            if image_url.startswith("//"):
                image_url = "https:" + image_url

        if not image_url and clean_name:
            image_url = f"https://tse2.mm.bing.net/th?q={urllib.parse.quote(clean_name)}&w=800&h=800&c=7&rs=1&p=0"

        # 3. 본문 텍스트 추출 (쿠폰/카드할인 조건 및 가짜할인 키워드 분석용)
        lines: List[str] = []
        if html_text:
            body_m = re.search(r'<td[^>]*class=["\']?board-contents["\']?[^>]*>(.*?)</td>', html_text, re.DOTALL | re.IGNORECASE)
            raw_body = body_m.group(1) if body_m else ""
            clean_body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw_body, flags=re.DOTALL | re.IGNORECASE)
            clean_body = re.sub(r"<[^>]+>", "\n", clean_body)
            clean_body = html.unescape(clean_body)
            lines = [ln.strip() for ln in clean_body.splitlines() if ln.strip()]

        if fallback_desc:
            for fln in fallback_desc.splitlines():
                fln_s = fln.strip()
                if fln_s and fln_s not in lines:
                    lines.append(fln_s)
        body_summary = "\n".join(lines[:12])

        return {
            "shop_url": shop_url,
            "image_url": image_url,
            "body_text": body_summary,
        }

    def build_hotdeal_post_content(
        self,
        deal: Dict[str, Any],
        eval_result: Dict[str, Any],
        detail: Dict[str, str],
    ) -> Tuple[str, List[Tuple[str, str]]]:
        """
        '핫딜은 못참지' 카페 스타일의 직관적 제목 + 가독성 높은 문단 블록을 생성합니다.
        """
        tier_tag = eval_result["tier_tag"]  # 🚨 [가격오류급] / ⚡ [무지성 구매급] / 🔥 [역대가]
        mall = deal["mall"]
        clean_name = deal["clean_name"]
        sale_price = eval_result["sale_price"]
        real_normal = eval_result["real_normal_price"]
        discount_rate = eval_result["discount_rate"]
        unit_summary = eval_result.get("unit_summary")
        shipping = deal["shipping"]

        shop_url, is_coupang = self.apply_affiliate_tag(detail["shop_url"])
        image_url = detail.get("image_url") or ""

        # 제목 구성 (당근카페 제목 최대 50자 제한 준수 -> 안전하게 45자 이내로 자동 맞춤)
        prefix = f"{tier_tag} [{mall}] "
        suffix = f" {sale_price:,}원({discount_rate}%↓)"
        max_name_len = max(44 - len(prefix) - len(suffix), 8)
        short_name = clean_name if len(clean_name) <= max_name_len else clean_name[:max_name_len].rstrip()
        post_title = f"{prefix}{short_name}{suffix}"

        body_items: List[Tuple[str, str]] = []

        # 1) 상품 대표 이미지 (가장 위에 배치하여 클릭 시 한눈에 상품 확인)
        if image_url:
            body_items.append(("image", image_url))

        # 2) 도입부 한 줄 코멘트
        if "가격오류급" in tier_tag:
            intro = f"🚨 지금 바로 탑승하세요! 시중 실거래가 대비 약 {discount_rate}% 이상 빠진 가격오류/대란급 딜이 떴습니다. 재고 소진 시 예고 없이 바로 종료됩니다!"
        elif "무지성 구매급" in tier_tag:
            intro = f"⚡ 고민하면 품절되는 무지성 구매급 특가입니다! 평소 자주 쓰는 품목이라면 지금 쟁여두시는 걸 강력 추천드려요."
        else:
            intro = f"🔥 가짜 할인(정가 뻥튀기) 검증을 완료한 찐 역대가 딜입니다! 평소 눈여겨보셨던 분들은 이번 기회에 득템하세요."
        body_items.append(("text", intro))

        # 3) 핵심 가격 & 팩트체크 요약 박스
        body_items.append(("text", "━━━━━━━━━━━━━━━"))
        body_items.append(("text", f"📦 상품명 : {clean_name}"))
        body_items.append(("text", f"🏬 판매처 : {mall}"))
        body_items.append(("text", f"💸 평소 실거래가 : 약 {real_normal:,}원"))
        body_items.append(
            ("text", f"🔥 현재 핫딜가 : {sale_price:,}원 ({shipping} / 약 {discount_rate}% 할인!)")
        )
        if unit_summary:
            body_items.append(("text", f"📊 체감 단가 팩트체크 : {unit_summary}"))
        body_items.append(("text", "━━━━━━━━━━━━━━━"))

        # 4) 본문 내 추가 할인 팁(쿠폰/카드 안내 등)이 있으면 깔끔하게 정리하여 포함
        extra_lines = []
        for ln in (detail.get("body_text") or "").splitlines():
            ln_clean = ln.strip()
            if len(ln_clean) < 4 or ln_clean.startswith("http"):
                continue
            if any(w in ln_clean for w in ("쿠폰", "카드", "할인", "적립", "와우", "체감", "박스로", "개당", "원")):
                if ln_clean not in extra_lines:
                    extra_lines.append(ln_clean)
            if len(extra_lines) >= 3:
                break

        if extra_lines:
            body_items.append(("text", "💡 구매 참고 포인트"))
            for tip in extra_lines:
                body_items.append(("text", f"• {tip}"))

        # 5) 구매 직링크
        body_items.append(("text", "👇 품절 전 바로 구매하러 가기"))
        body_items.append(("text", shop_url))

        # 6) 마감 안내 및 파트너스 문구
        body_items.append(
            ("text", "※ 실시간 핫딜 특성상 준비된 재고가 소진되면 가격이 원래대로 인상되거나 품절될 수 있습니다.")
        )
        if is_coupang or "coupang" in mall.lower() or "쿠팡" in mall:
            body_items.append(
                ("text", "※ 이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다.")
            )

        return post_title, body_items

    def scan_and_publish(self, max_posts: int = 1, dry_run: bool = False) -> List[str]:
        """
        실시간 피드를 스캔하여 조건을 충족한 진짜 핫딜만 당근카페에 즉시 발행합니다.
        """
        feed_items = self.fetch_ppomppu_realtime_feed()
        print(f"[스캔] 실시간 피드에서 가격 포함 후보 딜 {len(feed_items)}건을 감지했습니다.", file=sys.stderr)
        published_urls: List[str] = []

        client: Optional[DaangnCafeClient] = None
        if not dry_run:
            client = DaangnCafeClient(slug=self.slug, env_path=self.env_path)

        qualifying_candidates: List[Tuple[Tuple[int, int, int, int], Dict[str, Any], Dict[str, Any]]] = []

        for deal in feed_items:
            deal_id = deal["deal_id"]
            if deal_id in self.posted_deals:
                continue

            mall_lower = deal["mall"].lower()
            title_lower = deal["raw_title"].lower()
            desc_lower = deal.get("rss_description", "").lower()
            if self.allowed_malls and not any(
                am in mall_lower or am in title_lower or am in desc_lower
                for am in self.allowed_malls
            ):
                print(
                    f"[쇼핑몰 제외] {deal['raw_title']} -> 허용 쇼핑몰({','.join(self.allowed_malls)}) 아님",
                    file=sys.stderr,
                )
                continue

            eval_res = self.validator.evaluate_deal(
                title=deal["raw_title"],
                sale_price=deal["sale_price"],
                upvotes=deal["upvotes"],
                downvotes=deal["downvotes"],
                views=deal["views"],
                body_text=deal.get("rss_description", ""),
            )

            if not eval_res.get("valid"):
                print(
                    f"[필터 탈락] {deal['raw_title']} -> {eval_res.get('reason')}",
                    file=sys.stderr,
                )
                continue

            tier_tag = eval_res["tier_tag"]
            tier_rank = 3 if "가격오류급" in tier_tag else (2 if "무지성" in tier_tag else 1)
            has_unit = 1 if eval_res.get("unit_summary") else 0
            score = (tier_rank, has_unit, int(eval_res["discount_rate"]), int(deal["upvotes"]))
            qualifying_candidates.append((score, deal, eval_res))

        # 가장 메리트가 큰 핫딜(가격오류급 > 무지성 구매급 > 역대가, 단위당 팩트체크 통과 우선) 순으로 정렬
        qualifying_candidates.sort(key=lambda x: x[0], reverse=True)

        for _, deal, eval_res in qualifying_candidates[:max_posts]:
            deal_id = deal["deal_id"]
            detail = self.fetch_deal_detail(
                deal["source_url"],
                fallback_desc=deal.get("rss_description", ""),
                mall=deal["mall"],
                clean_name=deal["clean_name"],
            )
            post_title, body_items = self.build_hotdeal_post_content(deal, eval_res, detail)
            print(
                f"[조건 충족! 즉시 발행 대상] {post_title} | 사유: {eval_res.get('reason')}",
                file=sys.stderr,
            )

            if dry_run:
                published_urls.append(f"[DRY-RUN] {post_title} -> {detail.get('shop_url')}")
                continue

            assert client is not None
            post_url = client.create_post_from_items(
                board=self.board_id,
                title=post_title,
                body_items=body_items,
                base_dir=self.base_dir,
            )
            published_urls.append(post_url)
            print(post_url)

            self.posted_deals[deal_id] = {
                "title": post_title,
                "sale_price": deal["sale_price"],
                "tier_tag": eval_res["tier_tag"],
                "discount_rate": eval_res["discount_rate"],
                "shop_url": detail.get("shop_url"),
                "daangn_post_url": post_url,
                "published_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.save_posted_db()

            if len(published_urls) < max_posts:
                time.sleep(5)

        return published_urls


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="realtime_deal_monitor.py",
        description="실시간 핫딜 탐지 및 가짜 할인 차단 자동 발행 봇",
    )
    p.add_argument("--slug", default=DEFAULT_HOTDEAL_CAFE_SLUG, help="대상 당근카페 slug")
    p.add_argument("--board", default=DEFAULT_HOTDEAL_BOARD_ID, help="대상 게시판 ID (기본: 2293448 🔥 오늘의 핫딜)")
    p.add_argument("--max-posts", type=int, default=1, help="1회 스캔 시 최대 발행 건수")
    p.add_argument("--once", action="store_true", help="1회 실시간 스캔 후 조건 충족 딜 즉시 발행 및 종료")
    p.add_argument("--daemon", action="store_true", help="백그라운드에서 상시 실시간 감시 루프 실행")
    p.add_argument("--interval", type=int, default=30, help="상시 감시 모드 스캔 주기(초, 기본 30초)")
    p.add_argument("--max-runtime", type=int, default=0, help="데몬 1회 최대 실행 시간(초, 0이면 무제한)")
    p.add_argument("--dry-run", action="store_true", help="실제 발행 없이 필터링 및 원고 생성 결과만 검증")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        monitor = RealtimeDealMonitor(slug=args.slug, board_id=args.board)
        if args.daemon:
            print(
                f"[실시간 감시 시작] 카페={args.slug}, 게시판={args.board}, 스캔주기={args.interval}초, 최대런타임={args.max_runtime}초",
                file=sys.stderr,
            )
            start_time = time.time()
            while True:
                if args.max_runtime > 0 and (time.time() - start_time) >= args.max_runtime:
                    print(f"[세션 교대] 최대 런타임({args.max_runtime}초)에 도달하여 다음 세션으로 자동 인계합니다.", file=sys.stderr)
                    break
                try:
                    pub_urls = monitor.scan_and_publish(max_posts=args.max_posts, dry_run=args.dry_run)
                    if pub_urls and not args.dry_run:
                        os.system('git add logs/ && git commit -m "Auto: 실시간 핫딜 발행" && git push || true')
                except DaangnCafeError as de:
                    if de.exit_code == 2:
                        raise
                    print(f"[일시 오류] {de}", file=sys.stderr)
                time.sleep(max(args.interval, 15))
        else:
            monitor.scan_and_publish(max_posts=args.max_posts, dry_run=args.dry_run)
        return 0

    except DaangnCafeError as e:
        cookie_val = os.environ.get("DAANGN_COOKIE", "")
        print(mask_sensitive(str(e), cookie_val), file=sys.stderr)
        return e.exit_code
    except Exception as e:
        cookie_val = os.environ.get("DAANGN_COOKIE", "")
        print(mask_sensitive(f"[오류] {e}", cookie_val), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
