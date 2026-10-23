from pathlib import Path

from dataflow.pipeline import PipelineABC, StreamBatchedPipelineABC
from dataflow.utils.storage import FileStorage, StreamBatchedFileStorage

from ..operators.annotation import PCBMultiImageAnnotationGenerator


class PCBInspectionPipeline(PipelineABC):
    def __init__(self, input_path, output_dir, llm_serving, image_root, **kwargs):
        super().__init__()
        self.storage = FileStorage(first_entry_file_name=str(input_path), cache_path=str(output_dir),
                                   file_name_prefix="pcb", cache_type="jsonl")
        self.annotation = PCBMultiImageAnnotationGenerator(llm_serving, image_root, **kwargs)

    def forward(self):
        self.annotation.run(storage=self.storage.step(), input_key="sample", output_key="pcb_annotation")

    def _compiled_forward(self, resume_step=0):
        try:
            return super()._compiled_forward(resume_step=resume_step)
        finally:
            if self.active_llm_serving is not None:
                self.active_llm_serving.cleanup()
                self.active_llm_serving = None


class PCBInspectionStreamPipeline(StreamBatchedPipelineABC):
    """Bounded JSONL batches on a fresh output directory; resume is unsupported."""
    def __init__(self, input_path, output_dir, llm_serving, image_root, **kwargs):
        super().__init__()
        self.storage = StreamBatchedFileStorage(first_entry_file_name=str(input_path), cache_path=str(output_dir),
                                                file_name_prefix="pcb", cache_type="jsonl")
        self.annotation = PCBMultiImageAnnotationGenerator(llm_serving, image_root, **kwargs)

    def forward(self):
        self.annotation.run(storage=self.storage.step(), input_key="sample", output_key="pcb_annotation")

    def _compiled_forward(self, resume_step=0, batch_size=32, resume_from_last=False):
        if resume_step != 0 or resume_from_last:
            raise ValueError("extension stream pipeline requires a fresh run; resume is unsupported")
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        if any(Path(self.storage.cache_path).glob("pcb_step*.jsonl")):
            raise ValueError("output already contains step results; choose a new directory")
        try:
            return super()._compiled_forward(resume_step=0, batch_size=batch_size, resume_from_last=False)
        finally:
            if self.active_llm_serving is not None:
                self.active_llm_serving.cleanup()
                self.active_llm_serving = None
