"""
Tecnots AI Engineer Assessment — Question 1
AI-Powered Form Builder & Document Autofill — Backend (Google Gemini version)

Responsibilities of this backend:
  1. Accept a user-defined form schema + an uploaded document (PDF/PNG/JPG/JPEG).
  2. Validate the file (type, size, corruption).
  3. Turn the document into content the model can read (extracted text for
     text-based PDFs, or page images for scanned PDFs / photos).
  4. Ask the model to extract a value for every field in the schema — and ONLY
     the fields in the schema, since the schema is fully dynamic.
  5. Return a strict JSON result: {field_id: {value, confidence, found}}.

Run with:  uvicorn main:app --reload --port 8000
"""

import io
import json
import os

import fitz  # PyMuPDF (installed as the "PyMuPDF" package, imported as pymupdf/fitz)
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
from google import genai
from google.genai import types

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

if not GEMINI_API_KEY:
    # We don't crash on import so the health endpoint still works,
    # but /api/extract will refuse to run without a key.
    print("WARNING: GEMINI_API_KEY is not set. /api/extract will fail until it is.")

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

app = FastAPI(title="Tecnots Form Autofill API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev-friendly; tighten for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
MAX_FILE_SIZE_BYTES = 12 * 1024 * 1024  # 12 MB
MAX_PDF_PAGES_AS_IMAGES = 4  # cap cost/latency for scanned PDFs


@app.get("/api/health")
def health():
    return {"status": "ok", "model": GEMINI_MODEL, "api_key_configured": bool(GEMINI_API_KEY)}


def _extension_of(filename: str) -> str:
    return os.path.splitext(filename.lower())[1]


def _media_type_for_image(ext: str) -> str:
    return "image/png" if ext == ".png" else "image/jpeg"


def _pdf_to_content_parts(file_bytes: bytes):
    """
    Returns a list of Gemini content parts representing the PDF.
    Text-based PDFs -> a single text part with the extracted text.
    Scanned/image-only PDFs -> image parts, one per page (capped).
    Raises ValueError if the PDF cannot be opened (corrupted/unsupported).
    """
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"Could not open PDF — it may be corrupted. ({exc})")

    if doc.page_count == 0:
        raise ValueError("The PDF has no pages.")

    extracted_text = []
    for page in doc:
        extracted_text.append(page.get_text("text"))
    joined_text = "\n".join(extracted_text).strip()

    # Heuristic: if there's meaningful text, treat as a text-based PDF.
    if len(joined_text) >= 40:
        return [f"DOCUMENT TEXT CONTENT:\n{joined_text}"]

    # Otherwise treat as scanned — rasterize pages to images for vision.
    parts = []
    for i, page in enumerate(doc):
        if i >= MAX_PDF_PAGES_AS_IMAGES:
            break
        pix = page.get_pixmap(dpi=150)
        png_bytes = pix.tobytes("png")
        parts.append(types.Part.from_bytes(data=png_bytes, mime_type="image/png"))
    if not parts:
        raise ValueError("Could not extract any readable content from the PDF.")
    return parts


def _image_to_content_parts(file_bytes: bytes, ext: str):
    """Validates the image can actually be opened, then returns a Gemini image part."""
    try:
        Image.open(io.BytesIO(file_bytes)).verify()
    except Exception as exc:
        raise ValueError(f"Could not open image — it may be corrupted. ({exc})")

    media_type = _media_type_for_image(ext)
    return [types.Part.from_bytes(data=file_bytes, mime_type=media_type)]


def _build_system_prompt() -> str:
    return (
        "You are a precise document data-extraction engine embedded in a form-autofill product. "
        "You will be given (a) a JSON list describing the fields of a dynamic form the user built, "
        "and (b) a document (as text and/or images). Your job is to extract a value for each field "
        "from the document.\n\n"
        "STRICT RULES:\n"
        "1. Only use evidence explicitly present in the document. Never invent, assume, or guess.\n"
        "2. If a field's value cannot be confidently determined, set \"value\" to null and "
        "\"found\" to false. Do not fill it with a low-confidence guess.\n"
        "3. For \"number\" fields, \"value\" must be a plain JSON number (no currency symbols, "
        "units, or commas) or null.\n"
        "4. For \"checkbox\" fields, \"value\" must be boolean true/false only if the document "
        "explicitly supports it (e.g. a signed agreement / explicit yes); otherwise null.\n"
        "5. For \"dropdown\" fields, choose the single closest matching option from the field's "
        "\"options\" list, or null if none match confidently. Never invent an option not listed.\n"
        "6. For \"date\" fields, normalize to YYYY-MM-DD when the document gives enough "
        "information, otherwise null.\n"
        "7. Every field you do find a value for must include a \"confidence\" of exactly "
        "\"high\", \"medium\", or \"low\". Fields with value null must have confidence null.\n"
        "8. Respond with ONLY a single valid JSON object — no markdown fences, no commentary, "
        "no explanations before or after. The JSON object's keys must be exactly the field ids "
        "provided, each mapping to an object of shape: "
        '{"value": <string|number|boolean|null>, "confidence": "high"|"medium"|"low"|null, '
        '"found": true|false}.'
    )


@app.post("/api/extract")
async def extract(schema_json: str = Form(..., alias="schema"), file: UploadFile = File(...)):
    if client is None:
        raise HTTPException(status_code=500, detail="Server misconfigured: GEMINI_API_KEY not set.")

    # ---- Parse & validate schema ----
    try:
        fields = json.loads(schema_json)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Form schema was not valid JSON.")

    if not isinstance(fields, list) or len(fields) == 0:
        raise HTTPException(
            status_code=400,
            detail="Please build the form (add at least one field) before uploading a document.",
        )

    # ---- Validate file type ----
    ext = _extension_of(file.filename or "")
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Please upload a PDF, PNG, JPG, or JPEG file.",
        )

    file_bytes = await file.read()
    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File is too large. Maximum size is 12 MB.")

    # ---- Convert document to Gemini content parts ----
    try:
        if ext == ".pdf":
            doc_parts = _pdf_to_content_parts(file_bytes)
        else:
            doc_parts = _image_to_content_parts(file_bytes, ext)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # ---- Build the schema description for the model ----
    schema_for_prompt = [
        {
            "id": f.get("id"),
            "label": f.get("label"),
            "type": f.get("type"),
            "required": f.get("required", False),
            "options": f.get("options", []),
        }
        for f in fields
    ]

    instruction_text = (
        "FORM FIELDS (schema-driven, do not assume any other fields exist):\n"
        f"{json.dumps(schema_for_prompt, indent=2)}\n\n"
        "Extract a value for each field above from the document content that follows. "
        "Respond with ONLY the JSON object described in the system instructions."
    )

    contents = [instruction_text] + doc_parts

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_build_system_prompt(),
                response_mime_type="application/json",
                max_output_tokens=2000,
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Extraction model call failed: {exc}")

    raw_text = (response.text or "").strip()

    parsed = _safe_parse_json(raw_text)
    if parsed is None:
        raise HTTPException(
            status_code=502,
            detail="The extraction model did not return valid JSON. Please try again.",
        )

    # ---- Guarantee every schema field is present in the result ----
    result = {}
    for f in fields:
        fid = f.get("id")
        entry = parsed.get(fid) if isinstance(parsed, dict) else None
        if not isinstance(entry, dict):
            entry = {"value": None, "confidence": None, "found": False}
        entry.setdefault("value", None)
        entry.setdefault("confidence", None)
        entry.setdefault("found", entry.get("value") is not None)
        result[fid] = entry

    return {"filename": file.filename, "extracted": result}


def _safe_parse_json(text: str):
    """Tries straight json.loads, then falls back to slicing between the first { and last }."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None
