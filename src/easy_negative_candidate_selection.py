import json

import sys

import time

import unicodedata

from pathlib import Path
from typing import Any



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

# Reuse existing LLM utilities

# ============================================================



from src.generator import (

    create_llm,

    clean_json_response,

    read_text_file,

)





# ============================================================

# Paths

# ============================================================



DISTANCE_ANALYSIS_PATH = (

    BASE_DIR

    / "data/output/embedding_distance_analysis.csv"

)



COVERAGE_ANALYSIS_PATH = (

    BASE_DIR

    / "data/output/kmin_coverage_analysis.xlsx"

)



NEGATIVE_VALIDATOR_SYSTEM_PROMPT_PATH = (

    BASE_DIR

    / "prompts/negative_pair_validator_system_prompt.md"

)



NEGATIVE_VALIDATOR_PROMPT_PATH = (

    BASE_DIR

    / "prompts/negative_pair_validator_prompt.md"

)





# ------------------------------------------------------------

# Candidate output

# ------------------------------------------------------------



CANDIDATES_CSV_PATH = (

    BASE_DIR

    / "data/output/easy_negative_candidates.csv"

)





# ------------------------------------------------------------

# Final validated negatives

# ------------------------------------------------------------



VALIDATED_NEGATIVES_CSV_PATH = (

    BASE_DIR

    / "data/output/easy_negative_pairs_validated.csv"

)



VALIDATED_NEGATIVES_EXCEL_PATH = (

    BASE_DIR

    / "data/output/easy_negative_pairs_validated.xlsx"

)





# ------------------------------------------------------------

# Technical/debug workbook

# ------------------------------------------------------------



TECHNICAL_REPORT_PATH = (

    BASE_DIR

    / "data/output/easy_negative_pipeline_report.xlsx"

)





# ------------------------------------------------------------

# Checkpoints

# ------------------------------------------------------------



CHECKPOINT_NEGATIVES_PATH = (

    BASE_DIR

    / "data/output/checkpoint_easy_negatives.csv"

)



CHECKPOINT_REPORT_PATH = (

    BASE_DIR

    / "data/output/checkpoint_easy_negative_report.csv"

)



CHECKPOINT_CLIENTS_PATH = (

    BASE_DIR

    / "data/output/checkpoint_completed_clients.csv"

)





# ============================================================

# FINAL FULL RUN SETTINGS

#

# These settings process ALL clients:

#

# - outside_k95 where available

# - fallback_farthest_first otherwise

# ============================================================



VALIDATION_CLIENT_LIMIT = None



VALIDATION_STRATEGY_FILTER = None



VALIDATION_ONE_CLIENT_PER_CLASS = False





# ============================================================

# Retry settings

# ============================================================



MAX_LLM_RETRIES = 3



RETRY_DELAY_SECONDS = 5





# ============================================================

# Checkpoint / resume setting

#

# FIRST full run:

#     RESUME_FROM_CHECKPOINT = False

#

# If the run stops midway because of an error:

# change it to True and run the same script again.

# ============================================================



RESUME_FROM_CHECKPOINT = False





# ============================================================

# LLM response model

# ============================================================



class PairValidationResult(BaseModel):



    verdict: str = Field(

        min_length=1

    )



    reason: str = Field(

        min_length=1

    )





# ============================================================

# Helpers

# ============================================================



def clean_value(

    value,

) -> str:



    if pd.isna(value):

        return ""



    return str(value).strip()





def to_int(value: Any) -> int:
    """
    Convert pandas / NumPy scalar-like values to a Python int.

    The explicit Any annotation is intentional: pandas row/groupby
    access is typed broadly as Scalar/Hashable by static type checkers,
    even when these columns are known to contain integer-compatible values.
    """

    if pd.isna(value):
        raise ValueError("Cannot convert a missing value to int.")

    return int(value)


def normalize_class(

    value,

) -> str:



    return (

        clean_value(value)

        .casefold()

    )



def program_text_key(
    category,
    subcategory,
    description,
) -> str:
    """
    Comparable key of the Program Expense text the validator sees:
    Category + Subcategory + Expense Description.

    Several program rows (different programs/subprograms) carry
    exactly the same text. For the validator they are the same input.
    """

    parts = [
        " ".join(
            unicodedata.normalize("NFC", clean_value(value))
            .casefold()
            .split()
        )
        for value in [category, subcategory, description]
    ]

    return "\x1f".join(parts)





def to_bool(

    value,

) -> bool:



    if isinstance(

        value,

        bool,

    ):

        return value



    return (

        str(value)

        .strip()

        .casefold()

        == "true"

    )





# ============================================================

# Non-fundable marker handling
#
# IMPORTANT:
# "Μη επιλέξιμες δαπάνες" is NOT treated as an expense class.
# If the exact value appears in any candidate field, the pair is
# marked as non_fundable and skipped from negative labeling.
# It is never sent to the LLM and never appears in the final
# easy_negative_pairs_validated output.
# ============================================================



# Raw marker values as they appear in the data.
#
# They are normalized with normalize_marker_value() below, so input
# values and markers always go through exactly the same function.
# (A hand-normalized literal breaks on casefold(), which maps the
# Greek final sigma "ς" to "σ".)

NON_FUNDABLE_MARKER_VARIANTS = (

    "Μη επιλέξιμες δαπάνες",

    "Μη επιλεξιμες δαπανες",

    "Μη επιλέξιμη δαπάνη",

    "Μη επιλεξιμη δαπανη",

)



def normalize_marker_value(

    value,

) -> str:



    text = clean_value(value)



    if not text:

        return ""



    text = unicodedata.normalize(

        "NFD",

        text,

    )



    text = "".join(

        character

        for character in text

        if unicodedata.category(character)

        != "Mn"

    )



    return " ".join(

        text

        .casefold()

        .split()

    )



NON_FUNDABLE_MARKERS = frozenset(

    normalize_marker_value(variant)

    for variant in NON_FUNDABLE_MARKER_VARIANTS

)



def detect_non_fundable_pair(

    row: pd.Series,

) -> tuple[bool, list[str]]:



    matched_fields = []



    for column, value in row.items():



        if (

            normalize_marker_value(value)

            in NON_FUNDABLE_MARKERS

        ):



            matched_fields.append(

                str(column)

            )



    return (

        bool(matched_fields),

        matched_fields,

    )



def assert_non_fundable_detection() -> None:

    """
    Sanity checks for the non-fundable marker detection.

    Raises AssertionError if the exact Greek values are not detected,
    or if a longer narrative text that merely contains the phrase is
    detected.
    """

    must_match = [

        *NON_FUNDABLE_MARKER_VARIANTS,

        "  ΜΗ ΕΠΙΛΕΞΙΜΕΣ   ΔΑΠΑΝΕΣ ",

        "μη επιλέξιμες δαπάνες",

    ]

    for value in must_match:

        assert (
            normalize_marker_value(value)
            in NON_FUNDABLE_MARKERS
        ), f"Non-fundable marker not detected: {value!r}"

    must_not_match = [

        "",

        None,

        "Υπηρεσίες τρίτων",

        "Μη επιλέξιμες δαπάνες για την αγορά οχημάτων",

        "Ο ΦΠΑ θεωρείται μη επιλέξιμη δαπάνη όταν ανακτάται.",

    ]

    for value in must_not_match:

        assert (
            normalize_marker_value(value)
            not in NON_FUNDABLE_MARKERS
        ), f"Non-fundable marker falsely detected: {value!r}"

    detected, fields = detect_non_fundable_pair(

        pd.Series(
            {
                "expense_class": "Μη επιλέξιμες δαπάνες",
                "program_category": "Υπηρεσίες τρίτων",
                "distance_rank": 4,
            }
        )

    )

    assert detected and fields == ["expense_class"], (
        f"Unexpected pair detection result: {detected}, {fields}"
    )





# ============================================================

# Static validation

# ============================================================



def static_validate_pair(

    row: pd.Series,

) -> tuple[str, str]:



    client_title = clean_value(

        row[

            "client_expense_title"

        ]

    )



    client_description = clean_value(

        row[

            "client_expense_description"

        ]

    )



    client_class = normalize_class(

        row[

            "client_expense_class"

        ]

    )



    program_class = normalize_class(

        row[

            "program_expense_class"

        ]

    )



    program_category = clean_value(

        row[

            "program_category"

        ]

    )



    program_subcategory = clean_value(

        row[

            "program_subcategory"

        ]

    )



    # --------------------------------------------------------

    # Required client information

    # --------------------------------------------------------



    if not client_title:



        return (

            "UNCERTAIN",

            "Missing client expense title.",

        )



    if not client_description:



        return (

            "UNCERTAIN",

            "Missing client expense description.",

        )



    if not client_class:



        return (

            "UNCERTAIN",

            "Missing client expense class.",

        )



    if not program_class:



        return (

            "UNCERTAIN",

            "Missing program expense class.",

        )



    # --------------------------------------------------------

    # Same-class is a hard constraint.

    #

    # This should never fail because candidate generation

    # already works inside the same expense_class.

    # --------------------------------------------------------



    if (

        client_class

        != program_class

    ):



        raise ValueError(

            "Candidate pair has different "

            "client/program expense classes."

        )



    # --------------------------------------------------------

    # Need semantic information

    # --------------------------------------------------------



    if (

        not program_category

        and

        not program_subcategory

    ):



        return (

            "UNCERTAIN",

            "Both program category and "

            "subcategory are missing.",

        )



    return (

        "PASS",

        "Static validation passed.",

    )





# ============================================================

# Build negative validation prompt

# ============================================================



def build_validation_prompt(

    row: pd.Series,

) -> tuple[str, str]:



    system_prompt = (

        read_text_file(

            NEGATIVE_VALIDATOR_SYSTEM_PROMPT_PATH

        )

    )



    validation_template = (

        read_text_file(

            NEGATIVE_VALIDATOR_PROMPT_PATH

        )

    )



    validation_prompt = (

        validation_template



        .replace(

            "{client_expense_title}",

            clean_value(

                row[

                    "client_expense_title"

                ]

            ),

        )



        .replace(

            "{client_expense_description}",

            clean_value(

                row[

                    "client_expense_description"

                ]

            ),

        )



        .replace(

            "{client_expense_class}",

            clean_value(

                row[

                    "client_expense_class"

                ]

            ),

        )



        .replace(

            "{program_category}",

            clean_value(

                row[

                    "program_category"

                ]

            ),

        )



        .replace(

            "{program_subcategory}",

            clean_value(

                row[

                    "program_subcategory"

                ]

            ),

        )



        .replace(

            "{program_expense_description}",

            clean_value(

                row[

                    "program_expense_description"

                ]

            ),

        )



        .replace(

            "{program_expense_class}",

            clean_value(

                row[

                    "program_expense_class"

                ]

            ),

        )

    )



    return (

        system_prompt,

        validation_prompt,

    )





# ============================================================

# Single LLM validation call

# ============================================================



def validate_pair_with_llm(

    llm,

    row: pd.Series,

) -> PairValidationResult:



    (

        system_prompt,

        validation_prompt,

    ) = build_validation_prompt(

        row

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



    response_text = (

        clean_json_response(

            str(

                response.content

            )

        )

    )



    data = json.loads(

        response_text

    )



    result = (

        PairValidationResult

        .model_validate(

            data

        )

    )



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



    if (

        result.verdict

        not in allowed_verdicts

    ):



        raise ValueError(

            "Unexpected validator verdict: "

            f"{result.verdict}"

        )



    return result





# ============================================================

# LLM validation with retry

# ============================================================



def validate_pair_with_retry(

    llm,

    row: pd.Series,

) -> PairValidationResult:



    last_error = None



    for attempt in range(

        1,

        MAX_LLM_RETRIES + 1,

    ):



        try:



            return (

                validate_pair_with_llm(

                    llm=llm,

                    row=row,

                )

            )



        except Exception as exc:



            last_error = exc



            print(

                f"LLM error "

                f"(attempt "

                f"{attempt}/"

                f"{MAX_LLM_RETRIES}): "

                f"{exc}"

            )



            if (

                attempt

                < MAX_LLM_RETRIES

            ):



                print(

                    f"Retrying in "

                    f"{RETRY_DELAY_SECONDS} "

                    f"seconds..."

                )



                time.sleep(

                    RETRY_DELAY_SECONDS

                )



    # --------------------------------------------------------

    # Important:

    #

    # A technical error must NOT become UNCERTAIN.

    #

    # Stop the run so that it can later resume safely.

    # --------------------------------------------------------



    raise RuntimeError(

        "LLM validation failed after "

        f"{MAX_LLM_RETRIES} attempts. "

        f"Last error: {last_error}"

    )





# ============================================================

# Candidate selection

# ============================================================



def build_candidate_pool(

    pairs_df: pd.DataFrame,

    coverage_df: pd.DataFrame,

) -> tuple[

    pd.DataFrame,

    pd.DataFrame,

    pd.DataFrame,

]:



    pairs_df = (

        pairs_df.copy()

    )



    coverage_df = (

        coverage_df.copy()

    )



    # --------------------------------------------------------

    # Normalize expense classes

    # --------------------------------------------------------



    pairs_df[

        "_class_key"

    ] = (

        pairs_df[

            "expense_class"

        ]

        .apply(

            normalize_class

        )

    )



    coverage_df[

        "_class_key"

    ] = (

        coverage_df[

            "expense_class"

        ]

        .apply(

            normalize_class

        )

    )



    pairs_df[

        "is_known_positive"

    ] = (

        pairs_df[

            "is_known_positive"

        ]

        .apply(

            to_bool

        )

    )



    # --------------------------------------------------------

    # Class -> K95

    # --------------------------------------------------------



    k95_by_class = {}



    for _, row in (

        coverage_df

        .iterrows()

    ):



        if pd.isna(

            row[

                "k_95"

            ]

        ):

            continue



        k95_by_class[

            row[

                "_class_key"

            ]

        ] = to_int(

            row[

                "k_95"

            ]

        )



    # --------------------------------------------------------

    # Build candidates

    # --------------------------------------------------------



    candidate_rows = []



    client_diagnostic_rows = []



    for (

        analysis_client_id,

        client_df,

    ) in pairs_df.groupby(

        "analysis_client_id"

    ):



        analysis_client_id = to_int(

            analysis_client_id

        )



        client_df = (

            client_df

            .copy()

            .sort_values(

                "distance_rank"

            )

        )



        class_key = (

            client_df[

                "_class_key"

            ]

            .iloc[0]

        )



        expense_class = clean_value(

            client_df[

                "expense_class"

            ]

            .iloc[0]

        )



        if (

            class_key

            not in k95_by_class

        ):



            raise ValueError(

                "No K95 found for "

                "expense class: "

                f"{expense_class}"

            )



        k95 = (

            k95_by_class[

                class_key

            ]

        )



        same_class_program_count = (

            len(

                client_df

            )

        )



        # ----------------------------------------------------

        # Remove known positive anchor

        # ----------------------------------------------------



        possible_candidates = (

            client_df[

                ~client_df[

                    "is_known_positive"

                ]

            ]

            .copy()

        )



        # ----------------------------------------------------
        # Skip candidates with the same text as the known positive
        #
        # Same Category + Subcategory + Description means the
        # validator sees exactly the input of the known positive
        # pair, which is already VALID. Such a candidate can never
        # become a negative, so it is not sent to the LLM.
        #
        # These rows are NOT labelled as positives: another
        # program/subprogram may have different terms.
        # ----------------------------------------------------

        known_positive_rows = client_df[
            client_df[
                "is_known_positive"
            ]
        ]

        positive_text_duplicate_count = 0

        if not known_positive_rows.empty:

            known_positive_row = known_positive_rows.iloc[0]

            positive_text = program_text_key(
                known_positive_row["program_category"],
                known_positive_row["program_subcategory"],
                known_positive_row["program_expense_description"],
            )

            is_positive_text_duplicate = pd.Series(
                [
                    program_text_key(
                        row["program_category"],
                        row["program_subcategory"],
                        row["program_expense_description"],
                    )
                    == positive_text
                    for _, row in possible_candidates.iterrows()
                ],
                index=possible_candidates.index,
                dtype=bool,
            )

            positive_text_duplicate_count = int(
                is_positive_text_duplicate.sum()
            )

            possible_candidates = possible_candidates[
                ~is_positive_text_duplicate
            ].copy()



        # ----------------------------------------------------

        # Primary strategy:

        #

        # rank > K95

        #

        # Start from nearest candidate just outside K95.

        # ----------------------------------------------------



        outside_candidates = (

            possible_candidates[

                possible_candidates[

                    "distance_rank"

                ]

                > k95

            ]

            .sort_values(

                "distance_rank",

                ascending=True,

            )

        )



        outside_candidate_count = (

            len(

                outside_candidates

            )

        )



        # ----------------------------------------------------

        # Dynamic strategy selection

        # ----------------------------------------------------



        if (

            outside_candidate_count

            > 0

        ):



            selected_candidates = (

                outside_candidates

                .copy()

            )



            selection_strategy = (

                "outside_k95"

            )



        else:



            # ------------------------------------------------

            # Fallback:

            #

            # There is no candidate outside K95.

            #

            # Start from the farthest available

            # non-positive Program Expense.

            # ------------------------------------------------



            selected_candidates = (

                possible_candidates

                .sort_values(

                    "distance_rank",

                    ascending=False,

                )

                .copy()

            )



            selection_strategy = (

                "fallback_farthest_first"

            )



        # ----------------------------------------------------

        # Client diagnostics

        # ----------------------------------------------------



        client_diagnostic_rows.append(

            {

                "analysis_client_id":

                    analysis_client_id,



                "expense_class":

                    expense_class,



                "same_class_program_count":

                    same_class_program_count,



                "k95":

                    k95,



                "outside_candidate_count":

                    outside_candidate_count,



                "positive_text_duplicates_skipped":

                    positive_text_duplicate_count,



                "total_available_candidates":

                    len(

                        possible_candidates

                    ),



                "selection_strategy":

                    selection_strategy,

            }

        )



        # ----------------------------------------------------

        # Ordered validation candidates

        # ----------------------------------------------------



        for (

            candidate_order,

            (_, row),

        ) in enumerate(

            selected_candidates

            .iterrows(),

            start=1,

        ):



            pair_class = clean_value(

                row[

                    "expense_class"

                ]

            )



            candidate_rows.append(

                {

                    "analysis_client_id":

                        analysis_client_id,



                    "positive_program_expense_id":

                        to_int(

                            row[

                                "positive_program_expense_id"

                            ]

                        ),



                    "candidate_program_expense_id":

                        to_int(

                            row[

                                "candidate_program_expense_id"

                            ]

                        ),



                    "client_expense_title":

                        clean_value(

                            row[

                                "client_expense_title"

                            ]

                        ),



                    "client_expense_description":

                        clean_value(

                            row[

                                "client_expense_description"

                            ]

                        ),



                    "client_expense_class":

                        pair_class,



                    "program_category":

                        clean_value(

                            row[

                                "program_category"

                            ]

                        ),



                    "program_subcategory":

                        clean_value(

                            row[

                                "program_subcategory"

                            ]

                        ),



                    "program_expense_description":

                        clean_value(

                            row[

                                "program_expense_description"

                            ]

                        ),



                    "program_expense_class":

                        pair_class,



                    "cosine_similarity":

                        float(

                            row[

                                "cosine_similarity"

                            ]

                        ),



                    "cosine_distance":

                        float(

                            row[

                                "cosine_distance"

                            ]

                        ),



                    "distance_rank":

                        to_int(

                            row[

                                "distance_rank"

                            ]

                        ),



                    "k95":

                        k95,



                    "selection_strategy":

                        selection_strategy,



                    "candidate_order":

                        candidate_order,

                }

            )



    candidates_df = (

        pd.DataFrame(

            candidate_rows

        )

    )



    client_diagnostics_df = (

        pd.DataFrame(

            client_diagnostic_rows

        )

    )



    # --------------------------------------------------------

    # Class diagnostics

    # --------------------------------------------------------



    class_diagnostics_df = (

        client_diagnostics_df

        .groupby(

            [

                "expense_class",

                "same_class_program_count",

                "k95",

            ],

            as_index=False,

        )

        .agg(

            clients=(

                "analysis_client_id",

                "nunique",

            ),



            outside_k95_clients=(

                "selection_strategy",

                lambda values: (

                    values

                    == "outside_k95"

                ).sum(),

            ),



            fallback_clients=(

                "selection_strategy",

                lambda values: (

                    values

                    == "fallback_farthest_first"

                ).sum(),

            ),



            positive_text_duplicates_skipped=(

                "positive_text_duplicates_skipped",

                "sum",

            ),



            total_available_candidates=(

                "total_available_candidates",

                "sum",

            ),

        )

    )



    class_diagnostics_df[

        "fallback_percentage"

    ] = (

        class_diagnostics_df[

            "fallback_clients"

        ]

        /

        class_diagnostics_df[

            "clients"

        ]

        * 100

    )



    return (

        candidates_df,

        client_diagnostics_df,

        class_diagnostics_df,

    )





# ============================================================

# Checkpoint helpers

# ============================================================



def clear_old_checkpoints() -> None:



    checkpoint_paths = [

        CHECKPOINT_NEGATIVES_PATH,

        CHECKPOINT_REPORT_PATH,

        CHECKPOINT_CLIENTS_PATH,

    ]



    for path in checkpoint_paths:



        if path.exists():



            path.unlink()





def save_checkpoint(

    validated_negative_rows: list[dict],

    validation_report_rows: list[dict],

    completed_client_ids: set[int],

) -> None:



    # --------------------------------------------------------

    # Negatives found so far

    # --------------------------------------------------------



    pd.DataFrame(

        validated_negative_rows

    ).to_csv(

        CHECKPOINT_NEGATIVES_PATH,

        index=False,

        encoding="utf-8-sig",

    )



    # --------------------------------------------------------

    # Validation report so far

    # --------------------------------------------------------



    pd.DataFrame(

        validation_report_rows

    ).to_csv(

        CHECKPOINT_REPORT_PATH,

        index=False,

        encoding="utf-8-sig",

    )



    # --------------------------------------------------------

    # Clients fully processed

    # --------------------------------------------------------



    pd.DataFrame(

        {

            "analysis_client_id":

                sorted(

                    completed_client_ids

                )

        }

    ).to_csv(

        CHECKPOINT_CLIENTS_PATH,

        index=False,

        encoding="utf-8-sig",

    )





def load_checkpoint() -> tuple[

    list[dict],

    list[dict],

    set[int],

]:



    validated_negative_rows = []



    validation_report_rows = []



    completed_client_ids = set()



    if (

        CHECKPOINT_NEGATIVES_PATH

        .exists()

    ):



        checkpoint_df = (

            pd.read_csv(

                CHECKPOINT_NEGATIVES_PATH,

                encoding="utf-8-sig",

            )

        )



        validated_negative_rows = (

            checkpoint_df

            .to_dict(

                orient="records"

            )

        )



    if (

        CHECKPOINT_REPORT_PATH

        .exists()

    ):



        checkpoint_df = (

            pd.read_csv(

                CHECKPOINT_REPORT_PATH,

                encoding="utf-8-sig",

            )

        )



        validation_report_rows = (

            checkpoint_df

            .to_dict(

                orient="records"

            )

        )



    if (

        CHECKPOINT_CLIENTS_PATH

        .exists()

    ):



        checkpoint_df = (

            pd.read_csv(

                CHECKPOINT_CLIENTS_PATH,

                encoding="utf-8-sig",

            )

        )



        completed_client_ids = set(

            checkpoint_df[

                "analysis_client_id"

            ]

            .astype(int)

            .tolist()

        )



    return (

        validated_negative_rows,

        validation_report_rows,

        completed_client_ids,

    )





# ============================================================

# Sequential negative validation

# ============================================================



def validate_candidates(

    candidates_df: pd.DataFrame,

) -> tuple[

    pd.DataFrame,

    pd.DataFrame,

    int,

    int,

]:



    # --------------------------------------------------------

    # Strategy filtering

    #

    # FINAL RUN:

    # VALIDATION_STRATEGY_FILTER = None

    #

    # So BOTH strategies are processed.

    # --------------------------------------------------------



    if (

        VALIDATION_STRATEGY_FILTER

        is not None

    ):



        allowed_strategies = {

            "outside_k95",

            "fallback_farthest_first",

        }



        if (

            VALIDATION_STRATEGY_FILTER

            not in allowed_strategies

        ):



            raise ValueError(

                "Unknown validation strategy filter: "

                f"{VALIDATION_STRATEGY_FILTER}"

            )



        validation_candidates_df = (

            candidates_df[

                candidates_df[

                    "selection_strategy"

                ]

                == VALIDATION_STRATEGY_FILTER

            ]

            .copy()

        )



    else:



        validation_candidates_df = (

            candidates_df.copy()

        )



    if (

        validation_candidates_df

        .empty

    ):



        raise ValueError(

            "No candidates available "

            "for validation."

        )



    # --------------------------------------------------------

    # Client selection

    #

    # FINAL RUN:

    # one-client-per-class = False

    # therefore ALL clients are selected.

    # --------------------------------------------------------



    if (

        VALIDATION_ONE_CLIENT_PER_CLASS

    ):



        client_ids = (

            validation_candidates_df

            .sort_values(

                [

                    "client_expense_class",

                    "analysis_client_id",

                ]

            )

            .groupby(

                "client_expense_class"

            )[

                "analysis_client_id"

            ]

            .first()

            .astype(int)

            .tolist()

        )



    else:



        client_ids = (

            validation_candidates_df[

                "analysis_client_id"

            ]

            .drop_duplicates()

            .astype(int)

            .tolist()

        )



    # --------------------------------------------------------

    # Optional limit

    #

    # FINAL RUN:

    # None -> no limit.

    # --------------------------------------------------------



    if (

        VALIDATION_CLIENT_LIMIT

        is not None

    ):



        client_ids = (

            client_ids[

                :VALIDATION_CLIENT_LIMIT

            ]

        )



    total_clients = len(

        client_ids

    )



    if total_clients == 0:



        raise ValueError(

            "No clients selected "

            "for validation."

        )



    # --------------------------------------------------------

    # Checkpoint initialization

    # --------------------------------------------------------



    if RESUME_FROM_CHECKPOINT:



        (

            validated_negative_rows,

            validation_report_rows,

            completed_client_ids,

        ) = load_checkpoint()



        print()

        print(

            "RESUMING FROM CHECKPOINT"

        )



        print(

            "Previously completed clients:",

            len(

                completed_client_ids

            ),

        )



    else:



        clear_old_checkpoints()



        validated_negative_rows = []



        validation_report_rows = []



        completed_client_ids = set()



    # --------------------------------------------------------

    # LLM

    # --------------------------------------------------------



    llm = create_llm(

        temperature=0.0

    )



    llm_calls = 0



    # --------------------------------------------------------

    # Process every client

    # --------------------------------------------------------



    for (

        client_counter,

        analysis_client_id,

    ) in enumerate(

        client_ids,

        start=1,

    ):



        analysis_client_id = to_int(

            analysis_client_id

        )



        # ----------------------------------------------------

        # Skip already completed clients when resuming

        # ----------------------------------------------------



        if (

            analysis_client_id

            in completed_client_ids

        ):



            print(

                f"Client "

                f"{client_counter}/"

                f"{total_clients} "

                f"(ID "

                f"{analysis_client_id}) "

                f"already completed "

                f"-> SKIP"

            )



            continue



        client_candidates = (

            validation_candidates_df[

                validation_candidates_df[

                    "analysis_client_id"

                ]

                == analysis_client_id

            ]

            .sort_values(

                "candidate_order"

            )

        )



        print()

        print("=" * 70)



        print(

            f"Client "

            f"{client_counter}/"

            f"{total_clients}"

        )



        print(

            "analysis_client_id:",

            analysis_client_id,

        )



        print(

            "expense_class:",

            client_candidates[

                "client_expense_class"

            ]

            .iloc[0],

        )



        print(

            "strategy:",

            client_candidates[

                "selection_strategy"

            ]

            .iloc[0],

        )



        print(

            "candidates:",

            len(

                client_candidates

            ),

        )



        negative_found = False



        # ----------------------------------------------------

        # Sequential candidate validation

        # ----------------------------------------------------



        for _, row in (

            client_candidates

            .iterrows()

        ):



            print()



            print(

                "Candidate order:",

                to_int(

                    row[

                        "candidate_order"

                    ]

                ),

            )



            print(

                "Distance rank:",

                to_int(

                    row[

                        "distance_rank"

                    ]

                ),

            )



            print(

                "Program ID:",

                to_int(

                    row[

                        "candidate_program_expense_id"

                    ]

                ),

            )



            # ------------------------------------------------

            # Non-fundable marker check
            #
            # Exact marker only: this does NOT trigger on a
            # longer description that merely contains the phrase.
            #
            # non_fundable=True means: skip this candidate from
            # negative labeling. No LLM call, no label=0, and
            # continue with the next candidate for this client.
            # ------------------------------------------------



            (

                non_fundable,

                non_fundable_fields,

            ) = detect_non_fundable_pair(

                row

            )



            if non_fundable:



                used_llm = False



                verdict = (

                    "SKIPPED_NON_FUNDABLE"

                )



                validation_reason = (

                    "Exact non-fundable marker found in "

                    f"field(s): {', '.join(non_fundable_fields)}. "

                    "Candidate skipped; no negative label assigned."

                )



                print(

                    "non_fundable: True"

                )



                print(

                    "Non-fundable fields:",

                    ", ".join(

                        non_fundable_fields

                    ),

                )



                print(

                    "Verdict:",

                    verdict,

                )



                print(

                    "Reason:",

                    validation_reason,

                )



                report_row = (

                    row.to_dict()

                )



                report_row[

                    "non_fundable"

                ] = True



                report_row[

                    "non_fundable_source_fields"

                ] = ", ".join(

                    non_fundable_fields

                )



                report_row[

                    "used_llm"

                ] = False



                report_row[

                    "validation_verdict"

                ] = verdict



                report_row[

                    "validation_reason"

                ] = validation_reason



                validation_report_rows.append(

                    report_row

                )



                continue



            # ------------------------------------------------

            # Static validation

            # ------------------------------------------------



            (

                static_status,

                static_reason,

            ) = static_validate_pair(

                row

            )



            if (

                static_status

                == "UNCERTAIN"

            ):



                verdict = (

                    "UNCERTAIN"

                )



                validation_reason = (

                    static_reason

                )



                used_llm = False



            else:



                # --------------------------------------------

                # LLM validation WITH retry

                # --------------------------------------------



                result = (

                    validate_pair_with_retry(

                        llm=llm,

                        row=row,

                    )

                )



                llm_calls += 1



                used_llm = True



                verdict = (

                    result.verdict

                )



                validation_reason = (

                    result.reason

                )



            print(

                "Verdict:",

                verdict,

            )



            print(

                "Reason:",

                validation_reason,

            )



            # ------------------------------------------------

            # Technical report

            # ------------------------------------------------



            report_row = (

                row.to_dict()

            )



            report_row[

                "non_fundable"

            ] = False



            report_row[

                "non_fundable_source_fields"

            ] = ""



            report_row[

                "used_llm"

            ] = used_llm



            report_row[

                "validation_verdict"

            ] = verdict



            report_row[

                "validation_reason"

            ] = validation_reason



            validation_report_rows.append(

                report_row

            )



            # ------------------------------------------------

            # FIRST INVALID

            #

            # Confirmed negative.

            # Save and STOP this client.

            # ------------------------------------------------



            if (

                verdict

                == "INVALID"

            ):



                negative_row = (

                    row.to_dict()

                )



                negative_row[

                    "label"

                ] = 0



                validated_negative_rows.append(

                    negative_row

                )



                negative_found = True



                print(

                    "Confirmed negative found "

                    "-> STOP client."

                )



                break



        # ----------------------------------------------------

        # No negative found

        # ----------------------------------------------------



        if not negative_found:



            print(

                "No confirmed negative "

                "found for this client."

            )



        # ----------------------------------------------------

        # Mark client as fully completed

        # ----------------------------------------------------



        completed_client_ids.add(

            analysis_client_id

        )



        # ----------------------------------------------------

        # Save checkpoint AFTER EVERY client

        # ----------------------------------------------------



        save_checkpoint(

            validated_negative_rows=(

                validated_negative_rows

            ),

            validation_report_rows=(

                validation_report_rows

            ),

            completed_client_ids=(

                completed_client_ids

            ),

        )



        print(

            "Checkpoint saved."

        )



    # ========================================================

    # Technical validation report

    # ========================================================



    validation_report_df = (

        pd.DataFrame(

            validation_report_rows

        )

    )



    # ========================================================

    # Final clean negative dataset

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



    if validated_negative_rows:



        validated_df = (

            pd.DataFrame(

                validated_negative_rows

            )

        )



        validated_df = (

            validated_df

            .rename(

                columns={

                    "candidate_program_expense_id":

                        "program_expense_id",

                }

            )

        )



        validated_df = (

            validated_df[

                final_columns

            ]

            .copy()

        )



    else:



        validated_df = (

            pd.DataFrame(

                columns=(

                    final_columns

                )

            )

        )



    # --------------------------------------------------------

    # Final safety guard:
    # the clean negative output must NEVER contain an exact
    # non-fundable marker in any exported column.
    # --------------------------------------------------------



    if not validated_df.empty:



        bad_final_rows = []



        for final_index, final_row in (

            validated_df.iterrows()

        ):



            (

                final_non_fundable,

                final_non_fundable_fields,

            ) = detect_non_fundable_pair(

                final_row

            )



            if final_non_fundable:



                bad_final_rows.append(

                    (

                        to_int(final_index),

                        final_non_fundable_fields,

                    )

                )



        if bad_final_rows:



            raise ValueError(

                "Final validated negative dataset still "

                "contains non-fundable marker values: "

                f"{bad_final_rows[:10]}"

            )





    processed_clients = (

        len(

            completed_client_ids

        )

    )



    return (

        validated_df,

        validation_report_df,

        llm_calls,

        processed_clients,

    )





# ============================================================

# Main

# ============================================================



def main() -> None:



    assert_non_fundable_detection()



    # --------------------------------------------------------

    # Load embedding distance analysis

    # --------------------------------------------------------



    pairs_df = pd.read_csv(

        DISTANCE_ANALYSIS_PATH,

        encoding="utf-8-sig",

    )



    # --------------------------------------------------------

    # Load K95 analysis

    # --------------------------------------------------------



    coverage_df = (

        pd.read_excel(

            COVERAGE_ANALYSIS_PATH,

            sheet_name="coverage_summary",

        )

    )



    # --------------------------------------------------------

    # Required pair columns

    # --------------------------------------------------------



    required_pair_columns = {

        "analysis_client_id",

        "positive_program_expense_id",

        "candidate_program_expense_id",

        "client_expense_title",

        "client_expense_description",

        "expense_class",

        "program_category",

        "program_subcategory",

        "program_expense_description",

        "cosine_similarity",

        "cosine_distance",

        "distance_rank",

        "is_known_positive",

    }



    missing_pair_columns = (

        required_pair_columns

        - set(

            pairs_df.columns

        )

    )



    if missing_pair_columns:



        raise ValueError(

            "Missing pair columns: "

            f"{sorted(missing_pair_columns)}"

        )



    # --------------------------------------------------------

    # Required coverage columns

    # --------------------------------------------------------



    required_coverage_columns = {

        "expense_class",

        "k_95",

    }



    missing_coverage_columns = (

        required_coverage_columns

        - set(

            coverage_df.columns

        )

    )



    if missing_coverage_columns:



        raise ValueError(

            "Missing coverage columns: "

            f"{sorted(missing_coverage_columns)}"

        )



    # ========================================================

    # Phase 1A:

    # Candidate selection

    # ========================================================



    (

        candidates_df,

        client_diagnostics_df,

        class_diagnostics_df,

    ) = build_candidate_pool(

        pairs_df=pairs_df,

        coverage_df=coverage_df,

    )



    # --------------------------------------------------------

    # Save candidate pool

    # --------------------------------------------------------



    CANDIDATES_CSV_PATH.parent.mkdir(

        parents=True,

        exist_ok=True,

    )



    candidates_df.to_csv(

        CANDIDATES_CSV_PATH,

        index=False,

        encoding="utf-8-sig",

    )



    # --------------------------------------------------------

    # Candidate summary

    # --------------------------------------------------------



    outside_clients = int(

        (

            client_diagnostics_df[

                "selection_strategy"

            ]

            == "outside_k95"

        )

        .sum()

    )



    fallback_clients = int(

        (

            client_diagnostics_df[

                "selection_strategy"

            ]

            == "fallback_farthest_first"

        )

        .sum()

    )



    print()

    print("=" * 70)

    print(

        "EASY NEGATIVE CANDIDATE SELECTION"

    )

    print("=" * 70)



    print(

        "Clients:",

        len(

            client_diagnostics_df

        ),

    )



    print(

        "Clients with outside-K95 region:",

        outside_clients,

    )



    print(

        "Clients using fallback:",

        fallback_clients,

    )



    print(

        "Candidate pairs:",

        len(

            candidates_df

        ),

    )



    print(

        "Validation strategy filter:",

        (

            VALIDATION_STRATEGY_FILTER

            if

            VALIDATION_STRATEGY_FILTER

            is not None

            else "ALL"

        ),

    )



    print(

        "One client per class:",

        VALIDATION_ONE_CLIENT_PER_CLASS,

    )



    print(

        "Client limit:",

        (

            VALIDATION_CLIENT_LIMIT

            if

            VALIDATION_CLIENT_LIMIT

            is not None

            else "ALL"

        ),

    )



    print(

        "Resume from checkpoint:",

        RESUME_FROM_CHECKPOINT,

    )



    # ========================================================

    # Phase 1B:

    # Sequential LLM validation

    # ========================================================



    (

        validated_negatives_df,

        validation_report_df,

        llm_calls,

        processed_clients,

    ) = validate_candidates(

        candidates_df

    )



    # ========================================================

    # Save final clean validated negatives

    # ========================================================



    validated_negatives_df.to_csv(

        VALIDATED_NEGATIVES_CSV_PATH,

        index=False,

        encoding="utf-8-sig",

    )



    validated_negatives_df.to_excel(

        VALIDATED_NEGATIVES_EXCEL_PATH,

        index=False,

        engine="openpyxl",

    )



    # ========================================================

    # Technical report

    # ========================================================



    with pd.ExcelWriter(

        TECHNICAL_REPORT_PATH,

        engine="openpyxl",

    ) as writer:



        candidates_df.to_excel(

            writer,

            sheet_name="candidate_pool",

            index=False,

        )



        client_diagnostics_df.to_excel(

            writer,

            sheet_name="client_diagnostics",

            index=False,

        )



        class_diagnostics_df.to_excel(

            writer,

            sheet_name="class_diagnostics",

            index=False,

        )



        validation_report_df.to_excel(

            writer,

            sheet_name="validation_report",

            index=False,

        )



        if (

            not validation_report_df.empty

            and

            "non_fundable"

            in validation_report_df.columns

        ):



            non_fundable_skipped_df = (

                validation_report_df[

                    validation_report_df[

                        "non_fundable"

                    ]

                    .apply(to_bool)

                ]

                .copy()

            )



        else:



            non_fundable_skipped_df = (

                pd.DataFrame()

            )





        non_fundable_skipped_df.to_excel(

            writer,

            sheet_name="non_fundable_skipped",

            index=False,

        )





        validated_negatives_df.to_excel(

            writer,

            sheet_name="validated_negatives",

            index=False,

        )



    # --------------------------------------------------------

    # Count skipped non-fundable candidates

    # --------------------------------------------------------



    if (

        not validation_report_df.empty

        and

        "non_fundable"

        in validation_report_df.columns

    ):



        non_fundable_skips = int(

            validation_report_df[

                "non_fundable"

            ]

            .apply(to_bool)

            .sum()

        )



    else:



        non_fundable_skips = 0





    # ========================================================

    # Final summary

    # ========================================================



    print()

    print("=" * 70)

    print(

        "EASY NEGATIVE VALIDATION COMPLETE"

    )

    print("=" * 70)



    print(

        "Clients processed:",

        processed_clients,

    )



    print(

        "LLM calls:",

        llm_calls,

    )



    print(

        "Non-fundable candidates skipped:",

        non_fundable_skips,

    )



    print(

        "Confirmed negatives:",

        len(

            validated_negatives_df

        ),

    )



    print()



    print(

        "Validated negative CSV:",

        VALIDATED_NEGATIVES_CSV_PATH,

    )



    print(

        "Validated negative Excel:",

        VALIDATED_NEGATIVES_EXCEL_PATH,

    )



    print(

        "Technical report:",

        TECHNICAL_REPORT_PATH,

    )



    print("=" * 70)





if __name__ == "__main__":

    main()