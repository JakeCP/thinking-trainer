# Thinking Trainer

A personal app for learning 20 "lenses" (portable ideas from statistics, economics,
game theory and systems) so you notice them when nothing is labelled. Feature 1, the
problem bank, is built. Recognition drills, the prediction log and progress views come
next. Design and rationale: [`docs/DESIGN.md`](docs/DESIGN.md).

## Run it

Needs [uv](https://docs.astral.sh/uv/getting-started/installation/) and `make`.

```bash
make start     # first time: creates .env and stops so you can add ANTHROPIC_API_KEY
make start     # again: installs, builds the problem bank, starts the app
```

To try the extraction on one lens before paying for all of them, use `make start LENS=17`.

Then open http://127.0.0.1:8000. After the first time:

| command | what it does |
|---|---|
| `make run` | start the app (`PORT=8001` to change the port) |
| `make ingest` | build or update the problem bank; `LENS="17"` or `LENS="3 17"` for some lenses, `ARGS="--re-extract"` for other flags |
| `make test` | run the tests (no network or key needed) |
| `make` | list the commands |

The database and caches live in `data/` (gitignored). Migrations run automatically.

## The workflow

1. **Ingest.** `make ingest` fetches each document once (cached in
   `data/cache/`), makes one Claude call per document pair to transcribe the problems
   your curriculum names, and checks every transcription against the PDF's own text.
   Anything that doesn't pass goes to review; nothing is dropped silently. Re-running
   it is safe: extracted pairs are skipped, failed ones are retried.
   Flags go in `ARGS`, as in `make ingest ARGS="--re-extract"`:
   - `--re-extract`: re-run extraction (problems you've reviewed are never overwritten)
   - `--refetch`: download documents again
   - `--originals-dir DIR`: load the original substitutes from `DIR/substitute_exercises.html`
2. **Review** (`/review`). Each flagged problem shows why it was flagged, links to the
   source, and gives you editable statement and solution boxes. Edits go into a served
   override; the extracted original is kept. Approve puts the problem in rotation.
3. **Practice** (`/practice`). Problems are interleaved across lenses, with no lens or
   course shown until you've answered. Answer in text or LaTeX, optionally attach
   photos, optionally say how sure you are, then see the grade, the official solution
   and the lens. Rate yourself 1–4; FSRS schedules the next review.
4. **Desk** (`/desk`). Labs and worked examples, listed by lens with a done checkbox.

## Flags you'll see in review

| code | meaning |
|---|---|
| `low_coverage` | under 92% of the transcribed words appear in the source: possible paraphrase or invented text |
| `no_text_layer` | scanned PDF; nothing could be checked |
| `unmatched` / `missing_target` | no solution found, or the curriculum entry wasn't found in the document |
| `names_lens` | the statement names its own technique (terms in `content/lens_terms.yaml`); consider redacting |
| `figure`, `illegible`, `external_ref`, `depends_on_previous` | the problem relies on something you won't see |
| `known_erratum`, `ref_mismatch`, `off_lens` | reported by the extractor; read the detail |
| `fetch_failed`, `extraction_failed` | fix the cause and re-run ingest, or type the problem in by hand |
| `original` | an original substitute: check it before approving |

## Tuning

- **Prompts** live in `prompts/*.md`: YAML front matter (`model`, `effort`, `max_tokens`)
  followed by the system prompt. Edit them freely. Every call is logged in the
  `llm_call` table with the prompt's hash, so you can compare grades before and after
  a change.
- **Give-away terms**: `content/lens_terms.yaml`.
- **New problems per day**: `NEW_PER_DAY` (default 3). **Local day boundary**:
  `APP_TZ` (default UTC).

## Deploy

One container, one SQLite file. Set `APP_PASSWORD` and `SESSION_SECRET` on any host
other people can reach: the app spends your API credit.

```bash
docker build -t thinking-trainer .
docker run -p 8000:8000 -v tt-data:/data --env-file .env thinking-trainer
docker run --rm -v tt-data:/data --env-file .env thinking-trainer uv run python -m app.ingest
```

On Fly.io, mount a 1 GB volume at `/data`. Back up by copying `data/app.db` (use
`sqlite3 app.db ".backup copy.db"` while the app is running).

## Tests

```bash
uv run pytest
```

The tests never touch the network or the API. They use generated PDFs, a mock HTTP
client and a fake Claude transport.
