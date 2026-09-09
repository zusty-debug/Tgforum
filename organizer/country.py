"""Evidence-based country detection.

Language != country, and a single weak token is never enough to name a
topic after a country. Signals are combined with a confidence score:

  alias in full name        0.95   ("united kingdom")
  long single-word alias    0.95   ("india", "germany")
  short code token          0.80   ("uk", "de") — 'in' is only 0.60
  domain TLD in dotted tok  0.70   ("eda.yandex.ru" -> ru)
  city name                 0.55   ("almaty" — geographic, not proof alone)
  script / language         0.35   (supporting evidence only)

Two independent signals on the SAME country boost confidence (+0.08).
Two plausible countries close together Dampen it (ambiguity).
"""
from __future__ import annotations

import re
import unicodedata

from .models import CountryResult

# (iso, name, aliases, tlds, cities)
COUNTRIES: list[tuple[str, str, list[str], list[str], list[str]]] = [
    ("in", "India", ["india"], ["in"], ["mumbai", "delhi", "bangalore", "hyderabad", "pune", "chennai", "kolkata"]),
    ("gb", "United Kingdom", ["united kingdom", "uk", "great britain", "england"], ["uk", "co.uk"], ["london", "manchester", "birmingham", "leeds", "newcastle"]),
    ("us", "United States", ["united states", "usa", "u.s.a", "u.s", "america"], ["us"], ["new york", "los angeles", "chicago", "houston", "miami", "texas", "california", "florida"]),
    ("ru", "Russia", ["russia", "russian"], ["ru"], ["moscow", "saint petersburg", "spb", "novosibirsk", "ekaterinburg", "kazan"]),
    ("kz", "Kazakhstan", ["kazakhstan", "kazakh"], ["kz"], ["almaty", "astana", "karaganda", "shymkent", "aktobe"]),
    ("uz", "Uzbekistan", ["uzbekistan", "uzbek"], ["uz"], ["tashkent", "samarkand", "bukhara", "fergana"]),
    ("de", "Germany", ["germany", "german"], ["de"], ["berlin", "munich", "hamburg", "cologne", "frankfurt"]),
    ("fr", "France", ["france", "french"], ["fr"], ["paris", "lyon", "marseille", "bordeaux"]),
    ("es", "Spain", ["spain", "spanish"], ["es"], ["madrid", "barcelona", "seville", "valencia"]),
    ("it", "Italy", ["italy", "italian"], ["it"], ["rome", "milan", "naples", "turin"]),
    ("br", "Brazil", ["brazil", "brazilian"], ["br"], ["sao paulo", "rio de janeiro", "brazilia", "salvador"]),
    ("mx", "Mexico", ["mexico", "mexican"], ["mx"], ["mexico city", "guadalajara", "monterrey"]),
    ("ca", "Canada", ["canada", "canadian"], ["ca", "co.ca"], ["toronto", "vancouver", "montreal", "calgary"]),
    ("au", "Australia", ["australia", "australian"], ["au", "com.au"], ["sydney", "melbourne", "brisbane", "perth"]),
    ("nz", "New Zealand", ["new zealand"], ["nz"], ["auckland", "wellington", "christchurch"]),
    ("za", "South Africa", ["south africa"], ["za"], ["johannesburg", "cape town", "durban"]),
    ("ng", "Nigeria", ["nigeria", "nigerian"], ["ng"], ["lagos", "abuja", "kano"]),
    ("eg", "Egypt", ["egypt", "egyptian"], ["eg"], ["cairo", "alexandria"]),
    ("ar", "Argentina", ["argentina", "argentinian"], ["ar"], ["buenos aires", "cordoba", "rosario"]),
    ("cl", "Chile", ["chile", "chilean"], ["cl"], ["santiago", "valparaiso"]),
    ("co", "Colombia", ["colombia", "colombian"], ["co"], ["bogota", "medellin", "cali"]),
    ("pe", "Peru", ["peru", "peruvian"], ["pe"], ["lima", "arequipa"]),
    ("ve", "Venezuela", ["venezuela", "venezuelan"], ["ve"], ["caracas", "maracaibo"]),
    ("ec", "Ecuador", ["ecuador", "ecuadorian"], ["ec"], ["quito", "guayaquil"]),
    ("bo", "Bolivia", ["bolivia", "bolivian"], ["bo"], ["la paz", "sucre"]),
    ("uy", "Uruguay", ["uruguay", "uruguayan"], ["uy"], ["montevideo"]),
    ("py", "Paraguay", ["paraguay", "paraguayan"], ["py"], ["asuncion"]),
    ("tr", "Turkey", ["turkey", "turkish", "turkiye"], ["tr"], ["istanbul", "ankara", "izmir"]),
    ("il", "Israel", ["israel", "israeli"], ["il"], ["tel aviv", "jerusalem", "haifa"]),
    ("sa", "Saudi Arabia", ["saudi arabia", "saudi"], ["sa"], ["riyadh", "jeddah", "dammam"]),
    ("ae", "United Arab Emirates", ["united arab emirates", "uae", "emirates"], ["ae"], ["dubai", "abudhabi", "sharjah"]),
    ("jp", "Japan", ["japan", "japanese"], ["jp"], ["tokyo", "osaka", "kyoto", "yokohama"]),
    ("kr", "South Korea", ["south korea", "korea", "korean"], ["kr", "co.kr"], ["seoul", "busan", "incheon"]),
    ("sg", "Singapore", ["singapore", "singaporean"], ["sg"], []),
    ("my", "Malaysia", ["malaysia", "malaysian"], ["my"], ["kuala lumpur", "penang"]),
    ("id", "Indonesia", ["indonesia", "indonesian"], ["id"], ["jakarta", "surabaya", "bandung"]),
    ("th", "Thailand", ["thailand", "thai"], ["th"], ["bangkok", "chiang mai", "phuket"]),
    ("ph", "Philippines", ["philippines", "filipino", "filipinas"], ["ph"], ["manila", "cebu", "davao"]),
    ("vn", "Vietnam", ["vietnam", "vietnamese"], ["vn"], ["ho chi minh", "hanoi", "da nang"]),
    ("bd", "Bangladesh", ["bangladesh", "bangladeshi"], ["bd"], ["dhaka", "chittagong"]),
    ("pk", "Pakistan", ["pakistan", "pakistani"], ["pk"], ["karachi", "lahore", "islamabad"]),
    ("lk", "Sri Lanka", ["sri lanka", "sri lankan"], ["lk"], ["colombo", "kandy"]),
    ("np", "Nepal", ["nepal", "nepali"], ["np"], ["kathmandu", "pokhara"]),
    ("mm", "Myanmar", ["myanmar", "burma", "burmese"], ["mm"], ["yangon", "mandalay"]),
    ("kh", "Cambodia", ["cambodia", "cambodian"], ["kh"], ["phnom penh", "siem reap"]),
    ("la", "Laos", ["laos", "lao"], ["la"], ["vientiane"]),
    ("mn", "Mongolia", ["mongolia", "mongolian"], ["mn"], ["ulaanbaatar"]),
    ("ua", "Ukraine", ["ukraine", "ukrainian"], ["ua"], ["kyiv", "kiev", "odessa", "lviv"]),
    ("by", "Belarus", ["belarus", "belarusian"], ["by"], ["minsk"]),
    ("pl", "Poland", ["poland", "polish"], ["pl"], ["warsaw", "krakow", "gdansk"]),
    ("cz", "Czech Republic", ["czech", "czechia"], ["cz"], ["prague", "brno"]),
    ("at", "Austria", ["austria", "austrian"], ["at"], ["vienna", "salzburg"]),
    ("ch", "Switzerland", ["switzerland", "swiss"], ["ch"], ["zurich", "geneva", "bern"]),
    ("nl", "Netherlands", ["netherlands", "holland", "dutch"], ["nl"], ["amsterdam", "rotterdam", "the hague"]),
    ("be", "Belgium", ["belgium", "belgian"], ["be"], ["brussels", "antwerp", "gent"]),
    ("se", "Sweden", ["sweden", "swedish"], ["se"], ["stockholm", "gothenburg"]),
    ("no", "Norway", ["norway", "norwegian"], ["no"], ["oslo", "bergen"]),
    ("fi", "Finland", ["finland", "finish"], ["fi"], ["helsinki", "turku"]),
    ("dk", "Denmark", ["denmark", "danish"], ["dk"], ["copenhagen"]),
    ("ie", "Ireland", ["ireland", "irish"], ["ie"], ["dublin", "cork"]),
    ("gr", "Greece", ["greece", "greek"], ["gr"], ["athens", "thessaloniki"]),
    ("pt", "Portugal", ["portugal", "portuguese"], ["pt"], ["lisbon", "porto"]),
    ("hu", "Hungary", ["hungary", "hungarian"], ["hu"], ["budapest"]),
    ("ro", "Romania", ["romania", "romanian"], ["ro"], ["bucharest", "cluj"]),
    ("bg", "Bulgaria", ["bulgaria", "bulgarian"], ["bg"], ["sofia"]),
    ("rs", "Serbia", ["serbia", "serbian"], ["rs"], ["belgrade", "novi sad"]),
    ("hr", "Croatia", ["croatia", "croatian"], ["hr"], ["zagreb", "split"]),
    ("sk", "Slovakia", ["slovakia", "slovak"], ["sk"], ["bratislava"]),
    ("si", "Slovenia", ["slovenia", "slovenian"], ["si"], ["ljubljana"]),
    ("lt", "Lithuania", ["lithuania", "lithuanian"], ["lt"], ["vilnius"]),
    ("lv", "Latvia", ["latvia", "latvian"], ["lv"], ["riga"]),
    ("ee", "Estonia", ["estonia", "estonian"], ["ee"], ["tallinn"]),
    ("md", "Moldova", ["moldova", "moldovan"], ["md"], ["chisinau"]),
    ("am", "Armenia", ["armenia", "armenian"], ["am"], ["yerevan"]),
    ("az", "Azerbaijan", ["azerbaijan", "azerbaijani"], ["az"], ["baku"]),
    ("ge", "Georgia", ["georgia", "georgian"], ["ge"], ["tbilisi", "batumi"]),
    ("jo", "Jordan", ["jordan", "jordanian"], ["jo"], ["amman"]),
    ("lb", "Lebanon", ["lebanon", "lebanese"], ["lb"], ["beirut"]),
    ("iq", "Iraq", ["iraq", "iraqi"], ["iq"], ["baghdad", "basra"]),
    ("ir", "Iran", ["iran", "iranian"], ["ir"], ["tehran", "isfahan"]),
    ("kw", "Kuwait", ["kuwait", "kuwaiti"], ["kw"], ["kuwait city"]),
    ("qa", "Qatar", ["qatar", "qatari"], ["qa"], ["doha"]),
    ("om", "Oman", ["oman", "omani"], ["om"], ["muscat"]),
    ("bh", "Bahrain", ["bahrain", "bahraini"], ["bh"], ["manama"]),
    ("ma", "Morocco", ["morocco", "moroccan"], ["ma"], ["casablanca", "rabat", "marrakesh"]),
    ("dz", "Algeria", ["algeria", "algerian"], ["dz"], ["algiers", "oran"]),
    ("tn", "Tunisia", ["tunisia", "tunisian"], ["tn"], ["tunis", "sousse"]),
    ("sn", "Senegal", ["senegal", "senegalese"], ["sn"], ["dakar"]),
    ("gh", "Ghana", ["ghana", "ghanaian"], ["gh"], ["accra"]),
    ("ke", "Kenya", ["kenya", "kenyan"], ["ke"], ["nairobi", "mombasa"]),
    ("et", "Ethiopia", ["ethiopia", "ethiopian"], ["et"], ["addis ababa"]),
    ("tz", "Tanzania", ["tanzania", "tanzanian"], ["tz"], ["dar es salaam"]),
]

_COUNTRY_BY_CODE = {c[0]: c[1] for c in COUNTRIES}
# Standalone 2-letter tokens that are too risky as country codes.
_WEAK_CODE_WORDS = {"is", "or", "as", "at", "to", "of", "an", "so", "do", "no", "im", "am", "be"}

_CYRILLIC = re.compile(r"[\u0400-\u04FF]")
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_HEBREW = re.compile(r"[\u0590-\u05FF]")


def flag(iso_code: str | None) -> str:
    if not iso_code or len(iso_code) != 2 or not iso_code.isalpha():
        return "🌐"
    return "".join(chr(0x1F1E6 + ord(c.upper()) - 65) for c in iso_code)


def detect(text: str) -> CountryResult:
    """Detect a country from combined filename/caption text."""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    if not folded.strip():
        return CountryResult(None, None, 0.0, ["no text"])

    # "Care_Vision_United_Kingdom" must still match the alias "united kingdom"
    flat = re.sub(r"[_\-\s]+", " ", folded)
    word_tokens = set(re.findall(r"[a-z]{2,}", flat))
    dotted = set(re.findall(r"[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+", folded))

    signals: list[tuple[str, float, str]] = []
    for code, name, aliases, tlds, cities in COUNTRIES:
        hit = None
        for a in aliases:
            if " " in a:
                if a in flat:
                    pre = flat.split(a, 1)[0]
                    if pre.endswith(("south", "north", "central")):
                        continue  # "south america" is not USA
                    hit = (0.95, f"alias '{a}'")
                    break
            else:
                if a in word_tokens and a not in _WEAK_CODE_WORDS:
                    if len(a) >= 4:
                        conf = 0.95
                    elif a == "in":
                        conf = 0.60  # ambiguous English word
                    else:
                        conf = 0.80
                    hit = (conf, f"token '{a}'")
                    break
        if hit:
            signals.append((code, hit[0], hit[1]))
            continue
        tld_hit = None
        for d in dotted:
            for tl in tlds:
                if d.endswith("." + tl):
                    tld_hit = (tl, d)
                    break
            if tld_hit:
                break
        if tld_hit:
            signals.append((code, 0.70, f"TLD '.{tld_hit[0]}' in '{tld_hit[1]}'"))
            continue
        for c in cities:
            if " " in c:
                if c in flat:
                    signals.append((code, 0.60, f"city '{c}'"))
                    break
            elif c in word_tokens:
                signals.append((code, 0.55, f"city '{c}'"))
                break

    # Supporting language/script evidence (never decisive alone).
    if _CYRILLIC.search(folded):
        signals.append(("ru", 0.35, "Cyrillic script"))
    if _DEVANAGARI.search(folded):
        signals.append(("in", 0.35, "Devanagari script"))
    if _HEBREW.search(folded):
        signals.append(("il", 0.40, "Hebrew script"))

    if not signals:
        return CountryResult(None, None, 0.0, ["no country signals found"])

    per: dict[str, dict] = {}
    for code, conf, why in signals:
        e = per.setdefault(code, {"max": 0.0, "reasons": []})
        e["max"] = max(e["max"], conf)
        e["reasons"].append(why)

    ordered = sorted(per.items(), key=lambda kv: kv[1]["max"], reverse=True)
    top_code, top_e = ordered[0]
    conf = top_e["max"]
    reasons = list(top_e["reasons"])

    if len(ordered) > 1:
        second_code, second_e = ordered[1]
        if second_e["max"] >= 0.50 and conf - second_e["max"] < 0.15:
            second_name = _COUNTRY_BY_CODE[second_code]
            conf = min(conf, 0.60)
            reasons.append(f"ambiguous vs {second_name} — confidence dampened")
        elif len(reasons) >= 2:
            conf = min(0.99, conf + 0.08)
            reasons.append("multiple independent signals")

    return CountryResult(top_code, _COUNTRY_BY_CODE[top_code], round(conf, 2), reasons)
