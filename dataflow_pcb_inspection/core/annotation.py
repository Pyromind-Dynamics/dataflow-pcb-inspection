"""Platform-independent PCB v1 record validation and image binding."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator
from PIL import Image


PREDICTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "findings", "note"],
    "properties": {
        "verdict": {"enum": ["defect", "normal", "uncertain"]},
        "note": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["image_id", "category", "bbox_xyxy", "note"],
                "properties": {
                    "image_id": {"type": "string", "minLength": 1},
                    "category": {"type": "string", "minLength": 1},
                    "bbox_xyxy": {"type": "array", "minItems": 4, "maxItems": 4,
                                  "items": {"type": "number"}},
                    "note": {"type": "string"},
                },
            },
        },
    },
}
_PREDICTION_VALIDATOR = Draft202012Validator(PREDICTION_SCHEMA)


def file_signature(path: Path) -> tuple:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_evidence(path: Path, cache: dict) -> dict:
    """Per-process-call cache; stat changes invalidate both dimensions and content hash."""
    signature = file_signature(path)
    cached = cache.get(str(path))
    if cached is not None and cached["signature"] == signature:
        return cached
    with Image.open(path) as opened:
        opened.verify()
    with Image.open(path) as opened:
        expected = {"PNG": {".png"}, "JPEG": {".jpg", ".jpeg"}}
        if path.suffix.lower() not in expected.get(opened.format, set()):
            raise ValueError("v1 model input requires matching JPEG/PNG content and filename extension")
        width, height = opened.size
    digest = file_sha256(path)
    if file_signature(path) != signature:
        raise ValueError("image changed while validating input")
    evidence = {"signature": signature, "width": width, "height": height, "sha256": digest}
    cache[str(path)] = evidence
    return evidence


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def resource(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("resource path must be a non-empty relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value != path.as_posix():
        raise ValueError("resource path must be relative, normalized and inside image_root")
    resolved = (root / value).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("resource escapes image_root")
    if not resolved.is_file():
        raise ValueError(f"missing resource: {value}")
    return resolved


def normalize_sample(value: dict, image_root: Path, *, image_cache=None) -> tuple[dict, list[str]]:
    if not isinstance(value, dict):
        raise ValueError("sample must be an object")
    sample = copy.deepcopy(value)
    if not isinstance(sample.get("id"), str) or not sample["id"].strip():
        raise ValueError("sample.id must be a non-empty string")
    images = sample.get("images")
    if not isinstance(images, list) or not images:
        raise ValueError("sample.images must be a non-empty list")
    ids, paths = set(), []
    image_cache = {} if image_cache is None else image_cache
    for image in images:
        if not isinstance(image, dict) or not isinstance(image.get("id"), str) or not image["id"].strip():
            raise ValueError("every image must have a non-empty string id")
        if image["id"] in ids:
            raise ValueError("image ids must be unique within a sample")
        ids.add(image["id"])
        if image.get("role") not in {"inspection", "reference", "auxiliary"}:
            raise ValueError("image.role must be inspection, reference or auxiliary")
        path = resource(image_root, image.get("path"))
        evidence = image_evidence(path, image_cache)
        width, height = evidence["width"], evidence["height"]
        if image.get("width", width) != width or image.get("height", height) != height:
            raise ValueError("declared image dimensions do not match file")
        image.update(width=width, height=height, sha256=evidence["sha256"])
        paths.append(str(path))
    if not any(x["role"] == "inspection" for x in images):
        raise ValueError("at least one inspection image is required")
    if not isinstance(sample.get("inspection_rules", ""), str):
        raise ValueError("inspection_rules must be a string")
    return sample, paths


def validate_prediction(value: dict, sample: dict, categories=()) -> dict:
    _PREDICTION_VALIDATOR.validate(value)
    images = {x["id"]: x for x in sample["images"]}
    for finding in value["findings"]:
        image = images.get(finding["image_id"])
        if image is None or image["role"] != "inspection":
            raise ValueError("finding must refer to an inspection image id")
        if categories and finding["category"] not in categories:
            raise ValueError("finding category is not in the configured vocabulary")
        x1, y1, x2, y2 = finding["bbox_xyxy"]
        if any(isinstance(v, bool) or not math.isfinite(v) for v in (x1, y1, x2, y2)):
            raise ValueError("bbox coordinates must be finite numbers")
        if not (0 <= x1 < x2 <= image["width"] and 0 <= y1 < y2 <= image["height"]):
            raise ValueError("bbox must be inside the original inspection image in pixel xyxy units")
    if value["verdict"] == "normal" and value["findings"]:
        raise ValueError("normal verdict must have no findings")
    if value["verdict"] == "defect" and not value["findings"]:
        raise ValueError("defect verdict requires findings")
    if value["verdict"] == "uncertain" and not value["note"].strip():
        raise ValueError("uncertain verdict requires an explanation")
    return copy.deepcopy(value)


def parse_prediction(raw: str, sample: dict, categories=()) -> dict:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("model response must be non-empty JSON text")
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0] not in {"```", "```json"} or lines[-1] != "```":
            raise ValueError("invalid JSON code fence")
        text = "\n".join(lines[1:-1])
    return validate_prediction(json.loads(text), sample, categories)


def prediction_binding(sample: dict, prediction: dict, model_config: dict) -> str:
    """Bind human decisions to content, image bytes, and model configuration."""
    return fingerprint({"sample": sample, "prediction": prediction, "model_config": model_config})
