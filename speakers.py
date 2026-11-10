"""Guess whether quoted speech is said by a man or a woman, so it can be read by a matching voice.

Clues, strongest first, all taken from the narration around the quote (never from inside it):
  1. the speech tag: "...," she said / asked Natasha / Tony replied
  2. who the paragraph is about: She smiled. "Hello."  /  Bruce looked up. "Hello."
  3. back-and-forth: in a run of dialogue with no tags, speakers take turns, so a paragraph has the same
     speaker as the one two paragraphs before it
Names learn their gender from the book itself (a name followed by "she" in the same sentence, many
times) and from a short list of common first names and titles. Unknown stays unknown: that speech is
read by the narrator, which is never wrong.

For "each character their own voice", speakers() also works out *who* speaks (Tony, Pepper), with
"she said" resolved to the woman last named, surnames joined to first names ("Stark" -> "Tony" after
"Tony Stark"), and find_characters() / cast() pick a voice per recurring character.
"""
import hashlib
import json
import re
from collections import Counter, defaultdict

QUOTE_RE = re.compile(r"(“[^“”]*”|\"[^\"]*\")")
VERBS = ("said|says|asked|asks|replied|replies|answered|whispered|shouted|yelled|muttered|murmured|cried|called|"
         "added|continued|snapped|began|insisted|told|tells|demanded|exclaimed|admitted|agreed|explained|laughed|"
         "sighed|growled|hissed|breathed|offered|repeated|returned|pressed|warned|went on|corrected|interrupted|"
         "protested|suggested|announced|declared|stammered|managed|responded|retorted|groaned|grunted|mumbled|"
         "screamed|teased|wondered|noted|observed|mused|chuckled|sobbed|whimpered|barked|drawled|countered")
FEMALE_PRON, MALE_PRON = {"she", "her", "hers", "herself"}, {"he", "him", "his", "himself"}
FEMALE_TITLES = {"mrs", "ms", "miss", "lady", "queen", "princess", "aunt", "mother", "mom", "mum", "grandma",
                 "grandmother", "sister", "madam", "madame", "duchess", "countess", "dame", "girl", "woman", "wife",
                 "daughter", "mistress", "empress", "baroness", "nun", "niece", "widow", "ma'am", "mrs.", "ms."}
MALE_TITLES = {"mr", "sir", "lord", "king", "prince", "uncle", "father", "dad", "grandpa", "grandfather", "brother",
               "duke", "count", "boy", "man", "husband", "son", "master", "emperor", "baron", "monk", "nephew",
               "widower", "mr."}
FEMALE_NAMES = set("""mary patricia jennifer linda elizabeth barbara susan jessica sarah karen nancy lisa betty margaret
sandra ashley kimberly emily donna michelle dorothy carol amanda melissa deborah stephanie rebecca sharon laura cynthia
kathleen amy shirley angela helen anna brenda pamela nicole emma samantha katherine christine debra rachel catherine
carolyn janet ruth maria heather diane virginia julie joyce victoria olivia kelly christina lauren joan evelyn judith
megan cheryl andrea hannah martha jacqueline frances gloria ann teresa kathryn sara janice jean alice madison doris
abigail julia judy grace denise amber marilyn beverly danielle theresa sophia marie diana brittany natalie isabella
charlotte rose alexis kayla natasha wanda pepper gamora nebula okoye shuri carol jane peggy hope maya mara clara
eleanor ellie lucy lily chloe zoe ava mia harper ella scarlett aria leah ruby iris violet hazel nora claire bella
jessie fiona irene agnes edith ida hermione ginny luna arya sansa cersei daenerys brienne katniss primrose elsa anna
moana leia rey padme wilma hilda greta ingrid astrid freya sigrid yelena wanda kate jessica trinity sylvia""".split())
MALE_NAMES = set("""james robert john michael david william richard joseph thomas charles christopher daniel matthew
anthony mark donald steven paul andrew joshua kenneth kevin brian george timothy ronald edward jason jeffrey ryan jacob
gary nicholas eric jonathan stephen larry justin scott brandon benjamin samuel gregory alexander frank patrick raymond
jack dennis jerry tyler aaron jose adam nathan henry douglas zachary peter kyle noah ethan jeremy walter christian
keith roger terry austin sean gerald carl harold dylan arthur lawrence jordan jesse bryan billy bruce gabriel joe logan
albert willie alan eugene russell vincent philip bobby johnny bradley roy ralph randy louis harry tony steve thor loki
clint sam bucky nick stephen vision scott t'challa thanos rocket groot drax quill happy rhodey jon favreau feige kevin
tom jim bob ben max leo luke oliver liam lucas mason logan elijah owen caleb hunter ian ivan oscar hugo felix victor
simon theo ron harry draco severus albus sirius remus frodo sam gandalf aragorn legolas gimli boromir bilbo han
anakin obi-wan yoda ned robb bran tyrion jaime jorah sherlock watson hercule""".split())
STOP_CAPS = {"the", "a", "an", "and", "but", "or", "so", "then", "when", "while", "if", "as", "at", "in", "on", "of",
             "it", "its", "he", "she", "they", "we", "you", "i", "his", "her", "their", "our", "my", "your", "this",
             "that", "there", "here", "what", "who", "why", "how", "where", "no", "yes", "oh", "well", "now", "just",
             "still", "even", "after", "before", "for", "with", "from", "to", "by", "not", "all", "one", "some",
             "chapter", "mister", "missus", "doctor", "saint",
             "like", "again", "then", "still", "even", "maybe", "perhaps", "never", "always", "once", "just", "now",
             "when", "while", "after", "before", "because", "though", "although", "however", "instead", "suddenly",
             "slowly", "quickly", "finally", "sometimes", "already", "almost", "only", "yet", "also", "soon", "later",
             "enough", "nothing", "everyone", "someone", "anyone", "everything", "something", "please", "thank",
             "sorry", "okay", "well", "which", "if", "as", "so", "but", "or", "for", "with", "without", "not", "do",
             "did", "does", "can", "could", "would", "should", "will", "may", "might", "must", "let", "come", "go",
             "good", "great", "never", "ever", "every", "each", "both", "either", "neither", "all", "any", "more",
             "most", "much", "many", "few", "some", "other", "another", "such", "same", "own", "very", "too",
             "quite", "rather", "really", "truly", "surely", "indeed", "perhaps", "tonight", "today", "tomorrow",
             "yesterday", "here", "where", "why", "how", "what", "who", "whom", "whose", "these", "those", "than"}


def _base(word):
    """A word without a trailing possessive or contraction: Rhys's -> Rhys, He'd -> He, Cassian' -> Cassian."""
    return re.sub(r"[\u2019'](?:s|d|ll|ve|re|m|t)?$", "", word)


SENTENCE_SPLIT = re.compile(r"(?<!\bMr\.)(?<!\bMrs\.)(?<!\bMs\.)(?<!\bDr\.)(?<!\bSt\.)(?<!\bJr\.)(?<!\bSr\.)"
                            r"(?<=[.!?\u2026])\s+")


def _narration(paragraph):
    return QUOTE_RE.sub(" … ", paragraph)


def _runs_on(paragraph):
    """True when the paragraph ends inside a quote: the speech continues in the next paragraph."""
    if paragraph.count('"') % 2 == 1:
        return True
    return paragraph.rfind("\u201c") > paragraph.rfind("\u201d")


def _gender_of_words(words):
    f = sum(1 for w in words if w in FEMALE_PRON)
    m = sum(1 for w in words if w in MALE_PRON)
    return "f" if f and not m else "m" if m and not f else None


NOT_SPEECH = r"(?!\s+(?:nothing|no more|little|not a word|anything)\b)"   # "Feyre said nothing" is not a speech tag
NARRATOR = "(narrator)"   # the first-person narrator ("I said"): their lines stay in the narrator's voice
_I_AFTER = re.compile(r"\u2026\W{0,3}\s*(?:I\s+(?:" + VERBS + r")\b|(?:" + VERBS + r")\s+I\b)")
_I_BEFORE = re.compile(r"(?<![\w'\u2019])I\s+(?:" + VERBS + r")" + NOT_SPEECH + r"\W{0,3}\s*\u2026")
_PRON_AFTER = re.compile(r"\u2026\W{0,3}\s*(?:(she|he)\s+(?:" + VERBS + r")\b|(?:" + VERBS + r")\s+(she|he)\b)", re.IGNORECASE)
_PRON_BEFORE = re.compile(r"\b(she|he)\s+(?:" + VERBS + r")\W{0,3}\s*\u2026", re.IGNORECASE)


def _earliest(*matches):
    found = [m for m in matches if m]
    return min(found, key=lambda m: m.start()) if found else None


def _pronoun(m):
    return next(x for x in m.groups() if x).lower()


# af_nicole is left out: Kokoro's author made it as a whispering (ASMR) voice, wrong for a main character
FEMALE_POOL = ["af_kore", "af_sarah", "af_aoede", "af_bella,af_sarah", "af_kore,af_sarah", "af_alloy",
               "af_nova", "af_sky", "af_jessica", "af_river"]
# titles and roles that are not a person's name when they stand alone ("the Captain said", "Ms" before a name)
ROLE_WORDS = {"mr", "mrs", "ms", "miss", "dr", "doctor", "professor", "captain", "sir", "agent", "detective",
              "sergeant", "general", "colonel", "officer", "president", "senator", "governor", "secretary",
              "minister", "chairman", "commander", "admiral", "major", "lieutenant", "chief", "director", "judge",
              "mayor", "ambassador", "representative", "congressman", "congresswoman", "commissioner", "inspector"}
# what a sentence about one named person says about them: "she", "his eyes", "himself" (not "handed him",
# and not "her", which is as often someone else)
SUBJECT_F, SUBJECT_M = {"she", "hers", "herself"}, {"he", "his", "himself"}
CONFIRM_TAGS = 2      # explicit tags ("said Rhys") needed before someone counts as a character
MALE_POOL = ["am_fenrir", "am_puck", "am_michael", "am_michael,am_puck", "am_fenrir,am_puck", "am_michael,am_fenrir",
             "am_echo", "am_eric", "am_liam", "am_onyx", "am_adam"]
NAME_RE = r"[A-Z][a-z][\w'\u2019-]*"
TITLE_RE = r"(?:(Mr|Mrs|Ms|Miss|Dr|Doctor|Professor|Captain|Sir|Lady|Lord|King|Queen|Prince|Princess|Aunt|Uncle|Agent|" \
           r"Detective|Sergeant|General|Colonel|Officer|Father|Mother|Sister|Brother)\.?\s+)?"


class SpeakerGuesser:
    def __init__(self, paragraphs=()):
        paragraphs = list(paragraphs)
        self.learned = {}
        self.proper = Counter()      # capitalized words seen in the middle of sentences (names, places)
        self.lower = Counter()       # how often each word appears in lowercase (names almost never do)
        self.aliases = {}            # surname -> first name
        self.titles = {}             # name -> title it was used with (Mrs, Captain...)
        self.pending = defaultdict(Counter)   # unknown-gender name -> genders of the "he/she said" that followed it
        self._pron = False
        self.ai = {}                 # chapter key -> {paragraph index: [name, gender]} from speakertag.py
        self._explicit = False
        self.learn(paragraphs)
        self.learn_names(paragraphs)

    def learn_names(self, paragraphs):
        pairs = Counter()
        for p in paragraphs:
            for sentence in SENTENCE_SPLIT.split(p):
                words = re.findall(r"[A-Za-z'\u2019-]+", sentence)
                for i, w in enumerate(words):
                    base = _base(w)
                    if w == w.lower():
                        self.lower[base.lower()] += 1
                    elif i and w[:1].isupper() and base.lower() not in STOP_CAPS and len(base) > 1:
                        self.proper[base] += 1
                for a, b in re.findall(r"\b(" + NAME_RE + r")\s+(" + NAME_RE + r")\b", sentence):
                    a, b = _base(a), _base(b)
                    if a.lower() not in STOP_CAPS and b.lower() not in STOP_CAPS:
                        pairs[(a, b)] += 1
                for title, name in re.findall(TITLE_RE.replace("(?:(", "(?:(", 1) + r"(" + NAME_RE + r")", sentence):
                    if title:
                        self.titles.setdefault(_base(name), title)
        titles = ROLE_WORDS | FEMALE_TITLES | MALE_TITLES
        for (first, last), n in pairs.most_common():
            if first.lower() in titles or last.lower() in titles:
                continue                 # "Captain Lokken" is a title and a name, not a first name and a surname
            if last not in self.aliases and first not in self.aliases and \
                    (first.lower() in FEMALE_NAMES or first.lower() in MALE_NAMES or self.proper[first] >= 2):
                self.aliases[last] = first

    def use_ai(self, data):
        """Use the speaker tags the language model made (speakertag.py). Its genders fill in names the
        rules could not place."""
        self.ai = (data or {}).get("chapters") or {}
        for name, g in ((data or {}).get("genders") or {}).items():
            if g in ("f", "m") and self.name_gender(name) is None:
                self.learned[name.lower()] = g

    def _ai_tag(self, tags, i):
        t = tags.get(str(i)) if tags else None
        if not t or not (t[0] or t[1]):
            return None
        name, g = t[0], t[1]
        if name == NARRATOR:
            return NARRATOR, None
        return name, (self.name_gender(name) if name else None) or g

    def is_name(self, word, title=None, tagged=False):
        """tagged: the word sits right next to a speech verb ("Cassian said"), which on its own is good evidence."""
        base = _base(word)
        low = base.lower()
        if len(base) < 2 or low in STOP_CAPS or low in FEMALE_PRON | MALE_PRON or low in ROLE_WORDS:
            return False
        if self.lower[low] > 0.25 * max(1, self.proper[base]) and base not in self.aliases:
            return False                 # a word the book mostly writes in lowercase (like, again, rose)
        return bool(title) or tagged or low in FEMALE_NAMES or low in MALE_NAMES or low in self.learned or \
            self.proper[base] >= 2 or base in self.aliases

    def canon(self, word):
        base = _base(word)
        return self.aliases.get(base, base)

    # ------------------------------------------------------------------ names
    def learn(self, paragraphs):
        votes = defaultdict(Counter)
        for p in paragraphs:
            for sentence in SENTENCE_SPLIT.split(_narration(p)):
                words = re.findall(r"[A-Za-z'’]+", sentence)
                if len(words) < 3:
                    continue
                names = {_base(w) for i, w in enumerate(words) if w[0].isupper() and _base(w).lower() not in STOP_CAPS
                         and len(_base(w)) > 1}
                lows = {w.lower() for w in words[1:]}
                f, m = bool(lows & SUBJECT_F), bool(lows & SUBJECT_M)
                g = "f" if f and not m else "m" if m and not f else None
                if len(names) == 1 and g:
                    votes[next(iter(names))][g] += 1
        for name, c in votes.items():
            total = sum(c.values())
            g, n = c.most_common(1)[0]
            if total >= 2 and n / total >= 0.75:
                self.learned[name.lower()] = g

    def name_gender(self, name, before=""):
        name = _base(name)
        if not before and name in self.titles:
            before = self.titles[name]
        if not before:
            for surname, first in self.aliases.items():      # "Tony" also gets what "Stark" was called (Mr. Stark)
                if first == name and surname in self.titles:
                    before = self.titles[surname]
                    break
        key = name.lower()
        title = re.findall(r"([A-Za-z']+)\.?\s*$", before)
        if title and title[0].lower() in FEMALE_TITLES:
            return "f"
        if title and title[0].lower() in MALE_TITLES:
            return "m"
        if key in FEMALE_NAMES and key not in MALE_NAMES:
            return "f"                   # a common woman's name beats a guess from nearby pronouns
        if key in MALE_NAMES and key not in FEMALE_NAMES:
            return "m"
        if key in self.learned:
            return self.learned[key]
        return None

    # ------------------------------------------------------------------ paragraphs
    def tag_gender(self, narration):
        """Gender from the first speech tag: 'she said', 'said Natasha', 'Tony replied'; "n" for 'I said'."""
        i_tag = _earliest(_I_AFTER.search(narration), _I_BEFORE.search(narration))
        pron = _earliest(_PRON_AFTER.search(narration), _PRON_BEFORE.search(narration))
        named = re.search(r"\u2026\W{0,3}\s*(?:" + VERBS + r")\s+((?:[A-Z][a-z']+\.?\s)?)([A-Z][\w'\u2019-]+)", narration)
        first = _earliest(i_tag, pron, named)
        if first is None:
            m = re.search(r"((?:[A-Z][a-z']+\.?\s)?)([A-Z][\w'\u2019-]+)(?:\s+[A-Z][\w'\u2019-]+)?\s+(?:" + VERBS + r")\b",
                          narration)
            if m and _base(m.group(2)).lower() not in STOP_CAPS:
                return self.name_gender(m.group(2), m.group(1))
            return None
        if first is i_tag:
            return "n"
        if first is pron:
            return "f" if _pronoun(pron) == "she" else "m"
        return self.name_gender(first.group(2), first.group(1))

    def subject_gender(self, narration):
        """Gender of who the narration is about: the first He/She or name that starts a sentence."""
        for sentence in SENTENCE_SPLIT.split(narration.strip()):
            s = sentence.strip(" …,;:")
            if not s:
                continue
            first = re.match(r"([A-Za-z'’-]+)", s)
            if not first:
                continue
            w = first.group(1)
            if _base(w) == "I":
                return "n"
            if w.lower() == "she":
                return "f"
            if w.lower() == "he":
                return "m"
            if w[0].isupper() and w.lower() not in STOP_CAPS:
                g = self.name_gender(w)
                if g:
                    return g
            g = _gender_of_words(re.findall(r"[a-z']+", s.lower()))
            if g:
                return g
        return None

    def genders(self, paragraphs):
        """One 'f', 'm' or None per paragraph: the speaker of the quotes in it (None for no quotes)."""
        out, recent = [], []          # recent: genders of the dialogue paragraphs in the current exchange
        running_on = None             # speech whose closing quote has not come yet (it runs into this paragraph)
        paragraphs = list(paragraphs)
        tags = self.ai.get(chapter_key(paragraphs)) if self.ai else None
        for i, p in enumerate(paragraphs):
            if running_on is not None:
                g, running_on = running_on, None
                if _runs_on(p):
                    running_on = g
                out.append(None if g == "n" else g)
                recent.append(g)
                continue
            if not QUOTE_RE.search(p) and not p.lstrip().startswith(("“", '"')):
                out.append(None)
                recent = []
                continue
            narration = _narration(p)
            tagged = self.tag_gender(narration)
            ai = None if tagged else self._ai_tag(tags, i)
            if ai:                    # no tag in the text: the language model's reading beats the rules below
                g = "n" if ai[0] == NARRATOR else ai[1]
            else:
                g = tagged or self.subject_gender(narration)
                if g is None and len(recent) >= 2:
                    g = recent[-2]    # speakers take turns
            if g and _runs_on(p):
                running_on = g
            out.append(None if g == "n" else g)     # the narrator's own lines stay in the narrator's voice
            recent.append(g)
        return out

    # ------------------------------------------------------------------ who speaks
    def _names_in(self, text):
        found = []
        for m in re.finditer(TITLE_RE + r"(" + NAME_RE + r")", text):
            title, word = m.group(1), m.group(2)
            if self.is_name(word, title):
                found.append((m.start(), self.canon(word)))
        return found

    def _remember(self, last, text):
        for _, name in self._names_in(text):
            g = self.name_gender(name)
            if g:
                last[g] = name

    def _tag_speaker(self, narration, last):
        """(name or None, gender or None) from the first speech tag in the paragraph. 'I said' -> NARRATOR."""
        candidates = []
        self._explicit = self._pron = False
        i_tag = _earliest(_I_AFTER.search(narration), _I_BEFORE.search(narration))
        if i_tag:
            candidates.append((i_tag.start(), "i", i_tag))
        pron = _earliest(_PRON_AFTER.search(narration), _PRON_BEFORE.search(narration))
        if pron:
            candidates.append((pron.start(), "p", pron))
        m = re.search(r"\u2026\W{0,3}\s*(?:" + VERBS + r")\s+" + TITLE_RE + r"(" + NAME_RE + r")", narration)
        if m and self.is_name(m.group(2), m.group(1), tagged=True):
            candidates.append((m.start(), "n", (m.group(2), m.group(1) or "")))
        for m in re.finditer(TITLE_RE + r"(" + NAME_RE + r")(?:\s+(" + NAME_RE + r"))?\s+(?:" + VERBS + r")\b" + NOT_SPEECH,
                             narration):
            hit = None
            for word in ([m.group(3)] if m.group(3) else []) + [m.group(2)]:     # the word nearest the verb first
                if self.is_name(word, m.group(1) if word == m.group(2) else None, tagged=word == (m.group(3) or m.group(2))):
                    hit = (word, (m.group(1) or "") if word == m.group(2) else "")
                    break
            if hit:
                candidates.append((m.start(), "n", hit))
                break
        if not candidates:
            return None, None
        _, kind, found = min(candidates, key=lambda c: c[0])
        self._explicit = kind in ("i", "n")      # "said Rhys" / "I said": the text says so, nothing to guess
        self._pron = kind == "p"                 # "she said": the text gives the gender
        if kind == "i":
            return NARRATOR, None
        if kind == "n":
            name = self.canon(found[0])
            return name, self.name_gender(name, found[1])
        g = "f" if _pronoun(found) == "she" else "m"
        local = dict(last)
        self._remember(local, narration[:found.start()])
        who = local.get(g)
        if who is None:
            # "Rhys looked up. 'Go,' he said." Rhys has no known gender yet: after two agreeing sightings
            # (and no disagreeing one) he is learned as male and gets the line
            unknown = [n for _, n in self._names_in(narration[:found.start()]) if self.name_gender(n) is None]
            if unknown:
                n = unknown[-1]
                self.pending[n][g] += 1
                other = "m" if g == "f" else "f"
                if self.pending[n][g] >= 2 and not self.pending[n][other]:
                    self.learned[n.lower()] = g
                    who = n
        return who, g

    def _subject_speaker(self, narration, last):
        for sentence in SENTENCE_SPLIT.split(narration.strip()):
            s = sentence.strip(" \u2026,;:")
            m = re.match(TITLE_RE + r"([A-Za-z'\u2019-]+)", s)
            if not m:
                continue
            word = m.group(2)
            if _base(word) == "I":
                return NARRATOR, None
            if word.lower() == "she":
                return last.get("f"), "f"
            if word.lower() == "he":
                return last.get("m"), "m"
            if word[:1].isupper() and self.is_name(word, m.group(1)):
                name = self.canon(word)
                return name, self.name_gender(name, m.group(1) or "")
            return None, None
        return None, None

    def speakers(self, paragraphs):
        """One (name, gender) per paragraph: who says the quotes in it. Either can be None."""
        out, recent = [], []
        self.kinds = []                 # how each paragraph's speaker was found: name, ai, pron, subject, turn, runon
        last = {}                       # the woman and the man named most recently
        running_on = None
        paragraphs = list(paragraphs)
        tags = self.ai.get(chapter_key(paragraphs)) if self.ai else None
        for i, p in enumerate(paragraphs):
            if running_on is not None:
                who, running_on = running_on, None
                if _runs_on(p):
                    running_on = who
                out.append(who)
                self.kinds.append("runon")
                recent.append(who)
                continue
            if not QUOTE_RE.search(p) and not p.lstrip().startswith(("\u201c", '"')):
                out.append((None, None))
                self.kinds.append(None)
                recent = []
                self._remember(last, p)
                continue
            narration = _narration(p)
            name, g = self._tag_speaker(narration, last)
            kind = "name" if self._explicit else "pron" if self._pron else None
            ai = None if self._explicit else self._ai_tag(tags, i)
            if ai and self._pron and ai[1] and g and ai[1] != g:
                ai = None               # the model named a man for "she said": trust the text
            if ai:                      # the language model read the scene; only an explicit tag beats it
                name, g = ai[0], None if ai[0] == NARRATOR else ai[1] or (g if self._pron else None)
                kind = "ai"
            if name is None and g is None:
                name, g = self._subject_speaker(narration, last)
                kind = "subject"
            if name is None and g is None and len(recent) >= 2:
                name, g = recent[-2]    # speakers take turns
                kind = "turn"
            if name and not g:
                g = self.name_gender(name)
            self._remember(last, narration)
            if name and g:
                last[g] = name          # whoever just spoke is who "she" or "he" means next
            who = (name, g)
            if (name or g) and _runs_on(p):
                running_on = who
            out.append(who)
            self.kinds.append(kind)
            recent.append(who)
        return out


def _first_quote(paragraph):
    m = QUOTE_RE.search(paragraph)
    q = (m.group(0) if m else paragraph).strip("\u201c\u201d\" ")
    return q if len(q) <= 140 else q[:137].rsplit(" ", 1)[0] + "..."


def looks_like_fiction(chapters, share=0.15):
    """True when the book reads like a story told in dialogue: at least `share` of its paragraphs are short lines
    of speech. Measured on real books: novels 45 to 90 %, journalism and history with quotations 5 to 9 %."""
    paras = [p for c in chapters for p in c.paragraphs]
    if not paras:
        return False
    talk = sum(1 for p in paras if (QUOTE_RE.search(p) or p.lstrip().startswith(("\u201c", '"')))
               and len(QUOTE_RE.sub("", p).split()) <= 12)
    return talk / len(paras) >= share


def find_characters(chapters, guesser=None, min_lines=2, keep=()):
    """[{"name", "lines", "gender", "example"}] for everyone who speaks at least min_lines times, most first.
    Someone only counts after CONFIRM_TAGS explicit speech tags ("said Rhys", or the AI's tags), so place names
    and titles picked up by the looser guesses are not cast. Names in keep (chosen in Cast...) always count."""
    guesser = guesser or SpeakerGuesser(p for c in chapters for p in c.paragraphs)
    lines, votes, example, sure = Counter(), defaultdict(Counter), {}, Counter()
    for ch in chapters:
        who = guesser.speakers(ch.paragraphs)
        for p, (name, g), kind in zip(ch.paragraphs, who, guesser.kinds):
            if not name or name == NARRATOR:
                continue
            lines[name] += 1
            if kind in ("name", "ai"):
                sure[name] += 1
            if g and kind in ("name", "ai", "pron"):
                votes[name][g] += 1
            if name not in example and QUOTE_RE.search(p):
                example[name] = _first_quote(p)
    out = []
    keep = set(keep)
    for name, n in lines.most_common():
        if n < min_lines and name not in keep:
            continue
        known = name.lower() in FEMALE_NAMES or name.lower() in MALE_NAMES or name in guesser.titles
        if sure[name] < (1 if known else CONFIRM_TAGS) and name not in keep:
            continue                     # a common first name needs one clear tag, anything else two
        v = votes[name]
        listed = guesser.name_gender(name)
        g = listed or (v.most_common(1)[0][0] if v and v.most_common(1)[0][1] >= 0.75 * sum(v.values()) else None)
        title = guesser.titles.get(name)
        out.append({"name": name, "display": f"{title}. {name}" if title and title[-1] != "." and
                    title not in ("Sir", "Lady", "Lord", "King", "Queen", "Prince", "Princess", "Aunt", "Uncle")
                    else (f"{title} {name}" if title else name),
                    "lines": n, "gender": g, "example": example.get(name, "")})
    return out


def cast(characters, narrator, female_default="af_bella", male_default="am_michael", chosen=None, main_lines=3):
    """{name: voice}. Choices already made (chosen) are kept. Every character with at least main_lines
    lines gets a voice of their own from the pool for their gender, best voices to the biggest parts;
    smaller parts share the women's or men's voice; unknown gender stays with the narrator."""
    chosen = dict(chosen or {})
    taken = {narrator, female_default, male_default} | set(chosen.values())
    pools = {"f": [v for v in FEMALE_POOL if v not in taken], "m": [v for v in MALE_POOL if v not in taken]}
    out = {}
    for c in characters:
        name, g = c["name"], c.get("gender")
        if name in chosen:
            out[name] = chosen[name]
        elif g in pools and c["lines"] >= main_lines and pools[g]:
            out[name] = pools[g].pop(0)
        elif g == "f":
            out[name] = female_default
        elif g == "m":
            out[name] = male_default
        else:
            out[name] = narrator
    return out


def cast_file(book_stem, folder):
    from epub_reader import safe_name
    return folder / f"{safe_name(book_stem)}.json"


def load_cast(book_stem, folder):
    try:
        data = json.loads(cast_file(book_stem, folder).read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in (data.get("voices") or {}).items()}
    except Exception:  # noqa: BLE001
        return {}


def save_cast(book_stem, folder, voices):
    folder.mkdir(parents=True, exist_ok=True)
    f = cast_file(book_stem, folder)
    f.write_text(json.dumps({"voices": voices}, indent=2, ensure_ascii=False), encoding="utf-8")
    return f


def cast_hash(voices):
    return hashlib.sha1(json.dumps(sorted(voices.items())).encode("utf-8")).hexdigest()[:8] if voices else ""



def chapter_key(paragraphs):
    """Identifies a chapter's text, so saved tags are only used for exactly the text they were made for."""
    return hashlib.sha1("\n".join(paragraphs).encode("utf-8")).hexdigest()[:16]


def ai_tags_file(book_stem, folder):
    from epub_reader import safe_name
    return folder / f"{safe_name(book_stem)}.speakers.json"


def load_ai_tags(book_stem, folder):
    try:
        data = json.loads(ai_tags_file(book_stem, folder).read_text(encoding="utf-8"))
        return data if isinstance(data.get("chapters"), dict) else None
    except Exception:  # noqa: BLE001
        return None


def ai_hash(data):
    if not data:
        return ""
    return hashlib.sha1(json.dumps([data.get("chapters"), data.get("genders")], sort_keys=True).encode("utf-8")).hexdigest()[:8]
