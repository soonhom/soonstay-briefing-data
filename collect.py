#!/usr/bin/env python3
"""순스테이 아침 브리핑용 데이터 수집기.

GitHub Actions가 매일 03:30(KST)에 실행해서 data/latest.json 을 갱신한다.

왜 이런 구조인가:
  브리핑을 보내는 클라우드 루틴 샌드박스는 외부 도메인을 화이트리스트로 막는다.
  api.frankfurter.dev, open-meteo, badatime 전부 CONNECT 단계에서 403이 난다.
  뚫리는 건 raw.githubusercontent.com 뿐이라, 여기서 미리 긁어 커밋해두고
  루틴은 그 JSON 한 개만 읽는다.

표준 라이브러리만 쓴다(pip install 불필요).
"""

import json
import os
import ssl
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

# 부산 시청 기준 (날씨)
BUSAN_LAT, BUSAN_LON = 35.1796, 129.0756
# 해운대 앞바다 (파고·수온)
HAEUNDAE_LAT, HAEUNDAE_LON = 35.1587, 129.1604

TIMEOUT = 30
ERRORS = []


def fetch_json(url, label):
    """URL 하나를 받아 JSON으로 돌려준다. 실패하면 None + ERRORS 기록."""
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers={"User-Agent": "soonstay-briefing/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 수집 실패는 브리핑에서 "없음"으로 표기
        ERRORS.append(f"{label}: {type(exc).__name__} {exc}")
        return None


# ---------------------------------------------------------------- 환율

# 원/달러·엔화는 항상 싣는다. 나머지는 "이슈가 있을 때만" 싣는데,
# 그 판정을 브리핑 에이전트에게 맡기면 매번 기준이 흔들리므로 여기서 숫자로 정한다.
# 고른 통화는 부산 인바운드와 실제로 연결되는 시장들이다(ECB 제공 범위 안에서).
WATCH = {
    "CNY": ("위안", 1),
    "TWD": None,          # ECB 미제공 — 대만은 못 본다
    "HKD": None,          # 달러 페그라 항상 달러를 따라간다. 볼 이유가 없다
    "SGD": ("싱가포르달러", 1),
    "THB": ("바트", 100),
    "PHP": ("페소", 100),
    "MYR": ("링깃", 1),
    "IDR": ("루피아", 100),
    "EUR": ("유로", 1),
    "GBP": ("파운드", 1),
    "AUD": ("호주달러", 1),
    "USD": None,          # 아래에서 따로 다룬다
    "JPY": None,
}
WATCH = {k: v for k, v in WATCH.items() if v}

# 이슈 판정 기준: "달러 대비" 얼마나 다르게 움직였나.
# 절대 변동률로 재면 달러에 연동된 통화들이 매번 딸려 나와 달러 얘기를 반복하게 된다.
# 원/달러가 -4.6%인 날 페소도 -4.4%인 건 뉴스가 아니다. 갈라진 것만 뉴스다.
NOTABLE_DIVERGENCE = 2.0   # %p
NOTABLE_MAX = 2


def _series(rows, code):
    """EUR 기준 원계열에서 '1단위당 원화' 시계열을 뽑는다.

    ECB 응답은 기준통화(EUR)를 rates에 넣어주지 않는다. EUR을 물으면
    자기 자신이 없어서 빈 계열이 나오므로, 이 경우 비율을 1로 본다.
    """
    out = []
    for d in sorted(rows):
        r = rows[d]
        if "KRW" not in r:
            continue
        rate = 1.0 if code == "EUR" else r.get(code)
        if not rate:
            continue
        out.append({"date": d, "value": r["KRW"] / rate})
    return out


def _stats(points, unit=1, digits=2):
    """최신값·전일대비·3개월 최고/최저·한달 변동률을 계산한다."""
    if len(points) < 2:
        return None
    vals = [p["value"] * unit for p in points]
    latest, prev = vals[-1], vals[-2]
    high, low = max(vals), min(vals)

    # 한 달 전(영업일 기준 대략 22개 전) 대비 변동률
    i = max(0, len(vals) - 23)
    month_ago = vals[i]
    pct_1m = (latest - month_ago) / month_ago * 100 if month_ago else 0.0

    return {
        "latest": round(latest, digits),
        "latest_date": points[-1]["date"],
        "change": round(latest - prev, digits),
        "change_pct": round((latest - prev) / prev * 100, 2) if prev else 0.0,
        "high_3m": round(high, digits),
        "high_3m_date": points[vals.index(high)]["date"],
        "low_3m": round(low, digits),
        "low_3m_date": points[vals.index(low)]["date"],
        "pct_from_high": round((latest - high) / high * 100, 2) if high else 0.0,
        "pct_1m": round(pct_1m, 2),
        "at_3m_high": abs(latest - high) < 1e-9,
        "at_3m_low": abs(latest - low) < 1e-9,
        "unit": unit,
    }


def collect_fx(today):
    """원/달러·엔화는 상시, 그 외 통화는 이슈가 있을 때만.

    ECB 기준 EUR 계열 한 번만 받아서 교차환율로 전부 계산한다(요청 1회).
    """
    start = (today - timedelta(days=92)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    symbols = ",".join(["KRW", "USD", "JPY"] + list(WATCH))
    url = f"https://api.frankfurter.dev/v1/{start}..{end}?base=EUR&symbols={symbols}"

    data = fetch_json(url, "fx")
    if not data or not data.get("rates"):
        return None

    rows = data["rates"]

    usd = _stats(_series(rows, "USD"))
    jpy = _stats(_series(rows, "JPY"), unit=100)
    if not usd:
        ERRORS.append("fx: 원/달러 계산 실패")
        return None

    # 이슈 통화 추리기 — 달러와 다르게 움직인 것만, 벌어진 폭 큰 순으로 최대 2개
    notable = []
    for code, (name, unit) in WATCH.items():
        st = _stats(_series(rows, code), unit=unit)
        if not st:
            continue
        gap = round(st["pct_1m"] - usd["pct_1m"], 2)
        if abs(gap) < NOTABLE_DIVERGENCE:
            continue
        direction = "더 오름" if gap > 0 else "더 내림"
        reason = f"한 달 {st['pct_1m']:+.1f}% (달러 {usd['pct_1m']:+.1f}%보다 {abs(gap):.1f}%p {direction})"
        if st["at_3m_high"]:
            reason += " · 3개월 최고"
        if st["at_3m_low"]:
            reason += " · 3개월 최저"
        st.update({"code": code, "name": name, "gap_vs_usd": gap, "reason": reason})
        notable.append(st)
    notable.sort(key=lambda x: abs(x["gap_vs_usd"]), reverse=True)

    return {
        "usd": usd,
        "jpy": jpy,          # 100엔당 원
        "notable": notable[:NOTABLE_MAX],
        "notable_rule": f"원/달러와 한 달 변동률이 {NOTABLE_DIVERGENCE}%p 이상 벌어진 통화만",
        "source": "European Central Bank (frankfurter.dev)",
    }


# ---------------------------------------------------------------- 날씨

# WMO weather code -> 한국어 하늘 상태
SKY = {
    0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림",
    45: "안개", 48: "짙은 안개",
    51: "약한 이슬비", 53: "이슬비", 55: "강한 이슬비",
    61: "약한 비", 63: "비", 65: "강한 비",
    66: "얼어붙는 비", 67: "강하게 얼어붙는 비",
    71: "약한 눈", 73: "눈", 75: "강한 눈", 77: "싸락눈",
    80: "소나기", 81: "강한 소나기", 82: "매우 강한 소나기",
    85: "소낙눈", 86: "강한 소낙눈",
    95: "천둥번개", 96: "천둥번개+우박", 99: "강한 천둥번개+우박",
}


def _longest_run(hours, predicate):
    """조건을 만족하는 가장 긴 연속 시간대를 '06시~10시' 형태로 돌려준다."""
    best = cur = None
    for h in hours:
        if predicate(h):
            cur = cur or [h, h]
            cur[1] = h
        else:
            if cur and (best is None or _span(cur) > _span(best)):
                best = cur
            cur = None
    if cur and (best is None or _span(cur) > _span(best)):
        best = cur
    if not best:
        return None
    if best[0]["hour"] == best[1]["hour"]:
        return f"{best[0]['hour']:02d}시"
    return f"{best[0]['hour']:02d}시~{best[1]['hour']:02d}시"


def _span(pair):
    return pair[1]["hour"] - pair[0]["hour"]


def collect_weather(today):
    """부산 오늘 기온/강수확률 + 어제 대비 비교."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={BUSAN_LAT}&longitude={BUSAN_LON}"
        "&hourly=temperature_2m,precipitation_probability,weather_code,wind_speed_10m"
        "&daily=temperature_2m_min,temperature_2m_max"
        "&timezone=Asia%2FSeoul&past_days=1&forecast_days=2&wind_speed_unit=ms"
    )
    data = fetch_json(url, "weather")
    if not data or "hourly" not in data:
        return None

    h = data["hourly"]
    today_str = today.strftime("%Y-%m-%d")

    hours = []
    for i, t in enumerate(h["time"]):
        if not t.startswith(today_str):
            continue
        hours.append({
            "hour": int(t[11:13]),
            "temp": h["temperature_2m"][i],
            "pop": h["precipitation_probability"][i],
            "wind": h["wind_speed_10m"][i],
            "sky": SKY.get(h["weather_code"][i], "정보없음"),
        })
    if not hours:
        ERRORS.append("weather: 오늘 시간대 데이터 없음")
        return None

    lo = min(hours, key=lambda x: x["temp"])
    hi = max(hours, key=lambda x: x["temp"])

    # 낮 시간대(07~20시)에서 강수확률 20% 이하가 이어지는 가장 긴 구간
    daytime = [x for x in hours if 7 <= x["hour"] <= 20]
    walk = _longest_run(daytime, lambda x: (x["pop"] or 0) <= 20)
    caution = _longest_run(hours, lambda x: (x["pop"] or 0) >= 60 or (x["wind"] or 0) >= 9)

    daily = data.get("daily", {})
    yday_min = yday_max = None
    if daily.get("time"):
        ystr = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        if ystr in daily["time"]:
            j = daily["time"].index(ystr)
            yday_min = daily["temperature_2m_min"][j]
            yday_max = daily["temperature_2m_max"][j]

    return {
        "today_min": {"temp": lo["temp"], "hour": f"{lo['hour']:02d}시"},
        "today_max": {"temp": hi["temp"], "hour": f"{hi['hour']:02d}시"},
        "yesterday_min": yday_min,
        "yesterday_max": yday_max,
        "max_diff_vs_yesterday": (
            round(hi["temp"] - yday_max, 1) if yday_max is not None else None
        ),
        "max_pop": max((x["pop"] or 0) for x in hours),
        "walk_window": walk,
        "caution_window": caution,
        "hourly": hours,
        "source": "Open-Meteo (기상청 KMA 모델)",
    }


# ---------------------------------------------------------------- 바다

def suit_guide(temp):
    """메모에 확정된 표기 규칙. 좋음/나쁨 판단은 쓰지 않는다 — 숫자와 기준만."""
    if temp is None:
        return None
    if temp >= 26:
        return "수트 없이 1시간 이상 수영 가능"
    if temp >= 22:
        return "수트 없이 1시간 수영 가능"
    if temp >= 18:
        return "수트 없이 30분 내외"
    return "수트 권장"


def collect_marine(today):
    """해운대 파고·수온. 06시(입수 시각) 값을 대표로 뽑는다."""
    url = (
        "https://marine-api.open-meteo.com/v1/marine"
        f"?latitude={HAEUNDAE_LAT}&longitude={HAEUNDAE_LON}"
        "&hourly=wave_height,sea_surface_temperature"
        "&timezone=Asia%2FSeoul&forecast_days=1"
    )
    data = fetch_json(url, "marine")
    if not data or "hourly" not in data:
        return None

    h = data["hourly"]
    today_str = today.strftime("%Y-%m-%d")
    rows = [
        {
            "hour": int(t[11:13]),
            "wave": h["wave_height"][i],
            "temp": h["sea_surface_temperature"][i],
        }
        for i, t in enumerate(h["time"])
        if t.startswith(today_str)
    ]
    if not rows:
        ERRORS.append("marine: 오늘 시간대 데이터 없음")
        return None

    at6 = next((r for r in rows if r["hour"] == 6), rows[0])
    waves = [r["wave"] for r in rows if r["wave"] is not None]

    return {
        "at_0600": {"wave_m": at6["wave"], "sea_temp_c": at6["temp"]},
        "wave_max_m": max(waves) if waves else None,
        "suit_guide": suit_guide(at6["temp"]),
        "visibility": "미제공 (공개 데이터 없음)",
        "hourly": rows,
        "source": "Open-Meteo Marine",
    }


# ---------------------------------------------------------------- main

def main():
    now = datetime.now(KST)
    today = now.date()
    weekday = "월화수목금토일"[now.weekday()]

    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "date": today.strftime("%Y-%m-%d"),
        "weekday": weekday,
        "is_swim_day": weekday in ("수", "일"),
        "fx": collect_fx(today),
        "weather": collect_weather(today),
        "marine": collect_marine(today),
        "errors": ERRORS,
    }

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "latest.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    ok = [k for k in ("fx", "weather", "marine") if payload[k]]
    print(f"wrote {out}")
    print(f"  수집 성공: {', '.join(ok) if ok else '없음'}")
    if ERRORS:
        print("  실패:")
        for e in ERRORS:
            print(f"    - {e}")

    # 세 개 다 실패하면 Actions를 빨갛게 만들어 알아채게 한다
    return 1 if not ok else 0


if __name__ == "__main__":
    sys.exit(main())
