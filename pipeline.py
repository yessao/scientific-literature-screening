"""Screen batches of literature records with the Anthropic API.

Run specific-heat-filter.py first: it creates candidates.csv and batches/batch_*.txt.

Usage:
    python pipeline.py --dry-run          # print the first prompt, no API call
    python pipeline.py --limit 2          # screen only the first 2 batches (test)
    python pipeline.py                    # screen everything

Setup (once):
    pip install anthropic pandas
    export ANTHROPIC_API_KEY="your-key"   # never write the key in the code

What it does:
  1. Builds a prompt for each batch from prompts/screen.md + criteria.md.
  2. Sends it to the model and asks for a JSON answer.
  3. Checks that the model returned exactly the record IDs it was sent
     (one retry if not) and saves the raw answers in screening/raw/.
  4. Merges everything with candidates.csv into screening/screening_results.csv,
     with a needs_check column and an empty human_decision column for you.

If you stop the script, run it again: finished batches are skipped.
The model only judges the records you give it. It never supplies references.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import pandas as pd

DEFAULT_MODEL = "claude-sonnet-5-5"

OUTPUT_FORMAT = (
    "Return ONLY a JSON array, with no text before or after it and no code "
    "fences. One object per record, in the same order as the input, with "
    "exactly these keys:\n"
    '"id" (integer, the number in brackets), '
    '"date_ok" ("yes", "no" or "unknown"), '
    '"superconductivity" ("yes" or "no"), '
    '"specific_heat_score" (0, 1, 2 or 3), '
    '"type" ("experimental", "theoretical", "both" or "unclear"), '
    '"material" (string, empty if not stated), '
    '"needs_full_text" ("yes" or "no"), '
    '"uncertain" ("yes" or "no"), '
    '"reason" (one sentence).'
)


def build_prompt(template, criteria, records):
    return (
        template.replace("{{CRITERIA}}", criteria)
        .replace("{{OUTPUT_FORMAT}}", OUTPUT_FORMAT)
        .replace("{{RECORDS}}", records)
    )


def record_ids(text):
    """IDs of the records in a batch file (lines that start with [id])."""
    return [int(m) for m in re.findall(r"^\[(\d+)\]", text, flags=re.M)]


def parse_json(text):
    """Extract the JSON array from the model's answer."""
    text = text.strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("no JSON array found")
    return json.loads(text[start:end + 1])


def call_model(client, model, prompt, max_tokens=4000):
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in response.content if b.type == "text")


def screen_batch(client, model, prompt, sent_ids, raw_stem):
    """Return a list of result dicts, or None if the answer was unusable twice."""
    for attempt in (1, 2):
        text = call_model(client, model, prompt)
        Path(f"{raw_stem}_attempt{attempt}.txt").write_text(text, encoding="utf-8")
        try:
            results = parse_json(text)
            got = [int(r["id"]) for r in results]
        except (ValueError, KeyError, TypeError):
            continue
        if sorted(got) == sorted(sent_ids):
            return results
    return None


def build_table(raw_dir, candidates_path, out_path):
    rows = []
    for path in sorted(raw_dir.glob("batch_*.json")):
        rows += json.loads(path.read_text(encoding="utf-8"))
    cand = pd.read_csv(candidates_path)
    if not rows:
        print("No results to merge yet.")
        return None
    res = pd.DataFrame(rows)
    res["id"] = pd.to_numeric(res["id"], errors="coerce")
    merged = cand.merge(res.drop_duplicates("id"), on="id", how="left")

    for col in ("uncertain", "needs_full_text"):
        if col not in merged:
            merged[col] = ""
    score = pd.to_numeric(merged["specific_heat_score"], errors="coerce")
    flag = (
        merged["uncertain"].eq("yes")
        | merged["needs_full_text"].eq("yes")
        | score.isin([1, 2])
    )
    merged["needs_check"] = flag.map({True: "yes", False: ""})
    merged.loc[score.isna(), "needs_check"] = "not screened"
    merged["human_decision"] = ""

    front = ["id", "title", "year", "journal", "doi", "specific_heat_score",
             "type", "material", "needs_check", "reason", "human_decision"]
    front = [c for c in front if c in merged.columns]
    merged = merged[front + [c for c in merged.columns if c not in front]]
    merged.to_csv(out_path, index=False)
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batches-dir", default="batches")
    ap.add_argument("--candidates", default="candidates.csv")
    ap.add_argument("--prompt", default="prompts/screen.md")
    ap.add_argument("--criteria", default="criteria.md")
    ap.add_argument("--outdir", default="screening")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, help="screen only the first N batches")
    ap.add_argument("--dry-run", action="store_true", help="print the first prompt and stop")
    args = ap.parse_args()

    batches = sorted(Path(args.batches_dir).glob("batch_*.txt"))
    if args.limit:
        batches = batches[:args.limit]
    if not batches:
        sys.exit(f"No batch files found in {args.batches_dir}/. Run specific-heat-filter.py first.")

    template = Path(args.prompt).read_text(encoding="utf-8")
    criteria = Path(args.criteria).read_text(encoding="utf-8")
    outdir = Path(args.outdir)
    raw_dir = outdir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        prompt = build_prompt(template, criteria, batches[0].read_text(encoding="utf-8"))
        print(prompt)
        print(f"\n[dry run] {len(batches)} batches; first prompt is {len(prompt)} characters.")
        return

    try:
        import anthropic
    except ImportError:
        sys.exit("Install the SDK first:  pip install anthropic")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set the ANTHROPIC_API_KEY environment variable first.")
    client = anthropic.Anthropic(max_retries=5)

    failed = []
    for i, path in enumerate(batches, 1):
        out_json = raw_dir / f"{path.stem}.json"
        if out_json.exists():
            print(f"[{i}/{len(batches)}] {path.name}: already done, skipping")
            continue
        records = path.read_text(encoding="utf-8")
        sent = record_ids(records)
        print(f"[{i}/{len(batches)}] {path.name}: {len(sent)} records ... ", end="", flush=True)
        try:
            results = screen_batch(
                client, args.model, build_prompt(template, criteria, records),
                sent, raw_dir / path.stem,
            )
        except Exception as exc:  # API or network problem: keep going
            print(f"ERROR: {exc}")
            failed.append(path.name)
            continue
        if results is None:
            print("FAILED (unusable answer twice; see the files in raw/)")
            failed.append(path.name)
        else:
            out_json.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
            print("ok")

    merged = build_table(raw_dir, args.candidates, outdir / "screening_results.csv")
    if merged is not None:
        print("\nScreened records by specific-heat score:")
        print(merged["specific_heat_score"].value_counts(dropna=False).sort_index().to_string())
        print(f"\nNeed your check: {int((merged['needs_check'] == 'yes').sum())}")
        print(f"Table written to {outdir / 'screening_results.csv'}")
    if failed:
        print("\nBatches to rerun (just run the script again):", ", ".join(failed))


if __name__ == "__main__":
    main()
