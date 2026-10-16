# Thinking Trainer — design (draft for review)

Status: feature 1 (problem bank) is built; features 2–4 are still the plan below. Prompts are in `prompts/`.

## 1. Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12+, managed with `uv` | Best PDF tooling, the reference FSRS implementation (`fsrs` on PyPI), and the official `anthropic` SDK. One language end to end. |
| Web | FastAPI + Jinja2 templates, plain forms and redirects, about 60 lines of vanilla JS | Server-rendered forms are the right shape for this app: no SPA, no build step, no `node_modules`. (htmx was in the proposal; plain forms turned out to cover everything.) |
| DB | SQLite (WAL mode) through SQLAlchemy 2.0, with Alembic migrations | Single user, single file, trivial backups. Alembic means schema changes stay boring. |
| Math | KaTeX 0.16.11, vendored in `app/web/static/katex` (woff2 fonts only, 612 KB) | Renders problem and solution LaTeX, and gives a live preview of your answer while you type. Vendored so the app has no third-party runtime dependency. |
| Charts | Server-rendered inline SVG; the confusion matrix is an HTML table with shaded cells | A calibration plot has about 10 points and doesn't need a chart library. |
| LLM | `anthropic` SDK with structured outputs (Pydantic schemas) | Grades come back as typed JSON, not text that has to be parsed. |
| PDFs | `pypdf` for the text layer (used only for *verification*); Claude reads the PDF itself for extraction | OCW problem sets are LaTeX-typeset, and text-layer extraction mangles math. Claude reading the page is better at transcription. The text layer is what keeps it honest (§3). |
| Images | Pillow, to downscale phone photos to 1600 px before sending | Phone photos are 4–12 MB, and Claude doesn't need that many pixels. |
| Auth | One password (`APP_PASSWORD` env var) plus a signed session cookie | A hosted copy spends your API money, so it needs a lock, but a single user doesn't need accounts. |
| Deploy | One Dockerfile. Fly.io with a 1 GB volume, or any $5 VPS. Optional Litestream to R2/S3 for continuous SQLite backup. | One process and one file. |

Rejected options:
- **Next.js/TS**: fine, but PDF handling and FSRS are weaker, and you'd maintain a build pipeline for what is a set of forms.
- **Postgres**: no concurrency to justify it.
- **A chart library**: not needed until a chart is too hard to draw as SVG.

### Models and cost
Every call defaults to `claude-opus-5-5`. Model and effort are set per prompt in the prompt file's front matter, so you can move situation generation to Sonnet or Haiku without touching code. Rough costs at Opus 5.5 rates ($4/$20 per M tokens):
- **Ingest** (one-off): about $0.10–0.25 per problem/solution PDF pair. Low tens of dollars for the whole bank, depending on how many problem sets `lenses.json` points at.
- **Grading one answer**: about $0.03–0.08.
- **Generating 10 situations plus blind validation**: about $0.20.

## 2. Data model

```
lens            id (pk, from lenses.json), name, question, summary,
                prerequisites JSON, resources JSON,
                aliases JSON        -- names you might type for it
                tell_words JSON     -- jargon that gives it away; banned in situations

source_doc      id, url UNIQUE, kind (pdf|html), sha256, fetched_at, http_status,
                raw_path (cached bytes on disk), text_layer, has_text_layer, error

problem         id, problem_doc_id → source_doc, solution_doc_id → source_doc,
                source_label        -- "18.05 PS4 #3"; hidden until answered
                kind (exercise | lab | worked_example | original),
                situation_based     -- from the curriculum file
                statement_original  -- as extracted, never edited
                statement_served    -- nullable manual override (e.g. redacting a give-away phrase)
                solution,
                status (ok | needs_review | rejected),  -- only `ok` is ever served
                flags JSON [{code, detail}],
                coverage            -- verification score, see §3
                extraction_call_id → llm_call, reviewed_at

problem_lens    problem_id, lens_id, problem_ref (as written in the curriculum file),
                parts (e.g. "a–c"), curator_notes
                -- many-to-many: Yale ECON 159 final Q3 and Q5 are each listed under two lenses

card            problem_id (pk) → problem, due, stability, difficulty, state,
                step, last_review   -- FSRS state; `due` is indexed

attempt         id, problem_id, shown_at, submitted_at,
                answer_text, photo_path,
                confidence          -- optional 0–100 "I'm right", entered before the grade (see Q3)
                verdict (correct | partially_correct | incorrect | ungradeable),
                feedback, grade JSON, solution_concern,
                grading_call_id → llm_call,
                rating (1–4)        -- yours; this is what FSRS consumes

-- feature 2
situation       id, kind (single | competing | none | contrast), pair_id, pair_role (a|b),
                pair_type (same_surface | same_lens), domain (work | news | personal),
                text, key_lens_ids JSON, decoy_lens_id, rationale, prediction, why_not JSON,
                origin (generated | mine), validator_agreed, status (active | flagged | retired),
                gen_call_id → llm_call

drill_attempt   id, situation_id, answered_at, my_lens_text, my_prediction, confidence,
                named_lens_ids JSON -- my_lens_text resolved to lens ids (or ["none"]); you can correct it
                lens_correct, prediction_verdict, feedback, grading_call_id

-- feature 3
forecast        id, question, probability, resolve_by, lens_id (nullable), created_at,
                resolved_at, outcome (nullable bool), fatebook_id, fatebook_url, notes

-- infrastructure
llm_call        id, purpose, prompt_name, prompt_sha, model, effort, input_tokens,
                output_tokens, request_text (no binaries), response_json, error, created_at
```

Notes:
- **The confusion matrix is a query, not a table.** It is `drill_attempt ⋈ situation`, with rows for the true lens plus "none" and columns for the lens you named plus "none".
- **Every grade records `prompt_sha`.** When you tune a prompt, you can compare grades before and after the change instead of guessing.
- **`statement_original` is never edited.** Overrides go in `statement_served`, so re-ingesting never clobbers your fixes.

## 3. Feature 1: ingest pipeline

`uv run python -m app.ingest content/curriculum.json [--lens ID] [--refetch]`. Runs are idempotent and resumable.

What the curriculum file implies (91 entries):
- **60 exercises** from 24 document pairs (32 URLs). 43 of them use the same URL for problem and solution, because the statement and solution are in one PDF.
- **9 labs and 2 worked examples** (ISLP labs, Think Bayes, 15.010 slides). These are real sources, but the "problem" is "recreate this lab", which isn't something you answer in free text on a phone.
- **20 "original substitutes"** pointing at `substitute_exercises.html`, which wasn't attached. See Q5.
- **Kind** is inferred from markers in the file: `source` beginning "Original substitute", and notes containing "WORKED-LAB SUBSTITUTE" or "WORKED-EXAMPLE SUBSTITUTE". The inference is logged. An explicit `kind` field in the JSON overrides it.

Steps:
1. **Fetch** each URL once and cache the bytes by sha256. A fetch failure (404, timeout, paywall HTML where a PDF was expected) becomes a flag on the problem. Nothing is retried silently.
2. **Extract** with one Claude call per *document pair*, carrying every curriculum entry that points at it (`prompts/extract_problems.md`). The Yale final, for example, holds 7 entries across 5 lenses in one call. This is cheaper, keeps shared setup consistent, and dedupes cross-listed questions into a single problem with several lens tags. Claude transcribes only; it does not solve, complete, or fix anything. It returns each targeted problem with its label, the selected parts plus any shared setup they depend on, the matched solution, and self-reported issues.
3. **Verify mechanically**, with no LLM involved. Each check that trips becomes a flag and sends the problem to `needs_review`:
   - `low_coverage`: the share of alphabetic words in Claude's transcription that also appear in the PDF's text layer is below 0.92. This catches paraphrase and invented text. It deliberately ignores math tokens, which the text layer mangles. **Limit:** it catches added or reworded text, not *dropped* sentences. Omissions are only partly covered (by the label-count check below), which is why the review screen links the source.
   - `no_text_layer`: a scanned PDF. Nothing can be verified, so it always goes to review.
   - `unmatched`: a problem with no matching solution, or the reverse, or the problem and solution documents disagree on the number of problems.
   - `illegible`, `figure`, `external_ref` ("using the data in Table 3.2"), `depends_on_previous` ("continuing from Problem 2"): reported by Claude and confirmed by string match.
   - `names_lens`: the statement contains the lens name, an alias, or a tell word. The problem stays servable only once you've written a redacted `statement_served`, because "Use Little's law to…" defeats the purpose.
   - `off_lens`: Claude thinks this problem from a lens's problem set doesn't actually exercise that lens (a warm-up algebra question, say). Otherwise the reveal would show a wrong lens label.
4. **Review screen** (`/review`): source link, extracted fields side by side, editable, with buttons to approve, reject, or edit and approve. Only `ok` problems enter the pool.

Generated content never enters `problem`. The two tables share no code path for creation.

### Running without an API key
Everything above works without a key, with two substitutions:
- **Ingest fetches but doesn't extract.** Each exercise becomes a `not_extracted` placeholder, and the review page shows the document's text layer to copy from. Nothing is guessed: the alternative to the model is you.
- **You grade yourself.** After answering you see the official solution, then mark Right / Partly / Wrong. `attempt.verdict_source` records `self` or `llm`, so accuracy stats can keep the two apart.

Adding a key later picks up where you left off. Approved or rejected placeholders are never sent for extraction. A placeholder with hand-typed text is kept and flagged `superseded` rather than deleted.

### Serving and scheduling
- A session builds a queue of due cards, plus up to N new cards per day (default 3), ordered so that no two consecutive problems share a lens when that can be avoided.
- The problem page shows the statement only: no lens, course, or problem-set label. After you submit, you see the grade and feedback, then the official solution, lens, and source link, then the 1–4 rating buttons.
- FSRS state is per problem (Q1). FSRS's default 1- and 10-minute learning steps are turned off: they suit flashcards, but a problem whose solution you've just read shouldn't come back within minutes. The first review is always a day or more away.
- A graded attempt must be rated before the next problem is served, so no rating is ever lost.

## 4. Feature 2: recognition drills (outline only; built after feature 1)

- **Code chooses the mix; the LLM only writes.** The app decides each slot's kind, lens, and domain (default about 55% single, 20% competing, 15% none, 10% contrast pairs). Lens weights follow your confusion matrix: lenses you confuse come up more, and competing pairs are drawn from your top confusions. LLMs are bad at random mixing, so the mix isn't left to the model.
- A **buffer** of about 30 unanswered situations is kept, refilled in the background, so the phone never waits on generation.
- **Blind validation**: a second call sees only the situation text and the lens catalog and names the lens. A disagreement with the key marks the situation `flagged` and pulls it from the pool. A drill with an ambiguous or wrong key trains the wrong reflex, so this is worth the cost. Caveat: two calls to the same model can share a blind spot, so this filters the worst cases rather than guaranteeing correctness.
- You name the lens in **free text, with no menu or autocomplete**. A 20-item picker is multiple choice, and real life doesn't offer one. The drill grader maps your text to lens ids and also grades your prediction. Your raw text is stored, and you can correct the mapping.
- **Your own situations**: `origin = mine`, keyed with the lens you chose. The validator still runs and shows you whether it agrees, but it never overrides you.

## 5. Feature 3: prediction log (outline only)

Local storage is the source of truth either way, because Fatebook doesn't know about lenses and per-lens calibration is the point. If `FATEBOOK_API_KEY` is set, a forecast is also pushed with `createQuestion` and the returned URL is stored. A community MCP server shows Fatebook exposing `createQuestion` (with `title`, `resolveBy`, `forecast`) and `resolveQuestion` under `/api/v0/`. I couldn't reach fatebook.io's own docs from the build sandbox, so this gets verified against the real API when feature 3 is built. Brier score and the calibration chart are always computed locally.

## 6. Layout

```
app/        main.py  db.py  models.py  llm.py  scheduling.py  auth.py
            ingest/ (fetch.py extract.py verify.py __main__.py)
            routes/ (problems.py review.py drills.py forecasts.py progress.py)
            templates/  static/
prompts/    *.md: YAML front matter (model, effort, max_tokens) + Jinja body
migrations/ alembic
data/       app.db  cache/  uploads/   (gitignored)
tests/      fixture PDFs + recorded LLM responses; tests never hit the network
```

## 7. Open questions

- **Q1. Per-problem FSRS.** Decided: per problem. The bank trains *using* each tool, and the drills train *noticing* it.
- **Q2. Bare answers.** Decided: a correct final answer with no reasoning is `partially_correct`.
- **Q3. Confidence before reveal.** Decided: yes, an optional 0–100 field on every attempt.
- **Q4. Curriculum file.** Answered: `content/curriculum.json` (the attached file) is canonical. The two other versions pasted inline in the same message differ from it and from each other; they are ignored.
- **Q5. "Original substitute" exercises (20 of 91).** You found them online. The curriculum file itself calls them "not an externally sourced exercise", so their origin is unclear. They are kept out of rotation by default (`excluded`). With `--originals-dir`, each one is loaded from the bundle and waits in review with an `original` flag until you approve it.
- **Q6. Labs and worked examples (11 of 91).** Decided: kept out of the phone queue and listed on the `/desk` page.
- **Curator notes as errata.** Several notes correct the official key, for example "(1−0.4)e^{−2} ≈ 0.0812, not the printed 0.2696" (1.203J PS3 #1) or "should be y3/3000, not y1/3000" (15.053). The notes are passed to the grader as `curator_notes`, labelled as possibly wrong themselves, and shown to you next to the official solution.
- **Course style leaks the lens.** These problems come from about 8 courses, and each course maps to one or two lenses: 15.053 linear programs are shadow prices, 1.203J is queues, 18.098 is Fermi estimation. The notation alone will often give away the lens, the same way a chapter title would. That is another reason to treat the bank as training for *using* each tool and the drills as training for *noticing* (Q1).
