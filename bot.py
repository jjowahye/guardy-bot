# -*- coding: utf-8 -*-
"""
디스코드 출퇴근봇 (4인 팀용, 구글시트 연동, 주간/월간 통계+그래프)
- 명령어:
  !출근, !퇴근, !휴식 시작, !휴식 끝
  !수정 출근 [시간], !수정 퇴근 [시간]    (예: !수정 출근 09:12  /  !수정 퇴근 2025-09-13 18:03)
  !통계 주간, !통계 월간

시트 구조(자동 생성):
  [출퇴근] 시트: 날짜 | 유저명 | 출근시간 | 퇴근시간 | 휴식시간 | 총근무시간 | 유저ID
  [휴식기록] 시트: 날짜 | 유저ID | 유저명 | 휴식시작

환경변수:
  DISCORD_TOKEN                    : 디스코드 봇 토큰
  SHEET_ID                         : 구글시트 ID
  GOOGLE_APPLICATION_CREDENTIALS   : 서비스계정 JSON 경로 (credentials.json)
  ATTENDANCE_CHANNEL_ID            : (선택) 출퇴근인증 채널 ID (정수). 없으면 "출퇴근인증" 이름 채널 사용 시에만 동작.

PowerShell 예시:
  $env:DISCORD_TOKEN="..."; $env:SHEET_ID="..."; `
  $env:GOOGLE_APPLICATION_CREDENTIALS="C:\path\to\credentials.json"; `
  $env:ATTENDANCE_CHANNEL_ID="123456789012345678"
"""
import json
import os
import io
import re
import math
import asyncio
import logging
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import matplotlib
matplotlib.use("Agg")
# 한글 폰트 안 써도 되도록 영어만 사용 + 마이너스 표시 깨짐 방지
matplotlib.rcParams["axes.unicode_minus"] = False

matplotlib.use("Agg")  # 헤드리스 환경용
import matplotlib.pyplot as plt

# -----------------------------
# 설정/상수
# -----------------------------
KST = ZoneInfo("Asia/Seoul")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_CRED = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
ATTEND_CH_ID = os.getenv("ATTENDANCE_CHANNEL_ID")  # 문자열일 수 있음

# 채널 이름 fallback
ATTEND_CH_NAME = "출퇴근인증"

# 시트 탭 이름
SHEET_TAB_RECORDS = "출퇴근"
SHEET_TAB_BREAKS = "휴식기록"

# 기록 시트 헤더
HEADERS = ["날짜", "유저명", "출근시간", "퇴근시간", "휴식시간", "총근무시간", "유저ID"]
# 휴식 시트 헤더
BREAK_HEADERS = ["날짜", "유저ID", "유저명", "휴식시작"]

# 로깅
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("attendance-bot")


# -----------------------------
# 유틸: 시간 파싱/포맷
# -----------------------------
def now_kst_dt() -> datetime:
    return datetime.now(tz=KST)

def today_str() -> str:
    return now_kst_dt().strftime("%Y-%m-%d")

def hhmm_str(dt: datetime) -> str:
    return dt.strftime("%H:%M")

def minutes_to_hhmm(total_min: int) -> str:
    total_min = max(0, int(total_min))
    h = total_min // 60
    m = total_min % 60
    return f"{h:02d}:{m:02d}"

def hhmm_to_minutes(hhmm: str) -> int:
    if not hhmm:
        return 0
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", hhmm)
    if not m:
        return 0
    h = int(m.group(1))
    mi = int(m.group(2))
    return h * 60 + mi

def parse_time_input(s: str, default_date: date) -> datetime | None:
    """
    허용 형식:
      - "HH:MM" , "H:MM", "HHMM"
      - "YYYY-MM-DD HH:MM"
      - "YYYY/MM/DD HH:MM"
    반환: KST timezone-aware datetime
    """
    s = s.strip()
    # YYYY-MM-DD HH:MM 또는 YYYY/MM/DD HH:MM
    m = re.match(r"^\s*(\d{4})[-/](\d{2})[-/](\d{2})\s+(\d{1,2}):(\d{2})\s*$", s)
    if m:
        y, mo, d, hh, mm = map(int, m.groups())
        try:
            return datetime(y, mo, d, hh, mm, tzinfo=KST)
        except ValueError:
            return None

    # HH:MM
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", s)
    if m:
        hh, mm = map(int, m.groups())
        try:
            return datetime(default_date.year, default_date.month, default_date.day, hh, mm, tzinfo=KST)
        except ValueError:
            return None

    # HHMM (예: 0912)
    m = re.match(r"^\s*(\d{3,4})\s*$", s)
    if m:
        digits = m.group(1)
        if len(digits) == 3:  # e.g., 912 -> 09:12로 간주하지 말고 에러 처리
            return None
        hh = int(digits[:2])
        mm = int(digits[2:])
        try:
            return datetime(default_date.year, default_date.month, default_date.day, hh, mm, tzinfo=KST)
        except ValueError:
            return None

    return None


# -----------------------------
# Google Sheets 클라이언트
# -----------------------------
class SheetClient:
    def _find_open_row_by_date_userid(self, date_str: str, user_id: int) -> int | None:
        uid_col = self.colmap["유저ID"]
        date_col = self.colmap["날짜"]
        start_col = self.colmap["출근시간"]
        end_col = self.colmap["퇴근시간"]
        all_vals = self.ws_records.get_all_values()
        for idx, row in enumerate(all_vals[1:], start=2):
            if len(row) >= max(uid_col, date_col, start_col, end_col):
                if row[date_col - 1].strip() == date_str and row[uid_col - 1].strip() == str(user_id):
                    start_v = row[start_col - 1].strip()
                    end_v = row[end_col - 1].strip()
                    if start_v and not end_v:
                        return idx    # 출근은 했고, 퇴근은 안 한 "열린 시프트"
        return None

    def __init__(self, sheet_id: str):
        self.sheet_id = sheet_id
        self.gc = self._auth()
        self.sh = self.gc.open_by_key(sheet_id)
        self.ws_records = self._ensure_worksheet(self.sh, SHEET_TAB_RECORDS, HEADERS)
        self.ws_breaks = self._ensure_worksheet(self.sh, SHEET_TAB_BREAKS, BREAK_HEADERS)
        self.colmap = self._map_headers(self.ws_records, HEADERS)

    def _auth(self):
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]

        # ✅ 1) Railway 권장: 환경변수 GOOGLE_CREDENTIALS_JSON 에서 바로 읽기
        json_env = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if json_env:
            try:
                creds_dict = json.loads(json_env)  # 전체 JSON 문자열 파싱
            except Exception as e:
                raise RuntimeError("GOOGLE_CREDENTIALS_JSON 파싱 실패: JSON 형식을 확인하세요.") from e
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            return gspread.authorize(creds)

        # ✅ 2) 로컬 개발용: 파일 경로(기존 방식)로 폴백
        if GOOGLE_CRED and os.path.exists(GOOGLE_CRED):
            creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CRED, scope)
            return gspread.authorize(creds)

        # ❌ 둘 다 없으면 에러
        raise RuntimeError(
            "서비스 계정 자격 증명을 찾을 수 없습니다. "
            "Railway에서는 GOOGLE_CREDENTIALS_JSON 환경변수에 키 내용을 통째로 넣어주세요. "
            "로컬에서는 GOOGLE_APPLICATION_CREDENTIALS 경로가 유효한지 확인하세요."
        )


    def _ensure_worksheet(self, sh, title, headers):
        try:
            ws = sh.worksheet(title)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=title, rows=1000, cols=len(headers))
            ws.update("A1", [headers])
        # 헤더 보정
        existing = ws.row_values(1)
        if existing != headers:
            # 헤더 길이가 다르면 맞춰줌(덮어씌움)
            ws.resize(rows=ws.row_count, cols=len(headers))
            ws.update("A1", [headers])
        return ws

    @staticmethod
    def _map_headers(ws, headers):
        header_row = ws.row_values(1)
        mapping = {}
        for i, h in enumerate(header_row, start=1):
            mapping[h] = i
        # 누락 헤더는 추가 생성
        updated = False
        for h in headers:
            if h not in mapping:
                header_row.append(h)
                mapping[h] = len(header_row)
                updated = True
        if updated:
            ws.resize(rows=ws.row_count, cols=len(header_row))
            ws.update("A1", [header_row])
        return mapping

    # -------- 기본 CRUD --------
    def _find_row_by_date_userid(self, date_str: str, user_id: int) -> int | None:
        """날짜+유저ID로 행 번호 찾기 (헤더는 1행, 데이터는 2행부터)"""
        uid_col = self.colmap["유저ID"]
        date_col = self.colmap["날짜"]
        all_vals = self.ws_records.get_all_values()
        for idx, row in enumerate(all_vals[1:], start=2):
            if len(row) >= max(uid_col, date_col):
                r_date = row[date_col - 1].strip()
                r_uid = row[uid_col - 1].strip()
                if r_date == date_str and r_uid == str(user_id):
                    return idx
        return None

    def _append_row(self, vals: list[str]):
        self.ws_records.append_row(vals, value_input_option="USER_ENTERED")

    def get_or_create_row(self, dt: date, user_id: int, user_display: str) -> int:
        dstr = dt.strftime("%Y-%m-%d")
        # 1) 먼저 '열린 시프트'가 있으면 그걸 사용
        row = self._find_open_row_by_date_userid(dstr, user_id)
        if row:
            return row

        # 2) 없으면 '새 시프트' 행을 추가
        new_row = [dstr, user_display, "", "", "00:00", "", str(user_id)]
        self._append_row(new_row)

        # 방금 추가된 "해당 날짜+유저"의 마지막 행 번호를 찾아 반환
        all_vals = self.ws_records.get_all_values()
        last_idx = None
        uid_col = self.colmap["유저ID"]
        date_col = self.colmap["날짜"]
        for idx, row_vals in enumerate(all_vals[1:], start=2):
            if len(row_vals) >= max(uid_col, date_col):
                if row_vals[date_col - 1].strip() == dstr and row_vals[uid_col - 1].strip() == str(user_id):
                    last_idx = idx
        return last_idx


    def update_cell(self, row: int, header: str, value: str):
        col = self.colmap[header]
        self.ws_records.update_cell(row, col, value)

    def read_row(self, row: int) -> dict:
        # 행 전체 읽어 dict 반환
        vals = self.ws_records.row_values(row)
        # 패딩
        while len(vals) < len(self.colmap):
            vals.append("")
        inv = {v: k for k, v in self.colmap.items()}
        return {inv[i]: vals[i - 1] for i in self.colmap.values()}

    def calc_and_update_total(self, row: int):
        data = self.read_row(row)
        start_s = data.get("출근시간", "").strip()
        end_s = data.get("퇴근시간", "").strip()
        rest_s = data.get("휴식시간", "00:00").strip() or "00:00"
        if not start_s or not end_s:
            self.update_cell(row, "총근무시간", "")
            return
        # 같은 날짜 기준으로 시간 계산
        d = datetime.strptime(data["날짜"], "%Y-%m-%d").date()
        try:
            sh, sm = map(int, start_s.split(":"))
            eh, em = map(int, end_s.split(":"))
        except:
            self.update_cell(row, "총근무시간", "")
            return
        start_dt = datetime(d.year, d.month, d.day, sh, sm, tzinfo=KST)
        end_dt = datetime(d.year, d.month, d.day, eh, em, tzinfo=KST)
        if end_dt < start_dt:
            # 야간근무(자정 넘김) 지원
            end_dt += timedelta(days=1)
        work_min = int((end_dt - start_dt).total_seconds() // 60)
        rest_min = hhmm_to_minutes(rest_s)
        total_min = max(0, work_min - rest_min)
        self.update_cell(row, "총근무시간", minutes_to_hhmm(total_min))

    # -------- 휴식 관리 --------
    def start_break(self, d: date, user_id: int, user_display: str, start_dt: datetime):
        dstr = d.strftime("%Y-%m-%d")
        # 이미 휴식중인지 확인
        all_vals = self.ws_breaks.get_all_values()
        if len(all_vals) > 1:
            # 날짜 | 유저ID | 유저명 | 휴식시작
            for idx, row in enumerate(all_vals[1:], start=2):
                if len(row) >= 2 and row[0].strip() == dstr and row[1].strip() == str(user_id):
                    raise RuntimeError("이미 휴식 중입니다. (!휴식 끝 으로 종료하세요)")
        # 새 휴식 시작 추가
        self.ws_breaks.append_row(
            [dstr, str(user_id), user_display, hhmm_str(start_dt)],
            value_input_option="USER_ENTERED"
        )

    def end_break(self, d: date, user_id: int) -> int:
        """휴식 종료 -> 경과 분 반환, 기록 시트 '휴식시간' 누적"""
        dstr = d.strftime("%Y-%m-%d")
        all_vals = self.ws_breaks.get_all_values()
        target_row = None
        start_s = None
        if len(all_vals) > 1:
            for idx, row in enumerate(all_vals[1:], start=2):
                if len(row) >= 4 and row[0].strip() == dstr and row[1].strip() == str(user_id):
                    target_row = idx
                    start_s = row[3].strip()
                    break
        if not target_row or not start_s:
            raise RuntimeError("진행 중인 휴식이 없습니다.")

        # 경과 시간 계산
        sh, sm = map(int, start_s.split(":"))
        start_dt = datetime(d.year, d.month, d.day, sh, sm, tzinfo=KST)
        end_dt = now_kst_dt()
        if end_dt < start_dt:
            end_dt += timedelta(days=1)
        diff_min = int((end_dt - start_dt).total_seconds() // 60)

        # 누적 휴식시간 반영 대상 = 오늘의 '열린 시프트'
        rec_row = self._find_open_row_by_date_userid(dstr, user_id)
        if not rec_row:
            raise RuntimeError("출근 중인 시프트가 없어 휴식을 종료할 수 없습니다.")
        row_data = self.read_row(rec_row)

        current_rest = row_data.get("휴식시간", "00:00").strip() or "00:00"
        new_rest_min = hhmm_to_minutes(current_rest) + diff_min
        self.update_cell(rec_row, "휴식시간", minutes_to_hhmm(new_rest_min))
        # 총근무시간 재계산
        self.calc_and_update_total(rec_row)

        # 휴식기록에서 제거
        self.ws_breaks.delete_rows(target_row)
        return diff_min

    # -------- 질의/통계 --------
    def query_user_rows(self, user_id: int, start_date: date, end_date: date):
        """user_id로 기간 내 [출퇴근] 행 조회 -> list[dict]"""
        all_vals = self.ws_records.get_all_values()
        if len(all_vals) <= 1:
            return []
        header = all_vals[0]
        # 컬럼 인덱스
        idx_map = {h: i for i, h in enumerate(header)}
        res = []
        for row in all_vals[1:]:
            if len(row) < len(header):
                row += [""] * (len(header) - len(row))
            try:
                r_date = datetime.strptime(row[idx_map["날짜"]].strip(), "%Y-%m-%d").date()
            except:
                continue
            if not (start_date <= r_date <= end_date):
                continue
            if row[idx_map["유저ID"]].strip() != str(user_id):
                continue
            data = {h: row[idx_map[h]].strip() if idx_map.get(h) is not None else "" for h in HEADERS}
            res.append(data)
        return res

    @staticmethod
    def _compute_daily_minutes(item: dict) -> int:
        """행 dict -> 일일 총 근무 분(출근/퇴근/휴식 기반). 비정상이면 0."""
        ds = item.get("날짜", "")
        ss = item.get("출근시간", "")
        es = item.get("퇴근시간", "")
        rs = item.get("휴식시간", "00:00") or "00:00"
        if not ds or not ss or not es:
            return 0
        try:
            d = datetime.strptime(ds, "%Y-%m-%d").date()
            sh, sm = map(int, ss.split(":"))
            eh, em = map(int, es.split(":"))
        except:
            return 0
        sdt = datetime(d.year, d.month, d.day, sh, sm, tzinfo=KST)
        edt = datetime(d.year, d.month, d.day, eh, em, tzinfo=KST)
        if edt < sdt:
            edt += timedelta(days=1)
        work = int((edt - sdt).total_seconds() // 60)
        rest = hhmm_to_minutes(rs)
        return max(0, work - rest)

    def aggregate_minutes_by_date(self, user_id: int, start_date: date, end_date: date):
        """기간 일자별 합계(분) 딕셔너리 반환 {date: minutes}"""
        rows = self.query_user_rows(user_id, start_date, end_date)
        out = {}
        for item in rows:
            ds = item["날짜"]
            try:
                d = datetime.strptime(ds, "%Y-%m-%d").date()
            except:
                continue
            out[d] = out.get(d, 0) + self._compute_daily_minutes(item)
        return out


# -----------------------------
# 그래프 생성
# -----------------------------
def make_bar_chart(data_map: dict[date, int], title: str) -> bytes:
    import matplotlib
    matplotlib.rcParams["axes.unicode_minus"] = False

    if not data_map or max(data_map.values(), default=0) <= 0:
        fig = plt.figure(figsize=(6, 3))
        plt.title(title)
        plt.text(0.5, 0.5, "No data", ha="center", va="center")
        plt.axis("off")
        buf = io.BytesIO()
        plt.tight_layout()
        fig.savefig(buf, format="png", dpi=160)
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    xs = sorted(data_map.keys())
    ys_min = [max(0, data_map[d]) for d in xs]
    ys_hr = [m / 60.0 for m in ys_min]

    fig = plt.figure(figsize=(8, 4))
    labels = [d.strftime("%m-%d") for d in xs]
    plt.bar(labels, ys_hr)
    plt.xlabel("Date")
    plt.ylabel("Hours Worked")
    plt.title(title)
    plt.ylim(bottom=0)
    plt.xticks(rotation=45)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=160)
    plt.close(fig)
    buf.seek(0)
    return buf.read()




# -----------------------------
# Discord Bot
# -----------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

SHEET: SheetClient | None = None


async def ensure_attendance_channel(ctx) -> bool:
    """출퇴근인증 채널 제한."""
    guild = ctx.guild
    if guild is None:
        await ctx.reply("서버 내에서만 사용 가능합니다.")
        return False

    # ID 우선
    if ATTEND_CH_ID:
        try:
            ch_id = int(ATTEND_CH_ID)
            if ctx.channel.id != ch_id:
                await ctx.reply("이 명령어는 지정된 출퇴근인증 채널에서만 사용 가능합니다.")
                return False
            return True
        except ValueError:
            pass

    # 이름 fallback
    if ctx.channel.name != ATTEND_CH_NAME:
        await ctx.reply(f"이 명령어는 **#{ATTEND_CH_NAME}** 채널에서만 사용 가능합니다.")
        return False

    return True


@bot.event
async def on_ready():
    global SHEET
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    # Google Sheets 초기화
    try:
        SHEET = SheetClient(SHEET_ID)
        logger.info("Google Sheets 연결 성공")
    except Exception as e:
        logger.exception("Google Sheets 초기화 실패: %s", e)


def user_display_name(member: discord.Member | discord.User) -> str:
    # 닉네임 우선, 없으면 username
    if isinstance(member, discord.Member):
        return member.display_name
    return member.name


# ------------- 명령어 -------------
@bot.command(name="출근")
async def cmd_checkin(ctx: commands.Context):
    if not await ensure_attendance_channel(ctx):
        return
    assert SHEET is not None

    user = ctx.author
    disp = user_display_name(user)
    d = now_kst_dt().date()
    row = SHEET.get_or_create_row(d, user.id, disp)

    data = SHEET.read_row(row)
    if data.get("출근시간"):
        await ctx.reply(f"{disp}님은 이미 출근 기록이 있습니다. (출근 {data['출근시간']})\n필요 시 `!수정 출근 HH:MM` 을 사용하세요.")
        return

    t_now = now_kst_dt()
    SHEET.update_cell(row, "유저명", disp)  # 닉네임 갱신
    SHEET.update_cell(row, "출근시간", hhmm_str(t_now))
    SHEET.calc_and_update_total(row)

    await ctx.send(f"**{disp}님 출근 완료!** ({hhmm_str(t_now)})")


@bot.command(name="퇴근")
async def cmd_checkout(ctx: commands.Context):
    if not await ensure_attendance_channel(ctx):
        return
    assert SHEET is not None

    user = ctx.author
    disp = user_display_name(user)
    today = now_kst_dt().date()
    dstr = today.strftime("%Y-%m-%d")

    # ✅ 핵심: 오늘 날짜에 '열린 시프트'(출근 O, 퇴근 X) 행을 찾는다.
    open_row = SHEET._find_open_row_by_date_userid(dstr, user.id)
    if not open_row:
        await ctx.reply("출근 중인 시프트가 없습니다. `!출근`으로 새 시프트를 시작하세요.")
        return

    # 휴식이 진행 중이면 자동 종료(같은 날 기준)
    ended_break = False
    try:
        SHEET.end_break(today, user.id)
        ended_break = True
    except Exception:
        pass

    t_now = now_kst_dt()
    SHEET.update_cell(open_row, "유저명", disp)                 # 닉네임 최신화
    SHEET.update_cell(open_row, "퇴근시간", hhmm_str(t_now))     # 퇴근 기록
    SHEET.calc_and_update_total(open_row)                       # 총근무시간 재계산

    msg = f"**{disp}님 퇴근 완료!** ({hhmm_str(t_now)})"
    if ended_break:
        msg += " (진행 중이던 휴식 자동 종료)"
    await ctx.send(msg)


@bot.command(name="휴식")
async def cmd_break(ctx: commands.Context, subcmd: str | None = None):
    if not await ensure_attendance_channel(ctx):
        return
    assert SHEET is not None

    if subcmd not in ["시작", "끝"]:
        await ctx.reply("사용법: `!휴식 시작` 또는 `!휴식 끝`")
        return

    user = ctx.author
    disp = user_display_name(user)
    today = now_kst_dt().date()
    dstr = today.strftime("%Y-%m-%d")

    # ✅ 오늘 날짜의 '열린 시프트'(출근 O, 퇴근 X)만 대상으로 함
    open_row = SHEET._find_open_row_by_date_userid(dstr, user.id)
    if not open_row:
        await ctx.reply("출근 기록이 없습니다. 먼저 `!출근` 후 휴식을 사용하세요.")
        return

    if subcmd == "시작":
        try:
            SHEET.start_break(today, user.id, disp, now_kst_dt())
            await ctx.send(f"{disp}님 휴식 시작! ({hhmm_str(now_kst_dt())})")
        except Exception as e:
            await ctx.reply(str(e))
        return

    if subcmd == "끝":
        try:
            diff_min = SHEET.end_break(today, user.id)  # (c)에서 end_break도 수정됨
            await ctx.send(f"{disp}님 휴식 종료! (+{diff_min}분)")
        except Exception as e:
            await ctx.reply(str(e))
        return



@bot.group(name="수정", invoke_without_command=True)
async def cmd_edit(ctx: commands.Context):
    await ctx.reply("사용법: `!수정 출근 HH:MM` 또는 `!수정 퇴근 HH:MM` (날짜 포함 가능: `YYYY-MM-DD HH:MM`)")

@cmd_edit.command(name="출근")
async def edit_start(ctx: commands.Context, *, time_str: str):
    if not await ensure_attendance_channel(ctx):
        return
    assert SHEET is not None

    user = ctx.author
    disp = user_display_name(user)
    nowd = now_kst_dt().date()
    dt = parse_time_input(time_str, nowd)
    if not dt:
        await ctx.reply("시간 형식 오류. 예) `09:12` 또는 `2025-09-13 09:12`")
        return

    row = SHEET.get_or_create_row(dt.date(), user.id, disp)
    SHEET.update_cell(row, "유저명", disp)
    SHEET.update_cell(row, "출근시간", hhmm_str(dt))
    SHEET.calc_and_update_total(row)
    await ctx.send(f"{disp}님의 출근 시간이 `{hhmm_str(dt)}`로 수정되었습니다. (날짜 {dt.date().isoformat()})")

@cmd_edit.command(name="퇴근")
async def edit_end(ctx: commands.Context, *, time_str: str):
    if not await ensure_attendance_channel(ctx):
        return
    assert SHEET is not None

    user = ctx.author
    disp = user_display_name(user)
    nowd = now_kst_dt().date()
    dt = parse_time_input(time_str, nowd)
    if not dt:
        await ctx.reply("시간 형식 오류. 예) `18:03` 또는 `2025-09-13 18:03`")
        return

    # 휴식 종료 자동 처리(동일 날짜에 한함)
    if dt.date() == nowd:
        try:
            SHEET.end_break(nowd, user.id)
        except Exception:
            pass

    row = SHEET.get_or_create_row(dt.date(), user.id, disp)
    SHEET.update_cell(row, "유저명", disp)
    SHEET.update_cell(row, "퇴근시간", hhmm_str(dt))
    SHEET.calc_and_update_total(row)
    await ctx.send(f"{disp}님의 퇴근 시간이 `{hhmm_str(dt)}`로 수정되었습니다. (날짜 {dt.date().isoformat()})")


# -------- 통계 --------
@bot.group(name="통계", invoke_without_command=True)
async def cmd_stats(ctx: commands.Context):
    await ctx.reply("사용법: `!통계 주간` 또는 `!통계 월간`")

def week_range_kst(today: date) -> tuple[date, date]:
    # 월요일 시작 ~ 일요일 끝
    weekday = today.weekday()  # 월=0
    start = today - timedelta(days=weekday)
    end = start + timedelta(days=6)
    return start, end

def month_range(today: date) -> tuple[date, date]:
    start = today.replace(day=1)
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1, day=1)
    else:
        next_month = start.replace(month=start.month + 1, day=1)
    end = next_month - timedelta(days=1)
    return start, end

@cmd_stats.command(name="주간")
async def stats_week(ctx: commands.Context):
    assert SHEET is not None
    if not await ensure_attendance_channel(ctx):
        return

    user = ctx.author
    today = now_kst_dt().date()
    s, e = week_range_kst(today)
    data_map = SHEET.aggregate_minutes_by_date(user.id, s, e)

    # 영어 타이틀 (그래프 네모깨짐 방지)
    title = f"Weekly Hours Worked ({s.strftime('%m/%d')}–{e.strftime('%m/%d')})"
    png = make_bar_chart(data_map, title)

    total_min = sum(data_map.values())
    total_hhmm = minutes_to_hhmm(total_min)

    file = discord.File(io.BytesIO(png), filename="weekly.png")
    await ctx.reply(content=f"**Weekly total:** {total_hhmm}", file=file)


@cmd_stats.command(name="월간")
async def stats_month(ctx: commands.Context):
    assert SHEET is not None
    if not await ensure_attendance_channel(ctx):
        return

    user = ctx.author
    today = now_kst_dt().date()
    s, e = month_range(today)
    data_map = SHEET.aggregate_minutes_by_date(user.id, s, e)

    title = f"Monthly Hours Worked ({today.strftime('%Y-%m')})"
    png = make_bar_chart(data_map, title)

    total_min = sum(data_map.values())
    total_hhmm = minutes_to_hhmm(total_min)

    file = discord.File(io.BytesIO(png), filename="monthly.png")
    await ctx.reply(content=f"**Monthly total:** {total_hhmm}", file=file)


# -------- 도움말(옵션) --------
@bot.command(name="도움")
async def cmd_help(ctx: commands.Context):
    txt = (
        "**출퇴근 봇 명령어**\n"
        "`!출근` / `!퇴근`\n"
        "`!휴식 시작` / `!휴식 끝`\n"
        "`!수정 출근 HH:MM` / `!수정 퇴근 HH:MM`\n"
        "`!통계 주간` / `!통계 월간`\n"
        f"※ 명령어는 **#{ATTEND_CH_NAME}** 채널(또는 지정된 채널ID)에서만 사용 가능"
    )
    await ctx.reply(txt)

@bot.command(name="디버그시트")
async def debug_sheet(ctx):
    assert SHEET is not None
    await ctx.send(f"SHEET_ID in env: `{SHEET_ID}`")
    try:
        # 2행 유저명 칸에 PING 써보기 (없으면 자동 생성됨)
        SHEET.update_cell(2, "유저명", "PING")
        await ctx.send("Write test: OK")
    except Exception as e:
        await ctx.send(f"Write test: FAILED -> {e}")

# -----------------------------
# 실행
# -----------------------------
def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("환경변수 DISCORD_TOKEN이 설정되지 않았습니다.")
    if not SHEET_ID:
        raise RuntimeError("환경변수 SHEET_ID가 설정되지 않았습니다.")

    # ✅ 자격 증명은 둘 중 하나만 있으면 충분:
    # 1) GOOGLE_CREDENTIALS_JSON (Railway에서 권장)
    # 2) GOOGLE_APPLICATION_CREDENTIALS (로컬 파일 경로)
    has_env_json = bool(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
    has_file = bool(GOOGLE_CRED and os.path.exists(GOOGLE_CRED))

    if not (has_env_json or has_file):
        raise RuntimeError(
            "서비스 계정 자격 증명을 찾을 수 없습니다. "
            "Railway에서는 GOOGLE_CREDENTIALS_JSON 변수에 키 JSON 전체를 붙여넣으세요. "
            "또는 로컬에서는 GOOGLE_APPLICATION_CREDENTIALS에 파일 경로를 지정하세요."
        )

    # 실행
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
