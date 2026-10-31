# Teams attendance poll bot

You join the class meeting and sign in to the LMT site yourself. The bot then does three things,
for every poll of the session:

1. watches the meeting chat for the teacher's **POLL** message ("POLL", "poll 1", "Poll 12" all count),
2. reloads the H&R Block LMT course page and clicks **Acknowledge Attendance Poll** next to Join,
3. posts **done** in the chat,
4. and when the class says goodbye at the end, says **Goodbye** too, leaves the meeting and stops.

It remembers which poll numbers it has handled today, so each poll is acted on once even if the
message is repeated or the bot restarts mid-class.

It runs on Windows inside two Microsoft Edge windows that it controls: one for Teams, one for the
LMT site. The Teams window is a private session with no saved account, so you join the meeting
in it each class. The LMT window keeps its sign-in between runs. No Azure app registration, no
API keys, no passwords stored.

## One-time setup

1. Install Python 3.11 or newer from <https://www.python.org/downloads/windows/>.
   Tick **Add python.exe to PATH** on the first installer screen.
2. Download this folder (Code > Download ZIP on GitHub) and unzip it somewhere permanent,
   for example `C:\AttendanceBot`.
3. Double-click `setup.bat`. It installs Playwright and creates `config.json`.
4. Open `config.json` in Notepad and set the two URLs:
   - `course_url`: sign in to the LMT site the way you normally do, open your course so you
     see **Upcoming Sessions** and the Join buttons, then copy the whole address bar into this
     field. It looks like `https://lab.hrblock.com/lmt/clmsCourseDetails.prMain?in_sessionid=...`.
     The `in_sessionid` part expires. That is fine: when it does, the bot beeps and you sign in
     and open the course page in its second tab by hand. The bot then uses whatever page that tab
     is on. Do not use the bare `https://lab.hrblock.com/lmt/` address, it returns a 404.
   - `teams_chat_url`: the address of the place where the teacher posts "poll 1". If that is a
     normal Teams chat, open <https://teams.microsoft.com/> in a browser, open the chat, and copy
     the address bar. If it is the chat panel inside the class meeting, use the meeting join link
     and the bot's first tab will open the meeting lobby. Join the meeting there (camera and mic
     off) and open the Chat panel.
5. Recommended: set `trigger_sender` to part of the teacher's display name, for example
   `"Crowley"`. Then a classmate typing "is poll 1 up yet?" does not trigger it.

## What happens when you start it

1. Two Edge windows open. The Teams one opens your meeting link; the LMT one opens your course page.
2. You join the meeting in the Teams window (name, camera and mic off, Join) and open the
   **Chat** panel. You sign in to the LMT site in the other window if it asks.
3. The console prints `Baselined N existing messages. Watching for ...` once it can see both the
   chat and the course page. From then on it is watching. Leave both windows open.

## Joining on a schedule

Set `join_at` (24-hour clock, for example `"21:00"`) and `class_days` (for example
`["Mon", "Wed", "Fri"]`) in `config.json` and the bot runs the whole class on its own:

1. Start `run.bat` any time before class, or let the Startup shortcut start it at sign-in. The
   LMT window opens straight away so you can sign in early if it asks.
2. At `join_at` on a class day the Teams window opens the meeting, types `display_name`,
   switches camera and mic off, clicks **Join**, waits for admission, and opens the Chat panel.
   Starting the bot late still joins, as long as it is within `session_hours` of `join_at`.
3. It watches for polls as usual.
4. When `goodbye_threshold` people say goodbye, it says goodbye, leaves, closes the Teams
   window, and waits for the next class day. It never rejoins a class it has already left.

Leave `join_at` empty to go back to joining by hand. `auto_join` on its own joins immediately
at startup; `hello_message` posts a greeting once per day after the bot starts watching.

## First run

1. Double-click `run.bat`.
2. In the Teams window, join the meeting and open the Chat panel.
3. In the LMT window, sign in if asked and make sure the course page with the Join buttons is
   showing. If it shows a login page or an error, navigate to the course page in that same tab;
   the bot picks up whatever page the tab lands on.
4. Wait for `Baselined N existing messages` in the console. You can minimise the console.

The bot beeps and prints `ACTION NEEDED` if it loses either page, for example if the LMT site
signs you out.

## Testing without posting anything

| Command | What it does |
| --- | --- |
| `test-teams.bat` | Prints the last 8 messages the bot can read and marks which ones would trigger. |
| `test-site.bat` | Reports whether the acknowledge button is on the course page right now. |
| `dry-run.bat` | Runs the full watcher but logs what it *would* click and post instead of doing it. |
| `dump.bat` | Waits for you to get both tabs ready, then saves a page map, HTML and screenshot of each to the debug folder. |

Run `test-teams.bat` once after setup. If it reads zero messages or the wrong text, run `dump.bat`
and send back `teams-map.txt` and `site-map.txt` from `%LOCALAPPDATA%\AttendanceBot\debug`,
since the Teams page layout decides which selectors work. The map files are small text files
that describe the page structure without your message content beyond what is on screen.

## Start automatically when you sign in to Windows

```
powershell -ExecutionPolicy Bypass -File install_startup.ps1
```

Adds a shortcut to your Startup folder that launches `run.bat` minimised. Remove it with the same
command plus `-Remove`.

## How it decides

- A message counts as a trigger when its text matches `trigger_pattern` (default: the word
  "poll", any capitalisation, with or without a number) and, if `trigger_sender` is set, its
  author name contains that text. Set `trigger_sender` to part of the teacher's name so a
  classmate asking "is the poll up?" does not trigger it.
- When the message has a number, that number is recorded for the day in `state\polls.json` and
  the same number is never acted on twice. Messages without a number fall back to
  `cooldown_minutes`.
- Messages already on screen when the bot starts are ignored, unless they arrived in the last two
  minutes. Handled messages are remembered in a state file, so a restart never repeats one.
- After a trigger without a number, further triggers are ignored for `cooldown_minutes` (default 2).
- The bot reloads the course page every `ack_retry_interval_seconds` for up to
  `ack_retry_seconds` (default 10 minutes) waiting for the acknowledge button, in case the
  teacher posts before the button shows. It clicks the button and any OK/Confirm prompt.
- "done" is posted only after a successful acknowledge. Set `require_ack_for_done` to `false`
  to post regardless.
- Every `keepalive_minutes` the course tab is reloaded so the LMT session does not time out.
- When `goodbye_threshold` different people have said goodbye (bye, good night, see you, and so
  on) within `goodbye_window_minutes`, the bot posts `goodbye_message`, clicks **Leave** in the
  meeting, and exits. Your own messages do not count if `display_name` is set.

## Files and folders

Everything the bot writes lives in `%LOCALAPPDATA%\AttendanceBot`:

- `logs\bot.log` what it did and when
- `state\site-storage.json` the saved LMT sign-in cookies
- `state\seen.json` messages already handled
- `state\polls.json` poll numbers handled today
- `state\hello.json` the day the greeting was last posted
- `debug\` output of `dump.bat`

Delete `state\site-storage.json` to force a fresh LMT sign-in. Teams never keeps a sign-in.

## Config reference

| Key | Default | Meaning |
| --- | --- | --- |
| `teams_chat_url` | | Address bar URL of the class chat in Teams on the web. |
| `course_url` | | Address bar URL of the LMT course page. |
| `trigger_pattern` | `\bpoll\b\s*(\d+)?` | Regular expression a message must match; group 1 is the poll number. |
| `trigger_sender` | `""` | Only trigger on authors whose name contains this. Empty means anyone. |
| `done_message` | `done` | Text posted to Teams after acknowledging. |
| `display_name` | `""` | Name typed on the meeting's join screen. |
| `auto_join` | `true` | Fill in the name and click Join automatically. Always on when `join_at` is set. |
| `join_at` | `21:00` | Time of day to join, 24-hour clock. Empty disables scheduling. |
| `class_days` | `["Mon","Wed","Fri"]` | Days the bot joins on. Empty means every day. |
| `session_hours` | `3` | A start this long after `join_at` still joins the same class. |
| `hello_message` | `""` | Posted once per day after the bot starts watching. Empty disables it. |
| `join_wait_minutes` | `30` | How long to wait in the lobby before giving up. |
| `goodbye_pattern` | built in | Regular expression for a goodbye message. |
| `goodbye_threshold` | `5` | How many different people must say goodbye. |
| `goodbye_window_minutes` | `5` | They must all say it within this many minutes. |
| `goodbye_message` | `Goodbye` | What the bot posts before leaving. Empty skips the post. |
| `leave_after_goodbye` | `true` | Click Leave and exit once the goodbyes come in. |
| `cooldown_minutes` | `2` | For messages without a poll number: ignore further triggers for this long. |
| `check_interval_seconds` | `5` | How often the chat is checked. |
| `ack_button_pattern` | `acknowledge.*attendance.*poll` | Regular expression for the button text. |
| `ack_retry_seconds` | `600` | How long to keep reloading for the button. |
| `ack_retry_interval_seconds` | `15` | Seconds between reloads. |
| `require_ack_for_done` | `true` | Post "done" only after a successful click. |
| `keepalive_minutes` | `5` | Reload the course tab this often. |
| `browser_channel` | `msedge` | Installed browser to use. `chrome` also works. |
| `browser_executable` | `""` | Full path to a browser exe, if the channel is not found. |
| `teams_message_selectors` | built in | Override the CSS selectors used to find messages. |
| `teams_compose_selectors` | built in | Override the CSS selectors used to find the message box. |
| `data_dir` | `%LOCALAPPDATA%\AttendanceBot` | Where profile, logs and state are kept. |

## Caveats

- The bot works by reading the Teams web page. If Microsoft changes the page layout the bot may
  stop seeing messages until the selectors are updated. `test-teams.bat` shows you quickly
  whether that has happened.
- Teams on the web must stay open in the bot's window. Closing either window stops the bot.
- Because Teams runs as a guest, the teacher may need to admit you from the lobby each class.
- The computer must be awake and unlocked at class time. Check your power settings.
- Marking yourself present when you are not in class may breach your school's attendance
  policy. Use this to save the chore, not to skip class.
