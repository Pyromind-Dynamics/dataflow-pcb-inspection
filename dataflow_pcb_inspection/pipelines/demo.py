"""Offline synthetic fixture demo; never calls a model or records real human review."""
from __future__ import annotations

import argparse
import copy
from importlib.resources import files
import json
from pathlib import Path

from .inspection import PCBInspectionPipeline
from ..core.review import reconcile, merge_decision
from ..core.label_studio import to_label_studio, from_label_studio
from ..operators.synthesis import PCBTemplateSynthesisGenerator


class DemoServing:
    def generate_from_input_multi_images(self, **kwargs):
        return [json.dumps({"verdict": "defect", "findings": [{"image_id": "target", "category": "scratch",
                    "bbox_xyxy": [5, 3, 8, 7], "note": "artificial fixture"}],
                    "note": "DEMO ONLY: fixed response, not a model prediction"}) for _ in kwargs["list_of_image_paths"]]


def run(output_dir):
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    data = Path(str(files("dataflow_pcb_inspection").joinpath("data")))
    sample = json.loads((data / "sample.json").read_text())
    invalid = copy.deepcopy(sample)
    invalid["id"] = "invalid-fixture"
    invalid["images"][0]["path"] = "intentionally-missing.png"
    manifest = output / "input.jsonl"
    manifest.write_text("".join(json.dumps({"sample": x}) + "\n" for x in [sample, invalid]))
    pipeline = PCBInspectionPipeline(manifest, output / "steps", DemoServing(), data,
                                     model_identity="offline-demo-fixed-response")
    pipeline.compile()
    pipeline.forward()
    results = [json.loads(x)["pcb_annotation"] for x in (output / "steps/pcb_step1.jsonl").read_text().splitlines()]
    successful = next(x for x in results if x["status"] == "needs_review")
    successful = reconcile(successful)
    task = to_label_studio(successful, categories=["scratch", "dot", "small_object"])
    config = task.pop("label_config")
    (output / "label_config.xml").write_text(config)
    (output / "label_studio_tasks.json").write_text(json.dumps([task], ensure_ascii=False, indent=2) + "\n")
    # Simulate a tool export solely to demonstrate format conversion and merge.
    task["annotations"] = [{"id": "DEMO-ONLY", "result": copy.deepcopy(task["predictions"][0]["result"])}]
    human = from_label_studio(task, reviewer="DEMO-ONLY-SIMULATED")
    reviewed = merge_decision(successful, human, image_root=data)
    (output / "simulated_review.json").write_text(json.dumps({"demo_only": True, "result": reviewed}, ensure_ascii=False, indent=2) + "\n")
    spec = json.loads((data / "synthesis_spec.json").read_text())
    generated = PCBTemplateSynthesisGenerator(data, output / "synthesis").synthesize(spec)
    summary = {"demo_only": True, "input_count": len(results),
               "prediction_count": sum(x["status"] == "needs_review" for x in results),
               "failed_count": sum(x["status"] == "failed" for x in results),
               "synthesis_status": generated["status"], "model_called": False}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="new output directory")
    print(json.dumps(run(parser.parse_args().output), ensure_ascii=False))
