# Data backup and public bootstrap

PHDBOT has two different data-distribution needs. They must not share the same
artifact or trust boundary.

## Private disaster-recovery backup

The operational database is the source of truth. It can contain full source
text, contact details, model evidence, feedback context, schedules and local
destinations, so a complete dump must remain private.

Recommended policy:

- create an online PostgreSQL custom-format dump every 6–12 hours during long
  runs and whenever a run reaches `done`, `stopped` or `failed`;
- encrypt on the client before uploading to private object storage or a private
  Drive target, using a tool such as restic or rclone crypt;
- store a manifest containing the UTC creation time, Git commit, Alembic head,
  completed/current run, aggregate row counts and SHA-256 checksums;
- keep the encryption/recovery key separately from the storage account;
- use rolling, daily, weekly and monthly retention and perform a restore test
  at least monthly;
- snapshot Qdrant after completed indexing when fast recovery matters. It is a
  rebuildable cache, so PostgreSQL plus a complete index reconciliation remains
  the authoritative recovery path;
- do not back up Ollama models, virtual environments or caches. Record the
  exact model name/digest and recreate them.

Backups are excluded under `backups/` and must never be added to Git history,
Git LFS or a public release.

## Public fast-start bootstrap

A public bootstrap can reduce the first-use cost substantially, but it must be
an application-level allowlisted export—not a database dump. A pilot may be a
versioned GitHub Release asset; a dedicated data repository or object-storage
bucket is preferable once releases become regular. Git history and Git LFS are
poor fits for rolling binary snapshots.

The portable core should contain compressed JSONL or Parquet tables for:

- institutions and their public identifiers;
- sanitized listing-source metadata;
- active or explicitly uncertainty-labelled public opportunity metadata.

Useful public fields include a stable ID derived from the canonical source URL,
institution, country, opportunity type, factual dates/compensation, source
domain/URL, first/last observation when known, verification tier and
uncertainty reasons. URL sanitization must remove tracking, session and secret
parameters without discarding identity-bearing public job IDs.

Exclude by default:

- full descriptions, raw HTML, names, email addresses, telephone numbers and
  unstructured contact text;
- user profiles, local reading history and export destinations;
- schedules, macros, pipeline checkpoints/errors and machine paths;
- feedback notes/context, review attempts, prompts and model output;
- raw extraction schemas and credentials.

Full text may be added only for sources whose terms and licence explicitly
allow redistribution. “Privacy-minimized” is a more accurate label than
“anonymous”: public vacancy pages can still contain personal data and protected
third-party text.

Every release must include a machine-readable manifest with:

- bootstrap format version and UTC data cutoff;
- completed pipeline run ID and producing Git commit;
- Alembic, URL canonicalization and source-family versions;
- table/record counts, field contract and per-file SHA-256;
- source attribution/licensing notes and takedown contact;
- for optional vectors: Qdrant version, embedding model/digest, dimension and
  point count.

The importer should offer three explicit modes: collect from scratch, import a
portable metadata bootstrap, or import a compatible fast bootstrap with
vectors. It must verify checksums and compatibility, then run an incremental
source refresh and final index reconciliation so a dated snapshot is never
presented as guaranteed current.

Do not publish a bootstrap until the source pipeline and index have completed
and an export audit has checked privacy, licences, counts and restore behavior.

## References

- [GitHub: About large files](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)
- [GitHub: About releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
- [GitHub: Git LFS billing](https://docs.github.com/en/billing/concepts/product-billing/git-lfs)
- [Qdrant snapshot compatibility](https://qdrant.tech/documentation/snapshots/)
- [European Commission: data-protection obligations](https://commission.europa.eu/law/law-topic/data-protection/information-business-and-organisations/application-gdpr_en)
- [EU database protection overview](https://europa.eu/youreurope/business/growing/protecting-intellectual-property/database-protection/index_en.htm)
