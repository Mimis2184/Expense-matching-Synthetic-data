import argparse
import sys
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill


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
    PROCESSED_DATA_DIR,
    RAW_DATA_DIR,
)

from src.build_evaluation_set import (
    load_program_expenses,
    text_key,
    to_int_or_none,
)


# ============================================================
# POC annotation sample
#
# Builds one Excel sheet with existing pairs (no new pairs,
# no API call) for human annotation. All pairs are shuffled
# together; the pair category is not shown, only our label.
# The annotator leaves "annotation" empty when they agree with
# our_label, otherwise writes what they think and why.
# ============================================================

SEED = 42

POSITIVES_PATH = OUTPUT_DATA_DIR / "positive_pairs_validated_clean.csv"
EASY_PATH = OUTPUT_DATA_DIR / "easy_negative_pairs_validated_clean.csv"
SEMI_HARD_PATH = OUTPUT_DATA_DIR / "hard_negative_pairs_validated.csv"
NON_LEXICAL_PATH = OUTPUT_DATA_DIR / "non_lexical_pairs_pilot.xlsx"

NEW_SCORE_PATH = (
    PROCESSED_DATA_DIR
    / "direct-expense-match-scores-2026-10-05_new_score.xlsx"
)

HUMAN_PAIRS_DIR = RAW_DATA_DIR / "human_pairs"

OUTPUT_PATH = OUTPUT_DATA_DIR / "poc_annotation_sample.xlsx"

# How many pairs per category. None = take all.
SAMPLE_SIZES = {
    "positive": 380,
    "easy_negative": 210,
    "semi_hard_negative": None,
    "non_lexical_positive": None,
}

OUR_LABELS = {
    "positive": "Match",
    "easy_negative": "No match",
    "semi_hard_negative": "No match",
    "non_lexical_positive": "Match",
}

# Excel row of the first data row in the source files (row 1 = header).
FIRST_EXCEL_ROW = 2

OUTPUT_COLUMNS = [
    "source_pair_id",
    "annotation_id",
    "client_expense_title",
    "client_expense_description",
    "program_category",
    "program_subcategory",
    "program_expense_description",
    "expense_class",
    "our_label",
    "annotation",
]

COLUMN_WIDTHS = {
    "source_pair_id": 30,
    "annotation_id": 12,
    "client_expense_title": 32,
    "client_expense_description": 45,
    "program_category": 28,
    "program_subcategory": 32,
    "program_expense_description": 70,
    "expense_class": 28,
    "our_label": 12,
    "annotation": 45,
}

TITLE = "POC annotation"

INSTRUCTION = (
    "Ελέγξτε αν η δαπάνη του client καλύπτεται από τη δαπάνη του "
    "προγράμματος (category, subcategory, περιγραφή). Αν η περιγραφή βάζει "
    "περιορισμούς (π.χ. \"μόνο ηλεκτρικά\", \"εξαιρούνται τα "
    "μεταχειρισμένα\"), ελέγξτε ότι ο client δεν τους παραβιάζει. Κρίνετε "
    "μόνο τη δαπάνη, όχι αν η επιχείρηση δικαιούται το πρόγραμμα (περιοχή, "
    "ΚΑΔ, μέγεθος). Η απουσία κοινών λέξεων δεν σημαίνει απαραίτητα ότι δεν "
    "υπάρχει αντιστοίχιση. Αν συμφωνείτε με το our_label, αφήστε τη στήλη "
    "annotation κενή· αλλιώς γράψτε τι πιστεύετε και γιατί."
)

TITLE_ROW = 1
INSTRUCTION_ROW = 2
HEADER_ROW = 4
FIRST_DATA_ROW = 5

DATA_ROW_HEIGHT = 90

ANNOTATION_FILL = PatternFill(
    start_color="FFF2CC",
    end_color="FFF2CC",
    fill_type="solid",
)

HEADER_FILL = PatternFill(
    start_color="D9E1F2",
    end_color="D9E1F2",
    fill_type="solid",
)


# ============================================================
# Helpers
# ============================================================

def clean_value(value) -> str:

    if pd.isna(value):
        return ""

    return str(value).strip()


def pair_key(row) -> tuple:
    """
    Duplicate key: same client + same program expense.
    """

    return (
        text_key(row["client_expense_title"]),
        text_key(row["client_expense_description"]),
        int(row["program_expense_id"]),
    )


def stratified_sample(
    df: pd.DataFrame,
    size,
    class_column: str,
) -> pd.DataFrame:
    """
    Sample `size` rows proportionally to each class, at least one
    row from every class. size=None or size >= len(df) keeps all.
    """

    if size is None or size >= len(df):
        return df.copy()

    class_sizes = df[class_column].value_counts()

    # Start with one row per class, then share the rest
    # proportionally (largest remainder).
    allocation = pd.Series(1, index=class_sizes.index)

    remaining = size - allocation.sum()

    if remaining < 0:
        raise ValueError(
            f"Sample size {size} is smaller than the number of classes."
        )

    capacity = class_sizes - allocation

    shares = capacity / capacity.sum() * remaining

    extra = shares.astype(int)

    left_over = remaining - extra.sum()

    for class_name in (shares - extra).sort_values(ascending=False).index:

        if left_over == 0:
            break

        if extra[class_name] < capacity[class_name]:
            extra[class_name] += 1
            left_over -= 1

    allocation = allocation + extra.clip(upper=capacity)

    samples = [
        df[df[class_column] == class_name].sample(
            n=int(count),
            random_state=SEED,
        )
        for class_name, count in allocation.items()
        if count > 0
    ]

    return pd.concat(samples)


# ============================================================
# Load categories
# ============================================================

def load_standard_pairs(
    path: Path,
    category: str,
) -> pd.DataFrame:
    """
    Positives, easy and semi-hard negatives: the program columns
    are already in the source file.
    """

    df = pd.read_csv(
        path,
        encoding="utf-8-sig",
    ).reset_index(drop=True)

    df["_source_row"] = df.index + FIRST_EXCEL_ROW

    def source_id(row) -> str:

        parts = [path.name]

        if "analysis_client_id" in df.columns:
            parts.append(
                f"analysis_client_id={int(row['analysis_client_id'])}"
            )
        else:
            parts.append(f"row={int(row['_source_row'])}")

        if category == "semi_hard_negative":
            parts.append(
                f"program_expense_id={int(row['program_expense_id'])}"
            )

        return " · ".join(parts)

    return pd.DataFrame(
        {
            "category": category,
            "source_pair_id": df.apply(source_id, axis=1),
            "client_expense_title": df["client_expense_title"].map(clean_value),
            "client_expense_description": df["client_expense_description"].map(clean_value),
            "program_expense_id": df["program_expense_id"].astype(int),
            "program_category": df["program_category"].map(clean_value),
            "program_subcategory": df["program_subcategory"].map(clean_value),
            "program_expense_description": df["program_expense_description"].map(clean_value),
            "expense_class": df["program_expense_class"].map(clean_value),
        }
    )


def load_human_pairs() -> pd.DataFrame:

    frames = [
        pd.read_excel(path, sheet_name="Expenses")
        for path in sorted(HUMAN_PAIRS_DIR.glob("*.xlsx"))
    ]

    human = pd.concat(
        frames,
        ignore_index=True,
    )

    for column in [
        "Business_Expense",
        "Business_Expense_description",
        "Category",
        "Subcategory",
    ]:
        human[f"_{column}_key"] = human[column].map(text_key)

    return human


def load_non_lexical_pairs(
    program_df: pd.DataFrame,
) -> tuple[pd.DataFrame, int]:
    """
    Non-lexical pilot pairs with new_score == 0. The program
    description is found through source_row -> human pair ->
    Program_name + Subprogram_ID + Category + Subcategory.
    Pairs without a unique program expense are excluded.
    """

    pilot = pd.read_excel(
        NON_LEXICAL_PATH,
        sheet_name="pairs",
    ).reset_index(drop=True)

    pilot["_source_file_row"] = pilot.index + FIRST_EXCEL_ROW

    pilot = pilot[pilot["new_score"] == 0]

    scores = pd.read_excel(NEW_SCORE_PATH)

    human = load_human_pairs()

    rows = []

    excluded = 0

    for _, row in pilot.iterrows():

        example = scores.iloc[int(row["source_row"]) - FIRST_EXCEL_ROW]

        human_match = human[
            (human["_Business_Expense_key"] == text_key(example["Client Expense"]))
            & (human["_Business_Expense_description_key"] == text_key(example["Client Expense Description"]))
            & (human["_Category_key"] == text_key(example["Ideal Expense Category"]))
            & (human["_Subcategory_key"] == text_key(example["Ideal Expense Subcategory"]))
        ]

        if human_match.empty:
            excluded += 1
            continue

        human_row = human_match.iloc[0]

        program_match = program_df[
            (program_df["_title_key"] == text_key(human_row["Program_name"]))
            & (program_df["_subprogram"] == to_int_or_none(human_row["Subprogram_ID"]))
            & (program_df["_category_key"] == text_key(human_row["Category"]))
            & (program_df["_subcategory_key"] == text_key(human_row["Subcategory"]))
        ]

        if len(program_match) != 1:
            excluded += 1
            continue

        program = program_match.iloc[0]

        rows.append(
            {
                "category": "non_lexical_positive",
                "source_pair_id": (
                    f"{NON_LEXICAL_PATH.name} · "
                    f"row={int(row['_source_file_row'])} · "
                    f"source_row={int(row['source_row'])}"
                ),
                "client_expense_title": clean_value(row["client_expense_title"]),
                "client_expense_description": clean_value(row["client_expense_description"]),
                "program_expense_id": int(program["program_expense_id"]),
                "program_category": clean_value(program["category"]),
                "program_subcategory": clean_value(program["subcategory"]),
                "program_expense_description": clean_value(program["expense_description"]),
                "expense_class": clean_value(program["expense_class"]),
            }
        )

    return pd.DataFrame(rows), excluded


# ============================================================
# Excel formatting
# ============================================================

def format_workbook(path: Path, data_rows: int) -> None:

    workbook = load_workbook(path)

    sheet = workbook["annotation"]

    last_column = sheet.cell(row=HEADER_ROW, column=len(OUTPUT_COLUMNS)).column_letter

    last_row = HEADER_ROW + data_rows

    # Title and instruction
    sheet.cell(row=TITLE_ROW, column=1, value=TITLE).font = Font(bold=True, size=14)

    sheet.merge_cells(f"A{INSTRUCTION_ROW}:{last_column}{INSTRUCTION_ROW}")

    instruction_cell = sheet.cell(row=INSTRUCTION_ROW, column=1, value=INSTRUCTION)

    instruction_cell.alignment = Alignment(wrap_text=True, vertical="top")

    sheet.row_dimensions[INSTRUCTION_ROW].height = 60

    # Header
    for column_index, column_name in enumerate(OUTPUT_COLUMNS, start=1):

        cell = sheet.cell(row=HEADER_ROW, column=column_index)

        cell.font = Font(bold=True)

        cell.fill = HEADER_FILL

        cell.alignment = Alignment(wrap_text=True, vertical="top")

        sheet.column_dimensions[cell.column_letter].width = COLUMN_WIDTHS[column_name]

    # Data rows
    annotation_index = OUTPUT_COLUMNS.index("annotation") + 1

    for row_index in range(FIRST_DATA_ROW, last_row + 1):

        sheet.row_dimensions[row_index].height = DATA_ROW_HEIGHT

        for column_index in range(1, len(OUTPUT_COLUMNS) + 1):

            cell = sheet.cell(row=row_index, column=column_index)

            cell.alignment = Alignment(wrap_text=True, vertical="top")

            if column_index == annotation_index:
                cell.fill = ANNOTATION_FILL

    sheet.cell(row=HEADER_ROW, column=annotation_index).fill = ANNOTATION_FILL

    sheet.freeze_panes = f"A{FIRST_DATA_ROW}"

    sheet.auto_filter.ref = f"A{HEADER_ROW}:{last_column}{last_row}"

    workbook.save(path)


# ============================================================
# Main
# ============================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description="Build the POC annotation sample Excel."
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing annotation Excel.",
    )

    args = parser.parse_args()

    # Never overwrite an Excel that may already contain annotations.
    if OUTPUT_PATH.exists() and not args.overwrite:
        raise FileExistsError(
            f"{OUTPUT_PATH} already exists. Use --overwrite to replace it."
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
    # --------------------------------------------------------

    sampled = []

    seen = set()

    for category, df in sources.items():

        df = df[~df.apply(pair_key, axis=1).duplicated()]

        df = df[~df.apply(pair_key, axis=1).isin(seen)]

        size = SAMPLE_SIZES[category]

        if size is not None and len(df) < size:
            print(f"Warning: {category} has only {len(df)} pairs (asked {size}).")

        sample = stratified_sample(df, size, "expense_class")

        sample["our_label"] = OUR_LABELS[category]

        seen.update(sample.apply(pair_key, axis=1))

        sampled.append(sample)

    annotation_df = (
        pd.concat(sampled, ignore_index=True)
        .sample(frac=1, random_state=SEED)
        .reset_index(drop=True)
    )

    annotation_df["annotation_id"] = [
        f"A{number:03d}"
        for number in range(1, len(annotation_df) + 1)
    ]

    annotation_df["annotation"] = ""

    # --------------------------------------------------------
    # Safety assertions
    # --------------------------------------------------------

    assert annotation_df["annotation_id"].is_unique

    assert not annotation_df.apply(pair_key, axis=1).duplicated().any()

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:

        annotation_df[OUTPUT_COLUMNS].to_excel(
            writer,
            sheet_name="annotation",
            index=False,
            startrow=HEADER_ROW - 1,
        )

    format_workbook(
        OUTPUT_PATH,
        len(annotation_df),
    )

    # --------------------------------------------------------
    # Summary (terminal only)
    # --------------------------------------------------------

    print("=" * 70)
    print("POC ANNOTATION SAMPLE")
    print("=" * 70)
    print("Pairs:", len(annotation_df))
    print()
    print("Per category:")
    print(annotation_df["category"].value_counts().to_string())
    print()
    print("Our label:")
    print(annotation_df["our_label"].value_counts().to_string())
    print()
    print("Per expense class:")
    print(annotation_df["expense_class"].value_counts().to_string())
    print()
    print("Non-lexical pairs excluded (no unique program expense):", non_lexical_excluded)
    print()
    print("Saved:", OUTPUT_PATH)
    print("=" * 70)


if __name__ == "__main__":
    main()
