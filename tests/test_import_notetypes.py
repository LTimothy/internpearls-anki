"""Package reconciliation uses synthetic note types and disposable archives."""
import json

import pytest
import mock_anki


def legacy_package(anki, tmp_path):
    local = mock_anki.make_model('Study Deck - Basic++',
                                 ['Front', 'Back', 'Notes', 'Personal'])
    anki.col.models._models.append(local)
    note = anki.col.add_note('local', ['Question', 'Old', 'Annotation', 'Keep'],
                             ['InternPearls'], model=local)
    incoming = mock_anki.make_model(fields=['Front', 'Source', 'Back', 'Notes'])
    src = str(tmp_path / 'source.apkg')
    mock_anki.make_apkg(src, [('incoming', ['Question', 'Reference', 'New', ''],
                              'InternPearls')], model=incoming)
    return src, note, local


def test_reconcile_maps_after_guid_remap_and_preserves_extra_fields(anki, tmp_path):
    from internpearls import collection, logic
    src, note, local = legacy_package(anki, tmp_path)
    out = str(tmp_path / 'aligned.apkg')
    logic.write_personalized(src, {1: 'local'}, out,
                             prepare_notetypes=collection._prepare_import_notetypes)
    with logic._apkg_db(out) as (db, _):
        mid, fields = db.execute('select mid, flds from notes').fetchone()
        assert mid == local['id']
        assert fields.split(logic.FS) == ['Question', 'New', '', 'Keep', 'Reference']
        models = json.loads(db.execute('select models from col').fetchone()[0])
        assert set(models) == {str(mid)}
        assert models[str(mid)]['tmpls'] == local['tmpls']
    assert note['Personal'] == 'Keep'
    assert note['Source'] == ''  # Preparing is not an import of note content.
    assert logic.apkg_notes(src)[0][2] == 'incoming'


def test_declined_rows_do_not_add_fields(anki, tmp_path):
    from internpearls import collection, logic
    src, note, _ = legacy_package(anki, tmp_path)
    logic.write_personalized(src, {1: 'local'}, str(tmp_path / 'dropped.apkg'),
                             drop={1}, prepare_notetypes=collection._prepare_import_notetypes)
    assert 'Source' not in note.keys()


def test_unattended_field_addition_is_deferred_before_schema_mutation(anki, tmp_path):
    from internpearls import collection, logic
    src, note, _ = legacy_package(anki, tmp_path)
    before = anki.col.scm
    with pytest.raises(collection.NoteTypeFieldsRequired, match='manually'):
        logic.write_personalized(src, {1: 'local'}, str(tmp_path / 'deferred.apkg'),
                                 prepare_notetypes=lambda db:
                                 collection._prepare_import_notetypes(db, False))
    assert anki.col.scm == before
    assert 'Source' not in note.keys()


def test_preparation_failure_closes_scratch_database(anki, tmp_path):
    import sqlite3
    from internpearls import logic
    src, _, _ = legacy_package(anki, tmp_path)
    connections = []
    def fail(db):
        connections.append(db)
        raise ValueError('Cannot reconcile')
    with pytest.raises(ValueError, match='Cannot reconcile'):
        logic.write_personalized(src, {}, str(tmp_path / 'failed.apkg'),
                                 prepare_notetypes=fail)
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connections[0].execute('select 1')


def test_unconverted_row_keeps_its_original_schema_on_model_id_collision(anki, tmp_path):
    from internpearls import collection, logic
    src, note, local = legacy_package(anki, tmp_path)
    incoming = mock_anki.make_model(fields=['Front', 'Source', 'Back', 'Notes'])
    incoming['id'] = local['id']
    other = mock_anki.make_model('Study Deck - Cloze', ['Text', 'Notes'])
    anki.col.models._models.append(other)
    anki.col.add_note('different-family', ['{{c1::Fact}}', 'Annotation'],
                       ['InternPearls'], model=other)
    mock_anki.make_apkg(src, [
        ('local', ['Question', 'Reference', 'New', ''], 'InternPearls'),
        ('different-family', ['Question two', 'Reference two', 'Answer', ''], 'InternPearls'),
    ], model=incoming)
    out = str(tmp_path / 'collision.apkg')
    logic.write_personalized(src, {}, out,
                             prepare_notetypes=collection._prepare_import_notetypes)
    with logic._apkg_db(out) as (db, _):
        models = json.loads(db.execute('select models from col').fetchone()[0])
        for guid, mid, fields in db.execute('select guid, mid, flds from notes'):
            assert len(models[str(mid)]['flds']) == len(fields.split(logic.FS))
            if guid == 'different-family':
                assert mid != local['id']
                assert models[str(mid)]['name'] == incoming['name']
