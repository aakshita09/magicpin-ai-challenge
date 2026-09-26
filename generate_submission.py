"""
Generate submission.jsonl from the 30 canonical (merchant, trigger[, customer])
test pairs, calling composer.compose() directly (no HTTP needed for this step).

Usage:
    python generate_submission.py /path/to/expanded/dataset > submission.jsonl
"""
import json
import sys
from pathlib import Path

from composer import compose


def load(base: Path, kind: str, id_: str):
    return json.loads((base / kind / f"{id_}.json").read_text())


def main():
    base = Path(sys.argv[1] if len(sys.argv) > 1 else "./expanded")
    pairs = json.loads((base / "test_pairs.json").read_text())["pairs"]

    cat_cache = {}
    out_lines = []
    for p in pairs:
        trigger = load(base, "triggers", p["trigger_id"])
        merchant = load(base, "merchants", p["merchant_id"])
        cat_slug = merchant["category_slug"]
        if cat_slug not in cat_cache:
            cat_cache[cat_slug] = load(base, "categories", cat_slug)
        category = cat_cache[cat_slug]
        customer = load(base, "customers", p["customer_id"]) if p.get("customer_id") else None

        composed = compose(category, merchant, trigger, customer)
        out_lines.append(json.dumps({
            "test_id": p["test_id"],
            "body": composed["body"],
            "cta": composed["cta"],
            "send_as": composed["send_as"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        }, ensure_ascii=False))

    print("\n".join(out_lines))


if __name__ == "__main__":
    main()
