import json

from dataflow.core.prompt import DIYPromptABC
from dataflow.utils.registry import PROMPT_REGISTRY


@PROMPT_REGISTRY.register()
class PCBInspectionPrompt(DIYPromptABC):
    version = "pcb-v1"

    def build_prompt(self, sample, categories=()):
        return (
            "Inspect only images with role inspection. Reference and auxiliary images are evidence, "
            "not the inspection target. Do not infer defects from filenames, contrast or unaligned "
            "pixel differences. Follow the supplied inspection rules. Return JSON with verdict "
            "(defect/normal/uncertain), findings and note. Each finding has image_id, category, "
            "bbox_xyxy (pixel coordinates in the ORIGINAL inspection image) and note. "
            "normal requires empty findings; insufficient evidence means uncertain with a reason. "
            "Do not invent bounding boxes. This is a prediction requiring human review, not final acceptance.\n"
            + json.dumps({"id": sample["id"], "images": sample["images"],
                          "inspection_rules": sample.get("inspection_rules", ""),
                          "categories": list(categories)}, ensure_ascii=False)
        )
