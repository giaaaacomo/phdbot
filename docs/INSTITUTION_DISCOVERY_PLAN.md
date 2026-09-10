# Missing opportunities: repair discovery and institution identity

Updated 2026-09-10. Scope requested by the user: discover official opportunities
automatically, including independent and jointly operated research centres.
Curated sources are regression fixtures and temporary coverage repairs, not the
main expansion strategy. No global scraping of LinkedIn is planned.

## Findings and implementation state

| Boundary | Evidence in the current code | State |
| --- | --- | --- |
| Catalog | Research entities required two Wikipedia sitelinks in addition to a research class, official website and ROR. | Deployed patch removes the popularity gate for research entities only; catalog not yet repopulated. |
| Catalog identity | Every `University` needs a Wikidata ID; ROR is only checked indirectly through Wikidata, not imported. | Still open. ROR/national-register-only entities cannot yet enter without a schema change. Never fabricate QIDs. |
| Geography | Catalog is limited to an explicit European country list, and an institution has one country. | Still open for jointly operated cross-border centres and opportunity-location filtering. |
| Discovery | `/en/` homepages produced `/en/sitemap.xml`; sitemap indexes and namespaced child documents were ignored. | Deployed bounded traversal: four requests, 2 MB each, same-site redirects only. |
| Discovery | Plain Jobs/Careers links failed candidate keywords; `site:www...` could exclude sibling department/job subdomains. | Deployed generic fixes; admissions hubs/project candidates also recognised. |
| Discovery failure | Invalid model JSON was interpreted as an empty selection and could defer retry for 30 days. | Deployed patch records a technical failure, not `no_listing`; existing listing rows remain intact. |
| Depth / identity | Four short homepage hubs, only one hop; cross-domain centre affiliation is not established automatically. | Still open. A partner's link is not sufficient to assign all its jobs to the referring university. |
| Source kind | Selection prompt excludes individual job pages. A centre publishing jobs as individual news posts may have no qualifying listing. | Still open. Add an explicit detail-source route rather than turning arbitrary news into listings. |

The existing model selection still asks for JSON text. Migrating it to validated
tool calls (with feedback on invalid parameters, as requested by Michele) remains
work to do; distinguishing failure from empty selection does not implement that.

## IPAL is a useful cross-border regression case

[IPAL's own description](https://ipal.cnrs.fr/about-us/) identifies it as CNRS
IRL 2955, based in Singapore, jointly involving CNRS, NUS and A*STAR, with further
French university partners. It should not be collapsed into a single university.
[Its homepage](https://ipal.cnrs.fr/) exposes a PhD on assistive micro-drones with
location IRIT / Toulouse, France, and NUS collaboration. Current application
status/deadline still needs verification; a visible post is not proof it is open.

Live ROR lookup resolved IPAL to **00m3mb357**, with Wikidata mapping
**Q51781972**, under the older name **Image and Pervasive Access Laboratory**.
Aliases IPAL / IRL2955 and the official website establish the identity; a search
using only its modern expanded name produced many irrelevant matches. ROR marks
it active, type `facility`, country SG, and lists CNRS as parent. Other partners
must retain their separate official-site provenance, not be invented as ROR
relationships. SQL confirmed no matching institution currently exists in PHDBOT.
See [the dated identity/discovery sample](../benchmarks/institution_sources.2026-09-10.json).
Direct HTTPS reads of IPAL failed certificate verification; do not disable TLS.

This demonstrates why source location, organisational affiliation and the job's
location must be separate. A Europe-only headquarters filter can miss a French
opportunity. IPAL was explicitly requested; do not silently expand the whole
catalog to Singapore or the world as a side effect.

Internal groups also need their own source pages explored. Attaching a group to
its university for display does **not** guarantee its posts appear on the central
university jobs board. The same caveat applies to the user's Rainbow example,
which has not yet been individually audited.

## Reference indexes, not hand-maintained lists of job pages

1. [ROR](https://ror.org/registry/) as an organisational identity source alongside
   Wikidata. ROR includes qualifying independent research institutes/laboratories
   but does not claim to enumerate every department or temporary project.
   Its [current schema](https://ror.readme.io/docs/ror-data-structure) supplies
   identifiers, aliases, websites, locations and parent/child/related links.
   Do not infer a 'research centre' merely from the broad `nonprofit` type.
2. [RNSR active research structures](https://data.enseignementsup-recherche.gouv.fr/explore/dataset/fr-esr-structures-recherche-publiques-actives/)
   as a complementary official French register with structure IDs and supervisory
   institutions. [The full RNSR dataset](https://data.enseignementsup-recherche.gouv.fr/explore/dataset/fr-esr-repertoire-national-structures-recherche/)
   also contains inactive structures: do not import them as active employers.
   IPAL's ROR identity is resolved; its RNSR record has not yet been checked.
3. Official research-network directories as bounded, provenance-bearing discovery
   entry points; e.g. [CNRS GDR IHM laboratories](https://gdr-ihm.cnrs.fr/laboratoires/)
   explicitly lists IPAL IRL2955. These can reveal omissions from identity registers.
   A directory entry is a candidate to resolve, not automatic proof of an open job.
4. Temporary European/private projects belong in a separate project/source layer
   linked to their partner organisations. Not every consortium is an independent
   institution. Identify a suitable official project registry in the next audit;
   do not import every company or funding scheme as an employer.

## Next atomic implementation blocks

1. Completed initial live repairs: #101 acquired StatML (index=0 because quality
   was still unknown); #102 ran quality/index, publishing position 103383;
   #103 acquired, quality-checked and indexed DTU position 103384. No review LLM.
   New source recovery needs **scrape -> quality -> index**, not scrape/index
   alone. The temporary command-approval failure cleared; no bypass was used.
   Generic StatML hub discovery finds the specific-projects candidate from its
   own homepage without curated seeds. Cross-domain discovery from Imperial and
   final model selection remain unverified; the sitemap endpoint returned 501.
2. Add a **read-only catalog preview** for a small ROR/RNSR sample: canonical IDs,
   websites, status, affiliations, candidate matches, unmatched/conflicting rows.
   Include IPAL, a single-university group, an independent foundation, a joint
   lab, an inactive entity and a temporary project. No GPU required.
3. After a verified restorable backup, migrate identity additively: preserve
   existing institution integer IDs and API compatibility; allow missing Wikidata
   IDs and add unique `(scheme, external_id)` identifiers with provenance.
   Relationships must be many-to-many and keep the exact relation asserted by
   the source. Match exact IDs first; a shared website/name/parent alone must not
   merge a university, institute and its lab.
4. Generalise the visible Catalog/Institutions stage, retaining the historical
   `universities` key for Resume/API compatibility. Import the audited preview,
   not the whole world. Use country-of-opportunity separately from headquarters.
5. Expand bounded discovery from official directory/relationship links and add a
   detail-source route. Keep source ownership and university associations
   separate; persist how each URL was found. Use validated model tool selection,
   not silent empty output, for ambiguous candidate selection.
6. Canary catalog -> source -> acquisition -> index -> search **without curated
   source seeds**, measuring unique current opportunities and compute cost.
   Cross-source dedup follows exact external job ID/canonical application URL;
   fuzzy title + partners flags a candidate match, never an automatic deletion.
   Preserve all source provenance and contradictory dates for inspection.

Acceptance: IPAL-like centres can be discovered from registries/directories;
internal groups remain findable under their parent; one joint job appears once
with multiple affiliations; genuinely different positions in a shared project
remain distinct. Unknown application status stays visibly uncertain. No claim
of complete coverage until a dated independent benchmark demonstrates it.

## Validated low-cost approach (2026-09-10)

Separate **organisation discovery** from **vacancy refresh**. Refresh a local
registry snapshot on a new release (monthly check is a proposed default), not
once per university or job run. ROR publishes a versioned
[JSON dump](https://ror.readme.io/docs/data-dump) with relationship metadata;
use that for broad coverage, keeping targeted API queries for small previews.
This avoids thousands of repeated lookups and gives a stable snapshot for joins.
ROR alone is not exhaustive: supplement missing identities with official
national/network directories, preserving provenance and avoiding name-only merges.

Implemented `pipeline.registry_probe`: read-only, parent-ID-based discovery with
SQLite page cache (30 days), explicit next-page cursor, maximum request/page/time
budgets and persistent HTTP-error cooldowns (at least one hour; respects longer
Retry-After). No LLM, organisation-site fetch, catalog import or production DB
write. It locally checks actual relationship edges, active status, names and
websites; a query hit alone is insufficient. Invalid/truncated responses fail
rather than masquerading as an empty registry. It never follows API redirects.

Live observations (single measurements, not throughput guarantees):

| Preview | Registry hits | Candidates read | Network requests | Elapsed |
| --- | ---: | ---: | ---: | ---: |
| CNRS relationships, headquarters SG | 4 | 4 | 1 | 0.316 s |
| Same query, cached / network budget zero | 4 | 4 | 0 | 0.053 s |
| CNRS relationships, no country filter, one-page budget | 1,221 | 20 | 1 | 7.998 s |

The SG query discovered **IPAL, CINTRA, MajuLab and BMC** without their names,
websites or child IDs in the query/code. SG is an explicit test slice, not a
production geographical rule: headquarters filtering can lose cross-border jobs.
The unfiltered preview stopped at page 1 with `next_page=2`; it did not crawl the
1,221 organisations. Registry hits are NOT counts of hiring labs or missing jobs.

Reproduce a bounded preview:

```sh
uv run python -m phd_searcher.pipeline.registry_probe --parent 02feahw73 --country SG
uv run python -m phd_searcher.pipeline.registry_probe --parent 02feahw73 --country SG --max-requests 0
```

Default cache: ignored `exports/registry-probe.sqlite3`. `--start-page` consumes
the reported cursor; the probe does not schedule itself or persist a work queue.
`complete` means the requested API query slice reached its final page, not global
coverage or a consistent multi-page snapshot. Live paging can drift even when
totals remain stable; use the versioned dump for real imports. The API's 10,000-hit
limit is surfaced, not silently interpreted as completeness.

Production integration still to implement, in this order:

1. Add identifier/relationship storage while preserving existing institution
   IDs and run compatibility (verified backup first). Match by exact registry
   identifiers; inspect conflicts. Retain multiple affiliations and provenance.
2. Add a persistent, bounded queue for **new/changed/uncovered** organisations
   and official directory links. Store last checked, next eligible time and
   cursor; rotate seeds fairly so the first large parent cannot starve the rest.
   No unbounded recursive expansion into all partners worldwide. Cross-border
   relationships may yield candidates without admitting every country to scope.
3. Send newly established websites to the existing bounded source discovery;
   distinguish job detail pages from listings and retain ownership evidence.
   Reuse known boards on normal job refreshes. Unchanged organisation metadata
   must not trigger another discovery/schema/review cycle. Retry failures with
   backoff, separately from the successful boards' freshness schedule.
4. Canary acquisition -> quality -> index with reversible provisional status.
   Do not deep-review every newly found centre or position. Measure **new unique
   searchable opportunities**, fetch time and GPU time separately. Deduplicate
   canonical job IDs/URLs while retaining university/centre provenance.

This proves the identity-discovery method, not end-to-end IPAL ingestion. Main
pipeline behaviour is unchanged by the preview. RNSR adapter, external-ID schema,
durable source queue, cross-border location policy and end-to-end dedup remain
open; no claim that every requested centre is already searchable.
