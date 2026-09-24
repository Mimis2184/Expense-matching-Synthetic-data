import json
import re
import sys
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from pydantic import BaseModel, Field


sys.path.insert(
    0,
    str(Path(__file__).parent.parent),
)


from config import (
    CLEAN_CLIENT_DATA_PATH,
    TEST_OUTPUT_PATH,
    VALIDATED_TEST_OUTPUT_PATH,
    VALIDATED_TEST_EXCEL_PATH,
    VALIDATION_MIN_CONFIDENCE,
)

from src.generator import (
    create_llm,
    clean_json_response,
)


# ---------------------------------------------------------
# Required columns
# ---------------------------------------------------------

REQUIRED_COLUMNS = [
    "expense_title",
    "expense_description",
    "expense_class",
]


# ---------------------------------------------------------
# Pydantic model για semantic validation
# ---------------------------------------------------------

class SemanticValidationResult(BaseModel):

    valid: bool

    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )

    reason: str = Field(
        min_length=1
    )


# ---------------------------------------------------------
# Normalize text για deterministic comparisons
# ---------------------------------------------------------

def normalize_text(
    value: str,
) -> str:

    value = str(value).strip().lower()

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value


# ---------------------------------------------------------
# Έλεγχος required columns
# ---------------------------------------------------------

def validate_required_columns(
    df: pd.DataFrame,
) -> None:

    missing = [
        column
        for column in REQUIRED_COLUMNS
        if column not in df.columns
    ]

    if missing:
        raise ValueError(
            "Missing required columns: "
            + ", ".join(missing)
        )


# ---------------------------------------------------------
# Static validation
# ---------------------------------------------------------

def static_validate(
    synthetic_df: pd.DataFrame,
    real_df: pd.DataFrame,
) -> pd.DataFrame:

    validate_required_columns(
        synthetic_df
    )

    validate_required_columns(
        real_df
    )

    df = synthetic_df.copy()

    # Normalized helper columns

    df["_title_norm"] = (
        df["expense_title"]
        .fillna("")
        .map(normalize_text)
    )

    df["_description_norm"] = (
        df["expense_description"]
        .fillna("")
        .map(normalize_text)
    )

    df["_class_norm"] = (
        df["expense_class"]
        .fillna("")
        .map(normalize_text)
    )

    # Empty fields

    df["has_empty_field"] = (
        (df["_title_norm"] == "")
        | (df["_description_norm"] == "")
        | (df["_class_norm"] == "")
    )

    # Valid expense classes

    valid_classes = set(
        real_df["expense_class"]
        .dropna()
        .map(normalize_text)
    )

    df["invalid_expense_class"] = (
        ~df["_class_norm"].isin(
            valid_classes
        )
    )

    # Duplicate synthetic examples

    df["duplicate_synthetic"] = (
        df.duplicated(
            subset=[
                "_title_norm",
                "_description_norm",
                "_class_norm",
            ],
            keep="first",
        )
    )

    # Exact copies από real data

    real_keys = set(
        zip(
            real_df["expense_title"]
            .fillna("")
            .map(normalize_text),

            real_df["expense_description"]
            .fillna("")
            .map(normalize_text),

            real_df["expense_class"]
            .fillna("")
            .map(normalize_text),
        )
    )

    df["exact_real_copy"] = [
        (
            title,
            description,
            expense_class,
        )
        in real_keys

        for title, description, expense_class
        in zip(
            df["_title_norm"],
            df["_description_norm"],
            df["_class_norm"],
        )
    ]

    # Overall static result

    df["static_valid"] = ~(
        df["has_empty_field"]
        | df["invalid_expense_class"]
        | df["duplicate_synthetic"]
        | df["exact_real_copy"]
    )

    # Static reason

    reasons = []

    for _, row in df.iterrows():

        row_reasons = []

        if row["has_empty_field"]:
            row_reasons.append(
                "empty required field"
            )

        if row["invalid_expense_class"]:
            row_reasons.append(
                "unknown expense class"
            )

        if row["duplicate_synthetic"]:
            row_reasons.append(
                "duplicate synthetic example"
            )

        if row["exact_real_copy"]:
            row_reasons.append(
                "exact copy of real example"
            )

        if not row_reasons:
            row_reasons.append("ok")

        reasons.append(
            "; ".join(row_reasons)
        )

    df["static_reason"] = reasons

    return df


# ---------------------------------------------------------
# Semantic validation prompts
# ---------------------------------------------------------

SEMANTIC_SYSTEM_PROMPT = """
You are validating synthetic business expense data
for an expense classification dataset.

Your task is to evaluate semantic consistency only.

Judge whether the expense title and description
logically belong to the stated expense class.

Do not judge grammar, spelling, writing quality,
or whether the text is professionally written.

Real client text may be short, incomplete,
informal, uppercase, Greek, English,
or mixed Greek-English.

Return valid JSON only.
Do not use markdown.
"""


SEMANTIC_USER_PROMPT = """
Evaluate the following synthetic expense.

EXPENSE TITLE:
{expense_title}

EXPENSE DESCRIPTION:
{expense_description}

EXPECTED EXPENSE CLASS:
{expense_class}

Question:

If you saw only the expense title and description,
would it be reasonable to classify this expense
into the stated expense class?

Rules:

- valid=true only when the expense is semantically
  consistent with the stated class.
- If the expense mainly describes another type of
  expense, return valid=false.
- If the text mixes multiple different expense types
  and the intended class is unclear, lower confidence.
- Do not reject an expense merely because the text is
  short, informal, incomplete, uppercase, or mixed-language.

Return exactly:

{
  "valid": true,
  "confidence": 0.0,
  "reason": "short explanation"
}
"""


# ---------------------------------------------------------
# Semantic validation ενός row
# ---------------------------------------------------------

def semantic_validate_row(
    llm,
    row: pd.Series,
) -> dict:

    prompt = (
        SEMANTIC_USER_PROMPT
        .replace(
            "{expense_title}",
            str(row["expense_title"]),
        )
        .replace(
            "{expense_description}",
            str(row["expense_description"]),
        )
        .replace(
            "{expense_class}",
            str(row["expense_class"]),
        )
    )

    try:

        response = llm.invoke(
            [
                (
                    "system",
                    SEMANTIC_SYSTEM_PROMPT,
                ),
                (
                    "human",
                    prompt,
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
            SemanticValidationResult
            .model_validate(data)
        )

        return {
            "semantic_valid": result.valid,
            "semantic_confidence": round(
                result.confidence,
                3,
            ),
            "semantic_reason": result.reason,
            "semantic_error": False,
        }

    except Exception as error:

        return {
            "semantic_valid": None,
            "semantic_confidence": 0.0,
            "semantic_reason": str(error),
            "semantic_error": True,
        }


# ---------------------------------------------------------
# Full validation pipeline
# ---------------------------------------------------------

def validate_synthetic_dataset(
    synthetic_df: pd.DataFrame,
    real_df: pd.DataFrame,
) -> pd.DataFrame:

    df = static_validate(
        synthetic_df=synthetic_df,
        real_df=real_df,
    )

    df["semantic_valid"] = None
    df["semantic_confidence"] = 0.0
    df["semantic_reason"] = ""
    df["semantic_error"] = False

    # Μόνο rows που πέρασαν το static validation
    # πηγαίνουν στο LLM validator.

    rows_to_validate = df[
        df["static_valid"]
    ]

    if not rows_to_validate.empty:

        llm = create_llm(
            temperature=0.0
        )

        # Κάθε row ελέγχεται ξεχωριστά

        for index, row in (
            rows_to_validate.iterrows()
        ):

            result = semantic_validate_row(
                llm=llm,
                row=row,
            )

            for key, value in result.items():

                df.at[
                    index,
                    key,
                ] = value

    # -----------------------------------------------------
    # Final validation status
    # -----------------------------------------------------

    statuses = []

    for _, row in df.iterrows():

        if not row["static_valid"]:

            status = "reject_static"

        elif row["semantic_error"]:

            status = "validation_error"

        elif row["semantic_valid"] == False:

            status = "reject_semantic"

        elif (
            row["semantic_confidence"]
            < VALIDATION_MIN_CONFIDENCE
        ):

            status = "manual_review"

        else:

            status = "pass"

        statuses.append(
            status
        )

    df["validation_status"] = statuses

    # Δεν χρειαζόμαστε πλέον τα helper columns

    df = df.drop(
        columns=[
            "_title_norm",
            "_description_norm",
            "_class_norm",
        ]
    )

    return df


# ---------------------------------------------------------
# Validation summary
# ---------------------------------------------------------

def print_validation_summary(
    df: pd.DataFrame,
) -> None:

    print()
    print("Validation summary:")
    print()

    counts = (
        df["validation_status"]
        .value_counts()
    )

    for status, count in counts.items():

        print(
            f"{status}: {count}"
        )


# ---------------------------------------------------------
# Αποθήκευση καθαρού Excel για manual review
# ---------------------------------------------------------

def save_review_excel(
    df: pd.DataFrame,
) -> None:

    review_columns = [
        "expense_title",
        "expense_description",
        "expense_class",
        "static_valid",
        "static_reason",
        "semantic_valid",
        "semantic_confidence",
        "semantic_reason",
        "validation_status",
    ]

    review_df = df[
        review_columns
    ].copy()

    review_df.to_excel(
        VALIDATED_TEST_EXCEL_PATH,
        index=False,
    )

    # Μικρή μορφοποίηση για πιο καθαρό Excel

    workbook = load_workbook(
        VALIDATED_TEST_EXCEL_PATH
    )

    worksheet = workbook.active

    if worksheet is None:
        raise ValueError(
        "No active worksheet found in Excel file."
    )


    worksheet.freeze_panes = "A2"

    column_widths = {
        "A": 35,
        "B": 55,
        "C": 55,
        "D": 15,
        "E": 25,
        "F": 18,
        "G": 22,
        "H": 70,
        "I": 20,
    }

    for column, width in column_widths.items():
        worksheet.column_dimensions[
            column
        ].width = width

    workbook.save(
        VALIDATED_TEST_EXCEL_PATH
    )


# ---------------------------------------------------------
# Test run
# ---------------------------------------------------------

def main() -> None:

    synthetic_df = pd.read_csv(
        TEST_OUTPUT_PATH,
        encoding="utf-8-sig",
    )

    real_df = pd.read_csv(
        CLEAN_CLIENT_DATA_PATH,
        encoding="utf-8-sig",
    )

    validated_df = (
        validate_synthetic_dataset(
            synthetic_df=synthetic_df,
            real_df=real_df,
        )
    )

    VALIDATED_TEST_OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Πλήρες technical output

    validated_df.to_csv(
        VALIDATED_TEST_OUTPUT_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    # Καθαρό Excel για να το διαβάζουμε εύκολα

    save_review_excel(
        validated_df
    )

    print_validation_summary(
        validated_df
    )

    print()

    print(
        f"Saved CSV: "
        f"{VALIDATED_TEST_OUTPUT_PATH}"
    )

    print(
        f"Saved Excel: "
        f"{VALIDATED_TEST_EXCEL_PATH}"
    )


if __name__ == "__main__":
    main()