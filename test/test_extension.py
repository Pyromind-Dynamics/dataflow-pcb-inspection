from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from PIL import Image

from dataflow.utils.storage import FileStorage
from dataflow.utils.registry import OPERATOR_REGISTRY, PROMPT_REGISTRY
from dataflow_pcb_inspection import PCBMultiImageAnnotationGenerator, PCBTemplateSynthesisGenerator
from dataflow_pcb_inspection.core.label_studio import to_label_studio, from_label_studio
from dataflow_pcb_inspection.core.review import merge_decision, reconcile
from dataflow_pcb_inspection.pipelines.inspection import PCBInspectionPipeline

pytestmark = pytest.mark.cpu


class FakeServing:
    def __init__(self, responses=None, failure=None):
        self.responses = responses
        self.failure = failure
        self.calls = []

    def generate_from_input_multi_images(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        if self.responses is not None:
            return self.responses
        return [json.dumps({"verdict": "normal", "findings": [], "note": "offline fixture"})
                for _ in kwargs["list_of_image_paths"]]


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    normal = np.full((24, 32, 3), 180, dtype=np.uint8)
    donor = normal.copy()
    donor[3:7, 5:8] = 20
    mask = np.zeros((24, 32), dtype=np.uint8)
    mask[3:7, 5:8] = 255
    for name, array in [("normal", normal), ("donor", donor), ("mask", mask),
                        ("allowed", np.full((24, 32), 255, dtype=np.uint8))]:
        Image.fromarray(array).save(root / f"{name}.png")
    sample = {"id": "pcb-1", "inspection_rules": "Fixture only",
              "images": [{"id": "target", "role": "inspection", "path": "donor.png"},
                         {"id": "ref-1", "role": "reference", "path": "normal.png"},
                         {"id": "ref-2", "role": "reference", "path": "normal.png"}]}
    return root, sample


def defect():
    return {"verdict": "defect", "note": "visible scratch",
            "findings": [{"image_id": "target", "category": "scratch", "bbox_xyxy": [5, 3, 8, 7], "note": "scratch"}]}


def prediction(source):
    root, sample = source
    return PCBMultiImageAnnotationGenerator(FakeServing([json.dumps(defect())]), root).process([sample])[0]


def decision(annotation):
    return {"sample_id": annotation["sample_id"], "binding": annotation["binding"],
            "decision_id": "review-1", "decision": "accept", "reviewer": "fixture-reviewer", "reason": "inspected fixture"}


def test_registration_and_role_binding(source):
    root, sample = source
    serving = FakeServing()
    assert OPERATOR_REGISTRY.get("PCBMultiImageAnnotationGenerator") is PCBMultiImageAnnotationGenerator
    assert "PCBInspectionPrompt" in PROMPT_REGISTRY._obj_map
    outputs = PCBMultiImageAnnotationGenerator(serving, root).process([sample])
    assert outputs[0]["status"] == "needs_review"
    assert serving.calls[0]["list_of_image_labels"] == [["inspection:target", "reference:ref-1", "reference:ref-2"]]
    assert len(outputs[0]["sample"]["images"]) == 3
    assert sample["images"][0].get("sha256") is None


@pytest.mark.parametrize("raw", ["", "{bad}", '[]', '{"verdict":"normal"}',
                                 json.dumps({**defect(), "findings": [{**defect()["findings"][0], "image_id": "ref-1"}]}),
                                 json.dumps({**defect(), "findings": [{**defect()["findings"][0], "bbox_xyxy": [0, 0, 999, 999]}]})])
def test_invalid_model_outputs_never_become_normal(source, raw):
    root, sample = source
    result = PCBMultiImageAnnotationGenerator(FakeServing([raw]), root).process([sample])[0]
    assert result["status"] == "failed"
    assert result["prediction"] is None
    assert result["error"]["stage"] == "parse"


def test_bad_input_isolated_before_model_and_strict_mode(source):
    root, sample = source
    bad = copy.deepcopy(sample)
    bad["images"][0]["path"] = "../missing.png"
    serving = FakeServing()
    results = PCBMultiImageAnnotationGenerator(serving, root).process([bad, sample])
    assert [x["status"] for x in results] == ["failed", "needs_review"]
    assert len(serving.calls[0]["list_of_image_paths"]) == 1
    with pytest.raises(ValueError):
        PCBMultiImageAnnotationGenerator(serving, root, on_error="raise").process([bad])


@pytest.mark.parametrize("serving", [FakeServing([]), FakeServing(failure=TimeoutError("fixture timeout"))])
def test_request_failures_keep_source_correspondence(source, serving):
    root, sample = source
    second = {**sample, "id": "second"}
    result = PCBMultiImageAnnotationGenerator(serving, root).process([sample, second])
    assert [x["sample_id"] for x in result] == ["pcb-1", "second"]
    assert all(x["status"] == "failed" and x["error"]["stage"] == "request" for x in result)


def test_batch_partition_does_not_change_binding_or_prediction(source):
    root, sample = source
    samples = [{**sample, "id": f"sample-{i}"} for i in range(5)]
    operator = PCBMultiImageAnnotationGenerator(FakeServing(), root)
    combined = operator.process(samples)
    partitioned = operator.process(samples[:2]) + operator.process(samples[2:])
    assert combined == partitioned


def test_real_storage_pipeline_and_compile_key_validation(source, tmp_path):
    root, sample = source
    inputs = tmp_path / "input.jsonl"
    inputs.write_text(json.dumps({"sample": sample}) + "\n")
    direct = PCBMultiImageAnnotationGenerator(FakeServing(), root).process([sample])[0]
    pipeline = PCBInspectionPipeline(inputs, tmp_path / "pipeline", FakeServing(), root)
    pipeline.compile()
    pipeline.forward()
    output = pd.read_json(next((tmp_path / "pipeline").glob("*.jsonl")), lines=True)
    assert output.iloc[0]["pcb_annotation"] == direct
    assert output.iloc[0]["sample"] == sample
    serialized = output.iloc[0]["pcb_annotation"]
    assert merge_decision(serialized, decision(serialized), image_root=root)["status"] == "reviewed"
    # Typo is a schema error during compilation, before any model call.
    class Broken(PCBInspectionPipeline):
        def forward(self):
            self.annotation.run(storage=self.storage.step(), input_key="typo", output_key="result")
    with pytest.raises(KeyError):
        Broken(inputs, tmp_path / "broken", FakeServing(), root).compile()


def test_human_review_is_idempotent_and_rejects_stale_content(source):
    annotation = prediction(source)
    root, _ = source
    human = decision(annotation)
    merged = merge_decision(annotation, human, image_root=root)
    assert merged["status"] == "reviewed"
    assert merge_decision(merged, human, image_root=root) == merged
    with pytest.raises(ValueError, match="overwritten"):
        merge_decision(merged, {**human, "decision_id": "other"}, image_root=root)
    with pytest.raises(ValueError, match="version"):
        merge_decision(annotation, {**human, "binding": "old"}, image_root=root)
    tampered = copy.deepcopy(annotation)
    tampered["prediction"]["note"] = "a different prediction"
    with pytest.raises(ValueError, match="version binding"):
        merge_decision(tampered, human, image_root=root)
    Image.new("RGB", (32, 24), "red").save(root / "donor.png")
    with pytest.raises(ValueError, match="changed"):
        merge_decision(annotation, human, image_root=root)


def test_reconciliation_preserves_original_and_correction(source):
    root, sample = source
    sample["original_annotations"] = {"verdict": "normal", "findings": [], "note": "old"}
    annotation = prediction((root, sample))
    result = reconcile(annotation)
    assert result["label_review"] == {"relation": "change", "requires_human": True}
    assert result["sample"]["original_annotations"]["verdict"] == "normal"
    corrected = {**decision(annotation), "decision": "correct", "annotation": {"verdict": "normal", "findings": [], "note": "human correction"}}
    assert merge_decision(annotation, corrected, image_root=source[0])["final_annotation"]["verdict"] == "normal"


def test_label_studio_round_trip_preserves_geometry_and_rejects_unknown_shapes(source):
    annotation = prediction(source)
    task = to_label_studio(annotation, categories=["scratch", "dot"])
    task["annotations"] = [{"id": 42, "result": copy.deepcopy(task["predictions"][0]["result"])}]
    human = from_label_studio(task, reviewer="human")
    assert human["annotation"]["verdict"] == annotation["prediction"]["verdict"]
    assert human["annotation"]["findings"][0]["bbox_xyxy"] == pytest.approx(annotation["prediction"]["findings"][0]["bbox_xyxy"])
    merged = merge_decision(annotation, human, image_root=source[0])
    assert merged["status"] == "reviewed"
    bad = copy.deepcopy(task)
    bad["annotations"][0]["result"][-1]["type"] = "polygonlabels"
    with pytest.raises(ValueError, match="unsupported"):
        from_label_studio(bad, reviewer="human")


def test_normal_sample_needs_explicit_label_studio_vocabulary(source):
    root, sample = source
    annotation = PCBMultiImageAnnotationGenerator(FakeServing(), root).process([sample])[0]
    with pytest.raises(ValueError, match="vocabulary"):
        to_label_studio(annotation)
    assert '<Label value="scratch"/>' in to_label_studio(annotation, categories=["scratch"])["label_config"]


def test_failed_sample_cannot_be_approved(source):
    root, sample = source
    failed = PCBMultiImageAnnotationGenerator(FakeServing([""]), root).process([sample])[0]
    with pytest.raises(ValueError, match="failed prediction"):
        merge_decision(failed, {}, image_root=root)


@pytest.mark.parametrize("transform", ["identity", "flip_lr", "flip_ud", "rotate_180"])
def test_synthesis_end_to_end_reproducible_and_review_bound(source, tmp_path, transform):
    root, _ = source
    spec = {"id": "candidate", "category": "scratch", "source_split": "train", "target_split": "train",
            "source_image": "donor.png", "source_mask": "mask.png", "source_reference": "normal.png",
            "target_image": "normal.png", "candidate_mask": "allowed.png", "allowed_mask": "allowed.png",
            "seed": 7, "pair_transform": transform,
            "acceptance": {"min_area": 1, "max_area": 40, "max_clip_fraction": 0,
                           "visibility_threshold": 1, "min_visible_pixels": 1}}
    output = tmp_path / "generated"
    generator = PCBTemplateSynthesisGenerator(root, output)
    result = generator.synthesize(spec)
    second = PCBTemplateSynthesisGenerator(root, tmp_path / "second").synthesize(spec)
    assert result["binding"] == second["binding"]
    assert result["status"] == "needs_review"
    assert result["synthesis"]["semantic_validation"] == "not_performed"
    folder = output / "assets/candidate"
    mask = np.array(Image.open(folder / "mask.png")) > 0
    image = np.array(Image.open(folder / "image.png"))
    normal = np.array(Image.open(folder / "reference.png"))
    ys, xs = np.where(mask)
    assert result["prediction"]["findings"][0]["bbox_xyxy"] == [xs.min(), ys.min(), xs.max()+1, ys.max()+1]
    assert not np.any(np.any(image != normal, axis=2) & ~mask)
    assert merge_decision(result, decision(result), image_root=output)["status"] == "reviewed"
    Image.new("L", (32, 24), 0).save(folder / "mask.png")
    with pytest.raises(ValueError, match="artifact changed"):
        merge_decision(result, decision(result), image_root=output)
    with pytest.raises(ValueError, match="train"):
        generator.synthesize({**spec, "source_split": "test"})
    with pytest.raises((ValueError, FileExistsError)):
        generator.synthesize(spec)
