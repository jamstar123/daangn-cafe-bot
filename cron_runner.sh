#!/usr/bin/env bash
set -u

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${BASE_DIR}/.env"
LOCK_FILE="${BASE_DIR}/COOKIE_EXPIRED.lock"
ALERT_FILE="${BASE_DIR}/ALERT_NEW_COOKIE_REQUIRED.txt"
MANUSCRIPTS_DIR="${BASE_DIR}/manuscripts"
PUBLISHED_DIR="${MANUSCRIPTS_DIR}/published"
LOG_DIR="${BASE_DIR}/logs"
URL_LOG="${LOG_DIR}/published_urls.log"
ERROR_LOG="${LOG_DIR}/error.log"

mkdir -p "${PUBLISHED_DIR}" "${LOG_DIR}"

if [[ -f "${LOCK_FILE}" ]]; then
    echo "쿠키 만료 잠금 파일(COOKIE_EXPIRED.lock)이 존재하여 실행을 건너뜁니다."
    exit 0
fi

# .env 파일이 있으면 읽되, 이미 환경변수(GitHub Secrets 등)로 설정된 값이 있으면 덮어쓰지 않음
if [[ -f "${ENV_FILE}" ]]; then
    while IFS='=' read -r key val; do
        [[ -z "${key}" || "${key}" =~ ^# ]] && continue
        key=$(echo "${key}" | xargs)
        val=$(echo "${val}" | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
        if [[ -z "${!key:-}" ]]; then
            export "${key}=${val}"
        fi
    done < "${ENV_FILE}"
fi

HOTDEAL_SLUG="${HOTDEAL_CAFE_SLUG:-hasdil-eun-ye-1}"
HOTDEAL_BOARD="${HOTDEAL_BOARD_ID:-2293448}"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# 1. [핫딜은 여기] 실시간 조건 충족 핫딜 탐지 & 가짜 할인 필터링 후 즉시 발행
TEMP_ERR=$(mktemp)
HOTDEAL_URL=$(python3 "${BASE_DIR}/realtime_deal_monitor.py" --slug "${HOTDEAL_SLUG}" --board "${HOTDEAL_BOARD}" --once --max-posts 1 2>"${TEMP_ERR}")
EXIT_CODE=$?
ERR_CONTENT=$(cat "${TEMP_ERR}")
rm -f "${TEMP_ERR}"

if [[ ${EXIT_CODE} -eq 0 ]]; then
    if [[ -n "${HOTDEAL_URL}" ]]; then
        echo "${HOTDEAL_URL}"
        echo "[${TIMESTAMP}] [실시간 핫딜] ${HOTDEAL_URL}" >> "${URL_LOG}"
    else
        echo "[${TIMESTAMP}] 현재 기준(역대가/무지성구매급/가격오류급)을 충족한 신규 핫딜이 없어 대기합니다."
    fi
elif [[ ${EXIT_CODE} -eq 2 ]]; then
    echo "${ERR_CONTENT}" >&2
    echo "[${TIMESTAMP}] [쿠키 만료 중단] ${ERR_CONTENT}" >> "${ERROR_LOG}"
    touch "${LOCK_FILE}"
    cat <<EOF > "${ALERT_FILE}"
[긴급 알림] 당근 세션 쿠키가 만료되어 자동 발행 스케줄러가 중단되었습니다. (${TIMESTAMP})
사유: ${ERR_CONTENT}
새 쿠키를 발급받아 DAANGN_COOKIE를 갱신한 뒤 COOKIE_EXPIRED.lock 파일을 삭제해 주세요.
EOF
    if command -v crontab >/dev/null 2>&1; then
        crontab -l 2>/dev/null | grep -v "cron_runner.sh" | crontab - || true
    fi
    exit 2
else
    echo "${ERR_CONTENT}" >&2
    echo "[${TIMESTAMP}] [실시간 핫딜 스캔 알림] ${ERR_CONTENT}" >> "${ERROR_LOG}"
fi

# 2. 수동/예약 원고(manuscripts)가 대기 중이면 함께 발행
CAFE_SLUG="${DAANGN_CAFE_SLUG:-oneul-ui-jeotan}"
BOARD_ID="${DAANGN_BOARD_ID:-2252190}"
NEXT_MANUSCRIPT=$(find "${MANUSCRIPTS_DIR}" -maxdepth 1 -type f \( -name "*.txt" -o -name "*.md" \) | sort | head -n 1)

if [[ -n "${NEXT_MANUSCRIPT}" ]]; then
    TEMP_ERR2=$(mktemp)
    POST_URL=$(python3 "${BASE_DIR}/daangn_cafe.py" --slug "${CAFE_SLUG}" post_create --manuscript "${NEXT_MANUSCRIPT}" --board "${BOARD_ID}" 2>"${TEMP_ERR2}")
    M_EXIT=$?
    M_ERR=$(cat "${TEMP_ERR2}")
    rm -f "${TEMP_ERR2}"
    if [[ ${M_EXIT} -eq 0 ]]; then
        echo "${POST_URL}"
        echo "[${TIMESTAMP}] ${POST_URL} (원고: $(basename "${NEXT_MANUSCRIPT}"))" >> "${URL_LOG}"
        mv "${NEXT_MANUSCRIPT}" "${PUBLISHED_DIR}/"
    fi
fi

exit 0
