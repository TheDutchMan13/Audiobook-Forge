import sys
sys.dont_write_bytecode = True
from normalize import normalize_text, clean_clutter, fix_heteronyms, strip_markup, is_caption, prepare
cases_norm = [
    # money with size words
    ("The deal was worth $3.5M last year.", "The deal was worth 3.5 million dollars last year."),
    ("They raised $2 billion in a week.", "They raised 2 billion dollars in a week."),
    ("It cost £450m to build.", "It cost 450 million pounds to build."),
    ("Revenue hit $12K.", "Revenue hit 12 thousand dollars."),
    ("It costs $5.20 today.", "It costs $5.20 today."),                                # misaki reads this already
    # dates
    ("Born on June 5, 1990 in Ohio.", "Born on June 5th, 1990 in Ohio."),
    ("By Oct. 12 it was over.", "By October 12th it was over."),
    ("On 4 July 1776 they signed.", "On the 4th of July 1776 they signed."),
    ("Filed 6/5/2021 in court.", "Filed June 5th, 2021 in court."),
    ("Filed 25/12/2020 in court.", "Filed December 25th, 2020 in court."),
    ("May 3rd was sunny.", "May 3rd was sunny."),
    # times
    ("We met at 3:30 p.m. sharp.", "We met at 3 30 PM sharp."),
    ("The train left at 10:05.", "The train left at 10 oh 5."),
    ("Dinner at 7:00 tonight.", "Dinner at 7 o'clock tonight."),
    ("Alarm at 6am every day.", "Alarm at 6 AM every day."),
    ("He woke at 7:00 AM.", "He woke at 7 AM."),
    # decades
    ("Music of the '90s was loud.", "Music of the nineties was loud."),
    ("In the late 80s it changed.", "In the late eighties it changed."),
    ("The 1990s were good.", "The 1990s were good."),                                    # misaki reads this already
    # ranges
    ("The war lasted 1939–1945.", "The war lasted 1939 to 1945."),
    ("See pp. 12-15 for details.", "See pages 12 to 15 for details."),
    ("It takes 10-15 minutes.", "It takes 10 to 15 minutes."),
    ("For ages 8-12 only.", "For ages 8 to 12 only."),
    ("The score was 3-2 at half time.", "The score was 3-2 at half time."),             # not a range
    # fractions
    ("Add 1/2 cup of flour.", "Add one half cup of flour."),
    ("Add ½ cup of milk.", "Add one half cup of milk."),
    ("About 3/4 of them left.", "About three quarters of them left."),
    # units and temperatures
    ("It was 72°F outside.", "It was 72 degrees Fahrenheit outside."),
    ("Run 5 km a day.", "Run 5 kilometers a day."),
    ("Only 1 km away.", "Only 1 kilometer away."),
    ("It weighed 3.5 kg in all.", "It weighed 3.5 kilograms in all."),
    ("Driving at 60 mph.", "Driving at 60 miles per hour."),
    ("A 2 lb bag.", "A 2 pounds bag."),
    ("The film ran 120 mins.", "The film ran 120 minutes."),
    # symbols
    ("He was #1 in his class.", "He was number 1 in his class."),
    ("No. 5 was missing.", "number 5 was missing."),
    ("A 4x4 truck in a 10 x 12 room.", "A 4 by 4 truck in a 10 by 12 room."),
    ("It took ~20 tries.", "It took about 20 tries."),
    # Roman numerals
    ("Chapter IV begins here.", "Chapter 4 begins here."),
    ("Part II: The Return", "Part 2: The Return"),
    ("After World War II ended.", "After World War Two ended."),
    ("Henry VIII had six wives.", "Henry the Eighth had six wives."),
    ("Louis XIV built Versailles.", "Louis the Fourteenth built Versailles."),
    ("Queen Elizabeth I.", "Queen Elizabeth the First."),
    ("Then Elizabeth I went home.", "Then Elizabeth I went home."),                       # "I" the pronoun
    ("I think I can.", "I think I can."),
    ("Mix in a cup.", "Mix in a cup."),                                                   # "Mix" is not a numeral word
    # titles and abbreviations
    ("Dr. Smith met Mr. and Mrs. Jones.", "Doctor Smith met Mister and Missus Jones."),
    ("She lives on Elm Dr. now.", "She lives on Elm Drive now."),
    ("St. Louis is on Main St. downtown.", "Saint Louis is on Main Street downtown."),
    ("Mt. Everest and Ft. Worth.", "Mount Everest and Fort Worth."),
    ("Apples, pears, etc. are fine.", "Apples, pears, etc. are fine."),
    ("We bought apples, pears, etc. Then we left.", "We bought apples, pears, etc. Then we left."),
    ("Ms. Lee and Ms. Park", "[Ms](/m\u02c8\u026az/) Lee and [Ms](/m\u02c8\u026az/) Park"),
    ("Fruit, e.g. apples, is good.", "Fruit, for example, apples, is good."),
    ("Ali vs. Frazier", "Ali versus Frazier"),
    ("Martin Luther King Jr. spoke.", "Martin Luther King Junior spoke."),
    ("Prof. Lee and Gen. Grant", "Professor Lee and General Grant"),
    # must not change
    ("He said it plainly.", "He said it plainly."),
    ("The year 1984 was cold.", "The year 1984 was cold."),
    ("She was 21st in line.", "She was 21st in line."),
    ("Ratio of 3:2 overall.", "Ratio of 3:2 overall."),
    ("John 3:16 is famous.", "John 3 16 is famous."),
]
cases_clutter = [
    ("The study was clear.[12] Others agreed.", "The study was clear. Others agreed."),
    ("Several said so [3, 4] in print.", "Several said so in print."),
    ("It was true.¹² Then more.", "It was true. Then more."),
    ("See https://example.com/page for more.", "See for more."),
    ("He knew the end.12 Then he left.", "He knew the end. Then he left."),
    ("The witness* lied.", "The witness lied."),
    ("Visit www.example.org today.", "Visit today."),
    ("Room 101 was locked.", "Room 101 was locked."),
    ("Catch-22 is a novel.", "Catch-22 is a novel."),
    ("[citation needed] The claim stands.", "The claim stands."),
]
cases_het = [
    ("The old lead pipes were rusty.", True), ("It was made of lead.", True), ("Lead poisoning is real.", True),
    ("She took the lead in the race.", False), ("They will lead us home.", False), ("He played bass guitar.", True),
    ("She plays bass in a band.", True), ("He caught a bass in the lake.", False), ("We had a row about money.", True),
    ("A row of houses stood there.", False), ("They had a row of seats.", False), ("He spoke Polish fluently.", True),
    ("Polish the silver before dinner.", False), ("The striped bass swam away.", False),
]
fails = 0
for src, want in cases_norm:
    got = normalize_text(src)
    if got != want:
        fails += 1; print(f"NORM FAIL\n  in:   {src}\n  got:  {got}\n  want: {want}")
for src, want in cases_clutter:
    got = clean_clutter(src)
    if got != want:
        fails += 1; print(f"CLUTTER FAIL\n  in:   {src}\n  got:  {got}\n  want: {want}")
for src, changed in cases_het:
    got = fix_heteronyms(src)
    if (got != src) != changed:
        fails += 1; print(f"HETERONYM FAIL ({'should' if changed else 'should not'} change)\n  in:  {src}\n  got: {got}")
caps = [("Figure 3: The engine room in 1912.", True), ("Table 2. Results by year", True), ("Fig. 4 — a map", True),
        ("Figures show the trend clearly over many decades of careful study.", False)]
for p, want in caps:
    if is_caption(p) != want:
        fails += 1; print("CAPTION FAIL", p)
print(f"\n{len(cases_norm) + len(cases_clutter) + len(cases_het) + len(caps) - fails} passed, {fails} failed")
print("markup shown to people:", strip_markup(fix_heteronyms("It was made of lead.")))
print("all steps:", prepare("Dr. Smith paid $2.5M on 6/5/2021 at 3:30 p.m.[4] for lead pipes."))
extra = [("Up at 7 a.m. Then breakfast.", "Up at 7 AM. Then breakfast."), ("Meet at 9:15 a.m. Bring water.", "Meet at 9 15 AM. Bring water."),
         ("Leave at 9:15 a.m. or later.", "Leave at 9 15 AM or later."), ("Dr. and Mrs. Patel arrived.", "Doctor and Missus Patel arrived.")]
bad = [(s, normalize_text(s), w) for s, w in extra if normalize_text(s) != w]
print("extra:", len(extra) - len(bad), "passed", bad)
