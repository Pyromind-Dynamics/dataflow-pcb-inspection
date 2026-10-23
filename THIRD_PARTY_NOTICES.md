# Third-party notices

No platform SDK, credentials, or full raw dataset is included in the extension package.

## Adapted source modules

The following modules are derived from the locally provided Pyromind/OpenHands project. Copyright (c) 2026 OpenHands contributors. Licensed under MIT; see [LICENSE](LICENSE). Only import namespaces were changed; source hashes are recorded below.

- `.agents/skills/data-processing/scripts/preparation/template_synthesis.py` — SHA256 `040fe328bb1c4f7861aa6932e2f7e9345b5eb7b8167e29f75e4097a1eeedb034`
- `.agents/skills/data-processing/scripts/preparation/reference_synthesis.py` — SHA256 `04567ac8df5e20ac50ac5f8515401bf4c003bd56b29eae682cfc7c1bf5789486`

## Adapted domain tests

- `tests/tools/data_preparation/test_template_synthesis.py` — SHA256 `555950a37d54fe8355705b270944cd315308ea336971edf94c2090b3cad7eac2`; imports and test harness adapted, product execution cases excluded.
- `tests/tools/data_preparation/test_reference_synthesis.py` — SHA256 `704bf97e48eed29bff7d5663502eca2a5ef118698d5160d8213ea6c1ad62b3cb`; imports and test harness adapted, product execution cases excluded.

## DeepPCB demo image

`docs/assets/pcb-before-after.png` contains a rendered PCB sample image and its existing annotations from the public [DeepPCB dataset](https://github.com/tangsanli5201/DeepPCB), described in [Online PCB Defect Detector On A New PCB Defect Dataset](https://arxiv.org/abs/1902.06197). The upstream repository's notification limits dataset use to research purposes. The MIT license in that repository applies to its code; it is not presented here as a license for the dataset. Keep this demo asset within the dataset's applicable terms and review the upstream notice before other use.
