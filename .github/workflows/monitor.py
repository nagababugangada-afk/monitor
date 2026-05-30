#!/usr/bin/env python3
"""
Annex (Way2Mail / Port25) dashboard monitor  ->  Telegram summary.

Logs into https://annex.port25.app/ and reads, straight from the dashboard's
own JSON API (admin-dashboard-v2.php):

  * Emails sent      (today / yesterday / this month)
  * Contacts uploaded(today / yesterday / this month)
  * Domain blacklist status (per sender domain)

...then sends a tidy summary to your Telegram bot.

Usage:
    python monitor.py            # run once: fetch stats + send Telegram summary
    python monitor.py --no-send  # fetch + print only (no Telegram)
    python monitor.py --loop     # keep running, send every interval_minutes
    python monitor.py --raw      # dump the raw JSON from each endpoint (debug)

Auth: the site uses a normal PHP session cookie. We GET the login page, POST
username+password to it, and the session cookie then authorises the API calls.
"""

import argparse
import configparser
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

# Windows consoles default to cp1252 and can't print emoji; force UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.ini"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# -----------------------------------------------------------------------------
# config + session
# -----------------------------------------------------------------------------
def load_config():
    """
    Load settings from config.ini if present, then overlay any environment
    variables. This lets the SAME script run:
      * locally  -> values come from config.ini
      * in the cloud (GitHub Actions) -> values come from encrypted secrets
        passed in as env vars, with no config.ini needed.
    """
    cfg = configparser.ConfigParser(interpolation=None)
    if CONFIG_PATH.exists():
        cfg.read(CONFIG_PATH, encoding="utf-8")
    for sec in ("annex", "telegram", "run"):
        if not cfg.has_section(sec):
            cfg.add_section(sec)

    # env vars win over config.ini (used by GitHub Actions secrets)
    env_map = {
        ("annex", "base_url"): "ANNEX_BASE_URL",
        ("annex", "username"): "ANNEX_USERNAME",
        ("annex", "password"): "ANNEX_PASSWORD",
        ("telegram", "bot_token"): "TELEGRAM_BOT_TOKEN",
        ("telegram", "chat_id"): "TELEGRAM_CHAT_ID",
    }
    for (sec, key), env_name in env_map.items():
        val = os.environ.get(env_name)
        if val:
            cfg[sec][key] = val

    # sensible defaults
    cfg["annex"].setdefault("base_url", "https://annex.port25.app/")
    cfg["run"].setdefault("mode", "single")
    cfg["run"].setdefault("interval_minutes", "60")

    if not cfg["annex"].get("username") or not cfg["annex"].get("password"):
        sys.exit("Missing Annex username/password. Set them in config.ini "
                 "or via ANNEX_USERNAME / ANNEX_PASSWORD environment variables.")
    return cfg


class Annex:
    def __init__(self, cfg):
        self.base = cfg["annex"]["base_url"].rstrip("/") + "/"
        self.user = cfg["annex"]["username"]
        self.pwd = cfg["annex"]["password"]
        self.api_url = self.base + "admin-dashboard-v2.php"
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT,
                               "X-Requested-With": "XMLHttpRequest"})

    def login(self):
        self.s.get(self.base, timeout=30).raise_for_status()
        r = self.s.post(self.base,
                        data={"username": self.user, "password": self.pwd},
                        headers={"Referer": self.base}, timeout=30)
        r.raise_for_status()
        body = r.text.strip().lower()
        if "success" in body:
            return True
        # map the site's known failure responses to friendly text
        reason = {
            "inactive": "account inactive / not activated",
            "sub_inactive": "sub-account inactive",
            "noallow": "access not allowed at this time",
        }.get(body, "invalid credentials")
        raise RuntimeError(f"Login failed: {reason} (server said: {r.text.strip()!r})")

    def api(self, payload):
        r = self.s.post(self.api_url, data=payload,
                        headers={"Referer": self.base}, timeout=60)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            raise RuntimeError(f"Endpoint {payload} did not return JSON: {r.text[:200]!r}")

    # --- the three things we care about ---
    def date_summary(self):
        return self.api({"get_date_summary": 1, "period": "all"})

    def activity_stats(self, from_date=None, to_date=None):
        p = {"get_activity_stats": 1}
        if from_date:
            p["from_date"] = from_date
        if to_date:
            p["to_date"] = to_date
        return self.api(p)

    def domain_health(self):
        return self.api({"get_domain_health": 1})


# -----------------------------------------------------------------------------
# formatting
# -----------------------------------------------------------------------------
def n(x):
    """Format a number with thousands separators, tolerant of None/strings."""
    try:
        return f"{int(x):,}"
    except (TypeError, ValueError):
        return str(x if x is not None else "n/a")


def build_summary(ds, stats, health):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    L = [f"\U0001F4E8 <b>Annex Daily Summary</b>  ({ts})", ""]

    today = (ds or {}).get("today", {})
    yest = (ds or {}).get("yesterday", {})
    month = (ds or {}).get("month", {})

    L.append("<b>Emails sent</b>")
    L.append(f"  • Today: <b>{n(today.get('emailsSent'))}</b>")
    L.append(f"  • Yesterday: {n(yest.get('emailsSent'))}")
    L.append(f"  • This month: {n(month.get('emailsSent'))}")
    L.append("")

    L.append("<b>Contacts uploaded</b>")
    L.append(f"  • Today: <b>{n(today.get('contactsUploaded'))}</b>")
    L.append(f"  • Yesterday: {n(yest.get('contactsUploaded'))}")
    L.append(f"  • This month: {n(month.get('contactsUploaded'))}")
    L.append("")

    if stats:
        L.append("<b>Today's delivery</b>")
        L.append(f"  • Sent: {n(stats.get('total_sent'))} | "
                 f"Delivered: {n(stats.get('delivered'))} | "
                 f"Pending: {n(stats.get('pending_count'))}")
        L.append(f"  • Bounces: {n(stats.get('total_bounces'))} "
                 f"(hard {n(stats.get('hard_bounces'))} / soft {n(stats.get('soft_bounces'))})")
        L.append("")

    # --- blacklist status ---
    L.append("<b>Domain blacklist status</b>")
    domains = (health or {}).get("domains") or []
    if (health or {}).get("error"):
        L.append(f"  ! {health['error']}")
    elif not domains:
        L.append("  • No sender domains configured.")
    else:
        bad = []
        for d in domains:
            status = (d.get("blacklist_status") or "unknown").lower()
            name = d.get("domain", "?")
            if status == "pass":
                continue  # clean - only list problems below
            icon = {"warning": "⚠️", "unknown": "❓"}.get(status, "\U0001F6D1")
            msg = d.get("blacklist_message") or status
            bad.append(f"  {icon} <b>{name}</b>: {msg}")
        clean = len(domains) - len(bad)
        L.append(f"  • {clean}/{len(domains)} domain(s) clean.")
        if bad:
            L.append("  <b>Flagged:</b>")
            L.extend(bad)
        else:
            L.append("  ✅ All domains clean.")

    return "\n".join(L)


# -----------------------------------------------------------------------------
# telegram
# -----------------------------------------------------------------------------
def send_telegram(cfg, text):
    token = cfg["telegram"]["bot_token"].strip()
    chat_id = cfg["telegram"]["chat_id"].strip()
    if not token or token.startswith("PUT_") or not chat_id or chat_id.startswith("PUT_"):
        print("[telegram] not configured - printing instead:\n")
        # strip the simple HTML tags for console readability
        import re
        print(re.sub(r"</?b>", "", text))
        return False
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat_id, "text": text,
                            "parse_mode": "HTML", "disable_web_page_preview": True},
                      timeout=30)
    if r.ok and r.json().get("ok"):
        print("[telegram] summary sent.")
        return True
    print(f"[telegram] send FAILED: {r.status_code} {r.text}")
    return False


# -----------------------------------------------------------------------------
# run
# -----------------------------------------------------------------------------
def run_once(cfg, send=True, raw=False):
    a = Annex(cfg)
    a.login()
    ds = a.date_summary()
    stats = a.activity_stats(to_date=str(date.today()))  # today's figures
    health = a.domain_health()

    if raw:
        import json
        print(json.dumps({"date_summary": ds, "activity_stats": stats,
                          "domain_health": health}, indent=2))
        return

    text = build_summary(ds, stats, health)
    if send:
        send_telegram(cfg, text)
    else:
        import re
        print(re.sub(r"</?b>", "", text))


def main():
    ap = argparse.ArgumentParser(description="Annex dashboard -> Telegram monitor")
    ap.add_argument("--loop", action="store_true", help="run repeatedly every interval_minutes")
    ap.add_argument("--no-send", action="store_true", help="print only, don't send to Telegram")
    ap.add_argument("--raw", action="store_true", help="dump raw JSON from each endpoint")
    args = ap.parse_args()

    cfg = load_config()
    send = not args.no_send
    looping = args.loop or cfg["run"].get("mode", "single").lower() == "loop"

    if not looping:
        run_once(cfg, send=send, raw=args.raw)
        return

    interval = int(cfg["run"].get("interval_minutes", "60"))
    print(f"[loop] running every {interval} min. Ctrl+C to stop.")
    while True:
        try:
            run_once(cfg, send=send, raw=args.raw)
        except Exception as e:
            print(f"[loop] error this cycle: {e}")
        time.sleep(interval * 60)


if __name__ == "__main__":
    main()
