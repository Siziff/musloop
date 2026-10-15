## MusloopPlayer.gd — adaptive music from a musloop loops export (Godot 4)
##
## Reads manifest.json written by musloop ("Files -> Loops (zip)"), loops one
## section at a time and switches sections on musical boundaries:
##   NATURAL — at the end of the current loop pass
##   SOON    — at the next transition point (phrase boundary) inside the loop
##   NOW     — immediately, with a short crossfade
## Two AudioStreamPlayers (A/B) are alternated so that the outgoing section can
## crossfade / ring out while the incoming one starts.
class_name MusloopPlayer
extends Node

enum Cue { NATURAL, SOON, NOW }

@export_file("*.json") var manifest_path: String = ""
## Crossfade for NOW cues and for section changes when tails are missing.
@export_range(0.0, 2.0, 0.05) var crossfade_sec: float = 0.25
## Play *_tail.wav over the next section's start (post-exit tail).
@export var use_tails: bool = true
@export var bus: StringName = &"Master"

signal section_changed(index: int, name: String)
signal cue_queued(index: int)
signal cue_cancelled()

var manifest: Dictionary = {}
var loops: Array = []
var scenes: Array = []

var _a: AudioStreamPlayer
var _b: AudioStreamPlayer
var _tail: AudioStreamPlayer
var _cur: AudioStreamPlayer          # the player that carries the current section
var _streams: Dictionary = {}        # file -> AudioStream (cached)

var current: int = -1                # index into loops
var _pending: int = -1               # queued target (-1 = none)
var _pending_at: float = -1.0        # position (sec) inside the current loop to switch at, -1 = loop end
var _pass: int = 0
var _fading_out: bool = false


func _ready() -> void:
	_a = _make_player()
	_b = _make_player()
	_tail = _make_player()
	_cur = _a
	if manifest_path != "":
		load_manifest(manifest_path)


func _make_player() -> AudioStreamPlayer:
	var p := AudioStreamPlayer.new()
	p.bus = bus
	add_child(p)
	return p


## Load manifest.json; WAVs are resolved relative to it.
func load_manifest(path: String) -> bool:
	var f := FileAccess.open(path, FileAccess.READ)
	if f == null:
		push_error("musloop: cannot open %s" % path)
		return false
	var data = JSON.parse_string(f.get_as_text())
	if typeof(data) != TYPE_DICTIONARY or not data.has("loops"):
		push_error("musloop: not a musloop-loops manifest: %s" % path)
		return false
	manifest = data
	loops = data["loops"]
	scenes = data.get("scenes", [])
	var dir := path.get_base_dir()
	for l in loops:
		l["_stream"] = _load_wav(dir.path_join(l["file"]))
		if l.has("tail_file"):
			l["_tail"] = _load_wav(dir.path_join(l["tail_file"]))
	return true


func _load_wav(p: String) -> AudioStream:
	if _streams.has(p):
		return _streams[p]
	var s: AudioStream = load(p)
	if s == null:
		push_warning("musloop: missing %s" % p)
	elif s is AudioStreamWAV:
		# we loop by re-triggering, so the imported loop mode must be off
		(s as AudioStreamWAV).loop_mode = AudioStreamWAV.LOOP_DISABLED
	_streams[p] = s
	return s


## Start looping section `i` right away (stops whatever plays).
func play_loop(i: int) -> void:
	if i < 0 or i >= loops.size():
		return
	_a.stop(); _b.stop(); _tail.stop()
	_fading_out = false
	_a.volume_db = 0.0
	_b.volume_db = 0.0
	_cur = _a
	_pending = -1
	_pass = 0
	_start_section(i, _cur, 0.0)


## Queue a switch to section `i`.
func cue(i: int, mode: Cue = Cue.NATURAL) -> void:
	if i < 0 or i >= loops.size():
		return
	if current < 0:
		play_loop(i)
		return
	if i == current:
		cancel_cue()
		return
	match mode:
		Cue.NOW:
			_switch_to(i, crossfade_sec)
		Cue.SOON:
			var pos := _cur.get_playback_position()
			var pts: Array = loops[current].get("transition_points_sec", [])
			var at := -1.0
			for p in pts:
				if float(p) > pos + 0.15:
					at = float(p)
					break
			_pending = i
			_pending_at = at   # -1 -> falls back to the loop end
			cue_queued.emit(i)
		_:
			_pending = i
			_pending_at = -1.0
			cue_queued.emit(i)


## Use a scene saved in musloop (by name): its loop and its cue mode.
func cue_scene(name: String) -> void:
	for sc in scenes:
		if sc.get("name", "") == name:
			var m := Cue.NATURAL
			match sc.get("cue", "natural"):
				"soon": m = Cue.SOON
				"now": m = Cue.NOW
			cue(int(sc.get("loop_index", 0)), m)
			return
	push_warning("musloop: scene '%s' not found" % name)


func cancel_cue() -> void:
	if _pending >= 0:
		_pending = -1
		cue_cancelled.emit()


func fade_out(sec: float = 2.0) -> void:
	_fading_out = true
	var tw := create_tween()
	tw.tween_property(_cur, "volume_db", -60.0, sec)
	tw.tween_callback(func():
		_a.stop(); _b.stop(); _tail.stop()
		current = -1
		_pending = -1
	)


func _process(_dt: float) -> void:
	if current < 0 or _fading_out or not _cur.playing and _cur.stream == null:
		return
	var l: Dictionary = loops[current]
	var pos := _cur.get_playback_position()
	var length := float(l["duration_sec"])
	var remaining := length - pos

	# SOON: switch at the chosen phrase boundary
	if _pending >= 0 and _pending_at >= 0.0 and pos >= _pending_at - 0.02:
		_switch_to(_pending, maxf(crossfade_sec, 0.03))
		return

	# loop end (or a one-shot build-up finished): re-trigger or move on
	if remaining <= 0.03 or not _cur.playing:
		if _pending >= 0:
			_switch_to(_pending, 0.0)
		elif l.get("loop", true):
			_pass += 1
			var from := float(l.get("loop_start_sample", 0)) / float(l.get("sample_rate", 44100))
			_cur.play(from)
		else:
			# build-up plays once -> continue with the next section
			var nxt := mini(current + 1, loops.size() - 1)
			if nxt != current:
				_switch_to(nxt, 0.0)
			else:
				_cur.stop()
				current = -1


func _switch_to(i: int, xf: float) -> void:
	var old := _cur
	var new_p := _b if _cur == _a else _a
	var prev := current
	_pending = -1
	_pass = 0
	# post-exit tail of the outgoing section rings over the new start
	if use_tails and prev >= 0 and loops[prev].has("_tail") and loops[prev]["_tail"] != null:
		_tail.stream = loops[prev]["_tail"]
		_tail.volume_db = -1.0
		_tail.play()
	_start_section(i, new_p, xf)
	if xf > 0.0:
		var tw := create_tween()
		tw.tween_property(old, "volume_db", -40.0, xf)
		tw.tween_callback(old.stop)
	else:
		old.stop()
	old.volume_db = 0.0
	_cur = new_p


func _start_section(i: int, p: AudioStreamPlayer, fade_in: float) -> void:
	var l: Dictionary = loops[i]
	p.stream = l.get("_stream")
	if p.stream == null:
		return
	if fade_in > 0.0:
		p.volume_db = -40.0
		create_tween().tween_property(p, "volume_db", 0.0, fade_in)
	else:
		p.volume_db = 0.0
	p.play()
	current = i
	section_changed.emit(i, str(l.get("name", "")))
