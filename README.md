# Audiobook Forge

Converts EPUB, PDF, Word, text and web-page books into audiobooks (M4B or MP3) using the Kokoro
text-to-speech model on a local GPU. Built for Windows with an AMD Radeon GPU via ROCm.

## Features

- Drop a single book, several books, or a whole folder (subfolders included) into the window
- Per-character voices, footnotes read at the end of each chapter, chapter title cards
- Pronunciation fixes, name check, and an optional local language model for speaker tagging
- Listening report and optional check of the finished audio with Whisper

## Setup

The app needs a bundled Python runtime with PyTorch (ROCm build), Kokoro, transformers and ffmpeg.
See `installer/` for the Inno Setup installer and `installer/build_installer.ps1` for how the
runtime and ffmpeg are packaged.

## Running

    "Audiobook Forge.bat"          (opens the window)
    "Convert Book.bat" <book>      (command-line conversion)

## Layout

| File | What it does |
|---|---|
| `forge_gui.py` | The window |
| `convert.py` | Converts a book to audio |
| `books.py`, `epub_reader.py` | Reads the book formats into chapters |
| `speakers.py`, `speakertag.py`, `llm.py` | Who speaks each line (rules, and the optional local model) |
| `normalize.py`, `pronunciations.py` | Text clean-up and pronunciation fixes |
| `bookcheck.py` | Optional listening check with Whisper |
| `voicemix.py` | Custom voice mixes |
| `checks.py`, `selfcheck.py`, `diagnostics.py` | Disk/memory checks, setup check, diagnostics zip |

## Tests

    runtime\python\python.exe tests\run_all.py          (or any Python with the app's packages)

Runs every suite in a temporary copy of the app with small sample books and stand-ins for Kokoro, torch and
transformers (tests/stubs), so no graphics card, model download or real book is needed: conversion in every
format, readers, casting, the listening report, the AI speaker tags, the watch folder, and a tour of the whole
window. `python tests/run_all.py convert` runs only suites whose name contains "convert".

What the tests cannot check: how the real voice sounds, real graphics-card speed, and the ROCm runtime. Those
need a conversion on the PC (bench3.py measures speed; Settings > Speed > Compare by ear checks half precision).

## Building

- `installer\Build installer.bat` makes `installer\Output\Audiobook Forge Setup <VERSION>.exe` (Inno Setup 6.5.2+).
  The version comes from the `VERSION` file.
- `installer\build_exe.ps1` rebuilds `Audiobook Forge.exe` (the launcher) with PyInstaller in C:\forge-build. Only
  needed when `forge_launcher.py` or `forge.ico` change; the .py files never need a new exe.

## License

GNU General Public License v3.0 (GPL-3.0). Copyright (c) 2026 Hans De Kryger. See `LICENSE`.
