#!/usr/bin/env bash
set -euo pipefail

PORT="${1:-8188}"
BASE_URL="http://127.0.0.1:${PORT}"

if curl -fsS "${BASE_URL}/api/status" >/dev/null 2>&1; then
  curl -fsS -X DELETE "${BASE_URL}/api/credentials" >/dev/null
  if command -v open >/dev/null 2>&1; then
    open "${BASE_URL}/privacy/clear"
  else
    printf '브라우저에서 %s/privacy/clear 를 열어 저장 로그인 정보를 삭제하세요.\n' "${BASE_URL}"
  fi
else
  printf '실행 중인 앱을 찾지 못했습니다: %s\n' "${BASE_URL}" >&2
  printf '앱을 실행한 뒤 이 스크립트를 다시 실행하거나, 앱 화면에서 저장 로그인 삭제를 누르세요.\n' >&2
  exit 1
fi

rm -rf build dist __pycache__ .pytest_cache
find . -name '*.pyc' -delete

echo "server credentials cleared; browser clear page opened"
