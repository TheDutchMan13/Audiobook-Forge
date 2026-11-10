import os, sys, subprocess, traceback, types, time, json
os.makedirs("watchme", exist_ok=True)
from pathlib import Path
sys.path.insert(0, os.getcwd())
os.environ["STUB_DELAY"] = "0.08"
import forge_gui as g
from tkinterdnd2 import TkinterDnD
SHOTS = Path("shots"); SHOTS.mkdir(exist_ok=True)
json.dump({"out_dir": str(Path("out").resolve()), "theme": "light", "sound": False}, open("settings.json", "w"))
root = TkinterDnD.Tk()
errors, notes, commands = [], [], []
def report(*a):
    errors.append("".join(traceback.format_exception(*a))); print(errors[-1], flush=True)
root.report_callback_exception = report
g.messagebox.showinfo = lambda *a, **k: notes.append(("info", a[1] if len(a) > 1 else ""))
g.messagebox.showerror = lambda *a, **k: notes.append(("error", a[1] if len(a) > 1 else ""))
g.messagebox.showwarning = lambda *a, **k: notes.append(("warn", a[1] if len(a) > 1 else ""))
answers = {"yes": False}
g.messagebox.askyesno = lambda *a, **k: (notes.append(("ask", a[1] if len(a) > 1 else "")), answers["yes"])[1]
save_as = {"path": None}
g.filedialog.asksaveasfilename = lambda **k: save_as["path"]
g.filedialog.askopenfilename = lambda **k: save_as["path"]
app = g.App(root)
app.run_system_command = lambda cmd: commands.append(cmd)
def shot(name):
    root.update(); subprocess.run(["import", "-window", "root", str(SHOTS / f"{name}.png")])
def dialogs():
    return [w for w in root.winfo_children() if w.winfo_class() == "Toplevel"]
def close_dialogs():
    for w in dialogs(): w.destroy()
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
progress_seen = {}
steps = [
    # 1. a PDF loads into chapters
    (500, lambda: app.load_book("books/plain.pdf")),
    (400, lambda: (check(len(app.chapters) == 4 and app.title_lbl.cget("text") == "PDF Test Book", f"PDF loaded: {len(app.chapters)} chapters"), shot("01_pdf_loaded"))),
    # 2. dropping an unsupported file explains itself
    (200, lambda: (app.on_drop(types.SimpleNamespace(data="{/x/Some Book.mobi}", action="copy")),
                   check(notes and "Calibre" in notes[-1][1], "a dropped .mobi explains what to do"))),
    # 3. paste dialog
    (200, lambda: app.paste_text("Chapter 1\n\n" + "The harbor was quiet tonight and the keeper read by the lamp. " * 30 +
                                 "\n\nChapter 2\n\n" + "Morning came slowly over the water and the boats went out. " * 30)),
    (500, lambda: shot("02_paste_dialog")),
    (200, lambda: next(w for w in dialogs()).winfo_children()[0].winfo_children()[-1].winfo_children()[-1].invoke()),
    (500, lambda: (check(app.book and app.book.parent.name == "pasted" and len(app.chapters) == 2,
                         f"pasted text became a 2-chapter book: {app.book}"), shot("03_pasted_loaded"))),
    # 4. pronunciation editor
    (200, lambda: app.load_book("books/chaptered.txt")),
    (200, lambda: app.open_pronunciations()),
    (500, lambda: shot("04_pron_empty")),
    (200, lambda: add_rule("harbor", "har-ber")),
    (200, lambda: add_rule("K.E.V.I.N.", "Kevin")),
    (500, lambda: shot("05_pron_rules")),
    (200, lambda: save_pron()),
    (300, lambda: (check(app.pron_lbl.cget("text") == "2 in use", f"narration card shows rule count: {app.pron_lbl.cget('text')!r}"),
                   check(len(g.pron.load_rules()) == 2, "rules saved to pronunciations.json"))),
    # 5. convert with pause / resume, and an after-finishing action
    (200, lambda: (app.after_var.set(g.AFTER_ACTIONS["shutdown"]), app.start())),
    (2500, lambda: (app.toggle_pause(), progress_seen.__setitem__("a", app.bar["value"]))),
    (2500, lambda: (progress_seen.__setitem__("b", app.bar["value"]), shot("06_paused"),
                    check(g.PAUSE_FILE.exists() and app.pause_btn.cget("text").endswith("Resume"), "pause creates the flag and the button says Resume"))),
    (2000, lambda: (check(app.bar["value"] - progress_seen["b"] < 3, f"progress holds while paused ({progress_seen['b']:.0f}% -> {app.bar['value']:.0f}%)"),
                    app.toggle_pause())),
    (lambda: not app.running, lambda: (check(not g.PAUSE_FILE.exists(), "resume removes the flag; run finishes"),
                                       check(app.view == "done", f"finished screen shown ({app.view})"))),
    (2200, lambda: shot("07_countdown")),
    (300, lambda: (check(any(w.title() == "Audiobook Forge" for w in dialogs()), "after-finishing countdown appears"),
                   [b.invoke() for w in dialogs() for b in w.winfo_children()[0].winfo_children()[-1].winfo_children() if "now" in b.cget("text")])),
    (300, lambda: (check(commands and commands[-1][:2] == ["shutdown", "/s"], f"shutdown command issued: {commands}"),
                   check(app.after_var.get() == "Do nothing", "choice resets to Do nothing afterwards"))),
    (300, lambda: (check("har-ber" in Path("spoken.log").read_text() if Path("spoken.log").exists() else False, "the rule was used in the real conversion"))),
    # 6. watch folder
    (300, lambda: (app.nb.select(0), app.convert_another(), app.open_settings())),
    (600, lambda: shot("08_settings_watch")),
    (200, lambda: (close_dialogs(), app.set_setting("watch_dir", str(Path("watchme").resolve())), app.set_setting("watch_enabled", True), app.start_watcher())),
    (lambda: g.watchmod.read_status() is not None, lambda: subprocess.run(["cp", "books/notes.md", "watchme/notes.md"])),
    (lambda: (g.watchmod.read_status() or {}).get("state") == "converting", lambda: (app.update_watch_label(), shot("09_watch_converting"),
             check("converting notes.md" in app.watch_lbl.cget("text"), f"window shows the watcher: {app.watch_lbl.cget('text')!r}"))),
    (lambda: Path("watchme/done/notes.md").exists(), lambda: check(True, "watch folder converted the book and moved it to done/")),
    (300, lambda: (app.set_setting("watch_enabled", False), app.stop_watcher())),
    (lambda: g.watchmod.read_status() is None, lambda: check(True, "watcher stopped")),
    # 7. dark mode check of the new controls
    (300, lambda: (app.set_theme("dark"), app.load_book("books/sample.docx"), shot("10_docx_dark"))),
    # 8. new: narration settings, dialogue voice, spoken view
    (300, lambda: (app.set_theme("light"), app.load_book("books/story.txt"), app.open_settings())),
    (500, lambda: (check(settings_tabs() == ["Appearance", "Narration", "Sound", "Speed", "Output", "Watch folder"], f"settings has six tabs: {settings_tabs()}"), select_tab(1), shot("11_settings_narration"))),
    (300, lambda: (toggle_switch("Natural pauses"), check(app.settings["narration"].get("natural_pauses") is False, "a narration switch is saved"),
                   toggle_switch("Natural pauses"), close_dialogs())),
    (300, lambda: (app.dialogue_var.set(g.VOICES["am_adam"]), app.persist(),
                   check(json.load(open("settings.json")).get("dialogue_voice") == "am_adam", "dialogue voice saved"), shot("12_dialogue_voice"))),
    (300, lambda: app.show_chapter_text(1)),
    (400, lambda: (toggle_switch("Show it as it will be read (after fixes and clean-up)"), shot("13_as_read"),
                   check("Doctor Smith" in viewer_text() and "[4]" not in viewer_text() and "Figure 3" not in viewer_text(),
                         "chapter viewer shows the cleaned-up text"), close_dialogs())),
    (300, lambda: (Path("spoken.log").unlink(missing_ok=True), app.set_all(False), app.selected.add(1), app.update_summary(), app.start())),
    (lambda: not app.running, lambda: (check(app.view == "done", "converted with the new options"),
        check(any(l.startswith("am_adam\t") and "We should leave" in l for l in Path("spoken.log").read_text().splitlines()),
              "quoted speech used the dialogue voice in the real conversion"))),
    # 9. this round: settings tabs, he/she voices, name check, two at once
    (300, lambda: app.open_settings()),
    (400, lambda: (select_tab(1), shot("14_settings_narration2"))),
    (300, lambda: (select_tab(2), shot("15_settings_sound"), toggle_switch("Soft room tone"),
                   check(app.settings["narration"].get("room_tone") is True, "room tone switch saved"))),
    (300, lambda: (select_tab(3), shot("16_settings_speed"), toggle_switch("Two chapters at once (experimental)"),
                   check(app.settings["narration"].get("parallel") is True, "two-at-once switch saved"), close_dialogs())),
    (300, lambda: (app.dialogue_var.set(g.AUTO_VOICES), app.persist(),
                   check(json.load(open("settings.json")).get("dialogue_voice") == "auto", "he/she option saved as auto"))),
    (300, lambda: (Path("pronunciations.json").write_text('{"rules": []}'), app.update_pron_label(), app.load_book("books/round.txt"), app.check_names())),
    (lambda: len(names_rows()) >= 1,
     lambda: (shot("17_check_names"), check(names_rows()[:1] and names_rows()[0][0] in ("Natasha", "Tony", "Bruce"), f"names listed: {names_rows()[:4]}"))),
    (300, lambda: pick_name("Natasha")),
    (300, lambda: (set_say("nah-tah-shah"), shot("18_name_selected"), click("\u25B6 My version"))),
    (lambda: any(x.winfo_class() == "TButton" and x.cget("text") == "\u25B6 My version" and str(x.cget("state")) == "normal" for w in dialogs() for x in walk(w)),
     lambda: check(Path("models/previews/_sample.wav").exists() if Path("models/previews").exists() else g.PREVIEW_DIR.joinpath("_sample.wav").exists(),
                   "a sample with the new pronunciation was made")),
    (300, lambda: (click("Add fix"), check(any(r["find"] == "Natasha" and r["say"] == "nah-tah-shah" for r in g.pron.load_rules()), "Add fix saves the rule"),
                   check(app.pron_lbl.cget("text") == "1 in use", f"fix count updated: {app.pron_lbl.cget('text')}"), shot("19_name_fixed"), close_dialogs())),
    (300, lambda: (keep_rules.__setitem__("r", g.pron.load_rules()), app.check_names())),
    (lambda: len(names_rows()) >= 1, lambda: click("Suggest with AI")),
    (lambda: not app.ai_busy and any("Pick a word" in str(x.cget("text")) for w in dialogs() for x in walk(w) if x.winfo_class() == "TLabel"),
     lambda: (shot("19b_suggestions"),
              check(any(r[3] for r in names_rows()) and not [r for r in names_rows() if r[0] == "Natasha" and r[3]],
                    f"AI suggestions listed for words without a fix: {[r[:4] for r in names_rows()][:3]}"),
              pick_name(next(r[0] for r in names_rows() if r[3])),
              check(say_value() == next(r[3] for r in names_rows() if r[3]), f"picking a word fills in the suggestion: {say_value()}"),
              answers.__setitem__("yes", True), click("Add all suggestions"), answers.__setitem__("yes", False),
              check(len(g.pron.load_rules()) == 1 + sum(1 for r in names_rows() if r[3]), "Add all suggestions saves them as fixes"),
              g.pron.save_rules(keep_rules["r"]), app.update_pron_label(), close_dialogs())),
    (300, lambda: (Path("spoken.log").unlink(missing_ok=True), app.set_all(True), app.update_summary(), app.start())),
    (1500, lambda: (shot("20_two_at_once"), check(len(app.partial) >= 1, f"progress tracked per chapter: {app.partial}"))),
    (lambda: not app.running, lambda: (check(app.view == "done", "two-at-once conversion finished in the window"),
        check(len(app.done) == 4, f"all chapters marked done: {sorted(app.done)}"),
        check(any(l.startswith("af_bella\t") and "We need to leave" in l for l in Path("spoken.log").read_text().splitlines()), "she-speech used the women's voice"),
        check(any("nah-tah-shah" in l for l in Path("spoken.log").read_text().splitlines()), "the new name fix was used"),
        shot("21_done"))),
    # 10. this round: export/import fixes, footnotes, samples, output, casting, diagnostics
    (300, lambda: (close_dialogs(), app.load_book("books/round.txt"), app.open_pronunciations())),
    (400, lambda: (add_rule("ferry", "boat"), save_as.__setitem__("path", str(Path("fixes_export.json").resolve())), click("Export..."),
                   check(Path("fixes_export.json").exists() and "ferry" in Path("fixes_export.json").read_text(), "fixes exported to a file"))),
    (300, lambda: (Path("fixes_in.txt").write_text("Thanos = than-ohss\nOkoye, oh-koy-ay\n"), save_as.__setitem__("path", str(Path("fixes_in.txt").resolve())),
                   click("Import..."), shot("22_import"),
                   check(any(x.winfo_class() == "TLabel" and "2 added" in str(x.cget("text")) for w in dialogs() for x in walk(w)), "import reports 2 added"))),
    (300, lambda: (click("Save"), check(any(r["find"] == "Thanos" for r in g.pron.load_rules()), "imported fixes saved"))),
    (300, lambda: app.open_settings()),
    (400, lambda: (select_tab(1), shot("23_settings_narration_notes"), pick_combo("Read them at the end of each chapter"),
                   check(app.settings["narration"].get("footnotes") == "end", "footnote choice saved"))),
    (300, lambda: (select_tab(4), shot("24_settings_output"), pick_radio("A folder of MP3s, one per chapter"),
                   pick_radio("High: 96 kbps, about 43 MB per hour"), toggle_switch("A title card picture for every chapter"),
                   check(app.settings.get("output") == {"format": "mp3_chapters", "quality": "high", "chapter_art": True}, f"output choice saved: {app.settings.get('output')}"))),
    (300, lambda: (select_tab(0), shot("25_settings_help"), save_as.__setitem__("path", str(Path("diag.zip").resolve())), click("Save diagnostics..."))),
    (lambda: Path("diag.zip").exists(), lambda: (check(True, "diagnostics saved from Settings"), close_dialogs())),
    (300, lambda: (Path("models/previews/_book_sample.wav").unlink(missing_ok=True), app.book_sample())),
    (lambda: not app.sample_busy, lambda: (shot("26_sample"), check(g.PREVIEW_DIR.joinpath("_book_sample.wav").exists() and "Playing a sample" in app.preview_lbl.cget("text"),
                                                                     f"book sample made: {app.preview_lbl.cget('text')!r}"))),
    (300, lambda: app.show_chapter_text(1)),
    (400, lambda: (shot("27a_chapter_card"), click_text_line(3), click("\u25B6 Hear paragraph"))),
    (lambda: not app.sample_busy, lambda: (shot("27_hear_paragraph"), check(highlighted().startswith('"We need to leave'), f"the clicked paragraph is highlighted: {highlighted()[:40]!r}"),
                                           check(any("Playing" in str(x.cget("text")) for w in dialogs() for x in walk(w) if x.winfo_class() == "TLabel"), "paragraph sample played"),
                                           close_dialogs())),
    (300, lambda: (app.load_book("books/cast.txt"), app.cast_characters())),
    (lambda: any("character" in str(x.cget("text")) and "found" in str(x.cget("text")) for w in dialogs() for x in walk(w) if x.winfo_class() == "TLabel"),
     lambda: (shot("28_cast"), check(cast_names()[:3] == ["Pepper", "Natasha", "Tony"], f"characters found: {cast_names()}"))),
    (300, lambda: (check(any(x.winfo_class() == "TLabelframe" and x.cget("text") == "Find speakers with AI" for w in dialogs() for x in walk(w)),
                         "the cast window offers Find speakers with AI"), click("Read the book"))),
    (lambda: not app.ai_busy and any("now uses them" in str(x.cget("text")) for w in dialogs() for x in walk(w) if x.winfo_class() == "TLabel"),
     lambda: (shot("28b_cast_ai"),
              check(Path("casting/cast.speakers.json").exists() and any(json.loads(Path("casting/cast.speakers.json").read_text())["chapters"].values()),
                    "the AI's speaker tags were saved for the book"),
              check(json.loads(Path("casting/cast.speakers.json").read_text())["genders"].get("Tony") == "m", "the AI's genders were saved"),
              check(app.settings["narration"].get("ai_speakers") is True, "using the AI tags is switched on"))),
    (lambda: any("character" in str(x.cget("text")) and "found" in str(x.cget("text")) for w in dialogs() for x in walk(w) if x.winfo_class() == "TLabel"),
     lambda: (check("Tony" in cast_names(), f"the cast is rebuilt with the AI tags: {cast_names()}"),
              app.__setattr__("ai_busy", True), app.start(),
              check(not app.running and any("AI is still reading" in n[1] for n in notes), "no conversion while the AI uses the graphics card"),
              app.__setattr__("ai_busy", False))),
    (300, lambda: (set_cast_voice("Tony", g.VOICES["am_puck"]), click("Save"),
                   check(json.loads(Path("casting/cast.json").read_text())["voices"]["Tony"] == "am_puck", "cast saved per book"),
                   check(app.dialogue_var.get() == g.CAST_VOICES, "saving a cast switches quoted speech to per-character"))),
    (300, lambda: (Path("spoken.log").unlink(missing_ok=True), app.set_all(True), app.update_summary(), app.start())),
    (lambda: not app.running, lambda: (check(app.view == "done", "per-character conversion finished in the window"),
        check(any(l.startswith("am_puck\t") and "never late" in l for l in Path("spoken.log").read_text().splitlines()), "Tony spoke with the chosen voice"),
        check(Path(app.last_output).is_dir() and len(list(Path(app.last_output).glob("*.mp3"))) == 1, f"output was a folder of MP3s: {app.last_output}"),
        shot("29_done_mp3"))),
    (300, lambda: (app.nb.select(1), app.refresh_library())),
    (1500, lambda: (shot("30_library"), check(any("cast" in t["text"] for t in app.lib_tiles), f"Library shows the MP3 folder book: {[t['text'] for t in app.lib_tiles][:5]}"),
                    check(any(x.winfo_class() == "TButton" and x.cget("text") == "Report" for x in walk(app.lib_inner)), "Library tiles offer the listening report"),
                    app.nb.select(0))),
    # 11. this round: keep awake, mixer, setup check, cleanup, report, queue
    (300, lambda: (app.nb.select(0), app.convert_another() if app.view == "done" else None, app.open_settings())),
    (400, lambda: (select_tab(0), toggle_switch("Keep the PC awake (the screen can still turn off)"),
                   check(app.settings.get("keep_awake") is False, "keep-awake switch saved"),
                   toggle_switch("Keep the PC awake (the screen can still turn off)"), shot("31_settings_awake"), close_dialogs())),
    (300, lambda: app.open_mixer()),
    (400, lambda: (shot("32_mixer"), set_entry_value("My voice", "Warm Mix"), click("\u25B6 Hear it"))),
    (lambda: g.PREVIEW_DIR.joinpath("_mix.wav").exists() or any("Could not" in str(x.cget("text")) for w in dialogs() for x in walk(w) if x.winfo_class() == "TLabel"),
     lambda: (check(g.PREVIEW_DIR.joinpath("_mix.wav").exists(), "a mix can be heard before saving"), click("Save"))),
    (lambda: any("Saved" in str(x.cget("text")) or "Could not" in str(x.cget("text")) for w in dialogs() for x in walk(w) if x.winfo_class() == "TLabel"),
     lambda: (shot("33_mix_saved"), check("custom:Warm Mix" in g.VOICES and g.VOICES["custom:Warm Mix"] in app.voice_box.cget("values"),
                                          "the saved mix appears in the voice list"))),
    (300, lambda: (pick_listbox(0), click("Use as narrator"), check(app.voice_id() == "custom:Warm Mix", f"the mix is the narrator: {app.voice_id()}"))),
    (300, lambda: (answers.__setitem__("yes", True), click("Delete"), answers.__setitem__("yes", False),
                   check("custom:Warm Mix" not in g.VOICES and app.voice_id() == "af_heart", f"deleting the mix puts Heart back: {app.voice_id()}"),
                   close_dialogs())),
    (300, lambda: (g.PAUSE_FILE.write_text("x"), app.check_setup())),
    (lambda: any("Everything" in str(x.cget("text")) or "problem" in str(x.cget("text")) for w in dialogs() for x in walk(w) if x.winfo_class() == "TLabel"),
     lambda: (shot("34_setup_check"), check(any("Leftover files" in v for v in setup_rows()), f"setup check finds the leftover pause file: {setup_rows()[:3]}"),
              pick_setup_row("Leftover files"))),
    (300, lambda: (click("Remove the leftover files"))),
    (lambda: not g.PAUSE_FILE.exists() and setup_rows(), lambda: (check(True, "the fix button removed it"), close_dialogs())),
    (300, lambda: (Path("work/Old Book/audio-af_heart-1-abc").mkdir(parents=True, exist_ok=True),
                   Path("work/Old Book/audio-af_heart-1-abc/01.wav").write_bytes(b"0" * 300000), app.cleanup_work())),
    (800, lambda: (shot("35_cleanup"), click("Select all"), answers.__setitem__("yes", True), click("Delete selected"))),
    (lambda: not Path("work/Old Book").exists(), lambda: (answers.__setitem__("yes", False), check(True, "cleanup deleted the leftover work folder"), close_dialogs())),
    (300, lambda: (app.dialogue_var.set(g.SAME_VOICE), app.persist(), app.set_setting("output", {"format": "m4b", "quality": "standard"}),
                   app.load_book("books/report.txt"), Path("spoken.log").unlink(missing_ok=True), app.start())),
    (lambda: not app.running, lambda: (shot("36_done_report"), check(app.report_btn.winfo_ismapped(), f"the finished screen offers the listening report: {app.report_btn.cget('text')!r}"),
                                       app.open_report())),
    (500, lambda: (shot("37_report"), check(len(report_rows()) >= 2, f"the report lists places to check: {report_rows()[:3]}"),
                   select_report_row(0), click("\u25B6 Hear it"))),
    (lambda: g.PREVIEW_DIR.joinpath("_report_clip.wav").exists(), lambda: (check(True, "a clip from that spot was made"), click("Check this book"))),
    (lambda: any("Checked by listening on" in str(x.cget("text")) or "could not" in str(x.cget("text")).lower()
                 for w in dialogs() for x in walk(w) if x.winfo_class() == "TLabel"),
     lambda: (shot("37a_checked"), check(any(r[2] in ("Words missing", "End cut off") for r in report_rows()), f"listening results appear in the report: {[r[2] for r in report_rows()][:5]}"),
              check(json.loads(Path(app.report_path).read_text()).get("check", {}).get("checked", 0) >= 3, "the check is saved with the book's report"),
              close_dialogs())),
    # queue: the first book cannot start, the second converts, and the failed one can be retried
    (300, lambda: (app.set_batch([Path("books/round.txt").resolve(), Path("books/report.txt").resolve()]), break_next_start())),
    (lambda: not app.loading_batch and app.view == "batch", lambda: app.start_batch()),
    (lambda: app.view == "done" and not app.running, lambda: (shot("38_queue_summary"),
        check([r[1] is None for r in app.batch_results] == [True, False], f"the queue went on after a book failed: {[(r[0], bool(r[1]), r[2]) for r in app.batch_results]}"),
        check("could not start" in (app.batch_results[0][2] or ""), "the summary says why it failed"),
        check(app.retry_btn.winfo_ismapped(), "a retry button is offered"))),
    (300, lambda: app.retry_failed()),
    (lambda: app.view == "done" and not app.running, lambda: check(len(app.batch_results) == 1 and app.batch_results[0][1], "retrying converts the failed book")),
    # folders of books
    (300, lambda: (make_library(), app.convert_another() if app.view == "done" else None, notes.clear(), app.open_paths([str(Path("lib").resolve())]))),
    (lambda: not app.loading_batch, lambda: (shot("40_folder_queue"),
        check(app.view == "batch", f"a dropped folder becomes a queue: {app.view}"),
        check([Path(b[0]).name for b in app.batch] == ["A.epub", "Book 2.txt", "Book 10.txt", "Book 3.md"],
              f"books found in the folder and its subfolder, in order: {[Path(b[0]).name for b in app.batch]}"),
        check(any(n[0] == "warn" and "more than one format" in n[1] and "Kindle" in n[1] for n in notes),
              f"duplicates and Kindle files are explained: {notes}"))),
    (300, lambda: (notes.clear(), app.open_paths([str(Path("lib/kindle").resolve())]),
                   check(any("No books were found in that folder" in n[1] and ".mobi" in n[1] for n in notes), f"a folder with no books says so: {notes}"))),
    (300, lambda: (notes.clear(), app.load_book("books/round.txt"), setattr(g.filedialog, "askdirectory", lambda **k: str(Path("lib/sub").resolve())),
                   app.browse_folder())),
    (lambda: not app.loading_batch, lambda: (check(app.view == "batch" and [Path(b[0]).name for b in app.batch] == ["Book 3.md"] or
                                                   (app.book and app.book.name == "Book 3.md"), f"Choose > A folder of books works: {app.view}"),
                                             check(app.settings.get("last_book_folder", "").endswith("sub"), "the folder is remembered"))),
    (300, lambda: (notes.clear(), app.open_paths([str(Path("lib").resolve()), str(Path("lib/Book 2.txt").resolve())]))),
    (lambda: not app.loading_batch, lambda: check(len(app.batch) == 4, f"a book dropped twice (alone and in its folder) is queued once: {len(app.batch)}")),
]
def make_library():
    import shutil
    shutil.rmtree("lib", ignore_errors=True)
    Path("lib/sub").mkdir(parents=True); Path("lib/kindle").mkdir()
    shutil.copy("books/harbor.epub", "lib/A.epub"); shutil.copy("books/plain.pdf", "lib/A.pdf")
    shutil.copy("books/round.txt", "lib/Book 10.txt"); shutil.copy("books/report.txt", "lib/Book 2.txt")
    shutil.copy("books/notes.md", "lib/sub/Book 3.md"); Path("lib/x.mobi").write_text("x"); Path("lib/~$temp.docx").write_text("x")
    Path("lib/kindle/y.mobi").write_text("y"); Path("lib/kindle/z.azw3").write_text("z")
def set_entry_value(old, new):
    for w in dialogs():
        for x in walk(w):
            if x.winfo_class() == "TEntry" and x.get() == old:
                x.delete(0, "end"); x.insert(0, new); return
    raise RuntimeError("entry not found")
def pick_listbox(i):
    for w in dialogs():
        for x in walk(w):
            if x.winfo_class() == "Listbox":
                x.selection_clear(0, "end"); x.selection_set(i); return
def tree_in_dialog():
    return next(x for w in dialogs() for x in walk(w) if x.winfo_class() == "Treeview")
def setup_rows():
    try:
        t = tree_in_dialog()
        return [t.item(i, "values")[1] for i in t.get_children()]
    except StopIteration:
        return []
def pick_setup_row(title):
    t = tree_in_dialog()
    for i in t.get_children():
        if t.item(i, "values")[1] == title:
            t.selection_set(i); t.event_generate("<<TreeviewSelect>>"); root.update(); return
def report_rows():
    t = tree_in_dialog()
    return [t.item(i, "values") for i in t.get_children()]
def select_report_row(n):
    t = tree_in_dialog(); t.selection_set(t.get_children()[n])
real_popen = g.subprocess.Popen
def break_next_start():
    state = {"n": 0}
    def popen(cmd, *a, **k):
        if "convert.py" in " ".join(map(str, cmd)) and "--sample" not in cmd and state["n"] == 0:
            state["n"] += 1
            raise OSError("test: cannot start")
        return real_popen(cmd, *a, **k)
    g.subprocess.Popen = popen
def pick_combo(value):
    for w in dialogs():
        for x in walk(w):
            if x.winfo_class() == "TCombobox" and value in x.cget("values"):
                x.set(value); x.event_generate("<<ComboboxSelected>>"); return
    raise RuntimeError("combobox value not found: " + value)
def pick_radio(label):
    for w in dialogs():
        for x in walk(w):
            if x.winfo_class() == "TRadiobutton" and x.cget("text") == label:
                x.invoke(); return
    raise RuntimeError("radio not found: " + label)
def text_widget():
    return next(x for w in dialogs() for x in walk(w) if x.winfo_class() == "Text")
def click_text_line(line):
    t = text_widget(); t.see(f"{line}.0"); root.update()
    x, y, _, _ = t.bbox(f"{line}.0")
    t.event_generate("<Button-1>", x=x + 2, y=y + 2)
def highlighted():
    t = text_widget(); r = t.tag_ranges("hl")
    return t.get(r[0], r[1]) if r else ""
def cast_rows():
    w = dialogs()[-1]
    labels = [x for x in walk(w) if x.winfo_class() == "TLabel" and int(x.grid_info().get("column", -1) if x.grid_info() else -1) == 0]
    return labels
def cast_names():
    return [x.cget("text") for x in cast_rows()][1:]
def set_cast_voice(name, label):
    w = dialogs()[-1]
    for x in walk(w):
        if x.winfo_class() == "TCombobox" and x.grid_info() and int(x.grid_info()["row"]) == cast_names().index(name) + 1:
            x.set(label); return
    raise RuntimeError("cast row not found")
def names_rows():
    for w in dialogs():
        for x in walk(w):
            if x.winfo_class() == "Treeview":
                return [x.item(i, "values") for i in x.get_children()]
    return []
def pick_name(word):
    for w in dialogs():
        for x in walk(w):
            if x.winfo_class() == "Treeview":
                for i in x.get_children():
                    if x.item(i, "values")[0] == word:
                        x.selection_set(i); x.event_generate("<<TreeviewSelect>>"); return
keep_rules = {}
def say_value():
    for w in dialogs():
        for x in walk(w):
            if x.winfo_class() == "TEntry":
                return x.get()
def set_say(text):
    for w in dialogs():
        for x in walk(w):
            if x.winfo_class() == "TEntry":
                x.delete(0, "end"); x.insert(0, text); return
def click(label):
    for w in dialogs():
        for x in walk(w):
            if x.winfo_class() == "TButton" and x.cget("text") == label:
                x.invoke(); return
    raise RuntimeError("button not found: " + label)
def settings_tabs():
    for w in dialogs():
        for x in walk(w):
            if x.winfo_class() == "TNotebook":
                return [x.tab(t, "text").strip() for t in x.tabs()]
    return []
def select_tab(i):
    for w in dialogs():
        for x in walk(w):
            if x.winfo_class() == "TNotebook":
                x.select(i); return
def toggle_switch(label):
    for w in dialogs():
        for x in walk(w):
            if x.winfo_class() == "TCheckbutton" and x.cget("text") == label:
                x.invoke(); return
    raise RuntimeError("switch not found: " + label)
def viewer_text():
    for w in dialogs():
        for x in walk(w):
            if x.winfo_class() == "Text":
                return x.get("1.0", "end")
    return ""

def add_rule(f, s):
    w = dialogs()[-1]
    entries = [x for x in walk(w) if x.winfo_class() == "TEntry"]
    entries[0].delete(0, "end"); entries[0].insert(0, f); entries[1].delete(0, "end"); entries[1].insert(0, s)
    next(x for x in walk(w) if x.winfo_class() == "TButton" and x.cget("text") == "Add").invoke()
def save_pron():
    w = dialogs()[-1]
    next(x for x in walk(w) if x.winfo_class() == "TButton" and x.cget("text") == "Save").invoke()
def walk(w):
    out = [w]
    for c in w.winfo_children(): out += walk(c)
    return out
def run(i=0, t0=None):
    if i >= len(steps):
        print("ERRORS:", len(errors), "notes:", notes, flush=True)
        g.watchmod.STOP_FILE.write_text("x"); root.destroy(); return
    cond, act = steps[i]
    if callable(cond):
        t0 = t0 or time.time()
        if not cond():
            if time.time() - t0 > 90:
                print("TIMEOUT at step", i, flush=True); g.watchmod.STOP_FILE.write_text("x"); root.destroy(); return
            root.after(250, lambda: run(i, t0)); return
        delay = 300
    else:
        delay = cond
    def go():
        try: act()
        except Exception: report(*sys.exc_info())
        run(i + 1)
    root.after(delay, go)
run()
root.mainloop()
