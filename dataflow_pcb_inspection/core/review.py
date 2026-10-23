"""Human decisions are immutable and bound to a specific prediction and assets."""
from __future__ import annotations

import copy
from pathlib import Path

from .annotation import file_sha256, fingerprint, prediction_binding, resource, validate_prediction


def _verify_binding(annotation):
    if annotation.get("sample_id") != annotation["sample"].get("id"):
        raise ValueError("sample_id does not match the bound sample id")
    config = {"synthesis": annotation["synthesis"]} if "synthesis" in annotation else annotation.get("binding_config")
    expected = prediction_binding(annotation["sample"], annotation["prediction"], config)
    if expected != annotation.get("binding"):
        raise ValueError("prediction content or configuration changed since version binding")


def review_packet(annotation):
    if annotation.get("status") != "needs_review" or not annotation.get("binding"):
        raise ValueError("only a successful prediction may be exported for review")
    _verify_binding(annotation)
    return {"sample_id": annotation["sample_id"], "binding": annotation["binding"],
            "sample": copy.deepcopy(annotation["sample"]),
            "original_annotations": copy.deepcopy(annotation["sample"].get("original_annotations")),
            "prediction": copy.deepcopy(annotation["prediction"])}


def reconcile(annotation):
    result = copy.deepcopy(annotation)
    if result.get("status") != "needs_review":
        return result
    _verify_binding(result)
    sample = result["sample"]
    original = sample.get("original_annotations")
    if original is None:
        relation = "unlabeled"
    else:
        validate_prediction(original, sample)

        def semantics(prediction):
            return {"verdict": prediction["verdict"], "findings": sorted(
                [(x["image_id"], x["category"], tuple(x["bbox_xyxy"])) for x in prediction["findings"]])}

        relation = "keep" if semantics(original) == semantics(result["prediction"]) else "change"
    result["label_review"] = {"relation": relation, "requires_human": True}
    return result


def merge_decision(annotation, decision, *, image_root):
    result = copy.deepcopy(annotation)
    if result.get("status") not in {"needs_review", "reviewed", "rejected"}:
        raise ValueError("a failed prediction cannot receive a human acceptance decision")
    _verify_binding(result)
    if decision.get("sample_id") != result.get("sample_id") or decision.get("binding") != result.get("binding"):
        raise ValueError("review decision does not match sample and prediction version")
    for key in ("decision_id", "reviewer", "reason"):
        if not isinstance(decision.get(key), str) or not decision[key].strip():
            raise ValueError(f"decision.{key} must be a non-empty string")
    # Re-check bytes rather than trusting stale paths or the review export alone.
    for image in result["sample"]["images"]:
        path = resource(Path(image_root), image["path"])
        if file_sha256(path) != image.get("sha256"):
            raise ValueError("image content changed since prediction; regenerate the review packet")
    for value, expected in result.get("synthesis", {}).get("artifact_sha256", {}).items():
        if file_sha256(resource(Path(image_root), value)) != expected:
            raise ValueError("synthesis artifact changed since candidate generation")
    previous = result.get("human_decision")
    if previous is not None:
        if fingerprint(previous) != fingerprint(decision):
            raise ValueError("an existing human decision cannot be silently overwritten")
        expected_status = "rejected" if decision["decision"] == "reject" else "reviewed"
        expected_final = (None if decision["decision"] == "reject" else
                          result["prediction"] if decision["decision"] == "accept" else decision.get("annotation"))
        if result["status"] != expected_status or result.get("final_annotation") != expected_final:
            raise ValueError("stored review result does not match its human decision")
        return result
    review_packet(result)
    action = decision.get("decision")
    if action not in {"accept", "correct", "reject"}:
        raise ValueError("decision must be accept, correct or reject")
    final = None
    if action != "reject":
        if action == "accept" and "annotation" in decision and decision["annotation"] != result["prediction"]:
            raise ValueError("accept cannot replace the model prediction; use correct")
        final = result["prediction"] if action == "accept" else decision.get("annotation")
        final = validate_prediction(final, result["sample"])
    result.update(status="rejected" if action == "reject" else "reviewed",
                  final_annotation=final, human_decision=copy.deepcopy(decision))
    return result
