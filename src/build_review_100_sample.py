import argparse
import sys
from pathlib import Path

import pandas as pd


# ============================================================
# Project root
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

sys.path.insert(
    0,
    str(BASE_DIR),
)


from config import (
    OUTPUT_DATA_DIR,
)

from src.build_evaluation_set import (
    load_program_expenses,
)

from src.build_annotation_sample import (
    EASY_PATH,
    OUR_LABELS,
    POSITIVES_PATH,
    SEMI_HARD_PATH,
    load_non_lexical_pairs,
    load_standard_pairs,
    pair_key,
    stratified_sample,
)


# ============================================================
# Review sample (100 pairs)
#
# A small blind sample of existing synthetic pairs, reviewed
# against the matching guidelines (with the program description).
#
# Two files:
#   - review_100_pairs_without_labels.xlsx: what the reviewer
#     sees. No label, no pair category, no source.
#   - review_100_pairs_our_labels.xlsx: review_id ->
#     source_pair_id, pair_category, our_label. Used only after
#     the review.
#
# No new pairs, no API call, existing data files are not modified.
# ============================================================

SEED = 42

SAMPLE_SIZES = {
    "positive": 35,
    "easy_negative": 30,
    "semi_hard_negative": 15,
    "non_lexical_positive": 20,
}

# What the reviewer (Claude) sees: pairs without any label.
INPUT_OUTPUT_PATH = OUTPUT_DATA_DIR / "review_100_pairs_without_labels.xlsx"

# Our labels for the same pairs, used only after the review.
KEY_OUTPUT_PATH = OUTPUT_DATA_DIR / "review_100_pairs_our_labels.xlsx"

INPUT_COLUMNS = [
    "review_id",
    "client_expense_title",
    "client_expense_description",
    "expense_class",
    "program_category",
    "program_subcategory",
    "program_expense_description",
]

KEY_COLUMNS = [
    "review_id",
    "source_pair_id",
    "pair_category",
    "our_label",
]


# ============================================================
# Main
# ============================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description="Build the blind 100-pair review sample."
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing existing review files.",
    )

    args = parser.parse_args()

    for path in [INPUT_OUTPUT_PATH, KEY_OUTPUT_PATH]:
        if path.exists() and not args.overwrite:
            raise FileExistsError(
                f"{path} already exists. Use --overwrite to replace it."
            )

    program_df = load_program_expenses()

    non_lexical_df, non_lexical_excluded = load_non_lexical_pairs(
        program_df
    )

    sources = {
        "positive": load_standard_pairs(POSITIVES_PATH, "positive"),
        "easy_negative": load_standard_pairs(EASY_PATH, "easy_negative"),
        "semi_hard_negative": load_standard_pairs(SEMI_HARD_PATH, "semi_hard_negative"),
        "non_lexical_positive": non_lexical_df,
    }

    # --------------------------------------------------------
    # Remove duplicates, then sample each category
    # (stratified by expense class)
    # --------------------------------------------------------

    sampled = []

    seen = set()

    for category, df in sources.items():

        df = df[~df.apply(pair_key, axis=1).duplicated()]

        df = df[~df.apply(pair_key, axis=1).isin(seen)]

        size = SAMPLE_SIZES[category]

        if len(df) < size:
            print(f"Warning: {category} has only {len(df)} pairs (asked {size}).")

        sample = stratified_sample(df, size, "expense_class")

        sample = sample.rename(columns={"category": "pair_category"})

        sample["our_label"] = OUR_LABELS[category]

        seen.update(sample.apply(pair_key, axis=1))

        sampled.append(sample)

    review_df = (
        pd.concat(sampled, ignore_index=True)
        .sample(frac=1, random_state=SEED)
        .reset_index(drop=True)
    )

    review_df["review_id"] = [
        f"R{number:03d}"
        for number in range(1, len(review_df) + 1)
    ]

    # --------------------------------------------------------
    # Safety assertions
    # --------------------------------------------------------

    assert review_df["review_id"].is_unique

    assert not review_df.apply(pair_key, axis=1).duplicated().any()

    # The reviewer file must not reveal labels, categories or sources.
    assert not {"our_label", "pair_category", "source_pair_id"} & set(INPUT_COLUMNS)

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    OUTPUT_DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    review_df[INPUT_COLUMNS].to_excel(
        INPUT_OUTPUT_PATH,
        sheet_name="review_input",
        index=False,
        engine="openpyxl",
    )

    review_df[KEY_COLUMNS].to_excel(
        KEY_OUTPUT_PATH,
        sheet_name="review_key",
        index=False,
        engine="openpyxl",
    )

    # --------------------------------------------------------
    # Summary (category counts are printed here only, not in
    # the reviewer file)
    # --------------------------------------------------------

    print("=" * 70)
    print("REVIEW SAMPLE")
    print("=" * 70)
    print("Pairs:", len(review_df))
    print()
    print("Per category:")
    print(review_df["pair_category"].value_counts().to_string())
    print()
    print("Our label:")
    print(review_df["our_label"].value_counts().to_string())
    print()
    print("Per expense class:")
    print(review_df["expense_class"].value_counts().to_string())
    print()
    print("Non-lexical pairs excluded (no unique program expense):", non_lexical_excluded)
    print()
    print("Saved:", INPUT_OUTPUT_PATH)
    print("Saved:", KEY_OUTPUT_PATH)
    print("=" * 70)


if __name__ == "__main__":
    main()
