from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

RAW_DATA_DIR = BASE_DIR / "data" / "raw"
PROCESSED_DATA_DIR = BASE_DIR / "data" / "processed"
OUTPUT_DATA_DIR = BASE_DIR / "data" / "output"

CLIENT_DATA_PATH = RAW_DATA_DIR / "dataloansv2_utf8 2.csv"
CLEAN_CLIENT_DATA_PATH = PROCESSED_DATA_DIR / "client_expenses_clean.csv"
STYLE_PROFILE_PATH = PROCESSED_DATA_DIR / "class_style_profile.csv"

SEEDS_PATH = (
    PROCESSED_DATA_DIR
    / "generation_seeds.csv"
)

SYSTEM_PROMPT_PATH = BASE_DIR / "prompts" / "system_prompt.md"
GENERATION_PROMPT_PATH = BASE_DIR / "prompts" / "generation_prompt.md"

TEST_OUTPUT_PATH = OUTPUT_DATA_DIR / "synthetic_test.csv"

TEST_N_TO_GENERATE = 5
PAIR_SAMPLE_N_CLIENTS = 5

TEST_EXPENSE_CLASS = (
    "Εξοπλισμός αγορά/κατασκευή, Εξοπλισμός χρήση (αποσβέσεις/μισθώσεις)"
)

TARGET_PER_CLASS = 500
N_SEEDS_PER_GENERATION = 6

VALIDATED_TEST_OUTPUT_PATH = (
    OUTPUT_DATA_DIR
    / "synthetic_test_validated.csv"
)
#κάτω απο αυτό το στέλνω για manual review
VALIDATION_MIN_CONFIDENCE = 0.80

VALIDATED_TEST_EXCEL_PATH = (
    OUTPUT_DATA_DIR
    / "synthetic_test_validated.xlsx"
)

FINAL_COLUMNS = [
    "expense_title",
    "expense_description",
    "expense_class",
]