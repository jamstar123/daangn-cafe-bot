#!/usr/bin/env bash
set -u

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${BASE_DIR}/.env"
MANUSCRIPTS_DIR="${BASE_DIR}/manuscripts"
PUBLISHED_DIR="${MANUSCRIPTS_DIR}/published"
LOG_DIR="${BASE_DIR}/logs"
URL_LOG="${LOG_DIR}/published_urls.log"
ERROR_LOG="${LOG_DIR}/error.log"

mkdir -p "${PUBLISHED_DIR}" "${LOG_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
    set -a
    source "${ENV_FILE}" 2>/dev/null || true
    set +a
fi

CAFE_SLUG="${DAANGN_CAFE_SLUG:-oneul-ui-jeotan}"
BOARD_ID="${DAANGN_BOARD_ID:-free}"

# 원고 파일 탐색
NEXT_MANUSCRIPT=$(find "${MANUSCRIPTS_DIR}" -maxdepth 1 -type f \( -name "*.txt" -o -name "*.md" \) | sort | head -n 1)

if [[ -z "${NEXT_MANUSCRIPT}" ]]; then
    echo "대기 중인 새 원고가 없습니다."
    exit 0
fi

echo "원고 발행 시작: $(basename "${NEXT_MANUSCRIPT}") (게시판: ${BOARD_ID})"
POST_URL=$(python3 "${BASE_DIR}/daangn_cafe.py" --slug "${CAFE_SLUG}" post_create --manuscript "${NEXT_MANUSCRIPT}" --board "${BOARD_ID}")
EXIT_CODE=$?

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

if [[ ${EXIT_CODE} -eq 0 ]]; then
    echo "발행 성공 URL: ${POST_URL}"
    echo "[${TIMESTAMP}] ${POST_URL} (원고: $(basename "${NEXT_MANUSCRIPT}"))" >> "${URL_LOG}"
    mv "${NEXT_MANUSCRIPT}" "${PUBLISHED_DIR}/"
    exit 0
else
    echo "[${TIMESTAMP}] [실패 코드 ${EXIT_CODE}]" >> "${ERROR_LOG}"
    exit ${EXIT_CODE}
fi
