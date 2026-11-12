"""Prepare exported Web of Science / Scopus records for screening in chat.

Usage:
    python prepare_batches.py records.csv
    python prepare_batches.py records.csv --batch-size 15 --all

What it does:
  1. Reads the export (CSV or tab-delimited) and renames columns to a common set.
  2. Keeps only records from the chosen years and removes duplicates.
  3. Flags records whose title/abstract mention specific heat / heat capacity
     and records that mention superconductivity. It does NOT delete the
     others: a paper can report specific heat without saying so in the abstract.
  4. Writes candidates.csv (everything kept, with the flags) and text files
     in batches/ that you can paste under the screening prompt.

By default only keyword hits go into the batches. Use --all to include
every record.
"""
import argparse
import csv
import re
from pathlib import Path

import pandas as pd

COLUMN_MAP = {
    "title": ["Title", "Article Title", "TI"],
    "abstract": ["Abstract", "AB"],
    "year": ["Year", "Publication Year", "PY"],
    "doi": ["DOI", "DI"],
    "journal": ["Source title", "Source Title", "SO"],
    "authors": ["Authors", "AU", "Author full names"],
}

HEAT_PATTERN = re.compile(
    r"specific[\s-]*heats?|heat[\s-]*capacit|calorimetr|sommerfeld", re.I
)
SC_PATTERN = re.compile(r"superconduct", re.I)


def read_export(path):
    """Read a CSV or tab-delimited export, guessing the separator."""
    try:
        return pd.read_csv(path, sep=None, engine="python", encoding="utf-8-sig")
    except Exception:
        # Some tab-delimited exports contain stray quotation marks.
        return pd.read_csv(
            path, sep="\t", quoting=csv.QUOTE_NONE, encoding="utf-8-sig"
        )


def normalize_columns(df):
    out = pd.DataFrame()
    for target, options in COLUMN_MAP.items():
        found = next((c for c in options if c in df.columns), None)
        out[target] = df[found] if found else ""
    out = out.fillna("").astype(str)
    out["year"] = pd.to_numeric(out["year"], errors="coerce")
    return out


def format_record(i, row):
    abstract = row["abstract"].strip() or "(no abstract in the export)"
    return (
        f"[{i}] TITLE: {row['title'].strip()}\n"
        f"JOURNAL: {row['journal'].strip()} | YEAR: {row['year']} | DOI: {row['doi'].strip()}\n"
        f"ABSTRACT: {abstract}\n"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_file")
    ap.add_argument("--first-year", type=int, default=2000)
    ap.add_argument("--last-year", type=int, default=2001)
    ap.add_argument("--batch-size", type=int, default=15)
    ap.add_argument("--all", action="store_true", help="batch all records, not only keyword hits")
    ap.add_argument("--outdir", default=".")
    args = ap.parse_args()

    df = normalize_columns(read_export(args.csv_file))
    n_start = len(df)

    df = df[df["year"].between(args.first_year, args.last_year)]
    n_years = len(df)

    # Remove duplicates: by DOI when there is one, otherwise by normalized title.
    df = df.assign(
        _key=df["doi"].str.lower().str.strip().where(
            df["doi"].str.strip() != "",
            df["title"].str.lower().str.replace(r"\W+", " ", regex=True).str.strip(),
        )
    )
    df = df.drop_duplicates("_key").drop(columns="_key").reset_index(drop=True)

    text = df["title"] + " " + df["abstract"]
    df["heat_keyword"] = text.str.contains(HEAT_PATTERN)
    df["sc_keyword"] = text.str.contains(SC_PATTERN)
    df.insert(0, "id", range(1, len(df) + 1))

    outdir = Path(args.outdir)
    (outdir / "batches").mkdir(parents=True, exist_ok=True)
    df.to_csv(outdir / "candidates.csv", index=False)

    to_batch = df if args.all else df[df["heat_keyword"]]
    records = [format_record(r["id"], r) for _, r in to_batch.iterrows()]
    n_batches = 0
    for start in range(0, len(records), args.batch_size):
        n_batches += 1
        path = outdir / "batches" / f"batch_{n_batches:03d}.txt"
        path.write_text("\n".join(records[start:start + args.batch_size]), encoding="utf-8")

    print(f"Records read:                 {n_start}")
    print(f"In {args.first_year}-{args.last_year}:                  {n_years}")
    print(f"After removing duplicates:    {len(df)}")
    print(f"Mention specific heat:        {int(df['heat_keyword'].sum())}")
    print(f"Mention superconductivity:    {int(df['sc_keyword'].sum())}")
    print(f"Batches written:              {n_batches} (in {outdir / 'batches'})")


if __name__ == "__main__":
    main()
