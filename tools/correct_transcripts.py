import argparse
import base64
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.annotate_corpus import audio_info
from tools.corpus import Row, load_corpus, text
from tools.result_text import words

FIELDS = {"id", "audio_sha256", "correct_transcript", "status", "source", "engines"}
SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{7,40}")


def accepted_text(value: Any) -> bool:
    return isinstance(value, str) and bool(words(value))


def final_text(row: Row | None) -> str | None:
    if row is not None and row.get("status") == "final":
        value = row.get("correct_transcript")
        if isinstance(value, str) and accepted_text(value):
            return value
    return None


def identity_valid(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    pins = value.get("model_files")
    return (
        all(text(value.get(key)) for key in ("family", "model", "settings"))
        and isinstance(value.get("code_commit"), str)
        and COMMIT.fullmatch(value["code_commit"]) is not None
        and isinstance(pins, list)
        and bool(pins)
        and all(
            isinstance(pin, dict)
            and set(pin) == {"file", "file_sha256"}
            and text(pin["file"])
            and isinstance(pin["file_sha256"], str)
            and SHA256.fullmatch(pin["file_sha256"])
            for pin in pins
        )
        and len({pin["file"] for pin in pins}) == len(pins)
    )


def split_records(rows: list[Row]) -> tuple[Row, list[Row]]:
    if (
        not rows
        or set(rows[0]) != {"kind", "engine_identities"}
        or rows[0].get("kind") != "engines"
        or sum(row.get("kind") == "engines" for row in rows) != 1
        or not isinstance(rows[0]["engine_identities"], dict)
        or len(rows[0]["engine_identities"]) != 2
        or not all(
            text(name) and identity_valid(identity)
            for name, identity in rows[0]["engine_identities"].items()
        )
    ):
        raise ValueError("one valid engine header must be first")
    identities = rows[0]["engine_identities"]
    if len({identity["family"] for identity in identities.values()}) != 2:
        raise ValueError("different model families required")
    return identities, rows[1:]


def validate(rows: list[Row], labels: list[Row]) -> list[str]:
    try:
        identities, rows = split_records(rows)
    except ValueError as error:
        return [str(error)]
    failures = []
    by_id = {label["id"]: label for label in labels}
    ids = [row.get("id") for row in rows]
    if (
        not all(isinstance(value, str) for value in ids)
        or len(set(value for value in ids if isinstance(value, str))) != len(ids)
        or set(value for value in ids if isinstance(value, str)) != set(by_id)
    ):
        failures.append("transcript ids must match labels exactly")
    for row in rows:
        identifier = row.get("id")
        label = by_id.get(identifier) if isinstance(identifier, str) else None
        if set(row) != FIELDS:
            failures.append(f"{identifier}: transcript schema")
        if label is None or row.get("audio_sha256") != label["audio_sha256"]:
            failures.append(f"{identifier}: transcript audio hash")
        engines = row.get("engines")
        if (
            not isinstance(engines, list)
            or len(engines) != 2
            or any(
                not isinstance(engine, dict)
                or set(engine) != {"transcript", "engine"}
                or not isinstance(engine.get("transcript"), str)
                or not isinstance(engine.get("engine"), str)
                or engine["engine"] not in identities
                for engine in engines
            )
        ):
            failures.append(
                f"{identifier}: two engine identities and transcripts required"
            )
            continue
        if engines[0]["engine"] == engines[1]["engine"]:
            failures.append(f"{identifier}: different model families required")
        first, second = (words(engine["transcript"]) for engine in engines)
        agreement = bool(first) and first == second
        if row.get("status") == "needs_listening":
            if (
                row.get("source") is not None
                or row.get("correct_transcript") is not None
                or agreement
            ):
                failures.append(f"{identifier}: pending transcript state")
        elif row.get("status") == "unintelligible":
            if (
                row.get("source") != "person"
                or row.get("correct_transcript") is not None
            ):
                failures.append(f"{identifier}: unintelligible transcript state")
        elif row.get("status") == "final":
            if not accepted_text(row.get("correct_transcript")):
                failures.append(f"{identifier}: invalid correct text")
            elif row.get("source") == "engines_agree":
                if not agreement or words(row["correct_transcript"]) != first:
                    failures.append(f"{identifier}: engine agreement differs")
            elif row.get("source") != "person":
                failures.append(f"{identifier}: invalid transcript source")
        else:
            failures.append(f"{identifier}: invalid transcript status")
    return failures


def from_engines(labels: list[Row], first: list[Row], second: list[Row]) -> list[Row]:
    expected = {label["id"] for label in labels}
    indexed = []
    for output in (first, second):
        ids = [row.get("id") for row in output]
        if not all(isinstance(value, str) for value in ids):
            raise ValueError("engine ids must match labels")
        if len(set(ids)) != len(ids) or set(ids) != expected:
            raise ValueError("engine ids must match labels")
        indexed.append({row["id"]: row for row in output})
    identities = {}
    for output in (first, second):
        if not output or not identity_valid(output[0].get("engine")):
            raise ValueError("engine identity required")
        identity = output[0]["engine"]
        if any(row.get("engine") != identity for row in output):
            raise ValueError("engine identity changes between clips")
        identities[identity["family"]] = identity
    rows: list[Row] = [{"kind": "engines", "engine_identities": identities}]
    for label in labels:
        engines = [index[label["id"]] for index in indexed]
        if any(
            engine.get("audio_sha256") != label["audio_sha256"] for engine in engines
        ):
            raise ValueError("engine audio hash differs from label")
        if any(not isinstance(engine.get("transcript"), str) for engine in engines):
            raise ValueError("engine transcript must be text")
        one, two = (words(engine["transcript"]) for engine in engines)
        agree = bool(one) and one == two
        rows.append(
            {
                "id": label["id"],
                "audio_sha256": label["audio_sha256"],
                "correct_transcript": engines[0]["transcript"] if agree else None,
                "source": "engines_agree" if agree else None,
                "status": "final" if agree else "needs_listening",
                "engines": [
                    {
                        "transcript": engine["transcript"],
                        "engine": engine["engine"]["family"],
                    }
                    for engine in engines
                ],
            }
        )
    failures = validate(rows, labels)
    if failures:
        raise ValueError("; ".join(failures))
    return rows


def row_digest(row: Row) -> str:
    return hashlib.sha256(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def check_digest(path: Path) -> bool:
    try:
        expected = path.with_suffix(".sha256").read_text().strip()
        return (
            expected == hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name
        )
    except OSError:
        return False


def write_data(path: Path, rows: list[Row]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    path.with_suffix(".sha256").write_text(
        hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n"
    )


PAGE = r"""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Correct transcripts</title>
<h1>Correct transcripts</h1>
<p>Play each recording. Write the words that you hear, not the prompt.
A "Use" button copies that text into the input.
Change the text if it is not correct.</p>
<p>Select "Cannot understand" if you cannot understand the words.
Keep an input empty if you have not made a decision.
The page keeps your text in this browser if you reload it.
Export JSON before you close the page.</p>
<p><button id="export">Export JSON</button> <span id="count"></span></p>
<main id="clips"></main>
<script type="application/json" id="data">__DATA__</script>
<script>
const rows = JSON.parse(document.getElementById('data').textContent);
const inputs = new Map();
const unclear = new Map();
const key = row => 'correct-transcripts:' + row.row_sha256;
const count = () => {
  const written = rows.filter(row => inputs.get(row.id).value.trim() ||
    unclear.get(row.id).checked).length;
  document.getElementById('count').textContent =
    written + ' of ' + rows.length + ' written';
  return written;
};
for (const row of rows) {
  const section = document.createElement('section');
  const heading = document.createElement('h2'); heading.textContent = row.id;
  section.append(heading);
  const audio = document.createElement('audio'); audio.controls = true;
  audio.src = row.audio; section.append(audio);
  const input = document.createElement('input'); input.size = 90;
  const choice = document.createElement('input'); choice.type = 'checkbox';
  choice.checked = localStorage.getItem(key(row) + ':unintelligible') === 'true';
  input.disabled = choice.checked;
  unclear.set(row.id, choice);
  choice.onchange = () => {
    input.disabled = choice.checked;
    localStorage.setItem(key(row) + ':unintelligible', String(choice.checked));
    input.oninput();
  };
  input.dataset.id = row.id;
  input.value = localStorage.getItem(key(row)) || '';
  input.oninput = () => {
    localStorage.setItem(key(row), input.value); count();
  };
  inputs.set(row.id, input);
  for (const [name, text] of [['Prompt', row.prompt], ...row.engines.map(
      e => [e.engine.model, e.transcript])]) {
    const p = document.createElement('p');
    const use = document.createElement('button'); use.textContent = 'Use';
    use.dataset.pick = name;
    use.onclick = () => {
      choice.checked = false; choice.onchange();
      input.value = text; input.oninput(); input.focus();
    };
    p.append(use, ' ' + name + ': ' + text); section.append(p);
  }
  const label = document.createElement('label'); label.textContent = 'Correct text: ';
  label.append(input); section.append(label);
  const decision = document.createElement('label');
  decision.append(choice, ' Cannot understand'); section.append(decision);
  document.getElementById('clips').append(section);
}
count();
document.getElementById('export').onclick = () => {
  if (!count()) {
    alert('No decision is recorded. Nothing to export.');
    return;
  }
  const exported = rows.flatMap(row => {
    const input = inputs.get(row.id);
    const unintelligible = unclear.get(row.id).checked;
    if (!input.value.trim() && !unintelligible) return [];
    return [{id: row.id, audio_sha256: row.audio_sha256, row_sha256: row.row_sha256,
             status: unintelligible ? 'unintelligible' : 'final',
             correct_transcript: unintelligible ? null : input.value.trim()}];
  });
  const blob = new Blob([JSON.stringify({version: 2, rows: exported})],
                        {type: 'application/json'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url;
  a.download = 'correct-transcripts.json';
  a.click(); URL.revokeObjectURL(url);
};
</script></html>
"""


def listening_page(
    labels: list[Row], rows: list[Row], root: Path, output: Path
) -> None:
    output = output.resolve()
    if any((parent / ".git").exists() for parent in (output, *output.parents)):
        raise ValueError("listening output must be outside Git")
    by_id = {label["id"]: label for label in labels}
    payload = []
    identities, rows = split_records(rows)
    for row in rows:
        if row["status"] != "needs_listening":
            continue
        label = by_id[row["id"]]
        data, _ = audio_info(label, root)
        payload.append(
            {
                **row,
                "engines": [
                    {
                        "transcript": engine["transcript"],
                        "engine": identities[engine["engine"]],
                    }
                    for engine in row["engines"]
                ],
                "prompt": label["transcript"],
                "row_sha256": row_digest(row),
                "audio": "data:audio/wav;base64," + base64.b64encode(data).decode(),
            }
        )
    encoded = json.dumps(payload).replace("&", "\\u0026").replace("<", "\\u003c")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(PAGE.replace("__DATA__", encoded), encoding="utf-8")


def mark_unintelligible(rows: list[Row], identifier: str) -> list[Row]:
    _, clips = split_records(rows)
    by_id = {row["id"]: row for row in clips}
    if identifier not in by_id:
        raise ValueError("unknown listening id")
    status = by_id[identifier]["status"]
    if status != "needs_listening":
        raise ValueError(f"listening row is already {status}")
    return [
        rows[0],
        *[
            {
                **row,
                "status": "unintelligible",
                "source": "person",
                "correct_transcript": None,
            }
            if row["id"] == identifier
            else row
            for row in clips
        ],
    ]


def import_listened(rows: list[Row], document: Any) -> list[Row]:
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "rows"}
        or type(document["version"]) is not int
        or document["version"] not in (1, 2)
        or not isinstance(document["rows"], list)
    ):
        raise ValueError("invalid listening export")
    _, clips = split_records(rows)
    by_id = {row["id"]: dict(row) for row in clips}
    seen = set()
    fields = {"id", "audio_sha256", "row_sha256", "correct_transcript"}
    if document["version"] == 2:
        fields.add("status")
    for item in document["rows"]:
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("invalid listening row")
        identifier = item["id"]
        if not isinstance(identifier, str) or identifier not in by_id:
            raise ValueError("unknown listening id")
        if identifier in seen:
            raise ValueError("duplicate listening id")
        seen.add(identifier)
        row = by_id[identifier]
        if row["status"] != "needs_listening":
            raise ValueError("listening row is already final")
        if item["audio_sha256"] != row["audio_sha256"] or item[
            "row_sha256"
        ] != row_digest(row):
            raise ValueError("stale listening row")
        status = item.get("status", "final")
        if status == "unintelligible":
            if item["correct_transcript"] is not None:
                raise ValueError("unintelligible text must be null")
        elif status != "final" or not accepted_text(item["correct_transcript"]):
            raise ValueError("correct text rejected by words rule")
        row.update(
            correct_transcript=item["correct_transcript"],
            source="person",
            status=status,
        )
    return [rows[0], *[by_id[row["id"]] for row in clips]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("from-engines", "listen", "import", "check", "mark-unintelligible"):
        command = commands.add_parser(name)
        command.add_argument("--labels", type=Path, required=True)
        command.add_argument("--data", type=Path, required=True)
        if name == "from-engines":
            command.add_argument("--engine-a", type=Path, required=True)
            command.add_argument("--engine-b", type=Path, required=True)
        elif name == "listen":
            command.add_argument("--audio-root", type=Path, required=True)
            command.add_argument("--output", type=Path, required=True)
        elif name == "import":
            command.add_argument("--input", type=Path, required=True)
        elif name == "mark-unintelligible":
            command.add_argument("--id", required=True)
    args = parser.parse_args(argv)
    try:
        labels = load_corpus(args.labels)
        rows = (
            from_engines(labels, load_corpus(args.engine_a), load_corpus(args.engine_b))
            if args.command == "from-engines"
            else load_corpus(args.data)
        )
        failures = validate(rows, labels)
        if failures:
            raise ValueError("; ".join(failures))
        if args.command != "from-engines" and not check_digest(args.data):
            raise ValueError("correct transcript digest mismatch")
        if args.command == "from-engines":
            if args.data.exists():
                raise ValueError("refuse to replace existing correct transcripts")
            write_data(args.data, rows)
        elif args.command == "listen":
            listening_page(labels, rows, args.audio_root, args.output)
        elif args.command in ("import", "mark-unintelligible"):
            updated = (
                import_listened(rows, json.loads(args.input.read_text()))
                if args.command == "import"
                else mark_unintelligible(rows, args.id)
            )
            failures = validate(updated, labels)
            if failures:
                raise ValueError("; ".join(failures))
            write_data(args.data, updated)
        print(f"PASS correct transcripts: {len(rows) - 1} clips; one engine header")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"FAIL correct transcripts: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
