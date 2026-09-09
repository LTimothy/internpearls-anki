"""Regression contracts from the independent audit, using synthetic notes only."""
import datetime
from pathlib import Path

import pytest
import mock_anki
from test_sync_flows import _fields, _configure, _write_source, _update, DECK, TAGS


def source(anki, tmp_path, notes=None):
    from internpearls import config, sync
    folder = _write_source(tmp_path, {
        DECK: ('v2', notes or [('g1', _fields('Question', back='new'), TAGS)], None)})
    _configure(anki, folder)
    cfg = config._cfg()
    manifest, fetch, _ = sync._fetch_manifest(cfg)
    return cfg, manifest, fetch


def test_bookkeeping_failure_does_not_erase_personal_fields(anki, tmp_path, monkeypatch):
    from internpearls import sync
    anki.col.add_note('g1', _fields('Question', notes='personal mnemonic'), [TAGS], deck=DECK)
    cfg, manifest, fetch = source(anki, tmp_path)
    save = sync._save_json
    def fail(path, data):
        if path == sync.INSTALLED:
            raise OSError('ENOSPC')
        save(path, data)
    monkeypatch.setattr(sync, '_save_json', fail)
    try:
        sync._run_sync(cfg, manifest, fetch, manifest['decks'])
    except OSError:
        pass
    assert anki.col.note_by_guid('g1')['Notes'] == 'personal mnemonic'


def test_duplicate_front_keeps_complete_guid_identity(anki, tmp_path):
    from internpearls import sync
    anki.col.add_note('g1', _fields('Shared', back='first'), [TAGS], deck=DECK)
    anki.col.add_note('g2', _fields('Shared', back='second'), [TAGS], deck=DECK)
    cfg, manifest, fetch = source(anki, tmp_path, [('g2', _fields('Shared', back='updated'), TAGS)])
    sync._run_sync(cfg, manifest, fetch, manifest['decks'])
    assert anki.col.note_by_guid('g1')['Back'] == 'first'
    assert anki.col.note_by_guid('g2')['Back'] == 'updated'


def test_declined_duplicate_front_guid_keeps_nonnegative_preview_counts(anki, tmp_path):
    from internpearls import collection, logic
    anki.col.add_note('g1', _fields('Shared'), [TAGS], deck=DECK)
    anki.col.add_note('g2', _fields('Shared'), [TAGS], deck=DECK)
    cfg, manifest, fetch = source(anki, tmp_path, [('g2', _fields('Shared'), TAGS)])
    path = fetch(manifest['decks'][0])
    her = collection._her_front_to_guid(cfg['scope_tag'])
    remap, kept, new, _, _ = logic.remap_cards(path, her, {})
    _, touched, kept, new = logic.declined_drop(path, remap, her, {'g2'}, kept, new)
    assert (touched, kept, new) == (set(), 0, 0)


def test_out_of_scope_guid_is_not_imported_over(anki, tmp_path):
    from internpearls import sync
    anki.col.add_note('g1', _fields('Question', notes='personal'), ['Personal'], deck='Personal')
    cfg, manifest, fetch = source(anki, tmp_path)
    sync._run_sync(cfg, manifest, fetch, manifest['decks'])
    assert anki.col.note_by_guid('g1')['Notes'] == 'personal'


def test_source_switch_does_not_reuse_cached_package(anki, tmp_path):
    from internpearls import sync
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir(); b.mkdir()
    _, manifest, fetch = source(anki, a)
    first = sync._cached_fetch(fetch, manifest['decks'][0])
    _, manifest, fetch = source(anki, b, [('other', _fields('Other'), TAGS)])
    second = sync._cached_fetch(fetch, manifest['decks'][0])
    assert first != second


def test_installed_state_is_specific_to_collection(anki, tmp_path):
    from internpearls import config
    anki.col.path = str(tmp_path / 'profile-a' / 'collection.anki2')
    config._save_json(config.INSTALLED, {DECK: 'v2'})
    anki.col.path = str(tmp_path / 'profile-b' / 'collection.anki2')
    assert config._load_json(config.INSTALLED, {}) == {}
    anki.col.path = str(tmp_path / 'profile-a' / 'collection.anki2')
    assert config._load_json(config.INSTALLED, {}) == {DECK: 'v2'}


def test_failed_replacement_does_not_retire_only_copy(anki, tmp_path, monkeypatch):
    from internpearls import collection
    anki.col.add_note('old', _fields('Old'), [TAGS], deck=DECK)
    folder = _write_source(tmp_path, {DECK: ('v2', [('new', _fields('Replacement'), TAGS)], None)},
        retired={DECK: {'old': {'identity': 'Old', 'superseded_by': ['new']}}})
    _configure(anki, folder)
    def fail(path):
        raise RuntimeError('import failed')
    monkeypatch.setattr(collection, '_import_apkg', fail)
    trees = _update(anki)
    old = anki.col.note_by_guid('old')
    assert anki.col.get_card(old.card_ids()[0]).queue != -1
    assert 'replacements have not been imported' in str(trees)


def test_singleton_backups_cannot_overwrite_each_other(anki, tmp_path, monkeypatch):
    from internpearls import collection
    class Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 9, 12, 0, 0)
    monkeypatch.setattr(collection.datetime, 'datetime', Frozen)
    monkeypatch.setattr(collection, '_export_deck_to', lambda path, deck: Path(path).write_text(deck))
    for name in ('Root A', 'Root B'):
        anki.col.decks.id(name)
        assert collection._pre_sync_backup_or_confirm_skip(name, [name]) == (True, True)
    backups = list(Path(collection._deck_backup_folder()).glob('*.apkg'))
    assert sorted(p.read_text() for p in backups) == ['Root A', 'Root B']


def test_backup_retention_does_not_mix_profiles(anki, tmp_path):
    from internpearls import collection
    anki.col.path = str(tmp_path / 'profile-a' / 'collection.anki2')
    first = collection._deck_backup_folder()
    anki.col.path = str(tmp_path / 'profile-b' / 'collection.anki2')
    assert collection._deck_backup_folder() != first


def test_newest_backup_is_retained_when_clock_is_unchanged(anki, monkeypatch):
    from types import SimpleNamespace
    from internpearls import collection
    class Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 9, 12, 0, 0)
    monkeypatch.setattr(collection.datetime, 'datetime', Frozen)
    suffixes = iter(range(30, 0, -1))
    monkeypatch.setattr(collection.uuid, 'uuid4', lambda: SimpleNamespace(hex=f'{next(suffixes):012x}'))
    monkeypatch.setattr(collection, '_export_deck_to', lambda path, deck: Path(path).write_text(deck))
    for _ in range(collection.DECK_BACKUPS_KEEP + 1):
        newest = collection._backup_deck('Synthetic')
    assert Path(newest).is_file()


def test_github_sources_keep_distinct_download_files(anki, tmp_path, monkeypatch):
    from internpearls import sync
    import json
    manifest = {'decks': [{'name': DECK, 'version': 'v1', 'apkg': 'same.apkg'}]}
    def raw(repo, path, *args, **kwargs):
        return json.dumps(manifest).encode() if path == 'manifest.json' else repo.encode()
    monkeypatch.setattr(sync, '_gh_raw', raw)
    cfg = sync._cfg()
    cfg['gh_repo'] = 'example/one'
    _, fetch, _ = sync._fetch_manifest(cfg)
    first = sync._cached_fetch(fetch, manifest['decks'][0])
    cfg = dict(cfg, gh_repo='example/two')
    _, fetch, _ = sync._fetch_manifest(cfg)
    second = sync._cached_fetch(fetch, manifest['decks'][0])
    assert first != second
    assert Path(first).read_bytes() == b'example/one'


def test_installed_state_is_specific_to_source(anki, tmp_path):
    from internpearls import config
    anki.col.path = str(tmp_path / 'collection.anki2')
    anki.mw._config['github_decks_repo'] = 'example/one'
    config._save_json(config.INSTALLED, {DECK: 'v2'})
    anki.mw._config['github_decks_repo'] = 'example/two'
    assert config._load_json(config.INSTALLED, {}) == {}


def test_import_explicitly_updates_older_notes_for_sync_and_restore(anki, monkeypatch):
    from internpearls import collection
    requests = []
    monkeypatch.setattr(anki.col, 'import_anki_package', requests.append)
    collection._import_apkg('synthetic.apkg')
    collection._import_apkg('backup.apkg', with_scheduling=True)
    assert [getattr(r.options, 'update_notes', None) for r in requests] == [1, 1]
    assert all(r.options.merge_notetypes is False for r in requests)


def test_import_failure_reverts_conversion_and_keeps_answer(anki, tmp_path, monkeypatch):
    from internpearls import sync, collection
    cloze = mock_anki.make_model('Study Deck - Cloze', ['Text', 'Why', 'Image', 'Dosing', 'Notes'],
                                 qfmt='{{cloze:Text}}', afmt='{{cloze:Text}}')
    anki.col.models._models.append(cloze)
    anki.col.add_note('g1', _fields('Question', back='original answer'), [TAGS], deck=DECK)
    folder = _write_source(tmp_path, {DECK: ('v2', [('g1', ['{{c1::new}}', '', '', '', ''], TAGS)], cloze)})
    _configure(anki, folder)
    manifest, fetch, _ = sync._fetch_manifest(sync._cfg())
    def fail(path):
        raise RuntimeError('import failed')
    monkeypatch.setattr(collection, '_import_apkg', fail)
    sync._run_sync(sync._cfg(), manifest, fetch, manifest['decks'], convert_notetypes=True)
    assert anki.col.note_by_guid('g1')['Back'] == 'original answer'


def test_partial_import_outcome_rolls_back_and_stays_pending(anki, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from internpearls import sync, collection
    anki.col.add_note('g1', _fields('Question', back='original', notes='mine'), [TAGS], deck=DECK)
    cfg, manifest, fetch = source(anki, tmp_path)
    real = collection._import_apkg
    def partial(path):
        real(path)
        return SimpleNamespace(log=SimpleNamespace(conflicting=[object()]))
    monkeypatch.setattr(collection, '_import_apkg', partial)
    results = sync._run_sync(cfg, manifest, fetch, manifest['decks'])
    assert results[0][0].startswith('✗')
    assert sync._load_json(sync.INSTALLED, {}) == {}
    assert anki.col.note_by_guid('g1')['Back'] == 'original'


def test_single_import_refuses_out_of_scope_guid(anki, tmp_path):
    from internpearls import sync
    anki.col.add_note('g1', _fields('Question', notes='personal'), ['Personal'], deck='Personal')
    _, manifest, fetch = source(anki, tmp_path)
    anki.gui.file_picks.append(fetch(manifest['decks'][0]))
    anki.gui.answers.extend([True, True, True])
    sync.import_single()
    assert anki.col.note_by_guid('g1')['Notes'] == 'personal'


def test_single_import_failure_reverts_conversion(anki, tmp_path, monkeypatch):
    from internpearls import sync, collection
    cloze = mock_anki.make_model('Study Deck - Cloze', ['Text', 'Why', 'Image', 'Dosing', 'Notes'],
                                 qfmt='{{cloze:Text}}', afmt='{{cloze:Text}}')
    anki.col.models._models.append(cloze)
    anki.col.add_note('g1', _fields('Question', back='original answer'), [TAGS], deck=DECK)
    folder = _write_source(tmp_path, {DECK: ('v2', [('g1', ['{{c1::new}}', '', '', '', ''], TAGS)], cloze)})
    _configure(anki, folder)
    manifest, fetch, _ = sync._fetch_manifest(sync._cfg())
    anki.gui.file_picks.append(fetch(manifest['decks'][0]))
    anki.gui.answers.extend([True, True, True])
    def fail(path):
        raise RuntimeError('import failed')
    monkeypatch.setattr(collection, '_import_apkg', fail)
    monkeypatch.setattr(sync, '_import_apkg', fail)
    sync.import_single()
    note = anki.col.note_by_guid('g1')
    assert note.note_type()['name'] == 'Study Deck - Basic'
    assert note['Back'] == 'original answer'


def test_explicit_conversion_respects_lowercase_protected_field_name(anki, tmp_path):
    from internpearls import sync
    cloze = mock_anki.make_model('Study Deck - Cloze', ['Text', 'Why', 'Image', 'Dosing', 'Notes'],
                                 qfmt='{{cloze:Text}}', afmt='{{cloze:Text}}')
    anki.col.models._models.append(cloze)
    anki.col.add_note('g1', _fields('Question', back='protected answer'), [TAGS], deck=DECK)
    folder = _write_source(tmp_path, {DECK: ('v2', [
        ('g1', ['{{c1::new}}', '', '', '', ''], TAGS)], cloze)})
    _configure(anki, folder)
    cfg = sync._cfg()
    cfg['protected'] = ['back']
    manifest, fetch, _ = sync._fetch_manifest(cfg)

    results = sync._run_sync(cfg, manifest, fetch, manifest['decks'],
                             convert_notetypes=True)

    note = anki.col.note_by_guid('g1')
    assert results[0][0].startswith('✗')
    assert note.note_type()['name'] == 'Study Deck - Basic'
    assert note['Back'] == 'protected answer'


def test_sync_declining_unsafe_conversion_still_imports_as_new(anki, tmp_path):
    from internpearls import ai_logic, sync
    cloze = mock_anki.make_model('Study Deck - Cloze', ['Text', 'Why', 'Image', 'Dosing', 'Notes'],
                                 qfmt='{{cloze:Text}}', afmt='{{cloze:Text}}')
    anki.col.models._models.append(cloze)
    original = anki.col.add_note(
        'g1', _fields('Question', back='protected answer'), [TAGS], deck=DECK)
    original_nid = original.id
    original_cid = original.card_ids()[0]
    card = anki.col.get_card(original_cid)
    card.reps, card.ivl, card.due = 7, 23, 101
    folder = _write_source(tmp_path, {DECK: ('v2', [
        ('g1', ['{{c1::new}}', '', '', '', ''], TAGS)], cloze)})
    _configure(anki, folder)
    cfg = sync._cfg()
    cfg['protected'] = ['Back']
    manifest, fetch, _ = sync._fetch_manifest(cfg)
    anki.gui.answers.append(False)

    results = sync._run_sync(cfg, manifest, fetch, manifest['decks'])

    assert results[0][0].startswith('✓')
    assert anki.col.notetype_changes == []
    consent = anki.gui.asks[-1]
    assert 'local copies' in consent and 'review history' in consent
    assert 'stop receiving updates from this source' in consent
    local = anki.col.get_note(original_nid)
    assert ai_logic.is_generated_guid(local.guid)
    assert local.note_type()['name'] == 'Study Deck - Basic'
    assert local['Back'] == 'protected answer'
    assert local.card_ids() == [original_cid]
    kept = anki.col.get_card(original_cid)
    assert (kept.reps, kept.ivl, kept.due) == (7, 23, 101)

    incoming = anki.col.note_by_guid('g1')
    incoming_nid = incoming.id
    assert incoming_nid != original_nid
    assert incoming.note_type()['name'] == 'Study Deck - Cloze'
    assert incoming['Text'] == '{{c1::new}}'
    assert len(anki.col.find_notes(f'"tag:{TAGS}"')) == 2

    folder = _write_source(tmp_path, {DECK: ('v3', [
        ('g1', ['A reworded {{c1::fact}}', '', '', '', ''], TAGS)], cloze)})
    _configure(anki, folder)
    manifest, fetch, _ = sync._fetch_manifest(cfg)
    asks_before = len(anki.gui.asks)

    results = sync._run_sync(cfg, manifest, fetch, manifest['decks'])

    assert results[0][0].startswith('✓')
    assert len(anki.gui.asks) == asks_before
    assert len(anki.col.find_notes(f'"tag:{TAGS}"')) == 2
    assert anki.col.note_by_guid('g1').id == incoming_nid
    assert anki.col.note_by_guid('g1')['Text'] == 'A reworded {{c1::fact}}'
    assert anki.col.get_note(original_nid)['Back'] == 'protected answer'


def test_single_import_declining_unsafe_conversion_still_imports_as_new(anki, tmp_path):
    from internpearls import ai_logic, sync
    cloze = mock_anki.make_model('Study Deck - Cloze', ['Text', 'Why', 'Image', 'Dosing', 'Notes'],
                                 qfmt='{{cloze:Text}}', afmt='{{cloze:Text}}')
    anki.col.models._models.append(cloze)
    original = anki.col.add_note(
        'g1', _fields('Question', back='protected answer'), [TAGS], deck=DECK)
    original_nid = original.id
    original_cid = original.card_ids()[0]
    card = anki.col.get_card(original_cid)
    card.reps, card.ivl, card.due = 4, 17, 88
    folder = _write_source(tmp_path, {DECK: ('v2', [
        ('g1', ['{{c1::new}}', '', '', '', ''], TAGS)], cloze)})
    _configure(anki, folder)
    anki.mw._config['protected_fields'] = ['Back']
    manifest, fetch, _ = sync._fetch_manifest(sync._cfg())
    anki.gui.file_picks.append(fetch(manifest['decks'][0]))
    anki.gui.answers.extend([True, False])

    sync.import_single()

    assert anki.col.notetype_changes == []
    local = anki.col.get_note(original_nid)
    assert ai_logic.is_generated_guid(local.guid)
    assert local.note_type()['name'] == 'Study Deck - Basic'
    assert local['Back'] == 'protected answer'
    assert local.card_ids() == [original_cid]
    kept = anki.col.get_card(original_cid)
    assert (kept.reps, kept.ivl, kept.due) == (4, 17, 88)
    incoming = anki.col.note_by_guid('g1')
    assert incoming.id != original_nid
    assert incoming.note_type()['name'] == 'Study Deck - Cloze'
    assert incoming['Text'] == '{{c1::new}}'


def test_explicit_import_as_new_forks_alias_match(anki, tmp_path):
    from internpearls import ai_logic, sync
    cloze = mock_anki.make_model('Study Deck - Cloze', ['Text', 'Why', 'Image', 'Dosing', 'Notes'],
                                 qfmt='{{cloze:Text}}', afmt='{{cloze:Text}}')
    anki.col.models._models.append(cloze)
    original = anki.col.add_note(
        'legacy-guid', _fields('Old wording', back='mine'), [TAGS], deck=DECK)
    folder = _write_source(tmp_path, {DECK: ('v2', [
        ('source-guid', ['New {{c1::wording}}', '', '', '', ''], TAGS)], cloze)})
    manifest_path = Path(folder) / 'manifest.json'
    import json
    manifest_data = json.loads(manifest_path.read_text(encoding='utf8'))
    manifest_data['front_aliases'] = {'New {{c1::wording}}': 'Old wording'}
    manifest_path.write_text(json.dumps(manifest_data), encoding='utf8')
    _configure(anki, folder)
    cfg = sync._cfg()
    manifest, fetch, _ = sync._fetch_manifest(cfg)

    results = sync._run_sync(cfg, manifest, fetch, manifest['decks'],
                             convert_notetypes=False)

    local = anki.col.get_note(original.id)
    assert results[0][0].startswith('✓')
    assert ai_logic.is_generated_guid(local.guid)
    assert local['Back'] == 'mine'
    incoming = anki.col.note_by_guid('source-guid')
    assert incoming.id != original.id
    assert incoming.note_type()['name'] == 'Study Deck - Cloze'


def test_failed_import_rolls_back_local_identity_fork(anki, tmp_path, monkeypatch):
    from internpearls import collection, sync
    cloze = mock_anki.make_model('Study Deck - Cloze', ['Text', 'Why', 'Image', 'Dosing', 'Notes'],
                                 qfmt='{{cloze:Text}}', afmt='{{cloze:Text}}')
    anki.col.models._models.append(cloze)
    original = anki.col.add_note(
        'g1', _fields('Question', back='mine'), [TAGS], deck=DECK)
    original_cid = original.card_ids()[0]
    card = anki.col.get_card(original_cid)
    card.reps, card.ivl, card.due = 5, 19, 73
    folder = _write_source(tmp_path, {DECK: ('v2', [
        ('g1', ['A {{c1::fact}}', '', '', '', ''], TAGS)], cloze)})
    _configure(anki, folder)
    cfg = sync._cfg()
    manifest, fetch, _ = sync._fetch_manifest(cfg)
    anki.gui.answers.append(False)
    monkeypatch.setattr(collection, '_import_apkg',
                        lambda _path: (_ for _ in ()).throw(RuntimeError('failed')))

    results = sync._run_sync(cfg, manifest, fetch, manifest['decks'])

    restored = anki.col.get_note(original.id)
    assert results[0][0].startswith('✗')
    assert restored.guid == 'g1'
    assert restored.note_type()['name'] == 'Study Deck - Basic'
    assert restored['Back'] == 'mine'
    assert restored.card_ids() == [original_cid]
    kept = anki.col.get_card(original_cid)
    assert (kept.reps, kept.ivl, kept.due) == (5, 19, 73)
    assert len(anki.col.find_notes(f'"tag:{TAGS}"')) == 1


def test_failed_fork_does_not_poison_next_same_guid_deck(anki, tmp_path,
                                                          monkeypatch):
    from internpearls import collection, sync
    cloze = mock_anki.make_model('Study Deck - Cloze', ['Text', 'Why', 'Image', 'Dosing', 'Notes'],
                                 qfmt='{{cloze:Text}}', afmt='{{cloze:Text}}')
    anki.col.models._models.append(cloze)
    original = anki.col.add_note(
        'g1', _fields('Question', notes='my annotation'), [TAGS], deck=DECK)
    other = 'Intern Pearls::Intern Custom::Other'
    folder = _write_source(tmp_path, {
        DECK: ('v2', [('g1', ['A {{c1::fact}}', '', '', '', ''], TAGS)], cloze),
        other: ('v2', [('g1', _fields('Question revised'), TAGS)], None),
    })
    _configure(anki, folder)
    cfg = sync._cfg()
    manifest, fetch, _ = sync._fetch_manifest(cfg)
    real_import = collection._import_apkg
    calls = 0

    def fail_first(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError('first import failed')
        return real_import(path)

    monkeypatch.setattr(collection, '_import_apkg', fail_first)
    anki.gui.answers.append(False)

    results = sync._run_sync(cfg, manifest, fetch, manifest['decks'])

    assert results[0][0].startswith('✗')
    assert results[0][1].startswith('✓')
    note = anki.col.get_note(original.id)
    assert note.guid == 'g1'
    assert note['Front'] == 'Question revised'
    assert note['Notes'] == 'my annotation'


def test_successful_fork_restores_annotation_overwritten_by_prior_deck(anki,
                                                                        tmp_path):
    from internpearls import ai_logic, sync
    cloze = mock_anki.make_model('Study Deck - Cloze', ['Text', 'Why', 'Image', 'Dosing', 'Notes'],
                                 qfmt='{{cloze:Text}}', afmt='{{cloze:Text}}')
    anki.col.models._models.append(cloze)
    original = anki.col.add_note(
        'g1', _fields('Question', notes='my annotation'), [TAGS], deck=DECK)
    other = 'Intern Pearls::Intern Custom::Other'
    folder = _write_source(tmp_path, {
        DECK: ('v2', [('g1', _fields('Question revised', notes='source text'), TAGS)], None),
        other: ('v2', [('g1', ['A {{c1::fact}}', '', '', '', ''], TAGS)], cloze),
    })
    _configure(anki, folder)
    cfg = sync._cfg()
    manifest, fetch, _ = sync._fetch_manifest(cfg)
    anki.gui.answers.append(False)

    results = sync._run_sync(cfg, manifest, fetch, manifest['decks'])

    assert all(row.startswith('✓') for row in results[0])
    local = anki.col.get_note(original.id)
    assert ai_logic.is_generated_guid(local.guid)
    assert local.note_type()['name'] == 'Study Deck - Basic'
    assert local['Front'] == 'Question revised'
    assert local['Notes'] == 'my annotation'
    assert anki.col.note_by_guid('g1').note_type()['name'] == 'Study Deck - Cloze'


def test_personalized_rewrite_failure_removes_scratch_file(anki, tmp_path,
                                                            monkeypatch):
    import os
    from internpearls import collection
    cfg, manifest, fetch = source(anki, tmp_path)
    src = fetch(manifest['decks'][0])
    scratch = tmp_path / 'rewrite-failure.sync.apkg'
    fd = os.open(scratch, os.O_CREAT | os.O_RDWR)
    monkeypatch.setattr(collection.tempfile, 'mkstemp',
                        lambda suffix: (fd, str(scratch)))
    monkeypatch.setattr(collection, 'write_personalized',
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            RuntimeError('rewrite failed')))

    with pytest.raises(RuntimeError, match='rewrite failed'):
        collection._apply_deck(src, {}, collection._her_front_to_guid(cfg['scope_tag']))

    assert not scratch.exists()


def test_missing_target_allows_only_the_planned_conflicting_row(anki, tmp_path,
                                                                 monkeypatch):
    from types import SimpleNamespace
    from internpearls import collection, logic, sync
    cloze = mock_anki.make_model('Study Deck - Cloze', ['Text', 'Why', 'Image', 'Dosing', 'Notes'],
                                 qfmt='{{cloze:Text}}', afmt='{{cloze:Text}}')
    original = anki.col.add_note(
        'g1', _fields('Question', back='mine'), [TAGS], deck=DECK)
    folder = _write_source(tmp_path, {DECK: ('v2', [
        ('g1', ['A {{c1::fact}}', '', '', '', ''], TAGS)], cloze)})
    _configure(anki, folder)
    cfg = sync._cfg()
    manifest, fetch, _ = sync._fetch_manifest(cfg)
    real_import = collection._import_apkg
    first = True

    def import_with_real_conflict(path):
        nonlocal first
        if not first:
            return real_import(path)
        first = False
        anki.col.models._models.append(cloze)
        (rid, _fields_in, _guid), = logic.apkg_notes(path)
        empty = []
        return SimpleNamespace(log=SimpleNamespace(
            conflicting=[SimpleNamespace(id=SimpleNamespace(nid=rid))],
            missing_notetype=empty, missing_deck=empty, empty_first_field=empty,
            new=empty, updated=empty, duplicate=empty))

    monkeypatch.setattr(collection, '_import_apkg', import_with_real_conflict)
    first_result = sync._run_sync(cfg, manifest, fetch, manifest['decks'])

    assert first_result[0][0].startswith('✓')
    assert sync._load_json(sync.INSTALLED, {}) == {}
    assert anki.col.get_note(original.id).guid == 'g1'
    assert anki.col.get_note(original.id)['Back'] == 'mine'

    second_result = sync._run_sync(cfg, manifest, fetch, manifest['decks'],
                                   convert_notetypes=True)
    assert second_result[0][0].startswith('✓')
    assert anki.col.get_note(original.id).note_type()['name'] == 'Study Deck - Cloze'


def test_missing_target_rejects_an_unplanned_conflicting_row(anki, tmp_path,
                                                              monkeypatch):
    from types import SimpleNamespace
    from internpearls import collection, logic, sync
    cloze = mock_anki.make_model('Study Deck - Cloze', ['Text', 'Why', 'Image', 'Dosing', 'Notes'],
                                 qfmt='{{cloze:Text}}', afmt='{{cloze:Text}}')
    anki.col.add_note('g1', _fields('Question'), [TAGS], deck=DECK)
    folder = _write_source(tmp_path, {DECK: ('v2', [
        ('g1', ['A {{c1::fact}}', '', '', '', ''], TAGS)], cloze)})
    _configure(anki, folder)
    cfg = sync._cfg()
    manifest, fetch, _ = sync._fetch_manifest(cfg)

    def import_with_extra_conflict(path):
        anki.col.models._models.append(cloze)
        (rid, _fields_in, _guid), = logic.apkg_notes(path)
        empty = []
        return SimpleNamespace(log=SimpleNamespace(
            conflicting=[SimpleNamespace(id=SimpleNamespace(nid=rid)),
                         SimpleNamespace(id=SimpleNamespace(nid=rid + 1))],
            missing_notetype=empty, missing_deck=empty, empty_first_field=empty,
            new=empty, updated=empty, duplicate=empty))

    monkeypatch.setattr(collection, '_import_apkg', import_with_extra_conflict)
    results = sync._run_sync(cfg, manifest, fetch, manifest['decks'])

    assert results[0][0].startswith('✗')
    assert sync._load_json(sync.INSTALLED, {}) == {}


def test_progress_callback_error_after_import_still_restores_snapshot(anki, tmp_path):
    from internpearls import sync
    other = 'Intern Pearls::Intern Custom::Other'
    anki.col.add_note('g1', _fields('One', notes='my note'), [TAGS], deck=DECK)
    folder = _write_source(tmp_path, {
        DECK: ('v2', [('g1', _fields('One revised'), TAGS)], None),
        other: ('v2', [('g2', _fields('Two'), TAGS)], None),
    })
    _configure(anki, folder)
    cfg = sync._cfg()
    manifest, fetch, _ = sync._fetch_manifest(cfg)

    def progress(i, _total, _name):
        if i == 2:
            raise RuntimeError('progress callback failed')
        return True

    results = sync._run_sync(cfg, manifest, fetch, manifest['decks'],
                             on_progress=progress)

    assert results[0][0].startswith('✓')
    assert results[0][1].startswith('✗')
    assert anki.col.note_by_guid('g1')['Notes'] == 'my note'
    assert anki.col.imports
