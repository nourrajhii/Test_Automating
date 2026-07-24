"""
vision_parser_service.py
-------------------------
[... docstring inchangée, je la garde telle quelle dans ton fichier ...]
"""
import base64
import difflib
import io
import json
import logging
import re

import httpx
import pytesseract
from PIL import Image

from app.config import (
    OLLAMA_BASE_URL,
    VISION_MODEL,
    VISION_TIMEOUT,
    MAX_IMAGE_SIZE,
    TESSERACT_CMD,
    OCR_MIN_CONFIDENCE,
    VISION_CLASSIFY_BATCH_SIZE,
    OCR_UPSCALE_MIN_WIDTH,
    OCR_PSM_MODES,
)
from app.models.schemas import UIAnalysisResult, UIElement

logger = logging.getLogger("vision_parser")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[vision_parser] %(message)s"))
    logger.addHandler(_h)

if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


class ImageTooLargeError(Exception):
    pass


def _zone(x: int, y: int, w: int, h: int, width: int, height: int) -> str:
    cx, cy = x + w / 2, y + h / 2
    horiz = "left" if cx < width / 3 else ("right" if cx > 2 * width / 3 else "center")
    vert = "top" if cy < height / 3 else ("bottom" if cy > 2 * height / 3 else "middle")
    return f"{vert}-{horiz}"


def _upscale_if_needed(image: Image.Image) -> Image.Image:
    if image.width >= OCR_UPSCALE_MIN_WIDTH:
        return image
    scale = OCR_UPSCALE_MIN_WIDTH / image.width
    new_size = (int(image.width * scale), int(image.height * scale))
    return image.resize(new_size, Image.LANCZOS)


def _split_line_into_segments(words: list[dict]) -> list[list[dict]]:
    words_sorted = sorted(words, key=lambda w: w["x"])
    segments: list[list[dict]] = [[words_sorted[0]]]
    for prev, curr in zip(words_sorted, words_sorted[1:]):
        gap = curr["x"] - (prev["x"] + prev["w"])
        threshold = max(24, 2.2 * prev["h"])
        if gap > threshold:
            segments.append([curr])
        else:
            segments[-1].append(curr)
    return segments


def _tesseract_pass(image: Image.Image, psm: str) -> list[dict]:
    width, height = image.size
    config = f"--psm {psm}"
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT, config=config)

    lines: dict[tuple, list[dict]] = {}
    n = len(data["text"])
    for i in range(n):
        raw = (data["text"][i] or "").strip()
        try:
            conf = int(float(data["conf"][i]))
        except (ValueError, TypeError):
            conf = -1
        if not raw or conf < OCR_MIN_CONFIDENCE:
            continue

        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append({
            "word": raw,
            "x": data["left"][i],
            "y": data["top"][i],
            "w": data["width"][i],
            "h": data["height"][i],
        })

    segments_out: list[dict] = []
    for words in lines.values():
        for segment in _split_line_into_segments(words):
            label = " ".join(w["word"] for w in segment).strip()
            if not label or len(label) > 120:
                continue
            x0 = min(w["x"] for w in segment)
            y0 = min(w["y"] for w in segment)
            x1 = max(w["x"] + w["w"] for w in segment)
            y1 = max(w["y"] + w["h"] for w in segment)
            loc = _zone(x0, y0, x1 - x0, y1 - y0, width, height)
            segments_out.append({"text": label, "location": loc, "bbox": (x0, y0, x1, y1)})

    return segments_out


def _dedup_segments(all_segments: list[dict]) -> list[dict]:
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.lower()).strip()

    def _overlap(a, b) -> bool:
        ax0, ay0, ax1, ay1 = a
        bx0, by0, bx1, by1 = b
        return not (ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0)

    merged: list[dict] = []
    for seg in all_segments:
        norm = _norm(seg["text"])
        match_idx = None
        for i, existing in enumerate(merged):
            if _norm(existing["text"]) == norm and _overlap(existing["bbox"], seg["bbox"]):
                match_idx = i
                break
        if match_idx is None:
            merged.append(seg)
        elif len(seg["text"]) > len(merged[match_idx]["text"]):
            merged[match_idx] = seg

    return merged


def _ocr_extract_texts(image_bytes: bytes) -> list[dict]:
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    image = _upscale_if_needed(image)

    all_segments: list[dict] = []
    for psm in OCR_PSM_MODES:
        try:
            all_segments.extend(_tesseract_pass(image, psm))
        except Exception as e:
            logger.warning("Passage PSM %s a échoué : %s", psm, e)

    deduped = _dedup_segments(all_segments)

    texts: list[dict] = []
    seen: set[str] = set()
    for seg in sorted(deduped, key=lambda s: (s["bbox"][1], s["bbox"][0])):
        key_l = seg["text"].lower()
        if key_l in seen:
            continue
        seen.add(key_l)
        texts.append({"text": seg["text"], "location": seg["location"]})

    logger.info(
        "OCR (%s passages PSM %s) a extrait %d ligne(s) de texte (seuil confiance=%d) : %s",
        len(OCR_PSM_MODES), OCR_PSM_MODES, len(texts), OCR_MIN_CONFIDENCE,
        [t["text"] for t in texts],
    )
    return texts[:80]


CLASSIFICATION_PROMPT = """You are a UI analyst. Below is a screenshot and a
list of text pieces that were extracted from that exact image by an OCR
engine (ground truth — this text is 100% guaranteed to be visibly printed
in the image, no exceptions).

=== TEXT EXTRACTED FROM THE IMAGE BY OCR (ground truth — do not add to this list) ===
{texts_list}

YOUR TASK:
For each OCR text above, decide if it is part of an INTERACTIVE element
(something a user can click, type into, select, or toggle). Skip plain text
that is not interactive (paragraphs, titles, static labels with no
associated control).

CRITICAL RULES:
- Only use "label" values that appear, verbatim, in the OCR list above.
- NEVER add an element whose text is not in that list — even if it would be
  a "typical" or "expected" element for this kind of page (e.g. do NOT add
  email/password/login fields unless their exact text is in the list above).
- If nothing in the list looks interactive, return an empty "elements" array.
- If the OCR list does not contain the word "email", "password", "login",
  "sign in" etc., such fields DO NOT EXIST on this page — do not report them.

For each interactive element found:
- "type": one of button, input_text, input_email, input_password, input_search,
  link, select, checkbox, radio, textarea
- "label": must be copied verbatim from the OCR list
- "context": short location description (reuse the "location" info given above)
- "is_link": true only if it clearly navigates somewhere
- "possible_destination": best guess, or null

Also determine, based only on the OCR text and the image:
- "page_type": one of login, registration, form, dashboard, profile, settings,
  search, product, checkout, homepage, other
- "page_purpose": one sentence describing what this screen/app is for

Respond ONLY with valid JSON, no markdown, no explanation:
{{
  "page_type": "homepage",
  "page_purpose": "<one sentence>",
  "elements": [
    {{
      "type": "button",
      "label": "<verbatim text from the OCR list above>",
      "context": "<location>",
      "is_link": false,
      "possible_destination": null
    }}
  ]
}}
"""


async def _classify_elements(image_b64: str, texts: list[dict]) -> dict:
    all_elements: list[dict] = []
    page_type = "general"
    page_purpose = ""

    for i in range(0, len(texts), VISION_CLASSIFY_BATCH_SIZE):
        batch = texts[i:i + VISION_CLASSIFY_BATCH_SIZE]
        texts_list = "\n".join(f'  - "{t["text"]}"  ({t["location"]})' for t in batch)
        prompt = CLASSIFICATION_PROMPT.format(texts_list=texts_list)
        num_predict = min(2000, 500 + len(batch) * 90)

        data = await _call_vision_model(prompt, image_b64, num_predict=num_predict)

        batch_elements = data.get("elements", []) or []
        logger.info(
            "Lot OCR %d-%d (%d textes) -> LLM a classé %d élément(s) interactif(s) : %s",
            i, i + len(batch), len(batch), len(batch_elements),
            [e.get("label") for e in batch_elements],
        )
        if not data:
            logger.warning(
                "Lot OCR %d-%d : le LLM n'a renvoyé AUCUN JSON exploitable "
                "(réponse vide/mal formée). Textes du lot : %s",
                i, i + len(batch), [t["text"] for t in batch],
            )

        all_elements.extend(batch_elements)
        if not page_purpose and data.get("page_purpose"):
            page_purpose = data["page_purpose"]
        if data.get("page_type") and page_type == "general":
            page_type = data["page_type"]

    return {"elements": all_elements, "page_type": page_type, "page_purpose": page_purpose}


def _encode_image(image_bytes: bytes) -> str:
    if len(image_bytes) > MAX_IMAGE_SIZE:
        raise ImageTooLargeError(
            f"Image trop lourde ({len(image_bytes)} octets, max {MAX_IMAGE_SIZE})."
        )
    return base64.b64encode(image_bytes).decode("utf-8")


async def _call_vision_model(prompt: str, image_b64: str, num_predict: int = 1500) -> dict:
    payload = {
        "model": VISION_MODEL,
        "prompt": prompt,
        "images": [image_b64],
        "stream": True,
        # FIX VITESSE : keep_alive=0 forçait Ollama à DÉCHARGER le modèle
        # de la mémoire après CHAQUE appel -> rechargement complet depuis
        # le disque à chaque lot suivant (souvent 30-90s pour un modèle
        # multimodal) -> c'était la cause principale des ReadTimeout et
        # d'une bonne partie du temps total. "5m" garde le modèle chargé
        # entre les lots (et entre les requêtes proches dans le temps).
        "keep_alive": "5m",
        "format": "json",
        "options": {"temperature": 0.1, "num_predict": num_predict},
    }

    timeout = httpx.Timeout(timeout=VISION_TIMEOUT)
    full = ""
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", f"{OLLAMA_BASE_URL}/api/generate", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                    full += chunk.get("response", "")
                    if chunk.get("done"):
                        break
                except Exception:
                    continue

    try:
        return json.loads(full.strip())
    except Exception:
        match = re.search(r"\{[\s\S]*\}", full)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
    return {}


def _normalize_for_match(s: str) -> str:
    return re.sub(r"[^a-z0-9à-ÿ ]+", " ", s.lower()).strip()


def _label_matches_ocr(label: str, allowed_norm: list[str]) -> bool:
    norm_label = _normalize_for_match(label)
    if not norm_label:
        return False
    for norm_allowed in allowed_norm:
        if norm_label == norm_allowed:
            return True
        if len(norm_label) >= 3 and (norm_label in norm_allowed or norm_allowed in norm_label):
            return True
        if len(norm_label) >= 6:
            if difflib.SequenceMatcher(None, norm_label, norm_allowed).ratio() >= 0.72:
                return True
    return False


def _to_ui_elements(raw_elements: list[dict], allowed_texts: list[str]) -> list[UIElement]:
    elements: list[UIElement] = []
    rejected: list[str] = []
    for raw in raw_elements:
        label = (raw.get("label") or "").strip()
        if not label:
            continue
        if not _label_matches_ocr(label, allowed_texts):
            rejected.append(label)
            continue
        context = (raw.get("context") or "page").strip()
        el_type = (raw.get("type") or "button").strip()

        try:
            elements.append(UIElement(
                type=el_type,
                label=f"[{context}] {label}",
                selector_hint="NONE",
                is_link=bool(raw.get("is_link", False)),
                possible_destination=raw.get("possible_destination") or None,
            ))
        except Exception:
            continue

    if rejected:
        logger.warning(
            "%d élément(s) rejeté(s) par le filet anti-hallucination "
            "(label absent de l'OCR — vérifier une éventuelle faute de "
            "recopie du modèle) : %s",
            len(rejected), rejected,
        )

    seen = set()
    deduped = []
    for el in elements:
        key = (el.type, el.label.lower().strip())
        if key not in seen:
            seen.add(key)
            deduped.append(el)
    return deduped[:80]


async def analyze_screenshot(image_bytes: bytes) -> UIAnalysisResult:
    image_b64 = _encode_image(image_bytes)

    texts = _ocr_extract_texts(image_bytes)

    if not texts:
        return UIAnalysisResult(
            elements=[],
            raw_description=(
                "Aucun texte exploitable détecté par OCR dans cette image. "
                "Vérifiez la résolution/qualité de la capture d'écran."
            ),
            page_type="general",
        )

    data = await _classify_elements(image_b64, texts)
    allowed = [_normalize_for_match(t["text"]) for t in texts]
    elements = _to_ui_elements(data.get("elements", []), allowed_texts=allowed)

    page_type = data.get("page_type", "general")
    page_purpose = data.get("page_purpose", "")

    raw_desc = f"[Analyse OCR + vision IA] Page type: {page_type}. {page_purpose}. "
    raw_desc += f"{len(elements)} elements detectes (sur {len(texts)} textes OCR) dans contextes: "
    contexts = sorted(set(
        el.label.split("]")[0].replace("[", "").strip()
        for el in elements if "]" in (el.label or "")
    ))
    raw_desc += ", ".join(contexts)

    return UIAnalysisResult(
        elements=elements,
        raw_description=raw_desc,
        page_type=page_type,
    )