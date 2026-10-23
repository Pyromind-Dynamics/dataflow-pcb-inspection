"""Public field contracts against real DataFlow compilation and storage boundaries."""
import inspect
from pathlib import Path
import json

import pandas as pd
import pytest
from dataflow.pipeline import PipelineABC
from dataflow.utils.storage import FileStorage
import dataflow_pcb_inspection as extension

pytestmark = pytest.mark.cpu
NAMES = ['PCBMultiImageAnnotationGenerator', 'PCBLabelReconciler', 'PCBReviewExporter', 'PCBReviewDecisionMerger', 'PCBLabelStudioExporter', 'PCBLabelStudioImporter', 'PCBTemplateSynthesisGenerator']


class NeverServing:
    def generate_from_input_multi_images(self, **kwargs):
        raise AssertionError('compilation/field rejection must not call a model')


def make_operator(name, tmp_path):
    serving = NeverServing()
    options = {
        'PCBMultiImageAnnotationGenerator': dict(llm_serving=serving, image_root=tmp_path / 'source'),
        'PCBReviewDecisionMerger': dict(decisions=[], image_root=tmp_path / 'source'),
        'PCBLabelStudioImporter': dict(reviewer='offline-reviewer'),
        'PCBTemplateSynthesisGenerator': dict(image_root=tmp_path / 'source', output_dir=tmp_path / 'products'),
    }
    return getattr(extension, name)(**options.get(name, {}))


class ContractPipeline(PipelineABC):
    def __init__(self, op, source, cache, use_defaults):
        super().__init__()
        self.op = op
        self.storage = FileStorage(str(source), cache_path=str(cache), cache_type='jsonl')
        self.use_defaults = use_defaults

    def forward(self):
        if self.use_defaults:
            self.op.run(storage=self.storage.step())
        else:
            self.op.run(storage=self.storage.step(), input_key='custom_input', output_key='custom_output')


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('mode', ['default', 'custom', 'missing'])
def test_compile_declares_real_dependencies_without_execution(name, mode, tmp_path):
    op = make_operator(name, tmp_path)
    signature = inspect.signature(op.run)
    input_key = signature.parameters['input_key'].default if mode == 'default' else 'custom_input'
    output_key = signature.parameters['output_key'].default if mode == 'default' else 'custom_output'
    source = tmp_path / 'input.jsonl'
    source.write_text(json.dumps({'unrelated' if mode == 'missing' else input_key: {'path': '/missing'}})+'\n')
    before = {str(p) for p in tmp_path.rglob('*') if p.is_file()}
    pipeline = ContractPipeline(op, source, tmp_path / 'steps', mode == 'default')
    if mode == 'missing':
        with pytest.raises(KeyError):
            pipeline.compile()
    else:
        pipeline.compile()
        node = next(node for node in pipeline.op_nodes_list if node.op_obj is op)
        assert set(node.input_keys) == {input_key}
        assert set(node.output_keys) == {output_key}
    assert {str(p) for p in tmp_path.rglob('*') if p.is_file()} == before


class MemoryStorage:
    def __init__(self, frame):
        self.frame = frame.copy(deep=True)
        self.writes = 0
    def read(self, kind): return self.frame
    def write(self, frame):
        self.frame = frame
        self.writes += 1


@pytest.mark.parametrize('name', NAMES)
def test_existing_output_rejected_before_side_effects(name, tmp_path):
    op = make_operator(name, tmp_path)
    original = pd.DataFrame({'source': [{'path': '/missing'}], 'kept': ['keep me']})
    storage = MemoryStorage(original)
    with pytest.raises(ValueError, match='already exists'):
        op.run(storage, input_key='source', output_key='kept')
    pd.testing.assert_frame_equal(storage.frame, original)
    assert storage.writes == 0
    assert not (tmp_path / 'products').exists()


@pytest.mark.parametrize('name', NAMES)
def test_empty_input_keeps_schema_and_missing_column_is_task_error(name, tmp_path):
    op = make_operator(name, tmp_path)
    storage = MemoryStorage(pd.DataFrame(columns=['source', 'unrelated']))
    assert op.run(storage, input_key='source', output_key='result') == 'result'
    assert list(storage.frame.columns) == ['source', 'unrelated', 'result']
    assert len(storage.frame) == 0
    with pytest.raises(KeyError, match='missing input column'):
        op.run(MemoryStorage(pd.DataFrame({'other': [1]})), input_key='source', output_key='result')
    assert not (tmp_path / 'products').exists()


@pytest.mark.parametrize('name', ['PCBLabelReconciler', 'PCBReviewExporter', 'PCBReviewDecisionMerger', 'PCBLabelStudioExporter', 'PCBLabelStudioImporter'])
def test_real_review_conversions_preserve_rows_and_source_columns(name, tmp_path):
    import copy
    from importlib.resources import files
    from dataflow_pcb_inspection.core.annotation import normalize_sample, prediction_binding
    from dataflow_pcb_inspection.core.label_studio import to_label_studio
    data = files('dataflow_pcb_inspection').joinpath('data')
    sample, _ = normalize_sample(json.loads(data.joinpath('sample.json').read_text()), Path(str(data)))
    prediction = sample['original_annotations']
    annotation = dict(sample_id=sample['id'], status='needs_review', sample=sample, prediction=prediction)
    annotation['binding'] = prediction_binding(sample, prediction, None)
    op = make_operator(name, tmp_path)
    if name == 'PCBLabelStudioExporter':
        op = extension.PCBLabelStudioExporter(categories=['scratch'])
    payload = annotation
    if name == 'PCBLabelStudioImporter':
        payload = to_label_studio(annotation, categories=['scratch'])
        payload['annotations'] = [{'id': 'offline-roundtrip', 'result': copy.deepcopy(payload['predictions'][0]['result'])}]
    original = pd.DataFrame({'source': [payload, copy.deepcopy(payload)], 'id': [7, 3]})
    storage = MemoryStorage(original)
    op.run(storage, input_key='source', output_key='converted')
    pd.testing.assert_frame_equal(storage.frame[['source', 'id']], original)
    assert len(storage.frame['converted']) == 2
    if name == 'PCBLabelStudioImporter':
        assert storage.frame['converted'].iloc[0]['decision'] == 'correct'
    if name == 'PCBLabelReconciler':
        assert storage.frame['converted'].iloc[0]['label_review']['relation'] == 'keep'
