# Tencil for Blender setup

Paste everything below the line into your coding agent (Claude Code, T3 Code, Codex…)
from the folder your `.blend` files are in (or will be in). Quit Blender first: the
setup changes its preferences.

---

Set up Tencil for Blender on this machine and in this folder. Tencil for Blender
(github.com/tencildev/tencil-blender) is a Blender add-on: I point at an object in the
3D view, type what should change, and Tencil's daemon starts a Claude Code session in
this folder that changes the open scene through Blender Lab's MCP server.

Do the following, and stop and tell me if a step fails instead of working around it.
Ask before installing anything that isn't listed here.

1. **Check what's needed.** Find Blender (`/Applications/Blender.app/Contents/MacOS/Blender`
   on macOS, otherwise `blender` on the PATH) and check `blender --version` is 5.1 or
   newer. Check that Node 22+, `uvx` (from uv) and `claude` (Claude Code, logged in) are
   installed. Tell me what's missing and how to install it, and stop there if anything is.
   Check Blender isn't running (`pgrep -x Blender`); if it is, ask me to quit it.

2. **Start Tencil.** Ask me whether Tencil should start automatically when I log in
   (recommended). If yes, run `npx --yes tencil@latest setup --yes`; if not,
   `npx --yes tencil@latest start`.

3. **Install the add-on.** Download the zip from the latest release into a new temporary
   folder: `gh release download --repo tencildev/tencil-blender --pattern 'tencil-*.zip'`
   (or from https://github.com/tencildev/tencil-blender/releases/latest). Then
   `blender --command extension install-file -r user_default -e <the zip>`.

4. **Install Blender Lab's MCP add-on.** If `blender --command extension list` has no
   `blender_lab` repository, add it with
   `blender --command extension repo-add blender_lab --name "Blender Lab" --url https://lab.blender.org/`.
   Then `blender --command extension install -s -e mcp`.

5. **Let it start.** Blender Lab's add-on only opens its local socket with online access
   on, and should start on its own. Run:

   ```sh
   blender -b --python-expr "import bpy; p = bpy.context.preferences; p.system.use_online_access = True; a = p.addons.get('bl_ext.blender_lab.mcp'); a and setattr(a.preferences, 'use_autostart', True); bpy.ops.wm.save_userpref()"
   ```

   Then check both are on with the same kind of command (print the two values).

6. **Connect this folder.** Add Blender's MCP server to `.mcp.json` here (merge with an
   existing file, don't replace it):

   ```json
   { "mcpServers": { "blender": { "command": "uvx", "args": ["--from", "git+https://projects.blender.org/lab/blender_mcp.git#subdirectory=mcp", "blender-mcp"] } } }
   ```

   and approve it for Claude Code by adding `"blender"` to `enabledMcpjsonServers` in
   `.claude/settings.json` (merge too). If this folder isn't a git repository, ask me
   whether to run `git init`: Tencil finds the project from the git root.

7. **Tell me how to try it.** Don't open Blender yourself. Tell me to:
   - open Blender and save the `.blend` in this folder (or open one that's already here),
   - open the **Tencil** tab in the 3D view's sidebar (N): it should show this folder's
     name and **Claude Code**. If it says the agent can't use Blender's tools, click
     **Use Claude Code**,
   - press **Alt+T**, click an object and type what should change.

   The **Tencil** button in the 3D view's header (or Alt+Shift+T) lists every thread.

Don't commit any of these changes.

---

## Using it

- **Alt+T**: pick. Click an object to ask about it; Shift-click several, then Enter.
- Or select objects and use **Object ▸ Ask Tencil** or the right-click menu.
- Pins show on the objects; the header's **Tencil** button opens every thread, with
  Reply, Stop, Resolve and Show.
- Blender's MCP server runs the agent's Python with no safeguards: try it on files you
  can afford to lose.

To update later: download the new release's zip and install it the same way.
