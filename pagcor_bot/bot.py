import os
import re
import logging
import asyncio
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

ALLOWED_USER_ID   = int(os.environ["ALLOWED_USER_ID"])
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
SPREADSHEET_ID    = "1Yv3xRFyqR2ykQPSpH30zpHSGn0_r8FozFiSNXaVanpU"
OUTPUT_FOLDER_ID  = "0AAvPOmSqbiEKUk9PVA"
CERT_JILI_FOLDER  = "1TMJiM0fBTedI4C55f7phObwQ-8jqpWBm"
INSTRUCTION_FOLDERS = [
    "1r5K5KHpD-i6gEkQ9Hc3tnL33KNDWHt28",
    "1UUb2uXq1EJPq-HWa8MPZed6TxJxbz1lX",
    "1Kgy7Jqi5akFVTFsSvhU7w9J3oIEaHimW",
    "1XVNMHilWKrNidX0sjP8rr0t_e6bl0_S9",
    "1bXgsQDqDcqLAylVbhEEzY05QX7mnXzGQ",
]

CONFIRM_GAMES = 0
API_TIMEOUT = 10  # seconds


def get_google_services():
    sa_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
    )
    sheets = build("sheets", "v4", credentials=creds)
    drive  = build("drive",  "v3", credentials=creds)
    return sheets, drive


def fetch_game_list(sheets):
    """Fetch all games from spreadsheet with timeout"""
    try:
        result = sheets.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="ดึงข้อมูล-查詢表!A2:AB1000"
        ).execute()
        rows = result.get("values", [])
        keys = ["GameID","Name","GameID2","GAME_VERSION","GAME_OFFERING","Game_Type",
                "Min_Bet","Max_Bet","Max_Odds","Support","GamePlay","Hit_Rate",
                "Free_Game_Rate","Default_Bet","Max_Exposure","Paid_Spins","RTP",
                "SD1","SD2","CI90","CI95","CI99",
                "CI90_Min","CI90_Max","CI95_Min","CI95_Max","CI99_Min","CI99_Max"]
        games = []
        for row in rows:
            if not row or not row[0]:
                continue
            padded = row + [""] * (len(keys) - len(row))
            g = dict(zip(keys, padded))
            if g["GameID"] and g["Name"]:
                games.append(g)
        logger.info(f"✓ Fetched {len(games)} games from spreadsheet")
        return games
    except Exception as e:
        logger.error(f"✗ Error fetching game list: {e}")
        raise


def fetch_pagcor_approved(sheets):
    """Fetch PAGCOR approved games"""
    try:
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
        logger.info(f"✓ Fetched {len(lookup)} PAGCOR approved games")
        return lookup
    except Exception as e:
        logger.error(f"✗ Error fetching PAGCOR list: {e}")
        raise


def list_drive_folder(drive, folder_id, retries=3):
    """List files in Google Drive folder (retries on Google-side 5xx errors)"""
    import time
    for attempt in range(1, retries + 1):
        files = []
        page_token = None
        try:
            while True:
                resp = drive.files().list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    fields="files(id,name,mimeType,webViewLink,modifiedTime),nextPageToken",
                    pageSize=500,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ).execute()
                files.extend(resp.get("files", []))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
            return files
        except Exception as e:
            logger.warning(f"⚠ Error listing folder {folder_id} (attempt {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(2 * attempt)
    return []


# ---------- File matching helpers ----------
# Leading ID prefix in a Drive file/folder name, e.g.
#   "523_Fortune Coins", "GAMEID_887_Super Ace Hula", "ID_300 Fortune Gems 3"
_ID_PREFIX_RE = re.compile(r"^\s*(?:GAME\s*ID|ID)?[\s_\-]*0*(\d+)(?=[\s_\-.])", re.IGNORECASE)

# Suffixes that are not part of the game name
_SUFFIX_RE = re.compile(
    r"[\s_\-]*(game\s*rules?|instructions?|introduction(?:\s*ver\s*[\d.]+)?)(?:[\s_\-]*(en|eng|english))?\s*$",
    re.IGNORECASE,
)


def _file_game_id(name):
    """Return the Game ID at the start of a file name, or None"""
    m = _ID_PREFIX_RE.match(name)
    return m.group(1) if m else None


def _normalize_name(name):
    """Lowercase, drop extension / ID prefix / 'instructions' suffix / symbols"""
    n = re.sub(r"\.(pdf|docx?|xlsx?|pptx?|zip)$", "", name.strip(), flags=re.IGNORECASE)
    if _file_game_id(n):
        n = _ID_PREFIX_RE.sub("", n, count=1)
    for _ in range(2):
        n = _SUFFIX_RE.sub("", n)
    return re.sub(r"[^a-z0-9]", "", n.lower())


def _pick_best(candidates):
    """Prefer 'instruction' files, then PDFs, then the most recently modified"""
    def score(f):
        nm = f["name"].lower()
        return (
            "instruction" in nm or "rule" in nm,
            nm.endswith(".pdf") or f.get("mimeType") == "application/vnd.google-apps.folder",
            f.get("modifiedTime", ""),
        )
    return sorted(candidates, key=score, reverse=True)[0]


def match_file(files, game_id, game_name, label=""):
    """
    Pass 1: Game ID strict match — the ID must be the leading number of the name
            (523 matches '523_Fortune Coins' but NOT '1523_x', '5230_x', 'GAMEID_5231')
    Pass 2: Full game name exact match after normalising
            ('Fortune Hook Boom' == 'FortuneHookBoom_ instructions.pdf')
            Files whose own leading ID belongs to a different game are skipped.
    No keyword/fuzzy pass — that is what pulled 720 'Bison Fortune Coins' for 523 'Fortune Coins'.
    """
    id_str = str(game_id).strip().lstrip("0") or "0"

    by_id = [f for f in files if _file_game_id(f["name"]) == id_str]
    if by_id:
        best = _pick_best(by_id)
        logger.info(f"    ✓ {label} by ID {id_str}: {best['name']}")
        return best["webViewLink"]

    target = _normalize_name(game_name or "")
    if target:
        by_name = [
            f for f in files
            if _normalize_name(f["name"]) == target
            and _file_game_id(f["name"]) in (None, id_str)
        ]
        if by_name:
            best = _pick_best(by_name)
            logger.info(f"    ✓ {label} by name '{game_name}': {best['name']}")
            return best["webViewLink"]

    logger.info(f"    ✗ No {label} file for ID {id_str} / '{game_name}'")
    return None


def load_drive_indexes(drive):
    """List every instruction + certificate file ONCE per request"""
    instr_files = []
    for folder_id in INSTRUCTION_FOLDERS:
        instr_files.extend(list_drive_folder(drive, folder_id))
    cert_files = list_drive_folder(drive, CERT_JILI_FOLDER)
    logger.info(f"✓ Indexed {len(instr_files)} instruction / {len(cert_files)} certificate items")
    return instr_files, cert_files


def find_instruction_file(instr_files, game_id, game_name):
    return match_file(instr_files, game_id, game_name, label="instruction")


def find_cert_folder(cert_files, game_id, game_name):
    return match_file(cert_files, game_id, game_name, label="certificate")


def check_pagcor_approval(game, approved_lookup):
    """Check PAGCOR approval status"""
    name_upper = game["Name"].strip().upper()
    game_id = str(game["GameID"]).strip()
    if name_upper in approved_lookup:
        pagcor_id_ver = approved_lookup[name_upper]
        pagcor_num = pagcor_id_ver.split("-")[0].strip()
        if pagcor_num == game_id:
            return f"Approved by PAGCOR - Game ID & Name matched (PAGCOR ID: {pagcor_id_ver})"
        else:
            return f"Approved by PAGCOR - Name matched, but Game ID differs (JILI ID: {game_id} / PAGCOR ID: {pagcor_id_ver})"
    return "Not yet approved by PAGCOR"


def build_output_sheet(drive, sheets_svc, games_data, client_name, approved_lookup):
    import io, csv
    from googleapiclient.http import MediaIoBaseUpload
    today = datetime.now().strftime("%Y%m%d")
    count = len(games_data)
    file_name = f"{today}_PAGCOR_{client_name}_{count}games"

    header_row = [
        "GameID", "Name", "GAME VERSION", "GAME OFFERING", "Game Type",
        "Min Bet", "Max Bet", "Max Odds", "Support", "GamePlay",
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

    instr_files, cert_files = load_drive_indexes(drive)

    data_rows = []
    for g in games_data:
        approval = check_pagcor_approval(g, approved_lookup)
        instr = find_instruction_file(instr_files, g["GameID"], g["Name"])
        instr_val = instr if instr else "Please contact JILI BD"
        cert = find_cert_folder(cert_files, g["GameID"], g["Name"])
        cert_val = cert if cert else "This game has not yet been scheduled for lab certification"

        min_bet = str(g["Min_Bet"]) if g["Min_Bet"] else ""
        max_bet = str(g["Max_Bet"]) if g["Max_Bet"] else ""
        def_bet = str(g["Default_Bet"]) if g["Default_Bet"] else ""
        max_exp = str(g["Max_Exposure"]) if g["Max_Exposure"] else ""

        row = [
            g["GameID"], g["Name"], g["GAME_VERSION"], g["GAME_OFFERING"], g["Game_Type"],
            min_bet, max_bet, g["Max_Odds"], g["Support"], g["GamePlay"],
            def_bet, max_exp, g["RTP"],
            g["SD1"], g["SD2"],
            g["CI90"], g["CI95"], g["CI99"],
            g["CI90_Min"], g["CI90_Max"],
            g["CI95_Min"], g["CI95_Max"],
            g["CI99_Min"], g["CI99_Max"],
            approval, instr_val, cert_val,
        ]
        data_rows.append(row)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(header_row)
    writer.writerows(data_rows)
    csv_bytes = output.getvalue().encode("utf-8")

    media = MediaIoBaseUpload(
        io.BytesIO(csv_bytes),
        mimetype="text/csv",
        resumable=False
    )
    file_meta = {
        "name": file_name,
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": [OUTPUT_FOLDER_ID],
    }
    created = drive.files().create(
        body=file_meta,
        media_body=media,
        fields="id,webViewLink",
        supportsAllDrives=True
    ).execute()

    sheet_id  = created["id"]
    sheet_url = created["webViewLink"]

    # Add title row + formatting via Sheets API
    try:
        sheets_svc2 = sheets_svc

        # CSV import names the tab after the file (not "Sheet1") and the tab id
        # is not always 0 -> read the real id/title first
        meta = sheets_svc2.spreadsheets().get(
            spreadsheetId=sheet_id, fields="sheets.properties(sheetId,title)"
        ).execute()
        tab = meta["sheets"][0]["properties"]
        tab_id, tab_title = tab["sheetId"], tab["title"]

        # Insert title row at top
        sheets_svc2.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [
                {"insertDimension": {
                    "range": {"sheetId": tab_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                    "inheritFromBefore": False
                }}
            ]}
        ).execute()

        # Write title
        sheets_svc2.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{tab_title}'!A1",
            valueInputOption="RAW",
            body={"values": [["JILI Games: PAGCOR Game Parameter and RTP Details"]]}
        ).execute()

        num_cols = 27
        requests = [
            # Merge title row
            {"mergeCells": {
                "range": {"sheetId": tab_id, "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": num_cols},
                "mergeType": "MERGE_ALL"
            }},
            # Title style
            {"repeatCell": {
                "range": {"sheetId": tab_id, "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": num_cols},
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
                "range": {"sheetId": tab_id, "startRowIndex": 1, "endRowIndex": 2,
                          "startColumnIndex": 0, "endColumnIndex": num_cols},
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
            # Freeze top 2 rows
            {"updateSheetProperties": {
                "properties": {"sheetId": tab_id, "gridProperties": {"frozenRowCount": 2}},
                "fields": "gridProperties.frozenRowCount"
            }},
            # Title row height
            {"updateDimensionProperties": {
                "range": {"sheetId": tab_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 30}, "fields": "pixelSize"
            }},
            # Header row height
            {"updateDimensionProperties": {
                "range": {"sheetId": tab_id, "dimension": "ROWS", "startIndex": 1, "endIndex": 2},
                "properties": {"pixelSize": 45}, "fields": "pixelSize"
            }},
            # Name column width
            {"updateDimensionProperties": {
                "range": {"sheetId": tab_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
                "properties": {"pixelSize": 160}, "fields": "pixelSize"
            }},
            # Last 3 columns width
            {"updateDimensionProperties": {
                "range": {"sheetId": tab_id, "dimension": "COLUMNS", "startIndex": 24, "endIndex": 27},
                "properties": {"pixelSize": 300}, "fields": "pixelSize"
            }},
        ]
        sheets_svc2.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": requests}
        ).execute()
    except Exception as fmt_err:
        logger.warning(f"Formatting failed (non-critical): {fmt_err}")

    # Set public permission
    drive.permissions().create(
        fileId=sheet_id,
        body={"type": "anyone", "role": "reader"},
        fields="id",
        supportsAllDrives=True
    ).execute()

    return sheet_url, file_name


def parse_game_command(text):
    """
    Parse game command: ทำไฟล์เกม [ID1], [ID2], ... ให้ [CLIENT]
    Example: ทำไฟล์เกม 573, 523, 720 ให้ PY หน่อย
    """
    client_match = re.search(r'ให้\s*([^\s]+?)(?:\s+หน่อย|$)', text, re.IGNORECASE)
    client_name  = client_match.group(1).strip() if client_match else "CLIENT"
    
    game_section_match = re.search(r'เกม\s+(.+?)\s+ให้', text, re.IGNORECASE | re.DOTALL)
    if not game_section_match:
        return [], client_name
    
    game_section = game_section_match.group(1)
    # Split by comma, slash, newline, และ, and
    raw_games = re.split(r'[,/\n]|และ|and', game_section, flags=re.IGNORECASE)
    game_ids = [g.strip() for g in raw_games if g.strip() and g.strip().isdigit()]
    
    return game_ids, client_name


def match_games_by_id(game_ids, all_games):
    """
    Match games by ID instead of name
    Returns: found, not_found
    """
    result = {"found": [], "not_found": []}
    game_id_map = {g["GameID"]: g for g in all_games}
    
    for gid in game_ids:
        if gid in game_id_map:
            result["found"].append((gid, game_id_map[gid]))
            logger.info(f"  ✓ Found game ID {gid}: {game_id_map[gid]['Name']}")
        else:
            result["not_found"].append(gid)
            logger.warning(f"  ✗ Game ID {gid} not found")
    
    return result


def auth_check(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ALLOWED_USER_ID:
            await update.message.reply_text("Sorry, you are not authorized to use this bot.")
            return ConversationHandler.END
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


@auth_check
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I'm the PAGCOR Game Parameter Bot (ID-based search).\n\n"
        "How to use:\n"
        "`ทำไฟล์เกม [ID1], [ID2], [ID3] ให้ [client] หน่อย`\n\n"
        "Example:\n"
        "`ทำไฟล์เกม 573, 523, 720 ให้ PY หน่อย`\n\n"
        "🎮 Use Game IDs only (not game names)",
        parse_mode="Markdown"
    )


@auth_check
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if "ทำไฟล์เกม" not in text:
        await update.message.reply_text(
            "Please use the format:\n`ทำไฟล์เกม [ID1], [ID2] ให้ [client] หน่อย`\n\n"
            "Example: `ทำไฟล์เกม 573, 523 ให้ PY หน่อย`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    game_ids, client_name = parse_game_command(text)
    if not game_ids:
        await update.message.reply_text(
            "❌ No game IDs found. Please use numeric IDs only.\n"
            "Example: `ทำไฟล์เกม 573, 523 ให้ PY หน่อย`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    await update.message.reply_text("⏳ Checking game list, please wait...")
    logger.info(f"📋 User requested: Game IDs {game_ids} for client {client_name}")

    try:
        sheets_svc, drive = get_google_services()
        all_games = fetch_game_list(sheets_svc)
    except Exception as e:
        logger.error(f"❌ Google API error: {e}")
        await update.message.reply_text(f"❌ Google API connection failed: {e}")
        return ConversationHandler.END

    match_result = match_games_by_id(game_ids, all_games)
    context.user_data["found"]      = match_result["found"]
    context.user_data["not_found"]  = match_result["not_found"]
    context.user_data["client"]     = client_name
    context.user_data["sheets_svc"] = sheets_svc
    context.user_data["drive"]      = drive

    msg_lines = [f"🎯 Game check for client: *{client_name}*\n"]

    if match_result["found"]:
        msg_lines.append("✅ *Found:*")
        for gid, g in match_result["found"]:
            msg_lines.append(f"  • ID {gid}: {g['Name']}")

    if match_result["not_found"]:
        msg_lines.append("\n❌ *Not found:*")
        for gid in match_result["not_found"]:
            msg_lines.append(f"  • ID {gid}")

    msg_lines.append("\n─────────────────")

    if not match_result["found"]:
        msg_lines.append("No games found. Please check the IDs and try again.")
        await update.message.reply_text("\n".join(msg_lines), parse_mode="Markdown")
        return ConversationHandler.END

    today = datetime.now().strftime("%Y%m%d")
    count = len(match_result["found"])
    file_name = f"{today}_PAGCOR_{client_name}_{count}games"

    if match_result["not_found"]:
        msg_lines.append(
            "Type `ดำเนินการต่อ` to create file with found games only\n"
            "Or retype with correct game IDs to try again."
        )
    else:
        msg_lines.append(
            f"File name: `{file_name}`\n\n"
            "Type `ยืนยัน` to create the file, or `ยกเลิก` to cancel."
        )

    full_msg = "\n".join(msg_lines)
    if len(full_msg) > 4000:
        chunks = [full_msg[i:i+4000] for i in range(0, len(full_msg), 4000)]
        for chunk in chunks:
            try:
                await update.message.reply_text(chunk, parse_mode="Markdown")
            except Exception as e:
                logger.warning(f"Markdown parse failed, sending plain text: {e}")
                await update.message.reply_text(chunk)
    else:
        try:
            await update.message.reply_text(full_msg, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Markdown parse failed, sending plain text: {e}")
            await update.message.reply_text(full_msg)
    
    return CONFIRM_GAMES


@auth_check
async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()

    if text in ["ยกเลิก", "cancel"]:
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    if text not in ["ยืนยัน", "confirm", "ดำเนินการต่อ"]:
        await update.message.reply_text("Please type `ยืนยัน` or `ยกเลิก`", parse_mode="Markdown")
        return CONFIRM_GAMES

    found       = context.user_data.get("found", [])
    client_name = context.user_data.get("client", "CLIENT")
    sheets_svc  = context.user_data.get("sheets_svc")
    drive       = context.user_data.get("drive")

    if not found:
        await update.message.reply_text("No games to create file for.")
        return ConversationHandler.END

    await update.message.reply_text("⏳ Creating file, please wait...")
    logger.info(f"📝 Creating sheet for {len(found)} games, client: {client_name}")

    try:
        approved_lookup = fetch_pagcor_approved(sheets_svc)
        games_data = [g for _, g in found]
        sheet_url, file_name = build_output_sheet(
            drive, sheets_svc, games_data, client_name, approved_lookup
        )
        logger.info(f"✓ Sheet created: {file_name}")
        await update.message.reply_text(
            f"✅ File created successfully!\n\n"
            f"📄 {file_name}\n\n"
            f"🔗 {sheet_url}"
        )
    except Exception as e:
        logger.error(f"❌ Error creating file: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error creating file: {e}")

    return ConversationHandler.END


@auth_check
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def main():
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
    logger.info("🤖 Bot started with ID-based game search...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
