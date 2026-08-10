"""TDD for engine/midi_identity.py (Phase 9 Task 1, docs/phase4-bindings.md
+ docs/phase5-capture.md §7's own "identical-device collision" follow-up):
`IdentityResolver` resolves an ALSA-enumerated MIDI source name to a stable
`device_id` -- `usb:<vendor>:<product>[:<serial>]` for a USB-backed ALSA
card, `virt:<name>` for everything else. Every test here fakes all three
I/O seams (`run_aconnect`/`read_text`/`read_link`) -- no real subprocess or
filesystem access, matching this task's own "TDD (fake sysfs/identity
provider)" requirement; the module's own docstring documents the real
shapes these fakes are modeled on (live-probed on the Pi this ships to).
"""
import os

import pytest

from midicrt.engine.midi_identity import IdentityResolver

# One shared aconnect -l transcript modeling this Pi's REAL live topology
# (see midi_in.py/bindings.py's own module docstrings for the same real
# names) PLUS two hypothetical USB MIDI interfaces (never physically
# attached to this Pi -- no such hardware exists here today) standing in
# for the `usb:` rung of the ladder, which is therefore verified entirely
# by fakes, disclosed in the task-1 report.
ACONNECT_TRANSCRIPT = """\
client 0: 'System' [type=kernel]
    0 'Timer           '
client 14: 'Midi Through' [type=kernel]
    0 'Midi Through Port-0'
    1 'Midi Through Port-1'
client 20: 'USB Midi' [type=kernel,card=2]
    0 'USB Midi MIDI 1 '
client 21: 'USB Midi' [type=kernel,card=3]
    0 'USB Midi MIDI 1 '
client 22: 'bcm2835 Headphones' [type=kernel,card=0]
    0 'bcm2835 Headphones MIDI 1'
client 130: 'pivisualizer' [type=user,pid=339032]
    0 'Network Export  '
"""


def _fake_resolver(sysfs: dict, aconnect: str = ACONNECT_TRANSCRIPT) -> IdentityResolver:
    """`sysfs` is a flat {path: contents} dict standing in for both
    `/proc` (usbid files) and `/sys` (the card->device symlink target plus
    idVendor/serial files) -- a real `os.path.dirname`/string-keyed lookup
    is enough to model the sysfs walk `_find_serial` does without a real
    filesystem at all."""
    def read_text(path):
        return sysfs.get(path)

    def read_link(path):
        return sysfs.get(path)  # symlink targets stored under their own link path

    def run_aconnect():
        return aconnect

    return IdentityResolver(run_aconnect=run_aconnect, read_text=read_text, read_link=read_link)


def _usb_sysfs(card: int, vendor: str, product: str, device_path: str,
               serial: str | None = None) -> dict:
    """Models the real live-probed shape: `/sys/class/sound/cardN/device`
    is a symlink to the USB INTERFACE node, a real subdirectory NESTED
    inside the USB DEVICE node (`device_path`) -- e.g. `.../1-1.4/
    1-1.4:1.0` -- so `os.path.dirname(interface_dir) == device_path`
    exactly like the real sysfs tree, not a string-suffix trick."""
    interface_dir = os.path.join(device_path, os.path.basename(device_path) + ":1.0")
    sysfs = {
        f"/proc/asound/card{card}/usbid": f"{vendor}:{product}",
        f"/sys/class/sound/card{card}/device": interface_dir,
        f"{device_path}/idVendor": vendor,
        f"{device_path}/idProduct": product,
    }
    if serial is not None:
        sysfs[f"{device_path}/serial"] = serial
    return sysfs


# -- usb: rung ----------------------------------------------------------------

def test_usb_device_with_vendor_product_and_serial():
    sysfs = _usb_sysfs(2, "0d8c", "0014", "/sys/devices/.../1-1.4", serial="ABC123")
    r = _fake_resolver(sysfs)
    assert r.resolve("USB Midi:USB Midi MIDI 1 20:0") == "usb:0d8c:0014:ABC123"


def test_usb_device_vendor_product_lowercased_regardless_of_usbid_case():
    sysfs = _usb_sysfs(2, "0D8C", "0014", "/sys/devices/.../1-1.4", serial="ABC123")
    r = _fake_resolver(sysfs)
    assert r.resolve("USB Midi:USB Midi MIDI 1 20:0") == "usb:0d8c:0014:ABC123"


def test_usb_device_with_no_serial_file_falls_back_to_vendor_product_only():
    """The documented, still-colliding case (docs/phase5-capture.md §7) --
    live-probed on this Pi's actual C-Media USB Audio Device, which has no
    'serial' file at its device node at all."""
    sysfs = _usb_sysfs(2, "0d8c", "0014", "/sys/devices/.../1-1.4", serial=None)
    r = _fake_resolver(sysfs)
    assert r.resolve("USB Midi:USB Midi MIDI 1 20:0") == "usb:0d8c:0014"


def test_serial_lookup_never_walks_past_the_immediate_usb_device_node():
    """Regression, live-reproduced during this task's own Pi probing: one
    level further up the bus tree from a real USB device (a hub, or the
    root host controller) can ALSO have its own unrelated 'serial' file
    (on this Pi: the DWC OTG root hub's platform bus address,
    '3f980000.usb') -- picking that up would misattribute the HOST's own
    identifier to the plugged-in device. The sanity guard (re-checking
    'idVendor' at the exact device directory) must refuse to use a serial
    planted only at an ancestor directory."""
    device_path = "/sys/devices/soc/usb/usb1/1-1/1-1.4"
    ancestor_path = "/sys/devices/soc/usb/usb1"
    sysfs = _usb_sysfs(2, "0d8c", "0014", device_path, serial=None)
    # Simulate the real live gotcha: an ANCESTOR two levels up has its own
    # serial + idVendor, but the immediate device node (one level up from
    # the interface) does not.
    sysfs[f"{ancestor_path}/serial"] = "3f980000.usb"
    sysfs[f"{ancestor_path}/idVendor"] = "1d6b"
    r = _fake_resolver(sysfs)
    assert r.resolve("USB Midi:USB Midi MIDI 1 20:0") == "usb:0d8c:0014"


def test_no_usbid_file_falls_back_to_virt_for_a_non_usb_kernel_card():
    """A real, card-backed kernel client that ISN'T USB (e.g. this Pi's
    onboard 'bcm2835 Headphones', card 0 -- not MIDI-capable in practice,
    but nothing here assumes that) has no /proc/asound/cardN/usbid file at
    all -- falls all the way back to virt:, same as a virtual client."""
    r = _fake_resolver(sysfs={})   # no usbid file for card 0 anywhere
    device_id = r.resolve("bcm2835 Headphones:bcm2835 Headphones MIDI 1 22:0")
    assert device_id == "virt:bcm2835 Headphones:bcm2835 Headphones MIDI 1"


def test_malformed_usbid_contents_falls_back_to_virt():
    sysfs = {"/proc/asound/card2/usbid": "not-a-usbid\n"}
    r = _fake_resolver(sysfs)
    device_id = r.resolve("USB Midi:USB Midi MIDI 1 20:0")
    assert device_id == "virt:USB Midi:USB Midi MIDI 1"


# -- same-device-different-port / disambiguation -----------------------------

def test_same_device_different_port_resolves_to_the_same_device_id():
    """The headline capability: the SAME physical device, replugged into a
    different USB port (a different ALSA client number, card number, and
    even a totally different port-name suffix), must still resolve to the
    identical device_id as long as vendor/product/serial are unchanged."""
    sysfs_a = _usb_sysfs(2, "1234", "5678", "/sys/devices/.../1-1.2", serial="SN1")
    sysfs_b = _usb_sysfs(3, "1234", "5678", "/sys/devices/.../1-1.4", serial="SN1")
    r_a = _fake_resolver(sysfs_a)
    r_b = _fake_resolver(sysfs_b)
    id_a = r_a.resolve("USB Midi:USB Midi MIDI 1 20:0")
    id_b = r_b.resolve("USB Midi:USB Midi MIDI 1 21:0")
    assert id_a == id_b == "usb:1234:5678:SN1"


def test_distinct_serials_disambiguate_two_identical_model_devices():
    sysfs_a = _usb_sysfs(2, "1234", "5678", "/sys/devices/.../1-1.2", serial="SN1")
    sysfs_b = _usb_sysfs(3, "1234", "5678", "/sys/devices/.../1-1.4", serial="SN2")
    id_a = _fake_resolver(sysfs_a).resolve("USB Midi:USB Midi MIDI 1 20:0")
    id_b = _fake_resolver(sysfs_b).resolve("USB Midi:USB Midi MIDI 1 21:0")
    assert id_a != id_b
    assert id_a == "usb:1234:5678:SN1"
    assert id_b == "usb:1234:5678:SN2"


def test_no_serial_same_model_still_collides_documented_honestly():
    """Pinned, not silently fixed: two simultaneously-connected units of
    the exact same serial-less model are indistinguishable by this ladder
    -- both resolve to the identical usb:vendor:product device_id."""
    sysfs_a = _usb_sysfs(2, "1234", "5678", "/sys/devices/.../1-1.2", serial=None)
    sysfs_b = _usb_sysfs(3, "1234", "5678", "/sys/devices/.../1-1.4", serial=None)
    id_a = _fake_resolver(sysfs_a).resolve("USB Midi:USB Midi MIDI 1 20:0")
    id_b = _fake_resolver(sysfs_b).resolve("USB Midi:USB Midi MIDI 1 21:0")
    assert id_a == id_b == "usb:1234:5678"


# -- virt: rung ----------------------------------------------------------------

def test_virtual_client_with_no_card_at_all_resolves_to_virt():
    r = _fake_resolver(sysfs={})
    assert (r.resolve("Midi Through:Midi Through Port-0 14:0")
            == "virt:Midi Through:Midi Through Port-0")


def test_virtual_client_different_ports_keep_distinct_virt_identities():
    """Mirrors glob_port_pattern's own per-port (not per-client) identity
    grain for virtual sources -- Port-0 and Port-1 of the same kernel
    'Midi Through' client are still different logical ports."""
    r = _fake_resolver(sysfs={})
    id0 = r.resolve("Midi Through:Midi Through Port-0 14:0")
    id1 = r.resolve("Midi Through:Midi Through Port-1 14:1")
    assert id0 != id1


def test_user_client_rtpmidid_resolves_to_virt():
    r = _fake_resolver(sysfs={})
    assert r.resolve("pivisualizer:Network Export 130:0") == "virt:pivisualizer:Network Export"


def test_client_not_present_in_aconnect_output_falls_back_to_virt():
    """The client number isn't in the (possibly stale, possibly
    partial-scan) aconnect transcript at all -- must not crash, falls back
    exactly like "no card"."""
    r = _fake_resolver(sysfs={}, aconnect="client 0: 'System' [type=kernel]\n")
    assert r.resolve("Ghost Client:Port 99:0") == "virt:Ghost Client:Port"


def test_source_with_no_alsa_suffix_resolves_to_virt_of_the_whole_string():
    r = _fake_resolver(sysfs={})
    assert r.resolve("Midi Through:0") == "virt:Midi Through:0"


def test_aconnect_raising_falls_back_to_virt_without_crashing():
    def boom():
        raise FileNotFoundError("aconnect: command not found")

    r = IdentityResolver(run_aconnect=boom, read_text=lambda p: None, read_link=lambda p: None)
    assert (r.resolve("USB Midi:USB Midi MIDI 1 20:0")
            == "virt:USB Midi:USB Midi MIDI 1")


@pytest.mark.parametrize("bad_line", [
    "client 20: 'USB Midi' [type=user,pid=123]\n",   # user client, no card at all
])
def test_aconnect_client_with_no_card_annotation_falls_back_to_virt(bad_line):
    r = _fake_resolver(sysfs={}, aconnect=bad_line)
    assert (r.resolve("USB Midi:USB Midi MIDI 1 20:0")
            == "virt:USB Midi:USB Midi MIDI 1")


# -- Minor review fix: an apostrophe embedded in a real client name --------

def test_aconnect_client_name_containing_an_apostrophe_still_resolves_the_card():
    """`aconnect -l` never escapes an apostrophe in a client's own name
    (verbatim `'%s'` quoting) -- a device genuinely named e.g. "Roland's
    SC-55" would break a naive `'[^']*'` character-class regex (stops at
    the FIRST `'`, landing mid-name with no `[type=...` right after, so
    the whole line fails to match and a real card-backed device silently
    falls back to virt: instead). The lazy `'.*?'` pattern must expand
    past the embedded apostrophe to reach the real closing one."""
    transcript = "client 24: 'Roland's SC-55' [type=kernel,card=2]\n    0 'SC-55 MIDI 1'\n"
    sysfs = _usb_sysfs(2, "0582", "0007", "/sys/devices/.../1-1.5", serial="SN99")
    r = _fake_resolver(sysfs, aconnect=transcript)
    assert r.resolve("Roland's SC-55:SC-55 MIDI 1 24:0") == "usb:0582:0007:SN99"
