from pathlib import Path

import pandas as pd


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

def find_sheet(xls: pd.ExcelFile, sheet_name: str):
    """
    Finds a sheet by exact name, ignoring case and surrounding spaces.
    """
    for sheet in xls.sheet_names:
        if (
            sheet.strip().lower()
            == sheet_name.strip().lower()
        ):
            return sheet

    return None


def read_program_metadata(
    path: Path,
    program_sheet: str,
):
    """
    Reads the Program sheet.

    The Program sheet is structured as:
        Field Name | Value

    Example:
        Title       | ...
        Description | ...
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

    if df_program.shape[1] < 2:
        return program_title, program_description

    # First column = metadata field
    # Second column = value
    keys = (
        df_program.iloc[:, 0]
        .astype(str)
        .str.strip()
    )

    values = df_program.iloc[:, 1]

    program_metadata = dict(
        zip(keys, values)
    )

    program_title = program_metadata.get(
        "Title"
    )

    program_description = program_metadata.get(
        "Description"
    )

    return (
        program_title,
        program_description,
    )


def read_subprograms(
    path: Path,
    subprogram_sheet: str,
):
    """
    Reads Subprograms sheet.

    Keeps only the fields needed to connect
    each expense to its subprogram.
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
    expenses_sheet: str,
):
    """
    Reads the Expenses sheet.
    """

    if expenses_sheet is None:
        return None

    df_expenses = pd.read_excel(
        path,
        sheet_name=expenses_sheet,
    )

    return df_expenses


# ============================================================
# Process one Funding Program file
# ============================================================

def process_program_file(path: Path):
    print(f"Processing: {path.name}")

    xls = pd.ExcelFile(path)

    program_sheet = find_sheet(
        xls,
        "Program",
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
            f"no Expenses sheet found"
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
    # Add Program information
    # --------------------------------------------------------

    df_expenses["program_title"] = (
        program_title
    )

    df_expenses["program_description"] = (
        program_description
    )

    df_expenses["source_file"] = (
        path.name
    )

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
    }

    df_expenses = df_expenses.rename(
        columns=rename_map
    )

    # --------------------------------------------------------
    # Columns we want in the clean dataset
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
    ]

    # Add missing columns as empty
    # so every Excel produces the same schema
    for column in wanted_columns:
        if column not in df_expenses.columns:
            df_expenses[column] = None

    df_expenses = df_expenses[
        wanted_columns
    ]

    # --------------------------------------------------------
    # Basic cleaning
    # --------------------------------------------------------

    text_columns = [
        "source_file",
        "program_title",
        "program_description",
        "subprogram_name",
        "category",
        "subcategory",
        "expense_description",
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

    # Remove completely empty expense rows
    expense_content_columns = [
        "category",
        "subcategory",
        "expense_description",
    ]

    df_expenses = df_expenses.dropna(
        subset=expense_content_columns,
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
                f"ERROR processing "
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

    # --------------------------------------------------------
    # Remove exact duplicate rows
    # --------------------------------------------------------

    rows_before = len(final_df)

    final_df = final_df.drop_duplicates()

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
    print(
        f"Saved to: "
        f"{OUTPUT_PATH}"
    )

    print("=" * 60)


if __name__ == "__main__":
    main()