# Telegram Song Downloader V3 Final

Localhost UI for the Telegram song-search workflow.

## What's new in V3 Final

- Separate **Login**, **Downloader**, and **History** pages.
- **Result Selection Mode** dropdown with **First Result**, **Best Text Match**, and **Strict Match**.
- **First Result is the default**: it trusts the Telegram bot's own ranking and clicks the first song result.
- Strict title/artist matching is still available when you want weak or unrelated results marked **Not Found** and skipped.
- `Artist - Title` and `Title - Artist` input formats are both supported.
- Separate counters for **Downloaded**, **Not Found**, and **Failed**.
- Export the current batch's Not Found songs as a text file.
- Persistent SQLite history (`history.db`) across app restarts.
- History filters/search plus direct links to files that still exist in the local `downloads/` folder.
- Validates that the target bot is an actual Telegram `@username` rather than a display name.
- Continues waiting through bot loading/progress messages before downloading the audio.

Use this only for audio you have permission or rights to download.

## Run on macOS

### Easiest

Double-click `start_mac.command`.

macOS may ask you to right-click it and choose **Open** the first time.

The script creates a virtual environment, installs dependencies, starts the server, and opens:

`http://127.0.0.1:8000/login`

### Terminal

```bash
cd /path/to/telegram_local_ui_v3
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 app.py
```

## Login page

Enter:

- Telegram API ID
- Telegram API Hash
- Phone number with country code
- The target bot's exact `@username` (or a `t.me/username` link)

If Telegram asks for a login code or 2FA password, the UI will show those fields.

## Downloader page

Paste one song per line, for example:

```text
Mac DeMarco - On the Level
Creep - Radiohead
Tame Impala - Let It Happen
```

V3 processes them sequentially. Before starting a batch, choose a **Result Selection Mode**:

- **First Result** (default): click the first song option returned by the bot. Use this when your bot already ranks the correct song first.
- **Best Text Match**: compare the result labels and click the closest title/artist text, even when confidence is not high.
- **Strict Match**: choose the best text match only if it passes the confidence checks; otherwise mark the item **Not Found** and continue.

The bot's explicit “not found” response is still respected in every mode.

## History page

History is stored in `history.db` in the app folder. Clearing History only clears database records; it does not delete downloaded audio.

Downloaded files are stored in:

`downloads/`

The Telegram session is stored locally as `song_downloader_session.session`.

## Login troubleshooting added in this build

The Login page now shows the delivery method Telegram returned (for example Telegram app, SMS, or call), keeps the returned `phone_code_hash` explicitly for verification, includes a Resend Code button, and includes Reset Local Session for stale session files.
