"""Inventory product acceptance cases; never infer a pass from test counts.

`--initialize` writes an all-pending ledger once. The default command checks
that every documented case still exists and passed cases cite evidence.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIEFS = ROOT / "docs" / "product-briefs"
LEDGER = BRIEFS / "acceptance.json"
PATTERN = re.compile(r"^\| ((?:G|READ|SHARE|ASK|AI|SEARCH|FOLLOW|OFFLINE|SYNC)\d+) \| (.*?) \| (.*?) \|$", re.M)


def requirements() -> list[dict[str, str]]:
    cases = []
    for path in sorted(BRIEFS.glob("*.md")):
        for match in PATTERN.finditer(path.read_text(encoding="utf-8")):
            case_id, scenario, expected = match.groups()
            cases.append({"id": case_id, "document": path.name,
                          "scenario": scenario, "expected": expected})
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("duplicate acceptance case")
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args()
    documented = requirements()
    if args.initialize:
        with LEDGER.open("x", encoding="utf-8") as handle:
            json.dump({"version": 1, "baseline": "094f03ac673b84e571052c2dda1910b67e7b98e8",
                       "cases": [{**case, "status": "pending", "actual": "",
                                  "evidence": [], "implementation_commit": ""}
                                 for case in documented]}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    recorded = {case["id"]: case for case in ledger["cases"]}
    if len(recorded) != len(ledger["cases"]):
        raise ValueError("duplicate ledger case")
    if set(recorded) != {case["id"] for case in documented}:
        raise ValueError("ledger and requirements differ")
    counts: dict[str, int] = {}
    for requirement in documented:
        case = recorded[requirement["id"]]
        if any(case[key] != value for key, value in requirement.items()):
            raise ValueError(f"requirement changed: {case['id']}")
        status = case["status"]
        if status not in {"pending", "in_progress", "passed", "failed", "blocked"}:
            raise ValueError(f"invalid status: {case['id']}")
        if status == "passed":
            if not all(case.get(key) for key in ("actual", "evidence", "implementation_commit")):
                raise ValueError(f"pass without evidence: {case['id']}")
            for ref in case["evidence"]:
                evidence = (ROOT / ref).resolve()
                if not evidence.is_relative_to(ROOT) or not evidence.is_file():
                    raise ValueError(f"missing local evidence: {case['id']}")
        counts[status] = counts.get(status, 0) + 1
    print(json.dumps({"documented_cases": len(documented), "status_counts": counts,
                      "note": "Inventory validation does not constitute product acceptance."}))


if __name__ == "__main__":
    main()
