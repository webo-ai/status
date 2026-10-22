"""웹오 상태 페이지 도구. 공개 주소를 재서 status.json을 고치고 index.html을 다시 그린다.

python3 scripts/status.py run      # 재고 → status.json 반영 → index.html 그리기
python3 scripts/status.py render   # status.json만 보고 index.html 다시 그리기(손으로 고친 뒤)
표준 라이브러리만 쓴다. 비밀값을 받지 않는다.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import http.client
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUS = ROOT / "status.json"
INDEX = ROOT / "index.html"
CSS = ROOT / "style.css"

KST = timezone(timedelta(hours=9))
UA = "webo-status-check/1 (+https://webo-ai.github.io/status/)"
TIMEOUT_S = 15.0
SLOW_MS = 4000
TRIES = 3
RETRY_GAP_S = 5.0
# 상태가 그대로면 이 간격마다만 확인 시각을 새로 적는다. 커밋이 10분마다 쌓이지 않게 하려는 값이다.
HEARTBEAT = timedelta(minutes=55)
# 앞 실패가 이보다 오래면 다음 실패를 잇지 않고 1부터 센다.
STREAK_GAP = timedelta(minutes=30)
# 마지막 확인이 이보다 오래된 기능이 있으면 첫 화면에 안내를 띄운다.
STALE = timedelta(minutes=70)
# 이번 판에 잰 기능의 저장된 확인 시각이 이만큼 이르면 새로 써서 따라오게 한다. 안 쓴 판의 확인은 디스크에
# 안 남아, 이 값이 작을수록 안내가 실제로 못 잰 시간에 가깝고 커밋은 잦아진다.
REFRESH = timedelta(minutes=50)
HISTORY_DAYS = 30
INCIDENT_DAYS = 90
KEEP_INCIDENTS = 100

RANK = {"ok": 0, "slow": 1, "maintenance": 2, "down": 3}
# 셈을 멈추는 공지 등급. 느림·정상 공지는 칸 표시만 바꾸고 평소처럼 센다.
STOPS = ("maintenance", "down")
LABEL = {"ok": "정상", "slow": "느림", "down": "장애", "maintenance": "점검"}
WEEKDAY = "월화수목금토일"


# ---------- 재기 ----------


def judge(kind: str, status: int, headers, body: bytes) -> bool:
    if status != 200:
        return False
    if kind == "html":
        return b"<html" in body[:4096].lower()
    if kind == "health":
        try:
            return json.loads(body.decode("utf-8")).get("status") == "ok"
        except (ValueError, AttributeError):
            return False
    if kind == "store":
        # 게시 전 매장도 준비 중 화면을 200으로 준다. 판 번호 머리가 있어야 게시본이다.
        return bool(headers.get("x-webo-version"))
    raise ValueError(f"모르는 kind: {kind}")


def probe_once(url: str, kind: str) -> tuple[str, str]:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "text/html,application/json"}
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as res:
            status, headers, body = res.status, res.headers, res.read(65536)
    except urllib.error.HTTPError as err:
        try:
            body = err.read(4096)
        except (http.client.HTTPException, OSError):
            body = b""
        status, headers = err.code, err.headers
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as err:
        return "down", type(err).__name__
    ms = (time.monotonic() - start) * 1000
    # Cloudflare 봇 검사에 걸리면 서비스 상태가 아니라 우리 확인이 막힌 것이다. 판정을 미룬다.
    if status in (403, 429, 503) and headers.get("cf-mitigated"):
        return "unknown", f"cf-mitigated {status}"
    if not judge(kind, status, headers, body):
        return "down", f"http {status}"
    return ("slow" if ms > SLOW_MS else "ok"), f"{ms:.0f}ms"


def probe(url: str, kind: str) -> tuple[str, str]:
    seen: list[tuple[str, str]] = []
    for attempt in range(TRIES):
        result = probe_once(url, kind)
        seen.append(result)
        if result[0] in ("ok", "slow"):
            return result
        if attempt + 1 < TRIES:
            time.sleep(RETRY_GAP_S)
    if any(r[0] == "unknown" for r in seen):
        return "unknown", "; ".join(n for _, n in seen)
    return "down", "; ".join(n for _, n in seen)


def measure(doc: dict) -> dict[str, str]:
    results: dict[str, str] = {}
    for name, check in doc["checks"].items():
        state, note = probe(check["url"], check["kind"])
        print(f"{name}: {state} ({note})", file=sys.stderr)
        results[name] = state
    return results


# ---------- 반영 ----------


def iso(dt: datetime) -> str:
    return dt.astimezone(KST).isoformat(timespec="minutes")


def parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def notice_phase(doc: dict, ref: datetime) -> str | None:
    """공지가 예정·진행·끝 중 어디인지. 공지가 없으면 None."""
    notice = doc.get("notice")
    if not notice:
        return None
    starts, ends = parse(notice.get("starts_at")), parse(notice.get("ends_at"))
    if ends and ref >= ends:
        return "ended"
    if starts and ref < starts:
        return "scheduled"
    return "active"


def notice_level(doc: dict, comp: dict, ref: datetime) -> str | None:
    """그 기능에 지금 걸린 공지의 등급. 걸린 공지가 없으면 None."""
    notice = doc.get("notice")
    if notice_phase(doc, ref) == "active" and comp["id"] in notice.get("components", []):
        return notice["level"]
    return None


def worse(a: str | None, b: str) -> str:
    return b if a is None or RANK[b] > RANK[a] else a


def shown(doc: dict, comp: dict, ref: datetime) -> str:
    """칸에 그릴 상태. 공지가 걸렸으면 공지 등급이고, 아니면 마지막으로 잰 값이다."""
    return notice_level(doc, comp, ref) or comp["state"]


def stale(doc: dict, comp: dict, ref: datetime | None = None) -> datetime | None:
    """마지막 확인이 문서 시각보다 70분 넘게 이른 기능이면 그 시각. 잰 적이 없거나 공지가 걸려 칸이
    공지 등급이면 None."""
    checked = parse(doc["checked_at"])
    if notice_level(doc, comp, ref or checked):
        return None
    last = parse(comp.get("checked_at"))
    return last if last and checked - last > STALE else None


def round_result(seen: list[str]) -> str:
    """기능 하나의 이번 판 결과. 주소 여럿 중 나쁜 쪽을 따르고, 못 잰 쪽이 있으면 멈춤만 확실하다."""
    known = [s for s in seen if s != "unknown"]
    if "down" in known:
        return "down"
    if len(known) < len(seen):
        return "unknown"
    return max(known, key=RANK.__getitem__)


def paint(history: dict, comp_id: str, start: datetime, today, level: str, cutoff: str) -> None:
    """start가 든 날부터 오늘까지 30일 띠 칸을 level 이상으로 칠한다."""
    day = start.astimezone(KST).date()
    while day <= today:
        key = day.isoformat()
        if key >= cutoff:
            record = history.setdefault(key, {})
            record[comp_id] = worse(record.get(comp_id), level)
        day += timedelta(days=1)


def significant(doc: dict) -> dict:
    # 시각만 바뀐 것은 새로 쓸 까닭이 아니다. 기능별 확인 시각은 apply의 refresh 기준으로 따로 따라 적는다.
    out = copy.deepcopy(doc)
    out.pop("checked_at", None)
    for comp in out.get("components", []):
        comp.pop("checked_at", None)
    return out


def stale_ids(doc: dict, ref: datetime | None = None) -> list[str]:
    return [c["id"] for c in doc["components"] if stale(doc, c, ref)]


def apply(doc: dict, results: dict[str, str], now: datetime) -> tuple[dict, bool]:
    """재 본 결과를 반영한 새 문서와, 파일을 새로 써야 하는지를 돌려준다."""
    new = copy.deepcopy(doc)
    stamp = iso(now)
    today = now.astimezone(KST).date()
    cutoff = (today - timedelta(days=HISTORY_DAYS + 5)).isoformat()
    history = new.setdefault("history", {})
    incidents = new.setdefault("incidents", [])
    last = parse(doc.get("checked_at"))
    # 기능이 든 공지가 걸리거나 풀린 판은 다른 값이 그대로여도 새로 써야 칸과 띠가 따라온다.
    notice_moved = False
    refresh = False
    for comp in new["components"]:
        comp.setdefault("streak", 0)
        comp.setdefault("streak_since", None)
        comp.setdefault("failing", False)
        checks = comp["check"] if isinstance(comp["check"], list) else [comp["check"]]
        result = round_result([results.get(c, "unknown") for c in checks])
        level = notice_level(new, comp, now)
        if last is None or notice_level(doc, comp, last) != level:
            notice_moved = True
        # 점검·장애 공지가 걸린 판은 재든 못 재든 세지 않고, 이어지던 실패 수도 0으로 되돌린다.
        # 잰 값도 칸 값·확인 시각으로 안 쓴다. 공지가 끝나면 칸은 공지 없이 마지막으로 잰 값이다.
        if level in STOPS:
            comp["streak"], comp["streak_since"] = 0, None
            paint(history, comp["id"], parse(new["notice"].get("starts_at")) or now, today, level, cutoff)
            continue
        # 못 잰 판은 이 기능에 없던 판이다. 상태·시각·셈·띠를 하나도 안 바꾼다.
        if result == "unknown":
            continue
        # 저장된 시각이 문서 시각보다 이르면 지난 쓴 판에 못 잰 기능이고, 오래됐으면 안내 기준에 가깝다.
        # 둘 다 이번 판에 새로 써야 다음에 못 잰 판이 와도 안내가 실제로 못 잰 시간을 따른다.
        seen = parse(comp.get("checked_at"))
        if seen is None or (last and seen < last) or now - seen >= REFRESH:
            refresh = True
        comp["checked_at"] = stamp
        if comp["state"] != result:
            comp["state"], comp["since"] = result, stamp
        # 잰 날은 정상으로 칠한다. 기록이 열리면 아래에서 장애로 덮는다.
        paint(history, comp["id"], now, today, "ok", cutoff)
        if result == "down":
            # 앞 실패가 30분보다 오래면 1부터 센다. 수가 2면 기록이 이미 열려 있어 그대로 둔다.
            # 첫 실패 시각이 빠졌으면 이을 근거가 없어 1부터 센다(validate가 막지만 손으로 고친 파일 대비).
            first = parse(comp["streak_since"])
            if comp["streak"] == 1 and (not first or now - first > STREAK_GAP):
                comp["streak"] = 0
            if not comp["streak"]:
                comp["streak_since"] = stamp
            # 두 번이면 기록을 연다. 오래 멈춰도 수가 판마다 바뀌어 파일을 새로 쓰지 않게 2에서 멈춘다.
            comp["streak"] = min(comp["streak"] + 1, 2)
            comp["failing"] = True
        else:
            comp["streak"], comp["streak_since"], comp["failing"] = 0, None, False

    comps = {c["id"]: c for c in new["components"]}
    open_auto = next((i for i in incidents if i.get("auto") and not i.get("resolved_at")), None)
    members = set(open_auto["components"]) if open_auto else set()
    joined = [c for c in new["components"] if c["streak"] >= 2 and c["id"] not in members]
    if joined:
        started = min(parse(c["streak_since"]) for c in joined)
        inc_id = started.astimezone(KST).strftime("%Y%m%d-%H%M")
        if not open_auto:
            # 사람이 닫은 같은 첫 실패의 자동 기록이 있으면 새로 열지 않고 다시 연다. 사람이 쓴 기록은 두고 새로 연다.
            open_auto = next((i for i in incidents if i.get("auto") and i.get("id") == inc_id), None)
            if open_auto:
                open_auto["resolved_at"] = None
        if open_auto:
            merged = set(open_auto.get("components", [])) | {c["id"] for c in joined}
            open_auto["components"] = [c["id"] for c in new["components"] if c["id"] in merged]
        else:
            open_auto = {
                "id": inc_id,
                "auto": True,
                "level": "down",
                "title": "·".join(c["name"] for c in joined) + " 장애",
                "components": [c["id"] for c in joined],
                "started_at": iso(started),
                "resolved_at": None,
                "summary": "자동 확인에서 응답이 없어 기록했습니다. 원인을 확인하는 대로 고쳐 적습니다.",
            }
            incidents.insert(0, open_auto)
        # 기록에 든 날은 첫 실패를 잰 날까지 거슬러 장애로 칠한다(자정 너머 멈춘 경우).
        for comp in joined:
            paint(history, comp["id"], parse(comp["streak_since"]), today, "down", cutoff)
    if open_auto:
        # 든 기능이 다 센 정상·느림으로 돌아온 판에 닫는다. 점검·장애 공지 판과 못 잰 판은 닫지 않는다.
        if not any(comps.get(i, {}).get("failing") for i in open_auto["components"]):
            open_auto["resolved_at"] = stamp
        else:
            for comp_id in open_auto["components"]:
                paint(history, comp_id, now, today, "down", cutoff)
    del incidents[KEEP_INCIDENTS:]
    for key in [k for k in history if k < cutoff]:
        del history[key]

    new["checked_at"] = stamp
    changed = significant(new) != significant(doc)
    beat = last is None or now - last >= HEARTBEAT
    # 오래 못 잰 안내가 뜨거나 지는 판도 새로 써야 첫 화면이 따라온다.
    return new, changed or beat or notice_moved or refresh or stale_ids(new, now) != stale_ids(doc, last or now)


# ---------- 그리기 ----------


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def josa(word: str, with_final: str, without_final: str) -> str:
    """마지막 글자에 받침이 있으면 앞 조사, 없으면 뒤 조사를 붙인다."""
    last = word.rstrip()[-1:]
    code = ord(last) - 0xAC00 if last else -1
    has_final = 0 <= code <= 11171 and code % 28 != 0
    return word + (with_final if has_final else without_final)


def hm(dt: datetime) -> str:
    k = dt.astimezone(KST)
    return f"{'오전' if k.hour < 12 else '오후'} {k.hour % 12 or 12}:{k.minute:02d}"


def when(dt: datetime) -> str:
    k = dt.astimezone(KST)
    return f"{k.month}월 {k.day}일 ({WEEKDAY[k.weekday()]}) {hm(k)}"


def span(start: datetime, end: datetime) -> str:
    minutes = max(1, round((end - start).total_seconds() / 60))
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}시간 {mins}분"
    return f"{hours}시간" if hours else f"{mins}분"


ICONS = {
    "ok": '<path d="M7.5 12.5l3 3 6-6.5"/><circle cx="12" cy="12" r="9"/>',
    "slow": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "down": '<path d="M12 3.5l9.5 16.5h-19z"/><path d="M12 10v4.5"/><path d="M12 17.5v.01"/>',
    "maintenance": '<path d="M14.5 5.5a4 4 0 0 0 4.9 4.9l-8.6 8.6a2.1 2.1 0 0 1-3-3l8.6-8.6"/><path d="M14.5 5.5l2-2"/>',
    "store": '<path d="M4 10v9h16v-9"/><path d="M3 10l2-5h14l2 5"/><path d="M3 10h18"/><path d="M10 19v-5h4v5"/>',
    "mail": '<rect x="3" y="5.5" width="18" height="13" rx="2"/><path d="M3.5 7l8.5 6 8.5-6"/>',
    "arrow": '<path d="M5 12h14"/><path d="M13 6l6 6-6 6"/>',
}


def icon(name: str, cls: str = "ic") -> str:
    return (
        f'<svg class="{cls}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
        f"{ICONS[name]}</svg>"
    )


LOGO = (
    '<span class="logo" aria-hidden="true">Web<svg viewBox="0 0 84 76" focusable="false">'
    '<defs><linearGradient id="g-ring" x1="0" y1="1" x2="1" y2="0.1">'
    '<stop offset="0" stop-color="#12B5F5"/><stop offset=".5" stop-color="#1E6BFF"/>'
    '<stop offset="1" stop-color="#B06CF5"/></linearGradient>'
    '<linearGradient id="g-star" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#3D7BFF"/><stop offset="1" stop-color="#B06CF5"/>'
    "</linearGradient></defs>"
    '<circle cx="35" cy="44" r="25" fill="none" stroke="url(#g-ring)" stroke-width="12.5"/>'
    '<path d="M72 0 Q74.5 8.5 83 11 Q74.5 13.5 72 22 Q69.5 13.5 61 11 Q69.5 8.5 72 0Z" '
    'fill="url(#g-star)"/></svg></span>'
)


def name_list(comps: list[dict], with_final: str = "", without_final: str = "") -> str:
    """기능 이름을 한 덩어리씩 감싸 두 줄로 갈리지 않게 잇고, 마지막 이름의 받침에 맞춰 조사를 붙인다."""
    last = comps[-1]["name"]
    return ", ".join(f'<span class="nm">{esc(c["name"])}</span>' for c in comps) + josa(
        last, with_final, without_final)[len(last):]


def headline(doc: dict, states: dict[str, str], ref: datetime, unsure: bool) -> tuple[str, str, str, bool]:
    """(등급, 제목 HTML, 안내 글, 공지 제목·본문을 썼는지). unsure면 오래 못 잰 기능이 있어 단정하지 않는다.
    가장 나쁜 상태를 만든 기능에 공지가 걸렸으면 사람이 건 안내라 '지금 안내' 칸과 맞게 단정한다."""
    worst = max(states.values(), key=RANK.__getitem__)
    hit_comps = [c for c in doc["components"] if states[c["id"]] == worst]
    ga = name_list(hit_comps, "이", "가")
    # 다 정상인 판은 모든 기능이 제목을 만들어서 정상 공지 하나로 단정하지 않는다.
    if worst != "ok" and any(notice_level(doc, c, ref) for c in hit_comps):
        unsure = False
    if worst == "ok":
        if unsure:
            return (worst, "마지막 확인 때 모든 서비스가 정상이었습니다",
                    "마지막 확인 때는 관리 화면부터 손님 사이트까지 평소처럼 돌아갔습니다.", False)
        return worst, "모든 서비스가 정상입니다", "관리 화면부터 손님 사이트까지 평소처럼 돌아갑니다.", False
    if worst == "slow":
        if unsure:
            return (worst, ga + " 마지막 확인 때 평소보다 느렸습니다",
                    "마지막 확인 때는 열리기는 했지만 평소보다 오래 걸렸습니다. 잠시 기다렸다가 다시 시도해 주세요.",
                    False)
        return (
            worst,
            ga + " 평소보다 느립니다",
            "열리기는 하지만 평소보다 오래 걸릴 수 있습니다. 잠시 기다렸다가 다시 시도해 주세요.",
            False,
        )
    if worst == "maintenance":
        notice = doc.get("notice") or {}
        by_notice = notice_phase(doc, ref) == "active" and notice.get("level") == "maintenance"
        title = esc(notice["title"]) if by_notice else name_list(hit_comps) + " 점검 중입니다"
        lead = (notice.get("body") if by_notice else None) or "점검이 끝나면 바로 다시 쓰실 수 있습니다."
        return worst, title, lead, by_notice
    if unsure:
        return worst, ga + " 마지막 확인 때 멈춰 있었습니다", "원인을 찾고 있습니다. 바뀌는 대로 이 페이지에 알려 드립니다.", False
    return worst, ga + " 멈춰 있습니다", "원인을 찾아 고치고 있습니다. 고치는 대로 이 페이지에 알려 드립니다.", False


APART = "이미 게시한 매장 홈페이지는 관리 화면과 따로 운영됩니다."
CALM = ("calm", "손님 홈페이지는 계속 열립니다", [
    APART, "관리 화면이나 AI 고치기가 멈춰도 손님은 홈페이지를 그대로 볼 수 있습니다."])
NEUTRAL = ("neutral", "손님 홈페이지가 잠시 불편할 수 있습니다", [
    "평소보다 느리거나 잠시 열리지 않을 수 있습니다.", APART])
DOWN = ("down", "손님 홈페이지가 지금 열리지 않습니다", [
    "가장 먼저 고치고 있습니다.", "급한 안내는 전화나 메신저로 손님께 직접 알려 주세요."])
# 오래 못 잰 기능이 있는 판. 지금 상태처럼 단정하지 않고 마지막 확인 때 일로 적는다.
CALM_UNSURE = ("calm", "손님 홈페이지는 마지막 확인 때 열렸습니다", CALM[2])
DOWN_UNSURE = ("down", "손님 홈페이지가 마지막 확인 때 열리지 않았습니다", [
    "원인을 찾고 있습니다.", DOWN[2][1]])


def site_card(states: dict[str, str], unsure: bool = False) -> str:
    # 손님 사이트 칸 표시가 정상이고 오래 못 잰 기능이 없을 때만 "계속 열립니다"라고 단정한다.
    cards = {"ok": CALM_UNSURE, "down": DOWN_UNSURE} if unsure else {"ok": CALM, "down": DOWN}
    tone, title, lines = cards.get(states["sites"], NEUTRAL)
    if tone != "down" and RANK[states["publish"]] >= RANK["maintenance"]:
        lines = lines + [
            "다만 지금은 홈페이지의 상담 신청이 접수되지 않을 수 있습니다. 전화 문의는 그대로 받으실 수 있습니다.",
            "멈춘 동안 고친 내용은 다시 게시해야 손님 화면에 보입니다.",
        ]
    body = "".join(f"<p>{esc(line)}</p>" for line in lines)
    return (
        f'<section class="card site-card is-{tone}" aria-labelledby="h-site">'
        f'{icon("store", "ic ic-lg")}<div><h2 id="h-site">{esc(title)}</h2>{body}</div></section>'
    )


def days(n: int) -> str:
    return "하루" if n == 1 else f"{n}일"


def strip(doc: dict, comp_id: str, ref: datetime) -> str:
    history = doc.get("history", {})
    today = ref.astimezone(KST).date()
    cells, counts, recorded = [], {k: 0 for k in RANK}, 0
    for back in range(HISTORY_DAYS - 1, -1, -1):
        day = today - timedelta(days=back)
        state = history.get(day.isoformat(), {}).get(comp_id)
        # 예전 판이나 손으로 적은 느림 칸은 정상으로 읽는다. 띠에는 느림 색이 없다.
        state = "ok" if state == "slow" else state
        tip = f"{day.month}월 {day.day}일 "
        if state:
            recorded += 1
            counts[state] += 1
            cells.append(f'<span class="d is-{state}" title="{esc(tip + LABEL[state])}"></span>')
        else:
            cells.append(f'<span class="d is-none" title="{esc(tip + "기록 없음")}"></span>')
    bad = [f"{LABEL[k]} {days(counts[k])}" for k in ("down", "maintenance", "slow") if counts[k]]
    if bad:
        summary = f"기록한 {days(recorded)} 중 " + ", ".join(bad)
    elif recorded == 1:
        summary = "기록한 하루는 정상"
    else:
        summary = f"기록한 {recorded}일 모두 정상"
    return (
        f'<div class="days" role="img" aria-label="최근 {HISTORY_DAYS}일 기록: {esc(summary)}">'
        f'{"".join(cells)}</div>'
        f'<p class="days-cap" aria-hidden="true"><span>{HISTORY_DAYS}일 전</span>'
        f"<span>{esc(summary)}</span><span>오늘</span></p>"
    )


def components_html(doc: dict, states: dict[str, str], ref: datetime) -> str:
    rows = []
    day = parse(doc["checked_at"]).astimezone(KST).date()
    for comp in doc["components"]:
        state = states[comp["id"]]
        last = stale(doc, comp, ref)
        told = last and (hm(last) if last.astimezone(KST).date() == day else when(last))
        note = f'<p class="last">마지막 확인 {esc(told)}</p>' if told else ""
        rows.append(
            f'<li class="comp"><div class="comp-top"><div><h3>{esc(comp["name"])}</h3>'
            f'<p class="sub">{esc(comp["desc"])}</p>{note}</div>'
            f'<p class="state is-{state}">{icon(state, "ic ic-sm")}{LABEL[state]}</p></div>'
            f"{strip(doc, comp['id'], ref)}</li>"
        )
    return "".join(rows)


def incidents_html(doc: dict, ref: datetime) -> str:
    names = {c["id"]: c["name"] for c in doc["components"]}
    cutoff = ref - timedelta(days=INCIDENT_DAYS)
    items = []
    for inc in doc.get("incidents", []):
        start = parse(inc["started_at"])
        end = parse(inc.get("resolved_at"))
        if (end or ref) < cutoff:
            continue
        level = inc.get("level", "down")
        affected = "·".join(names.get(i, i) for i in inc.get("components", []))
        if end:
            period = f"{when(start)}부터 {span(start, end)} 동안"
            badge = f'<p class="state is-ok">{icon("ok", "ic ic-sm")}해결</p>'
        else:
            period = f"{when(start)}부터 이어지는 중"
            badge = f'<p class="state is-{level}">{icon(level, "ic ic-sm")}진행 중</p>'
        items.append(
            f'<li class="inc"><div class="inc-top"><h3>{esc(inc["title"])}</h3>{badge}</div>'
            f'<p class="meta"><time datetime="{esc(inc["started_at"])}">{esc(period)}</time>'
            f" · 영향 받은 곳: {esc(affected)}</p>"
            f'<p>{esc(inc.get("summary") or "")}</p></li>'
        )
    if not items:
        return f'<p class="empty">최근 {INCIDENT_DAYS}일 동안 기록된 장애가 없습니다.</p>'
    return f'<ol class="incs">{"".join(items)}</ol>'


def notice_html(doc: dict, ref: datetime, repeated: bool = False) -> str:
    """repeated면 첫 화면이 이미 공지 제목·본문을 보여서 기간과 영향 기능만 적는다."""
    phase = notice_phase(doc, ref)
    if phase not in ("active", "scheduled"):
        return ""
    notice = doc["notice"]
    names = {c["id"]: c["name"] for c in doc["components"]}
    affected = "·".join(names.get(i, i) for i in notice.get("components", []))
    starts, ends = parse(notice.get("starts_at")), parse(notice.get("ends_at"))
    period = ""
    if starts and ends:
        period = f"{when(starts)}부터 {when(ends)}까지"
    elif ends:
        period = f"{when(ends)}까지"
    elif starts:
        period = f"{when(starts)}부터"
    tag = "예정된 안내" if phase == "scheduled" else "지금 안내"
    if repeated and phase == "active":
        lines = [x for x in (period, f"영향 받는 곳: {affected}" if affected else "") if x]
        if not lines:
            return ""
        return (
            f'<section class="card notice" aria-labelledby="h-notice"><p class="eyebrow">{tag}</p>'
            f'<h2 id="h-notice">{LABEL[notice["level"]]} 시간과 영향 받는 기능</h2>'
            + "".join(f"<p>{esc(line)}</p>" for line in lines) + "</section>"
        )
    meta = " · ".join(x for x in (period, f"영향 받는 곳: {affected}" if affected else "") if x)
    meta_html = f'<p class="meta">{esc(meta)}</p>' if meta else ""
    return (
        f'<section class="card notice" aria-labelledby="h-notice"><p class="eyebrow">{tag}</p>'
        f'<h2 id="h-notice">{esc(notice["title"])}</h2>{meta_html}'
        f'<p>{esc(notice.get("body") or "")}</p></section>'
    )


def mail_html(mail: str) -> str:
    """좁은 칸에서 @ 뒤가 먼저 줄을 바꾸게 한다."""
    local, at, domain = mail.partition("@")
    return esc(local) + (esc(at) + "<wbr>" if at else "") + esc(domain)


def render(doc: dict, ref: datetime | None = None) -> str:
    """ref는 공지가 걸렸는지와 오늘이 언제인지 보는 기준 시각이다. 없으면 문서 시각을 쓴다."""
    ref = ref or parse(doc["checked_at"])
    states = {c["id"]: shown(doc, c, ref) for c in doc["components"]}
    late_comps = [c for c in doc["components"] if stale(doc, c, ref)]
    # 손님 사이트에 공지가 걸렸으면 손님 칸은 사람이 건 안내를 따라 단정한다.
    site_unsure = bool(late_comps) and not any(
        notice_level(doc, c, ref) for c in doc["components"] if c["id"] == "sites")
    worst, title, lead, repeated = headline(doc, states, ref, bool(late_comps))
    css_ver = hashlib.sha256(CSS.read_bytes()).hexdigest()[:10]
    mail = doc["contact"]
    mailto = "mailto:" + mail + "?subject=" + urllib.parse.quote("[웹오 문의] ")
    checked = when(parse(doc["checked_at"]))
    late = (
        f'<p class="stale-note">자동 확인이 1시간 넘게 되지 않은 기능이 있습니다: {name_list(late_comps)}. '
        "그 기능은 마지막으로 확인한 때 상태입니다.</p>"
        if late_comps else ""
    )
    hero_list = "".join(
        f'<li class="is-{states[c["id"]]}"><span>{esc(c["name"])}</span> '
        f'<b class="state is-{states[c["id"]]}">{icon(states[c["id"]], "ic ic-sm")}'
        f"{LABEL[states[c['id']]]}</b></li>"
        for c in doc["components"]
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="description" content="웹오(WebO) 관리 화면·AI 고치기·게시·손님 사이트가 지금 잘 돌아가는지 알려 드립니다.">
<meta name="robots" content="noindex">
<title>{esc(LABEL[worst])} · 웹오 서비스 상태</title>
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="style.css?v={css_ver}">
</head>
<body>
<a class="skip" href="#main">본문으로 건너뛰기</a>
<header class="hd"><div class="wrap hd-in">
  <a class="brand" href="./">{LOGO}<span class="sr">웹오</span><span class="brand-sub">서비스 상태</span></a>
  <a class="hd-mail" href="{esc(mailto)}">{icon("mail", "ic ic-sm")}문의하기</a>
</div></header>
<main id="main" class="wrap" tabindex="-1">
  {late}<section class="hero is-{worst}" aria-labelledby="h-status">
    <span class="hero-mark">{icon(worst, "ic ic-xl")}</span>
    <div class="hero-txt">
      <h1 id="h-status"><span class="eyebrow">웹오 서비스 상태 · {LABEL[worst]}</span><span class="hl">{title}</span></h1>
      <p class="lead">{esc(lead)}</p>
      <ul class="hero-list" aria-label="기능별 한눈 상태">{hero_list}</ul>
      <p class="checked">이 페이지를 마지막으로 바꾼 때 <time datetime="{esc(doc["checked_at"])}">{esc(checked)}</time> (한국 시간)</p>
      <p class="checked-note">상태가 그대로면 이 시각은 한 시간에 한 번쯤 바뀝니다.</p>
    </div>
  </section>
  <div class="grid">
    <div class="col-side">
      {site_card(states, site_unsure)}
      {notice_html(doc, ref, repeated)}
      <section class="card help" aria-labelledby="h-help">
        <h2 id="h-help">안 될 때는 이렇게 해 주세요</h2>
        <ul class="ticks">
          <li>몇 분 뒤 화면을 새로 고쳐 주세요. 잠깐 끊긴 경우가 많습니다.</li>
          <li>관리 화면이 여기서 정상으로 나오는데도 열리지 않으면 와이파이나 브라우저를 바꿔 보세요.</li>
          <li>고친 내용이 홈페이지에 안 보이면 게시 상태를 확인해 주세요.</li>
        </ul>
      </section>
      <section class="card contact" aria-labelledby="h-contact">
        <h2 id="h-contact">계속 안 되면 알려 주세요</h2>
        <p>매장 이름과 어떤 화면이 안 되는지 적어 주시면 빨리 확인합니다.</p>
        <a class="mail-link" href="{esc(mailto)}">{icon("mail", "ic")}<span>{mail_html(mail)}</span>{icon("arrow", "ic ic-sm")}</a>
      </section>
    </div>
    <div class="col-main">
      <section class="card c-comps" aria-labelledby="h-comps">
        <div class="sec-head"><h2 id="h-comps">기능별 상태</h2>
        <p class="sub">칸 하나가 하루입니다. 자동 확인에서 장애로 기록된 날과 장애 안내가 있던 날을 장애로 칠합니다.</p></div>
        <ul class="comps">{components_html(doc, states, ref)}</ul>
        <ul class="legend" aria-label="색 설명">
          <li><span class="d is-ok"></span>정상</li>
          <li><span class="d is-down"></span>장애</li><li><span class="d is-maintenance"></span>점검</li>
          <li><span class="d is-none"></span>기록 없음</li>
        </ul>
      </section>
      <section class="card c-incs" aria-labelledby="h-incs">
        <div class="sec-head"><h2 id="h-incs">지난 장애 기록</h2>
        <p class="sub">최근 {INCIDENT_DAYS}일 동안 있었던 일을 적어 둡니다.</p></div>
        {incidents_html(doc, ref)}
      </section>
    </div>
  </div>
</main>
<footer class="ft"><div class="wrap">
  <p>이 페이지는 웹오 서버와 떨어진 곳(GitHub)에서 열려, 웹오가 멈춰도 볼 수 있습니다.</p>
  <p>상태는 10분쯤마다 자동으로 확인합니다. 바뀌면 몇 분 안에 이 페이지에 반영되고, 그대로면 한 시간에 한 번쯤 시각만 새로 적습니다.</p>
  <p>© 2026 WebO(웹오)</p>
</div></footer>
</body>
</html>
"""


# ---------- 명령 ----------


class StatusFileError(ValueError):
    """status.json을 사람이 잘못 고친 경우. 메시지는 고칠 자리를 알려 준다."""


def check_time(where: str, value: object, required: bool = True) -> None:
    if value is None and not required:
        return
    example = "2026-10-25T02:00+09:00"
    if not isinstance(value, str):
        raise StatusFileError(f"{where}에 시각을 {example}처럼 글자로 적어 주세요.")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise StatusFileError(f"{where}의 시각 '{value}'를 읽지 못했습니다. {example}처럼 적어 주세요.") from None
    if dt.tzinfo is None:
        raise StatusFileError(f"{where}의 시각 '{value}'에 시간대가 없습니다. 끝에 +09:00을 붙여 주세요.")


def check_level(where: str, value: object) -> None:
    if value not in RANK:
        raise StatusFileError(
            f"{where}의 값 '{value}'를 알 수 없습니다. {', '.join(RANK)} 중 하나로 적어 주세요."
        )


def check_text(where: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise StatusFileError(f"{where}이 비어 있습니다. 화면에 보일 제목을 글자로 적어 주세요.")


def check_body(where: str, value: object) -> None:
    if value is not None and not isinstance(value, str):
        raise StatusFileError(f'{where}는 "..."처럼 따옴표로 감싼 글자로 적거나, 쓸 말이 없으면 지워 주세요.')


def check_ids(where: str, value: object, known: list[str]) -> None:
    if not isinstance(value, list):
        raise StatusFileError(f'{where}는 ["admin", "ai"]처럼 대괄호 목록으로 적어 주세요.')
    for item in value:
        if item not in known:
            raise StatusFileError(
                f"{where}의 '{item}'를 알 수 없습니다. {', '.join(known)} 중에서 글자로 적어 주세요."
            )


def validate(doc: dict) -> None:
    """손으로 고친 status.json이 그리기 전에 깨지지 않게 제목·등급 값·시각 꼴·기능 목록을 본다."""
    check_time("checked_at", doc.get("checked_at"))
    known = [comp.get("id") for comp in doc.get("components", [])]
    for n, comp in enumerate(doc.get("components", [])):
        at = f"components[{n}]({comp.get('id', '?')})"
        check_level(f"{at}.state", comp.get("state"))
        check_time(f"{at}.since", comp.get("since"))
        check_time(f"{at}.checked_at", comp.get("checked_at"), required=False)
        check_time(f"{at}.streak_since", comp.get("streak_since"), required=False)
        if not isinstance(comp.get("streak", 0), int) or comp.get("streak", 0) < 0:
            raise StatusFileError(f"{at}.streak는 0, 1, 2 같은 수로 적어 주세요.")
        if comp.get("streak", 0) >= 1 and not comp.get("streak_since"):
            raise StatusFileError(
                f"{at}.streak가 {comp['streak']}인데 streak_since가 비어 있습니다. "
                "첫 실패 시각을 적거나 streak를 0으로 적어 주세요."
            )
    notice = doc.get("notice")
    if notice is not None:
        if not isinstance(notice, dict):
            raise StatusFileError('notice는 { }로 감싼 묶음으로 적고, 공지가 없으면 null로 적어 주세요.')
        check_text("notice.title", notice.get("title"))
        check_body("notice.body", notice.get("body"))
        check_ids("notice.components", notice.get("components", []), known)
        check_level("notice.level", notice.get("level"))
        check_time("notice.starts_at", notice.get("starts_at"), required=False)
        check_time("notice.ends_at", notice.get("ends_at"), required=False)
    incidents = doc.get("incidents", [])
    if not isinstance(incidents, list):
        raise StatusFileError("incidents는 [ ]로 감싼 목록으로 적고, 기록이 없으면 []로 적어 주세요.")
    for n, inc in enumerate(incidents):
        if not isinstance(inc, dict):
            raise StatusFileError(f"incidents[{n}]는 {{ }}로 감싼 묶음으로 적어 주세요.")
        at = f"incidents[{n}]({inc.get('id', '?')})"
        check_text(f"{at}.title", inc.get("title"))
        check_body(f"{at}.summary", inc.get("summary"))
        check_ids(f"{at}.components", inc.get("components", []), known)
        check_level(f"{at}.level", inc.get("level", "down"))
        check_time(f"{at}.started_at", inc.get("started_at"))
        check_time(f"{at}.resolved_at", inc.get("resolved_at"), required=False)
    history = doc.get("history", {})
    if not isinstance(history, dict):
        raise StatusFileError("history는 { }로 감싼 묶음으로 적고, 기록이 없으면 {}로 적어 주세요.")
    for day, record in history.items():
        if not isinstance(record, dict):
            raise StatusFileError(f'history.{day}는 {{"admin": "ok"}}처럼 묶음으로 적어 주세요.')
        for comp_id, state in record.items():
            check_level(f"history.{day}.{comp_id}", state)


def load() -> dict:
    try:
        doc = json.loads(STATUS.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise StatusFileError(f"{err.lineno}번째 줄 {err.colno}번째 글자에서 JSON 꼴이 깨졌습니다.") from None
    validate(doc)
    return doc


def save(doc: dict) -> None:
    STATUS.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def write_index(doc: dict, ref: datetime | None = None) -> None:
    INDEX.write_text(render(doc, ref), encoding="utf-8", newline="\n")


def clock() -> datetime:
    return datetime.now(KST)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "render"))
    args = parser.parse_args(argv)
    try:
        doc = load()
    except StatusFileError as err:
        print(f"status.json을 고쳐야 합니다. {err}", file=sys.stderr)
        return 2
    now = clock()
    if args.command == "run":
        new, write = apply(doc, measure(doc), now)
        if write:
            save(new)
            doc = new
        else:
            print("상태가 그대로라 파일을 새로 쓰지 않습니다.", file=sys.stderr)
    # 손으로 공지를 넣고 그리기만 돌려도 공지가 걸렸는지는 지금 시각으로 본다.
    write_index(doc, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
