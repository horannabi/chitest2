#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
이 환경(GitHub Actions 러너, VM 등)에서 CGV 에 닿는지 방식별로 진단한다.
결과는 probe.txt 에 쓴다. 한 가지라도 OK 면 그 방식으로 감시할 수 있다.

  requests   일반 HTTP. 예전에 막힌 방식. 비교용.
  curl_cffi  크롬 TLS 지문 흉내.
  browser    진짜 크롬(Playwright) 안에서 fetch.   <- 가장 유력

사용: python probe.py
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
BASE = "https://cgv.co.kr"
ENTRY = BASE + "/cnm/movieBook/cinema"
SITES_API = "/api/v1/content/site/searchAllRegionAndSite?coCd=A420"
SCHED_API = ("/api/v1/booking/searchMovScnInfo?coCd=A420&siteNo=0056"
             "&scnYmd=%s&rtctlScopCd=08")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

lines = []


def out(msg):
    print(msg, flush=True)
    lines.append(msg)


def verdict(status, body):
    if status == 200:
        try:
            j = json.loads(body)
            data = j.get("data") or []
            n = len(data.get("siteInfo") or []) if isinstance(data, dict) else len(data)
            return "OK" if j.get("statusCode") in (0, "0") else "API오류 %s" % str(j)[:60], n
        except ValueError:
            return "BLOCKED(JSON 아님: %s)" % body[:40].replace("\n", " "), None
    tag = "비정상적으로" in (body or "") and " 차단문구" or ""
    return "BLOCKED(%s%s)" % (status, tag), None


def probe_requests(ymd):
    import requests
    for api in (SITES_API, SCHED_API % ymd):
        try:
            r = requests.get(BASE + api, timeout=20, headers={"User-Agent": UA})
            v, n = verdict(r.status_code, r.text)
        except Exception as e:
            v, n = "예외 %s" % str(e)[:60], None
        out("requests   %-8s %s  n=%s" % (api.split("/")[-1].split("?")[0][:8], v, n))


def probe_curl_cffi(ymd):
    try:
        from curl_cffi import requests as cr
    except ImportError:
        out("curl_cffi  (설치 안 됨)")
        return
    try:
        s = cr.Session(impersonate="chrome")
        r0 = s.get(ENTRY, timeout=20)
        out("curl_cffi  entry page            %s" % r0.status_code)
    except Exception as e:
        out("curl_cffi  entry 예외 %s" % str(e)[:80])
        return
    for api in (SITES_API, SCHED_API % ymd):
        try:
            r = s.get(BASE + api, timeout=20, headers={"Referer": ENTRY,
                                                       "Accept": "application/json"})
            v, n = verdict(r.status_code, r.text)
        except Exception as e:
            v, n = "예외 %s" % str(e)[:60], None
        out("curl_cffi  %-8s %s  n=%s" % (api.split("/")[-1].split("?")[0][:8], v, n))


def probe_browser(ymd, headless):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        out("browser    (playwright 설치 안 됨)")
        return
    tag = "browser(%s)" % ("headless" if headless else "headed")
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(headless=headless)
            ctx = b.new_context(user_agent=UA, locale="ko-KR", timezone_id="Asia/Seoul")
            page = ctx.new_page()
            resp = page.goto(ENTRY, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)
            body = page.content()
            out("%-16s entry page %s  차단문구=%s" % (
                tag, resp.status if resp else "?", "비정상적으로" in body))
            for api in (SITES_API, SCHED_API % ymd):
                res = page.evaluate(
                    "async u => { const r = await fetch(u, {credentials:'include'});"
                    " return {s:r.status, t:await r.text()}; }", api)
                v, n = verdict(res["s"], res["t"])
                out("%-16s %-8s %s  n=%s" % (
                    tag, api.split("/")[-1].split("?")[0][:8], v, n))
            b.close()
        except Exception as e:
            out("%s 예외 %s" % (tag, str(e)[:120]))


def main():
    ymd = (datetime.now(KST) + timedelta(days=2)).strftime("%Y%m%d")
    out("probe %s KST  date=%s" % (datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"), ymd))
    try:
        ip = __import__("urllib.request").request.urlopen(
            "https://api.ipify.org", timeout=10).read().decode()
        out("egress ip %s" % ip)
    except Exception:
        pass
    probe_requests(ymd)
    probe_curl_cffi(ymd)
    probe_browser(ymd, headless=True)
    probe_browser(ymd, headless=False)     # 러너에 xvfb 가 있어야 동작
    out("")
    out("OK 가 하나라도 있으면 그 방식으로 이 환경에서 감시할 수 있습니다.")
    with open("probe.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
