# Recall audit

PHDBOT treats official institution and funder pages as authoritative sources.
Third-party boards are useful as an independent **recall benchmark**, not as a
replacement source: a benchmark hit should lead back to the owning
institution's canonical vacancy URL before it enters the catalog.

## Benchmark panel

Use a rotating, stratified sample from:

- PhDScanner, PhDMap and PhDFinder public posts (manual/authorized review only);
- Academic Positions;
- AcademicTransfer for the Netherlands;
- FindAPhD for doctoral programmes;
- jobs.ac.uk for the United Kingdom;
- University Positions as a secondary cross-country check;
- EURAXESS as a control, not as an independent benchmark when PHDBOT already
  acquired the same EURAXESS record.

LinkedIn is deliberately not scraped by the production pipeline. Its posts are
mostly pointers to canonical listings, access is fragile, and automated access
would add duplicated records and policy risk without improving authoritative
detail quality.

## Per-refresh procedure

1. Sample 20–30 active opportunities across countries, fields, institution
   types and benchmark providers. Keep the benchmark URL, canonical URL, title,
   institution and observed closing date.
2. Look up the canonical URL in PostgreSQL first, then search by normalized
   title and institution. Do not judge recall from semantic rank alone.
3. Classify each miss at exactly one boundary:
   `catalog → discovery → schema → scrape → quality → index → retrieval`.
4. Repair the earliest failed boundary and rerun a bounded canary for that
   institution. Do not lower global quality gates to make one sample pass.
5. Record duplicate and stale benchmark posts separately. They are benchmark
   errors, not PHDBOT misses.

Track canonical-opportunity recall, current/searchable recall, false-active
rate and time to first searchable result. Raw processed-row counts are not a
success metric.

## Institution expansion

Research institutes and research centres enter the catalog only when Wikidata
identifies the research class, exposes an official website and ROR identifier,
and the entity has basic public notability. Curated gaps such as ISTI-CNR and
Fondazione Bruno Kessler remain explicit seeds. URL uniqueness prevents an
official portal from being attached twice when university and institute
catalogs overlap.

## Validated repairs, 2026-09-02

| Official source | Acquired cards | Current and indexed after the canary | Repair |
| --- | ---: | ---: | --- |
| [Imperial College London](https://www.imperial.ac.uk/jobs/search-jobs/) | 85 | 85 | Consume the public TalentLink feed used by the official board; retire old directory extractions. |
| [University of Turku](https://www.utu.fi/en/university/come-work-with-us/open-vacancies) | 26 | 26 | Consume the embedded TalentAdore feed rather than the page shell. |
| [ISTI-CNR](https://www.isti.cnr.it/it/comunicazioni/bandi) | 10 | 3 | Curated institute and official call-card schema. |
| [Fondazione Bruno Kessler](https://jobs.fbk.eu/) | 5 | 4 | Curated research foundation, job-table schema and explicit US date format. |

The exact Imperial institution filter with query `phd`, minimum score 0.60
and probable leads enabled returned 54 matches in that snapshot. This is a
retrieval count, not a count of doctoral vacancies: a semantic query can also
match postdoctoral and faculty descriptions. Use the position-type filter when
restricting the shortlist to doctoral opportunities.

The follow-up full index reconciliation added 1,039 official EURAXESS cards as
labelled provisional leads without another model review. These results prove
the repaired paths work; they do not establish overall recall or guarantee
that all opportunities have been found. Counts are dated snapshots and change
as jobs close. The stratified cross-provider benchmark remains the procedure
for measuring completeness.

## September 6 benchmark: remaining gaps

The dated input is [recall_sources.2026-09-06.json](../benchmarks/recall_sources.2026-09-06.json):
12 examples, including six primary-confirmed open examples, two expired controls and one
conflicting-deadline example. One open example is an EngD, not a PhD. Status is evidence as of
the recorded date, not a promise that an opportunity remains open today. This small targeted
sample cannot estimate whole-catalog recall.

- ETH Customer-Facing AI is acquired and indexed. Imperial Cardiac Imaging and StatML are
  absent at acquisition, not merely filtered out by relevance. Department studentships do not
  necessarily appear on central staff vacancy feeds. The cardiac page's direct fetch returned
  403; increasing model quality would not solve that access gap.
- Discovery previously spent its entire 30-link budget on homepage links even after fetching
  department hubs. Round-robin candidate selection now shares the same fixed budget between
  homepage, sitemap and hubs; `studentship` is also recognized. No extra fetch/model budget is
  introduced. This does not solve arbitrary multi-hop discovery or HTTP 403s.
- BI Norwegian Business School was missing from the catalog. Audit business-school admission
  criteria before widening taxonomy; do not include all entities of an unverified Wikidata class.
- A closed Twente opportunity has unknown-deadline indexed duplicates while its EURAXESS copy
  has the elapsed deadline. Cross-source identity/currentness needs evidence-aware reconciliation.
- FBK has unlinked institution aliases. One surviving alias contains a literal source deadline
  in year 2925. Do not assume a corrected year or blindly relink it as a valid current vacancy.
- Several sources were last refreshed August 25. Separate ordinary freshness gaps from missing
  catalog/source/schema paths before spending compute on review or embeddings.

Next: verify each sample at catalog → source → acquired row → published index → retrieval,
record the earliest failed boundary, and repair it with a bounded institutional canary. Preserve
source evidence and uncertain dates; do not turn aggregator labels into unverified ground truth.

### September 10: Copenhagen client-side pagination

Run #98's successful completion did not establish complete acquisition. A fresh read-only check
found the target Biomolecular Native Mass Spectrometry fellowship still absent from PostgreSQL.
The official PhD page contained 22 server-rendered table rows, but DataTables left only 10 in
the rendered DOM selected by the existing schema; the target was on a hidden page. The official
all-vacancies table contained 88 rows and also included the target.

The two exact official listing URLs now use a bounded raw-HTML adapter before browser extraction.
It reads all rows in one request, validates table columns, vacancy links and day-month-year dates,
and fails rather than treating malformed/blocked responses as empty successful refreshes. No new
LLM calls, generic table heuristics or database schema changes are involved. Other institutional
JavaScript-paginated tables still need independent audit; this fix is not a global completeness claim.
