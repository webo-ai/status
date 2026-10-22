"""python3 -m unittest discover -s scripts -p "test_*.py" 로 돈다. 바깥 주소를 부르지 않는다."""

import contextlib
import copy
import html
import http.client
import io
import json
import re
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import status

BASE = json.loads((Path(__file__).resolve().parent.parent / "status.json").read_text("utf-8"))
T0 = datetime(2026, 10, 22, 12, 0, tzinfo=status.KST)
ALL_OK = {"app": "ok", "api": "ok", "site": "ok"}


def fresh() -> dict:
    doc = copy.deepcopy(BASE)
    doc["checked_at"] = status.iso(T0)
    doc["history"] = {}
    doc["incidents"] = []
    doc["notice"] = None
    for comp in doc["components"]:
        comp["state"] = "ok"
    return doc


def text(page: str) -> str:
    return re.sub(r"<[^>]+>", "", page)


def state_of(doc: dict, comp_id: str) -> str:
    return next(c["state"] for c in doc["components"] if c["id"] == comp_id)


def at(hour: int, minute: int, day: int = 22) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=status.KST)


def notice(level, start, end, comps=("admin", "ai", "publish")) -> dict:
    return {
        "level": level, "title": "공지", "body": "안내합니다.", "components": list(comps),
        "starts_at": start and status.iso(start), "ends_at": end and status.iso(end),
    }


def play(steps, doc=None) -> dict:
    """판 차례를 돌린다. 판은 (시각, 결과) 또는 (시각, 결과, 이번 판부터 걸 공지 또는 None)이다."""
    doc = fresh() if doc is None else doc
    for step in steps:
        if len(step) == 3:
            doc["notice"] = step[2]
        doc, _ = status.apply(doc, {**ALL_OK, **step[1]}, step[0])
    return doc


def cell(doc: dict, comp_id: str) -> tuple[str, str | None]:
    """기능 칸에 그린 (상태 글, 덧붙인 글)."""
    page = status.render(doc)
    parts = dict(zip([c["id"] for c in doc["components"]], page.split('<li class="comp">')[1:]))
    label = re.search(r'<p class="state is-\w+"><svg.*?</svg>(.*?)</p>', parts[comp_id]).group(1)
    note = re.search(r'<p class="pending">(.*?)</p>', parts[comp_id])
    return label, note and html.unescape(note.group(1))


def strip_cells(doc: dict, comp_id: str) -> dict[str, str]:
    """30일 띠에서 칠한 칸만 {"월/일": 상태 글}로."""
    page = status.strip(doc, comp_id, status.parse(doc["checked_at"]))
    found = re.findall(r'title="(\d+)월 (\d+)일 ([^"]+)"', page)
    return {f"{m}/{d}": label for m, d, label in found if label != "기록 없음"}


def auto_incidents(doc: dict, comp_id: str) -> list[tuple[str, str | None]]:
    """그 기능이 든 자동 장애 기록의 (시작, 닫힘)."""
    return [(i["started_at"], i["resolved_at"]) for i in doc["incidents"]
            if i.get("auto") and comp_id in i["components"]]


I = status.iso
DOWN, UNK = {"api": "down"}, {"api": "unknown"}
MAINT = ("admin", "ai", "publish")
LAST_12 = "마지막으로 확인한 때는 10월 22일 (목) 오후 12:00이고, 그 무렵까지 "

# 규칙 1~7을 판 차례로 건다. (이름, 볼 기능, 판 차례, 자동 기록 [(시작, 닫힘)], 띠 칸, 칸 글)
SCENARIOS = [
    ("ok,down,unknown,down", "ai",
     [(at(12, 0), {}), (at(12, 10), DOWN), (at(12, 20), UNK), (at(12, 30), DOWN)],
     [(I(at(12, 10)), None)], {"10/22": "장애"}, ("장애", None)),
    ("ok,down,unknown,ok", "ai",
     [(at(12, 0), {}), (at(12, 10), DOWN), (at(12, 20), UNK), (at(12, 30), {})],
     [], {"10/22": "정상"}, ("정상", None)),
    ("down,ok,down,ok 한 번 실패하고 돌아온 날은 정상", "ai",
     [(at(12, 0), DOWN), (at(12, 10), {}), (at(12, 20), DOWN), (at(12, 30), {})],
     [], {"10/22": "정상"}, ("정상", None)),
    ("공지 중 down, 공지 끝 뒤 down 한 번, ok", "ai",
     [(at(12, 0), DOWN, notice("maintenance", at(11, 30), at(12, 15))), (at(12, 10), DOWN),
      (at(12, 20), DOWN), (at(12, 30), {})],
     [], {"10/22": "점검"}, ("정상", None)),
    ("공지 끝 뒤 못 잰 판 뒤 down 한 번", "ai",
     [(at(12, 0), DOWN, notice("maintenance", at(11, 30), at(12, 15))), (at(12, 10), DOWN),
      (at(12, 20), UNK), (at(12, 30), DOWN)],
     [], {"10/22": "점검"}, ("장애", None)),
    ("공지 전 down, 공지, 공지 끝 뒤 down 두 번", "ai",
     [(at(12, 0), DOWN), (at(12, 10), DOWN, notice("maintenance", at(12, 5), at(12, 25))),
      (at(12, 20), DOWN), (at(12, 30), DOWN), (at(12, 40), DOWN)],
     [(I(at(12, 30)), None)], {"10/22": "장애"}, ("장애", None)),
    ("down 공지, 재면 정상", "ai",
     [(at(12, 0), {}, notice("down", at(11, 55), None, ("ai",))), (at(12, 10), {})],
     [], {"10/22": "장애"}, ("장애", None)),
    ("down 공지, 재도 down", "ai",
     [(at(12, 0), DOWN, notice("down", at(11, 55), None, ("ai",))), (at(12, 10), DOWN), (at(12, 20), DOWN)],
     [], {"10/22": "장애"}, ("장애", None)),
    ("공지 전 실패 수는 공지를 걸린 채 지워도 안 살아난다", "ai",
     [(at(12, 0), DOWN), (at(12, 10), DOWN, notice("maintenance", at(12, 5), at(13, 0))),
      (at(12, 20), DOWN, None), (at(12, 30), DOWN)],
     [(I(at(12, 20)), None)], {"10/22": "장애"}, ("장애", None)),
    ("공지가 끝난 뒤 지우고 down 한 번", "ai",
     [(at(12, 0), DOWN, notice("maintenance", at(11, 30), at(12, 15))), (at(12, 10), DOWN),
      (at(12, 24), DOWN, None)],
     [], {"10/22": "점검"}, ("장애", None)),
    ("공지를 두고 같은 판 (지운 것과 결과가 같다)", "ai",
     [(at(12, 0), DOWN, notice("maintenance", at(11, 30), at(12, 15))), (at(12, 10), DOWN),
      (at(12, 24), DOWN)],
     [], {"10/22": "점검"}, ("장애", None)),
    ("한 번도 못 잰 기능", "ai",
     [(at(12, 10), UNK)],
     [], {}, ("확인 중", "이번 확인이 되지 않았습니다. 아직 확인한 기록이 없습니다.")),
    ("app만 unknown", "admin",
     [(at(12, 0), {}), (at(12, 10), {"app": "unknown"})],
     [], {"10/22": "정상"}, ("확인 중", "일부만 확인했습니다. " + LAST_12 + "정상이었습니다.")),
    ("app·api 둘 다 unknown", "admin",
     [(at(12, 0), {}), (at(12, 10), {"app": "unknown", "api": "unknown"})],
     [], {"10/22": "정상"}, ("확인 중", "이번 확인이 되지 않았습니다. " + LAST_12 + "정상이었습니다.")),
    ("app down, api unknown", "admin",
     [(at(12, 0), {}), (at(12, 10), {"app": "down", "api": "unknown"})],
     [], {"10/22": "정상"},
     ("장애", "일부만 확인했습니다. 마지막으로 확인한 때는 10월 22일 (목) 오후 12:10이고, 그 무렵까지 멈춰 있었습니다.")),
    ("장애 기록 뒤 둘 다 unknown", "admin",
     [(at(12, 0), DOWN), (at(12, 10), DOWN), (at(12, 20), {"app": "unknown", "api": "unknown"})],
     [(I(at(12, 0)), None)], {"10/22": "장애"},
     ("장애", "이번 확인이 되지 않았습니다. 마지막으로 확인한 때는 10월 22일 (목) 오후 12:10이고, 그 무렵까지 멈춰 있었습니다.")),
    ("자정 넘김", "ai",
     [(at(23, 55), DOWN), (at(0, 5, 23), DOWN), (at(0, 15, 23), {})],
     [(I(at(23, 55)), I(at(0, 15, 23)))], {"10/22": "장애", "10/23": "장애"}, ("정상", None)),
    ("자정 넘긴 점검 뒤 down 두 번", "ai",
     [(at(23, 30), DOWN, notice("maintenance", at(23, 20), at(0, 15, 23)))]
     + [(at(23, 30) + timedelta(minutes=10 * k), DOWN) for k in range(1, 7)],
     [(I(at(0, 20, 23)), None)], {"10/22": "점검", "10/23": "장애"}, ("장애", None)),
    ("기록 중 공지 판의 정상은 닫지 않는다", "ai",
     [(at(12, 0), DOWN), (at(12, 10), DOWN), (at(12, 20), {}, notice("maintenance", at(12, 15), at(13, 0))),
      (at(12, 30), {}), (at(13, 0), {})],
     [(I(at(12, 0)), I(at(13, 0)))], {"10/22": "장애"}, ("정상", None)),
    ("기록 중 못 잰 판은 닫지 않는다", "ai",
     [(at(12, 0), DOWN), (at(12, 10), DOWN), (at(12, 20), UNK), (at(12, 30), {})],
     [(I(at(12, 0)), I(at(12, 30)))], {"10/22": "장애"}, ("정상", None)),
    ("느림 두 번은 기록을 열지 않고 띠는 정상", "ai",
     [(at(12, 0), {"api": "slow"}), (at(12, 10), {"api": "slow"})],
     [], {"10/22": "정상"}, ("느림", None)),
    ("느림 공지 중 down 뒤 못 잼", "ai",
     [(at(12, 0), DOWN, notice("slow", at(11, 55), None, ("ai",))), (at(12, 10), UNK)],
     [], {}, ("느림", None)),
    # 4차 규칙 4. 공지 중에 잰 down은 칸 값이 아니라서, 센 값이 없는 칸은 확인 중이다.
    ("느림 공지가 끝난 뒤 못 잼 (상태 말은 마지막으로 센 값)", "ai",
     [(at(12, 0), DOWN, notice("slow", at(11, 55), at(12, 5), ("ai",))), (at(12, 10), UNK)],
     [], {}, ("확인 중", "이번 확인이 되지 않았습니다. 아직 확인한 기록이 없습니다.")),
    ("점검 공지 중 못 잰 판", "ai",
     [(at(12, 0), {}), (at(12, 10), UNK, notice("maintenance", at(12, 5), at(13, 0)))],
     [], {"10/22": "점검"}, ("점검", None)),
    ("공지 중에 잰 down은 공지 뒤 못 잰 칸의 값으로 안 쓴다", "ai",
     [(at(12, 0), {}), (at(12, 10), DOWN, notice("maintenance", at(12, 5), at(12, 25))),
      (at(12, 20), DOWN), (at(12, 30), UNK)],
     [], {"10/22": "점검"}, ("확인 중", "이번 확인이 되지 않았습니다. " + LAST_12 + "정상이었습니다.")),
    ("공지 끝 시각에 잰 판은 공지 밖으로 센다", "ai",
     [(at(12, 0), DOWN, notice("maintenance", at(11, 30), at(12, 10))), (at(12, 10), DOWN), (at(12, 20), DOWN)],
     [(I(at(12, 10)), None)], {"10/22": "장애"}, ("장애", None)),
    # 4차 규칙 2. 첫 실패 뒤 못 잰 판을 건너뛰는 건 30분까지다.
    ("첫 실패 뒤 꼭 30분에 다시 down이면 잇는다", "ai",
     [(at(12, 0), DOWN), (at(12, 10), UNK), (at(12, 20), UNK), (at(12, 30), DOWN)],
     [(I(at(12, 0)), None)], {"10/22": "장애"}, ("장애", None)),
    ("첫 실패 뒤 40분에 down이면 1부터 다시 센다", "ai",
     [(at(12, 0), DOWN), (at(12, 10), UNK), (at(12, 20), UNK), (at(12, 30), UNK), (at(12, 40), DOWN)],
     [], {}, ("장애", None)),
    ("다시 센 첫 실패에서 시작한다", "ai",
     [(at(12, 0), DOWN), (at(12, 10), UNK), (at(12, 20), UNK), (at(12, 30), UNK), (at(12, 40), DOWN),
      (at(12, 50), DOWN)],
     [(I(at(12, 40)), None)], {"10/22": "장애"}, ("장애", None)),
    ("기록이 열린 뒤엔 오래 못 재도 다시 세지 않는다", "ai",
     [(at(12, 0), DOWN), (at(12, 10), DOWN), (at(12, 20), UNK), (at(12, 30), UNK), (at(12, 40), UNK),
      (at(12, 50), DOWN)],
     [(I(at(12, 0)), None)], {"10/22": "장애"}, ("장애", None)),
    # 4차 규칙 7. 느림·정상 공지 중에 잰 ok·slow는 정상으로 칠한다.
    ("느림 공지 중 잰 ok는 정상으로 칠한다", "ai",
     [(at(12, 0), {}, notice("slow", at(11, 55), None, ("ai",)))],
     [], {"10/22": "정상"}, ("느림", None)),
    ("느림 공지 중 잰 slow도 정상으로 칠한다", "ai",
     [(at(12, 0), {"api": "slow"}, notice("slow", at(11, 55), None, ("ai",)))],
     [], {"10/22": "정상"}, ("느림", None)),
    ("정상 공지 중 잰 slow는 정상으로 칠한다", "ai",
     [(at(12, 0), {"api": "slow"}, notice("ok", at(11, 55), None, ("ai",)))],
     [], {"10/22": "정상"}, ("정상", None)),
    ("느림 공지 중 잰 down은 칠하지 않는다", "ai",
     [(at(12, 0), DOWN, notice("slow", at(11, 55), None, ("ai",))), (at(12, 10), DOWN)],
     [], {}, ("느림", None)),
]


class ScenarioTest(unittest.TestCase):
    def test_rules_by_round_order(self):
        for name, comp_id, steps, incidents, days, shown in SCENARIOS:
            with self.subTest(name):
                doc = play(steps)
                self.assertEqual(auto_incidents(doc, comp_id), incidents)
                self.assertEqual(strip_cells(doc, comp_id), days)
                self.assertEqual(cell(doc, comp_id), shown)

    # 규칙 2. 실패 수와 첫 실패 시각은 기능 칸에 저장한다.
    def test_streak_is_stored_on_component(self):
        def ai(doc):
            return next(c for c in doc["components"] if c["id"] == "ai")
        doc = play([(at(12, 0), DOWN)])
        self.assertEqual((ai(doc)["streak"], ai(doc)["streak_since"]), (1, I(at(12, 0))))
        doc = play([(at(12, 10), UNK)], doc)
        self.assertEqual((ai(doc)["streak"], ai(doc)["streak_since"]), (1, I(at(12, 0))))
        doc = play([(at(12, 20), {"api": "slow"})], doc)
        self.assertEqual((ai(doc)["streak"], ai(doc)["streak_since"]), (0, None))
        doc = play([(at(12, 30), DOWN), (at(12, 40), DOWN, notice("maintenance", at(12, 35), None))], doc)
        self.assertEqual((ai(doc)["streak"], ai(doc)["streak_since"]), (0, None))

    # 규칙 8. 손님 홈페이지 칸은 정상일 때만 단정하고, 첫 화면 제목과 부딪치지 않는다.
    def test_site_card_by_site_state(self):
        rows = [
            ([(at(12, 0), {})], "is-calm", "손님 홈페이지는 계속 열립니다", "모든 서비스가 정상입니다"),
            ([(at(12, 0), {}), (at(12, 10), {"site": "unknown"})], "is-neutral",
             "손님 홈페이지가 열리는지 다시 확인하고 있습니다", "손님 사이트 상태를 다시 확인하고 있습니다"),
            ([(at(12, 0), {"site": "slow"})], "is-neutral",
             "손님 홈페이지가 평소보다 느리게 열릴 수 있습니다", "손님 사이트가 평소보다 느립니다"),
            ([(at(12, 0), {}, notice("maintenance", at(11, 55), None, ("sites",)))], "is-neutral",
             "손님 홈페이지가 점검으로 잠시 열리지 않을 수 있습니다", "손님 사이트 점검 중입니다"),
            ([(at(12, 0), {"site": "down"})], "is-down", "손님 홈페이지가 지금 열리지 않습니다",
             "손님 사이트가 지금 멈춰 있습니다"),
        ]
        for steps, tone, title, hero in rows:
            with self.subTest(title):
                page = status.render(play(steps))
                card = re.search(r'<section class="card site-card (is-\w+)".*?</section>', page)
                self.assertEqual(card.group(1), tone)
                self.assertIn(title, card.group(0))
                self.assertIn(hero, text(re.search(r'<span class="hl">(.*?)</span></h1>', page).group(1)))
                if tone != "is-calm":
                    self.assertNotIn("계속 열립니다", card.group(0))
                    self.assertNotIn("그대로 볼 수 있습니다", card.group(0))

    # 규칙 7·10. 띠 설명과 첫 화면 시각 이름.
    def test_page_words(self):
        page = text(status.render(play([(at(12, 0), {})])))
        self.assertIn("잇달아 두 번 멈춘 날과 장애 안내가 있던 날을 장애로 칠합니다.", page)
        self.assertIn("이 페이지를 마지막으로 바꾼 때", page)
        self.assertNotIn("페이지를 고친 때", page)


def hl(page: str) -> str:
    return text(re.search(r'<span class="hl">(.*?)</span></h1>', page).group(1))


class FourthRoundTest(unittest.TestCase):
    # 4차 1. 공지가 걸리거나 풀린 판은 등급과 상관없이 새로 쓴다.
    def test_notice_start_and_end_rewrite_file(self):
        for level in ("ok", "slow", "maintenance", "down"):
            with self.subTest(level):
                doc, _ = status.apply(fresh(), ALL_OK, at(12, 0))
                doc["notice"] = notice(level, at(12, 5), at(12, 25), ("ai",))
                doc, write = status.apply(doc, ALL_OK, at(12, 10))
                self.assertTrue(write, "공지가 걸린 판")
                doc, write = status.apply(doc, ALL_OK, at(12, 20))
                self.assertFalse(write, "공지가 그대로인 판")
                doc, write = status.apply(doc, ALL_OK, at(12, 30))
                self.assertTrue(write, "공지가 풀린 판")
                self.assertNotIn("점검 중입니다", status.render(doc))
                _, write = status.apply(doc, ALL_OK, at(12, 40))
                self.assertFalse(write)

    # 4차 2. 그리기만 돌릴 때도 공지가 걸렸는지는 지금 시각으로 본다.
    def test_render_command_uses_current_time(self):
        rows = [
            (notice("maintenance", at(12, 5), at(13, 0)), "지금 안내", "예정된 안내"),
            (notice("maintenance", at(11, 0), at(12, 5)), "모든 서비스가 정상입니다", "점검 중입니다"),
        ]
        for item, want, unwanted in rows:
            with self.subTest(want), tempfile.TemporaryDirectory() as tmp:
                doc = fresh()
                doc["notice"] = item
                path, index = Path(tmp) / "status.json", Path(tmp) / "index.html"
                path.write_text(json.dumps(doc, ensure_ascii=False), "utf-8")
                with mock.patch.object(status, "STATUS", path), mock.patch.object(status, "INDEX", index), \
                        mock.patch.object(status, "clock", return_value=at(12, 6)):
                    self.assertEqual(status.main(["render"]), 0)
                page = index.read_text("utf-8")
                self.assertIn(want, page)
                self.assertNotIn(unwanted, page)

    # 4차 3. 보이는 장애 칸이 다 이번 판에 못 잰 칸이면 첫 화면 제목에서 단정을 뺀다.
    def test_headline_does_not_assert_unconfirmed_down(self):
        both_unk = {"app": "unknown", "api": "unknown"}
        doc = play([(at(12, 0), DOWN), (at(12, 10), DOWN), (at(12, 20), both_unk)])
        self.assertEqual(hl(status.render(doc)), "관리 화면, AI 고치기, 게시가 마지막 확인 때 멈춰 있었습니다")
        self.assertNotIn("지금 멈춰 있습니다", status.render(doc))
        # 하나라도 이번 판에 멈춘 게 재지면 단정한다.
        doc = play([(at(12, 0), DOWN), (at(12, 10), DOWN), (at(12, 20), {"app": "down", "api": "unknown"})])
        self.assertIn("지금 멈춰 있습니다", hl(status.render(doc)))
        # 장애 공지는 사람이 적은 단정이라 못 잰 판에도 단정한다.
        doc = play([(at(12, 0), {}), (at(12, 10), UNK, notice("down", at(12, 5), None, ("ai",)))])
        self.assertEqual(hl(status.render(doc)), "AI 고치기가 지금 멈춰 있습니다")

    # 4차 4. 목록·묶음 꼴과 글자 칸을 보고, 비었으면 빈 글로 그린다.
    def test_validate_shapes_and_empty_text(self):
        inc = {"id": "x", "level": "down", "title": "t", "components": ["admin"],
               "started_at": status.iso(T0), "resolved_at": None}
        rows = [
            ({"incidents": {"x": inc}}, r"^incidents는.*목록"),
            ({"history": []}, r"^history는"),
            ({"history": {"2026-10-22": "ok"}}, r"history\.2026-10-22는"),
            ({"notice": {"level": "maintenance", "title": "점검", "body": 3, "components": []}}, r"notice\.body"),
            ({"incidents": [dict(inc, summary=["a"])]}, r"incidents\[0\]\(x\)\.summary"),
        ]
        for change, pattern in rows:
            with self.subTest(pattern):
                doc = fresh()
                doc.update(change)
                with self.assertRaisesRegex(status.StatusFileError, pattern):
                    status.validate(doc)
        doc = fresh()
        doc["notice"] = dict(notice("slow", at(11, 0), None, ("ai",)), body=None)
        doc["incidents"] = [dict(inc, summary=None)]
        status.validate(doc)
        page = status.render(doc)
        self.assertNotIn("None", page)
        self.assertIn('<p></p></section>', page)
        # 띠의 느림 칸은 정상으로 읽는다.
        doc = fresh()
        doc["history"] = {"2026-10-22": {"admin": "slow"}}
        status.validate(doc)
        self.assertEqual(strip_cells(doc, "admin"), {"10/22": "정상"})

    # 4차 5. 사람이 닫은 같은 id 기록은 새로 열지 않고 다시 연다.
    def test_closed_by_hand_reopens_same_record(self):
        doc = play([(at(12, 0), DOWN), (at(12, 10), DOWN)])
        doc["incidents"][0]["resolved_at"] = I(at(12, 15))
        doc["incidents"][0]["title"] = "AI 회사 응답 지연"
        doc = play([(at(12, 20), DOWN)], doc)
        self.assertEqual([i["id"] for i in doc["incidents"]], ["20261022-1200"])
        self.assertIsNone(doc["incidents"][0]["resolved_at"])
        self.assertEqual(doc["incidents"][0]["title"], "AI 회사 응답 지연")
        doc = play([(at(12, 30), {})], doc)
        self.assertEqual(doc["incidents"][0]["resolved_at"], I(at(12, 30)))
        # 지운 뒤엔 같은 id로 하나만 다시 연다.
        doc = play([(at(12, 0), DOWN), (at(12, 10), DOWN)])
        doc["incidents"] = []
        doc = play([(at(12, 20), DOWN), (at(12, 30), DOWN)], doc)
        self.assertEqual([i["id"] for i in doc["incidents"]], ["20261022-1200"])

    # 4차 6. 메일 주소는 @ 뒤에서 먼저 줄을 바꾼다.
    def test_mail_breaks_after_at(self):
        self.assertIn("<span>webomni.team@<wbr>gmail.com</span>", status.render(fresh()))

    # 4차 7. 손님 사이트만 확인 중이면 제목 끝을 한 덩어리로 묶고 같은 말을 되풀이하지 않는다.
    def test_site_checking_words(self):
        page = status.render(play([(at(12, 0), {}), (at(12, 10), {"site": "unknown"})]))
        self.assertIn('상태를 <span class="nw">다시 확인하고 있습니다</span>', page)
        self.assertEqual(text(page).count("자동 확인이 잠시 되지 않았습니다"), 1)
        card = re.search(r'<section class="card site-card.*?</section>', page).group(0)
        self.assertNotIn("확인이", text(card).replace("확인하고", ""))


class ApplyTest(unittest.TestCase):
    def test_same_state_inside_heartbeat_skips_write(self):
        doc, _ = status.apply(fresh(), ALL_OK, T0)
        _, write = status.apply(doc, ALL_OK, T0 + timedelta(minutes=10))
        self.assertFalse(write)

    def test_heartbeat_rewrites_after_55_minutes(self):
        doc, _ = status.apply(fresh(), ALL_OK, T0)
        new, write = status.apply(doc, ALL_OK, T0 + timedelta(minutes=55))
        self.assertTrue(write)
        self.assertEqual(new["checked_at"], status.iso(T0 + timedelta(minutes=55)))

    def test_api_down_stops_admin_ai_publish_with_one_incident(self):
        # 관리 화면 파일은 열려도 API가 없으면 로그인부터 안 된다.
        api_down = {**ALL_OK, "api": "down"}
        doc, write = status.apply(fresh(), api_down, T0)
        self.assertTrue(write)
        self.assertEqual(state_of(doc, "admin"), "down")
        self.assertEqual(state_of(doc, "ai"), "down")
        self.assertEqual(state_of(doc, "publish"), "down")
        self.assertEqual(state_of(doc, "sites"), "ok")
        doc, _ = status.apply(doc, api_down, T0 + timedelta(minutes=10))
        self.assertEqual(len(doc["incidents"]), 1)
        self.assertEqual(doc["incidents"][0]["components"], ["admin", "ai", "publish"])

    def test_later_component_joins_open_incident(self):
        doc, _ = status.apply(fresh(), {**ALL_OK, "app": "down"}, T0)
        self.assertEqual(state_of(doc, "ai"), "ok")
        both = {"app": "down", "api": "down", "site": "ok"}
        doc, _ = status.apply(doc, both, T0 + timedelta(minutes=10))
        # 관리 화면은 두 번째라 기록을 열고, AI 고치기·게시는 첫 실패라 아직 안 든다.
        self.assertEqual(len(doc["incidents"]), 1)
        self.assertEqual(doc["incidents"][0]["components"], ["admin"])
        doc, _ = status.apply(doc, both, T0 + timedelta(minutes=20))
        self.assertEqual(len(doc["incidents"]), 1)
        self.assertEqual(doc["incidents"][0]["components"], ["admin", "ai", "publish"])
        doc, _ = status.apply(doc, ALL_OK, T0 + timedelta(minutes=40))
        self.assertEqual(doc["incidents"][0]["resolved_at"], status.iso(T0 + timedelta(minutes=40)))
        self.assertEqual(doc["history"]["2026-10-22"]["admin"], "down")

    # 1. 못 잰 기능은 확인 시각을 올리지 않고 "확인 중"으로 그린다.
    def test_unknown_keeps_component_checked_at_and_shows_checking(self):
        doc, _ = status.apply(fresh(), ALL_OK, T0)
        later = T0 + timedelta(minutes=60)
        doc, write = status.apply(doc, {**ALL_OK, "app": "unknown"}, later)
        self.assertTrue(write)
        admin = next(c for c in doc["components"] if c["id"] == "admin")
        ai = next(c for c in doc["components"] if c["id"] == "ai")
        self.assertEqual(admin["checked_at"], status.iso(T0))
        self.assertTrue(admin["pending"])
        self.assertEqual(ai["checked_at"], status.iso(later))
        self.assertNotIn("pending", ai)
        page = text(status.render(doc))
        self.assertIn("관리 화면 상태를 다시 확인하고 있습니다", page)
        self.assertIn("확인 중", page)
        self.assertIn("마지막으로 확인한 때는 10월 22일 (목) 오후 12:00이고, 그 무렵까지 정상이었습니다.", page)
        self.assertNotIn("모든 서비스가 정상입니다", page)
        # 막힌 채 다음 판이 돌아도 확인 시각은 그대로다.
        doc, _ = status.apply(doc, {**ALL_OK, "app": "unknown"}, later + timedelta(minutes=60))
        self.assertEqual(next(c for c in doc["components"] if c["id"] == "admin")["checked_at"], status.iso(T0))
        # 다시 재지면 풀린다.
        doc, write = status.apply(doc, ALL_OK, later + timedelta(minutes=70))
        self.assertTrue(write)
        self.assertNotIn("pending", next(c for c in doc["components"] if c["id"] == "admin"))
        self.assertIn("모든 서비스가 정상입니다", status.render(doc))

    def test_known_down_is_not_hidden_by_checking(self):
        # 화면 파일이 멈춘 게 확실하면 API를 못 쟀어도 장애로 그린다.
        doc, _ = status.apply(fresh(), {"app": "down", "api": "unknown", "site": "ok"}, T0)
        admin = next(c for c in doc["components"] if c["id"] == "admin")
        self.assertEqual(status.shown(doc, admin, T0), "down")

    # 2. 장애 기록은 두 번 잇달아 실패해야 연다.
    def test_single_failure_changes_state_but_opens_no_incident(self):
        api_down = {**ALL_OK, "api": "down"}
        doc, write = status.apply(fresh(), api_down, T0)
        self.assertTrue(write)
        self.assertEqual(state_of(doc, "ai"), "down")
        self.assertEqual(doc["incidents"], [])
        doc, _ = status.apply(doc, ALL_OK, T0 + timedelta(minutes=10))
        self.assertEqual(doc["incidents"], [])
        doc, _ = status.apply(doc, api_down, T0 + timedelta(minutes=20))
        self.assertEqual(doc["incidents"], [])
        doc, _ = status.apply(doc, api_down, T0 + timedelta(minutes=30))
        self.assertEqual(len(doc["incidents"]), 1)
        # 시작 시각은 기록을 연 때가 아니라 처음 멈춘 때다.
        self.assertEqual(doc["incidents"][0]["started_at"], status.iso(T0 + timedelta(minutes=20)))

    # 규칙 3. 공지는 기록을 닫지 않는다. 공지가 끝난 뒤 잰 정상이 닫는다.
    def test_notice_does_not_resolve_open_incident(self):
        both = {"app": "down", "api": "down", "site": "ok"}
        doc, _ = status.apply(fresh(), both, T0)
        doc, _ = status.apply(doc, both, T0 + timedelta(minutes=10))
        self.assertIsNone(doc["incidents"][0]["resolved_at"])
        doc["notice"] = {
            "level": "maintenance",
            "title": "긴급 점검",
            "body": "점검합니다.",
            "components": ["admin", "ai", "publish"],
            "starts_at": status.iso(T0 + timedelta(minutes=15)),
            "ends_at": status.iso(T0 + timedelta(hours=2)),
        }
        doc, _ = status.apply(doc, both, T0 + timedelta(minutes=20))
        self.assertIsNone(doc["incidents"][0]["resolved_at"])
        doc, _ = status.apply(doc, ALL_OK, T0 + timedelta(minutes=30))
        self.assertIsNone(doc["incidents"][0]["resolved_at"])
        doc, _ = status.apply(doc, ALL_OK, T0 + timedelta(hours=2))
        self.assertEqual(doc["incidents"][0]["resolved_at"], status.iso(T0 + timedelta(hours=2)))

    def test_unknown_keeps_previous_state(self):
        doc, _ = status.apply(fresh(), {**ALL_OK, "app": "down"}, T0)
        doc, _ = status.apply(doc, {**ALL_OK, "app": "unknown"}, T0 + timedelta(minutes=10))
        self.assertEqual(state_of(doc, "admin"), "down")

    # 2차 1. 한 번 실패한 다음 판을 봇 검사로 못 쟀으면 두 번째 실패로 세지 않는다.
    def test_down_then_unknown_opens_no_incident(self):
        doc, _ = status.apply(fresh(), {**ALL_OK, "api": "down"}, T0)
        doc, _ = status.apply(doc, {**ALL_OK, "api": "unknown"}, T0 + timedelta(minutes=10))
        self.assertEqual(state_of(doc, "ai"), "down")
        self.assertEqual(doc["incidents"], [])
        self.assertEqual(doc["history"]["2026-10-22"], {"sites": "ok"})
        # 그다음 판에 실제로 다시 멈춘 게 재지면 그때 연다.
        doc, _ = status.apply(doc, {**ALL_OK, "api": "down"}, T0 + timedelta(minutes=20))
        self.assertEqual(len(doc["incidents"]), 1)
        self.assertEqual(doc["incidents"][0]["started_at"], status.iso(T0))

    # 2차 2. 한 번 실패하고 돌아온 날은 띠에 장애로 적지 않고, 기록이 열리면 처음 멈춘 날까지 거슬러 적는다.
    def test_single_failure_day_is_not_down_in_history(self):
        doc, _ = status.apply(fresh(), ALL_OK, T0)
        doc, _ = status.apply(doc, {**ALL_OK, "api": "down"}, T0 + timedelta(minutes=10))
        doc, _ = status.apply(doc, ALL_OK, T0 + timedelta(minutes=20))
        self.assertEqual(doc["history"]["2026-10-22"]["ai"], "ok")
        self.assertEqual(doc["incidents"], [])
        self.assertNotIn("장애 하루", status.strip(doc, "ai", T0 + timedelta(minutes=20)))
        # 그날 처음 실패만 있었던 기능은 그날 칸을 비워 둔다.
        doc = fresh()
        doc, _ = status.apply(doc, {**ALL_OK, "api": "down"}, T0)
        self.assertNotIn("ai", doc["history"]["2026-10-22"])

    def test_incident_backfills_from_first_stop_day(self):
        night = datetime(2026, 10, 22, 23, 55, tzinfo=status.KST)
        doc = fresh()
        doc["checked_at"] = status.iso(night - timedelta(minutes=10))
        doc, _ = status.apply(doc, {**ALL_OK, "api": "down"}, night)
        self.assertNotIn("ai", doc["history"]["2026-10-22"])
        doc, _ = status.apply(doc, {**ALL_OK, "api": "down"}, night + timedelta(minutes=10))
        self.assertEqual(doc["history"]["2026-10-22"]["ai"], "down")
        self.assertEqual(doc["history"]["2026-10-23"]["ai"], "down")
        self.assertEqual(doc["incidents"][0]["started_at"], status.iso(night))

    # 규칙 6. 배포 직후 첫 판에 못 재면 기능 시각을 지어내지 않는다.
    def test_first_unknown_has_no_component_time(self):
        doc = fresh()
        for comp in doc["components"]:
            comp.pop("checked_at", None)
        doc, _ = status.apply(doc, {**ALL_OK, "app": "unknown"}, T0 + timedelta(minutes=10))
        admin = next(c for c in doc["components"] if c["id"] == "admin")
        self.assertEqual(admin["pending"], "some")
        self.assertNotIn("checked_at", admin)

    # 규칙 3. 공지 중에 멈춰 공지 뒤에도 멈춰 있으면, 공지 뒤 첫 실패부터 두 번을 센다.
    def test_incident_after_notice_starts_at_first_counted_failure(self):
        base = fresh()
        base["notice"] = {
            "level": "maintenance", "title": "정기 점검", "body": "점검합니다.",
            "components": ["admin", "ai", "publish"],
            "starts_at": status.iso(T0 - timedelta(minutes=30)),
            "ends_at": status.iso(T0 + timedelta(minutes=25)),
        }
        both = {"app": "down", "api": "down", "site": "ok"}
        doc, _ = status.apply(base, both, T0)
        doc, _ = status.apply(doc, both, T0 + timedelta(minutes=10))
        self.assertEqual(doc["incidents"], [])
        doc, _ = status.apply(doc, both, T0 + timedelta(minutes=30))
        self.assertEqual(doc["incidents"], [])
        doc, _ = status.apply(doc, both, T0 + timedelta(minutes=40))
        self.assertEqual(len(doc["incidents"]), 1)
        self.assertEqual(doc["incidents"][0]["started_at"], status.iso(T0 + timedelta(minutes=30)))
        # 공지 전에 이미 멈춰 있던 게 아니라면 처음 멈춘 때가 늦은 쪽이다.
        late = fresh()
        late["notice"] = dict(base["notice"], ends_at=status.iso(T0 - timedelta(minutes=5)))
        doc, _ = status.apply(late, both, T0)
        doc, _ = status.apply(doc, both, T0 + timedelta(minutes=10))
        self.assertEqual(doc["incidents"][0]["started_at"], status.iso(T0))

    def test_slow_does_not_open_incident(self):
        doc, write = status.apply(fresh(), {**ALL_OK, "app": "slow"}, T0)
        self.assertTrue(write)
        self.assertEqual(doc["incidents"], [])
        # 규칙 7. 셈하는 느림은 띠에서 정상이다.
        self.assertEqual(doc["history"]["2026-10-22"]["admin"], "ok")

    def test_maintenance_notice_suppresses_incident(self):
        base = fresh()
        base["notice"] = {
            "level": "maintenance",
            "title": "정기 점검",
            "body": "점검합니다.",
            "components": ["admin", "ai", "publish"],
            "starts_at": status.iso(T0 - timedelta(minutes=5)),
            "ends_at": status.iso(T0 + timedelta(hours=1)),
        }
        doc, _ = status.apply(base, {"app": "down", "api": "down", "site": "ok"}, T0)
        self.assertEqual(doc["incidents"], [])
        self.assertEqual(doc["history"]["2026-10-22"]["admin"], "maintenance")
        # 점검이 끝났는데도 두 번 잇달아 멈춰 있으면 끝난 뒤 첫 실패부터 장애로 적는다.
        doc, _ = status.apply(doc, {"app": "down", "api": "down", "site": "ok"}, T0 + timedelta(hours=1))
        self.assertEqual(doc["incidents"], [])
        doc, _ = status.apply(doc, {"app": "down", "api": "down", "site": "ok"}, T0 + timedelta(hours=1, minutes=10))
        self.assertEqual(len(doc["incidents"]), 1)
        self.assertEqual(doc["incidents"][0]["started_at"], status.iso(T0 + timedelta(hours=1)))

    def test_history_is_trimmed(self):
        doc = fresh()
        doc["history"] = {"2026-08-01": {"admin": "ok"}}
        new, _ = status.apply(doc, ALL_OK, T0)
        self.assertNotIn("2026-08-01", new["history"])


class ProbeTest(unittest.TestCase):
    def test_all_failures_are_down_and_challenge_is_unknown(self):
        with mock.patch.object(status, "probe_once", return_value=("down", "http 502")), \
                mock.patch.object(status.time, "sleep"):
            self.assertEqual(status.probe("https://x", "html")[0], "down")
        seq = [("unknown", "cf-mitigated 403"), ("down", "URLError"), ("down", "URLError")]
        with mock.patch.object(status, "probe_once", side_effect=seq), \
                mock.patch.object(status.time, "sleep"):
            self.assertEqual(status.probe("https://x", "html")[0], "unknown")

    # 4. 응답이 중간에 끊기면(IncompleteRead 등) 멈춤으로 본다.
    def test_http_exception_is_down(self):
        res = mock.MagicMock()
        res.__enter__.return_value = res
        res.status = 200
        res.read.side_effect = http.client.IncompleteRead(b"<ht", 100)
        with mock.patch.object(status.urllib.request, "urlopen", return_value=res):
            self.assertEqual(status.probe_once("https://x", "html"), ("down", "IncompleteRead"))
        with mock.patch.object(status.urllib.request, "urlopen",
                               side_effect=http.client.RemoteDisconnected("closed")):
            self.assertEqual(status.probe_once("https://x", "html")[0], "down")
        err = urllib.error.HTTPError("https://x", 502, "bad", {}, None)
        err.read = mock.Mock(side_effect=http.client.IncompleteRead(b"", 10))
        with mock.patch.object(status.urllib.request, "urlopen", side_effect=err):
            self.assertEqual(status.probe_once("https://x", "html"), ("down", "http 502"))

    def test_store_needs_version_header(self):
        self.assertFalse(status.judge("store", 200, {}, b"<html>"))
        self.assertTrue(status.judge("store", 200, {"x-webo-version": "5"}, b""))
        self.assertTrue(status.judge("health", 200, {}, b'{"status":"ok"}'))
        self.assertFalse(status.judge("health", 200, {}, b"<html>"))


class RenderTest(unittest.TestCase):
    def test_ok_page(self):
        doc, _ = status.apply(fresh(), ALL_OK, T0)
        page = status.render(doc)
        self.assertIn("모든 서비스가 정상입니다", page)
        self.assertIn("손님 홈페이지는 계속 열립니다", page)
        self.assertIn("최근 90일 동안 기록된 장애가 없습니다.", page)
        self.assertNotIn("<script", page)

    def test_api_down_page_warns_about_inquiry(self):
        doc, _ = status.apply(fresh(), {**ALL_OK, "api": "down"}, T0)
        doc, _ = status.apply(doc, {**ALL_OK, "api": "down"}, T0 + timedelta(minutes=10))
        page = status.render(doc)
        self.assertIn("관리 화면, AI 고치기, 게시가 지금 멈춰 있습니다", text(page))
        # 2차 9. 제목 속 기능 이름은 한 덩어리로 감싼다.
        self.assertIn('<span class="nm">AI 고치기</span>', page)
        self.assertIn("상담 신청이 접수되지 않을 수 있습니다", page)
        self.assertIn("진행 중", page)

    def test_site_down_page_does_not_promise_site(self):
        doc, _ = status.apply(fresh(), {**ALL_OK, "site": "down"}, T0)
        page = status.render(doc)
        self.assertIn("손님 홈페이지가 지금 열리지 않습니다", page)
        self.assertNotIn("손님 홈페이지는 계속 열립니다", page)

    # 규칙 5. 멈춘 칸에서 일부를 못 쟀으면 "일부만 확인했습니다"를 붙이고, 다 못 잰 칸은 4번 글을 붙인다.
    def test_partial_note_on_down_cell(self):
        doc, _ = status.apply(fresh(), ALL_OK, T0)
        doc, _ = status.apply(doc, {"app": "down", "api": "unknown", "site": "ok"}, T0 + timedelta(minutes=10))
        page = status.render(doc)
        cells = {c["id"]: part for c, part in zip(doc["components"], page.split('<li class="comp">')[1:])}
        self.assertIn("장애", cells["admin"])
        self.assertIn("일부만 확인했습니다.", cells["admin"])
        self.assertNotIn("항목", page)
        self.assertIn("이번 확인이 되지 않았습니다.", cells["ai"])
        self.assertNotIn("pending", cells["sites"])

    # 규칙 6. 기능 시각이 없으면 시각을 지어내지 않는다.
    def test_checking_without_component_time_has_no_time(self):
        doc = fresh()
        for comp in doc["components"]:
            comp.pop("checked_at", None)
        doc["components"][0]["pending"] = "all"
        page = text(status.render(doc))
        self.assertIn("이번 확인이 되지 않았습니다. 아직 확인한 기록이 없습니다.", page)
        self.assertNotIn("마지막으로 확인한 때", page)
        self.assertIn("이 페이지를 마지막으로 바꾼 때", page)
        self.assertIn("자동 확인이 잠시 되지 않았습니다", page)

    # 2차 5. 손님 사이트가 확인 중이면 "계속 열립니다"라고 단정하지 않는다.
    def test_site_checking_does_not_promise_site(self):
        doc, _ = status.apply(fresh(), ALL_OK, T0)
        doc, _ = status.apply(doc, {**ALL_OK, "site": "unknown"}, T0 + timedelta(minutes=10))
        page = status.render(doc)
        self.assertIn("손님 홈페이지가 열리는지 다시 확인하고 있습니다", page)
        self.assertNotIn("손님 홈페이지는 계속 열립니다", page)

    def test_text_is_escaped(self):
        doc = fresh()
        doc["incidents"] = [{
            "id": "x", "level": "down", "title": "<b>꺾쇠</b>", "components": ["admin"],
            "started_at": status.iso(T0), "resolved_at": None, "summary": "a&b",
        }]
        page = status.render(doc)
        self.assertIn("&lt;b&gt;꺾쇠&lt;/b&gt;", page)
        self.assertIn("a&amp;b", page)

    def test_josa(self):
        self.assertEqual(status.josa("관리 화면", "이", "가"), "관리 화면이")
        self.assertEqual(status.josa("AI 고치기·게시", "이", "가"), "AI 고치기·게시가")

    def test_one_day_reads_haru(self):
        doc = fresh()
        doc["history"] = {"2026-10-22": {"admin": "down", "ai": "ok"}}
        self.assertIn("기록한 하루 중 장애 하루", status.strip(doc, "admin", T0))
        self.assertIn("기록한 하루는 정상", status.strip(doc, "ai", T0))


# 4. 손으로 고친 status.json이 틀리면 고칠 자리를 알려 주고 멈춘다.
class ValidateTest(unittest.TestCase):
    def test_base_file_is_valid(self):
        status.validate(BASE)

    def test_bad_level_is_named(self):
        doc = fresh()
        doc["notice"] = {"level": "maint", "title": "점검", "components": ["admin"]}
        with self.assertRaisesRegex(status.StatusFileError, r"notice\.level.*'maint'"):
            status.validate(doc)
        doc = fresh()
        doc["components"][0]["state"] = "bad"
        with self.assertRaisesRegex(status.StatusFileError, r"components\[0\]\(admin\)\.state"):
            status.validate(doc)

    def test_time_needs_timezone_and_iso_shape(self):
        doc = fresh()
        doc["notice"] = {"level": "maintenance", "title": "점검", "components": ["admin"],
                         "starts_at": "2026-10-25T02:00"}
        with self.assertRaisesRegex(status.StatusFileError, r"notice\.starts_at.*시간대"):
            status.validate(doc)
        doc = fresh()
        doc["incidents"] = [{"id": "x", "level": "down", "title": "t", "components": ["admin"],
                             "started_at": "10월 25일 2시", "resolved_at": None}]
        with self.assertRaisesRegex(status.StatusFileError, r"incidents\[0\]\(x\)\.started_at.*읽지 못했습니다"):
            status.validate(doc)

    # 2차 6. 제목이 빠졌거나 영향 받는 곳이 목록이 아니면 고칠 자리를 알려 준다.
    def test_missing_title_and_bad_components_are_named(self):
        doc = fresh()
        doc["notice"] = {"level": "maintenance", "components": ["admin"]}
        with self.assertRaisesRegex(status.StatusFileError, r"notice\.title"):
            status.validate(doc)
        doc["notice"] = {"level": "maintenance", "title": "점검", "components": "admin"}
        with self.assertRaisesRegex(status.StatusFileError, r"notice\.components.*목록"):
            status.validate(doc)
        doc = fresh()
        doc["incidents"] = [{"id": "x", "level": "down", "title": " ", "components": ["admin"],
                             "started_at": status.iso(T0), "resolved_at": None}]
        with self.assertRaisesRegex(status.StatusFileError, r"incidents\[0\]\(x\)\.title"):
            status.validate(doc)
        del doc["incidents"][0]["title"]
        with self.assertRaisesRegex(status.StatusFileError, r"incidents\[0\]\(x\)\.title"):
            status.validate(doc)

    # 규칙 9. 공지 꼴, 제목, 기능 목록이 틀리면 고칠 자리를 알린다.
    def test_bad_shapes_are_named(self):
        inc = {"id": "x", "level": "down", "title": "t", "components": ["admin"],
               "started_at": status.iso(T0), "resolved_at": None}
        rows = [
            ({"notice": "점검"}, r"^notice는"),
            ({"notice": []}, r"^notice는"),
            ({"notice": {"level": "maintenance", "title": "", "components": ["admin"]}}, r"notice\.title"),
            ({"notice": {"level": "maintenance", "title": "점검", "components": [1]}}, r"notice\.components의 '1'"),
            ({"notice": {"level": "maintenance", "title": "점검", "components": ["adm"]}},
             r"notice\.components의 'adm'.*admin, ai, publish, sites"),
            ({"incidents": [dict(inc, components="admin")]}, r"incidents\[0\]\(x\)\.components.*목록"),
            ({"incidents": [dict(inc, components=["store"])]}, r"incidents\[0\]\(x\)\.components의 'store'"),
            ({"incidents": ["x"]}, r"incidents\[0\]는"),
        ]
        for change, pattern in rows:
            with self.subTest(pattern):
                doc = fresh()
                doc.update(change)
                with self.assertRaisesRegex(status.StatusFileError, pattern):
                    status.validate(doc)

    def test_main_stops_with_message(self):
        doc = fresh()
        doc["checked_at"] = "2026-10-22 12:00"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            path.write_text(json.dumps(doc, ensure_ascii=False), "utf-8")
            err = io.StringIO()
            with mock.patch.object(status, "STATUS", path), \
                    mock.patch.object(status, "write_index") as write_index, \
                    contextlib.redirect_stderr(err):
                self.assertEqual(status.main(["render"]), 2)
            write_index.assert_not_called()
            self.assertIn("status.json을 고쳐야 합니다. checked_at의 시각", err.getvalue())
            path.write_text("{", "utf-8")
            with mock.patch.object(status, "STATUS", path), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(status.main(["render"]), 2)


if __name__ == "__main__":
    unittest.main()
