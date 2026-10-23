"""Offline conversion of packaged synthetic ground-truth labels; no model involved."""
import argparse
import json
from importlib.resources import files
from pathlib import Path

from dataflow.pipeline import PipelineABC
from dataflow.utils.storage import FileStorage
from ..core.annotation import normalize_sample, prediction_binding
from ..operators.review import PCBLabelReconciler, PCBLabelStudioExporter


class LabelConversionPipeline(PipelineABC):
    def __init__(self, input_path, output_dir, *, categories=("scratch",)):
        super().__init__()
        self.storage = FileStorage(str(input_path), cache_path=str(output_dir),
                                   file_name_prefix='labels', cache_type='jsonl')
        self.reconciler = PCBLabelReconciler()
        self.exporter = PCBLabelStudioExporter(categories=categories)

    def forward(self):
        self.reconciler.run(storage=self.storage.step(), input_key='annotation', output_key='reconciled')
        self.exporter.run(storage=self.storage.step(), input_key='reconciled', output_key='label_studio')


def run(output_dir):
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    data = files('dataflow_pcb_inspection').joinpath('data')
    sample, _ = normalize_sample(json.loads(data.joinpath('labeled_sample.json').read_text()), Path(str(data)))
    # Existing labels are the input, not a model prediction or a generated human decision.
    prediction = sample['original_annotations']
    annotation = dict(sample_id=sample['id'], status='needs_review', sample=sample,
                      prediction=prediction, binding_config={'origin': 'packaged synthetic labels; no model'})
    annotation['binding'] = prediction_binding(sample, prediction, annotation['binding_config'])
    input_path = output / 'input.jsonl'
    input_path.write_text(json.dumps({'annotation': annotation}) + '\n', encoding='utf-8')
    pipeline = LabelConversionPipeline(input_path, output / 'steps')
    pipeline.compile()
    pipeline.forward()
    row = json.loads((output / 'steps/labels_step2.jsonl').read_text(encoding='utf-8'))
    task = row['label_studio']
    (output / 'label_config.xml').write_text(task.pop('label_config'), encoding='utf-8')
    (output / 'label_studio_tasks.json').write_text(json.dumps([task], ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    summary = {'model_called': False, 'input_count': 1,
               'box_count': len(sample['original_annotations']['findings']),
               'relation': row['reconciled']['label_review']['relation']}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2)+'\n', encoding='utf-8')
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='new output directory')
    print(json.dumps(run(parser.parse_args().output)))
