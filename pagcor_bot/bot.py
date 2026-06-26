import os
import re
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, ConversationHandler
)
from google.oauth2 import service_account
from googleapiclient.discovery import build
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── CONFIG ────────────────────────────────────────────────────────────────────
ALLOWED_USER_ID     = int(os.environ["ALLOWED_USER_ID"])
TELEGRAM_TOKEN      = os.environ["TELEGRAM_TOKEN"]
SPREADSHEET_ID      = "1Yv3xRFyqR2ykQPSpH30zpHSGn0_r8FozFiSNXaVanpU"
OUTPUT_FOLDER_ID    = "1crTziUo1EHvObK-msx63LNMYGOosxd00"
INSTRUCTION_SLOT_FOLDER   = "1r5K5KHpD-i6gEkQ9Hc3tnL33KNDWHt28"
CERT_JILI_FOLDER    = "1TMJiM0fBTedI4C55f7phObwQ-8jqpWBm"

# Conversation states
CONFIRM_GAMES, RESOLVE_AMBIGUOUS = range(2)

# ─── GOOGLE AUTH ────────────────────────────────────────────────────────────────
def get_google_services():
    sa_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive",
        ]
    )
    sheets = build("sheets", "v4", credentials=creds)
    drive  = build("drive",  "v3", credentials=creds)
    return sheets, drive

# ─── SHEETS HELPERS ─────────────────────────────────────────────────────────────
def fetch_game_list(sheets) -> list[dict]:
    """ดึงข้อมูลเกมทั้งหมดจาก ดึงข้อมูล-查詢表"""
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="ดึงข้อมูล-查詢表!A2:AB1000"
    ).execute()
    rows = result.get("values", [])

    games = []
    headers = ["GameID","Name","GameID2","GAME_VERSION","GAME_OFFERING","Game_Type",
               "Min_Bet","Max_Bet","Max_Odds","Support","GamePlay","Hit_Rate",
               "Free_Game_Rate","Default_Bet","Max_Exposure","Paid_Spins","RTP",
               "SD_General1","SD_General2","CI_90","CI_95","CI_99",
               "CI_90_Min","CI_90_Max","CI_95_Min","CI_95_Max","CI_99_Min","CI_99_Max"]
    for row in rows:
        if not row or not row[0]:
            continue
        padded = row + [""] * (len(headers) - len(row))
        game = dict(zip(headers, padded))
        if game["GameID"] and game["Name"]:
            games.append(game)
    return games

def fetch_pagcor_approved(sheets) -> dict:
    """ดึง PAGCOR Approved Product List → {game_name_upper: pagcor_id}"""
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="PAGCOR Approved Product List!A2:E500"
    ).execute()
    rows = result.get("values", [])
    lookup = {}
    for row in rows:
        if len(row) >= 3 and row[2]:
            name = row[2].strip().upper()
            pagcor_id = row[3].strip() if len(row) >= 4 else ""
            lookup[name] = pagcor_id
    return lookup

# ─── DRIVE HELPERS ──────────────────────────────────────────────────────────────
def list_drive_folder(drive, folder_id: str) -> list[dict]:
    """List ไฟล์และโฟลเดอร์ใน folder"""
    files = []
    page_token = None
    while True:
        resp = drive.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id,name,mimeType,webViewLink),nextPageToken",
            pageSize=500,
            pageToken=page_token
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files

def find_instruction_file(drive, game_name: str, game_id: str) -> str | None:
    """ค้นหาไฟล์ instruction ใน Slot folder (และ subfolder อื่นๆ)"""
    name_upper = game_name.strip().upper()
    id_str = str(game_id).strip()

    # ค้นหาใน Slot folder ก่อน
    slot_files = list_drive_folder(drive, INSTRUCTION_SLOT_FOLDER)
    for f in slot_files:
        title = f["name"].upper()
        if name_upper in title or id_str in title:
            return f["webViewLink"]

    # ค้นหาใน subfolder อื่นๆ ของ parent folder
    other_folders = [
        "1UUb2uXq1EJPq-HWa8MPZed6TxJxbz1lX",  # Table/Card
        "1Kgy7Jqi5akFVTFsSvhU7w9J3oIEaHimW",  # Fast Game/Casino
        "1XVNMHilWKrNidX0sjP8rr0t_e6bl0_S9",  # Fishing
        "1bXgsQDqDcqLAylVbhEEzY05QX7mnXzGQ",  # Bingo
    ]
    for fid in other_folders:
        files = list_drive_folder(drive, fid)
        for f in files:
            title = f["name"].upper()
            if name_upper in title or id_str in title:
                return f["webViewLink"]
    return None

def find_cert_folder(drive, game_name: str, game_id: str) -> str | None:
    """ค้นหาโฟลเดอร์ certificate ใน JILI Games Certification"""
    name_upper = game_name.strip().upper()
    id_str = str(game_id).strip()
    cert_files = list_drive_folder(drive, CERT_JILI_FOLDER)
    for f in cert_files:
        title = f["name"].upper()
        if name_upper in title or id_str in title:
            return f["webViewLink"]
    return None

# ─── PAGCOR APPROVAL LOGIC ──────────────────────────────────────────────────────
def check_pagcor_approval(game: dict, approved_lookup: dict) -> str:
    name_upper = game["Name"].strip().upper()
    game_id    = str(game["GameID"]).strip()

    if name_upper in approved_lookup:
        pagcor_id_ver = approved_lookup[name_upper]
        pagcor_num = pagcor_id_ver.split("-")[0].strip()
        if pagcor_num == game_id:
            return f"Approved by PAGCOR — Game ID & Name matched (PAGCOR ID: {pagcor_id_ver})"
        else:
            return f"Approved by PAGCOR — Name matched, but Game ID differs (JILI ID: {game_id} / PAGCOR ID: {pagcor_id_ver})"
    return "Not yet approved by PAGCOR"

# ─── BUILD GOOGLE SHEETS OUTPUT ─────────────────────────────────────────────────
def build_output_sheet(drive, sheets_svc, games_data: list[dict],
                       client_name: str, approved_lookup: dict) -> str:
    """สร้าง Google Sheets ใหม่ แล้ว return link"""
    today     = datetime.now().strftime("%Y%m%d")
    count     = len(games_data)
    file_name = f"{today}_PAGCOR_{client_name}_{count}games"

    # สร้าง Google Sheets เปล่า
    sheet_body = {"properties": {"title": file_name}}
    created = sheets_svc.spreadsheets().create(body=sheet_body).execute()
    sheet_id     = created["spreadsheetId"]
    sheet_url    = created["spreadsheetUrl"]

    # ย้ายไปไว้ใน output folder
    file_meta = drive.files().get(fileId=sheet_id, fields="parents").execute()
    prev_parents = ",".join(file_meta.get("parents", []))
    drive.files().update(
        fileId=sheet_id,
        addParents=OUTPUT_FOLDER_ID,
        removeParents=prev_parents,
        fields="id,parents"
    ).execute()

    # Headers
    title_row = ["JILI Games: PAGCOR Game Parameter and RTP Details"] + [""] * 26
    header_row = [
        "GameID", "Name", "GAME VERSION", "GAME OFFERING", "Game Type",
        "Min Bet (₱)", "Max Bet (₱)", "Max Odds", "Support", "GamePlay",
        "Default Bet", "Max Exposure", "RTP",
        "standard deviation (General)", "standard deviation (General)",
        "90.0% Confidence Range", "95.0% Confidence Range", "99.0% Confidence Range",
        "90.0% Confidence Min", "90.0% Confidence Max",
        "95.0% Confidence Min", "95.0% Confidence Max",
        "99.0% Confidence Min", "99.0% Confidence Max",
        "PAGCOR Approval Status",
        "Game Instruction File",
        "Game Certificate File",
    ]

    # Data rows
    data_rows = []
    for g in games_data:
        approval = check_pagcor_approval(g, approved_lookup)

        instr = find_instruction_file(drive, g["Name"], g["GameID"])
        instr_val = f"🔗 {instr}" if instr else "Please contact JILI BD"

        cert = find_cert_folder(drive, g["Name"], g["GameID"])
        cert_val = f"🔗 {cert}" if cert else "This game has not yet been scheduled for lab certification"

        # Min/Max Bet ใส่ ₱
        min_bet = f"₱{g['Min_Bet']}" if g["Min_Bet"] else ""
        max_bet = f"₱{g['Max_Bet']}" if g["Max_Bet"] else ""
        def_bet = f"₱{g['Default_Bet']}" if g["Default_Bet"] else ""
        max_exp = f"₱{g['Max_Exposure']}" if g["Max_Exposure"] else ""

        row = [
            g["GameID"], g["Name"], g["GAME_VERSION"], g["GAME_OFFERING"], g["Game_Type"],
            min_bet, max_bet, g["Max_Odds"], g["Support"], g["GamePlay"],
            def_bet, max_exp, g["RTP"],
            g["SD_General1"], g["SD_General2"],
            g["CI_90"], g["CI_95"], g["CI_99"],
            g["CI_90_Min"], g["CI_90_Max"],
            g["CI_95_Min"], g["CI_95_Max"],
            g["CI_99_Min"], g["CI_99_Max"],
            approval, instr_val, cert_val,
        ]
        data_rows.append(row)

    all_rows = [title_row, header_row] + data_rows

    # Write data
    sheets_svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range="Sheet1!A1",
        valueInputOption="RAW",
        body={"values": all_rows}
    ).execute()

    # Formatting
    requests = [
        # Merge title row
        {"mergeCells": {
            "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 27},
            "mergeType": "MERGE_ALL"
        }},
        # Title style
        {"repeatCell": {
            "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 27},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.051, "green": 0.231, "blue": 0.431},
                "textFormat": {"bold": True, "fontSize": 11,
                               "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "verticalAlignment": "MIDDLE",
                "padding": {"left": 8}
            }},
            "fields": "userEnteredFormat"
        }},
        # Header row style
        {"repeatCell": {
            "range": {"sheetId": 0, "startRowIndex": 1, "endRowIndex": 2,
                      "startColumnIndex": 0, "endColumnIndex": 27},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.122, "green": 0.306, "blue": 0.475},
                "textFormat": {"bold": True, "fontSize": 9,
                               "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP"
            }},
            "fields": "userEnteredFormat"
        }},
        # Freeze rows 1+2
        {"updateSheetProperties": {
            "properties": {"sheetId": 0, "gridProperties": {"frozenRowCount": 2}},
            "fields": "gridProperties.frozenRowCount"
        }},
        # Row 1 height
        {"updateDimensionProperties": {
            "range": {"sheetId": 0, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 32}, "fields": "pixelSize"
        }},
        # Row 2 height
        {"updateDimensionProperties": {
            "range": {"sheetId": 0, "dimension": "ROWS", "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 45}, "fields": "pixelSize"
        }},
        # Col B (Name) width
        {"updateDimensionProperties": {
            "range": {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 160}, "fields": "pixelSize"
        }},
        # Col Y (Approval) width
        {"updateDimensionProperties": {
            "range": {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 24, "endIndex": 25},
            "properties": {"pixelSize": 340}, "fields": "pixelSize"
        }},
        # Col Z, AA width
        {"updateDimensionProperties": {
            "range": {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 25, "endIndex": 27},
            "properties": {"pixelSize": 380}, "fields": "pixelSize"
        }},
    ]

    # Data row alternating colors + approval color
    for i, g in enumerate(games_data):
        row_idx = i + 2  # 0-indexed, row 0=title, 1=header, 2+=data
        bg = {"red": 0.922, "green": 0.953, "blue": 0.984} if i % 2 == 0 else {"red": 1, "green": 1, "blue": 1}
        requests.append({"repeatCell": {
            "range": {"sheetId": 0, "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                      "startColumnIndex": 0, "endColumnIndex": 25},
            "cell": {"userEnteredFormat": {
                "backgroundColor": bg,
                "textFormat": {"fontSize": 9},
                "verticalAlignment": "MIDDLE"
            }},
            "fields": "userEnteredFormat"
        }})

        # Approval cell color
        approval = check_pagcor_approval(g, approved_lookup)
        if "differs" in approval:
            ap_bg = {"red": 1.0, "green": 0.922, "blue": 0.612}
            ap_fg = {"red": 0.498, "green": 0.298, "blue": 0.0}
        elif "Approved" in approval:
            ap_bg = {"red": 0.776, "green": 0.937, "blue": 0.816}
            ap_fg = {"red": 0.216, "green": 0.337, "blue": 0.141}
        else:
            ap_bg = {"red": 1.0, "green": 0.78, "blue": 0.808}
            ap_fg = {"red": 0.612, "green": 0.0, "blue": 0.024}

        requests.append({"repeatCell": {
            "range": {"sheetId": 0, "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                      "startColumnIndex": 24, "endColumnIndex": 25},
            "cell": {"userEnteredFormat": {
                "backgroundColor": ap_bg,
                "textFormat": {"bold": True, "fontSize": 9, "foregroundColor": ap_fg},
                "wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE"
            }},
            "fields": "userEnteredFormat"
        }})

    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests}
    ).execute()

    # Set public permission
    drive.permissions().create(
        fileId=sheet_id,
        body={"type": "anyone", "role": "reader"},
        fields="id"
    ).execute()

    return sheet_url

# ─── PARSE COMMAND ──────────────────────────────────────────────────────────────
def parse_game_command(text: str) -> tuple[list[str], str]:
    """
    รับข้อความ เช่น 'ทำไฟล์เกม Golden Empire, Boxing King ให้ PY หน่อย'
    return (game_names, client_name)
    """
    # ดึงชื่อลูกค้า: ข้อความหลัง 'ให้' และก่อน 'หน่อย' (หรือท้ายประโยค)
    client_match = re.search(r'ให้\s*([^\s]+?)(?:\s+หน่อย|$)', text, re.IGNORECASE)
    client_name  = client_match.group(1).strip() if client_match else "CLIENT"

    # ดึงชื่อเกม: ข้อความหลัง 'เกม' และก่อน 'ให้'
    game_section_match = re.search(r'เกม\s+(.+?)\s+ให้', text, re.IGNORECASE | re.DOTALL)
    if not game_section_match:
        return [], client_name

    game_section = game_section_match.group(1)
    # แยกด้วย , หรือ / หรือ และ
    raw_games = re.split(r'[,/\n]|และ|and', game_section, flags=re.IGNORECASE)
    game_names = [g.strip() for g in raw_games if g.strip()]
    return game_names, client_name

def match_games(game_names: list[str], all_games: list[dict]) -> dict:
    """
    return {
      'found': [(name_input, game_dict), ...],
      'not_found': [name_input, ...],
      'ambiguous': [(name_input, [game_dict, ...]), ...]
    }
    """
    result = {"found": [], "not_found": [], "ambiguous": []}
    for name in game_names:
        name_upper = name.upper()
        exact = [g for g in all_games if g["Name"].upper() == name_upper]
        if len(exact) == 1:
            result["found"].append((name, exact[0]))
        elif len(exact) > 1:
            result["ambiguous"].append((name, exact))
        else:
            # fuzzy: contains
            partial = [g for g in all_games if name_upper in g["Name"].upper()]
            if len(partial) == 1:
                result["found"].append((name, partial[0]))
            elif len(partial) > 1:
                result["ambiguous"].append((name, partial))
            else:
                result["not_found"].append(name)
    return result

# ─── BOT HANDLERS ───────────────────────────────────────────────────────────────
def auth_check(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ALLOWED_USER_ID:
            await update.message.reply_text("⛔ ไม่มีสิทธิ์ใช้งานบอทนี้ค่ะ")
            return ConversationHandler.END
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper

@auth_check
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 สวัสดีค่ะ! บอทนี้ช่วยสร้างไฟล์ PAGCOR Game Parameters ให้ค่ะ\n\n"
        "📌 วิธีใช้:\n"
        "`ทำไฟล์เกม [ชื่อเกม1], [ชื่อเกม2] ให้ [ชื่อลูกค้า] หน่อย`\n\n"
        "ตัวอย่าง:\n"
        "`ทำไฟล์เกม Golden Empire, Boxing King, Mega Ace ให้ PY หน่อย`",
        parse_mode="Markdown"
    )

@auth_check
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # เช็คว่าเป็นคำสั่งทำไฟล์ไหม
    if "ทำไฟล์เกม" not in text:
        await update.message.reply_text(
            "💡 พิมพ์ `ทำไฟล์เกม [ชื่อเกม] ให้ [ลูกค้า] หน่อย` เพื่อเริ่มต้นค่ะ",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    game_names, client_name = parse_game_command(text)
    if not game_names:
        await update.message.reply_text("❌ ไม่พบชื่อเกมในคำสั่งค่ะ กรุณาลองใหม่นะคะ")
        return ConversationHandler.END

    await update.message.reply_text("🔍 กำลังตรวจสอบรายชื่อเกมค่ะ...")

    try:
        sheets_svc, drive = get_google_services()
        all_games = fetch_game_list(sheets_svc)
    except Exception as e:
        await update.message.reply_text(f"❌ เชื่อมต่อ Google API ไม่ได้ค่ะ: {e}")
        return ConversationHandler.END

    match_result = match_games(game_names, all_games)

    # เก็บ state ไว้ใน context
    context.user_data["found"]      = match_result["found"]
    context.user_data["not_found"]  = match_result["not_found"]
    context.user_data["ambiguous"]  = match_result["ambiguous"]
    context.user_data["client"]     = client_name
    context.user_data["sheets_svc"] = sheets_svc
    context.user_data["drive"]      = drive

    # Build summary message
    msg_lines = [f"📋 ตรวจสอบเกมสำหรับลูกค้า: *{client_name}*\n"]

    if match_result["found"]:
        msg_lines.append("✅ *พบแล้ว:*")
        for name_in, g in match_result["found"]:
            msg_lines.append(f"  • {g['Name']} (ID: {g['GameID']})")

    if match_result["not_found"]:
        msg_lines.append("\n❌ *ไม่พบในระบบ:*")
        for name in match_result["not_found"]:
            msg_lines.append(f"  • {name}")

    if match_result["ambiguous"]:
        msg_lines.append("\n⚠️ *ชื่อตรงกับหลายเกม (ต้องการข้อมูลเพิ่มเติม):*")
        for name_in, candidates in match_result["ambiguous"]:
            msg_lines.append(f"  • '{name_in}' ตรงกับ:")
            for c in candidates:
                msg_lines.append(f"    - {c['Name']} (ID: {c['GameID']})")

    msg_lines.append("\n─────────────────────")

    if match_result["not_found"] or match_result["ambiguous"]:
        if match_result["found"]:
            msg_lines.append(
                "จะดำเนินการอย่างไรต่อดีคะ?\n"
                "▶️ พิมพ์ `ดำเนินการต่อ` — สร้างไฟล์เฉพาะเกมที่พบแล้ว\n"
                "✏️ พิมพ์ชื่อเกมใหม่ที่ถูกต้อง — แก้ไขและลองใหม่"
            )
        else:
            msg_lines.append("❌ ไม่พบเกมใดเลยค่ะ กรุณาตรวจสอบชื่อเกมแล้วลองใหม่นะคะ")
            await update.message.reply_text("\n".join(msg_lines), parse_mode="Markdown")
            return ConversationHandler.END
    else:
        # พบครบทุกเกม
        count = len(match_result["found"])
        today = datetime.now().strftime("%Y%m%d")
        file_name = f"{today}_PAGCOR_{client_name}_{count}games"
        msg_lines.append(
            f"📁 ชื่อไฟล์ที่จะสร้าง: `{file_name}`\n"
            f"✅ พร้อมสร้างไฟล์ได้เลยค่ะ!\n\n"
            "พิมพ์ `ยืนยัน` เพื่อสร้างไฟล์ หรือ `ยกเลิก` เพื่อออกค่ะ"
        )

    await update.message.reply_text("\n".join(msg_lines), parse_mode="Markdown")
    return CONFIRM_GAMES

@auth_check
async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()

    if text in ["ยกเลิก", "cancel"]:
        await update.message.reply_text("❌ ยกเลิกแล้วค่ะ")
        return ConversationHandler.END

    if text not in ["ยืนยัน", "confirm", "ดำเนินการต่อ"]:
        await update.message.reply_text("กรุณาพิมพ์ `ยืนยัน` หรือ `ยกเลิก` ค่ะ", parse_mode="Markdown")
        return CONFIRM_GAMES

    found       = context.user_data.get("found", [])
    client_name = context.user_data.get("client", "CLIENT")
    sheets_svc  = context.user_data.get("sheets_svc")
    drive       = context.user_data.get("drive")

    if not found:
        await update.message.reply_text("❌ ไม่มีเกมที่จะสร้างไฟล์ค่ะ")
        return ConversationHandler.END

    await update.message.reply_text("⏳ กำลังสร้างไฟล์ กรุณารอสักครู่นะคะ...")

    try:
        approved_lookup = fetch_pagcor_approved(sheets_svc)
        games_data = [g for _, g in found]
        sheet_url = build_output_sheet(
            drive, sheets_svc, games_data, client_name, approved_lookup
        )
        count = len(games_data)
        today = datetime.now().strftime("%Y%m%d")
        file_name = f"{today}_PAGCOR_{client_name}_{count}games"
        await update.message.reply_text(
            f"✅ สร้างไฟล์เสร็จแล้วค่ะ!\n\n"
            f"📄 *{file_name}*\n"
            f"🔗 {sheet_url}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error creating sheet: {e}", exc_info=True)
        await update.message.reply_text(f"❌ เกิดข้อผิดพลาดค่ะ: {e}")

    return ConversationHandler.END

@auth_check
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ ยกเลิกแล้วค่ะ")
    return ConversationHandler.END

# ─── MAIN ───────────────────────────────────────────────────────────────────────
async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)],
        states={
            CONFIRM_GAMES: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)

    logger.info("Bot started...")
    await app.run_polling(drop_pending_updates=True)

import asyncio

if __name__ == "__main__":
    asyncio.run(main())
    main()
