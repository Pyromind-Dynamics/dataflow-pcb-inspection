from pathlib import Path

from dataflow.core import OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from ._contracts import describe, read_new_output

from ..core.label_studio import from_label_studio, to_label_studio
from ..core.review import merge_decision, reconcile, review_packet


class _PCBMapper(OperatorABC):
    def run(self, storage, input_key: str = "pcb_annotation", output_key: str = "pcb_review"):
        frame = read_new_output(storage, input_key, output_key)
        frame[output_key] = [self.transform(x) for x in frame[input_key]]
        storage.write(frame)
        return output_key


@OPERATOR_REGISTRY.register()
class PCBLabelReconciler(_PCBMapper):
    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        return describe(PCBLabelReconciler, lang)

    transform = staticmethod(reconcile)


@OPERATOR_REGISTRY.register()
class PCBReviewExporter(_PCBMapper):
    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        return describe(PCBReviewExporter, lang)

    transform = staticmethod(review_packet)


@OPERATOR_REGISTRY.register()
class PCBReviewDecisionMerger(_PCBMapper):
    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        return describe(PCBReviewDecisionMerger, lang)

    def __init__(self, decisions, image_root):
        super().__init__()
        self.image_root = Path(image_root)
        self.decisions = {}
        for decision in decisions:
            key = (decision["sample_id"], decision["binding"])
            if key in self.decisions and self.decisions[key] != decision:
                raise ValueError("conflicting decisions for the same prediction")
            self.decisions[key] = decision

    def transform(self, annotation):
        key = (annotation.get("sample_id"), annotation.get("binding"))
        decision = self.decisions.get(key)
        if decision is None:
            return annotation
        return merge_decision(annotation, decision, image_root=self.image_root)


@OPERATOR_REGISTRY.register()
class PCBLabelStudioExporter(_PCBMapper):
    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        return describe(PCBLabelStudioExporter, lang)

    def __init__(self, categories=()):
        super().__init__()
        self.categories = tuple(categories)

    def transform(self, annotation):
        return to_label_studio(annotation, categories=self.categories)


@OPERATOR_REGISTRY.register()
class PCBLabelStudioImporter(_PCBMapper):
    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        return describe(PCBLabelStudioImporter, lang)

    def __init__(self, reviewer):
        super().__init__()
        self.reviewer = reviewer

    def transform(self, task):
        return from_label_studio(task, reviewer=self.reviewer)
