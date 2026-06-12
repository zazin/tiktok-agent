# Internals — Device automation (the brittle parts)

Maintainer-facing. These flows drive TikTok's UI over adb and are fragile: selectors
are **calibrated on a real device** (`com.ss.android.ugc.trill`, ID locale) and TikTok
A/B-tests them. When a flow breaks, re-verify with `uiautomator dump` and adjust the
constants at the top of the relevant module. For the publisher/MQTT contracts see the
parent [`docs/`](../README.md).

## Auto-post: two phases (`tiktok_poster.py::post`)

Deliberately split because UI automation is fragile and arguably against TikTok's ToS.

- **Phase 1 (`open_in_tiktok`, always runs, reliable):** fires an `ACTION_SEND` intent
  to open TikTok's composer with the image attached. Requires resolving the
  `/sdcard/...` path to a MediaStore `content://` URI (`_resolve_content_uri`) —
  file:// URIs are blocked by scoped storage. The lookup matches on `_display_name`
  (filename), **not** `_data` (full path), because `_data` WHERE clauses return nothing
  on scoped-storage/MIUI devices. If `auto_post=False`, returns `"composer_open"` and
  stops here.
- **Phase 2 (opt-in `auto_post=True`, brittle):** walks `POST_FLOW_STEPS`, an
  **ordered** list of per-screen button labels (English + Indonesian). For each screen
  it dumps the UI tree (`uiautomator dump`), finds a node whose text/content-desc
  matches a label, and taps its center. The order matters so an ambiguous label on a
  later screen can't be tapped early. On any **unrecognized** screen it stops and
  returns `"needs_manual"` rather than tapping blindly. TikTok is then
  **`am force-stop`ped** — on success after waiting `POST_SUCCESS_KILL_DELAY` (so the
  upload finishes), and **immediately** on every error outcome
  (`needs_manual`/`wrong_account`) so the app is never left in a half-finished state;
  the item stays spooled for `--retry`. The **only** path that leaves TikTok open is
  `composer_open` (`--no-auto-post`), an explicit "push to phone, finish by hand" mode.

**Dry-run (`dry_run=True`, CLI `--dry-run`):** walks the flow and types the caption
exactly as a real post would, but **stops before the final Post tap** — leaving the
composer open with the (possibly truncated) caption visible for inspection. Returns
`"dry_run"` and never publishes. Use it to eyeball the on-device caption text.

**Tuning knobs (top of `tiktok_poster.py`):** `TIKTOK_PACKAGES`, `POST_FLOW_STEPS`,
`CAPTION_HINTS`, `STEP_DELAY`, `STEP_RETRIES`, `POST_SUCCESS_KILL_DELAY`,
`MAX_HASHTAGS`, `MAX_POST_CHARS`.

## Caption handling

TikTok has a single text field. `build_post_text` combines the `Caption` (hook) and
`Description` message fields (or ImageKit `customMetadata.caption`/`description` in
legacy mode) into one string — caption first, description on the next line. Hashtags
across the combined text are capped at `MAX_HASHTAGS` (extras dropped left-to-right).

**Length cap:** the on-device caption field drops anything past ~90 chars (a long
caption would otherwise swallow the whole description). `build_post_text` therefore
truncates the combined text to `MAX_POST_CHARS` (90) at a word boundary
(`_truncate_post_text`) so the cut is clean and the post still succeeds. The full text
still lives in the published MQTT message.

Typing uses `adb input text`, which **cannot enter emoji/non-ASCII**: `_input_line`
strips non-ASCII and quote chars, maps spaces to `%s`; newlines become
`KEYCODE_ENTER`. (Note: the pipeline does **not** store hashtags — captions/
descriptions are effectively hashtag-free aside from any inline ones.)

## Multi-account (`tiktok_profile.py`)

One device, multiple TikTok accounts logged into the **in-app account switcher**. A
message may carry an optional **`Account`** field (a TikTok `@handle`); when set, the
poster/commenter makes that account active **before** acting. Both `tiktok_poster.post`
and `tiktok_commenter.comment_on_post` call `tiktok_profile.ensure_account(account, …)`
first (the poster before opening the composer, the commenter before opening the post).
The CLIs also accept `--account`. `tiktok-profile` is a standalone CLI: no arg prints
the current `@handle`; `tiktok-profile @target` switches.

`ensure_account` opens the Profile tab and reads the active `@handle`; if it already
matches (compared normalized — leading `@` stripped, lowercased) it returns, otherwise
it opens the account switcher, taps the target row, and **re-reads to confirm**. If it
can't confirm the target is active it raises `TikTokProfileError`, which the posters map
to the status **`"wrong_account"`** — **nothing is posted / commented**, the spool file
is kept, and the broker message is acked `failed` (so it doesn't loop). It fails *safe*:
an unconfirmed account never posts to the wrong one.

This is the **most fragile** flow (TikTok A/B-tests the profile header heavily). Three
calibration facts drove the implementation, encoded in the constants/helpers at the top
of `tiktok_profile.py`:

- **The home feed blocks `uiautomator dump`** (it returns the launcher window behind
  it), so the profile is opened by **deep link** (`PROFILE_DEEPLINKS`,
  `snssdk1233://profile`), **not** by tapping the bottom-nav tab. Other TikTok screens
  (profile, search, composer, comment sheet) dump fine.
- The active handle is read from a `@name` **TEXT** node (`HANDLE_RE` requires a letter
  so untranslated `content-desc="@2131…"` resource refs aren't mistaken for it). The
  **account switcher is opened by tapping the display-NAME button** just above the
  handle (`_find_switch_trigger` anchors on the handle node).
- In the "Beralih akun" sheet each account row's **content-desc is the bare handle
  without `@`** (e.g. `captgani`), matched normalized by `_find_account_row`.

Use `uv run tiktok-profile` to re-calibrate in isolation.

## Comment-on-post (`tiktok_commenter.py`)

Leaves a comment on an existing TikTok post — triggered by HiveMQ on its own topic
(`tiktok/comments`) and own persistent session (`HIVEMQ_COMMENT_CLIENT_ID`), so it runs
as a separate process and never collides with `tiktok-agent`'s session. No image
handling, no AI — the exact comment text is in the message.

- **Reply flow (`reply_to`):** when the message carries `ReplyTo` (non-empty `author`),
  the comment is submitted as a **reply**. After opening the comment sheet,
  `comment_on_post` finds the target comment row (matching author + optional `text`
  substring, scrolling the sheet via `parse_comment_rows`/`_find_and_tap_reply`) and
  taps its **Balas/Reply** button — which auto-focuses the input ("Membalas <author>")
  — then types + sends like a top-level comment. If the target can't be found it returns
  **`comment_not_found`** (force-stop, kept for `--retry`) without submitting.
- **Flow (`comment_agent.py` → `comment_on_post`):** deep-link open the post (`am start
  -a android.intent.action.VIEW -d <url> -p <pkg>`, the reliable part) → **pause the
  video** (`_pause_video`) → tap the comment icon (its content-desc embeds a live count,
  so matched by **substring** via `_find_partial`/`_wait_and_tap_partial`) → focus the
  input field (capturing its bounds) → type → hide keyboard → **tap send positionally**
  → wait `COMMENT_SUCCESS_KILL_DELAY` then `am force-stop` TikTok.

### Three calibration facts (the part that breaks silently)

1. **Looping video keeps the UI non-idle** so `uiautomator dump` fails with *"could not
   get idle state"* and returns a **stale** tree — the first selector lookup matches
   nothing and you get `needs_manual` even though the controls are on screen.
   `_pause_video` fixes this: it taps the video center to pause, then **confirms a dump
   actually succeeds** (retrying — the post is still loading at first, so an early tap is
   a no-op), returning as soon as it does (a second tap would *resume* playback).
2. **A focused input's blinking cursor also keeps the UI non-idle**, so the **send
   button can't be found by dumping** — it's tapped **positionally** at the right end of
   the input row (`SEND_BTN_X_FRAC` × width, at the input field's vertical center) after
   hiding the keyboard. `PAUSE_TAP_*`/`SEND_BTN_X_FRAC` are the geometry knobs.
3. **The first-comment trap:** when a post has **no comments yet**, opening the sheet
   **auto-focuses the input with the keyboard already up**, so the input bounds captured
   before typing are the keyboard-UP (mid-screen) position — ~860px above where the
   input/send row rests once the keyboard is hidden. Reusing that Y made the send tap
   land in the comment list and the comment was **never posted** (reported `commented`
   anyway). Fix: after typing + hiding the keyboard, **re-derive the send row from a
   fresh dump** by anchoring on the just-typed text (`find_bounds_partial`, the only node
   carrying it), then tap `SEND_BTN_X_FRAC` × width at that row's Y.

### Submit verification + statuses

The send tap is geometric and silent — a miss leaves the text in the input with no
error. So after tapping send, `_submitted_ok` confirms the comment actually posted (the
input cleared back to its `COMMENT_INPUT_HINTS` placeholder, or the typed text now shows
as a comment row) before returning `commented`; otherwise it returns the retryable
`send_unverified`. Replies focus a different ("Membalas …") input that isn't verified
yet, so they keep the prior optimistic behavior (but still benefit from the re-derived
send tap).

Statuses (all ack-drop so a stuck UI doesn't loop): `commented` (success, and for a
top-level comment verified to have posted), `needs_manual` (unrecognized screen — stop,
don't tap blindly), `skipped_non_ascii` (nothing typeable after stripping — not
submitted), `wrong_account` (target `Account` couldn't be confirmed — not submitted),
`send_unverified` (typed + tapped send but the comment didn't post — retryable),
`comment_not_found` (reply target not found), `failed` (open/adb error). On success
TikTok is force-stopped after `COMMENT_SUCCESS_KILL_DELAY`; on every **error** outcome
it is force-stopped **immediately**. `--dry-run` opens + focuses the input and logs the
comment but **never submits**, leaves the message unacked, and is the one path that
leaves the app open (for inspection).

**ASCII limit (same as captions):** a comment with nothing typeable after stripping is
**not** submitted (`skipped_non_ascii`). Realistic scope is Latin-script languages.

**Tuning knobs (top of `tiktok_commenter.py`):** `COMMENT_OPEN_SUBSTRINGS`,
`COMMENT_INPUT_HINTS`, `PAUSE_TAP_X_FRAC`/`PAUSE_TAP_Y_FRAC`, `SEND_BTN_X_FRAC`,
`COMMENT_SUCCESS_KILL_DELAY`, `VERIFY_RETRIES`/`VERIFY_DELAY` (`STEP_DELAY`/
`STEP_RETRIES` are imported from `tiktok_ui`). The send button has **no** label constant
— see the positional-tap fact above.

## Comment-reader (`tiktok-comment-reader`, the read half of the reply loop)

Drains read-jobs, scrapes a post's comments on the phone, and publishes the list back so
the backend can generate replies (sent to `tiktok-commenter` with `ReplyTo`). The full
round-trip is **read-job → comment-list → reply-job**, all over HiveMQ.

- **Comment cap (`max`):** precedence is the read-job's `max` → `--max` →
  `$COMMENT_READ_MAX` → `DEFAULT_MAX_COMMENTS` (10). TikTok's sheet defaults to a
  **top/relevance sort**, so the first N scraped are the most-engaged. The scrape stops
  at the cap **or** after `SCROLL_STABLE_PASSES` swipes with no new rows (end of thread).
- **Scraping (`parse_comment_rows`):** with the video paused the comment sheet **is**
  dumpable. Each row is anchored on its **Balas/Reply** button (matched by label,
  stable); the author (`ROW_AUTHOR_ID`/`id/title` text) and body (`ROW_TEXT_ID`/`id/enp`
  text) are the id nodes falling **between the previous Reply button and this one** (so
  "Lihat N balasan" and adjacent rows can't be mis-attributed). These resource-id leaf
  names are **obfuscated and WILL drift** — re-verify with a real `uiautomator dump` if
  rows stop parsing. Three calibration facts: (1) **image/sticker comments have no text
  node** → skipped (can't be replied to); (2) a row whose author **scrolled partly off**
  resolves to no author → dropped (re-captured on an adjacent pass); (3) **nested replies
  are excluded** — collapsed reply threads ("Lihat N balasan") aren't scraped at all, and
  replies shown *inline* (e.g. a just-posted one) are dropped by **indentation**: a row
  whose author sits more than `REPLY_INDENT_TOLERANCE` px right of the left-most
  (top-level) author column is a reply, not a top-level comment. So only top-level
  comments are returned. `collect_comments` scrolls (`_swipe_sheet`), deduping by
  `(author, text)`.
- **Stateless + idempotent:** no local spool, no `--retry` — re-reading a post just
  re-publishes its current comments, and a failed read is left unacked so the broker
  redelivers it. All stateful policy (which posts to read, which comments deserve a
  reply, dedup of already-replied) lives on the **backend** — the correlation key is
  `(author + text)` since TikTok exposes no stable comment id.
- **Tuning knobs:** `DEFAULT_MAX_COMMENTS` (in `comment_reader_agent.py`); and in
  `tiktok_commenter.py`: `ROW_AUTHOR_ID`/`ROW_TEXT_ID`/`REPLY_BTN_LABELS`,
  `SHEET_CONTENT_MIN_X`, `REPLY_INDENT_TOLERANCE`, `SCROLL_*` (swipe geometry + loop
  bounds).
