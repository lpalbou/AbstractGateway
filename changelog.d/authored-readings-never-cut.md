### Fixed

- **An authored summary is never shown as cut** (Castor E2E 2026-11-10: two visit-close summaries the summaries
  capability authored at formation served `cut: true`). A reading whose `digest_method` is `authored:<capability>`,
  whose record carries a `metadata_authored` stamp covering the summary, or whose formation provenance is
  `authored:<capability>`, is whole, whatever writer formed the record.
