# dataflow-pcb-inspection

独立维护的 DataFlow PCB 扩展，当前实现版本为 **0.1.2**。它可以整理已有缺陷框并转换为 Label Studio 文件；也提供可选的多图模型预标注、人工复核和标注模板合成功能。

安装后通过 `import dataflow_pcb_inspection` 注册算子和 Prompt，与 DataFlow 的 Storage、Serving、Pipeline 组合使用。无需把源码复制进 DataFlow 主仓。

本文展示的主流程是：**已有像素坐标框 → 标签核对 → Label Studio 文件**。模型预标注和模板合成是独立的可选流程。

## WebUI 运行与处理前后对比

已通过官方 DataFlow-WebUI 的 **Save & Run → Ray** 执行标签核对与 Label Studio 导出，未添加自定义界面或接口。示例处理 2 条已有标注、共 17 个框，转换后的百分比坐标还原后与输入像素框一致；此流程不调用模型，也不代表模型检测结果。截图中的本机文件路径已遮挡。

![官方 WebUI 中实际执行 PCB 扩展](docs/assets/pcb-webui.png)

下面的对比图直接列出实际输入的像素框和输出的 Label Studio 百分比字段；原图也保留，并显示转换前后的框，编号对应同一条标注。

![PCB 标签转换前后对比](docs/assets/pcb-before-after.png)

对比图中的 PCB 图像来自公开 DeepPCB 样本；来源和数据集使用范围见 [第三方来源说明](THIRD_PARTY_NOTICES.md)。WebUI 加载方式是在后端安装本包、将 `dataflow_pcb_inspection` 加入 `_DATAFLOW_EXTENSIONS` 并重启；图片等外部文件须可被后端访问。多图预标注需要另行配置兼容的 DataFlow Serving，本次 WebUI 示例没有执行模型调用。

## 算子契约与字段安全

每个公开算子的 `get_desc("zh")` / `get_desc("en")` 包含初始化参数、实际默认字段、结果结构和错误行为。`run` 新增结果列并保留输入行数、顺序和源列；**输出列必须尚不存在**，已有同名列时在处理前报错，请换一个 `output_key`。旧代码若依赖覆盖列，需要显式改用新列。

`Pipeline.compile()` 检查表格字段依赖；来源字典的内部字段、文件是否存在和媒体内容在运行时检查。编译成功不代表这些外部资源已经通过校验。

## 安装

在扩展仓库目录内使用 Python 3.10–3.12 安装：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install .
```

## 已有标注转换：不调用模型

安装后运行包内人工构造的已标注图片示例：

```bash
python -m dataflow_pcb_inspection.pipelines.labels_demo --output artifacts/labels-demo
```

使用新的输出目录。示例通过真实 DataFlow Pipeline 执行“已有像素标注 → 标签核对 → Label Studio 任务”，输出 `label_studio_tasks.json`、`label_config.xml`、分步 JSONL 和 `summary.json`。预期 1 条记录、1 个框，`relation=keep`、`model_called=false`。它展示格式转换，不生成新标注，也不模拟真人审核。接入自己的已有标注时，按 [数据契约](docs/CONTRACT.md) 准备 sample、prediction 和版本绑定，并复用 `LabelConversionPipeline`。

## 本地离线示例与开发验证

离线示例使用包内示范素材和固定响应，不调用模型或需要 API key；输出中的 `simulated_review.json` 明确标记为模拟审核，不代表真人已复核。输出目录必须尚不存在。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m dataflow_pcb_inspection.pipelines.demo --output artifacts/my-demo
python -m pytest -q
```

依赖固定 `open-dataflow==1.0.11`。本机验证结果和未覆盖的平台见 [验证说明](docs/VALIDATION.md)；`requirements-macos-py312.txt` 是 macOS arm64 本地环境锁定文件，不用于 Linux。

## 输入与示例产物

预标注 Pipeline 的输入是 JSONL，每行含 `sample`。例如：

```json
{"sample":{"id":"pcb-001","inspection_rules":"按提供的缺陷类别检查待检图，输出原图像素坐标。","images":[{"id":"target","role":"inspection","path":"board.png"},{"id":"reference","role":"reference","path":"reference.png"}]}}
```

图片路径相对调用时指定的 `image_root`，图片 ID 在同一样本中唯一。输出框为对应待检图的原图像素 `bbox_xyxy`，参考图用于对照。详细 schema、类别配置及约束见 [数据契约](docs/CONTRACT.md)。

上面的离线示例会产生：

| 文件 | 用途 |
|---|---|
| `steps/pcb_step1.jsonl` | 保留源列的预测与逐条失败信息 |
| `label_studio_tasks.json`、`label_config.xml` | Label Studio 待复核任务及标签配置 |
| `simulated_review.json` | 格式往返示范，明确标记模拟审核 |
| `synthesis/` | 合成候选及关联图片、掩码等产物 |
| `summary.json` | 输入数、待复核数、失败数及示例状态 |

这些路径相对 `--output` 目录。正常图片预测也保持待复核状态；失败记录不会伪装成正常标签。

扩展安装后通过 `import dataflow_pcb_inspection` 注册算子和 Prompt。模型预标注算子要求 Serving 实现 `generate_from_input_multi_images`；仅继承 `LLMServingABC` 不足以证明兼容。标签导入导出在本地处理文件，不调用 Label Studio 在线 API。

| 算子 | 作用 |
|---|---|
| PCBMultiImageAnnotationGenerator | 多图预测与输入/响应校验 |
| PCBLabelReconciler | 核对原标签和模型建议，保留冲突 |
| PCBReviewExporter / PCBReviewDecisionMerger | 待复核文件与绑定版本的人工决策合并 |
| PCBLabelStudioExporter / PCBLabelStudioImporter | 本地文件转换，不调用 Label Studio API |
| PCBTemplateSynthesisGenerator | 真实标注模板几何合成与产物来源记录 |

所有算子通过 `run(storage, input_key=..., output_key=...)` 接入，保留输入列。具体构造参数与字段见 [CONTRACT.md](docs/CONTRACT.md)。预标注默认 isolate，把异常记录为 failed；复核和文件转换遇到契约不匹配时抛错。处理复核子集时应先选出成功的 needs_review 记录。

## 范围与维护

- 支持本地 JPEG/PNG、多张同角色图片和原图像素 bbox，不依赖 Pyromind/Agent 服务。
- 模板合成基于已有标注做几何变换，生成结果需人工检查；模型预标注结果也保持待复核状态，不自动成为训练标签。
- 首期 Label Studio 支持未旋转矩形、判定单选和说明文本，不支持复杂形状或在线服务集成。
- 当前没有通用续跑、失败补跑、HTTP API、TS SDK 或复核界面。
- 项目代码采用 MIT 许可证，第三方代码及演示素材的来源和限制见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
