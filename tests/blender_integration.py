"""Runs inside `blender -b`: the add-on against a real Tencil daemon and a fake agent.

Started by tests/integration.sh, which saves a .blend into a temporary git repo
and passes it on the command line.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bpy  # noqa: E402

import tencil  # noqa: E402
from tencil import context as tencil_context  # noqa: E402
from tencil.state import session  # noqa: E402


def wait(condition, what, seconds=20.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        session.tick()
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {what} (connected={session.connected}, error={session.error})")


tencil.register()
try:
    expected_root = os.path.realpath(os.environ["EXPECTED_ROOT"])
    session.start(int(os.environ["TENCIL_PORT"]))
    assert session.page_url.startswith("blender:///"), session.page_url
    assert os.path.realpath(session.root) == expected_root, (session.root, expected_root)

    wait(lambda: session.ready, "ready")
    assert os.path.realpath(session.project["root"]) == expected_root, session.project
    print("ready:", session.project["name"], "provider:", session.provider["label"], session.provider["state"])
    wait(lambda: (session.provider or {}).get("state") == "ready", "provider ready")

    cube = bpy.data.objects["Cube"]
    element, anchor = tencil_context.build([cube], None, None, None, None)
    assert anchor["selector"] == 'bpy.data.objects["Cube"]', anchor
    assert "Blender MCP" in element["html"] and "(mesh)" in element["html"], element["html"]
    assert tencil_context.object_from_path(anchor["selector"]) == cube

    thread_id = session.create_thread("Make the cube red", element, anchor, [], (0.0, 0.0, 1.0))
    assert thread_id

    def replied():
        thread = session.threads.get(thread_id)
        return bool(
            thread
            and thread["status"] == "idle"
            and any(m["role"] == "assistant" and m["text"] for m in thread["messages"])
        )

    wait(replied, "the agent's reply")
    thread = session.threads[thread_id]
    assert thread["title"].startswith("Cube"), thread["title"]
    assert [t["id"] for t in session.file_threads()] == [thread_id]
    print("thread:", thread["title"], "->", thread["messages"][-1]["text"].splitlines()[0])

    # A reconnect says hello again and gets the pin back.
    session.reconnect()
    wait(lambda: session.ready and thread_id in session.threads, "pins after reconnect")

    assert session.send({"type": "thread.resolve", "threadId": thread_id, "resolved": True})
    wait(lambda: session.threads[thread_id].get("resolved"), "resolve")
    assert session.file_threads() == []
    print("OK")
finally:
    tencil.unregister()
