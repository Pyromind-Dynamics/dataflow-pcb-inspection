from __future__ import annotations

import base64
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time

import pandas as pd
import pytest
from PIL import Image

from dataflow.core import LLMServingABC
from dataflow_pcb_inspection import PCBMultiImageAnnotationGenerator
from dataflow_pcb_inspection.core.review import merge_decision, review_packet
from dataflow_pcb_inspection.pipelines.inspection import PCBInspectionPipeline, PCBInspectionStreamPipeline
from test_extension import FakeServing, decision, prediction, source

pytestmark = pytest.mark.cpu


@pytest.mark.parametrize('batch_size', [1, 7, 32])
def test_real_streaming_batches_match_direct_results(source, tmp_path, batch_size):
    root, sample = source
    samples = [{**copy.deepcopy(sample), 'id': f'sample-{i}'} for i in range(35)]
    for index in range(0,35,9):
        samples[index]['images'][0]['path']='missing.png'
    manifest=tmp_path/'input.jsonl'
    manifest.write_text(''.join(json.dumps({'sample':x})+'\n' for x in samples))
    serving=FakeServing()
    pipeline=PCBInspectionStreamPipeline(manifest,tmp_path/'steps',serving,root)
    pipeline.compile()
    pipeline.forward(batch_size=batch_size)
    rows=[json.loads(x) for x in (tmp_path/'steps/pcb_step1.jsonl').read_text().splitlines()]
    expected=PCBMultiImageAnnotationGenerator(FakeServing(),root).process(samples)
    assert [x['pcb_annotation'] for x in rows]==expected
    assert len(rows)==35
    assert sum(x['pcb_annotation']['status']=='failed' for x in rows)==4
    assert max(len(x['list_of_image_paths']) for x in serving.calls)<=batch_size
    with pytest.raises(ValueError,match='already contains'):
        pipeline.forward(batch_size=batch_size)


def test_repeated_images_are_verified_once_per_call_and_cache_is_not_stale(source, monkeypatch):
    root,sample=source
    real_open=Image.open
    opened=[]
    def track(*args,**kwargs):
        opened.append(str(args[0]))
        return real_open(*args,**kwargs)
    monkeypatch.setattr(Image,'open',track)
    operator=PCBMultiImageAnnotationGenerator(FakeServing(),root)
    results=operator.process([{**sample,'id':str(i)} for i in range(50)])
    # Two unique files, each opened for verification and dimensions, despite repeated roles.
    assert len(opened)==4
    previous=results[0]['sample']['images'][0]['sha256']
    Image.new('RGB',(32,24),'blue').save(root/'donor.png')
    result=operator.process([sample])[0]
    assert result['sample']['images'][0]['sha256']!=previous


def test_image_modified_during_inference_is_rejected(source):
    root,sample=source
    class Mutating(FakeServing):
        def generate_from_input_multi_images(self,**kwargs):
            Image.new('RGB',(32,24),'red').save(root/'donor.png')
            return super().generate_from_input_multi_images(**kwargs)
    result=PCBMultiImageAnnotationGenerator(Mutating(),root).process([sample])[0]
    assert result['status']=='failed'
    assert result['error']['stage']=='input_changed'


def test_mismatched_file_extension_never_reaches_actual_serving(source):
    root,sample=source
    (root/'disguised.bmp').write_bytes((root/'donor.png').read_bytes())
    sample['images'][0]['path']='disguised.bmp'
    serving=FakeServing()
    result=PCBMultiImageAnnotationGenerator(serving,root).process([sample])[0]
    assert result['status']=='failed' and result['error']['stage']=='input'
    assert not serving.calls


def test_review_rejects_wrong_outer_id_and_corrupted_final_result(source):
    annotation=prediction(source)
    bad=copy.deepcopy(annotation);bad['sample_id']='another'
    with pytest.raises(ValueError,match='sample_id'):
        review_packet(bad)
    human=decision(annotation)
    reviewed=merge_decision(annotation,human,image_root=source[0])
    reviewed['final_annotation']={'verdict':'normal','findings':[],'note':'corrupted'}
    with pytest.raises(ValueError,match='stored review'):
        merge_decision(reviewed,human,image_root=source[0])


def test_strict_pipeline_failure_releases_actual_serving_interface(source,tmp_path):
    root,sample=source
    class Serving(FakeServing,LLMServingABC):
        closed=0
        def generate_from_input(self,*args,**kwargs):
            raise NotImplementedError
        def start_serving(self):
            pass
        def cleanup(self):
            self.closed+=1
    serving=Serving(['not json'])
    path=tmp_path/'input.jsonl';path.write_text(json.dumps({'sample':sample})+'\n')
    pipeline=PCBInspectionPipeline(path,tmp_path/'steps',serving,root,on_error='raise')
    pipeline.compile()
    with pytest.raises(ValueError):
        pipeline.forward()
    assert serving.closed==1


@pytest.mark.parametrize('options', [{'timeout':float('nan')},{'timeout':float('inf')},
                                      {'timeout':True},{'request_workers':0},{'request_mode':'unknown'}])
def test_invalid_execution_configuration_is_rejected(source,options):
    with pytest.raises(ValueError):
        PCBMultiImageAnnotationGenerator(FakeServing(),source[0],**options)


@pytest.fixture
def local_vlm():
    captured=[]
    lock=threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            content=payload['messages'][0]['content']
            sample_id=json.loads(content[0]['text'])['fixture_id']
            with lock:captured.append((sample_id,payload))
            if sample_id=='slow':time.sleep(.4)
            elif sample_id=='first':time.sleep(.03)
            if sample_id=='fail':
                status=500;body={'error':{'message':'injected request failure','type':'fixture'}}
            else:
                status=200;body={'id':'local-fixture','object':'chat.completion','created':0,'model':'fixture',
                    'choices':[{'index':0,'message':{'role':'assistant','content':json.dumps({
                        'verdict':'uncertain','findings':[],'note':sample_id})},'finish_reason':'stop'}]}
            raw=json.dumps(body).encode()
            try:
                self.send_response(status);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.02},daemon=True)
    thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}/v1',captured
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)
        assert not thread.is_alive()


def test_real_dataflow_http_client_isolates_errors_and_timeouts(source,local_vlm,monkeypatch):
    # This exercises real JSON/base64, HTTP, SDK parsing and concurrent DataFlow serving.
    # The endpoint is local and deterministic; it proves no model accuracy.
    from dataflow.serving.api_vlm_serving_openai import APIVLMServing_openai
    root,sample=source
    url,captured=local_vlm
    monkeypatch.setenv('PCB_LOCAL_FIXTURE_KEY','synthetic-local-key')
    serving=APIVLMServing_openai(api_url=url,key_name_of_api_key='PCB_LOCAL_FIXTURE_KEY',model_name='fixture',max_workers=2)
    serving.client=serving.client.with_options(max_retries=0)
    class Prompt:
        version='transport-fixture-v1'
        def build_prompt(self,sample,categories):return json.dumps({'fixture_id':sample['id']})
    samples=[{**sample,'id':i} for i in ('first','second','fail','slow')]
    try:
        results=PCBMultiImageAnnotationGenerator(serving,root,prompt_template=Prompt(),timeout=.15,
            request_mode='isolated',request_workers=4,model_identity='local-fixture').process(samples)
    finally:serving.cleanup()
    assert [x['sample_id'] for x in results]==['first','second','fail','slow']
    assert [x['status'] for x in results]==['needs_review','needs_review','failed','failed']
    assert [x['prediction']['note'] for x in results[:2]]==['first','second']
    assert all(x['error']['stage']=='request' for x in results[2:])
    assert sorted(x[0] for x in captured)==['fail','first','second','slow']
    for _,body in captured:
        assert body['model']=='fixture'
        assert body['response_format']['json_schema']['strict'] is True
        content=body['messages'][0]['content']
        assert [x['text'] for x in content[1::2]]==['inspection:target:','reference:ref-1:','reference:ref-2:']
        for part,image in zip(content[2::2],sample['images']):
            assert base64.b64decode(part['image_url']['url'].split(',',1)[1])==(root/image['path']).read_bytes()
