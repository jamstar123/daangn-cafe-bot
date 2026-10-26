#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daangn_cafe.py
당근카페 무인 자동 발행 CLI 및 통신 모듈

보안 규칙:
- 당근 세션 쿠키(DAANGN_COOKIE)는 반드시 .env 파일에서만 로드하며,
  절대로 표준 출력(stdout), 에러 출력(stderr), 로그 파일에 노출하지 않습니다.
"""

import argparse
import json
import mimetypes
import os
import re
import sys
import time
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

DEFAULT_POST_ACTION_ID = "403124fa9b0adc7ba42c71ac4d738e7b78c62e47bc"
DEFAULT_IMAGE_ACTION_ID = "40a64c6f3424be009d7225054055934226e1de89b9"
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
            if key not in os.environ:
                os.environ[key] = val
    return env_vars


def mask_sensitive(text: str, cookie: str) -> str:
    """로그나 에러 메시지에서 쿠키 원문이 절대 출력되지 않도록 마스킹합니다."""
    if not text:
        return text
    if cookie and cookie in text:
        text = text.replace(cookie, "[REDACTED_COOKIE]")
    # 일반적인 세션 토큰 패턴 마스킹
    text = re.sub(r"(?i)(cookie\s*[:=]\s*)([^\r\n]+)", r"\1[REDACTED]", text)
    return text


class DaangnCafeError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_GENERAL_ERROR):
        super().__init__(message)
        self.exit_code = exit_code


class DaangnCafeClient:
    def __init__(self, slug: str, env_path: Optional[Path] = None):
        base_dir = Path(__file__).resolve().parent
        self.env_path = env_path or (base_dir / ".env")
        load_env(self.env_path)

        self.slug = slug
        self.cookie = os.environ.get("DAANGN_COOKIE", "").strip()
        if not self.cookie or self.cookie == "YOUR_DAANGN_SESSION_COOKIE_HERE":
            raise DaangnCafeError(
                "[오류] .env 파일에 유효한 DAANGN_COOKIE가 설정되어 있지 않습니다.",
                EXIT_AUTH_EXPIRED,
            )

        self.base_url = os.environ.get("DAANGN_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        self.cafe_path_prefix = os.environ.get("DAANGN_CAFE_PREFIX", "/cafes").rstrip("/")
        self.post_action_id = os.environ.get("DAANGN_POST_ACTION_ID", DEFAULT_POST_ACTION_ID).strip()
        self.image_action_id = os.environ.get("DAANGN_IMAGE_ACTION_ID", DEFAULT_IMAGE_ACTION_ID).strip()
        self.max_retries = int(os.environ.get("DAANGN_MAX_RETRIES", "3"))
        self.rate_limit_wait = int(os.environ.get("DAANGN_RATE_LIMIT_WAIT", "60"))
        self.user_agent = os.environ.get(
            "DAANGN_USER_AGENT",
            "Mozilla/5.0 (Linux; Android 14; SM-S928N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36 DaangnCafeBot/1.0",
        )

    def _cafe_url(self, subpath: str = "") -> str:
        path = f"{self.cafe_path_prefix}/{self.slug}"
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
        """
        지정된 헤더 규격 및 재시도 정책("요청이 너무 많아요" -> 60초 대기, 유한 재시도)을 준수하여
        HTTP 요청을 수행합니다.
        """
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

            # 1) "요청이 너무 많아요" 또는 HTTP 429 처리 (60초 대기 후 최대 max_retries 재시도, 무한 재시도 금지)
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
                    "[인증 만료] 당근 세션 쿠키가 만료되었거나 권한이 없습니다. 새 쿠키를 .env에 설정해 주세요.",
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
                        "당근 서버 배포로 액션 ID가 변경되었는지 재확인해 주세요.",
                        EXIT_ACTION_ID_CHANGED,
                    )

            return status, resp_headers, body_text

    def request_server_action(self, url: str, action_id: str, payload: Any) -> str:
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
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, _, body = self._request("POST", url, data=data, headers=headers, include_cookie=True)
        if status >= 400:
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
            # 일부 RSC 프리픽스(예: I[...], E{...}) 제거 시도
            if raw_json and raw_json[0].isalpha() and len(raw_json) > 1 and raw_json[1] in "[{":
                raw_json = raw_json[1:]
            try:
                parsed_chunks.append(json.loads(raw_json))
            except Exception:
                continue
        return parsed_chunks

    def check_membership(self) -> Dict[str, Any]:
        """
        3. 연결 확인: membership
        자격: 회원 상태 super_host 확인.
        """
        url = self._cafe_url()
        headers = {
            "Accept": "text/x-component, text/html;q=0.9",
        }
        status, _, body = self._request("GET", url, headers=headers, include_cookie=True)
        if status >= 400:
            raise DaangnCafeError(f"[연결 실패] 카페 페이지 조회 실패 (HTTP {status})", EXIT_GENERAL_ERROR)

        # 응답 본문(RSC 페이로드 또는 내장 JSON)에서 role / membership 상태 추출
        is_super_host = "super_host" in body or '"role":"SUPER_HOST"' in body.upper()
        role_match = re.search(r'"(?:role|memberRole|membershipStatus|status)"\s*:\s*"([^"]+)"', body)
        detected_role = "super_host" if is_super_host else (role_match.group(1) if role_match else "unknown")

        if not is_super_host:
            raise DaangnCafeError(
                f"[권한 오류] 현재 계정의 회원 상태가 'super_host'가 아닙니다 (감지된 상태: {detected_role}). "
                "쿠키 만료 또는 권한 변경 여부를 확인해 주세요.",
                EXIT_AUTH_EXPIRED,
            )

        return {
            "slug": self.slug,
            "role": "super_host",
            "status": "active",
        }

    def list_boards(self) -> List[Dict[str, str]]:
        """
        3. 게시판 확인: boards
        카페의 게시판(카테고리) 목록을 조회합니다.
        """
        url = self._cafe_url()
        headers = {
            "Accept": "text/x-component, text/html;q=0.9",
        }
        status, _, body = self._request("GET", url, headers=headers, include_cookie=True)
        if status >= 400:
            raise DaangnCafeError(f"[조회 실패] 게시판 목록 조회 실패 (HTTP {status})", EXIT_GENERAL_ERROR)

        boards: List[Dict[str, str]] = []
        seen_ids = set()

        # RSC / JSON 내 boards 배열 패턴 탐색
        board_array_matches = re.findall(r'"boards"\s*:\s*(\[[^\]]+\])', body)
        for raw_arr in board_array_matches:
            try:
                items = json.loads(raw_arr)
                for item in items:
                    if isinstance(item, dict):
                        b_id = str(item.get("id") or item.get("boardId") or item.get("slug") or "")
                        b_name = str(item.get("name") or item.get("title") or b_id)
                        if b_id and b_id not in seen_ids:
                            seen_ids.add(b_id)
                            boards.append({"id": b_id, "name": b_name})
            except Exception:
                continue

        # 개별 board 객체 정규식 백업 탐색
        if not boards:
            pattern = re.compile(
                r'\{\s*"id"\s*:\s*"([^"]+)"\s*,\s*"name"\s*:\s*"([^"]+)"[^{}]*\}',
            )
            for m in pattern.finditer(body):
                b_id, b_name = m.group(1), m.group(2)
                if b_id not in seen_ids:
                    seen_ids.add(b_id)
                    boards.append({"id": b_id, "name": b_name})

        return boards

    def upload_image(self, image_path: Path) -> str:
        """
        이미지 업로드:
        1) 액션 ID (40a64c6f3424be009d7225054055934226e1de89b9)로 Presigned URL 발급 요청 (POST)
        2) 발급된 Presigned URL로 이미지 바이너리 직접 업로드 (PUT)
        3) 최종 이미지 URL(또는 미디어 키) 반환
        """
        if not image_path.exists():
            raise DaangnCafeError(f"[파일 오류] 첨부할 이미지 파일을 찾을 수 없습니다: {image_path}")

        mime_type, _ = mimetypes.guess_type(str(image_path))
        mime_type = mime_type or "image/jpeg"
        file_size = image_path.stat().st_size

        # 1단계: 이미지 업로드 액션 호출 (POST, Accept: text/x-component, Content-Type: text/plain;charset=UTF-8)
        action_url = self._cafe_url("posts/new")
        payload = [
            {
                "cafeSlug": self.slug,
                "fileName": image_path.name,
                "contentType": mime_type,
                "fileSize": file_size,
            }
        ]
        body = self.request_server_action(action_url, self.image_action_id, payload)
        chunks = self.parse_rsc_flight_response(body)

        upload_url: Optional[str] = None
        final_image_url: Optional[str] = None

        for chunk in chunks:
            if isinstance(chunk, dict):
                upload_url = upload_url or chunk.get("uploadUrl") or chunk.get("presignedUrl") or chunk.get("putUrl")
                final_image_url = (
                    final_image_url
                    or chunk.get("imageUrl")
                    or chunk.get("url")
                    or chunk.get("publicUrl")
                    or chunk.get("fileUrl")
                )
                if isinstance(chunk.get("data"), dict):
                    d = chunk["data"]
                    upload_url = upload_url or d.get("uploadUrl") or d.get("presignedUrl")
                    final_image_url = final_image_url or d.get("imageUrl") or d.get("url") or d.get("publicUrl")

        if not upload_url:
            # 정규식 폴백 추출
            urls = re.findall(r'https?://[^\s"\'<>]+', body)
            for u in urls:
                if "s3" in u or "storage" in u or "upload" in u or "X-Amz-Signature" in u:
                    upload_url = u
                    break
            for u in urls:
                if u != upload_url and any(ext in u.lower() for ext in (".jpg", ".jpeg", ".png", ".webp", "dn.daangn")):
                    final_image_url = u
                    break

        if not upload_url and not final_image_url:
            raise DaangnCafeError(
                f"[이미지 업로드 실패] 업로드 URL을 응답에서 찾을 수 없습니다. "
                f"액션 ID({self.image_action_id}) 변경 여부를 확인해 주세요.",
                EXIT_ACTION_ID_CHANGED,
            )

        # 2단계: Presigned URL이 있는 경우 PUT 요청으로 바이너리 업로드
        if upload_url:
            with open(image_path, "rb") as img_f:
                img_bytes = img_f.read()
            put_headers = {
                "Content-Type": mime_type,
                "Content-Length": str(len(img_bytes)),
            }
            put_status, _, _ = self._request(
                "PUT", upload_url, data=img_bytes, headers=put_headers, include_cookie=False
            )
            if put_status >= 400:
                raise DaangnCafeError(f"[이미지 PUT 실패] 스토리지 업로드 실패 (HTTP {put_status})")

        return final_image_url or upload_url.split("?", 1)[0]

    def create_post(
        self,
        board: str,
        title: str,
        content: str,
        image_urls: Optional[List[str]] = None,
    ) -> str:
        """
        글 발행:
        - 액션 ID: 403124fa9b0adc7ba42c71ac4d738e7b78c62e47bc
        - 헤더: Accept: text/x-component, Content-Type: text/plain;charset=UTF-8
        - 성공 시 최종 글 URL만 반환 (발행된 글 수정/삭제 기능은 일절 지원하지 않음)
        """
        # 사전 자격 확인 (super_host 여부 검증)
        self.check_membership()

        action_url = self._cafe_url("posts/new")
        payload = [
            {
                "cafeSlug": self.slug,
                "boardId": board,
                "title": title,
                "content": content,
                "images": image_urls or [],
            }
        ]
        body = self.request_server_action(action_url, self.post_action_id, payload)
        chunks = self.parse_rsc_flight_response(body)

        post_url: Optional[str] = None
        post_id: Optional[str] = None

        for chunk in chunks:
            if isinstance(chunk, dict):
                if chunk.get("error") or chunk.get("status") == "error":
                    err_msg = str(chunk.get("message") or chunk.get("error"))
                    raise DaangnCafeError(f"[글 발행 거부] {err_msg}")
                post_url = post_url or chunk.get("url") or chunk.get("postUrl") or chunk.get("redirectUrl")
                post_id = post_id or chunk.get("id") or chunk.get("postId") or chunk.get("articleId")
                if isinstance(chunk.get("data"), dict):
                    d = chunk["data"]
                    post_url = post_url or d.get("url") or d.get("postUrl")
                    post_id = post_id or d.get("id") or d.get("postId")

        # Next.js Server Action redirect 헤더 또는 본문 내 NEXT_REDIRECT 추출
        if not post_url:
            redirect_match = re.search(r"NEXT_REDIRECT;[^;]*;([^;]+);", body)
            if redirect_match:
                redirect_target = redirect_match.group(1).strip()
                if redirect_target.startswith("http"):
                    post_url = redirect_target
                else:
                    post_url = f"{self.base_url}{redirect_target}"

        if not post_url and post_id:
            post_url = self._cafe_url(f"posts/{post_id}")

        if not post_url:
            # 본문 내 게시글 URL 패턴 탐색
            url_match = re.search(
                rf"(https?://[^\s\"'<>]*{re.escape(self.slug)}/posts/[^\s\"'<>]+)", body
            )
            if url_match:
                post_url = url_match.group(1)

        if not post_url:
            raise DaangnCafeError(
                f"[발행 확인 실패] 응답에서 최종 게시글 URL을 확인할 수 없습니다. "
                f"액션 ID({self.post_action_id}) 변경 여부를 확인해 주세요.",
                EXIT_ACTION_ID_CHANGED,
            )

        return post_url


def parse_manuscript(manuscript_path: Path) -> Tuple[str, str, List[Path]]:
    """
    원고 파일 파싱 규격:
    - 첫 줄: `제목: [제목내용]`
    - 그 아래 `## 본문` 줄은 제외하고 순수 텍스트만 발행
    - 원고 내 이미지 문법(`![alt](경로)` 또는 `[이미지: 경로]`)이나 동일 파일명 이미지 자동 감지 후 업로드 목록 반환
    """
    if not manuscript_path.exists():
        raise DaangnCafeError(f"[원고 오류] 원고 파일을 찾을 수 없습니다: {manuscript_path}")

    raw_text = manuscript_path.read_text(encoding="utf-8-sig")
    lines = raw_text.splitlines()
    if not lines:
        raise DaangnCafeError(f"[원고 오류] 원고 파일이 비어 있습니다: {manuscript_path}")

    # 첫 줄 제목 파싱
    first_line = lines[0].strip()
    if not first_line.startswith("제목:"):
        raise DaangnCafeError(
            f"[원고 형식 오류] 첫 줄은 반드시 '제목: [제목내용]' 형식이어야 합니다. (현재 첫 줄: {first_line})"
        )

    title = first_line[len("제목:") :].strip()
    if not title:
        raise DaangnCafeError("[원고 형식 오류] 제목 내용이 비어 있습니다.")

    body_lines: List[str] = []
    image_paths: List[Path] = []
    base_dir = manuscript_path.parent

    md_img_pattern = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
    tag_img_pattern = re.compile(r"^\[(?:이미지|IMAGE)\s*:\s*([^\]]+)\]$", re.IGNORECASE)

    for line in lines[1:]:
        stripped = line.strip()
        # `## 본문` 줄은 제외
        if stripped == "## 본문":
            continue

        # [이미지: path/to/img.jpg] 단독 라인 처리
        tag_match = tag_img_pattern.match(stripped)
        if tag_match:
            img_ref = tag_match.group(1).strip()
            resolved = (base_dir / img_ref).resolve() if not Path(img_ref).is_absolute() else Path(img_ref)
            image_paths.append(resolved)
            continue

        # 마크다운 이미지 ![alt](path) 추출 및 본문에서 태그 제거
        for m in md_img_pattern.finditer(line):
            img_ref = m.group(1).strip()
            resolved = (base_dir / img_ref).resolve() if not Path(img_ref).is_absolute() else Path(img_ref)
            image_paths.append(resolved)

        cleaned_line = md_img_pattern.sub("", line)
        body_lines.append(cleaned_line)

    # 원고와 동일한 이름의 이미지 파일(예: post1.txt -> post1.jpg / post1.png)이 같은 폴더에 있으면 자동 첨부
    if not image_paths:
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            candidate = manuscript_path.with_suffix(ext)
            if candidate.exists():
                image_paths.append(candidate.resolve())

    content = "\n".join(body_lines).strip()
    if not content:
        raise DaangnCafeError("[원고 형식 오류] 본문 텍스트가 비어 있습니다.")

    return title, content, image_paths


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

    # 1. 연결 확인: membership
    subparsers.add_parser("membership", help="카페 연결 및 회원 상태(super_host) 확인")

    # 2. 게시판 확인: boards
    subparsers.add_parser("boards", help="카페 게시판 목록 확인")

    # 3. 글 발행: post_create
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
            title, content, image_paths = parse_manuscript(manuscript_path)

            for extra_img in args.images:
                p = Path(extra_img).resolve()
                if p not in image_paths:
                    image_paths.append(p)

            uploaded_urls: List[str] = []
            for img_path in image_paths:
                uploaded_url = client.upload_image(img_path)
                uploaded_urls.append(uploaded_url)

            final_url = client.create_post(
                board=args.board,
                title=title,
                content=content,
                image_urls=uploaded_urls,
            )
            # 규칙 4: 발행에 성공하면 최종 글 URL만 반환할 것.
            print(final_url)
            return EXIT_SUCCESS

        else:
            parser.print_help()
            return EXIT_GENERAL_ERROR

    except DaangnCafeError as e:
        # 에러 메시지에서도 혹시 모를 쿠키 노출 원천 차단
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
