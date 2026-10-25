import argparse
import base64
import hashlib
import json
import math
import os
import tempfile
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING or __package__:
    from .corpus import Row, integer, load_corpus, resolve_audio, text
else:
    from corpus import Row, integer, load_corpus, resolve_audio, text

MANUAL_FIELDS = {
    "last_voice_sample",
    "last_voice_annotation",
    "last_voice_annotator_note",
}
EXPORT_FIELDS = {
    "id",
    "audio_sha256",
    "row_sha256",
    "frames",
    "last_voice_sample",
    "transcript_differs",
    "transcript_note",
    "skip",
}


def row_digest(row: Row) -> str:
    protected = {key: value for key, value in row.items() if key not in MANUAL_FIELDS}
    return hashlib.sha256(
        json.dumps(
            protected, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def automatic_markers(row: Row) -> dict[str, int]:
    markers = {}
    aligned = row.get("aligned_end_sample")
    difference = row.get("energy_minus_aligned_samples")
    if integer(aligned):
        markers["aligned"] = aligned
        if integer(difference):
            markers["energy"] = aligned + difference
    if "energy" not in markers and row.get("last_voice_annotation") == "automatic":
        markers["energy"] = row["last_voice_sample"]
    note = row.get("last_voice_annotator_note")
    if "energy" not in markers and isinstance(note, dict):
        saved = note.get("automatic_energy_sample")
        if integer(saved):
            markers["energy"] = saved
    whisper = row.get("whisper_end_sample")
    if integer(whisper):
        markers["whisper"] = whisper
    return markers


def audio_info(row: Row, root: Path) -> tuple[bytes, int]:
    path = resolve_audio(root, row["audio_path"])
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != row.get("audio_sha256"):
        raise ValueError("audio hash mismatch")
    with wave.open(str(path), "rb") as wav:
        frames = wav.getnframes()
        if (
            wav.getframerate() != 16000
            or row.get("sample_rate") != 16000
            or wav.getnchannels() != 1
            or wav.getsampwidth() != 2
            or frames <= 0
            or len(wav.readframes(frames)) != frames * 2
        ):
            raise ValueError("expected complete 16 kHz mono 16-bit WAV")
    return data, frames


def select_rows(
    rows: list[Row], split: str | None, ids: str | None, flagged: bool
) -> list[Row]:
    identifiers = [row.get("id") for row in rows]
    if not all(text(value) for value in identifiers) or len(set(identifiers)) != len(
        rows
    ):
        raise ValueError("labels require unique ids")
    wanted = set(ids.split(",")) if ids is not None else None
    if wanted is not None and (not wanted or not wanted <= set(identifiers)):
        raise ValueError("unknown selection id")
    selected = [
        row
        for row in rows
        if (split is None or row.get("split") == split)
        and (wanted is None or row["id"] in wanted)
        and (not flagged or row.get("boundary_review_required") is True)
    ]
    if not selected:
        raise ValueError("empty selection")
    return selected


PAGE = r"""<!doctype html>
<html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';
script-src 'unsafe-inline'; style-src 'unsafe-inline';
media-src data:; connect-src 'none'">
<title>Corpus boundary annotation</title>
<style>
body{font:16px system-ui;margin:2em auto;max-width:1100px;padding:0 1em;
background:#fafafa;color:#222}
section{border:1px solid #777;padding:1em;margin:1em 0}
canvas{width:100%;height:130px;border:1px solid #777;touch-action:none}
button,input,textarea{font:inherit;margin:.3em}
textarea{width:90%;height:3em}button{padding:.4em}
.status{font-weight:bold} .warning{color:#922}label{display:block}
</style>
<h1>Corpus boundary annotation</h1>
<p>Local audio only. Use an anonymous annotator code.
Export a backup before you close this page.</p>
<p>Click a waveform to set the last voice sample.
Left/Right: 10 ms. Shift+Left/Right: 1 ms.
The second canvas shows the boundary ±500 ms.
Green: manual. Red: energy. Blue: aligned.</p>
<label>Annotator code <input id="annotator" autocomplete="off"></label>
<button id="export">Export JSON</button><span id="count"></span>
<p id="notice" class="warning" role="status"></p><main id="rows"></main>
<script id="payload" type="application/json">__DATA__</script>
<script>
"use strict";
const data=JSON.parse(document.getElementById('payload').textContent);
const key='assist-live-annotation-'+data.key;
const notice=document.getElementById('notice');
let progress={annotator:'',rows:{}};
try { const old=JSON.parse(localStorage.getItem(key));
  if(old && typeof old.annotator==='string' && old.rows &&
    typeof old.rows==='object') progress=old;
} catch(e) {notice.textContent='Browser storage is not available. Keep JSON backups.';}
const annotator=document.getElementById('annotator');annotator.value=progress.annotator;
function save(){progress.annotator=annotator.value;
  try{localStorage.setItem(key,JSON.stringify(progress));}
  catch(e){notice.textContent='Browser storage failed. Export JSON now.';}
  const done=Object.values(progress.rows).filter(x=>x.skip===false).length;
  document.getElementById('count').textContent=done+' / '+data.rows.length+' marked';
}
annotator.oninput=save;
let context,source,loopRow=null;
function stop(){if(source){source.stop();source=null;}loopRow=null;}
async function play(row,loop){stop();
  context=context||new AudioContext();await context.resume();
  if(!row.buffer){const bytes=Uint8Array.from(atob(row.audio.split(',')[1]),
    c=>c.charCodeAt(0));
    row.buffer=await context.decodeAudioData(bytes.buffer);}
  source=context.createBufferSource();source.buffer=row.buffer;
  source.connect(context.destination);
  if(loop){const end=(progress.rows[row.id].last_voice_sample+1)/row.rate;
    source.loop=true;source.loopStart=Math.max(0,end-.5);source.loopEnd=end;
    loopRow=row.id;source.start(0,source.loopStart);
  }else{source.start();}
}
const redraw=[];
for(const row of data.rows){
  const restored=progress.rows[row.id];
  if(!restored || !Number.isInteger(restored.last_voice_sample) ||
    restored.last_voice_sample<0 || restored.last_voice_sample>=row.frames){
    progress.rows[row.id]={last_voice_sample:row.initial,
      transcript_differs:false,transcript_note:'',skip:true};
  }
  const state=progress.rows[row.id];
  const section=document.createElement('section');
  const title=document.createElement('h2');title.textContent=row.id;
  section.append(title);
  const transcript=document.createElement('p');transcript.textContent=row.transcript;
  section.append(transcript);
  const status=document.createElement('p');status.className='status';
  section.append(status);
  const canvases=[];
  function draw(){
    status.textContent=(state.skip?'Pending or skipped':'Marked')+
      ' · sample '+state.last_voice_sample+
      ' · '+(state.last_voice_sample/row.rate).toFixed(3)+' s';
    for(const [canvas,zoom] of canvases){
      canvas.width=Math.max(300,Math.floor(canvas.getBoundingClientRect().width));
      canvas.height=130;
      const ctx=canvas.getContext('2d'),width=canvas.width;
      const start=zoom?Math.max(0,state.last_voice_sample-row.rate/2):0;
      const end=zoom?
        Math.min(row.frames,state.last_voice_sample+row.rate/2+1):row.frames;
      canvas.range=[start,end];ctx.clearRect(0,0,width,130);
      ctx.strokeStyle='#555';ctx.beginPath();
      for(let x=0;x<width;x++){const a=Math.floor((start+(end-start)*x/width)/row.bin);
        const b=Math.min(row.peaks.length,
          Math.ceil((start+(end-start)*(x+1)/width)/row.bin));
        let peak=0;for(let i=a;i<b;i++)peak=Math.max(peak,row.peaks[i]);
        ctx.moveTo(x,65-peak*60);ctx.lineTo(x,65+peak*60);}
      ctx.stroke();
      const markers={...row.markers,manual:state.last_voice_sample};
      for(const [name,sample] of Object.entries(markers)){
        if(sample<start||sample>=end)continue;
        const x=(sample-start)/(end-start)*width;
        ctx.strokeStyle={energy:'#b22',aligned:'#25b',
          whisper:'#a5a',manual:'#173'}[name];
        ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,130);ctx.stroke();
        ctx.fillStyle=ctx.strokeStyle;
        ctx.fillText(name,Math.min(x+3,width-55),
          12+Object.keys(markers).indexOf(name)*14);
      }
    }
  }
  function mark(sample){state.last_voice_sample=Math.max(0,
    Math.min(row.frames-1,Math.round(sample)));
    state.skip=false;save();draw();
    if(loopRow===row.id)play(row,true).catch(e=>notice.textContent=String(e));}
  for(const zoom of [false,true]){const canvas=document.createElement('canvas');
    canvas.tabIndex=0;
    canvas.setAttribute('aria-label',zoom?'Boundary detail':'Complete waveform');
    section.append(canvas);canvases.push([canvas,zoom]);
    canvas.onclick=e=>{const bounds=canvas.getBoundingClientRect();
      const [a,b]=canvas.range;
      mark(a+(e.clientX-bounds.left)/bounds.width*(b-a));canvas.focus();};
    canvas.onkeydown=e=>{if(e.key==='ArrowLeft'||e.key==='ArrowRight'){
      e.preventDefault();const step=Math.round(row.rate*(e.shiftKey ? .001 : .01));
      mark(state.last_voice_sample+(e.key==='ArrowLeft'?-1:1)*step);}};
  }
  for(const [label,action] of [['Play',()=>play(row,false)],
    ['Loop last 500 ms',()=>play(row,true)],['Stop',stop],
    ['Accept marker',()=>mark(state.last_voice_sample)],
    ['Skip',()=>{state.skip=true;save();draw();stop();}]]){
    const button=document.createElement('button');button.textContent=label;
    button.onclick=()=>Promise.resolve().then(action)
      .catch(e=>notice.textContent=String(e));section.append(button);
  }
  const checkLabel=document.createElement('label');
  const check=document.createElement('input');check.type='checkbox';
  check.checked=state.transcript_differs;
  checkLabel.append(check,document.createTextNode('Transcript differs'));
  section.append(checkLabel);
  const note=document.createElement('textarea');
  note.placeholder='Describe the transcript difference. Do not enter personal data.';
  note.value=state.transcript_note;section.append(note);
  check.onchange=()=>{state.transcript_differs=check.checked;save();};
  note.oninput=()=>{state.transcript_note=note.value;save();};
  document.getElementById('rows').append(section);redraw.push(draw);draw();
}
window.onresize=()=>redraw.forEach(draw=>draw());save();
document.getElementById('export').onclick=()=>{
  if(!annotator.value.trim()){
    notice.textContent='Enter an anonymous annotator code.';return;}
  const rows=data.rows.map(row=>({id:row.id,audio_sha256:row.audio_sha256,
    row_sha256:row.row_sha256,
    frames:row.frames,...progress.rows[row.id]}));
  if(rows.some(row=>!row.skip&&row.transcript_differs&&!row.transcript_note.trim())){
    notice.textContent='Describe each transcript difference before export.';return;}
  const blob=new Blob([JSON.stringify({version:1,split:data.split,
    annotator:annotator.value.trim(),rows},null,2)],{type:'application/json'});
  const url=URL.createObjectURL(blob),a=document.createElement('a');
  a.href=url;a.download='annotations.json';a.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
};
</script></html>
"""


def build(rows: list[Row], root: Path, output: Path, split: str | None) -> Path:
    output = output.resolve()
    if any((parent / ".git").exists() for parent in (output, *output.parents)):
        raise ValueError("annotation output must be outside Git")
    payload = []
    for row in rows:
        data, frames = audio_info(row, root)
        initial = row.get("last_voice_sample")
        if not integer(initial) or not 0 <= initial < frames:
            raise ValueError("initial sample outside clip")
        path = resolve_audio(root, row["audio_path"])
        with wave.open(str(path), "rb") as wav:
            samples = memoryview(wav.readframes(frames)).cast("B")
            bin_size = max(1, math.ceil(frames / 4000))
            peaks = []
            for start in range(0, frames, bin_size):
                peaks.append(
                    round(
                        max(
                            abs(
                                int.from_bytes(
                                    samples[2 * i : 2 * i + 2], "little", signed=True
                                )
                            )
                            for i in range(start, min(frames, start + bin_size))
                        )
                        / 32768,
                        5,
                    )
                )
        payload.append(
            {
                "id": row["id"],
                "transcript": row["transcript"],
                "rate": row["sample_rate"],
                "frames": frames,
                "initial": initial,
                "markers": automatic_markers(row),
                "row_sha256": row_digest(row),
                "audio_sha256": row["audio_sha256"],
                "audio": "data:audio/wav;base64," + base64.b64encode(data).decode(),
                "bin": bin_size,
                "peaks": peaks,
            }
        )
    key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    encoded = json.dumps(
        {"version": 1, "split": split or "all", "key": key, "rows": payload}
    )
    encoded = (
        encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    )
    output.mkdir(parents=True, exist_ok=True)
    path = output / "index.html"
    path.write_text(PAGE.replace("__DATA__", encoded), encoding="utf-8")
    return path


def import_annotations(
    labels: Path, root: Path, export: object, split: str | None
) -> int:
    rows = load_corpus(labels)
    select_rows(rows, None, None, False)
    if (
        not isinstance(export, dict)
        or set(export) != {"version", "split", "annotator", "rows"}
        or type(export["version"]) is not int
        or export["version"] != 1
        or export["split"] not in ("tuning", "held_out", "all")
        or (split is not None and export["split"] != split)
        or not text(export["annotator"])
        or not isinstance(export["rows"], list)
        or not export["rows"]
    ):
        raise ValueError("invalid export header")
    index = {row["id"]: row for row in rows}
    seen = set()
    changes = 0
    for entry in export["rows"]:
        if not isinstance(entry, dict) or set(entry) != EXPORT_FIELDS:
            raise ValueError("invalid export row fields")
        identifier = entry["id"]
        if (
            not isinstance(identifier, str)
            or identifier not in index
            or identifier in seen
        ):
            raise ValueError("unknown or duplicate export id")
        seen.add(identifier)
        row = index[identifier]
        if (split is not None and row.get("split") != split) or (
            export["split"] != "all" and row.get("split") != export["split"]
        ):
            raise ValueError("export row outside requested split")
        _, frames = audio_info(row, root)
        sample = entry["last_voice_sample"]
        if (
            entry["audio_sha256"] != row["audio_sha256"]
            or entry["row_sha256"] != row_digest(row)
            or not integer(entry["frames"])
            or entry["frames"] != frames
            or not integer(sample)
            or not 0 <= sample < frames
            or type(entry["skip"]) is not bool
            or type(entry["transcript_differs"]) is not bool
            or not isinstance(entry["transcript_note"], str)
            or (
                not entry["skip"]
                and entry["transcript_differs"]
                and not text(entry["transcript_note"])
            )
        ):
            raise ValueError("tampered export or invalid annotation")
        if entry["skip"]:
            continue
        updates: Row = {
            "last_voice_sample": sample,
            "last_voice_annotation": "manual",
            "last_voice_annotator_note": {
                "annotator": export["annotator"],
                "transcript_differs": entry["transcript_differs"],
                "transcript_note": entry["transcript_note"],
            },
        }
        if not (
            integer(row.get("aligned_end_sample"))
            and integer(row.get("energy_minus_aligned_samples"))
        ):
            energy = automatic_markers(row).get("energy")
            if energy is not None:
                updates["last_voice_annotator_note"]["automatic_energy_sample"] = energy
        if any(row.get(key) != value for key, value in updates.items()):
            row.update(updates)
            changes += 1
    if changes:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=labels.parent, delete=False
        ) as temporary:
            name = Path(temporary.name)
            try:
                temporary.write(
                    "".join(
                        json.dumps(row, sort_keys=True, allow_nan=False) + "\n"
                        for row in rows
                    )
                )
                temporary.flush()
                os.fsync(temporary.fileno())
            except BaseException:
                name.unlink(missing_ok=True)
                raise
        try:
            os.replace(name, labels)
        finally:
            name.unlink(missing_ok=True)
    return changes


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def compare(rows: list[Row]) -> list[str]:
    errors: dict[str, list[float]] = {}
    closest: dict[str, int] = {}
    method_rows: dict[str, set[int]] = {}
    for number, row in enumerate(rows):
        if row.get("last_voice_annotation") != "manual":
            continue
        rate, manual = row.get("sample_rate"), row.get("last_voice_sample")
        if not integer(rate) or rate <= 0 or not integer(manual):
            raise ValueError("invalid manual sample or rate")
        differences = {
            method: abs(sample - manual) * 1000 / rate
            for method, sample in automatic_markers(row).items()
        }
        for method, error in differences.items():
            errors.setdefault(method, []).append(error)
            method_rows.setdefault(method, set()).add(number)
            if error == min(differences.values()):
                closest[method] = closest.get(method, 0) + 1
    if not errors:
        raise ValueError("no paired manual and automatic labels")
    lines = [
        f"{method} n={len(values)} median_ms={percentile(values, 0.5):.3f} "
        f"p90_ms={percentile(values, 0.9):.3f} max_ms={max(values):.3f} "
        f"closest_rows={closest.get(method, 0)}"
        for method, values in sorted(errors.items())
    ]
    # Equal row coverage is necessary for a method-to-method summary.
    coverage = {frozenset(values) for values in method_rows.values()}
    winner = min(
        errors,
        key=lambda method: (
            percentile(errors[method], 0.5),
            percentile(errors[method], 0.9),
        ),
    )
    lines.append(
        f"closest={winner}"
        if len(coverage) == 1
        else "closest=not-comparable (different row coverage)"
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mark corpus boundaries without network access."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "import", "compare"):
        command = commands.add_parser(name)
        command.add_argument("--labels", type=Path, required=True)
        command.add_argument(
            "--split",
            choices=("tuning", "held_out"),
            default="tuning" if name == "compare" else None,
        )
        if name != "compare":
            command.add_argument("--audio-root", type=Path, required=True)
        if name == "build":
            command.add_argument("--ids", help="Comma-separated recording ids")
            command.add_argument("--flagged", action="store_true")
            command.add_argument("--output", type=Path, required=True)
        elif name == "import":
            command.add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            export: Any = json.loads(args.input.read_text(encoding="utf-8"))
            count = import_annotations(args.labels, args.audio_root, export, args.split)
            print(f"PASS imported manual annotations changed={count}")
        else:
            rows = load_corpus(args.labels)
            if args.command == "build":
                if args.split is None and args.ids is None and not args.flagged:
                    raise ValueError("select --split, --ids, or --flagged")
                selected = select_rows(rows, args.split, args.ids, args.flagged)
                path = build(selected, args.audio_root, args.output, args.split)
                print(f"PASS annotation page rows={len(selected)} path={path}")
            else:
                selected = select_rows(rows, args.split, None, False)
                print(
                    f"NOTE split={args.split}; held_out is evaluation only; "
                    "no parameter tuning"
                )
                for line in compare(selected):
                    print(line)
                print("PASS manual calibration comparison (no parameters changed)")
    except (OSError, ValueError, KeyError, TypeError, wave.Error, EOFError) as error:
        print(f"FAIL annotation {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
