"""Normalizzazione leggera dei paesi usati da filtri e aggregatori."""

from __future__ import annotations

import unicodedata


def _key(value: object) -> str:
    return unicodedata.normalize("NFKD", str(value or "").strip()).encode("ascii", "ignore").decode().casefold()


# Codice alpha-2 -> nomi inglese/italiano e codice alpha-3. Il catalogo copre
# l'Europa e le destinazioni più comuni nei feed accademici internazionali.
_COUNTRIES: dict[str, tuple[str, ...]] = {
    "AT": ("AUT", "austria"),
    "BE": ("BEL", "belgium", "belgio"),
    "BG": ("BGR", "bulgaria"),
    "CH": ("CHE", "switzerland", "svizzera"),
    "CY": ("CYP", "cyprus", "cipro"),
    "CZ": ("CZE", "czech republic", "czechia", "repubblica ceca", "cechia"),
    "DE": ("DEU", "germany", "germania"),
    "DK": ("DNK", "denmark", "danimarca"),
    "EE": ("EST", "estonia"),
    "ES": ("ESP", "spain", "spagna"),
    "FI": ("FIN", "finland", "finlandia"),
    "FR": ("FRA", "france", "francia"),
    "GB": ("GBR", "united kingdom", "great britain", "uk", "regno unito", "gran bretagna"),
    "GR": ("GRC", "greece", "grecia"),
    "HR": ("HRV", "croatia", "croazia"),
    "HU": ("HUN", "hungary", "ungheria"),
    "IE": ("IRL", "ireland", "irlanda"),
    "IS": ("ISL", "iceland", "islanda"),
    "IT": ("ITA", "italy", "italia"),
    "LI": ("LIE", "liechtenstein"),
    "LT": ("LTU", "lithuania", "lituania"),
    "LU": ("LUX", "luxembourg", "lussemburgo"),
    "LV": ("LVA", "latvia", "lettonia"),
    "MT": ("MLT", "malta"),
    "NL": ("NLD", "netherlands", "the netherlands", "holland", "paesi bassi", "olanda"),
    "NO": ("NOR", "norway", "norvegia"),
    "PL": ("POL", "poland", "polonia"),
    "PT": ("PRT", "portugal", "portogallo"),
    "RO": ("ROU", "romania"),
    "SE": ("SWE", "sweden", "svezia"),
    "SI": ("SVN", "slovenia"),
    "SK": ("SVK", "slovakia", "slovacchia"),
    "AL": ("ALB", "albania"),
    "BA": ("BIH", "bosnia and herzegovina", "bosnia erzegovina"),
    "ME": ("MNE", "montenegro"),
    "MK": ("MKD", "north macedonia", "macedonia del nord"),
    "RS": ("SRB", "serbia"),
    "TR": ("TUR", "turkey", "turkiye", "turchia"),
    "US": ("USA", "united states", "united states of america", "stati uniti"),
    "CA": ("CAN", "canada"),
    "AU": ("AUS", "australia"),
    "NZ": ("NZL", "new zealand", "nuova zelanda"),
}

_ALIASES = {alias.casefold(): code for code, aliases in _COUNTRIES.items() for alias in (code, *aliases)}


def country_code(value: object) -> str | None:
    """Restituisce ISO alpha-2 accettando maiuscole, alpha-3 e nomi EN/IT."""
    key = _key(value)
    if len(key) == 2 and key.isalpha():
        return key.upper()
    return _ALIASES.get(key)
