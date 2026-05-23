# 최신 작업 요약

- 날짜: 2026-05-23
- 대상: `ticket_web.py`, `templates/ticket_web.html`
- 변경: 검색 결과, 스나이핑 payload, 로그 이력, 마지막 poll, 성공 정보를 서버 메모리 세션에 저장하도록 추가.
- 변경: `/api/state`를 추가해 새로고침 후 브라우저가 기존 검색/실행 상태를 복원하게 함.
- 변경: 프론트엔드에서 페이지 로드 시 상태를 복원하고, 실행 중이면 SSE 스트림에 다시 연결하게 함.
- 검증: `python3 -m py_compile ticket_web.py`, 템플릿 스크립트 `node --check`, Flask test client의 `/api/state` 200 응답 확인.
- 리뷰: `ticket_web.py` 전체를 검토해 `review_2026-05-23.md` 생성. 주요 개선점은 인증 부재, 스나이프 루프 조회 범위 불일치, 전역 세션 공유, 입력 검증 부족, SSE 단일 queue 소비 구조.

## 2026-05-23 추가 작업

- 대상: `app_discord.py`, `test_app_discord.py`, `.env.example`
- 변경: `app_t.py` 기반 디스코드 slash command `/예매` 봇을 별도 파일로 추가.
- 동작: `/예매` 단독 실행 시 양식 안내, `/예매 양식:<양식>` 실행 시 KTX/SRT 출발·도착·날짜·시간 범위 기반 표 조회.
- 보안: `DISCORD_BOT_TOKEN` env 필수, 선택적으로 `DISCORD_GUILD_ID`, `DISCORD_ALLOWED_CHANNEL_IDS`, `DISCORD_ALLOWED_USER_IDS` allowlist 지원.
- 검증: `python3 -m py_compile app_discord.py test_app_discord.py`, `python3 -m unittest test_app_discord.py -v`, `arch -arm64/-x86_64 python3` 양쪽의 `app_discord._require_discord()` 통과.
- 환경 조치: 현재 x86_64 Python에서 arm64 `audioop-lts`가 잡혀 `discord.py` import가 실패해 `python3 -m pip install --force-reinstall --no-cache-dir audioop-lts`로 재설치.
- 후속 수정: universal2 Python에서 arm64/x86_64 실행별로 `audioop-lts` native wheel이 충돌할 수 있어 `app_discord.py`에서 사용하지 않는 discord voice/player 모듈을 stub 처리.
- 재검증: `DISCORD_BOT_TOKEN= python3 app_discord.py`, `DISCORD_BOT_TOKEN= arch -arm64 python3 app_discord.py` 모두 import 오류 없이 토큰 누락 메시지까지 도달.
- 후속 수정2: `VoiceClient.warn_dave` stub 누락과 macOS Python CA 인증서 문제를 수정. `certifi` CA를 `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`에 설정.
- 재검증2: 실제 `.env` 토큰으로 `python3 app_discord.py`, `arch -arm64 python3 app_discord.py` 모두 Discord Gateway 연결 및 `Discord bot ready` 도달.
- 운영: `ticket-discord` tmux 세션 생성. `/Users/sarbii/Desktop/sarbii/ticket`에서 `python3 app_discord.py 2>&1 | tee -a logs/app_discord.log`로 실행 중.
- 후속 수정3: `python3 app_discord.py` 실행 시 tmux 밖이면 `ticket-discord` 세션 생성/attach, tmux 안이면 봇 직접 실행하도록 자동 tmux 진입 추가. 우회는 `APP_DISCORD_NO_TMUX=1`.
- 후속 수정4: `/예매` 단독 입력 안내문을 상세화해 필수/선택 항목, 날짜/시간/좌석/대기/승객/간격 형식과 별칭을 설명.
- 재검증3: 새 코드로 `ticket-discord` 세션 재시작, Discord Gateway 연결, slash command sync, `Discord bot ready` 확인.

## 2026-05-23 app_discord 예약 실행 수정

- 대상: `app_discord.py`, `test_app_discord.py`
- 변경: `/예매 양식:<양식>`이 표 조회에서 끝나지 않고 `app_t.snipe()`를 백그라운드로 실행해 실제 예약/예약대기까지 진행하도록 연결.
- 변경: 양식에 `대상`/`열차선택` 항목을 추가. `all`, `1`, `1,3`, `1-3` 형식으로 스나이핑 대상 선택 가능하며 생략 시 조회된 전체 열차 대상.
- 변경: 예약 루프는 한 번에 하나만 실행되도록 제한하고, 시작 응답에는 조회 표와 대상 개수/좌석/대기/간격을 표시. 성공 시 채널에 예약번호, 열차, 구간, 운임, 결제기한 안내를 전송.
- 검증: `python3 -m py_compile app_discord.py test_app_discord.py`, `python3 -m unittest test_app_discord.py -v` 통과.

## 2026-05-23 app_discord 모달 입력 전환

- 대상: `app_discord.py`, `test_app_discord.py`
- 변경: `/예매` 실행 시 Discord Modal을 열어 열차, 출발역, 도착역, 날짜, 시간범위를 입력받도록 전환.
- 변경: 모달 제출 후 ephemeral 확인 화면에서 좌석 선호만 선택하고 `예약 시작` 버튼으로 실제 `app_t.snipe()` 예약 루프를 시작.
- 변경: 사용자 입력 옵션에서 예약대기, 대상 열차 선택, 조회 간격, 승객 수를 제거. 내부값은 예약대기 OFF, 조회된 전체 열차 대상, 45초 간격, 성인 1명으로 고정.
- 검증: `python3 -m py_compile app_discord.py test_app_discord.py`, `python3 -m unittest test_app_discord.py -v`, `APP_DISCORD_NO_TMUX=1 DISCORD_BOT_TOKEN= python3 app_discord.py` 통과.
- 운영: `ticket-discord` tmux 세션 재시작 후 Discord Gateway 연결, slash command sync, `Discord bot ready` 확인.

## 2026-05-23 app_discord 성인 인원 선택 추가

- 대상: `app_discord.py`, `test_app_discord.py`
- 변경: 모달 제출 후 확인 화면에 `성인 인원` select를 추가해 성인 1~4명 선택 가능하도록 변경. 어린이/경로/유아는 계속 미지원.
- 변경: 예약 요청은 선택한 성인 수를 `app_t.snipe()` 승객 리스트로 전달해 한 예약 건에 묶어 시도.
- 변경: 성인 2명 이상 선택 시 확인 화면과 예약 시작 안내에 "동시에 빈 좌석이 나와야 해서 예약까지 오래 걸릴 수 있음" 경고 표시.
- 검증: `python3 -m py_compile app_discord.py test_app_discord.py`, `python3 -m unittest test_app_discord.py -v`, `APP_DISCORD_NO_TMUX=1 DISCORD_BOT_TOKEN= python3 app_discord.py` 통과.
- 운영: `ticket-discord` tmux 세션 재시작 후 Discord Gateway 연결, slash command sync, `Discord bot ready` 확인.
