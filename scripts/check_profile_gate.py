"""Run the "is this a work profile?" check against the REAL model.

    ./run scripts/check_profile_gate.py

Not a gate, and not in CI: it spends a few model calls and needs a valid
ANTHROPIC_API_KEY. Run it after changing the check's prompt, or the model.

## Why it exists

The unit tests prove the route refuses what the check refuses. They cannot
prove the check refuses the RIGHT things, because every one of them uses a fake
that answers whatever the test needs. The failure that matters most is the
quiet one: a check that turns away a real resume closes the door at step one,
and nothing in the suite would notice.

So this sends synthetic documents — four real profiles in the shapes people
actually have (a LinkedIn export, a student with no jobs, a Hinglish bio, a
career changer) and four that are clearly not — and exits non-zero on any wrong
answer. Synthetic on purpose: nobody's real file belongs in a script.

First run: it could not run at all. The local `.env` key was refused with
`401 invalid x-api-key` — which is worth knowing before blaming the check for
a 503.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carigma_api.config import Settings  # noqa: E402
from carigma_api.services import extraction  # noqa: E402
from carigma_api.services.ai import UpstreamError, call_claude_json  # noqa: E402

SAMPLES: list[tuple[str, bool, str]] = [
    (
        "LinkedIn PDF export",
        True,
        """Contact
www.linkedin.com/in/priya-sharma-analytics (LinkedIn)
Top Skills
SQL
Power BI
Stakeholder Management
Priya Sharma
Senior Data Analyst at Swiggy
Bengaluru, Karnataka, India
Summary
I turn operational data into decisions. Six years across food delivery and fintech.
Experience
Swiggy
Senior Data Analyst
April 2021 - Present (3 years 6 months)
Built the city-level unit economics dashboard used in weekly business reviews.
Paytm
Data Analyst
July 2018 - March 2021 (2 years 9 months)
Education
Delhi Technological University
Bachelor of Technology - BTech, Computer Science (2014 - 2018)
Page 1 of 2""",
    ),
    (
        "final-year student CV, no jobs",
        True,
        """ANANYA REDDY | ananya.r@example.com | Hyderabad
Final-year B.Com student, Osmania University (expected 2025), CGPA 8.4
Projects: Built a budgeting tracker in Excel for my college fest committee (Rs 2 lakh budget).
Volunteer: Teaching assistant, Make A Difference NGO, weekends 2023-2024.
Skills: Excel, Tally, basic Python, English, Telugu, Hindi.
Looking for: internship in finance or operations.""",
    ),
    (
        "short bio, Hinglish",
        True,
        """Main Rohit hoon, 12 saal se sales mein kaam kar raha hoon. Currently Regional Sales Manager
at Asian Paints, North zone. Pehle Berger Paints mein Area Sales Manager tha. Team of 40 handle
karta hoon, dealer network growth meri strength hai. MBA from IMT Ghaziabad.""",
    ),
    (
        "career changer",
        True,
        """Former secondary school maths teacher (8 years, Kendriya Vidyalaya) moving into product
management. Completed a 6-month product management certificate (2024). Led the rollout of a
Google Classroom workflow for 30 teachers during 2020. Seeking associate product manager roles.""",
    ),
    (
        "freight invoice",
        False,
        """TAX INVOICE  No. INV-2024-4471  Date: 12/08/2024
Bill To: Acme Logistics Pvt Ltd, Plot 22, Sector 18, Gurugram, Haryana 122015
Description: 40ft container ocean freight, Nhava Sheva to Rotterdam. Qty 1. Rate 1,85,000.00
CGST 9%: 16,650.00  SGST 9%: 16,650.00  Total payable: 2,18,300.00
Payment due within 30 days.""",
    ),
    (
        "flight e-ticket",
        False,
        """E-TICKET / ITINERARY RECEIPT  PNR: X7K9QP
Passenger: MR A KUMAR  Flight 6E 2134  Delhi (DEL) T1 to Bengaluru (BLR) T1
Departure 14 Sep 2024 06:15  Arrival 09:00  Seat 12C  Baggage 15kg check-in, 7kg cabin
Fare: 5,432  Taxes: 1,210  Total: 6,642 INR  Web check-in opens 48 hours before departure.""",
    ),
    (
        "job advertisement",
        False,
        """We're hiring: Senior Data Analyst, Gurugram (hybrid).
What you'll do: own the metrics for our restaurant partner platform; partner with product and ops.
What we're looking for: 4+ years in analytics, strong SQL, experience with Tableau or Power BI.
Perks: health insurance, ESOPs, learning budget. Apply by 30 September.""",
    ),
    (
        "bank statement",
        False,
        """Statement of Account  01/07/2024 to 31/07/2024
Account No: XXXXXXXX5678  Branch: Connaught Place, New Delhi
Date       Narration                         Withdrawal   Deposit     Balance
02/07/24   UPI-FOOD-ORDER                    412.00                   84,210.55
05/07/24   SALARY JULY                                    1,12,000.00 1,96,210.55
09/07/24   ACH-HOME LOAN EMI                 38,450.00                1,57,760.55""",
    ),
]


def main() -> int:
    settings = Settings()
    wrong = 0
    for label, expected, text in SAMPLES:
        try:
            extraction.check_is_profile(
                text,
                extractor=lambda system, user, max_tokens: call_claude_json(
                    system, user, max_tokens, api_key=settings.anthropic_api_key
                ),
                pasted=False,
            )
            got: bool | None = True
        except extraction.NotAProfile:
            got = False
        except (extraction.ProfileCheckFailed, UpstreamError) as exc:
            print(f"could not run the check: {exc}")
            return 2
        ok = got is expected
        wrong += 0 if ok else 1
        want = "profile" if expected else "refuse"
        print(f"{'ok   ' if ok else 'WRONG'}  expected {want:<7}  got {got}  {label}")

    print(f"\n{len(SAMPLES) - wrong}/{len(SAMPLES)} as expected")
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
