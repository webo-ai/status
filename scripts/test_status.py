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

    # 3. 점검 공지가 걸려도 실제로 멈춰 있으면 자동 기록을 닫지 않는다.
    def test_notice_does_not_resolve_open_incident_while_down(self):
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
        self.assertEqual(doc["incidents"][0]["resolved_at"], status.iso(T0 + timedelta(minutes=30)))

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

    # 2차 4. 배포 직후 첫 판에 못 재면 앞 판의 문서 시각을 기능 시각으로 둔다.
    def test_first_unknown_uses_document_time(self):
        doc = fresh()
        for comp in doc["components"]:
            comp.pop("checked_at", None)
        doc, _ = status.apply(doc, {**ALL_OK, "app": "unknown"}, T0 + timedelta(minutes=10))
        admin = next(c for c in doc["components"] if c["id"] == "admin")
        self.assertTrue(admin["pending"])
        self.assertEqual(admin["checked_at"], status.iso(T0))

    # 2차 7. 점검 공지 중에 멈춰 공지 뒤에도 멈춰 있으면 기록은 공지가 끝난 때부터다.
    def test_incident_after_notice_starts_at_notice_end(self):
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
        self.assertEqual(len(doc["incidents"]), 1)
        self.assertEqual(doc["incidents"][0]["started_at"], status.iso(T0 + timedelta(minutes=25)))
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
        self.assertEqual(doc["history"]["2026-10-22"]["admin"], "slow")

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
        # 점검이 끝났는데도 멈춰 있으면 그때부터 장애로 적는다.
        doc, _ = status.apply(doc, {"app": "down", "api": "down", "site": "ok"}, T0 + timedelta(hours=1))
        self.assertEqual(len(doc["incidents"]), 1)

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
        self.assertIn("손님 홈페이지도 지금 열리지 않습니다", page)
        self.assertNotIn("손님 홈페이지는 계속 열립니다", page)

    # 2차 3. 확인 중 안내는 확인 중 칸에만 붙고, 멈춘 칸에서 일부를 못 쟀으면 따로 적는다.
    def test_checking_note_only_on_checking_cell(self):
        doc, _ = status.apply(fresh(), ALL_OK, T0)
        doc, _ = status.apply(doc, {"app": "down", "api": "unknown", "site": "ok"}, T0 + timedelta(minutes=10))
        page = status.render(doc)
        cells = {c["id"]: part for c, part in zip(doc["components"], page.split('<li class="comp">')[1:])}
        self.assertIn("장애", cells["admin"])
        self.assertIn("일부 항목은 아직 확인하지 못했습니다.", cells["admin"])
        self.assertNotIn("다시 확인하고 있습니다", cells["admin"])
        self.assertIn("다시 확인하고 있습니다", cells["ai"])
        self.assertNotIn("pending", cells["sites"])

    # 2차 4. 기능 시각이 없으면 문서 시각을 쓰고 "아직 한 번도"라고 하지 않는다.
    def test_checking_without_component_time_uses_document_time(self):
        doc = fresh()
        for comp in doc["components"]:
            comp.pop("checked_at", None)
        doc["components"][0]["pending"] = True
        page = text(status.render(doc))
        self.assertIn("마지막으로 확인한 때는 10월 22일 (목) 오후 12:00이고, 그 무렵까지 정상이었습니다.", page)
        self.assertNotIn("아직 한 번도", page)
        self.assertIn("페이지를 고친 때", page)
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
