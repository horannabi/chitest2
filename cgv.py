#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CGV 조회 계층.

  - Fetcher      : cgv.co.kr 의 JSON API 를 부르는 객체. 두 가지 구현
        CurlFetcher     curl_cffi 로 크롬 TLS 지문을 흉내낸다. 가볍고 빠르다. (기본값)
                        집 IP 에서 동작을 확인했다. 데이터센터 IP 에서는 막힐 수 있다.
        BrowserFetcher  실제 크롬(Playwright)을 띄우고 페이지 안에서 fetch 한다.
                        curl_cffi 가 막힐 때의 대안. 무겁지만 브라우저와 똑같이 보인다.
    CGV 는 일반 requests/curl 접속과 GitHub Actions 같은 서버 환경을 403 으로 막는다.

  - 지점 목록(catalog), 시간표 조회, 응답 정규화, 오전/키워드 필터.

API (서명 헤더 없이 응답하는 것을 브라우저에서 확인함)
  GET /api/v1/content/site/searchAllRegionAndSite?coCd=A420
        -> data.siteInfo[{siteNo, siteNm, regnGrpCd}]
  GET /api/v1/booking/searchMovScnInfo?coCd=A420&siteNo=0056&scnYmd=20261111&rtctlScopCd=08
        -> data[{prodNm, movNm, scnsrtTm("1000"), scnendTm, scnsNm, frSeatCnt, stcnt, ...}]
     rtctlScopCd 는 값을 바꿔도 결과가 같아서(전부 내려옴) 시간대 필터는 우리가 직접 건다.
"""

import re
import sys
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

BASE = "https://cgv.co.kr"
ENTRY_PAGE = BASE + "/cnm/movieBook/cinema"
CO_CD = "A420"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def log(msg):
    print("[%s] %s" % (datetime.now(KST).strftime("%m-%d %H:%M:%S"), msg), flush=True)


class FetchError(Exception):
    pass


# ---------------------------------------------------------------- Fetcher

class BrowserFetcher:
    """
    Playwright 로 크롬을 띄워 두고 페이지 컨텍스트 안에서 fetch 를 실행한다.
    쿠키/지문이 진짜 브라우저와 같아서 WAF 에 걸리지 않는다.

    HEADLESS=0 이면 창을 띄운다. 헤드리스가 막히면 이쪽을 쓴다.
    CHROME_CHANNEL=chrome 이면 설치된 구글 크롬을 쓴다. (기본은 Playwright 번들 크롬)
    """

    RELOAD_SEC = 20 * 60        # 쿠키가 낡지 않게 주기적으로 페이지를 다시 연다

    def __init__(self, headless=True, channel=None):
        self.headless = headless
        self.channel = channel
        self._pw = None
        self._browser = None
        self._page = None
        self._opened_at = 0.0
        self.fails = 0

    def _open(self):
        self.close()
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        kw = {"headless": self.headless}
        if self.channel:
            kw["channel"] = self.channel
        self._browser = self._pw.chromium.launch(**kw)
        ctx = self._browser.new_context(user_agent=UA, locale="ko-KR",
                                        timezone_id="Asia/Seoul",
                                        viewport={"width": 1280, "height": 900})
        self._page = ctx.new_page()
        self._goto()

    def _goto(self):
        self._page.goto(ENTRY_PAGE, wait_until="domcontentloaded", timeout=45000)
        self._page.wait_for_timeout(1500)
        self._opened_at = time.time()

    def close(self):
        for obj, fn in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, fn)()
            except Exception:
                pass
        self._pw = self._browser = self._page = None

    def get_json(self, path):
        if self._page is None:
            self._open()
        elif time.time() - self._opened_at > self.RELOAD_SEC:
            try:
                self._goto()
            except Exception as e:
                log("페이지 새로고침 실패: %s" % str(e)[:120])
                self._open()

        for attempt in (1, 2):
            try:
                res = self._page.evaluate(
                    """async (u) => {
                        const ctl = new AbortController();
                        const timer = setTimeout(() => ctl.abort(), 20000);   // 멈춤 방지
                        try {
                            const r = await fetch(u, {credentials: 'include', signal: ctl.signal,
                                                      headers: {'Accept': 'application/json'}});
                            return {s: r.status, t: await r.text()};
                        } finally { clearTimeout(timer); }
                    }""", path)
            except Exception as e:
                log("브라우저 fetch 예외(%d): %s" % (attempt, str(e)[:120]))
                res = None
            if res and res["s"] == 200:
                self.fails = 0
                return _loads(res["t"])
            if attempt == 1:
                # 막혔거나 페이지가 죽었다. 처음부터 다시 연다
                status = res["s"] if res else "예외"
                log("응답 %s. 브라우저를 다시 엽니다." % status)
                try:
                    self._open()
                except Exception as e:
                    log("브라우저 재시작 실패: %s" % str(e)[:120])
                    break
        self.fails += 1
        raise FetchError("조회 실패: " + path.split("?")[0])


class CurlFetcher:
    """curl_cffi 로 크롬을 흉내낸다. pip install curl_cffi"""

    def __init__(self, impersonate="chrome"):
        self.impersonate = impersonate
        self._sess = None
        self._warmed = 0.0
        self.fails = 0

    def _session(self):
        if self._sess is None:
            from curl_cffi import requests as cr
            self._sess = cr.Session(impersonate=self.impersonate)
        if time.time() - self._warmed > 20 * 60:
            try:                           # 쿠키를 받기 위해 진입 페이지를 한 번 연다
                self._sess.get(ENTRY_PAGE, timeout=20)
            except Exception as e:
                log("진입 페이지 접속 예외: %s" % str(e)[:120])
            self._warmed = time.time()
        return self._sess

    def close(self):
        self._sess = None

    def get_json(self, path):
        try:
            r = self._session().get(BASE + path, timeout=20, headers={
                "Accept": "application/json", "Referer": ENTRY_PAGE,
                "Accept-Language": "ko-KR,ko;q=0.9"})
        except Exception as e:
            self.fails += 1
            raise FetchError("접속 예외: %s" % str(e)[:120])
        if r.status_code != 200:
            self.fails += 1
            self._warmed = 0.0             # 다음엔 쿠키부터 다시 받는다
            raise FetchError("응답 %s" % r.status_code)
        self.fails = 0
        return _loads(r.text)


def _loads(text):
    import json
    try:
        j = json.loads(text)
    except ValueError:
        raise FetchError("JSON 이 아닌 응답: %s" % text[:80].replace("\n", " "))
    if not isinstance(j, dict) or j.get("statusCode") not in (0, "0"):
        raise FetchError("API 오류: %s" % str(j)[:120])
    return j


def make_fetcher(kind, headless=True, channel=None):
    kind = (kind or "curl").lower()
    if kind == "curl":
        return CurlFetcher()
    if kind == "browser":
        return BrowserFetcher(headless=headless, channel=channel)
    raise ValueError("FETCHER 는 browser 또는 curl 이어야 합니다: %r" % kind)


# ---------------------------------------------------------------- 지점 목록

def fetch_catalog(fetcher):
    """전체 지점 -> [{no, name, region}]. 이름의 'CGV ' 접두는 없다."""
    j = fetcher.get_json("/api/v1/content/site/searchAllRegionAndSite?coCd=%s" % CO_CD)
    data = j.get("data") or {}
    regions = {r["comCdval"]: r["comCdvalNm"] for r in data.get("regionInfo") or []}
    out = []
    for s in data.get("siteInfo") or []:
        out.append({"no": str(s["siteNo"]), "name": str(s["siteNm"]).strip(),
                    "region": regions.get(s.get("regnGrpCd"), "")})
    return out


def _norm(s):
    return re.sub(r"\s+", "", s or "").lower()


def find_sites(catalog, query):
    """
    지점 검색. 번호(0056)가 정확히 맞으면 그것, 이름이 정확히 맞으면 그것,
    아니면 이름에 포함된 모두. 반환: 후보 리스트(0개 이상).
    """
    q = _norm(query)
    if not q:
        return []
    by_no = [c for c in catalog if c["no"] == query.strip() or c["no"].lstrip("0") == q]
    if by_no and q.isdigit():
        return by_no
    exact = [c for c in catalog if _norm(c["name"]) == q]
    if exact:
        return exact
    return [c for c in catalog if q in _norm(c["name"])]


# ---------------------------------------------------------------- 시간표

def hhmm(raw):
    """'1000' -> '10:00'. 이상하면 원문."""
    raw = str(raw or "")
    return "%s:%s" % (raw[:2], raw[2:4]) if re.match(r"^\d{4}$", raw) else raw


def minutes(raw):
    """'1000' -> 600. 해석 못 하면 None. '2530' 같은 심야 표기도 그대로 계산."""
    raw = str(raw or "")
    if not re.match(r"^\d{4}$", raw):
        return None
    return int(raw[:2]) * 60 + int(raw[2:])


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def normalize(rows):
    """
    searchMovScnInfo 의 data -> 회차 리스트.
    key 는 같은 회차를 구분하는 값(지점/날짜/관/회차순번). 시간이 바뀌어도 유지된다.
    """
    out = []
    for r in rows or []:
        title = (r.get("movNm") or r.get("expoProdNm") or r.get("prodNm") or "").strip()
        out.append({
            "key": "%s-%s-%s-%s" % (r.get("siteNo"), r.get("scnYmd"),
                                    r.get("scnsNo"), r.get("scnSseq")),
            "site_no": str(r.get("siteNo") or ""),
            "site_name": str(r.get("siteNm") or "").replace("CGV ", "", 1).strip(),
            "title": title,
            "full_title": (r.get("expoProdNm") or r.get("prodNm") or title).strip(),
            "hall": (r.get("scnsNm") or "").strip(),
            "start": hhmm(r.get("scnsrtTm")),
            "end": hhmm(r.get("scnendTm")),
            "start_min": minutes(r.get("scnsrtTm")),
            "free": to_int(r.get("frSeatCnt")),
            "total": to_int(r.get("stcnt")),
            "zone": (r.get("salsTznNm") or "").strip(),      # 모닝/브런치/일반/...
        })
    return out


def fetch_schedule(fetcher, site_no, ymd):
    j = fetcher.get_json(
        "/api/v1/booking/searchMovScnInfo?coCd=%s&siteNo=%s&scnYmd=%s&rtctlScopCd=08"
        % (CO_CD, site_no, ymd))
    return normalize(j.get("data"))


def keep_slot(slot, keywords, morning_end_min):
    """제목 키워드 일치 + (morning_end_min 이 있으면) 그 시각 이전 시작."""
    low = (slot["title"] + " " + slot["full_title"]).lower()
    if not any(k.lower() in low for k in keywords):
        return False
    if morning_end_min is None:
        return True
    return slot["start_min"] is not None and slot["start_min"] < morning_end_min


def parse_clock(text):
    """'12:00' / '1200' / '12' -> 분. 실패하면 None."""
    s = (text or "").strip()
    m = re.match(r"^(\d{1,2}):?(\d{2})?$", s)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h > 24 or mi > 59:
        return None
    return h * 60 + mi


def fmt_clock(minute):
    return "%02d:%02d" % (minute // 60, minute % 60)


if __name__ == "__main__":
    # 간단 점검: python cgv.py [지점이름] [YYYYMMDD]
    import os
    f = make_fetcher(os.environ.get("FETCHER", "curl"),
                     headless=os.environ.get("HEADLESS", "1") != "0",
                     channel=os.environ.get("CHROME_CHANNEL") or None)
    try:
        cat = fetch_catalog(f)
        print("지점 %d개" % len(cat))
        if len(sys.argv) > 1:
            site = find_sites(cat, sys.argv[1])[0]
            ymd = sys.argv[2] if len(sys.argv) > 2 else datetime.now(KST).strftime("%Y%m%d")
            for s in fetch_schedule(f, site["no"], ymd):
                print(s["start"], s["hall"], s["free"], "/", s["total"], s["title"])
    finally:
        f.close()
