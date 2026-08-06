# Phase 1 smoke test (on the Pi)

1. `sudo cp packaging/midicrtd.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now midicrtd`
2. `midicrt status` (venv bin on PATH or full path) — expect JSON with page=eventlog.
3. Feed real MIDI: play the rack, or from mothership with the aseqnet rig up:
   `aplaymidi -p PiVisualizer:0 <file.mid>`; or locally
   `~/midicrt2-venv/bin/python -c "import mido; p=mido.open_output('Midi Through:Midi Through Port-0'); p.send(mido.Message('note_on', note=60))"`
   (Midi Through is matched by the default `["*"]` source pattern.)
4. `midicrt status` again — events_total increased.
5. `midicrt tui` in an SSH session — see events arrive live; `c` clears; `q` quits.
6. `journalctl -u midicrtd -n 20` — startup lines, no errors.
