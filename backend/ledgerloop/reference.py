"""Reference data the agents screen against.

The sanctions list is a small made-up stand-in. A real deployment would load an
actual screening list and refresh it; here it only needs to exercise the
exact-match / near-match / no-match paths.
"""

SANCTIONED_ENTITIES = (
    "Obsidian Harbor Trading FZE",
    "Karstvel Maritime Holdings",
    "Redmarsh Logistics Group",
    "Northgate Petrochem Supply",
    "Veloria Arms Brokerage",
    "Tessaract Metals Exchange",
)

HIGH_RISK_COUNTRIES = frozenset({"XX", "ZZ"})
