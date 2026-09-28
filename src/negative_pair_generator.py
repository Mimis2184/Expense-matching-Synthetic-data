from pathlib import Path
import sys

import pandas as pd


# ============================================================
# Project root
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

sys.path.insert(
    0,
    str(BASE_DIR),
)


# ============================================================
# Config
# ============================================================

from config import (
    POSITIVE_PAIRS_VALIDATED_CSV_PATH,
    NEGATIVE_PAIRS_CSV_PATH,
    NEGATIVE_PAIRS_EXCEL_PATH,
)


# ============================================================
# Program Expenses input
# ============================================================

PROGRAM_DATA_PATH = (
    BASE_DIR
    / "data"
    / "processed"
    / "program_expenses_clean.csv"
)


# ============================================================
# Helpers
# ============================================================

def clean_value(value) -> str:
    """
    Convert missing values to empty strings
    and remove surrounding whitespace.
    """

    if pd.isna(value):
        return ""

    return str(value).strip()


def normalize_text(value) -> str:
    """
    Normalize text only for comparison.

    Example:
    'Υπηρεσίες Τρίτων'
    and
    'υπηρεσίες τρίτων'

    will be treated as the same value.
    """

    return clean_value(value).casefold()


def prepare_program_expense_ids(
    program_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Ensure that every Program Expense has a program_expense_id.

    Current pipeline:
    program_expense_id was originally created from the row index
    using index + 1.

    If program_expenses_clean.csv already contains a
    program_expense_id in the future, we use it directly.

    Otherwise, recreate the same ID logic currently used by the
    positive-pair pipeline.
    """

    program_df = program_df.copy()

    if "program_expense_id" not in program_df.columns:

        program_df = (
            program_df
            .reset_index(drop=True)
        )

        program_df[
            "program_expense_id"
        ] = (
            program_df.index + 1
        )

    else:

        program_df[
            "program_expense_id"
        ] = (
            pd.to_numeric(
                program_df[
                    "program_expense_id"
                ],
                errors="raise",
            )
            .astype(int)
        )

    return program_df


# ============================================================
# Find all candidate negatives for one positive client
# ============================================================

def select_negative_candidates(
    positive_row: pd.Series,
    program_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    For one validated positive client:

    1. Keep only Program Expenses of the SAME expense_class.
    2. Remove the Program Expense already known to be positive.
    3. Return ALL remaining same-class Program Expenses.

    IMPORTANT:
    These are candidate negatives only.

    They are NOT considered true negatives until they pass
    the negative-pair validator.
    """

    client_class = normalize_text(
        positive_row[
            "client_expense_class"
        ]
    )

    positive_program_id = int(
        positive_row[
            "program_expense_id"
        ]
    )

    # --------------------------------------------------------
    # Same expense_class only
    # --------------------------------------------------------

    same_class_mask = (
        program_df[
            "expense_class"
        ]
        .apply(normalize_text)
        .eq(client_class)
    )

    candidates = program_df[
        same_class_mask
    ].copy()

    # --------------------------------------------------------
    # Remove original positive Program Expense
    # --------------------------------------------------------

    candidates = candidates[
        candidates[
            "program_expense_id"
        ]
        !=
        positive_program_id
    ].copy()

    # --------------------------------------------------------
    # Stable order for reproducibility
    # --------------------------------------------------------

    candidates = (
        candidates
        .sort_values(
            by="program_expense_id"
        )
        .reset_index(drop=True)
    )

    return candidates


# ============================================================
# Main
# ============================================================

def main() -> None:

    # ========================================================
    # Load validated positive pairs
    # ========================================================

    positive_df = pd.read_csv(
        POSITIVE_PAIRS_VALIDATED_CSV_PATH,
        encoding="utf-8-sig",
    )

    print(
        f"Validated positive pairs loaded: "
        f"{len(positive_df)}"
    )

    # ========================================================
    # Load all real Program Expenses
    # ========================================================

    program_df = pd.read_csv(
        PROGRAM_DATA_PATH,
        encoding="utf-8-sig",
    )

    program_df = prepare_program_expense_ids(
        program_df
    )

    print(
        f"Program expenses loaded: "
        f"{len(program_df)}"
    )

    # ========================================================
    # Basic required-column checks
    # ========================================================

    required_positive_columns = [
        "program_expense_id",
        "client_expense_title",
        "client_expense_description",
        "client_expense_class",
    ]

    required_program_columns = [
        "program_expense_id",
        "category",
        "subcategory",
        "expense_description",
        "expense_class",
    ]

    missing_positive_columns = [
        column
        for column in required_positive_columns
        if column not in positive_df.columns
    ]

    if missing_positive_columns:
        raise ValueError(
            "Missing columns in positive pairs: "
            f"{missing_positive_columns}"
        )

    missing_program_columns = [
        column
        for column in required_program_columns
        if column not in program_df.columns
    ]

    if missing_program_columns:
        raise ValueError(
            "Missing columns in program expenses: "
            f"{missing_program_columns}"
        )

    # ========================================================
    # Generate candidate negative pairs
    # ========================================================

    rows = []

    clients_without_candidates = 0

    total_positive_clients = len(
        positive_df
    )

    for counter, (_, positive_row) in enumerate(
        positive_df.iterrows(),
        start=1,
    ):

        print(
            f"Building negative candidates "
            f"{counter}/{total_positive_clients}",
            end="\r",
            flush=True,
        )

        # ----------------------------------------------------
        # Find every other Program Expense of the same class
        # ----------------------------------------------------

        candidates = (
            select_negative_candidates(
                positive_row=positive_row,
                program_df=program_df,
            )
        )

        # ----------------------------------------------------
        # It is theoretically possible that a class contains
        # only the original positive Program Expense.
        # ----------------------------------------------------

        if candidates.empty:

            clients_without_candidates += 1
            continue

        # ----------------------------------------------------
        # Same client paired with every alternative
        # same-class Program Expense
        # ----------------------------------------------------

        for _, candidate in candidates.iterrows():

            rows.append(
                {
                    # ========================================
                    # Original known positive Program Expense
                    #
                    # Used only to preserve the relationship
                    # between this client and its positive pair.
                    # ========================================

                    "positive_program_expense_id":
                        int(
                            positive_row[
                                "program_expense_id"
                            ]
                        ),

                    # ========================================
                    # Alternative Program Expense being tested
                    # as a possible negative.
                    # ========================================

                    "program_expense_id":
                        int(
                            candidate[
                                "program_expense_id"
                            ]
                        ),

                    # ========================================
                    # SAME client from the validated positive
                    # ========================================

                    "client_expense_title":
                        clean_value(
                            positive_row[
                                "client_expense_title"
                            ]
                        ),

                    "client_expense_description":
                        clean_value(
                            positive_row[
                                "client_expense_description"
                            ]
                        ),

                    "client_expense_class":
                        clean_value(
                            positive_row[
                                "client_expense_class"
                            ]
                        ),

                    # ========================================
                    # Alternative Program Expense
                    # ========================================

                    "program_category":
                        clean_value(
                            candidate[
                                "category"
                            ]
                        ),

                    "program_subcategory":
                        clean_value(
                            candidate[
                                "subcategory"
                            ]
                        ),

                    "program_expense_description":
                        clean_value(
                            candidate[
                                "expense_description"
                            ]
                        ),

                    "program_expense_class":
                        clean_value(
                            candidate[
                                "expense_class"
                            ]
                        ),

                    # ========================================
                    # Candidate label.
                    #
                    # IMPORTANT:
                    # This is not validated yet.
                    # Only INVALID pairs from the validator
                    # will finally remain with label = 0.
                    # ========================================

                    "label": 0,
                }
            )

    print()

    # ========================================================
    # Build DataFrame
    # ========================================================

    candidate_df = pd.DataFrame(
        rows
    )

    if candidate_df.empty:

        raise ValueError(
            "No negative pair candidates were generated."
        )

    # ========================================================
    # Remove accidental duplicate pairs
    # ========================================================

    before_dedup = len(
        candidate_df
    )

    candidate_df = (
        candidate_df
        .drop_duplicates(
            subset=[
                "positive_program_expense_id",
                "program_expense_id",
                "client_expense_title",
                "client_expense_description",
            ]
        )
        .reset_index(drop=True)
    )

    duplicates_removed = (
        before_dedup
        -
        len(candidate_df)
    )

    # ========================================================
    # Final column order
    # ========================================================

    candidate_columns = [
        "positive_program_expense_id",
        "program_expense_id",
        "client_expense_title",
        "client_expense_description",
        "client_expense_class",
        "program_category",
        "program_subcategory",
        "program_expense_description",
        "program_expense_class",
        "label",
    ]

    candidate_df = candidate_df[
        candidate_columns
    ]

    # ========================================================
    # Save
    # ========================================================

    NEGATIVE_PAIRS_CSV_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # CSV
    # --------------------------------------------------------

    candidate_df.to_csv(
        NEGATIVE_PAIRS_CSV_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    # --------------------------------------------------------
    # Excel
    # --------------------------------------------------------

    candidate_df.to_excel(
        NEGATIVE_PAIRS_EXCEL_PATH,
        index=False,
        engine="openpyxl",
    )

    # ========================================================
    # Summary
    # ========================================================

    clients_with_candidates = (
        total_positive_clients
        -
        clients_without_candidates
    )

    print()
    print("=" * 60)
    print(
        "NEGATIVE PAIR CANDIDATE GENERATION SUMMARY"
    )
    print("=" * 60)

    print(
        f"Validated positive clients: "
        f"{total_positive_clients}"
    )

    print(
        f"Clients with same-class alternatives: "
        f"{clients_with_candidates}"
    )

    print(
        f"Clients without same-class alternatives: "
        f"{clients_without_candidates}"
    )

    print(
        f"Total candidate pairs generated: "
        f"{len(candidate_df)}"
    )

    print(
        f"Exact duplicates removed: "
        f"{duplicates_removed}"
    )

    print()

    print(
        f"Saved CSV:\n"
        f"{NEGATIVE_PAIRS_CSV_PATH}"
    )

    print()

    print(
        f"Saved Excel:\n"
        f"{NEGATIVE_PAIRS_EXCEL_PATH}"
    )

    print("=" * 60)


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    main()