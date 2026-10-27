# Answer evaluation

About 40 questions with known answers, asked the way the chat asks them, against
the test documents in `test-documents/`. It catches the regressions unit tests
cannot: a Swedish question answered in English, one country's rule passed off
as another's, an invented answer where the documents say nothing, or the right
paragraph not making it into the answer.

Run it before merging anything that changes retrieval, chunking or the prompts:

```bash
docker compose up -d                          # the local stack
python3 eval/run.py --seed                    # once: upload and tag the test documents
python3 eval/run.py                           # ask, score, save eval/reports/<time>.json
python3 eval/run.py --compare eval/reports/<earlier>.json
```

- `--only anchor-day-se,mileage-se` runs just those cases; `--repeat 3` asks each
  three times, to tell a flaky case from a broken one.
- It talks to the query service on `localhost:8002` and ingestion on `:8001`
  with `SERVICE_API_KEY` from `.env` (never printed). `--seed` refuses anything
  but localhost: test documents never go to a real deployment.
- Its questions are sent with `"log": false`, so they stay out of the question
  log and the Dashboard's Insights.
- A run costs a few cents of LLM time. Reports stay local (`eval/reports/` is
  gitignored), since they quote the answers.

## What is checked

Each case in `cases.json` names its question, market (`null` for All markets),
the language the answer must be in, and what must hold:

| Check | Passes when |
|---|---|
| retrieval | one of `documents` is among the cited sources |
| include | every pattern matches the answer |
| exclude | no pattern matches it - e.g. Sweden's karensavdrag in a UK answer |
| language | the answer is in `lang` (a stop-word check; too short to tell counts as a pass) |
| not_covered | for questions the documents do not answer, the answer says so |

`fixtures.json` lists the test documents and how each must be tagged; the run
stops early if one is missing or tagged differently. Documents are found by
name, so a renamed test document counts as missing. The scoring and the case
file are unit-tested in CI (`eval/tests/`); asking the questions needs the
stack and real keys, so it stays a local command.
