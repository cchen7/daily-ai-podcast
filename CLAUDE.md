# CLAUDE.md — daily-ai-podcast

## What this project does

Turns a hand-authored daily AI newsletter (Markdown) into a published podcast episode:

- Azure AI Speech (neural TTS) synthesizes the newsletter to MP3.
- MP3 + `podcast.xml` RSS are uploaded to Azure Blob (public read).
- Subscribed clients pull from `https://aipodcast875e3b63.blob.core.windows.net/podcast/podcast.xml`.

The newsletter **content** is NOT auto-curated by this repo. It is written each day to `newsletters/YYYY-MM-DD.md` (by the user, or by a separate LLM workflow). This repo only does the publish step.

## Daily workflow

1. Drop today's newsletter at `newsletters/YYYY-MM-DD.md` (format below).
2. Run:
   ```bash
   ./publish-daily-ai-podcast \
     --date YYYY-MM-DD \
     --newsletter newsletters/YYYY-MM-DD.md \
     --title "Daily AI Brief" \
     --description "..."
   ```
   The wrapper bootstraps `.venv`, installs `requirements.txt`, runs `python -m daily_ai_podcast.publish`.
3. Local artifacts → `output/`. Blobs uploaded to container `podcast`:
   - `episodes/{date}-{slug}.mp3` (audio/mpeg)
   - `podcast.xml` at root (application/rss+xml)
4. Re-running with the same date **overwrites** both blobs (and pushes a duplicate to subscribers). No need to delete first.

## Newsletter → script transform (`newsletter_to_script` in `src/daily_ai_podcast/publish.py`)

- `# H1` → dropped (intro line is generated).
- `## H2`:
  - `Top Stories` / `Signals to Watch` / `Bottom Line` → heading word NOT spoken.
  - any other H2 → spoken as `"<heading>."`
- `### H3` → `"Next: <heading>."`
- Lines starting with `Source:` / `Sources:` / `http` → stripped. Sections titled `## Sources` / `## Source Links` → entire block stripped. **URLs never reach TTS.**
- `Why it matters:` → `Why it matters.` (colon → pause).
- Markdown (`* _ ~ > # [text](url) `code``) → flattened to plain text.

Authoring tip: put source URLs as `Source: https://...` lines (one per source; for multi-source use `Sources:` then bare URLs each on its own line). They render for readers but are stripped from audio.

## Azure resources (already provisioned)

| Item | Value |
|---|---|
| Subscription | Visual Studio Enterprise · `875e3b63-92c7-44ca-853a-f06ddf54df9d` |
| Tenant | `450b40db-b570-41de-92ab-a4e45cf2cd59` |
| Resource Group | `aoai` (eastus) |
| Storage Account | `aipodcast875e3b63` (StorageV2, eastus, `allowBlobPublicAccess=true`) |
| Container | `podcast` (`publicAccess=blob` — anonymous read per blob) |
| Speech | `speech-daily-ai-podcast` (kind=`SpeechServices`, **SKU=F0 free tier**, eastus) |
| Public RSS URL | `https://aipodcast875e3b63.blob.core.windows.net/podcast/podcast.xml` |

F0 cap: ≤0.5 M chars/month neural TTS. One newsletter ≈ 3–5 K chars; ample headroom but watch this if doing many test runs.

## Auth model

`publish.py` uses **key-based auth at runtime** (it does NOT use the SP):

- `AZURE_SPEECH_KEY` — Speech subscription key.
- `AZURE_STORAGE_CONNECTION_STRING` — SA connection string (with account key).

Both are read from `.env` via `python-dotenv`.

The SP credentials in `.env` (`AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET`) are for `az` CLI / future IaC / `DefaultAzureCredential` work. **They are not consumed by `publish.py`.** Currently set to SP `myadmin` (`309a813a-...`) which has broad rights — before this becomes more than a personal pipeline, replace with a least-privilege SP scoped to RG `aoai` (`Storage Blob Data Contributor` on the SA + `Cognitive Services User` on the Speech account suffices) and switch `publish.py` to `DefaultAzureCredential` so account keys leave `.env`.

## Claude Code conventions for this repo

- **Isolated `az` config.** When running `az` from Claude Code, always set:
  `export AZURE_CONFIG_DIR=~/Local/my-proj-temp/daily-ai-podcast/azconfig`
  so SP login does not overwrite the user's global `~/.azure` state. The user's normal shell uses `~/.azure` as usual.
- **Never echo secrets to the transcript.** Pipe `az ... show-connection-string` / `az ... keys list` straight into a shell var or file and update `.env` from there. Printing live keys is blocked by a permission rule and is bad hygiene.
- **`.env` is real.** Contains live secrets. Permissions are `600`. Add `.env` (and `.venv/`, `output/`) to `.gitignore` BEFORE `git init`.
- **Temp / scratch under** `~/Local/my-proj-temp/daily-ai-podcast/`. Never pollute project root. `output/` is reserved for `publish.py` artifacts.

## Gotchas

- `.env` values containing spaces (e.g. `PODCAST_TITLE`) MUST be double-quoted, or zsh `source .env` errors with `command not found: Daily`. `python-dotenv` accepts either form.
- `az cognitiveservices account show` does NOT support `--ids`; use `-g`/`-n`.
- `podcast.xml` in blob is the **source of truth** for the feed. `build_or_update_feed()` fetches the existing XML and inserts/replaces the item whose `guid == mp3_url`. Deleting the blob silently truncates history on the next publish.
- New role assignments take effect on the next token issuance, but `az login` re-uses cached tokens for ~5 min — `az logout && az login` to force fresh.
