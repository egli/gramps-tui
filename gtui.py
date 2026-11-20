#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Christian Egli
"""gtui: LifeLines-style terminal browser/editor for Gramps family trees.

Usage: gtui.py TREE     (tree name as listed in Gramps, or path to a tree directory)

All writes go through Gramps' own database layer (DbTxn), so backlinks and
indexes stay consistent. Gramps must not have the tree open at the same time.
"""
import os
import shlex
import subprocess
import sys
import tempfile

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from gramps.gen.datehandler import displayer as date_disp, parser as date_parser
from gramps.gen.db import DbTxn
from gramps.gen.db.utils import get_dbid_from_path, lookup_family_tree, make_database
from gramps.gen.display.name import displayer as name_disp
from gramps.gen.display.place import displayer as place_disp
from gramps.gen.lib import Date, Event, EventRef, EventType, Family, Person
from gramps.gen.utils.db import get_birth_or_fallback, get_death_or_fallback

GENDER = {Person.MALE: "M", Person.FEMALE: "F", Person.UNKNOWN: "U", Person.OTHER: "X"}
GENDER_IN = {v: k for k, v in GENDER.items()}
NEW = "__new__"


def open_tree(arg):
    if os.path.isdir(arg):
        path = arg
    else:
        found = lookup_family_tree(arg)
        if not found:
            sys.exit(f"No Gramps tree named {arg!r}")
        path = found[0]
    lock = os.path.join(path, "lock")
    if os.path.exists(lock):
        with open(lock, encoding="utf8") as f:
            sys.exit(f"Tree is locked by {f.read().strip()}. Close it in Gramps first.")
    db = make_database(get_dbid_from_path(path))
    db.load(path)
    return db


# ---------- read helpers ----------

def fmt_date(ev):
    return date_disp.display(ev.get_date_object()) if ev else ""


def label(db, h):
    p = db.get_person_from_handle(h)
    b, d = fmt_date(get_birth_or_fallback(db, p)), fmt_date(get_death_or_fallback(db, p))
    life = f"  {b}–{d}" if b or d else ""
    return f"{name_disp.display(p)}{life}  [{p.gramps_id}]"


def event_of(db, person, kind):
    ref = person.get_birth_ref() if kind == "birth" else person.get_death_ref()
    return db.get_event_from_handle(ref.ref) if ref else None


def details(db, p):
    lines = [f"{name_disp.display(p)}   [{p.gramps_id}]   {GENDER.get(p.gender, '?')}"]
    for kind in ("birth", "death"):
        ev = event_of(db, p, kind)
        if ev:
            place = place_disp.display_event(db, ev)
            lines.append(f"  {kind:6} {fmt_date(ev)}" + (f", {place}" if place else ""))
    return "\n".join(lines)


def relatives(db, me):
    """Rows of (text, person_to_visit, family_handle, unlink) where unlink = (role, person, family)."""
    rows = []
    for fh in me.get_parent_family_handle_list():
        f = db.get_family_from_handle(fh)
        unlink = ("child", me.handle, fh)
        rows.append((f"Parents  [{f.gramps_id}]", None, fh, unlink))
        for role, h in (("Father", f.get_father_handle()), ("Mother", f.get_mother_handle())):
            if h:
                rows.append((f"  {role}: {label(db, h)}", h, fh, unlink))
        for ref in f.get_child_ref_list():
            if ref.ref != me.handle:
                rows.append((f"  Sibling: {label(db, ref.ref)}", ref.ref, fh, None))
    for fh in me.get_family_handle_list():
        f = db.get_family_from_handle(fh)
        sp = f.get_mother_handle() if f.get_father_handle() == me.handle else f.get_father_handle()
        rows.append((f"Family  [{f.gramps_id}]", None, fh, ("parent", me.handle, fh)))
        if sp:
            rows.append((f"  Spouse: {label(db, sp)}", sp, fh, ("parent", sp, fh)))
        for ref in f.get_child_ref_list():
            rows.append((f"    Child: {label(db, ref.ref)}", ref.ref, fh, ("child", ref.ref, fh)))
    return rows


# ---------- write helpers (callers wrap in DbTxn) ----------

TEMPLATE = """\
# Save and close to apply; close unchanged to cancel.
# gender: M, F, U or X. Dates in any form Gramps understands; empty = unset.
given: {given}
surname: {surname}
gender: {gender}
birth: {birth}
death: {death}
"""


def person_form(db, p):
    name = p.get_primary_name()
    return TEMPLATE.format(
        given=name.get_first_name(), surname=name.get_surname(),
        gender=GENDER.get(p.gender, "U"),
        birth=fmt_date(event_of(db, p, "birth")), death=fmt_date(event_of(db, p, "death")),
    )


def parse_form(text):
    """Return (fields, error)."""
    data = {}
    for line in text.splitlines():
        if line.startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        data[key.strip()] = val.strip()
    missing = [k for k in ("given", "surname", "gender", "birth", "death") if k not in data]
    if missing:  # a deleted line must not silently wipe that field
        return None, f"missing line(s): {', '.join(missing)} (leave the value empty instead)"
    g = data["gender"].upper() or "U"
    if g not in GENDER_IN:
        return None, f"gender must be one of M, F, U, X (got {g!r})"
    if not (data["given"] or data["surname"]):
        return None, "given name or surname required"
    data["gender"] = g
    return data, None


def apply_form(db, p, data, trans):
    name = p.get_primary_name()
    name.set_first_name(data.get("given", ""))
    name.get_primary_surname().set_surname(data.get("surname", ""))
    p.set_primary_name(name)
    p.set_gender(GENDER_IN[data["gender"]])
    for kind, etype in (("birth", EventType.BIRTH), ("death", EventType.DEATH)):
        text = data.get(kind, "")
        ev = event_of(db, p, kind)
        if text == fmt_date(ev):
            continue
        if ev is None:
            ev = Event()
            ev.set_type(etype)
            db.add_event(ev, trans)
            ref = EventRef()
            ref.ref = ev.handle
            p.add_event_ref(ref)
            (p.set_birth_ref if kind == "birth" else p.set_death_ref)(ref)
        ev.set_date_object(date_parser.parse(text) if text else Date())
        db.commit_event(ev, trans)


def couple(f, a, b):
    """Put a and b into family f's father/mother slots, guided by gender."""
    if a.gender == Person.FEMALE or (b.gender == Person.MALE and a.gender != Person.MALE):
        a, b = b, a
    f.set_father_handle(a.handle)
    f.set_mother_handle(b.handle)


# ---------- screens ----------

class Picker(ModalScreen):
    """Filterable person list. Dismisses with a handle, NEW, or None."""

    BINDINGS = [("escape", "dismiss(None)", "Cancel"), ("down", "move(1)"), ("up", "move(-1)")]
    CSS = "Picker { align: center middle; } #box { width: 90%; height: 80%; border: round $accent; }"

    def __init__(self, title, people, allow_new):
        super().__init__()
        self.title_text, self.people, self.allow_new = title, people, allow_new

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(self.title_text)
            yield Input(placeholder="type to filter")
            yield OptionList()

    def on_mount(self):
        self.refill("")

    def refill(self, q):
        ol = self.query_one(OptionList)
        ol.clear_options()
        if self.allow_new:
            ol.add_option(Option(Text("+ New person"), id=NEW))
        ol.add_options(Option(Text(t), id=h) for t, h in self.people if q.lower() in t.lower())
        ol.highlighted = 0 if ol.option_count else None

    def on_input_changed(self, ev):
        self.refill(ev.value)

    def action_move(self, d):
        ol = self.query_one(OptionList)
        (ol.action_cursor_down if d > 0 else ol.action_cursor_up)()

    def on_input_submitted(self, _):
        ol = self.query_one(OptionList)
        if ol.highlighted is not None:
            self.dismiss(ol.get_option_at_index(ol.highlighted).id)

    def on_option_list_option_selected(self, ev):
        self.dismiss(ev.option.id)


class Confirm(ModalScreen):
    BINDINGS = [("y", "dismiss(True)", "Yes"), ("n,escape", "dismiss(False)", "No")]
    CSS = "Confirm { align: center middle; } Static { width: auto; border: round $error; padding: 1 2; }"

    def __init__(self, msg):
        super().__init__()
        self.msg = msg

    def compose(self) -> ComposeResult:
        yield Static(Text(self.msg + "  [y/n]"))


class GTui(App):
    TITLE = "gtui"
    CSS = "#details { padding: 1 2; border-bottom: solid $accent; } OptionList { border: none; }"
    BINDINGS = [
        ("slash", "search", "Search"), ("e", "edit", "Edit"), ("n", "new", "New"),
        ("p", "add_parent", "+Parent"), ("s", "add_spouse", "+Spouse"), ("c", "add_child", "+Child"),
        ("x", "unlink", "Unlink"), ("D", "delete", "Delete"),
        ("backspace,b", "back", "Back"), ("q", "quit", "Quit"),
    ]

    def __init__(self, db):
        super().__init__()
        self.db = db
        self.history = []  # stack of person handles; last = current
        self.rows = []

    def compose(self) -> ComposeResult:
        yield Static(id="details")
        yield OptionList(id="rel")
        yield Footer()

    def on_mount(self):
        start = self.db.get_default_person() or next(self.db.iter_people(), None)
        if start:
            self.history.append(start.handle)
        self.refresh_view()

    # ----- view -----

    @property
    def me(self):
        return self.db.get_person_from_handle(self.history[-1]) if self.history else None

    def refresh_view(self):
        ol = self.query_one("#rel", OptionList)
        ol.clear_options()
        me = self.me
        if me is None:
            self.query_one("#details", Static).update("Empty tree. Press n to add a person.")
            self.rows = []
            return
        self.query_one("#details", Static).update(Text(details(self.db, me)))
        self.rows = relatives(self.db, me)
        ol.add_options(Option(Text(r[0])) for r in self.rows)
        ol.highlighted = 0 if self.rows else None
        ol.focus()

    def selected_row(self):
        i = self.query_one("#rel", OptionList).highlighted
        return self.rows[i] if i is not None and i < len(self.rows) else None

    def goto(self, h):
        self.history.append(h)
        self.refresh_view()

    def on_option_list_option_selected(self, ev):
        h = self.rows[ev.option_index][1]
        if h:
            self.goto(h)

    def action_back(self):
        if len(self.history) > 1:
            self.history.pop()
            self.refresh_view()

    def all_people(self, exclude=()):
        rows = [(label(self.db, p.handle), p.handle) for p in self.db.iter_people() if p.handle not in exclude]
        return sorted(rows)  # ponytail: labels rebuilt per picker; cache if trees get into the 10k range

    # ----- editor -----

    def run_editor(self, text):
        """Open text in $VISUAL/$EDITOR, return the saved text."""
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        with tempfile.NamedTemporaryFile("w+", suffix=".gtui", delete=False, encoding="utf8") as f:
            f.write(text)
        try:
            with self.suspend():
                subprocess.run([*shlex.split(editor), f.name])
            with open(f.name, encoding="utf8") as g:
                return g.read()
        finally:
            os.unlink(f.name)

    def edit_form(self, form):
        """Loop until the form parses or the user closes it unchanged. Returns fields or None."""
        text = form
        while True:
            out = self.run_editor(text)
            if out == text:
                return None
            data, err = parse_form(out)
            if not err:
                return data
            text = f"# ERROR: {err}\n" + "\n".join(l for l in out.splitlines() if not l.startswith("# ERROR"))

    def new_person(self):
        data = self.edit_form(TEMPLATE.format(given="", surname="", gender="U", birth="", death=""))
        if data is None:
            return None
        p = Person()
        with DbTxn("gtui: add person", self.db) as t:
            apply_form(self.db, p, data, t)
            self.db.add_person(p, t)
        return p.handle

    # ----- actions -----

    def action_edit(self):
        me = self.me
        if not me:
            return
        data = self.edit_form(person_form(self.db, me))
        if data:
            with DbTxn(f"gtui: edit {me.gramps_id}", self.db) as t:
                apply_form(self.db, me, data, t)
                self.db.commit_person(me, t)
            self.refresh_view()

    def action_new(self):
        h = self.new_person()
        if h:
            self.goto(h)

    def action_search(self):
        def done(h):
            if h:
                self.goto(h)
        self.push_screen(Picker("Go to person", self.all_people(), False), done)

    def pick_then(self, title, fn):
        """Pick an existing or new person (not me), then call fn(other)."""
        me = self.me
        if not me:
            return

        def done(h):
            if h == NEW:
                h = self.new_person()
            if h:
                try:
                    fn(self.db.get_person_from_handle(h))
                except ValueError as e:
                    self.notify(str(e), severity="error")
                self.refresh_view()
        self.push_screen(Picker(title, self.all_people(exclude={me.handle}), True), done)

    def action_add_spouse(self):
        def fn(sp):
            me = self.me
            f = Family()
            couple(f, me, sp)
            with DbTxn("gtui: add spouse", self.db) as t:
                self.db.add_family(f, t)
                for p in (me, sp):
                    p.add_family_handle(f.handle)
                    self.db.commit_person(p, t)
        self.pick_then("Add spouse", fn)

    def action_add_child(self):
        def fn(child):
            me = self.me
            row = self.selected_row()
            mine = me.get_family_handle_list()
            fh = row[2] if row and row[2] in mine else (mine[0] if mine else None)
            with DbTxn("gtui: add child", self.db) as t:
                if fh:
                    f = self.db.get_family_from_handle(fh)
                else:
                    f = Family()
                    (f.set_mother_handle if me.gender == Person.FEMALE else f.set_father_handle)(me.handle)
                    self.db.add_family(f, t)
                    me.add_family_handle(f.handle)
                    self.db.commit_person(me, t)
                if any(r.ref == child.handle for r in f.get_child_ref_list()):
                    raise ValueError(f"already a child in family {f.gramps_id}")  # aborts the txn
                self.db.add_child_to_family(f, child, trans=t)
        self.pick_then("Add child", fn)

    def action_add_parent(self):
        def fn(par):
            me = self.me
            want_mother = par.gender == Person.FEMALE

            def free(f):
                if par.gender in (Person.MALE, Person.FEMALE):
                    return not (f.get_mother_handle() if want_mother else f.get_father_handle())
                return not (f.get_father_handle() and f.get_mother_handle())

            fams = [self.db.get_family_from_handle(h) for h in me.get_parent_family_handle_list()]
            f = next((f for f in fams if free(f)), None)
            with DbTxn("gtui: add parent", self.db) as t:
                if f is None:
                    f = Family()
                    self.db.add_family(f, t)
                    self.db.add_child_to_family(f, me, trans=t)
                if want_mother or (par.gender != Person.MALE and f.get_father_handle()):
                    f.set_mother_handle(par.handle)
                else:
                    f.set_father_handle(par.handle)
                par.add_family_handle(f.handle)
                self.db.commit_family(f, t)
                self.db.commit_person(par, t)
        self.pick_then("Add parent", fn)

    def action_unlink(self):
        row = self.selected_row()
        if not row or not row[3]:
            self.notify("Nothing to unlink on this row", severity="warning")
            return
        role, h, fh = row[3]
        who = name_disp.display(self.db.get_person_from_handle(h))
        fid = self.db.get_family_from_handle(fh).gramps_id

        def done(ok):
            if ok:
                with DbTxn("gtui: unlink", self.db) as t:
                    if role == "child":
                        self.db.remove_child_from_family(h, fh, t)
                    else:
                        self.db.remove_parent_from_family(h, fh, t)
                self.refresh_view()
        self.push_screen(Confirm(f"Remove {who} as {role} from family {fid}?"), done)

    def action_delete(self):
        me = self.me
        if not me:
            return

        def done(ok):
            if ok:
                with DbTxn(f"gtui: delete {me.gramps_id}", self.db) as t:
                    self.db.delete_person_from_database(me, t)
                self.history = [h for h in self.history if h != me.handle]
                if not self.history:
                    nxt = next(self.db.iter_people(), None)
                    self.history = [nxt.handle] if nxt else []
                self.refresh_view()
        self.push_screen(Confirm(f"Delete {name_disp.display(me)} [{me.gramps_id}] from the tree?"), done)


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    db = open_tree(sys.argv[1])
    try:
        GTui(db).run()
    finally:
        db.close()


if __name__ == "__main__":
    main()
