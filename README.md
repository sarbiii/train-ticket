# KTX / SRT 취소표 스나이퍼

KTX와 SRT 취소표를 로컬 웹 화면에서 조회하고, 선택한 열차를 반복 조회해 좌석이 나오면 예약을 시도하는 데스크톱용 도구입니다.

## 주요 기능

- KTX / SRT 열차 조회
- 웹 화면에서 Korail 또는 SRT 로그인
- 선택한 열차 대상 반복 조회
- 일반석/특실 선호 옵션
- 예약대기 시도 옵션
- 실시간 로그
- 예약 성공 시 브라우저 알림 및 비프음
- macOS / Windows 실행 파일 빌드

## 보안 정책

- 외부 접속을 열지 않습니다.
- 앱은 `127.0.0.1` 로만 실행됩니다.
- `.env` 파일을 사용하지 않습니다.
- 로그인 정보는 웹 화면에서 입력하고 실행 중인 프로세스 메모리에만 보관합니다.
- `/api/state` 응답과 실행 로그에는 비밀번호를 저장하지 않습니다.

## 실행

개발 실행:

```bash
python3 -m pip install -r requirements.txt
python3 ticket_web.py
```

실행하면 브라우저가 자동으로 열립니다. 열리지 않으면 터미널에 표시되는 `http://127.0.0.1:8188` 주소로 접속하세요.

## macOS 실행 파일 만들기

```bash
./build_macos.sh
./dist/ticket-sniper
```

Apple Silicon 환경에서는 `arm64` Python을 우선 사용합니다.

## Windows 실행 파일 만들기

PowerShell에서 실행:

```powershell
.\build_windows.ps1
.\dist\ticket-sniper.exe
```

PyInstaller는 Windows 실행 파일을 Windows 환경에서 빌드해야 합니다.

## GitHub Actions 빌드

`main` 브랜치에 푸시하면 GitHub Actions가 다음 산출물을 만듭니다.

- `ticket-sniper-macos`
- `ticket-sniper-windows`

Actions 화면의 해당 workflow run에서 Artifacts를 다운로드하면 됩니다.

## 테스트

```bash
python3 -m py_compile ticket_web.py ktx_booking.py srt_booking.py test_ticket_web.py
python3 -m pytest -q
```

템플릿 JavaScript 문법 확인:

```bash
sed -n '/<script>/,/<\/script>/p' templates/ticket_web.html | sed '1d;$d' > /tmp/ticket_web.js
node --check /tmp/ticket_web.js
```

## 사용 순서

1. 실행 파일 또는 `python3 ticket_web.py`로 앱을 실행합니다.
2. KTX 또는 SRT를 선택합니다.
3. 로그인 ID와 비밀번호를 입력합니다.
4. 출발역, 도착역, 날짜, 시간 범위를 입력하고 열차를 조회합니다.
5. 스나이핑할 열차를 선택합니다.
6. 좌석 선호, 예약대기 여부, 조회 간격을 선택합니다.
7. `스나이핑 시작`을 누릅니다.
8. 예약 성공 시 브라우저 알림과 화면의 예약 정보를 확인합니다.

예약만 시도하며 결제는 Korail 또는 SRT 공식 앱/웹에서 직접 완료해야 합니다.

## 주의

- 과도하게 짧은 조회 간격은 서비스 차단이나 오류를 유발할 수 있습니다.
- 실제 예약 성공 여부는 공식 서비스 상태, 로그인 상태, 잔여 좌석, 네트워크 상태에 영향을 받습니다.
- 이 도구는 로컬 개인 실행을 전제로 합니다.
