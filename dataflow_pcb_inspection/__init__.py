"""Importing this package registers PCB operators and prompts without model I/O."""
from .version import __version__
from .prompts import PCBInspectionPrompt
from .operators.annotation import PCBMultiImageAnnotationGenerator
from .operators.review import (PCBLabelReconciler, PCBReviewExporter, PCBReviewDecisionMerger,
                               PCBLabelStudioExporter, PCBLabelStudioImporter)
from .operators.synthesis import PCBTemplateSynthesisGenerator

__all__ = ["__version__", "PCBInspectionPrompt", "PCBMultiImageAnnotationGenerator",
           "PCBLabelReconciler", "PCBReviewExporter", "PCBReviewDecisionMerger",
           "PCBLabelStudioExporter", "PCBLabelStudioImporter", "PCBTemplateSynthesisGenerator"]
