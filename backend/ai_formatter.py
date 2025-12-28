import re

def clean_ai_output(text: str) -> str:
    if not text:
        return ""

    # Remove markdown symbols
    text = re.sub(r"[#*_>`]", "", text)

    # Replace multiple newlines with single line breaks
    text = re.sub(r"\n{2,}", "\n", text)

    # Strip leading/trailing whitespace
    text = text.strip()

    return text
