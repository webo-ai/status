# 웹오 서비스 상태 페이지

웹오(WebO)에 장애가 났을 때 사장님께 알리는 정적 페이지입니다. 웹오 서버·Cloudflare와 다른 곳(GitHub Pages)에서 열리게 따로 둡니다.

- 주소: https://webo-ai.github.io/status/
- 장애 때 차례와 장애 문자 문안은 문서 저장소 webo-docs의 장애 런북(17)에 있습니다.

이 저장소는 공개입니다. 내부 주소, 서버 이름, 손님 정보, 비밀값은 적지 않습니다.

## 파일

| 파일 | 하는 일 |
|---|---|
| `status.json` | 상태의 정본입니다. 재는 주소, 기능 이름, 지금 상태, 날마다 기록, 공지, 장애 기록이 다 여기 있습니다 |
| `index.html` | `status.json`으로 그린 결과입니다. 손으로 고치지 않습니다 |
| `style.css`, `favicon.svg` | 화면 모양과 아이콘입니다. 바깥 글꼴·스크립트를 부르지 않습니다 |
| `scripts/status.py` | 재기(`run`)와 그리기(`render`)를 합니다. 파이썬 표준 라이브러리만 씁니다 |
| `.github/workflows/status.yml` | 10분마다 `run`을 돌리고, 바뀌면 커밋한 뒤 Pages 빌드를 부릅니다 |

## 자동 확인

GitHub Actions가 10분마다(정각 뒤 4분부터) 공개 주소 셋을 GET으로 잽니다. 비밀값은 쓰지 않습니다.

| 기능 | 재는 주소 | 정상 판정 |
|---|---|---|
| 관리 화면 | `https://app.webomni.dpdns.org/`와 API `/healthz` | 화면 파일이 200이고 HTML이며, API도 정상. API가 없으면 로그인부터 안 돼서 둘 중 나쁜 쪽을 따릅니다 |
| AI 고치기·게시 | `https://api.webomni.dpdns.org/healthz` | 200이고 `{"status":"ok"}` |
| 손님 사이트 | `https://demo-skin.webomni.dpdns.org/` (시연 매장) | 200이고 `x-webo-version` 머리가 있음 |

- 한 번 재는 데 15초를 기다리고, 실패하면 5초 뒤 다시 재서 세 번까지 봅니다. 셋 다 실패하면 장애입니다.
- 열렸지만 4초를 넘기면 느림입니다.
- 매장 주소는 게시 전에도 준비 중 화면을 200으로 줍니다. 그래서 판 번호 머리로 게시본인지 봅니다.
- Cloudflare 봇 검사에 걸린 응답(`cf-mitigated`)은 우리 확인이 막힌 것이라 상태를 바꾸지 않습니다. 그 기능은 확인 시각(`components[].checked_at`)도 올리지 않고, 화면엔 "확인 중"으로 보입니다.
- API `/healthz`는 API가 떴는지만 봅니다. DB·작업자·AI 회사 상태는 밖에서 못 재서, 그쪽 장애는 아래 공지로 사람이 적습니다.
- 상태 칸은 첫 실패부터 장애로 바뀝니다. `incidents` 자동 기록은 두 번 잇달아(20분) 멈춰야 엽니다. 수동 API 배포처럼 잠깐 멈춘 것이 공개 기록으로 남지 않게 하려는 값입니다.
- 자동 기록은 기록에 든 기능이 실제로 다 돌아와야 닫습니다. 점검 공지가 걸려도 실제로 멈춰 있으면 닫지 않습니다. 느림은 기록을 열지 않습니다.
- 상태가 그대로면 55분마다만 확인 시각을 새로 적습니다. 커밋이 10분마다 쌓이지 않게 하려는 값입니다.
- `GITHUB_TOKEN`으로 민 커밋은 Pages 빌드를 깨우지 않습니다(GitHub 문서). 그래서 민 뒤에 `POST /repos/{저장소}/pages/builds`를 부릅니다. 이 일에 `pages: write` 권한을 줍니다.
- 반영까지는 예약 지연(몇 분), Pages 빌드(1분쯤), github.io 캐시(최대 10분)가 겹칩니다.

## 사람이 고치는 법

`status.json`을 고쳐 main에 밉니다. 워크플로가 `index.html`을 다시 그려 커밋합니다. 내 PC에서 미리 보려면 `python scripts/status.py render` 뒤 `index.html`을 엽니다.

점검이나 밖에서 못 재는 장애는 `notice`에 적습니다. 걸린 기능은 자동 판정보다 공지 등급이 앞서고, 점검 중엔 장애 기록을 열지 않습니다.

```json
"notice": {
  "level": "maintenance",
  "title": "관리 화면 정기 점검",
  "body": "점검 중에도 손님 홈페이지는 그대로 열립니다.",
  "components": ["admin", "ai", "publish"],
  "starts_at": "2026-10-25T02:00+09:00",
  "ends_at": "2026-10-25T03:00+09:00"
}
```

- `level`은 `ok`·`slow`·`down`·`maintenance` 중 하나입니다. `components`는 `admin`·`ai`·`publish`·`sites`입니다.
- 시각은 `2026-10-25T02:00+09:00`처럼 시간대까지 적습니다. 등급 값이나 시각 꼴이 틀리면 `status.py`가 고칠 자리를 알려 주고 멈춥니다.
- 시작 전엔 "예정된 안내"로만 보이고, 끝 시각이 지나면 저절로 사라집니다. 끝나면 `null`로 되돌립니다.
- 자동 기록의 `title`·`summary`는 원인을 안 뒤 사장님이 알아듣는 말로 고쳐 씁니다. 손님 정보와 내부 주소는 적지 않습니다.

## 시험

```sh
python3 -m unittest discover -s scripts -p "test_*.py"
```

바깥 주소를 부르지 않습니다. 워크플로도 재기 전에 같은 시험을 돌립니다.

## 알아 둘 것

- 공개 저장소에서 60일 동안 활동이 없으면 GitHub가 예약 워크플로를 끕니다. 자동 커밋이 활동으로 셈되는지는 아직 재 보지 못했습니다. Actions 화면에서 꺼졌으면 다시 켭니다.
- 이 저장소 Actions는 GitHub 공식 액션만 받고 SHA 고정을 요구합니다. `actions/checkout` 판을 올릴 땐 SHA로 적습니다.
- 워크플로 파일을 밀려면 토큰에 `workflow` 권한이 있어야 합니다.
