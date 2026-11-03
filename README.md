# Blender Rigging Workflow

Open-source development baseline for a Blender rigging add-on and its companion agent skill.

**The basic feature areas are present; detailed deformation quality has not been accepted.** This is a development snapshot, not a finished auto-rigging system or a promise that every component already shares one UI. Existing components cover bone/control operations, weight editing, expression Shape Keys and hair-bone physics. Test animation is a rig-inspection aid, not an animation-authoring product.

Release **0.1.0-dev1** · base add-on **0.22.0** · GPL-3.0-or-later · runtime baseline: Blender **5.2.1**, Windows.

## Contents

- `work/hrig-p2-v028/addon/humanoid_rigging_probe`: the existing installable base add-on, including inspection, control, weight and hair helpers.
- Later `work/*/product` folders: development components for bone structure edits, local weight tools, templates, face Shape Key editing, local proportion studies and component coordination. Folder versions preserve source provenance, not separate products to install together.
- `hrig_paths.py`: opt-in development import setup and package-relative code paths.
- `skills/blender-rigging`: the single portable companion skill.
- `docs`: setup, actual support boundaries, validation and the next-stage task outline.
- `SOURCE_MAP.json`: selected original source and packaging changes. No private audit logs, character files, textures or credentials are included.

## Install the base add-on

Download `humanoid-rigging-probe-0.22.0.zip` from Releases. In Blender use **Preferences → Add-ons → Install from Disk**, then enable **Humanoid Rigging Probe**. The panel is in **3D View → Sidebar → P2 Rig Probe**. This installs the original base panel; it does not automatically initialize later component sessions or supply a character.

For development, clone this repository, then in Blender's Python environment:

```python
import sys
sys.path.insert(0, '/absolute/path/to/blender-rigging-workflow')
from hrig_paths import configure
configure()
from weight_workflow import contract
from rest_workspace import session
```

Use your own authorized character and explicit source/profile/output parameters. Character-specific face adapters are preserved as development code, not universal face-generation tools. See [setup and component entry points](docs/SETUP.md).

The separate [blenderctl](https://github.com/tsist/blenderctl) repository provides background jobs and the existing managed `extension.run` entry. No new universal public rigging command is claimed. Source-bound receipts from the original workstation are not reusable with this distribution.

## Capabilities and limits

Bone structure edits, existing control operations, smoothing/mirroring/normalization/influence limits, neighbor-based weight filling, explicit templates and expression editing have prior project evidence. Neighbor filling is not whole-character automatic initial skinning. Hair physics has finite character/input evidence; visible hair-mesh repair is separate. Garment fitting and local proportion experiments remain development studies.

Observe large-angle test cycles such as splits, arm bends and squats, including intermediate poses. Expression tests are designed for each character. Functional response and artistic/deformation quality are reported separately. See [validation](docs/VALIDATION.md).

The planned next stage studies selected major capabilities from Auto-Rig Pro, auto-rigpro-ai, Rigify, wiggle-2-rtx-version, an Aon bone-panel script and voxel skinning. Detailed requirements will be collected with grilling when that task is formally started. **None of those third-party add-ons is bundled or claimed implemented here.** See [development outline](docs/DEVELOPMENT.md) and [third-party notices](THIRD_PARTY_NOTICES.md).
