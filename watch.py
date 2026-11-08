#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CGV 에서 지정한 날짜의 특정 영화(키워드)가 뜨는지 주기적으로 조회하고,
뜨면 텔레그램으로 알린다. 감시 지점은 텔레그램 명령(/add /remove)으로 바꾼다.

씨네큐용 watch.py 와 같은 운영 방식(텔레그램 명령, /ok 반복 알림, 상태 파일 유지)이고
다른 점은 세 가지다.
  1) 조회가 HTML 이 아니라 CGV JSON API. 잔여석이 응답에 들어 있어 좌석 조회가 필요 없다.
  2) 오전 회차만 본다 (/morning 으로 시각 조정).
  3) 감시 지점이 여러 곳이고 /add /remove 로 바꾼다.

설정은 환경변수 또는 같은 폴더의 .env 파일에서 읽는다. (README 참고)
"""

import os
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

import cgv
from bot import State, Telegram, handle_command, parse_date, WDAY, morning_text

KST = timezone(timedelta(hours=9))
SITE_URL = "https://cgv.co.kr/cnm/movieBook/cinema"


# ---------------------------------------------------------------- 유틸

def log(msg):
    print("[%s] %s" % (datetime.now(KST).strftime("%m-%d %H:%M:%S"), msg), flush=True)


def die(msg, code=2):
    log("오류: " + msg)
    sys.exit(code)


def load_dotenv(path=".env"):
    """간단한 .env 로더. 이미 있는 환경변수는 덮어쓰지 않는다."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            os.environ.setdefault(k, v)


def env(name, default=None):
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def env_int(name, default):
    try:
        return int(env(name) or default)
    except ValueError:
        return default


def env_float(name, default):
    try:
        return float(env(name) or default)
    except ValueError:
        return default


def env_bool(name, default="0"):
    return (env(name, default) or "").lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- 설정

def parse_windows(spec):
    """'09:50-10:20,13:00-13:10' -> [(분, 분), ...] (자정 기준 분 단위)."""
    out = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$", chunk)
        if not m:
            log("BURST_WINDOWS 항목을 해석하지 못해 무시합니다: %r" % chunk)
            continue
        out.append((int(m.group(1)) * 60 + int(m.group(2)),
                    int(m.group(3)) * 60 + int(m.group(4))))
    return out


class Config:
    def __init__(self):
        self.keywords = [k.strip() for k in (env("KEYWORDS", "치이카와") or "").split(",") if k.strip()]
        if not self.keywords:
            die("KEYWORDS 가 비어 있습니다.")

        # 처음 한 번만 쓰이는 기본값. 이후에는 텔레그램 명령으로 바꾸고 state.json 이 우선이다.
        self.sites_spec = [s.strip() for s in (env("SITES", "") or "").split(",") if s.strip()]
        raw = env("MORNING_END", "12:00")
        self.morning_end = None if (raw or "").lower() in ("off", "all", "0") else cgv.parse_clock(raw)
        if raw and raw.lower() not in ("off", "all", "0") and self.morning_end is None:
            die("MORNING_END 형식이 잘못되었습니다: %r (예: 12:00)" % raw)

        raw_date = env("TARGET_DATE")
        self.target = None
        if raw_date:
            self.target = parse_date(raw_date)
            if self.target is None:
                die("TARGET_DATE 형식이 잘못되었습니다: %r (예: 20261115)" % raw_date)

        self.label = env("LABEL", "CGV")
        # until = 오늘부터 지정일까지 매일 감시 / onday = 지정일 당일에만 감시
        self.mode = (env("MODE", "until") or "until").lower()

        # 한 바퀴(모든 지점 한 번씩)를 도는 간격. 지점이 많을수록 요청이 늘어난다.
        self.poll_sec = env_int("POLL_SEC", 30)
        self.burst_sec = env_int("BURST_SEC", 10)
        self.burst_windows = parse_windows(env("BURST_WINDOWS", ""))
        self.site_gap = env_float("SITE_GAP_SEC", 0.7)     # 지점 사이 간격
        self.idle_sec = env_int("IDLE_SEC", 5)
        self.repeat_sec = env_int("ALERT_REPEAT_SEC", 30)  # /ok 전까지 다시 보내는 간격
        self.fail_alert_cycles = env_int("FAIL_ALERT_CYCLES", 10)

        self.fetcher = env("FETCHER", "curl")
        self.headless = env_bool("HEADLESS", "1")
        self.chrome_channel = env("CHROME_CHANNEL")

        self.tg_token = env("TELEGRAM_BOT_TOKEN")
        self.tg_chat = env("TELEGRAM_CHAT_ID")
        self.force = env_bool("FORCE")
        self.notify_start = env_bool("NOTIFY_START")


# ---------------------------------------------------------------- 지점 목록 캐시

class Catalog:
    """전체 지점 목록. 1시간 캐시하고, 조회가 안 되면 파일에 남긴 마지막 목록을 쓴다."""

    def __init__(self, fetcher, path="catalog_cache.json", ttl=3600):
        self.fetcher = fetcher
        self.path = path
        self.ttl = ttl
        self._data = None
        self._at = 0.0

    def get(self):
        import json
        if self._data and time.time() - self._at < self.ttl:
            return self._data
        try:
            self._data = cgv.fetch_catalog(self.fetcher)
            self._at = time.time()
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False)
            except OSError:
                pass
            return self._data
        except Exception as e:                       # noqa: BLE001
            log("지점 목록 조회 실패: %s" % str(e)[:120])
            if self._data:
                return self._data
            try:
                with open(self.path, encoding="utf-8") as f:
                    self._data = json.load(f)
                self._at = time.time() - self.ttl + 300      # 5분 뒤 다시 시도
                log("저장해 둔 지점 목록을 대신 씁니다.")
                return self._data
            except (OSError, ValueError):
                raise


# ---------------------------------------------------------------- 표시

def slot_seat_text(s):
    if s["free"] is None:
        return ""
    if s["free"] == 0:
        return " 매진"
    return " 잔여 %d/%s" % (s["free"], s["total"] if s["total"] is not None else "?")


def format_slots(slots):
    """지점 -> 영화 -> 회차 순으로 묶어서 보여준다."""
    lines = []
    sites = []
    for s in slots:
        if s["site_name"] not in sites:
            sites.append(s["site_name"])
    for site in sites:
        lines.append("▪ CGV %s" % site)
        titles = []
        for s in slots:
            if s["site_name"] == site and s["title"] not in titles:
                titles.append(s["title"])
        for t in titles:
            lines.append("   %s" % t)
            rows = sorted((s for s in slots if s["site_name"] == site and s["title"] == t),
                          key=lambda x: (x["start_min"] if x["start_min"] is not None else 9999))
            for s in rows:
                lines.append("     %s~%s  %s%s" % (s["start"], s["end"], s["hall"], slot_seat_text(s)))
    return lines


def head_line(cfg, title, target):
    return "%s - %s\n날짜 %s (%s)\n" % (cfg.label, title, target.strftime("%Y%m%d"),
                                      WDAY[target.weekday()])


def build_message(cfg, state, target, slots, title="찾았습니다!"):
    lines = [head_line(cfg, title, target), "(%s)" % morning_text(state), ""]
    lines.extend(format_slots(slots))
    lines.extend(["", SITE_URL])
    return "\n".join(lines)


# ---------------------------------------------------------------- 판단 (순수 로직)

def process(state, matched, ymd):
    """
    이번 조회에서 키워드/시간대가 맞는 회차(matched)로 알림이 필요한지 판단한다.
    state 의 announced / known_slots / zero_slots 를 갱신한다.

    반환: (알림 목록 [(종류, 회차들)], 감시를 끝내야 하는지)
      종류: 'found' 처음 발견 / 'added' 회차 추가 / 'opened' 매진이던 회차에 자리 남
    """
    keys = {s["key"] for s in matched}

    if not state.announced:
        if not matched:
            return [], False
        state.announced = ymd
        state.known_slots |= keys
        state.zero_slots = {s["key"] for s in matched if s["free"] == 0}
        return [("found", list(matched))], not state.seatwatch

    # 이미 알렸다. 계속 지켜보는 설정이 꺼졌으면 여기서 끝낸다.
    if not state.seatwatch:
        return [], True

    new = [s for s in matched if s["key"] not in state.known_slots]
    new_keys = {s["key"] for s in new}
    opened = [s for s in matched
              if s["key"] in state.zero_slots and s["key"] not in new_keys
              and (s["free"] or 0) > 0]

    state.known_slots |= keys
    for s in matched:
        if s["free"] == 0:
            state.zero_slots.add(s["key"])
        elif s["free"] is not None and s["free"] > 0:
            state.zero_slots.discard(s["key"])

    alerts = []
    if new:
        alerts.append(("added", new))
    if opened:
        alerts.append(("opened", opened))
    return alerts, False


def should_watch(cfg, target, now):
    """지금 이 시점에 target 날짜를 감시해야 하는지. (감시여부, 사유)"""
    if target is None:
        return False, "지정된 날짜가 없습니다. 텔레그램에서 /watch 20261111 로 지정하세요."
    if cfg.force:
        return True, "FORCE=1 이라 날짜 조건을 무시합니다."
    today = now.date()
    if today > target:
        return False, "지정일(%s)이 이미 지났습니다." % target.strftime("%Y%m%d")
    if cfg.mode == "onday" and today != target:
        return False, ("오늘은 지정일(%s)이 아닙니다. MODE=onday 라 대기합니다."
                       % target.strftime("%Y%m%d"))
    return True, "날짜 %s 감시 중 (D-%d)" % (target.strftime("%Y%m%d"), (target - today).days)


def interval_for(cfg, now):
    minute = now.hour * 60 + now.minute
    for a, b in cfg.burst_windows:
        if a <= minute <= b:
            return cfg.burst_sec
    return cfg.poll_sec


# ---------------------------------------------------------------- 조회

def collect(state, fetcher, target, gap=0.0, pump=None):
    """
    감시 지점 전부의 그날 회차를 모은다. 반환: (회차 리스트, 실패한 지점 리스트).
    지점 사이에 pump() 를 불러 텔레그램 명령이 밀리지 않게 한다.
    """
    ymd = target.strftime("%Y%m%d")
    slots, failed = [], []
    for site in list(state.sites):
        if pump:
            pump()
        if site["no"] not in state.site_nos():       # pump 중에 /remove 된 지점
            continue
        try:
            rows = cgv.fetch_schedule(fetcher, site["no"], ymd)
        except cgv.FetchError as e:
            log("%s 조회 실패: %s" % (site["name"], e))
            failed.append(site)
            continue
        for r in rows:                                # 응답에 지점명이 없으면 우리 이름을 쓴다
            if not r["site_name"]:
                r["site_name"] = site["name"]
        slots.extend(rows)
        if gap:
            time.sleep(gap)
    return slots, failed


def snapshot(cfg, state, fetcher, target, show_all=False, pump=None):
    """지금 한 번 조회해서 사람이 읽을 문자열로 돌려준다. (/now, /nowall)"""
    if target is None:
        return "지정된 날짜가 없습니다. /watch 20261111 처럼 지정하세요."
    if not state.sites:
        return "감시 지점이 없습니다. /add 강남 처럼 추가하세요."
    slots, failed = collect(state, fetcher, target, gap=cfg.site_gap, pump=pump)
    ymd = target.strftime("%Y%m%d")
    note = ("\n※ 조회 실패: %s" % ", ".join(s["name"] for s in failed)) if failed else ""
    if failed and not slots:
        return "조회에 실패했습니다. 잠시 후 다시 시도하세요." + note

    if show_all:
        if not slots:
            return "날짜 %s\n회차가 없습니다. 아직 시간표가 열리지 않았을 수 있습니다.%s" % (ymd, note)
        return "\n".join(["날짜 %s / 전체 %d회차" % (ymd, len(slots)), ""] + format_slots(slots)) + note

    matched = [s for s in slots if cgv.keep_slot(s, cfg.keywords, state.morning_end)]
    if not matched:
        any_kw = [s for s in slots if cgv.keep_slot(s, cfg.keywords, None)]
        hint = ""
        if any_kw:
            hint = "\n(%s 는 %d회차 있지만 %s 가 아닙니다)" % (
                "/".join(cfg.keywords), len(any_kw), morning_text(state))
        return ("날짜 %s\n아직 없습니다. (%d곳 전체 %d회차)%s\n전체를 보려면 /nowall%s"
                % (ymd, len(state.sites) - len(failed), len(slots), hint, note))
    return "\n".join(["날짜 %s / %d건" % (ymd, len(matched)), ""] + format_slots(matched)) + note


# ---------------------------------------------------------------- 알림

def raise_alert(state, tg, text):
    """
    알림을 걸어둔다. /ok 를 받을 때까지 계속 다시 보낸다.
    확인 안 된 알림이 있으면 덮어쓰지 않고 뒤에 이어 붙인다.
    """
    entry = "[%s] %s" % (datetime.now(KST).strftime("%H:%M"), text)
    if state.ack_date is not None and state.ack_text:
        state.ack_text = state.ack_text + "\n\n" + ("-" * 20) + "\n" + entry
    else:
        state.ack_text = entry
    state.ack_date = state.target or state.ack_date or datetime.now(KST).date()
    state.ack_count = 1
    state.ack_last = datetime.now(KST)
    state.save("alert")
    tg.send(state.ack_text + "\n\n확인하셨으면 /ok 를 보내주세요.")


def resend_alert(cfg, state, tg):
    """미확인 알림이 있으면 간격에 맞춰 다시 보낸다."""
    if state.ack_date is None:
        return
    now = datetime.now(KST)
    if state.ack_last and (now - state.ack_last).total_seconds() < cfg.repeat_sec:
        return
    state.ack_count += 1
    state.ack_last = now
    tg.send("%s\n\n아직 확인하지 않으셨습니다. (%d번째)\n확인하셨으면 /ok 를 보내주세요."
            % (state.ack_text or "(알림 본문 없음)", state.ack_count))
    if state.ack_count % 10 == 0:
        state.save("ack count")


# ---------------------------------------------------------------- 지점 기본값

def seed_sites(cfg, state, catalog):
    """처음 실행(state 에 지점 목록이 없음)일 때만 SITES 환경변수로 채운다."""
    if state.has_sites_key or not cfg.sites_spec:
        return
    try:
        cat = catalog.get()
    except Exception as e:                           # noqa: BLE001
        log("SITES 를 해석하지 못했습니다(지점 목록 조회 실패): %s" % str(e)[:120])
        return
    names = []
    for q in cfg.sites_spec:
        found = cgv.find_sites(cat, q)
        if len(found) == 1:
            if state.add_site(found[0]):
                names.append(found[0]["name"])
        else:
            log("SITES 항목 %r: %s" % (q, "맞는 지점 없음" if not found else
                                       "여러 곳과 맞음(번호로 지정하세요): " +
                                       ", ".join(c["no"] + " " + c["name"] for c in found[:6])))
    state.save("seed sites")
    log("기본 지점 %d곳: %s" % (len(state.sites), ", ".join(names) or "없음"))


# ---------------------------------------------------------------- main

def main():
    load_dotenv()
    cfg = Config()
    start = datetime.now(KST)

    tg = Telegram(cfg.tg_token, cfg.tg_chat)
    state = State(default_morning=cfg.morning_end)
    fetcher = cgv.make_fetcher(cfg.fetcher, headless=cfg.headless, channel=cfg.chrome_channel)
    catalog = Catalog(fetcher)

    stop = [False]

    def on_signal(_signum, _frame):
        stop[0] = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, on_signal)

    if state.target is None and cfg.target is not None and not state.announced:
        state.target = cfg.target
        state.save("init from env")
    seed_sites(cfg, state, catalog)

    log("기동 완료 / 키워드 %s / 지점 %d곳 / %s / 조회기 %s"
        % ("·".join(cfg.keywords), len(state.sites), morning_text(state), cfg.fetcher))
    log("간격 평상시 %d초, 집중 %d초, 집중구간 %s"
        % (cfg.poll_sec, cfg.burst_sec, cfg.burst_windows or "없음"))
    if not tg.enabled:
        log("경고: 텔레그램이 설정되지 않아 명령 수신이 불가합니다.")

    checks = 0
    backoff = 0
    fail_cycles = 0
    fail_alerted = False
    last_reason = None
    last_summary = None
    target_changed = [False]

    def status_text():
        _ok, why = should_watch(cfg, state.target, datetime.now(KST))
        up = int((datetime.now(KST) - start).total_seconds() // 60)
        out = [why,
               "지점 %d곳: %s" % (len(state.sites), ", ".join(s["name"] for s in state.sites) or "없음"),
               "대상: %s / %s" % ("·".join(cfg.keywords), morning_text(state)),
               "조회 %d회 / 실행 %d분째 / 조회기 %s" % (checks, up, cfg.fetcher)]
        if state.announced:
            out.append("첫 발견 알림: 완료")
        if state.seatwatch:
            out.append("계속 지켜보기: 켜짐")
        if state.ack_date is not None:
            out.append("미확인 알림 있음 (%d번 보냄). /ok 로 멈춥니다." % state.ack_count)
        if fail_cycles:
            out.append("최근 %d회 연속 조회 실패 중" % fail_cycles)
        return "\n".join(out)

    def pump():
        """쌓인 텔레그램 명령을 처리한다. 루프 맨 앞과 지점 조회 사이사이에 부른다."""
        for update_id, text in tg.poll(state.tg_offset):
            state.tg_offset = update_id
            if text is not None:
                log("명령 수신")
                if handle_command(
                        text, state, tg, status_text,
                        lambda d, allitems=False: snapshot(cfg, state, fetcher, d, allitems),
                        catalog.get):
                    target_changed[0] = True
            state.save("offset")

    if cfg.notify_start:
        tg.send("감시 프로세스가 올라왔습니다.\n" + status_text())

    try:
        while not stop[0]:
            pump()
            if target_changed[0]:
                target_changed[0] = False
                checks = 0
                last_summary = None
            resend_alert(cfg, state, tg)

            target = state.target
            watching, reason = should_watch(cfg, target, datetime.now(KST))

            if not watching or not state.sites:
                if state.ack_date is not None:
                    time.sleep(min(cfg.repeat_sec, cfg.idle_sec))
                    continue
                why = reason if not watching else "감시 지점이 없습니다. /add 강남 처럼 추가하세요."
                if why != last_reason:
                    log(why + " 대기 모드로 텔레그램 명령만 기다립니다.")
                    last_reason = why
                time.sleep(cfg.idle_sec)
                continue

            if reason != last_reason:
                log(reason)
                last_reason = reason

            # ---- 조회
            checks += 1
            ymd = target.strftime("%Y%m%d")
            slots, failed = collect(state, fetcher, target, gap=cfg.site_gap, pump=pump)
            if target_changed[0] or state.target != target:
                continue                              # 조회 도중 날짜가 바뀌었다. 결과는 버린다

            live = state.site_nos()
            slots = [s for s in slots if s["site_no"] in live]
            failed_nos = {s["no"] for s in failed}
            ok_sites = len([s for s in state.sites if s["no"] not in failed_nos])

            if state.sites and ok_sites <= 0:
                fail_cycles += 1
                backoff = min(120, (backoff * 2) if backoff else 15)
                log("모든 지점 조회 실패(%d회 연속). %d초 후 재시도합니다." % (fail_cycles, backoff))
                if fail_cycles >= cfg.fail_alert_cycles and not fail_alerted:
                    fail_alerted = True
                    tg.send("CGV 조회가 %d번 연속 실패하고 있습니다.\n"
                            "접속이 막혔을 수 있습니다. 계속 재시도합니다." % fail_cycles)
                time.sleep(backoff)
                continue
            backoff = 0
            if fail_alerted:
                tg.send("CGV 조회가 다시 됩니다.")
            fail_cycles = 0
            fail_alerted = False

            matched = [s for s in slots if cgv.keep_slot(s, cfg.keywords, state.morning_end)]
            alerts, finish = process(state, matched, ymd)

            for kind, group in alerts:
                if kind == "found":
                    log("일치 회차 %d건 발견. 알림을 보냅니다." % len(group))
                    text = build_message(cfg, state, target, group, "찾았습니다!")
                elif kind == "added":
                    log("회차 추가 %d건" % len(group))
                    text = build_message(cfg, state, target, group, "회차가 추가됐습니다!")
                else:
                    log("빈자리 발생 %d건" % len(group))
                    text = build_message(cfg, state, target, group, "자리가 났습니다!")
                raise_alert(state, tg, text)          # 여기서 state 가 같이 저장된다
                if kind == "found" and not finish:
                    tg.send("이어서 새 회차와 빈자리를 지켜봅니다. 끄려면 /seatwatch off")

            if finish:
                state.target = None                   # 감시는 끝. 확인 받을 때까지 재전송만 한다
                state.save("done")
            elif not alerts:                          # 알림이 있었으면 raise_alert 가 이미 저장했다
                state.save("progress")

            if state.trace_on():
                tg.send("[trace] %s · %d회 · 전체 %d회차 · 일치 %d건%s"
                        % (datetime.now(KST).strftime("%H:%M:%S"), checks, len(slots), len(matched),
                           (" · 실패 %d곳" % len(failed)) if failed else ""))
            elif state.trace_until is not None:
                state.trace_until = None
                state.save("trace expired")
                tg.send("trace 시간이 끝나 종료했습니다. 감시는 계속됩니다.")

            summary = (len(slots), len(matched))
            if summary != last_summary:
                log("현재 전체 %d회차 / 일치 %d건" % summary)
                last_summary = summary
            elif checks % 30 == 0:
                log("감시 중... %d회 조회, 변동 없음 (전체 %d회차 / 일치 %d건)" % ((checks,) + summary))

            if not finish:
                # 대기 중에도 명령에 빨리 반응하도록 잘게 쪼개서 잔다
                wait = interval_for(cfg, datetime.now(KST))
                end = time.time() + wait
                while time.time() < end and not stop[0]:
                    time.sleep(min(2.0, max(0.0, end - time.time())))
                    pump()
                    if target_changed[0]:
                        break
    finally:
        log("종료합니다.")
        fetcher.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
