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
    OUTPUT_DATA_DIR,
    POSITIVE_PAIRS_CSV_PATH,
    POSITIVE_PAIR_VALIDATOR_SYSTEM_PROMPT_PATH,
    POSITIVE_PAIR_VALIDATOR_PROMPT_PATH,
    POSITIVE_PAIRS_VALIDATED_CSV_PATH,
)


# ============================================================
# Reuse existing LLM utilities
# ============================================================

from src.generator import (
    create_llm,
    clean_json_response,
    read_text_file,
)


# ============================================================
# Excel output
# ============================================================

POSITIVE_PAIRS_VALIDATED_EXCEL_PATH = (
    OUTPUT_DATA_DIR
    / "positive_pairs_validated.xlsx"
)


# ============================================================
# Validation response model
# ============================================================

class PairValidationResult(BaseModel):
    verdict: str = Field(min_length=1)
    reason: str = Field(min_length=1)


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


def normalize_class(value) -> str:
    """
    Normalize expense_class only for comparison.
    """

    return clean_value(value).casefold()


def clean_program_expense_id(value) -> int:
    """
    Convert program_expense_id to integer.
    """

    if pd.isna(value):
        raise ValueError(
            "Missing program_expense_id."
        )

    return int(float(value))


# ============================================================
# Static validation
# ============================================================

def static_validate_pair(
    row: pd.Series,
) -> str:
    """
    Deterministic checks before calling the LLM.

    Returns:
        PASS
        INVALID
        UNCERTAIN

    Rules:
    - program_expense_id must exist
    - client title must exist
    - client description must exist
    - client and program expense_class must exist
    - client/program expense_class must match
    - Category/Subcategory are the main semantic fields
    """

    # --------------------------------------------------------
    # Program Expense ID
    # --------------------------------------------------------

    try:
        clean_program_expense_id(
            row["program_expense_id"]
        )

    except Exception:
        return "UNCERTAIN"

    # --------------------------------------------------------
    # Client title and description are mandatory
    # --------------------------------------------------------

    client_title = clean_value(
        row["client_expense_title"]
    )

    client_description = clean_value(
        row["client_expense_description"]
    )

    if not client_title:
        return "INVALID"

    if not client_description:
        return "INVALID"

    # --------------------------------------------------------
    # Expense class is a hard constraint
    # --------------------------------------------------------

    client_class = normalize_class(
        row["client_expense_class"]
    )

    program_class = normalize_class(
        row["program_expense_class"]
    )

    if not client_class:
        return "INVALID"

    if not program_class:
        return "INVALID"

    if client_class != program_class:
        return "INVALID"

    # --------------------------------------------------------
    # Program semantic information
    # --------------------------------------------------------

    program_category = clean_value(
        row["program_category"]
    )

    program_subcategory = clean_value(
        row["program_subcategory"]
    )

    # Category and Subcategory are the main semantic fields.
    #
    # If BOTH are missing, there is not enough information
    # for reliable semantic validation.
    if (
        not program_category
        and
        not program_subcategory
    ):
        return "UNCERTAIN"

    return "PASS"


# ============================================================
# Build validation prompt
# ============================================================

def build_validation_prompt(
    row: pd.Series,
) -> tuple[str, str]:
    """
    Semantic priority:

    1. Program Category
    2. Program Subcategory
    3. Program Expense Description as supporting information
    """

    system_prompt = read_text_file(
        POSITIVE_PAIR_VALIDATOR_SYSTEM_PROMPT_PATH
    )

    validation_template = read_text_file(
        POSITIVE_PAIR_VALIDATOR_PROMPT_PATH
    )

    validation_prompt = (
        validation_template

        .replace(
            "{client_expense_title}",
            clean_value(
                row["client_expense_title"]
            ),
        )

        .replace(
            "{client_expense_description}",
            clean_value(
                row["client_expense_description"]
            ),
        )

        .replace(
            "{client_expense_class}",
            clean_value(
                row["client_expense_class"]
            ),
        )

        .replace(
            "{program_category}",
            clean_value(
                row["program_category"]
            ),
        )

        .replace(
            "{program_subcategory}",
            clean_value(
                row["program_subcategory"]
            ),
        )

        .replace(
            "{program_expense_description}",
            clean_value(
                row["program_expense_description"]
            ),
        )

        .replace(
            "{program_expense_class}",
            clean_value(
                row["program_expense_class"]
            ),
        )
    )

    return (
        system_prompt,
        validation_prompt,
    )


# ============================================================
# LLM validation
# ============================================================

def validate_pair_with_llm(
    llm,
    row: pd.Series,
) -> PairValidationResult:
    """
    Independently validate whether the proposed
    positive pair is actually valid.
    """

    system_prompt, validation_prompt = (
        build_validation_prompt(row)
    )

    response = llm.invoke(
        [
            (
                "system",
                system_prompt,
            ),
            (
                "human",
                validation_prompt,
            ),
        ]
    )

    response_text = clean_json_response(
        str(response.content)
    )

    data = json.loads(
        response_text
    )

    result = (
        PairValidationResult
        .model_validate(data)
    )

    # --------------------------------------------------------
    # Normalize verdict
    # --------------------------------------------------------

    result.verdict = (
        result.verdict
        .strip()
        .upper()
    )

    allowed_verdicts = {
        "VALID",
        "INVALID",
        "UNCERTAIN",
    }

    if result.verdict not in allowed_verdicts:
        raise ValueError(
            f"Unexpected validator verdict: "
            f"{result.verdict}"
        )

    return result


# ============================================================
# Main
# ============================================================

def main() -> None:

    # --------------------------------------------------------
    # Load proposed positive pairs
    # generated by positive_pair_generator.py
    # --------------------------------------------------------

    pairs_df = pd.read_csv(
        POSITIVE_PAIRS_CSV_PATH,
        encoding="utf-8-sig",
    )

    print(
        f"Positive pairs to validate: "
        f"{len(pairs_df)}"
    )

    # --------------------------------------------------------
    # Create validator LLM
    # --------------------------------------------------------

    llm = create_llm(
        temperature=0.0
    )

    # --------------------------------------------------------
    # ONLY VALID pairs will be stored here
    # --------------------------------------------------------

    valid_rows = []

    invalid_count = 0
    uncertain_count = 0

    # --------------------------------------------------------
    # Validate every proposed pair
    # --------------------------------------------------------

    for counter, (_, row) in enumerate(
        pairs_df.iterrows(),
        start=1,
    ):

        # Only display progress in terminal
        print(
            f"Validating pair "
            f"{counter}/{len(pairs_df)}",
            end="\r",
            flush=True,
        )

        # ----------------------------------------------------
        # Stage 1: Static checks
        # ----------------------------------------------------

        static_status = static_validate_pair(
            row
        )

        if static_status == "INVALID":
            invalid_count += 1
            continue

        if static_status == "UNCERTAIN":
            uncertain_count += 1
            continue

        # ----------------------------------------------------
        # Stage 2: LLM semantic/business validation
        # ----------------------------------------------------

        try:

            result = validate_pair_with_llm(
                llm=llm,
                row=row,
            )

        except Exception:

            # Technical problems must never cause
            # a pair to be accepted as positive.
            uncertain_count += 1
            continue

        # ----------------------------------------------------
        # Discard INVALID pairs
        # ----------------------------------------------------

        if result.verdict == "INVALID":
            invalid_count += 1
            continue

        # ----------------------------------------------------
        # Discard UNCERTAIN pairs
        # ----------------------------------------------------

        if result.verdict == "UNCERTAIN":
            uncertain_count += 1
            continue

        # ----------------------------------------------------
        # VALID pair
        #
        # Keep ONLY the fields required for the dataset.
        # ----------------------------------------------------

        valid_rows.append(
            {
                "program_expense_id":
                    clean_program_expense_id(
                        row["program_expense_id"]
                    ),

                "client_expense_title":
                    clean_value(
                        row["client_expense_title"]
                    ),

                "client_expense_description":
                    clean_value(
                        row["client_expense_description"]
                    ),

                "client_expense_class":
                    clean_value(
                        row["client_expense_class"]
                    ),

                "program_category":
                    clean_value(
                        row["program_category"]
                    ),

                "program_subcategory":
                    clean_value(
                        row["program_subcategory"]
                    ),

                "program_expense_description":
                    clean_value(
                        row[
                            "program_expense_description"
                        ]
                    ),

                "program_expense_class":
                    clean_value(
                        row["program_expense_class"]
                    ),

                "label": 1,
            }
        )

    # ========================================================
    # Final columns
    # ========================================================

    final_columns = [
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

    # ========================================================
    # Build final dataset
    # ========================================================

    validated_df = pd.DataFrame(
        valid_rows,
        columns=final_columns,
    )

    # ========================================================
    # Save ONLY VALID positive pairs
    # ========================================================

    POSITIVE_PAIRS_VALIDATED_CSV_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # CSV
    # --------------------------------------------------------

    validated_df.to_csv(
        POSITIVE_PAIRS_VALIDATED_CSV_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    # --------------------------------------------------------
    # Excel
    # --------------------------------------------------------

    validated_df.to_excel(
        POSITIVE_PAIRS_VALIDATED_EXCEL_PATH,
        index=False,
        engine="openpyxl",
    )

    # ========================================================
    # Final summary
    # ========================================================

    print()
    print()
    print("=" * 60)
    print("POSITIVE PAIR VALIDATION SUMMARY")
    print("=" * 60)

    print(
        f"Proposed positive pairs: "
        f"{len(pairs_df)}"
    )

    print(
        f"VALID pairs kept: "
        f"{len(validated_df)}"
    )

    print(
        f"INVALID pairs discarded: "
        f"{invalid_count}"
    )

    print(
        f"UNCERTAIN / errors discarded: "
        f"{uncertain_count}"
    )

    print()

    print(
        f"Validated CSV:\n"
        f"{POSITIVE_PAIRS_VALIDATED_CSV_PATH}"
    )

    print()

    print(
        f"Validated Excel:\n"
        f"{POSITIVE_PAIRS_VALIDATED_EXCEL_PATH}"
    )

    print("=" * 60)


if __name__ == "__main__":
    main()