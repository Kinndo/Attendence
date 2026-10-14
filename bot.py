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
    teams_chat_url: str
    course_url: str
    trigger_pattern: str = r"\bpoll\s*\d+\b"
    trigger_sender: str = ""
    done_message: str = "done"
    check_interval_seconds: float = 5
    cooldown_minutes: float = 10
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
        if not cfg.teams_chat_url or not cfg.course_url:
            sys.exit("config.json needs both teams_chat_url and course_url.")
        cfg.teams_message_selectors = (
            cfg.teams_message_selectors or DEFAULT_MESSAGE_SELECTORS
        )
        cfg.teams_compose_selectors = (
            cfg.teams_compose_selectors or DEFAULT_COMPOSE_SELECTORS
        )
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


def stable_key(msg: dict) -> str:
    if msg.get("key"):
        return msg["key"]
    raw = "|".join([msg.get("author", ""), msg.get("ts", ""), msg.get("text", "")])
    return "h:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Teams side
# --------------------------------------------------------------------------
class Teams:
    def __init__(self, page: Page, cfg: Config, log: logging.Logger):
        self.page = page
        self.cfg = cfg
        self.log = log
        self.trigger_re = re.compile(cfg.trigger_pattern, re.IGNORECASE)

    def open(self) -> None:
        self.log.info("Opening Teams chat: %s", self.cfg.teams_chat_url)
        self.page.goto(self.cfg.teams_chat_url, wait_until="domcontentloaded")
        self._dismiss_open_in_app()

    def _dismiss_open_in_app(self) -> None:
        """The 'open in the Teams app?' page. Pick the web app if offered."""
        for label in ("Use the web app instead", "Continue on this browser"):
            try:
                loc = self.page.get_by_text(label, exact=False).first
                if loc.count() and loc.is_visible():
                    loc.click()
                    self.log.info("Chose '%s' on the Teams launcher page.", label)
                    return
            except PlaywrightError:
                pass

    def read_messages(self) -> list[dict]:
        try:
            msgs = self.page.evaluate(
                EXTRACT_MESSAGES_JS, self.cfg.teams_message_selectors
            )
        except PlaywrightError as exc:
            self.log.debug("read_messages failed: %s", exc)
            return []
        for m in msgs:
            m["key"] = stable_key(m)
        return msgs

    def wait_until_ready(self) -> None:
        """Block until the chat pane renders. Nag the user if they must log in."""
        last_nag = 0.0
        while True:
            self._dismiss_open_in_app()
            if self.read_messages():
                self.log.info("Teams chat is loaded.")
                return
            if time.time() - last_nag > 30:
                alert_user(
                    self.log,
                    "Teams chat is not showing messages yet. "
                    "If the first tab asks you to sign in, do that now.",
                )
                last_nag = time.time()
            time.sleep(3)

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
                self.page.goto(self.cfg.course_url, wait_until="domcontentloaded")
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

    def wait_until_ready(self) -> None:
        last_nag = 0.0
        while True:
            if self.looks_ready():
                self.log.info("Course page is loaded.")
                return
            if time.time() - last_nag > 30:
                alert_user(
                    self.log,
                    "Course page is not showing 'Upcoming Sessions'. "
                    "If the second tab asks you to sign in, do that now, "
                    "then open the course page in that same tab.",
                )
                last_nag = time.time()
            time.sleep(3)

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
def launch_browser(p, cfg: Config, log: logging.Logger) -> BrowserContext:
    profile_dir = Path(cfg.data_dir) / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    # ATTENDANCE_BOT_HEADLESS=1 is only for automated testing; the real bot
    # needs a visible window so you can sign in.
    headless = os.environ.get("ATTENDANCE_BOT_HEADLESS") == "1"
    common = dict(user_data_dir=str(profile_dir), headless=headless, no_viewport=True)
    if cfg.browser_executable:
        return p.chromium.launch_persistent_context(executable_path=cfg.browser_executable, **common)
    if cfg.browser_channel:
        try:
            return p.chromium.launch_persistent_context(channel=cfg.browser_channel, **common)
        except PlaywrightError as exc:
            log.warning(
                "Could not start browser channel %r (%s). Falling back to Playwright's Chromium. "
                "If that also fails, run:  python -m playwright install chromium",
                cfg.browser_channel,
                str(exc).splitlines()[0],
            )
    return p.chromium.launch_persistent_context(**common)


def dump_debug(cfg: Config, teams_page: Page, site_page: Page, log: logging.Logger) -> None:
    out = Path(cfg.data_dir) / "debug" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    for name, page in (("teams", teams_page), ("site", site_page)):
        try:
            (out / f"{name}.html").write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(out / f"{name}.png"), full_page=True)
        except PlaywrightError as exc:
            log.error("Could not dump %s: %s", name, exc)
    log.info("Debug files written to %s", out)


def run(cfg: Config, mode: str, log: logging.Logger) -> int:
    seen = SeenStore(Path(cfg.data_dir) / "state" / "seen.json")
    started_at = datetime.now(timezone.utc)
    old_cutoff = started_at - timedelta(minutes=2)

    with sync_playwright() as p:
        ctx = launch_browser(p, cfg, log)
        teams_page = ctx.pages[0] if ctx.pages else ctx.new_page()
        site_page = ctx.new_page()
        teams = Teams(teams_page, cfg, log)
        site = CourseSite(site_page, cfg, log)

        teams.open()
        site.open()

        if mode == "dump":
            time.sleep(8)
            dump_debug(cfg, teams_page, site_page, log)
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
                log.info("  %s | %-20s | %-25s | %s", flag, m["author"][:20], m["ts"], m["text"][:80].replace("\n", " "))
            return 0

        site.wait_until_ready()

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
                    if not teams.is_trigger(m):
                        continue
                    if time.time() - last_trigger_time < cfg.cooldown_minutes * 60:
                        log.info("Trigger matched but within cooldown; ignoring.")
                        continue
                    last_trigger_time = time.time()
                    log.info("TRIGGER: %r from %r", m["text"][:80], m["author"])
                    acked = site.acknowledge()
                    if acked or not cfg.require_ack_for_done:
                        teams.post(cfg.done_message)
                    else:
                        alert_user(log, "Could not acknowledge the poll. Do it by hand and post 'done'.")
                    seen.save()
                seen.save()

                if time.time() - last_keepalive > cfg.keepalive_minutes * 60:
                    site.reload()
                    if site.looks_logged_out():
                        alert_user(log, "Course site logged you out. Sign in again in the second tab.")
                    last_keepalive = time.time()
            except PlaywrightError as exc:
                first = str(exc).splitlines()[0] if str(exc) else repr(exc)
                log.error("Browser error in main loop: %s", first)
                log.debug("Traceback:", exc_info=True)
                if teams_page.is_closed() or site_page.is_closed():
                    log.error("A browser tab was closed. Exiting.")
                    return 1
            except Exception:
                # Anything unexpected: record it in full and keep watching.
                log.exception("Unexpected error in main loop; continuing.")
            time.sleep(cfg.check_interval_seconds)


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
