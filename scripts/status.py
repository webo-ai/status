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
HISTORY_DAYS = 30
INCIDENT_DAYS = 90
KEEP_INCIDENTS = 100

RANK = {"ok": 0, "slow": 1, "maintenance": 2, "down": 3}
# 화면에만 쓰는 등급. "확인 중"은 저장하지 않고, 마지막 재기가 막힌 기능에 그릴 때만 붙는다.
SHOW_RANK = {"ok": 0, "checking": 1, "slow": 2, "maintenance": 3, "down": 4}
LABEL = {"ok": "정상", "slow": "느림", "down": "장애", "maintenance": "점검", "checking": "확인 중"}
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
    """칸에 그릴 상태. 공지가 걸렸으면 공지 등급이고, 아니면 마지막으로 잰 값이다.
    이번 판을 못 쟀는데 마지막 값이 정상·느림이거나 잰 적이 없으면 "확인 중"이다."""
    level = notice_level(doc, comp, ref)
    if level:
        return level
    if comp.get("pending") and (not comp.get("checked_at") or comp["state"] in ("ok", "slow")):
        return "checking"
    return comp["state"]


def round_result(seen: list[str]) -> tuple[str, str | None]:
    """기능 하나의 이번 판 결과(ok·slow·down·unknown)와 못 잰 정도(None·"some"·"all")."""
    known = [s for s in seen if s != "unknown"]
    missed = None if len(known) == len(seen) else ("some" if known else "all")
    # 주소 여럿 중 나쁜 쪽을 따른다. 못 잰 쪽이 있으면 멈춤만 확실하고, 정상·느림은 못 잰 판으로 둔다.
    if "down" in known:
        return "down", missed
    if missed:
        return "unknown", missed
    return max(known, key=RANK.__getitem__), None


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
    # 시각만 바뀐 것은 새로 쓸 까닭이 아니다. 기능별 확인 시각도 55분 간격으로만 따라 적힌다.
    out = copy.deepcopy(doc)
    out.pop("checked_at", None)
    for comp in out.get("components", []):
        comp.pop("checked_at", None)
    return out


def apply(doc: dict, results: dict[str, str], now: datetime) -> tuple[dict, bool]:
    """재 본 결과를 반영한 새 문서와, 파일을 새로 써야 하는지를 돌려준다."""
    new = copy.deepcopy(doc)
    stamp = iso(now)
    today = now.astimezone(KST).date()
    cutoff = (today - timedelta(days=HISTORY_DAYS + 5)).isoformat()
    history = new.setdefault("history", {})
    incidents = new.setdefault("incidents", [])
    was_failing: dict[str, bool] = {}
    for comp in new["components"]:
        checks = comp["check"] if isinstance(comp["check"], list) else [comp["check"]]
        result, missed = round_result([results.get(c, "unknown") for c in checks])
        if missed:
            comp["pending"] = missed
        else:
            comp.pop("pending", None)
        # 기능 시각과 상태는 실제로 잰 판에만 바꾼다. 못 잰 판은 마지막으로 잰 값을 그대로 둔다.
        if result != "unknown":
            comp["checked_at"] = stamp
            if comp["state"] != result:
                comp["state"] = result
                comp["since"] = stamp
        was_failing[comp["id"]] = comp.get("failing", False)
        level = notice_level(new, comp, now)
        if level:
            # 공지가 걸린 판은 세지 않고, 이어지던 실패 수도 0으로 되돌린다.
            comp["streak"], comp["streak_since"] = 0, None
            if level in ("down", "maintenance"):
                start = parse(new["notice"].get("starts_at")) or now
                paint(history, comp["id"], start, today, level, cutoff)
        elif result == "down":
            if not comp.get("streak"):
                comp["streak_since"] = stamp
            # 두 번이면 기록을 연다. 오래 멈춰도 수가 판마다 바뀌어 파일을 새로 쓰지 않게 2에서 멈춘다.
            comp["streak"] = min(comp.get("streak", 0) + 1, 2)
            comp["failing"] = True
        elif result in ("ok", "slow"):
            comp["streak"], comp["streak_since"], comp["failing"] = 0, None, False
            paint(history, comp["id"], now, today, "ok", cutoff)
        comp.setdefault("streak", 0)
        comp.setdefault("streak_since", None)
        comp.setdefault("failing", False)

    comps = {c["id"]: c for c in new["components"]}
    open_auto = next((i for i in incidents if i.get("auto") and not i.get("resolved_at")), None)
    members = set(open_auto["components"]) if open_auto else set()
    joined = [c for c in new["components"] if c["streak"] >= 2 and c["id"] not in members]
    if joined:
        if open_auto:
            merged = members | {c["id"] for c in joined}
            open_auto["components"] = [c["id"] for c in new["components"] if c["id"] in merged]
        else:
            started = min(parse(c["streak_since"]) for c in joined)
            open_auto = {
                "id": started.astimezone(KST).strftime("%Y%m%d-%H%M"),
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
        for comp_id in open_auto["components"]:
            if comp_id in comps and (was_failing[comp_id] or comps[comp_id]["failing"]):
                paint(history, comp_id, now, today, "down", cutoff)
        # 든 기능이 다 공지 없이 잰 정상·느림으로 돌아온 판에 닫는다. 공지 판과 못 잰 판은 닫지 않는다.
        if not any(comps.get(i, {}).get("failing") for i in open_auto["components"]):
            open_auto["resolved_at"] = stamp
    del incidents[KEEP_INCIDENTS:]
    for key in [k for k in history if k < cutoff]:
        del history[key]

    new["checked_at"] = stamp
    changed = significant(new) != significant(doc)
    last = parse(doc.get("checked_at"))
    stale = last is None or now - last >= HEARTBEAT
    return new, changed or stale


# ---------- 그리기 ----------


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def josa(word: str, with_final: str, without_final: str) -> str:
    """마지막 글자에 받침이 있으면 앞 조사, 없으면 뒤 조사를 붙인다."""
    last = word.rstrip()[-1:]
    code = ord(last) - 0xAC00 if last else -1
    has_final = 0 <= code <= 11171 and code % 28 != 0
    return word + (with_final if has_final else without_final)


def when(dt: datetime) -> str:
    k = dt.astimezone(KST)
    ampm = "오전" if k.hour < 12 else "오후"
    hour = k.hour % 12 or 12
    return f"{k.month}월 {k.day}일 ({WEEKDAY[k.weekday()]}) {ampm} {hour}:{k.minute:02d}"


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
    "checking": '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>',
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


def headline(doc: dict, states: dict[str, str]) -> tuple[str, str, str]:
    """(등급, 제목 HTML, 안내 글). 제목의 기능 이름은 한 덩어리로 감싸 두 줄로 갈리지 않게 한다."""
    names = {c["id"]: c["name"] for c in doc["components"]}
    worst = max(states.values(), key=SHOW_RANK.__getitem__)
    hit_names = [names[i] for i, s in states.items() if s == worst]
    hit = ", ".join(f'<span class="nm">{esc(n)}</span>' for n in hit_names)
    # 조사는 마지막 이름의 받침을 따른다.
    ga = josa(hit_names[-1], "이", "가")[len(hit_names[-1]):]
    if worst == "ok":
        return worst, "모든 서비스가 정상입니다", "관리 화면부터 손님 사이트까지 평소처럼 돌아갑니다."
    if worst == "checking":
        return (
            worst,
            hit + " 상태를 다시 확인하고 있습니다",
            "자동 확인이 잠시 되지 않았습니다. 멈췄다는 뜻은 아니며, 확인되는 대로 이 페이지에 알려 드립니다.",
        )
    if worst == "slow":
        return (
            worst,
            hit + ga + " 평소보다 느립니다",
            "열리기는 하지만 평소보다 오래 걸릴 수 있습니다. 잠시 기다렸다가 다시 시도해 주세요.",
        )
    if worst == "maintenance":
        body = (doc.get("notice") or {}).get("body") or "점검이 끝나면 바로 다시 쓰실 수 있습니다."
        return worst, hit + " 점검 중입니다", body
    return (
        worst,
        hit + ga + " 지금 멈춰 있습니다",
        "원인을 찾아 고치고 있습니다. 고치는 대로 이 페이지에 알려 드립니다.",
    )


APART = "이미 게시한 매장 홈페이지는 관리 화면과 따로 운영됩니다."
# 손님 사이트 상태별 (색, 제목, 글). 정상일 때만 "계속 열립니다"라고 단정한다.
SITE_CARD = {
    "ok": ("calm", "손님 홈페이지는 계속 열립니다", [
        APART, "관리 화면이나 AI 고치기가 멈춰도 손님은 홈페이지를 그대로 볼 수 있습니다."]),
    "checking": ("neutral", "손님 홈페이지가 열리는지 다시 확인하고 있습니다", [
        "자동 확인이 잠시 되지 않았습니다. 확인되는 대로 이 페이지에 알려 드립니다.", APART]),
    "slow": ("neutral", "손님 홈페이지가 평소보다 느리게 열릴 수 있습니다", [
        "열리기는 하지만 평소보다 오래 걸릴 수 있습니다.", APART]),
    "maintenance": ("neutral", "손님 홈페이지가 점검으로 잠시 열리지 않을 수 있습니다", [
        "점검이 끝나면 다시 열립니다.", APART]),
    "down": ("down", "손님 홈페이지가 지금 열리지 않습니다", [
        "가장 먼저 고치고 있습니다.", "급한 안내는 전화나 메신저로 손님께 직접 알려 주세요."]),
}


def site_card(states: dict[str, str]) -> str:
    tone, title, lines = SITE_CARD[states["sites"]]
    if tone != "down" and SHOW_RANK[states["publish"]] >= SHOW_RANK["maintenance"]:
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


WAS = {"ok": "정상이었습니다", "slow": "열리지만 느렸습니다", "down": "멈춰 있었습니다", "maintenance": "점검 중이었습니다"}


def pending_note(doc: dict, comp: dict, ref: datetime) -> str:
    """이번 판을 다 못 잰 기능 칸에 붙일 글. 공지가 걸린 칸은 공지 등급만 보인다."""
    if not comp.get("pending") or notice_level(doc, comp, ref):
        return ""
    told = "일부만 확인했습니다." if comp["pending"] == "some" else "이번 확인이 되지 않았습니다."
    last = parse(comp.get("checked_at"))
    if not last:
        return told + " 아직 확인한 기록이 없습니다."
    # 기능 시각은 파일을 새로 쓸 때만 적혀 실제보다 이를 수 있다. 그래서 "무렵까지"로 넓게 적는다.
    return told + f" 마지막으로 확인한 때는 {when(last)}이고, 그 무렵까지 {WAS[comp['state']]}."


def components_html(doc: dict, states: dict[str, str], ref: datetime) -> str:
    rows = []
    for comp in doc["components"]:
        state = states[comp["id"]]
        told = pending_note(doc, comp, ref)
        note = f'<p class="pending">{esc(told)}</p>' if told else ""
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
            f'<p>{esc(inc.get("summary", ""))}</p></li>'
        )
    if not items:
        return f'<p class="empty">최근 {INCIDENT_DAYS}일 동안 기록된 장애가 없습니다.</p>'
    return f'<ol class="incs">{"".join(items)}</ol>'


def notice_html(doc: dict, ref: datetime) -> str:
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
    meta = " · ".join(x for x in (period, f"영향 받는 곳: {affected}" if affected else "") if x)
    meta_html = f'<p class="meta">{esc(meta)}</p>' if meta else ""
    return (
        f'<section class="card notice" aria-labelledby="h-notice"><p class="eyebrow">{tag}</p>'
        f'<h2 id="h-notice">{esc(notice["title"])}</h2>{meta_html}'
        f'<p>{esc(notice.get("body", ""))}</p></section>'
    )


def render(doc: dict) -> str:
    ref = parse(doc["checked_at"])
    states = {c["id"]: shown(doc, c, ref) for c in doc["components"]}
    worst, title, lead = headline(doc, states)
    css_ver = hashlib.sha256(CSS.read_bytes()).hexdigest()[:10]
    mail = doc["contact"]
    mailto = "mailto:" + mail + "?subject=" + urllib.parse.quote("[웹오 문의] ")
    checked = when(ref)
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
  <section class="hero is-{worst}" aria-labelledby="h-status">
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
      {site_card(states)}
      {notice_html(doc, ref)}
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
        <a class="mail-link" href="{esc(mailto)}">{icon("mail", "ic")}<span>{esc(mail)}</span>{icon("arrow", "ic ic-sm")}</a>
      </section>
    </div>
    <div class="col-main">
      <section class="card c-comps" aria-labelledby="h-comps">
        <div class="sec-head"><h2 id="h-comps">기능별 상태</h2>
        <p class="sub">칸 하나가 하루입니다. 20분 넘게 멈춘 날만 장애로 칠합니다.</p></div>
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
    notice = doc.get("notice")
    if notice is not None:
        if not isinstance(notice, dict):
            raise StatusFileError('notice는 { }로 감싼 묶음으로 적고, 공지가 없으면 null로 적어 주세요.')
        check_text("notice.title", notice.get("title"))
        check_ids("notice.components", notice.get("components", []), known)
        check_level("notice.level", notice.get("level"))
        check_time("notice.starts_at", notice.get("starts_at"), required=False)
        check_time("notice.ends_at", notice.get("ends_at"), required=False)
    for n, inc in enumerate(doc.get("incidents", [])):
        if not isinstance(inc, dict):
            raise StatusFileError(f"incidents[{n}]는 {{ }}로 감싼 묶음으로 적어 주세요.")
        at = f"incidents[{n}]({inc.get('id', '?')})"
        check_text(f"{at}.title", inc.get("title"))
        check_ids(f"{at}.components", inc.get("components", []), known)
        check_level(f"{at}.level", inc.get("level", "down"))
        check_time(f"{at}.started_at", inc.get("started_at"))
        check_time(f"{at}.resolved_at", inc.get("resolved_at"), required=False)
    for day, record in doc.get("history", {}).items():
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


def write_index(doc: dict) -> None:
    INDEX.write_text(render(doc), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "render"))
    args = parser.parse_args(argv)
    try:
        doc = load()
    except StatusFileError as err:
        print(f"status.json을 고쳐야 합니다. {err}", file=sys.stderr)
        return 2
    if args.command == "run":
        new, write = apply(doc, measure(doc), datetime.now(KST))
        if write:
            save(new)
            doc = new
        else:
            print("상태가 그대로라 파일을 새로 쓰지 않습니다.", file=sys.stderr)
    write_index(doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
