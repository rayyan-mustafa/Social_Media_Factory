"""Notification Dispatcher for multi-channel alerts (Telegram, Gmail, WhatsApp)."""

import os
import aiohttp
from email.message import EmailMessage
from src.core.logging import get_logger

logger = get_logger(__name__)

async def dispatch_ledger(title: str, ledger: dict) -> None:
    """Dispatches a formatted ledger to all configured channels."""
    
    telegram_token = os.getenv("TELEGRAM_TOKEN")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    gmail_user = os.getenv("GMAIL_USER")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")
    
    message = f"🚨 {title} 🚨\n\n"
    for k, v in ledger.items():
        message += f"{k}: {v}\n"
        
    logger.info(
        "notification_dispatcher_triggered", 
        extra={"title": title, "ledger_keys": list(ledger.keys())}
    )
    
    # Telegram Dispatch
    if telegram_token and telegram_chat_id:
        try:
            url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
            payload = {"chat_id": telegram_chat_id, "text": message}
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        logger.info("telegram_alert_sent", extra={"destination": telegram_chat_id})
                    else:
                        logger.error("telegram_alert_failed", extra={"status": resp.status, "text": await resp.text()})
        except Exception as e:
            logger.error("telegram_alert_error", extra={"error": str(e)})
    else:
        logger.warning("telegram_not_configured", extra={"msg": "Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID in .env"})
        
    # Gmail Dispatch
    if gmail_user and gmail_password:
        try:
            # We import here so if aiosmtplib is missing, it doesn't break the whole app unless configured
            import aiosmtplib
            
            msg = EmailMessage()
            msg.set_content(message)
            msg["Subject"] = f"Automation Factory Alert: {title}"
            msg["From"] = gmail_user
            msg["To"] = gmail_user
            
            await aiosmtplib.send(
                msg,
                hostname="smtp.gmail.com",
                port=465,
                use_tls=True,
                username=gmail_user,
                password=gmail_password
            )
            logger.info("gmail_alert_sent", extra={"destination": gmail_user})
        except Exception as e:
            logger.error("gmail_alert_error", extra={"error": str(e)})
    else:
        logger.warning("gmail_not_configured", extra={"msg": "Missing GMAIL_USER or GMAIL_APP_PASSWORD in .env"})
