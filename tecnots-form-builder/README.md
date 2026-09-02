# Formulate — AI-Powered Form Builder & Document Autofill
## Project structure

```
tecnots-form-builder/
  backend/
    main.py            FastAPI service: validation + document → Claude extraction
    requirements.txt
    .env.example
  frontend/
    index.html         Single-file React app: builder → upload → review/save
  README.md
```

## How to run it locally

### 1. Backend

```bash
cd backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then paste your Gemini API key into .env
uvicorn main:app --reload --port 8000
```

Check it's alive: open `http://localhost:8000/api/health` — should return
`{"status": "ok", ...}`.

### 2. Frontend

No build step — it's a single HTML file using React via CDN.

```bash
cd frontend
python -m http.server 5500
```

Then open `http://localhost:5500` in your browser. If your backend runs somewhere other
than `http://localhost:8000`, set it before the page loads by adding, in `index.html`'s
`<head>`, a line like:

```html
<script>window.TECNOTS_API_BASE = "http://localhost:8000";</script>
```

(Opening `index.html` directly via `file://` also works for the builder/preview; the
extraction step needs it served over `http://` so the browser's fetch/CORS behaves.)

## Walking through the product

1. **Build** — add fields, pick a type (single-line text, multi-line text, number, date,
   dropdown, checkbox), mark required/optional, drag the `⠿` handle to reorder, and watch the
   live preview update. Save the form as a reusable template, or export/import the schema as
   JSON.
2. **Upload** — drag or browse a PDF/PNG/JPG/JPEG. Wrong file types are rejected with a message
   before anything is sent to the server.
3. **Extract** — the backend sends the document plus your exact field schema to Claude and asks
   it to return a value, a confidence level, and whether it found each field — nothing else.
   Fields the model can't confidently support are left blank rather than guessed. Fields fill in
   one-by-one in the UI to make the extraction feel live.
4. **Review & save** — every field is editable. Required fields still empty are flagged in a
   banner and marked "needs input" inline. Save stores the completed form to the browser
   (`localStorage`, since there's no user auth/database in scope for this assessment) or export
   it as a plain JSON file.

## Technology choices and why

- **FastAPI (Python)** for the backend — quick to wire up file upload validation, plays nicely
  with `python-multipart` for `multipart/form-data`, and has first-class typing for the JSON
  responses this task needs.
- **PyMuPDF (`fitz`)** to read PDFs. It extracts embedded text directly for text-based PDFs
  (cheaper, more accurate) and can rasterize pages to images for scanned/photographed PDFs, so
  the same endpoint handles both cases by checking how much real text a PDF actually contains.
- **Google Gemini (via the `google-genai` SDK)**, default model `gemini-2.5-flash`, to call the
  model directly with either extracted text or page/photo images (native vision support), rather
  than running a separate OCR step — one model call reads and reasons about the document. The
  request sets `response_mime_type="application/json"` so the API itself enforces valid JSON
  output.
- **Plain React via CDN + Babel standalone** on the frontend — zero build tooling, so "clone and
  open" is genuinely all that's needed to see the UI, while still getting real component state
  and JSX ergonomics for the builder/preview/review views.
- **`localStorage`** for templates and saved submissions — there's no requirement for multi-user
  persistence in the brief, so a database would be over-engineering for this scope.

## Schema-driven extraction, concretely

The frontend never sends fixed field names to the backend — it sends whatever field list the
user built, e.g.:

```json
[{"id": "f_a1b2c3", "label": "Candidate Name", "type": "text", "required": true, "options": []}]
```

The backend forwards that exact list to Claude inside the prompt and instructs it to return an
object keyed by those same `id`s — so a résumé form and an invoice form hit the identical code
path with completely different fields.

## Edge cases handled

| Situation | Behaviour |
|---|---|
| Required field has no match in the document | Left blank, flagged "needs input" in review |
| Upload attempted with zero fields built | Blocked client-side with a message to build the form first; also rejected server-side (400) as a safety net |
| Corrupted or unsupported file | Client rejects wrong extensions immediately; server also validates the file actually opens (PDF via PyMuPDF, image via Pillow) and returns a clear 400 error if not |
| Number field with no numeric value found | Model is instructed to return `null`, which renders as blank rather than "0" or a guess |
| Model isn't confident about a field | Explicit prompt rule: null value + `found: false` instead of a low-confidence fill |

## Assumptions & trade-offs

- Single-user, no auth — saved templates/submissions live in the browser's `localStorage`, not
  a shared database. Fine for a take-home; would move to a real DB + auth for production.
- PDF handling caps scanned documents at the first 4 pages sent as images, to keep latency and
  token cost reasonable for a demo; easy to raise via `MAX_PDF_PAGES_AS_IMAGES` in `main.py`.
- Extraction is one model call per document rather than field-by-field calls — cheaper and lets
  the model use whole-document context (e.g. inferring "years of experience" from dates), at the
  cost of true per-field streaming.
- `GEMINI_MODEL` is read from an environment variable so it's easy to switch between
  vision-capable Gemini models (`gemini-2.5-flash`, `gemini-2.5-pro`, etc.) without touching code.

## Testing it yourself

A quick smoke test: build a form with "Candidate Name" (text, required), "Email Address" (text,
required), "Skills" (multi-line text), and "Years of Experience" (number). Upload any resume PDF
or a photo of one. Confirm values fill in, an intentionally-missing field (e.g. add a "LinkedIn
URL" field the resume doesn't have) is left blank and flagged, and edits + save work.
