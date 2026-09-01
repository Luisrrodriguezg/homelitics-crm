"""Hand-curated geography for the Valle de Aburrá (Medellín metro area).

HT-02 AC2 asks for "one real metro area (city × neighborhood × price tier) with
market-coherent price-per-m² by segment". This is that table. It replaces the
previous `Central City` with twelve invented neighborhoods, which was explicitly
location-agnostic and could not support the price-vs-zone-median conditioning the
funnel simulation needs.

Money is COP. A 120 m² apartment in El Poblado lands near 1.2e9, comfortably
inside `numeric(15,2)`. There is no currency column in the schema — the whole
dataset is COP and `docs/ground_truth.yaml` records that.

`city` is the municipality, not "Medellín" for everything: Envigado, Sabaneta,
Itagüí, Bello and La Estrella are separate municipalities inside the metro area,
and the Oriente towns (Rionegro, El Retiro, Guarne, La Ceja) are where fincas
actually are. That placement is what makes the geography read as real rather
than as a shuffled list of names.

Prices are 2025-ish asking prices per built m², rounded to something a local
would recognise. They encode the estrato ladder, which is the single strongest
predictor of price in this market.
"""
from __future__ import annotations

from dataclasses import dataclass

# Property types the schema allows: APARTMENT, HOUSE, STUDIO, COUNTRY_HOUSE.
URBAN = ("APARTMENT", "HOUSE", "STUDIO")
FINCA = ("COUNTRY_HOUSE", "HOUSE")


@dataclass(frozen=True)
class Zone:
    barrio: str
    city: str
    tier: int          # 1 (estrato 1-2) .. 5 (estrato 6)
    cop_per_m2: int    # asking price per built m², SALE
    weight: float      # relative share of inventory
    types: tuple[str, ...] = URBAN


# ---------------------------------------------------------------------------
# Tier 5 — estrato 6. El Poblado and the Envigado ridge.
# ---------------------------------------------------------------------------
_T5 = [
    Zone("El Tesoro",                    "Medellín", 5, 10_800_000, 0.9),
    Zone("Los Balsos",                   "Medellín", 5, 10_200_000, 0.9),
    Zone("Provenza",                     "Medellín", 5,  9_800_000, 1.1),
    Zone("Manila",                       "Medellín", 5,  9_400_000, 0.8),
    Zone("Castropol",                    "Medellín", 5,  9_100_000, 0.7),
    Zone("Santa María de los Ángeles",   "Medellín", 5,  8_700_000, 0.7),
    Zone("Zúñiga",                       "Envigado", 5,  8_500_000, 0.9),
]

# ---------------------------------------------------------------------------
# Tier 4 — estrato 5. Laureles, Estadio, the Envigado/Sabaneta centres.
# ---------------------------------------------------------------------------
_T4 = [
    Zone("Laureles",         "Medellín", 4, 7_400_000, 1.6),
    Zone("Conquistadores",   "Medellín", 4, 7_100_000, 1.2),
    Zone("Estadio",          "Medellín", 4, 6_800_000, 1.2),
    Zone("Bolivariana",      "Medellín", 4, 6_600_000, 0.9),
    Zone("Suramericana",     "Medellín", 4, 6_300_000, 0.9),
    Zone("Envigado Centro",  "Envigado", 4, 6_500_000, 1.4),
    Zone("Aves María",       "Sabaneta", 4, 6_100_000, 1.2),
]

# ---------------------------------------------------------------------------
# Tier 3 — estrato 4. Belén, La América, the southern municipalities.
# ---------------------------------------------------------------------------
_T3 = [
    Zone("Loma de los Bernal", "Medellín",    3, 5_200_000, 1.2),
    Zone("Belén La Palma",     "Medellín",    3, 4_900_000, 1.5),
    Zone("Rosales",            "Medellín",    3, 4_700_000, 1.1),
    Zone("Calasanz",           "Medellín",    3, 4_600_000, 1.3),
    Zone("Los Colores",        "Medellín",    3, 4_800_000, 1.0),
    Zone("Simón Bolívar",      "Medellín",    3, 4_400_000, 1.0),
    Zone("Buenos Aires",       "Medellín",    3, 4_200_000, 1.2),
    Zone("Itagüí Centro",      "Itagüí",      3, 4_300_000, 1.4),
    Zone("La Estrella Centro", "La Estrella", 3, 4_100_000, 0.9),
]

# ---------------------------------------------------------------------------
# Tier 2 — estrato 3. The northern and western comunas, Bello.
# ---------------------------------------------------------------------------
_T2 = [
    Zone("Robledo",   "Medellín", 2, 3_500_000, 1.5),
    Zone("Castilla",  "Medellín", 2, 3_400_000, 1.4),
    Zone("Aranjuez",  "Medellín", 2, 3_100_000, 1.2),
    Zone("Niquía",    "Bello",    2, 3_300_000, 1.3),
    Zone("Bello Centro", "Bello", 2, 3_000_000, 1.3),
    Zone("Belén Rincón", "Medellín", 2, 3_600_000, 1.0),
]

# ---------------------------------------------------------------------------
# Tier 1 — estrato 1-2. The nororiental and centro-occidental comunas.
# ---------------------------------------------------------------------------
_T1 = [
    Zone("Manrique",         "Medellín", 1, 2_400_000, 1.2),
    Zone("Doce de Octubre",  "Medellín", 1, 2_300_000, 1.1),
    Zone("San Javier",       "Medellín", 1, 2_200_000, 1.1),
    Zone("Santa Cruz",       "Medellín", 1, 2_000_000, 0.9),
    Zone("Villa Hermosa",    "Medellín", 1, 2_100_000, 0.9),
    Zone("Popular",          "Medellín", 1, 1_850_000, 0.8),
]

# ---------------------------------------------------------------------------
# Oriente — fincas only. Priced per built m², but the lots are large, so these
# carry the highest absolute prices in the dataset despite a mid tier rate.
# ---------------------------------------------------------------------------
_ORIENTE = [
    Zone("Llanogrande",  "Rionegro",  5, 5_600_000, 0.7, FINCA),
    Zone("El Retiro",    "El Retiro", 4, 4_600_000, 0.5, FINCA),
    Zone("La Ceja",      "La Ceja",   3, 3_600_000, 0.5, FINCA),
    Zone("Guarne",       "Guarne",    2, 2_900_000, 0.4, FINCA),
]

ZONES: list[Zone] = _T5 + _T4 + _T3 + _T2 + _T1 + _ORIENTE

ZONES_BY_TYPE: dict[str, list[Zone]] = {
    t: [z for z in ZONES if t in z.types]
    for t in ("APARTMENT", "HOUSE", "STUDIO", "COUNTRY_HOUSE")
}

# Type multiplier on the zone rate. A house costs more per m² than a flat in the
# same barrio (it carries land); a studio less (small units trade at a discount
# per m² here, unlike in some markets).
TYPE_FACTOR = {"APARTMENT": 1.00, "HOUSE": 1.06, "STUDIO": 0.92, "COUNTRY_HOUSE": 1.10}

# Built area, in m². Fincas are big; studios (aparta-estudios) are small.
AREA_RANGE = {
    "APARTMENT":     (48, 190),
    "HOUSE":         (90, 320),
    "STUDIO":        (28, 55),
    "COUNTRY_HOUSE": (180, 520),
}

# AC2: monthly rent as a fraction of sale price. The band, not a fixed number,
# so `docs/ground_truth.yaml` has an interval to assert against.
RENT_YIELD_RANGE = (0.004, 0.006)

# Rough share of inventory by operation. Rentals skew to the cheaper tiers in
# this market; the generator tilts this by tier rather than using it flat.
SALE_SHARE_BY_TIER = {1: 0.50, 2: 0.55, 3: 0.60, 4: 0.65, 5: 0.75}


def pick_zone(rng, property_type: str) -> Zone:
    """Weighted draw from the zones that allow this property type."""
    pool = ZONES_BY_TYPE[property_type]
    return rng.choices(pool, weights=[z.weight for z in pool], k=1)[0]


def street_address(rng, zone: Zone) -> str:
    """A Medellín-shaped address: `Calle 10 # 43A-25`.

    Hand-rolled rather than Faker's, whose Colombian addresses run to `Calle 199`
    — a Bogotá number. Medellín's grid does not go that high, and an address that
    could not exist undermines the point of curating the geography at all.
    """
    via = rng.choice(["Calle", "Carrera", "Transversal", "Diagonal"])
    main = rng.randint(1, 99)
    suffix = rng.choice(["", "", "", "A", "B", "C"])
    cross = rng.randint(1, 99)
    number = rng.randint(1, 99)
    unit = rng.choice(["", "", f" Apto {rng.randint(101, 1504)}", f" Torre {rng.randint(1, 6)}"])
    return f"{via} {main}{suffix} # {cross}-{number:02d}{unit}"


def phone(rng) -> str:
    """Colombian mobile, canonical digits only: 3XXXXXXXXX."""
    return f"3{rng.randint(0, 2)}{rng.randint(0, 9)}{rng.randint(0, 9999999):07d}"


def phone_variant(rng, canonical: str) -> str:
    """The same number, reformatted — AC5's duplicate-client fixture.

    Four shapes a Colombian CRM actually receives. All normalise to the same ten
    digits, which is what makes them findable by a data-quality rule and what a
    naive exact-match dedup misses.
    """
    d = "".join(c for c in canonical if c.isdigit())[-10:]
    style = rng.choice(["intl", "dashed", "spaced", "landline"])
    if style == "intl":
        return f"+57 {d[:3]} {d[3:6]} {d[6:]}"
    if style == "dashed":
        return f"{d[:3]}-{d[3:6]}-{d[6:]}"
    if style == "spaced":
        return f"{d[:3]} {d[3:]}"
    return f"(604) {d[2:]}"          # Medellín's landline code, mobile digits
