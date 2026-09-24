from pathlib import Path
import sys

import pandas as pd


# ============================================================
# Paths
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

# Allow imports from project root
sys.path.insert(0, str(BASE_DIR))

from config import (
    TEST_EXPENSE_CLASS,
    PAIR_SAMPLE_N_CLIENTS,
)


CLIENT_PATH = (
    BASE_DIR
    / "data"
    / "processed"
    / "client_expenses_clean.csv"
)

PROGRAM_PATH = (
    BASE_DIR
    / "data"
    / "processed"
    / "program_expenses_clean.csv"
)

CSV_OUTPUT_PATH = (
    BASE_DIR
    / "data"
    / "processed"
    / "candidate_pairs.csv"
)

EXCEL_OUTPUT_PATH = (
    BASE_DIR
    / "data"
    / "processed"
    / "candidate_pairs.xlsx"
)


# ============================================================
# Helper
# ============================================================

def normalize_class(value):
    """
    Normalizes expense_class only for comparison.

    Example:
    'Υπηρεσίες Τρίτων'
    and
    'Υπηρεσίες τρίτων'

    are treated as the same class.
    """

    if pd.isna(value):
        return ""

    return str(value).strip().casefold()


# ============================================================
# Main
# ============================================================

def main():

    # --------------------------------------------------------
    # Load data
    # --------------------------------------------------------

    client_df = pd.read_csv(
        CLIENT_PATH
    )

    program_df = pd.read_csv(
        PROGRAM_PATH
    )

    target_class = normalize_class(
        TEST_EXPENSE_CLASS
    )


    # --------------------------------------------------------
    # Find client expenses of selected class
    # --------------------------------------------------------

    client_df["class_key"] = (
        client_df["expense_class"]
        .apply(normalize_class)
    )

    client_class_df = client_df[
        client_df["class_key"]
        == target_class
    ].copy()

    print(
        f"Client expenses in selected class: "
        f"{len(client_class_df)}"
    )


    # --------------------------------------------------------
    # Find program expenses of selected class
    # --------------------------------------------------------

    program_df["class_key"] = (
        program_df["expense_class"]
        .apply(normalize_class)
    )

    program_class_df = program_df[
        program_df["class_key"]
        == target_class
    ].copy()

    print(
        f"Program expenses in selected class: "
        f"{len(program_class_df)}"
    )


    # --------------------------------------------------------
    # Safety checks
    # --------------------------------------------------------

    if client_class_df.empty:
        raise ValueError(
            "No client expenses found "
            "for TEST_EXPENSE_CLASS."
        )

    if program_class_df.empty:
        raise ValueError(
            "No program expenses found "
            "for TEST_EXPENSE_CLASS."
        )


    # --------------------------------------------------------
    # Small client sample
    # --------------------------------------------------------

    n_clients = min(
        PAIR_SAMPLE_N_CLIENTS,
        len(client_class_df),
    )

    sampled_clients = (
        client_class_df
        .sample(
            n=n_clients,
            random_state=42,
        )
        .reset_index(drop=True)
    )


    # --------------------------------------------------------
    # Create candidate pairs
    # --------------------------------------------------------

    pairs = []

    for client_id, client in sampled_clients.iterrows():

        for program_id, program in program_class_df.iterrows():

            pairs.append(
                {
                    # ========================================
                    # Client side
                    # ========================================

                    "client_expense_title":
                        client["expense_title"],

                    "client_expense_description":
                        client["expense_description"],

                    "client_expense_class":
                        client["expense_class"],


                    # ========================================
                    # Program side
                    # ========================================

                    "program_title":
                        program["program_title"],

                    "subprogram_name":
                        program["subprogram_name"],

                    "program_category":
                        program["category"],

                    "program_subcategory":
                        program["subcategory"],

                    "program_expense_description":
                        program["expense_description"],

                    "program_expense_class":
                        program["expense_class"],


                    # ========================================
                    # Ground Truth labeling
                    # ========================================

                    "label": "",

                    "label_reason": "",
                }
            )


    # --------------------------------------------------------
    # Create DataFrame
    # --------------------------------------------------------

    pairs_df = pd.DataFrame(
        pairs
    )


    # --------------------------------------------------------
    # Create output directory
    # --------------------------------------------------------

    CSV_OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )


    # --------------------------------------------------------
    # Save CSV
    # --------------------------------------------------------

    pairs_df.to_csv(
        CSV_OUTPUT_PATH,
        index=False,
        encoding="utf-8-sig",
    )


    # --------------------------------------------------------
    # Save Excel
    # --------------------------------------------------------

    pairs_df.to_excel(
        EXCEL_OUTPUT_PATH,
        index=False,
        engine="openpyxl",
    )


    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print()
    print("=" * 60)

    print(
        f"Selected class:\n"
        f"{TEST_EXPENSE_CLASS}"
    )

    print()

    print(
        f"Sampled clients: "
        f"{len(sampled_clients)}"
    )

    print(
        f"Program candidates: "
        f"{len(program_class_df)}"
    )

    print(
        f"Candidate pairs created: "
        f"{len(pairs_df)}"
    )

    print()

    print(
        "Saved CSV to:"
    )

    print(
        CSV_OUTPUT_PATH
    )

    print()

    print(
        "Saved Excel to:"
    )

    print(
        EXCEL_OUTPUT_PATH
    )

    print("=" * 60)


if __name__ == "__main__":
    main()