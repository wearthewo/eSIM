"""Write a synthetic CSV with every supported column, for load tests (default 10,000 rows).

Usage (from the repo root):
    .venv\\Scripts\\python.exe scripts\\make_big_csv.py
    .venv\\Scripts\\python.exe scripts\\make_big_csv.py --rows 2500 --out output\\big_2500.csv

The file goes to ``output/`` (gitignored). Data is synthetic only: ICCIDs use the
fake ``8999`` prefix, activation codes point at ``smdp.example.invalid``, IMSIs use
the test network 001-01, and emails use ``example.invalid``. Never put real
activation codes in a file.

Names are Greek. By default they come from a pool of first names and surnames,
like a real order. ``scripts/bench.py`` reuses ``make_csv`` with other name modes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROWS = 10_000
MAX_SCRIPT_ROWS = 10_000

COLUMNS = (
    "iccid",
    "activation_code",
    "confirmation_code",
    "imsi",
    "msisdn",
    "customer_name",
    "plan_name",
    "email",
)
SAME_NAME = "Γιώργος Παπαδόπουλος"
PLANS = ("Ταξίδι 10GB", "Ευρώπη 5GB", "Ελλάδα 20GB", "Κόσμος 3GB")
FIRST_NAMES = (
    "Γιώργος", "Μαρία", "Δημήτρης", "Ελένη", "Κώστας", "Κατερίνα", "Νίκος", "Βασιλική",
    "Γιάννης", "Σοφία", "Παναγιώτης", "Αγγελική", "Χρήστος", "Δήμητρα", "Θανάσης", "Ευαγγελία",
    "Αντώνης", "Ειρήνη", "Μιχάλης", "Αναστασία", "Σπύρος", "Ζωή", "Στέλιος", "Χριστίνα",
    "Αλέξανδρος", "Φωτεινή", "Ευάγγελος", "Παρασκευή", "Ηλίας", "Ξανθή",
)  # fmt: skip
SURNAMES = (
    "Παπαδόπουλος", "Νικολάου", "Δημητρίου", "Αθανασίου", "Γεωργίου", "Παπαδάκης",
    "Οικονόμου", "Βασιλείου", "Καραγιάννης", "Μακρής", "Αντωνίου", "Ιωάννου", "Χατζής",
    "Κωνσταντίνου", "Παππάς", "Ψαρράς-Ξανθόπουλος", "Θεοδωρίδης", "Ζαφειρίου", "Λαμπράκης",
    "Μιχαηλίδης", "Σταθόπουλος", "Τσολάκης", "Φραγκιαδάκης", "Ραφαηλίδης", "Βλάχος",
    "Αναγνωστόπουλος", "Ευθυμίου", "Κυριακίδης", "Ξυδάκης", "Ωραιόπουλος",
)  # fmt: skip
# Greek letters used to build a different character set per row (bench worst case).
# Letters of the plan names are left out so the non-ASCII characters drawn in the
# bold font (name + plan) are unique per row, not just the name's.
GREEK = "".join(
    ch
    for ch in "αβγδεζηθικλμνξοπρστυφχψωάέήίόύώΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ"
    if ch not in "".join(PLANS)
)


def luhn_check_digit(payload: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(payload)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def synthetic_iccid(n: int) -> str:
    """19-digit synthetic ICCID with the fake ``8999`` prefix and a valid Luhn digit."""
    payload = f"8999{n:014d}"
    return payload + luhn_check_digit(payload)


def pool_name(n: int) -> str:
    """A name from the pool; consecutive rows differ and all 900 combinations are used."""
    first = FIRST_NAMES[n % len(FIRST_NAMES)]
    surname = SURNAMES[(n // len(FIRST_NAMES) + n) % len(SURNAMES)]
    return f"{first} {surname}"


def unique_charset_name(key: int) -> str:
    """The ``GREEK`` letters picked by the bits of ``key`` (>= 1): a distinct set per key."""
    return "".join(ch for k, ch in enumerate(GREEK) if (key >> k) & 1)


def make_csv(rows: int, names: str = "pool", first_key: int = 1) -> bytes:
    """Return CSV bytes with ``rows`` valid synthetic rows and every supported column.

    ``names`` is ``"pool"`` (realistic mix), ``"same"`` (one name for every row) or
    ``"unique"`` (a different set of Greek characters per row, numbered from
    ``first_key``). Everything except the name is the same in all three modes.
    """
    lines = [",".join(COLUMNS)]
    for n in range(1, rows + 1):
        if names == "same":
            name = SAME_NAME
        elif names == "unique":
            name = unique_charset_name(first_key + n - 1)
        else:
            name = pool_name(n)
        plan = PLANS[0] if names != "pool" else PLANS[n % len(PLANS)]
        lines.append(
            f"{synthetic_iccid(n)},LPA:1$smdp.example.invalid$TEST-{n:05d}-AAAA-BBBB,"
            f"CONF{n:05d},00101{n:010d},+3069{n:08d},{name},{plan},"
            f"customer{n:05d}@mail.example.invalid"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def main() -> int:
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--rows", type=int, default=DEFAULT_ROWS, help="data rows (1-10000)")
    args.add_argument("--out", type=Path, default=None, help="default: output/big_<rows>.csv")
    opts = args.parse_args()
    if not 1 <= opts.rows <= MAX_SCRIPT_ROWS:
        args.error(f"--rows must be between 1 and {MAX_SCRIPT_ROWS}")

    target = opts.out or ROOT / "output" / f"big_{opts.rows}.csv"
    data = make_csv(opts.rows)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    megabytes = len(data) / (1024 * 1024)
    print(f"{target}: {opts.rows} rows, {len(data):,} bytes ({megabytes:.2f} MB)")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
