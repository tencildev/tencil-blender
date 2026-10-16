# Tencil for Blender (experiment)

Point at an object in Blender, say what should change, and your coding agent does it
in the open scene. It's [Tencil](https://github.com/tencildev/tencil)'s pin-and-prompt
flow, with Blender as the client. Tencil itself knows nothing about Blender: the add-on
talks to the normal Tencil daemon, and the agent changes the scene through
[Blender Lab's MCP server](https://www.blender.org/lab/mcp-server/).

```
Blender ── Tencil add-on ──ws──▶ tencil daemon ──▶ agent (T3 Code, Claude Code, …)
   ▲                                                    │
   └──── Blender MCP add-on ◀──tcp── blender-mcp ◀─stdio─┘
```

## Try it

1. Run the daemon: `tencil start`.
2. Install this add-on: `blender --command extension build --source-dir tencil` makes a zip;
   drag it into Blender, or for development symlink `tencil/` into
   `~/Library/Application Support/Blender/<version>/extensions/user_default/`.
3. Install Blender Lab's **MCP Server** add-on (Blender 5.1+) from the
   `https://lab.blender.org/` extensions repository and start its server.
4. Save your `.blend` inside the project folder (a git repo, or any folder), open the
   **Tencil** tab in the 3D view's sidebar (N) and click **Add Blender MCP to .mcp.json**,
   so the agent gets the Blender tools (needs [uv](https://docs.astral.sh/uv/)).

Then:

- **Alt+T** (or **Pick with Tencil**) picks: hover outlines the object under the cursor,
  click asks about it, Shift-click picks several and Enter asks about all of them.
  Cameras, lights and empties are hit near their origin.
- Or select objects as usual and use **Object ▸ Ask Tencil** or the right-click menu.

Pins show up as numbers on the objects and in the sidebar, with the agent's replies,
Reply / Stop / Resolve / Open in agent / Delete.

## How it works

- `ws.py` is a standard-library WebSocket client (extensions can't pip-install);
  `client.py` runs it on a thread and a Blender timer hands messages to the main thread.
- The page URL is `blender://` plus the file's path, and the project is the git repository
  the file is in (or its folder); the add-on sends `project.select` with it. An unsaved
  file says `hello { scratch: true }` and the agent works in Tencil's scratch folder.
- Tencil's wire format describes DOM elements, so the object goes into those fields
  (`context.py`): the data path (`bpy.data.objects["Cube"]`) as the selector, and a
  description (transforms, modifiers, materials, picked point, instructions to use the
  Blender MCP tools) where the HTML would go. A viewport screenshot is attached.

## Limits (it's an experiment)

- The daemon's prompt still opens with "an element of their running app… locate the
  code"; the description corrects it. A generic, app-described context kind in Tencil's
  protocol would fix it.
- The daemon remembers projects per page origin, and every `blender://` URL has the
  same one, so the add-on corrects the project on every connect.
- Pins only know their object by name: renaming the object detaches the pin.
- Where on the object a pin was clicked lives in memory; after a restart pins sit on
  top of their object.
- Blender's MCP server runs agent-written Python with no safeguards. Work on files you
  can afford to lose, or in a VM.

## Tests

`tests/integration.sh` runs the add-on headless against a real daemon from a Tencil
checkout (`TENCIL_REPO`, default `../tencil`, built) and its fake T3 Code: connect,
project resolution, a pin on the default cube, the agent's reply, reconnect, resolve.
