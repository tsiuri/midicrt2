import midicrt


def test_version():
    assert midicrt.__version__.startswith("2.")
