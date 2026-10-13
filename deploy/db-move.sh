#!/usr/bin/env bash
# Move the bridge database (schema "bridge") to another PostgreSQL host.
#
#   db-move.sh --target-secrets /path/target-db.env [--dry-run] [--overwrite-target]
#              [--force-market-hours --reason "..."]
#   db-move.sh --revert          # switch back to db.env.previous (fallback window)
#
# The source database is only read, never modified or deleted. See docs/RUNBOOK.md
# "Moving to another database host". Exit codes: 0 ok, 1 failed (source restored),
# 2 preflight refused (nothing changed), 3 manual action required.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$here/ops.sh" dbmove "$@"
