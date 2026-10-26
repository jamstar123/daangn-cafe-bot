#!/usr/bin/env bash
# ==============================================================================
# cron_runner.sh
# 클라우드 서버 crontab 24시간 무인 구동 래퍼 스크립트
#
# 기능:
# 1. COOKIE_EXPIRED.lock 잠금 파일 존재 시 실행하지 않고 즉시 종료
# 2. manuscripts/ 폴더에서 가장 오래된(또는 이름순 첫 번째) 원고 1건 선택
# 3. daangn_cafe.py post_create 실행
# 4. 성공(Exit 0) 시 최종 글 URL을 published_urls.log에 기록하고 원고를 manuscripts/published/로 이동
# 5. 쿠키 만료(Exit 2) 시:
#    - 스케줄러(crontab)에서 자동 발행 잡을 주석 처리하여 즉시 중단
#    - COOKIE_EXPIRED.lock 및 ALERT_NEW_COOKIE_REQUIRED.txt 생성하여 새 쿠키 요청
# ==============================================================================

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

# 1. 이미 쿠키 만료로 중단된 상태인지 확인
if [[ -f "${LOCK_FILE}" ]]; then
    exit 0
fi

# 2. .env에서 DAANGN_CAFE_SLUG, DAANGN_BOARD_ID만 안전하게 읽기 (DAANGN_COOKIE는 절대 echo/export 하지 않음)
if [[ ! -f "${ENV_FILE}" ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [오류] .env 파일이 존재하지 않습니다." >> "${ERROR_LOG}"
    exit 1
fi

CAFE_SLUG=$(grep -E '^DAANGN_CAFE_SLUG=' "${ENV_FILE}" | head -n 1 | cut -d'=' -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
BOARD_ID=$(grep -E '^DAANGN_BOARD_ID=' "${ENV_FILE}" | head -n 1 | cut -d'=' -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")

if [[ -z "${CAFE_SLUG}" || -z "${BOARD_ID}" ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [오류] .env에 DAANGN_CAFE_SLUG 또는 DAANGN_BOARD_ID가 설정되지 않았습니다." >> "${ERROR_LOG}"
    exit 1
fi

# 3. 발행할 원고 파일 탐색 (.txt 또는 .md)
NEXT_MANUSCRIPT=$(find "${MANUSCRIPTS_DIR}" -maxdepth 1 -type f \( -name "*.txt" -o -name "*.md" \) | sort | head -n 1)

if [[ -z "${NEXT_MANUSCRIPT}" ]]; then
    # 대기 중인 원고가 없으면 조용히 종료
    exit 0
fi

# 4. 글 발행 실행 (표준 출력은 최종 URL, 표준 에러는 에러 로그)
TEMP_ERR=$(mktemp)
POST_URL=$(python3 "${BASE_DIR}/daangn_cafe.py" --slug "${CAFE_SLUG}" post_create --manuscript "${NEXT_MANUSCRIPT}" --board "${BOARD_ID}" 2>"${TEMP_ERR}")
EXIT_CODE=$?
ERR_CONTENT=$(cat "${TEMP_ERR}")
rm -f "${TEMP_ERR}"

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

if [[ ${EXIT_CODE} -eq 0 ]]; then
    # 성공 시 최종 글 URL만 출력 및 기록, 원고 이동
    echo "${POST_URL}"
    echo "[${TIMESTAMP}] ${POST_URL} (원고: $(basename "${NEXT_MANUSCRIPT}"))" >> "${URL_LOG}"
    mv "${NEXT_MANUSCRIPT}" "${PUBLISHED_DIR}/"
    exit 0
elif [[ ${EXIT_CODE} -eq 2 ]]; then
    # 쿠키 만료 또는 super_host 자격 상실 -> 스케줄러 즉시 중단 및 새 쿠키 요청
    echo "[${TIMESTAMP}] [쿠키 만료 중단] ${ERR_CONTENT}" >> "${ERROR_LOG}"
    touch "${LOCK_FILE}"
    cat <<EOF > "${ALERT_FILE}"
========================================================================
[긴급 알림] 당근 세션 쿠키가 만료되어 자동 발행 스케줄러가 중단되었습니다.
발생 시각: ${TIMESTAMP}
사유: ${ERR_CONTENT}

[조치 방법]
1. 새 당근 세션 쿠키를 발급받아 서버의 ${ENV_FILE} 내 DAANGN_COOKIE 값을 갱신하세요.
2. 잠금 파일을 삭제하세요: rm -f ${LOCK_FILE} ${ALERT_FILE}
3. 스케줄러를 다시 활성화하세요: bash ${BASE_DIR}/setup_cloud.sh --resume-cron
========================================================================
EOF
    # 현재 사용자의 crontab에서 cron_runner.sh 라인을 자동 제거하여 스케줄러 중지
    if command -v crontab >/dev/null 2>&1; then
        crontab -l 2>/dev/null | grep -v "cron_runner.sh" | crontab -
    fi
    exit 2
else
    # 기타 오류 (액션 ID 변경(3), 요청 제한 초과(4), 일반 오류(1))
    echo "[${TIMESTAMP}] [실패 코드 ${EXIT_CODE}] ${ERR_CONTENT} (원고: $(basename "${NEXT_MANUSCRIPT}"))" >> "${ERROR_LOG}"
    exit ${EXIT_CODE}
fi
