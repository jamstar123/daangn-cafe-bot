#!/usr/bin/env bash
set -u

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${BASE_DIR}/.env"
LOCK_FILE="${BASE_DIR}/COOKIE_EXPIRED.lock"
ALERT_FILE="${BASE_DIR}/ALERT_NEW_COOKIE_REQUIRED.txt"
LOG_DIR="${BASE_DIR}/logs"
URL_LOG="${LOG_DIR}/published_urls.log"
ERROR_LOG="${LOG_DIR}/error.log"

mkdir -p "${LOG_DIR}"

if [[ -f "${LOCK_FILE}" ]]; then
    echo "쿠키 만료 잠금 파일(COOKIE_EXPIRED.lock)이 존재하여 실행을 건너뜁니다."
    exit 0
fi

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

git config --global user.name "github-actions[bot]"
git config --global user.email "github-actions[bot]@users.noreply.github.com"
echo "[${TIMESTAMP}] 24시간 실시간 핫딜 감시 데몬을 시작합니다 (주기 30초, 세션 5.5시간)..."

pip install -q curl_cffi beautifulsoup4 || true

# 30초마다 상시 실시간 감시 (한 세션당 5.5시간 = 19,800초 지속)
python3 "${BASE_DIR}/realtime_deal_monitor.py" \
    --slug "${HOTDEAL_SLUG}" \
    --board "${HOTDEAL_BOARD}" \
    --daemon \
    --interval 30 \
    --max-runtime 19800
EXIT_CODE=$?

TIMESTAMP_END=$(date '+%Y-%m-%d %H:%M:%S')

if [[ ${EXIT_CODE} -eq 0 ]]; then
    echo "[${TIMESTAMP_END}] 5.5시간 실시간 감시 세션이 안전하게 종료되었습니다. 다음 세션으로 자동 인계합니다."
    
    # 깃 변경사항 저장
    git add logs/ || true
    git commit -m "Auto: 실시간 감시 세션 상태 저장" || true
    git push || true

    # 다음 5.5시간 실시간 세션을 즉시 클라우드에 자동 트리거 (24시간 무중단 연속 감시)
    GH_TOKEN="${GH_PAT:-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}"
    curl -s -X POST \
        -H "Authorization: token ${GH_TOKEN}" \
        -H "Accept: application/vnd.github.v3+json" \
        https://api.github.com/repos/jamstar123/daangn-cafe-bot/actions/workflows/397122068/dispatches \
        -d '{"ref":"main"}' || true
    exit 0
elif [[ ${EXIT_CODE} -eq 2 ]]; then
    echo "[${TIMESTAMP_END}] [쿠키 만료 중단]" >> "${ERROR_LOG}"
    touch "${LOCK_FILE}"
    cat <<EOF > "${ALERT_FILE}"
[긴급 알림] 당근 세션 쿠키가 만료되어 자동 감시가 중단되었습니다. (${TIMESTAMP_END})
새 쿠키를 발급받아 DAANGN_COOKIE를 갱신한 뒤 COOKIE_EXPIRED.lock 파일을 삭제해 주세요.
EOF
    exit 2
else
    echo "[${TIMESTAMP_END}] [에러 종료 코드 ${EXIT_CODE}]" >> "${ERROR_LOG}"
    exit ${EXIT_CODE}
fi
