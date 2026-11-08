# -*- coding: utf-8 -*-
"""
네트워크 없이 도는 단위 테스트.   python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import cgv                                   # noqa: E402
import watch                                 # noqa: E402
from bot import State, handle_command        # noqa: E402

KST = timezone(timedelta(hours=9))

CATALOG = [
    {"no": "0056", "name": "강남", "region": "서울"},
    {"no": "0001", "name": "강변", "region": "서울"},
    {"no": "0013", "name": "압구정", "region": "서울"},
    {"no": "0300", "name": "씨네드쉐프 압구정", "region": "서울"},
]


def row(site="0056", name="CGV 강남", ymd="20261111", hall="001", seq="1",
        start="1000", end="1150", free="132", total="144",
        title="극장판 치이카와-인어 섬의 비밀"):
    """searchMovScnInfo 응답의 data 한 줄 (실제 응답에서 확인한 필드명)."""
    return {"siteNo": site, "siteNm": name, "scnYmd": ymd, "scnsNo": hall, "scnSseq": seq,
            "scnsrtTm": start, "scnendTm": end, "frSeatCnt": free, "stcnt": total,
            "movNm": title, "expoProdNm": title + "(자막)", "prodNm": title,
            "scnsNm": "1관 (Laser)", "salsTznNm": "모닝"}


def slots(*rows):
    return cgv.normalize(list(rows))


class FakeTG:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True

    @property
    def last(self):
        return self.sent[-1]


def new_state(**kw):
    d = tempfile.mkdtemp()
    return State(path=os.path.join(d, "state.json"), default_morning=720, **kw)


class NormalizeTest(unittest.TestCase):
    def test_fields(self):
        s = slots(row())[0]
        self.assertEqual(s["key"], "0056-20261111-001-1")
        self.assertEqual(s["site_name"], "강남")
        self.assertEqual((s["start"], s["end"]), ("10:00", "11:50"))
        self.assertEqual((s["free"], s["total"]), (132, 144))
        self.assertEqual(s["start_min"], 600)

    def test_bad_numbers_become_none(self):
        s = slots(row(free=None, start="x"))[0]
        self.assertIsNone(s["free"])
        self.assertIsNone(s["start_min"])


class FilterTest(unittest.TestCase):
    def test_morning_boundary(self):
        kw = ["치이카와"]
        self.assertTrue(cgv.keep_slot(slots(row(start="1000"))[0], kw, 720))
        self.assertTrue(cgv.keep_slot(slots(row(start="1159"))[0], kw, 720))
        self.assertFalse(cgv.keep_slot(slots(row(start="1200"))[0], kw, 720))   # 정오는 오전 아님
        self.assertFalse(cgv.keep_slot(slots(row(start="1210"))[0], kw, 720))
        self.assertTrue(cgv.keep_slot(slots(row(start="1850"))[0], kw, None))   # 제한 없음

    def test_unknown_start_is_not_morning(self):
        self.assertFalse(cgv.keep_slot(slots(row(start="x"))[0], ["치이카와"], 720))

    def test_keyword(self):
        s = slots(row(title="폴2-데드포인트"))[0]
        self.assertFalse(cgv.keep_slot(s, ["치이카와"], 720))
        self.assertTrue(cgv.keep_slot(s, ["치이카와", "폴2"], 720))

    def test_parse_clock(self):
        self.assertEqual(cgv.parse_clock("12:00"), 720)
        self.assertEqual(cgv.parse_clock("1200"), 720)
        self.assertEqual(cgv.parse_clock("9"), 540)
        self.assertIsNone(cgv.parse_clock("25:00"))
        self.assertIsNone(cgv.parse_clock("abc"))


class FindSitesTest(unittest.TestCase):
    def names(self, q):
        return [c["name"] for c in cgv.find_sites(CATALOG, q)]

    def test_exact_and_partial(self):
        self.assertEqual(self.names("강남"), ["강남"])
        self.assertEqual(self.names("강"), ["강남", "강변"])
        self.assertEqual(self.names("압구정"), ["압구정"])            # 정확히 일치하면 그것만
        self.assertEqual(self.names("씨네드쉐프압구정"), ["씨네드쉐프 압구정"])   # 공백 무시

    def test_by_number(self):
        self.assertEqual(self.names("0001"), ["강변"])
        self.assertEqual(self.names("56"), ["강남"])

    def test_none(self):
        self.assertEqual(self.names("없는곳"), [])
        self.assertEqual(self.names(""), [])


class ProcessTest(unittest.TestCase):
    YMD = "20261111"

    def test_nothing_yet(self):
        st = new_state()
        self.assertEqual(watch.process(st, [], self.YMD), ([], False))
        self.assertIsNone(st.announced)

    def test_found_then_finish_without_seatwatch(self):
        st = new_state()
        m = slots(row())
        alerts, finish = watch.process(st, m, self.YMD)
        self.assertEqual([k for k, _ in alerts], ["found"])
        self.assertTrue(finish)
        self.assertEqual(st.announced, self.YMD)

    def test_found_keeps_watching_with_seatwatch(self):
        st = new_state()
        st.seatwatch = True
        alerts, finish = watch.process(st, slots(row()), self.YMD)
        self.assertEqual([k for k, _ in alerts], ["found"])
        self.assertFalse(finish)
        # 같은 결과가 또 와도 알리지 않는다
        self.assertEqual(watch.process(st, slots(row()), self.YMD), ([], False))

    def test_new_slot_and_opened_seat(self):
        st = new_state()
        st.seatwatch = True
        watch.process(st, slots(row(seq="1", free="0")), self.YMD)       # 매진으로 발견
        self.assertEqual(st.zero_slots, {"0056-20261111-001-1"})

        # 새 회차(다른 지점)가 생김
        alerts, _ = watch.process(
            st, slots(row(seq="1", free="0"), row(site="0001", name="CGV 강변")), self.YMD)
        self.assertEqual([k for k, _ in alerts], ["added"])
        self.assertEqual(alerts[0][1][0]["site_name"], "강변")

        # 매진이던 회차에 자리가 남
        alerts, _ = watch.process(
            st, slots(row(seq="1", free="3"), row(site="0001", name="CGV 강변")), self.YMD)
        self.assertEqual([k for k, _ in alerts], ["opened"])
        self.assertEqual(st.zero_slots, set())

    def test_new_soldout_slot_is_not_opened(self):
        st = new_state()
        st.seatwatch = True
        watch.process(st, slots(row(seq="1")), self.YMD)
        alerts, _ = watch.process(st, slots(row(seq="1"), row(seq="2", free="0")), self.YMD)
        self.assertEqual([k for k, _ in alerts], ["added"])

    def test_seatwatch_turned_off_after_found_finishes(self):
        st = new_state()
        st.seatwatch = True
        watch.process(st, slots(row()), self.YMD)
        st.seatwatch = False
        self.assertEqual(watch.process(st, slots(row()), self.YMD), ([], True))


class StateTest(unittest.TestCase):
    def test_remove_site_clears_its_progress(self):
        st = new_state()
        st.add_site({"no": "0056", "name": "강남"})
        st.add_site({"no": "0001", "name": "강변"})
        st.known_slots = {"0056-20261111-001-1", "0001-20261111-001-1"}
        st.zero_slots = {"0056-20261111-001-1"}
        st.remove_site("0056")
        self.assertEqual([s["no"] for s in st.sites], ["0001"])
        self.assertEqual(st.known_slots, {"0001-20261111-001-1"})
        self.assertEqual(st.zero_slots, set())

    def test_add_is_idempotent(self):
        st = new_state()
        self.assertTrue(st.add_site({"no": "0056", "name": "강남"}))
        self.assertFalse(st.add_site({"no": "0056", "name": "강남"}))

    def test_persists_across_restart(self):
        st = new_state()
        st.add_site({"no": "0056", "name": "강남"})
        st.morning_end = None
        st.seatwatch = True
        st.save()
        again = State(path=st.path, default_morning=720)
        self.assertEqual(again.sites, [{"no": "0056", "name": "강남"}])
        self.assertIsNone(again.morning_end)          # None(제한 없음)이 기본값으로 덮이지 않는다
        self.assertTrue(again.seatwatch)
        self.assertTrue(again.has_sites_key)


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.st = new_state()
        self.tg = FakeTG()

    def cmd(self, text):
        return handle_command(text, self.st, self.tg, lambda: "상태", lambda d, a=False: "조회",
                              lambda: CATALOG)

    def site_names(self):
        return [s["name"] for s in self.st.sites]

    def test_add_multiple(self):
        self.cmd("/add 강남, 강변")
        self.assertEqual(self.site_names(), ["강남", "강변"])
        self.assertIn("추가했습니다", self.tg.sent[0])

    def test_add_ambiguous_adds_nothing(self):
        self.cmd("/add 강")
        self.assertEqual(self.site_names(), [])
        self.assertIn("여러 곳", self.tg.last)

    def test_add_missing_and_duplicate(self):
        self.cmd("/add 강남")
        self.cmd("/add 강남, 없는곳")
        self.assertEqual(self.site_names(), ["강남"])
        self.assertIn("이미 감시 중", self.tg.last)
        self.assertIn("찾지 못했습니다", self.tg.last)

    def test_add_by_number(self):
        self.cmd("/add 0013")
        self.assertEqual(self.site_names(), ["압구정"])

    def test_remove(self):
        self.cmd("/add 강남, 강변")
        self.cmd("/remove 강남")
        self.assertEqual(self.site_names(), ["강변"])
        self.assertIn("뺐습니다", self.tg.last)
        self.cmd("/remove 강남")
        self.assertIn("감시 목록에 없습니다", self.tg.last)

    def test_sites_listing_and_search(self):
        self.cmd("/sites")
        self.assertIn("없습니다", self.tg.last)
        self.cmd("/add 강남")
        self.cmd("/sites 강")
        self.assertIn("✔ 0056", self.tg.last)            # 감시 중 표시

    def test_morning(self):
        self.cmd("/morning 11:30")
        self.assertEqual(self.st.morning_end, 690)
        self.cmd("/morning off")
        self.assertIsNone(self.st.morning_end)
        self.cmd("/morning 99")
        self.assertIsNone(self.st.morning_end)            # 잘못된 값은 무시
        self.assertIn("알아듣지 못했습니다", self.tg.last)

    def test_watch_resets_progress_and_reports_target_change(self):
        self.st.announced = "20261111"
        self.st.known_slots = {"x"}
        d = (datetime.now(KST).date() + timedelta(days=3))
        self.assertTrue(self.cmd("/watch " + d.strftime("%Y%m%d")))
        self.assertEqual(self.st.target, d)
        self.assertIsNone(self.st.announced)
        self.assertEqual(self.st.known_slots, set())

    def test_ok_stops_alert(self):
        self.st.ack_date = datetime.now(KST).date()
        self.st.ack_count = 3
        self.cmd("/ok")
        self.assertIsNone(self.st.ack_date)

    def test_bot_suffix_command(self):
        self.cmd("/add@my_bot 강남")
        self.assertEqual(self.site_names(), ["강남"])


class MessageTest(unittest.TestCase):
    def test_build_message(self):
        st = new_state()
        cfg = type("C", (), {"label": "CGV"})()
        target = datetime(2026, 11, 11, tzinfo=KST).date()
        text = watch.build_message(cfg, st, target, slots(row(), row(seq="2", start="1100", free="0")))
        self.assertIn("▪ CGV 강남", text)
        self.assertIn("10:00~11:50  1관 (Laser) 잔여 132/144", text)
        self.assertIn("매진", text)
        self.assertIn("20261111 (수)", text)
        self.assertIn("12:00 이전", text)


class WindowTest(unittest.TestCase):
    def test_parse_windows(self):
        self.assertEqual(watch.parse_windows("09:50-10:20, 13:00-13:10"),
                         [(590, 620), (780, 790)])


if __name__ == "__main__":
    unittest.main()
