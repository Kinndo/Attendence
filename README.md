# Teams attendance poll bot

Joins your Teams class meeting under your name, says **Hello** in the meeting chat, then watches
the chat for the teacher's **"poll 1"** / **"poll 2"** message.


It runs on Windows inside Microsoft Edge windows that it controls. Teams runs in a throwaway
private session, the same as an InPrivate window: no account, no cookies, so you join every
class as a guest under the name you set. The LMT site runs in a second window whose sign-in is
saved between runs. No Azure app registration, no API keys, no passwords stored.

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

1. The Teams window opens your meeting link in a private session. The bot rewrites the link
   to the address the web app uses, so Edge's "This site is trying to open Microsoft Teams"
   popup never appears. If a launcher page shows anyway, it picks **Continue on this browser**,
   and if Teams offers it, **Join as a guest**. It then types `display_name` into the name box, switches
   the camera and microphone off, clicks **Join**, and waits to be admitted (up to
   `join_wait_minutes`).
2. It opens the **Chat** panel if it is not already open.
3. It posts `hello_message` once per day. Restarting the bot during the same class does not
   post it again.
4. It then watches for the trigger message as described below.

Set `auto_join` to `false` if you would rather join by hand, and `hello_message` to `""` to skip
the greeting.

## First run

1. Double-click `run.bat`. Two Edge windows open: one for Teams, one for the LMT site.
2. Sign in to the LMT site in its window. If it lands somewhere other than your course page,
   go to the course page in that same tab. The Teams window joins on its own.
3. The console says `Baselined N existing messages. Watching for ...` once both tabs are ready.
   Leave the window open. You can minimise it.

The LMT sign-in is saved, so the next time `run.bat` is usually enough on its own.
The bot beeps and prints `ACTION NEEDED` if either site logs you out.

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
  "poll" followed by a number, any capitalisation) and, if `trigger_sender` is set, its author
  name contains that text.
- Messages already on screen when the bot starts are ignored, unless they arrived in the last two
  minutes. Handled messages are remembered in a state file, so a restart never repeats one.
- After a trigger, further triggers are ignored for `cooldown_minutes` (default 10).
- The bot reloads the course page every `ack_retry_interval_seconds` for up to
  `ack_retry_seconds` (default 10 minutes) waiting for the acknowledge button, in case the
  teacher posts before the button shows. It clicks the button and any OK/Confirm prompt.
- "done" is posted only after a successful acknowledge. Set `require_ack_for_done` to `false`
  to post regardless.
- Every `keepalive_minutes` the course tab is reloaded so the LMT session does not time out.

## Files and folders

Everything the bot writes lives in `%LOCALAPPDATA%\AttendanceBot`:

- `logs\bot.log` what it did and when
- `state\site-storage.json` the saved LMT sign-in cookies
- `state\seen.json` messages already handled
- `state\hello.json` the day the greeting was last posted
- `debug\` output of `dump.bat`

Delete `state\site-storage.json` to force a fresh LMT sign-in. Teams never keeps a sign-in.

## Config reference

| Key | Default | Meaning |
| --- | --- | --- |
| `teams_chat_url` | | Address bar URL of the class chat in Teams on the web. |
| `course_url` | | Address bar URL of the LMT course page. |
| `trigger_pattern` | `\bpoll\s*\d+\b` | Regular expression a message must match. |
| `trigger_sender` | `""` | Only trigger on authors whose name contains this. Empty means anyone. |
| `done_message` | `done` | Text posted to Teams after acknowledging. |
| `display_name` | `""` | Name typed on the meeting's join screen. |
| `auto_join` | `true` | Fill in the name and click Join automatically. |
| `hello_message` | `Hello` | Posted once per day after joining. Empty string disables it. |
| `join_wait_minutes` | `30` | How long to wait in the lobby before giving up. |
| `cooldown_minutes` | `10` | Ignore further triggers for this long after one fires. |
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
