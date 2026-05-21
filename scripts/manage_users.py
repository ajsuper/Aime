#!/usr/bin/env python3
"""Aime user-account admin tool.

Manages the account lifecycle: soft-delete (a reversible deactivation), restore,
and the permanent purge of accounts whose grace period has expired. It is a
thin wrapper over `aime.auth.LocalAuthBackend` and `aime.accounts`, which hold
all the logic — a future admin web dashboard would wrap the same functions.

The model, in short:

  * `delete` soft-deletes an account — it stops working and vanishes from
    listings, but its data directory is left intact. The user (or admin) can
    bring it back with `restore` during the grace period.
  * `purge` permanently removes accounts that have been soft-deleted longer
    than the grace period (default 30 days): a final backup zip is written,
    then the DB row and data directory are deleted.

See docs/access-control.md for the deployment model.

Examples:
    # See active accounts and ones pending purge
    ./scripts/manage_users.py list

    # Soft-delete an account (reversible)
    ./scripts/manage_users.py delete testuser

    # Bring a soft-deleted account back
    ./scripts/manage_users.py restore testuser

    # Show what a purge would remove, without removing anything
    ./scripts/manage_users.py purge --dry-run

    # Permanently purge accounts soft-deleted 30+ days ago
    ./scripts/manage_users.py purge
"""

import os
import sys
import argparse

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# This script's dependencies (argon2-cffi, cryptography — pulled in by
# aime.auth) live only in the uv-managed virtualenv that install.sh builds;
# the host Python itself has nothing installed. Re-exec under that venv so the
# admin can run `./scripts/manage_users.py ...` directly, the same way the .sh
# service scripts invoke .venv/bin/python. Skipped when the venv is absent
# (e.g. inside the Docker image, where the deps are installed system-wide).
_VENV_PYTHON = os.path.join(_REPO_ROOT, ".venv", "bin", "python")
if os.environ.get("_AIME_REEXEC") != "1" and os.path.exists(_VENV_PYTHON):
    os.environ["_AIME_REEXEC"] = "1"
    os.execv(_VENV_PYTHON, [_VENV_PYTHON, os.path.abspath(__file__), *sys.argv[1:]])

# Allow running directly from a checkout: make the `aime` package importable.
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

try:
    from aime import config as aime_config  # noqa: E402
    from aime import auth as _auth  # noqa: E402
    from aime import accounts as _accounts  # noqa: E402
except ModuleNotFoundError as exc:  # noqa: E402
    sys.stderr.write(
        f"Error: missing dependency ({exc.name}). "
        "Run ./scripts/install.sh to build the virtualenv first.\n"
    )
    raise SystemExit(1)


def _backend() -> _auth.LocalAuthBackend:
    return _auth.LocalAuthBackend(os.path.join(aime_config.DATABASE_DIR, "auth.sql"))


def cmd_list(args: argparse.Namespace) -> int:
    backend = _backend()

    users = backend.list_users()
    print(f"Active users ({len(users)}):")
    if not users:
        print("  (none)")
    for u in users:
        flag = "send" if u.api_access else "no-send"
        print(f"  #{u.id:<4} {u.username:<24} [{flag}]")

    pending = _accounts.list_pending(backend, grace_days=args.days)
    print(f"\nSoft-deleted users ({len(pending)}):")
    if not pending:
        print("  (none)")
    for p in pending:
        if p.expired:
            state = "PAST GRACE — eligible for purge"
        else:
            state = f"{max(0, args.days - p.days_deleted)} day(s) until purge"
        print(f"  #{p.user.id:<4} {p.user.username:<24} "
              f"deleted {p.deleted_at} ({p.days_deleted}d ago) — {state}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    if _backend().soft_delete_by_username(args.username):
        print(f"Soft-deleted {args.username!r}. It is hidden and disabled but "
              f"recoverable — restore it with:\n"
              f"  ./scripts/manage_users.py restore {args.username}")
        return 0
    print(f"No such active user: {args.username!r} "
          f"(already deleted, or never existed).", file=sys.stderr)
    return 1


def cmd_restore(args: argparse.Namespace) -> int:
    if _backend().restore_by_username(args.username):
        print(f"Restored {args.username!r}; the account is active again.")
        return 0
    print(f"No soft-deleted user named {args.username!r} to restore.",
          file=sys.stderr)
    return 1


def cmd_purge(args: argparse.Namespace) -> int:
    backend = _backend()
    expired = [p for p in _accounts.list_pending(backend, grace_days=args.days)
               if p.expired]

    if not expired:
        print(f"Nothing to purge — no account has been soft-deleted for "
              f"{args.days}+ days.")
        return 0

    print(f"{len(expired)} account(s) past the {args.days}-day grace period:")
    for p in expired:
        print(f"  #{p.user.id:<4} {p.user.username:<24} "
              f"deleted {p.deleted_at} ({p.days_deleted}d ago)")

    if args.dry_run:
        print("\n(dry run — nothing was removed)")
        return 0

    if not args.yes:
        reply = input(f"\nPermanently purge these {len(expired)} account(s)? "
                       f"A final backup is written first. Type 'yes': ")
        if reply.strip().lower() != "yes":
            print("Aborted.")
            return 1

    results = _accounts.purge_expired(backend, grace_days=args.days)
    print()
    for p, backup_path in results:
        where = backup_path if backup_path else "no data directory"
        print(f"Purged #{p.user.id} {p.user.username!r} — backup: {where}")
    print(f"\nPurged {len(results)} account(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="manage_users.py",
        description="Manage Aime user accounts: soft-delete, restore, purge.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list active and soft-deleted users")
    p_list.add_argument("--days", type=int, default=_accounts.DEFAULT_GRACE_DAYS,
                        help="grace period in days, for the purge countdown "
                             f"(default {_accounts.DEFAULT_GRACE_DAYS})")
    p_list.set_defaults(func=cmd_list)

    p_del = sub.add_parser("delete", help="soft-delete an account (reversible)")
    p_del.add_argument("username")
    p_del.set_defaults(func=cmd_delete)

    p_res = sub.add_parser("restore", help="restore a soft-deleted account")
    p_res.add_argument("username")
    p_res.set_defaults(func=cmd_restore)

    p_purge = sub.add_parser(
        "purge", help="permanently remove accounts past the grace period")
    p_purge.add_argument("--days", type=int, default=_accounts.DEFAULT_GRACE_DAYS,
                         help="grace period in days — only accounts soft-deleted "
                              f"at least this long ago are purged "
                              f"(default {_accounts.DEFAULT_GRACE_DAYS})")
    p_purge.add_argument("--dry-run", action="store_true",
                         help="show what would be purged, remove nothing")
    p_purge.add_argument("--yes", action="store_true",
                         help="skip the confirmation prompt")
    p_purge.set_defaults(func=cmd_purge)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
