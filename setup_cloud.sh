#!/usr/bin/env bash
# ==============================================================================
# setup_cloud.sh
# 클라우드 서버(Oracle Cloud Ubuntu/Oracle Linux 등) 초기 환경 및 crontab 자동 구성 스크립트
#
# 사용법:
#   초기 설치 및 스케줄 등록: bash setup_cloud.sh
#   쿠키 갱신 후 스케줄 재개: bash setup_cloud.sh --resume-cron
# ==============================================================================

set -e

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${BASE_DIR}/.env"
CRON_SCRIPT="${BASE_DIR}/cron_runner.sh"
# 기본 스케줄: 매일 오전 10시, 오후 3시, 저녁 8시 자동 실행 (원하는 주기로 변경 가능)
CRON_SCHEDULE="${DAANGN_CRON_SCHEDULE:-0 10,15,20 * * *}"

mkdir -p "${BASE_DIR}/manuscripts/published" "${BASE_DIR}/logs"

chmod +x "${BASE_DIR}/daangn_cafe.py" "${CRON_SCRIPT}"

if [[ ! -f "${ENV_FILE}" ]]; then
    cp "${BASE_DIR}/.env.example" "${ENV_FILE}"
fi

# 보안: .env 파일은 소유자만 읽고 쓸 수 있도록 권한 600 강제 적용
chmod 600 "${ENV_FILE}"

# --resume-cron 옵션 처리 (쿠키 갱신 후 잠금 해제 및 크론 재등록)
if [[ "${1:-}" == "--resume-cron" ]]; then
    rm -f "${BASE_DIR}/COOKIE_EXPIRED.lock" "${BASE_DIR}/ALERT_NEW_COOKIE_REQUIRED.txt"
fi

# crontab 등록 (기존 cron_runner.sh 항목 중복 방지 후 추가)
CRON_JOB="${CRON_SCHEDULE} /usr/bin/env bash ${CRON_SCRIPT} >> ${BASE_DIR}/logs/cron_stdout.log 2>&1"
if command -v crontab >/dev/null 2>&1; then
    (crontab -l 2>/dev/null | grep -v "cron_runner.sh" || true; echo "${CRON_JOB}") | crontab -
    echo "[완료] crontab 스케줄러가 정상 등록되었습니다: ${CRON_SCHEDULE}"
else
    echo "[경고] crontab 명령어를 찾을 수 없습니다. cron 패키지를 설치해 주세요 (sudo apt-get install -y cron)."
fi
