import argparse
import sys
from pathlib import Path

import pandas as pd
from sklearn.model_selection import StratifiedGroupKFold


# ============================================================
# Project root
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config import PROCESSED_DATA_DIR
from src.evaluate_real_pairs import load_inputs


# ============================================================
# Settings
#
# Reuse real-pair selection and leakage checks without computing
# embeddings or calling APIs. Keep every normalized client in a
# single split, including clients with multiple correct expenses.
# Stratify by source Excel to approximately balance each program.
# ============================================================

OUTPUT_PATH = PROCESSED_DATA_DIR / "real_pairs_split.csv"
SEED = 42
SPLITS = ["real_validation", "real_test"]
OUTPUT_COLUMNS = [
    "real_pair_id", "split", "source_excel", "excel_row",
    "program_title", "subprogram_id", "client_expense_title",
    "client_expense_description", "program_expense_id",
    "program_category", "program_subcategory", "program_expense_description",
]


# ============================================================
# Grouped split and assertions
# ============================================================

def split_pairs(pairs):
    pairs = pairs.copy().reset_index(drop=True)
    # Encode tuple keys as one-dimensional integer groups. Passing a
    # list of title/description tuples directly can create a 2-D array.
    groups, _ = pd.factorize(pairs["_client_key"], sort=False)
    splitter = StratifiedGroupKFold(
        n_splits=2, shuffle=True, random_state=SEED,
    )
    pairs["split"] = ""
    assigned = set()
    for fold, (_, indices) in enumerate(splitter.split(
        pairs, y=pairs["source_excel"], groups=groups,
    )):
        assert assigned.isdisjoint(indices), "A pair was assigned to multiple folds."
        assigned.update(indices)
        pairs.loc[indices, "split"] = SPLITS[fold]

    validation = pairs.loc[pairs["split"].eq("real_validation")]
    test = pairs.loc[pairs["split"].eq("real_test")]
    assert set(validation["_client_key"]).isdisjoint(test["_client_key"]), (
        "Client overlap between real_validation and real_test."
    )
    programs = set(pairs["source_excel"])
    for name, frame in [("real_validation", validation), ("real_test", test)]:
        assert set(frame["source_excel"]) == programs, (
            f"Some programs are absent from {name}: "
            f"{sorted(programs - set(frame['source_excel']))}"
        )
    assert len(validation) + len(test) == len(pairs) == len(assigned), (
        "The two splits must contain every selected pair exactly once."
    )
    assert pairs["split"].isin(SPLITS).all()
    pairs.insert(0, "real_pair_id", [f"R{index:04d}" for index in range(1, len(pairs) + 1)])
    assert pairs["real_pair_id"].is_unique
    return pairs


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Split real pairs by client and stratify by program.")
    parser.add_argument("--dry-run", action="store_true", help="Print the split and checks without writing files.")
    parser.add_argument("--overwrite", action="store_true", help="Replace the existing output CSV.")
    args = parser.parse_args()
    if OUTPUT_PATH.exists() and not args.overwrite and not args.dry_run:
        raise FileExistsError(f"Output exists: {OUTPUT_PATH}. Use --overwrite to replace it.")

    pairs, _, _, _ = load_inputs()
    result = split_pairs(pairs)

    print("=" * 70)
    print("REAL PAIRS SPLIT")
    print("=" * 70)
    print("Pairs and unique clients per split:")
    summary = result.groupby("split").agg(
        pairs=("real_pair_id", "size"),
        unique_clients=("_client_key", "nunique"),
    ).reindex(SPLITS)
    print(summary.to_string())
    print()
    print("Program (source Excel) x split:")
    print(pd.crosstab(result["source_excel"], result["split"]).reindex(
        columns=SPLITS, fill_value=0,
    ).to_string())
    print()
    print("All split and client-overlap assertions passed.")

    if args.dry_run:
        print("Dry run complete: no output file written.")
        return

    # Build the CSV before touching its destination. Exclusive creation
    # also protects against a file created by another process mid-run.
    csv_bytes = result[OUTPUT_COLUMNS].to_csv(index=False).encode("utf-8-sig")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("wb" if args.overwrite else "xb") as handle:
        handle.write(csv_bytes)
    print("Saved:", OUTPUT_PATH)


if __name__ == "__main__":
    main()
