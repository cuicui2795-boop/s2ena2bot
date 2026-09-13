import asyncio
import base64
import difflib
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone

import anthropic
from PIL import Image

import discord
import httpx
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

VERIFICATION_CHANNEL_ID = int(os.getenv("VERIFICATION_CHANNEL_ID", "0"))
VERIFIED_ROLE_ID = int(os.getenv("VERIFIED_ROLE_ID", "0"))
FOLLOWER_ROLE_ID = int(os.getenv("FOLLOWER_ROLE_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
TOLERANCE_MINUTES = int(os.getenv("TOLERANCE_MINUTES", "15"))
TIMEZONE_OFFSET = int(os.getenv("TIMEZONE_OFFSET", "9"))
INSTAGRAM_ACCOUNTS = [x.strip().lower() for x in os.getenv("INSTAGRAM_ACCOUNTS", "").split(",") if x.strip()]

# 한달 역할 자동 부여/제거
TRIGGER_ROLE_ID   = 1477170516434616331  # 이 역할을 받으면 → TEMP_ROLE 자동 부여
TEMP_ROLE_ID      = 1511322290187665418  # 한달 뒤 자동 제거할 역할
TEMP_ROLE_SECONDS = 30 * 24 * 3600      # 30일(초)
TIMERS_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'role_timers.json')

_claude_client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ── 구독자 추적 / 쿨다운 ───────────────────────────────────────
_SUBSCRIBER_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'subscriber_registry.json')

COOLDOWN_SECONDS = 60  # 거절 후 재시도 대기 시간(초)

def _load_subscriber_registry() -> dict:
    try:
        with open(_SUBSCRIBER_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_subscriber_registry(registry: dict):
    with open(_SUBSCRIBER_FILE, 'w') as f:
        json.dump(registry, f, indent=2)

_subscriber_registry: dict = _load_subscriber_registry()
_user_cooldowns: dict      = {}  # {discord_user_id: rejection_timestamp}
# ──────────────────────────────────────────────────────────────────


ANALYSIS_PROMPT = """Analyze this Instagram screenshot and identify which of the TWO valid screen types it is.

══════════════════════════════════════════════════
TYPE A — Instagram Profile Page
══════════════════════════════════════════════════
A user's Instagram profile page. Identified by a profile photo, bio, post grid, and action buttons.
- target_username: the username displayed at the TOP of the profile (no @ symbol).
- is_subscribed: true ONLY if an ACTIVE "already subscribed" button is visible (NOT the subscribe action):
    ⚠️ CRITICAL — Korean button text rules (read character by character):
      "구독 중"  (3 syllables: 구-독-중) = IS subscribed        ✅  → is_subscribed: true
      "구독"     (2 syllables: 구-독)    = NOT subscribed yet   ❌  → is_subscribed: false
      The character "중" (中) MUST be present. If the button shows only "구독" without "중",
      the user has NOT subscribed. Do NOT confuse these two.
    English:     "Subscribed" ✅   vs  "Subscribe"      ❌
    Japanese:    "登録済み"  ✅   vs  "登録する"       ❌
    Chinese:     "已订阅"    ✅   vs  "订阅"           ❌
    Spanish:     "Suscrito"  ✅   vs  "Suscribirse"    ❌
    French:      "Abonné"    ✅   vs  "S'abonner"      ❌
    Portuguese:  "Inscrito"  ✅   vs  "Inscrever-se"   ❌
    Arabic:      "مشترك"     ✅   vs  "اشترك"          ❌

══════════════════════════════════════════════════
TYPE B — Instagram Subscription Management Page
══════════════════════════════════════════════════
A billing/management page showing active Instagram subscriptions. There are multiple layouts:

  [Layout 1 — Subscription list]  (page title: "訂閱內容" / "Subscriptions" / "구독 관리" etc.)
    Each entry looks like:  "[price] · [username]'s subscription"
                            "將於 YYYY年M月D日續訂"  /  "Renews on [date]"

  [Layout 2 — Subscription detail]  (page title: "訂閱詳情" / "Subscription Details" etc.)
    Header:  "[username]'s subscription"  +  "每月定期付款" / "Monthly"
    Fields:  renewal status/date, subscription date, price, payment method, cancel button

  [Layout 4 — Meta-style subscription detail]
    Shows Meta (∞) logo at top, then:
    Header:  "[username]'s subscription"
             "Active since [date]" / "활성: [날짜]부터" / "자격: [날짜]"
    Row:     "[subscriber profile photo]  [subscriber username]  Subscribed account"
    Row:     "Subscription details   Renews on [date]   [price]"
    Row:     "Payment method"
    Row:     "Cancel subscription"
    - target_username: from "[username]'s subscription" title (NOT the "Subscribed account" name)
    - is_subscribed: true if "Renews on [date]" shows a FUTURE date

  [Layout 3 — Creators page]  (page title: "Creators")
    Shows:   "X active subscription(s)"
    Each entry: "[username]'s subscription  /  Renews on [date]  /  [price]"

  [Layout 5 — Creator subscription detail (newer Instagram UI)]
    Header: creator's profile photo + username shown like a mini profile (e.g. "s2ena2"),
            NOT phrased as "[username]'s subscription".
    Below the header: "[date]부터 활성화됨" / "Active since [date]" / "Active from [date]"
      — this confirms an ACTIVE, currently-valid subscription (no need for a "Renews on" line here).
    Row:    "[subscriber profile photo]  [subscriber username]  구독한 계정" / "Subscribed account"
    Section: "제공되는 혜택" / "Benefits" — a checklist (구독자 배지, 구독자 전용 콘텐츠, 소셜 채널 및 공지 채널, "더보기"/"See more")
    Section: "내 구독" / "My subscriptions" — appears BELOW the benefits list, contains a card:
             "구독 상세정보" / "Subscription details"     "[date]에 갱신" / "Renews on [date]"     [price]
             "결제 수단" / "Payment method"
    - target_username: the username shown in the profile-style header (e.g. "s2ena2")
    - subscriber_username: the username in the "구독한 계정"/"Subscribed account" row
    - is_subscribed: true — "[date]부터 활성화됨"/"Active since [date]" is proof of an active subscription
      on this layout.
    - renewal_date_iso: read from the "내 구독" → "구독 상세정보" card's "[date]에 갱신"/"Renews on [date]"
      text (e.g. "2026년 9월 23일에 갱신" → "2026-09-23"). null ONLY if the "내 구독" section itself is not
      visible in the screenshot (e.g. cut off before scrolling that far).

  [Layout 6 — "크리에이터"/"Creators" management list (Korean UI variant of Layout 3)]
    Header: star badge icon, title "크리에이터" ("Creator"), subtitle "N 활성화됨 구독" ("N active subscriptions")
    Section: "관리" ("Management") — list of entries, each:
             "[creator profile photo] [username]"   "[date]에 갱신" / "Renews on [date]"   [price]
    - target_username: the username in the entry row (e.g. "s2ena2")
    - is_subscribed: true if a "[date]에 갱신" date is shown for that entry (always upcoming/future)
    - renewal_date_iso: from that entry's "[date]에 갱신" text

Across ALL layouts:
- target_username: extract the account name from "[username]'s subscription" text (no @ symbol).
  IMPORTANT: extract ONLY the Instagram username before "'s subscription". Ignore any trailing
  numbers or suffixes that appear to be display artifacts (e.g. if you see "s2ena2_1's subscription",
  check if the base name "s2ena2" makes more sense as an Instagram username than "s2ena2_1").
  ⚠️ EXCEPTION — the two valid creator accounts for this bot are exactly "s2ena2" and
  "s2eeena1_214" (read character by character: s-2-e-e-e-n-a-1-underscore-2-1-4 — THREE "e"s in a
  row, then "na1", then "_214"). "_214" here is part of the real handle, NOT a display artifact —
  never strip it. Read the username slowly letter-by-letter rather than guessing a "cleaner-looking"
  variant; "s2eena1", "s2eeena1", "s2eeena1_21" etc. are OCR mistakes, not valid alternatives.
  If a Layout 6 "크리에이터"/"Creators" list has MULTIPLE entries, target_username/is_subscribed/
  renewal_date_iso must come from whichever entry's username matches "s2ena2" or "s2eeena1_214" —
  never return the page's own header/title (e.g. "크리에이터") as target_username.
- is_subscribed: true if a FUTURE renewal date is visible, indicating active subscription.

  Renewal indicators by language:
    English:        "Renews on", "Next billing date"
    Korean:         "갱신", "다음 갱신일"
    Chinese (TW):   "將於...續訂", "下次付款期限", "將於...續訂"
    Chinese (CN):   "续订日期", "下次扣款"
    Japanese:       "次回更新日", "更新日"
    Any language:   a date shown as the upcoming renewal/billing date

══════════════════════════════════════════════════
TYPE C — OS-level Subscription Management Page (Apple / Google)
══════════════════════════════════════════════════
The device's built-in subscription manager, NOT inside the Instagram app.

  Apple iOS (Settings > [Apple ID] > Subscriptions):
    Section headers: "활성 상태" (KO) / "Active" (EN) / "アクティブ" (JA) / "使用中" (ZH)
    Each entry: [App icon]  [App name e.g. "Instagram"]
                            [Subscription plan name = Instagram username]  [price]
                            "[date]에 갱신 예정" / "Renews [date]" / "將於...續訂"

  Google Play (Play Store > Subscriptions):
    Similar layout showing app name + subscription plan + renewal date.

For TYPE C:
- screenshot_type: use "subscription_page"
- target_username: the subscription PLAN NAME shown directly under "Instagram" in the list.
  This is the Instagram account username the user subscribed to.
- is_subscribed: true if a FUTURE renewal date is visible.

══════════════════════════════════════════════════

Extract ALL fields below:

1. screenshot_type — "profile" (Type A), "subscription_page" (Type B), or "unknown".

2. target_username —
   Type A: exact username at top of profile (lowercase, no @).
   Type B: account name from "X's subscription" title (lowercase, no @).

3. is_subscribed — See type-specific rules above.

4. renewal_date_iso — (Type B / Type C ONLY) The renewal/next-billing date shown in the screenshot,
   formatted strictly as "YYYY-MM-DD". Extract from any text like:
     "Renews on July 27, 2026"  → "2026-07-27"
     "2026年7月27日續訂"        → "2026-07-27"
     "2026년 7월 27일 갱신"      → "2026-07-27"
     "2026.7.27 갱신예정"        → "2026-07-27"
   null for Type A (profile page) or if no renewal date is visible.

5. clock_time_text — Time in the smartphone status bar at the very top of the screen
   (e.g. "오후 2:21", "14:21", "2:21 PM"). null if not visible.

6. clock_time_hour — Hour as integer (raw value from clock, e.g. 2 for "오후 2:21").

7. clock_time_minute — Minute as integer.

8. clock_is_pm — true or false for 12-hour format clocks only. null if the clock uses 24-hour format.

9. subscriber_username — (subscription_page ONLY) The Instagram username of the SUBSCRIBER
   (the person who purchased the subscription). This appears as:
     Layout 4: "[subscriber profile photo]  [username]  Subscribed account"
     Layout 1/2/3: typically not shown
   lowercase, no @. null for profile pages or if not visible.

10. confidence — "high", "medium", or "low".

11. notes — Any relevant observations.

Respond ONLY in this JSON format (no other text):

{
  "screenshot_type": "profile",
  "target_username": "username",
  "subscriber_username": null,
  "is_subscribed": true,
  "renewal_date_iso": null,
  "clock_time_text": "오후 2:21",
  "clock_time_hour": 2,
  "clock_time_minute": 21,
  "clock_is_pm": true,
  "confidence": "high",
  "notes": ""
}"""


VERIFICATION_CRITERIA = (
    "📋 **구독봇 인증 통과 조건**\n\n"
    "인스타그램 메뉴 → 하단 크리에이터 구독 **(X)**\n"
    "설정 → 계정센터 → 구독 → 관리 **(O)**\n\n"
    "**s2ena2** 구독 갱신날짜 포함 스크린샷\n\n"
    "업로드하시면 자동으로 권한이 부여됩니다!\n\n"
    f"⏰ 스크린샷 내 시간의 오차가 **{TOLERANCE_MINUTES}분 이상** 차이나지 않아야 합니다.\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "📋 **Subscriber Bot Verification Requirements**\n\n"
    "Instagram Menu → Bottom Creator Subscription **(X)**\n"
    "Settings → Account Center → Subscriptions → Manage **(O)**\n\n"
    "Screenshot including the **s2ena2** subscription renewal date\n\n"
    "If you upload it, permission will be automatically granted!\n\n"
    f"⏰ The time shown in the screenshot must not differ by more than **{TOLERANCE_MINUTES} minute**.\n\n"
    "🔗 https://www.instagram.com/s2ena2?igsh=bWh4djR6NXJ4OXpq&utm_source=qr%3A15"
)

RESET_ANNOUNCEMENT = (
    "✅ **구독자 역할 초기화 완료!**\n"
    "전체 인원의 구독자 역할 제거가 완료되었습니다.\n"
    "다시 스크린샷을 올려주시면 봇이 대조 후 구독 역할을 지급해드립니다! 📸\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "✅ **Subscriber Role Reset Complete!**\n"
    "All subscriber roles have been removed.\n"
    "Please upload your screenshot again — the bot will verify it and re-grant the subscriber role! 📸"
)


def _parse_analysis(raw: str) -> dict:
    """Claude 응답에서 JSON 추출."""
    text = raw.strip()
    for marker in ("```json", "```"):
        if marker in text:
            text = text.split(marker, 1)[1].rsplit("```", 1)[0].strip()
            break
    return json.loads(text)


def _fuzzy_match_account(detected: str, accounts: list) -> str | None:
    """OCR 오류 허용 유사도 매칭 (85% 이상 유사하면 매칭된 계정명 반환)."""
    if not detected or not accounts:
        return None
    if detected in accounts:
        return detected
    matches = difflib.get_close_matches(detected, accounts, n=1, cutoff=0.85)
    return matches[0] if matches else None


async def _download_image(url: str) -> bytes:
    """Discord 첨부 이미지 다운로드."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    return resp.content


_SUPPORTED_MEDIA_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}


async def _analyze_screenshot(img_bytes: bytes) -> dict:
    """Claude Vision으로 스크린샷 분석."""
    # 5MB 초과 시 JPEG으로 압축, 지원하지 않는 포맷도 JPEG으로 변환
    MAX_BYTES = 5 * 1024 * 1024
    raw_bytes = img_bytes
    img_format = Image.open(io.BytesIO(raw_bytes)).format
    media_type = _SUPPORTED_MEDIA_TYPES.get(img_format)

    if len(raw_bytes) > MAX_BYTES or not media_type:
        orig_size = len(raw_bytes)
        img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        quality = 85
        while quality >= 40:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            raw_bytes = buf.getvalue()
            if len(raw_bytes) <= MAX_BYTES:
                break
            quality -= 15
        media_type = "image/jpeg"
        print(f"[이미지 압축] {orig_size//1024}KB → {len(raw_bytes)//1024}KB (quality={quality})")

    image_b64 = base64.b64encode(raw_bytes).decode("ascii")
    today_local = (datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_OFFSET)).strftime("%Y-%m-%d")
    prompt = ANALYSIS_PROMPT + (
        f"\n\nIMPORTANT — Today's date is {today_local}. Renewal/billing dates shown as upcoming are "
        "ALWAYS in the future relative to today. If the screenshot's date text does not show an explicit "
        "year (e.g. just \"9월 30일\" / \"Sep 30\"), infer the year so the resulting date is the nearest "
        "date on or after today — never output a year that makes the date fall in the past."
    )
    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": prompt},
        ],
    }]

    # 할당량 초과/일시적 장애 시 순서대로 폴백 모델 시도, 전부 실패하면 잠시 대기 후 재시도
    _MODELS = ["claude-haiku-4-5-20251001", "claude-sonnet-5"]
    _RETRY_ROUNDS = 3
    _RETRY_DELAY_SECONDS = 5
    _MAX_RETRY_DELAY_SECONDS = 30
    last_err = None
    for round_num in range(1, _RETRY_ROUNDS + 1):
        round_delay = _RETRY_DELAY_SECONDS
        for model in _MODELS:
            try:
                response = await _claude_client.messages.create(
                    model=model,
                    max_tokens=1024,
                    messages=messages,
                )
                if model != _MODELS[0] or round_num != 1:
                    print(f"[폴백 성공] {model} 모델로 분석 완료 (시도 {round_num}회차)")
                return _parse_analysis(response.content[0].text)
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "rate_limit" in err_str or "529" in err_str or "overloaded" in err_str or "503" in err_str or "500" in err_str or "internal_server_error" in err_str:
                    print(f"[폴백] {model} 일시적 오류 → 다음 모델 시도")
                    last_err = e
                    # API가 알려주는 실제 재시도 대기시간(retry-after)이 있으면 그걸 우선 사용
                    m = re.search(r"retry-after['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)", err_str, re.IGNORECASE)
                    if m:
                        round_delay = max(round_delay, min(float(m.group(1)), _MAX_RETRY_DELAY_SECONDS))
                    continue
                raise
        if round_num < _RETRY_ROUNDS:
            print(f"[재시도] 모든 모델 일시적 오류 → {round_delay}초 후 재시도 ({round_num}/{_RETRY_ROUNDS})")
            await asyncio.sleep(round_delay)
    raise last_err


def _compare_timestamps(analysis: dict, message_time: datetime) -> tuple[bool, str]:
    """
    폰 시계 vs Discord 메시지 시간 비교.

    폰에 표시된 시각(시+분)과 Discord 메시지 전송 UTC 시각의 차이가
    유효한 타임존 오프셋(UTC-12 ~ UTC+14, 정수 시간)에 해당하는지 검증합니다.
    어느 나라 사용자든 자국 시간으로 찍은 스크린샷이면 통과합니다.
    """
    minute = analysis.get("clock_time_minute")
    hour_raw = analysis.get("clock_time_hour")
    is_pm = analysis.get("clock_is_pm")
    clock_text = analysis.get("clock_time_text") or "알 수 없음"

    if minute is None or hour_raw is None:
        return False, (
            f"스크린샷에서 시계를 읽지 못했습니다. (신뢰도: {analysis.get('confidence', '?')})\n"
            "**스크린샷 상단 상태바에 현재 시각이 보여야 합니다.**\n"
            "상태바가 가려지지 않도록 전체 화면 그대로 캡처해주세요."
        )

    # 24시간으로 변환
    if is_pm is True:
        hour_24 = (hour_raw % 12) + 12   # 오후 12시=12, 오후 1시=13 … 오후 11시=23
    elif is_pm is False:
        hour_24 = hour_raw % 12           # 오전 12시=0, 오전 1시=1 … 오전 11시=11
    else:
        hour_24 = hour_raw                # 24시간 표기 그대로

    utc_hour = message_time.hour
    utc_minute = message_time.minute

    # 폰 시간 - UTC 시간 (분 단위)
    diff_raw = hour_24 * 60 + minute - (utc_hour * 60 + utc_minute)

    # UTC-12 ~ UTC+14 범위로 정규화 (날짜 경계 처리)
    while diff_raw < -12 * 60:
        diff_raw += 24 * 60
    while diff_raw > 14 * 60:
        diff_raw -= 24 * 60

    # 분 나머지: 타임존 오프셋의 분 부분 (±30 기준)
    minute_part = diff_raw % 60
    if minute_part > 30:
        minute_part -= 60
    hour_offset = (diff_raw - minute_part) // 60

    effective_tolerance = TOLERANCE_MINUTES
    ok = abs(minute_part) <= effective_tolerance and (-12 <= hour_offset <= 14)

    detail = (
        f"폰 시계: {clock_text}\n"
        f"메시지 전송: {utc_hour:02d}:{utc_minute:02d} (UTC)\n"
        f"추정 타임존: UTC{hour_offset:+d}\n"
        f"분 차이: {abs(minute_part)}분 / 허용 오차: {TOLERANCE_MINUTES}분"
    )
    return ok, detail


async def _send_log(bot: commands.Bot, embed: discord.Embed):
    if not LOG_CHANNEL_ID:
        return
    ch = bot.get_channel(LOG_CHANNEL_ID)
    if ch:
        await ch.send(embed=embed)


class Verification(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._processing_msg_ids: set[int] = set()  # 중복 처리 방지
        print(f"[인증봇 설정]")
        print(f"  인증 채널 ID : {VERIFICATION_CHANNEL_ID}")
        print(f"  인증 역할 ID : {VERIFIED_ROLE_ID}")
        print(f"  로그 채널 ID : {LOG_CHANNEL_ID or '비활성'}")
        print(f"  허용 오차    : {TOLERANCE_MINUTES}분")
        print(f"  타임존       : UTC+{TIMEZONE_OFFSET}")
        print(f"  인증 계정    : {', '.join(INSTAGRAM_ACCOUNTS) if INSTAGRAM_ACCOUNTS else '미설정(전체 허용)'}")
        print(f"[대기 중] 인증 채널에서 스크린샷을 기다리고 있습니다...")

    # ------------------------------------------------------------------
    # 시작 시 미처리 메시지 처리
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self):
        channel = self.bot.get_channel(VERIFICATION_CHANNEL_ID)
        if not channel:
            print(f"[경고] 인증 채널({VERIFICATION_CHANNEL_ID})을 찾을 수 없습니다.")
            return

        print(f"[시작 검사] 미처리 메시지 확인 중...")

        # 5일 이내만 대상 (Discord 첨부 URL 만료 대비) — 개수 제한 없이 시간 기준으로 조회해야
        # 채널이 활발해 2000개 넘게 쌓여도 오래된 미처리 메시지를 놓치지 않는다.
        now_utc = datetime.now(timezone.utc)
        since = now_utc - timedelta(minutes=7200)

        # 봇이 이미 답한 메시지 ID 수집
        replied_to = set()
        async for msg in channel.history(limit=None, after=since):
            if msg.author == self.bot.user and msg.reference:
                replied_to.add(msg.reference.message_id)

        # 이미지가 있고 아직 처리 안 된 메시지 처리
        pending = []
        async for msg in channel.history(limit=None, after=since):
            if msg.author.bot:
                continue
            images = [a for a in msg.attachments if (a.content_type or "").startswith("image/")]
            if images and msg.id not in replied_to:
                pending.append(msg)

        if not pending:
            print(f"[시작 검사] 미처리 메시지 없음.")
        else:
            print(f"[시작 검사] 미처리 메시지 {len(pending)}개 발견, 처리 시작...")
            for msg in reversed(pending):  # 오래된 것부터 처리
                try:
                    await self._process_message(msg)
                except Exception as e:
                    print(f"[오류] 메시지 처리 중 예외 발생 ({msg.author}): {e}")

        print(f"[대기 중] 인증 채널에서 스크린샷을 기다리고 있습니다...")

        # 봇 재시작 후 미완료 한달 역할 타이머 복구
        await self._restore_role_timers()

    # ------------------------------------------------------------------
    # 한달 역할 자동 부여/제거
    # ------------------------------------------------------------------

    def _load_timers(self) -> dict:
        try:
            with open(TIMERS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_timers(self, timers: dict):
        with open(TIMERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(timers, f, indent=2)

    async def _restore_role_timers(self):
        """봇 재시작 후 저장된 타이머를 읽어 남은 시간만큼 대기 후 역할 제거.
        또한 트리거 역할은 있지만 한달 역할이 없는 멤버에게 자동으로 부여."""
        timers = self._load_timers()
        now = datetime.utcnow().timestamp()
        changed = False

        # 1) 기존 타이머 복구
        for member_id_str, expiry in list(timers.items()):
            remaining = expiry - now
            if remaining <= 0:
                member_id = int(member_id_str)
                for guild in self.bot.guilds:
                    member = guild.get_member(member_id)
                    if member:
                        role = guild.get_role(TEMP_ROLE_ID)
                        if role and role in member.roles:
                            try:
                                await member.remove_roles(role, reason="한달 구독 역할 만료(재시작 후 처리)")
                                print(f"[역할만료] {member} — 재시작 후 즉시 제거")
                            except (discord.Forbidden, discord.HTTPException):
                                pass
                        break
                timers.pop(member_id_str)
                changed = True
            else:
                asyncio.create_task(self._delayed_role_remove(int(member_id_str), expiry))
                print(f"[역할타이머 복구] member_id={member_id_str}, 남은 시간={remaining/3600:.1f}시간")

        # 2) 트리거 역할 있지만 한달 역할 없는 멤버 → 지금부터 30일 부여
        for guild in self.bot.guilds:
            trigger_role = guild.get_role(TRIGGER_ROLE_ID)
            temp_role    = guild.get_role(TEMP_ROLE_ID)
            if not trigger_role or not temp_role:
                continue
            for member in guild.members:
                if trigger_role not in member.roles:
                    continue
                if temp_role in member.roles:
                    continue
                if str(member.id) in timers:
                    continue  # 이미 타이머 있음
                # 한달 역할 부여
                expiry = now + TEMP_ROLE_SECONDS
                try:
                    await member.add_roles(temp_role, reason="한달 구독 역할 자동 부여(시작 시 누락 복구)")
                    print(f"[한달역할 복구] {member}에게 역할 부여 — 만료: {datetime.utcfromtimestamp(expiry).strftime('%Y-%m-%d %H:%M UTC')}")
                except (discord.Forbidden, discord.HTTPException) as e:
                    print(f"[한달역할 복구 오류] {member}: {e}")
                    continue
                timers[str(member.id)] = expiry
                changed = True
                asyncio.create_task(self._delayed_role_remove(member.id, expiry))

        if changed:
            self._save_timers(timers)

    async def _delayed_role_remove(self, member_id: int, expiry: float):
        """expiry(UTC timestamp)까지 대기 후 TEMP_ROLE 제거."""
        remaining = expiry - datetime.utcnow().timestamp()
        if remaining > 0:
            await asyncio.sleep(remaining)

        # 타이머가 재설정됐는지 확인 (재부여 시 덮어씌워짐)
        timers = self._load_timers()
        if timers.get(str(member_id)) != expiry:
            return  # 다른 타이머가 담당

        member = None
        for guild in self.bot.guilds:
            member = guild.get_member(member_id)
            if member:
                break

        if member:
            role = member.guild.get_role(TEMP_ROLE_ID)
            if role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="한달 구독 역할 만료")
                    print(f"[역할만료] {member} — 한달 역할 자동 제거 완료")
                except (discord.Forbidden, discord.HTTPException) as e:
                    print(f"[역할만료 오류] {member_id}: {e}")

        timers = self._load_timers()
        if timers.get(str(member_id)) == expiry:
            timers.pop(str(member_id), None)
            self._save_timers(timers)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """TRIGGER_ROLE 부여 감지 → TEMP_ROLE 자동 부여 + 30일 타이머 설정."""
        before_ids = {r.id for r in before.roles}
        after_ids  = {r.id for r in after.roles}

        if TRIGGER_ROLE_ID not in after_ids or TRIGGER_ROLE_ID in before_ids:
            return  # 역할이 새로 추가된 게 아님

        temp_role = after.guild.get_role(TEMP_ROLE_ID)
        if not temp_role:
            print(f"[한달역할 오류] TEMP_ROLE_ID({TEMP_ROLE_ID})를 찾을 수 없습니다.")
            return

        expiry = datetime.utcnow().timestamp() + TEMP_ROLE_SECONDS

        try:
            await after.add_roles(temp_role, reason="한달 구독 역할 자동 부여")
            print(f"[한달역할] {after}에게 역할 부여 — 만료: {datetime.utcfromtimestamp(expiry).strftime('%Y-%m-%d %H:%M UTC')}")
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[한달역할 오류] {after}: {e}")
            return

        timers = self._load_timers()
        timers[str(after.id)] = expiry
        self._save_timers(timers)

        asyncio.create_task(self._delayed_role_remove(after.id, expiry))

    # ------------------------------------------------------------------
    # 메시지 감시
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # 우리 봇 자신의 메시지 — 사진 자동삭제만 처리
        if message.author == self.bot.user:
            await self._check_and_schedule_delete(message)
            return

        # !승아사진 명령어 채널 안내 (사람 유저만)
        if not message.author.bot:
            if message.channel.id == 1477186081203159235 and message.content.strip().startswith("!승아사진"):
                await message.reply(f"📸 사진은 <#1511432005143888002> 채널에서 확인해주세요!", mention_author=False)
                return

        # 인증 채널 처리 (사람 유저만)
        if not message.author.bot and message.channel.id == VERIFICATION_CHANNEL_ID:
            images = [a for a in message.attachments if (a.content_type or "").startswith("image/")]
            if images:
                await self._process_message(message)
            return

        # 인증 채널은 사진 자동 삭제 제외
        if message.channel.id == VERIFICATION_CHANNEL_ID:
            return

        await self._check_and_schedule_delete(message)

    EXEMPT_ROLE_IDS = {1477182447287402537, 1492183743920869448}

    def _has_image(self, message: discord.Message) -> bool:
        MEDIA_EXTS = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.heic', '.heif',
                      '.mp4', '.mov', '.avi', '.webm', '.mkv', '.flv', '.wmv', '.m4v')
        if any(
            (a.content_type or "").startswith("image/")
            or (a.content_type or "").startswith("video/")
            or a.filename.lower().endswith(MEDIA_EXTS)
            for a in message.attachments
        ):
            return True
        if any(e.image or e.thumbnail or e.type in ("image", "gifv", "video") for e in message.embeds):
            return True
        return False

    def _is_exempt(self, message: discord.Message) -> bool:
        if message.author.bot:
            return False
        member = message.author
        if not isinstance(member, discord.Member):
            return False
        return (
            member.guild_permissions.administrator
            or any(r.id in self.EXEMPT_ROLE_IDS for r in member.roles)
        )

    PHOTO_BOT_ID = 1477596447703699497  # 승아사진봇
    SHOP_GUILD_ID = 1475081549828460706
    PET_GUILD_ID = 1464701988737388869

    def _is_photo_bot_photo(self, message: discord.Message) -> bool:
        """승아사진봇의 사진 명령어(!사진/!승아사진/!구독사진) 사진인지 확인
        사진 명령: 첨부파일만 있고 embed 없음
        펫 명령: embed 있음 → 삭제 대상 아님"""
        return (
            message.author.id == self.PHOTO_BOT_ID
            and message.attachments
            and not message.embeds
        )

    PROTECTED_CHANNEL_IDS = {1511935851179802725}

    async def _check_and_schedule_delete(self, message: discord.Message):
        if not message.guild:
            return
        if message.channel.id in self.PROTECTED_CHANNEL_IDS:
            return
        guild_id = message.guild.id

        if guild_id == self.SHOP_GUILD_ID:
            # 승아사진봇의 사진 명령어 사진만 1분 후 삭제 (펫 제외, 일반유저 제외)
            if not self._is_photo_bot_photo(message):
                return
            delay = 60
        elif guild_id == self.PET_GUILD_ID:
            # 승아사진봇의 사진 명령어 사진만 1분 후 삭제 (펫 제외, 일반유저/다른봇 제외)
            if not self._is_photo_bot_photo(message):
                return
            delay = 60
        else:
            # 다른 서버: 기존 로직 유지
            if message.channel.id == VERIFICATION_CHANNEL_ID:
                return
            if not self._has_image(message):
                return
            if self._is_exempt(message):
                return
            delay = 60

        label = f"{delay // 60}분"
        print(f"[미디어감지] {message.channel.name} | {message.author} — {label} 후 삭제 예정")
        async def _delayed_delete(msg, d, lbl):
            await asyncio.sleep(d)
            try:
                await msg.delete()
                print(f"[미디어삭제] {msg.channel.name} | {msg.author} — {lbl} 후 자동 삭제")
            except (discord.NotFound, discord.Forbidden):
                pass
        asyncio.create_task(_delayed_delete(message, delay, label))

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """URL auto-embed이 나중에 추가되는 경우 처리"""
        if after.author == self.bot.user:
            return
        if after.channel.id == VERIFICATION_CHANNEL_ID:
            return
        # embed가 새로 추가된 경우만 처리
        if len(before.embeds) < len(after.embeds):
            await self._check_and_schedule_delete(after)

    async def _process_message(self, message: discord.Message):
        images = [a for a in message.attachments if (a.content_type or "").startswith("image/")]
        if not images:
            return

        # 같은 메시지를 두 번 처리하지 않도록 중복 방지
        if message.id in self._processing_msg_ids:
            print(f"[중복 방지] 이미 처리 중인 메시지 무시: {message.id}")
            return
        self._processing_msg_ids.add(message.id)

        # 이미 인증된 사용자면 재분석 없이 스킵 (짧은 시간 내 중복 업로드로 인한 중복 승인 메시지 방지)
        verified_role = message.guild.get_role(VERIFIED_ROLE_ID) if VERIFIED_ROLE_ID else None
        if verified_role and verified_role in message.author.roles:
            print(f"[스킵] 이미 인증된 사용자 | {message.author}")
            await message.reply("✅ 이미 인증되어 있습니다!", mention_author=False, delete_after=10)
            self._processing_msg_ids.discard(message.id)
            return

        # ── 쿨다운 체크 (거절 후 60초 대기) ──────────────────────────
        now_ts = datetime.now(timezone.utc).timestamp()
        last_rejected = _user_cooldowns.get(message.author.id, 0)
        remaining = int(COOLDOWN_SECONDS - (now_ts - last_rejected))
        if remaining > 0:
            print(f"[쿨다운] {message.author} — {remaining}초 남음")
            status_msg = await message.reply(f"⏳ {remaining}초 후에 다시 시도해주세요.", mention_author=False)
            self._processing_msg_ids.discard(message.id)
            return
        # ──────────────────────────────────────────────────────────

        print(f"[인증 요청] {message.author} ({message.author.id}) | 이미지: {images[0].filename}")
        status_msg = await message.reply("⏳ 스크린샷 분석 중... 잠시만 기다려주세요.")

        # 이미지 다운로드
        try:
            img_bytes = await _download_image(images[0].url)
        except Exception as e:
            print(f"[오류] 이미지 다운로드 실패: {e}")
            await status_msg.edit(content=f"⚠️ 이미지를 다운로드할 수 없습니다. 잠시 후 다시 시도해주세요.\n`{e}`")
            return

        try:
            print(f"[AI 분석] Claude Vision 호출 중...")
            analysis = await _analyze_screenshot(img_bytes)
            print(f"[AI 분석 완료] {analysis}")
        except Exception as e:
            err_str = str(e)
            print(f"[오류] 이미지 분석 실패: {e}")
            if "429" in err_str or "rate_limit" in err_str:
                await status_msg.edit(content="⏰ AI 분석 서비스가 일시적으로 한도에 도달했어요.\n**잠시 후 다시 시도해주세요!** (이미지가 올바른 경우 재업로드하시면 됩니다)")
            elif "529" in err_str or "overloaded" in err_str or "503" in err_str or "500" in err_str:
                await status_msg.edit(content="⏳ AI 분석 서비스가 일시적으로 요청량이 많아요.\n**잠시 후 스크린샷을 다시 올려주세요!** 🙏")
            else:
                await status_msg.edit(content=f"⚠️ 이미지 분석 중 오류가 발생했습니다. 관리자에게 문의하세요.\n`{e}`")
            return

        screenshot_type = analysis.get("screenshot_type", "unknown")

        async def _reject(msg: str):
            """거절 공통 처리: 쿨다운 설정 + 상태 메시지 갱신 + 인증 조건 채팅 안내."""
            _user_cooldowns[message.author.id] = datetime.now(timezone.utc).timestamp()
            await status_msg.edit(content=msg)
            await message.channel.send(VERIFICATION_CRITERIA)

        # 인스타그램 화면 확인
        if screenshot_type == "unknown":
            print(f"[거절] 인식 불가 화면 | {message.author}")
            await _reject(
                "❌ 인증 가능한 화면이 아닙니다.\n\n"
                "설정 → 계정센터 → 구독 → 관리 페이지에서 갱신일이 보이는 스크린샷을 올려주세요."
            )
            return

        # 프로필 화면("구독 중" 버튼)은 더 이상 인증 방식으로 허용하지 않음
        if screenshot_type == "profile":
            print(f"[거절] 프로필 화면(인증 불가 방식) | {message.author}")
            await _reject(
                "❌ 프로필 화면(**구독 중** 버튼)으로는 인증할 수 없습니다.\n"
                "설정 → 계정센터 → 구독 → 관리 페이지의 스크린샷을 올려주세요."
            )
            return

        # 계정 확인
        if INSTAGRAM_ACCOUNTS:
            target = (analysis.get("target_username") or "").lower().strip()
            matched = _fuzzy_match_account(target, INSTAGRAM_ACCOUNTS)
            if not matched:
                account_list = ", ".join(f"@{a}" for a in INSTAGRAM_ACCOUNTS)
                print(f"[거절] 인증 대상 계정 아님 | 감지된 계정: '{target}' | 타입: {screenshot_type} | {message.author}")
                await _reject(
                    f"❌ 인증 대상 계정이 아닙니다.\n아래 계정 중 하나의 화면을 올려주세요.\n{account_list}"
                )
                return
            if matched != target:
                print(f"[퍼지매칭] '{target}' → '{matched}' (OCR 보정)")

        # 구독 확인
        if not analysis.get("is_subscribed"):
            print(f"[거절] 구독 상태 미확인 | 타입: {screenshot_type} | {message.author}")
            await _reject("❌ 구독 중이 아닙니다! 구독 후 인증해주세요 🙏")
            return

        # 구독 관리 페이지 전용 검사
        if screenshot_type == "subscription_page":
            # 갱신일 검사
            renewal_str = (analysis.get("renewal_date_iso") or "").strip()
            if renewal_str:
                try:
                    from datetime import date as _date
                    renewal_date = _date.fromisoformat(renewal_str)
                    msg_date = message.created_at.date()

                    # 스크린샷에 연도가 표시되지 않아 AI가 연도를 잘못 추정한 경우 보정
                    # (갱신 예정일은 항상 미래이므로, 과거로 나오면 가장 가까운 미래 연도로 굴림)
                    if renewal_date < msg_date:
                        try:
                            candidate = renewal_date.replace(year=msg_date.year)
                            if candidate < msg_date:
                                candidate = candidate.replace(year=msg_date.year + 1)
                            renewal_date = candidate
                        except ValueError:
                            pass  # 2/29처럼 치환 불가능한 날짜는 원본 유지

                    if renewal_date < msg_date:
                        print(f"[거절] 갱신일 만료 | 갱신일: {renewal_str} | {message.author}")
                        await _reject(
                            f"❌ 구독 갱신일({renewal_str})이 이미 지났습니다.\n구독을 갱신한 후 다시 인증해주세요 🙏"
                        )
                        return
                except ValueError:
                    print(f"[경고] 갱신일 파싱 실패: {renewal_str!r}")
            else:
                print(f"[거절] 갱신일 미확인 | {message.author}")
                await _reject(
                    "❌ 구독 갱신일을 확인할 수 없습니다.\n갱신일이 명확히 보이는 스크린샷을 올려주세요 🙏"
                )
                return

            # ── 구독자 계정 중복 검사 (구독 공유 차단) ───────────────
            subscriber = (analysis.get("subscriber_username") or "").lower().strip()
            if subscriber:
                registered_uid = _subscriber_registry.get(subscriber)
                if registered_uid and registered_uid != message.author.id:
                    print(f"[거절] 구독 계정 공유 | subscriber='{subscriber}' 이미 uid={registered_uid} 사용 | {message.author}")
                    await _reject(
                        "❌ 이 구독 계정은 이미 다른 멤버가 인증에 사용했습니다.\n"
                        "본인의 구독 화면을 올려주세요 🙏"
                    )
                    return
            # ──────────────────────────────────────────────────────

        approved, detail = _compare_timestamps(analysis, message.created_at)
        print(f"[시간 비교] {'통과' if approved else '실패'} | {detail.replace(chr(10), ' | ')}")

        if approved:
            # ── 승인: 구독자 저장 ─────────────────────────────────
            if screenshot_type == "subscription_page":
                subscriber = (analysis.get("subscriber_username") or "").lower().strip()
                if subscriber:
                    _subscriber_registry[subscriber] = message.author.id
                    _save_subscriber_registry(_subscriber_registry)
            # ──────────────────────────────────────────────────────
            await self._approve_user(message, status_msg, detail, analysis)
        else:
            _user_cooldowns[message.author.id] = datetime.now(timezone.utc).timestamp()
            await self._reject_user(message, status_msg, detail)

    # ------------------------------------------------------------------
    # 승인/거절 내부 처리
    # ------------------------------------------------------------------

    async def _approve_user(
        self,
        message: discord.Message,
        status_msg: discord.Message,
        detail: str,
        analysis: dict,
    ):
        guild = message.guild
        role = guild.get_role(VERIFIED_ROLE_ID) if VERIFIED_ROLE_ID else None

        if role:
            try:
                roles_to_add = [role]
                follower_role = message.guild.get_role(FOLLOWER_ROLE_ID) if FOLLOWER_ROLE_ID else None
                if follower_role:
                    roles_to_add.append(follower_role)
                await message.author.add_roles(*roles_to_add, reason="인스타그램 팔로우 인증 자동 완료")
                print(f"[승인] {message.author}에게 {[r.name for r in roles_to_add]} 역할 부여 완료")
                await status_msg.edit(
                    content=f"✅ 인증 완료! {role.mention} 역할이 부여되었습니다.\n\n{detail}"
                )
            except discord.Forbidden:
                print(f"[오류] 역할 부여 권한 없음 — 봇 역할이 '승이단(구독)' 역할보다 위에 있는지 확인하세요.")
                await status_msg.edit(content="⚠️ 역할 부여 권한이 없습니다. 관리자에게 문의하세요.")
                return
            except Exception as e:
                print(f"[오류] 역할 부여 실패: {e}")
                await status_msg.edit(content=f"⚠️ 역할 부여 중 오류 발생: `{e}`")
                return
        else:
            print(f"[경고] 역할 ID {VERIFIED_ROLE_ID}를 찾을 수 없음")
            await status_msg.edit(
                content=f"✅ 인증은 확인됐지만 역할을 찾을 수 없습니다. 관리자에게 문의하세요.\n\n{detail}"
            )

        type_label = {"profile": "프로필 구독 중", "subscription_page": "구독 관리 페이지"}.get(analysis.get("screenshot_type", ""), "알 수 없음")
        embed = discord.Embed(title="✅ 인증 완료", color=0x00C851)
        embed.add_field(name="사용자", value=f"{message.author.mention} (`{message.author}`)", inline=False)
        embed.add_field(name="인증 방식", value=type_label, inline=True)
        embed.add_field(name="인증 계정", value=f"@{analysis.get('target_username', '?')}", inline=True)
        embed.add_field(name="결과", value=detail, inline=False)
        embed.set_footer(text=f"신뢰도: {analysis.get('confidence', '?')}")
        await _send_log(self.bot, embed)

        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
        try:
            await status_msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
        await message.channel.send(f"✅ {message.author.mention} 인증되었습니다!")

    async def _reject_user(
        self,
        message: discord.Message,
        status_msg: discord.Message,
        detail: str,
    ):
        await status_msg.edit(
            content=(
                f"❌ **시간 불일치** {message.author.mention}\n\n"
                f"{detail}\n\n"
                "스크린샷 찍은 후 **1분 이내**에 업로드해주세요! 📸"
            )
        )

        embed = discord.Embed(title="❌ 인증 실패", color=0xFF4444)
        embed.add_field(name="사용자", value=f"{message.author.mention} (`{message.author}`)", inline=False)
        embed.add_field(name="사유", value=detail, inline=False)
        await _send_log(self.bot, embed)

        await message.channel.send(VERIFICATION_CRITERIA)

        async def _auto_delete():
            await asyncio.sleep(120)
            try:
                await message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
            try:
                await status_msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        asyncio.create_task(_auto_delete())

    # ------------------------------------------------------------------
    # 관리자 명령어
    # ------------------------------------------------------------------

    @commands.command(name="숭이")
    async def cmd_sungyi(self, ctx: commands.Context):
        await ctx.send(
            "1. 최승화\n"
            "2. 활동이름 : 승아\n"
            "3. 별명 : 숭이\n"
            "4. 163/43 60-65 f\n"
            "5. 인스타 S2ENA2 (현재 정지상태)\n"
            "6. 부 인스타 : https://www.instagram.com/s2eeena1_214?igsh=Z29uYW5seGd5d3pi&utm_source=qr\n"
            "7. X : https://x.com/insta_s2ena2\n"
            "8. 롤 : 곰바빠닥#바빠닥\n"
            "9. 현재 솔로"
        )

    @commands.command(name="낀낀")
    async def cmd_kkinkkIn(self, ctx: commands.Context):
        await ctx.send(
            "**이름 : 낀낀**\n"
            "승아가 전남자친구에게 편지를 보낼때 가계부를 찢어다 쓰는 모습을 보고 슬퍼서 "
            "쿠팡에서 헬로키티 편지지를 사준 전적이 있음"
        )

    @commands.command(name="대우겸")
    async def cmd_daewoogyeom(self, ctx: commands.Context):
        await ctx.send(
            "**이름 : 대우겸**\n"
            "남자 여자 안가리고 꼬시고 다님"
        )

    @commands.command(name="문가을")
    async def cmd_moonkaeul(self, ctx: commands.Context):
        await ctx.send(
            "**이름 : 문가을**\n"
            "세상 제일 말괄량이"
        )

    @commands.command(name="승아이상형")
    async def cmd_ideal_type(self, ctx: commands.Context):
        await ctx.send(
            "**승아 이상형**\n"
            "1. 롤을 함\n"
            "2. 근데 롤만 하는 샛기면 챌린저여야 함\n"
            "3. 칼바람 자주 하는 샛기면 좋겠음 근데 안 해도 문제는 없음\n"
            "4. 내가 랭크를 안하지만 같이 랭크를 하고싶다면 자신의 포지션이 어디든 나의 서포터가 되어줄수 있는 사람\n"
            "5. 눈코입이 달려있으면 좋겠음"
        )

    @commands.command(name="낀낀이상형")
    async def cmd_kkinkkun_ideal_type(self, ctx: commands.Context):
        await ctx.send("**낀낀 이상형**\n대 - 우 - 겸\n말잘듣고 자아없고 게임잘하는 큐티뽀짝 노예남")

    @commands.command(name="강세준")
    async def cmd_kang_sejun(self, ctx: commands.Context):
        await ctx.send("강세준을 변기에 넣고 내려")

    @commands.command(name="강예찬")
    async def cmd_kang_yechan(self, ctx: commands.Context):
        await ctx.send("강예찬을 변기에 넣고 내려")

    @commands.command(name="이하진")
    async def cmd_lee_hajin(self, ctx: commands.Context):
        await ctx.send("이하진을 변기에 넣고 내려")

    @commands.command(name="approve")
    @commands.has_permissions(manage_roles=True)
    async def cmd_approve(self, ctx: commands.Context, member: discord.Member):
        """수동 승인: !approve @유저"""
        role = ctx.guild.get_role(VERIFIED_ROLE_ID) if VERIFIED_ROLE_ID else None
        if not role:
            await ctx.send("❌ 인증 역할을 찾을 수 없습니다. VERIFIED_ROLE_ID를 확인하세요.")
            return
        await member.add_roles(role, reason=f"수동 승인 by {ctx.author}")
        await ctx.send(f"✅ {member.mention} 수동 승인 완료.")

        embed = discord.Embed(title="✅ 수동 승인", color=0x00C851)
        embed.add_field(name="대상", value=str(member))
        embed.add_field(name="승인자", value=str(ctx.author))
        await _send_log(self.bot, embed)

    @commands.command(name="reject")
    @commands.has_permissions(manage_roles=True)
    async def cmd_reject(self, ctx: commands.Context, member: discord.Member, *, reason: str = "사유 없음"):
        """수동 거절: !reject @유저 [사유]"""
        await ctx.send(f"❌ {member.mention} 인증 거절. 사유: {reason}")
        try:
            await member.send(f"인증이 거절되었습니다.\n사유: {reason}\n\n올바른 스크린샷을 다시 제출해주세요.")
        except discord.Forbidden:
            pass

        embed = discord.Embed(title="❌ 수동 거절", color=0xFF4444)
        embed.add_field(name="대상", value=str(member))
        embed.add_field(name="거절자", value=str(ctx.author))
        embed.add_field(name="사유", value=reason, inline=False)
        await _send_log(self.bot, embed)

    @commands.command(name="청소")
    @commands.has_permissions(manage_messages=True)
    async def cmd_clear(self, ctx: commands.Context, 개수: int = 1000):
        """채널 메시지 삭제: !청소 [개수] (기본값: 전체)"""
        deleted = await ctx.channel.purge(limit=개수, check=lambda m: not m.pinned)
        await ctx.send(f"🗑️ 메시지 {len(deleted)}개 삭제 완료.", delete_after=5)
        print(f"[청소] {ctx.author}가 메시지 {len(deleted)}개 삭제")

    @commands.command(name="정리")
    @commands.has_permissions(manage_messages=True)
    async def cmd_clear_keep_status(self, ctx: commands.Context, 개수: int = 1000):
        """인증완료(✅)/대기중(⏰) 메시지는 남기고 나머지만 삭제: !정리 [개수]"""
        def should_delete(m):
            if m.pinned:
                return False
            content = m.content or ""
            if content.startswith("✅") or content.startswith("⏰"):
                return False
            return True

        deleted = await ctx.channel.purge(limit=개수, check=should_delete)
        await ctx.send(f"🗑️ 메시지 {len(deleted)}개 삭제 완료 (✅/⏰ 메시지는 유지).", delete_after=5)
        print(f"[정리] {ctx.author}가 메시지 {len(deleted)}개 삭제 (✅/⏰ 제외)")

    @commands.command(name="사진지우기")
    @commands.has_permissions(manage_messages=True)
    async def cmd_clear_images(self, ctx: commands.Context):
        """현재 채널 사진 메시지만 삭제: !사진지우기"""
        channel = ctx.channel
        def has_image(m):
            if m.pinned:
                return False
            if m.attachments:
                return True
            if any(e.image or e.thumbnail or e.type == "image" for e in m.embeds):
                return True
            return False

        deleted = await channel.purge(limit=1000, check=has_image)
        await ctx.send(f"🗑️ 사진 메시지 {len(deleted)}개 삭제 완료.", delete_after=5)
        print(f"[사진지우기] {ctx.author}가 사진 {len(deleted)}개 삭제")

    @commands.command(name="동영상지우기")
    @commands.has_permissions(manage_messages=True)
    async def cmd_clear_videos(self, ctx: commands.Context):
        """현재 채널 동영상 메시지만 삭제: !동영상지우기"""
        VIDEO_EXTS = ('.mp4', '.mov', '.avi', '.webm', '.mkv', '.flv', '.wmv', '.m4v')
        def has_video(m):
            if m.pinned:
                return False
            for a in m.attachments:
                if (a.content_type or "").startswith("video/") or a.filename.lower().endswith(VIDEO_EXTS):
                    return True
            return False

        deleted = await ctx.channel.purge(limit=1000, check=has_video)
        await ctx.send(f"🗑️ 동영상 메시지 {len(deleted)}개 삭제 완료.", delete_after=5)
        print(f"[동영상지우기] {ctx.author}가 동영상 {len(deleted)}개 삭제")

    @commands.command(name="설정")
    @commands.has_permissions(manage_guild=True)
    async def cmd_settings(self, ctx: commands.Context):
        """현재 봇 설정 확인: !설정"""
        embed = discord.Embed(title="⚙️ 봇 설정", color=0x5865F2)
        embed.add_field(name="인증 채널", value=f"<#{VERIFICATION_CHANNEL_ID}>" if VERIFICATION_CHANNEL_ID else "미설정", inline=False)
        embed.add_field(name="인증 역할", value=f"<@&{VERIFIED_ROLE_ID}>" if VERIFIED_ROLE_ID else "미설정")
        embed.add_field(name="로그 채널", value=f"<#{LOG_CHANNEL_ID}>" if LOG_CHANNEL_ID else "비활성")
        embed.add_field(name="허용 오차", value=f"{TOLERANCE_MINUTES}분")
        embed.add_field(name="타임존", value=f"UTC+{TIMEZONE_OFFSET}")
        account_val = ", ".join(f"@{a}" for a in INSTAGRAM_ACCOUNTS) if INSTAGRAM_ACCOUNTS else "미설정 (전체 허용)"
        embed.add_field(name="인증 계정", value=account_val, inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="모든구독권한삭제")
    @commands.has_permissions(administrator=True)
    async def cmd_remove_all_verified(self, ctx: commands.Context):
        """구독자 역할 보유자 전원 역할 제거: !모든구독권한삭제"""
        role = ctx.guild.get_role(VERIFIED_ROLE_ID) if VERIFIED_ROLE_ID else None
        if not role:
            await ctx.send("❌ 인증 역할을 찾을 수 없습니다. VERIFIED_ROLE_ID를 확인하세요.")
            return

        targets = [m for m in ctx.guild.members if role in m.roles]
        if not targets:
            await ctx.send("ℹ️ 현재 구독자 역할을 가진 멤버가 없습니다.")
            return

        progress = await ctx.send(f"⏳ 구독자 역할 제거 중... (총 {len(targets)}명)")

        sem = asyncio.Semaphore(10)

        async def remove_one(member):
            async with sem:
                try:
                    await member.remove_roles(role, reason=f"!모든구독권한삭제 by {ctx.author}")
                    return True
                except (discord.Forbidden, discord.HTTPException):
                    return False

        results = await asyncio.gather(*[remove_one(m) for m in targets])
        failed = results.count(False)

        removed = len(targets) - failed
        await progress.edit(content=f"✅ 완료: {removed}명 역할 제거" + (f" / {failed}명 실패" if failed else ""))
        print(f"[모든구독권한삭제] {ctx.author} 실행 | 제거: {removed}명, 실패: {failed}명")

        embed = discord.Embed(title="🗑️ 구독자 역할 전체 제거", color=0xFF8800)
        embed.add_field(name="실행자", value=str(ctx.author))
        embed.add_field(name="제거 인원", value=f"{removed}명")
        if failed:
            embed.add_field(name="실패", value=f"{failed}명")
        await _send_log(self.bot, embed)

    @commands.command(name="구독자초기화")
    @commands.has_permissions(administrator=True)
    async def cmd_reset_subscribers(self, ctx: commands.Context):
        """1~2달에 한 번 수동 실행: 구독자 역할 전원 제거 + 인증 채널에 재인증 안내 게시.
        !구독자초기화"""
        role = ctx.guild.get_role(VERIFIED_ROLE_ID) if VERIFIED_ROLE_ID else None
        if not role:
            await ctx.send("❌ 인증 역할을 찾을 수 없습니다. VERIFIED_ROLE_ID를 확인하세요.")
            return

        targets = [m for m in ctx.guild.members if role in m.roles]
        if not targets:
            await ctx.send("ℹ️ 현재 구독자 역할을 가진 멤버가 없습니다.")
            return

        progress = await ctx.send(f"⏳ 구독자 역할 초기화 중... (총 {len(targets)}명)")

        sem = asyncio.Semaphore(10)

        async def remove_one(member):
            async with sem:
                try:
                    await member.remove_roles(role, reason=f"구독자 역할 초기화 by {ctx.author}")
                    return True
                except (discord.Forbidden, discord.HTTPException):
                    return False

        results = await asyncio.gather(*[remove_one(m) for m in targets])
        failed = results.count(False)
        removed = len(targets) - failed

        await progress.edit(content=f"✅ 초기화 완료: {removed}명 역할 제거" + (f" / {failed}명 실패" if failed else ""))
        print(f"[구독자초기화] {ctx.author} 실행 | 제거: {removed}명, 실패: {failed}명")

        embed = discord.Embed(title="🔄 구독자 역할 초기화", color=0xFF8800)
        embed.add_field(name="실행자", value=str(ctx.author))
        embed.add_field(name="제거 인원", value=f"{removed}명")
        if failed:
            embed.add_field(name="실패", value=f"{failed}명")
        await _send_log(self.bot, embed)

        verify_channel = self.bot.get_channel(VERIFICATION_CHANNEL_ID)
        if verify_channel:
            await verify_channel.send(RESET_ANNOUNCEMENT)

    # ------------------------------------------------------------------
    # 오류 처리
    # ------------------------------------------------------------------

    @cmd_approve.error
    @cmd_reject.error
    @cmd_remove_all_verified.error
    @cmd_reset_subscribers.error
    async def on_command_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ 권한이 부족합니다.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("❌ 해당 유저를 찾을 수 없습니다.")
        else:
            await ctx.send(f"❌ 오류: {error}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))
