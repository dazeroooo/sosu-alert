# -*- coding: utf-8 -*-
"""
소수의원 네이버 예약 빈자리 알림 (텔레그램)

- 몇 분에 한 번씩 예약 가능한 시간을 확인합니다.
- 새로 빈자리가 생기면 텔레그램으로 알려줍니다.
- [공지] 예약 공지 내용(다음 오픈 일정)이 바뀌면 알려줍니다.
- 공지에 적힌 예약 오픈 시간 1시간 전, 10분 전에 미리 알려줍니다.
- 예약은 알림을 받고 직접 하세요. 이 프로그램은 예약하지 않습니다.

PC에서 사용
  1) 아래 설정에 TELEGRAM_TOKEN 입력
  2) python sosu_alert.py --setup   (내 채팅 ID 확인 + 테스트 메시지)
  3) 아래 설정에 TELEGRAM_CHAT_ID 입력
  4) python sosu_alert.py           (알림 시작, 끄려면 Ctrl+C)

GitHub Actions에서 사용
  토큰과 채팅 ID는 이 파일에 넣지 말고 GitHub Secrets에 넣으세요.
  (python sosu_alert.py --github 로 실행됩니다)
"""

# ======================= 설정 =======================
TELEGRAM_TOKEN = ""      # BotFather에게 받은 토큰 (예: "123456789:ABC...")
TELEGRAM_CHAT_ID = ""    # --setup 으로 확인한 숫자 (예: "987654321")

# 확인할 원장님 (원하지 않는 줄은 맨 앞에 # 를 붙이면 제외됩니다)
ITEMS = {
    "7616009": "김나래 원장님",
    "7616038": "박소현 원장님",
    "8227854": "원장님 미정",
}

CHECK_EVERY_MINUTES = 3   # 확인 간격(분). 너무 줄이면 차단될 수 있어요. 3분 이상 권장
DAYS_AHEAD = 75           # 오늘부터 며칠 뒤까지 확인할지
WATCH_NOTICE = True       # 예약 공지(다음 오픈 일정) 변경 + 오픈 전 미리 알림
GITHUB_RUN_MINUTES = 25   # GitHub에서 한 번 실행될 때 몇 분 동안 확인할지
# ====================================================

import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") or TELEGRAM_TOKEN
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or TELEGRAM_CHAT_ID

BUSINESS_ID = "1132902"
NOTICE_ITEM_ID = "7654206"
BIZ_URL = f"https://booking.naver.com/booking/13/bizes/{BUSINESS_ID}"
KST = timezone(timedelta(hours=9))
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
WD = "월화수목금토일"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SCHEDULE_QUERY = """query hourlySchedule($scheduleParams: ScheduleParams) {
  schedule(input: $scheduleParams) {
    bizItemSchedule {
      hourly {
        unitStartTime
        unitBookingCount
        unitStock
        isUnitSaleDay
        isUnitBusinessDay
      }
    }
  }
}"""


def now_kst():
    return datetime.now(KST)


def log(msg):
    print(f"[{now_kst():%m-%d %H:%M:%S}] {msg}", flush=True)


def http(url, data=None, headers=None, timeout=20):
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def item_url(item_id):
    return f"{BIZ_URL}/items/{item_id}"


# ---------------- 네이버 예약 확인 ----------------
def get_open_slots(item_id):
    """예약 가능한 시간 목록: {"2026-12-03 14:30", ...}"""
    now = now_kst()
    end = now + timedelta(days=DAYS_AHEAD)
    body = {
        "operationName": "hourlySchedule",
        "variables": {"scheduleParams": {
            "businessTypeId": 13,
            "businessId": BUSINESS_ID,
            "bizItemId": item_id,
            "startDateTime": now.strftime("%Y-%m-%dT00:00:00"),
            "endDateTime": end.strftime("%Y-%m-%dT23:59:59"),
            "fixedTime": True,
            "includesHolidaySchedules": True,
        }},
        "query": SCHEDULE_QUERY,
    }
    d = http("https://booking.naver.com/graphql?opName=hourlySchedule",
             data=json.dumps(body).encode(),
             headers={"Content-Type": "application/json",
                      "Referer": item_url(item_id)})
    slots = set()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    for s in d["data"]["schedule"]["bizItemSchedule"]["hourly"] or []:
        if not (s.get("isUnitBusinessDay") and s.get("isUnitSaleDay")):
            continue
        if (s.get("unitBookingCount") or 0) >= (s.get("unitStock") or 0):
            continue
        t = s["unitStartTime"][:16]
        if t > now_str:  # 이미 지난 시간 제외
            slots.add(t)
    return slots


def get_notice():
    items = http(f"https://api.booking.naver.com/v3.1/businesses/{BUSINESS_ID}"
                 f"/biz-items?bizItemType=STANDARD&lang=ko")
    for i in items:
        if str(i.get("bizItemId")) == NOTICE_ITEM_ID:
            return (i.get("desc") or "").strip()
    return ""


def notice_summary(text):
    """공지에서 '다음 예약 오픈' 부분만 뽑기"""
    out, on = [], False
    for l in (x.strip() for x in text.splitlines()):
        if "다음 예약 오픈" in l:
            on = True
            continue
        if on:
            if l.startswith("※") or (not l and out):
                break
            if l:
                out.append(l)
    return "\n".join(out) or text[:300]


def parse_open_time(text):
    """'예약 오픈 일시: 11월 18일 (수) 오후 3시' -> datetime (KST)"""
    m = re.search(r"오픈\s*일시\s*[:：]?\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일[^\n]*?"
                  r"(오전|오후)?\s*(\d{1,2})\s*(?:시|:)\s*(?:(\d{1,2})\s*분?)?", text)
    if not m:
        return None
    mon, day, ampm, hour, minute = m.groups()
    mon, day, hour, minute = int(mon), int(day), int(hour), int(minute or 0)
    if ampm == "오후" and hour < 12:
        hour += 12
    if ampm == "오전" and hour == 12:
        hour = 0
    now = now_kst()
    try:
        dt = datetime(now.year, mon, day, hour, minute, tzinfo=KST)
    except ValueError:
        return None
    if dt < now - timedelta(days=180):   # 12월에 '1월 오픈' 공지 같은 경우
        dt = dt.replace(year=now.year + 1)
    return dt


def fmt_dt(dt):
    return f"{dt:%m/%d}({WD[dt.weekday()]}) {dt:%H:%M}"


def fmt_slots(slots):
    by_day = {}
    for t in sorted(slots):
        day, hm = t.split(" ")
        by_day.setdefault(day, []).append(hm)
    lines = []
    for day, times in by_day.items():
        dt = datetime.strptime(day, "%Y-%m-%d")
        lines.append(f"  {dt:%m/%d}({WD[dt.weekday()]}) " + ", ".join(times))
    return "\n".join(lines)


# ---------------- 텔레그램 ----------------
def tg(method, **params):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode() if params else None
    return http(url, data=data)


def send(text):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        log("(텔레그램 미설정 - 화면에만 표시)\n" + text)
        return
    for attempt in range(3):
        try:
            tg("sendMessage", chat_id=TELEGRAM_CHAT_ID, text=text,
               disable_web_page_preview="true")
            return
        except Exception as e:
            log(f"텔레그램 전송 실패({attempt + 1}/3): {e}")
            time.sleep(5)


# ---------------- 확인 1회 ----------------
def check_once(state):
    """state를 갱신하고, 보낼 알림을 바로 보냅니다."""
    first = not state.get("initialized")
    slots_state = state.setdefault("slots", {})

    for item_id, name in ITEMS.items():
        now_slots = get_open_slots(item_id)
        new = now_slots - set(slots_state.get(item_id, []))
        if new and not first:
            send(f"🔔 소수의원 {name} 빈자리!\n{fmt_slots(new)}\n\n"
                 f"예약하기 👉 {item_url(item_id)}")
            log(f"{name}: 새 빈자리 {len(new)}개 알림 보냄")
        slots_state[item_id] = sorted(now_slots)
        time.sleep(random.uniform(1, 3))

    if WATCH_NOTICE:
        n = get_notice()
        old = state.get("notice")
        if n and old and n != old:
            msg = f"📢 소수의원 예약 공지가 바뀌었어요\n\n{notice_summary(n)}"
            ot = parse_open_time(n)
            if ot and ot > now_kst():
                msg += f"\n\n⏰ 오픈 1시간 전, 10분 전에 다시 알려드릴게요."
            send(msg + f"\n\n👉 {BIZ_URL}")
            log("공지 변경 알림 보냄")
        if n:
            state["notice"] = n

        # 오픈 시간 미리 알림
        ot = parse_open_time(state.get("notice", ""))
        if ot:
            left = (ot - now_kst()).total_seconds() / 60
            sent = state.setdefault("reminders", [])
            for mins, label in ((60, "1시간"), (10, "10분")):
                key = f"{ot.isoformat()}|{mins}"
                lower = 15 if mins == 60 else 0
                if lower < left <= mins and key not in sent:
                    send(f"⏰ 소수의원 예약 오픈 {label} 전!\n"
                         f"오픈: {fmt_dt(ot)}\n\n"
                         f"미리 네이버에 로그인해 두세요 👉 {BIZ_URL}")
                    sent.append(key)
                    log(f"오픈 {label} 전 알림 보냄")
            state["reminders"] = sent[-20:]

    if first:
        total = sum(len(slots_state[k]) for k in ITEMS)
        msg = f"✅ 알림 시작! 약 {CHECK_EVERY_MINUTES}분마다 확인합니다.\n현재 빈자리: {total}개"
        for item_id, name in ITEMS.items():
            if slots_state[item_id]:
                msg += f"\n\n[{name}]\n{fmt_slots(slots_state[item_id])}"
        if WATCH_NOTICE and state.get("notice"):
            msg += f"\n\n[현재 공지]\n{notice_summary(state['notice'])}"
        send(msg)
        state["initialized"] = True

    log("확인 완료 - " + ", ".join(f"{ITEMS[k]} {len(slots_state[k])}개" for k in ITEMS))


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ---------------- 실행 방식 ----------------
def run_local():
    log("소수의원 빈자리 알림 시작 (끄려면 Ctrl+C)")
    state = {}
    fails = 0
    while True:
        try:
            check_once(state)
            if fails >= 3:
                send("✅ 다시 정상적으로 확인 중이에요.")
            fails = 0
        except KeyboardInterrupt:
            raise
        except Exception as e:
            fails += 1
            log(f"확인 실패({fails}회): {e}")
            if fails == 3:
                send(f"⚠️ 예약 페이지 확인이 계속 실패하고 있어요. 자동으로 다시 시도합니다.\n({e})")
        wait = CHECK_EVERY_MINUTES * 60 * (1 + min(fails, 4)) + random.uniform(-20, 20)
        time.sleep(max(60, wait))


def run_github():
    """GitHub Actions: 정해진 시간 동안 반복 확인하고 상태를 state.json에 저장"""
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        log("GitHub Secrets에 TELEGRAM_TOKEN, TELEGRAM_CHAT_ID를 넣어주세요.")
        sys.exit(1)
    state = load_state()
    deadline = time.time() + GITHUB_RUN_MINUTES * 60
    while True:
        try:
            check_once(state)
            if state.get("fails", 0) >= 6:
                send("✅ 다시 정상적으로 확인 중이에요.")
            state["fails"] = 0
        except Exception as e:
            state["fails"] = state.get("fails", 0) + 1
            log(f"확인 실패({state['fails']}회): {e}")
            if state["fails"] == 6:
                send(f"⚠️ 예약 페이지 확인이 계속 실패하고 있어요. 자동으로 다시 시도합니다.\n({e})")
        save_state(state)
        wait = CHECK_EVERY_MINUTES * 60 + random.uniform(-20, 20)
        if time.time() + wait > deadline:
            break
        time.sleep(wait)
    log("이번 실행 종료")


def setup():
    if not TELEGRAM_TOKEN:
        print("먼저 파일 위쪽 TELEGRAM_TOKEN 에 봇 토큰을 넣어주세요.")
        return
    me = tg("getMe")["result"]
    print(f"봇 연결 OK: @{me['username']}")
    print("텔레그램에서 이 봇에게 아무 메시지(예: 안녕)를 보낸 뒤 Enter를 누르세요...")
    input()
    chats = {}
    for u in tg("getUpdates").get("result", []):
        m = u.get("message") or u.get("edited_message")
        if m:
            chats[m["chat"]["id"]] = m["chat"].get("first_name") or m["chat"].get("title")
    if not chats:
        print("메시지를 못 찾았어요. 봇에게 메시지를 보냈는지 확인하고 다시 실행해 주세요.")
        return
    for cid, name in chats.items():
        print(f"채팅 ID: {cid}  ({name})")
        tg("sendMessage", chat_id=cid, text="✅ 소수의원 알림 연결 테스트 성공!")
    print("\n위 숫자를 TELEGRAM_CHAT_ID 에 넣고 저장한 뒤 python sosu_alert.py 로 실행하세요.")


if __name__ == "__main__":
    try:
        if "--setup" in sys.argv:
            setup()
        elif "--github" in sys.argv:
            run_github()
        else:
            run_local()
    except KeyboardInterrupt:
        log("종료합니다.")
