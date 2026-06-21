import argparse
import collections
import csv
import json
import sys


LEGACY_FIELDS = ["type", "externalId", "title", "status", "source", "addedAt", "error"]


def load_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return [
            {field: row.get(field, "") for field in LEGACY_FIELDS}
            for row in csv.DictReader(handle)
        ]


def main():
    parser = argparse.ArgumentParser(description="Verify that a Driparr queue migration preserved legacy fields.")
    parser.add_argument("before")
    parser.add_argument("after")
    args = parser.parse_args()
    before = load_rows(args.before)
    after = load_rows(args.after)
    identical = before == after
    differences = []
    for index, (old, new) in enumerate(zip(before, after)):
        changed = {field: {"before": old[field], "after": new[field]} for field in LEGACY_FIELDS if old[field] != new[field]}
        if changed:
            differences.append({"index": index, "externalId": old.get("externalId", ""), "changes": changed})
        if len(differences) >= 5:
            break
    print(
        json.dumps(
            {
                "ok": identical,
                "beforeCount": len(before),
                "afterCount": len(after),
                "beforeStatuses": dict(collections.Counter(row["status"] for row in before)),
                "afterStatuses": dict(collections.Counter(row["status"] for row in after)),
                "differences": differences,
            },
            indent=2,
        )
    )
    return 0 if identical else 1


if __name__ == "__main__":
    sys.exit(main())
