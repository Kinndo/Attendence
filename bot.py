#!/usr/bin/env python3
"""
Attendance poll bot.

Watches a Microsoft Teams chat (Teams on the web) for the teacher's
"poll 1" / "poll 2" message, then:

  1. reloads the H&R Block LMT course page and clicks
     "Acknowledge Attendance Poll" next to the Join button,
  2. posts "done" back into the Teams chat.

Everything runs inside one browser (Edge by default) with its own saved
profile, so you log in to Teams and the LMT site once by hand and the
bot stays signed in after that.

Modes:
  python bot.py                 run the watcher (normal use)
  python bot.py --dry-run       watch and log, but never click or post
  python bot.py --test-teams    print what the bot can read from the chat
  python bot.py --test-site     check whether the acknowledge button exists
  python bot.py --dump          save HTML + screenshots of both tabs to debug/
"""

import argparse
import hashlib
import json
import logging
import logging.handlers
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "config.json"

# --------------------------------------------------------------------------
# Selectors. These are tried in order; the first one that finds something
# wins. If Teams changes its page layout, add the new selector to
# config.json ("teams_message_selectors") instead of editing this file.
# --------------------------------------------------------------------------
DEFAULT_MESSAGE_SELECTORS = [
    '[data-tid="chat-pane-message"]',
    '[data-tid="channel-pane-message"]',
    '[data-tid="chat-pane-item"]',
    'div[id^="content-"]',
    '[role="listitem"]',
]
DEFAULT_COMPOSE_SELECTORS = [
    '[data-tid="ckeditor"] [contenteditable="true"]',
    'div[role="textbox"][contenteditable="true"]',
    '[data-tid="ckeditor"]',
    'div[contenteditable="true"]',
]

# JavaScript that pulls every visible message out of the Teams page in one
# round trip. Returns a list of {key, author, text, ts}.
EXTRACT_MESSAGES_JS = """
(selectors) => {
  let nodes = [];
  for (const sel of selectors) {
    try { nodes = Array.from(document.querySelectorAll(sel)); } catch (e) { nodes = []; }
    if (nodes.length) break;
  }
  return nodes.map(n => {
    const authorEl = n.querySelector(
      '[data-tid="message-author-name"], [data-tid*="author"], [class*="author"]');
    const timeEl = n.querySelector('time[datetime]');
    const contentEl = n.querySelector(
      '[id^="content-"], [data-tid="chat-pane-message-content"], [class*="message-body"]');
    const text = ((contentEl || n).innerText || '').trim();
    const author = authorEl ? (authorEl.innerText || '').trim() : '';
    const ts = timeEl ? (timeEl.getAttribute('datetime') || '') : '';
    let key = n.id || n.getAttribute('data-mid') || n.getAttribute('data-message-id') || '';
    if (!key && contentEl && contentEl.id) key = contentEl.id;
    return { key, author, text, ts };
  });
}
"""


@dataclass
class Config:
    course_url: str
    teams_chat_url: str = ""
    trigger_pattern: str = r"\bpoll\b\s*(\d+)?"
    trigger_sender: str = ""
    done_message: str = "done"
    display_name: str = ""
    auto_join: bool = False
    hello_message: str = ""
    join_wait_minutes: float = 30
    meeting_link_from_site: bool = True   # click Join on the course page to get the meeting link
    meeting_link_pattern: str = r"teams\."  # what the popped-up link must contain
    join_at: str = ""            # e.g. "21:00": wait until then, join, and repeat on class days
    class_days: list = None      # e.g. ["Mon", "Wed", "Fri"]; empty means every day
    session_hours: float = 3     # how long after join_at a late start still joins
    goodbye_pattern: str = r"\b(good\s*bye|bye+|good\s*night|see (you|ya|y'all)|take care|have a good (one|night))\b"
    goodbye_threshold: int = 5
    goodbye_window_minutes: float = 5
    goodbye_message: str = "Goodbye"
    leave_after_goodbye: bool = True
    check_interval_seconds: float = 5
    cooldown_minutes: float = 2
    ack_button_pattern: str = r"acknowledge.*attendance.*poll"
    ack_retry_seconds: float = 600
    ack_retry_interval_seconds: float = 15
    require_ack_for_done: bool = True
    keepalive_minutes: float = 5
    browser_channel: str = "msedge"
    browser_executable: str = ""
    data_dir: str = ""
    teams_message_selectors: list = None
    teams_compose_selectors: list = None
    dry_run: bool = False

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            sys.exit(
                f"Config file not found: {path}\n"
                f"Copy config.example.json to config.json and fill it in."
            )
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        cfg = cls(**known)
        if not cfg.course_url:
            sys.exit("config.json needs course_url.")
        if not cfg.teams_chat_url and not cfg.meeting_link_from_site:
            sys.exit("config.json needs teams_chat_url, or meeting_link_from_site set to true.")
        if re.sub(r"/+$", "", cfg.course_url).lower().endswith("/lmt"):
            sys.exit(
                "course_url in config.json is still the placeholder (it ends in /lmt/, which the "
                "site answers with a 404).\n"
                "Sign in to the LMT site, open your course so you see 'Upcoming Sessions', and "
                "paste the whole address bar into course_url. It starts with\n"
                "  https://lab.hrblock.com/lmt/clmsCourseDetails.prMain?in_sessionid=..."
            )
        cfg.teams_message_selectors = (
            cfg.teams_message_selectors or DEFAULT_MESSAGE_SELECTORS
        )
        cfg.teams_compose_selectors = (
            cfg.teams_compose_selectors or DEFAULT_COMPOSE_SELECTORS
        )
        cfg.class_days = cfg.class_days or []
        if cfg.join_at:
            try:
                parse_hhmm(cfg.join_at)
            except ValueError:
                sys.exit(f"join_at must look like 21:00 (24-hour clock); got {cfg.join_at!r}")
            cfg.auto_join = True  # a scheduled join is an automatic join
        if not cfg.data_dir:
            base = os.environ.get("LOCALAPPDATA") or str(Path.home())
            cfg.data_dir = str(Path(base) / "AttendanceBot")
        return cfg


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def setup_logging(data_dir: Path) -> logging.Logger:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("bot")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(logging.INFO)
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    rotating = logging.handlers.RotatingFileHandler(
        log_dir / "bot.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    rotating.setFormatter(fmt)
    logger.addHandler(rotating)
    return logger


def alert_user(log: logging.Logger, message: str) -> None:
    """Something needs a human. Log loudly and beep on Windows."""
    log.warning("ACTION NEEDED: %s", message)
    if sys.platform.startswith("win"):
        try:
            import winsound

            for _ in range(3):
                winsound.Beep(880, 250)
                time.sleep(0.1)
        except Exception:
            pass


class SeenStore:
    """Remembers message keys already handled, across restarts."""

    def __init__(self, path: Path, cap: int = 5000):
        self.path = path
        self.cap = cap
        self.keys: list[str] = []
        self.set: set[str] = set()
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self.keys = json.load(fh).get("seen", [])
                self.set = set(self.keys)
            except Exception:
                self.keys, self.set = [], set()

    def __contains__(self, key: str) -> bool:
        return key in self.set

    def add(self, key: str) -> None:
        if key in self.set:
            return
        self.keys.append(key)
        self.set.add(key)
        if len(self.keys) > self.cap:
            dropped = self.keys[: len(self.keys) - self.cap]
            self.keys = self.keys[len(dropped):]
            self.set.difference_update(dropped)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"seen": self.keys}, fh)
        tmp.replace(self.path)


def parse_ts(value: str):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_hhmm(value: str):
    hh, mm = value.strip().split(":")
    hh, mm = int(hh), int(mm)
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(value)
    return hh, mm


def is_class_day(day: datetime, class_days: list) -> bool:
    if not class_days:
        return True
    wanted = {d.strip().lower()[:3] for d in class_days}
    return DAY_NAMES[day.weekday()] in wanted


def next_join_time(cfg: Config, now: datetime, skip_date: str = "") -> datetime:
    """
    When to join next. If today is a class day and we are within
    session_hours after join_at, that is now. Otherwise the next class day
    at join_at. A day equal to skip_date (YYYY-MM-DD) is passed over, used
    after a class we already attended and left.
    """
    hh, mm = parse_hhmm(cfg.join_at)
    for offset in range(0, 15):
        day = (now + timedelta(days=offset)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if not is_class_day(day, cfg.class_days):
            continue
        if skip_date and day.strftime("%Y-%m-%d") == skip_date:
            continue
        if day <= now <= day + timedelta(hours=cfg.session_hours):
            return now
        if day > now:
            return day
    return now + timedelta(days=1)


def stable_key(msg: dict) -> str:
    if msg.get("key"):
        return msg["key"]
    raw = "|".join([msg.get("author", ""), msg.get("ts", ""), msg.get("text", "")])
    return "h:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def direct_teams_url(url: str) -> str:
    """
    Turn a Teams meeting link into the address the web app itself uses, so the
    browser never shows the launcher page. The launcher is what triggers
    Edge's "This site is trying to open Microsoft Teams" popup, which no
    script can click away.

      https://teams.live.com/meet/123?p=abc
        -> https://teams.live.com/_#/meet/123?p=abc&anon=true
      https://teams.live.com/dl/launcher/launcher.html?url=%2F_%23%2Fmeet...
        -> https://teams.live.com/_#/meet...   (the launcher's own target)
      https://teams.microsoft.com/l/meetup-join/...
        -> https://teams.microsoft.com/_#/l/meetup-join/...&anon=true
    Anything else is returned unchanged.
    """
    try:
        u = urlparse(url)
    except ValueError:
        return url
    host = (u.netloc or "").lower()
    if "teams" not in host:
        return url
    origin = f"{u.scheme}://{u.netloc}"
    if "launcher" in u.path.lower():
        target = parse_qs(u.query).get("url", [""])[0]
        if target.startswith("/"):
            return origin + target
        return url
    path = u.path or ""
    if path.startswith("/meet/") or path.startswith("/l/"):
        q = parse_qs(u.query, keep_blank_values=True)
        q.setdefault("anon", ["true"])
        flat = {k: v[0] for k, v in q.items()}
        return f"{origin}/_#{path}?{urlencode(flat)}"
    return url


# --------------------------------------------------------------------------
# Teams side
# --------------------------------------------------------------------------
class Teams:
    LAUNCHER_LABELS = (
        "Continue on this browser",
        "Use the web app instead",
        "Join on the web instead",
        "Join from this browser",
    )
    GUEST_LABELS = ("Join as a guest", "Join as guest", "Continue without an account")
    PREJOIN_DISMISS_LABELS = (
        "Continue without audio or video",
        "Continue without audio",
        "Continue without video",
    )

    def __init__(self, page: Page, cfg: Config, log: logging.Logger):
        self.page = page
        self.cfg = cfg
        self.log = log
        self.trigger_re = re.compile(cfg.trigger_pattern, re.IGNORECASE)
        self._last_launcher_click = 0.0
        # Teams sometimes opens the web app in a new tab. Follow it.
        page.context.on("page", self._on_new_page)

    def _on_new_page(self, new_page: Page) -> None:
        try:
            new_page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except PlaywrightError:
            pass
        url = new_page.url or ""
        # Browser-internal pages (edge://permission-request-dialog/ and the
        # like) show up here too. Only follow a real Teams web page.
        if not url.startswith(("http://", "https://", "file://")) or "teams" not in url.lower() and not url.startswith("file://"):
            self.log.debug("Ignoring new non-Teams page: %s", url[:80])
            return
        self.log.info("Teams opened a new tab (%s); following it.", url[:80])
        old = self.page
        self.page = new_page
        try:
            if not old.is_closed() and "teams" not in (old.url or ""):
                old.close()
        except PlaywrightError:
            pass

    def open(self, url: str = "") -> None:
        source = url or self.cfg.teams_chat_url
        url = direct_teams_url(source)
        if url != source:
            self.log.info("Skipping the Teams launcher page; opening the web app directly.")
        self.log.info("Opening Teams: %s", url)
        self.page.goto(url, wait_until="domcontentloaded")
        self._dismiss_open_in_app()

    def _click_label(self, labels, what: str) -> bool:
        """Click the first visible button/link whose text matches one of labels."""
        for label in labels:
            pat = re.compile(re.escape(label), re.I)
            for frame in self.page.frames:
                for finder in (
                    lambda f: f.get_by_role("button", name=pat),
                    lambda f: f.get_by_role("link", name=pat),
                    lambda f: f.get_by_text(pat),
                ):
                    try:
                        loc = finder(frame).first
                        if loc.count() and loc.is_visible():
                            loc.click()
                            self.log.info("Clicked '%s' (%s).", label, what)
                            return True
                    except PlaywrightError:
                        continue
        return False

    def _dismiss_open_in_app(self) -> None:
        """The 'Open Microsoft Teams?' launcher page: stay in the browser.
        Clicked at most once every 20 seconds so it cannot loop."""
        if time.time() - self._last_launcher_click < 20:
            return
        current = self.page.url or ""
        if "launcher" in current.lower():
            target = direct_teams_url(current)
            if target != current:
                self._last_launcher_click = time.time()
                self.log.info("On the launcher page; going straight to the web app instead.")
                try:
                    self.page.goto(target, wait_until="domcontentloaded")
                except PlaywrightError as exc:
                    self.log.warning("Could not leave the launcher page: %s", str(exc).splitlines()[0])
                return
        if self._click_label(self.LAUNCHER_LABELS, "stay in the browser"):
            self._last_launcher_click = time.time()
            time.sleep(3)
        if self._click_label(self.GUEST_LABELS, "join as guest"):
            time.sleep(2)

    # ---- meeting join -------------------------------------------------
    def _visible(self, locator):
        try:
            if locator.count() and locator.first.is_visible():
                return locator.first
        except PlaywrightError:
            pass
        return None

    def _find_in_frames(self, make_locator):
        for frame in self.page.frames:
            try:
                loc = self._visible(make_locator(frame))
            except PlaywrightError:
                loc = None
            if loc is not None:
                return loc
        return None

    def find_name_box(self):
        name_re = re.compile(r"name", re.I)
        return (
            self._find_in_frames(
                lambda f: f.locator(
                    'input[placeholder*="name" i], input[aria-label*="name" i], '
                    'input[data-tid*="name" i], input[id*="name" i]'
                )
            )
            or self._find_in_frames(lambda f: f.get_by_role("textbox", name=name_re))
            or self._find_in_frames(lambda f: f.locator('input[type="text"]:not([readonly])'))
        )

    def _dismiss_prejoin_dialogs(self) -> bool:
        """Teams' 'Are you sure you don't want audio or video?' box and friends."""
        return self._click_label(self.PREJOIN_DISMISS_LABELS, "continue without devices")

    def find_join_button(self):
        join_re = re.compile(r"^\s*join( now| meeting)?\s*$", re.I)
        return self._find_in_frames(lambda f: f.get_by_role("button", name=join_re))

    def is_goodbye(self, msg: dict) -> bool:
        if not self.cfg.goodbye_pattern:
            return False
        if self.cfg.display_name and self.cfg.display_name.lower() in msg.get("author", "").lower():
            return False  # our own message
        return re.search(self.cfg.goodbye_pattern, msg.get("text", ""), re.I) is not None

    def find_leave_button(self):
        leave_re = re.compile(r"^\s*(leave|leave meeting|hang up)\b", re.I)
        return self._find_in_frames(lambda f: f.get_by_role("button", name=leave_re))

    def leave_meeting(self) -> bool:
        if self.cfg.dry_run:
            self.log.info("[dry-run] Would click Leave now.")
            return True
        btn = self.find_leave_button()
        if btn is None:
            self.log.warning("No Leave button found; leave the meeting by hand.")
            return False
        try:
            btn.click()
        except PlaywrightError as exc:
            self.log.warning("Clicking Leave failed: %s", str(exc).splitlines()[0])
            return False
        # Some layouts open a small menu (Leave / End meeting). Pick Leave.
        time.sleep(1.5)
        confirm_re = re.compile(r"^\s*leave( meeting)?\s*$", re.I)
        for frame in self.page.frames:
            for role in ("menuitem", "button"):
                try:
                    loc = frame.get_by_role(role, name=confirm_re).first
                    if loc.count() and loc.is_visible():
                        loc.click()
                        break
                except PlaywrightError:
                    continue
        self.log.info("Left the meeting.")
        return True

    def find_chat_button(self):
        chat_re = re.compile(r"\b(chat|conversation)\b", re.I)
        return self._find_in_frames(lambda f: f.get_by_role("button", name=chat_re))

    def _enter_name(self, name_box) -> None:
        name = self.cfg.display_name
        try:
            try:
                name_box.click(timeout=5_000)
            except PlaywrightError:
                pass
            name_box.fill(name)
            if (name_box.input_value() or "").strip() != name:
                name_box.fill("")
                name_box.press_sequentially(name, delay=40)
            self.log.info("Entered display name %r.", name)
        except PlaywrightError as exc:
            self.log.warning("Could not type the display name: %s", str(exc).splitlines()[0])

    def _switch_off_devices(self) -> None:
        """Turn the camera and mic toggles off on the pre-join screen, if present."""
        dev_re = re.compile(r"(camera|video|mic|microphone|audio)", re.I)
        for frame in self.page.frames:
            try:
                switches = frame.get_by_role("switch", name=dev_re)
                for i in range(switches.count()):
                    sw = switches.nth(i)
                    if sw.is_visible() and (sw.get_attribute("aria-checked") or "").lower() == "true":
                        sw.click()
                        self.log.info("Turned off: %s", sw.get_attribute("aria-label") or "a device toggle")
            except PlaywrightError:
                continue

    def ensure_chat_open(self) -> bool:
        """Make sure the chat panel (with its message box) is showing."""
        if self.find_compose_box() is not None:
            return True
        btn = self.find_chat_button()
        if btn is None:
            return False
        try:
            btn.click()
            self.log.info("Opened the Chat panel.")
            time.sleep(2)
        except PlaywrightError:
            return False
        return self.find_compose_box() is not None

    def join_meeting(self) -> bool:
        """Fill in the display name and click Join. Returns True once in the meeting."""
        if not self.cfg.auto_join:
            return False
        # Pre-join screen: name box and Join button. Give it up to 2 minutes to appear.
        deadline = time.time() + 120
        name_box = None
        while time.time() < deadline:
            self._dismiss_open_in_app()
            if self._dismiss_prejoin_dialogs():
                time.sleep(2)
            if self.find_compose_box() is not None:
                self.log.info("Already in the meeting with the chat open.")
                return True
            name_box = self.find_name_box()
            if name_box is not None or self.find_join_button() is not None:
                break
            time.sleep(2)
        if name_box is not None and self.cfg.display_name:
            self._enter_name(name_box)
        self._switch_off_devices()
        btn = self.find_join_button()
        if btn is None:
            self.log.warning("No Join button found. Join the meeting by hand in the first tab.")
            return False
        # Join stays greyed out until Teams accepts the name. Give it a moment.
        for _ in range(15):
            try:
                if btn.is_enabled():
                    break
            except PlaywrightError:
                pass
            self._dismiss_prejoin_dialogs()
            nb = self.find_name_box()
            if nb is not None and self.cfg.display_name:
                try:
                    if (nb.input_value() or "").strip() != self.cfg.display_name:
                        self._enter_name(nb)
                except PlaywrightError:
                    pass
            time.sleep(1)
            btn = self.find_join_button() or btn
        if self.cfg.dry_run:
            self.log.info("[dry-run] Would click Join now.")
        try:
            btn.click(timeout=15_000)
            self.log.info("Clicked Join. Waiting to be admitted...")
        except PlaywrightError as exc:
            self.log.warning(
                "Clicking Join failed (%s). If the Join button is greyed out, type the name "
                "by hand in the first tab and click Join yourself.",
                str(exc).splitlines()[0],
            )
            return False
        # Lobby: wait until the in-meeting toolbar (Chat button) or chat box shows up.
        deadline = time.time() + self.cfg.join_wait_minutes * 60
        last_note = 0.0
        while time.time() < deadline:
            if self.ensure_chat_open():
                self.log.info("In the meeting; chat panel is open.")
                return True
            if time.time() - last_note > 60:
                self.log.info("Still waiting to get into the meeting (lobby?)...")
                last_note = time.time()
            time.sleep(3)
        self.log.warning("Gave up waiting to be admitted after %s minutes.", self.cfg.join_wait_minutes)
        return False

    def read_messages(self) -> list[dict]:
        """Collect messages from the page and every iframe inside it."""
        msgs: list[dict] = []
        for frame in self.page.frames:
            try:
                msgs.extend(frame.evaluate(EXTRACT_MESSAGES_JS, self.cfg.teams_message_selectors))
            except PlaywrightError as exc:
                self.log.debug("read_messages failed in a frame: %s", str(exc).splitlines()[0])
        for m in msgs:
            m["key"] = stable_key(m)
        return msgs

    def wait_until_ready(self) -> None:
        """Block until the chat pane renders. Nag the user if they must log in."""
        last_nag = 0.0
        while True:
            self._dismiss_open_in_app()
            self.ensure_chat_open()
            if self.read_messages() or self.find_compose_box() is not None:
                self.log.info("Teams chat is loaded.")
                return
            if time.time() - last_nag > 30:
                alert_user(
                    self.log,
                    "Teams chat is not showing yet. In the first tab, sign in or join the "
                    "meeting and open the Chat panel; the bot carries on once it sees it.",
                )
                last_nag = time.time()
            time.sleep(3)

    def poll_number(self, msg: dict):
        """The number after 'poll', if the teacher wrote one (e.g. 'POLL 3' -> '3')."""
        m = self.trigger_re.search(msg.get("text", ""))
        if not m:
            return None
        try:
            num = m.group(1)
        except IndexError:
            num = None
        return num.strip() if num else None

    def is_trigger(self, msg: dict) -> bool:
        if not self.trigger_re.search(msg.get("text", "")):
            return False
        if self.cfg.trigger_sender:
            if self.cfg.trigger_sender.lower() not in msg.get("author", "").lower():
                return False
        return True

    def find_compose_box(self):
        for sel in self.cfg.teams_compose_selectors:
            try:
                loc = self.page.locator(sel).last
                if loc.count() and loc.is_visible():
                    return loc
            except PlaywrightError:
                continue
        return None

    def post(self, text: str) -> bool:
        if self.cfg.dry_run:
            self.log.info("[dry-run] Would post %r to Teams.", text)
            return True
        self.ensure_chat_open()
        box = self.find_compose_box()
        if box is None:
            self.log.error("Could not find the Teams message box. Run --dump and send me the files.")
            return False
        try:
            box.click()
            self.page.keyboard.type(text, delay=30)
            time.sleep(0.3)
            self.page.keyboard.press("Enter")
        except PlaywrightError as exc:
            self.log.error("Posting to Teams failed: %s", exc)
            return False
        # Confirm it actually landed.
        deadline = time.time() + 15
        while time.time() < deadline:
            for m in self.read_messages()[-5:]:
                if m["text"].strip().lower() == text.strip().lower():
                    self.log.info("Posted %r to Teams.", text)
                    return True
            time.sleep(1)
        self.log.warning("Typed %r but could not confirm it appeared. Check the chat.", text)
        return False


# --------------------------------------------------------------------------
# LMT course site side
# --------------------------------------------------------------------------
class CourseSite:
    LOGGED_OUT_HINTS = ("sign in", "log in", "login", "password", "session has expired")

    def __init__(self, page: Page, cfg: Config, log: logging.Logger):
        self.page = page
        self.cfg = cfg
        self.log = log
        self.ack_re = re.compile(cfg.ack_button_pattern, re.IGNORECASE)
        self.course_url = cfg.course_url
        self.page.on("dialog", lambda d: d.accept())

    def open(self) -> None:
        self.log.info("Opening course page: %s", self.cfg.course_url)
        self.page.goto(self.cfg.course_url, wait_until="domcontentloaded")

    def reload(self) -> None:
        try:
            self.page.reload(wait_until="domcontentloaded")
        except PlaywrightError as exc:
            self.log.warning("Course page reload failed (%s); opening the URL again.", exc)
            try:
                self.page.goto(self.course_url, wait_until="domcontentloaded")
            except PlaywrightError as exc2:
                self.log.error("Could not open course page: %s", exc2)

    def looks_logged_out(self) -> bool:
        try:
            body = (self.page.inner_text("body") or "").lower()
        except PlaywrightError:
            return False
        if "upcoming sessions" in body:
            return False
        return any(h in body for h in self.LOGGED_OUT_HINTS)

    def looks_ready(self) -> bool:
        try:
            body = (self.page.inner_text("body") or "").lower()
        except PlaywrightError:
            return False
        return "upcoming sessions" in body

    def adopt_current_url(self) -> None:
        """Remember the tab's URL as the course page once it is showing."""
        url = self.page.url
        if url and url != self.course_url and "lmt" in url.lower():
            self.course_url = url
            self.log.info("Using the course page the tab is on now: %s", url)

    def wait_until_ready(self) -> None:
        last_nag = 0.0
        while True:
            if self.looks_ready():
                self.adopt_current_url()
                self.log.info("Course page is loaded.")
                return
            if time.time() - last_nag > 30:
                alert_user(
                    self.log,
                    "Course page is not showing 'Upcoming Sessions'. In the second tab, "
                    "sign in to the LMT site and open your course page (the one with the "
                    "Join buttons). The bot will carry on by itself once it sees it.",
                )
                last_nag = time.time()
            time.sleep(3)

    def find_join_link(self):
        """
        The Join button for today's session on the course page. Prefers the
        row that mentions today's date (e.g. "Wed, 10/14"); otherwise the
        first visible, enabled Join.
        """
        now = datetime.now()
        today = f"{now.month}/{now.day}"
        join_re = re.compile(r"^\s*join\s*$", re.I)
        for frame in self.page.frames:
            for role in ("link", "button"):
                try:
                    cands = frame.get_by_role(role, name=join_re)
                    n = cands.count()
                except PlaywrightError:
                    continue
                fallback = None
                for i in range(n):
                    c = cands.nth(i)
                    try:
                        if not c.is_visible() or not c.is_enabled():
                            continue
                        # Text of the smallest block around this Join that holds a
                        # date and no other Join button: that is its session row.
                        row_text = c.evaluate(
                            """el => {
                              const isJoin = n => /^\\s*join\\s*$/i.test(n.innerText || '');
                              let e = el.parentElement;
                              for (let i = 0; i < 10 && e && e !== document.body; i++) {
                                const joins = Array.from(e.querySelectorAll('a,button')).filter(isJoin).length;
                                if (joins > 1) break;
                                const t = e.innerText || '';
                                if (/\\d{1,2}\\/\\d{1,2}/.test(t)) return t;
                                e = e.parentElement;
                              }
                              return '';
                            }"""
                        ) or ""
                    except PlaywrightError:
                        continue
                    if re.search(r"\b" + re.escape(today) + r"\b", row_text):
                        return c
                    fallback = fallback or c
                if fallback is not None:
                    return fallback
        return None

    def meeting_link(self, wait_minutes: float) -> str:
        """
        Click Join on the course page and return the Teams link it opens.
        Retries (reloading) until the Join button is enabled, for up to
        wait_minutes. Returns "" if it never gets a link.
        """
        link_re = re.compile(self.cfg.meeting_link_pattern, re.I)
        deadline = time.time() + wait_minutes * 60
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            if attempt > 1:
                self.reload()
                time.sleep(2)
            if self.looks_logged_out():
                alert_user(self.log, "Course site is asking you to sign in. Sign in now in the course window.")
                time.sleep(30)
                continue
            btn = self.find_join_link()
            if btn is None:
                self.log.info("Attempt %d: no enabled Join button on the course page yet; retrying in 30s.", attempt)
                time.sleep(30)
                continue
            if self.cfg.dry_run:
                self.log.info("[dry-run] Would click Join on the course page.")
            popup = None
            before = self.page.url
            try:
                with self.page.expect_popup(timeout=10_000) as info:
                    btn.click()
                popup = info.value
            except PlaywrightTimeout:
                popup = None
            except PlaywrightError as exc:
                self.log.warning("Clicking Join on the course page failed: %s", str(exc).splitlines()[0])
                time.sleep(10)
                continue
            self.log.info("Clicked Join on the course page.")
            url = ""
            if popup is not None:
                url = self._link_from_page(popup, link_re)
                try:
                    popup.close()
                except PlaywrightError:
                    pass
            else:
                # No popup: maybe it navigated this tab instead.
                url = self._link_from_page(self.page, link_re)
                if self.page.url != before:
                    try:
                        self.page.go_back(wait_until="domcontentloaded")
                    except PlaywrightError:
                        self.page.goto(self.course_url, wait_until="domcontentloaded")
            if url:
                self.log.info("Meeting link from the course page: %s", url[:120])
                return url
            self.log.warning("Join opened something, but no Teams link was found in it. Retrying.")
            time.sleep(15)
        self.log.error("Gave up looking for the Join button after %s minutes.", wait_minutes)
        return ""

    def _link_from_page(self, page: Page, link_re) -> str:
        """Wait for a page to land on (or show) a link matching link_re."""
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                url = page.url or ""
                if link_re.search(url) and not url.startswith("about:"):
                    return url
                for frame in page.frames:
                    hrefs = frame.evaluate(
                        "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"
                    ) or []
                    for h in hrefs:
                        if link_re.search(h):
                            return h
            except PlaywrightError:
                pass
            time.sleep(1)
        return ""

    def find_ack_button(self):
        """Search every frame for a button/link whose name matches the pattern."""
        for frame in self.page.frames:
            for role in ("button", "link"):
                try:
                    loc = frame.get_by_role(role, name=self.ack_re).first
                    if loc.count() and loc.is_visible():
                        return loc
                except PlaywrightError:
                    continue
            try:
                loc = frame.get_by_text(self.ack_re).first
                if loc.count() and loc.is_visible():
                    return loc
            except PlaywrightError:
                continue
        return None

    def _click_confirm_if_shown(self) -> None:
        confirm_re = re.compile(r"^\s*(ok|okay|yes|confirm|submit|acknowledge|continue)\s*$", re.I)
        deadline = time.time() + 4
        while time.time() < deadline:
            for frame in self.page.frames:
                try:
                    loc = frame.get_by_role("button", name=confirm_re).first
                    if loc.count() and loc.is_visible():
                        loc.click()
                        self.log.info("Clicked a confirmation button after acknowledging.")
                        return
                except PlaywrightError:
                    continue
            time.sleep(0.5)

    def acknowledge(self) -> bool:
        """Reload until the acknowledge button appears, then click it."""
        deadline = time.time() + self.cfg.ack_retry_seconds
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            self.reload()
            try:
                self.page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeout:
                pass
            if self.looks_logged_out():
                alert_user(self.log, "Course site is asking you to sign in. Sign in now in the second tab.")
                time.sleep(self.cfg.ack_retry_interval_seconds)
                continue
            btn = self.find_ack_button()
            if btn is None:
                self.log.info(
                    "Attempt %d: acknowledge button not on the page yet; retrying in %ss.",
                    attempt,
                    self.cfg.ack_retry_interval_seconds,
                )
                time.sleep(self.cfg.ack_retry_interval_seconds)
                continue
            if self.cfg.dry_run:
                self.log.info("[dry-run] Found the acknowledge button; would click it.")
                return True
            try:
                btn.scroll_into_view_if_needed()
                btn.click()
            except PlaywrightError as exc:
                self.log.error("Clicking the acknowledge button failed: %s", exc)
                time.sleep(self.cfg.ack_retry_interval_seconds)
                continue
            self.log.info("Clicked the acknowledge button.")
            self._click_confirm_if_shown()
            time.sleep(2)
            if self.find_ack_button() is None:
                self.log.info("Acknowledge button is gone; treating the poll as acknowledged.")
            else:
                self.log.warning(
                    "Acknowledge button is still visible after clicking. "
                    "It may have worked anyway; check the site if unsure."
                )
            return True
        self.log.error("Gave up: no acknowledge button appeared within %ss.", self.cfg.ack_retry_seconds)
        return False


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def launch_browser(p, cfg: Config, log: logging.Logger):
    """Start Edge (or a fallback browser). Contexts are created by the caller."""
    # ATTENDANCE_BOT_HEADLESS=1 is only for automated testing; the real bot
    # needs a visible window so you can sign in.
    headless = os.environ.get("ATTENDANCE_BOT_HEADLESS") == "1"
    # The sandbox is what keeps Edge from showing its "unsupported flag" bar.
    common = dict(headless=headless, chromium_sandbox=sys.platform.startswith("win"))
    if cfg.browser_executable:
        return p.chromium.launch(executable_path=cfg.browser_executable, **common)
    if cfg.browser_channel:
        try:
            return p.chromium.launch(channel=cfg.browser_channel, **common)
        except PlaywrightError as exc:
            log.warning(
                "Could not start browser channel %r (%s). Falling back to Playwright's Chromium. "
                "If that also fails, run:  python -m playwright install chromium",
                cfg.browser_channel,
                str(exc).splitlines()[0],
            )
    return p.chromium.launch(**common)


def open_contexts(browser, cfg: Config, log: logging.Logger):
    """
    Teams gets a throwaway context: no cookies, no account, gone when the bot
    exits. That is the same as an InPrivate window, so Teams treats you as a
    guest every time. The course site gets a context whose cookies are saved
    to disk so its sign-in can survive between runs.
    """
    teams_ctx = browser.new_context(no_viewport=True)
    # Teams insists on camera and mic permission before it shows the join
    # screen. Grant it up front so Edge never shows the Allow/Block prompt;
    # the bot still switches both devices off on the join screen.
    try:
        teams_ctx.grant_permissions(["camera", "microphone"])
    except PlaywrightError as exc:
        log.warning("Could not pre-grant camera/mic permission: %s", str(exc).splitlines()[0])
    site_state = Path(cfg.data_dir) / "state" / "site-storage.json"
    site_kwargs = dict(no_viewport=True)
    if site_state.exists():
        site_kwargs["storage_state"] = str(site_state)
    try:
        site_ctx = browser.new_context(**site_kwargs)
    except PlaywrightError as exc:
        log.warning("Saved site cookies could not be loaded (%s); starting clean.", str(exc).splitlines()[0])
        site_ctx = browser.new_context(no_viewport=True)
    return teams_ctx, site_ctx, site_state


def save_site_state(site_ctx, site_state: Path, log: logging.Logger) -> None:
    try:
        site_state.parent.mkdir(parents=True, exist_ok=True)
        site_ctx.storage_state(path=str(site_state))
    except PlaywrightError as exc:
        log.debug("Could not save site cookies: %s", exc)


# Compact description of a page: which data-tid values exist, what the
# ancestors of timestamps and text boxes look like, and the visible text.
# Small enough to paste into a chat, unlike the full HTML.
PAGE_MAP_JS = """
() => {
  const desc = el => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + String(el.id).slice(0, 40);
    for (const a of ['data-tid', 'role', 'aria-label', 'data-testid', 'contenteditable', 'name']) {
      const v = el.getAttribute && el.getAttribute(a);
      if (v) s += ' [' + a + '=' + v.slice(0, 60) + ']';
    }
    if (typeof el.className === 'string' && el.className)
      s += ' .' + el.className.trim().split(/\\s+/).slice(0, 3).join('.').slice(0, 70);
    return s;
  };
  const chain = el => {
    const out = []; let e = el;
    for (let i = 0; i < 12 && e && e !== document.body; i++) { out.push(desc(e)); e = e.parentElement; }
    return out;
  };
  const L = [];
  L.push('URL: ' + location.href);
  L.push('TITLE: ' + document.title);
  const tids = {};
  document.querySelectorAll('[data-tid]').forEach(e => { const t = e.getAttribute('data-tid'); tids[t] = (tids[t] || 0) + 1; });
  L.push('', '== data-tid values (count) ==');
  Object.entries(tids).sort((a, b) => b[1] - a[1]).forEach(([k, v]) => L.push(v + '\\t' + k));
  L.push('', '== time[datetime] elements: ancestor chain (first 4) ==');
  Array.from(document.querySelectorAll('time[datetime]')).slice(0, 4).forEach((t, i) => {
    L.push('-- time #' + i + ' datetime=' + t.getAttribute('datetime') + ' text=' + (t.innerText || '').slice(0, 30));
    chain(t).forEach(c => L.push('   ' + c));
  });
  L.push('', '== editable elements: ancestor chain ==');
  document.querySelectorAll('[contenteditable="true"], textarea').forEach((e, i) => {
    L.push('-- editable #' + i);
    chain(e).forEach(c => L.push('   ' + c));
  });
  L.push('', '== buttons (first 60) ==');
  Array.from(document.querySelectorAll('button, [role=button]')).slice(0, 60).forEach(b => L.push('   ' + desc(b) + '  text=' + (b.innerText || '').trim().slice(0, 40)));
  L.push('', '== iframes ==');
  document.querySelectorAll('iframe').forEach(f => L.push('   ' + (f.src || '').slice(0, 200) + '  ' + desc(f)));
  L.push('', '== visible text (first 4000 chars) ==');
  L.push((document.body.innerText || '').slice(0, 4000));
  return L.join('\\n');
}
"""


def page_map(page: Page) -> str:
    parts = []
    for i, frame in enumerate(page.frames):
        try:
            parts.append(f"##### FRAME {i}: {frame.url}\n" + frame.evaluate(PAGE_MAP_JS))
        except PlaywrightError as exc:
            parts.append(f"##### FRAME {i}: {frame.url}\n(could not read: {str(exc).splitlines()[0]})")
    return "\n\n".join(parts)


def dump_debug(cfg: Config, teams_page: Page, site_page: Page, log: logging.Logger) -> None:
    out = Path(cfg.data_dir) / "debug" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    for name, page in (("teams", teams_page), ("site", site_page)):
        try:
            (out / f"{name}-map.txt").write_text(page_map(page), encoding="utf-8")
            (out / f"{name}.html").write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(out / f"{name}.png"), full_page=False)
        except PlaywrightError as exc:
            log.error("Could not dump %s: %s", name, exc)
    log.info("Debug files written to %s", out)
    log.info("Send teams-map.txt and site-map.txt (and the .png files) back for selector fixes.")
    if sys.platform.startswith("win"):
        try:
            os.startfile(str(out))  # open the folder in Explorer
        except OSError:
            pass


def say_hello_once(cfg: Config, teams: "Teams", log: logging.Logger) -> None:
    """Post hello_message once per day, so restarts during the same class stay quiet."""
    if not cfg.hello_message:
        return
    hello_marker = Path(cfg.data_dir) / "state" / "hello.json"
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        if json.loads(hello_marker.read_text(encoding="utf-8")).get("date") == today:
            return
    except Exception:
        pass
    if teams.post(cfg.hello_message):
        hello_marker.parent.mkdir(parents=True, exist_ok=True)
        hello_marker.write_text(json.dumps({"date": today}), encoding="utf-8")


def wait_for_join_time(cfg: Config, site: "CourseSite", site_ctx, site_state: Path,
                       log: logging.Logger, skip_date: str = "") -> None:
    """Sleep until join_at on the next class day, keeping the course tab alive."""
    target = next_join_time(cfg, datetime.now(), skip_date)
    if target <= datetime.now():
        return
    log.info("Waiting until %s to join the meeting. Sign in to the course site meanwhile if it asks.",
             target.strftime("%a %Y-%m-%d %H:%M"))
    last_keepalive = time.time()
    last_note = time.time()
    while datetime.now() < target:
        time.sleep(min(30, max(1, (target - datetime.now()).total_seconds())))
        if time.time() - last_keepalive > cfg.keepalive_minutes * 60:
            try:
                site.reload()
                if site.looks_ready():
                    site.adopt_current_url()
                    save_site_state(site_ctx, site_state, log)
            except PlaywrightError as exc:
                log.debug("keepalive reload failed: %s", exc)
            last_keepalive = time.time()
        if time.time() - last_note > 1800:
            log.info("Still waiting; joining at %s.", target.strftime("%a %H:%M"))
            last_note = time.time()
    log.info("It is %s. Joining now.", target.strftime("%H:%M"))


def watch(cfg: Config, teams: "Teams", site: "CourseSite", site_ctx, site_state: Path,
          seen: SeenStore, log: logging.Logger) -> str:
    """
    The main loop for one class. Returns "goodbye" when the class wrapped up
    and the bot left, or "closed" when a browser window was closed.
    """
    old_cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
    site_page = site.page

    # Baseline: everything visible now is old news, unless its timestamp
    # says it arrived in the last two minutes (covers a quick restart).
    baseline = 0
    for m in teams.read_messages():
        ts = parse_ts(m["ts"])
        if ts is None or ts < old_cutoff:
            seen.add(m["key"])
            baseline += 1
    seen.save()
    log.info("Baselined %d existing messages. Watching for /%s/ ...", baseline, cfg.trigger_pattern)
    if cfg.dry_run:
        log.info("DRY RUN: nothing will be clicked or posted.")

    last_keepalive = time.time()
    last_trigger_time = 0.0
    last_empty_warning = 0.0

    # Which poll numbers were handled today, so "poll 3" is acted on once
    # even if the teacher repeats it or the bot restarts mid-class.
    polls_path = Path(cfg.data_dir) / "state" / "polls.json"
    handled_polls: set[str] = set()
    try:
        data = json.loads(polls_path.read_text(encoding="utf-8"))
        if data.get("date") == datetime.now().strftime("%Y-%m-%d"):
            handled_polls = set(data.get("polls", []))
    except Exception:
        pass

    def polls_sorted() -> str:
        return ", ".join(sorted(handled_polls, key=lambda x: int(x) if x.isdigit() else 0)) or "none"

    def remember_poll(num: str) -> None:
        handled_polls.add(num)
        polls_path.parent.mkdir(parents=True, exist_ok=True)
        polls_path.write_text(
            json.dumps({"date": datetime.now().strftime("%Y-%m-%d"),
                        "polls": sorted(handled_polls, key=lambda x: int(x) if x.isdigit() else 0)}),
            encoding="utf-8",
        )

    goodbyes: dict[str, float] = {}  # author -> time of their last goodbye

    while True:
        try:
            msgs = teams.read_messages()
            if not msgs:
                if time.time() - last_empty_warning > 60:
                    log.warning("Teams chat shows no messages; page may have reloaded or logged out.")
                    last_empty_warning = time.time()
                teams._dismiss_open_in_app()
            for m in msgs:
                if m["key"] in seen:
                    continue
                seen.add(m["key"])
                ts = parse_ts(m["ts"])
                if ts is not None and ts < old_cutoff:
                    continue
                log.info("New message from %r: %s", m["author"], m["text"][:120].replace("\n", " "))
                if teams.is_goodbye(m):
                    goodbyes[m["author"] or m["key"]] = time.time()
                    window = cfg.goodbye_window_minutes * 60
                    recent = [a for a, t in goodbyes.items() if time.time() - t <= window]
                    log.info("Goodbye from %r (%d/%d in the last %g min).", m["author"], len(recent),
                             cfg.goodbye_threshold, cfg.goodbye_window_minutes)
                    if len(recent) >= cfg.goodbye_threshold:
                        log.info("Class is wrapping up. Saying goodbye and leaving.")
                        if cfg.goodbye_message:
                            teams.post(cfg.goodbye_message)
                        if cfg.leave_after_goodbye:
                            teams.leave_meeting()
                        seen.save()
                        log.info("Done for today. Handled polls: %s", polls_sorted())
                        return "goodbye"
                if not teams.is_trigger(m):
                    continue
                num = teams.poll_number(m)
                if num is not None and num in handled_polls:
                    log.info("Poll %s was already handled today; ignoring.", num)
                    continue
                if num is None and time.time() - last_trigger_time < cfg.cooldown_minutes * 60:
                    log.info("Trigger matched but within cooldown; ignoring.")
                    continue
                last_trigger_time = time.time()
                log.info("TRIGGER: %r from %r (poll %s)", m["text"][:80], m["author"], num or "?")
                acked = site.acknowledge()
                if acked or not cfg.require_ack_for_done:
                    if teams.post(cfg.done_message) and num is not None:
                        remember_poll(num)
                        log.info("Handled polls today: %s", polls_sorted())
                else:
                    alert_user(log, "Could not acknowledge the poll. Do it by hand and post 'done'.")
                seen.save()
            seen.save()

            if time.time() - last_keepalive > cfg.keepalive_minutes * 60:
                site.reload()
                if site.looks_ready():
                    site.adopt_current_url()
                    save_site_state(site_ctx, site_state, log)
                elif site.looks_logged_out():
                    alert_user(log, "Course site logged you out. Sign in again in the second tab.")
                last_keepalive = time.time()
        except PlaywrightError as exc:
            first = str(exc).splitlines()[0] if str(exc) else repr(exc)
            log.error("Browser error in main loop: %s", first)
            log.debug("Traceback:", exc_info=True)
            if teams.page.is_closed() or site_page.is_closed():
                log.error("A browser tab was closed. Exiting.")
                return "closed"
        except Exception:
            # Anything unexpected: record it in full and keep watching.
            log.exception("Unexpected error in main loop; continuing.")
        time.sleep(cfg.check_interval_seconds)


def run(cfg: Config, mode: str, log: logging.Logger) -> int:
    seen = SeenStore(Path(cfg.data_dir) / "state" / "seen.json")

    with sync_playwright() as p:
        browser = launch_browser(p, cfg, log)
        teams_ctx, site_ctx, site_state = open_contexts(browser, cfg, log)
        site_page = site_ctx.new_page()
        site = CourseSite(site_page, cfg, log)
        site.open()

        scheduled = mode == "run" and bool(cfg.join_at)
        if scheduled:
            log.info("Scheduled mode: joining at %s on %s.", cfg.join_at,
                     ", ".join(cfg.class_days) if cfg.class_days else "every day")

        attended_date = ""  # the class day we already joined and left
        while True:
            if scheduled:
                wait_for_join_time(cfg, site, site_ctx, site_state, log, attended_date)

            meeting_url = ""
            if cfg.meeting_link_from_site and mode in ("run", "test-teams"):
                site.wait_until_ready()
                save_site_state(site_ctx, site_state, log)
                meeting_url = site.meeting_link(cfg.join_wait_minutes)
                if not meeting_url and not cfg.teams_chat_url:
                    alert_user(log, "Could not get the meeting link from the course page. "
                                    "Join by hand in the Teams window when it opens.")
                    meeting_url = "https://teams.live.com/"
                elif not meeting_url:
                    log.info("Falling back to teams_chat_url from config.json.")

            teams_page = teams_ctx.new_page()
            teams = Teams(teams_page, cfg, log)
            teams.open(meeting_url)
            if mode in ("run", "test-teams"):
                teams.join_meeting()

            if mode == "dump":
                print()
                print("  DUMP MODE")
                print("  1. In the first tab, get the Teams chat on screen (if it is a meeting,")
                print("     join it and open the Chat panel so messages are visible).")
                print("  2. In the second tab, sign in and open the course page.")
                print("  3. Come back here and press Enter.")
                print()
                try:
                    input("  Press Enter when both tabs are ready... ")
                except EOFError:
                    time.sleep(20)
                dump_debug(cfg, teams.page, site_page, log)
                return 0

            if mode == "test-site":
                site.wait_until_ready()
                btn = site.find_ack_button()
                if btn is None:
                    log.info("No acknowledge button on the course page right now (expected outside a poll).")
                else:
                    log.info("Found acknowledge button with text: %r", btn.inner_text().strip())
                return 0

            teams.wait_until_ready()

            if mode == "test-teams":
                msgs = teams.read_messages()
                log.info("Read %d messages. Last 8:", len(msgs))
                for m in msgs[-8:]:
                    flag = "TRIGGER" if teams.is_trigger(m) else "       "
                    log.info("  %s | %-20s | %-25s | %s", flag, m["author"][:20], m["ts"],
                             m["text"][:80].replace("\n", " "))
                return 0

            site.wait_until_ready()
            save_site_state(site_ctx, site_state, log)
            say_hello_once(cfg, teams, log)

            result = watch(cfg, teams, site, site_ctx, site_state, seen, log)
            if result == "closed":
                return 1
            if not scheduled:
                return 0

            # Scheduled mode: drop this Teams session and wait for the next class.
            attended_date = datetime.now().strftime("%Y-%m-%d")
            log.info("Closing the Teams window until the next class.")
            try:
                teams_ctx.close()
            except PlaywrightError:
                pass
            teams_ctx = browser.new_context(no_viewport=True)
            try:
                teams_ctx.grant_permissions(["camera", "microphone"])
            except PlaywrightError:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config.json")
    ap.add_argument("--dry-run", action="store_true", help="watch and log but never click or post")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--test-teams", action="store_true", help="show what the bot reads from the chat")
    group.add_argument("--test-site", action="store_true", help="check the course page for the acknowledge button")
    group.add_argument("--dump", action="store_true", help="save HTML and screenshots of both tabs for debugging")
    args = ap.parse_args()

    cfg = Config.load(Path(args.config))
    if args.dry_run:
        cfg.dry_run = True
    log = setup_logging(Path(cfg.data_dir))
    mode = "test-teams" if args.test_teams else "test-site" if args.test_site else "dump" if args.dump else "run"
    log.info("Starting attendance bot (mode=%s, data_dir=%s)", mode, cfg.data_dir)
    try:
        return run(cfg, mode, log)
    except KeyboardInterrupt:
        log.info("Stopped by user.")
        return 0
    except Exception:
        log.exception("Bot crashed. Full details are in %s", Path(cfg.data_dir) / "logs" / "bot.log")
        return 2


if __name__ == "__main__":
    sys.exit(main())
