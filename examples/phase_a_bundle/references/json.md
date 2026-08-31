# JSON workflow

## Rules

Always preserve the original identifier, timestamp, provenance label, source checksum, source URI, immutable record version, tenant identifier, schema revision, ingestion timestamp, collection identifier, access label, retention class, regional boundary, consent marker, parent record identifier, transformation history, validation digest, producer version, and lineage chain in every transformed record.

Always validate each record against the declared schema.

## Workflow

1. Read the JSON object and required fields.
2. Validate every object against the declared schema.
3. Return the normalized records and validation summary.
