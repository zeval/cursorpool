"""cursorpool command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Sequence

from cursorpool import __version__, paths
from cursorpool.analytics import get_fresh_usage, load_usage_cache, refresh_usage
from cursorpool.pool import (
    add_account,
    find_account,
    load_pool,
    remove_account,
    resolve_locked_account,
    resolve_session_file,
    resolve_current_session_file,
    set_mode,
    update_account,
)
from cursorpool.runner import run_pooled
from cursorpool.reporting import collect_stats
from cursorpool.select import pick, rank_accounts
from cursorpool.session_io import (
    build_share_envelope,
    decode_share_blob,
    encode_share_blob,
    export_shell_line,
    load_session,
    normalize_email,
    read_share_blob_arg,
    restore_backup,
)
from cursorpool.state import locked_state, record_selection
from cursorpool.usage_view import render_usage_dashboard, usage_report_payload


def _root_from_args(args: argparse.Namespace) -> Path:
    if getattr(args, "home", None):
        return Path(args.home).expanduser().resolve()
    return paths.home()


def _resolve_who(pool, state, who: str | None, usage):
    if who:
        return find_account(pool, who)
    return pick(pool, state, usage).account


def _usage_map(pool, root: Path, *, force: bool = False, refresh_if_stale: bool = True):
    return get_fresh_usage(pool, root=root, force=force, refresh_if_stale=refresh_if_stale)


def _print_mutation(args: argparse.Namespace, action: str, email: str, human: str) -> None:
    if bool(getattr(args, "json", False)):
        print(json.dumps({"ok": True, "action": action, "email": email}))
    else:
        print(human)


def cmd_add(args: argparse.Namespace) -> int:
    root = _root_from_args(args)
    pool = load_pool(root)
    account = add_account(
        pool,
        email=args.email,
        session_source=args.session,
        root=root,
        label=args.label or "",
        weight=args.weight,
        notes=args.notes or "",
        force=bool(getattr(args, "force", False)),
    )
    print(f"added {account.email} -> {account.session_path}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """Import blob, --self (current session.json), or --session file + --email."""
    root = _root_from_args(args)
    pool = load_pool(root)

    session_flag = getattr(args, "session", None)
    modes = sum(bool(x) for x in (args.self, session_flag, args.blob))
    if modes > 1:
        raise ValueError(
            "pass only one of: blob, --self, or --session\n"
            "  examples:\n"
            "    cursorpool import eyJ...\n"
            "    cursorpool import --self --email you@company.com\n"
            "    cursorpool import --session ./session.json --email you@company.com"
        )

    if args.self or session_flag:
        if not args.email:
            which = "--self" if args.self else "--session"
            extra = "" if args.self else "./session.json "
            raise ValueError(
                f"import {which} requires --email\n"
                f"  example: cursorpool import {which} {extra}--email you@company.com"
            )
        email = normalize_email(args.email)
        if args.self:
            key = os.environ.get("CURSOR_API_KEY", "")
            session_path = resolve_current_session_file(pool, root)
            if key.strip():
                session = {"apiKey": key}
                source_label = "CURSOR_API_KEY"
            elif session_path.is_file():
                session = load_session(session_path)
                source_label = str(session_path)
            else:
                raise FileNotFoundError(
                    "no Cursor API key available; set CURSOR_API_KEY or use "
                    "import --session FILE --email EMAIL (JSON with apiKey). "
                    "Browser-login import is not supported."
                )
            force = True
        else:
            if str(session_flag) == "-":
                import json as _json
                import sys

                try:
                    payload = _json.load(sys.stdin)
                except _json.JSONDecodeError as e:
                    raise ValueError(f"stdin is not valid JSON: {e}") from e
                from cursorpool.session_io import validate_session

                session = validate_session(payload)
                source_label = "stdin"
            else:
                session_path = paths.expand(session_flag)
                if not session_path.is_file():
                    raise FileNotFoundError(f"session file not found: {session_path}")
                session = load_session(session_path)
                source_label = str(session_path)
            force = bool(args.force)

        account = add_account(
            pool,
            email=email,
            session=session,
            root=root,
            label=args.label or email,
            force=force,
        )
        _print_mutation(
            args,
            "import",
            account.email,
            f"imported {account.email} from {source_label}",
        )
        return 0

    if not args.blob:
        raise ValueError(
            "blob required, or use --self / --session\n"
            "  example: cursorpool import eyJ...\n"
            "  example: cursorpool import --self --email you@company.com\n"
            "  example: cursorpool import --session ./session.json --email you@company.com"
        )
    if args.email:
        raise ValueError(
            "--email is only valid with --self or --session "
            "(blob already embeds the email)"
        )
    raw = read_share_blob_arg(args.blob)
    env = decode_share_blob(raw)
    account = add_account(
        pool,
        email=env["email"],
        session=env["session"],
        root=root,
        label=env.get("label") or "",
        force=bool(args.force),
    )
    _print_mutation(args, "import", account.email, f"imported {account.email}")
    return 0



def cmd_remove(args: argparse.Namespace) -> int:
    root = _root_from_args(args)
    pool = load_pool(root)
    account = remove_account(pool, args.email, root=root)
    _print_mutation(args, "remove", account.email, f"removed {account.email}")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    root = _root_from_args(args)
    pool = load_pool(root)
    enabled = True if args.enable else False if args.disable else None
    account = update_account(
        pool,
        args.email,
        enabled=enabled,
        weight=args.weight,
        root=root,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "action": "update",
                    "email": account.email,
                    "enabled": account.enabled,
                    "weight": account.weight,
                    "locked": pool.locked_email == account.email,
                }
            )
        )
    else:
        print(
            f"updated {account.email}: enabled={account.enabled} "
            f"weight={account.weight:g}"
        )
    return 0


def cmd_mode(args: argparse.Namespace) -> int:
    root = _root_from_args(args)
    pool = load_pool(root)
    changed = args.target is not None
    if changed:
        set_mode(pool, args.target, root=root)
    account = resolve_locked_account(pool)
    mode = "locked" if account is not None else "auto"
    email = account.email if account is not None else None
    if args.json:
        payload: dict[str, object] = {"mode": mode, "email": email}
        if changed:
            payload = {"ok": True, "action": "mode", **payload}
        print(json.dumps(payload))
    elif account is None:
        print("auto")
    else:
        print(f"locked: {account.email}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    root = _root_from_args(args)
    pool = load_pool(root)
    with locked_state(root) as state:
        usage = _usage_map(pool, root, force=args.refresh, refresh_if_stale=True)
        ranked = rank_accounts(pool, state, usage)
    if args.json:
        payload = [
            {
                "email": r.account.email,
                "enabled": r.account.enabled,
                "weight": r.account.weight,
                "score": r.score,
                "credits_consumed": r.credits_consumed,
                "local_uses": r.local_uses,
                "source": r.source,
                "in_cooldown": r.in_cooldown,
                "locked": r.account.email == pool.locked_email,
            }
            for r in ranked
        ]
        enabled_emails = {r.account.email for r in ranked}
        for a in pool.accounts:
            if a.email not in enabled_emails:
                payload.append(
                    {
                        "email": a.email,
                        "enabled": a.enabled,
                        "weight": a.weight,
                        "score": None,
                        "credits_consumed": None,
                        "local_uses": None,
                        "source": None,
                        "in_cooldown": False,
                        "locked": a.email == pool.locked_email,
                    }
                )
        print(json.dumps(payload, indent=2))
        return 0
    if not pool.accounts:
        print("pool empty")
        print("  cursorpool import --self --email you@company.com")
        print("  cursorpool import <blob>")
        return 0
    headers = ("EMAIL", "SCORE", "CREDITS", "LOCAL", "SRC", "COOL", "LOCKED")
    rows = []
    for r in ranked:
        rows.append(
            (
                r.account.email[:40],
                f"{r.score:.1f}",
                f"{r.credits_consumed:.0f}" if r.source == "analytics" else "-",
                str(r.local_uses),
                r.source[:5],
                "yes" if r.in_cooldown else "-",
                "*" if r.account.email == pool.locked_email else "",
            )
        )
    for a in pool.accounts:
        if not a.enabled:
            rows.append((a.email[:40], "-", "-", "-", "off", "-", ""))
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in rows:
        print(fmt.format(*row))
    cache = load_usage_cache(root)
    if cache and cache.get("fetched_at"):
        age = int(time.time() - float(cache["fetched_at"]))
        print(f"\nusage cache age: {age}s  window: {cache.get('start_date')}..{cache.get('end_date')}")
        if cache.get("errors"):
            print("cache notes: " + "; ".join(cache["errors"]))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    if not args.json:
        raise ValueError("stats requires --json")
    snapshot = collect_stats(
        _root_from_args(args),
        force_refresh=bool(args.refresh),
    )
    print(json.dumps(snapshot, separators=(",", ":"), sort_keys=True))
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    root = _root_from_args(args)
    pool = load_pool(root)
    if not pool.accounts:
        print("pool empty — nothing to refresh", file=sys.stderr)
        return 1
    cache = refresh_usage(pool, root=root)
    print(f"refreshed {len(cache.get('by_id') or {})} accounts ({cache.get('start_date')}..{cache.get('end_date')})")
    for err in cache.get("errors") or []:
        print(f"warning: {err}", file=sys.stderr)
    for email, credits in sorted((cache.get("by_id") or {}).items()):
        print(f"  {email}: {credits}")
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    root = _root_from_args(args)
    pool = load_pool(root)
    with locked_state(root) as state:
        usage = _usage_map(pool, root, force=args.refresh, refresh_if_stale=True)
        cache = load_usage_cache(root)
        if args.json:
            print(
                json.dumps(
                    usage_report_payload(pool, state, usage, cache),
                    indent=2,
                )
            )
            return 0

        color = (
            not args.no_color
            and sys.stdout.isatty()
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
        )
        width = min(120, shutil.get_terminal_size(fallback=(88, 24)).columns)
        print(
            render_usage_dashboard(
                pool,
                state,
                usage,
                cache,
                width=width,
                color=color,
            )
        )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    root = _root_from_args(args)
    pool = load_pool(root)
    with locked_state(root) as state:
        if args.self and args.email:
            raise ValueError("pass either --self or an email, not both")
        # Known target -> no analytics. Auto-pick only then ranks usage.
        if args.self:
            account = resolve_locked_account(pool)
            if account is None:
                if len(pool.accounts) == 1:
                    account = pool.accounts[0]
                else:
                    raise ValueError(
                        "export --self needs a locked account\n"
                        "  fix: cursorpool mode you@company.com\n"
                        "  or:  cursorpool export you@company.com"
                    )
        elif args.email:
            account = find_account(pool, args.email)
        else:
            usage = _usage_map(pool, root)
            account = pick(pool, state, usage).account
        if args.record:
            record_selection(state, account.email)
        session_path = resolve_session_file(account, root)
        if not session_path.is_file():
            raise FileNotFoundError(
                f"missing session file for {account.email}: {session_path}\n"
                "  fix: re-import the account"
            )
        session = load_session(session_path)
    envelope = build_share_envelope(email=account.email, session=session, label=account.label)
    if args.env:
        print(export_shell_line(session))
    elif args.json:
        print(json.dumps(envelope, indent=2, sort_keys=True))
    else:
        print(encode_share_blob(envelope))
    print(f"# exported {account.email}", file=sys.stderr)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    root = _root_from_args(args)
    cmd = list(args.cmd)
    if not cmd:
        print("usage: cursorpool run -- <command...>", file=sys.stderr)
        return 2
    pool = load_pool(root)
    if not pool.accounts:
        print("error: pool empty — import an account first", file=sys.stderr)
        print("  cursorpool import --self --email you@company.com", file=sys.stderr)
        return 1
    from cursorpool.runner import is_protocol_mode, should_capture_output
    from cursorpool.state import load_state, save_state

    state = load_state(root)
    capture = should_capture_output(cmd, no_capture=args.no_capture)
    if args.email:
        fixed_account = find_account(pool, args.email)
    else:
        fixed_account = resolve_locked_account(pool)
    fixed_email = fixed_account.email if fixed_account is not None else None
    allow_failover = fixed_account is None
    usage = _usage_map(pool, root) if allow_failover else None

    def _persist(account_email: str) -> None:
        save_state(state, root)
        print(f"# cursorpool: account={account_email} (protocol exec)", file=sys.stderr)

    result = run_pooled(
        pool,
        state,
        cmd,
        root=root,
        account_email=fixed_email,
        usage=usage,
        max_failovers=args.max_failovers,
        capture=capture,
        allow_failover=allow_failover,
        on_before_exec=_persist if is_protocol_mode(cmd) else None,
    )
    save_state(state, root)
    if result.failovers:
        print(
            f"# cursorpool: {result.attempts} attempts, {result.failovers} failovers, "
            f"last={result.account_email}",
            file=sys.stderr,
        )
    return result.exit_code


def cmd_status(args: argparse.Namespace) -> int:
    root = _root_from_args(args)
    pool = load_pool(root)
    session_path = resolve_current_session_file(pool, root)
    print(f"home:     {root}")
    print(f"session:  {session_path}  ({'exists' if session_path.is_file() else 'missing'})")
    mode = f"locked: {pool.locked_email}" if pool.locked_email else "auto"
    print(f"mode:     {mode}")
    print(f"accounts: {len(pool.accounts)}  strategy: {pool.strategy}")
    cache = load_usage_cache(root)
    if cache:
        age = int(time.time() - float(cache.get("fetched_at") or 0))
        print(f"usage:    cache age {age}s  ttl {pool.usage_cache_ttl_seconds}s")
    else:
        print("usage:    no cache (run cursorpool refresh)")
    return cmd_list(args)


def cmd_restore(args: argparse.Namespace) -> int:
    root = _root_from_args(args)
    pool = load_pool(root)
    target = restore_backup(resolve_current_session_file(pool, root), root=root)
    print(f"restored {target} from backup")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cursorpool",
        description="Pool and load-balance Cursor CLI credentials (identity = email)",
    )
    p.add_argument("--version", action="version", version=f"cursorpool {__version__}")
    p.add_argument("--home", help="override CURSORPOOL_HOME / ~/.cursorpool")
    sub = p.add_subparsers(dest="command", required=True)
    add_p = sub.add_parser("add", help="add a session.json keyed by email")
    add_p.add_argument("--email", required=True, help="account email (unique key)")
    add_p.add_argument("--session", required=True, help="path to session.json, or - for stdin")
    add_p.add_argument("--label", default="")
    add_p.add_argument("--weight", type=float, default=1.0)
    add_p.add_argument("--notes", default="")
    add_p.add_argument("--force", action="store_true", help="replace existing email")
    add_p.set_defaults(func=cmd_add)
    imp_p = sub.add_parser(
        "import",
        help="import share blob, --self, or --session file (email required for file/self)",
    )
    imp_p.add_argument("blob", nargs="?", default=None, help="base64url blob, or - for stdin blob")
    imp_p.add_argument(
        "--self",
        action="store_true",
        help="import CURSOR_API_KEY or cursorpool session.json (requires --email)",
    )
    imp_p.add_argument(
        "--session",
        default=None,
        help="path to session.json (or - for stdin JSON); requires --email",
    )
    imp_p.add_argument(
        "--email",
        default=None,
        help="required with --self or --session",
    )
    imp_p.add_argument("--label", default="")
    imp_p.add_argument("--force", action="store_true", help="replace existing email")
    imp_p.add_argument("--json", action="store_true")
    imp_p.set_defaults(func=cmd_import)
    rm_p = sub.add_parser("remove", help="remove an account by email")
    rm_p.add_argument("email", help="full email (or unique local-part)")
    rm_p.add_argument("--json", action="store_true")
    rm_p.set_defaults(func=cmd_remove)
    update_p = sub.add_parser("update", help="change account enabled state or weight")
    update_p.add_argument("email", help="full email (or unique local-part)")
    enabled_group = update_p.add_mutually_exclusive_group()
    enabled_group.add_argument("--enable", action="store_true")
    enabled_group.add_argument("--disable", action="store_true")
    update_p.add_argument("--weight", type=float)
    update_p.add_argument("--json", action="store_true")
    update_p.set_defaults(func=cmd_update)
    mode_p = sub.add_parser("mode", help="show or change account selection mode")
    mode_p.add_argument(
        "target",
        nargs="?",
        default=None,
        help="auto or account email",
    )
    mode_p.add_argument("--json", action="store_true")
    mode_p.set_defaults(func=cmd_mode)
    ls_p = sub.add_parser("list", help="list accounts ranked least-used first")
    ls_p.add_argument("--json", action="store_true")
    ls_p.add_argument("--refresh", action="store_true")
    ls_p.set_defaults(func=cmd_list)
    stats_p = sub.add_parser("stats", help="print versioned machine-readable stats")
    stats_p.add_argument("--json", action="store_true")
    stats_p.add_argument("--refresh", action="store_true")
    stats_p.set_defaults(func=cmd_stats)
    rf_p = sub.add_parser("refresh", help="check credit usage availability (local-only for now)")
    rf_p.set_defaults(func=cmd_refresh)
    usage_p = sub.add_parser(
        "usage",
        help="show account credits and local sessions as a terminal dashboard",
    )
    usage_p.add_argument(
        "--json",
        action="store_true",
        help="emit structured JSON instead of the dashboard",
    )
    usage_p.add_argument(
        "--refresh",
        action="store_true",
        help="check credit usage availability before rendering",
    )
    usage_p.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI color",
    )
    usage_p.set_defaults(func=cmd_usage)
    ex_p = sub.add_parser("export", help="print portable share blob (email is identity)")
    ex_p.add_argument("email", nargs="?", default=None)
    ex_p.add_argument("--self", action="store_true", help="export locked account")
    ex_p.add_argument("--record", action="store_true")
    ex_p.add_argument("--env", action="store_true")
    ex_p.add_argument("--json", action="store_true")
    ex_p.set_defaults(func=cmd_export)
    run_p = sub.add_parser("run", help="run command with mode-selected auth")
    run_p.add_argument("--email", default=None)
    run_p.add_argument("--max-failovers", type=int, default=2)
    run_p.add_argument("--no-capture", action="store_true")
    run_p.add_argument("cmd", nargs=argparse.REMAINDER)
    run_p.set_defaults(func=cmd_run)
    st_p = sub.add_parser("status", help="show mode, pool, and session status")
    st_p.add_argument("--json", action="store_true")
    st_p.add_argument("--refresh", action="store_true")
    st_p.set_defaults(func=cmd_status)
    rs_p = sub.add_parser("restore", help="restore cursorpool session.json from backup")
    rs_p.set_defaults(func=cmd_restore)
    return p


# Built-in subcommands. Anything else at argv[0] is treated as agent args
# via the default: cursorpool [run-opts] [agent-args...]  ==  cursorpool run [run-opts] -- agent [agent-args...]
_SUBCOMMANDS = frozenset({
    "add", "import", "remove", "update", "list", "stats", "refresh", "export",
    "run", "status", "restore", "usage", "mode", "help",
})
_REMOVED_SUBCOMMANDS = frozenset({"next", "use"})


def _normalize_argv(argv: list[str]) -> list[str]:
    """Rewrite bare invocation into an explicit `run -- agent ...` form.

    Examples:
      cursorpool                         -> run -- agent
      cursorpool -p hi                  -> run -- agent -p hi
      cursorpool --email x@y -p hi       -> run --email x@y -- agent -p hi
      cursorpool --acp                 -> run -- agent --acp
      cursorpool run -- agent -p hi     -> unchanged
      cursorpool list                    -> unchanged
    """
    if not argv:
        return ["run", "--", "agent"]

    # Global flags that belong to cursorpool itself (before any subcommand).
    # --home / --version are on the root parser; leave them in place.
    i = 0
    prefix: list[str] = []
    while i < len(argv):
        a = argv[i]
        if a in {"--home"} and i + 1 < len(argv):
            prefix.extend([a, argv[i + 1]])
            i += 2
            continue
        if a.startswith("--home="):
            prefix.append(a)
            i += 1
            continue
        if a in {"--version", "-h", "--help"}:
            # Let argparse handle help/version on root or after rewrite.
            break
        break

    rest = argv[i:]
    if not rest:
        return prefix + ["run", "--", "agent"]

    head = rest[0]
    # Root help/version must stay on the root parser (not agent).
    if head in {"-h", "--help", "--version"}:
        return prefix + rest
    # Explicit subcommand (including `run`) — pass through.
    if head in _SUBCOMMANDS or head in _REMOVED_SUBCOMMANDS:
        return prefix + rest

    # Otherwise: optional run-specific flags, then agent args.
    # Run flags: --email, --max-failovers, --no-capture
    run_opts: list[str] = []
    j = 0
    while j < len(rest):
        a = rest[j]
        if a == "--email" and j + 1 < len(rest):
            run_opts.extend([a, rest[j + 1]])
            j += 2
            continue
        if a.startswith("--email="):
            run_opts.append(a)
            j += 1
            continue
        if a == "--max-failovers" and j + 1 < len(rest):
            run_opts.extend([a, rest[j + 1]])
            j += 2
            continue
        if a.startswith("--max-failovers="):
            run_opts.append(a)
            j += 1
            continue
        if a == "--no-capture":
            run_opts.append(a)
            j += 1
            continue
        # Stop at first non-run flag — remainder is agent argv.
        break

    agent_args = rest[j:]
    # If user already wrote `agent` as first arg, do not double-prefix.
    if agent_args and Path(agent_args[0]).name in {"agent", "agent.exe", "cursor-agent", "cursor-agent.exe"}:
        cmd = agent_args
    else:
        cmd = ["agent", *agent_args]
    return prefix + ["run", *run_opts, "--", *cmd]


def _removed_subcommand(argv: Sequence[str]) -> str | None:
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--home" and i + 1 < len(argv):
            i += 2
            continue
        if arg.startswith("--home="):
            i += 1
            continue
        break
    if i < len(argv) and argv[i] in _REMOVED_SUBCOMMANDS:
        return argv[i]
    return None


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    removed = _removed_subcommand(raw_argv)
    if removed is not None:
        print(
            f"error: '{removed}' was removed; use 'cursorpool mode' instead",
            file=sys.stderr,
        )
        return 2
    argv = _normalize_argv(raw_argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run" and args.cmd and args.cmd[0] == "--":
        args.cmd = args.cmd[1:]
    try:
        return int(args.func(args))
    except BrokenPipeError:
        return 0
    except (KeyError, ValueError, FileNotFoundError, RuntimeError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
