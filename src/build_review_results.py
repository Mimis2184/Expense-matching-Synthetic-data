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


# ============================================================
# Review results (100 or 300 pairs)
#
# Joins the blind review input, our labels and Claude's
# decisions into one Excel for the manual review of the
# disagreements.
#
# Decisions GENERIC / DIRECT / PLAUSIBLE / RESIDUAL -> Match,
# NO -> No match (matching guidelines v0.3).
#
# Local only: no API call, input files are not modified.
# ============================================================

def sample_paths(sample: int) -> tuple[Path, Path, Path, Path]:
    """
    Input, key, decisions and output paths of one review sample.
    """

    prefix = OUTPUT_DATA_DIR / f"review_{sample}_pairs"

    return (
        Path(f"{prefix}_without_labels.xlsx"),
        Path(f"{prefix}_our_labels.xlsx"),
        Path(f"{prefix}_claude_decisions.csv"),
        Path(f"{prefix}_comparison.xlsx"),
    )


DECISION_TO_LABEL = {
    "GENERIC": "Match",
    "DIRECT": "Match",
    "PLAUSIBLE": "Match",
    "RESIDUAL": "Match",
    "NO": "No match",
}

OUTPUT_COLUMNS = [
    "review_id",
    "source_pair_id",
    "pair_category",
    "seen_before",
    "client_expense_title",
    "client_expense_description",
    "expense_class",
    "program_category",
    "program_subcategory",
    "program_expense_description",
    "our_label",
    "claude_label",
    "agreement",
    "claude_decision",
    "claude_confidence",
    "client_evidence",
    "program_evidence",
    "claude_reason",
    "manual_review_label",
    "manual_review_comment",
]


def clean_text(value) -> str:

    if pd.isna(value):
        return ""

    return str(value)


# ============================================================
# Main
# ============================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description="Build the 100- or 300-pair review comparison."
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing comparison file.",
    )

    parser.add_argument(
        "--sample",
        type=int,
        choices=[100, 300],
        default=100,
        help="Which review sample to build.",
    )

    args = parser.parse_args()

    INPUT_PATH, KEY_PATH, DECISIONS_PATH, OUTPUT_PATH = sample_paths(args.sample)

    if OUTPUT_PATH.exists() and not args.overwrite:
        raise FileExistsError(
            f"{OUTPUT_PATH} already exists. Use --overwrite to replace it."
        )

    input_df = pd.read_excel(INPUT_PATH)

    key_df = pd.read_excel(KEY_PATH)

    decisions_df = pd.read_csv(
        DECISIONS_PATH,
        encoding="utf-8-sig",
    )

    if "seen_before" not in decisions_df.columns:
        decisions_df["seen_before"] = False

    # --------------------------------------------------------
    # Safety assertions
    # --------------------------------------------------------

    for name, df in [
        ("input", input_df),
        ("key", key_df),
        ("decisions", decisions_df),
    ]:
        assert df["review_id"].is_unique, f"Duplicate review_id in {name}."

    assert (
        set(input_df["review_id"])
        == set(key_df["review_id"])
        == set(decisions_df["review_id"])
    ), "The three files do not have the same review_ids."

    unknown = set(decisions_df["claude_decision"]) - set(DECISION_TO_LABEL)

    assert not unknown, f"Unknown decisions: {unknown}"

    # --------------------------------------------------------
    # Join and compare
    # --------------------------------------------------------

    df = (
        input_df
        .merge(key_df, on="review_id", validate="one_to_one")
        .merge(decisions_df, on="review_id", validate="one_to_one")
    )

    df["claude_label"] = df["claude_decision"].map(DECISION_TO_LABEL)

    df["agreement"] = df["our_label"] == df["claude_label"]

    df["manual_review_label"] = ""

    df["manual_review_comment"] = ""

    # --------------------------------------------------------
    # Evidence must be verbatim
    # --------------------------------------------------------

    client_text = (
        df["client_expense_title"].map(clean_text)
        + " "
        + df["client_expense_description"].map(clean_text)
    )

    program_text = (
        df["program_category"].map(clean_text)
        + " "
        + df["program_subcategory"].map(clean_text)
        + " "
        + df["program_expense_description"].map(clean_text)
    )

    bad_client = [
        review_id
        for review_id, evidence, text in zip(df["review_id"], df["client_evidence"], client_text)
        if clean_text(evidence) not in text
    ]

    bad_program = [
        review_id
        for review_id, evidence, text in zip(df["review_id"], df["program_evidence"], program_text)
        if clean_text(evidence) not in text
    ]

    # --------------------------------------------------------
    # Disagreements first
    # --------------------------------------------------------

    df = (
        df.sort_values(["agreement", "review_id"])
        .reset_index(drop=True)
    )[OUTPUT_COLUMNS]

    OUTPUT_DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    df.to_excel(
        OUTPUT_PATH,
        sheet_name="review_comparison",
        index=False,
        engine="openpyxl",
    )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    per_category = df.groupby("pair_category").agg(
        pairs=("agreement", "size"),
        agree=("agreement", "sum"),
    )

    per_category["agreement_rate"] = (
        per_category["agree"] / per_category["pairs"]
    ).round(2)

    print("=" * 70)
    print("REVIEW RESULTS")
    print("=" * 70)
    print("Pairs:", len(df))
    print("Agreement:", int(df["agreement"].sum()), f"({df['agreement'].mean():.0%})")
    print()
    print("Per category:")
    print(per_category.to_string())
    print()
    print("Our label vs Claude label:")
    print(pd.crosstab(df["our_label"], df["claude_label"]).to_string())
    print()
    print("Claude decision on disagreements:")
    print(df.loc[~df["agreement"], "claude_decision"].value_counts().to_string())
    print()
    print("Non-verbatim client evidence:", bad_client or "none")
    print("Non-verbatim program evidence:", bad_program or "none")
    print()
    print("Saved:", OUTPUT_PATH)
    print("=" * 70)


if __name__ == "__main__":
    main()
