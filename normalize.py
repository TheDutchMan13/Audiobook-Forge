"""Text clean-up applied just before speech, so the narrator says what a person would say.

Kokoro's own text processor (misaki) already reads plain numbers, years ("1984"), ordinals ("21st"),
simple money ("$5.20") and percentages. This module only fills its gaps:

  clean_clutter    footnote markers, URLs, stray reference numbers
  normalize_text   money with "million/billion", dates, times, ranges, fractions, units, decades,
                   Roman numerals, titles and common abbreviations
  fix_heteronyms   words spelled alike but said differently where grammar alone cannot decide
                   (lead the metal, bass the instrument, a row = an argument, Polish the nationality)
  is_caption       paragraphs that are figure or table captions, which are not read aloud
  sanitize         always on: ligatures (ﬁ ﬂ), invisible characters, soft hyphens, odd spaces, -- dashes
  fix_shouting     ALL-CAPS words are read as words ("STOP RIGHT THERE"), real acronyms stay spelled (FBI)
"""
import re

VERSION = "2"   # part of the cache fingerprint: bump when the output of this module changes

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]
MONTH_RE = "|".join(MONTHS + ["Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Sept", "Oct", "Nov", "Dec"])
MONTH_FULL = {m[:3]: m for m in MONTHS}
MONTH_FULL["Sep"] = "September"

ORDINAL_WORDS = ["zeroth", "First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth", "Ninth",
                 "Tenth", "Eleventh", "Twelfth", "Thirteenth", "Fourteenth", "Fifteenth", "Sixteenth",
                 "Seventeenth", "Eighteenth", "Nineteenth", "Twentieth", "Twenty-First", "Twenty-Second",
                 "Twenty-Third", "Twenty-Fourth", "Twenty-Fifth", "Twenty-Sixth", "Twenty-Seventh",
                 "Twenty-Eighth", "Twenty-Ninth", "Thirtieth"]
CARDINAL_WORDS = ["zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"]
DECADES = {"00": "two thousands", "10": "twenty-tens", "20": "twenties", "30": "thirties", "40": "forties",
           "50": "fifties", "60": "sixties", "70": "seventies", "80": "eighties", "90": "nineties"}
FRACTIONS = {"1/2": "one half", "1/3": "one third", "2/3": "two thirds", "1/4": "one quarter",
             "3/4": "three quarters", "1/8": "one eighth", "3/8": "three eighths", "5/8": "five eighths",
             "7/8": "seven eighths", "1/5": "one fifth", "2/5": "two fifths", "1/10": "one tenth"}
UNICODE_FRACTIONS = {"½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4",
                     "⅛": "1/8", "⅕": "1/5"}
UNITS = {  # abbreviation: (singular, plural); only expanded right after a number
    "km": ("kilometer", "kilometers"), "cm": ("centimeter", "centimeters"), "mm": ("millimeter", "millimeters"),
    "kg": ("kilogram", "kilograms"), "mg": ("milligram", "milligrams"), "lb": ("pound", "pounds"),
    "lbs": ("pound", "pounds"), "oz": ("ounce", "ounces"), "ft": ("foot", "feet"), "mph": ("mile per hour", "miles per hour"),
    "km/h": ("kilometer per hour", "kilometers per hour"), "kph": ("kilometer per hour", "kilometers per hour"),
    "ml": ("milliliter", "milliliters"), "mL": ("milliliter", "milliliters"), "sq ft": ("square foot", "square feet"),
    "GB": ("gigabyte", "gigabytes"), "MB": ("megabyte", "megabytes"), "TB": ("terabyte", "terabytes"),
    "kW": ("kilowatt", "kilowatts"), "kWh": ("kilowatt hour", "kilowatt hours"), "hrs": ("hour", "hours"),
    "hr": ("hour", "hours"), "mins": ("minute", "minutes"), "min": ("minute", "minutes"), "secs": ("second", "seconds"),
}
MAGNITUDE = {"thousand": "thousand", "k": "thousand", "K": "thousand", "million": "million", "M": "million",
             "m": "million", "mn": "million", "billion": "billion", "B": "billion", "bn": "billion", "b": "billion",
             "trillion": "trillion", "T": "trillion", "tn": "trillion"}
CURRENCY_WORDS = {"$": ("dollar", "dollars"), "£": ("pound", "pounds"), "€": ("euro", "euros"),
                  "¥": ("yen", "yen")}
REGNAL_NAMES = ("Henry|Edward|Elizabeth|Charles|George|Louis|James|William|Richard|Mary|Anne|Victoria|John|Paul|"
                "Pius|Leo|Benedict|Gregory|Innocent|Clement|Philip|Peter|Catherine|Frederick|Ferdinand|Napoleon|"
                "Alexander|Nicholas|Ivan|Edmund|Harold|Stephen|Malcolm|Robert|David|Constantine|Ramesses|Ptolemy|"
                "Amenhotep|Thutmose|Darius|Xerxes|Antiochus|Juan Carlos|Felipe|Carl|Gustav|Haakon|Olaf|Christian|"
                "Frederik|Wilhelm|Otto|Rudolf|Sigismund|Casimir|Sobieski|Selim|Suleiman|Mehmed|Murad")
NUMBERED_WORDS = "Chapter|Part|Book|Volume|Vol\\.|Act|Scene|Section|Phase|Episode|Season|Article|Appendix|Canto|Stage|Level"
ROMAN_RE = r"(?:X{0,3})(?:IX|IV|V?I{0,3})"


def roman_to_int(s):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    total, prev = 0, 0
    for ch in reversed(s.upper()):
        v = vals.get(ch, 0)
        total += -v if v < prev else v
        prev = max(prev, v)
    return total


def ordinal_suffix(n):
    n = int(n)
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


# ------------------------------------------------------------------------------------------ clutter
def clean_clutter(text):
    """Remove things a narrator would skip: footnote markers, links, reference numbers."""
    text = re.sub(r"https?://\S+|www\.[A-Za-z0-9-]+\.\S+", "", text)            # web addresses
    text = re.sub(r"\[\d{1,3}(?:[,–-]\s?\d{1,3})*\]", "", text)             # [12] [3, 4] [5-7]
    text = re.sub(r"\[(?:[a-z]|[ivx]{1,4}|note \d+|citation needed)\]", "", text, flags=re.I)
    # a raised 2 or 3 after a short symbol or unit is a power: mc\u00b2, km\u00b2, x\u00b3
    text = re.sub(r"(?<![A-Za-z])([A-Za-z]{1,3})\u00b2(?![\u00b9\u00b2\u00b3\u2070\u2074-\u2079\d])", r"\1 squared", text)
    text = re.sub(r"(?<![A-Za-z])([A-Za-z]{1,3})\u00b3(?![\u00b9\u00b2\u00b3\u2070\u2074-\u2079\d])", r"\1 cubed", text)
    text = re.sub(r"[¹²³⁰⁴-⁹]+", "", text)            # other raised digits are footnote markers
    text = re.sub(r"(?<=[A-Za-z’')])[*†‡§]+(?=[\s.,;:!?]|$)", "", text)  # word* word†
    # a footnote number glued to the end of a sentence: "...the end.12 Next" -> "...the end. Next"
    text = re.sub(r"(?<=[a-z][.,;:!?\"”’])\d{1,3}(?=\s+[A-Z“\"]|\s*$)", "", text)
    text = re.sub(r"\(\s*\)", "", text)                                            # "( )" left behind
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    return text.strip()


CAPTION_RE = re.compile(r"^(figure|fig\.|table|photo|image|illustration|chart|map|source|credit|caption)\s*"
                        r"(\d+[\d.\-]*|[ivx]+)?\s*[:.—–-]", re.I)


def is_caption(paragraph):
    return bool(CAPTION_RE.match(paragraph.strip())) and len(paragraph.split()) <= 45


# ------------------------------------------------------------------------------------------ numbers etc.
def _money(m):
    cur, amount, mag = m.group(1), m.group(2), m.group(3)
    unit = CURRENCY_WORDS[cur][1]
    return f"{amount} {MAGNITUDE.get(mag, mag)} {unit}"


def _numeric_date(m):
    a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
    month, day = (a, b) if a <= 12 else (b, a)          # US month/day, unless the first number cannot be a month
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return m.group(0)
    if len(y) == 2:
        y = ("20" if int(y) <= 30 else "19") + y
    return f"{MONTHS[month - 1]} {ordinal_suffix(day)}, {y}"


def _ends_sentence(m):
    """True when the matched text's own full stop also ended the sentence ("at 7 a.m. Then...")."""
    if not m.group(0).endswith("."):
        return False
    after = m.string[m.end():m.end() + 2]
    return not after.strip() or bool(re.match(r"\s[A-Z\u201c\"]", after))


def _time(m):
    h, mins, ampm = int(m.group(1)), m.group(2), (m.group(3) or "")
    letters = re.sub(r"[^ap]", "", ampm.lower())
    suffix = f" {letters[0].upper()}M" if letters else ""
    if suffix and _ends_sentence(m):
        suffix += "."
    if h > 23:
        return m.group(0)
    if mins == "00":
        return f"{h}{suffix}" if suffix else f"{h} o'clock"
    if mins.startswith("0"):
        return f"{h} oh {int(mins)}{suffix}"
    return f"{h} {mins}{suffix}"


def _unit(m):
    num, unit = m.group(1), m.group(2)
    sing, plur = UNITS[unit]
    try:
        one = float(num.replace(",", "")) == 1
    except ValueError:
        one = False
    return f"{num} {sing if one else plur}"


def _abbrev_title(word):
    return lambda m: word + " "


TITLES = {"Mr.": "Mister", "Mrs.": "Missus", "Ms.": "[Ms](/m\u02c8\u026az/)", "Dr.": "Doctor", "Prof.": "Professor", "Gen.": "General",
          "Capt.": "Captain", "Lt.": "Lieutenant", "Sgt.": "Sergeant", "Col.": "Colonel", "Gov.": "Governor",
          "Sen.": "Senator", "Rep.": "Representative", "Rev.": "Reverend", "Pres.": "President", "Adm.": "Admiral",
          "Cpl.": "Corporal", "Maj.": "Major", "Fr.": "Father", "Mt.": "Mount", "Ft.": "Fort", "Hon.": "Honorable",
          "Supt.": "Superintendent", "Insp.": "Inspector", "Det.": "Detective"}
INLINE = [  # (pattern, replacement); applied anywhere
    (r"\bvs\.?(?=\s)", "versus"), (r"\be\.g\.,?", "for example,"), (r"\bi\.e\.,?", "that is,"),
    (r"\bapprox\.", "approximately"), (r"\bcf\.", "compare"), (r"\bet al\.", "and others"),
    (r"\bJr\.", "Junior"), (r"\bSr\.(?=\s|,|$)", "Senior"), (r"\bInc\.", "Incorporated"), (r"\bLtd\.", "Limited"),
    (r"\bCorp\.", "Corporation"), (r"\bDept\.", "Department"), (r"\bUniv\.", "University"),
    (r"\bAve\.", "Avenue"), (r"\bBlvd\.", "Boulevard"), (r"\bRd\.", "Road"),
]


def _etc(m):
    nxt = m.string[m.end():m.end() + 2]
    # keep the full stop when "etc." also ends the sentence
    return "et cetera." if (not nxt.strip() or re.match(r"\s[A-Z“\"]", nxt)) else "et cetera"


def normalize_text(text):
    t = text
    # unicode fractions first, so "1½" -> "1 1/2"
    for ch, frac in UNICODE_FRACTIONS.items():
        t = re.sub(rf"(\d){ch}", rf"\1 {frac}", t)
        t = t.replace(ch, frac)
    # money with a size word: "$3.5M", "$2 billion" -> "3.5 million dollars"
    t = re.sub(r"([$£€¥])\s?(\d[\d,]*(?:\.\d+)?)\s?(thousand|million|billion|trillion|bn|mn|tn|[KkMmBbT])\b", _money, t)
    # dates
    t = re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b", _numeric_date, t)
    t = re.sub(rf"\b({MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!:)",
               lambda m: f"{MONTH_FULL.get(m.group(1)[:3], m.group(1)) if len(m.group(1)) <= 4 else m.group(1)} {ordinal_suffix(m.group(2))}"
               if int(m.group(2)) <= 31 else m.group(0), t)
    t = re.sub(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_RE})\b",
               lambda m: f"the {ordinal_suffix(m.group(1))} of {MONTH_FULL.get(m.group(2)[:3], m.group(2))}"
               if int(m.group(1)) <= 31 else m.group(0), t)
    # times: 3:30 pm, 10:05, 7:00 a.m.
    t = re.sub(r"\b(\d{1,2}):([0-5]\d)(?!:\d)\s?([aApP]\.?\s?[mM]\.?)?(?=\W|$)", _time, t)
    t = re.sub(r"\b(\d{1,2})\s?([aApP])\.?[mM]\.?(?=\W|$)",
               lambda m: f"{m.group(1)} {m.group(2).upper()}M" + ("." if _ends_sentence(m) else ""), t)
    # decades: '90s, the 90s
    t = re.sub(r"[’']([0-9]0)s\b", lambda m: DECADES.get(m.group(1), m.group(0)), t)
    t = re.sub(r"\b(the|early|mid|late|Early|Mid|Late)\s([1-9]0)s\b", lambda m: f"{m.group(1)} {DECADES.get(m.group(2), m.group(2) + 's')}", t)
    # ranges: 1990–1995, pp. 12-15, 10-15 minutes
    t = re.sub(r"\bpp\.\s?(\d+)\s?[-–]\s?(\d+)", r"pages \1 to \2", t)
    t = re.sub(r"\bp\.\s?(\d+)", r"page \1", t)
    t = re.sub(r"(\d)\s?[–—]\s?(\d)", r"\1 to \2", t)
    t = re.sub(r"\b(\d{1,4})-(\d{1,4})(?=\s(?:years?|days?|weeks?|months?|minutes?|hours?|seconds?|percent|people|times|miles|feet|"
               r"inches|pounds|kilometers|meters|degrees|pages|chapters|games|points)\b)", r"\1 to \2", t)
    t = re.sub(r"\b(ages?|aged|between|from|chapters|verses|pages|years)\s(\d{1,4})-(\d{1,4})\b", r"\1 \2 to \3", t, flags=re.I)
    # fractions written with a slash (dates are already gone)
    t = re.sub(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])", lambda m: FRACTIONS.get(m.group(0), f"{m.group(1)} over {m.group(2)}")
               if m.group(0) in FRACTIONS else m.group(0), t)
    # temperatures and units after a number
    t = re.sub(r"(\d)\s?°\s?F\b", r"\1 degrees Fahrenheit", t)
    t = re.sub(r"(\d)\s?°\s?C\b", r"\1 degrees Celsius", t)
    t = re.sub(r"(\d)\s?°", r"\1 degrees", t)
    unit_alt = "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True))
    t = re.sub(rf"(\d[\d,]*(?:\.\d+)?)\s?({unit_alt})(?![A-Za-z/])", _unit, t)
    # symbols
    t = re.sub(r"#\s?(\d)", r"number \1", t)
    t = re.sub(r"\bNo\.\s?(\d)", r"number \1", t)
    t = re.sub(r"\bNos\.\s?(\d)", r"numbers \1", t)
    t = re.sub(r"(\d)\s?[x×]\s?(\d)", r"\1 by \2", t)
    t = re.sub(r"~\s?(\d)", r"about \1", t)
    # Roman numerals: Chapter IV, World War II, Henry VIII
    t = re.sub(rf"\b((?i:{NUMBERED_WORDS}))\s({ROMAN_RE})\b(?![\w'])",
               lambda m: f"{m.group(1)} {roman_to_int(m.group(2))}" if m.group(2) else m.group(0), t)
    t = re.sub(rf"\bWorld War ({ROMAN_RE})\b", lambda m: f"World War {CARDINAL_WORDS[roman_to_int(m.group(1))]}"
               if m.group(1) and roman_to_int(m.group(1)) <= 10 else m.group(0), t)
    t = re.sub(rf"\b({REGNAL_NAMES})\s(X{{0,3}}(?:IX|IV|V?I{{1,3}}|V|X))\b(?=[\s,.;:!?)'’]|$)",
               lambda m: (f"{m.group(1)} the {ORDINAL_WORDS[roman_to_int(m.group(2))]}"
                          if 0 < roman_to_int(m.group(2)) <= 30 and not (m.group(2) == "I" and re.match(r"\s+[a-z]", m.string[m.end():]))
                          else m.group(0)), t)
    # titles before a name: "Dr. Smith" -> "Doctor Smith"; "St." is Saint before a name, Street after one
    for abbr, word in TITLES.items():
        t = re.sub(rf"(?<![A-Za-z]){re.escape(abbr)}\s?(?=[A-Z])", word + " ", t)
        t = re.sub(rf"(?<![A-Za-z]){re.escape(abbr)}(?=\s+(?:and|&)\s+[A-Z])", word, t)   # "Mr. and Mrs. Jones"
    t = re.sub(r"(?<![A-Za-z])St\.\s?(?=[A-Z])", "Saint ", t)
    t = re.sub(r"(?<=[A-Za-z0-9] )St\.(?=[\s,;:)]|$)", "Street", t)
    t = re.sub(r"(?<=[A-Za-z] )Dr\.(?=[\s,;:)]|$)", "Drive", t)
    # "etc." is left alone: Kokoro already says "etcetera"
    for pat, rep in INLINE:
        t = re.sub(pat, rep, t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t


# ------------------------------------------------------------------------------------------ heteronyms
# Kokoro's text processor already picks the right sound for most of these from the grammar (read, wind,
# tear, live, close, object, record, present, wound, dove...). These rules only cover cases where the
# grammar is the same and the meaning decides. "[word](/phonemes/)" is Kokoro's own way to say exactly
# how to pronounce a word.
LEAD_METAL = "[lead](/lˈɛd/)"
BASS_MUSIC = "[bass](/bˈAs/)"
ROW_ARGUMENT = "[row](/ɹˈW/)"
NOT_FISH = r"(?! (?:fish|fishing|boat|lake|pond|river))"
HETERONYM_RULES = [  # (pattern, replacement); \1 keeps a captured lead-in word
    (re.compile(r"\blead(?=[ -](?:pipes?|poisoning|paint|weights?|shot|pencils?|bullets?|sheets?|roof|lined|acid|crystal|"
                r"glass|free|mine|mines|miners?|ore|soldiers?|balloon|piping|content|levels?)\b)", re.I), LEAD_METAL),
    (re.compile(r"\b(of|with|in|heavy as) lead\b(?! (?:the|a|an|us|them|him|her|to|by|from|with|role|singer|character|story)\b)"
                r"(?=[\s.,;:!?)]|$)"), r"\1 " + LEAD_METAL),
    (re.compile(r"\bbass(?=[ -](?:guitars?|players?|guitarists?|lines?|drums?|notes?|clef|voice|singers?|section|amps?|"
                r"amplifiers?|boost|solo|parts?|baritone|register|tones?|frequencies|speakers?)\b)", re.I), BASS_MUSIC),
    (re.compile(r"\b(on|plays|played|playing|double|electric|upright|on the|plays the|played the|playing the) bass\b" + NOT_FISH),
     r"\1 " + BASS_MUSIC),
    (re.compile(r"\b(a|another|big|huge|blazing|terrible|furious|almighty|the) row(?= (?:with|about|over|between|broke out)\b)"),
     r"\1 " + ROW_ARGUMENT),
    (re.compile(r"\b(had|having|has|have) a row\b(?! of| boat| of)"), r"\1 a " + ROW_ARGUMENT),
    (re.compile(r"(?<=[a-z,;] )Polish\b"), "[Polish](/pˈOlɪʃ/)"),   # the nationality, not shoe polish
]


def fix_heteronyms(text):
    for pattern, replacement in HETERONYM_RULES:
        text = pattern.sub(replacement, text)
    return text


def strip_markup(text):
    """For showing processed text to people: '[lead](/lˈɛd/)' -> 'lead'."""
    return re.sub(r"\[([^\]]+)\]\(/[^)]*/\)", r"\1", text)


# ------------------------------------------------------------------------------------------ all together
# ------------------------------------------------------------------------------------ sanitizer
LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st",
             "ﬆ": "st", "Ĳ": "IJ", "ĳ": "ij", "Ǉ": "LJ", "ǉ": "lj", "ǌ": "nj"}
INVISIBLE_RE = re.compile("[­​‌‍⁠᠎﻿\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]")
ODD_SPACE_RE = re.compile("[  -   　\t]")


def sanitize(text):
    """Characters that look fine on a page but trip the voice: ligatures from PDFs, invisible joiners and
    soft hyphens (which split words in two), unusual spaces, and typewriter dashes and quotes."""
    for a, b in LIGATURES.items():
        if a in text:
            text = text.replace(a, b)
    text = INVISIBLE_RE.sub("", text)
    text = ODD_SPACE_RE.sub(" ", text)
    text = text.replace("‐", "-").replace("‑", "-").replace("―", "—").replace("‒", "–")
    text = re.sub(r"(?<=\w)\s*-{2,3}\s*(?=\w)", " — ", text)       # word--word  -> word — word
    text = re.sub(r"\s+-{2,3}\s+", " — ", text)                     # word -- word
    text = re.sub(r"-{2,3}(?=[\s\"”’')]|$)", "—", text)  # "But--"  (speech cut off)
    text = re.sub(r"``(.+?)''", "“\\1”", text)                # ``old style'' quotes
    text = re.sub(r"\.{4,}", "…", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


# ------------------------------------------------------------------------------------ shouting
SHOUT_RE = re.compile(r"(?<![\w'’])([A-Z][A-Z'’]*[A-Z])(?![\w'’])")
MARKUP_RE = re.compile(r"\[[^\]]+\]\(/[^)]*/\)")
KEEP_CAPS = {"AM", "PM", "BC", "AD", "BCE", "CE", "TV", "OK", "ID", "UK", "US", "USA", "EU", "UN", "DC", "LA", "NY",
             "PS", "PhD", "DNA", "CEO", "FBI", "CIA", "NASA", "NATO", "IT", "AI", "OS", "PC", "CD", "DVD", "VIP"}
_LEXICON = None


def _lexicon():
    """Kokoro's own dictionary (misaki), or False when it is not installed (then simple rules are used)."""
    global _LEXICON
    if _LEXICON is None:
        try:
            import warnings
            warnings.filterwarnings("ignore")
            from misaki import en
            lex = en.Lexicon(False)
            ctx = en.TokenContext()
            _LEXICON = (lex, ctx)
        except Exception:  # noqa: BLE001
            _LEXICON = False
    return _LEXICON


def known_word(word):
    """True when Kokoro's dictionary has the word (plurals, -ed and -ing forms included)."""
    lx = _lexicon()
    if not lx:
        return None
    lex, ctx = lx
    word = word.replace("\u2019", "'").replace("\u2018", "'")   # the dictionary spells "I'm", books often "I’m"
    for w in (word, word.lower(), word.capitalize()):
        try:
            if w in lex.golds or w in lex.silvers or lex.get_word(w, None, None, ctx)[0]:
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def _shout(m):
    w = m.group(1)
    plain = w.replace("’", "'")
    if w in KEEP_CAPS or len(plain.replace("'", "")) < 2:
        return w
    if w and re.fullmatch(ROMAN_RE, w):
        return w                                    # Part IV, Henry VIII, Louis XVIII (read as numbers later)
    lx = _lexicon()
    if lx and w in lx[0].golds:
        return w                                    # the dictionary already knows how to say it in capitals
    low = w.lower()
    known = known_word(low)
    if known:
        return low
    vowels = re.search(r"[AEIOUY]", w)
    if len(plain) >= 4 and vowels:
        return w.capitalize()                       # KEVIN -> Kevin: a shouted name, said as a word
    return w                                        # FBI, MCU, HTML: spelled out letter by letter


def fix_shouting(text):
    """Read ALL-CAPS words as words. Kokoro spells out capitals it does not know, so "STUDIOS" became
    "S-T-U-D-I-O-S". Acronyms it knows (NASA) or that cannot be words (FBI, HTML) are left alone."""
    if not re.search(r"[A-Z]{2}", text):
        return text
    out, pos = [], 0
    for m in MARKUP_RE.finditer(text):              # never touch [word](/phonemes/) fixes
        out.append(SHOUT_RE.sub(_shout, text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(SHOUT_RE.sub(_shout, text[pos:]))
    return "".join(out)


def prepare(text, options=None, rules=None):
    """Run the enabled steps in order. options: dict with clutter / normalize / heteronyms / shouting booleans.
    rules: optional function applying the person's pronunciation fixes. It runs after the sanitizer and the
    shouting step (so a fix for "Kevin" also catches "KEVIN") and before everything else (so what a fix
    says is never changed). The sanitizer always runs."""
    o = {"clutter": True, "normalize": True, "heteronyms": True, "shouting": True}
    o.update(options or {})
    text = sanitize(text)
    if o["shouting"]:
        text = fix_shouting(text)
    if rules:
        text = rules(text)
    if o["clutter"]:
        text = clean_clutter(text)
    if o["normalize"]:
        text = normalize_text(text)
    if o["heteronyms"]:
        text = fix_heteronyms(text)
    return text
