from pathlib import Path
import warnings

import pandas as pd


# ============================================================
# Ignore harmless Excel validation warnings
# ============================================================

warnings.filterwarnings(
    "ignore",
    message="Data Validation extension is not supported and will be removed",
)


# ============================================================
# Paths
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

PROGRAMS_DIR = BASE_DIR / "data" / "raw"

OUTPUT_PATH = (
    BASE_DIR
    / "data"
    / "processed"
    / "program_expenses_clean.csv"
)


# ============================================================
# Helpers
# ============================================================

def find_sheet(xls: pd.ExcelFile, sheet_name: str) -> str | int | None:
    """
    Finds a sheet by exact name,
    ignoring case and surrounding spaces.
    """

    for sheet in xls.sheet_names:
        if str(sheet).strip().lower() == sheet_name.strip().lower():
            return sheet

    return None


def read_program_metadata(
    path: Path,
    program_sheet: str | int | None,
):
    """
    Reads Program_Fields.

    Searches the whole sheet for:
    - Title
    - Description

    and takes the value immediately to the right.
    """

    program_title = None
    program_description = None

    if program_sheet is None:
        return program_title, program_description

    df_program = pd.read_excel(
        path,
        sheet_name=program_sheet,
        header=None,
    )

    for row_idx in range(df_program.shape[0]):

        for col_idx in range(df_program.shape[1]):

            value = df_program.iat[
                row_idx,
                col_idx,
            ]

            if pd.isna(value):
                continue

            key = str(value).strip()

            if col_idx + 1 >= df_program.shape[1]:
                continue

            metadata_value = df_program.iat[
                row_idx,
                col_idx + 1,
            ]

            if key == "Title":
                program_title = metadata_value

            elif key == "Description":
                program_description = metadata_value

    return (
        program_title,
        program_description,
    )


def read_subprograms(
    path: Path,
    subprogram_sheet: str | int | None,
):
    """
    Reads the Subprograms sheet.
    """

    if subprogram_sheet is None:
        return None

    df_subprograms = pd.read_excel(
        path,
        sheet_name=subprogram_sheet,
    )

    required_columns = [
        "Subprogram_ID",
        "Subprogram_Name",
    ]

    existing_columns = [
        column
        for column in required_columns
        if column in df_subprograms.columns
    ]

    if "Subprogram_ID" not in existing_columns:
        return None

    return df_subprograms[
        existing_columns
    ].copy()


def read_expenses(
    path: Path,
    expenses_sheet: str | int | None,
):
    """
    Reads the Expenses sheet.
    """

    if expenses_sheet is None:
        return None

    return pd.read_excel(
        path,
        sheet_name=expenses_sheet,
    )


# ============================================================
# Process one Funding Program
# ============================================================

def process_program_file(path: Path):

    print(f"Processing: {path.name}")

    xls = pd.ExcelFile(path)

    program_sheet = find_sheet(
        xls,
        "Program_Fields",
    )

    subprogram_sheet = find_sheet(
        xls,
        "Subprograms",
    )

    expenses_sheet = find_sheet(
        xls,
        "Expenses",
    )

    if expenses_sheet is None:
        print(
            f"Skipping {path.name}: "
            "no Expenses sheet found"
        )
        return None

    # --------------------------------------------------------
    # Program-level information
    # --------------------------------------------------------

    (
        program_title,
        program_description,
    ) = read_program_metadata(
        path,
        program_sheet,
    )

    # --------------------------------------------------------
    # Expenses
    # --------------------------------------------------------

    df_expenses = read_expenses(
        path,
        expenses_sheet,
    )

    if df_expenses is None:
        return None

    # --------------------------------------------------------
    # Subprograms
    # --------------------------------------------------------

    df_subprograms = read_subprograms(
        path,
        subprogram_sheet,
    )

    if (
        df_subprograms is not None
        and "Subprogram_ID" in df_expenses.columns
        and "Subprogram_ID" in df_subprograms.columns
    ):
        df_expenses = df_expenses.merge(
            df_subprograms,
            on="Subprogram_ID",
            how="left",
        )

    # --------------------------------------------------------
    # Add Program-level information
    # --------------------------------------------------------

    df_expenses["program_title"] = program_title
    df_expenses["program_description"] = program_description
    df_expenses["source_file"] = path.name

    # --------------------------------------------------------
    # Standardize column names
    # --------------------------------------------------------

    rename_map = {
        "Subprogram_ID": "subprogram_id",
        "Subprogram_Name": "subprogram_name",
        "Category_ID": "category_id",
        "Category": "category",
        "Subcategory_ID": "subcategory_id",
        "Subcategory": "subcategory",
        "Description": "expense_description",
        "Expense_Class": "expense_class",
    }

    df_expenses = df_expenses.rename(
        columns=rename_map
    )

    # --------------------------------------------------------
    # Final columns
    # --------------------------------------------------------

    wanted_columns = [
        "source_file",
        "program_title",
        "program_description",
        "subprogram_id",
        "subprogram_name",
        "category_id",
        "category",
        "subcategory_id",
        "subcategory",
        "expense_description",
        "expense_class",
    ]

    # Ensure same schema for every workbook
    for column in wanted_columns:
        if column not in df_expenses.columns:
            df_expenses[column] = None

    df_expenses = df_expenses[
        wanted_columns
    ]

    # --------------------------------------------------------
    # Basic text cleaning
    # --------------------------------------------------------

    text_columns = [
        "source_file",
        "program_title",
        "program_description",
        "subprogram_name",
        "category",
        "subcategory",
        "expense_description",
        "expense_class",
    ]

    for column in text_columns:

        df_expenses[column] = (
            df_expenses[column]
            .apply(
                lambda x:
                x.strip()
                if isinstance(x, str)
                else x
            )
        )

    # --------------------------------------------------------
    # Remove completely empty expense rows
    # --------------------------------------------------------

    df_expenses = df_expenses.dropna(
        subset=[
            "category",
            "subcategory",
            "expense_description",
            "expense_class",
        ],
        how="all",
    )

    return df_expenses


# ============================================================
# Main
# ============================================================

def main():

    program_files = sorted(
        PROGRAMS_DIR.glob("*.xlsx")
    )

    if not program_files:

        raise ValueError(
            "No .xlsx program files found in: "
            f"{PROGRAMS_DIR.resolve()}"
        )

    all_program_expenses = []

    for path in program_files:

        try:

            df = process_program_file(
                path
            )

            if (
                df is not None
                and not df.empty
            ):
                all_program_expenses.append(
                    df
                )

        except Exception as error:

            print()
            print(
                f"ERROR processing: "
                f"{path.name}"
            )
            print(error)
            print()

    if not all_program_expenses:

        raise ValueError(
            "No program expenses were "
            "successfully extracted."
        )

    # --------------------------------------------------------
    # Combine all program expenses
    # --------------------------------------------------------

    final_df = pd.concat(
        all_program_expenses,
        ignore_index=True,
    )

    rows_before = len(final_df)

    # --------------------------------------------------------
    # Exact duplicate removal
    # --------------------------------------------------------

    final_df = final_df.drop_duplicates()
    final_df = final_df.reset_index(
        drop=True
    )

    rows_after = len(final_df)

    duplicates_removed = (
        rows_before - rows_after
    )

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    final_df.to_csv(
        OUTPUT_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print()
    print("=" * 60)

    print(
        f"Program files found: "
        f"{len(program_files)}"
    )

    print(
        f"Program files processed: "
        f"{len(all_program_expenses)}"
    )

    print(
        f"Program expenses before "
        f"deduplication: {rows_before}"
    )

    print(
        f"Exact duplicate rows removed: "
        f"{duplicates_removed}"
    )

    print(
        f"Final program expenses: "
        f"{len(final_df)}"
    )

    print()

    print("Missing values:")

    print(
        final_df.isna().sum()
    )

    print()

    print("Expense class counts:")

    if "expense_class" in final_df.columns:
        print(
            final_df["expense_class"]
            .value_counts(
                dropna=False
            )
            .to_string()
        )

    print()

    print(
        f"Saved to: "
        f"{OUTPUT_PATH}"
    )

    print("=" * 60)


if __name__ == "__main__":
    main()