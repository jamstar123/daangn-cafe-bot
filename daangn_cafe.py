#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daangn_cafe.py
당근카페 무인 자동 발행 CLI 및 통신 모듈

보안 규칙:
- 당근 세션 쿠키(DAANGN_COOKIE)는 반드시 .env 파일(또는 GitHub Secrets 환경변수)에서만 로드하며,
  절대로 표준 출력(stdout), 에러 출력(stderr), 로그 파일에 노출하지 않습니다.
"""

import argparse
import json
import mimetypes
import os
import re
import struct
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import urllib.request
import urllib.error
import urllib.parse

# 종료 코드 정의 (스케줄러/워커 연동용)
EXIT_SUCCESS = 0
EXIT_GENERAL_ERROR = 1
EXIT_AUTH_EXPIRED = 2       # 쿠키 만료 또는 super_host 권한 없음 -> 스케줄러 즉시 중단
EXIT_ACTION_ID_CHANGED = 3  # Next-Action ID 불일치/변경 감지 -> 재확인 필요
EXIT_RATE_LIMIT_FAILED = 4  # 요청 제한 대기 후 재시도 초과

DEFAULT_POST_ACTION_ID = "40ec4883aa4a0be726249db944264360e828b02bb2"
DEFAULT_IMAGE_ACTION_ID = "40c9d529704d23a3514ffcea26bda581a58a0672ca"
DEFAULT_BASE_URL = "https://cafe.daangn.com"


def load_env(env_path: Path) -> Dict[str, str]:
    """외부 패키지 의존성 없이 .env 파일을 안전하게 파싱하여 환경 변수에 병합합니다."""
    env_vars: Dict[str, str] = {}
    if not env_path.exists():
        return env_vars

    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if (val.startswith('"') and val.endswith('"')) or (
                val.startswith("'") and val.endswith("'")
            ):
                val = val[1:-1]
            env_vars[key] = val
            if key not in os.environ or not os.environ[key]:
                os.environ[key] = val
    return env_vars


def normalize_cookie(raw_cookie: str) -> str:
    """쿠키 값에 __Secure-authjs.session-token= 접두사가 누락된 경우 자동으로 보정합니다."""
    c = raw_cookie.strip().strip('"').strip("'")
    if not c:
        return ""
    if "session-token=" not in c and c.startswith("eyJ"):
        return f"__Secure-authjs.session-token={c}"
    return c


def mask_sensitive(text: str, cookie: str) -> str:
    """로그나 에러 메시지에서 쿠키 원문이 절대 출력되지 않도록 마스킹합니다."""
    if not text:
        return text
    if cookie:
        raw_val = cookie.replace("__Secure-authjs.session-token=", "")
        if cookie in text:
            text = text.replace(cookie, "[REDACTED_COOKIE]")
        if raw_val and len(raw_val) > 15 and raw_val in text:
            text = text.replace(raw_val, "[REDACTED_TOKEN]")
    text = re.sub(r"(?i)(cookie\s*[:=]\s*)([^\r\n]+)", r"\1[REDACTED]", text)
    text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_.-]+", "[REDACTED_JWT]", text)
    return text


def get_image_dimensions_from_bytes(img_bytes: bytes) -> Tuple[int, int]:
    """외부 라이브러리(PIL) 없이 이미지 바이트에서 가로/세로 해상도(width, height)를 추출합니다."""
    try:
        head = img_bytes[:32]
        if len(head) < 24:
            return 1080, 1080
        if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
            w, h = struct.unpack(">LL", head[16:24])
            return int(w), int(h)
        if head[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", head[6:10])
            return int(w), int(h)
        if head[:2] == b"\xff\xd8":
            idx = 2
            length = len(img_bytes)
            while idx < length:
                while idx < length and img_bytes[idx] == 0xFF:
                    idx += 1
                if idx >= length:
                    break
                ftype = img_bytes[idx]
                idx += 1
                if ftype in (0xD8, 0xD9) or (0xD0 <= ftype <= 0xD7):
                    continue
                if idx + 2 > length:
                    break
                size = struct.unpack(">H", img_bytes[idx : idx + 2])[0]
                if 0xC0 <= ftype <= 0xCF and ftype not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", img_bytes[idx + 3 : idx + 7])
                    return int(w), int(h)
                idx += size
    except Exception:
        pass
    return 1080, 1080


class DaangnCafeError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_GENERAL_ERROR):
        super().__init__(message)
        self.exit_code = exit_code


class DaangnCafeClient:
    def __init__(self, slug: str, env_path: Optional[Path] = None):
        base_dir = Path(__file__).resolve().parent
        self.env_path = env_path or (base_dir / ".env")
        load_env(self.env_path)

        self.slug = slug.strip().lstrip("/")
        raw_cookie = os.environ.get("DAANGN_COOKIE", "")
        self.cookie = normalize_cookie(raw_cookie)
        if not self.cookie or "YOUR_DAANGN_SESSION_COOKIE_HERE" in self.cookie:
            raise DaangnCafeError(
                "[오류] .env 또는 환경변수에 유효한 DAANGN_COOKIE가 설정되어 있지 않습니다.",
                EXIT_AUTH_EXPIRED,
            )

        self.base_url = os.environ.get("DAANGN_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        self.cafe_path_prefix = os.environ.get("DAANGN_CAFE_PREFIX", "").rstrip("/")
        self.post_action_id = os.environ.get("DAANGN_POST_ACTION_ID", DEFAULT_POST_ACTION_ID).strip()
        self.image_action_id = os.environ.get("DAANGN_IMAGE_ACTION_ID", DEFAULT_IMAGE_ACTION_ID).strip()
        self.max_retries = int(os.environ.get("DAANGN_MAX_RETRIES", "3"))
        self.rate_limit_wait = int(os.environ.get("DAANGN_RATE_LIMIT_WAIT", "60"))
        self.user_agent = os.environ.get(
            "DAANGN_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        )
        self._cached_cafe_html: Optional[str] = None
        self._cached_cafe_id: Optional[int] = None

    def _cafe_url(self, subpath: str = "") -> str:
        path = f"{self.cafe_path_prefix}/{self.slug}" if self.cafe_path_prefix else f"/{self.slug}"
        if subpath:
            path = f"{path}/{subpath.lstrip('/')}"
        return f"{self.base_url}{path}"

    def _request(
        self,
        method: str,
        url: str,
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        include_cookie: bool = True,
    ) -> Tuple[int, Dict[str, str], str]:
        req_headers = {
            "User-Agent": self.user_agent,
        }
        if include_cookie:
            req_headers["Cookie"] = self.cookie
        if headers:
            req_headers.update(headers)

        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    status = resp.getcode()
                    resp_headers = {k.lower(): v for k, v in resp.info().items()}
                    body_bytes = resp.read()
                    body_text = body_bytes.decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                status = e.code
                resp_headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
                try:
                    body_text = e.read().decode("utf-8", errors="replace")
                except Exception:
                    body_text = ""
            except urllib.error.URLError as e:
                safe_reason = mask_sensitive(str(e.reason), self.cookie)
                raise DaangnCafeError(f"[네트워크 오류] 서버에 연결할 수 없습니다: {safe_reason}")

            # 1) "요청이 너무 많아요" 또는 HTTP 429 처리 (60초 대기 후 최대 max_retries 재시도)
            if status == 429 or "요청이 너무 많아요" in body_text or "Too Many Requests" in body_text:
                if attempt > self.max_retries:
                    raise DaangnCafeError(
                        f"[제한 초과] '요청이 너무 많아요' 에러가 {self.max_retries}회 연속 발생하여 재시도를 중단합니다.",
                        EXIT_RATE_LIMIT_FAILED,
                    )
                print(
                    f"[대기] '요청이 너무 많아요' 감지됨. {self.rate_limit_wait}초 대기 후 재시도합니다... ({attempt}/{self.max_retries})",
                    file=sys.stderr,
                )
                time.sleep(self.rate_limit_wait)
                continue

            # 2) 쿠키 만료 / 인증 실패 감지
            if status in (401, 403) or any(
                kw in body_text
                for kw in ("로그인이 필요", "세션이 만료", "UNAUTHENTICATED", "UNAUTHORIZED", "login_required")
            ):
                raise DaangnCafeError(
                    "[인증 만료] 당근 세션 쿠키가 만료되었거나 권한이 없습니다. 새 쿠키를 설정해 주세요.",
                    EXIT_AUTH_EXPIRED,
                )

            # 3) Next.js Server Action ID 변경 여부 확인
            if (
                status == 404
                or "Server action not found" in body_text
                or "Failed to find Server Action" in body_text
                or "ACTION_NOT_FOUND" in body_text
            ):
                if req_headers.get("Next-Action"):
                    raise DaangnCafeError(
                        f"[액션 ID 확인 필요] Next-Action ID({req_headers.get('Next-Action')})가 서버에서 거부되었습니다. "
                        "액션 ID 변경 여부를 재확인해 주세요.",
                        EXIT_ACTION_ID_CHANGED,
                    )

            return status, resp_headers, body_text

    def auto_refresh_server_action_ids(self) -> bool:
        """
        당근 마켓이 프론트엔드를 신규 배포하여 Next-Action ID가 변경된 경우,
        posts/new 페이지 및 dynamic chunk를 자동으로 추적하여 최신 action ID로 자가 갱신합니다.
        """
        try:
            url = f"{self.base_url}/{self.slug}/posts/new"
            headers = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
            _, _, html = self._request("GET", url, headers=headers, include_cookie=True)
            w_match = re.search(r'\"([^\"]*static/chunks/[^\"]+)\"[^\]]*WritePageClient', html)
            if not w_match:
                return False
            w_url = w_match.group(1)
            if not w_url.startswith("http"):
                w_url = urllib.parse.urljoin(self.base_url, w_url)

            _, _, w_js = self._request("GET", w_url, include_cookie=False)
            chunk_matches = re.findall(r'\"([^\"]*static/chunks/[^\"]+\.js)\"', w_js)
            found = False
            for chunk_path in chunk_matches:
                c_url = chunk_path if chunk_path.startswith("http") else urllib.parse.urljoin(w_url, chunk_path)
                try:
                    _, _, c_js = self._request("GET", c_url, include_cookie=False)
                    post_m = re.search(r'\"(4[a-f0-9]{39})\".*?createPostAction', c_js)
                    img_m = re.search(r'\"(4[a-f0-9]{39})\".*?getImageUploadUrl', c_js)
                    if post_m:
                        self.post_action_id = post_m.group(1)
                        found = True
                    if img_m:
                        self.image_action_id = img_m.group(1)
                        found = True
                except Exception:
                    continue
            return found
        except Exception:
            return False

    def request_server_action(self, url: str, action_id: str, payload: Any, retry_on_changed: bool = True) -> str:
        """
        통신 규격 2번:
        - 브라우저 없이 당근 서버에 직접 POST 요청
        - 헤더: Accept: text/x-component, Content-Type: text/plain;charset=UTF-8
        - Next-Action: <action_id>
        """
        headers = {
            "Accept": "text/x-component",
            "Content-Type": "text/plain;charset=UTF-8",
            "Next-Action": action_id,
            "Origin": self.base_url,
            "Referer": url,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, _, body = self._request("POST", url, data=data, headers=headers, include_cookie=True)
        if status >= 400 or "Server Action unavailable" in body:
            if retry_on_changed and self.auto_refresh_server_action_ids():
                new_action_id = self.post_action_id if "cafeSlug" in str(payload) else self.image_action_id
                headers["Next-Action"] = new_action_id
                status, _, body = self._request("POST", url, data=data, headers=headers, include_cookie=True)
                if status < 400 and "Server Action unavailable" not in body:
                    return body
            safe_body = mask_sensitive(body[:300], self.cookie)
            raise DaangnCafeError(
                f"[요청 실패] HTTP {status} (Action ID: {action_id}) 변경 여부를 재확인해 주세요. 응답 요약: {safe_body}",
                EXIT_ACTION_ID_CHANGED if status in (400, 404, 500) else EXIT_GENERAL_ERROR,
            )
        return body

    @staticmethod
    def parse_rsc_flight_response(body: str) -> List[Any]:
        """Next.js RSC (text/x-component) 응답 라인들을 파싱하여 JSON 객체 리스트를 반환합니다."""
        parsed_chunks: List[Any] = []
        for line in body.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            _, raw_json = line.split(":", 1)
            raw_json = raw_json.strip()
            if raw_json and raw_json[0].isalpha() and len(raw_json) > 1 and raw_json[1] in "[{":
                raw_json = raw_json[1:]
            try:
                parsed_chunks.append(json.loads(raw_json))
            except Exception:
                continue
        return parsed_chunks

    def _fetch_cafe_page_decoded(self) -> str:
        if self._cached_cafe_html is not None:
            return self._cached_cafe_html
        url = self._cafe_url()
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        status, _, body = self._request("GET", url, headers=headers, include_cookie=True)
        if status >= 400:
            raise DaangnCafeError(f"[연결 실패] 카페 페이지 조회 실패 (HTTP {status})", EXIT_GENERAL_ERROR)

        decoded = body.replace('\\"', '"').replace("\\\\", "\\")
        self._cached_cafe_html = decoded

        cafe_id_match = re.search(r'"cafeId"\s*:\s*(\d+)', decoded)
        if cafe_id_match:
            self._cached_cafe_id = int(cafe_id_match.group(1))

        return decoded

    def check_membership(self) -> Dict[str, Any]:
        """
        3. 연결 확인: membership
        자격: 회원 상태 super_host 확인.
        """
        decoded = self._fetch_cafe_page_decoded()

        is_super_host = (
            '"permissionLevel":"super_host"' in decoded
            or '"role":"super_host"' in decoded
            or "super_host" in decoded
        )
        perm_match = re.search(r'"permissionLevel"\s*:\s*"([^"]+)"', decoded)
        detected_role = "super_host" if is_super_host else (perm_match.group(1) if perm_match else "unknown")

        if not is_super_host:
            raise DaangnCafeError(
                f"[권한 오류] 현재 계정의 회원 상태가 'super_host'가 아닙니다 (감지된 상태: {detected_role}). "
                "쿠키 만료 또는 권한 변경 여부를 확인해 주세요.",
                EXIT_AUTH_EXPIRED,
            )

        return {
            "slug": self.slug,
            "cafeId": self._cached_cafe_id,
            "role": "super_host",
            "status": "active",
        }

    def list_boards(self) -> List[Dict[str, str]]:
        """
        3. 게시판 확인: boards
        카페의 게시판(카테고리) 목록을 조회합니다.
        """
        decoded = self._fetch_cafe_page_decoded()
        boards: List[Dict[str, str]] = []
        seen_ids = set()

        cat_pattern = re.compile(
            r'\{\s*"id"\s*:\s*(\d+)\s*,\s*"type"\s*:\s*"([^"]+)"\s*,\s*"category"\s*:\s*"([^"]+)"'
        )
        for m in cat_pattern.finditer(decoded):
            b_id, b_type, b_name = m.group(1), m.group(2), m.group(3)
            if b_id not in seen_ids:
                seen_ids.add(b_id)
                boards.append({"id": b_id, "name": b_name, "type": b_type})

        if not boards:
            item_pattern = re.compile(
                r'\{\s*"id"\s*:\s*"([^"]+)"\s*,\s*"label"\s*:\s*"([^"]+)"'
            )
            for m in item_pattern.finditer(decoded):
                b_id, b_name = m.group(1), m.group(2)
                if b_id != "all" and b_id not in seen_ids:
                    seen_ids.add(b_id)
                    boards.append({"id": b_id, "name": b_name})

        return boards

    def resolve_board_id(self, board_arg: str) -> int:
        """사용자가 게시판 번호, 이름, 일부 키워드, 혹은 'free'를 입력하더라도 정확한 숫자 게시판 ID(int)로 변환합니다."""
        boards = self.list_boards()
        arg = board_arg.strip()

        if boards:
            for b in boards:
                if str(b["id"]) == arg:
                    return int(b["id"])
            for b in boards:
                if arg and (arg in b["name"] or b["name"] in arg):
                    return int(b["id"])
            if arg.lower() in ("", "free", "default", "자유", "자유게시판"):
                for b in boards:
                    if b.get("type") == "default" or "자유" in b["name"]:
                        return int(b["id"])
                return int(boards[0]["id"])
            # 요청한 게시판 ID가 카페에 없으면(게시판이 바뀌거나 삭제됨) 이름으로 핫딜 게시판을 찾는다.
            # 죽은 ID를 그대로 보내면 당근이 매번 "잘못된 게시판"으로 거부해서 조용히 무게시 상태가 된다.
            for b in boards:
                if "핫딜" in b["name"]:
                    print(f"[경고] 요청 게시판 {arg}이(가) 카페에 없어 이름으로 찾은 핫딜 게시판 {b['id']}({b['name']})을 사용합니다.")
                    return int(b["id"])

        return int(arg) if arg.isdigit() else 2252250

    def _fetch_fallback_food_image(self, query: str) -> Tuple[bytes, str]:
        """키워드 또는 URL 실패 시 TheMealDB 레시피 이미지 CDN에서 고화질 음식 JPEG 사진을 가져옵니다."""
        for api_url in (
            f"https://www.themealdb.com/api/json/v1/1/search.php?s={urllib.parse.quote(query)}",
            "https://www.themealdb.com/api/json/v1/1/random.php",
        ):
            try:
                req = urllib.request.Request(api_url, headers={"User-Agent": self.user_agent})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                meals = data.get("meals") or []
                if meals:
                    thumb = meals[0].get("strMealThumb")
                    if thumb:
                        img_req = urllib.request.Request(thumb, headers={"User-Agent": self.user_agent})
                        with urllib.request.urlopen(img_req, timeout=30) as img_resp:
                            return img_resp.read(), "image/jpeg"
            except Exception:
                continue
        raise DaangnCafeError(f"[이미지 오류] 음식 이미지를 가져올 수 없습니다: {query}")

    def upload_image(self, image_ref: str, base_dir: Optional[Path] = None) -> str:
        """
        이미지 업로드 규격:
        - 로컬 파일 경로뿐만 아니라 https:// 웹 이미지 URL도 자동 다운로드하여 당근 서버로 업로드 지원
        1) 액션 ID (40a64c6f3424be009d7225054055934226e1de89b9, getImageUploadUrl) 호출:
           payload: [{"contentType": mime_type, "width": width, "height": height}]
        2) 응답의 uploadUrl 로 PUT 요청 (헤더: Content-Type, x-amz-meta-id: o.image.id)
        3) 업로드된 image.id 반환
        """
        ref = str(image_ref).strip()
        if ref.startswith("http://") or ref.startswith("https://"):
            try:
                parsed_img = urllib.parse.urlparse(ref)
                referer = (
                    "https://www.ppomppu.co.kr/"
                    if "ppomppu.co.kr" in parsed_img.netloc
                    else f"{parsed_img.scheme}://{parsed_img.netloc}/"
                )
                img_headers = {
                    "User-Agent": self.user_agent,
                    "Referer": referer,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                }
                req = urllib.request.Request(ref, headers=img_headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    img_bytes = resp.read()
                    content_type = resp.info().get_content_type() or "image/jpeg"
                if content_type not in ("image/jpeg", "image/png", "image/webp", "image/avif"):
                    content_type = "image/jpeg"
                mime_type = content_type
            except Exception:
                img_bytes, mime_type = self._fetch_fallback_food_image("keto food dish")
        else:
            img_path = Path(ref)
            if not img_path.is_absolute() and base_dir:
                img_path = (base_dir / img_path).resolve()
            if not img_path.exists():
                img_bytes, mime_type = self._fetch_fallback_food_image(ref)
            else:
                mime_type, _ = mimetypes.guess_type(str(img_path))
                mime_type = mime_type or "image/jpeg"
                img_bytes = img_path.read_bytes()

        width, height = get_image_dimensions_from_bytes(img_bytes)

        action_url = self._cafe_url("posts/new")
        payload = [
            {
                "contentType": mime_type,
                "width": width,
                "height": height,
            }
        ]
        body = self.request_server_action(action_url, self.image_action_id, payload)
        chunks = self.parse_rsc_flight_response(body)

        upload_url: Optional[str] = None
        image_id: Optional[str] = None

        for chunk in chunks:
            if isinstance(chunk, dict):
                upload_url = upload_url or chunk.get("uploadUrl") or chunk.get("presignedUrl")
                if isinstance(chunk.get("image"), dict):
                    image_id = image_id or str(chunk["image"].get("id") or "")
                image_id = image_id or (str(chunk["id"]) if chunk.get("id") else None)

        if not upload_url or not image_id:
            raise DaangnCafeError(
                f"[이미지 업로드 실패] 업로드 URL 또는 이미지 ID를 응답에서 찾을 수 없습니다. "
                f"액션 ID({self.image_action_id}) 변경 여부를 확인해 주세요.",
                EXIT_ACTION_ID_CHANGED,
            )

        put_headers = {
            "Content-Type": mime_type,
            "x-amz-meta-id": image_id,
            "Content-Length": str(len(img_bytes)),
        }
        put_status, _, _ = self._request(
            "PUT", upload_url, data=img_bytes, headers=put_headers, include_cookie=False
        )
        if put_status >= 400:
            raise DaangnCafeError(f"[이미지 PUT 실패] 스토리지 업로드 실패 (HTTP {put_status})")

        return image_id

    def build_blocks_from_items(
        self,
        body_items: List[Tuple[str, str]],
        base_dir: Optional[Path] = None,
        extra_images: Optional[List[str]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        원고 본문 아이템 리스트([('text', '문단'), ('image', '경로/URL'), ...])를 순서대로 처리하여
        문단 중간에도 사진이 자연스럽게 배치되는 blockContent.blocks를 생성합니다.
        """
        blocks: List[Dict[str, Any]] = []
        default_attrs = {
            "bold": False,
            "italic": False,
            "underline": False,
            "strikethrough": False,
        }

        for item_type, item_val in body_items:
            if item_type == "image":
                uploaded_id = self.upload_image(item_val, base_dir=base_dir)
                blocks.append(
                    {
                        "id": str(uuid.uuid4()),
                        "type": "image",
                        "imageAttribute": {
                            "imageId": str(uploaded_id),
                        },
                    }
                )
            elif item_type == "text":
                text_val = item_val.strip()
                if text_val:
                    blocks.append(
                        {
                            "id": str(uuid.uuid4()),
                            "type": "paragraph",
                            "contents": [
                                {
                                    "id": str(uuid.uuid4()),
                                    "type": "text",
                                    "text": text_val,
                                    "attributes": dict(default_attrs),
                                }
                            ],
                        }
                    )

        if extra_images:
            for extra_ref in extra_images:
                uploaded_id = self.upload_image(extra_ref, base_dir=base_dir)
                blocks.append(
                    {
                        "id": str(uuid.uuid4()),
                        "type": "image",
                        "imageAttribute": {
                            "imageId": str(uploaded_id),
                        },
                    }
                )

        return {"blocks": blocks}

    def create_post_from_items(
        self,
        board: str,
        title: str,
        body_items: List[Tuple[str, str]],
        base_dir: Optional[Path] = None,
        extra_images: Optional[List[str]] = None,
    ) -> str:
        """
        글 발행:
        - 액션 ID: 403124fa9b0adc7ba42c71ac4d738e7b78c62e47bc (createPostAction)
        - 헤더: Accept: text/x-component, Content-Type: text/plain;charset=UTF-8
        - 성공 시 최종 글 URL만 반환
        """
        self.check_membership()
        board_category_id = self.resolve_board_id(board)
        block_content = self.build_blocks_from_items(body_items, base_dir=base_dir, extra_images=extra_images)

        action_url = self._cafe_url("posts/new")
        safe_title = title.strip()[:48].rstrip()
        payload = [
            {
                "cafeSlug": self.slug,
                "title": safe_title,
                "blockContent": block_content,
                "visibility": "public",
                "searchable": True,
                "boardCategoryId": board_category_id,
            }
        ]
        body = self.request_server_action(action_url, self.post_action_id, payload)
        chunks = self.parse_rsc_flight_response(body)

        post_url: Optional[str] = None
        post_slug: Optional[str] = None

        for chunk in chunks:
            if isinstance(chunk, dict):
                if chunk.get("success") is False or chunk.get("error"):
                    err_msg = str(chunk.get("error") or chunk.get("message") or chunk)
                    if "요청이 너무 많아요" in err_msg:
                        raise DaangnCafeError(f"[제한 초과] {err_msg}", EXIT_RATE_LIMIT_FAILED)
                    raise DaangnCafeError(f"[글 발행 거부] {err_msg}")
                post_slug = post_slug or chunk.get("postSlug") or chunk.get("id") or chunk.get("postId")
                post_url = post_url or chunk.get("url") or chunk.get("postUrl") or chunk.get("href")
                if chunk.get("success") is True and not post_slug and not post_url:
                    post_url = self._cafe_url("posts")

        if post_slug and not post_url:
            post_url = self._cafe_url(f"posts/{post_slug}")

        if not post_url:
            redirect_match = re.search(r"NEXT_REDIRECT;[^;]*;([^;]+);", body)
            if redirect_match:
                redirect_target = redirect_match.group(1).strip()
                post_url = redirect_target if redirect_target.startswith("http") else f"{self.base_url}{redirect_target}"

        if post_url and not post_url.startswith("http"):
            post_url = f"{self.base_url}/{post_url.lstrip('/')}"

        if not post_url:
            safe_preview = mask_sensitive(body[:300], self.cookie)
            raise DaangnCafeError(
                f"[발행 확인 실패] 응답에서 최종 게시글 URL을 확인할 수 없습니다 (응답: {safe_preview}). "
                f"액션 ID({self.post_action_id}) 변경 여부를 확인해 주세요.",
                EXIT_ACTION_ID_CHANGED,
            )

        return post_url


def parse_manuscript_items(manuscript_path: Path) -> Tuple[str, List[Tuple[str, str]], Optional[str]]:
    """
    원고 파일 파싱 규격:
    - 첫 줄: `제목: [제목내용]`
    - 선택 줄: `게시판: [게시판이름 또는 ID]` (지정 시 해당 게시판 우선 적용)
    - 그 아래 `## 본문` 줄은 제외하고, 텍스트 문단과 이미지(`[이미지: 경로/URL]` 또는 `![](경로/URL)`)의
      순서를 그대로 유지하여 반환합니다.
    """
    if not manuscript_path.exists():
        raise DaangnCafeError(f"[원고 오류] 원고 파일을 찾을 수 없습니다: {manuscript_path}")

    raw_text = manuscript_path.read_text(encoding="utf-8-sig")
    lines = raw_text.splitlines()
    if not lines:
        raise DaangnCafeError(f"[원고 오류] 원고 파일이 비어 있습니다: {manuscript_path}")

    first_line = lines[0].strip()
    if not first_line.startswith("제목:"):
        raise DaangnCafeError(
            f"[원고 형식 오류] 첫 줄은 반드시 '제목: [제목내용]' 형식이어야 합니다. (현재 첫 줄: {first_line})"
        )

    title = first_line[len("제목:") :].strip()
    if not title:
        raise DaangnCafeError("[원고 형식 오류] 제목 내용이 비어 있습니다.")

    board_override: Optional[str] = None
    items: List[Tuple[str, str]] = []
    has_image = False

    md_img_pattern = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
    tag_img_pattern = re.compile(r"^\[(?:이미지|IMAGE)\s*:\s*([^\]]+)\]$", re.IGNORECASE)

    for line in lines[1:]:
        stripped = line.strip()
        if not stripped or stripped == "## 본문":
            continue

        if stripped.startswith("게시판:") and not items:
            board_override = stripped[len("게시판:") :].strip()
            continue

        tag_match = tag_img_pattern.match(stripped)
        if tag_match:
            img_ref = tag_match.group(1).strip()
            items.append(("image", img_ref))
            has_image = True
            continue

        md_matches = list(md_img_pattern.finditer(line))
        cleaned_line = md_img_pattern.sub("", line).strip()
        if cleaned_line:
            items.append(("text", cleaned_line))
        for m in md_matches:
            items.append(("image", m.group(1).strip()))
            has_image = True

    if not has_image:
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            candidate = manuscript_path.with_suffix(ext)
            if candidate.exists():
                items.append(("image", str(candidate.resolve())))

    if not any(t == "text" for t, _ in items):
        raise DaangnCafeError("[원고 형식 오류] 본문 텍스트가 비어 있습니다.")

    return title, items, board_override


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daangn_cafe.py",
        description="당근카페 무인 자동 발행 CLI (수정/삭제 미지원, 쿠키 비노출 보안 적용)",
    )
    parser.add_argument(
        "--slug",
        required=True,
        help="대상 당근카페 slug 식별자",
    )
    parser.add_argument(
        "--env",
        default=None,
        help=".env 파일 경로 (기본값: 스크립트와 동일한 폴더의 .env)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True, help="실행할 명령")
    subparsers.add_parser("membership", help="카페 연결 및 회원 상태(super_host) 확인")
    subparsers.add_parser("boards", help="카페 게시판 목록 확인")

    post_parser = subparsers.add_parser("post_create", help="원고 파일을 읽어 카페 게시글 발행")
    post_parser.add_argument(
        "--manuscript",
        required=True,
        help="발행할 원고 파일 경로 (첫 줄 '제목: ...', '## 본문' 제외)",
    )
    post_parser.add_argument(
        "--board",
        required=True,
        help="발행할 대상 게시판 ID 또는 이름",
    )
    post_parser.add_argument(
        "--images",
        nargs="*",
        default=[],
        help="추가로 첨부할 이미지 파일 경로 목록 (선택)",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    env_path = Path(args.env).resolve() if args.env else None

    try:
        client = DaangnCafeClient(slug=args.slug, env_path=env_path)

        if args.command == "membership":
            info = client.check_membership()
            print(json.dumps(info, ensure_ascii=False))
            return EXIT_SUCCESS

        elif args.command == "boards":
            boards = client.list_boards()
            print(json.dumps(boards, ensure_ascii=False, indent=2))
            return EXIT_SUCCESS

        elif args.command == "post_create":
            manuscript_path = Path(args.manuscript).resolve()
            title, body_items, board_override = parse_manuscript_items(manuscript_path)
            target_board = board_override or args.board

            final_url = client.create_post_from_items(
                board=target_board,
                title=title,
                body_items=body_items,
                base_dir=manuscript_path.parent,
                extra_images=args.images,
            )
            print(final_url)
            return EXIT_SUCCESS

        else:
            parser.print_help()
            return EXIT_GENERAL_ERROR

    except DaangnCafeError as e:
        cookie_val = os.environ.get("DAANGN_COOKIE", "")
        safe_msg = mask_sensitive(str(e), cookie_val)
        print(safe_msg, file=sys.stderr)
        return e.exit_code
    except Exception as e:
        cookie_val = os.environ.get("DAANGN_COOKIE", "")
        safe_msg = mask_sensitive(f"[예기치 않은 오류] {e}", cookie_val)
        print(safe_msg, file=sys.stderr)
        return EXIT_GENERAL_ERROR


if __name__ == "__main__":
    sys.exit(main())
