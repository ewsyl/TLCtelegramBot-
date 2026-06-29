import os
import json
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, JobQueue
)
from closeio_api import Client as CloseClient

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CLOSE_API_KEY = os.environ["CLOSE_API_KEY"]
ALLOWED_CHAT_IDS = [int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if x]

api = CloseClient(CLOSE_API_KEY)

STATUS_SETTING_TERMIN = "stat_JwsU6xiZ6FLOFj6L7OuQszArTHI1B7bnqWqlq5lG2eD"
STATUS_GEWONNEN_NEUKUNDEN = "stat_l7OrjaDo2dfydXbwNo17AsW5ZGKpclv0nZAkNQy1uow"
STATUS_GEWONNEN_SALES = "stat_4PhCxgaZi75vBNZYnn5RQ4l8Fg21yLWATBwTWWVb2Ea"


def check_auth(update: Update) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True
    return update.effective_chat.id in ALLOWED_CHAT_IDS


def format_currency(value) -> str:
    if value is None:
        return "€0"
    return f"€{value:,.0f}".replace(",", ".")


def get_date_range(period: str) -> tuple[str, str]:
    today = datetime.now()
    if period == "today":
        start = today.replace(hour=0, minute=0, second=0, microsecond=0)
        end = today
    elif period == "week":
        start = (today - timedelta(days=today.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = today
    elif period == "month":
        start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = today
    else:
        start = today.replace(hour=0, minute=0, second=0, microsecond=0)
        end = today
    return start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")


def parse_meeting_note(note: str) -> dict:
    """Extrahiert Infos aus dem Meeting-Note (Ereignisname, Telefon, Quelle)."""
    result = {"type": "Sonstiges", "phone": "", "source": ""}
    if not note:
        return result
    lines = note.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "Ereignisname" and i + 1 < len(lines):
            event_name = lines[i + 1].strip()
            if "Kennenlerngespräch" in event_name or "Kennenlerngespräch" in event_name:
                result["type"] = "Kennenlerngespräch"
            elif "Strategiegespräch" in event_name or "Strategiegespräch" in event_name:
                result["type"] = "Strategiegespräch"
            else:
                result["type"] = event_name[:40]
        if "Telefonnummer" in line and i + 1 < len(lines):
            result["phone"] = lines[i + 1].strip() if lines[i].endswith(":") else line.split(":")[-1].strip()
        if "aufmerksam" in line.lower():
            result["source"] = line.split(":")[-1].strip()
    return result


async def get_meetings_for_period(start: str, end: str) -> list:
    """Alle Meetings in einem Zeitraum abrufen."""
    result = api.get("activity/meeting", params={
        "date_created__gte": start,
        "date_created__lte": end,
        "_fields": "id,lead_id,lead_name,note,title,starts_at,duration,user_name"
    })
    return result.get("data", [])


async def get_calls_for_period(start: str, end: str) -> list:
    """Alle Calls in einem Zeitraum abrufen."""
    result = api.get("activity/call", params={
        "date_created__gte": start,
        "date_created__lte": end,
        "_fields": "id,lead_id,lead_name,duration,user_name,date_created"
    })
    return result.get("data", [])


async def build_report(period: str) -> str:
    start, end = get_date_range(period)
    label = {"today": "Heute", "week": "Diese Woche", "month": "Diesen Monat"}.get(period, "Heute")

    # --- Termine nach Kalendertyp ---
    meetings = await get_meetings_for_period(start, end)
    kennenlern = [m for m in meetings if "Kennenlerngespräch" in (m.get("note") or "")]
    strategie = [m for m in meetings if "Strategiegespräch" in (m.get("note") or "") or "Strategiegespräch" in (m.get("note") or "")]

    # --- Calls die stattgefunden haben (duration > 0) ---
    calls = await get_calls_for_period(start, end)
    calls_happened = [c for c in calls if (c.get("duration") or 0) > 0]

    # Lead-IDs der Meetings (nur vergangene, d.h. starts_at <= jetzt)
    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    past_meetings = [m for m in meetings if (m.get("starts_at") or "") <= now_str]
    meeting_lead_ids = set(m.get("lead_id") for m in past_meetings if m.get("lead_id"))
    calls_on_meeting_leads = [c for c in calls_happened if c.get("lead_id") in meeting_lead_ids]

    total_past = len(past_meetings)
    calls_done = len(calls_on_meeting_leads)
    no_shows = total_past - calls_done
    no_show_rate = round((no_shows / total_past * 100)) if total_past > 0 else 0
    show_rate = 100 - no_show_rate

    # --- Settings ---
    settings_result = api.get("opportunity", params={
        "status_id": STATUS_SETTING_TERMIN,
        "date_updated__gte": start,
        "date_updated__lte": end,
        "_fields": "id,lead_name,date_updated,user_name"
    })
    settings_deals = settings_result.get("data", [])

    # --- Closings ---
    won_neu = api.get("opportunity", params={
        "status_id": STATUS_GEWONNEN_NEUKUNDEN,
        "date_won__gte": start,
        "date_won__lte": end,
        "_fields": "id,value,lead_name,date_won,user_name"
    })
    won_sales = api.get("opportunity", params={
        "status_id": STATUS_GEWONNEN_SALES,
        "date_won__gte": start,
        "date_won__lte": end,
        "_fields": "id,value,lead_name,date_won,user_name"
    })
    closings = won_neu.get("data", []) + won_sales.get("data", [])
    closing_value = sum((d.get("value") or 0) for d in closings) / 100

    lines = [
        f"📊 *Sales Report — {label}*",
        f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        "",
        "📞 *Termine*",
        f"  • Kennenlerngespräche: *{len(kennenlern)}*",
        f"  • Strategiegespräche: *{len(strategie)}*",
        f"  • Gesamt: *{len(meetings)}*",
        "",
        "✅ *Show-Rate (vergangene Termine)*",
        f"  • Termine stattgefunden: *{total_past}*",
        f"  • Calls durchgeführt: *{calls_done}*",
        f"  • No-Shows: *{no_shows}*",
        f"  • Show-Rate: *{show_rate}%* | No-Show: *{no_show_rate}%*",
        "",
        f"📅 *Settings (Termin gesetzt):* {len(settings_deals)}",
        f"🏆 *Closings:* {len(closings)} ({format_currency(closing_value)})",
    ]

    if settings_deals:
        lines.append("\n*Settings:*")
        for d in settings_deals[:8]:
            lines.append(f"  • {d.get('lead_name', '?')} ({d.get('user_name', '')})")

    if closings:
        lines.append("\n*Closings:*")
        for d in closings[:8]:
            val = format_currency((d.get("value") or 0) / 100)
            lines.append(f"  • {d.get('lead_name', '?')}: {val} ({d.get('user_name', '')})")

    return "\n".join(lines)


async def get_upcoming_appointments() -> str:
    """Offene/bevorstehende Termine (Pipeline)."""
    now = datetime.now()
    future_end = now + timedelta(days=7)
    start_str = now.strftime("%Y-%m-%dT%H:%M:%S")
    end_str = future_end.strftime("%Y-%m-%dT%H:%M:%S")

    result = api.get("activity/meeting", params={
        "date_created__gte": (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S"),
        "_fields": "id,lead_name,note,title,starts_at,duration,user_name"
    })
    all_meetings = result.get("data", [])
    upcoming = [m for m in all_meetings if (m.get("starts_at") or "") >= start_str and (m.get("starts_at") or "") <= end_str]
    upcoming.sort(key=lambda x: x.get("starts_at") or "")

    kennenlern = [m for m in upcoming if "Kennenlerngespräch" in (m.get("note") or "")]
    strategie = [m for m in upcoming if "Strategiegespräch" in (m.get("note") or "") or "Strategiegespräch" in (m.get("note") or "")]

    lines = [
        "🗓️ *Offene Termine (nächste 7 Tage)*",
        f"Gesamt: *{len(upcoming)}* | Kennenlernen: *{len(kennenlern)}* | Strategie: *{len(strategie)}*",
        ""
    ]
    for m in upcoming[:15]:
        name = m.get("lead_name") or m.get("title") or "?"
        user = m.get("user_name", "")
        starts = (m.get("starts_at") or "")[:16].replace("T", " ")
        note = m.get("note") or ""
        mtype = "🔵 KG" if "Kennenlerngespräch" in note else "🟣 SG" if "Strategiegespräch" in note or "Strategiegespräch" in note else "⚪"
        lines.append(f"{mtype} *{name}* ({user})\n    📅 {starts}")

    return "\n".join(lines)


async def get_won_deals(period: str = "week") -> str:
    start, _ = get_date_range(period)
    result = api.get("opportunity", params={
        "status_type": "won",
        "date_won__gte": start,
        "_fields": "id,value,lead_name,date_won,user_name"
    })
    deals = result.get("data", [])
    total = sum((d.get("value") or 0) for d in deals) / 100
    lines = ["✅ *Closings (diese Woche)*", f"Gesamt: *{format_currency(total)}*", ""]
    for d in deals[:15]:
        val = format_currency((d.get("value") or 0) / 100)
        date = (d.get("date_won") or "")[:10]
        lines.append(f"  • {d.get('lead_name', '?')}: {val} ({d.get('user_name', '')}) — {date}")
    return "\n".join(lines)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not check_auth(update):
        return
    data = query.data
    loading_msg = await query.message.reply_text("⏳ Lade Daten...")
    try:
        if data == "report_today":
            text = await build_report("today")
        elif data == "report_week":
            text = await build_report("week")
        elif data == "deals_won":
            text = await get_won_deals("week")
        elif data == "pipeline":
            text = await get_upcoming_appointments()
        else:
            text = "Unbekannte Aktion."
    except Exception as e:
        logger.error(f"Error: {e}")
        text = f"❌ Fehler beim Laden: {str(e)}"
    await loading_msg.delete()
    keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="main_menu")]]
    await query.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        await update.message.reply_text("Kein Zugriff.")
        return
    keyboard = [
        [InlineKeyboardButton("📊 Tagesreport", callback_data="report_today"),
         InlineKeyboardButton("📅 Wochenreport", callback_data="report_week")],
        [InlineKeyboardButton("✅ Closings", callback_data="deals_won"),
         InlineKeyboardButton("🗓️ Offene Termine", callback_data="pipeline")],
    ]
    await update.message.reply_text(
        "🤖 *TLC Sales Bot*\n\nWähle eine Auswertung:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("📊 Tagesreport", callback_data="report_today"),
         InlineKeyboardButton("📅 Wochenreport", callback_data="report_week")],
        [InlineKeyboardButton("✅ Closings", callback_data="deals_won"),
         InlineKeyboardButton("🗓️ Offene Termine", callback_data="pipeline")],
    ]
    await query.message.edit_text(
        "🤖 *TLC Sales Bot*\n\nWähle eine Auswertung:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    text = await build_report("today")
    for chat_id in ALLOWED_CHAT_IDS:
        await context.bot.send_message(chat_id=chat_id, text=f"🌅 *Automatischer Tagesreport*\n\n{text}", parse_mode="Markdown")


async def send_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    text = await build_report("week")
    for chat_id in ALLOWED_CHAT_IDS:
        await context.bot.send_message(chat_id=chat_id, text=f"📆 *Automatischer Wochenreport*\n\n{text}", parse_mode="Markdown")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(main_menu_handler, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(button_handler))
    job_queue: JobQueue = app.job_queue
    job_queue.run_daily(send_daily_report, time=datetime.strptime("08:00", "%H:%M").time())
    job_queue.run_daily(send_weekly_report, time=datetime.strptime("08:30", "%H:%M").time(), days=(0,))
    logger.info("Bot gestartet...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
