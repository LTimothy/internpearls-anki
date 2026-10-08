import pytest


def _list():
    from internpearls import widgets
    calls = []

    def build(item):
        calls.append(item)
        row = widgets.QLabel(str(item))
        row.item = item
        return row

    return widgets.StreamingList(build, list(range(15)), batch=4), calls


def _consistent(lst):
    assert len(lst.rows()) == lst.built()
    assert sum(p.layout().count() for p in lst._pages if p.isVisible()) == lst.shown()
    assert all(0 < p.layout().count() <= 4 for p in lst._pages)
    assert 0 <= lst.shown() <= lst.built() <= lst.total()


@pytest.mark.parametrize('operation', ['remove', 'replace'])
def test_edit_near_the_bottom_reveals_more_rows(operation):
    from test_widgets import _grown
    lst, _ = _list()
    bar = lst.verticalScrollBar()
    bar.setMaximum(100)
    bar.setValue(70)
    _grown(lst, viewport_height=30, row_height=10)
    before = lst.shown()
    if operation == 'remove':
        lst.remove_item(0)
    else:
        lst.replace_item(0, 100)
    assert lst.shown() > before
    _consistent(lst)


def test_edit_refills_a_viewport_with_no_scroll_range():
    from test_widgets import _grown
    lst, _ = _list()
    _grown(lst, viewport_height=35, row_height=10)
    lst.remove_item(0)
    assert lst.shown() == 7
    _consistent(lst)


@pytest.mark.parametrize('index', [0, 3, 5, 12])
def test_replace_keeps_other_rows_and_later_builds_the_new_item(index):
    lst, calls = _list()
    lst._build_upto(7)
    old = lst.rows()
    calls.clear()
    lst.replace_item(index, 100)
    assert calls == ([100] if index < 7 else [])
    assert lst.shown() == 4 and lst.built() == 7 and lst.total() == 15
    assert all(row is old[i] for i, row in enumerate(lst.rows()) if i != index)
    _consistent(lst)
    lst.fill_all()
    assert [w.item for w in lst.rows()] == list(range(index)) + [100] + list(range(index + 1, 15))
    _consistent(lst)


@pytest.mark.parametrize('built', [4, 7, 15])
@pytest.mark.parametrize('index', [0, 3, 4, 6, 14])
def test_remove_keeps_rows_counts_and_pages_consistent(built, index):
    lst, calls = _list()
    lst._build_upto(built)
    old = lst.rows()
    calls.clear()
    lst.remove_item(index)
    assert calls == []
    assert lst.shown() == (3 if index < 4 else 4)
    assert lst.built() == built - (index < built)
    assert lst.total() == 14
    assert lst.rows() == [row for i, row in enumerate(old) if i != index]
    _consistent(lst)
    lst._extend()
    _consistent(lst)
    lst.fill_all()
    assert [w.item for w in lst.rows()] == [i for i in range(15) if i != index]
    _consistent(lst)


def test_removing_a_whole_page_and_all_rows_leaves_no_empty_pages():
    lst, _ = _list()
    lst._build_upto(7)
    for _ in range(4):
        lst.remove_item(0)
    assert lst.shown() == 0 and lst.built() == 3
    _consistent(lst)
    lst._extend()
    assert [w.item for w in lst.rows()][:4] == [4, 5, 6, 7]
    while lst.total():
        lst.remove_item(0)
    assert lst._pages == [] and lst.rows() == []
    assert lst.shown() == lst.built() == 0
    lst._extend()


@pytest.mark.parametrize('operation', ['replace', 'remove'])
def test_edits_invalidate_inflight_prefetch_and_allow_a_new_chunk(anki, operation):
    from internpearls.platform import use_platform
    from test_platform_callers import _DeferredPlatform, _Checkpoints
    native = _DeferredPlatform()
    with use_platform(native):
        lst, _ = _list()
        lst.isVisible = lambda: True
        lst._last_scroll = -1
        lst._idle_extend()
        compute, deliver = native.pending[0]
        if operation == 'replace':
            lst.replace_item(6, 100)
            expected = [0, 1, 2, 3, 4, 5, 100, 7, 8, 9, 10, 11, 12, 13, 14]
        else:
            lst.remove_item(1)
            expected = [0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
        lst._idle_extend()
        assert lst._prefetching
        deliver(compute(_Checkpoints()))
        assert lst.built() == (4 if operation == 'replace' else 3)
        assert lst._prefetching, 'an old delivery must not retire the new chunk'
        compute, deliver = native.pending[1]
        deliver(compute(_Checkpoints()))
        assert not lst._prefetching
        lst.fill_all()
        assert [w.item for w in lst.rows()] == expected
