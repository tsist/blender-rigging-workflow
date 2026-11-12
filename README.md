# Blender Rigging Workflow

[简体中文](README.zh-CN.md)

**0.40.0-r3** — a reviewed Windows / Blender **5.2.1 LTS** runtime snapshot and its single companion agent skill. GPL-3.0-or-later for project code; bundled third-party files retain their own licenses.

The current add-on unifies 38 business panels under **3D View → N → 角色工作台** (Character Workbench), with character context, freely selectable production stages, and Daily / Advanced / Developer levels. Existing supported rigs, weights, Shape Keys, Actions, slots and NLA are preserved within the documented operations.

## Install

Download [rigging_workflow-0.40.0-local-r3.zip](https://github.com/tsist/blender-rigging-workflow/releases/tag/v0.40.0-r3), install it from **Preferences → Add-ons → Install from Disk**, and enable it. The repository source ZIP is not the add-on installer.

The [release](https://github.com/tsist/blender-rigging-workflow/releases/tag/v0.40.0-r3) also includes the portable `blender-rigging` skill, full source and SHA256SUMS. Installation is manual; this repository does not modify an existing Blender installation.

## What is available

- Preparation, positioning, a fixed C94 template fitting/generation workflow, volumetric skinning, local weight tools and native rig maintenance.
- Shared bone/Shape Key face authoring and daily controls, explicit existing-Key mapping and recoverable revisions.
- Chest/hip elasticity and native Cloth-based hair/skirt residual clips, preview, stop, reset and bake.
- Supported animation retargeting, source/profile configuration, background queues, collection, preview and cancellation.
- A manually executed portable Text workbench for the documented daily runtime.

**The private C94 rig template, positioning atlas, target characters and sample motions are not distributed.** New-character C94 generation requires independently supplied authorized inputs. This is not a universal auto-rig for arbitrary control graphs. See [setup](docs/SETUP.md), [workflow guide](rigging_workflow/USER-GUIDE.md), [AI calls](rigging_workflow/AI-CALLS.md) and [support matrix](rigging_workflow/SUPPORT-MATRIX.md).

## Review and boundaries

The native Windows review exercised a real 53,598-point binding input, six existing-project warm/cold paths, face and short physical clips, real background queue success/failure/cancellation, visible GUI interaction and isolated native installation. [Validation](docs/VALIDATION.md) distinguishes technical behavior, visual quality and untested scope. These are named sample results, not all-input or all-branch coverage.

This release preserves the reviewed runtime payload **byte for byte**. A few original path defaults and provenance records describe the test workstation: configure your own CLI / MMD Tools / OpenCC paths in the existing panels. Private audit contents are not uploaded. Package-internal README/notices retain the state of the original local snapshot; this repository's current publication status and limitations are recorded in [PUBLICATION.md](docs/PUBLICATION.md).

The release remains a **prerelease**: runtime evidence covers Windows / Blender 5.2.1, not cross-platform production qualification. Source packaging CI on Linux does not certify Blender runtime support there.

## Repository layout

- `rigging_workflow/`: current 183-file runtime, 165 Python files; install this product.
- `skills/blender-rigging/`: the only current portable companion skill.
- `scripts/`: reproducible packaging and optional factory-startup registration smoke.
- `work/`, `tools/`, `hrig_paths.py` and root `SOURCE_MAP.json`: preserved historical 0.1.0-dev1 SDK sources. Do not combine those legacy components with the current runtime.

[License](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md) · [Changes](CHANGELOG.md) · [Development scope](docs/DEVELOPMENT.md)

The separate [blenderctl](https://github.com/tsist/blenderctl) provides managed `extension.run`; no invented universal rigging command is claimed. Historical receipts and saved RAM session tokens do not grant new submission or commit rights.
