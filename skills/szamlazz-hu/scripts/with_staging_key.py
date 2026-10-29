#!/usr/bin/env python3
"""Run a child with the staging Agent key from the user's OS keyring."""

import argparse
import os
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report availability only")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not args.check and not command:
        parser.error("provide --check or -- COMMAND [ARG ...]")
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "service", "szamlazz.hu", "account", "chronosync-staging-teszt-ceg"],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        print("Staging keyring is unavailable or locked.", file=sys.stderr)
        return 1
    key = result.stdout.strip()
    if result.returncode or not key:
        print("Staging Agent key is unavailable; unlock or configure the keyring.", file=sys.stderr)
        return 1
    if args.check:
        print("Teszt cég staging Agent key is available in the OS keyring.")
        return 0
    child_env = os.environ.copy()
    child_env["SZAMLAZZ_AGENT_KEY"] = key
    os.execvpe(command[0], command, child_env)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
