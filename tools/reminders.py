"""Reminders: a question that comes back on a schedule until it's answered. Python stdlib only.

Kept in /var/lib/epaper/reminders.json and edited on the page at /reminders. When one is due,
slowbd's deck (from the prompt instances `render` writes, below) puts its question in the peek strip
of its panel, and four answers go on that panel's key and the spare (●):

  own key tap    Yes        done for this occurrence; the rest card says when it's next due
  ● tap          Not yet    ask again at the reminder's retry time (or in 3 h if that has passed)
  own key hold   Snooze     ask again in an hour
  ● hold         Skip       skip this occurrence

Schedules: daily, weekly (a weekday), monthly (a day of the month; 31 means the month's last day)
or once (a date), each at a time of day. Every answer is logged, from the keys or from the page.

With slowbhub (P2, docs/SLOWBOARD.md §10), the prompts go to the device as prompt instances:

  python3 -m tools.reminders render --out DIR [--now ISO]

writes DIR/prompts.json and the four answers' rest cards (needs Pillow; reminders.json is only
read), and the web service applies the answers slowbhub hands back (Reminders.apply_event).
State is in $EPAPER_STATE (default /var/lib/epaper).
"""
import copy
import json
import os
import threading
import uuid
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

STATE_DIR = os.environ.get("EPAPER_STATE", "/var/lib/epaper")
FILE = os.path.join(STATE_DIR, "reminders.json")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
ANSWER_NAMES = {"yes": "Yes", "not_yet": "Not yet", "snooze": "Snooze 1 h", "skip": "Skip"}
LOG_KEEP = 60
APPLIED_KEEP = 500           # slowbhub event ids (and answered prompt instances) remembered


def _zone():
    """The local zone: $TZ if it names one (slowbhub sets it to prefs.tz), else the system's."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz:
        try:
            return ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return None


def to_local(dt):
    """An aware datetime as the naive local time this module works in."""
    if dt.tzinfo is None:
        return dt
    z = _zone()
    return (dt.astimezone(z) if z else dt.astimezone()).replace(tzinfo=None)


def to_aware(dt):
    """A naive local time with its offset."""
    if dt.tzinfo is not None:
        return dt
    z = _zone()
    return dt.replace(tzinfo=z) if z else dt.astimezone()


def event_time(ev):
    """When an emit event happened: its `at` if the device's clock was synced, else when the hub
    got it (`hub_at`, ms), else now. Naive local."""
    if ev.get("at") and ev.get("at_synced", True):
        try:
            return to_local(datetime.fromisoformat(ev["at"]))
        except ValueError:
            pass
    if isinstance(ev.get("hub_at"), (int, float)):
        return to_local(datetime.fromtimestamp(ev["hub_at"] / 1000, timezone.utc))
    return datetime.now()


def _minute(s):
    try:
        return to_local(datetime.fromisoformat(s)).replace(second=0, microsecond=0)
    except (TypeError, ValueError):
        return None


def _answered_since(item, occurrence, ask_from):
    """Was this occurrence answered (from the page, say) after the prompt asking it went up?"""
    last = (item.get("state") or {}).get("last") or {}
    occ, at, start = _minute(last.get("occurrence")), _minute(last.get("at")), _minute(ask_from)
    return occ is not None and occ == _minute(occurrence) and at is not None and start is not None and at >= start


def _settled(item, occ):
    """Is occurrence `occ` (naive local) already finished: answered yes / skip, or a newer
    occurrence answered since? A late answer to it (from a device that was away from the hub and
    kept showing a later piece of the same ask) then changes nothing."""
    st = item.get("state") or {}
    done, last = _minute(st.get("done_for")), _minute((st.get("last") or {}).get("occurrence"))
    return (done is not None and done >= occ) or (last is not None and last > occ)


def _forced_at(st, now):
    """The occurrence "ask now" stands for when nothing is scheduled yet: the minute the ask was
    made (fixed, so the prompt instance id is the same on every render). An old boolean flag
    falls back to `since`, then to now."""
    for v in (st.get("force"), st.get("since")):
        if isinstance(v, str):
            m = _minute(v)
            if m is not None:
                return m
    return now


def _hm(s):
    h, m = (int(v) for v in s.split(":"))
    return h, m


def _at(day, hm):
    h, m = _hm(hm)
    return datetime(day.year, day.month, day.day, h, m)


def _fmt(dt, now):
    if dt.date() == now.date():
        return dt.strftime("today %H:%M")
    if dt.date() == (now + timedelta(days=1)).date():
        return dt.strftime("tomorrow %H:%M")
    return dt.strftime("%a %-d %b, %H:%M")


def occurrences(item, start, end):
    """Scheduled times in [start, end], oldest first."""
    rep, t = item.get("repeat", "monthly"), item.get("time", "09:00")
    out = []
    if rep == "once":
        try:
            dt = _at(datetime.fromisoformat(item["date"]), t)
        except (KeyError, ValueError):
            return []
        return [dt] if start <= dt <= end else []
    if rep == "monthly":
        y, m = start.year, start.month
        while True:
            dt = _at(datetime(y, m, min(int(item.get("day", 1)), monthrange(y, m)[1])), t)
            if dt > end:
                break
            if dt >= start:
                out.append(dt)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return out
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        if rep == "daily" or (rep == "weekly" and day.weekday() == int(item.get("weekday", 0))):
            dt = _at(day, t)
            if start <= dt <= end:
                out.append(dt)
        day += timedelta(days=1)
    return out


def _span(item):
    return {"daily": 2, "weekly": 8}.get(item.get("repeat"), 400)    # days to look through


def latest(item, now):
    past = occurrences(item, now - timedelta(days=_span(item)), now)
    return past[-1] if past else None


def upcoming(item, after):
    nxt = occurrences(item, after + timedelta(seconds=1), after + timedelta(days=_span(item)))
    return nxt[0] if nxt else None


def describe(item):
    rep, t = item.get("repeat", "monthly"), item.get("time", "09:00")
    if rep == "daily":
        return f"every day at {t}"
    if rep == "weekly":
        return f"every {WEEKDAYS[int(item.get('weekday', 0))]} at {t}"
    if rep == "once":
        return f"once, {item.get('date', '?')} at {t}"
    d = int(item.get("day", 1))
    return f"monthly on the {'last day' if d >= 31 else ordinal(d)} at {t}"


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


class Reminders:
    def __init__(self, path=FILE, note=print):
        self.path = path
        self.note = note
        self.lock = threading.Lock()
        try:
            with open(path) as f:
                self.data = json.load(f)
        except (FileNotFoundError, ValueError):
            self.data = {"items": []}

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path + ".tmp", "w") as f:
            json.dump(self.data, f, indent=1)
        os.replace(self.path + ".tmp", self.path)

    def get(self, rid):
        return next((i for i in self.data["items"] if i["id"] == rid), None)

    # ---------------------------------------------------------------- what's due
    def open_occurrence(self, item, now):
        """The occurrence this reminder is asking about right now, or None."""
        if not item.get("enabled", True):
            return None
        st = item.setdefault("state", {})
        occ = latest(item, now)
        if st.get("force"):
            return occ or _forced_at(st, now)
        if occ is None or st.get("done_for") == occ.isoformat():
            return None
        if st.get("next_ask") and now < datetime.fromisoformat(st["next_ask"]):
            return None
        if st.get("since") and occ < datetime.fromisoformat(st["since"]):
            return None                             # scheduled before the reminder existed
        return occ

    def due(self, now=None):
        now = now or datetime.now()
        with self.lock:
            best = None
            for item in self.data["items"]:
                occ = self.open_occurrence(item, now)
                if occ and (best is None or occ < best[1]):
                    best = (item, occ)
            return dict(best[0]) if best else None

    # ---------------------------------------------------------------- answers
    def answer(self, rid, ans, via="keys", now=None, dry=False):
        """Record an answer; returns the rest card for the panel (title, head, lines, ...).
        dry=True only returns the rest card, recording nothing: render draws the answers ahead.
        The card's words depend only on the day, not the minute, so a pre-rendered one still
        matches when the key is pressed."""
        with self.lock:
            rest, item = self._answer(rid, ans, via, now, dry)
            if rest is None or dry:
                return rest
            self._save()
        self.note(f"reminder “{item['title']}”: {ANSWER_NAMES[ans]} ({via})")
        return rest

    def _answer(self, rid, ans, via, now, dry, occ=None):
        """answer() with the lock held, without saving: (rest card, item) or (None, None).
        occ: the occurrence answered, when the caller knows it (a prompt instance names it)."""
        now = now or datetime.now()
        item = self.get(rid)
        if item is None or ans not in ANSWER_NAMES:
            return None, None
        if dry:
            item = json.loads(json.dumps(item))       # work on a copy
        st = item.setdefault("state", {})
        occ = occ or self.open_occurrence(item, now) or latest(item, now) or now
        st.pop("force", None)
        nxt = upcoming(item, max(occ, now))
        lines, strip = [], ""
        if ans in ("yes", "skip"):
            st["done_for"], st["next_ask"] = occ.isoformat(), None
            if item.get("repeat") == "once":
                item["enabled"] = False
            when = f"Next due {_fmt(nxt, now)}." if nxt and item.get("enabled", True) else "That was the last one."
            head = "Done" if ans == "yes" else "Skipped"
            lines.append(when)
            strip = f"{head} · next {nxt:%a %-d %b}" if nxt and item.get("enabled", True) else head
        elif ans == "not_yet":
            retry = item.get("retry") or "18:00"
            h, m = _hm(retry)
            again = now.replace(hour=h, minute=m, second=0, microsecond=0)
            head = "Not yet"
            if again <= now + timedelta(minutes=30):           # from retry - 30 min on (ask_windows splits there)
                again = now + timedelta(hours=3)
                lines.append("I'll ask again in 3 hours.")
                strip = "Asking again in 3 h"
            else:
                lines.append(f"I'll ask again at {retry}.")
                strip = f"Asking again at {retry}"
            st["next_ask"] = again.isoformat()
        else:
            st["next_ask"] = (now + timedelta(hours=1)).isoformat()
            head = "Snoozed"
            lines.append("Back in an hour.")
            strip = "Snoozed for an hour"
        rest = dict(title=item.get("question") or item["title"], head=head, answer=ans, lines=lines,
                    at=now.strftime("%a %-d %b"), strip=strip, footer=f"{ANSWER_NAMES[ans]} · {via}")
        if not dry:
            st["last"] = dict(answer=ans, at=now.isoformat(timespec="minutes"), occurrence=occ.isoformat(), via=via)
            item.setdefault("log", []).insert(0, st["last"])
            del item["log"][LOG_KEEP:]
        return rest, item

    # ---------------------------------------------------------------- answers from slowbhub (P2)
    def apply_event(self, ev):
        """Apply one `emit` event pulled from slowbhub (GET /admin/v1/events?app=reminders):
        {"id", "at", "at_synced", "hub_at", "data": {"prompt": "rid@occurrence@ask_from", "via",
        "emit": {"kind": "answer", "data": {"answer", "rid", "occurrence"}}}}, with now = the
        event's time (§4.3 H3). Delivery is at least once, so applied event ids are kept in
        reminders.json (the last APPLIED_KEEP) and a repeat does nothing. The first answer per
        prompt instance wins, whether it came from the keys or the page, and an answer to an
        occurrence already settled (yes / skip, or a newer one answered) is ignored: a device away
        from the hub keeps showing the later pieces of an ask it already answered. If saving
        fails nothing is kept (not even the event id) and the error is raised, so the event can be
        pulled again.
        Returns "applied", "duplicate" or "ignored: <why>"."""
        eid = ev.get("id")
        if not eid:
            return "ignored: no event id"
        d = ev.get("data") or {}
        emit = d.get("emit") or {}
        edata = emit.get("data") or {}
        inst = d.get("prompt") or ""
        parts = inst.split("@")
        ans = edata.get("answer")
        via = d.get("via") or "keys"
        item = None
        with self.lock:
            if eid in self.data.get("applied_events", []):
                return "duplicate"
            before = copy.deepcopy(self.data)
            applied = self.data.setdefault("applied_events", [])
            applied.append(eid)
            del applied[:-APPLIED_KEEP]
            why = None
            if emit.get("kind") != "answer":
                why = f"emit kind {emit.get('kind')!r}"
            elif len(parts) != 3:
                why = f"prompt id {inst!r} isn't rid@occurrence@ask_from"
            elif ans not in ANSWER_NAMES:
                why = f"unknown answer {ans!r}"
            elif _minute(parts[1]) is None:
                why = f"prompt id {inst!r} has no occurrence time"
            else:
                rid = edata.get("rid") or parts[0]
                occ = _minute(parts[1])
                answered = self.data.setdefault("answered_prompts", [])
                item = self.get(rid)
                if item is None:
                    why = f"no reminder {rid}"
                elif inst in answered or _answered_since(item, parts[1], parts[2]):
                    why = f"prompt {inst} was already answered"
                elif _settled(item, occ):
                    why = f"the {occ:%Y-%m-%d %H:%M} occurrence was already settled"
                else:
                    self._answer(rid, ans, via, event_time(ev), False, occ=occ)
                    answered.append(inst)
                    del answered[:-APPLIED_KEEP]
            try:
                self._save()
            except BaseException:
                self.data = before                  # not applied: the next pull tries again
                raise
        if why:
            self.note(f"reminder event {eid}: ignored, {why}")
            return f"ignored: {why}"
        self.note(f"reminder “{item['title']}”: {ANSWER_NAMES[ans]} ({via}, via slowbhub)")
        return "applied"

    def ask_now(self, rid):
        with self.lock:
            item = self.get(rid)
            if item:
                item.setdefault("state", {})["force"] = datetime.now().isoformat(timespec="minutes")
                self._save()
        return item is not None

    # ---------------------------------------------------------------- editing
    def save_item(self, body):
        title = (body.get("title") or "").strip()
        if not title:
            raise ValueError("a reminder needs a title")
        rep = body.get("repeat", "monthly")
        if rep not in ("daily", "weekly", "monthly", "once"):
            raise ValueError(f"unknown repeat {rep!r}")
        for k in ("time", "retry"):
            if body.get(k):
                _hm(body[k])
        if rep == "once":
            datetime.fromisoformat(body.get("date") or "")
        with self.lock:
            item = self.get(body.get("id") or "")
            if item is None:
                item = dict(id=uuid.uuid4().hex[:10], state=dict(since=datetime.now().isoformat(timespec="minutes")), log=[])
                self.data["items"].append(item)
            for k in ("title", "question", "repeat", "time", "retry", "date", "panel"):
                if k in body:
                    item[k] = (body[k] or "").strip() if isinstance(body[k], str) else body[k]
            for k in ("day", "weekday"):
                if body.get(k) not in (None, ""):
                    item[k] = int(body[k])
            item["enabled"] = bool(body.get("enabled", True))
            item.setdefault("panel", "right")
            self._save()
            return item["id"]

    def delete(self, rid):
        with self.lock:
            n = len(self.data["items"])
            self.data["items"] = [i for i in self.data["items"] if i["id"] != rid]
            self._save()
            return len(self.data["items"]) < n

    def status(self, prompt_rid=None, now=None):
        now = now or datetime.now()
        out = []
        with self.lock:
            for item in self.data["items"]:
                occ = self.open_occurrence(item, now)
                st = item.get("state", {})
                if occ:
                    state = "asking now" if prompt_rid == item["id"] else "due, waiting for its panel"
                elif not item.get("enabled", True):
                    state = "off"
                elif st.get("next_ask") and datetime.fromisoformat(st["next_ask"]) > now:
                    state = f"asking again {_fmt(datetime.fromisoformat(st['next_ask']), now)}"
                else:
                    nxt = upcoming(item, now)
                    state = f"next {_fmt(nxt, now)}" if nxt else "no more"
                out.append(dict(item, schedule=describe(item), status=state, open=bool(occ)))
        return out


def handle(rem, path, body, prompt_rid=None):
    """POST /api/reminders/<verb> -> (http code, json)."""
    verb = path.rsplit("/", 1)[-1]
    try:
        if verb == "save":
            return 200, dict(ok=True, id=rem.save_item(body))
        if verb == "delete":
            return (200, dict(ok=True)) if rem.delete(body.get("id")) else (404, dict(error="no such reminder"))
        if verb == "ask":
            return (200, dict(ok=True)) if rem.ask_now(body.get("id")) else (404, dict(error="no such reminder"))
        if verb == "answer":
            rest = rem.answer(body.get("id"), body.get("answer"), via="page")
            return (200, dict(ok=True, rest=rest)) if rest else (400, dict(error="unknown reminder or answer"))
    except (ValueError, KeyError) as e:
        return 400, dict(error=str(e))
    return 404, dict(error="not found")


# ---------------------------------------------------------------- prompts for slowbhub (P2)
# python3 -m tools.reminders render --out DIR: the reminders app's `render` job (HostRunner,
# crates/slowbhub/slowbhub.example.toml). It writes DIR/prompts.json (sb_proto::manifest::OutPrompts)
# and DIR/prompts/<instance>/<binding>.png, and reads reminders.json without ever writing it.

BINDINGS = (("own.tap", "own", "tap", "yes"), ("spare.tap", "spare", "tap", "not_yet"),
            ("own.hold", "own", "hold", "snooze"), ("spare.hold", "spare", "hold", "skip"))
MAX_PROMPTS = 8              # sb_proto::limits::MAX_PROMPTS
SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.@:+-")
DEFAULT_SURFACE = (452, 444)


def safe_name(s):
    """slowbhub's ingest rule for names in /out: [A-Za-z0-9_.@:+-], 96 characters at most."""
    return 0 < len(s) <= 96 and not s.startswith(".") and set(s) <= SAFE


def next_render(now):
    """The next scheduled render: the job runs daily at 00:01 (and on demand after any change)."""
    t = now.replace(hour=0, minute=1, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def ask_windows(rem, item, now, horizon):
    """When this reminder asks, from `now` until `horizon`: [(occurrence, from, until)].

    Without new answers the state is fixed, so what open_occurrence() returns only changes at an
    occurrence, at next_ask (not yet / snooze), or at since. The answers' wording changes at
    midnight (the rest card's date, "today" / "tomorrow") and at retry - 30 min (where "not yet"
    goes from "at 18:00" to "in 3 hours"). Each piece between those points is one prompt instance,
    so every binding body stays right for its whole [from, until] (§4.3 app rule). The first piece
    can start before now: an ask already up keeps its instance id across renders."""
    item = json.loads(json.dumps(item))
    start, end = now - timedelta(days=2), horizon + timedelta(days=2)
    retry_h, retry_m = _hm(item.get("retry") or "18:00")
    pts = set(occurrences(item, start, end))
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        pts.add(day)
        pts.add(day.replace(hour=retry_h, minute=retry_m) - timedelta(minutes=30))
        day += timedelta(days=1)
    st = item.get("state") or {}
    for k in ("next_ask", "since"):
        if st.get(k):
            try:
                pts.add(datetime.fromisoformat(st[k]))
            except ValueError:
                pass
    pts = sorted(p for p in pts if start <= p <= end)
    out = []
    for a, b in zip(pts, pts[1:]):
        if b <= now or a >= horizon:
            continue
        occ = rem.open_occurrence(item, a)                # "ask now" with nothing scheduled: _forced_at
        if occ is None:
            continue
        out.append((occ, a, b))
    return out


def prompt_instances(rem, now, horizon=None):
    """Every prompt instance from now until the next scheduled render, earliest first:
    [dict(item, occurrence, from, until, id)]."""
    horizon = horizon or next_render(now)
    out = []
    for item in rem.data.get("items", []):
        for occ, a, b in ask_windows(rem, item, now, horizon):
            iid = f"{item['id']}@{occ:%Y-%m-%dT%H:%M}@{a:%Y-%m-%dT%H:%M}"
            out.append(dict(item=item, occurrence=occ, start=a, until=b, id=iid))
    out.sort(key=lambda p: (p["start"], p["id"]))
    return out


def _save_k4(img, path):
    """A pure four-ink RGB image as a 2-bit indexed PNG (renderer.weather.save_feeds' format)."""
    from PIL import Image
    inks = ((0, 0, 0), (255, 255, 255), (255, 255, 0), (255, 0, 0))
    im = img.convert("RGB")
    stray = {c for _, c in im.getcolors(maxcolors=1 << 24)} - set(inks)
    if stray:
        raise ValueError(f"{path}: {len(stray)} colours are not inks, e.g. {sorted(stray)[:3]}")
    pal = Image.new("P", (1, 1))
    pal.putpalette([v for c in inks for v in c])
    q = im.quantize(palette=pal, dither=Image.Dither.NONE)
    ix = Image.frombytes("P", q.size, q.tobytes())
    ix.putpalette([v for c in inks for v in c])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    ix.save(tmp, format="PNG", bits=2)
    os.replace(tmp, path)


def prompt_surface():
    """(w, h) of the prompts feed from $SLOWAPP_FEEDS (slowbhub's placement), else 452 x 444."""
    try:
        feeds = json.loads(os.environ.get("SLOWAPP_FEEDS") or "[]")
    except ValueError:
        feeds = []
    for f in feeds if isinstance(feeds, list) else []:
        if isinstance(f, dict) and f.get("feed") == "prompts":
            if f.get("inks", "k4") != "k4":
                raise ValueError(f"the prompts feed is {f.get('inks')}: only k4 is drawn")
            return int(f["w"]), int(f["h"])
    return DEFAULT_SURFACE


def _fit(body, w, h, white, body_h):
    """A card drawn BODY_H tall onto the prompts surface (w x h), cropped or padded with white."""
    from PIL import Image
    if body.size == (w, h):
        return body
    canvas = Image.new("RGB", (w, h), white)
    canvas.paste(body.crop((0, 0, w, min(h, body_h))), (0, 0))
    return canvas


def render(out, now=None, path=None, horizon=None, note=lambda m: None):
    """Write out/prompts.json and out/prompts/<instance>/<binding>.png; returns the document."""
    import shutil
    from PIL import Image
    from epd.layout import BODY_H, KEY_OF, ORDER, SPARE
    from renderer import cards

    rem = Reminders(path or os.path.join(os.environ.get("EPAPER_STATE", "/var/lib/epaper"), "reminders.json"), note=note)
    now = to_local(now) if now else datetime.now()
    w, h = prompt_surface()
    shutil.rmtree(os.path.join(out, "prompts"), ignore_errors=True)
    prompts, skipped, later = [], [], None
    for p in prompt_instances(rem, now, horizon):
        item, iid = p["item"], p["id"]
        if not safe_name(iid):
            skipped.append(iid)
            continue
        if len(prompts) == MAX_PROMPTS:
            later = p["start"]                            # render again then (timing.json)
            break
        panel = item.get("panel") if item.get("panel") in ORDER else "right"
        ask = cards.ask_card(w, dict(title=item["title"], question=item.get("question") or item["title"],
                                     at=p["occurrence"].strftime("%H:%M"), footer=describe(item)))
        _save_k4(_fit(ask, w, h, cards.PW, BODY_H), os.path.join(out, f"prompts/{iid}/ask.png"))
        bindings = {}
        for name, who, kind, ans in BINDINGS:
            key = SPARE if who == "spare" else KEY_OF[panel]
            rest = rem.answer(item["id"], ans, via=f"{key} key {kind}", now=p["start"], dry=True)
            body = _fit(cards.rest_card(w, rest), w, h, cards.PW, BODY_H)
            show = f"prompts/{iid}/{name}.png"
            _save_k4(body, os.path.join(out, show))
            bindings[name] = dict(show=show, strip=rest.get("strip", ""),
                                  emit=dict(kind="answer", data=dict(rid=item["id"], answer=ans,
                                                                     occurrence=p["occurrence"].isoformat(timespec="minutes"))))
        prompts.append(dict(id=iid, group=item["id"], slot_hint=panel, question=item.get("question") or item["title"],
                            ask=f"prompts/{iid}/ask.png",
                            **{"from": to_aware(p["start"]).isoformat(timespec="seconds"),
                               "until": to_aware(p["until"]).isoformat(timespec="seconds")},
                            bindings=bindings))
    doc = dict(surface=f"{w}x{h}.k4", prompts=prompts)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "prompts.json.tmp"), "w") as f:
        json.dump(doc, f, indent=1)
    os.replace(os.path.join(out, "prompts.json.tmp"), os.path.join(out, "prompts.json"))
    if later:
        with open(os.path.join(out, "timing.json"), "w") as f:
            json.dump(dict(next_change_at=to_aware(later).isoformat(timespec="seconds")), f)
    for iid in skipped:
        note(f"skipped prompt {iid!r}: not a safe name")
    return doc


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m tools.reminders", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="write prompt instances for slowbhub (prompts.json + binding bodies)")
    r.add_argument("--out", required=True, help="the job's output directory ($OUT)")
    r.add_argument("--now", help="ISO time to render for (default $SLOWAPP_NOW, else now)")
    r.add_argument("--until", help="ISO time of the next render (default the next 00:01)")
    r.add_argument("--state", help="reminders.json to read (default $EPAPER_STATE/reminders.json)")
    args = ap.parse_args(argv)
    import sys
    when = args.now or os.environ.get("SLOWAPP_NOW")
    now = to_local(datetime.fromisoformat(when)) if when else None
    until = to_local(datetime.fromisoformat(args.until)) if args.until else None
    doc = render(args.out, now=now, path=args.state, horizon=until, note=lambda m: print(m, file=sys.stderr))
    print(f"reminders: {len(doc['prompts'])} prompt instance(s) at {doc['surface']} -> {args.out}")


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reminders</title>
<style>
@font-face { font-family: "Alfa Slab One"; src: url("/font.ttf"); }
:root { --k: #111; --w: #fbfaf6; --y: #ffd400; --r: #d8231b; --paper: #e9e7e1; --muted: #5d5a53; --rule: #c9c5bb;
  --display: "Alfa Slab One", Georgia, serif; --body: system-ui, -apple-system, "Segoe UI", sans-serif; --mono: ui-monospace, Menlo, monospace; }
@media (prefers-color-scheme: dark) { :root { --paper: #1b1c1d; --w: #26282a; --k: #ecebe6; --muted: #a29e95; --rule: #3a3c3e; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--k); font: 15px/1.5 var(--body); }
.wrap { max-width: 900px; margin: 0 auto; padding: 24px 16px 48px; display: grid; gap: 18px; }
h1 { font: 30px/1.1 var(--display); margin: 0; color: var(--r); }
h2 { font: 20px/1.2 var(--display); margin: 0; }
a { color: inherit; }
.sub { color: var(--muted); margin: 4px 0 0; }
.card { background: var(--w); border: 1px solid var(--rule); border-radius: 8px; padding: 16px; display: grid; gap: 10px; }
.card.open { border: 2px solid var(--y); }
.label { font: 600 11px/1 var(--mono); letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
.row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.status { font: 13px/1.4 var(--mono); }
.status.open { background: var(--y); color: #111; padding: 2px 6px; border-radius: 4px; }
.log { margin: 0; padding-left: 18px; font: 12px/1.5 var(--mono); color: var(--muted); }
button { font: 600 14px/1 var(--body); padding: 10px 13px; border-radius: 8px; border: 2px solid var(--k); background: var(--w); color: var(--k); cursor: pointer; }
button.primary { background: var(--k); color: var(--w); }
button.danger { border-color: var(--r); color: var(--r); }
button:focus-visible, input:focus-visible, select:focus-visible { outline: 3px solid var(--y); outline-offset: 2px; }
form { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
form label { display: grid; gap: 4px; font-size: 13px; color: var(--muted); }
form .wide { grid-column: 1 / -1; }
input, select { font: 15px/1.2 var(--body); padding: 9px; border-radius: 6px; border: 1px solid var(--rule); background: var(--paper); color: var(--k); min-width: 0; }
.keys { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; font-size: 14px; }
.chip { font: 700 13px/1 var(--mono); border: 2px solid var(--k); border-radius: 6px; padding: 3px 8px; white-space: nowrap; }
.msg { font: 13px/1.4 var(--mono); min-height: 1.4em; }
[hidden] { display: none !important; }
</style></head><body><div class="wrap">
<header><h1>Reminders</h1><p class="sub">Questions that come back until they're answered. When one is due it appears in the
peek strip of its panel and you answer with the keys. <a href="/">← Panel</a></p></header>
<div class="card"><span class="label">Answering on the keys</span>
  <div class="keys"><span class="chip">panel key · tap</span><span>Yes: done until the next one</span>
  <span class="chip">● · tap</span><span>Not yet: ask again at the retry time (or in 3 h)</span>
  <span class="chip">panel key · hold</span><span>Snooze for an hour</span>
  <span class="chip">● · hold</span><span>Skip this one</span></div></div>
<div id="list" style="display:grid;gap:12px"></div>
<div class="card"><h2 id="form-title">New reminder</h2>
<form id="form">
  <input type="hidden" name="id">
  <label class="wide">Title<input name="title" required placeholder="NexGuard for Hudson"></label>
  <label class="wide">Question on the panel<input name="question" placeholder="Did Hudson get NexGuard today?"></label>
  <label>Repeats<select name="repeat"><option value="monthly">Monthly</option><option value="weekly">Weekly</option><option value="daily">Daily</option><option value="once">Once</option></select></label>
  <label data-for="monthly">Day of the month<input name="day" type="number" min="1" max="31" value="1"></label>
  <label data-for="weekly">Weekday<select name="weekday"><option value="0">Monday</option><option value="1">Tuesday</option><option value="2">Wednesday</option><option value="3">Thursday</option><option value="4">Friday</option><option value="5">Saturday</option><option value="6">Sunday</option></select></label>
  <label data-for="once">Date<input name="date" type="date"></label>
  <label>Ask at<input name="time" type="time" value="07:45" required></label>
  <label>“Not yet” asks again at<input name="retry" type="time" value="18:00"></label>
  <label>Panel<select name="panel"><option value="right">■ Agenda (right)</option><option value="middle">◆ Weather (middle)</option><option value="left">▲ Map (left)</option></select></label>
  <label>On<select name="enabled"><option value="1">Yes</option><option value="">Paused</option></select></label>
  <div class="row wide"><button class="primary">Save</button><button type="button" id="cancel" hidden>Cancel</button><span class="msg" id="msg"></span></div>
</form></div>
</div>
<script>
const $ = id => document.getElementById(id);
const form = $("form");
const NAMES = { yes: "Yes", not_yet: "Not yet", snooze: "Snooze 1 h", skip: "Skip" };
let items = [];
function showFields() {
  const rep = form.repeat.value;
  form.querySelectorAll("[data-for]").forEach(l => l.hidden = l.dataset.for !== rep);
}
form.repeat.addEventListener("change", showFields);
async function post(verb, body) {
  const r = await fetch("/api/reminders/" + verb, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json();
  $("msg").textContent = r.ok ? "" : j.error;
  load();
  return r.ok;
}
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) k === "onclick" ? e.onclick = v : e.setAttribute(k, v);
  e.append(...kids);
  return e;
}
function edit(it) {
  form.reset();
  for (const k of ["id", "title", "question", "repeat", "day", "weekday", "date", "time", "retry", "panel"]) if (it[k] !== undefined) form[k].value = it[k];
  form.enabled.value = it.enabled === false ? "" : "1";
  $("form-title").textContent = "Edit reminder"; $("cancel").hidden = false; showFields();
  form.scrollIntoView({ behavior: "smooth" });
}
$("cancel").onclick = () => { form.reset(); form.id.value = ""; $("form-title").textContent = "New reminder"; $("cancel").hidden = true; showFields(); };
async function load() {
  const j = await (await fetch("/api/reminders", { cache: "no-store" })).json();
  items = j.items;
  $("list").replaceChildren(...(items.length ? items.map(it => el("div", { class: "card" + (it.open ? " open" : "") },
    el("div", { class: "row" }, el("h2", {}, it.title), el("span", { class: "status" + (it.open ? " open" : "") }, it.status)),
    el("div", {}, (it.question ? `“${it.question}” · ` : "") + it.schedule + ` · ${({ left: "▲ map", middle: "◆ weather", right: "■ agenda" })[it.panel] || it.panel} panel`),
    el("div", { class: "row" },
      el("button", { class: "primary", onclick: () => post("answer", { id: it.id, answer: "yes" }) }, "Mark done"),
      el("button", { onclick: () => post("ask", { id: it.id }) }, "Ask now"),
      el("button", { onclick: () => edit(it) }, "Edit"),
      el("button", { class: "danger", onclick: () => confirmDelete(it) }, "Delete")),
    (it.log || []).length ? el("ol", { class: "log" }, ...it.log.slice(0, 6).map(l => el("li", {}, `${l.at.replace("T", " ")} · ${NAMES[l.answer] || l.answer} · ${l.via}`))) : ""
  )) : [el("div", { class: "card" }, "No reminders yet.")]));
}
function confirmDelete(it) {
  const b = event.target;
  if (b.dataset.sure) return post("delete", { id: it.id });
  b.dataset.sure = "1"; b.textContent = "Really delete?";
  setTimeout(() => { b.textContent = "Delete"; delete b.dataset.sure; }, 4000);
}
form.addEventListener("submit", async e => {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(form));
  body.enabled = !!body.enabled;
  if (await post("save", body)) $("cancel").onclick();
});
showFields(); load(); setInterval(load, 10000);
</script></body></html>"""


if __name__ == "__main__":
    main()
