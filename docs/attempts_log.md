# Attempts log

A record of every attempt made while building and running the project, including attempts that are
not part of the final run. All times are UTC. The machine-readable logs of the system's own runs are in
`runs/<run_id>/log.jsonl`. This file adds the attempts that happened outside those runs.

## 1. Runs of the system (notebook `brochure_pro.ipynb`)

| Run ID | Date / time | Companies | Model calls | Site requests | Result |
|---|---|---|---|---|---|
| `20261127-163931-b1fd` | 2026-11-27 16:39–16:40 | מחסני חשמל (`payngo.co.il`, id `MH`) | 0 | 1 (failed) | **Stopped.** The home page returned **HTTP 403**, so the notebook's check stopped the run before any candidate list, model call or further request. Replaced (see section 2). |
| `20261127-165418-3575` | 2026-11-27 16:54 – 2026-11-28 08:54 | אייבורי (`ivory.co.il`, A), לאסטפרייס (`lastprice.co.il`, B) | 8 (all attempt 1, all successful) | 8 (all HTTP 200) | Full run, **before the fixes** (kept as the "before" evidence). 5 of 6 outputs passed the checks; the comparison failed the source-list format check. Review found that the contexts were mostly site-wide menu text, so job and "about" content never reached the model. This led to two fixes: site-wide menu handling in the context builder, and the source-list format line in the comparison prompt. |
| `20261128-095326-2480` | 2026-11-28 09:53 | same | 8 (all attempt 1, all successful) | 8 (all HTTP 200) | **Not used.** Started right after the fixes were saved, but the Jupyter kernel still held the old code in memory (it was not restarted), so the run used the code from before the fixes. Its contexts and comparison prompt match the old code; the comparison failed the same check and `ivory_candidates` had 190 words. Outputs kept as a record only. |
| `20261128-095839-d673` | 2026-11-28 09:58 | same | 8 (all attempt 1, all successful) | 8 (all HTTP 200) | **Final run**, after restarting the kernel, with both fixes active. All 6 outputs passed the checks: 4 brochures of 233–271 words, the comparison, and the baseline. Tokens: 163,280 in, 17,089 out. |

Notes:
- The notebook's saved outputs only show the final run. Re-running the cells replaced the error output of the
  first run, and the record of that run is its `log.jsonl`.
- `runs/20261127-163931-b1fd/summary.json` was written afterwards from that run's existing log, because the run
  stopped before the summary cell. The log itself was not changed.
- Lesson from run `20261128-095326-2480`: after changing files in `brochure/`, restart the notebook kernel before
  running. Python loads the package once per kernel, and re-running cells does not reload it.
- Line endings in the three runs above: their `contexts/<name>.txt` copies were written with Windows newline
  conversion ("\n" became "\r\n"), so they are not byte-exact. The exact text sent is in `contexts/<name>.json`
  (and inside `prompts/<name>.json`). For all 18 files, undoing the conversion gives exactly the JSON text, and every
  context rebuilds exactly from the saved sources. Since the fix, `.txt` and `.md` files are written and read with
  no newline conversion.

## 2. Site readability check (before choosing the replacement)

On 2026-11-27, between the two runs, each candidate home page was fetched once with the project's
`scraper.fetch_page`, and the links were counted with `build_candidates`. These requests were made from the
terminal, outside any run, so they are not in a run log.

| Site | HTTP | Text (chars) | Candidate links | Decision |
|---|---|---|---|---|
| `www.payngo.co.il` (מחסני חשמל) | 403 | 0 | 0 | Not usable: blocked for the class scraper |
| `www.shekem-electric.co.il` (שקם אלקטריק) | 403 | 0 | 0 | Not usable: blocked |
| `ksp.co.il` (KSP) | 403 | 0 | 0 | Not usable: blocked |
| `www.machsanei-hashmal.co.il` | – | 0 | 0 | Not usable: the domain does not resolve |
| `www.bug.co.il` (באג) | 200 | 28,935 | 666 | Readable |
| `www.ivory.co.il` (אייבורי) | 200 | 69,006 | 721 | Readable: **chosen (company A)** |
| `www.lastprice.co.il` (לאסטפרייס) | 200 | 15,836 | 487 | Readable: **chosen (company B)** |
| `www.electra.co.il` (אלקטרה) | 200 | 3,916 | 40 | Readable but little text, and a manufacturing group rather than a retail chain |

## 3. Development test calls (not part of the submitted run)

While the code was being built, a few calls were made to check that each part worked. They used test data
(`edwarddonner.com`, the site from the class notebooks), and their files were written to a temporary folder
outside the project.

| Date / time | Purpose | Model | Tokens (in / out) | Duration | Result |
|---|---|---|---|---|---|
| 2026-11-27, before 11:51 | Check that the API key, the endpoint and the model work | `claude-opus-5-5` | 18 / 4 | 1.0 s | Replied "OK" |
| 2026-11-27 11:51 | Test the link selection call and its validation | `claude-opus-5-5` | 1,068 / 243 | 4.8 s | Accepted: 3 links |
| 2026-11-27 12:17 | Test a streamed brochure (customers) | `claude-opus-5-5` | 5,359 / 2,562 | 22.9 s | Complete, but 304 words (above 300), so the length rule in the prompt was made stricter |

Development requests to websites: about 10 requests to `edwarddonner.com` to test the scraper and the source
collection (one test URL returned HTTP 429, which was correctly reported as a failure), and 1 request to a
non-existent test host to check error handling. All other tests used fake pages and fake model responses, with
no network and no API calls (see `tests/` and `test_results/`).
