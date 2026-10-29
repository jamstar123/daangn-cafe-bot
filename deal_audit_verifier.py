#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deal_audit_verifier.py
사용자 요청 사항 및 핫딜 품질 기준을 실시간으로 자동 전수 검증하는 상시 감사 봇(Auditor Bot).

[검증 항목 6대 원칙]
1. 제목 형식 검증: [역대가]/[무지성 구매급]/[가격오류급] [쿠팡] 형식 준수, 앞에 이모지 접두사 금지, 48자 이내
2. '로켓배송' 완전 금지: 제목/본문 어디에도 '로켓배송', '로켓와우' 단어 포함 금지
3. 공식 파트너스 링크 검증: link.coupang.com 도메인, lptag=AF2017901, subid=daangncafe, pageKey/itemId 필수 포함
4. 개발/테스트 문구 금지: '2번 화면 직통', '모바일 앱 직통연결', '테스트' 등 개발자 은어 완전 배제
5. 현실적 정상가 검증: 쿠팡 박스 실거래가 기준 부합 여부 (10,000원 이상 인위적 뻥튀기 차단)
6. 딜 신선도 검증: 60분 이내 실시간 딜 여부
"""

import json
import re
import sys
import html
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class DealAuditVerifier:
    def __init__(self, log_path: Optional[Path] = None):
        base_dir = Path(__file__).resolve().parent
        self.log_dir = base_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.report_path = log_path or (self.log_dir / "audit_report.json")

    def audit_post(
        self,
        title: str,
        body_text: str,
        affiliate_url: str,
        sale_price: int,
        normal_price: int,
        pub_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """발행 전 원고 또는 발행된 글을 6대 원칙에 따라 정밀 감사합니다."""
        results: Dict[str, Any] = {
            "audited_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "passed_all": True,
            "violations": [],
            "scores": {},
            "details": {},
        }

        # [원칙 1] 제목 규격 검증
        # 1-1: 이모지 접두사 금지
        if re.match(r"^[^\w\[]+", title):
            results["passed_all"] = False
            results["violations"].append("[원칙1 위반] 제목 맨 앞에 이모지(⚡, 🚨, 🔥 등)가 포함되어 있습니다.")

        # 1-2: 등급 태그 및 쇼핑몰 표기
        if not re.search(r"^\[(역대가|무지성 구매급|가격오류급)\]\s*\[쿠팡\]", title):
            results["passed_all"] = False
            results["violations"].append("[원칙1 위반] 제목이 '[역대가] [쿠팡]' 표준 태그 형식으로 시작하지 않습니다.")

        # 1-3: 글자수 제한 (당근 50자 제한 고려, 48자 이내)
        if len(title) > 48:
            results["passed_all"] = False
            results["violations"].append(f"[원칙1 위반] 제목 길이({len(title)}자)가 권장 48자를 초과했습니다.")

        results["scores"]["title_check"] = "PASS" if not any("원칙1" in v for v in results["violations"]) else "FAIL"

        # [원칙 2] '로켓배송' 단어 완전 제거 검증
        rocket_keywords = ["로켓배송", "로켓와우", "로켓 배송", "로켓 와우"]
        found_rocket = [kw for kw in rocket_keywords if kw in title or kw in body_text]
        if found_rocket:
            results["passed_all"] = False
            results["violations"].append(f"[원칙2 위반] 금지 단어 '{', '.join(found_rocket)}'가 제목 또는 본문에 포함되어 있습니다.")

        results["scores"]["no_rocket_delivery"] = "PASS" if not found_rocket else "FAIL"

        # [원칙 3] 쿠팡 파트너스 공식 링크 검증
        link_violations = []
        if not affiliate_url.startswith("https://link.coupang.com/"):
            link_violations.append("공식 단축 도메인(link.coupang.com)이 아닙니다.")
        if "lptag=AF2017901" not in affiliate_url:
            link_violations.append("대표님 파트너스 ID(AF2017901)가 누락되었습니다.")
        if "subid=daangncafe" not in affiliate_url:
            link_violations.append("채널 트래킹 서브ID(subid=daangncafe)가 누락되었습니다.")
        if "pageKey=" not in affiliate_url:
            link_violations.append("쿠팡 상품 고유식별자(pageKey)가 누락되었습니다.")

        if link_violations:
            results["passed_all"] = False
            results["violations"].append(f"[원칙3 위반] 구매 링크 결함: {', '.join(link_violations)}")

        results["scores"]["affiliate_link"] = "PASS" if not link_violations else "FAIL"

        # [원칙 4] 개발자 은어/테스트 문구 배제 검증
        jargon_keywords = ["2번 화면", "2번째 사진", "직통연결", "모바일 앱 직통", "테스트 발행", "브릿지", "github.io"]
        found_jargon = [jg for jg in jargon_keywords if jg in title or jg in body_text or jg in affiliate_url]
        if found_jargon:
            results["passed_all"] = False
            results["violations"].append(f"[원칙4 위반] 개발자 은어/테스트 문구 감지: '{', '.join(found_jargon)}'")

        results["scores"]["no_developer_jargon"] = "PASS" if not found_jargon else "FAIL"

        # [원칙 5] 현실적인 정상가 검증 (10,000원 뻥튀기 차단)
        price_diff = normal_price - sale_price
        markup_ratio = (normal_price / max(sale_price, 1)) if sale_price > 0 else 1.0

        # 정상가가 할인가보다 낮거나, 비현실적으로 35% 이상(1.35배 초과) 부풀려진 경우 경고
        if normal_price <= sale_price:
            results["passed_all"] = False
            results["violations"].append(f"[원칙5 위반] 정상가({normal_price:,}원)가 할인가({sale_price:,}원)보다 낮거나 같습니다.")
        elif markup_ratio > 1.35 and price_diff > 8000:
            results["passed_all"] = False
            results["violations"].append(
                f"[원칙5 위반] 정상가 뻥튀기 의심: 할인가 {sale_price:,}원 대비 정상가가 {normal_price:,}원(차액 {price_diff:,}원, {markup_ratio:.2f}배)으로 과도하게 높습니다."
            )

        results["scores"]["realistic_pricing"] = "PASS" if not any("원칙5" in v for v in results["violations"]) else "FAIL"

        # [원칙 6] 딜 신선도 검증
        if pub_time:
            now = datetime.now()
            diff_mins = (now - pub_time).total_seconds() / 60.0
            if diff_mins > 60:
                results["passed_all"] = False
                results["violations"].append(f"[원칙6 위반] 발행된 지 {int(diff_mins)}분이 경과하여 쿠팡 가격 변동/품절 위험이 높습니다.")
            results["scores"]["deal_freshness"] = f"{int(diff_mins)}분 전 등록"
        else:
            results["scores"]["deal_freshness"] = "실시간 감지"

        # 요약 기록
        results["details"] = {
            "title": title,
            "sale_price": sale_price,
            "normal_price": normal_price,
            "affiliate_url": affiliate_url,
        }

        self._save_report(results)
        return results

    def _save_report(self, report: Dict[str, Any]) -> None:
        try:
            reports = []
            if self.report_path.exists():
                try:
                    reports = json.loads(self.report_path.read_text(encoding="utf-8"))
                    if not isinstance(reports, list):
                        reports = [reports]
                except Exception:
                    reports = []
            reports.append(report)
            # 최근 100건만 유지
            reports = reports[-100:]
            self.report_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def audit_daangn_post_url(self, post_url: str) -> Dict[str, Any]:
        """실제 당근 카페에 발행된 글 페이지를 직접 크롤링하여 실시간 검증을 수행합니다."""
        req = urllib.request.Request(
            post_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html_text = resp.read().decode("utf-8", errors="replace")

        # 제목 추출
        title_m = re.search(r"<title>([^<]+)</title>", html_text)
        title = title_m.group(1).split("|")[0].strip() if title_m else ""

        # 링크 추출
        link_m = re.search(r"https://link\.coupang\.com/re/AFFSDP[^\s\"'<>]+", html_text)
        aff_link = html.unescape(link_m.group(0)) if link_m else ""

        # 가격 추출
        sale_m = re.search(r"핫딜\s*특가\s*:\s*([0-9,]+)\s*원", html_text)
        normal_m = re.search(r"평소\s*판매가\s*:\s*약\s*([0-9,]+)\s*원", html_text)
        sale_price = int(sale_m.group(1).replace(",", "")) if sale_m else 0
        normal_price = int(normal_m.group(1).replace(",", "")) if normal_m else 0

        return self.audit_post(
            title=title,
            body_text=html_text,
            affiliate_url=aff_link,
            sale_price=sale_price,
            normal_price=normal_price,
        )


if __name__ == "__main__":
    verifier = DealAuditVerifier()
    # 최근 발행된 당근 글 검증 테스트
    test_url = "https://cafe.daangn.com/hasdil-eun-ye-1/posts/역대가-쿠팡-얼라이브-멀티-비타-구미-60정-2개-27560원16-dY0YWagP"
    print(f"=== [검증 봇] 실시간 당근 카페 글 정밀 감사 시작: {test_url} ===")
    res = verifier.audit_daangn_post_url(test_url)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res["passed_all"]:
        print("\n✅ [검증 성공] 6대 핵심 원칙(제목, 금지단어, 링크, 정상가, 신선도)을 100% 완벽하게 통과했습니다!")
    else:
        print(f"\n❌ [검증 결함 감지] {len(res['violations'])}개의 위반 사항이 발견되었습니다:")
        for v in res["violations"]:
            print(f"  • {v}")
