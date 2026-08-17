#!/usr/bin/env python3
"""순스테이 아침 브리핑용 데이터 수집기.

GitHub Actions가 매일 04:20(KST)에 실행해서 data/latest.json 을 갱신한다.

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

def collect_fx(today):
    """원/달러 3개월 시계열. 그래프용으로 주 1회 간격 13점을 뽑아둔다."""
    start = (today - timedelta(days=92)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    url = f"https://api.frankfurter.dev/v1/{start}..{end}?from=USD&to=KRW"

    data = fetch_json(url, "fx")
    if not data or not data.get("rates"):
        return None

    points = sorted(
        ({"date": d, "value": round(v["KRW"], 2)} for d, v in data["rates"].items() if "KRW" in v),
        key=lambda p: p["date"],
    )
    if len(points) < 2:
        ERRORS.append("fx: 데이터 포인트 부족")
        return None

    values = [p["value"] for p in points]
    high = max(values)
    low = min(values)

    # 그래프용 13점: 균등 간격으로 뽑되 마지막(최신)은 반드시 포함
    n = min(13, len(points))
    step = (len(points) - 1) / (n - 1) if n > 1 else 1
    series = [points[round(i * step)] for i in range(n)]
    series[-1] = points[-1]

    latest, prev = points[-1], points[-2]
    return {
        "latest": latest["value"],
        "latest_date": latest["date"],
        "prev": prev["value"],
        "change": round(latest["value"] - prev["value"], 2),
        "high_3m": high,
        "high_3m_date": next(p["date"] for p in points if p["value"] == high),
        "low_3m": low,
        "low_3m_date": next(p["date"] for p in points if p["value"] == low),
        "pct_from_high": round((latest["value"] - high) / high * 100, 2),
        "series": series,
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
