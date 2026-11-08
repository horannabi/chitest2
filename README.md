# cgv-watch

CGV 에서 지정한 날짜에 특정 영화(기본: 치이카와)의 **오전 회차**가 뜨는지 지켜보고,
뜨면 텔레그램으로 알립니다. 감시 지점은 텔레그램 명령으로 추가/제거합니다.

씨네큐용 `chitest` 와 운영 방식은 같습니다 (텔레그램 명령, `/ok` 전까지 반복 알림,
상태 파일 유지, 집중 구간). 다른 점은 아래와 같습니다.

| | 씨네큐 | CGV |
|---|---|---|
| 조회 | HTML + 좌석 배치도 | JSON API, 잔여석이 응답에 포함 |
| 시간대 | 전체 | 오전만 (`/morning` 으로 조정) |
| 지점 | 1곳 | 여러 곳, `/add` `/remove` |
| 접속 | requests | **`curl_cffi`(크롬 TLS 흉내)** — CGV 는 일반 요청을 403 으로 막음 |

## 접속 가능 여부 먼저 확인

CGV 는 일반 `requests`/`curl` 접속을 막습니다 (GitHub Actions 에서도, 집 IP 에서도 403).
집 IP(macOS)에서는 `curl_cffi` 로 지점 목록과 시간표 조회가 되는 것을 확인했습니다.
다른 환경(클라우드 VM, Actions)에서는 아래 진단으로 되는 방식을 먼저 확인하세요.

```bash
pip install -r requirements.txt playwright
playwright install chromium     # browser 방식까지 진단할 때만
python probe.py                 # 결과는 probe.txt
```

GitHub Actions 에서는 `.github/workflows/probe.yml` 을 수동 실행하면 됩니다.
`curl_cffi` 줄에 `OK` 가 있으면 기본 설정(`FETCHER=curl`)으로 되고,
`browser(...)` 줄만 `OK` 면 `FETCHER=browser` 로 쓰세요.
전부 막히면 한국 IP 의 VM/PC 에서 돌리세요.

## 설정

`.env.example` 을 `.env` 로 복사해서 채웁니다. 환경변수로 직접 줘도 됩니다.

| 변수 | 설명 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather 에서 받은 토큰 |
| `TELEGRAM_CHAT_ID` | 본인 chat id. 다른 대화방의 명령은 무시 |
| `KEYWORDS` | 영화 제목 키워드(쉼표). 기본 `치이카와` |
| `SITES` | **처음 한 번만** 쓰는 기본 지점. 이후는 `/add` `/remove` 가 우선 |
| `MORNING_END` | **처음 한 번만** 쓰는 기본값 `12:00`. 이후는 `/morning` 이 우선 |
| `TARGET_DATE` | 감시 날짜. 비우고 `/watch` 로 지정해도 됨 |
| `FETCHER` | `curl`(기본, curl_cffi) 또는 `browser`(Playwright 크롬, 무겁지만 대안) |
| `HEADLESS` | `browser` 일 때만. `0` 이면 크롬 창을 띄움 (헤드리스가 막힐 때) |
| `POLL_SEC` / `BURST_SEC` / `BURST_WINDOWS` | 조회 간격 / 집중 간격 / 집중 구간(KST, 예 `09:50-10:20`) |
| `MODE` | `until`(기본, 지정일까지 매일) / `onday`(당일만) |

## 실행

```bash
pip install -r requirements.txt
cp .env.example .env     # 열어서 텔레그램 토큰/chat id 등을 채운다
python watch.py
```

`FETCHER=browser` 로 쓸 때만 추가로 `pip install playwright` 와 `playwright install chromium` 이 필요합니다.

상태는 `state.json` 에 저장되어 다시 띄워도 이어집니다.

## 텔레그램 명령

```
/watch 20261111   이 날짜 감시 시작
/sites            감시 중인 지점
/sites 강          지점 이름 검색 (✔ 는 이미 감시 중)
/add 강남, 강변    지점 추가 (이름 또는 번호, 쉼표로 여러 개)
/remove 강남      지점 제거
/morning 12:00    이 시각 이전에 시작하는 회차만 (기본 12:00)
/morning off      시간 제한 없이 하루 전체
/now              찾는 것만 바로 확인   (/now 20261111 처럼 날짜 지정 가능)
/nowall           감시 지점의 그날 전체 회차
/status           현재 상태
/ok               알림 확인 (보낼 때까지 반복해서 알림)
/seatwatch on|off 찾은 뒤에도 새 회차/빈자리를 계속 지켜보기
/trace on 30      30분 동안 매 조회 결과를 한 줄씩 보고
/stop             감시 중지
```

이름이 여러 곳과 맞으면(예: `/add 압구정` 처럼 씨네드쉐프 지점이 따로 있는 경우)
후보 목록을 보여 주니 번호로 다시 추가하세요.

## 알림 동작

- **찾았습니다**: 키워드와 시간대가 맞는 회차가 처음 나타났을 때. 지점별 회차와 잔여석을 보냅니다.
- `/seatwatch on` 이면 이후에도 **회차 추가**(새 회차, 새로 추가한 지점 포함)와
  **자리가 났습니다**(매진이던 회차에 잔여석 발생)를 알립니다.
- `/seatwatch off`(기본)면 한 번 찾고 감시를 끝냅니다.
- 어느 경우든 `/ok` 를 보낼 때까지 `ALERT_REPEAT_SEC` 간격으로 다시 보냅니다.
- 모든 지점 조회가 `FAIL_ALERT_CYCLES`(기본 10)번 연속 실패하면 한 번 알리고,
  복구되면 다시 알립니다.

## 테스트

```bash
python -m unittest discover -s tests -v
```

네트워크 없이 도는 단위 테스트입니다 (오전 경계, 지점 검색, 알림 판단, 명령 처리).

## 메모

- 시간표 API: `GET /api/v1/booking/searchMovScnInfo?coCd=A420&siteNo=<지점번호>&scnYmd=<날짜>`.
  브라우저에서 서명 헤더 없이 응답하는 것을 확인했습니다. `rtctlScopCd` 는 값을 바꿔도
  결과가 같아서 시간대 필터는 `scnsrtTm`(시작시각)으로 직접 합니다.
- "오전" 은 시작시각이 `MORNING_END` 미만인 회차입니다. CGV 의 브런치(12시대 시작)는 제외됩니다.
- 시간표가 아직 열리지 않은 날짜는 빈 목록이 오므로 "아직 없습니다" 로 처리됩니다.
