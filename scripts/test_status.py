"""python3 -m unittest discover -s scripts -p "test_*.py" 로 돈다. 바깥 주소를 부르지 않는다."""

import copy
import json
import unittest
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
        doc, write = status.apply(fresh(), {**ALL_OK, "api": "down"}, T0)
        self.assertTrue(write)
        self.assertEqual(state_of(doc, "admin"), "down")
        self.assertEqual(state_of(doc, "ai"), "down")
        self.assertEqual(state_of(doc, "publish"), "down")
        self.assertEqual(state_of(doc, "sites"), "ok")
        self.assertEqual(len(doc["incidents"]), 1)
        self.assertEqual(doc["incidents"][0]["components"], ["admin", "ai", "publish"])

    def test_later_component_joins_open_incident(self):
        doc, _ = status.apply(fresh(), {**ALL_OK, "app": "down"}, T0)
        self.assertEqual(state_of(doc, "ai"), "ok")
        doc, _ = status.apply(doc, {"app": "down", "api": "down", "site": "ok"}, T0 + timedelta(minutes=10))
        self.assertEqual(len(doc["incidents"]), 1)
        self.assertEqual(doc["incidents"][0]["components"], ["admin", "ai", "publish"])
        doc, _ = status.apply(doc, ALL_OK, T0 + timedelta(minutes=40))
        self.assertEqual(doc["incidents"][0]["resolved_at"], status.iso(T0 + timedelta(minutes=40)))
        self.assertEqual(doc["history"]["2026-10-22"]["admin"], "down")

    def test_unknown_keeps_previous_state(self):
        doc, _ = status.apply(fresh(), {**ALL_OK, "app": "down"}, T0)
        doc, _ = status.apply(doc, {**ALL_OK, "app": "unknown"}, T0 + timedelta(minutes=10))
        self.assertEqual(state_of(doc, "admin"), "down")

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
        page = status.render(doc)
        self.assertIn("관리 화면, AI 고치기, 게시가 지금 멈춰 있습니다", page)
        self.assertIn("상담 신청이 접수되지 않을 수 있습니다", page)
        self.assertIn("진행 중", page)

    def test_site_down_page_does_not_promise_site(self):
        doc, _ = status.apply(fresh(), {**ALL_OK, "site": "down"}, T0)
        page = status.render(doc)
        self.assertIn("손님 홈페이지도 지금 열리지 않습니다", page)
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


if __name__ == "__main__":
    unittest.main()
