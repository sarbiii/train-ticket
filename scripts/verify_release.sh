#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m py_compile ticket_web.py ktx_booking.py srt_booking.py test_ticket_web.py
python3 -m pytest -q

python3 - <<'PY'
from pathlib import Path
html = Path("templates/ticket_web.html").read_text()
start = html.index("<script>") + len("<script>")
end = html.index("</script>", start)
Path("/tmp/ticket_web.js").write_text(html[start:end])
PY
node --check /tmp/ticket_web.js
rm -f /tmp/ticket_web.js

if [ "$(uname -s)" = "Darwin" ]; then
  ./build_macos.sh
else
  python3 -m PyInstaller --clean --noconfirm ticket_web.spec
fi

log_file="$(mktemp)"
./dist/ticket-sniper >"$log_file" 2>&1 &
pid=$!
cleanup() {
  pkill -P "$pid" 2>/dev/null || true
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  rm -f "$log_file"
}
trap cleanup EXIT

base_url=""
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:8188/api/status" >/dev/null 2>&1; then
    base_url="http://127.0.0.1:8188"
  else
    base_url="$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk '/ticket-sn/ && /127[.]0[.]0[.]1:[0-9]+/ { sub(/.*127[.]0[.]0[.]1:/, "", $0); sub(/ .*/, "", $0); print "http://127.0.0.1:" $0; exit }')"
  fi
  if [ -z "$base_url" ]; then
    base_url="$(sed -n 's/.*\(http:\/\/127\.0\.0\.1:[0-9][0-9]*\).*/\1/p' "$log_file" | tail -1)"
  fi
  if [ -n "$base_url" ] && curl -fsS "${base_url}/api/status" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if [ -z "$base_url" ]; then
  cat "$log_file" >&2
  echo "failed to detect local app URL" >&2
  exit 1
fi

curl -fsS "${base_url}/api/status" | python3 -m json.tool >/dev/null
curl -fsS -X DELETE "${base_url}/api/credentials" | python3 -m json.tool >/dev/null
curl -fsS "${base_url}/privacy/clear" | rg "trainTicketLoginV1|저장된 로그인 정보" >/dev/null

echo "release verification ok"
