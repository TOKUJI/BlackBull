from blackbull.protocol.stream import Stream


def test_create():
    Stream(0)


def test_add_child():
    s = Stream(0)
    s.add_child(1)


def test_get_children():
    s = Stream(0)
    c = s.add_child(1)
    assert s.get_children() == [c]


def test_window_size_stored():
    s = Stream(1, window_size=65535)
    assert s.window_size == 65535


def test_drop_child():
    root = Stream(0)
    root.add_child(1)
    root.drop_child(1)
    assert 1 not in root.children


def test_find_child_recursive():
    root = Stream(0)
    child = root.add_child(1)
    grandchild = child.add_child(3)
    assert root.find_child(3) is grandchild


def test_repr_nonempty():
    s = Stream(5)
    assert repr(s) != ''
    assert '5' in repr(s)
