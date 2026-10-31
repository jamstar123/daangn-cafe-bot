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
HOTDEAL_BOARD="${HOTDEAL_BOARD_ID:-2293417}"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

git config --global user.name "github-actions[bot]"
git config --global user.email "github-actions[bot]@users.noreply.github.com"
echo "[${TIMESTAMP}] 24시간 실시간 핫딜 감시 데몬을 시작합니다 (주기 30초, 세션 5.5시간)..."

pip install -q curl_cffi beautifulsoup4 || true

# 중복 세션 방지: 이미 실행 중인 같은 워크플로 세션이 있으면 이 세션은 바로 종료한다.
# (두 세션이 겹치면 각자 자기 메모리에만 의존해서 같은 상품을 둘 다 올리는 중복 발행이 생긴다.
#  2026-10-31 09:30 딸기쨈 8초 간격 중복 발행이 이 원인으로 확인됨.)
# API 조회가 실패하면(토큰 없음·네트워크 오류) 그냥 진행한다 (fail-open — 감시가 멈추는 게 더 나쁘다).
# 중복으로 종료될 때는 연쇄(dispatch)도 하지 않는다 — 살아남은 세션의 연쇄가 이어간다.
if [[ -n "${GITHUB_TOKEN:-}" && -n "${GITHUB_RUN_ID:-}" ]]; then
    OTHER_RUNNING=$(curl -s --max-time 15 \
        -H "Authorization: token ${GITHUB_TOKEN}" \
        -H "Accept: application/vnd.github.v3+json" \
        "https://api.github.com/repos/jamstar123/daangn-cafe-bot/actions/workflows/auto_post.yml/runs?status=in_progress&per_page=20" \
        | python3 -c "
import json, sys, os
try:
    runs = json.load(sys.stdin).get('workflow_runs', [])
    mine = os.environ.get('GITHUB_RUN_ID', '')
    others = [str(r['id']) for r in runs if str(r['id']) != mine]
    print(' '.join(others))
except Exception:
    print('')
" 2>/dev/null || echo "")
    if [[ -n "${OTHER_RUNNING}" ]]; then
        echo "[중복 세션 방지] 이미 실행 중인 세션(${OTHER_RUNNING})이 있어 이 세션을 종료합니다 (연쇄 없음)."
        exit 0
    fi
fi

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
    # 보안: 토큰을 코드에 직접 적지 않는다. 액션에서는 자동 제공되는 GITHUB_TOKEN을
    # 우선 쓰고, 자체 서버에서 돌릴 때는 GH_PAT 시크릿을 쓴다. 둘 다 없으면 연쇄를
    # 건너뛰고 다음 예약 실행 때 재개된다 (예약 실행만으로는 하루 16.5시간 커버, 빈틈 발생).
    DISPATCH_TOKEN="${GITHUB_TOKEN:-${GH_PAT:-}}"
    if [[ -n "${DISPATCH_TOKEN}" ]]; then
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST \
            -H "Authorization: token ${DISPATCH_TOKEN}" \
            -H "Accept: application/vnd.github.v3+json" \
            https://api.github.com/repos/jamstar123/daangn-cafe-bot/actions/workflows/auto_post.yml/dispatches \
            -d '{"ref":"main"}' || echo "000")
        echo "[연쇄 트리거] 다음 세션 dispatch HTTP ${HTTP_CODE}"
        if [[ "${HTTP_CODE}" != "204" ]]; then
            echo "[${TIMESTAMP_END}] [연쇄 실패] dispatch HTTP ${HTTP_CODE}" >> "${ERROR_LOG}"
            git add "${ERROR_LOG}" || true
            git commit -m "Alert: 다음 세션 자동 인계 실패 (HTTP ${HTTP_CODE})" || true
            git push || true
        fi
    else
        echo "[경고] GITHUB_TOKEN도 GH_PAT도 없어 다음 세션 자동 트리거를 건너뜁니다."
        echo "[${TIMESTAMP_END}] [연쇄 건너뜀] 토큰 없음" >> "${ERROR_LOG}"
        git add "${ERROR_LOG}" || true
        git commit -m "Alert: 다음 세션 자동 인계 건너뜀 (토큰 없음)" || true
        git push || true
    fi
    exit 0
elif [[ ${EXIT_CODE} -eq 2 ]]; then
    echo "[${TIMESTAMP_END}] [쿠키 만료 중단]" >> "${ERROR_LOG}"
    touch "${LOCK_FILE}"
    cat <<EOF > "${ALERT_FILE}"
[긴급 알림] 당근 세션 쿠키가 만료되어 자동 감시가 중단되었습니다. (${TIMESTAMP_END})
새 쿠키를 발급받아 DAANGN_COOKIE를 갱신한 뒤 COOKIE_EXPIRED.lock 파일을 삭제해 주세요.
EOF
    # 액션 러너는 일회용이라 알림 파일이 사라진다. 저장소에 커밋해서 남긴다.
    git add "${ALERT_FILE}" "${ERROR_LOG}" || true
    git commit -m "Alert: 당근 세션 쿠키 만료로 감시 중단" || true
    git push || true
    exit 2
else
    echo "[${TIMESTAMP_END}] [에러 종료 코드 ${EXIT_CODE}]" >> "${ERROR_LOG}"
    exit ${EXIT_CODE}
fi
