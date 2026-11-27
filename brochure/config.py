"""
Central configuration: the model client, the model name, budgets, companies,
audiences and comparison dimensions. Every other module reads its settings from here.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PROJECT_ROOT / "runs"

load_dotenv(PROJECT_ROOT / ".env", override=True)

# ---------------------------------------------------------------------------
# Model access: the OpenAI SDK pointed at Anthropic's OpenAI-compatible endpoint
# ---------------------------------------------------------------------------
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1/"
API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
MODEL = "claude-opus-5-5"

# Output cap per call. On Claude 5 models thinking is on by default and counts
# toward this limit, so it is set well above the length of a brochure.
MAX_TOKENS = 16_000

# Seconds before a model request is abandoned (one attempt only, see max_retries=0).
MODEL_TIMEOUT_SECONDS = 300

# Parameters we deliberately do NOT send through the compatibility endpoint:
#   reasoning_effort  - ignored by the endpoint
#   temperature/top_p - rejected (400) on Claude Opus 5.5 unless left at the default
#   n != 1            - rejected (400)
#   response_format json_object - rejected (400); link selection uses json_schema + strict


def check_api_key() -> bool:
    """Report whether a plausible key is configured, without ever printing the key."""
    key = os.getenv(API_KEY_ENV_VAR)
    if not key:
        print(f"{API_KEY_ENV_VAR} is not set. Copy .env.example to .env and add your key.")
        return False
    if not key.startswith("sk-ant-"):
        print(f"{API_KEY_ENV_VAR} is set but does not start with 'sk-ant-'; check that it is a Claude API key.")
        return False
    print("API key found and looks like a Claude API key.")
    return True


_client: OpenAI | None = None


def get_client() -> OpenAI:
    """
    Return the single shared client. max_retries=0 so that every attempt is one
    that we made ourselves and wrote to the run log - the SDK never retries silently.
    """
    global _client
    if _client is None:
        key = os.getenv(API_KEY_ENV_VAR)
        if not key:
            raise RuntimeError(f"{API_KEY_ENV_VAR} is not set (see .env.example).")
        _client = OpenAI(
            api_key=key,
            base_url=ANTHROPIC_BASE_URL,
            max_retries=0,
            timeout=MODEL_TIMEOUT_SECONDS,
        )
    return _client


# ---------------------------------------------------------------------------
# Scraping and source collection
# ---------------------------------------------------------------------------
MAX_EXTRA_PAGES = 3            # pages the model may add on top of the home page
FETCH_TIMEOUT_SECONDS = 15     # per HTTP request to a company site
MIN_TEXT_CHARS = 200           # below this a fetched page counts as "empty"
FETCH_DELAY_SECONDS = 1.0      # pause between requests to the same site (some sites rate-limit)

# ---------------------------------------------------------------------------
# Context budget
# ---------------------------------------------------------------------------
# A budget in CHARACTERS of source text (not tokens) per model call.
# Instructions, headers and source IDs are added outside this budget and never cut.
# For the comparison call, both companies' sources share this one budget.
CONTEXT_CHAR_BUDGET = 12_000

# ---------------------------------------------------------------------------
# Output requirements
# ---------------------------------------------------------------------------
OUTPUT_LANGUAGE = "Hebrew"
MIN_WORDS = 200                # counted with str.split(), headings included,
MAX_WORDS = 300                # the closing source list excluded
MISSING_INFO_PHRASE = "לא נמצא במקורות שנמסרו למודל"
SOURCES_HEADING = "## מקורות"  # the closing source list starts here (not counted as words)

# ---------------------------------------------------------------------------
# Companies (chosen later; both must be in the same field)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Company:
    company_id: str   # short slug used in file names, e.g. "acme"
    name: str         # display name
    url: str          # home page URL
    letter: str       # source ID prefix: "A" for the first company, "B" for the second


COMPANIES: list[Company] = [
    # Company(company_id="...", name="...", url="https://...", letter="A"),
    # Company(company_id="...", name="...", url="https://...", letter="B"),
]


def get_companies() -> list[Company]:
    """Return the two configured companies, or fail with a clear message."""
    if len(COMPANIES) != 2:
        raise RuntimeError("Configure exactly two companies in brochure/config.py (COMPANIES).")
    if [c.letter for c in COMPANIES] != ["A", "B"]:
        raise RuntimeError("The first company must use letter 'A' and the second 'B'.")
    return COMPANIES


# ---------------------------------------------------------------------------
# Audiences and comparison dimensions (fixed before any generation)
# ---------------------------------------------------------------------------
AUDIENCES = {
    "customers": "לקוחות פוטנציאליים",
    "candidates": "מועמדים לעבודה",
}

COMPARISON_DIMENSIONS = [
    ("products_services", "מוצרים ושירותים"),
    ("target_audiences", "קהלי יעד"),
    ("pricing_trial", "תמחור או תקופת ניסיון"),
    ("working_at", "עבודה בחברה"),
]
