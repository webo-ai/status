"""python3 -m unittest discover -s scripts -p "test_*.py" 로 돈다. 바깥 주소를 부르지 않는다."""

import contextlib
import copy
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


def comp_of(doc: dict, comp_id: str) -> dict:
    return next(c for c in doc["components"] if c["id"] == comp_id)


def at(hour: int, minute: int, day: int = 22) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=status.KST)


def notice(level, start, end, comps=("admin", "ai", "publish"), title="공지") -> dict:
    return {
        "level": level, "title": title, "body": "안내합니다.", "components": list(comps),
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


def cell(doc: dict, comp_id: str, ref=None) -> tuple[str, str | None]:
    """기능 칸에 그린 (상태 글, 마지막 확인 글)."""
    page = status.render(doc, ref)
    parts = dict(zip([c["id"] for c in doc["components"]], page.split('<li class="comp">')[1:]))
    label = re.search(r'<p class="state is-\w+"><svg.*?</svg>(.*?)</p>', parts[comp_id]).group(1)
    note = re.search(r'<p class="last">(.*?)</p>', parts[comp_id])
    return label, note and note.group(1)


def strip_cells(doc: dict, comp_id: str) -> dict[str, str]:
    """30일 띠에서 칠한 칸만 {"월/일": 상태 글}로."""
    page = status.strip(doc, comp_id, status.parse(doc["checked_at"]))
    found = re.findall(r'title="(\d+)월 (\d+)일 ([^"]+)"', page)
    return {f"{m}/{d}": label for m, d, label in found if label != "기록 없음"}


def auto_incidents(doc: dict, comp_id: str) -> list[tuple[str, str | None]]:
    """그 기능이 든 자동 장애 기록의 (시작, 닫힘)."""
    return [(i["started_at"], i["resolved_at"]) for i in doc["incidents"]
            if i.get("auto") and comp_id in i["components"]]


def hl(page: str) -> str:
    return text(re.search(r'<span class="hl">(.*?)</span></h1>', page).group(1))


I = status.iso
DOWN, UNK = {"api": "down"}, {"api": "unknown"}
STALE_NOTE = "자동 확인이 1시간 넘게 되지 않은 기능이 있습니다"


def stale_note(*names) -> str:
    return f"{STALE_NOTE}: {', '.join(names)}. 그 기능은 마지막으로 확인한 때 상태입니다."


def unk(start, minutes):
    """start부터 10분마다 minutes분까지 AI 고치기를 못 잰 판들."""
    return [(start + timedelta(minutes=m), UNK) for m in range(10, minutes + 1, 10)]

# 6차 규칙 1~7을 판 차례로 건다. (이름, 볼 기능, 판 차례, 자동 기록 [(시작, 닫힘)], 띠 칸, 칸 글)
SCENARIOS = [
    # 규칙 1. 못 잰 판은 그 기능에 없던 판이다. 상태·시각·셈·띠를 안 바꾼다.
    ("1 down, 못 잼, down 은 이어 센다", "ai",
     [(at(12, 0), {}), (at(12, 10), DOWN), (at(12, 20), UNK), (at(12, 30), DOWN)],
     [(I(at(12, 10)), None)], {"10/22": "장애"}, ("장애", None)),
    ("1 down, 못 잼, ok", "ai",
     [(at(12, 0), {}), (at(12, 10), DOWN), (at(12, 20), UNK), (at(12, 30), {})],
     [], {"10/22": "정상"}, ("정상", None)),
    ("1 정상 뒤 못 잰 판은 정상 그대로", "ai",
     [(at(12, 0), {}), (at(12, 10), UNK)],
     [], {"10/22": "정상"}, ("정상", None)),
    ("1 장애 뒤 못 잰 판은 장애 그대로이고 기록을 안 닫는다", "ai",
     [(at(12, 0), DOWN), (at(12, 10), DOWN), (at(12, 20), UNK)],
     [(I(at(12, 0)), None)], {"10/22": "장애"}, ("장애", None)),
    ("1 한 번도 못 잰 기능", "ai",
     [(at(12, 10), UNK)],
     [], {}, ("정상", None)),
    ("1 관리 화면은 한쪽이 멈추면 다른 쪽을 못 재도 장애", "admin",
     [(at(12, 0), {}), (at(12, 10), {"app": "down", "api": "unknown"})],
     [], {"10/22": "정상"}, ("장애", None)),
    ("1 관리 화면 한쪽만 못 재면 못 잰 판", "admin",
     [(at(12, 0), {}), (at(12, 10), {"app": "unknown"})],
     [], {"10/22": "정상"}, ("정상", None)),
    # 공지 판의 셈 되돌리기와 띠 칠하기는 공지를 따른다. 그 기능을 못 잰 판에도 한다.
    ("1 점검 공지 판은 못 재도 점검으로 칠한다", "ai",
     [(at(12, 0), {}), (at(12, 10), UNK, notice("maintenance", at(12, 5), at(13, 0)))],
     [], {"10/22": "점검"}, ("점검", None)),
    ("1 점검 공지 판은 못 재도 실패 수를 0으로", "ai",
     [(at(12, 0), DOWN), (at(12, 10), UNK, notice("maintenance", at(12, 5), at(12, 15))), (at(12, 20), DOWN)],
     [], {"10/22": "점검"}, ("장애", None)),
    # 규칙 2. 마지막 확인이 70분보다 오래된 칸에만 시각을 붙인다(7차-1).
    ("2 꼭 70분 못 재면 아직 시각을 안 붙인다", "ai",
     [(at(12, 0), {})] + unk(at(12, 0), 70),
     [], {"10/22": "정상"}, ("정상", None)),
    ("2 80분 못 재면 마지막 확인 시각을 붙인다", "ai",
     [(at(12, 0), {})] + unk(at(12, 0), 80),
     [], {"10/22": "정상"}, ("정상", "마지막 확인 오후 12:00")),
    ("2 멈춘 채 80분 못 재도 장애 칸에 시각만 붙인다", "ai",
     [(at(12, 0), DOWN), (at(12, 10), DOWN)] + unk(at(12, 10), 80),
     [(I(at(12, 0)), None)], {"10/22": "장애"}, ("장애", "마지막 확인 오후 12:10")),
    ("2 날이 바뀌면 날짜까지 적는다", "ai",
     [(at(23, 0), {}), (at(0, 20, 23), UNK)],
     [], {"10/22": "정상"}, ("정상", "마지막 확인 10월 22일 (목) 오후 11:00")),
    ("7차-1 점검 공지가 걸린 기능은 오래 못 재도 시각을 안 붙인다", "ai",
     [(at(12, 0), {}), (at(12, 10), UNK, notice("maintenance", at(12, 5), at(15, 0)))] + unk(at(12, 10), 80),
     [], {"10/22": "점검"}, ("점검", None)),
    # 규칙 3. 셈.
    ("3 down, ok, down, ok 한 번 실패하고 돌아온 날은 정상", "ai",
     [(at(12, 0), DOWN), (at(12, 10), {}), (at(12, 20), DOWN), (at(12, 30), {})],
     [], {"10/22": "정상"}, ("정상", None)),
    ("3 한 번 실패만 한 날도 잰 날이라 정상", "ai",
     [(at(12, 0), DOWN)],
     [], {"10/22": "정상"}, ("장애", None)),
    ("3 앞 실패가 꼭 30분 전이면 잇는다", "ai",
     [(at(12, 0), DOWN), (at(12, 10), UNK), (at(12, 20), UNK), (at(12, 30), DOWN)],
     [(I(at(12, 0)), None)], {"10/22": "장애"}, ("장애", None)),
    ("3 사이가 못 잰 판이고 앞 실패가 40분 전이면 1부터", "ai",
     [(at(12, 0), DOWN), (at(12, 10), UNK), (at(12, 20), UNK), (at(12, 30), UNK), (at(12, 40), DOWN)],
     [], {"10/22": "정상"}, ("장애", None)),
    ("3 예약이 밀려 40분 벌어져도 1부터", "ai",
     [(at(12, 0), DOWN), (at(12, 40), DOWN)],
     [], {"10/22": "정상"}, ("장애", None)),
    ("3 다시 센 첫 실패가 시작이다", "ai",
     [(at(12, 0), DOWN), (at(12, 40), DOWN), (at(12, 50), DOWN)],
     [(I(at(12, 40)), None)], {"10/22": "장애"}, ("장애", None)),
    ("3 기록이 열린 뒤엔 오래 못 재도 다시 세지 않는다", "ai",
     [(at(12, 0), DOWN), (at(12, 10), DOWN), (at(12, 20), UNK), (at(12, 30), UNK), (at(12, 40), UNK),
      (at(12, 50), DOWN)],
     [(I(at(12, 0)), None)], {"10/22": "장애"}, ("장애", None)),
    ("3 점검 공지 판은 세지 않고, 끝난 뒤 두 번이면 연다", "ai",
     [(at(12, 0), DOWN), (at(12, 10), DOWN, notice("maintenance", at(12, 5), at(12, 25))),
      (at(12, 20), DOWN), (at(12, 30), DOWN), (at(12, 40), DOWN)],
     [(I(at(12, 30)), None)], {"10/22": "장애"}, ("장애", None)),
    ("3 점검 끝 뒤 실패 한 번, 정상", "ai",
     [(at(12, 0), DOWN, notice("maintenance", at(11, 30), at(12, 15))), (at(12, 10), DOWN),
      (at(12, 20), DOWN), (at(12, 30), {})],
     [], {"10/22": "점검"}, ("정상", None)),
    ("3 공지 끝 시각에 잰 판은 공지 밖으로 센다", "ai",
     [(at(12, 0), DOWN, notice("maintenance", at(11, 30), at(12, 10))), (at(12, 10), DOWN), (at(12, 20), DOWN)],
     [(I(at(12, 10)), None)], {"10/22": "장애"}, ("장애", None)),
    ("3 장애 공지 판도 세지 않는다", "ai",
     [(at(12, 0), DOWN, notice("down", at(11, 55), None, ("ai",))), (at(12, 10), DOWN), (at(12, 20), DOWN)],
     [], {"10/22": "장애"}, ("장애", None)),
    ("3 공지 전 실패 수는 공지를 걸린 채 지워도 안 살아난다", "ai",
     [(at(12, 0), DOWN), (at(12, 10), DOWN, notice("maintenance", at(12, 5), at(13, 0))),
      (at(12, 20), DOWN, None), (at(12, 30), DOWN)],
     [(I(at(12, 20)), None)], {"10/22": "장애"}, ("장애", None)),
    ("3 느림 공지 중 down 두 번은 기록을 열고 칸은 느림", "ai",
     [(at(12, 0), DOWN, notice("slow", at(11, 55), None, ("ai",))), (at(12, 10), DOWN)],
     [(I(at(12, 0)), None)], {"10/22": "장애"}, ("느림", None)),
    ("3 정상 공지 중 down 두 번도 기록을 연다", "ai",
     [(at(12, 0), DOWN, notice("ok", at(11, 55), None, ("ai",))), (at(12, 10), DOWN)],
     [(I(at(12, 0)), None)], {"10/22": "장애"}, ("정상", None)),
    ("3 느림 두 번은 기록을 열지 않는다", "ai",
     [(at(12, 0), {"api": "slow"}), (at(12, 10), {"api": "slow"})],
     [], {"10/22": "정상"}, ("느림", None)),
    ("3 점검 공지 판의 정상은 기록을 닫지 않는다", "ai",
     [(at(12, 0), DOWN), (at(12, 10), DOWN), (at(12, 20), {}, notice("maintenance", at(12, 15), at(13, 0))),
      (at(12, 30), {}), (at(13, 0), {})],
     [(I(at(12, 0)), I(at(13, 0)))], {"10/22": "장애"}, ("정상", None)),
    ("3 못 잰 판은 기록을 닫지 않는다", "ai",
     [(at(12, 0), DOWN), (at(12, 10), DOWN), (at(12, 20), UNK), (at(12, 30), {})],
     [(I(at(12, 0)), I(at(12, 30)))], {"10/22": "장애"}, ("정상", None)),
    ("3 느림 공지 판의 ok는 닫고 그날 띠는 정상", "ai",
     [(at(12, 0), DOWN), (at(12, 10), DOWN), (at(0, 4, 23), {}, notice("slow", at(0, 0, 23), None, ("ai",)))],
     [(I(at(12, 0)), I(at(0, 4, 23)))], {"10/22": "장애", "10/23": "정상"}, ("느림", None)),
    ("3 자정 넘김", "ai",
     [(at(23, 55), DOWN), (at(0, 5, 23), DOWN), (at(0, 15, 23), {})],
     [(I(at(23, 55)), I(at(0, 15, 23)))], {"10/22": "장애", "10/23": "장애"}, ("정상", None)),
    ("3 자정 넘긴 점검 뒤 down 두 번", "ai",
     [(at(23, 30), DOWN, notice("maintenance", at(23, 20), at(0, 15, 23)))]
     + [(at(23, 30) + timedelta(minutes=10 * k), DOWN) for k in range(1, 7)],
     [(I(at(0, 20, 23)), None)], {"10/22": "점검", "10/23": "장애"}, ("장애", None)),
    # 규칙 4. 칸은 공지가 걸리면 공지 등급, 아니면 점검·장애 공지 밖에서 마지막으로 잰 값이다(7차-3).
    ("7차-3 점검 중 잰 down은 공지 뒤 칸 값이 아니다", "ai",
     [(at(12, 0), {}), (at(12, 10), DOWN, notice("maintenance", at(12, 5), at(12, 25))),
      (at(12, 20), DOWN), (at(12, 30), UNK)],
     [], {"10/22": "점검"}, ("정상", None)),
    ("7차-3 장애 공지 중 잰 down도 공지 뒤 칸 값이 아니다", "ai",
     [(at(12, 0), {}), (at(12, 10), DOWN, notice("down", at(12, 5), at(12, 25), ("ai",))),
      (at(12, 20), DOWN), (at(12, 30), UNK)],
     [], {"10/22": "장애"}, ("정상", None)),
    ("7차-3 점검 전 장애는 점검 중 ok를 재도 공지 뒤 장애이고 기록도 안 닫는다", "ai",
     [(at(12, 0), DOWN), (at(12, 10), DOWN), (at(12, 20), {}, notice("maintenance", at(12, 15), at(12, 35))),
      (at(12, 30), {}), (at(12, 40), UNK)],
     [(I(at(12, 0)), None)], {"10/22": "장애"}, ("장애", None)),
    ("7차-3 느림 공지 중 잰 값은 그대로 칸 값이다", "ai",
     [(at(12, 0), {}), (at(12, 10), DOWN, notice("slow", at(12, 5), at(12, 25), ("ai",))), (at(12, 30), UNK)],
     [], {"10/22": "정상"}, ("장애", None)),
    # 7차-4. 점검·장애 공지 판은 그 기능을 못 재도 셈을 0으로 되돌리고 띠를 공지 등급으로 칠한다.
    ("7차-4 장애 공지 판은 못 재도 실패 수를 0으로 하고 장애로 칠한다", "ai",
     [(at(12, 0), DOWN), (at(12, 10), UNK, notice("down", at(12, 5), at(12, 15), ("ai",))), (at(12, 20), DOWN)],
     [], {"10/22": "장애"}, ("장애", None)),
    ("4 점검 공지 칸", "ai",
     [(at(12, 0), {}, notice("maintenance", at(11, 55), at(13, 0)))],
     [], {"10/22": "점검"}, ("점검", None)),
    ("4 장애 공지가 걸리면 재서 정상이어도 장애", "ai",
     [(at(12, 0), {}, notice("down", at(11, 55), None, ("ai",))), (at(12, 10), {})],
     [], {"10/22": "장애"}, ("장애", None)),
    ("4 느림 공지 중 down 뒤 못 잼", "ai",
     [(at(12, 0), DOWN, notice("slow", at(11, 55), None, ("ai",))), (at(12, 10), UNK)],
     [], {"10/22": "정상"}, ("느림", None)),
    ("4 느림 공지가 끝난 뒤 못 잼", "ai",
     [(at(12, 0), DOWN, notice("slow", at(11, 55), at(12, 5), ("ai",))), (at(12, 10), UNK)],
     [], {"10/22": "정상"}, ("장애", None)),
    # 규칙 7. 자동 기록이 열려 있던 날은 그 기록에 든 기능을 다 장애로 칠한다.
    ("7 기록이 열린 동안엔 먼저 돌아온 기능도 그날을 장애로", "ai",
     [(at(23, 40), DOWN), (at(23, 50), DOWN), (at(0, 5, 23), {"app": "down"})],
     [(I(at(23, 40)), None)], {"10/22": "장애", "10/23": "장애"}, ("정상", None)),
    ("7 느림 공지 중 잰 ok·slow는 정상으로 칠한다", "ai",
     [(at(12, 0), {}, notice("slow", at(11, 55), None, ("ai",))), (at(12, 10), {"api": "slow"})],
     [], {"10/22": "정상"}, ("느림", None)),
]


class ScenarioTest(unittest.TestCase):
    def test_rules_by_round_order(self):
        for name, comp_id, steps, incidents, days, shown in SCENARIOS:
            with self.subTest(name):
                doc = play(steps)
                self.assertEqual(auto_incidents(doc, comp_id), incidents)
                self.assertEqual(strip_cells(doc, comp_id), days)
                self.assertEqual(cell(doc, comp_id), shown)
                page = text(status.render(doc))
                for gone in ("확인 중", "다시 확인하고", "지금 멈춰", "멈췄다는 뜻은"):
                    self.assertNotIn(gone, page)
                # 단정하지 않는 글은 오래 못 잰 안내가 뜬 판에만 나온다(7차-2).
                self.assertEqual("마지막 확인 때" in page, STALE_NOTE in page)

    # 규칙 1·3. 실패 수와 첫 실패 시각은 기능 칸에 저장하고, 못 잰 판은 시각도 안 올린다.
    def test_streak_and_time_are_stored_on_component(self):
        doc = play([(at(12, 0), DOWN)])
        self.assertEqual((comp_of(doc, "ai")["streak"], comp_of(doc, "ai")["streak_since"]), (1, I(at(12, 0))))
        doc = play([(at(12, 10), UNK)], doc)
        ai = comp_of(doc, "ai")
        self.assertEqual((ai["streak"], ai["streak_since"], ai["checked_at"]), (1, I(at(12, 0)), I(at(12, 0))))
        doc = play([(at(12, 20), {"api": "slow"})], doc)
        self.assertEqual((comp_of(doc, "ai")["streak"], comp_of(doc, "ai")["streak_since"]), (0, None))
        doc = play([(at(12, 30), DOWN), (at(12, 40), {}, notice("maintenance", at(12, 35), None))], doc)
        ai = comp_of(doc, "ai")
        # 점검 중에 잰 판은 시각도 칸 값도 안 올린다(7차-3).
        self.assertEqual((ai["streak"], ai["streak_since"], ai["checked_at"], ai["state"]),
                         (0, None, I(at(12, 30)), "down"))
        for key in ("pending", "counted_at", "streak_missed"):
            self.assertNotIn(key, ai)


def real_run(doc, rounds):
    """실제로는 새로 쓰지 않은 판의 문서는 디스크에 안 남는다. 그 흐름대로 돌리고 판마다 새로 썼는지 돌려준다."""
    writes = []
    for when, results in rounds:
        new, write = status.apply(doc, {**ALL_OK, **results}, when)
        writes.append(write)
        if write:
            doc = new
    return doc, writes


class StaleNoteTest(unittest.TestCase):
    """규칙 2. 마지막 확인이 70분보다 오래된 기능이 있으면 첫 화면에 기능 이름을 넣은 안내 한 줄(7차-1)."""

    def test_note_and_rewrite_follow_real_run(self):
        doc, _ = status.apply(fresh(), ALL_OK, at(12, 0))
        doc, writes = real_run(doc, [(at(12, 0) + timedelta(minutes=m), UNK) for m in range(10, 91, 10)])
        # 13:00은 55분 시각 갱신, 13:20은 안내가 뜨는 판이다. 70분째인 13:10엔 아직 안 뜬다.
        self.assertEqual(writes, [False, False, False, False, False, True, False, True, False])
        page = text(status.render(doc))
        want = stale_note("관리 화면", "AI 고치기", "게시")
        self.assertIn(want, page)
        self.assertEqual(page.count(STALE_NOTE), 1)
        self.assertEqual(cell(doc, "ai"), ("정상", "마지막 확인 오후 12:00"))
        self.assertEqual(cell(doc, "sites"), ("정상", None))
        # 다시 재지면 그 판에 새로 쓰고 안내가 사라진다.
        doc, write = status.apply(doc, ALL_OK, at(13, 40))
        self.assertTrue(write)
        self.assertNotIn(STALE_NOTE, text(status.render(doc)))

    # 30분 기준이면 거짓으로 뜨던 판. 다 쟀지만 55분마다만 써서 저장된 기능 시각이 이르다.
    def test_heartbeat_gap_alone_does_not_raise_note(self):
        doc, _ = status.apply(fresh(), ALL_OK, at(12, 0))
        doc, writes = real_run(doc, [(at(12, m), {}) for m in range(10, 51, 10)] + [(at(13, 0), UNK)])
        self.assertEqual(writes, [False] * 5 + [True])
        self.assertEqual(comp_of(doc, "ai")["checked_at"], I(at(12, 0)))
        self.assertNotIn(STALE_NOTE, text(status.render(doc)))
        # 그다음 판도 못 재면 70분이 안 넘어 아직 안 뜨고, 80분째 판에 뜬다.
        doc, writes = real_run(doc, [(at(13, 10), UNK), (at(13, 20), UNK)])
        self.assertEqual(writes, [False, True])
        self.assertIn(stale_note("관리 화면", "AI 고치기", "게시"), text(status.render(doc)))

    # 공지가 걸린 기능은 칸이 공지 등급이라 안내에서 뺀다. 공지가 끝나면 다시 든다.
    def test_noticed_component_is_left_out(self):
        doc = play([(at(12, 0), {}), (at(12, 10), {"app": "unknown", "api": "unknown", "site": "unknown"},
                                      notice("maintenance", at(12, 5), at(13, 30)))]
                   + [(at(12, 10) + timedelta(minutes=m), {"app": "unknown", "api": "unknown", "site": "unknown"})
                      for m in range(10, 71, 10)])
        page = text(status.render(doc))
        self.assertIn(stale_note("손님 사이트"), page)
        self.assertNotIn("마지막 확인 오후", text(status.render(doc).split('<li class="comp">')[1]))
        # 점검 끝난 직후. 다시 재기 전에 그리면 점검 전 값이 오래됐다고 알린다.
        page = text(status.render(doc, at(13, 31)))
        self.assertIn(stale_note("관리 화면", "AI 고치기", "게시", "손님 사이트"), page)
        self.assertEqual(cell(doc, "ai", at(13, 31)), ("정상", "마지막 확인 오후 12:00"))
        self.assertEqual(cell(doc, "ai"), ("점검", None))
        # 공지에 든 기능만 오래 못 쟀으면 안내가 안 뜬다. 느림 공지도 칸이 공지 등급이라 같다.
        for level in ("maintenance", "down", "slow"):
            with self.subTest(level):
                doc = play([(at(12, 0), {}), (at(12, 10), UNK, notice(level, at(12, 5), None, ("ai", "admin", "publish")))]
                           + unk(at(12, 10), 80))
                self.assertNotIn(STALE_NOTE, text(status.render(doc)))
                self.assertEqual(status.stale_ids(doc), [])

    # 7차-2. 안내가 뜬 판엔 첫 화면 제목·안내 글·손님 홈페이지 칸을 단정하지 않는다.
    def test_note_makes_headline_and_site_card_unsure(self):
        late = [(at(12, 0), {})] + unk(at(12, 0), 80)
        rows = [
            ("정상", late, "마지막 확인 때 모든 서비스가 정상이었습니다",
             "마지막 확인 때는 관리 화면부터 손님 사이트까지 평소처럼 돌아갔습니다.",
             "is-calm", "손님 홈페이지는 마지막 확인 때 열렸습니다"),
            ("멈춤", [(at(12, 0), DOWN)] + unk(at(12, 0), 80), "관리 화면, AI 고치기, 게시가 마지막 확인 때 멈춰 있었습니다",
             "원인을 찾고 있습니다. 바뀌는 대로 이 페이지에 알려 드립니다.",
             "is-calm", "손님 홈페이지는 마지막 확인 때 열렸습니다"),
            ("느림", [(at(12, 0), {"api": "slow"})] + unk(at(12, 0), 80),
             "관리 화면, AI 고치기, 게시가 마지막 확인 때 평소보다 느렸습니다",
             "마지막 확인 때는 열리기는 했지만 평소보다 오래 걸렸습니다.", "is-calm", "마지막 확인 때 열렸습니다"),
            ("손님 사이트 멈춤", [(at(12, 0), {"site": "down"})]
             + [(at(12, 0) + timedelta(minutes=m), {"site": "unknown"}) for m in range(10, 81, 10)],
             "손님 사이트가 마지막 확인 때 멈춰 있었습니다", "원인을 찾고 있습니다.",
             "is-down", "손님 홈페이지가 마지막 확인 때 열리지 않았습니다"),
            ("손님 사이트 느림", [(at(12, 0), {"site": "slow"})]
             + [(at(12, 0) + timedelta(minutes=m), {"site": "slow", "api": "unknown"}) for m in range(10, 81, 10)],
             "손님 사이트가 마지막 확인 때 평소보다 느렸습니다", "마지막 확인 때는", "is-neutral", "잠시 불편할 수 있습니다"),
        ]
        for name, steps, title, lead, tone, site in rows:
            with self.subTest(name):
                page = status.render(play(steps))
                self.assertIn(STALE_NOTE, text(page))
                self.assertEqual(hl(page), title)
                self.assertIn(lead, text(re.search(r'<p class="lead">.*?</p>', page).group(0)))
                card = re.search(r'<section class="card site-card (is-\w+)".*?</section>', page)
                self.assertEqual(card.group(1), tone)
                self.assertIn(site, text(card.group(0)))
                for sure in ("정상입니다", "멈춰 있습니다", "느립니다", "계속 열립니다", "지금 열리지"):
                    self.assertNotIn(sure, text(page).split("안 될 때는")[0])
        # 안내가 없으면 예전처럼 단정한다.
        page = status.render(play([(at(12, 0), {})] + unk(at(12, 0), 70)))
        self.assertEqual(hl(page), "모든 서비스가 정상입니다")
        self.assertIn("손님 홈페이지는 계속 열립니다", page)
        # 점검 공지 제목은 공지에서 온 글이라 그대로 둔다.
        page = status.render(play([(at(12, 0), {}), (at(12, 10), {"site": "unknown"},
                                   notice("maintenance", at(12, 5), at(15, 0), title="관리 화면 정기 점검"))]
                                  + [(at(12, 10) + timedelta(minutes=m), {"site": "unknown"}) for m in range(10, 81, 10)]))
        self.assertIn(stale_note("손님 사이트"), text(page))
        self.assertEqual(hl(page), "관리 화면 정기 점검")
        self.assertIn("손님 홈페이지는 마지막 확인 때 열렸습니다", page)

    def test_no_note_while_measured_inside_heartbeat(self):
        # 다 잰 판인데 55분 안이라 안 쓴 경우. 디스크 문서의 기능 시각은 문서 시각과 같아 안내가 안 뜬다.
        doc, _ = status.apply(fresh(), ALL_OK, at(12, 0))
        _, write = status.apply(doc, ALL_OK, at(12, 50))
        self.assertFalse(write)
        self.assertNotIn(STALE_NOTE, text(status.render(doc, at(12, 50))))

    def test_never_measured_has_no_time(self):
        doc = fresh()
        for comp in doc["components"]:
            comp.pop("checked_at", None)
        doc, _ = status.apply(doc, {**ALL_OK, "app": "unknown"}, at(13, 10))
        self.assertNotIn("checked_at", comp_of(doc, "admin"))
        page = text(status.render(doc))
        self.assertNotIn("마지막 확인", page)
        self.assertNotIn(STALE_NOTE, page)


class HeadlineTest(unittest.TestCase):
    # 규칙 5. 멈춤은 묶어 "OO이 멈춰 있습니다", 점검은 공지 제목, 다 정상이면 정상 문장.
    def test_headline_by_cell(self):
        maint = notice("maintenance", at(11, 55), at(13, 0), title="관리 화면 정기 점검")
        rows = [
            ([(at(12, 0), {})], "모든 서비스가 정상입니다"),
            ([(at(12, 0), DOWN)], "관리 화면, AI 고치기, 게시가 멈춰 있습니다"),
            ([(at(12, 0), DOWN), (at(12, 10), DOWN), (at(12, 20), {"app": "unknown", "api": "unknown"})],
             "관리 화면, AI 고치기, 게시가 멈춰 있습니다"),
            ([(at(12, 0), {"site": "down"})], "손님 사이트가 멈춰 있습니다"),
            ([(at(12, 0), {}, maint)], "관리 화면 정기 점검"),
            ([(at(12, 0), {"site": "down"}, maint)], "손님 사이트가 멈춰 있습니다"),
            ([(at(12, 0), {"api": "slow"})], "관리 화면, AI 고치기, 게시가 평소보다 느립니다"),
        ]
        for steps, want in rows:
            with self.subTest(want):
                self.assertEqual(hl(status.render(play(steps))), want)

    def test_down_names_are_chunks(self):
        page = status.render(play([(at(12, 0), DOWN)]))
        self.assertIn('<span class="nm">AI 고치기</span>', page)
        self.assertIn("원인을 찾아 고치고 있습니다.", page)


class SiteCardTest(unittest.TestCase):
    # 규칙 6. 손님 홈페이지 칸은 정상 글, 중립 글, 멈춤 글 셋뿐이다.
    def test_site_card_by_site_cell(self):
        calm = ("is-calm", "손님 홈페이지는 계속 열립니다")
        neutral = ("is-neutral", "손님 홈페이지가 잠시 불편할 수 있습니다")
        down = ("is-down", "손님 홈페이지가 지금 열리지 않습니다")
        rows = [
            ("정상", [(at(12, 0), {})], calm),
            ("정상 뒤 못 잼", [(at(12, 0), {}), (at(12, 10), {"site": "unknown"})], calm),
            ("느림", [(at(12, 0), {"site": "slow"})], neutral),
            ("점검", [(at(12, 0), {}, notice("maintenance", at(11, 55), None, ("sites",)))], neutral),
            ("멈춤", [(at(12, 0), {"site": "down"})], down),
            ("멈춤 뒤 못 잼", [(at(12, 0), {"site": "down"}), (at(12, 10), {"site": "unknown"})], down),
        ]
        for name, steps, (tone, title) in rows:
            with self.subTest(name):
                page = status.render(play(steps))
                card = re.search(r'<section class="card site-card (is-\w+)".*?</section>', page)
                self.assertEqual(card.group(1), tone)
                self.assertIn(title, card.group(0))
                if tone != "is-calm":
                    self.assertNotIn("계속 열립니다", card.group(0))

    # 7차-6. 첫 화면이 점검 공지 제목·본문을 보이면 '지금 안내' 칸은 기간과 영향 기능만 적는다.
    def test_notice_card_does_not_repeat_hero(self):
        maint = dict(notice("maintenance", at(11, 55), at(13, 0), title="관리 화면 정기 점검"), body="더 빠르게 바꿉니다.")

        def card(page):
            found = re.search(r'<section class="card notice".*?</section>', page)
            return found and text(found.group(0))

        page = status.render(play([(at(12, 0), {}, maint)]))
        self.assertEqual(hl(page), "관리 화면 정기 점검")
        self.assertEqual(text(page).count("관리 화면 정기 점검"), 1)
        self.assertEqual(text(page).count("더 빠르게 바꿉니다."), 1)
        self.assertEqual(card(page), "지금 안내점검 시간과 영향 받는 기능"
                         "10월 22일 (목) 오전 11:55부터 10월 22일 (목) 오후 1:00까지영향 받는 곳: 관리 화면·AI 고치기·게시")
        # 기간이 없으면 영향 기능만 적는다.
        page = status.render(play([(at(12, 0), {}, dict(maint, starts_at=None, ends_at=None))]))
        self.assertEqual(hl(page), "관리 화면 정기 점검")
        self.assertEqual(card(page), "지금 안내점검 시간과 영향 받는 기능영향 받는 곳: 관리 화면·AI 고치기·게시")
        # 첫 화면이 공지를 안 쓰는 판은 칸에 제목·본문을 그대로 적는다.
        rows = [
            ("예정", [(at(11, 0), {}, maint)]),
            ("손님 사이트가 멈춰 첫 화면이 멈춤", [(at(12, 0), {"site": "down"}, maint)]),
            ("느림 공지", [(at(12, 0), {}, dict(maint, level="slow"))]),
        ]
        for name, steps in rows:
            with self.subTest(name):
                page = status.render(play(steps))
                self.assertIn("관리 화면 정기 점검", card(page))
                self.assertIn("더 빠르게 바꿉니다.", card(page))

    def test_publish_stop_warns_about_inquiry(self):
        page = status.render(play([(at(12, 0), DOWN), (at(12, 10), DOWN)]))
        self.assertIn("상담 신청이 접수되지 않을 수 있습니다", page)
        self.assertIn("진행 중", page)


class KeptTest(unittest.TestCase):
    """규칙 8. 걷어 내면서도 남긴 것."""

    # 공지가 걸리거나 풀린 판은 등급과 상관없이 새로 쓴다.
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

    # 기능이 없는 공지는 칸을 안 바꿔서 그 까닭만으로는 새로 쓰지 않는다(README와 맞춘다).
    def test_notice_without_components_does_not_force_write(self):
        doc, _ = status.apply(fresh(), ALL_OK, at(12, 0))
        doc["notice"] = notice("maintenance", at(12, 5), at(12, 25), ())
        _, write = status.apply(doc, ALL_OK, at(12, 10))
        self.assertFalse(write)
        self.assertIn("기능이 든 공지가 걸리거나 풀린 판은", (status.ROOT / "README.md").read_text("utf-8"))

    # render(doc, now). 그리기만 돌릴 때도 공지가 걸렸는지는 지금 시각으로 본다.
    def test_render_command_uses_current_time(self):
        rows = [
            (notice("maintenance", at(12, 5), at(13, 0)), "지금 안내", "예정된 안내"),
            (notice("maintenance", at(11, 0), at(12, 5)), "모든 서비스가 정상입니다", "점검 중"),
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

    # validate(). 목록·묶음 꼴과 글자 칸을 보고, 비었으면 빈 글로 그린다.
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

    # auto 기록만 다시 연다. 사람이 닫은 같은 id 자동 기록은 새로 열지 않고 다시 연다.
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
        # 사람이 쓴 같은 id 기록은 건드리지 않고 자동 기록을 따로 연다.
        hand = {"id": "20261022-1200", "auto": False, "level": "maintenance", "title": "손으로 쓴 점검 기록",
                "components": ["admin"], "started_at": I(at(11, 0)), "resolved_at": I(at(11, 30)), "summary": "사람"}
        doc = play([(at(12, 0), DOWN)])
        doc["incidents"] = [copy.deepcopy(hand)]
        doc = play([(at(12, 10), DOWN)], doc)
        self.assertEqual(doc["incidents"][1], hand)
        self.assertTrue(doc["incidents"][0]["auto"])
        self.assertEqual(doc["incidents"][0]["started_at"], I(at(12, 0)))
        self.assertIsNone(doc["incidents"][0]["resolved_at"])

    # 메일 주소는 @ 뒤에서 먼저 줄을 바꾼다.
    def test_mail_breaks_after_at(self):
        self.assertIn("<span>webomni.team@<wbr>gmail.com</span>", status.render(fresh()))

    # 띠 설명과 첫 화면 시각 이름.
    def test_page_words(self):
        page = text(status.render(play([(at(12, 0), {})])))
        self.assertIn("자동 확인에서 장애로 기록된 날과 장애 안내가 있던 날을 장애로 칠합니다.", page)
        self.assertIn("이 페이지를 마지막으로 바꾼 때", page)


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

    # 장애 기록은 두 번 잇달아 실패해야 연다.
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

    # 공지는 기록을 닫지 않는다. 공지가 끝난 뒤 잰 정상이 닫는다.
    def test_notice_does_not_resolve_open_incident(self):
        both = {"app": "down", "api": "down", "site": "ok"}
        doc, _ = status.apply(fresh(), both, T0)
        doc, _ = status.apply(doc, both, T0 + timedelta(minutes=10))
        self.assertIsNone(doc["incidents"][0]["resolved_at"])
        doc["notice"] = notice("maintenance", T0 + timedelta(minutes=15), T0 + timedelta(hours=2))
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

    # 한 번 실패한 다음 판을 봇 검사로 못 쟀으면 두 번째 실패로 세지 않는다.
    def test_down_then_unknown_opens_no_incident(self):
        doc, _ = status.apply(fresh(), {**ALL_OK, "api": "down"}, T0)
        doc, _ = status.apply(doc, {**ALL_OK, "api": "unknown"}, T0 + timedelta(minutes=10))
        self.assertEqual(state_of(doc, "ai"), "down")
        self.assertEqual(doc["incidents"], [])
        self.assertEqual(doc["history"]["2026-10-22"], {"admin": "ok", "ai": "ok", "publish": "ok", "sites": "ok"})
        # 그다음 판에 실제로 다시 멈춘 게 재지면 그때 연다.
        doc, _ = status.apply(doc, {**ALL_OK, "api": "down"}, T0 + timedelta(minutes=20))
        self.assertEqual(len(doc["incidents"]), 1)
        self.assertEqual(doc["incidents"][0]["started_at"], status.iso(T0))

    # 한 번 실패하고 돌아온 날은 띠에 장애로 적지 않고, 기록이 열리면 처음 멈춘 날까지 거슬러 적는다.
    def test_single_failure_day_is_not_down_in_history(self):
        doc, _ = status.apply(fresh(), ALL_OK, T0)
        doc, _ = status.apply(doc, {**ALL_OK, "api": "down"}, T0 + timedelta(minutes=10))
        doc, _ = status.apply(doc, ALL_OK, T0 + timedelta(minutes=20))
        self.assertEqual(doc["history"]["2026-10-22"]["ai"], "ok")
        self.assertEqual(doc["incidents"], [])
        self.assertNotIn("장애 하루", status.strip(doc, "ai", T0 + timedelta(minutes=20)))

    def test_incident_backfills_from_first_stop_day(self):
        night = datetime(2026, 10, 22, 23, 55, tzinfo=status.KST)
        doc = fresh()
        doc["checked_at"] = status.iso(night - timedelta(minutes=10))
        doc, _ = status.apply(doc, {**ALL_OK, "api": "down"}, night)
        self.assertEqual(doc["history"]["2026-10-22"]["ai"], "ok")
        doc, _ = status.apply(doc, {**ALL_OK, "api": "down"}, night + timedelta(minutes=10))
        self.assertEqual(doc["history"]["2026-10-22"]["ai"], "down")
        self.assertEqual(doc["history"]["2026-10-23"]["ai"], "down")
        self.assertEqual(doc["incidents"][0]["started_at"], status.iso(night))

    # 공지 중에 멈춰 공지 뒤에도 멈춰 있으면, 공지 뒤 첫 실패부터 두 번을 센다.
    def test_incident_after_notice_starts_at_first_counted_failure(self):
        base = fresh()
        base["notice"] = notice("maintenance", T0 - timedelta(minutes=30), T0 + timedelta(minutes=25))
        both = {"app": "down", "api": "down", "site": "ok"}
        doc, _ = status.apply(base, both, T0)
        doc, _ = status.apply(doc, both, T0 + timedelta(minutes=10))
        self.assertEqual(doc["incidents"], [])
        doc, _ = status.apply(doc, both, T0 + timedelta(minutes=30))
        self.assertEqual(doc["incidents"], [])
        doc, _ = status.apply(doc, both, T0 + timedelta(minutes=40))
        self.assertEqual(len(doc["incidents"]), 1)
        self.assertEqual(doc["incidents"][0]["started_at"], status.iso(T0 + timedelta(minutes=30)))
        # 공지가 이미 끝났으면 처음 멈춘 때가 시작이다.
        late = fresh()
        late["notice"] = dict(base["notice"], ends_at=status.iso(T0 - timedelta(minutes=5)))
        doc, _ = status.apply(late, both, T0)
        doc, _ = status.apply(doc, both, T0 + timedelta(minutes=10))
        self.assertEqual(doc["incidents"][0]["started_at"], status.iso(T0))

    def test_slow_does_not_open_incident(self):
        doc, write = status.apply(fresh(), {**ALL_OK, "app": "slow"}, T0)
        self.assertTrue(write)
        self.assertEqual(doc["incidents"], [])
        # 셈하는 느림은 띠에서 정상이다.
        self.assertEqual(doc["history"]["2026-10-22"]["admin"], "ok")

    def test_maintenance_notice_suppresses_incident(self):
        base = fresh()
        base["notice"] = notice("maintenance", T0 - timedelta(minutes=5), T0 + timedelta(hours=1))
        both = {"app": "down", "api": "down", "site": "ok"}
        doc, _ = status.apply(base, both, T0)
        self.assertEqual(doc["incidents"], [])
        self.assertEqual(doc["history"]["2026-10-22"]["admin"], "maintenance")
        # 점검이 끝났는데도 두 번 잇달아 멈춰 있으면 끝난 뒤 첫 실패부터 장애로 적는다.
        doc, _ = status.apply(doc, both, T0 + timedelta(hours=1))
        self.assertEqual(doc["incidents"], [])
        doc, _ = status.apply(doc, both, T0 + timedelta(hours=1, minutes=10))
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

    # 응답이 중간에 끊기면(IncompleteRead 등) 멈춤으로 본다.
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
        self.assertNotIn(STALE_NOTE, page)

    def test_site_down_page_does_not_promise_site(self):
        doc, _ = status.apply(fresh(), {**ALL_OK, "site": "down"}, T0)
        page = status.render(doc)
        self.assertIn("손님 홈페이지가 지금 열리지 않습니다", page)
        self.assertNotIn("손님 홈페이지는 계속 열립니다", page)

    def test_text_is_escaped(self):
        doc = fresh()
        doc["incidents"] = [{
            "id": "x", "level": "down", "title": "<b>꺾쇠</b>", "components": ["admin"],
            "started_at": status.iso(T0), "resolved_at": None, "summary": "a&b",
        }]
        doc["notice"] = notice("maintenance", at(11, 0), None, title="<i>점검</i>")
        page = status.render(doc)
        self.assertIn("&lt;b&gt;꺾쇠&lt;/b&gt;", page)
        self.assertIn("a&amp;b", page)
        self.assertIn('<span class="hl">&lt;i&gt;점검&lt;/i&gt;</span>', page)

    def test_josa(self):
        self.assertEqual(status.josa("관리 화면", "이", "가"), "관리 화면이")
        self.assertEqual(status.josa("AI 고치기·게시", "이", "가"), "AI 고치기·게시가")

    def test_one_day_reads_haru(self):
        doc = fresh()
        doc["history"] = {"2026-10-22": {"admin": "down", "ai": "ok"}}
        self.assertIn("기록한 하루 중 장애 하루", status.strip(doc, "admin", T0))
        self.assertIn("기록한 하루는 정상", status.strip(doc, "ai", T0))


# 손으로 고친 status.json이 틀리면 고칠 자리를 알려 주고 멈춘다.
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

    # 제목이 빠졌거나 영향 받는 곳이 목록이 아니면 고칠 자리를 알려 준다.
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

    # 공지 꼴, 제목, 기능 목록이 틀리면 고칠 자리를 알린다.
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

    # 7차-7. 실패 수가 있는데 첫 실패 시각이 비면 막고, apply도 그 조합에서 죽지 않는다.
    def test_streak_needs_streak_since(self):
        for streak, since in ((1, None), (2, None)):
            with self.subTest(streak=streak, since=since):
                doc = fresh()
                comp_of(doc, "ai").update(streak=streak, streak_since=since)
                with self.assertRaisesRegex(status.StatusFileError,
                                            rf"components\[1\]\(ai\)\.streak가 {streak}인데 streak_since가 비어"):
                    status.validate(doc)
        doc = fresh()
        comp_of(doc, "ai").update(streak=0, streak_since=None)
        status.validate(doc)
        comp_of(doc, "ai").update(streak=1, streak_since=I(at(11, 50)))
        status.validate(doc)
        # 검사를 건너뛴 문서여도 apply는 1부터 다시 센다.
        doc = fresh()
        comp_of(doc, "ai").update(streak=1, streak_since=None)
        doc, _ = status.apply(doc, {**ALL_OK, **DOWN}, at(12, 0))
        ai = comp_of(doc, "ai")
        self.assertEqual((ai["streak"], ai["streak_since"], doc["incidents"]), (1, I(at(12, 0)), []))

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
