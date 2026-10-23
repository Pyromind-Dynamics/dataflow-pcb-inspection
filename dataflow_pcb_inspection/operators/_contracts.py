"""Field safety and operator descriptions; no model or media I/O at import time."""
import inspect


def read_new_output(storage, input_key: str, output_key: str):
    """Reject field mistakes before model calls or external artifact writes."""
    if not isinstance(input_key, str) or not input_key.strip():
        raise ValueError("input_key must be a non-empty string")
    if not isinstance(output_key, str) or not output_key.strip():
        raise ValueError("output_key must be a non-empty string")
    if input_key == output_key:
        raise ValueError("output_key must preserve the source input column")
    frame = storage.read("dataframe")
    if input_key not in frame.columns:
        raise KeyError(f"missing input column: {input_key}")
    if output_key in frame.columns:
        raise ValueError(f"output column already exists: {output_key}; choose a new output_key")
    return frame


def describe(cls, lang="zh"):
    """Use real signatures for defaults and per-operator semantic descriptions."""
    index = 0 if lang == "zh" else 1
    spec = _SPECS[cls.__name__]
    lines = [cls.__name__ + ": " + spec["purpose"][index]]
    lines.append("初始化参数：" if index == 0 else "Constructor parameters:")
    for name, parameter in inspect.signature(cls).parameters.items():
        default = ("必填" if index == 0 else "required") if parameter.default is inspect.Parameter.empty else repr(parameter.default)
        detail = _PARAMS[name][index]
        if name == "categories":
            detail = {
                "PCBMultiImageAnnotationGenerator": ("允许类别词表；空元组不限制类别，但仍校验结果结构。", "Allowed categories; an empty tuple leaves category names unrestricted while still validating structure."),
                "PCBLabelStudioExporter": ("允许类别词表；空元组从已有 findings 推导，正常样本需显式非空词表。", "Allowed categories; an empty tuple infers from findings, so normal samples need an explicit nonempty vocabulary."),
                "PCBTemplateSynthesisGenerator": ("允许合成的类别；空词表不接受任何类别。", "Allowed synthesis categories; an empty vocabulary accepts no categories."),
            }[cls.__name__][index]
        lines.append(f"- {name} ({default}): {detail}")
    lines.append("字段参数：" if index == 0 else "Field parameters:")
    for name, parameter in inspect.signature(cls.run).parameters.items():
        if name in {"self", "storage"}:
            continue
        detail = spec["input"][index] if name.startswith("input_") else spec["output"][index]
        lines.append(f"- {name} ({parameter.default!r}): {detail}")
    lines.append(("run 通过 storage 读写 DataFrame，新增结果列，保留行数、顺序和所有源列；输出列必须尚不存在。返回 output_key 字符串。" if index == 0 else
                  "run reads/writes a DataFrame through storage, adding a new result column while preserving row count, order and all source columns. The output column must not exist. Returns the output_key string."))
    lines.append(spec["effects"][index])
    if cls.__name__.startswith("Embodied"):
        lines.append("直接 transform(value) 不经过逐行隔离，错误直接抛出。run 的 on_error 仅处理预期记录级错误；Storage/字段错误抛出。" if index == 0 else
                     "Direct transform(value) bypasses row isolation and propagates errors. run on_error handles expected record errors only; Storage/field errors propagate.")
    return "\n".join(lines)


_PARAMS = {'llm_serving': ('外部注入的 Serving，须实现 generate_from_input_multi_images，按请求顺序返回等长列表。',
                 'Injected Serving implementing generate_from_input_multi_images; return one '
                 'response per request in order.'),
 'image_root': ('本地图片资源根目录；相对图片路径以此解析。',
                'Local image root used to resolve relative resource paths.'),
 'prompt_template': ('None 使用 PCBInspectionPrompt；自定义对象实现 build_prompt(sample, categories)。',
                     'None selects PCBInspectionPrompt; custom objects implement '
                     'build_prompt(sample, categories).'),
 'categories': ('允许的缺陷类别词表；空值的推导规则与对应算子契约一致。',
                'Allowed category vocabulary; empty-vocabulary inference follows the operator '
                'contract.'),
 'timeout': ('传给 Serving 的秒数；不是扩展实现的强制总时限。',
             'Timeout in seconds passed to Serving; not an extension-enforced total deadline.'),
 'on_error': ('isolate 保留失败行；raise 抛出预期记录错误。整列缺失、输出冲突及 Storage 错误始终抛出。',
              'isolate retains failed rows; raise propagates expected record errors. Missing '
              'columns, output conflicts and Storage errors always propagate.'),
 'model_identity': ('调用方提供的模型/配置标识，用于预测版本绑定。',
                    'Caller-supplied model/configuration identity used in prediction binding.'),
 'request_mode': ('batch 批量调用；isolated 逐样本有界并发，不额外自动重试。',
                  'batch sends batches; isolated uses bounded per-sample concurrency without added '
                  'retries.'),
 'request_workers': ('isolated 模式的最大并发数，正整数。', 'Positive maximum concurrency for isolated mode.'),
 'decisions': ('含 sample_id/binding/decision_id/reviewer/reason 的决策序列；correct 另需 annotation。',
               'Decisions carrying sample_id/binding/decision_id/reviewer/reason; correct also '
               'requires annotation.'),
 'reviewer': ('写入导入决策的审核人标识；不执行用户认证。',
              'Reviewer identity recorded in imported decisions; no user authentication is '
              'performed.'),
 'output_dir': ('本地输出目录，须在来源之外；已有产物不覆盖。',
                'Local output directory outside the source; existing artifacts are not '
                'overwritten.')}

_SPECS = {'PCBMultiImageAnnotationGenerator': {'purpose': ('多图结构化预标注；保留每条输入及图片对应关系。',
                                                  'Generate multi-image PCB pre-annotations while '
                                                  'retaining every source row and image binding.'),
                                      'input': ('sample 字典：id、images（id/role/path），可选 '
                                                'inspection_rules/original_annotations。',
                                                'sample dict: id, images (id/role/path), optional '
                                                'inspection_rules/original_annotations.'),
                                      'output': ('pcb.v1 '
                                                 '结果：status=needs_review/failed，prediction、binding '
                                                 '或 error；不是已审核真值。',
                                                 'pcb.v1 result: status=needs_review/failed, '
                                                 'prediction and binding or error; not reviewed '
                                                 'ground truth.'),
                                      'effects': ('读取图片并调用注入模型；不写媒体。isolate 保留逐条失败；raise '
                                                  '抛错。process(samples) 遵循同一错误模式。',
                                                  'Reads images and calls the injected model; '
                                                  'writes no media. isolate retains per-row '
                                                  'failures; raise propagates errors. '
                                                  'process(samples) follows the same error mode.')},
 'PCBLabelReconciler': {'purpose': ('核对已有标注与预测，保留差异，追加核对结果。',
                                    'Reconcile existing labels and predictions while preserving '
                                    'discrepancies.'),
                        'input': ('成功的 pcb.v1 预标注结果，含 sample/prediction/binding，可带原标注。',
                                  'Successful pcb.v1 annotation containing '
                                  'sample/prediction/binding and optional original labels.'),
                        'output': ('保留原内容并增加 label_review；不自动批准或覆盖真值。',
                                   'Original content plus label_review; no automatic approval or '
                                   'ground-truth replacement.'),
                        'effects': ('仅处理内存结构，不写文件、不调用模型。非 needs_review 状态原样保留；成功候选的绑定或结构错误直接抛出。',
                                    'In-memory conversion only, with no files or model calls. '
                                    'Non-needs_review states pass through; successful candidates '
                                    'with invalid binding/structure raise errors.')},
 'PCBReviewExporter': {'purpose': ('生成绑定预测版本的待复核数据包。',
                                   'Build a review packet bound to a prediction version.'),
                       'input': ('成功的 pcb.v1 预标注结果。', 'Successful pcb.v1 annotation.'),
                       'output': ('包含 sample_id、binding、sample、prediction 等字段的复核数据包。',
                                  'Review packet including sample_id, binding, sample and '
                                  'prediction.'),
                       'effects': ('返回字典，不自动写 JSON 文件或启动审核页面。契约错误直接抛出。',
                                   'Returns a dictionary; does not write JSON or launch a review '
                                   'UI. Contract errors propagate.')},
 'PCBReviewDecisionMerger': {'purpose': ('将人工决策合并到对应版本的本地预标注。',
                                         'Merge human decisions into the matching local prediction '
                                         'version.'),
                             'input': ('pcb.v1 预标注结果；按 sample_id 与 binding 匹配构造时提供的决策。',
                                       'pcb.v1 annotation; matches constructor decisions by '
                                       'sample_id and binding.'),
                             'output': ('匹配后返回 reviewed/rejected 结果；无匹配时保留原结果。',
                                        'Reviewed/rejected result on a match; otherwise retains '
                                        'the original result.'),
                             'effects': ('读取本地图片/合成证据验证绑定，不写媒体。冲突、过期或损坏决策报错；重复同一决策幂等。',
                                         'Reads local image/synthesis evidence to validate '
                                         'binding; writes no media. Conflicting, stale or damaged '
                                         'decisions fail; repeated identical decisions are '
                                         'idempotent.')},
 'PCBLabelStudioExporter': {'purpose': ('将 PCB 预标注转换为 Label Studio 本地任务结构。',
                                        'Convert PCB annotations to local Label Studio task '
                                        'structures.'),
                            'input': ('成功的 pcb.v1 预标注结果，图片具有原始尺寸和像素 bbox。',
                                      'Successful pcb.v1 annotation with original image dimensions '
                                      'and pixel bounding boxes.'),
                            'output': ('含 data、meta、predictions、label_config 的任务字典；bbox 转百分比。',
                                       'Task dict containing data, meta, predictions and '
                                       'label_config; boxes use percentage coordinates.'),
                            'effects': ('不自动写文件、不上传或调用在线 API。空 categories 从 findings '
                                        '推导；正常样本也必须有非空词表。契约错误报错。',
                                        'Does not write files, upload or call online APIs. Empty '
                                        'categories infer from findings; normal samples also '
                                        'require a nonempty vocabulary. Contract errors '
                                        'propagate.')},
 'PCBLabelStudioImporter': {'purpose': ('将 Label Studio 已完成人工标注转换为版本绑定的 correct 决策。',
                                        'Convert a completed Label Studio annotation into a '
                                        'version-bound correct decision.'),
                            'input': ('保留 meta 的任务字典，恰好一个未取消且带稳定 id 的 annotations。',
                                      'Task dict preserving meta, with exactly one non-cancelled '
                                      'annotation carrying a stable id.'),
                            'output': ('包含 sample_id、binding、decision_id、reviewer、annotation 的 '
                                       'correct 决策。',
                                       'A correct decision containing sample_id, binding, '
                                       'decision_id, reviewer and annotation.'),
                            'effects': ('仅支持未旋转矩形、verdict 单选和 note 文本；不自动合并决策或调用 '
                                        'API。不支持的控件及维度不符报错。',
                                        'Supports unrotated rectangles, verdict choices and note '
                                        'text; no automatic merge or API calls. Unsupported '
                                        'controls or dimension mismatches fail.')},
 'PCBTemplateSynthesisGenerator': {'purpose': ('按真实标注模板生成几何合成候选和来源证据。',
                                               'Generate geometric candidates from labeled '
                                               'templates with provenance.'),
                                   'input': ('synthesis_spec 字典：id/category、显式 train '
                                             '划分、源图/参考图/掩码路径、整数 seed、acceptance。',
                                             'synthesis_spec dict: id/category, explicit train '
                                             'splits, source/reference/mask paths, integer seed '
                                             'and acceptance.'),
                                   'output': ('pcb.v1 needs_review 候选或 failed；含同步图片、框、掩码及来源散列引用。',
                                              'pcb.v1 needs_review candidate or failed result, '
                                              'referencing aligned images, boxes, masks and '
                                              'provenance hashes.'),
                                   'effects': ('写图片、掩码和 provenance；不调用模型，不保证业务语义质量。isolate '
                                               '保留失败行；直接 synthesize(spec) 总是抛出错误；无跨文件事务恢复。',
                                               'Writes images, masks and provenance; no model '
                                               'calls or business-quality guarantee. isolate '
                                               'retains failed rows; direct synthesize(spec) '
                                               'always propagates errors; no cross-file '
                                               'transaction recovery.')}}
