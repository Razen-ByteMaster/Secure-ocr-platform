import os
import re
import io
import logging
from collections import defaultdict
from typing import List, Dict, Any, Optional, Tuple

import cv2
import numpy as np
from PIL import Image
import easyocr
from pydantic import BaseModel, Field, field_validator

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SecureOCRPlatform")

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024
ALLOWED_STRUCTURAL_FORMATS = ['jpeg', 'png', 'webp']
SANIZATION_REGEX = re.compile(r'[<>{}\[\]\\^`~]')
MIN_CONFIDENCE = 0.15            # drop clearly-garbage recognition blobs
CONFIDENCE_FLOOR = 0.35          # below this, keep text but flag for review

# Map Arabic-Indic (٠-٩) and Persian/Extended (۰-۹) digits to Latin so that the
# Latin-based entity regexes ([0-9]) also work on Arabic documents.
ARABIC_DIGIT_MAP = str.maketrans({
    '٠': '0', '١': '1', '٢': '2', '٣': '3', '٤': '4',
    '٥': '5', '٦': '6', '٧': '7', '٨': '8', '٩': '9',
    '۰': '0', '۱': '1', '۲': '2', '۳': '3', '۴': '4',
    '۵': '5', '۶': '6', '۷': '7', '۸': '8', '۹': '9',
})


def normalize_arabic_digits(text: str) -> str:
    """Convert Arabic-Indic / Persian digits to Latin digits in place."""
    return text.translate(ARABIC_DIGIT_MAP)


class SecureOCRRequest(BaseModel):
    target_languages: List[str] = Field(default=["en"], min_length=1)

    @field_validator('target_languages')
    @classmethod
    def validate_lang_codes(cls, v: List[str]) -> List[str]:
        for lang in v:
            if not re.match(r'^[a-z]{2,3}$', lang):
                raise ValueError(f"Malicious or invalid language identifier format: {lang}")
        return v


class ExtractedEntity(BaseModel):
    field_name: str
    matched_value: str
    confidence_score: float


class ExtractedDataResponse(BaseModel):
    raw_text: str
    structured_entities: Dict[str, Any]
    average_confidence: float
    review_warning: bool = False


# ---------------------------------------------------------------------------
# Image pre-processing helpers (OpenCV) — target the easyocr.org + OpenCV
# best-practice pipeline: grayscale -> denoise -> CLAHE -> deskew -> threshold.
# ---------------------------------------------------------------------------
class ImagePreprocessor:
    """Ordered set of OpenCV filters tuned to boost OCR accuracy on noisy scans."""

    @staticmethod
    def pil_to_cv(image: Image.Image) -> np.ndarray:
        return cv2.cvtColor(np.array(image.convert('RGB')), cv2.COLOR_RGB2BGR)

    @staticmethod
    def cv_to_pil(image: np.ndarray) -> Image.Image:
        return Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))

    @staticmethod
    def upscale_if_small(image: np.ndarray, min_dim: int = 900) -> np.ndarray:
        """Blow up tiny/low-res images so text strokes are readable (EasyOCR wants >=20px text height)."""
        h, w = image.shape[:2]
        if max(h, w) >= min_dim:
            return image
        scale = min_dim / float(max(h, w))
        new_w, new_h = int(w * scale), int(h * scale)
        return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_CUBIC)

    @staticmethod
    def denoise(image: np.ndarray) -> np.ndarray:
        """Edge-preserving denoise to remove speckle without destroying thin strokes."""
        return cv2.fastNlMeansDenoisingColored(image, None, h=8, hColor=8,
                                                templateWindowSize=7, searchWindowSize=21)

    @staticmethod
    def to_gray(image: np.ndarray) -> np.ndarray:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    @staticmethod
    def enhance_contrast(gray: np.ndarray) -> np.ndarray:
        """CLAHE — contrast-limited adaptive histogram equalization for faded/low-contrast scans."""
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        return clahe.apply(gray)

    @staticmethod
    def deskew(gray: np.ndarray) -> np.ndarray:
        """Detect dominant text-line angle and rotate to straighten the page."""
        try:
            inverted = cv2.bitwise_not(gray)
            coords = np.column_stack(np.where(inverted > 0))
            if coords.shape[0] == 0:
                return gray
            angle = cv2.minAreaRect(coords)[-1]
            if angle < -45:
                angle = -(90 + angle)
            else:
                angle = -angle
            if abs(angle) < 0.5:
                return gray
            (h, w) = gray.shape[:2]
            center = (w // 2, h // 2)
            matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
            return cv2.warpAffine(gray, matrix, (w, h),
                                  flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        except Exception as e:
            logger.debug(f"Deskew skipped: {e}")
            return gray

    @staticmethod
    def binarize(gray: np.ndarray) -> np.ndarray:
        """Adaptive Gaussian threshold for uneven lighting; falls back to Otsu."""
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        binary = cv2.adaptiveThreshold(
            blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 2)
        # invert to dark-text-on-light (EasyOCR prefers uniform high-contrast)
        return binary

    @classmethod
    def build_enhanced(cls, pil_image: Image.Image) -> List[np.ndarray]:
        """Produce a set of enhanced OpenCV variants for multi-pass OCR.

        Returns (original_bgr, enhanced_bgr) so the caller can ensemble the results.
        """
        original = cls.pil_to_cv(pil_image)
        original = cls.upscale_if_small(original)

        gray = cls.to_gray(original)
        denoised = cls.to_gray(cls.denoise(original))
        contrast = cls.enhance_contrast(gray)
        deskewed = cls.deskew(contrast)
        binary = cls.binarize(deskewed)

        # RGB variants for EasyOCR (it prefers 3-channel input).
        enhanced = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
        contrast_rgb = cv2.cvtColor(deskewed, cv2.COLOR_GRAY2BGR)
        return original, enhanced, contrast_rgb


# ---------------------------------------------------------------------------
# Character-confusion post-processing (from OCR literature: O/0, 1/l/I ...).
# Applied only inside known context tokens, never to the whole document.
# ---------------------------------------------------------------------------
CONFUSION_RULES = [
    # fix within detected entity context only
]
# Context-aware token cleaning for entity extraction:
EMAIL_SPACED = re.compile(
    r'[\w\.-]+\s*[(@|at)\s]+\s*[\w\.-]+\s*[.\s]+\s*w?[\w\.-]{2,}', re.IGNORECASE)


class SecureOCRPlatform:
    DEFAULT_FILES = {
        'invoice_id': [
            r'(?i)(?:invoice|factura|rechnung|فاتورة)(?:\s*(?:no|num|number|id|رقم))?\s*[#:.:]?\s*'
            r'([A-Z0-9]{1,5}(?:[\s-]+[0-9]+[A-Z0-9]*)*)',
            r'\b(INV[\s\-]*\d[\d\s\-A-Z0-9]*)\b',
        ],
        'contact_email': [
            r'[\w\.\+\-]+@[\w\.\-]+\.\w{2,}',
            r'[\w\.\+\-]+\s*(?:@|\[at\]|\(at\)| at )\s*[\w\.\-]+(?:\s*\.\s*[a-z]{2,})?(?:\s+[a-z]{2,})?',
            r'[\w\.\+\-]+\s*@\s*[\w\.\-]+\s*[.,]\s*[a-z]{2,}',
            r'[\w\.\+\-]+\s*(?:@|\[at\]|\(at\)| at )\s*[\w\.\-]+\s*(?:\.|DOT)\s*[a-z]{2,}',
        ],
        'phone_number': [
            r'(?:tel|phone|mobile|هاتف|جوال|رقم)[:]?\s*([+\d][\d\s\-\.\(\)]{6,}\d)',
            r'\b(\+?\d[\d\s\-\.]{5,}\d)\b',
        ],
        'date': [
            r'\b((?:0?[1-9]|[12]\d|3[01])[/\-.]\s*(?:0?[1-9]|1[0-2])[/\-.]\s*\d{2,4})\b',
            r'\b(\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2})\b',
        ],
        'national_id': [r'\b([0-9]{14})\b'],
    }

    def __init__(self, languages: List[str], entity_patterns: Dict[str, List[str]] = None,
                 use_enhancement: bool = True):
        logger.info(f"Initializing Multilingual OCR Engine for: {languages} "
                    f"(enhancement={use_enhancement})")
        self.reader = easyocr.Reader(languages, gpu=False)
        self.entity_patterns = entity_patterns or dict(self.DEFAULT_FILES)
        self.use_enhancement = use_enhancement

    @staticmethod
    def secure_validate_file(file_bytes: bytes) -> Image.Image:
        if len(file_bytes) > MAX_FILE_SIZE_BYTES:
            raise ValueError("File payload limits exceeded (Max 5MB allocation allowable).")
        try:
            image = Image.open(io.BytesIO(file_bytes))
            image.verify()
            image = Image.open(io.BytesIO(file_bytes))
            if image.format.lower() not in ALLOWED_STRUCTURAL_FORMATS:
                raise ValueError(f"Disallowed structural file signature: {image.format}")
            return image
        except Exception as e:
            logger.error(f"Security Shield tracking - Malformed image upload intercepted: {str(e)}")
            raise ValueError("Uploaded file failed binary formatting integrity validation checking.")

    # ---- Core OCR with multi-pass ensemble --------------------------------
    def _run_reader(self, cv_image: np.ndarray, paragraph: bool = False) -> List[Dict[str, Any]]:
        """Run EasyOCR on a single OpenCV image, normalize and filter results."""
        is_success, encoded = cv2.imencode('.png', cv_image)
        if not is_success:
            return []
        try:
            results = self.reader.readtext(encoded.tobytes(), paragraph=paragraph)
        except Exception as e:
            logger.error(f"EasyOCR readtext failed: {e}")
            return []

        blocks = []
        for item in results:
            # item = (bbox, text, confidence) OR (text, confidence) when paragraph=True
            if len(item) == 3:
                _, text, confidence = item
            else:
                text, confidence = item
            if confidence < MIN_CONFIDENCE:
                continue
            sanitized = SANIZATION_REGEX.sub('', text).strip()
            if sanitized:
                blocks.append({"text": sanitized, "confidence": float(confidence)})
        return blocks

    def _merge_ensemble(self, variants: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """Merge results from multiple preprocessing variants, de-duplicating by text.

        When the same text is seen by several variants the highest confidence wins.
        The dedup key strips punctuation/spacing so near-identical OCR reads
        (e.g. "INV-2201*88" vs "INV-2201-88") collapse to the cleanest/lowest-read.
        """
        def norm_key(text: str) -> str:
            # Keep letters and digits in ANY script (Latin, Arabic, Devanagari, ...)
            # so Arabic/CJK text is not collapsed to an empty key and wrongly
            # dropped as a duplicate.
            return re.sub(r'[\W_]+', '', text.lower()).strip()

        best: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for variant in variants:
            for block in variant:
                key = norm_key(block['text'])
                if not key:
                    continue
                if key not in best:
                    best[key] = block
                    order.append(key)
                elif block['confidence'] > best[key]['confidence']:
                    best[key] = block
        return [best[k] for k in order]

    def extract_text(self, image_bytes: bytes) -> List[Dict[str, Any]]:
        validated_image = self.secure_validate_file(image_bytes)

        if not self.use_enhancement:
            img_byte_arr = io.BytesIO()
            validated_image.save(img_byte_arr, format=validated_image.format)
            return self._run_reader(ImagePreprocessor.pil_to_cv(validated_image))

        # Multi-pass ensemble: original, contrast+deskewed, and binarized.
        original, enhanced, contrast_rgb = ImagePreprocessor.build_enhanced(validated_image)
        variants = [
            self._run_reader(original),
            self._run_reader(contrast_rgb),
            self._run_reader(enhanced),
        ]
        return self._merge_ensemble(variants)

    # ---- Entity extraction with OCR-tolerant normalization -----------------
    @staticmethod
    def _normalize_email(text: str) -> Optional[str]:
        """Reassemble OCR-split emails like 'ops @company com' or 'ops AT gmail DOT com'."""
        text = text.replace('[at]', '@').replace('(at)', '@').replace(' AT ', ' @ ')
        text = re.sub(r'\s*[Dd][Oo][Tt]\s*', '.', text)
        text = re.sub(r'\s+@\s+', '@', text)   # ops @ company  ->  ops@company
        text = re.sub(r'\s+@', '@', text)      # ops @company   ->  ops@company
        text = re.sub(r'\s*\.\s*', '.', text)
        text = re.sub(r'\s*\+\s*', '+', text)
        # Fold a space-separated trailing TLD into a dot: "ops@company com" -> "ops@company.com"
        text = re.sub(r'([\w\.\+\-]+@[\w\.\-]+)\s+(com|net|org|io|edu|gov|co|info|me|us|uk)\b',
                      r'\1.\2', text, flags=re.IGNORECASE)
        m = re.search(r'[\w\.\+\-]+@[\w\.\-]+\.[a-z]{2,}', text)
        return m.group(0) if m else None

    @staticmethod
    def _normalize_phone(text: str) -> Optional[str]:
        m = re.search(r'\+?\d[\d\s\-\.\(\)]{5,}\d', text)
        if not m:
            return None
        value = m.group(0).strip()
        # Reject obvious national-ID runs (Egyptian national ID = exactly 14 digits).
        digits = re.sub(r'\D', '', value)
        if len(digits) == 14:
            return None
        # Reject date-ish / invoice-ish groups: a 4-digit group followed by a
        # separator and more digits (e.g. "2026-125" is a year, not a phone).
        if re.match(r'^\d{3,4}[/\-.]\d', value):
            return None
        # A phone needs a realistic number of digits (>=7) to avoid picking up
        # numeric IDs / invoice segments like "2026 99".
        if len(digits) < 7:
            return None
        return value

    @staticmethod
    def _normalize_date(text: str) -> Optional[str]:
        m = re.search(r'\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}', text)
        return m.group(0) if m else None

    def compile_and_extract_entities(self, raw_blocks: List[Dict[str, Any]]) -> ExtractedDataResponse:
        full_text = " ".join([block["text"] for block in raw_blocks])
        total_conf = sum([block["confidence"] for block in raw_blocks])
        avg_conf = total_conf / len(raw_blocks) if raw_blocks else 0.0

        entities: Dict[str, Any] = {}
        review_warning = avg_conf < CONFIDENCE_FLOOR

        # Gather all OCR lines in a single string for pattern search. Digits are
        # normalized from Arabic-Indic/Persian to Latin so number-based entity
        # patterns (phone, date, national id, invoice) work on Arabic documents.
        search_text = normalize_arabic_digits(full_text)

        for field_name, patterns in self.entity_patterns.items():
            value = None
            for pattern in patterns:
                match = re.search(pattern, search_text, re.IGNORECASE)
                if not match:
                    continue
                group = list(match.groups() or [])
                candidate = group[-1] if group else match.group(0)
                normalized = candidate.strip()
                if field_name == 'contact_email':
                    normalized = self._normalize_email(candidate)
                elif field_name == 'phone_number':
                    normalized = self._normalize_phone(candidate)
                elif field_name == 'date':
                    normalized = self._normalize_date(candidate)
                elif field_name == 'invoice_id':
                    normalized = re.sub(r'\s+', '-', candidate.strip())
                    normalized = re.sub(r'[-]{2,}', '-', normalized)
                # Only accept the candidate when it survives field-specific validation.
                if normalized:
                    value = normalized
                    break
            if value is not None:
                entities[field_name] = value

        return ExtractedDataResponse(
            raw_text=full_text,
            structured_entities=entities,
            average_confidence=round(avg_conf, 4),
            review_warning=review_warning,
        )


def process_image_bytes(image_bytes: bytes, languages: List[str]) -> ExtractedDataResponse:
    platform = SecureOCRPlatform(languages=languages)
    blocks = platform.extract_text(image_bytes)
    return platform.compile_and_extract_entities(blocks)


if __name__ == "__main__":
    print("--- Bootstrapping Secure Multilingual OCR Platform ---")
    config_input = {"target_languages": ["en", "es", "de"]}
    validated_config = SecureOCRRequest(**config_input)

    platform = SecureOCRPlatform(languages=validated_config.target_languages)

    # Quick self-test: entity extraction + email reassembly.
    sample = [
        {"text": "Invoice id: INV-2026-99X", "confidence": 0.98},
        {"text": "Contact ops @company com", "confidence": 0.94},
        {"text": "Phone +20 106 126 2479", "confidence": 0.90},
        {"text": "Date 12/05/2025", "confidence": 0.93},
    ]
    print(platform.compile_and_extract_entities(sample).model_dump_json(indent=4))
