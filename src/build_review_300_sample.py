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
    # Shared seed (42) for stratified sampling and the final shuffle.
    SEED,
    SEMI_HARD_PATH,
    load_non_lexical_pairs,
    load_standard_pairs,
    pair_key,
    stratified_sample,
)


# ============================================================
# Review sample (300 pairs)
#
# A new blind sample of existing synthetic pairs, reviewed
# against the matching guidelines (with the program description).
# Only pairs from the annotation sample are eligible; pairs
# already included in the 100-pair review are excluded.
#
# Two files:
#   - review_300_pairs_without_labels.xlsx: what the reviewer
#     sees. No label, no pair category, no source.
#   - review_300_pairs_our_labels.xlsx: review_id ->
#     source_pair_id, pair_category, our_label. Used only after
#     the review.
#
# No new pairs, no API call, existing data files are not modified.
# ============================================================

SAMPLE_SIZES = {
    "positive": 100,
    "easy_negative": 125,
    "semi_hard_negative": 16,
    "non_lexical_positive": 59,
}

ANNOTATION_PATH = OUTPUT_DATA_DIR / "poc_annotation_sample.xlsx"
PREVIOUS_KEY_PATH = OUTPUT_DATA_DIR / "review_100_pairs_our_labels.xlsx"

# What the reviewer (Claude) sees: pairs without any label.
INPUT_OUTPUT_PATH = OUTPUT_DATA_DIR / "review_300_pairs_without_labels.xlsx"

# Our labels for the same pairs, used only after the review.
KEY_OUTPUT_PATH = OUTPUT_DATA_DIR / "review_300_pairs_our_labels.xlsx"

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
        description="Build the blind 300-pair review sample."
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

    annotation_df = pd.read_excel(
        ANNOTATION_PATH,
        sheet_name="annotation",
        header=3,
    )

    previous_df = pd.read_excel(
        PREVIOUS_KEY_PATH,
        sheet_name="review_key",
    )

    for path, df in [
        (ANNOTATION_PATH, annotation_df),
        (PREVIOUS_KEY_PATH, previous_df),
    ]:
        if "source_pair_id" not in df.columns:
            raise ValueError(f"{path}: missing source_pair_id column.")

        if df["source_pair_id"].isna().any():
            raise ValueError(f"{path}: empty source_pair_id values.")

    annotation_ids = set(annotation_df["source_pair_id"])

    previous_ids = set(previous_df["source_pair_id"])

    program_df = load_program_expenses()

    non_lexical_df, non_lexical_excluded = load_non_lexical_pairs(
        program_df
    )

    # Shared loaders preserve the original source_pair_id format
    # (source filename, row / analysis_client_id and optional IDs).
    sources = {
        "positive": load_standard_pairs(POSITIVES_PATH, "positive"),
        "easy_negative": load_standard_pairs(EASY_PATH, "easy_negative"),
        "semi_hard_negative": load_standard_pairs(SEMI_HARD_PATH, "semi_hard_negative"),
        "non_lexical_positive": non_lexical_df,
    }

    # --------------------------------------------------------
    # Keep annotation pairs not used in the 100-pair review,
    # remove duplicates, then sample each category
    # (stratified by expense class)
    # --------------------------------------------------------

    sampled = []

    seen = set()

    print("Available pairs (after filters and duplicate removal):")

    for category, df in sources.items():

        size = SAMPLE_SIZES[category]

        if not df.empty:

            df = df[
                df["source_pair_id"].isin(annotation_ids)
                & ~df["source_pair_id"].isin(previous_ids)
            ].copy()

        if not df.empty:

            df = df[~df.apply(pair_key, axis=1).duplicated()]

            df = df[~df.apply(pair_key, axis=1).isin(seen)]

        print(f"  {category}: {len(df)} (requested {size})")

        if len(df) < size:
            print(f"Warning: {category} has only {len(df)} pairs (asked {size}).")

        if df.empty:
            continue

        sample = stratified_sample(df, size, "expense_class")

        sample = sample.rename(columns={"category": "pair_category"})

        sample["our_label"] = OUR_LABELS[category]

        seen.update(sample.apply(pair_key, axis=1))

        sampled.append(sample)

    if not sampled:
        raise ValueError("No eligible pairs remain. No review files were written.")

    review_df = (
        pd.concat(sampled, ignore_index=True)
        .sample(frac=1, random_state=SEED)
        .reset_index(drop=True)
    )

    review_df["review_id"] = [
        f"S{number:03d}"
        for number in range(1, len(review_df) + 1)
    ]

    input_df = review_df[INPUT_COLUMNS].copy()

    key_df = review_df[KEY_COLUMNS].copy()

    # --------------------------------------------------------
    # Safety assertions
    # --------------------------------------------------------

    assert review_df["review_id"].is_unique

    assert review_df["source_pair_id"].is_unique

    assert not review_df.apply(pair_key, axis=1).duplicated().any()

    assert not review_df["source_pair_id"].isin(previous_ids).any()

    assert review_df["source_pair_id"].isin(annotation_ids).all()

    assert input_df["review_id"].equals(key_df["review_id"])

    # The reviewer file must not reveal labels, categories or sources.
    assert not {"our_label", "pair_category", "source_pair_id"} & set(input_df.columns)

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    OUTPUT_DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    input_df.to_excel(
        INPUT_OUTPUT_PATH,
        sheet_name="review_input",
        index=False,
        engine="openpyxl",
    )

    key_df.to_excel(
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
    print("REVIEW SAMPLE (300 pairs requested)")
    print("=" * 70)
    print("Pairs:", len(review_df))
    print()
    print("Per category:")
    print(
        review_df["pair_category"].value_counts()
        .reindex(SAMPLE_SIZES, fill_value=0).to_string()
    )
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
