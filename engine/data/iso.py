"""Reference lists the data checks and the source mappers share: ISO 3166-1 alpha-2 country codes and common country names.

Why a static list: country decides which law applies to a person (GDPR in GB and the EU, CASL in CA), so the checks must
know exactly which codes are real, offline and without a dependency. The 249 officially assigned codes are listed;
XK (Kosovo) is accepted as well because Shopify and Stripe use it. COUNTRY_NAMES maps the English names vendors export
(Klaviyo stores "United States", not "US") to the code, lower-cased and accent-free.

    >>> from engine.data.iso import country_code
    >>> country_code("United Kingdom"), country_code("us"), country_code("Atlantis")
    ('GB', 'US', None)
"""
from __future__ import annotations

import unicodedata
from typing import Dict, FrozenSet, Optional

COUNTRY_CODES: FrozenSet[str] = frozenset("""
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT
    BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH
    ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT
    HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS
    LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI
    NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG
    SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG
    UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split()) | {"XK"}

COUNTRY_NAMES: Dict[str, str] = {
    "afghanistan": "AF", "aland islands": "AX", "albania": "AL", "algeria": "DZ", "america": "US",
    "american samoa": "AS", "andorra": "AD", "angola": "AO", "anguilla": "AI", "antarctica": "AQ",
    "antigua & barbuda": "AG", "argentina": "AR", "armenia": "AM", "aruba": "AW", "australia": "AU", "austria": "AT",
    "azerbaijan": "AZ", "bahamas": "BS", "bahrain": "BH", "bangladesh": "BD", "barbados": "BB", "belarus": "BY",
    "belgium": "BE", "belize": "BZ", "benin": "BJ", "bermuda": "BM", "bhutan": "BT", "bolivia": "BO",
    "bosnia & herzegovina": "BA", "botswana": "BW", "bouvet island": "BV", "brazil": "BR", "britain": "GB",
    "british indian ocean territory": "IO", "british virgin islands": "VG", "brunei": "BN", "bulgaria": "BG",
    "burkina faso": "BF", "burma": "MM", "burundi": "BI", "cambodia": "KH", "cameroon": "CM", "canada": "CA",
    "cape verde": "CV", "caribbean netherlands": "BQ", "cayman islands": "KY", "central african republic": "CF",
    "chad": "TD", "chile": "CL", "china": "CN", "christmas island": "CX", "cocos (keeling) islands": "CC",
    "colombia": "CO", "comoros": "KM", "congo - brazzaville": "CG", "congo - kinshasa": "CD", "cook islands": "CK",
    "costa rica": "CR", "cote d'ivoire": "CI", "cote divoire": "CI", "croatia": "HR", "cuba": "CU", "curacao": "CW",
    "cyprus": "CY", "czech republic": "CZ", "czechia": "CZ", "denmark": "DK", "djibouti": "DJ", "dominica": "DM",
    "dominican republic": "DO", "east timor": "TL", "ecuador": "EC", "egypt": "EG", "el salvador": "SV",
    "england": "GB", "equatorial guinea": "GQ", "eritrea": "ER", "estonia": "EE", "eswatini": "SZ", "ethiopia": "ET",
    "falkland islands": "FK", "faroe islands": "FO", "fiji": "FJ", "finland": "FI", "france": "FR",
    "french guiana": "GF", "french polynesia": "PF", "french southern territories": "TF", "gabon": "GA",
    "gambia": "GM", "georgia": "GE", "germany": "DE", "ghana": "GH", "gibraltar": "GI", "great britain": "GB",
    "greece": "GR", "greenland": "GL", "grenada": "GD", "guadeloupe": "GP", "guam": "GU", "guatemala": "GT",
    "guernsey": "GG", "guinea": "GN", "guinea-bissau": "GW", "guyana": "GY", "haiti": "HT",
    "heard & mcdonald islands": "HM", "holland": "NL", "honduras": "HN", "hong kong": "HK",
    "hong kong sar china": "HK", "hungary": "HU", "iceland": "IS", "india": "IN", "indonesia": "ID", "iran": "IR",
    "iran, islamic republic of": "IR", "iraq": "IQ", "ireland": "IE", "isle of man": "IM", "israel": "IL",
    "italy": "IT", "ivory coast": "CI", "jamaica": "JM", "japan": "JP", "jersey": "JE", "jordan": "JO",
    "kazakhstan": "KZ", "kenya": "KE", "kiribati": "KI", "korea": "KR", "korea, republic of": "KR", "kuwait": "KW",
    "kyrgyzstan": "KG", "laos": "LA", "latvia": "LV", "lebanon": "LB", "lesotho": "LS", "liberia": "LR",
    "libya": "LY", "liechtenstein": "LI", "lithuania": "LT", "luxembourg": "LU", "macao": "MO",
    "macao sar china": "MO", "macau": "MO", "macedonia": "MK", "madagascar": "MG", "malawi": "MW", "malaysia": "MY",
    "maldives": "MV", "mali": "ML", "malta": "MT", "marshall islands": "MH", "martinique": "MQ", "mauritania": "MR",
    "mauritius": "MU", "mayotte": "YT", "mexico": "MX", "micronesia": "FM", "moldova": "MD",
    "moldova, republic of": "MD", "monaco": "MC", "mongolia": "MN", "montenegro": "ME", "montserrat": "MS",
    "morocco": "MA", "mozambique": "MZ", "myanmar (burma)": "MM", "namibia": "NA", "nauru": "NR", "nepal": "NP",
    "netherlands": "NL", "new caledonia": "NC", "new zealand": "NZ", "nicaragua": "NI", "niger": "NE",
    "nigeria": "NG", "niue": "NU", "norfolk island": "NF", "north korea": "KP", "north macedonia": "MK",
    "northern ireland": "GB", "northern mariana islands": "MP", "norway": "NO", "oman": "OM", "pakistan": "PK",
    "palau": "PW", "palestine": "PS", "palestinian territories": "PS", "panama": "PA", "papua new guinea": "PG",
    "paraguay": "PY", "peru": "PE", "philippines": "PH", "pitcairn islands": "PN", "poland": "PL", "portugal": "PT",
    "puerto rico": "PR", "qatar": "QA", "republic of korea": "KR", "reunion": "RE", "romania": "RO", "russia": "RU",
    "russian federation": "RU", "rwanda": "RW", "samoa": "WS", "san marino": "SM", "sao tome & principe": "ST",
    "saudi arabia": "SA", "scotland": "GB", "senegal": "SN", "serbia": "RS", "seychelles": "SC",
    "sierra leone": "SL", "singapore": "SG", "sint maarten": "SX", "slovakia": "SK", "slovenia": "SI",
    "solomon islands": "SB", "somalia": "SO", "south africa": "ZA", "south georgia & south sandwich islands": "GS",
    "south korea": "KR", "south sudan": "SS", "spain": "ES", "sri lanka": "LK", "st lucia": "LC",
    "st. barthelemy": "BL", "st. helena": "SH", "st. kitts & nevis": "KN", "st. lucia": "LC", "st. martin": "MF",
    "st. pierre & miquelon": "PM", "st. vincent & grenadines": "VC", "sudan": "SD", "suriname": "SR",
    "svalbard & jan mayen": "SJ", "swaziland": "SZ", "sweden": "SE", "switzerland": "CH", "syria": "SY",
    "taiwan": "TW", "taiwan, province of china": "TW", "tajikistan": "TJ", "tanzania": "TZ", "thailand": "TH",
    "the netherlands": "NL", "timor-leste": "TL", "togo": "TG", "tokelau": "TK", "tonga": "TO",
    "trinidad & tobago": "TT", "tunisia": "TN", "turkey": "TR", "turkiye": "TR", "turkmenistan": "TM",
    "turks & caicos islands": "TC", "tuvalu": "TV", "u.k.": "GB", "u.s.": "US", "u.s. outlying islands": "UM",
    "u.s. virgin islands": "VI", "u.s.a.": "US", "uae": "AE", "uganda": "UG", "uk": "GB", "ukraine": "UA",
    "united arab emirates": "AE", "united kingdom": "GB", "united states": "US", "united states of america": "US",
    "uruguay": "UY", "us": "US", "usa": "US", "uzbekistan": "UZ", "vanuatu": "VU", "vatican": "VA",
    "vatican city": "VA", "venezuela": "VE", "viet nam": "VN", "vietnam": "VN", "wales": "GB",
    "wallis & futuna": "WF", "western sahara": "EH", "yemen": "YE", "zambia": "ZM", "zimbabwe": "ZW",
}


def country_code(value: Optional[str]) -> Optional[str]:
    """The alpha-2 code for a code or an English country name, or None when it is not one."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if len(raw) == 2 and raw.upper() in COUNTRY_CODES:
        return raw.upper()
    key = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode().lower().replace("\u2019", "'")
    key = " ".join(key.split())
    return COUNTRY_NAMES.get(key)
