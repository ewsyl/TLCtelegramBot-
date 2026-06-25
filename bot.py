import os
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, JobQueue
)
import closeio_api
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
        start = today.replace(hour=0, minute=0, second=0)
        end = today
    elif period == "week":
        start = today - timedelta(days=today.weekday())
        start = start.replace(hour=0, minute=0, second=0)
        end = today
    elif period == "month":
        start = today.replace(day=1, hour=0, minute=0, second=0)
        end = today
    else:
        start = today.replace(hour=0, minute=0, second=0)
        end = today
    return start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        await update.message.reply_text("Kein Zugriff.")
        return

    keyboard = [
        [InlineKeyboardButton("📊 Tagesreport", callback_data="report_today"),
         InlineKeyboardButton("📅 Wochenreport", callback_data="report_week")],
        [InlineKeyboardButton("✅ Abgeschlossene Deals", callback_data="deals_won"),
         InlineKeyboardButton("❌ Verlorene Deals", callback_data="deals_lost")],
        [InlineKeyboardButton("👥 Lead-Statistiken", callback_data="leads"),
         InlineKeyboardButton("🔍 Pipeline", callback_data="pipeline")],
        [InlineKeyboardButton("💬 Gesprächsanalyse", callback_data="conversations")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🤖 *TLC Sales Bot*\n\nWähle eine Auswertung:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    period = context.args[0] if context.args else "today"
    text = await build_report(period)
    await update.message.reply_text(text, parse_mode="Markdown")


async def build_report(period: str) -> str:
    start, end = get_date_range(period)
    label = {"today": "Heute", "week": "Diese Woche", "month": "Diesen Monat"}.get(period, "Heute")

    # Settings = Deals die zu "Setting Termin" bewegt wurden
    settings = api.get("opportunity", params={
        "status_id": "stat_JwsU6xiZ6FLOFj6L7OuQszArTHI1B7bnqWqlq5lG2eD",
        "date_updated__gte": start,
        "date_updated__lte": end,
        "_fields": "id,lead_name,status_label,date_updated,user_name"
    })
    settings_deals = settings.get("data", [])

    # Closings = Gewonnene Deals (beide Pipelines)
    won_neukunden = api.get("opportunity", params={
        "status_id": "stat_l7OrjaDo2dfydXbwNo17AsW5ZGKpclv0nZAkNQy1uow",
        "date_won__gte": start,
        "date_won__lte": end,
        "_fields": "id,value,lead_name,status_label,date_won,user_name"
    })
    won_sales = api.get("opportunity", params={
        "status_id": "stat_4PhCxgaZi75vBNZYnn5RQ4l8Fg21yLWATBwTWWVb2Ea",
        "date_won__gte": start,
        "date_won__lte": end,
        "_fields": "id,value,lead_name,status_label,date_won,user_name"
    })
    closings = won_neukunden.get("data", []) + won_sales.get("data", [])
    closing_value = sum((d.get("value") or 0) for d in closings) / 100

    # Verlorene Deals
    lost = api.get("opportunity", params={
        "status_type": "lost",
        "date_lost__gte": start,
        "date_lost__lte": end,
        "_fields": "id,lead_name,status_label"
    })
    lost_deals = lost.get("data", [])

    lines = [
        f"📊 *Sales Report — {label}*",
        f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        "",
        f"📅 *Settings:* {len(settings_deals)}",
        f"🏆 *Closings:* {len(closings)} ({format_currency(closing_value)})",
        f"❌ *Verloren:* {len(lost_deals)}",
    ]

    if settings_deals:
        lines.append("\n*Settings:*")
        for d in settings_deals[:8]:
            name = d.get("lead_name", "?")
            user = d.get("user_name", "")
            lines.append(f"  • {name} ({user})")

    if closings:
        lines.append("\n*Closings:*")
        for d in closings[:8]:
            name = d.get("lead_name", "?")
            val = format_currency((d.get("value") or 0) / 100)
            user = d.get("user_name", "")
            lines.append(f"  • {name}: {val} ({user})")

    return "\n".join(lines)


async def get_leads_stats() -> str:
    today = datetime.now()
    week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%dT00:00:00")

    # Leads nach Status
    result = api.get("lead", params={
        "date_created__gte": week_start,
        "_fields": "id,status_label,display_name"
    })
    leads = result.get("data", [])

    status_count: dict[str, int] = {}
    for lead in leads:
        status = lead.get("status_label", "Unbekannt")
        status_count[status] = status_count.get(status, 0) + 1

    lines = [
        "👥 *Lead-Statistiken (diese Woche)*",
        f"Gesamt neue Leads: *{len(leads)}*",
        ""
    ]
    for status, count in sorted(status_count.items(), key=lambda x: -x[1]):
        lines.append(f"  • {status}: {count}")

    return "\n".join(lines)


async def get_pipeline_overview() -> str:
    result = api.get("opportunity", params={
        "status_type": "active",
        "_fields": "id,value,lead_name,status_label,user_name"
    })
    opps = result.get("data", [])
    total_value = sum((o.get("value") or 0) for o in opps) / 100

    stage_data: dict[str, dict] = {}
    for o in opps:
        stage = o.get("status_label", "Unbekannt")
        if stage not in stage_data:
            stage_data[stage] = {"count": 0, "value": 0}
        stage_data[stage]["count"] += 1
        stage_data[stage]["value"] += (o.get("value") or 0) / 100

    lines = [
        "🔍 *Pipeline Übersicht*",
        f"Aktive Deals: *{len(opps)}* | Gesamtwert: *{format_currency(total_value)}*",
        ""
    ]
    for stage, data in stage_data.items():
        lines.append(f"  • {stage}: {data['count']} Deals ({format_currency(data['value'])})")

    return "\n".join(lines)


async def get_won_deals(period: str = "week") -> str:
    start, _ = get_date_range(period)
    result = api.get("opportunity", params={
        "status_type": "won",
        "date_won__gte": start,
        "_fields": "id,value,lead_name,status_label,date_won,user_name"
    })
    deals = result.get("data", [])
    total = sum((d.get("value") or 0) for d in deals) / 100

    lines = [f"✅ *Abgeschlossene Deals (diese Woche)*", f"Gesamt: *{format_currency(total)}*", ""]
    for d in deals[:15]:
        name = d.get("lead_name", "?")
        val = format_currency((d.get("value") or 0) / 100)
        user = d.get("user_name", "")
        date = d.get("date_won", "")[:10] if d.get("date_won") else ""
        lines.append(f"  • {name}: {val} ({user}) — {date}")

    return "\n".join(lines)


async def get_lost_deals(period: str = "week") -> str:
    start, _ = get_date_range(period)
    result = api.get("opportunity", params={
        "status_type": "lost",
        "date_lost__gte": start,
        "_fields": "id,value,lead_name,status_label,date_lost,loss_reason_label"
    })
    deals = result.get("data", [])

    lines = [f"❌ *Verlorene Deals (diese Woche)*", f"Anzahl: *{len(deals)}*", ""]
    for d in deals[:15]:
        name = d.get("lead_name", "?")
        reason = d.get("loss_reason_label", "Kein Grund")
        lines.append(f"  • {name} — {reason}")

    return "\n".join(lines)


async def get_conversation_analysis() -> str:
    # Letzte Aktivitäten/Calls abrufen
    today = datetime.now()
    week_start = (today - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00")

    result = api.get("activity/call", params={
        "date_created__gte": week_start,
        "_fields": "id,lead_name,note,duration,disposition,user_name,date_created"
    })
    calls = result.get("data", [])

    # Notizen abrufen
    notes_result = api.get("activity/note", params={
        "date_created__gte": week_start,
        "_fields": "id,lead_name,note,user_name,date_created"
    })
    notes = notes_result.get("data", [])

    # Analyse: Keywords für Besonderheiten
    keywords_positive = ["interessiert", "kaufen", "deal", "ja", "super", "perfekt", "abschluss", "vertrag", "zusage"]
    keywords_negative = ["kein interesse", "zu teuer", "nicht", "nein", "ablehnung", "konkurrenz", "später"]
    keywords_followup = ["rückruf", "follow up", "nachfassen", "termin", "demo", "angebot schicken"]

    positive_count = 0
    negative_count = 0
    followup_count = 0
    highlighted: list[str] = []

    all_activities = [(c, "Call") for c in calls] + [(n, "Notiz") for n in notes]

    for activity, atype in all_activities:
        text = (activity.get("note") or "").lower()
        if not text:
            continue

        pos = any(kw in text for kw in keywords_positive)
        neg = any(kw in text for kw in keywords_negative)
        fup = any(kw in text for kw in keywords_followup)

        if pos:
            positive_count += 1
        if neg:
            negative_count += 1
        if fup:
            followup_count += 1

        if pos or fup:
            lead = activity.get("lead_name", "?")
            user = activity.get("user_name", "")
            snippet = text[:80].replace("\n", " ")
            highlighted.append(f"  🟢 [{atype}] *{lead}* ({user})\n    _{snippet}..._")

    lines = [
        "💬 *Gesprächsanalyse (letzte 7 Tage)*",
        f"Calls analysiert: *{len(calls)}* | Notizen: *{len(notes)}*",
        "",
        f"🟢 Positiv-Signale: *{positive_count}*",
        f"🔴 Negativ-Signale: *{negative_count}*",
        f"🔵 Follow-up nötig: *{followup_count}*",
    ]

    if highlighted:
        lines.append("\n*Auffällige Gespräche:*")
        lines.extend(highlighted[:8])

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
        elif data == "deals_lost":
            text = await get_lost_deals("week")
        elif data == "leads":
            text = await get_leads_stats()
        elif data == "pipeline":
            text = await get_pipeline_overview()
        elif data == "conversations":
            text = await get_conversation_analysis()
        else:
            text = "Unbekannte Aktion."
    except Exception as e:
        logger.error(f"Error in button_handler: {e}")
        text = f"❌ Fehler beim Laden: {str(e)}"

    await loading_msg.delete()

    keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="main_menu")]]
    await query.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("📊 Tagesreport", callback_data="report_today"),
         InlineKeyboardButton("📅 Wochenreport", callback_data="report_week")],
        [InlineKeyboardButton("✅ Abgeschlossene Deals", callback_data="deals_won"),
         InlineKeyboardButton("❌ Verlorene Deals", callback_data="deals_lost")],
        [InlineKeyboardButton("👥 Lead-Statistiken", callback_data="leads"),
         InlineKeyboardButton("🔍 Pipeline", callback_data="pipeline")],
        [InlineKeyboardButton("💬 Gesprächsanalyse", callback_data="conversations")],
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
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CallbackQueryHandler(main_menu_handler, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Tagesreport: täglich um 08:00 Uhr
    job_queue: JobQueue = app.job_queue
    job_queue.run_daily(send_daily_report, time=datetime.strptime("08:00", "%H:%M").time())

    # Wochenreport: jeden Montag um 08:30 Uhr
    job_queue.run_daily(
        send_weekly_report,
        time=datetime.strptime("08:30", "%H:%M").time(),
        days=(0,)  # 0 = Montag
    )

    logger.info("Bot gestartet...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
