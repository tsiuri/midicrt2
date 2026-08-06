from midicrt.clients.tui import render_lines

VM = {"title": "EVENT LOG", "count": 12,
      "lines": [{"text": f"line{i}", "style": "normal"} for i in range(10)]}


def test_render_geometry_and_content():
    out = render_lines(VM, width=30, height=5)
    assert len(out) == 5
    assert all(len(line) == 30 for line in out)
    assert out[0].startswith("EVENT LOG  (12 events)")
    assert out[-1].strip() == "line9"          # newest last
    assert out[1].strip() == "line6"           # only the tail fits


def test_render_truncates_long_lines():
    vm = {"title": "EVENT LOG", "count": 1,
          "lines": [{"text": "x" * 100, "style": "normal"}]}
    out = render_lines(vm, width=10, height=2)
    assert all(len(line) == 10 for line in out)


def test_render_empty():
    out = render_lines({"title": "EVENT LOG", "count": 0, "lines": []}, 20, 3)
    assert len(out) == 3 and out[2] == " " * 20
