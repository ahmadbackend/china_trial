# chinadrugtrials.org.cn bulk harvester

Automates what the recorded journey in
`capture_www.chinadrugtrials.org.cn_20261110-195919/` did by hand, across all
**37,197** registered trials: for each one it saves the official detail
document and a structured record of every field in it.

Built from the HAR of that journey, so it reproduces the site's own requests
rather than guessing at an API.

---

## Why it is built this way

The site sits behind a JavaScript-challenge WAF (the `FSSBBIl1Ugzb*` cookies and
the `MmEwMD=` query parameter). A plain HTTP client gets `HTTP 202` and an
obfuscated script instead of content, so:

| Stage | Tool | Why |
|---|---|---|
| Clear the WAF, mint cookies | **nodriver** (real Chrome via CDP) | only a real JS engine can solve the challenge |
| Do the ~39,000 requests | **curl_cffi** (`impersonate="chrome"`) | matches Chrome's TLS/JA3 fingerprint, far faster than driving a browser |

Cookies are re-minted automatically whenever they expire mid-run — a worker that
sees a challenge triggers one `nodriver` refresh that all threads then share.

**One important discovery:** the `?_export=doc` download is Word 2003 **XML**,
not a binary `.doc`. So a single request per trial yields both the downloadable
file *and* every detail field, fully parseable. There is no need to scrape the
HTML detail page as well, which halves the request count.

Also established by testing: the `pagesize` form field is accepted but **ignored**
(the server pins 20 rows per page → 1,860 pages), and `?_export=xls` exports only
the current page, not the whole result set.

---

## Install

```powershell
pip install nodriver curl_cffi lxml
```

## Run it

One command does everything, unattended:

```powershell
cd C:\Users\user-03\Downloads\china_trials
python -X utf8 scraper\run_all.py --gzip --workers 4
```

It will self-test, clear the WAF, walk the index, download every document, parse
them and write the exports — retrying failures and resuming cleanly. Stop it with
Ctrl-C and run the same line again whenever you like; nothing is re-downloaded.

Check progress at any time (no network needed, safe with the VPN off):

```powershell
python -X utf8 scraper\run_all.py --status
```

### Reachability — a VPN is not required

The site is served by a Chinese host, but tested from a non-Chinese IP (Egypt,
no VPN) a *fresh* handshake cleared the WAF, reported all 37,197 records and
downloaded documents normally. So a VPN is **not** needed by default. Note that
WAF cookies cannot bypass a geo-block even in principle — geo-blocking is
applied to the IP before cookies are examined — so if requests do start being
refused, a Chinese-exit VPN is the fallback. The harvester runs unattended
precisely so it can keep working while you are disconnected.

### Verifying what has been collected

`verify.py` audits every downloaded document, offline: sha256 and byte count
against what was recorded at download time, WordML well-formedness and the
closing tag (catching truncated transfers), the CTR number inside the file
matching the index, and the presence of every core field.

```powershell
python -X utf8 scraper\verify.py
```

Measured over 6,919 documents: **6,919 intact and parseable, 0 problems**, every
one carrying all core fields, 42–50 fields each (median 50), 16.4x compression.

### Is the document really everything?

Checked by diffing a live HTML detail page against the stored document: of 270
visible strings on the page, every **data value** was present in the document.
The only page-only content is navigation, breadcrumbs and the site's footer
address. Two apparent gaps turned out to be formatting, not loss:

- the HTML label `其他说明:` (the clarification shown when 试验分期 is 其它) is
  dropped, but its *value* is in the document, concatenated — e.g.
  `其它生物等效性试验`. Split on the known phase prefix if you need the two parts.
- an unfilled results summary shows as `暂未填写此信息` in HTML and as an empty
  table in the document. Same meaning.

So the single `?_export=doc` request per trial is sufficient; there is no need to
also store the HTML page.

### Measured rates

| Phase | Requests | Rate (4 workers) | Time |
|---|---|---|---|
| Index | 1,860 pages | ~60–80 pages/min | ~30 min |
| Documents | 37,197 docs | ~67–90 docs/min | **6–9 hours** |

Be gentle: 4 workers is deliberate. This is a public registry on modest
infrastructure — raising `--workers` much higher mostly produces truncated
transfers and retries, not speed.

### Disk

`--gzip` is strongly recommended and costs nothing analytically — the parsers
read `.doc.gz` directly:

| | 37,197 documents |
|---|---|
| raw `.doc` | **9.9 GB** |
| `.doc.gz` | **0.66 GB** (15x smaller) |

To hand a single document to Word again:

```powershell
python -X utf8 -c "import gzip,sys; open('CTR20264286.doc','wb').write(gzip.open(sys.argv[1],'rb').read())" out\docs\86\CTR20264286.doc.gz
```

---

## What you get

```
out/
  trials.db                 SQLite: the full dataset and all resume state
  session.json              current WAF cookies (short-lived)
  run.log                   append-only log of every run
  docs/<nn>/CTR########.doc[.gz]      the official documents, sharded by CTR suffix
  export/
    trials_index.csv        37,197 rows: the list-page summary + doc path + sha256
    trials_flat.csv         one row per trial, one column per common field  <- start here
    trials_full.jsonl       one JSON object per trial: all fields, sections, tables
    field_coverage.csv      every field name and how often it occurs
```

`trials_flat.csv` is the analysis table. `trials_full.jsonl` preserves structure
that a flat table cannot hold — the embedded sub-tables (study drugs, endpoints,
participating sites, ethics reviews) and the repeated investigator blocks.

### What is captured per trial

All seven sections of the registry record:

1. 题目和背景信息 — registration no., drug name/type, indication, titles, protocol no.
2. 申请人信息 — applicant, contact name, phone, email, address
3. 临床试验信息 — objective, design, phase, blinding, randomisation, age/sex,
   full numbered inclusion **and** exclusion criteria, study/control drugs,
   primary and secondary endpoints
4. 研究者信息 — every principal investigator (name, degree, title, phone, email,
   affiliation) and every participating site
5. 伦理委员会信息 — ethics committee reviews and dates
6. 试验状态 — status, target/actual enrolment, first-consent and completion dates
7. 临床试验结果摘要 — results summary versions

Each record also carries `text`, the whole document as plain text, for full-text
search, plus a sha256 of the original file for provenance.

---

## Scripts

| Script | Role |
|---|---|
| `run_all.py` | supervisor: runs every phase, retries, reports status |
| `selftest.py` | **offline** checks against the recorded capture — no network |
| `bootstrap.py` | nodriver: clears the WAF, writes `out/session.json` |
| `harvest_index.py` | walks the 1,860 result pages, records every trial GUID |
| `harvest_details.py` | downloads and parses each document |
| `parse_doc.py` | WordML → fields / sections / tables (also a CLI: pass it a `.doc`) |
| `export.py` | `trials.db` → CSV + JSONL |

Run the phases individually if you prefer:

```powershell
python -X utf8 scraper\harvest_index.py --workers 4
python -X utf8 scraper\harvest_details.py --workers 4 --gzip
python -X utf8 scraper\export.py
```

Useful flags: `--limit N` (smoke test), `--pages 1-50` (index slice),
`--retry-errors`, `--no-parse`, `--skip-index`.

### Verifying changes without the VPN

`selftest.py` validates both parsers against the fixtures in the recorded
capture and exercises the database and export paths — 45 assertions, no network:

```powershell
python -X utf8 scraper\selftest.py
```

Run it after editing anything. `run_all.py` runs it automatically before it
touches the network, so a parser regression stops the job instead of spoiling
hours of downloads.

---

## If something goes wrong

| Symptom | Cause / fix |
|---|---|
| `could not clear the WAF` | Chrome could not solve the JS challenge. Re-run; if it persists the site may be refusing your IP, in which case a Chinese-exit VPN is the fallback. |
| Chrome window opens mid-run | normal: cookies expired and are being re-minted. |
| `truncated doc` lines | partial transfers on a slow link; retried automatically, and `--retry-errors` mops up the rest. |
| Progress stalls | Ctrl-C and re-run — it resumes from the database. |
| Index ends below 37,197 | registry grew or pages failed; re-run `harvest_index.py`, failed pages are retried. |

Everything lands in `out/run.log`, which is safe to share once the VPN is off.

---

## Note on the data

This is a public registry (NMPA trial disclosure) and the harvester only
requests pages the site serves to any visitor, at a deliberately modest rate.
Documents are stored byte-for-byte with a sha256, so anything you publish can be
traced back to the original. Check the site's terms for how the data may be
redistributed.
