# Teams attendance poll bot

Watches your class chat in Microsoft Teams for the teacher's **"poll 1"** / **"poll 2"** message.


It runs on Windows inside one Microsoft Edge window that it controls. You sign in to Teams and
the LMT site in that window once; the sign-ins are saved in the bot's own browser profile.
No Azure app registration, no API keys, no passwords stored.

## One-time setup

1. Install Python 3.11 or newer from <https://www.python.org/downloads/windows/>.
   Tick **Add python.exe to PATH** on the first installer screen.
2. Download this folder (Code > Download ZIP on GitHub) and unzip it somewhere permanent,
   for example `C:\AttendanceBot`.
3. Double-click `setup.bat`. It installs Playwright and creates `config.json`.
4. Open `config.json` in Notepad and set the two URLs:
   - `course_url`: open <https://lab.hrblock.com/lmt/>, sign in, open your course so you see
     **Upcoming Sessions**, then copy the address bar into this field. The `in_sessionid`
     part changes each sign-in. That is fine, the bot only uses this URL for the first load and
     keeps the tab alive after that.
   - `teams_chat_url`: open <https://teams.microsoft.com/> in a browser (not the desktop app),
     open the class chat, then copy the address bar into this field.
5. Recommended: set `trigger_sender` to part of the teacher's display name, for example
   `"Crowley"`. Then a classmate typing "is poll 1 up yet?" does not trigger it.

## First run

1. Double-click `run.bat`. An Edge window opens with two tabs.
2. Sign in to Teams in the first tab and to the LMT site in the second tab. If the LMT tab
   lands somewhere other than your course page, go to the course page in that same tab.
3. The console says `Baselined N existing messages. Watching for ...` once both tabs are ready.
   Leave the window open. You can minimise it.

Sign-ins persist, so the next time `run.bat` is usually enough on its own.
The bot beeps and prints `ACTION NEEDED` if either site logs you out.

## Testing without posting anything

| Command | What it does |
| --- | --- |
| `test-teams.bat` | Prints the last 8 messages the bot can read and marks which ones would trigger. |
| `test-site.bat` | Reports whether the acknowledge button is on the course page right now. |
| `dry-run.bat` | Runs the full watcher but logs what it *would* click and post instead of doing it. |
| `dump.bat` | Saves HTML and screenshots of both tabs to the debug folder, for troubleshooting. |

Run `test-teams.bat` once after setup. If it reads zero messages or the wrong text, run `dump.bat`
and send back the files from `%LOCALAPPDATA%\AttendanceBot\debug`, since the Teams page layout
decides which selectors work.

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

- `profile\` the saved browser sign-ins
- `logs\bot.log` what it did and when
- `state\seen.json` messages already handled
- `debug\` output of `dump.bat`

Delete `profile\` to force fresh sign-ins.

## Config reference

| Key | Default | Meaning |
| --- | --- | --- |
| `teams_chat_url` | | Address bar URL of the class chat in Teams on the web. |
| `course_url` | | Address bar URL of the LMT course page. |
| `trigger_pattern` | `\bpoll\s*\d+\b` | Regular expression a message must match. |
| `trigger_sender` | `""` | Only trigger on authors whose name contains this. Empty means anyone. |
| `done_message` | `done` | Text posted to Teams after acknowledging. |
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
- Teams on the web must stay open in the bot's window. Closing the window stops the bot.
- The computer must be awake and unlocked at class time. Check your power settings.
- Marking yourself present when you are not in class may breach your school's attendance
  policy. Use this to save the chore, not to skip class.
