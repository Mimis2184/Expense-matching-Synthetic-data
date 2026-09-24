import json
import os
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from langchain_openai import AzureChatOpenAI
from pydantic import BaseModel, Field, SecretStr


sys.path.insert(
    0,
    str(Path(__file__).parent.parent),
)


# ---------------------------------------------------------
# Imports από config.py
# ---------------------------------------------------------

from config import (
    STYLE_PROFILE_PATH,
    SEEDS_PATH,
    SYSTEM_PROMPT_PATH,
    GENERATION_PROMPT_PATH,
    TEST_OUTPUT_PATH,
    TEST_N_TO_GENERATE,
    TEST_EXPENSE_CLASS,
)


# ---------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------

class SyntheticExpense(BaseModel):
    expense_title: str = Field(min_length=1)
    expense_description: str = Field(min_length=1)
    expense_class: str = Field(min_length=1)


class SyntheticExpenseBatch(BaseModel):
    expenses: list[SyntheticExpense]


load_dotenv(
    Path(__file__).resolve().parent.parent / ".env"
)


# ---------------------------------------------------------
# Helper function για text files
# ---------------------------------------------------------

def read_text_file(path: Path) -> str:
    with open(
        path,
        "r",
        encoding="utf-8",
    ) as file:
        return file.read()


# ---------------------------------------------------------
# Azure OpenAI client
# ---------------------------------------------------------

def create_llm(
        temperature : float = 0.8,
) -> AzureChatOpenAI:

    endpoint = os.getenv(
        "AZURE_OPENAI_ENDPOINT"
    )

    api_key = os.getenv(
        "OPENAI_API_KEY"
    )

    api_version = os.getenv(
        "OPENAI_API_VERSION"
    )

    deployment = os.getenv(
        "OPENAI_DEPLOYMENT_ID"
    )

    missing = []

    if not endpoint:
        missing.append(
            "AZURE_OPENAI_ENDPOINT"
        )

    if not api_key:
        missing.append(
            "OPENAI_API_KEY"
        )

    if not api_version:
        missing.append(
            "OPENAI_API_VERSION"
        )

    if not deployment:
        missing.append(
            "OPENAI_DEPLOYMENT_ID"
        )

    if missing:
        raise ValueError(
            "Missing environment variables: "
            + ", ".join(missing)
        )

    return AzureChatOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version=api_version,
        azure_deployment=deployment,
        temperature=temperature,
    )


# ---------------------------------------------------------
# Παίρνουμε style profile μιας class
# ---------------------------------------------------------

def get_style_profile(
    expense_class: str,
) -> dict:

    profile_df = pd.read_csv(
        STYLE_PROFILE_PATH,
        encoding="utf-8-sig",
    )

    row = profile_df[
        profile_df["expense_class"]
        == expense_class
    ]

    if row.empty:
        raise ValueError(
            f"No style profile found for: "
            f"{expense_class}"
        )

    return row.iloc[0].to_dict()


# ---------------------------------------------------------
# Παίρνουμε seeds μιας class
# ---------------------------------------------------------

def get_seeds(
    expense_class: str,
) -> list[dict]:

    seeds_df = pd.read_csv(
        SEEDS_PATH,
        encoding="utf-8-sig",
    )

    class_seeds = seeds_df[
        seeds_df["expense_class"]
        == expense_class
    ].copy()

    if class_seeds.empty:
        return []

    return (
        class_seeds[
            [
                "expense_title",
                "expense_description",
            ]
        ]
        .to_dict(
            orient="records"
        )
    )


# ---------------------------------------------------------
# Μετατροπή style profile σε text για το prompt
# ---------------------------------------------------------

def format_style_profile(
    profile: dict,
) -> str:

    return (
        f"Number of real examples: "
        f"{profile['n_examples']}\n"

        f"Title equals description: "
        f"{profile['pct_title_equals_description']}%\n"

        f"Greek titles: "
        f"{profile['pct_greek']}%\n"

        f"English titles: "
        f"{profile['pct_english']}%\n"

        f"Mixed Greek-English titles: "
        f"{profile['pct_mixed']}%\n"

        f"Uppercase titles: "
        f"{profile['pct_uppercase_title']}%\n"

        f"Titles containing digits: "
        f"{profile['pct_title_has_digit']}%\n"

        f"Median title length: "
        f"{profile['median_title_words']} words\n"

        f"Median description length: "
        f"{profile['median_description_words']} words"
    )


# ---------------------------------------------------------
# Μετατροπή seeds σε text για το prompt
# ---------------------------------------------------------

def format_seeds(
    seeds: list[dict],
) -> str:

    if not seeds:
        return "No real seeds available."

    lines = []

    for index, seed in enumerate(
        seeds,
        start=1,
    ):

        lines.append(
            f"Example {index}:\n"
            f"Title: "
            f"{seed['expense_title']}\n"
            f"Description: "
            f"{seed['expense_description']}"
        )

    return "\n\n".join(lines)


# ---------------------------------------------------------
# Κατασκευή generation prompt
# ---------------------------------------------------------

def build_generation_prompt(
    expense_class: str,
    n_to_generate: int,
) -> tuple[str, str]:

    system_prompt = read_text_file(
        SYSTEM_PROMPT_PATH
    )

    generation_template = read_text_file(
        GENERATION_PROMPT_PATH
    )

    profile = get_style_profile(
        expense_class
    )

    seeds = get_seeds(
        expense_class
    )

    style_profile_text = (
        format_style_profile(
            profile
        )
    )

    seeds_text = format_seeds(
        seeds
    )

    generation_prompt = (
        generation_template
        .replace(
            "{n_to_generate}",
            str(n_to_generate),
        )
        .replace(
            "{expense_class}",
            expense_class,
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


# ---------------------------------------------------------
# Καθαρισμός πιθανών ```json code fences
# ---------------------------------------------------------

def clean_json_response(
    text: str,
) -> str:

    text = text.strip()

    if text.startswith("```json"):
        text = text[7:]

    elif text.startswith("```"):
        text = text[3:]

    if text.endswith("```"):
        text = text[:-3]

    return text.strip()


# ---------------------------------------------------------
# Κλήση στο LLM
# ---------------------------------------------------------

def generate_expenses(
    expense_class: str,
    n_to_generate: int,
) -> list[SyntheticExpense]:

    system_prompt, generation_prompt = (
        build_generation_prompt(
            expense_class=expense_class,
            n_to_generate=n_to_generate,
        )
    )

    llm = create_llm()

    print()
    print(
        f"Generating {n_to_generate} "
        f"expenses for:"
    )

    print(expense_class)
    print()

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

    response_text = str(
        response.content
    )

    response_text = clean_json_response(
        response_text
    )

    data = json.loads(
        response_text
    )

    validated = (
        SyntheticExpenseBatch
        .model_validate(data)
    )

    if len(validated.expenses) != n_to_generate:
        raise ValueError(
            f"Expected {n_to_generate} expenses, "
            f"but received "
            f"{len(validated.expenses)}."
        )

    for expense in validated.expenses:

        if expense.expense_class != expense_class:
            raise ValueError(
                "Generated expense has "
                "incorrect expense_class."
            )

    return validated.expenses


# ---------------------------------------------------------
# Αποθήκευση test output
# ---------------------------------------------------------

def save_generated_expenses(
    expenses: list[SyntheticExpense],
) -> None:

    rows = [
        expense.model_dump()
        for expense in expenses
    ]

    df = pd.DataFrame(rows)

    TEST_OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    df.to_csv(
        TEST_OUTPUT_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    print(
        f"Saved: {TEST_OUTPUT_PATH}"
    )


# ---------------------------------------------------------
# Test run
# ---------------------------------------------------------

def main() -> None:

    expenses = generate_expenses(
        expense_class=TEST_EXPENSE_CLASS,
        n_to_generate=TEST_N_TO_GENERATE,
    )

    save_generated_expenses(
        expenses
    )

    print()
    print("Generated expenses:")
    print()

    for i, expense in enumerate(
        expenses,
        start=1,
    ):

        print(
            f"{i}. "
            f"{expense.expense_title}"
        )

        print(
            f"   {expense.expense_description}"
        )

        print()


if __name__ == "__main__":
    main()