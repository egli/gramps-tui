"""Headless check: runs gtui against a temp copy of a Gramps tree.

Usage: python3 test_gtui.py PATH_TO_TREE_DIR   (e.g. ~/.local/share/gramps/grampsdb/<id>)
"""
import asyncio, os, shutil, sys, tempfile

import gtui


async def main(src):
    tmp = os.path.join(tempfile.mkdtemp(), "tree")
    shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("lock"))
    db = gtui.open_tree(tmp)
    app = gtui.GTui(db)
    forms = []  # queued editor results
    app.run_editor = lambda text: forms.pop(0)(text)
    try:
        async with app.run_test() as pilot:
            me = app.me
            n_people = db.get_number_of_people()

            # edit: change birth date
            forms.append(lambda t: t.replace("birth:", "birth: 1.2.1900", 1) if "birth: \n" in t
                         else "\n".join("birth: 1.2.1900" if l.startswith("birth:") else l for l in t.splitlines()))
            await pilot.press("e")
            assert gtui.fmt_date(gtui.event_of(db, app.me, "birth")) == "1900-02-01", gtui.details(db, app.me)

            # add spouse: new person via editor
            forms.append(lambda t: t.replace("given: ", "given: Testa").replace("gender: U", "gender: F"))
            await pilot.press("s")
            await pilot.press("enter")  # "+ New person" is highlighted first
            await pilot.pause()
            assert db.get_number_of_people() == n_people + 1
            fam = db.get_family_from_handle(app.me.get_family_handle_list()[-1])
            spouse = db.get_person_from_handle(fam.get_mother_handle() or fam.get_father_handle())
            assert spouse.get_primary_name().get_first_name() == "Testa"
            assert fam.handle in spouse.get_family_handle_list()

            # add child: new person, lands in the highlighted/first own family
            forms.append(lambda t: t.replace("given: ", "given: Kiddo"))
            await pilot.press("c")
            await pilot.press("enter")
            await pilot.pause()
            kids = [db.get_person_from_handle(r.ref) for f in app.me.get_family_handle_list()
                    for r in db.get_family_from_handle(f).get_child_ref_list()]
            kid = next(k for k in kids if k.get_primary_name().get_first_name() == "Kiddo")
            assert kid.get_parent_family_handle_list()

            # navigate to the kid via search, check parents shown, go back
            await pilot.press("slash")
            await pilot.press(*"Kiddo", "enter")
            assert app.me.handle == kid.handle
            assert any(r[0].startswith("  Father") or r[0].startswith("  Mother") for r in app.rows)

            # unlink kid from its parents (first row = Parents header), then delete kid
            app.query_one("#rel").highlighted = 0
            await pilot.press("x", "y")
            assert not db.get_person_from_handle(kid.handle).get_parent_family_handle_list()
            await pilot.press("D", "y")
            assert not db.has_person_handle(kid.handle)
            assert app.me.handle == me.handle  # fell back in history

            # bad gender re-opens editor; closing unchanged cancels
            forms.append(lambda t: t.replace("gender: U", "gender: Q").replace("given: ", "given: Bad"))
            forms.append(lambda t: (assert_err(t), t)[1])
            await pilot.press("n")
            assert db.get_number_of_people() == n_people + 1  # only Testa added
    finally:
        db.close()
    assert not os.path.exists(os.path.join(tmp, "lock"))
    print("ok", tmp)


def assert_err(t):
    assert t.startswith("# ERROR: gender"), t


asyncio.run(main(os.path.expanduser(sys.argv[1])))
