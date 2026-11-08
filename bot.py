#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
텔레그램 명령 수신 + 감시 상태 보관.

명령
    /watch 20261115   감시할 날짜 지정 (1115, 11-15, 11/15 도 허용)
    /sites            지금 감시 중인 지점
    /add 강남, 강변    지점 추가 (이름 또는 번호, 쉼표로 여러 개)
    /remove 강남      지점 제거
    /morning 12:00    이 시각 이전에 시작하는 회차만 (off 면 하루 전체)
    /status /now /nowall /ok /seatwatch /trace /stop /help

상태는 state.json 에 보관한다. 프로세스를 다시 띄워도 이어간다.
"""

import json
import os
import re
import tempfile
from datetime import datetime, date, timedelta, timezone

import requests

import cgv

KST = timezone(timedelta(hours=9))
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

WDAY = ("월", "화", "수", "목", "금", "토", "일")

HELP = """사용법

/watch 20261115   이 날짜에 뜨면 알림
/ok               알림 확인 (이걸 보낼 때까지 반복해서 보냅니다)

[지점]
/sites            지금 감시 중인 지점
/sites 강남        지점 검색 (감시 대상 아님, 이름만 찾아봄)
/add 강남, 강변    지점 추가 (이름 또는 번호, 쉼표로 여러 개)
/remove 강남      지점 제거

[시간대]
/morning          지금 설정 보기
/morning 12:00    12:00 이전에 시작하는 회차만 (기본)
/morning off      시간 제한 없이 하루 전체

[조회]
/now              찾는 것만 바로 확인
/nowall           감시 지점의 그날 전체 회차 확인
/status           현재 감시 상태
/seatwatch on     찾은 뒤에도 새 회차/자리가 나는지 계속 지켜보기
/seatwatch off    끄기
/trace on 30      30분 동안 매 조회마다 한 줄씩 보고 (off 로 끄기)
/stop             감시 중지
/help             이 도움말

/now 20261107 처럼 날짜를 직접 줄 수도 있습니다.
날짜는 20261115 형식입니다. (1115 처럼 월일만 보내도 됩니다)"""


def log(msg):
    print("[%s] %s" % (datetime.now(KST).strftime("%m-%d %H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- 날짜 파싱

def parse_date(text):
    """사용자가 보낸 문자열에서 날짜를 뽑아낸다. 실패하면 None."""
    s = (text or "").strip().replace(".", "-").replace("/", "-")
    today = datetime.now(KST).date()
    year_omitted = False

    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)              # 20261115 (기본)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)    # 2026-11-15
        if m:
            y, mo, d = (int(x) for x in m.groups())
        else:
            m = re.match(r"^(\d{2})(\d{2})$", s)             # 1115
            if not m:
                m = re.match(r"^(\d{1,2})-(\d{1,2})$", s)    # 11-15
            if not m:
                return None
            mo, d = int(m.group(1)), int(m.group(2))
            y = today.year
            year_omitted = True

    try:
        result = date(y, mo, d)
    except ValueError:
        return None

    # 연도를 생략했고 이미 지난 날짜면 내년으로 본다
    if year_omitted and result < today:
        try:
            result = date(y + 1, mo, d)
        except ValueError:
            return None
    return result


# ---------------------------------------------------------------- 상태

class State:
    """감시 상태. state.json 에 저장한다."""

    def __init__(self, path=None, default_morning=720):
        self.path = path or STATE_FILE
        self.target = None          # date | None
        self.tg_offset = 0          # 처리한 텔레그램 update_id
        self.trace_until = None     # datetime | None. 이 시각까지 매 조회를 보고
        self.seatwatch = False      # 찾은 뒤에도 새 회차/자리를 계속 지켜볼지

        self.sites = []             # [{no, name}] 감시 지점
        self.morning_end = default_morning   # 분. None 이면 시간 제한 없음

        # 날짜가 바뀌면 비우는 진행 상황
        self.announced = None       # "YYYYMMDD". 첫 발견을 이미 알린 날짜
        self.known_slots = set()    # 이미 본(알린) 회차 키
        self.zero_slots = set()     # 매진(0석)으로 확인된 회차 키. 자리가 나면 알린다

        # 읽음 확인(/ok) 전까지 알림을 반복한다
        self.ack_date = None
        self.ack_count = 0
        self.ack_text = None
        self.ack_last = None        # 메모리 전용

        # state.json 에 지점 목록이 한 번이라도 저장됐는지. 아니면 환경변수 SITES 로 시드한다.
        self.has_sites_key = False
        self.load(default_morning)

    def trace_on(self):
        return self.trace_until is not None and datetime.now(KST) <= self.trace_until

    # ---- 읽기

    def load(self, default_morning):
        data = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:
                log("state 읽기 실패(새로 시작): %s" % e)

        raw = data.get("target_date")
        if raw:
            self.target = parse_date(str(raw))
        self.tg_offset = int(data.get("tg_offset") or 0)
        self.seatwatch = bool(data.get("seatwatch"))

        self.has_sites_key = "sites" in data
        self.sites = [{"no": str(s["no"]), "name": str(s["name"])}
                      for s in data.get("sites") or []]
        if "morning_end" in data:
            self.morning_end = data["morning_end"]       # None 허용
        else:
            self.morning_end = default_morning

        self.announced = data.get("announced") or None
        self.known_slots = set(str(k) for k in (data.get("known_slots") or []))
        self.zero_slots = set(str(k) for k in (data.get("zero_slots") or []))

        raw = data.get("ack_date")
        self.ack_date = parse_date(str(raw)) if raw else None
        self.ack_count = int(data.get("ack_count") or 0)
        self.ack_text = data.get("ack_text") or None

        raw = data.get("trace_until")
        if raw:
            try:
                self.trace_until = datetime.strptime(
                    str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            except ValueError:
                self.trace_until = None

    # ---- 쓰기 (임시 파일에 쓴 뒤 바꿔치기해서 도중에 끊겨도 파일이 깨지지 않게 한다)

    def save(self, reason=""):
        payload = {
            "target_date": self.target.strftime("%Y%m%d") if self.target else None,
            "tg_offset": self.tg_offset,
            "seatwatch": self.seatwatch,
            "sites": self.sites,
            "morning_end": self.morning_end,
            "announced": self.announced,
            "known_slots": sorted(self.known_slots),
            "zero_slots": sorted(self.zero_slots),
            "ack_date": self.ack_date.strftime("%Y%m%d") if self.ack_date else None,
            "ack_count": self.ack_count,
            "ack_text": self.ack_text,
            "trace_until": (self.trace_until.strftime("%Y-%m-%d %H:%M:%S")
                            if self.trace_until else None),
            "updated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        d = os.path.dirname(os.path.abspath(self.path))
        try:
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            os.replace(tmp, self.path)
        except OSError as e:
            log("state 쓰기 실패: %s" % e)

    # ---- 변경

    def reset_progress(self):
        """감시 날짜가 바뀌었을 때. 직전 날짜에 대한 판단은 전부 버린다."""
        self.announced = None
        self.known_slots = set()
        self.zero_slots = set()

    def site_nos(self):
        return {s["no"] for s in self.sites}

    def add_site(self, site):
        if site["no"] in self.site_nos():
            return False
        self.sites.append({"no": site["no"], "name": site["name"]})
        return True

    def remove_site(self, no):
        """지점을 빼고 그 지점의 진행 기록도 지운다."""
        self.sites = [s for s in self.sites if s["no"] != no]
        prefix = no + "-"
        self.known_slots = {k for k in self.known_slots if not k.startswith(prefix)}
        self.zero_slots = {k for k in self.zero_slots if not k.startswith(prefix)}


# ---------------------------------------------------------------- 텔레그램

class Telegram:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = str(chat_id) if chat_id else None
        self.enabled = bool(token and chat_id)

    def _url(self, method):
        return "https://api.telegram.org/bot%s/%s" % (self.token, method)

    # 텔레그램 메시지 한 통의 최대 길이는 4096자. 여유를 두고 자른다.
    MAX_LEN = 4000

    def send(self, text):
        if not self.enabled:
            log("(텔레그램 미설정) " + text.replace("\n", " / ")[:200])
            return False
        ok = True
        for part in self._split(text):
            ok = self._send_one(part) and ok
        return ok

    def _send_one(self, text):
        try:
            r = requests.post(self._url("sendMessage"),
                              data={"chat_id": self.chat_id, "text": text,
                                    "disable_web_page_preview": "true"}, timeout=20)
        except requests.RequestException as e:
            log("텔레그램 전송 예외: %s" % str(e)[:160])
            return False
        if r.status_code != 200:
            log("텔레그램 전송 실패 %s: %s" % (r.status_code, r.text[:200]))
            return False
        return True

    @classmethod
    def _split(cls, text):
        """길면 줄 경계에서 나눈다. 한 줄이 너무 길면 그 줄은 글자 수로 자른다."""
        if len(text) <= cls.MAX_LEN:
            return [text]
        parts, cur = [], ""
        for line in text.split("\n"):
            while len(line) > cls.MAX_LEN:
                if cur:
                    parts.append(cur)
                    cur = ""
                parts.append(line[:cls.MAX_LEN])
                line = line[cls.MAX_LEN:]
            if cur and len(cur) + 1 + len(line) > cls.MAX_LEN:
                parts.append(cur)
                cur = line
            else:
                cur = (cur + "\n" + line) if cur else line
        if cur:
            parts.append(cur)
        return parts

    def poll(self, offset):
        """새 명령을 [(update_id, 본문), ...] 로 반환. 남의 대화방 메시지는 본문이 None."""
        if not self.enabled:
            return []
        try:
            r = requests.get(self._url("getUpdates"),
                             params={"offset": offset + 1, "timeout": 0, "limit": 20},
                             timeout=20)
        except requests.RequestException as e:
            log("텔레그램 수신 예외: %s" % str(e)[:160])
            return []
        if r.status_code != 200:
            log("텔레그램 수신 실패 %s" % r.status_code)
            return []

        out = []
        for upd in r.json().get("result", []):
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            # 내 대화방 메시지만 처리한다
            if text and chat == self.chat_id:
                out.append((upd["update_id"], text))
            elif text:
                log("다른 대화방(%s) 메시지는 무시합니다." % chat)
                out.append((upd["update_id"], None))
            else:
                out.append((upd["update_id"], None))      # 텍스트 아닌 업데이트도 넘긴다
        return out


# ---------------------------------------------------------------- 명령 처리

def handle_command(text, state, tg, status_fn, now_fn, catalog_fn):
    """
    명령 하나를 처리한다.
    status_fn() -> 상태 문자열
    now_fn(날짜|None, 전체보기) -> 즉시 조회 결과 문자열
    catalog_fn() -> 전체 지점 [{no,name,region}] (조회 실패 시 예외 또는 빈 리스트)
    반환값: 감시 대상 날짜가 바뀌었으면 True
    """
    cmd = text.strip()
    low = cmd.lower()
    head = low.split()[0] if low.split() else ""
    head = head.split("@")[0]                    # /cmd@botname 형태 허용
    arg = cmd[len(cmd.split()[0]):].strip() if cmd.split() else ""

    if head in ("/help", "help", "/start"):
        tg.send(HELP)
        return False

    if head in ("/status", "status"):
        tg.send(status_fn())
        return False

    if head in ("/now", "now", "/nowall", "nowall"):
        d = parse_date(arg) if arg else None
        if arg and d is None:
            tg.send("날짜를 알아듣지 못했습니다: %r\n예) /now 20261107" % arg)
            return False
        tg.send(now_fn(d or state.target, head.lstrip("/") == "nowall"))
        return False

    if head in ("/ok", "ok", "/ack", "ack", "확인"):
        if state.ack_date is None:
            tg.send("반복 중인 알림이 없습니다.")
            return False
        n = state.ack_count
        state.ack_date = None
        state.ack_count = 0
        state.ack_text = None
        state.ack_last = None
        state.save("ack")
        tg.send("확인했습니다. 알림을 멈춥니다. (%d번 보냈습니다)" % n)
        return False

    if head in ("/trace", "trace"):
        return _set_trace(arg, state, tg)

    if head in ("/seatwatch", "seatwatch"):
        on = (arg or "").lower().strip() not in ("off", "0", "stop")
        state.seatwatch = on
        state.save("seatwatch")
        tg.send("찾은 뒤에도 새 회차와 자리가 나는지 계속 지켜봅니다." if on
                else "계속 지켜보기를 껐습니다. 찾으면 알리고 끝냅니다.")
        return False

    if head in ("/sites", "sites", "/site"):
        return _cmd_sites(arg, state, tg, catalog_fn)

    if head in ("/add", "add"):
        return _cmd_add(arg, state, tg, catalog_fn)

    if head in ("/remove", "remove", "/rm", "/del"):
        return _cmd_remove(arg, state, tg)

    if head in ("/morning", "morning"):
        return _cmd_morning(arg, state, tg)

    if head in ("/stop", "stop"):
        state.target = None
        state.reset_progress()
        state.save("stop")
        tg.send("감시를 중지했습니다. 다시 시작하려면 /watch 날짜 를 보내세요.")
        return True

    if head in ("/watch", "watch"):
        d = parse_date(arg)
        if not d:
            tg.send("날짜를 알아듣지 못했습니다: %r\n예) /watch 20261115" % arg)
            return False
        return _set_date(d, state, tg)

    # 명령어 없이 날짜만 보낸 경우도 받아준다
    d = parse_date(cmd)
    if d:
        return _set_date(d, state, tg)

    tg.send("모르는 명령입니다.\n\n" + HELP)
    return False


def site_lines(sites):
    return ["  %s  %s" % (s["no"], s["name"]) for s in sites]


def _load_catalog(catalog_fn, tg):
    try:
        cat = catalog_fn()
    except Exception as e:                       # noqa: BLE001 - 어떤 실패든 사용자에게 알린다
        log("지점 목록 조회 실패: %s" % str(e)[:160])
        cat = None
    if not cat:
        tg.send("CGV 지점 목록을 불러오지 못했습니다. 잠시 후 다시 시도하거나 "
                "번호로 추가해 보세요. 예) /add 0056")
    return cat


def _cmd_sites(arg, state, tg, catalog_fn):
    if not arg:
        if not state.sites:
            tg.send("감시 중인 지점이 없습니다.\n/add 강남 처럼 추가하세요.")
        else:
            tg.send("감시 중인 지점 %d곳\n%s" % (len(state.sites), "\n".join(site_lines(state.sites))))
        return False

    cat = _load_catalog(catalog_fn, tg)
    if not cat:
        return False
    found = cgv.find_sites(cat, arg)
    if not found:
        tg.send("%r 와 맞는 지점이 없습니다." % arg)
        return False
    have = state.site_nos()
    lines = ["%s%s  %s  (%s)" % ("✔ " if c["no"] in have else "  ", c["no"], c["name"], c["region"])
             for c in found[:30]]
    more = "\n… 외 %d곳" % (len(found) - 30) if len(found) > 30 else ""
    tg.send("검색 결과 %d곳 (✔ 는 감시 중)\n%s%s\n\n추가: /add 이름 또는 번호" % (len(found), "\n".join(lines), more))
    return False


def _cmd_add(arg, state, tg, catalog_fn):
    queries = [q.strip() for q in re.split(r"[,\n]", arg) if q.strip()]
    if not queries:
        tg.send("추가할 지점을 적어 주세요.\n예) /add 강남, 강변\n지점 이름은 /sites 강 처럼 검색해 볼 수 있습니다.")
        return False
    cat = _load_catalog(catalog_fn, tg)
    if not cat:
        return False

    added, already, ambiguous, missing = [], [], [], []
    for q in queries:
        found = cgv.find_sites(cat, q)
        if not found:
            missing.append(q)
        elif len(found) > 1:
            ambiguous.append((q, found))
        elif state.add_site(found[0]):
            added.append(found[0])
        else:
            already.append(found[0])

    if added:
        state.save("add sites")
    lines = []
    if added:
        lines.append("추가했습니다: " + ", ".join(s["name"] for s in added))
    if already:
        lines.append("이미 감시 중: " + ", ".join(s["name"] for s in already))
    for q, found in ambiguous:
        lines.append("%r 는 여러 곳과 맞습니다. 번호로 다시 추가해 주세요:" % q)
        lines.extend("  %s  %s (%s)" % (c["no"], c["name"], c["region"]) for c in found[:10])
    if missing:
        lines.append("찾지 못했습니다: " + ", ".join(missing))
    lines.append("")
    lines.append("현재 %d곳: %s" % (len(state.sites), ", ".join(s["name"] for s in state.sites) or "없음"))
    tg.send("\n".join(lines))
    return False


def _cmd_remove(arg, state, tg):
    queries = [q.strip() for q in re.split(r"[,\n]", arg) if q.strip()]
    if not queries:
        tg.send("뺄 지점을 적어 주세요.\n예) /remove 강남\n지금 목록은 /sites")
        return False

    removed, ambiguous, missing = [], [], []
    for q in queries:
        found = cgv.find_sites(state.sites, q)           # 감시 중인 지점 안에서만 찾는다
        if not found:
            missing.append(q)
        elif len(found) > 1:
            ambiguous.append((q, found))
        else:
            state.remove_site(found[0]["no"])
            removed.append(found[0])

    if removed:
        state.save("remove sites")
    lines = []
    if removed:
        lines.append("뺐습니다: " + ", ".join(s["name"] for s in removed))
    for q, found in ambiguous:
        lines.append("%r 는 여러 곳과 맞습니다. 번호로 다시: %s" % (q, ", ".join(c["no"] + " " + c["name"] for c in found)))
    if missing:
        lines.append("감시 목록에 없습니다: " + ", ".join(missing))
    lines.append("")
    lines.append("현재 %d곳: %s" % (len(state.sites), ", ".join(s["name"] for s in state.sites) or "없음"))
    tg.send("\n".join(lines))
    return False


def _cmd_morning(arg, state, tg):
    a = (arg or "").strip().lower()
    if not a:
        tg.send(morning_text(state))
        return False
    if a in ("off", "all", "0", "끔"):
        state.morning_end = None
        state.save("morning off")
        tg.send("시간 제한을 껐습니다. 하루 전체 회차를 봅니다.")
        return False
    m = cgv.parse_clock(a)
    if m is None or m == 0:
        tg.send("시각을 알아듣지 못했습니다: %r\n예) /morning 12:00" % arg)
        return False
    state.morning_end = m
    state.save("morning")
    tg.send("%s 이전에 시작하는 회차만 봅니다." % cgv.fmt_clock(m))
    return False


def morning_text(state):
    if state.morning_end is None:
        return "시간 제한 없음 (하루 전체)"
    return "%s 이전에 시작하는 회차만" % cgv.fmt_clock(state.morning_end)


def _set_trace(arg, state, tg):
    """
    /trace on        5분간 매 조회 보고
    /trace on 30     30분간
    /trace off       끄기
    """
    tokens = (arg or "").lower().split()
    if tokens and tokens[0] in ("off", "stop", "0"):
        state.trace_until = None
        state.save("trace off")
        tg.send("trace 를 껐습니다.")
        return False

    if tokens and tokens[0] in ("on", "start"):
        tokens = tokens[1:]

    minutes = 5
    if tokens:
        if not tokens[0].isdigit():
            tg.send("시간을 알아듣지 못했습니다: %r\n예) /trace on 30" % arg)
            return False
        minutes = max(1, min(180, int(tokens[0])))

    state.trace_until = datetime.now(KST) + timedelta(minutes=minutes)
    state.save("trace on")
    tg.send("trace 를 켰습니다.\n%d분 동안 매 조회마다 한 줄씩 보냅니다.\n끄려면 /trace off" % minutes)
    return False


def _set_date(d, state, tg):
    today = datetime.now(KST).date()
    if d < today:
        tg.send("%s 는 이미 지난 날짜입니다." % d.strftime("%Y%m%d"))
        return False
    state.target = d
    state.reset_progress()
    state.save("watch")
    warn = "" if state.sites else "\n\n※ 감시 지점이 없습니다. /add 강남 처럼 추가하세요."
    tg.send("감시 시작합니다.\n날짜 %s (%s) D-%d\n지점 %d곳 / %s\n뜨는 즉시 알려드릴게요.%s"
            % (d.strftime("%Y%m%d"), WDAY[d.weekday()], (d - today).days,
               len(state.sites), morning_text(state), warn))
    return True
