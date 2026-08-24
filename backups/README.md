# Local backups

This directory is intentionally excluded from Git, apart from this notice.
Database dumps, Qdrant snapshots and exports may contain operational state,
source text or personal contact details and must never be added to repository
history.

Use encrypted private object storage for disaster-recovery backups. Public
bootstrap data must be produced by a separate, versioned sanitization/export
process and distributed as a release artifact with a manifest and checksum.
