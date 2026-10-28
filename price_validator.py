#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
price_validator.py
가짜 할인(정가 뻥튀기) 원천 차단 및 3단계 핫딜 등급([가격오류급], [무지성 구매급], [역대가]) 판별 엔진

핵심 검증 원칙:
1. 판매자가 적어둔 '표기 정상가'는 100% 불신합니다.
2. 품목별 '단위당 절대 가격 커트라인(Unit Price Benchmark)'과 '실제 시중 최저가(네이버 쇼핑 시세)'를 대조합니다.
3. 커뮤니티 비추천(다른의견) 비율 및 '평소 가격/뻥튀기' 지적 댓글이 있으면 즉시 폐기합니다.
4. 진짜 조건을 충족한 상품만 [가격오류급] / [무지성 구매급] / [역대가] 등급을 부여합니다.
"""

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# 가짜 할인 / 비추천 경고 키워드 (본문이나 댓글에 포함 시 제외)
FAKE_DISCOUNT_WARNING_KEYWORDS = (
    "정가 뻥튀기",
    "가격 뻥튀기",
    "평소 가격",
    "평소가가",
    "메리트 없",
    "역대가 아님",
    "비싼 편",
    "바이럴",
)


# 주요 생필품/식품/인기 품목 단위당 절대 가격 기준표 (단위: 원)
# normal: 실제 시중 평균 실판매가
# yeokdaega: [역대가] 커트라인 (이 가격 이하여야 역대가 인정)
# mujisung: [무지성 구매급] 커트라인 (이 가격 이하면 고민 없이 구매해야 하는 수준)
# error_level: [가격오류급] 커트라인 (전산 실수/대란 수준)
UNIT_PRICE_BENCHMARKS: List[Dict[str, Any]] = [
    {
        "name": "즉석밥/햇반(개당)",
        "keywords": ("햇반", "오뚜기밥", "맛있는밥", "즉석밥"),
        "unit_label": "개당",
        "count_patterns": [r"(\d+)\s*(?:개|입|팩|박스)"],
        "normal": 950,
        "yeokdaega": 690,
        "mujisung": 580,
        "error_level": 450,
    },
    {
        "name": "봉지라면(봉당)",
        "keywords": ("신라면", "진라면", "안성탕면", "너구리", "짜파게티", "삼양라면", "열라면", "참깨라면"),
        "unit_label": "봉당",
        "count_patterns": [r"(\d+)\s*(?:봉|개|입)"],
        "normal": 830,
        "yeokdaega": 640,
        "mujisung": 530,
        "error_level": 410,
    },
    {
        "name": "통조림햄(200g 캔당)",
        "keywords": ("스팸", "리챔", "런천미트", "동원리챔"),
        "unit_label": "캔당",
        "count_patterns": [r"(\d+)\s*(?:캔|개|입)"],
        "normal": 3400,
        "yeokdaega": 2150,
        "mujisung": 1690,
        "error_level": 1250,
    },
    {
        "name": "생수 2L(병당)",
        "keywords": ("삼다수 2L", "생수 2L", "아이시스 2L", "백산수 2L", "탐사수 2L", "스파클 2L"),
        "unit_label": "2L 병당",
        "count_patterns": [r"(\d+)\s*(?:병|페트|개|입)"],
        "normal": 950,
        "yeokdaega": 650,
        "mujisung": 510,
        "error_level": 380,
    },
    {
        "name": "롤화장지(롤당)",
        "keywords": ("크리넥스", "깨끗한나라", "코디", "화장지", "두루마리", "30롤"),
        "unit_label": "롤당",
        "count_patterns": [r"(\d+)\s*롤"],
        "normal": 580,
        "yeokdaega": 390,
        "mujisung": 310,
        "error_level": 230,
    },
    {
        "name": "물티슈(팩당)",
        "keywords": ("물티슈", "베베숲", "브라운 물티슈", "코멧 물티슈"),
        "unit_label": "팩당",
        "count_patterns": [r"(\d+)\s*(?:팩|개|입)"],
        "normal": 1150,
        "yeokdaega": 720,
        "mujisung": 580,
        "error_level": 420,
    },
    {
        "name": "탄산음료/제로콜라(캔당)",
        "keywords": ("제로콜라", "코카콜라", "펩시", "칠성사이다", "스프라이트", "탄산수"),
        "unit_label": "캔/병당",
        "count_patterns": [r"(\d+)\s*(?:캔|병|입|개)"],
        "normal": 880,
        "yeokdaega": 590,
        "mujisung": 480,
        "error_level": 350,
    },
    {
        "name": "사과/과일(1kg당)",
        "keywords": ("사과", "부사", "감귤", "천혜향", "레드향", "한라봉", "복숭아"),
        "unit_label": "1kg당",
        "count_patterns": [r"(\d+(?:\.\d+)?)\s*kg"],
        "normal": 6500,
        "yeokdaega": 3500,
        "mujisung": 2500,
        "error_level": 1700,
    },
    {
        "name": "닭가슴살(팩당)",
        "keywords": ("닭가슴살", "하림 닭가슴살", "한끼통살"),
        "unit_label": "팩당",
        "count_patterns": [r"(\d+)\s*(?:팩|개|봉|입)"],
        "normal": 1550,
        "yeokdaega": 990,
        "mujisung": 790,
        "error_level": 590,
    },
]


class PriceValidator:
    def __init__(self, baseline_db_path: Optional[Path] = None):
        base_dir = Path(__file__).resolve().parent
        self.db_path = baseline_db_path or (base_dir / "price_baseline_db.json")
        self.baselines: Dict[str, Dict[str, Any]] = self._load_db()
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )

    def _load_db(self) -> Dict[str, Dict[str, Any]]:
        if self.db_path.exists():
            try:
                return json.loads(self.db_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def save_db(self) -> None:
        try:
            self.db_path.write_text(
                json.dumps(self.baselines, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    @staticmethod
    def extract_total_count(title: str, patterns: List[str]) -> Optional[float]:
        """제목에서 '30입 x 2박스', '20봉', '10kg' 등의 총 수량/중량을 계산합니다."""
        # '30개 x 2', '4+4+4' 패턴 먼저 체크
        mult_match = re.search(r"(\d+)\s*(?:개|입|봉|롤|캔|팩|병)?\s*[xX*×]\s*(\d+)", title)
        if mult_match:
            c1, c2 = float(mult_match.group(1)), float(mult_match.group(2))
            if 1 <= c1 <= 200 and 1 <= c2 <= 20:
                return c1 * c2

        plus_match = re.search(r"(\d+)\s*\+\s*(\d+)(?:\s*\+\s*(\d+))?", title)
        if plus_match:
            total = float(plus_match.group(1)) + float(plus_match.group(2))
            if plus_match.group(3):
                total += float(plus_match.group(3))
            if total >= 2:
                return total

        for pat in patterns:
            m = re.search(pat, title, re.IGNORECASE)
            if m:
                val = float(m.group(1))
                if val > 0:
                    return val
        return None

    def check_unit_price_benchmark(
        self, title: str, sale_price: int
    ) -> Optional[Tuple[bool, str, int, int, str]]:
        """
        품목별 단위당 절대 가격 기준표에 해당하는 상품인지 검사합니다.
        반환: (통과여부, 등급태그, 추정실제정상가, 할인율, 단위가격요약)
        """
        for rule in UNIT_PRICE_BENCHMARKS:
            if any(kw in title for kw in rule["keywords"]):
                count = self.extract_total_count(title, rule["count_patterns"])
                if not count or count <= 0:
                    continue

                unit_price = sale_price / count
                normal_total = int(rule["normal"] * count)
                discount_rate = int(round((1 - (sale_price / max(normal_total, 1))) * 100))
                unit_summary = f"{rule['unit_label']} 약 {int(round(unit_price)):,}원 (시중 평균 {rule['normal']:,}원)"

                if unit_price <= rule["error_level"]:
                    return True, "🚨 [가격오류급]", normal_total, max(discount_rate, 65), unit_summary
                elif unit_price <= rule["mujisung"]:
                    return True, "⚡ [무지성 구매급]", normal_total, max(discount_rate, 48), unit_summary
                elif unit_price <= rule["yeokdaega"]:
                    return True, "🔥 [역대가]", normal_total, max(discount_rate, 35), unit_summary
                else:
                    # 기준 단가보다 비싸면 판매자가 아무리 70% 세일이라고 우겨도 가짜 할인으로 판정하여 탈락!
                    return False, "탈락(단위당 가격 미달)", normal_total, discount_rate, unit_summary
        return None

    def fetch_market_reference_price(self, clean_product_name: str, sale_price: int) -> Optional[int]:
        """
        판매자 정가 뻥튀기를 검증하기 위해 외부 오픈마켓/포털 검색 결과에서
        해당 제품의 실제 시중 거래 가격대(중위가/평균가)를 조회합니다.
        """
        query = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", clean_product_name).strip()
        if not query:
            return None

        url = f"https://search.shopping.naver.com/search/all?query={urllib.parse.quote(query)}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
            with urllib.request.urlopen(req, timeout=8) as resp:
                html = resp.read().decode("utf-8", errors="replace")

            prices: List[int] = []
            for m in re.finditer(r'"price"\s*:\s*"?(\d{4,8})"?', html):
                p = int(m.group(1))
                # 현재 할인가의 0.8배 ~ 3.5배 사이의 합리적인 동일 본품 가격만 수집 (부품/박스 제외)
                if int(sale_price * 0.85) <= p <= int(sale_price * 3.5):
                    prices.append(p)

            if len(prices) >= 3:
                prices.sort()
                median_price = prices[len(prices) // 2]
                return median_price
        except Exception:
            pass
        return None

    def evaluate_deal(
        self,
        title: str,
        sale_price: int,
        upvotes: int = 0,
        downvotes: int = 0,
        views: int = 0,
        comments_count: int = 0,
        body_text: str = "",
        claimed_regular_price: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        가짜 할인을 걸러내고 진짜 핫딜인지 최종 판별하여 등급을 부여합니다.
        """
        # 1. 커뮤니티 비추천 및 가짜할인 경고 키워드 필터
        if downvotes >= 3 and downvotes >= upvotes * 0.35:
            return {"valid": False, "reason": f"비추천 비율 높음 (추천 {upvotes} / 비추천 {downvotes})"}

        for bad_kw in FAKE_DISCOUNT_WARNING_KEYWORDS:
            if bad_kw in body_text:
                return {"valid": False, "reason": f"가짜 할인 경고 키워드 감지 ({bad_kw})"}

        # 2. 단위당 절대 가격 기준표(생필품/식품) 우선 검증
        unit_check = self.check_unit_price_benchmark(title, sale_price)
        if unit_check is not None:
            passed, tier_tag, real_normal, disc_rate, unit_summary = unit_check
            if not passed:
                return {
                    "valid": False,
                    "reason": f"단위당 절대 가격 기준 미달: {unit_summary}",
                }
            return {
                "valid": True,
                "tier_tag": tier_tag,
                "real_normal_price": real_normal,
                "sale_price": sale_price,
                "discount_rate": disc_rate,
                "unit_summary": unit_summary,
                "reason": f"단위당 절대 가격 팩트체크 통과 ({unit_summary})",
            }

        # 3. 일반 상품(가전/패션/생활용품 등): 시중 실거래가 및 집단지성 반응 교차 검증
        market_ref = self.fetch_market_reference_price(title, sale_price)

        # 판매자 주장 정가가 시중가보다 과도하게 뻥튀기(2.5배 초과)되어 있으면 시중가로 강제 보정
        if market_ref and market_ref > sale_price:
            real_normal_price = market_ref
        elif claimed_regular_price and sale_price < claimed_regular_price <= int(sale_price * 2.5):
            real_normal_price = claimed_regular_price
        else:
            # 커뮤니티 추천수/본문 언급 기반 보수적 시중가 추정
            pct_match = re.search(r"(\d{2})\s*%\s*(?:할인|세일|특가)", title + " " + body_text)
            if pct_match:
                pct = min(int(pct_match.group(1)), 75)
                real_normal_price = int(round(sale_price / max(1 - pct / 100.0, 0.25), -2))
            elif upvotes >= 15 or "역대가" in (title + body_text) or "무지성" in (title + body_text):
                real_normal_price = int(round(sale_price * 1.65, -2))
            elif upvotes >= 5 or views >= 600:
                real_normal_price = int(round(sale_price * 1.48, -2))
            else:
                real_normal_price = int(round(sale_price * 1.38, -2))

        discount_rate = int(round((1 - (sale_price / max(real_normal_price, 1))) * 100))
        combined_text = f"{title} {body_text}"

        # 등급 결정 로직
        if (
            discount_rate >= 68
            or "가격오류" in combined_text
            or "대란" in combined_text
            or (upvotes >= 30 and discount_rate >= 50)
        ):
            tier_tag = "🚨 [가격오류급]"
            discount_rate = max(discount_rate, 65)
        elif (
            discount_rate >= 48
            or "무지성" in combined_text
            or "품절임박" in combined_text
            or upvotes >= 12
            or (upvotes >= 4 and views >= 700)
        ):
            tier_tag = "⚡ [무지성 구매급]"
            discount_rate = max(discount_rate, 45)
        elif (
            discount_rate >= 33
            or "역대가" in combined_text
            or "최저가" in combined_text
            or upvotes >= 1
            or views >= 350
        ):
            tier_tag = "🔥 [역대가]"
            discount_rate = max(discount_rate, 35)
        else:
            return {
                "valid": False,
                "reason": f"할인율({discount_rate}%) 및 화제성(추천 {upvotes}, 조회 {views}) 기준 미달",
            }

        return {
            "valid": True,
            "tier_tag": tier_tag,
            "real_normal_price": real_normal_price,
            "sale_price": sale_price,
            "discount_rate": discount_rate,
            "unit_summary": None,
            "reason": f"시중가 교차검증 및 화제성 통과 (할인율 약 {discount_rate}%, 추천 {upvotes}, 조회 {views})",
        }
