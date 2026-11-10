import os, sys, re, json, shutil, tempfile
from pathlib import Path
sys.path.insert(0, os.getcwd())
import speakers, speakertag, llm

tmp = Path(tempfile.mkdtemp())
speakertag.CASTING_DIR = tmp
ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1
    else: fail += 1; print("FAIL", name, extra)

class Ch:
    def __init__(s, title, paras): s.title, s.paragraphs, s.notes = title, paras, []
paras = [
 "Feyre walked into the room where Rhys and Cassian sat by the fire.",
 "“You are late,” Rhys said.",
 "“I know.”",
 "“Where were you?”",
 "Cassian grinned at her. “She was with Nesta.”",
 "“I was not,” I said.",
 "“Liar.”",
 "The fire crackled. Azriel stepped out of the shadows.",
 "“Enough,” she said.",
 "Mor laughed. Feyre sat. Rhys sighed. Cassian nodded. Azriel waited.",
]
ch = Ch("One", paras)
chapters = [ch, Ch("Two", ["Rhys smiled at Feyre. Cassian left. Mor smiled.", "“Hello,” said Mor."])]

class Fake:
    name = "fake/model"
    def __init__(s): s.prompts = []
    def chat(s, system, user, max_new_tokens=0):
        s.prompts.append(user)
        nums = [int(n) for n in re.findall(r"^\[(\d+)\]", user, re.M)]
        answers = {1: "Rhys, man", 2: "I", 3: "Rhys, man", 4: "Cassian, man", 5: "Rhys, man", 6: "Cassian (man)", 7: "Lord Azriel, man",
                   }
        # second chapter has 1 dialogue paragraph
        if "said Mor" in user:
            return "1: Mor, woman"
        return "\n".join(f"{n}: {answers.get(n, '?')}" for n in nums) + "\nSome chatter"
m = Fake()
data = speakertag.tag_book(chapters, m, "My Book", "A Court", "SJM")
key = speakers.chapter_key(paras)
tags = data["chapters"][key]
check("tag count", len(tags) == 7, tags)
check("first", tags["1"] == ["Rhys", "m"], tags)
check("narrator", tags["2"] == [speakers.NARRATOR, None], tags)
check("title stripped", tags["8"] == ["Azriel", "m"], tags)
check("prompt has characters", "Characters met so far" in m.prompts[0] and "[1]" in m.prompts[0])
check("genders", data["genders"].get("Mor") == "f" and data["genders"].get("Cassian") == "m", data["genders"])
check("saved", (tmp / "My Book.speakers.json").exists())

# resume: no new calls
m2 = Fake()
speakertag.tag_book(chapters, m2, "My Book")
check("resume no calls", m2.prompts == [], len(m2.prompts))
m3 = Fake(); speakertag.tag_book(chapters, m3, "My Book", fresh=True)
check("fresh calls", len(m3.prompts) == 2)
# stop
m4 = Fake(); d4 = speakertag.tag_book(chapters, m4, "Other", stop=lambda: True)
check("stop", m4.prompts == [] and d4["chapters"] == {})

# merge rules
g = speakers.SpeakerGuesser(p for c in chapters for p in c.paragraphs)
before = g.speakers(paras)
g.use_ai(speakertag.load("My Book"))
after = g.speakers(paras)
check("explicit tag kept", after[1][0] == "Rhys")
check("untagged uses ai", after[3][0] == "Rhys", after[3])     # "Where were you?" 
check("I said wins", after[5][0] == speakers.NARRATOR, after[5])
check("ai narrator", after[2][0] == speakers.NARRATOR and after[2][1] is None, after[2])
check("pron conflict -> text", after[8][1] == "f", after[8])   # "she said" but AI says Azriel man
gen = g.genders(paras)
check("genders explicit", gen[1] == "m")
check("genders ai", gen[3] == "m" and gen[2] is None, gen)
check("genders pron", gen[8] == "f", gen)
check("learned gender", g.name_gender("Azriel") == "m")
# changed text -> tags unused
g2 = speakers.SpeakerGuesser(paras); g2.use_ai(speakertag.load("My Book"))
changed = paras[:-1] + ["changed"]
check("changed text ignored", speakers.chapter_key(changed) not in g2.ai and speakers.chapter_key(paras) in g2.ai)
check("hash", speakers.ai_hash(speakertag.load("My Book")) and speakers.ai_hash(None) == "")
# find_characters with ai
chars = speakers.find_characters(chapters, g, min_lines=1)
check("characters", any(c["name"] == "Rhys" for c in chars), chars)

# parse
pa = speakertag.parse_answer("1: Rhys, man\n**2**: ?\n3 - The narrator\n[4] Nesta (woman)\n5: unknown, woman\nnope")
check("parse", pa == {1: ("Rhys", "m"), 2: (None, None), 3: ("I", None), 4: ("Nesta", "f"), 5: (None, "f")}, pa)
check("windows", speakertag.windows(["x" * 800] * 20 + ["“hi”"]) == [(18, 21)], speakertag.windows(["x" * 800] * 20 + ["“hi”"]))

# pronunciation parsing
s = llm.parse_suggestions("Here you go:\n1. Feyre = fay-ruh\n**Rhysand** = REE-sand\nAzriel: az-ree-el\nTamlin = tamlin\nVelaris → vel-air-iss (like Paris)\nBogus = x",
                          ["Feyre", "Rhysand", "Azriel", "Tamlin", "Velaris"])
check("suggest parse", s == {"Feyre": "fay-ruh", "Rhysand": "ree-sand", "Azriel": "az-ree-el", "Velaris": "vel-air-iss"}, s)
class FakeS:
    def chat(s, sy, u, max_new_tokens=0):
        assert "A Court of Mist and Fury by Sarah J. Maas" in u and "as in:" in u
        return "\n".join(f"{w} = {w.lower()}-ah" for w in re.findall(r"^(\w+)   \(as in", u, re.M))
seen = []
r = llm.suggest(FakeS(), [{"word": f"Word{i}", "example": "It was Word here."} for i in range(25)], "A Court of Mist and Fury",
                "Sarah J. Maas", on_found=lambda w, v: seen.append(w))
check("suggest batches", len(r) == 25 and len(seen) == 25, len(r))
print(f"{ok} passed, {fail} failed")
shutil.rmtree(tmp)
