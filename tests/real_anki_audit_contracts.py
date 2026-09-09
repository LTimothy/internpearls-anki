"""Run with Anki's Python, separately from pytest's mock process.

Creates only disposable synthetic collections; never opens a user profile.
"""
import os
from pathlib import Path
import sys
import tempfile
import types
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import mock_anki
world = mock_anki.install()
for name in list(sys.modules):
    if name == 'anki' or name.startswith('anki.'):
        del sys.modules[name]
package = types.ModuleType('internpearls')
package.__path__ = [str(ROOT / 'internpearls')]
sys.modules['internpearls'] = package
from anki.collection import Collection
from internpearls import collection as addon, sync, config, logic


def model(col, kind):
    m = col.models.new('Study Deck - ' + kind)
    for field in config.TARGET_FIELDS[m['name']]:
        col.models.add_field(m, col.models.new_field(field))
    t = col.models.new_template('Card 1')
    if kind == 'Cloze':
        m['type'] = 1
        t['qfmt'] = t['afmt'] = '{{cloze:Text}}'
    else:
        t['qfmt'], t['afmt'] = '{{Front}}', '{{Back}}'
    col.models.add_template(m, t)
    col.models.add(m)
    return m


def run():
    with tempfile.TemporaryDirectory(prefix='ip-real-contracts-') as tmp:
        root = Path(tmp)
        p = Collection(str(root / 'source.anki2'))
        c = Collection(str(root / 'reader.anki2'))
        try:
            for mod in (config, sync, addon):
                mod.INSTALLED = str(root / 'installed.json')
            sync.SHIPPED = str(root / 'shipped_fields.json')
            config.DECLINED = str(root / 'declined.json')
            world.mw.reset = lambda: None
            basic, cloze = model(p, 'Basic'), model(p, 'Cloze')
            for guid, m, text in [('one', basic, 'Question'), ('dummy', cloze, '{{c1::dummy}}')]:
                n = p.new_note(m)
                n.guid, n.fields[0], n.tags = guid, text, ['InternPearls']
                p.add_note(n, p.decks.id('Synthetic'))
            world.mw.col = p
            initial = str(root / 'initial.apkg')
            addon._export_deck_to(initial, 'Synthetic')
            with zipfile.ZipFile(initial) as archive:
                assert {'collection.anki2', 'collection.anki21'} <= set(archive.namelist())
            rows = {guid: rid for rid, _front, guid in logic.apkg_notes(initial)}
            rewritten = str(root / 'rewritten.apkg')
            logic.write_personalized(initial, {rows['one']: 'remapped'}, rewritten,
                                     drop={rows['dummy']})
            legacy_reader = Collection(str(root / 'legacy-reader.anki2'))
            try:
                world.mw.col = legacy_reader
                addon._import_apkg(rewritten)
                assert legacy_reader.db.list('select guid from notes') == ['remapped']
                assert legacy_reader.db.scalar('select count(*) from cards') == 1
                print('PASS real dual-member import honors GUID rewrite and declined drop')
            finally:
                legacy_reader.close()
            world.mw.col = c
            addon._import_apkg(initial)
            nid = c.db.scalar('select id from notes where guid=?', 'one')
            n = c.get_note(nid)
            n['Notes'], n['Back'] = 'my annotation', 'my protected answer'
            c.update_note(n)
            world.mw.col = p
            addon.change_note_types([{'guid': 'one', 'old': basic['name'], 'new': cloze['name']}])
            pnid = p.db.scalar('select id from notes where guid=?', 'one')
            n = p.get_note(pnid)
            n['Text'] = 'A {{c1::fact}}'
            p.update_note(n)
            p.db.execute('update notes set mod=1000000000 where id=?', pnid)
            incoming = str(root / 'incoming.apkg')
            addon._export_deck_to(incoming, 'Synthetic')
            decline_reader = Collection(str(root / 'decline-reader.anki2'))
            ask = sync._ask
            try:
                world.mw.col = decline_reader
                addon._import_apkg(initial)
                old_nid = decline_reader.db.scalar('select id from notes where guid=?', 'one')
                original = decline_reader.get_note(old_nid)
                original['Back'] = 'protected answer'
                decline_reader.update_note(original)
                decline_cfg = config._cfg()
                decline_cfg['protected'] = ['back']
                sync._ask = lambda *args, **kwargs: False
                decline_manifest = {'decks': [{'name': 'Synthetic', 'version': 'v2'}]}
                outcome = sync._run_sync(decline_cfg, decline_manifest, lambda d: incoming,
                                         decline_manifest['decks'])
                assert decline_reader.get_note(old_nid)['Back'] == 'protected answer'
                assert decline_reader.db.scalar('select count(*) from notes') == 3, outcome
                print('PASS real declined conversion imports a separate new note')
                canonical_nid = decline_reader.db.scalar('select id from notes where guid=?', 'one')
                assert canonical_nid != old_nid
                world.mw.col = p
                revised = p.get_note(pnid)
                revised['Text'] = 'A reworded {{c1::fact}}'
                p.update_note(revised)
                followup = str(root / 'followup.apkg')
                addon._export_deck_to(followup, 'Synthetic')
                world.mw.col = decline_reader
                def unexpected_offer(*args, **kwargs):
                    raise AssertionError('the canonical new note must not need another conversion')
                sync._ask = unexpected_offer
                outcome = sync._run_sync(decline_cfg, decline_manifest, lambda d: followup,
                                         decline_manifest['decks'])
                assert decline_reader.db.scalar('select count(*) from notes') == 3, outcome
                assert decline_reader.get_note(canonical_nid)['Text'] == 'A reworded {{c1::fact}}'
                assert decline_reader.get_note(old_nid)['Back'] == 'protected answer'
                print('PASS follow-up updates canonical note without another conversion or duplicate')
            finally:
                sync._ask = ask
                decline_reader.close()
            world.mw.col = c
            cfg = config._cfg()
            manifest = {'decks': [{'name': 'Synthetic', 'version': 'v2'}]}
            missing_reader = Collection(str(root / 'missing-reader.anki2'))
            try:
                world.mw.col = missing_reader
                addon._import_apkg(initial)
                missing_reader.models.remove(missing_reader.models.by_name(cloze['name'])['id'])
                old_nid = missing_reader.db.scalar('select id from notes where guid=?', 'one')
                first = sync._run_sync(cfg, manifest, lambda d: incoming, manifest['decks'],
                                       convert_notetypes=True)
                assert missing_reader.models.by_name(cloze['name']) is not None, first
                assert missing_reader.get_note(old_nid).note_type()['name'] == basic['name']
                assert config._load_json(sync.INSTALLED, {}).get('Synthetic') is None
                second = sync._run_sync(cfg, manifest, lambda d: incoming, manifest['decks'],
                                        convert_notetypes=True)
                assert missing_reader.get_note(old_nid)['Text'] == 'A {{c1::fact}}', second
                assert config._load_json(sync.INSTALLED, {})['Synthetic'] == 'v2'
                print('PASS missing target first pass stays pending and second pass converges')
            finally:
                missing_reader.close()
                world.mw.col = c
            before = list(c.get_note(nid).fields)
            cfg['protected'] = ['Notes', 'Back']
            sync._run_sync(cfg, manifest, lambda d: incoming, manifest['decks'], convert_notetypes=True)
            assert c.get_note(nid).note_type()['name'] == basic['name'], 'unmappable protected answer must block conversion'
            assert c.get_note(nid).fields == before
            print('PASS protected conversion rejected before mutation')
            cfg['protected'] = ['Notes']
            real_import = addon._import_apkg
            def fail(path):
                return real_import(str(root / 'missing-package.apkg'))
            addon._import_apkg = fail
            try:
                sync._run_sync(cfg, manifest, lambda d: incoming, manifest['decks'], convert_notetypes=True)
            finally:
                addon._import_apkg = real_import
            assert c.get_note(nid).note_type()['name'] == basic['name'], 'failed import must undo conversion'
            assert c.get_note(nid).fields == before
            print('PASS failed import rolls conversion back')
            sync._run_sync(cfg, manifest, lambda d: incoming, manifest['decks'], convert_notetypes=True)
            n = c.get_note(nid)
            assert n['Text'] == 'A {{c1::fact}}', 'source older than local conversion must still import'
            assert n['Notes'] == 'my annotation'
            assert config._load_json(sync.SHIPPED, {})['one']['Notes'] == ''
            print('PASS real import replaces older source and preserves annotation')
            sync._run_sync(cfg, manifest, lambda d: incoming, manifest['decks'], convert_notetypes=True)
            assert c.get_note(nid)['Notes'] == 'my annotation'
            assert config._load_json(sync.SHIPPED, {})['one']['Notes'] == ''
            print('PASS repeated real sync does not poison shipped annotation baseline')
            backup = str(root / 'backup.apkg')
            addon._export_deck_to(backup, 'Synthetic')
            n = c.get_note(nid)
            n['Text'], n['Notes'] = '{{c1::later edit}}', 'later annotation'
            c.update_note(n)
            c.db.execute('update notes set mod=2000000000 where id=?', nid)
            addon._import_apkg(backup, with_scheduling=True)
            assert c.get_note(nid)['Text'] == 'A {{c1::fact}}'
            assert c.get_note(nid)['Notes'] == 'my annotation'
            print('PASS real older backup replaces newer local content')
        finally:
            c.close()
            p.close()


if __name__ == '__main__':
    run()
