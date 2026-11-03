# Setup and component entry points

Install Blender separately. The packaging smoke is for Blender 5.2.1; other runtimes and detailed character quality are not certified by import or registration checks.

The base add-on ZIP contains one `humanoid_rigging_probe` directory. Its original runtime version is retained. For source development call `hrig_paths.configure()` explicitly before importing later components. This sets import paths; it does not register every panel, initialize a character session, edit preferences or alter a scene.

| Area | Source entry |
|---|---|
| Base add-on | `work/hrig-p2-v028/addon/humanoid_rigging_probe` |
| Existing control core | `work/hrig-p2-v065/product/control_core`, `work/hrig-p2-v068/product/native_controls` |
| Full-body semantic controls and fixed test helper | `work/hrig-p2-v094/product/fullbody_controls`, `control_animation` |
| Bone structure edits | `work/hrig-rest-v116/product/rest_workspace/session.py` |
| Local weight tools | `work/hrig-d-workspace-v122/product/weight_workflow/session.py` |
| Component ownership | `work/hrig-d-workspace-v122/product/d_workspace` |
| Templates | `work/hrig-p2-v068/product/template_bundle`, `weight_template`, `control_transport` |
| Face Shape Keys | `work/hrig-d3-v121/product/facial_key_edit`, `facial_controls`; `work/hrig-d3-v124/product/facial_key_ui` |
| Proportion study | `work/hrig-proportion-v127/product/local_proportion` |
| Garment studies | `work/hrig-d4-v114/product/garment_adapt` |

Read each component's initializer and current contract before use. Most later components require an explicit source identity, profile, mesh/rig names and owned output directory. They are developer APIs, not a combined automatic setup wizard. Missing profiles or unsupported rigs must be reported rather than bypassed by modifying the character.

`HRIG_DATA_ROOT` optionally locates user-provided data for old source-relative examples. Private assets, historical requests, receipts and their outputs are deliberately absent. Supply fresh inputs and regenerate source-bound metadata through the actual tool; do not edit old hashes to make a request appear validated.

The old `rigctl.py` front end is retained as developer source. Set `BLENDERCTL_ENTRY` to the separate managed `blenderctl.ps1` wrapper when using it. Required runtime capabilities depend on the selected request; an older CLI snapshot does not automatically support every newer component. The original workstation layout is not an installation requirement or a claim of a new universal command.
