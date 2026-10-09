# Continuity

Continuity is a simple project-memory assistant. Paste an AI chat or project notes, upload documents/images, choose the output you need, and get a useful result while Continuity updates persistent project memory in the background.

## What it does

- **Summary:** extracts key points from the material.
- **Final conclusion:** gives the bottom line, recommendation and important caveats.
- **Project status:** organizes the work into goal, current approach, completed/approved items, pending/next steps and an overall summary.
- **Persistent memory:** saves clear, explicit, high-confidence items when they have a matching source excerpt and no detected conflict.
- **Needs your input:** only shows items that are uncertain, conflict with saved decisions or lack sufficient evidence.
- **Saved memory:** shows current project memory and lets you inspect source excerpts; change history remains available in the underlying audit records.
- **Resume from memory:** creates a project-status context from the current approved memory.

The UI is one main workspace. There is no feature-heavy navigation, separate review queue screen or separate Lexicon screen.

## Tech stack

- HTML, CSS and vanilla JavaScript
- Python and FastAPI
- SQLite and SQLAlchemy
- Optional OpenAI API for LLM interpretation and image understanding
- `pypdf` for selectable text in PDFs and `python-docx` for Word documents

## Requirements

- Python 3.10+ (Python 3.11+ recommended)
- Install dependencies from `requirements.txt`
- An OpenAI API key is optional. Without a key, text and text-based documents can be organized by the basic local mode. Images require a working AI API key. Local mode is deliberately limited and only auto-saves lines that are explicitly labelled, such as `Goal:`, `Decision:`, `Completed:`, `Pending:` or `Next step:`.

## Run on Windows PowerShell

Open PowerShell in the folder that contains `main.py` and `requirements.txt`.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

Open <http://127.0.0.1:8000>. API documentation: <http://127.0.0.1:8000/docs>.

If the `py` launcher is not available, install Python from python.org and enable the Python launcher during installation.

## Configure AI (optional)

Copy `.env.example` to `.env`, then set:

```dotenv
OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-4o-mini
```

Never commit `.env` or expose your API key. When configured, submitted text and images are sent to the selected model provider for processing. Original image bytes are used for the request but are not saved to the database; extracted source text, memory and the generated result are saved locally.

## Accepted inputs

- Paste text or an exported AI conversation into the main input.
- Upload `.txt`, `.md`, `.csv`, `.json`, text-based `.pdf` or `.docx` documents.
- Upload `.png`, `.jpg`, `.jpeg`, `.webp` or `.gif` images when AI is configured.
- Maximum 8 files, 8 MB per file and 16 MB combined.

Scanned PDFs that contain no selectable text are not OCR-processed in this MVP. Upload screenshots of relevant pages instead if AI image understanding is configured.

## Project structure

```text
continuity_project/
├── main.py                 # FastAPI routes, processing and memory rules
├── database.py             # SQLite / SQLAlchemy models
├── static/
│   ├── index.html           # Single workspace UI
│   ├── style.css            # UI styles
│   └── app.js               # UI interactions and API calls
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```
