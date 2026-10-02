# Resolve-to-Nuke Shot Exporter

An installable DaVinci Resolve 20.2+ Scripts-menu tool that builds Nuke scripts
and optional isolated per-layer plates from a conformed timeline.

The Rename action requires Resolve 20.2+. Resolve 20.0 and 20.1 can preview
the proposed names and use the exporter, but Blackmagic had not yet exposed
timeline-item renaming through the public scripting API.

## Install

### macOS and Windows — Workflow Integration (recommended)

Copy `ResolveToNukeShotExporter.py` directly into the **Workflow Integration
Plugins** root, then restart Resolve. It will appear under **Workspace →
Workflow Integrations** and open its UI in a separate window.

- macOS: `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins/`
- Windows: `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Workflow Integration Plugins\`

This uses the Python Workflow Integration mechanism documented by Blackmagic;
Resolve injects `resolve`, `project`, `fusion`, and `bmd` into the script before
the UI is created.

Resolve does not necessarily create this directory. Use the included installer
instead of creating/copying it manually:

- macOS: [install_macos.command](install_macos.command) — run it and enter an
  administrator password when prompted.
- Windows: [install_windows.ps1](install_windows.ps1) — run PowerShell as
  Administrator from this folder, then run `./install_windows.ps1`.

### Linux — Scripts menu

Workflow Integration Plugins are not supported by Resolve on Linux. Copy the
same file to **Fusion/Scripts/Utility**, restart Resolve, and run it from
**Workspace → Scripts → Utility**.

The same file runs on macOS, Windows, and Linux. It uses `pathlib` for native
filesystem paths, stores only forward-slash paths inside `.nk` files, and has
no external Python, Qt, or operating-system dependency. When launched from
Resolve's Scripts menu it uses Fusion's built-in Resolve bridge; when invoked
from an external Python interpreter it also supports `RESOLVE_SCRIPT_API`.

Scripts-menu locations:

- macOS: `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/`
- Windows: `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\`
- Linux: `/opt/resolve/Fusion/Scripts/Utility/`

Open a project and timeline before launching the exporter.

## Workflow

1. In **Rename**, choose the tracks to rename (or leave the field blank for
   every video track), enter a template such as `{sequence}_{track}_{shot_index:03}`,
   preview it, then apply the names. Duplicate names are reported as a warning;
   add `{track}` to the template when cross-track names must be unique.
2. In **Export**, choose a project root, version, video tracks, handles, and
   the track that supplies Write timecode. Leave **Tracks** blank to include
   every video track. The chosen shot track is the fallback if the timecode
   track has no overlapping item.
3. Enable **Export isolated source plates** only if Resolve should consolidate
   media. Use **Plate tracks** to choose which selected tracks are rendered;
   leave it blank to render every selected track. Other Nuke Reads use their
   original source media paths.
4. Choose **Source scale** for native source dimensions or **Timeline scale**
   for the timeline raster and Resolve scaling method. Preview paths first;
   collisions block export.
   The `{frame}` token is kept for image sequences and automatically removed
   (with its adjoining `_`, `-`, or `.` separator) for single-file formats
   such as MOV and MXF.
5. Adjust the output templates on **Export** to alter the default Nuke
   Studio-style layouts:

   ```text
   Projects/Nuke/{sequence}/{shot}/{shot}_comp_{version}.nk
   Renders/Nuke/{sequence}/{shot}/{shot}_comp_{version}_{frame}.{ext}
   Video/Footage/Transodes/{sequence}/{track}/{shot}_{track}_{clip}_{version}.{frame}.{ext}
   ```

All output controls and templates are on the **Export** tab. The token
reference remains visible beside both the Rename and Export forms.

Set **Nuke start frame** to the delivery/comp start frame (default `1001`).
Each generated Nuke script uses this as its Root first frame and offsets its
sources with TimeClip nodes; the Root last frame is calculated from the primary
exported source duration.

## Behaviour and limits

- Every edit on the chosen shot track is one shot. Auto selects the lowest
  non-empty video track, so an empty V1 does not prevent export. Every item
  overlapping that range on an included video track becomes a separate named
  Nuke Read and optional plate.
- The script makes an editable Nuke graph: the shot-track Read is the default
  Write input and additional layer Reads are left ready for the compositor.
- Transcoded movie plates use their native Nuke frame range (`1` through the
  rendered duration). Original movie Reads expose their full available source
  range. In both cases TimeClip holds the Resolve source in/out trim and places
  it at the Nuke shot range; image-sequence plates retain their rendered frame
  numbering.
- For MOV Write targets, choose a Nuke codec (Apple ProRes, H.264, DNxHR, or
  Uncompressed). The generated Write node stores this as Nuke's MOV codec
  setting.
- `AddTimeCode` is created directly before Write from the selected source
  track's source timecode. It falls back to the shot track and is omitted if
  no source timecode exists.
- Resolve's current colour pipeline is used for plate renders. Sticky notes
  retain the Resolve colour context; v1 deliberately does not guess an OCIO
  mapping in Nuke.
- The sidecar option produces a JSON production manifest. Container/image
  metadata is retained only when the selected Resolve codec supports it.
- Existing scripts, manifest files, plate sequences, and Write targets block
  export. Pick a different folder or a higher manual version.
- Presets are stored per-user, not inside the Resolve installation: Application
  Support on macOS, `%APPDATA%` on Windows, and `$XDG_CONFIG_HOME` (or
  `~/.config`) on Linux.
- The supported format/codec list is Resolve-installation dependent. Verify
  the exact format/codec names shown by Resolve before launching a render.

## Testing outside Resolve

Run:

```sh
python3 -m unittest discover -s tests -v
```

The tests cover token expansion, range/handle clamping, no-overwrite
preflight, primary-track timecode fallback, missing-timecode omission, Nuke generation,
and optional manifests. They do not require Resolve or Nuke.
