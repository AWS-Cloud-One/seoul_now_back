"""Explainable origin matching. Provider order only breaks semantic ties."""

import re
import unicodedata
from dataclasses import dataclass

from app.schemas.origin import OriginCandidate, OriginHint, OriginMatch

# Province aliases normalize user/provider text, never establish service coverage.
PROVINCES = {
    "서울특별시": ("서울", "서울시", "서울특별시"),
    "인천광역시": ("인천", "인천시", "인천광역시"),
    "경기도": ("경기", "경기도"),
    "부산광역시": ("부산", "부산시", "부산광역시"),
    "대구광역시": ("대구", "대구시", "대구광역시"),
    "대전광역시": ("대전", "대전시", "대전광역시"),
    "광주광역시": ("광주광역시",),
    "울산광역시": ("울산", "울산시", "울산광역시"),
    "세종특별자치시": ("세종", "세종시", "세종특별자치시"),
    "강원특별자치도": ("강원", "강원도", "강원특별자치도"),
    "충청북도": ("충북", "충청북도"),
    "충청남도": ("충남", "충청남도"),
    "전북특별자치도": ("전북", "전라북도", "전북특별자치도"),
    "전라남도": ("전남", "전라남도"),
    "경상북도": ("경북", "경상북도"),
    "경상남도": ("경남", "경상남도"),
    "제주특별자치도": ("제주", "제주도", "제주특별자치도"),
}
ALIASES = {alias: province for province, aliases in PROVINCES.items() for alias in aliases}
# Kakao Local CategoryGroupCode: https://developers.kakao.com/docs/ko/local/dev-guide
PLACE_TYPES = {
    "SW8": ("지하철역", "전철역", "역"),
    "CE7": ("카페", "커피숍"),
    "FD6": ("음식점", "식당"),
    "HP8": ("병원",),
    "BK9": ("은행",),
    "AD5": ("숙박", "호텔"),
    "SC4": ("학교",),
    "PK6": ("주차장",),
    "PM9": ("약국",),
}


def compact(value):
    return "".join(unicodedata.normalize("NFC", value).casefold().split())


def address_text(value):
    words = unicodedata.normalize("NFC", value).split()
    if words:
        words[0] = ALIASES.get(words[0], words[0])
    return compact(" ".join(words))


def province(value):
    words = value.split()
    return ALIASES.get(words[0]) if words else None


def contains_address(hint, address):
    needle, haystack = address_text(hint), address_text(address)
    hint_words, address_words = hint.split(), address.split()
    if hint_words and re.search(r"[시도군구동읍면]$", hint_words[0]):
        first = address_text(hint_words[0])
        if first not in {address_text(word) for word in address_words}:
            return False
    if len(hint_words) == 1 and re.search(r"[시도군구동읍면]$", hint):
        return needle in {address_text(word) for word in address_words}
    # Building number 110 must not match 1100; a line number 1 must not match 11.
    suffix = r"(?!\d)" if needle[-1:].isdigit() else ""
    return bool(needle and re.search(re.escape(needle) + suffix, haystack))


def type_code(value):
    return next((code for code, names in PLACE_TYPES.items() if value in names), None)


def is_road_address(value):
    # Legal-dong names such as 태평로1가 are not road/building-number addresses.
    return bool(re.search(r"(?:로|길)(?:\s*\d+(?!\d|가)|$)", value))


def effective_hints(name, hints):
    hints = list(hints)
    first = name.split()[0]
    if first in ALIASES and not any(h.kind == "region" and h.source == "message" for h in hints):
        hints.append(OriginHint(kind="region", text=first, source="message", quote=name))
    explicit_kinds = {h.kind for h in hints if h.source == "message"}
    if explicit_kinds & {"region", "address"}:
        explicit_kinds.update({"region", "address"})
    if compact(name).endswith("역") or "카페" in name:
        explicit_kinds.add("place_type")
    result = []
    for kind in ("region", "address", "station_line", "branch", "place_type"):
        values = [h for h in hints if h.kind == kind]
        explicit = [h for h in values if h.source == "message"]
        if kind in explicit_kinds:
            values = explicit
        else:
            recent = [h for h in values if h.source == "recent_user_message"]
            if recent:
                latest = max(h.message_index for h in recent)
                values = [h for h in recent if h.message_index == latest]
        seen = set()
        for hint in values:
            key = address_text(hint.text)
            if key not in seen:
                result.append(hint)
                seen.add(key)
    return tuple(result)


@dataclass(frozen=True)
class OriginSearchIntent:
    name: str
    hints: tuple[OriginHint, ...]

    @property
    def category(self):
        typed = [h for h in self.hints if h.kind == "place_type"]
        # A new, named station must not inherit an old cafe's type.
        explicit = [h for h in typed if h.source == "message"]
        if explicit:
            return type_code(explicit[0].text)
        base = self.base_name()
        if compact(base).endswith("역"):
            return "SW8"
        if "카페" in base:
            return "CE7"
        return type_code(typed[0].text) if typed else None

    @property
    def conflicting(self):
        explicit = [h for h in self.hints if h.source == "message"]
        provinces = {province(h.text) for h in explicit if h.kind in {"region", "address"}}
        types = {type_code(h.text) for h in explicit if h.kind == "place_type"}
        return len(provinces - {None}) > 1 or len(types - {None}) > 1

    def base_name(self):
        value = self.name
        for hint in self.hints:
            if hint.kind in {"region", "address"} and value.startswith(hint.text + " "):
                value = value[len(hint.text) :].strip() or self.name
            if hint.kind == "station_line" and value.endswith(" " + hint.text):
                value = value[: -len(hint.text)].strip() or self.name
        return value

    def query(self):
        parts = []
        for kind in ("region", "address"):
            for hint in self.hints:
                if hint.kind == kind and address_text(hint.text) not in address_text(self.name):
                    if not any(address_text(hint.text) in address_text(p) for p in parts):
                        parts.append(hint.text)
        parts.append(self.name)
        for hint in self.hints:
            if hint.kind not in {"region", "address"} and compact(hint.text) not in compact(
                " ".join(parts)
            ):
                parts.append(hint.text)
        return " ".join(parts)

    def address_query(self):
        addresses = [h.text for h in self.hints if h.kind == "address"]
        return addresses[0] if addresses else self.query()

    @property
    def is_address(self):
        return any(h.kind == "address" for h in self.hints) or bool(
            re.search(r"(?:로|길)\s*\d|(?:동|읍|면)\s+\d", self.name)
        )


def hint_match(hint, candidate):
    location = candidate.location
    addresses = [a for a in (candidate.land_address, candidate.road_address, location.address) if a]
    if hint.kind in {"region", "address"}:
        verified = candidate.region_addresses
        expected = province(hint.text)
        actual = {province(a) for a in (verified or addresses)} - {None}
        if expected and actual and expected not in actual:
            return "conflict"
        facts = verified if hint.kind == "region" and verified else addresses
        if any(contains_address(hint.text, fact) for fact in facts):
            return "match"
        # Missing dong in a road address (or district in a province-only lookup)
        # is absence, not a conflicting administrative area.
        if hint.kind == "region":
            for word in hint.text.split():
                if province(word):
                    continue
                unit = re.search(r"[시군구동읍면리]$", word)
                if unit is None:
                    continue
                comparable = [
                    part
                    for fact in facts
                    for part in fact.split()
                    if not province(part) and part.endswith(unit.group())
                ]
                if comparable and word not in comparable:
                    return "conflict"
            return "unknown"
        if is_road_address(hint.text):
            facts = [fact for fact in facts if is_road_address(fact)]
        elif re.search(r"(?:동|읍|면|리|가)\s*\d", hint.text):
            facts = [fact for fact in facts if not is_road_address(fact)]
        if facts and any(len(fact.split()) >= len(hint.text.split()) for fact in facts):
            return "conflict"
        return "unknown"
    if hint.kind == "station_line":
        fact = " ".join(filter(None, (location.label, candidate.category_name)))
        if re.search(r"(?<!\d)" + re.escape(compact(hint.text)) + r"(?!\d)", compact(fact)):
            return "match"
        return "conflict" if re.search(r"\d+호선", fact) else "unknown"
    if hint.kind == "branch":
        if compact(hint.text) in compact(location.label):
            return "match"
        return "conflict" if re.search(r"\S+점(?:\s|$)", location.label) else "unknown"
    expected = type_code(hint.text)
    if expected and candidate.category_code:
        return "match" if candidate.category_code == expected else "conflict"
    if candidate.category_name and compact(hint.text) in compact(candidate.category_name):
        return "match"
    return "unknown"


def assess(candidate: OriginCandidate, intent: OriginSearchIntent):
    label, wanted = candidate.location.label, intent.base_name()
    # A station's supplied line suffix is presentation detail, not another station name.
    station_name = (
        re.sub(r"(역)\s*\S*선$", r"\1", label) if candidate.category_code == "SW8" else label
    )
    if label == wanted:
        name_match = "exact"
    elif compact(station_name) == compact(wanted):
        name_match = "normalized"
    elif compact(wanted) in compact(label) or compact(label) in compact(wanted):
        name_match = "partial"
    else:
        name_match = "unknown"
    match = OriginMatch(name=name_match)
    if intent.category and candidate.category_code:
        match.place_type = "match" if intent.category == candidate.category_code else "conflict"
    for hint in intent.hints:
        state = hint_match(hint, candidate)
        if state == "match":
            target = match.explicit_matches if hint.source == "message" else match.context_matches
            if hint.kind not in target:
                target.append(hint.kind)
        elif state == "conflict" and hint.source == "message" and hint.kind not in match.conflicts:
            match.conflicts.append(hint.kind)
    return candidate.model_copy(update={"match": match})


def semantic_key(candidate):
    match = candidate.match
    return (
        {"exact": 0, "normalized": 0, "partial": 1, "unknown": 2}[match.name],
        {"match": 0, "unknown": 1, "conflict": 2}[match.place_type],
        -len(match.explicit_matches),
        -len(match.context_matches),
    )


def order_key(candidate):
    return (*semantic_key(candidate), candidate.provider_order, candidate.candidate_id)


def match_reasons(candidate):
    match, reasons = candidate.match, []
    if match.name in {"exact", "normalized"}:
        reasons.append("name_" + match.name)
    if match.place_type == "match":
        reasons.append("place_type")
    names = {"region": "explicit_region", "address": "explicit_address"}
    for kind in match.explicit_matches:
        reason = names.get(kind, kind)
        if reason not in reasons:
            reasons.append(reason)
    if match.context_matches:
        reasons.append("origin_context")
    return reasons
