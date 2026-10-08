# slowapp-reminders

Reminder prompts for a Slowboard dashboard, as a [slowapp](https://github.com/mike-anderson/slowapp-base) on the slim base.
The `render` job writes the prompt instances due before the next render (`prompts.json`). Each one
carries its four answers' rest cards (`own.tap` yes, `spare.tap` not yet, `own.hold` snooze,
`spare.hold` skip). slowbd overlays them on a slot's peek strip.

This is P4a step one: only the render job lives here. The legacy page still edits reminders and
applies answers to `reminders.json`. The hub mounts that file read-only at
`/imports/legacy/reminders.json` through `host_imports` in `slowbhub.toml`. When the page moves in
(P4b), the file moves to `/data/reminders.json`, which is already the fallback.

- `tools/reminders.py` and `renderer/cards.py` are the host code, unchanged.
- `reminders_app/` is the kit wrapper.
- `epd/` holds just the layout and pixel packing.
- `fixtures/two-due` renders three prompts at 10:00 on 6 Oct 2026. The output was checked
  byte-for-byte against the host render.
