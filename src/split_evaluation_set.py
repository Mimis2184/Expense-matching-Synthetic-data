import sys
from pathlib import Path

import numpy as np
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
    PROCESSED_DATA_DIR,
)

from src.build_evaluation_set import (
    text_key,
)


# ============================================================
# Split the human evaluation set
#
# The 535 human pairs are split into:
#   - validation: used to choose loss, learning rate, margin,
#                 epochs and checkpoint
#   - test:       used once, at the end, for the final comparison
#
# Stratified by expense class. Rows with the same client text
# (title + description) always go to the same split, so that the
# same client never appears in both.
#
# The original evaluation_set.xlsx is not modified.
# Local only: no API call.
# ============================================================

SEED = 42

VALIDATION_SHARE = 0.35

INPUT_PATH = PROCESSED_DATA_DIR / "evaluation_set.xlsx"

VALIDATION_PATH = PROCESSED_DATA_DIR / "human_pairs_for_validation.xlsx"

TEST_PATH = PROCESSED_DATA_DIR / "human_pairs_for_test.xlsx"

# Same sheet name as the original, so the comparison script can
# read any of the three files.
SHEET_NAME = "evaluation_set"


# ============================================================
# Main
# ============================================================

def main() -> None:

    df = pd.read_excel(
        INPUT_PATH,
        sheet_name=SHEET_NAME,
    )

    # --------------------------------------------------------
    # Client group: same client text = same group
    # --------------------------------------------------------

    df["_client_group"] = (
        df["client_expense_title"].map(text_key)
        + " || "
        + df["client_expense_description"].map(text_key)
    )

    rng = np.random.default_rng(SEED)

    validation_groups = set()

    # --------------------------------------------------------
    # Per class: shuffle the client groups and assign groups to
    # validation until it reaches VALIDATION_SHARE of the rows
    # --------------------------------------------------------

    for _, class_df in df.groupby(
        "program_expense_class",
        sort=True,
    ):

        group_sizes = (
            class_df["_client_group"]
            .value_counts()
            .sort_index()
        )

        groups = group_sizes.index.to_numpy()

        rng.shuffle(groups)

        target = VALIDATION_SHARE * len(class_df)

        assigned = 0

        for group in groups:

            if assigned >= target:
                break

            # A group may already be in validation from another class.
            if group in validation_groups:
                assigned += group_sizes[group]
                continue

            validation_groups.add(group)

            assigned += group_sizes[group]

    is_validation = df["_client_group"].isin(validation_groups)

    validation_df = df[is_validation].drop(columns="_client_group")

    test_df = df[~is_validation].drop(columns="_client_group")

    # --------------------------------------------------------
    # Safety assertions
    # --------------------------------------------------------

    assert len(validation_df) + len(test_df) == len(df)

    assert not set(validation_df["eval_id"]) & set(test_df["eval_id"])

    validation_clients = set(
        validation_df["client_expense_title"].map(text_key)
        + " || "
        + validation_df["client_expense_description"].map(text_key)
    )

    test_clients = set(
        test_df["client_expense_title"].map(text_key)
        + " || "
        + test_df["client_expense_description"].map(text_key)
    )

    assert not validation_clients & test_clients, (
        "The same client appears in both validation and test."
    )

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    for path, split_df in [
        (VALIDATION_PATH, validation_df),
        (TEST_PATH, test_df),
    ]:

        with pd.ExcelWriter(path, engine="openpyxl") as writer:

            split_df.to_excel(
                writer,
                sheet_name=SHEET_NAME,
                index=False,
            )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    summary = pd.DataFrame(
        {
            "validation": validation_df["program_expense_class"].value_counts(),
            "test": test_df["program_expense_class"].value_counts(),
        }
    ).fillna(0).astype(int)

    summary["validation_share"] = (
        summary["validation"]
        / (summary["validation"] + summary["test"])
    ).round(2)

    print("=" * 70)
    print("EVALUATION SET SPLIT")
    print("=" * 70)
    print("Human pairs:", len(df))
    print("Validation:", len(validation_df))
    print("Test:", len(test_df))
    print()
    print("Per expense class:")
    print(summary.to_string())
    print()
    print("Per source Excel (validation / test):")
    print(
        pd.DataFrame(
            {
                "validation": validation_df["source_excel"].value_counts(),
                "test": test_df["source_excel"].value_counts(),
            }
        ).fillna(0).astype(int).to_string()
    )
    print()
    print("Saved:", VALIDATION_PATH)
    print("Saved:", TEST_PATH)
    print("=" * 70)


if __name__ == "__main__":
    main()
