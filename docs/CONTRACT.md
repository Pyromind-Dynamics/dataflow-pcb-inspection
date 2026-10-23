# PCB v1 契约与接口

## 输入和输出

默认 JSONL 每行一个 `sample`，包括非空字符串 id、images 列表、可选 inspection_rules/original_annotations。images 每项为唯一 id、role（inspection/reference/auxiliary）和相对 image_root 的 JPEG/PNG 路径。重复角色保留顺序；width/height 若声明须与文件一致。

预测为：

```json
{"verdict":"defect","findings":[{"image_id":"target","category":"scratch","bbox_xyxy":[5,3,8,7],"note":"可定位的现象"}],"note":"预测依据"}
```

坐标为原待检图像的像素 xyxy、右下界外边界。normal 必须无 finding，defect 必须有 finding；uncertain 需要说明。具体分类词表由调用方提供，不代表客户验收规则。

`PCBMultiImageAnnotationGenerator` 构造参数：llm_serving、image_root；可选 prompt_template、categories、timeout=1800、on_error=isolate、model_identity=unspecified。生产调用应传入明确的模型和配置版本标识。Serving 必须提供多图方法，其参数为 list_of_image_paths、list_of_image_labels、user_prompts、system_prompt、timeout、json_schema。注入的 Prompt 实现 `build_prompt(sample, categories)`；Prompt 版本及渲染文本写入绑定配置。

`run(storage, input_key='sample', output_key='pcb_annotation')` 保留全部输入行。成功输出为 schema_version=pcb.v1、sample_id、status=needs_review、sample、prediction、binding、binding_config、error=null。input/request/parse 失败输出 status=failed、prediction=null 和定位原因；raise 模式抛错。源文件 JSONL 读取异常仍由 Storage 处理。

## 核对和复核

- `PCBLabelReconciler`：比较原标注和模型结果的判定、类别、图片、几何，写入 keep/change/unlabeled；不自动覆盖。
- `PCBReviewExporter`：导出稳定样本 ID、binding、原标注和预测。
- `PCBReviewDecisionMerger(decisions, image_root)`：按样本和绑定版本合并，未知样本保留原状态，不视为已经复核。决策中的 sample_id/binding/decision_id/reviewer/reason 必填；decision 为 accept/correct/reject，correct 另带完整 annotation。
- 复核时验证原始本地预测、配置、图片字节及合成产物散列。重复同一决策幂等，冲突报错，旧版本不应用到新候选。复核结果状态 reviewed/rejected，原标注和模型建议保留；reviewed 不等于自动训练就绪。

这些映射算子默认 `run(storage, input_key='pcb_annotation', output_key='pcb_review')`，调用时应显式指定有意义的输入输出列；先分离失败样本，再对成功子集导出复核文件。

## Label Studio 文件转换

`PCBLabelStudioExporter(categories=...)` 与 `PCBLabelStudioImporter(reviewer=...)`，或直接调用 core.label_studio 中的 to_label_studio/from_label_studio。当前支持未旋转 RectangleLabels、一个 verdict Choices、一个 note TextArea。未知控件、旋转、维度不符或多个已完成 annotation 报错；不支持多审核者仲裁。

导出需有非空分类词表（也可由已有 finding 推导），并包含预测中的类别。将返回对象里的 label_config 单独保存为 Label Studio 项目配置，剩余任务写入 JSON 数组。data 中的本地相对路径需由使用者映射成该 Label Studio 实例能访问的媒体地址；本包不签名、不上传、不调用在线 API。保留 meta，以便导入后与原始本地预测合并。

坐标显式做像素/百分比转换，像素值保留十位小数，以数值容差核对，不要求 JSON 字节一致。人工导出需恰好一个未取消的 annotation，有稳定 id；导入后产生 correct 决策，仍须与原始本地预测合并才算完成。

## 模板合成

`PCBTemplateSynthesisGenerator(image_root, output_dir, categories=('scratch','dot','small_object'), on_error='isolate')`：默认输入列 synthesis_spec、输出列 pcb_synthesis，也可直接调用 synthesize(spec)。output_dir 必须在 image_root 之外，已有候选不覆盖。

完整 spec 见包内 data/synthesis_spec.json，包含：id/category、显式 train 划分、source_image/source_reference/source_mask/target_image/candidate_mask/allowed_mask、整数 seed、acceptance。mask 仅有 0/255；acceptance 明确 min_area/max_area/max_clip_fraction/visibility_threshold/min_visible_pixels。可选 max_attempts/scale_xy/angle_deg/pair_transform，后者为 identity/flip_lr/flip_ud/rotate_180。

输出图片、mask、bbox 同步，包含源图、标注、参数和产物散列。默认只有几何与证据检查；status 固定为 needs_review，semantic_validation=not_performed，需领域规则和人工验证。低层 manifest 的 program_status=passed 不代表缺陷语义或训练就绪。

核心 template_synthesis/reference_synthesis 的更细粒度算法和回调随包保留；不含原平台打包器/任务运行器，也不宣称所有类别已具备业务质量验证。中断后产物与 JSONL 输出的事务一致性、失败补跑尚未实现。

## 0.1.1 请求隔离、图片缓存与流式处理

构造参数新增 request_mode（batch/isolated，默认 batch）和 request_workers（正整数，默认 4）。batch 保留底层批量语义：一次 Serving 调用抛错时，这次调用中的样本均记为请求失败。isolated 以有界并发逐样本调用 Serving；一条异常不会丢掉其他样本结果，也不会先批量失败再自动重发。底层 SDK 自身的重试仍以调用方配置为准；本扩展没有新增自动重试或强制取消保证。

重复图片的尺寸、格式和 SHA256 只在单次 process 调用内缓存，用文件签名变化失效；校验和使用 64 KiB 分块读取。推理期间检测到图片变化时结果标为 failed，error.stage=input_changed。文件名扩展名必须与 JPEG/PNG 内容一致。

大文件使用 pipelines.inspection.PCBInspectionStreamPipeline，compile() 后调用 forward(batch_size=32)。采用真实 StreamBatchedFileStorage 分批读写，控制准备样本与图片缓存的规模；不要求把全量结果保留在调用进程。默认禁止恢复，已有步骤结果时拒绝覆盖，必须选择新目录。普通 Pipeline 保留原用法，并在严格失败时释放已激活的 Serving。

人工复核增加顶层 sample_id 一致性检查；重复合并同一决策前还校验已保存的最终结果，拒绝被改写的复核状态或最终标注。

## 2026-10-23 字段契约与调用边界

所有公开 `run(storage, input_key=..., output_key=...)` 均要求非空字符串字段名，输入列必须存在，输出列必须尚不存在。整列缺失、同名输出和 Storage 读写错误属于任务错误，即使 `on_error="isolate"` 也不隔离。输出冲突在模型调用和外部文件写入之前拒绝；迁移已有覆盖用法时选择新输出列。

算子新增结果列，保留外层 DataFrame 的条数、顺序和所有输入列；内部框、子任务段或视频帧的数量变化不等于表格删行。每条结果的领域质量与运行状态分别解释。直接 `transform` / `synthesize` 不经过 Storage 的逐记录隔离层，错误直接抛出；模型算子的 `process` 则遵循其 on_error 配置。

每个公开类的 `get_desc("zh"/"en")` 可独立读取功能、构造参数、默认字段、结果、文件副作用和限制。保持现有公开类名、根包导入和模块路径，没有为对齐主仓目录而改名；分类按表格行为和内部领域职责分别记录。
