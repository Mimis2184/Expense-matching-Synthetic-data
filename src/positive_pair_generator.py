import json
import sys
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, Field


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
    POSITIVE_PAIR_SYSTEM_PROMPT_PATH,
    POSITIVE_PAIR_GENERATION_PROMPT_PATH,
    POSITIVE_TARGET_TOTAL,
    POSITIVE_ANCHORS_PER_CLASS,
    POSITIVE_PAIRS_CSV_PATH,
    POSITIVE_PAIRS_EXCEL_PATH,
)


# ============================================================
# Reuse existing generator utilities
# ============================================================

from src.generator import (
    create_llm,
    clean_json_response,
    read_text_file,
    get_style_profile,
    get_seeds,
    format_style_profile,
    format_seeds,
)


# ============================================================
# Input data
# ============================================================

PROGRAM_DATA_PATH = (
    BASE_DIR
    / "data"
    / "processed"
    / "program_expenses_clean.csv"
)


# ============================================================
# LLM output models
# ============================================================

class SyntheticClientExpense(BaseModel):

    expense_title: str = Field(
        min_length=1
    )

    expense_description: str = Field(
        min_length=1
    )

    expense_class: str = Field(
        min_length=1
    )


class SyntheticClientBatch(BaseModel):

    expenses: list[SyntheticClientExpense]


# ============================================================
# Helpers
# ============================================================

def clean_value(value) -> str:

    if pd.isna(value):
        return ""

    return str(value).strip()


def normalize_class(value) -> str:

    return clean_value(value).casefold()


# ============================================================
# Prepare valid Program Expenses
# ============================================================

def get_valid_program_expenses(
    program_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Keep Program Expenses that can reasonably be used
    as anchors for positive generation.

    Requirements:
    - expense_class must exist
    - at least Category or Subcategory must exist

    Category and Subcategory are our primary
    semantic fields.
    """

    valid_df = program_df.copy()

    # --------------------------------------------------------
    # Expense class must exist
    # --------------------------------------------------------

    has_class = (
        valid_df["expense_class"]
        .fillna("")
        .astype(str)
        .str.strip()
        .ne("")
    )

    valid_df = valid_df[
        has_class
    ].copy()

    # --------------------------------------------------------
    # At least Category OR Subcategory must exist
    # --------------------------------------------------------

    has_category = (
        valid_df["category"]
        .fillna("")
        .astype(str)
        .str.strip()
        .ne("")
    )

    has_subcategory = (
        valid_df["subcategory"]
        .fillna("")
        .astype(str)
        .str.strip()
        .ne("")
    )

    valid_df = valid_df[
        has_category
        |
        has_subcategory
    ].copy()

    if valid_df.empty:

        raise ValueError(
            "No valid Program Expense anchors found."
        )

    return valid_df


# ============================================================
# Allocate target across expense classes
# ============================================================

def allocate_class_targets(
    valid_df: pd.DataFrame,
) -> dict[str, int]:
    """
    Distribute POSITIVE_TARGET_TOTAL as evenly as possible
    across all available Program Expense classes.

    Example with 500 pairs and 14 classes:

    500 // 14 = 35
    remainder = 10

    Therefore:
    - 10 classes receive 36
    - 4 classes receive 35
    """

    expense_classes = sorted(
        valid_df[
            "expense_class"
        ]
        .dropna()
        .astype(str)
        .str.strip()
        .unique()
    )

    if not expense_classes:

        raise ValueError(
            "No expense classes available."
        )

    n_classes = len(
        expense_classes
    )

    base_target = (
        POSITIVE_TARGET_TOTAL
        //
        n_classes
    )

    remainder = (
        POSITIVE_TARGET_TOTAL
        %
        n_classes
    )

    targets = {}

    for position, expense_class in enumerate(
        expense_classes
    ):

        targets[
            expense_class
        ] = (
            base_target
            +
            (
                1
                if position < remainder
                else 0
            )
        )

    return targets


# ============================================================
# Build generation plan
# ============================================================

def build_generation_plan(
    valid_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Create a balanced generation plan.

    For every expense class:

    1. Determine how many synthetic positives are needed.
    2. Select up to POSITIVE_ANCHORS_PER_CLASS different
       real Program Expense anchors.
    3. Spread the synthetic clients as evenly as possible
       across those anchors.

    Example:

    class target = 36
    selected anchors = 10

    generation counts may become:

    4, 4, 4, 4, 4, 4,
    3, 3, 3, 3
    """

    class_targets = allocate_class_targets(
        valid_df
    )

    plan_rows = []

    for expense_class, class_target in (
        class_targets.items()
    ):

        class_df = valid_df[
            valid_df["expense_class"]
            .astype(str)
            .str.strip()
            .eq(expense_class)
        ].copy()

        if class_df.empty:
            continue

        # ----------------------------------------------------
        # Select several different real anchors
        # ----------------------------------------------------

        n_anchors = min(
            POSITIVE_ANCHORS_PER_CLASS,
            len(class_df),
            class_target,
        )

        selected_anchors = (
            class_df.sample(
                n=n_anchors,
                random_state=42,
            )
            .reset_index(drop=True)
        )

        # ----------------------------------------------------
        # Spread class target across selected anchors
        # ----------------------------------------------------

        base_per_anchor = (
            class_target
            //
            n_anchors
        )

        remainder = (
            class_target
            %
            n_anchors
        )

        for position, (_, program_row) in enumerate(
            selected_anchors.iterrows()
        ):

            n_to_generate = (
                base_per_anchor
                +
                (
                    1
                    if position < remainder
                    else 0
                )
            )

            plan_row = (
                program_row.to_dict()
            )

            plan_row[
                "n_to_generate"
            ] = n_to_generate

            plan_rows.append(
                plan_row
            )

    generation_plan = pd.DataFrame(
        plan_rows
    )

    if generation_plan.empty:

        raise ValueError(
            "Generation plan is empty."
        )

    return generation_plan


# ============================================================
# Build generation prompt
# ============================================================

def build_positive_prompt(
    program_row: pd.Series,
    n_to_generate: int,
) -> tuple[str, str]:
    """
    Build the prompt for one specific Program Expense anchor.

    Main semantic information:
    - Category
    - Subcategory

    Supporting information:
    - Program Expense Description

    Real client examples are used only for writing style.
    """

    expense_class = clean_value(
        program_row["expense_class"]
    )

    # --------------------------------------------------------
    # Real client style
    # --------------------------------------------------------

    style_profile = get_style_profile(
        expense_class
    )

    seeds = get_seeds(
        expense_class
    )

    style_profile_text = (
        format_style_profile(
            style_profile
        )
    )

    seeds_text = format_seeds(
        seeds
    )

    # --------------------------------------------------------
    # Read prompts
    # --------------------------------------------------------

    system_prompt = read_text_file(
        POSITIVE_PAIR_SYSTEM_PROMPT_PATH
    )

    generation_template = read_text_file(
        POSITIVE_PAIR_GENERATION_PROMPT_PATH
    )

    # --------------------------------------------------------
    # Program fields
    # --------------------------------------------------------

    program_title = clean_value(
        program_row.get(
            "program_title",
            "",
        )
    )

    subprogram_name = clean_value(
        program_row.get(
            "subprogram_name",
            "",
        )
    )

    category = clean_value(
        program_row["category"]
    )

    subcategory = clean_value(
        program_row["subcategory"]
    )

    program_description = clean_value(
        program_row[
            "expense_description"
        ]
    )

    # --------------------------------------------------------
    # Fill prompt
    # --------------------------------------------------------

    generation_prompt = (
        generation_template

        .replace(
            "{n_to_generate}",
            str(n_to_generate),
        )

        .replace(
            "{program_title}",
            program_title,
        )

        .replace(
            "{subprogram_name}",
            subprogram_name,
        )

        .replace(
            "{expense_class}",
            expense_class,
        )

        .replace(
            "{category}",
            category,
        )

        .replace(
            "{subcategory}",
            subcategory,
        )

        .replace(
            "{program_expense_description}",
            program_description,
        )

        .replace(
            "{style_profile}",
            style_profile_text,
        )

        .replace(
            "{seeds}",
            seeds_text,
        )
    )

    return (
        system_prompt,
        generation_prompt,
    )


# ============================================================
# Generate clients for one Program Expense
# ============================================================

def generate_for_program_expense(
    llm,
    program_row: pd.Series,
    n_to_generate: int,
) -> list[SyntheticClientExpense]:
    """
    Generate one or more synthetic client expenses
    that should be valid positive matches for the
    given real Program Expense anchor.
    """

    expected_expense_class = clean_value(
        program_row["expense_class"]
    )

    system_prompt, generation_prompt = (
        build_positive_prompt(
            program_row=program_row,
            n_to_generate=n_to_generate,
        )
    )

    # --------------------------------------------------------
    # LLM call
    # --------------------------------------------------------

    response = llm.invoke(
        [
            (
                "system",
                system_prompt,
            ),
            (
                "human",
                generation_prompt,
            ),
        ]
    )

    response_text = clean_json_response(
        str(response.content)
    )

    data = json.loads(
        response_text
    )

    validated_batch = (
        SyntheticClientBatch
        .model_validate(data)
    )

    # --------------------------------------------------------
    # Correct number of generated clients
    # --------------------------------------------------------

    if (
        len(validated_batch.expenses)
        != n_to_generate
    ):

        raise ValueError(
            "Unexpected number of generated "
            "synthetic client expenses. "
            f"Expected {n_to_generate}, "
            f"received "
            f"{len(validated_batch.expenses)}."
        )

    # --------------------------------------------------------
    # Validate every generated client
    # --------------------------------------------------------

    for synthetic in (
        validated_batch.expenses
    ):

        # ----------------------------------------------------
        # Class is a hard constraint
        # ----------------------------------------------------

        if (
            normalize_class(
                synthetic.expense_class
            )
            !=
            normalize_class(
                expected_expense_class
            )
        ):

            raise ValueError(
                "Generated client expense has "
                "incorrect expense_class."
            )

        # ----------------------------------------------------
        # Both title AND description are mandatory
        # ----------------------------------------------------

        if not synthetic.expense_title.strip():

            raise ValueError(
                "Generated client expense has "
                "an empty title."
            )

        if not synthetic.expense_description.strip():

            raise ValueError(
                "Generated client expense has "
                "an empty description."
            )

    return validated_batch.expenses


# ============================================================
# Main
# ============================================================

def main() -> None:

    # --------------------------------------------------------
    # Load Program Expenses
    # --------------------------------------------------------

    program_df = pd.read_csv(
        PROGRAM_DATA_PATH,
        encoding="utf-8-sig",
    )

    program_df = (
        program_df
        .reset_index(drop=True)
    )

    # --------------------------------------------------------
    # Internal ID for traceability
    # --------------------------------------------------------

    program_df[
        "program_expense_id"
    ] = (
        program_df.index + 1
    )

    print(
        f"Program expenses loaded: "
        f"{len(program_df)}"
    )

    # --------------------------------------------------------
    # Filter valid anchors
    # --------------------------------------------------------

    valid_program_df = (
        get_valid_program_expenses(
            program_df
        )
    )

    print(
        f"Valid Program Expense anchors available: "
        f"{len(valid_program_df)}"
    )

    print(
        f"Expense classes available: "
        f"{valid_program_df['expense_class'].nunique()}"
    )

    # --------------------------------------------------------
    # Build balanced generation plan
    # --------------------------------------------------------

    generation_plan = (
        build_generation_plan(
            valid_program_df
        )
    )

    planned_total = int(
        generation_plan[
            "n_to_generate"
        ].sum()
    )

    print(
        f"Planned proposed positives: "
        f"{planned_total}"
    )

    print(
        f"Program anchors selected: "
        f"{len(generation_plan)}"
    )

    # --------------------------------------------------------
    # Show target distribution
    # --------------------------------------------------------

    planned_distribution = (
        generation_plan
        .groupby(
            "expense_class"
        )["n_to_generate"]
        .sum()
    )

    print()
    print(
        "Planned pairs per expense class:"
    )

    print(
        planned_distribution.to_string()
    )

    # --------------------------------------------------------
    # Create LLM once
    # --------------------------------------------------------

    llm = create_llm(
        temperature=0.8
    )

    # --------------------------------------------------------
    # Generate proposed positive pairs
    # --------------------------------------------------------

    rows = []

    for counter, (_, program_row) in enumerate(
        generation_plan.iterrows(),
        start=1,
    ):

        expense_class = clean_value(
            program_row["expense_class"]
        )

        category = clean_value(
            program_row["category"]
        )

        subcategory = clean_value(
            program_row["subcategory"]
        )

        n_to_generate = int(
            program_row[
                "n_to_generate"
            ]
        )

        print()
        print("=" * 60)

        print(
            f"Anchor "
            f"{counter}/"
            f"{len(generation_plan)}"
        )

        print(
            f"Expense class: "
            f"{expense_class}"
        )

        print(
            f"Category: "
            f"{category}"
        )

        print(
            f"Subcategory: "
            f"{subcategory}"
        )

        print(
            f"Synthetic clients requested: "
            f"{n_to_generate}"
        )

        # ----------------------------------------------------
        # Generate clients
        # ----------------------------------------------------

        try:

            synthetic_clients = (
                generate_for_program_expense(
                    llm=llm,
                    program_row=program_row,
                    n_to_generate=n_to_generate,
                )
            )

        except Exception as exc:

            # One failed anchor does not terminate
            # the entire generation process.
            print(
                f"Generation failed: "
                f"{exc}"
            )

            continue

        # ----------------------------------------------------
        # Client + anchor = proposed positive
        # ----------------------------------------------------

        for synthetic in (
            synthetic_clients
        ):

            rows.append(
                {
                    # ========================================
                    # Client side
                    # ========================================

                    "client_expense_title":
                        synthetic.expense_title.strip(),

                    "client_expense_description":
                        synthetic.expense_description.strip(),

                    "client_expense_class":
                        synthetic.expense_class.strip(),


                    # ========================================
                    # Program side
                    # ========================================

                    "program_expense_id":
                        program_row[
                            "program_expense_id"
                        ],

                    "program_title":
                        clean_value(
                            program_row.get(
                                "program_title",
                                "",
                            )
                        ),

                    "subprogram_name":
                        clean_value(
                            program_row.get(
                                "subprogram_name",
                                "",
                            )
                        ),

                    "program_category":
                        category,

                    "program_subcategory":
                        subcategory,

                    "program_expense_description":
                        clean_value(
                            program_row[
                                "expense_description"
                            ]
                        ),

                    "program_expense_class":
                        expense_class,


                    # ========================================
                    # Proposed binary label
                    #
                    # Still needs validation.
                    # ========================================

                    "label": 1,
                }
            )

    # ========================================================
    # Build output DataFrame
    # ========================================================

    positive_df = pd.DataFrame(
        rows
    )

    if positive_df.empty:

        raise ValueError(
            "No positive pairs were generated."
        )

    # ========================================================
    # Final deterministic checks
    # ========================================================

    # --------------------------------------------------------
    # Title must exist
    # --------------------------------------------------------

    invalid_title = (
        positive_df[
            "client_expense_title"
        ]
        .fillna("")
        .astype(str)
        .str.strip()
        .eq("")
    )

    # --------------------------------------------------------
    # Description must exist
    # --------------------------------------------------------

    invalid_description = (
        positive_df[
            "client_expense_description"
        ]
        .fillna("")
        .astype(str)
        .str.strip()
        .eq("")
    )

    # --------------------------------------------------------
    # Client and program classes must match
    # --------------------------------------------------------

    class_match = (
        positive_df[
            "client_expense_class"
        ]
        .astype(str)
        .str.strip()
        .str.casefold()
        ==
        positive_df[
            "program_expense_class"
        ]
        .astype(str)
        .str.strip()
        .str.casefold()
    )

    invalid_rows = (
        invalid_title
        |
        invalid_description
        |
        ~class_match
    )

    if invalid_rows.any():

        print()
        print(
            f"Removing "
            f"{int(invalid_rows.sum())} "
            f"invalid generated pair(s)."
        )

        positive_df = (
            positive_df[
                ~invalid_rows
            ]
            .copy()
        )

    # ========================================================
    # Remove exact duplicate pairs
    # ========================================================

    before_dedup = len(
        positive_df
    )

    positive_df = (
        positive_df
        .drop_duplicates(
            subset=[
                "client_expense_title",
                "client_expense_description",
                "client_expense_class",
                "program_expense_id",
            ]
        )
        .reset_index(drop=True)
    )

    duplicates_removed = (
        before_dedup
        -
        len(positive_df)
    )

    if duplicates_removed > 0:

        print(
            f"Exact duplicate pairs removed: "
            f"{duplicates_removed}"
        )

    # ========================================================
    # Save
    # ========================================================

    POSITIVE_PAIRS_CSV_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    positive_df.to_csv(
        POSITIVE_PAIRS_CSV_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    positive_df.to_excel(
        POSITIVE_PAIRS_EXCEL_PATH,
        index=False,
        engine="openpyxl",
    )

    # ========================================================
    # Summary
    # ========================================================

    print()
    print("=" * 60)
    print(
        "POSITIVE PAIR GENERATION SUMMARY"
    )
    print("=" * 60)

    print(
        f"Target proposed positives: "
        f"{POSITIVE_TARGET_TOTAL}"
    )

    print(
        f"Actual proposed positives: "
        f"{len(positive_df)}"
    )

    print(
        f"Expense classes covered: "
        f"{positive_df['client_expense_class'].nunique()}"
    )

    print()

    print(
        "Generated pairs per expense class:"
    )

    print(
        positive_df[
            "client_expense_class"
        ]
        .value_counts()
        .to_string()
    )

    print()

    print(
        f"Saved CSV:\n"
        f"{POSITIVE_PAIRS_CSV_PATH}"
    )

    print()

    print(
        f"Saved Excel:\n"
        f"{POSITIVE_PAIRS_EXCEL_PATH}"
    )

    print("=" * 60)


if __name__ == "__main__":
    main()