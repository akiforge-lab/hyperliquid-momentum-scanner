# -*- coding: utf-8 -*-
"""
Telegram sender -- env-driven, optional.

If TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are missing, send_message logs
a warning and returns False.  This lets the script run safely on first
deployment without credentials (e.g. for dry-runs).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

from .config import (
    API_TIMEOUT_SEC,
    TELEGRAM_BOT_TOKEN_ENV,
    TELEGRAM_CHAT_ID_ENV,
)

logger = logging.getLogger(__name__)


def _creds() -> tuple[Optional[str], Optional[str]]:
    return (
        os.environ.get(TELEGRAM_BOT_TOKEN_ENV),
        os.environ.get(TELEGRAM_CHAT_ID_ENV),
    )


def send_message(text: str) -> bool:
    """
    POST text to Telegram.  Returns True on 200, False otherwise (incl. no creds).
    HTML parse mode is used so callers may include <b>, <code>, etc.
    """
    token, chat_id = _creds()
    if not token or not chat_id:
        logger.warning(
            "Telegram credentials not set (%s / %s) -- skipping send.",
            TELEGRAM_BOT_TOKEN_ENV, TELEGRAM_CHAT_ID_ENV,
        )
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id":    chat_id,
                "text":       text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=API_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            logger.warning(
                "Telegram send failed: HTTP %d -- %s",
                resp.status_code, resp.text[:200],
            )
            return False
        return True
    except Exception as exc:
        logger.warning("Telegram send raised: %s", exc)
        return False
