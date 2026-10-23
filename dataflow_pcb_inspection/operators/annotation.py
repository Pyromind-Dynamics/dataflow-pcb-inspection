from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import math
from pathlib import Path

from jsonschema import ValidationError

from dataflow.core import OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from ._contracts import describe, read_new_output

from ..core.annotation import (PREDICTION_SCHEMA, file_signature, normalize_sample,
                               parse_prediction, prediction_binding)
from ..prompts import PCBInspectionPrompt


@dataclass(frozen=True)
class _RequestFailure:
    exception: Exception


@OPERATOR_REGISTRY.register()
class PCBMultiImageAnnotationGenerator(OperatorABC):
    """Preserve every source row; prediction/error lives in one output column."""
    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        return describe(PCBMultiImageAnnotationGenerator, lang)


    def __init__(self, llm_serving, image_root, *, prompt_template=None,
                 categories=(), timeout=1800, on_error="isolate", model_identity="unspecified",
                 request_mode="batch", request_workers=4):
        super().__init__()
        if not callable(getattr(llm_serving, "generate_from_input_multi_images", None)):
            raise TypeError("Serving must implement generate_from_input_multi_images")
        if on_error not in {"raise", "isolate"}:
            raise ValueError("on_error must be raise or isolate")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive")
        if request_mode not in {"batch", "isolated"}:
            raise ValueError("request_mode must be batch or isolated")
        if type(request_workers) is not int or request_workers <= 0:
            raise ValueError("request_workers must be a positive integer")
        if isinstance(categories, str):
            raise ValueError("categories must be a collection of non-empty strings")
        categories = tuple(categories)
        if any(not isinstance(x, str) or not x.strip() for x in categories):
            raise ValueError("categories must be a collection of non-empty strings")
        self.llm_serving = llm_serving
        self.image_root = Path(image_root).resolve()
        self.prompt_template = prompt_template or PCBInspectionPrompt()
        self.categories = tuple(categories)
        self.timeout = timeout
        self.on_error = on_error
        self.model_identity = model_identity
        self.request_mode = request_mode
        self.request_workers = request_workers

    def _request_batch(self, prepared):
        responses = self.llm_serving.generate_from_input_multi_images(
            list_of_image_paths=[x[2] for x in prepared],
            list_of_image_labels=[[f"{i['role']}:{i['id']}" for i in x[1]["images"]] for x in prepared],
            user_prompts=[x[3] for x in prepared], system_prompt="",
            timeout=self.timeout, json_schema=copy.deepcopy(PREDICTION_SCHEMA),
        )
        if not isinstance(responses, list) or len(responses) != len(prepared):
            raise ValueError("Serving response count does not match request count")
        return responses

    def _request(self, prepared):
        if self.request_mode == "batch":
            return self._request_batch(prepared)
        # Opt-in: exactly one serving invocation per sample, with bounded concurrency.
        # A failed batch is never silently retried as individual requests.
        responses = [None] * len(prepared)
        with ThreadPoolExecutor(max_workers=self.request_workers) as executor:
            futures = {executor.submit(self._request_batch, [item]): position
                       for position, item in enumerate(prepared)}
            for future in as_completed(futures):
                position = futures[future]
                try:
                    responses[position] = future.result()[0]
                except Exception as exc:
                    responses[position] = _RequestFailure(exc)
        return responses

    def process(self, samples):
        output = [None] * len(samples)
        prepared = []
        image_cache = {}
        config = {"model": self.model_identity, "categories": list(self.categories),
                  "prompt": getattr(self.prompt_template, "version", type(self.prompt_template).__name__)}

        def failure(index, stage, exc):
            if self.on_error == "raise":
                raise exc
            source = samples[index]
            output[index] = {"schema_version": "pcb.v1", "sample_id": source.get("id") if isinstance(source, dict) else None,
                             "status": "failed", "prediction": None,
                             "error": {"stage": stage, "type": type(exc).__name__, "reason": str(exc)}}

        for index, raw in enumerate(samples):
            try:
                sample, paths = normalize_sample(raw, self.image_root, image_cache=image_cache)
                prompt = self.prompt_template.build_prompt(sample, self.categories)
                prepared.append((index, sample, paths, prompt, [image_cache[path]["signature"] for path in paths]))
            except (ValueError, TypeError, OSError) as exc:
                failure(index, "input", exc)
        if prepared:
            try:
                responses = self._request(prepared)
            except Exception as exc:
                for index, *_ in prepared:
                    failure(index, "request", exc)
                return output
            for (index, sample, paths, prompt, signatures), raw in zip(prepared, responses):
                if isinstance(raw, _RequestFailure):
                    failure(index, "request", raw.exception)
                    continue
                try:
                    if any(file_signature(Path(path)) != signature for path, signature in zip(paths, signatures)):
                        raise ValueError("image changed during model request; prediction discarded")
                except (OSError, ValueError) as exc:
                    failure(index, "input_changed", exc)
                    continue
                try:
                    prediction = parse_prediction(raw, sample, self.categories)
                    binding = prediction_binding(sample, prediction, {**config, "rendered_prompt": prompt})
                    output[index] = {"schema_version": "pcb.v1", "sample_id": sample["id"],
                                     "status": "needs_review", "sample": sample,
                                     "prediction": prediction, "binding": binding,
                                     "binding_config": {**config, "rendered_prompt": prompt}, "error": None}
                except (ValueError, TypeError, KeyError, ValidationError) as exc:
                    failure(index, "parse", exc)
        return output

    def run(self, storage, input_key: str = "sample", output_key: str = "pcb_annotation"):
        frame = read_new_output(storage, input_key, output_key)
        frame[output_key] = self.process(frame[input_key].tolist())
        storage.write(frame)
        return output_key
