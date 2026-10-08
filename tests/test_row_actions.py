import pytest


@pytest.fixture
def dupes(anki, monkeypatch):
    from internpearls import ai_cli, dupes_dialog
    monkeypatch.setattr(ai_cli, 'find_cli', lambda *a, **kw: None)
    for i in range(4):
        text = f'topicword{i} mechanism{i} receptor{i} clearance{i} threshold{i}'
        anki.col.add_note(f'o{i}', ['Ours ' + text, 'answer'], ['InternPearls'], deck='Ours')
        anki.col.add_note(f't{i}', ['Theirs ' + text, 'answer'], ['Other'], deck='Theirs')
    dlg = dupes_dialog._DuplicateScanDialog('InternPearls')
    dlg._wait_for_backends()
    dlg._wait_for_scan()
    assert len(dlg._pairs) == 4
    return dlg


def _no_reset(monkeypatch, lst):
    resets = []
    original = lst.reset

    def reset(items):
        resets.append(items)
        return original(items)

    monkeypatch.setattr(lst, 'reset', reset)
    return resets


def test_suspend_and_unsuspend_replace_only_the_pair(dupes, monkeypatch):
    resets = _no_reset(monkeypatch, dupes._list)
    pair = dupes._pairs[1]
    old = dupes._list.rows()
    dupes._suspend(pair, 'left')
    assert not resets
    rows = dupes._list.rows()
    assert rows[2] is not old[2]
    assert all(rows[i] is old[i] for i in (0, 1, 3, 4, 5, 6))
    from test_dialogs import _all_text
    assert 'Unsuspend ours' in _all_text(rows[2].node())
    dupes._unsuspend(pair, 'left')
    assert not resets
    assert 'Suspend ours' in _all_text(dupes._list.rows()[2].node())


@pytest.mark.parametrize('index', [0, 1, 3])
def test_ignore_removes_pair_and_rule_without_reset(dupes, monkeypatch, index):
    resets = _no_reset(monkeypatch, dupes._list)
    pair = dupes._pairs[index]
    survivors = [row for i, row in enumerate(dupes._list.rows()) if i % 2 == 0 and i != 2 * index]
    dupes._ignore(pair)
    assert not resets
    assert dupes._list.rows()[::2] == survivors
    assert [i[0] for i in dupes._list._items] == ['pair', 'rule', 'pair', 'rule', 'pair']
    assert '3 candidates' in dupes.summary_label.text()


def test_suspension_in_open_different_group_still_replaces_one_pair(dupes, monkeypatch):
    pair = dupes._pairs[0]
    pair['judged'] = 'different'
    dupes._fold_open = True
    dupes._rebuild_list()
    resets = _no_reset(monkeypatch, dupes._list)
    old = dupes._list.rows()
    dupes._suspend(pair, 'right')
    assert not resets
    assert dupes._list.rows()[:-1] == old[:-1]
    assert dupes._list.rows()[-1] is not old[-1]


@pytest.mark.parametrize('guid', ['later', 'never', 'unknown', 'garbage'])
def test_offer_again_removes_entry_and_empty_heading_without_reset(anki, monkeypatch, guid):
    from internpearls import config, dialogs
    config.save_declined({'later': {'state': 'held', 'front': 'Alpha'},
                         'never': {'state': 'never', 'front': 'Beta'},
                         'unknown': {'state': 'future', 'front': 'Gamma'},
                         'garbage': 'not a dict'})
    dlg = dialogs._DeclinedDialog(None)
    resets = _no_reset(monkeypatch, dlg._list)
    old_rows = {i[1]: row for i, row in zip(dlg._list._items, dlg._list.rows()) if i[0] == 'row'}
    dlg._offer_again(guid)
    assert not resets
    assert guid not in config.load_declined()
    assert all(row is old_rows[i[1]] for i, row in zip(dlg._list._items, dlg._list.rows()) if i[0] == 'row')
    headings = [i[1] for i in dlg._list._items if i[0] == 'heading']
    assert headings == (['Never imported', 'Other'] if guid == 'later' else
                        ['Later', 'Other'] if guid == 'never' else
                        ['Later', 'Never imported', 'Other'])


def test_offer_again_leaves_an_empty_message_without_reset(anki, monkeypatch):
    from internpearls import config, dialogs
    config.save_declined({'last': {'state': 'held', 'front': 'Alpha'}})
    dlg = dialogs._DeclinedDialog(None)
    resets = _no_reset(monkeypatch, dlg._list)
    dlg._offer_again('last')
    assert not resets
    assert dlg._list._items == [('empty',)]
    from test_dialogs import _all_text
    assert "You haven't declined any cards." in _all_text(dlg._list.rows()[0].node())


def test_offer_again_keeps_a_filter_and_count_without_reset(anki, monkeypatch):
    from internpearls import config, dialogs
    config.save_declined({'a': {'state': 'held', 'front': 'Alpha'},
                         'b': {'state': 'held', 'front': 'Alpha two'},
                         'c': {'state': 'never', 'front': 'Beta'}})
    dlg = dialogs._DeclinedDialog(None)
    dlg._refilter('held', 'ALPHA')
    resets = _no_reset(monkeypatch, dlg._list)
    dlg._offer_again('a')
    assert not resets
    assert dlg._filter_mode == 'held' and dlg._filter_query == 'ALPHA'
    assert dlg._filter._count.text() == 'Showing 1 of 2 cards'
    dlg._offer_again('b')
    assert not resets
    assert dlg._list._items == []
    assert dlg._filter._count.text() == 'Showing 0 of 1 cards'


def test_ignore_a_different_pair_refreshes_its_fold_count(dupes, monkeypatch):
    pair = dupes._pairs[0]
    pair['judged'] = 'different'
    dupes._rebuild_list()
    resets = _no_reset(monkeypatch, dupes._list)
    dupes._ignore(pair)
    assert len(resets) == 1
    assert not any(i[0] == 'fold' for i in dupes._list._items)


def test_changed_grouping_falls_back_to_rebuild(dupes, monkeypatch):
    pair = dupes._pairs[0]
    pair['judged'] = 'different'
    resets = _no_reset(monkeypatch, dupes._list)
    dupes._suspend(pair, 'left')
    assert len(resets) == 1
    assert dupes._list._items[-1] == ('fold', 1)


@pytest.mark.parametrize('action', ['suspend', 'unsuspend', 'ignore'])
def test_action_on_an_unbuilt_pair_does_not_build_or_reset_other_rows(dupes, monkeypatch, action):
    pair = dupes._pairs[-1]
    if action == 'unsuspend':
        dupes._suspend(pair, 'left')
    dupes._list._batch = 2
    dupes._list.reset(dupes._list_items())
    resets = _no_reset(monkeypatch, dupes._list)
    old = dupes._list.rows()
    if action == 'ignore':
        dupes._ignore(pair)
    else:
        getattr(dupes, '_' + action)(pair, 'left')
    assert not resets
    assert dupes._list.rows() == old
    dupes._list.fill_all()
    assert len(dupes._list.rows()) == (5 if action == 'ignore' else 7)
    if action != 'ignore':
        from test_dialogs import _all_text
        expected = 'Unsuspend ours' if action == 'suspend' else 'Suspend ours'
        assert expected in _all_text(dupes._list.rows()[-1].node())


def test_offer_again_on_an_unbuilt_entry_keeps_the_built_rows(anki, monkeypatch):
    from internpearls import config, dialogs
    config.save_declined({str(i): {'state': 'held', 'front': f'Card {i}'} for i in range(60)})
    dlg = dialogs._DeclinedDialog(None)
    resets = _no_reset(monkeypatch, dlg._list)
    old = dlg._list.rows()
    dlg._offer_again('59')
    assert not resets
    assert dlg._list.rows() == old
    dlg._list.fill_all()
    assert [i[1] for i in dlg._list._items if i[0] == 'row'] == [str(i) for i in range(59)]
