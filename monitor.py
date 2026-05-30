#!/usr/bin/env python3
"""
Annex (Way2Mail / Port25) dashboard monitor  ->  Telegram summary.

Logs into the Annex dashboards and reads, straight from each dashboard's
own JSON API (admin-dashboard-v2.php):

  * Emails sent      (today / this month)
  * Contacts uploaded(today / this month)
  * Domain blacklist status (per sender domain)

...then sends one tidy combined summary to Telegram.
"""

import argparse
import configparser
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

# Always show times in India time, regardless of where the script runs
# (your PC = local, GitHub cloud = UTC). This keeps the report consistent.
IST = timezone(timedelta(hours=5, minutes=30))

# The dashboards to monitor (all share one username/password).
DEFAULT_URLS = [
    "https://annex.postpanel.info/",
    "https://saj.postpanel.info/",
    "https://oatext.postpanel.in/",
    "https://annex.port25.app/",
    "https://saj.port25.app/",
    "https://oatext.port25.app/",
]

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


def load_config():
    cfg = configparser.ConfigParser(interpolation=None)
    if CONFIG_PATH.exists():
        cfg.read(CONFIG_PATH, encoding="utf-8")
    for sec in ("annex", "telegram", "run"):
        if not cfg.has_section(sec):
            cfg.add_section(sec)
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
    cfg["annex"].setdefault("base_url", "https://annex.port25.app/")
    cfg["run"].setdefault("mode", "single")
    cfg["run"].setdefault("interval_minutes", "60")
    if not cfg["annex"].get("username") or not cfg["annex"].get("password"):
        sys.exit("Missing Annex username/password. Set them in config.ini "
                 "or via ANNEX_USERNAME / ANNEX_PASSWORD environment variables.")
    return cfg


def get_sites(cfg):
    raw = os.environ.get("ANNEX_URLS") or cfg["annex"].get("urls", "")
    urls = [u.strip() for u in re.split(r"[,\s]+", raw) if u.strip()]
    return urls or list(DEFAULT_URLS)


def site_label(url):
    return urlparse(url).netloc or url


class Annex:
    def __init__(self, base_url, user, pwd):
        self.base = base_url.rstrip("/") + "/"
        self.user = user
        self.pwd = pwd
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


def n(x):
    try:
        return f"{int(x):,}"
    except (TypeError, ValueError):
        return str(x if x is not None else "n/a")


def build_site_section(idx, label, ds, health):
    today = (ds or {}).get("today", {})
    month = (ds or {}).get("month", {})
    L = [f"<b>{idx}.</b> <code>{label}</code>"]
    L.append(f"   \U0001F4E4 Sent:  <b>{n(today.get('emailsSent'))}</b>  "
             f"<i>(month {n(month.get('emailsSent'))})</i>")
    L.append(f"   \U0001F4E5 Uploaded:  <b>{n(today.get('contactsUploaded'))}</b>  "
             f"<i>(month {n(month.get('contactsUploaded'))})</i>")
    domains = (health or {}).get("domains") or []
    if (health or {}).get("error"):
        L.append(f"   \U0001F6E1 Blacklist:  ! {health['error']}")
    elif not domains:
        L.append("   \U0001F6E1 Blacklist:  no domains")
    else:
        flagged = []
        for d in domains:
            status = (d.get("blacklist_status") or "unknown").lower()
            if status == "pass":
                continue
            icon = {"warning": "⚠️", "unknown": "❓"}.get(status, "\U0001F6D1")
            sev = d.get("blacklist_severity") or d.get("blacklist_message") or status
            flagged.append((icon, d.get("domain", "?"), sev))
        if not flagged:
            L.append(f"   \U0001F6E1 Blacklist:  ✅ all clean ({len(domains)})")
        elif len(flagged) == 1:
            icon, dom, sev = flagged[0]
            L.append(f"   \U0001F6E1 Blacklist:  {icon} {dom} — {sev}")
        else:
            L.append(f"   \U0001F6E1 Blacklist:  {len(flagged)} flagged")
            for icon, dom, sev in flagged:
                L.append(f"        {icon} {dom} — {sev}")
    return "\n".join(L)


def collect_site(idx, url, user, pwd):
    label = site_label(url)
    try:
        a = Annex(url, user, pwd)
        a.login()
        ds = a.date_summary()
        health = a.domain_health()
        return label, build_site_section(idx, label, ds, health)
    except Exception as e:
        return label, f"<b>{idx}.</b> <code>{label}</code>\n   ⚠️ could not read: {e}"


def send_telegram(cfg, text):
    token = cfg["telegram"]["bot_token"].strip()
    chat_id = cfg["telegram"]["chat_id"].strip()
    if not token or token.startswith("PUT_") or not chat_id or chat_id.startswith("PUT_"):
        print("[telegram] not configured - printing instead:\n")
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


def run_once(cfg, send=True, raw=False):
    user = cfg["annex"]["username"]
    pwd = cfg["annex"]["password"]
    sites = get_sites(cfg)
    if raw:
        import json
        for url in sites:
            a = Annex(url, user, pwd)
            a.login()
            print(f"===== {site_label(url)} =====")
            print(json.dumps({"date_summary": a.date_summary(),
                              "activity_stats": a.activity_stats(to_date=str(date.today())),
                              "domain_health": a.domain_health()}, indent=2))
        return
    ts = datetime.now(IST).strftime("%d %b %Y  ·  %I:%M %p")
    header = (f"\U0001F4CA <b>ANNEX HOURLY REPORT</b>\n"
              f"\U0001F5D3 {ts} IST  ·  {len(sites)} portals\n"
              f"━━━━━━━━━━━━━━━━━━━━")
    sections = [header]
    for i, url in enumerate(sites, 1):
        _, section = collect_site(i, url, user, pwd)
        sections.append(section)
    text = "\n\n".join(sections)
    if send:
        send_telegram(cfg, text)
    else:
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
