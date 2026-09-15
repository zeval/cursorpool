"""Wrap a command with pooled credentials and rate-limit failover."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from cursorpool.pool import Account, Pool, resolve_session_file
from cursorpool.select import pick
from cursorpool.session_io import apply_env, load_session
from cursorpool.state import State, record_selection, set_cooldown

RATE_LIMIT_PATTERNS = (
    re.compile(r"rate\s*limit", re.I),
    re.compile(r"\b429\b"),
    re.compile(r"too many requests", re.I),
    re.compile(r"\bquota\b", re.I),
    re.compile(r"credits?\s*(exhausted|exceeded|limit)", re.I),
    re.compile(r"\bbudget\b", re.I),
    re.compile(r"insufficient\s+credits", re.I),
)

DEFAULT_COOLDOWN_SECONDS = 300.0


@dataclass
class RunResult:
    exit_code: int
    account_email: str | None
    attempts: int
    failovers: int


def looks_like_rate_limit(text: str, exit_code: int) -> bool:
    if exit_code == 0:
        return False
    if not text:
        return exit_code == 429
    return any(p.search(text) for p in RATE_LIMIT_PATTERNS) or exit_code == 429


def _is_cursor(cmd: Sequence[str]) -> bool:
    """Recognize Cursor CLI executables, including the legacy binary name."""
    return bool(cmd) and Path(cmd[0]).name.lower() in {
        "agent", "agent.exe", "cursor-agent", "cursor-agent.exe",
    }


def is_protocol_mode(cmd: Sequence[str]) -> bool:
    """ACP/MCP need raw inherited stdio — never buffer, prefer exec."""
    if _is_cursor(cmd):
        # In print mode a positional "acp" is a prompt, not a subcommand.
        if any(arg in {"-p", "--print"} or arg.startswith("--print=") for arg in cmd[1:]):
            return False
        values = {
            "--model", "-m", "--mode", "--workspace", "--output-format",
            "--header", "-H", "--endpoint", "-e", "--sandbox", "--resume", "-r",
            "--api-key", "-a", "--auth-token",
        }
        args = iter(cmd[1:])
        for arg in args:
            if arg == "--":
                return False
            if arg in values:
                next(args, None)
            elif arg in {"--acp", "--mcp"}:
                return True
            elif not arg.startswith("-"):
                return arg == "acp"
        return False
    for a in cmd:
        if a in {"--acp", "--mcp"} or a.startswith("--acp=") or a.startswith("--mcp="):
            return True
    return False


def should_capture_output(cmd: Sequence[str], *, no_capture: bool = False) -> bool:
    """
    Decide whether to buffer child stdio.

    Capture only for one-shot print-mode runs where we want rate-limit text detection.
    Never capture ACP/MCP/interactive — that breaks the protocol handshake (kandev).
    """
    if no_capture:
        return False
    if is_protocol_mode(cmd):
        return False
    if _is_cursor(cmd):
        print_mode = any(
            a in {"-p", "--print"} or a.startswith("--print=") for a in cmd
        )
        # Interactive Cursor CLI: passthrough. Print mode: capture for failover heuristics.
        return print_mode
    # Other commands: capture only when stdout is not a TTY (scripts); TTY passthrough.
    if sys.stdout.isatty():
        return False
    # Non-TTY non-Cursor CLI still defaults to capture for failover — but never for protocols
    return True


def _has_resume_flag(cmd: Sequence[str]) -> bool:
    for a in cmd:
        if a in {"--continue", "-c", "--resume", "-r"}:
            return True
        if a.startswith("--resume=") or a.startswith("-r="):
            return True
    return False


def prepare_cmd(cmd: list[str], *, add_continue: bool) -> list[str]:
    out = list(cmd)
    if _is_cursor(out):
        auth_flags = {"--api-key", "-a", "--auth-token"}
        if any(
            arg.split("=", 1)[0] in auth_flags or arg.startswith("-a")
            for arg in out[1:]
        ):
            raise ValueError(
                "child authentication flags override pooled credentials; "
                "import the key and use --email instead"
            )
        if is_protocol_mode(out):
            out = ["acp" if arg == "--acp" else arg for arg in out]
    # Never inject --continue into ACP/MCP servers
    if (
        add_continue
        and _is_cursor(out)
        and not _has_resume_flag(out)
        and not is_protocol_mode(out)
    ):
        out.append("--continue")
    return out


def _build_env_and_cmd(
    cmd: Sequence[str],
    account: Account,
    *,
    root: Path | None,
    env: dict[str, str] | None,
    add_continue: bool,
) -> tuple[list[str], dict[str, str]]:
    """Prepare the command and credential environment without temporary files."""
    session = load_session(resolve_session_file(account, root))
    run_env = apply_env(session, env)
    final_cmd = prepare_cmd(list(cmd), add_continue=add_continue)

    return final_cmd, run_env


def run_with_account(
    cmd: Sequence[str],
    account: Account,
    *,
    root: Path | None,
    env: dict[str, str] | None = None,
    add_continue: bool = False,
    capture: bool = True,
    use_exec: bool = False,
) -> tuple[int, str]:
    final_cmd, run_env = _build_env_and_cmd(
        cmd, account, root=root, env=env, add_continue=add_continue
    )

    # ACP/MCP: replace this process so the host (kandev) speaks stdio to Cursor CLI
    # with no intermediate parent buffering or waiting.
    if use_exec or (is_protocol_mode(final_cmd) and not capture):
        # Best-effort: record selection already done by caller for protocol path.
        try:
            os.execvpe(final_cmd[0], final_cmd, run_env)
        except OSError as e:
            raise RuntimeError(f"exec failed for {final_cmd[0]!r}: {e}") from e

    if capture:
        proc = subprocess.run(
            final_cmd,
            env=run_env,
            text=True,
            capture_output=True,
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        # Replay output so user still sees it
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        return proc.returncode, combined
    proc = subprocess.run(final_cmd, env=run_env)
    return proc.returncode, ""


def run_pooled(
    pool: Pool,
    state: State,
    cmd: Sequence[str],
    *,
    root: Path | None = None,
    account_email: str | None = None,
    usage: dict[str, float] | None = None,
    max_failovers: int = 2,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    capture: bool | None = None,
    allow_failover: bool = True,
    on_before_exec=None,
) -> RunResult:
    """
    Run cmd with a fixed or pool-selected account.

    When allow_failover is false, a rate-limited fixed account records cooldown
    state and returns its error without selecting another account.

    For ACP/MCP (`--acp` / `--mcp`), picks an account, optionally calls
    on_before_exec(account_email) so the caller can persist state, then os.exec's
    the child (does not return).
    """
    if not cmd:
        raise ValueError("command required")

    if capture is None:
        capture = should_capture_output(cmd)

    protocol = is_protocol_mode(cmd)
    # Protocol servers cannot failover mid-flight; single pick + exec.
    if protocol:
        if account_email:
            account = pool.get(account_email)
        else:
            account = pick(pool, state, usage).account
        record_selection(state, account.email)
        if on_before_exec is not None:
            on_before_exec(account.email)
        # never returns on success
        run_with_account(
            cmd,
            account,
            root=root,
            add_continue=False,
            capture=False,
            use_exec=True,
        )
        # unreachable
        return RunResult(
            exit_code=1,
            account_email=account.email,
            attempts=1,
            failovers=0,
        )

    excluded: list[str] = []
    attempts = 0
    failovers = 0
    add_continue = False

    while True:
        if account_email and attempts == 0:
            account = pool.get(account_email)
            from cursorpool.select import RankedAccount

            ranked = RankedAccount(
                account=account,
                credits_consumed=0,
                local_uses=0,
                last_selected_at=None,
                score=0,
                in_cooldown=False,
                cooldown_until=None,
                source="forced",
            )
        else:
            ranked = pick(pool, state, usage, exclude_ids=excluded)

        attempts += 1
        code, output = run_with_account(
            cmd,
            ranked.account,
            root=root,
            add_continue=add_continue,
            capture=capture,
        )

        if code == 0:
            record_selection(state, ranked.account.email)
            return RunResult(
                exit_code=0,
                account_email=ranked.account.email,
                attempts=attempts,
                failovers=failovers,
            )

        rate_limited = looks_like_rate_limit(output, code)
        if rate_limited:
            set_cooldown(
                state,
                ranked.account.email,
                cooldown_seconds,
                error="rate_limit",
            )

        if rate_limited and allow_failover and failovers < max_failovers:
            excluded.append(ranked.account.email)
            failovers += 1
            add_continue = True
            account_email = None  # auto-pick next
            continue

        record_selection(state, ranked.account.email)
        return RunResult(
            exit_code=code,
            account_email=ranked.account.email,
            attempts=attempts,
            failovers=failovers,
        )
