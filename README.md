# LLM usage

> Automatic weekly token usage and model cost from my AI coding tools. Data is
> collected from Cursor's official API, local Codex session logs, local
> Antigravity conversation databases, and the DeepSeek platform export, then
> GitHub Actions rebuilds the card below.

<!-- WIDGET_START -->
<div>
<img src="assets/widget-light.svg#gh-light-mode-only" alt="This week's LLM usage" width="100%">
<img src="assets/widget-dark.svg#gh-dark-mode-only" alt="This week's LLM usage" width="100%">
</div>
<!-- WIDGET_END -->

This page only shows **this week**. To browse the last few weeks, use the blog
[widget](widget/), which can switch between this week and the previous three.

## About "model cost"

The amount on the card is in **USD**. Cursor supplies a per-request figure
(`tokenUsage.totalCents`). Codex does not, so those rows are priced at OpenAI's
published API rates — an API-equivalent figure, not a ChatGPT bill. Antigravity
is priced the same way at Google's published Gemini API rates, not a Google AI
subscription bill. DeepSeek
supplies a platform invoice in CNY; that amount is converted to USD cents at
collect time (`usd_cny` in `config/aggregate.yaml`).

For Cursor, Codex, and Antigravity it is **not a bill**: it excludes plan fees, discounts,
and platform markup, and it does not care who pays. Included-in-plan usage still
counts, because this project measures consumption, not invoices. Cursor's billed
amount and markup fields never enter this repository.

## How it works

```
Cursor official API / Codex local logs / Antigravity local DBs / DeepSeek platform export
                → data/raw/<source>/…        (raw events, collected locally)
                → data/stats.json            (CI fold output, with four WeekViews)
                → assets/*.svg + widget      (both renderers only consume weeks[].view)
```

Collection needs a local Cursor login, the local Codex session directory
(`~/.codex`), the local Antigravity conversation directory
(`~/.gemini/antigravity/conversations`), and a DeepSeek console `userToken`. Fold and render are pure
reductions over files in the repo, so CI can regenerate artifacts without any of
those credentials.

**Collection is idempotent**: each run re-fetches `[since, today]` and overwrites.
Running once or ten times yields the same files. Miss a few days, run again — no
duplicates. Cursor and DeepSeek keep usage history on the account, so a missed
local run does not lose data.

**Artifacts are determined by data, not by the clock.** "This week" is the ISO
week of the most recent day that has usage, not the runtime calendar. The same
raw files always produce the same artifacts.

## Two machines

Cursor and DeepSeek usage come from account-level APIs, so both machines collect
the same payload. Codex and Antigravity sessions exist only on the machine that
produced them, so both the work Mac and the home Windows box need to collect; files are sharded as
`data/raw/<source>/<machine>/` and do not overwrite each other.

## What is collected, and what is not

| Source | Token detail | Cost | Notes |
| --- | --- | --- | --- |
| `cursor` | input / output / cache write / cache read | yes | Official dashboard API, back to account creation |
| `codex` | input / output / cache write / cache read | API list price | Codex local session logs. ChatGPT Plus and relays both belong to this ADE; cost is filled at fold time from published OpenAI rates |
| `antigravity` | input / output / cache read (no write) | API list price | Local per-conversation SQLite (protobuf metadata). No official usage-history API exists; quota endpoints only report remaining fractions. Collection refuses to write if the storage format drifts |
| `deepseek` | input / output / cache read (no write) | platform billed CNY → USD | Console monthly export. Account-level. Peak/off-peak is already in the invoice; `cache_write` is omitted, not zero |

When a source cannot report a metric, the field is **omitted**, not filled with 0.
The view then shows a dash. "Does not report tokens" and "reported zero" are
different.

Cache reads are usually more than 90% of total tokens, so the four kinds are
always stored separately and never merged into one total.

Excluded sources, and the measurements that excluded them, are in the module docs
of `llm_usage/collect/cursor.py` and [`docs/DESIGN.md`](docs/DESIGN.md):

- Cursor's local `ai-code-tracking.db`: request counts only, no token or cost fields.
- Cursor's local `state.vscdb`: token fields have 0% coverage in the current window; leftover from an older version.
- WorkBuddy: `session_usage.used` is context occupancy, not token consumption. Showing it would mislead.

## Running

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config/sources.example.yaml sources.yaml
.venv/bin/python run.py                       # collect + fold + render
# equivalent: python -m llm_usage
```

On Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config\sources.example.yaml sources.yaml
# Set a unique machine name such as home-win in sources.yaml, then:
.\.venv\Scripts\python.exe run.py --only chatgpt
.\.venv\Scripts\python.exe run.py --only antigravity
```

Codex logs are detected at the platform's user home automatically
(`C:\Users\<user>\.codex` on Windows). A custom `codex_home` may use
`%USERPROFILE%/.codex` as well as `~/.codex`. Antigravity works the same way:
`~/.gemini/antigravity/conversations` (plus the CLI's
`~/.gemini/antigravity-cli/conversations`) by default, overridable with
`antigravity_dirs`.

Collection reads credentials from the local Cursor login by default. If Cursor is
signed in, nothing else is needed. On a new machine, set
`CURSOR_SESSION_TOKEN=<sub>::<jwt>` (copy the `WorkosCursorSessionToken` cookie
from a dashboard request).

DeepSeek needs `DEEPSEEK_PLATFORM_TOKEN`: sign in at `platform.deepseek.com`,
then copy `userToken` from `localStorage`. This is the console session, not an
`sk-` API Key. Put it in a repo-root `.env` (gitignored) so the hourly launchd
job and `update-local.sh` can see it; a `zshrc` export is not inherited.

Common flags:

- `--only cursor` collect one source
- `--since 2026-07-01` override the collection start
- `--collect-only` write raw events only; this is what the local update scripts run
- `--skip-collect` fold and render only; this is what CI runs

For automatic updates, macOS launchd can run `./update-local.sh`; Windows Task
Scheduler can run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File
update-local.ps1`. Both scripts collect and push raw data; CI builds the rest.

On the work Mac (hourly at minute 0; a missed hour runs on wake):

```bash
./install-scheduled-task.sh
./install-scheduled-task.sh --run-now          # install and collect immediately
```

Manage it with `launchctl` or the same script:

```bash
launchctl print gui/$(id -u)/com.llm-usage.update              # inspect
launchctl kickstart -k gui/$(id -u)/com.llm-usage.update       # run now
./install-scheduled-task.sh --uninstall                        # remove
```

Logs go to `~/Library/Logs/llm-usage-update.log`.

To install the recommended Windows schedule (daily at midnight, catch up after
a missed run, and retry failures every 30 minutes), run once in PowerShell:

```powershell
.\install-scheduled-task.ps1
# Choose another time and test it immediately:
.\install-scheduled-task.ps1 -DailyAt 01:00 -RunNow
```

Manage the task in Task Scheduler (`taskschd.msc`) or from PowerShell:

```powershell
Get-ScheduledTask -TaskName "LLM Usage Update"       # inspect
Start-ScheduledTask -TaskName "LLM Usage Update"     # run now
Disable-ScheduledTask -TaskName "LLM Usage Update"   # pause
.\install-scheduled-task.ps1 -Uninstall               # remove
```

Logs go to `%LOCALAPPDATA%\llm-usage\update.log`.

## Config is two files

| File | Committed | Contents |
| --- | --- | --- |
| `sources.yaml` | no | Collection start, each source's `base_url` and credential env var names |
| `config/aggregate.yaml` | yes | Timezone, model alias table, and `usd_cny` |

They are split because the readers differ: collection runs only locally, while CI
needs the timezone and aliases to fold, and must not receive any credentials.

The alias table normalizes the same model under different labels; otherwise the
ranking splits one model into several rows. Raw events keep the original name.
Normalization happens only at fold time, so a bad mapping is fixed by re-running.

## Data contract

`stats.schema.json` is the single source of truth, currently v4. Consumers should
check `schema_version` so a semantic change cannot silently render wrong data.
Each display week carries a precomputed `view` (sort, share, display strings).
The SVG and the blog widget only render; they do not recompute.

`daily` keeps detail for the last four ISO weeks; `year` is the year-to-date
summary, **stored but not shown yet**, until a full year of data exists. Full
history always lives in `data/raw` and can be re-windowed at any time.

## Tests

```bash
.venv/bin/python tests/test_core.py
```

Design tradeoffs are in [`docs/DESIGN.md`](docs/DESIGN.md); product premises are
in [`PRODUCT.md`](PRODUCT.md).
