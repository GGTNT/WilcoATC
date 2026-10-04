"""Who else is on the frequency, and what language they speak.

The traffic around you is not a random sample of the world's airlines. At Orly
most of it is Air France and Transavia, with easyJet and Vueling and a Royal Air
Maroc; at Frankfurt it is Lufthansa and Condor and Eurowings. An injector that
draws uniformly from a global list produces a Qantas at Clermont-Ferrand, which
is the single fastest way to stop believing the radio.

So an operator declares where it is based, and the roster for an airport is
drawn from operators based near it -- heavily weighted to the home country,
then the region, then the long-haul carriers that only appear at large fields.

*Where people go on holiday is its own geography.* Funchal is not served by
whoever is based near Portugal: it is TAP and easyJet, and then Jet2, TUI,
Transavia, Brussels, Luxair, Edelweiss, Condor and Binter, most of them
based two thousand miles north. With only home and region, Madeira was TAP
with the odd Iberia on every visit. So the sun-and-island airports are
listed once (:data:`LEISURE`), and an operator that flies the north of
Europe to them says so with ``leisure=True``.

*The language.* An operator has a home language, and the rule that decides what
its crews actually speak on a given frequency is the same one that applies to
the user: the local language if the state below works it and the crew knows it,
otherwise English. Air France speaks French at Orly, French at Geneva, and
English at Frankfurt. Lufthansa speaks German at Frankfurt, German at Vienna,
and English at Heathrow. Neither of those is a special case; both fall out of
:func:`wilcoatc.atc.language.reply_language` applied to the crew's language.
"""

from __future__ import annotations

from dataclasses import dataclass

from .language import reply_language

# Fleets, by the shape of the operation rather than by exact type. The injector
# picks from these so a regional carrier is not flying a 777 into a field with
# a 5,000 foot runway.
NARROWBODY = ("A320", "A321", "A319", "B738", "B38M", "A20N")
REGIONAL = ("E190", "E195", "CRJ9", "AT76", "DH8D", "E75L")
WIDEBODY = ("B77W", "A359", "B789", "A333", "B78X", "A388")
FREIGHT = ("B763", "B77L", "A306", "B752")


@dataclass(frozen=True)
class Operator:
    """An airline, as the radio knows it."""

    icao: str                      # AFR
    language: str                  # what the crew speaks when they can
    home: tuple[str, ...]          # ICAO prefixes of the home country
    region: tuple[str, ...] = ()   # prefixes it serves without being based there
    fleet: tuple[str, ...] = NARROWBODY
    weight: float = 1.0            # relative frequency at home
    long_haul: bool = False        # only appears at large fields
    leisure: bool = False          # flies to the airports in LEISURE
    # A tour operator's airline: based at home, but at the secondary fields
    # -- Gatwick and Manchester, not Heathrow -- so rare at a major hub.
    charter: bool = False
    # Flight numbers are drawn from the operator's own range, because a real
    # one is not uniform over four digits: short-haul is low, long-haul high.
    number_range: tuple[int, int] = (100, 3999)

    def speaks_at(self, ident: str) -> str:
        """The language this operator's crew uses at a given facility.

        The crew's own language if the state below works it, English if not.
        This is the whole of the rule the user asked for, and it is the same
        function the user's own aircraft is answered by.
        """
        return reply_language(ident, self.language, None)


def _op(icao, language, home, **kwargs) -> Operator:
    return Operator(icao, language, tuple(home), **kwargs)


# The radiotelephony name of every operator here that the general table in
# :mod:`wilcoatc.atc.speech` does not already carry. Several are not the
# airline's trading name at all -- Neos is "Moonflower", Air Nostrum is
# "Aironur", South African is "Springbok" -- which is exactly why they cannot
# be guessed from the designator.
TELEPHONY: dict[str, str] = {
    "AEA": "Europa", "AEE": "Aegean", "AEZ": "Aeroitalia",
    "AMX": "Aeromexico", "ANE": "Aironur", "ARG": "Argentina",
    "AZU": "Azul", "BOX": "German Cargo", "CMP": "Copa", "CRL": "Corsair",
    "DAH": "Air Algerie", "DLA": "Dolomiti", "EDW": "Edelweiss",
    "ENY": "Envoy", "EVE": "Evelop", "EWG": "Eurowings",
    "FPO": "French Post", "GEC": "Lufthansa Cargo", "HOP": "Air France",
    "ITY": "Italia", "JZA": "Jazz", "NAX": "Nor Shuttle",
    "NOS": "Moonflower", "RAM": "Royalair Maroc", "RZO": "Azores",
    "SAA": "Springbok", "TAR": "Tunair", "TRA": "Transavia",
    "WHT": "Yankee Whiskey",
}


# The airports the north of Europe flies to on holiday, as ICAO prefixes:
# whole countries where the holiday is the country (Portugal, the Canaries,
# Greece, Cyprus, Malta, Cape Verde), single fields where it is not.
LEISURE: tuple[str, ...] = (
    "LP", "GC", "GV", "LG", "LC", "LM",
    # Spain beyond the Canaries: the Balearics and the southern coasts.
    "LEPA", "LEIB", "LEMH", "LEAL", "LEMG", "LEGE", "LERS", "LEAM",
    # Italy's islands and the south.
    "LICC", "LICJ", "LIEO", "LIEE", "LIBD", "LIRN",
    # The Adriatic, Turkey's coast, Egypt's Red Sea, Morocco and Tunisia.
    "LDDU", "LDSP", "LDZD", "LTAI", "LTBS", "LTFE", "LTBJ",
    "HEGN", "HESH", "HEMA", "GMAD", "GMMX", "DTNH", "DTTJ", "DTMB",
    # Corsica, the Riviera and Gibraltar.
    "LFKJ", "LFKB", "LFMN", "LXGB",
)

# How often a leisure carrier appears at one of those fields, against the
# 1.2 of a regional operator. Several of them are there at once, so each one
# is a little rarer than a regional carrier and together they are a good
# half of the traffic, which is what Funchal's departure board looks like.
LEISURE_WEIGHT = 1.0

# What is left of a charter airline's home weight at a rank-5 hub. Jet2 and
# TUI are a tenth of Manchester's movements and none of Heathrow's.
CHARTER_AT_HUB = 0.15


# The operators, grouped by where they are based. `home` is the ICAO prefix of
# the base; `region` widens where the operator is common without being local.
OPERATORS: tuple[Operator, ...] = (

    # ------------------------------------------------------------ France --
    _op("AFR", "fr", ("LF",), region=("EB", "LS", "LI", "LE", "EG", "ED"),
        fleet=NARROWBODY, weight=6.0, number_range=(1000, 1999)),
    _op("AFR", "fr", ("LF",), fleet=WIDEBODY, weight=2.0, long_haul=True,
        number_range=(6, 999)),
    _op("HOP", "fr", ("LF",), fleet=REGIONAL, weight=2.5,
        number_range=(1000, 7999)),
    _op("TVF", "fr", ("LF",), region=("GM", "DT"), fleet=("B738", "A20N"),
        weight=2.5, leisure=True, number_range=(3000, 3999)),
    _op("FPO", "fr", ("LF",), fleet=("B738", "A320"), weight=0.8),
    _op("CRL", "fr", ("EB", "LF"), fleet=REGIONAL, weight=1.0),

    # ----------------------------------------------------------- Germany --
    _op("DLH", "de", ("ED",), region=("LO", "LS", "EP", "LK", "EH"),
        fleet=NARROWBODY, weight=6.0, number_range=(100, 1999)),
    _op("DLH", "de", ("ED",), fleet=WIDEBODY, weight=2.0, long_haul=True,
        number_range=(400, 799)),
    _op("EWG", "de", ("ED",), fleet=("A320", "A319"), weight=3.0,
        leisure=True, number_range=(1000, 9999)),
    _op("CFG", "de", ("ED",), fleet=("A320", "A321", "B753"), weight=1.5,
        leisure=True, number_range=(2000, 2999)),
    _op("TUI", "de", ("ED",), fleet=("B738", "B38M"), weight=2.5,
        leisure=True, charter=True, number_range=(1000, 9999)),
    _op("DLA", "de", ("ED",), fleet=REGIONAL, weight=1.5),
    _op("AUA", "de", ("LO",), region=("ED", "LK", "LH"), fleet=REGIONAL,
        weight=3.0, number_range=(100, 999)),
    _op("SWR", "de", ("LS",), region=("ED", "LO", "LF"), fleet=NARROWBODY,
        weight=3.5, number_range=(100, 1999)),
    _op("EDW", "de", ("LS",), fleet=("A320",), weight=1.2, leisure=True),

    # ------------------------------------------------------------- Spain --
    _op("IBE", "es", ("LE", "GC"), region=("LP", "LF", "EG"), fleet=NARROWBODY,
        weight=5.0, number_range=(1000, 6999)),
    _op("IBE", "es", ("LE",), fleet=WIDEBODY, weight=1.5, long_haul=True,
        number_range=(6000, 6999)),
    _op("VLG", "es", ("LE", "GC"), region=("LF", "LI"), fleet=("A320", "A321"),
        weight=4.0, leisure=True, number_range=(1000, 8999)),
    _op("ANE", "es", ("LE", "GC"), fleet=REGIONAL, weight=2.0),
    # The Canaries' own airline, which also links them to Madeira, Cape
    # Verde and Morocco.
    _op("IBB", "es", ("GC",), region=("LPMA", "LPPS", "GV", "GMAD", "GMMX"),
        fleet=("AT76", "E195"), weight=4.0, number_range=(1000, 9999)),
    _op("AEA", "es", ("LE",), fleet=NARROWBODY, weight=1.5),
    _op("EVE", "es", ("LE",), fleet=("A320",), weight=1.0),
    _op("AVA", "es", ("SK",), region=("LE",), fleet=NARROWBODY, weight=3.0),
    _op("ARG", "es", ("SA",), fleet=NARROWBODY, weight=3.0),
    _op("LAN", "es", ("SC",), region=("SP", "SK"), fleet=NARROWBODY, weight=3.0),
    _op("AMX", "es", ("MM",), fleet=NARROWBODY, weight=3.5),
    _op("VOI", "es", ("MM",), fleet=("A320", "A20N"), weight=3.0),
    _op("CMP", "es", ("MP",), region=("MM", "SK"), fleet=("B738",), weight=3.0),

    # ------------------------------------------------------------- Italy --
    _op("ITY", "it", ("LI",), region=("LF", "ED", "LE"), fleet=NARROWBODY,
        weight=5.0, number_range=(1000, 3999)),
    _op("AZA", "it", ("LI",), fleet=NARROWBODY, weight=1.0,
        number_range=(1000, 3999)),
    _op("NOS", "it", ("LI",), fleet=("B788", "A321"), weight=1.2),
    _op("AEZ", "it", ("LI",), fleet=REGIONAL, weight=1.5),
    _op("DLA", "de", ("LI", "ED"), fleet=REGIONAL, weight=0.8),

    # -------------------------------------------------------- Portuguese --
    _op("TAP", "pt", ("LP",), region=("LE", "LF", "GV"), fleet=NARROWBODY,
        weight=5.0, number_range=(1000, 1999)),
    _op("TAP", "pt", ("LP",), fleet=WIDEBODY, weight=1.2, long_haul=True,
        number_range=(1, 99)),
    _op("RZO", "pt", ("LP", "GV"), fleet=REGIONAL, weight=1.5),
    _op("WHT", "pt", ("LP",), fleet=("A320",), weight=0.8),
    _op("TAM", "pt", ("SB",), region=("SA", "SC"), fleet=NARROWBODY, weight=4.0),
    _op("AZU", "pt", ("SB",), fleet=REGIONAL, weight=3.5),
    _op("GLO", "pt", ("SB",), fleet=("B738",), weight=4.0),

    # ----------------------------------------------- English, in Europe --
    _op("BAW", "en", ("EG",), region=("LF", "ED", "LE", "LI", "EI"),
        fleet=NARROWBODY, weight=6.0, number_range=(100, 2999)),
    # BA's holiday flying, apart so it does not carry mainline's weight into
    # every island in the Mediterranean.
    _op("BAW", "en", ("EG",), fleet=("A320", "A20N"), weight=1.5,
        leisure=True, number_range=(2500, 2999)),
    _op("BAW", "en", ("EG",), fleet=WIDEBODY, weight=2.0, long_haul=True,
        number_range=(1, 299)),
    _op("EZY", "en", ("EG",), region=("LF", "LE", "LI", "LS", "EH", "LP"),
        fleet=("A320", "A319", "A20N", "A21N"), weight=5.0, leisure=True,
        number_range=(1000, 9999)),
    _op("EXS", "en", ("EG",), fleet=("B738", "A321", "A21N"), weight=3.5,
        leisure=True, charter=True, number_range=(100, 999)),
    _op("TOM", "en", ("EG",), fleet=("B738", "B38M", "B789"), weight=3.0,
        leisure=True, charter=True, number_range=(1000, 9999)),
    _op("RYR", "en", ("EI",), region=("EG", "LE", "LI", "LP", "ED"),
        fleet=("B738", "B38M"), weight=5.0, leisure=True,
        number_range=(1000, 9999)),
    _op("EIN", "en", ("EI",), region=("EG",), fleet=NARROWBODY, weight=2.5),
    _op("KLM", "en", ("EH",), region=("EG", "LF", "ED"), fleet=NARROWBODY,
        weight=5.0, number_range=(1000, 1999)),
    _op("KLM", "en", ("EH",), fleet=WIDEBODY, weight=1.5, long_haul=True,
        number_range=(600, 899)),
    _op("TRA", "en", ("EH",), fleet=("B738", "A20N"), weight=2.0,
        leisure=True),
    _op("BEL", "en", ("EB",), region=("LF", "EG"), fleet=("A320", "A319"),
        weight=2.5, leisure=True),
    _op("JAF", "en", ("EB",), fleet=("B738", "B38M"), weight=1.5,
        leisure=True, charter=True, number_range=(1000, 9999)),
    # Luxair's crews are as at home in French as in English.
    _op("LGL", "fr", ("EL",), region=("LF", "ED", "EB"),
        fleet=("B738", "B38M", "DH8D", "E195"), weight=2.0, leisure=True,
        number_range=(4000, 9999)),
    _op("SAS", "en", ("EK", "ES", "EN"), region=("ED", "EG"), fleet=NARROWBODY,
        weight=3.5),
    _op("NAX", "en", ("EN", "EK"), fleet=("B738", "B38M"), weight=3.0,
        leisure=True),
    _op("ENT", "en", ("EP",), fleet=("B738", "B38M"), weight=1.0,
        leisure=True, charter=True, number_range=(1000, 9999)),
    _op("TVS", "en", ("LK",), fleet=("B738", "B38M"), weight=1.0,
        leisure=True, charter=True, number_range=(1000, 9999)),
    _op("FIN", "en", ("EF",), region=("ES", "EK"), fleet=NARROWBODY, weight=3.0),
    _op("WZZ", "en", ("LH", "EP"), region=("EG", "LI", "LE"),
        fleet=("A321", "A21N", "A320"), weight=3.5, leisure=True),
    _op("LOT", "en", ("EP",), region=("ED", "EG"), fleet=REGIONAL, weight=3.0),
    _op("AEE", "en", ("LG",), region=("LI", "ED"), fleet=NARROWBODY, weight=3.0),
    _op("THY", "en", ("LT",), region=("ED", "EG", "LF"), fleet=NARROWBODY,
        weight=3.5),
    _op("SXS", "en", ("LT",), region=("ED",), fleet=("B738", "B38M"),
        weight=2.5, leisure=True, charter=True, number_range=(1000, 9999)),
    _op("TAR", "en", ("DT",), region=("LF",), fleet=NARROWBODY, weight=1.5),
    _op("RAM", "en", ("GM",), region=("LF", "LE"), fleet=NARROWBODY, weight=2.0),
    _op("DAH", "en", ("DA",), region=("LF",), fleet=NARROWBODY, weight=1.5),

    # ------------------------------------------------ English, elsewhere --
    _op("UAL", "en", ("K",), fleet=NARROWBODY, weight=6.0,
        number_range=(100, 2999)),
    _op("DAL", "en", ("K",), fleet=NARROWBODY, weight=6.0,
        number_range=(100, 2999)),
    _op("AAL", "en", ("K",), fleet=NARROWBODY, weight=6.0,
        number_range=(100, 2999)),
    _op("SWA", "en", ("K",), fleet=("B738", "B37M"), weight=6.0,
        number_range=(100, 4999)),
    _op("JBU", "en", ("K",), fleet=("A320", "E190"), weight=3.0),
    _op("ASA", "en", ("K",), fleet=("B738",), weight=2.5),
    _op("SKW", "en", ("K",), fleet=REGIONAL, weight=4.0,
        number_range=(3000, 5999)),
    _op("ENY", "en", ("K",), fleet=REGIONAL, weight=3.0,
        number_range=(3000, 3999)),
    _op("UAL", "en", ("K",), fleet=WIDEBODY, weight=1.5, long_haul=True,
        number_range=(1, 199)),
    _op("ACA", "en", ("C",), region=("K",), fleet=NARROWBODY, weight=5.0),
    _op("WJA", "en", ("C",), fleet=("B738",), weight=4.0),
    _op("JZA", "en", ("C",), fleet=REGIONAL, weight=3.0),
    _op("UAE", "en", ("OM",), fleet=WIDEBODY, weight=4.0, long_haul=True,
        number_range=(1, 999)),
    _op("QTR", "en", ("OT",), fleet=WIDEBODY, weight=4.0, long_haul=True,
        number_range=(1, 999)),
    _op("ETD", "en", ("OM",), fleet=WIDEBODY, weight=2.5, long_haul=True),
    _op("SIA", "en", ("WS",), fleet=WIDEBODY, weight=3.0, long_haul=True),
    _op("QFA", "en", ("Y",), fleet=NARROWBODY, weight=5.0),
    _op("ANZ", "en", ("NZ",), fleet=NARROWBODY, weight=5.0),
    _op("JAL", "en", ("RJ",), fleet=NARROWBODY, weight=5.0),
    _op("ANA", "en", ("RJ",), fleet=NARROWBODY, weight=5.0),
    _op("CES", "en", ("Z",), fleet=NARROWBODY, weight=5.0),
    _op("CCA", "en", ("Z",), fleet=NARROWBODY, weight=5.0),
    _op("AIC", "en", ("V",), fleet=NARROWBODY, weight=4.0),
    _op("SAA", "en", ("FA",), fleet=NARROWBODY, weight=4.0),
    _op("ETH", "en", ("HA",), region=("FA",), fleet=WIDEBODY, weight=3.0,
        long_haul=True),

    # ----------------------------------------------------------- freight --
    _op("FDX", "en", ("K",), region=("LF", "ED", "EG"), fleet=FREIGHT,
        weight=1.5, number_range=(1000, 3999)),
    _op("UPS", "en", ("K",), region=("ED", "EG"), fleet=FREIGHT, weight=1.5,
        number_range=(200, 999)),
    _op("GEC", "de", ("ED",), fleet=FREIGHT, weight=1.0),
    _op("BOX", "de", ("ED",), fleet=FREIGHT, weight=0.8),
)


# Registration prefixes, for the light aircraft that make up the traffic at a
# field too small for an airline. The letters after the prefix are drawn at
# random, so a Cessna at Toussus is F-GXXX and one at Sywell is G-XXXX.
REGISTRATION_PREFIX: tuple[tuple[tuple[str, ...], str], ...] = (
    (("K", "PA", "PH"), "N"),
    (("C",), "C-G"),
    (("EG",), "G-"),
    (("EI",), "EI-"),
    (("LF",), "F-G"),
    (("ED", "ET"), "D-E"),
    (("LO",), "OE-"),
    (("LS",), "HB-"),
    (("LE", "GC"), "EC-"),
    (("LI",), "I-"),
    (("LP",), "CS-"),
    (("EH",), "PH-"),
    (("EB",), "OO-"),
    (("EL",), "LX-"),
    (("EK",), "OY-"),
    (("ES",), "SE-"),
    (("EN",), "LN-"),
    (("EF",), "OH-"),
    (("EP",), "SP-"),
    (("Y",), "VH-"),
    (("NZ",), "ZK-"),
    (("SB",), "PR-"),
    (("SA",), "LV-"),
)

# The light types that fill a general aviation field.
LIGHT_FLEET: tuple[str, ...] = (
    "C172", "C152", "C182", "PA28", "PA34", "DA40", "DA42", "SR22", "BE36",
    "P28A", "AT3", "TB20", "R44", "C25A", "PC12", "TBM9",
)


def registration_prefix(ident: str) -> str:
    """The registration prefix used where this airport is."""
    code = (ident or "").upper()
    best = ("", "N")
    for prefixes, prefix in REGISTRATION_PREFIX:
        for candidate in prefixes:
            if code.startswith(candidate) and len(candidate) > len(best[0]):
                best = (candidate, prefix)
    return best[1]


def _matches(prefixes: tuple[str, ...], ident: str) -> str:
    """The longest of ``prefixes`` that ``ident`` starts with."""
    longest = ""
    for prefix in prefixes:
        if ident.startswith(prefix) and len(prefix) > len(longest):
            longest = prefix
    return longest


@dataclass(frozen=True)
class Candidate:
    """An operator and how likely it is to turn up at one airport."""

    operator: Operator
    weight: float


def roster_for(ident: str, size_rank: int = 4) -> list[Candidate]:
    """The operators plausibly on frequency at an airport, with weights.

    Based at the field's own country counts most, then in the region it
    serves, then everybody else at a distant and much lower weight -- which is
    what puts one Emirates a day into Milan without putting one into Bergerac.

    ``size_rank`` is the airport's size, 0 to 5. Long-haul operators are left
    out below rank 4, because a 777 does not visit a regional field.
    """
    code = (ident or "").upper()
    found: list[Candidate] = []
    for operator in OPERATORS:
        if operator.long_haul and size_rank < 4:
            continue
        home = _matches(operator.home, code)
        region = _matches(operator.region, code)
        if home:
            # A longer prefix match is a closer base: "LFPO" against "LF" is
            # the home country, which is what should dominate.
            weight = operator.weight * (4.0 + len(home))
            if operator.charter and size_rank >= 5:
                weight *= CHARTER_AT_HUB
        elif region:
            weight = operator.weight * 1.2
        elif operator.leisure and _matches(LEISURE, code):
            weight = operator.weight * LEISURE_WEIGHT
        elif size_rank >= 5:
            # A major international field sees everyone, rarely.
            weight = operator.weight * 0.08
        elif size_rank >= 4:
            weight = operator.weight * 0.03
        else:
            continue
        found.append(Candidate(operator, weight))
    return found


def by_icao(code: str) -> Operator | None:
    """Any operator with this designator, for looking up a known callsign."""
    code = (code or "").upper()
    for operator in OPERATORS:
        if operator.icao == code:
            return operator
    return None


# The languages a cabin crew announce in, in the order they read them.
#
# Not :attr:`Operator.language`, which is what the crew say on the *radio*
# and decides the traffic around you: KLM talk to Schiphol in English and
# welcome their passengers in Dutch first. A carrier from a country with
# more than one language reads them all -- Brussels Airlines in Dutch, then
# French -- and English always comes last and once, whatever order a crew
# might use at home (Air Canada at Toronto reads English first; here it is
# French and then English, so English is never read twice).
CABIN_LANGUAGES: dict[str, tuple[str, ...]] = {
    "BEL": ("nl", "fr"),
    "JAF": ("nl", "fr"),
    "KLM": ("nl",),
    "TRA": ("nl",),
    "SWR": ("de", "fr"),
    "EDW": ("de",),
    "LGL": ("fr", "de"),
    # Canada's carriers announce in both official languages; the country
    # fallback below cannot say so, because "Canada" is not a language.
    "ACA": ("fr",),
    "JZA": ("fr",),
    "ROU": ("fr",),
    "TSC": ("fr",),
    "POE": ("fr",),
    "ICE": ("is",),
    "FIN": ("fi", "sv"),
    "RAM": ("ar", "fr"),
    "DAH": ("ar", "fr"),
    "TAR": ("ar", "fr"),
}

# For an airline the table above and :data:`OPERATORS` do not know, the
# country the callsign file registers it in -- written in French there.
COUNTRY_LANGUAGES: dict[str, tuple[str, ...]] = {
    "France": ("fr",), "Allemagne": ("de",), "Autriche": ("de",),
    "Suisse": ("de", "fr"), "Belgique": ("nl", "fr"), "Luxembourg": ("fr", "de"),
    "Pays-Bas": ("nl",), "Espagne": ("es",), "Italie": ("it",),
    "Portugal": ("pt",),
}


def cabin_languages(code: str) -> tuple[str, ...]:
    """The languages an airline's cabin announces in, English last and once.

    Empty for something that is not an airline at all -- a private
    registration -- which the caller decides about itself.
    """
    code = (code or "").strip().upper()
    if not code:
        return ()
    order = CABIN_LANGUAGES.get(code)
    if order is None:
        operator = by_icao(code)
        if operator is not None:
            order = (operator.language,)
    if order is None:
        from .airlines import country_of

        country = country_of(code)
        if not country:
            return ()
        order = COUNTRY_LANGUAGES.get(country, ())
    seen: list[str] = []
    for language in order:
        if language and language != "en" and language not in seen:
            seen.append(language)
    return tuple(seen) + ("en",)


__all__ = [
    "Operator", "Candidate", "OPERATORS", "TELEPHONY", "LEISURE", "roster_for",
    "by_icao", "cabin_languages", "CABIN_LANGUAGES",
    "registration_prefix", "LIGHT_FLEET", "NARROWBODY", "REGIONAL",
    "WIDEBODY", "FREIGHT",
]
