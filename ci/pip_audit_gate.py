"""Fail CI only for vulnerabilities that have a fix available.

Usage: python ci/pip_audit_gate.py reports/pip-audit.json

Vulnerabilities without a fixed release are printed as warnings (nothing to
upgrade to yet) but do not fail the build.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    report = Path(argv[1])
    if not report.exists():
        print(f"pip-audit gate: {report} not found - pip-audit did not run")
        return 1
    data = json.loads(report.read_text(encoding="utf-8"))
    fixable: list[str] = []
    unfixable: list[str] = []
    for dep in data.get("dependencies", []):
        if "skip_reason" in dep:
            print(f"WARNING: pip-audit skipped {dep.get('name')}: {dep['skip_reason']}")
        for vuln in dep.get("vulns", []):
            line = f"{dep['name']}=={dep['version']}: {vuln['id']}"
            if vuln.get("fix_versions"):
                fixable.append(f"{line} (fix: {', '.join(vuln['fix_versions'])})")
            else:
                unfixable.append(line)
    for line in unfixable:
        print(f"WARNING (no fix available yet): {line}")
    if fixable:
        print("FAIL: vulnerabilities with an available fix - upgrade and re-lock (make lock):")
        for line in fixable:
            print(f"  {line}")
        return 1
    print(f"pip-audit gate passed ({len(unfixable)} unfixable advisories)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
