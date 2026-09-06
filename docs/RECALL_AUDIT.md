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
