# cursorpool

[![Build](https://github.com/zeval/cursorpool/actions/workflows/build.yml/badge.svg)](https://github.com/zeval/cursorpool/actions/workflows/build.yml)

Pool multiple [Cursor CLI](https://cursor.com/docs/cli/overview) accounts using
augpool's command interface. Run in **auto** mode to choose the least-used
account, or lock new sessions to one account. Identity is the **full email**.

This is a private adaptation of [augpool](https://github.com/zeval/augpool).
The command names, routing controls, share-envelope shape, dashboard layout,
and stats schema are retained. Cursor authentication uses **user API keys**.
Personal usage is available through an optional **Cursor dashboard session**.
Without it, local session counts drive selection; they do not measure token
usage, spend, or remaining quota. Browser-login import for CLI execution remains
unsupported.

| | |
|---|---|
| Home | `~/.cursorpool/` — override with `CURSORPOOL_HOME` or `--home` |
| Runtime | Python **3.11+**, zero third-party runtime dependencies |
| Child | Cursor's `agent` executable; `cursor-agent` is also recognized |
| Authentication | Selected key injected through `CURSOR_API_KEY` |

## Install

This project is **not published to PyPI**. Access to the private repository and
GitHub SSH authentication are required:

```bash
# Until the initial PR is merged, install its feature branch:
pipx install "git+ssh://git@github.com/zeval/cursorpool.git@feature/cursor-support"
```

Keep `~/.local/bin` on the PATH used by your terminal and agent host. Install
[Cursor CLI](https://cursor.com/docs/cli/installation) separately; its `agent`
executable must also be on that PATH.

For development:

```bash
git clone git@github.com:zeval/cursorpool.git
cd cursorpool
git switch feature/cursor-support
python3 -m pip install -e ".[dev]"
```

A project virtual environment works for development when activated. For agent
hosts, use pipx or another stable installation whose executable is always on PATH.

## Import your account

Create a **Cursor user API key** in the
[Cursor dashboard](https://cursor.com/dashboard). This is a Cursor credential,
not an OpenAI or Anthropic provider key. See
[Cursor CLI authentication](https://cursor.com/docs/cli/reference/authentication).

No JSON file is needed. Import your key through a hidden terminal prompt:

```bash
cursorpool import --email you@example.com
cursorpool list
cursorpool -p "hello"
```

`cursorpool add --email you@example.com` uses the same prompt. You can also supply
a key directly or read a raw key from stdin:

```bash
cursorpool import --email you@example.com --api-key 'fake-cursor-api-key'
cursorpool import --email you@example.com --api-key - < private-key.txt
cursorpool add --email you@example.com --api-key --weight 2.5
```

Use the hidden prompt or stdin to keep real keys out of shell history and process
arguments. `--api-key` without a value prompts; `--api-key -` reads raw text, not
JSON. A terminal without hidden-input support is rejected with a stdin suggestion.
Existing accounts require `--force` to replace their credentials.

If `CURSOR_API_KEY` is already set, `cursorpool import --self --email you@example.com` imports it. For compatibility, `--self` also retains its old
portable-credentials-file fallback and replacement behavior. Normal key imports
do not look for that file or read browser-login storage or an IDE database.

Legacy JSON import remains available through `--session FILE` (or `--session -`
for JSON on stdin), and share blobs remain supported. Existing stored accounts
need no migration. Cursorpool manages its own credential files with atomic writes
and `0600` permissions; you do not need to create or maintain them yourself.

## Enable personal usage

For an existing installation from the feature branch, first run:

```bash
pipx reinstall cursorpool
```

1. Sign into [Cursor dashboard](https://cursor.com/dashboard) as the account you
   imported into cursorpool.
2. Open browser developer tools: **Application → Cookies → cursor.com** in
   Chromium, or **Storage → Cookies** in Firefox. Copy only the **value** of
   `WorkosCursorSessionToken`.
3. Run the following command and paste the value into its hidden terminal prompt:

   ```bash
   cursorpool import --usage-session --email you@example.com
   ```

4. Fetch and display usage:

   ```bash
   cursorpool refresh
   cursorpool list
   cursorpool usage --no-color
   ```

The dashboard session is separate from the API key used to run Cursor. Importing
it preserves the existing key and account settings. For automation,
`--usage-session FILE` reads a raw cookie value from a file and
`--usage-session -` reads stdin. Never put the value in command arguments or chat.
Re-import it when Cursor rejects or expires the session.

The session is password-equivalent: stored atomically with `0600` permissions,
never included in share exports or child environments. No browser database or
IDE credential store is read automatically. Normal API-key/session replacement
imports can remove the optional dashboard session; attach it again afterward.

This uses the same dashboard endpoints identified in
[CodexBar's Cursor provider](https://github.com/steipete/CodexBar/blob/main/Sources/CodexBarCore/Providers/Cursor/CursorStatusProbe.swift):
`/api/auth/me` checks the email before `/api/usage-summary` is fetched. These are
internal dashboard endpoints, not a documented personal API-key contract; they
may change. Automated tests use fake responses, not live subscriptions.

## Export and share

```bash
cursorpool export you@example.com       # one unpadded base64url share blob
cursorpool export --self                # locked account, or sole account in auto
cursorpool export --json you@example.com
cursorpool import - < private-share-blob.txt
cursorpool import --force - < private-share-blob.txt
```

The v2 envelope remains `{v, email, label, session}`. The embedded session now
contains `apiKey`. A blob embeds its email, so do not supply `--email` with it.
Share only with consent through a private channel. Blobs and exports contain
complete credentials; never log them or paste them into public chat.

Environment export retains the same command:

```bash
eval "$(cursorpool export --env you@example.com)"
```

It clears `CURSOR_AUTH_TOKEN` and exports the selected `CURSOR_API_KEY` using
shell quoting. Prefer `cursorpool run` to avoid changing your shell environment.

## Run and select accounts

```bash
cursorpool                             # interactive Cursor session
cursorpool -p "hello"                  # print mode
cursorpool --email you@example.com -p "hello"
cursorpool run -- cursor-agent -p "hello"
cursorpool mode                        # show mode
cursorpool mode auto                    # balance new sessions
cursorpool mode you@example.com         # lock new sessions to this account
```

Selection order is explicit `--email` → persisted lock → automatic selection.
Auto ranks by current-cycle USD used divided by weight when usage is available
for every enabled account; otherwise all accounts use local selections divided
by weight. Ties use oldest selection and email. Dollar totals are not normalized
by plan allowance and do not guarantee remaining quota. Disabled accounts and accounts in cooldown cannot be selected. A locked
account cannot be disabled or removed until you switch mode.

Auto-mode print runs can fail over after a nonzero exit with rate-limit output.
The default is two failovers and a five-minute cooldown. The next Cursor attempt
adds `--continue` unless a resume option is already present. A retry can repeat
work that the earlier process already performed. Locked and explicit-account
runs return the selected account's error without switching accounts.

Interactive sessions inherit the terminal. Credentials are fixed for a child
process lifetime. Authentication is passed through the environment; no secret
is added to argv or a temporary session file. Child `--api-key`, `-a`, and
`--auth-token` flags are rejected because they would override pool selection.
Other child options use Cursor's syntax, not Auggie's (for example, `-p "hello"`
instead of `-p -q "hello"`).

## Kandev / ACP

```bash
cursorpool --acp                       # compatibility shorthand
cursorpool acp                        # native Cursor subcommand
cursorpool run -- agent acp
```

`--acp` is translated to Cursor's `acp` subcommand. Protocol mode selects one
account and uses `os.exec` with inherited stdin/stdout. There is no buffered
protocol output or mid-session failover. Cursor does not use Auggie-specific
flags such as `--allow-indexing`; configure the host with the commands above.

`run -- <command...>` still supports arbitrary commands, including a custom
protocol server. Cursorpool retains generic `--mcp` passthrough detection; it
does not implement a Cursor `--mcp` server option.

## Usage and machine integrations

```bash
cursorpool usage
cursorpool usage --no-color
cursorpool usage --json
cursorpool stats --json
cursorpool list --json
cursorpool refresh
```

The dashboard keeps augpool's layout, per-account bars, and 30-day session
history. With dashboard sessions configured, it shows **USD used** during each
account's billing cycle: included-plan usage plus on-demand usage, converted from
Cursor's cents. The list column becomes `USED USD`. JSON adds plan usage, on-demand
usage, available allowance/remaining values, billing dates, and fetch time. Older
request-only or otherwise unsupported responses remain unavailable rather than
being displayed as zero.

`refresh` and `--refresh` fetch current usage. Ordinary reads use the configured
cache TTL (300 seconds by default). Temporary network failures, HTTP 429, or
server errors can use cached data for up to 24 hours within the same billing
cycle, with an error explaining the fallback. Authentication failures, identity
mismatches, and changed dashboard sessions invalidate prior data. Copied Augment
caches cannot influence ranking.

Partial results show known USD usage, but automatic selection uses local counts
for the whole pool whenever any enabled account lacks usage. Without dashboard
sessions, no usage request is made and the dashboard shows local session bars.
Local counts are UTC, retained for 90 days, and track cursorpool selections only.
Failed attempts that trigger failover are cooled down but not counted as completed
selections; activity outside cursorpool is absent from local history.

`stats --json` retains **schema_version 2**, mode, nullable `locked_email`, usage
metadata, and ranked account rows. For compatibility, `stats` and `list` retain
`credits_consumed`: read `source` alongside it (`analytics` means USD used,
`local` means selection count, `unknown` means no recorded usage). Stats adds
`usage.unit` and `usage.account_usage` for dashboard data; `usage --json` adds
`usage_unit`, `usage_complete`, and `account_usage`. Errors and refresh provenance
remain available in JSON. This does not integrate the separate Teams Admin API.

Reports exclude credentials, session paths, tenant URLs, and notes. `export`
remains the explicit exception: its stdout is credential data.

```bash
cursorpool update you@example.com --weight 2.5 --json
cursorpool update you@example.com --disable --json
cursorpool update you@example.com --enable --json
cursorpool remove you@example.com --json
```

`mode`, `import`, `remove`, and `update` preserve their JSON mutation responses.

## Commands

| Command | Purpose |
|---|---|
| `import --email … [--api-key [KEY]]` | Import API key; hidden prompt by default, `-` = raw stdin |
| `import --self --email …` | Import environment key (legacy portable-file fallback) |
| `import --session PATH --email …` | Legacy credentials JSON import (`-` = stdin) |
| `import --usage-session [FILE] --email …` | Attach dashboard cookie (hidden prompt by default; `-` = stdin) |
| `import <blob>` | Import share blob (`-` = stdin; `--json`) |
| `add --email … [--api-key [KEY]]` | Add API key; supports label, weight, notes and `--force` |
| `export [email] \| --self` | Export blob (`--env`, `--json`) |
| `remove EMAIL` | Remove account (`--json`) |
| `update EMAIL [--enable\|--disable] [--weight N]` | Update settings (`--json`) |
| `mode [auto\|email]` | Read or change routing (`--json`) |
| `list` | Ranked accounts (`--json`, `--refresh`) |
| `usage` | Session dashboard (`--json`, `--refresh`, `--no-color`) |
| `stats --json` | Versioned safe snapshot (`--refresh`) |
| `refresh` | Fetch personal dashboard usage |
| `run -- <cmd…>` | Execute with pooled authentication |
| `status` | Home, mode and ranks (`--json`, `--refresh`) |
| `restore` | Restore a legacy portable credentials backup |

`next` and `use` remain removed, as in augpool; use `mode`. `restore` preserves
the backup interface for portable session files. Normal runs do not overwrite
session files or create backups, and restoring never changes the selection mode.

## Files and development

```text
~/.cursorpool/
  pool.json           # registry, lock, weights; schema v3
  state.json          # local selections, daily history, cooldowns
  creds/<email>.json  # API keys and optional dashboard sessions, mode 0600
  cache/usage.json    # usage amounts, billing dates, cache provenance and errors
  backups/            # optional portable-session backup
```

The legacy `cursor_session_path` registry field remains readable for `--self`
and `restore` compatibility. Normal setup and runs use the managed account
credentials above. Augpool's home and credentials are independent.
Forked from augpool commit `eccfc570a9a0d105c884526ede68f5b737b2b95b`.
The original MIT license is retained.

```bash
python3 -m pytest -q
python3 -m build
python3 -m twine check dist/*
git diff --check
```

CI tests Python 3.11–3.14, builds wheel and sdist, smoke-tests the installed CLI,
and uploads artifacts to this private repository. There is no PyPI publishing
workflow. Tests use fake credentials and temporary homes, with no live API calls.
