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


def list_drive_folder(drive, folder_id):
    """List files in Google Drive folder"""
    files = []
    page_token = None
    try:
        while True:
            resp = drive.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="files(id,name,mimeType,webViewLink),nextPageToken",
                pageSize=500,
                pageToken=page_token,
                supportsAllDrives=True
            ).execute()
            files.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:
        logger.warning(f"⚠ Error listing folder {folder_id}: {e}")
    return files


def find_instruction_file(drive, game_id):
    """
    Find instruction file by GAME ID ONLY (strict matching)
    Pass 1: Exact ID boundary matching
    Pass 2: Try without ID if Pass 1 fails (fallback)
    """
    id_str = str(game_id).strip()
    logger.info(f"  🔍 Searching instruction for ID: {id_str}")
    
    all_files = []
    for folder_id in INSTRUCTION_FOLDERS:
        all_files.extend(list_drive_folder(drive, folder_id))
    
    logger.info(f"    Total instruction files: {len(all_files)}")
    
    # Pass 1: Match by Game ID with strict boundary
    # Pattern: _523_, GAMEID_523, 523_, _523
    patterns = [
        f"{id_str}_",       # 300_
        f"GAMEID_{id_str}_", # GAMEID_300_
        f"_{id_str}_",      # _523_
        f"GAMEID_{id_str}",  # GAMEID_523
        f"_{id_str}",        # _523
    ]
    
    for f in all_files:
        title = f["name"].upper()
        for pattern in patterns:
            if pattern in title:
                logger.info(f"    ✓ Found by ID: {f['name']}")
                return f["webViewLink"]
    
    logger.info(f"    ✗ No instruction file found for ID {id_str}")
    return None


def find_cert_folder(drive, game_id):
    """
    Find certificate file by GAME ID ONLY (strict matching)
    """
    id_str = str(game_id).strip()
    logger.info(f"  🔍 Searching certificate for ID: {id_str}")
    
    all_files = list_drive_folder(drive, CERT_JILI_FOLDER)
    logger.info(f"    Total certificate files: {len(all_files)}")
    
    # Match by Game ID with strict boundary
    patterns = [
        f"{id_str}_",       # 300_
        f"GAMEID_{id_str}_", # GAMEID_300_
        f"_{id_str}_",
        f"GAMEID_{id_str}",
        f"_{id_str}",
    ]
    
    for f in all_files:
        title = f["name"].upper()
        for pattern in patterns:
            if pattern in title:
                logger.info(f"    ✓ Found by ID: {f['name']}")
                return f["webViewLink"]
    
    logger.info(f"    ✗ No certificate file found for ID {id_str}")
    return None


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

    data_rows = []
    for g in games_data:
        approval = check_pagcor_approval(g, approved_lookup)
        instr = find_instruction_file(drive, g["GameID"])
        instr_val = instr if instr else "Please contact JILI BD"
        cert = find_cert_folder(drive, g["GameID"])
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

        # Insert title row at top
        sheets_svc2.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [
                {"insertDimension": {
                    "range": {"sheetId": 0, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                    "inheritFromBefore": False
                }}
            ]}
        ).execute()

        # Write title
        sheets_svc2.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range="Sheet1!A1",
            valueInputOption="RAW",
            body={"values": [["JILI Games: PAGCOR Game Parameter and RTP Details"]]}
        ).execute()

        num_cols = 27
        requests = [
            # Merge title row
            {"mergeCells": {
                "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": num_cols},
                "mergeType": "MERGE_ALL"
            }},
            # Title style
            {"repeatCell": {
                "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1,
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
                "range": {"sheetId": 0, "startRowIndex": 1, "endRowIndex": 2,
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
                "properties": {"sheetId": 0, "gridProperties": {"frozenRowCount": 2}},
                "fields": "gridProperties.frozenRowCount"
            }},
            # Title row height
            {"updateDimensionProperties": {
                "range": {"sheetId": 0, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 30}, "fields": "pixelSize"
            }},
            # Header row height
            {"updateDimensionProperties": {
                "range": {"sheetId": 0, "dimension": "ROWS", "startIndex": 1, "endIndex": 2},
                "properties": {"pixelSize": 45}, "fields": "pixelSize"
            }},
            # Name column width
            {"updateDimensionProperties": {
                "range": {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
                "properties": {"pixelSize": 160}, "fields": "pixelSize"
            }},
            # Last 3 columns width
            {"updateDimensionProperties": {
                "range": {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 24, "endIndex": 27},
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
