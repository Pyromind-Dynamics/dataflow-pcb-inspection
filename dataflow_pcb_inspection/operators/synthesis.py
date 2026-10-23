"""Real labeled-template synthesis; geometric checks never imply business acceptance."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from dataflow.core import OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from ._contracts import describe, read_new_output

from ..core.annotation import prediction_binding, resource


@OPERATOR_REGISTRY.register()
class PCBTemplateSynthesisGenerator(OperatorABC):
    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        return describe(PCBTemplateSynthesisGenerator, lang)

    def __init__(self, image_root, output_dir, *, categories=("scratch", "dot", "small_object"), on_error="isolate"):
        super().__init__()
        self.image_root = Path(image_root).resolve()
        self.output_dir = Path(output_dir).resolve()
        if self.output_dir.is_relative_to(self.image_root):
            raise ValueError("output_dir must be outside the source image_root")
        if on_error not in {"raise", "isolate"}:
            raise ValueError("on_error must be raise or isolate")
        self.on_error = on_error
        self.categories = tuple(categories)

    def synthesize(self, spec):
        # Geometry dependencies load only when this operator is executed.
        import numpy as np
        from PIL import Image
        from ..core.template_synthesis import Acceptance, Check, SourceEvidence, extract_template, synthesize_template
        from ..core.reference_synthesis import export_sample

        sample_id = spec.get("id")
        if not isinstance(sample_id, str) or not sample_id.strip() or Path(sample_id).name != sample_id or sample_id in {".", ".."}:
            raise ValueError("synthesis id must be a plain non-empty filename")
        if spec.get("source_split") != "train" or spec.get("target_split") != "train":
            raise ValueError("source and target split must explicitly be train")
        if spec.get("category") not in self.categories:
            raise ValueError("unsupported configured synthesis category")
        paths = {key: resource(self.image_root, spec[key]) for key in
                 ("source_image", "source_mask", "source_reference", "target_image", "candidate_mask", "allowed_mask")}

        def rgb(key):
            with Image.open(paths[key]) as image:
                return np.array(image.convert("RGB"))

        def mask(key):
            with Image.open(paths[key]) as image:
                array = np.array(image.convert("L"))
            if not np.isin(array, [0, 255]).all():
                raise ValueError("mask must contain only 0 and 255")
            return array > 0

        donor, normal, target = rgb("source_image"), rgb("source_reference"), rgb("target_image")
        evidence = mask("source_mask")
        source = SourceEvidence(donor, normal, evidence, extract_template(evidence, source_rgb=donor))
        allowed, candidates = mask("allowed_mask"), mask("candidate_mask")
        acceptance = Acceptance(**spec["acceptance"])
        seed = spec.get("seed")
        if type(seed) is not int:
            raise ValueError("an explicit integer seed is required")

        def source_check(source):
            changed = np.any(source.image != source.reference, axis=2)
            return [Check("source_evidence", bool(np.any(changed & source.template.mask)),
                          "labeled donor has visible change inside its mask")]

        generated = synthesize_template(
            source, target, candidate_centers=candidates, allowed_mask=allowed,
            acceptance=acceptance, seed=seed, max_attempts=spec.get("max_attempts", 32),
            scale_xy=tuple(spec.get("scale_xy", (1.0, 1.0))), angle_deg=spec.get("angle_deg", 0),
            pair_transform=spec.get("pair_transform", "identity"),
            validate_source=source_check,
            validate_candidate=lambda c: [Check("allowed_geometry", bool(c.constrained.mask.any()), "non-empty constrained placement")],
            validate_rendered=lambda r: [Check("visible_change", bool(r.changed.any()), "candidate has visible pixels")],
        )
        manifest = export_sample(generated, self.output_dir, sample_id, spec["category"])
        images = []
        for image_id, role, filename in [("inspection", "inspection", "image.png"), ("reference", "reference", "reference.png")]:
            path = self.output_dir / "assets" / sample_id / filename
            with Image.open(path) as opened:
                width, height = opened.size
            images.append({"id": image_id, "role": role, "path": path.relative_to(self.output_dir).as_posix(),
                           "width": width, "height": height, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        sample = {"id": sample_id, "images": images, "split": "train"}
        prediction = {"verdict": "defect", "findings": [{"image_id": "inspection", "category": spec["category"],
                        "bbox_xyxy": list(generated.artifacts.bbox_xyxy), "note": "synthetic labeled-template candidate"}],
                      "note": "Geometric candidate only; requires domain and human review"}
        source_hashes = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
        # Bind every exported artifact, including masks, so later edits invalidate review.
        artifact_hashes = {p.relative_to(self.output_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in sorted((self.output_dir / "assets" / sample_id).iterdir()) if p.is_file()}
        provenance = {"spec": spec, "source_sha256": source_hashes,
                      "artifact_sha256": artifact_hashes, "parameters": generated.parameters,
                      "checks": [asdict(x) for x in generated.checks],
                      "semantic_validation": "not_performed", "manifest": manifest}
        # DataFlow's writer treats nested tuples as strings; emit JSON-native types.
        provenance = json.loads(json.dumps(provenance, ensure_ascii=False, allow_nan=False))
        result = {"schema_version": "pcb.v1", "sample_id": sample_id, "status": "needs_review",
                  "sample": sample, "prediction": prediction, "error": None, "synthesis": provenance}
        result["binding"] = prediction_binding(sample, prediction, {"synthesis": provenance})
        (self.output_dir / "assets" / sample_id / "provenance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result

    def run(self, storage, input_key: str = "synthesis_spec", output_key: str = "pcb_synthesis"):
        frame = read_new_output(storage, input_key, output_key)
        results = []
        for spec in frame[input_key]:
            try:
                result = self.synthesize(spec)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                if self.on_error == "raise":
                    raise
                result = {"schema_version": "pcb.v1", "sample_id": spec.get("id") if isinstance(spec, dict) else None,
                          "status": "failed", "prediction": None,
                          "error": {"stage": "synthesis", "type": type(exc).__name__, "reason": str(exc)}}
            results.append(result)
        frame[output_key] = results
        storage.write(frame)
        return output_key
