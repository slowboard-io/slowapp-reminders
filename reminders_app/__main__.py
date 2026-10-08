"""The reminders slowapp's render job: the prompt instances due before the next render, each with
its four answers' rest cards (tools/reminders.py `render`, unchanged from the host version).

reminders.json is still owned by the legacy page (it edits reminders and applies answers), so the
hub mounts it read-only at /imports/legacy/reminders.json (`host_imports`, P4 transitional). In
fixture mode, and once the page moves into this app, it is /data/reminders.json. A missing file
means no reminders: an empty prompts deck.
"""
import os
from pathlib import Path

import slowapp_kit as kit
from tools import reminders


def source(ctx):
    legacy = Path(os.environ.get("SLOWAPP_IMPORTS", "/imports")) / "legacy" / "reminders.json"
    if not ctx.fixture and legacy.is_file():
        return legacy
    return ctx.data / "reminders.json"


@kit.job("render")
def render(ctx):
    path = source(ctx)
    doc = reminders.render(str(ctx.out), now=reminders.to_local(ctx.for_), path=str(path),
                           note=lambda m: kit.log(m))
    kit.log(f"reminders: {len(doc['prompts'])} prompt instance(s) at {doc['surface']} from {path}")


kit.main()
