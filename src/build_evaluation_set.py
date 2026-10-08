import sys
import unicodedata
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
    PROCESSED_DATA_DIR,
    RAW_DATA_DIR,
)


# ============================================================
# Evaluation set
#
# Builds the human-labelled evaluation set used to compare
# embedding models (before / after fine-tuning, baseline).
#
# Source: the human example Excels, one per funding program.
# Each row is a correct pair: a program expense (left columns)
# and a client expense that matches it (right columns).
#
# For every row the correct program expense is located in
# program_expenses_clean.csv, so that its expense_description
# is available. Rows are excluded when:
#   - the program does not exist in our program data
#   - no single program expense matches the row
#   - the client expense was used as a few-shot example
#     (non-lexical examples, new_score == 0)
#
# Extra columns of the Excels (e.g. "Μη Επιλέξιμη Δαπάνη")
# are ignored: every row is a correct pair.
#
# Local only: no API call.
# ============================================================

# Kept in their own folder: program_data_preperation.py reads
# every .xlsx directly under data/raw as a funding program.
HUMAN_PAIRS_DIR = (
    RAW_DATA_DIR
    / "human_pairs"
)

HUMAN_PAIRS_PATTERN = (
    "Παραδείγματα_αντιστοίχισης_δαπανών_*.xlsx"
)

PROGRAM_DATA_PATH = (
    PROCESSED_DATA_DIR
    / "program_expenses_clean.csv"
)

FEW_SHOT_SOURCE_PATH = (
    PROCESSED_DATA_DIR
    / "direct-expense-match-scores-2026-10-05_new_score.xlsx"
)

OUTPUT_PATH = (
    PROCESSED_DATA_DIR
    / "evaluation_set.xlsx"
)

# Excel row of the first data row (row 1 is the header).
FIRST_EXCEL_ROW = 2

HUMAN_COLUMNS = [
    "Program_name",
    "Subprogram_ID",
    "Category",
    "Subcategory",
    "Expense_Class",
    "Business_Expense",
    "Business_Expense_description",
    "Business_Category_Expense_Class",
]

DASHES = "‐‑‒–—−"

REASON_PROGRAM = "program not in data"

REASON_NO_UNIQUE = "no unique program expense"

REASON_FEW_SHOT = "used as few-shot"


# ============================================================
# Helpers
# ============================================================

def clean_value(value) -> str:

    if pd.isna(value):
        return ""

    return str(value).strip()


def text_key(value) -> str:
    """
    Comparison key: ignores case, accents, dash type,
    quotes and extra spaces.
    """

    text = clean_value(value).casefold()

    for dash in DASHES:
        text = text.replace(dash, "-")

    for quote in ['"', "«", "»"]:
        text = text.replace(quote, " ")

    text = "".join(
        character
        for character in unicodedata.normalize("NFD", text)
        if unicodedata.category(character) != "Mn"
    )

    return " ".join(text.split())


def to_int_or_none(value):

    number = pd.to_numeric(
        value,
        errors="coerce",
    )

    if pd.isna(number):
        return None

    return int(number)


# ============================================================
# Load inputs
# ============================================================

def load_human_pairs() -> pd.DataFrame:

    paths = sorted(
        HUMAN_PAIRS_DIR.glob(HUMAN_PAIRS_PATTERN)
    )

    if not paths:
        raise ValueError(
            "No human pair Excels found: "
            f"{HUMAN_PAIRS_DIR / HUMAN_PAIRS_PATTERN}"
        )

    frames = []

    for path in paths:

        df = pd.read_excel(
            path,
            sheet_name="Expenses",
        )

        missing_columns = [
            column
            for column in HUMAN_COLUMNS
            if column not in df.columns
        ]

        if missing_columns:
            raise ValueError(
                f"{path.name}: missing columns {missing_columns}"
            )

        # Extra columns are ignored.
        df = df[HUMAN_COLUMNS].copy()

        df.insert(
            0,
            "excel_row",
            df.index + FIRST_EXCEL_ROW,
        )

        df.insert(
            0,
            "source_excel",
            path.name,
        )

        frames.append(df)

    return pd.concat(
        frames,
        ignore_index=True,
    )


def load_program_expenses() -> pd.DataFrame:

    program_df = pd.read_csv(
        PROGRAM_DATA_PATH,
        encoding="utf-8-sig",
    ).reset_index(drop=True)

    # Same id rule as the rest of the pipeline: row index + 1.
    program_df["program_expense_id"] = (
        program_df.index + 1
    )

    program_df["_title_key"] = (
        program_df["program_title"].map(text_key)
    )

    program_df["_subprogram"] = (
        program_df["subprogram_id"].map(to_int_or_none)
    )

    program_df["_category_key"] = (
        program_df["category"].map(text_key)
    )

    program_df["_subcategory_key"] = (
        program_df["subcategory"].map(text_key)
    )

    return program_df


def load_few_shot_titles() -> set[str]:
    """
    Client titles used as few-shot examples:
    the non-lexical pairs (new_score == 0).
    """

    df = pd.read_excel(
        FEW_SHOT_SOURCE_PATH,
    )

    few_shot = df[
        df["new_score"] == 0
    ]

    return set(
        few_shot["Client Expense"].map(text_key)
    )


# ============================================================
# Main
# ============================================================

def main() -> None:

    human_df = load_human_pairs()

    program_df = load_program_expenses()

    few_shot_titles = load_few_shot_titles()

    kept_rows = []

    excluded_rows = []

    for _, row in human_df.iterrows():

        # ----------------------------------------------------
        # 1. Program must exist in our program data
        # ----------------------------------------------------

        program_rows = program_df[
            program_df["_title_key"]
            == text_key(row["Program_name"])
        ]

        if program_rows.empty:

            excluded_rows.append(
                {**row.to_dict(), "reason": REASON_PROGRAM}
            )

            continue

        # ----------------------------------------------------
        # 2. Exactly one program expense:
        #    subprogram + category + subcategory
        #    (expense class is not part of the key)
        # ----------------------------------------------------

        matches = program_rows[
            (program_rows["_subprogram"] == to_int_or_none(row["Subprogram_ID"]))
            & (program_rows["_category_key"] == text_key(row["Category"]))
            & (program_rows["_subcategory_key"] == text_key(row["Subcategory"]))
        ]

        if len(matches) != 1:

            excluded_rows.append(
                {**row.to_dict(), "reason": REASON_NO_UNIQUE}
            )

            continue

        # ----------------------------------------------------
        # 3. Client must not be a few-shot example
        # ----------------------------------------------------

        if text_key(row["Business_Expense"]) in few_shot_titles:

            excluded_rows.append(
                {**row.to_dict(), "reason": REASON_FEW_SHOT}
            )

            continue

        program = matches.iloc[0]

        kept_rows.append(
            {
                "source_excel":
                    row["source_excel"],

                "excel_row":
                    row["excel_row"],

                "client_expense_title":
                    clean_value(row["Business_Expense"]),

                "client_expense_description":
                    clean_value(row["Business_Expense_description"]),

                "client_expense_class":
                    clean_value(row["Business_Category_Expense_Class"]),

                "program_expense_id":
                    int(program["program_expense_id"]),

                "program_title":
                    clean_value(program["program_title"]),

                "subprogram_id":
                    program["_subprogram"],

                "program_category":
                    clean_value(program["category"]),

                "program_subcategory":
                    clean_value(program["subcategory"]),

                "program_expense_description":
                    clean_value(program["expense_description"]),

                "program_expense_class":
                    clean_value(program["expense_class"]),
            }
        )

    evaluation_df = pd.DataFrame(
        kept_rows
    )

    evaluation_df.insert(
        0,
        "eval_id",
        range(1, len(evaluation_df) + 1),
    )

    excluded_df = pd.DataFrame(
        excluded_rows
    )

    # --------------------------------------------------------
    # Safety assertions
    # --------------------------------------------------------

    assert evaluation_df["program_expense_id"].isin(
        program_df["program_expense_id"]
    ).all()

    assert not evaluation_df["client_expense_title"].map(
        text_key
    ).isin(few_shot_titles).any()

    assert len(evaluation_df) + len(excluded_df) == len(human_df)

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with pd.ExcelWriter(
        OUTPUT_PATH,
        engine="openpyxl",
    ) as writer:

        evaluation_df.to_excel(
            writer,
            sheet_name="evaluation_set",
            index=False,
        )

        excluded_df.to_excel(
            writer,
            sheet_name="excluded",
            index=False,
        )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print("=" * 70)
    print("EVALUATION SET")
    print("=" * 70)
    print("Human pairs read:", len(human_df))
    print()
    print("Rows per Excel:")
    print(human_df["source_excel"].value_counts().sort_index().to_string())
    print()
    print("Excluded by reason:")
    print(
        excluded_df["reason"].value_counts().to_string()
        if not excluded_df.empty
        else "none"
    )
    print()
    print("Evaluation pairs:", len(evaluation_df))
    print()
    print("Evaluation pairs per class:")
    print(evaluation_df["program_expense_class"].value_counts().to_string())
    print()
    print("Saved:", OUTPUT_PATH)
    print("=" * 70)


if __name__ == "__main__":
    main()
