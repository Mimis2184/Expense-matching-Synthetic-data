Generate exactly {n_to_generate} synthetic client expenses for the following expense class.

# Target Expense Class

{expense_class}

# Real Writing Style Profile

{style_profile}

# Real Client Examples

{seeds}

Use the style profile and the real client examples only as references for how real clients write.

Do not copy any real example verbatim.

# Generation Requirements

For every generated expense:

- The expense must belong to the target expense_class.
- Generate a new expense_title.
- Generate a new expense_description.
- Write as a real client would write, not as a consultant or funding-program expert.
- Preserve realistic variation in wording and level of detail.
- Some titles or descriptions may be short or incomplete.
- expense_title and expense_description may be identical when this is consistent with the observed real-data pattern.
- Respect the observed Greek / English / mixed-language distribution.
- Respect the typical title and description length reflected in the style profile.
- Commercial names, software names, abbreviations, acronyms, product models, codes, versions, and years may appear when realistic.
- Uppercase text may appear when consistent with the observed writing style.
- Avoid repeatedly generating examples that differ only by one word, number, model, or brand.
- Avoid artificial or overly polished phrases such as:
  - "Η δαπάνη αφορά..."
  - "Σύμφωνα με..."
  - "Σκοπός της δαπάνης είναι..."
  unless such wording is genuinely consistent with the provided real examples.

# Strict Constraints

- Generate exactly {n_to_generate} expenses.
- Every generated row must use exactly the provided expense_class.
- Do not rename, normalize, merge, or reinterpret the expense_class.
- Do not copy real examples verbatim.
- Do not generate any additional fields.
- Do not include explanations, comments, markdown, or text outside the JSON object.

# Output Format

Return valid JSON only.

Do not use markdown code fences.

Use exactly this structure:

{
  "expenses": [
    {
      "expense_title": "...",
      "expense_description": "...",
      "expense_class": "{expense_class}"
    }
  ]
}