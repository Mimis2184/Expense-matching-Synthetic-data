import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import CLEAN_CLIENT_DATA_PATH, STYLE_PROFILE_PATH


def detect_language_style(text: str) -> str:
    """
    Ελέγχει αν ένα κείμενο είναι:
    - greek
    - english
    - mixed
    - other
    """

    text = str(text)

    # Ψάχνει αν υπάρχουν ελληνικοί χαρακτήρες
    has_greek = bool(
        re.search(r"[Α-Ωα-ωΆ-Ώά-ώ]", text)
    )

    # Ψάχνει αν υπάρχουν λατινικοί χαρακτήρες
    has_latin = bool(
        re.search(r"[A-Za-z]", text)
    )

    if has_greek and has_latin:
        return "mixed"

    if has_greek:
        return "greek"

    if has_latin:
        return "english"

    return "other"


def is_uppercase_text(text: str) -> bool:
    """
    Ελέγχει αν όλα τα γράμματα του title
    είναι γραμμένα με κεφαλαία.
    """

    text = str(text)

    # Κρατάμε μόνο τα γράμματα
    letters = "".join(
        char
        for char in text
        if char.isalpha()
    )

    # Αν δεν υπάρχουν γράμματα
    if not letters:
        return False

    return letters == letters.upper()


def has_digit(text: str) -> bool:
    """
    Ελέγχει αν το κείμενο περιέχει αριθμό.
    """

    return bool(
        re.search(r"\d", str(text))
    )


def word_count(text: str) -> int:
    """
    Μετράει πόσες λέξεις έχει το κείμενο.
    """

    return len(
        str(text).split()
    )


def analyze_styles() -> None:
    """
    Αναλύει το writing style των πραγματικών
    client expenses ανά expense_class.
    """

    print(f"Reading: {CLEAN_CLIENT_DATA_PATH}")

    # Διαβάζουμε το cleaned client dataset
    df = pd.read_csv(
        CLEAN_CLIENT_DATA_PATH,
        encoding="utf-8-sig",
    )

    print(f"Rows loaded: {len(df):,}")

    # ---------------------------------------------------
    # 1. Title == Description
    # ---------------------------------------------------

    df["title_equals_description"] = (
        df["expense_title"].str.strip()
        == df["expense_description"].str.strip()
    )

    # ---------------------------------------------------
    # 2. Greek / English / Mixed
    # ---------------------------------------------------

    df["language_style"] = (
        df["expense_title"]
        .apply(detect_language_style)
    )

    # ---------------------------------------------------
    # 3. Uppercase title
    # ---------------------------------------------------

    df["title_uppercase"] = (
        df["expense_title"]
        .apply(is_uppercase_text)
    )

    # ---------------------------------------------------
    # 4. Αριθμοί / model codes
    # ---------------------------------------------------

    df["title_has_digit"] = (
        df["expense_title"]
        .apply(has_digit)
    )

    # ---------------------------------------------------
    # 5. Αριθμός λέξεων στο title
    # ---------------------------------------------------

    df["title_words"] = (
        df["expense_title"]
        .apply(word_count)
    )

    # ---------------------------------------------------
    # 6. Αριθμός λέξεων στο description
    # ---------------------------------------------------

    df["description_words"] = (
        df["expense_description"]
        .apply(word_count)
    )

    # ---------------------------------------------------
    # Ανάλυση ανά expense_class
    # ---------------------------------------------------

    rows = []

    for expense_class, group in df.groupby("expense_class"):

        # Πόσα examples έχει αυτή η class
        total = len(group)

        # Μετράμε πόσα greek / english / mixed / other
        language_counts = (
            group["language_style"]
            .value_counts()
        )

        # Μικρή function για μετατροπή σε %
        def pct(count: float) -> float:
            if total == 0:
                return 0.0

            return round(
                100 * count / total,
                2,
            )

        # Φτιάχνουμε ένα row με τα statistics
        rows.append(
            {
                "expense_class": expense_class,

                "n_examples": total,

                "pct_title_equals_description": pct(
                    group[
                        "title_equals_description"
                    ].sum()
                ),

                "pct_greek": pct(
                    language_counts.get(
                        "greek",
                        0,
                    )
                ),

                "pct_english": pct(
                    language_counts.get(
                        "english",
                        0,
                    )
                ),

                "pct_mixed": pct(
                    language_counts.get(
                        "mixed",
                        0,
                    )
                ),

                "pct_other": pct(
                    language_counts.get(
                        "other",
                        0,
                    )
                ),

                "pct_uppercase_title": pct(
                    group[
                        "title_uppercase"
                    ].sum()
                ),

                "pct_title_has_digit": pct(
                    group[
                        "title_has_digit"
                    ].sum()
                ),

                "median_title_words": round(
                    group[
                        "title_words"
                    ].median(),
                    1,
                ),

                "median_description_words": round(
                    group[
                        "description_words"
                    ].median(),
                    1,
                ),
            }
        )

    
    profile = pd.DataFrame(rows)

    # Βάζουμε πρώτες τις classes
    # με τα περισσότερα πραγματικά examples
    profile = profile.sort_values(
        "n_examples",
        ascending=False,
    )

    # Εξασφαλίζουμε ότι υπάρχει
    # ο φάκελος data/processed
    STYLE_PROFILE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )


    profile.to_csv(
        STYLE_PROFILE_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print(f"Saved: {STYLE_PROFILE_PATH}")
    print()

   
    print(
        profile.to_string(
            index=False
        )
    )



if __name__ == "__main__":
    analyze_styles()